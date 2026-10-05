# SPEC-11 SP3 — app identity (pyproject, main.py, desktop, icons) + README sweep

**Spec:** `docs/specs/SPEC-11-RENAME-MIGRATION.md` §2 (identity items)
**Pre-flight (binding):** `docs/specs/phases/SPEC-11-PREFLIGHT-DECISIONS.md` — D1, D5
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `pyproject.toml`, `main.py` (class + app id), `data/com.develcakes.app.desktop`
(new), icons (mechanism-preserving), `README.md` sweep, `main.py` migration wiring (the
SP1 call + banner). Sub-phase if >3 concurrent edits — disclose.

---

## 1. pyproject.toml (D1)

- `name = "develcakes"` (was crabcakes)
- `[project.scripts]`: `develcakes = "main:main"` — REMOVE the old `crabcakes`
  entry (spec explicit; the venv's old entry disappears on reinstall —
  expected, disclose in the report).

## 2. main.py (D1 + migration wiring)

- `class CrabcakesApp → DevelcakesApp` (all references: definition,
  `DevelcakesApp(...)` instantiation, `sys.exit(...)` if it names the class).
- `application_id='com.develcakes.app'`.
- Icon: VERIFY the current mechanism first (`set_default_icon_name('crabcakes')`
  at :97 — a THEME name). Per D5: keep the mechanism, change the name to
  `'develcakes'` ONLY IF an installed theme icon ships with the packaging —
  otherwise switch to the file-based `set_default_icon` loading the bundled
  PNG set (icons/ dir). Disclose which and why. Do not break the existing
  startup path.
- **Migration wiring (D3 ordering):** call `migrate_v1_config()` at app
  start BEFORE the window build and before any config-reading import side
  effects; on a non-None report, surface the first-run banner:
  - feed card via the SPEC-02 pattern (title "Config migrated from v1",
    body listing copied/skipped counts) — wire through whatever the
    SPEC-02 error-surfacing path makes available at that point in startup;
  - if the feed isn't up yet at that stage (verify — the migration runs
    pre-window), defer the card emission to the first available feed
    callback (store the report; emit on feed-ready).
  - failure report (failed non-empty) → banner reports the failure +
    "v1 untouched" (D3).

## 3. Desktop entry (D5 — NEW file)

`data/com.develcakes.app.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=Develcakes
Comment=AI-native project development environment
Exec=develcakes
Icon=develcakes
Terminal=false
Categories=Development;
StartupWMClass=develcakes
```

(No existing desktop file to rename — verified. If pyproject/packaging
installs data files, check and wire; otherwise the file lands in-repo as
the packaging artifact. Disclose what you find.)

## 4. README sweep (D9)

- Title/badges/quickstart → develcakes (launcher name, app name).
- **Lineage section KEPT** (spec explicit) — v1 references that describe
  history stay.
- `grep -rin crabcakes README.md` after the sweep: remaining hits must be
  lineage-prose only (quote them in the report).

## 5. Tests

- `tests/test_app_identity.py` (new, RED-first):
  1. `test_pyproject_name_and_script` — parse pyproject.toml: name ==
     develcakes; scripts contains develcakes, NOT crabcakes.
  2. `test_main_class_and_app_id` — source-pin: `DevelcakesApp` defined;
     `application_id='com.develcakes.app'` present; zero `CrabcakesApp`
     remains.
  3. `test_desktop_entry_exists_and_wellformed` — file exists; key lines
     present.
  4. `test_migration_wired_before_window` — source-pin ordering: the
     migrate_v1_config call appears before the window-build import/call in
     main.py's startup sequence.
  5. `test_banner_emitted_on_migration` — a startup-shaped test (mock what
     startup needs) asserting a successful report produces the banner card
     (or the deferred-emit path) and a second run (marker) does NOT.
- Launcher smoke: `pip install -e .` then verify `develcakes` entry exists
  AND `crabcakes` is gone from the venv's bin (paste `ls .venv/bin/ | grep
  -i cake`). If the reinstall has side effects on the venv, disclose.

## 6. Battery (paste all)

- `pytest tests/test_app_identity.py tests/test_config_migration.py tests/test_env_divergence.py -q`
- `pytest tests/ -k "main or window or app" -q` (adjacent, xvfb if GUI)
- ruff/pyright vs measured baselines on touched files
- README grep proof
- Launcher smoke output

## 7. COMPLETENESS (mandatory)

- [ ] pyproject name+script — hunk
- [ ] main.py class/app-id/icon — hunks + mechanism disclosure
- [ ] Migration wiring + banner (incl. deferred-emit if needed) — hunks
- [ ] Desktop entry — new file
- [ ] README sweep — diff + grep proof (lineage quotes)
- [ ] 5 tests RED-first — outputs
- [ ] Launcher smoke — output
- [ ] Battery + baselines
- [ ] Related issues found, NOT fixed

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
