# Add Chain-Invocation Test — Phase A2 Coverage Gap

**Scope:** Add ONE test to `tests/test_tool_middleware.py` (the `TestIntegration` class). Do NOT touch any other file.
**Rule reference:** `prompts/steelFramedCodeWriter.md` — apply every rule.

## Objective

The two existing `TestIntegration` tests construct their own `ToolMiddlewareChain` directly. They do NOT prove that `AgentRuntime._run_loop` actually calls `self._tool_chain.run(...)`. This is why two silent reverts of Edit 4 went undetected. Add a test that constructs a real `AgentRuntime`, runs `_run_loop` with a mock LLM that returns a tool call, and asserts the chain was invoked.

## CRITICAL: Read these first

1. `prompts/steelFramedCodeWriter.md` (your standing orders)
2. `tests/test_agent_runtime.py` lines 30-75 and 206-240 — the `_resp()` and `_make_cfg()` helpers (USE THESE EXACT HELPERS — copy them into your test or import them)
3. `tests/test_agent_runtime.py` lines 224-250 — the `TestToolLoop::test_user_plus_assistant_in_conversation` pattern (this is how you construct a runtime and run `_run_loop` with a mocked `_call_llm`)
4. `agent/runtime.py` lines 1700-1725 — the `__init__` chain construction (so you know what to assert against)

## Deliverable

Add this test to the `TestIntegration` class in `tests/test_tool_middleware.py`:

```python
def test_run_loop_invokes_tool_chain(self):
    """Regression guard: _run_loop must call self._tool_chain.run(...).

    Without this test, a silent revert of Edit 4 (replacing the chain
    call with the old inline blocks) is undetectable — the existing
    TestIntegration tests construct their own chain and bypass
    AgentRuntime entirely. This test constructs a real AgentRuntime,
    runs _run_loop with a mock LLM that returns a write_file tool call,
    and asserts the chain's run() method was invoked.
    """
    from agent.config import AgentConfig, LLMProviderConfig
    from agent.runtime import AgentRuntime

    # Build a minimal config (mirror _make_cfg from test_agent_runtime.py)
    cfg = AgentConfig(
        providers={
            "openai": LLMProviderConfig(
                name="openai",
                base_url="https://api.openai.com/v1",
                api_key="test-key",
                default_model="gpt-4o",
            )
        },
        default_provider="openai",
        default_model="openai/gpt-4o",
        max_tool_iterations=5,
        tool_timeout_seconds=30,
        auto_save_conversations=False,
    )
    rt = AgentRuntime(cfg)
    rt.start()
    sk = f"test-{uuid.uuid4().hex[:8]}"
    rt.create_conversation("Coder", sk, "/tmp")

    # Mock _call_llm to return a write_file tool call, then "Done."
    responses = [
        {
            "choices": [{"message": {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path": "/tmp/test_out.txt", "content": "hello"}',
                    },
                }],
            }}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        },
        {
            "choices": [{"message": {"content": "Done."}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5},
        },
    ]
    with unittest.mock.patch.object(rt, "_call_llm", lambda sk2, msgs, tools: responses.pop(0)):
        # Patch the chain's run method to track invocation
        original_run = rt._tool_chain.run
        chain_calls = []
        def tracking_run(*args, **kwargs):
            chain_calls.append(kwargs.get("tool_name", args[0] if args else None))
            return original_run(*args, **kwargs)
        rt._tool_chain.run = tracking_run

        rt._run_loop(sk, "write a file")

    # THE CRITICAL ASSERTION: the chain was invoked at least once
    assert len(chain_calls) >= 1, (
        f"Expected _tool_chain.run to be called during _run_loop, "
        f"but it was never invoked. chain_calls={chain_calls}"
    )
    # And specifically for a write_file tool call
    assert "write_file" in chain_calls, (
        f"Expected write_file in chain calls, got {chain_calls}"
    )
    rt.stop()
```

**Key points:**
- This test constructs a REAL `AgentRuntime` and runs `_run_loop`. It does not construct its own chain.
- It wraps `rt._tool_chain.run` with a tracking function that records calls but delegates to the original.
- The assertion `assert len(chain_calls) >= 1` is the regression guard. If Edit 4 is reverted (inline blocks replace the chain call), this test FAILS because `_tool_chain.run` is never called.
- Use `unittest.mock.patch.object(rt, "_call_llm", ...)` to mock the LLM — this is the established pattern in `test_agent_runtime.py`.
- `uuid` and `unittest.mock` are already imported at the top of `tests/test_tool_middleware.py` (verify this; if not, add the imports).

## Verification commands

```bash
# New test passes on current (wired) code
python3 -m pytest tests/test_tool_middleware.py::TestIntegration::test_run_loop_invokes_tool_chain -v

# All tool middleware tests pass (should be 23 now)
python3 -m pytest tests/test_tool_middleware.py -q

# Confirm the test would FAIL on reverted code:
# (the supervisor will verify this by temporarily reverting Edit 4 and running the test)
```

## COMPLETENESS checklist (mandatory in your reply)

```
COMPLETENESS:
- [x/not done] test_run_loop_invokes_tool_chain added to TestIntegration class — evidence: pytest -v output
- [x/not done] Test passes on current (wired) code — evidence: PASSED
- [x/not done] All tool_middleware tests pass (23 total) — evidence: pytest summary
- [x/not done] No other files modified — evidence: git diff --name-only
```

## Do NOT

- Do NOT modify `agent/runtime.py`, `agent/tool_middleware.py`, or any file other than `tests/test_tool_middleware.py`.
- Do NOT change the existing 22 tests.
- Do NOT import from `tests/test_agent_runtime.py` (copy the helper pattern, don't cross-import test files).

## When done

Reply with COMPLETENESS checklist + raw verification output.
