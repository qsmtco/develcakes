# Supervisor Instructions for QTR Builder

## Role
You are the **implementation supervisor**. Your job is to hand each phase to QTR,
audit every QTR response with the adversarial debugger, and verify evidence
before approving each phase.

**You do NOT write code.** You delegate to QTR and verify.

---

## Files to Read Before Starting

1. `/path/to/projects/crabcakes/docs/specs/SPEC-RUNTIME-HARDENING-AUDIT.md`
2. `/path/to/projects/crabcakes/docs/specs/SUPERVISOR-QTR-LOOP.md` (this file)
3. `/path/to/projects/crabcakes/agent/runtime.py` (full file — already read)
4. `/path/to/projects/crabcakes/docs/ARCHITECTURE.md` (already read)

---

## The 7 Phases

Execute in strict order. Do NOT start Phase N+1 until Phase N is verified.

| Phase | File | Target |
|-------|------|--------|
| 1 | `PHASE-1-INSTRUCTIONS.md` | Extract `_convert_messages_for_anthropic` + `_convert_tools_for_anthropic` |
| 2 | `PHASE-2-INSTRUCTIONS.md` | Fix `_stream_anthropic_events` (W2/W3/W4) |
| 3 | `PHASE-3-INSTRUCTIONS.md` | Add 3 streaming regression tests |
| 4 | `PHASE-4-INSTRUCTIONS.md` | Dead code cleanup W5–W10 |
| 5 | `PHASE-5-INSTRUCTIONS.md` | Fix stuck-message double-pop W12 |
| 6 | `PHASE-6-INSTRUCTIONS.md` | Extract MiniMax SSE helper W11 |
| 7 | `PHASE-7-INSTRUCTIONS.md` | Optimize `list_conversations` W13/W14 |

---

## Per-Phase Protocol

For each phase:

1. **Read the phase instruction file** completely.
2. **Spawn QTR** with `sessions_spawn`, sending:
   - The phase instruction file path
   - The spec section reference
   - A clear one-paragraph objective
   - Instruction: "Read every file before starting. Report completion with evidence."
3. **Wait for QTR completion** via `sessions_yield`.
4. **Adversarial audit** the returned code:
   - Read every changed file with `read` tool
   - Run the verification commands from the phase instructions
   - Challenge every assumption — trace every reference
   - If audit fails: send QTR back with specific corrections
5. **Final audit** at the end: verify all 9 work items from the spec are addressed

---

## Adversarial Audit Checklist (Per Phase)

- [ ] `python3 -c "from agent.runtime import AgentRuntime; print('import ok')"` passes
- [ ] `python3 -m py_compile agent/runtime.py && echo "syntax ok"` passes
- [ ] No new import errors introduced
- [ ] All grep references from spec still point to valid code
- [ ] Phase-specific checks (listed in each phase instructions)

## Final Audit Checklist (End of All Phases)

- [ ] W1: Colon fix verified in `test_special_agent_colon_key_ok`
- [ ] W2: `_stream_anthropic_events` uses `_convert_messages_for_anthropic` + `_convert_tools_for_anthropic`
- [ ] W3: `stream_options` removed from `_stream_anthropic_events`
- [ ] W4: `_sse_lines` return type annotation is `Iterator[bytes]`
- [ ] W5: No function-local `urllib.request` imports remain
- [ ] W6: `finally: pass` block removed from `execute_tool`
- [ ] W7: Duplicate `execute_tool` import removed
- [ ] W8: `result2` renamed to `conv_infos`
- [ ] W9: `stream_options` removed from `_stream_openai_events` and `_stream_minimax_events`
- [ ] W10: Duplicated SSE delta parsing unified via `_parse_sse_delta`
- [ ] W11: Stuck-message double-pop removed (only one pop remains)
- [ ] W12/W13: `list_conversations` uses lightweight JSON load
- [ ] W14: Three streaming regression tests pass
- [ ] `python3 -m pytest tests/test_agent_runtime.py -v` passes (or at least no new failures)
