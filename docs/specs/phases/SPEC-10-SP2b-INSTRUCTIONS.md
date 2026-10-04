# SPEC-10 SP2b — ARH wiring: turn-complete agent checkpoints (worktree-aware)

**Spec:** `docs/specs/SPEC-10-REVIEW-QUEUES.md` §3 (data flow), AC#1
**Pre-flight (REV 2+3+4b, binding):** `docs/specs/phases/SPEC-10-PREFLIGHT-DECISIONS.md`
— D2 (REV 2), D2b, D5b, D8b (REV 2), D8c, D9b
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `ui/handlers/agent_runtime_handler.py` (checkpoint wiring) +
`tests/test_agent_runtime.py` (new test block; file is large — APPEND a new
class, do not refactor existing blocks). **No UI changes** (SP3).

---

## 1. What D2 REV 2 requires (read the decisions doc first)

Agent-side checkpoints fire at **turn completion for COMPLETED turns only**,
for **writer agents**, when the active project's review state is
`review_mode == "review"` AND `state.is_active()` (D2b — same gate as the
existing `needs_review` card flagging).

Attribution is snapshotted at **turn dispatch** (after
`_prepare_turn_conversation` resolves the worktree), keyed by
**turn_token** — never read `_active_project` at completion time.

The checkpoint runs on a **background daemon thread** (D2), under the
**project accept lock** (D3 REV 4b — it is a mutating git call on a tree;
for worktree items the lock keyed by PROJECT still applies per D3's
per-project serialization rule — see §3 below), with the **two-gate
stop-all check** (D8b REV 2).

## 2. Edits in `ui/handlers/agent_runtime_handler.py`

### 2a. Dispatch-time attribution snapshot

Where the turn token is assigned (~line 1225–1235,
`new_token = object(); self._turn_tokens[session_key] = new_token`):

Add a parallel dict:

```python
        self._turn_tokens[session_key] = new_token
        # SPEC-10 SP2b (D2 REV 2): dispatch-time attribution snapshot for the
        # turn-complete agent checkpoint. None until _prepare_turn_conversation
        # resolves the write cwd; read at completion via the token's entry.
        self._turn_attr: dict[str, tuple[str, str, str]] = {}   # in __init__
```

At the point `_prepare_turn_conversation` returns in the
`_prepare_turn()` closure (or immediately after the call at ~1238), record:

```python
            attr = self._turn_attr.get(session_key)
```

— actually record INSIDE `_prepare_turn_conversation` right after the
conv.project_path reconciliation completes (after the `if worktree_cwd is
not None:` block, ~line 1437), keyed by the CURRENT token:

```python
            # SPEC-10 SP2b (D2 REV 2): snapshot (project_name, project_path,
            # write_cwd) for this turn — the completion-side checkpoint reads
            # THIS, never _active_project (project-tab switch mid-turn must
            # not mis-attribute).
            self._turn_attr[session_key] = (
                project_name, project_path, conv.project_path,
            )
```

(project_name/project_path are the parameters of the turn — thread them
from `send_to_special_agent`'s call of `_prepare_turn_conversation` if not
already visible; check the signature at ~1317.)

### 2b. Completion-side checkpoint (`_do_response_complete`)

After the stale-token guard passes and BEFORE the rendering work — a
compact call, not inline git:

```python
        # SPEC-10 SP2b (D2 REV 2): turn-complete agent checkpoint.
        self._maybe_agent_checkpoint(session_key, complete_token)
```

New method:

```python
    def _maybe_agent_checkpoint(self, session_key: str, turn_token: object) -> None:
        """D2 REV 2: COMPLETED-turn checkpoint for writer agents under an
        active review session. Attribution from the dispatch-time snapshot
        (_turn_attr keyed by token freshness); runs on a background daemon
        thread; two-gate stop-all (D8b); failure is non-fatal (D8c)."""
        agent_def = self._agents.get(session_key)
        if not getattr(agent_def, "can_write", False):
            return
        current_attr = self._turn_attr.get(session_key)
        if current_attr is None:
            return
        # token freshness: _turn_tokens[session_key] IS the token we were
        # handed → the snapshot belongs to this turn
        if self._turn_tokens.get(session_key) is not turn_token:
            return
        project_name, project_path, write_cwd = current_attr
        rh = self._review_handler
        if rh is None:
            return
        state = rh.get_state(project_name)
        if state is None or not (state.review_mode == "review" and state.is_active()):
            return
        # D8b REV 2 gate 1 (pre-flight, before the thread)
        if self._agent_runtime_handler is not None and \
                self._agent_runtime_handler.stop_all_in_progress():
            ...
```

**STOP.** ARH IS `self` here — there is no `self._agent_runtime_handler` on
ARH (that's ReviewHandler's reference). The stop-all gate on ARH reads its
own runtime registry — find the real stop-all signal on ARH (grep
`stop_all` in this file; SP3 wired `stop_all_agents` and
`stop_all_in_progress` — locate where they live and use the correct one;
if `stop_all_in_progress` is a ReviewHandler-side helper that reads ARH,
then ARH owns the flag). **Investigate before writing — do not fabricate an
API.** (This paragraph is the brief's own warning: the shape below assumes
`self.stop_all_in_progress()` exists on ARH; verify and adapt.)

The thread body (D2): is_repo/init if needed → stage_all → gate 2
(stop-all re-check just before commit) → `git_ops.commit(write_cwd,
"[review] agent checkpoint", allow_empty=True, agent_trailer=session_key)`
→ on success `rh._enqueue(project_name, session_key, QueueEntry(...,
path_used=write_cwd, ts=utcnow))`... **use the PUBLIC surface where one
exists** — `_enqueue` is private; if ReviewHandler exposes a public
enqueue (check SP2's final surface: `agents_with_pending`, `pending_count`,
`accept_agent_queue`, `accept_all_queues` — SP2's `_enqueue` stayed
private), add a thin PUBLIC `enqueue_agent_checkpoint(project, agent_key,
sha, path_used)` on ReviewHandler in this phase (5 lines + docstring; it
belongs to the SP2 surface and Coder may add it now — disclose).

**Locking:** the checkpoint's stage/commit runs under
`rh._project_lock_for(project_name)`? — same lock family, but keyed by
project while the tree may be a WORKTREE. D3's serialization rule is
per-project on the project root; worktree trees are agent-private (one
writer), so a worktree checkpoint needs NO project lock (no cross-thread
mutator of that tree); a project-root checkpoint DOES take the project
lock (PM/accept paths mutate the root). Implement exactly that
distinction, with a comment citing D3 REV 4b.

**allow_empty=True rationale:** the checkpoint is a SHA marker (D8c);
agent-tree empty checkpoints are the sweep mechanism for cancelled-turn
work (D2 REV 2's superset property).

### 2c. Cleanup

Pop `_turn_attr[session_key]` when a new turn starts (dispatch overwrites
it anyway — a pop-on-new-dispatch keeps the dict bounded) and on
`clear_conversation`/session teardown if such a hook exists (grep; keep it
bounded via overwrite-discipline if no hook).

## 3. Tests (append class `TestSpec10AgentCheckpoints` to
`tests/test_agent_runtime.py`)

Use the file's existing ARH test doubles (grep for how tests construct ARH
without GTK). RED-first:

1. `test_completed_writer_turn_checkpoints_under_review` — writer agent,
   review session active, turn completes → `pending_count(project, sk) ==
   1`, entry's `path_used` == the conv's write cwd, commit on the right
   tree carries `Agent: <session_key>`.
2. `test_non_writer_no_checkpoint` / `test_review_off_no_checkpoint` /
   `test_no_active_session_no_checkpoint` — gate pins (D2b).
3. `test_cancelled_turn_no_checkpoint` — COMPLETED-only (the runtime
   dispatches on_error for CANCELLED — simulate a completed+cancelled pair
   if a direct harness is impractical: assert the checkpoint helper is not
   invoked when completion never fires... at minimum pin the dispatch
   contract via the token-freshness guard test below).
4. `test_stale_token_skips_checkpoint` — rotate the token after the
   snapshot; completion with the old token → no enqueue.
5. `test_tab_switch_attribution` — snapshot at dispatch says project A;
   `_active_project` switched to B before completion → entry lands under
   project A (path_used from the snapshot).
6. `test_stop_all_gates_agent_checkpoint` — gate 1 (pre-thread) and gate
   2 (before-commit) both abort with no commit + no enqueue.
7. `test_checkpoint_failure_non_fatal` — commit fails (mock) → turn
   completion unaffected, no enqueue, no error card into agent chat (D8c).
8. `test_worktree_turn_checkpoints_in_worktree` — leased writer (worktree
   cwd) → commit lands in the worktree (branch `agent/<id>`), entry
   `path_used` = worktree path; NO project-lock acquisition on the root
   (assert via lock-state probe or by the test simply not deadlocking
   against a held root lock — hold the root project lock in the test
   thread while the checkpoint runs; it must complete).

## 4. Battery (paste all)

- `xvfb-run -a .venv/bin/python -m pytest tests/test_agent_runtime.py -q`
  (full file — this is the big one; expect several minutes)
- `xvfb-run -a .venv/bin/python -m pytest tests/test_review_queues.py tests/test_stop_all.py -q`
- ruff multiset vs HEAD on agent_runtime_handler.py (baseline is 24
  findings — measure at HEAD first)
- pyright on agent_runtime_handler.py vs the 17-error baseline (SP3's
  registered pre-existing set — zero NEW errors)
- RED proofs for the new tests

## 5. COMPLETENES (mandatory)

- [ ] Edit 1: _turn_attr + dispatch snapshot in _prepare_turn_conversation
- [ ] Edit 2: _maybe_agent_checkpoint (verified stop-all API — no
      fabricated API) + completion call
- [ ] Edit 3: public enqueue_agent_checkpoint on ReviewHandler (disclosed)
- [ ] Edit 4: lock distinction (worktree none / root project lock)
- [ ] Edit 5: bounded _turn_attr discipline
- [ ] Tests 1–8 RED-first
- [ ] Battery + baselines pasted
- [ ] Related issues found, NOT fixed (flagged)

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
