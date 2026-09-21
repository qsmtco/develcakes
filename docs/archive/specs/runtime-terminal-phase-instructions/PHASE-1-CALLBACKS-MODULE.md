# Phase 1 — Create `agent/callbacks.py` (typed Protocol module)

**Spec:** `docs/specs/SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION.md` §2.1
**Scope:** 1 NEW file, 0 edits to existing files. Standalone — no dependencies on other phases.

## What to build

Create `agent/callbacks.py` (~140 lines) defining 9 typed `Protocol` classes + 1 type alias for the AgentRuntime → UI handler callback contract.

### File location
`/path/to/projects/crabcakes/agent/callbacks.py`

### Verify the path is clear before writing
```bash
ls agent/callbacks.py 2>/dev/null && echo "EXISTS — DO NOT OVERWRITE" || echo "CLEAR"
```
Expected: `CLEAR` (file does not exist yet).

### Exact content specification

Write a Python module with:

1. **Module docstring** explaining: this module formalizes the callback contract between `agent/runtime.py` and `ui/handlers/agent_runtime_handler.py`. Both sides reference the protocols; neither imports from the other (same pattern as `agent/llm/protocol.py`).

2. **Imports:** `from __future__ import annotations` and `from typing import Any, Callable, Protocol`. That's it — no other imports. Do NOT import from `agent.runtime`, `ui/`, `gateway/`, or `models/`.

3. **9 Protocol classes** (exact names + signatures). Each must be a `class X(Protocol)` with a single `def __call__(self, ...) -> None: ...` method. The keyword argument `_turn_token: object | None = None` MUST appear on every protocol (leading underscore — this matches production dispatch in `agent/runtime.py`'s `_dispatch` helper, which passes `_turn_token=...`).

   The 9 protocols:

   | Class name | `__call__` positional args (before `*`) |
   |---|---|
   | `OnTextDelta` | `(self, session_key: str, text: str, *, _turn_token: object \| None = None)` |
   | `OnToolCallStart` | `(self, session_key: str, name: str, args: dict[str, Any], *, _turn_token: object \| None = None)` |
   | `OnToolCallResult` | `(self, session_key: str, name: str, result: Any, success: bool = True, *, _turn_token: object \| None = None)` |
   | `OnToolCallApprovalNeeded` | `(self, session_key: str, tool_name: str, args: dict[str, Any], *, _turn_token: object \| None = None)` |
   | `OnResponseComplete` | `(self, session_key: str, text: str, *, _turn_token: object \| None = None)` |
   | `OnTokenUsage` | `(self, session_key: str, total_tokens: int, cost: float, *, _turn_token: object \| None = None)` |
   | `OnTokenBreakdown` | `(self, session_key: str, breakdown: dict, *, _turn_token: object \| None = None)` |
   | `OnError` | `(self, session_key: str, message: str \| BaseException, *, _turn_token: object \| None = None)` |
   | `OnEnforcementStatus` | `(self, session_key: str, tool_name: str, status: dict, *, _turn_token: object \| None = None)` |

   Each protocol class needs a docstring (2-4 lines) describing WHEN it fires and what the args mean. The spec §2.1 has full docstrings for each — use those.

4. **Type alias** at the end:
   ```python
   AgentRuntimeCallbacks = dict[str, Callable | None]
   ```

### CRITICAL rules
- The keyword is `_turn_token` (WITH leading underscore), NOT `turn_token`. This is verified by production code: `grep -n "_turn_token=" agent/runtime.py` shows the dispatch helper passes `_turn_token=...`.
- Do NOT use `@runtime_checkable` decorator on any protocol (we don't need isinstance checks).
- Do NOT import `Enum` or `dataclass` — only `Protocol`, `Any`, `Callable`.
- File must end with a trailing newline.

## Verification commands (run all, paste output)

```bash
# 1. File compiles
python3 -c "import agent.callbacks; print('import OK')"

# 2. All 9 protocols + alias export
python3 -c "from agent.callbacks import OnTextDelta, OnToolCallStart, OnToolCallResult, OnToolCallApprovalNeeded, OnResponseComplete, OnTokenUsage, OnTokenBreakdown, OnError, OnEnforcementStatus, AgentRuntimeCallbacks; print('exports OK')"

# 3. All protocols have __call__ (callable protocols)
python3 -c "from agent.callbacks import *; ps=[OnTextDelta,OnToolCallStart,OnToolCallResult,OnToolCallApprovalNeeded,OnResponseComplete,OnTokenUsage,OnTokenBreakdown,OnError,OnEnforcementStatus]; assert all(hasattr(p,'__call__') for p in ps); print('callable OK')"

# 4. NO forbidden imports (layer rule)
grep -nE "from (ui|gateway|models)\.|import (ui|gateway|models)\." agent/callbacks.py
# Expected: 0 matches (empty output)

# 5. Uses _turn_token (leading underscore) — NOT turn_token
grep -nE "def __call__\(.*turn_token" agent/callbacks.py | grep -v _turn_token
# Expected: 0 matches (empty output)

# 6. No @runtime_checkable
grep -c "@runtime_checkable" agent/callbacks.py
# Expected: 0

# 7. Trailing newline
tail -c 1 agent/callbacks.py | xxd | tail -1
# Expected: last byte is 0a

# 8. Line count
wc -l agent/callbacks.py
# Expected: ~130-160 lines
```

## COMPLETENESS checklist (mandatory — include in your report)

```
COMPLETENESS:
- [x/not done] File agent/callbacks.py created — evidence: wc -l output
- [x/not done] 9 Protocol classes defined — evidence: grep -c "class On.*Protocol" output = 9
- [x/not done] AgentRuntimeCallbacks alias defined — evidence: import output
- [x/not done] All __call__ signatures use _turn_token (leading underscore) — evidence: grep step 5
- [x/not done] No @runtime_checkable — evidence: grep step 6 output = 0
- [x/not done] No forbidden imports (ui/gateway/models) — evidence: grep step 4 empty
- [x/not done] File ends with newline — evidence: xxd step 7
- [x/not done] import OK — evidence: step 1 output
```

Report: files changed, all verification command outputs, any issues or deviations from this spec (with one-sentence rationale each).
