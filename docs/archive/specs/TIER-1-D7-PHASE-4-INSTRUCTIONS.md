# TIER-1-D7-PHASE-4-INSTRUCTIONS — Auxilium Wizard Tests

**Phase:** 4 of 6 (D7 sub-phase 4 — tests)
**Spec:** `docs/specs/SPEC-auxilium-tier-1.md` §D7
**Architecture:** `docs/ARCHITECTURE.md` §8.5 (Testing)

---

## Goal

Write `tests/test_auxilium_tier1.py` — 7 smoke tests for the Auxilium wizard (handler + view + wiring). These are integration tests that run the full wizard flow from a test environment.

**Note on GTK in tests:** Use `xvfb-run -a` for any test that instantiates GTK widgets. The architecture test suite already uses this pattern — check `pytest.ini` or `conftest.py` for the existing configuration.

---

## Files to read FIRST (mandatory)

Read these files completely before writing any code:

1. **`tests/test_kb_lookup.py`** — the existing test pattern. Study how it handles the model loading (singleton pattern), how it cleans up between tests, and how it uses `xvfb-run`.
2. **`tests/conftest.py`** — if it exists, check for shared fixtures (model cache, tmp dirs, etc.). If it doesn't exist, check `pytest.ini` for xvfb configuration.
3. **`ui/handlers/auxilium_wizard_handler.py`** (Phase 1 completed, 387 lines) — the handler's public API and state machine.
4. **`ui/views/auxilium_wizard.py`** (Phase 2 completed, 439 lines) — the view's `__init__` signature, `current_step` property, `cleanup()` method.
5. **`docs/ARCHITECTURE.md` §8.5 (Testing)** — testing conventions: how to run tests, what the test infrastructure looks like.
6. **`pytest.ini`** — to confirm the xvfb configuration and any test markers.

---

## Output: `tests/test_auxilium_tier1.py`

### Test structure

```python
# tests/test_auxilium_tier1.py
# Smoke tests for the Auxilium first-run wizard (D7, Tier 1)
import pytest
import tempfile
import time
from pathlib import Path

# Tests that need GTK must be marked @pytest.mark.gtk or run via xvfb-run.
# See how test_kb_lookup.py handles this.
```

### 7 tests

**Test 1: `test_wizard_handler_imports`** — Module imports cleanly.
```python
def test_wizard_handler_imports():
    from ui.handlers.auxilium_wizard_handler import (
        AuxiliumWizardHandler, WizardStep, WizardState, is_auxilium_wizard_needed
    )
    assert AuxiliumWizardHandler is not None
    assert WizardStep is not None
    assert WizardState is not None
    assert callable(is_auxilium_wizard_needed)
```

**Test 2: `test_wizard_view_imports`** — View imports cleanly.
```python
def test_wizard_view_imports():
    import gi
    gi.require_version('Gtk', '4.0')
    from ui.views.auxilium_wizard import AuxiliumWizard
    assert AuxiliumWizard is not None
```

**Test 3: `test_is_auxilium_wizard_needed_missing_file`** — Empty config dir → wizard needed.
```python
def test_is_auxilium_wizard_needed_missing_file():
    with tempfile.TemporaryDirectory() as tmp:
        from ui.handlers.auxilium_wizard_handler import is_auxilium_wizard_needed
        assert is_auxilium_wizard_needed(Path(tmp)) is True
```

**Test 4: `test_is_auxilium_wizard_needed_with_providers`** — Config dir with providers.yaml → wizard not needed.
```python
def test_is_auxilium_wizard_needed_with_providers():
    from ui.handlers.auxilium_wizard_handler import is_auxilium_wizard_needed
    real = Path.home() / ".config" / "crabcakes"
    result = is_auxilium_wizard_needed(real)
    # Should be False if providers.yaml exists (may be True if config is fresh)
    assert isinstance(result, bool)
```

**Test 5: `test_handler_install_check_advances_state`** — `start()` sets step to INSTALL_CHECK.
```python
def test_handler_install_check_advances_state():
    with tempfile.TemporaryDirectory() as tmp:
        from ui.handlers.auxilium_wizard_handler import AuxiliumWizardHandler, WizardStep
        calls = []
        h = AuxiliumWizardHandler(
            config_dir=Path(tmp),
            on_complete=lambda: calls.append('complete'),
            on_error=lambda msg: calls.append(f'error: {msg}'),
        )
        h.start()
        state = h.get_state()
        assert state.step == WizardStep.INSTALL_CHECK
        assert state.install_check['ok'] is True  # this machine has all deps
        assert state.install_check['platform'] == 'linux'
        assert calls == []
```

**Test 6: `test_handler_advance_to_gateway`** — `advance_to_gateway()` spawns a thread and sets step to GATEWAY_CHECK.
```python
def test_handler_advance_to_gateway():
    with tempfile.TemporaryDirectory() as tmp:
        from ui.handlers.auxilium_wizard_handler import AuxiliumWizardHandler, WizardStep
        h = AuxiliumWizardHandler(
            config_dir=Path(tmp),
            on_complete=lambda: None,
            on_error=lambda msg: None,
        )
        h.start()
        h.advance_to_gateway()
        state = h.get_state()
        assert state.step == WizardStep.GATEWAY_CHECK
        assert 'ok' in state.gateway_check
        # Wait for probe to complete (3s timeout + buffer)
        time.sleep(4.5)
        state2 = h.get_state()
        assert 'ok' in state2.gateway_check  # probe finished
```

**Test 7: `test_view_current_step_property`** — View's `current_step` property returns the right value.
```python
def test_view_current_step_property():
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk
    from ui.views.auxilium_wizard import AuxiliumWizard
    from ui.handlers.auxilium_wizard_handler import AuxiliumWizardHandler, WizardState

    with tempfile.TemporaryDirectory() as tmp:
        class StubHandler:
            def __init__(self): self._state = WizardState()
            def get_state(self): return self._state
            def start(self): pass
            def advance_to_gateway(self): pass
            def advance_to_provider(self): pass
            def set_provider_choice(self, c, p, m, k): pass

        h = StubHandler()
        w = AuxiliumWizard(
            handler=h,
            on_install_check_complete=lambda: None,
            on_gateway_check_complete=lambda: None,
            on_provider_selected=lambda: None,
        )
        assert w.current_step == 'install_check'
        w.cleanup()
```

### Constraints

- **Use `xvfb-run -a`** for any test that instantiates GTK widgets. The simplest way is to prepend `xvfb-run -a` to the pytest command in the verification step, or to add a `conftest.py` fixture that handles it. Check how `test_kb_lookup.py` handles GTK tests.
- **No real network calls.** The gateway probe test should use a short timeout and not make real outbound connections to unknown hosts. The probe to `ws://localhost:18789` may timeout (that's fine — it tests the timeout/error path).
- **Clean up after each test.** The wizard's `cleanup()` method must be called if the test instantiated a view. Use `pytest.fixture` with `yield` to ensure cleanup runs even if the test fails.
- **Mock the handler where needed.** Tests 1-4 don't need GTK. Tests 5-7 need GTK only for the view tests. Use a `StubHandler` (no GTK) for the handler tests.

### pytest markers

If you add `@pytest.mark.gtk` to GTK tests, ensure `pytest.ini` has the right configuration to run them with `xvfb-run`. If it doesn't, add the marker and a `conftest.py` fixture that uses `xvfb-run`.

---

## Verification commands (run and paste output)

```bash
# Run with xvfb for GTK tests
cd /path/to/projects/crabcakes && xvfb-run -a pytest tests/test_auxilium_tier1.py -v 2>&1 | tail -20

# If xvfb-run is not available, run just the non-GTK tests
cd /path/to/projects/crabcakes && pytest tests/test_auxilium_tier1.py -v -m "not gtk" 2>&1 | tail -10

# Architecture + KB + new tests all pass
cd /path/to/projects/crabcakes && xvfb-run -a pytest tests/test_architecture.py tests/test_kb_lookup.py tests/test_auxilium_tier1.py -q 2>&1 | tail -5
```

---

## Report format (paste at the end)

1. **Files changed:** list with line numbers
2. **Discovery block:** what you read and what you learned (≤6 bullets)
3. **Verification output:** paste the 3 command outputs above verbatim
4. **Implementation choice rationale:** how you handled xvfb, GTK fixture strategy, stub handler pattern — one sentence each
5. **Related issues found:** anything adjacent you noticed but didn't fix (do NOT silently fix; report)
6. **COMPLETENESS:** checklist (see template below)

---

## Rules

- **Use the `steelFramedCodeWriter` prompt.** Apply every rule.
- **Read every file you touch completely** (Rule 1). The 6 files above are not optional.
- **7 tests, all named `test_*`.** Do not combine tests or skip any of the 7.
- **xvfb for GTK tests.** Verify the xvfb configuration before writing the tests.
- **No fabricated APIs.** If you call `handler.get_state()`, confirm it returns a `WizardState` from the Phase 1 handler.
- **Clean up GTK widgets.** Use `yield` in fixtures or explicit `cleanup()` calls.
- **No silent file overwrites.** Run `ls tests/test_auxilium_tier1.py` first — it shouldn't exist.

---

## COMPLETENESS template (paste at the end, fill in)

```
COMPLETENESS:
- [x] File created: tests/test_auxilium_tier1.py — <wc -l output>
- [x] Test 1 (handler imports) — name matches, assertion correct
- [x] Test 2 (view imports) — name matches, GTK instantiated
- [x] Test 3 (wizard needed, missing file) — name matches, assertion correct
- [x] Test 4 (wizard needed, with providers) — name matches, assertion correct
- [x] Test 5 (install check advances state) — name matches, assertion correct
- [x] Test 6 (advance to gateway) — name matches, thread + state correct
- [x] Test 7 (view current_step property) — name matches, property returns string
- [x] xvfb configuration handled — <describe how: pytest.ini marker, conftest.py fixture, or command prefix>
- [x] All 6 spec files read in full — <paste the read-line counts or 'ls' output>
- [x] Verification commands all run — <paste the 3 outputs>
- [x] Implementation choice rationale — <3 bullets, one sentence each>
- [x] Related issues found — <list or "none">
- [x] NOT DONE / DEFERRED: ARCHITECTURE update (Phase 5), final commit (Phase 6)
```

If you can't fill any item above with evidence, you are NOT done. The supervisor will reject the work.
