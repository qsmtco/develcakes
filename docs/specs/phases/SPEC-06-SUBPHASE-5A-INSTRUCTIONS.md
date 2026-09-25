# SPEC-06 Sub-Phase 5a Instructions — Surface Mount + Session Lifecycle

**Spec:** SPEC-06 §2 retirement (mount half) · **Plan:** SPEC-06-SUBPHASE-5-PLAN.md
**Scope: exactly 3 files** — `ui/views/main_content.py`, `ui/window.py`,
`tests/test_chat_render_handler.py` (additions). Budget ~12, STOP at 14.
**This closes the PM-escalated transcript-blank window — first action.**

## Pre-verified survey facts

- `main_content._close_tab` (:749) and `close_project_tab` (:998) are the lifecycle
  hook sites. `main_content` has 1 chat_bubble import (dies in SP5c, not now).
- The SP4 handler exposes `_surfaces[session_key]` + `close_session(sk)` — orphaned.
- The chat container: chat boxes are per-session Gtk.Box created in main_content
  (`get_chat_box(session_key)` family — verify exact API) — the mount target.

## R1 — MOUNT RULING (simplest thing that closes the window)

The surface mounts INTO the session's chat box: when `_surface_for(sk)` creates a
surface, the handler must hand it to the container. Two options — pick the honest
one and document:
(a) Handler owns mounting via a `set_chat_container_getter(callable)` ctor wire —
    handler calls `getter(sk).append(surface)` on first create. No window.py edits.
(b) window.py/main_content wire explicitly post-construction.
Prefer (a): one wiring point, the handler already tracks session→surface, and SP5c's
chat_bubble deletion doesn't disturb it. If (a), scope becomes 2 files + tests.

- Mount is ONCE per surface (guard: surface already in a parent → skip).
- The surface is a Gtk.Box — packing is trivial. Scroll behavior: the surface owns
  its own scroll (ChatSurface internal — verify it HAS a scrolled window; if not,
  wrap in ScrolledWindow at mount).
- TextViewFallback path mounts identically (same Gtk.Box base) — parity test.

## R2 — LIFECYCLE WIRING

`main_content._close_tab` and `close_project_tab`: before/after removing the page,
call the render handler's `close_session(session_key)` (or fan out per session if
a project tab holds several). Wire via the existing handler reference in
main_content or a setter — avoid new global state. The SP3 destroy contract
(source_remove + guards) handles the rest.

## Tests (~6, xvfb)

1. Mount: render → surface IS in the session's chat box (assert parent chain).
2. Mount-once: second render, same session → no repack (child count stable).
3. Fallback parity: WebKit=None → TextViewFallback mounts identically.
4. close_session on _close_tab: spy — surfaces dict drains; surface._destroyed.
5. close_project_tab: multi-session fan-out drains all.
6. Transcript end-to-end: render_async (threaded) → pump main loop → surface
   contains the sanitized row (the closes-the-window pin — assert via the surface's
   row deque or document HTML).

Falsifiers: remove the mount call → tests 1/6 fail. Remove close_session wire → 4/5 fail.

## Verify (paste ALL):
```
xvfb-run -a .venv/bin/python -m pytest tests/test_chat_render_handler.py tests/test_chat_surface.py tests/test_main_content_tab_switch.py -q
.venv/bin/python -m ruff check ui/views/main_content.py ui/window.py tests/test_chat_render_handler.py
.venv/bin/pyright ui/views/main_content.py 2>&1 | tail -1
```
Baselines: measure main_content/window ruff+pyright FIRST (old files — zero NEW only).

## COMPLETENESS
- [ ] (a)/(b) choice documented; mount-once; scroll verified
- [ ] Both close hooks wired
- [ ] 6 tests incl. the end-to-end pin; falsifiers SAID
- [ ] 3 outputs; deviations flagged
