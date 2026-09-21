# PHASE 7 — Optimize `list_conversations` (W13/W14)

## Objective
Replace expensive full-Conversation deserialization in `list_conversations`
with a lightweight JSON load that extracts only `agent_name`.

## Files to Read First
- `/path/to/projects/crabcakes/agent/runtime.py` (lines 2257–2291)
- `/path/to/projects/crabcakes/docs/specs/SPEC-RUNTIME-HARDENING-AUDIT.md` (W13, W14)

## Step 1 — Read Current `list_conversations`

**Read lines 2257–2291** of `runtime.py`.

## Step 2 — Replace the Inner Loop

The current code (around lines 2265–2273) calls `_load_conversation_from_disk(cid)`
which fully deserializes every saved conversation (with all message objects,
tool calls, etc.) just to read `agent_name`.

Replace the inner loop:

OLD (expensive):
```python
for cid in conversation_ids:
    try:
        conv = _load_conversation_from_disk(cid)
        result.append({
            "id": cid,
            "agent_name": conv.agent_name,
            ...
        })
    except Exception:
        ...
```

NEW (lightweight):
```python
import json as _json  # local alias to avoid shadowing outer `json` import if any

for cid in conversation_ids:
    try:
        path = os.path.join(conv_dir, f"{cid}.json")
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        result.append({
            "id": cid,
            "agent_name": data.get("agent_name", "Unknown"),
            ...
        })
    except Exception:
        ...
```

**Keep the `result2` rename from Phase 4** (W8 — rename inner `result2` to `conv_infos`).

Also add `import os` at the top of `list_conversations` or ensure it is already
imported at module level.

## Step 3 — Verify

```bash
cd /path/to/projects/crabcakes
python3 -c "from agent.runtime import AgentRuntime; print('import ok')"
python3 -m py_compile agent/runtime.py && echo "syntax ok"
```

## What NOT to Change
- Do NOT change `_load_conversation_from_disk` — it is still used by `get_conversation`
- Do NOT change any other function
