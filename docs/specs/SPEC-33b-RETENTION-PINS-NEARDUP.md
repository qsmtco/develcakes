# SPEC-33b: Retention parity, read pins, and near-duplicate loop detection

**Date:** 2026-10-14
**Author:** Supervisor
**Status:** Draft — for implementation
**Implements:** `docs/audits/2026-10-14-CONTEXT-MASK-READ-LOOP-READ-ONLY.md` — ranked fixes **3 and 5**
**Scope:** items 3 + 5 only. **Item 6 is described in §2.4 for a follow-up unit and is NOT authorised here.**
**Depends on:** SPEC-32 (read dedup + stub recovery), SPEC-33 (turn immunity, fail-keep, actionable stubs)
**Baseline:** HEAD `3a24d8c7` — SPEC-33 landed, tree clean
**Work unit:** `mask-loop-2`

---

## DISCOVERY (verified against source, 2026-10-14)

Every claim below was read directly at the stated line. Nothing here is inferred.

- **`_note_tool_path` (runtime.py:3647)** — already implements item 5's *mechanism*: a successful `read_file` counts via `_read_counts`; the **third** counted read stores `(mtime_ns, size)` in `_hot_pins`; a later read of a changed file drops the pin and restarts the count at 1; `edit_file`/`write_file` add to `_edited_paths`; empty path, non-dict args, no `project_path`, or a failed `stat` all return early. Called at runtime.py:2451, guarded by `self._tool_history_lock`.
- **`_protect_paths` (runtime.py:3689)** — returns edited paths **plus** pins whose `mtime_ns` and `size` still match on disk. A pinned file that changed is dropped. This is what feeds `select_maskable(protect_paths=...)` in the mask sweep, so a pinned path is already protected from masking.
- **Item 5 gap is the threshold, not the mechanism.** The count-to-pin ladder exists but only fires on the **third** read. A single read of a large file — the exact shape of the SPEC-33 incident — is never pinned and is therefore maskable while still hot. Item 5 as written in the audit ("pin on first read") is a **threshold change from 3 → 1**, not a new feature.
- **`_check_stuck` (runtime.py:3391)** — two purely **counter-based** checks over `_tool_history` (last-20 ring, `_tool_history_lock`-guarded, cleaned by `_cleanup_tool_history` on terminal transitions). Check 1: same `tool` + same `args_hash` (md5 of `str(sorted(args.items()))`, 8 hex chars) **3+ times in the last 10** calls. Check 2: 8+ `write_file`/`edit_file` with no `exec_command` in the last 8. Returns a string or `None`. **There is no near-duplicate detection** — a `read_file` of `f.py` offset 0 vs `f.py` offset 0 with `limit=2000` vs `f.py offset=800` are three distinct `args_hash` values and none trip check 1. That is precisely the loop shape in the audit's 143-call round.
- **`_key_body_in_transcript` (runtime.py:3847)** — newest-to-oldest walk for a live `read_file` body for `(path, offset or 0, limit)`; **skips `[unchanged —` notices**, requires `role == TOOL_RESULT`, and returns `False` for `[masked —`/`[compacted —` stubs. Wrapped in a bare `except Exception` → `False`. Called by `_dedup_read_output` **before** the new read is appended.
- **`_dedup_read_output` (runtime.py:3885)** — on a fingerprint match, if `_key_body_in_transcript` is `False` it **re-sends the real output** ("Re-sending once is cheaper than an exec_command loop") and refreshes the fingerprint; otherwise it returns the `[unchanged — ...]` notice. Defensive: non-dict args, non-str path, bool/non-int offset or limit, empty `project_path`, failed `stat` → all return `output` unchanged.
- **Item 3 gap is real and is a name-domain gap.** `exec_command` appears **nowhere** in `agent/context_strategy.py` — masking is `read_file`-centric by construction. `_tool_result_meta` drives the SPEC-32 walk. Meanwhile `read_file` has: a dedup notice, a pin ladder, a protected-path list, and a stub-recovery path. `exec_command` has none of these, so an exec result is maskable with no way to detect that its body was lost. This is the asymmetry.
- **`_pending_stuck_messages` (runtime.py:630)** — per-session list written by `_check_stuck`, drained at runtime.py:2992-2996 and prepended as a transient prefix. **A JEV-based near-dup verdict must land in this same queue**, not a new mechanism, or the message will not reach the model.
- **No `stuck`/`pin`/`protect` test file exists.** `tests/` has only `test_dedup_stub_recovery.py` in this area. Item 6 and the item-5 threshold change both need new files.

---

## 1. Overview

### Problem

SPEC-33 closed the three loop *generators* (turn immunity, JEV fail-keep, actionable stubs). Three loop *enablers* from the same audit remain open, and each one degrades a read-heavy turn:

1. **Item 3 — exec retention parity.** `read_file` results survive masking with a full recovery path (dedup notice, pin, protected path, stub recovery). `exec_command` results are maskable with **no** equivalent: once stubbed, nothing in the system notices, and the model's only remedy is to re-run the command.
2. **Item 5 — pins on first read.** A file read once — the exact shape of the SPEC-33 incident — never reaches the 3-read pin threshold, so it stays maskable while the turn is still working from it.
3. **Item 6 — near-duplicate loop detection.** `_check_stuck` only catches *byte-identical* `args_hash` repeats. The 143-call round varied its reads, so it never tripped.

### Solution

Bring `exec_command` into the retention family (item 3) and pin on the **first** successful read (item 5). **Both are mechanical, independently testable, and carry no network dependency.**

Item 6 — the JEV near-duplicate check — is **out of scope for this round** by PM decision. It is the only change that would put a network call on a hot path, and it should ship with real evidence about how often the local pre-filter actually fires. §2.4 keeps the design so the follow-up unit starts from a spec rather than from nothing.

### Scope

| In | Out |
|---|---|
| `exec_command` retention: recovery walk + pin ladder + protect | `exec_command` dedup notice (output is non-deterministic; §7) |
| Pin threshold 3 → 1 | The two existing counter checks (they stay) |
| Tests for both | **Item 6 in its entirety** — no JEV call site in this round (§6 rule 13) |
| Tests: 3 new files / extensions | `panic_prune`, `apply_mask_verdicts`, SPEC-33's turn floor |

### Ordering rationale

**3 → 5 in this round. Item 6 is deferred to its own work unit.**

*PM decision, 2026-10-14:* items 3 and 5 ship together — they are mechanical, independently testable, and carry no network dependency. **Item 6 is not authorised in this round.** It is the only change that puts a network call on a hot path, and it will be specced separately with real evidence about how often the local pre-filter would actually fire. Shipping loop detection with no observed pre-filter hit rate is how you get a change you cannot evaluate.

§2.4 below is **design intent, not this round's scope.** Do not implement it. It is retained so the follow-up unit starts from a spec rather than from scratch.

---

## 2. Changes by File

### 2.1 `agent/context_strategy.py` — `exec_command` in the retention family

**2.1a — `_tool_result_meta` already handles `exec_command`. NO CHANGE — verify only.** *(Corrected 2026-10-14: an earlier draft said this needed an added branch. It does not.)*

Verified by reading `context_strategy.py:624-648`: the method is **tool-agnostic**. It resolves `tc.tool_name` off the producing tool call and returns it, then `args.get("path", "")` yields `""` for `exec_command` because the tool has no `path` argument, and `offset` defaults to `0`. There is no per-tool dispatch table to extend.

An earlier draft of this spec proposed adding `if name == "exec_command": return name, "", 0, parent_index`. **Do not do this** — it would duplicate behaviour the method already provides and add a branch nothing calls.

**Verify, do not edit.** Confirm at implementation time that a masked `exec_command` result reports `("exec_command", "", 0, parent)`, then move on. The stub at `context_strategy.py:575` builds `f"{tool_name} {path_shown}"` with a **space separator**, so an empty path renders as `[masked — exec_command  offset=0 …]` — a deliberate double space matching the audit's own example line, and not a formatting bug to "fix".

**2.1b — `select_maskable` must not treat an empty path as "protected".** The existing protected-path check compares `args.get("path")`; for exec that is absent. Confirm an empty/missing path cannot match a protected entry and therefore stays maskable. This is a **no-change verification**, listed because it is the one place a naive implementation would silently make all exec results permanently protected.

**2.1c — stub text.** The existing F6 suffix already reads `re-read to restore]` for every masked result. For `exec_command` that instruction is actively wrong — you do not "re-read" a command, you re-run it. Add a per-tool recovery verb:

```python
    _RECOVERY_HINT = {
        "exec_command": "re-run to restore]",
        "read_file": "re-read to restore]",
    }
    # default for anything else: "re-run to restore]"
```

Applied in `apply_mask_verdicts` where the stub is built (context_strategy.py:576). All existing `startswith("[masked —")` assertions still pass — this is a suffix change.

### 2.2 `agent/runtime.py` — exec retention in the recovery walk

**2.2a — `_key_body_in_transcript` generalises to exec.** Today it matches only `read_file` and is keyed `(path, offset, limit)`. Generalise to a tool-aware lookup:

```python
    def _body_live_in_transcript(self, conv: Any, tool: str, key: tuple) -> bool:
        """SPEC-33b: does a real body for this (tool, key) still live here?

        Supersedes _key_body_in_transcript, which is read_file-only.
        Newest to oldest. Skips [unchanged — notices and any other tool.
        The first remaining result for the key decides: a [masked — or
        [compacted — stub is False; any other content is True.
        """
```

`key` is `(path, offset or 0, limit)` for `read_file` and `("", 0, None)` for `exec_command`. The old method stays as a **thin wrapper** calling the new one, so `test_dedup_stub_recovery.py` keeps passing unmodified.

**2.2b — exec pin ladder.** `_note_tool_path` currently returns early for any tool that is not `read_file`. Add exec to the counting branch, keyed on a synthetic path so it shares the pin machinery unchanged:

```python
        if tool_name == "exec_command":
            path = "<exec:" + session_key + ">"   # synthetic, never a real path
```

A synthetic key keeps `_protect_paths`' `os.stat` loop correct — it will simply fail to stat and drop the pin, which is the correct behaviour for exec (there is no file to re-check). **Confirm this in code, not in a comment**: an un-stat-able pin must be dropped by the existing `except OSError: drop` branch, never raise.

**2.2c — exec recovery on repeat.** When an `exec_command` result for the same synthetic key is masked and the agent re-runs it, `_dedup_read_output` must re-send the real output rather than a notice. The existing fail-open already returns `output` for exec (no path, no fingerprint match), so the behaviour is correct **by default** — 2.2a/2.2b are what make the *detection* possible. State this explicitly so the implementer does not add redundant logic.

### 2.3 `agent/runtime.py` — pin on first read (item 5)

The ladder already exists. The change is the threshold:

```python
            counts[path] = counts.get(path, 0) + 1
            if counts[path] >= 3:      # current
                pins[path] = fp        # current
```

becomes

```python
            counts[path] = counts.get(path, 0) + 1
            if counts[path] >= 1:      # SPEC-33b item 5: pin on first read
                pins[path] = fp
```

**Consequences the implementer must handle:**

- `_read_counts` becomes write-only bookkeeping. **Leave the dict in place** — the changed-file path (`stored != fp` → `counts[path] = 1`) still reads it, and removing it widens the diff for no gain.
- A pin now expires only via `_protect_paths`' fingerprint mismatch, which is the intended mechanism. Confirm the `stored is not None: return` early-out still short-circuits correctly on repeat reads of an unchanged file.
- **Scope check:** pinning a file on first read means it stays protected for the whole turn. That is the intent — but it does mean a turn that reads fifty files pins fifty paths. Confirm `_protect_paths` stays O(pins) and does no I/O per call beyond the existing `os.stat`. If the existing implementation stats on every call, note it as a known cost rather than optimising it here.

### 2.4 `agent/runtime.py` — JEV near-duplicate check (item 6) — **DEFERRED, DO NOT IMPLEMENT**

> **Status: out of scope for this round** (PM decision, 2026-10-14). The design below is preserved for the follow-up work unit, which will be specced fresh with pre-filter hit-rate evidence. Implementing any of this now would put a network call on a hot path with no measurement behind it.

**New third check inside `_check_stuck`, after the two existing counters.** It must:

1. **Gate on cheap local pre-filtering first.** Only call JEV when the last N calls are all reads of the *same path* with *differing* args — the loop shape from the audit. If the pre-filter does not fire, return without a network call. This keeps the hot path untouched in the common case.
2. **Fail open.** On `None` from `post_decision`, return `None` — identical to today's "not stuck" answer. No warning spam; a JEV outage must not change loop behaviour.
3. **Land in `_pending_stuck_messages`** (runtime.py:630, drained at 2992) so the message actually reaches the model. Not a new queue.

```python
    _NEAR_DUP_WINDOW = 8
    _NEAR_DUP_MIN_DISTINCT = 3      # same path, 3+ differing arg-sets

    def _check_stuck_near_dup(self, session_key: str, tool_name: str, args: dict) -> str | None:
        """SPEC-33b item 6: same file, varying reads, no progress.

        Pre-filter on the local history first; only a real candidate
        shape reaches JEV. None (JEV unavailable or says no) is not stuck.
        """
```

The state text should carry the **actual tool call sequence** — tool name plus the args that varied — not a pre-digest, because the judgement is *about* the variation pattern. Criteria should be `progressing` / `looping` / `insufficient_evidence`, so a low-confidence answer maps cleanly onto today's `None`.

**Do not** make the verdict a hard stop. A JEV call on a hot path that can halt a turn is item 3.14 territory in `docs/proposals/PROPOSAL-jev-decision-substrate.md` — deliberately parked, and it needs a threat model before it is real.

### 2.5 `agent/llm/jev_client.py`

No changes. `post_decision` is the entry point; the fail-open contract is already correct. This is deliberate — item 6 is a *caller* change, not a client change.

---

## 3. Data Flow

```
read_file / exec_command
  └─ _note_tool_path (runtime.py:3647)  ── count read; >=1 → _hot_pins
                                          ── edit → _edited_paths
  └─ _protect_paths (runtime.py:3689)    ── edited + pins still matching mtime/size
        └─ select_maskable(protect_paths=…)  ── protected path never a candidate
              └─ apply_mask_verdicts → stub + recovery hint
                    └─ agent re-reads / re-runs
                          └─ _dedup_read_output → _body_live_in_transcript
                                ├─ live body   → [unchanged — …] notice
                                └─ stub/dead  → real output re-sent
_stuck_check_fn = _check_stuck (middleware, runtime.py:760)
  └─ counters (unchanged)  ── then new: _check_stuck_near_dup
        └─ local pre-filter ── miss → None
             └─ post_decision ── None → None (fail open)
                  └─ _pending_stuck_messages → transient prefix → model
```

---

## 4. File Change Summary

| File | Change | ~Lines | Risk |
|---|---|---|---|
| `agent/context_strategy.py` | per-tool recovery verb only (`_tool_result_meta` needs no change) | +12/−2 | low |
| `agent/runtime.py` | `_body_live_in_transcript` + exec pin + threshold | +55/−9 | medium |
| `agent/llm/jev_client.py` | **none** | 0 | — |
| `tests/test_spec33b_exec_retention.py` | NEW | ~140 | — |
| `tests/test_spec33b_read_pins.py` | NEW | ~110 | — |
| `tests/test_dedup_stub_recovery.py` | unchanged — must still pass | 0 | — |
| `docs/ARCHITECTURE.md` | retention family (exec joins read_file) | +10 | — |
| ~~`tests/test_spec33b_near_dup.py`~~ | **DEFERRED with item 6** | — | — |

**~76 lines changed, no network dependency.** Item 6 is out of scope, which is what brings `agent/runtime.py` down from a high-risk ~97-line change to a medium ~64-line one. Land items 3 and 5 in **two commits**; neither touches the other's files' behaviour.

---

## 5. Implementation Order

1. **Item 3a — verify `_tool_result_meta` is already tool-agnostic** (context_strategy.py:624). No edit. Run `tests/test_dedup_stub_recovery.py` — must stay green.
2. **Item 3b — `_body_live_in_transcript`** with the old method as a wrapper. Dedup suite green.
3. **Item 3c — exec recovery verb** (`re-run` vs `re-read`). `tests/test_context_strategy_tier0.py` green (it has exact-string stub asserts).
4. **Item 3d — exec pin ladder** via a synthetic `<exec:…>` key. New `test_spec33b_exec_retention.py` RED-first, then green.
5. **Commit** — "SPEC-33b item 3: exec_command retention parity". Independently revertible.
6. **Item 5 — threshold 3 → 1.** New `test_spec33b_read_pins.py` RED-first. Confirm `tests/test_dedup_stub_recovery.py` unaffected.
7. **Full suite**, then the pattern sweep (§6).
8. **Commit** — "SPEC-33b item 5: pin on first read", including the ARCHITECTURE.md note and a resolution stamp in the audit doc.
9. **Item 6 is NOT part of this round.** Do not write `test_spec33b_near_dup.py`. Do not call `post_decision` anywhere in this change.

---

## 6. Acceptance Criteria

**Item 3**
1. A masked `exec_command` result is detectable as dead by the generalised walk; a live one is not.
2. The exec stub's recovery hint says **re-run**, not re-read.
3. An exec result with no live body is re-sent in full on the agent's next run of the same command.
4. `read_file` behaviour is **byte-identical** to SPEC-33 — old `_key_body_in_transcript` tests pass unmodified.
5. No exec path is permanently protected: an unpinned, unmasked exec result remains maskable.

**Item 5**
6. A single successful `read_file` results in a pin.
7. A file that changes on disk between reads is dropped from pins and re-pinned at count 1.
8. An empty path, missing `project_path`, or a `stat` failure still skips silently.
9. A file read once, then masked, is protected from masking in the first place.

**Item 6 — DEFERRED. None of the following apply to this round.** (10–13 are carried into the follow-up unit; 14 becomes a *harder* rule here, below.)

**Global**
10. Full suite green, no new skips.
11. `docs/audits/2026-10-14-CONTEXT-MASK-READ-LOOP-READ-ONLY.md` gains a resolution stamp marking items 3 and 5 closed and item 6 explicitly deferred.
12. Tree clean at close; two commits, each independently revertible.
13. **Hard rule:** `grep -rn "post_decision\|jev_client" agent/` shows **no new call site** introduced by this change. Item 6 must not be smuggled in.
14. Pattern sweep: `grep -rn "exec_command" agent/context_strategy.py` → non-empty (item 3 landed).

---

## 7. Edge Cases

| Case | Expected |
|---|---|
| `exec_command` output is non-deterministic | No dedup notice — re-running is correct. Explicitly out of scope. |
| Exec pinned under a synthetic key | `os.stat` fails → pin dropped by the existing `except` branch. No raise. |
| Empty `args` / non-dict | Every new function returns early, matching `_note_tool_path` today. |
| Pin on 50 files in one turn | `_protect_paths` stays O(n) with one `os.stat` per pin. Known cost, not optimised here. |
| JEV low confidence → `None` | Not stuck. Today's behaviour exactly. |
| JEV timeout 2.0 s | `None`. The pre-filter keeps this rare. |
| Near-dup on a legitimately progressive read | Agent sees a nudge, not a stop. Reversible by nature. |
| `[unchanged —` notice during the walk | Skipped, exactly as today. |
| `_read_counts` after threshold change | Write-only but retained; the changed-file path still reads it. |
| Exec stub and `_dedup_read_output` | Returns `output` by default — no new logic needed (§2.2c). |

---

## 8. Design Notes for the Implementer

- **Do not** add a second stuck-message queue. `_check_stuck` already writes to `_pending_stuck_messages`; a new queue is a message the model never sees. *(Relevant to the deferred item 6, not this round.)*
- **The synthetic exec pin key must be unmistakably not a path.** Prefix it `<exec:…>` so a future reader cannot mistake it for a real file.
- **Do not implement item 6, and do not "helpfully" add a JEV call.** The hard rule in §6 exists because this spec still *describes* item 6. Describing it is not authorising it.
- **Keep the two commits separate.** Item 3 (retention parity) and item 5 (pin threshold) are independent changes; neither should need reverting because of the other.

---

## Self-Audit (Rule 9)

- Signatures verified at source this session: `_note_tool_path` (3647), `_protect_paths` (3689), `_check_stuck` (3391), `_key_body_in_transcript` (3847), `_dedup_read_output` (3885), `_pending_stuck_messages` (630, drained 2992). ✅
- `exec_command` confirmed absent from `agent/context_strategy.py` — item 3's premise. ✅
- **`_tool_result_meta` re-read and corrected.** (context_strategy.py:624-648) It is tool-agnostic: it returns `tc.tool_name` and `args.get("path", "")`, so `exec_command` already yields `("", 0)`. The earlier draft's proposed branch was wrong and has been removed — §2.1a is now verify-only. ✅
- Stub separator verified (context_strategy.py:575): `f"{tool_name} {path_shown}"` renders an empty path as a deliberate double space, matching the audit's example line. Not a bug to fix. ✅
- Baseline green before implementation: `tests/test_dedup_stub_recovery.py` + `tests/test_context_strategy_tier0.py` → 28 passed. ✅
- Exceptions enumerated: `_protect_paths` `except OSError` drops un-stat-able pins; `_body_live_in_transcript` keeps a bare `except` → `False`; `_check_stuck_near_dup` (deferred) returns early on non-dict args. ✅
- No invented parameters in this round's scope. `post_decision` and `_DECISION_MIN_CONFIDENCE` exist in `agent/llm/jev_client.py` but are **not called** by this change. ✅
