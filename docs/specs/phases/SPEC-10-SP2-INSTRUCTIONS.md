# SPEC-10 SP2 — ReviewHandler per-agent queues + batch accept

**Spec:** `docs/specs/SPEC-10-REVIEW-QUEUES.md` §2 (review_handler), §6 AC#2/AC#3
**Pre-flight (REV 2, binding):** `docs/specs/phases/SPEC-10-PREFLIGHT-DECISIONS.md`
— especially D3 (REV 2), D7b, D8, D9, D9b, D10b
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `models/review_state.py` (+QueueEntry), `ui/handlers/review_handler.py`
(queue store + enqueue + per-agent + batch accept), `tests/test_review_queues.py`
(new file). **No ARH wiring yet** (that is SP2b). **No UI yet** (that is SP3).

---

## 1. `models/review_state.py` — QueueEntry (pure data)

Append:

```python
from typing import NamedTuple
from datetime import datetime

class QueueEntry(NamedTuple):
    """One pending reviewable checkpoint, attributed to an agent (SPEC-10).

    agent_key: queue identity — an agent session_key (e.g. "special:coder")
        or the literal "pm" for PM-initiated checkpoints (D3/D8).
    sha: checkpoint commit SHA (full hex).
    path_used: ABSOLUTE, realpath'd tree the checkpoint committed in — the
        worktree for leased writers, else the project root (D2/D5b).
    ts: UTC enqueue time.
    """
    agent_key: str
    sha: str
    path_used: str
    ts: datetime
```

(File header docstring notes: pure data, no GTK, no git calls.)

## 2. `ui/handlers/review_handler.py` — queue store + accept paths

### 2a. `__init__` additions

```python
        # SPEC-10: per-agent review queues. project_name -> agent_key ->
        # ordered QueueEntry list. Guarded by self._queue_lock (D9b) —
        # enqueue arrives from ARH runtime threads, reads from the review
        # bar (main thread), batch accept from the PM's click.
        self._queues: dict[str, dict[str, list[QueueEntry]]] = {}
        self._queue_lock = threading.Lock()
```

(`threading` already imported in the file.)

### 2b. New public methods (place after `get_state`, before the project
lifecycle hooks section marker)

```python
    # ── SPEC-10: per-agent queues (D9b surface) ────────────────────────

    def agents_with_pending(self, project_name: str) -> list[str]:
        """Agent keys with ≥1 pending entry, in first-enqueue order.
        O(agents) under the lock; snapshot semantics."""
        with self._queue_lock:
            q = self._queues.get(project_name, {})
            return [k for k, v in q.items() if v]

    def pending_count(self, project_name: str, agent_key: str) -> int:
        """Pending entry count for one agent. O(1) lookup + len."""
        with self._queue_lock:
            return len(self._queues.get(project_name, {}).get(agent_key, []))

    def _enqueue(self, project_name: str, agent_key: str, entry: QueueEntry) -> None:
        """Append one entry; enforce the D9 cap (50/agent FIFO, drop-OLDEST
        with warning log + feed card). Caller must have validated the entry."""
        with self._queue_lock:
            q = self._queues.setdefault(project_name, {})
            lst = q.setdefault(agent_key, [])
            lst.append(entry)
            if len(lst) > 50:
                dropped = lst.pop(0)
                _logger.warning(
                    "review queue cap hit for %s/%s — dropped OLDEST %s",
                    project_name, agent_key, dropped.sha[:7],
                )
                self._emit_feed_card({
                    "title": f"Review queue cap: dropped oldest for {agent_key}",
                    "body": f"Entry {dropped.sha[:7]} exceeded the 50-entry cap.",
                    "project_name": project_name,
                    "commit_sha": dropped.sha,
                })
```

**IMPORTANT — the feed card emission inside the lock:** `_emit_feed_card` →
`FeedHandler.add_card` runs on the CALLER's thread. It must not re-enter the
queue lock (it doesn't — different object), but the emission happens while
holding `_queue_lock`. If the auditor finds this objectionable, the fix is to
collect the overflow card and emit AFTER `with` — **builder's choice, disclose
it**. (Emitter: use the existing `_emit_feed_card` helper; the card dict shape
matches its current contract.)

### 2c. `start_review` — enqueue PM checkpoint (GAP-3/D8)

In `start_review`'s `_do()`, after `sha = commit_result.sha` (inside the
thread, before `_update_state`):

```python
            # SPEC-10: PM checkpoints enter the "pm" queue (D8).
            self._enqueue(project_name, "pm", QueueEntry(
                agent_key="pm",
                sha=sha,
                path_used=state.project_path,
                ts=datetime.now(timezone.utc),
            ))
```

And the checkpoint commit itself gains the trailer:

```python
            commit_result = git_ops.commit(
                project_path, "[review] checkpoint", allow_empty=True,
                agent_trailer="pm",
            )
```

(`datetime`/`timezone` already imported in the file. Note: trailer
rejection for the literal "pm" is impossible — it's a clean str.)

### 2d. `accept_agent_queue(agent_key, project_name)` — per D3 REV 2

```python
    def accept_agent_queue(self, agent_key: str, project_name: str,
                           session_key: str | None = None) -> None:
        """SPEC-10 D3 (REV 2): accept every pending entry for one agent.

        Worktree items (path under <project>/.worktrees/): "mark reviewed"
        bookkeeping — NO new commit (D2's checkpoint already carries the
        Agent: trailer; an accept commit would be empty or livelock). Merge
        stays manual per-unit.
        Project-root items (unleased writers, "pm"): existing accept path —
        stage + commit with agent_trailer=<agent_key>.
        Snapshot-iterate; dequeue-on-success-only; stale items (worktree
        gone) drop with an error card and do NOT abort remaining; item-level
        git failures abort remaining with a partial-completion card.
        """
        state = self._states.get(project_name)
        if state is None:
            return
        sk = session_key or f"project:{project_name}"
        project_path = state.project_path

        def _do():
            with self._queue_lock:
                snapshot = list(self._queues.get(project_name, {}).get(agent_key, []))
            if not snapshot:
                self._GLib.idle_add(lambda sk=sk: self._on_display_text(
                    sk, f"No pending checkpoints for {agent_key}"))
                return
            from utils.worktree_manager import WORKTREES_DIR_NAME  # .worktrees

            outcomes: list[str] = []
            succeeded = failed = stale = 0
            worktrees_root = os.path.realpath(os.path.join(project_path, WORKTREES_DIR_NAME))
            for entry in snapshot:
                entry_root = os.path.realpath(os.path.dirname(entry.path_used) or entry.path_used)
                is_wt = os.path.dirname(os.path.realpath(entry.path_used)) == worktrees_root
                if is_wt:
                    # Worktree item — mark reviewed (D3 step 2). Verify the
                    # tree still exists (D3 step 1: path_for semantics —
                    # isdir + registered; we approximate with isdir on the
                    # recorded path + direct-child check; SP2b's ARH wiring
                    # will pass the manager's own path_for result at enqueue
                    # time, and SP3 re-verifies at click time).
                    if not os.path.isdir(entry.path_used):
                        stale += 1
                        outcomes.append(f"stale (worktree gone): {entry.sha[:7]}")
                        self._emit_feed_card({...error card...})
                        self._dequeue(project_name, agent_key, entry)
                        continue
                    succeeded += 1
                    outcomes.append(f"reviewed {entry.sha[:7]}")
                    self._dequeue(project_name, agent_key, entry)
                else:
                    # Project-root item — real accept commit (D3 step 3).
                    stage = git_ops.stage_all(project_path)
                    if not stage.success:
                        failed += 1
                        ...abort remaining with partial card...
                        break
                    commit = git_ops.commit(
                        project_path, f"[review] accepted: {agent_key} checkpoint {entry.sha[:7]}",
                        agent_trailer=agent_key,
                    )
                    if not commit.success:
                        failed += 1
                        ...abort remaining...
                        break
                    succeeded += 1
                    outcomes.append(f"accepted {entry.sha[:7]}")
                    self._dequeue(project_name, agent_key, entry)
            ...emit one summary card: agent, N accepted/reviewed, M stale-dropped, K failed...
```

**This is a SHAPE, not verbatim code** — the `...` ellipses are yours to fill
(card dicts, abort cards, `_dequeue` helper). Requirements the implementation
must meet (auditable):
- `_dequeue(project, agent, entry)` — O(1)-ish removal under the lock
  (list.remove on a NamedTuple with identical fields is equality-based;
  acceptable at ≤50 entries — disclose if you deviate).
- The summary card lands via `_emit_feed_card`; per-item stale cards too.
- The abort card reports exactly how many succeeded before the failure.
- Errors surface as cards, never silently dropped (spec AC#3).
- No new thread is spawned for the batch — it runs on the caller's thread
  (SP3 wires it to the bar button via idle_add, same as accept_changes's
  current `_do` pattern... note: `accept_changes` DOES spawn a thread —
  mirror `accept_changes`: spawn `threading.Thread(target=_do, daemon=True)`).

### 2e. `accept_all_queues(project_name)` — D8

```python
    def accept_all_queues(self, project_name: str,
                          session_key: str | None = None) -> None:
        """SPEC-10 D8: batch-accept every AGENT queue (never "pm")."""
        for agent_key in self.agents_with_pending(project_name):
            if agent_key == "pm":
                continue
            self.accept_agent_queue(agent_key, project_name, session_key)
```

(Sequential in-loop is acceptable at MVP scale — disclose if you thread it.)

### 2f. `on_project_closed` — queues SURVIVE (GAP-5b ruling)

**No change** to `on_project_closed` (queues persist in memory across tab
close/reopen per GAP-5b). Add one docstring line to `on_project_closed`
stating queues intentionally survive (so a future reader doesn't "fix" it).

## 3. Tests — `tests/test_review_queues.py` (new file)

Reuse `tests/test_review_handler_feed_card.py`'s doubles: `MockGLib`
(immediate idle_add), `DeferredGLib`, `_make_handler`, `MockGitResult`. Copy
them or import where importable — your call, disclose.

RED-first for every behavior:
1. `test_start_review_enqueues_pm` — start_review → queue has "pm" entry,
   sha matches commit result, path_used == project_path. (RED: no enqueue
   exists at HEAD.)
2. `test_trailer_on_pm_checkpoint` — `git log --format=%B -1` after
   start_review contains `Agent: pm`. (RED at HEAD: no trailer.)
3. `test_agents_with_pending / test_pending_count` — empty, one agent, two
   agents; count reflects cap-less state.
4. `test_accept_agent_queue_worktree_mark_reviewed` — worktree-shaped entry
   (tmp worktree dir under project/.worktrees/), accept → dequeued, NO new
   commit on any tree (HEAD sha of project unchanged; worktree's branch HEAD
   unchanged), summary card emitted.
5. `test_accept_agent_queue_root_commit` — project-root entry, accept →
   commit created with `Agent: <key>` trailer, dequeued.
6. `test_accept_agent_queue_stale_worktree_dropped` — worktree dir removed
   before accept → entry dropped with error card, OTHER agent's entries (or
   later same-agent entries) still processed, queue drains of the stale one.
7. `test_accept_all_queues_excludes_pm` — "pm" + agent queues populated →
   accept_all → agent queue drained, "pm" queue intact.
8. 9. `test_queue_cap_drops_oldest` — 51 enqueues → len==50, first sha
   gone, feed card emitted.
9. `test_accept_agent_queue_mid_batch_failure_aborts_remaining` — root
   commit fails on 2nd of 3 entries → first dequeued+committed, 2nd+3rd
   remain, abort card reports 1 succeeded.
10. `test_queues_survive_project_close` — on_project_closed →
    agents_with_pending still returns the key.
11. `test_mid_batch_enqueue_lands_next_batch` — accept in progress
    (DeferredGLib holding the idle callbacks?) — simulate by enqueuing
    during iteration via a hook OR by snapshot-then-enqueue ordering test:
    assert a new entry added after the snapshot is NOT accepted in this
    batch. (Design the mechanism; disclose.)

## 4. Battery (paste all outputs)

- `.venv/bin/python -m pytest tests/test_review_queues.py tests/test_review_handler_feed_card.py tests/test_review_state.py -q`
- `.venv/bin/python -m pytest tests/test_review_log.py -q` (adjacent)
- `ruff check` profile vs HEAD on both touched source files
- `pyright models/review_state.py ui/handlers/review_handler.py` vs baseline
  (review_handler has pre-existing errors? measure at HEAD first, keep ≤)
- RED proofs for the new tests at pre-edit HEAD
- `wc -l` on touched files

## 5. COMPLETENESS (mandatory)

- [ ] Edit 1: QueueEntry in models/review_state.py — evidence: diff hunk
- [ ] Edit 2: queue store + lock + cap + enqueue — evidence: diff hunk
- [ ] Edit 3: start_review enqueue + trailer — evidence: diff hunk
- [ ] Edit 4: accept_agent_queue per D3 REV 2 — evidence: diff hunk + how the ellipses were filled
- [ ] Edit 5: accept_all_queues per D8 — evidence: diff hunk
- [ ] Edit 6: on_project_closed docstring note — evidence: diff hunk
- [ ] Tests: 11 new tests, RED proofs — evidence: outputs + count
- [ ] Battery pasted
- [ ] Related issues found, NOT fixed (flagged)

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
