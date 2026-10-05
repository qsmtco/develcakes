# tests/test_status_alert_wiring.py
# AGENTCTRL1 Phase 1b+ — the §2.4 alert-dedupe wiring in scripts/crab_status.py
# (SPEC-AGENT-CONTROL-1 §2.4, supervisor ruling on episode identity for all
# five stall classes).
#
# HERMETICITY (critical — two leak incidents this sprint): HOME, XDG_CACHE_HOME
# and XDG_CONFIG_HOME are redirected into tmp_path by a module-scoped autouse
# fixture, and the reporter's PROC_ROOT / CRASH_DIR point at fixture trees.
# Nothing here reads or writes the real ~/.cache/crabcakes or
# ~/.config/crabcakes. The CLI is driven in-process through main(argv).
#
# HEADLESS: no GTK in this path — runs without xvfb.

import importlib.util
import json
import os
import stat as statmod
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

import utils.status_report as status_report

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI_PATH = PROJECT_ROOT / "scripts" / "crab_status.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("crab_status_cli_wiring", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Module-scoped autouse: redirect every writable path into tmp_path.

    The real ~/.cache/crabcakes and ~/.config/crabcakes must be untouched by
    this suite (asserted externally via mtime/ls before+after the run).
    """
    home = tmp_path / "home"
    (home / "proc").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setattr(status_report, "PROC_ROOT", str(home / "proc"))
    monkeypatch.setattr(status_report, "CRASH_DIR", str(home / "crash"))
    return home


def _state_file():
    return Path(status_report.state_path())


def _project(tmp_path, name="proj"):
    project = tmp_path / name
    (project / "docs" / "specs").mkdir(parents=True)
    (project / ".crabcakes").mkdir(parents=True)
    (project / "docs" / "specs" / "SPEC-DEMO.md").write_text("# demo\n")
    (project / ".crabcakes" / "tasks.md").write_text(
        "# Work Units\n\n## 00000007 - Demo unit\n- **Status:** Done\n- **Priority:** High\n"
    )
    return project


def _backdate(root, when):
    """Stamp every path under root with `when` — a repo written AFTER the
    agent's last message means "not idle" and suppresses turn_stalled."""
    for dirpath, dirs, files in os.walk(root):
        for name in list(files) + list(dirs):
            os.utime(os.path.join(dirpath, name), (when, when))


def _config_dir(home):
    """The conversations dir lives under the app config dir (not the project).

    SPEC-11 SP1 (D4): get_config_dir() now resolves to develcakes — was
    crabcakes. Spec-drift update with disclosure.
    """
    d = home / ".config" / "develcakes"
    (d / "conversations").mkdir(parents=True, exist_ok=True)
    return d


def _write_conversation(config_dir, project, session_key, content, mtime):
    """A stale assistant-final conversation → turn_stalled at threshold."""
    conv_dir = Path(config_dir) / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    path = conv_dir / f"{session_key}.json"
    path.write_text(json.dumps({
        "session_key": session_key,
        "agent_name": "Coder",
        "project_path": os.path.realpath(str(project)),
        "model": "test/model",
        "provider": "test",
        "messages": [{
            "role": "assistant",
            "content": content,
            "tool_calls": [],
            "tool_call_id": "",
            "tokens_used": 1,
            "timestamp": datetime.fromtimestamp(mtime).isoformat(),
        }],
    }))
    os.utime(path, (mtime, mtime))
    return path


def _run_json(project):
    """Deprecated helper kept for symmetry — tests use _run_json_captured.

    stdout capture belongs to pytest's capsys, not a StringIO (the reporter
    writes via sys.stdout, which capsys replaces).
    """
    code = cli.main(["--project", str(project), "--json"])
    return code, None


def _run_json_captured(project, capsys):
    code = cli.main(["--project", str(project), "--json"])
    out = capsys.readouterr().out
    try:
        return code, json.loads(out)
    except (ValueError, TypeError):
        return code, None


def _fake_app(tmp_path, project, pid=4242, state="S", wchan="poll",
              cmdline=b"python3\x00main.py\x00", exe="/usr/bin/python3.12",
              starttime=1000):
    """Fabricate a running app for `project` under the pinned PROC_ROOT.

    Mirrors tests/test_crab_status_cli.py::_fake_app (not importable —
    separate test namespaces). `state` is the /proc stat state char; the
    reporter requires a python interpreter at /proc/<pid>/exe.
    """
    root = Path(tmp_path) / "home" / "proc"
    root.mkdir(parents=True, exist_ok=True)
    (root / "stat").write_text("cpu 1 2 3 4\nbtime 1700000000\n")
    base = root / str(pid)
    (base / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
    (base / "cmdline").write_bytes(cmdline)
    os.symlink(os.path.realpath(str(project)), base / "cwd")
    if exe is not None:
        os.symlink(exe, base / "exe")
    rest = ["0", "1", "2", "3", "4", "5", "0", "0", "0", "0", "0",
            "100", "0", "0", "0", "20", "0", "1", "0", str(starttime),
            "1000000", "1000"]
    (base / "stat").write_text(f"{pid} (main) {state} " + " ".join(rest))
    (base / "statm").write_text("45000 45000 0 0 0 0 0\n")
    (base / "wchan").write_text(wchan + "\n")
    return base


# ── 1. healthy run: alert False, NO state file ───────────────────────────────


def test_healthy_run_sets_alert_false_and_writes_no_state(tmp_path, capsys):
    project = _project(tmp_path)
    _fake_app(tmp_path, project)  # app running, no stalls → exit 0 path
    code, report = _run_json_captured(project, capsys)
    assert code == 0, f"healthy project must exit 0; got {code}"
    assert report is not None
    assert report["alert"] is False
    assert not _state_file().exists(), (
        "healthy run must not write the alert state file (silence is default)"
    )


# ── 2. stall run: alert True, state persisted 0600 ───────────────────────────


def test_stall_run_sets_alert_true_and_persists_state(tmp_path, capsys):
    project = _project(tmp_path)
    _fake_app(tmp_path, project)  # app must be up: exit 3 outranks stalls
    config = _config_dir(tmp_path / "home")
    now = time.time()
    _backdate(project, now - 7200)  # no repo writes after the message
    _write_conversation(config, project, "special:coder", "done", mtime=now - 3600)
    code, report = _run_json_captured(project, capsys)
    assert code == 2, "a 1h-stale assistant turn must be a stall (exit 2)"
    assert report["alert"] is True, "first sighting of an episode must alert"
    state_file = _state_file()
    assert state_file.exists(), "alerted run must persist the dedupe state"
    mode = statmod.S_IMODE(state_file.stat().st_mode)
    assert mode == 0o600, f"state file must be 0600, got {oct(mode)}"
    payload = json.loads(state_file.read_text())
    assert payload.get("episodes"), "state must record the alerted episode"


# ── 3. same episode within the floor is deduped ──────────────────────────────


def test_same_episode_within_floor_is_deduped(tmp_path, capsys):
    project = _project(tmp_path)
    _fake_app(tmp_path, project)
    config = _config_dir(tmp_path / "home")
    now = time.time()
    _backdate(project, now - 7200)
    _write_conversation(config, project, "special:coder", "done", mtime=now - 3600)
    code1, report1 = _run_json_captured(project, capsys)
    assert report1["alert"] is True
    code2, report2 = _run_json_captured(project, capsys)
    assert code2 == 2, "exit code still reports the stall class"
    assert report2["alert"] is False, (
        "second sighting within the re-alert floor must be deduped — "
        "this is the whole point (no 96-alerts/day nuisance)"
    )


# ── 4. new episode alerts again ──────────────────────────────────────────────


def test_new_episode_alerts_again(tmp_path, capsys):
    project = _project(tmp_path)
    _fake_app(tmp_path, project)
    config = _config_dir(tmp_path / "home")
    now = time.time()
    _backdate(project, now - 7200)
    _write_conversation(config, project, "special:coder", "done v1", mtime=now - 3600)
    _, report1 = _run_json_captured(project, capsys)
    assert report1["alert"] is True
    _, report2 = _run_json_captured(project, capsys)
    assert report2["alert"] is False
    # New agent activity → new episode id → must alert again.
    _write_conversation(config, project, "special:coder",
                        "done v2 — new activity", mtime=now - 3500)
    _write_conversation(config, project, "special:coder", "done v2 — new activity",
                        mtime=now - 3500)
    _, report3 = _run_json_captured(project, capsys)
    assert report3["alert"] is True, (
        "a genuinely new episode must not be suppressed by the old one"
    )


# ── 5. re-alert after the floor elapses ─────────────────────────────────────


def test_re_alert_after_floor_elapses(tmp_path, capsys, monkeypatch):
    project = _project(tmp_path)
    _fake_app(tmp_path, project)
    config = _config_dir(tmp_path / "home")
    base = 1_000_000.0
    clock = {"now": base}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    _backdate(project, base - 7200)
    _write_conversation(config, project, "special:coder", "stalled work",
                        mtime=base - 3600)
    _, report1 = _run_json_captured(project, capsys)
    assert report1["alert"] is True, "first sighting alerts"

    clock["now"] = base + 60.0  # still inside the 1h floor
    _, report2 = _run_json_captured(project, capsys)
    assert report2["alert"] is False, "inside the floor: deduped"

    clock["now"] = base + 2 * 3600.0  # floor (1h) long elapsed
    _, report3 = _run_json_captured(project, capsys)
    assert report3["alert"] is True, "after the re-alert floor: alert again"


# ── 6. episode ids: stable across invocations, change on new evidence ────────


def test_episode_id_stable_across_invocations_and_changes_on_new_activity():
    now = 1_700_000_000.0
    turn = {"class": "turn_stalled", "session": "special:coder",
            "last_message_ts": now - 3600, "last_message_sha": "abc123",
            "mtime": now - 3600}
    sendback = {"class": "blocked_on_sendback",
                "sendback_file": "docs/specs/X-SENDBACK.md",
                "sendback_mtime": now - 60}
    frozen = {"class": "app_frozen", "app_pid": 4242, "app_starttime": 12345.5}
    spinning = {"class": "app_spinning", "app_pid": 4242, "app_starttime": 12345.5}
    crash = {"class": "crash_after_start",
             "crash_file": "/var/crash/foo.1000.crash"}
    approvals = {"class": "approvals_pending", "project_path": "/home/q/proj"}

    for stall in (turn, sendback, frozen, spinning, crash, approvals):
        first = status_report.stall_episode_id(stall)
        second = status_report.stall_episode_id(stall)
        assert first == second, f"{stall['class']} id must be stable"
        assert isinstance(first, str) and len(first) == 16

    # Same posture, same process → same id (stability while unchanged)
    assert status_report.stall_episode_id(frozen) == \
        status_report.stall_episode_id(dict(frozen))

    # New underlying activity → id MUST change
    moved = dict(turn, last_message_ts=now - 60, last_message_sha="def456")
    assert status_report.stall_episode_id(moved) != status_report.stall_episode_id(turn)
    new_sendback = dict(sendback, sendback_mtime=now - 10)
    assert status_report.stall_episode_id(new_sendback) != \
        status_report.stall_episode_id(sendback)
    restarted = dict(frozen, app_pid=9999, app_starttime=777.0)
    assert status_report.stall_episode_id(restarted) != \
        status_report.stall_episode_id(frozen)
    new_crash = dict(crash, crash_file="/var/crash/bar.1000.crash")
    assert status_report.stall_episode_id(new_crash) != \
        status_report.stall_episode_id(crash)
    # Same process, same posture → same id even when NON-identity fields drift
    # (detail text, cpu numbers — render concerns, not episode evidence).
    assert status_report.stall_episode_id(
        dict(frozen, detail="different wording", cpu_percent=99)) == \
        status_report.stall_episode_id(frozen)


# ── 7. tolerant input: never raise, class-only fallback ──────────────────────


def test_stall_episode_id_tolerates_missing_keys():
    # Missing everything, wrong types, partial evidence — must not raise.
    empty = status_report.stall_episode_id({})
    assert len(empty) == 16
    assert status_report.stall_episode_id({}) == empty  # stable fallback
    assert status_report.stall_episode_id(None) == status_report.stall_episode_id(None)
    assert status_report.stall_episode_id("not-a-dict") == \
        status_report.stall_episode_id("not-a-dict")

    turn_bare = status_report.stall_episode_id({"class": "turn_stalled"})
    turn_full = status_report.stall_episode_id(
        {"class": "turn_stalled", "session": "special:coder",
         "last_message_ts": 1.0, "last_message_sha": "x"})
    assert turn_bare != turn_full, "class-only fallback differs from full id"

    # Different classes must not collide on the fallback
    classes = {status_report.stall_episode_id({"class": c})
               for c in ("turn_stalled", "blocked_on_sendback", "app_frozen",
                         "app_spinning", "crash_after_start", "approvals_pending")}
    assert len(classes) == 6, "class-only fallbacks must be distinct per class"


# ── 8. the Phase-3 auto-resume stub is untouched ─────────────────────────────


def test_auto_resume_stub_unchanged(tmp_path, capsys):
    project = _project(tmp_path)
    code = cli.main(["--project", str(project), "--auto-resume", "--json"])
    captured = capsys.readouterr()
    assert code == 10, "reserved flag must keep exiting 10"
    assert "not implemented" in (captured.err or "").lower(), (
        "stub must keep its stderr message"
    )
    assert not _state_file().exists(), "the stub must not write alert state"


# ── 9. usage errors (64) can never alert ─────────────────────────────────────


def test_usage_error_exit_64_never_alerts(tmp_path, capsys):
    project = _project(tmp_path)
    code = cli.main(["--project", str(project), "--bogus-flag", "--json"])
    assert code == 64, "argv typo must exit 64 (BUG #6 follow-through)"
    captured = capsys.readouterr()
    # No report was printed, and no state was written: the dedupe path was
    # never reached, so nothing can have alerted.
    assert not (captured.out or "").strip().startswith("{")
    assert not _state_file().exists()
