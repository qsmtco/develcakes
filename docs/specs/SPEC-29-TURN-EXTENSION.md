# SPEC-29: One Jev grant when the tool cap is hit

**Date:** 2026-10-13
**Author:** Micah
**Status:** Draft — for implementation
**Implements:** none (no proposal). Decided in conversation: ask Jev whether the agent is still progressing, and grant one bounded extension.
**Depends on:** SPEC-22 (`post_decision` fail-open contract). The tool loop and `_terminate_turn` from SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION.
**Target branch:** main

> Architecture compliance: the decision stays on the tool-loop thread inside `AgentRuntime._run_loop`. Transport stays `agent/llm/jev_client.post_decision`. No new provider, no model slug, no UI, no config knob. `docs/ARCHITECTURE.md` §3.21m owns this paragraph.

---

DISCOVERY:
- Read `agent/runtime.py` `_run_loop`: `iteration` starts at 0, `max_iter = self._config.max_tool_iterations` (default 50, `AgentConfig.max_tool_iterations: int`). The `while iteration < max_iter` body increments `iteration` at the top, after the cancel checks. When the condition fails, `iteration == max_iter`. The tail then `add_assistant_message("[max tool iterations reached]", [])` and `_terminate_turn(FAILED)` with `metadata={"reason": "max_iterations", "iterations": max_iter}`.
- Read `agent/runtime.py` `_check_and_stop_on_limit(session_key, conv) -> tuple[str, str] | None`: pure. `"cost_limit"` if `conv.total_cost > cost_limit`, `"step_limit"` if `conv.step_count > step_limit`. Caller returns. An extension must not skip this check.
- Read `agent/runtime.py` `_inactive_turn_reason(session_key, turn_token) -> str | None`: `"superseded"`, `"cancelled"`, or `None`. Never raises.
- Read `agent/runtime.py` tool recording: `tc.mark_completed(...)` / `tc.mark_failed(...)` then `tc.result = None` then `conv.add_tool_result(call_id, text)`. The durable body is the `TOOL_RESULT` message. `ToolCall.status` remains `completed` or `failed`. `ToolCall.result` is `None` by the time the cap is hit.
- Read `models/conversation.py`: `Message.role` is `MessageRole` (`USER`, `ASSISTANT`, `TOOL_RESULT`). `Message.tool_calls: list[ToolCall]` on assistant messages. `Message.tool_call_id: str | None` and `Message.content: str` on tool results. `ToolCall.tool_name: str`, `ToolCall.arguments: dict`, `ToolCall.call_id: str`, `ToolCall.status: ToolCallStatus` (str enum: `pending`, `executing`, `completed`, `failed`). `add_assistant_message` increments `conv.step_count`. `add_user_message(content) -> Message` does not.
- Read `agent/llm/jev_client.py` `post_decision(state: str, questions: dict) -> tuple[dict | None, str]`: never raises. `(None, error)` on no card, empty `default_model`, missing key or URL, HTTP error, timeout (2.0 s), or a body without an `answers` dict. Success is `(answers, "")`. `classify_unknown` trusts a `choice` answer only when `confidence` is an int or float `>= _DECISION_MIN_CONFIDENCE` (0.6). A `noul` answer is rejected there. `bool` is an `int` subclass; this spec rejects it explicitly.
- Read `tests/test_agent_runtime.py` `test_max_iterations_enforced`: asserts `_call_llm` count `<= max_tool_iterations` (2). `test_run_loop_terminates_with_failed_on_max_iterations`: asserts `FAILED` and `metadata["reason"] == "max_iterations"`. Both call the real `post_decision` today if this gate is added unpatched.
- Architecture owner: `AgentRuntime._run_loop` owns the cap. `jev_client.post_decision` owns the HTTP call.
- Existing pattern to copy: `classify_unknown` — choice question, confidence floor, fail closed to the previous behavior. Here the previous behavior is the hard stop, not a heuristic retry.

---

## 1. Overview

The tool loop stops when `iteration` reaches `max_tool_iterations` and records a failed turn. A long task and a stuck loop look the same at that line.

On that hit, once, ask Jev a `choice` question over a short record of the goal and the recent tools. Grant 10 more iterations only when the answer is `still_progressing` with confidence at least 0.6. Anything else, including Jev being down, keeps the stop that exists today. Do not ask again at the new cap. Do not ask the coding model.

### Scope

| In | Out |
|---|---|
| One grant of 10 iterations inside `_run_loop` | A second grant, a user setting, a new config field |
| `choice` question via `post_decision` | A `noul` question. `noul` has no confidence field; SPEC-25 already refused it for that reason |
| Short state: goal, tool name, status, repeated flag, 120-char result | Tool arguments, file bodies, the transcript |
| Patch the two tests that exhaust the cap | Changing `cost_limit` / `step_limit` |

### Principles

The cap remains the backstop. Jev is a gate in front of one extension, not a replacement for the cap. Outbound text is the same class of payload SPEC-22 already sends (sanitized provider errors). This payload is smaller and is listed in §8.

## 2. Changes by file

### `agent/runtime.py`

Module constants, next to `CANCEL_MESSAGE`:

```python
_TURN_EXTENSION_GRANT = 10
_TURN_EXTENSION_MAX = 1
_TURN_EXTENSION_STATE_CAP = 4000
_TURN_EXTENSION_GOAL_CAP = 500
_TURN_EXTENSION_RESULT_CAP = 120
_TURN_EXTENSION_TOOL_WINDOW = 8
```

`_DECISION_MIN_CONFIDENCE` stays in `agent/llm/jev_client.py`. Read it from there. Do not copy `0.6` into `runtime.py`.

Initialize next to the existing `max_iter = self._config.max_tool_iterations`:

```python
extensions_used = 0
```

`extensions_used` is a local in `_run_loop`. It is not an instance field. One turn, one count.

At the bottom of the `while iteration < max_iter` body, after the `_check_and_stop_on_limit` block (that block still `return`s on a hit), and before the `while` ends:

```python
if (
    iteration >= max_iter
    and extensions_used < _TURN_EXTENSION_MAX
    and self._inactive_turn_reason(session_key, turn_token) is None
    and self._grant_turn_extension(session_key, conv, iteration)
):
    extensions_used += 1
    max_iter = iteration + _TURN_EXTENSION_GRANT
    logger.info(
        "[tool-loop] sk=%s iteration cap %d extended by %d",
        session_key, iteration, _TURN_EXTENSION_GRANT,
    )
```

`_grant_turn_extension` returns `bool`. `True` is the only grant. The `and` chain does not call it when the turn is inactive or the grant was already used.

The tail outside the `while` stays. Same assistant message `"[max tool iterations reached]"`. Same `TurnStatus.FAILED`. Same `error="Max tool iterations reached"`. Metadata becomes:

```python
metadata={
    "reason": "max_iterations",
    "iterations": iteration,
    "extensions": extensions_used,
}
```

`iteration` at that tail equals the cap that just failed (`max_iter` after any grant). `reason` stays the string `"max_iterations"`. `extensions` is `0` when Jev did not grant, `1` when it did and the new cap was then hit.

Early `return`s inside the body (cancel, text-only `COMPLETED`, cost limit, step limit, tool-loop exception) are unchanged and sit above this check. A finished turn does not ask Jev.

#### `_turn_extension_state(self, conv, iteration: int) -> str`

Never raises. On any unexpected shape, return a short string `"iteration={iteration}\ngoal=\ntools:\n(none)"` rather than propagating.

Goal: walk `conv.messages` in reverse. The first message with `role == MessageRole.USER` supplies `content`. Truncate to `_TURN_EXTENSION_GOAL_CAP` characters. Missing user message → empty goal. The loop's `add_user_message` has already run by the time the cap can hit, so the goal is normally present.

Tools: walk `conv.messages` in order. For each assistant message, for each `ToolCall` in `msg.tool_calls`, record `(tool_name, arguments, call_id, status)`. Build a `call_id → content` map from messages with `role == MessageRole.TOOL_RESULT` and a non-empty `tool_call_id`. Later message wins if a call id is repeated. Do not read `ToolCall.result`. It has been set back to `None`.

Fingerprint, local only, not sent: `tool_name` plus `json.dumps(arguments, sort_keys=True, default=str)`. `TypeError` or `ValueError` from `dumps` → fingerprint is `tool_name` alone. A call is `repeated=yes` when an earlier call in this same list has the same fingerprint. Otherwise `repeated=no`.

Render the last `_TURN_EXTENSION_TOOL_WINDOW` calls, goal first:

```
iteration={iteration}
goal={goal}
tools:
1. {tool_name} status={status.value} repeated={yes|no} result={snippet}
```

`status` is `ToolCall.status`. It is a `str` enum; use `.value`. Snippet is the tool-message content with newlines replaced by spaces, truncated to `_TURN_EXTENSION_RESULT_CAP`. Missing tool message → empty snippet. No calls → the line `(none)` under `tools:`.

Do not put `arguments` in the string. `write_file` / `edit_file` arguments are the file body.

After rendering, return `text[:_TURN_EXTENSION_STATE_CAP]`. The goal is at the top, so a cut drops tool lines, not the goal.

#### `_grant_turn_extension(self, session_key: str, conv, iteration: int) -> bool`

Never raises. `session_key` is for the debug log only.

```python
from agent.llm.jev_client import _DECISION_MIN_CONFIDENCE, post_decision

state = self._turn_extension_state(conv, iteration)
questions = {
    "progress": {
        "type": "choice",
        "instructions": (
            "A coding agent hit its tool-iteration cap. "
            "Classify the recent tool record. "
            "still_progressing means the latest calls are new work toward the goal. "
            "stuck means the same calls, the same errors, or no forward work. "
            "unclear means the record is too thin to tell."
        ),
        "criteria": {
            "still_progressing": "Recent tool calls are distinct and move the goal forward.",
            "stuck": "Recent calls repeat, error, or do not change the task.",
            "unclear": "Not enough evidence to choose the other two.",
        },
    }
}
answers, error = post_decision(state, questions)
```

`post_decision` never raises. If `answers` is `None`, log `logger.debug` with `error` (the string it returned) and return `False`. Do not log `state`.

Then, inside `try` / `except (KeyError, TypeError, AttributeError)` returning `False`:

- `answer = answers["progress"]`
- `answer.get("type")` must be `"choice"`. A `noul` or `score` answer returns `False`.
- `choice = answer.get("choice")` must be the string `"still_progressing"`. `"stuck"` and `"unclear"` return `False`.
- `confidence = answer.get("confidence", 0.0)`. Reject `bool` (`isinstance(confidence, bool)`). Require `isinstance(confidence, (int, float))` and `confidence >= _DECISION_MIN_CONFIDENCE`. Below the floor returns `False`.

Return `True` only after all of those pass. On a refusal that parsed a choice, `logger.debug` the choice and the confidence. Not `logger.error`. Not `logger.exception`.

Return value is consumed by the `if` in the loop. `False` means the `while` condition fails on the next check and the existing tail runs.

Imports used by the new methods: `json` (already imported in `runtime.py`), `MessageRole` from `models.conversation` inside the method (the file already lazy-imports that enum in other methods). `post_decision` and `_DECISION_MIN_CONFIDENCE` imported inside `_grant_turn_extension`, same lazy style as `classify_unknown`. No GTK. No `GLib`. The call blocks the tool-loop thread for at most `post_decision`'s 2.0 s timeout.

Line-count estimate: about 120 lines added to `runtime.py`.

### `tests/test_agent_runtime.py`

Two existing tests exhaust the cap and will call the real Decision Model card if this machine has one.

- `test_max_iterations_enforced` asserts the LLM call count is `<= 2`. A grant makes that 12. Patch `agent.llm.jev_client.post_decision` to `return (None, "decision model unavailable")` for the `_run_loop` call. The assertion stays `<= 2`.
- `test_run_loop_terminates_with_failed_on_max_iterations` still expects `FAILED` / `"max_iterations"`. Patch the same way so the test does not open a socket and does not depend on a live card. Assertion unchanged.

No other assertion in those tests changes.

### `tests/test_turn_extension.py` — new

Use `AgentRuntime` and `_make_cfg` the way `test_run_loop_terminates_with_failed_on_max_iterations` does (`GLib=None`, `auto_save_conversations=False`, `rt.start()`, `create_conversation`, patch `_call_llm` and `agent.tools.execute_tool`). Set `cfg.max_tool_iterations = 2` unless a test says otherwise. Patch `post_decision`; do not hit the network.

1. `still_progressing` at confidence `0.9` → `_call_llm` called `2 + 10` times, then `FAILED`, `metadata["reason"] == "max_iterations"`, `metadata["extensions"] == 1`, `metadata["iterations"] == 12`. `post_decision` called once.
2. Same choice at confidence `0.59` → `_call_llm` called 2 times, `extensions == 0`, `post_decision` called once.
3. `stuck` at confidence `1.0` → 2 calls, `extensions == 0`.
4. `post_decision` returns `(None, "decision model unavailable")` → 2 calls, no exception, `extensions == 0`.
5. Answer type `noul` (`{"progress": {"type": "noul", "noul": 0.99}}`) → 2 calls, `extensions == 0`.
6. `post_decision` call count is 1 even though the extended loop hits the new cap. The second cap does not ask again.
7. State: a tool result longer than 120 characters, and a `write_file` argument containing `"SECRET_BODY"`. The `state` string passed to `post_decision` is at most 4000 characters, contains the user goal, contains the tool name, does not contain `SECRET_BODY`, and the result snippet in `state` is at most 120 characters.
8. Two identical `list_files` calls then a different `read_file`. The rendered tool lines mark the second `list_files` as `repeated=yes` and `read_file` as `repeated=no`.

`execute_tool` returns a successful `ToolResult` so the loop records a real tool message. `_call_llm` always returns one tool call, so the loop cannot finish on its own.

### `docs/ARCHITECTURE.md`

In §3.21m, after the sentence that `post_decision(state, questions)` returns `(answers, error)`, add:

> When `_run_loop` hits `max_tool_iterations`, it calls `post_decision` once with a short goal-and-tools state. A `choice` of `still_progressing` at confidence `>= 0.6` adds 10 iterations. Any other answer, or `(None, error)`, keeps the failed turn. The grant happens once.

Do not claim `repo_map.py` reranks with `post_decision`. The current §3.21m sentence that says the repo map reranks with `post_decision` is already false (`agent/repo_map.py` does not call it). Leave that sentence. This spec does not fix it.

### Files NOT changed

- `agent/llm/jev_client.py` — `post_decision` already has the contract. No new entry point.
- `agent/config.py` — `max_tool_iterations` stays the base cap. No new field.
- `models/conversation.py` — read-only. `ToolCall.result` stays cleared.
- `ui/` — the failed turn still arrives through the existing `on_error` path. No new card for the grant.
- `agent/repo_map.py` — not this gate.

## 3. Data flow

1. `_run_loop` adds the user message, then runs tools until `iteration` reaches `max_iter`.
2. Cancel, a text answer, a cost limit, or a step limit returns before the gate. Jev is not called.
3. On the iteration where `iteration >= max_iter`, if `extensions_used` is 0 and `_inactive_turn_reason` is `None`, `_grant_turn_extension` builds the state and calls `post_decision(state, questions)`.
4. `post_decision` reads the `DecisionModel` card and POSTs `{base_url}/v1/systemone`, or returns `(None, error)` without raising.
5. `True` → `extensions_used = 1`, `max_iter = iteration + 10`, log info, loop continues. The cost and step checks still run on every later iteration.
6. `False`, inactive turn, or grant already used → the `while` ends. Tail writes `"[max tool iterations reached]"` and `_terminate_turn(FAILED)` with `extensions` set.
7. A second approach to the cap does not call `post_decision`. The tail runs.

Key structures: turn state stays `(session_key, turn_token)`. The new metadata keys are `iterations` (the loop counter) and `extensions` (`0` or `1`). `reason` stays `"max_iterations"`.

## 4. File change summary

| File | Change | Lines | Risk |
|---|---|---|---|
| `agent/runtime.py` | Gate at the bottom of the tool `while`; two methods; constants | ~120 | Medium — the cap is the stop tests rely on |
| `tests/test_turn_extension.py` | New | ~250 | Low |
| `tests/test_agent_runtime.py` | Patch `post_decision` in two cap tests | ~10 | Low |
| `docs/ARCHITECTURE.md` | One paragraph in §3.21m | ~4 | Low |

## 5. Implementation order

1. Add the constants and the two methods. Unit-test `_turn_extension_state` and `_grant_turn_extension` through the loop tests in §2. Do not call a live card.
2. Insert the `if` at the bottom of the `while`, and add `extensions` to the tail metadata. Confirm `reason` is still `"max_iterations"`.
3. Patch the two existing cap tests.
4. Run `tests/test_turn_extension.py` and `tests/test_agent_runtime.py::TestToolLoop::test_max_iterations_enforced` plus `test_run_loop_terminates_with_failed_on_max_iterations`.
5. Update §3.21m.

## 6. Acceptance criteria

- [ ] A confident `still_progressing` answer adds exactly 10 iterations, once.
- [ ] Low confidence, `stuck`, `unclear`, a `noul` answer, and `(None, error)` leave the call count at `max_tool_iterations` and the turn `FAILED` with `extensions == 0`.
- [ ] `post_decision` is called at most once per turn.
- [ ] The state string is at most 4000 characters, omits tool arguments, and uses the tool-message body (not `ToolCall.result`).
- [ ] `_grant_turn_extension` and `_turn_extension_state` do not raise.
- [ ] `cost_limit` and `step_limit` still stop the turn inside an extension. The gate sits below `_check_and_stop_on_limit`.
- [ ] An inactive turn (`_inactive_turn_reason` not `None`) does not call `post_decision`.
- [ ] `test_max_iterations_enforced` still asserts `<= 2` LLM calls with `post_decision` patched to `(None, ...)`.
- [ ] No new `AgentConfig` field.

## 7. Edge cases

| Case | Expected |
|---|---|
| No Decision Model card, empty model, timeout, HTTP error | `post_decision` returns `(None, error)`. No grant. Turn fails as today. |
| `confidence` is `true` (JSON boolean) | Reject. `bool` is an `int` subclass. |
| `confidence` missing | Treat as `0.0`. No grant. |
| Choice `still_progressing` at exactly `0.6` | Grant. The floor is `>=`. |
| Cap is 0 | The `while` body never runs. Jev is not asked. Tail still fails. |
| User message missing | Goal is empty. Jev still gets a state string. A non-grant answer stops. |
| `arguments` not JSON-serializable | Fingerprint falls back to `tool_name`. State still omits arguments. |
| Same tool name, different arguments | `repeated=no`. |
| Same tool name and same arguments | Later call is `repeated=yes`. |
| Cancel or a new turn token during the last iteration | `_inactive_turn_reason` is not `None`. No Jev call. Existing tail still runs (`FAILED`, and `_terminate_turn` rejects a stale token). |
| Cost or step limit on an extended iteration | Existing `_check_and_stop_on_limit` return. No second Jev call. |
| `post_decision` blocks | At most its own 2.0 s timeout, on the tool-loop thread. |

## 8. ARCHITECTURE.md updates required

§3.21m only, the sentence specified in §2. No other section. The repo-map sentence in that same paragraph is stale and is out of scope.
