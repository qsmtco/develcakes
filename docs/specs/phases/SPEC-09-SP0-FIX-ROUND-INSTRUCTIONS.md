# SPEC-09 SP0 FIX ROUND — audit BUG #1–#7 (Debugger, 2026-10-02)

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Supervisor ruling (BUG#4 design):** trusted roots = system bins ∪ realpath(HOME)
∪ app-venv bin (app/worktree ONLY — fixes BUG#3) ∪ checked-project venv bin. The
gate's contract is "project-supplied config cannot falsify validation," not host
hardening — document this in the module docstring.

## BUG #1 (bug) — site-packages symlink false-PASS

`_detect_venv_prefix`: after the existing dir+python checks, ALSO resolve the
venv's site-packages (parse `pyvenv.cfg` for the version, or glob
`lib/python*/site-packages`) and realpath it. A non-app/non-worktree project whose
site-packages resolves inside `<app>/.venv` → refuse (None + warning naming the
surface). Also realpath-validate `pyvenv.cfg`'s `home`/`executable` targets — a
home pointing into the app venv is the same claim.
**Tests:** foreign venv (own pyvenv.cfg) + site-packages symlink → probe's exact
shape → `_resolve_tests_python` None AND end-to-end `check()` FAILED (the
false-PASS regression). Control: own venv fully copied → runs.

## BUG #2 (bug) — dead clause-2 + fixture mismatch

Clause-2 (`realpath(python) == join(app_venv, "bin", "python")`) compares a
realpath'd LHS to a non-realpath'd RHS — unreachable for real venvs (bin/python is
a symlink chain landing in /usr/bin). REPLACE the literal comparison with
containment: refuse when `realpath(python)` (or the venv dir) resolves inside
`app_venv_dir` for non-app projects — the dir-level containment is what catches
chains. **Re-fixture the existing test** to build `bin/python` as a real symlink
(mkdir bin; ln -s /usr/bin/python3 bin/python) — the current write_text fixture
fabricates an impossible shape. RED-then-GREEN: dead clause can't pass the
symlinked fixture.

## BUG #3 (bug) — root (b) scope leak + non-fail-closed refusal

1. `_resolve_binary_roots`: append `dirname(sys.executable)` ONLY when
   `realpath(project_path) == _APP_ROOT or _is_app_worktree(project_path)`.
   Foreign projects never run on the app interpreter via root (b).
2. `_resolve_tests_python` returning None (V2 refusal) must NOT fall through to
   bare `python3` + PATH resolution. The caller (`_check_tests`) treats a
   refused-venv as a visible FAILED tier (detail: "test interpreter refused
   (venv validation): <reason>") — no execution attempt.
**Tests:** the auditor's two probes as tests: foreign + no venv + app bin on PATH
→ tests tier FAILED not passed (nh3 marker untouched); foreign + dir-symlink venv
(refused) → FAILED tier, not PATH-resolved run.

## BUG #4 (bug) — user-dir tooling over-refusal (host regression)

Per the ruling: roots gain `os.path.realpath(os.path.expanduser("~"))` — the
operator's HOME subtree is trusted. Implementation note: containment check
`commonpath([resolved, home]) == home` (same pattern as the worktree parent).
The module docstring documents the threat-model ruling (one short paragraph).
**Tests:** linter binary in a tmp dir UNDER a fake HOME (monkeypatch expanduser)
→ runs; binary in /tmp → refused (the shim catch stays); binary in project dir
outside venv bin → refused.

## BUG #5 (bug) — refused syntax cascades tiers off

A REFUSED syntax check: still a visible FAILED check, but GATE-NEUTRAL for the
tests/lint tiers (same treatment as `SKIPPED:` in the `syntax_gate` decision).
One condition change + comment.
**Test:** the cascade probe's shape — refused syntax (fake HOME-less python3
shape) → tests and lint tiers BOTH still ran.

## BUG #6 (issue) — `command` field bypasses the token allowlist

`_check_tests`: the `test_config.command` branch routes the string through
`_validate_test_command` BEFORE argv parsing (same as `full_suite_command`).
**Test:** `command: "sh -c 'touch /tmp/marker'"` → refused, marker untouched.

## BUG #7 (issue) — docstring vs behavior

`_is_app_worktree` docstring: state the ACTUAL rule — lexical/realpath membership
of the direct-child slot (existence NOT checked; phantom children pass by design,
test_real_worktree_without_git_still_passes_gate pins it); drop the
"symlink planted elsewhere does not confer" claim that the implementation
contradicts (a symlink whose realpath lands in .worktrees DOES confer).

## BUG #8 — stays REGISTERED (skip-DoS probes). No code.

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
xvfb-run -a .venv/bin/python -m pytest tests/test_enforcement.py -v
.venv/bin/python -m pyright agent/enforcement.py
python -m ruff check agent/enforcement.py tests/test_enforcement.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] BUG#1: site-packages + pyvenv.cfg realpath validation + the false-PASS e2e regression test
- [ ] BUG#2: containment replaces the dead clause + symlinked-fixture re-test (RED-then-GREEN)
- [ ] BUG#3: root (b) identity-gated + refusal → visible FAILED tier (both probe tests)
- [ ] BUG#4: HOME in trusted roots + threat-model docstring + fake-HOME test trio
- [ ] BUG#5: REFUSED gate-neutral for downstream tiers + cascade test
- [ ] BUG#6: command routed through the token allowlist + sh -c test
- [ ] BUG#7: docstring matches behavior
- [ ] Full battery green incl. full suite with enforcement
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
