# SPEC-56: Chat bubble polish — agent-color frame, timestamps, shy copy

**Date:** 2026-10-23
**Author:** Supervisor
**Status:** Draft — for implementation
**Implements:** PM pick 2026-10-23 — Mock-up Variant A ("Feed Echo") + Variant B's shy copy icon and hover lift (canvas `chat-bubble-mockups`, PM-approved)
**Depends on:** SPEC-14 (agent chrome/cards), SPEC-19 (live tier; document JS is content-off unless live), SPEC-17 (isolated-world scroll script)
**Target branch:** main

---

## 1. The approved design (Variant A + two B elements)

Agent bubbles (the `.agent-box` group card):
- **Full border in the agent's color** (the avatar color, same `_sanitize_color`
  value already applied to avatar/name/border-bottom), with a **4px-thick left
  edge** — feed-card DNA. Border base stays `#3b4261` when no color.
- **Timestamp** in the header row (right side): `Oct 23 · 14:02` format,
  small (10.5px), muted `#9b9bab`, `margin-left:auto`.
- **Copy icon button** in the header row, right of the timestamp: the same
  inline-SVG copy icon as the feed (13px, `stroke="currentColor"`), tiny and
  **shy — `opacity:0` until `.agent-box:hover`** (then `.45`, `1` on its own
  hover), ✓-swap for ~1.2s on click, **bridge-gated** (only when the copy
  bridge is live).
- **Gentle hover lift**: `.agent-box` gets `translateY(-1px)` + deeper shadow
  on hover, ~150ms ease transition.

User bubbles (`.agent-box.role-user`): green identity frame (border +
3-4px left rail in `#9ece6a`), tinted background
`rgba(158,206,106,.08)`, right-aligned at `max-width:78%`, same timestamp
treatment. No copy button needed on user bubbles (own text) — allowed but
optional; default: omit.

Live sections (SPEC-19): unchanged — no chrome wrap (existing behavior).

## 2. Discovery (verified anchors)

- `ui/views/chat_surface.py:560-606` — `_BASE_CSS`: current rules
  `.agent-box` (border-left:none), `.agent-chrome`, `.agent-avatar`,
  `.agent-card` (border-top:none, bottom-radius only). The color rides inline
  styles set in `_blocks_html` (lines 706-722): `card_style` currently ONLY
  sets `border-bottom-color` — extend to the full frame (`border-color`), and
  the thick left edge comes from CSS (`border-left-width:4px`), NOT inline.
- `_blocks_html:659-725` — the single markup source shared by the full-document
  and injection paths. Header is `.agent-chrome` (avatar, name) — the stamp
  span and copy button slot in here. Rows do NOT carry timestamps today;
  `_append_row:1602` builds `{"role","html","agent","color","seq"}`.
- `append_message` (~1575-1600) and `append_live` (1619) are the append
  chokepoints; `end_stream` (1734) is the streaming flush. The render handler
  calls `append_message` via `_append_composed` (chat_render_handler.py:141).
- **JS state**: the chat document has content JS OFF unless the live tier is
  on (`_live_js`, `is_live_enabled:1610`; SPEC-17 keeps scroll in the isolated
  world `_DOC_WORLD:623`). A document click listener only runs on the live
  tier. The feed solved this with `window.webkit.messageHandlers.feed` +
  `postMessage` (render/feed_html.py FEED_SCRIPT) and a Python-side handler
  (`ui/handlers/feed_handler.py on_feed_action:1112` via the feed surface's
  message-handler registration). Mirror that pattern.

## 3. Solution

### 3a. Timestamps ride the rows

- `append_message(..., ts: datetime | None = None)` — default `utcnow`-ish
  local time at append; store on the row dict (`"ts": isoformat string`).
  `end_stream` stamps the flush time. `_blocks_html` renders
  `<span class="agent-stamp">{fmt}</span>` in the header, using a small local
  formatter (month-abbrev · HH:MM, e.g. `Oct 23 · 14:02`); missing ts →
  stamp omitted (old persisted/pending rows must not crash rendering).
- ChatRenderHandler passes nothing new (surface defaults) — zero call-site
  churn beyond the surface itself.

### 3b. Frame + lift + shy copy CSS (in `_BASE_CSS`)

- `.agent-card` gains `border:1px solid #3b4261; border-left-width:4px;
  border-radius:8px;` and the inline `card_style` in `_blocks_html` sets
  `border-color:{color}` (full frame) instead of `border-bottom-color`.
  Chrome/card seam: flatten to one visual card (chrome bg transparent,
  card keeps top radius) — match the approved mock-up.
- `.agent-box { transition: transform .15s ease, box-shadow .15s ease; }`
  `.agent-box:hover { transform: translateY(-1px); box-shadow: 0 3px 10px
  rgba(0,0,0,.35); }`
- `.chat-copy-btn` — same visual language as the feed's `feed-copy-btn`
  (transparent, tiny, inline SVG, `.copied` ✓-swap) but `opacity:0` at rest;
  `.agent-box:hover .chat-copy-btn { opacity:.45 }` and
  `.chat-copy-btn:hover { opacity:1 }`.
- `.role-user` rules: green frame + tint + right-align per §1.

### 3c. The copy bridge (live tier only)

- Surface registers a script message handler `"chat"` on its
  UserContentManager when `_live_js` (mirror the feed surface's
  registration + `script-message-received` wiring; handler runs on the GTK
  thread).
- A small document script (injected with the live shims — the
  `_LIVE_RESURRECT_JS` init slot is the existing "install once per document"
  seam) adds ONE delegated click listener: `.chat-copy-btn` → extract the
  owning `.agent-box`'s `.msg-body` textContent → `postMessage` to the
  `"chat"` handler `{action:'copy', text}` → add `.copied` class locally
  (1.2s timer). **Bridge-gated**: class swap only when
  `window.webkit.messageHandlers.chat` exists (the feed-audit lesson).
- Python side: clipboard set (GTK Clipboard, main thread), log on failure.
- **Degradation**: `_live_js` off → `_blocks_html` emits NO copy button (the
  surface passes its live flag; a visible button that does nothing is a
  false affordance). Stamp + frame + lift still render (CSS needs no JS).

### 3d. Guardrails (hard constraints)

- `_sanitize_color` gate unchanged — only 6-hex colors reach inline styles.
- One markup source: `_blocks_html` stays shared by both render paths.
- The click listener is the ONLY new document JS; IIFE, no globals, no
  timers beyond the 1.2s ✓ (cleared on re-click), text posted is the
  group's own body text (user-visible content, no secrets).
- Live sections, scroll scripts, and SPEC-19 machinery untouched.

## 4. Tests

- `_blocks_html` unit tests: frame markup (inline `border-color:{color}`,
  `border-left-width` in CSS not inline), stamp present/formatted/omitted
  without ts, copy button emitted when live-flag on / absent when off,
  `.role-user` treatment classes, hover/lift/shy rules present in
  `_BASE_CSS`, keyframes-free (transform only on :hover).
- Bridge: script contains the gate token sequence (messageHandler check
  before `.copied`), the listener is inside the init-slot IIFE; Python
  handler copies to clipboard (fake clipboard), ignores unknown actions.
- Timestamps: append_message default-stamps; end_stream stamps.
- Existing chat-surface + render-handler suites stay green.

## 5. Acceptance

- [ ] Agent bubble: full agent-color frame + 4px left rail (matches mock-up A)
- [ ] Timestamp right side of header, `Oct 23 · 14:02` style
- [ ] Copy icon: shy (hover-of-bubble reveal), ✓-swap, bridge-gated, works
      on the live tier; absent entirely when live tier off
- [ ] Gentle hover lift, no transform/animation conflicts with :hover
- [ ] User bubbles: green frame + tint + right-align
- [ ] Live sections unchanged; scroll behavior unchanged
- [ ] ruff zero-new, pyright 0 on changed files, full targeted suites green
