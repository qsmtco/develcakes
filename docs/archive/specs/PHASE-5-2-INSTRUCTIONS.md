# Phase 5-2 — FeedTab Toolbar Widget + MockFeedTab Extension

> Part of FEED-CARD-UX-PHASE-5 — Persistent Feed Toolbar + Auto-Accept Toggle
> Implements spec Steps 2 and 3.

## Before Starting

1. Read the full master spec: `docs/specs/FEED-CARD-UX-PHASE-5-INSTRUCTIONS.md`
2. Read the steelFramedCodeWriter prompt: `prompts/steelFramedCodeWriter.md`
3. Read every file you will edit in full before touching it.

## Edit 1: `ui/views/feed_tab.py` — Toolbar widget + remove old `_batch_bar`

Read the full file first. Key anchors:
- `FeedTab.__init__` — where `_feed_scroll` is created and appended
- `FeedTab.update_batch_bar` — where `_batch_bar` is lazy-created inline (no separate `_build_batch_bar` method)

### 1a. Add toolbar in `__init__`

After `_feed_scroll` is appended to `self`, build and append the toolbar:

```python
# Persistent bottom toolbar (Phase 5)
self._toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
self._toolbar.add_css_class("feed-toolbar")

self._auto_accept_toggle = Gtk.ToggleButton(label="Auto-Accept: OFF")
self._auto_accept_toggle.add_css_class("feed-toolbar-toggle")
self._auto_accept_toggle.connect("toggled", self._on_auto_accept_toggled)

self._divider = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
self._divider.add_css_class("feed-toolbar-divider")

self._batch_accept_button = Gtk.Button(label="Accept All")
self._batch_accept_button.add_css_class("feed-btn-batch-accept")
self._batch_accept_button.connect("clicked", self._on_batch_button_clicked)

self._batch_accept_label = Gtk.Label(label="")
self._batch_accept_label.add_css_class("feed-batch-bar-info")

self._toolbar.append(self._auto_accept_toggle)
self._toolbar.append(self._divider)
self._toolbar.append(self._batch_accept_button)
self._toolbar.append(self._batch_accept_label)

self.append(self._toolbar)
```

Also add instance vars (near other `_`-prefixed attrs in `__init__`):
- `self._auto_accept_callback: Callable[[bool], None] | None = None`
- `self._batch_button_label: str = ""` (for tests)

### 1b. Remove old `_batch_bar` construction from `update_batch_bar`

In `update_batch_bar`, find the block that lazy-creates `self._batch_bar` (the `if self._batch_bar is None:` block with `self._batch_bar = Gtk.Box(...)`, `info_label`, `accept_btn`, `parent.prepend(self._batch_bar)`). **Remove this entire block.**

Replace the body of `update_batch_bar` with:
```python
def update_batch_bar(self, count: int) -> None:
    if count >= 2:
        self._batch_accept_button.set_label(f"Accept All ({count})")
        self._batch_accept_button.set_visible(True)
    else:
        self._batch_accept_button.set_label("Accept All")
        self._batch_accept_button.set_visible(False)
    self._batch_button_label = self._batch_accept_button.get_label()
    self._batch_accept_label.set_text("")
```

### 1c. Add new methods

```python
def set_auto_accept_callback(self, callback: Callable[[bool], None] | None) -> None:
    self._auto_accept_callback = callback

def update_auto_accept_state(self, active: bool) -> None:
    self._auto_accept_toggle.set_active(active)
    self._auto_accept_toggle.set_label("Auto-Accept: ON" if active else "Auto-Accept: OFF")

def _on_auto_accept_toggled(self, button: Gtk.ToggleButton) -> None:
    if self._auto_accept_callback is not None:
        self._auto_accept_callback(button.get_active())

def _on_batch_button_clicked(self, button: Gtk.Button) -> None:
    if self._batch_accept_callback is not None:
        self._batch_accept_callback()
```

### 1d. Verify no `_batch_bar` references remain

`grep -n "_batch_bar" ui/views/feed_tab.py` must return nothing.

## Edit 2: `tests/test_feed_handler.py` — Extend MockFeedTab

Read the full `MockFeedTab` class first.

### 2a. Add to `MockFeedTab.__init__`:
```python
self._auto_accept_active = False
self._auto_accept_callback = None
self._batch_button_label = ""
self._batch_button_visible = True
```

### 2b. Add stub methods to `MockFeedTab`:
```python
def update_auto_accept_state(self, active: bool) -> None:
    self._auto_accept_active = active

def set_auto_accept_callback(self, callback) -> None:
    self._auto_accept_callback = callback
```

### 2c. Update `MockFeedTab.update_batch_bar`:
Add at the end of the existing method body:
```python
self._batch_button_label = f"Accept All ({count})" if count >= 2 else "Accept All"
self._batch_button_visible = count >= 2
```

**Important:** Keep the existing `self._batch_bar_count = pending_count` and `self._batch_bar_visible = pending_count >= 2` lines. Do NOT remove them. The existing `TestBatchAccept` tests still assert on `_batch_bar_visible` and `_batch_bar_count`. Step 10 of the spec (in a later phase) will update those assertions. For now, set BOTH the old and new attrs to keep all tests green.

## Rules

- Use the steelFramedCodeWriter prompt at `prompts/steelFramedCodeWriter.md`
- Do NOT modify any files other than the two listed above
- Do NOT add new tests — tests are in a later phase
- Do NOT modify `tests/test_low2_file_sandbox.py`
- Report: files changed with line numbers, grep evidence, test results
- Include a COMPLETENESS checklist with evidence for each edit

## Verify

1. `python3 -c "import ui.views.feed_tab"` — no import errors
2. `grep -n "_batch_bar" ui/views/feed_tab.py` — returns nothing
3. `pytest tests/test_feed_handler.py -k "TestBatchAccept" -q --tb=short` — all pass
4. `pytest tests/test_feed_handler.py -q --tb=short` — all pass
