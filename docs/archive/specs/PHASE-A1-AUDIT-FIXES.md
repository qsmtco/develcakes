# Phase A1 Audit Fixes — 3 Bugs

**Source:** Debugger adversarial audit of Phase A1
**Spec constraint violated:** "Both middleware must catch exceptions from their delegated functions and return the original result (sad-path)." (supervisor brief + spec §A.5 sad-path cases 16-17)
**Rule reference:** `prompts/steelFramedCodeWriter.md` — apply every rule.
**Scope:** Edit `agent/tool_middleware.py` (3 fixes) and `tests/test_tool_middleware.py` (3 new regression tests). Do NOT touch any other file.

## Fix 1 + 2 (combined — same root cause): Widen try/except in EnforcementMiddleware

**BUG #1:** The `on_status` callback dispatch is not wrapped in try/except. If the callback raises, the exception propagates and crashes the tool loop.

**BUG #2:** Attribute access on `enf_result.appended_message` and `enf_result.checks` happens AFTER the `try/except` block that wraps `self._check(...)`. If `enforcement_check_fn` returns a malformed object (e.g., `None`), `AttributeError` propagates uncaught.

**Both fixes are the same edit:** widen the existing `try/except` in `EnforcementMiddleware.__call__` to cover the entire result-processing block (call + attribute access + replace + callback dispatch).

**Current code (agent/tool_middleware.py, in `EnforcementMiddleware.__call__`):**
```python
        try:
            enf_result = self._check(
                tool_name, args, result,
                ctx.project_path,
                ctx.enforcement_config,
            )
        except Exception:
            logger.exception(
                "Enforcement check failed for %s (session=%s):",
                tool_name, ctx.session_key,
            )
            return result

        if enf_result.appended_message:
            result = dataclasses.replace(
                result,
                output=(result.output or "") + "\n" + enf_result.appended_message,
            )
            if self._on_status is not None:
                for check_record in enf_result.checks:
                    self._on_status(ctx.session_key, tool_name, {
                        "tier": check_record.tier,
                        "file": check_record.file,
                        "passed": check_record.passed,
                        "detail": check_record.detail,
                    })

        return result
```

**Required code (widen the try/except to cover result processing + callback dispatch):**
```python
        try:
            enf_result = self._check(
                tool_name, args, result,
                ctx.project_path,
                ctx.enforcement_config,
            )
            if enf_result.appended_message:
                result = dataclasses.replace(
                    result,
                    output=(result.output or "") + "\n" + enf_result.appended_message,
                )
                if self._on_status is not None:
                    for check_record in enf_result.checks:
                        self._on_status(ctx.session_key, tool_name, {
                            "tier": check_record.tier,
                            "file": check_record.file,
                            "passed": check_record.passed,
                            "detail": check_record.detail,
                        })
        except Exception:
            logger.exception(
                "Enforcement check failed for %s (session=%s):",
                tool_name, ctx.session_key,
            )
            return result

        return result
```

**Key points:**
- The `if enf_result.appended_message:` block and everything inside it moves INTO the `try`.
- On ANY exception (from `_check`, from attribute access on a malformed return, from `dataclasses.replace`, or from `on_status` callback), log and return the ORIGINAL `result` (the one from `next()`, before any `dataclasses.replace`).
- Keep the existing log message format (`"Enforcement check failed for %s (session=%s):", tool_name, ctx.session_key`) — it is correct.

## Fix 3: StuckDetectionMiddleware log format string

**BUG #3:** The log format string passes `ctx.session_key` for the first `%s` slot ("for X") which should be the tool name. The session key then appears twice in the rendered message.

**Current code (agent/tool_middleware.py, in `StuckDetectionMiddleware.__call__`):**
```python
        except Exception:
            logger.exception(
                "Stuck check failed for %s (session=%s, tool=%s):",
                ctx.session_key, ctx.session_key, tool_name,
            )
            return result
```

**Required code (match the EnforcementMiddleware pattern — tool name first, session second, drop redundant slot):**
```python
        except Exception:
            logger.exception(
                "Stuck check failed for %s (session=%s):",
                tool_name, ctx.session_key,
            )
            return result
```

## Regression tests to add (3 new tests in tests/test_tool_middleware.py)

Add these to the `TestSadPath` class (or a new `TestAuditFixes` class — your choice, but keep them grouped):

**Test for BUG #1 — `on_status` callback raises:**
```python
def test_enforcement_on_status_callback_raises_does_not_crash_loop(self):
    """BUG #1 regression: on_status callback exception must be caught."""
    enf_check = MagicMock(return_value=EnforcementResult(
        checks=[EnforcementCheck(tier="syntax", tool="write_file", file="x.py",
                                  passed=True, detail="OK", output="", duration_ms=1)],
        appended_message="Syntax: OK",
    ))
    def bad_callback(*a, **kw):
        raise RuntimeError("UI disconnected")
    mw = EnforcementMiddleware(enf_check, on_status=bad_callback)
    executor = MagicMock(return_value=ToolResult(success=True, output="wrote 42 bytes"))
    ctx = ToolContext(
        session_key="special:coder", project_path="/p", iteration=0,
        enforcement_config=_make_enf_config(),  # reuse your existing helper
    )
    # Must NOT raise — must return a result
    result = mw("write_file", {"path": "x.py"}, ctx, executor)
    assert result.success is True
    # Original output preserved (the dataclasses.replace may or may not have run
    # before the callback raised, so just assert success and no crash)
```

**Test for BUG #2 — `enforcement_check_fn` returns malformed object:**
```python
def test_enforcement_malformed_return_does_not_crash_loop(self):
    """BUG #2 regression: malformed enforcement_check_fn return must be caught."""
    enf_check = MagicMock(return_value=None)  # malformed — no .appended_message
    mw = EnforcementMiddleware(enf_check)
    executor = MagicMock(return_value=ToolResult(success=True, output="wrote 42 bytes"))
    ctx = ToolContext(
        session_key="special:coder", project_path="/p", iteration=0,
        enforcement_config=_make_enf_config(),
    )
    # Must NOT raise — must return the original result unchanged
    result = mw("write_file", {"path": "x.py"}, ctx, executor)
    assert result.success is True
    assert result.output == "wrote 42 bytes"
```

**Test for BUG #3 — log format string correctness:**
```python
def test_stuck_check_log_format_string_correct(self):
    """BUG #3 regression: log message uses tool_name for 'for %s' slot, not session_key."""
    stuck_check = MagicMock(side_effect=ValueError("boom"))
    mw = StuckDetectionMiddleware(stuck_check, {})
    executor = MagicMock(return_value=ToolResult(success=True, output="ok"))
    ctx = ToolContext(session_key="special:coder", project_path="/p", iteration=0)
    with patch("agent.tool_middleware.logger.exception") as mock_exc:
        mw("read_file", {"path": "x.py"}, ctx, executor)
        mock_exc.assert_called_once()
        # First positional arg is the format string; second is the first %s value.
        # It must be tool_name ("read_file"), NOT session_key ("special:coder").
        call_args = mock_exc.call_args
        format_str = call_args[0][0]
        first_arg = call_args[0][1]
        assert first_arg == "read_file", f"Expected tool_name 'read_file', got {first_arg!r}"
        assert "special:coder" in format_str or call_args[0][2] == "special:coder"
```

## Verification commands (run yourself, paste output in COMPLETENESS)

```bash
# All tests pass (should now be 19 — 16 original + 3 new regression tests)
python3 -m pytest tests/test_tool_middleware.py -v

# Confirm the 3 bugs are fixed by grepping the source
grep -A2 "Stuck check failed for" agent/tool_middleware.py  # must show tool_name, ctx.session_key
grep -B1 -A15 "enf_result = self._check" agent/tool_middleware.py  # try/except must cover the if-block

# Confirm no collateral damage to other files
git diff --name-only agent/runtime.py agent/tools.py agent/enforcement.py  # must be empty
```

## COMPLETENESS checklist (mandatory in your reply)

```
COMPLETENESS:
- [x/not done] Fix 1+2: EnforcementMiddleware try/except widened to cover result processing + callback dispatch — evidence: grep output showing the if-block inside try
- [x/not done] Fix 3: StuckDetectionMiddleware log format fixed (tool_name, ctx.session_key) — evidence: grep output
- [x/not done] Test for BUG #1 (on_status raises) added + passes — evidence: pytest output
- [x/not done] Test for BUG #2 (malformed return) added + passes — evidence: pytest output
- [x/not done] Test for BUG #3 (log format) added + passes — evidence: pytest output
- [x/not done] All 19 tests pass (16 original + 3 new) — evidence: pytest summary line
- [x/not done] No collateral damage — evidence: git diff --name-only output for forbidden files
```

## Do NOT

- Do NOT modify any file other than `agent/tool_middleware.py` and `tests/test_tool_middleware.py`.
- Do NOT change the public API (class names, method signatures, dataclass fields).
- Do NOT change the happy-path behavior — only the error-handling scope and the log string.
- Do NOT reformat or refactor unrelated code.

## When done

Reply with COMPLETENESS checklist + all verification command outputs pasted verbatim.
