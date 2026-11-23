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

    # (c) SPEC-66 BUG #2 — a symlinked sensitive NAME asks at BOTH gates.
    # A resolved-only check passes (a)-(b) and fails here, so the R-12
    # mutation (which reverts both gates to resolved-only) is not vacuous.
    # See test_symlink_sensitive_name_to_plain_target_asks_approval for the
    # full attack; this is the row's own pin.
    project2, _target = _make_symlinked_sensitive_name(tmp_path / "links")
    assert is_sensitive_path("notes/enforcement.json") is False
    assert is_sensitive_path(".crabcakes/enforcement.json") is True

    asked = []

    def deny_link(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    for tool_name, args in (
        ("write_file", {"path": ".crabcakes/enforcement.json", "content": "{}"}),
        ("edit_file", {"path": ".crabcakes/enforcement.json",
                       "old_text": "{}", "new_text": "{ }"}),
    ):
        result = execute_tool(tool_name, dict(args), project2, "agent:coder",
                              approval_callback=deny_link)
        assert result.success is False, tool_name

    rt_asked = []

    def deny_link_rt(session_key, tool_name, args, _asked=rt_asked):
        _asked.append((tool_name, args))
        return False

    rt = AgentRuntime(_make_cfg())
    rt._dispatch_approval = deny_link_rt
    conv2 = Conversation(
        agent_name="Coder",
        agent_role="coder",
        project_path=project2,
    )
    rt._execute_tool_batch(
        "agent:coder",
        None,
        conv2,
        "",
        [("c1", "write_file", {"path": ".crabcakes/enforcement.json", "content": "{}"})],
        1,
    )
    assert rt_asked, "runtime gate missed the symlinked sensitive name"

    # And the eval flag is refused (BUG #3: the clustered spelling too, so the
    # node documents the fully closed chain — symlink write refused → eval
    # refused in both spellings).
    assert _validate_test_command("python3 -c 'import os'") is False
    assert _validate_test_command("python3 -ic 'import os'") is False


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
        # SPEC-66 BUG #3: clustered short options — Python runs any cluster
        # containing `c` as `-c` (all verified by execution).
        "python3 -ic 'import os'",
        "python3 -qc 'import os'",
        "python3 -Bc 'import os'",
        "python3 -uc 'import os'",
        "python3 -Oc 'import os'",
        "python3 -ic=1 'import os'",
    ],
)
def test_python_eval_flags_are_refused(command):
    """python/python3: any token starting with `-c`, or any cluster with `c`.

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
        # SPEC-66 BUG #3: node clusters carrying `e` or `p`, plus `--print`
        # (`node -p` / `--print` evaluate the script and print the result).
        "node -pe 'x=1'",
        "node -p 'x=1'",
        "node --print 'x=1'",
        "node -ie 'x=1'",
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
        # SPEC-66 BUG #3 boundaries: long options and non-eval letters stay
        # allowed so the cluster rule does not over-refuse ordinary flags.
        "python3 --config setup.cfg",
        "node -c script.js",
        "node -v",
        "node -r module.js",
        "python3 -i script.py",
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


def test_clustered_eval_flag_matcher():
    """The cluster matcher: one `-`, `=value` stripped, letters scanned.

    SPEC-66 BUG #3: `-ic` is `-i` + `-c` for python, so prefix matching alone
    never sees it. Long options belong to the prefix matcher, not this one.
    """
    from agent.enforcement import _is_clustered_eval_flag

    assert _is_clustered_eval_flag("-ic", ("c",)) is True
    assert _is_clustered_eval_flag("--config", ("c",)) is False
    assert _is_clustered_eval_flag("-c", ("c",)) is True
    assert _is_clustered_eval_flag("-e", ("e", "p")) is True
    assert _is_clustered_eval_flag("script.py", ("c",)) is False
    assert _is_clustered_eval_flag("--print", ("e", "p")) is False
    assert _is_clustered_eval_flag("-ic=1", ("c",)) is True


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


# ── §12.9 fetch edge rows ─────────────────────────────────────────────────
# SPEC-66 v4 §12.9: pin the IP-literal and mixed-DNS edge cases. Every test
# mocks socket.getaddrinfo — no network. The restricted gate is default-ON.


def _restricted_default(monkeypatch):
    monkeypatch.delenv("DEVELCAKES_WEB_FETCH_RESTRICT", raising=False)
    monkeypatch.delenv("CRABCAKES_WEB_FETCH_RESTRICT", raising=False)


class TestFetchEdgeRows:
    """SPEC-66 §12.9 — the three fetch-edge rows (mocked DNS)."""

    def test_ipv6_unspecified_literal_is_refused(self, monkeypatch):
        """http://[::]/ is refused via is_private (getaddrinfo normalises)."""
        from agent.tools import _reject_restricted_url

        _restricted_default(monkeypatch)

        def fake_getaddrinfo(host, *args, **kwargs):
            # getaddrinfo("::") resolves to the unspecified v6 address
            return [(10, 1, 6, "", ("::", 0, 0, 0))]

        with patch.object(socket, "getaddrinfo", fake_getaddrinfo):
            blocked = _reject_restricted_url("http://[::]/")
        assert blocked is not None, "the unspecified IPv6 literal must be refused"
        assert blocked.success is False

    def test_ipv4_mapped_ipv6_loopback_is_refused(self, monkeypatch):
        """http://[::ffff:127.0.0.1]/ — the mapped form reaches is_private."""
        from agent.tools import _reject_restricted_url

        _restricted_default(monkeypatch)

        def fake_getaddrinfo(host, *args, **kwargs):
            return [(10, 1, 6, "", ("::ffff:127.0.0.1", 0, 0, 0))]

        with patch.object(socket, "getaddrinfo", fake_getaddrinfo):
            blocked = _reject_restricted_url("http://[::ffff:127.0.0.1]/")
        assert blocked is not None, (
            "an IPv4-mapped loopback must not slip past the private check"
        )
        assert blocked.success is False

    def test_mixed_dns_any_private_address_refuses(self, monkeypatch):
        """A hostname resolving to [public, private] is refused: ANY private
        address in the resolved set blocks the fetch."""
        from agent.tools import _reject_restricted_url

        _restricted_default(monkeypatch)

        def fake_getaddrinfo(host, *args, **kwargs):
            return [
                (2, 1, 6, "", ("93.184.216.34", 0)),   # public
                (2, 1, 6, "", ("192.168.1.10", 0)),    # private
            ]

        with patch.object(socket, "getaddrinfo", fake_getaddrinfo):
            blocked = _reject_restricted_url("http://mixed.example.com/")
        assert blocked is not None, (
            "a DNS answer containing ANY private address must be refused"
        )
        assert blocked.success is False

    def test_all_public_dns_answer_is_allowed(self, monkeypatch):
        """Control: a fully-public answer set passes the restricted gate."""
        from agent.tools import _reject_restricted_url

        _restricted_default(monkeypatch)

        def fake_getaddrinfo(host, *args, **kwargs):
            return [(2, 1, 6, "", ("93.184.216.34", 0))]

        with patch.object(socket, "getaddrinfo", fake_getaddrinfo):
            assert _reject_restricted_url("http://example.com/") is None


# ── SPEC-66 BUG #2: symlink-aware sensitive-path approval ─────────────────
#
# `_resolve_project_path` calls realpath, so a sensitive NAME pointing at a
# plain target hides its sensitivity from the resolved-path check. Both gates
# must consult the lexical (unresolved) form as well. These tests are the
# external report's exact attack plus the reverse direction (so a future
# refactor cannot trade one for the other) and the `.crabcakes`-is-a-dir
# -symlink variant that only the lexical check can catch.
#
# Note the two forms differ in BOTH directions and the gates must catch either:
#   .crabcakes/enforcement.json -> notes/enforcement.json : lexical sensitive
#   notes/innocent.txt          -> .crabcakes/enforcement.json : resolved sensitive


def _make_symlinked_sensitive_name(tmp_path):
    """The attack: a sensitive NAME whose target is a plain file."""
    (tmp_path / ".crabcakes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "notes").mkdir(exist_ok=True)
    target = tmp_path / "notes" / "enforcement.json"
    target.write_text("{}\n")
    link = tmp_path / ".crabcakes" / "enforcement.json"
    os.symlink(target, link)
    return str(tmp_path), target


def test_symlink_sensitive_name_to_plain_target_asks_approval(tmp_path):
    """The report's attack, write_file: symlinked sensitive NAME must ask.

    Before BUG #2 the gate saw only `notes/enforcement.json` (the target),
    approved nothing, and wrote the payload through the link — which the
    enforcement loader then read back as a legal command.
    """
    from agent.tools import is_sensitive_path

    project, target = _make_symlinked_sensitive_name(tmp_path)

    # Precondition: the resolved form is NOT sensitive, the lexical one IS.
    # Without this the test would pass for the wrong reason.
    assert is_sensitive_path("notes/enforcement.json") is False
    assert is_sensitive_path(".crabcakes/enforcement.json") is True

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    payload = '{"test_commands": ["node pwn.js"]}\n'
    result = execute_tool(
        "write_file",
        {"path": ".crabcakes/enforcement.json", "content": payload},
        project,
        "agent:coder",
        approval_callback=deny,
    )
    assert len(asked) == 1, f"expected exactly one approval request, got {asked!r}"
    assert asked[0][0] == "write_file"
    # The card must name a sensitive form (here: the lexical one, since the
    # resolved target is plain).
    assert is_sensitive_path(asked[0][1]["path"]) is True, asked[0][1]["path"]
    assert asked[0][1]["path"] == ".crabcakes/enforcement.json"
    assert result.success is False
    assert "sensitive" in (result.error or "").lower()
    # Denied means the target was NOT written through the link.
    assert target.read_text() == "{}\n", "a denied write still reached the target"

    # Approving lets the write through — the gate is a gate, not a wall.
    result_ok = execute_tool(
        "write_file",
        {"path": ".crabcakes/enforcement.json", "content": payload},
        project,
        "agent:coder",
        approval_callback=lambda *a, **k: True,
    )
    assert result_ok.success is True, result_ok.error
    assert target.read_text() == payload


def test_symlink_sensitive_name_to_plain_target_asks_approval_edit(tmp_path):
    """Same fixture through edit_file."""
    from agent.tools import is_sensitive_path

    project, target = _make_symlinked_sensitive_name(tmp_path)
    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = execute_tool(
        "edit_file",
        {"path": ".crabcakes/enforcement.json", "old_text": "{}", "new_text": "{ }"},
        project,
        "agent:coder",
        approval_callback=deny,
    )
    assert len(asked) == 1, f"expected exactly one approval request, got {asked!r}"
    assert asked[0][0] == "edit_file"
    assert is_sensitive_path(asked[0][1]["path"]) is True, asked[0][1]["path"]
    assert result.success is False
    assert target.read_text() == "{}\n", "a denied edit still reached the target"

    result_ok = execute_tool(
        "edit_file",
        {"path": ".crabcakes/enforcement.json", "old_text": "{}", "new_text": "{ }"},
        project,
        "agent:coder",
        approval_callback=lambda *a, **k: True,
    )
    assert result_ok.success is True, result_ok.error
    assert target.read_text() == "{ }\n"


def test_symlink_sensitive_name_asks_approval_at_runtime_gate(tmp_path):
    """The same attack through the RUNTIME gate (agent/runtime.py).

    Mirrors `test_runtime_gate_asks_approval_for_every_spelling`: the gate
    fires before execution and the denied call never reaches the tool.
    """
    project, target = _make_symlinked_sensitive_name(tmp_path)
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
        [("c1", "write_file",
          {"path": ".crabcakes/enforcement.json",
           "content": '{"test_commands": ["node pwn.js"]}\n'})],
        1,
    )
    assert asked, "runtime gate requested no approval for a symlinked sensitive name"
    assert asked[0][0] == "write_file"
    assert target.read_text() == "{}\n", "the denied runtime call wrote through the link"


def test_symlink_plain_name_to_sensitive_target_asks_approval(tmp_path):
    """The REVERSE direction: a plain NAME pointing at a sensitive target.

    This direction already worked (the resolved form is sensitive) — it is
    pinned here so adding the lexical check can never be done by dropping
    the resolved one.
    """
    from agent.tools import is_sensitive_path

    (tmp_path / ".crabcakes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "notes").mkdir(exist_ok=True)
    sensitive = tmp_path / ".crabcakes" / "enforcement.json"
    sensitive.write_text("{}\n")
    link = tmp_path / "notes" / "innocent.txt"
    os.symlink(sensitive, link)
    project = str(tmp_path)

    # Here it is the RESOLVED form that is sensitive.
    assert is_sensitive_path("notes/innocent.txt") is False
    assert is_sensitive_path(".crabcakes/enforcement.json") is True

    for tool, args in (
        ("write_file", {"path": "notes/innocent.txt", "content": '{"a": 1}\n'}),
        ("edit_file", {"path": "notes/innocent.txt", "old_text": "{}", "new_text": "{ }"}),
    ):
        asked = []

        def deny(session_key, tool_name, a, _asked=asked):
            _asked.append((tool_name, a))
            return False

        result = execute_tool(tool, dict(args), project, "agent:coder",
                             approval_callback=deny)
        assert asked, f"{tool}: no approval for a plain name -> sensitive target"
        assert is_sensitive_path(asked[0][1]["path"]) is True, asked[0][1]["path"]
        assert result.success is False
        assert sensitive.read_text() == "{}\n"

    # And the runtime gate catches it too.
    rt = AgentRuntime(_make_cfg())
    asked = []

    def boom(session_key, tool_name, a, _asked=asked):
        _asked.append((tool_name, a))
        return False

    rt._dispatch_approval = boom
    conv = Conversation(agent_name="Coder", agent_role="coder", project_path=project)
    rt._execute_tool_batch(
        "agent:coder", None, conv, "",
        [("c1", "write_file", {"path": "notes/innocent.txt", "content": '{"a": 1}\n'})],
        1,
    )
    assert asked, "runtime gate missed a plain name -> sensitive target"
    assert sensitive.read_text() == "{}\n"


def test_symlinked_crabcakes_dir_asks_approval(tmp_path):
    """`.crabcakes` ITSELF is a symlink to `notes/`.

    Resolving `.crabcakes/enforcement.json` lands in `notes/`, which is not
    sensitive — so ONLY the lexical check catches this one.
    """
    from agent.tools import is_sensitive_path

    (tmp_path / "notes").mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / "notes", tmp_path / ".crabcakes")
    target = tmp_path / "notes" / "enforcement.json"
    project = str(tmp_path)

    assert is_sensitive_path("notes/enforcement.json") is False
    assert is_sensitive_path(".crabcakes/enforcement.json") is True

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = execute_tool(
        "write_file",
        {"path": ".crabcakes/enforcement.json",
         "content": '{"test_commands": ["node pwn.js"]}\n'},
        project,
        "agent:coder",
        approval_callback=deny,
    )
    assert asked, "no approval for a write through a symlinked .crabcakes dir"
    assert is_sensitive_path(asked[0][1]["path"]) is True, asked[0][1]["path"]
    assert result.success is False
    assert not target.exists(), "a denied write created the target through the dir link"

    # The runtime gate must see it as well.
    rt = AgentRuntime(_make_cfg())
    rt_asked = []

    def boom(session_key, tool_name, args, _asked=rt_asked):
        _asked.append((tool_name, args))
        return False

    rt._dispatch_approval = boom
    conv = Conversation(agent_name="Coder", agent_role="coder", project_path=project)
    rt._execute_tool_batch(
        "agent:coder", None, conv, "",
        [("c1", "write_file",
          {"path": ".crabcakes/enforcement.json", "content": '{"a": 1}\n'})],
        1,
    )
    assert rt_asked, "runtime gate missed a symlinked .crabcakes dir"
    assert not target.exists()
