# SPEC: Split the chat document builders out of ChatSurface

**Date:** 2026-10-17
**Author:** Micah
**Status:** Draft — for implementation
**Implements:** Read-only review of subsystems other than `AgentRuntime` (2026-10-17). No proposal file.
**Depends on:** SPEC-06, SPEC-13, SPEC-14, SPEC-17, SPEC-19, SPEC-27 are already in the tree. This spec does not reopen them.
**Target branch:** current working tree. Anchor edits to identifiers. Line numbers below were read on 2026-10-17 and will drift.

> **Architecture compliance:** `docs/ARCHITECTURE.md` §3.6 and §8.6 say `ui/window.py` is the composition root: it creates handlers and wires callbacks, and a handler does not import another handler. This spec does not move window methods. `ChatSurface` stays the GTK widget. The pure HTML and JavaScript builders move to a sibling module that does not import GTK. `render/sanitize.py` and `TextViewFallback` stay where they are.
>
> §9.1 says application CSS lives in `ui/styles.py`. `_BASE_CSS` is the WebKit document stylesheet, not a `Gtk.CssProvider`. It already lives next to `_document`. This spec moves that string with the document builder. It does not fold it into `ui/styles.py`.

---

## DISCOVERY

```
DISCOVERY:
- Read ui/views/chat_surface.py (2,121 lines).
  Lines 1–37 say JavaScript is off, there is no bridge, appends rebuild
  the document with load_html, and WebKit 4.1 is the fallback.
  Lines 79–86: the 4.1 branch was removed. WebKit is 6.0 or None.
  get_env("NO_WEBKIT") also forces WebKit = None.
  Lines 88–114: _LIVE_JS_FLAG = "LIVE_JS". _live_js_enabled() is True
  unless get_env returns the exact string "0". An exception returns True.
  Lines 136–752: module-level string builders and the HTML document.
  They do not use Gtk. The class starts at line 769.
  ChatSurface docstring (772–774) still says "JS is OFF".
  __init__ (793) sets self._live_js = _live_js_enabled().
  is_live_enabled() returns bool(self._live_js).
  create_chat_surface(window_max=500, live_bridge_approver=None) at 2108.
  If WebKit is None, the name ChatSurface is rebound to TextViewFallback
  (2104–2105) and the factory returns TextViewFallback.
- Read ui/handlers/chat_render_handler.py _compose_text (118) and
  _append_composed (134). A ```live fence becomes
  (payload, sanitize_agent_html(payload)). _append_composed calls
  surface.append_live only when surface.is_live_enabled() is true.
  Otherwise it calls append_message with the sanitized fragment.
  The handler imports create_chat_surface from ui.views.chat_surface.
- Read tests. They import these names from ui.views.chat_surface:
  _document, _cap_row_html, _BOTTOM_SCRIPT, _BASE_CSS, _sanitize_color,
  _inject_script, _bottom_and_settle_script, _live_section_island_html,
  _live_flattened_section_html, _live_js_enabled, _LIVE_ID_SHIM_JS,
  ChatSurface, TextViewFallback.
  tests/test_live_guard.py and tests/test_live_bridge.py do
  monkeypatch.setattr(ui.views.chat_surface, "_live_js_enabled", ...).
  ChatSurface.__init__ calls _live_js_enabled() as a global of that module.
- Read ui/window.py methods from _on_connect_clicked (1112) through
  _show_auto_accept_warning_v2 (1816). They call ProjectHandler,
  FeedHandler, MainContent, AgentRuntimeHandler, and the Telegram bridge,
  and they own _branch_request_token, _branch_active_token,
  _branch_request_path, and _cached_branch_by_path. The warning dialogs
  use transient_for=self. ARCHITECTURE §3.6 documents this worker as
  window-owned.
- Read docs/ARCHITECTURE.md §2 (chat_surface.py line), §3.6, §8.6, §9.1.
- Architecture owner: ChatSurface owns the widget, the row deque, scroll
  restore, and the live-section registry. The new module owns the strings
  those methods already call. The window keeps cross-handler coordination.
- Existing pattern: render/html.py is pure and does not import GTK.
  This split copies that boundary. It does not copy a mixin.
```

---

## 1. Overview

### Problem statement

`ui/views/chat_surface.py` describes two designs. The module header and the `ChatSurface` docstring say JavaScript is off, appends call `load_html`, and WebKit 4.1 is a fallback. The code under that header defaults JavaScript on, injects rows with `evaluate_javascript`, and has no 4.1 import.

The JavaScript and HTML builders are already functions, not methods. They share the file with the GTK widget, so the header at the top of the widget file is the text a reader trusts.

### Solution summary

Move the pure builders and their constants into `ui/views/chat_document.py`. Re-export every name tests and `ChatSurface` already use, from `ui/views/chat_surface.py`, so call sites and monkeypatches do not change. Replace the module header and the `ChatSurface` docstring with the behavior the flag actually has.

Leave `ui/window.py` alone. The methods that look like feature logic are the composition root coordinating several handlers, which §8.6 says stays in the window.

### Scope

| In scope | Out of scope |
|---|---|
| New `ui/views/chat_document.py` with the pure builders, moved verbatim | Rewriting `ChatSurface`, scroll restore, `LiveGuard`, or `LiveBridge` |
| Re-exports from `ui/views/chat_surface.py` | Retargeting tests onto the new module |
| Module header and `ChatSurface` docstring | Editing the bytes of `_BASE_CSS`, the JavaScript templates, or the stub prefixes |
| `docs/ARCHITECTURE.md` §2 entry for the new file | Moving `_BASE_CSS` into `ui/styles.py` |
| | `ui/window.py`, including branch refresh, auto-accept dialogs, bridge cards, and project-tab close |
| | `ui/handlers/chat_render_handler.py`, `render/sanitize.py`, `render/html.py` |
| | Stripping probe comments out of the moved functions |

### Architecture principles

- The view stays a widget. The document module imports no GTK and no `ui.handlers`.
- Handlers still do not import each other. Nothing in this spec adds such an import.
- §0: the directory tree gains the new file in the same change.

---

## 2. Changes by File

### 2.1 `ui/views/chat_document.py` (new)

No GTK import. No `gi.require_version`. Imports are `html`, `json`, `logging`, `re`, `html.parser.HTMLParser`, and `utils.config.get_env`.

Logger: `logging.getLogger(__name__)`. The name becomes `ui.views.chat_document`. No test asserts the old logger name `ui.views.chat_surface` for the flatten failure. The debug line `"live flatten parse failed"` stays.

Move these definitions verbatim, in this order, including the comments that sit on them. Do not rewrite the JavaScript. Do not change `_BASE_CSS`, including the comment inside it that says "surface JS stays off". That comment is part of the stylesheet string tests match against.

| Name | Signature / kind | What it raises or swallows |
|---|---|---|
| `_LIVE_JS_FLAG` | `"LIVE_JS"` | — |
| `_live_js_enabled` | `() -> bool` | Any exception from `get_env` returns `True`. Only the exact string `"0"` returns `False`. |
| `_FOLLOW_SETTLE_FRAMES` | `30` | — |
| `_FOLLOW_TAIL_CAP` | `420` | — |
| `_settle_tail_js` | `() -> str` | Does not raise. Interpolates the two caps above. |
| `_bottom_and_settle_script` | `(threshold: float) -> str` | `threshold` is formatted with `:g`. |
| `_inject_script` | `(row_html: str, threshold: float) -> str` | Embeds `row_html` only through `json.dumps`. Does not raise on markup. |
| `_LIVE_SECTION_CAP` | `10` | — |
| `_DOM_COMPACT_SLACK` | `100` | — |
| `_LIVE_SHIM_JS` | `str` | — |
| `_LIVE_ID_SHIM_JS` | `str` | — |
| `_LIVE_BRIDGE_INIT_JS` | `str` | — |
| `_LIVE_BRIDGE_RESOLVE_JS` | `str` | Placeholder is the literal `__DC_RESOLVE__`. |
| `_LIVE_RESURRECT_JS` | `str` | Placeholders `__SHIMS__` and `__IDS__`. |
| `_LIVE_FLATTEN_JS` | `str` | Placeholder `__IDS__`. |
| `_live_ids_js` | `(ids) -> str` | `int(i)` for each id. A non-int raises `ValueError` or `TypeError`. Do not catch it. |
| `_live_resurrect_js` | `(ids) -> str` | Same, via `_live_ids_js`. Shims concatenated are `_LIVE_SHIM_JS + _LIVE_ID_SHIM_JS + _LIVE_BRIDGE_INIT_JS`. |
| `_live_flatten_js` | `(ids) -> str` | Same. |
| `_live_section_island_html` | `(live_id: int, payload_html: str) -> str` | `int(live_id)` propagates `ValueError` / `TypeError`. `<` in the JSON becomes `\\u003c`. |
| `_LiveHtmlFlattener` | class, `HTMLParser` | `convert_charrefs=True`. Drops `script` elements and `on*` attributes. |
| `_flatten_live_html` | `(payload_html: str) -> str` | Any exception from `feed` / `close` returns `""`. |
| `_live_flattened_section_html` | `(live_id: int, payload_html: str) -> str` | `int(live_id)` propagates. Flatten failure becomes an empty section body. |
| `_MAX_ROW_BYTES` | `512 * 1024` | — |
| `_TRUNCATION_MARKER` | `" [truncated]"` | — |
| `_BASE_CSS` | `str` | Bytes unchanged. |
| `_TAG_STRIP_RE` | `re.compile(r"<[^>]*>")` | — |
| `_DOC_WORLD` | `"develcakes-scroll"` | — |
| `_BOTTOM_SCRIPT` | `str` | — |
| `_COLOR_RE` | `re.compile(r"#[0-9a-fA-F]{6}")` | Used with `fullmatch`, not `match`. |
| `_sanitize_color` | `(value) -> str` | Falsy returns `""`. Whole-string six hex digits return `value.lower()`. Anything else returns `""`. Does not raise. |
| `_blocks_html` | `(rows: list[dict]) -> str` | A row with `live_id` not `None` appends `row["html"]` verbatim. A missing `"html"` key raises `KeyError`. Do not add a default. |
| `_document` | `(rows: list[dict]) -> str` | Wraps `_blocks_html` in a document whose `<style>` is `_BASE_CSS` and whose host id is `transcript`. |
| `_cap_row_html` | `(fragment: str) -> str` | UTF-8 byte cap. Marker bytes are inside the budget. Does not raise. |

`_blocks_html` grouping key is `(agent, role)`, not the display name. A row with no `agent` is a `message-row`. User rows get no inline color. Do not change that.

`_inject_script` returns a script whose last statements are `return 'held'` or `return 'followed'`. The threshold appears as `var T = {threshold:g};`, so `80.0` becomes `var T = 80;`. Tests assert that spelling.

### 2.2 `ui/views/chat_surface.py`

Delete the moved definitions. Import them and bind the same names:

```python
from ui.views.chat_document import (
    _BASE_CSS,
    _BOTTOM_SCRIPT,
    _DOC_WORLD,
    _DOM_COMPACT_SLACK,
    _LIVE_BRIDGE_RESOLVE_JS,
    _LIVE_ID_SHIM_JS,
    _LIVE_SECTION_CAP,
    _TAG_STRIP_RE,
    _blocks_html,
    _bottom_and_settle_script,
    _cap_row_html,
    _document,
    _inject_script,
    _live_flatten_js,
    _live_flattened_section_html,
    _live_js_enabled,
    _live_resurrect_js,
    _live_section_island_html,
    _sanitize_color,
)
```

`ChatSurface` and `TextViewFallback` keep calling those names as globals of this module. Do not change a call to `chat_document._live_js_enabled()` or any other qualified lookup. `monkeypatch.setattr(ui.views.chat_surface, "_live_js_enabled", ...)` replaces the global the methods look up. A qualified call would ignore the patch.

Keep in this file, unmoved:

- the `gi` / `Gtk` / `WebKit` import block, including `get_env("NO_WEBKIT")` and the absence of a WebKit 4.1 branch
- `from render.html import live_fence`
- `from utils.live_bridge import READ_ONLY_METHODS, LiveBridge`
- `from utils.live_guard import LiveGuard`
- `from utils.live_reader import read_local_image`
- `_make_owned_scroll`
- `class ChatSurface`, `class TextViewFallback`
- the `if WebKit is None: ChatSurface = TextViewFallback` rebind
- `create_chat_surface`
- `ChatSurface._BOTTOM_THRESHOLD = 80.0` and `TextViewFallback._BOTTOM_THRESHOLD = 80.0`
- `ChatSurface._LIVE_BRIDGE_POLL_MS = 500`

`_BOTTOM_THRESHOLD` stays a class attribute. `_bottom_and_settle_script` and `_inject_script` receive it as the `threshold` argument. Do not move the `80.0` into the document module as a second constant.

Replace the module docstring (lines 1–37) with the current machine. Delete the sentences that say JavaScript is off, that there is no `evaluate_javascript`, that scroll restore is future work, and that WebKit 4.1 is a fallback. The replacement states:

- `ChatSurface` is the WebKit 6.0 transcript widget. If `get_env("NO_WEBKIT")` is set, or the WebKit 6.0 import fails, `WebKit` is `None` and `create_chat_surface` returns `TextViewFallback`. There is no 4.1 import.
- `_live_js_enabled()` is read once, in `ChatSurface.__init__`, into `self._live_js`. Only `get_env("LIVE_JS") == "0"` turns it off. Unset, `""`, `"1"`, and `"true"` are on. A `get_env` exception is on.
- When `self._live_js` is true, appends inject into `#transcript` with `evaluate_javascript`, live sections use the JSON island plus `_live_resurrect_js`, and `LiveGuard` / `LiveBridge` attach. `is_live_enabled()` is what `chat_render_handler._append_composed` branches on.
- When it is false, appends rebuild through `_document` and `load_html`. A ```live fence still arrives as sanitized HTML via `append_message`, because `_append_composed` does not call `append_live`.
- Scroll: the JavaScript-on inject path measures at-bottom inside the same script that appends the row (`_inject_script`). The load path keeps the Python `Gtk.Adjustment` restore. Tests of that restore drive a real `Gtk.Adjustment`. Do not replace it with a fake.
- `_BASE_CSS` class names (`tok-*`, `lang-*`, `terminal`, `task-*`, `message-row`, and the agent-card classes) are the sanitizer allowlist. They live in `chat_document.py` and are re-exported here.

Replace the `ChatSurface` class docstring. It currently says JavaScript is off. The replacement: one WebKit transcript per tab; `self._live_js` is fixed at init from `_live_js_enabled()`; the row window is `deque(maxlen=window_max)` default 500.

Do not edit method bodies except for the import that makes the names resolve. Do not delete the probe comments that move with the builders.

### 2.3 `docs/ARCHITECTURE.md`

In §2, under `ui/views/`, add one line next to `chat_surface.py`:

`chat_document.py` — pure HTML document and live-section JavaScript builders used by `ChatSurface`. No GTK. Re-exported from `chat_surface.py`.

Do not change the `window.py` paragraphs in §3.6 or §8.6. Do not refresh unrelated stale line counts in §2.

### 2.4 Files NOT changed

| File | Why |
|---|---|
| `ui/window.py` | `_on_feed_bar_update`, `_schedule_branch_refresh`, `_resolve_branch_worker`, `_on_branch_result`, `_on_project_opened`, `_on_project_closed`, `_close_project_tab`, `_show_auto_accept_warning`, `_show_auto_accept_warning_v2`, `_on_agent_response`, `_remote_stop_all`, and `_remote_status_summary` coordinate `ProjectHandler`, `FeedHandler`, `MainContent`, `AgentRuntimeHandler`, and the bridge. Branch state (`_branch_request_token`, `_branch_active_token`, `_branch_request_path`, `_cached_branch_by_path`) lives on the window. The dialogs take `transient_for=self`. §8.6 says that wiring stays in the window. Moving any of them into one handler would make that handler call the others. |
| `ui/handlers/chat_render_handler.py` | Still imports `create_chat_surface` from `ui.views.chat_surface`. `_append_composed` is already the live-versus-static branch. |
| `render/html.py`, `render/sanitize.py`, `render/syntax_html.py` | The sanitizer stays on the render path. This split does not change markup policy. |
| `utils/live_bridge.py`, `utils/live_guard.py`, `utils/live_reader.py` | The widget still constructs them. |
| `ui/styles.py` | `_BASE_CSS` is not GTK CSS. |
| `tests/test_chat_surface.py`, `tests/test_live_guard.py`, `tests/test_live_bridge.py`, `tests/test_chat_render_handler.py` | They import and patch names on `ui.views.chat_surface`. The re-exports keep those patches pointed at the widget module. |

---

## 3. Data Flow

Unchanged. The builders are called from the same methods.

1. `ChatRenderHandler` composes text in `_compose_text`. A live fence returns `(payload, sanitize_agent_html(payload))`. Anything else returns `(None, render_message(text))`.
2. `_append_composed` calls `surface.is_live_enabled()`. True and a payload: `append_live`. Otherwise: `append_message` with the sanitized fragment.
3. `append_message` stores `_cap_row_html(html_fragment)` and `_sanitize_color(agent_color)` on the row, then `_schedule_render`.
4. `append_live` stores `_live_section_island_html(live_id, payload_html)` on the row, registers `_live_sections[live_id]`, and flattens the oldest id while `len(_live_sections) > _LIVE_SECTION_CAP` (10).
5. With `self._live_js`, a render that does not need compaction calls `_inject_script(_blocks_html(new_rows), self._BOTTOM_THRESHOLD)`. Compaction and the JavaScript-off path call `_document(list(self._rows))` and `load_html`.
6. `evaluate_javascript` for the scroll world uses `_DOC_WORLD` (`"develcakes-scroll"`). Bridge resolution replaces `__DC_RESOLVE__` in `_LIVE_BRIDGE_RESOLVE_JS`.

`_live_js` is not read again after `__init__`. Patching `_live_js_enabled` after the surface exists does not change that instance. Tests that patch it do so before `ChatSurface()`.

---

## 4. File Change Summary

| File | Change | Risk |
|---|---|---|
| `ui/views/chat_document.py` | New. Verbatim move of the pure builders. | Medium. A byte change in a template fails the surface tests. |
| `ui/views/chat_surface.py` | Import re-exports. New header and class docstring. Widget stays. | Medium. A qualified call breaks the `_live_js_enabled` monkeypatch. |
| `docs/ARCHITECTURE.md` | One §2 line. | Low. |

---

## 5. Implementation Order

1. Add `ui/views/chat_document.py` and move the names in §2.1 verbatim. Leave `chat_surface.py` calling them via the re-export import. Do not edit function bodies in the same commit as the header rewrite if the move is not green yet.
2. Run `tests/test_chat_surface.py`, `tests/test_live_guard.py`, `tests/test_live_bridge.py`, `tests/test_live_bridge_readonly.py`, `tests/test_chat_render_handler.py`, and `tests/test_chat_render_scroll.py`.
3. Replace the module docstring and the `ChatSurface` docstring. Re-run `tests/test_chat_surface.py`. Docstring text is not what those tests assert; this step is so the header change is reviewable on its own.
4. Add the §2 line to `docs/ARCHITECTURE.md`.

---

## 6. Acceptance Criteria

- [ ] `ui/views/chat_document.py` does not import `gi`, `Gtk`, or `WebKit`.
- [ ] `from ui.views.chat_surface import _document, _inject_script, _live_js_enabled, _BASE_CSS, _BOTTOM_SCRIPT, _LIVE_ID_SHIM_JS, _sanitize_color, _cap_row_html, _live_section_island_html, _bottom_and_settle_script` still works.
- [ ] `ChatSurface.__init__` still calls `_live_js_enabled()` as a name in `ui.views.chat_surface`, not a qualified `chat_document` lookup.
- [ ] `_BASE_CSS`, `_inject_script`, `_BOTTOM_SCRIPT`, `_LIVE_ID_SHIM_JS`, and `_LIVE_RESURRECT_JS` are byte-identical to the pre-move definitions.
- [ ] `_COLOR_RE.fullmatch` is still the color gate. `"#16a34a\n"` still returns `""`.
- [ ] `_cap_row_html` still slices UTF-8 bytes, and the marker's bytes sit inside `512 * 1024`.
- [ ] `_flatten_live_html` still returns `""` when the parser raises.
- [ ] `_live_ids_js` still lets `int()` raise.
- [ ] `create_chat_surface` still returns `TextViewFallback` when `WebKit` is `None`, and still passes `live_bridge_approver`.
- [ ] The module docstring does not say JavaScript is off, and it does not mention a WebKit 4.1 fallback.
- [ ] The `ChatSurface` class docstring does not say JavaScript is off.
- [ ] `ui/window.py` has no diff.
- [ ] `docs/ARCHITECTURE.md` §2 names `chat_document.py`. §3.6 and §8.6 are untouched.

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| `DEVELCAKES_LIVE_JS` unset | `_live_js_enabled()` is `True`. |
| `DEVELCAKES_LIVE_JS=0` | `False`. Any other string, including `""` and `"false"`, is `True`. |
| `get_env("LIVE_JS")` raises | `True`. |
| Patch `_live_js_enabled` after `ChatSurface()` | That instance keeps the value captured in `self._live_js`. |
| `NO_WEBKIT` set, or WebKit 6.0 import fails | `create_chat_surface` returns `TextViewFallback`. The document module is still imported; the fallback calls `_cap_row_html`, `_sanitize_color`, and `_TAG_STRIP_RE`. |
| Row with `live_id` set and no `"html"` | `_blocks_html` raises `KeyError`. |
| `live_id` that is not an int | `_live_section_island_html` and `_live_ids_js` raise from `int()`. |
| Flatten parser raises | Section body is `""`. The section element is still emitted. |
| More than 10 live sections | `append_live` flattens the oldest until the registry length is 10. |
| Reader not at bottom during inject | Script returns `'held'` and does not set `scrollTop`. |
| Reader at bottom during inject | Script sets `scrollTop` to `scrollHeight` and arms the settle tail. The tail stops when `atBottom()` becomes false, after 30 stable frames, or after 420 frames. |
| Color `"#16a34a\n"` or `"red"` | `_sanitize_color` returns `""`. No inline style. |

---

## 8. ARCHITECTURE.md Updates Required

Only §2, the `ui/views/` list: add `chat_document.py` as specified in §2.3.

Do not edit §3.6, §8.6, or §9.1. §9.1 remains the rule for GTK CSS. `_BASE_CSS` is the document stylesheet and stays with `_document`.

---

## 9. Self-audit

Checked against the source read on 2026-10-17:

1. The re-export list is the set of names `ChatSurface`, `TextViewFallback`, and the tests actually reference. `_make_owned_scroll` and the WebKit import stay because they use GTK.
2. Exceptions: `_live_js_enabled` swallows `Exception` and returns `True`. `_flatten_live_html` swallows `Exception` and returns `""`. `_live_ids_js` / `int(live_id)` propagate `ValueError` and `TypeError`. `_blocks_html` propagates `KeyError` on a missing `"html"`. `_sanitize_color` and `_cap_row_html` do not raise. No sample adds a new `except`.
3. Keys: live rows are distinguished by `row.get("live_id") is not None`. Grouping is `(agent, role)`. The transcript host id is the string `transcript`. The isolated world is `develcakes-scroll`. The bridge placeholder is `__DC_RESOLVE__`.
4. Return values: `_inject_script` is a script that returns `'held'` or `'followed'`; the eval callback ignores that value. `append_live` returns the int id, or `None` if the surface is destroyed. `is_live_enabled` returns `bool(self._live_js)`. `create_chat_surface` returns a surface instance. Those returns are not changed.
5. The window extraction from the review does not survive this read. Those methods are the composition root §8.6 already assigns to `window.py`. The spec leaves them there instead of inventing a handler that would import its siblings.

Not verified by executing pytest. This document is the spec, not the implementation.
