"""agent_context rejects anyone who is not the supervisor, before the callback."""

from types import SimpleNamespace

from agent.group_tools import set_agent_context_callback
from agent.tools import ToolResult, execute_tool


def _agent(role, key, name):
    return SimpleNamespace(role=role, conv_id_prefix=key, display_name=name)


def test_coder_cannot_clear_a_peer(monkeypatch):
    coder = _agent("coder", "special:coder", "Coder")
    debugger = _agent("debugger", "special:debugger", "Debugger")
    monkeypatch.setattr(
        "agent.special_agents.get_special_agent",
        lambda key: coder if key == "special:coder" else None,
    )
    monkeypatch.setattr(
        "agent.special_agents.get_special_agents",
        lambda: [coder, debugger],
    )
    called = []

    def _cb(*args):
        called.append(args)
        return ToolResult(success=True, output="should not run")

    set_agent_context_callback(_cb)
    try:
        result = execute_tool(
            "agent_context",
            {"agent": "Debugger", "action": "clear"},
            "/tmp/proj",
            session_key="special:coder",
        )
    finally:
        set_agent_context_callback(None)
    assert result.success is False
    assert result.error == "agent_context is supervisor-only"
    assert called == []


def test_supervisor_cannot_target_self(monkeypatch):
    supervisor = _agent("supervisor", "special:supervisor", "Supervisor")
    monkeypatch.setattr(
        "agent.special_agents.get_special_agent",
        lambda key: supervisor if key == "special:supervisor" else None,
    )
    monkeypatch.setattr(
        "agent.special_agents.get_special_agents",
        lambda: [supervisor],
    )
    called = []
    set_agent_context_callback(lambda *args: called.append(args))
    try:
        result = execute_tool(
            "agent_context",
            {"agent": "Supervisor", "action": "compact"},
            "/tmp/proj",
            session_key="special:supervisor",
        )
    finally:
        set_agent_context_callback(None)
    assert result.success is False
    assert result.error == "agent_context cannot target the caller"
    assert called == []


def test_unknown_peer(monkeypatch):
    supervisor = _agent("supervisor", "special:supervisor", "Supervisor")
    monkeypatch.setattr(
        "agent.special_agents.get_special_agent",
        lambda key: supervisor if key == "special:supervisor" else None,
    )
    monkeypatch.setattr("agent.special_agents.get_special_agents", lambda: [supervisor])
    set_agent_context_callback(lambda *a: ToolResult(success=True, output="x"))
    try:
        result = execute_tool(
            "agent_context",
            {"agent": "Nobody", "action": "clear"},
            "/tmp/proj",
            session_key="special:supervisor",
        )
    finally:
        set_agent_context_callback(None)
    assert result.success is False
    assert result.error == "no agent named Nobody"
