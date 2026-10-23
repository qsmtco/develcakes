# SPEC: Web surface paint — canvas color, feed patches, no white clear

**Date:** 2026-10-23
**Author:** Micah
**Status:** Draft — for implementation
**Implements:** read-only investigation 2026-10-23 (resize slabs and feed white flash)
**Depends on:** SPEC-FEED-HTML-SURFACE (the feed document), SPEC-19 (chat incremental inject), SPEC-17 (chat document scroll)
**Target branch:** main

> Architecture: `FeedCardData` stays the record. `FeedHandler` stays the lifecycle. `FeedTab` stays the GTK host and the toolbar. `FeedSurface` stays the only feed WebView. `ChatSurface` stays the only chat WebView. This spec changes how those two views are cleared and how an already-loaded feed document is updated. It does not add a WebView, a script-message handler, or a second scroll policy.

---

## DISCOVERY

- Read `ui/views/feed_surface.py`: `FeedSurface.__init__(self, on_action, *, is_near_bottom, on_load_finished)` builds `WebKit.WebView()`, `set_vexpand(True)`, `set_hexpand(True)`, `get_settings().set_enable_javascript(True)`, registers script-message handler `"feed"`. There is no `set_background_color` call. `set_cards` clears `_order`, `_cards`, `_html`, `_body`, `_revealed`, `_context_open`, sets `_doc_near_bottom = True` and `_doc_y = None`, then `_schedule()`. `upsert(card, *, prepend: bool = False)` inserts at 0 or appends, then `_schedule()`. `remove`, `reveal`, `toggle_context` also `_schedule()`. `_schedule` sets `_dirty = True` and returns without a new idle when `_render_source is not None` or `_load_in_flight` is true. `_flush() -> bool` clears `_dirty`, sets `_scroll_after = bool(self._doc_near_bottom)`, calls `render_feed_document(self._fragments())`, sets `_load_in_flight = True`, and `load_html(document, "about:blank")`. `load_html` exceptions are logged and clear `_load_in_flight`. Return value of `_flush` is `False` (idle source removed). `_eval_document_scroll` calls `evaluate_javascript(script, len(script.encode("utf-8")), None, None, None, _done, None)` and ignores the async result. `_done(*_args)` returns `None`. `_on_load_changed` acts only on `WebKit.LoadEvent.FINISHED`: clears `_load_in_flight`, consumes `_scroll_after`, runs `_BOTTOM_SCRIPT` or `_restore_script(self._doc_y)`, then `_schedule()` if `_dirty`. `_on_load_failed` returns `False`. There is no `_destroyed` flag and no `destroy` method.
- Read `render/feed_html.py`: `FEED_CSS` sets `body { margin: 0; background: #1a1b26; ...}`. No `html` rule. No `min-height`. `.feed-card` includes `animation: feed-card-in .25s ease-out`. `@keyframes feed-card-in` is opacity 0 → 1 only. `render_feed_card(...) -> str` returns one `<article class="..." data-card-id="...">` root. `render_feed_document(fragments) -> str` wraps fragments in `<div id="feed">` plus `<script>{FEED_SCRIPT}</script>`. Empty fragments render `_EMPTY` (`div.feed-empty`). `FEED_SCRIPT` listens on `document` for clicks (`closest`, `getAttribute`) and posts `scroll_state` with `near_bottom` computed as `(doc.scrollHeight - viewport - scrollTop()) <= 80`. The script is static. A comment in it says upsert reloads replace the whole document.
- Read `ui/views/chat_surface.py`: `_BASE_CSS` is `body { background: #1a1b26; ... margin: 8px; }` and no `html` rule. `_document` wraps rows in `<div id="transcript">`. `_make_owned_scroll()` builds a `Gtk.ScrolledWindow` with policy `(NEVER, AUTOMATIC)`, vexpand and hexpand, and no CSS class. `_ensure_webview` does `WebKit.WebView()` then settings, then signal connects, then `self._scroll.set_child(self._webview)`. No `set_background_color`. `_load_html` is `self._ensure_webview().load_html(doc, "about:blank")`. `_issue_load` sets `_load_in_flight` before `_load_html` and clears it if `_load_html` raises. Live JS (default on, off only when env `LIVE_JS` is the string `"0"`) injects rows via `_inject_script`, which `json.dumps`s the HTML into a `template.innerHTML` assignment and appends to `#transcript`. `_document_eval_main` calls `evaluate_javascript` with world `None` (main world). `_document_eval` passes world `_DOC_WORLD` (`"develcakes-scroll"`) because content JS can be off. The feed must not copy that world name. `load_unsanitized_full_document` routes through `_issue_load`, which calls `_load_html`, which calls `_ensure_webview`. Imports today: `from gi.repository import GLib, Gtk` after `gi.require_version("Gtk", "4.0")`. No TOP-LEVEL `Gdk` import — but SPEC-56 (2026-10-23) added a function-local `from gi.repository import Gdk` inside `_copy_text_to_clipboard` (~line 771). When §2a adds the top-level Gdk import, remove that now-redundant local import (one-line cleanup; the function body is unchanged).
- Read `ui/views/main_content.py` `create_chat_tab`: the page is `Gtk.Overlay` → `chat_scroll` (`PolicyType.AUTOMATIC`, `AUTOMATIC`) → vexpand `chat_box` → `ChatSurface` (which owns a second scrolled window). The overlay holds the project-settings bar and the scroll-to-bottom button. `_WELCOME_FALLBACK_HTML` already sets `html,body` background `#121417`. `create_welcome_tab` loads `assets/splash.html` through `load_unsanitized_full_document` on that same chat WebView.
- Read `assets/splash.html`: `html,body{height:100%}` and `body{background:var(--navy-900)}` with `--navy-900:#06060d`. The splash paints its own canvas once the document is up. It is not the transcript color.
- Read `ui/styles.py` line 1310: `.feed-scroll { background: transparent; }`. No other rule targets `.feed-scroll`. No test asserts that string.
- Read `ui/views/feed_tab.py`: `attach_html_surface` sets the webview as the scrolled window's child and `set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)`. `schedule_smart_scroll_to_bottom` returns immediately when `self._html_surface is not None`. `_on_scroll_mapped` does not. When `_was_near_bottom` it calls `schedule_scroll_to_bottom()` and returns `False`. That arms a GTK adjustment settle. Under vertical `NEVER` the adjustment does not move the document.
- Read `tests/test_feed_surface_scroll.py`: surfaces are `FeedSurface.__new__` plus hand-set fields. `_flush` tests expect `load_html` and then `_BOTTOM_SCRIPT` / `_restore_script` from `_on_load_changed`. `TestAttachHtmlSurfaceIntegration.test_attach_flips_vertical_policy_to_never` asserts policy `(EXTERNAL, NEVER)`. `test_schedule_smart_scroll_is_inert_with_a_surface` asserts the GTK settle ids stay `None`.
- Read `tests/test_feed_html.py` `test_surface_toggle_reveal_and_body_reset_without_a_page_load`: another `__new__` surface. It sets `_load_in_flight = True` and calls `toggle_context` / `reveal` / `upsert`, which call `_schedule`. `test_keyframes_are_opacity_only` and `test_full_document_contains_svg_and_keyframes` require the name `@keyframes feed-card-in` to remain in `FEED_CSS`. They do not require the animation to sit on `.feed-card`.
- Read `tests/test_feed_handler.py` around `test_map_follows_when_caught_up` and `test_map_restores_reading_offset`: they drive a real `FeedTab` whose `_html_surface` is still `None` (the fixture never calls `attach_html_surface`). Those tests must keep passing.
- Read `tests/test_chat_surface.py`: several tests assert substrings of `_BASE_CSS` (`.agent-chrome`, button colors, block display). None assert `margin: 8px` or the `body {` background line. Grep of `tests/` for `margin: 8px`, `#1a1b26`, and `feed-scroll` found no pins.
- Probed on this machine (no widget constructed): GTK 4.14.5, WebKitGTK 2.52.6 (`WebKit.MAJOR_VERSION/MINOR/MICRO`). `WebKit.WebView.set_background_color` exists. `Gdk.RGBA().parse("#1a1b26")` returns `True` and sets alpha `1.0` (channels about 0.102, 0.106, 0.149). `inspect.signature` on the GI methods is `(*args, **kwargs)`; the call shape is taken from the existing `evaluate_javascript` call in `_eval_document_scroll`, not from `inspect`.
- WebKit 2.52 docs for `webkit_web_view_set_background_color`: the color is drawn before contents render; a page background takes precedence once painted; the default is opaque white. The older WebKit2 docs mention `app-paintable` for a transparent window. The 2.52 page does not. This spec does not set `app-paintable`.
- Architecture owner: `FeedSurface` owns the feed document and the feed WebView. `ChatSurface` owns the chat WebView. `ui/styles.py` owns GTK CSS (`apply_styles()`). `render/feed_html.py` owns the feed document CSS and must stay GTK-free.

---

## 1. Overview

**Problem.** Both WebViews leave WebKit's default clear color, which is opaque white. The feed host scrolled window is transparent, so a late frame shows the dark GTK notebook behind it. The feed also calls `load_html` for every card change, which clears the view to that white, then `window.scrollTo` jumps the new document. `.feed-card` runs `feed-card-in` from opacity 0 on every article, and a full reload is a new document, so every card fades in, not just the new one.

Window resize reallocates the GTK widget immediately. The web process paints the new size a frame or more later (WebKitGTK accelerated compositing; bug 189743 describes the same gap). That lag does not go away in this spec. What goes away is the wrong color in the gap, and the white clear on every feed update.

**Solution.**

1. Before any `load_html`, set each WebView's background to opaque `#1a1b26`. Paint `html` and `body` the same color, with `html { height: 100% }` and `body { min-height: 100% }`, so the canvas is covered when the document is shorter than the viewport.
2. Paint the GTK hosts that sit behind those views the same color. `.feed-scroll` is transparent today. The chat scrolled windows have no background class.
3. After the feed's first successful `load_html`, apply later card inserts, deletes, and replacements in the existing document with `evaluate_javascript`. Fall back to `load_html` when the id order is not a pure insert/delete/replace, when the document is the empty state, or when the patch reports failure.
4. Run the entrance animation only on cards the patch inserts. A full document load (project switch, empty state, fallback) does not fade every card.
5. When an HTML surface is attached, `_on_scroll_mapped` does not arm the GTK settle. `schedule_smart_scroll_to_bottom` already returns in that case. The map handler does not.

**Scope**

| In | Out |
|---|---|
| Canvas color on both WebViews, set once before the first load | `WEBKIT_DISABLE_COMPOSITING_MODE`, `WEBKIT_DISABLE_DMABUF_RENDERER`, or any hardware-acceleration policy |
| `html`/`body` canvas rules in `FEED_CSS` and `_BASE_CSS` | Restyling cards, toolbar, or splash |
| Opaque `#1a1b26` on `.feed-scroll` and on both chat scrolled windows | Removing either chat `ScrolledWindow`, or changing feed policy `(EXTERNAL, NEVER)` |
| In-place feed DOM patches after the first `FINISHED` | Incremental patches for the chat transcript (already implemented when live JS is on) |
| Entrance animation only on patched inserts | Deleting the GTK feed-card CSS, `schedule_scroll_to_bottom`, or `_build_load_more_widget` |
| Map handler no-ops when `_html_surface` is set | Making resize pixel-synchronous. The gap remains; it is the page color |

**Principles.** Card bodies stay escaped text from `render_feed_card`. The patch script is a static function in `feed_surface.py`. Card HTML enters it only as `json.dumps` data (`ensure_ascii=True`). The script does not read card bodies and does not call `postMessage`. Decisions (which op, when to reload) stay in Python. A failed patch reloads the document. It does not leave Python believing the DOM matches `_order`.

---

## 2. Changes by File

### 2a. Shared color — copied, not imported

Do not add a module. `render/` must not import GTK. `feed_surface` must not import `chat_surface`.

Put this function in **both** `ui/views/feed_surface.py` and `ui/views/chat_surface.py`. The body is the same. The name is `_apply_canvas_color`.

```python
_CANVAS = "#1a1b26"


def _apply_canvas_color(webview) -> None:
    """Draw #1a1b26 before the page paints.

    WebKit's default is opaque white. The call has to happen before the
    first load_html. set_background_color returns None. A later call does
    not refresh the accelerated-compositing clear color, so do not call
    this again after a load.
    """
    rgba = Gdk.RGBA()
    if not rgba.parse(_CANVAS):
        logger.error("canvas color %s did not parse", _CANVAS)
        return
    try:
        webview.set_background_color(rgba)
    except Exception:
        logger.exception("set_background_color failed")
```

`Gdk.RGBA.parse` returns `bool` and mutates the object. It does not raise for a bad string. Verified: `parse("#1a1b26")` is `True` and `alpha` is `1.0`. If it returns `False`, do not call `set_background_color` (a failed parse leaves the RGBA at zeros, which is transparent). `set_background_color` is a void C method; ignore the return. The `except Exception` matches the existing `load_html` / `evaluate_javascript` handlers in these two files. Do not narrow it. Do not set `Gtk.Widget` `app-paintable`.

**feed_surface imports.** Replace

```python
gi.require_version("Gtk", "4.0")
from gi.repository import GLib
```

with

```python
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib
```

Call `_apply_canvas_color(self._webview)` in `FeedSurface.__init__` on the next line after `self._webview = WebKit.WebView()`, before `set_vexpand` and before any `_schedule` / `load_html`. `attach_html_surface` calls `set_cards([])` after init, so the color is already set.

**chat_surface imports.** The file already has `gi.require_version("Gtk", "4.0")`. Add `gi.require_version("Gdk", "4.0")` immediately after it. Change the import to `from gi.repository import Gdk, GLib, Gtk`.

Call `_apply_canvas_color(self._webview)` in `_ensure_webview` on the next line after `self._webview = WebKit.WebView()`, before `get_settings()`. `_load_html` and `load_unsanitized_full_document` both reach `_ensure_webview` before `load_html`, so the welcome splash and the transcript share this one call. The splash's own CSS (`#06060d`) covers the view after it paints. The pre-paint color is the transcript color, not white.

### 2b. `render/feed_html.py` — canvas and entrance class

In `FEED_CSS`, insert this rule above the `body` rule:

```css
html { background: #1a1b26; height: 100%; }
```

Change the `body` rule so it keeps every property it has and adds `min-height: 100%`. It already has `margin: 0` and `background: #1a1b26`. Do not add a margin.

On `.feed-card`, delete the declaration `animation: feed-card-in .25s ease-out;`. Leave `transition`, hover, and `@keyframes feed-card-in` in place. Add:

```css
.feed-card-enter { animation: feed-card-in .25s ease-out; }
```

`render_feed_card` does not add `feed-card-enter`. The patch script adds it, and only on insert. A full `load_html` therefore does not fade the list.

No Python signature changes. `render_feed_document` stays as it is.

### 2c. `ui/views/chat_surface.py` — transcript canvas

Replace the `body` rule at the start of `_BASE_CSS` with:

```css
html { background: #1a1b26; height: 100%; }
body { background: #1a1b26; color: #a9b1d6; font-family: sans-serif;
       font-size: 14px; margin: 0; min-height: 100%; }
#transcript { padding: 8px; }
```

The 8px inset moves from `body` margin to `#transcript` padding. Body margin was painting a ring of the WebView clear color (white) around the document. `#transcript` is the node `_document` and `_inject_script` already use. Do not change any other `_BASE_CSS` rule. Tests pin `.agent-chrome`, button colors, and the block-display defaults.

In `_make_owned_scroll`, after `scroll.set_hexpand(True)`, add:

```python
scroll.add_css_class("web-surface-host")
```

Do not change the policy `(NEVER, AUTOMATIC)`. Do not remove `_scroll`. `get_vadjustment`, the GTK smart-scroll handlers, and `tests/test_chat_surface.py` drive that adjustment.

### 2d. `ui/views/main_content.py` — outer chat host

In `create_chat_tab`, after `chat_scroll.set_policy(...)`, add:

```python
chat_scroll.add_css_class("web-surface-host")
```

Do not remove this scrolled window. It is the overlay child. The project-settings bar and the scroll-to-bottom button are `add_overlay` on `chat_overlay`. `_tab_scrolls` stores `chat_scroll`.

### 2e. `ui/styles.py` — host fill

Replace the feed-scroll rule:

```css
.feed-scroll { background: #1a1b26; }
```

Add next to it:

```css
.web-surface-host { background: #1a1b26; }
```

`background` here is the GTK widget behind the WebView texture. It does not paint inside the web process. `set_background_color` does not paint the GTK parent. Both are required. Do not delete the `.feed-card-*` GTK rules in this change. The HTML document does not use them. The GTK "Load More" bar still uses `feed-card-load-more` and `feed-btn-load-more`.

### 2f. `ui/views/feed_surface.py` — patch after the first load

**New instance fields**, set in `__init__` before `register_script_message_handler`:

```python
self._dom_ready = False
self._epoch = 0
self._load_epoch: int | None = None
self._patch_in_flight = False
self._dom_ids: list[str] = []
self._dom_html: dict[str, str] = {}
self._queued_reload = False
self._last_ops: list | None = None
self._patch_count = 0
```

*(Supervisor amendment 2026-10-23: the field list above reflects the implemented production names from the AlphaCoder worktree — `_epoch` replaces the drafted `_dom_epoch`/`_patch_epoch` pair (one counter, checked against the captured eval/load epoch; `_patch_epoch` was unused by the final design), `_queued_reload` replaces the `_pending_dom_*` staging pair (staging is unnecessary because `_flush` recomputes fragments and `_on_load_changed` adopts `_order` directly), and `_last_ops`/`_patch_count` are debug instrumentation. The callback contract likewise evolved: `_ops_script` returns `[ids, outcome]` — ids adopted as `_dom_ids`, outcome (`'followed'`/`'held'`) logged — rather than the drafted followed/held-only strings.)*

`__new__` tests do not run `__init__`. Section 2h lists the fields those helpers must set.

**`set_cards`.** At the top of the method, before `_order` is cleared:

```python
self._dom_epoch += 1
self._dom_ready = False
self._dom_ids = []
self._dom_html = {}
self._patch_in_flight = False
```

Leave the rest of `set_cards` as it is, including the scroll reset and the final `_schedule()`. Bumping `_dom_epoch` makes an in-flight patch callback or an in-flight `load_html` `FINISHED` refuse to commit. Clearing `_patch_in_flight` lets `_schedule` queue the new document. If `_load_in_flight` is still true, `_schedule` only sets `_dirty` (see below). `FINISHED` of the old load then schedules the new one.

**`_schedule`.** Extend the early return. Current code:

```python
def _schedule(self) -> None:
    self._dirty = True
    if self._render_source is not None or self._load_in_flight:
        return
    self._render_source = GLib.idle_add(self._flush)
```

New condition: also return when `self._patch_in_flight` is true. Still set `_dirty = True` first. Still return `None`. `GLib.idle_add`'s source id is stored and not inspected, same as today.

**`_flush`.** Keep the signature `() -> bool` and keep returning `False`.

```python
def _flush(self) -> bool:
    self._render_source = None
    if self._load_in_flight or self._patch_in_flight:
        return False
    self._dirty = False
    self._fragments()  # fills self._html; return value is the fragment list
    if self._dom_ready and self._dom_ids and self._order:
        ops = self._plan_feed_ops()
        if ops == []:
            return False
        if ops is not None:
            self._pending_dom_ids = list(self._order)
            self._pending_dom_html = {
                cid: self._html[cid] for cid in self._order if cid in self._html
            }
            self._patch_epoch = self._dom_epoch
            self._patch_in_flight = True
            epoch = self._patch_epoch
            script = _ops_script(ops)

            def _finish(view, result, _data, epoch=epoch):
                text = None
                try:
                    text = view.evaluate_javascript_finish(result).to_string()
                except Exception:
                    logger.exception("feed patch eval failed")
                self._on_patch_finished(text, epoch)

            try:
                self._webview.evaluate_javascript(
                    script,
                    len(script.encode("utf-8")),
                    None, None, None,
                    _finish, None,
                )
            except Exception:
                logger.exception("feed patch eval failed")
                self._patch_in_flight = False
                self._dom_ready = False
                self._dirty = True
                self._schedule()
            return False
    document = render_feed_document(self._fragments())
    self._scroll_after = bool(self._doc_near_bottom)
    self._dom_ready = False
    self._load_epoch = self._dom_epoch
    self._pending_dom_ids = list(self._order)
    self._pending_dom_html = {
        cid: self._html[cid] for cid in self._order if cid in self._html
    }
    self._load_in_flight = True
    try:
        self._webview.load_html(document, "about:blank")
    except Exception:
        logger.exception("feed load_html failed")
        self._load_in_flight = False
    return False
```

Trace notes:

- `_fragments()` already writes `self._html[cid]` for every id in `_order` and returns the list. Calling it twice is the same data. The second call in the load path matches today's single call. Its return value is passed to `render_feed_document` and otherwise ignored, which is what the current `_flush` does.
- `ops == []` means the DOM already matches. Do not eval. Do not set `_scroll_after`.
- `ops is None` falls through to `load_html`. `_dom_ready` is set `False` before the load so a later flush cannot patch against a document that is about to be replaced.
- `_scroll_after` is set only on the load path. The patch script scrolls itself. Existing `_flush` tests construct a surface with `_dom_ready` false, so they still take the load path and still see `_scroll_after`.
- The `evaluate_javascript` argument order is the one in `_eval_document_scroll`: script, byte length, world `None`, three `None`s, callback, user-data `None`. World `None` is the main world. Do not pass `"develcakes-scroll"`. That world is the chat surface's isolated world for when content JS is off. Feed JS is on, and the page's `#feed` node is what this script edits.
- The callback's return value is ignored by WebKit. `_on_patch_finished` does the work.
- A synchronous `evaluate_javascript` exception clears the gate, drops `_dom_ready`, sets `_dirty`, and `_schedule()`s. The next flush takes the load path because `_dom_ready` is false. It does not retry the patch, so a raising eval cannot idle-loop.
- Do not set `_load_in_flight` around a patch. `FINISHED` will not fire for an eval, so that flag would stick.

**`_on_load_changed`.** After the existing `event != FINISHED` return, and before scroll restore:

```python
self._load_in_flight = False
if self._load_epoch != self._dom_epoch:
    self._scroll_after = False
    if self._dirty:
        self._schedule()
    return
self._dom_ready = True
self._dom_ids = list(self._pending_dom_ids)
self._dom_html = dict(self._pending_dom_html)
```

Then the existing `follow = self._scroll_after` / `_eval_document_scroll` / `_dirty` schedule. Do not commit `_dom_*` on an epoch mismatch. That is the `set_cards`-during-load case: the `FINISHED` belongs to the previous document. `_on_load_failed` and `_on_web_process_terminated` stay as they are (clear `_load_in_flight`, schedule if `_dirty`). They must not set `_dom_ready = True`.

**`_on_patch_finished(self, text: str | None, epoch: int) -> None`.**

```python
def _on_patch_finished(self, text: str | None, epoch: int) -> None:
    if epoch != self._dom_epoch:
        return
    self._patch_in_flight = False
    if text in ("followed", "held"):
        self._dom_ids = list(self._pending_dom_ids)
        self._dom_html = dict(self._pending_dom_html)
    else:
        logger.error("feed patch did not apply (%s) — reloading the document", text)
        self._dom_ready = False
        self._dirty = True
    if self._dirty:
        self._schedule()
```

An epoch mismatch returns without touching `_patch_in_flight`. `set_cards` already cleared it and may have started a newer flush that set it true again. A stale callback must not clear the newer gate and must not write `_dom_ids`.

`text is None` (finish raised, or the script returned nothing) takes the reload branch. `"missing"` takes the reload branch. `"followed"` and `"held"` commit. No other success string.

**`_plan_feed_ops(self) -> list[dict] | None`.**

`None` means "do not patch; the caller reloads". A list is the ops, and it may be empty.

Ops are dicts with string keys:

| op | keys | meaning |
|---|---|---|
| `remove` | `id` | drop that article |
| `prepend` | `id`, `html` | insert this fragment before the first node that survives the removes |
| `append` | `id`, `html` | insert this fragment after the last child |
| `replace` | `id`, `html` | replace that article; the script strips `feed-card-enter` |

```python
def _plan_feed_ops(self) -> list[dict] | None:
    order = self._order
    dom = self._dom_ids
    if len(set(order)) != len(order) or len(set(dom)) != len(dom):
        return None
    dom_set = set(dom)
    order_set = set(order)
    removed = [cid for cid in dom if cid not in order_set]
    removed_set = set(removed)
    remain_dom = [cid for cid in dom if cid not in removed_set]
    remain_order = [cid for cid in order if cid in dom_set]
    if remain_dom != remain_order:
        return None
    new_ids = [cid for cid in order if cid not in dom_set]
    if not remain_dom and new_ids:
        return None
    if new_ids and order[:len(new_ids)] == new_ids:
        prepends = new_ids
        appends: list[str] = []
    elif new_ids and order[-len(new_ids):] == new_ids:
        prepends = []
        appends = new_ids
    elif not new_ids:
        prepends = []
        appends = []
    else:
        return None
    ops: list[dict] = [{"op": "remove", "id": cid} for cid in removed]
    for cid in prepends:
        html = self._html.get(cid)
        if html is None:
            return None
        ops.append({"op": "prepend", "id": cid, "html": html})
    for cid in appends:
        html = self._html.get(cid)
        if html is None:
            return None
        ops.append({"op": "append", "id": cid, "html": html})
    for cid in remain_order:
        html = self._html.get(cid)
        if html is None:
            return None
        if html != self._dom_html.get(cid):
            ops.append({"op": "replace", "id": cid, "html": html})
    return ops
```

Traced cases:

- Append one id: `order[-1:]` is that id, remain order unchanged → one `append`.
- Prepend one id (`upsert(..., prepend=True)` does `_order.insert(0, cid)`): `order[:1]` is that id → one `prepend`.
- Two appends coalesced before the idle: both are the suffix → two `append`s, document order.
- Two prepends coalesced: both are the prefix, in the list's final order. The script inserts each before the same anchor, so the prefix stays in list order (see the script).
- One prepend and one append in the same flush: neither the prefix nor the suffix equals `new_ids` → `None` → one `load_html`. That is the rare load-more-plus-new-card race. One clear, then patches resume.
- Reveal, context toggle, body change: same ids, different `self._html[cid]` → one `replace`.
- Reorder of surviving ids → `None`.
- Every previous id gone and new ids present → `None` (the `not remain_dom and new_ids` line).
- Duplicate ids → `None`.

`_html.get` returning `None` is a reload, not a patch with a hole. `_fragments()` runs first, so a present card has an entry. The `None` branch is the defensive path if a caller plans against a stale `_html`.

**`_ops_script(ops: list[dict]) -> str`.** Module-level, next to `_restore_script`. `json` is already imported. `json.dumps(..., ensure_ascii=True)` produces an ASCII JSON text that is a valid JS expression. Card HTML sits inside that JSON. It is not concatenated into the script source. `ensure_ascii=True` escapes non-ASCII, including line separators.

The script uses the feed's own scroll math, not the chat surface's `scrollingElement` math. `FEED_SCRIPT` decides near-bottom with `scrollHeight - innerHeight - scrollY <= 80`. `_BOTTOM_SCRIPT` is `window.scrollTo(0, document.documentElement.scrollHeight)`. The patch follows and restores with those, so a patch and a full load land in the same place. The `80` must stay equal to the `<= 80` in `FEED_SCRIPT`.

```python
def _ops_script(ops: list[dict]) -> str:
    payload = json.dumps(ops, ensure_ascii=True)
    return (
        "(function () {"
        "  var doc = document.documentElement;"
        "  function scrollTop() { return window.scrollY || doc.scrollTop || 0; }"
        "  function nearBottom() {"
        "    var viewport = window.innerHeight || doc.clientHeight || 0;"
        "    return (doc.scrollHeight - viewport - scrollTop()) <= 80;"
        "  }"
        "  var feed = document.getElementById('feed');"
        "  if (!feed) { return 'missing'; }"
        f"  var ops = {payload};"
        "  function find(id) {"
        "    var nodes = feed.querySelectorAll('article[data-card-id]');"
        "    for (var i = 0; i < nodes.length; i++) {"
        "      if (nodes[i].getAttribute('data-card-id') === id) { return nodes[i]; }"
        "    }"
        "    return null;"
        "  }"
        "  function node(html, enter) {"
        "    var tpl = document.createElement('template');"
        "    tpl.innerHTML = html;"
        "    var eln = tpl.content.firstElementChild;"
        "    if (!eln) { return null; }"
        "    if (enter) { eln.classList.add('feed-card-enter'); }"
        "    return eln;"
        "  }"
        "  var wasBottom = nearBottom();"
        "  var y = scrollTop();"
        "  var before = doc.scrollHeight;"
        "  var prepended = false;"
        "  for (var r = 0; r < ops.length; r++) {"
        "    if (ops[r].op !== 'remove') { continue; }"
        "    var gone = find(ops[r].id);"
        "    if (gone && gone.parentNode) { gone.parentNode.removeChild(gone); }"
        "  }"
        "  var anchor = feed.firstElementChild;"
        "  for (var p = 0; p < ops.length; p++) {"
        "    if (ops[p].op !== 'prepend') { continue; }"
        "    var pel = node(ops[p].html, true);"
        "    if (!pel) { return 'missing'; }"
        "    feed.insertBefore(pel, anchor);"
        "    prepended = true;"
        "  }"
        "  for (var a = 0; a < ops.length; a++) {"
        "    if (ops[a].op !== 'append') { continue; }"
        "    var ael = node(ops[a].html, true);"
        "    if (!ael) { return 'missing'; }"
        "    feed.appendChild(ael);"
        "  }"
        "  for (var u = 0; u < ops.length; u++) {"
        "    if (ops[u].op !== 'replace') { continue; }"
        "    var old = find(ops[u].id);"
        "    var nel = node(ops[u].html, false);"
        "    if (!old || !nel || !old.parentNode) { return 'missing'; }"
        "    old.parentNode.replaceChild(nel, old);"
        "  }"
        "  if (wasBottom) {"
        "    window.scrollTo(0, doc.scrollHeight);"
        "    return 'followed';"
        "  }"
        "  if (prepended) {"
        "    var grew = doc.scrollHeight - before;"
        "    if (grew > 0) { window.scrollTo(0, y + grew); }"
        "  }"
        "  return 'held';"
        "})();"
    )
```

`find` compares `getAttribute` to the raw id. Do not build a CSS selector from the id. `render_feed_card` HTML-escapes the attribute; `getAttribute` returns the decoded value, which is the Python `card_id` that `json.dumps` put in the op.

`insertBefore(pel, anchor)` with a stable anchor: start `[C]`, insert A before C → `[A, C]`, insert B before C → `[A, B, C]`. That is why prepend ops are in document order and share one anchor captured after removes.

`insertBefore(node, null)` appends. After a remove of every article, anchor may be `null` or the empty-state div. The planner does not emit prepends in the "no remaining ids" case (`None` → reload), so the script does not rely on that.

`replace` passes `enter=false`, so a badge or body update does not fade the card. Inserts pass `true`.

`removeChild` / `replaceChild` are used instead of `Element.remove` / `replaceWith`. Those two are not used anywhere in this repo. `template`, `appendChild`, `classList.add`, and `getAttribute` are.

The copy-button "copied" class lives on the button node. A `replace` of that card drops it. A full `load_html` already drops it for every card (`FEED_SCRIPT` says so). A patch drops it only for the card that was replaced.

### 2g. `ui/views/feed_tab.py` — map handler

At the top of `_on_scroll_mapped`, before the `_was_near_bottom` branch:

```python
if self._html_surface is not None:
    return False
```

The signal's other returns are already `False`. GTK does not use this return as an idle-source flag; match the existing returns anyway. Do not change `schedule_smart_scroll_to_bottom`. It already returns when `_html_surface is not None`. Do not delete `schedule_scroll_to_bottom` or the settle helpers. `tests/test_feed_handler.py` calls them on a tab with `_html_surface is None`.

### 2h. Tests

**`tests/test_feed_surface_scroll.py` `_surface()`.** `__init__` does not run. Add, next to the other field sets:

```python
surface._dom_ready = False
surface._dom_epoch = 0
surface._load_epoch = 0
surface._patch_epoch = 0
surface._patch_in_flight = False
surface._dom_ids = []
surface._dom_html = {}
surface._pending_dom_ids = []
surface._pending_dom_html = {}
```

Without `_patch_in_flight`, `_schedule` (called from `set_cards`) raises `AttributeError`. Without `_dom_ready`, `_flush` raises. With them false/empty, every existing `_flush` test still takes `load_html`. Do not rewrite those tests.

**`tests/test_feed_html.py` `test_surface_toggle_reveal_and_body_reset_without_a_page_load`.** That `__new__` surface calls `_schedule` via `toggle_context`. Add `surface._patch_in_flight = False` next to `surface._load_in_flight = True`. `_load_in_flight` is already true, so `_schedule` returns before it reads `_dom_ready`. The new flag is the one it will read.

**New tests in `tests/test_feed_surface_scroll.py`.** Use `_surface()` and `_card`. Do not construct a real `WebKit.WebView`.

1. `_apply_canvas_color` on a fake with `set_background_color(self, rgba)`. Assert `rgba.alpha == 1.0` and that `parse` was not skipped (red > 0). Import `_apply_canvas_color` from `ui.views.feed_surface`.
2. `_dom_ready True`, `_dom_ids == ["card-1"]`, `_dom_html` equal to `render_feed_card` of that card, then `upsert` a second card and call `_flush` directly (do not rely on the idle). Assert `loads == []`, `len(evals) == 1`, the script contains `"append"` and the new card id, and does not contain `load_html`. `_FakeView.evaluate_javascript` already records the script and does not call the callback. Then call `_on_patch_finished("followed", surface._patch_epoch)` and assert `_dom_ids` is both ids and `_patch_in_flight` is false.
3. Same setup, `_on_patch_finished("missing", epoch)` asserts `_dom_ready` is false and `_dirty` is true.
4. `_on_patch_finished("followed", epoch + 1)` after bumping nothing: pass `surface._dom_epoch + 1`. Assert `_dom_ids` is unchanged and `_patch_in_flight` stays at whatever the test set (set it `True` before the call, assert it is still `True`).
5. `_plan_feed_ops`: dom `[a, b]`, order `[z, a, b]` → one prepend. dom `[a, b]`, order `[a, b, d]` → one append. dom `[a, b]`, order `[z, a, b, d]` → `None`. Identical html → `[]`.
6. `set_cards` on a surface with `_dom_ready True` and `_patch_in_flight True` (use `_frozen`, which also sets `_load_in_flight`) asserts `_dom_ready` is false, `_dom_epoch` increased, `_patch_in_flight` is false. Existing scroll-reset assertions stay.

**`tests/test_chat_surface.py`.** One test: import `_apply_canvas_color` and run it against a fake `set_background_color`. Assert alpha `1.0`. Do not instantiate `WebKit.WebView` in this test. A second assertion: `"margin: 0"` is in `_BASE_CSS`, `"#transcript { padding: 8px; }"` is in `_BASE_CSS`, and `"html { background: #1a1b26; height: 100%; }"` is in `_BASE_CSS`.

**`tests/test_feed_html.py`.** Assert `"html { background: #1a1b26; height: 100%; }"` is in `FEED_CSS`. Assert `"animation: feed-card-in"` is not in the `.feed-card { ... }` block (slice from `.feed-card {` to the first `}`), and is in `.feed-card-enter`. Keep `test_keyframes_are_opacity_only` unchanged; the keyframes name stays.

**`tests/test_feed_surface_scroll.py` `TestAttachHtmlSurfaceIntegration`.** New test: after `_attached_tab`, set `_was_near_bottom = True`, call `_on_scroll_mapped(tab._feed_scroll)`, assert `_scroll_handler_id is None`. Today, without the guard, that call hits `schedule_scroll_to_bottom` and sets the handler id.

### 2i. Line counts (estimate)

| File | Delta |
|---|---|
| `ui/views/feed_surface.py` | +220 |
| `render/feed_html.py` | +6 |
| `ui/views/chat_surface.py` | +30 |
| `ui/styles.py` | +2 |
| `ui/views/main_content.py` | +1 |
| `ui/views/feed_tab.py` | +2 |
| `tests/test_feed_surface_scroll.py` | +160 |
| `tests/test_feed_html.py` | +15 |
| `tests/test_chat_surface.py` | +25 |

**Files NOT changed**

- `ui/views/feed_tab.py` policy and `attach_html_surface` — `tests/test_feed_surface_scroll.py` pins `(EXTERNAL, NEVER)`. The webview is already `vexpand`/`hexpand`. Vertical `NEVER` allocates it the viewport. The dark slab is the transparent CSS, not the policy.
- `ChatSurface._scroll` and the page-level `chat_scroll` in `create_chat_tab` — overlay, `_tab_scrolls`, `get_vadjustment`, and the chat scroll tests depend on them. Removing either is a different spec.
- `schedule_scroll_to_bottom`, `_settle_scroll`, `_build_load_more_widget`, `ui/feed/snapshots.py` `_rebuild_context_panel`, and the `.feed-card-*` block in `ui/styles.py` — not on the WebView paint path. The handler tests still call the GTK scroll API when no HTML surface is attached. Deleting them here would churn that suite without changing the flash.
- `assets/splash.html` and `_WELCOME_FALLBACK_HTML` — both already paint `html,body`.
- `FEED_SCRIPT` — the click and `scroll_state` listeners stay. Patches do not replace the document, so they keep working. Do not add card data to the script.
- No environment variable. Compositing-off makes resize smoother in WebKit's own notes and makes scrolling worse. It is not a default.

---

## 3. Data Flow

**First feed paint.** `FeedHandler` → `FeedTab.set_html_cards` → `FeedSurface.set_cards` → `_dom_epoch` bumps, `_dom_ready = False` → `_schedule` → idle `_flush`. `_dom_ready` is false, so `_flush` calls `render_feed_document` and `load_html`. The WebView's clear color is already `#1a1b26` from `__init__`. On `load-changed` `FINISHED`, if `_load_epoch == _dom_epoch`, commit `_pending_dom_ids` / `_pending_dom_html`, set `_dom_ready`, then the existing `_BOTTOM_SCRIPT` or `_restore_script`.

**Another card while the reader is at the bottom.** `upsert` appends the id and `_schedule`s. `_flush` calls `_fragments()` then `_plan_feed_ops()`. One `append` op. `evaluate_javascript` runs `_ops_script` in the main world. The script reads `nearBottom()` before mutating, appends the article with `feed-card-enter`, then `window.scrollTo(0, scrollHeight)` because `wasBottom` was true, and returns `"followed"`. `_on_patch_finished` copies `_pending_dom_*` onto `_dom_ids` / `_dom_html`. No `load_html`. The `scroll_state` listener in `FEED_SCRIPT` is still the same document, so it posts the new offset on the next scroll.

**Reader scrolled up, older cards prepended.** Plan emits `prepend`s. The script captures `y = scrollTop()` and `before = scrollHeight`, inserts before the anchor, then `window.scrollTo(0, y + (scrollHeight - before))` because `wasBottom` was false. The viewport stays on the cards the reader was reading.

**`set_cards` during a patch.** Epoch bumps. The patch callback's epoch does not match, so it returns without writing `_dom_ids`. `_schedule` from `set_cards` queues a full load because `_dom_ready` is false.

**`set_cards` during `load_html`.** `_schedule` sees `_load_in_flight` and only sets `_dirty`. The old load's `FINISHED` sees `_load_epoch != _dom_epoch`, does not commit, and `_schedule()`s. That flush loads the new document.

**Chat resize.** GTK grows the overlay, both scrolled windows, and the WebView. The web process paints later. New pixels outside the old texture are the host's `#1a1b26` (GTK CSS) and, inside the view, the WebView clear color `#1a1b26`. The transcript's `html`/`body` rules cover the viewport once that frame arrives. The welcome splash then replaces the document with its own `#06060d` background.

**Feed tab mapped.** `_on_scroll_mapped` sees `_html_surface` and returns. It does not connect a `changed` handler or start `_settle_scroll`.

---

## 4. File Change Summary

| File | Change | Risk |
|---|---|---|
| `ui/views/feed_surface.py` | Canvas color; patch planner, script, epoch | High — scroll and coalescing |
| `render/feed_html.py` | `html` rule; animation moves to `.feed-card-enter` | Low |
| `ui/views/chat_surface.py` | Canvas color; `body` margin → `#transcript` padding; CSS class on the inner scroll | Low |
| `ui/views/main_content.py` | CSS class on the outer chat scroll | Low |
| `ui/styles.py` | `.feed-scroll` and `.web-surface-host` fill | Low |
| `ui/views/feed_tab.py` | Map handler returns when a surface is attached | Low |
| `tests/test_feed_surface_scroll.py` | Helper fields; patch and epoch tests; map test | Medium |
| `tests/test_feed_html.py` | `__new__` flag; CSS pins | Low |
| `tests/test_chat_surface.py` | Canvas helper and `_BASE_CSS` pins | Low |

---

## 5. Implementation Order

1. **Color only.** `_apply_canvas_color` in both modules, the CSS edits in `FEED_CSS`, `_BASE_CSS`, and `ui/styles.py`, the two `add_css_class` calls, the map-handler guard. Update the `__new__` helpers so the suite still imports. Do not add the patch path yet. Run the three test modules below. Existing feed flush tests must still see `load_html`.
2. **Patch path.** `_plan_feed_ops`, `_ops_script`, `_flush` branch, `_on_patch_finished`, epoch checks in `set_cards` and `_on_load_changed`, `_schedule` gate. Add the new tests from §2h. Run the same modules again.
3. **Do not continue into the "not changed" list** if step 2 is green. The resize gap will still be one web-process frame late. That is the acceptance state, not a reason to set a WebKit environment variable.

Verification after each step:

```bash
python3 -m pytest tests/test_feed_surface_scroll.py tests/test_feed_html.py tests/test_chat_surface.py tests/test_feed_handler.py -q --tb=short
```

`test_feed_handler.py` is in the run because the map-handler guard sits next to tests that call `_on_scroll_mapped` with no HTML surface. Those must stay green.

Paste the pytest summary (passed/failed counts) in the implementation report. "Tests pass" without the summary is not done.

After step 2, grep and expect no hits in the changed production files for a second `set_background_color` call site beyond `_apply_canvas_color`:

```bash
grep -n "set_background_color" ui/views/feed_surface.py ui/views/chat_surface.py
```

Both files have the call once, inside `_apply_canvas_color`, and one call to `_apply_canvas_color`. A second direct `set_background_color` is a bug (the clear color latches on first paint).

```bash
grep -n "animation: feed-card-in" render/feed_html.py
```

One hit, on `.feed-card-enter`, plus the `@keyframes feed-card-in` name. Zero hits on the `.feed-card {` rule.

---

## 6. Acceptance Criteria

- [ ] Both WebViews call `_apply_canvas_color` before their first `load_html`. Alpha of the color is 1.
- [ ] `FEED_CSS` and `_BASE_CSS` set `html` and `body` to `#1a1b26`, `html` height `100%`, `body` min-height `100%`. Chat `body` margin is 0. `#transcript` padding is 8px.
- [ ] `.feed-scroll` and `.web-surface-host` are `#1a1b26`, not transparent.
- [ ] A feed `upsert` / `remove` / `reveal` / `toggle_context` after a committed document does not call `load_html` when `_plan_feed_ops` returns a list.
- [ ] `set_cards`, an empty document, a plan of `None`, and a patch result other than `"followed"` or `"held"` use `load_html`.
- [ ] A patch callback whose epoch is not `_dom_epoch` does not write `_dom_ids` and does not clear a newer `_patch_in_flight`.
- [ ] A `load-changed` `FINISHED` whose `_load_epoch` is not `_dom_epoch` does not set `_dom_ready`.
- [ ] While following, the patch script calls `window.scrollTo(0, document.documentElement.scrollHeight)`. While not following, a prepend adjusts `scrollTop` by the scrollHeight delta. A replace or append while not following does not change `scrollTop`.
- [ ] `.feed-card` has no entrance animation. Insert ops add `feed-card-enter`. Replace ops do not.
- [ ] Feed policy stays `(EXTERNAL, NEVER)`. Both chat scrolled windows still exist.
- [ ] `_on_scroll_mapped` does not arm `_scroll_handler_id` when `_html_surface` is set. The no-surface map tests in `test_feed_handler.py` still arm it.
- [ ] No `WEBKIT_DISABLE_*` assignment in Python.
- [ ] Resize still updates a frame late. The late region is `#1a1b26`, not white and not the notebook showing through a transparent scroll.
- [ ] Feeding another card does not blank the web view to white.

---

## 7. Edge Cases

| Case | Expected |
|---|---|
| `Gdk.RGBA.parse("#1a1b26")` returns `False` | Log, do not call `set_background_color`, continue. The view still loads, on the default white. |
| `set_background_color` raises | Log, continue. Same as a missed color, not a dead surface. |
| First card after the empty state (`_dom_ids` empty) | `load_html`. The empty state is a `div.feed-empty`, not an article. Do not patch onto it. |
| Last card removed (`_order` empty) | The `_dom_ready and _dom_ids and _order` guard fails. `load_html` of the empty document. |
| Patch eval raises before the callback | `_dom_ready = False`, `_dirty = True`, `_schedule()`. Next flush is `load_html`. |
| Callback text is `None` or `"missing"` | Same reload. Do not commit `_pending_dom_*`. |
| `set_cards` while a patch callback is outstanding | Epoch mismatch. Callback returns. New document loads. |
| `set_cards` while `load_html` is in flight | `_dirty` stays set. Old `FINISHED` does not commit. New flush loads the new cards. |
| Prepend and append in one flush | `_plan_feed_ops` returns `None`. One `load_html`. |
| Reorder of existing ids | `None`. `load_html`. |
| Duplicate id in `_order` or `_dom_ids` | `None`. `load_html`. |
| Identical upsert (same HTML) | Plan is `[]`. No eval, no load, scroll unchanged. |
| Reader at bottom, append | Script returns `"followed"` and scrollTo bottom. |
| Reader not at bottom, append or replace | Script returns `"held"`. `scrollTop` unchanged. |
| Reader not at bottom, prepend | `scrollTop` increases by the scrollHeight growth. |
| Replace of the card under the copy-check timer | That card's copied state drops. Other cards keep theirs. |
| Web process crash (`web-process-terminated`) | Existing handler clears `_load_in_flight` and reschedules if `_dirty`. It does not set `_dom_ready`. The next flush reloads. |
| `load-failed` | Existing handler. Does not set `_dom_ready`. |
| Welcome splash | Same WebView, color already applied in `_ensure_webview`. Splash CSS paints `#06060d` after load. |
| Live JS off (`LIVE_JS=0`) | Chat still full-reloads via `load_html`. The canvas color and the `html`/`body` rules still apply. This spec does not change the inject-vs-reload choice. |
| `NO_WEBKIT` or WebKit import failure | `create_feed_surface` returns `None`. No WebView, no color call. Chat uses `TextViewFallback`, which never calls `_ensure_webview`. |
| Feed tab shown with a surface | Map handler returns. No GTK settle. |
| Feed tab shown with no surface | Map handler behaves as it does today. |

---

## 8. ARCHITECTURE.md Updates Required

`docs/ARCHITECTURE.md` does not mention `ui/views/feed_surface.py` or `render/feed_html.py`. Section 3.22a still says `feed_card.py` is the `NO_WEBKIT` fallback and describes `build_feed_card`, which the view module no longer exports. Do not rewrite 3.22a in this change.

Add one paragraph under 3.22a:

- The live feed is one `WebKit.WebView` in `ui/views/feed_surface.py`. The first paint and any structural reset use `load_html`. Later inserts, deletes, and in-place card updates patch `#feed` with `evaluate_javascript`. The view background and the `.feed-scroll` host are `#1a1b26`. Chat uses the same view background from `ChatSurface._ensure_webview`.

No other architecture section changes.

---

## 9. What this spec will not make smooth

Resizing the window still delivers the new web frame after GTK has already grown the widget. WebKit paints that frame in another process. Epiphany looks smoother because the late pixels are the page color and the view is the window. After this spec the late pixels are `#1a1b26` on both the GTK host and the WebView clear color, and a feed update does not navigate. The frame is still late. Do not paper over that with `WEBKIT_DISABLE_COMPOSITING_MODE` or `WEBKIT_DISABLE_DMABUF_RENDERER` in Python. Those switches trade scroll glitches for resize glitches, and which one wins depends on the GPU.

The nested chat scrolled windows (overlay → page scroll → surface scroll → WebView) stay. They add a measure pass on resize. Taking them out moves the scroll-to-bottom button, the overlay, and the adjustment tests. That is a separate spec.

Leftover GTK feed code (`schedule_scroll_to_bottom` once a surface exists, the Pango `.feed-card-*` rules, `_build_load_more_widget`, the snapshot context-panel rebuild) does not draw the WebView. It is not the white flash and it is not the dark slab. Leave it.
