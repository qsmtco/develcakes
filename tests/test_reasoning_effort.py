# tests/test_reasoning_effort.py
# SPEC-REASONING-EFFORT SP1 — payload mapping, store defaults, live-card overlay.
#
# No GTK, no network. Adapter tests patch urlopen_with_ssl_retry and read req.data.

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from agent.config import AgentConfig, LLMProviderConfig
from agent.llm.openai_provider import OpenAIProvider
from agent.runtime import AgentRuntime
from models.providers import (
    _VALID_REASONING_LEVELS,
    ProviderConfig,
    validate_provider_reasoning_effort,
)
from utils.providers_store import save_providers


class _FakeResp:
    def __init__(self):
        self._body = json.dumps(
            {"choices": [{"message": {"content": "hi"}}]}
        ).encode()
        self._lines = [b"data: [DONE]"]

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)


def _fake_urlopen(req, timeout=None):
    return _FakeResp()


def _payload_from(monkeypatch, caller: str, level: str, *, stream: bool = False) -> dict:
    captured: dict = {}

    def _capture(req, timeout=None):
        captured["payload"] = json.loads(req.data.decode())
        return _fake_urlopen(req, timeout)

    monkeypatch.setattr(
        "agent.llm.openai_provider.urlopen_with_ssl_retry", _capture
    )
    provider = OpenAIProvider(caller)
    kwargs = {
        "base_url": "https://example.com/v1",
        "api_key": "sk-test",
        "model": f"{caller}/demo",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": None,
        "timeout": 8.0,
        "reasoning_effort": level,
    }
    if stream:
        list(provider.stream(**kwargs))
    else:
        provider.call(**kwargs)
    return captured["payload"]


class TestValidateReasoningEffort:
    def test_closed_set(self):
        assert _VALID_REASONING_LEVELS == frozenset({"off", "low", "medium", "high"})
        for level in _VALID_REASONING_LEVELS:
            assert validate_provider_reasoning_effort(level) == level
            assert validate_provider_reasoning_effort(level.upper()) == level

    def test_missing_empty_invalid_become_off(self):
        assert validate_provider_reasoning_effort("") == "off"
        assert validate_provider_reasoning_effort("  ") == "off"
        assert validate_provider_reasoning_effort("turbo") == "off"
        assert validate_provider_reasoning_effort(None) == "off"
        assert validate_provider_reasoning_effort(3) == "off"


class TestPayloadMapping:
    """T1 + T2: openai/openrouter wire shapes; off is omitted; zai omits."""

    @pytest.mark.parametrize("level", ["low", "medium", "high"])
    @pytest.mark.parametrize("stream", [False, True])
    def test_openai_maps_reasoning_effort(self, monkeypatch, level, stream):
        payload = _payload_from(monkeypatch, "openai", level, stream=stream)
        assert payload["reasoning_effort"] == level
        assert "reasoning" not in payload

    @pytest.mark.parametrize("level", ["low", "medium", "high"])
    @pytest.mark.parametrize("stream", [False, True])
    def test_openrouter_maps_reasoning_object(self, monkeypatch, level, stream):
        payload = _payload_from(monkeypatch, "openrouter", level, stream=stream)
        assert payload["reasoning"] == {"effort": level}
        assert "reasoning_effort" not in payload

    @pytest.mark.parametrize("caller", ["openai", "openrouter", "zai"])
    @pytest.mark.parametrize("stream", [False, True])
    def test_off_omits_key(self, monkeypatch, caller, stream):
        payload = _payload_from(monkeypatch, caller, "off", stream=stream)
        assert "reasoning_effort" not in payload
        assert "reasoning" not in payload

    @pytest.mark.parametrize("stream", [False, True])
    def test_zai_high_still_omits(self, monkeypatch, stream):
        payload = _payload_from(monkeypatch, "zai", "high", stream=stream)
        assert "reasoning_effort" not in payload
        assert "reasoning" not in payload

    def test_off_byte_identical_to_default_call(self, monkeypatch):
        """T2: reasoning_effort='off' matches a call that omits the kwarg."""
        off_payload = _payload_from(monkeypatch, "openai", "off")

        captured: dict = {}

        def _capture(req, timeout=None):
            captured["payload"] = json.loads(req.data.decode())
            return _fake_urlopen(req, timeout)

        monkeypatch.setattr(
            "agent.llm.openai_provider.urlopen_with_ssl_retry", _capture
        )
        OpenAIProvider("openai").call(
            base_url="https://example.com/v1",
            api_key="sk-test",
            model="openai/demo",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            timeout=8.0,
        )
        assert set(off_payload) == set(captured["payload"])
        assert "reasoning_effort" not in captured["payload"]


class TestStoreDefaults:
    """T4: YAML without the keys loads as off/False; invalid level coerces."""

    def test_missing_keys_default(self, tmp_config_dir):
        from utils.providers_store import _from_dict
        cfg = _from_dict({
            "name": "p1",
            "base_url": "https://x.example.com/v1",
            "api_key": "k",
            "default_model": "openrouter/m",
        })
        assert cfg.reasoning_effort == "off"
        assert cfg.supports_reasoning is False

    def test_invalid_level_coerces_and_warns(self, tmp_config_dir, caplog):
        from utils.providers_store import _from_dict
        with caplog.at_level(logging.WARNING, logger="utils.providers_store"):
            cfg = _from_dict({
                "name": "p1",
                "base_url": "https://x.example.com/v1",
                "api_key": "k",
                "default_model": "openrouter/m",
                "reasoning_effort": "turbo",
            })
        assert cfg.reasoning_effort == "off"
        assert any("invalid reasoning_effort" in r.getMessage() for r in caplog.records)

    def test_round_trip_persists_both_fields(self, tmp_config_dir):
        from utils.providers_store import load_providers
        save_providers([
            ProviderConfig(
                name="p1",
                base_url="https://x.example.com/v1",
                api_key="k",
                default_model="openrouter/m",
                caller="openrouter",
                reasoning_effort="high",
                supports_reasoning=True,
            )
        ])
        loaded = load_providers()
        assert loaded[0].reasoning_effort == "high"
        assert loaded[0].supports_reasoning is True

    def test_save_coerces_unknown_level(self, tmp_config_dir):
        from utils.providers_store import load_providers
        save_providers([
            ProviderConfig(
                name="p1",
                base_url="https://x.example.com/v1",
                api_key="k",
                default_model="openrouter/m",
                caller="openrouter",
                reasoning_effort="turbo",
            )
        ])
        loaded = load_providers()
        assert loaded[0].reasoning_effort == "off"


_RUNTIME_LOGGER = "agent.runtime"


def _make_runtime(providers: dict[str, LLMProviderConfig]) -> AgentRuntime:
    return AgentRuntime(config=AgentConfig(providers=providers, default_provider="p1"))


def _inject_conversation(rt: AgentRuntime, api_key: str | None = None) -> None:
    rt._conversations["s1"] = SimpleNamespace(
        api_key=api_key, model="p1/m1", app_title="t"
    )


class _CapturingProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": "captured"}


class TestLiveCardOverlay:
    """T4b: live card wins; flag is the guard; summary stays off; turbo keeps frozen."""

    def test_live_high_and_flag_reaches_call(self, tmp_config_dir, monkeypatch):
        frozen = LLMProviderConfig(
            name="p1", base_url="https://old.example.com/v1", api_key="sk-p1",
            default_model="p1/m1", caller="openrouter",
            reasoning_effort="off", supports_reasoning=False,
        )
        rt = _make_runtime({"p1": frozen})
        _inject_conversation(rt)
        save_providers([
            ProviderConfig(
                name="p1", base_url="https://old.example.com/v1", api_key="sk-p1",
                default_model="p1/m1", caller="openrouter",
                reasoning_effort="high", supports_reasoning=True,
            )
        ])
        cap = _CapturingProvider()
        monkeypatch.setattr("agent.runtime._get_provider", lambda _key: cap)

        rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        assert cap.calls[0]["reasoning_effort"] == "high"

    def test_live_high_without_flag_omits(self, tmp_config_dir, monkeypatch):
        frozen = LLMProviderConfig(
            name="p1", base_url="https://old.example.com/v1", api_key="sk-p1",
            default_model="p1/m1", caller="openrouter",
            reasoning_effort="off", supports_reasoning=False,
        )
        rt = _make_runtime({"p1": frozen})
        _inject_conversation(rt)
        save_providers([
            ProviderConfig(
                name="p1", base_url="https://old.example.com/v1", api_key="sk-p1",
                default_model="p1/m1", caller="openrouter",
                reasoning_effort="high", supports_reasoning=False,
            )
        ])
        cap = _CapturingProvider()
        monkeypatch.setattr("agent.runtime._get_provider", lambda _key: cap)

        rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        assert cap.calls[0]["reasoning_effort"] == "off"

    def test_summary_stays_off(self, tmp_config_dir, monkeypatch):
        frozen = LLMProviderConfig(
            name="p1", base_url="https://old.example.com/v1", api_key="sk-p1",
            default_model="p1/m1", caller="openrouter",
            reasoning_effort="high", supports_reasoning=True,
        )
        rt = _make_runtime({"p1": frozen})
        cap = _CapturingProvider()
        monkeypatch.setattr("agent.runtime._get_provider", lambda _key: cap)

        rt._call_for_summary("sys", "user", model_id="p1/m1")

        assert cap.calls[0]["reasoning_effort"] == "off"

    def test_invalid_live_level_keeps_frozen(self, tmp_config_dir, monkeypatch, caplog):
        frozen = LLMProviderConfig(
            name="p1", base_url="https://old.example.com/v1", api_key="sk-p1",
            default_model="p1/m1", caller="openrouter",
            reasoning_effort="high", supports_reasoning=True,
        )
        rt = _make_runtime({"p1": frozen})
        _inject_conversation(rt)

        def _bad_live():
            return [
                ProviderConfig(
                    name="p1", base_url="https://old.example.com/v1", api_key="sk-p1",
                    default_model="p1/m1", caller="openrouter",
                    reasoning_effort="turbo", supports_reasoning=True,
                )
            ]

        monkeypatch.setattr("utils.providers_store.load_providers", _bad_live)
        cap = _CapturingProvider()
        monkeypatch.setattr("agent.runtime._get_provider", lambda _key: cap)

        with caplog.at_level(logging.WARNING, logger=_RUNTIME_LOGGER):
            rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        assert rt._config.providers["p1"].reasoning_effort == "high"
        assert cap.calls[0]["reasoning_effort"] == "high"
        assert any("not a valid level" in r.getMessage() for r in caplog.records)

    def test_streaming_path_gets_effective_level(self, tmp_config_dir):
        frozen = LLMProviderConfig(
            name="p1", base_url="https://old.example.com/v1", api_key="sk-p1",
            default_model="p1/m1", caller="openrouter",
            reasoning_effort="off", supports_reasoning=False,
            supports_streaming=True,
        )
        rt = _make_runtime({"p1": frozen})
        rt._on_text_delta = lambda *a, **k: None
        _inject_conversation(rt)
        save_providers([
            ProviderConfig(
                name="p1", base_url="https://old.example.com/v1", api_key="sk-p1",
                default_model="p1/m1", caller="openrouter",
                reasoning_effort="medium", supports_reasoning=True,
            )
        ])
        captured: list[dict] = []
        rt._call_llm_streaming = lambda **kwargs: captured.append(kwargs)

        rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        assert captured[0]["reasoning_effort"] == "medium"
