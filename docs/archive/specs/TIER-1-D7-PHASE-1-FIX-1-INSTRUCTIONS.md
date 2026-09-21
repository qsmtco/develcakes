# TIER-1-D7-PHASE-1-FIX-1-INSTRUCTIONS — Snapshot Bug

**Phase:** 1.5 of 4 (Phase 1 bug fix)
**Severity:** HIGH
**Reference:** `ui/handlers/auxilium_wizard_handler.py` line 100 (`get_state` method)

---

## Bug

`get_state()` returns `self._state` (a live reference to the internal `WizardState` dataclass instance) instead of a copy. The docstring promises a "snapshot" but the implementation returns the live object.

**Audit probe that caught it:**
```python
h = AuxiliumWizardHandler(...)
s1 = h.get_state()
s1.step = WizardStep.DONE  # caller mutates the returned state
s2 = h.get_state()
assert s2.step != WizardStep.DONE  # FAILS — internal state is corrupted
```

**Attack vectors this enables:**
- The view code in Phase 2 (or any future consumer) can accidentally mutate `state.step` or `state.install_check` and corrupt the handler. There is no defensive copy.
- The view that does `for k, v in state.install_check.items()` and then `state.install_check['something'] = ...` will silently corrupt the handler.
- Multi-threaded consumers (e.g., the gateway thread writes `self._state.gateway_check` while the view reads via `get_state()`) can race — the view sees a half-mutated dict.

**Why this matters for the Phase 2 view:** The view will poll `get_state()` from a `GLib.timeout_add` callback and render the result. If the view ever does `state.install_check['last_seen'] = time.time()` (a natural thing to do for "I've seen this") the handler loses the actual install check data on the next call.

**Spec quote (your own line 33):**
> `WizardState` ... `Snapshot of wizard state — consumed by the view to render the current step.`

---

## Fix

In `get_state()`, return a deep copy:

```python
import copy
def get_state(self) -> WizardState:
    """Return a deep copy of the current wizard state. Used by the view to render."""
    return copy.deepcopy(self._state)
```

Use `copy.deepcopy` because the dicts inside `WizardState` are also mutable. A shallow `dataclasses.replace(self._state)` would still share the dict references.

---

## Files to read

- `ui/handlers/auxilium_wizard_handler.py` lines 30-40 (WizardState dataclass), 95-105 (get_state)
- (Optional) `docs/specs/SPEC-auxilium-tier-1.md` §D7 to confirm snapshot semantics

---

## Verification commands (run and paste output)

```bash
# 1. Re-run the failing probe
cd /path/to/projects/crabcakes && python3 -c "
from pathlib import Path
import tempfile
from ui.handlers.auxilium_wizard_handler import AuxiliumWizardHandler, WizardStep

with tempfile.TemporaryDirectory() as tmp:
    h = AuxiliumWizardHandler(config_dir=Path(tmp), on_complete=lambda: None, on_error=lambda msg: None)
    s1 = h.get_state()
    s1.step = WizardStep.DONE
    s1.install_check['corrupted'] = True
    s2 = h.get_state()
    assert s2.step != WizardStep.DONE, f's2.step={s2.step.value}'
    assert 'corrupted' not in s2.install_check, f'install_check has caller mutation'
    print('PASS: get_state returns deep copy')
"

# 2. Confirm all architecture tests still pass
cd /path/to/projects/crabcakes && pytest tests/test_architecture.py tests/test_kb_lookup.py -q 2>&1 | tail -3

# 3. Re-run the full install-check smoke from Phase 1
cd /path/to/projects/crabcakes && python3 -c "
from pathlib import Path
from ui.handlers.auxilium_wizard_handler import AuxiliumWizardHandler
h = AuxiliumWizardHandler(config_dir=Path.home() / '.config' / 'crabcakes', on_complete=lambda: None, on_error=lambda msg: None)
h.start()
s = h.get_state()
print(f'step={s.step.value} platform={s.install_check[\"platform\"]} ok={s.install_check[\"ok\"]}')
"
```

---

## Rules

- **Use the steelFramedCodeWriter prompt.** Apply every rule.
- **One edit only:** change `get_state()`. Do not refactor anything else.
- **Add `import copy` at the top of the file** (alphabetical with the existing imports).
- **Update the docstring** to say "deep copy" explicitly.
- **Do NOT change other methods.** The fix is surgical.
- **Do NOT touch Phase 2 work.** View doesn't exist yet.

---

## COMPLETENESS template (paste at the end)

```
COMPLETENESS:
- [x] get_state now returns copy.deepcopy(self._state) — <paste the new method>
- [x] `import copy` added — <grep output>
- [x] Docstring updated to say "deep copy" — <paste the new docstring>
- [x] Probe 7 (caller mutation) now passes — <paste output of command 1>
- [x] Architecture tests still pass — <paste command 2 output>
- [x] Install check smoke still works — <paste command 3 output>
- [x] Diff is one method + one import — <paste git diff --stat output>
- [x] NOT DONE / DEFERRED: view (Phase 2), wiring (Phase 3), tests (Phase 4)
```

Please write when ready. After this fix, the audit on this phase is complete and I will move to Phase 2.
