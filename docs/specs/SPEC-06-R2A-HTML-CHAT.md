# SPEC-06: R2 Phase A — HTML Chat Surface

**Date:** 2026-09-20
**Author:** Supervisor (develcakes v2)
**Status:** Draft — for implementation
**Implements:** docs/proposals/DEVELCAKES-V2-CHANGE-LIST.md §1.2/§5 R2 [SUP-REV staging];
docs/proposals/WEBKIT-RENDER-SURFACE_PROPOSAL.md ("adopt or amend — do not re-invent")
**Depends on:** SPEC-05 (R2 rides R1's consolidated send path), SPEC-03 (ratchet fixed first)
**Target branch:** main

> Architecture compliance: `render/` pure pipeline, nh3 fail-closed sanitizer (ruling #2),
> one webview per chat surface, lazy (ruling #3), JS off by default; Pango guards for
> unconverted surfaces stay green.

---

## 1. Overview

**Problem.** The chat transcript renders through `markdown → escaping → Pango markup →
GtkLabel.set_markup` (242 markup sites repo-wide; chat surface ≈ 90). Pango markup cannot
express rich agent output and was a real injection bug class (47 guarded sites).

**Solution.** Phase A converts **only the chat transcript surface**: new
`render/` pipeline (markdown→HTML→sanitize) + `ui/views/chat_surface.py` (WebKit webview,
windowed DOM, JS off), retiring `chat_bubble.py` for the transcript role. Sanitizer ships
**in the same change**. Feed cards, file tree, toolbar, diff cards stay Pango.

**Scope**

| In | Out |
|---|---|
| render/ package (3 modules) | Any non-chat surface conversion (Phase B = open decision #5) |
| ui/views/chat_surface.py | Feed/file-tree/toolbar/diff conversion |
| chat_bubble.py retirement for transcript | activity_drawer, left_panel |
| Sanitizer suite + XSS probes | |
| Windowed DOM (recent N nodes live) | |

## 2. Changes by File

### render/ (NEW)

**render/html.py** — markdown → HTML pure function:

```python
def markdown_to_html(text: str) -> str:
    """Convert agent markdown to HTML fragment. Pure; no I/O; no GTK."""
```

Implementation: wrap the existing `utils/markdown.py` lexer (16,584 bytes, kept for
Pango consumers) or vendored minimal parser — builder reads utils/markdown.py and
reuses its block/inline token logic with an HTML emitter. **Never** interpolates raw
HTML from input (escaping happens first; only known-safe tags emitted).

**render/sanitize.py** — nh3 wrapper, fail-closed:

```python
import nh3

_ALLOWED_TAGS = frozenset({"h1","h2","h3","h4","h5","h6","ul","ol","li","p","br",
    "hr","blockquote","pre","code","em","strong","del","a","table","thead","tbody",
    "tr","th","td","img"})  # img: data blocked via url policy below

def sanitize_html(html: str) -> str:
    """Fail-closed: any policy violation strips content, never passes raw through."""
    try:
        return nh3.clean(html,
            tags=_ALLOWED_TAGS,
            url_policy=lambda url: url if url.startswith(("http://","https://")) else None,
            link_rel="noopener noreferrer nofollow",
            strip_content=False,
            url_schemes={"http","https"},
        )
    except Exception:
        return ""   # fail closed — empty beats injected
```

Verified against nh3's API (clean(tags=, url_policy=, link_rel=, url_schemes=,
strip_content=) — signature current as of nh3 2.x; builder pins `nh3>=2.0` in pyproject).

**render/syntax_html.py** — port `utils/syntax_highlight.py` token→span-color logic to
HTML class-based spans (colors via CSS in the surface, not inline styles).

### ui/views/chat_surface.py (NEW)

WebKit2 widget via `gi.require_version("WebKit", "6.0")` (fallback 4.1 if 6.0 absent —
builder probes the system lib). One instance per chat tab, lazy-created on first
message, destroyed with tab:
- Base HTML with CSS (message rows, code blocks, diff colors);
- Message append via `WebKit.UserContentManager` script message handler (JS bridge),
  **JS enabled only for the bridge API** — page content itself sanitized (scripts
  stripped by nh3 before insert);
- **Windowed DOM**: JS-side ring buffer keeping last N (default 500) message nodes;
  older nodes removed from DOM (transcript store is the durable record — SPEC-08;
  until then, conversation JSON remains the record);
- Activity status pill (text + class swap only) — the R4 landing zone.

### Retirement: chat_bubble.py (transcript role)

`ui/handlers/chat_render_handler.py` (37,279 bytes) repoints its append/stream paths to
the surface's `append_message(role, html)` / `stream_delta(...)`. chat_bubble.py's
non-transcript uses (if any remain after audit) move to equivalent widgets; the file is
deleted when zero imports remain (`grep -rn chat_bubble` → tests only, then tests
retired/rewritten).

### pyproject.toml

`dependencies += ["nh3>=2.0"]`.

### Guard tests

- NEW `tests/test_html_guard_sites.py` — every chat-surface render call site must pass
  through `sanitize_html` (AST/grep guard, modeled on test_pango_guard_sites.py).
- `tests/test_pango_guard_sites.py` — **stays green**: chat-surface sites are removed
  from its catalog as they convert; unconverted surfaces keep theirs.
- `tests/test_sanitize.py` — XSS probe battery: `<script>`, `<iframe>`, `onerror=`,
  `javascript:` URLs, `file://` links, `data:` URIs, nested-tag smuggling, mXSS
  fragments → all stripped/neutralized.

**Files NOT changed:** utils/gtk_safe_link.py, ui/views/feed_card.py, diff_card.py,
file_tree.py, session_menu.py, main_content.py settings bar (Pango consumers until Phase B).

## 3. Data Flow

Stream delta → chat_render_handler → `render.html.markdown_to_html` →
`render.sanitize.sanitize_html` → surface.append (JS bridge) → DOM append (windowed).
Complete message → same path once, atomic.

## 4. File Change Summary

| File | Change | ~Lines | Risk |
|---|---|---|---|
| render/ (3 files) | new | +600 | med |
| ui/views/chat_surface.py | new | +400 | med-high (WebKit integration) |
| ui/handlers/chat_render_handler.py | repoint | ~150 edits | med |
| ui/views/chat_bubble.py | delete | −43,243 bytes | med |
| pyproject.toml | +nh3 | +1 | low |
| tests (3 new suites + guard catalog updates) | | ~600 | — |

## 5. Implementation Order

1. render/ pipeline + sanitize suite green (pure functions — no UI).
2. chat_surface.py with stub content; JS bridge round-trip test.
3. chat_render_handler repoint; Pango guard catalog updated for converted sites.
4. chat_bubble retirement + import sweep.
5. Windowed-DOM ring buffer test (10k appends → node count ≤ N).
6. Full suite + ruff + pyright.

## 6. Acceptance Criteria

- [ ] Chat renders markdown→HTML in WebKit; code blocks, tables, links work
- [ ] Sanitizer battery passes (script/iframe/event-handler/js-URL/file-URL/data-URI all neutralized)
- [ ] JS off for content (only bridge API exposed); no `allow_universal_access`
- [ ] Pango guard tests green for every unconverted surface
- [ ] New HTML guard test enforces sanitize-at-every-chat-call-site
- [ ] 10k-append windowing test: live nodes ≤ 500, no growth
- [ ] Full pytest green, ruff clean, pyright clean

## 7. Edge Cases

| Case | Behavior |
|---|---|
| Sanitizer internal error | Fail-closed → empty message body + feed card (SPEC-02 pattern) |
| WebKit lib absent at runtime | chat_surface falls back to a plain Gtk.TextView (raw text, no HTML) — app remains usable |
| Huge single message (1 MB code block) | Truncate for render (configurable cap, default 512 KB) with "truncated" marker; full text in transcript |
| Link with unicode tricks (IDN homoglyph) | url_policy allows only http(s); nh3 escapes display text |
| Session with existing Pango-era history | Renders through new pipeline (content is markdown source — safe by construction) |

## 8. ARCHITECTURE.md Updates

§Modules/render + Chat surface — mark implemented; record WebKit version chosen.
