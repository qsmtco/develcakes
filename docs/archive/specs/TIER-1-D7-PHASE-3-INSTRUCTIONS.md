# TIER-1-D7-PHASE-3-INSTRUCTIONS — Wire Wizard into Window

**Phase:** 3 of 6 (D7 sub-phase 3 — wiring)
**Spec:** `docs/specs/SPEC-auxilium-tier-1.md` §D7 (wiring section)
**Architecture:** `docs/ARCHITECTURE.md` §5 (Callback Pattern), §8.4 (Window composition)

---

## Goal

Wire the AuxiliumWizardHandler (Phase 1) and AuxiliumWizard (Phase 2) into `ui/window.py` so the wizard appears automatically when the Auxilium tab opens for a user who has not yet configured a provider. On `on_complete`, dismiss the wizard and show the normal Auxilium chat.

**Sub-phasing:** Integration is high-risk. This is sub-phased into 3 sub-phases:

- **Phase 3a** — Helper: `is_auxilium_wizard_needed(config_dir) -> bool` (pure function)
- **Phase 3b** — Wire the wizard into the Auxilium tab in `window.py` (creation + injection into chat_box)
- **Phase 3c** — Wire the dismissal flow (on_complete → reload config → remove wizard from chat_box → show welcome)

If the spec/architecture allows Phase 3a, 3b, 3c to be combined cleanly, do them in one file change. If the integration starts to feel risky, STOP and ask the supervisor to split.

---

## Files to read FIRST (mandatory)

Read these files completely before writing any code:

1. **`ui/window.py` lines 1-200** — `MainWindow.__init__`. Understand the existing wiring pattern for `MainContent`, `_agent_runtime_handler`, `_main_content.create_chat_tab()`.
2. **`ui/window.py` lines 160-200** — the auto-open agents loop. This is where Auxilium's tab gets created. The wizard injection will happen *after* this loop, on the Auxilium tab specifically.
3. **`ui/window.py` lines 280-360** — `set_on_buffer_changed` / `set_on_project_tab_closed` patterns. These show how `MainContent` exposes setters for callbacks, and how `MainWindow` wires them. The wizard wiring follows the same shape.
4. **`ui/views/main_content.py` lines 262-380** — `create_chat_tab()`. The wizard will be appended to the chat_box of the Auxilium tab after the welcome bubble.
5. **`ui/views/main_content.py` lines 698-720** — `get_chat_box_for_session()`. The wizard wiring needs to find the Auxilium tab's chat_box to inject the wizard into.
6. **`utils/agent_defs.py` lines 537-560** — `get_available_providers()`. This returns `[]` for first-run state, which is the "wizard needed" signal.
7. **`ui/handlers/auxilium_wizard_handler.py` lines 60-180** — re-read the handler's public API and callback signatures.
8. **`ui/views/auxilium_wizard.py` lines 95-110, 432-439** — the wizard's `__init__` signature and `cleanup()` method.
9. **`ui/handlers/agent_runtime_handler.py` lines 1-80** — how `agent_runtime_handler` is constructed and how `reload_agents_and_mcp()` works (you'll call this on wizard completion to pick up the new provider).

---

## Output

### 3a. Helper function (where?)

Add a single helper function. Pick one location based on what makes architectural sense:

**Recommended location:** `ui/handlers/auxilium_wizard_handler.py` — add a module-level function:

```python
def is_auxilium_wizard_needed(config_dir: Path) -> bool:
    """
    Return True if the user has not yet configured a provider
    and the Auxilium first-run wizard should be shown.
    
    'Not yet configured' = providers.yaml is missing or empty,
    OR contains only Ollama placeholders (still first-run-ish — 
    defer this case to a follow-up; just check empty for now).
    """
    try:
        from utils.providers_store import load_providers
        providers = load_providers()
        return len(providers) == 0
    except Exception:
        return True  # If we can't read, assume first-run
```

Rationale: it sits next to the handler that uses it. No new module.

### 3b. Wire the wizard into the Auxilium tab

In `ui/window.py`, after the auto-open agents loop (around line 188, after the `for agent_def in auto_open_agents` block), add a new block that:

1. Checks `is_auxilium_wizard_needed(config_dir)` — if False, skip
2. Builds the AuxiliumWizardHandler
3. Builds the AuxiliumWizard view
4. Looks up the Auxilium tab's chat_box via `self._main_content.get_chat_box_for_session("special:helper")`
5. Appends the wizard to that chat_box (after the welcome bubble, if present)
6. Stores `self._auxilium_wizard` and `self._auxilium_wizard_handler` so they don't get GC'd

```python
# After the auto-open loop:
try:
    from utils.config import get_config_dir
    config_dir = get_config_dir()
except Exception:
    config_dir = Path.home() / ".config" / "crabcakes"

if is_auxilium_wizard_needed(config_dir):
    chat_box = self._main_content.get_chat_box_for_session("special:helper")
    if chat_box is not None:
        # Build handler
        self._auxilium_wizard_handler = AuxiliumWizardHandler(
            config_dir=config_dir,
            on_complete=lambda: self._on_auxilium_wizard_complete(),
            on_error=lambda msg: logger.error("Auxilium wizard error: %s", msg),
        )
        # Build view
        self._auxilium_wizard = AuxiliumWizard(
            handler=self._auxilium_wizard_handler,
            on_install_check_complete=lambda: self._auxilium_wizard_handler.advance_to_gateway(),
            on_gateway_check_complete=lambda: self._auxilium_wizard_handler.advance_to_provider(),
            on_provider_selected=lambda: None,  # The handler fires on_complete internally
        )
        chat_box.append(self._auxilium_wizard)
```

Note: `is_auxilium_wizard_needed` returns True on missing config or read error. We assume the first-run case.

### 3c. Wire the dismissal flow

Add a method `_on_auxilium_wizard_complete` to `MainWindow`:

```python
def _on_auxilium_wizard_complete(self) -> None:
    """Called when the Auxilium first-run wizard finishes successfully."""
    logger.info("Auxilium wizard complete — reloading agent config")
    
    # Remove wizard from chat_box
    if hasattr(self, "_auxilium_wizard") and self._auxilium_wizard is not None:
        self._auxilium_wizard.cleanup()
        chat_box = self._main_content.get_chat_box_for_session("special:helper")
        if chat_box is not None:
            chat_box.remove(self._auxilium_wizard)
        self._auxilium_wizard = None
    if hasattr(self, "_auxilium_wizard_handler"):
        self._auxilium_wizard_handler = None
    
    # Reload agent config so the new provider is picked up
    try:
        self._agent_runtime_handler.reload_agents_and_mcp(
            on_complete=lambda: logger.info("Agents reloaded after Auxilium wizard")
        )
    except Exception as e:
        logger.exception("Failed to reload agents after Auxilium wizard: %s", e)
```

Add `from pathlib import Path` if not already imported in window.py.

**Critical: verify the chat_box.remove() method exists.** The view is a `Gtk.Box`; chat_box is also a `Gtk.Box` (the chat tab's content area). `Gtk.Box.remove(child)` is GTK4 API. If the spec has used a different removal pattern elsewhere (e.g., `widget.unparent()`), match that. Run `grep -n "chat_box.remove\|child.unparent" ui/views/main_content.py` to confirm.

---

## Constraints (ARCHITECTURE.md)

- **No new top-level imports of `ui/views/auxilium_wizard.py`** at module-level in window.py. Import inside the function (per the existing pattern of `from ui.handlers.agent_builder_handler import AgentBuilderHandler` inside `__init__`).
- **No business logic in the wiring.** The handler is the only place that does install checks, gateway probes, and config writes.
- **No new state machine code.** Reuse the existing `AuxiliumWizardHandler` API.
- **On error, do not crash the app.** Wrap the wizard wiring in `try/except`; on failure, log and continue (the user just won't see the wizard, and the existing chat experience is unaffected).

---

## Verification commands (run and paste output)

```bash
# 1. Does the helper function exist?
cd /path/to/projects/crabcakes && python3 -c "from ui.handlers.auxilium_wizard_handler import is_auxilium_wizard_needed; from pathlib import Path; print('wizard_needed =', is_auxilium_wizard_needed(Path.home() / '.config' / 'crabcakes'))"

# 2. Existing tests still pass
cd /path/to/projects/crabcakes && pytest tests/test_architecture.py tests/test_kb_lookup.py -q 2>&1 | tail -3

# 3. Module imports
cd /path/to/projects/crabcakes && python3 -c "import ui.window; print('window imports OK')"

# 4. G_DEBUG=fatal-criticals smoke — the app launches without GTK warnings
# Note: this requires xvfb-run for headless mode
cd /path/to/projects/crabcakes && xvfb-run -a G_DEBUG=fatal-criticals python3 -c "
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
import ui.window
# Don't actually open the window in this test — just check imports + class definition
print('AuxiliumWizard importable from window.py import chain: OK')
"

# 5. Static check: chat_box has a remove method
cd /path/to/projects/crabcakes && python3 -c "
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
b = Gtk.Box()
print('Gtk.Box has remove method:', hasattr(b, 'remove'))
"
```

---

## Report format (paste at the end)

1. **Files changed:** list with line numbers
2. **Discovery block:** what you read and what you learned (≤8 bullets)
3. **Sub-phase results:** what was wired in 3a / 3b / 3c
4. **Verification output:** paste the 5 command outputs above verbatim
5. **Implementation choice rationale:** any non-obvious decisions — one sentence each
6. **Related issues found:** anything adjacent you noticed but didn't fix (do NOT silently fix; report)
7. **COMPLETENESS:** checklist (see template below)

---

## Rules

- **Use the `steelFramedCodeWriter` prompt.** Apply every rule.
- **Read every file you touch completely** (Rule 1). The 9 files above are not optional.
- **Hard part first** (Rule 2). Implement 3a (the helper) first. Then 3b. Then 3c. The order matters because 3b depends on 3a, and 3c depends on 3b's reference to `self._auxilium_wizard`.
- **Verify every claim** (Rule 3). If you write `chat_box.remove(self._auxilium_wizard)`, run `python3 -c "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk; print(hasattr(Gtk.Box(), 'remove'))"` to confirm.
- **No silent file overwrites.** Run `ls` on each target path first.
- **Don't refactor existing wiring** (Rule 8 — do not modify what you were not asked to modify). The auto-open agents loop is unchanged; you add a new block AFTER it.
- **No new top-level imports.** `from ui.views.auxilium_wizard import AuxiliumWizard` goes inside the function, not at the top of `ui/window.py`.

---

## COMPLETENESS template (paste at the end, fill in)

```
COMPLETENESS:
- [x] Phase 3a: helper is_auxilium_wizard_needed(config_dir) added — <paste the function + wc -l>
- [x] Phase 3b: wizard wired into Auxilium tab in window.py — <paste the new block with line numbers>
- [x] Phase 3c: _on_auxilium_wizard_complete() dismisses wizard + reloads agents — <paste the method>
- [x] chat_box.remove() exists and is used correctly — <paste command 5 output>
- [x] No new top-level imports in window.py — <grep "from ui.views.auxilium_wizard" window.py output = 0 at top>
- [x] Module imports cleanly — <paste command 3 output>
- [x] Existing tests still pass — <paste command 2 output>
- [x] G_DEBUG=fatal-criticals smoke — <paste command 4 output>
- [x] All 9 spec files read in full — <paste the read-line counts or 'ls' output>
- [x] Implementation choice rationale — <3-5 bullets, one sentence each>
- [x] Related issues found — <list or "none">
- [x] NOT DONE / DEFERRED: tests (Phase 4), ARCHITECTURE update (Phase 5), final commit (Phase 6)
```

If you can't fill any item above with evidence, you are NOT done. The supervisor will reject the work.
