# Phase 5 Instructions — AuditLog Extraction

**Spec:** `docs/specs/SPEC-RUNTIME-EXTRACTION-PHASE-5.md`
**Files:** `agent/audit.py` (NEW) + `agent/runtime.py` + `tests/test_agent_audit.py` (NEW)

**FIRST ACTION: Load the steelFramedCodeWriter prompt fresh.** Read `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md` IN FULL. Activate it. Begin with Discovery Phase block.

Read `agent/runtime.py` lines 86-164 (the AuditEntry + AuditLog classes) in full before editing.

---

## Edit 1 — Create `agent/audit.py` (NEW FILE)

Create this file with the verbatim content of AuditEntry + AuditLog from runtime.py lines 86-164. The spec §3.1 has the full code. Add a module docstring and imports (hashlib, json, os, threading, time).

**IMPORTANT:** This is a VERBATIM MOVE. Copy the class bodies exactly. Do not change any logic, any method signature, any docstring. The only change is the module location.

## Edit 2 — `agent/runtime.py`: add import

Near the other `from agent.` imports at the top, add:
```python
from agent.audit import AuditEntry, AuditLog
```

## Edit 3 — `agent/runtime.py`: remove inline classes

Delete the `AuditEntry` class and `AuditLog` class (lines ~86-164). These are now imported from `agent.audit`.

**Do NOT touch:**
- `self._audit_log = AuditLog()` (line ~745) — still works, uses the imported class
- The 3 `self._audit_log.record(...)` call sites (lines ~1543, 1563, 1633) — unchanged
- `audit_log=self._audit_log` at line ~1606 (passed to ToolContext) — unchanged

## Edit 4 — Create `tests/test_agent_audit.py` (NEW FILE)

Create the 7 tests from spec §3.3. The tests exercise AuditLog without instantiating AgentRuntime:
- test_record_creates_entry_with_hashed_args
- test_record_hashes_result
- test_record_empty_result_has_empty_hash
- test_flush_writes_jsonl_and_clears
- test_flush_empty_returns_none
- test_entries_returns_copy
- test_concurrent_record_is_thread_safe

---

## Verification

1. `grep -c "class AuditEntry\|class AuditLog" agent/runtime.py` → **0**
2. `grep -c "from agent.audit import" agent/runtime.py` → **1**
3. `python3 -c "from agent.audit import AuditEntry, AuditLog; print('OK')"` → OK
4. `python3 -c "from agent.runtime import AgentRuntime; print('OK')"` → OK
5. `python3 -m pytest tests/test_agent_audit.py -v` → 7/7 pass

## COMPLETENESS checklist (mandatory)
```
COMPLETENESS:
- [x/not done] Edit 1: Created agent/audit.py — evidence: <python import>
- [x/not done] Edit 2: Added import to runtime.py — evidence: <grep>
- [x/not done] Edit 3: Removed inline classes from runtime.py — evidence: <grep -c = 0>
- [x/not done] Edit 4: Created test_agent_audit.py — evidence: <pytest 7/7>
- [x/not done] Runtime imports OK — evidence: <python output>
```
