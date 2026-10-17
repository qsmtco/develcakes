# Bugfix B1 — file_search replay must still execute (audit BUG #2, HIGH)

**Loop:** CONTEXT-EFFICIENCY-FOLLOWUP, post-commit audit round
**Basis:** Debugger adversarial audit 2026-10-16, BUG #2
**Files in scope (exactly one):** `agent/tools.py`
**Commits under audit:** 898aaa54..351f2d75

## The bug

Item 3 removed `file_search` from `prompts/default_agents/supervisor.yaml`'s
`tools:` list. But `execute_tool`'s `allowed_tools` gate runs BEFORE the
handler dispatch and rejects any tool not in the list (tools.py:1528-1539 —
its own docstring calls the gate "the law"). So a replayed `file_search`
call — the exact case the spec hard rule protects ("Old transcripts that
already stored a `file_search` call must still execute") — is rejected:

```
Tool 'file_search' is not in the agent's allowed_tools (permitted: [...])
```

Verified by Supervisor probe: `execute_tool('file_search', ..., allowed_tools=[supervisor list])`
→ `success=False`.

Schema visibility and call permission are not the same control. The yaml edit
removed the permit; the schema filter (`_API_SCHEMA_HIDDEN`) already hid the
tool from the model. The two halves of item 3 contradict each other as shipped.

## Fix requirements

1. **Restore `file_search` to the `tools:` list in
   `prompts/default_agents/supervisor.yaml`** (it is the permit, not the
   schema — the model still sees one search tool because
   `get_tool_definitions_for_api` filters it out via `_API_SCHEMA_HIDDEN`).
   Coder/Debugger yamls are untouched (they never had it).
2. **Add ONE test** in `tests/test_tools.py`: `execute_tool('file_search',
   {'query': ...}, project_path=fixture, allowed_tools=[the supervisor list
   including file_search])` returns success=True and grouped-by-file output —
   the replay-compat contract.
3. Confirm the schema gate still hides it: extend (or add) an assertion that
   `get_tool_definitions_for_api(supervisor_tools)` contains `search_files`
   once and NOT `file_search` — the existing test
   `test_search_files_is_the_only_search_schema` already covers this; do not
   duplicate it, just verify it still passes after the yaml edit.
4. Touch nothing else. Do not remove the `_API_SCHEMA_HIDDEN` mechanism, do
   not touch the gate itself, do not touch `agent/context.py`.

## Rules

- `prompts/steelFramedCodeWriter.md` applies — read before write.
- py_compile only: `.venv/bin/python -m py_compile agent/tools.py` is NOT
  needed (no .py change if only the yaml+test change) — syntax-check any
  Python file you touch; yaml needs no compile.
- Do NOT run pytest — the Supervisor runs the suite.
- Deliver: COMPLETENESS checklist with the yaml diff and test evidence.
