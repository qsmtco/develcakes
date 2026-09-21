# TIER-1-D7-PHASE-1-INSTRUCTIONS — Auxilium Wizard Handler

**Phase:** 1 of 4 (D7 sub-phase 1 — handler)
**Spec:** `docs/specs/SPEC-auxilium-tier-1.md` §D7
**Architecture:** `docs/ARCHITECTURE.md` §3, §8.2, §8.4, §8.5

---

## Goal

Write `ui/handlers/auxilium_wizard_handler.py` — the business-logic, GTK-free state machine for the Auxilium first-run wizard. This is the risk-bearing piece (install detection, gateway probe, provider config write). Get this right; the view in Phase 2 is just a thin GTK4 shell over this.

---

## Files to read FIRST (mandatory)

Read these files completely before writing any code:

1. **`docs/ARCHITECTURE.md`** — focus on §2 (Directory Structure), §3 (Module Architecture), §5 (Callback Pattern), §8.2 (Adding a New UI Component), §8.5 (Testing)
2. **`docs/specs/SPEC-auxilium-tier-1.md`** — read §D7 entirely (the deliverable you're implementing)
3. **`ui/handlers/agent_builder_handler.py`** — closest pattern: another handler with callbacks, no GTK imports. Read all 130 lines.
4. **`ui/views/agent_builder.py`** (lines 36-76) — how a view takes a `handler` in `__init__` and uses `on_save` / `on_cancel` callbacks
5. **`ui/handlers/agent_runtime_handler.py`** (lines 1-80) — see how handlers are wired in `MainWindow.__init__`; pattern for `__init__` signature
6. **`agent/config.py`** (lines 1-50, 144-200) — provider config structure; you will write a provider config to disk
7. **`utils/providers_store.py`** (lines 1-50, 114-180) — `load_providers()` and `save_providers()` are the canonical read/write for `providers.yaml`
8. **`prompts/default_agents/auxilium.yaml`** — the existing Auxilium agent definition; the wizard will rewrite this on provider selection

---

## Output: `ui/handlers/auxilium_wizard_handler.py`

### Class structure

```python
class AuxiliumWizardHandler:
    def __init__(
        self,
        config_dir: Path,                    # ~/.config/crabcakes/
        on_complete: Callable[[], None],     # fired when wizard finishes successfully
        on_error: Callable[[str], None],     # fired on error with user-facing message
        on_step_changed: Callable[[WizardState], None] | None = None,
    ): ...

    def get_state(self) -> WizardState: ...
    def start(self) -> None: ...
    def advance_to_gateway(self) -> None: ...
    def advance_to_provider(self) -> None: ...
    def set_provider_choice(
        self, choice: str, provider: str, model: str, api_key: str | None
    ) -> None: ...
```

### State machine states

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
    install_check: dict  # {ok, python, gtk4, platform, missing, warnings}
    gateway_check: dict  # {ok, url, error}
    provider_pick: dict  # {choice, provider, model, api_key}
```

### Method semantics

- **`start()`** — synchronously run install check (no I/O, just `sys.platform` + `importlib.util.find_spec('gi')`), set state, fire `on_step_changed`.
- **`advance_to_gateway()`** — start a background thread that probes the gateway WebSocket. When done, fire `on_step_changed`. Wire via `threading.Thread(daemon=True)`; do NOT block the GTK main loop.
- **`advance_to_provider()`** — sync; no I/O. Just set step.
- **`set_provider_choice(choice, provider, model, api_key)`** — synchronous config write:
  1. Validate `choice` is one of `{"openrouter_free", "ollama", "bring_your_own"}`
  2. If `choice == "ollama"`, set `api_key = "ollama"` (Ollama doesn't need a key)
  3. Use `utils.providers_store.save_providers([...])` to write a new `providers.yaml` with the chosen provider
  4. Update the in-memory auxilium agent by reloading the agent config
  5. Fire `on_complete` (NOT `on_step_changed` — this terminates the wizard)
  6. On exception, fire `on_error(message)` and stay on `PROVIDER_PICK`
- **`get_state()`** — return current state; used by the view to render.

### Install check details

Detect via:
- `sys.platform` → "linux" / "darwin" / "win32"
- `sys.version_info` → major.minor.micro as string
- `importlib.util.find_spec("gi")` → GTK4 / PyGObject presence
- `importlib.util.find_spec("cryptography")` → cryptography (optional, just a warning)
- `importlib.util.find_spec("websockets")` → websockets (required for gateway)

Return:
```python
{
    "ok": <bool — all required present>,
    "platform": "linux" | "darwin" | "win32",
    "python": "3.12.3",
    "gtk4": True,    # gi present
    "websockets": True,
    "missing": ["websockets"],   # empty if ok
    "warnings": [],   # optional deps
}
```

### Gateway check details

- Read gateway URL from `<config_dir>/agent.json` field `gateway_url` (default `ws://localhost:8765`)
- Open WebSocket with 3-second timeout
- On success: `{"ok": True, "url": url}`
- On timeout/connection refused: `{"ok": False, "url": url, "error": str}`
- Run in a background thread (do NOT block the wizard)
- When the thread finishes, fire `on_step_changed`

Implementation hint: use `websockets.sync.client.connect(url, open_timeout=3.0)` in a try/except.

### Config write details

When `set_provider_choice` runs, do this:
```python
from utils.providers_store import ProviderConfig, save_providers

# For OpenRouter free tier:
provider = ProviderConfig(
    name="openrouter",
    base_url="https://openrouter.ai/api/v1",
    default_model="openrouter/free",
    api_key=api_key,  # user-supplied; for free tier, can be "sk-or-v1-..."
    models=[("OpenRouter Free", "openrouter/free"), ...],
)

# For Ollama:
provider = ProviderConfig(
    name="ollama",
    base_url="http://localhost:11434/v1",
    default_model="llama3.2:7b",
    api_key="ollama",  # placeholder; Ollama doesn't enforce
    models=[("Llama 3.2 7B (local)", "llama3.2:7b"), ...],
)

# For "bring your own" (OpenAI / Anthropic / Google):
# Build the right ProviderConfig based on the `provider` argument.

save_providers([provider])
```

Read `utils/providers_store.py` to confirm the `ProviderConfig` dataclass fields. Do not invent fields.

### Constraints (ARCHITECTURE.md)

- **No imports from `ui/`, `gateway/`, or `subprocess`.**
- **No GTK at import time.** This handler is pure Python.
- **Threading only for the gateway probe.** All other methods are synchronous and run on the GTK main thread (they only do file I/O and short subprocess-free checks).
- The thread's callback `on_step_changed` must be marshaled back to the GTK main thread. **Recommended approach: avoid GLib coupling.** Use a `threading.Event` and let the view poll. The view will check `get_state()` after a short timer. This keeps the handler GTK-import-free at import time. (Phase 2 will implement the polling timer in the view.)

### Docstrings

Every public method gets a docstring. Use the spec's state machine as the reference.

### Tests

**Do NOT write tests in this phase.** Tests are in Phase 4. Just the handler.

---

## Verification commands (run and paste output)

```bash
# 1. Does the module import?
cd /path/to/projects/crabcakes && python3 -c "from ui.handlers.auxilium_wizard_handler import AuxiliumWizardHandler, WizardStep, WizardState; print('imports OK')"

# 2. Does install check work?
cd /path/to/projects/crabcakes && python3 -c "
from pathlib import Path
from ui.handlers.auxilium_wizard_handler import AuxiliumWizardHandler
calls = []
h = AuxiliumWizardHandler(
    config_dir=Path.home() / '.config' / 'crabcakes',
    on_complete=lambda: calls.append('complete'),
    on_error=lambda msg: calls.append(f'error: {msg}'),
)
h.start()
state = h.get_state()
print(f'step={state.step.value}')
print(f'platform={state.install_check[\"platform\"]}')
print(f'python={state.install_check[\"python\"]}')
print(f'gtk4={state.install_check[\"gtk4\"]}')
print(f'ok={state.install_check[\"ok\"]}')
print(f'calls={calls}')
"

# 3. Existing tests still pass
cd /path/to/projects/crabcakes && pytest tests/test_kb_lookup.py -q 2>&1 | tail -3
```

---

## Report format (paste at the end)

1. **Files changed:** list with line numbers
2. **Discovery block:** what you read and what you learned (≤8 bullets)
3. **Per-method description:** one sentence each
4. **Verification output:** paste the 3 command outputs above verbatim
5. **Implementation choice rationale:** any non-obvious decisions (threading model, GLib vs polling, how to handle Ollama's lack of key, etc.) — one sentence each
6. **Related issues found:** anything adjacent you noticed but didn't fix (do NOT silently fix; report)
7. **COMPLETENESS:** checklist (see template below)

---

## Rules

- **Use the `steelFramedCodeWriter` prompt** at `prompts/steelFramedCodeWriter.md` — read it if you haven't recently. Apply every rule.
- **Read every file you touch completely** (Rule 1). The 8 files above are not optional.
- **Hard part first** (Rule 2). Implement `set_provider_choice` (the config write) before the install check.
- **Verify every claim** (Rule 3). If you write `ProviderConfig(name=...)`, run `inspect.signature(ProviderConfig)` and paste the result.
- **Wire it up or delete it** (Rule 5). Every method on the handler must be called from somewhere. Phase 3 wires the view to this handler.
- **No GTK imports at top level.** (ARCHITECTURE.md §3, §2)
- **No imports from `ui/`, `gateway/`, or `subprocess`.**
- **No fabricated APIs.** If you call `save_providers(...)`, `grep -n "def save_providers" utils/providers_store.py` and confirm the signature.
- **No silent file overwrites.** Run `ls ui/handlers/auxilium_wizard_handler.py` first — it shouldn't exist.

---

## COMPLETENESS template (paste at the end, fill in)

```
COMPLETENESS:
- [x] File created: ui/handlers/auxilium_wizard_handler.py — <wc -l output>
- [x] Public API matches spec — <paste dir(AuxiliumWizardHandler) output>
- [x] State machine: 5 states (INSTALL_CHECK, GATEWAY_CHECK, PROVIDER_PICK, WRITING_CONFIG, DONE) — <grep output>
- [x] Install check: platform + python + gtk4 + websockets detection — <paste install-check output>
- [x] Gateway check: 3s timeout, background thread — <paste snippet>
- [x] set_provider_choice: validates, writes providers.yaml, fires on_complete — <paste snippet>
- [x] No GTK at module top-level — <grep -c 'gi.repository\|Gtk' file output = 0>
- [x] No ui/ imports — <grep -c 'from ui\.' file output = 0>
- [x] All 8 spec files read in full — <paste the read-line counts or 'ls' output>
- [x] Verification commands all run — <paste the 3 outputs>
- [x] Implementation choice rationale — <3-5 bullets, one sentence each>
- [x] Related issues found — <list or "none">
- [x] NOT DONE / DEFERRED: tests (Phase 4), view (Phase 2), wiring (Phase 3)
```

If you can't fill any item above with evidence, you are NOT done. The supervisor will reject the work.
