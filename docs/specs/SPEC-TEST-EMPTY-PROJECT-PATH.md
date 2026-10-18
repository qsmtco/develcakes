# SPEC: Stop test conversations from using `/tmp` as the project

**Date:** 2026-10-17
**Author:** Micah
**Status:** Draft — for implementation
**Implements:** Measurement that `AgentRuntime.create_conversation(..., "/tmp")` takes 3.49s on this machine because the prompt builder walks `/tmp` (97,233 files). The same call on an empty directory took 0.00s.
**Depends on:** None. Does not change production code.
**Target branch:** current working tree. Anchor edits to the call, not to line numbers. Line numbers below were read on 2026-10-17 and will drift.

> **Architecture compliance:** `docs/ARCHITECTURE.md` does not need an update. No module, public function, or data owner changes. `create_conversation` still receives a real directory and still builds the system prompt. The tests stop handing it the machine's shared temp directory.

---

## DISCOVERY

```
DISCOVERY:
- Read agent/runtime.py create_conversation (1029). Signature:
  (self, agent_name, session_key, project_path: str | None = None, model=None,
   allowed_tools=None, mcp_servers=None, agent_role="", si_enforcement=None,
   api_key=None, app_title="", fallback_provider=None, fallback_model=None,
   defer_prompt_build=False) -> str
  Returns the same session_key. When defer_prompt_build is False (every
  call site in this spec), it calls build_system_prompt(agent_name,
  project_path, tool_names, review_mode="off", ...).
- Read agent/context.py build_system_prompt (1012). If project_path is set,
  it calls utils.project_awareness.build_awareness_dict inside try/except
  Exception (failure is ignored). Then utils.prompt_loader.compose_system_prompt,
  also inside try/except Exception. A failure falls through to a one-line
  "You are {agent_name}" prompt. It does not raise to the test.
- Read utils/prompt_loader.py compose_system_prompt around line 336. When
  project_path is set it calls build_file_context_with_core_files(project_path,
  context_mode=...).
- Read agent/context.py walks: os.walk(project_path) at lines 497, 724, and
  974. The index walk stats every non-skipped file, then slices to
  max_entries (default 200). An empty directory finishes the walk immediately.
  Measured: os.walk("/tmp") 0.99s for 97,233 files; create_conversation
  with project_path="/tmp" 3.49s; create_conversation with an empty
  tempfile directory 0.00s.
- Read tests/test_agent_runtime.py. tempfile is already imported (line 12).
  TestPersistence already passes a TemporaryDirectory path into
  create_conversation (line 1254). The slow calls pass the literal "/tmp"
  as the third positional argument. Classes are plain pytest classes, not
  unittest.TestCase, so a tmp_path parameter is injected by pytest.
  70 such calls. One of them overwrites conv.system_prompt = "Test" after
  create (line 1057); the walk has already happened by then.
- Read tests/test_error_surfacing.py _seed_conversation (442). It calls
  create_conversation("Coder", sk, "/tmp"). Eight tests in
  TestEmptyMessageRollback call that helper (491, 514, 535, 564, 592,
  617, 703, 725, 759).
- Read tests/test_failure_classifier.py line 622:
  rt.create_conversation("Coder", sk, "/tmp") inside
  test_provider_network_error_without_marker_is_recorded(self, tmp_config_dir).
- Read tests/test_tool_middleware.py line 497:
  rt.create_conversation("Coder", sk, "/tmp"). The following tool-call
  JSON uses "path": "/tmp/test_out.txt". The test asserts only that
  _tool_chain.run saw "write_file". agent/tools.py _resolve_project_path
  returns None for an absolute path outside project_path. execute_tool
  returns a ToolResult; it does not raise. The chain is recorded before
  original_run returns.
- Read the other "/tmp" literals in these files. They are asserted strings
  or a ToolContext default, not the project walk:
  test_agent_runtime.py streamed argument fixtures "/tmp/foo.py" and
  "/tmp/bar.py"; handler._active_project ("test", "/tmp/test") asserted
  at prep_kwargs["project_path"]; checkpoint paths "/tmp/x";
  ("projB", "/tmp/spec10-other-proj").
  test_tool_middleware.py _make_context default project_path="/tmp/test"
  is stored on ToolContext. Those unit tests do not call
  build_system_prompt.
- Architecture owner: tests only. AgentRuntime still owns conversation
  creation. context.py still owns the walk.
- Existing pattern to copy: TestPersistence.test_save_conversation, which
  passes a private directory as project_path.
```

---

## 1. Overview

### Problem statement

Seventy calls in `tests/test_agent_runtime.py`, the helper in `tests/test_error_surfacing.py`, and one call each in `tests/test_failure_classifier.py` and `tests/test_tool_middleware.py` pass `"/tmp"` as `project_path`. `create_conversation` then walks that directory to build the system prompt. On this machine that walk sees 97,233 files and the call takes 3.49 seconds. The tests do not read anything that walk finds.

### Solution summary

Pass pytest's `tmp_path`, converted to `str`, as `project_path`. That directory exists and is empty. The prompt builder still runs. It no longer walks the machine temp directory.

### Scope

| In scope | Out of scope |
|---|---|
| The `create_conversation` third argument when it is the literal `"/tmp"` | `agent/runtime.py`, `agent/context.py`, `utils/prompt_loader.py` |
| `_seed_conversation` and its eight callers | `defer_prompt_build=True` as a way to skip the build |
| | Any `"/tmp/..."` string that is not that project-path argument |
| | `ToolContext(project_path="/tmp/test")` in `_make_context` |

---

## 2. Changes by File

### 2.1 `tests/test_agent_runtime.py`

There are 70 calls of the form `rt.create_conversation("Coder", <session>, "/tmp")`, including `rt.create_conversation("Coder", _uniq(), "/tmp")` in `TestLifecycle.test_create_get`.

For each test method that contains one of those calls, add the pytest fixture parameter `tmp_path` and pass `str(tmp_path)` as `project_path`.

`tmp_path` is a `pathlib.Path`. `create_conversation`'s annotation is `str | None`. Pass the string:

```python
def test_user_plus_assistant_in_conversation(self, tmp_path):
    rt = AgentRuntime(_make_cfg())
    rt.start()
    sk = _uniq()
    rt.create_conversation("Coder", sk, str(tmp_path))
```

`TestLifecycle.test_create_get` is the same change. Its assertion is `conv.agent_name == "Coder"`. It does not assert the project path.

If a method already has parameters, add `tmp_path` after `self`. Do not remove an existing fixture. `tempfile` is already imported and is the pattern in `TestPersistence`; either that or `tmp_path` is correct. Use `tmp_path` so the directory is the one pytest deletes at the end of the test. Do not call `tempfile.mkdtemp()` and leave the directory behind.

Do not replace these literals. They are expected strings, not the directory that gets walked:

- Streamed tool-argument fixtures `"/tmp/foo.py"` and `"/tmp/bar.py"`, and the asserts that compare `tool_calls[0]["function"]["arguments"]` to `'{"path":"/tmp/foo.py"}'` and `'{"path":"/tmp/bar.py"}'`.
- `handler._active_project = ("test", "/tmp/test")` and `assert prep_kwargs["project_path"] == "/tmp/test"`.
- Checkpoint calls whose path argument is `"/tmp/x"`, and `assert entry.path_used == "/tmp/x"`.
- `handler._active_project = ("projB", "/tmp/spec10-other-proj")`.

After the edit, `rg 'create_conversation\([^)]*"/tmp"' tests/test_agent_runtime.py` returns no matches. `rg '"/tmp' tests/test_agent_runtime.py` still matches the fixtures listed above.

### 2.2 `tests/test_error_surfacing.py`

`_seed_conversation` is the only `create_conversation(..., "/tmp")` in this file. Give it the directory:

```python
def _seed_conversation(rt, sk, pre_history, project_path: str):
    rt.create_conversation("Coder", sk, project_path)
    conv = rt._conversations[sk]
    conv.messages.extend(pre_history)
    return conv
```

`project_path` is required. There is no default of `"/tmp"`.

Each of the eight methods in `TestEmptyMessageRollback` that call `_seed_conversation` takes `tmp_path` and passes `str(tmp_path)`:

- `test_rollback_removes_trailing_empty_assistant`
- `test_rollback_stops_at_user_message` (the call at line 514; confirm the method name in the file if it has drifted)
- the method at the call on line 535
- the method at the call on line 564
- the method at the call on line 592
- the method at the call on line 617
- `test_cancel_requested_site_dispatches_cancel_message` (call at 703)
- the `_cancelled` site (call at 725)
- the remaining call at 759

Read the `def` above each call and add `tmp_path` to that method. The helper's other behavior stays: it extends `conv.messages` with `pre_history` and returns `conv`. Callers that ignore the return (`_seed_conversation(...)` with no assignment) stay that way.

### 2.3 `tests/test_failure_classifier.py`

`test_provider_network_error_without_marker_is_recorded(self, tmp_config_dir)` becomes `(self, tmp_config_dir, tmp_path)`.

Change only the project argument:

```python
rt.create_conversation("Coder", sk, str(tmp_path))
```

`tmp_config_dir` stays. It isolates config. `sk` stays `"special:coder"`. The assertion on `recent()` stays.

### 2.4 `tests/test_tool_middleware.py`

`test_run_loop_invokes_chain` (the test whose body starts near line 497; confirm the `def` name above that line) takes `tmp_path`. Change the project argument to `str(tmp_path)`.

Leave the tool-call JSON as `'{"path": "/tmp/test_out.txt", "content": "hello"}'`. That absolute path will not resolve inside the empty project. `_resolve_project_path` returns `None` for an absolute path whose `realpath` is not under `realpath(project_path)`. `execute_tool` returns a `ToolResult` and does not raise. `tracking_run` appends `"write_file"` before it calls `original_run`, so both assertions still hold:

- `len(chain_calls) >= 1`
- `"write_file" in chain_calls`

Leave `_make_context`'s `project_path="/tmp/test"`. Those tests construct a `ToolContext` and do not build a system prompt.

### 2.5 Files NOT changed

| File | Why |
|---|---|
| `agent/runtime.py` | `create_conversation` already accepts any directory. |
| `agent/context.py` | The walk is how a real project is indexed. This spec does not cap it. |
| `utils/prompt_loader.py` | Still calls `build_file_context_with_core_files` when `project_path` is set. |
| `utils/project_awareness.py` | Still called, and its failure is still swallowed by `build_system_prompt`. |
| `tests/conftest.py` | The autouse `HOME` / `XDG_CONFIG_HOME` isolation stays. `tmp_path` is a different directory from that config dir, which is what these tests already rely on for `tmp_config_dir`. |

---

## 3. Data Flow

Unchanged, except the directory that gets walked.

1. The test calls `create_conversation(agent_name, session_key, str(tmp_path))`.
2. `create_conversation` builds a `Conversation` with that `project_path`, then `build_system_prompt`.
3. `build_system_prompt` calls `build_awareness_dict(project_path)`. On an empty directory that finds no `.crabcakes` project files. Any exception is ignored.
4. `compose_system_prompt` calls `build_file_context_with_core_files`. `os.walk` sees the empty directory and returns. The prompt is the template text without a file index of `/tmp`.
5. The test's `_run_loop` or assertions run as they do now. `TestPersistence` already follows this path with its own directory.

`_run_loop` calls `_ensure_system_prompt`, which returns immediately when `conv.system_prompt` is already set. It does not walk a second time unless a later rebuild sees different inputs. These tests do not change those inputs before the first model call.

---

## 4. File Change Summary

| File | Change | Risk |
|---|---|---|
| `tests/test_agent_runtime.py` | 70 `project_path` arguments, plus `tmp_path` on each owning method | Low. Assertions are about roles, tools, and turn state. |
| `tests/test_error_surfacing.py` | Helper gains `project_path`. Eight methods pass `str(tmp_path)`. | Low. The helper's message setup is unchanged. |
| `tests/test_failure_classifier.py` | One call. Existing `tmp_config_dir` stays. | Low. |
| `tests/test_tool_middleware.py` | One call. The `write_file` JSON path stays `/tmp/test_out.txt`. | Low. The assertion is that the chain ran. |

---

## 5. Implementation Order

1. Replace the 70 project-path arguments in `tests/test_agent_runtime.py`. Run that file.
2. Change `_seed_conversation` and its eight callers. Run `tests/test_error_surfacing.py`.
3. Change the one call in `tests/test_failure_classifier.py` and the one call in `tests/test_tool_middleware.py`. Run those two files.
4. Confirm the grep in §6. A global replace of the characters `/tmp` is a failure of this spec, because the fixture strings in §2.1 must remain.

---

## 6. Acceptance Criteria

- [ ] `rg 'create_conversation\([^)]*"/tmp"' tests/test_agent_runtime.py tests/test_error_surfacing.py tests/test_failure_classifier.py tests/test_tool_middleware.py` prints nothing.
- [ ] `rg '"/tmp/foo.py"|"/tmp/bar.py"|"/tmp/test"|"/tmp/x"|"/tmp/spec10-other-proj"|"/tmp/test_out.txt"' tests` still finds those fixture strings.
- [ ] `_make_context` in `tests/test_tool_middleware.py` still defaults `project_path` to `"/tmp/test"`.
- [ ] No diff in `agent/runtime.py`, `agent/context.py`, or `utils/prompt_loader.py`.
- [ ] `tests/test_agent_runtime.py`, `tests/test_error_surfacing.py`, `tests/test_failure_classifier.py`, and `tests/test_tool_middleware.py` pass.
- [ ] A `create_conversation` in these tests passes a directory that the test created, not `"/tmp"`.

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| `tmp_path` is empty | `os.walk` returns one directory and no files. `build_system_prompt` returns the template prompt. `create_conversation` returns the session key. |
| `build_awareness_dict` raises on the empty directory | `build_system_prompt` swallows that exception and still composes the prompt. |
| `compose_system_prompt` raises | `build_system_prompt` returns `You are {agent_name}` plus the tool list. The tests in this spec do not assert prompt body text. |
| Test overwrites `conv.system_prompt = "Test"` after create | The overwrite still happens. The walk it used to pay for was of `/tmp`; now it is of `tmp_path`. |
| `write_file` argument is the absolute path `/tmp/test_out.txt` while the project is `tmp_path` | `_resolve_project_path` returns `None`. `execute_tool` returns a failed `ToolResult` and does not raise. `test_run_loop_invokes_chain` still sees `"write_file"` in `chain_calls`. |
| Two tests in one class | Each gets its own `tmp_path`. Session keys stay `_uniq()` or the literal `"special:coder"` where that literal is already the key. |
| Method already takes `tmp_config_dir` | Add `tmp_path` as another parameter. Do not reuse the config directory as the project directory. |

---

## 8. ARCHITECTURE.md Updates Required

None.

---

## 9. Self-audit

Checked against the source read on 2026-10-17:

1. The sample call matches `create_conversation(agent_name, session_key, project_path)` and passes `str`, which is the annotated type. Return value is the session key; the samples that ignore it match the current tests.
2. `build_system_prompt` catches `Exception` around awareness and around compose. The spec does not add a new `except`. `execute_tool` returns `ToolResult` when the path is outside the project; the middleware test does not require `success is True`.
3. The third positional argument is `project_path`. The `"/tmp"` calls in these four files use that position. The other `"/tmp"` strings are JSON, tuples, and a `ToolContext` field, and the spec leaves them.
4. `tmp_path` is the pytest built-in. These classes are not `unittest.TestCase`, so a test method can take it. `TestPersistence` already proves a private directory is a valid `project_path`.
5. An implementer who replaces every `"/tmp"` in the file will break the argument-fixture asserts. §2.1 and §6 name the strings that must remain.

Not verified by re-running the four test files after an edit. This document is the spec, not the implementation.
