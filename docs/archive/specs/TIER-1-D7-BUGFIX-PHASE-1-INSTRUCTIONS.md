# TIER-1-D7-BUGFIX-PHASE-1-INSTRUCTIONS

**Phase:** 1 of 4
**Bugs fixed:** #1 (CRITICAL)
**Spec reference:** `docs/specs/SPEC-auxilium-tier-1.md` §D7
**Architecture:** `docs/ARCHITECTURE.md` §8.6 (Handler Pattern)

---

## Goal

Fix Bug #1 — `AuxiliumWizardHandler.start()` is never called in `ui/window.py` after the handler is constructed. Fresh-install users see the install frame stuck on "Checking..." forever.

---

## Files to read FIRST (mandatory)

1. **`ui/window.py`** — lines 195-240 (the Auxilium wizard wiring block). Read the full block to confirm where `AuxiliumWizardHandler` is constructed and where the view is created.
2. **`ui/handlers/auxilium_wizard_handler.py`** — lines 91-135 (the `start()` method and the state machine). Confirm what `start()` does (runs install check synchronously, fires no callbacks, sets state to INSTALL_CHECK).
3. **`ui/views/auxilium_wizard.py`** — lines 100-115 (the `_sync_to_handler_state()` method at init). Confirm that an empty `install_check` dict causes the "Checking..." placeholder to render and never update.

---

## The Bug

In `ui/window.py` around lines 214-227:

```python
self._auxilium_wizard_handler = AuxiliumWizardHandler(
    config_dir=_config_dir,
    on_complete=lambda: self._on_auxilium_wizard_complete(),
    on_error=lambda msg: logger.error("Auxilium wizard error: %s", msg),
)
self._auxilium_wizard = AuxiliumWizard(
    handler=self._auxilium_wizard_handler,
    on_install_check_complete=lambda: self._auxilium_wizard_handler.advance_to_gateway(),
    on_gateway_check_complete=lambda: self._auxilium_wizard_handler.advance_to_provider(),
    on_provider_selected=lambda: None,
)
_wizard_chat_box.append(self._auxilium_wizard)
```

`self._auxilium_wizard_handler.start()` is never called. The handler's `start()` method runs the synchronous install check (platform, Python version, GTK4, websockets detection) and sets `state.step = WizardStep.INSTALL_CHECK` with populated `state.install_check`. Without it, the install frame's `_sync_to_handler_state()` at line ~109 checks `if state.install_check:` — an empty dict is falsy — so it renders the "Checking..." placeholder and never updates.

---

## Fix

After `AuxiliumWizard` is constructed and appended (line ~227), add:

```python
self._auxilium_wizard_handler.start()
```

This must come **after** the view is created (the view polls `get_state()` on a GLib timer), not before.

---

## Verification commands

```bash
# 1. Confirm the fix is present
grep -n "\.start()" ui/window.py | grep auxilium

# 2. Does the module import cleanly?
cd /path/to/projects/crabcakes && python3 -c "from ui.handlers.auxilium_wizard_handler import AuxiliumWizardHandler, WizardStep; print('imports OK')"

# 3. Does the app launch without crash?
cd /path/to/projects/crabcakes && xvfb-run -a python3 -c "
import gi; gi.require_version('Gtk', '4.0')
from ui.window import MainWindow
w = MainWindow(application=None)
print('window created OK')
w.close()
" 2>&1 | tail -3

# 4. Run existing tests
cd /path/to/projects/crabcakes && pytest tests/test_auxilium_tier1.py -q 2>&1 | tail -5
```

---

## Report format

1. **Files changed:** list with line numbers
2. **Discovery block:** what you read and what you confirmed
3. **Verification output:** paste the 4 command outputs verbatim
4. **COMPLETENESS checklist** — fill in:

```
COMPLETENESS:
- [x/not done] Fix: added self._auxilium_wizard_handler.start() — evidence
- [x/not done] No regressions in existing tests — evidence
- [x/not done] No GTK import issues — evidence
- [x/not done] Related issues found (flag only) — list
```