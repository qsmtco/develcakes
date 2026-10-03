# SPEC-09 SP0 FIX ROUND 2 — re-audit BUG #9–#13 (Debugger, 2026-10-02)

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Rulings in force (top of brief — read first):** (10) project-containment OVERRIDES
all trust roots (a resolved binary inside the checked project's realpath is refused
unless inside that project's `.venv/bin`); (12) mirrored-interpreter refusal stays
conservative but the detail must be truthful; (13) hop-cap truncation = claim
(fail-closed); (14) registers into SP2.

## BUG #9 (HIGH) + BUG #11 — scan ALL site-packages candidates

`_venv_site_packages` early-returns the FIRST isdir hit; a decoy
`lib/python3.10/site-packages` + lying pyvenv.cfg `version` evades the scan (probe:
false-PASS with `import nh3`). Also only `lib/` is globbed (lib64 hosts missed).

**Fix:** replace the single-path helper with a candidate ENUMERATOR:
`_venv_site_packages_all(venv_dir) -> list[str]` returning EVERY existing
`lib*/python*/site-packages` (glob `lib` AND `lib64` bases) PLUS the cfg-`version`
candidate — no early return, dedup'd. `_venv_app_claims` realpaths and checks EACH;
any containment hit → refuse with that surface named.
**Tests:** the auditor's decoy probe as `test_b9_decoy_site_packages_refused` —
real `python -m venv`, REAL app-linked site-packages under the version the cfg
lies about, decoy lower-version dir first in sort order → tests tier FAILED, and
the victim test's `import nh3` did NOT run (assert via the probe's marker/output
shape). Plus a lib64 fixture: venv-shaped dir with only `lib64/python3.12/...`
linked → refused.

## BUG #10 (HIGH) — project-containment overrides trust

**Fix:** in `_validate_resolved_binary`, BEFORE the roots check: if
`commonpath([resolved_real, project_real]) == project_real` and the resolved path
is NOT inside `<project_real>/.venv/bin` → REFUSE ("binary resolves inside the
checked project (outside its venv bin) — project-supplied binaries are not trusted
for validation"). project_real = realpath(project_path). This rule runs FIRST;
then the existing roots (system ∪ HOME ∪ app-venv-if-app ∪ project-venv-bin).
**Tests:** the auditor's probe as `test_b10_project_local_binary_refused_under_home`
— project under a FAKE HOME (monkeypatch `os.path.expanduser` or HOME env;
realpath'd), `<proj>/tools/pytest` via enforcement.json command → REFUSED, marker
untouched. Rebuild `test_b4_project_dir_outside_venv_bin_refused` to place the
project under the fake HOME too (its /tmp-only placement encoded the false
invariant). Control: `<proj>/.venv/bin/pytest` → still admitted (root (c) intact).

## BUG #12 (issue) — truthful refusal detail

The hop-walk refusal detail currently asserts "claims the app environment" — false
when prefix/site-packages are local. Reword to:
`"interpreter symlink chain enters the app venv ({path}) — conservative refusal;
if this venv is genuinely independent, give it its own interpreter binary"`.
Keep the refusal itself. **Test:** positive-control pin: foreign venv, own cfg +
own site-packages, bin/python → app venv's bin/python → still REFUSED (pin the
conservative behavior so a future loosening is deliberate) with the new wording
asserted.

## BUG #13 (LOW) — cap truncation is fail-closed

`_symlink_chain`: on hitting the hop cap, do NOT return silently — record a
truncation marker; `_venv_app_claims` treats a truncated chain as a CLAIM
("symlink chain exceeded {N} hops — treated as claiming the app environment").
Cap stays 40 (cycle-set bounds termination; the marker converts miss→refuse).
**Test:** 41-hop chain → refused with the truncation reason (probe_sp0fixround10's
CAP shape).

## BUG #14 — NO code. Registered for SP2: worktree wiring sets project_path to
the REAL worktree path, never a symlink into `.worktrees`. (Add one line to
`_is_app_worktree`'s docstring noting SP2's obligation.)

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
xvfb-run -a .venv/bin/python -m pytest tests/test_enforcement.py -v
.venv/bin/python -m pyright agent/enforcement.py tests/test_enforcement.py
python -m ruff check agent/enforcement.py tests/test_enforcement.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] BUG#9+#11: multi-candidate site-packages scan (lib+lib64, no early return) + decoy test + lib64 test
- [ ] BUG#10: project-containment override + under-fake-HOME tests (new probe test + rebuilt b4 control) + project-venv-bin control
- [ ] BUG#12: truthful detail + conservative-behavior pin
- [ ] BUG#13: truncation-as-claim + 41-hop test
- [ ] BUG#14: docstring line (no code)
- [ ] Full battery green incl. full suite
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
