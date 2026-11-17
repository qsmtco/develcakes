# SPEC-64: Runtime Turn-Text Continuity & Streaming Transport Hardening

**Date:** 2026-11-14
**Author:** Supervisor / Diagnostics (per PM direction)
**Status:** Draft — for implementation
**Implements:** `docs/audits/2026-11-14-ASK-COMMAND-STREAMING-INVESTIGATION-READ-ONLY.md` Findings **1A, 1B, 1C, 4A, 4B** (companion spec `docs/specs/SPEC-65-ASK-PARSER-REGISTRY-AND-RELAY-HARDENING.md` covers Findings **2A, 2B, 2C, 3A, 3B, 3C, 3D** with zero file overlap)
**Depends on:** None — zero file overlap with `SPEC-65`; may land before, after, or in parallel worktrees with `SPEC-65`
**Target branch:** main
**Spec authoring prompt:** `.crabcakes/prompts/steelFramedSpecWriter.md` (Rules 1–10 applied; discovery block + self-audit below)

> Architecture compliance: All changes stay strictly inside `agent/` (`agent/runtime.py`, `agent/llm/streaming.py`, `agent/llm/extractors.py`, `agent/llm/openai_provider.py`, `agent/llm/minimax_provider.py`, `agent/llm/anthropic_provider.py`). Zero imports from `ui/` or `gi`/GTK (`docs/ARCHITECTURE.md` §3.2, §3.21). `StreamingCallKwargs` and `_call_llm_streaming`'s 11-parameter signature are **untouched** (`TestStreamingSignature` invariant preserved). `LLMProvider` protocol method signatures (`call`, `stream`) are **untouched**. `TurnResult` remains the single terminal carrier through `_terminate_turn`.

---

## DISCOVERY

Starting Spec Discovery — reading all referenced source files before writing any spec content.

- Read `agent/runtime.py` (`StreamingCallKwargs` `:65-82`, `TurnResult` `:212-218`, `_is_empty_content` `:376-406`, `_Inbox` / `_stranded` `:735-736`, `_terminate_turn` `:859-1054`, `begin_send` / `take_stranded` `:1500-1523`, `_run_loop` `:1955-2672`, `_call_llm_once` `:3501-3710`, `_call_llm_streaming` `:3712-3878`, `_iteration_judge` / `_graceful_iteration_close` `:3928-3988`):
  - Verified signature `AgentRuntime._call_llm_streaming(self, session_key: str, base_url: str, api_key: str, model: str, caller_key: str, messages: list[dict], tools: list[dict] | None, timeout: float, x_title: str = '', turn_token: object | None = None, reasoning_effort: str = 'off') -> dict` — pinned by `tests/test_agent_runtime.py::TestStreamingSignature` (`:2077-2145`). Must NOT add or remove parameters.
  - Verified signature `AgentRuntime._graceful_iteration_close(self, session_key: str, conv, turn_token, iteration: int, tool_errors: int, grants_used: int, hit_hard_cap: bool) -> None` (`:3971-3988`). Currently constructs `TurnResult(status=TurnStatus.COMPLETED, session_key=session_key, turn_token=turn_token, error=None, metadata=...)` omitting `text=`, so `TurnResult.text` defaults to `""`.
  - Verified `_run_loop` (`:2352-2514`): when `not tool_calls_raw`, `_is_empty_content(text_content)` (`:2362`) terminates `TurnStatus.FAILED` (`reason="empty_content"`) even when earlier tool-calling iterations in the **same** turn streamed substantial prose + `/ask` commands; and on normal completion (`:2505-2513`), `TurnResult.text` receives **only** the final iteration's `text_content`, discarding prose emitted during earlier tool-calling iterations.
- Read `ui/agent_runtime/turn_ui.py` (`_on_text_delta` `:15-89`, `_do_response_complete` `:415-610`, `_do_error` `:612-794`):
  - `_on_text_delta` (`:64`) accumulates `self._streaming_text[session_key]` across **all** iterations of the turn.
  - `_do_response_complete` (`:485-486`) runs `if was_streaming and text: self._crh.set_streaming_text(session_key, text)`, then (`:531-533`) `if self._on_agent_response is not None and text: self._on_agent_response(session_key, text, project_name)`. Consequently: (a) when `TurnResult.text == ""` (Finding 1A), `set_streaming_text` is skipped so `end_streaming` renders all streamed prose in chat while `_on_agent_response` is **skipped**; (b) when `TurnResult.text` has only the final iteration's text (Finding 1C), Iteration 1..N-1's prose is wiped from the bubble and hidden from `_on_agent_response`.
- Read `agent/llm/convert.py` (`convert_messages_for_anthropic` `:16-65`) and `models/conversation.py` (`serialize_messages` `:144-247`):
  - **Critical wire invariant verified:** `convert_messages_for_anthropic` (`:54-62`) converts every `role == "tool"` row into `{"role": "user", "content": [{"type": "tool_result", ...}]}` without merging adjacent `"user"` rows. Therefore, every completed turn in `conv.messages` **must** end with a `MessageRole.ASSISTANT` message containing non-empty `content` — otherwise a turn ending on `MessageRole.TOOL_RESULT` followed by the next turn's `MessageRole.USER` produces two consecutive `role: "user"` messages on Anthropic (HTTP 400) or an empty assistant message on strict providers (`tests/test_agent_runtime.py:559-569`).
- Read `agent/llm/streaming.py` (`parse_sse_line` `:60-81`, `parse_sse_delta` `:84-114`, `first_choice` `:117-128`):
  - `parse_sse_delta` (`:100-102`) checks `content = delta.get("content"); if content is not None: events.append(SSEEvent(type="text_delta", data={"content": content}))`. Structured `list[dict]` deltas pass `is not None` uncoerced and crash `_call_llm_streaming` (`"".join(parts)` at `agent/runtime.py:3831`) and `turn_ui.py:64` with `TypeError`.
- Read `agent/llm/extractors.py` (`extract_text_content` `:111-128`):
  - OpenAI branch (`:113-118`) returns `msg.get("content", "") or ""`, returning a raw `list` instead of `str` when `message.content` is a structured content-block array.
- Read `agent/llm/openai_provider.py` (`call` `:56-99`, `stream` `:101-205`), `agent/llm/minimax_provider.py` (`_handle_finish_frame` `:24-73`, `call` `:91-138`, `stream` `:140-248`), `agent/llm/anthropic_provider.py` (`call` `:53-105`, `stream` `:107-198`), `agent/llm/protocol.py` (`LLMProvider` `:23-56`):
  - `AnthropicProvider.call` (`:80`) and `AnthropicProvider.stream` (`:134`) hardcode `"max_tokens": 4096`.
  - `OpenAIProvider.stream` (`:181-202`) and `_handle_finish_frame` (`:50-66`) emit `SSEEvent(type="done", data={})` on `finish_reason == "length"` with no truncation metadata.
  - **Existing test contract verified** (`tests/test_llm_providers.py:582-589`, `TestHandleFinishFrame::test_length`): asserts `events[-1].type == "done"` and `"error" not in [ev.type for ev in events]`. Therefore `finish_reason == "length"` MUST signal truncation via `SSEEvent(type="done", data={"truncated": True})` — **never** as an `"error"` event.
  - **Existing test contract verified** (`tests/test_llm_providers.py:62`, `TestOpenAIProvider::test_call_builds_correct_request`): asserts `result == {"choices": [{"message": {"content": "hi"}}]}` when `finish_reason` is absent. `_output_truncated` must be set on `call()` responses **only** when `finish_reason == "length"` / `stop_reason == "max_tokens"`.
  - **Pass 2 auto-close contract verified** (`ui/handlers/agent_command_handler.py:112-127`): unclosed `/ask @Agent "partial` matches `"([^"]*)$` to end-of-string. Therefore truncation metadata MUST NOT append a synthetic `[truncated]` footer onto `TurnResult.text` (which would corrupt the auto-closed `/ask` payload).
- Read `tests/test_iteration_judge.py` (`:1-255`) and `tests/test_agent_runtime.py` (`:525-715`, `:2077-2265`):
  - Single-iteration empty responses (`call_count == 1`, `turn_text_segments == []`) and tool-only turns followed by empty response (`turn_text_segments == []`) MUST continue to terminate `TurnStatus.FAILED` with `reason="empty_content"` (`test_agent_runtime.py:533-715`).
- Architecture owner: `agent/runtime.py` (`AgentRuntime`) owns turn-loop text assembly and terminal `TurnResult` construction; `agent/llm/*` owns SSE wire normalization and provider output-token limits.
- Existing patterns: `[tool loop closed — ...]` synthetic non-empty assistant message in `_graceful_iteration_close` (`agent/runtime.py:3976-3981`); `captured_usage` / `captured_error` side-channel keys (`_stream_error`) on the dict returned by `_call_llm_streaming` (`agent/runtime.py:3834-3860`).

---

## 1. Overview

### Problem
Five runtime and provider-streaming defects cause `/ask` consultations and `_pending_asks` relays to fail silently even when the model streamed valid output:
- **1A:** `_graceful_iteration_close` omits `TurnResult.text` (`""`), so `_do_response_complete` renders Debugger's streamed deltas in chat while `if self._on_agent_response is not None and text:` evaluates to `False` — dropping the relay back to Supervisor (triggered immediately at 50 tool calls on any machine without `DecisionModel` configured).
- **1B:** Models that stream their full report/`/ask` alongside their final tool call (Iteration $N$) and emit `content: ""` with `finish_reason: "stop"` on Iteration $N+1$ hit `TurnStatus.FAILED` (`empty_content`), rendering Iteration $N$'s prose in `_do_error` without firing `_on_agent_response`.
- **1C:** `_run_loop` passes only the final LLM iteration's `text_content` to `TurnResult.text`, wiping prose and `/ask` commands streamed during earlier tool-calling iterations of the same turn.
- **4A:** `AnthropicProvider` hardcodes `max_tokens: 4096` (cutting off long Supervisor/Debugger turns before their trailing `/ask`), and `finish_reason == "length"` / `stop_reason == "max_tokens"` is silently indistinguishable from `"stop"`.
- **4B:** OpenAI-compatible proxies that return non-SSE HTTP 200 JSON to `stream: True` yield empty content, and structured `delta.content` / `message.content` `list[dict]` chunks raise `TypeError`.

### Solution Summary
1. **Turn-level text accumulator (`turn_text_segments`) in `AgentRuntime._run_loop`:** Collect non-empty `text_content` emitted on tool-calling iterations (and across mid-loop `_drain_inbox` continuations).
   - **1A fix:** Pass accumulated `prior_text` into `_graceful_iteration_close`, setting `TurnResult.text` to `f"{prior_text}\n\n{close_note}"` (or `close_note` when `prior_text` is empty) so `_do_response_complete` renders the full turn text + close note and **always** fires `_on_agent_response`.
   - **1B fix:** When `not tool_calls_raw` and `_is_empty_content(text_content)` holds, if `turn_text_segments` is non-empty, `not response.get("_stream_error")`, and `text_content.strip() != "[calling tools]"`, append a non-empty role-alternation-safe closing assistant message (`"[completed after tool execution]"`) to `conv`, run limit/inbox checks, and complete `TurnStatus.COMPLETED` with `text="\n\n".join(turn_text_segments)`.
   - **1C fix:** On normal completion, keep `conv.add_assistant_message(text_content, [])` storing only the final iteration's message (no transcript duplication), while setting `TurnResult.text` to `"\n\n".join([*turn_text_segments, text_content])` when `turn_text_segments` is non-empty.
   - **Read-only inbound peek (`peek_pending_inbound`):** Expose `AgentRuntime.peek_pending_inbound(session_key) -> list[str]` under `self._lock` for cross-turn inbound inspection.
2. **Model-aware Anthropic `max_tokens` + truncation telemetry (4A):** Replace hardcoded `4096` in `AnthropicProvider` with `_anthropic_max_output_tokens(model)` (`4096` for Claude 3.0 / `20240620`, `8192` for Claude 3.5, `16384` for Claude 3.7 / Claude 4+). Propagate `truncated: True` on `done` SSE events (`finish_reason == "length"` / `stop_reason == "max_tokens"`) into `response["_output_truncated"]` and `TurnResult.metadata["output_truncated"]` with a `WARNING` log — without polluting `TurnResult.text`.
3. **Structured content-block coercion & non-SSE JSON fallback (4B):** Coerce `list[str | dict]` content blocks to `str` in `parse_sse_delta`, `extract_text_content`, and `_call_llm_streaming`. In `OpenAIProvider.stream`, if an HTTP 200 stream closes with zero SSE frames (`saw_sse_event == False`) and buffered non-SSE bytes parse as JSON, emit normalized `SSEEvent`s (`error` / `text_delta` / `tool_call_delta` / `usage` / `done`).

### Scope

| In Scope | Out of Scope |
|---|---|
| `agent/runtime.py` (`turn_text_segments`, `_graceful_iteration_close`, `peek_pending_inbound`, `_output_truncated`, `str` guard in `_call_llm_streaming`) | Any changes to `StreamingCallKwargs` or `_call_llm_streaming` signature |
| `agent/llm/streaming.py` (`coerce_content_text` + `parse_sse_delta`) | Changing `LLMProvider` protocol signatures in `agent/llm/protocol.py` |
| `agent/llm/extractors.py` (`extract_text_content` list-block support) | `ui/*`, `prompts/*`, `models/command.py` (covered in `SPEC-65`) |
| `agent/llm/openai_provider.py`, `minimax_provider.py`, `anthropic_provider.py` (4A + 4B) | Automatic multi-turn continuation prompts on `finish_reason == "length"` |
| `tests/test_spec64_turn_text_streaming.py` + SPEC-47 rows `R-8`, `R-9` | Modifying legacy assertions in `tests/test_agent_runtime.py` or `tests/test_llm_providers.py` |

### Architecture Principles
- **Strict layer isolation:** `agent/` has zero `ui/` or GTK dependencies.
- **Provider transcript validity:** `conv.messages` never duplicates prior-iteration prose and never ends a completed turn on `MessageRole.TOOL_RESULT` or an empty `MessageRole.ASSISTANT` (preserving Anthropic `user`/`assistant` alternation and Cohere/OpenAI non-empty assistant rules).
- **Signature stability:** `StreamingCallKwargs` and `LLMProvider` signatures do not change.

---

## 2. Changes by File

### 2.1 `agent/llm/streaming.py` (~30 lines added)

Add `coerce_content_text(content: object) -> str | None` above `parse_sse_delta` (`:84`) and use it inside `parse_sse_delta` (`:100-102`).

**Exact signature & implementation:**
```python
def coerce_content_text(content: object) -> str | None:
    """Normalize an OpenAI-compatible delta/message content value to ``str``.

    Returns:
      - ``None`` when ``content is None`` or contains no text blocks (so
        ``delta: {}`` and reasoning-only block frames emit no ``text_delta``).
      - ``content`` unchanged when ``isinstance(content, str)`` (preserving
        explicit ``""`` empty deltas).
      - Joined text from ``str`` items and ``{"type": "text"|"output_text",
        "text": str}`` dicts when ``isinstance(content, list)``.
    """
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                btext = block.get("text")
                if btype in ("text", "output_text", None) and isinstance(btext, str):
                    parts.append(btext)
        return "".join(parts) if parts else None
    return None
```

In `parse_sse_delta(d: dict) -> list[SSEEvent]` (`:100-102`), replace:
```python
    content = coerce_content_text(delta.get("content"))
    if content is not None:
        events.append(SSEEvent(type="text_delta", data={"content": content}))
```
Export `coerce_content_text` in module docstring / imports as needed. No new external imports required.

---

### 2.2 `agent/llm/extractors.py` (~15 lines changed)

Update `extract_text_content(response: dict, response_format: str = "openai") -> str` (`:111-128`) so non-streaming OpenAI responses carrying structured `list` content blocks extract cleanly to `str` instead of returning a raw `list`.

**Exact code (`:111-128`):**
```python
def extract_text_content(response: dict, response_format: str = "openai") -> str:
    """Extract text content from an API response."""
    if response_format == "openai":
        choices = response.get("choices", [])
        if not choices:
            return ""
        msg = choices[0].get("message", {})
        if not isinstance(msg, dict):
            return ""
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    btype = block.get("type")
                    btext = block.get("text")
                    if btype in ("text", "output_text", None) and isinstance(btext, str):
                        parts.append(btext)
            return "".join(parts)
        return ""

    elif response_format == "anthropic":
        content = response.get("content", [])
        if not isinstance(content, list):
            return ""
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts)

    return ""
```

---

### 2.3 `agent/llm/anthropic_provider.py` (~35 lines added/changed)

1. Add `_anthropic_max_output_tokens(model: str) -> int` above `AnthropicProvider` (`:38`):
   - Strips provider prefix via `model_id(model).strip().lower()`.
   - Legacy 4K-capped models (`claude-3-opus*`, `claude-3-sonnet*`, `claude-3-haiku*`, `claude-3-5-sonnet-20240620`, `claude-2*`, `claude-instant*`) return `4096` (avoids Anthropic HTTP 400 `max_tokens > 4096`).
   - Claude 3.7 and Claude 4+ families (`claude-3-7*`, `claude-sonnet-4*`, `claude-opus-4*`, `claude-haiku-4*`, `claude-4*`) return `16384`.
   - Claude 3.5 (`claude-3-5-sonnet-20241022`, `claude-3-5-haiku*`, etc.) and any other model default to `8192`.

```python
def _anthropic_max_output_tokens(model: str) -> int:
    """Return a safe max_tokens ceiling for the target Anthropic model."""
    bare = model_id(model).strip().lower()
    if bare.startswith((
        "claude-3-opus",
        "claude-3-sonnet",
        "claude-3-haiku",
        "claude-2",
        "claude-instant",
    )) or bare == "claude-3-5-sonnet-20240620":
        return 4096
    if bare.startswith((
        "claude-3-7",
        "claude-sonnet-4",
        "claude-opus-4",
        "claude-haiku-4",
        "claude-4",
    )):
        return 16384
    return 8192
```

2. In `AnthropicProvider.call` (`:80` and `:99-100`):
   - Replace `"max_tokens": 4096` with `"max_tokens": _anthropic_max_output_tokens(model)`.
   - Before returning the parsed response (`:100`):
     ```python
     with urlopen_with_ssl_retry(req, timeout=timeout) as resp:
         parsed = json.loads(resp.read())
         if isinstance(parsed, dict) and parsed.get("stop_reason") == "max_tokens":
             parsed["_output_truncated"] = True
         return parsed
     ```

3. In `AnthropicProvider.stream` (`:134`, `:153`, `:192-198`):
   - Replace `"max_tokens": 4096` with `"max_tokens": _anthropic_max_output_tokens(model)`.
   - Track `stop_reason: str = ""` alongside `usage: dict = {}` (`:153`).
   - In `elif etype == "message_delta":` (`:192-195`), capture `delta_meta = d.get("delta")`:
     ```python
     elif etype == "message_delta":
         delta_meta = d.get("delta")
         if isinstance(delta_meta, dict) and isinstance(delta_meta.get("stop_reason"), str):
             stop_reason = delta_meta["stop_reason"]
         usage_raw = d.get("usage")
         if _merge_usage(usage, usage_raw if isinstance(usage_raw, dict) else {}):
             yield SSEEvent(type="usage", data={"usage": dict(usage)})
     elif etype == "message_stop":
         done_data = {"truncated": True} if stop_reason == "max_tokens" else {}
         yield SSEEvent(type="done", data=done_data)
         return
     ```

---

### 2.4 `agent/llm/openai_provider.py` (~55 lines added/changed)

1. Import `extract_text_content` from `agent.llm.extractors` (pure module in same `agent/llm/` package; zero circular imports).
2. Add module-level helper `_events_from_non_sse_json(raw_bytes: bytes) -> list[SSEEvent] | None` to convert an HTTP 200 `application/json` ChatCompletion body (returned when a proxy ignores `"stream": True`) into the exact `SSEEvent` sequence `stream()` yields:
   ```python
   _MAX_NON_SSE_FALLBACK_BYTES = 2 * 1024 * 1024  # 2 MiB safety bound

   def _events_from_non_sse_json(raw_bytes: bytes) -> list[SSEEvent] | None:
       """Parse a non-SSE HTTP 200 JSON ChatCompletion body into SSEEvents."""
       if not raw_bytes or len(raw_bytes) > _MAX_NON_SSE_FALLBACK_BYTES:
           return None
       try:
           d = json.loads(raw_bytes.decode("utf-8"))
       except (json.JSONDecodeError, UnicodeDecodeError):
           return None
       if not isinstance(d, dict):
           return None
       events: list[SSEEvent] = []
       top_error = d.get("error")
       if top_error and isinstance(top_error, dict):
           events.append(SSEEvent(type="error", data={"error": top_error}))
           events.append(SSEEvent(type="done", data={}))
           return events
       choice = first_choice(d)
       finish_reason = None
       if choice:
           finish_reason = choice.get("finish_reason")
           msg = choice.get("message")
           if isinstance(msg, dict):
               text = extract_text_content(d, response_format="openai")
               if text:
                   events.append(SSEEvent(type="text_delta", data={"content": text}))
               tcs = msg.get("tool_calls") or []
               if isinstance(tcs, list):
                   for idx, tc in enumerate(tcs):
                       if isinstance(tc, dict) and isinstance(tc.get("function"), dict):
                           fn = tc["function"]
                           events.append(SSEEvent(type="tool_call_delta", data={
                               "index": tc.get("index", idx),
                               "name": fn.get("name") or "",
                               "arguments": fn.get("arguments") or "",
                               "id": tc.get("id") or "",
                           }))
           else:
               events.extend(parse_sse_delta(d))
           if finish_reason == "content_filter":
               events.append(SSEEvent(type="error", data={"error": {
                   "code": 400,
                   "reason": "content_filter",
                   "message": "Content was filtered by the provider (finish_reason=content_filter).",
               }}))
       usage = d.get("usage")
       if isinstance(usage, dict) and usage:
           events.append(SSEEvent(type="usage", data={"usage": usage}))
       if choice or usage is not None:
           done_data = {"truncated": True} if finish_reason == "length" else {}
           events.append(SSEEvent(type="done", data=done_data))
           return events
       return None
   ```
3. In `OpenAIProvider.call` (`:93-94`), mark `_output_truncated` **only** when `finish_reason == "length"`:
   ```python
   with urlopen_with_ssl_retry(req, timeout=timeout) as resp:
       parsed = json.loads(resp.read())
       if isinstance(parsed, dict) and first_choice(parsed).get("finish_reason") == "length":
           parsed["_output_truncated"] = True
       return parsed
   ```
4. In `OpenAIProvider.stream` (`:150-205`):
   - Track `saw_sse_event = False`, `non_sse_chunks: list[bytes] = []`, `non_sse_bytes = 0`.
   - When `ev is None`: if `line` is non-empty and `not line.startswith(b":")` and `not saw_sse_event` and `non_sse_bytes <= _MAX_NON_SSE_FALLBACK_BYTES`, append `line` to `non_sse_chunks` and add `len(line)` to `non_sse_bytes`.
   - When `ev is not None`: set `saw_sse_event = True`.
   - At `:201`, replace `yield SSEEvent(type="done", data={})` with:
     ```python
     done_data = {"truncated": True} if finish_reason == "length" else {}
     yield SSEEvent(type="done", data=done_data)
     ```
   - After the `for line in sse_lines(resp):` loop exits without returning, if `not saw_sse_event and non_sse_chunks`:
     ```python
     fallback_events = _events_from_non_sse_json(b"\n".join(non_sse_chunks))
     if fallback_events is not None:
         for out_ev in fallback_events:
             yield out_ev
     ```

---

### 2.5 `agent/llm/minimax_provider.py` (~10 lines changed)

1. In `_handle_finish_frame(d: dict) -> Iterator[SSEEvent]` (`:66`), pass `{"truncated": True}` on `done` when `finish_reason == "length"`:
   ```python
   done_data = {"truncated": True} if finish_reason == "length" else {}
   yield SSEEvent(type="done", data=done_data)
   ```
   *(Verified against `tests/test_llm_providers.py:582-589`: `events[-1].type == "done"` and `"error" not in [ev.type for ev in events]` remain `True`.)*
2. In `MiniMaxProvider.call` (`:133`), mark `_output_truncated` when `first_choice(result).get("finish_reason") == "length"`:
   ```python
   if isinstance(result, dict) and first_choice(result).get("finish_reason") == "length":
       result["_output_truncated"] = True
   return result
   ```

---

### 2.6 `agent/runtime.py` (~85 lines added/changed)

1. **New public read-only method `peek_pending_inbound` (after `take_stranded` at `:1523`):**
   ```python
   def peek_pending_inbound(self, session_key: str) -> list[str]:
       """Return a copy of queued (_inbox) and stranded (_stranded) texts for session_key without mutating state."""
       with self._lock:
           return list(self._inbox.get(session_key, ())) + list(self._stranded.get(session_key, ()))
   ```
2. **Turn-level text accumulator in `_run_loop` (`:2102`, `:2352-2525`, `:2572-2577`):**
   - Initialize `turn_text_segments: list[str] = []` and `turn_output_truncated = False` at `:2102` (once per `_run_loop`).
   - After `response = self._call_llm(...)` (`:2266-2274`), record truncation:
     ```python
     if response.get("_output_truncated"):
         turn_output_truncated = True
         logger.warning(
             "[tool-loop] sk=%s LLM response hit max output token limit at iteration=%d",
             session_key, iteration,
         )
     ```
   - **Finding 1B fix** inside `if not tool_calls_raw:` → `if _is_empty_content(text_content):` (`:2362`):
     Before treating empty content as `TurnStatus.FAILED`, check whether earlier tool-calling iterations in this turn already produced valid prose and there is no stream error or `[calling tools]` hallucination:
     ```python
     stream_err = response.get("_stream_error")
     is_calling_tools_stub = (
         isinstance(text_content, str) and text_content.strip() == "[calling tools]"
     )
     if turn_text_segments and not stream_err and not is_calling_tools_stub:
         combined_text = "\n\n".join(turn_text_segments)
         logger.info(
             "[tool-loop] sk=%s empty post-tool follow-up after %d prose segment(s); "
             "completing turn with accumulated text (len=%d)",
             session_key, len(turn_text_segments), len(combined_text),
         )
         # Non-empty assistant row preserves strict user/assistant alternation
         # after tool_result rows (Anthropic 400 guard) without duplicating
         # prior-iteration prose in conv.messages.
         conv.add_assistant_message("[completed after tool execution]", [])
         limit_result = self._check_and_stop_on_limit(session_key, conv)
         if limit_result is not None:
             stopped_reason, reason_msg = limit_result
             conv.add_assistant_message(f"[stopped: {reason_msg}]", [])
             self._terminate_turn(TurnResult(
                 status=TurnStatus.FAILED,
                 session_key=session_key,
                 turn_token=turn_token,
                 error=reason_msg,
                 metadata={"reason": stopped_reason, "iteration": iteration},
             ))
             return
         if self._drain_inbox(session_key, conv):
             continue
         meta = {
             "stream_error": None,
             "completed_from_prior_iteration_text": True,
         }
         if turn_output_truncated:
             meta["output_truncated"] = True
         self._terminate_turn(TurnResult(
             status=TurnStatus.COMPLETED,
             session_key=session_key,
             turn_token=turn_token,
             text=combined_text,
             metadata=meta,
         ))
         return
     ```
   - **Finding 1C fix** on the normal `not tool_calls_raw` completion path (`:2480-2514`):
     - `conv.add_assistant_message(text_content, [])` stays unchanged (stores only this iteration's message in `conv.messages`).
     - At `:2503` (`if self._drain_inbox(session_key, conv):`), preserve `text_content` before continuing:
       ```python
       if self._drain_inbox(session_key, conv):
           if not _is_empty_content(text_content):
               turn_text_segments.append(text_content)
           continue
       ```
     - Construct `final_turn_text = "\n\n".join([*turn_text_segments, text_content]) if turn_text_segments else text_content`.
     - Include `"output_truncated": True` in `metadata` when `turn_output_truncated` is `True`, and pass `text=final_turn_text` to `TurnResult`.
   - **Accumulate tool-iteration prose** right before `self._execute_tool_batch(...)` (`:2516`):
     ```python
     if not _is_empty_content(text_content):
         turn_text_segments.append(text_content)
     ```
   - **Finding 1A fix** at the soft-cap exhaustion call (`:2572-2577`) and in `_graceful_iteration_close` (`:3971-3988`):
     - Update `_graceful_iteration_close` signature with default `prior_text: str = ""` (backward-compatible for direct unit callers):
       ```python
       def _graceful_iteration_close(self, session_key: str, conv, turn_token,
                                     iteration: int, tool_errors: int,
                                     grants_used: int, hit_hard_cap: bool,
                                     prior_text: str = "") -> None:
           """Close the turn COMPLETED with a synthetic summary instead of FAILED."""
           reason = "hard_cap" if hit_hard_cap else "iteration_judge"
           close_note = (
               f"[tool loop closed — {reason} at {iteration} iterations, "
               f"{grants_used} extension(s) granted, {tool_errors} tool error(s). "
               f"Partial work above stands; resume with a follow-up message.]"
           )
           conv.add_assistant_message(close_note, [])
           final_text = f"{prior_text}\n\n{close_note}" if prior_text else close_note
           self._terminate_turn(TurnResult(
               status=TurnStatus.COMPLETED,
               session_key=session_key,
               turn_token=turn_token,
               text=final_text,
               error=None,
               metadata={"reason": reason, "iterations": iteration, "grants": grants_used},
           ))
       ```
     - At `:2572-2577`, pass `prior_text="\n\n".join(turn_text_segments)`.

3. **`_call_llm_streaming` hardening (`:3755`, `:3781-3785`, `:3828-3872`):**
   - Track `output_truncated = False` alongside `captured_error: dict = {}`.
   - At `:3781-3785` (`ev.type == "text_delta"`), guard against non-`str` content:
     ```python
     raw_text = ev.data.get("content")
     text = raw_text if isinstance(raw_text, str) else (coerce_content_text(raw_text) or "")
     parts.append(text)
     if self._on_text_delta:
         self._dispatch(self._on_text_delta, session_key, text, _turn_token=turn_token)
     ```
     *(Import `coerce_content_text` from `agent.llm.streaming` alongside `SSEEvent` at `:313`.)*
   - When `ev.type == "done"` (and in the post-done drain loop `:3845-3853`), check `if isinstance(ev.data, dict) and ev.data.get("truncated"): output_truncated = True`.
   - Set `if output_truncated: result["_output_truncated"] = True` on the returned `result` dict in both the `done` return (`:3860`) and the fallback return (`:3872`).

---

### 2.7 NEW `tests/test_spec64_turn_text_streaming.py` (~260 lines)

Covers every finding in this spec with deterministic, network-free unit tests:
1. **Finding 1A:** `_run_loop` streams prose across tool iterations until `max_tool_iterations` with no `DecisionModel` card → `TurnResult.status == COMPLETED`, `TurnResult.text` contains both the streamed prose AND `[tool loop closed — iteration_judge ...]`, and `_on_response_complete` receives that non-empty `text` (proving `turn_ui.py:531` fires `_on_agent_response`). Also tests tool-only exhaustion (`prior_text == ""` → `TurnResult.text == close_note`).
2. **Finding 1B:** Iteration 1 returns `content="Audit complete.\n/ask @Supervisor \"ready\""` + `tool_calls=[feed_card]`; Iteration 2 returns `content=""` + `tool_calls=[]` → completes `TurnStatus.COMPLETED` with `TurnResult.text == "Audit complete.\n/ask @Supervisor \"ready\""`, `_on_error` is NOT called, and `conv.messages[-1]` is a non-empty `ASSISTANT` message (`convert_messages_for_anthropic(conv.to_api_messages())` has strictly alternating `user`/`assistant` roles when a subsequent `USER` message is added). Also verifies single-turn empty content (`turn_text_segments == []`) and Iteration-2 `_stream_error` still terminate `TurnStatus.FAILED`.
3. **Finding 1C:** Iteration 1 returns `content="/ask @Debugger \"audit x.py\""` + `tool_calls=[diary]`; Iteration 2 returns `content="Logged to diary."` + `tool_calls=[]` → `TurnResult.text` contains both segments separated by `"\n\n"`, while `conv.messages` stores each segment once on its own assistant message.
4. **Finding 4A:** `_anthropic_max_output_tokens` returns `4096` for `claude-3-haiku-20240307` and `claude-3-5-sonnet-20240620`, `8192` for `claude-3-5-sonnet-20241022`, and `16384` for `claude-sonnet-4-20250514`; `OpenAIProvider.stream`, `MiniMaxProvider.stream`, and `AnthropicProvider.stream` propagate `truncated: True` into `_call_llm_streaming` → `TurnResult.metadata["output_truncated"] is True` without altering `TurnResult.text`.
5. **Finding 4B:** `parse_sse_delta`, `extract_text_content`, and `_call_llm_streaming` coerce `list[dict]` content blocks to `str` without `TypeError`; `OpenAIProvider.stream` falls back cleanly when an endpoint returns single-line or multi-line HTTP 200 `application/json` instead of `data:` SSE frames (including tool calls and HTTP-200 JSON error envelopes).

---

### Files NOT Changed (Rule 8)

- `agent/llm/protocol.py` — `LLMProvider.call` and `LLMProvider.stream` signatures already accept `model: str`, from which `_anthropic_max_output_tokens(model)` derives the safe output ceiling without adding protocol parameters.
- `agent/config.py` — `max_tool_iterations = 50` and `tool_iteration_hard_cap = 100` defaults from SPEC-60 remain unchanged.
- `ui/agent_runtime/turn_ui.py` — `_do_response_complete` (`:485-533`) already overwrites streaming text and invokes `_on_agent_response(session_key, text, project_name)` whenever `TurnResult.text` is non-empty; fixing `TurnResult.text` in `AgentRuntime` heals the UI and command-hook contract at its root owner (and keeps zero file overlap with `SPEC-65`).

---

## 3. Data Flow

1. **Multi-iteration / post-tool-empty / graceful-close turn (`1A`, `1B`, `1C`):**
   `Provider.stream()` → `SSEEvent("text_delta", {"content": str})` → `AgentRuntime._call_llm_streaming()` → `AgentRuntime._run_loop()`:
   - Iteration $k$ (with `tool_calls_raw` non-empty): non-empty `text_content` appended to `turn_text_segments`; `conv.add_assistant_message(text_content, tool_call_objects)` records Iteration $k$.
   - Terminal iteration:
     - Normal final text (`1C`): `TurnResult(COMPLETED, text="\n\n".join([*turn_text_segments, text_content]))`
     - Empty post-tool follow-up with prior prose and no `_stream_error` (`1B`): `conv.add_assistant_message("[completed after tool execution]", [])` + `TurnResult(COMPLETED, text="\n\n".join(turn_text_segments))`
     - Soft/hard iteration close (`1A`): `_graceful_iteration_close(..., prior_text="\n\n".join(turn_text_segments))` → `TurnResult(COMPLETED, text=f"{prior_text}\n\n{close_note}" or close_note)`
   - `_terminate_turn()` → `_on_response_complete(sk, result.text, _turn_token=tk)` → `RuntimeTurnUiMixin._do_response_complete()` → `set_streaming_text(sk, text)` + `end_streaming(...)` + `_on_agent_response(sk, text, project_name)` → `AgentCommandHandler.on_agent_response()` relays `_pending_asks` and extracts `/ask`.

2. **Truncation & non-SSE / list-content normalization (`4A`, `4B`):**
   `OpenAIProvider.stream` / `MiniMaxProvider.stream` / `AnthropicProvider.stream` → `parse_sse_delta` (`coerce_content_text`) or `_events_from_non_sse_json` → `SSEEvent("done", {"truncated": True})` → `_call_llm_streaming` sets `result["_output_truncated"] = True` → `_run_loop` logs `WARNING` and stamps `TurnResult.metadata["output_truncated"] = True`.

---

## 4. File Change Summary

| File | Change Type | Est. Lines | Risk Level |
|---|---|---|---|
| `agent/llm/streaming.py` | Modify (`coerce_content_text` + `parse_sse_delta`) | +30 / -2 | Low |
| `agent/llm/extractors.py` | Modify (`extract_text_content` list-block support) | +16 / -2 | Low |
| `agent/llm/anthropic_provider.py` | Modify (`_anthropic_max_output_tokens` + `stop_reason == "max_tokens"`) | +35 / -4 | Low |
| `agent/llm/openai_provider.py` | Modify (`_events_from_non_sse_json` + `finish_reason == "length"`) | +58 / -3 | Low-Medium |
| `agent/llm/minimax_provider.py` | Modify (`finish_reason == "length"` truncation flag) | +8 / -2 | Low |
| `agent/runtime.py` | Modify (`turn_text_segments`, `1A`/`1B`/`1C`, `peek_pending_inbound`, `_output_truncated`) | +85 / -12 | Medium |
| `tests/test_spec64_turn_text_streaming.py` | New (unit + integration coverage for `1A`, `1B`, `1C`, `4A`, `4B`) | +260 | Low |
| `docs/acceptance/rows.yaml` + `tests/mutations/R-8.diff`, `R-9.diff` | Modify / New (SPEC-47 rows `R-8`, `R-9`) | +35 | Low |

---

## 5. Implementation Order

1. **`agent/llm/streaming.py` + `agent/llm/extractors.py` (Finding 4B content coercion):** Implement `coerce_content_text` and list-block extraction. Verify with `.venv/bin/python -m pytest tests/test_llm_streaming.py tests/test_llm_extractors.py -q`.
2. **`agent/llm/anthropic_provider.py`, `openai_provider.py`, `minimax_provider.py` (Findings 4A & 4B):** Implement `_anthropic_max_output_tokens`, `truncated: True` on `done` events, and `_events_from_non_sse_json`. Verify with `.venv/bin/python -m pytest tests/test_llm_providers.py -q` (confirming `TestHandleFinishFrame::test_length` and `TestOpenAIProvider::test_call_builds_correct_request` stay green).
3. **`agent/runtime.py` (Findings 1A, 1B, 1C, 4A, 4B + `peek_pending_inbound`):** Wire `turn_text_segments`, `_graceful_iteration_close(..., prior_text=...)`, post-tool empty-content completion with `[completed after tool execution]`, and `_output_truncated`. Verify `TestStreamingSignature`, `tests/test_iteration_judge.py`, and `tests/test_agent_runtime.py`.
4. **Tests & Acceptance Gate (`tests/test_spec64_turn_text_streaming.py`, `R-8`, `R-9`):** Write the new test module, register `R-8` and `R-9` in `docs/acceptance/rows.yaml` with `tests/mutations/R-8.diff` and `R-9.diff`, and run `.venv/bin/python scripts/acceptance.py rows`.

---

## 6. Acceptance Criteria

- [ ] `_graceful_iteration_close` sets `TurnResult.text` to `f"{prior_text}\n\n{close_note}"` when `prior_text` is non-empty and `close_note` otherwise; `_on_response_complete` receives non-empty `text` on every graceful iteration close (Finding 1A).
- [ ] A multi-iteration turn that emits non-empty prose alongside tool calls in Iteration $N$ and receives empty `content` (`""` / whitespace) with no `tool_calls` and no `_stream_error` in Iteration $N+1$ terminates `TurnStatus.COMPLETED` with `TurnResult.text` equal to the accumulated prose, appends `"[completed after tool execution]"` to `conv.messages`, and preserves strict `user`/`assistant` alternation in `convert_messages_for_anthropic` (Finding 1B).
- [ ] Single-iteration empty responses (`turn_text_segments == []`), tool-only turns followed by empty responses (`turn_text_segments == []`), and responses with `_stream_error` or `"[calling tools]"` still terminate `TurnStatus.FAILED` (`tests/test_agent_runtime.py` green).
- [ ] A multi-iteration turn with prose in Iteration 1 (plus tool calls) and prose in Iteration 2 (no tool calls) delivers both segments joined by `"\n\n"` in `TurnResult.text` without duplicating Iteration 1's prose in `conv.messages` (Finding 1C).
- [ ] `AnthropicProvider` sends `max_tokens=4096` for Claude 3.0/`20240620`, `8192` for Claude 3.5, and `16384` for Claude 3.7/Claude 4+; `finish_reason == "length"` / `stop_reason == "max_tokens"` sets `TurnResult.metadata["output_truncated"] = True` without modifying `TurnResult.text` (Finding 4A).
- [ ] `OpenAIProvider.stream` parses non-SSE HTTP 200 JSON bodies when `saw_sse_event == False`; `parse_sse_delta`, `extract_text_content`, and `_call_llm_streaming` coerce `list[dict]` content blocks to `str` without `TypeError` (Finding 4B).
- [ ] `TestStreamingSignature` (`tests/test_agent_runtime.py`) and `tests/test_iteration_judge.py` pass without modification.

---

## 7. Acceptance rows (SPEC-47)

| Row | Claim | Node | Mutation | Registered |
|---|---|---|---|---|
| R-8 | Multi-iteration prose accumulated before tool calls and before `_graceful_iteration_close` is delivered in `TurnResult.text` to `_on_response_complete`. | `tests/test_spec64_turn_text_streaming.py::TestTurnTextContinuity` | `tests/mutations/R-8.diff` (drops `text=final_text` in `_graceful_iteration_close`) | 2026-11-14 |
| R-9 | Post-tool empty follow-up after non-empty tool-iteration prose completes `TurnStatus.COMPLETED` with accumulated text and Anthropic-valid role alternation instead of `FAILED` (`empty_content`). | `tests/test_spec64_turn_text_streaming.py::TestPostToolEmptyFollowup` | `tests/mutations/R-9.diff` (disables `if turn_text_segments and not stream_err` recovery branch in `_run_loop`) | 2026-11-14 |

---

## 8. Edge Cases

| Case | Expected Behavior |
|---|---|
| Turn calls 50 read-only tools with zero streamed prose and hits `iteration_judge` stop | `prior_text` is `""`; `TurnResult.text` is `close_note` (`"[tool loop closed — ...]"`), so the UI renders the close note instead of blank silence and `_on_agent_response` still runs to clear/relay any `_pending_asks`. |
| Iteration 1 streams prose + tool call; Iteration 2 hits OpenRouter mid-stream 429 (`_stream_error` set, `content=""`) | `not stream_err` is `False` → takes existing `TurnStatus.FAILED` provider-error path (never masks real mid-stream errors as `COMPLETED`). |
| Iteration 1 calls tools with `content=""`; Iteration 2 returns `content=""` and `tool_calls=[]` | `turn_text_segments` is `[]` → takes existing `TurnStatus.FAILED` (`reason="empty_content"`). |
| Next user message arrives after a 1B turn on an Anthropic model | `conv.messages` ended with `ASSISTANT("[completed after tool execution]")`, so `convert_messages_for_anthropic` emits `user(tool_result)` → `assistant` → `user(new_msg)` with zero consecutive `user` roles. |
| Model hits `finish_reason="length"` mid-`/ask @Debugger "unclosed payload` | `done` carries `{"truncated": True}`; `TurnResult.text` ends cleanly at `"unclosed payload` with no appended footer, allowing `AgentCommandHandler` Pass 2 auto-close to recover the command. |
| OpenAI-compatible proxy returns 3 MB of non-SSE garbage with HTTP 200 | `_MAX_NON_SSE_FALLBACK_BYTES` (`2 MiB`) stops buffering and skips JSON parse (bounded memory, no OOM). |
| Provider sends reasoning block `[{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "Answer"}]` in `delta.content` | `coerce_content_text` extracts `"Answer"` only, ignoring `"thinking"` dicts. |

---

## 9. ARCHITECTURE.md Updates Required

- **`§3.21` (`agent/runtime.py` — Tool-loop ceiling & turn completion, around line 1697):** Document `turn_text_segments` multi-iteration text assembly into `TurnResult.text`, `_graceful_iteration_close`'s non-empty `TurnResult.text` contract, the post-tool empty-content completion rule (`[completed after tool execution]`), `peek_pending_inbound(session_key)`, and `TurnResult.metadata["output_truncated"]`.
- **`§3.21` (`agent/llm/` provider adapters):** Document `_anthropic_max_output_tokens(model)` tiered ceilings (`4096` / `8192` / `16384`), `SSEEvent("done", {"truncated": True})`, `coerce_content_text`, and `OpenAIProvider.stream`'s non-SSE HTTP 200 JSON fallback.

---

## 10. Spec Self-Audit (Rule 9)

1. **Does every code sample work against the current codebase?** Verified: `_call_llm_streaming` keeps all 11 `StreamingCallKwargs` parameters untouched; `_graceful_iteration_close` adds `prior_text: str = ""` with a default so 7-arg callers remain valid; `TestHandleFinishFrame::test_length` (`events[-1].type == "done"`, no `"error"`) and `TestOpenAIProvider::test_call_builds_correct_request` (`_output_truncated` omitted when `finish_reason` is `None`) stay green.
2. **Did I enumerate all exception types?** Verified: `_events_from_non_sse_json` catches `(json.JSONDecodeError, UnicodeDecodeError)` and bounds input size to `2 MiB`; `coerce_content_text` and `extract_text_content` type-guard every container and element (`isinstance(..., dict)`, `isinstance(..., str)`).
3. **Did I verify key structures?** Verified: `_inbox` and `_stranded` are `dict[str, list[str]]` guarded by `self._lock` (`agent/runtime.py:735-736`); `_turn_state` and `_turn_results` are keyed by `(session_key, turn_token)` under `self._state_lock` (`:750-753`).
4. **Did I trace data flow end-to-end (including downstream Anthropic/UI consumers)?** Verified: caught and prevented both the Anthropic consecutive-`user` HTTP 400 after `tool_result` and the Pass 2 auto-close corruption if truncation text were appended to `TurnResult.text`.
5. **Zero file overlap with `SPEC-65`?** Verified: `SPEC-64` modifies only `agent/*` + `tests/test_spec64_turn_text_streaming.py`; `SPEC-65` modifies only `ui/*`, `models/command.py`, `prompts/system/*`, and `tests/test_spec65_*` / `tests/test_agent_command_handler.py`.
