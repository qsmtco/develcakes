"""Regression lock — WU minimax-2013 chunk B.

Proves `normalize_tool_history` is wired at BOTH dispatch seams:
`AgentRuntime._call_llm_once` (blocking) and `AgentRuntime._call_llm_streaming`.

One test per seam. Each test asserts both halves of the brief:
- a clean transcript reaches the provider IDENTICAL (==) to input, proving no
  behavior change for providers that were already receiving good data;
- a dirty transcript (one orphan `role:"tool"` result) is dropped.

The fake provider harness mirrors tests/test_provider_failover.py:
`_get_provider` is monkeypatched to a bare object exposing `call` / `stream`.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.config import AgentConfig, LLMProviderConfig
from agent.llm.streaming import SSEEvent
from agent.runtime import AgentRuntime

SESSION_KEY = "s1"
MODEL = "openrouter/demo"


class _BlockingProvider:
    """Fake blocking provider — records the messages it is dispatched.

    Deliberately has no `stream` attribute: if `_call_llm_once` ever routed
    this test down the streaming path, the AttributeError fails the test
    instead of silently passing at the wrong seam.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}


class _StreamingProvider:
    """Fake streaming provider — records kwargs, yields a single done event."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        yield SSEEvent(type="done", data={})


def _cfg() -> AgentConfig:
    provider = LLMProviderConfig(
        name="OpenRouter",
        base_url="https://openrouter.example/v1",
        api_key="sk-a",
        default_model=MODEL,
        caller="openrouter",
    )
    return AgentConfig(
        providers={"openrouter": provider, "OpenRouter": provider},
        default_provider="openrouter",
        default_model=MODEL,
        provider_failover_enabled=False,
    )


def _runtime() -> AgentRuntime:
    rt = AgentRuntime(config=_cfg())
    # _call_llm_once reads model/api_key/app_title off the conversation.
    rt._conversations[SESSION_KEY] = SimpleNamespace(
        api_key=None,
        model=MODEL,
        app_title="t",
        fallback_provider=None,
        project_path="/tmp/proj",
        messages=[],
    )
    return rt


def _clean_history() -> list[dict]:
    """A well-formed pair in natural order — must pass through untouched."""
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "read a.py"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "print(1)"},
        {"role": "assistant", "content": "done"},
    ]


def _dirty_history() -> list[dict]:
    """A clean pair plus one orphan result that no assistant declared."""
    return _clean_history() + [
        {"role": "tool", "tool_call_id": "call_orphan", "content": "stale output"},
    ]


def _without_orphan(messages: list[dict]) -> list[dict]:
    return [m for m in messages if m.get("tool_call_id") != "call_orphan"]


def test_blocking_seam_normalizes_and_passes_clean_history(monkeypatch):
    rt = _runtime()
    provider = _BlockingProvider()
    monkeypatch.setattr("agent.runtime._get_provider", lambda _key: provider)

    # Half 1: clean transcript must arrive byte-identical.
    clean = _clean_history()
    rt._call_llm_once(SESSION_KEY, clean, [])
    assert len(provider.calls) == 1, "expected exactly one provider dispatch"
    assert provider.calls[0]["messages"] == clean, (
        "clean transcript must reach the provider unchanged — "
        "normalization altered a history that was already valid"
    )

    # Half 2: the orphan tool result must be dropped.
    rt._call_llm_once(SESSION_KEY, _dirty_history(), [])
    dispatched = provider.calls[1]["messages"]
    assert dispatched == _without_orphan(_clean_history()), (
        f"orphan tool result was not dropped at the blocking seam: {dispatched}"
    )


def test_streaming_seam_normalizes_and_passes_clean_history(monkeypatch):
    rt = _runtime()
    provider = _StreamingProvider()
    monkeypatch.setattr("agent.runtime._get_provider", lambda _key: provider)

    def _dispatch(messages):
        rt._call_llm_streaming(
            session_key=SESSION_KEY,
            base_url="https://openrouter.example/v1",
            api_key="sk-a",
            model=MODEL,
            caller_key="openrouter",
            messages=messages,
            tools=None,
            timeout=5.0,
        )

    # Half 1: clean transcript must arrive byte-identical.
    clean = _clean_history()
    _dispatch(clean)
    assert len(provider.calls) == 1, "expected exactly one streaming dispatch"
    assert provider.calls[0]["messages"] == clean, (
        "clean transcript must reach the streaming provider unchanged"
    )

    # Half 2: the orphan tool result must be dropped.
    _dispatch(_dirty_history())
    dispatched = provider.calls[1]["messages"]
    assert dispatched == _without_orphan(_clean_history()), (
        f"orphan tool result was not dropped at the streaming seam: {dispatched}"
    )
