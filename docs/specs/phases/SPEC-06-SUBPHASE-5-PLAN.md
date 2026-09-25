# SPEC-06 Sub-Phase 5 Plan — chat_bubble Retirement + Mount/Lifecycle/Crabcards

**Spec:** docs/specs/SPEC-06-R2A-HTML-CHAT.md §2 retirement + SP4's hard items
**PM sizing directive: this splits into 3 micro-rounds** (survey below).

## Survey (2026-09-24)

- Production chat_bubble imports: only 3 left — chat_render_handler (:38 lazy block),
  main_content (1), window (1). The handler's is dead post-SP4 (verify: the lazy
  import block at :81 — if unreferenced now, deletion is trivial).
- Test blast: 8 files / ~52 refs — but test_presentation_injection, test_streaming,
  test_gtk_safe_link test UTILS consumed via bubbles; test_chat_heading/task/terminal
  test chat_bubble internals directly (retire or repoint per-file).
- SP4's hard items land HERE: (3) surface mount (window/main_content wiring — the
  transcript-blank window closes), (4) close_session hook (main_content._close_tab
  :749 + close_project_tab :998 are the sites), (5) crabcard metadata stamping →
  ARH's two add_cards_batch blocks (:2072/:2112).

## Rounds

### SP5a — MOUNT + LIFECYCLE (closes the transcript-blank window; PM item)
window.py/main_content.py: surface into the chat container (per-session), swap
the SP4 unmounted path; wire close_session at _close_tab/close_project_tab.
2-3 files + test updates. Gate: transcript renders end-to-end (spy-level test
that the surface is IN the container); close-tab → surfaces dict drains.

### SP5b — CRABCARD METADATA (BUG #5) + the handler's dead chat_bubble import removal
ARH :2072/:2112: stamp session_key/tab_key at card construction (window's old
callback dies). chat_render_handler's lazy-import block deleted if unreferenced.
2-3 files. Gate: crabcard → metadata carries session/tab key; snapshot linkage
probe green.

### SP5c — chat_bubble.py DELETION + import sweep (window/main_content last refs)
Delete the file; sweep the 2 remaining production imports; test dispositions
per-file (retire bubble-internals tests: test_chat_heading/task/terminal;
repoint utils-tests to direct calls; test_pango_guard catalog entries for
bubble sites retire with it). 3-4 files, may split c1 (delete+sweep) / c2 (tests).

### SP6 — guards (test_html_guard_sites) + packaging (nh3/pygments pins, venvPath,
pyproject include) + close-out (spec status, ARCHITECTURE.md, post-mortem, PUSH).

## Rules: standing. SP5a opens with the PM-escalated item — first action.
