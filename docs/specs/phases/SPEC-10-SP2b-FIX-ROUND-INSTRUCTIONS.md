# SPEC-10 SP2b Fix Round — pin the four gate tests (BUG#1) + per-session checkpoint lock (ISSUE#2, D8d)

**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `tests/test_agent_runtime.py` (4 negative tests) +
`ui/handlers/agent_runtime_handler.py` (D8d lock) + one new test.
Binding ruling: **D8d** in `docs/specs/phases/SPEC-10-PREFLIGHT-DECISIONS.md`.

---

## BUG #1 (MEDIUM, test-quality) — four negative tests are timing-masked

`test_non_writer_no_checkpoint` / `test_review_off_no_checkpoint` /
`test_inactive_session_no_checkpoint` / `test_stale_token_skips_checkpoint`
assert `pending_count == 0` synchronously after `_finish()` — the daemon
thread hasn't run, so the assert passes even with the guard DELETED
(auditor's fresh-process drivers: all four mutations still green).

### Fix

Use the auditor's suggested shape (or an equivalent that genuinely pins):

```python
        # BUG#1 fix: the synchronous ==0 assert raced the daemon thread and
        # passed with the guard removed. Pin by asserting NO entry appears
        # within a settle window — fails if the guard is deleted (the
        # checkpoint thread enqueues within ~1s in that case).
        assert not self._wait(
            lambda: rh.pending_count("proj", "special:coder") != 0,
            timeout=1.0,
        ), "<guard-name> guard missing: checkpoint enqueued despite <condition>"
```

Apply to all four tests. RED proof: delete each guard in turn (the
auditor's drivers did exactly this) → each test now FAILS. Paste one
representative RED (guard removed → test fails) per test.

## ISSUE #2 (LOW-MED) → D8d — per-session checkpoint serialization

Same-session overlapping turns race the worktree checkpoint (auditor: 18/25
one-fail; modes: `COMMIT_EDITMSG` FileNotFoundError, index.lock; root tree
with the lock: 0 failures). Consequence: a silently-missing queue entry for
that turn (last turn never self-heals).

### Fix (D8d)

In `__init__`:

```python
        # SPEC-10 D8d: per-session checkpoint serialization. Two same-session
        # turns can overlap (turn N's checkpoint daemon still running when
        # N+1 completes); both write ONE worktree. Serializes the whole
        # checkpoint body; no eviction (session keys are roster-bounded).
        self._checkpoint_locks: dict[str, threading.Lock] = {}
```

In `_maybe_agent_checkpoint`'s thread body, wrap the ENTIRE body
(stage → gates → commit → enqueue) with:

```python
            with self._checkpoint_locks.setdefault(session_key, threading.Lock()):
                ...existing body...
```

Rules: this is IN ADDITION to the existing lock distinction (worktree: no
project lock; root: project lock) — nesting order
checkpoint_lock → project_lock (only the root branch takes the project
lock inside it; never the reverse). No GLib blocking inside.

### Test

`test_overlapping_same_session_checkpoints_serialize` — two overlapping
completions in one worktree (the auditor's stress shape); assert BOTH queue
entries land (or: no git error, entry count == 2 after settle). RED
pre-fix: the auditor's 18/25 one-fail rate (one entry missing) — reproduce
at a smaller N (e.g. 10 iterations, expect ≥1 missing entry in at least one
run; document the observed rate).

## SUGGESTION #3 (LOW) — enqueue_agent_checkpoint validation

Add the cheap fail-closed guard (types + sha shape), matching SP1's
posture:

```python
    def enqueue_agent_checkpoint(self, project_name: str, agent_key: str,
                                 sha: str, path_used: str) -> None:
        """...existing docstring... Rejects malformed inputs (non-str,
        empty, bad sha shape) with a log — never raises (D9b's lock
        discipline is upheld; garbage never enters the queue)."""
        if (not isinstance(agent_key, str) or not agent_key.strip()
                or not isinstance(sha, str)
                or not _VALID_SHA_RE.match(sha)
                or not isinstance(path_used, str) or not path_used.strip()):
            _logger.warning("enqueue_agent_checkpoint: rejected malformed entry "
                            "(agent=%r, sha=%r, path=%r)", agent_key, sha, path_used)
            return
        ...existing wrapper...
```

(`_VALID_SHA_RE` already exists in review_handler.) Test: malformed
enqueue → no entry, no raise (RED: current code enqueues under None key).

## Do NOT change

- The positive tests, snapshot/token/gate logic, lock adapter, D8c posture.
- ReviewHandler's SP2 surface (beyond the wrapper guard).

## Battery (paste all)

- `xvfb-run -a .venv/bin/python -m pytest tests/test_agent_runtime.py -q` (full file)
- `xvfb-run -a .venv/bin/python -m pytest tests/test_review_queues.py tests/test_stop_all.py -q`
- ruff multiset (25=25) + pyright 0 on both files
- RED proofs: 4 guard-deletion REDs + overlap RED rate + malformed-enqueue RED

## COMPLETENESS (mandatory)

- [ ] BUG#1: four tests pinned — diff hunks + 4 RED proofs
- [ ] ISSUE#2/D8d: per-session lock — diff hunk + overlap test + RED rate
- [ ] SUGGESTION#3: wrapper guard — diff hunk + RED
- [ ] Battery + baselines
- [ ] Related issues found, NOT fixed (flagged)

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
