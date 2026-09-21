# TIER-1-D7-PHASE-3-FIX-1-INSTRUCTIONS — wizard-needed check ignores config_dir parameter

**Phase:** 3.5 of 6 (Phase 3 bug fix)
**Severity:** HIGH (the wizard will not appear for fresh-install users)
**Reference:** `ui/handlers/auxilium_wizard_handler.py` line 60 (`is_auxilium_wizard_needed`)

---

## Bug

`is_auxilium_wizard_needed(config_dir)` accepts a `config_dir: Path` parameter but the parameter is **never used**. Inside the function, `load_providers()` is called — and `load_providers()` calls `get_providers_path()`, which calls `get_config_dir()` (the real one), ignoring the parameter entirely.

**Audit probe that caught it:**
```python
with tempfile.TemporaryDirectory() as tmp:
    # tmp is a brand new dir with no providers.yaml
    result = is_auxilium_wizard_needed(Path(tmp))
    # Expected: True (wizard needed, no providers.yaml in tmp)
    # Actual:   False (load_providers() reads from real ~/.config/crabcakes/)
```

**Why this matters:** on a fresh install where the user has no provider configured, the wizard won't appear. The user opens the app, sees an empty Auxilium tab, and there's no wizard to guide them. The whole Tier 1 first-run promise is broken.

**Compounding issue:** even on the *real* machine today, providers.yaml exists from QTR's earlier Phase 1 test (Ollama placeholder). So the wizard won't appear here either, even though the user hasn't actually completed setup.

---

## Root cause

`load_providers()` is a singleton read — it uses module-level `get_config_dir()`, not a parameter. So `is_auxilium_wizard_needed(config_dir)` is asking the question against the wrong filesystem location.

Two possible fixes:

**Option A** — Check the file directly using the parameter:
```python
def is_auxilium_wizard_needed(config_dir: Path) -> bool:
    """Return True if the user has not yet configured a provider."""
    providers_yaml = config_dir / "providers.yaml"
    if not providers_yaml.is_file():
        return True
    # File exists — check if it has any real providers
    try:
        from utils.providers_store import load_providers
        providers = load_providers()
        return len(providers) == 0
    except Exception:
        return True
```

**Option B** — Read the file at the explicit path:
```python
def is_auxilium_wizard_needed(config_dir: Path) -> bool:
    """Return True if providers.yaml is missing or has no providers."""
    providers_yaml = config_dir / "providers.yaml"
    if not providers_yaml.is_file():
        return True
    try:
        text = providers_yaml.read_text(encoding="utf-8")
        # Empty file or comment-only = wizard needed
        return not text.strip() or text.strip().startswith("#")
    except OSError:
        return True
```

**Recommended: Option A.** It's the smallest change and reuses the existing parser. The reason to also keep the `load_providers() == []` check: a user might have a `providers.yaml` with just comments or a malformed line, in which case `load_providers()` returns `[]`. We should still show the wizard in that case.

---

## Files to read

- `ui/handlers/auxilium_wizard_handler.py` lines 60-77 (the helper to fix)
- `utils/providers_store.py` lines 25-35, 114-130 (confirm `get_providers_path()` uses `get_config_dir()`, not a parameter)

---

## Verification commands (run and paste output)

```bash
# 1. Re-run the failing probe — wizard_needed in an empty dir must be True
cd /path/to/projects/crabcakes && python3 -c "
import tempfile
from pathlib import Path
from ui.handlers.auxilium_wizard_handler import is_auxilium_wizard_needed

with tempfile.TemporaryDirectory() as tmp:
    result = is_auxilium_wizard_needed(Path(tmp))
    print(f'empty dir → wizard_needed = {result}')
    assert result is True, f'expected True for empty dir, got {result}'
    print('PASS: empty config dir → wizard needed')
"

# 2. Real config dir (currently has providers.yaml) — wizard not needed
cd /path/to/projects/crabcakes && python3 -c "
from pathlib import Path
from ui.handlers.auxilium_wizard_handler import is_auxilium_wizard_needed
result = is_auxilium_wizard_needed(Path.home() / '.config' / 'crabcakes')
print(f'real dir → wizard_needed = {result}')
print('  (expected False because providers.yaml exists from prior tests)')
"

# 3. Existing tests still pass
cd /path/to/projects/crabcakes && pytest tests/test_architecture.py tests/test_kb_lookup.py -q 2>&1 | tail -3

# 4. Module still imports cleanly
cd /path/to/projects/crabcakes && python3 -c "from ui.handlers.auxilium_wizard_handler import is_auxilium_wizard_needed; print('imports OK')"
```

---

## Rules

- **Use the steelFramedCodeWriter prompt.** Apply every rule.
- **One function, surgical fix.** Change only `is_auxilium_wizard_needed`.
- **Use Option A from the bug report** (the recommended approach).
- **Do NOT modify `utils/providers_store.py`.** The bug is in the helper, not in providers_store. Adding a parameter to `load_providers()` would be a larger change with broader implications.
- **Do NOT touch Phase 1 or 2 work.** The handler and view are correct; this is purely a Phase 3 wiring bug.
- **Do NOT touch the window.py wiring.** The wiring is correct; it just calls a buggy helper.

---

## COMPLETENESS template (paste at the end)

```
COMPLETENESS:
- [x] is_auxilium_wizard_needed now uses the config_dir parameter — <paste the new function>
- [x] Empty dir probe now returns True — <paste command 1 output>
- [x] Real dir returns the expected value (False, providers.yaml exists) — <paste command 2 output>
- [x] Existing tests still pass — <paste command 3 output>
- [x] Module imports cleanly — <paste command 4 output>
- [x] Diff is one function body — <paste git diff --stat output>
- [x] NOT DONE / DEFERRED: tests (Phase 4), ARCHITECTURE update (Phase 5), final commit (Phase 6)
```

Please write when ready. After this fix, the audit on this phase is complete and I will move to Phase 4 (tests).
