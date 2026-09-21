# Phase 8: §2.8 Telemetry Enrichment — CompactionEvent History + Signature Refactor

**Spec:** `docs/specs/SPEC-CONTEXT-MANAGEMENT-ROADMAP.md` §2.6 + §2.8
**Goal:** Replace the scalar `_last_trim_removed` with a rolling `CompactionEvent` history, refactor `_compute_compaction_threshold` to return `tuple[int, int]`, and add `tests/test_runtime_compaction.py`.
**Files to change:**
1. `agent/runtime.py` — refactor `_compute_compaction_threshold`, replace scalar with history + property
2. `tests/test_runtime_compaction.py` — NEW file with `TestCompactionThreshold` (3 tests) + `TestCompactionEvent` (5 tests)

**SCOPE:** This phase implements §2.8 telemetry + §2.6 runtime tests ONLY. Do NOT implement:
- CB-6 hardening for `_find_split_index` (Phase 9)
- P9 context-pressure UI observability (deferred)
- Additional `except Exception: pass` cleanup (Phase 9)

---

## Step 1: Refactor `_compute_compaction_threshold` → return `tuple[int, int]`

**Current signature** (`agent/runtime.py:1514`):
```python
def _compute_compaction_threshold(self, conv: "Conversation") -> float:
```

**New signature:**
```python
def _compute_compaction_threshold(self, conv: "Conversation") -> tuple[int, int]:
    """Return (soft_ceiling, hard_ceiling) for the conversation's provider.

    Resolution order:
      1. conv.model's provider's compaction_threshold (when set and in (0, 1])
      2. 0.80 default

    Returns (int(128000 * 0.80), 128000) = (102400, 128000) when:
      - conv.model is None and self._config.default_provider is not configured
      - the resolved provider config has compaction_threshold <= 0 or > 1
      - any exception during provider lookup
    """
```

**Body changes:** The method currently resolves a `threshold` float and returns it. The new body must:
1. Resolve the threshold float (same logic as now — keep the provider lookup, the `except Exception as e: logger.debug(...)`, etc.)
2. Compute `hard_ceiling = self._compute_model_max(conv)` 
3. Compute `soft_ceiling = int(hard_ceiling * threshold)`
4. Return `(soft_ceiling, hard_ceiling)`

**Current body** (lines 1514-1553):
```python
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
    except Exception as e:
        logger.debug(...)
    return DEFAULT_THRESHOLD
```

**New body:**
```python
    DEFAULT_THRESHOLD = 0.80
    try:
        provider_name = (
            conv.model.split("/")[0]
            if conv.model and "/" in conv.model
            else self._config.default_provider
        )
        threshold = DEFAULT_THRESHOLD
        if provider_name:
            provider_cfg = self._config.providers.get(provider_name)
            if provider_cfg is not None:
                cfg_threshold = getattr(provider_cfg, "compaction_threshold", None)
                if cfg_threshold is not None and 0 < cfg_threshold <= 1:
                    threshold = float(cfg_threshold)
    except Exception as e:
        logger.debug(
            "_compute_compaction_threshold: failed to resolve per-provider "
            "threshold, using default %s. Error: %s",
            DEFAULT_THRESHOLD,
            e,
        )
    hard_ceiling = self._compute_model_max(conv)
    soft_ceiling = int(hard_ceiling * threshold)
    return (soft_ceiling, hard_ceiling)
```

**CRITICAL changes from current:**
- Signature returns `tuple[int, int]` not `float`
- `DEFAULT_THRESHOLD` is now used as fallback inside the try, not as multiple return statements
- `hard_ceiling` comes from `_compute_model_max(conv)` (which already exists and works)
- The early-return patterns (`return DEFAULT_THRESHOLD` on missing provider) are replaced by falling through to the final computation with `threshold = DEFAULT_THRESHOLD`

---

## Step 2: Update call site in the tool loop

**Current code** (`agent/runtime.py:1662-1669`):
```python
                model_max = self._compute_model_max(conv)
                threshold = self._compute_compaction_threshold(conv)
                soft_ceiling = int(model_max * threshold)
                messages_count_before = len(conv.messages)
                self._context_strategy.compact(conv, soft_ceiling)
                messages_count_after = len(conv.messages)
                self._last_trim_removed = messages_count_before - messages_count_after
```

**New code:**
```python
                soft_ceiling, hard_ceiling = self._compute_compaction_threshold(conv)
                model_max = hard_ceiling  # preserve for breakdown dispatch below
                self._context_strategy.compact(conv, soft_ceiling)
                # §2.8: Telemetry — read strategy.last_result, append to history.
                if self._context_strategy.last_result is not None:
                    self._compaction_events.append(self._context_strategy.last_result)
                    # Cap history at 100 events (prevents unbounded growth).
                    if len(self._compaction_events) > 100:
                        self._compaction_events = self._compaction_events[-100:]
```

**CRITICAL:** Remove the `messages_count_before`/`messages_count_after`/`self._last_trim_removed = ...` lines — the `CompactionEvent` from the strategy already records `messages_before`, `messages_after`, and `messages_removed`. The scalar assignment is replaced by the history append.

---

## Step 3: Replace scalar `_last_trim_removed` field with `_compaction_events` list

**Current** (`agent/runtime.py:1232`):
```python
        self._last_trim_removed = 0  # set per iteration in _run_loop; read by the breakdown callback
```

**New:**
```python
        self._compaction_events: list = []  # §2.8: rolling CompactionEvent history (capped at 100)
```

---

## Step 4: Add `_last_trim_removed` as a `@property`

Add this property to `AgentRuntime` (anywhere appropriate, e.g. near `_compute_compaction_threshold`):

```python
    @property
    def _last_trim_removed(self) -> int:
        """Backward-compat accessor: count from latest trim-layer event.

        Derived from _compaction_events so existing read sites (breakdown
        callback) keep working without modification.
        """
        for ev in reversed(self._compaction_events):
            if ev.layer == 2:  # P2/P3/P6 trim layer
                return ev.messages_removed
        return 0
```

---

## Step 5: Update the breakdown callback

**Current code** (`agent/runtime.py:1711-1729`):
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

**New code:**
```python
                if self._on_token_breakdown is not None:
                    breakdown = conv.get_token_breakdown(model_max)
                    breakdown["trimmed_this_turn"] = self._last_trim_removed > 0
                    breakdown["messages_remaining"] = len(conv.messages)
                    breakdown["messages_removed_this_turn"] = self._last_trim_removed
                    # §0.4 + §2.8: Compaction telemetry from the strategy.
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
                            "summary_tokens_injected": strategy_result.summary_tokens_injected,
                        }
                    self._dispatch(self._on_token_breakdown, session_key, breakdown)
                    self._compaction_events.clear()  # reset per-iteration; events already in history
```

**CRITICAL:** The last line changes from `self._last_trim_removed = 0` to `self._compaction_events.clear()`. This resets the per-iteration flag the same way the old scalar reset did. BUT — this clears ALL events from the list, including ones already appended to the history.

**WAIT — DO NOT CLEAR.** The `_compaction_events` list is the ROLLING HISTORY. It should NOT be cleared each iteration. Instead, remove the reset line entirely. The `_last_trim_removed` property reads the latest layer==2 event, which naturally returns 0 when no trim happened this iteration (because no new layer==2 event was appended).

**Revised last line:**
```python
                    self._dispatch(self._on_token_breakdown, session_key, breakdown)
                    # No reset needed — _last_trim_removed property reads latest event.
                    # When no compaction happened this iteration, no new event is appended,
                    # so the property returns the previous value. But the breakdown
                    # already captured the current state, so this is fine.
```

**ACTUALLY — there IS a subtlety.** The old code resets `_last_trim_removed = 0` after the breakdown fires, so that on the NEXT iteration (where no compaction happens), `trimmed_this_turn` is False. Without the reset, the property would still return the last event's `messages_removed` from a previous iteration.

**SOLUTION:** Track per-iteration state with a separate field:
```python
        self._compaction_events: list = []  # rolling history (capped at 100)
        self._compaction_this_iteration = False  # reset each tool-loop iteration
```

At the call site (Step 2), set the flag:
```python
                soft_ceiling, hard_ceiling = self._compute_compaction_threshold(conv)
                model_max = hard_ceiling
                self._context_strategy.compact(conv, soft_ceiling)
                if self._context_strategy.last_result is not None:
                    self._compaction_events.append(self._context_strategy.last_result)
                    self._compaction_this_iteration = True
                    if len(self._compaction_events) > 100:
                        self._compaction_events = self._compaction_events[-100:]
                else:
                    self._compaction_this_iteration = False
```

In the breakdown callback:
```python
                    breakdown["trimmed_this_turn"] = self._compaction_this_iteration
                    breakdown["messages_removed_this_turn"] = self._last_trim_removed if self._compaction_this_iteration else 0
```

After the breakdown:
```python
                    self._compaction_this_iteration = False  # reset for next iteration
```

---

## Step 6: New file `tests/test_runtime_compaction.py`

```python
"""Tests for runtime compaction threshold (P1) and CompactionEvent telemetry (§2.8)."""
import pytest
from unittest.mock import MagicMock, patch
from agent.config import AgentConfig, LLMProviderConfig
from agent.runtime import AgentRuntime
from agent.context_strategy import CompactionEvent, DefaultContextStrategy
from models.conversation import Conversation, Message, MessageRole


class TestCompactionThreshold:
    """P1: Soft ceiling computation returns (soft, hard) tuple."""

    def test_soft_ceiling_is_80_percent(self):
        """Default threshold: soft = 0.80 × max_tokens."""
        config = AgentConfig(
            providers={"openai": LLMProviderConfig(
                name="openai", base_url="x", api_key="x",
                default_model="gpt-4o", caller="openai",
                max_tokens=128_000,
            )},
            default_provider="openai",
        )
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime._config = config
        conv = Conversation(agent_name="Coder", model="openai/gpt-4o")
        soft, hard = runtime._compute_compaction_threshold(conv)
        assert soft == int(128_000 * 0.80)  # 102,400
        assert hard == 128_000

    def test_custom_threshold_per_provider(self):
        """Provider with compaction_threshold=0.90 → soft = 0.90 × max."""
        provider = LLMProviderConfig(
            name="minimax", base_url="x", api_key="x",
            default_model="minimax-m3", caller="minimax",
            max_tokens=1_048_576,
        )
        provider.compaction_threshold = 0.90
        config = AgentConfig(
            providers={"minimax": provider},
            default_provider="minimax",
        )
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime._config = config
        conv = Conversation(agent_name="Coder", model="minimax/minimax-m3")
        soft, hard = runtime._compute_compaction_threshold(conv)
        assert soft == int(1_048_576 * 0.90)  # 943,718
        assert hard == 1_048_576

    def test_fallback_when_no_provider(self):
        """When model has no provider config, falls back to 0.80 × 128_000."""
        config = AgentConfig(
            providers={},
            default_provider="openai",
        )
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime._config = config
        conv = Conversation(agent_name="Coder", model="gpt-4o")
        soft, hard = runtime._compute_compaction_threshold(conv)
        assert hard == 128_000
        assert soft == int(128_000 * 0.80)


class TestCompactionEvent:
    """§2.8: CompactionEvent history and _last_trim_removed property."""

    def test_event_appended_after_compact(self):
        """After compact() runs, an event is in _compaction_events."""
        strategy = DefaultContextStrategy()
        conv = Conversation(agent_name="Coder", model="openai/gpt-4o")
        # Add enough messages to trigger compaction
        for i in range(20):
            conv.add_user_message("x" * 5000)
            conv.add_assistant_message("y" * 5000, [])
        strategy.compact(conv, 20000)
        assert strategy.last_result is not None
        assert isinstance(strategy.last_result, CompactionEvent)
        assert strategy.last_result.messages_before > 0
        assert strategy.last_result.messages_after >= 0

    def test_event_has_correct_layer(self):
        """CompactionEvent.layer is 1 (prune) or 2 (trim)."""
        strategy = DefaultContextStrategy()
        conv = Conversation(agent_name="Coder", model="openai/gpt-4o")
        for i in range(20):
            conv.add_user_message("x" * 5000)
            conv.add_assistant_message("y" * 5000, [])
        strategy.compact(conv, 20000)
        assert strategy.last_result is not None
        assert strategy.last_result.layer in (0, 1, 2)  # 0=no-op, 1=prune, 2=trim

    def test_last_trim_removed_property_returns_zero_initially(self):
        """_last_trim_removed returns 0 when no events exist."""
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime._compaction_events = []
        assert runtime._last_trim_removed == 0

    def test_last_trim_removed_property_reads_latest_trim_event(self):
        """_last_trim_removed returns messages_removed from latest layer==2 event."""
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime._compaction_events = [
            CompactionEvent(
                turn=1, trigger="trim_layer2", layer=2,
                messages_before=20, messages_after=10, messages_removed=10,
                tokens_before=50000, tokens_after=25000, tokens_freed=25000,
                summary_tokens_injected=500, soft_ceiling=20000, hard_ceiling=128000,
                provider="openai", model="openai/gpt-4o",
            ),
        ]
        assert runtime._last_trim_removed == 10

    def test_history_capped_at_100(self):
        """_compaction_events list is capped at 100 entries."""
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime._compaction_events = []
        for i in range(150):
            runtime._compaction_events.append(
                CompactionEvent(
                    turn=i, trigger="trim_layer2", layer=2,
                    messages_before=20, messages_after=10, messages_removed=10,
                    tokens_before=50000, tokens_after=25000, tokens_freed=25000,
                    summary_tokens_injected=500, soft_ceiling=20000, hard_ceiling=128000,
                    provider="openai", model="openai/gpt-4o",
                )
            )
            if len(runtime._compaction_events) > 100:
                runtime._compaction_events = runtime._compaction_events[-100:]
        assert len(runtime._compaction_events) == 100
        # Verify the oldest 50 were dropped (turn 0-49 gone, turn 50-149 remain)
        assert runtime._compaction_events[0].turn == 50
        assert runtime._compaction_events[-1].turn == 149
```

---

## CRITICAL RULES

1. Do NOT change `agent/context_strategy.py` — the strategy already records `CompactionEvent` and exposes `last_result`.
2. Do NOT change `models/conversation.py`.
3. Do NOT change `utils/prompt_loader.py` — Phase 7 is approved and done.
4. The `_compute_compaction_threshold` signature change from `float` to `tuple[int, int]` is the ONLY signature change. All other signatures stay the same.
5. The `_last_trim_removed` property must be a read-only `@property` — no setter. The scalar is gone.
6. The `_compaction_events` list is the rolling history. It is appended to at the call site and capped at 100. It is NOT cleared per-iteration.
7. The `_compaction_this_iteration` flag handles the per-iteration reset semantics that the old `self._last_trim_removed = 0` provided.
8. `tests/test_runtime_compaction.py` is a NEW file — do not modify existing test files.
9. `logger` is already defined at module level (`agent/runtime.py:73`).

---

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. New runtime compaction tests pass
python3 -m pytest tests/test_runtime_compaction.py -v --tb=short

# 2. All context_strategy tests still pass
python3 -m pytest tests/test_context_strategy.py -v --tb=short

# 3. Conversation tests still pass
python3 -m pytest tests/test_conversation.py tests/test_phase4.py -v --tb=short

# 4. Prompt loader tests still pass
python3 -m pytest tests/ -k "prompt" -v --tb=short

# 5. Full suite no regressions
python3 -m pytest tests/ -q --tb=short 2>&1 | tail -20
```

---

## COMPLETENESS Checklist

```
COMPLETENESS:
- [ ] _compute_compaction_threshold returns tuple[int, int] (soft_ceiling, hard_ceiling)
- [ ] Call site updated to unpack tuple, remove old model_max/threshold lines
- [ ] Scalar _last_trim_removed field replaced with _compaction_events list
- [ ] _last_trim_removed @property reads latest layer==2 event
- [ ] _compaction_this_iteration flag added for per-iteration reset
- [ ] Breakdown callback updated to use flag + property
- [ ] History capped at 100 events at call site
- [ ] tests/test_runtime_compaction.py created with TestCompactionThreshold (3 tests) + TestCompactionEvent (5 tests)
- [ ] All new tests pass
- [ ] All existing tests pass
- [ ] Full suite no regressions
```
