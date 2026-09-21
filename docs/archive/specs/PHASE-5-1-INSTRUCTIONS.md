# Phase 5-1 — Toolbar CSS + Prefs I/O

> Part of FEED-CARD-UX-PHASE-5 — Persistent Feed Toolbar + Auto-Accept Toggle
> Implements spec Steps 1 and 4.

## Before Starting

1. Read the full master spec: `docs/specs/FEED-CARD-UX-PHASE-5-INSTRUCTIONS.md`
2. Read the steelFramedCodeWriter prompt: `prompts/steelFramedCodeWriter.md`
3. Read every file you will edit in full before touching it.

## Edit 1: `ui/styles.py` — Toolbar CSS classes

Anchor: append after the existing `.feed-btn-batch-accept:hover { ... }` block (around line 889).

Add four new CSS classes:
- `.feed-toolbar` — horizontal box bg, padding 6px 12px, margin-top 8px
- `.feed-toolbar-toggle` — flat button styling for the toggle
- `.feed-toolbar-batch` — reuses `.feed-btn-batch-accept` palette with adjusted padding
- `.feed-toolbar-divider` — vertical separator (1px wide, 24px tall, low-opacity color)

Match the existing `.feed-batch-bar` / `.feed-btn-batch-accept` color palette.

**Verify:** `grep -n "feed-toolbar" ui/styles.py` returns new lines.

## Edit 2: `utils/feed_store.py` — Prefs I/O constants + functions

**Constants** — add near module top alongside other filename constants:
```python
FEED_PREFS_FILENAME = "feed-prefs.json"
PREFS_VERSION = 1
```

**Functions** — append at end of file:

1. `_prefs_path(project_path: str) -> str` → returns `<project_path>/.crabcakes/feed-prefs.json`

2. `load_feed_prefs(project_path: str) -> dict` — returns default `{"version": 1, "auto_accept_enabled": False, "auto_accept_agent": None}` on missing or invalid file. Handles `json.JSONDecodeError` and missing keys gracefully. Logs warnings on parse errors.

3. `save_feed_prefs(project_path: str, prefs: dict) -> None` — calls `_ensure_crabcakes_dir(project_path)`, validates `prefs.get("version") == 1`, calls `_atomic_write_json`. Logs on failure.

**Verify (interactive):**
```python
from utils.feed_store import load_feed_prefs, save_feed_prefs
import tempfile
d = tempfile.mkdtemp()
save_feed_prefs(d, {"version": 1, "auto_accept_enabled": True, "auto_accept_agent": "coder"})
assert load_feed_prefs(d) == {"version": 1, "auto_accept_enabled": True, "auto_accept_agent": "coder"}
```

**Verify:** `pytest tests/test_feed_store.py -q --tb=short` passes.

## Rules

- Use the steelFramedCodeWriter prompt at `prompts/steelFramedCodeWriter.md`
- Do NOT modify any files other than the two listed above
- Do NOT add tests — tests are in a later phase
- Report: files changed with line numbers, grep evidence, test results
- Include a COMPLETENESS checklist with evidence for each edit
