# SPEC-17: Chat Scroll — Newest at the Bottom

**Date:** 2026-10-07
**Status:** READY — not started
**Implements:** The read-only scroll finding from 2026-10-07. A chat shows the
newest message at the bottom. If the reader is already there, new messages
keep them there. If they have scrolled up to read, the view stays put.
**Depends on:** Nothing in SPEC-16. Do not start those phases as part of this
spec.
**Target branch:** main

Line numbers below were true on 2026-10-07. Anchor on the symbol names.
If a line has moved, follow the symbol.

---

## 0. Rules for the implementing agent

- One phase, then its tests. Do not widen into SPEC-16.
- `settings.set_enable_javascript(False)` stays inside `_ensure_webview`.
  Do not call `set_enable_javascript(True)`.
  `tests/test_html_guard_sites.py::TestChatSurfaceJavaScriptOff` pins that.
- `evaluate_javascript` is the app-side API. It is allowed. Page content
  stays non-scriptable.
- Do not reverse message order. The document is already oldest-first.
- Do not treat the position after `load_html` as the reader's intent.
  A load opens the document at the top. That top is the oldest message.
- Handlers must not import each other. This change stays in
  `ui/views/chat_surface.py` and the one call site in
  `ui/views/main_content.py`.
- Do not touch `scratch/`, SPEC-15, or the Telegram bridge.

---

## 1. What is already true

**Document order is chat order.** `ChatSurface.append_message` appends to
`self._rows`. `_document` writes that list from first to last. The newest
row is the last element in `<body>`. That is the bottom of the document.
Do not prepend, and do not use `column-reverse`.

**The view opens on the oldest row.** Every append rebuilds the page with
`_load_html` → `WebKit.WebView.load_html`. A load shows the start of the
document. The start is the first message.

**Smart scroll moves the wrong scrollbar.** `ChatSurface._settle_restore`
does `vadj.set_value(vadj.get_upper() - vadj.get_page_size())` on
`self._scroll.get_vadjustment()`, the `Gtk.ScrolledWindow` around the web
view. `WebKit.WebView` does not implement `Gtk.Scrollable`. The page
scrolls inside the web view. The outer adjustment is not that scroll.
`_BOTTOM_THRESHOLD` is 80 pixels and the follow/preserve split is the right
rule. It is applied to the wrong object.

**The tests pass against that wrong object.** `TestSmartScroll` in
`tests/test_chat_surface.py` monkeypatches `_load_html` and then calls
`vadj.set_upper(...)`. It never loads a document. A green run does not
mean the newest message is on screen.

**`TextViewFallback` is already a chat.** It inserts at the end of the
buffer and, when `_was_at_bottom` is true, sets its own adjustment to
`upper - page_size`. Leave that path alone.

**The scroll-to-bottom button uses the same outer adjustment.**
`MainContent.scroll_chat_to_bottom` calls `surface.get_vadjustment()` and
sets that value. On an HTML tab that does not move the document.

---

## SP1 — Scroll the document, not the outer adjustment

**Goal.** After each HTML reload, the web view's document is scrolled to
match the reader's pre-reload intent. Caught up means the bottom of the
document (newest message). Reading means the same document offset as
before the reload. The first paint of an empty surface lands on the
bottom once there is content.

**Files.** `ui/views/chat_surface.py`. `ui/views/main_content.py` only for
the button.

### SP1.1 Capture before the load

In `ChatSurface._do_render`, do not call `_load_html` until the pre-load
read has finished or failed.

- If there is no web view yet, or the read fails, store
  `(at_bottom=True, y=0.0)` and then load. The fresh surface lands at
  the bottom.
- If a document is already loaded, call `evaluate_javascript` with a
  script that returns a JSON string:

```javascript
(function () {
  var el = document.scrollingElement || document.documentElement;
  var y = el.scrollTop || 0;
  var max = Math.max(0, el.scrollHeight - el.clientHeight);
  var atBottom = (max - y) <= 80;
  return JSON.stringify({y: y, atBottom: atBottom});
})();
```

- Finish with `evaluate_javascript_finish`. Read the
  `JavaScriptCore.Value` with `to_string()` (or `to_json` if that returns
  the object text) and `json.loads`. On any exception, timeout, or
  malformed payload, use `(True, 0.0)`.
- `length` is the script length in bytes, the argument
  `evaluate_javascript` requires.
  **REVISED by probe (2026-10-07, SP1 delivery): `world_name` must be a
  named isolated world (implemented as `_DOC_WORLD = "develcakes-scroll"`).
  The original "may be `None`" is stale on WebKit 6.0 / GI 3.48 with
  `set_enable_javascript(False)`: `None` resolves to the disabled MAIN
  world and every call raises `WebKitJavascriptError 699` (probe case A).
  The named world runs app-side script with page JS still off (probe case
  B). `source_uri` may be `None`.**
- Store the pair on the surface as the single pending intent. A newer
  read replaces an older unread intent. Coalesced appends still produce
  one load.

Only then call the existing `_load_html(_document(...))`.

The 80 in the script is the same threshold as `ChatSurface._BOTTOM_THRESHOLD`.
Use the constant. Do not invent a second number.

### SP1.2 Apply after the load finishes

Connect `load-changed` once, in `_ensure_webview`, on that web view.

When the event is `WebKit.LoadEvent.FINISHED`:

- If the surface is destroyed, do nothing.
- If the pending intent says at-bottom, run:

```javascript
(function () {
  var el = document.scrollingElement || document.documentElement;
  el.scrollTop = el.scrollHeight;
})();
```

- If the pending intent says reading, run a script that sets
  `el.scrollTop` to the captured `y`. Pass `y` as a number you format
  yourself. Do not interpolate a string from the page.
- Clear the pending intent after you issue the scroll script.
- Do not read the document position at `FINISHED` and then decide.
  The load has already forced that position to the top. Using it here
  is the bug: the code concludes the reader left the bottom, and leaves
  them on the oldest message.

If `_dirty` became true while the load was in flight, schedule one more
render after this scroll is issued. Do not drop the row that arrived
mid-load.

### SP1.3 The button

Add `ChatSurface.scroll_to_latest()`. It sets the follow intent to
at-bottom and runs the same bottom script as SP1.2. If no document is
loaded yet, it only stores the intent so the next `FINISHED` lands at
the bottom.

`TextViewFallback.scroll_to_latest()` keeps today's behavior: set its
adjustment to `upper - page_size`, and set `_was_at_bottom = True`.

`MainContent.scroll_chat_to_bottom`: when the page's child is one of
these surfaces, call `scroll_to_latest()` and return. Do not also set
the outer adjustment for that page. Non-surface children keep the
existing `vadj.set_value(upper - page_size)` path.

### Do not

- Remove `set_enable_javascript(False)`.
- Change `_document` row order.
- Delete `TextViewFallback._follow_to_bottom`.
- Cancel a follow because the post-load GTK adjustment is at 0. That
  is `_settle_restore`'s live-tracker re-read (`if not self._was_at_bottom:
  return`). It must not be the decision for the document scroll.
- Make the outer `ScrolledWindow` the proof of success.

The GTK capture (`_pending_restore`, `_settle_restore`) may stay for now
if removing it breaks an unrelated test. It is not the fix. The document
script is the fix. Do not spend this phase deleting that machinery
unless a test you are already retargeting requires it.

**Done when**

- A reader at the document bottom who receives a message is at the
  document bottom afterward, on the newest row.
- A reader whose document offset is more than 80 pixels above the
  bottom stays at that offset after a message is appended below it.
- The first message on a fresh surface is shown at the bottom.
- The scroll-to-bottom button on an HTML tab runs the document script.
- `set_enable_javascript(True)` does not appear in `chat_surface.py`.

**Tests.** Retarget `TestSmartScroll` in `tests/test_chat_surface.py`.
Monkeypatch `evaluate_javascript` (or a one-line wrapper you add only
if the method is awkward to patch) so the test records the script and
invokes the callback.

Required cases:

| Case | Assert |
|---|---|
| At bottom, then append | The `FINISHED` script sets `scrollTop` from `scrollHeight`. The test fails if that script is not issued. |
| Reading (`atBottom` false, `y` captured) | The `FINISHED` script sets `scrollTop` to that `y`, not to `scrollHeight`. |
| Load resets position to 0 before `FINISHED` | Follow still scrolls to the bottom. The reset is not a cancel. |
| First message, read fails | Intent is at-bottom, and `FINISHED` scrolls to `scrollHeight`. |
| `scroll_to_latest` | Issues the bottom script and arms the next append to follow. |
| Fallback | Existing `test_fallback_autoscrolls_at_bottom` and the reading-preserve fallback test stay green without a document script. |

A test that only asserts `vadj.get_value() == upper - page_size` after
`set_upper` does not satisfy this spec. Change those assertions so they
watch the document script.

```bash
python -m pytest tests/test_chat_surface.py tests/test_chat_render_scroll.py tests/test_html_guard_sites.py -q
```

---

## 2. Out of scope

- Reordering messages so the newest row is first in the HTML.
- Turning page JavaScript on.
- SPEC-16 (status bar, pill, dead code, `_build`, `_run_loop`, class splits).
- Rewriting `TextViewFallback` scroll.
- `scratch/` and SPEC-15.

---

## 3. Definition of done

1. The test command above is green.
2. Caught up: a new message leaves the document scrolled to its bottom.
3. Reading: a new message leaves `scrollTop` at the captured `y`.
4. The load's jump to the top does not cancel a follow.
5. `settings.set_enable_javascript(False)` is still applied in
   `_ensure_webview`, and `set_enable_javascript(True)` is absent.
