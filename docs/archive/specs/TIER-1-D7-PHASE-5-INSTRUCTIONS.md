# TIER-1-D7-PHASE-5-INSTRUCTIONS — Update ARCHITECTURE.md for Auxilium Wizard

**Phase:** 5 of 6 (D7 sub-phase 5 — docs)
**Spec:** `docs/specs/SPEC-auxilium-tier-1.md` §D7
**Architecture:** `docs/ARCHITECTURE.md` §3 (Module Responsibilities)

---

## Goal

Add two new sections to `docs/ARCHITECTURE.md` documenting the Auxilium wizard:

1. **§3.21q.6** — `ui/handlers/auxilium_wizard_handler.py` (handler from Phase 1)
2. **§3.21q.7** — `ui/views/auxilium_wizard.py` (view from Phase 2)

Insert them immediately after the existing §3.21q.5 (kb_lookup.py) section, following the same template as the KB lookup entry.

---

## Files to read FIRST (mandatory)

1. **`docs/ARCHITECTURE.md` lines 1337-1370** — the KB lookup section to use as a template
2. **`docs/ARCHITECTURE.md` lines 56-199** — §2 Directory Structure. The wizard files need to be added to the tree.
3. **`ui/handlers/auxilium_wizard_handler.py`** (Phase 1, 387 lines) — the handler's full public API
4. **`ui/views/auxilium_wizard.py`** (Phase 2, 439 lines) — the view's full public API
5. **`ui/window.py` lines 197-230, 731-755** — the wiring (Phase 3). May or may not need its own section; see "Optional: Window.py section" below.

---

## Output 1: Add to §2 Directory Structure

Find the line that documents `ui/handlers/` and `ui/views/` in the tree (around line 56-199). Add the new files to their respective subtrees. The exact insertion point will be near other `ui/handlers/*_handler.py` and `ui/views/*_view.py` entries.

Suggested text to add in the `ui/handlers/` subtree:
```
│   │   ├── auxilium_wizard_handler.py  # Auxilium first-run wizard handler (Tier 1, D7)
```

Suggested text to add in the `ui/views/` subtree:
```
│   │   ├── auxilium_wizard.py          # Auxilium first-run wizard view (Tier 1, D7)
```

(Look at the actual file to find the exact format and indentation; the tree in §2 uses box-drawing characters with specific alignment.)

---

## Output 2: Add §3.21q.6 (handler section)

Insert after line 1370 (the end of the KB lookup section), as a new section:

```markdown
### 3.21q.6 `ui/handlers/auxilium_wizard_handler.py` — Auxilium First-Run Wizard Handler (Tier 1, D7)

**Responsibility:** Business logic for the Auxilium first-run wizard. Owns the install check (Python + GTK4 + websockets detection), the gateway WebSocket probe (3-second timeout, background thread), and the provider config write (via `utils.providers_store.save_providers()`). Does NOT touch GTK; the view polls `get_state()` for state changes.

**Public API:**
```python
class WizardStep(str, Enum):
    INSTALL_CHECK = "install_check"
    GATEWAY_CHECK = "gateway_check"
    PROVIDER_PICK = "provider_pick"
    WRITING_CONFIG = "writing_config"
    DONE = "done"

@dataclass
class WizardState:
    step: WizardStep
    install_check: dict  # {ok, platform, python, gtk4, websockets, missing, warnings}
    gateway_check: dict  # {ok, url, error}
    provider_pick: dict  # {choice, provider, model, api_key}

def is_auxilium_wizard_needed(config_dir: Path) -> bool
    # True if providers.yaml is missing or has no providers.

class AuxiliumWizardHandler:
    def __init__(config_dir, on_complete, on_error, on_step_changed=None)
    def get_state() -> WizardState               # deep copy — caller cannot mutate internal state
    def start() -> None                          # synchronous install check
    def advance_to_gateway() -> None             # spawns daemon thread to probe WebSocket
    def advance_to_provider() -> None            # sync; no I/O
    def set_provider_choice(choice, provider, model, api_key) -> None
                                                # writes providers.yaml; fires on_complete or on_error
```

**State machine:** 5 states (INSTALL_CHECK → GATEWAY_CHECK → PROVIDER_PICK → WRITING_CONFIG → DONE). Linear flow; no backward transitions. The `on_step_changed` callback fires on each transition.

**Architectural constraints:**
- No `ui/` imports. No `gateway/` imports. No `subprocess`. No GTK at import time.
- Threading only for the gateway probe; all other methods are synchronous (file I/O + dict ops).
- The handler does not call back into the view directly — the view polls `get_state()` on a `GLib.timeout_add` loop.
- `get_state()` returns a deep copy (`copy.deepcopy`) so view mutations cannot corrupt the handler.
- `set_provider_choice` validates the choice (one of `"openrouter_free"`, `"ollama"`, `"bring_your_own"`), normalizes Ollama's empty key to `"ollama"`, and writes `providers.yaml` atomically via `utils.providers_store.save_providers()`.
- On error, `set_provider_choice` fires `on_error(message)` and stays on `PROVIDER_PICK` so the user can retry.

**Provider config writes:** For `"openrouter_free"`, builds a `ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1", default_model="openrouter/free", api_key=user-supplied)`. For `"ollama"`, `ProviderConfig(name="ollama", base_url="http://localhost:11434/v1", default_model="llama3.2:7b", api_key="ollama")`. For `"bring_your_own"`, the `provider` argument drives the `name` field and `base_url` is synthesized from a lookup table (OpenAI, Anthropic, Google, MiniMax, ZAI).
```

---

## Output 3: Add §3.21q.7 (view section)

Insert immediately after §3.21q.6, as another new section:

```markdown
### 3.21q.7 `ui/views/auxilium_wizard.py` — Auxilium First-Run Wizard View (Tier 1, D7)

**Responsibility:** GTK4 view widget for the Auxilium wizard. Renders 3 step frames (install check, gateway check, provider picker) in a `Gtk.Stack`, dispatches user actions to the handler, polls the handler for gateway probe completion. Embeds in the Auxilium chat tab (replaces the welcome bubble) when the user has no provider configured.

**Public API:**
```python
class AuxiliumWizard(Gtk.Box):
    def __init__(
        handler,                                # AuxiliumWizardHandler
        on_install_check_complete,              # fires on Continue click in install frame
        on_gateway_check_complete,              # fires on Continue click in gateway frame
        on_provider_selected,                   # fires on Finish click in provider frame
    )
    @property
    def current_step: str                       # 'install_check' | 'gateway_check' | 'provider_pick'

    def cleanup() -> None                       # removes GLib poll timer; call before destroy
```

**Layout:**
- Vertical `Gtk.Box` with 3 zones: step indicator (3 dots, top), `Gtk.Stack` with 3 named pages, button bar (Back + Continue, bottom).
- Stack page names match `WizardStep` values: `install_check`, `gateway_check`, `provider_pick`.
- Provider frame uses 3 `Gtk.CheckButton` radio buttons (grouped via `set_group()`) for the 3 choices, with a conditional `Gtk.Entry` for the API key (hidden for Ollama) and a `Gtk.DropDown` for the bring-your-own-key provider list.

**Polling model:** When the gateway frame is shown, `GLib.timeout_add(250, self._poll_gateway)` polls `handler.get_state().gateway_check` until the probe completes. The poll function returns `False` to stop the timer once a result is set.

**Architectural constraints:**
- No business logic in the view. No `sys.platform` checks, no `importlib.util.find_spec`, no WebSocket calls — all of that is in the handler.
- No imports of other `ui/views/*` or `ui/handlers/*` modules (except the handler received in `__init__`).
- No direct manipulation of `agent_runtime_handler` or any global state.
- The view must call `cleanup()` before destruction to remove the GLib timer source and avoid leaks.

**CSS classes** (defined in `ui/styles.py`): `auxilium-wizard` (root), `auxilium-wizard-frame` (each frame), `auxilium-wizard-step-dot` / `-active` / `-done` (step indicator dots), `auxilium-wizard-title` (frame titles).
```

---

## Optional: Window.py wiring section

The wiring in `ui/window.py` (lines 197-230, 731-755) is small and self-explanatory; a separate ARCHITECTURE.md section is not required. If you have time, add a short note to the existing `### 3.6 ui/window.py — Main Window` section (around line 394) describing when the wizard is shown. Otherwise, skip this and the supervisor will accept the §2 + §3.21q.6 + §3.21q.7 changes as the docs deliverable.

---

## Verification commands (run and paste output)

```bash
# 1. New sections exist
cd /path/to/projects/crabcakes && grep -nE "### 3\.21q\.[67]" docs/ARCHITECTURE.md

# 2. §2 tree updated
cd /path/to/projects/crabcakes && grep -nE "auxilium_wizard" docs/ARCHITECTURE.md | head -5

# 3. ARCHITECTURE.md still parses as markdown (no broken headings, no orphaned brackets)
cd /path/to/projects/crabcakes && python3 -c "
content = open('docs/ARCHITECTURE.md').read()
lines = content.splitlines()
import re
heading_re = re.compile(r'^(#+)\s')
broken = []
for i, line in enumerate(lines, 1):
    if line.startswith('```') and not line.startswith('```python') and 'image' not in line and not line.strip().startswith('```'):
        # Just check for unclosed code blocks
        pass
print(f'Total lines: {len(lines)}')
print(f'Headings: {len([l for l in lines if heading_re.match(l)])}')
print(f'Code fences: {content.count(chr(96)*3)} (should be even)')
if content.count(chr(96)*3) % 2 != 0:
    print('WARNING: odd number of code fences — possible unclosed block')
print('PASS')
"

# 4. Existing tests still pass (no code changes here, but verify)
cd /path/to/projects/crabcakes && pytest tests/test_architecture.py tests/test_kb_lookup.py -q 2>&1 | tail -3
```

---

## Report format (paste at the end)

1. **Files changed:** docs/ARCHITECTURE.md with line numbers
2. **Discovery block:** what you read and what you learned (≤4 bullets)
3. **Sections added:** list the new sections
4. **Verification output:** paste the 4 command outputs above verbatim
5. **Implementation choice rationale:** any deviations from the spec (one sentence each)
6. **Related issues found:** anything adjacent you noticed but didn't fix
7. **COMPLETENESS:** checklist (see template below)

---

## Rules

- **Use the steelFramedCodeWriter prompt.** Apply every rule.
- **Markdown is content-only.** No broken headings, no orphan code fences.
- **Match the existing tone.** Read the KB lookup section to see how previous Phase 1/2 docs were written. Mirror the style.
- **No fabricated APIs.** If you document `AuxiliumWizard.cleanup()`, confirm it exists in the Phase 2 file.
- **No silent file overwrites.** Run `git status docs/ARCHITECTURE.md` first — it should be unmodified from main.
- **Don't touch existing sections.** Insert new sections; don't modify what's already there.

---

## COMPLETENESS template (paste at the end, fill in)

```
COMPLETENESS:
- [x] §2 directory tree updated with auxilium_wizard_handler.py and auxilium_wizard.py — <grep output>
- [x] §3.21q.6 added: handler section — <paste section heading>
- [x] §3.21q.7 added: view section — <paste section heading>
- [x] All public APIs documented match the actual code — <grep verification>
- [x] No broken markdown (headings balanced, code fences even) — <paste command 3 output>
- [x] Existing tests still pass — <paste command 4 output>
- [x] No fabricated APIs — <paste grep of actual public methods>
- [x] All 5 spec files read in full — <paste the read-line counts or 'ls' output>
- [x] Implementation choice rationale — <3 bullets, one sentence each>
- [x] Related issues found — <list or "none">
- [x] NOT DONE / DEFERRED: final commit (Phase 6)
```

Please write when ready. After this, the audit on this phase is complete and I will move to Phase 6 (final commit + post-mortem with code quality breakdown + evolution suggestions).
