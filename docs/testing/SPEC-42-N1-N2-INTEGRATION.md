# SPEC-42: Integration targets N1 + N2 — writer turn end-to-end; compiled wire

**Date:** 2026-10-19
**Author:** Supervisor
**Status:** Draft — for implementation
**Implements:** integration targets N1 and N2 of
`docs/testing/MVP-INTEGRATION-VALIDATION-PLAN-v2.md` (read it first — it
carries the reconciliation table and the ordering rationale)
**Depends on:** SPEC-09 (worktrees + leases), SPEC-10 (checkpoints/queues),
SPEC-24 (compiled packets), SPEC-35 (per-session persistence), SPEC-40
(wire-aware guard), SPEC-41 phase 2 (wire adjacency)
**Target branch:** main

> Architecture: one test file per target, both under
> `tests/test_mvp_integration.py` (new module, two classes). The tests drive
> REAL modules — real `WorktreeManager`, real `agent/persistence.py`, real
> `serialize_messages` — with fakes ONLY where a provider/network sits.
> Mocking the seam under test is forbidden (that was the WU-10 lesson: the
> mock had `remove_card`, so the suite was green against a crash).

---

## DISCOVERY (verified by Supervisor 2026-10-19)

- `tests/test_agent_runtime.py::TestSpec10AgentCheckpoints` (:6472) already
  builds: real ARH + real ReviewHandler + real git repo (`_sp2_git_repo`)
  + writer agent def (`_sp2_writer_def`) + an active review session +
  `_finish(handler)` driving `_do_response_complete`. Its `_wired()` helper
  returns `(handler, rh, pstr, token)`. **N1 builds on this pattern.**
- `tests/test_review_queues.py` has `_make_handler` / `_activate` /
  `_entry` queue doubles (:46-102).
- `utils/work_persistence.py`: `claim_work(project_path, unit_id, holder,
  ttl)` (:598) → `WorkLease | None`; same-holder re-claim refreshes
  (heartbeat); `find_live_lease(project_path, session_key)` drives
  `_worktree_for_turn`.
- `ui/handlers/agent_runtime_handler.py::_worktree_for_turn` (:923):
  writer + live lease → `WorktreeManager.ensure_worktree(worktree_id)`,
  worktree id = `WorktreeManager.worktree_id_for_session(session_key)`;
  any failure → None (project root). Worktrees live under
  `<repo>/.worktrees/<agent_id>` (agent/enforcement.py:81).
- `agent/persistence.py`: `save_conversation_to_disk(conv, session_key)` /
  `load_conversation_from_disk(session_key)` — one JSON file per session.
- `agent/runtime.py::_compiled_messages` (:3492): packet + `wire_from` +
  hot windows → ONE `serialize_messages(turn + tail)` pass (SPEC-41 phase
  2). `_maybe_compile` (:3716) stores `after`/`wire_from`/`hot`.
- `agent/llm/convert.py::normalize_tool_history` — the adjacency normalizer
  (idempotent; orphan tool results demoted by the serializer; the runtime
  one-pass serialize makes demotion unreachable for the compiled path).
- `Conversation(agent_name=...)` is required (positional); `ToolCall(
  call_id=..., tool_name=..., arguments=...)` is the tool-call record.
- Verified live by Supervisor: the split-shape transcript
  `[user, assistant(w+r), tool(write), user, tool(read)]` serializes with
  one pass to all five rows (no demotion) — the N2 fixtures reuse this shape.

---

## 1. Overview

### N1 — writer turn end-to-end

**Question:** does ONE writer turn name itself identically at every stage —
lease, worktree, enforced write, checkpoint commit, queue attribution,
persistence file?

**Test (one long test, real modules, fakes only for provider/network):**
1. Real git project in `tmp_path`; `claim_work(project, "00000001",
   "special:coder")` → live lease recorded in `.crabcakes/work.json`.
2. Real `FeedTab` NOT needed; real `WorktreeManager(project)` +
   `_worktree_for_turn(session, writer_def, project)` → a worktree path
   under `<project>/.worktrees/`; assert it is a git worktree of the
   project (`git worktree list`) and is not the project root.
3. Enforce the turn's write INSIDE the worktree: write a file whose path is
   under the worktree (the same call enforcement makes — reuse
   `agent/enforcement` helpers if cheap, else a plain write + the existing
   enforcement result shape).
4. Drive `_do_response_complete(sk, "done", token)` (the `_wired` pattern)
   → checkpoint commit lands IN THE WORKTREE with the `Agent: special:coder`
   trailer; queue entry `path_used` == the worktree path.
5. `agent/persistence.save_conversation_to_disk(conv, session)` → the JSON
   file exists under the ACTIVE config dir and `load_conversation_from_disk`
   round-trips the turn (the enforced write is in the transcript).
6. `accept_agent_queue(...)` → entry reviewed, no duplicate commit.

**What may be faked:** the LLM response itself (`_do_response_complete` is
the seam — it is the real post-provider handler). **What may not:** lease,
worktree, checkpoint, queue, persistence.

### N2 — compiled wire ↔ normalization ↔ provider adjacency

**Question:** does EVERY packet shape we emit serialize to a wire where
every declared tool_call id carries a tool row — i.e. no MiniMax-2013
shape can leave the compiled path?

**Tests (parametrized over packet shapes; all through
`_compiled_messages` + `serialize_messages`, no provider):**
1. `wire_from` splits an assistant from its own results (the audit's
   BUG#2 shape) — both tool rows present, no demotion.
2. Parallel calls + interleaved nudge (the reproduction from SPEC-41).
3. Hot windows present: byte-mode and line-mode both replay (`line=42`
   header, no `offset=0` for a line capture), and the replay survives
   `normalize_tool_history` idempotently (`normalize(normalize(x)) ==
   normalize(x)` on the assembled wire).
4. Hot windows dropped by the over-budget retry (`hot=False`) — wire still
   adjacency-clean.
5. `wire_from` absent (legacy packet) → tail-only slice, unchanged shape.
6. Every shape ALSO passes through `normalize_tool_history` unchanged
   (idempotency lock) — proving the two layers agree.

**What may be faked:** the provider. **What may not:** `_maybe_compile`,
`_compiled_messages`, `serialize_messages`, `normalize_tool_history`.

---

## 2. Changes by File

| File | Change |
|---|---|
| `tests/test_mvp_integration.py` | NEW module: `TestN1WriterTurnEndToEnd`, `TestN2CompiledWireAdjacency` |
| (nothing else) | production code must not change — if a test finds a defect, STOP and report; defects become fix rounds |

Reuse from `tests/test_agent_runtime.py`: `_sp2_make_handler`,
`_sp2_git_repo`, `_sp2_writer_def`, `_spec10_rh`, `_wired`, `_finish` —
import or copy (disclose which). Reuse from
`tests/test_feed_retention.py`: the `_LiteHtmlSurface` double pattern if a
feed surface is needed (N1 does not need one).

## 3. Data Flow

N1: `claim_work` → lease in work.json → `_worktree_for_turn` →
`ensure_worktree` → write under worktree → `_do_response_complete` →
SPEC-10 checkpoint (worktree commit, `Agent:` trailer) → queue entry
(`path_used` = worktree) → `accept_agent_queue` → per-session JSON updated.

N2: `_maybe_compile` (packet: goal/hunk/checks/hot/after/wire_from) →
`_compiled_messages` (turn+tail one-pass + hot replay) →
`normalize_tool_history` (idempotent no-op on clean wire) →
`serialize_messages` (adjacency contract) → provider-shaped rows.

## 4. Implementation Order

1. N2 first (pure runtime, no GTK): shapes 1-6, one class.
2. N1 second (real git + real ARH wiring): the six-step long test.
3. Full suite backgrounded per the gate protocol.

## 5. Acceptance Criteria

- [ ] N1: every stage of the six-step chain asserts the same turn identity
      (lease holder == session; worktree path == queue `path_used`;
      checkpoint trailer names the agent; persistence file belongs to the
      session).
- [ ] N1: worktree commit lands in the WORKTREE, not the project root.
- [ ] N1: accept produces no duplicate commit.
- [ ] N2: all six shapes produce a wire with zero demoted tool results.
- [ ] N2: `line=42` hot window replays as `line=42`.
- [ ] N2: `normalize(normalize(wire)) == normalize(wire)` for every shape.
- [ ] N2: legacy packet (no `wire_from`) wire is byte-identical to today's.
- [ ] Full suite: 46F/5325P/5S baseline + new tests; failure set diffed
      identical (`/tmp/p41b_fails.txt`).

## 6. Edge Cases

| Case | Expected |
|---|---|
| Lease expires mid-turn | `_worktree_for_turn` → None → project root; N1 asserts the fallback exists (secondary assertion) |
| Non-git project | worktree None; N1 stays on the git happy path (out of scope here) |
| Hot window body > 16 KiB | capped at capture; replay shows the cap |
| `wire_from` > `after` | treated as absent (existing guard) |
| Provider timeout mid-N1 | out of scope (N4's target); N1 fakes the response |

## 7. ARCHITECTURE.md Updates Required

- Testing section: `tests/test_mvp_integration.py` is the cross-seam
  module; mocks may not cover a seam under test.

---

## Self-Audit (Rule 9)

- All anchors verified this session: `_worktree_for_turn` (:923),
  `claim_work` (:598), `_compiled_messages` (:3492), `_maybe_compile`
  (:3716), `_wired`/`_finish` (test_agent_runtime :6510/:6526),
  `_sp2_*` helpers (:6244-:6285), `Conversation(agent_name=...)`,
  `ToolCall(call_id=...)`, the split-shape serialization (reproduced live).
- Mock policy explicit; reuse list disclosed.
- Acceptance criteria are assertions, not hopes.
