# tests/test_crab_status_cli.py
# Tests for scripts/crab_status.py — the AGENTCTRL1 Phase 1b CLI shim
# (SPEC-AGENT-CONTROL-1 §2.2 exit codes, §2.3 invariants, §2.4 alert path,
# plus the §7 argv rows).
#
# HERMETICITY: HOME / XDG_CONFIG_HOME / XDG_CACHE_HOME are redirected into
# tmp_path and the reporter's PROC_ROOT / CRASH_DIR are pointed at fixture
# trees, so nothing in this file reads or writes the real ~/.config/crabcakes,
# the real /proc, or /var/crash. The CLI is driven in-process through
# `main(argv)` — never through a shell-out (except the two subprocess tests,
# which are the point: they prove the script is runnable standalone, headless,
# with the app closed).
#
# HEADLESS: no GTK anywhere in this path — this suite runs without xvfb.

import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

import utils.status_report as status_report

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI_PATH = PROJECT_ROOT / "scripts" / "crab_status.py"

SECRET = "AKIA-SUPER-SECRET-KEY-1234567890"  # must never reach stdout/stderr

# Captured at import time, before the autouse fixture redirects HOME: child
# processes need the real HOME so the interpreter's user site-packages (where
# GitPython lives) resolves — see test_script_runs_standalone_with_app_closed.
REAL_HOME = os.path.expanduser("~")


def _load_cli():
    """Load scripts/crab_status.py by path (scripts/ is not an importable pkg)."""
    spec = importlib.util.spec_from_file_location("crab_status_cli", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Redirect HOME/XDG + /proc + /var/crash into tmp_path for every test."""
    home = tmp_path / "home"
    (home / "proc").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    # Default: /proc readable but no app running, no crash reports.
    monkeypatch.setattr(status_report, "PROC_ROOT", str(home / "proc"))
    monkeypatch.setattr(status_report, "CRASH_DIR", str(home / "crash"))
    return home


# ── Fixture builders ─────────────────────────────────────────────────────────

def _project(tmp_path, name="proj"):
    project = tmp_path / name
    (project / "docs" / "specs").mkdir(parents=True)
    (project / ".crabcakes").mkdir(parents=True)
    (project / "docs" / "specs" / "SPEC-DEMO.md").write_text("# demo\n")
    (project / ".crabcakes" / "tasks.md").write_text(
        "# Work Units\n\n## 00000007 - Demo unit\n- **Status:** Done\n- **Priority:** High\n"
    )
    return project


def _config_dir(tmp_path):
    # SPEC-11 SP1 (D4): the CLI resolves config via get_config_dir() →
    # develcakes now. Was crabcakes — spec-drift update with disclosure.
    d = tmp_path / "home" / ".config" / "develcakes"
    (d / "conversations").mkdir(parents=True, exist_ok=True)
    return d


def _msg(role, content, ts, tool_calls=None):
    return {
        "role": role,
        "content": content,
        "tool_calls": tool_calls or [],
        "tool_call_id": "call_deadbeef" if role == "tool" else "",
        "tokens_used": 1,
        "timestamp": datetime.fromtimestamp(ts).isoformat(),
    }


def _tool_call(name, args=None):
    return {"call_id": "call_1", "tool_name": name, "arguments": args or {}}


def _write_conversation(config_dir, session_key, project, messages,
                        mtime=None, agent_name="Coder", model="test/model"):
    path = Path(config_dir) / "conversations" / f"{session_key}.json"
    path.write_text(json.dumps({
        "session_key": session_key,
        "agent_name": agent_name,
        "project_path": os.path.realpath(str(project)),
        "model": model,
        "provider": "test",
        "messages": messages,
    }))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _feed_card(title, ts, tool_name=None, tool_args=None, needs_approval=False):
    metadata = {}
    if tool_name:
        metadata["tool_name"] = tool_name
    if tool_args:
        metadata["tool_args"] = tool_args
    if needs_approval:
        metadata["needs_approval"] = True
    return {
        "card_type": "agent_action", "source": "agent", "title": title,
        "body": "body", "author": "Coder",
        "timestamp": datetime.fromtimestamp(ts).isoformat(),
        "project_name": "proj", "file_path": None, "commit_sha": None,
        "additions": None, "deletions": None, "task_id": None,
        "metadata": metadata, "card_id": "c1", "reviewed": False,
        "accepted": None, "seq_num": 1,
    }


def _write_feed(project, cards):
    path = Path(project) / ".crabcakes" / "feed.json"
    path.write_text(json.dumps(cards))
    return path


def _stat_line(pid, state, utime, stime, starttime=0, num_threads=1):
    """Build a /proc/<pid>/stat line with correct field offsets.

    After `)`, index 0 = field 3 (state), 11 = field 14 (utime),
    12 = field 15 (stime), 17 = field 20 (num_threads), 19 = field 22 (starttime).
    """
    rest = ["0", "1", "2", "3", "4", "5", "0", "0", "0", "0", "0",
            str(utime), str(stime), "0", "0", "20", "0", str(num_threads),
            "0", str(starttime), "1000000", "1000"]
    return f"{pid} (python3) {state} " + " ".join(rest) + "\n"


def _fake_app(tmp, project, pid=4242, state="S", wchan="poll",
              cmdline=b"python3\x00main.py\x00", exe="/usr/bin/python3.12"):
    """Fabricate a running app for `project` under the pinned PROC_ROOT.

    `exe` is the `/proc/<pid>/exe` target — the reporter requires a python
    interpreter there (audit fix round BUG #2).
    """
    root = Path(tmp) / "home" / "proc"
    root.mkdir(parents=True, exist_ok=True)
    (root / "stat").write_text("cpu 1 2 3 4\nbtime 1700000000\n")
    base = root / str(pid)
    (base / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
    (base / "cmdline").write_bytes(cmdline)
    os.symlink(os.path.realpath(str(project)), base / "cwd")
    if exe is not None:
        os.symlink(exe, base / "exe")
    (base / "stat").write_text(_stat_line(pid, state, 100, 0, starttime=1000))
    (base / "statm").write_text("45000 45000 0 0 0 0 0\n")
    (base / "wchan").write_text(wchan + "\n")
    return base


def _backdate(project, when):
    """Stamp every path under `project` with `when` (so nothing looks freshly written)."""
    for root, dirs, files in os.walk(project):
        for name in list(files) + list(dirs):
            os.utime(os.path.join(root, name), (when, when))


def _stalled_project(tmp_path, *, app=True):
    """The 2026-09-11 shape: assistant-final message, idle past the threshold,
    no repo writes after it, app alive."""
    project = _project(tmp_path)
    config = _config_dir(tmp_path)
    now = time.time()
    if app:
        _fake_app(tmp_path, project)
    _backdate(project, now - 7200)
    _write_conversation(config, "special:coder", project,
                        [_msg("user", "fix it", now - 4000),
                         _msg("assistant",
                              "Confirmed the bug. Writing the red tests first.",
                              now - 3600)],
                        mtime=now - 3600)
    return project, config


def _snapshot(root):
    seen = set()
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            seen.add(os.path.join(dirpath, name))
    return seen


def _no_report(*args, **kwargs):
    raise AssertionError("this argv path must short-circuit before collect()")


# ── §7 argv rows ─────────────────────────────────────────────────────────────

def test_argv_parse_nudge_forms(tmp_path, monkeypatch, capsys):
    """§7 row `test_argv_parse_nudge_forms`, realized for Phase 1b.

    The nudge FORMS themselves are Phase 2 and live on main.py (§3.1) — the
    shim has no nudge path. What Phase 1b must prove is that the reserved
    Phase-3 flag is parsed, refused, and never dispatched (exit 10, §2.2), and
    that a `--nudge` argv is rejected rather than silently accepted.
    """
    monkeypatch.setattr(status_report, "collect", _no_report)

    # (a) the reserved Phase-3 flag alone → exit 10, message on stderr
    assert cli.main(["--auto-resume"]) == cli.EXIT_NOT_IMPLEMENTED == 10
    err = capsys.readouterr().err
    assert "not implemented (Phase 3)" in err
    assert capsys.readouterr().out == ""

    # (b) combined with every implemented flag — the refusal still wins
    code = cli.main(["--auto-resume", "--json", "--full", "--no-feed",
                     "--project", str(tmp_path), "--watch", "5"])
    assert code == cli.EXIT_NOT_IMPLEMENTED
    assert "not implemented (Phase 3)" in capsys.readouterr().err

    # (c) `--nudge` is not a crab_status.py flag (it lives on main.py, §3.1)
    assert cli.main(["--nudge", "@Supervisor", "ping"]) == cli.EXIT_USAGE
    assert "usage:" in capsys.readouterr().err


def test_usage_error_exit_code_is_not_attention(monkeypatch, capsys):
    """A cron typo must not masquerade as "attention needed" (§2.2 = 2).

    Audit fix round BUG #6: usage errors exit 64, so the only codes the §2.4
    cron sees are 0/2/3/10 (plus 1 for an internal failure).
    """
    monkeypatch.setattr(status_report, "collect", _no_report)

    for argv in (["--bogus"], ["--project"], ["--watch", "abc"], ["--watch", "0"],
                 ["--json", "--bogus"], ["--watch", "-1"]):
        code = cli.main(argv)
        assert code == cli.EXIT_USAGE == 64, f"{argv} -> {code}"
        assert code not in (cli.EXIT_HEALTHY, cli.EXIT_ATTENTION,
                            cli.EXIT_APP_DOWN, cli.EXIT_NOT_IMPLEMENTED)
        assert "usage:" in capsys.readouterr().err


def test_help_is_not_reported_as_a_healthy_run(monkeypatch, capsys):
    """`--help` exits 0 (normal) with help text — but a `--json` consumer must
    not read that as a successful report, so the combination exits 64."""
    monkeypatch.setattr(status_report, "collect", _no_report)

    assert cli.main(["--help"]) == cli.EXIT_HEALTHY
    assert "usage:" in capsys.readouterr().out

    assert cli.main(["--json", "--help"]) == cli.EXIT_USAGE
    out = capsys.readouterr().out
    assert "usage:" in out                 # help text, not a report
    assert not out.lstrip().startswith("{")


def test_project_defaults_to_cwd(tmp_path, monkeypatch, capsys):
    seen = []

    def fake_collect(path, **kwargs):
        seen.append(path)
        return {"project_path": str(path), "generated_at": "t",
                "stalls": [], "alert": False}

    monkeypatch.setattr(status_report, "collect", fake_collect)
    monkeypatch.chdir(tmp_path)

    assert cli.main([]) == cli.EXIT_APP_DOWN  # no app in the fixture /proc
    assert seen == ["."]


# ── exit-code mapping (§2.2: 0 / 2 / 3) ──────────────────────────────────────

def test_exit_code_app_down_3_still_emits_report(tmp_path, capsys):
    project = _project(tmp_path)
    _config_dir(tmp_path)

    assert cli.main(["--project", str(project)]) == 3
    out = capsys.readouterr().out
    # invariant 2: the filesystem side is still reported with the app closed
    assert "SPEC-DEMO.md" in out
    assert "app_not_running" in out
    assert "EXIT        3" in out


def test_exit_code_healthy_0(tmp_path, capsys):
    project = _project(tmp_path)
    config = _config_dir(tmp_path)
    _fake_app(tmp_path, project)
    now = time.time()
    _write_conversation(config, "special:coder", project,
                        [_msg("assistant", "run the suite", now - 200),
                         _msg("tool", "1 passed", now - 100)],
                        mtime=now - 100)

    assert cli.main(["--project", str(project)]) == cli.EXIT_HEALTHY == 0
    out = capsys.readouterr().out
    assert "STALLS      none" in out
    assert "EXIT        0 (healthy" in out


def test_exit_code_frozen_app_2(tmp_path, capsys):
    """Audit BUG #7: a stopped app is attention-worthy, not healthy."""
    project = _project(tmp_path)
    _config_dir(tmp_path)
    _fake_app(tmp_path, project, state="T", wchan="do_signal_stop")

    assert cli.main(["--project", str(project)]) == cli.EXIT_ATTENTION == 2
    out = capsys.readouterr().out
    assert "app_frozen" in out
    assert "main=frozen" in out


def test_exit_code_console_script_launch_is_detected(tmp_path, capsys):
    """Audit BUG #1: the declared `crabcakes` entry point must be recognised."""
    project = _project(tmp_path)
    _config_dir(tmp_path)
    _fake_app(tmp_path, project,
              cmdline=b"/usr/bin/python3\x00/home/q/.local/bin/crabcakes\x00")

    assert cli.main(["--project", str(project)]) == cli.EXIT_HEALTHY == 0
    assert "APP         running pid=4242" in capsys.readouterr().out


def test_json_payload_carries_summary_and_exit_code(tmp_path, capsys):
    """Audit BUG #10: §2.4 delivery needs the summary as a field."""
    project = _project(tmp_path)
    _config_dir(tmp_path)

    assert cli.main(["--project", str(project), "--json"]) == cli.EXIT_APP_DOWN
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == cli.EXIT_APP_DOWN
    assert payload["summary"].startswith("app_not_running")
    assert payload["alert"] is False


def test_readonly_without_auto_resume_flag(tmp_path, monkeypatch, capsys):
    """§7 row: a stall is detected and reported, but nothing is mutated.

    Phase 1b has no action path at all: no resume, and no §2.4 alert
    bookkeeping (the `alert` field stays False and no state file appears —
    that wiring belongs to a later phase).
    """
    project, _config = _stalled_project(tmp_path)
    _write_feed(project, [_feed_card("Coder is reading utils/status_report.py",
                                     time.time() - 10)])

    def _boom(*args, **kwargs):
        raise AssertionError("Phase 1b must not run the §2.4 alert bookkeeping")

    monkeypatch.setattr(status_report, "should_alert", _boom)
    monkeypatch.setattr(status_report, "save_alert_state", _boom)

    before = _snapshot(tmp_path)
    assert cli.main(["--project", str(project), "--json"]) == cli.EXIT_ATTENTION
    captured = capsys.readouterr()
    after = _snapshot(tmp_path)

    payload = json.loads(captured.out)
    assert {s["class"] for s in payload["stalls"]} == {"turn_stalled"}
    assert "special:coder" in json.dumps(payload["stalls"])
    assert payload["alert"] is False  # no dedupe wiring in Phase 1b

    cache_root = str(status_report.cache_dir())
    new = {p for p in after - before if not p.startswith(cache_root + os.sep)}
    assert new == set(), f"CLI wrote outside its cache dir: {sorted(new)}"
    # …and it wrote no alert/auto-resume state anywhere
    assert not Path(status_report.state_path()).exists()
    assert not (project / ".crabcakes" / "status-state.json").exists()


# ── rendering: stdout contract ───────────────────────────────────────────────

def test_json_output_is_pure_and_machine_readable(tmp_path, capsys):
    project = _project(tmp_path)
    _config_dir(tmp_path)

    assert cli.main(["--project", str(project), "--json"]) == cli.EXIT_APP_DOWN
    out = capsys.readouterr().out
    assert out.startswith("{\n  ")  # render_json: indent=2
    payload = json.loads(out)       # nothing but JSON on stdout
    assert payload["project_path"] == os.path.realpath(str(project))
    assert payload["stalls"] == []
    assert payload["alert"] is False


def test_full_flag_raises_message_cap_and_never_leaks_tool_args(tmp_path, capsys):
    """--full raises message BODIES only; tool args/commands never render."""
    project = _project(tmp_path)
    config = _config_dir(tmp_path)
    command = f"cd /tmp && export TOKEN={SECRET} && rm -rf /"
    body = "M" * 1500 + "P" * 1000 + SECRET
    now = time.time()
    _write_conversation(config, "special:coder", project,
                        [_msg("assistant", body, now - 30,
                              tool_calls=[_tool_call("exec_command",
                                                     {"command": command})])],
                        mtime=now - 30)
    _write_feed(project, [_feed_card(f"Coder is running: {command}", now - 10,
                                     tool_name="exec_command",
                                     tool_args={"command": command})])

    assert cli.main(["--project", str(project), "--full"]) == cli.EXIT_APP_DOWN
    full_out = capsys.readouterr().out
    assert "M" * 1500 in full_out          # bodies raised to BODY_CAP_FULL
    assert "P" * 500 in full_out
    assert "P" * 501 not in full_out       # …and stops exactly at the cap
    assert SECRET not in full_out          # secret never rendered
    assert command not in full_out         # …at any verbosity
    assert "exec_command" in full_out      # the tool NAME is reported

    assert cli.main(["--project", str(project)]) == cli.EXIT_APP_DOWN
    default_out = capsys.readouterr().out
    assert "M" * 1500 not in default_out   # default cap is 120 chars
    assert SECRET not in default_out
    assert command not in default_out


def test_no_feed_skips_the_feed_section(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path)
    _config_dir(tmp_path)
    _write_feed(project, [_feed_card("Coder is reading x", time.time() - 10)])

    calls = []
    real = status_report._parse_feed_cards

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(status_report, "_parse_feed_cards", counting)

    assert cli.main(["--project", str(project), "--no-feed", "--json"]) == 3
    skipped = json.loads(capsys.readouterr().out)
    assert calls == []                     # --no-feed never parses the feed
    assert skipped["activity"]["cache"] == "skipped"
    assert skipped["activity"]["cards"] == []
    # ruling 2: the other sections still run
    assert skipped["work"]["spec"] == "SPEC-DEMO.md"
    assert skipped["agents"]["count"] == 0

    assert cli.main(["--project", str(project), "--json"]) == 3
    with_feed = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert with_feed["activity"]["cache"] == "miss"
    assert with_feed["activity"]["count"] == 1


# ── --watch ──────────────────────────────────────────────────────────────────

def test_watch_rerenders_each_interval_and_ctrl_c_exits_0(monkeypatch, capsys):
    calls, sleeps = [], []

    def fake_collect(project, **kwargs):
        calls.append(project)
        return {"project_path": str(project), "generated_at": "t",
                "stalls": [], "alert": False}

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(status_report, "collect", fake_collect)
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    assert cli.main(["--project", "stub", "--watch", "5"]) == cli.EXIT_HEALTHY
    out = capsys.readouterr().out
    assert out.count("crabcakes status —") == 2   # one report per iteration
    assert calls == ["stub", "stub"]              # re-collected, not re-rendered
    assert sleeps == [5.0, 5.0]                   # --watch N parsed as seconds


def test_report_failure_exits_1_loudly(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(status_report, "collect", boom)

    assert cli.main([]) == cli.EXIT_FAILURE == 1
    err = capsys.readouterr().err
    assert "collector exploded" in err
    assert "Traceback" in err           # §1.4: failure is loud, never a silent no-op


def test_watch_stops_on_report_failure(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("collector exploded")

    sleeps = []
    monkeypatch.setattr(status_report, "collect", boom)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert cli.main(["--watch", "1"]) == cli.EXIT_FAILURE
    assert "collector exploded" in capsys.readouterr().err
    assert sleeps == []                 # no silent retry loop after a hard failure


# ── headless / standalone (§2.3.1, §2.3.2) ───────────────────────────────────

def test_cli_imports_headless_without_gtk(tmp_path):
    """The shim imports in a clean process and pulls in no GTK, no ui/."""
    code = (
        "import importlib.util, sys\n"
        "print('GI_AVAILABLE', importlib.util.find_spec('gi') is not None)\n"
        f"spec = importlib.util.spec_from_file_location('cli', {str(CLI_PATH)!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "assert 'gi' not in sys.modules, sorted(m for m in sys.modules "
        "if m.startswith('gi'))\n"
        "assert not any(m == 'ui' or m.startswith('ui.') for m in sys.modules), "
        "'ui/ was imported'\n"
        "assert mod.NOT_IMPLEMENTED_MSG == 'not implemented (Phase 3)'\n"
        "print('OK')\n"
    )
    env = {"PYTHONDONTWRITEBYTECODE": "1",
           "HOME": str(tmp_path / "home"),
           "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, timeout=60, env=env, cwd=str(PROJECT_ROOT))
    assert proc.returncode == 0, f"headless import failed:\n{proc.stdout}\n{proc.stderr}"
    # GTK is installed on this box, so the no-GTK assertion above is not vacuous
    assert "GI_AVAILABLE True" in proc.stdout
    assert "OK" in proc.stdout


def test_script_runs_standalone_with_app_closed(tmp_path):
    """`python3 scripts/crab_status.py` works from an unrelated cwd, with no
    PYTHONPATH — the shim bootstraps the project root itself (§2.3.2).

    HOME stays real on purpose: the reporter imports GitPython (utils/git_ops)
    from the user site-packages, so a scrubbed HOME would only test a broken
    environment. Every crabcakes path is still redirected through XDG_* (config
    dir + cache dir), so nothing real is read or written.
    """
    project = _project(tmp_path)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": os.environ.get("PATH", ""),
        "HOME": REAL_HOME,
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
    }
    proc = subprocess.run(
        [sys.executable, str(CLI_PATH), "--json", "--project", str(project)],
        capture_output=True, text=True, timeout=60, env=env, cwd=str(tmp_path))

    assert proc.returncode == 3, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.startswith("{\n  ")
    payload = json.loads(proc.stdout)
    assert payload["project_path"] == os.path.realpath(str(project))
    assert "Traceback" not in proc.stderr
