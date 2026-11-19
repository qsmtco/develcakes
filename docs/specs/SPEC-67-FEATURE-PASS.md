# SPEC-67: Feature Pass

**Date:** 2026-11-18
**Author:** Spec draft from the 18 Nov 2026 feature list
**Status:** Draft — for implementation
**Implements:** The eight features from that review, in that order: remember the room, ask a person when addressing is unsure, open a file, snapshot a turn, send a local image to the model, a terminal for the person, dream proposals accepted by hand, branch and push on the project bar.
**Depends on:** SPEC-47 for acceptance rows. Feature 7 (dream) depends on SPEC-66 §2.3–§2.4 (fence tags are escaped and project text is fenced) and must not land before that slice. Feature 8 (push) depends on SPEC-66 §2.9 (Accept no longer runs `git add -A`) and must not land before that slice. Features 1–6 do not wait on SPEC-66.
**Target branch:** main
**Spec authoring prompt:** `.crabcakes/prompts/steelFramedSpecWriter.md`

> Architecture compliance: `utils/` and `agent/` stay free of GTK. `utils/git_ops.py` does not import `agent/`. `ChatHandler` does not build widgets. `ui/window.py` is the composition root. New prompt text that comes from a project file and has not been accepted by a person goes through `utils.prompt_loader._untrusted_fence`. An accepted dream file is person-approved prompt text and is not fenced. The person's terminal is not an agent tool and does not call `_coder_shell_allowed`.

---

## DISCOVERY

Starting Spec Discovery — reading all referenced source files before writing any spec content.

- Read `ui/window.py`:
  - `set_default_size(800, 600)` at `:62`.
  - `_on_close_request(self, *args) -> bool` (`:108`) flushes the feed writer, stops the bridge, releases settings, and returns `False`. It does not read widget sizes.
  - Horizontal paned is a local `paned` set to position 250 (`:839-846`). `self._activity_paned` is vertical, position 600 (`:861-864`).
  - `_cached_branch_by_path` is in memory only (`:96`).
  - Branch lookup is `_resolve_branch_worker` (`:2056`), which calls `get_branch(path)` and idle-adds the result. `get_branch` success with `"(detached HEAD)"` is a real stdout. Failure yields `None`.
- Read `ui/views/main_content.py`:
  - The chat/input split is a local `paned` at position 400 (`:261-268`). It is not stored on `self`.
  - `update_project_settings(self, project_name, member_count, solo_target, auto_accept_level, branch_name)` (`:368`) rebuilds the bar. The branch widget is a `Gtk.Label`, not a button (`:444-452`). Project names go through `xml_escape_text`.
- Read `ui/views/left_panel.py`: `self._PAP_notebook` (`:169`) is the Prompts/Agents/Projects/Settings notebook. Startup forces page 2 (`:171-173`).
- Read `ui/handlers/project_handler.py`:
  - `open_project(self, name: str, path: str)` (`:106`) sets `_active_project_name` and `_active_project_path`, inits `.crabcakes/`, and loads members. `init_workflow` exceptions are swallowed.
  - `get_solo_target(self, project_name: str) -> str | None` (`:388`) reads `self._solo_targets`.
  - `set_solo_target(self, project_name: str, member_session_key: str | None)` (`:400`) writes that dict. It is RAM only.
- Read `ui/handlers/chat_handler.py`:
  - `_resolve_room_fallback(self, members, roster)` (`:174`) reads `get_agent_setting("jev_room_fallback", "supervisor")`. `"drop"` returns `[], "message not routed"`. `"all"` returns every member. Anything else, including an unknown string, routes to a roster name equal to `"supervisor"` or else every member.
  - Group send (`:443-553`) runs `resolve_room_addressee` on a daemon thread. `[]` calls `_resolve_room_fallback` and then `_send_local` for every target. `["all"]` sends the raw text to every member. The You bubble is shown inside `_show_and_send` before this branch.
- Read `agent/llm/jev_client.py` `resolve_room_addressee(text: str, roster: list[tuple[str, str]]) -> list[str]` (`:327`). It never raises. Low confidence, a bad body, and a failed `post_decision` return `[]`. A confident whole-room pick returns `["all"]`. `post_decision(state, questions) -> tuple[dict | None, str]` (`:111`).
- Read `agent/config.py`: `ROOM_FALLBACK_DEFAULT = "supervisor"` and `ROOM_FALLBACK_VALUES = ("supervisor", "all", "drop")` (`:324-325`). `get_agent_setting(key, default=None)` (`:328`) returns `default` on a missing file, a non-dict, `OSError`, or `JSONDecodeError`. It never raises.
- Read `ui/views/file_tree.py` `_on_row_activated` (`:1048`). A directory expands or collapses. A file calls `_toggle_drawer(row.props.full_path)`. Drawer rows are not activatable.
- Read `utils/image_paths.py`: `is_path_in_allowed_roots(file_path: str) -> bool` (`:29`). `OSError` from `realpath` returns `False`.
- Read `render/html.py` `_local_image_uri(path: str) -> str | None` (`:73`). Extensions png/jpeg/jpg/gif/webp. Cap `_MAX_IMAGE_BYTES = 8 * 1024 * 1024`. Any exception returns `None`. A ```image fence is display-only.
- Read `models/conversation.py` `to_api_messages(self) -> list[dict]` (`:417`). User `content` is a string. The task card is concatenated onto the last user string (`:443-449`).
- Read `agent/llm/convert.py` `convert_messages_for_anthropic(messages: list[dict]) -> list[dict]` (`:16`). The `else` branch appends `msg` unchanged (`:64`). A list `content` would be sent to Anthropic as an OpenAI part list.
- Read `agent/llm/registry.py` `get_provider(provider_id: str)` (`:18`). Raises `KeyError` when the id is not in `_REGISTRY` (`openai`, `openrouter`, `zai`, `minimax`, `anthropic`).
- Read `agent/runtime.py` `_call_llm_once`: `caller_key = self._resolve_caller_key(provider_cfg, model)` at `:3723`, then `_call_llm_streaming(..., messages=messages)` and, on the non-streaming path, `provider.call(...)`.
- Read `agent/llm/openai_provider.py` `call(self, base_url, api_key, model, messages, tools, timeout, x_title="", reasoning_effort="off") -> dict` (`:121`). MiniMax `call` has the same parameters (`agent/llm/minimax_provider.py:92`) and posts `messages` through.
- Read `agent/tools.py` `ToolResult` (`:45`). `to_dict` omits `enforcement_checks`. `_write_file` opens the destination with `"w"` at `:676` after the size check. There is no copy of the previous bytes.
- Read `ui/handlers/crabwatch_handler.py` `_emit_event` (`:124`). The card's `file_path` is `relative_path`. `metadata` is not set.
- Read `ui/feed/review.py` `handle_reject` (`:136`). Git cards call `checkout_paths(project_path, sha, [fp])` when `fp` is set. Invalid sha returns before checkout. Missing `project_path` returns with no card update.
- Read `utils/git_ops.py`:
  - `get_branch(project_path) -> GitResult` (`:87`). Detached HEAD is `success=True`, `stdout="(detached HEAD)"`.
  - `push(project_path, remote="origin", branch="main") -> GitResult` (`:345`). `repo.remote(remote)` then `origin.push(refspec=f"HEAD:{branch}")`. `IndexError` if `info` is empty is an `Exception` and becomes `_safe_error`. Flags `ERROR` returns `success=False`.
  - `checkout_paths(project_path, sha, paths) -> GitResult` (`:250`). Non-string sha or a sha other than `HEAD` that fails `_VALID_SHA_RE` returns `success=False` and does not call git.
- Read `utils/review_log.py`: `get_review_log_path`, `get_dream_log_path` (`:22-29`). `append_review_entry(project_path, entry)` (`:32`) appends one JSON line and can raise from `open`. `DREAM_LOG_FILENAME` is unused by a cycle.
- Read `utils/prompt_loader.py`: role bugs load from `.crabcakes/{agent_role}-bugs.md` when the project is trusted (`:299`). `compose_system_prompt` is the insertion point for an accepted dream file.
- Read `utils/improve.py` `load_improve_card(destination=None)` (`:71`) and `improve_prompt(raw_text, callback, GLib=None)` (`:88`). The card fields used are `api_key`, `base_url`, `default_model`. `validate_provider_url` raises `ValueError` on a non-https non-loopback URL.
- Read `utils/feedback_processor.py` `append_to_bug_journal` (`:123`). The journal path is `.crabcakes/{target_role}-bugs.md`.
- Architecture owners: window geometry is `MainWindow`. Solo target is `ProjectHandler._solo_targets`. Room delivery is `ChatHandler._deliver`. The decision call stays `resolve_room_addressee`. File bytes for a view are read in `ui/`. Wire images are `agent/llm`. Snapshots are `utils/` plus `agent/tools.py` before the mutating `open`. Dream files are `utils/` and `prompt_loader`. Git mutations stay `utils/git_ops.py`. The person's shell is `ui/` only.

---

## 1. Overview

### Problem

The app can hold a conversation, show a tree, and draw a branch name. Eight gaps sit on top of that:

1. Close forgets the window size, the three splits, the left tab, the open project, and the `Chat:` target.
2. When `resolve_room_addressee` returns `[]`, the fallback can still start every member. The person is not asked.
3. Activating a file opens a diff drawer. There is no buffer.
4. Reject restores a git sha. A file that was created in the turn, or a write that is not yet a commit, has nothing to put back except `HEAD`.
5. A ```image fence becomes a `data:` URI in the chat. `to_api_messages` sends a string. The model never receives the bytes.
6. The person has no shell. Coder's shell allowlist is a safety control and stays closed.
7. `dream_consolidation` is a flag. Nothing reads the journal, and nothing writes a proposal the person can accept.
8. `push` has no caller. The branch label is not a control.

### Solution summary

1. `utils/ui_state.py` stores one JSON file. Close writes it. Startup applies it, then opens the last project when that directory still exists, then restores `Chat:` when that session is still a member.
2. Add fallback value `"ask"` and make it the default when the setting is missing. `[]` with that setting shows a row of buttons and does not call `_send_local` until one is clicked.
3. A read-only `Gtk.TextView` page opens for a file in the project. Double-click and a feed-card path both open it. The diff drawer moves to a right-click.
4. Before a write or edit replaces bytes, copy the old file, or record that it was absent, under `.crabcakes/turn-snapshots/`. Reject restores that snapshot when the card has its id. Git checkout remains the path when there is no snapshot.
5. User messages that contain a ```image fence gain provider image parts, after the task-card string concat, only for callers that can take them. Anthropic's converter maps those parts. MiniMax keeps text and a one-line omission note.
6. A project-scoped pane runs the person's `$SHELL` on a pty. It is not an agent tool. Closing the window kills the process group.
7. A cycle reads the role journal and the review log since the last dream-log line, calls the Improve Model card through `get_provider`, and writes a proposal file. Accept copies that file to `.crabcakes/dream-accepted/{role}.md`. `compose_system_prompt` includes the accepted file. The cycle never writes `prompts/system/` or the agent YAML.
8. The branch label becomes a button that lists local branches and checks one out only when `status_porcelain` is empty. A Push button calls `push` with the current branch name and remote `origin`, on a worker thread, after a confirm dialog.

### Scope

| In | Out |
|---|---|
| The eight features and rows R-23–R-30 | SPEC-66's fixes, except the two dependencies named above |
| `"ask"` as the missing-setting fallback | Changing a stored `jev_room_fallback` of `supervisor`, `all`, or `drop` |
| Read-only source view, 1 MB, UTF-8 | GtkSource, LSP, diagnostics, editing in the view |
| One snapshot per write path | A full-tree snapshot, or snapshotting `exec_command` |
| ```image fences of local png/jpeg/gif/webp under the existing roots | Remote image URLs, vision for tool results, MiniMax image parts |
| A line-oriented pty for the person | VTE, curses, or widening Coder's shell |
| Dream proposals and an explicit accept | Editing the live role prompt, a nightly daemon, graph memory |
| Checkout of a clean local branch, and push of `HEAD` to `origin` under that name | Pull, rebase, force push, creating a remote branch other than the current name |

### Principles

1. Restored UI state is advisory. A missing file, a bad integer, or a path that is not a directory is skipped. Startup still reaches the window.
2. Uncertain room routing sends no model turn. A confident `["all"]` still fans out.
3. The source view and the snapshot store refuse paths that `realpath` outside the project.
4. Image bytes use the same roots, extensions, and 8 MB cap as `_local_image_uri`.
5. The person's shell inherits the app environment. It is not `get_scrubbed_env()`. The agent shell is unchanged.
6. A dream cycle that cannot call a model writes no proposal. Accept is a separate call with the proposal path the person chose.

---

## 2. Changes by file

### 2.1 `utils/ui_state.py` — new

Stdlib only. No GTK.

```python
def ui_state_path() -> str:
    """<config_dir>/ui-state.json. Does not create the directory."""

def load_ui_state() -> dict:
    """Return a dict. Missing file, OSError, JSONDecodeError, or a non-dict
    returns {}. Never raises."""

def save_ui_state(state: dict) -> None:
    """Atomic replace, mode 0o600. OSError propagates to the caller.
    The caller on the window close path logs it and still returns False
    from _on_close_request."""
```

Keys, all optional:

| Key | Type |
|---|---|
| `width`, `height` | int, clamped 640–3840 and 480–2160 on load |
| `h_pane`, `v_pane`, `chat_pane` | int, clamped 80–4000 |
| `left_page` | int, clamped 0–3 |
| `last_project_name`, `last_project_path` | str |
| `solo_targets` | dict[str, str] |

A value of the wrong type is dropped, not coerced, except that `bool` is not an int (reject `True`/`False` for the numeric keys). `solo_targets` values must be strings. Other keys are ignored.

### 2.2 `ui/window.py`, `ui/views/main_content.py`, `ui/views/left_panel.py`, `ui/handlers/project_handler.py`

`MainWindow._build` keeps the paned: `self._h_paned = paned`. `MainContent` keeps `self._chat_paned = paned`.

After the widgets exist, `load_ui_state()`:

- `set_default_size(width, height)` when both keys survived the clamp. Otherwise leave 800×600.
- `self._h_paned.set_position`, `self._activity_paned.set_position`, and `self._main_content._chat_paned.set_position` when those keys exist. `set_position` is the GTK method already used at the three call sites. It returns `None`.
- `self._left_panel._PAP_notebook.set_current_page(left_page)` when the key exists. Page 2 remains the default when the key is absent.
- If `last_project_path` is a directory, call `self._project_handler.open_project(last_project_name or os.path.basename(path), path)`. `open_project` does not raise for a normal directory. The `init_workflow` handler inside it already swallows `Exception`.
- After that open, if `solo_targets[name]` is a string and it is in `get_project_members(name)`, call `set_solo_target(name, that key)`.

`_on_close_request` builds the dict from `self.get_width()`, `self.get_height()`, the three `get_position()` calls, `self._left_panel._PAP_notebook.get_current_page()`, the active project name and path, and `dict(self._project_handler._solo_targets)` with `None` values omitted. `save_ui_state` is inside `try/except OSError` plus `Exception`. A failure is logged. The method still returns `False`.

`ProjectHandler.set_solo_target` is unchanged. The window already listens through `set_on_solo_target_changed`. That callback also calls `save_ui_state` with the same gather helper, so a `Chat:` click persists without waiting for close. `OSError` is logged.

`open_project` does not itself write the file. The window's existing project-opened callback does, after `open_project` returns, so a failed open does not become the saved project. The existing callback is the one that already calls `set_active_project`. Add the save there.

### 2.3 Room choice — `agent/config.py`, `ui/handlers/chat_handler.py`, `ui/views/main_content.py`, `ui/window.py`

`ROOM_FALLBACK_VALUES` becomes `("ask", "supervisor", "all", "drop")`. `ROOM_FALLBACK_DEFAULT` becomes `"ask"`.

`_resolve_room_fallback` is not used when the effective setting is `"ask"`. The call site that today does:

```python
targets, note_suffix = self._resolve_room_fallback(members, roster)
```

becomes: if `get_agent_setting("jev_room_fallback", ROOM_FALLBACK_DEFAULT) == "ask"`, call a new method instead of sending.

```python
def _ask_room_choice(self, project_name: str, text: str, members, roster) -> None:
    """Show a chooser. Do not call _send_local.

    options is a list of (session_key, label). The first entries are the
    roster pairs. Then ("all", "Everyone") and ("drop", "Nobody").
    Stores self._pending_room = (project_name, text, members). A second
    send for the same project replaces _pending_room and asks again.
    The previous pending text is not delivered.
    """
```

`ChatHandler.set_on_room_choice(self, cb) -> None`. The callback is `(project_name, text, options)`. `window.py` wires it to `MainContent.show_room_choice`.

`MainContent.show_room_choice(self, project_name, text, options)` clears any previous choice box, builds one `Gtk.Button` per option, and packs the box into `bottom_box` above the input. Button labels are the display name, `Everyone`, or `Nobody`. `clicked` calls `self._on_room_choice_made(project_name, key)` which the window sets to `ChatHandler.complete_room_choice`.

```python
def complete_room_choice(self, project_name: str, key: str) -> None:
    """Deliver the pending text as if resolve_room_addressee had returned.

    key "all" -> _deliver(["all"]) with the stored members.
    key "drop" -> _deliver([]) is wrong, because [] means unsure.
    "drop" records the room note "[room] not sent" via record_room_line
    for every member and does not call _send_local.
    Any other key that is in members -> _deliver([key]).
    A key that is not in members and is not all/drop is ignored.
    Clears _pending_room either way when the key is recognized.
    """
```

`_deliver` stays the function that already fans out. `complete_room_choice` calls it on the main thread. It does not call `resolve_room_addressee` again.

A stored setting of `supervisor`, `all`, or `drop` keeps today's `_resolve_room_fallback` behavior. Only a missing key, or an explicit `"ask"`, waits.

`["all"]` from the resolver is still a confident fan-out. This feature does not intercept it.

### 2.4 `ui/views/source_view.py` — new, and the open paths

```python
class SourceView(Gtk.Box):
    def show_file(self, project_path: str, file_path: str) -> str:
        """Load file_path into a read-only monospace TextView.

        Returns "ok", "missing", "outside", "binary", or "too_large".
        Does not raise for those five. OSError while reading returns
        "missing" after logging.

        file_path may be absolute or project-relative. realpath must stay
        under realpath(project_path), using commonpath. ValueError from
        commonpath is "outside". A NUL in the first 512 bytes is "binary".
        Size over 1_048_576 bytes is "too_large". Decode utf-8 with
        errors="replace" only after those checks.
        """
```

`Gtk.TextView.set_editable(False)`, `set_monospace(True)`, inside a `Gtk.ScrolledWindow`. The buffer is replaced on each `show_file`. The widget does not write the file.

`MainContent` gains a notebook page titled with the basename, created on first open and reused. `open_source(project_path, file_path)` shows that page and calls `show_file`. The return string other than `"ok"` becomes the buffer text `Cannot open this file ({reason}).` and nothing else.

`FileTree._on_row_activated` for a file calls `self._on_open_source(row.props.full_path)` when the callback is set, and does not toggle the drawer. Directories still expand. A right-click (`Gtk.GestureClick`, button 3) on a file calls the existing `_toggle_drawer`. Window sets `_on_open_source` to a lambda that passes the active project path into `MainContent.open_source`. If no project is open, the callback is a no-op.

Feed cards: `FeedHandler` gains `set_on_open_source(cb)`. The feed HTML already puts `file_path` on the card. Add a button in `render/feed_html.py` `_actions` when `card.file_path` is non-empty: `_button("open", "Open", card_id)`. `on_feed_action` for `"open"` calls the callback with `(project_path, card.file_path)` and does not accept or reject. The existing action names stay.

### 2.5 `utils/turn_snapshot.py` — new, plus write, crabwatch, reject

```python
def capture(project_path: str, absolute_path: str) -> str | None:
    """Copy the file's current bytes, or record that it does not exist.

    Returns a snapshot id (16 hex chars) or None when absolute_path
    realpath's outside the project. OSError on read returns None and
    logs. Does not modify the destination file.

    Writes <project>/.crabcakes/turn-snapshots/<id>/manifest.json
    and, when the file exists, body.bin. manifest keys: relpath, absent
    (bool), mode (int or null). Atomic replace for the manifest.
    """

def restore(project_path: str, snapshot_id: str) -> str:
    """Put the file back. Returns "restored", "removed", "missing", or "outside".

    absent true and the file exists: os.remove the file. absent true and
    the file is already gone: "removed". body.bin is written back with
    the saved mode masked to 0o777. A snapshot id that is not 16 hex
    chars, or a relpath that escapes, returns "outside" and writes
    nothing.
    """
```

`_write_file`, immediately before `open(resolved, "w")`, and `_edit_file`, immediately before it writes the new content, call `capture`. On a non-`None` id, set `ToolResult` field `snapshot_id: str | None = None`. `to_dict` does not include it, matching `enforcement_checks`.

`CrabWatchHandler._emit_event` reads `utils.turn_snapshot.latest_id(project_path, relative_path)`, which returns the id captured for that relpath in the last 10 seconds or `None`. When it is not `None`, set `card.metadata["snapshot_id"]` to that id. `latest_id` is a process-local dict updated by `capture`. A restart loses the mapping. The bytes remain on disk under the id. Reject without an id uses git, as today.

`handle_reject`, before `checkout_paths`: if `card.metadata.get("snapshot_id")` is a string, call `restore`. `"restored"` or `"removed"` marks the card rejected and skips `checkout_paths`. `"missing"` or `"outside"` falls through to the existing git path. `restore` does not raise for those four returns.

### 2.6 Images — `utils/image_paths.py`, `render/html.py`, `agent/llm/images.py`, `agent/llm/convert.py`, `agent/runtime.py`

Move the body of `_local_image_uri` into:

```python
def read_image_data_uri(path: str) -> str | None:
```

in `utils/image_paths.py`. Same extensions, same 8 MB cap, same `is_path_in_allowed_roots`, same "never raise". `render/html.py` `_local_image_uri` calls it and returns the result. Display behavior does not change.

New `agent/llm/images.py`:

```python
_IMAGE_FENCE = re.compile(r"```image[ \t]*\r?\n(.+?)\r?\n```", re.DOTALL | re.IGNORECASE)

def apply_image_parts(messages: list[dict], caller: str) -> list[dict]:
    """Return a new list. messages is not mutated.

    For each user message whose content is a str, split on _IMAGE_FENCE.
    A fence whose read_image_data_uri returns None is replaced with the
    text "[image omitted]". A successful read becomes a part.

    caller in {"openai", "openrouter", "zai"}: content is a list of
    {"type":"text","text":...} and
    {"type":"image_url","image_url":{"url": <data uri>}}.
    At most 4 images per message. Further fences become "[image omitted]".

    caller == "anthropic": the same OpenAI-shaped list. convert_messages_for_anthropic
    maps it. This function does not emit Anthropic blocks.

    Any other caller, including "minimax" and "": fences become
    "[image omitted]" and content stays a string.

    A user message with no fence is copied unchanged. Non-user roles are
    copied unchanged. A content value that is already a list is copied
    unchanged.
    """
```

`convert_messages_for_anthropic`, in the `else` branch, when `content` is a list: build Anthropic blocks. A part with `type=="text"` becomes `{"type":"text","text": part["text"]}` when `text` is a str. A part with `type=="image_url"` whose url starts with `data:image/` and contains `,` becomes:

```python
header, b64 = url.split(",", 1)
media_type = header[len("data:"):].split(";", 1)[0]
{"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}
```

A part that does not match is skipped. If the list yields no blocks, append `{"role": role, "content": ""}` so the message is not dropped. Do not `json.loads` the image. `Exception` is not expected. A missing comma skips that part.

In `_call_llm_once`, immediately after `caller_key` is assigned (`:3723`), and on the non-streaming path at the equivalent assignment:

```python
from agent.llm.images import apply_image_parts
messages = apply_image_parts(messages, caller_key)
```

Both `provider.call` and `_call_llm_streaming` receive that list. The stored `Conversation.messages` stay strings. The task-card concat in `to_api_messages` still runs first, because `apply_image_parts` sees the wire list.

### 2.7 `ui/views/person_terminal.py` — new

```python
class PersonTerminal(Gtk.Box):
    def start(self, cwd: str) -> None:
        """Spawn $SHELL or /bin/bash on a pty with cwd.

        Uses pty.openpty and subprocess.Popen(..., start_new_session=True).
        stdin, stdout, and stderr are the slave. The slave fd is closed
        in the parent. A reader thread reads the master and idle-adds
        appended text. CSI sequences (ESC [ ... final byte) are stripped,
        not interpreted. The widget is a TextView plus an Entry. Activate
        on the entry writes the line plus newline to the master.

        OSError from openpty or Popen sets the view text to the exception
        string and does not start a thread. Does not use get_scrubbed_env.
        """

    def stop(self) -> None:
        """SIGTERM the process group, then close the master. Idempotent.
        OSError from killpg is logged. Does not raise."""
```

`MainContent` adds a Show terminal / Hide terminal button on the input bar. The pane is the terminal, packed under the input, hidden until shown. `set_terminal_cwd(path)` calls `stop` then `start` when the pane is visible. Window calls that from the project-opened path and calls `stop` from `_on_close_request` before returning `False`.

This widget is not registered as a tool. `agent/tools.py` is not modified for it.

### 2.8 Dream — `utils/dream.py`, `utils/prompt_loader.py`, `ui/handlers/dream_handler.py`

```python
def run_dream(project_path: str, role: str) -> tuple[str | None, str | None]:
    """Write one proposal. Returns (proposal_path, error).

    error is None on success. proposal_path is None when error is set.
    Never raises. Never writes prompts/system, agent YAML, or
    dream-accepted/.

    Refuses when role is not a single [a-z0-9-] token.
    Refuses when the role's agent YAML self_improvement.dream_consolidation
    is not True. The lookup is utils.agent_defs.load_agent_def_by_role.
    A missing role is an error string, not an exception.

    Reads .crabcakes/{role}-bugs.md and review-log.jsonl lines whose
    timestamp is greater than the last dream-log.jsonl "at" field.
    A missing journal and an empty review log return
    (None, "nothing new") and do not call the model.

    Model: load_improve_card(). None card -> (None, "Improve model not configured").
    validate_provider_url(base_url) ValueError -> that string as error.
    get_provider(card.caller).call(base_url, api_key, default_model,
    messages, None, 60.0). KeyError -> "unknown provider".
    The provider call's exceptions are caught as Exception and returned
    as the error string. The response text is the assistant content via
    extract_text_content(response, response_format=provider.response_format).

    Writes .crabcakes/dream-proposals/<utc>-<role>.md with the text,
    mode 0o600, temp file plus os.replace. Appends one JSON line to
    dream-log.jsonl: {"at": iso, "role": role, "proposal": basename}.
    A second run with no newer journal lines returns "nothing new"
    and does not write a second proposal.
    """
```

The messages are a system string that says the model may propose a prompt paragraph and must not claim the live prompt changed, plus a user string that is the journal excerpt. The excerpt is wrapped by `_untrusted_fence` before it is placed in the user message, so SPEC-66's escape applies. If SPEC-66 has not landed, call the same function: this spec's dependency says feature 7 waits for that function to escape closers.

Accept:

```python
def accept_proposal(project_path: str, proposal_basename: str, role: str) -> str | None:
    """Copy the proposal onto .crabcakes/dream-accepted/{role}.md.

    Returns None on success, or an error string. Refuses a basename that
    is not a single path segment, a role that is not [a-z0-9-], or a
    resolved path outside dream-proposals/. Does not delete the proposal.
    Overwrites dream-accepted/{role}.md using temp plus os.replace.
    """
```

`compose_system_prompt`, after the role template is loaded and only when `is_project_trusted(project_path)` is true, reads `.crabcakes/dream-accepted/{agent_role}.md`. If the file exists and is non-empty, append it as a section `## Accepted dream notes` plus the file text. This text is not fenced. A missing file is skipped. `OSError` is logged and skipped. Untrusted projects do not receive the file.

`DreamHandler` registers `/dream` on the command registry from `window.py`.

- `/dream` with no args calls `run_dream` for role `coder` on a daemon thread and shows the result path or the error through the command response text.
- `/dream accept <basename>` calls `accept_proposal` for role `coder`.
- Any other shape returns a usage line. It does not call the model.

Coder is the only role this command runs. The function itself takes `role` so a later command can pass another role. This spec does not add that command.

`run_dream` also checks the journal hash against the last dream-log line's optional `input_hash`. The same hash returns `"nothing new"` even if the clock moved. The hash is sha256 of the excerpt. Store it on the log line.

### 2.9 Branch and push — `utils/git_ops.py`, `ui/views/main_content.py`, `ui/window.py`

```python
def list_local_branches(project_path: str) -> GitResult:
    """stdout is one branch name per line, active branch first.
    Detached HEAD still lists branches and uses stdout "(detached HEAD)"
    as the first line only when head.is_detached, then the names.
    Exception -> success=False, _safe_error."""

def checkout_branch(project_path: str, branch: str) -> GitResult:
    """Checkout a local branch name.

    Refuse when branch is not a single token matching [A-Za-z0-9._/-]+,
    when it contains "..", or when status_porcelain(project_path) is
    non-empty. The refusal is success=False and does not call git.
    Otherwise repo.git.checkout(branch). Exception -> _safe_error.
    """
```

`status_porcelain` already exists (`:387`) and returns `dict[str, str]`. Non-empty means dirty. It can raise from git. `checkout_branch` catches `Exception` from it and returns that error. It does not checkout.

`update_project_settings` replaces the branch `Gtk.Label` with a frameless `Gtk.Button`, same markup, `clicked` calls `self._on_branch_clicked()`. A second frameless button `Push` calls `self._on_push_clicked()`. Both callbacks are set from the window. When `project_name` is empty the buttons are not built. The existing early return stays.

Window `_on_branch_clicked` runs `list_local_branches` on a daemon thread and idle-adds a `Gtk.Popover` of buttons, one per name that is not the `(detached HEAD)` marker. Click calls `checkout_branch` on a worker, then the existing branch refresh. Failure sets the command-response text to `result.error`.

`_on_push_clicked` opens a confirm dialog using the same pattern as `_on_stop_all_clicked` (`:1780`): a `Gtk.MessageDialog`. Confirm runs `push(path, remote="origin", branch=current)` on a worker, where `current` is `get_branch(path).stdout` and a detached or failed branch aborts with the error text and does not call `push`. `push` is not called on the main thread. The dialog's response handler returns after starting the thread.

`push`'s `refspec=f"HEAD:{branch}"` is the existing function. This spec does not change that refspec. The branch argument is the name `get_branch` returned, not the literal `"main"`, except when that name is the current branch.

### Files NOT changed

- `agent/runtime.py` `_coder_shell_allowed` and the coder `test_run` replace. Those are SPEC-66.
- `resolve_room_addressee`. Unsure stays `[]`. The UI decides what `[]` means when the setting is `ask`.
- `Conversation.to_api_messages`. Image parts are applied to the wire list only.
- `prompts/system/*.md` and agent YAML. Dream accept writes under the project's `.crabcakes/dream-accepted/`.
- `utils/env_security.get_scrubbed_env`. The person's terminal does not call it.
- `handle_accept` and `stage_all`. Push does not commit.

---

## 3. Data flow

**Startup.** `load_ui_state`. `set_default_size` when both ints exist. After `_build` creates the paneds, `set_position` for each saved int. `set_current_page` on `_PAP_notebook`. If the saved path `os.path.isdir`, `open_project`. If the saved solo key is still a member, `set_solo_target`.

**Close.** Gather width, height, three positions, page, project, solo map. `save_ui_state`. Then the existing feed and bridge shutdown. Return `False`.

**Unsure room.** The You bubble is already shown. The worker gets `[]`. The setting is missing, so it is `"ask"`. `_ask_room_choice` stores the pending tuple and the window builds buttons. No `_send_local`. The person clicks Debugger. `complete_room_choice` calls `_deliver(["agent:debugger"])`. The other members get `record_room_line`, which is what `_deliver` already does for a single target.

**Open file.** Double-click. `full_path` goes to `SourceView.show_file`. `commonpath` equals the project. The buffer shows the text. A feed Open button uses `card.file_path` the same way.

**Snapshot.** `_write_file` calls `capture` before `open(..., "w")`. The id lands on `ToolResult.snapshot_id` and in the process-local latest map. Crabwatch's card for that relative path copies the id into `metadata`. Reject calls `restore`. The old bytes replace the file. `checkout_paths` is not called.

**Image.** The user message contains a ```image fence. `to_api_messages` still has a string. `_call_llm_once` computes `caller_key`. `apply_image_parts` reads the file through `read_image_data_uri`. OpenAI gets `image_url`. Anthropic's converter turns that part into an image source block. MiniMax gets the text with `[image omitted]`.

**Terminal.** Show terminal. `start` opens a pty, spawns `$SHELL`, and the reader thread idle-adds output. Close request calls `stop`, which `killpg`s the group.

**Dream.** `/dream` threads `run_dream`. The Improve card's `caller` is passed to `get_provider`. `call` returns a dict. The assistant text is written to `dream-proposals/`. The role prompt file is not opened for write. `/dream accept <basename>` copies that file to `dream-accepted/coder.md`. The next `compose_system_prompt` for a trusted project appends it.

**Push.** Confirm. Worker calls `get_branch`. On success and a real branch name, `push(path, "origin", that_name)`. `GitResult.error` is shown on failure. The main thread does not call `origin.push`.

---

## 4. File change summary

| File | Change | Risk |
|---|---|---|
| `utils/ui_state.py` | New load/save | Low. A bad file loads as `{}`. |
| `ui/window.py` | Save/restore, wire choice, terminal, push | Medium. Close must still return `False`. |
| `ui/views/main_content.py` | Keep the chat paned, choice box, branch button, push button, source page, terminal toggle | Medium. `update_project_settings` rebuilds the bar. |
| `ui/views/left_panel.py` | No logic change if `_PAP_notebook` is already stored | Low. It is already stored. |
| `ui/handlers/project_handler.py` | No signature change | Low. Solo restore uses the existing setter. |
| `ui/handlers/chat_handler.py` | `"ask"` does not send | High. A missing setting no longer wakes the Supervisor. |
| `agent/config.py` | Default and allowed values | Medium. Stored values win. |
| `ui/views/source_view.py` | New | Low. |
| `ui/views/file_tree.py` | Activate opens source. Right-click opens the drawer | Medium. Double-click no longer toggles the drawer. |
| `render/feed_html.py`, `ui/handlers/feed_handler.py` | Open action | Low. |
| `utils/turn_snapshot.py`, `agent/tools.py`, crabwatch, `ui/feed/review.py` | Capture and restore | High. Reject of a snapshotted card no longer checks out git. |
| `utils/image_paths.py`, `render/html.py` | Shared data-URI reader | Low. Display path stays the same function's rules. |
| `agent/llm/images.py`, `convert.py`, `runtime.py` | Wire parts | High. Anthropic list content must be converted or the call fails. |
| `ui/views/person_terminal.py` | New pty | Medium. Close must kill the group. |
| `utils/dream.py`, `prompt_loader.py`, `ui/handlers/dream_handler.py` | Propose and accept | High. Must not write the live system prompt. |
| `utils/git_ops.py` | List and checkout | Medium. Checkout refuses a dirty tree. |
| `docs/acceptance/rows.yaml` | R-23–R-30 with the tests | High. |

---

## 5. Implementation order

1. **UI state.** Round-trip a temp config dir. A non-int `width` is ignored. Close's `OSError` does not change the `False` return.
2. **Ask.** Missing setting does not call `_send_local` until `complete_room_choice`. Stored `"all"` still fans out on `[]`. A resolver result of `["all"]` still fans out without buttons.
3. **Source view.** A path with `..` that leaves the project returns `"outside"` and does not read. Double-click calls open. Right-click still toggles the drawer.
4. **Snapshot.** Write a new file, reject, the file is gone. Write over an existing file, reject, the old bytes are back. A card with no `snapshot_id` still calls `checkout_paths`.
5. **Images.** An OpenAI caller message contains `image_url`. An Anthropic conversion of that list contains `source.type == "base64"`. MiniMax content stays a str. A path outside the roots becomes `[image omitted]`.
6. **Terminal.** `start` spawns a process. `stop` kills the group. `agent/tools.py` has no new tool name.
7. **Dream,** after SPEC-66's fence. No new journal lines returns `"nothing new"` and does not call `get_provider`. Accept writes `dream-accepted/coder.md` and does not modify `prompts/system/coder.md`. An untrusted project does not include the accepted file.
8. **Branch and push,** after SPEC-66's path-scoped accept. Dirty tree: `checkout_branch` returns `success=False` and does not call `git checkout`. Push runs off the main thread. Detached HEAD does not call `push`.

Run the slice's tests before the next slice. Add `docs/acceptance/rows.yaml` entries in the same change as the tests, not before the nodes exist.

---

## 6. Acceptance criteria

- [ ] A saved width and height are applied on the next `MainWindow` build. A missing file still builds at 800×600.
- [ ] The left notebook page, the three pane positions, the last project path, and the solo target survive a save/load of `ui-state.json`.
- [ ] A solo target that is not a current member is not applied.
- [ ] With no `jev_room_fallback` key, a resolver result of `[]` does not call `_send_local` until a button is chosen.
- [ ] A stored fallback of `"supervisor"` still routes `[]` to the Supervisor without buttons.
- [ ] `["all"]` still messages every member with no chooser.
- [ ] `show_file` of a path outside the project returns `"outside"` and does not open the file.
- [ ] Double-click opens the source page. Right-click toggles the drawer.
- [ ] Reject of a card with `snapshot_id` restores those bytes or removes a file that was absent, and does not call `checkout_paths`.
- [ ] Reject of a card without `snapshot_id` still calls `checkout_paths`.
- [ ] OpenAI wire content for a ```image fence includes `image_url` and the `data:` URI from `read_image_data_uri`.
- [ ] Anthropic conversion of that list uses `source.media_type` and base64 data, not `image_url`.
- [ ] MiniMax wire content is a string containing `[image omitted]`.
- [ ] The terminal process is a new session and `stop` kills the group. No tool named for it is registered.
- [ ] `run_dream` with an empty new-journal set does not call the provider and does not write a proposal.
- [ ] `accept_proposal` writes `dream-accepted/coder.md` and leaves `prompts/system/coder.md` unchanged.
- [ ] An untrusted project’s composed prompt does not contain the accepted dream file.
- [ ] `checkout_branch` on a dirty porcelain map does not call git.
- [ ] Push uses `get_branch`'s stdout as the branch argument and does not run on the thread that clicked the button.

---

## 7. Acceptance rows (SPEC-47)

The implementer adds these to `docs/acceptance/rows.yaml` with the tests. The nodes do not exist yet.

| Row | Claim | Node | Mutation | Registered |
|---|---|---|---|---|
| R-23 | Saved window and pane ints reload, and a wrong-typed width is ignored. | `tests/test_spec67_ui_state.py::test_round_trip_and_bad_width` | `tests/mutations/R-23.diff` | 2026-11-18 |
| R-24 | Missing room-fallback setting does not send on `[]` until a choice is completed. | `tests/test_spec67_room_ask.py::test_ask_waits` | `tests/mutations/R-24.diff` | 2026-11-18 |
| R-25 | A source path outside the project is refused and not read. | `tests/test_spec67_source_view.py::test_outside_path_is_not_read` | `tests/mutations/R-25.diff` | 2026-11-18 |
| R-26 | Reject restores a snapshot and does not call checkout when the id is present. | `tests/test_spec67_snapshot.py::test_restore_skips_checkout` | `tests/mutations/R-26.diff` | 2026-11-18 |
| R-27 | OpenAI parts, Anthropic blocks, and MiniMax omission follow the caller. | `tests/test_spec67_images.py::test_parts_follow_caller` | `tests/mutations/R-27.diff` | 2026-11-18 |
| R-28 | The person terminal is not registered as an agent tool, and stop kills the group. | `tests/test_spec67_terminal.py::test_stop_kills_and_no_tool` | `tests/mutations/R-28.diff` | 2026-11-18 |
| R-29 | An empty dream input writes no proposal, and accept does not modify `prompts/system/coder.md`. | `tests/test_spec67_dream.py::test_nothing_new_and_accept_path` | `tests/mutations/R-29.diff` | 2026-11-18 |
| R-30 | A dirty tree refuses checkout, and push is invoked with the current branch off the click thread. | `tests/test_spec67_git_bar.py::test_dirty_checkout_and_push_branch` | `tests/mutations/R-30.diff` | 2026-11-18 |

`runner: plain` for R-23, R-26, R-27, R-29, R-30. `runner: xvfb` for R-24, R-25, R-28.

Each mutation deletes the one guard the node asserts: the type check on `width`, the `"ask"` branch, the `commonpath` refusal, the `snapshot_id` branch in `handle_reject`, the `apply_image_parts` call, the `stop` kill, the `"nothing new"` return, or the porcelain check in `checkout_branch`. A mutation the node still passes is not a row.

---

## 8. Edge cases

| Case | Expected |
|---|---|
| `ui-state.json` is a list | `load_ui_state` returns `{}`. |
| Width is `true` | Dropped. `bool` is not accepted as an int. |
| Saved project path was deleted | `open_project` is not called. The rest of the state still applies. |
| Saved solo key was removed from the team | `set_solo_target` is not called with it. The room stays ALL. |
| `get_width()` before realize | Close can run after the window was shown. If it returns 0, do not save width or height. Leave the previous ints. |
| Resolver returns `[]` and the setting is `"all"` | Every member is sent. No buttons. |
| Resolver returns `["all"]` and the setting is `"ask"` | Every member is sent. No buttons. |
| Person sends a new line while buttons are up | `_pending_room` is replaced. The old text is not delivered. |
| `complete_room_choice` with no pending tuple | No send. |
| Source file is 2 MB | `"too_large"`. The buffer text is the reason line. |
| Source file contains a NUL in the first 512 bytes | `"binary"`. The file is not fully read. |
| Snapshot `capture` cannot read the old file | Returns `None`. The write still proceeds. The card has no id. Reject uses git. |
| Snapshot id on the card does not match `[0-9a-f]{16}` | `restore` returns `"outside"`. Reject falls through to `checkout_paths`. |
| Image fence path is a symlink out of the project | `is_path_in_allowed_roots` is false. The part is `[image omitted]`. |
| Five image fences, OpenAI caller | Four `image_url` parts. The fifth is `[image omitted]`. |
| User content is already a list | `apply_image_parts` copies it and does not parse fences inside parts. |
| `get_provider` raises `KeyError` | `run_dream` returns that as the error string. No proposal file. |
| `validate_provider_url` raises `ValueError` | Same. No proposal file. |
| Dream journal contains a fence closer | `_untrusted_fence` escapes it before the user message is built. |
| Accept basename is `../coder.md` | Error string. `dream-accepted/` is not written. |
| Project is not trusted | Accepted file is not appended. |
| `status_porcelain` raises | `checkout_branch` returns `success=False` with `_safe_error`. No checkout. |
| `get_branch` returns `(detached HEAD)` | Push is not called. |
| `push` `info` is empty | The existing `Exception` handler returns `success=False`. |
| Terminal `Popen` fails | The view shows the error. No reader thread. |
| Terminal already started and the project changes | `stop` then `start` with the new cwd. |

---

## 9. ARCHITECTURE.md updates required

Add, after this lands:

- A `utils/ui_state.py` row: `ui-state.json` under the config dir, atomic, `0o600`.
- Chat handler: missing `jev_room_fallback` means `"ask"`. `[]` waits for a button. Stored `supervisor` / `all` / `drop` are unchanged.
- File tree: primary activation opens `SourceView`. Secondary click toggles the drawer.
- `utils/turn_snapshot.py`: capture before write, restore on reject when `metadata.snapshot_id` is set.
- `agent/llm/images.py`: wire-only image parts. Conversation storage stays text.
- `ui/views/person_terminal.py`: pty, not a tool, not scrubbed.
- Dream: proposals under `.crabcakes/dream-proposals/`, accepted text under `.crabcakes/dream-accepted/`, included only for a trusted project.
- Project bar: branch button calls `list_local_branches` / `checkout_branch`. Push calls `git_ops.push` off the main thread.

---

## Self-audit

Checked against the tree while writing:

- `load`/`save` of UI state is new. The GTK calls it uses (`set_default_size`, `Paned.set_position`, `Paned.get_position`, `Notebook.set_current_page`, `Notebook.get_current_page`, `Window.get_width`, `Window.get_height`) are the ones already used or the GTK4 methods on the widgets this file already constructs. `set_position` is verified at `window.py:846`, `:864`, and `main_content.py:268`.
- `get_agent_setting` never raises. The new default is passed by the caller. A stored string still wins.
- `resolve_room_addressee` is not given a new return value. `[]` remains the unsure result.
- `show_file` uses `commonpath` and treats `ValueError` as outside, matching `_resolve_project_path`.
- `ToolResult.to_dict` stays free of `snapshot_id`, matching `enforcement_checks`.
- `read_image_data_uri` preserves `_local_image_uri`'s fail-closed `None`.
- `convert_messages_for_anthropic` today passes list content through. The spec changes that branch because an unchanged list would be an invalid Anthropic body.
- `get_provider` raises `KeyError`. `run_dream` catches it.
- `checkout_paths` is unchanged. Reject uses it only when there is no usable snapshot.
- `push` and `get_branch` signatures are the ones in `utils/git_ops.py`. The branch argument is the stdout of `get_branch`, not a hardcoded `main`, except that `push`'s default parameter remains `main` for other callers. This spec's button does not rely on the default.
- No code was changed. The suite was not run. The implementer pastes pytest output per slice.
