"""SPEC-25: Python repo map in graph order, without a decision-model call."""
from __future__ import annotations

import json
from unittest.mock import patch

from agent.repo_map import build_repo_map
from agent.runtime import REPO_MAP_HEADER, AgentRuntime
from models.conversation import Conversation
from tests.test_agent_runtime import _make_cfg, _resp


def _fixture(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    bar()\n")
    (tmp_path / "b.py").write_text("def bar():\n    pass\n")


def test_graph_order_does_not_call_the_decision_model(tmp_path):
    _fixture(tmp_path)
    called = {"n": 0}

    def boom(state, questions):
        called["n"] += 1
        raise AssertionError("repo map must not call the decision model")

    with patch("agent.llm.jev_client.post_decision", side_effect=boom):
        text = build_repo_map(str(tmp_path), "fix bar")
    assert called["n"] == 0
    assert text.index("def bar") < text.index("def foo")
    assert len(text) // 4 <= 2000


def test_one_parser_and_query_for_the_walk(tmp_path, monkeypatch):
    _fixture(tmp_path)
    import tree_sitter

    from agent import repo_map
    if hasattr(repo_map._PARSE_LOCAL, "tools"):
        del repo_map._PARSE_LOCAL.tools
    made = {"parser": 0, "query": 0}
    RealParser = tree_sitter.Parser
    RealQuery = tree_sitter.Query

    def counting_parser(language):
        made["parser"] += 1
        return RealParser(language)

    def counting_query(language, source):
        made["query"] += 1
        return RealQuery(language, source)

    monkeypatch.setattr(tree_sitter, "Parser", counting_parser)
    monkeypatch.setattr(tree_sitter, "Query", counting_query)
    text = build_repo_map(str(tmp_path), "fix bar")
    assert "def bar" in text and "def foo" in text
    assert made == {"parser": 1, "query": 1}


def test_no_python_files_yields_empty_and_wire_is_unchanged(tmp_path):
    (tmp_path / "readme.txt").write_text("hello")
    assert build_repo_map(str(tmp_path), "fix bar") == ""
    conv = Conversation(agent_name="Coder", system_prompt="SYS", project_path=str(tmp_path))
    conv.add_user_message("fix bar")
    rt = AgentRuntime(_make_cfg())
    sk = "agent:empty"
    rt._conversations[sk] = conv
    with patch("agent.repo_map.build_repo_map", return_value="") as built:
        wired = rt._messages_for_model(sk, conv, 128_000)
    assert built.call_count == 1
    user = next(m for m in wired if m["role"] == "user")
    assert user["content"] == "fix bar"
    assert conv.system_prompt == "SYS"


def test_wire_prefixes_the_map_without_touching_system_prompt(tmp_path):
    _fixture(tmp_path)
    conv = Conversation(agent_name="Coder", system_prompt="SYS", project_path=str(tmp_path))
    conv.add_user_message("fix bar")
    rt = AgentRuntime(_make_cfg())
    sk = "agent:map"
    rt._conversations[sk] = conv
    with patch("agent.repo_map.build_repo_map", return_value="MAPBLOCK"):
        wired = rt._messages_for_model(sk, conv, 128_000)
    user = next(m for m in wired if m["role"] == "user")
    assert user["content"] == REPO_MAP_HEADER + "\n\nMAPBLOCK\n\nfix bar"
    assert conv.system_prompt == "SYS"
    assert conv.messages[-1].content == "fix bar"


def test_coder_role_wire_omits_the_repo_map(tmp_path):
    _fixture(tmp_path)
    conv = Conversation(
        agent_name="Coder",
        agent_role="coder",
        system_prompt="SYS",
        project_path=str(tmp_path),
    )
    conv.add_user_message("fix bar")
    rt = AgentRuntime(_make_cfg())
    sk = "agent:coder-map"
    rt._conversations[sk] = conv
    with patch("agent.repo_map.build_repo_map", return_value="MAPBLOCK"):
        wired = rt._messages_for_model(sk, conv, 128_000)
    user = next(m for m in wired if m["role"] == "user")
    assert REPO_MAP_HEADER not in user["content"]
    assert user["content"] == "fix bar"


def test_two_iterations_call_build_repo_map_once(tmp_path):
    _fixture(tmp_path)
    rt = AgentRuntime(_make_cfg())
    sk = "agent:once"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    rt._running = True
    calls = {"n": 0, "map": 0}

    def llm(session_key, messages, tools, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp(tool_calls=[{
                "id": "c1",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "a.py"})},
            }])
        return _resp("done")

    def fake_map(project, goal):
        calls["map"] += 1
        return "MAPBLOCK"

    with patch.object(rt, "_call_llm", llm), patch("agent.repo_map.build_repo_map", fake_map):
        rt._run_loop(sk, "fix bar")
    assert calls["n"] == 2
    assert calls["map"] == 1
