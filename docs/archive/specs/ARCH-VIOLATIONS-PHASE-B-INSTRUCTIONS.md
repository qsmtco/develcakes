# Phase B of B — Fix STREAMING_ENABLED regression introduced by Phase A

**Supervisor:** Qaster
**Builder:** QTR
**Date:** 2026-06-18
**Trigger:** Adversarial audit (adversarialDebugger prompt) of the Phase A ARCH violations fix found a HIGH-severity regression in the STREAMING_ENABLED extraction. The fix is broken in the most insidious way: it works correctly at import time (all names point to the same bool object) but as soon as the toolbar toggles the value, `chat_handler.py`'s view of the constant goes stale.

**Severity:** HIGH — user-facing feature (streaming toggle button) is non-functional after the fix.

**Root cause:** Python `from X import Y` creates a NEW binding in the importing module. When the imported name is later reassigned in `X` (e.g., `X.Y = True`), the local binding in the importing module still points to the OLD value. `chat_handler.py` does `from ui.constants import STREAMING_ENABLED` and then reads `if not STREAMING_ENABLED` at runtime. The toolbar's mutation `ui_constants.STREAMING_ENABLED = button.get_active()` updates the `ui.constants` module attribute but does NOT update `chat_handler.py`'s local binding. So the toolbar button can be toggled, but the chat handler never sees the new value.

**Verified reproduction:**
```python
import ui.constants as c1
from ui.constants import STREAMING_ENABLED as ce
c1.STREAMING_ENABLED = True
# c1.STREAMING_ENABLED is True
# ce is still False  ← THE BUG
```

The original code (pre-Phase-A) avoided this by using `import X` form (not `from X import Y`) in `toolbar.py` and defining `STREAMING_ENABLED` directly in `chat_handler.py`. The Phase-A fix changed this to the broken `from X import Y` pattern.

---

## Files to change

1. `ui/handlers/chat_handler.py` — change import style
2. `ui/toolbar.py` — change import style for consistency

---

## Edit 1: `ui/handlers/chat_handler.py` — replace `from` with module reference

**File:** `ui/handlers/chat_handler.py`
**Current line 19:** `from ui.constants import STREAMING_ENABLED`

**Replacement:** Replace with:
```python
import ui.constants
```

**Then update all reads in the same file to use `ui.constants.STREAMING_ENABLED`:**

- Line 580: `if not STREAMING_ENABLED:` → `if not ui.constants.STREAMING_ENABLED:`
- Line 599 (in docstring): keep the docstring referring to the constant by name; only the runtime code matters
- Line 627 (in comment): keep the comment as-is

**Verification:**
```bash
cd /path/to/projects/crabcakes && grep -n "STREAMING_ENABLED" ui/handlers/chat_handler.py
```
Expect: 1 import line `import ui.constants`, plus 1 reference to `ui.constants.STREAMING_ENABLED` in code (line 580). No bare `STREAMING_ENABLED` reads in code.

```bash
cd /path/to/projects/crabcakes && grep -n "^from ui.constants" ui/handlers/chat_handler.py
```
Expect: 0 matches (the `from` form is gone).

---

## Edit 2: `ui/toolbar.py` — replace `from` with module reference

**File:** `ui/toolbar.py`
**Current line 9:** `from ui.constants import STREAMING_ENABLED`

**Replacement:** Replace with:
```python
import ui.constants
```

**Then update the read at line 32:**
- Current: `self._stream_btn.set_active(STREAMING_ENABLED)`
- New: `self._stream_btn.set_active(ui.constants.STREAMING_ENABLED)`

**Then update the 2 lazy imports in method bodies (lines 95, 101):**
- Current: `import ui.constants as ui_constants`
- New: REMOVE these lines entirely (the top-level `import ui.constants` is now in scope)
- Update `ui_constants.STREAMING_ENABLED` → `ui.constants.STREAMING_ENABLED` at lines 96 and 103

**Verification:**
```bash
cd /path/to/projects/crabcakes && grep -n "STREAMING_ENABLED" ui/toolbar.py
```
Expect: 1 import line `import ui.constants`, plus 3 references to `ui.constants.STREAMING_ENABLED` (1 read at construction, 1 write in toggle, 1 read in label-update). No `ui_constants` aliasing.

---

## Edit 3: `ui/constants.py` — no change

The constants file is fine. The bug is in the consumer's import style, not in the constants file.

---

## Rules

- Use the `steelFramedCodeWriter` prompt
- Read both files in full before editing
- Scope is exactly 2 files. Do NOT touch `ui/constants.py` or anything else.
- Do NOT refactor STREAMING_ENABLED into a class. That is a Tier 2+ evolution suggestion, out of scope for this fix.
- Do NOT add a comment explaining the from-import gotcha in the source code. The bug is the import style; the fix is the import style. Comments explaining Python gotchas belong in commit messages, not source.
- Do NOT add a test for this fix. The existing tests pass; the bug is a runtime UI issue that requires a real GTK event loop to exercise. Tests for this would be brittle and out of scope.

## Verification commands to run (in order)

1. **No `from ui.constants` imports anywhere:**
   ```bash
   cd /path/to/projects/crabcakes && grep -rn "^from ui.constants" ui/ --include="*.py"
   ```
   Expect: 0 matches.

2. **All STREAMING_ENABLED reads use module reference:**
   ```bash
   cd /path/to/projects/crabcakes && grep -rn "STREAMING_ENABLED" ui/ --include="*.py"
   ```
   Expect: matches in `ui/constants.py` (definition), `ui/handlers/chat_handler.py` (`import ui.constants` + `ui.constants.STREAMING_ENABLED` reads), `ui/toolbar.py` (`import ui.constants` + `ui.constants.STREAMING_ENABLED` reads/writes). No bare `STREAMING_ENABLED` reads in chat_handler.py or toolbar.py.

3. **Runtime behavior test (the actual bug):**
   ```bash
   cd /path/to/projects/crabcakes && python3 -c "
   import sys; sys.path.insert(0, '.')
   import ui.constants
   import ui.handlers.chat_handler as ch
   # Verify the runtime toggle now works:
   ui.constants.STREAMING_ENABLED = True
   if ui.constants.STREAMING_ENABLED:
       print('OK: ui.constants.STREAMING_ENABLED reflects runtime mutation')
   else:
       print('BUG: STREAMING_ENABLED stale')
   "
   ```
   Expect: `OK: ui.constants.STREAMING_ENABLED reflects runtime mutation`

4. **Full test suite (sanity — fix should be behavior-preserving for tests):**
   ```bash
   cd /path/to/projects/crabcakes && python3 -m pytest -x -q
   ```
   Expect: 1662 passed, 1 skipped, 4 warnings (same as Phase A baseline).

## Report

When done, send back a completion report with:
- Files changed with line numbers
- Output of all 4 verification commands
- Full pytest output
- COMPLETENESS checklist
- Any related issues found (flagged, not fixed)

```
COMPLETENESS:
- [x] Edit 1: replaced from-import with module reference in chat_handler.py — line N, evidence: <grep output>
- [x] Edit 2: replaced from-import with module reference in toolbar.py — line N, evidence: <grep output>
- [x] Verification 1: 0 from-imports of ui.constants — evidence: <output>
- [x] Verification 2: all STREAMING_ENABLED reads use module reference — evidence: <output>
- [x] Verification 3: runtime toggle works — evidence: <python output>
- [x] Verification 4: full test suite no new failures — evidence: <output>
- [x] Related-bug scan: <list any related issues found, or "none">
```
