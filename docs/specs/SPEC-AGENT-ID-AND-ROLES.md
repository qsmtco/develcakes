# SPEC: Agent ids and user-defined roles

**Date:** 2026-10-22
**Author:** Micah
**Status:** Draft — for implementation
**Implements:** design agreed in chat, 2026-10-22
**Depends on:** none
**Target branch:** main

> Architecture: agent definitions stay in `utils/agent_defs.py`. New role files stay in a new `utils/roles.py` with no GTK. The agent editor stays in `ui/views/agent_builder.py` and `ui/handlers/agent_builder_handler.py`. Prompt composition stays in `utils/prompt_loader.py`, called from `agent/context.py` `build_system_prompt`. Session keys stay the registry key in `agent/special_agents.py` (`conv_id_prefix`). `ARCHITECTURE.md` §3.21r is updated in the same change.

---

DISCOVERY:
- Read `ui/views/agent_builder.py`: Role is `Gtk.Entry` `_role_entry` (line 105). `get_values()` (line 175) sets `role` from that entry, or from the name if the entry is empty. System Prompts is a `Gtk.ListBox` of `Gtk.CheckButton`s built by `_build_prompts_list()` (line 438) from `AgentBuilderHandler.get_prompt_options()`. `_get_selected_prompts()` (line 466) returns the `filepath` strings of checked rows. `_update_save_button()` (line 765) enables Save only when name, at least one prompt checkbox, at least one tool, a provider, and a fallback are set. `_fill_form()` (line 704) writes `agent_def["role"]` into the entry. Compaction already uses `Gtk.DropDown.new_from_strings`.
- Read `ui/handlers/agent_builder_handler.py`: `save(agent_def) -> tuple[bool, list[str]]` (line 85) calls `validate_agent_def`, then on rename `delete_agent_def(old_name)` before `save_agent_def`. `create_new()` (line 57) returns `role: ""`.
- Read `utils/agent_defs.py`: `save_agent_def(agent_def) -> str` (line 498) writes `<config>/agents/<sanitized-name>.yaml`. `_assign_session_key` (line 466) keeps an explicit `session_key`, otherwise writes `special:<role>`, or `special:<role>-2` when that key is taken. `_PRESERVED_ON_EDIT` includes `session_key`. `validate_agent_def` (line 566) requires a non-empty `prompts` list and checks each path against `prompts/<p>` and `prompts/system/<p>`. `get_available_prompts()` (line 751) returns `[{name, filepath}]` with filepaths like `system/coder.md` or `steelFramedCodeWriter.md`. `_derive_role` (line 120) uses the `role` field, lowercased, or the name.
- Read `agent/special_agents.py` `_load_registry()` (line 73): key is `session_key` or `f"special:{role}"`. A duplicate key logs a warning and the later file replaces the earlier one. `SpecialAgentDef` has no `prompts` field. `get_special_agent(prefix) -> SpecialAgentDef | None`.
- Read `utils/prompt_loader.py` `compose_system_prompt(...)` (line 148): if `agent_role == "coder"` it returns `coder.md` filled with `AGENT_NAME` and `TOOL_LIST` and does not read the agent's prompt list. Debugger and supervisor append `debugger.md` / `supervisor.md` after the shared templates (lines 276–283). `_get_agent_self_improvement_config(agent_role)` (line 128) calls `load_agent_def_by_role`, which returns the first file with that role. Bug-journal files are `{agent_role}-bugs.md` (line 299). `load_prompt_template(name) -> str | None`. `fill_template(template, variables) -> str`.
- Read `agent/context.py` `build_system_prompt(agent_name, project_path, tools, review_mode="off", agent_role="", model_max_tokens=None, *, context_mode="auto") -> str` (line 1027). It calls `compose_system_prompt` and, when `agent_role` is empty, guesses coder/debugger/supervisor from the display name.
- Read `agent/runtime.py`: `create_conversation(...)` (line 1196) passes `agent_role` into `build_system_prompt` and does not pass the YAML prompt list. `_coder_shell_allowed(command, project_path) -> str | None` (line 435) refuses shell use when `conv.agent_role == "coder"` (line 2432). The same role check forces `lint_check=False, test_run=False` (line 2551) and clears the repo-map block (line 3816).
- Read `agent/persistence.py` `save_conversation_to_disk(conv, session_key) -> str` (line 42): file is `<conversations_dir>/<session_key>.json`. The loader builds `Conversation` with explicit kwargs (line 164) and does not pass unknown keys, so a new field with a default does not break old files.
- Read `agent/group_tools.py`: `_require_supervisor` (line 68) allows an action only when `role == "supervisor"`. `_resolve_peer` (line 75) matches session key, display name, or role, and rejects a key that does not start with `special:` (lines 81 and 105).
- Read `utils/project_memory.py` `dispatch(project_path, session_key, role, action, *, name="", body="", query="", scope="shared")` (line 64): `del session_key`, then private files go under `agents/<role-slug>/`.
- Read `models/colors.py` `color_for_special_agent(role: str) -> str` (line 48): the cache dict `_SPECIAL_AGENT_COLORS` is keyed by the string passed in. Callers pass `agent_def.role` (`ui/window.py:761`, `ui/handlers/chat_render_handler.py:813`, `ui/handlers/agent_list_handler.py:87`).
- Read `ui/handlers/chat_handler.py`: a project send with no solo target fans out to every member (lines 420–433). An `/ask` tab label is `target.split(":", 1)[-1]` (line 246), so `special:coder-2` is shown as `coder-2`.
- Read `ui/views/review_bar.py` `set_queue_view`: the chip label is `f"{agent_key} ({count})"` (line 239). The queue key is the session key.
- Read `ui/handlers/agent_runtime_handler.py` `_worktree_for_turn` (line 936): after SPEC-49 the rule is suffix-only — a writer returns `None` (project root) when its key is exactly `special:<role>` with no suffix, and gets a worktree otherwise (`_suffixed_writer`, line 49). `runtime_for` (line 845) returns `None` unless the key starts with `special:`. `WorktreeManager.ensure_worktree` is called inside `except Exception`, which already covers `WorktreeError` and `ValueError`. (Pre-SPEC-49 this branch also consulted a work lease; that system is gone.)
- Read `utils/worktree_manager.py` `worktree_id_for_session(session_key) -> str` (line 130): replaces `:` with `-`, then `_validate_agent_id`. `agent:alphacoder` becomes `agent-alphacoder`. Raises `ValueError` on an empty or illegal id. `ensure_worktree(worktree_id) -> str | None` raises `WorktreeError` on git failure and returns `None` when the manager is disabled.
- Read `ui/agent_runtime/session.py`: `clear_conversation`, `compact_conversation`, and `_owned_conversation` return early unless the key starts with `special:`.
- Read `ui/handlers/project_handler.py`: membership records are created only for `special:` keys (line 576). `/clear` and `/compact` dispatch only for `special:` keys (lines 790 and 876).
- Read `ui/views/left_panel.py`: `startswith("special:")` chooses the DevelCakes tag (line 543) and the Edit/Delete menu (line 610). Any other key is treated as an OpenClaw gateway agent.
- Read `utils/feedback_processor.py` (line 91): role lookup runs only for `special:` keys.
- Read `utils/config.py` `get_config_dir() -> str` (line 15): `$XDG_CONFIG_HOME/develcakes` or `~/.config/develcakes`. It does not create the directory.
- Read `prompts/system/coder.md`, `debugger.md`, `supervisor.md`: coder is the short harness (`You are {{AGENT_NAME}}`). Debugger is the investigation stance. Supervisor is the orchestrator stance and names Coder and Debugger as delegates.
- Read `~/.config/develcakes/agents/alphacoder.yaml` and `coder.yaml`: both `role: coder`. Coder's key is `special:coder`. AlphaCoder's key is `special:coder-2`. AlphaCoder's prompt list is `codeWriter.md` and `steelFramedCodeWriter.md`. There is no `conversations/special:coder-2.json`. `conversations/special:coder.json` is only Coder.
- Architecture owner: `utils/agent_defs.py` owns agent files. `agent/special_agents.py` owns the in-memory registry keyed by session key. `utils/prompt_loader.py` owns system-prompt text.
- Existing patterns to copy: YAML load/save and `_PRESERVED_ON_EDIT` in `utils/agent_defs.py`; `Gtk.DropDown.new_from_strings` already in the agent editor; `get_config_dir()` for the new role directory.

---

## 1. Overview

### Problem

Two agents with the same role are not two agents. The role string is also the session key (`special:coder`), the prompt selector, the color, the private-memory folder, and a peer-lookup name. The current saver papers over a collision by writing `special:coder-2`. The Agents tab shows the display name, while `/ask` tabs and review chips show the key. The System Prompts checklist is stored on the agent and never composed. A project message with no solo target is delivered to every member, so replies from separate agents land in one pane.

### Solution

An agent gets one id when it is created, from the slug of its name: AlphaCoder becomes `agent:alphacoder`. That string is the session key for the conversation, membership, review queue, worktree, and private memory. It does not change when the role or the label changes.

A role is a name plus one prompt. Coder, Debugger, and Supervisor ship as the three built-in roles and keep the behavior already tied to those words. Any other role is created from the Role dropdown in the agent editor. The dropdown's last item, "Create role…", opens a small dialog for a name and a prompt. There is no Roles page in Settings.

The agent's own System Prompts checklist is appended after the role prompt. That is how two coders differ.

### Scope

| In | Out |
|---|---|
| Id `agent:<slug>` for agents created after this change | Rewriting `special:coder`, `special:debugger`, `special:supervisor`, or an existing `special:coder-2` |
| Role dropdown and Create-role dialog in the agent editor | A Settings section for roles |
| A role file is a name and a prompt | Tools, provider, or compaction on a role |
| Compose the role prompt and the agent's checked prompts | Changing `coder.md`, `debugger.md`, or `supervisor.md` |
| Built-in role behavior stays on the role word (`coder` shell limits, `supervisor` tool gates) | Giving a user-created role those gates |
| Local-agent gates accept `agent:` as well as `special:` | Gateway session keys |
| Private memory and color keyed by session key | Migrating old `agents/coder/` notes |
| Extra writers use a worktree; the three built-in keys stay on the project root | Auto-merge of worktrees |
| A plain project message does not fan out | Removing `@` broadcast |

### Principles

- The id is assigned once and then only read.
- Role is shared. Transcripts, memory, color, and worktrees are not.
- A user-created role adds prompt text and nothing else.
- GTK stays in `ui/`. Role files are read and written from `utils/roles.py`.

---

## 2. Changes by file

### 2.1 `utils/roles.py` (new)

Owns role files under `get_config_dir()/roles/`. One file per role, `<slug>.yaml` (JSON only when PyYAML is missing, same fallback as `save_agent_def`).

```python
BUILTIN_ROLES: tuple[str, ...] = ("supervisor", "coder", "debugger")

def role_slug(name: str) -> str:
    """Lowercase, spaces to hyphens, drop every other character.
    Strip leading and trailing hyphens and underscores. '' when nothing remains.
    """

def load_roles() -> list[dict]:
    """User roles only, sorted by name. Each dict is
    {name: str, slug: str, prompt: str}. Skips a file that does not parse,
    has an empty name, or has an empty prompt. Does not include the three
    built-in names.
    """

def load_role(slug: str) -> dict | None:
    """One user role, or None. Builtin slugs return None (they are not files)."""

def save_role(name: str, prompt: str) -> tuple[str | None, str | None]:
    """Write the role. Returns (slug, None) on success, (None, error) on refusal.
    Refuses an empty name, an empty prompt, a slug in BUILTIN_ROLES, or a slug
    that already has a file. Does not overwrite.
    OSError from the write is returned as the error string, not raised.
    """
```

`save_role` creates `roles/` with `os.makedirs(..., exist_ok=True)`. `get_config_dir()` itself does not create directories. The file body is `name`, `slug`, and `prompt`. `prompt` is the dialog text, stored as a string, not as a path under `prompts/`.

Return value: the caller shows `error` in the dialog and does not close it. `slug` is how the dropdown selects the new row.

### 2.2 `utils/agent_defs.py`

Replace `_assign_session_key`.

Built-in keys that stay on the project root and are never minted for a new agent:

```python
BUILTIN_SESSION_KEYS = (
    "special:supervisor",
    "special:coder",
    "special:debugger",
)
```

New assignment, after the existing preserve step in `_prepare_persisted_def`:

1. If `session_key` is already a non-empty string, keep it. This covers edits, renames, and the files that already say `special:coder` or `special:coder-2`.
2. Otherwise slug the display name with the same character rule as `role_slug`.
3. If the slug is empty, leave `session_key` unset and let `validate_agent_def` report "Name does not yield an id".
4. The key is `agent:<slug>`.
5. If that key is in `BUILTIN_SESSION_KEYS` or in `_session_keys_owned_by_others`, leave it unset and let validation report "An agent with this id already exists".
6. Do not write `special:<role>` and do not write `special:<role>-N`.

`validate_agent_def` changes:

- Require `role` to be one of `BUILTIN_ROLES` or a slug returned by `load_roles()`. Error: `Unknown role: <role>`.
- `prompts` may be an empty list when `role` is non-empty. A missing role and an empty prompt list is still an error.
- When the id cannot be assigned, append the error strings from step 3 or 5. `save_agent_def` is only called after validation, so it can trust the key is either preserved or free.

`AgentBuilderHandler.save` must copy `session_key` off the old file before `delete_agent_def` on rename. The new path does not exist yet, so `_PRESERVED_ON_EDIT` cannot see the old key. Put the copied key on the dict passed to `save_agent_def`. A rename keeps the id. A role change keeps the id.

### 2.3 `agent/special_agents.py`

Add `prompts: list[str]` to `SpecialAgentDef`, default `field(default_factory=list)`, set from `agent_def.get("prompts") or []` in `_load_registry`. Ignore entries that are not strings.

Add:

```python
def is_local_agent_key(session_key: str) -> bool:
    return isinstance(session_key, str) and (
        session_key.startswith("special:") or session_key.startswith("agent:")
    )
```

`_load_registry` still defaults a missing `session_key` to `special:<role>` so an old hand-edited file keeps its current key. New files written by `save_agent_def` always have `session_key`, so this default is not the creator of new ids.

Duplicate keys still log and replace. That path should stop being hit for new agents because validation refuses a taken id.

### 2.4 `utils/prompt_loader.py`

`compose_system_prompt` gains two keyword-only arguments, both optional so current callers and tests keep working:

```python
prompt_files: list[str] | None = None,
si_config: dict | None = None,
```

Behavior:

- `agent_role == "coder"` keeps the current early return of `coder.md` plus the tool list. Before that return, append `_read_agent_prompts(prompt_files)`, skipping a path whose normalized form is `system/coder.md` so the harness is not pasted twice.
- For any other role, keep the shared templates and the debugger/supervisor branches as they are. When `load_role(agent_role)` returns a dict, append its `prompt` string where the role template would have gone. Do not also append `coder.md` / `debugger.md` / `supervisor.md` for that role.
- `_read_agent_prompts` resolves each path the way `validate_agent_def` does: `prompts/<p>` then `prompts/system/<p>`, under the app `prompts/` directory (`os.path.dirname(os.path.dirname(__file__)), "prompts"`). A missing file is skipped and logged. File text is wrapped by the existing `_untrusted_fence` only when the path is not under `prompts/system/`. System templates are the app's own text. A user prompt file is project-facing text and gets the fence.
- When `si_config` is a dict, use it instead of `_get_agent_self_improvement_config(agent_role)`. Journal and rules filenames become `<session_slug>-bugs.md` and `<session_slug>-rules.md` when a new keyword-only `memory_slug: str = ""` is non-empty. When it is empty, keep `{agent_role}-bugs.md` so existing callers do not change. The coder early return still does not load those files.

`build_system_prompt` in `agent/context.py` gains `session_key: str = ""`. When that is non-empty it loads `get_special_agent(session_key)` and passes `prompt_files=list(agent.prompts)` and `si_config=agent.get_self_improvement_config()`. `memory_slug` is `WorktreeManager.worktree_id_for_session(session_key)` inside a `try/except ValueError`, and `""` when that raises. The name-based role guess (`"coder" if "coder" in agent_name.lower()`) runs only when `agent_role` is empty, as it does today. Do not guess a role for an agent that has one.

Call sites that already have a session key pass it:

- `AgentRuntime.create_conversation` (the `build_system_prompt` call near the end of that method)
- `AgentRuntime._ensure_system_prompt`
- `AgentRuntime._rebuild_system_prompt_if_inputs_changed`

`_system_prompt_fingerprint` includes the prompt-path tuple and the role slug so an edit of either rebuilds the prompt on the next turn. Prompt file body changes are picked up the same way `load_prompt_template` already caches for the process: not until restart. That matches `coder.md` today.

### 2.5 `ui/views/agent_builder.py`

Replace `_role_entry` with a `Gtk.DropDown`.

Rows, in order: `Supervisor`, `Coder`, `Debugger`, then user role names from `load_roles()`, then `Create role…`.

Store parallel lists on the dialog: display labels, and values `supervisor` / `coder` / `debugger` / `<slug>` / `""` for the sentinel. `get_values()` writes the value, not the label. `create_new()` leaves the dropdown on Coder (`coder`), which is the previous placeholder's first example.

`notify::selected`: if the sentinel row is selected, open the create dialog and set the dropdown back to the previous index. Remember the previous index on each successful non-sentinel change.

Create dialog: a `Gtk.Window`, `modal=True`, `transient_for` the agent-builder window, title `Create role`. Fields: Name (`Gtk.Entry`), Prompt (`Gtk.TextView` in a `Gtk.ScrolledWindow`). Buttons: Cancel (close, no write) and Create. Create calls `AgentBuilderHandler.save_role(name, prompt_text)`, which returns the `(slug, error)` tuple from `utils.roles.save_role`. The view does not write the role file. On error, show the string on a label in the dialog and leave it open. On success, rebuild the dropdown from `get_role_options()`, select the new slug, and close the dialog.

`_update_save_button`: `has_prompts` is true when any prompt checkbox is on or a non-sentinel role is selected. A role is a prompt, so the checklist may be empty.

`_fill_form`: select the row whose value equals `agent_def["role"]`. If the stored role is absent from the list, insert it before the sentinel and select it so an edit does not silently retarget the agent.

No new CSS class is required. Reuse `agent-builder-error` for the dialog's error label.

### 2.6 `ui/handlers/agent_builder_handler.py`

Add `get_role_options() -> list[tuple[str, str]]` returning `(label, value)` for the three built-ins plus `load_roles()`. Add `save_role(name, prompt) -> tuple[str | None, str | None]` that calls `utils.roles.save_role` and returns its tuple unchanged. The view already imports `VALID_COMPACTION_STRATEGIES` from `utils.agent_defs`, but the file header says persistence stays in the handler. The dialog uses these two methods and does not import `utils.roles`.

On rename, copy `session_key` as described in §2.2 before deleting the old file.

### 2.7 Local-agent key gates

Replace `session_key.startswith("special:")` with `is_local_agent_key(session_key)` in:

- `ui/agent_runtime/session.py`: `clear_conversation`, `compact_conversation`, `_owned_conversation`
- `ui/handlers/agent_runtime_handler.py`: `runtime_for`
- `ui/handlers/project_handler.py`: member construction (`elif sk.startswith("special:")`), `/clear`, `/compact`
- `ui/views/left_panel.py`: the DevelCakes tag and the Edit/Delete menu
- `utils/feedback_processor.py`: the role lookup
- `agent/group_tools.py`: both `startswith("special:")` checks in `_resolve_peer`

`/clear` and `/compact` currently label the agent with `sk.split(":", 1)[1]`. For `agent:alphacoder` that is `alphacoder`. Use `get_special_agent(sk).display_name` when the lookup returns an agent, and the split tail only when it returns `None`.

`_resolve_peer` stops matching `role`. Two agents with role `coder` must not both match the needle `coder`. Match `conv_id_prefix` and `display_name` only. A miss still returns `no agent named <needle>`. Two display names that fold to the same string still return `agent name <needle> is ambiguous`.

### 2.8 `models/colors.py` and its three callers

`color_for_special_agent` keeps its signature and its cache. Callers pass `agent_def.conv_id_prefix` instead of `agent_def.role`. Two agents with role `coder` then get two colors. Existing processes keep the colors already cached under role strings until restart; that is acceptable because the cache is process-local.

### 2.9 `utils/project_memory.py`

Stop discarding `session_key`. For `scope == "private"`, slug the session key with `WorktreeManager.worktree_id_for_session`. On `ValueError`, return `ToolResult(success=False, error="caller has no role")` — that is the existing error for an empty slug. Shared scope is unchanged and stays project-wide.

Do not read or move `agents/coder/`. New private notes for `special:coder` go to `agents/special-coder/`. Old notes stay on disk, unreferenced.

### 2.10 `ui/handlers/agent_runtime_handler.py` worktrees

Delete `_suffixed_writer`.

```python
_ROOT_KEYS = frozenset({
    "special:supervisor",
    "special:coder",
    "special:debugger",
})
```

Inside `_worktree_for_turn`, after the `can_write` and `project_path` checks:

- If `session_key` is in `_ROOT_KEYS`, return `None` (the built-in Coder, Debugger, and Supervisor stay on the open project).
- Otherwise call `WorktreeManager.worktree_id_for_session` and `ensure_worktree`.
- Keep the `except Exception` that logs and returns `None`. `ValueError` from a bad id and `WorktreeError` from git both land there. A non-git project still gets `None` from a disabled manager.

`agent:alphacoder` and an existing `special:coder-2` both get a worktree. `special:coder` does not.

### 2.11 `ui/handlers/chat_handler.py` and `ui/views/review_bar.py`

Tab label at the `/ask` create-tab site: `get_special_agent(target).display_name` when that returns an agent, else `target.split(":", 1)[-1]`.

Project send: when there is no solo target, do not loop members. Leave the text in place is wrong because the current function clears the buffer after dispatch. Show one system line in the project tab: `Choose an agent, or start with @ to reach everyone.` Do not call `send_to_special_agent`. `@` with an empty name still resolves to every member through `_resolve_mention`. `@Name` still resolves to one key.

Review chips: `set_queue_view` still receives `(agent_key, count)` and the queue is still keyed by session key. The chip label uses a new optional callback `label_for(agent_key) -> str` set by `ReviewHandler` from `get_special_agent`. When the lookup misses, the label is the key. The count stays in the label.

### 2.12 What stays role-gated

These keep comparing `agent_role` / `role` to the literal words. An agent whose dropdown value is `coder` gets them even when its id is `agent:alphacoder`. A user-created role does not.

- `agent/runtime.py` `_coder_shell_allowed` gate, lint/test suppression, repo-map suppression
- `agent/group_tools.py` `_require_supervisor`
- `utils/prompt_loader.py` supervisor onboarding template (`agent_role == "supervisor"` and the project is not onboarded)

**Files NOT changed:**

- `prompts/system/coder.md`, `debugger.md`, `supervisor.md` — the built-in role texts
- `agent/runtime.py` role comparisons listed above — behavior stays, callers already pass `conv.agent_role`
- `utils/worktree_manager.py` — `worktree_id_for_session` and `ensure_worktree` already accept `agent:alphacoder`
- `agent/persistence.py` — the conversation file is already `<session_key>.json`

(The work-unit system referenced by earlier drafts of this section — `models/work_unit.py`, `ui/handlers/work_handler.py` — was removed entirely by SPEC-49 and no longer exists.)

---

## 3. Data flow

### Create a role

1. User opens Create Agent. `AgentBuilderHandler.create_new()` supplies the blank dict. The dropdown lists Supervisor, Coder, Debugger, saved roles, Create role….
2. User selects Create role…. `notify::selected` sees the sentinel, restores the previous index, and opens the modal window.
3. Create calls `AgentBuilderHandler.save_role(name, text)`, which returns `utils.roles.save_role`'s `(slug, error)`.
4. On `(slug, None)`, the handler's `get_role_options()` is read again, the dropdown selects `slug`, and the window closes.
5. On `(None, error)`, the dialog label shows `error`. Nothing is written.

### Create an agent

1. User sets name AlphaCoder, role `coder`, optional extra prompt checkboxes, tools, providers.
2. `_do_save` calls `get_values()`. `role` is `coder`. `prompts` is the checked filepaths. `session_key` is absent.
3. `AgentBuilderHandler.save` runs `validate_agent_def`. Role `coder` is built-in. Empty extra prompts are allowed.
4. `save_agent_def` → `_prepare_persisted_def` → `_assign_session_key` writes `session_key: agent:alphacoder` because the name slug is free.
5. `reload_registry` → `_load_registry` registers `SpecialAgentDef(conv_id_prefix="agent:alphacoder", role="coder", prompts=[...], display_name="AlphaCoder")`.
6. The next turn's `create_conversation` / `_ensure_system_prompt` calls `build_system_prompt(..., session_key="agent:alphacoder")`.
7. `compose_system_prompt(agent_role="coder", prompt_files=that list)` returns the coder harness plus the checked files.
8. `save_conversation_to_disk` writes `conversations/agent:alphacoder.json`.
9. `_worktree_for_turn` sees a writer whose key is not in `_ROOT_KEYS`, calls `worktree_id_for_session` → `agent-alphacoder`, then `ensure_worktree`. The turn's `project_path` becomes that directory. `special:coder` still gets `None` and edits the open project.

### Edit and rename

1. `load_for_edit` loads the file, including `session_key`.
2. The form does not display the id.
3. On save, the handler copies `session_key` from the pre-edit def onto the submitted dict.
4. A name change deletes the old filename only after that copy. The new file keeps the same `session_key`.

### Project send

1. Text with no `@` and no solo target: one system line, no `send_to_special_agent`.
2. `@AlphaCoder` resolves through `_resolve_mention` by display name to `agent:alphacoder`.
3. `@` with an empty name still returns every member key.

---

## 4. File change summary

| File | Change | Risk |
|---|---|---|
| `utils/roles.py` | New role store | Low |
| `utils/agent_defs.py` | Id assignment, role validation | High |
| `agent/special_agents.py` | `prompts` field, `is_local_agent_key` | Medium |
| `utils/prompt_loader.py` | Append agent prompts and user role text | High |
| `agent/context.py` | Pass session key into compose | Medium |
| `agent/runtime.py` | Pass session key from the three prompt-build sites; fingerprint | Medium |
| `ui/views/agent_builder.py` | Role dropdown and dialog | Medium |
| `ui/handlers/agent_builder_handler.py` | Role options, preserve id across rename | Medium |
| `ui/agent_runtime/session.py` | Accept `agent:` | Medium |
| `ui/handlers/agent_runtime_handler.py` | `runtime_for`, worktree predicate | High |
| `ui/handlers/project_handler.py` | Membership, `/clear`, `/compact` | Medium |
| `ui/views/left_panel.py` | Local-agent tag and menu | Low |
| `ui/handlers/chat_handler.py` | Tab label, stop default fan-out | High |
| `ui/views/review_bar.py` | Chip label callback | Low |
| `ui/handlers/review_handler.py` | Supply display names to the bar | Low |
| `agent/group_tools.py` | Peer match by id and name; accept `agent:` | Medium |
| `utils/project_memory.py` | Private path from session key | Medium |
| `utils/feedback_processor.py` | Role lookup for `agent:` | Low |
| `models/colors.py` callers | Cache key is the session key | Low |
| `docs/ARCHITECTURE.md` | §3.21r | Low |

---

## 5. Implementation order

1. `utils/roles.py` and its tests. Verify save, builtin refusal, duplicate refusal, empty prompt refusal, round-trip.
2. Session-key assignment and validation in `utils/agent_defs.py`. Verify a second role-`coder` agent gets `agent:<slug>`, an edit keeps `special:coder`, a rename keeps the id, and a taken slug is an error.
3. `is_local_agent_key` and the gate sweep in §2.7. Verify `/clear` and Edit/Delete work on `agent:alphacoder`.
4. `SpecialAgentDef.prompts` and prompt composition. Verify role `coder` still starts with the harness, a checked extra file is appended, and a user role's prompt text appears for that role only.
5. Dropdown and Create-role dialog. Verify the sentinel does not stay selected, and a created role can be saved on an agent.
6. Display names on `/ask` tabs and review chips.
7. Color key, private memory path, and peer lookup.
8. Worktree predicate. Verify `special:coder` gets `None`, and `agent:alphacoder` gets `agent-alphacoder`.
9. Project send no longer fans out. Verify `@` still does.
10. `ARCHITECTURE.md` §3.21r.

Each step is done when its tests pass before the next step starts.

---

## 6. Acceptance criteria

- [ ] Creating AlphaCoder with role Coder writes `session_key: agent:alphacoder` and `role: coder`.
- [ ] Creating a second agent named AlphaCoder is refused with an id error. No `special:coder-2` is written.
- [ ] Editing Coder keeps `special:coder`. Editing AlphaCoder's role or name keeps `agent:alphacoder`.
- [ ] The Role control is a dropdown. The last row is Create role…. Choosing it opens a name-and-prompt dialog and does not navigate to Settings.
- [ ] A saved role appears in the dropdown and can be selected on more than one agent.
- [ ] A role named Coder, Debugger, or Supervisor is refused by the dialog.
- [ ] AlphaCoder's system prompt contains the coder harness and the text of each checked prompt file, and does not contain those files only because the role string matched.
- [ ] An agent with a user role does not take the coder shell allowlist and cannot call a supervisor-only tool.
- [ ] An agent with role `coder` and id `agent:alphacoder` does take the coder shell allowlist.
- [ ] `conversations/agent:alphacoder.json` is AlphaCoder's transcript. `conversations/special:coder.json` gains no AlphaCoder messages from a send addressed to AlphaCoder.
- [ ] `/ask @AlphaCoder` opens a tab titled AlphaCoder.
- [ ] A review chip for that agent says AlphaCoder, not `agent:alphacoder`.
- [ ] The two coders have different colors.
- [ ] Private project memory for AlphaCoder is under `agents/agent-alphacoder/`, not `agents/coder/`.
- [ ] `agent_context` with agent `coder` does not open Coder when AlphaCoder also has that role. `agent_context` with agent `AlphaCoder` opens AlphaCoder.
- [ ] AlphaCoder's writes go to `.worktrees/agent-alphacoder`. Coder's writes stay in the project root.
- [ ] A project message with no `@` and no solo target reaches no agent. `@` still reaches every member.
- [ ] Left-panel row for AlphaCoder says DevelCakes and offers Edit and Delete.

---

## 7. Edge cases

| Case | Expected behavior |
|---|---|
| Name `Alpha Coder` | Id `agent:alpha-coder`. Worktree id `agent-alpha-coder`. |
| Name `!!!` | Validation error. No file written. |
| Name that slugs to an existing id | Validation error. The existing agent is untouched. |
| Existing `alphacoder.yaml` with `special:coder-2` | Left as-is. It still loads. It is a local agent. It gets a worktree because the key is not one of the three root keys. |
| Role file with an empty prompt | `load_roles` skips it. |
| PyYAML missing | `save_role` writes JSON, same as `save_agent_def`. |
| Checked prompt path missing at compose time | Skipped, warning logged, turn still runs. |
| `system/coder.md` also checked on a coder | Not appended a second time. |
| User role plus the coder harness | Not combined. The user role replaces the role slot only. |
| Rename AlphaCoder to BetaCoder | File becomes `betacoder.yaml`. `session_key` stays `agent:alphacoder`. |
| Delete the role file while an agent still names it | `_fill_form` keeps the stored role as an extra dropdown row. Compose appends no user-role text (`load_role` returns `None`) and does not switch the agent to another role. |
| Non-git project | `ensure_worktree` returns `None`. The extra writer stays on that folder. The turn does not raise. |
| `agent_context` needle `self` on `agent:alphacoder` | Resolves to that agent. The old `special:` prefix check must not reject it. |
| Solo target already set | Project send still goes only to that member. |
| `@` with no project members | Existing error string from `_resolve_mention`. |
| Two agents, same display name, different files | Already rejected at load by name dedupe. Id check is a second guard. |
| Color cache already holding `coder` from this process | New lookups use the session key. Old cache entries are unused after the caller change. |

---

## 8. ARCHITECTURE.md updates required

§3.21r `utils/agent_defs.py`:

- Replace the session-key paragraph that describes `special:<role>-N`.
- Document `agent:<slug>` as the id for new agents, preserved across edit and rename.
- Document built-in keys `special:supervisor`, `special:coder`, `special:debugger`.
- Add `utils/roles.py`: `load_roles`, `load_role`, `save_role`. A role is `name`, `slug`, and `prompt`. Files live in `<config>/roles/`. The agent editor dropdown is the only creator.
- Note that `compose_system_prompt` appends the agent's `prompts` list, and that a user role contributes its stored prompt text.
- Note that private project memory and the color cache are keyed by session key.

No other architecture section describes the role entry or the suffix worktree rule. §3.21s should mention `get_role_options` if that section lists `AgentBuilderHandler` methods.
