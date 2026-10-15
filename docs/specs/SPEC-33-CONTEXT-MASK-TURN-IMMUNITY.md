# SPEC: Context-mask turn immunity, JEV fail-keep, and actionable stubs

**Date:** 2026-10-14
**Author:** Supervisor
**Status:** Draft — for implementation
**Implements:** docs/audits/2026-10-14-CONTEXT-MASK-READ-LOOP-READ-ONLY.md (findings F1, F2, F5, F6; keep_last verdict)
**Depends on:** SPEC-23, SPEC-30, SPEC-31, SPEC-32
**Target branch:** main
**Work unit tag:** `mask-loop-1`
**Baseline:** HEAD `c30cb5e5` **plus two uncommitted dirty edits** — see §2.5 (step 0 reverts them)

> **Architecture compliance:** The pluggable context strategy (`agent/context_strategy.py`) is the sole owner of masking semantics (SPEC-AUDIT-CLEANUP-2 Phase 4). Turn-immunity selection logic lives in the strategy; the runtime keeps wiring (cadence, JEV call, band gate). No UI, transport, or storage changes.

---

## DISCOVERY (verified against source, 2026-10-14)

- Read `agent/context_strategy.py` — `select_maskable(conv, keep_last=6, protect_paths=())` (dirty; HEAD default is 3) builds `tool_result_indices` **newest-first**, slices `[keep_last:]`, then per-index checks in order: already-masked/compacted skip → `<500` chars skip → protected path skip → candidate. `apply_mask_verdicts(conv, indices, verdicts: dict | None)` treats `None` as mask-**every**-index; dict maps `candidate_{n}` → keep/mask/summarize; missing/invalid key = mask; `summarize` keeps mask line + first 200 chars; sets matching parent `tc.result = None`; invalidates `conv._token_estimate_cache` after each mutation. `panic_prune` already has a turn guard: `last = self._last_user_index(conv)`, `None → return 0`, only indices `< last` masked. `_last_user_index` walks newest→oldest for `MessageRole.USER`. `mask_old_observations` wraps select+apply with `None` verdicts — **no production callers** (grep-verified).
- Read `agent/runtime.py` — cadence at the top of each tool-loop iteration (~:1858): `if self._obs_since_mask.get(session_key, 0) >= 4: reset to 0; self._maybe_mask_observations(session_key, conv)`. `_maybe_mask_observations` (~:3742): band gate `landing_goal < estimate <= soft_ceiling`; `_protect_paths`; `select_maskable(conv, protect_paths=protect)` (HEAD had explicit `keep_last=3`); empty → return; `classify_mask_candidates(state, candidates)`; `apply_mask_verdicts(conv, indices, verdicts)`. `_mask_candidate_state(conv, indices) -> tuple[str, list[dict]]` (~:3719). Landing goal = `min(int(hard*0.15), 24_000)`.
- Read `agent/llm/jev_client.py` — `classify_mask_candidates(state, candidates)` returns `dict` on success, `{}` for empty candidates, **`None` when the whole call fails** (bad state, garbage answer, HTTP failure — all collapsed to None). Never raises (test-pinned: `test_bad_state_does_not_raise`).
- Read tests — `tests/test_spec23_context_working_set.py::test_mask_batch_resets_so_later_calls_do_not_rewrite` drives `_run_loop` with a fake LLM issuing 7 `read_file` calls, patches `classify_mask_candidates → None`, asserts oldest result masked, newest 3 unmasked, `_obs_since_mask == 3`. **Currently FAILING on the dirty tree** (keep_last=6 masks nothing) — this is pre-existing, not your regression; step 0 restores green. That `None` patch and the `len(tools) == 7` assert are the pre-fix contract; §6 changes both (fail-keep would otherwise mask nothing, including the seeded prior turn). `tests/test_jev_mask_adjudication.py::test_whole_batch_failure_masks_all` calls `apply_mask_verdicts(conv, indices, None)` **directly at the strategy layer** and asserts all masked — this contract must survive (see §2.3). `tests/test_context_strategy_tier0.py::test_under_landing_goal_does_not_mask` gates out via band (estimate=100) — unaffected. Prefix stub assertions (`startswith("[masked —")`, or a trailing-space prefix such as `offset=0 `) survive a suffix: spec23, panic_prune, dedup_stub_recovery, jev. **Exception:** `tests/test_context_strategy_tier0.py::test_select_maskable_keeps_short_recent_and_protected` asserts the full old line through `]` — update it with the hint (§2.3). `tests/test_context_strategy.py::test_prune_does_not_rewrite_masked_body` seeds an old stub as prune input and asserts identity; leave it.
- Architecture owner: `agent/context_strategy.py` owns masking; `agent/runtime.py` owns cadence + wiring.

---

## 1. Overview

### Problem

The periodic observation-mask sweep (SPEC-30) generates a live read-loop: every 4 tool results it masks older results with **no current-turn guard**, so a read-heavy turn deletes its own working set mid-turn. Two amplifiers: a failed JEV DecisionModel call returns `None` verdicts, which the sweep passes straight to `apply_mask_verdicts` = **mask everything**; and the mask stub gives the model no recovery instruction, so it fragments reads instead of re-reading.

Live evidence (audit): a 143-tool-call Coder round (100 exec, 40 read, **0 writes**) that ended "Stopped — I'm halting the round here without writing anything", and a 7-minute supervisor feed loop of near-duplicate git probes re-issued every ~40s.

### Solution

1. **Turn immunity (F1/F2):** the periodic sweep never masks results produced since the last user message — the same floor `panic_prune` already uses.
2. **JEV fail-keep (F5):** in the **periodic sweep only**, `verdicts is None` masks nothing and logs a loud warning. (Panic-prune's mask-all stays: over-ceiling emergency, intentional.)
3. **Actionable stubs (F6):** mask stubs end with a recovery instruction.
4. **Revert the dirty `keep_last` 3→6 bump** (§2.5): with turn immunity, the sweep only touches previous-turn results; SPEC-23/30 pin 3.

### Scope

| In | Out |
|---|---|
| `select_maskable` / `mask_old_observations` turn floor | `panic_prune` (already correct) |
| `_maybe_mask_observations` fail-keep + floor wiring | `jev_client.py` (None contract stays) |
| Mask stub suffix | exec retention parity (audit item 3 → future SPEC-35) |
| `keep_last` revert to 3 | pins-on-first-read (item 5), stuck-detector near-dup (item 6), repo-map header (item 8) |
| New RED-first test file + spec23 fixture update | `[unchanged —]` notice wording (already correct post-SPEC-32) |

---

## 2. Changes by File

### 2.1 `agent/context_strategy.py` — `select_maskable`: turn floor + revert default

```python
def select_maskable(
    self,
    conv: Conversation,
    keep_last: int = 3,
    protect_paths: tuple[str, ...] = (),
    *,
    after_index: int | None = None,
) -> list[int]:
    """Tool-result indices eligible for masking.

    Kept: the ``keep_last`` newest tool results, anything at or after
    ``after_index`` (last user message — current turn is immune), a path in
    ``protect_paths``, a body under 500 characters, and anything
    already masked or stubbed.
    """
    if keep_last < 0:
        keep_last = 0
    protected = protect_paths or ()
    tool_result_indices: list[int] = []
    for i in range(len(conv.messages) - 1, -1, -1):
        if conv.messages[i].role == MessageRole.TOOL_RESULT:
            tool_result_indices.append(i)
    maskable: list[int] = []
    for idx in tool_result_indices[keep_last:]:
        if after_index is not None and idx >= after_index:
            continue
        msg = conv.messages[idx]
        # ... existing body unchanged: already-masked skip, <500 skip,
        # protected-path skip, then maskable.append(idx)
    return maskable
```

Traced: `after_index=None` (default) skips the check → exact old behavior for existing callers. `after_index >= 0` skips every tool result at or after that index — the current turn, which sits strictly after the last user message — and leaves earlier results eligible. Same cutoff as `panic_prune` (`i >= last: continue`). `keep_last` still slices the global newest-first list before this check, so current-turn results occupy those slots and a previous-turn result is a candidate only once it falls outside that window. Negative `after_index` matches every real index (`idx >= 1` is true for any negative floor), so the sweep masks nothing — harmless, no caller passes a negative floor, no special case. Exceptions: none — pure list walking.

### 2.2 `agent/context_strategy.py` — `mask_old_observations`: forward + revert default

```python
def mask_old_observations(
    self,
    conv: Conversation,
    keep_last: int = 3,
    protect_paths: tuple[str, ...] = (),
    *,
    after_index: int | None = None,
) -> int:
    """... update docstring to mention after_index ..."""
    if keep_last < 0:
        keep_last = 0
    return self.apply_mask_verdicts(
        conv,
        self.select_maskable(conv, keep_last, protect_paths, after_index=after_index),
        None,
    )
```

No production callers (grep-verified). Tier0 and SPEC-23 tests call it with `keep_last=` explicitly — unaffected, because `after_index` defaults to `None`.

### 2.3 `agent/context_strategy.py` — `apply_mask_verdicts`: stub suffix (F6)

Replace the `masked = (...)` assignment (inside the loop, after `_tool_result_meta`):

```python
            masked = (
                f"[masked — {tool_name} {path_shown} offset={offset} ({n_chars} chars removed)"
                " — re-read to restore]"
            )
```

Resulting line: `[masked — exec_command  offset=0 (3737 chars removed) — re-read to restore]`. Prefix assertions stay green (`startswith("[masked —")`, or a trailing-space prefix). **One assertion does not:** `tests/test_context_strategy_tier0.py::test_select_maskable_keeps_short_recent_and_protected` expects the full old line through the closing `]`:

```python
assert masked[1].startswith("[masked — read_file f1.py offset=0 (520 chars removed)]")
```

Update it so the expected prefix includes the hint and the bracket stays at the end: `"[masked — read_file f1.py offset=0 (520 chars removed) — re-read to restore]"`. `_key_body_in_transcript` (SPEC-32 walk) matches on `startswith("[masked —")` — suffix-safe. `summarize` verdict unchanged: mask line (now with hint) + `\n` + first 200 chars. **Do NOT touch the `None`-masks-all contract here** — `panic_prune` and `tests/test_jev_mask_adjudication.py::test_whole_batch_failure_masks_all` depend on it.

### 2.4 `agent/runtime.py` — `_maybe_mask_observations`: floor + fail-keep + explicit keep_last

```python
    def _maybe_mask_observations(self, session_key: str, conv) -> None:
        """Tier 1 then one JEV batch, only between the landing goal and the trigger."""
        soft_ceiling, hard_ceiling = self._compute_compaction_threshold(conv)
        landing_goal = self._compute_landing_goal(hard_ceiling)
        estimate = conv.get_token_estimate()
        if not (landing_goal < estimate <= soft_ceiling):
            return
        strategy = self._context_strategy
        floor = strategy._last_user_index(conv)
        if floor is None:
            return  # no user message: nothing is "old" — mirror panic_prune
        protect = self._protect_paths(session_key, conv)
        indices = strategy.select_maskable(
            conv, keep_last=3, protect_paths=protect, after_index=floor,
        )
        if not indices:
            return
        from agent.llm.jev_client import classify_mask_candidates
        state, candidates = self._mask_candidate_state(conv, indices)
        verdicts = classify_mask_candidates(state, candidates)
        if verdicts is None:
            logger.warning(
                "[mask] JEV batch failed for sk=%s — keeping all %d candidates",
                session_key, len(indices),
            )
            return
        strategy.apply_mask_verdicts(conv, indices, verdicts)
```

Traced: `classify_mask_candidates` never raises (returns None on failure); `logger` exists at module scope; `_last_user_index` returns `int | None`. Return value of `apply_mask_verdicts` (int count) deliberately ignored — same as current code; mutation is the effect.

### 2.5 Step 0 — revert the dirty working tree

`git checkout -- agent/context_strategy.py agent/runtime.py` before starting. This restores `keep_last=3` defaults and the explicit `keep_last=3` call site, and turns the currently-failing `test_mask_batch_resets_so_later_calls_do_not_rewrite` back to green (verify before writing code). The audit doc `docs/audits/2026-10-14-CONTEXT-MASK-READ-LOOP-READ-ONLY.md` is **untracked** — include it in the final commit.

**Files NOT changed:**
- `agent/llm/jev_client.py` — None-on-failure contract stays; interpretation moves to the runtime call site.
- `panic_prune` — over-ceiling mask-all is intentional.
- `_key_body_in_transcript` / `_dedup_read_output` — SPEC-32 notice wording already correct.
- `_note_tool_path` / pins, `_check_stuck`, repo-map header — audit items 5/6/8, deferred.
- `[compacted — ...]` compaction stub — separate mechanism (SPEC-24), not implicated.

---

## 3. Data Flow

Tool-loop iteration top (`runtime.py` ~:1858) → `_obs_since_mask >= 4` → reset counter → `_maybe_mask_observations(sk, conv)` → band gate (`landing_goal < estimate <= soft_ceiling`) → `_last_user_index` → `None`? skip (nothing old) → `select_maskable(keep_last=3, protect_paths, after_index=floor)` → empty? return → `_mask_candidate_state` → `classify_mask_candidates` → `None`? **warn + keep all** → `apply_mask_verdicts(conv, indices, verdicts)` → stubs written with recovery hint, parent `tc.result=None`, token cache invalidated.

---

## 4. File Change Summary

| File | Change | ~Lines | Risk |
|---|---|---|---|
| `agent/context_strategy.py` | floor param ×2, default 6→3 ×2, stub suffix, docstrings | +20/−6 | low |
| `agent/runtime.py` | floor wiring, fail-keep, explicit keep_last, warning | +12/−3 | medium |
| `tests/test_spec33_mask_turn_immunity.py` | NEW — 6 RED-first tests | ~180 | — |
| `tests/test_spec23_context_working_set.py` | fixture: seed prior turn; `None` patch → `{}`; `len(tools) == 8` | ~15 | low |
| `tests/test_context_strategy_tier0.py` | full-line stub assertion gains the recovery hint | ~1 | low |
| `docs/audits/2026-10-14-...md` | untracked → committed | 0 | — |

---

## 5. Implementation Order

0. **Revert dirty files** (§2.5). Verify: `.venv/bin/python -m pytest tests/test_spec23_context_working_set.py -q` → green.
1. **Write the new test file** (§6) — run it, confirm RED for tests 1, 2, 3, 5, and 6. Test 4 stays green (panic still mask-alls on `None`).
2. Strategy changes (§2.1–2.3), including the tier0 full-line assertion update (§2.3).
3. Runtime changes (§2.4).
4. Spec23 fixture update (§6 note): seed the prior turn, point `classify_mask_candidates` at `{}`, assert 8 tool bodies.
5. Full targeted suite — paste actual output in the report:
   `.venv/bin/python -m pytest tests/test_spec33_mask_turn_immunity.py tests/test_spec23_context_working_set.py tests/test_jev_mask_adjudication.py tests/test_context_strategy_tier0.py tests/test_panic_prune.py tests/test_dedup_stub_recovery.py -q`
6. Runtime smoke: `.venv/bin/python -m pytest tests/test_agent_runtime.py -k "mask or panic or compact" -q`
7. Pattern sweep: `grep -rn "keep_last" agent/` → defaults are 3, call site explicit; `grep -rn "chars removed)]" agent/` → **zero hits** (old stub tail gone).
8. Commit everything in one commit: `SPEC-33: context-mask turn immunity, JEV fail-keep, actionable stubs (fixes read-loop)`.

---

## 6. Acceptance Criteria — new tests (`tests/test_spec33_mask_turn_immunity.py`, RED-first)

1. `test_sweep_never_masks_current_turn_results` — one user message, then 7 big tool results (no later user message); force `conv.get_token_estimate` into the band; patch `classify_mask_candidates` to `return_value={}`; call `rt._maybe_mask_observations(sk, conv)`; assert **zero** results masked. `{}` is not `None`, so fail-keep does not apply, and a missing key still masks. Today the results outside `keep_last=3` mask → RED. After the floor, `select_maskable` returns empty and the sweep returns before apply. Fail-keep alone leaves this red. (RED today.)
2. `test_sweep_masks_previous_turn_after_new_user_message` — seed one user message and one big prior-turn tool result, then a new user message, then four big current-turn results; force band; patch `classify_mask_candidates` to `return_value={}`; sweep; assert the prior-turn result is masked and all four current-turn results are intact. `{}` is required: an unpatched call returns `None` when no DecisionModel card is configured, and fail-keep would then keep the prior result on the corrected code. `keep_last=3` slices newest-first before the floor. The newest three current-turn results fill that window; the fourth current-turn result and the prior result both fall outside it. The floor drops the fourth current-turn result and leaves the prior result maskable. One prior plus one current is only two tool results — the slice is empty and nothing masks. Three priors plus one current is already green today (`keep_last` alone masks the oldest prior and keeps the single current result), so it never goes red. With `{}`: red today (the fourth current-turn body masks), red if the floor comparison is inverted (prior kept, that body masks), green only with `idx >= after_index`. (RED today.)
3. `test_jev_failure_keeps_all_in_sweep` — previous-turn maskable results exist (enough big prior-turn results that `select_maskable` returns at least one — otherwise the sweep returns before the JEV call and `caplog` stays empty); patch `classify_mask_candidates → None`; sweep; assert nothing masked AND `caplog` caught the warning.
4. `test_panic_prune_still_masks_all_on_none` — regression guard pinning the asymmetry as intentional: call `panic_prune` (it applies `None` itself and does not go through the sweep); over-ceiling path with `None` verdicts still masks. Stays green across this change.
5. `test_stub_carries_recovery_hint` — mask through `mask_old_observations` or `apply_mask_verdicts`, where `None` still mask-alls. Do not use the sweep: fail-keep on `None` writes no stub. Eligible body is outside `keep_last` and at least 500 characters. After masking, content `startswith("[masked —")` **and** contains `— re-read to restore]`. (RED today: the stub has no hint.)
6. `test_no_user_message_sweep_is_noop` — conversation with at least four big tool results and no user message; patch `classify_mask_candidates` to `return_value={}`; sweep; assert nothing masked. Today `{}` masks the result outside `keep_last=3` → RED. After the `floor is None` early return, nothing is masked. Fail-keep alone does not turn this green, because `{}` is not `None`. (RED today.)

**Spec23 fixture note:** `test_mask_batch_resets_so_later_calls_do_not_rewrite` must seed one prior-turn big read before `_run_loop` (`conv.add_user_message("old turn")` + assistant ToolCall + `add_tool_result` with a 600-char body). The 4th in-loop result fills `keep_last`, so that seed is the candidate and one prior result is enough here. Change the `classify_mask_candidates` patch from `return_value=None` to `return_value={}` — `None` is fail-keep and masks nothing, including the seed; a dict with a missing key still masks. Assert `len(tools) == 8`, `tools[0]` (the seed) `startswith("[masked —")`, and the seven in-loop bodies unmasked. `_obs_since_mask == 3` stays.

Also verify existing `test_mask_keeps_last_three_and_message_count` still passes (it calls the strategy directly; default-arg change 6→3 may require its fixture to match HEAD behavior — check, adjust only if it was written against the dirty tree).

---

## 7. Edge Cases

| Case | Expected |
|---|---|
| No user message in conv | sweep returns early; nothing masked |
| `after_index` negative | every real index matches `idx >= after_index`, so the sweep masks nothing — no caller passes a negative floor, no special case |
| JEV `None` verdicts | keep all + warning log |
| JEV partial dict | missing/invalid keys = mask (unchanged) |
| `summarize` verdict | mask line with hint + first 200 chars |
| floor + keep_last overlap | `keep_last` slices the global newest-first list first (current-turn results fill those slots); the floor then drops any current-turn index still in the slice. A previous-turn result is masked only when it sits outside that newest-3 window |
| `[unchanged —` notices | <500 chars → never candidates; SPEC-32 walk skips them (unchanged) |
| exec outputs | previous-turn exec results maskable with hint; current-turn immune; retention parity deferred |
| panic path | unchanged — None = mask-all, by design |

---

## 8. ARCHITECTURE.md Updates Required

Context-management section: periodic sweep now (a) turn-immune via last-user floor, (b) fail-keep on JEV batch failure with warning, (c) stubs carry `— re-read to restore]` hint. Panic-prune semantics unchanged. Note the intentional asymmetry between sweep (fail-keep) and panic (mask-all).

---

## Self-Audit (Rule 9)

- Signatures verified against source this session (`select_maskable`, `mask_old_observations`, `apply_mask_verdicts`, `_maybe_mask_observations`, `_last_user_index`, `classify_mask_candidates`). ✅
- Code samples: floor check is `idx >= after_index` (skip the current turn), matching `panic_prune`'s `i >= last`. An earlier draft had `<=`, which masked the current turn. `keep_last` slices newest-first before the floor. The tier0 full-line `startswith` is updated with the stub suffix; other mask asserts are prefix-only. SPEC-32 walk matches `startswith("[masked —")`. Sweep tests that expect a mask pin `classify_mask_candidates` to `{}`; the hint test masks at the strategy layer, where `None` still mask-alls. ✅
- Exceptions enumerated: `classify_mask_candidates` never raises; strategy functions are pure walks with defensive guards; `logger.warning` cannot raise. ✅
- No invented parameters: `after_index` is new and defined here; all others exist. ✅
