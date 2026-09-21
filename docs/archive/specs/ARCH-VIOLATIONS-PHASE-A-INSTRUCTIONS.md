# Phase A of A — ARCH §8.6 View/Handler Boundary Violations (3 fixes)

**Supervisor:** Qaster
**Builder:** QTR
**Date:** 2026-06-18
**Trigger:** Adversarial audit of ARCHITECTURE.md compliance (2026-06-18) found 3 real architectural boundary violations: views importing handlers. Per ARCH §8.6 R2 (line 824) and §8.6 R7 (line 886), views must not import handlers. The composition root (`ui/window.py`) wires handlers into views via constructor or setter.

**Scope:** 3 fixes in 3 files. Each fix is 1-2 lines of code plus a `__init__` signature change for the two view files. **No new design work** — the pattern is already documented in ARCH §8.6 and the views already have setter methods that take the handler (verified in the file inspection).

---

## Severity of the violations

All 3 are rated **HIGH** in my audit (handler-to-handler or view-to-handler imports). The original report rated them CRITICAL, but on re-audit I downgraded to HIGH because:
- They are import-level violations, not runtime-call violations (no view is *invoking* a handler's methods through the import)
- The existing setter methods (`set_prompts_handler` at left_panel.py:170, `handler` parameter at settings_dialog.py:280) already exist — the imports are for type annotations only
- For `toolbar.py`, the import is a module-level constant, not a class — extracting to a neutral module is the minimal fix

---

## Edit 1: `ui/views/left_panel.py` — remove `from ui.handlers.prompts_handler import PromptsHandler`

**File:** `ui/views/left_panel.py`
**Current line 13:** `from ui.handlers.prompts_handler import PromptsHandler`

**Replacement:** Remove line 13 entirely. The class is referenced only as a type annotation; the actual instance is passed via the existing `set_prompts_handler()` method at line 170.

**Additional change:** Add a `from __future__ import annotations` import at the top of the file (if not already present) so that any forward reference to `PromptsHandler` in docstrings or annotations works without the import. Check the file header first.

**Verification:**
```bash
cd /path/to/projects/crabcakes && grep -n "from ui.handlers" ui/views/left_panel.py
```
Expect: 0 matches.

```bash
cd /path/to/projects/crabcakes && grep -n "PromptsHandler" ui/views/left_panel.py
```
Expect: matches only inside docstrings or string annotations (e.g., `set_prompts_handler` method body, docstring of `set_prompts_handler`). No `from ui.handlers.prompts_handler` import.

**Sanity check (read the file after edit):**
- Line 13 should be gone
- The class should still construct (the import was for type hints, not runtime behavior)
- The setter at line 170 should still take a `PromptsHandler` parameter (its docstring says "Set the PromptsHandler and refresh the prompts tab")

---

## Edit 2: `ui/views/settings_dialog.py` — remove `from ui.handlers.settings_handler import SettingsHandler`

**File:** `ui/views/settings_dialog.py`
**Current line 22:** `from ui.handlers.settings_handler import SettingsHandler`

**Replacement:** Remove line 22 entirely. The class is already imported via `from __future__ import annotations` (line 12) and the existing `handler: SettingsHandler` parameter at line 280 already documents the dependency without needing a runtime import.

**Verification:**
```bash
cd /path/to/projects/crabcakes && grep -n "from ui.handlers" ui/views/settings_dialog.py
```
Expect: 0 matches.

```bash
cd /path/to/projects/crabcakes && grep -n "SettingsHandler" ui/views/settings_dialog.py
```
Expect: matches only in docstrings/annotations, not as an import.

**Sanity check:**
- Line 22 should be gone
- The `from __future__ import annotations` at line 12 makes type annotations lazy, so removing the runtime import is safe
- The constructor at line 280 should still take `handler: SettingsHandler`

---

## Edit 3: `ui/toolbar.py` — extract `STREAMING_ENABLED` to `ui/constants.py`

**Files:** `ui/toolbar.py`, `ui/handlers/chat_handler.py`, **new file** `ui/constants.py`

This is the trickiest of the 3 because `STREAMING_ENABLED` is **mutable shared state** that `Toolbar` toggles on button click (line 96) and `ChatHandler` reads on every streaming event (lines 583, 602, 630). The minimal fix that satisfies ARCH §8.6 R7 (line 886) is to move the constant to a neutral location.

**Step 3a — create `ui/constants.py`:**

```python
# ui/constants.py
# Cross-cutting UI constants used by both views and handlers.
#
# Architecture rule (ARCHITECTURE.md §8.6 R7):
#   Views must not import from ui/handlers/. To share state between a view
#   and a handler, put the constant here. Both sides import from this neutral
#   module.
#
# Mutable state lives here, not on handler classes, when both the view and
# the handler need to read AND write it. For one-way state (handler-only or
# view-only), pass via constructor or setter from ui/window.py instead.

# Streaming toggle: when True, the chat shows live token deltas as the agent
# types. When False, only the final assembled message is shown. The toolbar's
# stream button toggles this; ChatHandler reads it on every streaming event.
STREAMING_ENABLED: bool = False
```

**Step 3b — update `ui/handlers/chat_handler.py`:**

**Current line 23:** `STREAMING_ENABLED = False  # True = show live updates as agent types; False = final only`

**Replacement:** Remove line 23 entirely. Add at the top (after the existing imports):
```python
from ui.constants import STREAMING_ENABLED
```

**Step 3c — update `ui/toolbar.py`:**

**Current line 9:** `from ui.handlers.chat_handler import STREAMING_ENABLED`

**Replacement:** Replace with:
```python
from ui.constants import STREAMING_ENABLED
```

**Step 3d — update the `set_streaming` toggle inside `Toolbar._on_stream_toggled` (line 96):**

**Current code at line 95-96:**
```python
        import ui.handlers.chat_handler as chat_handler
        chat_handler.STREAMING_ENABLED = button.get_active()
```

**Replacement:** Replace with:
```python
        import ui.constants as ui_constants
        ui_constants.STREAMING_ENABLED = button.get_active()
```

**Step 3e — update the read inside `Toolbar._update_stream_label` (line 101-103):**

**Current code:**
```python
        import ui.handlers.chat_handler as chat_handler
        self._stream_btn.set_label(
            "Stream: ON" if chat_handler.STREAMING_ENABLED else "Stream: OFF"
        )
```

**Replacement:** Replace with:
```python
        import ui.constants as ui_constants
        self._stream_btn.set_label(
            "Stream: ON" if ui_constants.STREAMING_ENABLED else "Stream: OFF"
        )
```

**Verification:**
```bash
cd /path/to/projects/crabcakes && grep -n "from ui.handlers.chat_handler" ui/toolbar.py
```
Expect: 0 matches.

```bash
cd /path/to/projects/crabcakes && grep -n "import.*chat_handler" ui/toolbar.py
```
Expect: 0 matches (the lazy `import` lines should also be gone, replaced with `import ui.constants`).

```bash
cd /path/to/projects/crabcakes && grep -n "STREAMING_ENABLED" ui/handlers/chat_handler.py ui/toolbar.py ui/constants.py
```
Expect:
- `ui/constants.py`: 1 match (the definition)
- `ui/handlers/chat_handler.py`: 1 match (the new import at top), plus matches where the constant is *read* in method bodies (lines ~583, 602, 630)
- `ui/toolbar.py`: 1 match (the new import at top), plus matches where the constant is *read* or *written* in method bodies (the read at line 32, the write at the new toggled line, the read at the new label-update line)

**Note on mutability:** `STREAMING_ENABLED` is a module-level mutable bool. Python's lack of const enforcement means both `toolbar.py` and `chat_handler.py` will continue to mutate/read it via `ui.constants.STREAMING_ENABLED`. This is a code smell but is OUT OF SCOPE for this fix. A future refactor could replace it with a `StreamingState` class injected into both consumers. **Do not expand scope.**

---

## Edit 4: `models/__init__.py` — add `task_store` to `__all__`

**File:** `models/__init__.py`
**Location:** The `__all__` list (line 25 onward)

**Action:** Add `"task_store",` to the `__all__` list, in a sensible location (e.g., after the `# task` section header, before the `# colors` section). Use the existing alphabetical-ish ordering as a guide — the `# task` section already groups `Task`, `TaskStore`, `TASK_STATUS_LABELS`, `PRIORITY_LABELS`. Add `task_store` to that group.

**Verification:**
```bash
cd /path/to/projects/crabcakes && python3 -c "import models; assert 'task_store' in models.__all__; print('OK')"
```
Expect: `OK`

---

## Edit 5: `docs/ARCHITECTURE.md` §13 — update test count

**File:** `docs/ARCHITECTURE.md`
**Location:** Line 3512 (the "Test count" entry)

**Current text:**
> **Test count:** 61 test files (snapshot as of 2026-05-31: 1680 tests collected; 1632 passed, 1 failed, 0 errors). 22 TestUpdateAgentSession errors resolved in Phase 5; 1 pre-existing failure in test_agents.py::TestLoadAgentDefs::test_does_not_overwrite_existing. Run `pytest --co -q` for current count.

**Replacement text:**
> **Test count:** 84 test files (snapshot as of 2026-05-31 was 61; grew by 23 files in June 2026 with the KB, providers, settings, and wizard features). For the current collected-test count and pass/fail status, run `pytest --co -q` and `pytest -q`. The explicit test-file enumeration in §13 is illustrative, not exhaustive — new tests are added with the features they cover and may not be retroactively enumerated.

**Verification:**
```bash
cd /path/to/projects/crabcakes && grep -n "61 test files\|84 test files" docs/ARCHITECTURE.md
```
Expect: 0 matches for "61 test files"; 1 match for "84 test files" at the updated location.

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md` for every edit
- Read each file in full before editing (left_panel.py ~200 lines, settings_dialog.py ~400 lines, toolbar.py ~150 lines, chat_handler.py ~700 lines, models/__init__.py ~63 lines, ARCHITECTURE.md is long — only the §13 area matters here)
- Scope is exactly 5 edits in 5 files (4 modified + 1 new). Do NOT touch any other file.
- Do NOT silently expand scope. If you find a related issue (e.g., the `STREAMING_ENABLED` mutable-state smell), note it in the COMPLETENESS checklist under "related issues found" and stop. The supervisor decides what to do.
- Do NOT refactor `STREAMING_ENABLED` into a class. That is a Tier 2+ evolution suggestion, not this fix.
- Do NOT touch the test suite to add tests for these changes. The existing tests cover the behavior; the fix preserves the behavior. New tests are out of scope.

## Verification commands to run (in order)

1. **Per-file removal checks:**
   ```bash
   cd /path/to/projects/crabcakes && grep -n "from ui.handlers" ui/views/left_panel.py ui/views/settings_dialog.py ui/toolbar.py
   ```
   Expect: 0 matches across all 3 files.

   ```bash
   cd /path/to/projects/crabcakes && grep -n "import.*chat_handler" ui/toolbar.py
   ```
   Expect: 0 matches.

2. **New file check:**
   ```bash
   cd /path/to/projects/crabcakes && ls -la ui/constants.py
   ```
   Expect: file exists, size > 0.

3. **STREAMING_ENABLED usage check:**
   ```bash
   cd /path/to/projects/crabcakes && grep -rn "STREAMING_ENABLED" ui/ --include="*.py"
   ```
   Expect: matches in `ui/constants.py` (definition + import), `ui/handlers/chat_handler.py` (import + reads), `ui/toolbar.py` (import + reads/writes via `ui.constants.STREAMING_ENABLED`). No references to `chat_handler.STREAMING_ENABLED` should remain.

4. **Task store __all__ check:**
   ```bash
   cd /path/to/projects/crabcakes && python3 -c "import models; assert 'task_store' in models.__all__; print('OK')"
   ```
   Expect: `OK`

5. **ARCH §13 test count check:**
   ```bash
   cd /path/to/projects/crabcakes && grep -n "61 test files\|84 test files" docs/ARCHITECTURE.md
   ```
   Expect: 0 matches for "61 test files"; 1 match for "84 test files".

6. **Full test suite (sanity — fix should be behavior-preserving):**
   ```bash
   cd /path/to/projects/crabcakes && python3 -m pytest -x -q
   ```
   Expect: no new failures. The pre-existing 1 skipped is unchanged.

## Report

When done, send back a completion report with:
- Files changed with line numbers
- Full pytest output for the full suite
- Output of all 6 verification commands
- COMPLETENESS checklist (see template below)
- Any related issues found (flagged, not fixed)

```
COMPLETENESS:
- [x] Edit 1: removed PromptsHandler import from left_panel.py — line N, evidence: <grep output>
- [x] Edit 2: removed SettingsHandler import from settings_dialog.py — line N, evidence: <grep output>
- [x] Edit 3a: created ui/constants.py — line N, evidence: <ls output>
- [x] Edit 3b: updated chat_handler.py to import from ui.constants — line N, evidence: <grep output>
- [x] Edit 3c: updated toolbar.py to import from ui.constants — line N, evidence: <grep output>
- [x] Edit 3d: updated toolbar._on_stream_toggled to mutate ui.constants — line N, evidence: <grep output>
- [x] Edit 3e: updated toolbar._update_stream_label to read ui.constants — line N, evidence: <grep output>
- [x] Edit 4: added task_store to models/__init__.py __all__ — line N, evidence: <python output>
- [x] Edit 5: updated ARCH §13 test count — line N, evidence: <grep output>
- [x] Verification 1: 0 matches for view→handler imports — evidence: <output>
- [x] Verification 2: ui/constants.py exists — evidence: <output>
- [x] Verification 3: STREAMING_ENABLED usage clean — evidence: <output>
- [x] Verification 4: task_store in __all__ — evidence: <output>
- [x] Verification 5: ARCH §13 has "84 test files" — evidence: <output>
- [x] Verification 6: full test suite no new failures — evidence: <output>
- [x] Related-bug scan: <list any related issues found, or "none">
```

Do not skip the COMPLETENESS checklist. The supervisor will send the work back if it is missing.
