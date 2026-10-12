# SPEC-24: Compiled turn after a write, and sha-scoped project facts

**Date:** 2026-10-11
**Author:** Micah
**Status:** Draft — Stage A for implementation; Stage B after Stage A lands
**Implements:** PM direction 2026-10-11 (failure-shaped context and a project fact board that reuses `project_awareness`, not a new memory module)
**Depends on:** `agent.enforcement.check`, `Conversation.to_api_messages`, `utils.git_ops.get_head_sha` and `diff_file_against_working_tree`. SPEC-23 is the transcript-cap spec. Stage A does not require it: enforcement already slices each check's `output` to `EnforcementConfig.max_output_chars` (default 2000).
**Target branch:** main

> **Architecture compliance:** `utils/project_awareness.py` remains the owner of `.crabcakes/` project memory. It does not import `agent/`. The runtime calls into it. The wire payload for one model call is built in `agent/runtime.py` and does not delete `conv.messages`. `agent/llm/convert.py` is not modified. No new UI surface. The context meter keeps reading `get_token_estimate()` on the stored conversation.

---

## DISCOVERY

- Read `agent/enforcement.py`: `EnforcementCheck` fields are `tier`, `tool`, `file`, `passed`, `detail`, `output`, `duration_ms` (line 190). `EnforcementResult` is `checks` plus `appended_message` (line 203). `check(tool_name, tool_args, tool_result, project_path, config, verbose=False)` returns that result (line 1542). It runs for `write_file` and `edit_file` only when `tool_result.success` is true and `tool_args["path"]` is non-empty. A skipped file (`_is_skipped`, default patterns include `*.md` and `*.json`) makes `_check_syntax` return `None` when `verbose` is false (line 678). Empty `checks` returns `EnforcementResult()` with no message (line 1641). `_format_result` slices each check's displayed output to `config.max_output_chars` (line 1529). `check` does not raise for a normal skip; the middleware catches any exception from `check`.
- Read `agent/tool_middleware.py`: `EnforcementMiddleware.__call__` runs `next()` first, then `check`, then `dataclasses.replace` to append `appended_message` onto `ToolResult.output` (line 132). The structured `checks` are not stored on the result. `on_status` is invoked per check with `tier`, `file`, `passed`, `detail` only (line 139). The middleware file's own rule is stdlib plus `agent.tools`. Do not import `utils.project_awareness` from here.
- Read `agent/runtime.py`: the chain is built at line 683 with `on_status=self._dispatch_enforcement_status`. That method (line 730) calls `_dispatch`, which uses `GLib.idle_add` when GLib is set (line 725). A status callback is therefore not guaranteed to have run before the next line of the tool thread. `messages = conv.to_api_messages()` is line 1848, after the budget guard at line 1841 that compares `conv.get_token_estimate()` to `model_max - 4096`. That guard runs on the stored transcript, before the wire list exists. `_call_llm` is line 1858.
- Read `agent/tools.py`: `ToolResult` is a dataclass (line 44) with `success`, `output`, `error`, `duration_ms`, `stdout`, `stderr`, `exit_code`. `to_dict` lists those keys explicitly (line 55) and will not pick up a new field unless someone adds it. `_edit_file(path, old_text, new_text, project_path, session_key="", approval_callback=None)` returns `OK — edited {path}` and does not include the hunk in `output` (line 506). The hunk is the tool arguments. `_write_file` returns `OK — wrote {byte_count} bytes to {path}` (line 433) and does not echo `content`. `is_sensitive_path` is true for any path whose normalized form starts with `.crabcakes/` (pattern at line 267). `_resolve_project_path` returns an absolute real path inside the project, or `None`.
- Read `utils/project_awareness.py`: `CONTEXT_FILENAME = "context.md"`, `MAX_CONTEXT_SIZE = 50 * 1024`, `CONTEXT_READ_CAP = 8000`, `MAX_CONTEXT_ENTRIES = 50`. `append_project_context(project_path, entry) -> None` supersedes matching in-progress headings and FIFO-caps entries, then calls `save_project_context`. `save_project_context` catches `OSError`, logs, and does not raise (line 513). `append_project_context` has no production caller. `build_awareness_dict` puts `context[:CONTEXT_READ_CAP]` in `PROJECT_MEMORY` inside `_untrusted_fence` (line 1107) and the last `## ` heading in `CURRENT_TASK` via `get_current_task`. The awareness cache key is directory mtime plus a SHA1 of `context.md` only (line 1048). `_awareness_dir_mtime` stats every file in `.crabcakes/`, so a new file's mtime invalidates the cache.
- Read `utils/git_ops.py`: `get_head_sha(project_path) -> GitResult`. On success `result.sha` is `repo.head.commit.hexsha` (40 hex chars). On failure `success` is false and `sha` is `None`. It catches `Exception` and does not raise. `diff_file_against_working_tree(project_path, sha, file_path) -> GitResult` is `git diff <sha> -- <file>` including unstaged edits (line 226). A non-string sha or a sha that fails `^(HEAD|[0-9a-fA-F]{4,40})$` returns `success=False` and does not raise. An untracked file produces an empty diff. `status_porcelain(project_path) -> dict[str, str]` maps relative path to a 2-char porcelain code and returns `{}` on error.
- Read `models/conversation.py`: `to_api_messages` emits the system message from `conv.system_prompt`, then one dict per `conv.messages` entry. It does not delete messages. `MessageRole.USER` serializes as `"user"`.
- Read `agent/llm/convert.py`: a `system` message becomes one Anthropic `user` message (line 28). A following `user` message is passed through. They stay adjacent. That is the same shape as today's system-plus-first-user call.
- Architecture owner: project memory is `utils/project_awareness.py` (ARCHITECTURE.md project-awareness section; the module header lists `context.md` and `awareness.json` as its files). The turn engine in `agent/runtime.py` owns what is sent to the model. `Conversation` owns the stored transcript.
- Existing pattern to copy: `append_to_bug_journal` holds a file lock across read-modify-write. Fact writes need the same shape, as a `threading.Lock` inside `project_awareness` (one process, many agent threads). Do not copy the bug journal's role file. A bug lesson is supposed to survive the next edit. A fact is not.

---

## 1. Overview

### Problem

After a write, the next model call still carries every earlier read. The enforcement verdict is mashed onto the tool output string, and the structured checks are discarded. Separately, `.crabcakes/context.md` is the project diary and it is already injected into every agent, but `append_project_context` is never called, entries are not tied to a git sha, and the prompt pastes the first 8,000 characters whether or not they are still true.

### Solution

Stage A, on the model call after an enforced write, sends a compiled packet instead of the exploration that led to the write. The stored transcript is kept. Stage B records one sha-scoped fact per enforced write in `.crabcakes/facts.json`, drops a fact when its file differs from that sha in the working tree, and injects the live facts through `PROJECT_MEMORY` in place of the diary paste. `context.md` stays the human diary. Writes to that path go through `append_project_context`.

### Scope

| In | Out |
|---|---|
| Stage A: stash `EnforcementCheck`s on `ToolResult`, compile the next wire payload from the goal plus the hunk plus those checks, keep doing so for later calls in that session until the conversation is cleared | Deleting `conv.messages`. A new message role. Changing `convert_messages_for_anthropic` |
| Stage B: `facts.json` beside `context.md`, sha drop via `diff_file_against_working_tree` and `status_porcelain`, `PROJECT_MEMORY` becomes the live facts, `context.md` writes go through `append_project_context` | A new `utils/fact_board.py`. A model tool that appends prose. Putting facts on `WorkUnit`. Invalidating `{role}-bugs.md` |

### Principles

- The transcript on disk and in the UI is the audit log. The compiled list is what the model sees after a write.
- Stage A with an empty fact list is a complete feature. Stage B does not change the compile shape except to prepend the live fact lines to the packet.
- `on_status` / `_dispatch` is the UI path. It is not the compile path, because GLib delivery is later than the next loop iteration.
- A fact whose git check errors is kept. A git failure must not wipe the board.
- `PROJECT_MEMORY` is either the diary paste or the live facts. It is not both.

---

## 2. Changes by file

### 2.1 `agent/tools.py` — Stage A

Add a field to `ToolResult`, after `exit_code`:

```python
enforcement_checks: list = dataclasses.field(default_factory=list)
```

`to_dict` stays as it is. The field is in-process only. `dataclasses.replace` in the middleware and in `execute_tool` (the `duration_ms` replace at line 1491) preserves fields that are not named, so the checks survive that replace.

Do not put check text into `output` a second time. The middleware already appends `appended_message`.

**Line count:** about +2.

### 2.2 `agent/tool_middleware.py` — Stage A

After a successful `check` that returns a non-empty `checks` list, attach it:

```python
result = dataclasses.replace(
    result,
    output=(result.output or "") + ("\n" + enf_result.appended_message if enf_result.appended_message else ""),
    enforcement_checks=list(enf_result.checks),
)
```

When `checks` is empty, leave `enforcement_checks` as the default empty list and keep today's output behavior. The `except Exception` path still returns the pre-check `result` (line 150). Do not import `project_awareness`.

**Line count:** about +8.

### 2.3 `agent/runtime.py` — Stage A

Add `self._compile: dict[str, dict] = {}` in `__init__`, next to `_tool_history`. Value shape, all keys required:

```python
{
    "goal": str,            # last user message content
    "path": str,            # tool args["path"]
    "hunk": str,            # see below
    "checks": list,         # list of {tier, passed, detail, output}
    "after": int,           # len(conv.messages) AFTER the tool result was appended
}
```

Set it at the end of `_execute_tool_batch`, after `add_tool_result`, only when `tool_name in ("write_file", "edit_file")` and `result.enforcement_checks` is non-empty.

Goal: walk `conv.messages` from the end and take the content of the last message whose role is `MessageRole.USER`. If there is no user message, do not set `_compile`.

Hunk:

- `edit_file`: `old_text` and `new_text` from `args`, each sliced to 4_000 characters. Format:

```text
--- old_text
{old}
--- new_text
{new}
```

- `write_file`: do not include `args["content"]`. One line: `write_file replaced {path} ({byte_count} bytes)` using `len(args.get("content", "").encode("utf-8"))`.

Checks: for each `EnforcementCheck`, store `tier`, `passed`, `detail`, and `output` sliced to 2_000 characters (the config default is already 2_000; slice again so a future config change cannot blow the packet).

`after` is `len(conv.messages)` after the tool-result append. The exploration, including this write's assistant message and tool result, sits at indexes below `after`. Later messages sit at `after` and above.

**Compiled wire list.** In `_run_loop`, where `messages = conv.to_api_messages()` is assigned (line 1848), if `session_key` is in `_compile`:

```python
packet = self._compile[session_key]
tail = conv.messages[packet["after"]:]
body = packet["goal"] + "\n\n## Write\n" + packet["hunk"] + "\n\n## Checks\n" + _format_checks(packet["checks"])
messages = []
if conv.system_prompt:
    messages.append({"role": "system", "content": conv.system_prompt})
messages.append({"role": "user", "content": body})
messages.extend(_serialize_tail(tail))
```

`_serialize_tail` emits the same dict shape as the per-message loop inside `to_api_messages` (user / assistant / tool). Do not call `to_api_messages` and then try to drop the system row only: that would still include the exploration. Copy the three role branches, or extract the existing loop into a helper that accepts a message list. The helper must keep the empty-assistant placeholder behavior at `to_api_messages` line 267.

`_format_checks` is a private function in `runtime.py`. One block per check: `[{tier}] passed={passed} {detail}` plus `output` when non-empty. No new emoji. The middleware string already has the icons; the packet is the structured copy.

**Budget guard.** The check at line 1841 uses `conv.get_token_estimate()`, which counts the stored transcript. A compiled call can fit while the transcript does not. When `_compile` is set for this session, skip that raise. After building `messages`, if the sum of `len(content)` across those dicts (and tool-call argument JSON) `// 4` is `>= model_max - _RESPONSE_RESERVE_TOKENS`, raise the same `RuntimeError` text with the compiled sizes filled in. `content` on an assistant dict is a string. Tool dicts use `content` too.

Do not pop `_compile` at the end of the turn. The next user message is appended after `after`, so it shows up in `tail`. The reads from before the write stay in `conv.messages` and stay out of the wire list. Pop `_compile[session_key]` in `clear_conversation`'s reset path (`ui/agent_runtime/session.py`, the block that sets `conv.messages = []`). Add `AgentRuntime.drop_compile(self, session_key: str) -> None` that pops the key and ignores a missing key. Call it next to the existing cache invalidation. On the refusal path (active tool loop, the function returns `False` before the reset) do not drop it.

A later enforced write in the same session replaces the dict. `after` moves forward. The previous packet is gone. The new goal is still the latest user message, which may be newer than the original task. That is the message the model was just answering.

Reads, searches, and writes that produced no checks (markdown and json are skipped by enforcement's default `skip_patterns`) do not set `_compile` and do not clear an existing one.

**Line count:** about +120.

### 2.4 `utils/project_awareness.py` — Stage B

Add `FACTS_FILENAME = "facts.json"` next to `CONTEXT_FILENAME`.

A fact dict, the only shape stored:

```python
{
    "text": str,          # one line, no newline
    "files": list[str],   # project-relative, forward slashes
    "sha": str,           # get_head_sha result.sha
    "session_key": str,
    "created_at": str,    # time.strftime("%Y-%m-%dT%H:%M:%S%z") — same clock as awareness.json opened_at
}
```

`_FACTS_LOCK = threading.Lock()` at module level.

```python
def load_facts(project_path: str) -> list[dict]:
```

Reads `facts.json`. Missing file returns `[]`. `OSError` or `json.JSONDecodeError` returns `[]` and logs. A non-list JSON value returns `[]`. Entries that are not dicts, or that lack a string `text`, a list of strings `files`, and a string `sha`, are skipped.

```python
def load_live_facts(project_path: str) -> list[dict]:
```

Calls `load_facts`. Drops a fact when any path in `files` has changed since `sha`:

```python
diff = diff_file_against_working_tree(project_path, fact["sha"], rel)
if not diff.success:
    keep  # git error must not wipe the fact
elif diff.stdout.strip():
    drop
elif rel in status_porcelain(project_path):
    drop  # untracked and other porcelain states that diff omits
```

`status_porcelain` is called once per `load_live_facts`, not once per file. Import both functions inside the function, the same way `build_awareness_dict` lazy-imports workflow, so a git import failure follows the existing `except` style: log and treat the diff as unsuccessful (keep the fact).

```python
def record_fact(project_path: str, text: str, files: list[str], sha: str, session_key: str) -> None:
```

Under `_FACTS_LOCK`: load, append, if `len > 50` keep the last 50, write JSON with `indent=2`. `OSError` is logged and not raised, matching `save_project_context`. `text` is `text.replace("\n", " ").strip()`. Empty `text` or empty `sha` or empty `files` returns without writing.

```python
def record_enforcement_fact(project_path: str, session_key: str, path: str, checks: list) -> None:
```

`checks` items are objects with `tier` and `passed` (the `EnforcementCheck` instances the runtime already holds). Build one line:

```text
{path}: syntax passed, tests failed
```

using `tier` and `passed` in list order, joined by `", "`. Call `get_head_sha(project_path)`. If `success` is false or `sha` is `None`, log and return. Otherwise `record_fact(..., files=[path], sha=result.sha, session_key=session_key)`.

**`PROJECT_MEMORY`.** In `build_awareness_dict`, replace the block that assigns `context[:CONTEXT_READ_CAP]` (lines 1103–1112). New behavior:

```python
live = load_live_facts(project_path)
if live:
    lines = [f["text"] for f in live[-30:]]
    blob = "\n".join(lines)
    if len(blob) > 4000:
        blob = blob[-4000:]
    parts["PROJECT_MEMORY"] = _untrusted_fence(blob, "facts.json")
else:
    parts["PROJECT_MEMORY"] = ""
```

Do not also assign the diary slice. `CURRENT_TASK` stays `get_current_task` (the diary heading). The cache fingerprint must include the facts file as well as `context.md`, or a facts-only write can be served from the cache when the directory mtime does not move. Extend the fingerprint:

```python
_facts_for_fp = open(facts path).read() if the file exists else ""
_content_fp = sha1(context_bytes + b"\0" + facts_bytes)
```

Catch `OSError` on the facts read and use `b""`.

**Line count:** about +140.

### 2.5 `agent/runtime.py` — Stage B writer

Where Stage A sets `_compile`, also call:

```python
from utils.project_awareness import record_enforcement_fact
record_enforcement_fact(
    conv.project_path or "",
    session_key,
    args.get("path", ""),
    result.enforcement_checks,
)
```

`record_enforcement_fact` returns `None`. A failure inside it must not fail the turn: call it inside `try/except Exception` and log. Empty `project_path` hits `get_head_sha` on `""`, which returns `success=False` (git raises, caught). No fact is written. That is the right outcome.

Prepend the live fact lines to the packet body, above `## Write`, by calling `load_live_facts(conv.project_path)` when `project_path` is non-empty. Render at most 30 texts. If the load raises, proceed with no fact section. Stage A tests that pass `project_path=""` skip this.

SPEC-25 may insert a `## Repo map` block above those fact lines. This spec does not build that block. If the runtime has no map, the packet is unchanged.

**Line count:** about +25.

### 2.6 `agent/tools.py` — Stage B diary intercept

Helper, private:

```python
def _is_context_md(path: str, project_path: str) -> bool:
    resolved = _resolve_project_path(path, project_path)
    if resolved is None:
        return False
    expected = os.path.realpath(os.path.join(project_path, ".crabcakes", "context.md"))
    return resolved == expected
```

`_write_file`: keep the sandbox check and the `is_sensitive_path` approval block first. `.crabcakes/` is a sensitive prefix, so a denied approval returns the existing error and does not append. Only after approval succeeds (or the path is somehow not sensitive), if `_is_context_md`, do not open the file for writing.

- If `content.lstrip().startswith("## ")`, call `append_project_context(project_path, content.strip())`, then `load_project_context`. If the new content's first heading line is absent from the loaded text, return `success=False` with `error="context.md append did not persist"`. Otherwise return `success=True`, `output="OK — appended an entry to .crabcakes/context.md"`.
- Otherwise return `success=False`, `error="context.md is append-only. write_file a single ## entry. The file was not changed."` The file on disk is untouched.

`append_project_context` does not raise on `OSError`. The reload check is what makes a swallowed write visible.

`_edit_file`, after the sandbox check and before the approval call: if `_is_context_md`, return `success=False`, `error="context.md is append-only. write_file a single ## entry. The file was not changed."` Do not read or write the file, and do not ask for approval. The edit cannot succeed, so the PM is not prompted.

`.crabcakes/` is a sensitive prefix, so a normal write still asks the PM before `_write_file` runs when the runtime gate is in front. The intercept is inside `_write_file` so a caller that reached the function, approved or not, cannot replace the diary. Direct unit tests call `_write_file` / `execute_tool` without the PM gate. Those tests cover the intercept.

**Line count:** about +40.

### Files NOT changed

- `agent/llm/convert.py` — compiled messages are system plus user, which this function already converts.
- `models/work_unit.py` and `utils/work_persistence.py` — a work unit is a task. A fact is not a field on it.
- `utils/feedback_processor.py` — bug journals stay lessons that survive edits.
- `utils/workflow_state.py` — phase table, different file.
- `agent/enforcement.py` `check` — already returns the checks. No new tier.
- `.gitignore` — `.crabcakes/` is already ignored.

---

## 3. Data flow

### Stage A

User message is already on `conv.messages`. The tool loop calls the model with `to_api_messages()` while `_compile` is empty. The model calls `edit_file` or `write_file`. `_execute_tool_batch` runs the middleware chain. `check` fills `EnforcementResult.checks`. The middleware copies that list onto `ToolResult.enforcement_checks` and still appends the human string to `output`. The runtime appends the tool message, then stores `_compile` with `after == len(conv.messages)`.

The next iteration builds `messages` from the system prompt, one user message (goal, hunk, checks), and `conv.messages[after:]`. The reads from before the write are not in that list. They remain on `conv`. `get_token_estimate()` still counts them, so the meter can read higher than the request that was just sent. That is intentional.

The model's next reply is appended after `after`, so the following iteration includes it in `tail` and still omits the exploration. `clear_conversation` drops `_compile`. The following send uses `to_api_messages()` on whatever messages remain.

A write of `README.md` matches `skip_patterns` (`README*`). `_check_syntax` returns `None`. `checks` is empty. `_compile` is not set.

### Stage B

The same write calls `record_enforcement_fact`. `get_head_sha` supplies `sha`. The line and the path land in `facts.json` under the lock. The next `build_awareness_dict` sees the new file mtime and the new fingerprint, and `PROJECT_MEMORY` is those lines inside the untrusted fence. `context.md` is not in that variable.

Later, `diff_file_against_working_tree` for that path against the stored sha returns a non-empty diff, or `status_porcelain` contains the path. `load_live_facts` omits the fact. The JSON file still has it until the 50-cap drops it on a later `record_fact`. Live reads filter; they do not rewrite the file.

A `write_file` whose resolved path is `.crabcakes/context.md` and whose content starts with `## ` calls `append_project_context` and does not replace the file. An `edit_file` of that path returns an error and does not open the file.

---

## 4. File change summary

| File | Change | Stage | Risk |
|---|---|---|---|
| `agent/tools.py` | `enforcement_checks` field; context.md intercept | A then B | Medium — one path becomes append-only |
| `agent/tool_middleware.py` | Copy `checks` onto the result | A | Low |
| `agent/runtime.py` | `_compile` wire list; budget guard exception; fact writer | A then B | High — the model stops seeing exploration after a write |
| `utils/project_awareness.py` | `facts.json`, live filter, `PROJECT_MEMORY` swap | B | High — every agent's standing prompt changes |
| `ui/agent_runtime/session.py` | `drop_compile` on clear | A | Low |
| `docs/ARCHITECTURE.md` | Name `facts.json` and the compiled wire | B | Low |

---

## 5. Implementation order

1. **Stage A field and middleware.** Unit-test: a fake `check` returning one `EnforcementCheck` leaves that object on `result.enforcement_checks`, and `result.output` still contains `appended_message`.
2. **Stage A compile.** Build a conversation with a user goal, several reads, then a write whose result has checks. The next wire list's user content contains the goal, the hunk, and the check detail, and does not contain the read body. `len(conv.messages)` is unchanged. A second iteration appends an assistant message and the wire list contains that assistant message and still omits the read.
3. **Stage A budget.** A transcript whose `get_token_estimate()` is over the ceiling still produces a compiled call when the compiled character count is under the ceiling. Do not raise the line-1841 error in that case.
4. **Stage A non-trigger.** A write of `notes.md` with default enforcement config does not set `_compile`. A failed `edit_file` (`old_text` not found) does not set it.
5. **Stop.** Stage B starts only after those tests pass.
6. **Stage B facts.** `record_fact` then `load_live_facts` returns the line. Point `diff_file_against_working_tree` at a temp git repo: after changing the named file, `load_live_facts` returns `[]` while `load_facts` still returns the row. A `get_head_sha` failure (pass a path that is not a repo) does not write.
7. **Stage B prompt.** `build_awareness_dict` `PROJECT_MEMORY` contains the fact text and does not contain a marker that exists only in `context.md`. `CURRENT_TASK` still returns the diary's last heading.
8. **Stage B diary.** `write_file` of `.crabcakes/context.md` with `## 2026-10-11 — Note\nBody` leaves the previous entry in the file and adds the new one. `write_file` of the same path with `no heading` leaves the file bytes unchanged and `success` is false. `edit_file` of that path leaves the bytes unchanged and `success` is false.

---

## 6. Acceptance criteria

Stage A:

- [ ] After an `edit_file` that produced at least one `EnforcementCheck`, the next model message list contains the goal, a 4_000-char-capped hunk, and the check `detail`, and does not contain an earlier `read_file` body.
- [ ] `conv.messages` still contains that `read_file` body.
- [ ] The following model call in the same session still omits that body and includes replies appended after the write.
- [ ] `clear_conversation` clears the compile state. The next send uses `to_api_messages()`.
- [ ] A skipped markdown write does not start a compile.
- [ ] The over-budget guard does not fire when the transcript is over the ceiling and the compiled list is under it.

Stage B:

- [ ] An enforced write of a `.py` file appends one line to `.crabcakes/facts.json` with `files == [that path]`, `sha` equal to `get_head_sha().sha`, and `session_key` equal to the caller.
- [ ] After that file's working tree differs from `sha`, `load_live_facts` omits the row.
- [ ] `git` failure while checking a fact leaves the fact in `load_live_facts`.
- [ ] `PROJECT_MEMORY` is the live lines inside the untrusted fence and does not include the diary body.
- [ ] `context.md` cannot be replaced by `write_file` or `edit_file`. A `## ` write goes through `append_project_context`.
- [ ] No new tool name appears in `get_tool_definitions_for_api()`.

---

## 7. Edge cases

| Case | Expected behavior |
|---|---|
| Enforcement disabled (`EnforcementConfig.enabled` false, or `si_enforcement` false) | Middleware returns before `check`. `enforcement_checks` stays `[]`. No compile, no fact. |
| `check` raises | Middleware logs and returns the original result (line 145). No compile, no fact. |
| Several checks on one write | One fact line listing each `tier` and passed/failed. All checks appear in the packet. |
| `write_file` content is large | Packet says the byte count. The content is not copied into the packet or the fact. |
| `old_text` longer than 4_000 chars | Packet stores the first 4_000 of `old_text` and of `new_text`. The file edit already happened with the full strings. |
| No user message on the conversation | Do not set `_compile`. |
| `project_path` is `None` | `record_enforcement_fact` is called with `""`. `get_head_sha` fails. No fact. Compile still happens. |
| Not a git repo | Same: no fact. Compile still happens. |
| Untracked new file named by a fact | `diff` stdout is empty and `status_porcelain` contains the path, so the fact is dropped. |
| `facts.json` is corrupt | `load_facts` returns `[]`. The next `record_fact` overwrites with a one-element list. Log the parse error. |
| Two agents record at once | `_FACTS_LOCK` serializes read-modify-write. |
| `context.md` write that is itself a sensitive-path approval | Approval may already have been granted by the runtime. The intercept still refuses a full-file replace. |
| `append_project_context` hits the 50-entry cap | Existing FIFO. The reload check looks for the new heading, which is kept (the new entry is appended after eviction). |
| Compile active and the model calls `read_file` | The read is appended after `after` and is included in the next tail. Compiling does not forbid new reads. |
| GLib `on_status` arrives late | UI status is unchanged. Compile does not read that callback. |

---

## 8. ARCHITECTURE.md updates required

After Stage B:

- `utils/project_awareness.py` also owns `.crabcakes/facts.json`: one-line evidence, files, sha, session. Live facts are what `PROJECT_MEMORY` carries. `context.md` remains the diary and still supplies `CURRENT_TASK`.
- `agent/runtime.py` after an enforced write sends a compiled message list (system, goal plus hunk plus checks plus live facts, then messages appended after that write). `conv.messages` is not trimmed by this path.

---

## 9. Self-audit

1. `check`'s real signature is `(tool_name, tool_args, tool_result, project_path, config, verbose=False)`. The spec does not call it from new code. The middleware already calls it.
2. `get_head_sha` and `diff_file_against_working_tree` return `GitResult` and catch `Exception`. Samples check `success` and do not wrap them in another try for those exceptions. `diff_file_against_working_tree` rejects a bad sha without raising.
3. `append_project_context` returns `None` and swallows `OSError`. The spec reloads `context.md` and fails the tool if the heading is missing. That return value is not ignored by accident.
4. `is_sensitive_path` is true for `.crabcakes/`. The intercept does not assume the write was unguarded.
5. `_dispatch` is idle-queued. The compile state is set on the tool thread from `result.enforcement_checks`, not from `on_status`.
6. The budget guard today can reject a turn the compiled payload would have fit. The spec moves that comparison onto the compiled list when `_compile` is set.
7. An implementer who ships Stage A and stops has a working compiled turn and an unchanged `PROJECT_MEMORY`. Stage B is the fact file and the prompt swap.
