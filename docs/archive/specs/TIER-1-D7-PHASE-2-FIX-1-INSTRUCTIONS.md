# TIER-1-D7-PHASE-2-FIX-1-INSTRUCTIONS — Continue button does not advance view

**Phase:** 2.5 of 6 (Phase 2 bug fix)
**Severity:** HIGH (the wizard is functionally broken — user clicks Continue, nothing visible happens)
**Reference:** `ui/views/auxilium_wizard.py` line 388-403 (`_on_continue_clicked` method)

---

## Bug

`_on_continue_clicked()` fires the user callback (which calls `handler.advance_to_gateway()`), but the **view itself never re-syncs to the handler's new state**. The handler advances through its state machine, but the visible frame stays on "install check" forever.

**Audit probe that caught it:**
```python
w._on_continue_clicked()  # user clicks Continue
# Expected: view shows gateway frame
# Actual:   view still shows install frame
#           (handler state correctly advanced, but view didn't observe it)
```

**Output:**
```
Probe B: state.step=gateway_check, frame=install_check, fired=['callback']
AssertionError
```

**Why this matters:** the entire user-facing flow is broken. From the user's perspective, clicking "Continue" on the install check appears to do nothing — the wizard never advances past step 1.

---

## Root cause

The view's `_on_continue_clicked()` method dispatches to one of the wired callbacks (`on_install_check_complete`, etc.) which fire the handler's advance methods. But the view itself has no observer mechanism — it only reads handler state in two places: (1) `_sync_to_handler_state()` (called once at init), and (2) the gateway poll timer (which only runs on the gateway frame).

The architecture is "view polls handler state" but the polling is only active during the gateway step. On every other step, the view reads handler state exactly once at init and then never again. Any handler state change initiated by a button click is invisible to the view.

---

## Fix

After firing the user callback, the view must re-read the handler state and update its frame. The simplest, most local fix is to call `self._sync_to_handler_state()` at the end of `_on_continue_clicked()`.

**Updated `_on_continue_clicked()`:**
```python
def _on_continue_clicked(self) -> None:
    """Continue/Finish button clicked — dispatch based on current frame."""
    idx = self._get_frame_index()

    if idx == 0:
        # Install check → advance to gateway
        self._on_install_check_complete()
    elif idx == 1:
        # Gateway check → advance to provider
        self._on_gateway_check_complete()
    elif idx == 2:
        # Provider pick → finish
        self._on_finish_clicked()
        return  # finish has its own validation; don't re-sync mid-validation

    # Re-sync view to handler state after a state transition.
    # The handler has now advanced; switch the visible frame to match.
    self._sync_to_handler_state()
```

Note the `return` on the finish path — `_on_finish_clicked()` validates the form and may bail early (`api_key empty → grab focus, return`). In that case, the handler state is *not* advanced (we never called `handler.set_provider_choice`), so re-syncing would be a no-op but is misleading. The early return keeps the semantics clear.

For the gateway path (idx == 1), `_sync_to_handler_state` will read the handler state, see `step == gateway_check`, and call `_start_gateway_poll()`. But the gateway probe may have already completed in the background between the previous `_start_gateway_poll` and now. The existing `_sync_to_handler_state` already handles this case: it checks `gw.get("error")` and `gw.get("error") != "Probing..."` and renders the result directly without starting a new poll timer. So the fix is safe.

---

## Files to read

- `ui/views/auxilium_wizard.py` lines 388-403 (`_on_continue_clicked`)
- `ui/views/auxilium_wizard.py` lines 335-362 (`_sync_to_handler_state` — the function you'll be calling)
- `ui/handlers/auxilium_wizard_handler.py` lines 100-200 (handler state transitions, to confirm what `_sync_to_handler_state` will see after each click)

---

## Verification commands (run and paste output)

```bash
# 1. Re-run the failing probe — Continue on install must advance the view
cd /path/to/projects/crabcakes && G_DEBUG=fatal-criticals python3 -c "
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
from ui.views.auxilium_wizard import AuxiliumWizard
from ui.handlers.auxilium_wizard_handler import AuxiliumWizardHandler
import tempfile, time
from pathlib import Path

with tempfile.TemporaryDirectory() as tmp:
    handler = AuxiliumWizardHandler(config_dir=Path(tmp), on_complete=lambda: None, on_error=lambda msg: None)
    handler.start()
    w = AuxiliumWizard(
        handler=handler,
        on_install_check_complete=lambda: handler.advance_to_gateway(),
        on_gateway_check_complete=lambda: handler.advance_to_provider(),
        on_provider_selected=lambda: None,
    )
    print(f'before click: current_step={w.current_step}, handler.step={handler.get_state().step.value}')
    w._on_continue_clicked()
    print(f'after click:  current_step={w.current_step}, handler.step={handler.get_state().step.value}')
    assert w.current_step == 'gateway_check', f'expected gateway_check, got {w.current_step}'
    assert handler.get_state().step.value == 'gateway_check'
    print('PASS: Continue advances both handler and view to gateway_check')
    w.cleanup()
"

# 2. Re-run probe A (initial state) — must still show install_check
cd /path/to/projects/crabcakes && G_DEBUG=fatal-criticals python3 -c "
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
from ui.views.auxilium_wizard import AuxiliumWizard
from ui.handlers.auxilium_wizard_handler import AuxiliumWizardHandler
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as tmp:
    handler = AuxiliumWizardHandler(config_dir=Path(tmp), on_complete=lambda: None, on_error=lambda msg: None)
    handler.start()
    w = AuxiliumWizard(handler=handler, on_install_check_complete=lambda: None, on_gateway_check_complete=lambda: None, on_provider_selected=lambda: None)
    assert w.current_step == 'install_check'
    print('PASS: initial state shows install_check')
"

# 3. Architecture + KB tests still pass
cd /path/to/projects/crabcakes && pytest tests/test_architecture.py tests/test_kb_lookup.py -q 2>&1 | tail -3
```

---

## Rules

- **Use the steelFramedCodeWriter prompt.** Apply every rule.
- **One method, surgical fix.** Add `self._sync_to_handler_state()` at the end of `_on_continue_clicked()` and a `return` in the finish branch. Do not refactor anything else.
- **Do not call `_sync_to_handler_state()` from `_on_finish_clicked()`.** The finish path validates the form and may bail early. Re-syncing after validation is misleading.
- **Do not touch Phase 1 work.** The handler is correct; the bug is purely in the view.
- **Do not touch the gateway poll logic.** The existing `_sync_to_handler_state` already handles the "probe already finished" case for the gateway frame.

---

## COMPLETENESS template (paste at the end)

```
COMPLETENESS:
- [x] _on_continue_clicked now calls self._sync_to_handler_state() at the end — <paste the new method body>
- [x] finish branch (idx == 2) returns early, does NOT re-sync — <paste snippet>
- [x] Probe B (Continue advances view) now passes — <paste command 1 output>
- [x] Probe A (initial state) still shows install_check — <paste command 2 output>
- [x] Architecture tests still pass — <paste command 3 output>
- [x] Diff is one method body — <paste git diff --stat output>
- [x] NOT DONE / DEFERRED: tests (Phase 4), wiring (Phase 3), styles.py (follow-up)
```

Please write when ready. After this fix, the audit on this phase is complete and I will move to Phase 3 (wiring).
