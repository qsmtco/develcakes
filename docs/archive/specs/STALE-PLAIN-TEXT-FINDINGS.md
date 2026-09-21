# Stale `sb.plain_text` Verification Findings

**Scope:** `AgentRuntimeHandler._do_text_delta/_do_response_complete`, `ChatRenderHandler.update_streaming/end_streaming`, and runtime streaming assembly.

## Verdict

The stale-`sb.plain_text` explanation is **real and sufficient for the reported one-word truncation**, but the proposed fix is not fully correct. The handler throttle runs before `ChatRenderHandler.update_streaming`; therefore skipped calls do not update `sb.plain_text`, while `_streaming_text` continues accumulating. `end_streaming._finalize` renders `sb.plain_text`, so a partial bubble is expected. The runtime logs prove completion ran, but do not disprove this UI-state loss.

## BUG #1 — crabcard cleaning is overwritten by raw text

**Severity:** HIGH  
**Attack vector:** A streamed response contains a crabcard and `extract_crabcards` returns shorter `cleaned` text.  
**Reproduction:** At `agent_runtime_handler.py:1528-1530`, `set_streaming_text(cleaned)` is called. Then `streaming_text` is read and the new guard at `:1543-1545` sees `len(text) > len(cleaned)` and writes raw `text` back. `end_streaming` then renders the raw crabcard block, undoing Phase C's documented cleanup.  
**Fix:** Establish the authoritative final display text once: extract cards from the authoritative source, then set the cleaned result after any stale-text repair; or do not apply the raw-text overwrite when Phase C has already produced `cleaned` text. Never use length alone to decide which representation wins.

## BUG #2 — length guard can preserve wrong/partial streaming text

**Severity:** MEDIUM  
**Assumption violated:** `len(text) <= len(streaming_text)` means the tracked streaming text is correct.  
**Attack vector:** Runtime completion text differs from the handler accumulator (tool-loop iterations, provider normalization, or a completion payload that omits streamed content) and is shorter while `sb.plain_text` is stale or otherwise not the authoritative response.  
**Actual:** The guard skips `set_streaming_text`; `_finalize` renders `sb.plain_text`. Length is not an identity or provenance check.  
**Fix:** Use an explicit authoritative-text contract rather than a length heuristic. If `text` is guaranteed to be the complete display response, set it unconditionally (then apply crabcard cleaning). If not guaranteed, reconcile the streamed accumulator and completion payload explicitly and test tool-call/multi-iteration cases.

## Question answers

1. **Actual root cause?** Yes, for this path: `_do_text_delta` accumulates `_streaming_text` on every accepted delta, but only invokes `update_streaming` every 50ms (`:1068-1076`). `update_streaming` is the only writer of `sb.plain_text` during streaming (`chat_render_handler.py:459-462`), and `end_streaming` renders that field (`:570-604`). A first-chunk/last-throttled value can therefore be displayed despite a 546-character completion.
2. **Does overwrite guarantee full render?** If `set_streaming_text(session_key, text)` executes while the bubble exists, yes: `end_streaming` closes over the same `StreamingBubble` object after popping it, and `_finalize` reads its updated `plain_text`. The current conditional does not guarantee it because it may skip the write.
3. **Can `text` be shorter?** Yes, it can differ in tool-loop/multi-response scenarios: `_call_llm_streaming` builds `full_content` per streaming LLM call, while `_run_loop`'s final `text_content` is the current call's extracted content. Also empty/error/provider-normalized completion paths can diverge from deltas. This must be verified by contract/tests, not assumed.
4. **Is length guard correct?** No. It cannot distinguish stale partial text from valid cleaned text. It is specifically wrong after crabcard extraction because cleaning intentionally shortens the text.
5. **Crabcard interaction?** The current ordering is broken: Phase C writes cleaned text, then the length guard can overwrite it with raw `text`. This reintroduces feed-card markup into the final bubble.

## Remaining diagnostic caveat

The evidence rules out a runtime truncation/storage bug only for the shown path: `text_len=546` and conversation JSON are consistent with runtime completion. It does not rule out other UI causes in a separate path (missing bubble, cross-turn stale callbacks, or Pango/render failure), but no alternate explanation is needed for this exact throttle/`plain_text` mismatch.

## Tests needed

Add pure tests for: throttled delta accumulation followed by completion; unconditional authoritative replacement; crabcard-cleaned text not overwritten; tool-loop where completion and streamed accumulator differ; empty completion; and `set_streaming_text` called after `start_streaming` but before `end_streaming`.
