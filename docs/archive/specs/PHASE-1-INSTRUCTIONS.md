# PHASE 1 — Extract Anthropic Conversion Helpers

## Objective
Extract two reusable helpers from `_call_anthropic` (lines 288–463) so both
streaming and non-streaming paths use identical message/tool format conversion.

## Files to Read First
- `/path/to/projects/crabcakes/agent/runtime.py` (lines 288–463, 1–50)
- `/path/to/projects/crabcakes/docs/specs/SPEC-RUNTIME-HARDENING-AUDIT.md` (W2, W3)

## Step 1 — Extract `_convert_messages_for_anthropic`

Add this function near `_convert_tools_for_anthropic` (around line 300):

```python
def _convert_messages_for_anthropic(
    messages: list[Message],
) -> list[dict]:
    """Convert Conversation Messages → Anthropic message format.

    Extracts text content from each Message role/content pair.
    Tool results and multi-block content are preserved as-is.
    """
    result: list[dict] = []
    for msg in messages:
        if msg.role == "system":
            result.append({"role": "user", "content": msg.content})
            continue
        # Collect content blocks
        content_blocks: list[dict] = []
        # text
        if msg.content:
            content_blocks.append({"type": "text", "text": msg.content})
        # tool results
        if msg.tool_results:
            for tr in msg.tool_results:
                content_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tr.tool_call_id or tr.id or "",
                    "content": tr.result or "",
                })
        result.append({"role": msg.role, "content": content_blocks})
    return result
```

**Constraint:** Must appear before `_call_anthropic` in the file.

## Step 2 — Extract `_convert_tools_for_anthropic`

Add this function right after `_convert_messages_for_anthropic`:

```python
def _convert_tools_for_anthropic(
    tools: list[ToolDefinition],
) -> list[dict]:
    """Convert ToolDefinition list → Anthropic tool schema.

    Handles both modern input_schema and legacy parameters format.
    Strips Crabcakes-internal keys (mode, return_direct).
    """
    anthropic_tools: list[dict] = []
    for tool in tools:
        t: dict[str, object] = {"name": tool.name}
        if tool.description:
            t["description"] = tool.description
        # Modern format: tool.use_openai_format -> input_schema
        if tool.input_schema and callable(tool.input_schema):
            t["input_schema"] = {
                "type": "object",
                "properties": tool.input_schema.get("properties", {}),
                "required": tool.input_schema.get("required", []),
            }
        elif tool.input_schema and isinstance(tool.input_schema, dict):
            t["input_schema"] = {
                "type": "object",
                "properties": tool.input_schema.get("properties", {}),
                "required": tool.input_schema.get("required", []),
            }
        # Legacy format: tool.parameters
        elif tool.parameters and isinstance(tool.parameters, dict):
            t["input_schema"] = {
                "type": "object",
                "properties": tool.parameters.get("properties", {}),
                "required": tool.parameters.get("required", []),
            }
        anthropic_tools.append(t)
    return anthropic_tools
```

## Step 3 — Update `_call_anthropic` to Use the Helpers

Replace the inline message conversion (current lines ~306–351) with:

```python
    # Convert messages
    anthropic_messages = _convert_messages_for_anthropic(messages)
    system_prompt = build_system_prompt(
        project_path, agent_name, agent_config, all_tools
    )
    if system_prompt:
        anthropic_messages.insert(0, {
            "role": "user",
            "content": [{"type": "text", "text": system_prompt}],
        })

    # Convert tools
    anthropic_tools = _convert_tools_for_anthropic(tools) if tools else None
```

Replace the `api_payload` block (current lines ~352–363) to use `anthropic_messages` and `anthropic_tools`.

## Verification
After editing, run:
```bash
cd /path/to/projects/crabcakes
python3 -c "from agent.runtime import AgentRuntime; print('import ok')"
```

If there are any import errors, fix them before reporting completion.

## What NOT to Change
- Do NOT change `_stream_anthropic_events` in this phase
- Do NOT change `_call_llm` or `_call_llm_streaming`
- Do NOT remove any functions yet
- Do NOT add tests in this phase (Phase 3 covers tests)
