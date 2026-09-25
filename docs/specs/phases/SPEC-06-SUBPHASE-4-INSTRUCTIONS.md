# SPEC-06 Sub-Phase 4 Instructions — chat_render_handler Repoint (append/stream → ChatSurface)

**Spec:** docs/specs/SPEC-06-R2A-HTML-CHAT.md §2 ("chat_render_handler repoints its
append/stream paths") + §5 step 3
**Parent plan:** docs/specs/phases/SPEC-06-SUBPHASES.md
**MICRO-phase. Scope: exactly 2 files** — `ui/handlers/chat_render_handler.py` +
`tests/test_chat_render_handler.py`. Budget ~12. STOP at 14.

## Survey facts (supervisor 2026-09-24)

- chat_render_handler.py: 858 lines, 34 methods. Imports from chat_bubble:
  `build_role_bubble, process_segments, _clear_crabcards_registry` (:38) +
  lazy ` _build_segment_widget, _build_code_from_markup, _add_action_buttons` (:81).
- Public surface used by callers: `render_async`, `render_sync`, `end_streaming`,
  `get_streaming_text` (+ ctor wiring). SP3's ChatSurface API: `append_message`,
  `stream_delta`, `end_stream`, `set_activity_pill`, `destroy`.
- The handler currently returns Gtk.Widget bubbles to callers (chat_handler appends
  them to a chat_box). **RULING R1 — the surface owns the widget tree now**: the
  handler routes to a per-session ChatSurface (created lazily via
  `create_chat_surface()`), and `render_async`'s `on_bubble_ready` callback fires
  with None (the surface already displayed the message — callers that append the
  bubble to a box must tolerate None; they already do: `if bubble is not None` is
  the existing guard pattern, verified at chat_handler's on_bubble_ready sites).
- **RULING R2 — feature parity checklist**: before repointing, inventory what
  chat_bubble paths do beyond display: forward buttons (on_forward_click), crabcards
  registry, action buttons, error fallbacks, agent color. Map each to: (a) surface
  supports it (pill/stream), (b) dropped for Phase A (documented — forward-click on
  HTML rows becomes a Phase B nicety; the FORWARD toolbar button still works),
  (c) kept via hybrid (bubble for X, surface for Y) — AVOID (c) unless load-bearing;
  the spec says chat_bubble RETIRES for the transcript role in SP5.
- **RULING R3 — pango guard catalog**: grep tests/test_pango_guard_sites.py for
  chat_render_handler entries; converted sites leave the catalog (the test's own
  convention — sites retire as surfaces convert). Do NOT touch non-chat entries.

## Tasks

1. Inventory (R2 checklist) — report the disposition table FIRST in your reply.
2. Repoint: render_async/render_sync → surface.append_message(role, html) where
   html = render_document(text) (the SP2 composed entry — sanitize ALWAYS in the
   path, this is what SP6's guard pins). end_streaming → surface.end_stream.
   Streaming deltas (if the handler has a delta path — check) → surface.stream_delta.
3. Per-session surfaces: dict[session_key → ChatSurface]; lazy create; destroy on
   session end (find the hook — chat_handler session-close or the handler's own
   cleanup) with the destroy-race contract honored (SP3's fixes).
4. Errors: render failure → SPEC-02 error-card path already exists upstream; the
   handler's own fallback (plain text bubble) becomes surface.append_message(role,
   html.escape(text)) — raw text through the pipeline, still sanitized.
5. R3 catalog update.
6. **SP3 RIDERS (4, from the audit — trivial)**: (a) alias pin indent-exact;
   (b) e2e filter-test docstring trims the pyo3 claim; (c) revert the
   TextViewFallback pill-expression churn (match ChatSurface's form);
   (d) Ctrl-C comment clause on the FIX B catch.

## Tests (~8 new/updated in test_chat_render_handler.py; xvfb)

1. render_async → surface.append_message called with SANITIZED html (spy on the
   surface; assert no raw <script> in the html arg)
2. render_sync same, sync path
3. on_bubble_ready fires with None (callers tolerate — pin the contract)
4. end_streaming → surface.end_stream
5. Per-session isolation: two sessions → two surfaces; closing one destroys only it
6. Error fallback: render_document raises (monkeypatch) → escaped-text append, no raise
7. Stream path (if exists): deltas → stream_delta; end → end_stream
8. Pango catalog: converted sites absent; unconverted untouched (run the test file)

Falsifier: bypass the composition (call markdown_to_html directly in the repoint) →
test 1's sanitize assert fails.

## Verify (paste ALL):
```
xvfb-run -a .venv/bin/python -m pytest tests/test_chat_render_handler.py tests/test_chat_surface.py tests/test_pango_guard_sites.py -q
.venv/bin/python -m ruff check ui/handlers/chat_render_handler.py tests/test_chat_render_handler.py
.venv/bin/pyright ui/handlers/chat_render_handler.py 2>&1 | tail -1
```
Baselines: measure ruff/pyright on the handler FIRST (it's an old file — expect
pre-existing findings; zero NEW). Pango guard file: count entries before/after.

## COMPLETENESS
- [ ] R2 disposition table FIRST
- [ ] Repoint + per-session lifecycle + error fallback
- [ ] 4 SP3 riders
- [ ] Catalog updated with count
- [ ] Falsifier SAID
- [ ] 3 outputs; deviations flagged
