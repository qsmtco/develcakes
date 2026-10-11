"""SPEC-22 classifier, store, and policy. Sad paths are the point."""

from __future__ import annotations

import json
import os
import urllib.error

from agent.llm.failure_classifier import (
    FailureClass,
    classify_exception,
    classify_http_400,
    classify_stream_error,
    sanitize_detail,
)
from agent.llm.failure_policy import Decision, RetryConfig, decide
from agent.llm.jev_client import classify_unknown
from utils.provider_failure_store import recent, record, reset_for_tests, summary


def _failure(kind: FailureClass, provider: str = "OpenRouter", attempt: int = 1):
    from agent.llm.failure_classifier import ProviderFailure
    return ProviderFailure(
        session_key="special:coder",
        provider=provider,
        model="openrouter/demo",
        failure_class=kind,
        status_code=400 if kind == FailureClass.BAD_REQUEST else 401,
        detail="detail",
        stage="request",
        attempt=attempt,
        timestamp=1_700_000_000.0,
    )


class TestClassifyHttp400:
    def test_schema_is_bad_request(self):
        assert classify_http_400("JSON schema validation failed for tools") == FailureClass.BAD_REQUEST

    def test_unsupported_parameter_advances_class(self):
        body = "Invalid arguments passed to the model: reasoning_effort"
        assert classify_http_400(body) == FailureClass.PERMANENT_MODEL

    def test_ambiguous_is_unknown(self):
        assert classify_http_400("400 bad request") == FailureClass.UNKNOWN

    def test_schema_wins_when_both_markers_present(self):
        assert classify_http_400("schema rejected reasoning") == FailureClass.BAD_REQUEST

    def test_empty_and_none(self):
        assert classify_http_400("") == FailureClass.UNKNOWN
        assert classify_http_400(None) == FailureClass.UNKNOWN  # type: ignore[arg-type]


class TestClassifyException:
    def test_http_401_on_cause(self):
        cause = urllib.error.HTTPError("https://x", 401, "unauthorized", hdrs=None, fp=None)
        exc = RuntimeError("OpenAI API error 401 unauthorized: no")
        exc.__cause__ = cause
        assert classify_exception(exc, "openrouter", "m") == FailureClass.PERMANENT_AUTH

    def test_http_429(self):
        exc = urllib.error.HTTPError("https://x", 429, "rate", hdrs=None, fp=None)
        assert classify_exception(exc) == FailureClass.TRANSIENT

    def test_http_500(self):
        exc = urllib.error.HTTPError("https://x", 500, "boom", hdrs=None, fp=None)
        assert classify_exception(exc) == FailureClass.TRANSIENT

    def test_http_404(self):
        exc = urllib.error.HTTPError("https://x", 404, "missing", hdrs=None, fp=None)
        assert classify_exception(exc) == FailureClass.PERMANENT_MODEL

    def test_wrapped_400_schema(self):
        cause = urllib.error.HTTPError("https://x", 400, "bad", hdrs=None, fp=None)
        exc = RuntimeError("OpenAI API error 400 bad: invalid json in body")
        exc.__cause__ = cause
        assert classify_exception(exc) == FailureClass.BAD_REQUEST

    def test_message_only_401(self):
        exc = RuntimeError("OpenAI API error 401 unauthorized: denied")
        assert classify_exception(exc) == FailureClass.PERMANENT_AUTH

    def test_minimax_login_is_auth_not_transient(self):
        exc = RuntimeError("MiniMax API error (status_code=1004): login fail")
        assert classify_exception(exc) == FailureClass.PERMANENT_AUTH

    def test_timeout(self):
        assert classify_exception(TimeoutError("timed out")) == FailureClass.TRANSIENT

    def test_garbage_type_and_empty(self):
        assert classify_exception("nope") == FailureClass.UNKNOWN  # type: ignore[arg-type]
        assert classify_exception(RuntimeError("")) == FailureClass.UNKNOWN

    def test_urlerror_dns(self):
        import socket
        exc = urllib.error.URLError(socket.gaierror("dns"))
        assert classify_exception(exc) == FailureClass.TRANSIENT


class TestStreamError:
    def test_code_429(self):
        assert classify_stream_error({"code": 429, "message": "slow"}) == FailureClass.TRANSIENT

    def test_code_0_rate_limit_text(self):
        assert classify_stream_error({"code": 0, "message": "Rate limit exceeded"}) == FailureClass.TRANSIENT

    def test_code_0_useless(self):
        assert classify_stream_error({"code": 0, "message": "nope"}) == FailureClass.UNKNOWN

    def test_not_a_dict(self):
        assert classify_stream_error(None) == FailureClass.UNKNOWN
        assert classify_stream_error("400") == FailureClass.UNKNOWN

    def test_code_400_parameter(self):
        payload = {"code": 400, "message": "unsupported parameter: reasoning"}
        assert classify_stream_error(payload) == FailureClass.PERMANENT_MODEL


class TestSanitize:
    def test_strips_key_and_userinfo_and_caps(self):
        raw = "https://user:secret@example.com/v1 sk-abcDEF123 token " + ("x" * 300)
        detail = sanitize_detail(raw)
        assert "secret" not in detail
        assert "sk-abcDEF123" not in detail
        assert len(detail) <= 200

    def test_none(self):
        assert sanitize_detail(None) == ""


class TestStore:
    def setup_method(self):
        reset_for_tests()

    def test_round_trip_and_idempotent_record(self, tmp_config_dir):
        item = _failure(FailureClass.PERMANENT_AUTH)
        record(item)
        record(item)
        assert recent() == [item]
        from utils.config import get_config_dir
        path = os.path.join(get_config_dir(), "audit-log.jsonl")
        with open(path, encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        assert len(lines) == 1
        assert lines[0]["kind"] == "provider_failure"
        assert lines[0]["failure_class"] == "permanent_auth"
        assert "secret" not in lines[0]["detail"]

    def test_cap_and_session_filter(self, tmp_config_dir):
        for i in range(105):
            record(_failure(FailureClass.TRANSIENT, provider=f"p{i}"))
        assert len(recent(limit=1000)) == 100
        other = _failure(FailureClass.UNKNOWN)
        other.session_key = "special:other"
        record(other)
        assert recent(session_key="special:other") == [other]

    def test_rejects_garbage(self, tmp_config_dir):
        record("nope")  # type: ignore[arg-type]
        assert recent() == []

    def test_summary_ignores_old_timestamp(self, tmp_config_dir):
        old = _failure(FailureClass.TRANSIENT)
        old.timestamp = 1.0
        record(old)
        assert summary()["last_24h"] == 0


class TestPolicy:
    def test_pm_correction_401_advances_chain(self):
        decision = decide(_failure(FailureClass.PERMANENT_AUTH), 1, ["OpenRouter", "M3"])
        assert decision.action == "advance_chain"
        assert decision.provider == "M3"

    def test_400_schema_never_fails_over(self):
        decision = decide(_failure(FailureClass.BAD_REQUEST), 1, ["OpenRouter", "M3"])
        assert decision.action == "halt"

    def test_400_unsupported_parameter_advances_chain(self):
        decision = decide(_failure(FailureClass.PERMANENT_MODEL), 1, ["OpenRouter", "M3"])
        assert decision.action == "advance_chain"
        assert decision.provider == "M3"

    def test_400_ambiguous_is_unknown_and_does_not_advance(self):
        first = decide(_failure(FailureClass.UNKNOWN), 1, ["OpenRouter", "M3"])
        assert first.action == "retry"
        assert first.provider == "OpenRouter"
        second = decide(_failure(FailureClass.UNKNOWN), 2, ["OpenRouter", "M3"])
        assert second.action == "halt"

    def test_transient_retries_then_advances(self):
        cfg = RetryConfig(provider_retries=2)
        assert decide(_failure(FailureClass.TRANSIENT), 1, ["A", "B"], cfg).action == "retry"
        assert decide(_failure(FailureClass.TRANSIENT), 2, ["A", "B"], cfg).action == "retry"
        third = decide(_failure(FailureClass.TRANSIENT), 3, ["A", "B"], cfg)
        assert third.action == "advance_chain"
        assert third.provider == "B"

    def test_same_name_fallback_is_not_an_advance(self):
        decision = decide(_failure(FailureClass.PERMANENT_AUTH), 1, ["M3", "M3"])
        assert decision.action == "halt"

    def test_empty_chain_and_bad_attempt(self):
        decision = decide(_failure(FailureClass.TRANSIENT, provider="Z"), True, [], None)  # type: ignore[arg-type]
        assert isinstance(decision, Decision)
        assert decision.action in ("retry", "halt")

    def test_jev_stub_is_none(self):
        assert classify_unknown("anything", 400) is None


class TestRuntimeRecordOnce:
    def setup_method(self):
        reset_for_tests()

    def test_generic_keyerror_is_not_a_provider_failure(self, tmp_config_dir):
        from agent.config import AgentConfig
        from agent.runtime import AgentRuntime
        rt = AgentRuntime(config=AgentConfig())
        exc = KeyError("tool bug")
        assert rt._attach_provider_failure(exc, "s", "request") is None
        assert recent() == []

    def test_http_error_records_once(self, tmp_config_dir):
        from agent.config import AgentConfig
        from agent.runtime import AgentRuntime, TurnResult, TurnStatus
        rt = AgentRuntime(config=AgentConfig())
        exc = urllib.error.HTTPError("https://x", 401, "no", hdrs=None, fp=None)
        rt._attach_provider_failure(exc, "special:coder", "request")
        rt._attach_provider_failure(exc, "special:coder", "request_boundary")
        result = rt._terminate_turn(TurnResult(
            status=TurnStatus.FAILED,
            session_key="special:coder",
            turn_token=object(),
            error=exc,
        ))
        assert result is not None
        assert recent()[0].failure_class.value == "permanent_auth"
        rt._record_attached_failure(exc)
        assert len(recent()) == 1

    def test_call_boundary_valueerror_is_recorded(self, tmp_config_dir):
        from agent.config import AgentConfig
        from agent.runtime import AgentRuntime
        rt = AgentRuntime(config=AgentConfig())
        exc = ValueError("Expecting value: line 1")
        failure = rt._attach_provider_failure(exc, "s", "request_boundary")
        assert failure is not None
        assert failure.failure_class.value == "unknown"


class TestStartup:
    def test_missing_brave_key_named(self, tmp_config_dir, monkeypatch):
        from utils.startup_checks import startup_warnings
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        monkeypatch.delenv("OPENCLAW_BRAVE_API_KEY", raising=False)
        monkeypatch.setattr(
            "utils.agent_defs.load_agent_defs",
            lambda: [{"name": "Coder", "tools": ["web_search"], "fallback_provider": "M3", "llm_name": "OpenRouter"}],
        )
        monkeypatch.setattr("utils.providers_store.load_providers", list)
        found = startup_warnings()
        assert any("BRAVE_API_KEY" in item for item in found)
        assert any("M3" in item for item in found)


def test_policy_table_does_not_import_jev(monkeypatch):
    """Enumerated decide() must not call the JEV stub."""
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("jev consulted")

    monkeypatch.setattr("agent.llm.jev_client.classify_unknown", boom)
    for kind in FailureClass:
        decide(_failure(kind), 1, ["A", "B"])
    assert called["n"] == 0


# ═══════════════════════════════════════════════════════════════════
#  SPEC-22 §2.8 — DecisionModel card lookup (fix round JOB 2)
# ═══════════════════════════════════════════════════════════════════

class TestDecisionModelLookup:
    def test_no_card_returns_none(self, tmp_config_dir):
        """Deterministic floor: no card → None."""
        assert classify_unknown("anything", 500) is None

    def test_keyless_card_returns_none(self, tmp_config_dir):
        from utils.providers_store import save_providers
        from models.providers import ProviderConfig
        save_providers([ProviderConfig(
            name="DecisionModel", base_url="https://jev.example/v1",
            api_key="", default_model="typesafe/system-one", caller="",
        )])
        assert classify_unknown("anything", 400) is None

    def test_keyed_card_still_none_until_adapter(self, tmp_config_dir):
        """Card present + keyed, but the Stage-2 adapter is not shipped —
        classify_unknown honestly stays None (documented, not faked)."""
        from utils.providers_store import save_providers
        from models.providers import ProviderConfig
        save_providers([ProviderConfig(
            name="DecisionModel", base_url="https://jev.example/v1",
            api_key="jev-key", default_model="typesafe/system-one", caller="",
        )])
        assert classify_unknown("anything", 400) is None

    def test_load_decision_card_finds_by_constant(self, tmp_config_dir):
        from agent.llm.jev_client import load_decision_card
        from utils.providers_store import save_providers
        from models.providers import ProviderConfig
        save_providers([
            ProviderConfig(name="Chat", base_url="u", api_key="k",
                           default_model="openai/x", caller="openai"),
            ProviderConfig(name="DecisionModel", base_url="https://jev/v1",
                           api_key="jev-key", default_model="typesafe/s1", caller=""),
        ])
        card = load_decision_card()
        assert card is not None and card.name == "DecisionModel"


class TestStartupDecisionCard:
    def test_keyed_decision_card_no_warning(self, tmp_config_dir, monkeypatch):
        from utils.providers_store import save_providers
        from models.providers import ProviderConfig
        from utils.startup_checks import startup_warnings
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        monkeypatch.delenv("OPENCLAW_BRAVE_API_KEY", raising=False)
        monkeypatch.setattr("utils.agent_defs.load_agent_defs", list)
        save_providers([ProviderConfig(
            name="DecisionModel", base_url="https://jev/v1",
            api_key="jev-key", default_model="typesafe/s1", caller="",
        )])
        found = startup_warnings()
        assert not any("DecisionModel" in item for item in found)
