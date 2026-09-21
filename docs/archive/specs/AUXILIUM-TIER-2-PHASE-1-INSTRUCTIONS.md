# Phase T2-1 — Add `agent_role` to `Conversation` dataclass

**Spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-auxilium-tier-2.md` §2.1, §2.3
**Target:** main
**Risk:** Low
**Lines:** +2 (one field, one propagation line)

## Goal

Make `Conversation` carry an `agent_role` field so `agent/runtime.py:_run_loop()` can gate KB synthesis on `agent_role == "helper"`. Today the field is accepted by `create_conversation()` at `agent/runtime.py:959` but never reaches the `Conversation` object — it's dropped at the constructor call.

## Files to change

1. `models/conversation.py` — add the field
2. `agent/runtime.py` — propagate the parameter into the `Conversation(...)` constructor call

## Edit 1: `models/conversation.py`

Add `agent_role: str = ""` to the `Conversation` dataclass. Place it **immediately after** `agent_name` (the first field). This keeps the role field adjacent to the agent identity and matches the spec's "add after `agent_name`" instruction.

Anchor on the line containing `agent_name: str` (currently line 92, after the docstring). The new line is:

```
    agent_role: str = ""          # "helper" for Auxilium, "" for other agents
```

Add a trailing space + comment per the spec's style. Match the existing 4-space field indentation.

## Edit 2: `agent/runtime.py`

In `create_conversation()` (starts at line 951), the `Conversation(...)` constructor call is at lines 1005-1018. Add `agent_role=agent_role,` so the parameter received at line 959 is actually stored on the dataclass.

Place the new line **after** `agent_name=agent_name,` (line 1005), to keep the field order in the constructor matching the order in the dataclass.

## Rules

- Use `prompts/steelFramedCodeWriter.md` as the active prompt.
- Use identifiers as anchors, not line numbers (line numbers may have drifted; the spec warns about this in §5).
- Do not reformat adjacent code.
- Do not "improve" comments.
- Do not reorder existing fields.

## Verification (run yourself, paste output in report)

1. Dataclass import + field:
   ```
   python3 -c "from models.conversation import Conversation; c = Conversation(agent_name='Auxilium', model='openai/gpt-4o', agent_role='helper'); print('agent_role=', c.agent_role); c2 = Conversation(agent_name='Test', model='openai/gpt-4o'); print('default=', repr(c2.agent_role))"
   ```
   Expected: `agent_role= helper` and `default= ''`.

2. Propagation check — `create_conversation()` now stores `agent_role`:
   ```
   python3 -c "
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime
   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   rt.start()
   rt.create_conversation(session_key='s1', agent_name='Auxilium', agent_role='helper')
   print('stored=', rt.get_conversation('s1').agent_role)
   rt.create_conversation(session_key='s2', agent_name='Test')
   print('default=', repr(rt.get_conversation('s2').agent_role))
   "
   ```
   Expected: `stored= helper` and `default= ''`.

3. Grep — no other call sites broken:
   ```
   grep -rn "Conversation(" agent/ ui/ 2>&1 | grep -v __pycache__
   ```
   All existing call sites should still work (the new field has a default).

4. Test suite:
   ```
   python3 -m pytest tests/ -q --tb=short 2>&1 | tail -20
   ```
   Must show no NEW failures. Pre-existing failures (if any) are not your problem — flag them in the report.

## Deliverable

- Both edits applied
- All four verification commands run by you, output pasted in the report
- A `**COMPLETENESS:**` block listing each edit with evidence (line number, grep output, or test result)
- A related-bug scan (Rule 6.6 in `steelFramedCodeWriter.md`): if you find other places where a parameter is accepted but dropped before storage, flag them — do NOT silently fix.

## Word marker

Include the word "please write" in your opening reply so the channel knows this delegation is canonical.

## COMPLETENESS template

End your reply with:

```
**COMPLETENESS:**
- [x] Edit 1: added agent_role field to Conversation dataclass — line N in models/conversation.py, evidence: <python -c output>
- [x] Edit 2: propagated agent_role through create_conversation() to Conversation(...) — line N in agent/runtime.py, evidence: <python -c output>
- [x] Verification 1: dataclass import + field check — <paste output>
- [x] Verification 2: propagation through create_conversation — <paste output>
- [x] Verification 3: grep for other Conversation() call sites — <paste output, count>
- [x] Verification 4: full test suite — <paste last 20 lines>
- [x] Related-bug scan: <list of any related issues found, or "none">
```

A reply missing the `**COMPLETENESS:**` block is incomplete and will be sent back.
