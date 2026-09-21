# Phase 1 Instructions — Runtime File Sandbox (LOW-2)

**Phase:** 1 of 5
**Finding (original review):** LOW-2 — File tools default sandbox to `/tmp`
**Master spec:** `docs/specs/SPEC-LOW-FOLLOWUP-PHASE-4.md` §4.1
**Authority chain:** Captain → `docs/ARCHITECTURE.md` → spec → this file → code

---

## READ FIRST

1. **Read the master spec** — `docs/specs/SPEC-LOW-FOLLOWUP-PHASE-4.md` §4.1 in full
2. **Read these files in full** before editing:
   - `agent/runtime.py` (especially lines 1150-1250 and 1700-1740)
   - `agent/tools.py` (look for `execute_tool` signature, the `tmp` parameter)
   - `tests/test_tools.py` (existing test patterns for tools)
3. **Use the steelFramedCodeWriter prompt** at `prompts/steelFramedCodeWriter.md`

## Edits to make (1 file)

### `agent/runtime.py`

**LOW-2 — file sandbox**

Find both call sites at lines 1722 and 1736 (pattern: `conv.project_path or "/tmp"`). Both should be replaced with a helper that:
- if `conv.project_path` is empty, RAISE (do NOT fall back to `/tmp`)
- if `conv.project_path` is set, use `<project_path>/.crabcakes/tmp/<session_key>/` as the `project_path` arg to `execute_tool`
- the temp dir is created with `0o700` permissions if it doesn't exist (idempotent)
- the path is stored on the `Conversation` object so subsequent calls in the same session reuse the same temp dir

Suggested new helper (place near the top of the file, after imports, before `class AgentRuntime`):

```python
def _resolve_session_workspace(project_path: str | None, session_key: str) -> str:
    """Return a per-session secure workspace under the project's .crabcakes/ dir.

    LOW-2: never fall back to /tmp — raise if project_path is empty.
    Returns a path that has been created with 0o700 permissions.
    """
    if not project_path:
        raise ValueError(
            f"LOW-2: project_path is empty for session {session_key!r}; "
            "refusing to use a world-writable default"
        )
    workspace = os.path.join(project_path, ".crabcakes", "tmp", session_key)
    os.makedirs(workspace, mode=0o700, exist_ok=True)
    return workspace
```

Then in the tool-execution path (line 1722 and 1736):

```python
# LOW-2: never fall back to /tmp — use per-session workspace or raise
if conv.project_path:
    workspace = _resolve_session_workspace(conv.project_path, session_key)
else:
    raise RuntimeError(
        f"LOW-2: cannot execute tool {tool_name!r} without a project_path "
        f"(session {session_key!r})"
    )
result = execute_tool(tool_name, args, workspace, session_key, approval_callback=per_call_cb)
```

**Note:** `agent/tools.py` already accepts a `project_path` parameter that is used as the base for `read_file`/`write_file`/`edit_file`. By passing the secure workspace, all relative paths inside tools will be resolved under that directory, not `/tmp`.

**Conftest/test impact:** Some existing tests in `tests/test_tools.py` and `tests/test_agent_runtime.py` may call `execute_tool` with no `project_path` (relying on the `/tmp` fallback). After this change, those tests will need a `project_path` argument or a `pytest.raises(ValueError)` assertion. **Do not change existing tests' expectations to make them pass** — instead, update them to pass a valid `project_path` or to assert the new `ValueError`. The supervisor will verify this in the audit.

## Tests to add

Add to `tests/test_tools.py` (or new file `tests/test_low2_file_sandbox.py`):

1. `test_low2_empty_project_path_raises` — call `execute_tool("write_file", {"path":"x","content":"y"}, "", "session_x")` and assert `ValueError` with message containing "LOW-2".
2. `test_low2_workspace_under_project` — call with a tmp_path-based project, write a file, assert it lands under `<project>/.crabcakes/tmp/<session_key>/`, NOT under `/tmp`.
3. `test_low2_workspace_0o700` — assert the created workspace dir has mode `0o700` (or `0o700 & 0o777` to mask out type bits).
4. `test_low2_idempotent_workspace` — call `_resolve_session_workspace` twice with the same `session_key`; assert both return the same path.
5. `test_low2_does_not_write_to_tmp` — call with a project_path, write a file, assert `/tmp` directory listing does NOT gain a new entry from the test (use a marker file in a temp `/tmp/foo-LOW2-test-xyz` location that the test cleans up).

## Verification commands

Run these and paste the output in your completion report:

```bash
# 1. Confirm /tmp is no longer a fallback
git grep -n 'project_path or "/tmp"' agent/runtime.py
# Expected: no output

# 2. Run new tests
python -m pytest tests/test_tools.py tests/test_low2_file_sandbox.py -v 2>&1 | tail -40
# Expected: all pass, including the 5 new tests

# 3. Confirm the existing test suite still passes (LOW-2 may break tests that relied on /tmp)
python -m pytest tests/ -x --ignore=tests/test_agent_runtime.py -q 2>&1 | tail -30
# If a test fails, you must update it to pass a project_path or assert ValueError.
# Do not silently skip.
```

## COMPLETENESS checklist (return this in your reply)

```
COMPLETENESS:
- [x/not done] Edit 1: agent/runtime.py — added _resolve_session_workspace helper — evidence: <file:line>
- [x/not done] Edit 2: agent/runtime.py — replaced both `or "/tmp"` fallbacks with helper call — evidence: <git grep output>
- [x/not done] Test 1: test_low2_empty_project_path_raises — evidence: <pytest line>
- [x/not done] Test 2: test_low2_workspace_under_project — evidence: <pytest line>
- [x/not done] Test 3: test_low2_workspace_0o700 — evidence: <pytest line>
- [x/not done] Test 4: test_low2_idempotent_workspace — evidence: <pytest line>
- [x/not done] Test 5: test_low2_does_not_write_to_tmp — evidence: <pytest line>

RELATED ISSUES FOUND (do not silently fix — report them):
- <any related bugs you noticed while editing this file>

VERIFICATION OUTPUT:
<paste full pytest output, both new and existing>
```

## Word marker for acknowledgment

Include the phrase "please proceed" in your reply so the channel knows the message is canonical.

## Important reminders

- **Read every file before editing it.** Do not assume what's in `agent/tools.py` — open it and check the `execute_tool` signature.
- **Do not skip the COMPLETENESS checklist.** The supervisor will send the work back if it is missing.
- **Flag related issues, do not silently fix them.** Use the "related-bug scan" pattern from the steelFramedCodeWriter prompt.
- **Do not change tests to make them pass.** If existing tests rely on `/tmp`, update them to use a `tmp_path` fixture (pytest builtin) or to assert the new `ValueError`.
