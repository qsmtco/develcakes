# Bugfix C3 — Agent Builder must preserve hidden-tool permits across save (audit BUG #3, MEDIUM)

**Loop:** CONTEXT-EFFICIENCY-FOLLOWUP, B-round re-audit
**Basis:** Debugger re-audit 2026-10-17, BUG #3 (supervisor-verified by code read)
**Files in scope (exactly two):** `ui/views/agent_builder.py`, one test.

## The bug (verified by code read)

The Builder is an editor, not a view. `_get_selected_tools()`
(agent_builder.py:622-623) returns only checkbox names, and `_tool_checks`
is populated from `get_available_tools()` — which B4 filtered to hide
`file_search`. The load path (:713-715) ticks checkboxes from
`agent_def["tools"]`, but `file_search` has no checkbox, so it is never
ticked — and any save silently drops the B1 replay permit from the yaml.

Attack vector: user opens Supervisor in the Builder, changes a prompt or
provider (anything unrelated), saves → `file_search` permit destroyed →
next replayed `file_search` call fails the allowed_tools gate. B1 silently
regresses on an unrelated edit.

## Fix (Debugger's option (a) — minimal, safest)

1. Carry the loaded agent's hidden tools through the session: when
   loading an agent def into the Builder (the same place `:713-715` reads
   `agent_def["tools"]`), stash the subset that is NOT in the visible
   checkbox set — e.g. `self._hidden_tools_preserved: set[str]`.
2. `_get_selected_tools()` returns `visible_ticked ∪
   self._hidden_tools_preserved`.
3. Reset `self._hidden_tools_preserved` when a different agent is loaded
   (or no agent) so it never leaks across sessions.
4. ONE test (wherever builder logic is tested — if UI tests are
   GTK-gated, test `_get_selected_tools` logic via a lightweight harness
   or extract the merge into a pure helper and test that): loading a def
   with `[read_file, file_search]` then calling `_get_selected_tools()`
   with `read_file` ticked returns both, `file_search` included.
5. Touch nothing else. Do NOT un-hide `file_search` in the Builder.

## Rules

- `prompts/steelFramedCodeWriter.md` applies — read before write.
- py_compile: `.venv/bin/python -m py_compile ui/views/agent_builder.py`
- No pytest — supervisor runs the suite.
- Deliver COMPLETENESS checklist.
