# FIX: Step count never resets between tasks

**Status:** ✅ IMPLEMENTED — /clear command with cmd_clear, clear_conversation, _clear_callback; wiring in window.py

## Problem

`step_count` in `models/conversation.py` increments every assistant turn (line 190: `self.step_count += 1`) and is **never reset**. Over multiple tasks, it accumulates until it hits `step_limit=100` (from `agent/config.py:268`), at which point the agent is killed mid-work with:

```
[Error] Step limit exceeded: 103 > 100
```

There is no `/clear`, `/reset`, or `/new` command. The only way to recover is to manually edit the JSON file.

## Goal

Add a `/clear` slash command that resets the conversation for a special agent — clears messages, resets step_count to 0, resets total_tokens and total_cost to 0. This gives the user a way to start fresh when a new task begins.

## Files to Read First (ALL of them, completely)

1. `ui/handlers/agent_runtime_handler.py` — focus on:
   - `send_to_special_agent` (line ~430) — how conversations are created
   - `_on_text_delta` and other callbacks — understand the handler structure
   - The `__init__` method — see what state the handler holds
2. `ui/handlers/command_handler.py` — focus on:
   - How commands are registered (line ~83-140)
   - The `Command` type and `CommandResult` type
   - The `register_command` method
3. `ui/handlers/project_handler.py` — focus on `cmd_status` (line ~478) as an example of a command handler that reads session state
4. `agent/runtime.py` — focus on:
   - `create_conversation` (line ~1340) — how conversations are made
   - `load_conversation` (line ~2445) — how conversations are restored from disk
   - `_conversations` dict — the in-memory conversation store
   - `send_message` (line ~1433) — how messages are sent
5. `models/conversation.py` — the Conversation dataclass: `step_count`, `total_tokens`, `total_cost`, `messages`
6. `prompts/steelFramedCodeWriter.md` — the implementation prompt you must follow

## Implementation

### Edit 1: Add `/clear` command registration in `command_handler.py`

In `command_handler.py`, inside `__init__` where commands are registered (around line 83-140), add registration for a `clear` command. Place it near the other project-scoped commands (after `cost`):

```python
if hasattr(project_handler, "cmd_clear"):
    self.register_command("clear", project_handler.cmd_clear,
        help_text="Clear agent conversation history and reset step count")
```

### Edit 2: Add `cmd_clear` method in `project_handler.py`

Add a new method to `ProjectHandler`:

```python
def cmd_clear(self, cmd: Command, session_key: str | None = None) -> CommandResult:
    """/clear — reset the current agent's conversation (clears messages, step_count, tokens, cost)"""
    sk = cmd.source_session_key
    if not sk:
        return CommandResult(handled=True, response_text="No active session to clear.")

    # For special agent tabs
    if sk.startswith("special:"):
        agent_name = sk.split(":", 1)[1]
        # Tell the runtime handler to reset this conversation
        if hasattr(self, '_clear_callback') and self._clear_callback:
            result = self._clear_callback(sk)
            if result:
                return CommandResult(handled=True, response_text=f"Cleared {agent_name}'s conversation. Step count reset to 0.")
        return CommandResult(handled=True, response_text=f"Could not clear {agent_name}'s conversation.")

    # For project tabs — clear the conversation for the solo DM target or all members
    if sk.startswith("project:"):
        return CommandResult(handled=True, response_text="Use /clear in an agent tab to reset that agent's conversation.")

    return CommandResult(handled=True, response_text="Nothing to clear.")
```

### Edit 3: Add `_clear_callback` setter in `project_handler.py`

In `ProjectHandler.__init__` (or wherever instance vars are set), add:

```python
self._clear_callback = None
```

Add a setter method:

```python
def set_clear_callback(self, fn) -> None:
    """Set callback to clear a special agent's conversation.
    Called by window.py to wire AgentRuntimeHandler.clear_conversation."""
    self._clear_callback = fn
```

### Edit 4: Add `clear_conversation` method in `agent_runtime_handler.py`

Add a method to `AgentRuntimeHandler`:

```python
def clear_conversation(self, session_key: str) -> bool:
    """Reset a special agent's conversation: clear messages, step_count, tokens, cost.

    Removes the conversation from the in-memory dict and deletes the persisted
    JSON file so the next message creates a fresh conversation.
    Returns True on success.
    """
    rt = self._get_runtime_for_session(session_key)
    if rt is None:
        return False

    # Remove from memory
    conv = rt.get_conversation(session_key)
    if conv is not None:
        # Reset in-place (safer than removing — avoids race with in-flight messages)
        conv.messages = []
        conv.step_count = 0
        conv.total_tokens = 0
        conv.total_cost = 0.0
        conv._token_estimate_cache = None
        logger.info("[handler] Cleared conversation for %s (in-memory reset)", session_key)

    # Also delete the persisted file so restart doesn't restore old state
    import os
    from utils.config import get_config_dir
    conv_path = os.path.join(get_config_dir(), "conversations", f"{session_key}.json")
    try:
        os.remove(conv_path)
        logger.info("[handler] Deleted persisted conversation file %s", conv_path)
    except FileNotFoundError:
        pass  # Already gone — fine
    except OSError as e:
        logger.warning("[handler] Could not delete %s: %s", conv_path, e)

    return True
```

NOTE: You need to check if `_get_runtime_for_session` exists. If it doesn't, look at how `_get_runtime` works and adapt. The runtime handler may have a single runtime or multiple — check the code.

### Edit 5: Wire the callback in `window.py`

In `window.py`, wherever handlers are wired together (look for where `project_handler` is connected to other handlers, near `set_agent_manager`, `set_gateway_client`, etc.), add:

```python
project_handler.set_clear_callback(runtime_handler.clear_conversation)
```

If you can't find the wiring spot, search for `set_runtime_usage_fn` or `set_agent_manager` or `set_special_agents` in `window.py`.

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`
- Make ALL edits before running tests
- Run: `python3 -m pytest tests/ -q --tb=short -x -k "clear or command"` and paste full output
- Run: `grep -n "cmd_clear\|clear_conversation\|_clear_callback" ui/handlers/project_handler.py ui/handlers/agent_runtime_handler.py ui/handlers/command_handler.py` and paste output
- Do NOT modify `agent/runtime.py` or `models/conversation.py` — only handler and UI layer
- Report files changed with line numbers
- Include COMPLETENESS checklist at the end

## COMPLETENESS

At the end of your response, include:

```
COMPLETENESS:
- [x/not done] Edit 1: /clear registered in command_handler.py — evidence
- [x/not done] Edit 2: cmd_clear method in project_handler.py — evidence
- [x/not done] Edit 3: _clear_callback setter in project_handler.py — evidence
- [x/not done] Edit 4: clear_conversation method in agent_runtime_handler.py — evidence
- [x/not done] Edit 5: callback wired in window.py — evidence
- [x/not done] Tests pass — paste output
```
