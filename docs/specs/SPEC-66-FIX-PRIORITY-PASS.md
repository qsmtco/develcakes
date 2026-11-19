# SPEC-66: Fix Priority Pass

**Date:** 2026-11-18
**Author:** Spec draft from the 18 Nov 2026 fixes list
**Status:** Draft — for implementation
**Implements:** The ordered fixes in the 18 Nov 2026 review. Features from that review (remember the room, ask-on-unsure, source view, turn snapshot, vision, a person-owned terminal, dream proposals, branch and push) are not this spec.
**Depends on:** SPEC-47 (acceptance rows). SPEC-64 row R-9 and SPEC-65 row R-10 are repaired here; their behavior is not redesigned except for the `<think>` rule in §2.8.
**Target branch:** main
**Spec authoring prompt:** `prompts/steelFramedSpecWriter.md`

> Architecture compliance: `agent/` and `utils/` stay free of GTK. `models/command.py` stays a pure dataclass. `CollabHandler` does not import the runtime. `ui/window.py` is the only place that wires a cancel callback into `ChatHandler` and `AgentCommandHandler`. `utils/git_ops.py` does not import `agent/`. Conversation files stay under `get_config_dir()/conversations/`, mode `0o600`, temp file plus `os.replace`. Prompt text from a project file enters the system prompt only through `utils.prompt_loader._untrusted_fence`.

---

## DISCOVERY

Starting Spec Discovery — reading all referenced source files before writing any spec content.

- Read `agent/tools.py`:
  - `_resolve_project_path(path, project_path) -> str | None` (`:419`). Absolute and relative inputs are `realpath`'d. Return is the absolute path when `commonpath` equals the project realpath, else `None`. `ValueError` from `commonpath` returns `None`.
  - `is_sensitive_path(path: str) -> bool` (`:374`) matches the raw string. Prefixes are `.git/`, `.crabcakes/`, `.github/`. Globs are `Makefile`, `*.toml`, `*.yml`, `*.yaml`, `*hook*`, `*venv*`. A leading-dot basename is sensitive. `./.crabcakes/enforcement.json` does not start with `.crabcakes/`.
  - `_write_file` (`:628`) and `_edit_file` (`:803`) call `is_sensitive_path` on the model string after a successful resolve (`:635`, `:837`).
  - `_get_approval(session_key, tool_name, arguments, *, approval_callback=None) -> bool` (`:142`). No callback or any exception returns `False`.
  - `_is_web_fetch_restricted() -> bool` (`:1759`) is true only when `get_env("WEB_FETCH_RESTRICT") == "1"`.
  - `_reject_restricted_url(url) -> ToolResult | None` (`:1766`) returns `None` immediately when the flag is off. When on, it refuses loopback, link-local, `is_private`, `is_loopback`, and `is_link_local`. It does not check `is_unspecified` (`0.0.0.0`). `socket.gaierror` and any other resolution exception return a failed `ToolResult`.
  - `_run_grep` (`:1154`) calls `subprocess.run` twice (`:1191`, `:1205`) with no `env=`. `TimeoutExpired` is not caught. `FileNotFoundError` on `rg` falls back to `grep`.
  - `cancel_all_processes(session_key: str | None = None) -> tuple[int, int]` (`:254`) pops the registry under `_PROCESS_REGISTRY_LOCK`, then group-kills. A second call for the same key returns `(0, 0)`.
- Read `agent/enforcement.py`:
  - `_ALLOWED_BINARIES` (`:60`) includes `python3` and `node`.
  - `_validate_test_command(command: str | None) -> bool` (`:176`) checks the first whitespace token's basename only. `python3 -c '...'` returns `True`.
  - `_parse_command_to_argv(command: str) -> list[str]` (`:875`) is `shlex.split`, or `str.split` on `ValueError`.
  - `check(...)` (`:1546`) runs only after a successful `write_file` or `edit_file`.
- Read `agent/runtime.py`:
  - `cancel(self, session_key: str) -> None` (`:1622`) marks a live loop, flushes approvals, dispatches `on_error`. It does not call `cancel_all_processes`.
  - `cancel_peer` (`:1684`) marks and flushes. It does not kill.
  - `stop_all` (`:1708`) calls `cancel(sk)` then `cancel_all_processes(sk)` and records `killed` from that second call.
  - The coder tool batch (`:2928`) does `dataclasses.replace(enf, lint_check=False, test_run=False)` when `conv.agent_role == "coder"`.
  - Empty post-tool follow-up completes with accumulated prose at `:2384-2416` when `turn_text_segments` is non-empty. That is the R-9 claim. `tests/mutations/R-9.diff` is not in the tree.
  - `load_conversation(self, session_key: str) -> bool` (`:5227`) calls `load_conversation_from_disk(session_key)` and stores under `_conversations[session_key]`.
  - `save_conversation` (`:5218`) calls `save_conversation_to_disk(conv, session_key)`.
- Read `agent/persistence.py`:
  - `save_conversation_to_disk(conv, session_key) -> str` (`:42`) writes `conversations_dir() / f"{session_key}.json"` via `os.open(..., 0o600)`, `fsync`, `os.replace`. The JSON includes `project_path`.
  - `load_conversation_from_disk(session_key) -> tuple[Conversation, dict] | None` (`:246`). Bad JSON or `OSError` returns `None`. `_conversation_from_data` (`:155`) sets `project_path=None` and `system_prompt=""` regardless of the file. The raw `data` dict still has `project_path`.
  - Imports today: `json`, `logging`, `os`, `re`, `datetime`. No `hashlib`.
- Read `utils/env_security.py`: `get_scrubbed_env() -> dict[str, str]` (`:38`). No raise. Keys are the `ALLOWED_SUBPROCESS_ENV_VARS` frozenset (`:26`).
- Read `utils/config.py`: `get_env(name)` (`:44`) returns `DEVELCAKES_<name>` when the variable is present, including `""`. Else `CRABCAKES_<name>`.
- Read `utils/prompt_loader.py`: `_untrusted_fence(content: str, source: str) -> str` (`:110`) interpolates `content` unchanged. A body containing `</untrusted-project-data>` closes the fence early. `compose_system_prompt` fills `{{CURRENT_TASK}}` and `{{WORKFLOW_STATUS}}` from the awareness dict. It lazy-imports `project_awareness` only inside the function (`:257`).
- Read `utils/project_awareness.py`: `get_current_task(project_path) -> str` (`:864`) returns the last `## ` heading with the prefix removed. The docstring and the call site (`:1215-1219`) assign it to `CURRENT_TASK` with no fence. `WORKFLOW_STATUS` (`:1221-1237`) is the raw `workflow.md` text, tail-truncated to `WORKFLOW_STATUS_MAX_CHARS` (2000). Missing workflow and the exception path are the literal strings `(workflow.md not yet initialized)` and `(workflow state unavailable)`.
- Read `prompts/system/project-awareness.md`: `{{WORKFLOW_STATUS}}` at line 13, `{{CURRENT_TASK}}` at line 30, under the heading `## Current Task`.
- Read `utils/git_ops.py`: `stage_all(project_path) -> GitResult` (`:98`) runs `repo.git.add("-A")`. `commit(project_path, message, allow_empty=False, agent_trailer=None) -> GitResult` (`:109`) commits the whole index. There is no path-limited stage. The module imports `re`, `git`, `dataclass`, `Optional`. It does not import `os`. Exceptions become `GitResult(success=False, error=_safe_error(e))`. `_safe_error` does not raise.
- Read `ui/feed/review.py` `handle_accept` (`:26`): git-backed types call `stage_all`, then `commit` of whatever is staged. Missing `project_path`, a failed stage, an empty diff, and `ImportError` on `git` return after a log line. The card stays unaccepted. `card.file_path` is only used to fill `_recent_git_paths` (`:112`).
- Read `render/feed_html.py`: `_main_body` (`:378`) escapes `file_path` and body. `_classes` (`:322`) adds `feed-card-error` when `metadata["status"] == "error"`. No accept-failure line exists.
- Read `ui/handlers/feed_handler.py`: `update_card(self, card_id, card_data) -> None` (`:610`). `_update_card_visual(self, card_id, accepted: bool)` (`:1143`) sets `card.accepted` and upserts.
- Read `models/command.py`: `CommandResult` (`:66`) fields are `handled`, `response_text`, `response_card`, `forward_to`, `forward_text`, `broadcast_targets`. No cancel field.
- Read `ui/handlers/collab_handler.py`: `cmd_stop` (`:45`) returns `forward_to=target`, `forward_text="stop"`.
- Read `ui/handlers/chat_handler.py` `on_send` (`:230`): a handled result with `forward_to` and `forward_text` echoes and calls `_send_local`.
- Read `ui/handlers/agent_command_handler.py`:
  - `_strip_thinking_blocks` (`:818`) removes balanced `<think>` blocks, then `re.sub(r"<think\b[^>]*>.*$", "", ...)` deletes from an unclosed tag to the end of the string.
  - `_sanitize_for_command_scan` (`:826`) calls that strip before fence unwrapping.
  - `on_agent_response` routes only `forward_to` / `broadcast_targets` (`:444`).
  - `tests/test_spec65_ask_command_hardening.py:67` requires `_cmds("<think>\n/ask @Coder \"never closed") == []`.
  - `tests/mutations/R-10.diff` deletes `results.sort` and the `_strip_thinking_blocks` call. The test file itself says a forward scan is already ordered, so the sort deletion does not fail `TestParserAndOrdering`.
- Read `ui/views/chat_surface.py`: `_ensure_webview` (`:997`) sets `set_enable_javascript(live)` before `LiveGuard.compile`. `_on_live_guard_compiled` (`:1061`) disables JavaScript when `content_filter is None`. When the filter exists it calls `attach` and ignores the `bool`.
- Read `utils/live_guard.py`: `attach(self, view) -> bool` (`:153`). `self._filter is None`, `add_filter` failure, and `decide-policy` connect failure return `False`. No exception escapes.
- Read `ui/handlers/telegram_bridge_handler.py`: `_send` (`:733`) and `forward_to_phone` (`:787`) call `self._transport.send_message` on the caller thread. `on_supervisor_reply` (`:743`) calls `forward_to_phone` directly. Inbound `_send` calls run from `_handle_update`, which production reaches through `self._dispatch` (`GLib.idle_add`). `send_message` is `TelegramTransport.send_message(self, text, reply_markup=None, ...)` (`transport/telegram.py:237`).
- Read `ui/handlers/agent_runtime_handler.py`: `stop_all_agents` (`:421`) calls `rt.stop_all()`. There is no single-session cancel method. Runtimes are keyed by `agent_def.display_name`. Agents are keyed by session key in `self._agents`.
- Read `agent/config.py`: `EnforcementConfig.test_run` defaults `True` (`:60`). `lint_check` defaults `True`.
- Read `pyproject.toml`: `packages.find` include is `ui*`, `agent*`, `utils*`, `models*`, `prompts*`, `transport*` (`:70`). `render/__init__.py` exists. `prompts/` has no `__init__.py`, so `prompts*` matches nothing. `utils/config.py` `get_project_root` (`:398`) documents that a non-editable install only has bundled files that are declared package data. `prompt_loader.SYSTEM_DIR` is `dirname(dirname(__file__))/prompts/system`.
- Read `ui/views/left_panel.py:97-99`: the Agents placeholder markup is `Click Connect to discover agents`.
- Read `knowledge/setup.md:1-37`: title and commands still say CrabCakes, `~/.config/crabcakes/providers.yaml`, and a `local-kb` / Auxilium seed.
- Architecture owners: path approval and fetch/search are `agent/tools.py`. Enforcement command policy is `agent/enforcement.py`. Turn cancel is `AgentRuntime.cancel`. Disk transcripts are `agent/persistence.py`. Awareness strings are `utils/project_awareness.py`. The fence is `utils/prompt_loader.py`. Accept is `FeedReviewMixin.handle_accept`. Slash results are `CommandResult`, interpreted by `ChatHandler` and `AgentCommandHandler`, wired in `ui/window.py`. Live JS is `ChatSurface`. Phone HTTP is `TelegramBridgeHandler`.

---

## 1. Overview

### Problem

Eleven current behaviors are wrong. They are ordered by what they can do to the machine, then by the gestures a person already uses.

1. A write to `./.crabcakes/enforcement.json` or `foo/../.crabcakes/enforcement.json` skips approval. The enforcement command's first token may be `python3`, so `python3 -c` is an allowed argv and a later write runs it.
2. The last `##` heading of `context.md` is injected as `CURRENT_TASK` with no fence. `workflow.md` is injected whole. The fence does not escape its own closing tag.
3. `search_files` runs `rg` or `grep` with the app environment. `web_fetch` reaches private and link-local hosts unless an env flag is set, and `0.0.0.0` is not refused when the flag is on.
4. Live JavaScript is enabled before the content filter attaches. `attach` returning `False` leaves JavaScript on.
5. One conversation file per session key is reused for every project. Switching projects rebuilds the system prompt and keeps the messages.
6. Accept on a git card runs `git add -A` and commits the index. Failure is a log line.
7. `/stop` sends the word `stop`. `cancel` does not kill the process. Only `stop_all` does.
8. An unclosed `<think>` deletes the rest of the reply from command scanning, so a later `/ask` never runs. R-9 has no mutation file. R-10's mutation deletes a sort the test does not depend on.
9. Role `coder` forces `test_run=False` on every tool call. The shell allowlist stays as it is.
10. Telegram `send_message` runs on the thread that called `_send` or `forward_to_phone`. Production inbound dispatch is the GTK main thread. Supervisor replies are forwarded on the caller thread.
11. `render` is imported by the chat, the feed, and Telegram, and is not a setuptools package. `prompts/system` is loaded from the sibling of `utils/` and is not package data. The Agents tab and `knowledge/setup.md` still describe the removed gateway product.

### Solution summary

1. Run `is_sensitive_path` on the project-relative path of the resolved file. Refuse `python3`/`python` argv that contains the token `-c`, and `node` argv that contains `-e` or `--eval`, inside `_validate_test_command`.
2. Escape fence tags inside the body. Put file-derived `CURRENT_TASK` and `WORKFLOW_STATUS` through `_untrusted_fence`.
3. Pass `env=get_scrubbed_env()` to both `subprocess.run` calls in `_run_grep`. Treat fetch as restricted unless `DEVELCAKES_WEB_FETCH_RESTRICT` is `0`. Also refuse `is_unspecified`.
4. Leave JavaScript off until `attach` returns `True`. A false return disables JavaScript the same way a missing filter already does.
5. Store `conversations/<session_key>__<sha256(realpath)[:16]>.json`. Loading project B does not read project A's messages. A legacy `<session_key>.json` is read only when its stored `project_path` realpath equals the project being opened, then the next save writes the scoped file. The legacy file is not deleted.
6. Accept stages `card.file_path` only. If the index contains any other staged path, do not commit. Failure sets `metadata["accept_error"]` and `metadata["status"] = "error"` and redraws the card.
7. `CommandResult.cancel_session_key` makes `/stop` call `AgentRuntime.cancel`. `cancel` calls `cancel_all_processes` after the cancel mark and returns `(killed, unkillable)`. `stop_all` uses that return and does not kill a second time.
8. An unclosed `<think>` swallows through end of string only when the first non-whitespace of the string is `<think`. A later mention does not. Rewrite `R-10.diff` so it deletes the strip call only. Add `R-9.diff` that removes the `turn_text_segments` completion branch.
9. The coder replace sets `lint_check=False` only.
10. `_send` and `forward_to_phone` enqueue one daemon worker. `flush_outbound()` joins it for tests.
11. Add `render*` to package discovery, add `prompts/__init__.py` plus package data for `system/*.md`, replace the Agents placeholder, and correct the install section of `knowledge/setup.md`.

### Scope

| In | Out |
|---|---|
| The eleven fixes above, their tests, and rows R-12–R-22 | The feature list: layout restore, JEV ask-the-person, source view, turn snapshot, image inputs, a person-owned terminal, dream proposals, branch and push |
| Repair of mutation files R-9 and R-10 | Redesigning SPEC-64 turn text or SPEC-65 parsing beyond the `<think>` rule |
| `stage_paths` in `git_ops` | Changing `project_handler`'s `stage_all` on project create |
| Default-on fetch restriction and `is_unspecified` | A custom HTTP transport that pins DNS. The rebind window stays documented |
| Coder `test_run` left at the config default | Widening `_coder_shell_allowed` |
| Packaging `render` and `prompts/system` | Rewriting the rest of `knowledge/` or `docs/ARCHITECTURE.md`'s gateway chapters |

### Principles

1. A resolved path is the path the approval decision sees.
2. Bytes from a project file are fenced before they are substituted into the system prompt. Strings this process invented (`(no current task recorded)`, `(workflow.md not yet initialized)`, `(workflow state unavailable)`) stay unfenced.
3. A subprocess that returns output to the model uses `get_scrubbed_env()`.
4. Live JavaScript and the content filter are one state. JavaScript is off whenever the filter is not attached.
5. A session key plus a project realpath is the conversation identity. The in-memory dict stays keyed by session key. Switching projects drops that entry when the loop is idle.
6. Accept commits the card's path or it commits nothing.
7. Cancel marks the turn, then kills the process group. `/stop` is that cancel.
8. The shell allowlist for Coder is unchanged.

---

## 2. Changes by file

### 2.1 `agent/tools.py` — approval path, search env, fetch default

`_write_file`, after `resolved` is not `None` and before `_get_approval`:

```python
rel = os.path.relpath(resolved, os.path.realpath(project_path)).replace("\\", "/")
if is_sensitive_path(rel):
    approved = _get_approval(
        session_key, "write_file", {"path": rel}, approval_callback=approval_callback
    )
```

Use `rel` in the error string too. `_edit_file` does the same with tool name `"edit_file"`. Do not call `is_sensitive_path` on the original `path`. `is_sensitive_path` itself stays the prefix/glob/leading-dot function it is. Callers that already pass a normalized relative path keep working.

`_run_grep`: both `subprocess.run` calls gain `env=get_scrubbed_env()`. Import is the existing function-level style:

```python
from utils.env_security import get_scrubbed_env
```

`TimeoutExpired` still propagates. `FileNotFoundError` still falls back from `rg` to `grep`. The fallback `subprocess.run` also gets the scrubbed env.

`_is_web_fetch_restricted`:

```python
def _is_web_fetch_restricted() -> bool:
    """True unless DEVELCAKES_WEB_FETCH_RESTRICT is the string \"0\".

    Absence is restricted. An empty string is present (utils.config.get_env)
    and is not \"0\", so it is restricted. CRABCAKES_WEB_FETCH_RESTRICT is
    the one-release fallback inside get_env.
    """
    return get_env("WEB_FETCH_RESTRICT") != "0"
```

Inside the address loop in `_reject_restricted_url`, after the existing `is_link_local` check, add:

```python
if ip.is_unspecified:
    return ToolResult(
        success=False,
        error=f"MED-3: Refusing unspecified address: {url} → {ip}",
    )
```

`ipaddress.ip_address(...).is_unspecified` is `True` for `0.0.0.0` and `::`. Those values are not `is_private`, `is_loopback`, or `is_link_local`. No new exception type. DNS rebind stays the limitation already written at `:1770`. This spec does not add a pinning transport.

### 2.2 `agent/enforcement.py` — refuse interpreter eval flags

Extend `_validate_test_command`. Keep the first-token allowlist. After it passes, `shlex.split` the original command. `ValueError` returns `False`. Then:

```python
base = os.path.basename(argv[0]).lower()
flags = argv[1:]
if base in {"python", "python3"} and "-c" in flags:
    return False
if base == "node" and ("-e" in flags or "--eval" in flags):
    return False
```

`python3 -m pytest` has no `-c` token and stays valid. A basename like `python3.12` is not in this set; the existing first-token check already rejects it because it is not in `_ALLOWED_BINARIES`. Do not catch anything except `shlex.ValueError`. The three call sites that already consult `_validate_test_command` (`:1232`, `:1248`, `:1257`) pick up the new refusal with no further edit.

### 2.3 `utils/prompt_loader.py` — escape the fence

`_untrusted_fence` runs this on `content` before interpolation. `source` stays a caller-supplied literal (`context.md`, `workflow.md`). Do not pass user text as `source`.

```python
def _break_fence_tags(content: str) -> str:
    return re.sub(
        r"(?i)</?\s*untrusted-project-data\b",
        lambda m: m.group(0).replace("<", "&lt;", 1),
        content,
    )
```

`re` is already imported. The wrapper tags are written after `_break_fence_tags`. The trailing instruction sentence stays.

### 2.4 `utils/project_awareness.py` — fence the two file fields

Lazy-import inside `build_awareness_dict`, next to the current-task block, so module import does not cycle (`prompt_loader` imports this module only inside `compose_system_prompt`):

```python
from utils.prompt_loader import _untrusted_fence
```

`get_current_task` is unchanged. The assignment changes:

```python
task = get_current_task(project_path)
if task:
    parts["CURRENT_TASK"] = _untrusted_fence(task, "context.md")
else:
    parts["CURRENT_TASK"] = "(no current task recorded)"
```

Workflow: truncate first, then fence the file text. The two invented strings stay as they are.

```python
shown = wf if len(wf) <= WORKFLOW_STATUS_MAX_CHARS else (
    wf[-WORKFLOW_STATUS_MAX_CHARS:] + "\n[... workflow status truncated ...]"
)
parts["WORKFLOW_STATUS"] = _untrusted_fence(shown, "workflow.md")
```

`prompts/system/project-awareness.md` is not edited. The template still says `## Current Task` and `{{CURRENT_TASK}}`. The substituted value is the fence.

### 2.5 `agent/persistence.py` — one file per session and project

Add `import hashlib`.

```python
_SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]+$")

def conversation_store_name(session_key: str, project_path: str | None) -> str:
    """Basename of the conversation file. Raises ValueError on a bad key.

    session_key must match ``[A-Za-z0-9._:-]+`` and must not contain ``..``.
    The slug is 16 hex chars of sha256(realpath(project_path)), or ``none``
    when project_path is empty. The separator is ``__``.
    """
    if (
        not isinstance(session_key, str)
        or not _SESSION_KEY_RE.fullmatch(session_key)
        or ".." in session_key
    ):
        raise ValueError(f"refusing session key for a filename: {session_key!r}")
    if not isinstance(project_path, str) or not project_path:
        slug = "none"
    else:
        slug = hashlib.sha256(
            os.path.realpath(project_path).encode("utf-8")
        ).hexdigest()[:16]
    return f"{session_key}__{slug}.json"
```

`save_conversation_to_disk` uses `conversation_store_name(session_key, conv.project_path)` instead of `f"{session_key}.json"`. `ValueError` propagates to the existing `except Exception` in `_auto_save` (`agent/runtime.py` around the auto-save call), which logs and does not raise. `save_conversation` still raises `ValueError` when the conversation is missing; a bad key now also raises `ValueError` from `conversation_store_name`. That is the same exception type the method already documents.

`load_conversation_from_disk(session_key: str, project_path: str | None = None)`:

1. Compute the scoped name. `ValueError` from `conversation_store_name` propagates.
2. If that file exists, load it (current JSON / `OSError` behavior: return `None`).
3. If it does not, and `project_path` is non-empty, open the legacy `f"{session_key}.json"` when that path is a regular file. If `os.path.realpath` of `data.get("project_path")` equals `os.path.realpath(project_path)`, return that conversation. A missing, non-string, or different `project_path` returns `None`. Do not delete the legacy file.
4. `project_path is None` reads only `{session_key}__none.json`. It does not read the legacy file.

The runtime, after a legacy hit, calls `save_conversation_to_disk` so the scoped file exists before the next launch. Failure to save is logged and the in-memory conversation is still used.

### 2.6 `agent/runtime.py` — load by project, cancel kills, coder tests

`load_conversation(self, session_key: str, project_path: str | None = None) -> bool` passes `project_path` through. On a legacy hit (`scoped file was absent`, which the loader can report by a third return or by comparing paths — implement as: `load_conversation_from_disk` returns the pair, and the runtime saves when the scoped path was missing and the load succeeded). Simplest contract: `load_conversation_from_disk` returns `tuple[Conversation, dict, bool]` where the bool is `migrated_from_legacy`. Update the one production caller and the tests that unpack the pair. If a test unpacks two values, update that test in the same change. Do not leave a two-value return.

New method:

```python
def release_if_other_project(self, session_key: str, project_path: str) -> bool:
    """Drop the in-memory conversation when it is bound to another project.

    Returns True when an entry was removed. Returns False when the session
    is absent, the loop is active, or the realpaths match. Does not delete
    the file. Callers that hold ``_prep_lock`` must not call this; it takes
    ``self._lock`` only.
    """
```

Compare `os.path.realpath` of `conv.project_path` to `os.path.realpath(project_path)`. `conv.project_path` is `None` for a conversation loaded and not yet rebuilt: treat `None` as not matching a non-empty `project_path`, so the stale object is dropped and the next send loads the scoped file. Do not pop when `session_key in self._active_loops`.

`cancel` returns `tuple[int, int]`. After the existing body, outside `self._lock`:

```python
from agent.tools import cancel_all_processes
return cancel_all_processes(session_key)
```

`cancel_peer`, after releasing `self._lock`, calls `cancel_all_processes(session_key)` only when the session was in `_active_loops` at the start of the critical section. Capture that boolean inside the lock. Ignore the return. An idle peer is not killed.

`stop_all` replaces the pair of calls with:

```python
killed, unkillable = self.cancel(sk)
```

The outcome strings (`killed:N`, `N-unkillable`) stay. A second `cancel_all_processes` would see an empty registry and report `killed:0`.

Coder batch, replace only lint:

```python
if getattr(conv, "agent_role", "") == "coder":
    enf = dataclasses.replace(enf, lint_check=False)
```

`test_run` stays whatever `self._config.enforcement.test_run` is (default `True`). `_coder_shell_allowed` is not edited.

### 2.7 `ui/handlers/agent_runtime_handler.py`

`set_active_project`, in the loop that already looks up `rt` and `conv`: when `conv` is not `None` and the prep lock is acquired, call `rt.release_if_other_project(sk, project_path)` before `_rebuild_conversation_context`. If it returns `True`, skip the rebuild. The next `send_to_special_agent` loads the scoped file. The existing non-blocking lock behavior stays: if the prep lock is held, skip, and the in-flight turn keeps the project it started with.

`_prepare_turn_conversation` already calls `rt.load_conversation(session_key)`. Pass the active `project_path` (the project root, not the worktree cwd). The worktree cwd remains the conversation's `project_path` for tool sandboxing after load, via the existing assignment. The store slug uses the project root so a worktree turn and a root turn share one transcript. Pass `project_path` (the argument), not `effective_project_path`, into `load_conversation`.

```python
def cancel_agent(self, session_key: str) -> str:
    """Cancel one local agent and kill its registered processes.

    Returns ``unknown`` when the session key is not in ``self._agents``,
    ``no_runtime`` when that agent has no runtime yet, otherwise
    ``cancelled``, ``cancelled+killed:N``, or with an unkillable suffix.
    ``rt.cancel`` returns ``(killed, unkillable)``.
    """
```

### 2.8 `models/command.py`, `collab_handler.py`, `chat_handler.py`, `agent_command_handler.py`, `window.py`

Add to `CommandResult`:

```python
cancel_session_key: str | None = None
```

Default `None`. Existing constructors stay valid.

`CollabHandler.cmd_stop`:

```python
def cmd_stop(self, cmd: Command) -> CommandResult:
    if not cmd.target_session_key:
        return CommandResult(
            handled=True,
            response_text="No target agent. Usage: /stop @agent",
        )
    return CommandResult(
        handled=True,
        cancel_session_key=cmd.target_session_key,
        response_text=f"Stopping {cmd.target_session_key}",
    )
```

No `forward_to`. No `forward_text`.

`ChatHandler.set_on_cancel_session(self, cb) -> None` stores the callback. In `on_send`, when `result.handled` and `result.cancel_session_key`, call the callback, clear the input, show `result.response_text` through the existing command-text path if one is wired, and do not call `_send_local`. A missing callback shows the response text `Stop is not wired.` and does not forward.

`AgentCommandHandler.set_on_cancel_session(self, cb) -> None`. In the `parsed_commands` loop, before the `forward_to` branch: if `result.cancel_session_key` and the callback is set, call it and count the command. Do not `_route_to_target`. Tests that construct the handler with `__new__` must use `getattr(self, "_on_cancel_session", None)`.

`ui/window.py` wires both setters to `self._agent_runtime_handler.cancel_agent` after both objects exist. No handler imports another handler.

`_strip_thinking_blocks`: keep the balanced `re.sub`. Replace the second substitution with:

```python
if text.lstrip().lower().startswith("<think"):
    text = re.sub(
        r"<think\b[^>]*>.*$",
        "",
        text,
        count=1,
        flags=re.DOTALL | re.IGNORECASE,
    )
```

A string whose first non-whitespace is `<think` still loses the rest, which is what `test_think_fence_html_and_unquoted_are_dropped_or_unwrapped` asserts for `"<think>\n/ask @Coder \"never closed"`. A string that has other text first keeps everything after a later `<think>` tag, so `/ask` on a following line is scanned. Balanced pairs are still removed anywhere.

### 2.9 `utils/git_ops.py` and `ui/feed/review.py` and `render/feed_html.py`

Add `import os` to `git_ops.py`.

```python
def stage_paths(project_path: str, paths: list[str]) -> GitResult:
    """Stage exactly these project-relative or absolute paths.

    A path that realpath's outside project_path returns success=False and
    stages nothing. An empty list returns success=False, error=\"no paths\".
    Does not call ``git add -A``. Any exception becomes ``_safe_error``.
    """
```

Use `os.path.realpath` and `os.path.commonpath`. `ValueError` from `commonpath` is caught by the existing `except Exception`. Then `repo.git.add("--", *safe)`.

`handle_accept` for git-backed cards:

- No `project_path`: call the failure path below. Do not return silently.
- No `card.file_path`: failure `this card has no file path`. Do not stage.
- `stage_paths(project_path, [card.file_path])`. On `success=False`, failure with `result_stage.error`.
- Open the repo the way the function already does. If `repo.index.diff("HEAD")` contains any path other than the staged relative path, do not commit. Failure `index has other staged files`. Leave those files staged. Do not `git reset`.
- Otherwise `git_ops.commit` as today.
- On commit failure, failure with `result_commit.error`. On success, the existing accepted path stays.

Failure helper, run on the main thread because `update_card` upserts the document:

```python
def _fail(message: str) -> None:
    def _apply() -> None:
        card.metadata["accept_error"] = message
        card.metadata["status"] = "error"
        self.update_card(card_id, card)
    if self._GLib is not None:
        self._GLib.idle_add(_apply)
    else:
        _apply()
```

`_main_body` appends, after the body, when `meta.get("accept_error")`:

```python
parts.append(
    f'<p class="feed-accept-error">{_esc(meta.get("accept_error"))}</p>'
)
```

`_esc` is already the feed escaper. `_classes` already adds `feed-card-error` for `status == "error"`.

`stage_all` remains for project creation. `handle_accept` does not call it.

### 2.10 `ui/handlers/telegram_bridge_handler.py`

Add `import threading`. In `__init__`, `self._send_thread: threading.Thread | None = None` and `self._send_lock = threading.Lock()`.

```python
def _deliver(self, chunks: list[str], chat_id: int | None) -> None:
    """Send chunks on a daemon thread. Order inside one call is preserved.

    send_message exceptions are logged with redact_log_preview and do not
    propagate. The return value of send_message is ignored. flush_outbound
    joins the last thread this method started.
    """
    def _work() -> None:
        with self._send_lock:
            transport = self._transport
            if transport is None:
                return
            for chunk in chunks:
                try:
                    if chat_id is None:
                        transport.send_message(chunk)
                    else:
                        transport.send_message(chunk, chat_id=chat_id)
                except Exception as e:  # noqa: BLE001
                    _logger.warning(
                        "bridge send failed: %s", redact_log_preview(str(e))
                    )
    thread = threading.Thread(target=_work, name="telegram-send", daemon=True)
    self._send_thread = thread
    thread.start()

def flush_outbound(self) -> None:
    thread = self._send_thread
    if thread is not None:
        thread.join(timeout=5)
```

`_send` becomes `_deliver([text], None)`. `forward_to_phone` builds the chunk list it already builds, then one `_deliver(chunks, None)`. `_send_refusal` uses `_deliver([message], chat_id)`. Do not call `send_message` on the GTK thread. The lock serializes deliveries. A 5 second join timeout means a hung socket does not hang `flush_outbound` forever. Tests that assert a fake transport received the text call `flush_outbound()` first. `tests/test_telegram_bridge_handler.py` is the file. Update each assertion that reads the fake immediately after a send.

### 2.11 `ui/views/chat_surface.py`

In `_ensure_webview`, when `live` is true, call `set_enable_javascript(False)` before `compile`. Do not enable it there. In `_on_live_guard_compiled`:

```python
if content_filter is None or self._live_guard is None or self._webview is None:
    # existing disable path
    return
if not self._live_guard.attach(self._webview):
    logger.error(
        "live_guard: attach returned False — disabling live JS"
    )
    try:
        self._webview.get_settings().set_enable_javascript(False)
    except Exception:
        logger.debug("live_guard: could not disable JS", exc_info=True)
    self._live_guard = None
    return
self._webview.get_settings().set_enable_javascript(True)
```

`attach` catches its own failures and returns `False`. `set_enable_javascript` can raise; that `Exception` is logged and swallowed, matching the compile-failure branch. JavaScript stays off in that case.

### 2.12 Packaging and the two stale strings

`pyproject.toml` include becomes:

```toml
include = ["ui*", "agent*", "utils*", "models*", "prompts*", "transport*", "render*"]
```

Add `prompts/__init__.py` whose body is a one-line docstring. Add:

```toml
[tool.setuptools.package-data]
prompts = ["system/*.md"]
```

`SYSTEM_DIR` stays `dirname(dirname(prompt_loader.__file__))/prompts/system`. An installed `prompts` package is a sibling of `utils` in site-packages, which is that path. An editable install from the repo is the same layout.

`ui/views/left_panel.py` placeholder markup becomes `No agents yet`. Do not mention Connect.

`knowledge/setup.md` first screen: title `How do I install Develcakes?`, clone and directory names `develcakes`, config path `~/.config/develcakes/providers.yaml`, start command `python3 main.py` from that checkout. Delete the sentence that says a `local-kb` provider is auto-seeded and that Auxilium answers with no API key. Leave the rest of `knowledge/` unchanged.

### 2.13 Mutations

`tests/mutations/R-9.diff` deletes the branch at `agent/runtime.py` that completes a turn when `turn_text_segments` is non-empty and the follow-up content is empty (`:2384` through the `return` of that block, so execution falls through to the existing `empty_content` failure). The clean node `tests/test_spec64_turn_text_streaming.py::TestPostToolEmptyFollowup::test_post_tool_empty_followup_completes_with_prior_prose_and_anthropic_valid_roles` must fail on the mutated tree and pass on the clean tree.

`tests/mutations/R-10.diff` is rewritten against current `ui/handlers/agent_command_handler.py`. It deletes only `text = cls._strip_thinking_blocks(text)` inside `_sanitize_for_command_scan`. It does not delete `results.sort`. `test_think_fence_html_and_unquoted_are_dropped_or_unwrapped` must then see two commands for the balanced-think fixture and fail.

### Files NOT changed

- `agent/runtime.py` `_coder_shell_allowed` — Coder's shell allowlist stays.
- `utils/git_ops.py` `stage_all` and `commit` signatures — project create still stages everything. Accept stops calling `stage_all`.
- `prompts/system/project-awareness.md` — placeholders stay; the values become fenced.
- `utils/live_guard.py` `attach` — its `False` returns are already correct. The caller changes.
- `transport/telegram.py` `send_message` — the thread boundary is in the handler.
- `agent/tools.py` exec blocklist and Coder shell allowlist.
- `docs/ARCHITECTURE.md` gateway chapters — this spec does not pretend to repair them. §9 lists the paragraphs this change adds.

---

## 3. Data flow

**Sensitive write.** Model calls `write_file` with `./.crabcakes/enforcement.json`. `_resolve_project_path` returns the real file inside the project. `os.path.relpath` yields `.crabcakes/enforcement.json`. `is_sensitive_path` is true. `_get_approval` runs. Denial returns the existing blocked `ToolResult` and the file is not opened for write.

**Enforcement command.** A later write of `a.py` calls `enforcement.check`. `_validate_test_command("python3 -c 'import os'")` splits to `["python3", "-c", "import os"]`, sees `-c`, returns `False`. The existing branch logs the warning and returns `None` for the test tier. The command is not executed.

**Prompt.** `build_awareness_dict` reads `context.md`, takes the last `## ` heading, and `_untrusted_fence` wraps it. `compose_system_prompt` substitutes that string into `{{CURRENT_TASK}}`. A heading that contains `</untrusted-project-data>` is stored with `&lt;` in place of the first `<` of that tag.

**Search.** `search_files` → `_run_grep` → `subprocess.run(..., env=get_scrubbed_env())`. Provider keys are not in that dict.

**Fetch.** `_web_fetch` calls `_reject_restricted_url`. With the env var unset, the function does not return `None` at the top. `http://169.254.169.254/` fails the link-local check. `http://0.0.0.0/` fails `is_unspecified`.

**Live JS.** First append creates the webview with JavaScript off and starts `compile`. The callback runs `attach`. `True` enables JavaScript. `False` or a `None` filter leaves it off.

**Project switch.** `set_active_project` calls `release_if_other_project`. The in-memory conversation for `agent:coder` is dropped when its `project_path` realpath differs and the loop is idle. The next send calls `load_conversation(session_key, project_path)`, which reads `agent:coder__<sha>.json` or a legacy file whose stored `project_path` matches.

**Accept.** Click Accept on a `diff` card. The worker calls `stage_paths` with `[card.file_path]`. If the index diff contains another path, `_fail` runs on the main thread, `update_card` upserts, and `_main_body` shows `accept_error`. `accepted` stays unset.

**Stop.** Person types `/stop @Coder`. `cmd_stop` returns `cancel_session_key="agent:coder"`. `ChatHandler` calls `cancel_agent`. That calls `runtime.cancel`, which marks the loop and `cancel_all_processes("agent:coder")`. The word `stop` is not sent.

**Think.** Scan text `See <think> in the parser.\n/ask @Debugger "real"`. The string does not `lstrip()`-startswith `<think`, so the unclosed rule does not run. The balanced rule finds no pair. `_extract_quoted_commands` sees the `/ask`. Scan text `<think>\n/ask @Coder "never closed"` still strips to empty.

**Telegram.** `on_supervisor_reply` → `forward_to_phone` → `_deliver` → daemon thread → `send_message`. The caller returns without waiting. Tests call `flush_outbound()`.

---

## 4. File change summary

| File | Change | Risk |
|---|---|---|
| `agent/tools.py` | Relative approval path, scrubbed grep env, fetch default | High. Approval and network defaults move. |
| `agent/enforcement.py` | `-c` / `-e` / `--eval` refusal inside the existing validator | Medium. A project that used `python3 -c` as its test command loses that command. |
| `utils/prompt_loader.py` | Escape fence tags | Low. |
| `utils/project_awareness.py` | Fence two fields | Medium. Prompts get longer. Agents see a fence where they saw an order. |
| `agent/persistence.py` | Scoped filenames, legacy match | High. Old files stop loading into the wrong project. |
| `agent/runtime.py` | Load arg, release, cancel return, coder `test_run` | High. `cancel` return type changes. Coder starts running tests. |
| `ui/handlers/agent_runtime_handler.py` | Release on switch, `cancel_agent`, load arg | Medium. |
| `models/command.py` | One optional field | Low. |
| `ui/handlers/collab_handler.py` | `/stop` returns a cancel key | Medium. Agents that expected the word `stop` in the chat stop seeing it. |
| `ui/handlers/chat_handler.py` | Honor cancel key | Medium. |
| `ui/handlers/agent_command_handler.py` | Honor cancel key, narrower unclosed-think rule | Medium. |
| `ui/window.py` | Wire the cancel callback twice | Low. |
| `utils/git_ops.py` | `stage_paths` | Medium. |
| `ui/feed/review.py` | Accept one path, show failure | High. Accept no longer commits the index. |
| `render/feed_html.py` | One error paragraph | Low. |
| `ui/handlers/telegram_bridge_handler.py` | Sender thread | Medium. Tests must flush. |
| `ui/views/chat_surface.py` | JS on only after attach | Medium. A frame of no-JS before the callback. |
| `pyproject.toml`, `prompts/__init__.py` | Package `render` and prompts | Medium. Editable installs must still resolve `SYSTEM_DIR`. |
| `ui/views/left_panel.py`, `knowledge/setup.md` | Copy | Low. |
| `tests/mutations/R-9.diff`, `R-10.diff` | Gate repairs | High. The acceptance job reads these. |
| `docs/acceptance/rows.yaml` | R-12–R-22, same commit as the tests | High. |

---

## 5. Implementation order

Each step is one commit-sized slice. Run the named test before the next slice. Do not land the `rows.yaml` entries until the node exists.

1. **Approval path and `-c`.** Tests for `./.crabcakes/`, `foo/../.crabcakes/`, and an absolute path. Test `_validate_test_command("python3 -c 'x'")` is `False` and `_validate_test_command("python3 -m pytest")` is `True`.
2. **Fence.** Test a heading that contains the closing tag and a workflow body. Assert both substituted values contain `<untrusted-project-data` and the literal closer appears only as the wrapper, once.
3. **Search env and fetch default.** Test `_run_grep` was called with the scrubbed mapping (patch `subprocess.run`). Test `_reject_restricted_url("http://0.0.0.0/")` fails with the env var unset, and that `DEVELCAKES_WEB_FETCH_RESTRICT=0` returns `None` from the restrict predicate.
4. **Live JS.** A fake `attach` returning `False` leaves `set_enable_javascript(False)`. A `True` return is the only path that enables it.
5. **Transcripts.** Two temp projects, one session key. Save under A, switch to B, assert B's load is `None` and A's messages are not in memory. Legacy file whose `project_path` matches A loads for A and writes the scoped name.
6. **Accept.** A dirty unrelated file plus a card path. Accept commits only the card path, or commits nothing and sets `accept_error` when another path is already staged. Assert `stage_all` is not called.
7. **Stop.** `/stop @Coder` does not call `_send_local`. `cancel_all_processes` is called with that session key. `stop_all` still reports `killed:N` when a process was registered.
8. **Think and mutations.** Prose-then-`/ask` extracts the ask. The leading unclosed fixture still extracts nothing. Write both diff files and run `python scripts/acceptance.py rows` for R-9 and R-10 only if the runner can select them; otherwise run the two pytest nodes mutated and clean by hand and then the full `rows` command.
9. **Coder tests.** A coder-role tool batch passes an enforcement config with `test_run is True`.
10. **Telegram.** A fake `send_message` records the thread name. The caller of `forward_to_phone` is not that thread. `flush_outbound` returns after the record exists.
11. **Package and copy.** `pip install` from a temp wheel or `python -c "import render, prompts"` against an isolated install. Placeholder string no longer contains `Connect`. `knowledge/setup.md` no longer contains `local-kb` or `CrabCakes`.

---

## 6. Acceptance criteria

- [ ] `write_file("./.crabcakes/enforcement.json")` asks for approval.
- [ ] `python3 -c` and `node -e` are rejected by `_validate_test_command`. `python3 -m pytest` is not.
- [ ] `CURRENT_TASK` and file-derived `WORKFLOW_STATUS` are inside `_untrusted_fence`. A closing tag in the file does not appear as a raw closer.
- [ ] Both grep subprocesses receive `get_scrubbed_env()`.
- [ ] Fetch is restricted when the env var is unset. `0.0.0.0` is refused. `DEVELCAKES_WEB_FETCH_RESTRICT=0` turns the check off.
- [ ] JavaScript is enabled only after `attach` returns `True`.
- [ ] Project B does not load project A's messages for the same session key.
- [ ] A matching legacy file is adopted once and saved under the scoped name. The legacy file remains on disk.
- [ ] Accept does not call `stage_all`. Another staged path blocks the commit and the card shows `accept_error`.
- [ ] `/stop @agent` cancels that session and kills its process group. The word `stop` is not forwarded.
- [ ] `stop_all` still reports a non-zero kill count when a process was registered.
- [ ] A `<think>` mention after other text does not hide a later `/ask`. A message that starts with `<think` and never closes still hides the command inside it.
- [ ] R-9.diff exists and fails its clean node when applied. R-10.diff fails the think fixture and does not delete `results.sort`.
- [ ] A coder tool call does not force `test_run=False`.
- [ ] `send_message` for a Supervisor reply does not run on the calling thread.
- [ ] `render` and `prompts.system` markdown are present in a non-editable install.
- [ ] The Agents placeholder does not say Connect. `knowledge/setup.md` does not say CrabCakes or local-kb.

---

## 7. Acceptance rows (SPEC-47)

The implementer adds these to `docs/acceptance/rows.yaml` in the same change as the tests. Nodes below are the tests this spec requires. They do not exist yet. Adding the yaml first turns the gate red.

| Row | Claim | Node | Mutation | Registered |
|---|---|---|---|---|
| R-12 | A relative or `..` path to `.crabcakes/enforcement.json` is sensitive after resolve, and `python3 -c` is not a valid test command. | `tests/test_spec66_approval_and_eval.py::test_resolved_sensitive_path_and_python_c` | `tests/mutations/R-12.diff` | 2026-11-18 |
| R-13 | `CURRENT_TASK` and `WORKFLOW_STATUS` from files are fenced, and a closer inside the file is escaped. | `tests/test_spec66_prompt_fence.py::test_task_and_workflow_are_fenced` | `tests/mutations/R-13.diff` | 2026-11-18 |
| R-14 | Grep uses the scrubbed env, and fetch refuses `0.0.0.0` when the restrict flag is unset. | `tests/test_spec66_net_and_search.py::test_scrubbed_grep_and_default_fetch` | `tests/mutations/R-14.diff` | 2026-11-18 |
| R-15 | JavaScript stays off when `attach` returns False. | `tests/test_spec66_live_js.py::test_attach_false_leaves_js_off` | `tests/mutations/R-15.diff` | 2026-11-18 |
| R-16 | Loading a second project does not return the first project's messages. | `tests/test_spec66_transcript_scope.py::test_other_project_is_not_loaded` | `tests/mutations/R-16.diff` | 2026-11-18 |
| R-17 | Accept stages the card path only and records `accept_error` when another path is staged. | `tests/test_spec66_accept_path.py::test_accept_does_not_stage_all` | `tests/mutations/R-17.diff` | 2026-11-18 |
| R-18 | `/stop` cancels the target session and does not forward the word stop. | `tests/test_spec66_stop.py::test_stop_cancels_and_kills` | `tests/mutations/R-18.diff` | 2026-11-18 |
| R-19 | A `<think>` mention after prose does not drop a following `/ask`. | `tests/test_spec66_think.py::test_prose_think_keeps_later_ask` | `tests/mutations/R-19.diff` | 2026-11-18 |
| R-20 | A coder-role tool batch keeps `test_run` true. | `tests/test_spec66_coder_tests.py::test_coder_does_not_disable_test_run` | `tests/mutations/R-20.diff` | 2026-11-18 |
| R-21 | `forward_to_phone` does not call `send_message` on the caller thread. | `tests/test_spec66_telegram_thread.py::test_send_leaves_the_caller_thread` | `tests/mutations/R-21.diff` | 2026-11-18 |
| R-22 | Package discovery includes `render` and `prompts` system markdown. | `tests/test_spec66_package.py::test_render_and_prompts_are_packages` | `tests/mutations/R-22.diff` | 2026-11-18 |

R-9 and R-10 stay the rows they are. This spec replaces their mutation files. It does not assign them new ids.

Each new mutation deletes the single check that the node asserts (the `is_sensitive_path(rel)` call, the `_untrusted_fence` call, the `env=` argument, the `if not attach` branch, the scoped filename, `stage_paths`, `cancel_session_key`, the `lstrip().lower().startswith("<think")` guard, the removal of `test_run=False`, `_deliver`, or `render*` in `pyproject.toml`). A mutation that the node still passes is not a row.

`runner: plain` for R-12, R-13, R-14, R-16, R-20, R-21, R-22. `runner: xvfb` for R-15, R-17, R-18, R-19, because those modules import GTK or the chat surface.

---

## 8. Edge cases

| Case | Expected |
|---|---|
| `is_sensitive_path(".crabcakes/x")` called directly | Still `True`. The function did not change. |
| Resolved path is a leading-dot file not under `.crabcakes/` | `rel` basename starts with `.`. `is_sensitive_path` returns `True`. Approval runs. |
| `python3 -m pytest tests/test_foo.py` | Validator returns `True`. |
| `python3 -c` inside a quoted argument that is not its own token | `shlex` does not yield a `-c` token. The command is allowed. A quote that `shlex` rejects returns `False`. |
| `WEB_FETCH_RESTRICT=""` | `get_env` returns `""`. `"" != "0"`, so fetch is restricted. |
| `DEVELCAKES_WEB_FETCH_RESTRICT=0` | Restrict predicate is false. `_reject_restricted_url` returns `None` at the top, including for `0.0.0.0`. |
| Hostname does not resolve, restrict on | Existing `gaierror` `ToolResult`. Unchanged. |
| `workflow.md` missing | `WORKFLOW_STATUS` is the invented sentence, unfenced. |
| Heading is only `## ` | `get_current_task` returns `""` after the strip when the remainder is empty. Treated as no task. |
| Session key `../agent:coder` or `agent/coder` | `conversation_store_name` raises `ValueError`. No file is written. |
| Loop active during project switch | `release_if_other_project` returns `False`. The running turn keeps its conversation. The next idle send loads the new project. |
| Legacy file's `project_path` is null | Not loaded. |
| Card `file_path` is absolute inside the project | `stage_paths` realpath-checks and stages the relative form. |
| Card `file_path` escapes the project | `stage_paths` returns `success=False`. The card shows that error. Nothing is staged. |
| Index already has the card path and another path | No commit. The other path stays staged. The error says so. |
| `/stop` with no target | The existing usage string. `cancel_session_key` is `None`. |
| Cancel callback missing | Chat shows `Stop is not wired.` and does not forward. |
| `cancel` when no process is registered | `cancel_all_processes` returns `(0, 0)`. The turn is still marked cancelled. |
| `stop_all` after `cancel` already killed | It does not call `cancel_all_processes` again. The count comes from `cancel`'s return. |
| Message is `<think>draft</think>\n/ask @Debugger "real"` | Balanced strip removes the draft. The ask remains. |
| Message starts with `<think` and never closes | Rest of the string is removed from the scan. The existing test stays. |
| `attach` raises | It does not. It returns `False`. JavaScript stays off. |
| `set_enable_javascript` raises after a true attach | Logged. JavaScript stays at the False set before compile. The filter is attached. This is the fail-closed side. |
| Two `forward_to_phone` calls overlap | `_send_lock` serializes `send_message`. Chunks inside one call stay in order. |
| `flush_outbound` and the worker is still in `send_message` after 5s | Join returns. The test may still be racing. Production does not call `flush_outbound`. |
| Editable install | `prompts` is a package at the repo root. `SYSTEM_DIR` still finds `prompts/system`. |
| Non-editable install without this spec's package data | Out of date after this change. `import render` and `prompts/system/*.md` resolve. |

---

## 9. ARCHITECTURE.md updates required

Add, do not rewrite the gateway chapters in this change:

- §3 tools: sensitive approval uses the relative path of the resolved file. `web_fetch` is restricted unless `DEVELCAKES_WEB_FETCH_RESTRICT=0`.
- §3 enforcement: `python3 -c` and `node -e` / `--eval` are rejected even though the basename is allowlisted. Coder no longer forces `test_run=False`.
- §3 persistence: the conversation file name is `conversation_store_name(session_key, project_path)`.
- §3.2a Telegram: outbound `send_message` runs on the `telegram-send` thread.
- §3 chat surface: live JavaScript is enabled only after `LiveGuard.attach` returns true.
- §3 feed: Accept stages `card.file_path` through `stage_paths`. It does not `git add -A`.
- §2 packaging: `render` and `prompts` are installed packages. `prompts/system/*.md` is package data.

---

## Self-audit

Checked against current source while writing:

- `_resolve_project_path`, `is_sensitive_path`, `_get_approval`, `_validate_test_command`, `_parse_command_to_argv`, `get_scrubbed_env`, `get_env`, `_untrusted_fence`, `get_current_task`, `stage_all`, `commit`, `cancel`, `cancel_all_processes`, `load_conversation_from_disk`, `attach`, `CommandResult`, `cmd_stop`, `_strip_thinking_blocks`, and `send_message` match the signatures cited.
- `_run_grep`'s `TimeoutExpired` is still uncaught on purpose.
- `conversation_store_name` raises `ValueError`, which `save_conversation` already raises for a missing conversation and which `_auto_save` already logs.
- `stage_paths` lives in `utils/git_ops.py` so the feed does not import `agent.tools._resolve_project_path`.
- The unclosed-`<think>` rule keeps the existing leading-tag test and changes only the mid-string case.
- `cancel`'s new return is consumed by `stop_all`. Other callers may ignore a tuple.
- No code was changed. The pytest suite was not run. This spec is not an implementation, so Rule 10's "tests passed" check does not apply to this file. The implementer runs the tests in §5 and pastes the output in the implementation report.

---

## 10. v2 amendments — post-0df09338 re-anchor (2026-11-18)

Where this section conflicts with earlier sections, this section wins. Discovery above was taken pre-`0df09338` (SPEC-64/65); three items are stale.

### 10.1 R-9/R-10 "gate repairs" are optional hygiene, not blockers

§Overview item 8 and §2.13 claim "R-9 has no mutation file" and "R-10's mutation deletes a sort the test does not depend on." Both are stale: `tests/mutations/R-9.diff` and `R-10.diff` exist on main and the gate PASSES both rows (verified: `scripts/acceptance.py rows --ids R-9,R-10` → mutated rc=1, clean green, 2026-11-18). The mutation-file rewrite in §2.13 is dropped from scope. The `_strip_thinking_blocks` change in §2.8 still stands on its own merits (it is the R-19 behavior, not a gate repair).

### 10.2 Re-anchor all `agent_command_handler.py` and `runtime.py` line anchors

`0df09338` rewrote both files (+368/−131 and +112). All `:NNN` anchors in §DISCOVERY and §2.13 for those two files are pre-change; implementers must re-anchor by symbol name, not line number. The spec's code blocks remain authoritative for shape.

### 10.3 Legacy-adoption must set `conv.project_path` before the scoped save

§2.5 step 4 and §2.6 say the runtime saves the scoped file immediately after a legacy hit. But `_conversation_from_data` (`agent/persistence.py:155-175`) constructs the Conversation with `project_path=None` unconditionally ("not trusted from disk"), so that immediate save would use the `none` slug — `session__none.json` — not the project's. Amendment: on a legacy hit, after the `realpath` equality check passes, set `conv.project_path = data["project_path"]` (the raw stored string; the equality check already validated it) BEFORE the scoped save. `_rebuild_conversation_context` still owns re-assignment on the next turn as today. Add a test: legacy hit → the scoped file written after the load carries the project slug, not `none`.

### 10.4 PM rulings recorded (2026-11-18)

The three embedded product decisions are explicitly blessed:
- `web_fetch` restricted by default; `DEVELCAKES_WEB_FETCH_RESTRICT=0` is the documented escape.
- Coder-role tool batches run tests again (`test_run` stays at config default); only `lint_check` is forced off.
- `python3 -c` / `node -e` / `--eval` test commands are refused; projects using them must move to a real test invocation.

---

## 11. v3 amendments — pre-flight audit incorporation (2026-11-18)

Incorporates Debugger's pre-flight (docs/audits/2026-11-18-SPEC-66-PREFLIGHT-ADVERSARIAL-AUDIT.md; 7 findings, all independently re-verified by Supervisor). Where this section conflicts with earlier sections, this section wins.

### 11.1 §2.2 REPLACEMENT — prefix-based eval-flag matching (BUG #1, CRITICAL)

The §2.2 exact-membership check (`"-c" in flags`) is DEAD: `shlex.split` makes `python3 -c"import os"` into one token `['-cimport os']`, and every attached spelling passes (verified). Replace with shape matching:

```python
def _is_eval_flag(tok: str, prefixes: tuple[str, ...]) -> bool:
    tok = tok.split("=", 1)[0]          # --eval=x -> --eval
    return tok in prefixes or any(
        tok.startswith(p) for p in prefixes if p not in ("--",)
    )
```

Rules: python/python3 → refuse any token starting `-c` (catches `-c`, `-cCODE`, `-c=CODE`; does NOT catch `--config`); node → refuse any token starting `-e` or `--eval`. Explicitly document the accepted false positive: `python3 -m pytest -c setup.cfg` is refused (pytest's config flag) — projects must pass config via env or file args. `python` (bare) cannot reach the check (not in `_ALLOWED_BINARIES`); remove it from the base set in the spec's code block.

### 11.2 §2.2 ADDITION — npx removal (BUG #2, CRITICAL)

`npx -c '<cmd>'` is `npm exec -c` — a full shell escape, and `npx` is in `_ALLOWED_BINARIES` (enforcement.py:60-63; verified executable, marker created). FIX: **remove `npx` from `_ALLOWED_BINARIES`** entirely. An enforcement test command has no legitimate npx use. Also refuse `npm` if ever added later. Note: `_coder_shell_allowed` (the agent's own shell tool allowlist) is a DIFFERENT list and stays unchanged.

### 11.3 §2.9 ADDITION — normalize before index comparison (BUG #3, HIGH)

`repo.index.diff("HEAD")` yields repo-relative paths; `card.file_path` may be `./x`, absolute, or `sub/../x` — none match, refusing legitimate accepts. Compare against the repo-relative form derived from the resolved path `stage_paths` already computes: `rel = os.path.relpath(staged_abs, project_real).replace("\\", "/")`.

### 11.4 §2.3 CLARIFICATION — fence consumer, and strip not escape (BUG #4, HIGH)

`_break_fence_tags` escapes `<` (HTML-safe) but the fence body is ALSO read by the model (Markdown-aware). State the target consumer explicitly: BOTH. Fix: replace the literal closing tag `</untrusted-project-data>` (case-insensitive, whitespace-tolerant) with a stripped inert marker — e.g. remove the `<`/`>` angle brackets entirely (`/untrusted-project-data/` text form) — inert for HTML AND for a model scanning for the fence terminator.

### 11.5 §2.3 CORRECTION — is_unspecified rationale (BUG #5, MEDIUM)

The claim "0.0.0.0/:: are not is_private" is FALSE (measured: both `is_private=True`); the existing check already refuses them, and §1 Problem item 3 is already false on 0df09338. Keep the `is_unspecified` check as defense-in-depth ONLY, with a corrected comment; fix the §1 problem statement.

### 11.6 §2.11 SCOPE ADDITION — feed surface (BUG #6, MEDIUM)

`ui/views/feed_surface.py:281-288` is a SECOND JS-enabled WebView with no LiveGuard (verified; not exploitable today only because `_esc` escapes all card fields — an accidental guarantee). Add feed_surface.py to §2.11's scope: LiveGuard must attach there too (the feed needs JS — FEED_SCRIPT drives scroll/click), and add it to `tests/test_html_guard_sites.py`'s catalog.

### 11.7 §2.5/§2.6 ADDITIONS — required parameter + multi-project note (BUG #7, LOW)

- `AgentRuntime.load_conversation(session_key, project_path)` — `project_path` has NO default (a silent empty conversation on omission is the failure mode).
- Document the multi-project legacy case in §8: a session key used in two projects gets its transcript only in the project matching the legacy file's stored path; the other starts empty (by design, scoped identity).
- Legacy files are not deleted after adoption; note the 2.2 MiB duplication as accepted hygiene debt.

### 11.8 Wave plan (replaces the §5 order for delegation)

- **Wave 1 (security)**: §2.1 approval fix, §2.2 (as amended 11.1+11.2), §2.3 (as amended 11.4+11.5) — enforcement.py, tools.py, prompt_loader.py.
- **Wave 2 (persistence/accept/stop)**: §2.5/§2.6 (as amended 10.3+11.7), §2.7 stage_paths + 11.3, §2.9/§2.10 cancel semantics.
- **Wave 3 (surface/packaging/transport)**: §2.11 (as amended 11.6), §2.12 packaging, §2.13 re-anchored parser items, Telegram worker.

---

## 12. v4 amendments — Flagger pre-flight incorporation (2026-11-18)

Incorporates Flagger's test-quality pre-flight (14 findings; load-bearing claims independently verified by Supervisor). Where this section conflicts with earlier sections, this section wins.

### 12.1 §2.1 ADDITION — the runtime-level gate is in scope (Flagger BUG #1, CRITICAL)

`agent/runtime.py:2867-2875` gates write/edit on `is_sensitive_path(args.get("path"))` — the RAW model string — the exact input §2.1 exists to defeat, and `:2902-2904` computes `bypass_approval` from the same raw string. §2.1 MUST change both runtime sites to use the resolved relpath (same normalization as tools.py). R-12's mutation must delete the rel checks at BOTH `agent/tools.py:635`/`:837` AND the runtime gate; the node asserts approval requested for all three fixtures (`./.crabcakes/`, `foo/../.crabcakes/`, absolute).

### 12.2 §2.3/§7 — R-14 re-scoped; the 0.0.0.0 claim corrected (Flagger BUG #2, CRITICAL; supersedes §11.5 detail)

`is_unspecified` is redundant (0.0.0.0/:: are already `is_private=True` — verified twice). R-14's node drops all `0.0.0.0` assertions; it pins (a) scrubbed-grep env kwarg — `mock.call_args.kwargs["env"] == get_scrubbed_env()` on BOTH subprocess.run sites (tools.py:1191, :1205) — and (b) the `DEVELCAKES_WEB_FETCH_RESTRICT=0` off-switch (the NEW env name). The `is_unspecified` branch may ship as commented defense-in-depth or be dropped; either way no row depends on it.

### 12.3 §5 NEW STEP — existing fetch tests must be updated (Flagger BUG #3, CRITICAL)

Six `tests/test_tools.py` tests assume the old unrestricted default (three `delenv CRABCAKES_WEB_FETCH_RESTRICT` at :1784/:1947/:1964). Every unrestricted-mode test must clear BOTH env names and set `DEVELCAKES_WEB_FETCH_RESTRICT=0` where unrestricted is intended. All `_reject_restricted_url` tests with hostname fixtures MUST mock `socket.getaddrinfo` (the :1833-1839 pattern). §1 Problem item 3 corrected: 0.0.0.0/:: were already refused; the finding is default-off, not a bypass.

### 12.4 §2.6 ADDITION — the 18 unpackers (Flagger BUG #4, CRITICAL)

`load_conversation_from_disk` arity change breaks 18 sites (verified by grep): tests/test_agent_runtime ×6 (:3092,:3133,:3688,:3750,:3792,:3831), test_spec48_phase3_digest ×6, test_spec23_context_working_set ×3, test_agent_persistence ×2, agent/runtime.py:5229. §5 gains an explicit migration step + acceptance grep: `conv, _ = load_conversation_from_disk` → zero matches after.

### 12.5 §2.5 ADDITION — four legacy-filename readers (Flagger BUG #5, HIGH; R-1 vacuity)

Scoped filenames break four readers (verified): project_handler.py:740 (`_read_agent_usage` → silent (0,0) metrics), session.py:119 (`clear_conversation` deletes a nonexistent path → scoped file SURVIVES /clear → R-1 becomes vacuous), runtime.py:5336 (`list_conversations` yields `sk__slug` keys), status_report.py (slug-prefixed session keys resolve to no agent). All four MUST migrate: glob `<session_key>__*.json`, split on `__`, strip slug. R-1's node/mutation re-verified after.

### 12.6 §7 — transcript coverage restructured (Flagger BUG #6, HIGH)

R-16's node becomes a class with four methods (R-8/R-9/R-10 pattern): `test_other_project_is_not_loaded`, `test_legacy_file_is_rejected_on_realpath_mismatch`, `test_legacy_hit_writes_scoped_file_with_project_slug` (asserts the file NAME on disk — `<sk>__<sha16>.json` exists, `__none.json` does not — pinning §10.3's ordering), `test_scoped_file_wins_over_legacy_with_different_content`.

### 12.7 §7 — runner corrections + the plain-row contract (Flagger BUG #7/#8, HIGH)

R-19 → `runner: plain` (closure is GTK-free). R-18 splits: the ChatHandler/CollabHandler half is plain, the `cancel_agent` half stays xvfb. RULE for every new plain row: (a) no import of ui.window/ui.views.*/agent_runtime_handler/feed_handler; (b) `GTK_HEADLESS_SAFE = True` where the closure pulls a GTK module without constructing widgets; (c) never `skip_ok: true` — a skipped node is a row failure (acceptance.py:253-259).

### 12.8 §7 — R-22 split (Flagger BUG #9, HIGH)

R-22 covers packaging discovery ONLY (declared via `setuptools.packages.find` include + package-data; no pip-install-in-pytest). New R-23: stale strings — left_panel placeholder no longer says "Connect", knowledge/setup.md has no `local-kb`/`CrabCakes`.

### 12.9 §2.3/§8 — fetch edge rows (Flagger BUG #10/#11, MEDIUM)

Add: `http://[::]/` (refused via is_private — test it), `http://[::ffff:127.0.0.1]/` (verified is_private=True → refused; ADJUDICATED against Flagger's static claim — Debugger's executed probe + my interpreter run both confirm refused; a pin test still lands), mixed-address DNS (any-private-refuses). Mock getaddrinfo everywhere.

### 12.10 §2.4 — no lazy import (Flagger BUG #14, LOW)

No cycle exists (prompt_loader already lazy-imports project_awareness). Keep the module-scope import at project_awareness.py:38; fix the rationale text.

### 12.11 Test isolation contract (Flagger BUG #12, MEDIUM)

Transcript tests use the autouse isolation fixture (no get_config_dir monkeypatch) and assert the scoped file's absolute path is under tmp_path.
