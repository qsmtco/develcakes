# SPEC-53 — JEV Room-Addressing Fallback & Multi-Addressee Routing

Status: DRAFT (2026-10-22)
Owner: Supervisor · Implementer: Coder · Audit: Debugger
Base: ac61e4f4 (uncommitted dashboard work unrelated)
Predecessor: SPEC-39 (room addressing), SPEC-46 (JEV wiring)

## Problem

The group chat's "all" broadcast stalled 2026-10-22 when the JEV decision model
(zai/glm-5.3 via openrouter fallback) began timing out. `resolve_room_addressee()`
already has a hard 2 s timeout (`_DECISION_TIMEOUT_S`) and never raises — every
failure path returns `"all"`. The residual gaps:

1. **Fallback target.** On JEV failure, low confidence, or "don't know", the
   message broadcasts to ALL members. The room's primary addressee is
   Supervisor, so a full spray is noisy and costly. No user control exists.
2. **Multi-addressee.** JEV can only return `"all"` or ONE member. A message
   aimed at 2 or 3 of 4 agents is unrepresentable: it resolves to one agent
   (missing the others) or to all (hitting the rest unintentionally).
3. **Visibility.** When a fallback fires, the human has no idea the routing
   decision differed from what they typed.

## Non-goals

- No changes to message text, tab keys, or the room-PM line format
  (`[room] PM: ...`) beyond the new multi-target fan-out.
- No new settings surface beyond ONE field (no per-agent routing rules).
- No changes to solo-DM mode (right-click → member name menu) — that path
  bypasses JEV entirely and stays as-is.

## Design

### §1 — Resolver returns a list

`agent/llm/jev_client.py`:

- `resolve_room_addressee(text, roster) -> list[str]` — returns a list of
  session keys (possibly empty), or `["all"]` for the whole room.
- JEV question changes to accept MULTIPLE selections: the addressee answer
  type gains a `selection: multi` field; the model may return a list of
  choices. Every key in the returned list must be in `criteria`; any key
  that is not is dropped. An empty/invalid/low-confidence result returns
  `[]` (NOT `["all"]`) — fallback routing moves to the caller.
- All existing failure invariants (timeout, non-2xx, unparseable body,
  wrong answer type, bool confidence, empty roster) return `[]`.
  The 2 s total-exchange timeout is kept as-is.
- Backward compatibility: keep a thin wrapper or update all call sites
  (there is exactly one production call site, `chat_handler.py:~468`).

### §2 — Caller fallback routing + setting

`ui/handlers/chat_handler.py` `_deliver()`:

```
targets = set(choice) if isinstance(choice, list) else set()
targets &= members            # drop hallucinated keys
if not targets:              # JEV failed / empty / unknown
    targets = resolve_fallback(members)   # per setting
    note_room_line(fallback_reason)       # visible [room] note line
send to targets as user message
room-PM line to members - targets
```

- Uniform handling of 1-of-4, 2-of-4, 3-of-4: targets get the raw text as
  their user message; everyone else gets `[room] PM: <text>`. "all" is
  `targets == set(members)` with no special case.
- `resolve_fallback(members)` reads a new agent setting:

  **jev_room_fallback**: `"supervisor"` (default) | `"all"` | `"drop"`

  - `supervisor`: route to the Supervisor member if present in the project
    roster; if Supervisor is not a member (unusual), fall through to `"all"`.
  - `all`: current behavior (broadcast to every member).
  - `drop": render the human line in the room, wake nobody.
- Visibility: whenever the fallback fires, emit one room line per woken
  target: `[room] JEV offline (timeout/low confidence) — routed to <name>`
  (or `— broadcast to all (fallback)` / `— message not routed`).

### §3 — Settings dialog field

`ui/views/settings_dialog.py` (+ settings handler/persistence):

- One combo field in the agents section: "When JEV can't route a group
  message: [ Route to Supervisor ▾ ]" with options
  `Route to Supervisor` / `Broadcast to all` / `Don't route`.
- Persisted key `jev_room_fallback`, default `"supervisor"`.
- A short caption naming the three options.

## Phases

| # | Phase | Files | Owner |
|---|-------|-------|-------|
| 1 | Resolver list-return + multi-select JEV question | agent/llm/jev_client.py + tests/test_jev_client.py (resolver tests) | Coder |
| 2 | Caller fallback routing + room note line | ui/handlers/chat_handler.py (+ tests) | Coder |
| 3 | Settings field + persistence | ui/views/settings_dialog.py, settings handler (+ tests) | Coder |
| 4 | Docs + Debugger adversarial audit | docs/ARCHITECTURE.md §3.14n, audit | Debugger |

## Acceptance

- `scripts/acceptance.py` green; full pytest suite green.
- Resolver returns `[]` on: timeout (2 s), non-2xx, garbage body, low
  confidence, empty roster. Returns a validated key list on success;
  hallucinated keys are dropped (an empty result after filtering is `[]`).
- `chat_handler` delivers 2-of-4 and 3-of-4 correctly: selected members get
  the raw text, the rest get the `[room] PM:` line; the "all" case sends to
  all with no PM lines.
- With `jev_room_fallback=supervisor` and JEV forced to fail, only
  Supervisor is woken and a visible `[room] JEV offline — routed to
  Supervisor` line appears in the room.
- `"all"` and `"drop"` fallback settings behave per §2.