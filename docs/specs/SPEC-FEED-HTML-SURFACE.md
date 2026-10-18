# SPEC: Feed tab — HTML document instead of a Pango card list

**Date:** 2026-10-17
**Author:** Micah
**Status:** Implemented (2026-10-17)
**Implements:** investigation 2026-10-17 (feed tab should be an HTML surface like chat)
**Depends on:** SPEC-06 (WebKit document pattern), SPEC-18 (feed scroll), SPEC-UI-RESPONSIVENESS-2 (body cap, live-widget window), SPEC-13 (feed was explicitly left on Pango)
**Target branch:** main
**Does not depend on:** SPEC-CHAT-MIXED-HTML. Feed bodies are tool output, diffs, and commands. They do not go through `render_message`.

> Architecture: `FeedCardData` in `models/feed_card.py` stays the record. `FeedHandler` stays the lifecycle (id, seq, persist, accept/reject, auto-accept). `FeedTab` stays the GTK host and the toolbar. This spec replaces the card *list* (a `Gtk.Box` of Pango labels) with one WebKit document when WebKit is available, and leaves the current widgets as the fallback when it is not.

---

## DISCOVERY

- Read `ui/views/feed_tab.py`: `FeedTab` is a vertical `Gtk.Box`. Child 1 is `Gtk.ScrolledWindow` (`.feed-scroll`) whose child is a vertical `Gtk.Box` (`.feed-card-list`, `_card_container`). Child 2 is `_toolbar` (Diffs, Files, Exec, agent dropdown, snooze, Accept All). Public list API: `append_card(widget, card_id)`, `prepend_card`, `remove_card`, `replace_card`, `show_empty_state`, `get_vadjustment`, `is_near_bottom`, `is_above_viewport`. `_cards_by_id: dict[str, Gtk.Widget]`. Scroll-to-bottom is a `changed` handler plus a timeout on that adjustment (SPEC-18). A freshly built tab already has a real adjustment.
- Read `ui/views/feed_card.py`: `build_feed_card(card_data, *, on_review, on_accept, on_reject, on_copy) -> Gtk.Widget` builds header, body, footer, optional action row, hidden context panel. Bodies are `Gtk.Label`s via `_apply_text_markup` → `escape_for_pango` → `set_markup`, with `Pango.parse_markup` and a `set_text` fallback. `RENDERED_BODY_LIMIT = 2000`. Reveal is a sibling `Gtk.Button` that calls `_apply_text_markup` with the full string. Action labels: "Approve"/"Deny" when `metadata["needs_approval"]` else "Accept"/"Reject". Informational cards get no buttons (`FeedCardData.is_informational`). `update_card_badge(widget, accepted)` walks header → body → footer and appends an ACCEPTED/REJECTED label. `update_card_in_place` mutates `_body_label` and returns `False` when the seam is missing. The module imports `gi` / `Gtk` / `Pango` at import time.
- Read `models/feed_card.py`: `FeedCardData` fields include `card_type`, `source`, `title`, `body`, `author`, `timestamp`, `project_name`, `file_path`, `metadata`, `conversation_snapshot`, `card_id`, `reviewed`, `accepted`, `seq_num`. `__post_init__` caps stored body at `MAX_STORED_BODY` (200_000). `css_class_for_type` returns `feed-card-git`, `feed-card-diff`, `feed-card-file-new`, `feed-card-file-mod`, `feed-card-file-del`, `feed-card-dir-new`, `feed-card-dir-del`, `feed-card-agent`, `feed-card-task`, `feed-card-system`, `feed-card-audit`. `is_actionable` is true for `needs_approval` or for `diff` / `file_created` / `file_modified` / `file_deleted`. No GTK in this module. `_format_timestamp` does **not** live here; it lives in the view (just now / Nm / Nh / Nd).
- Read `ui/handlers/feed_handler.py`: `add_card(card_data, persist: bool = True) -> str` assigns `uuid4` `card_id`, increments `_project_seq[project_name]` under `_lock`, stores `_cards[card_id]`, inserts the id at index 0 of `_project_cards[project]`, builds a widget, stores `_card_widgets[card_id]`, then `GLib.idle_add`s `_feed_tab.append_card`. Approval cards (`metadata["needs_approval"]`) pass `on_accept` / `on_reject` from `_make_approve_exec_cb`, which calls `handle_approve_exec(card_id, True|False)`. Other cards use `handle_accept` / `handle_reject`. Copy uses `handle_copy(text)` → `Gdk.Display.get_default().get_clipboard().set(text)` on idle. `MAX_LIVE_CARD_WIDGETS = 120`, `KEEP_NEWEST_CARDS = 40`.
- Read `ui/feed/review.py`: `handle_review(self, card_id: str, card_widget=None)` sets `card.reviewed = True` and, only when `card_widget` has `_context_panel`, toggles `panel.set_visible`. A `None` widget sets the flag and does nothing visible. `handle_accept(card_id)` git-commits diff/file cards on a daemon thread (`git_ops.stage_all`, then `git.Repo.index.diff("HEAD")`, then `git_ops.commit`) and calls `_update_card_visual` on idle. `ImportError` on `import git` yields `staged = []` and then the "nothing to commit" return. Other exceptions from `Repo` are logged and return. No project path returns immediately. Non-git cards set `accepted = True` and `update_card`.
- Read `ui/feed/load.py`: `_evict_surplus_card_widgets` and `_load_more` exist. Eviction releases GTK widgets, not `FeedCardData`. Architecture text: widgets are capped because GTK does not return the memory; card data is not capped that way.
- Read `ui/views/chat_surface.py` `ChatSurface._ensure_webview`: `WebKit.WebView()`, `get_settings().set_enable_javascript(live)`, `load_html(doc, "about:blank")`, signals `load-changed`, `load-failed`, `web-process-terminated`. JS is on only for the live tier, and then only with `LiveGuard`. `evaluate_javascript` is async (`evaluate_javascript_finish` in the callback). There is no script-message handler anywhere in this repo. `get_env("NO_WEBKIT")` forces `WebKit = None` before the import. The document is built in Python and loaded whole; the scroll adjustment belongs to the surface's own `Gtk.ScrolledWindow`.
- Read `utils/config.py` usage via the chat surface: `get_env("NO_WEBKIT")` is the switch. Feed must use that same call, not a second env name.
- Architecture owner: `FeedTab` owns the scroll and the toolbar. `FeedHandler` owns card identity and decisions. The view factory owns pixels. A WebKit document is a new view, same split as `render/html.py` (pure) plus `ChatSurface` (GTK).
- Pattern to copy: chat's "one document, Python builds the HTML, `load_html`, fallback when WebKit is absent". Do not subclass `ChatSurface`. Its row deque, live sections, and color freeze are chat-shaped.

---

## 1. Overview

**Problem.** The feed tab is a stack of GTK labels. Pango is the renderer: `escape_for_pango`, a 2,000-character cap because text measurement dominated the main thread, and per-card widgets that the process never fully returns to the OS (`MAX_LIVE_CARD_WIDGETS`). Chat already moved to a WebKit document. SPEC-13 left the feed on Pango on purpose. The feed should be that same kind of surface: one HTML document in the existing feed scroll, toolbar still GTK.

**Solution.** A pure function builds one card's HTML from `FeedCardData`. A `FeedSurface` holds those fragments and loads one document into a `WebKit.WebView` that replaces `_card_container` as the child of the existing `Gtk.ScrolledWindow`. Buttons post an action back to `FeedHandler`, which calls the methods it already calls. Bodies stay escaped text. When WebKit cannot load, `build_feed_card` stays the path that ships.

**Scope**

| In | Out |
|---|---|
| WebKit document of cards inside the existing feed scroll | Running `render_message` / `sanitize_agent_html` on card bodies |
| App-authored card chrome (title, meta, buttons, badges, diff colors) | Agent-authored HTML inside a feed body |
| Click path to `handle_accept` / `handle_reject` / `handle_approve_exec` / `handle_review` / `handle_copy` | Rewriting git accept/reject, auto-accept prefs, or `feed.json` |
| Escaped body, 2,000-character cap, reveal | Deleting `build_feed_card` |
| Context panel as HTML, toggled from Review | Live JS, `LiveGuard`, or `` ```live `` in the feed |
| DOM window using the existing 120 / 40 caps | A second `ScrolledWindow` inside the feed tab |
| GTK card list kept when `NO_WEBKIT` or the WebKit import fails | Restyling the toolbar as HTML |

**Principles.** Card data is untrusted text (git stdout, diffs, shell commands, file paths). The document's only script is a static string compiled into the view module, never a string from `card.body`. GTK CSS does not apply inside the WebView; feed colors move into a document `<style>`. Decisions stay in Python. If the click channel cannot be probed, do not ship dead HTML buttons — keep the GTK list.

---

## 2. Changes by File

### 2a. `models/feed_card.py` — timestamp helper

`_format_timestamp` in `ui/views/feed_card.py` is pure `datetime` math: `None` or under 60s → `"just now"`; under 3600 → `"{mins}m ago"`; under 86400 → `"{hrs}h ago"`; else `"{days}d ago"`. Naive datetimes are treated as UTC. Move that function onto the model as `format_feed_timestamp(ts) -> str` and call it from the view (fallback path) and from the new renderer. No other field changes. `to_dict` / `from_dict` stay.

### 2b. `render/feed_html.py` — NEW, no GTK

```python
def render_feed_card(
    card: FeedCardData,
    *,
    revealed: bool = False,
    context_open: bool = False,
) -> str:
    """One card as an HTML fragment. Body text is escaped. No sanitizer."""
```

```python
def render_feed_document(fragments: list[str]) -> str:
    """Full document. fragments are already render_feed_card output, oldest first
    so the newest card is last (FeedTab.append_card order). Empty list → the
    empty state ('No activity yet' / 'Agent actions will appear here')."""
```

`FEED_RENDERED_BODY_LIMIT = 2000`. A test asserts it equals `ui.views.feed_card.RENDERED_BODY_LIMIT`. The pure module must not import `ui.views.feed_card` (that import pulls GTK).

Rules, all traced from the widget factory:

- Root element: `<article class="feed-card {css_class_for_type(card.card_type)}" data-card-id="{escaped id}">`. Add `feed-card-accepted` or `feed-card-rejected` from `card.accepted`. For `card_type == "agent_action"`, add `feed-card-approval` / `feed-card-running` / `feed-card-complete` / `feed-card-error` from the same metadata tests `build_feed_card` uses (`needs_approval`, else `status` in `running|complete|error`).
- Title, author, path, task id, body: `html.escape(..., quote=True)`. Never `markup=True` and never `render_message`.
- Body cap: if `not revealed` and `len(body) > FEED_RENDERED_BODY_LIMIT`, emit `html.escape(body[:FEED_RENDERED_BODY_LIMIT])` plus a button whose label is `f"… {len(body) - FEED_RENDERED_BODY_LIMIT} more characters"` (same string as `_reveal_label`). The button's `data-feed-action` is `reveal`. Approval cards (`needs_approval`) put `Full command text — view before approving` on the button's `title`; others put `Show the full text`.
- `mono` bodies (`diff`, `git_commit`, `system`) get class `feed-body-mono` and `white-space: pre-wrap` in the document CSS. File events show the escaped path in bold plus the optional body. Task cards show the title and, when set, `ID: {task_id}`.
- Buttons, only when `is_actionable(...)` and `accepted is None`: Review, then Accept or Approve, then Reject or Deny. The Approve/Deny wording is `metadata.get("needs_approval")`, same ternary as the widget. When actionable and already resolved: Review only. Otherwise no action row.
- Every button: `<button type="button" data-feed-action="{action}" data-feed-card="{escaped card_id}">`. `action` is one of `review`, `accept`, `reject`, `copy`, `reveal`. Copy sits in the header, as it does now. `accept` covers both Accept and Approve; the handler branches on `needs_approval`, the document does not.
- Context: if `card.conversation_snapshot is None`, omit the panel. If `context_open` is false, omit it (Review toggles it in). If open and `snapshot_type == "conversation"`, escape each `msg.role` and `msg.text`. If `snapshot_type == "diff"`, escape each line and wrap `+` / `-` lines (not `+++` / `---`) in `<span class="feed-diff-add">` / `feed-diff-del`. This replaces the Pango `foreground=` spans, which are not CSS.
- Document `<style>` is a constant in this module (`FEED_CSS`). Duplicate the colors the Pango path hardcodes (`#6a9955` add, `#f44747` del, `#9b9bab` task id) as classes. Do not expect `ui/styles.py` to reach inside the WebView. Toolbar classes stay in `ui/styles.py` because the toolbar stays GTK.
- The document's `<script>` is a constant. It is not formatted with card data. It reads `data-feed-action` / `data-feed-card` from the clicked button and posts `{action, card_id}`. The post call is filled in after the probe in §2c. Until that probe has a recorded signature, this function still returns the buttons, but `FeedSurface` must not be switched on.

`render_feed_document` wraps fragments in `<!DOCTYPE html><html><head><meta charset="utf-8"><style>…</style></head><body><div id="feed">…</div><script>…</script></body></html>`. Same skeleton as chat's `_document` (`chat_surface.py`, the function that builds `<div id="transcript">`), without chat's row chrome.

**Return values.** Both functions return `str`. They do not raise on a normal card. A bug in `html.escape` is not expected; do not catch `BaseException` here and return `""` — a blank feed hiding a programming error is worse than a traceback in tests. Card fields used above are plain str/bool/dict/datetime. `metadata` can be a non-dict only if a caller bypasses the dataclass default; treat non-dict metadata as `{}` (`meta = card.metadata if isinstance(card.metadata, dict) else {}`) so `.get` is safe.

### 2c. Probe — script message handler (blocking)

This repo never registers a WebKit script-message handler. `evaluate_javascript` is outbound and async. Do not invent the GI call.

Before writing `FeedSurface`, probe in the project venv with WebKit 6.0 (the import `chat_surface.py` uses):

- The user-content manager object on `WebKit.WebView`.
- The register method and its arguments (handler name, world).
- The signal name and the callback arity for a message from the page.
- How the page script addresses that handler (`webkit.messageHandlers.<name>.postMessage` is the usual WebKit shape — confirm it; do not ship it unverified).

Record the signature in a comment at the register site, the way `chat_surface.py` records probe results for `load-failed` and `web-process-terminated`.

If the probe fails, stop. `FeedTab` keeps `_card_container` and `build_feed_card`. Do not enable JavaScript on a feed document that has buttons and no way to deliver the click.

The page script may only post `{action, card_id}` for the five actions. The Python side ignores any other `action`, and ignores a `card_id` that is not in `FeedHandler._cards`.

### 2d. `ui/views/feed_surface.py` — NEW

`FeedSurface` is not a `Gtk.Box` with its own scroll. `FeedTab` already owns `_feed_scroll`. Construct `WebKit.WebView` the way `ChatSurface._ensure_webview` does, with these differences:

- `set_enable_javascript(True)` always for this view. There is no live tier and no `LiveGuard`. The script is ours; card text is escaped.
- Connect `load-changed`, `load-failed`, and `web-process-terminated` so a failed load cannot stick the surface permanently dirty. On failure, log and leave the previous document. Do not fall through to raw HTML.
- `load_html(doc, "about:blank")` is the only load path.

State, all Python-side:

- `_order: list[str]` card ids, oldest first (append order).
- `_html: dict[str, str]` fragment per id.
- `_body: dict[str, str]` the body string last rendered for that id.
- `_revealed: set[str]`, `_context_open: set[str]`.
- `_dirty` + one `GLib.idle_add` rebuild. Same coalescing idea as `ChatSurface._schedule_render`: many `add_card`s in one turn produce one `load_html`.

Methods:

- `set_cards(cards: list[FeedCardData]) -> None` — replace the document (project switch / reload).
- `upsert(card: FeedCardData) -> None` — insert or replace one fragment, preserve order (append if new). If `_body.get(card_id)` is present and differs from `card.body`, discard that id from `_revealed` before rendering (a new body goes back under the 2,000-character cap, matching `update_card_in_place`). Then store `card.body`.
- `remove(card_id: str) -> None`.
- `reveal(card_id: str) -> None` — add to `_revealed`, rebuild. The new body is still `html.escape` of the full stored body, not a raised cap.
- `toggle_context(card_id: str) -> None` — flip `_context_open`.
- `card_ids() -> list[str]`.

Rebuild reads the handler's card objects (the surface holds ids; the handler passes the `FeedCardData` into `upsert` / `set_cards`). Do not keep a second copy of the body.

Scroll. Before `load_html`, read `FeedTab.is_near_bottom()`. The WebView is the child of the existing scrolled window, so the adjustment `FeedTab` already tracks is the one that moves. After `load-changed` reaches the finished event, if the reader was near the bottom, call the tab's existing `schedule_smart_scroll_to_bottom`. If they were not, do not jump. Chat's lesson (`ChatSurface._settle_restore`): WebKit fires several `changed` events and the first height is not final. Reuse the tab's settle path rather than adding a second scroller. Do not wrap the WebView in another `Gtk.ScrolledWindow`.

`get_env("NO_WEBKIT")` true, or the WebKit import failing: `FeedSurface` is not constructed. `FeedTab` behaves as it does today.

### 2e. `ui/views/feed_tab.py`

When the surface exists:

- `scroll.set_child(surface.webview)` instead of `_card_container`. Keep `_toolbar` as the next child of the tab.
- `append_card` / `prepend_card` / `remove_card` / `replace_card` / `show_empty_state` stay for the fallback. Add parallel methods that the handler calls only in HTML mode: `upsert_card(card)`, `remove_card_html(card_id)`, `show_empty_document()`. Do not pass a `Gtk.Widget` into the HTML path.
- `get_vadjustment`, `is_near_bottom`, the map handler, and the toolbar builders stay.

`is_above_viewport(widget)` stays for the GTK fallback. The HTML path does not have a per-card widget, so eviction must not call it. See §2f.

### 2f. `ui/handlers/feed_handler.py` and `ui/feed/load.py`

Mode flag: `self._html_feed` true only after the WebView exists. `add_card` / `add_cards_batch` / the project-open render loop:

- HTML mode: do not call `build_feed_card`. Do not store a widget in `_card_widgets`. `idle_add` `feed_tab.upsert_card(card_data)` (the card object, not a widget). Persist, seq, auto-accept, and `_on_card_added` stay in the same order they run today. Auto-accept still calls `handle_accept` / `_auto_approve_exec_card` after the card is in the document.
- GTK mode: today's widget path, unchanged.

`update_card`: HTML mode replaces the fragment via `upsert` instead of `update_card_in_place`. GTK mode keeps the seam.

New method on `FeedHandler`:

```python
def on_feed_action(self, card_id: str, action: str) -> None:
    card = self._cards.get(card_id)
    if card is None or not isinstance(action, str):
        return
    if action == "copy":
        self.handle_copy(card.body or card.title)
        return
    if action == "review":
        self.handle_review(card_id, None)
        return
    if action == "reveal":
        surface = self._feed_surface
        if surface is not None:
            surface.reveal(card_id)
        return
    if action == "accept":
        if (card.metadata or {}).get("needs_approval"):
            self.handle_approve_exec(card_id, True)
        else:
            self.handle_accept(card_id)
        return
    if action == "reject":
        if (card.metadata or {}).get("needs_approval"):
            self.handle_approve_exec(card_id, False)
        else:
            self.handle_reject(card_id)
        return
    # any other action: ignore
```

`handle_accept` / `handle_reject` / `handle_approve_exec` are unchanged, including their git threading and their "no project path → return" behavior. The click only selects which one runs.

`_update_card_visual` today does `widget.add_css_class` and `update_card_badge`. In HTML mode the card's `accepted` flag is already set by the caller; call `upsert` so the document rebuilds with `feed-card-accepted` / `feed-card-rejected` and the badge text. If `_card_widgets.get(card_id)` is `None`, skip the GTK walk (do not call `update_card_badge` on `None`).

### 2g. `ui/feed/review.py`

`handle_review`: keep the GTK branch (`card_widget._context_panel`). When `card_widget` is `None` and `self._feed_surface` is not `None`, call `self._feed_surface.toggle_context(card_id)`. Still set `card.reviewed = True` first. Still return immediately when `self._cards.get(card_id)` is `None`.

### 2h. Eviction (`ui/feed/load.py`)

`_evict_surplus_card_widgets` stays the GTK algorithm.

HTML mode, new helper called from the same sites (after append, batch, project-open render, `_load_more`):

- Caps stay `MAX_LIVE_CARD_WIDGETS` (120) and `KEEP_NEWEST_CARDS` (40). Do not invent new numbers.
- If `FeedTab.is_near_bottom()` is false, evict nothing. The reader may be looking at older cards, and there is no sync per-card allocation to test. This is a deliberate deviation from `is_above_viewport`, which needs a GTK widget.
- If near the bottom, drop the oldest ids from the document until at most 120 remain, never dropping an id in the newest 40 by `seq_num`. Dropping means `FeedSurface.remove`. `_cards` keeps the data. `_load_more` still knows how to put older cards back.

`evaluate_javascript` is async. Do not block eviction on a DOM rect query.

**Files NOT changed**

- `utils/feed_store.py`, `FeedCardData.to_dict` / `from_dict` — the record on disk is the same.
- `ui/feed/auto_accept.py` — prefs and the toolbar callbacks stay.
- `git_ops` and the body of `handle_accept` / `handle_reject`.
- `ui/views/chat_surface.py`, `render/html.py`, `render/sanitize.py`.
- `build_feed_card` and `update_card_in_place` — fallback and current tests.

---

## 3. Data Flow

Open project, HTML mode: existing load builds `list[FeedCardData]` → `FeedSurface.set_cards` → `render_feed_document` → `load_html` into the WebView that is `_feed_scroll`'s child. Toolbar is untouched.

New card: `add_card` assigns id and seq, stores `_cards`, persists on the background thread as today, `idle_add`s `upsert`. No `Gtk.Box`.

Accept click: page script posts `{action: "accept", card_id}` → probed handler → `FeedHandler.on_feed_action` → `handle_accept` or `handle_approve_exec` depending on `metadata["needs_approval"]` → existing git thread or `update_card` → `upsert` rebuilds that article with the badge.

Review click: `on_feed_action` → `handle_review(card_id, None)` → `toggle_context` → document includes the escaped snapshot.

Copy click: `handle_copy(card.body or card.title)` → idle clipboard `set`. Return value of `set` is ignored today; keep ignoring it.

Reveal: `surface.reveal` → fragment uses the full body, still escaped.

NO_WEBKIT: none of the above. `build_feed_card` → `append_card(widget, card_id)`.

---

## 4. File Change Summary

| File | Change | ~Lines | Risk |
|---|---|---|---|
| `render/feed_html.py` | pure card + document | +250 | med |
| `models/feed_card.py` | `format_feed_timestamp` | +25 | low |
| `ui/views/feed_card.py` | call the moved timestamp helper | ~5 | low |
| `ui/views/feed_surface.py` | WebView, coalesced load, click dispatch | +280 | high |
| `ui/views/feed_tab.py` | host the WebView, keep toolbar and fallback | +80 | med |
| `ui/handlers/feed_handler.py` | `on_feed_action`, branch add/update | +80 | med |
| `ui/feed/review.py` | toggle context when there is no widget | +12 | low |
| `ui/feed/load.py` | DOM eviction when near bottom | +40 | med |
| `tests/test_feed_html.py` | pure renderer | +200 | low |
| `docs/ARCHITECTURE.md` | §3.22a / §3.22c | +20 | low |

The high risk is the click probe and the scroll settle, not the HTML builder. If the probe fails, the rest does not land.

---

## 5. Implementation Order

1. Move `format_feed_timestamp`. Point the view at it. Existing feed card tests stay green.
2. `render/feed_html.py` and `tests/test_feed_html.py`. No WebView. Cover escape, cap, reveal label, Approve vs Accept, informational cards with no buttons, diff colors as classes, `</script>` in a body becoming `&lt;/script&gt;`.
3. Probe the script-message API. Write the signature down. On failure, stop and report. Do not flip `FeedTab`.
4. `FeedSurface` plus `FeedTab` hosting, behind "WebKit imported and probe succeeded". `NO_WEBKIT` tests still build GTK cards.
5. `on_feed_action` and the `add_card` / `update_card` / `_update_card_visual` branches. A unit test can call `on_feed_action` with a fake surface and assert `handle_approve_exec` vs `handle_accept` without a WebView.
6. Eviction helper. Test: near-bottom drops oldest past 120 and never drops the newest 40; not-near-bottom drops nothing.
7. xvfb smoke: one card appears, Accept on a non-git card sets the badge, body text containing `<div>` is visible as text. Then ARCHITECTURE.

---

## 6. Acceptance Criteria

- [ ] With WebKit, the feed scroll's child is a WebView. The toolbar is still a GTK box under it. There is one vertical scrollbar.
- [ ] Card title, body, path, and author render. A body of `<div>x</div>` is visible text, not an element. The document source contains `&lt;div&gt;`, not a raw `<div` from the body.
- [ ] Diff / git_commit / system bodies are monospace. A body longer than 2000 characters shows the reveal control with the exact hidden count. Activating it shows the rest, still escaped.
- [ ] Pending diff card shows Review, Accept, Reject. Pending `needs_approval` card shows Review, Approve, Deny. A `system` card shows no action row. Resolved actionable card shows Review only.
- [ ] Accept on a file card still runs the existing git path (stage, commit, badge). Approve on an approval card still calls `handle_approve_exec(card_id, True)`. Deny calls it with `False`.
- [ ] Review opens the snapshot as escaped text. A second Review hides it.
- [ ] Copy puts `body or title` on the clipboard.
- [ ] `NO_WEBKIT=1` (via `get_env`) builds GTK cards through `build_feed_card`. Accept still works.
- [ ] Project switch replaces the document. Cards from another project are not left in `#feed`.
- [ ] Near the bottom, the DOM holds at most 120 cards and the newest 40 by `seq_num` stay. Scrolled up, a new card does not drop document rows.
- [ ] `feed.json` bytes for a given card are unchanged by this spec (no new persisted fields).
- [ ] The static page script does not read `card.body`. A body that contains `</script>` cannot close the document script.

---

## 7. Edge Cases

| Case | Behavior |
|---|---|
| WebKit import fails | GTK list. No half-built WebView. |
| Probe fails | Same. Do not enable JS. |
| `load_html` raises or `load-failed` | Log, keep the last good document, clear the in-flight flag so the next card can load. |
| `web-process-terminated` | Same recovery as chat: the in-flight flag must clear or every later card sticks. |
| Unknown `action` or unknown `card_id` | `on_feed_action` returns. No git call. |
| `needs_approval` missing / metadata not a dict | Treated as not an approval. Accept/Reject wording. `handle_accept` path. |
| Accept when git has nothing staged | Existing log-and-return. Card stays unaccepted. Document does not show a badge. |
| Accept with no project path | Existing early return. |
| Empty body | No reveal button. Header and footer still render. Copy copies the title (`handle_copy` already uses `body or title`). |
| Snapshot missing | Review sets `reviewed` and toggles an empty set membership; the document has no panel to show. |
| Card updated while revealed | New body re-enters the cap (same as `update_card_in_place` today: a new body resets to truncated). Clear that id from `_revealed` inside `upsert` when the body string changes. |
| Reader not at bottom | No DOM eviction. Scroll position is not forced down (existing `is_near_bottom` contract). |
| Auto-accept | Still `handle_accept` / `_auto_approve_exec_card` after the card is upserted. Buttons disappear on the next rebuild because `accepted` is set or the approval path hides them the way it does now. |

---

## 8. ARCHITECTURE.md Updates Required

- §3.22a `ui/views/feed_card.py`: state that it is the NO_WEBKIT fallback. The primary renderer is `render/feed_html.py` plus `ui/views/feed_surface.py`.
- §3.22c `FeedHandler`: `on_feed_action` is the click entry. `_card_widgets` is empty in HTML mode. The 120/40 cap applies to DOM rows when the reader is near the bottom, and is unchanged for GTK widgets otherwise.
- §3.22b: `format_feed_timestamp`.
- Data-flow §4.14 does not change (persist path is the same). Add one sentence that accept/reject clicks from the document enter through `on_feed_action` and then the existing handlers.
- SPEC-13's "feed stays Pango" line is historical. Point it at this spec rather than editing the old decision into a lie.
