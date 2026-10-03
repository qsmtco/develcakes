# SPEC-09 SP0 FIX ROUND 3 — re-audit: glob-metachar (HIGH), path-normalization (MED), docstring

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Scope note:** the auditor confirmed rounds 1–2 correct on their own terms; these
three live in round-2's new code + one stale docstring. Surgical.

## BUG #15 (HIGH) — glob metacharacter evasion in `_venv_site_packages_all`

`glob.glob(os.path.join(venv_abs, "lib", "python*", "site-packages"))` treats
`venv_abs` as a PATTERN. Project-supplied `venv_path=".venv[1]"` (via
enforcement.json) → `[` opens a character class → glob returns [] → claims empty →
false-PASS (auditor's probe: tests tier passed with `import nh3`).

**Fix:** escape the literal base at BOTH glob sites (:621/:624):
`glob.glob(os.path.join(glob.escape(venv_abs), "lib", "python*", "site-packages"))`
(same for lib64). The cfg-`version` candidate path construction stays literal
(join, no glob).
**Tests:** the auditor's fixture: foreign project, app-linked site-packages,
`enforcement.json` carrying `"venv": ".venv[1]"` — wait, check how venv_path
reaches `_detect_venv_prefix` from config (the auditor's probe passed it; mirror
its wiring exactly). RED-then-GREEN: `.venv[1]` → REFUSED naming site-packages.
Add a `*` variant (`.venv*old`).

## BUG #16 (MEDIUM) — literal-vs-realpath asymmetry: app-env check vs root (c)

The app-env refusal compares LITERAL `dirname(resolved)` against the realpath'd
app venv (:1000) while root (c) is realpath'd (:957/:1027). Foreign project with
`.venv → <app>/.venv`: root (c) = realpath(`<proj>/.venv/bin`) = the app venv bin
→ a REAL app binary admitted; the literal dirname check misses the indirection.
Auditor's e2e: foreign syntax tier executed a planted `<app>/.venv/bin/python3`.

**Fix — normalization must AGREE:**
1. App-env refusal: `os.path.realpath(os.path.dirname(resolved))`.
2. Root (c): require the binary to be literally inside the CHECKED PROJECT —
   `realpath(project)/.venv/bin` — AND resolve (realpath) inside that dir. The
   symlinked-venv foreign project then fails root (c) (realpath of project/.venv
   is NOT inside realpath(project)) AND hits the app-env refusal. The SP2
   contract (app worktree + symlinked venv ALLOWED) must still pass — worktrees
   are app projects (identity-gated root (b)), not root (c) consumers.
**Tests:** auditor's probe shape end-to-end: foreign + `.venv → app venv` +
planted marker binary via PATH → syntax tier REFUSED, marker untouched. Control:
the SP2 worktree case still allowed (probe_sp0fixround13 shape).

## BUG #17 (suggestion) — stale docstring

`_detect_venv_prefix` :389 says refusal → "tier skipped"; round-1 BUG#3 made it a
visible FAILED tier (:1153). Reword to match.

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
xvfb-run -a .venv/bin/python -m pytest tests/test_enforcement.py -v
.venv/bin/python -m pyright agent/enforcement.py tests/test_enforcement.py
python -m ruff check agent/enforcement.py tests/test_enforcement.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] BUG#15: glob.escape both sites + metachar tests (RED-then-GREEN, `[` and `*` variants)
- [ ] BUG#16: realpath'd app-env check + literal-project root (c) + e2e symlinked-venv refusal + SP2-worktree control still green
- [ ] BUG#17: docstring reworded
- [ ] Full battery green incl. full suite
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
