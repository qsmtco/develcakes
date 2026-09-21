# P10 Audit Fix — Phase 1: normalize context_mode + reject negative model_max_tokens

**Bugs fixed:** BUG #2 (HIGH), BUG #3 (MEDIUM)
**Files to change:** `agent/context.py`, `models/providers.py`
**Spec reference:** `docs/audits/2026-06-27-P10-ADVERSARIAL-AUDIT.md` BUG #2 and BUG #3

## BUG #2 — Case-sensitive context_mode validation (HIGH)

**Problem:** `resolve_context_mode()` and `build_file_context_with_core_files()` validate `context_mode` with case-sensitive `in` checks. A user writing `context_mode: AUTO` or `context_mode: Preload` in `providers.yaml` gets a `ValueError` that crashes every LLM call. The `validate_provider_context_mode()` helper in `models/providers.py` already normalizes via `.lower().strip()` but is never called by these functions.

**Fix:** Make `resolve_context_mode()` normalize input via `validate_provider_context_mode()` before the comparison logic.

### Edit 1: `agent/context.py` — `resolve_context_mode()` function

Current code (around line 357):
```python
def resolve_context_mode(
    explicit_mode: str,
    model_max_tokens: int | None,
) -> str:
    ...
    if explicit_mode in ("preload", "jit", "hybrid"):
        return explicit_mode
    if explicit_mode != "auto":
        raise ValueError(f"Invalid context_mode: {explicit_mode!r}")
    # auto: resolve by model context window size
    window = model_max_tokens or 128_000
```

Replace with:
```python
def resolve_context_mode(
    explicit_mode: str,
    model_max_tokens: int | None,
) -> str:
    """Resolve the effective context mode based on provider configuration.

    v1: resolves at conversation-creation time only, using model_max_tokens.
    Mid-session escalation (turn_count, token_estimate) is deferred to P10.8.

    Args:
        explicit_mode: One of "auto", "preload", "jit", "hybrid".
            Case-insensitive; whitespace is stripped. "auto" is resolved by
            this function.
        model_max_tokens: Model context window from ProviderConfig.
            If None, 0, or negative, defaults to 128_000 for heuristics.

    Returns:
        One of "preload", "hybrid", "jit".
    """
    # Normalize input via the shared validator (case-insensitive, strips whitespace)
    from models.providers import validate_provider_context_mode
    explicit_mode = validate_provider_context_mode(explicit_mode)

    if explicit_mode in ("preload", "jit", "hybrid"):
        return explicit_mode
    # explicit_mode is "auto" — resolve by model context window size
    if model_max_tokens is not None and model_max_tokens <= 0:
        # Negative/zero window → unknown, use balanced default
        return "hybrid"
    window = model_max_tokens or 128_000
```

### Edit 2: `agent/context.py` — `build_file_context_with_core_files()` mode validation

Current code (around line 558):
```python
    # Validate mode
    if context_mode not in ("preload", "jit", "hybrid"):
        raise ValueError(f"Invalid context_mode: {context_mode!r}")
```

Replace with:
```python
    # Validate mode (normalize via shared validator for case-insensitivity)
    from models.providers import validate_provider_context_mode
    context_mode = validate_provider_context_mode(context_mode)
    # After normalization, "auto" is not valid here — resolve first
    if context_mode == "auto":
        context_mode = resolve_context_mode("auto", None)
```

Wait — actually `build_file_context_with_core_files` receives an already-resolved mode from `compose_system_prompt`. The caller (`compose_system_prompt`) calls `resolve_context_mode()` first and passes the result. So `build_file_context_with_core_files` should just normalize for safety but not re-resolve. Replace with:

```python
    # Validate and normalize mode (case-insensitive)
    from models.providers import validate_provider_context_mode
    context_mode = validate_provider_context_mode(context_mode)
    if context_mode == "auto":
        # Should have been resolved by caller; treat as hybrid fallback
        context_mode = "hybrid"
```

## BUG #3 — resolve_context_mode accepts negative model_max_tokens (MEDIUM)

**Problem:** `model_max_tokens=-1` is truthy, so `window = model_max_tokens or 128_000` uses -1 as the window, which is `<= 32_000` → returns `"jit"`.

**Fix:** Already handled in the Edit 1 above — the `if model_max_tokens is not None and model_max_tokens <= 0: return "hybrid"` guard rejects negatives before the `or` fallback.

## Verification

After edits, run:
```bash
python3 -c "
from agent.context import resolve_context_mode
# BUG #2 tests
assert resolve_context_mode('AUTO', 128_000) == 'hybrid'
assert resolve_context_mode('Preload', 128_000) == 'preload'
assert resolve_context_mode(' jit ', 128_000) == 'jit'
print('BUG #2: PASS')

# BUG #3 tests
assert resolve_context_mode('auto', -1) == 'hybrid'
assert resolve_context_mode('auto', -1_000_000) == 'hybrid'
assert resolve_context_mode('auto', 0) == 'hybrid'
assert resolve_context_mode('auto', None) == 'hybrid'
print('BUG #3: PASS')
"
```

Then run existing tests:
```bash
python3 -m pytest tests/test_jit_context_discovery.py -v --tb=short
```

## COMPLETENESS checklist (required in builder response):

```
COMPLETENESS:
- [ ] Edit 1: resolve_context_mode normalizes via validate_provider_context_mode — evidence (paste the function)
- [ ] Edit 2: build_file_context_with_core_files normalizes mode — evidence (paste the changed lines)
- [ ] BUG #2 verification: paste output of the BUG #2 test script
- [ ] BUG #3 verification: paste output of the BUG #3 test script
- [ ] All 50 existing tests still pass — paste pytest output tail
```
