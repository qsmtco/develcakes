"""SPEC-22 Stage 3 chain walk. The flag defaults off."""

from __future__ import annotations

import urllib.error
from types import SimpleNamespace

from agent.config import AgentConfig, LLMProviderConfig
from agent.runtime import AgentRuntime


class _Provider:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def _cfg(enabled=True):
    primary = LLMProviderConfig(
        name="OpenRouter", base_url="https://openrouter.example/v1", api_key="sk-a",
        default_model="openrouter/demo", caller="openrouter",
    )
    fallback = LLMProviderConfig(
        name="M3", base_url="https://fallback.example/v1", api_key="sk-b",
        default_model="zai/glm", caller="zai",
    )
    return AgentConfig(
        providers={"openrouter": primary, "OpenRouter": primary, "M3": fallback},
        default_provider="openrouter",
        provider_failover_enabled=enabled,
        provider_retries=2,
        provider_retry_backoff_s=0.0,
        provider_failover_budget_s=30.0,
    )


def _runtime(cfg) -> AgentRuntime:
    return AgentRuntime(config=cfg)


def _conv(rt, **extra):
    data = dict(
        api_key=None, model="openrouter/demo", app_title="t",
        fallback_provider="M3", project_path="/tmp/proj", messages=[],
    )
    data.update(extra)
    rt._conversations["s1"] = SimpleNamespace(**data)


def _http(code: int, body: str) -> RuntimeError:
    cause = urllib.error.HTTPError("https://x", code, "err", hdrs=None, fp=None)
    exc = RuntimeError(f"OpenAI API error {code} err: {body}")
    exc.__cause__ = cause
    return exc


class TestFailoverFlagOff:
    def test_off_does_not_switch(self, monkeypatch):
        rt = _runtime(_cfg(enabled=False))
        _conv(rt)
        provider = _Provider([_http(401, "unauthorized")])
        monkeypatch.setattr("agent.runtime._get_provider", lambda _key: provider)
        try:
            rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])
            raised = False
        except RuntimeError:
            raised = True
        assert raised
        assert len(provider.calls) == 1
        assert rt._conversations["s1"].model == "openrouter/demo"


class TestFailoverChain:
    def test_401_continues_same_turn_on_fallback(self, monkeypatch):
        rt = _runtime(_cfg())
        _conv(rt)
        notices = []
        rt.set_on_provider_failover(lambda sk, reason: notices.append((sk, reason)))
        provider = _Provider([
            _http(401, "unauthorized"),
            {"choices": [{"message": {"content": "from fallback"}}]},
        ])
        monkeypatch.setattr("agent.runtime._get_provider", lambda _key: provider)
        sent = [{"role": "user", "content": "hi"}]
        result = rt._call_llm("s1", sent, [])
        assert result["choices"][0]["message"]["content"] == "from fallback"
        assert [call["model"] for call in provider.calls] == ["openrouter/demo", "zai/glm"]
        assert sent == [{"role": "user", "content": "hi"}]
        assert rt._conversations["s1"].model == "zai/glm"
        assert notices and "M3" in notices[0][1]

    def test_schema_400_does_not_fail_over(self, monkeypatch):
        rt = _runtime(_cfg())
        _conv(rt)
        provider = _Provider([_http(400, "invalid json schema in tools")])
        monkeypatch.setattr("agent.runtime._get_provider", lambda _key: provider)
        try:
            rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])
            raised = False
        except RuntimeError:
            raised = True
        assert raised
        assert len(provider.calls) == 1
        assert rt._conversations["s1"].model == "openrouter/demo"

    def test_dual_auth_halts_and_releases_claim(self, monkeypatch):
        rt = _runtime(_cfg())
        _conv(rt)
        released = []
        monkeypatch.setattr(
            "utils.work_persistence.find_live_lease",
            lambda path, holder: SimpleNamespace(unit_id="0007"),
        )
        monkeypatch.setattr(
            "utils.work_persistence.release_work",
            lambda path, unit, holder: released.append((path, unit, holder)) or True,
        )
        provider = _Provider([
            _http(401, "unauthorized"),
            _http(401, "unauthorized"),
        ])
        monkeypatch.setattr("agent.runtime._get_provider", lambda _key: provider)
        try:
            rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])
            raised = False
        except RuntimeError:
            raised = True
        assert raised
        assert len(provider.calls) == 2
        assert released == [("/tmp/proj", "0007", "s1")]

    def test_midstream_partial_is_not_sent_to_fallback(self, monkeypatch):
        rt = _runtime(_cfg())
        partial = "HALF"
        rt._conversations["s1"] = SimpleNamespace(
            api_key=None, model="openrouter/demo", app_title="t",
            fallback_provider="M3", project_path="",
            messages=[SimpleNamespace(role="assistant", content=partial, tool_calls=[])],
        )
        markers = []
        rt._on_text_delta = lambda *a, **k: markers.append(a)
        calls = []

        def stream(**kwargs):
            calls.append(kwargs["messages"])
            if len(calls) == 1:
                return {
                    "choices": [{"message": {"content": partial}}],
                    "_stream_error": {"code": 401, "message": "unauthorized"},
                }
            return {"choices": [{"message": {"content": "DONE"}}]}

        monkeypatch.setattr(rt, "_call_llm_streaming", lambda **kwargs: stream(**kwargs))
        result = rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])
        assert result["choices"][0]["message"]["content"] == "DONE"
        assert all("HALF" not in str(batch) for batch in calls)
        assert rt._conversations["s1"].messages == []
        assert any("retrying on M3" in str(item) for item in markers)

    def test_cancelled_turn_does_not_reissue(self, monkeypatch):
        rt = _runtime(_cfg())
        _conv(rt)
        rt._turn_tokens["s1"] = "other"
        provider = _Provider([_http(401, "unauthorized")])
        monkeypatch.setattr("agent.runtime._get_provider", lambda _key: provider)
        try:
            rt._call_llm("s1", [{"role": "user", "content": "hi"}], [], turn_token="this")
            raised = False
        except RuntimeError:
            raised = True
        assert raised
        assert provider.calls == []
