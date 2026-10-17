# Bugfix C1 — prompt TOOL_LIST must not advertise schema-hidden tools (audit BUG #5, MEDIUM)

**Loop:** CONTEXT-EFFICIENCY-FOLLOWUP, B-round re-audit
**Basis:** Debugger re-audit 2026-10-17, BUG #5 (supervisor-verified)
**Files in scope (exactly two):** `agent/runtime.py` (`_prompt_tool_names`),
`tests/test_agent_runtime.py` (one test).

## The bug (verified)

`_prompt_tool_names` builds the system prompt's TOOL_LIST from
`get_all_tools()` — unfiltered. The shipped Supervisor carries the B1
`file_search` execute permit, so every Supervisor turn advertises
`file_search` in the prompt while the API schema omits it. The model is
told it has a tool it can never call — the mirror image of the original
item-3 confusion.

Probe: `file_search in _prompt_tool_names(conv)` = True;
`file_search in API schema` = False → INCOHERENT.

## Fix

1. In `_prompt_tool_names` (agent/runtime.py:1167-1175): filter
   `_API_SCHEMA_HIDDEN` out of `get_all_tools()` before the allowed-tools
   intersection. The prompt must show exactly what the API schema offers.
2. Do NOT touch `conv.allowed_tools` or `execute_tool`'s gate — those keep
   the B1 replay permit working (they are separate from TOOL_LIST).
3. ONE test in `tests/test_agent_runtime.py`: a conversation whose
   allowed_tools includes `file_search` — assert `_prompt_tool_names` omits
   it AND `get_tool_definitions_for_api(allowed)` omits it (the two halves
   stay in lockstep), while `execute_tool('file_search', ...)` still
   succeeds with that allowed list (replay preserved).
4. Touch nothing else.

## Rules

- `prompts/steelFramedCodeWriter.md` applies. py_compile the touched files.
- No pytest. Deliver COMPLETENESS checklist.
