# SPEC: Settings tab and project-bar transport controls

**Date:** 2026-10-19
**Author:** Micah
**Status:** Draft — for implementation
**Implements:** none (no prior proposal)
**Depends on:** none
**Target branch:** main

> Architecture compliance: views stay in `ui/views/`, CSS stays in `ui/styles.py`, and `ui/window.py` remains the only composition root. `ui/views/` must not import `ui/handlers/` at runtime (`tests/test_architecture.py::test_views_do_not_import_handlers`). Settings persistence stays in `SettingsHandler`; Telegram token/pairing stays in `TelegramSettingsController`; bridge start/stop stays in `TelegramBridgeHandler`; stop-all stays in `AgentRuntimeHandler.stop_all_agents`. This spec moves widgets and retargets existing callbacks. It does not add a handler and does not change those call paths.

---

## DISCOVERY

- Read `ui/toolbar.py`: `Toolbar.__init__(self, on_connect_clicked=None, *, on_settings_clicked=None, on_stop_all_clicked=None)`. The bar is only four controls: settings button (`_settings_btn`, label `"⚙ Settings"`, class `settings-toolbar-btn`, wrapped in an overlay with `_status_dot`), `_status_label`, `_connect_btn` (label `"Connect"` / `"Connecting…"` / `"Disconnect"`), `_stop_all_btn` (label `"■ Stop All"`, class `destructive-action`). Public painters: `set_settings_status(has_verified_provider: bool) -> None` (dot visible when `not has_verified_provider`), `set_telegram_bridge_state(state: str) -> None` (`"unconfigured" | "disconnected" | "connecting" | "connected" | "error"`, else treated as disconnected), `update_connection_state(state)` (legacy labels; production calls it once with `"offline"` and then overwrites it). Click handlers invoke the stored callbacks and do not paint state themselves.
- Read `ui/views/settings_dialog.py`: `SettingsDialog` is a modal `Gtk.Window` (560×480, class `settings-dialog`). Grouping is a `Gtk.Stack` plus a `Gtk.StackSwitcher` in the header. Pages, in order, via `add_titled`: `("providers", "Providers")`, `("telegram", "Telegram Bridge")`, `("decision-model", "Decision Model")`, `("stt", "STT Model")`, `("speech", "Speech")`. Default visible child is `"providers"` (first page). Constructor: `(parent, *, handler, on_close=None, telegram_controller=None)`. Page bodies, provider cards, and handler calls stay as they are. `refresh_providers(providers: list[ProviderConfig]) -> None` filters out the decision-model card by name `_DECISION_MODEL_PROVIDER` and syncs `_decision_card`. Close path `_on_close_request` calls `self._telegram.cancel_pairing()` inside `except Exception` then `self._on_close()`. Two `Gtk.MessageDialog`s use `transient_for=self._window`: provider remove (`_ProviderCard._on_remove_clicked`) and pairing confirm (`_on_telegram_pair_candidate`). `_labeled` and `_labeled_row` are horizontal rows with `label.set_size_request(100, -1)`.
- Read `ui/views/left_panel.py`: `LeftPanel` is a vertical box whose only child is `_PAP_notebook`, a `Gtk.Notebook` with three pages appended in order: Prompts, Agents, Projects. Nothing in the repo selects those pages by index. Handler injection is setter-based (`set_prompts_handler`, `set_agent_list_handler`) because the view must not import handlers. Default pane width is 250 (`ui/window.py` `paned.set_position(250)`).
- Read `ui/views/main_content.py`: `_project_settings` is a horizontal `Gtk.Box` (default orientation), CSS `project-feed-bar`, height 28, `margin_top` 4, `visible` False until `update_project_settings`. It is an overlay on the chat page, under the notebook tab strip (`create_chat_tab` comment: "below the tab row"). `update_project_settings(project_name, member_count, solo_target, auto_accept_level, branch_name)` rebuilds it. Falsy `project_name` → pill-only: one child, `_activity_pill`. Otherwise one `info_box` (`hexpand`, `START`) then the pill. `info_box` children, in order: project name, Chat button, Files button, Git branch label. Branch markup is `Git:` plus `⎇ {branch}`. `_clear_settings_bar` sibling-walks `get_first_child`/`remove` and only unparents direct children. `set_project_settings_text` and `set_feed_bar_text` have no production caller; they clear, append a label, re-append the pill. The bar is overlaid only once a chat tab exists (`create_chat_tab`, first tab only). `Gtk.Box()` at construction is not stored on `self` (`top_box` is local). Switch-page already unparents before `add_overlay` because `Gtk.Overlay` has no `remove()`.
- Read `ui/window.py`: `_wire_chat` builds `Toolbar(on_connect_clicked=self._on_connect_clicked, on_settings_clicked=self._open_settings, on_stop_all_clicked=lambda: self._on_stop_all_clicked())`, stores `self._toolbar`, calls `update_connection_state("offline")` then `_init_bridge_toolbar_state()`. `_wire_feed(toolbar, left_panel)` appends that toolbar to `main_box` above the paned. `_open_settings` builds a fresh `SettingsDialog` plus a fresh `TelegramSettingsController(GLib_module=GLib)` every click. `wire_settings_handler(self._settings_handler, self._toolbar, settings_dialog_factory=lambda: None, ...)`. `_on_connect_clicked(*args)` reads `telegram_store.load_bridge_config()` / `is_configured`; unconfigured → `set_telegram_bridge_state("unconfigured")` and return; no bridge → warning and return; `bridge.is_connected() or bridge.state == "connecting"` → `stop_bridge()` else `start_bridge()`. `_on_bridge_state_change(state)` paints the toolbar then, on `"error"`, may emit a feed card. `_on_stop_all_clicked` shows a confirm `Gtk.MessageDialog`; OK calls `arh.stop_all_agents()` (return value ignored; the method emits its own summary card) and `bridge.stop_bridge()`. `_on_close_request` stops the bridge and does not cancel pairing (pairing cancel lives on the dialog).
- Read `ui/wiring.py`: `wire_settings_handler(handler, toolbar, *, settings_dialog_factory=None, agent_builder_factory=None, on_runtimes_refresh=None) -> SettingsHandler`. Second arg is duck-typed: it only calls `toolbar.set_settings_status(bool)`. Idempotent via `handler._wired`. `on_providers_changed` calls `factory().refresh_providers(providers)` when the factory returns an object with that method. Today the window factory returns `None`, which is safe only because the dialog is rebuilt on each open.
- Read `ui/handlers/telegram_settings_controller.py`: `cancel_pairing(self) -> None` is idempotent (clears `_pairing`, sets `_pairing_stop`, tears down the throwaway transport). `TelegramSettingsController(*, GLib_module=None, transport_factory=None, client_factory=None, pairing_timeout_sec=_PAIRING_TIMEOUT_SEC)`.
- Read `ui/handlers/telegram_bridge_handler.py`: `BridgeState` strings are `"disconnected"`, `"connecting"`, `"connected"`, `"error"`. `"unconfigured"` is a toolbar-only state, not a bridge state. `is_connected() -> bool`, `state` property, `start_bridge() -> None`, `stop_bridge() -> None`.
- Read `ui/handlers/agent_runtime_handler.py`: `stop_all_agents(self) -> dict[str, str]`. Window ignores the dict. The method emits the summary feed card itself.
- Read `utils/telegram_store.py`: `load_bridge_config() -> dict`, `is_configured(data) -> bool` (token and `chat_id` both present).
- Read `ui/handlers/settings_handler.py`: `add_or_update(provider, *, allow_empty_caller=False) -> None` raises `ValueError` on empty name/url/key/model and on a bad caller. `test_provider(provider, on_result) -> None` does not raise for a bad caller; it dispatches a failed `TestResult`. `remove(name) -> None` no-ops when missing. `list_providers() -> list[ProviderConfig]`.
- Read `ui/styles.py`: `.project-feed-bar`, `.project-bar-agent`, `.project-bar-autoaccept` (flat, `min-height: 0`). `.toolbar-status-dot` is `color: #ef4444`. `.settings-dialog` sets `min-width: 560px; min-height: 480px`. No rule for `settings-toolbar-btn`.
- Read `ui/views/chat_input_toolbar.py`: `_icon_button` uses `Gtk.Button.set_icon_name`. This spec uses `Gtk.Image.new_from_icon_name` + `set_pixel_size` (same call shape as `file_tree.py` `go-previous-symbolic` and `main_content.py` mic image) so the glyph is 12px inside a 28px bar. Icon theme on this machine has `network-transmit-receive-symbolic` and `process-stop-symbolic`.
- Read tests that pin the current widgets: `tests/test_toolbar.py`, `tests/test_window_telegram_bridge.py` (mocks `win._toolbar`), `tests/test_window_settings_wiring.py::TestSettingsDialogLifecycle` (calls `dlg._window.close()` and expects a new dialog per open), `tests/test_main_content_settings_bar.py` (pill-only = 1 child; full bar = `[info_box, pill]`; branch label is `info_box.children[-1]`; gear absence pin says the toolbar is the sole settings path), `tests/test_no_gateway_residuals.py::test_toolbar_tooltip_honest` (reads `ui/toolbar.py`), `tests/test_settings_telegram_section.py::TestSectionedLayout` (stack names `"providers"` / `"telegram"`, default visible child `"providers"`).
- Architecture owner: window composition root owns wiring (`docs/ARCHITECTURE.md` §3.6). `SettingsHandler` owns provider data. `LeftPanel` owns the notebook. `MainContent` owns the project bar. `SettingsDialog` owns settings widgets and delegates I/O.
- Existing patterns to copy: activity-pill re-append after `_clear_settings_bar`; `widget.unparent()` then `overlay.add_overlay` in `_on_notebook_switch_page`; setter injection into `LeftPanel`; `wire_settings_handler`'s duck-typed `set_settings_status`.

---

## 1. Overview

### Problem

The top toolbar is a 40px strip whose only controls are Settings, a connection status sentence, Connect, and Stop All. Settings opens a 560×480 modal whose five groups are a horizontal `Gtk.StackSwitcher` in the header. That switcher and those form rows (100px label beside each field) do not fit the left sidebar, and the modal is the thing leaving the toolbar.

Connect and Stop All are app-global. They sit on that same top strip, far from the project group-chat bar the user is looking at (project name, Chat, Files, Git branch, activity pill).

### Solution

1. Add a **Settings** page as the fourth tab of the left notebook (after Projects). Host the existing settings pages there. Replace the header switcher with a vertical group list — same five names, same order, one group visible at a time. Stack the field label above the control so a ~250px column can hold the forms. Retire the modal window and the toolbar Settings button. Move the red "no verified provider" dot onto the Settings tab label.
2. Move Connect and Stop All onto the project bar, immediately after the Git branch label: a 12px connect icon, a tiny round pip button beside it, and a 12px stop icon. No text. The pip is gray, amber, green, or red for the five bridge states the toolbar already paints. Clicking the icon or the pip runs today's `_on_connect_clicked`. Clicking stop runs today's confirm dialog. Remove the top toolbar; after this move it would be an empty 40px strip.

### Scope

| In | Out |
|----|-----|
| Left-notebook Settings tab hosting the five existing groups | New settings, new providers, new persistence |
| Vertical group selector replacing the header `StackSwitcher` | Changing stack page names or default page |
| Label-above-field layout for `_labeled` / `_labeled_row` | Rewriting provider cards, STT, speech, or Telegram form fields |
| Red dot on the Settings tab label | A second settings entry point |
| Icon + pip + stop icon on the project bar | Changing connect/stop/confirm/bridge logic |
| Deleting the top `Toolbar` widget and its 40px strip | Touching `ChatInputToolbar` (find/replace, spell check) |
| Tests and `ARCHITECTURE.md` updated to match | Phone `/stop`, remote status, feed cards |

### Architecture principles

- §8.6: no new handler. Click callbacks already live on `MainWindow` and keep living there.
- §8.2 / §8.6 R7: `LeftPanel` receives the settings widget via `set_settings_pane`. It does not construct `SettingsDialog` or import a handler.
- §9: new look is CSS classes in `ui/styles.py` only.
- One parent per widget. The transport cluster and the activity pill are singletons, detached before every bar rebuild.

---

## 2. Changes by File

### 2.1 `ui/views/settings_dialog.py` — REVISED

Stop building a `Gtk.Window`. Build a vertical `Gtk.Box` the notebook can parent.

**Constructor stays source-compatible** for `tests/test_settings_dialog.py`, `tests/test_settings_telegram_section.py`, and `tests/test_stt_settings_page.py`:

```python
def __init__(
    self,
    parent: Gtk.Window | None,
    *,
    handler: SettingsHandler,
    on_close=None,
    telegram_controller=None,
) -> None:
```

`parent` is stored as `self._parent` (the `MainWindow`, or `None` in tests). It is no longer `transient_for` of a settings window. `on_close` is stored and invoked only from `release()` below. Tests that pass `on_close=lambda: None` keep working.

**Delete** `_window`, the header bar, the Close button, `Gtk.StackSwitcher`, `show()`, `close()`, and `_on_close_request`. Do not put CSS class `settings-dialog` on the embedded root — that rule is `min-width: 560px` and would force the sidebar open.

**Root layout**, top to bottom:

1. `_group_box` — vertical `Gtk.Box`, spacing 4, margins 8. Five `Gtk.Button`s, one per existing stack page, in this order (these are the current `add_titled` title strings):

   | `set_visible_child_name` | Button label |
   |---------------------------|--------------|
   | `providers` | Providers |
   | `telegram` | Telegram Bridge |
   | `decision-model` | Decision Model |
   | `stt` | STT Model |
   | `speech` | Speech |

2. A `Gtk.ScrolledWindow` (`vexpand`, horizontal policy `NEVER`, vertical `AUTOMATIC`) whose child is the existing `_stack`.

The stack, its five `add_titled` names, and the page builders (`_build_telegram_page`, `_build_decision_page`, `_build_stt_page`, `_build_speech_page`, the providers list) stay. `TestSectionedLayout` asserts `_stack.get_child_by_name("providers")`, `"telegram"`, and `get_visible_child_name() == "providers"`. Do not rename pages. Do not call `set_visible_child_name` to anything other than `"providers"` during `__init__` before the constructor returns — the default first page is already `"providers"`. After the buttons exist, mark the Providers button active (CSS only) without switching the stack away from `"providers"`.

```python
_SETTINGS_GROUPS = (
    ("providers", "Providers"),
    ("telegram", "Telegram Bridge"),
    ("decision-model", "Decision Model"),
    ("stt", "STT Model"),
    ("speech", "Speech"),
)

def _select_group(self, name: str) -> None:
    """Show one existing stack page and highlight its button.

    ``name`` is an add_titled child name. Returns None.
    Gtk.Stack.set_visible_child_name does not raise for a known name.
    """
    self._stack.set_visible_child_name(name)
    for group_name, button in self._group_buttons:
        if group_name == name:
            button.add_css_class("settings-group-active")
        else:
            button.remove_css_class("settings-group-active")
```

Each button's `"clicked"` handler calls `_select_group` with its name. Button CSS class: `settings-group-btn`. `hexpand` True. Child of the button is a `Gtk.Label` with `set_wrap(True)`, `set_xalign(0)`, so "Telegram Bridge" and "Decision Model" wrap inside the 250px pane instead of clipping.

**Public widget accessor** (new):

```python
def widget(self) -> Gtk.Widget:
    """The box LeftPanel parents into the Settings page. No window."""
    return self._root
```

**`release(self) -> None`** replaces `_on_close_request`. Same body, minus `return False`:

```python
def release(self) -> None:
    """Cancel an in-flight Telegram pairing poll. Idempotent.

    cancel_pairing() -> None and is safe to call when no poll is running.
    Broad catch matches the old close-request handler: teardown must not
    block window close or a notebook switch.

    Do not call _refresh_telegram_section() here. That method writes the
    stored token back into _telegram_token_entry and would wipe a token
    the user had typed but not saved. Only re-enable Start Pairing and
    clear the "Pairing…" line.
    """
    if self._telegram is not None:
        try:
            self._telegram.cancel_pairing()
        except Exception as e:  # noqa: BLE001
            logger.warning("telegram pairing teardown failed: %s", e)
        pair_btn = getattr(self, "_telegram_pair_btn", None)
        if pair_btn is not None:
            pair_btn.set_sensitive(self._telegram.has_saved_token())
        if getattr(self, "_telegram_status_label", None) is not None:
            self._telegram_set_status("")
    if self._on_close is not None:
        self._on_close()
```

`on_close` may run more than once (leave the Settings tab, later quit). The only production callback today is unused (`lambda: None` inside `_open_settings`, which this spec deletes). Keep the callback idempotent.

**Message dialogs.** Both sites that pass `transient_for=self._window` pass `transient_for=self._parent` instead. `parent=None` in tests is valid for `Gtk.MessageDialog`. Sites:

- `_ProviderCard._on_remove_clicked` — `Gtk.MessageDialog(..., transient_for=self._dialog._parent, ...)`
- `SettingsDialog._on_telegram_pair_candidate` — same, `transient_for=self._parent`

**Label layout.** `_ProviderCard._labeled` and `SettingsDialog._labeled_row` become vertical so the entry gets the full column:

```python
def _labeled(self, text: str, widget: Gtk.Widget) -> Gtk.Box:
    row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    label = Gtk.Label(label=text)
    label.set_halign(Gtk.Align.START)
    label.add_css_class("dim-label")
    widget.set_hexpand(True)
    row.append(label)
    row.append(widget)
    return row
```

`_labeled_row` gets the same body. Drop `set_size_request(100, -1)`. No test asserts that size request.

`refresh_providers` is unchanged. Return value is `None`.

Imports: no new modules. Still no runtime import of `ui.handlers` (`SettingsHandler` stays under `TYPE_CHECKING`).

Line estimate: −80 window/header, +70 group rail and `release`/`widget`.

### 2.2 `ui/views/left_panel.py` — REVISED

In `__init__`, after the Projects `append_page`, append a fourth page.

```python
self._settings_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
self._settings_page.set_vexpand(True)

self._settings_tab_label = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
self._settings_tab_label.append(Gtk.Label(label="Settings"))
self._settings_dot = Gtk.Label(label="●")
self._settings_dot.add_css_class("settings-tab-dot")
self._settings_dot.set_visible(False)
self._settings_tab_label.append(self._settings_dot)

PAP_notebook.append_page(self._settings_page, self._settings_tab_label)
```

The dot starts hidden, matching `Toolbar._status_dot.set_visible(False)`.

```python
def set_settings_pane(self, widget: Gtk.Widget) -> None:
    """Parent the settings box into the Settings page. Call once.

    Raises no application exception. GTK raises if ``widget`` already has
    a parent; the window passes ``SettingsDialog.widget()`` before it is
    parented, so that does not happen on the production path.
    """
    widget.set_vexpand(True)
    self._settings_page.append(widget)

def set_settings_status(self, has_verified_provider: bool) -> None:
    """Show the red dot when no provider is verified.

    Same contract as the deleted Toolbar.set_settings_status:
    visible iff ``not has_verified_provider``. Returns None.
    """
    self._settings_dot.set_visible(not has_verified_provider)

def set_on_settings_hidden(self, cb) -> None:
    """``cb() -> None``, fired when the notebook leaves the Settings page."""
    self._on_settings_hidden = cb
```

Initialize `self._on_settings_hidden = None` in `__init__`.

Connect the notebook once:

```python
PAP_notebook.connect("switch-page", self._on_pap_switch_page)

def _on_pap_switch_page(self, _notebook, page, _page_num) -> None:
    """Gtk.Notebook ``switch-page`` (notebook, page, page_num).

    Pairing stays alive while the user moves between the five groups
    (that is an inner stack, not this notebook). Leaving the Settings
    page cancels it, which is the old dialog-close behavior.
    """
    if page is self._settings_page:
        return
    cb = self._on_settings_hidden
    if cb is not None:
        cb()
```

`switch-page` does not fire during `__init__`, so construction does not cancel pairing.

Agents-tab placeholder text still says "Click Connect to discover agents". Leave it. It is already stale relative to the Telegram bridge and is not this change.

Line estimate: +45.

### 2.3 `ui/views/main_content.py` — REVISED

#### Transport cluster (built once in `__init__`)

After `_activity_pill` is created, build `_transport_cluster` and three children. Do not rebuild them inside `update_project_settings`.

| Widget | Attribute | Contents | Click |
|--------|-----------|----------|-------|
| Cluster | `_transport_cluster` | horizontal `Gtk.Box`, spacing 2, CSS `project-bar-transport` | — |
| Connect | `_connect_btn` | `Gtk.Button`, CSS `project-bar-icon`, child `Gtk.Image.new_from_icon_name("network-transmit-receive-symbolic")` with `set_pixel_size(12)` | `_on_transport_connect_clicked` |
| Pip | `_connect_pip` | `Gtk.Button` with no label, CSS `project-bar-connect-pip` and `project-bar-connect-off` | same callback |
| Stop | `_stop_all_btn` | `Gtk.Button`, CSS `project-bar-icon` and `project-bar-stop`, child `Gtk.Image.new_from_icon_name("process-stop-symbolic")` with `set_pixel_size(12)` | `_on_transport_stop_clicked` |

Store callbacks as `self._on_connect_clicked = None` and `self._on_stop_all_clicked = None` (setters below). The click methods:

```python
def _on_transport_connect_clicked(self, *_args) -> None:
    if self._on_connect_clicked is not None:
        self._on_connect_clicked()

def _on_transport_stop_clicked(self, *_args) -> None:
    if self._on_stop_all_clicked is not None:
        self._on_stop_all_clicked()
```

No label rewrite after the click. The window paints state through `set_telegram_bridge_state`, same rule as the deleted `_on_connect_click` (it must not clobber `"connecting"`).

Tooltips at construction (disconnected copy; `set_telegram_bridge_state` replaces the connect/pip tips):

- Connect and pip: `"Connect the Supervisor to your phone over Telegram (chat + exec approvals; configure a bot token in Settings)"`
- Stop: `"Halt every agent: cancel in-flight turns, kill spawned process groups, abort pending review checkpoints."` — the current `_stop_all_btn` tooltip in `ui/toolbar.py`.

```python
def set_on_connect_clicked(self, cb) -> None:
    self._on_connect_clicked = cb

def set_on_stop_all_clicked(self, cb) -> None:
    self._on_stop_all_clicked = cb
```

#### `set_telegram_bridge_state(self, state: str) -> None`

Same five states as `Toolbar.set_telegram_bridge_state`. No button labels. The pip class and both tooltips change. Unknown `state` uses the disconnected row (the toolbar's `else` branch).

| `state` | Pip class added | Tooltip on `_connect_btn` and `_connect_pip` |
|---------|-----------------|-----------------------------------------------|
| `unconfigured` | `project-bar-connect-off` | `No transport (configure a bot token in Settings → Telegram Bridge)` |
| `disconnected` | `project-bar-connect-off` | `Connect the Supervisor to your phone over Telegram (chat + exec approvals; configure a bot token in Settings)` |
| `connecting` | `project-bar-connect-pending` | `Connecting…` |
| `connected` | `project-bar-connect-on` | `Disconnect the Telegram bridge` |
| `error` | `project-bar-connect-error` | `Offline — check the bot token and pairing in Settings` |
| anything else | `project-bar-connect-off` | same text as `disconnected` |

Before adding the new class, remove all four pip classes from `_connect_pip`. Returns `None`. Does not call the bridge. Does not touch the stop button.

Colors match the markup the toolbar uses today: off `#6b6b7a`, pending `#f59e0b`, on `#22c55e`, error `#ef4444`.

#### Where the cluster sits

`_project_settings` is horizontal. `info_box` is `hexpand` + `START`, so a cluster appended as a sibling of `info_box` would sit next to the activity pill, not next to the Git label. Append the cluster **inside `info_box`, after `branch_label`**.

`_clear_settings_bar` only unparents direct children of `_project_settings`. A cluster parented to `info_box` is still parented to that box after the clear. Unparent it explicitly on every rebuild before appending it again. `Gtk.Widget.unparent` is already the pattern in `_on_notebook_switch_page`.

```python
def _detach(self, widget) -> None:
    if widget.get_parent() is not None:
        widget.unparent()
```

`update_project_settings` — falsy `project_name` (pill-only). There is no Git label. The cluster is still shown, because Connect and Stop All are app-global and this is the only bar they live on:

```python
if not project_name:
    self._clear_settings_bar()
    self._detach(self._transport_cluster)
    self._project_settings.append(self._transport_cluster)
    self._project_settings.append(self._activity_pill)
    self._project_settings.set_visible(True)
    return
```

Non-empty path: after `info_box.append(branch_label)`, `_detach(self._transport_cluster)` then `info_box.append(self._transport_cluster)`. Then append `info_box` and `_activity_pill` to `_project_settings` as today. Top-level children stay exactly two: `info_box`, pill.

`set_project_settings_text` and `set_feed_bar_text`: after the clear, `_detach(self._transport_cluster)`, append the label (when they do today), append the cluster, append the pill. No production caller; the tests require the pill to survive, and the cluster has the same contract.

#### Visible before the first chat tab

Today `_project_settings` is `visible` False and is parented only when `create_chat_tab` overlays it on the first page. With the top toolbar gone, Connect and Stop All would be unreachable until a project open calls `update_project_settings`. Fix that without inventing a fake chat tab.

1. Store the local `top_box` as `self._top_box`.
2. At the end of `__init__`, run the pill-only shape and show the bar: `update_project_settings("", 0, None, "off", None)` (that sets `visible` True). Then `self._top_box.prepend(self._project_settings)`. `prepend` places it above the notebook. That is the zero-tab host only. Delete the placeholder `"Project Settings"` label that `__init__` appends today; the pill-only rebuild replaces it.
3. In `create_chat_tab`, the first-tab branch currently calls `add_overlay` with no unparent. After step 2 the bar has a parent, and `add_overlay` will fail. Unparent first, using the same guard as `_on_notebook_switch_page`:

```python
if len(self._tab_sessions) == 0:
    for widget in (self._project_settings, self._scroll_btn_box):
        if widget.get_parent() is not None:
            widget.unparent()
        chat_overlay.add_overlay(widget)
```

`_scroll_btn_box` has no parent at this moment today; the guard is a no-op for it. Once overlaid, the bar sits under the tab strip, which is the group-chat placement. The above-notebook position is only the zero-tab fallback.

4. In `_close_tab`, after `remove_page`, when `get_n_pages() == 0`, the overlay is gone and the bar's parent is `None` (page removal unparents overlay children; the Python attribute keeps the widget). Re-host it:

```python
if self._chat_notebook.get_n_pages() == 0:
    self._chat_notebook.set_show_tabs(False)
    self._detach(self._project_settings)
    self._top_box.prepend(self._project_settings)
    self._project_settings.set_visible(True)
```

Do this even when `_bulk_closing` is true. The last page is the one that hits `n_pages == 0`.

`_on_notebook_switch_page` already moves the bar between tab overlays. Do not also prepend it to `top_box` on a switch.

Line estimate: +120.

### 2.4 `ui/window.py` — REVISED

**Delete** `from ui.toolbar import Toolbar` and every `Toolbar(...)` / `self._toolbar` use.

`_wire_chat` no longer builds a toolbar and no longer returns one. After `MainContent()` exists:

```python
self._main_content.set_on_connect_clicked(self._on_connect_clicked)
self._main_content.set_on_stop_all_clicked(lambda: self._on_stop_all_clicked())
self._init_bridge_toolbar_state()
```

Delete the `update_connection_state("offline")` call. It is overwritten immediately by `_init_bridge_toolbar_state` today, and the legacy painter goes away with the toolbar.

`_build` stops passing a toolbar into `_wire_feed`. Signature becomes `_wire_feed(self, left_panel)`. Inside `_wire_feed`, `main_box` starts with the paned. Delete `main_box.append(toolbar)`. The bottom status bar (`Agent: —`) stays.

**Settings pane**, in `_wire_agents`, after `SettingsHandler(...)` is constructed and before `wire_settings_handler`:

```python
from ui.handlers.telegram_settings_controller import TelegramSettingsController
from ui.views.settings_dialog import SettingsDialog

self._telegram_settings = TelegramSettingsController(GLib_module=GLib)
self._settings_pane = SettingsDialog(
    parent=self,
    handler=self._settings_handler,
    telegram_controller=self._telegram_settings,
)
self._left_panel.set_settings_pane(self._settings_pane.widget())
self._left_panel.set_on_settings_hidden(self._settings_pane.release)
```

`GLib` is the module-level import already at the top of `window.py`. One controller for the life of the window, not one per click.

```python
self._settings_handler = wire_settings_handler(
    self._settings_handler,
    self._left_panel,
    settings_dialog_factory=lambda: self._settings_pane,
    agent_builder_factory=lambda: getattr(self, "_builder_dialog", None),
    on_runtimes_refresh=self._on_providers_changed,
)
```

`LeftPanel.set_settings_status` satisfies the second argument. The factory must return the live pane. Today's `lambda: None` would leave the persistent cards stale after a provider save, because `on_providers_changed` is the only refresh path and it no-ops on `None`.

Delete `_open_settings`.

**`_init_bridge_toolbar_state`** keeps its name. `tests/test_window_telegram_bridge.py::test_build_wires_initial_bridge_toolbar_state` asserts the source still contains `self._init_bridge_toolbar_state()`. Retarget the painter:

```python
def _init_bridge_toolbar_state(self) -> None:
    main = getattr(self, "_main_content", None)
    if main is None:
        return
    try:
        from utils import telegram_store
        configured = telegram_store.is_configured(
            telegram_store.load_bridge_config()
        )
    except Exception:
        logger.exception("failed to read Telegram bridge config at startup")
        configured = False
    main.set_telegram_bridge_state(
        "disconnected" if configured else "unconfigured"
    )
```

`load_bridge_config` returns a dict and does not raise on a missing file (controller docstring: never raises). The `except Exception` is the existing startup guard; keep it.

**`_on_connect_clicked`** — logic unchanged. The one paint call becomes `self._main_content.set_telegram_bridge_state("unconfigured")`. Guard: if `_main_content` is missing, return before that call (the telegram tests build a bare `MainWindow.__new__`). `is_configured` / `is_connected` / `state` / `start_bridge` / `stop_bridge` signatures are unchanged. `start_bridge` and `stop_bridge` return `None`.

**`_on_bridge_state_change`** — replace the `self._toolbar` lookup with `self._main_content`. Feed-card behavior on `"error"` stays.

**`_on_stop_all_clicked`** — unchanged. The icon only invokes this callback. `stop_all_agents()` returns `dict[str, str]`; keep ignoring it. The method writes the summary card internally (`_emit_stop_all_card`). `stop_bridge()` returns `None`.

**`_on_close_request`** — after the existing bridge `stop_bridge()`, also:

```python
pane = getattr(self, "_settings_pane", None)
if pane is not None:
    try:
        pane.release()
    except Exception:
        logger.exception("close-request: settings release failed")
```

`release` already catches `cancel_pairing` failures. This outer catch covers a `release` that itself raises (it should not; the catch matches the bridge shutdown next to it).

Line estimate: −40 toolbar, +25 settings wiring.

### 2.5 `ui/wiring.py` — REVISED (docstring only)

`wire_settings_handler`'s second parameter stays named `toolbar` so every positional test call keeps working. It is any object with `set_settings_status(bool) -> None`. Update the docstring to say that. The window passes `LeftPanel`. No logic change. Initial status call and the `except Exception` around it stay.

### 2.6 `ui/styles.py` — REVISED

Add classes. Views only call `add_css_class`.

```css
button.settings-group-btn {
    background: transparent;
    color: #cfd8e8;
    border: none;
    box-shadow: none;
    border-radius: 4px;
    padding: 4px 8px;
    min-height: 0;
}
button.settings-group-btn.settings-group-active {
    background: rgba(99, 102, 241, 0.35);
    color: #e0e7ff;
}
button.project-bar-icon {
    min-height: 0;
    min-width: 0;
    padding: 0 2px;
    margin: 0;
    background: transparent;
    border: none;
    box-shadow: none;
}
button.project-bar-stop {
    color: #ef4444;
}
button.project-bar-connect-pip {
    min-height: 8px;
    min-width: 8px;
    padding: 0;
    margin: 0 2px;
    border-radius: 8px;
    border: none;
    box-shadow: none;
    background: #6b6b7a;
}
button.project-bar-connect-pip.project-bar-connect-off { background: #6b6b7a; }
button.project-bar-connect-pip.project-bar-connect-pending { background: #f59e0b; }
button.project-bar-connect-pip.project-bar-connect-on { background: #22c55e; }
button.project-bar-connect-pip.project-bar-connect-error { background: #ef4444; }
.settings-tab-dot {
    color: #ef4444;
    font-size: 14px;
    font-weight: 700;
}
```

Delete `.toolbar-status-dot` and `.settings-dialog` (the 560px minimum). Leave `.settings-provider-card`, `.settings-test-btn`, `.settings-remove-btn`, `.settings-status-*`.

### 2.7 `ui/toolbar.py` — DELETED

The class has no remaining control. Deleting it removes the empty 40px strip. `ChatInputToolbar` in `ui/views/chat_input_toolbar.py` is a different widget and stays.

### 2.8 Tests

**`tests/test_toolbar.py` — DELETED.** Replace with `tests/test_project_bar_transport.py` (GTK required, same `pytest.importorskip("gi")` skip as `test_toolbar.py`). Construct a real `MainContent()`:

- `_connect_btn`, `_connect_pip`, `_stop_all_btn` exist; none of their labels are `"Connect"`, `"Disconnect"`, `"Connecting…"`, or `"■ Stop All"`.
- `set_telegram_bridge_state("connected")` adds `project-bar-connect-on` and removes `project-bar-connect-off`. `"connecting"` adds `project-bar-connect-pending`. `"error"` adds `project-bar-connect-error`. `"unconfigured"` adds `project-bar-connect-off` and the tooltip contains `"No transport"`. An unknown string (`"offline"`) adds `project-bar-connect-off`.
- Connect click and pip click both append to the same callback list. With the callback setting state to `"connecting"`, the pip class is `project-bar-connect-pending` after the click (the click handler must not paint over it).
- Stop click fires its callback. Missing callbacks do not raise.
- Stop tooltip contains `"Halt every agent"`. Connect tooltip contains `"Telegram"` and does not contain `"post-MVP"` or `"gateway"`.

**`tests/test_main_content_settings_bar.py` — REVISED.** Fixture sets `instance._transport_cluster = _FakeBox()` and `instance._top_box` is not required (these tests do not call `__init__`). Add `unparent` to `_FakeWidget`: if `_parent` has `children` and `self` is in them, remove it, then set `_parent = None`.

Assertions to change:

- Pill-only: two children, `[cluster, pill]`, bar visible.
- Non-empty: still two top-level children, `[info_box, pill]`. `info_box`'s last child is `mc._transport_cluster`. The Git label is the child before it; `test_xml_escape_for_branch` finds it with `"Git:" in (child.markup or "")`, not `children[-1]`.
- `set_project_settings_text` / `set_feed_bar_text`: pill is the last child, and `mc._transport_cluster` is also a child.
- Gear-absence comments that say the toolbar is the sole settings path: the Settings tab is the sole path. Keep the assertions that `MainContent` has no `set_on_settings_clicked`.

**`tests/test_window_telegram_bridge.py` — REVISED.** Fixture sets `w._main_content = MagicMock()` and does not set `_toolbar`. Every `win._toolbar.set_telegram_bridge_state` becomes `win._main_content.set_telegram_bridge_state`. `test_build_wires_initial_bridge_toolbar_state` still looks for the string `self._init_bridge_toolbar_state()` in `ui/window.py`. `test_no_bridge_wired_configured_is_safe` still expects no paint call when the bridge is missing and the store is configured (`_on_connect_clicked` returns before painting in that branch).

**`tests/test_window_settings_wiring.py::TestSettingsDialogLifecycle` — REPLACED.** The fresh-dialog-per-open contract is retired. New test, source-shape plus a constructed pane:

- `ui/window.py` contains `set_settings_pane(` and does not contain `def _open_settings`.
- `SettingsDialog(parent=None, handler=SettingsHandler()).widget()` returns a widget, and the instance has no `_window`.
- `release()` with `telegram_controller=None` and `on_close=None` does not raise. With a mock controller, `release()` calls `cancel_pairing` once.

The duck-typed `wire_settings_handler(h, stub)` tests stay. They never needed a real toolbar.

**`tests/test_no_gateway_residuals.py::test_toolbar_tooltip_honest` — REVISED.** The file `ui/toolbar.py` will not exist. Point the read at `ui/views/main_content.py` and keep the assertion: the source lowercased does not contain `"gateway"`. Rename the test to `test_project_bar_transport_has_no_gateway_word`.

**`tests/test_left_panel_settings_tab.py` — NEW** (GTK skip). `LeftPanel()`:

- Notebook has 4 pages; the last tab label contains a `"Settings"` label.
- `set_settings_status(False)` shows the dot; `True` hides it.
- `set_on_settings_hidden` callback runs from `_on_pap_switch_page` when `page is not panel._settings_page`, and does not run when `page is panel._settings_page`.

**`tests/test_settings_dialog.py` and the section tests** keep constructing `SettingsDialog(parent=None, handler=...)`. They must still pass without a window. Do not add a test that calls `show()` or `_window`.

### 2.9 `docs/ARCHITECTURE.md` — REVISED

Same commit as the code (document rule §0).

- §2 tree and the §11 inventory: remove `ui/toolbar.py`. Note `left_panel.py` is a four-tab notebook (Prompts, Agents, Projects, Settings).
- §3.4: delete the top-bar section. Say the app-level toolbar is gone. Connect state is `MainContent.set_telegram_bridge_state`. The unverified-provider dot is `LeftPanel.set_settings_status`.
- §3.6 wiring table: remove the row that says the toolbar Settings button is the sole settings path. Settings pane is created in `_wire_agents` and parented with `set_settings_pane`. `wire_settings_handler` receives the left panel and `settings_dialog_factory=lambda: self._settings_pane`.
- §3.7: fourth tab. Public API gains `set_settings_pane`, `set_settings_status`, `set_on_settings_hidden`.
- §3.11 project bar (the `update_project_settings` block): document the transport cluster after the Git label, pill-only inclusion, and the zero-tab `top_box` host.

---

## 3. Data Flow

### Open Settings

User clicks the **Settings** notebook tab. No callback. The pane was built once in `_wire_agents`:

`SettingsHandler(...)` → `TelegramSettingsController(GLib_module=GLib)` → `SettingsDialog(parent=window, handler=..., telegram_controller=...)` → `left_panel.set_settings_pane(pane.widget())`.

Group click → `SettingsDialog._select_group(name)` → `_stack.set_visible_child_name(name)`. Page contents call the same handler methods as today:

- Provider Save → `_handler.add_or_update(provider, allow_empty_caller=...)` which raises `ValueError` (caught in `_on_save_clicked`) or saves and fires `on_providers_changed`.
- `wire_settings_handler` closure → `settings_dialog_factory()` → `pane.refresh_providers(providers)` and `left_panel.set_settings_status(has_any_verified_provider(...))`.
- Provider Test → `_handler.test_provider(provider, on_result)`. Bad caller becomes a failed `TestResult`, not a raise.
- Telegram Save / Test / Pair → `TelegramSettingsController` methods, unchanged. Pairing confirm dialog is `transient_for` the main window.

Leaving the Settings page: notebook `switch-page` → `LeftPanel._on_pap_switch_page` → `pane.release()` → `cancel_pairing()`.

Window close: `_on_close_request` → `bridge.stop_bridge()` (existing) and `pane.release()`.

### Connect

User clicks the connect icon or the pip on the project bar.

`MainContent._on_transport_connect_clicked` → `MainWindow._on_connect_clicked`:

1. `cfg = telegram_store.load_bridge_config()` then `telegram_store.is_configured(cfg)`.
2. Not configured → `main_content.set_telegram_bridge_state("unconfigured")` and return. Pip class `project-bar-connect-off`. Bridge not started.
3. `bridge is None` → log warning, return, no paint.
4. `bridge.is_connected() or bridge.state == "connecting"` → `bridge.stop_bridge()`. Else `bridge.start_bridge()`.
5. State changes arrive on the main thread via `TelegramBridgeHandler._set_state` → `on_state_change` → `_on_bridge_state_change(state)` → `main_content.set_telegram_bridge_state(state)`. `"error"` may also emit the existing feed card. The click handler does not set the pip itself.

Startup: `_init_bridge_toolbar_state` paints `"disconnected"` or `"unconfigured"` before any click.

### Stop All

User clicks the stop icon.

`MainContent._on_transport_stop_clicked` → `MainWindow._on_stop_all_clicked` → confirm dialog. Cancel closes the dialog and does not call the handler. OK → `self._agent_runtime_handler.stop_all_agents()` (dict return ignored; card emitted inside the method) and, if present, `self._bridge_handler.stop_bridge()`. Bridge stop then paints `"disconnected"` through `_on_bridge_state_change`.

The phone path `_remote_stop_all` is unchanged and does not show the dialog.

### Bar rebuild

Project open → `_on_feed_bar_update(name, member_count)` → `update_project_settings(...)` → clear → new `info_box` (name, Chat, Files, Git, transport cluster) → activity pill.

Project close → `_on_feed_bar_update(None, 0)` → pill-only: cluster, then pill.

Any path that clears the bar detaches the cluster first so it can be reparented. A widget has one parent.

---

## 4. File Change Summary

| File | Change | Lines (est.) | Risk |
|------|--------|--------------|------|
| `ui/views/settings_dialog.py` | Drop the window; vertical group list; `widget`/`release`; vertical labels | ~150 touched | Medium — dialog tests construct this class |
| `ui/views/left_panel.py` | Fourth notebook page, dot, hide callback | +45 | Low |
| `ui/views/main_content.py` | Transport cluster, bar placement, zero-tab host | +120 | Medium — overlay parenting |
| `ui/window.py` | Wire pane and bar; delete toolbar mount | ~60 touched | Medium — composition order |
| `ui/wiring.py` | Docstring | ~5 | Low |
| `ui/styles.py` | New classes; drop toolbar/dialog min-size | ~40 | Low |
| `ui/toolbar.py` | Delete | −194 | Low once call sites are gone |
| `tests/test_toolbar.py` | Delete | — | — |
| `tests/test_project_bar_transport.py` | New | +120 | Low |
| `tests/test_main_content_settings_bar.py` | Child-order updates, `unparent` on the fake | ~40 | Low |
| `tests/test_window_telegram_bridge.py` | Mock `_main_content` | ~20 | Low |
| `tests/test_window_settings_wiring.py` | Replace lifecycle test | ~30 | Low |
| `tests/test_no_gateway_residuals.py` | Point the pin at `main_content.py` | ~5 | Low |
| `tests/test_left_panel_settings_tab.py` | New | +60 | Low |
| `docs/ARCHITECTURE.md` | §2, §3.4, §3.6, §3.7, §3.11, §11 | docs | Low |

---

## 5. Implementation Order

1. **Styles.** Add the classes in §2.6. Delete `.toolbar-status-dot` and `.settings-dialog`.
2. **Settings pane.** Edit `settings_dialog.py` per §2.1. Run:
   `python -m pytest tests/test_settings_dialog.py tests/test_settings_telegram_section.py tests/test_stt_settings_page.py -q`
   Section layout tests must pass with no `_window`.
3. **Left panel.** Fourth page and setters per §2.2. Add `tests/test_left_panel_settings_tab.py`. Run that file.
4. **Project bar.** Cluster, detach, placement, zero-tab host per §2.3. Update `tests/test_main_content_settings_bar.py`. Add `tests/test_project_bar_transport.py`. Run both.
5. **Window.** Wire pane, retarget connect/stop/state, unmount the toolbar, delete `ui/toolbar.py`. Update the telegram, wiring, and gateway-residual tests. Run:
   `python -m pytest tests/test_window_telegram_bridge.py tests/test_window_settings_wiring.py tests/test_no_gateway_residuals.py tests/test_config_invalidation.py -q`
6. **Architecture.** Update `docs/ARCHITECTURE.md` per §2.9.
7. **Grep sweep** (§ Rule 10 below) and the full targeted pytest list in §6.

---

## 6. Acceptance Criteria

- [ ] Left notebook pages, left to right: Prompts, Agents, Projects, Settings.
- [ ] Settings shows all five groups: Providers, Telegram Bridge, Decision Model, STT Model, Speech. One group is visible. Providers is selected on startup.
- [ ] Provider save, test, remove-confirm, Telegram save/test/pair, Decision Model card, STT dropdown, and Speech form behave as they do today (same handler and controller methods).
- [ ] No verified provider → red dot on the Settings tab. A verified provider hides it. `wire_settings_handler` still drives this at startup and on provider changes.
- [ ] Provider saves refresh the live pane (`settings_dialog_factory` returns it).
- [ ] Leaving the Settings tab, and closing the window, both call `cancel_pairing`. Switching among the five groups does not.
- [ ] The top 40px toolbar is gone. `ui/toolbar.py` is gone. Find/replace `ChatInputToolbar` is unchanged.
- [ ] With a project open, the project bar order is: name, Chat, Files, Git branch, connect icon, pip, stop icon, then the activity pill at the right end.
- [ ] Connect icon and pip have no text. Pip is gray when off or unconfigured, amber while connecting, green when connected, red on error.
- [ ] Clicking the icon or the pip runs the existing connect toggle, including the unconfigured no-op and the connecting→stop toggle.
- [ ] Stop icon opens the existing "Stop all agents?" confirm dialog. OK still calls `stop_all_agents()` and `stop_bridge()`.
- [ ] With no chat tab open, the connect icon, pip, and stop icon are visible above the empty notebook. After the first tab opens they sit on the overlay under the tab strip. After the last tab closes they return above the notebook.
- [ ] Pill-only bar (no project) still shows the cluster and the activity pill.
- [ ] `python -m pytest tests/test_settings_dialog.py tests/test_settings_telegram_section.py tests/test_stt_settings_page.py tests/test_main_content_settings_bar.py tests/test_project_bar_transport.py tests/test_left_panel_settings_tab.py tests/test_window_telegram_bridge.py tests/test_window_settings_wiring.py tests/test_no_gateway_residuals.py tests/test_config_invalidation.py tests/test_architecture.py -q` passes.

---

## 7. Edge Cases

| Case | Expected behavior |
|------|-------------------|
| No provider verified at startup | `wire_settings_handler` calls `set_settings_status(False)`. Dot visible on the Settings tab. |
| Settings pane not yet parented, factory called | Window parents the pane before `wire_settings_handler`. Factory returns the pane. |
| `set_settings_pane` called with an already-parented widget | Not the production path. GTK refuses the second parent. Do not call it twice. |
| User starts pairing, switches to Agents | `release()` → `cancel_pairing()`, Start Pairing sensitive again when `has_saved_token()` is true, status line cleared. The token entry is left as the user typed it. `_refresh_telegram_section()` is not called here because it does `set_text` on the token entry from disk. |
| User starts pairing, switches from Telegram to Providers (inner stack) | Poll keeps running. Confirm dialog can still appear (`transient_for` the main window). |
| `cancel_pairing` raises | `release` logs and continues. Window close still proceeds. |
| `parent=None` (unit tests) | Message dialogs use `transient_for=None`. No window to present. |
| Bridge not configured, user clicks connect | Pip stays off. Tooltip is the "No transport" string. `start_bridge` is not called. |
| Bridge `state == "connecting"`, user clicks pip | `stop_bridge()` runs. Later state signal paints disconnected. |
| Bridge handler missing | `_on_connect_clicked` logs and returns. No paint when the store is configured (current test). |
| `set_telegram_bridge_state("offline")` or any unknown string | Treated as disconnected. Pip off. |
| Project name empty | Pill-only: cluster then pill. No Git label. Cluster still works. |
| Bar rebuild while cluster is inside the old `info_box` | `_detach` unparents it, then the new row parents it. Second `append` without detach would throw (widget already has a parent). |
| Last chat tab closed during stop-all bulk close | `_close_tab` re-hosts the bar on `top_box` when `n_pages == 0`, including the bulk path. |
| First `create_chat_tab` while the bar is on `top_box` | Unparent, then `add_overlay`. |
| `stop_all_agents` return dict | Ignored, as today. The summary card is emitted inside the method. |
| Dirty provider card while the tab stays open | Existing `refresh_providers` rule: dirty cards keep edits and update the provider ref. A persistent pane makes this matter more than a dialog that was rebuilt every open. Do not change that rule. |
| Window closed mid-test | `release` with no controller returns after the `on_close` check. |

---

## 8. ARCHITECTURE.md Updates Required

Update in the same change as the code:

- Section 2 directory tree: remove `toolbar.py`.
- Section 3.4: remove the top-bar API (`Toolbar`, `update_connection_state`, `set_settings_status` on the toolbar).
- Section 3.6: settings wiring table and the sentence that the toolbar gear is the sole settings path. Document `set_settings_pane`, the factory, and `set_telegram_bridge_state` on `MainContent`.
- Section 3.7: four-tab notebook and the three new `LeftPanel` methods.
- Section 3.11 (`update_project_settings`): transport cluster after the Git label; pill-only still includes it; zero-tab host is `top_box`.
- Section 11 file inventory: remove `ui/toolbar.py`.

No environment variables. No new handler module. No gateway-protocol change.

---

## Files NOT changed

- `ui/handlers/settings_handler.py` — `add_or_update`, `remove`, `test_provider`, `list_providers` already back the forms.
- `ui/handlers/telegram_settings_controller.py` — `cancel_pairing`, `save_token`, `test_token`, `start_pairing`, `confirm_pairing` stay.
- `ui/handlers/telegram_bridge_handler.py` — state machine and `start_bridge` / `stop_bridge` stay. `"unconfigured"` remains a UI-only state.
- `ui/handlers/agent_runtime_handler.py` — `stop_all_agents` stays. Return dict stays ignored by the window.
- `ui/views/chat_input_toolbar.py` — find/replace bar. Not the project bar and not the top toolbar.
- `utils/telegram_store.py`, `utils/stt_config.py`, `utils/speech_config.py`, `utils/providers_store.py` — persistence unchanged.
- `ui/handlers/input_toolbar_handler.py` — spell check / find. Unrelated.
- Agents placeholder string in `left_panel.py` ("Click Connect to discover agents") — pre-existing, not this move.

---

## Self-audit (Rule 9)

1. Code samples call functions that exist with the signatures read above: `set_visible_child_name`, `cancel_pairing()`, `is_configured(load_bridge_config())`, `is_connected()`, `state`, `start_bridge()`, `stop_bridge()`, `stop_all_agents()`, `unparent()`, `add_overlay`, `set_settings_status(bool)`, `refresh_providers(list)`. New methods (`widget`, `release`, `_select_group`, `set_settings_pane`, `set_telegram_bridge_state` on `MainContent`) are specified here, not assumed to exist.
2. `cancel_pairing` has no `raise` in its body; `release` still catches `Exception` because the old close handler did, and teardown must not block close. `add_or_update` raises `ValueError` only; the existing save handler already catches it and this spec does not replace that handler. `test_provider` does not raise for a bad caller.
3. Stack keys are the `add_titled` names listed in discovery, not guessed. Bridge states are the `BridgeState` constants plus the UI-only `"unconfigured"`.
4. `stop_all_agents`'s dict is ignored on purpose, matching `window.py` today, because the card is emitted inside the method.
5. An implementer who follows the child order, the detach rule, and the factory change gets a pane that stays in sync and a cluster that survives bar rebuilds.
