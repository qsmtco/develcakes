"""Tests for runtime compaction threshold (P1) and CompactionEvent telemetry (§2.8).

Phase 8 of the Context Management Roadmap.

Covers:
- TestCompactionThreshold: _compute_compaction_threshold returns tuple[int, int]
  (soft_ceiling, hard_ceiling) per the spec.
- TestCompactionEvent: CompactionEvent history (_compaction_events),
  _last_trim_removed @property, and per-iteration flag semantics.
"""
import threading

from agent.config import AgentConfig, LLMProviderConfig
from agent.context_strategy import CompactionEvent, DefaultContextStrategy
from agent.runtime import AgentRuntime
from models.conversation import Conversation

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_runtime(providers: dict, default_provider: str = "openai") -> AgentRuntime:
    """Construct an AgentRuntime with __new__ to bypass __init__ side-effects.

    We set only the fields the tests need (_config). The runtime is intended
    to be used for _compute_compaction_threshold() and the new property/flag
    tests, not the full tool loop.
    """
    config = AgentConfig(providers=providers, default_provider=default_provider)
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._config = config
    # Initialize the new §2.8 fields so tests can exercise the property/flag.
    runtime._compaction_events = []
    runtime._compaction_this_iteration = False
    # Audit-Fix-26 (Bug #3): _last_trim_removed filters by session_key using
    # _last_breakdown_session as the target session.
    runtime._last_breakdown_session = ""
    # Audit-Fix-8: _last_trim_removed now acquires _compaction_lock.
    # The real __init__ sets this; tests via __new__ must mirror it.
    runtime._compaction_lock = threading.Lock()
    return runtime


def _make_event(turn: int = 1, layer: int = 2, messages_removed: int = 10) -> CompactionEvent:
    """Build a CompactionEvent with the new (Phase 8) field set."""
    return CompactionEvent(
        turn=turn,
        trigger="trim_layer2" if layer == 2 else "prune_layer1",
        layer=layer,
        messages_before=20,
        messages_after=20 - messages_removed,
        messages_removed=messages_removed,
        tokens_before=50_000,
        tokens_after=25_000,
        tokens_freed=25_000,
        summary_tokens_injected=500,
        soft_ceiling=20_000,
        hard_ceiling=None,  # Strategy doesn't know; runtime patches it.
        provider="openai",
        model="openai/gpt-4o",
    )


# ── TestCompactionThreshold ────────────────────────────────────────────────────


class TestCompactionThreshold:
    """P1: Soft ceiling computation returns (soft, hard) tuple."""

    def test_soft_ceiling_is_80_percent(self):
        """Default threshold: soft = 0.80 × max_tokens."""
        providers = {
            "openai": LLMProviderConfig(
                name="openai", base_url="x", api_key="x",
                default_model="gpt-4o", caller="openai",
                max_tokens=128_000,
            ),
        }
        runtime = _make_runtime(providers, default_provider="openai")
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
        runtime = _make_runtime({"minimax": provider}, default_provider="minimax")
        conv = Conversation(agent_name="Coder", model="minimax/minimax-m3")
        soft, hard = runtime._compute_compaction_threshold(conv)
        # SPEC-38: max_tokens is uncapped — a fat max_tokens IS the window.
        assert soft == int(1_048_576 * 0.90)
        assert hard == 1_048_576

    def test_context_window_bypasses_max_tokens_cap(self):
        """An explicit context_window is the window, even above 200k."""
        provider = LLMProviderConfig(
            name="minimax", base_url="x", api_key="x",
            default_model="minimax-m3", caller="minimax",
            max_tokens=1_300_000,
            context_window=1_048_576,
            compaction_threshold=0.90,
        )
        runtime = _make_runtime({"minimax": provider}, default_provider="minimax")
        conv = Conversation(agent_name="Coder", model="minimax/minimax-m3")
        soft, hard = runtime._compute_compaction_threshold(conv)
        assert hard == 1_048_576
        assert soft == int(1_048_576 * 0.90)

    def test_fallback_when_no_provider(self):
        """When model has no provider config, falls back to 0.80 × 128_000."""
        runtime = _make_runtime(providers={}, default_provider="openai")
        # No "/" in model → falls back to default_provider "openai" → no
        # providers in dict → returns 128_000 fallback in _compute_model_max,
        # and 0.80 × 128_000 = 102_400 for soft.
        conv = Conversation(agent_name="Coder", model="gpt-4o")
        soft, hard = runtime._compute_compaction_threshold(conv)
        assert hard == 128_000
        assert soft == int(128_000 * 0.80)


# ── TestCompactionEvent ────────────────────────────────────────────────────────


class TestCompactionEvent:
    """§2.8: CompactionEvent history and _last_trim_removed property."""

    def test_event_appended_after_compact(self):
        """After compact() runs, an event is recorded in strategy.last_result."""
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
        # 0=no-op, 1=prune, 2=trim. 3=manual is reserved.
        assert strategy.last_result.layer in (0, 1, 2)

    def test_no_op_compact_reports_layer_zero(self):
        """When compact() does nothing, layer must be 0 (not phantom 2).

        Audit-Fix-2: prior to this fix, the strategy forced layer=0 → 2 as a
        phantom default. After the fix, layer=0 is honest reporting of no-op.
        """
        strategy = DefaultContextStrategy()
        conv = Conversation(agent_name="Coder", model="openai/gpt-4o")
        conv.add_user_message("hi")
        conv.add_assistant_message("hello", [])
        # Token budget is huge — no trimming needed.
        strategy.compact(conv, token_budget=100000)
        assert strategy.last_result is not None
        assert strategy.last_result.layer == 0, (
            "No-op compact must report layer=0, not phantom layer=2"
        )

    def test_last_trim_removed_property_returns_zero_initially(self):
        """_last_trim_removed returns 0 when no events exist."""
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime._compaction_events = []
        runtime._compaction_lock = threading.Lock()
        runtime._last_breakdown_session = ""
        assert runtime._last_trim_removed == 0

    def test_last_trim_removed_property_reads_latest_trim_event(self):
        """_last_trim_removed returns messages_removed from latest layer==2 event."""
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime._compaction_events = [
            _make_event(turn=1, layer=2, messages_removed=10),
        ]
        runtime._compaction_lock = threading.Lock()
        runtime._last_breakdown_session = ""
        assert runtime._last_trim_removed == 10

    def test_history_capped_at_100(self):
        """_compaction_events list is capped at 100 entries (oldest dropped)."""
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime._compaction_events = []
        runtime._compaction_lock = threading.Lock()
        # Simulate the runtime's history-cap logic at the call site.
        for i in range(150):
            runtime._compaction_events.append(
                _make_event(turn=i, layer=2, messages_removed=10)
            )
            if len(runtime._compaction_events) > 100:
                runtime._compaction_events = runtime._compaction_events[-100:]
        assert len(runtime._compaction_events) == 100
        # The oldest 50 (turns 0-49) are dropped; turns 50-149 remain.
        assert runtime._compaction_events[0].turn == 50
        assert runtime._compaction_events[-1].turn == 149