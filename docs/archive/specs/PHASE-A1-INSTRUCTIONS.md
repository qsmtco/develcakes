# Phase A1 Instructions — Tool Middleware Module + Unit Tests

**Track:** A (Tool Middleware Chain)
**Scope:** Create `agent/tool_middleware.py` (NEW) and `tests/test_tool_middleware.py` (NEW). **Do NOT touch `agent/runtime.py` in this phase.**
**Spec reference:** `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §A.2.1, §A.2.2, §A.5
**Rule reference:** `prompts/steelFramedCodeWriter.md` — apply every rule.

## Objective

Create the tool middleware module with all classes, but do NOT wire it into `AgentRuntime` yet. Wiring is Phase A2. This phase delivers an isolated, fully-tested module that compiles and imports cleanly. The unit tests exercise the middleware classes in isolation (no `AgentRuntime` instance required).

## CRITICAL: Read these first

1. `prompts/steelFramedCodeWriter.md` (your standing orders for writing code)
2. `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §A.2.1 (Protocol), §A.2.2 (Concrete classes), §A.5 (Test cases), §A.7 (Backward compat)
3. `agent/tools.py` — verify `ToolResult` dataclass fields (import it, read the class). Do not modify it.
4. `docs/ARCHITECTURE.md` §2 — confirm `agent/` layer rules (imports only from `models/`, `utils/`, stdlib; no UI imports)

## Deliverable

### File 1: `agent/tool_middleware.py` (NEW)

Create the module containing EXACTLY these public symbols (verbatim from spec §A.2.1 + §A.2.2, with these adjustments from the spec's "Revised approach" in §A.2.4):

1. **`ToolMiddleware` Protocol** (§A.2.1) — `__call__(self, tool_name, args, ctx, next) -> ToolResult`
2. **`ToolContext` dataclass** (§A.2.1) — fields: `session_key, project_path, iteration, bypass_approval=False, audit_log=None, user_id="", enforcement_config=None, si_enforcement=None`
3. **`EnforcementMiddleware`** (§A.2.2) — wraps executor, runs enforcement check after successful writes.
4. **`StuckDetectionMiddleware`** (§A.2.2) — wraps executor, records tool calls + detects loops.
5. **`ToolMiddlewareChain`** (§A.2.2) — composes middleware in onion order.

**DO NOT create `ApprovalMiddleware`.** The spec's "Revised approach" (§A.2.4, "IMPORTANT UPDATE" block) removes it from the chain — approval stays inline in `_run_loop` (Phase A2). Creating `ApprovalMiddleware` would be dead code. Final chain order is `[EnforcementMiddleware, StuckDetectionMiddleware]` per §A.2.4.

### File 2: `tests/test_tool_middleware.py` (NEW)

Write tests from spec §A.5. Minimum 18 test cases. **30%+ sad-path coverage required** (spec §A.5 cases 16-18). One test file, organized by class with clear test class names.

**Test cases required (spec §A.5):**

EnforcementMiddleware (7):
- test_enforcement_passes_through_non_write_tool
- test_enforcement_passes_through_failed_write
- test_enforcement_appends_message_on_success
- test_enforcement_skips_when_globally_disabled
- test_enforcement_skips_when_agent_disabled
- test_enforcement_dispatches_status_per_check
- test_enforcement_no_status_callback_is_safe

StuckDetectionMiddleware (3):
- test_stuck_no_message_when_not_stuck
- test_stuck_appends_message_when_stuck
- test_stuck_uses_correct_session_key

ToolMiddlewareChain (3):
- test_chain_executes_in_order
- test_chain_short_circuit_does_not_reach_executor
- test_chain_executor_result_passes_through

Sad-path (3):
- test_enforcement_check_raises_does_not_crash_loop
- test_stuck_check_raises_does_not_crash_loop
- test_chain_with_empty_middleware_list

**Integration tests (`test_enforcement_fires_on_write_in_run_loop`, `test_stuck_detection_fires_in_run_loop`) are DEFERRED to Phase A2** — they require `AgentRuntime` wiring which doesn't exist yet. Do not write them in A1.

### Implementation notes from spec

**EnforcementMiddleware behavior (§A.2.2):**
- Only runs on `write_file` / `edit_file` tools AND `result.success == True`.
- Two-level gate: `ctx.enforcement_config is not None and ctx.enforcement_config.enabled` AND (`ctx.si_enforcement if ctx.si_enforcement is not None else True`).
- Calls `enforcement_check_fn(tool_name, args, result, ctx.project_path, ctx.enforcement_config)`.
- On `enf_result.appended_message`: replaces result with appended output (use `dataclasses.replace`), dispatches per-check via `on_status` callback if provided.
- **Sad-path:** if `enforcement_check_fn` raises, catch the exception, log it, return the original result unmodified. Do NOT crash the tool loop. (Spec §A.5 case 16.)

**StuckDetectionMiddleware behavior (§A.2.2):**
- Calls `stuck_check_fn(ctx.session_key, tool_name, args, ctx.iteration)` after execution.
- If it returns a message string, appends to `pending_messages[ctx.session_key]`.
- **Sad-path:** if `stuck_check_fn` raises, catch, log, return result unmodified. (Spec §A.5 case 17.)

**ToolMiddlewareChain behavior (§A.2.2):**
- `run(tool_name, args, ctx, executor)` — middlewares wrap in registration order. `middlewares[0]` is outermost.
- Empty middleware list → executor called directly (spec §A.5 case 18).

**Imports allowed:**
- stdlib (`dataclasses`, `typing`, `logging`)
- `agent.tools.ToolResult` (read-only — you import it, do not modify `agent/tools.py`)
- NO imports from `ui/`, `gateway/`, `agent.runtime`

### Mock pattern for tests

Use `unittest.mock.MagicMock` for `enforcement_check_fn`, `stuck_check_fn`, `on_status`, and `executor`. Construct `ToolContext` directly with primitive fields. Do NOT import `AgentRuntime` in this test file.

## Verification commands (run yourself, paste output in COMPLETENESS)

```bash
# Module compiles
python3 -c "from agent.tool_middleware import ToolMiddlewareChain, ToolContext, EnforcementMiddleware, StuckDetectionMiddleware, ToolMiddleware; print('import OK')"

# Module enforces no ApprovalMiddleware
grep -c "class ApprovalMiddleware" agent/tool_middleware.py  # must print 0

# Tests pass (should be 15 cases — 12 unit + 3 sad-path)
python3 -m pytest tests/test_tool_middleware.py -v

# Lint clean
ruff check agent/tool_middleware.py tests/test_tool_middleware.py
```

## COMPLETENESS checklist (mandatory in your reply)

At the end of your reply, include this block filled in:

```
COMPLETENESS:
- [x/not done] agent/tool_middleware.py created with 5 public symbols (Protocol, ToolContext, EnforcementMiddleware, StuckDetectionMiddleware, ToolMiddlewareChain) — evidence: <import output>
- [x/not done] ApprovalMiddleware NOT created (spec revised approach §A.2.4) — evidence: grep count = 0
- [x/not done] tests/test_tool_middleware.py created with 15 test cases (12 unit + 3 sad-path) — evidence: pytest count
- [x/not done] All tests pass — evidence: pytest output
- [x/not done] ruff check clean on both files — evidence: ruff output
- [x/not done] agent/tools.py not modified — evidence: git diff --name-only agent/tools.py shows no changes
- [x/not done] agent/runtime.py not modified — evidence: git diff --name-only agent/runtime.py shows no changes
- [x/not done] No imports from ui/, gateway/, or agent.runtime in tool_middleware.py — evidence: grep output
```

## Do NOT

- Do NOT modify `agent/runtime.py`, `agent/tools.py`, `agent/enforcement.py`, `agent/context_strategy.py`, or any file under `tests/test_agent_runtime.py`.
- Do NOT create `ApprovalMiddleware` (dead code per spec §A.2.4 revised approach).
- Do NOT write the integration tests that require `AgentRuntime` (deferred to Phase A2).
- Do NOT anchor anything to line numbers — use identifiers.

## When done

Reply with COMPLETENESS checklist + all verification command outputs pasted verbatim. Do not summarize — paste raw output.
