# TEST-DEBT-3 — Instructions (Coder)

**Goal:** Close 12 of the remaining 14 baseline failures. Supervisor triage
(2026-09-12, tree at 9ebbefa) — verify by reading, do not re-derive.

## CLUSTER 1 — 11 failures, ONE ROOT CAUSE — TEST-SIDE (stale mock signature)

`agent/runtime.py:1486` calls `self._call_llm(session_key, messages_for_call,
tools, turn_token=turn_token)`. Tests patch `_call_llm` with local fakes whose
signature predates the `turn_token` kwarg → `TypeError: ... got an unexpected
keyword argument 'turn_token'` → the callback never fires → downstream
`IndexError` / `assert 0 == 1` cascade.

Affected (verified by running each suite):
- `tests/test_auxilium_tier2.py` — 6 failures (`fake_call()` in
  TestKBContextInjection / TestKBLookupFiresForAuxilium)
- `tests/test_runtime_fallback.py` — 4 failures (`mock_call_llm()` in
  TestFallbackOneShot / TestFallbackOnOutOfScope / TestNoFallbackWithoutConfig
  / TestFallbackModelDerivation)
- `tests/test_kb_integration.py` — 1 failure (`mock_call_llm()`)

**Fix:** add `**_kwargs` (or an explicit `turn_token=None`) to each fake's
signature. This is the same "mock drifted from the real call signature" class
as TEST-DEBT-2 cluster 1 — and the same remedy shape (accept the kwarg; don't
weaken what the test asserts). Do NOT change `agent/runtime.py`.

**Watch for a hidden second defect** (the E-cluster-1 lesson): after fixing the
signature, re-run and check whether any test now passes for the WRONG reason or
still fails because a *second* drift hid behind the TypeError. Report per test.

## CLUSTER 2 — 1 failure, PRODUCT QUESTION (do the minimal safe fix)

`tests/test_mcp_config.py::TestToStdioParams::test_env_var_substitution` fails
because MED-12 (`utils/mcp_config.py:30`) added an allowlist
`_MCP_FORWARDABLE_ENV_VARS = {PATH, HOME, LANG, VIRTUAL_ENV, PYTHONPATH}`;
`TEST_MCP_TOKEN` is refused → `env` empty → `None` → `TypeError`.

DO THIS: fix the TEST to use an allowlisted var (e.g. `PATH` or `LANG`) and
assert the substitution + refusal behavior explicitly — i.e. two tests:
(a) an allowlisted var IS substituted; (b) a NON-allowlisted var is refused
(`env` is None/absent) with the MED-12 warning logged. That pins the current
security control as intended behavior instead of leaving it untested.

DO NOT change `utils/mcp_config.py`. The allowlist's product adequacy is a PM
question (below) — do not resolve it in code.

## CLUSTER 3 — 2 failures — DO NOT TOUCH (needs PM product decisions)

- `tests/test_architecture.py::test_views_do_not_import_handlers` — real
  violation at `ui/views/left_panel.py:15` (runtime view→handler import).
  Fixing means moving production code; out of scope here.
- `::test_utils_gtk_imports_are_documented` — `ui/gtk_containers.py` carve-out
  is undocumented; needs a doc/product decision.

Leave both red. Note them in the report as deferred.

## PM QUESTIONS (put in your report, do not act)

1. **MED-12 allowlist over-restriction** — the allowlist permits only
   PATH/HOME/LANG/VIRTUAL_ENV/PYTHONPATH, so every user-defined credential var
   (e.g. `${GITHUB_TOKEN}`) is refused. Passing credentials to MCP servers is
   the primary purpose of `env:` in MCP config. Recommend switching to a
   DENYLIST of dangerous vars (LD_PRELOAD, LD_LIBRARY_PATH, PYTHONSTARTUP,
   PYTHONPATH-ish, BASH_ENV, …) to preserve the security intent while restoring
   function. Needs PM sign-off — it is a security control.

## GATES

- Clusters 1+2 green, run TWICE consecutively (flake gate).
- Full suite: baseline is **14F/3618P**. Expected after this unit:
  **2F** (the two architecture tests) — report the actual count + list.
- pyflakes 0 undefined on touched files. Hermetic (test_auxilium_tier2 touches
  KB paths — do NOT let it write a real KB index; the existing suite pattern
  handles this, keep it).
- Commits: one per cluster (cluster 1; cluster 2). Conventional messages.
- Report COMPLETENESS per cluster + red/green evidence + the 2 PM questions.

Then STOP.
