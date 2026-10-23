# SPEC-54: Agent-to-Agent Room Communication — Proposal (DRAFT, not yet approved)

**Status:** DRAFT proposal for PM review — nothing implemented, nothing ordered.
**Author:** Supervisor, on PM direction (2026-10-22 late session).
**Supersedes/extends:** parked agent→agent room routing spec (2026-10-22 memory log).
**Related:** SPEC-39 (room addressing), SPEC-53 (multi-addressee + fallback), V7 hybrid router (committed cd9b8e74), converge (~/Downloads/converge).

---

## 1. Vision — the destination (PM's north star, stated 2026-10-22)

> Humans and agents as peers on a network, building software together, from wherever.

Concretely: a **DevelCakes project development server**. Each human developer runs a
client and connects. Each developer can bring **their own agents** — agent clients
connecting to the same server. Everyone in the room: humans and agents, chatting,
routing, writing and reviewing software, from wherever they are.

This is the product direction. It is **not** a spark, not a maybe: the PM has
declared multi-machine, multi-human, multi-agent the destination. Two consequences
we accept up front (PM ruling, not mine):

- **Message persistence matters.** Rooms must survive disconnects; late joiners
  must be able to catch up. Retrofitting persistence later is expensive, so the
  design assumes a durable room log from the start (even if v1 keeps it in-memory
  and only the *shape* is durable).
- **Identity and auth for agents matter.** An agent joining a room is a principal
  with a key, not an implicit in-process assumption. v1 can punt (in-process
  identity), but the envelope carries identity from day one so no retrofit.

What we are **not** doing in this spec: no new wire protocol ("A2ACP" was
considered and rejected — see §8), no sockets in v1, no multi-machine in v1.

---

## 2. What already works (proven tonight, live)

The proof that "group chat with agents is a viable way to build software" is
**already mostly in the tree**:

- **PM → agent routing**: `resolve_room_addressee` with the **V7 hybrid router**
  (committed `cd9b8e74`). Live-verified tonight: single-address (primary path,
  one `post_decision` call), multi-address ("hey supervisor and debugger" →
  fallback fires on split mass → both wake), no-name broadcast, indirect
  address. Bench: 28/30 strict.
- **Fallback discipline**: JEV outage → resolver fails closed → broker fallback
  (supervisor) with a visible `[room] JEV offline` note. Verified live tonight.
- **`/ask` DM consultation**: works today (Coder confirmed receipt).
- **The referee exists**: `converge` (PM's prior work) — Random Forest
  conversation-convergence detection, 10 signals, 3-layer policy
  (QAC floor → model → hard wall). Local, pre-trained, no network. It is the
  missing loop-breaker layer.

**The one gap (live-proven tonight):** agent → agent addressing does not exist.
Supervisor typing "@coder hi coder" in a reply delivers nothing. The resolver only
runs on PM-originated text in the project surface (`ui/handlers/chat_handler.py`
line 430-446: the solo-DM path bypasses it entirely; agent replies never re-enter
the resolver).

---

## 3. Architecture — the in-process bus (the "server", phase 1)

The PM's original sketch: every agent connects *as a client* to a DevelCakes
server; the server broadcasts; each client JEV-filters "is this for me?";
a server-side round cap stops loops; a server-side JEV judges contribution.

Supervisor's amendment (accepted direction): **keep every part of that design
except the sockets.** "Server" = an in-process message bus with broker
discipline. "Client" = a queue plus a delivery check. Same topology, zero
transport layer. When multi-machine arrives, the bus gets a socket
implementation and **nothing above it changes** — that's the whole point.

### 3.1 The message envelope

The protocol instinct was right; the protocol ceremony wasn't. We take the
*envelope*, not a wire format:

```
RoomMessage:
  id: str                    # monotonic, broker-assigned
  room: str                  # project room key (e.g. "project:develcakes")
  from: str                  # sender session_key (agent or human)
  to: list[str] | "all"      # RESOLVED addressee(s) — V7 output, never re-resolved
  kind: "human" | "agent" | "room_pm" | "system"
  text: str                  # raw payload, unmodified
  turn: int                  # broker's per-thread round counter
  ts: float                  # wall clock
```

Load-bearing choices:

- **`to` is pre-resolved.** Routing happens ONCE (see 3.2); delivery is
  mechanical. No agent re-routes another agent's mail (the trust fix).
- **`kind` matters for routing rules** (3.2): human and agent lines route
  differently by design.
- **`turn` is broker-owned.** Agents cannot set it; they can only read it.
  This is the loop-breaker's counter.

### 3.2 Routing rules (what fires the resolver)

The V7 resolver runs on **exactly two kinds of lines**, with different inputs:

1. **Human lines** (today's behavior, unchanged): `resolve_room_addressee(text,
   roster)` — full V7 hybrid, as committed.
2. **Agent lines with an explicit @-mention**: deterministic prefix match ONLY.
   An agent writing `@coder fix the login bug` routes to Coder. **v1 does NOT
   JEV-route free-text agent replies.** Reason (observed live tonight): agent
   replies are name-saturated ("Coder implemented, Debugger audited") —
   free-text routing through the 5-way ask would misfire constantly, exactly
   the Q2 indirect case where JEV leaks to `all`. Explicit mentions only.
   An agent line with no @-mention **does not route** — it lands in the room
   transcript for humans/PM and does not wake anyone. (Cheap, safe default.)
3. **Room PM echoes**: never re-routed. A room PM line records visibility, it
   is not an address.

### 3.3 Loop prevention — the broker policy (PM's round cap + converge)

The classic agent-chat killer: A asks B, B's reply routes to A, A replies...
Three stacked guards, all broker-side, none agent-side:

- **Hard round cap** (PM's number, kept): **10 rounds** per agent↔agent thread
  without a human touch. Any human line in the thread resets the counter.
  At cap: agents in the thread go **idle-mute** (not disconnect — recovery
  friendly; PM can revive with one message). A visible system line:
  `[room] round cap reached (supervisor↔coder) — agents muted until next PM line`.
- **Converge as referee** (PM's code, integrated): each agent↔agent exchange is
  scored by `should_stop(responses, turn)`. `prob_stop >= 0.50` → thread ends
  *before* the cap, gracefully. This is the "thank you / you're welcome"
  detector — `is_politeness_only()` + the 10 signals. It runs **in the broker**,
  never inside an agent's context (a looping agent can't argue with a referee
  it can't see).
  - **Port note:** converge's RF was trained on the PM's prior agents' phrasing.
  It needs a validation pass against develcakes transcripts (Coder's hedging,
  Supervisor's long replies) before its numbers are trusted; possibly a
  retrain with `converge/train.py` once we log real agent↔agent exchanges.
- **No re-route of a routed reply.** An agent's *reply* to a routed thread
  carries the thread's `to` (the original addressee) unless it starts a NEW
  explicit @-mention. This kills ping-pong proliferation structurally.

### 3.4 What the broker is NOT doing (v1)

- No JEV "is this message contributing" filter on general traffic (rejected —
  open-ended judgment, JEV mediocre even on the closed set; converge does this
  job better, and only inside active agent↔agent threads).
- No agent-initiated broadcasts (an agent line can address one or more agents
  by mention; it cannot wake the whole room).
- No sockets, no serialization, no reconnect logic. In-process only.

---

## 4. Delivery phases — small first, prove, then promote

**Phase 1 — envelope + bus + agent→agent @-mention routing (in-process).**
The core feature. Supervisor can say `@coder fix the login bug in auth.py` in
the room and Coder wakes with it as a user message; PM sees the whole exchange
in the transcript; round caps and converge referee active. **This is the
"prove group chat with agents is viable" milestone.**

**Phase 2 — converge integration hardening.** Retrain/validate converge on
develcakes transcripts; log every agent↔agent thread with its referee verdicts
for the training corpus. Cap tuning with real data.

**Phase 3 — transport promotion (the PM's server/client picture).** The bus
gets a socket implementation; RoomMessage serializes 1:1 (that's why the
envelope exists); agents connect as real clients with keys; humans connect
with the DevelCakes client; room log becomes durable (SQLite today's shape →
server store). **Adopt MCP or A2A here if interop is ever wanted — do NOT
hand-roll a wire protocol** (§8). Nothing in phases 1-2 changes.

**Phase 4 — multi-human, roaming, persistence for real.** Multiple PMs,
multiple machines, late join, replay. The product picture fully live.

Each phase proves the previous one at the smallest possible scope. No phase
starts before the previous is live-verified by the PM in the actual app.

---

## 5. What this fixes (concrete pain, named)

- The "on your orders" opacity: PM had to reconstruct what Supervisor delegated
  to Coder from memory. With agent→agent routing **in the room**, delegation is
  a visible line: `PM sees: @coder implement V8 wording per spec`.
- The `@coder` theater tonight: Supervisor "said hi" to Coder and nothing
  delivered. The resolver wasn't consulted — the gap this spec closes.
- The "stop getting ahead of yourself" authority fights: delegation becomes
  interjectable mid-stream instead of a committed `/ask` payload.
- Loop risk that any agent-to-agent channel creates: guarded by caps +
  converge + no-re-route, broker-enforced.

---

## 6. Costs, honestly stated

- **Every agent line may spawn turns in other agents.** Cost multiplies.
  Mitigation: @-mention-only routing (no broadcast), round caps, converge
  early-stop. Token spend is bounded by design, and every routed exchange is
  visible in the room for the PM to kill.
- **converge needs real training data.** Its current RF is fitted to the PM's
  prior agent phrasings. Phase 2 exists because this is real work, not a
  checkbox.
- **The envelope is a contract.** Getting `to`/`kind`/`turn` slightly wrong
  now costs retries later. That's why this proposal exists before code.
- **Autonomy increases.** Supervisor can act on Coder directly. Every
  increase in agent autonomy needs the visibility and interruptibility this
  spec bakes in, or it recreates the PM's exact frustration, automated.

---

## 7. Open questions for the PM (decisions needed before Phase 1 implementation)

1. **Room PM echo for agent→agent lines:** when Supervisor says `@coder ...`,
   do Debugger and AlphaCoder see it as a `[room]` PM line (visibility
   symmetry with today's PM→agent behavior), or is agent→agent private?
   Supervisor's lean: **symmetry — everyone in the room sees the line**,
   because the whole point is visible collaboration.
2. **Does a human line reset ALL agent↔agent thread counters in the room, or
   only threads the human addressed?** Lean: **all threads in the room** —
   a human speaking is a context change for everyone.
3. **Turn caps: is 10 the right number for our agents specifically?** PM's
   original sketch said ~10; converge's QAC floor says ≥3. Somewhere between
   3 and 10 is tunable with data; start at PM's 10, log, tune in Phase 2.
4. **Mute vs. disconnect at cap** (this proposal says idle-mute, PM's original
   sketch said disconnect — confirm which).
5. **Does the envelope carry cost/token accounting per message** (for the
   `/cost` surface and cap enforcement budget), or is that phase 3+?

---

## 8. Explicitly rejected (with reasons, so we don't relitigate)

- **"A2ACP" — a hand-rolled agent-to-agent wire protocol.** A protocol is a
  promise to the world; we have one app, one team. The *envelope* gives us all
  the structure without the ceremony. If interop ever matters: **adopt MCP or
  A2A**, existing protocols with momentum. Never invent.
- **JEV as contribution-judge on all traffic.** Open-ended judgment; JEV was
  mediocre even on the 5-way closed set without tuned prompts. converge does
  this better and locally; and it only runs inside agent↔agent threads where
  the question is well-posed.
- **Sockets in v1.** Prove it in-process first (this whole document's thesis).
- **Agent-initiated broadcasts.** An agent can @-mention; it cannot `all`.

---

## 9. Acceptance sketch (what "done" looks like for Phase 1)

- PM types `@coder hi coder` in the room → Coder wakes, replies in the room,
  Supervisor + others see the exchange. **The exact test that failed tonight.**
- Supervisor replies `@coder implement V8 wording per spec §2` → Coder wakes
  with it as a user message → does the work → PM sees delegation *and* result
  in the room transcript, no `/ask` archaeology.
- A seeded infinite loop (two agents instructed to echo each other) is
  stopped by: converge early-stop OR the round cap, whichever first; a system
  line in the transcript records which.
- The room transcript shows, for every routed message: who sent, who was
  addressed, and the routing decision (V7 output or @-mention). Inspectable
  after the fact.
- Zero behavior change for PM→agent routing as shipped tonight (V7 live-
  verified paths still pass).

---

*Draft ends. Nothing in this document is implemented, ordered, or committed.
Awaiting PM ruling on §7 questions, then this graduates from proposal to spec
with phase 1 delegated to Coder under supervision, audited by Debugger.*