# FIX: load_conversation() never called on startup — persisted conversations orphaned

**Status:** ✅ IMPLEMENTED — rt.load_conversation(session_key) at agent_runtime_handler.py:738, runtime.load_conversation() at runtime.py:2888

## Problem

When the app restarts, `send_to_special_agent` in `agent_runtime_handler.py` checks `rt.get_conversation(session_key)` — if it returns None (which it always does after restart since the in-memory `_conversations` dict is empty), it calls `rt.create_conversation()` which creates a fresh Conversation with `step_count=0`, empty messages, zero tokens/cost. The persisted JSON file (`~/.config/crabcakes/conversations/special:coder.json`) is never loaded.

This means:
- Token/cost data resets to zero on every app restart
- Conversation history is lost on restart
- Any step_count accumulation is wiped (which accidentally "fixes" the step limit issue but loses the conversation)

## Goal

Before calling `create_conversation()`, attempt `rt.load_conversation(session_key)`. If it returns True (persisted file found), use the loaded conversation. Only create fresh if load returns False.

## Files to Read First (ALL completely)

1. `ui/handlers/agent_runtime_handler.py` — focus on `send_to_special_agent` method (around line 523-620). Read the FULL method. Understand the flow: get agent_def, get runtime, check `get_conversation`, create if None.
2. `agent/runtime.py` — focus on:
   - `load_conversation` (line 2445) — loads from disk into `_conversations` dict, returns bool
   - `create_conversation` (line 1333) — creates fresh Conversation, stores in `_conversations` dict
   - `get_conversation` (line 1429) — returns Conversation from `_conversations` dict or None
   - `_load_conversation_from_disk` (line 1026) — module-level helper that reads JSON and constructs Conversation
3. `prompts/steelFramedCodeWriter.md` — the implementation prompt you MUST follow

## Implementation (Single Edit)

### Edit: Add load_conversation attempt before create_conversation

In `ui/handlers/agent_runtime_handler.py`, inside `send_to_special_agent`, find this block (around line 568):

```python
        # Create conversation if it doesn't exist yet, with project context and filtered tools
        if rt.get_conversation(session_key) is None:
            rt.create_conversation(
```

Change it to:

```python
        # Create conversation if it doesn't exist yet, with project context and filtered tools.
        # First try to load the persisted conversation from disk (preserves message history,
        # token/cost data, and other state across app restarts). Only create fresh if no
        # persisted conversation exists.
        if rt.get_conversation(session_key) is None:
            loaded = rt.load_conversation(session_key)
            if loaded:
                logger.info("send_to_special_agent: loaded persisted conversation for %s", session_key)

        if rt.get_conversation(session_key) is None:
            rt.create_conversation(
```

That's it. One edit, two files read. The `load_conversation` method already exists on `AgentRuntime` and handles all the disk I/O, JSON parsing, and Conversation reconstruction. We just need to call it.

## Why Not Modify runtime.py?

`load_conversation` already works correctly. The bug is that nobody calls it. This is a wiring fix, not a logic fix.

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`
- Read ALL files listed above completely before making any edit
- Make the edit, then run the verification commands below
- Run: `python3 -c "import ui.handlers.agent_runtime_handler; print('import OK')"` and paste output
- Run: `python3 -m pytest tests/test_command_handler.py tests/test_project_handler.py -q --tb=short -x` and paste output
- Run: `grep -n "load_conversation" ui/handlers/agent_runtime_handler.py` and paste output
- Report files changed with line numbers
- Include COMPLETENESS checklist at the end

## COMPLETENESS

At the end of your response, include:

```
COMPLETENESS:
- [x/not done] Edit 1: load_conversation attempt before create_conversation in agent_runtime_handler.py — evidence (line N, grep output)
- [x/not done] Import check passes — paste output
- [x/not done] Tests pass — paste output
```
