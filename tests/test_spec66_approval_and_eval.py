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

    # (d) SPEC-66 BUG #1 (report #6) — the ALIAS direction asks at BOTH
    # gates. Neither the resolved nor the lexical form of `notes/...` is
    # sensitive, so (a)-(c) pass while a resolved+lexical-only gate lets the
    # write through. One case per gate keeps the R-12 mutation non-vacuous
    # for the alias disjunct too.
    alias_root = tmp_path / "alias"
    alias_root.mkdir()
    (alias_root / ".crabcakes").mkdir()
    (alias_root / "notes").mkdir()
    alias_target = alias_root / "notes" / "enforcement.json"
    alias_target.write_text("{}\n")
    os.symlink(alias_target, alias_root / ".crabcakes" / "enforcement.json")
    assert is_sensitive_path("notes/enforcement.json") is False

    alias_project = str(alias_root)
    alias_asked = []

    def deny_alias(session_key, tool_name, args, _asked=alias_asked):
        _asked.append((tool_name, args))
        return False

    alias_result = execute_tool(
        "write_file",
        {"path": "notes/enforcement.json",
         "content": '{"test_commands": ["node pwn.js"]}\n'},
        alias_project,
        "agent:coder",
        approval_callback=deny_alias,
    )
    assert alias_asked, "tool gate missed the alias direction (BUG #1)"
    assert alias_asked[0][1]["path"] == ".crabcakes/enforcement.json", alias_asked
    assert alias_result.success is False
    assert alias_target.read_text() == "{}\n"

    alias_rt_asked = []

    def deny_alias_rt(session_key, tool_name, args, _asked=alias_rt_asked):
        _asked.append((tool_name, args))
        return False

    rt_alias = AgentRuntime(_make_cfg())
    rt_alias._dispatch_approval = deny_alias_rt
    from agent.runtime import _sensitive_relpath as _alias_gate

    assert _alias_gate("notes/enforcement.json", alias_project) == (
        ".crabcakes/enforcement.json")
    conv_alias = Conversation(
        agent_name="Coder",
        agent_role="coder",
        project_path=alias_project,
    )
    rt_alias._execute_tool_batch(
        "agent:coder",
        None,
        conv_alias,
        "",
        [("c1", "write_file",
          {"path": "notes/enforcement.json",
           "content": '{"test_commands": ["node pwn.js"]}\n'})],
        1,
    )
    assert alias_rt_asked, "runtime gate missed the alias direction (BUG #1)"
    assert alias_target.read_text() == "{}\n"

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
        # SPEC-66 BUG #4: preload (`-r` / `--require` runs a module before the
        # entry point) and bare script paths (a positional IS the script).
        "node -r ./m.js app.js",
        "node --require ./m.js app.js",
        "node --require=./m.js app.js",
        "node evil.js",
        "node ./scripts/run.js",
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
        # SPEC-66 BUG #3 boundaries: long options and non-eval letters stay
        # allowed so the cluster rule does not over-refuse ordinary flags.
        # C-C (PM RULING 2026-11-23) forced the `=` spelling here: the
        # space-separated `python3 --config setup.cfg` carries a positional
        # (`setup.cfg`) and is refused by the python script-positional ban.
        # What this row still pins is the `--config` ≠ `-c` boundary.
        "python3 --config=setup.cfg",
        "node -v",
        # C-C (PM RULING 2026-11-23): `python3 -i script.py` is now REFUSED —
        # a script positional is banned whether or not the flag is an eval
        # flag. The non-eval-flag boundary it used to pin is kept here in a
        # spelling that survives the ban (module invocation, so `-i` is just
        # an interpreter flag with a resolved module behind it).
        "python3 -i -m pytest -q",
        # SPEC-66 BUG #4 boundary: `--check` / `-c` is syntax-parse only (no
        # execution) and is what our own syntax tier template uses, so a node
        # positional WITH a check flag stays allowed.
        "node --check script.js",
        "node -c script.js",
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
    # SPEC-66 BUG #4: node's `r` (require/preload) is a cluster letter too.
    assert _is_clustered_eval_flag("-r", ("e", "p", "r")) is True
    assert _is_clustered_eval_flag("-ir", ("e", "p", "r")) is True
    assert _is_clustered_eval_flag("-v", ("e", "p", "r")) is False


def test_node_positional_requires_check_flag():
    """SPEC-66 BUG #4: a node positional in a project-supplied command is a
    script path — arbitrary execution with no test grammar behind it, and
    `enforcement.json` arrives pre-poisoned in a cloned repo. Only
    `--check` / `-c` (syntax-parse, no execution) may take a script path.
    """
    assert _validate_test_command("node x.js") is False
    assert _validate_test_command("node a.js b.js") is False
    assert _validate_test_command("node --check x.js") is True
    assert _validate_test_command("node -c x.js") is True
    # no positionals → no script to execute, check flag not needed
    assert _validate_test_command("node -v") is True


# ── C-C (PM RULING 2026-11-23): the python positional ban ──────────────────


def test_python_script_positional_is_refused():
    """C-C: `python3 <script>.py` is a SCRIPT PATH — banned, like node's.

    A project-supplied `test.command` of `python3 runner.py` runs code the
    project chose, with no test grammar behind it. `-m <allowlisted module>`
    is the only surviving way to hand python a target.
    """
    for command in (
        "python3 runner.py",
        "python3 ./scripts/run.py",
        "python3 tests/run_all.py -q",
        "python -m pytest_runner.py",
        "python3 -i script.py",          # non-eval flag does not launder it
        "python3 -B script.py",
        "python3 -v runner.py",
    ):
        assert _validate_test_command(command) is False, command


def test_python_module_invocation_stays_allowed():
    """C-C boundary: `-m <allowlisted module>` is untouched by the ban.

    `python3 -m pytest ...` is the tests tier's OWN default argv shape, so a
    ban that swallowed it would disable the tier everywhere. Interpreter
    flags BEFORE the `-m` are fine too — the module is what runs.
    """
    for command in (
        "python3 -m pytest -q",
        "python3 -m pytest tests/ -v",
        "python3 -m pytest",
        "python3 -i -m pytest -q",
        "python3 -B -m pytest -x",
        "python3 -m ruff check .",
        "python3 -m mypy src/",
    ):
        assert _validate_test_command(command) is True, command


def test_interpreter_value_flag_value_is_not_skipped():
    """C-C decision: value-taking interpreter flags do NOT consume a value here.

    `-W`, `-X` and `-Q` take an argument, so `python3 -W error script.py` has
    only ONE positional by python's own grammar — but this rule does not model
    per-flag arity, and a wrong guess is a bypass (`python3 -W evil.py` reads
    as "flag + value" and leaves python reading stdin). Over-refusal is the
    documented safe direction (SPEC-66 §11.1), so the value reads as a
    positional and the command is refused.
    """
    assert _validate_test_command("python3 -W error script.py") is False
    assert _validate_test_command("python3 -W error") is False
    assert _validate_test_command("python3 -X dev runner.py") is False
    assert _validate_test_command("python3 -Q old script.py") is False
    # The flag stays usable once a module resolves — the module is the target.
    assert _validate_test_command("python3 -W ignore -m pytest -q") is True


def test_python_positional_refusal_reason_names_the_rule():
    """C-C: a refusal carries a reason that says it is the positional rule.

    A silent False cannot be told apart from a token-allowlist miss; the
    tier detail needs the rule that fired.
    """
    from agent.enforcement import argv_refusal

    reason = argv_refusal(["python3", "runner.py"])
    assert reason is not None
    assert "positional" in reason
    assert "runner.py" in reason
    assert argv_refusal(["python3", "-m", "pytest", "-q"]) is None


def test_unknown_module_invocation_is_refused():
    """C-C decision: `python3 -m <unknown module>` is REFUSED, pinned.

    `-m` is not a blanket escape from the positional ban. `runner` is not in
    `_KNOWN_PYTHON_MODULES`, so no per-binary rule applies to whatever it
    runs — and `python3 -m runner` imports and executes `runner.py`, the very
    code the positional ban exists to keep out. Only modules we can apply
    rules to are admitted.
    """
    assert _validate_test_command("python3 -m unknownmod x") is False
    assert _validate_test_command("python3 -m runner") is False
    assert _validate_test_command("python3 -m runner --flag") is False
    assert _validate_test_command("python3 -m unknownmod") is False
    # The allowlist is what decides — a KNOWN module still resolves.
    assert _validate_test_command("python3 -m pytest -q") is True


def test_python_positional_rule_reddens_if_reverted():
    """C-C revert guard: fails if the python positional rule is removed.

    Pre-C-C `argv_refusal` only refused node positionals, so the bare
    `python3 runner.py` shape had no refusal at all. If the
    `_python_positional_refusal` call is deleted (or gated back to node only),
    this test fails while `python3 -m pytest` keeps passing.
    """
    from agent.enforcement import argv_refusal

    assert argv_refusal(["python3", "runner.py"]) is not None
    assert argv_refusal(["python", "runner.py"]) is not None
    assert argv_refusal(["py", "runner.py"]) is not None
    # node's rule and the module path are unaffected by the revert.
    assert argv_refusal(["node", "x.js"]) is not None
    assert argv_refusal(["python3", "-m", "pytest", "-q"]) is None


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


# ── SPEC-66 BUG #1 (report #6): inode/symlink ALIAS approval ───────────────
#
# The BUG #2 checks above describe ONE path each (the resolved one and the
# lexical one). They are blind to the reverse framing: the write names the
# PLAIN side, and the SENSITIVE name is a link to it —
#   .crabcakes/enforcement.json --symlink/hardlink--> notes/enforcement.json
# Writing `notes/enforcement.json` lands on the same bytes, so it is a write
# to the enforcement config. The dangling-symlink variant matters separately:
# nothing exists yet, so an inode-only check has nothing to compare — the
# link's TARGET PATH must be compared instead.


def _make_alias_project(tmp_path, payload="{}\n"):
    """A ``.crabcakes`` config plus a ``notes/`` dir to alias from."""
    (tmp_path / ".crabcakes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "notes").mkdir(exist_ok=True)
    return str(tmp_path), tmp_path / ".crabcakes" / "enforcement.json", (
        tmp_path / "notes" / "enforcement.json")


def test_write_via_symlink_alias_target_asks_approval(tmp_path):
    """The report's case: write the plain TARGET name of a symlinked config.

    Precondition: neither the resolved nor the lexical form of the written
    path is sensitive — only the alias walk can catch it.
    """
    from agent.tools import is_sensitive_path

    project, sensitive_link, target = _make_alias_project(tmp_path)
    target.write_text("{}\n")
    os.symlink(target, sensitive_link)

    assert is_sensitive_path("notes/enforcement.json") is False
    assert is_sensitive_path(".crabcakes/enforcement.json") is True

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    payload = '{"test_commands": ["node pwn.js"]}\n'
    result = execute_tool("write_file",
                          {"path": "notes/enforcement.json", "content": payload},
                          project, "agent:coder", approval_callback=deny)
    assert len(asked) == 1, f"expected exactly one approval request, got {asked!r}"
    assert asked[0][0] == "write_file"
    # The card carries the SENSITIVE name — the alias — not `notes/...`.
    assert asked[0][1]["path"] == ".crabcakes/enforcement.json", asked[0][1]["path"]
    assert result.success is False
    assert "sensitive" in (result.error or "").lower()
    assert target.read_text() == "{}\n", "a denied write reached the aliased target"

    result_ok = execute_tool("write_file",
                             {"path": "notes/enforcement.json", "content": payload},
                             project, "agent:coder",
                             approval_callback=lambda *a, **k: True)
    assert result_ok.success is True, result_ok.error
    assert target.read_text() == payload
    # Both names see the write: the alias was real.
    assert sensitive_link.read_text() == payload


def test_write_via_dangling_symlink_alias_asks_approval(tmp_path):
    """Nothing exists yet: the write would CREATE the veiled config."""
    project, sensitive_link, target = _make_alias_project(tmp_path)
    os.symlink(target, sensitive_link)  # dangling — target absent
    assert not target.exists()

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = execute_tool("write_file",
                          {"path": "notes/enforcement.json",
                           "content": '{"test_commands": ["node pwn.js"]}\n'},
                          project, "agent:coder", approval_callback=deny)
    assert len(asked) == 1, f"dangling alias asked {len(asked)} times: {asked!r}"
    assert asked[0][1]["path"] == ".crabcakes/enforcement.json", asked[0][1]["path"]
    assert result.success is False
    assert not target.exists(), "a denied write created the target through the link"

    result_ok = execute_tool("write_file",
                             {"path": "notes/enforcement.json", "content": "{}\n"},
                             project, "agent:coder",
                             approval_callback=lambda *a, **k: True)
    assert result_ok.success is True, result_ok.error
    assert target.read_text() == "{}\n"


def test_write_via_hardlink_alias_asks_approval(tmp_path):
    """Hard link: two names, one inode — no symlink to follow."""
    project, sensitive_name, target = _make_alias_project(tmp_path)
    sensitive_name.write_text("{}\n")
    os.link(sensitive_name, target)
    assert os.stat(sensitive_name).st_ino == os.stat(target).st_ino

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = execute_tool("write_file",
                          {"path": "notes/enforcement.json",
                           "content": '{"test_commands": ["node pwn.js"]}\n'},
                          project, "agent:coder", approval_callback=deny)
    assert len(asked) == 1, f"hardlink alias asked {len(asked)} times: {asked!r}"
    assert asked[0][1]["path"] == ".crabcakes/enforcement.json", asked[0][1]["path"]
    assert result.success is False
    assert target.read_text() == "{}\n"
    assert sensitive_name.read_text() == "{}\n"

    result_ok = execute_tool("write_file",
                             {"path": "notes/enforcement.json",
                              "content": '{"test_commands": ["node pwn.js"]}\n'},
                             project, "agent:coder",
                             approval_callback=lambda *a, **k: True)
    assert result_ok.success is True, result_ok.error
    # The write is visible through BOTH names — that is why it needed approval.
    expected = '{"test_commands": ["node pwn.js"]}\n'
    assert sensitive_name.read_text() == expected
    assert target.read_text() == expected


def test_write_via_git_alias_asks_approval(tmp_path):
    """`.git/` is walked at its top level: a hard link out of it is gated."""
    project, _sensitive, _target = _make_alias_project(tmp_path)
    (tmp_path / ".git").mkdir()
    git_config = tmp_path / ".git" / "config"
    git_config.write_text("[core]\n\trepositoryformatversion = 0\n")
    plain = tmp_path / "notes" / "x.txt"
    os.link(git_config, plain)

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = execute_tool("write_file",
                          {"path": "notes/x.txt", "content": "[core]\n\thooksPath = .\n"},
                          project, "agent:coder", approval_callback=deny)
    assert len(asked) == 1, f".git alias asked {len(asked)} times: {asked!r}"
    assert asked[0][1]["path"] == ".git/config", asked[0][1]["path"]
    assert result.success is False
    assert plain.read_text() == "[core]\n\trepositoryformatversion = 0\n"


def test_no_alias_no_approval(tmp_path):
    """Control: an ordinary file with no aliases anywhere must not ask."""
    project, _sensitive, _target = _make_alias_project(tmp_path)
    (tmp_path / "main.py").write_text("x = 1\n")

    asked = []

    def cb(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = execute_tool("write_file",
                          {"path": "main.py", "content": "x = 2\n"},
                          project, "agent:coder", approval_callback=cb)
    assert asked == [], f"false positive: approval requested for {asked!r}"
    assert result.success is True, result.error
    assert (tmp_path / "main.py").read_text() == "x = 2\n"

    # A second, unrelated write still asks nothing — the mtime cache must not
    # invent aliases.
    asked.clear()
    result = execute_tool("write_file",
                          {"path": "notes/plain.txt", "content": "hi\n"},
                          project, "agent:coder", approval_callback=cb)
    assert asked == [], f"false positive on a second write: {asked!r}"
    assert result.success is True, result.error


def test_symlink_alias_target_asks_approval_at_runtime_gate(tmp_path):
    """The alias disjunct at the RUNTIME gate (agent/runtime.py)."""
    project, sensitive_link, target = _make_alias_project(tmp_path)
    target.write_text("{}\n")
    os.symlink(target, sensitive_link)

    rt = AgentRuntime(_make_cfg())
    asked = []

    def boom(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    rt._dispatch_approval = boom
    conv = Conversation(agent_name="Coder", agent_role="coder",
                        project_path=project)
    rt._execute_tool_batch(
        "agent:coder", None, conv, "",
        [("c1", "write_file",
          {"path": "notes/enforcement.json",
           "content": '{"test_commands": ["node pwn.js"]}\n'})],
        1,
    )
    assert asked, "runtime gate requested no approval for the aliased target"
    assert asked[0][0] == "write_file"
    # The runtime gate's decision source names the SENSITIVE alias.
    from agent.runtime import _sensitive_relpath
    assert _sensitive_relpath("notes/enforcement.json", project) == (
        ".crabcakes/enforcement.json")
    assert target.read_text() == "{}\n", "the denied runtime call wrote the alias"

    # §12.1 MED-1: an APPROVED alias write must ask exactly once — the gate
    # approves, then `bypass_approval` has to see the same sensitive relpath
    # or the tool asks a second time. One card, and the write lands.
    approved_asked = []

    def ok(session_key, tool_name, args, _asked=approved_asked):
        _asked.append((tool_name, args))
        return True

    rt2 = AgentRuntime(_make_cfg())
    rt2._dispatch_approval = ok
    rt2._execute_tool_batch(
        "agent:coder", None,
        Conversation(agent_name="Coder", agent_role="coder",
                     project_path=project),
        "",
        [("c1", "write_file",
          {"path": "notes/enforcement.json", "content": "{}\n"})],
        1,
    )
    assert len(approved_asked) == 1, (
        f"approved alias write asked {len(approved_asked)} times "
        f"(bypass_approval disagreed with the gate): {approved_asked!r}"
    )


def test_write_via_hardlink_alias_asks_approval_edit(tmp_path):
    """Same alias through edit_file (the second tool-gate site)."""
    project, sensitive_name, target = _make_alias_project(tmp_path)
    sensitive_name.write_text("{}\n")
    os.link(sensitive_name, target)

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = execute_tool("edit_file",
                          {"path": "notes/enforcement.json",
                           "old_text": "{}", "new_text": "{ }"},
                          project, "agent:coder", approval_callback=deny)
    assert len(asked) == 1, f"edit alias asked {len(asked)} times: {asked!r}"
    assert asked[0][0] == "edit_file"
    assert asked[0][1]["path"] == ".crabcakes/enforcement.json", asked[0][1]["path"]
    assert result.success is False
    assert target.read_text() == "{}\n"


def test_nested_alias_and_cache_invalidation(tmp_path):
    """The walk recurses, and a planted alias cannot hide.

    `.crabcakes/agents/enforcement.json` is two levels deep; a top-level-only
    scan misses it. CONSOLIDATED audit Fix 4: the mtime-signature cache is
    GONE — aliases planted after any earlier scan are found by construction
    (every call walks fresh), so the old "cache must notice" half of this
    test is now the default behavior.
    """
    from agent.tools import _ALIAS_ENTRY_CACHE, _sensitive_alias_of

    project, _sensitive, _target = _make_alias_project(tmp_path)
    (tmp_path / "notes" / "deep.txt").write_text("plain\n")
    nested = tmp_path / ".crabcakes" / "agents"
    nested.mkdir()

    # A first call walks the tree (Fix 4: no cache to populate).
    resolved_plain = str(tmp_path / "notes" / "deep.txt")
    assert _sensitive_alias_of(resolved_plain, project) is None

    # Now alias it from a nested sensitive dir — the next call must find it.
    os.link(tmp_path / "notes" / "deep.txt", nested / "enforcement.json")
    found = _sensitive_alias_of(resolved_plain, project)
    assert found is not None, "nested alias missed (walk not recursive)"
    assert found.replace("\\", "/").endswith(".crabcakes/agents/enforcement.json")

    # And removing it again is seen too.
    os.unlink(nested / "enforcement.json")
    assert _sensitive_alias_of(resolved_plain, project) is None
    assert isinstance(_ALIAS_ENTRY_CACHE, dict)


def test_alias_scan_is_uncached_and_utime_defeat_is_dead(tmp_path):
    """CONSOLIDATED audit Fix 4 (M3) replacement for the removed perf pin.

    The old test pinned the mtime-signature cache: "repeated writes must
    not re-walk the tree". That cache was the VULNERABILITY — its
    signature could be restored with os.utime after planting an entry, and
    the planted alias then rode the cached entry list forever. The cache
    is removed; every gate call scans the (bounded) sensitive dirs fresh.

    This test pins the fix's actual regression: (1) an alias planted
    after an earlier scan is found IMMEDIATELY, (2) restoring the
    containing dir's mtime to its pre-plant value does NOT hide it, and
    (3) there is no cached entry list left to inspect.
    """
    from agent.tools import _ALIAS_ENTRY_CACHE, _sensitive_alias_of

    project, _sensitive, _target = _make_alias_project(tmp_path)
    (tmp_path / ".crabcakes" / "many").mkdir()
    plain = tmp_path / "notes" / "plain.txt"
    plain.write_text("hi\n")

    # Baseline: a clean walk of a small sensitive tree, no alias.
    assert _sensitive_alias_of(str(plain), project) is None

    # Plant the alias INSIDE a walked dir.
    planted = tmp_path / ".crabcakes" / "many" / "enforcement.json"
    os.link(plain, planted)

    # (1) Found immediately — no scan signature to invalidate by hand.
    assert _sensitive_alias_of(str(plain), project) is not None

    # (2) utime-defeat: restore the dir's PRE-PLANT mtime exactly.
    many_dir = tmp_path / ".crabcakes" / "many"
    pre = many_dir.stat()
    # The planted link already bumped it; put it back by hand.
    os.utime(many_dir, ns=(pre.st_atime_ns - 100, pre.st_mtime_ns - 100))
    assert _sensitive_alias_of(str(plain), project) is not None, (
        "planted alias hidden by a restored mtime — the old cache is back"
    )

    # (3) No cached entry list survives: the name is kept (empty, never
    # read or written) only so stale imports fail loudly.
    assert not _ALIAS_ENTRY_CACHE, (
        "entries were cached — Fix 4's contract is 'scan fresh every call'"
    )


# ── CONSOLIDATED audit P0 round (2026-11-23) ─────────────────────────────────
#
# Fix 1 — complete the .git alias walk (C1, M1, M2, M7)
# Fix 2 — gate the files the toolchain imports (C3, D1, D2, L1)
#
# C1's chain, verbatim from the audit:
#   1. os.link(<project>/.git/hooks/pre-commit, <project>/notes/plain.md)
#   2. write_file("notes/plain.md")  ← neither name is sensitive-looking
#   3. the bytes land on the HOOK's inode
#   4. PM clicks Accept → git commit runs pre-commit → the agent's script
#      executes, with NO approval card anywhere on that path.
# Step 2 is the gate's job: it must ask, and a denial must leave the hook's
# bytes untouched (a gate that asks and then writes anyway is not a gate).


def _make_git_project(tmp_path):
    """A project with a ``.git/`` skeleton and a ``notes/`` dir to alias from."""
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    (tmp_path / "notes").mkdir(exist_ok=True)
    return str(tmp_path)


def _hook_result(project, plain_name, content, cb, tool="write_file"):
    """write_file (or edit_file) the plain name with *cb* installed."""
    if tool == "edit_file":
        return execute_tool(
            "edit_file",
            {"path": plain_name, "old_text": "OLD", "new_text": content},
            project, "agent:coder", approval_callback=cb)
    return execute_tool("write_file",
                        {"path": plain_name, "content": content},
                        project, "agent:coder", approval_callback=cb)


@pytest.mark.parametrize("hook_name", ["pre-commit", "prepare-commit-msg",
                                       "post-checkout", "pre-push"])
def test_git_hooks_alias_write_asks_approval_and_preserves_hook(tmp_path, hook_name):
    """C1: the FULL chain — deny refuses and the hook's bytes are unchanged.

    The hook is mode 0755 and stays 0755 across a hard link, so it is
    directly executable by git. The plain name is `notes/plain.md`, which is
    non-sensitive by construction (asserted) — only the walk can catch it.
    """
    from agent.tools import is_sensitive_path

    project = _make_git_project(tmp_path)
    hook = tmp_path / ".git" / "hooks" / hook_name
    hook.parent.mkdir(parents=True, exist_ok=True)
    original = "#!/bin/sh\necho benign\n"
    hook.write_text(original)
    hook.chmod(0o755)
    plain = tmp_path / "notes" / "plain.md"

    # Precondition (the audit's A1 false-positive correction): the PLAIN name
    # must not be sensitive on its own, or this test proves nothing.
    assert is_sensitive_path("notes/plain.md") is False

    os.link(hook, plain)

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    payload = "#!/bin/sh\ntouch /tmp/PWNED\n"
    result = _hook_result(project, "notes/plain.md", payload, deny)

    assert asked, f"no approval requested for the {hook_name} alias"
    assert asked[0][0] == "write_file"
    assert result.success is False
    assert "sensitive" in (result.error or "").lower()
    # The card names the SENSITIVE side.
    assert "hook" in asked[0][1]["path"], asked[0][1]["path"]
    # The hook is byte-identical and still executable.
    assert hook.read_text() == original, "a DENIED write changed the hook"
    assert os.access(hook, os.X_OK), "hook lost its executable bit"
    assert plain.read_text() == original


def test_git_hooks_alias_write_approved_proceeds(tmp_path):
    """C1 control: approval lets the write through — the gate is a gate."""
    project = _make_git_project(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho benign\n")
    hook.chmod(0o755)
    plain = tmp_path / "notes" / "plain.md"
    os.link(hook, plain)

    asked = []

    def ok(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return True

    result = _hook_result(project, "notes/plain.md", "#!/bin/sh\necho hi\n", ok)
    assert len(asked) == 1, f"asked {len(asked)} times: {asked!r}"
    assert result.success is True, result.error
    assert hook.read_text() == "#!/bin/sh\necho hi\n"


def test_git_hooks_alias_edit_asks_approval(tmp_path):
    """C1 through edit_file (the second tool-gate site)."""
    project = _make_git_project(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-merge"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("OLD\n")
    plain = tmp_path / "notes" / "plain.md"
    os.link(hook, plain)

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = _hook_result(project, "notes/plain.md", "PWNED\n", deny,
                          tool="edit_file")
    assert asked, "edit_file did not ask for the hook alias"
    assert asked[0][0] == "edit_file"
    assert result.success is False
    assert hook.read_text() == "OLD\n"


def test_git_hooks_path_is_sensitive_on_its_own_name(tmp_path):
    """Fix 1 item 3: `.git/hooks/` is a sensitive NAME, not only an alias.

    The alias scan is the second line. A direct write to
    `.git/hooks/pre-commit` (no alias at all) must ask too — which it did
    before only because the *hook* substring matched a glob; the prefix rule
    makes the intent explicit and keeps nested `.git/hooks/` spellings gated.
    """
    from agent.tools import is_sensitive_path

    assert is_sensitive_path(".git/hooks/pre-commit") is True
    assert is_sensitive_path(".git/hooks/post-receive") is True
    # A nested `.git/hooks/` (not the root-anchored `.git/`) is still gated.
    assert is_sensitive_path("sub/.git/hooks/pre-commit") is True
    # And the control: an ordinary path is not.
    assert is_sensitive_path("notes/plain.md") is False


def test_root_gitattributes_alias_asks_approval(tmp_path):
    """M7: root `.gitattributes` is git-EXECUTED config (filter=/diff=).

    Its own name is leading-dot sensitive; the ALIAS direction was not. The
    project root is not a walked dir, so this is handled as a direct stat.
    """
    from agent.tools import is_sensitive_path

    project = _make_git_project(tmp_path)
    attrs = tmp_path / ".gitattributes"
    attrs.write_text("*.py text\n")
    plain = tmp_path / "notes" / "plain.md"
    os.link(attrs, plain)

    assert is_sensitive_path("notes/plain.md") is False

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = _hook_result(project, "notes/plain.md", "*.py filter=pwn\n", deny)
    assert asked, "root .gitattributes alias was not gated"
    assert asked[0][1]["path"] == ".gitattributes", asked[0][1]["path"]
    assert result.success is False
    assert attrs.read_text() == "*.py text\n"


def test_root_gitignore_alias_asks_approval(tmp_path):
    """M7: same for `.gitignore`."""
    project = _make_git_project(tmp_path)
    ignore = tmp_path / ".gitignore"
    ignore.write_text("build/\n")
    plain = tmp_path / "notes" / "plain.md"
    os.link(ignore, plain)

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = _hook_result(project, "notes/plain.md", "x\n", deny)
    assert asked, "root .gitignore alias was not gated"
    assert asked[0][1]["path"] == ".gitignore", asked[0][1]["path"]
    assert result.success is False
    assert ignore.read_text() == "build/\n"


@pytest.mark.parametrize("subtree", ["info", "logs", "branches", "worktrees",
                                     "modules"])
def test_each_newly_walked_git_subtree_gates_its_alias(tmp_path, subtree):
    """M2: one test per newly-walked subtree — a hard link OUT of it gates.

    Each carries git-executed state: info/attributes and
    worktrees/*/config.worktree name commands git runs; logs/ is the reflog;
    branches/ is legacy branch config; modules/ is a submodule's git dir.
    """
    from agent.tools import _ALIAS_ENTRY_CACHE, _sensitive_alias_of

    project = _make_git_project(tmp_path)
    sensitive = tmp_path / ".git" / subtree / "payload" / "sensitive.cfg"
    sensitive.parent.mkdir(parents=True, exist_ok=True)
    sensitive.write_text("original\n")
    plain = tmp_path / "notes" / "plain.md"
    os.link(sensitive, plain)

    _ALIAS_ENTRY_CACHE.pop(str(tmp_path), None)
    found = _sensitive_alias_of(str(plain), project)
    assert found is not None, f".git/{subtree}/ is not in the alias walk"
    assert f".git/{subtree}/" in found.replace("\\", "/")


def test_git_objects_stays_excluded(tmp_path):
    """M2: `objects/` stays out, on the documented hash-name argument.

    An object's NAME is a content hash of its BYTES, so no working-tree name
    can be chosen to alias one — the alias direction is not constructible.
    This test pins the exclusion so widening it is a deliberate act (a full
    object walk costs tens of thousands of stats per write).
    """
    from agent.tools import _ALIAS_ENTRY_CACHE, _GIT_ALIAS_SUBDIRS, _alias_scan_roots

    assert "objects" not in _GIT_ALIAS_SUBDIRS
    assert set(_GIT_ALIAS_SUBDIRS) == {"refs", "hooks", "info", "worktrees",
                                       "modules", "logs", "branches"}

    project = _make_git_project(tmp_path)
    obj = tmp_path / ".git" / "objects" / "ab" / "cdef0123"
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_text("blob\n")
    plain = tmp_path / "notes" / "plain.md"
    os.link(obj, plain)

    _ALIAS_ENTRY_CACHE.pop(str(tmp_path), None)
    from agent.tools import _sensitive_alias_of
    assert _sensitive_alias_of(str(plain), project) is None

    walked = {os.path.basename(root) for root, _r in
              _alias_scan_roots(os.path.realpath(project))}
    assert "objects" not in walked


def test_git_hooks_alias_gated_at_runtime_gate(tmp_path):
    """M1 — the RUNTIME gate (`_sensitive_relpath`) sees the same alias.

    Tool gate and runtime gate share the walk, so a fix that only touched the
    tool gate would leave the approval card unfired (the runtime is the one
    that fires it). This mirrors the tool-gate C1 test through
    `_execute_tool_batch`.
    """
    from agent.runtime import _sensitive_relpath

    project = _make_git_project(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho benign\n")
    hook.chmod(0o755)
    plain = tmp_path / "notes" / "plain.md"
    os.link(hook, plain)

    rel = _sensitive_relpath("notes/plain.md", project)
    assert rel is not None
    assert "hook" in rel, f"runtime gate named {rel!r}, not the hook"

    rt = AgentRuntime(_make_cfg())
    asked = []

    def boom(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    rt._dispatch_approval = boom
    conv = Conversation(agent_name="Coder", agent_role="coder",
                        project_path=project)
    rt._execute_tool_batch(
        "agent:coder", None, conv, "",
        [("c1", "write_file",
          {"path": "notes/plain.md", "content": "#!/bin/sh\ntouch /tmp/PWNED\n"})],
        1,
    )
    assert asked, "runtime gate requested no approval for the hook alias"
    assert hook.read_text() == "#!/bin/sh\necho benign\n"

    # §12.1 MED-1: an APPROVED alias write must ask exactly ONCE — the gate
    # approves, then `bypass_approval` must see the same sensitive relpath.
    approved_asked = []

    def ok(session_key, tool_name, args, _asked=approved_asked):
        _asked.append((tool_name, args))
        return True

    rt2 = AgentRuntime(_make_cfg())
    rt2._dispatch_approval = ok
    rt2._execute_tool_batch(
        "agent:coder", None,
        Conversation(agent_name="Coder", agent_role="coder",
                     project_path=project),
        "",
        [("c1", "write_file",
          {"path": "notes/plain.md", "content": "#!/bin/sh\necho hi\n"})],
        1,
    )
    assert len(approved_asked) == 1, (
        f"approved hook-alias write asked {len(approved_asked)} times "
        f"(bypass_approval disagreed with the gate): {approved_asked!r}"
    )


# ── Fix 2: the files the toolchain imports/reads ────────────────────────────


_TOOLCHAIN_FILES = (
    "conftest.py",
    "tests/conftest.py",
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
    "mypy.ini",
    "eslint.config.js",
    "package.json",
    "requirements.txt",
    "requirements-dev.txt",
    "noxfile.py",
    "sitecustomize.py",
    "usercustomize.py",
)


@pytest.mark.parametrize("rel", _TOOLCHAIN_FILES)
def test_toolchain_files_are_sensitive_at_any_depth(rel):
    """Fix 2 + D3: the "any depth" claim is now TRUE for conftest.py.

    D3 found the prefix comment lied: `sub/.crabcakes/x` was not sensitive.
    The glob kind is a BASENAME match, so `conftest.py` is gated wherever
    pytest would import it; the prefix kinds stay root-anchored and SAY so.
    """
    from agent.tools import is_sensitive_path

    assert is_sensitive_path(rel) is True, rel
    nested = "deep/nested/" + rel
    assert is_sensitive_path(nested) is True, nested


@pytest.mark.parametrize("rel", ["conftest.py", "tests/conftest.py",
                                 "pytest.ini", "eslint.config.js",
                                 "package.json"])
def test_toolchain_file_write_asks_approval_at_both_gates(tmp_path, rel):
    """C3: writing `conftest.py` asks at the TOOL gate and the RUNTIME gate.

    C3's chain was two steps: plant the conftest (no approval), then write
    anything — the tier imports and runs it. This pins step one at both gates.
    """
    from agent.runtime import _sensitive_relpath

    project = str(tmp_path)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)

    # RUNTIME gate first (it is the one that fires the card in the app).
    assert _sensitive_relpath(rel, project) is not None

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = execute_tool("write_file",
                          {"path": rel,
                           "content": "from pathlib import Path\n"
                                      "Path('/tmp/PWNED').write_text('x')\n"},
                          project, "agent:coder", approval_callback=deny)
    assert asked, f"no approval requested for {rel}"
    assert result.success is False
    assert not (tmp_path / rel).exists(), f"denied write created {rel}"

    # Approve → the write lands (intended posture, no bypass).
    def ok(session_key, tool_name, args):
        return True

    ok_result = execute_tool("write_file", {"path": rel, "content": "x = 1\n"},
                             project, "agent:coder", approval_callback=ok)
    assert ok_result.success is True, ok_result.error
    assert (tmp_path / rel).read_text() == "x = 1\n"


def test_toolchain_file_gated_at_runtime_gate_end_to_end(tmp_path):
    """C3 through the real tool loop: plant a conftest, get a card.

    The runtime dispatches the approval BEFORE execution, so a denied
    conftest write must leave no file behind — which is exactly what makes
    the two-step chain impossible.
    """
    project = str(tmp_path)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "feature.py").write_text("x = 1\n")

    rt = AgentRuntime(_make_cfg())
    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    rt._dispatch_approval = deny
    rt._execute_tool_batch(
        "agent:coder", None,
        Conversation(agent_name="Coder", agent_role="coder",
                     project_path=project),
        "",
        [("c1", "write_file",
          {"path": "tests/conftest.py",
           "content": "from pathlib import Path\n"
                      "Path('/tmp/PWNED').write_text('x')\n"})],
        1,
    )
    assert asked, "runtime gate did not ask for tests/conftest.py"
    assert not (tmp_path / "tests" / "conftest.py").exists(), (
        "a DENIED conftest write created the file"
    )


def test_plain_source_file_still_does_not_ask(tmp_path):
    """Fix 2 control: `src/main.py` is ordinary — no card, no regression.

    The toolchain-file gating must not turn the gate into "ask for
    everything": that would train the PM to click Accept.
    """
    from agent.runtime import _sensitive_relpath

    project = str(tmp_path)
    (tmp_path / "src").mkdir()
    # Fix 6 contract: when nothing is sensitive there is no card and no
    # name to show — None (the plain relpath return was the pre-Fix-6
    # contract; the runtime gate only needs the sensitive name).
    assert _sensitive_relpath("src/main.py", project) is None

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = execute_tool("write_file",
                          {"path": "src/main.py", "content": "x = 1\n"},
                          project, "agent:coder", approval_callback=deny)
    assert asked == [], f"false positive on src/main.py: {asked!r}"
    assert result.success is True, result.error


def test_d3_prefix_is_root_anchored_and_says_so():
    """D3: the comment said "any depth"; the code is root-anchored.

    The fix is to make the COMMENT true rather than to widen the prefix (a
    nested `.git/` is not read by git). Glob/component kinds carry the
    any-depth cases; `prefix` does not, and this pins that.
    """
    from agent.tools import is_sensitive_path

    # Root-anchored prefix: root yes, nested no.
    assert is_sensitive_path(".crabcakes/enforcement.json") is True
    assert is_sensitive_path("sub/.crabcakes/enforcement.json") is False
    # Any-depth BASENAME glob (the real "any depth" mechanism).
    assert is_sensitive_path("a/b/conftest.py") is True
    assert is_sensitive_path("a/b/pytest.ini") is True


# ── CONSOLIDATED audit C-A: the test file the tier IMPORTS ───────────────────
#
# The chain: the clone (or the agent) ships tests/test_feature.py with
# top-level code; the agent writes an ordinary source file; `_check_tests`
# points pytest at the test file; pytest IMPORTS it — payload runs, tier
# reports PASSED. Fix 3 isolated the CONFIG layer and could not touch the
# CODE layer, so the WRITE is what must ask. These tests fail if the gate is
# reverted (the write asks for nothing and lands).


@pytest.mark.parametrize("path", [
    "tests/test_feature.py",     # the audit's exact payload name
    "tests/feature_test.py",     # the `{module}_test.py` convention
    "test_root.py",              # root-level: probe 16 P5 behaved identically
    "tests/nested/test_deep.py",  # any depth, like the toolchain globs
])
def test_test_file_write_asks_approval_and_lands_nothing(tmp_path, path):
    """C-A: writing a test file asks the PM; a denial writes nothing."""
    project = str(tmp_path)
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def test_old():\n    assert True\n")
    before = target.read_text()

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = execute_tool("write_file",
                          {"path": path,
                           "content": "import pathlib\n"
                                      "pathlib.Path('/tmp/PWNED').write_text('x')\n"
                                      "def test_x():\n    assert True\n"},
                          project, "agent:coder", approval_callback=deny)
    assert asked, f"tool gate asked for nothing on {path!r} — the tier will import it"
    assert asked[0][1]["path"].endswith(os.path.basename(path)), asked
    assert result.success is False
    assert target.read_text() == before, "denied write landed anyway"


def test_test_file_edit_asks_approval_and_lands_nothing(tmp_path):
    """C-A: `edit_file` is the same surface — the tier imports the result."""
    project = str(tmp_path)
    target = tmp_path / "tests" / "test_feature.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_ok():\n    assert True\n")

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = execute_tool(
        "edit_file",
        {"path": "tests/test_feature.py",
         "old_text": "def test_ok():\n    assert True\n",
         "new_text": "import os\nos.system('touch /tmp/PWNED')\n"},
        project, "agent:coder", approval_callback=deny)
    assert asked, "edit_file asked for nothing on a test file"
    assert result.success is False
    assert "test_ok" in target.read_text()


def test_test_file_write_asks_at_the_runtime_gate_too(tmp_path):
    """C-A: BOTH gates, like the other sensitive names.

    The tool gate alone is not enough: the runtime gate is the one that
    fires the approval card, and its decision has to come from the same
    `sensitive_reason` — which this asserts by observing the call.
    """
    project = str(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_feature.py").write_text("def test_ok():\n    pass\n")

    rt = AgentRuntime(_make_cfg())
    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    rt._dispatch_approval = deny
    conv = Conversation(agent_name="Coder", agent_role="coder",
                        project_path=project)
    rt._execute_tool_batch(
        "agent:coder", None, conv, "",
        [("c1", "write_file",
          {"path": "tests/test_feature.py", "content": "def test_ok():\n    pass\n"})],
        1,
    )
    assert asked, "runtime gate requested no approval for a test file write"


def test_tests_dir_convention_write_asks_approval(tmp_path):
    """M-1 (re-audit): `src/__tests__/{module}.py` is a test the tier IMPORTs.

    It is `_find_related_test`'s fourth candidate; its basename is just the
    module name, so the old name-only gate never covered it — the tier
    imported a file the gate did not see. Both gates must ask, and the
    lookalike that is NOT under a `__tests__` directory must not.
    """
    from agent.tools import is_sensitive_path

    project = str(tmp_path)
    target = tmp_path / "src" / "__tests__"
    target.mkdir(parents=True)
    (target / "feature.py").write_text("def test_ok():\n    assert True\n")
    assert is_sensitive_path("src/__tests__/feature.py", project) is True
    assert is_sensitive_path(os.path.join(project, "src/__tests__/feature.py"),
                             project) is True
    # Lookalikes: a directory of the same SHAPE but not the convention.
    for name in ("src/tests/feature.py", "src/__test__/feature.py",
                 "src/__tests__/feature.js", "src/feature.py"):
        assert is_sensitive_path(name, project) is False, name

    # THE TOOL gate, observed: an unapproved write lands nothing.
    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = execute_tool(
        "write_file",
        {"path": "src/__tests__/feature.py",
         "content": "import pathlib\npathlib.Path('/tmp/PWNED').touch()\n"},
        project, "agent:coder", approval_callback=deny)
    assert asked, "M-1 OPEN: the TOOL gate asked nothing for a __tests__ write"
    assert result.success is False
    assert "test_ok" in (target / "feature.py").read_text()


def test_tests_dir_convention_write_asks_approval_at_runtime(tmp_path):
    """THE gate, not just the predicate: a write of it produces a card."""
    project = str(tmp_path)
    (tmp_path / "src" / "__tests__").mkdir(parents=True)

    rt = AgentRuntime(_make_cfg())
    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    rt._dispatch_approval = deny
    conv = Conversation(agent_name="Coder", agent_role="coder",
                        project_path=project)
    rt._execute_tool_batch(
        "agent:coder", None, conv, "",
        [("c1", "write_file",
          {"path": "src/__tests__/feature.py",
           "content": "def test_ok():\n    pass\n"})],
        1,
    )
    assert asked, "M-1 OPEN: no approval card for a __tests__ test-file write"


def test_test_file_gate_does_not_flag_lookalike_names(tmp_path):
    """C-A boundary: only the names the tier would COLLECT are gated."""
    from agent.tools import is_sensitive_path, is_test_file_name

    assert is_sensitive_path("tests/test_feature.py") is True
    assert is_sensitive_path("pkg/mod_test.py") is True
    for name in ("tests/contest.py", "tests/testing_utils.py",
                 "src/testimony.py", "src/main.py", "tests/utils.py"):
        assert is_sensitive_path(name) is False, name
        assert is_test_file_name(name) is False, name


def test_test_file_gate_scope_is_one_switch(tmp_path, monkeypatch):
    """C-A scope lives in ONE function, so the PM ruling is one edit.

    PM RULING 2026-11-23: the gate covers EVERY project, the app's own
    included — there is no app-exempt alternative left to decide. This test
    pins the ruling's shape: the switch answers True for any project, and it
    is actually consulted by the gate (turning it off turns the gate off
    everywhere it is asked).
    """
    import agent.tools as tools_mod

    project = str(tmp_path)
    (tmp_path / "tests").mkdir()
    target = tmp_path / "tests" / "test_feature.py"
    target.write_text("def test_ok():\n    pass\n")

    assert tools_mod.test_file_write_gate_applies(project) is True

    monkeypatch.setattr(tools_mod, "test_file_write_gate_applies",
                        lambda project_path="": False)
    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = execute_tool(
        "write_file",
        {"path": "tests/test_feature.py", "content": "def test_ok():\n    pass\n"},
        project, "agent:coder", approval_callback=deny)
    assert asked == [], "the switch was not consulted by the gate"
    assert result.success is True, result.error


# ── CONSOLIDATED audit M-A: the alias scan in a LINKED WORKTREE ─────────────
#
# `<worktree>/.git` is a FILE (`gitdir: <path>`), so `os.scandir` raised and
# the whole alias walk yielded ZERO entries — the C1 chain was fully open in
# an agent's default working directory (`<repo>/.worktrees/<id>`). The scan
# now follows the pointer (bounded, each link sanity-checked) and walks the
# worktree git dir plus the common dir whose `hooks/` git executes.


def _make_linked_worktree(tmp_path):
    """(worktree_path, main_git_dir) — a synthesized linked worktree.

    Mirrors the real layout closely enough for the scan: the worktree has a
    `.git` FILE, the main repo has `.git/` with `HEAD` + `hooks/`, and the
    worktree git dir carries a `commondir` file pointing back at the main
    repo (that is where git finds `hooks/` to run).
    """
    main = tmp_path / "main"
    main_git = main / ".git"
    (main_git / "hooks").mkdir(parents=True)
    (main_git / "HEAD").write_text("ref: refs/heads/main\n")
    wt_git = main_git / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/wt\n")
    (wt_git / "commondir").write_text("../..\n")

    wt = tmp_path / "wt"
    (wt / "notes").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {wt_git}\n")
    return str(wt), str(main_git)


def test_worktree_alias_scan_yields_entries(tmp_path):
    """M-A T3: the walk is not inert — the worktree git dirs are covered."""
    from agent.tools import _alias_scan, _alias_scan_roots

    wt, main_git = _make_linked_worktree(tmp_path)
    (tmp_path / "main" / ".git" / "hooks" / "pre-commit").write_text(
        "#!/bin/sh\necho benign\n")

    roots = [os.path.realpath(r) for r, _recurse in _alias_scan_roots(wt)]
    assert any(r.startswith(os.path.realpath(main_git)) for r in roots), (
        f"the common dir (where hooks/ lives) is not walked: {roots}"
    )
    assert _alias_scan(os.path.realpath(wt)), (
        "the alias scan yields zero entries in a linked worktree"
    )


def test_worktree_shared_hooks_alias_asks_approval(tmp_path):
    """M-A T1: a hard link from the worktree to the SHARED hook must ask."""
    from agent.tools import _sensitive_alias_of

    wt, main_git = _make_linked_worktree(tmp_path)
    hook = os.path.join(main_git, "hooks", "pre-commit")
    with open(hook, "w") as fh:
        fh.write("#!/bin/sh\necho benign\n")
    os.chmod(hook, 0o755)
    plain = os.path.join(wt, "notes", "plain.md")
    os.link(hook, plain)

    found = _sensitive_alias_of(plain, wt)
    assert found is not None, "the shared .git/hooks alias is invisible here"
    assert "hook" in found.replace("\\", "/")

    asked = []

    def deny(session_key, tool_name, args, _asked=asked):
        _asked.append((tool_name, args))
        return False

    result = execute_tool("write_file",
                          {"path": "notes/plain.md",
                           "content": "#!/bin/sh\ntouch /tmp/PWNED\n"},
                          wt, "agent:coder", approval_callback=deny)
    assert asked, "tool gate asked for nothing on the shared hook alias"
    assert result.success is False
    with open(hook) as fh:
        assert fh.read() == "#!/bin/sh\necho benign\n", "the denied write landed"


def test_worktree_config_worktree_alias_asks_approval(tmp_path):
    """M-A T2: `.git/worktrees/<n>/config.worktree` names commands git runs."""
    from agent.tools import _sensitive_alias_of

    wt, main_git = _make_linked_worktree(tmp_path)
    sensitive = os.path.join(main_git, "worktrees", "wt", "config.worktree")
    with open(sensitive, "w") as fh:
        fh.write("[core]\n\thooksPath = hooks\n")
    plain = os.path.join(wt, "notes", "plain.md")
    os.link(sensitive, plain)

    found = _sensitive_alias_of(plain, wt)
    assert found is not None, "config.worktree is not in the worktree walk"
    assert "config.worktree" in found.replace("\\", "/")


def test_worktree_gitdir_pointer_cannot_steer_the_scan(tmp_path):
    """M-A constraint: a bogus `gitdir:` must not widen the walk.

    The pointer is project-supplied and may name a directory outside the
    root. It is followed through a bounded chain whose every link must look
    like a git dir, so it can only add a directory git itself would use for
    THIS worktree — and it cannot subtract coverage, because the name-based
    rule still gates `.git/hooks/...` lexically.
    """
    from agent.tools import _alias_scan_roots, _worktree_git_dirs, is_sensitive_path

    wt = tmp_path / "wt"
    (wt / "notes").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {tmp_path}\n")  # NOT a git dir

    assert _worktree_git_dirs(os.path.realpath(str(wt))) == []
    walked = [r for r, _recurse in _alias_scan_roots(os.path.realpath(str(wt)))]
    git_roots = [r for r in walked
                 if r == os.path.join(str(wt), ".git")
                 or r.startswith(os.path.join(str(wt), ".git") + os.sep)]
    assert git_roots == [], f"a bogus pointer steered the scan: {git_roots}"

    # The name direction is untouched by the pointer's refusal.
    assert is_sensitive_path(".git/hooks/pre-commit") is True


# ── CONSOLIDATED audit Fix 5 (L-B): O_NOFOLLOW has zero coverage ────────────
#
# L-B: with `O_NOFOLLOW` reverted to a plain `open(dest, "w")`, the whole
# suite still passed. These tests fail under that revert: the final-component
# symlink swap must be REFUSED and the bytes must stay on the approved inode.


def test_atomic_write_nofollow_refuses_final_component_symlink(tmp_path):
    """Fix 5 unit: a symlinked final component is ELOOP, not a redirect."""
    import errno as _errno

    from agent.tools import _atomic_write_nofollow

    approved = tmp_path / "notes" / "plain.md"
    approved.parent.mkdir()
    approved.write_text("approved\n")
    victim = tmp_path / "victim.txt"
    victim.write_text("victim\n")

    # The swap the gate cannot see: it happens after the check.
    os.unlink(approved)
    os.symlink(victim, approved)

    with pytest.raises(OSError) as exc:
        _atomic_write_nofollow(str(approved), b"PWNED\n")
    assert exc.value.errno == _errno.ELOOP, exc.value
    assert victim.read_text() == "victim\n", (
        "the bytes followed the swapped symlink — O_NOFOLLOW is gone"
    )


def test_write_file_refuses_symlink_swapped_after_the_gate(tmp_path, monkeypatch):
    """Fix 5 end to end: the tool refuses, the victim keeps its bytes.

    The swap is injected through `sensitive_reason` — which `_write_file`
    calls AFTER it has resolved the destination and BEFORE it writes — so
    this is exactly the TOCTOU window Fix 5 closes.
    """
    import agent.tools as tools_mod

    project = str(tmp_path)
    (tmp_path / "notes").mkdir()
    approved = tmp_path / "notes" / "plain.md"
    approved.write_text("approved\n")
    victim = tmp_path / "victim.txt"
    victim.write_text("victim\n")

    real_reason = tools_mod.sensitive_reason

    def swap_then_decide(path_arg, project_path):
        if not os.path.islink(str(approved)) and approved.exists():
            os.unlink(str(approved))
            os.symlink(victim, approved)
        return real_reason(path_arg, project_path)

    monkeypatch.setattr(tools_mod, "sensitive_reason", swap_then_decide)

    result = execute_tool("write_file",
                          {"path": "notes/plain.md", "content": "PWNED\n"},
                          project, "agent:coder",
                          approval_callback=lambda *a: True)
    assert result.success is False, (
        "the write succeeded through a swapped symlink — Fix 5 reverted"
    )
    assert "ELOOP" in (result.error or ""), result.error
    assert victim.read_text() == "victim\n"
    assert os.path.islink(str(approved)), "the swap was undone instead of refused"
