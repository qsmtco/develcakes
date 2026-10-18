"""cancel_peer marks one session and does not arm the global cancel flag."""

import threading

from agent.config import AgentConfig
from agent.runtime import AgentRuntime


def test_cancel_peer_leaves_global_flag_false():
    rt = AgentRuntime(config=AgentConfig())
    assert rt._cancel_requested is False
    rt.cancel_peer("special:coder")
    assert "special:coder" in rt._cancelled
    assert "special:coder" in rt._cancelled_epochs
    assert rt._cancel_requested is False
    rt.cancel_peer("special:debugger")
    assert "special:debugger" in rt._cancelled
    assert "special:coder" in rt._cancelled
    assert rt._cancel_requested is False


def test_cancel_peer_flushes_only_that_sessions_approval():
    rt = AgentRuntime(config=AgentConfig())
    coder_event = threading.Event()
    other_event = threading.Event()
    rt._pending_approvals["special:coder:1"] = {
        "event": coder_event,
        "result": "pending",
    }
    rt._pending_approvals["special:debugger:1"] = {
        "event": other_event,
        "result": "pending",
    }
    rt.cancel_peer("special:coder")
    assert coder_event.is_set()
    assert "special:coder:1" not in rt._pending_approvals
    assert not other_event.is_set()
    assert rt._pending_approvals["special:debugger:1"]["result"] == "pending"
    assert rt._cancel_requested is False


def test_cancel_peer_ignores_blank():
    rt = AgentRuntime(config=AgentConfig())
    rt.cancel_peer("")
    rt.cancel_peer(None)  # type: ignore[arg-type]
    assert rt._cancelled == set()
    assert rt._cancel_requested is False
