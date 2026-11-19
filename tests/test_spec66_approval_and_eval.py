"""SPEC-66 Wave 1 — approval path (§2.1/§12.1) + eval-flag refusal (§2.2/§11.1-11.2).

Plain-row contract (SPEC-66 §12.7): no ui.window / ui.views.* /
agent_runtime_handler / feed_handler import, no widget construction, no
GTK_HEADLESS_SAFE needed (the closure pulls no GTK module — verified).

Three claims, one file:

1. A path that RESOLVES to a sensitive location must ask for approval at
   BOTH gates — the tool level (agent/tools.py) and the runtime level
   (agent/runtime.py) — whatever spelling the model used
   ("./.crabcakes/x", "foo/../.crabcakes/x", absolute-in-project).
2. `_validate_test_command` refuses interpreter eval flags in every
   attached spelling and refuses `npx` outright.
3. The grep subprocess gets the scrubbed env, and the fetch restriction
   is default-ON with `DEVELCAKES_WEB_FETCH_RESTRICT=0` as the off-switch.
"""
from __future__ import annotations

import os
import socket
import subprocess
import uuid
from unittest.mock import patch

import pytest

from agent.enforcement import _validate_test_command
from agent.runtime import AgentRuntime
from agent.tools import _run_grep, execute_tool
from models.conversation import Conversation


def _make_cfg():
    from agent.config import AgentConfig, LLMProviderConfig
    return AgentConfig(
        providers={
            "openai": LLMProviderConfig(
                name="openai",
                base_url="https://api.openai.com/v1",
                api_key="test-key",
                default_model="gpt-4o",
            ),
        },
        default_provider="openai",
        default_model="openai/gpt-4o",
        max_tool_iterations=5,
        tool_timeout_seconds=30,
    )


def _uniq() -> str:
    return f"spec66{uuid.uuid4().hex[:8]}"


# ── Item 1: the approval path ───────────────────────────────────────────────
#
# The three spellings all resolve to <project>/.crabcakes/enforcement.json.
# The raw string of the first two does NOT start with ".crabcakes/", so a
# gate that reads the raw string skips approval — that is the bypass.

_SENSITIVE_FIXTURES = (
    "./.crabcakes/enforcement.json",
    "foo/../.crabcakes/enforcement.json",
    "ABSOLUTE",
)


def _fixture_paths(tmp_path):
    """The three spellings of the same sensitive file, rooted at tmp_path."""
    return (
        "./.crabcakes/enforcement.json",
        "foo/../.crabcakes/enforcement.json",
        str(tmp_path / ".crabcakes" / "enforcement.json"),
    )


def _make_project(tmp_path):
    (tmp_path / ".crabcakes").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".crabcakes" / "enforcement.json").write_text("{}\n")
    (tmp_path / "foo").mkdir(exist_ok=True)


def test_tool_gate_asks_approval_for_every_spelling(tmp_path):
    """TOOL level: all three spellings must request approval."""
    _make_project(tmp_path)
    project = str(tmp_path)

    for path in _fixture_paths(tmp_path):
        asked = []

        def cb(session_key, tool_name, args, _asked=asked):
            _asked.append((tool_name, args))
            return False  # deny — the file must not be written

        result = execute_tool(
            "write_file",
            {"path": path, "content": "{}"},
            project,
            "agent:coder",
            approval_callback=cb,
        )
        assert asked, f"no approval requested for {path!r}"
        assert asked[0][0] == "write_file"
        # The approval card must carry the RESOLVED relative path.
        assert asked[0][1]["path"] == ".crabcakes/enforcement.json"
        assert result.success is False
        assert "sensitive" in (result.error or "").lower()


def test_tool_gate_edit_asks_approval_for_every_spelling(tmp_path):
    """TOOL level, edit_file: same three spellings."""
    _make_project(tmp_path)
    project = str(tmp_path)

    for path in _fixture_paths(tmp_path):
        asked = []

        def cb(session_key, tool_name, args, _asked=asked):
            _asked.append((tool_name, args))
            return False

        result = execute_tool(
            "edit_file",
            {"path": path, "old_text": "{}", "new_text": '{"x": 1}'},
            project,
            "agent:coder",
            approval_callback=cb,
        )
        assert asked, f"no approval requested for edit_file {path!r}"
        assert asked[0][0] == "edit_file"
        assert asked[0][1]["path"] == ".crabcakes/enforcement.json"
        assert result.success is False


def test_runtime_gate_asks_approval_for_every_spelling(tmp_path):
    """RUNTIME level: the gate must read the resolved relpath, not the raw string.

    §12.1: `agent/runtime.py` gated on `is_sensitive_path(args.get("path"))`
    — the raw model string — and computed `bypass_approval` from it too.
    """
    _make_project(tmp_path)
    project = str(tmp_path)

    for path in _fixture_paths(tmp_path):
        rt = AgentRuntime(_make_cfg())
        asked = []

        def boom(session_key, tool_name, args, _asked=asked):
            _asked.append((tool_name, args))
            return False

        rt._dispatch_approval = boom
        conv = Conversation(
            agent_name="Coder",
            agent_role="coder",
            project_path=project,
        )
        rt._execute_tool_batch(
            "agent:coder",
            None,
            conv,
            "",
            [("c1", "write_file", {"path": path, "content": "{}"})],
            1,
        )
        assert asked, f"runtime gate requested no approval for {path!r}"
        assert asked[0][0] == "write_file"


def test_runtime_gate_does_not_ask_for_a_plain_path(tmp_path):
    """Control: an ordinary file must NOT trip either gate."""
    _make_project(tmp_path)
    (tmp_path / "main.py").write_text("x = 1\n")
    project = str(tmp_path)

    rt = AgentRuntime(_make_cfg())

    def boom(*_a, **_k):
        raise AssertionError("approval requested for a non-sensitive path")

    rt._dispatch_approval = boom
    conv = Conversation(
        agent_name="Coder",
        agent_role="coder",
        project_path=project,
    )
    rt._execute_tool_batch(
        "agent:coder",
        None,
        conv,
        "",
        [("c1", "write_file", {"path": "main.py", "content": "y = 2\n"})],
        1,
    )


def test_resolved_sensitive_path_and_python_c(tmp_path):
    """R-12 node: the resolved path is gated at BOTH levels, and `python3 -c` is refused.

    This is the row's node id (SPEC-66 §7). §12.1 requires the node to
    assert approval requested for all three fixtures — `./.crabcakes/`,
    `foo/../.crabcakes/`, absolute-in-project — at BOTH the tool gate
    (agent/tools.py) and the runtime gate (agent/runtime.py). The
    R-12 mutation deletes the rel checks at all three sites, so this
    node must fail when it is applied.
    """
    _make_project(tmp_path)
    project = str(tmp_path)

    # The resolved relative form of every spelling is sensitive, and the
    # RAW string of the first two is not — which is why the gate must not
    # read the raw string.
    from agent.tools import _project_relpath, _resolve_project_path, is_sensitive_path

    for path in _fixture_paths(tmp_path):
        resolved = _resolve_project_path(path, project)
        assert resolved is not None, path
        rel = _project_relpath(resolved, project)
        assert rel == ".crabcakes/enforcement.json", rel
        assert is_sensitive_path(rel) is True
        if not os.path.isabs(path):
            assert is_sensitive_path(path) is False, path

    # (a) TOOL level — approval requested for every spelling.
    for path in _fixture_paths(tmp_path):
        asked = []

        def cb(session_key, tool_name, args, _asked=asked):
            _asked.append((tool_name, args))
            return False

        result = execute_tool(
            "write_file",
            {"path": path, "content": "{}"},
            project,
            "agent:coder",
            approval_callback=cb,
        )
        assert asked, f"tool gate requested no approval for {path!r}"
        assert result.success is False

    # (b) RUNTIME level — approval requested for every spelling.
    for path in _fixture_paths(tmp_path):
        rt = AgentRuntime(_make_cfg())
        asked = []

        def boom(session_key, tool_name, args, _asked=asked):
            _asked.append((tool_name, args))
            return False

        rt._dispatch_approval = boom
        conv = Conversation(
            agent_name="Coder",
            agent_role="coder",
            project_path=project,
        )
        rt._execute_tool_batch(
            "agent:coder",
            None,
            conv,
            "",
            [("c1", "write_file", {"path": path, "content": "{}"})],
            1,
        )
        assert asked, f"runtime gate requested no approval for {path!r}"

    # And the eval flag is refused.
    assert _validate_test_command("python3 -c 'import os'") is False


# ── Item 2: interpreter eval flags ──────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'import os'",
        "python3 -c'import os'",
        'python3 -c"import os"',
        "python3 -c=import os",
        "python3 -cimport os",
        "python3 -m pytest -c setup.cfg",
    ],
)
def test_python_eval_flags_are_refused(command):
    """python/python3: any token starting with `-c` is refused.

    `python3 -m pytest -c setup.cfg` is the DOCUMENTED FALSE POSITIVE
    (SPEC-66 §11.1): pytest's config flag collides with python's `-c`.
    """
    assert _validate_test_command(command) is False, command


@pytest.mark.parametrize(
    "command",
    [
        "node -e 'require(\"fs\")'",
        "node -e'require(\"fs\")'",
        'node -e"require(\"fs\")"',
        "node --eval 'require(\"fs\")'",
        "node --eval=require(\"fs\")",
    ],
)
def test_node_eval_flags_are_refused(command):
    assert _validate_test_command(command) is False, command


@pytest.mark.parametrize(
    "command",
    [
        "python3 -m pytest tests/",
        "python3 -m pytest",
        "ruff check .",
        "pytest tests/",
        "node script.js",
    ],
)
def test_ordinary_commands_are_allowed(command):
    assert _validate_test_command(command) is True, command


def test_npx_is_not_allowlisted():
    """SPEC-66 §11.2: `npx -c '<cmd>'` is `npm exec -c` — a shell escape.

    `npx` is removed from `_ALLOWED_BINARIES` entirely.
    """
    from agent.enforcement import _ALLOWED_BINARIES

    assert "npx" not in _ALLOWED_BINARIES
    assert _validate_test_command("npx -c 'touch /tmp/pwned'") is False
    assert _validate_test_command("npx jest") is False


def test_eval_flag_matcher_is_prefix_based():
    """The matcher itself: `=` is split first, then prefix matching."""
    from agent.enforcement import _is_eval_flag

    assert _is_eval_flag("-c", ("-c",)) is True
    assert _is_eval_flag("-cimport os", ("-c",)) is True
    assert _is_eval_flag("-c=import os", ("-c",)) is True
    assert _is_eval_flag("--config", ("-c",)) is False
    assert _is_eval_flag("--eval=x", ("-e", "--eval")) is True
    assert _is_eval_flag("-e", ("-e", "--eval")) is True


# ── Item 2/3 (R-14 scope): scrubbed grep env + fetch default ───────────────


def test_scrubbed_grep_and_default_fetch(tmp_path, monkeypatch):
    """R-14 node: grep gets the scrubbed env; fetch is restricted by default.

    SPEC-66 §12.2 re-scopes R-14: the `0.0.0.0` assertions are DROPPED
    (0.0.0.0/:: are already `is_private=True`, so they were refused before
    this spec). The node pins (a) the scrubbed-grep env kwarg on BOTH
    subprocess.run sites and (b) the `DEVELCAKES_WEB_FETCH_RESTRICT=0`
    off-switch under the NEW env name.
    """
    from utils.env_security import get_scrubbed_env

    # (a) scrubbed env on both subprocess.run sites.
    class _Completed:
        returncode = 0
        stdout = "hit\n"
        stderr = ""

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _Completed()

    monkeypatch.setattr("agent.tools.subprocess.run", fake_run)
    _run_grep("needle", str(tmp_path))
    assert len(calls) == 1
    assert calls[0][1].get("env") == get_scrubbed_env()

    # The fallback site (rg missing) gets the scrubbed env too.
    calls.clear()
    monkeypatch.setattr("agent.tools._rg_path", lambda: None)
    _run_grep("needle", str(tmp_path))
    assert len(calls) == 1
    assert calls[0][1].get("env") == get_scrubbed_env()

    # (b) the off-switch, under the NEW env name.
    from agent.tools import _is_web_fetch_restricted

    monkeypatch.delenv("DEVELCAKES_WEB_FETCH_RESTRICT", raising=False)
    monkeypatch.delenv("CRABCAKES_WEB_FETCH_RESTRICT", raising=False)
    assert _is_web_fetch_restricted() is True

    monkeypatch.setenv("DEVELCAKES_WEB_FETCH_RESTRICT", "0")
    assert _is_web_fetch_restricted() is False

    monkeypatch.setenv("DEVELCAKES_WEB_FETCH_RESTRICT", "1")
    assert _is_web_fetch_restricted() is True


def test_fetch_refuses_a_private_host_by_default(tmp_path, monkeypatch):
    """Default-on: a private host is refused when no env var is set."""
    from agent.tools import _reject_restricted_url

    monkeypatch.delenv("DEVELCAKES_WEB_FETCH_RESTRICT", raising=False)
    monkeypatch.delenv("CRABCAKES_WEB_FETCH_RESTRICT", raising=False)

    def fake_getaddrinfo(host, *args, **kwargs):
        return [(2, 1, 6, "", ("10.0.0.5", 0))]

    with patch.object(socket, "getaddrinfo", fake_getaddrinfo):
        blocked = _reject_restricted_url("http://internal.example.com/")
    assert blocked is not None
    assert blocked.success is False
    assert "MED-3" in blocked.error


def test_fetch_off_switch_allows_the_same_url(monkeypatch):
    """`DEVELCAKES_WEB_FETCH_RESTRICT=0` turns the whole check off."""
    from agent.tools import _reject_restricted_url

    monkeypatch.setenv("DEVELCAKES_WEB_FETCH_RESTRICT", "0")
    monkeypatch.delenv("CRABCAKES_WEB_FETCH_RESTRICT", raising=False)
    assert _reject_restricted_url("http://10.0.0.5/") is None


def test_grep_subprocess_gets_the_scrubbed_env_not_the_app_env(monkeypatch):
    """The scrubbed env must not carry provider keys."""
    from utils.env_security import get_scrubbed_env

    monkeypatch.setenv("OPENAI_API_KEY", "super-secret")

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return _Completed()

    monkeypatch.setattr("agent.tools.subprocess.run", fake_run)
    monkeypatch.setattr("agent.tools._rg_path", lambda: None)
    _run_grep("needle", "/tmp")
    assert "OPENAI_API_KEY" not in seen["env"]
    assert seen["env"] == get_scrubbed_env()
