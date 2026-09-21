# PHASE 2 of 2 — Token Tracking: /cost command reads real data

**Spec:** `docs/specs/SPEC-token-tracking-fix.md`
**Depends on:** Phase 1 (needs `get_session_usage()` from agent_runtime_handler)

## Files to Change

1. `ui/handlers/project_handler.py` — rewrite `cmd_cost` to read conversation files
2. `tests/test_project_handler.py` (or `tests/test_command_handler.py`) — add tests for cmd_cost

## Instructions

### Read First (ALL of these, completely)

- `ui/handlers/project_handler.py` — the full file, focus on `cmd_cost` at approximately line 520
- `docs/specs/SPEC-token-tracking-fix.md` — the spec
- `models/conversation.py` — see `total_tokens` and `total_cost` fields, and `_conversations_dir` usage
- `agent/runtime.py` — see `_conversations_dir()` function at approximately line 904
- `utils/config.py` — see `get_config_dir()` which returns `~/.config/crabcakes`

### Edit 1: Rewrite `cmd_cost` to read conversation files

Replace the current stub `cmd_cost` method with a version that:

1. Gets the project members (as it already does)
2. For each member that is a special agent (session_key starts with `special:`):
   - Reads the conversation file at `~/.config/crabcakes/conversations/{session_key}.json`
   - Extracts `total_tokens` and `total_cost` from the JSON
   - If the file doesn't exist or can't be parsed, uses (0, 0.0)
3. For each member that is NOT a special agent (gateway agents):
   - Falls back to the in-memory `_session_usage` dict from agent_runtime_handler (if available)
   - If not available, uses (0, 0.0)
4. Formats the output as a table

Implementation approach:

```python
def cmd_cost(self, cmd: Command, session_key: str | None = None) -> CommandResult:
    """/cost — spending summary for current project"""
    sk = cmd.source_session_key
    if not sk.startswith("project:"):
        return CommandResult(handled=True, response_text="Open a project tab to check cost.")
    project_name = sk.split(":", 1)[1]
    members = self.get_project_members(project_name)
    if not members:
        return CommandResult(handled=True, response_text="No members in this project.")

    # Get in-memory usage cache if available (set by window.py wiring)
    mem_usage: dict[str, tuple[int, float]] = {}
    if self._runtime_usage_fn:
        mem_usage = self._runtime_usage_fn()

    lines = [
        f"Spending summary for {project_name}:",
        "",
        "Agent      Tokens   Cost",
        "────────────────────────",
    ]
    for m in members:
        name = (self._agent_mgr.get_name(m) if self._agent_mgr else "") or self._extract_display_name(m)
        tokens, cost = self._read_agent_usage(m, mem_usage)
        lines.append(f"  @{name}  {tokens:,} tokens  ${cost:.4f}")
    lines.extend([
        "────────────────────────",
    ])
    return CommandResult(handled=True, response_text="\n".join(lines))
```

### Edit 2: Add `_read_agent_usage` helper method

```python
def _read_agent_usage(self, session_key: str, mem_usage: dict[str, tuple[int, float]]) -> tuple[int, float]:
    """Read token usage for an agent from conversation file or in-memory cache.

    For special agents (special:*), reads the persisted conversation file.
    For gateway agents, falls back to the in-memory usage dict.
    Returns (total_tokens, total_cost).
    """
    import json, os
    from utils.config import get_config_dir

    # Try conversation file first (authoritative for special agents)
    conv_path = os.path.join(get_config_dir(), "conversations", f"{session_key}.json")
    try:
        with open(conv_path) as f:
            data = json.load(f)
        return (data.get("total_tokens", 0), data.get("total_cost", 0.0))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    # Fall back to in-memory cache
    if session_key in mem_usage:
        return mem_usage[session_key]

    return (0, 0.0)
```

### Edit 3: Add `_runtime_usage_fn` setter and instance variable

In `__init__` (or wherever the handler initializes its instance variables), add:

```python
self._runtime_usage_fn: Callable[[], dict[str, tuple[int, float]]] | None = None
```

Add a setter:

```python
def set_runtime_usage_fn(self, fn) -> None:
    """Set callback to read in-memory session usage from agent_runtime_handler."""
    self._runtime_usage_fn = fn
```

This will be wired in `window.py` as: `project_handler.set_runtime_usage_fn(runtime_handler.get_session_usage)`

NOTE: The wiring in window.py is NOT part of this phase. It will be done in a follow-up. For now, `_runtime_usage_fn` defaults to None and the fallback path handles it gracefully.

### Edit 4: Add tests

Add at least 3 tests to the nearest appropriate test file:

**Test 1 — cmd_cost reads conversation file:**
- Create a mock conversation file with `total_tokens=5000, total_cost=0.15`
- Call `cmd_cost` with a project that has a special agent
- Assert output contains `5,000 tokens` and `$0.1500`

**Test 2 — cmd_cost with missing conversation file:**
- No conversation file exists for the agent
- `_runtime_usage_fn` is None
- Assert output contains `0 tokens` and `$0.0000`

**Test 3 — _read_agent_usage graceful fallback:**
- Corrupted JSON file → returns (0, 0.0) without raising

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`
- Run: `python3 -m pytest tests/test_project_handler.py -q --tb=short -x` and paste the output
- Run: `grep -n "contact gateway" ui/handlers/project_handler.py` and paste output (should show 0 matches — the stub text is gone)
- Run: `grep -n "_read_agent_usage\|_runtime_usage_fn\|set_runtime_usage_fn" ui/handlers/project_handler.py` and paste output
- Report: files changed with line numbers, test results, COMPLETENESS checklist
- At the end, include:
  COMPLETENESS:
  - [x/not done] Edit 1: cmd_cost reads real data — evidence
  - [x/not done] Edit 2: _read_agent_usage helper — evidence
  - [x/not done] Edit 3: _runtime_usage_fn setter + init — evidence
  - [x/not done] Edit 4: Tests added (3 tests) — evidence
  - [x/not done] Old "contact gateway" stub removed — grep evidence
  - [x/not done] All tests pass — paste output
