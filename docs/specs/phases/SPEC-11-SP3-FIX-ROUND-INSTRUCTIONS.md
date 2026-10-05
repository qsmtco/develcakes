# SPEC-11 SP3 Fix Round — theme-named icons, real-call wiring pin, README corrections

**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** pyproject.toml + icons/ + tests/test_app_identity.py + README.md.

---

## BUG #1 (MED) — packaged icons can't resolve `develcakes`

data-files installs `16.png..256.png` — theme lookup needs basename
`develcakes`. Auditor wheel-verified: "Entries that satisfy Icon=develcakes:
NONE."

### Fix

Add theme-named copies (do NOT rename the originals — other code may read
them; check first, disclose): `icons/hicolor/{16x16,32x32,48x48,128x128,
256x256}/apps/develcakes.png` — byte-identical copies of the size PNGs.
Point `[tool.setuptools.data-files]` mappings at the theme tree (the old
mappings go unless something else installs them — disclose).

### Tests

`test_icon_theme_packaging_wired` STRENGTHENED: read pyproject, resolve the
data-files source paths, assert each exists AND `os.path.basename(p) ==
"develcakes.png"`. (The current `"icons" in str(...)` shape is false
assurance — replace it.) Plus a wheel-content check if buildable in-test
(or a documented manual check — disclose).

## BUG #2 (MED) — the wiring pin asserts a comment

`MAIN.index("migrate_v1_config()")` finds the comment at main.py:115.
Auditor: reversed order → PASS; call deleted → PASS.

### Fix

Pin the exact statement and its order vs app construction:

```python
    call_idx = MAIN.index("report = migrate_v1_config()")
    app_idx = MAIN.index("app = DevelcakesApp()")
    assert call_idx < app_idx, "D3: migration must run BEFORE the app/window build"
```

RED proofs: (a) reorder the two statements in a temp copy → fails;
(b) delete the call → fails (`ValueError` from .index on the statement —
catch and fail with a clear message, or assert the substring exists first).

## BUG #3 (LOW) — README dropped LIVE row STT_MODEL_SIZE

`utils/stt.py:92` still reads it (not a renamed-family var — my sweep
instruction over-reached). Restore the row.

## BUG #4 (LOW) — migration sentence now self-referential

"your v1 `~/.config/develcakes/` is copied to `~/.config/develcakes/`" —
restore the SOURCE as `~/.config/crabcakes/`.

## BUG #5 (LOW) — project-layout names files that don't exist

`develcakes-commands.md` / `develcakes-context.md` / `develcakes.yaml` —
revert to the real v1-era filenames actually in-repo (verify each against
the tree; drop rows for files that don't exist at all).

## Non-blocking (disclose-only)

- StartupWMClass: keep `develcakes` for now; add a README/desktop comment
  that GTK4's WM class for com.develcakes.app may differ (post-MVP polish).
- agent/runtime.py:110 "setdefault" comment — SP4 sweep list.

## Battery (paste all)

- `pytest tests/test_app_identity.py tests/test_config_migration.py tests/test_env_divergence.py -q`
- ruff/pyright vs baselines; README grep re-proof (lineage rows intact,
  STT_MODEL_SIZE present, crabcakes→develcakes source path correct)
- RED proofs: pin reorder + delete; strengthened icon test vs current
  mis-mapped state

## COMPLETENESS (mandatory)

- [ ] BUG#1 theme-named icons + strengthened test — hunks + RED
- [ ] BUG#2 statement-pin — hunk + RED (both shapes)
- [ ] BUG#3 STT row restored — hunk
- [ ] BUG#4 source path fixed — hunk
- [ ] BUG#5 layout names reverted — hunks
- [ ] Battery + baselines + grep re-proof
- [ ] Related issues found, NOT fixed

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
