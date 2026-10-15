# SPEC-34: Delete the observation-mask sweep

**Date:** 2026-10-14
**Revised:** 2026-10-15 — discovery and acceptance rules corrected against HEAD `f227333e`. The PM decision is unchanged.
**Author:** Supervisor
**Status:** Implemented
**Decision:** PM, 2026-10-14 — "delete the periodic sweep, not tune it"
**Supersedes:** SPEC-30 mask half; the sweep-only parts of SPEC-33 (turn floor on the sweep, sweep fail-keep, sweep stub hint)
**Depends on:** SPEC-32 (read dedup), SPEC-33 (turn immunity, already in `panic_prune`), SPEC-33b items 3 and 5 (landed)
**Baseline:** HEAD `f227333e` — SPEC-33b item 5, pin on first read
**Work unit:** `mask-sweep-removal`

The 2026-10-14 draft said `panic_prune` does not call the mask methods, that `[compacted — …]` preserves the body, that `apply_mask_verdicts` can be deleted while `panic_prune` stays byte-identical, that `test_dedup_stub_recovery.py` needs no edits, and that pins still arm on the third read. None of that is true at this baseline. Those rules contradicted each other. This revision replaces them.

---

## DISCOVERY (verified against source, 2026-10-15)

Every claim below was read at the stated line.

- **The sweep has one production caller.** `agent/runtime.py:1860-1863` — inside the tool loop, when `_obs_since_mask >= 4`, reset the counter and call `_maybe_mask_observations`. `_count_tool_observation` (`runtime.py:3820`) is the only writer. Call sites: `runtime.py:2349`, `:2372`, `:2455`. The counter is created at `:731`, cleared at `:1810` and `:3966`.
- **`_maybe_mask_observations` (`runtime.py:3766`)** runs only when `landing_goal < estimate <= soft_ceiling`. It passes the last user-message index as `after_index`, so the current turn is already immune (SPEC-33). It calls `select_maskable`, then `classify_mask_candidates`, and on `None` it keeps every candidate. A partial answer still masks any candidate the dict omits.
- **`panic_prune` calls `apply_mask_verdicts`.** `context_strategy.py:625` is `return self.apply_mask_verdicts(conv, indices, None)`. `None` masks every index. The stub is `[masked — {tool} {path} offset=… — re-read|re-run to restore]`, built at `context_strategy.py:582-586`. It is not an independent walker and it does not keep the body.
- **`prune_tool_outputs` also drops the body.** Its stub is `[compacted — {tool_name} output, {N} chars removed]` (`context_strategy.py` inside `prune_tool_outputs`). "Lossless" there means the message slot survives. SPEC-32 exists because both prefixes mean the bytes are gone. The recovery walk treats `[masked —` and `[compacted —` as dead (`runtime.py` `_body_live_in_transcript`).
- **What the sweep still destroys.** SPEC-33 already stops it from masking the current turn. While the estimate sits between the landing goal (`min(15% of the window, 24_000)`) and the soft ceiling (80%), every four new tool results can still replace previous-turn bodies. On a 128k window that band is about 19,200 to 102,400 tokens. `exec_command` has an empty path, so a pin never matches it. SPEC-33b's `<exec:session>` key is stored and then dropped by `_protect_paths` on `OSError` (`runtime.py:3694`). Shell output stays eligible.
- **Pins are already first-read.** `runtime.py:3691` is `if counts[path] >= 1`. `tests/test_context_strategy_tier0.py::test_first_read_pins_a_file_and_a_change_drops_it` already encodes that. Do not change the threshold again. Do not rename that test.
- **`_compact_before_llm` (`runtime.py:3794`)** is the other mutator. Over the soft ceiling it calls `panic_prune` then `compact` toward the landing goal, with `preserve_current_turn=True` and the pin set. Under the ceiling, `compact(conv, soft_ceiling)` returns from `prune_tool_outputs` without stubbing. `tests/test_panic_prune.py::test_over_threshold_lands_on_the_goal` monkeypatches both methods, so it does not prove any body survived.

---

## 1. Overview

### Problem

The periodic sweep deletes previous-turn tool results while the conversation is still under the provider ceiling. The current turn is already safe. The damage that remains is the next turn: after four new tool results, older file reads and shell output become `[masked — … re-read|re-run to restore]`. A file read once is now pinned (SPEC-33b). A shell result is not. Re-running the command does not restore the output that was deleted.

Compaction and panic-prune already run on every LLM call. They are the mechanism that should react to real token pressure. They do not preserve tool bodies. They replace them with a one-line stub. That is acceptable at the ceiling. It is not acceptable every four observations on the way there.

### Solution

**Delete the sweep.** Remove the every-4-observations call, its counter, and the selection/verdict helpers that exist only for it.

**Point `panic_prune` at the compacted stub in the same change.** It must stop calling `apply_mask_verdicts`. Same selection as today: tool results before the last user message, skip a protected path, skip a body that already starts with `[masked —` or `[compacted —`, no user message means stub nothing. The new text is the `prune_tool_outputs` line, `[compacted — {tool_name} output, {N} chars removed]`. No recovery hint. No JEV. `compact()` itself stays byte-identical.

Pins stay as SPEC-33b left them.

### Scope

| In | Out |
|---|---|
| Delete the sweep call, `_maybe_mask_observations`, `_mask_candidate_state`, `_count_tool_observation`, every `_obs_since_mask` use | `compact()` body |
| Delete `select_maskable`, `mask_old_observations`, `apply_mask_verdicts` after `panic_prune` no longer calls them | SPEC-32 walk, including its `[masked —` prefix check |
| `panic_prune` writes the compacted stub; turn floor and `protect_paths` stay | Pin threshold (already 1) |
| Tests that planted masks through the deleted methods plant the stub string instead | Item 6 / a new JEV call site |
| One real over-ceiling test that checks bodies | `agent/llm/jev_client.py` |

### Why the stub change is part of the deletion

Leaving `apply_mask_verdicts` in place only so `panic_prune` can pass `None` keeps a second stub dialect and the "re-run to restore" line on the path that fires when context is actually full. The verdict dict (`keep` / `mask` / `summarize`) has no production caller once the sweep is gone. One stub, the one `prune_tool_outputs` already writes.

Saved transcripts may still contain `[masked —`. The recovery walk keeps recognizing that prefix. Tests plant it as a literal. Nothing in `agent/context_strategy.py` writes it after this change.

---

## 2. Changes by File

### 2.1 `agent/runtime.py` — remove the sweep

**2.1a — the call site.** Delete the block at `runtime.py:1858-1863` (the comment plus the `_obs_since_mask` check and the `_maybe_mask_observations` call). Do not replace it with a no-op. The pluggable-strategy comment below it stays.

**2.1b — the methods.** Delete `_maybe_mask_observations` (`3766`), `_mask_candidate_state` (`3743`), and `_count_tool_observation` (`3820`).

**2.1c — the counter's other sites.** Delete the three `_count_tool_observation` calls (`2349`, `2372`, `2455`), the `__init__` dict (`731`), the pop at the start of a user turn (`1810`), and the pop in `_cleanup_tool_history` (`3966`). Grep `_obs_since_mask` and `_count_tool_observation` after the edit. Zero hits.

**2.1d — the JEV import.** The `classify_mask_candidates` import inside `_maybe_mask_observations` goes away with the method. Do not add a call site. `agent/llm/jev_client.py` is not edited. `classify_mask_candidates` becomes unused. Leave it.

### 2.2 `agent/context_strategy.py` — panic stub, then delete the verdict layer

**2.2a — rewrite `panic_prune` first.** Replace the `return self.apply_mask_verdicts(...)` tail with an in-place stub of the indices it already collected:

```python
stub = f"[compacted \u2014 {tool_name} output, {n_chars} chars removed]"
```

Use `_tool_result_meta` for `tool_name` and the parent, clear the matching `tc.result`, set `tokens_used` to `len(stub) // 4`, and invalidate `conv._token_estimate_cache`. Skip a body that already starts with either stub prefix. Return the number replaced.

Do not copy the recovery hint. Do not consult a verdict dict.

Selection stays: `i >= last` is kept; empty path is not protected; a path in `protect_paths` is kept; `last is None` returns 0.

**2.2b — then delete**, once grep shows no remaining caller:

- `select_maskable`
- `mask_old_observations`
- `apply_mask_verdicts`
- `_RECOVERY_HINT` and `_DEFAULT_RECOVERY_HINT`, if nothing else references them

**Keep** `_tool_result_meta`, `_tool_result_limit`, `_last_user_index`, `_kept_from_heavy`, `prune_tool_outputs`, and `compact`. `MessageRole` stays imported; panic and compact still use it. If the edit orphans the import, remove the import. Do not leave an unused import.

**`compact()` is byte-identical.** `panic_prune` is not. The difference is the stub writer. The set of messages it is willing to stub does not grow.

### 2.3 Pins — no change

SPEC-33b item 5 already stored a pin on the first successful `read_file`. A changed file is dropped and re-pinned at count 1. This spec does not edit `_note_tool_path` or `_note_exec_pin`.

`tests/test_spec33b_read_pins.py::test_first_read_is_protected_from_masking` calls `select_maskable` and `mask_old_observations`. Retarget it at `panic_prune`: one read pins `a.py`; a previous-turn result for `a.py` keeps its body; an unpinned previous-turn result becomes the compacted stub.

### 2.4 Tests

| File | Action | Reason |
|---|---|---|
| `test_spec33_mask_turn_immunity.py` | **Delete** | Every test targets the sweep, the sweep's fail-keep, or the sweep stub. `panic_prune`'s turn floor stays covered by `test_panic_prune.py`. |
| `test_jev_mask_adjudication.py` | **Narrow** | Keep `test_empty_candidates_do_not_post`, `test_garbage_answer_is_omitted`, `test_bad_state_does_not_raise`, and the `classify_mask_candidates` assertions inside `test_six_candidates_are_one_post`, `test_low_confidence_masks_that_candidate_only`, and `test_whole_batch_failure_masks_all`. Delete every `select_maskable` / `apply_mask_verdicts` call and `test_short_result_is_not_a_candidate`, `test_summarize_keeps_prefix_and_first_200_chars`. The client stays; the applicator does not. |
| `test_spec23_context_working_set.py` | **Narrow** | Delete `test_mask_keeps_last_three_and_message_count`, `test_mask_keep_last_larger_than_history_replaces_nothing`, `test_mask_negative_keep_last_masks_all`, `test_mask_missing_parent_uses_unknown_tool`, `test_mask_batch_resets_so_later_calls_do_not_rewrite`, `test_new_turn_does_not_mask_from_a_stale_counter`. Keep the dedup and task-card tests. |
| `test_context_strategy_tier0.py` | **Narrow** | Delete `test_select_maskable_keeps_short_recent_and_protected` and `test_under_landing_goal_does_not_mask`. Leave the pin tests, including `test_first_read_pins_a_file_and_a_change_drops_it`. |
| `test_panic_prune.py` | **Edit the prefix** | `test_panic_stubs_only_before_the_last_user_message` expects `[masked — read_file`. Expect `[compacted — read_file output,`. The current-turn body assertion stays. |
| `test_dedup_stub_recovery.py` | **Edit the planter only** | Eight tests call `apply_mask_verdicts`. Assign a literal `[masked — …]` (or the two-line summarize form in `test_summarize_stub_returns_full_body_not_tail`) on that message. Do not change what the re-read is asserted to return. `test_compacted_reread_returns_full_body` already plants its stub as a string. |
| `test_spec33b_exec_retention.py` | **Edit** | Plant dead bodies as literal `[masked —` strings so the walk tests still cover old transcripts. Delete the assertion that the live writer says `re-run to restore` — nothing writes that hint after §2.2. `test_unmasked_exec_stays_maskable` becomes: `panic_prune` stubs a previous-turn `exec_command` result, because its path is empty. |
| `test_spec33b_read_pins.py` | **Edit one test** | §2.3. The other pin tests stay. |

Add `test_over_ceiling_stubs_previous_turn_and_keeps_the_current_turn` to `test_panic_prune.py`. Do not monkeypatch `panic_prune`, `compact`, or `get_token_estimate`. Build a real conversation whose estimate is over the soft ceiling: a previous user turn with one large unpinned tool result and one large tool result on a path that has been pinned, then a current user turn with one large tool result. Call `_compact_before_llm`. Assert:

- the current-turn tool body is unchanged
- the pinned previous-turn body is unchanged
- the unpinned previous-turn body starts with `[compacted —`
- the current user message is still present

`test_over_threshold_lands_on_the_goal` stays. It only checks which budget was passed. It is not this test.

If this new test cannot hold the window without deleting the current turn or the pin, stop. Fix compaction in this change. Do not put the sweep back in order to make the assertion pass.

### 2.5 `agent/llm/jev_client.py` — unchanged

`classify_mask_candidates` is unused after the sweep goes. Keep the module. Deleting a tested client in a deletion round is how the next round rewrites it.

---

## 3. Data Flow (after)

```
tool loop iteration
  └─ _compact_before_llm (every call)
        ├─ estimate > soft_ceiling
        │    ├─ panic_prune → [compacted — …] on previous-turn tool results
        │    │    except the current turn, a protected path, an already-stubbed body
        │    └─ compact(landing_goal, protect, preserve_current_turn)
        └─ else → compact(soft_ceiling)     # no-op while under the line
read_file
  └─ _dedup_read_output → [unchanged — …] or the full body
  └─ _note_tool_path → pin on the first successful read   # already true
exec_command
  └─ no pin that survives _protect_paths
  └─ _body_live_in_transcript still treats [masked — and [compacted — as dead
no sweep. no _obs_since_mask. context_strategy.py does not write [masked —.
```

---

## 4. File Change Summary

| File | Change | Risk |
|---|---|---|
| `agent/runtime.py` | delete the sweep, its methods, the counter, three increment sites | low |
| `agent/context_strategy.py` | `panic_prune` writes the compacted stub; delete three methods and the hint table | medium — the stub text changes, the selection rules do not |
| `tests/` | delete 1 file, narrow 3, edit planters in 4, add 1 body test | low |
| `docs/ARCHITECTURE.md` | record the removal and the stub change | — |
| `agent/llm/jev_client.py` | none | — |

---

## 5. Implementation Order

1. Grep `_obs_since_mask` and `_count_tool_observation`. The hits listed in Discovery are the full set. A hit outside `agent/runtime.py` is a stop-and-report.
2. Rewrite `panic_prune` (§2.2a). Run `tests/test_panic_prune.py` and expect the prefix assertion to fail, then update it.
3. Delete the sweep (§2.1) and the three strategy methods (§2.2b).
4. Repair the tests in §2.4, including the new over-ceiling body test. Run it before any other new test is treated as optional.
5. Run `tests/test_dedup_stub_recovery.py`, `tests/test_spec33b_exec_retention.py`, `tests/test_spec33b_read_pins.py`, `tests/test_spec23_context_working_set.py`, `tests/test_context_strategy_tier0.py`, `tests/test_jev_mask_adjudication.py`, `tests/test_panic_prune.py`, and `tests/test_context_strategy.py`.
6. ARCHITECTURE.md and the audit resolution stamp.
7. One commit: `SPEC-34: remove observation-mask sweep; panic_prune uses compacted stub`.

Item 6 of SPEC-33b is not part of this commit.

---

## 6. Acceptance Criteria

1. `grep -n "_maybe_mask_observations\|_mask_candidate_state\|_count_tool_observation\|_obs_since_mask\|select_maskable\|apply_mask_verdicts\|mask_old_observations" agent/ ui/ utils/` → no hits.
2. `grep -n "\[masked —" agent/context_strategy.py` → no hits. `agent/runtime.py` may still match the prefix inside the SPEC-32 walk.
3. `grep -n "\[compacted —" agent/context_strategy.py` shows the prune stub and the panic stub. Both drop the body. The report says so in one sentence.
4. `compact()` is byte-identical to `f227333e`. `panic_prune` is not. The diff is the stub writer. A previous-turn unpinned tool result is still stubbed; the current turn, a protected path, and a conversation with no user message are not.
5. `_note_tool_path`'s `>= 1` check and `_note_exec_pin` are unchanged. `test_first_read_pins_a_file_and_a_change_drops_it` is not renamed.
6. Dedup tests still assert that a re-read returns the full body when the live copy is a stub. The planter is a string assignment. `test_summarize_stub_returns_full_body_not_tail` plants its two-line stub the same way.
7. The new over-ceiling test in §2.4 passes without monkeypatching the strategy or the estimator. The current turn and the pinned body are intact.
8. Deleted tests are listed in the report next to the section that removed the behaviour: sweep tests → §2.1; verdict applicator tests → §2.2b.
9. No new `@pytest.mark.skip`. No new `post_decision` or `jev_client` call site under `agent/`. `jev_client.py` is unmodified.
10. `MessageRole` is still used, or the unused import is removed.
11. ARCHITECTURE.md says the sweep is gone, `panic_prune` writes `[compacted — …]`, tool bodies are not preserved, and a first-read pin already protects `read_file` through that prune.
12. The audit doc notes that the sweep is removed, SPEC-33b already closed items 3 and 5, and item 6 stays deferred.

---

## 7. What this does not make next

SPEC-33b item 6, the near-duplicate nudge, stays deferred. Deleting the sweep removes the under-ceiling shredder. A nudge does not, and this unit does not add a JEV call to find out. `classify_mask_candidates` remains in the tree, unused, for a later unit that has a measured reason to call it.

SPEC-33b item 3 stays. The walk is how a later `read_file` recovers a body after panic or after an old `[masked —` transcript. Item 5 stays. This spec does not redo the pin.

---

## 8. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Context grows until the soft ceiling, then panic stubs the previous turn in one shot | Medium | That is the intended trade. §2.4's body test is the proof the current turn and pins survive that shot. |
| An old transcript full of `[masked —` no longer matches a writer | Low | The walk still treats the prefix as dead. Dedup tests plant the literal. |
| `panic_prune`'s new stub surprises a test that matched `[masked — read_file` | Certain | §2.4 names `test_panic_prune.py` and the exec retention file. Update them in the same commit. |
| Someone treats a failing landing-goal number as a reason to restore the sweep | Low | §2.4: fix compaction. Do not restore the sweep. |

---

## 9. Assumption the body test has to discharge

The 2026-10-14 draft did not run an over-ceiling turn, and it described `panic_prune` as content-preserving. The code does the opposite: over the ceiling, `panic_prune` mask-alls every previous-turn tool result through `apply_mask_verdicts(..., None)`. The body test in §2.4 is required before the commit. It checks the text that remains, not only the budget integer passed into `compact`.
