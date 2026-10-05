# SPEC-11 SP2 — env-var divergence (DEVELCAKES_* + one-release fallback) + config-dir sweep

**Spec:** `docs/specs/SPEC-11-RENAME-MIGRATION.md` §2 (env vars)
**Pre-flight (binding):** `docs/specs/phases/SPEC-11-PREFLIGHT-DECISIONS.md` — D2, D2b, D4
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `utils/config.py` (get_env) + 7 read sites + docstring sweep +
`tests/test_env_divergence.py` (new).

---

## 1. `get_env` helper (D2)

In `utils/config.py`:

```python
def get_env(name: str) -> str | None:
    """DEVELCAKES_<name> if set, else CRABCAKES_<name> (one-release fallback).

    D2: reads the NEW name first; the old name is honored only when the new
    one is unset — never mixed. Sites must not read os.environ directly for
    the renamed family. Removal of the fallback is a registered post-MVP
    chore.
    """
    new = os.environ.get(f"DEVELCAKES_{name}")
    if new is not None:
        return new
    return os.environ.get(f"CRABCAKES_{name}")
```

## 2. The 7 sites (verified list — anchor by current literal)

| # | Site | Current literal | New |
|---|---|---|---|
| 1 | `utils/config.py:46` get_projects_dir | `CRABCAKES_PROJECTS_DIR` | `get_env("PROJECTS_DIR")` |
| 2 | `main.py:14` debug | `CRABCAKES_DEBUG` | `get_env("DEBUG")` truthiness |
| 3 | `main.py:30` setdefault + `agent/runtime.py:116` + comment :572 | `CRABCAKES_MIGRATE_STORE` | see 2a below |
| 4 | `agent/context.py:260` | `CRABCAKES_INCLUDE_DOCS` | `get_env("INCLUDE_DOCS") == "1"` |
| 5 | `agent/tools.py:883` | `CRABCAKES_WEB_FETCH_RESTRICT` | `get_env("WEB_FETCH_RESTRICT") == "1"` |
| 6 | `ui/wiring.py:22` constant + `ui/views/event_cards.py:67` | `CRABCAKES_ACTIVE_PROJECT_PATH` | see 2b |
| 7 | `ui/views/chat_surface.py:44` | `CRABCAKES_NO_WEBKIT` | `get_env("NO_WEBKIT")` truthiness |

### 2a — MIGRATE_STORE (the setdefault wrinkle)

`main.py` does `os.environ.setdefault("CRABCAKES_MIGRATE_STORE", "1")` —
it WRITES the old name. Change to `DEVELCAKES_MIGRATE_STORE` setdefault.
`agent/runtime.py:116` reads via `get_env("MIGRATE_STORE")`. Ordering note:
main.py's setdefault runs before window import — preserve that order.
conftest's `CRABCAKES_MIGRATE_STORE=0` pin keeps working via fallback —
BUT verify: with main.py now setdefaulting the NEW name only, a conftest
pinned OLD=0 → get_env returns "0" (old set, new unset) ✓. No conftest
edits this phase; the 13 CRABCAKES_-setting test files ride the fallback
(sweep post-MVP per D2).

### 2b — ACTIVE_PROJECT_PATH (module constant)

`ui/wiring.py:22` defines `ACTIVE_PROJECT_ENV = "CRABCAKES_ACTIVE_PROJECT_PATH"`.
Rename the constant's VALUE to `"DEVELCAKES_ACTIVE_PROJECT_PATH"` and route
reads: `event_cards.py:67` via `get_env("ACTIVE_PROJECT_PATH")`. Check for
external writers of this env (tests that monkeypatch it — grep
`ACTIVE_PROJECT_ENV` + the literal): any test setting the old literal
keeps working ONLY if it sets via the constant or the fallback reads. If a
test writes the old literal directly and the code reads new-first, the
fallback still sees it (new unset) ✓ — but flag any such test in the report.

## 3. Docstring/comment sweep (config-dir mentions)

`agent/config.py:190` (`~/.config/crabcakes`) → develcakes. Grep remaining
`~/.config/crabcakes` and `.config/crabcakes` prose in non-test .py — sweep
to develcakes. Do NOT touch history files (post-mortems/specs/proposals)
or the migration's own v1 references.

## 4. Tests — `tests/test_env_divergence.py` (new, RED-first)

1. `test_get_env_new_wins` — both set, different values → new returned.
2. `test_get_env_old_fallback` — only old set → old returned.
3. `test_get_env_neither` — none → None.
4. `test_get_env_empty_string_new` — new="" (set-but-empty) → "" returned
   (empty IS set — never falls back; pin this semantic).
5. `test_projects_dir_reads_env` — DEVELCAKES_PROJECTS_DIR honored;
   CRABCAKES_PROJECTS_DIR alone still honored.
6. `test_migrate_store_fallback` — old name set "0" → runtime's reader
   returns "0" semantics (false) even with main.py's new-name setdefault
   path untriggered (import-order-safe: test the reader, not main).
7. `test_active_project_env_constant` — `ui.wiring.ACTIVE_PROJECT_ENV ==
   "DEVELCAKES_ACTIVE_PROJECT_PATH"` + event_cards resolves via get_env.

## 5. Battery (paste all)

- `pytest tests/test_env_divergence.py tests/test_config_migration.py -q`
- `pytest tests/ -k "env or config or wiring or chat_surface or context or
  tools" -q` (adjacent)
- Full-ish: the 13 CRABCAKES_-setting test files (list them; run them) —
  fallback proof at suite scale
- ruff/pyright on touched files vs baselines (measure first)

## 6. COMPLETENESS (mandatory)

- [ ] get_env helper — hunk
- [ ] 7 sites converted — hunks (table rows)
- [ ] Docstring sweep — hunks + grep proof (zero non-history `.config/
      crabcakes` prose remains)
- [ ] 7 tests RED-first — outputs
- [ ] 13-file fallback battery — outputs
- [ ] ruff/pyright/wc — outputs
- [ ] Related issues found, NOT fixed

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
