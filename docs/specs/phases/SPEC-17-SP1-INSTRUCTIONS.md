# SPEC-17 SP1 — Phase Instructions: Scroll the Document, Not the Outer Adjustment

**Spec:** `docs/specs/SPEC-17-CHAT-SCROLL.md` (read it in full FIRST — this file is the
phase plan; the spec is the contract).
**Files in scope:** `ui/views/chat_surface.py`, `ui/views/main_content.py` (button seam
only), `tests/test_chat_surface.py` (retarget), possibly `tests/test_chat_render_scroll.py`.
**Out of scope:** SPEC-16, SPEC-15, scratch/, Telegram bridge, `TextViewFallback` scroll
rewrite, `_document` row order, `set_enable_javascript(True)` anywhere.

---

## 0. Baseline (do this BEFORE any edit)

```bash
python -m pytest tests/test_chat_surface.py tests/test_chat_render_scroll.py tests/test_html_guard_sites.py -q
```

Record the pass/fail counts verbatim. Every failure you introduce later is judged against
this baseline. (Full-suite context: 3 pre-existing enforcement env-bleed failures exist
repo-wide; the three files above are expected green on HEAD.)

---

## 1. The bug (why this spec exists — verified 2026-10-07)

`ChatSurface` renders by **full-document reload**: every append calls
`_load_html(_document(...))` → `WebKit.WebView.load_html`. The document scrolls INSIDE
the web view. But the shipped "smart scroll" (2026-10-07 micro-unit) drives
`self._scroll.get_vadjustment()` — the OUTER `Gtk.ScrolledWindow` adjustment. WebKit
does not implement `Gtk.Scrollable`; the outer adjustment is NOT the page scroll. The
fix below moves the follow/preserve logic to the document, via
`evaluate_javascript` (app-side API — allowed; page JS stays OFF, pinned by
`TestChatSurfaceJavaScriptOff` in `tests/test_html_guard_sites.py`).

## 2. The fix

### SP1.1 — Capture the reader's intent BEFORE each load

In `ChatSurface._do_render`, before calling `_load_html`:

1. **No webview yet, or the read fails** → store `(at_bottom=True, y=0.0)` and load.
   A fresh surface's first paint lands at the bottom.
2. **A document is already loaded** → run a read script via `evaluate_javascript`:

```javascript
(function () {
  var el = document.scrollingElement || document.documentElement;
  var y = el.scrollTop || 0;
  var max = Math.max(0, el.scrollHeight - el.clientHeight);
  var atBottom = (max - y) <= 80;
  return JSON.stringify({y: y, atBottom: atBottom});
})();
```

   - The `80` MUST come from `ChatSurface._BOTTOM_THRESHOLD` interpolated as a number —
     never a second literal.
   - Finish with `evaluate_javascript_finish`; read the `JavaScriptCore.Value` with
     `to_string()` (or `to_json` if that returns the object text) → `json.loads`.
     On ANY exception/timeout/malformed payload → `(True, 0.0)`.
   - **C-API reality check (MANDATORY before you write the wrapper):** the WebKit
     `evaluate_javascript(script, length, world_name, source_uri, cancellable, callback)`
     signature is per spec §SP1.1 but was NOT verified against this box's introspection.
     `length` is the script length **in bytes**; `world_name`/`source_uri` may be `None`.
     Verify with a 10-line xvfb probe (WebKit 6.0 is importable on this box) that calls
     `evaluate_javascript` on a real `load_html`'d `WebView`, finishes it, and prints the
     value you read. Paste the probe + its output in your report. **Do not fabricate
     arguments** (steelFramed no-fabricated-APIs rule) — if the real signature differs,
     adapt the wrapper to it and note the deviation.
   - Wrap the call in a small one-line-seam helper on `ChatSurface` (e.g.
     `_document_eval(script, callback)`) — ONE place that touches the WebKit C API, so
     tests monkeypatch the helper. Route BOTH the read (SP1.1) and the apply (SP1.2)
     scripts through it.
3. Store the pair on the surface as the **single pending intent slot**. A newer read
   replaces an older unread intent (coalesced appends → one load → one slot).
   **Only then** call `_load_html(_document(list(self._rows)))`.

### SP1.2 — Apply after the load finishes

Connect `load-changed` ONCE, in `_ensure_webview`, on that web view.
On `WebKit.LoadEvent.FINISHED`:

- Surface destroyed → do nothing.
- Intent at-bottom → issue the bottom script:

```javascript
(function () {
  var el = document.scrollingElement || document.documentElement;
  el.scrollTop = el.scrollHeight;
})();
```

- Intent reading → issue a script that sets `el.scrollTop = <y>`, with `y` formatted as
  a NUMBER by Python (e.g. `f"{y!r}"` of a float). **Never interpolate a string that
  came from the page** into the script.
- Clear the pending intent after issuing the apply script.
- **Do not read the document position at FINISHED to re-derive the intent.** The load
  has already forced the position to the top — reading it there is exactly the bug
  (the code concludes the reader left the bottom and strands them on the oldest message).
- If `_dirty` became true while the load was in flight, schedule one more render after
  the scroll is issued (the row that arrived mid-load must not be dropped).

### SP1.3 — The button

- Add `ChatSurface.scroll_to_latest()`: sets the follow intent to at-bottom AND runs the
  bottom script immediately (if a document is loaded). If no document yet → only store
  the intent so the next `FINISHED` lands at the bottom.
- **Ruling (PM asked; keep this asymmetry):** `scroll_to_latest()` ALWAYS sets the follow
  intent to at-bottom — the user pressed "go to latest," so the next append should
  follow. A reader who wants to stop following scrolls up; the tracker re-arms
  non-follow. No additional yank-back guard beyond the existing tracker re-arm.
- `TextViewFallback.scroll_to_latest()`: set `self._bottom_adj` to
  `upper - page_size` and `_was_at_bottom = True` (today's behavior, no script).
- `MainContent.scroll_chat_to_bottom` (`ui/views/main_content.py:1061`): when the page's
  child is a surface (`crh.surface_for_box(chat_box)` is not None), call
  `surface.scroll_to_latest()` and RETURN — do not also set the outer adjustment for
  that page. Non-surface children keep the existing deferred
  `vadj.set_value(upper - page_size)` path.

### Keep / do-not-touch

- `set_enable_javascript(False)` stays in `_ensure_webview`. Grep-proof required:
  `set_enable_javascript(True)` has ZERO matches in `chat_surface.py`.
- `_document` row order unchanged (oldest-first).
- The existing GTK capture machinery (`_pending_restore`, `_settle_restore`,
  `_RESTORE_STABLE_FRAMES`, collapse guards) **may stay** — it is not the fix, but do
  NOT spend this phase deleting it unless a test you are ALREADY retargeting requires
  it. The GTK tracker (`_was_at_bottom`) keeps feeding `scroll_to_latest()` and the
  fallback path. If removal of GTK-machinery tests is needed to keep the suite green,
  list each in COMPLETENESS as "proposed removal, reason" — the supervisor decides.
- Do not cancel a follow because the post-load GTK adjustment reads 0 — that is
  `_settle_restore`'s live-tracker re-read; it must not gate the document scroll.
- The outer `ScrolledWindow` is NOT the proof of success. Success = the document script.

---

## 3. Tests — retarget `TestSmartScroll` in `tests/test_chat_surface.py`

Monkeypatch the helper seam (SP1.2 note) so tests record the issued script and invoke
the callback. **The spec is explicit: an assertion that only checks
`vadj.get_value() == upper - page_size` after `set_upper` does NOT satisfy this spec —
change those assertions so they watch the DOCUMENT script.**

Required cases (spec table):

| Case | Assert |
|---|---|
| At bottom, then append | The `FINISHED` script sets `scrollTop` from `scrollHeight`; the test FAILS if that script is not issued |
| Reading (`atBottom` false, `y` captured) | The `FINISHED` script sets `scrollTop` to that `y`, NOT to `scrollHeight` |
| Load resets position to 0 before `FINISHED` | Follow still scrolls to bottom — the reset is not a cancel |
| First message, read fails | Intent is at-bottom; `FINISHED` scrolls to `scrollHeight` |
| `scroll_to_latest` | Issues the bottom script and arms the next append to follow |
| Fallback | `test_fallback_autoscrolls_at_bottom` + the reading-preserve fallback test stay green with NO document script |

Retargeting discipline:

- Retarget the OLD `TestSmartScroll` tests ONE AT A TIME (edit → run → next). Some old
  tests were pinning the GTK-machinery contract (cross-frame settle, collapse guard,
  two-consecutive-stable) — those contracts are NOT invalidated by this spec unless the
  GTK machinery is removed. If a GTK-machinery test conflicts with the new document
  path, list it in COMPLETENESS (proposed removal + reason) rather than silently
  deleting or neutering it.
- The tautology warning (from the last loop's audit): a test whose only new assertion is
  "monkeypatched value equals what the monkeypatch set" proves nothing. Assert the
  SCRIPT STRING (the bottom script or `scrollTop = <y>`) that the FINISHED handler
  issues.

## 4. Verification (paste ALL outputs in your report)

```bash
python -m pytest tests/test_chat_surface.py tests/test_chat_render_scroll.py tests/test_html_guard_sites.py -q
ruff check ui/views/chat_surface.py ui/views/main_content.py tests/test_chat_surface.py
pyright ui/views/chat_surface.py ui/views/main_content.py 2>&1 | tail -5
grep -n "set_enable_javascript(True)" ui/views/chat_surface.py   # expect: no matches
grep -c "scroll_to_latest" ui/views/chat_surface.py ui/views/main_content.py
```

NO NEW ruff findings vs baseline (note: chat_surface.py has PRE-EXISTING findings —
report the count before/after; zero NEW is the bar).

## 5. Report format (mandatory)

- Files changed with line numbers.
- The xvfb C-API probe + its raw output.
- All verification command outputs verbatim.
- COMPLETENESS checklist — one line per edit above (SP1.1 read/seam, SP1.1 no-webview
  path, SP1.2 connect + both branches + clear + re-render kick, SP1.3 button +
  main_content seam, each required test case), `[x]`/`[not done]` + evidence.
- Related-bug scan (steelFramed Step 6.6): list any adjacent issues you noticed but did
  NOT fix in this phase.

Word marker for this delegation: **please write**.
