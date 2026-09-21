# Phase 5 — Add 22 new tests for the state machine + terminal paths + alias removal

**Spec:** `docs/specs/SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION.md` §2.6
**Scope:** `tests/test_agent_runtime.py` only. Add new test classes/functions. Do NOT modify existing tests.

## Goal

Add 22 new tests covering: TurnStatus/TurnResult dataclasses (3), _terminate_turn behavior (8), _run_loop state transitions (4), audit-driven terminal paths (5), provider alias removal (2), callback protocol exports (1), send_message token rotation (1).

## Required reading first

Read these IN FULL:
- `tests/test_agent_runtime.py` — especially the `_uniq()`, `_resp()`, `_make_cfg()` helpers at the top (lines 1-210), and the existing `TestToolLoop` class for patterns.
- `agent/runtime.py` — the `_terminate_turn` method, `TurnStatus`/`TurnResult` definitions, `get_turn_state`/`get_last_turn_result` accessors.
- `docs/specs/SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION.md` §2.6 — the exact test code.

## Where to add the tests

Add a NEW test class at the END of `tests/test_agent_runtime.py` (after the last existing class). Name it `TestTurnStateMachine`. All 22 tests go in this class (or a few related classes if you prefer — but keep them at the end of the file).

## Test implementations

Use the spec §2.6 test code as the authoritative source. Key adaptations:

1. **Use the existing `_make_cfg()`, `_uniq()`, `_resp()` helpers** — they're already defined at the top of the test file. Don't redefine them.

2. **For tests that need a real conversation:** call `rt.create_conversation("Coder", sk, "/tmp")` before `rt._run_loop(sk, text)`. Use `_uniq()` for the session key.

3. **For tests that patch `_call_llm`:** use `lambda sk, msgs, tools, **kwargs: _resp(...)` (note the `**kwargs` — required because `_call_llm` receives `turn_token` as a kwarg).

4. **For tests that need `_terminate_turn` called directly:** set up `_turn_tokens[sk]` and `_turn_state[(sk, tk)]` first (or call `_run_loop` which does it automatically).

### The 22 tests (from spec §2.6, adapted)

**Group 1 — TurnStatus and TurnResult (3 tests):**
- `test_turn_status_enum_values` — 5 values, 2 non-terminal, 3 terminal
- `test_turn_result_required_fields` — status/session_key/turn_token required; text/error/metadata have defaults
- `test_turn_result_metadata_isolation` — two instances don't share metadata dict

**Group 2 — _terminate_turn behavior (8 tests):**
- `test_terminate_turn_dispatches_on_response_complete_for_completed`
- `test_terminate_turn_dispatches_on_error_for_failed`
- `test_terminate_turn_dispatches_on_error_for_cancelled`
- `test_terminate_turn_rejects_non_terminal_status`
- `test_terminate_turn_dedups_duplicate_terminal_transitions`
- `test_terminate_turn_rejects_stale_token`
- `test_terminate_turn_persistence_uses_separate_session_keys` — uses 3 separate session keys (one per status)
- `test_terminate_turn_cancelled_with_persist_metadata_saves`

**Group 3 — _run_loop state transitions (4 tests):**
- `test_run_loop_starts_in_running_state`
- `test_run_loop_transitions_to_streaming_before_first_llm_call`
- `test_run_loop_terminates_with_failed_on_max_iterations`
- `test_run_loop_terminates_with_cancelled_on_cancel_signal`

**Group 3a — BUG #2 terminal paths (2 tests):**
- `test_run_loop_terminates_with_failed_on_no_conversation`
- `test_run_loop_terminates_with_failed_on_prompt_build_failure`

**Group 3b — BUG #5 mid-stream error (1 test):**
- `test_run_loop_terminates_with_failed_on_stream_error_with_content`

**Group 3c — BUG #6 limit handling (2 tests):**
- `test_run_loop_terminates_with_failed_on_cost_limit`
- `test_run_loop_terminates_with_failed_on_step_limit`

**Group 4 — Provider alias removal (2 tests):**
- `test_runtime_no_longer_exposes_call_provider_aliases`
- `test_runtime_no_longer_exposes_stream_provider_aliases`

**Group 5 — Callback protocol exports (1 test):**
- `test_callbacks_module_exports_protocols`

**Group 6 — send_message token rotation (1 test):**
- `test_send_message_rotates_turn_token` — this tests that two consecutive `_run_loop` calls produce different `_turn_tokens[sk]`. NOTE: `_run_loop` rotates the token at the RUNNING init (it sets `_turn_tokens[sk] = turn_token`). Since `send_message` passes `self._turn_token` to `_run_loop`, and the handler rotates `self._turn_token` before each `send_message`, the rotation happens in the handler. For this test, manually set different turn_tokens: call `rt._run_loop(sk, "first")` with one token, then `rt._run_loop(sk, "second")` with a different token, and verify `_turn_tokens[sk]` changed.

Use the spec §2.6 code verbatim where possible. Adapt only where the spec's code references helpers differently from the test file's existing helpers.

## CRITICAL test-writing rules

1. **Every test must use `_uniq()` for session keys** — never hardcode `"test:sk"`.
2. **Every test that calls `_run_loop` must call `create_conversation` first** (unless testing the missing-conversation path).
3. **Every `_call_llm` mock must accept `**kwargs`** (for the `turn_token` kwarg).
4. **Every test must `rt.start()` before `_run_loop` and `rt.stop()` after.**
5. **Tests must be able to FAIL** — if the feature were broken, the test must catch it. Don't write tests that pass regardless.

## Verification commands

```bash
# 1. All 22 new tests pass
XDG_CONFIG_HOME=/tmp/cctest_home/.config timeout 120 python3 -m pytest tests/test_agent_runtime.py -q --no-header --timeout=15 -k "turn_status or turn_result or terminate_turn or run_loop_starts or run_loop_transitions or run_loop_terminates or test_runtime_no_longer_exposes or test_callbacks_module_exports or send_message_rotates or rejects_stale_token or persistence_uses_separate or cancelled_with_persist_metadata" 2>&1 | tail -5
# Expected: 22 passed

# 2. No existing tests broken
XDG_CONFIG_HOME=/tmp/cctest_home/.config timeout 120 python3 -m pytest tests/test_agent_runtime.py -q --no-header --timeout=15 --deselect "tests/test_agent_runtime.py::TestApproval::test_exec_with_approval_allow" --deselect "tests/test_agent_runtime.py::TestApproval::test_exec_with_approval_deny" 2>&1 | tail -5
# Expected: 4 failed (pre-existing), 166+22=188 passed
```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] Group 1: 3 tests (TurnStatus/TurnResult) — evidence: pytest -k output
- [x/not done] Group 2: 8 tests (_terminate_turn behavior) — evidence: pytest -k output
- [x/not done] Group 3: 4 tests (_run_loop state transitions) — evidence: pytest -k output
- [x/not done] Group 3a: 2 tests (BUG #2 terminal paths) — evidence: pytest -k output
- [x/not done] Group 3b: 1 test (BUG #5 mid-stream error) — evidence: pytest -k output
- [x/not done] Group 3c: 2 tests (BUG #6 limit handling) — evidence: pytest -k output
- [x/not done] Group 4: 2 tests (alias removal) — evidence: pytest -k output
- [x/not done] Group 5: 1 test (callback exports) — evidence: pytest -k output
- [x/not done] Group 6: 1 test (token rotation) — evidence: pytest -k output
- [x/not done] All 22 new tests pass — evidence: step 1 output
- [x/not done] No existing tests broken — evidence: step 2 output
```
