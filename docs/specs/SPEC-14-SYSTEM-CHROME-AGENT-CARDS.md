# SPEC-14: System Chrome for Agent Cards

**Date:** 2026-10-06
**Author:** Supervisor (develcakes v2)
**Status:** IMPLEMENTED (SP1–SP2, 2026-10-07; audit CLEAN; close-out battery at commit)
**Implements:** PM direction 2026-10-06 ("consistent boxes for all agents — system
draws the box, agent furnishes the inside")
**Depends on:** SPEC-12 SP1 (grouped `.agent-box`), SPEC-13 (agent-author policy)
**Target branch:** main

---

## 1. Overview

**Problem.** Agents author rich HTML (SPEC-13) but each message re-draws its own
frame; agents that don't (or plain-markdown replies) render with inconsistent
visual identity. Consistency-by-prompting fails structurally: N agents → N header
styles, plus perpetual token cost re-shipping identical boilerplate.

**Ruling (PM-approved):** the PLATFORM draws the card chrome; the agent authors
content. The platform chrome is: card frame + header bar with avatar initial +
agent display name in the agent's stable color. The agent's payload (HTML card or
markdown) renders inside the body region. Zero token cost; one CSS source; applies
to markdown and HTML payloads identically.

**Trust note:** chrome values are PLATFORM-generated (name via `html.escape`,
color via a strict hex gate). Agent content stays inside the body region under
the SPEC-13 author policy — chrome never mixes with payload markup.

## 2. Changes by File

### 2a. `ui/views/chat_surface.py` — chrome rendering + color row storage

1. `append_message(role, html_fragment, agent_name=None, agent_color=None)` —
   both classes store `"color": _sanitize_color(agent_color)` on the row dict.
   Color is FROZEN at append time (deque rows re-render verbatim; no color-map
   lookup at render time; `_document` stays pure).

2. `_sanitize_color(value: str | None) -> str` (module function):
   - `None`/empty → `""`
   - `re.fullmatch(r"#[0-9a-fA-F]{6}", value)` (implemented as FULLMATCH —
     the originally drafted `^…$` form admits a trailing `\n` because
     Python's `$` matches before a final newline; fullmatch enforces the
     intent "ONLY 6 hex digits reach a style attribute") → lowercased value
   - anything else → `""` (drops to CSS default) — security gate: agent_mgr
     colors are platform hex, but the surface must not embed arbitrary
     attribute-sourced strings into a style attribute.

3. `_document(rows)` — grouped agent boxes upgrade from the bare
   `<span class="agent-name">` header to the system chrome:

```python
# inside the same-(agent, role) run (i..j):
color = ""
for k in range(i, j):
    if rows[k].get("color"):
        color = rows[k]["color"]
        break
initial = html.escape((agent[:1] or "?").upper())
style = f' style="border-bottom-color:{color}"' if color else ""
avatar_style = f' style="background-color:{color}"' if color else ""
name_style = f' style="color:{color}"' if color else ""
blocks.append(
    f'<div class="agent-box {box_class}">'
    f'<div class="agent-chrome">'
    f'<span class="agent-avatar"{avatar_style}>{initial}</span>'
    f'<span class="agent-name"{name_style}>{name}</span>'
    f'</div>'
    f'<div class="agent-card"{style}>{body}</div></div>'
)
```

   - Grouping key stays `(agent, role)` — color rides the same grouping (one
     agent = one stable color; first non-empty row color wins, defensive).
   - User rows ("You"): no color threaded (CSS default) — `.role-user` keeps
     its green identity via `_BASE_CSS`.
   - No-agent rows (system/welcome): render bare exactly as today.

4. `_BASE_CSS` additions (chrome defaults; agent color overrides via inline
   style when present):

```css
.agent-chrome { display: flex; align-items: center; gap: 8px;
                padding: 6px 10px; background: #16161e;
                border-radius: 8px 8px 0 0; }
.agent-avatar { width: 24px; height: 24px; border-radius: 50%;
                background: #3b4261; color: #1a1b26; font-weight: bold;
                font-size: 12px; text-align: center; line-height: 24px;
                flex: none; }
.agent-name { font-size: 11px; font-weight: bold; letter-spacing: 1px;
              color: #7aa2f7; }
.agent-card { border: 1px solid #3b4261; border-top: none;
              border-radius: 0 0 8px 8px; padding: 4px 10px 8px; }
.agent-box { border-left: none; margin-bottom: 14px; }
.agent-box.role-user .agent-name { color: #9ece6a; }
.agent-box.role-user .agent-avatar { background: #9ece6a; }
```

   (Replaces the old `.agent-box { border-left: … }` rule — SP1 of SPEC-12's
   border-left becomes the card frame. The old `.agent-box.role-user
   .agent-name` green rule is KEPT and an avatar rule joins it.)

5. `TextViewFallback.append_message` — accepts `agent_color=None` (signature
   parity; plain text keeps the `[name]` prefix — color has no text medium).
   Store it on the row for symmetry but it is inert in text mode.

### 2b. `ui/handlers/chat_render_handler.py` — revive the color resolver

1. `_append_to_surface(..., agent_color=None)` — threads to
   `surface.append_message(..., agent_color=agent_color)` at BOTH call sites
   (`:498` sync path, `:630` async `_append_on_main`; the escaped-raw fallback
   at `:636` passes agent_name only — no color, acceptable).
2. `render_sync` / `render_async` / `end_streaming` — resolve once per render:
   `agent_color = self._resolve_agent_color(agent_name)` when `agent_name` is
   truthy and NOT `"You"` (user echoes keep CSS identity), then thread.
3. **PM requirement — header color == agent-list avatar color, by construction.**
   The agent list tab resolves via `get_color_with_fallback`
   (`agent_list_handler.py:65-90`): Tier 1 `agent_mgr.get_color(name)` →
   Tier 2 `color_for_special_agent(role)` → Tier 3 `#6366f1`. The chat
   resolver (`chat_render_handler.py:687`) is the SAME 3-tier chain (same
   Tier-1 call, same Tier-2 lookup, same Tier-3 constant). Because both
   surfaces consult the same `agent_mgr` color map keyed by agent name, a
   live agent renders the SAME hex in the list avatar and the chat header —
   guaranteed by shared source, not by coincidence.
   - PIN (SP2 test): for each special agent (Coder/Debugger/Supervisor),
     `_resolve_agent_color(name) == agent_list_handler color for the same
     name` — one parametrized test over the registry; a divergence mutant
     (e.g. chat Tier-3 returning a different constant) fails it.
   - Reconciliation (Tier-3 asymmetry): the list's fallback returns `#6366f1`
     ALWAYS; the chat resolver returns None → surface drops to CSS default
     when the caller passes None. SP2 threads the resolver result through;
     for names the list renders with `#6366f1` (unknown agents), the chat
     header would differ (CSS default vs #6366f1). RESOLUTION: SP2 changes
     the chat resolver's Tier 3 to return `#6366f1` (matching the list) —
     then both surfaces agree in all three tiers. The surface gate accepts
     `#6366f1` (6-hex) and renders it inline.
3. `_resolve_agent_color` docstring — UPDATE: revived by SPEC-14 (no longer
   "no longer used"); the 3-tier fallback (agent_mgr → registry → default)
   is the color source. Method body unchanged.

### 2c. `prompts/system/*.md` — one line each

Append to the existing SPEC-13 section: "The platform draws your name card
(header + avatar) around every message — never draw your own header/name
banner; style the content inside."

### 2d. Tests

| File | Coverage |
|---|---|
| `tests/test_chat_surface.py` | chrome DOM (avatar+name+card), color gate (valid hex survives, `javascript:`/`url()`/7-digit/naked-hex dropped), grouping still (agent, role)-keyed, user row green via CSS (no inline), no-agent rows bare, TextViewFallback parity, markdown-inside-chrome unchanged |
| `tests/test_chat_render_handler.py` | color threading: render_sync/render_async/end_streaming resolve + pass; "You" echo gets NO inline color; fallback appends without color |
| kill-proofs | chrome removal → chrome tests red; color-gate removal → gate tests red; resolver bypass → threading tests red (sha-verified restores) |

## 3. Acceptance Criteria

- [ ] Every agent row renders the system chrome: avatar initial + name header
      + card body; agent's stable color tints avatar + name + card top border
- [ ] Color gate: only `^#[0-9a-fA-F]{6}$` survives; all else → CSS default
- [ ] **Chat header color == agent-list avatar color for every agent** (shared
      3-tier source incl. Tier-3 `#6366f1` alignment); parametrized pin test
- [ ] User "You" rows: chrome renders with green CSS identity, no inline color
- [ ] System/welcome rows render bare (unchanged)
- [ ] Grouping unchanged: consecutive same-(agent, role) rows = ONE card
- [ ] Markdown and HTML payloads render inside the chrome identically
- [ ] TextViewFallback: same API, `[name]` prefix, color inert
- [ ] Prompts updated (no self-drawn headers)
- [ ] Full pytest green, ruff 0 new, pyright clean

## 4. Edge Cases

| Case | Behavior |
|---|---|
| Agent name with HTML chars (`<b>`) | `html.escape` on name (existing) + escape on initial |
| Color from agent_mgr malformed | Gate drops → CSS default |
| Unicode initial (é, 日) | `.upper()` + escape; WebKit renders natively |
| Rows same agent different color (re-colored mid-session) | First non-empty color in the group wins; next group re-syncs |
| Empty agent name with color | No agent → bare row (color ignored) |
| Escaped-raw fallback append | No color (name-only) — acceptable, error path |

## 5. Implementation Order

1. **SP1:** surface (`_document` chrome + `_sanitize_color` + CSS + append
   threading + fallback parity) + tests.
2. **SP2:** handler threading (resolver revived, 3 render paths, "You" gate)
   + tests. Prompt line ×3.
3. **SP3:** audit + battery + close-out.
