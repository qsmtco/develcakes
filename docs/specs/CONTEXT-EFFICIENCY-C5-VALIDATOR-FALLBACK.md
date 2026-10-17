# Bugfix C5 — validator ImportError fallback must skip the check, not reject every tool (audit BUG #4, LOW)

**Loop:** CONTEXT-EFFICIENCY-FOLLOWUP, B-round re-audit
**Basis:** Debugger re-audit 2026-10-17, BUG #4
**Files in scope (exactly two):** `utils/agent_defs.py`
(`validate_agent_def`), one test in `tests/test_agent_defs.py`.

## The bug (audit-verified)

The `except ImportError` branch in `validate_agent_def` calls
`get_available_tools()`, which swallows ITS OWN ImportError and returns
`[]` — so the fallback validates against an empty set and every tool
becomes "Unknown tool". The log message says "validating visible tools
only" but the behavior is hard-fail of every agent def. Only reachable
with a broken install, but actively misleading when it is.

## Fix (audit's sketch)

1. In the ImportError branch: `known_names = None` and log
   `"agent.tools unavailable — skipping tool-name validation"`; guard the
   per-tool check with `if known_names is not None:`.
2. ONE test: monkeypatch `agent.tools` import failure (e.g. patch
   `builtins.__import__` or insert a None in sys.modules) →
   `validate_agent_def` on a def with `tools: [read_file]` returns `[]`
   errors (check skipped), and no "Unknown tool" appears.
3. Touch nothing else. The happy path is unchanged.

## Rules

- `prompts/steelFramedCodeWriter.md` applies. py_compile touched files.
- No pytest. Deliver COMPLETENESS checklist.
