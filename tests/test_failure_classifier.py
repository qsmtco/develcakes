"""SPEC-22 classifier, store, and policy. Sad paths are the point."""

from __future__ import annotations

import json
import os
import time
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

    def test_bare_status_without_chain_is_transient(self):
        """SPEC-22 fix-round BUG #8: a bare provider status in the body (no
        chained HTTPError, no 'API error' prefix) must still classify."""
        assert classify_exception(RuntimeError("HTTP/1.1 500 Internal Server Error")) == FailureClass.TRANSIENT
        assert classify_exception(RuntimeError("HTTP 503 Service Unavailable")) == FailureClass.TRANSIENT
        assert classify_exception(RuntimeError("Error 429 Too Many Requests")) == FailureClass.TRANSIENT

    def test_bare_status_auth_and_model(self):
        assert classify_exception(RuntimeError("HTTP/1.1 401 Unauthorized")) == FailureClass.PERMANENT_AUTH
        assert classify_exception(RuntimeError("got status 404")) == FailureClass.PERMANENT_MODEL

    def test_suppressed_context_401_via_from_none(self):
        """`raise ... from None` keeps __context__ — _extract_status still wins."""
        h = urllib.error.HTTPError("https://x", 401, "u", hdrs=None, fp=None)
        try:
            try:
                raise h
            except urllib.error.HTTPError:
                raise RuntimeError("could not authenticate") from None
        except RuntimeError as exc:
            assert classify_exception(exc) == FailureClass.PERMANENT_AUTH

    def test_bare_status_does_not_widen_is_provider_exception(self):
        """The second-pass regex lives in classify_exception ONLY — a bare
        number in arbitrary text must not make a non-provider exc look like one."""
        from agent.llm.failure_classifier import is_provider_exception, status_code_of
        exc = RuntimeError("we processed 500 records")
        assert status_code_of(exc) is None
        assert is_provider_exception(exc) is False


class TestStreamError:
    def test_code_429(self):
        assert classify_stream_error({"code": 429, "message": "slow"}) == FailureClass.TRANSIENT

    def test_code_0_rate_limit_text(self):
        assert classify_stream_error({"code": 0, "message": "Rate limit exceeded"}) == FailureClass.TRANSIENT

    def test_code_0_useless(self):
        assert classify_stream_error({"code": 0, "message": "nope"}) == FailureClass.UNKNOWN

    def test_code_0_auth_markers_are_permanent_auth(self):
        """SPEC-22 fix-round BUG #6: a 401-shaped stream event with a
        non-numeric code carrying auth text must advance the chain, not
        burn a retry on a dead key. Shared with classify_exception."""
        for msg in ("invalid api key", "Unauthorized", "login failed"):
            assert classify_stream_error({"code": 0, "message": msg}) == FailureClass.PERMANENT_AUTH

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

    def test_summary_falls_back_to_audit_log_when_cold(self, tmp_config_dir):
        """SPEC-22 fix-round BUG #7: after a restart the deque is empty, but
        summary() must reflect fresh audit-log.jsonl events (cold-cache).

        Note: SPEC-26 also gives recent() a cold path, so the post-restart
        recent() now rehydrates from the audit log instead of returning [].
        """
        fresh = _failure(FailureClass.PERMANENT_AUTH)
        fresh.timestamp = time.time()
        record(fresh)  # writes both the deque AND the audit line
        reset_for_tests()  # simulate restart — deque cold, file warm
        # SPEC-26: recent() now rehydrates from the audit log post-restart
        assert len(recent()) == 1
        assert recent()[0].failure_class == FailureClass.PERMANENT_AUTH
        summed = summary()
        assert summed["last_24h"] == 1
        assert summed["by_class"] == {"permanent_auth": 1}
        assert summed["agents_affected"] == ["special:coder"]

    def test_summary_cold_empty_audit_log(self, tmp_config_dir):
        reset_for_tests()
        assert summary() == {"last_24h": 0, "by_class": {}, "agents_affected": []}

    def test_summary_tail_ignores_old_audit_lines(self, tmp_config_dir):
        old = _failure(FailureClass.TRANSIENT)
        old.timestamp = 1.0  # outside the 24h window
        record(old)
        reset_for_tests()
        assert summary()["last_24h"] == 0

    # ----------------------------------------------------------------
    # SPEC-26 RECENT-COLD-PATH — recent() must fall back to audit log.
    # ----------------------------------------------------------------

    def test_recent_rehydrates_from_audit_log_after_restart(self, tmp_config_dir):
        """After a restart the deque is empty, but recent() must reflect
        fresh audit-log.jsonl events (SPEC-26 cold path)."""
        fresh = _failure(FailureClass.PERMANENT_AUTH)
        fresh.timestamp = time.time()
        record(fresh)  # writes both deque AND audit line
        reset_for_tests()  # simulate restart — deque cold, file warm
        out = recent()
        assert len(out) == 1
        item = out[0]
        assert item.session_key == fresh.session_key
        assert item.provider == fresh.provider
        assert item.model == fresh.model
        assert item.failure_class == fresh.failure_class
        assert item.status_code == fresh.status_code
        assert item.detail == fresh.detail
        assert item.stage == fresh.stage
        assert item.attempt == fresh.attempt
        assert item.timestamp == fresh.timestamp

    def test_recent_warm_path_unchanged_after_record(self, tmp_config_dir):
        """record() populates the deque; recent() must read deque (warm path)
        — same behavior as before SPEC-26."""
        a = _failure(FailureClass.TRANSIENT)
        a.timestamp = 100.0
        a.session_key = "alpha"
        b = _failure(FailureClass.PERMANENT_AUTH)
        b.timestamp = 200.0
        b.session_key = "beta"
        record(a)
        record(b)
        out = recent()
        # warm path = deque order (chronological insert order, not audit file)
        assert out == [a, b]
        # both arg shapes still work
        assert recent(10) == [a, b]
        assert recent(limit=10) == [a, b]
        assert recent(limit=10, session_key="alpha") == [a]
        assert recent(limit=10, session_key="beta") == [b]
        # legacy positional form used by agent/tools.py:1418
        assert recent(10, "alpha") == [a]

    def test_recent_session_key_filter_on_cold_path(self, tmp_config_dir):
        """session_key filter must work on the cold path (rehydrated records)
        in addition to the warm path."""
        a = _failure(FailureClass.TRANSIENT)
        a.session_key = "alpha"
        a.timestamp = time.time()
        b = _failure(FailureClass.PERMANENT_AUTH)
        b.session_key = "beta"
        b.timestamp = time.time()
        record(a)
        record(b)
        reset_for_tests()  # deque cold

        # cold path, positional
        only_alpha = recent(50, "alpha")
        assert len(only_alpha) == 1
        assert only_alpha[0].session_key == "alpha"
        # cold path, keyword
        only_beta = recent(session_key="beta")
        assert len(only_beta) == 1
        assert only_beta[0].session_key == "beta"
        # cold path, mismatch
        assert recent(session_key="nope") == []

    def test_recent_limit_includes_zero(self, tmp_config_dir):
        """limit==0 must return [] on both warm and cold paths."""
        record(_failure(FailureClass.TRANSIENT))
        assert recent(0) == []
        reset_for_tests()  # cold path
        assert recent(0) == []

        # and a normal limit still works on the cold path
        for i in range(5):
            item = _failure(FailureClass.TRANSIENT, provider=f"p{i}")
            item.timestamp = time.time()
            record(item)
        reset_for_tests()
        last3 = recent(3)
        assert len(last3) == 3
        # "last 3 in file order" = final 3 records written
        assert [x.provider for x in last3] == ["p2", "p3", "p4"]

    def test_recent_corrupt_tail_lines_skipped(self, tmp_config_dir):
        """Malformed lines in the audit tail must not raise; valid
        provider_failure records are still returned."""
        from utils.config import get_config_dir
        path = os.path.join(get_config_dir(), "audit-log.jsonl")
        valid_a = _failure(FailureClass.PERMANENT_AUTH)
        valid_a.timestamp = time.time()
        valid_b = _failure(FailureClass.TRANSIENT)
        valid_b.timestamp = time.time()
        # deque is empty (setup_method reset); write to audit file directly.
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('not json at all\n')
            handle.write('{"kind": "unrelated", "timestamp": 1.0}\n')
            handle.write(json.dumps({
                "kind": "provider_failure",
                "timestamp": valid_a.timestamp,
                "session_key": valid_a.session_key,
                "provider": valid_a.provider,
                "model": valid_a.model,
                "failure_class": "permanent_auth",
                "status_code": valid_a.status_code,
                "detail": valid_a.detail,
                "stage": valid_a.stage,
                "attempt": valid_a.attempt,
            }) + "\n")
            handle.write('{"kind": "provider_failure", "timestamp": "oops"}\n')
            handle.write('{"kind": "provider_failure", "timestamp": null}\n')
            handle.write('{"kind": "provider_failure", "timestamp": true}\n')
            handle.write(json.dumps({
                "kind": "provider_failure",
                "timestamp": valid_b.timestamp,
                "session_key": valid_b.session_key,
                "provider": valid_b.provider,
                "model": valid_b.model,
                "failure_class": "transient",
                "status_code": valid_b.status_code,
                "detail": valid_b.detail,
                "stage": valid_b.stage,
                "attempt": valid_b.attempt,
            }) + "\n")
            handle.write('{"kind": "provider_failure"}\n')  # missing timestamp
            handle.write('{}\n')  # all fields missing
        out = recent(20)
        # only the two valid records should be rehydrated
        assert len(out) == 2
        assert out[0].session_key == valid_a.session_key
        assert out[1].session_key == valid_b.session_key
        assert out[0].failure_class == FailureClass.PERMANENT_AUTH
        assert out[1].failure_class == FailureClass.TRANSIENT

    def test_recent_unknown_failure_class_on_cold_path_does_not_raise(self, tmp_config_dir):
        """A valid-JSON provider_failure line carrying a failure_class value
        written by an older build must NOT raise in the cold path; it degrades
        to FailureClass.UNKNOWN (SPEC-26 BUG #1)."""
        from utils.config import get_config_dir
        path = os.path.join(get_config_dir(), "audit-log.jsonl")
        now = time.time()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "kind": "provider_failure",
                "timestamp": now,
                "session_key": "special:coder",
                "provider": "OpenRouter",
                "model": "openrouter/demo",
                "failure_class": "galactic_drift",
                "status_code": 500,
                "detail": "detail",
                "stage": "request",
                "attempt": 1,
            }) + "\n")
        out = recent()  # must not raise
        assert len(out) == 1
        assert out[0].failure_class == FailureClass.UNKNOWN
        # the rest of the record still round-trips
        assert out[0].session_key == "special:coder"
        assert out[0].provider == "OpenRouter"

    def test_recent_cold_path_survives_non_numeric_attempt(self, tmp_config_dir):
        """A non-numeric attempt on disk must NOT raise in the cold path; it
        degrades to 0 while the surrounding record still rehydrates
        (SPEC-26 BUG #2)."""
        from utils.config import get_config_dir
        path = os.path.join(get_config_dir(), "audit-log.jsonl")
        now = time.time()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "kind": "provider_failure",
                "timestamp": now,
                "session_key": "special:coder",
                "provider": "OpenRouter",
                "model": "openrouter/demo",
                "failure_class": "transient",
                "status_code": 502,
                "detail": "detail",
                "stage": "request",
                "attempt": "abc",
            }) + "\n")
        out = recent()  # must not raise
        assert len(out) == 1
        # hostile attempt degrades, does not propagate
        assert out[0].attempt == 0
        # every other field still rehydrates intact
        assert out[0].session_key == "special:coder"
        assert out[0].provider == "OpenRouter"
        assert out[0].model == "openrouter/demo"
        assert out[0].failure_class == FailureClass.TRANSIENT
        assert out[0].status_code == 502
        assert out[0].stage == "request"
        assert out[0].timestamp == now

    def test_recent_cold_path_status_code_bool_rejected(self, tmp_config_dir):
        """status_code==true must degrade to None (bool is not an int status
        code), and a timestamp==true line must be skipped entirely rather than
        rehydrated as 1.0 (SPEC-26 BUG #3)."""
        from utils.config import get_config_dir
        path = os.path.join(get_config_dir(), "audit-log.jsonl")
        now = time.time()
        with open(path, "w", encoding="utf-8") as handle:
            # valid timestamp, hostile bool status_code -> kept, status_code None
            handle.write(json.dumps({
                "kind": "provider_failure",
                "timestamp": now,
                "session_key": "special:coder",
                "provider": "OpenRouter",
                "model": "openrouter/demo",
                "failure_class": "transient",
                "status_code": True,
                "detail": "detail",
                "stage": "request",
                "attempt": 1,
            }) + "\n")
            # hostile bool timestamp -> whole record skipped
            handle.write(json.dumps({
                "kind": "provider_failure",
                "timestamp": True,
                "session_key": "special:debugger",
                "provider": "OpenRouter",
                "model": "openrouter/demo",
                "failure_class": "transient",
                "status_code": 500,
                "detail": "detail",
                "stage": "request",
                "attempt": 1,
            }) + "\n")
        out = recent()  # must not raise
        # the timestamp-bool line is dropped, not coerced
        assert len(out) == 1
        assert out[0].session_key == "special:coder"
        assert out[0].status_code is None
        # the rest of the surviving record still round-trips
        assert out[0].provider == "OpenRouter"
        assert out[0].failure_class == FailureClass.TRANSIENT
        assert out[0].stage == "request"
        assert out[0].attempt == 1
        assert out[0].timestamp == now

    def test_recent_cold_path_negative_attempt_preserved(self, tmp_config_dir):
        """A negative attempt is preserved verbatim on the cold path — it is
        display-only for fidelity and must never be clamped to 0."""
        from utils.config import get_config_dir
        path = os.path.join(get_config_dir(), "audit-log.jsonl")
        now = time.time()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "kind": "provider_failure",
                "timestamp": now,
                "session_key": "special:coder",
                "provider": "OpenRouter",
                "model": "openrouter/demo",
                "failure_class": "transient",
                "status_code": 500,
                "detail": "detail",
                "stage": "request",
                "attempt": -3,
            }) + "\n")
        out = recent()  # must not raise
        assert len(out) == 1
        # preserved verbatim, NOT clamped to 0
        assert out[0].attempt == -3
        assert out[0].attempt != 0
        # surrounding record unaffected
        assert out[0].session_key == "special:coder"
        assert out[0].status_code == 500
        assert out[0].timestamp == now


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

    def test_provider_network_error_without_marker_is_recorded(self, tmp_config_dir):
        """REGRESSION GUARD (SPEC-22 audit, BUG #1): a provider call raising a
        network/HTTP error carries NO `_crabcakes_context` marker — the narrow
        handlers only catch (IndexError, KeyError, TypeError, ValueError).
        The generic tool-loop handler MUST still classify+record it.

        A guard like `if getattr(e, "_crabcakes_context", None) is not None:`
        (proposed in the audit's BUG #1) silently suppresses this — the whole
        point of the SPEC-22 store. This test fails loudly if that guard
        (or any origin-marker gate) is reintroduced.
        """
        import unittest.mock

        from agent.config import AgentConfig, LLMProviderConfig
        from agent.runtime import AgentRuntime

        reset_for_tests()
        card = LLMProviderConfig(
            name="OpenRouter", base_url="https://x/v1", api_key="k",
            default_model="openrouter/demo", caller="openrouter",
        )
        cfg = AgentConfig(
            providers={"openrouter": card, "OpenRouter": card},
            default_provider="openrouter", default_model="openrouter/demo",
            provider_failover_enabled=False, auto_save_conversations=False,
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = "special:coder"
        rt.create_conversation("Coder", sk, "/tmp")

        class _Provider:
            def call(self, **kwargs):
                cause = urllib.error.HTTPError("https://x/v1", 401, "unauthorized", hdrs=None, fp=None)
                err = RuntimeError("OpenAI API error 401 unauthorized: bad key")
                err.__cause__ = cause
                raise err
            stream = None

        with unittest.mock.patch("agent.runtime._get_provider", lambda _key: _Provider()):
            rt._run_loop(sk, "hi")

        recorded = recent()
        assert len(recorded) == 1, (
            "genuine provider 401 was NOT recorded — an origin-marker guard "
            f"suppresses the classification path (recorded={recorded!r})"
        )
        assert recorded[0].failure_class == FailureClass.PERMANENT_AUTH
        rt.stop()


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

    def test_same_card_fallback_warns_for_bare_name(self, tmp_config_dir, monkeypatch):
        """SPEC-22 fix-round BUG #4: `llm_name` is a card display name (no
        vendor prefix), so the same-card warning must fire on plain names.
        The old `"/" not in primary` clause was dead — this pins the removal."""
        from models.providers import ProviderConfig
        from utils.providers_store import save_providers
        from utils.startup_checks import startup_warnings
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        monkeypatch.delenv("OPENCLAW_BRAVE_API_KEY", raising=False)
        save_providers([ProviderConfig(
            name="M3", base_url="https://x/v1", api_key="k",
            default_model="zai/glm", caller="zai",
        )])
        monkeypatch.setattr(
            "utils.agent_defs.load_agent_defs",
            lambda: [{"name": "Coder", "llm_name": "M3", "fallback_provider": "M3"}],
        )
        found = startup_warnings()
        assert any("same card as the primary" in item for item in found)


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

    def test_empty_model_does_not_open_a_socket(self, tmp_config_dir, monkeypatch):
        from utils.providers_store import save_providers
        from models.providers import ProviderConfig
        save_providers([ProviderConfig(
            name="DecisionModel", base_url="https://jev.example/v1",
            api_key="jev-key", default_model="", caller="",
        )])

        def boom(req, timeout=None):
            raise AssertionError("urlopen must not be called")

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", boom)
        assert classify_unknown("mystery", 500) is None
        from agent.llm.jev_client import probe_decision_model
        result = probe_decision_model(ProviderConfig(
            name="DecisionModel", base_url="https://jev.example/v1",
            api_key="jev-key", default_model="", caller="",
        ))
        assert result.ok is False
        assert "default_model" in (result.error or "")

    def test_keyed_card_classifies_via_mock_endpoint(self, tmp_config_dir, monkeypatch):
        """Card present + keyed → a REAL typed-decision call. The endpoint is
        mocked (no network in tests): a well-formed choice answer with good
        confidence must produce the corresponding FailureClass."""
        from utils.providers_store import save_providers
        from models.providers import ProviderConfig
        save_providers([ProviderConfig(
            name="DecisionModel", base_url="https://jev.example/v1",
            api_key="jev-key", default_model="typesafe/system-one", caller="",
        )])

        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({
                    "model": "jev-test",
                    "answers": {"failure_class": {
                        "type": "choice", "choice": "transient",
                        "probabilities": {"transient": 0.9},
                        "confidence": 0.9,
                    }},
                }).encode()

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            captured["auth"] = req.headers.get("Authorization")
            captured["timeout"] = timeout
            return _Resp()

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        result = classify_unknown("429 rate limited", 429)
        from agent.llm.failure_classifier import FailureClass
        assert result is FailureClass.TRANSIENT
        assert captured["url"].endswith("/v1/systemone")
        assert captured["auth"] == "Bearer jev-key"
        assert captured["body"]["model"] == "typesafe/system-one"
        assert captured["body"]["questions"]["failure_class"]["type"] == "choice"
        assert captured["timeout"] is not None and captured["timeout"] <= 2.5

    def test_keyed_card_low_confidence_returns_none(self, tmp_config_dir, monkeypatch):
        """Confidence below the floor → None (fail open, not a guess)."""
        from utils.providers_store import save_providers
        from models.providers import ProviderConfig
        save_providers([ProviderConfig(
            name="DecisionModel", base_url="https://jev.example/v1",
            api_key="jev-key", default_model="typesafe/system-one", caller="",
        )])

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({
                    "answers": {"failure_class": {
                        "type": "choice", "choice": "transient",
                        "confidence": 0.3,
                    }},
                }).encode()

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp())
        assert classify_unknown("mystery", None) is None

    def test_keyed_card_invalid_choice_returns_none(self, tmp_config_dir, monkeypatch):
        """An answer outside the closed class set → None (shape guard)."""
        from utils.providers_store import save_providers
        from models.providers import ProviderConfig
        save_providers([ProviderConfig(
            name="DecisionModel", base_url="https://jev.example/v1",
            api_key="jev-key", default_model="typesafe/system-one", caller="",
        )])

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({
                    "answers": {"failure_class": {
                        "type": "choice", "choice": "purple", "confidence": 0.99,
                    }},
                }).encode()

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp())
        assert classify_unknown("mystery", None) is None

    def test_keyed_card_network_failure_returns_none(self, tmp_config_dir, monkeypatch):
        """Endpoint unreachable → None, never an exception (fail-open floor)."""
        from utils.providers_store import save_providers
        from models.providers import ProviderConfig
        save_providers([ProviderConfig(
            name="DecisionModel", base_url="https://jev.example/v1",
            api_key="jev-key", default_model="typesafe/system-one", caller="",
        )])

        import urllib.request

        def boom(req, timeout=None):
            raise OSError("network down")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        assert classify_unknown("mystery", None) is None

    def test_keyed_card_shape_drift_returns_none(self, tmp_config_dir, monkeypatch):
        """A noul answer (wrong type) or missing keys → None."""
        from utils.providers_store import save_providers
        from models.providers import ProviderConfig
        save_providers([ProviderConfig(
            name="DecisionModel", base_url="https://jev.example/v1",
            api_key="jev-key", default_model="typesafe/system-one", caller="",
        )])

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({
                    "answers": {"failure_class": {"type": "noul", "noul": 0.9}},
                }).encode()

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp())
        assert classify_unknown("mystery", None) is None

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


class TestAuditTailRecordBound:
    """SPEC-28: _tail_audit_records() bounds its window by RECORDS, not raw
    lines, so unrelated audit traffic (agent_call etc.) can no longer evict
    provider failures out of the cold-read window."""

    def setup_method(self):
        reset_for_tests()

    def _write_log(self, lines):
        """Cold idiom: write audit-log.jsonl directly, never via record()."""
        from utils.config import get_config_dir
        path = os.path.join(get_config_dir(), "audit-log.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(json.dumps(line) + "\n" for line in lines)
        return path

    def _pf(self, provider, timestamp=None):
        now = time.time()
        return {
            "kind": "provider_failure",
            "timestamp": now if timestamp is None else timestamp,
            "session_key": "special:default",
            "provider": provider,
            "model": "m",
            "failure_class": "transient",
            "status_code": None,
            "detail": "d",
        }

    def test_cold_read_survives_unrelated_traffic(self, tmp_config_dir):
        """50 failures interleaved with 150 unrelated lines: all 50 survive
        the cold read, where a raw-line window would keep only the last ~100
        lines and lose most failures."""
        from utils.provider_failure_store import _tail_audit_records
        rows = []
        for i in range(50):
            rows.append(self._pf(f"fail-{i}"))
            for j in range(3):
                rows.append({"kind": "agent_call", "tool": f"t{i}-{j}"})
        self._write_log(rows)

        records = _tail_audit_records()
        assert len(records) == 50
        assert [r["provider"] for r in records] == [f"fail-{i}" for i in range(50)]

        # recent() rehydrates the same 50 from the cold log. limit=50 is
        # required: recent() defaults to limit=20, so the bare call would
        # return the newest 20 and cap-test the default rather than eviction.
        rehydrated = recent(limit=50)
        assert len(rehydrated) == 50
        assert [f.provider for f in rehydrated] == [f"fail-{i}" for i in range(50)]

    def test_summary_cold_ignores_unrelated_traffic(self, tmp_config_dir):
        """summary()'s cold path must count all 50 failures, not only those
        inside a raw-line window."""
        rows = []
        for i in range(50):
            rows.append(self._pf(f"fail-{i}"))
            for j in range(3):
                rows.append({"kind": "agent_call", "tool": f"t{i}-{j}"})
        self._write_log(rows)

        assert summary()["last_24h"] == 50

    def test_stops_at_matched_record_cap(self, tmp_config_dir):
        """150 failures: the scan stops at _CAP matches, returning the LAST
        _CAP records in file order (oldest -> newest)."""
        import utils.provider_failure_store as pfs
        from utils.provider_failure_store import _tail_audit_records
        rows = [self._pf(f"fail-{i}") for i in range(150)]
        self._write_log(rows)

        records = _tail_audit_records()
        assert len(records) == pfs._CAP
        assert [r["provider"] for r in records] == [f"fail-{i}" for i in range(150 - pfs._CAP, 150)]

    def test_multichunk_fragment_boundary(self, tmp_config_dir, monkeypatch):
        """A provider_failure record straddling a chunk boundary must be
        reassembled, parsed exactly once, and not duplicated."""
        import utils.provider_failure_store as pfs
        from utils.provider_failure_store import _tail_audit_records
        filler = self._pf("filler", timestamp=1.0)
        rows = [filler] * 40
        # A deliberately long detail guarantees a boundary-spanning record.
        spanning = self._pf("spanning")
        spanning["detail"] = "x" * 400
        self._write_log(rows + [spanning])

        # 256-byte chunks over a multi-KB log => >= 3 chunks read.
        monkeypatch.setattr(pfs, "_TAIL_READ_CHUNK", 256)
        records = _tail_audit_records()

        assert len(records) == 41
        providers = [r["provider"] for r in records]
        assert providers.count("spanning") == 1
        assert providers[-1] == "spanning"
        assert records[-1]["detail"] == "x" * 400

    def test_scan_byte_cap_truncates_cleanly(self, tmp_config_dir, monkeypatch):
        """When the scan cap is hit before any match, return [] rather than
        raising or scanning the whole file."""
        import utils.provider_failure_store as pfs
        from utils.provider_failure_store import _tail_audit_records
        # A large log whose newest records are all unrelated.
        rows = [{"kind": "agent_call", "pad": "y" * 200} for _ in range(200)]
        self._write_log(rows)

        monkeypatch.setattr(pfs, "_TAIL_MAX_SCAN_BYTES", 64)
        records = _tail_audit_records()
        assert records == []

    def test_missing_log_returns_empty(self, tmp_config_dir):
        """No audit-log.jsonl at all: the tail read must yield []."""
        from utils.provider_failure_store import _tail_audit_records

        assert _tail_audit_records() == []
