# SPEC-43: Cut the coder harness so a turn writes

**Date:** 2026-10-19
**Author:** Micah
**Status:** Draft — for implementation
**Implements:** PM decision 2026-10-19 — stop the context-efficiency program; make the Coder write
**Depends on:** none of SPEC-23 through SPEC-41 are extended. This spec does not add a guard to that stack.
**Target branch:** main
**Baseline:** HEAD `92f6d971`. Line numbers below were read from that tree. `agent/runtime.py`, `agent/tools.py`, `utils/prompt_loader.py`, and `prompts/system/coder.md` are unchanged in the working tree at discovery.

> Architecture: `agent/` owns the turn engine (` .crabcakes/architecture.md`, "agent/ — the engine"). This spec trims what the Coder is told and what its tools return. The send path stays `AgentRuntime._run_loop`. Supervisor and Debugger keep today's prompt composition, repo map, shell, and enforcement tiers.

---

## DISCOVERY

Read before any sample below was written.

- Read `utils/prompt_loader.py` `compose_system_prompt` (`148`): parts are always `default`, `collab`, `crabcakes-context`, `agent-tools`, then (when `project_path` is set) `project-awareness` and `crabcakes-commands`, then exactly one of `coder` / `debugger` / `supervisor`, then the trusted bug journal and project rules. `fill_template` (`59`) replaces `{{KEY}}` and strips unknown keys. After fill, a project path appends `build_file_context_with_core_files` (`342-352`). There is no role branch that skips that append.
- Read `prompts/system/coder.md` (123 lines). Principle 1 is "Read before write." The starting-a-task list tells the model to read `.crabcakes/context.md`, architecture, and every file it will modify. Step 4 says do not run tests. The tool-strategy line says `exec_command` is not for file I/O. `prompts/system/default.md` (14 lines) also says "read files before answering questions." `prompts/system/supervisor.md` (56 lines) says "Plan then delegate" and "Read before you act." It does not constrain the shape of an `/ask` payload.
- Read `agent/context.py` `resolve_context_mode` (`632`) and `build_file_context_with_core_files` (`861`). `auto` plus a window above 128_000 returns `"hybrid"`. Hybrid emits core-file stubs, `build_file_index`, and prompt names. No file bodies. The live Coder provider window is 1_000_000, so the Coder prompt gets that index.
- Read `agent/runtime.py` `REPO_MAP_HEADER` (`319`), `_repo_map_block_for` (`3450`), `_prefix_repo_map` (`3477`), `_messages_for_model` (`3670`). Every model call builds a map from the latest user message and prefixes it onto that message, and onto the compiled packet's fact lines. The header text is "open the file for the body". `Conversation.agent_role` is `models/conversation.py:258`, default `""`.
- Read `agent/tools.py` `_read_file` (`435`). `MAX_READ_SIZE` is 16 * 1024 (`270`). `MAX_READ_LINES` is 2000 (`277`). A `line` argument seeks with `_start_byte_for_line` (`407`) and then still applies the byte cap. When the cap stops the read, the output appends `continue with offset={next_offset}` and, in line mode, ` or line={next_line}` (`506-526`). Caught exceptions, all returned as `ToolResult(success=False)`: `PermissionError`, `UnicodeDecodeError`, `OSError`. No new exception type is introduced by the line-window change, because it stays inside that `try`.
- Read `tests/test_tools.py` `test_read_continuation_hint_after_mid_line_window` (`312`) and `test_read_continuation_hint_after_newline_terminated_window` (`338`). Both call `read_file` with `line: 1` and no `limit`, and both assert `or line=` plus `offset=`. Those assertions describe the behavior this spec removes for line mode.
- Read `agent/tools.py` `_search_files` (`954`). `preview_lines` is clamped with `min(preview_lines, 5)` (`977`). Hits are parsed as `path:lineno:content` via `split(":", 2)` (`1054-1064`). The footer is `[Use read_file("path") for the file body.]` (`1099`). `grep -C` is not used. Adding it would break that split. This spec does not add `-C`.
- Read `agent/tools.py` `_exec_command` (`690`) and `_is_blocked` (`364`). The blocklist runs first and returns `ToolResult(success=False, error="Command blocked by safety policy: ...")`. Approval is `_get_approval` (`710`). The process is `subprocess.Popen(..., shell=True)` (`732`). `shell=True` is why an allowlist has to reject metacharacters, not trust a leading word.
- Read `agent/runtime.py` `_execute_tool_batch` (`2273`). The loop is sequential. `exec_command` hits `_block_file_reprint` (`2318`) before `_dispatch_approval` (`2345`). `_REPRINT_COMMANDS` (`383`) is `cat`, `sed`, `head`, `tail`, `nl`, `less`, `bat`. `grep` and `awk` are absent. A refusal there calls `tc.mark_failed`, `conv.add_tool_result`, `_dispatch(_on_tool_call_result)`, and `continue`. The Coder allowlist uses that same refusal shape, and it runs before approval so the PM is not asked.
- A hard stop after eight `read_file` calls was drafted and then dropped (PM, 2026-10-19, after Claude Code and SWE-agent). Those harnesses do not refuse the next read. They return a line window and keep the shell from being the reader. This spec does not add `_reads_since_write` or `_READ_BUDGET`. `_read_counts` (`673`) stays as it is.
- Read `agent/config.py` `EnforcementConfig` (`51`), a dataclass, defaults `syntax_check=True`, `test_run=True`, `lint_check=True`. Read `agent/enforcement.py` `check` (`1542`): it returns `EnforcementResult()` immediately unless `tool_name` is `write_file` or `edit_file` and `tool_result.success`. Tiers run only when the matching flag is true (`1604`, `1630`, `1636`). `dataclasses.replace` is already used in that function (`1593`). `agent/runtime.py` builds `ToolContext` at `2419` with `enforcement_config=self._config.enforcement`. Replacing that object for the Coder does not mutate the runtime config.
- Read `agent/runtime.py` `_compact_before_llm` (`3926`) and `_compute_landing_goal` (`1628`). Over the soft ceiling it calls `panic_prune` then `compact` toward `min(15% of the window, 24_000)`. Under the ceiling, `compact(conv, soft_ceiling)` returns from `prune_tool_outputs` when the estimate is already under the target (`context_strategy.py` `403`). This spec does not change that path. Deleting it is a later spec, after a Coder turn writes.

Architecture owner: `AgentRuntime` owns the wire list and the tool batch. `compose_system_prompt` owns the system prompt text. `agent/tools.py` owns `read_file` and `search_files` bytes.

Existing pattern to copy: a refused tool in `_execute_tool_batch` is a failed `ToolCall` plus a tool result. It is not a raised exception. See `_block_file_reprint`'s call site at `2318-2327`.

---

## 1. Overview

### Problem

The Coder spends the turn reading and shelling because the prompt, the read tool, and the shell disagree.

The prompt says read everything before writing. The read tool returns 16KB and then tells the model the next offset. The shell can slice a function, and the reprint denylist does not include `grep` or `awk`. A turn is allowed 50 iterations (`AgentConfig.max_tool_iterations`, default 50, `agent/config.py:83`). Nothing counts reads. The repo map and the hybrid file index both tell the model to open files.

The context window is not full on these turns. Compaction under the soft ceiling does not stub. The fix is not another mask.

### Solution

Six changes, in the order in section 5. All of them are Coder-only except the line window and the search preview, which are properties of the tools.

1. Replace `prompts/system/coder.md` with a work order.
2. When `agent_role == "coder"`, compose only that file plus the tool list. Do not append the shared templates or the file index.
3. When `agent_role == "coder"`, pass an empty repo-map block into the wire list.
4. A `read_file` call with `line` set and `limit` omitted returns at most 200 lines and does not append a continuation notice.
5. Raise `search_files` preview from 5 matching lines to 40. Do not add `grep -C`.
6. For the Coder only: allowlist `exec_command`, and run syntax check only (no lint tier, no test tier) on that session's writes. There is no cap on how many times `read_file` may be called.

### Scope

| In | Out |
|---|---|
| Coder system prompt and Coder composition branch | Debugger and Supervisor composition |
| Coder repo-map skip | Deleting `agent/repo_map.py` |
| Line window and search preview | `grep -C` or a new parser |
| Coder shell allowlist | Changing `_is_blocked` or `shell=True`. A cap on `read_file` calls |
| Coder enforcement: syntax on, lint and tests off | Editing `EnforcementConfig` defaults |
| Tests named in section 5 | Deleting compiled packets, hot windows, dedup, reprint block, pins, or the landing goal |

The deferred deletions stay deferred on purpose. They are the machinery that tries to restrain this loop. Pull them out in a later spec after a Coder turn writes inside the first handful of calls. Doing both in one change makes the result unreadable.

### Architecture principles

`agent/runtime.py` remains the only send path. No new package. No new tool name. `edit_file` and `write_file` stay the write tools.

---

## 2. Changes by File

### 2.1 `prompts/system/coder.md` — replace the file

Delete the current 123 lines. Write this file, and nothing else, in its place. `{{AGENT_NAME}}` and `{{TOOL_LIST}}` are the only placeholders. `fill_template` strips any other `{{KEY}}`.

```markdown
You are {{AGENT_NAME}}. You edit code in this project.

{{TOOL_LIST}}

## Work order

The user message names the files and the behavior. Do that.

1. Read each file you will change, once, with `read_file` and `line` set to the function you will edit.
2. Change it with `edit_file`. Use `write_file` only for a new file.
3. Syntax-check the files you changed: `.venv/bin/python -m py_compile <path>`.
4. Report what changed. Stop.

`search_files` is how you find a symbol. The matching lines are the result. Open a file only when you are about to edit it.

Do not explore the repository. Do not read a file you will not edit. Do not use the shell to read files. `grep`, `awk`, `sed`, and `cat` are refused. `git status`, `git diff`, `git log`, `py_compile`, and `pytest` on one path under `tests/` are allowed.
```

`test_coder_template_included` in `tests/test_prompt_loader.py` (`118`) asserts `"Core Principles" in prompt or "software" in prompt.lower()`. That phrase is gone. Change the assertion to `"Read each file you will change" in prompt` (work-order step 1). Keep `assert "Coder" in prompt` only if `agent_name="Coder"` is still passed; `"You are Coder"` satisfies it via `{{AGENT_NAME}}`.

### 2.2 `prompts/system/supervisor.md` — one operating rule

Insert this bullet after "Plan then delegate." (`29-31`). Do not rewrite the rest of the file.

```markdown
- **Delegate a task, not an investigation.** An `/ask` to Coder names the file, the behavior, and how you will know it is done. Do not include a grep plan, a call-count tripwire, or a pytest invocation.
```

`tests/test_prompt_loader.py` `test_supervisor_unonboarded_gets_both_prompts` (`132`) asserts `"orchestrator" in prompt.lower()`. That word stays in the role paragraph. No change to that assertion.

### 2.3 `utils/prompt_loader.py` — Coder composition is the work order only

Inside `compose_system_prompt`, after `awareness = project_awareness or {}` and before the "1. Always load default" comment (`194`), add:

```python
if agent_role == "coder":
    coder_only = load_prompt_template("coder")
    if not coder_only:
        _logger.warning("coder template missing; Coder prompt is empty")
        return ""
    if tools:
        tool_list_str = "## Tools\n" + "\n".join(f"  - {t}" for t in tools)
    else:
        tool_list_str = ""
    variables = {
        "AGENT_NAME": agent_name or "",
        "TOOL_LIST": tool_list_str,
    }
    return fill_template(coder_only, variables)
```

Return value: the filled coder template. Callers of `compose_system_prompt` already treat `""` as "fall through" only inside `agent/context.py` `build_system_prompt` (`1083`), which then builds a one-line fallback. An empty return here means the coder file failed to load. That is the same as today's missing-template path (`298-300`). Do not fall through into the shared templates when the role is coder and the file loaded.

This branch does not call `build_file_context_with_core_files`. It does not load `collab`, `crabcakes-context`, `agent-tools`, `project-awareness`, `crabcakes-commands`, the bug journal, or project rules. Those stay on the existing path for every other role.

`context_mode` is ignored on this branch. That is intentional. The Coder prompt does not grow a file index in preload, jit, or hybrid.

`test_tools_included` (`tests/test_prompt_loader.py:197`) calls `compose_system_prompt(agent_name="Coder", tools=[...])` with the default `agent_role=""`. It still hits the shared path. Leave it. Add a test that passes `agent_role="coder"` and asserts the tool name is present and that `"Phase History" not in prompt` and `"File index" not in prompt` and `"suggest the next phase" not in prompt`.

`test_coder_no_longer_gets_onboarding` (`154`) still passes: the early return never loads onboarding.

These tests call `compose_system_prompt` or `build_system_prompt` with the Coder role and then assert on the file index. They fail if left as they are, because the early return never builds an index. Move the index assertions onto `agent_role="debugger"`. Do not delete the mode coverage.

- `tests/test_jit_context_discovery.py` `test_compose_prompt_jit_mode_produces_smaller_prompt` (`573`): both calls pass `agent_role="coder"`, then assert `len(jit) < len(preload)`. After the early return both strings are the work order, so the lengths are equal. Change both calls to `agent_role="debugger"`. Debugger still takes the shared path, so jit stays smaller than preload.
- `test_compose_prompt_hybrid_mode_stubs_core_files` (`596`): asserts `"File index" in prompt` and `use read_file("README.md")` for `agent_role="coder"`. Change to `agent_role="debugger"`.
- `test_compose_prompt_accepts_auto_mode` (`610`): only asserts `"Coder" in prompt`. It still passes on the early return. Leave the role. `"You are Coder"` is in the work order.
- `test_build_system_prompt_forwards_context_mode` (`634`): `build_system_prompt("Coder", ...)` passes `agent_role=""`, and `build_system_prompt` (`agent/context.py:1071-1074`) derives `"coder"` from the name. The early return then drops the index, so `assert "File index" in prompt` (`646`) fails. Call it as `build_system_prompt("Debugger", ..., agent_role="debugger", ...)`. The derived-role line is unchanged.

`build_file_context_with_core_files` tests in that file (`TestBuildFileContextModes`, `290`) do not go through `compose_system_prompt`. Leave them.

### 2.4 `agent/runtime.py` — no repo map for the Coder

In `_messages_for_model`, the line `block = self._repo_map_block_for(session_key, conv)` (`3692`) stays. Immediately after it:

```python
if getattr(conv, "agent_role", "") == "coder":
    block = ""
```

`block == ""` makes `_prefix_repo_map` return the messages unchanged (`3478-3479`) and makes `_compiled_messages` skip `fact_lines` (`3511-3513`). The compiled packet, when one exists, still replays the hunk. This spec does not delete `_compile`.

`_repo_map_block_for` still runs for the Coder and still walks the tree. That cost is accepted in this spec. Skipping the call entirely is a one-line follow-up only if the walk shows up in a profile. Do not do it here: the empty-string assignment is the behavior, and a skipped call is easy to get wrong on the compiled branch.

No change to `REPO_MAP_HEADER`. `tests/test_repo_map.py` and `tests/test_repo_map_v2.py` build maps without `agent_role == "coder"`. They stay.

Add one test: a conversation with `agent_role="coder"` and a non-empty map block produces a wire list whose user content does not contain `REPO_MAP_HEADER`. A conversation with `agent_role=""` still gets the header. Use the existing helper pattern in `tests/test_repo_map.py` (`85`), which compares `user["content"]` to `REPO_MAP_HEADER + "\n\n" + block + "\n\n" + text`.

### 2.5 `agent/tools.py` — line window, no continue footer

Next to `MAX_READ_LINES` (`277`):

```python
LINE_WINDOW = 200
```

In `_read_file`, after `lines = content.splitlines(keepends=True)` (`501`), the line-mode path with no caller `limit` cuts to `LINE_WINDOW` and does not build `notice`.

```python
if line is not None and limit is None:
    content = "".join(lines[:LINE_WINDOW])
    content = f"[line {line} of {path}]\n" + content
    return ToolResult(success=True, output=content)
```

This return sits inside the existing `try`, before the `hit_byte_cap` block. `PermissionError`, `UnicodeDecodeError`, and `OSError` are still caught below. A line past the end still returns the existing error at `486-488` and never reaches this cut.

Byte mode (`line is None`) is unchanged, including the `continue with offset=` notice. Line mode with an explicit `limit` is unchanged: the caller asked for a byte cap, and the existing notice rules apply.

Update the `read_file` tool description (`1308-1310`) to: `"Read a file. line is a 1-based start line and returns at most 200 lines. offset and limit are bytes."`

Rewrite the two continuation tests in `tests/test_tools.py` so they no longer expect a notice from `line: 1` with no limit:

- `test_read_continuation_hint_after_mid_line_window`: `line: 1`, no limit, file longer than 200 lines. Assert `r.output.startswith("[line 1 of t.txt]\n")`, `"continue with offset" not in r.output`, and the output has at most 200 newlines after the header line.
- `test_read_continuation_hint_after_newline_terminated_window`: same assertions.
- Add `test_read_byte_mode_still_names_the_next_offset`: `read_file` with no `line`, file larger than `MAX_READ_SIZE`. Assert `f"offset={MAX_READ_SIZE}" in r.output`. That preserves the byte-mode contract the old tests were covering.

### 2.6 `agent/tools.py` — search preview is 40 matching lines

In `_search_files`:

- Default `preview_lines: int = 40`.
- The fallback assignments `preview_lines = 5` become `40`.
- `preview_lines = min(preview_lines, 5)` becomes `min(preview_lines, 40)`.

Do not pass `-C` or `-A` to `_run_grep`. The parser at `1054` requires `path:lineno:content`.

Change the closing line (`1099`) from `[Use read_file("path") for the file body.]` to `[Open with read_file line=N only to edit.]`.

Change the `search_files` description (`1424-1428`) so it does not say "up to 5 preview lines" or "Use read_file for the body." Say "up to 40 matching lines."

`file_search` (`1446`) calls `_search_files`. It picks up the new cap. Update its description's "5 preview lines" the same way. Do not give `file_search` a different cap.

Grep tests for the old footer and the literal `5` tied to preview. Update those assertions to the new footer and to 40. Do not leave a test that requires the old sentence.

### 2.7 `agent/runtime.py` — Coder shell allowlist

Add this function next to `_reprint_target` (`407`). It returns `None` when the command may run, and a refusal string when it may not. It does not raise. `shlex.split` raises `ValueError` on bad quotes; catch that and refuse.

```python
import shlex

_CODER_GIT = frozenset({"status", "diff", "log"})
_SHELL_META = frozenset({"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "`"})


def _coder_shell_allowed(command: str, project_path: str) -> str | None:
    """None when a Coder shell command is on the allowlist. Else a refusal.

    One optional leading `cd <path> &&`, then exactly one of:
    git status|diff|log, python -m py_compile <files>, pytest <one tests/ path>.
    """
    refusal = (
        "REFUSED: Coder shell is limited to git status, git diff, git log, "
        "py_compile, and pytest on one path under tests/. "
        "Use read_file or search_files to read code."
    )
    if not isinstance(command, str) or not command.strip():
        return refusal
    if "\n" in command or "\r" in command:
        return refusal
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return refusal
    if not tokens:
        return refusal
    if tokens[0] == "cd":
        if len(tokens) < 4 or tokens[2] != "&&":
            return refusal
        from agent.tools import _resolve_project_path
        if _resolve_project_path(tokens[1], project_path) is None:
            return refusal
        tokens = tokens[3:]
    if any(tok in _SHELL_META or "$" in tok or "`" in tok for tok in tokens):
        return refusal
    if not tokens:
        return refusal
    head = os.path.basename(tokens[0])
    if head == "git" and len(tokens) >= 2 and tokens[1] in _CODER_GIT:
        return None
    if head in {"python", "python3"} and tokens[1:3] == ["-m", "py_compile"] and len(tokens) > 3:
        return None
    if head == "pytest" and _one_tests_path(tokens[1:], project_path):
        return None
    if head in {"python", "python3"} and tokens[1:3] == ["-m", "pytest"] and _one_tests_path(tokens[3:], project_path):
        return None
    return refusal


def _one_tests_path(args: list[str], project_path: str) -> bool:
    """True when args contain exactly one path and it stays under tests/."""
    from agent.tools import _resolve_project_path
    paths = [a for a in args if not a.startswith("-")]
    if len(paths) != 1:
        return False
    resolved = _resolve_project_path(paths[0], project_path)
    if resolved is None:
        return False
    root = os.path.realpath(project_path)
    try:
        rel = os.path.relpath(resolved, root)
    except ValueError:
        return False
    return rel == "tests" or rel.startswith("tests" + os.sep)
```

`_resolve_project_path` (`agent/tools.py:373`) returns `str | None`. It does not raise. `os.path.relpath` raises `ValueError` on different drives; that is caught. Return value of `_coder_shell_allowed`: `None` means run the command; a string means do not. The caller must not ignore a string.

In `_execute_tool_batch`, inside the `for` loop, before the `_block_file_reprint` call (`2318`):

```python
if tool_name == "exec_command" and getattr(conv, "agent_role", "") == "coder":
    command = args.get("command") if isinstance(args, dict) else None
    refusal = _coder_shell_allowed(
        command if isinstance(command, str) else "",
        conv.project_path or "",
    )
    if refusal is not None:
        tc.mark_failed(refusal)
        tc.result = None
        conv.add_tool_result(call_id, refusal)
        self._dispatch(
            self._on_tool_call_result, session_key, tool_name, refusal, False,
        )
        continue
```

`conv` is already the parameter of `_execute_tool_batch` (`2276`). `project_path` may be `None`; `or ""` makes `_resolve_project_path` return `None` (`380`: absolute and relative both need a real project root), so the command is refused. That is the right failure for a session with no project.

This runs before `_dispatch_approval`. A refused grep never opens an approval card. `_is_blocked` still runs later inside `_exec_command` for commands that pass the allowlist. Do not remove it.

Debugger and Supervisor do not enter this branch.

Update the `exec_command` description (`1380-1383`) with one extra sentence: `"Coder is limited to git status/diff/log, py_compile, and pytest on one tests/ path."` Other roles still need PM approval per call. Do not say the Coder needs no approval for the allowlist. Approval stays. The allowlist is what may be asked.

Tests, new file `tests/test_coder_shell_allowlist.py`. Call `_coder_shell_allowed` directly. Do not spawn a shell.

| Command | Result |
|---|---|
| `git status` | `None` |
| `git diff --stat` | `None` |
| `git log -1 --oneline` | `None` |
| `cd /tmp/proj && git status` when `/tmp/proj` is the project | `None` |
| `cd /etc && git status` | refusal |
| `.venv/bin/python -m py_compile agent/tools.py` | `None` |
| `.venv/bin/python -m pytest tests/test_tools.py -q` | `None` |
| `pytest tests/test_tools.py tests/test_repo_map.py` | refusal (two paths) |
| `grep -n foo agent/runtime.py` | refusal |
| `awk 'NR>=1' agent/runtime.py` | refusal |
| `git status && grep foo x` | refusal |
| `git commit -m x` | refusal |
| `python -c 'print(1)'` | refusal |
| `git status\ngrep foo x` | refusal |
| unbalanced quote | refusal (`ValueError`) |

### 2.8 `agent/runtime.py` — Coder writes get syntax only

Do not add a read counter. `read_file` is not refused because of how many times it has been called.

At the `ToolContext` construction (`2419`), do not mutate `self._config.enforcement`.

```python
enf = self._config.enforcement
if getattr(conv, "agent_role", "") == "coder":
    enf = dataclasses.replace(enf, lint_check=False, test_run=False)
ctx = ToolContext(
    ...
    enforcement_config=enf,
    ...
)
```

`dataclasses` is already imported in this module (`replace` is called at `2453`). `EnforcementConfig` is a dataclass (`agent/config.py:51`). `replace` returns a new instance. `syntax_check` stays whatever the runtime config had. `check` (`enforcement.py:1542`) skips the lint and test tiers when those flags are false, and still runs `_check_syntax` when `syntax_check` is true.

Debugger and Supervisor pass `self._config.enforcement` unchanged.

`check` returns `EnforcementResult()` for non-write tools. The replace is harmless on a read.

Tests: a Coder `edit_file` of a `.py` file that is valid Python produces a syntax check and does not produce a lint check. A Debugger edit of the same shape still lints when `lint_check` is true on the runtime config. Use the enforcement middleware's existing test module if one asserts lint output; do not weaken a Debugger assertion to make the Coder pass.

---

## 3. Data Flow

Coder turn, after this spec:

1. `AgentRuntime.send_message` starts `_run_loop` (`1669`).
2. `_ensure_system_prompt` / `_rebuild_system_prompt_if_inputs_changed` call `build_system_prompt` → `compose_system_prompt`. Role `coder` returns the filled work order. No file index.
3. Each iteration: `_compact_before_llm` (`1855`) unchanged. `_messages_for_model` (`1920`) forces `block = ""` for the Coder, so the user message is not prefixed with `REPO_MAP_HEADER`.
4. The model returns tool calls. `_execute_tool_batch` walks them in order.
5. `exec_command` on the Coder: `_coder_shell_allowed`. A string refusal is stored as the tool result and the loop `continue`s. No approval card. `None` falls through to today's reprint check, then `_dispatch_approval`, then `execute_tool` → `_exec_command` → `_is_blocked` → `Popen`.
6. `read_file` runs. There is no count and no refusal for reading again. Line mode with no `limit` returns at most 200 lines and no continuation notice.
7. `edit_file` / `write_file`: `ToolContext.enforcement_config` has `lint_check=False` and `test_run=False`. `EnforcementMiddleware` calls `check`, which runs syntax only.
8. The tool result is `conv.add_tool_result`. The next iteration sends that transcript. The turn ends when the model returns no tool calls, or at `max_tool_iterations`, or on cancel. Those endings are unchanged.

Supervisor and Debugger skip step 2's early return, step 3's empty block, step 5's allowlist, and step 7's tier replace.

---

## 4. File Change Summary

| File | Change | Risk |
|---|---|---|
| `prompts/system/coder.md` | Replace | Low. Prompt text. |
| `prompts/system/supervisor.md` | One bullet | Low. |
| `utils/prompt_loader.py` | Early return for `agent_role == "coder"` | Medium. Easy to skip the tool list. The new test locks it. |
| `agent/tools.py` | `LINE_WINDOW`, search cap 40 | Medium. Two existing tests change meaning. |
| `agent/runtime.py` | Empty map, allowlist, syntax-only context | High. The shell refusal must happen before approval and must `continue`. |
| `tests/test_prompt_loader.py` | Coder assertion | Low. |
| `tests/test_tools.py` | Line-mode tests, new byte-mode test | Low. |
| `tests/test_coder_shell_allowlist.py` | New | Low. |
| `tests/test_repo_map.py` or a sibling | Coder gets no header | Low. |
| `.crabcakes/architecture.md` | One sentence, section 8 | Low. |

**Files NOT changed:**

- `agent/repo_map.py` — still builds maps for other roles
- `agent/context.py` — hybrid index stays for non-Coder roles
- `agent/context_strategy.py` — landing goal and `panic_prune` stay until a later spec
- `agent/enforcement.py` — flags already exist; the Coder passes different flags
- `agent/tools.py` `_exec_command` body — allowlist is in the runtime, before approval
- `prompts/system/default.md`, `collab.md`, `project-awareness.md`, `crabcakes-commands.md`, `debugger.md` — other roles still load them
- Compiled-packet, hot-window, dedup, reprint, and pin code — deferred

---

## 5. Implementation Order

Do these in order. Do not start at the bottom. After each step, run the named tests and paste the output. A green step is the only reason to start the next one.

1. Replace `prompts/system/coder.md`. Update `test_coder_template_included`. Run `python -m pytest tests/test_prompt_loader.py -q`.
2. Add the Coder early return in `compose_system_prompt`. Add the "no Phase History / no File index" test. Retarget the four Coder-role cases in `tests/test_jit_context_discovery.py` named in section 2.3 (`573`, `596`, `610`, `634`). Run `python -m pytest tests/test_prompt_loader.py tests/test_jit_context_discovery.py -q`.
3. Add the supervisor bullet. Re-run `tests/test_prompt_loader.py -q`.
4. Empty the repo-map block for `agent_role == "coder"`. Add the wire test. Run `python -m pytest tests/test_repo_map.py tests/test_repo_map_v2.py -q`.
5. `LINE_WINDOW` and the line-mode early return. Rewrite the two continuation tests. Add the byte-mode test. Run `python -m pytest tests/test_tools.py -q`.
6. Search preview cap 40 and the new footer. Fix any test that asserts the old footer or "5 preview". Run `python -m pytest tests/test_tools.py -q` again.
7. `_coder_shell_allowed` and the batch check. Add `tests/test_coder_shell_allowlist.py`. Run it.
8. `dataclasses.replace` on the Coder's `ToolContext`. Run the enforcement tests that already cover `check` (`python -m pytest tests/test_enforcement.py -q` if that file exists; otherwise the file that imports `agent.enforcement.check`). Confirm a non-Coder config still lints.
9. Architecture sentence, section 8.

Step 8's test file name is verified at implementation time with `rg -l "enforcement import check|from agent.enforcement import" tests`. Do not invent a module name in the test if the grep finds a different one. Do not add a test that the Nth `read_file` is refused.

---

## 6. Acceptance Criteria

- [ ] A Coder `compose_system_prompt(..., agent_role="coder", project_path=<this repo>)` result contains `You are Coder`, the tool names passed in, and `Read each file you will change`, and does not contain `Phase History`, `File index`, `suggest the next phase`, `Core Principles`, or `eight reads`.
- [ ] The same call with `agent_role="supervisor"` still contains `orchestrator` and, on an un-onboarded project, `ONBOARDING phase`.
- [ ] `_messages_for_model` for `agent_role="coder"` does not put `REPO_MAP_HEADER` on the user message. Another role still does.
- [ ] `read_file` with `line=1` and no `limit` on a file longer than 200 lines returns at most 200 lines and does not contain `continue with offset`.
- [ ] `read_file` with no `line` on a file larger than `MAX_READ_SIZE` still contains `offset=`.
- [ ] `search_files` returns up to 40 matching lines and the footer `[Open with read_file line=N only to edit.]`.
- [ ] The allowlist table in section 2.7 passes, including the `ValueError` quote case.
- [ ] A refused Coder `grep` does not call `_dispatch_approval`. Assert with a callback that fails the test if it is invoked.
- [ ] A Coder `read_file` is never refused for count. The diff does not add `_reads_since_write` or `_READ_BUDGET`.
- [ ] A Coder edit runs the syntax tier and does not run the lint tier. `self._config.enforcement` is unchanged after the turn.
- [ ] `grep -n "continue with offset" prompts/system/coder.md` is empty.
- [ ] No edit to `agent/context_strategy.py` or `agent/repo_map.py` in the diff.

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| Coder `read_file` with `line` past EOF | Existing error `line N is past the end of {path}`. |
| Coder `read_file` with `line` and an explicit `limit` | Today's byte-cap path, including the notice. |
| Many `read_file` calls in one turn | All of them run. Nothing refuses a later read because an earlier one succeeded. |
| `edit_file` fails (old_text not unique) | `success=False` (`tools.py:659`). No syntax tier, because `check` returns immediately when the write failed. |
| `edit_file` succeeds | Syntax tier runs. Lint tier does not. |
| Coder command `cd <project> && git status && ls` | Refused. The second `&&` remains after the prefix strip. |
| Coder command with a newline | Refused before `shlex.split`. |
| `shlex.split` `ValueError` | Refused. No approval. |
| Coder session with `project_path` None or `""` | Allowlist refuses. `_resolve_project_path` returns `None`. |
| Debugger `grep` | Not allowlisted. Today's approval path. |
| Coder `git diff > /tmp/x` | Refused. `>` is in `_SHELL_META`. |
| Role `""` (gateway) | No early prompt return, no empty map, no allowlist, full enforcement flags. |
| Bug journal `.crabcakes/coder-bugs.md` | Not loaded for the Coder. Other roles unchanged. Intentional. |
| Compiled packet already stored for the Coder | Still replayed. Repo-map fact lines are empty because `block` is `""`. |
| Turn cancel between reads | No read counter exists to reset. The next turn is a normal turn. |

---

## 8. ARCHITECTURE.md Updates Required

Under "agent/ — the engine", after the file list, add:

> Coder prompts are role-local (SPEC-43): `compose_system_prompt` returns `prompts/system/coder.md` only, with no file index and no repo map on the wire. The Coder shell is an allowlist. Other roles keep the shared templates, the index, and the map.

No diagram change. The send path is still `agent/runtime.py`.

---

## Deferred — do not implement in this spec

After a Coder turn writes code inside its first handful of tool calls, a follow-up spec may delete:

- `_compute_landing_goal` and the over-ceiling crush to 24_000 tokens. Replace with one rule: near the real window, stub tool results from earlier turns, leave the current turn intact.
- Compiled packets, hot windows, read dedup, `REFUSED` / `unchanged` notices, the reprint blocklist, and the pin tables.
- `agent/repo_map.py` on the wire for every role, not just the Coder.

Do not start that spec in the same change. The acceptance test for starting it is a Coder turn whose first edit lands before the 8th read, observed in the feed, not a unit test.
