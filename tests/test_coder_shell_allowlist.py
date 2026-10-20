"""SPEC-43: Coder shell allowlist. The helper is called directly; no shell is spawned."""
from __future__ import annotations

from agent.runtime import AgentRuntime, _coder_shell_allowed
from models.conversation import Conversation
from tests.test_agent_runtime import _make_cfg


def test_allowlist_table(tmp_path):
    project = str(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_tools.py").write_text("def test_ok():\n    assert True\n")
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "tools.py").write_text("x = 1\n")

    allowed = [
        "git status",
        "git diff --stat",
        "git log -1 --oneline",
        f"cd {project} && git status",
        ".venv/bin/python -m py_compile agent/tools.py",
        ".venv/bin/python -m pytest tests/test_tools.py -q",
    ]
    refused = [
        "cd /etc && git status",
        "pytest tests/test_tools.py tests/test_repo_map.py",
        "grep -n foo agent/runtime.py",
        "awk 'NR>=1' agent/runtime.py",
        "git status && grep foo x",
        "git commit -m x",
        "python -c 'print(1)'",
        "python -m py_compile /etc/passwd",
        "git status\ngrep foo x",
        "git status 'unterminated",
        "git status <(cat /etc/passwd)",
        "git diff --output=/tmp/x",
    ]
    for command in allowed:
        assert _coder_shell_allowed(command, project) is None, command
    for command in refused:
        result = _coder_shell_allowed(command, project)
        assert result is not None and result.startswith("REFUSED:"), command
    assert _coder_shell_allowed("git status", "") is not None
    assert _coder_shell_allowed("git status", None) is not None


def test_refused_grep_does_not_ask_for_approval(tmp_path):
    rt = AgentRuntime(_make_cfg())
    conv = Conversation(
        agent_name="Coder",
        agent_role="coder",
        project_path=str(tmp_path),
    )

    def boom(*_args, **_kwargs):
        raise AssertionError("approval requested")

    rt._dispatch_approval = boom
    stopped = rt._execute_tool_batch(
        "special:coder",
        None,
        conv,
        "",
        [("c1", "exec_command", {"command": "grep -n foo agent/runtime.py"})],
        1,
    )
    assert stopped is False
    bodies = [m.content or "" for m in conv.messages]
    assert any(body.startswith("REFUSED:") for body in bodies)


def test_coder_edit_runs_syntax_not_lint(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")
    (tmp_path / "mod.py").write_text("def keep():\n    return 1\n")
    rt = AgentRuntime(_make_cfg())
    original = rt._config.enforcement
    assert original.lint_check is True
    conv = Conversation(
        agent_name="Coder",
        agent_role="coder",
        project_path=str(tmp_path),
    )
    stopped = rt._execute_tool_batch(
        "special:coder",
        None,
        conv,
        "",
        [("c1", "edit_file", {
            "path": "mod.py",
            "old_text": "return 1",
            "new_text": "return 2",
        })],
        1,
    )
    assert stopped is False
    assert rt._config.enforcement is original
    assert original.lint_check is True
    body = conv.messages[-1].content or ""
    assert "[enforcement:syntax]" in body
    assert "[enforcement:lint]" not in body
    assert "[enforcement:tests]" not in body


def test_debugger_edit_still_lints(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")
    (tmp_path / "mod.py").write_text("def keep():\n    return 1\n")
    rt = AgentRuntime(_make_cfg())
    conv = Conversation(
        agent_name="Debugger",
        agent_role="debugger",
        project_path=str(tmp_path),
    )
    rt._execute_tool_batch(
        "special:debugger",
        None,
        conv,
        "",
        [("c1", "edit_file", {
            "path": "mod.py",
            "old_text": "return 1",
            "new_text": "return 2",
        })],
        1,
    )
    body = conv.messages[-1].content or ""
    assert "[enforcement:lint]" in body
