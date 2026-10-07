# SPEC-13: Agent-Native HTML Chat — The Agent Authors HTML

**Date:** 2026-10-06
**Author:** Supervisor (develcakes v2)
**Status:** IMPLEMENTED (SP1–SP4, 2026-10-06; full suite 4563 passed / 3 skipped; PM manual acceptance 4/4)
**Implements:** PM direction 2026-10-06 ("the chat is just an HTML render surface; agents
communicate by outputting HTML") — the Phosphor model
(https://github.com/qsmtco/Phosphor): the model draws the reply as HTML, the WebView
is the canvas.
**Depends on:** SPEC-06 (render pipeline), SPEC-12 (display-keyed surfaces)
**Target branch:** main

---

## 1. Overview

**Problem.** SPEC-06 built the WebKit HTML chat surface but kept the Pango-era trust
model: agent output is treated as *untrusted text* — every message is parsed as
markdown and every literal HTML tag the agent writes is **escaped** (probe-verified:
`<div style="color:red">hi</div>` renders as visible source text). The surface is a
browser that refuses the agent's HTML. The PM's model is different and correct for
local teammates: **the agent is the author**; the reply IS the HTML; the chat surface
is a render target, not a text pane.

**Solution.** A fenced-payload protocol, Phosphor-style:

1. **HTML messages.** When an agent's ENTIRE message is one ` ```html ` fenced block,
   the fence content is the message payload: sanitized with an agent-author policy
   (rich tag set + inline `style` + `class`/`id`/`data-*`) and rendered as HTML —
   never escaped, never shown as source.
2. **Markdown stays.** Any other message (plain text, prose, non-html fences, a
   ` ```html ` fence mixed into prose) takes the EXISTING markdown path unchanged.
   A ` ```html ` fence inside prose stays a documentation code block.
3. **Sanitizer: fail-closed, retuned — not disabled.** `<script>`, event handlers,
   `javascript:`/`file:`/`data:` URIs, iframes/objects/embeds, form action targets:
   still stripped. NEW: inline `style` attributes pass through a **CSS property-name
   allowlist** (nh3 0.3.7 `filter_style_properties` — probe-verified API: a SET of
   property names; all others stripped). `url()`-bearing properties (background,
   background-image, list-style-image, …) are simply NOT on the allowlist, so
   `url(...)` exfiltration/injection vectors die by omission.
4. **Trust boundary moved, not removed.** Untrusted text still exists and is still
   escaped: tool results, fetched web content, and USER rows keep the markdown path
   (escape-first). Only the agent-authored fenced payload gets the author policy.
   The sanitizer remains fail-closed (`except BaseException: return ""`).

**Scope**

| In | Out |
|---|---|
| Whole-message ` ```html ` fence → HTML payload | Per-message JS (JS stays OFF — ruling R2 of SPEC-06) |
| Agent-author sanitize policy (style/class/id/data-*) | Raw-HTML sniffing without fences (post-MVP nicety) |
| CSS property-name allowlist via nh3 | Remote/network message sources (none exist) |
| TextViewFallback parity | Feed cards / other Pango surfaces |
| Agent prompt convention (3 system prompts) | |
| Docs: ARCHITECTURE, SPEC-06/12 notes, README | |

---

## 2. Changes by File

### 2a. `render/sanitize.py` — agent-author policy (additive, no policy weakening of the markdown path)

Keep `sanitize_html` EXACTLY as-is (markdown path consumers: chat_render_handler
welcome, fallbacks). ADD:

```python
# ── Agent-author policy (SPEC-13): the agent is the AUTHOR ──────────────
# Rich vocabulary for agent-authored HTML payloads. Security invariants
# UNCHANGED from sanitize_html: no script/iframe/object/embed/form-action,
# no event handlers, href/src http(s)-only, link_rel forced, fail-closed.

_AGENT_AUTHOR_TAGS = _ALLOWED_TAGS | frozenset({
    # containers
    "div", "section", "article", "header", "footer", "main", "aside", "nav",
    "figure", "figcaption", "details", "summary", "hgroup",
    # text/inline
    "b", "i", "u", "s", "small", "sub", "sup", "mark", "abbr", "cite", "q",
    "time", "wbr", "font", "label",
    # lists (def)
    "dl", "dt", "dd",
    # inert-without-JS interactive (Phosphor-style cards: details/summary
    # toggles NATIVELY, no JS). No <form> — ammonia strips it anyway as a
    # clean_content target unless admitted; do NOT admit (form submissions
    # navigate — keep the surface non-navigating).
    "button",
    # media (src gated http(s) by the SAME url_schemes + attribute filter)
    "img", "video", "audio", "source", "picture",
    # vector (static; JS-off webview cannot script SVG events)
    "svg", "path", "circle", "rect", "line", "polyline", "polygon", "g",
    "defs", "stop", "use", "symbol",
})

# CSS properties an agent payload may set. Deny-by-omission kills every
# url()-bearing property (background/background-image/list-style-image/
# behavior/filter/…): nh3's filter_style_properties takes a SET of allowed
# property names and strips all others (probe-verified nh3 0.3.7 — passing a
# callable raises TypeError).
_AGENT_CSS_PROPERTIES = frozenset({
    "color", "background-color", "font-size", "font-family", "font-weight",
    "font-style", "line-height", "letter-spacing", "text-align",
    "text-decoration", "text-transform", "text-shadow", "white-space",
    "border", "border-color", "border-radius", "border-width", "border-style",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "width", "max-width", "min-width", "height", "max-height", "min-height",
    "display", "flex-direction", "flex-wrap", "gap", "justify-content",
    "align-items", "align-content", "opacity", "overflow", "box-sizing",
    "box-shadow", "cursor", "border-collapse", "vertical-align", "float",
})

def _agent_attribute_filter(element, attribute, value):
    """Same shape as _attribute_filter + author additions:
    style → property-name allowlist (delegate to nh3's own CSS gate by
    returning the value only if every `prop:` token is allowlisted — nh3
    applies filter_style_properties when the property is admitted here),
    class/id → any value (author namespace), data-* → any value."""
    if attribute == "style":
        # nh3 owns property filtering via filter_style_properties; admit
        # the attribute here. (Verify in SP2 probe: if nh3 requires the
        # attribute admitted AND the set passed, this returns value.)
        return value
    if attribute in ("class", "id"):
        return value
    if attribute.startswith("data-"):
        return value
    return _attribute_filter(element, attribute, value)

def sanitize_agent_html(html: str) -> str:
    """Agent-authored HTML payload (SPEC-13): author vocabulary + CSS
    property allowlist. Fail-closed identical to sanitize_html."""
    try:
        return nh3.clean(
            html,
            tags=_AGENT_AUTHOR_TAGS,
            attributes=_agent_attributes_map(),   # style/class/id/data-*
            attribute_filter=_agent_attribute_filter,
            filter_style_properties=_AGENT_CSS_PROPERTIES,
            link_rel="noopener noreferrer nofollow",
            url_schemes={"http", "https"},
        )
    except BaseException:
        return ""
```

`_agent_attributes_map()` admits `{"style","class","id","title","alt","src","href",
"width","height","colspan","rowspan","name"}` (+`data-*` via
`generic_attribute_prefixes=("data-",)`) for ALL `_AGENT_AUTHOR_TAGS`. NO `rel`
(see sanitize.py NEVER-add note). **REGISTER (probe-verified DELTA):** admitting
`attributes` REPLACES ammonia's defaults — `width/height/colspan/rowspan` must be
listed deliberately (same as sanitize.py BUG#6 note).

**style-tag probe (SP1 REQUIRED):** verify whether nh3 admits `<style>` CONTENT
(likely strips it as a non-text-content tag). If content is stripped, DO NOT admit
the `style` tag — register "per-message <style> blocks" as a register item and
rely on inline `style` + existing surface classes for SP1. If nh3 passes style
content through cleanly with tags={"style"}, admit it and pin with a test.

### 2b. `render/html.py` — whole-message fence promotion

```python
def _whole_message_html_fence(text: str) -> str | None:
    """SPEC-13 protocol: the ENTIRE trimmed message is ONE ```html fenced
    block → return the fence content (the agent's HTML payload). Any other
    shape → None (markdown path, untouched)."""
    stripped = text.strip()
    m = re.match(r"^```html[ \t]*\n(.*?)\n?```[ \t]*$", stripped, re.DOTALL | re.IGNORECASE)
    return m.group(1) if m else None

def render_message(text: str) -> str:
    """THE chat entry point (SPEC-13). ONE rule set:
      1. whole-message ```html fence → sanitize_agent_html(payload)
      2. otherwise → render_document(text)   (markdown, unchanged)
    Fail-closed end-to-end: sanitize_agent_html returns "" on internal error;
    render_document already fails closed. An empty payload renders as an
    empty message body (acceptable: an author's empty card)."""
    payload = _whole_message_html_fence(text)
    if payload is not None:
        from render.sanitize import sanitize_agent_html
        return sanitize_agent_html(payload)
    return render_document(text)
```

**Mixed-content rule (explicit):** a ` ```html ` fence that is NOT the whole
message keeps today's behavior — `block_parser` yields it as a `code` block,
`highlight_html` tokenizes it, it renders as a documentation code block. This is
load-bearing: "how to write a div" must stay visible code.

**Guard update:** `tests/test_html_guard_sites.py` pins that every chat call site
composes through `render_document`. Repoint the pin to `render_message` (the
handler will call `render_message`; `render_document` remains its markdown
branch — the guard must accept BOTH names but require at least one on every
chat call site).

### 2c. `ui/handlers/chat_render_handler.py` — repoint the three compose sites

Exactly three message-composition sites (SP6 guard already catalogues them):

| Site | Change |
|---|---|
| `_append_to_surface` `html_fragment = render_document(text)` (`:469`) | → `render_message(text)` |
| `render_async._compose_off_thread` `html_fragment = render_document(text)` (`:605`) | → `render_message(text)` |
| welcome (`render_welcome`, `_WELCOME_MARKDOWN`) | UNCHANGED (markdown by design; class hook preserved) |

The exception fallbacks (escaped raw text on compose failure) stay — they are
the fail-closed backstop for a THROWING compose, and `render_message` cannot
throw (both branches sanitize in try/except).

TextViewFallback parity: its `append_message` strips tags for plain text — an
HTML payload degrades to visible text. IMPROVE: strip tags on the SANITIZED
payload (same call — `_TAG_STRIP_RE` already exists). No code change expected;
pin with a test.

### 2d. `ui/views/chat_surface.py` — surface CSS additions

`_BASE_CSS` gains nothing REQUIRED (agents style their own payloads), but add
sane defaults so unstyled payload elements don't render as black-on-black:

```css
div, section, article, header, footer, aside, nav, figure { display: block; }
img, video { max-width: 100%; height: auto; border-radius: 4px; }
button { background: #2f334d; color: #c0caf5; border: 1px solid #3b4261;
         border-radius: 6px; padding: 4px 10px; }
```

No other change: `append_message` already accepts a sanitized HTML fragment
verbatim. **JS stays OFF** (inert button/details; no navigation).

### 2e. Agent prompt convention — `prompts/system/{coder,debugger,supervisor}.md`

Add one section (identical text):

```markdown
## Communicating in HTML (SPEC-13)

When a reply deserves real formatting — cards, status panels, side-by-side
layouts, callouts, styled summaries — author it as HTML: make the ENTIRE
message one ```html fenced block whose content is the markup. Inline styles
and classes are allowed; <script>, iframes, and event handlers are stripped.
Keep the HTML self-contained (no external assets). For plain conversation,
write normal text — don't fence it.
```

### 2f. Docs (PM requirement: ALL docs updated, no stale claims)

| File | Update |
|---|---|
| `ARCHITECTURE.md` | §Modules/render: add `render_message` + `sanitize_agent_html`, the author policy, the trust boundary statement. §Data Flow/Render: fenced-payload protocol. §Patterns: add "trusted-author / untrusted-tool-result" split. |
| `docs/specs/SPEC-06-R2A-HTML-CHAT.md` | Status note: escape-first contract superseded for agent-authored fenced payloads by SPEC-13; sanitizer remain fail-closed. |
| `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` | Status note: rendering model unchanged; payload policy now SPEC-13. |
| `README.md` | Chat-surface bullet: replace the stale "Markdown → Pango markup" line with the HTML-payload model. |
| `.crabcakes/context.md` | Dated entry. |

### 2g. Tests

| File | Coverage |
|---|---|
| `tests/test_sanitize.py` | `sanitize_agent_html`: script/iframe/onerror/js-URI/data-URI die; div/span/style/class/id/data-* survive; CSS allowlist — `color` survives, `background:url(...)` property stripped; fail-closed (panic → ""); rel forced; http(s)-only href/src. |
| `tests/test_render_html.py` | `render_message`: whole-message fence → rendered HTML (tags present); mixed prose+fence → escaped code block (documentation rule); plain text → markdown path byte-identical to `render_document`; empty fence → empty/safe output. |
| `tests/test_html_guard_sites.py` | Repoint chat-site pin to `render_message` (accept `render_document` as the markdown branch). |
| `tests/test_chat_surface.py` | Default CSS additions present; TextViewFallback tag-strip parity. |
| `tests/test_welcome_html.py` | Welcome still rides markdown path (no regression). |

---

## 3. Data Flow (post-SPEC-13)

**Agent HTML reply:** model output = ` ```html\n<div style="...">…</div>\n``` `
→ `render_message` → fence detect → `sanitize_agent_html` (author policy +
CSS property allowlist) → surface `append_message` → WebKit renders the card.
No escape. No source text.

**Everything else:** unchanged markdown pipeline (escape-first). User rows,
tool results, fetched web text never enter the author policy.

**Degradations:** no WebKit → TextViewFallback strips tags (readable text).
Non-fenced HTML in agent text → escaped code block (visible, diagnosable,
prompt-convention fixes it).

---

## 4. File Change Summary

| File | Change | ~Lines | Risk |
|---|---|---|---|
| render/sanitize.py | author policy + CSS allowlist | +70 | med |
| render/html.py | `_whole_message_html_fence` + `render_message` | +35 | low |
| ui/handlers/chat_render_handler.py | 2 compose sites repoint | +4/−4 | low |
| ui/views/chat_surface.py | default CSS | +8 | low |
| prompts/system/*.md | convention section ×3 | +27 | low |
| tests (4 files) | new policy + protocol pins | +220 | — |
| docs (5 files) | status notes + architecture | +60 | low |

---

## 5. Implementation Order

1. **SP1 — pipeline (pure):** `sanitize_agent_html` + `render_message` + both
   test suites. Probe nh3 style-tag behavior; record verdict in the code.
2. **SP2 — wiring:** 2 compose sites + guard repoint + surface CSS + fallback
   parity tests.
3. **SP3 — convention + docs:** 3 system prompts, ARCHITECTURE, SPEC-06/12
   notes, README, context.md.
4. **SP4 — battery + close-out:** full suite, ruff/pyright vs baseline,
   post-mortem.

---

## 6. Acceptance Criteria

- [ ] Agent message = whole-message ` ```html ` fence → renders as HTML (styled
      card visible; source NOT shown)
- [ ] `sanitize_agent_html`: script/iframe/event-handler/js:/data:/file: all die;
      div/span/section/details/button/svg/style-attr/class/id/data-* survive
- [ ] CSS property allowlist: `color`/`font-size`/flex props survive;
      `background`/`background-image`/any url()-bearing property is stripped
- [ ] Mixed prose + ` ```html ` fence → escaped code block (documentation rule)
- [ ] Plain text messages byte-identical to the old pipeline (no regression)
- [ ] Welcome row unchanged (markdown + welcome-row class)
- [ ] TextViewFallback strips payload tags to readable text
- [ ] JS remains OFF; no navigation-capable elements (no form admission)
- [ ] `sanitize_html` (markdown path) UNCHANGED — its existing suite passes untouched
- [ ] 3 system prompts carry the HTML convention
- [ ] ARCHITECTURE/SPEC-06/SPEC-12/README updated; no stale "escape-first" claims
- [ ] Full pytest green, ruff clean (no NEW findings), pyright clean

---

## 7. Edge Cases

| Case | Behavior |
|---|---|
| Empty fence (` ```html\n``` `) | Empty payload → sanitized "" → empty body (harmless) |
| Unclosed fence | `regex` requires closing fence → None → markdown path (shows as code block) |
| Fence with leading prose ("Here: ```html…") | Mixed rule → code block (NOT promoted) |
| `<script>alert(1)</script>` in payload | Stripped by tag policy; inner text may survive as text — safe |
| `style="background:url(http://x)"` | `background` not in allowlist → property stripped |
| `onclick="…"` | Event handler attrs never admitted → stripped |
| `<a href="javascript:…">` | url_schemes + attribute filter → href stripped, text stays |
| `<iframe src=…>` | Not in tag set → stripped |
| SVG `<use href="#x">` | http(s)-gated like everything else |
| 1 MB payload | `_cap_row_html` byte-budget unchanged (512 KB) |
| Sanitizer panic | `except BaseException: ""` — fail-closed (same contract) |
| Non-agent surfaces (feed cards, diff cards) | Pango path untouched — SPEC-13 is chat-surface only |

---

## 8. ARCHITECTURE.md Updates

- §Modules/render — `render_message` (chat entry) + `sanitize_agent_html`
  (author policy); trusted-author/untrusted-tool-result boundary.
- §Data Flow/Render — fenced-payload protocol replaces "markdown only".
- §Patterns — add the Phosphor-style "agent authors HTML" pattern with the
  security invariants (no JS, no navigation, CSS allowlist).
