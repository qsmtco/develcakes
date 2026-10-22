"""agent_context: peers are supervisor-only; any agent may edit itself."""

from types import SimpleNamespace

from agent.group_tools import set_agent_context_callback
from agent.tools import ToolResult, execute_tool


def _agent(role, key, name):
    return SimpleNamespace(role=role, conv_id_prefix=key, display_name=name)


def test_coder_cannot_clear_a_peer(monkeypatch):
    coder = _agent("coder", "agent:coder", "Coder")
    debugger = _agent("debugger", "agent:debugger", "Debugger")
    monkeypatch.setattr(
        "agent.special_agents.get_special_agent",
        lambda key: coder if key == "agent:coder" else None,
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
            session_key="agent:coder",
        )
    finally:
        set_agent_context_callback(None)
    assert result.success is False
    assert result.error == "agent_context is supervisor-only"
    assert called == []


def test_supervisor_can_target_self(monkeypatch):
    supervisor = _agent("supervisor", "agent:supervisor", "Supervisor")
    monkeypatch.setattr(
        "agent.special_agents.get_special_agent",
        lambda key: supervisor if key == "agent:supervisor" else None,
    )
    monkeypatch.setattr(
        "agent.special_agents.get_special_agents",
        lambda: [supervisor],
    )
    called = []

    def _cb(*args):
        called.append(args)
        return ToolResult(success=True, output="edited")

    set_agent_context_callback(_cb)
    try:
        result = execute_tool(
            "agent_context",
            {"agent": "self", "action": "edit", "drop": ["0-1"]},
            "/tmp/proj",
            session_key="agent:supervisor",
        )
    finally:
        set_agent_context_callback(None)
    assert result.success is True
    assert called == [(
        "agent:supervisor",
        "agent:supervisor",
        "edit",
        "",
        {"drop": [0, 1], "keep": None},
    )]


def test_coder_can_edit_self_and_not_a_peer(monkeypatch):
    coder = _agent("coder", "agent:coder", "Coder")
    debugger = _agent("debugger", "agent:debugger", "Debugger")
    monkeypatch.setattr(
        "agent.special_agents.get_special_agent",
        lambda key: coder if key == "agent:coder" else None,
    )
    monkeypatch.setattr(
        "agent.special_agents.get_special_agents",
        lambda: [coder, debugger],
    )
    called = []

    def _cb(*args):
        called.append(args)
        return ToolResult(success=True, output="edited")

    set_agent_context_callback(_cb)
    try:
        self_edit = execute_tool(
            "agent_context",
            {"agent": "Coder", "action": "inspect"},
            "/tmp/proj",
            session_key="agent:coder",
        )
        peer_edit = execute_tool(
            "agent_context",
            {"agent": "Debugger", "action": "edit", "keep": [0]},
            "/tmp/proj",
            session_key="agent:coder",
        )
    finally:
        set_agent_context_callback(None)
    assert self_edit.success is True
    assert called == [("agent:coder", "agent:coder", "inspect", "", None)]
    assert peer_edit.success is False
    assert peer_edit.error == "agent_context is supervisor-only"


def test_edit_rejects_both_drop_and_keep(monkeypatch):
    supervisor = _agent("supervisor", "agent:supervisor", "Supervisor")
    monkeypatch.setattr(
        "agent.special_agents.get_special_agent",
        lambda key: supervisor if key == "agent:supervisor" else None,
    )
    monkeypatch.setattr("agent.special_agents.get_special_agents", lambda: [supervisor])
    called = []
    set_agent_context_callback(lambda *args: called.append(args))
    try:
        result = execute_tool(
            "agent_context",
            {"agent": "self", "action": "edit", "drop": [1], "keep": [0]},
            "/tmp/proj",
            session_key="agent:supervisor",
        )
    finally:
        set_agent_context_callback(None)
    assert result.success is False
    assert result.error == "pass drop or keep, not both"
    assert called == []


def test_prompt_teaches_self_edit():
    from pathlib import Path
    text = Path("prompts/system/agent-tools.md").read_text(encoding="utf-8")
    assert "agent to `self`" in text
    assert "`drop` or `keep`" in text


def test_unknown_peer(monkeypatch):
    supervisor = _agent("supervisor", "agent:supervisor", "Supervisor")
    monkeypatch.setattr(
        "agent.special_agents.get_special_agent",
        lambda key: supervisor if key == "agent:supervisor" else None,
    )
    monkeypatch.setattr("agent.special_agents.get_special_agents", lambda: [supervisor])
    set_agent_context_callback(lambda *a: ToolResult(success=True, output="x"))
    try:
        result = execute_tool(
            "agent_context",
            {"agent": "Nobody", "action": "clear"},
            "/tmp/proj",
            session_key="agent:supervisor",
        )
    finally:
        set_agent_context_callback(None)
    assert result.success is False
    assert result.error == "no agent named Nobody"
