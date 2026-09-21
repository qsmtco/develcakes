# PHASE 1 — SSE Frame-Shape Hardening

**Spec:** `docs/specs/SPEC-SSE-FRAME-SHARDENING.md`
**Files to change:** `agent/runtime.py`, `ui/handlers/agent_runtime_handler.py`, `tests/test_agent_runtime.py`

---

## FIX 1 — Add `_first_choice` helper

**File:** `agent/runtime.py`
**Insertion point:** After `_parse_sse_delta` function ends (around line 530, before the next function).

Insert:
```python
def _first_choice(d: dict) -> dict:
    """Return choices[0] from an OpenAI-format SSE frame, or {} if missing/empty.

    Defensive against three legitimate frame shapes:
      - {"choices": [...]} — normal delta/finish frame
      - {"choices": [], "usage": {...}} — OpenAI trailing usage frame
      - {} or {"usage": {...}} — keepalive / pre-delta frame

    Replaces the unsafe d.get("choices", [{}])[0] pattern.
    """
    choices = d.get("choices")
    return choices[0] if choices else {}
```

---

## FIX 2 — Patch `_parse_sse_delta` line 519

**File:** `agent/runtime.py`

**Current (line 519):**
```python
    delta = d.get("choices", [{}])[0].get("delta", {})
```

**Replace with:**
```python
    choice = _first_choice(d)
    delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
```

---

## FIX 3 — Patch `_stream_openai_events` (around lines 900-905)

**File:** `agent/runtime.py`

**Current:**
```python
            if ev.type != "raw":
                continue
            d = ev.data
            # W11: text + tool_call deltas are shared with _stream_minimax_events
            for out_ev in _parse_sse_delta(d):
                yield out_ev
            # OpenAI-compatible providers emit a usage chunk at the end of the stream,
            # typically in a frame with empty choices. Capture and forward it.
            # See SPEC-CONTEXT-BLOAT-PHASE-3.md §2.1.1 (BUG #3 fix).
            usage = d.get("usage")
            if usage:
                yield SSEEvent(type="usage", data={"usage": usage})
```

**Replace with:**
```python
            if ev.type != "raw":
                continue
            d = ev.data
            # Gate on choices presence — skip delta extraction on empty-choices
            # frames (OpenAI trailing usage, OpenRouter keepalive).
            choice = _first_choice(d)
            if choice:
                # W11: text + tool_call deltas are shared with _stream_minimax_events
                for out_ev in _parse_sse_delta(d):
                    yield out_ev
            # Capture usage regardless (some frames have both choices and usage)
            usage = d.get("usage")
            if usage:
                yield SSEEvent(type="usage", data={"usage": usage})
```

---

## FIX 4 — Patch `_stream_minimax_events` first branch (around line 985)

**File:** `agent/runtime.py`

**Current:**
```python
                    d = ev.data
                    # W11: text + tool_call deltas are shared with _stream_openai_events
                    for out_ev in _parse_sse_delta(d):
                        yield out_ev
                    finish_reason = d.get("choices", [{}])[0].get("finish_reason")
                    if finish_reason in ("stop", "tool_calls", "length"):
```

**Replace with:**
```python
                    d = ev.data
                    choice = _first_choice(d)
                    if choice:
                        # W11: text + tool_call deltas are shared with _stream_openai_events
                        for out_ev in _parse_sse_delta(d):
                            yield out_ev
                        finish_reason = choice.get("finish_reason")
                        if finish_reason in ("stop", "tool_calls", "length"):
```

**IMPORTANT:** The `if finish_reason ...` block AND its body (the usage capture + done yield + return) must now be indented one more level (inside the `if choice:` block). Read the full current code block before editing to get the indentation right.

---

## FIX 5 — Patch `_stream_minimax_events` main loop (around line 1008)

**File:** `agent/runtime.py`

**Current:**
```python
            d = ev.data
            # W11: text + tool_call deltas are shared with _stream_openai_events
            for out_ev in _parse_sse_delta(d):
                yield out_ev
            # MiniMax signals stream end via finish_reason, not [DONE]
            finish_reason = d.get("choices", [{}])[0].get("finish_reason")
            if finish_reason in ("stop", "tool_calls", "length"):
```

**Replace with:**
```python
            d = ev.data
            choice = _first_choice(d)
            if choice:
                # W11: text + tool_call deltas are shared with _stream_openai_events
                for out_ev in _parse_sse_delta(d):
                    yield out_ev
                # MiniMax signals stream end via finish_reason, not [DONE]
                finish_reason = choice.get("finish_reason")
                if finish_reason in ("stop", "tool_calls", "length"):
```

**Same indentation note as FIX 4.** The `finish_reason` block must be inside `if choice:`.

---

## FIX 6 — Log malformed SSE frames at DEBUG

**File:** `agent/runtime.py`

**Current (inside `_parse_sse_line`, the except clause around line 502):**
```python
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
```

**Replace with:**
```python
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.debug(
            "[sse-line] drop malformed frame (%s): %r",
            type(e).__name__,
            line[:200],
        )
        return None
```

---

## FIX 7 — Error context in `_call_llm_streaming`

**File:** `agent/runtime.py`

Find the `try/except` block around `_call_llm_streaming` (search for the exception handler after the streaming call). Add context attachment. The exception handler should look like:

```python
    except (IndexError, KeyError, TypeError, ValueError) as e:
        e._crabcakes_context = {
            "provider": caller_key,
            "model": model,
            "exception_type": type(e).__name__,
        }
        raise
```

Read the actual code around the streaming call to find the exact try/except block and the variable names for `caller_key` and `model`.

---

## FIX 8 — UI error enrichment

**File:** `ui/handlers/agent_runtime_handler.py`

**Step A:** In `__init__`, add:
```python
        self._last_error_exception: dict[str, "BaseException | None"] = {}
```

**Step B:** In `_on_error` (or wherever the error callback is), capture the exception:
```python
    def _on_error(self, session_key: str, message: str) -> None:
        if isinstance(message, BaseException):
            self._last_error_exception[session_key] = message
        else:
            self._last_error_exception[session_key] = None
        # ... rest of existing method unchanged ...
```

Read the actual `_on_error` method first — it may have a different signature or dispatch pattern. Adapt accordingly.

**Step C:** In `_do_error`, enrich the message:
```python
        rendered = f"[Error] {message}"
        try:
            exc_obj = self._last_error_exception.get(session_key)
            if exc_obj is not None:
                ctx = getattr(exc_obj, "_crabcakes_context", None)
                if ctx:
                    rendered += f"\nProvider: {ctx.get('provider')} | Model: {ctx.get('model')}"
        except Exception:
            pass
```

Use `rendered` instead of `f"[Error] {message}"` in the bubble render call.

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- **Read `agent/runtime.py` lines 480-1130 and `ui/handlers/agent_runtime_handler.py` lines 1270-1320 before editing.** The exact line numbers may have drifted.
- **Indentation is critical in FIX 4 and FIX 5** — the finish_reason block moves inside `if choice:`.
- Do NOT touch `_stream_anthropic_events`, `_extract_tool_calls`, `_extract_text_content`, or `_extract_usage` — they're already correct.

## Verification commands (run these, paste the output)

```bash
cd /path/to/projects/crabcakes

# 1. _first_choice works
python3 -c "
from agent.runtime import _first_choice
assert _first_choice({}) == {}
assert _first_choice({'choices':[]}) == {}
assert _first_choice({'choices':[{'x':1}]}) == {'x':1}
print('OK: _first_choice works')
"

# 2. _parse_sse_delta on empty choices doesn't crash
python3 -c "
from agent.runtime import _parse_sse_delta
assert _parse_sse_delta({'choices':[],'usage':{'total_tokens':42}}) == []
assert _parse_sse_delta({}) == []
print('OK: empty choices handled')
"

# 3. No remaining unguarded [0] indexing
grep -n 'd\.get(\"choices\", \[{}\])\[0\]' agent/runtime.py
# Expected: 0 matches

# 4. _first_choice used at all 3 sites
grep -n '_first_choice' agent/runtime.py
# Expected: 4 matches (1 def + 3 call sites)

# 5. Existing tests pass
python3 -m pytest tests/test_agent_runtime.py tests/test_streaming.py -v

# 6. Syntax check
python3 -c "import ast; ast.parse(open('agent/runtime.py').read()); ast.parse(open('ui/handlers/agent_runtime_handler.py').read()); print('SYNTAX OK')"
```

## Deliverables (COMPLETENESS checklist required)

```
COMPLETENESS:
- [x/not done] Fix 1: _first_choice helper added — evidence: (command 1)
- [x/not done] Fix 2: _parse_sse_delta patched — evidence: (command 2)
- [x/not done] Fix 3: _stream_openai_events patched — evidence: (command 3)
- [x/not done] Fix 4-5: _stream_minimax_events patched (both sites) — evidence: (command 3)
- [x/not done] Fix 6: malformed frame logging — evidence: (grep shows logger.debug)
- [x/not done] Fix 7: error context annotation — evidence: (grep shows _crabcakes_context)
- [x/not done] Fix 8: UI error enrichment — evidence: (grep shows _last_error_exception)
- [x/not done] Existing tests pass — evidence: (command 5)
- [x/not done] No syntax errors — evidence: (command 6)
```
