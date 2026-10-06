# SPEC-12: Project-First Group Chat — One Surface Per Project

**Date:** 2026-10-05
**Author:** Supervisor (develcakes v2)
**Status:** IMPLEMENTED (SP1–SP7, 2026-10-05; full suite 4469 passed / 0 failed)
**Implements:** architecture.md §Modules/Chat surface (revision), PM direction 2026-10-05
**Depends on:** SPEC-06 (HTML chat surface — this revises its surface-keying model)
**Target branch:** main

> Architecture compliance: develcakes is a **project-first** PDE. The chat surface is
> the **project group chat**, not a per-agent chatbot. One HTML surface per project
> tab; every agent renders into it as a named HTML box. The per-agent-session surface
> path is removed.

---

## DISCOVERY

- **Read `ui/handlers/chat_render_handler.py`**: [`ChatRenderHandler`] keys surfaces
  `self._surfaces: dict` **by session_key** (`_surface_for`, `:282-300`). `render_sync`
  (`:662`) and `end_streaming` (`:754`) accept `mount_key` — the *display* box key
  (project) — but the surface CACHE stays session-keyed. **`render_async` (`:545`)
  does NOT accept `mount_key`** (signature: `role, text, session_key, on_bubble_ready,
  on_forward_click, on_error, agent_name, agent_color`) and its append calls
  `_surface_for(session_key)` with no mount key (BUG#4). `_append_to_surface` (`:432`)
  tombstones on `session_key` (`:450-451`); `close_session` (`:338`) is session-keyed.
  `surface_for_key` (`:272`) — **zero production callers** (see BUG#6). `append_message`
  already takes `agent_name`.
- **Read `ui/window.py:260-269`**: the activity pill resolves via
  `self._main_content.activity_pill()` (a single shared `ActivityPillLabel`) — it does
  NOT consult the surface cache. `surface_for_key` is dead in production (BUG#6).
- **Read `ui/views/chat_surface.py`**: [`ChatSurface`] is "One per chat tab, lazy
  webview." Each surface owns its own `Gtk.ScrolledWindow` (`_make_owned_scroll`,
  `:127-137`; `self._scroll` `:162`). `_document()` renders each row as
  `<div class="message-row role-X"><span class="agent-name">{name}</span><div
  class="msg-body">{html}</div></div>` (`:89-105`) — **per-agent name headers already
  render in HTML.** Deque windowed at 500.
- **Read `ui/views/main_content.py`**: `create_chat_tab(session_key, agent_name)`
  (`:507`) creates ONE tab per session_key; `_tab_chat_boxes[page_idx] = chat_box`;
  `get_chat_box_for_session(session_key)` (`:1002`) matches the tab whose
  `_tab_sessions` value == session_key. A project tab's session_key is `project:<name>`.
- **Read `ui/window.py`**: (`:193-207`) **auto-opens a tab per agent** with
  `auto_open=True`; (`:536`) project open creates `project:<name>` tab; (`:1450`)
  `_on_agent_selected` creates an agent tab on Chat-button click.
- **Read `ui/handlers/agent_runtime_handler.py`**: `_resolve_chat_box` (`:1593`) and
  `_resolve_mount_key` (`:1611`) **prefer the agent's own tab** (`get_chat_box_for_session(session_key)`)
  before falling back to `project:<name>` via `AgentRoutingTable`.
- **Read `ui/handlers/chat_handler.py`**: `on_send` (`:188`) — project tabs fan out to
  all members (`:404-416`); non-project tabs send to one agent (`:416`).
- **Read `ui/handlers/forward_handler.py`**: `forward_to_agent` (`:130`) creates/
  opens the target agent's tab and appends the forwarded bubble there.
- **Read `ui/handlers/agent_list_handler.py`**: agent list = add/edit/remove + member
  toggle (`on_agent_chat`, `on_agent_toggle`) — **retained** for member management.
- **Read `models/routing.py`**: `AgentRoutingTable.get_project(session_key) -> str|None`.
- **Architecture owner:** `ChatRenderHandler` (surface lifecycle) + `MainContent` (tab
  model) + `AgentRuntimeHandler` (display-key resolution).
- **Existing patterns:** `mount_key` threading (SPEC-06 FIX 2/FIX 7) already routes
  replies to a project box; this spec makes that the DEFAULT path (private views are the
  sole opt-in exception — §2f).

**The defect (PM-observed):** in a project tab, each agent's output lands in its own
`ChatSurface` (each with its own `ScrolledWindow`), stacked in the project box → N
agents = N scrollbars, not one group transcript. Root cause: surfaces are keyed by
session_key and mounted into the project box, so N session-keyed surfaces coexist in
one box.

---

## 1. Overview

**Problem.** The chat surface model is per-agent-session. In a project tab the
surfaces stack (one scrollbar per agent). develcakes is a project-first tool; the
surface must be the **project group chat**.

**Solution.**
1. **One surface per project tab.** Surface cache keyed by the DISPLAY key
   (`project:<name>`), not session_key.
2. **Every agent turn renders into the project surface** with its `agent_name` set →
   named HTML box (already supported by `_document`).
3. **Remove the per-agent-session surface path**: no auto-opened agent tabs; the
   display-key resolver never prefers an agent tab.
4. **Agent list stays** as member management (add/edit/remove + member toggle).
5. **`/ask` + `/delegate` keep a private view** (PM decision B) — the ONE remaining
   agent-keyed surface, explicitly opt-in, never auto-opened.

**Scope**

| In | Out |
|---|---|
| Surface keyed by display key (project) | Feed / file-tree / toolbar (still GTK) |
| Remove auto-open agent tabs | Feed card pipeline (unchanged) |
| `_resolve_mount_key` → project key always | Activity pill internals (unchanged) |
| Agent list = member management | Transcript store schema (unchanged) |
| `/ask`+`/delegate` private view retained | Group-chat "addressing"/JEV (post-MVP) |
| Tests | Multi-project simultaneous-tab redesign |

---

## 2. Changes by File

### 2a. `ui/views/chat_surface.py` — grouped agent boxes (rendering model)

**Current:** each message row is `<div class="message-row role-X"><span
class="agent-name">NAME</span><div class="msg-body">HTML</div></div>`.

**Change:** keep per-row rendering, but **group consecutive rows from the same
agent into ONE `.agent-box` with a single `.agent-name` header**, so each agent's
consecutive output reads as one named box:

```python
def _document(rows: list[dict]) -> str:
    """Render rows as grouped agent boxes — consecutive rows with the same
    agent collapse under ONE header (SPEC-12). Rows with no agent name render
    bare (system/welcome)."""
    blocks = []
    i = 0
    n = len(rows)
    while i < n:
        row = rows[i]
        agent = row.get("agent") or ""
        if not agent:
            blocks.append(
                f'<div class="message-row role-{row["role"]}">'
                f'<div class="msg-body">{row["html"]}</div></div>'
            )
            i += 1
            continue
        # collect the run of same-agent rows — key on (agent, role) so an
        # agent displaying the literal name "You" (role "agent") can never
        # merge with the user's own rows (role "user") (SP1-audit BUG#1).
        role = row.get("role") or "system"
        j = i
        while (j < n and (rows[j].get("agent") or "") == agent
               and (rows[j].get("role") or "system") == role):
            j += 1
        body = "".join(
            f'<div class="message-row role-{rows[k]["role"]}">'
            f'<div class="msg-body">{rows[k]["html"]}</div></div>'
            for k in range(i, j)
        )
        name = html.escape(agent)
        # SP1-audit BUG#1: derive the box class from the row's ROLE, not the
        # display-name string — the name is not a user/agent discriminator.
        box_class = "role-user" if role == "user" else "role-agent"
        blocks.append(
            f'<div class="agent-box {box_class}">'
            f'<span class="agent-name">{name}</span>{body}</div>'
        )
        i = j
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{_BASE_CSS}</style></head><body>"
        + "".join(blocks) + "</body></html>"
    )
```

CSS: add to `_BASE_CSS`:
```css
.agent-box { border-left: 2px solid #3b4261; padding-left: 8px; margin-bottom: 12px; }
/* SP1-audit BUG#3 correction: the box element ITSELF carries role-user, so the
   header <span class="agent-name"> is still a DESCENDANT of a .role-user element
   and the OLD `.role-user .agent-name` rule STILL matches. This box-level rule is
   therefore REDUNDANT (kept for explicitness/intent; either rule styles the user
   header green). The earlier "sibling / stops matching" rationale was a mis-model
   of CSS ancestor matching. */
.agent-box.role-user .agent-name { color: #9ece6a; }
```

**BUG#5 fix — user rows.** The grouping key is `(row.get("agent"), row.get("role"))`.
A run of the user's own rows (`agent="You"`, `role="user"`) collapses under ONE "You"
box, matching agent behavior. State explicitly: **user messages group like agents.**
Emit `role-user` on the `.agent-box` **when the grouped row's `role` is `"user"`**
(`box_class = "role-user" if role == "user" else "role-agent"`) so the CSS rule above
applies. **SP1-audit BUG#1 (fix applied):** deriving the class from the display-name
string `"You"` is unsound — an agent whose display name is literally "You" (the
agent-builder path does not reserve the name) would be misclassified as the user AND,
before this fix, would merge with the user's adjacent rows. The row's normalized
`role` field (`append_message` stores only `user`/`agent`/`system`, chat_surface.py:241)
is the authoritative discriminator.

(The "You" role string is set by `render_sync`/`render_async(agent_name="You")` —
verify at implementation; the surface maps roles via `_surface_role`.)

**`TextViewFallback`** keeps its `[name] line` prefix (already `:313`) — no change
beyond parity.

### 2b. `ui/handlers/chat_render_handler.py` — surface keyed by display key

**KEY DOMAIN (BUG#1 fix — this is the load-bearing rule of the whole spec):**
after this change there are exactly **two key domains**, and every structure belongs
to exactly one:

| Structure | Key domain | Rationale |
|---|---|---|
| `_surfaces` (surface cache) | **display key** | one surface per project box |
| `_surfaces_by_parent` (box-id index) | box identity | unchanged (id-keyed) |
| `_closed_sessions` (tombstones) | **display key** | a closed project box tombstones its box key |
| `_mounted_box_keys` | **display key** | box-key metadata |
| `_welcome_shown` | **display key** | welcome rides the surface lifetime |
| `_mount_misses` (eviction counter) | **display key** | counts misses for the surface being mounted |
| `_stream_text`, `_streaming`, `_stream_role` | **session key** | concurrent agent streams buffer per agent |
| `_ReentrancySet` | **session key** | per-agent render in-flight guard |

(BUG#9: the earlier draft listed a `_changed` row — no such attribute exists in
`ChatRenderHandler`; welcome gating is `_welcome_shown` only, already listed above.)

**The one rule:** every method that touches a surface-lifecycle structure resolves
`display_key = mount_key or session_key` ONCE at the top and uses that variable
throughout. Session-keyed streaming stays session-keyed.

**`surface_for_key(session_key)` (BUG#9):** the method NAME and arg are legacy; the arg
is now a **display key** (it does `self._surfaces.get(session_key)` against a
display-keyed cache). Update its docstring to say so, and update its one test caller
(see §2b-testnote below). No production callers exist (verified).

**`_append_to_surface` (BUG#1a fix):** the tombstone check MUST use the display key,
not the raw session key:

```python
    def _append_to_surface(self, role, text, session_key, agent_name=None,
                           mount_key=None):
        ...
        display_key = mount_key or session_key or ""
        if self._closed_sessions.get(display_key):     # BUG#1a: display key
            return
        surface = self._surface_for(session_key, mount_key=mount_key)
        if surface is None:
            return
        surface.append_message(_surface_role(role), html_fragment, agent_name=agent_name)
```

**`_surface_for` (BUG#1b fix) — fully respecified, no "…kept" prose:**

```python
    def _surface_for(self, session_key, mount_key=None):
        display_key = mount_key or session_key
        surface = self._surfaces.get(display_key)
        if surface is None:
            surface = create_chat_surface()
            self._surfaces[display_key] = surface
        mounted = self._mount_surface(display_key, surface, display_key)
        if mounted:
            self._mount_misses.pop(display_key, None)
        elif surface.get_parent() is None and self._container_getter is not None:
            self._mount_misses[display_key] = self._mount_misses.get(display_key, 0) + 1
        if (not mounted
                and surface.get_parent() is None
                and self._container_getter is not None
                and self._mount_misses.get(display_key, 0) >= self._MOUNT_MISS_LIMIT):
            surface.destroy()
            self._surfaces.pop(display_key, None)          # BUG#1b: no KeyError
            self._mount_misses.pop(display_key, None)
            self._welcome_shown.discard(display_key)
            _logger.warning(...)
            return None
        return surface
```

(`_mount_surface` signature stays `(session_key, surface, mount_key)` — the first arg
is now the display key; rename it `key` for clarity. `drop` semantics unchanged.)

**`close_session(session_key, box=None)` (BUG#1 fix):** callers pass a box; the
fan-out loop collapses to: destroy the surface whose display key == this box's key,
plus any surface mounted in the passed box (defensive). Tombstone the **display key**.
Because one box now holds one surface, the fan-out is `N=1` in the normal case.

**`render_welcome`:** gate on the display key (the box), not the session key.

**`render_async` (BUG#4+#10 fix):** add `mount_key=None` to the signature and thread it
through `_append_on_main` → `_append_to_surface(session_key, mount_key=mount_key)`.
Then **every `render_async` call site in `chat_handler.py` (`:227,249,277,326,354,381`)
must PASS `mount_key=self._agent_runtime_handler._resolve_mount_key(session_key)`** (for
agent-content callers) or `session_key` when it is already the display key (the "You"
echo in a project tab). Non-emptiness: threading a param no caller sets is not a fix
(BUG#10) — the call sites are part of this change. Add a guard comment in
`render_async`'s docstring: *"pass mount_key for any non-project caller; the surface
cache is display-keyed."*

**`surface_for_key` (BUG#6+#9 fix):** NO production callers (verified). Keep
`return self._surfaces.get(session_key)` (now display-keyed); update its DOCSTRING to
say the arg is a display key. **BUG#8 test scope (widened — spec pre-flight):**
`tests/test_activity_pill_adapter.py` drives `surface_for_key("agent:coder")` and
asserts a surface — that asserts the RETIRED model (agent-keyed surfaces). Rewrite
its project-tab test to the new contract: one display-keyed surface
(`project:<name>`); `surface_for_key("agent:coder")` returns None. This is a
semantic rewrite, not a one-line update — state it in §5 and the summary.
**Also asserting the retired session-keyed model (pre-flight-verified), all in scope:**
`tests/test_chat_render_handler.py` (`:733`, `:958-959`, `:977-984`, `:1050-1056` pin
`_closed_sessions` on agent keys) and `tests/test_welcome_html.py` (`:192` writes
`_closed_sessions["sk"]`). The "+240" estimate in §4 must cover these rewrites.

### 2c. `ui/handlers/agent_runtime_handler.py` — display key = project (turn-scoped routing)

**BUG#2 + BUG#11 fix — the resolver rule (R5 REV 3).** A **session-scoped** privacy
mark is wrong: it would mute a member in the group surface for as long as its private
tab is open, contradicting AC1/AC4. The reply surface is decided **per send**.

**Mechanism — turn-scoped reply target (BUG#17/#18 resolved):**
- ARH gains `self._turn_reply_target: dict[str, str] = {}` (session_key → display key)
  in `__init__`.
- `send_to_special_agent(session_key, text, reply_target: str | None = None)` — new
  param. **On ENTRY, ALWAYS set the slot (BUG#18 — no "only when supplied"):**
  ```python
        # BUG#26: placed AFTER the two early-return guards (unregistered
        # agent; no active project) so the slot is set only for sends that
        # will actually run a turn.
        self._turn_reply_target[session_key] = (
            reply_target if reply_target is not None
            else self._resolve_mount_key(session_key)
        )
  ```
  So every send (group, `/ask`, bubble-forward, work assignment, review ping,
  agent-command, `@`-mention) normalizes the slot. A non-targeting caller defaults to
  the routing (project) — never a stale private target.
- **SP3a-audit BUG#1 fix — the guard-2 path MUST clear the slot (the "renders
  nothing" premise was FALSE):** the no-project guard calls `_do_error` — which DOES
  render — and `_do_error`'s `_resolve_chat_box` is slot-FIRST. A stale slot
  (`_turn_reply_target[sk]` left from a prior `/ask`) would therefore shadow the
  session's own live tab and **drop or misroute the no-project error bubble**. Fix:
  on the guard-2 branch, `self._turn_reply_target.pop(session_key, None)` BEFORE
  dispatching `_do_error`, so the error resolves to the session's own tab (the
  pre-SP3a behavior). The unregistered guard-1 path renders nothing, so it needs no
  clear. Correct the comment: the guard DOES render (an error bubble).
- **Clear at the ARH terminal paths (BUG#17 + BUG#21, turn-guarded by SP3b-audit
  BUG#1):** the slot must be cleared at the **END** of the terminal methods — AFTER
  every render read — NOT at the `_ended_sessions.add` marker (which sits at the TOP
  of `_do_response_complete` / `_do_error`, ~56–145 lines BEFORE the reads). The pop
  is **conditional on turn identity**: capture the turn's token at method entry and
  pop ONLY if `self._turn_tokens.get(session_key)` is still that token. An
  unconditional pop is wrong — a nested send to the SAME session_key launched from
  inside the try (a self-directed A2A command, reachable via `_on_agent_response`
  firing inside `_do_response_complete`) runs a NEW turn and sets a NEW slot; the
  OUTER turn's `finally` would clobber it, misrouting the nested reply to the project
  instead of its private target.
  Implementing the clear beside the marker would nullify the `/ask` privacy (the read
  would see an empty slot → fall back to project routing).

  **Recommended form — read into a local at the top, use the local everywhere, pop in a
  `finally:`:** in `_do_response_complete` and `_do_error`, immediately after the token
  guard, bind `reply_key = self._turn_reply_target.get(session_key) or
  self._resolve_mount_key(session_key)` and use `reply_key` at every render/box site in
  that method; wrap the method body in `try: ... finally:
  self._turn_reply_target.pop(session_key, None)` so EVERY exit path (including the
  early returns for duplicate-completion at `_do_response_complete:2488`/`:2497` and
  `_do_error:2882`) clears the slot (BUG#25). (Alternative: leave all reads as
  `self._turn_reply_target.get(session_key) or self._resolve_mount_key(session_key)` and
  pop once at the method's fall-through end — correct for the render paths but leaves
  early-return paths to the set-on-entry overwrite as the safety net; disclose your
  choice.) **NOT `_terminate_turn`** — that is `agent/runtime.py`, a
  different class with no handle on this dict.
- The render sites read `self._turn_reply_target.get(session_key) or
  self._resolve_mount_key(session_key)`.

**BUG#20a — the render-site enumeration (ALL, not "~7"):** `_resolve_mount_key(` is
called at **11 sites** — `:2541, :2565, :2577, :2593, :2605, :2746, :2765, :2801,
:2805, :2915, :2930`. ALL 11 become
`self._turn_reply_target.get(session_key) or self._resolve_mount_key(session_key)`.
`_resolve_chat_box(` is called at **7 sites** — `:1771, :1930, :2572, :2601, :2749,
:2792, :2917` — each likewise prefixed with the slot lookup. A builder editing only a
subset silently regresses `/ask` on the unlisted paths.

`_resolve_mount_key` keeps R7 only (no private-view set):

```python
    def _resolve_mount_key(self, session_key: str) -> str | None:
        """SPEC-12 R7: the agent's project, else the ACTIVE open project,
        else the raw session key (no project open). Turn-scoped private
        targets are applied by the CALLER via _turn_reply_target (R5 REV 3)."""
        project_name = None
        if self._agent_to_project is not None:
            project_name = self._agent_to_project.get_project(session_key)
        if project_name is None and self._active_project is not None:
            project_name = self._active_project[0]
        if project_name is not None:
            return f"project:{project_name}"
        return session_key
```

`_resolve_chat_box` (`:1593`) — **BUG#27 fix (R7 gap): it must carry the SAME
R7 active-project fallback.** Its current body returns `None` when there is no
direct tab and no `AgentRoutingTable` routing. The `_do_error` / non-streaming
sites gate on `if chat_box is not None:` **before** calling `render_sync`, so an
unrouted agent (e.g. Supervisor before it is added) with a project open would
get `None` → the row is silently dropped — reintroducing the exact BUG#3 class
R7 exists to eliminate. Full respec (slot + routing + R7 fallback):

```python
    def _resolve_chat_box(self, session_key: str):
        """SPEC-12 R7: resolve the box from the turn reply key (slot first),
        else the direct tab, else the agent's project, else the ACTIVE
        project, else None (no project open, no tab)."""
        key = self._turn_reply_target.get(session_key) or session_key
        direct = self._mc.get_chat_box_for_session(key)
        if direct is not None:
            return direct
        project_name = None
        if self._agent_to_project is not None:
            project_name = self._agent_to_project.get_project(session_key)
        if project_name is None and self._active_project is not None:
            project_name = self._active_project[0]
        if project_name is not None:
            return self._mc.get_chat_box_for_session(f"project:{project_name}")
        return None
```

The 7 `_resolve_chat_box(` call sites therefore need **no source edit** — the
slot lookup lives inside the method, so the spec's "each likewise prefixed with
the slot lookup" is satisfied by this internal consult rather than 7 call-site
edits (needless churn). The 11 `_resolve_mount_key(` sites DO become
`self._turn_reply_target.get(session_key) or self._resolve_mount_key(session_key)`
(or the `_reply_key` helper — see the preflight decisions).

### 2d. `ui/window.py` — no auto-open agent tabs

- **Delete** the auto-open block (`:193-207`). **BUG#6 fix:** also remove the now-unused
  `get_auto_open_agents` import (`:192`) or ruff F401 fires. The block's replacement is a
  **no-op** — `ProjectHandler._active_project_name` starts `None` at launch
  (`project_handler.py:266`), so there is no "active project" to auto-open. Do NOT
  invent a launch-time project auto-open (the spec's earlier "if a project is
  active/auto-openable" described nonexistent state).
- **BUG#3 fix — unrouted-agent host (R7 revision).** The original R7 premise ("every
  special agent is auto-added to the project") is **FALSE** — verified: no default agent
  carries `auto_add_to_projects: true` (`supervisor.yaml: auto_add_to_projects: false
  # deliberately manual`), and `get_project_onboarding_agents()` returns `[]`. So a
  Supervisor session not yet added to the project is unroutable and, with auto-open gone,
  tabless — its replies would drop at `_MOUNT_MISS_LIMIT`, killing the onboarding path.

  **Ruling R7 (revised) — project-first render routing:** when a project is OPEN, EVERY
  agent's output renders into that project's surface (agent_name set), member or not.
  Membership governs SEND fan-out, not render routing. This eliminates the unrouted-drop
  class entirely and is the purest expression of "the project tab is THE chat."

  **Mechanism:** the `_resolve_mount_key`/`_resolve_chat_box` sample in §2c already
  carries the R7 branch (`if project_name is None and self._active_project is not None`).
  No separate mechanism here — §2c's block IS the R7 implementation. The only remaining
  unrouted case is "no project open at all" → raw session key; the picker is shown when no
  project is open and no agent turn is expected, so nothing drops. Update the FIX-11
  eviction note accordingly (eviction stays for genuinely-dead getters only).
- `_on_agent_selected` (`:1450`) — the agent list "Chat" button now opens the
  **project tab** (member management → project surface). **Ruling R3:** if the agent is
  a member of the active project, open `project:<name>`; if not a member, no-op (the
  member toggle `+` is the path to add it). The private-view command path is unaffected.

### 2e. `ui/handlers/chat_handler.py` — send path

- Project tab group-send (`:412`) — now sets `reply_target=f"project:{name}"` per
  member (was a bare `_send_local`); see §2f. This is the BUG#11 fix.
- **BUG#3b fix — do NOT remove the non-project branch wholesale.** That branch
  (`:416`) is a send path for a non-special-agent non-project tab.
  **Ruling R6 (restated):** keep the branch; it runs for a current tab whose
  session_key is not `project:` and not a special agent. The branch is *not* deleted.
- **The special-agent branch (`:267`) + `forward_to` branch (`:216`) both change**
  (see §2f): `forward_to` sets `reply_target=target`; the special-agent branch passes
  `reply_target=session_key` when the tab is agent-keyed (private).

### 2f. Private view — `/ask`+`/delegate` (command path) + bubble-forward

**BUG#2/#11 fix — the call site + the mechanism (R5 REV 3).** Verified: `/ask`/`/delegate`
set `CommandResult.forward_to` and are handled in `ChatHandler.on_send` at
**`chat_handler.py:216-236`** via `self._send_local(result.forward_to, …)` —
`forward_to_agent` is NOT on this path (bubble-forward popover only,
`forward_handler.py:124`). The reply surface is chosen **per send** via
`reply_target` (§2c), NOT a session mark.

**Command path, `chat_handler.on_send` `forward_to` branch:**

```python
if result.forward_to and result.forward_text:
    target = result.forward_to
    if self._agent_runtime_handler is not None:          # BUG#14: None-guard
        # open a private tab (agent-keyed) — idempotent
        if self._mc.get_chat_box_for_session(target) is None:
            # BUG#20c: "special:coder" has no "/", split yields the whole
            # string — strip the "special:" prefix for a clean tab label.
            label = target.split(":", 1)[-1]
            self._mc.create_chat_tab(target, label)
        # R5 REV 3: reply renders in the PRIVATE tab for THIS send
        self._agent_runtime_handler.send_to_special_agent(
            target, result.forward_text, reply_target=target)
    else:
        self._send_local(target, result.forward_text)    # fallback (ARH unwired)
```

Note this changes the branch from `_send_local(...)` to a direct
`send_to_special_agent(..., reply_target=target)`. The mark + tab creation happen
**synchronously in the branch** (before `_dispatch`), so the reply can't race (BUG#14).

**BUG#23 — private-tab UX:** the `/ask` echo renders in the CURRENT tab while the reply
renders in the private tab. **Ruling:** the spec SELECTS the newly-created private tab
on creation (`_chat_notebook.set_current_page(idx)`, as `create_chat_tab` already does
for a new tab at `main_content.py:~649`) so the user sees the reply. If the tab already
exists, do not steal focus.

**Bubble-forward path, `forward_handler.forward_to_agent` (`:130`):** same idea —
call `self._agent_runtime_handler.send_to_special_agent(target_session_key, text,
reply_target=target_session_key)`. **BUG#28 (ordering, pre-flight-verified):** the
CURRENT code sends at `:~172` and creates/selects the tab at `:193`+ — send
*before* tab. Reorder to **create/select the tab first, then send**, so the private
tab exists when the reply is produced (a literal port of the current order would
race the first reply against the box).

**Group fan-out (`chat_handler.on_send` `:412`) — set `reply_target` to the PROJECT
key** so member replies land in the group surface (this is what makes BUG#11 fixed):

```python
    for member in members:
        self._agent_runtime_handler.send_to_special_agent(
            member, text, reply_target=f"project:{project_name}")
```

(The existing `_send_local` wrapper gains a `reply_target` kwarg, or the branch calls
`send_to_special_agent` directly — builder's choice, disclose.)

**BUG#19 — manual send in an open private tab.** `on_send` checks the special-agent
branch (`chat_handler.py:267`) BEFORE the `project:` branch, so a private tab for a
special agent hits that branch and calls `_send_local(session_key, text)` — with no
`reply_target`, the reply would route to the project. **Fix:** in the special-agent
branch, pass `reply_target=session_key` when the current tab is agent-keyed (a private
view): `if self._mc.get_chat_box_for_session(session_key) is not None: reply_target=
session_key else None`. So follow-up typing in a private tab stays private; a group send
still targets the project.

**The special-agent branch edit (was prose-only in the earlier draft — pre-flight
BUG#29):** the branch at `chat_handler.py:267-289` currently ends with
`self._send_local(session_key, text)`. Replace with a direct call carrying the
per-send target:

```python
        # ── Special agent check (Phase 1.4) ─────────────────────────────
        if (self._agent_runtime_handler is not None
                and session_key in self._agent_runtime_handler.get_special_agents()):
            # BUG#19: typing in an open private (agent-keyed) tab stays
            # private; a group send reaches the project branch below and
            # targets the project.
            reply_target = (
                session_key
                if self._mc.get_chat_box_for_session(session_key) is not None
                else None
            )

            def _show_and_route_to_agent():
                ...  # unchanged echo block (render_async "You")
                self._agent_runtime_handler.send_to_special_agent(
                    session_key, text, reply_target=reply_target)

            self._dispatch(_show_and_route_to_agent)
            buf.set_text("")
            if self._on_send_initiated:
                self._on_send_initiated(session_key)
            return
```

(If the builder keeps the `_send_local` wrapper, it must gain a
`reply_target: str | None = None` kwarg forwarded to `send_to_special_agent` —
disclose which form landed.)

**BUG#7 — no teardown needed (superseded by R5 REV 3).** The session mark is GONE, so
there is nothing to clear on tab close. A closed private tab's surface is destroyed by
`MainContent._close_tab`'s existing `close_session` call (`:776` — the single tab-close
funnel). The NEXT send to that agent normalizes `reply_target` (BUG#18), so a stale
target cannot persist. **BUG#7 is resolved by design.**

`chat_handler.py` non-project send branch (`:416`) — **RETAINED** (R6): it remains the
send path for a non-special-agent non-project tab. (The special-agent branch handles a
special-agent private tab per BUG#19 above.)

### 2g. `ui/handlers/agent_list_handler.py` — member management (kept)

No change to `on_agent_chat`/`on_agent_toggle` semantics; the callback target changes
to the project tab (§2d).

**Files NOT changed** (already correct):
- `models/routing.py` — `AgentRoutingTable` API unchanged.
- `utils/transcript_store.py`, `agent/persistence.py` — store schema unchanged.
- `render/*` — pipeline unchanged.
- `ui/views/main_content.py` `create_chat_tab` — signature unchanged (called with
  `project:<name>` only for the group surface; agent keys only for private views).

---

## 3. Data Flow

**Group send (project tab):** user types → `ChatHandler.on_send` → fan-out to members
(`_send_local`) → each member's `send_to_special_agent` → turn → completion →
`render_sync/end_streaming(mount_key=_turn_reply_target.get(sk) or _resolve_mount_key(sk))` → display key `project:<name>` → the ONE project surface → `append_message(role, html, agent_name)`
→ `_document` groups the run under the agent's header → one HTML doc → one webview /
one scroll.

**Reply routing:** `_turn_reply_target.get(sk) or _resolve_mount_key(sk)` → `project:<name>` for group turns; the agent key for a `/ask` turn.

**Private view (`/ask`):** command → `chat_handler.on_send` `forward_to` branch →
private tab + `send_to_special_agent(target, text, reply_target=target)` → reply there.
(`forward_to_agent` covers the bubble-forward popover.)

---

## 4. File Change Summary

| File | Change | ~Lines | Risk |
|---|---|---|---|
| ui/views/chat_surface.py | grouped agent boxes + CSS (user-row fix) | +45 | low |
| ui/handlers/chat_render_handler.py | display-key surfaces (full respec) + render_async mount_key | +60/−35 | med-high |
| ui/handlers/agent_runtime_handler.py | `_turn_reply_target` + `reply_target` param + R7 resolvers | +30/−15 | med |
| ui/window.py | remove auto-open + unused import; Chat→project | −20/+8 | med |
| ui/handlers/chat_handler.py | `forward_to` branch + group fan-out reply_target (§2f) | +12 | low |
| ui/handlers/forward_handler.py | `reply_target` on forward_to_agent (R5 REV 3) | +4 | low |
| tests (new/updated) | surface-per-project + grouping + key-domain + private view | +240 | — |

---

## 5. Implementation Order

1. `chat_surface._document` grouped boxes + CSS (+ tests incl. user-row grouping).
2. `chat_render_handler` display-key surfaces (FULL respec of `_surface_for` +
   `_append_to_surface` + `close_session` + `render_welcome` + eviction) +
   `render_async(mount_key=)` (+ tests; rewrite the `test_activity_pill_adapter`
   project-tab test — BUG#8).
3. `agent_runtime_handler` `_turn_reply_target` slot + `send_to_special_agent`
   `reply_target` param + R7 `_resolve_mount_key`/`_resolve_chat_box` (+ tests).
4. `chat_handler.on_send` `forward_to` branch: `reply_target=target` + private tab;
   group fan-out: `reply_target=f"project:{name}"`; `forward_handler.forward_to_agent`
   too.
5. `window.py` remove auto-open + remove the unused `get_auto_open_agents` import +
   `_on_agent_selected` → project tab (+ tests).
6. `send_to_special_agent` `reply_target` param + `_turn_reply_target` slot +
   clear at the END of `_do_response_complete`/`_do_error`, after the reads (R5 REV 3) (+ tests).
7. Full suite + ruff + pyright.

---

## 6. Acceptance Criteria

- [ ] A project tab renders **one** HTML surface; N agents → 1 scrollbar, not N
- [ ] Each agent's consecutive output renders as a named HTML box (`.agent-box` +
      `.agent-name`); user rows group too, header stays green (BUG#5)
- [ ] No auto-opened agent tab on launch; the unused `get_auto_open_agents` import is
      removed (BUG#6)
- [ ] Agent turns route to the project surface; `/ask` replies go to the private tab via
      turn-scoped `reply_target` at `chat_handler.on_send`'s `forward_to` branch
      (BUG#2/#11 — NOT a session mark)
- [ ] All 11 `_resolve_mount_key` sites + 7 `_resolve_chat_box` sites read
      `_turn_reply_target` first (BUG#20a); `_turn_reply_target` is set on every send
      (BUG#18) and cleared at the END of `_do_response_complete`/`_do_error` — AFTER the
      reads (BUG#17/#21); `/ask` reply lands in the private tab on both completed+error paths
- [ ] Manual send in a private tab stays private (BUG#19)
- [ ] Only ONE key domain per structure (§2b table); no `KeyError` on eviction; no orphan
      resurrection after project close (BUG#1)
- [ ] `render_async` gains `mount_key=` AND its call sites pass it (BUG#4+#10)
- [ ] Agent list still supports add/edit/remove + member toggle
- [ ] Any unrouted agent (e.g. Supervisor before it is added) renders into the OPEN
      project surface — never dropped (R7/BUG#3)
- [ ] `/ask` + `/delegate` private view works; a member's GROUP reply still renders in the
      project surface (BUG#11)
- [ ] `test_activity_pill_adapter` project-tab test rewritten to the display-key model
      (BUG#8)
- [ ] Code blocks/tables/links render as HTML in the single surface
- [ ] Full pytest green, ruff clean, pyright clean

---

## 6a. Resolved Ambiguities (rulings)

- **R5 REV 3 — turn-scoped reply target:** `_turn_reply_target[session_key] = <display
  key>` set on ENTRY of EVERY `send_to_special_agent` (`reply_target or
  _resolve_mount_key`), read at the 11 render sites + 7 box sites. Group fan-out →
  `project:<name>`; `/ask` → the agent key. NO session mark (BUG#11). Cleared at the
  END of the ARH terminal methods `_do_response_complete`/`_do_error` — after the
  renders read it (BUG#17/#21). **Call sites:**
  `chat_handler.on_send` `forward_to` branch + special-agent branch + group fan-out;
  `forward_handler.forward_to_agent` (bubble-forward).
- **R6 — private-view send path:** the `chat_handler.on_send` non-project branch is
  KEPT (it is the private view's send path). Resolves BUG#3b.
- **R7 — unrouted agent host:** when a project is OPEN, every agent renders into that
  project's surface (membership governs send fan-out, not render routing). Resolves
  BUG#3.
- **R8 — close-window in-flight render (BUG#16):** if a project closes while a turn is in
  flight, its completion render resolves to the raw session key with no box and drops at
  `_MOUNT_MISS_LIMIT`. This is ACCEPTED for this unit (the stop-all path already cancels
  in-flight turns on close; a genuinely-racing completion is bounded and logged). A
  test pins "no exception, one warning" rather than silent corruption.
- **Key domain:** the table in §2b is authoritative. Resolves BUG#1.

---

## 7. Edge Cases

| Case | Behavior |
|---|---|
| Agent not a project member, project open | Renders into the OPEN project surface (R7) — never dropped |
| Agent not a member, NO project open | Raw session key; picker shown → no agent turn expected |
| Project closes with a turn in flight | Completion render drops-with-warning (R8, accepted); no exception, no corruption |
| Multiple agents stream at once | One surface; per-session stream buffers + session-keyed reentrancy; rows group per agent |
| Project tab closed then reopened | One surface destroyed/recreated; tombstone on the DISPLAY key |
| `project:<name>` tab absent at render | `_resolve_mount_key` returns the key; mount retries (FIX 1) |
| Agent tab exists from before this change | Not created anymore; if a private view, resolver honors it (R5) |
| `/ask` to a project member | Reply-target = agent key for THAT send → private tab; the member's LATER group replies still land in the project surface (BUG#11) |
| Private view tab closed | `_close_tab` destroys the surface; next send sets a fresh reply_target (no mark to clear) |
| Typing in a private view | special-agent branch passes `reply_target=session_key` when tab is agent-keyed (BUG#19) → stays private |
| WebKit absent | `TextViewFallback` renders `[name] line` per agent (parity) |
| User rows in a project tab | Group under one "You" box; `.agent-box.role-user .agent-name` keeps green (BUG#5) |
| Two agents' cards (review queue) | `metadata["tab_key"]` flips to `project:<name>` — SPEC-10 card-jump re-verified |

---

## 8. ARCHITECTURE.md Updates Required

- §Modules/Chat surface — revise: **one surface per project tab**; per-agent HTML
  boxes; the per-agent-session surface path removed (private view is the sole
  exception, opt-in).
- §Data Flow / Render — "one surface per project" replaces "one per chat surface."
