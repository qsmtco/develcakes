# Stale `plain_text` Fix v2 — Audit Findings

**Scope:** `ui/handlers/agent_runtime_handler.py` and `ui/handlers/chat_render_handler.py`.

## Verdict

**Fixes both v1 defects in the single-response streaming path, but is not universally safe.** The ordering is correct and `set_streaming_text()` updates the exact `StreamingBubble.plain_text` later consumed by `end_streaming`. However, the runtime completion `text` is not necessarily the complete text accumulated across a multi-iteration tool loop; unconditional replacement can discard earlier streamed content.

## BUG #1 — unconditional replacement can lose earlier tool-loop text

**Severity:** HIGH  
**Assumption violated:** Runtime `text` at final completion is the complete text represented by the streaming bubble.  
**Attack vector:** A tool loop emits text deltas in an earlier LLM iteration, executes a tool, then emits a final text-only response.  
**Reproduction:** `_call_llm_streaming` accumulates `full_content` per call and returns it (`agent/runtime.py:1681-1704`, `:1763-1769`). `_run_loop` assigns `text_content` from the current response (`:1047`) and dispatches completion with that value (`:1262-1265`). The handler's `_streaming_text`/`sb.plain_text` may contain text from prior iterations, but v2 overwrites it with only final-iteration `text`. Earlier visible content is lost.

**Fix:** Define the display contract explicitly. If only the final text-only response is user-visible, suppress/replace prior iteration text intentionally. If all streamed text is intended to remain, maintain a per-turn authoritative accumulator in runtime and pass that complete value to completion; do not assume final `text_content` is cumulative.

## Question answers

1. **Ordering:** Correct: authoritative assignment (`:1521-1522`) precedes crabcard extraction (`:1524-1538`) and `end_streaming` (`:1546-1551`).
2. **Empty/None text:** Yes, tool-only/error paths can complete with empty text. The guard skips replacement, leaving the existing `sb.plain_text`; `render=bool(streaming_text.strip())` suppresses an empty final bubble but still cleans up. If non-empty stale text exists from an earlier iteration, it will be rendered despite an empty authoritative completion; that is either intentional accumulated output or another manifestation of the contract ambiguity. A truly authoritative empty response should clear it explicitly.
3. **Setter correctness:** Correct. `set_streaming_text` assigns `sb.plain_text` directly (`chat_render_handler.py:425-437`), and `end_streaming` closes over the same popped `StreamingBubble` object and reads `sb.plain_text` (`:568-604`).
4. **Text divergence:** Yes. In multi-iteration tool loops, streamed chunks and final `text_content` are per-call, not proven cumulative. This is the remaining high-risk issue above.

## Confirmed v1 fixes

- No length guard remains; valid shorter authoritative text is not rejected.
- Crabcard extraction runs after raw text assignment and its `cleaned` result is the final write; raw text is not re-applied afterward. The v1 crabcard-overwrite bug is fixed.
- The original throttle explanation remains valid: handler `_streaming_text` accumulates every delta, while `update_streaming`/`sb.plain_text` only receives non-throttled updates (`:1068-1076`; `chat_render_handler.py:459-462`).

## Additional notes

- `set_streaming_text` can return `False` if the bubble disappeared between `was_streaming` and the setter; current code ignores that result. On the GTK main thread this should not normally race, but a defensive check/log would make the guarantee explicit.
- No dedicated v2 tests were found. Required tests: throttle + authoritative replacement, crabcard cleaning, empty completion, shorter completion, and multi-iteration tool-loop text semantics.
