# TIER-1-D7-PHASE-2-INSTRUCTIONS — Auxilium Wizard View

**Phase:** 2 of 6 (D7 sub-phase 2 — view)
**Spec:** `docs/specs/SPEC-auxilium-tier-1.md` §D7
**Architecture:** `docs/ARCHITECTURE.md` §5 (Callback Pattern), §8.2 (Adding a New UI Component)
**Companion:** `ui/handlers/auxilium_wizard_handler.py` (Phase 1, completed)

---

## Goal

Write `ui/views/auxilium_wizard.py` — the GTK4 view widget for the Auxilium first-run wizard. Renders 3 step frames (install check, gateway check, provider picker), dispatches user actions to the handler, polls the handler for state changes during the gateway probe. ~150 lines per the spec.

**Architecture rule:** the view OWNS rendering. It does NOT do install checks, gateway probes, or config writes — those are the handler's job. The view only:
1. Renders the current `WizardState.step` (a frame, not a wizard)
2. Calls handler methods when the user clicks "Continue" or picks a provider
3. Polls `handler.get_state()` on a GTK timer to detect when the gateway probe finishes
4. Fires the callbacks (`on_install_check_complete`, `on_gateway_check_complete`, `on_provider_selected`) wired in `__init__`

The view should be a `Gtk.Box` (vertical), not a `Gtk.Window` — it embeds inside the Auxilium chat tab's existing scrolled window, replacing the welcome bubble.

---

## Files to read FIRST (mandatory)

Read these files completely before writing any code:

1. **`ui/handlers/auxilium_wizard_handler.py`** — the handler from Phase 1. **Re-read the COMPLETED version (387 lines)** so you know the current public API: `get_state()` returns a deep copy, `start()`, `advance_to_gateway()`, `advance_to_provider()`, `set_provider_choice(choice, provider, model, api_key)`.
2. **`ui/views/chat_bubble.py`** (lines 969-1001) — `build_welcome_bubble()` shows how a non-bubble widget is constructed as a `Gtk.Box` and added to a chat tab.
3. **`ui/views/chat_input_toolbar.py`** (lines 1-80) — the closest pattern: a `Gtk.Box`-based view that embeds in the chat area, has a handler reference, and uses callbacks. Pay attention to how it sets CSS classes and lays out children.
4. **`ui/views/agent_builder.py`** (lines 36-100) — the other view pattern: takes `handler` + callbacks in `__init__`, builds a `Gtk.Box` hierarchy with header + body + buttons. Focus on the `on_save`/`on_cancel` pattern, but note: agent_builder is a `Gtk.Window` (modal dialog). Our view is a `Gtk.Box` (embedded in a tab). Different parent.
5. **`docs/ARCHITECTURE.md` §5 (Callback Pattern)** — the contract for how view fires callbacks up to the parent (window.py in Phase 3).
6. **`docs/ARCHITECTURE.md` §8.2 (Adding a New UI Component)** — naming, file location, and the "no imports of other UI components" rule.
7. **`ui/views/main_content.py`** (lines 262-380) — `create_chat_tab()`. This is where the wizard view will be **inserted** in Phase 3. Note the structure: `chat_scroll.set_child(chat_box)` where `chat_box` is a `Gtk.Box(orientation=VERTICAL)`. The view will be appended to that `chat_box` instead of the welcome bubble, when the handler reports the user has no provider configured.

---

## Output: `ui/views/auxilium_wizard.py`

### Class structure (matches the spec §D7 verbatim)

```python
class AuxiliumWizard(Gtk.Box):
    """
    GTK4 view for the Auxilium first-run wizard.

    Embeds in the Auxilium chat tab (replaces the welcome bubble when
    the user has no provider configured). Renders 3 step frames,
    dispatches user actions to the handler, polls the handler for
    gateway probe completion.
    """
    def __init__(
        self,
        handler,                              # AuxiliumWizardHandler instance
        on_install_check_complete: Callable[[], None],
        on_gateway_check_complete: Callable[[], None],
        on_provider_selected: Callable[[], None],
    ):
        ...
```

### Step frames

3 frames, only one visible at a time. The view tracks which frame is shown and switches based on `handler.get_state().step`:

1. **INSTALL_CHECK frame** — shows the install check results (OK + green check, or list of missing deps). One "Continue" button. Fires `on_install_check_complete` → calls `handler.advance_to_gateway()`.
2. **GATEWAY_CHECK frame** — shows "Probing gateway..." spinner, then "OK" or "Failed: {error}" with a "Continue anyway" or "Retry" button. Uses a `GLib.timeout_add(250ms, ...)` to poll `handler.get_state().gateway_check` until `ok` or `error` is set. Fires `on_gateway_check_complete` → calls `handler.advance_to_provider()`.
3. **PROVIDER_PICK frame** — shows 3 radio buttons:
   - "OpenRouter free tier (no key, online)" → user clicks → opens a key input field (single-line `Gtk.Entry` for the API key)
   - "Ollama (local, free, offline)" → user clicks → no key field; uses `api_key="ollama"` placeholder
   - "Bring your own key" → opens a provider dropdown (OpenAI / Anthropic / Google) and a key input field

   One "Finish" button. Validates the form, then calls `handler.set_provider_choice(choice, provider, model, api_key)`. The handler's `on_complete` callback (wired in Phase 3) dismisses the wizard.

### Layout

```
┌─ AuxiliumFirstRunWizard ────────────────────┐
│  Step indicator: ● ─── ○ ─── ○              │
│                                              │
│  [Current frame's content here]              │
│                                              │
│  [Back]                          [Continue]  │
└──────────────────────────────────────────────┘
```

Use a vertical `Gtk.Box` with:
- Top: a small step indicator (`HBox` of 3 dots, with a CSS class for the "active" dot)
- Middle: a `Gtk.Stack` (NOT 3 separate widgets) — the stack is a GTK container that shows one of its children at a time. Add each frame as a named page; switch via `self._stack.set_visible_child_name("install_check" | "gateway_check" | "provider_pick")`.
- Bottom: a button bar (Back + Continue). Back is hidden on frame 1. Continue label changes per frame ("Continue" → "Continue" → "Finish").

### Polling model

When the gateway frame is shown, start a `GLib.timeout_add(250, self._poll_gateway)`. The poll function:
- Calls `handler.get_state()`
- If `gateway_check` is set (i.e., has `ok` or `error` filled in), stop the timer, update the frame, return `False` to stop the timeout
- Otherwise return `True` to keep the timer running

Critical: when the handler is constructed in Phase 3, the gateway probe may already have started (depending on init order). Always check `gateway_check` in `__init__` too — if it's already populated, show the result immediately.

### Step indicator

Three dots, CSS classes `step-dot` and `step-dot-active`. The active dot gets `step-dot-active`. Dots for past steps get `step-dot-done`. Don't over-engineer; simple labels are fine.

### CSS classes (defined in `ui/styles.py` or inline)

The view should add the following CSS classes so the styles.py can style them later:
- `auxilium-wizard` (root container)
- `auxilium-wizard-frame` (each step frame)
- `auxilium-wizard-step-dot` (each dot in the indicator)
- `auxilium-wizard-step-dot-active` (active dot)
- `auxilium-wizard-step-dot-done` (completed dot)

**You don't need to add CSS rules to styles.py in this phase.** Just add the class names. The styles can come in a follow-up.

### Constraints (ARCHITECTURE.md)

- **No business logic** in the view. No `sys.platform` checks, no `importlib.util.find_spec`, no WebSocket calls. All of that is in the handler.
- **No imports of other UI components.** Do NOT import any other `ui/views/*` or `ui/handlers/*` (other than the handler you receive in `__init__`).
- **No direct manipulation of `agent_runtime_handler` or any global state.** The view communicates only via the callbacks wired in `__init__`.
- The view should be safe to destroy mid-gateway-probe. The handler's gateway thread is a daemon; it will die with the process. The `GLib.timeout_add` returns a source id; store it and call `GLib.source_remove(source_id)` in a destructor or in the final "dismiss" path to avoid timer leaks.

### Public API

The class has just `__init__`. Everything else is internal (`_poll_gateway`, `_on_continue_clicked`, `_on_provider_choice_changed`, etc.). The view exposes a property `current_step` that returns the current step name — useful for the window.py wiring in Phase 3.

### Tests

**Do NOT write tests in this phase.** Tests are in Phase 4. The view requires a running GTK main loop or extensive mocking, so unit tests are deferred. Phase 4 will cover the view's behavior with `xvfb-run` integration tests.

---

## Verification commands (run and paste output)

```bash
# 1. Does the module import?
cd /path/to/projects/crabcakes && python3 -c "
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
from ui.views.auxilium_wizard import AuxiliumWizard
print('imports OK')
" 2>&1

# 2. Does the class instantiate (with stub handler)?
cd /path/to/projects/crabcakes && python3 -c "
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
from ui.views.auxilium_wizard import AuxiliumWizard

class StubHandler:
    def __init__(self):
        self.calls = []
        from ui.handlers.auxilium_wizard_handler import WizardStep, WizardState
        self._state = WizardState()
    def get_state(self):
        return self._state
    def start(self): self.calls.append('start')
    def advance_to_gateway(self): self.calls.append('advance_to_gateway')
    def advance_to_provider(self): self.calls.append('advance_to_provider')
    def set_provider_choice(self, c, p, m, k): self.calls.append(('set', c, p, m, k))

h = StubHandler()
calls = {'install': 0, 'gateway': 0, 'provider': 0}
w = AuxiliumWizard(
    handler=h,
    on_install_check_complete=lambda: (calls.__setitem__('install', calls['install']+1), h.advance_to_gateway()),
    on_gateway_check_complete=lambda: (calls.__setitem__('gateway', calls['gateway']+1), h.advance_to_provider()),
    on_provider_selected=lambda: calls.__setitem__('provider', calls['provider']+1),
)
print(f'wizard created: {type(w).__name__}, current_step={w.current_step}')
assert isinstance(w, Gtk.Box)
print('PASS')
" 2>&1

# 3. Existing tests still pass (architecture + KB)
cd /path/to/projects/crabcakes && pytest tests/test_architecture.py tests/test_kb_lookup.py -q 2>&1 | tail -3

# 4. G_DEBUG=fatal-criticals smoke — does the wizard instantiate without GTK warnings?
cd /path/to/projects/crabcakes && G_DEBUG=fatal-criticals python3 -c "
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
from ui.views.auxilium_wizard import AuxiliumWizard
from ui.handlers.auxilium_wizard_handler import WizardStep, WizardState

class StubHandler:
    def __init__(self): self._state = WizardState()
    def get_state(self): return self._state
    def start(self): pass
    def advance_to_gateway(self): pass
    def advance_to_provider(self): pass
    def set_provider_choice(self, c, p, m, k): pass

w = AuxiliumWizard(
    handler=StubHandler(),
    on_install_check_complete=lambda: None,
    on_gateway_check_complete=lambda: None,
    on_provider_selected=lambda: None,
)
print('smoke clean: no GTK-CRITICAL warnings')
" 2>&1
```

---

## Report format (paste at the end)

1. **Files changed:** list with line numbers
2. **Discovery block:** what you read and what you learned (≤8 bullets)
3. **Class structure description:** one paragraph on layout choices
4. **Verification output:** paste the 4 command outputs above verbatim
5. **Implementation choice rationale:** any non-obvious decisions (Gtk.Stack vs 3 widgets, polling frequency, where to call GLib.source_remove, etc.) — one sentence each
6. **Related issues found:** anything adjacent you noticed but didn't fix (do NOT silently fix; report)
7. **COMPLETENESS:** checklist (see template below)

---

## Rules

- **Use the `steelFramedCodeWriter` prompt** at `prompts/steelFramedCodeWriter.md`. Apply every rule.
- **Read every file you touch completely** (Rule 1). The 7 files above are not optional.
- **Hard part first** (Rule 2). Implement the polling logic + Gtk.Stack switching before the provider picker form.
- **Verify every claim** (Rule 3). If you write `set_visible_child_name("install_check")`, run the smoke command above and confirm the property exists.
- **No business logic in the view.** (ARCHITECTURE.md §5, §8.2)
- **No imports of other UI components.** (ARCHITECTURE.md §2)
- **No fabricated APIs.** If you call `handler.set_provider_choice(...)`, the handler's signature must match what you pass.
- **No silent file overwrites.** Run `ls ui/views/auxilium_wizard.py` first — it shouldn't exist.

---

## COMPLETENESS template (paste at the end, fill in)

```
COMPLETENESS:
- [x] File created: ui/views/auxilium_wizard.py — <wc -l output>
- [x] Class AuxiliumWizard(Gtk.Box) — <grep output>
- [x] __init__(handler, on_install_check_complete, on_gateway_check_complete, on_provider_selected) — <paste signature>
- [x] Step 1 (install check) frame renders — <paste snippet>
- [x] Step 2 (gateway check) frame renders — <paste snippet>
- [x] Step 3 (provider pick) frame renders with 3 radio buttons — <paste snippet>
- [x] Gtk.Stack used to switch between frames — <grep "Gtk.Stack" output>
- [x] GLib.timeout_add(250, self._poll_gateway) for gateway polling — <paste snippet>
- [x] GLib.source_remove() in destructor / dismiss path — <paste snippet>
- [x] CSS classes added: auxilium-wizard, auxilium-wizard-frame, auxilium-wizard-step-dot, etc. — <grep output>
- [x] No business logic (no sys.platform, no importlib, no websockets) — <grep -c output = 0>
- [x] No imports of other UI components — <grep -c "from ui\." output = 0 (only handler allowed)>
- [x] All 7 spec files read in full — <paste read-line counts or 'ls' output>
- [x] Verification commands all run — <paste the 4 outputs>
- [x] Implementation choice rationale — <3-5 bullets, one sentence each>
- [x] Related issues found — <list or "none">
- [x] NOT DONE / DEFERRED: tests (Phase 4), wiring (Phase 3), styles.py (follow-up)
```

If you can't fill any item above with evidence, you are NOT done. The supervisor will reject the work.
