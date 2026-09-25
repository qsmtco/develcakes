# SPEC-06 Sub-Phase 3 Instructions — ui/views/chat_surface.py (WebKit) + Sanitizer class= Ruling

**Spec:** docs/specs/SPEC-06-R2A-HTML-CHAT.md §2 chat_surface.py + §6 windowing
**Parent plan:** docs/specs/phases/SPEC-06-SUBPHASES.md
**MICRO-phase. Scope: exactly 3 files** — `render/sanitize.py` (class= policy —
small, sanctioned rider), `ui/views/chat_surface.py` (NEW), `tests/test_chat_surface.py`
(NEW). Budget ~14 (WebKit work is heavier; STOP at 16 with a state report).

## Pre-verified / pre-ruled

- WebKit 6.0 importable in .venv (probe); 4.1 fallback per spec §2 edge table
- **RULING (from SP2 discovery): the sanitizer gains `allowed_classes`** —
  `"code": {"lang-python", ...}` won't scale; use PREFIX form if nh3 supports it,
  else an explicit-set via attribute_filter for class on code/span/pre with values
  matching `^(tok|lang)-[a-z0-9+#-]*$`. Probe which form works FIRST, report, use it.
  This unblocks the styling hook without opening arbitrary class names.
- The sanitizer change is IN this phase's scope (2 lines + 2 tests) because the
  surface's own CSS depends on it.

## Task — chat_surface.py

```python
class ChatSurface(Gtk.Box):
    """WebKit-hosted chat transcript (SPEC-06 R2A). One per chat tab, lazy."""
    def __init__(self): ...                    # webview lazy — created on first append
    def append_message(self, role, html, agent_name=None): ...
    def stream_delta(self, session_key, text, agent_name=None): ...  # buffering path
    def end_stream(self, session_key, agent_name=None): ...
    def set_activity_pill(self, state: str): ...  # "idle"|"thinking"|"tool"|"error" — text+class swap
    def destroy(self): ...                    # unrefs webview
```

Key requirements (spec §2):
1. `gi.require_version("WebKit", "6.0")` try, fallback `"4.1"`, else
   **Gtk.TextView fallback** (spec §7: app remains usable — raw text, no HTML).
   The fallback is a plain class with the same API (append/stream/end/pill) —
   ~40 lines, keeps call sites uniform.
2. **JS OFF for content**: `webview.get_settings().set_enable_javascript(False)`
   — then the spec's JS-bridge append becomes **load_html/WebKit.UserContentManager
   evaluate** — SURVEY ADJUSTMENT: with JS off, there IS no JS bridge. Use
   `WebView.load_html(full_document)` on first message + **incremental DOM updates
   via run_javascript** — NO. JS off means no run_javascript either. **RULING: the
   windowed DOM moves to the PYTHON side** — ChatSurface keeps a bounded deque of
   rendered rows (default 500), re-renders the document (load_alternate_html with
   full HTML) on append. Coalesce re-renders via idle_add + dirty flag (max ~10/s
   under streaming). This satisfies §6's 10k-append test (live nodes ≤ 500 by
   construction — the deque IS the window) without JS. Document this deviation
   from the spec's JS-bridge sketch in the module docstring (JS-off won).
3. Windowing: deque(maxlen=N configurable via arg, default 500). Spec §7 huge-message
   cap: truncate html > 512KB with "[truncated]" marker before append.
4. Activity pill: Gtk.Label overlaid or packed at top — text + CSS class swap only.
5. CSS: base HTML template with message-row, code-block, tok-* classes (colors via
   CSS classes per SP2's no-inline-styles contract).

## Tests — tests/test_chat_surface.py (~12, xvfb-run)

1-3. Fallback path (no WebKit env needed): TextView fallback appends, streams, pills.
4. WebKit path (skipif WebKit import fails): append_message renders — load_html
   called with escaped+sanitized content (can assert via get_uri/title or a
   monkeypatched loader).
5. 10k-append windowing: 10_000 appends → deque len == 500, document rebuild bounded
   (assert rebuild count coalesced < 10_000).
6-7. Stream: deltas buffer → end_stream flushes one atomic message (not per-delta rows).
8. Pill states cycle text/class.
9. Huge-message truncation marker.
10. destroy() twice-safe.
11. Sanitizer class= ruling test: render_document of fenced python code now KEEPS
    lang-/tok- classes (the SP2 discovery, now green).
12. Class allowlist stays closed: `class="evil"` on arbitrary input still stripped.

Falsifier: remove the deque bound → test 5 fails (rebuild grows).

## Verify (paste ALL; xvfb where GTK):
```
xvfb-run -a .venv/bin/python -m pytest tests/test_chat_surface.py tests/test_render_html.py tests/test_sanitize.py -q
.venv/bin/python -m ruff check render/sanitize.py ui/views/chat_surface.py tests/test_chat_surface.py
.venv/bin/pyright ui/views/chat_surface.py 2>&1 | tail -1
```
Baselines: sanitize.py ruff 0 (keep); new files 0/0 (WebKit import may pyright-artifact —
report like SP1's nh3).

## COMPLETENESS
- [ ] class= policy probe result reported + implemented
- [ ] JS-off ruling documented in docstring
- [ ] 12 tests green; falsifier SAID
- [ ] 3 outputs; deviations flagged (STOP at 16 calls)
