# SPEC-32: Read dedup recovery — return the body when no live body for that key remains

**Date:** 2026-10-15
**Author:** Supervisor (PM ruling 2026-10-15: "Dedup should return the file body when the latest tool result for that same path is a mask or compact stub… Check only the latest result for that path. Skip the exec half.")
**Steel-frame pass:** 2026-10-14, against commit `e8911868`
**Status:** Draft — for implementation
**Depends on:** SPEC-23 (working set), SPEC-30 (aggressive tiers — produces the masks this recovers from), SPEC-31 (repo map v2)
**Target branch:** main

> Architecture: `AgentRuntime` owns `_read_fingerprints` and `_dedup_read_output` (`docs/ARCHITECTURE.md` §3.21m). `DefaultContextStrategy` owns tool-result metadata (`§3.21p.5`). `models/conversation.py` stays pure data (`§3.21l`). This change is a read of messages the strategy already walks, plus one branch in the runtime. No schema change, no GTK.

---

## Discovery

```
DISCOVERY:
- Read agent/runtime.py: _dedup_read_output is 3835-3870. Signature
  (self, session_key: str, conv, args: dict, output: str) -> str.
  Key is (path, offset or 0, limit). Value is (st.st_mtime_ns, st.st_size).
  Limit is args.get("limit") with no default: omitted and explicit None
  both stay None (tests/test_spec23_context_working_set.py asserts the
  notice text "limit=None"). Offset None becomes 0 only via `offset or 0`.
  Bool is rejected before the key because bool is a subclass of int.
  Empty project_path and OSError from os.stat return output before the
  lock. Bucket is self._read_fingerprints[session_key] (init at 721,
  use at 3863), guarded by self._tool_history_lock (init at 717).
  logger is module-level at line 109. MessageRole is not imported at
  module level; methods import it locally (3460, 3574).
- Read the call site, runtime.py:2442-2452: dedup runs only for
  read_file + result.success, and it runs BEFORE conv.add_tool_result.
  The output argument is not a transcript message yet. _note_tool_path
  runs after dedup returns (3645). _protect_paths is 3686.
- Read agent/context_strategy.py: _tool_result_meta (612-636) returns
  (tool_name, path, offset, parent). Offset uses args.get("offset", 0)
  and maps None to 0. It does not return limit. There is no
  _tool_result_limit (inspect confirmed). MessageRole is already
  imported (lines 17-22). Callers unpack exactly 4 values: 503, 560,
  600, 655, and runtime.py:3725. Do not change that tuple.
- Read the stub writers: prune_tool_outputs writes
  "[compacted \u2014 {tool_name} output, {N} chars removed]" (456).
  apply_mask_verdicts writes
  "[masked \u2014 {tool_name} {path} offset={offset} ({n} chars removed)]"
  (563-564). choice "summarize" sets content to that line plus "\n"
  plus content[:200] (566-567). The dict key is candidate_{n} where n
  is enumerate() over the indices argument, not the message index (549).
  select_maskable (476) keeps the newest keep_last tool results and
  skips bodies under 500 characters. keep_last=3 on a one-result
  transcript masks nothing. panic_prune (578) masks via
  apply_mask_verdicts, so its stub prefix is [masked \u2014, not
  [compacted \u2014. Prefix checks at 434, 499, 558, 598.
- Read agent/tools.py: read_file is the only tool with offset and limit
  (1122-1134). write_file (1154), edit_file (1194), list_files (1258),
  and search_files (1291) all take path and do not take offset or limit.
  _tool_result_meta therefore reports offset 0 for those calls, and a
  limit reader that preserves None reports limit None. That is the
  whole-file read key (path, 0, None).
- Read the mask cadence: _run_loop checks _obs_since_mask >= 4 (1858-1861),
  then _maybe_mask_observations (3740) returns unless
  landing_goal < estimate <= soft_ceiling. Landing goal is
  min(15% of the hard ceiling, 24_000) (1649-1658).
- Read models/conversation.py: MessageRole.TOOL_RESULT (77), Message
  (123, content: str, tool_call_id), ToolCall.arguments is a dict (97),
  add_tool_result appends a TOOL_RESULT (280).
- Read tests/test_spec23_context_working_set.py: harness is _runtime()
  plus _execute_tool_batch. 21 tests passed on this pass.
- Architecture owner: AgentRuntime for the fingerprint and the decision.
  DefaultContextStrategy for reading the producing call's arguments.
  Existing pattern to copy: _tool_result_meta's parent walk, and the
  local MessageRole import inside runtime methods.
```

### Corrections this pass made to the draft

1. **`MessageRole` is not in scope in `runtime.py`.** The helper sample used it with no import. `NameError` would be caught by the walk's `except Exception`, the helper would return False, and every fingerprint match would re-send the body.
2. **Do not default a missing limit to 0.** `_tool_result_meta` does that for offset. The fingerprint stores `args.get("limit")`, and the common whole-file key is `(path, 0, None)`. A limit reader that copies the offset default never matches that key, the walk exhausts, and dedup stops deduping.
3. **Require `tool_name == "read_file"`.** A newer `write_file`, `edit_file`, `list_files`, or `search_files` result for the same path looks like key `(path, 0, None)`. If that result is not a stub, the walk would treat it as a live body and answer `[unchanged]` while the read body is masked.
4. **The walk cannot see the call in progress.** Dedup is before `add_tool_result`. "Newest result" means the newest result already stored.
5. **There is no read-dedup paragraph in ARCHITECTURE.md.** §3.21p's observation-masking sentence belongs to `agent/context.py`. The new sentence goes in §3.21m.
6. **A one-result transcript plus `keep_last=3` does not create a stub.** Tests that need a stub call `apply_mask_verdicts` with the message index, or `keep_last=0`. Summarize verdicts use `candidate_0` for the first entry of that indices list.

---

## 1. Overview

### Problem — the mutually-reinforcing pair that loops agents

Two runtime features interact to produce an infinite read loop (observed on `special:coder`, 2026-10-14/15, STT round):

1. **Periodic masking** (SPEC-30 tiers): when `_obs_since_mask >= 4` and the estimate is between the landing goal and the soft ceiling, tool results older than the last 3 (and at least 500 characters) can be replaced with `[masked — read_file <path> offset=0 (N chars removed)]`. Correct behavior — but it deletes the body. `prune_tool_outputs` deletes bodies with a `[compacted —` stub instead.
2. **Read dedup** (`_dedup_read_output`, `agent/runtime.py:3835-3870`): a second successful `read_file` of the same `(path, offset or 0, limit)` with matching `(st_mtime_ns, st_size)` answers `[unchanged — read_file … same mtime and size as the earlier read this turn]` — **the body is not restored.**

A rule-following agent (steel-frame: "read the whole file before writing") reacts to the mask by re-reading; dedup refuses; the agent concludes `read_file` is withholding and shells out via `exec_command` (cat/sed); those observations get masked on the next batch; repeat. The mask deletes the body, and the dedup blocks the one legitimate recovery path. Each feature is fine alone; together they are a loop generator.

### Why the existing protections don't cover it (PM-verified)

- **Hot-path pin (`_note_tool_path` / `_protect_paths`, runtime.py:3645/3686)**: pins a path on the **third** successful read while mtime+size match. Coder's files were each read **once** — nothing pinned. `_note_tool_path` runs at line 2449, after `_dedup_read_output` has already chosen its reply, so a pin can never change that reply or restore a masked body. It protects against *future* masking, not *past*.
- **`select_maskable` keep-last-3**: keeps the newest three tool results — but the re-read never produces a new body result, so there is nothing new to keep.

### Solution — one rule, one decision site (PM revision 2026-10-15: the recovery walk)

In `_dedup_read_output`, when the fingerprint matches, **before** answering `[unchanged]`, walk stored tool results **newest → oldest**. Skip `[unchanged —` notices. Consider only `read_file` results whose `(path, offset, limit)` equals the fingerprint key. The first such result decides:

1. **A real body is still in the transcript** → `[unchanged]` is correct (the bytes are in the model's context).
2. **That result is a `[masked —` or `[compacted —` stub** — including a summarize stub that keeps 200 characters (the 200-character tail is appended after the mask line, so the prefix test catches it; a 200-character tail is not "the bytes in context") → **return the full body** (`output`).
3. **No `read_file` result for this key remains** (walk exhausts: trimmed, compacted away, or none exist) → **return the full body.**
4. **The walk raises** → log at debug, **return the full body.** Never raise out of dedup.

Why skip notices (the shadowing trap): an `[unchanged]` notice is itself a tool result. Trace: body at msg 10 → redundant re-read answers notice at msg 15 (correct at the time) → msg 10 masked in place → re-read sees msg 15 (the notice) as newest, which has no stub prefix → `[unchanged]` forever. The notice is never a body copy; it must not decide. The skip happens before the stub/body return.

Why only `read_file`: `write_file`, `edit_file`, `list_files`, and `search_files` store `path` and not `offset`/`limit`. Meta reports offset 0 and the limit reader reports None, which is the whole-file key. A newer listing or write result would otherwise hide a masked read.

Closing property: `add_tool_result` stores whatever dedup returned. The recovered body becomes the newest real `read_file` result for the key, so a later re-read can honestly answer `[unchanged]` again. One early redundant read no longer arms a permanent loop; cost is bounded at one body re-send per mask cycle. The fingerprint key is not removed on recovery.

**Explicitly out of scope (PM ruling):** no pin changes; no exec-command policy ("deciding which exec_command was recovering a read is a second policy"). Once a masked read can be read again, the shell workaround dies on its own.

### Scope

| In | Out |
|---|---|
| Recovery walk in `_dedup_read_output` | Pin counting, `_protect_paths` |
| `_tool_result_limit` on the strategy | Masking, pruning, `select_maskable` rules |
| Tests in `tests/test_dedup_stub_recovery.py` | `agent/tools.py`, exec-command policy |
| One paragraph in ARCHITECTURE.md §3.21m | Changing `_tool_result_meta`'s 4-tuple |

---

## 2. Changes by File

### 2.1 `agent/context_strategy.py` — limit reader only

Add this sibling directly below `_tool_result_meta` (after line 636). No new import: `Conversation` and `MessageRole` are already imported. Do not change `_tool_result_meta` or its return tuple.

`_tool_result_meta` can raise `IndexError` if `idx` is out of range. This sibling can raise the same. The walk only passes in-range indexes and catches `Exception`. This function does not catch.

Return value is the raw `limit` argument. Missing, explicit None, a missing parent, and non-dict `arguments` all return None. Integer 0 stays 0. A non-int stored value is returned as stored; the walk's `!=` then skips it because a fingerprinted limit is only `None` or `int`.

```python
def _tool_result_limit(self, conv: Conversation, idx: int):
    """Return the producing call's limit, or None when it was omitted.

    The read-dedup key stores args.get("limit") unchanged. Missing and
    explicit None are both None. Do not default a missing limit to 0:
    _tool_result_meta does that for offset, and copying it here makes a
    whole-file read (key limit None) miss every result.
    """
    msg = conv.messages[idx]
    if not msg.tool_call_id or idx <= 0:
        return None
    for parent_idx in range(idx - 1, -1, -1):
        candidate = conv.messages[parent_idx]
        if candidate.role != MessageRole.ASSISTANT or not candidate.tool_calls:
            continue
        for tc in candidate.tool_calls:
            if tc.call_id != msg.tool_call_id:
                continue
            args = tc.arguments if isinstance(tc.arguments, dict) else {}
            return args.get("limit", None)
    return None
```

Masking, pruning, and `select_maskable` stay untouched. ~24 lines.

### 2.2 `agent/runtime.py` — the recovery walk

**New helper** directly above `_dedup_read_output` (~3834). `MessageRole` is imported inside the `try`, matching the local-import pattern at lines 3460 and 3574. The import stays inside the `try` so a failure still returns False instead of escaping the tool loop.

The walk holds `self._tool_history_lock` because the caller invokes it inside that lock. `_tool_result_meta` and `_tool_result_limit` only read `conv.messages`. They must not acquire `_tool_history_lock` (they do not today). The session tool loop is one thread, and this conv is not mutated between the fingerprint check and `add_tool_result`.

Walk order, which must not be rearranged:

1. Skip messages whose role is not `MessageRole.TOOL_RESULT`.
2. Skip content that startswith `[unchanged \u2014` before any key test.
3. Skip anything whose tool name is not `read_file`.
4. Skip a different `(path, offset or 0, limit)`. Compare limit with `!=` against the raw reader value. None matches None. Do not coerce None to 0.
5. The first remaining result returns False when it startswith `[masked \u2014` or `[compacted \u2014`, and True otherwise.
6. The loop ending returns False.
7. `Exception` logs `logger.debug(..., exc_info=True)` and returns False. If that log call raises, the helper still returns False. `logger` is the module logger at line 109.

```python
def _key_body_in_transcript(
    self, conv: Any, path: str, offset: int | None, limit: int | None,
) -> bool:
    """SPEC-32: does a real read_file body for this key still live here?

    Newest to oldest. Skips [unchanged \u2014 notices and any tool that
    is not read_file. The first remaining result for (path, offset or 0,
    limit) decides: a [masked \u2014 or [compacted \u2014 stub is False;
    any other content is True. Nothing left, or any exception, is False.
    Called before add_tool_result, so the read in progress is not visible.
    """
    try:
        from models.conversation import MessageRole
        strategy = self._context_strategy
        for i in range(len(conv.messages) - 1, -1, -1):
            msg = conv.messages[i]
            if getattr(msg, "role", None) != MessageRole.TOOL_RESULT:
                continue
            content = msg.content or ""
            if content.startswith("[unchanged \u2014"):
                continue
            name, rpath, roffset, _parent = strategy._tool_result_meta(conv, i)
            if name != "read_file":
                continue
            rlimit = strategy._tool_result_limit(conv, i)
            if rpath != path or roffset != (offset or 0) or rlimit != limit:
                continue
            return not (
                content.startswith("[masked \u2014")
                or content.startswith("[compacted \u2014")
            )
    except Exception:
        try:
            logger.debug("recovery walk failed for %s", path, exc_info=True)
        except Exception:
            pass
    return False
```

**Replace the tail of `_dedup_read_output`** from the key construction through the end of the method. Leave the guards above it (non-dict args, path type, bool/non-int offset, bool/non-int limit, empty `project_path`, `OSError` from `os.stat`) exactly as they are. Those returns happen before the walk. `OSError` already covers `FileNotFoundError` and `PermissionError`.

`offset` and `limit` passed into the helper are the raw `args.get` values, which may be None. The helper applies `offset or 0` on the right-hand side only.

`bucket[key] = fp` on the recovery path is idempotent: the value is already `fp`. Do not pop the key. Popping it would make the next read look like a first read, which also returns the body, but it would drop the fingerprint the third-read case relies on once the recovered body is stored.

```python
        key = (path, offset or 0, limit)
        fp = (st.st_mtime_ns, st.st_size)
        with self._tool_history_lock:
            bucket = self._read_fingerprints.setdefault(session_key, {})
            if bucket.get(key) == fp:
                if not self._key_body_in_transcript(conv, path, offset, limit):
                    # SPEC-32: the body is masked, compacted, evicted, or
                    # the walk could not prove a live read_file body.
                    # Re-sending once is cheaper than an exec_command loop.
                    bucket[key] = fp
                    return output
                return (
                    f"[unchanged \u2014 read_file {path} offset={offset or 0} "
                    f"limit={limit}; same mtime and size as the earlier read this turn]"
                )
            bucket[key] = fp
        return output
```

Update the method docstring with one sentence: a fingerprint match returns `output` when no live `read_file` body for that key remains. The return type stays `str`. Both branches return a string; the `output` argument is not discarded on recovery and is not returned on the live-body path.

No new module-level import. ~40 lines added in the helper, ~8 lines in the match branch.

### 2.3 Files NOT changed

- `agent/tools.py` — `read_file` already returns the body (`_read_file`, line 372). Dedup is applied by the runtime after `execute_tool` returns.
- `_note_tool_path` / `_protect_paths` — still run after dedup. A recovery still counts as a successful read. Pins stay a future-mask protection.
- `models/conversation.py` — `add_tool_result` already stores the string dedup returns.
- `_tool_result_meta`'s return tuple — four existing unpack sites.
- No exec-command policy anywhere.

---

## 3. Data Flow

```
_execute_tool_batch
  read_file success
    → _dedup_read_output(session_key, conv, args, output)     # runtime.py:2445
        non-dict / bad path / bad offset / bad limit → output
        empty project_path or os.stat OSError → output         # before the walk
        key = (path, offset or 0, limit)
        fp = (st_mtime_ns, st_size)
        bucket[key] == fp ?
        ├─ no  → record fp, return output                      # existing
        └─ yes → _key_body_in_transcript(conv, path, offset, limit)
                  # stored TOOL_RESULT messages only; this call is not one yet
                  ├─ true  → return "[unchanged — …]"          # live read_file body
                  └─ false → record fp, return output          # stub, nothing left, or walk failed
    → _note_tool_path                                          # runtime.py:2449, unchanged
    → conv.add_tool_result(call_id, tool_result_text)          # runtime.py:2452

mask cycle (n_obs >= 4, then only if landing_goal < est <= soft_ceiling)
  → an older read_file result for that key becomes "[masked — …]"
    or prune_tool_outputs turns it into "[compacted — …]"
  → agent re-reads → fingerprint matches
  → walk skips [unchanged — notices and non-read_file results
  → first read_file result for that key is the stub, or the walk finds none
  → body returned → add_tool_result stores it
  → a later re-read of the same key, while that body is still live, is [unchanged]
```

Cost model: worst case re-sends the body once per mask cycle per hot key. The alternative today is an unbounded exec loop. If the body is re-masked later, the next re-read recovers the same way.

---

## 4. File Change Summary

| File | Change | Est. lines | Risk |
|---|---|---|---|
| `agent/context_strategy.py` | `_tool_result_limit` only | ~24 | Low. Wrong default (0) disables dedup for whole-file reads |
| `agent/runtime.py` | `_key_body_in_transcript` + match branch | ~48 | Low if the walk order in §2.2 is kept |
| `tests/test_dedup_stub_recovery.py` | tests in §6 | ~250 | — |
| `docs/ARCHITECTURE.md` | one paragraph in §3.21m | ~4 | — |

---

## 5. Implementation Order

1. Add `_tool_result_limit`. Check: omitted limit is None, explicit None is None, limit 0 stays 0, and `_tool_result_meta` still returns 4 values.
2. Add `_key_body_in_transcript` above `_dedup_read_output`, with the local `MessageRole` import inside the `try`.
3. Replace the match branch. Do not move the `project_path` / `OSError` returns below the walk.
4. Tests in §6. Run `tests/test_dedup_stub_recovery.py` and `tests/test_spec23_context_working_set.py`. No full battery.
5. ARCHITECTURE.md paragraph in §8.

---

## 6. Acceptance Criteria

Harness: copy `_runtime` from `tests/test_spec23_context_working_set.py` (`AgentRuntime`, `create_conversation`, `_execute_tool_batch`). Plant stubs with `DefaultContextStrategy.apply_mask_verdicts(conv, [message_index], verdicts)`. Do not expect `mask_old_observations(conv)` (keep_last defaults to 3) to stub a one-result transcript.

- [ ] After a successful `read_file`, `apply_mask_verdicts` on that result's message index, and the same `read_file` again, the new tool result is the full body, not an `[unchanged —` notice and not a `[masked —` line
- [ ] Same when that result's content is replaced with a string that startswith `[compacted \u2014` and the parent `read_file` `ToolCall` is left in place
- [ ] `apply_mask_verdicts(conv, [idx], {"candidate_0": "summarize"})` on a body longer than 200 characters leaves a mask line plus a 200-character tail. The re-read returns the full body, not the tail. The verdict key is `candidate_0` because `idx` is the first element of the indices list
- [ ] Two reads of an unmasked body: the second result is the existing notice, including `limit=None` when limit was omitted (`test_second_identical_read_is_unchanged_notice` stays the pattern)
- [ ] A newer `read_file` of a different limit (or offset) does not decide the original key. Mask only the original result: the original key's re-read returns the body. Mask only the other window: the original key's re-read is `[unchanged]`
- [ ] Read, re-read (notice), mask the original body in place, re-read again: the third result is the full body. The notice is skipped
- [ ] After that recovery, another re-read while the recovered body is live is `[unchanged]`. Mask the recovered body and the next re-read returns the body again. The fingerprint key is still present after each recovery (`bucket[key] == (st_mtime_ns, st_size)`)
- [ ] First read records the fingerprint. Clear `conv.messages`. The next `_dedup_read_output` for that key returns `output`
- [ ] Patch `rt._context_strategy._tool_result_limit` to raise `RuntimeError`. The second read returns `output` and does not raise
- [ ] Mask the read body, then append a newer `write_file` tool result for the same path without touching the file (so mtime and size still match). The next `_dedup_read_output` returns the body. A real `write_file` execution is the wrong setup: it changes mtime and misses the fingerprint for a different reason
- [ ] `test_empty_project_path_skips_dedup`, `test_stat_oserror_skips_dedup`, and `test_changed_mtime_is_not_deduped` in `tests/test_spec23_context_working_set.py` stay green
- [ ] Those two test files pass. No full battery

---

## 7. Edge Cases

| Case | Behavior |
|---|---|
| Newest `read_file` for the key is the body, an older one is masked | `[unchanged]` |
| Newest message for that path is `write_file`, `edit_file`, `list_files`, or `search_files`, and the read body is masked | Full body. Those tools are skipped |
| Same, and the read body is still live under the other tool's result | `[unchanged]`. The other tool is skipped; the live read decides |
| Two windows of one path | A different offset or limit does not decide |
| Result content empty or shorter than 500 characters | Not a stub prefix, so it counts as a live body → `[unchanged]`. `select_maskable` would not have masked it |
| Walk finds no `read_file` result for the key | Full body |
| Walk raises | Debug log, full body, no raise to `_execute_tool_batch` |
| File text itself startswith `[unchanged \u2014`, `[masked \u2014`, or `[compacted \u2014` | Treated as a notice or a stub. The re-read sends the body again |
| Body re-masked after recovery | Next re-read recovers again |
| `project_path` empty / `os.stat` raises `OSError` | Return `output` before the walk |
| `offset` or `limit` is a bool or a non-int | Return `output` before the walk (existing guard) |
| Limit omitted vs limit 0 | Different keys. None stays None |
| `keep_last=3` and only one tool result | Nothing is masked. Not a recovery case |
| Orphan tool result (no parent call) | Meta name is `[unknown tool]`, path `""`. Skipped unless the key really is that |

---

## 8. ARCHITECTURE.md Updates

There is no read-dedup paragraph today. §3.21p (line 2069) mentions observation masking, and that section is `agent/context.py`, which does not own fingerprints. Do not add the sentence there. §3.21p.5 owns compaction and the hot-path pin sentence; do not add it there either.

In §3.21m `agent/runtime.py`, after the paragraph that ends "The replay line says the write and its checks already ran.", add:

> Same-turn `read_file` dedup keys `(path, offset or 0, limit)` to `(st_mtime_ns, st_size)` on `_read_fingerprints`, cleared at turn end and by `drop_read_cache`. A re-read returns the full body when no live `read_file` body for that key remains in the transcript (the newest real result is a mask or compact stub, the results were evicted, or the walk fails). `[unchanged]` applies only while a live copy of that same window remains. Notices and results from other tools do not count as that copy.

---

## 9. Self-Audit (Rule 9)

1. Both code samples were traced against `e8911868`. `_dedup_read_output` guards, key, fingerprint, lock, and notice string match lines 3835-3870. The helper's `MessageRole` import matches the local-import pattern. `_tool_result_limit` follows `_tool_result_meta`'s parent walk and deliberately does not copy offset's `None → 0` default. The 4-tuple unpack sites were counted. Stub prefixes match lines 456 and 563-567, including the summarize tail. Dedup-before-`add_tool_result` was read at 2445-2452.
2. Exceptions: `os.stat` already catches `OSError` (parent of `FileNotFoundError` and `PermissionError`) and returns `output` before the walk. `_tool_result_meta` / `_tool_result_limit` can raise `IndexError` on a bad index; the walk never passes one, and `except Exception` still returns False. `content.startswith` raises `AttributeError` if content is not a str; same fallback. The helper does not raise to the tool loop, including when `logger.debug` itself raises. `SystemExit` and `KeyboardInterrupt` are `BaseException` and are not caught.
3. Key is `(path, offset or 0, limit)` with limit `None` or `int`, verified by the existing notice assertion `limit=None`. The walk uses that same triple, plus `tool_name == "read_file"`, because four other tools collide with `(path, 0, None)`.
4. Data flow: guards → stat → fingerprint → walk of stored tool results → body or notice → `add_tool_result`. The in-flight read is not in the transcript. Mask cycle re-entry stays bounded by the 4-observation cadence.
5. An implementer who copies §2 and the §6 setups gets the recovery without disabling whole-file dedup and without letting a `write_file` / `list_files` result hide a masked read. Code blocks use `\u2014`, the same escape as the stub writers and the existing notice.
