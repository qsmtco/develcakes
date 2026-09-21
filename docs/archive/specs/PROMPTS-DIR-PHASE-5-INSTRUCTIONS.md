# PHASE 5 of 7 — wire project switches into prompts handlers

**Spec:** `docs/specs/SPEC-PROJECT-PROMPTS-DIRECTORY.md` (§2.4, §2.7(c) incl. its single-registration warning; data flows §3.1–§3.4)
**Scope:** exactly 1 file — `ui/window.py`. Nothing else.

## Context you must respect

- `PromptsHandler.set_project_path` / `InputToolbarHandler.set_project_path` already exist (Phase 3).
- `seed_project_prompts(project_path)` exists in `utils/project_awareness.py` (Phase 2) — idempotent, copy-only-if-missing, safe to call on EVERY project open (that IS the lazy-seed mechanism; no guard needed at the call site).
- The open/close callbacks are SINGLE lambdas registered once at `ui/window.py` ~lines 561/579. They are tuples of expressions. **Do NOT register a second `set_on_project_opened`/`set_on_project_closed` — it would overwrite the existing registration** (spec §2.7 Note).

## Changes (all in ui/window.py)

1. Top-of-file import (with the other utils imports): `from utils.project_awareness import seed_project_prompts` — verify no import cycle (project_awareness imports no ui modules; safe).

2. In the `set_on_project_opened` lambda tuple (~line 561), AFTER `set_active_project_path(p)` and keeping tuple syntax valid (trailing commas), append IN THIS ORDER:
   ```python
                # SPEC-PROJECT-PROMPTS-DIRECTORY: lazy-seed the per-project
                # prompt library (idempotent), then point both prompt
                # consumers at it and refresh the UI.
                seed_project_prompts(p),
                self._prompts_handler.set_project_path(p),
                self._input_toolbar_handler.set_project_path(p),
                self._prompts_handler.load_prompts(),
                self._left_panel.refresh_prompts(),
   ```
   Order matters: seed BEFORE set_project_path (resolver isdir-check must see the seeded dir on first open); load_prompts AFTER both setters.

3. In the `set_on_project_closed` lambda tuple (~line 579), append:
   ```python
                # Reset prompt library to app-level fallback when no project
                # is active.
                self._prompts_handler.set_project_path(None),
                self._input_toolbar_handler.set_project_path(None),
                self._prompts_handler.load_prompts(),
                self._left_panel.refresh_prompts(),
   ```

4. Check `self._input_toolbar_handler` is constructed BEFORE these registrations execute (construction ~line 314, registrations ~line 561 — should be fine; confirm and state it in your report).

## Verification constraints

`ui/window.py` is GTK glue — no new unit-test seam is being added this phase. Evidence required:
- `grep -n "seed_project_prompts\|set_project_path\|load_prompts\|refresh_prompts" ui/window.py` pasted.
- `python3 -B -m pytest tests/ -q -p no:cacheprovider -x --ignore=tests/test_chat_input_toolbar.py` full-suite output (chat_input_toolbar excluded: known environmental segfault, documented by Debugger in the Phase 3 audit). If your exec is gated, say so explicitly and paste whatever command output you CAN produce; supervisor will run the suite independently.
- Confirm via reading that the lambda tuples remain syntactically valid single expressions (no stray semicolons, trailing commas correct).

## Rules

- Use the **steelFramedCodeWriter** prompt at `prompts/steelFramedCodeWriter.md`.
- Read ui/window.py lines ~270–330 and ~540–600 fully before editing.
- Flag ANY deviation explicitly.

COMPLETENESS:
- [x/not done] Edit 1: import added — evidence
- [x/not done] Edit 2: opened-callback additions (5 lines, correct order) — evidence (grep)
- [x/not done] Edit 3: closed-callback additions (4 lines) — evidence (grep)
- [x/not done] Construction-order confirmation for _input_toolbar_handler — stated
