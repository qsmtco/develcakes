# PHASE 4 — Dead Code Cleanup (W5–W10)

## Objective
Remove dead code and fix structural issues identified in the audit. Six
target items: duplicate urllib.request imports, empty finally blocks, a
shadowed variable name, and wrong return-type annotations.

## Files to Read First
- `/path/to/projects/crabcakes/agent/runtime.py`
- `/path/to/projects/crabcakes/docs/specs/SPEC-RUNTIME-HARDENING-AUDIT.md` (W5–W10)

## ⚠️ Important: Do These in Order

Do Steps 1–5 in sequence. Read the file sections referenced before each edit.

---

## Step 1 — Remove `urllib.request` Duplicate Imports (W5)

**Read lines 1–50** of `runtime.py` to see all imports.

Remove function-local `import urllib.request` from these three locations
(keep the module-level import at ~line 404):

| Location | Approximate Line |
|----------|-----------------|
| `_call_openai` | ~203 |
| `_call_minimax` | ~248 |
| `_call_anthropic` | ~298 |

Each removal looks like:
```python
import urllib.request   # ← REMOVE this line from each function
```

Keep the module-level `import urllib.request` at ~line 404.

---

## Step 2 — Remove `finally: pass` Block in `execute_tool` (W6)

**Read lines 1780–1800** of `runtime.py`.

Remove the empty finally block:
```python
    finally:
        pass   # ← REMOVE lines 1789–1792
```

---

## Step 3 — Remove Duplicate `execute_tool` Import (W7)

**Read lines 1715–1720** of `runtime.py`.

The import at line 1715 is the actual used import.
The import at line 1772 (inside `execute_tool` function body) is dead.

Remove:
```python
from agent.tools import execute_tool   # ← at line ~1772, inside execute_tool body
```

---

## Step 4 — Fix `result2` Shadow Variable (W8)

**Read lines 2260–2280** of `runtime.py`.

The inner list comprehension shadows the outer `result` variable name.
Rename the inner variable from `result2` to `conv_info` or `item`:

OLD:
```python
result2 = [
    {"id": cid, "agent_name": _load_conversation_from_disk(cid).agent_name}
    for cid in conversation_ids
]
```

NEW:
```python
conv_infos = [
    {"id": cid, "agent_name": _load_conversation_from_disk(cid).agent_name}
    for cid in conversation_ids
]
```

Then update the reference to `result2` later in the function to `conv_infos`.

---

## Step 5 — Remove Redundant `stream_options` from OpenAI and MiniMax (W10 part)

**Read lines 473–545** (`_stream_openai_events`) and **lines 547–665** (`_stream_minimax_events`).

From `_stream_openai_events` payload (around line 488):
```python
"stream_options": {"include_usage": True},   # ← REMOVE
```

From `_stream_minimax_events` payload (around line 562):
```python
"stream_options": {"include_usage": True},   # ← REMOVE
```

---

## Step 6 — Verify

```bash
cd /path/to/projects/crabcakes
python3 -c "from agent.runtime import AgentRuntime; print('import ok')"
python3 -m py_compile agent/runtime.py && echo "syntax ok"
```

## What NOT to Change
- Do NOT change `_stream_anthropic_events` — fixed in Phase 2
- Do NOT change `_call_llm` or `_call_llm_streaming`
- Do NOT add tests
