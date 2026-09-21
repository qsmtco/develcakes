# Phase A2 Instructions — Wire ToolMiddlewareChain into AgentRuntime

**Track:** A (Tool Middleware Chain) — completes Track A
**Scope:** Edit `agent/runtime.py` (4 edits) + add integration tests to `tests/test_tool_middleware.py`
**Spec reference:** `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §A.2.3 (wiring), §A.2.4 (call-site change), §A.2.5 (audit log), §A.5 (integration tests 14-15)
**Rule reference:** `prompts/steelFramedCodeWriter.md` — apply every rule.

## Objective

Replace the inline enforcement block (currently at runtime.py lines ~2524-2551) and the inline stuck-detection block (~2553-2561) in `_run_loop` with a single call to `self._tool_chain.run(...)`. Construct the chain once in `__init__`. **Approval gating stays inline** (temporal ordering constraint per spec §A.2.4 — must fire before `on_tool_call_start`).

After this phase, the tool-execution section of `_run_loop` reads:
1. Pre-condition: approval gate (inline — unchanged)
2. Dispatch `on_tool_call_start` + `tc.mark_executing()`
3. Execute through chain (`EnforcementMiddleware` → `StuckDetectionMiddleware` → `execute_tool`)
4. Post-condition: record results + audit log

## CRITICAL: Read these first

1. `prompts/steelFramedCodeWriter.md` (your standing orders)
2. `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §A.2.3, §A.2.4, §A.2.5, §A.6
3. `agent/tool_middleware.py` (the module you're wiring in — read it to see exact signatures)
4. `agent/runtime.py` lines 1700-1720 (`__init__` tail where you'll add chain construction)
5. `agent/runtime.py` lines 2455-2585 (the `_run_loop` tool block you're editing)
6. `agent/enforcement.py` around the `check()` function — verify its signature

## Deliverable — 4 edits to runtime.py + 2 integration tests

### Edit 1: Add imports (top of runtime.py)

Find the existing import block near the top of `agent/runtime.py`. After the existing `from agent.tools import ...` and `from agent.enforcement import ...` lines, add:

```python
from agent.tool_middleware import (
    EnforcementMiddleware,
    StuckDetectionMiddleware,
    ToolContext,
    ToolMiddlewareChain,
)
```

**Do NOT remove** any existing imports. Add only these 4 names.

### Edit 2: Construct the chain in `__init__`

In `AgentRuntime.__init__`, find this line (currently ~line 1703):

```python
        # A-4: Audit log for tool executions
        self._audit_log = AuditLog()
```

Immediately AFTER it (before the `# §0: Pluggable context management strategy` comment), add:

```python
        # Track-A: Tool middleware chain (enforcement + stuck detection).
        # Approval gating stays inline in _run_loop (temporal ordering:
        # must fire before on_tool_call_start). The chain wraps only the
        # execution phase. See spec §A.2.3-§A.2.4.
        self._tool_chain = ToolMiddlewareChain([
            EnforcementMiddleware(
                enforcement_check_fn=_enforcement_check,
                on_status=self._dispatch_enforcement_status,
            ),
            StuckDetectionMiddleware(
                stuck_check_fn=self._check_stuck,
                pending_messages=self._pending_stuck_messages,
            ),
        ])
```

**Key points:**
- `_enforcement_check` is the already-imported `check` function from `agent.enforcement` (verify the import alias at the top of runtime.py — grep for `_enforcement_check` to find the existing import). If it's imported as `check`, you may need to reference the existing alias. **Grep first, do not assume.**
- `self._dispatch_enforcement_status` is a new method (Edit 3).
- `self._check_stuck` is the existing method (line ~2909) — passed by reference, NOT called.
- `self._pending_stuck_messages` is already initialized at line ~1675.

### Edit 3: Add `_dispatch_enforcement_status` method

Add this new method to the `AgentRuntime` class (place it near `_dispatch`, around line 1717 — after `_dispatch` is defined, before `set_approval_callback` or another logical spot in the dispatch helpers section):

```python
    def _dispatch_enforcement_status(
        self, session_key: str, tool_name: str, status: dict
    ) -> None:
        """Dispatch a per-check enforcement status to the callback.

        Called by EnforcementMiddleware for each EnforcementCheck result.
        Wraps the existing _dispatch(self._on_enforcement_status, ...) pattern
        that was inline in _run_loop (spec §A.2.3).
        """
        self._dispatch(self._on_enforcement_status, session_key, tool_name, status)
```

### Edit 4: Replace inline enforcement + stuck blocks in `_run_loop` with chain call

Find this exact block in `_run_loop` (currently ~lines 2519-2561). The block starts with `result = execute_tool(...)` and ends right before `tc.mark_completed(...)`:

```python
                    result = execute_tool(tool_name, args, conv.project_path, session_key,
                                          approval_callback=per_call_cb,
                                          allowed_tools=conv.allowed_tools)
                    logger.debug("[tool-loop] sk=%s tool %s result: success=%s output_len=%d",
                                 session_key, tool_name, result.success, len(result.output or ""))

                    # === ENFORCEMENT LAYER HOOK ===
                    # Two-level gate: (1) global config enabled, (2) per-agent SI override
                    global_enabled = self._config.enforcement.enabled
                    agent_enabled = conv.si_enforcement if conv.si_enforcement is not None else True
                    if tool_name in ("write_file", "edit_file") and global_enabled and agent_enabled:
                        enf_result = _enforcement_check(
                            tool_name, args, result,
                            conv.project_path,
                            self._config.enforcement,
                        )
                        if enf_result.appended_message:
                            result = dataclasses.replace(
                                result,
                                output=(result.output or "") + "\n" + enf_result.appended_message,
                            )
                            for check in enf_result.checks:
                                self._dispatch(
                                    self._on_enforcement_status,
                                    session_key, tool_name,
                                    {
                                        "tier": check.tier,
                                        "file": check.file,
                                        "passed": check.passed,
                                        "detail": check.detail,
                                    },
                                )
                    # === END ENFORCEMENT HOOK ===

                    # §E: Stuck detection — record this tool call and check for loops
                    stuck_msg = self._check_stuck(session_key, tool_name, args, iteration)
                    if stuck_msg:
                        logger.warning("[stuck-detection] sk=%s: %s", session_key, stuck_msg)
                        # Phase CB-3: store as transient signal, NOT in conv.messages.
                        # The next LLM call will prepend it to the request's messages list.
                        # See SPEC-CONTEXT-BLOAT-PHASE-3.md §2.3 (BUG #4 fix).
                        self._pending_stuck_messages.setdefault(session_key, []).append(stuck_msg)
```

**Replace the ENTIRE block above** (from `result = execute_tool(...)` through the end of the stuck-detection block, stopping BEFORE `# Record tool result`) with:

```python
                    # Execute through the tool middleware chain.
                    # The chain wraps execute_tool with EnforcementMiddleware
                    # (post-write verification) and StuckDetectionMiddleware
                    # (loop detection). Approval was already resolved inline
                    # above (before on_tool_call_start) per spec §A.2.4.
                    ctx = ToolContext(
                        session_key=session_key,
                        project_path=conv.project_path,
                        iteration=iteration,
                        bypass_approval=bypass_approval,
                        audit_log=self._audit_log,
                        user_id=getattr(self._config, "user_id", ""),
                        enforcement_config=self._config.enforcement,
                        si_enforcement=conv.si_enforcement,
                    )
                    result = self._tool_chain.run(
                        tool_name=tool_name,
                        args=args,
                        ctx=ctx,
                        executor=lambda: execute_tool(
                            tool_name, args, conv.project_path, session_key,
                            approval_callback=per_call_cb,
                            allowed_tools=conv.allowed_tools,
                        ),
                    )
                    logger.debug("[tool-loop] sk=%s tool %s result: success=%s output_len=%d",
                                 session_key, tool_name, result.success, len(result.output or ""))
```

**Key points:**
- `bypass_approval` is the local variable computed in the approval gate above (already exists in the surrounding code). Do NOT recompute it.
- `iteration` is the loop variable from `_run_loop`. It's already in scope.
- The `executor` lambda wraps `execute_tool` with the same args as before.
- The `logger.debug` line is preserved (moved after the chain call).
- The `dataclasses` import is no longer needed in `_run_loop` for the enforcement block, but **do NOT remove the `import dataclasses`** at the top of the file — it may be used elsewhere. Grep before removing.

### Edit 5: Add 2 integration tests to tests/test_tool_middleware.py

Add a new test class `TestIntegration` at the end of `tests/test_tool_middleware.py` with these 2 tests (spec §A.5 cases 14-15):

**Test 14: `test_enforcement_fires_on_write_in_run_loop`**
- End-to-end: construct an `AgentRuntime` with a mock LLM that returns a `write_file` tool call, execute one loop iteration, verify enforcement check fires and the result output includes enforcement output.
- Use the existing test patterns in `tests/test_agent_runtime.py` (look at `TestApproval` or `TestStuckDetection` classes for how they construct a runtime with mocked providers).
- Key assertions: (a) the enforcement check function was called, (b) the tool result includes the appended enforcement message.

**Test 15: `test_stuck_detection_fires_in_run_loop`**
- End-to-end: construct an `AgentRuntime` with `_check_stuck` mocked to return a stuck message, send a message that triggers a tool call, verify the stuck message lands in `_pending_stuck_messages`.
- Key assertions: (a) `_check_stuck` was called with the correct args, (b) the stuck message is in `_pending_stuck_messages[session_key]`.

**If constructing a full AgentRuntime in a unit test is too heavy** (it requires GTK, provider config, etc.), use a lighter approach: test the chain wiring directly by constructing the same chain that `__init__` constructs and running it with a mock executor. This tests the wiring without the full runtime. If you take this approach, document it clearly in the test docstring.

## Verification commands (run yourself, paste output in COMPLETENESS)

```bash
# runtime.py compiles
python3 -c "from agent.runtime import AgentRuntime; print('import OK')"

# Chain is constructed in __init__
grep -n "self._tool_chain = ToolMiddlewareChain" agent/runtime.py

# _dispatch_enforcement_status method exists
grep -n "def _dispatch_enforcement_status" agent/runtime.py

# Old inline enforcement block is gone from _run_loop
grep -c "=== ENFORCEMENT LAYER HOOK ===" agent/runtime.py  # must be 0
grep -c "=== END ENFORCEMENT HOOK ===" agent/runtime.py    # must be 0

# Old inline stuck block is gone (the _check_stuck CALL, not the def)
# The call should now be inside StuckDetectionMiddleware, not inline in _run_loop
grep -n "stuck_msg = self._check_stuck" agent/runtime.py   # should be 0 matches in _run_loop
# (the def _check_stuck at ~2909 stays — it's the method, not the call)

# All existing agent runtime tests still pass (CRITICAL — no regressions)
python3 -m pytest tests/test_agent_runtime.py -q 2>&1 | tail -5

# All tool middleware tests pass (should be 19 + 2 new = 21)
python3 -m pytest tests/test_tool_middleware.py -q 2>&1 | tail -5

# runtime.py line count (should drop slightly — ~37 lines freed)
wc -l agent/runtime.py

# No collateral damage
git diff --name-only agent/tools.py agent/enforcement.py agent/context_strategy.py  # must be empty
```

## COMPLETENESS checklist (mandatory in your reply)

```
COMPLETENESS:
- [x/not done] Edit 1: 4 middleware imports added — evidence: grep output
- [x/not done] Edit 2: ToolMiddlewareChain constructed in __init__ — evidence: grep output
- [x/not done] Edit 3: _dispatch_enforcement_status method added — evidence: grep output
- [x/not done] Edit 4: inline enforcement + stuck blocks replaced with chain call — evidence: grep -c shows 0 for old markers
- [x/not done] Edit 5: 2 integration tests added — evidence: pytest count
- [x/not done] All existing test_agent_runtime.py tests pass (NO regressions) — evidence: pytest summary
- [x/not done] All tool_middleware tests pass — evidence: pytest summary
- [x/not done] runtime.py compiles + imports — evidence: python3 -c output
- [x/not done] No collateral damage to forbidden files — evidence: git diff output
- [x/not done] runtime.py line count — evidence: wc -l output
```

## Do NOT

- Do NOT modify the approval gate (the `if tool_name == "exec_command"` / sensitive-path block above the replaced section). It stays inline.
- Do NOT modify `agent/tools.py`, `agent/enforcement.py`, `agent/context_strategy.py`, or `tests/test_agent_runtime.py`.
- Do NOT remove the `import dataclasses` line at the top of runtime.py (grep for other uses first).
- Do NOT change `_call_llm_streaming`'s signature or any other method signature.
- Do NOT remove the `_check_stuck` method definition (line ~2909) — the middleware references it.
- Do NOT remove the `_pending_stuck_messages` initialization (line ~1675) — the middleware references it.
- Do NOT anchor to line numbers — use identifiers.

## When done

Reply with COMPLETENESS checklist + all verification command outputs pasted verbatim. The most critical evidence is **test_agent_runtime.py passing with zero regressions**.
