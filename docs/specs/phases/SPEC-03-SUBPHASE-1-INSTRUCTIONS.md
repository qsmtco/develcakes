# SPEC-03 Sub-Phase 1 Instructions — Config Accessors + Clamp Tests

**Spec:** docs/specs/SPEC-03-MEMORY-RATCHET.md (read §2 "utils/feed_store.py — retention
config" and §6/§7 first)
**Parent plan:** docs/specs/phases/SPEC-03-SUBPHASES.md (read the "Existing machinery"
section — the live window ALREADY EXISTS in feed_handler; this spec is hardening it)
**Scope: exactly 2 files** — `utils/feed_store.py` (+~25), `tests/test_feed_retention.py`
(NEW). Nothing else.

## Context (why this is first)

The live window (eviction + backlog + Load More) already landed via MEMRATCHET P3/P5.
The ONLY missing pieces from SPEC-03 §2 are: (a) a configurable window, (b) a test
harness proving the invariant. This sub-phase lands (a)'s storage layer + its tests.
Zero UI risk. Zero handler risk.

## Task — add to utils/feed_store.py

Follow the module's existing doc conventions (read the top-of-file docstring first).

1. `get_live_window() -> int` — read `live_window` from the existing feed-prefs.json
   (same file `load_feed_prefs`/`save_feed_prefs` use; mirror their read pattern —
   tolerant of missing file/corrupt JSON → return `LIVE_WINDOW_DEFAULT`). 
2. `LIVE_WINDOW_DEFAULT = 300` module constant.
3. `set_live_window(n: int) -> None` — validate + clamp 50–5000 (raise `ValueError`
   on non-int/bool — note bool is an int subclass; reject it), write via the existing
   prefs pattern (preserve sibling keys when prefs exist — read-modify-write, not blind
   overwrite).
3b. **(Hardening, sanctioned)** While you are in this file: the feed-prefs path
   resolution uses `get_config_dir()`. Sanity-check with a probe that the accessors
   honor `XDG_CONFIG_HOME` (lazily resolved at call time, not import time). If the
   module resolves eagerly at import, note it in your report — do NOT fix silently.
4. DOC update: extend the feed_store module docstring's §2.3 retention section with a
   `live_window` paragraph (default 300, clamp 50–5000, view-level not store-level,
   view is a projection of the disk store).

## Tests — tests/test_feed_retention.py (NEW)

Header comment: link SPEC-03, state the 3-track role (config/2k-harness/invariant).
**MUST use the isolated-config pattern** — module-scoped autouse fixture redirecting
XDG_CONFIG_HOME to tempfile.mkdtemp() (mirror tests/test_error_surfacing.py
`_isolated_config_home` :119–148, INCLUDING the `_fixture_config_root` stash + sentinel
pattern). The suite itself must be self-falsifiable on isolation.

Minimum 10 tests:
1. `test_default_window_returns_300` — fresh env, no prefs file.
2. `test_get_returns_persisted_value` — set 120 → read-back 120.
2b. `test_persisted_value_survives_new_process` — set 120, spawn
   `subprocess.run([sys.executable, "-c", "from utils import feed_store; print(feed_store.get_live_window())"])` — prints 120. (Guards against future eager resolution.)
3. `test_set_clamps_low` — set(25) → get() == 50.
4. `test_set_clamps_high` — set(9000) → get() == 5000.
5. `test_set_rejects_non_int` — ValueError for "300", 300.5, None.
6. `test_set_rejects_bool` — ValueError for True/False (bool-is-int trap).
7. `test_get_corrupt_prefs_returns_default` — write garbage bytes to feed-prefs.json
   → get() == 300.
8. `bserves_sibling_keys` — pre-seed prefs with {"auto_accept": {...}} → set(200) →
   auto_accept key still present with identical value.
9. `test_isolation_active` — the sentinel (config dir inside fixture tmp root).
10. `test_accessor_honors_xdg_env` — point XDG_CONFIG_HOME elsewhere within the test
    → reads/writes land under the new root.

## Verification (paste ALL, real runs)

```
.venv/bin/python -m pytest tests/test_feed_retention.py -q
.venv/bin/python -m pytest tests/test_feed_store.py tests/test_feed_card.py -q
.venv/bin/python -m ruff check utils/feed_store.py tests/test_file_retention.py tests/test_feed_retention.py
.venv/bin/python -m ruff format --check tests/test_feed_retention.py
.venv/bin/pyright utils/feed_store.py 2>&1 | tail -1
```

Baselines (ZERO new allowed): feed_store.py current ruff count = measure first with
`.venv/bin/python -m ruff check utils/feed_store.py | tail -1` and report it; pyright
likewise. NEW test file must be ruff-clean and format-clean.

Env: `.venv/bin/python` is THE pytest env (pytest 9.1.1). Do NOT run bare
tests/test_agent_runtime.py ever (in-app OOM risk).

## COMPLETENESS report (in your reply)

- [ ] Both files touched/created (line ranges)
- [ ] All 5 verification outputs pasted
- [ ] Baselines measured BEFORE your change and reported
- [ ] Isolation sentinel + XDG probe included
- [ ] Sibling-key preservation + bool-rejection tests present
- [ ] Any deviation flagged (no silent fixes)
