"""SPEC-46 wiring tests: runtime JEV refinement + jev-decision.md composition.

Covers the acceptance criteria from docs/specs/SPEC-46-JEV-WIRING.md:

- §4.1  refined UNKNOWN → concrete class on the stream-metadata path.
- §4.2  fail-open: no card / None / low-confidence / malformed / exception
         leaves the class UNKNOWN.
- §4.3  deterministic classes never consult JEV — asserted with a calls-list
         spy returning a real class (not a raising stub: the previous
         AssertionError stub was swallowed by the product's fail-open
         `except Exception:`, so the M1 guard mutant survived v1).
- §4.4  prompt-composition gating: non-coder with jev_ask gets the block;
         gateway / tool-less agents do not; coder never does.
- §4.5  the refined class reaches decide() on the `_stream_failure_exception`
         and `_attach_provider_failure` paths and changes the policy outcome.
- Mutant-kill coverage: removing the UNKNOWN-only guard (M1) fails
         test_deterministic_classes_never_consult_jev; removing the
         `refined != UNKNOWN` check (M2) fails
         test_jev_unknown_answer_never_overwrites_class_identity.
- The post_decision 2.0 s wall-clock ceiling (fix-round item 4): a mocked
         slow-drip response must stop at ~2 s and fail open to None.
"""

from __future__ import annotations

import time
import urllib.request

import pytest

from agent.config import AgentConfig, LLMProviderConfig
from agent.llm.failure_classifier import FailureClass, ProviderFailure
from agent.llm.failure_policy import decide
from agent.runtime import AgentRuntime
from models.providers import ProviderConfig
from utils.prompt_loader import compose_system_prompt
from utils.providers_store import save_providers


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    return tmp_path


@pytest.fixture(autouse=True)
def _isolate_config_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))


@pytest.fixture
def runtime(tmp_config_dir):
    primary = LLMProviderConfig(
        name="OpenRouter", base_url="https://openrouter.example/v1",
        api_key="sk-a", default_model="openrouter/demo", caller="openrouter",
    )
    cfg = AgentConfig(providers={"openrouter": primary, "OpenRouter": primary})
    rt = AgentRuntime(config=cfg)
    rt.start()
    yield rt, "s1"
    rt.stop()


@pytest.fixture
def conv(runtime, tmp_path):
    rt, sk = runtime
    rt.create_conversation("Coder", sk, str(tmp_path))
    return rt, sk


def _patch_classify(monkeypatch, fn):
    monkeypatch.setattr("agent.llm.jev_client.classify_unknown", fn)


def _assert_unknown_halts_at_attempt_two(sk: str):
    unrefined = ProviderFailure(
        session_key=sk, provider="openai", model="openai/gpt-4o",
        failure_class=FailureClass.UNKNOWN, status_code=None,
        detail="x", stage="stream", attempt=1, timestamp=0.0,
    )
    assert decide(unrefined, 2, ["openai", "M3"]).action == "halt"


class TestJevRefinesUnknown:
    def test_high_confidence_answer_replaces_unknown(self, conv, monkeypatch):
        rt, sk = conv
        _patch_classify(monkeypatch, lambda d, s=None: FailureClass.TRANSIENT)
        failure = rt._classified_stream_error(sk, None, "stream")
        assert failure.failure_class is FailureClass.TRANSIENT

    def test_refinement_reaches_decide_via_stream_failure(self, conv, monkeypatch):
        """SPEC-46 §4.5: on the mid-stream path the refined class must reach
        `decide()` and change the outcome (retry where UNKNOWN would halt)."""
        rt, sk = conv
        _patch_classify(monkeypatch, lambda d, s=None: FailureClass.TRANSIENT)

        resp = {
            "choices": [{"message": {"content": ""}}],
            "_stream_error": {"code": 0, "message": "something inscrutable"},
        }
        carrier = rt._stream_failure_exception(sk, resp)
        assert carrier is not None
        failure = carrier._failure
        assert failure.failure_class is FailureClass.TRANSIENT
        assert decide(failure, 2, ["openai", "M3"]).action == "retry"
        _assert_unknown_halts_at_attempt_two(sk)

    def test_refinement_reaches_decide_via_attach(self, conv, monkeypatch):
        """SPEC-46 §4.5: same guarantee on the raised-exception / request-
        boundary path (the `_attach_provider_failure` producer)."""
        rt, sk = conv
        _patch_classify(monkeypatch, lambda d, s=None: FailureClass.TRANSIENT)

        exc = RuntimeError("something inscrutable")
        failure = rt._attach_provider_failure(exc, sk, "request_boundary")
        assert isinstance(failure, ProviderFailure)
        assert failure.failure_class is FailureClass.TRANSIENT
        assert decide(failure, 2, ["openai", "M3"]).action == "retry"
        _assert_unknown_halts_at_attempt_two(sk)


class TestJevFailOpen:
    def test_none_answer_keeps_unknown(self, conv, monkeypatch):
        rt, sk = conv
        _patch_classify(monkeypatch, lambda d, s=None: None)
        failure = rt._classified_stream_error(sk, None, "stream")
        assert failure.failure_class is FailureClass.UNKNOWN

    def test_downward_reclassification_to_unknown_ignored(self, conv, monkeypatch):
        rt, sk = conv
        _patch_classify(monkeypatch, lambda d, s=None: FailureClass.UNKNOWN)
        failure = rt._classified_stream_error(sk, None, "stream")
        assert failure.failure_class is FailureClass.UNKNOWN

    def test_jev_exception_fails_open_keeps_unknown(self, conv, monkeypatch):
        rt, sk = conv

        def _boom(detail, status_code=None):
            raise RuntimeError("JEV down")

        _patch_classify(monkeypatch, _boom)
        failure = rt._classified_stream_error(sk, None, "stream")
        assert failure.failure_class is FailureClass.UNKNOWN

    def test_jev_unknown_answer_never_overwrites_class_identity(self, conv, monkeypatch):
        """Invariant §3.2: an 'unknown' answer is never applied as a refinement.

        The stub returns the raw string (not the enum) to prove the guard is
        structural rather than luck of enum identity. If the
        `refined != UNKNOWN` check is removed (M2), the class field is
        overwritten with the raw value and this test fails.
        """
        rt, sk = conv
        _patch_classify(monkeypatch, lambda d, s=None: "unknown")
        failure = rt._classified_stream_error(sk, None, "stream")
        assert failure.failure_class is FailureClass.UNKNOWN


class TestJevNotConsultedForDeterministicClasses:
    def test_deterministic_classes_never_consult_jev(self, conv, monkeypatch):
        """SPEC-46 §4.3 / invariant §3.2. The spy returns a real class so the
        assertion does not depend on an exception escaping the fail-open
        `except Exception:` (that is how M1 survived v1)."""
        rt, sk = conv
        calls = []

        def _spy(detail, status_code=None):
            calls.append((detail, status_code))
            return FailureClass.TRANSIENT

        _patch_classify(monkeypatch, _spy)

        transient = rt._classified_stream_error(
            sk, {"code": 429, "message": "rate limit"}, "stream")
        auth = rt._classified_stream_error(
            sk, {"code": 401, "message": "bad key"}, "stream")

        assert transient.failure_class is FailureClass.TRANSIENT
        assert auth.failure_class is FailureClass.PERMANENT_AUTH
        assert calls == [], f"JEV consulted for deterministic classes: {calls}"


class TestJevDecisionPromptComposition:
    # Markers are template-unique phrases; "jev_ask" alone would also appear
    # in the agent-tools/coder tool list, so it is not a usable marker.
    _MARKER_HEADING = "## When you are uncertain, use a decision oracle"
    _MARKER_BODY = "no leading phrasing"

    def test_included_for_non_coder_roles(self):
        # tools is None → legacy behaviour: composed for non-coder roles.
        for role in ("supervisor", "debugger", ""):
            prompt = compose_system_prompt(agent_name="Agent", agent_role=role)
            assert self._MARKER_HEADING in prompt
            assert self._MARKER_BODY in prompt

    def test_absent_for_coder(self):
        prompt = compose_system_prompt(agent_name="Coder", agent_role="coder")
        assert self._MARKER_HEADING not in prompt
        assert self._MARKER_BODY not in prompt

    def test_absent_when_tools_lack_jev_ask(self):
        prompt = compose_system_prompt(
            agent_name="Gateway", agent_role="",
            tools=["web_search", "read_file"],
        )
        assert self._MARKER_HEADING not in prompt
        assert self._MARKER_BODY not in prompt

    def test_absent_for_gateway_empty_tools(self):
        prompt = compose_system_prompt(agent_name="Gateway", agent_role="", tools=[])
        assert self._MARKER_HEADING not in prompt
        assert self._MARKER_BODY not in prompt

    def test_present_when_tools_include_jev_ask(self):
        prompt = compose_system_prompt(
            agent_name="Supervisor", agent_role="supervisor",
            tools=["jev_ask", "read_file"],
        )
        assert self._MARKER_HEADING in prompt
        assert self._MARKER_BODY in prompt

    def test_absent_for_coder_even_with_jev_ask_tool(self):
        prompt = compose_system_prompt(
            agent_name="Coder", agent_role="coder",
            tools=["jev_ask", "read_file"],
        )
        assert self._MARKER_HEADING not in prompt
        assert self._MARKER_BODY not in prompt


class TestDecisionWallClockCeiling:
    def test_slow_drip_body_is_bounded_by_wall_clock(self, tmp_config_dir, monkeypatch):
        """Fix-round item 4: the 2.0 s budget is a wall-clock ceiling, not a
        per-socket-op timeout. A body that drips a byte every 40ms (a full
        body would take ~8s) must be aborted at ~2s and fail open to None."""
        from agent.llm.jev_client import classify_unknown

        save_providers([ProviderConfig(
            name="DecisionModel", base_url="https://jev.example/v1",
            api_key="jev-key", default_model="typesafe/system-one", caller="",
        )])

        class _SlowDrip:
            def __init__(self):
                self._chunks = 0

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read1(self, n=4096):
                time.sleep(0.04)
                self._chunks += 1
                if self._chunks >= 200:
                    return b""
                return b'{"answers": '  # forever-incomplete body

        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout=None: _SlowDrip(),
        )

        started = time.monotonic()
        result = classify_unknown("slow drip", 500)
        elapsed = time.monotonic() - started

        assert result is None
        assert elapsed < 2.5, f"wall-clock ceiling missed: {elapsed:.2f}s"
