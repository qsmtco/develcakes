"""Tests for the timer tool (SPEC-52).

Three layers:
  * `utils/timer_store.py` — pure store (atomic writes, validation, corrupt
    input tolerance) — no GTK.
  * the public tool path — `execute_tool("set_timer"/"cancel_timer", ...)`
    → `agent.group_tools.set_timer/cancel_timer` → the installed callback,
    covering the validation matrix and the forwarding contract;
  * the window-side scheduler — `MainWindow._schedule_timer` / `_fire` /
    `_boot_sweep_timers` driven unbound against a stub window with the
    conftest `fake_glib` fixture patched in for `ui.window.GLib` (armed
    timeouts are fired by calling the recorded callback by hand).

This module opts out of the conftest GTK headless guard with the top-level
``GTK_HEADLESS_SAFE = True`` marker (replacing the old 'invisible to the AST
guard' trick — dynamic ui imports are now detected too).
"""

GTK_HEADLESS_SAFE = True

import threading
from datetime import datetime, timedelta

import pytest

from agent import group_tools
from agent.tools import ToolResult, execute_tool, get_all_tools, set_timer_callback
from utils.timer_store import (
    STALE_GRACE,
    add_timer,
    load_timers,
    remove_timer,
    save_timers,
)


def _main_window():
    """Return the MainWindow class via a dynamic import.

    The conftest GTK guard (pytest_collection_modifyitems) walks the AST of
    every collected module and skips any file that can reach widget
    construction without a display — including dynamic ui imports, which the
    static scan used to miss. This module opts out by declaring
    ``GTK_HEADLESS_SAFE = True`` at top level: the tests only call MainWindow
    methods unbound on a stub and construct no widgets, so the suite runs
    headless."""
    import importlib
    return importlib.import_module("ui.window").MainWindow


MainWindow = _main_window()


@pytest.fixture(autouse=True)
def _clear_timer_cb():
    """Group-level callback state is global — reset around every test."""
    group_tools._timer_cb = None
    yield
    group_tools._timer_cb = None


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return str(p)


class _ProjectHandler:
    def __init__(self, path):
        self._path = path
        self._name = "P"

    def get_active_project_path(self):
        return self._path

    def get_active_project_name(self):
        return self._name

    def set_on_project_opened(self, cb):
        self._opened_cb = cb


class _AgentRuntimeHandler:
    def __init__(self):
        self.delivered = []

    def send_to_special_agent(self, session_key, text):
        self.delivered.append((session_key, text))

    def _reply_key(self, session_key):
        return "project:P"


class _FeedHandler:
    def __init__(self):
        self.cards = []

    def add_card(self, card):
        self.cards.append(card)
        return f"card-{len(self.cards)}"


class _StubWindow:
    def __init__(self, path):
        self._project_handler = _ProjectHandler(path)
        self._agent_runtime_handler = _AgentRuntimeHandler()
        self._feed_handler = _FeedHandler()
        self._timer_sources = {}
        self._timer_lock = threading.Lock()
        self._bind_scheduler()

    def _post_feed_card(self, session_key, fields):
        return ToolResult(success=True, output="posted")

    def _bind_scheduler(self):
        """Bind the real MainWindow scheduler methods to this stub.

        The scheduler methods under test live on MainWindow; unbound calls
        like ``MainWindow._boot_sweep_timers(stub, ...)`` dispatch through
        these, so the stub exercises the exact production code path against
        the fake GLib.
        """
        self._arm_timer = MainWindow._arm_timer.__get__(self)
        self._fire = MainWindow._fire.__get__(self)
        self._deliver_timer = MainWindow._deliver_timer.__get__(self)
        self._post_expired_card = MainWindow._post_expired_card.__get__(self)


# ── Store (utils/timer_store.py) ─────────────────────────────────────────────


def test_store_roundtrip(project):
    timers = [{"id": "ab12cd34", "target": "agent:coder", "prompt": "p",
               "fires_at": "2099-01-01T00:00:00", "repeat": ""}]
    assert save_timers(project, timers) is True
    assert load_timers(project) == timers


def test_store_missing_and_corrupt(project):
    assert load_timers(project) == []  # missing file → []
    path = project + "/.crabcakes/timers.json"
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("{ not json !!")
    assert load_timers(project) == []  # corrupt → []
    with open(path, "w") as fh:
        fh.write('{"not": "a list"}')
    assert load_timers(project) == []  # non-list → []


def test_store_add_timer_validation(project):
    fires = (datetime.now() + timedelta(minutes=5)).isoformat()
    assert add_timer(project, target="coder", prompt="p", fires_at=fires) is None
    assert add_timer(project, target="agent:coder", prompt="   ", fires_at=fires) is None
    assert add_timer(project, target="agent:coder", prompt="p" * 2001, fires_at=fires) is None
    assert add_timer(project, target="agent:coder", prompt="p", fires_at="not-a-date") is None
    assert add_timer(project, target="agent:coder", prompt="p", fires_at=fires, repeat="weekly") is None
    timer = add_timer(project, target="agent:coder", prompt="ping", fires_at=fires)
    assert timer is not None
    assert timer["id"] and len(timer["id"]) == 8
    assert timer["target"] == "agent:coder"
    assert timer["prompt"] == "ping"
    assert load_timers(project) == [timer]
    assert remove_timer(project, timer["id"]) is True
    assert load_timers(project) == []
    assert remove_timer(project, timer["id"]) is False  # unknown id


# ── Tool registration ────────────────────────────────────────────────────────


def test_timer_tools_registered():
    names = {d.name for d in get_all_tools()}
    assert "set_timer" in names
    assert "cancel_timer" in names
    by_name = {d.name: d for d in get_all_tools()}
    assert by_name["set_timer"].requires_approval is False
    assert by_name["cancel_timer"].requires_approval is False
    assert "delay" in by_name["set_timer"].description.lower()
    assert "repeat" in by_name["set_timer"].description.lower()


def test_delay_parser():
    now = datetime.now()
    ts, err = group_tools._compute_fires_at("90s")
    assert err is None
    assert abs((datetime.fromisoformat(ts) - now).total_seconds() - 90) < 2
    ts, err = group_tools._compute_fires_at("1h30m")
    assert err is None
    assert abs((datetime.fromisoformat(ts) - now).total_seconds() - 5400) < 2
    ts, err = group_tools._compute_fires_at("at 12:30")
    assert err is None
    assert datetime.fromisoformat(ts).hour == 12
    ts, err = group_tools._compute_fires_at("banana")
    assert err is not None
    ts, err = group_tools._compute_fires_at("")
    assert err is not None


# ── Tool-layer validation matrix ─────────────────────────────────────────────


def test_set_timer_validation_errors(project):
    r = execute_tool("set_timer", {"delay": "90s", "prompt": "ping"},
                     project_path="", session_key="agent:coder")
    assert not r.success and r.error == "no project open"

    r = execute_tool("set_timer", {"delay": "banana", "prompt": "ping"},
                     project_path=project, session_key="agent:coder")
    assert not r.success and "delay must be like" in r.error

    r = execute_tool("set_timer", {"delay": "90s", "prompt": "   "},
                     project_path=project, session_key="agent:coder")
    assert not r.success and "prompt must be" in r.error

    r = execute_tool("set_timer", {"delay": "90s", "prompt": "x" * 2001},
                     project_path=project, session_key="agent:coder")
    assert not r.success and "prompt exceeds 2000" in r.error

    r = execute_tool("set_timer",
                     {"delay": "90s", "prompt": "ping", "repeat": "weekly"},
                     project_path=project, session_key="agent:coder")
    assert not r.success and "repeat must be" in r.error

    r = execute_tool("set_timer",
                     {"delay": "90s", "prompt": "ping", "agent": "NoSuchAgent"},
                     project_path=project, session_key="agent:coder")
    assert not r.success and "no agent named" in r.error

    # not wired — everything else valid
    r = execute_tool("set_timer", {"delay": "90s", "prompt": "ping"},
                     project_path=project, session_key="agent:coder")
    assert not r.success and r.error == "set_timer is not wired"


def test_set_timer_callback_exception(project):
    def _boom(*args):
        raise ValueError("nope")
    set_timer_callback(_boom)
    r = execute_tool("set_timer", {"delay": "90s", "prompt": "ping"},
                     project_path=project, session_key="agent:coder")
    assert not r.success and r.error == "set_timer failed: ValueError"


def test_cancel_timer_validation_errors(project):
    r = execute_tool("cancel_timer", {"timer_id": ""},
                     project_path=project, session_key="agent:coder")
    assert not r.success and r.error == "cancel_timer needs a timer id"

    r = execute_tool("cancel_timer", {"timer_id": "ab12cd34"},
                     project_path=project, session_key="agent:coder")
    assert not r.success and r.error == "cancel_timer is not wired"

    r = execute_tool("cancel_timer", {"timer_id": "ab12cd34"},
                     project_path="", session_key="agent:coder")
    assert not r.success and r.error == "no project open"


# ── Window-side scheduler (with fake_glib) ───────────────────────────────────


def _patch_glib(monkeypatch, fake_glib):
    monkeypatch.setattr("ui.window.GLib", fake_glib)


def test_schedule_arm_fire_delivery(monkeypatch, fake_glib, project):
    _patch_glib(monkeypatch, fake_glib)
    stub = _StubWindow(project)
    set_timer_callback(MainWindow._schedule_timer.__get__(stub))

    r = execute_tool("set_timer", {"delay": "90s", "prompt": "ping"},
                     project_path=project, session_key="agent:coder")
    assert r.success, r.error

    assert len(fake_glib.armed) == 1
    source_id, delay_ms, cb = fake_glib.armed[0]
    assert abs(delay_ms - 90000) < 2000  # scheduling drift between arm and assert

    timers = load_timers(project)
    assert len(timers) == 1
    timer = timers[0]
    assert timer["target"] == "agent:coder"
    assert timer["prompt"] == "ping"
    assert timer["repeat"] == ""

    # Fire the recorded callback — the source (one-shot) delivers and is removed.
    cb()
    assert stub._agent_runtime_handler.delivered == [
        ("agent:coder", f"[timer {timer['id']}]: ping"),
    ]
    assert load_timers(project) == []
    assert timer["id"] not in stub._timer_sources


def test_cancel_timer_removes_source_and_store(monkeypatch, fake_glib, project):
    _patch_glib(monkeypatch, fake_glib)
    stub = _StubWindow(project)
    set_timer_callback(MainWindow._schedule_timer.__get__(stub))

    r = execute_tool("set_timer", {"delay": "45m", "prompt": "ping"},
                     project_path=project, session_key="agent:coder")
    assert r.success, r.error
    timers = load_timers(project)
    assert len(timers) == 1
    timer_id = timers[0]["id"]
    source_id = fake_glib.armed[0][0]
    assert source_id in fake_glib.armed_ids

    r = execute_tool("cancel_timer", {"timer_id": timer_id},
                     project_path=project, session_key="agent:coder")
    assert r.success, r.error
    assert source_id not in fake_glib.armed_ids
    assert load_timers(project) == []


def test_daily_timer_advances_and_rearms(monkeypatch, fake_glib, project):
    _patch_glib(monkeypatch, fake_glib)
    stub = _StubWindow(project)
    fires_at = (datetime.now() + timedelta(minutes=30)).isoformat()
    timer = add_timer(project, target="agent:coder", prompt="daily ping",
                      fires_at=fires_at, repeat="daily")
    assert timer is not None

    MainWindow._arm_timer(stub, timer)
    assert len(fake_glib.armed) == 1
    first_cb = fake_glib.armed[0][2]

    first_cb()
    assert len(stub._agent_runtime_handler.delivered) == 1
    timers = load_timers(project)
    assert len(timers) == 1
    day1 = datetime.fromisoformat(timers[0]["fires_at"])
    assert day1 == datetime.fromisoformat(fires_at) + timedelta(days=1)
    assert len(fake_glib.armed) == 2  # re-armed a fresh source
    second_cb = fake_glib.armed[1][2]

    second_cb()
    assert len(stub._agent_runtime_handler.delivered) == 2
    timers = load_timers(project)
    day2 = datetime.fromisoformat(timers[0]["fires_at"])
    assert day2 == datetime.fromisoformat(fires_at) + timedelta(days=2)
    assert len(fake_glib.armed) == 3


def test_boot_sweep_stale_one_shot_posts_card(monkeypatch, fake_glib, project):
    _patch_glib(monkeypatch, fake_glib)
    stub = _StubWindow(project)
    stale_fires = (datetime.now() - timedelta(seconds=STALE_GRACE + 60)).isoformat()
    timer = add_timer(project, target="agent:coder", prompt="stale",
                      fires_at=stale_fires)
    assert timer is not None

    MainWindow._boot_sweep_timers(stub, "P", project)
    assert len(stub._feed_handler.cards) == 1
    card = stub._feed_handler.cards[0]
    assert card.card_type == "agent_action"
    assert card.title == f"Timer {timer['id']} expired offline"
    assert load_timers(project) == []  # consumed, not re-armed


def test_boot_sweep_future_one_shot_arms(monkeypatch, fake_glib, project):
    _patch_glib(monkeypatch, fake_glib)
    stub = _StubWindow(project)
    future_fires = (datetime.now() + timedelta(hours=1)).isoformat()
    timer = add_timer(project, target="agent:coder", prompt="future",
                      fires_at=future_fires)
    assert timer is not None

    MainWindow._boot_sweep_timers(stub, "P", project)
    assert stub._feed_handler.cards == []
    assert len(fake_glib.armed) == 1
    assert fake_glib.armed[0][0] in fake_glib.armed_ids
    assert len(load_timers(project)) == 1


def test_boot_sweep_past_daily_rearms_for_next_day(monkeypatch, fake_glib, project):
    _patch_glib(monkeypatch, fake_glib)
    stub = _StubWindow(project)
    past_fires = (datetime.now() - timedelta(hours=3)).isoformat()
    timer = add_timer(project, target="agent:coder", prompt="daily past",
                      fires_at=past_fires, repeat="daily")
    assert timer is not None

    MainWindow._boot_sweep_timers(stub, "P", project)
    assert stub._feed_handler.cards == []
    assert len(fake_glib.armed) == 1
    timers = load_timers(project)
    assert len(timers) == 1
    assert datetime.fromisoformat(timers[0]["fires_at"]) > datetime.now()


# ── GTK-freedom ──────────────────────────────────────────────────────────────


def test_no_gtk_import_in_agent_layer():
    for path in ("utils/timer_store.py", "agent/group_tools.py"):
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        assert "gi.repository" not in content, f"{path} imports gi.repository"
