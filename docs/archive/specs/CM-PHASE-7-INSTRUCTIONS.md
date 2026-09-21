# Phase 7: P7 Dynamic Prompt Budget + `except Exception: pass` Polish

**Spec:** `docs/specs/SPEC-CONTEXT-MANAGEMENT-ROADMAP.md` §2.4 (P7 dynamic budget), §2.2.1 (exception handling polish)
**Goal:** Replace the static 15% system prompt budget fraction with a dynamic fraction that grows when templates are large, and replace the `except Exception: pass` in `_compute_compaction_threshold()` with proper `logger.debug(...)` logging.
**Files to change:**
1. `utils/prompt_loader.py` — P7 dynamic budget fraction in `_apply_system_prompt_budget()`
2. `agent/runtime.py` — replace `except Exception: pass` with `except Exception as e: logger.debug(...)` in `_compute_compaction_threshold()`
3. `tests/test_context_strategy.py` — add `TestDynamicPromptBudget` test class (testing the budget arithmetic)

**SCOPE:** This phase implements P7 ONLY plus the polish item carried since Phase 3. Do NOT implement:
- Phase 8/9 items (deferred to PR)

---

## Step 1: P7 Dynamic Budget Fraction in `utils/prompt_loader.py`

Replace the static budget computation inside `_apply_system_prompt_budget()`:

**Current code (lines 391-395):**
```python
    if model_max_tokens is not None and model_max_tokens > 0:
        budget_tokens = int(model_max_tokens * SYSTEM_PROMPT_BUDGET_FRACTION)
        budget_chars = budget_tokens * 4
    else:
        budget_chars = DEFAULT_SYSTEM_PROMPT_BUDGET_CHARS
```

**New code (P7 dynamic fraction):**
```python
    if model_max_tokens is not None and model_max_tokens > 0:
        # P7: Dynamic budget fraction.
        # Goal: ensure (templates + file_context) fits in ≤ 25% of the context window.
        # Floor: 15% (backward-compatible default from SYSTEM_PROMPT_BUDGET_FRACTION).
        # Ceiling: 25% (system prompt budget never exceeds 25% of context).
        # Behavior:
        #   - template_fraction <= 0.15 → budget stays at 15% (no growth for small
        #     templates; preserves backward-compatible behavior).
        #   - template_fraction > 0.15 → budget expands to fit the templates plus
        #     some file_context (budget_fraction = template_fraction, capped at 0.25).
        template_tokens = len(template_result) // 4
        template_fraction = template_tokens / model_max_tokens
        budget_fraction = min(0.25, max(SYSTEM_PROMPT_BUDGET_FRACTION, template_fraction))
        budget_tokens = int(model_max_tokens * budget_fraction)
        budget_chars = budget_tokens * 4
    else:
        budget_chars = DEFAULT_SYSTEM_PROMPT_BUDGET_CHARS
```

**CRITICAL:** Only the `if` branch body changes. The `if` condition and `else` branch stay exactly the same. The variables `template_result` and `model_max_tokens` are function parameters — they are in scope.

---

## Step 2: Replace `except Exception: pass` in `agent/runtime.py`

**Current code (line ~1538-1539):**
```python
        except Exception:
            pass
        return DEFAULT_THRESHOLD
```

**New code:**
```python
        except Exception as e:
            # Defensive coding should not hide programming errors. The default
            # 0.80 is used as fallback. Operators can enable DEBUG logging to
            # see the underlying cause. A misconfigured provider shouldn't
            # crash compaction, but it shouldn't be silently invisible either.
            logger.debug(
                "_compute_compaction_threshold: failed to resolve per-provider "
                "threshold, using default %s. Error: %s",
                DEFAULT_THRESHOLD,
                e,
            )
        return DEFAULT_THRESHOLD
```

**CRITICAL:** Verify `logger` is available at module level. Check:
```bash
grep -n "^import logging\|^from logging\|^logger = " agent/runtime.py | head -5
```

If `logger` is NOT already defined, add at module top (after existing imports):
```python
import logging
logger = logging.getLogger(__name__)
```

If `logger` IS already defined (likely — most runtime modules have it), just use it.

---

## Step 3: Add tests for P7 Dynamic Budget

Add this test class to `tests/test_context_strategy.py`:

```python
class TestDynamicPromptBudget:
    """P7: Dynamic system prompt budget fraction."""

    def test_small_template_uses_floor(self):
        """Templates under 15% of context → budget stays at 15%."""
        from utils.prompt_loader import _apply_system_prompt_budget
        template = "small template"  # ~3 tokens
        file_ctx = "x" * 1000
        prompt, unused = _apply_system_prompt_budget(template, file_ctx, model_max_tokens=128000)
        # Template is tiny, so budget = 15% = 19200 tokens = 76800 chars
        # File context should fit entirely within budget
        assert len(unused) == 0

    def test_large_template_grows_budget(self):
        """Templates over 15% of context → budget grows to fit template."""
        from utils.prompt_loader import _apply_system_prompt_budget
        # Template takes ~20% of context (25600 tokens = 102400 chars)
        template = "x" * 102400
        file_ctx = "file context data"
        prompt, unused = _apply_system_prompt_budget(template, file_ctx, model_max_tokens=128000)
        # Budget grew to 20% to accommodate template. File context fits.
        assert len(unused) == 0

    def test_budget_capped_at_25_percent(self):
        """Templates over 25% of context → budget capped at 25%."""
        from utils.prompt_loader import _apply_system_prompt_budget
        # Template takes ~30% of context
        template = "x" * 153600  # ~38400 tokens, 30% of 128000
        file_ctx = "y" * 10000
        prompt, unused = _apply_system_prompt_budget(template, file_ctx, model_max_tokens=128000)
        # Budget capped at 25% = 32000 tokens = 128000 chars
        # Template (153600 chars) exceeds budget_chars (128000), so file context dropped
        assert len(unused) == len(file_ctx)  # all file context is unused

    def test_zero_model_max_uses_default(self):
        """model_max_tokens=0 → uses DEFAULT_SYSTEM_PROMPT_BUDGET_CHARS."""
        from utils.prompt_loader import _apply_system_prompt_budget
        template = "template"
        file_ctx = "x" * 100
        prompt, unused = _apply_system_prompt_budget(template, file_ctx, model_max_tokens=0)
        # Default budget is 64000 chars; template + file_ctx fit easily
        assert len(unused) == 0

    def test_none_model_max_uses_default(self):
        """model_max_tokens=None → uses DEFAULT_SYSTEM_PROMPT_BUDGET_CHARS."""
        from utils.prompt_loader import _apply_system_prompt_budget
        template = "template"
        file_ctx = "x" * 100
        prompt, unused = _apply_system_prompt_budget(template, file_ctx, model_max_tokens=None)
        assert len(unused) == 0
```

---

## CRITICAL RULES

1. Do NOT change the `_apply_system_prompt_budget()` function signature or return type.
2. Do NOT change the `_compute_compaction_threshold()` function signature (still returns `float`).
3. Do NOT change `agent/context_strategy.py` — this phase is P7 + polish only.
4. Do NOT change `models/conversation.py`.
5. The P7 formula is `min(0.25, max(SYSTEM_PROMPT_BUDGET_FRACTION, template_fraction))` — verify the order of min/max arguments matches the spec.
6. The `else` branch of the budget computation stays exactly the same (`DEFAULT_SYSTEM_PROMPT_BUDGET_CHARS`).
7. The `logger.debug(...)` call must use `%s` format strings (not f-strings) for logging best practice.

---

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. New P7 tests pass
python3 -m pytest tests/test_context_strategy.py::TestDynamicPromptBudget -v --tb=short

# 2. Prompt loader tests pass
python3 -m pytest tests/ -k "prompt" -v --tb=short

# 3. All context_strategy tests pass
python3 -m pytest tests/test_context_strategy.py -v --tb=short

# 4. Existing trim/summary tests still pass
python3 -m pytest tests/test_conversation.py tests/test_phase4.py -v --tb=short

# 5. Full suite no regressions
python3 -m pytest tests/ -q --tb=short 2>&1 | tail -20
```

---

## COMPLETENESS Checklist

```
COMPLETENESS:
- [x/not done] P7 dynamic budget fraction replaces static 15% in _apply_system_prompt_budget()
- [x/not done] Budget floor: 15% (SYSTEM_PROMPT_BUDGET_FRACTION) preserved for small templates
- [x/not done] Budget ceiling: 25% (hard cap)
- [x/not done] Budget grows: template_fraction > 0.15 → budget_fraction = template_fraction
- [x/not done] except Exception: pass replaced with logger.debug(...) in _compute_compaction_threshold()
- [x/not done] TestDynamicPromptBudget added with 5 tests
- [x/not done] All new tests pass
- [x/not done] All existing tests pass
- [x/not done] Full suite no regressions
```
