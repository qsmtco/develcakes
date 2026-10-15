"""SPEC-30 Stage 1: real ceiling, landing goal, mask rules, hot-path pins."""
from __future__ import annotations

from agent.config import LLMProviderConfig
from agent.runtime import AgentRuntime, _LANDING_CAP
from models.conversation import Conversation
from tests.test_agent_runtime import _make_cfg
from tests.test_runtime_compaction import _make_runtime
from utils.providers_store import _context_window_value, load_providers, save_providers
from models.providers import ProviderConfig


def _provider(**kwargs) -> LLMProviderConfig:
    base = dict(
        name="openai",
        base_url="x",
        api_key="x",
        default_model="gpt-4o",
        caller="openai",
        max_tokens=128_000,
    )
    base.update(kwargs)
    return LLMProviderConfig(**base)


def _conv(model: str) -> Conversation:
    return Conversation(agent_name="Coder", model=model)


def test_fat_max_tokens_is_capped_without_context_window():
    provider = _provider(name="weird", caller="not-a-caller", max_tokens=1_300_000)
    runtime = _make_runtime({"weird": provider}, default_provider="weird")
    conv = _conv("weird/m")
    soft, hard = runtime._compute_compaction_threshold(conv)
    assert hard == 200_000
    assert soft == 160_000
    assert runtime._compute_landing_goal(hard) == 24_000


def test_context_window_wins_over_fat_max_tokens():
    provider = _provider(
        name="big", caller="openai", max_tokens=1_300_000, context_window=1_000_000,
    )
    runtime = _make_runtime({"big": provider}, default_provider="big")
    _soft, hard = runtime._compute_compaction_threshold(_conv("big/m"))
    assert hard == 1_000_000


def test_minimax_zero_max_tokens_stays_on_caller_table():
    provider = _provider(name="minimax", caller="minimax", max_tokens=0, default_model="m")
    runtime = _make_runtime({"minimax": provider}, default_provider="minimax")
    _soft, hard = runtime._compute_compaction_threshold(_conv("minimax/m"))
    assert hard == 1_048_576
    assert runtime._compute_landing_goal(hard) == _LANDING_CAP


def test_default_128k_threshold_and_landing_goal():
    provider = _provider()
    runtime = _make_runtime({"openai": provider}, default_provider="openai")
    soft, hard = runtime._compute_compaction_threshold(_conv("openai/gpt-4o"))
    assert (soft, hard) == (102_400, 128_000)
    assert runtime._compute_landing_goal(hard) == 19_200


def test_landing_goal_rejects_bad_ceiling():
    runtime = _make_runtime({}, default_provider="openai")
    assert runtime._compute_landing_goal(0) == 0
    assert runtime._compute_landing_goal(-5) == 0
    assert runtime._compute_landing_goal(True) == 0  # type: ignore[arg-type]


def test_context_window_non_int_is_zero():
    assert _context_window_value("1000000") == 0
    assert _context_window_value(1.5) == 0
    assert _context_window_value(True) == 0
    assert _context_window_value(None) == 0
    assert _context_window_value(1_000_000) == 1_000_000


def test_context_window_round_trips_and_reaches_runtime(tmp_config_dir):
    save_providers([ProviderConfig(
        name="openai",
        base_url="https://example.com",
        api_key="k",
        default_model="openai/gpt-4o",
        caller="openai",
        max_tokens=1_300_000,
        context_window=1_000_000,
    )])
    loaded = load_providers()
    assert loaded[0].context_window == 1_000_000
    from agent.config import _to_llm_provider
    copied = _to_llm_provider(loaded[0])
    runtime = _make_runtime({"openai": copied}, default_provider="openai")
    _soft, hard = runtime._compute_compaction_threshold(_conv("openai/gpt-4o"))
    assert hard == 1_000_000


def test_first_read_pins_a_file_and_a_change_drops_it(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("hello\n")
    rt = AgentRuntime(_make_cfg())
    sk = "special:pin"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    conv = rt.get_conversation(sk)
    args = {"path": "a.py"}
    rt._note_tool_path(sk, conv, "read_file", args)
    assert "a.py" in rt._protect_paths(sk, conv)
    stored = rt._hot_pins[sk]["a.py"]
    rt._note_tool_path(sk, conv, "read_file", args)
    assert rt._hot_pins[sk]["a.py"] == stored
    target.write_text("hello\nchanged\n")
    assert "a.py" not in rt._protect_paths(sk, conv)
    assert rt._read_counts[sk]["a.py"] == 0


def test_failed_stat_does_not_count(tmp_path):
    rt = AgentRuntime(_make_cfg())
    sk = "special:pin"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    conv = rt.get_conversation(sk)
    rt._note_tool_path(sk, conv, "read_file", {"path": "missing.py"})
    assert rt._read_counts.get(sk, {}) == {}
    rt._note_tool_path(sk, conv, "read_file", {"path": ""})
    rt._note_tool_path(sk, conv, "read_file", "nope")  # type: ignore[arg-type]
    assert sk not in rt._hot_pins or rt._hot_pins[sk] == {}


def test_empty_project_does_not_pin(tmp_path):
    rt = AgentRuntime(_make_cfg())
    sk = "special:pin"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    conv = rt.get_conversation(sk)
    conv.project_path = ""
    rt._note_tool_path(sk, conv, "read_file", {"path": "a.py"})
    assert rt._read_counts.get(sk, {}) == {}


def test_edit_survives_pin_drop_and_clear_drops_both(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("v1\n")
    rt = AgentRuntime(_make_cfg())
    sk = "special:pin"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    conv = rt.get_conversation(sk)
    for _ in range(3):
        rt._note_tool_path(sk, conv, "read_file", {"path": "a.py"})
    rt._note_tool_path(sk, conv, "edit_file", {"path": "a.py"})
    target.write_text("v2\n")
    protected = rt._protect_paths(sk, conv)
    assert "a.py" in protected
    assert sk not in rt._hot_pins or "a.py" not in rt._hot_pins.get(sk, {})
    rt.drop_read_cache(sk)
    assert rt._protect_paths(sk, conv) == ()


def test_drop_read_cache_missing_session_is_noop():
    rt = AgentRuntime(_make_cfg())
    rt.drop_read_cache("special:missing")


def test_compact_under_threshold_uses_soft_ceiling(tmp_path, monkeypatch):
    rt = AgentRuntime(_make_cfg())
    sk = "special:pin"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    conv = rt.get_conversation(sk)
    seen = {}

    def compact(conv_arg, budget, **kwargs):
        seen["budget"] = budget
        seen["kwargs"] = kwargs

    monkeypatch.setattr(rt._context_strategy, "compact", compact)
    monkeypatch.setattr(rt._context_strategy, "panic_prune", lambda *a, **k: (_ for _ in ()).throw(AssertionError("panic")))
    conv.get_token_estimate = lambda: 1_000  # type: ignore[method-assign]
    soft, hard = rt._compute_compaction_threshold(conv)
    rt._compact_before_llm(sk, conv, soft, hard)
    assert seen["budget"] == soft
    assert seen["kwargs"] == {}
