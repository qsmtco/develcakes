"""cancel_peer marks only sessions whose loop is live; the epoch is always armed."""

import threading

from agent.config import AgentConfig
from agent.runtime import AgentRuntime


def test_cancel_peer_marks_live_sessions():
    rt = AgentRuntime(config=AgentConfig())
    rt._active_loops.update({"agent:coder", "agent:debugger"})
    rt.cancel_peer("agent:coder")
    assert "agent:coder" in rt._cancelled
    assert "agent:coder" in rt._cancelled_epochs
    rt.cancel_peer("agent:debugger")
    assert "agent:debugger" in rt._cancelled
    assert "agent:coder" in rt._cancelled


def test_cancel_peer_flushes_only_that_sessions_approval():
    rt = AgentRuntime(config=AgentConfig())
    coder_event = threading.Event()
    other_event = threading.Event()
    rt._pending_approvals["agent:coder:1"] = {
        "event": coder_event,
        "result": "pending",
    }
    rt._pending_approvals["agent:debugger:1"] = {
        "event": other_event,
        "result": "pending",
    }
    rt.cancel_peer("agent:coder")
    assert coder_event.is_set()
    assert "agent:coder:1" not in rt._pending_approvals
    assert not other_event.is_set()
    assert rt._pending_approvals["agent:debugger:1"]["result"] == "pending"


def test_cancel_peer_ignores_blank():
    rt = AgentRuntime(config=AgentConfig())
    rt.cancel_peer("")
    rt.cancel_peer(None)  # type: ignore[arg-type]
    assert rt._cancelled == set()
