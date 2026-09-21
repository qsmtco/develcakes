# FIX: exec_command runs in scratch directory instead of project root

**Status:** ✅ IMPLEMENTED — exec_cwd = project_path at tools.py:412

When a special agent (Coder, Debugger) runs `exec_command`, the subprocess executes in the per-session scratch directory (`<project>/.crabcakes/tmp/special-coder/`) instead of the project root (`<project>/`). This was introduced as LOW-2 (per-session secure workspace).

The scratch directory is empty — it contains no source files. Every `grep`, `cat`, `pytest`, `git` command fails with "Exit 2" (file not found). The model doesn't know its shell CWD differs from `{{PROJECT_PATH}}` shown in the system prompt, so it burns iterations retrying with different syntax before eventually discovering it needs `cd <project_path> && ...` prefixes.

In the Coder's last conversation (84 messages, 36 tool calls), **~30 of 50 iterations were wasted** on Exit 2 errors from wrong CWD. This is the primary cause of "Max tool iterations reached" errors.

File tools (`read_file`, `write_file`, `edit_file`, `list_files`, `search_files`) are unaffected — they correctly use `project_path` as their sandbox base and ignore `scratch_dir`.

## Root Cause

1. `runtime.py:2001` — `_resolve_session_workspace()` creates a per-session dir and passes it as `scratch_dir`
2. `runtime.py:2005` — `scratch_dir=workspace` passed to `execute_tool()`
3. `tools.py:1222` — `execute_tool` injects `scratch_dir` into all tool handler kwargs
4. `tools.py:408` — `_exec_command` sets `exec_cwd = scratch_dir if scratch_dir else project_path`
5. Since `scratch_dir` is always provided for special agents, `exec_cwd` is always the scratch dir

## Goal

`exec_command` must run in `project_path`, not `scratch_dir`. The scratch directory (`_resolve_session_workspace`) remains available for future use cases (temp file isolation), but it must not override the exec CWD.

## Files to Read First (ALL completely)

1. `agent/tools.py` — focus on:
   - `_exec_command` (line ~380–430) — the `exec_cwd` selection at line 408
   - `execute_tool` (line ~1151–1250) — how `scratch_dir` is injected into kwargs at line 1222
   - The `exec_command` tool registration lambda (line ~963–964) — how `scratch_dir` flows through
2. `agent/runtime.py` — focus on:
   - `_resolve_session_workspace` (line ~1149–1185) — creates the scratch dir
   - The tool execution block (line ~1995–2010) — where `scratch_dir=workspace` is passed
3. `tests/test_low2_file_sandbox.py` — ALL tests. The file tool tests (BUG #1 regression) are correct and must still pass. No test currently asserts that exec_command uses scratch_dir as cwd — the tests only assert file tools ignore it.
4. `prompts/steelFramedCodeWriter.md` — the implementation prompt you MUST follow

## Implementation

### Edit 1: `_exec_command` — ignore `scratch_dir`, always use `project_path`

In `agent/tools.py`, line 407–408, change:

```python
    # Use scratch_dir if provided, otherwise fall back to project_path
    exec_cwd = scratch_dir if scratch_dir else project_path
```

to:

```python
    # exec_command always runs in project_path — the scratch_dir must NOT
    # override the CWD because the model expects commands to run in the project
    # root (as advertised by {{PROJECT_PATH}} in the system prompt).
    # scratch_dir is accepted as a parameter for API compatibility but ignored.
    exec_cwd = project_path
```

### Edit 2: Update `_exec_command` docstring

In `agent/tools.py`, lines 384–385, change:

```python
        scratch_dir: Per-session scratch directory for exec_command working directory.
            When provided, subprocess runs in scratch_dir instead of project_path.
```

to:

```python
        scratch_dir: Deprecated/ignored. Previously used as exec_command working directory.
            Now ignored — exec_command always runs in project_path. Retained for API
            compatibility; will be removed in a future cleanup.
```

### Edit 3: Update `execute_tool` docstring

In `agent/tools.py`, lines 1169–1172, change:

```python
        scratch_dir: Per-session scratch directory for exec_command working directory.
            File tools (read_file, write_file, edit_file, list_files, search_files)
            ignore this and always use project_path as their sandbox base.
            exec_command uses scratch_dir (or project_path if None) as cwd.
```

to:

```python
        scratch_dir: Deprecated/ignored. Previously used as exec_command cwd.
            All tools now use project_path. Retained for API compatibility.
```

### Edit 4: Update comment in `runtime.py`

In `agent/runtime.py`, lines 2003–2005, change:

```python
                    # project_path = sandbox base (file tools resolve relative paths here)
                    # scratch_dir = per-session workspace for exec_command cwd
                    result = execute_tool(tool_name, args, conv.project_path, session_key,
                                          approval_callback=per_call_cb, scratch_dir=workspace)
```

to:

```python
                    # project_path is the sandbox base for all tools AND exec_command cwd.
                    # scratch_dir (workspace) is resolved for future use but no longer
                    # overrides exec_command CWD — see exec-cwd-fix spec.
                    result = execute_tool(tool_name, args, conv.project_path, session_key,
                                          approval_callback=per_call_cb)
```

## What NOT to Change

- **Do NOT** remove `scratch_dir` parameter from `_exec_command`, `execute_tool`, or the exec_command lambda signature. API compatibility — callers (including tests) still pass it. Just ignore it inside.
- **Do NOT** remove `_resolve_session_workspace` or its call in `runtime.py`. The workspace dir is still created (may be used for temp files in the future). Just don't pass it to `execute_tool`.
- **Do NOT** modify `prompts/system/coder.md` or `project-awareness.md`. The system prompt already says `{{PROJECT_PATH}}` — with this fix, that's now accurate for exec_command too.
- **Do NOT** modify file tool tests in `tests/test_low2_file_sandbox.py`. They test that file tools ignore `scratch_dir` — still true and correct.

## Why Not Remove `scratch_dir` Entirely?

The parameter is threaded through multiple layers (`execute_tool` → lambda → `_exec_command`). Removing it requires changing all signatures, all test calls, and the lambda. Ignoring it is a 1-line behavioral change with zero call-site breakage. Full removal is a cleanup task for later.

## Verification

After all edits, run:

```bash
# Import check
python3 -c "import agent.tools; print('import OK')"

# Full test suite — all 83+ tests must pass
python3 -m pytest tests/ -q --tb=short -x

# Verify exec_cwd uses project_path
grep -n "exec_cwd" agent/tools.py
# Should show: exec_cwd = project_path  (NOT scratch_dir)

# Verify scratch_dir no longer passed in runtime.py
grep -n "scratch_dir" agent/runtime.py
# Should show NO scratch_dir= argument in the execute_tool call
```

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`
- Read ALL files listed above completely before making any edit
- Make ALL edits before running tests
- Report files changed with line numbers
- Include COMPLETENESS checklist at the end

## COMPLETENESS

At the end of your response, include:

```
COMPLETENESS:
- [x/not done] Edit 1: exec_cwd = project_path in tools.py — evidence (grep output)
- [x/not done] Edit 2: Updated _exec_command docstring — evidence
- [x/not done] Edit 3: Updated execute_tool docstring — evidence
- [x/not done] Edit 4: Updated runtime.py comment + removed scratch_dir= from execute_tool call — evidence (grep output)
- [x/not done] Import check passes — paste output
- [x/not done] All tests pass — paste output
```
