# SPEC-12 Pre-flight Decisions (Supervisor, 2026-10-05)

Spec: `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` (verified against HEAD 8c03a9b,
dirty). Loop: `prompts/implementationLoop.md`. Supervisor: Supervisor; Builder: Coder;
Auditor: Debugger.

This spec is unusually precise — its line-level inventory was audited against HEAD and
**matched**: 11 `_resolve_mount_key` sites, 7 `_resolve_chat_box` sites, `render_async`
lacking `mount_key`, `surface_for_key` having zero production callers, the auto-open
block in `window.py`, `supervisor.yaml auto_add_to_projects: false`, `_active_project_name`
init `None`, the terminal-marker-vs-read line gap, and the `forward_to`-branch code shape
were all confirmed. Four spec bugs were found and **fixed in the spec** before phasing
(spec-writing turn — not audited per §3.1a). Rulings below are the build contract.

## Spec bugs found in pre-flight (fixed in spec)

### BUG#27 — `_resolve_chat_box` was missing the R7 fallback (fixed)
The spec gave `_resolve_mount_key` the R7 active-project branch but only said
`_resolve_chat_box` "reads the slot." That is insufficient: the `_do_error` and
non-streaming sites call `render_sync` **only inside `if chat_box is not None:`**, and
`_resolve_chat_box` returns `None` for an unrouted agent with no direct tab and no
`AgentRoutingTable` routing. An unrouted agent (Supervisor-before-add) with a project
open → row silently dropped → R7's "never dropped" promise broken on 2 of 3 render
paths. **Fix (spec §2c):** `_resolve_chat_box` now carries the SAME R7 fallback
(slot → direct tab → agent's project → ACTIVE project → None). The 7 call sites need
**no source edit** — the slot consult lives inside the method.

### BUG#28 — bubble-forward ordering (fixed)
`forward_handler.forward_to_agent` currently SENDS at `~:172`, then creates/selects the
tab at `:193`. The spec claimed the tab "already creates ... at :193" as if it preceded
the send. A literal port would race the first reply against the box. **Fix (spec §2f):**
create/select the tab FIRST, then send.

### BUG#29 — special-agent branch had no code block (fixed)
The "typing in an open private tab stays private" mechanism (BUG#19) was prose-only.
**Fix (spec §2f):** full branch edit shown (`reply_target = session_key if
get_chat_box_for_session(session_key) is not None else None`).

### BUG#30 — test-churn scope understated (fixed)
The spec named only `test_activity_pill_adapter` for rewrite. Pre-flight-verified:
`tests/test_chat_render_handler.py` (`:733`, `:958-959`, `:977-984`, `:1050-1056`) and
`tests/test_welcome_html.py` (`:192`) also encode the retired session-keyed tombstone
model. **Fix (spec §2b):** enumerated in scope; the "+240" estimate must cover them.

## Decisions

### D1 — Key-domain table (§2b of the spec) is authoritative
Every surface-lifecycle structure resolves `display_key = mount_key or session_key`
ONCE and uses that variable throughout. Streaming (`_stream_text`, `_streaming`,
`_stream_role`) and `_ReentrancySet` stay **session-keyed**. `_surfaces`,
`_closed_sessions`, `_mounted_box_keys`, `_welcome_shown`, `_mount_misses` become
**display-keyed**. Any method mixing the two domains is a bug.

### D2 — `_reply_key` helper (avoid 11 hand-edits)
The 11 `_resolve_mount_key` call sites + 7 `_resolve_chat_box` call sites are error-prone
to hand-edit. **Ruling:** add a small private helper on AgentRuntimeHandler:
```python
def _reply_key(self, session_key: str) -> str:
    """SPEC-12 R5 REV 3: the turn's reply/display key — the per-send private
    target when set, else the routing (project) key. NEVER None (falls back
    to the session key) so `render_sync(mount_key=...)` is always set."""
    return self._turn_reply_target.get(session_key) or self._resolve_mount_key(session_key) or session_key
```
- The 11 `_resolve_mount_key(session_key)` sites become `self._reply_key(session_key)`.
- `_resolve_chat_box` consults `self._turn_reply_target.get(session_key)` internally
  (BUG#27 respec) — its 7 call sites are unchanged.
- Builder discloses if it keeps the inline `X.get(sk) or _resolve_mount_key(sk)` form
  instead; the helper is preferred (one place to reason about the None-fallback).

### D3 — Terminal-path clear (R5 REV 3 / BUG#17/#21/#25)
`_turn_reply_target` is cleared at the **END** of `_do_response_complete` and `_do_error`
— NOT at the `_ended_sessions.add` marker (which sits ~56–145 lines before the reads).
**Ruling:** read into a local at the top after the token guard, use the local at all
render/box sites, pop in a `finally:` so ALL exit paths (incl. duplicate-completion
early returns) clear the slot. Not `_terminate_turn` (that is `agent/runtime.py`).

### D4 — `send_to_special_agent` slot set placement (BUG#26 / BUG#18)
The slot is set on ENTRY, **after** the two early-return guards (unregistered agent;
no active project) so only sends that will actually run set it. Always set (default =
`self._resolve_mount_key(session_key)`), never conditionally — a non-targeting caller
normalizes the slot away from any stale private target.

### D5 — R7 semantics (render routing ≠ membership)
When a project is OPEN, EVERY agent renders into that project surface. Membership
governs SEND fan-out, not render routing. The only unrouted case left is "no project
open at all" → raw session key.

### D6 — `_send_local` gains `reply_target` kwarg
`_send_local(session_key, text, reply_target=None)` forwards to
`send_to_special_agent(session_key, text, reply_target=reply_target)`. Group fan-out and
the special-agent branch use it. Builder may instead call `send_to_special_agent`
directly — disclose which.

### D7 — Phasing (see SPEC-12-SUBPHASES.md)
SP1 chat_surface · SP2 chat_render_handler · SP3 agent_runtime_handler · SP4a/b/c
chat_handler + forward_handler · SP5 window.py · SP6 close-out. One file per phase,
sub-phased where a file carries ≥3 edits (SP4 = integration, sub-phased).

### D8 — Tests
RED-first per steelFramedCodeWriter. New/updated tests: grouped boxes (incl. user-row
grouping + CSS class), display-keyed surfaces + tombstone on display key + eviction/no
KeyError, `render_async(mount_key=)` threads + call sites pass it, turn-scoped reply
target (group → project, `/ask` → agent key), terminal clear after reads, R7 unrouted
render, activity-pill rewrite, tombstone-welcome rewrites.

### D9 — Out of scope (do not expand)
Feed/file-tree/toolbar stay GTK. Transcript schema unchanged. JEV/addressing post-MVP.
Multi-project simultaneous-tab redesign NOT in scope. `.crabcakes/` state dir untouched
(BLOCKING-1). No env-var changes.

## Standing verification commands
- `python -m pytest tests/test_chat_render_handler.py tests/test_welcome_html.py tests/test_activity_pill_adapter.py -q`
- `python -m pytest tests/test_chat_handler.py tests/test_forward_handler.py tests/test_agent_runtime_handler.py -q`
- Full battery at SP6 (xvfb, ~15 min) — compare to the pre-existing 3-failure enforcement trio.
- `ruff check <touched files>` / `pyright <touched files>` vs measured baselines.