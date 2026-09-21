# FEED-CARD-UX-PHASE-5-INSTRUCTIONS

> Phase 5 — **Persistent Feed Toolbar + Auto-Accept Toggle**
> Implements `SPEC-FEED-CARD-UX.md` §2.13 (new) and supersedes §2.10–§2.12 for layout (batch bar moves to toolbar).

---

## §0. How to use this spec

1. **Read sections in order.** §1 = goal, §2 = current state (what's already true), §3 = the change, §4 = implementation steps (numbered, atomic, testable), §5 = test plan, §6 = risks.
2. **Code anchors use identifiers, not line numbers.** Line numbers are stale within a day. Anchor to the class name + method name + variable name. Example: `FeedTab._batch_bar` not `feed_tab.py:340`.
3. **GTK4 threading rule (restated).** Every GTK widget call happens on the main thread. Background work uses `threading.Thread(target=_git_X, daemon=True).start()` and re-enters GTK via `GLib.idle_add()`. This applies to: appending cards, reading vadjustments, building dialogs, showing/hiding widgets.
4. **Run the test suite after each step.** `tests/test_feed_handler.py` has 182 passing tests today; this phase adds 6 tests, none of which should break existing ones.

---

## §1. Goal & Non-Goals

### Goal

Replace the in-feed contextual batch accept bar with a **persistent bottom toolbar** in `FeedTab` that contains two controls:

1. **Auto-Accept toggle** — when ON, automatically `handle_accept()` every actionable file-change card (from a single configured agent) as soon as it's appended to the feed. When OFF, behavior matches today's per-card Accept button.
2. **Accept All (batch)** — the existing "Accept N pending file changes" button, now always visible (not contextual). Counter updates dynamically.

**Why this exists:** the user is reviewing many consecutive Coder edits per session. Clicking Accept on each card by hand is friction. The toggle makes the PM "trust this agent for the rest of this session" explicit and reversible.

### Non-goals

- Per-agent matrix UI (toggle is single-agent; one toggle per project, applies to one author)
- Auto-approving `needs_approval` exec-approval cards (separate phase)
- Auto-accepting `tool_result` cards (informational, no Accept button)
- Agent-edit-dialog changes (out of scope for this phase — toolbar lives in Feed tab)
- Reading-position preservation across tab switches (fixed in prior session)

---

## §2. Current State (already true before this phase)

These are verified facts from reading source files. Do not re-verify; build on them.

### §2.1 `FeedTab` (`ui/views/feed_tab.py`)

- Class `FeedTab(Gtk.Box)` extends `Gtk.Box` with orientation `VERTICAL`.
- Children at runtime: `_feed_scroll` (ScrolledWindow) → `_card_container` (Box).
- Has `_batch_bar` (Box) with CSS class `feed-batch-bar`. Today it is **lazy-created inside `update_batch_bar()`** on first call (not in `__init__`). The widget is prepended to the parent of `_feed_scroll` via `parent.prepend(self._batch_bar)` inline in `update_batch_bar()` — there is **no separate `_build_batch_bar` method**.
- Public methods used by `FeedHandler`:
  - `update_batch_bar(count: int) -> None` — lazy-creates `_batch_bar` on first call (when `count >= 2`), sets info text, shows the bar. Hides it when `count < 2`.
  - `set_batch_accept_callback(callback: Callable[[], None]) -> None` — stores the callback wired to the "Accept All" button.
- Existing scroll machinery: `schedule_scroll_to_bottom()` defers via `vadjustment.changed` signal + 150 ms timeout. **Reuse this — do not invent a new scroll path.**

### §2.2 `FeedHandler` (`ui/handlers/feed_handler.py`)

- Single entry point for new cards: `add_card(card_data) -> str` (returns `card_id`).
- `handle_accept(card_id: str)` — for git-backed cards runs git ops in a daemon thread; for non-git cards sets `accepted=True` and updates visual on main thread.
- `handle_batch_accept(card_ids: list[str])` — iterates `handle_accept` per id.
- `_update_batch_bar_for_active_project(project_name: str | None = None)` — recomputes the trailing-run count and calls `self._feed_tab.update_batch_bar(count)`.
- `_on_batch_accept_clicked()` — wired to the batch bar button; computes the list of consecutive pending file-change cards (newest-first), reverses to top-to-bottom order, calls `handle_batch_accept`, then `_update_batch_bar_for_active_project()`.
- `_lock = threading.Lock()` exists. Use it only when crossing the `self._cards` / `self._project_cards` dicts; **do not** use it for the new toggle bool (Python bool reads/writes are atomic under the GIL and the toggle is only touched on main thread).
- Constructor signature today: `FeedHandler(*, GLib, on_send_to_agent, get_chat_box_for_session, on_approve_exec)`. Constructor does **not** take a window reference; callback injection is the established pattern (mirror `on_approve_exec`).

### §2.3 Persistence (`utils/feed_store.py`)

- `_feed_path(project_path)` → `<project_path>/.crabcakes/feed.json`.
- `_atomic_write_json(path, data)` — writes to `.tmp`, `os.replace`, then `chmod 0o600`. **Reuse this exact function for the new prefs file.**
- `_ensure_crabcakes_dir(project_path)` — creates `.crabcakes` if missing. **Reuse this too.**
- No existing per-project preferences file. New file: `<project_path>/.crabcakes/feed-prefs.json`.

### §2.4 Configuration precedent (`agent/enforcement.py`)

- `.crabcakes/enforcement.json` is the existing per-project config file. **Do not extend it.** Auto-accept is feed-scoped, not enforcement-scoped — separation of concerns. New file `feed-prefs.json` follows the same atomic-write pattern.

### §2.5 Confirmation dialog precedent (`ui/handlers/agent_builder_handler.py`)

- `Gtk.MessageDialog(transient_for=parent_window, modal=True, message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.YES_NO, text=..., secondary_text=...)` — used for delete-agent confirmation.
- Wrapped in `GLib.idle_add(_show_dialog, ...)` so the show happens on the main thread.
- Response handler calls `_dialog.close()`, then branches on `Gtk.ResponseType.YES` vs anything else.

### §2.6 ToggleButton precedent (`ui/toolbar.py`)

- `Gtk.ToggleButton(label="Stream: OFF")` connected to `toggled` signal. `set_active(True)` programmatically does **not** fire `toggled` (verified). Pattern to mirror.

### §2.7 Existing CSS (do not re-add)

- `.feed-batch-bar`, `.feed-batch-bar-info`, `.feed-btn-batch-accept`, `.feed-btn-batch-accept:hover`, `.feed-card-seq` — all already in `ui/styles.py`.

### §2.8 Test infrastructure (`tests/test_feed_handler.py`)

- `MockFeedTab` provides `update_batch_bar(count)` and `set_batch_accept_callback(cb)` stubs. **Extend it** (don't replace) with: `_auto_accept_active` (bool, default False), `_auto_accept_callback` (None), `update_auto_accept_state(active: bool)` stub, `set_auto_accept_callback(cb)` stub.
- Fixtures: `mock_glib`, `mock_feed_tab`, `feed_handler` — all exist. No new fixtures needed.
- `TestBatchAccept` class — pattern to mirror for `TestFeedToolbarAutoAccept`.

### §2.9 GTK version

- GTK 4.14.5, GLib 2.80. `Gtk.Box.prepend()` (4.4+), `Gtk.ToggleButton` (4.0+), `Gtk.MessageDialog` (4.0+) all available.

---

## §3. The Change

### §3.1 Layout change: move batch bar to bottom toolbar

**Old layout:**
```
FeedTab (vertical Box)
├── _batch_bar           ← OLD: contextual, visible when count ≥ 2
└── _feed_scroll
    └── _card_container
```

**New layout:**
```
FeedTab (vertical Box)
├── _feed_scroll
│   └── _card_container
└── _toolbar (new, persistent, always visible)
    ├── [auto-accept toggle + label]
    ├── [divider]
    └── [batch accept button + counter label]
```

- `_toolbar` is always visible (no `count >= 2` gate on the bar itself).
- The batch button inside `_toolbar` shows "Accept All (3)" with the count, or is hidden (CSS `display: none`) when count is 0 or 1.
- The auto-accept toggle is always visible regardless of pending count.

### §3.2 Auto-accept semantics

- Toggle has two states: **OFF** (default, matches today's behavior) and **ON**.
- When ON, every actionable file-change card (`card_type` ∈ {`diff`, `file_created`, `file_modified`, `file_deleted`}) with `card.accepted is None` and `card.author == _auto_accept_agent` is automatically `handle_accept()`ed immediately after `add_card()` appends it to `_card_container`.
- `_auto_accept_agent` is currently a single string (the agent whose cards the PM wants to auto-accept). Today, the FeedHandler is agent-agnostic — it accepts cards from any author. For this phase, we pick the **first author** that ever arrives as `_auto_accept_agent` (the project is implicitly single-agent in practice — PM interacts with one coding agent at a time per project). Persisted in `feed-prefs.json` so reopens honor it.
- Auto-accept is checked **inside the `_append` closure** that runs on the main thread via `GLib.idle_add()` (inside `add_card()`). This is where `self._feed_tab.append_card()` appends the widget to `_card_container` — the check runs immediately after, so the visual exists before the git ops start. The `handle_accept()` call is itself a method that spawns a daemon thread for git ops, so the check site is the appropriate non-blocking context.
- `tool_result` and `needs_approval` cards are **never** auto-accepted — they're out of scope.

### §3.3 Warning dialog

- Toggling OFF→ON triggers a `Gtk.MessageDialog`:
  - `message_type=WARNING`, `buttons=YES_NO`
  - `text`: "Trust {agent_name} to auto-accept all file changes?"
  - `secondary_text`: "Every new diff, file_created, file_modified, and file_deleted card from {agent_name} will be committed automatically. You can turn this off anytime in the bottom toolbar."
- If user picks NO, the toggle visually snaps back to OFF and no state is persisted.
- If user picks YES, toggle stays ON and `_auto_accept_enabled = True` is persisted.
- Toggling ON→OFF never shows the dialog (no destructive reversal — accepting individual cards is safe).
- Warning is shown **every** time the user turns the toggle ON. No "don't show again" checkbox. The cost of an unwanted auto-accept (a committed change the PM didn't review) is high enough that a confirmation-per-session is justified.

### §3.4 Persistence

- New file: `<project_path>/.crabcakes/feed-prefs.json`
- Schema:
  ```json
  {
    "version": 1,
    "auto_accept_enabled": false,
    "auto_accept_agent": "coder"
  }
  ```
- Written atomically via `_atomic_write_json` (chmod 0o600). Created with `_ensure_crabcakes_dir` first.
- Loaded in `on_project_opened()` **inside the same `_load_and_render` thread** as card load. The parsed result is passed to the existing `_append_and_schedule_scroll` GLib idle callback (main thread) which then calls `self._feed_tab.update_auto_accept_state(enabled)` and sets `self._auto_accept_enabled`.
- If file doesn't exist or `auto_accept_enabled` is missing, default is OFF. If `auto_accept_agent` is missing, use the first observed card author (lazy assignment on first `add_card`).

### §3.5 Visual design

- Toolbar is a horizontal `Gtk.Box` with CSS class `feed-toolbar`, `margin-top: 8px`, `padding: 6px 12px`, dark bg matching `.feed-batch-bar`.
- Toggle is `Gtk.ToggleButton` with label that flips between "Auto-Accept: OFF" and "Auto-Accept: ON" (mirrors `toolbar.py:_update_stream_label` pattern).
- Batch button label: "Accept All (N)" where N is the trailing pending count, or hidden when N < 2.
- Existing `.feed-batch-bar`, `.feed-btn-batch-accept` CSS classes are reused on the button inside the toolbar (just relocate the widget).

---

## §4. Implementation Steps

Each step is atomic, testable in isolation, and ends with a green test run. Anchor edits to identifiers, not line numbers.

### Step 1 — Add toolbar CSS to `ui/styles.py`

Anchor: append after the existing `.feed-btn-batch-accept:hover { ... }` block (around line 889).

Add these classes:
- `.feed-toolbar` — horizontal box bg, padding 6px 12px, margin-top 8px
- `.feed-toolbar-toggle` — flat button styling for the toggle
- `.feed-toolbar-batch` — reuses `.feed-btn-batch-accept` palette but with adjusted padding
- `.feed-toolbar-divider` — vertical separator (1px wide, 24px tall, low-opacity color)

**Verify:** `grep -n "feed-toolbar" ui/styles.py` returns the new lines.

### Step 2 — Add toolbar widget + methods to `FeedTab`

In `ui/views/feed_tab.py`, class `FeedTab`:

1. In `__init__`, **after** the `_feed_scroll` is appended to `self`, append the new toolbar:
   - Build `_toolbar` as `Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)`, CSS class `feed-toolbar`, spacing 8
   - Build `_auto_accept_toggle` as `Gtk.ToggleButton(label="Auto-Accept: OFF")`, CSS class `feed-toolbar-toggle`
   - Build `_divider` as `Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)`, CSS class `feed-toolbar-divider`
   - Build `_batch_accept_button` as `Gtk.Button(label="Accept All")`, CSS class `feed-btn-batch-accept` (reusing existing class)
   - Build `_batch_accept_label` as `Gtk.Label(label="")`, CSS class `feed-batch-bar-info`
   - Append to `_toolbar` in order: toggle, divider, batch_button, batch_label
   - Append `_toolbar` to `self` AFTER `_feed_scroll`
   - Connect `_auto_accept_toggle` `toggled` signal to `self._on_auto_accept_toggled`
   - Connect `_batch_accept_button` `clicked` signal to `self._on_batch_button_clicked`
2. **Remove** the old `_batch_bar` widget entirely. It is lazy-created inside `update_batch_bar()` (lines with `self._batch_bar = Gtk.Box(...)`, the `info_label`, `accept_btn`, `parent.prepend(self._batch_bar)`, and the `.set_text`/`.set_visible` calls). There is **no separate `_build_batch_bar` method** — all the construction is inline in `update_batch_bar()`. Remove it all.
3. Update `update_batch_bar(count)`:
   - Set `_batch_accept_button.set_label(f"Accept All ({count})")` when count ≥ 2, else `set_label("Accept All")`
   - Set `_batch_accept_button.set_visible(count >= 2)` — hides at 0/1, shows at 2+
   - Set `_batch_accept_label.set_text("")` (legacy contextual label removed) OR remove the label widget entirely. **Decision: remove the label widget** — the count is in the button label.
4. Add `set_auto_accept_callback(callback: Callable[[bool], None]) -> None`:
   - Stores callback on `self._auto_accept_callback`
   - Used by FeedHandler to react to toggle state changes (to persist + manage dialog)
5. Add `update_auto_accept_state(active: bool) -> None`:
   - Calls `self._auto_accept_toggle.set_active(active)` (does NOT fire `toggled`)
   - Updates label to "Auto-Accept: ON" if active else "OFF"
6. Add `_on_auto_accept_toggled(button)`:
   - Calls `self._auto_accept_callback(button.get_active())` if set
7. Add `_on_batch_button_clicked(button)`:
   - Calls `self._batch_accept_callback()` if set (reuses the wiring already used by `_on_batch_accept_clicked` in FeedHandler)

**Verify:** `ui/views/feed_tab.py` parses (`python3 -c "import ui.views.feed_tab"`). The old `_batch_bar` attribute no longer exists.

### Step 3 — Extend `MockFeedTab` in `tests/test_feed_handler.py`

Anchor: inside `class MockFeedTab` (around line 47), add to `__init__`:
- `self._auto_accept_active = False`
- `self._auto_accept_callback = None`
- `self._batch_button_clicks = []`  (optional, for verifying button click wiring)

Add stub methods:
- `def update_auto_accept_state(self, active: bool): self._auto_accept_active = active`
- `def set_auto_accept_callback(self, callback): self._auto_accept_callback = callback`

**Verify:** `pytest tests/test_feed_handler.py -k "TestBatchAccept"` still passes (these tests don't exercise the new methods yet).

### Step 4 — Add prefs I/O to `utils/feed_store.py`

Anchor: append at the end of `utils/feed_store.py`.

New constants at module top:
```python
FEED_PREFS_FILENAME = "feed-prefs.json"
PREFS_VERSION = 1
```

New functions:
- `_prefs_path(project_path: str) -> str` → `<project_path>/.crabcakes/feed-prefs.json`
- `load_feed_prefs(project_path: str) -> dict` — returns `{"version": 1, "auto_accept_enabled": False, "auto_accept_agent": None}` on missing/invalid file. Logs warnings on parse errors.
- `save_feed_prefs(project_path: str, prefs: dict) -> None` — calls `_ensure_crabcakes_dir`, validates `version == 1`, calls `_atomic_write_json`.

**Verify:** write a one-shot test in repl: `from utils.feed_store import load_feed_prefs, save_feed_prefs; import tempfile, os; d = tempfile.mkdtemp(); save_feed_prefs(d, {"version": 1, "auto_accept_enabled": True, "auto_accept_agent": "coder"}); assert load_feed_prefs(d) == {"version": 1, "auto_accept_enabled": True, "auto_accept_agent": "coder"}`.

### Step 5 — Add auto-accept state + hook in `FeedHandler`

In `ui/handlers/feed_handler.py`, class `FeedHandler`:

1. In `__init__`, add instance vars after `_active_project_name` (around line 78):
   - `self._auto_accept_enabled: bool = False`
   - `self._auto_accept_agent: str | None = None`
   - `self._show_auto_accept_warning: Callable | None = None` (callback to show warning dialog; injected by window)
2. Add a module-level constant near the top of `feed_handler.py`:
   - `_AUTO_ACCEPT_TYPES = {"diff", "file_created", "file_modified", "file_deleted"}`
3. Add `set_show_auto_accept_warning(callback: Callable | None)`:
   - Stores `self._show_auto_accept_warning = callback` (matches the established callback-injection pattern; caller provides the dialog wrapper)
3. In `set_feed_tab(tab)`:
   - Call `tab.set_auto_accept_callback(self._on_auto_accept_toggled)` after the existing `tab.set_batch_accept_callback(...)` call
4. Add `_resolve_agent_name_for_dialog() -> str`:
   - Returns the best human-readable agent name for the warning dialog.
   - Fallback chain: `self._auto_accept_agent` → most recent card's `author` in `self._cards.get(self._active_project_name, [])` (iterate reversed) → the literal string `"the active agent"`. Never returns `None` or the string `"None"`.
5. Add `_on_auto_accept_toggled(active: bool)`:
   - If `active` is True: call `self._show_auto_accept_warning(self._resolve_agent_name_for_dialog(), on_confirm=self._enable_auto_accept, on_cancel=self._cancel_auto_accept)` if callback is set; else just call `_enable_auto_accept()` (fallback for tests/missing wiring)
   - If `active` is False: call `_disable_auto_accept()`
6. Add `_enable_auto_accept()`:
   - `self._auto_accept_enabled = True`
   - Schedule persistence: `self._GLib.idle_add(self._save_feed_prefs_idle)`
7. Add `_cancel_auto_accept()`:
   - `self._GLib.idle_add(lambda: self._feed_tab.update_auto_accept_state(False) if self._feed_tab else None)` — visually snaps toggle back to OFF
8. Add `_disable_auto_accept()`:
   - `self._auto_accept_enabled = False`
   - `self._GLib.idle_add(self._save_feed_prefs_idle)`
9. Add `_save_feed_prefs_idle()`:
   - Reads `self._project_paths.get(self._active_project_name)`
   - If set, calls `feed_store.save_feed_prefs(project_path, {"version": 1, "auto_accept_enabled": self._auto_accept_enabled, "auto_accept_agent": self._auto_accept_agent})`
10. In `add_card(card_data)` — auto-accept check goes inside the `_append` closure (after `self._feed_tab.append_card(widget, card_id)`), NOT between the widget storage and `_update_batch_bar_for_active_project`. This is because `_append` runs on the main thread via `GLib.idle_add`, and only after `append_card` does the card widget exist in the tree (§3.2 requires the visual to exist before git ops start).
    - Inside the `_append` lambda, after `self._feed_tab.append_card(widget, card_id)` and `self._schedule_smart_scroll()` (and before `self._on_card_added` if set):
      - ```python
        if (self._auto_accept_enabled
                and card_data.accepted is None
                and card_data.card_type in _AUTO_ACCEPT_TYPES
                and (self._auto_accept_agent is None or card_data.author == self._auto_accept_agent)):
            self._auto_accept_agent = self._auto_accept_agent or card_data.author
            self._GLib.idle_add(self._save_feed_prefs_idle)
            self._GLib.idle_add(lambda cid=card_data.card_id: self.handle_accept(cid))
        ```
    - **Why `_append` not the main body of `add_card`:** `add_card` runs on a background thread. `self._feed_tab.append_card()` is `idle_add`'d and hasn't executed yet when `add_card` returns. The check must run after the widget is in the tree on the main thread.
    - **Why `_GLib.idle_add` wrapping `handle_accept`:** Even though `_append` runs on the main thread, `handle_accept` for git-backed cards spawns a daemon thread for git ops. The `idle_add` wrapper is consistent with the rest of the codebase and harmless here (the card is already visible). For test simplicity, the `idle_add` call can be omitted in unit tests that mock `_GLib.idle_add`.
    - **Lazy agent lock-in happens here, not in `_enable_auto_accept`:** The first card after the toggle turns ON promotes `self._auto_accept_agent` from None to that card's author and persists immediately. Subsequent cards from other authors are skipped (because the agent check is now non-None). If the agent IS already persisted, all cards are scoped strictly to that author.

**Verify:** existing tests still pass. The new flow only fires when `_auto_accept_enabled` is True (default False), so no behavior change for existing tests.

### Step 6 — Load prefs in `on_project_opened()`

In `ui/handlers/feed_handler.py`, method `on_project_opened`:

1. Inside `_load_and_render`, after `cards = feed_store.load_feed(project_path)`:
   - `prefs = feed_store.load_feed_prefs(project_path)`
   - Store on `self`: `self._auto_accept_enabled = prefs.get("auto_accept_enabled", False)`, `self._auto_accept_agent = prefs.get("auto_accept_agent")`
2. Inside `_append_and_schedule_scroll` (the GLib idle callback that runs on main thread):
   - After appending cards + `_schedule_smart_scroll()`:
     - `if self._feed_tab is not None: self._feed_tab.update_auto_accept_state(self._auto_accept_enabled)`

**Verify:** open a project with a known prefs file → toggle visual matches persisted state. Open a project with no prefs file → toggle is OFF.

### Step 7 — Wire warning dialog in `ui/window.py`

In `ui/window.py`, class `Window`:

1. After `self._feed_handler = FeedHandler(...)` (around line 454), wire the warning callback:
   - `self._feed_handler.set_show_auto_accept_warning(self._show_auto_accept_warning_dialog)`
   - This matches the established callback-injection pattern (mirrors `on_approve_exec`).
2. Add method `_show_auto_accept_warning_dialog(self, agent_name: str, *, on_confirm: Callable, on_cancel: Callable) -> None`:
   - Build `Gtk.MessageDialog` with `transient_for=self`, `modal=True`, `message_type=MessageType.WARNING`, `buttons=ButtonsType.YES_NO`
   - `text` = f"Trust {agent_name} to auto-accept all file changes?"
   - `secondary_text` = f"Every new diff, file_created, file_modified, and file_deleted card from {agent_name} will be committed automatically. You can turn this off anytime in the bottom toolbar."
   - On response: if `YES` → `on_confirm()`, else → `on_cancel()`. Then `_dialog.close()`.
   - Wrap construction + show in `GLib.idle_add()` to guarantee main-thread execution (matches `agent_builder_handler.py` pattern).

**Verify:** manually click toggle in dev → dialog appears with correct text → YES persists state → NO snaps toggle back.

### Step 8 — Remove old batch-bar DOM construction (final cleanup)

Verify `grep -n "_batch_bar" ui/views/feed_tab.py` returns nothing. If lazy-creation code remains inside `update_batch_bar()`, remove it now (it should have been removed in Step 2, but this is a safety check). The `_batch_bar_count`, `_batch_bar_visible` attrs are gone; the batch button inside `_toolbar` replaces them.

### Step 9 — Add tests in `tests/test_feed_handler.py`

Add a new `TestFeedToolbarAutoAccept` class. Anchor: append after the existing `TestBatchAccept` class (after line 1170 area, before the `_FakeAdjustment` import).

Tests to add:

1. `test_default_auto_accept_is_off` — fresh `feed_handler` → `mock_feed_tab._auto_accept_active is False`.
2. `test_set_feed_tab_wires_auto_accept_callback` — `set_feed_tab()` populates `mock_feed_tab._auto_accept_callback`.
3. `test_enable_auto_accept_sets_state` — call `feed_handler._on_auto_accept_toggled(True)` (with no warning callback wired, falls through to enable path) → `feed_handler._auto_accept_enabled is True`.
4. `test_disable_auto_accept_sets_state` — start enabled → call `_on_auto_accept_toggled(False)` → `_auto_accept_enabled is False`.
5. `test_cancel_auto_accept_resets_toggle` — call `feed_handler.set_show_auto_accept_warning(mock)` where mock calls `on_cancel` immediately → `_on_auto_accept_toggled(True)` → `mock_feed_tab._auto_accept_active` is False (toggle snapped back).
6. `test_add_card_with_auto_accept_on_invokes_handle_accept` — set `_auto_accept_enabled = True`, add a `diff` card → assert `handle_accept` was called. **Note:** the check runs inside `_append` which is fired via `self._GLib.idle_add`. The existing `mock_glib` fixture records idle calls; verify the handle_accept call appears in `mock_glib._pending` rather than being called synchronously.

Plus 3 tests:

7. `test_auto_accept_only_for_actionable_cards` — auto-accept ON + add a `tool_result` card → `handle_accept` NOT called.
8. `test_auto_accept_only_for_matching_author_when_persisted` — set `_auto_accept_enabled=True`, `_auto_accept_agent="coder"`, add a card with `author="qa"` → `handle_accept` NOT called. Add a card with `author="coder"` → `handle_accept` IS called.
9. `test_add_card_without_auto_accept_is_passive` — `_auto_accept_enabled=False`, add an actionable `diff` card → `handle_accept` is NOT called (regression guard).

**Verify:** `pytest tests/test_feed_handler.py -k "TestFeedToolbarAutoAccept"` → all 9 pass. `pytest tests/test_feed_handler.py` → all 188 pass (182 original + 9 new in TestFeedToolbarAutoAccept, with the existing TestBatchAccept assertions adapted to the new mock attrs from Step 3/10).

### Step 10 — Update existing `TestBatchAccept` tests

The existing `TestBatchAccept` tests check `mock_feed_tab._batch_bar_visible` and `_batch_bar_count`. These attrs no longer exist. Update the assertions:
- `_batch_bar_visible` → check that the toolbar widget exists (or just remove the visibility check; the batch button's visibility is internal to FeedTab)
- `_batch_bar_count` → check `mock_feed_tab._batch_button_label` (or add a `self._batch_button_label` attr to MockFeedTab that records the last set label)

Concretely: in `MockFeedTab.update_batch_bar`, store `self._batch_button_label = f"Accept All ({count})"` and `self._batch_button_visible = count >= 2`. Update the 4 assertions in `TestBatchAccept` accordingly.

**Verify:** `pytest tests/test_feed_handler.py -k "TestBatchAccept"` → all pass.

### Step 11 — Run full test suite

`pytest tests/` (excluding GUI-only tests). Expected: all green. No test should rely on `_batch_bar` or `_batch_bar_visible` after this phase.

---

## §5. Test Plan

### §5.1 Unit tests (added in Step 9, Step 10)

| Test | Setup | Assertion |
|---|---|---|
| default_auto_accept_is_off | fresh handler | `_auto_accept_active is False` |
| set_feed_tab_wires_auto_accept_callback | fresh handler, set_feed_tab called | `mock_feed_tab._auto_accept_callback is callable` |
| enable_auto_accept_sets_state | handler, no warning cb | `_auto_accept_enabled is True` after toggled(True) |
| disable_auto_accept_sets_state | handler, enabled=True | `_auto_accept_enabled is False` after toggled(False) |
| cancel_auto_accept_resets_toggle | handler, mock warning cb invokes cancel | toggle snaps back to False |
| add_card_with_auto_accept_on_invokes_handle_accept | enabled=True, add diff card | handle_accept called (via idle_add) |
| auto_accept_only_for_actionable_cards | enabled=True, add tool_result | handle_accept NOT called |
| auto_accept_only_for_matching_author_when_persisted | enabled=True, agent="coder", add qa card then coder card | only coder card triggers handle_accept |
| add_card_without_auto_accept_is_passive | enabled=False, add diff card | handle_accept NOT called (regression guard) |

### §5.2 Manual UI tests (after app restart)

1. Open a fresh project. Toggle is OFF, batch button hidden.
2. Send a request to an agent → file-change card appears. Click the card's Accept button. Verify git commit happens (existing behavior).
3. Send another request → second file-change card appears. Verify batch button shows "Accept All (2)".
4. Click toggle ON. Verify warning dialog appears with correct text. Click NO → toggle snaps back OFF.
5. Click toggle ON again. Click YES → toggle stays ON, label flips to "Auto-Accept: ON".
6. Send a third request → verify the third card gets accepted automatically (no manual click needed; a `git_commit` card appears in its place).
7. Click toggle OFF. Send a fourth request → verify it appears as a pending card (no auto-accept).
8. Restart app. Reopen the same project → toggle should still be ON (persisted).
9. Open a different project → toggle should be OFF (per-project scope).
10. Verify bottom toolbar doesn't disturb scroll-to-bottom behavior when project opens (regression check for prior scroll fix).

### §5.3 Regression checks

- `pytest tests/test_feed_handler.py` — all pass
- `pytest tests/test_feed_card.py` — all pass
- `pytest tests/test_review_handler_feed_card.py` — all pass
- `pytest tests/test_feed_store.py` — all pass
- `pytest tests/test_low12_13_feed.py` — all pass

---

## §6. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Toggle persists but project has new agent → first card from new agent gets auto-accepted unexpectedly | Lazy lock-in happens inside `_append` on the first card after toggle ON: `self._auto_accept_agent = self._auto_accept_agent or card_data.author`. From then on the agent is non-None and only matching authors are auto-accepted. Persistence fires from that same path so reopening the project honors the lock-in. |
| User clicks YES to warning, then immediately wants to undo the auto-accept | Toggle is one click away. No undo needed. |
| Warning dialog appears on every OFF→ON transition → user fatigue | Acceptable: the cost of an unwanted auto-accept (committed code the PM didn't review) is high enough to justify one extra click per session. Add "don't show again" only if users complain. |
| Persistence write fails (disk full, permission denied) → toggle state diverges between memory and disk | On save failure, log warning and keep memory state. Next session reverts to persisted state. Log loudly so the user notices. |
| Race: card added during `_load_and_render` thread while main thread sets `_auto_accept_enabled` | Prefs loaded in `_load_and_render` thread, but `update_auto_accept_state` only runs inside the main-thread `_append_and_schedule_scroll` callback. After that callback runs, all subsequent `add_card` calls (which happen on main thread) see consistent state. |
| Card with `accepted is not None` (already accepted/rejected) gets re-processed | `add_card` checks `card.accepted is None` before auto-accept. Already-accepted cards pass through unchanged. |
| Batch button inside toolbar visually competes with the toggle | Toolbar is bottom-anchored, always 36–40px tall. Card container has 8px margin-top. No overlap. |
| Removal of `_batch_bar` breaks tests that mock `_batch_bar_visible` | Step 10 updates the mock + assertions explicitly. The 4 affected tests are listed. |

---

## §7. Out of Scope (deferred)

- Per-agent auto-accept matrix UI (Phase 6+)
- Auto-approving `needs_approval` exec-approval cards
- Auto-accepting `tool_result` cards
- Agent edit dialog changes
- Cross-project global toggle
- "Don't show warning again" checkbox

---

## §8. File Manifest

Files modified (8):
- `ui/views/feed_tab.py` — toolbar widget, remove `_batch_bar`
- `ui/handlers/feed_handler.py` — auto-accept state + hook + prefs load
- `ui/window.py` — wire `set_show_auto_accept_warning` callback + dialog method
- `ui/styles.py` — toolbar CSS classes
- `utils/feed_store.py` — prefs I/O
- `tests/test_feed_handler.py` — extend MockFeedTab, new test class, update TestBatchAccept
- (no new files in `ui/`)
- (no new files in `utils/`)

Files **not** modified (deliberate):
- `models/feed_card.py` — no new fields; `accepted: bool | None` already exists
- `ui/views/feed_card.py` — no changes; card UI doesn't need to know about auto-accept
- `agent/enforcement.py` — separation of concerns; feed prefs ≠ enforcement prefs
- `ui/handlers/review_handler.py` — review flow unchanged

---

## §9. Implementation Order

Steps 1 → 11 (in §4) are ordered to minimize broken-window time:
1. CSS first (pure addition, no risk)
2. FeedTab widget (additive, old `_batch_bar` still works)
3. Mock test extension (prepares for tests, no production change)
4. Prefs I/O (pure addition)
5. FeedHandler state (additive, default OFF so no behavior change)
6. Load prefs in on_project_opened (read-only at this point)
7. Window warning dialog (additive, only used when toggle clicked)
8. Remove `_batch_bar` (cleanup; only safe after step 2's new toolbar exists)
9. Tests (verify the new behavior)
10. Update existing tests (adapt to mock change from step 3)
11. Full suite green

Each step ends with a green partial test run.