# SPEC-09 SP0 — Enforcement Hardening (PATH-bleed, fake-venv, worktree gate)

**Spec:** SPEC-09 pre-flight decisions §B-D (PM approved (b) + all riders, 2026-10-02).
**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Touches:** `agent/enforcement.py`, `utils/env_security.py`, tests. Read-only probes
first — this file is security-sensitive (CRIT-1/CRIT-2 surface).

---

## Verified live vectors (probe them yourself first; reproduce before fixing)

1. **PATH-bleed (V1):** `_get_scrubbed_env()` forwards the USER's PATH verbatim
   (verified: full user PATH including ~/.local/bin survives). Enforcement subprocesses
   (`_run_timed_command` :779) resolve binaries through it → a `python3`/`pytest`/`ruff`
   shim planted in ANY user-writable PATH dir shadows the allowlist's decision and
   executes attacker-controlled code with the scrubbed (but real) env. The allowlist
   checks the FIRST TOKEN of the command string — not the resolved binary path.
2. **Fake-venv via symlink (V2):** `_resolve_tests_python` (:532-563) guards the
   sys.executable fallback with `realpath(project_path) != _APP_ROOT`. A foreign
   project whose `.venv/bin/python` is a SYMLINK to develcakes' venv python:
   `_detect_venv_prefix` returns the foreign path, `venv_python` is not None →
   the fallback guard is never reached, and the foreign tier runs on develcakes'
   deps. The venv_path itself is never realpath-validated.
3. **No worktree gate (V3):** `_APP_ROOT` compare is a single-path equality. Under
   SPEC-09 SP2, agents exec inside `<repo>/.worktrees/<agent>` — realpath(worktree) ≠
   _APP_ROOT → the identity gate DENIES a legitimate in-app worktree (false-FAIL),
   or worse, any directory whose realpath happens to match. Worktrees need an
   explicit, tested membership rule.

## Edit 1 — V1: resolved-binary allowlist (PATH hardening)

`_run_timed_command` (and any other enforcement subprocess site — grep them all):

- After `_parse_command_to_argv`, resolve the FIRST argv token via
  `shutil.which(token, path=scrubbed_PATH)`. If `which` returns None → fail-closed.
- Compute the resolved path's realpath; require it to be **inside one of**:
  (a) a system bin dir (`/usr/bin`, `/usr/local/bin`, `/bin`, `/sbin`,
  `/usr/sbin` — realpath'd, from a module constant), or
  (b) the RUNNING interpreter's dir (`os.path.dirname(sys.executable)` — the
  develcakes venv bin), or
  (c) the CHECKED project's venv bin (`<project_path>/.venv/bin`, realpath'd).
- ELSE: refuse with a clear error naming the resolved path and the violation
  ("binary resolves outside allowed roots — PATH shadowing refused").
- The token allowlist STAYS (defense in depth: token check first, then
  resolution check).
- Tests: plant a shim `pytest` in a tmp user-PATH dir (write a script that
  touches a marker file and exits 0), set scrubbed PATH with that dir FIRST,
  run the tier → REFUSED, marker NOT touched. Control: same command with clean
  PATH → runs. Also: relative-dir `.` in PATH, PATH containing the project
  venv bin → allowed via (c).

## Edit 2 — V2: realpath-validate the venv python

`_detect_venv_prefix` (:287) and every consumer:

- After detecting `<project>/.venv/bin/python`, `os.path.realpath()` the
  python path AND the venv dir. If `realpath(venv_python)` resolves INSIDE
  `_APP_ROOT/.venv` while `realpath(project_path) != _APP_ROOT` → REFUSE
  (foreign project claiming our interpreter via symlink). Return None so the
  tier fails as designed (fail-closed, same as BUG #1's probe outcome).
- Tests: foreign project + `.venv` symlinked to develcakes' venv →
  `_resolve_tests_python` returns None (not sys.executable). Control: foreign
  project with its OWN venv (copied, not linked) → its python is used.

## Edit 3 — V3: worktree-aware identity gate (design D2, tested now)

Add to enforcement.py:

```python
def _is_app_worktree(project_path: str) -> bool:
    """True when project_path is a git worktree OF the running app's repo.

    Membership rule: realpath(project_path) is a direct child directory of
    realpath(<_APP_ROOT>/.worktrees) — the SPEC-09 layout
    (<repo>/.worktrees/<agent_id>). Anything deeper, sibling, or elsewhere
    is NOT an app worktree. The .worktrees parent must itself resolve under
    _APP_ROOT (no symlink escape).
    """
```

- `_resolve_tests_python`: the identity check becomes
  `realpath(p) == _APP_ROOT or _is_app_worktree(p)`.
- Tests: (a) tmp dir that is NOT under the app → False; (b) a REAL tiny git
  repo + `git worktree add` under `.worktrees/test-agent` → True; (c) a
  sibling dir named like a worktree but outside `.worktrees/` → False;
  (d) symlink into the worktrees dir from elsewhere → False (the parent must
  resolve under _APP_ROOT through its own realpath).
- NOTE in the docstring: SP2 wires the runtime to exec inside worktrees;
  this gate is the contract SP2 builds on — it must exist and be tested
  BEFORE SP2's integration.

## Edit 4 — registry + doc updates

- context.md note: the SP6 "env-broken reds" for test_enforcement are HEALED
  (51 passed/1 skipped today) — the standing ignore of that file in
  verification batteries can be lifted next full-suite run (report the count
  when you do; if any red returns, re-register it, don't force).
- `docs/specs/SPEC-09-PREFLIGHT-DECISIONS.md` — no edit needed (yours truly).

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
xvfb-run -a .venv/bin/python -m pytest tests/test_enforcement.py -v
.venv/bin/python -m pyright agent/enforcement.py utils/env_security.py
python -m ruff check agent/enforcement.py utils/env_security.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -3
```

NOTE: this full-suite run INCLUDES test_enforcement (the heal check rides it).

## COMPLETENESS (mandatory)

- [ ] V1 probe reproduced (shim executes today) BEFORE the fix — paste the marker-touch evidence
- [ ] V1: resolved-binary root allowlist + shim-refused test (marker untouched)
- [ ] V2 probe reproduced (symlinked venv passes today) BEFORE the fix
- [ ] V2: realpath validation + refusal test + own-venv control
- [ ] V3: _is_app_worktree + 4-case test matrix (incl. a real git worktree)
- [ ] Full battery incl. test_enforcement + full suite WITH enforcement included
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
