# SPEC-09: Safety Gates — Stop-All, Per-Coder Worktrees, Work Claiming

**Date:** 2026-09-20
**Author:** Supervisor (develcakes v2)
**Status:** Draft — for implementation
**Implements:** .crabcakes/architecture.md §Modules (Worktrees, Work claiming, Stop-all)
**Depends on:** SPEC-08 (transcript store lands before roster safety)
**Target branch:** main

> Architecture compliance: stop-all = everything in flight, mid-tool-call included (PM
> ruling #6); worktrees one-per-writer; leases with TTL, double-claim refused.

---

## 1. Overview

**Problem.** N autonomous writers need three safety rails before any roster expansion:
1. **Stop-all** — one action halting every agent; today `/stop` is per-agent and can't
   reach a mid-tool-call turn.
2. **Worktrees** — multiple coders in one working tree clobber each other.
3. **Work claiming** — N coders collide on the same work unit without a lease.

**Solution.**
1. Runtime turn registry → `stop_all()` cancelling every in-flight turn (tool-call
   included) + review-layer checkpoint abort.
2. `utils/worktree_manager.py`: one `git worktree` per writer agent; runtime executes
   writers with cwd = worktree path; merges only through review gate.
3. `utils/work_persistence.py` extended: claim/release/assert with TTL lease.

**Scope**

| In | Out |
|---|---|
| Turn registry + stop_all + toolbar button | Roster expansion itself (post-MVP per requirements) |
| worktree_manager.py | Merge automation (review gate is manual) |
| Lease extension to work_persistence | JEV/group-chat |
| Tests incl. mid-tool-call halt | |

## 2. Changes by File

### agent/runtime.py — turn registry + stop_all

Existing pieces (verified): `_turn_state: dict[(sk, token) → TurnStatus]`,
`_active_loops: set[str]`, `_turn_tokens: dict[sk → token]`, `cancel(session_key)`
methods; `_dispatch_approval` blocks on `event.wait(timeout=60)` — the mid-tool-call
window.

Add on AgentRuntime (and aggregate at handler level):

```python
def stop_all(self) -> dict[str, str]:
    """Cancel every in-flight turn on this runtime. Returns sk → outcome."""
    outcomes = {}
    for sk in list(self._active_loops):
        outcomes[sk] = self.cancel(sk)   # existing cancel handles token rotation + termination
    return outcomes
```

Handler-level (agent_runtime_handler.py) iterates `self._runtimes` calling `rt.stop_all()`
— plus sets every pending approval event to denied (unblocks `_dispatch_approval`
waiters): verified `_pending_approvals` structure `key → {"event", "result_ref", ...}`
(approve_exec pops it; stop_all sets `result_ref[0] = False; event.set()` for all).

**Mid-tool-call halt path:** `cancel()` from the main thread sets the turn token stale
(verified stale-token rejection in `_terminate_turn` :614 — "stale turn_token... result
rejected") and the loop's next check terminates; a **running tool subprocess** is halted
by cancelling its process — extend the tool executor's process registry
(`agent/tools.py` exec_command tracks Popen handles) with a `cancel_all_processes()`
hook the handler calls in stop_all. (Builder verifies the executor's Popen tracking
during implementation; if untracked today, add a module-level registry keyed by
session_key.)

### utils/worktree_manager.py (NEW)

```python
class WorktreeManager:
    """One git worktree per writer agent. Merges only through the review gate."""

    def __init__(self, repo_path: str):
        self._repo = GitPython Repo(repo_path)   # verified dep: git_ops.py uses GitPython

    def ensure_worktree(self, agent_id: str) -> str:
        """Create <repo>/.worktrees/<agent_id> if missing; returns path."""
        # git worktree add -b agent/<agent_id> <path>

    def path_for(self, agent_id: str) -> str | None: ...
    def remove_worktree(self, agent_id: str) -> None: ...
    def list_worktrees(self) -> dict[str, str]: ...
```

Runtime integration: `_prepare_turn_conversation` (verified:
ui/handlers/agent_runtime_handler.py:1053) gains an optional `cwd_override` — writer
agents' conversations get `project_path = worktree path` (tools sandbox to project_path;
verified: `resolve_session_workspace(conv.project_path, session_key)` and
`execute_tool(..., conv.project_path, ...)`). Worktree branches merge via the review
layer's existing accept-commit path.

### utils/work_persistence.py — lease extension

Verified current shape: work units persisted in `.crabcakes/work.json` via
WorkUnitStore; no claim/lease today (grep `claim|lease|ttl` → no matches). Add:

```python
@dataclass
class WorkLease:
    unit_id: int
    holder: str            # session_key
    claimed_at: float      # time.time()
    ttl_seconds: float = 900.0

def claim_work(store, unit_id: int, holder: str, ttl: float = 900.0) -> WorkLease | None:
    """Claim if unclaimed or lease expired. None = refused (double-claim)."""

def release_work(store, unit_id: int, holder: str) -> bool: ...
def assert_lease(store, unit_id: int, holder: str) -> bool: ...
```

Store-level atomicity: WorkUnitStore persists via atomic .tmp+rename (verified pattern);
claim writes lease into the unit record under the store's own lock.

### ui/toolbar.py — stop-all button

New `■ Stop All` button (distinct from per-agent `/stop`): calls handler
`stop_all_agents()`; confirmation dialog (destructive action); emits one feed card
listing cancelled sessions + aborted checkpoints.

### Review-layer abort

review_handler checkpoint commit (verified: `git_ops.commit(project_path, "[review]
checkpoint", allow_empty=True)` path) gains a pre-commit gate: if stop_all in progress
(flag on handler), abort with card instead of committing.

## 3. Data Flow

PM clicks Stop All → confirm → `stop_all_agents()` → per-runtime `stop_all()` →
per-session `cancel()` (token rotation + FAILED/CANCELLED termination) + approval
waiters denied + tool processes killed → review checkpoints aborted → summary feed card.

`/work start #N` → `claim_work(store, N, sk)` (lease) → worktree ensure → runtime with
project_path = worktree → commits on agent branch → review → merge via accept.

## 4. File Change Summary

| File | Change | ~Lines | Risk |
|---|---|---|---|
| agent/runtime.py | stop_all + process-registry hook | +45 | med-high |
| ui/handlers/agent_runtime_handler.py | aggregate stop_all + approval flush | +50 | med |
| agent/tools.py | Popen registry + cancel_all | +40 | med |
| utils/worktree_manager.py | new | +160 | med |
| utils/work_persistence.py | lease API | +120 | med |
| ui/toolbar.py | button + confirm | +50 | low |
| tests (4 areas) | new | ~400 | — |

## 5. Implementation Order

1. Lease API + tests (double-claim refused, TTL expiry, release).
2. worktree_manager + tests (create/execute-in/remove).
3. stop_all runtime + handler + toolbar + review abort.
4. Mid-tool-call halt test (test-proven per requirements AC #7).
5. Full suite + ruff + pyright.

## 6. Acceptance Criteria

- [ ] Stop-all halts an agent **mid-tool-call** (test-proven: exec_command killed,
      turn terminates CANCELLED)
- [ ] Approval waiters unblocked (denied) by stop-all — no 60s hangs
- [ ] In-flight review checkpoint aborts (no commit lands)
- [ ] Same-file writers land in separate worktrees (two runtimes, one repo, distinct
      paths, both suites green)
- [ ] Double-claim refused; stale lease (TTL expired) re-claimable; release works
- [ ] Full pytest green, ruff clean, pyright clean

## 7. Edge Cases

| Case | Behavior |
|---|---|
| Stop-all during stream | Stream loop's next event checks turn token → CANCELLED termination (existing stale-token path) |
| Tool subprocess ignores SIGTERM | Registry escalates to SIGKILL after 2s (subprocess.run timeout pattern) |
| Lease holder crashes (no release) | TTL expiry (default 15 min) makes unit re-claimable; assert_lease logs |
| Worktree dir deleted externally | ensure_worktree recreates on next claim; feed card notes recreation |
| Stop-all with nothing in flight | No-op + card "0 turns in flight" |

## 8. ARCHITECTURE.md Updates

§Modules/Stop-all, Worktrees, Work claiming — mark implemented.
