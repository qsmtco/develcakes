# SPEC-06 Sub-Phase Plan — R2 Phase A HTML Chat Surface

**Spec:** docs/specs/SPEC-06-R2A-HTML-CHAT.md · **PM sizing directive applies from SP1.**

## Survey results (2026-09-24, this fork)

- **WebKit 6.0 available** (gir1.2-webkit-6.0 + libwebkitgtk-6.0 present; 4.1 also
  installed as fallback) — probe confirmed importable in .venv
- **nh3: spec's `>=2.0` pin DOES NOT EXIST** — max is 0.3.7 (installed). API differs
  from the spec's sketch: no `url_policy` param; use `attribute_filter` (probe-verified
  working for scheme-restriction) + `url_schemes`. Event handlers stripped by default;
  try/except fail-closed verified. **SP1 brief bakes in the corrected API; the spec's
  code sample is superseded.**
- chat_bubble.py: 1,112 lines; **20 referencing files** (7 UI + 13 test) — bigger
  retirement than spec's table implies; retirement gets its own multi-round phase
- chat_render_handler.py: 858 lines (spec said 37KB — close)
- utils/markdown.py: 367 lines (lexer to reuse); syntax_highlight.py: 164 (port source)

## Sub-phases (micro, per directive)

### SP1 — render/sanitize.py + test_sanitize.py (2 files, pure, security-first)
The fail-closed nh3 wrapper with the CORRECTED 0.3.7 API + the full XSS probe battery.
No UI. Gate: battery green incl. fail-closed probes.

### SP2 — render/html.py + render/syntax_html.py + tests (3 files, pure)
markdown_to_html wrapping utils/markdown.py's lexer with an HTML emitter (never
interpolates raw input); syntax_html port (class-based spans, no inline styles).
Gate: pure-function tests; round-trip sanitize(markdown_to_html(x)) never raw.

### SP3 — ui/views/chat_surface.py + bridge test (2 files, WebKit under xvfb)
WebKit 6.0 (4.1 fallback probe at import), lazy per-tab, JS bridge append,
windowed-DOM ring buffer (JS-side, N=500), activity pill stub. Gate: bridge
round-trip + 10k-append windowing test (nodes ≤ 500).

### SP4 — chat_render_handler repoint (1-2 files, may split append/stream)
Repoint append + stream paths to surface API; Pango guard catalog updated for
converted sites. STOP if >4 files.

### SP5 — chat_bubble retirement (sweep + test dispositions, 2-3 rounds)
Import sweep across 20 files; non-transcript uses → equivalent widgets; file deleted
at zero imports; test retirement/rewrite per-file rounds.

### SP6 — Guard tests + close-out
tests/test_html_guard_sites.py (sanitize-at-every-chat-call-site, AST/grep guard
modeled on test_pango_guard_sites.py); Pango guards green for unconverted surfaces;
spec status; ARCHITECTURE.md; post-mortem; push.

## Rules (standing)
Briefs via file with tool budgets; .venv + xvfb-run; probe-before-adjudicate;
falsifiers on fix rounds; zero-new baselines; supervisor owns commits; audit every
code-bearing phase; STOP-on-discovery.
