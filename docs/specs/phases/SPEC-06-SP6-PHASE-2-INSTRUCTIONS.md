# SP6 Phase Instructions — Phase 2: 12-red triage (environment debt)

**SPEC-06 close-out · SP6 Phase 2 of 3. Env: project `.venv` (THE env).**
**HEAD at delegation: c4b8c713 (clean). Builder: steelFramedCodeWriter.md every turn.**

## Diagnosis (supervisor probes)

Three clusters, all environment-shaped:

**Cluster A — tests/test_enforcement.py (3):** the enforcement hook shells out to
`/usr/bin/python3 -m pytest`, which has no pytest (PEP 668 system python). The
hook's test-command must resolve the PROJECT venv when run from the project.

**Cluster B — tests/test_mcp_config.py (9):** `utils/mcp_config.py:84` imports
`from mcp import StdioServerParameters` — `mcp` is not installed and not a
declared dependency. SURVEY FIRST: is `mcp` (a) still a used feature in
production code paths, or (b) dead since the R5 KB removal? Grep all imports of
utils/mcp_config and the mcp module; check spec history.

**Cluster C — 8 ERRORs in test_feed_retention.py:** standalone the class passes
(72s, green — supervisor-verified). ERRORs only appear in the full-suite run =
fixture/worker state issue, not test logic. DO NOT touch the test file.

## Phase 2 tasks

### 2A — enforcement venv resolution
In the enforcement module (find the check-runner that builds the test command):
resolve python as the CURRENT interpreter (`sys.executable`) when the target
project is the running project, or probe `.venv/bin/python` adjacent to the
checked file's project root before falling back to `sys.executable`. Prefer the
minimal change that makes the 3 tests pass honestly (they assert a passing
`tests/` tier on a demo file). Do NOT make the hook call the venv when running
against OTHER projects without a venv probe.

### 2B — mcp disposition (survey-gated)
- If mcp is dead code post-R5: mark the 9 tests `pytest.importorskip("mcp")` (or
  module-level skip with reason "mcp dependency removed with R5; conversion
  path unexercised") and file a register entry to delete utils/mcp_config.py in
  a dedicated dead-code sweep. Do NOT delete production code in this phase.
- If mcp is live: add `mcp` to pyproject deps (pin to a venv-installable floor)
  and install into the venv; tests must then pass for real.

### 2C — retention ERRORs
Register-only: document that TestTwoThousandCardRetention passes standalone
(72s) and ERRORS only under full-suite parallel context; recommend running it
with `-p no:cacheprovider` isolation or as a marked `slow` separate gate.
No code change. Report the recommendation text for the post-mortem.

## Gates

1. Cluster A: `xvfb-run -a .venv/bin/python -m pytest tests/test_enforcement.py -q`
   → 44 passed, 0 failed.
2. Cluster B: disposition applied; `pytest tests/test_mcp_config.py -q` → 27
   passed, 27 skipped (or 27 passed if live path chosen) — report exact.
3. Cluster C: no file change; standalone re-run green (paste timing).
4. Ruff/pyright exact baselines on touched files; `git status` sanctioned; no commit.

## Report format
COMPLETENESS mandatory; survey evidence for 2B (imports grep + spec reference);
related issues flagged not fixed.
