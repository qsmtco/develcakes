# Phase 3: Runtime Wiring — Strategy Resolution + Tool Loop Change

**Spec:** `docs/specs/SPEC-CONTEXT-MANAGEMENT-ROADMAP.md` §0.3, §0.4, §2.3, §2.4
**Goal:** Wire `DefaultContextStrategy` into `AgentRuntime`, add `_compute_compaction_threshold()`, and change the tool loop call site from `conv.trim_to_token_limit(model_max)` to `self._context_strategy.compact(conv, soft_ceiling)`.
**Files to change:**
1. `agent/runtime.py` — `__init__()` gains strategy field, new `_compute_compaction_threshold()` method, tool loop call site changes

---

## Step 1: Add strategy field to `__init__()` (around line 1233)

Current code in `__init__()` (around line 1233):
```python
        # A-4: Audit log for tool executions
        self._audit_log = AuditLog()
```

Add AFTER that line:
```python
        # §0: Pluggable context management strategy.
        # DefaultContextStrategy is the extracted trim_to_token_limit algorithm
        # (Phase 1). Future: configurable via AgentConfig.context_strategy.
        from agent.context_strategy import DefaultContextStrategy
        self._context_strategy = DefaultContextStrategy()
```

## Step 2: Add `_compute_compaction_threshold()` method

Add this new method right AFTER `_compute_model_max()` (which ends around line 1498). Place it before the `_run_loop` method.

```python
    def _compute_compaction_threshold(self, conv: "Conversation") -> float:
        """Return the compaction threshold for the current conversation's provider.

        Resolution order:
          1. conv.model's provider's compaction_threshold (when set and in (0, 1])
          2. 0.80 default

        Returns 0.80 when:
          - conv.model is None and self._config.default_provider is not configured
          - the resolved provider config has compaction_threshold <= 0 or > 1
          - any exception during provider lookup
        """
        DEFAULT_THRESHOLD = 0.80
        try:
            provider_name = (
                conv.model.split("/")[0]
                if conv.model and "/" in conv.model
                else self._config.default_provider
            )
            if not provider_name:
                return DEFAULT_THRESHOLD
            provider_cfg = self._config.providers.get(provider_name)
            if provider_cfg is None:
                return DEFAULT_THRESHOLD
            threshold = getattr(provider_cfg, "compaction_threshold", None)
            if threshold is not None and 0 < threshold <= 1:
                return float(threshold)
        except Exception:
            pass
        return DEFAULT_THRESHOLD
```

## Step 3: Change the tool loop call site (around line 1616-1620)

Current code (lines ~1616-1620):
```python
                # Context-bloat fix (BUG #1) — cap history before each LLM call.
                # Conversation.trim_to_token_limit() is unit-tested at
                # tests/test_conversation.py:249 (TestConversationTrim) and
                # tests/test_phase4.py:280 (summary-on-trim). It preserves
                # the system prompt and the last 4 messages, and (per §4.10)
                # injects a budget-aware summary when >= 8 messages remain.
                model_max = self._compute_model_max(conv)
                messages_count_before = len(conv.messages)
                conv.trim_to_token_limit(model_max)
                messages_count_after = len(conv.messages)
                self._last_trim_removed = messages_count_before - messages_count_after
```

Replace with:
```python
                # §0: Pluggable context strategy — compaction before each LLM call.
                # The strategy lives in agent/context_strategy.py and replaces the
                # old conv.trim_to_token_limit() call. The delegation shim on
                # Conversation remains for backward compat with tests.
                #
                # soft_ceiling = model_max * compaction_threshold
                # (e.g. 128000 * 0.80 = 102400 — compact when usage exceeds 80%.)
                model_max = self._compute_model_max(conv)
                threshold = self._compute_compaction_threshold(conv)
                soft_ceiling = int(model_max * threshold)
                messages_count_before = len(conv.messages)
                self._context_strategy.compact(conv, soft_ceiling)
                messages_count_after = len(conv.messages)
                self._last_trim_removed = messages_count_before - messages_count_after
```

## Step 4: Update the token breakdown block (around line 1632)

Current code (around line 1632):
```python
                if self._on_token_breakdown is not None:
                    breakdown = conv.get_token_breakdown(model_max)
                    breakdown["trimmed_this_turn"] = self._last_trim_removed > 0
                    breakdown["messages_remaining"] = len(conv.messages)
                    breakdown["messages_removed_this_turn"] = self._last_trim_removed
                    self._dispatch(self._on_token_breakdown, session_key, breakdown)
                    self._last_trim_removed = 0
```

Add compaction telemetry after `breakdown["messages_removed_this_turn"]` line:
```python
                if self._on_token_breakdown is not None:
                    breakdown = conv.get_token_breakdown(model_max)
                    breakdown["trimmed_this_turn"] = self._last_trim_removed > 0
                    breakdown["messages_remaining"] = len(conv.messages)
                    breakdown["messages_removed_this_turn"] = self._last_trim_removed
                    # §0.4: Compaction telemetry from the strategy.
                    strategy_result = self._context_strategy.last_result
                    if strategy_result is not None:
                        breakdown["compaction_event"] = {
                            "trigger": strategy_result.trigger,
                            "layer": strategy_result.layer,
                            "tokens_before": strategy_result.tokens_before,
                            "tokens_after": strategy_result.tokens_after,
                            "tokens_freed": strategy_result.tokens_freed,
                            "soft_ceiling": strategy_result.soft_ceiling,
                            "hard_ceiling": strategy_result.hard_ceiling,
                        }
                    self._dispatch(self._on_token_breakdown, session_key, breakdown)
                    self._last_trim_removed = 0
```

---

## CRITICAL RULES

1. Do NOT remove `conv.trim_to_token_limit()` or `conv._last_exchange_summary()` from `models/conversation.py` — they are delegation shims (Phase 1) that existing tests call directly.
2. Do NOT change `_compute_model_max()` — it already works correctly.
3. The `_compute_compaction_threshold()` method follows the SAME provider resolution pattern as `_compute_model_max()` for consistency.
4. Use `getattr(provider_cfg, "compaction_threshold", None)` for safety even though Phase 2 added the field — an old pickled object might lack it.
5. The `soft_ceiling = int(model_max * threshold)` computation uses `int()` (floor) — this matches how `_compute_model_max` returns ints.

---

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. Module imports cleanly
python3 -c "
from agent.runtime import AgentRuntime
print('AgentRuntime imports OK')
"

# 2. Strategy is wired in __init__
python3 -c "
from agent.runtime import AgentRuntime
from agent.config import AgentConfig
from agent.context_strategy import DefaultContextStrategy
rt = AgentRuntime(AgentConfig(), GLib=None)
assert isinstance(rt._context_strategy, DefaultContextStrategy), 'strategy not wired'
print('Strategy wiring OK:', type(rt._context_strategy).__name__)
"

# 3. Compaction threshold resolution
python3 -c "
from agent.runtime import AgentRuntime
from agent.config import AgentConfig, LLMProviderConfig
from models.conversation import Conversation

# Default threshold
config = AgentConfig()
rt = AgentRuntime(config, GLib=None)
conv = Conversation(agent_name='test')
assert rt._compute_compaction_threshold(conv) == 0.80, 'default threshold failed'

# Custom threshold
config2 = AgentConfig()
config2.providers['testprov'] = LLMProviderConfig(
    name='testprov', base_url='x', api_key='***', default_model='testprov/x',
    compaction_threshold=0.90
)
rt2 = AgentRuntime(config2, GLib=None)
conv2 = Conversation(agent_name='test', model='testprov/x')
t = rt2._compute_compaction_threshold(conv2)
assert t == 0.90, f'custom threshold failed: got {t}'
print('Compaction threshold resolution OK')
"

# 4. Full test suite — no regressions
python3 -m pytest tests/ -q --tb=short 2>&1 | tail -20
```

---

## COMPLETENESS Checklist

```
COMPLETENESS:
- [x/not done] DefaultContextStrategy imported and stored as self._context_strategy in __init__()
- [x/not done] _compute_compaction_threshold() method added after _compute_model_max()
- [x/not done] Tool loop call site changed from conv.trim_to_token_limit(model_max) to self._context_strategy.compact(conv, soft_ceiling)
- [x/not done] soft_ceiling = int(model_max * threshold) computed correctly
- [x/not done] Token breakdown block includes compaction_event telemetry
- [x/not done] Default threshold (0.80) resolves when no provider configured
- [x/not done] Custom threshold (0.90) resolves when provider configured
- [x/not done] Full test suite has no regressions
```
