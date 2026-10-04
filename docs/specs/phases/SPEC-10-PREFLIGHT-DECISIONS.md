# SPEC-10 Pre-flight Decisions (Supervisor, 2026-10-03)

Spec: `docs/specs/SPEC-10-REVIEW-QUEUES.md` (dated 2026-09-20; verified against
code at HEAD 92694f2f — post-SPEC-09. Line refs in the spec drifted; all edits
anchor by identifier).

## Design gaps found in verification (spec drift)

### GAP-1 — No agent-side checkpoint creation site exists
The spec's data flow (§3) begins "Agent write → enforcement → checkpoint (w/
`Agent:` trailer)". Verified: no such site exists. The only checkpoint creation
is PM-initiated `ReviewHandler.start_review()` (`/review`). Without new wiring,
per-agent queues could only ever hold "pm" items — AC#2 (review bar shows
per-agent queues) would be undeliverable. **Resolution:** add an agent-side
checkpoint path. See D2.

### GAP-2 — "Agent identity" needs a concrete source
The spec says queues key off `conv.session_key` "where a conversation context
exists". Verified: `_do_tool_call_result` has `session_key` in hand at card
flag time. **Resolution:** D2.

### GAP-3 — PM's own edits: queue key "pm" (spec §7 row 1)
Verified: `cmd_review` runs `start_review(project_name, sk)` with sk =
`project:<name>` — the PM's session key varies per project, which would
fragment the PM's queue. **Ruling: the PM queue key is the literal `"pm"`**
for all projects (the PM is one person; per-project tabs each show their own
state but the identity is constant). Agent keys remain their session_keys
(e.g. `special:coder`). PM-initiated `start_review` checkpoints enqueue under
`"pm"` with `agent_trailer="pm"` on the commit.

### GAP-4 — accept commits must carry the trailer too
Spec §2 (git_ops) + AC#1: "every agent commit carries `Agent: <session_key>`
trailer" — checkpoint AND accept commits. The existing `accept_changes` path
does not thread a trailer. **Resolution:** thread through.

### GAP-5 — ReviewBar is single-session; queue selector must not assume a review session is active
Verified: the bar's states (idle/reviewing/has_changes) are per-project single
session. A queue selector listing agents with pending queues must render even
when state.is_active() is False (e.g., queues with agent checkpoints exist but
the PM never started a review session). **Resolution:** the queue view is a
separate additive region; not gated on session activity.

### GAP-5b — queue content must be able to persist across project close/reopen
Verified: `on_project_closed` pops ReviewState without touching queue entries.
**Resolution:** queues keyed `(project_name)` → per-agent ordered list, stored
in a new dict on ReviewHandler; on project close the queue for that project is
kept in memory (PM may reopen the tab; nothing committed). Not persisted to
disk in this spec (in-memory only). Post-MVP: durable queue store. **Accepted
trade-off:** losing queues on app restart loses *nothing committed* — the
worktrees + branches survive, and the PM can still review via git directly.
Registering as an evolution item, not a blocker.

### GAP-6 — `_active_project` is global on ARH
Verified: single `_active_project: tuple[str, str] | None` on ARH; `_do_tool_call_result`
uses it. Multiple projects open simultaneously → writes to a non-active project
would be attributed to the wrong project/agent queue. **Resolution for this
spec:** scope queues per project (dict keyed by project_name); ARH flags
needs_review + staging only for the ACTIVE project today, and that's the
behavior we inherit. The queue data structure is per-project from day one so
the multi-project attribution bug is not baked into the new code.

### GAP-6b — acceptance in worktrees: batch accept must operate on the right tree
SPEC-09 landed leased writers writing into `<project>/.worktrees/<id>` (branch
`agent/<id>`). The spec's "accept = per-item existing accept-commit path"
assumes one tree. **Resolution:** see D3.

## Decisions

### D1 — Trailer format: literal `Agent: <session_key>` in commit message body
Format: `message + "\n\nAgent: <key>"` appended by `git_ops.commit` when
`agent_trailer` is set (exactly as the spec sketches). Sanitization: the trailer
value must not contain newlines (else it could forge multiple trailer lines or
break the message format). `git_ops.commit` sanitizes: strip whitespace, reject
embedded newline/control chars → fail-closed (return `success=False` with
error) rather than silently dropping the trailer. Backward compat: callers not
passing `agent_trailer` unchanged behavior.

### D2 — Agent-side checkpoints via the existing turn-complete hook *(REV 2 — post-probe, 2026-10-03)*
Agent-side checkpoints are created at **turn completion** (`_do_response_complete`)
for writer agents when the active project has review mode ON: `add -A` + commit
`[review] agent checkpoint` with `agent_trailer=<session_key>` in the agent's
write location (worktree when leased, else project root — the path the turn
actually used), then `enqueue(agent_key, sha, project_name, path_used)`.

**REV 2 rulings (probe BUG#3 + D2 race findings — binding on SP2b):**
- **COMPLETED-only.** The runtime dispatches `on_response_complete` ONLY for
  COMPLETED turns; CANCELLED/FAILED → `on_error` → **no checkpoint, no queue
  entry**. A cancelled writer's edits stay uncommitted in the worktree and are
  swept into that agent's NEXT completed-turn checkpoint (tree-wide `add -A`).
  Surfacing: the existing cancel card already tells the PM the turn halted;
  nothing is lost (git state inspectable; next checkpoint sweeps it).
- **Attribution snapshot at turn DISPATCH, not completion.** ARH records
  `(project_name, path_used)` keyed by **turn_token** at send time (after
  `_prepare_turn_conversation` resolves the worktree), reads it at completion.
  Never `_active_project` at completion time (project-tab switch mid-turn
  would mis-attribute — GAP-6).
- **Stale-token completions skip checkpointing.** If the token rotated (rapid
  re-send), the old turn's completion early-returns; its edits are swept by
  the next checkpoint of the same agent. Self-healing; no queue entry.
- **Background thread.** The checkpoint (is_repo/init → stage → commit →
  enqueue) runs on `threading.Thread(daemon=True)`, mirroring `start_review`.
  Never inline in `_do_response_complete` (GTK main-loop blocking).
- **Superset property (documented, accepted):** a checkpoint may include
  partial writes from the agent's next turn (checkpoint thread interleaves
  the next turn's tool loop). Safe direction for review — the gate reviews a
  superset, never a subset. A torn file is visible in the diff and recoverable
  via reject.
- Multiple write-capable agents per project are the norm; every writer gets a
  queue entry at its own turn end. Non-writer turns never checkpoint.

### D2b — Review-mode gate: agent checkpoints only in review mode
`state.review_mode == "review"` AND `state.is_active()` — same gate as the
existing needs_review card flagging (which requires is_active()). This makes
agent checkpoints the *unit of review* the PM opted into. Turn completes
outside a review session → no checkpoint, no queue entry.
> Rationale: without this gate, every turn of every write agent in every
> project would checkpoint-commit to agent branches, polluting history with
> empty commits and queueing unreviewed work the PM never asked to track.

### D3 — Batch accept semantics (worktree-aware) *(REV 2 — post-probe, 2026-10-03)*
`accept_agent_queue(agent_key)` iterates the queue in order; for each item:
1. **Verify with `WorktreeManager.path_for(id)`** (isdir + registration
   membership), NOT `is_worktree_of` (probe BUG#5: pure path-shape predicate,
   True for removed worktrees → stage_all NoSuchPathError → abort-remaining
   livelocks the whole batch). A stale item surfaces as a **single-item error
   card + entry dropped** (never abort-remaining, never silent). Existence
   check runs BEFORE any `ensure_worktree` — revival from the surviving branch
   would stage a freshly-recreated empty checkout.
2. **Accept semantics for already-committed items (probe BUG#4):** the D2
   checkpoint already committed the work on the agent branch. Accept for a
   worktree item = **"mark reviewed" bookkeeping + dequeue** — NO new commit
   (the work already carries the `Agent:` trailer from the checkpoint
   commit; an accept commit would be empty or livelock). Merge to main stays
   manual per-unit (spec Out-of-scope). Dequeue keys on the **bookkeeping
   success**, not a git success. The batch card reports per-item outcomes.
3. For project-root items (unleased writers, PM): existing accept path —
   stage + commit with `agent_trailer=<agent_key>`. **REV 3 ruling (audit
   BUG#1, 2026-10-03):** the D2 checkpoint already committed the work, so a
   root item on a CLEAN tree is the NORMAL end-state — treat commit failure
   with the exact error `"nothing to commit (working tree clean)"` as
   **bookkeeping success + dequeue** (mirroring step 2; never fabricate an
   empty commit, never strand the entry). Any OTHER commit failure is a real
   git failure → abort-remaining. The same REV 3 ruling applies to
   `accept_changes`' PM path: its existing friendly "Nothing to commit"
   branch stays, and its real commits gain `agent_trailer="pm"` (GAP-4).
4. Queue item is removed on success; **failure aborts remaining items with an
   error card reporting exactly how many succeeded before the failure** (spec:
   no partial silent loss). Root-cause items (stale path) are dropped with an
   error card and do NOT abort remaining (BUG#5 fix — only item-level git
   failures abort the batch). **REV 4 ruling (re-audit Finding A, 2026-10-03):**
   the git critical section (stage/commit) is serialized **per project across
   ALL public accept entry points** — a `_project_accept_lock: dict[str,
   threading.Lock]` (or single in-flight guard) held by
   `_accept_agent_queue_sync` around the stage/commit section. Concurrent
   public calls (double-click, accept_all + per-agent) then block-or-no-op,
   never stampede. Snapshot-iterate may still run concurrently with the
   enqueue side (fine — `_queue_lock` guards the dict), but two stage/commit
   sections never overlap on one repo. **REV 4b enumeration (re-audit BUG#1
   + BUG#4, 2026-10-03):** "ALL public accept entry points" means every
   mutating git call on the project root, complete list: `accept_changes`
   (stage + diff-read + commit), `start_review` (stage + commit),
   `reject_changes` (checkout), `reject_file` (checkout),
   `revert_file_to_sha` (checkout), and `_accept_agent_queue_sync`.
   Read-only diffs (`check_changes`' `diff_against`) take no lock. The
   lock-dict is intentionally NEVER
   evicted on project close (ruling vs BUG#3): evicting a lock an in-flight
   accept holds would let close→reopen→setdefault create a second live lock
   for one project — reintroducing the stampede the lock exists to prevent.
   The ~40-byte-per-name leak is the accepted cost (single-user desktop,
   bounded by distinct project names per session).
5. `accept_all_queues()` = iterate all agent queues (excluding "pm" — see D8)
   in insertion order.
6. **Dequeue-on-success-only** (fail-safe for races): items are removed from
   the queue only after their accept succeeds. If an agent's new checkpoint
   lands mid-batch (race), it lands in *next* batch (spec §7 row 2) — natural
   consequence of snapshot-iterate. Snapshot the list before iterating (no
   ConcurrentModificationError; mid-batch new enqueues land next batch).

### D4 — Queue selector UI (ReviewBar additive region)
`set_queue_view(agents, counts)` — per-agent row w/ count; click → filter
review bar's pending set (status label + Check Changes scoped to that agent's
items). Buttons: `Accept All (agent)` / `Accept All (everyone)` with a
confirmation dialog (existing `stop-all` confirm-dialog pattern in window.py
for consistency). Pure view; callbacks injected. Not gated on
`state.is_active()`.
- **Confirmation must show the N being accepted** — anti-fat-finger for a
  multi-commit batch action.

### D4b — The queue view must not regress the bar's existing states
idle/reviewing/has_changes state methods unchanged. Queue view is a separate
widget region appended to the bar; visibility tied to len(queues) > 0.

### D5 — Diff-card metadata: `metadata["agent"] = session_key`
On agent-flagged diff cards (the existing `needs_review` flag path in
`_do_tool_call_result`) — the diff cards shown in `check_changes` come from
parse_diff output; the agent attribution is recorded on the queue entry (which
carries sha + agent + path), not on every diff card. The spec's "diff cards
(metadata.agent) → feed filter-by-agent comes free" is honored by setting
`metadata["agent"]` on the queue entry's emit side (see SP3, the feed-card
bridge `QueueReviewCard` — every queue emit also emits a feed card with
`metadata["agent"]`). Post-MVP group-chat filtering builds on this.

### D5b — The ARH staging path uses conv.project_path — not _active_project — for leased writers
Verified drift: `_do_write_success` staging (ARH ~2216) computes staging from
`self._active_project` for ALL writers, including leased ones whose writes
landed in their worktrees. SPEC-10's queue entries must record `path_used`
(the tree the turn wrote to), so batch accept works on the right tree. The
staging mismatch is pre-existing (SPEC-09 legacy, flagging to auditor; SP3
aligns it as part of the queue-emit bridge since the same `path_used`
attribution logic feeds both).

### D6 — Tests
`tests/test_review_queues.py` (~220 lines per spec; may grow) + additions to
`tests/test_git_ops.py` (trailer) + ARH integration tests in
`tests/test_agent_runtime.py` (worktree-aware checkpoint). RED-first for every
new behavior (steelFramed rule 4).

### D7 — Rejection path unchanged (spec: per-item only)
`reject_file`/`reject_changes` untouched. Queue rejection = PM rejects via
existing per-item path; no new batch reject (spec silent; batch reject
**without** revert semantics would be dangerous — reverting N agents' trees in
one click is exactly the class of action that deserves per-item review).

### D7b — Path attribution at enqueue-time, verified at accept-time
Queue entries carry `(agent_key, sha, path_used, project_name, ts)`. At
accept time the path is re-verified (worktree still registered / root still
matches) — stale entries (worktree removed) surface as error cards, not
silent skips.

### D8 — PM queue ("pm") never included in batch accept *(REV 2 — 2026-10-03)*
Per spec §7 row 1: `accept_all_queues()` processes **agent queues only** —
the literal key `"pm"` is excluded from every batch accept. **REV 2 ruling
(audit BUG#4):** the PM queue has a consumer — `accept_changes`' success
path dequeues the project's "pm" entries (and `reject_changes` clears them
too: a rejected session's checkpoints are moot). The PM's own `/accept` /
`/reject` IS the drain. **REV 3 ruling (re-audit Finding B, 2026-10-03):**
EVERY session-resolving exit path drains — including the diff-read-error
branch's `_reset_state` (the session is already reset there; orphaned pm
entries would desync from any live session and the next `/review` enqueues
fresh ones anyway). Rationale: the PM's own edits are the one queue whose
acceptance should never ride along with a bulk action.

### D8b — Stop-all gate *(REV 2 — post-probe, 2026-10-03)*
Agent-side checkpoint creation inherits SP3's stop-all gates — with the
threading ruling explicit (probe BUG#6): the checkpoint runs on a **background
daemon thread** (D2 REV 2), so the two gates are LIVE, not dead code:
- **Gate 1 (pre-flight):** before the checkpoint thread starts.
- **Gate 2 (commit-boundary):** re-check `stop_all_in_progress()` just before
  `git_ops.commit` — stop-all may land while staging ran.
- **Counter timing (inherited SP3 BUG#4 fix):** `note_stop_all_aborted()` must
  land BEFORE the stop-all summary card is emitted (the `_abort_checkpoint_
  for_stop_all` helper emits + counts in one call — agent checkpoints reuse
  the same helper, so the card-count defect class is closed by construction).
Queue *display* is not gated.

### D8c — Empty-commit guard for agent checkpoints
`allow_empty=True` on agent checkpoints mirrors PM checkpoints (marker SHA is
the output). But: unlike PM checkpoints, agent checkpoint failure does NOT
error-card into the agent chat (non-fatal; log + skip queue entry). Rationale:
mid-turn kills already have their own surfacing; a failed checkpoint shouldn't
spam the agent's transcript. The queue simply doesn't get the entry (and the
agent's uncommitted work stays in the worktree — recoverable via git directly).

### D8d — Per-session checkpoint serialization *(ruling on SP2b audit ISSUE#2, 2026-10-03)*
Two same-session checkpoint threads (turn N + turn N+1 overlapping) race in
one worktree — the "one writer per worktree" premise of D3 REV 4b's no-lock
rule does not hold WITHIN a session. Consequence without serialization: a
checkpoint git failure (D8c non-fatal) silently drops that turn's queue
entry; the LAST turn's work never self-heals into the queue. Ruling: a
**per-session checkpoint lock on ARH** (dict session_key → Lock, mirroring
`_prep_locks`; no eviction — session keys are roster-bounded), held around
the whole checkpoint body (stage → gates → commit → enqueue). Worktree
checkpoints still take NO project lock (the project lock is for the shared
root only); the per-session lock serializes same-session threads. Sweep
semantics make serialization correct: the blocked checkpoint B sweeps
everything A committed plus its own turn's work.

### D9 — Queue caps
In-memory queue cap per agent: 50 entries (FIFO overflow drops OLDEST with a
warning log + a feed card noting the drop). Bounded-memory invariant (P11
generalized): every append-driven surface is bounded.

### D9b — Threading: queues owned by ReviewHandler under a lock
`_queues: dict[str, dict[str, list[QueueEntry]]]` (project → agent → ordered
entries) guarded by `threading.Lock` — enqueue happens from ARH (runtime-loop
thread), reads from the review bar (main thread), batch accept from the PM's
click (main → background thread). The lock is O(1) per op; enqueue appends
bounded lists (cap D9).
```
_agents_of(project) -> list[str]            # agents with pending entries
_pending_count(project, agent) -> int
_enqueue(project, agent, entry)             # O(1) append + cap
_entries(project, agent) -> snapshot list   # copies the list
```
Public surface intentionally small; ReviewBar never touches the dict directly —
it calls handler methods via injected callbacks.

### D10 — SP phasing
- SP1: `git_ops` trailer (+ tests). Independently verifiable.
- SP2: ReviewHandler queues + batch accept (+ tests).
- SP2b: ARH enqueue wiring (turn-complete checkpoints, worktree-aware) (+ tests).
- SP3: ReviewBar queue view + confirmation + window wiring + feed-card bridge (+ tests).
- SP4: ARCHITECTURE.md update + full battery + post-mortem.

### D10b — Naming
New public identifiers: `git_ops.commit(agent_trailer=...)`;
`ReviewHandler.enqueue_agent_checkpoint(...)`, `accept_agent_queue(agent_key,
project_name)`, `accept_all_queues(project_name)`; `ReviewBar.set_queue_view`,
`set_batch_accept_callbacks`. File `tests/test_review_queues.py`.
```
QueueEntry = tuple[str, str, str, datetime]  # (agent_key, sha, path_used, ts)
```
A NamedTuple — `QueueEntry(agent_key, sha, path_used, ts)` (importable from
models/review_state.py alongside ReviewState; pure data, no GTK).

## Standing verification commands

- Targeted: `python -m pytest tests/test_review_queues.py tests/test_git_ops.py -x -q`
- ARH: `xvfb-run -a python -m pytest tests/test_agent_runtime.py -x -q`
- Ruff/pyright per-phase on touched files vs baseline.
- Full battery at SP4: full xvfb suite + ruff + pyright vs baselines.

