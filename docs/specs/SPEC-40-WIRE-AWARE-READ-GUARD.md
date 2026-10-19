# SPEC: Wire-Aware Read Guard + Hot-File Carry v2 (compile-packet spiral fix)

**Date:** 2026-10-18 (v2, incorporates external audit)
**Author:** Supervisor
**Status:** Draft — for implementation (supersedes v1 in full)
**Implements:** Supervisor investigation 2026-10-18 + audit findings
**Depends on:** SPEC-32 (read dedup), SPEC-33b (transcript body walk), SPEC-24 (compiled branch), D1 tool-adjacency (models/conversation.py)
**Target branch:** main

> Compliance: production changes confined to `agent/runtime.py` (wire payload
> owner). Documentation change in `docs/ARCHITECTURE.md` §compiled-packet
> (~:1874). No provider, serialization, or strategy changes. Follows existing
> `_tool_history_lock` patterns.

---

## 0. Corrections from the audits (accepted — rounds 1 and 2)

**Round 1:**
1. Turn died at **150** iterations (`~/.config/develcakes/agent.json:6`
   `max_tool_iterations: 150`); 50 is the config default only.
2. First repeat gets `[unchanged`; **REFUSED is the second rung**
   (`_reread_notices` starts at 0 → 1 → notice, 2 → REFUSED).
3. A re-read of the **just-written file** does not hit the guard — the
   fingerprint `(mtime_ns, size)` misses and the body serves fresh. The
   spiral victims are *unchanged-by-the-write* files.
4. Offsets/grep are NOT blocked — different `(offset, limit)` is a new key;
   `grep` is not in `_REPRINT_COMMANDS` (cat/sed/head/tail/nl/less/bat).
   The turn still got bodies for 64 of 82 reads. `sed` path-scope is what
   blocked the shell path.
5. Enforced write = enforcement_checks non-empty. Failed edits and skip
   patterns store nothing. All 8 writes this turn qualified.
6. Call-site line for `_block_file_reprint` is **:2288** (v1 said 2284).
   Fingerprint pop at turn start is **:1765** (v1 said 1771).
7. Part B v1 selection algorithm #1 carried nothing (counts freeze at 1;
   `_protect_paths` = edited ∪ live pins, so `path in protect → skip`
   empties the list). Replaced wholesale in v2.
8. Part B v1 read `f.read(16*1024)` from byte 0 — the header of a 200KB
   file, not the window the model sought. Replaced with serve-time capture.
9. `after`-index slide under message deletion is a **pre-existing wire-slice
   limitation** — documented as out of scope (§7).
10. ARCHITECTURE.md update belongs in the scope table (v1 banner implied
    runtime-only).

**Round 2 (verified against the fix-chunk transcript by Supervisor):**
11. Plain `wins[key] = ...` on an existing key does NOT move it to the end
    — the next new file would evict the window just re-read. Fixed with
    pop-then-set (§2.2a).
12. Capture must run at EVERY real-body return — SPEC-32 recovery, off-wire
    serve, and fingerprint miss — not just the tail. All three specified
    (§2.2a).
13. The two slots hold the LAST reads, not the most useful: on this turn
    the slots at write time were prompt_loader/work_persistence slices and
    a 180-char probe — not the brief, not the awareness region sought.
    Mitigations: probe floor (512 chars), write-evicts-own-window. Part A
    remains the mechanism that returns the sought window. Part B claims
    continuity only, no more.

## 1. Overview

### Problem

Two subsystems disagree about what the model can see:

1. `_maybe_compile` (:3641) stores a packet on every enforced write.
   `_compiled_messages` (:3461) builds the wire as `system + compiled user
   message + conv.messages[packet["after"]:]` — every pre-write tool result
   is off the wire.
2. `_dedup_read_output` (:3935) / `_block_file_reprint` (:3988) decide
   "already in this conversation" by walking the **full transcript**
   (`_body_live_in_transcript` :3898). The SPEC-32 recovery (re-serve when
   the body is gone) only triggers for masked/compacted/missing bodies —
   not for wire-cut bodies, which stay "live" in the transcript.

Result: after a write, re-reads of unchanged files return `[unchanged` then
`REFUSED`, `sed` is refused by path-scope, and the model loops — this turn:
182 calls, 8 packet resets, 39 guard firings, dead at 150 iterations.
Compaction never fired (0 stubs, ~95K tokens vs 600K soft ceiling) and is
not involved.

### Solution

- **A. Wire-aware guard:** serve the body fresh when the newest live body
  for that key sits below `packet["after"]` (off the wire).
- **B. Hot-file carry:** capture the exact `(path, offset, limit)` window
  bodies of the last two successful reads **at read time** and replay them
  verbatim in the compiled message.

### Scope

| In | Out |
|---|---|
| `agent/runtime.py` guard + packet + hot windows | Providers, models/conversation.py |
| `tests/test_spec40_wire_aware_guard.py` (new) | agent/context_strategy.py |
| `docs/ARCHITECTURE.md` compiled-packet ¶ (~:1874) | Settings UI, prompts, tools.py |

## 2. Changes by File

### 2.1 Part A — wire-aware guard (all in agent/runtime.py)

**(a) `_wire_cutoff(session_key, conv) -> int | None`** — return
`packet["after"]` for the session's compile packet, else None. Pure dict
read; no lock needed (`self._compile` is replaced whole, never mutated).

**(b) `_last_transcript_body_index(conv, tool, key) -> int | None`** —
mirror of `_body_live_in_transcript` (:3898) returning the index of the
newest live body. MUST: walk newest-first; skip `[unchanged` notices; match
`_tool_result_meta`/`_tool_result_limit` against the key; return the first
live index; return None when none. Wrap the ENTIRE walk in
`try/except Exception: return None` — identical swallow semantics to
`_body_live_in_transcript`; an exception here must not escape the tool
batch after `_key_body_in_transcript` already said live.

**(c) `_dedup_read_output` (:3935), fingerprint-match branch** — after the
existing `_key_body_in_transcript` check and BEFORE the notice-counter
increment:

```python
cutoff = self._wire_cutoff(session_key, conv)
if cutoff is not None:
    idx = self._last_transcript_body_index(
        conv, "read_file", (path, offset or 0, limit))
    if idx is None or idx < cutoff:
        # Only live body is off the wire (compile packet replaced it).
        # Serve fresh; reset this key's notice counter.
        seen = self._reread_notices.setdefault(session_key, {})
        seen.pop((path, offset or 0, limit), None)
        return output
```

Returns `output` before incrementing — both the `[unchanged` and REFUSED
rungs are skipped for off-wire bodies. `bucket[key] = fp` re-pin happens on
the fingerprint-miss tail path (unchanged).

**(d) `_block_file_reprint` (:3988) — signature grows `conv`:**

```python
def _block_file_reprint(self, session_key, conv, args) -> str | None:
```

Call site :2288 becomes
`self._block_file_reprint(session_key, conv, args)` (verified: `conv` is in
scope in `_execute_tool_batch`). Path-set construction: a path is refused
only when SOME fingerprint key of that path has a live body at index ≥
cutoff. A path with only off-wire bodies is not refused. Documented
behavior (accepted by design): once a sed/cat of an off-wire-only path is
allowed, it stays allowed until a NEW read_file of that path lands at or
after the cutoff — the shell result is not a read_file body and does not
re-arm the refusal. This is the deliberate relaxation that breaks the
spiral; the read-side guard (c) remains the strict one.

**(e)** No change to `_body_live_in_transcript` (its heading in v1 was
misleading; the snippet was right). Its transcript walk is load-bearing for
`tests/test_spec33b_exec_retention.py` (exec_command retention) and stays.

### 2.2 Part B — hot-window carry (agent/runtime.py)

**(a) Capture at EVERY real-body return, inside the lock.** New lock-free
helper (callers hold `_tool_history_lock`; all three call sites do):

```python
_HOT_WINDOW_MIN_CHARS = 512  # a probe read must not evict a real window

def _capture_hot_window(self, session_key, path, offset, limit, output) -> None:
    """Store one served read window as a hot-carry candidate.

    Callers hold _tool_history_lock. Pop-then-set so a re-read moves the
    key to the END (plain assignment would keep stale insertion order and
    let the next new file evict the window just re-read). Bodies below
    _HOT_WINDOW_MIN_CHARS are not stored — a 1-byte probe must not occupy
    a slot (fix-chunk turn: msg-260 read of 180 chars would have evicted
    a real window at the next write).
    """
    if len(output) < _HOT_WINDOW_MIN_CHARS:
        return
    key = (path, offset or 0, limit)
    wins = self._hot_windows.setdefault(session_key, {})
    wins.pop(key, None)
    wins[key] = {
        "path": path, "offset": offset or 0, "limit": limit,
        "body": output[:16 * 1024],
    }
    while len(wins) > 2:  # evict OLDEST by insertion order
        wins.pop(next(iter(wins)))
```

**Three call sites** in `_dedup_read_output` — every path that returns a
real body:
1. the SPEC-32 recovery return (`bucket[key] = fp; return output` inside
   the match branch),
2. the new off-wire return (§2.1c — this path does NOT write
   `bucket[key] = fp`; the fingerprint already matched),
3. the fingerprint-miss tail return.

And **one eviction site** in `_note_tool_path` (:3720): when
`tool_name in ("edit_file", "write_file")`, drop any hot window whose
`path` equals the written path. The hunk is the authority for a written
file; without this, a second edit with no re-read ships the pre-image of
the EARLIER edit beside the new hunk. A re-read after the write re-captures
the post-edit bytes through the normal serve paths.

New instance attr `self._hot_windows: dict[str, dict]` initialized beside
`_read_fingerprints` (:637 area). Bodies are the verbatim served bytes —
no disk re-read, no staleness, no encoding, no sandbox question. Cap each
body at 16KiB. Pop `_hot_windows[session_key]` wherever `_read_fingerprints`
is popped (turn start :1765, `drop_read_cache`, `_cleanup_tool_history`
:3996 area).

**Known limitation, accepted (audit round 2):** the two slots hold the
LAST reads, not the most useful ones — on the fix-chunk turn they were
prompt_loader/work_persistence slices and a tiny probe, not the brief or
the awareness region the model was after. Part A is the mechanism that
returns the sought window; Part B is continuity for whatever was just
read, with probes excluded and written files evicted. Do not promise more.

**(b) Snapshot into the packet — `_maybe_compile` (:3641):**

```python
with self._tool_history_lock:
    hot = tuple(
        {"path": w["path"], "offset": w["offset"],
         "limit": w["limit"], "body": w["body"]}
        for w in self._hot_windows.get(session_key, {}).values()
    )
self._compile[session_key] = {
    "goal": goal, "path": path, "hunk": hunk, "checks": stored,
    "after": len(conv.messages), "hot": hot,
}
```

No selection heuristic, no pins, no stat checks — the captured bodies ARE
the ones the model was using when it wrote. Edited-file exclusion is
unnecessary: the body is the pre-write served copy; the hunk shows the
edit; together they are the truth. A later re-read that changes bytes
replaces the window via (a).

**(c) Replay — `_compiled_messages` (:3461):** gains `hot: bool = True`
keyword. When True and `packet.get("hot")`:

```python
parts = []
for w in packet["hot"]:
    parts.append(
        f"### {w['path']} offset={w['offset']} limit={w['limit']} "
        f"(as read before the write above; re-read if unsure)\n\n"
        f"```\n{w['body']}\n```"
    )
hot_text = ("\n\n## Hot files (recently read, kept for continuity)\n\n"
            + "\n\n".join(parts))
```

Append after the `## Checks` block in `body`. Escaping: bodies containing
a triple backtick close the fence early — acceptable for v2 (byte-verbatim
matters more than fence purity); noted in §7.

**(d) Over-budget retry — `_messages_for_model` (:3599) packet branch:**

```python
messages = self._compiled_messages(conv, packet, block)
compiled = self._compile_token_count(conv, messages, tools)
if compiled >= effective and packet.get("hot"):
    messages = self._compiled_messages(conv, packet, block, hot=False)
    compiled = self._compile_token_count(conv, messages, tools)
if compiled >= effective:
    raise self._over_budget_error(compiled, model_max)
return messages
```

Hot text is inside the compiled user message, so `_wire_text` counts it
already (verified against `_compile_token_count` :3519, which runs the
estimator over the wire list). A packet with no `hot` key stays falsy —
existing compiled-message tests keep the no-retry path.

### 2.3 Files NOT changed (Rule 8)

- `models/conversation.py` — D1 serialization is correct; the wire cut is a
  packet-index decision.
- `agent/context_strategy.py` — compaction not involved; `panic_prune`/
  `prune_tool_outputs` stubs already flip `_body_live_in_transcript` to
  False (recovery path intact).
- `agent/tools.py` — `MAX_READ_SIZE` untouched; hot-window capture lives in
  the runtime where the served body already passed sandbox checks.
- `ui/agent_runtime/session.py` — `/clear` already drops both stores
  together (session.py:88-89); an active tool loop returns before either
  drop — unchanged semantics.

## 3. Data Flow

Spiral today: `read_file(A,w)` → edit #1 → packet `after=N` → wire = system
+ compiled + msgs[N:] → model re-reads A (unchanged file: fingerprint
MATCHES) → first rung `[unchanged`, second `REFUSED` → sed → path-scoped
refusal (a tail window of A from byte 0 ≠ the sought window but still
re-arms the refusal) → … dies at 150.

After fix: the re-read's newest live body index < `after` → body served,
notice counter reset. The model proceeds. With B, the exact windows ride
the compiled message — re-reads become rare; when they happen, A serves
them honestly. Off-wire sed of a path with zero tail bodies runs.

## 4. File Change Summary

| File | Change | Lines | Risk |
|---|---|---|---|
| agent/runtime.py | `_wire_cutoff`, `_last_transcript_body_index`, guard insert, `_block_file_reprint` conv param, `_hot_windows` store + capture + snapshot + replay + retry | ~90 | Med |
| tests/test_spec40_wire_aware_guard.py | new | ~200 | — |
| docs/ARCHITECTURE.md | compiled-packet ¶ gains hot-window + guard sentence (~:1874) | ~4 | Low |

## 5. Implementation Order

1. (b) `_last_transcript_body_index` + unit test (newest-first, notice
   skip, exception swallow).
2. (a) `_wire_cutoff` + unit test.
3. (c) guard insert + off-wire serve/reset test.
4. (d) `_block_file_reprint` conv param + call site + off-wire-allow /
   on-wire-refuse tests.
5. B(a) `_capture_hot_window` + tests: pop-then-set move-to-end, probe
   floor, write-evicts-own-window (in `_note_tool_path`), all-three-returns
   capture (one test per return path).
6. B(b)+(c) packet snapshot + replay + count test.
7. B(d) retry path test (fake tiny-window provider).
8. Full xvfb suite; failure set diff identical vs
   `.crabcakes/reports/wu-project-memory-index/fullsuite.log` (5299P/63F/5S
   baseline; tree carries uncommitted fix-chunk — run on the current tree
   and compare against a same-tree baseline if in doubt).

## 6. Acceptance Criteria

- [ ] After one enforced write, re-`read_file` of an unchanged file whose
      newest body predates the packet serves the BODY.
- [ ] That key's `_reread_notices` entry resets (next on-wire repeat starts
      at `[unchanged`, not REFUSED).
- [ ] On-wire bodies keep the notice ladder exactly as today.
- [ ] Off-wire-only path: first sed runs. On-wire path: sed refused.
- [ ] Allowed sed stays allowed until a new read_file of that path lands
      in the tail (documented; test pins the behavior).
- [ ] Packet carries ≤2 windows; bodies are the verbatim served bytes.
- [ ] A re-read of a stored key MOVES it to the end (pop-then-set): the
      next new file evicts the OLDEST, never the just-re-read window.
- [ ] A read body < 512 chars stores nothing (probe exclusion).
- [ ] A write to path P evicts P's hot window before the packet snapshot
      (no pre-image beside the new hunk).
- [ ] All three real-body returns capture: SPEC-32 recovery, off-wire
      serve, fingerprint miss (test each path in isolation).
- [ ] Compiled message contains the hot section; count-with-hot >
      count-without (assert via `_compile_token_count`).
- [ ] Over-budget + hot → retry without hot → no raise.
- [ ] Spiral regression: a scripted double that re-reads the same key
      until a body arrives, then proceeds — asserts the BODY ARRIVES on
      the first post-write re-read and the double completes; no arbitrary
      iteration cap. Today the same double climbs the notice ladder.
- [ ] Baseline suite delta = new tests only.

## 7. Edge Cases

| Case | Behavior |
|---|---|
| No packet (plain branch) | cutoff None; guard unchanged |
| Body before AND after cutoff | newest-first walk → newest index decides (on-wire → notice path) |
| `after` > len(messages) | idx < cutoff always → fresh serve (safe direction) |
| Compaction DELETES messages below `after` | indices slide; the wire slice itself is wrong today (pre-existing, out of scope); guard may serve an extra copy — benign |
| Hot window deleted from disk | body was captured at serve time — replay is unaffected; note in section says "as read before the write" |
| Body contains triple backticks | fence closes early — accepted v2 limitation (byte-verbatim > fence purity) |
| Probe read (<512 chars) | not stored — cannot evict a real window |
| Write with no re-read | written path's window evicted at write; hunk is the authority |
| Over-budget WITH hot still over without | raise (unchanged) |
| Session reset mid-turn | packet + fingerprints + hot windows popped together (same sites) |
| Empty `conv.project_path` | irrelevant — capture happens on serve, which already required a project path |

## 8. ARCHITECTURE.md Updates Required

`docs/ARCHITECTURE.md` compiled-packet paragraph (~:1874) — append two
sentences: hot-window carry (last two served read bodies ride the compiled
message; over-budget drops them first) and the wire-aware guard contract
(the transcript is an audit trail; the guard serves fresh when the wire
cannot see the body).

---

## Self-Audit (Rule 9)

- All anchors re-verified post-audit: :2288 call site, :1765 fingerprint
  pop, `_note_tool_path` early-return (counts freeze), `agent.json`
  max_tool_iterations 150, ARCHITECTURE.md :1874 text, msg-319 tail window
  (read_file awareness offset=0 limit=3000 after last edit at :293).
- **Round-2 audit verified in the transcript by Supervisor before edit:**
  the 180-char probe read (msg 260), the slot contents at each write
  (prompt_loader/work_persistence slices at edits 1/3; tiny probes at
  edit 8), and dict-assignment ordering semantics.
- Part B v1's dead selection algorithm removed; replaced with serve-time
  capture — no `_protect_paths` interaction, no disk reads, no stat races.
  Round-2 amendments applied: pop-then-set move-to-end, three-return
  capture, 512-char probe floor, write-evicts-own-window.
- `_last_transcript_body_index` exception handling specified to match
  `_body_live_in_transcript` semantics.
- Scope table now includes ARCHITECTURE.md; banner corrected.
- Acceptance rewritten: defined double, body-arrival assertion, no
  "under 15 iterations" hand-wave; probe/eviction/ordering properties
  pinned.
