# SPEC-32: Read dedup recovery — return the body when the latest result for a path is a stub

**Date:** 2026-10-15
**Author:** Supervisor (PM ruling 2026-10-15: "Dedup should return the file body when the latest tool result for that same path is a mask or compact stub… Check only the latest result for that path. Skip the exec half.")
**Status:** Draft — for implementation
**Depends on:** SPEC-23 (working set), SPEC-30 (aggressive tiers — produces the masks this recovers from), SPEC-31 (repo map v2)
**Target branch:** main

---

## 1. Overview

### Problem — the mutually-reinforcing pair that loops agents

Two runtime features interact to produce an infinite read loop (observed on `special:coder`, 2026-10-14/15, STT round):

1. **Periodic masking** (SPEC-30 tiers): every 4th observation, tool results older than the last 3 are replaced with `[masked — read_file <path> offset=0 (N chars removed)]`. Correct behavior — but it deletes the body.
2. **Read dedup** (`_dedup_read_output`, `agent/runtime.py:3835-3871`): a second `read_file` of the same `(path, offset, limit)` with matching `(st_mtime_ns, st_size)` answers `[unchanged — read_file … same mtime and size as the earlier read this turn]` — **the body is not restored.**

A rule-following agent (steel-frame: "read the whole file before writing") reacts to the mask by re-reading; dedup refuses; the agent concludes `read_file` is withholding and shells out via `exec_command` (cat/sed); those observations get masked on the next batch; repeat. The mask deletes the body, and the dedup blocks the one legitimate recovery path. Each feature is fine alone; together they are a loop generator.

### Why the existing protections don't cover it (PM-verified)

- **Hot-path pin (`_note_tool_path` / `_protect_paths`, runtime.py:3645/3686)**: pins a path on the **third** successful read while mtime+size match. Coder's files were each read **once** — nothing pinned. The count also updates only after dedup has already answered `[unchanged]`, so a pin can never change that reply or restore a masked body. It protects against *future* masking, not *past*.
- **`select_maskable` keep-last-3**: keeps the newest three tool results — but the re-read never produces a new body result, so there is nothing new to keep.

### Solution — one rule, one site

In `_dedup_read_output`, when the fingerprint matches, **before** answering `[unchanged]`, check the **latest** tool result for that same path in the conversation:

- If it is a `[masked —` or `[compacted —` stub → **return the full body.** The agent is explicitly recovering; re-sending the bytes once is cheaper than the exec_command workaround (which spends another observation and gets masked too).
- If the latest result for that path still carries the body (i.e. a live copy sits in the keep-last window) → `[unchanged]` remains correct.

**Explicitly out of scope (PM ruling):** no pin changes; no exec-command policy ("deciding which exec_command was recovering a read is a second policy"). Once a masked read can be read again, the shell workaround dies on its own.

---

## 2. Changes by File

### 2.1 `agent/runtime.py` — the whole fix

**New helper** (place directly above `_dedup_read_output`, ~:3834):

```python
def _latest_read_is_stub(self, conv: Any, path: str) -> bool:
    """True when the most recent tool result for ``path`` is a mask/compact stub.

    Scans backward; the FIRST tool result whose extracted path equals ``path``
    is the latest. Returns False when no result exists for the path (dedup
    then falls through to the unchanged notice — safe default).
    """
    try:
        strategy = self._context_strategy
        for i in range(len(conv.messages) - 1, -1, -1):
            msg = conv.messages[i]
            if getattr(msg, "role", None) != MessageRole.TOOL_RESULT:
                continue
            content = msg.content or ""
            _name, rpath, _off, _parent = strategy._tool_result_meta(conv, i)
            if rpath != path:
                continue
            return content.startswith("[masked \u2014") or content.startswith("[compacted \u2014")
    except Exception:
        logger.debug("latest-read-is-stub scan failed for %s", path, exc_info=True)
    return False
```

**Edit `_dedup_read_output`** — inside the `with self._tool_history_lock:` block, change the match branch:

```python
            if bucket.get(key) == fp:
                if self._latest_read_is_stub(conv, path):
                    # SPEC-32: recovery re-read. The body was masked/compacted
                    # away; re-sending once is cheaper than the exec_command
                    # workaround (another observation that gets masked too).
                    bucket[key] = fp
                    return output
                return (
                    f"[unchanged \u2014 read_file {path} offset={offset or 0} "
                    f"limit={limit}; same mtime and size as the earlier read this turn]"
                )
```

Everything else in the method is unchanged: key construction, stat-failure passthrough, non-project passthrough, fingerprint write. **The fingerprint is still recorded on the recovery path** (`bucket[key] = fp`) so a third read that gets masked again recovers the same way.

### 2.2 Files NOT changed

- `agent/context_strategy.py` — masking/pruning/select_maskable untouched; `_tool_result_meta` consumed as-is (verified present, used at :499/:558 for the same prefix checks).
- `agent/tools.py` — no tool-layer changes.
- `_note_tool_path` / `_protect_paths` — untouched (PM ruling).
- No exec-command policy anywhere.

---

## 3. Data Flow

```
read_file(path, offset, limit)
  → _dedup_read_output(session_key, conv, args, output)
      stat → fingerprint (mtime_ns, size)
      bucket[(path, offset, limit)] == fp ?
      ├─ no  → record fp, return output            (existing)
      └─ yes → _latest_read_is_stub(conv, path)?   [NEW]
                ├─ yes → return output             [NEW — recovery re-read]
                └─ no  → return "[unchanged — …]"  (existing)

mask cycle (every 4th obs, landing_goal < est <= soft_ceiling)
  → old read_file result for path becomes "[masked — …]"
  → agent re-reads → fingerprint matches → latest result IS the stub
  → body returned → agent proceeds (loop broken, one observation spent)
```

Cost model: worst case re-sends the body once per mask cycle per hot file. The alternative today is an unbounded exec loop. If the body is re-masked later, the next re-read recovers identically — the loop cannot reform.

## 4. File Change Summary

| File | Change | Est. lines | Risk |
|---|---|---|---|
| `agent/runtime.py` | `_latest_read_is_stub` helper + match-branch edit | +26 / −2 | Low (single site, guarded) |
| `tests/test_dedup_stub_recovery.py` | 6 tests (below) | ~140 | — |

## 5. Implementation Order

1. Helper `_latest_read_is_stub`.
2. Match-branch edit in `_dedup_read_output` (two lines + comment).
3. Tests (design first if new-loop; Debugger executes).

## 6. Acceptance Criteria

- [ ] Re-read whose latest result for the path is a `[masked —` stub returns the **full body**
- [ ] Same for `[compacted —` stubs
- [ ] Re-read while a live body copy sits in the last 3 results still returns `[unchanged — …]` (no over-sending)
- [ ] Fingerprint recorded on the recovery path (third read after re-mask also recovers)
- [ ] Paths with no results / scan failure → `[unchanged]` or passthrough as today (never raises)
- [ ] Existing dedup behavior for changed files (mtime mismatch) untouched
- [ ] Tier-1 tests green; no battery

## 7. Edge Cases

| Case | Behavior |
|---|---|
| Latest result for path is the body, an older one is masked | `[unchanged]` — the live copy is in the window |
| Two paths read in interleaved turns | Scan matches per-path; other paths' stubs don't trigger |
| Result content empty/short | `startswith` False → treated as live body → `[unchanged]` (short results are never masked anyway) |
| Scan raises (odd message shape) | Caught, logged debug, returns False → `[unchanged]` (today's behavior) |
| Body re-masked after recovery | Next re-read recovers again — bounded by mask cadence, not unbounded |
| `project_path` empty / stat fails | Passthrough before dedup (existing, unchanged) |
| Offset/limit variants | Different key → fingerprint mismatch → body (existing path; scan is path-level per PM ruling) |

## 8. ARCHITECTURE.md Updates

Context subsystem, dedup paragraph: add one sentence — "A re-read whose latest result for the path is a mask/compact stub returns the full body (recovery re-read); `[unchanged]` applies only while a live copy of the body remains in the recent window."

## 9. Self-Audit (Rule 9)

1. Signatures traced to source: `_dedup_read_output` at `agent/runtime.py:3835-3871` (key `(path, offset or 0, limit)`, value `(st_mtime_ns, st_st_size)`, lock `self._tool_history_lock`, bucket `self._read_fingerprints[session_key]` at :721/:3863) ✓; stub prefixes `"[masked \u2014"` / `"[compacted \u2014"` verified at `context_strategy.py:434,456,499,558` ✓; `_tool_result_meta(conv, idx)` returning `(_tool_name, path, _offset, _parent)` verified in use ✓; `MessageRole.TOOL_RESULT` message-role scan matches `select_maskable`'s own enumeration pattern (:491-495) ✓.
2. Exceptions: helper wrapped in try/except → returns False → falls back to today's exact behavior; dedup method's existing guards untouched ✓.
3. No schema/key changes — the fix is read-side only inside one branch ✓.
4. Data flow traced read → stat → fingerprint match → stub scan → body/unchanged; mask cycle re-entry analyzed (bounded) ✓.
5. An implementer following §2 verbatim produces the fix; both code blocks carry the exact em-dash literals (`\u2014`) used by the existing stub writers ✓.
