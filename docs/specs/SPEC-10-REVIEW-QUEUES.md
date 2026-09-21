# SPEC-10: Review Layer — Per-Agent Queues + Batch Accept

**Date:** 2026-09-20
**Author:** Supervisor (develcakes v2)
**Status:** Draft — for implementation
**Implements:** .crabcakes/architecture.md §Modules/Review layer (PM decision:
both per-agent queues AND batch accept)
**Depends on:** SPEC-09 (worktrees + claiming land first — queues key off agent identity)
**Target branch:** main

> Architecture compliance: per-agent checkpoint queues keyed by commit trailers;
> batch-accept over the queue set; per-worktree review for parallel coders.

---

## 1. Overview

**Problem.** The review layer is single-queue: checkpoints from all agents interleave in
one stream (ReviewHandler + review_bar). With N coders, the PM can't review Coder-2's
work as a unit, and accepting fifteen checkpoints one-by-one is friction.

**Solution.**
1. **Agent attribution in git** — every agent commit gains an `Agent: <session_key>`
   trailer (checkpoint + accept commits).
2. **Per-agent queues** — review state gains a queue keyed by agent; the review bar
   shows a per-agent filter/queue selector.
3. **Batch accept** — one action accepting all pending checkpoints in the selected
   queue (or all queues), each via the existing accept-commit path.

**Scope**

| In | Out |
|---|---|
| git_ops trailer support | Auto-merge logic (gate stays manual per-unit) |
| ReviewHandler per-agent queues | Review-mode redesign (exec gate untouched) |
| review_bar queue selector + batch accept | |
| Tests | |

## 2. Changes by File

### utils/git_ops.py — trailers

Verified current: `git_ops.commit(project_path, message, allow_empty=True)` used by
ReviewHandler (checkpoint at review_handler.py:245). Extend:

```python
def commit(project_path: str, message: str, allow_empty: bool = False,
           agent_trailer: str | None = None) -> str:
    """Commit; when agent_trailer set, append 'Agent: <key>' trailer line."""
    if agent_trailer:
        message = f"{message}\n\nAgent: {agent_trailer}"
    ...  # existing GitPython commit
```

(Callers pass `agent_trailer=conv.session_key` where a conversation context exists.)

### ui/handlers/review_handler.py — queues

Verified: ReviewHandler owns checkpoint/diff/accept/reject; review state
(`models/review_state.py`, 1,213 bytes) holds checkpoint_sha + mode. Add:
- `_queues: dict[str, list[str]]` — agent session_key → ordered card/commit list;
- `enqueue(agent_key, checkpoint_ref)` on checkpoint creation;
- `accept_agent_queue(agent_key)` — iterate queue, per-item existing accept path
  (commit w/ trailer), errors abort remaining + card;
- `accept_all_queues()` — union.

### ui/views/review_bar.py — queue selector + batch buttons

Verified: review_bar.py (6,977 bytes) renders reviewing state
(`bar.set_state_reviewing(state.checkpoint_sha)`). Add a dropdown (agents with pending
queues) + `Accept All (agent)` / `Accept All (everyone)` buttons with confirmation.

### Diff cards

`FeedCardData.metadata["agent"] = session_key` on diff cards (verified metadata dict
exists — feed_card.py:99) → feed filter-by-agent comes free for post-MVP group chat.

## 3. Data Flow

Agent write → enforcement → checkpoint (commit w/ `Agent:` trailer) → diff card
(metadata.agent) → `enqueue(agent_key, ref)` → review bar queue view → PM accepts one /
batch → per-item accept commits (trailer preserved) or rejects (reset to checkpoint).

## 4. File Change Summary

| File | Change | ~Lines | Risk |
|---|---|---|---|
| utils/git_ops.py | trailer param | +15 | low |
| ui/handlers/review_handler.py | queues + batch | +140 | med |
| ui/views/review_bar.py | selector + buttons | +90 | med |
| tests/test_review_queues.py | new | ~220 | — |

## 5. Implementation Order

1. git_ops trailer + tests (trailer present in `git log --format=%B`).
2. Queue bookkeeping + per-agent accept; batch accept; tests.
3. review_bar UI + confirmation; feed card metadata.
4. Full suite + ruff + pyright.

## 6. Acceptance Criteria

- [ ] Every agent commit carries `Agent: <session_key>` trailer (log-verifiable)
- [ ] Review bar shows per-agent queues; selecting filters the pending set
- [ ] Batch accept commits every pending checkpoint in queue; failure aborts
      remaining with an error card (no partial silent loss)
- [ ] Reject path unchanged per-item
- [ ] Full pytest green, ruff clean, pyright clean

## 7. Edge Cases

| Case | Behavior |
|---|---|
| Unattributed checkpoint (PM's own edits) | Queue key "pm"; never batch-accepted with agents' by default |
| Queue accept racing a new checkpoint | Handler lock serializes; new item lands in next batch |
| Reject mid-batch | Remaining items untouched; batch reports partial completion |
| Two agents in same worktree (misconfig) | Queue keys still distinct; review shows both — worktree invariant enforced by SPEC-09, not here |

## 8. ARCHITECTURE.md Updates

§Modules/Review layer — mark implemented; note trailer format.
