"""SPEC-22 Stage 3 chain walk. The flag defaults on."""

from __future__ import annotations

import urllib.error
from types import SimpleNamespace

import logging

from agent.config import AgentConfig, LLMProviderConfig
from agent.runtime import CANCEL_MESSAGE, AgentRuntime, TurnInactive, TurnStatus


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
        assert provider.calls[0]["timeout"] == 120
        assert rt._conversations["s1"].model == "openrouter/demo"


def test_default_failover_is_on():
    cfg = AgentConfig()
    assert cfg.provider_failover_enabled is True
    assert cfg.provider_failover_attempt_timeout_s == 8.0


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
        assert [call["timeout"] for call in provider.calls] == [8.0, 8.0]
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

    def test_dual_auth_halts(self, monkeypatch):
        rt = _runtime(_cfg())
        _conv(rt)
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

    def test_superseded_turn_does_not_reissue(self, monkeypatch):
        rt = _runtime(_cfg())
        _conv(rt)
        rt._turn_tokens["s1"] = "other"
        provider = _Provider([_http(401, "unauthorized")])
        monkeypatch.setattr("agent.runtime._get_provider", lambda _key: provider)
        try:
            rt._call_llm("s1", [{"role": "user", "content": "hi"}], [], turn_token="this")
            raised = None
        except TurnInactive as exc:
            raised = exc
        assert raised is not None and raised.reason == "superseded"
        assert provider.calls == []

    def test_cancelled_turn_does_not_reissue(self, monkeypatch):
        rt = _runtime(_cfg())
        _conv(rt)
        rt._turn_tokens["s1"] = "this"
        rt._cancelled_epochs.add("s1")
        provider = _Provider([_http(401, "unauthorized")])
        monkeypatch.setattr("agent.runtime._get_provider", lambda _key: provider)
        try:
            rt._call_llm("s1", [{"role": "user", "content": "hi"}], [], turn_token="this")
            raised = None
        except TurnInactive as exc:
            raised = exc
        assert raised is not None and raised.reason == "cancelled"
        assert provider.calls == []

    def test_tiny_budget_does_not_retry_the_same_provider(self, monkeypatch):
        cfg = _cfg()
        cfg.provider_failover_budget_s = 0.4
        rt = _runtime(cfg)
        _conv(rt)
        provider = _Provider([
            _http(429, "rate limit"),
            {"choices": [{"message": {"content": "from fallback"}}]},
        ])
        monkeypatch.setattr("agent.runtime._get_provider", lambda _key: provider)
        result = rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])
        assert result["choices"][0]["message"]["content"] == "from fallback"
        assert [call["model"] for call in provider.calls] == ["openrouter/demo", "zai/glm"]
        assert provider.calls[1]["timeout"] == 8.0

    def test_backoff_sleep_leaves_a_second_for_the_next_attempt(self, monkeypatch):
        cfg = _cfg()
        cfg.provider_retry_backoff_s = 10.0
        cfg.provider_failover_budget_s = 6.0
        rt = _runtime(cfg)
        _conv(rt)
        slept = []
        monkeypatch.setattr("agent.runtime.time.sleep", lambda seconds: slept.append(seconds))
        provider = _Provider([
            _http(429, "rate limit"),
            {"choices": [{"message": {"content": "retried"}}]},
        ])
        monkeypatch.setattr("agent.runtime._get_provider", lambda _key: provider)
        result = rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])
        assert result["choices"][0]["message"]["content"] == "retried"
        assert [call["model"] for call in provider.calls] == ["openrouter/demo", "openrouter/demo"]
        assert len(slept) == 1
        assert 4.0 < slept[0] < 10.0
        assert provider.calls[1]["timeout"] <= 6.0

    def test_chain_disables_inner_ssl_retries(self, monkeypatch):
        from agent.llm.streaming import _SSL_RETRY_BUDGET
        rt = _runtime(_cfg())
        _conv(rt)
        seen = []

        class _Seeing(_Provider):
            def call(self, **kwargs):
                seen.append(_SSL_RETRY_BUDGET.get())
                return super().call(**kwargs)

        provider = _Seeing([{"choices": [{"message": {"content": "ok"}}]}])
        monkeypatch.setattr("agent.runtime._get_provider", lambda _key: provider)
        rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])
        assert seen == [0]
        assert _SSL_RETRY_BUDGET.get() is None


def _quiet_loop(tmp_path, llm):
    """Run one coder turn whose provider call is ``llm``."""
    cfg = _cfg()
    cfg.auto_save_conversations = False
    rt = _runtime(cfg)
    sk = "agent:coder"
    token = object()
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    rt._running = True
    errors = []

    def on_error(session_key, message, _turn_token=None):
        errors.append(message)

    rt._on_error = on_error
    rt._call_llm = llm(rt)
    rt._run_loop(sk, "hello", turn_token=token)
    return rt, sk, token, errors


def test_superseded_turn_stops_without_a_failure(tmp_path, caplog):
    def llm(rt):
        def call(session_key, messages, tools, turn_token=None, **kwargs):
            rt._conversations[session_key].add_assistant_message("", [])
            rt._turn_tokens[session_key] = object()
            return AgentRuntime._call_llm(
                rt, session_key, messages, tools, turn_token,
            )
        return call

    with caplog.at_level(logging.ERROR, logger="agent.runtime"):
        rt, sk, token, errors = _quiet_loop(tmp_path, llm)
    assert errors == []
    assert "Error in tool loop" not in caplog.text
    assert "stale turn_token" not in caplog.text
    assert rt._turn_state[(sk, token)] == TurnStatus.STREAMING
    assert (sk, token) not in rt._turn_results
    last = rt._conversations[sk].messages[-1]
    assert last.content == ""


def test_cancel_during_failover_ends_cancelled(tmp_path, caplog):
    def llm(rt):
        def call(session_key, messages, tools, turn_token=None, **kwargs):
            rt._cancelled_epochs.add(session_key)
            return AgentRuntime._call_llm(
                rt, session_key, messages, tools, turn_token,
            )
        return call

    with caplog.at_level(logging.ERROR, logger="agent.runtime"):
        rt, sk, token, errors = _quiet_loop(tmp_path, llm)
    assert errors == [CANCEL_MESSAGE]
    assert "Error in tool loop" not in caplog.text
    assert rt._turn_state[(sk, token)] == TurnStatus.CANCELLED
    result = rt._turn_results[(sk, token)]
    assert result.status == TurnStatus.CANCELLED
    assert result.error == CANCEL_MESSAGE
