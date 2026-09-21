# Implementation Phase I.2 — FeedHandler auto-accept level methods

**Spec:** `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-3.md` §2.3
**Prompt to load:** `prompts/steelFramedCodeWriter.md`
**Working dir:** `/path/to/projects/crabcakes`

## Task

Add three new methods to `FeedHandler` in `ui/handlers/feed_handler.py`:

1. `get_auto_accept_level() -> str` — returns `"off" | "diffs" | "files" | "all"` based on the four `file_changes` prefs
2. `set_auto_accept_level(level: str) -> None` — sets the level, routes through the warning gate, commits via `_refresh_auto_accept_state()`
3. `_commit_auto_accept_level(level: str) -> None` — private helper that writes the distinct state and calls `_refresh_auto_accept_state()`

## File to change

**`ui/handlers/feed_handler.py`** — only this file.

## Rules

- Use the `steelFramedCodeWriter.md` prompt at `prompts/steelFramedCodeWriter.md`
- Read `ui/handlers/feed_handler.py` in full before editing. Anchor to method names, not line numbers.
- Read `models/feed_card.py` to verify the `AutoAcceptPrefs` / `FileChangePref` structure and the `_refresh_auto_accept_state` / `_show_auto_accept_warning` / `_save_feed_prefs_idle` methods.
- Follow the spec §2.3 code samples verbatim (they were verified across 4 audit rounds).
- **Key invariants** (verified in spec audit):
  - `"off"` = all four file_changes disabled; `"diffs"` = diff-only; `"files"` = 3-file-group-only (diff OFF); `"all"` = all four on. Each state is **distinct and round-trippable**.
  - `set_auto_accept_level` must route through `_show_auto_accept_warning` for enabling states (not bypass it).
  - Commit must call `_refresh_auto_accept_state()`, NOT `_save_feed_prefs_idle()` directly.
  - Invalid level → no-op.
  - The label is file-scoped: exec_command auto-accept is a separate axis and must NOT be touched.

## Verification (paste output in COMPLETENESS)

1. **Round-trip test** (paste actual output):
```python
from models.feed_card import AutoAcceptPrefs
# Construct a FeedHandler with a real AutoAcceptPrefs, no warning callback wired
# set each level, get it back, assert round-trip
```

2. **Warning gate test**: wire a mock `_show_auto_accept_warning` that captures `(category, agent, on_confirm, on_cancel)`; call `set_auto_accept_level("files")`; assert the warning was invoked; call `on_confirm`; assert `_refresh_auto_accept_state` was called and the level committed.

3. **grep**: `grep -n "get_auto_accept_level\|set_auto_accept_level\|_commit_auto_accept_level" ui/handlers/feed_handler.py` — confirm 3 new method definitions.

4. **Import smoke test**: `python3 -c "from ui.handlers.feed_handler import FeedHandler"` — no errors.

## COMPLETENESS checklist (required)

```
COMPLETENESS:
- [x] Edit 1: Added get_auto_accept_level() — evidence (line/method anchor)
- [x] Edit 2: Added set_auto_accept_level() — evidence
- [x] Edit 3: Added _commit_auto_accept_level() — evidence
- [x] Round-trip test: off/diffs/files/all all round-trip — output
- [x] Warning gate test: enabling routes through _show_auto_accept_warning — output
- [x] _refresh_auto_accept_state called after each commit — evidence
- [x] grep confirms 3 new method definitions — output
- [x] Import smoke test passes — output
- [x] exec_command auto-accept NOT touched by any new code — evidence
```

Report back with COMPLETENESS + verification evidence. Please write when done.
