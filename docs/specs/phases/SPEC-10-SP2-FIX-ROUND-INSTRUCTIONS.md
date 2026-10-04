# SPEC-10 SP2 Fix Round — clean-tree semantics, serialization, pm drain, trailer, test truthiness

**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `ui/handlers/review_handler.py` + `tests/test_review_queues.py`.
Rulings: D3 **REV 3** and D8 **REV 2** in
`docs/specs/phases/SPEC-10-PREFLIGHT-DECISIONS.md` (binding).

---

## BUG #1 (HIGH) — clean-tree root item strands

`accept_agent_queue` root branch: commit failure with the EXACT error
`"nothing to commit (working tree clean)"` is the normal D2 end-state →
**bookkeeping success + dequeue**, outcome text e.g. `reviewed (clean) <sha>`.
Any other commit failure = real failure → abort-remaining (unchanged).

Implementation hint (match the exact string, it's git_ops' own contract):

```python
                    if not commit.success:
                        if commit.error == "nothing to commit (working tree clean)":
                            succeeded += 1
                            outcomes.append(f"reviewed (clean) {entry.sha[:7]}")
                            self._dequeue(project_name, agent_key, entry)
                            continue
                        failed += 1
                        ...abort remaining with partial card...
                        break
```

Do NOT pass `allow_empty=True` — that fabricates empty commits.

## BUG #2 (HIGH) — accept_all_queues races (concurrent threads on one repo)

`accept_all_queues` must run the whole per-agent loop in **ONE background
worker thread**, sequentially: snapshot agents under the lock, then for each
(non-"pm") agent run the same accept body. Refactor so both
`accept_agent_queue` and `accept_all_queues` share the per-agent worker
logic (a `_accept_agent_queue_sync(agent_key, project, sk, state)` internal
running on a caller-provided thread; public methods spawn ONE thread for
one agent, or ONE thread iterating all agents). No joins on the GTK main
thread; no concurrent stage/commit on one repo, ever.

## BUG #3 (MEDIUM) — mock-truthiness tests (false confidence)

- `test_accept_agent_queue_root_commit`: add a REAL-git variant (no git_ops
  patching) — clean tree (D2 end-state) → entry dequeued, no new commit
  created (count HEAD before/after), summary card says reviewed.
- `test_accept_all_queues_two_agents_real_git`: real repo, two agents with
  root entries (one staged change each... note: clean-tree post-checkpoint
  is the normal case — after BUG#1's fix both drain via bookkeeping; also
  include one dirty-tree case to pin the commit path with trailer).
  Assert both queues drain, no PARTIAL cards, and if any commit was made it
  carries the right `Agent:` trailer.
- Keep the existing mocked tests (they pin card plumbing), but the real-git
  variants must be added — the auditor's probes A/C4 are the RED proofs.

## BUG #4 (MEDIUM) — pm queue never drained

`accept_changes` `_do()` success path (both the commit branch AND the
"Nothing to commit" branch — both resolve the session): dequeue ALL of the
project's "pm" entries. `reject_changes` success path: clear the project's
"pm" entries too (a rejected session's checkpoints are moot).

## BUG #5 (MEDIUM) — accept_changes commit carries no trailer (GAP-4)

`git_ops.commit(project_path, full_message)` in `accept_changes` gains
`agent_trailer="pm"`.

## BUG #6 (LOW) — isdir-only stale check (bounded; SP2b handoff)

No code change this round (worktree accept is bookkeeping-only — no bad
commit possible; the hole is a misleading "reviewed" card for an
unregistered dir). **Documentation duty:** one comment line at the
classification site stating the SP2b handoff (registration membership via
`WorktreeManager.path_for` arrives with ARH wiring). The auditor's exact
`.worktrees`-as-project edge is registered, not fixed.

## Tests to add/strengthen (RED-first)

1. `test_accept_root_clean_tree_dequeues` (BUG#1) — real git, clean tree →
   dequeued + no commit + reviewed outcome.
2. `test_accept_all_two_agents_no_concurrency` (BUG#2) — two agents, real
   git; assert both drain; optionally assert serialization by instrumenting
   the worker (e.g. an event hook or by asserting only ONE worker thread
   ever ran via a thread-id recorder in a patched emit callback).
3. `test_pm_queue_drained_by_accept_changes` (BUG#4) — start_review →
   accept_changes → pm pending == 0 (both branches: commit and
   nothing-to-commit).
4. `test_pm_queue_cleared_by_reject_changes` (BUG#4) — start_review →
   reject → pm pending == 0.
5. `test_accept_changes_commit_has_pm_trailer` (BUG#5) — real git; log
   contains `Agent: pm`.
6. Strengthen `test_accept_agent_queue_root_commit` + add the two-agent
   real-git test (BUG#3).

## Battery (paste all)

- `.venv/bin/python -m pytest tests/test_review_queues.py tests/test_review_handler_feed_card.py tests/test_review_state.py tests/test_review_log.py tests/test_stop_all.py -q` (xvfb-run)
- ruff multiset vs current (no new classes)
- pyright 0 on both files
- RED proofs for the 6 fixes

## COMPLETENESS (mandatory)

- [ ] BUG#1 clean-tree bookkeeping — diff hunk + RED
- [ ] BUG#2 single-worker serialization — diff hunk + RED (probe C4 shape)
- [ ] BUG#3 real-git tests — added, RED-verified
- [ ] BUG#4 pm drain both paths — diff hunk + 2 tests RED
- [ ] BUG#5 pm trailer — diff hunk + test RED
- [ ] BUG#6 comment handoff — diff hunk
- [ ] Battery + ruff/pyright outputs
- [ ] Related issues found, NOT fixed (flagged)

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
