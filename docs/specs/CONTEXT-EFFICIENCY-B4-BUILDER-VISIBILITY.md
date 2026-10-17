# Bugfix B4 — file_search leaks into Agent Builder UI with a stale description (audit BUG #4, LOW)

**Loop:** CONTEXT-EFFICIENCY-FOLLOWUP, post-commit audit round
**Basis:** Debugger adversarial audit 2026-10-16, BUG #4
**Files in scope (exactly two):** `utils/agent_defs.py`, one test file
(`tests/test_agent_defs.py` — create if absent, else extend the nearest
agent-defs test file).

## The bug

`_API_SCHEMA_HIDDEN` filters only `get_tool_definitions_for_api`.
`utils/agent_defs.py::get_available_tools()` still calls `get_all_tools()`,
so:
1. The Agent Builder UI (`ui/handlers/agent_builder_handler.py` via
   `get_tool_options`) shows a `file_search` checkbox — a user can tick it
   and re-create exactly the state B1 just fixed (schema hidden vs permit
   confusion), or worse, an agent with `file_search` permitted but
   invisible to itself.
2. The yaml validator (agent_defs.py:405) still accepts it.
3. The `file_search` description still says "search_files is plain grep" —
   stale since the union made search_files name-or-content.

## Fix requirements

1. In `get_available_tools()` (utils/agent_defs.py), filter out
   `_API_SCHEMA_HIDDEN` tools — import from `agent.tools` (single source of
   truth; do not duplicate the set). If the import would create a cycle,
   move `_API_SCHEMA_HIDDEN` to a shared home and re-export; document.
2. Update the `file_search` description in `agent/tools.py` only if step 1
   would leave it user-visible anywhere — with step 1 in place it should
   not be; verify and state this in the report rather than editing blind.
3. ONE test: `get_available_tools()` contains `search_files` and does NOT
   contain `file_search`.
4. Touch nothing else. Do not remove the `file_search` ToolDefinition
   registration, do not touch `execute_tool`, do not touch
   `get_all_tools()` itself (other callers may rely on it).

## Rules

- `prompts/steelFramedCodeWriter.md` applies — read before write.
- py_compile: `.venv/bin/python -m py_compile utils/agent_defs.py`
- Do NOT run pytest — supervisor runs the suite.
- Deliver: COMPLETENESS checklist + grep evidence that
  `get_tool_options`/builder UI surfaces no `file_search`.
