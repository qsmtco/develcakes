# tests/test_cli_nudge.py
# AGENTCTRL1 Phase 2 — --nudge Supervisor-only command-line channel.
#
# Contract: docs/specs/SPEC-AGENT-CONTROL-1.md §3 + §11 PM decisions.
# All tests run IN-PROCESS: GTK second-instance forwarding is stubbed with a
# fake command_line object exposing get_arguments()/set_exit_status(); no
# second process is spawned. GTK suites run under xvfb.

import json
import os
import sys
from types import SimpleNamespace

import gi
gi.require_version('Gtk', '4.0')

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import CrabcakesApp, handle_cli_args


# ── Fakes for the window → AgentRuntimeHandler graph ─────────────────────────


class FakeRuntime:
    """Minimal AgentRuntime stand-in: only get_turn_state is consumed."""

    def __init__(self, state=None):
        self._state = state

    def get_turn_state(self, session_key):
        return self._state


class FakeFeedHandler:
    def __init__(self):
        self.cards = []

    def add_card(self, card):
        self.cards.append(card)
        return "card-1234"


class FakeHandler:
    """Duck-typed AgentRuntimeHandler exposing ONLY the public nudge surface."""

    def __init__(self, turn_state=None, session_exists=True,
                 active_project=("proj", "/tmp/proj"), feed_handler=None):
        self._runtimes = {"Supervisor": FakeRuntime(turn_state)} if turn_state is not None or True else {}
        self._active_project = active_project
        self._fh = feed_handler if feed_handler is not None else FakeFeedHandler()
        self._session_exists = session_exists
        self.sent = []
        self.published = []

    # Public nudge surface (mirrors the real handler's Phase 2 methods)
    def special_agent_session_exists(self, session_key):
        return self._session_exists

    def special_agent_turn_state(self, session_key):
        rt = self._runtimes.get("Supervisor")
        return rt.get_turn_state(session_key) if rt is not None else None

    def publish_cli_nudge_card(self, session_key, text):
        self.published.append((session_key, text))
        return self._fh.add_card(SimpleNamespace())

    def send_to_special_agent(self, session_key, text):
        self.sent.append((session_key, text))


class FakeWindow:
    def __init__(self, handler):
        self._agent_runtime_handler = handler


def _raise_dispatch_error(session_key, text):
    """Stand-in for a dispatch that fails AFTER the guardrails passed."""
    raise RuntimeError("simulated handler failure mid-dispatch")


class FakeApp:
    """Stand-in for CrabcakesApp: what handle_cli_args may touch.

    TRIPWIRE: ``get_is_remote`` defaults to False — the real value on the
    PRIMARY instance, which is the one that HANDLES a forwarded nudge. If the
    implementation wrongly gates delivery on get_is_remote() (the spec's
    literal wording), every delivery test here fails. The real "app running"
    discriminator is the _main_window/handler graph.
    """

    def __init__(self, handler, is_remote=False):
        self._main_window = FakeWindow(handler)
        self._is_remote = is_remote
        self.activate_calls = 0

    def get_is_remote(self):
        return self._is_remote

    def activate(self):
        self.activate_calls += 1


class StubCommandLine:
    """In-process stand-in for Gio.ApplicationCommandLine."""

    def __init__(self, args):
        self._args = ["crabcakes"] + list(args)
        self.exit_status = None

    def get_arguments(self):
        return list(self._args)

    def set_exit_status(self, code):
        self.exit_status = code


def make_app(**handler_kwargs):
    handler = FakeHandler(**handler_kwargs)
    return FakeApp(handler), handler


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    """HERMETICITY: every test in this module delivers nudges that call
    _write_nudge_audit_record → utils.config.get_config_dir. Without this
    fixture the delivering tests append cli-nudge records to the REAL
    ~/.config/crabcakes/audit-log.jsonl (Unit C leak class — happened and was
    purged before this fixture was added). Function-scoped autouse; the
    audit-record test's own monkeypatch overrides this one within that test.
    """
    monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path / "cfg"))


# ── 1. argv forms: quoting, missing agent, oversized payload ────────────────


def test_argv_parse_nudge_forms():
    # Quoting: a multi-word quoted payload arrives as ONE argument and parses.
    app, handler = make_app()
    code, msg = handle_cli_args(app, ["--nudge", "@Supervisor", "hello there world"])
    assert code == 0, f"multi-word payload must parse as a single text arg; got {code}: {msg}"
    assert handler.sent == [("special:supervisor", "hello there world")]

    # Missing agent → exit 3, no dispatch
    app, handler = make_app()
    code, msg = handle_cli_args(app, ["--nudge"])
    assert code == 3
    assert handler.sent == []

    # Missing text → exit 3 (malformed form), no dispatch
    app, handler = make_app()
    code, msg = handle_cli_args(app, ["--nudge", "@Supervisor"])
    assert code == 3
    assert handler.sent == []

    # Oversized payload (>4096 chars) → exit 5, no dispatch
    app, handler = make_app()
    code, msg = handle_cli_args(app, ["--nudge", "@Supervisor", "x" * 4097])
    assert code == 5
    assert handler.sent == []

    # Boundary: exactly 4096 chars is accepted
    app, handler = make_app()
    code, _ = handle_cli_args(app, ["--nudge", "@Supervisor", "x" * 4096])
    assert code == 0


# ── 2. Reach: Supervisor only ────────────────────────────────────────────────


def test_nudge_refused_for_non_supervisor_agent():
    for target in ("@Coder", "@Debugger", "@Tester", "@unknown-agent"):
        app, handler = make_app()
        code, msg = handle_cli_args(app, ["--nudge", target, "do things"])
        assert code == 3, f"{target} must be refused with exit 3"
        assert "Supervisor" in (msg or ""), "refusal must name the permitted target"
        assert handler.sent == [], f"{target} must not dispatch"
        assert handler.published == [], f"{target} must not publish a card"

    # Case-insensitive Supervisor IS permitted (PM reach decision)
    app, handler = make_app()
    code, _ = handle_cli_args(app, ["--nudge", "@supervisor", "ping"])
    assert code == 0
    assert handler.sent == [("special:supervisor", "ping")]


# ── 3. Turn in flight → exit 2, no dispatch ──────────────────────────────────


def test_nudge_refused_when_turn_in_flight():
    for state in ("running", "streaming"):
        app, handler = make_app(turn_state=state)
        code, msg = handle_cli_args(app, ["--nudge", "@Supervisor", "ping"])
        assert code == 2, f"state {state} must refuse with exit 2"
        assert msg == "turn in flight for Supervisor; retry when idle", (
            f"exact refusal message required; got {msg!r}"
        )
        assert handler.sent == [], "busy turn must not dispatch"
        assert handler.published == [], "busy turn must not publish a card"


# ── 4. Feed card + audit record; audit carries hash, never the text ─────────


def test_nudge_logs_feed_card_and_audit_record(tmp_path, monkeypatch):
    monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
    app, handler = make_app()
    code, _ = handle_cli_args(app, ["--nudge", "@Supervisor", "secret payload text"])
    assert code == 0

    # Feed card artefact
    assert len(handler.published) == 1, "nudge must publish exactly one feed card"

    # Audit record artefact
    audit_path = tmp_path / "audit-log.jsonl"
    assert audit_path.exists(), "nudge must append an audit-log.jsonl record"
    record = json.loads(audit_path.read_text().strip().splitlines()[-1])
    assert record["origin"] == "cli-nudge"
    assert record["target"] == "special:supervisor"
    assert record["chars"] == len("secret payload text")
    assert len(record["text_sha256"]) == 16
    # Content policy §11.3: the raw text must NEVER reach the audit record
    assert "secret payload text" not in audit_path.read_text()
    # Timestamp present
    assert "timestamp" in record


# ── 5. The card is visibly marked as CLI origin; a typed one is not ─────────


def test_nudge_card_is_marked_as_cli_origin():
    """Exercise the REAL publisher on a REAL AgentRuntimeHandler + FeedCardData.

    Steel-framed Rule 4: do not test the mock. The handler under test here is
    publish_cli_nudge_card itself, so the fake-handler indirection used by the
    other tests would pass even if the real publisher were broken.
    """
    from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
    from models.feed_card import FeedCardData

    captured = []

    class CapturingFeedHandler:
        def add_card(self, card):
            captured.append(card)
            return "card-1"

    rt_handler = AgentRuntimeHandler(main_content=None, chat_render_handler=None)
    rt_handler.set_feed_handler(CapturingFeedHandler())

    card_id = rt_handler.publish_cli_nudge_card("special:supervisor", "visible marker check")
    assert card_id == "card-1"
    assert len(captured) == 1
    card = captured[0]
    assert isinstance(card, FeedCardData)
    assert card.metadata.get("origin") == "cli-nudge", (
        "card metadata must carry origin='cli-nudge'"
    )
    assert "[via CLI]" in card.body, (
        "card body must carry the visible 'via CLI' origin marker"
    )
    assert "visible marker check" in card.body, "card must show the delivered text"

    # Contrast: a typed message's card does NOT carry the CLI origin.
    typed_card = FeedCardData(
        card_type="agent_action", source="agent", title="t", body="typed",
        author="PM", timestamp=card.timestamp, project_name="p",
    )
    assert "origin" not in typed_card.metadata, (
        "a PM-typed card must not carry the CLI origin marker"
    )


# ── 6. Not remote → exit 4, never present a GUI ──────────────────────────────


def test_nudge_does_not_start_gui_when_not_remote():
    """No running instance → exit 4, never activate/present a GUI.

    Models the real no-app case: `main.py --nudge ...` with no instance
    running becomes a fresh, windowless PRIMARY (never activated), so
    `_main_window`/handler graph is missing. get_is_remote() stays False —
    the spec-literal gate would have refused every legitimate nudge.
    """
    app, handler = make_app()
    app._main_window = None  # windowless primary — the real no-app state
    code, msg = handle_cli_args(app, ["--nudge", "@Supervisor", "ping"])
    assert code == 4, "nudge with no running instance must exit 4"
    assert "not running" in (msg or "")
    assert handler.sent == []
    assert handler.published == []
    assert app.activate_calls == 0, "refusal path must never activate/present a GUI"


# ── Adapter: on_command_line bridges GTK command_line → handle_cli_args ─────


def test_on_command_line_sets_exit_status():
    app = CrabcakesApp()
    handler = FakeHandler()
    app._main_window = FakeWindow(handler)  # pretend the GUI is built

    stub = StubCommandLine(["--nudge", "@Supervisor", "adapter check"])
    app.on_command_line(app, stub)
    assert stub.exit_status == 0
    assert handler.sent == [("special:supervisor", "adapter check")]

    # Refusal path propagates the exit code too
    stub = StubCommandLine(["--nudge", "@Coder", "nope"])
    app.on_command_line(app, stub)
    assert stub.exit_status == 3

    # Empty argv (normal GUI launch) must route to activate, exit 0
    app2 = CrabcakesApp()
    app2.activate_calls = 0
    app2.activate = lambda: setattr(app2, "activate_calls", app2.activate_calls + 1)
    stub = StubCommandLine([])
    app2.on_command_line(app2, stub)
    assert stub.exit_status == 0
    assert app2.activate_calls == 1, "empty argv (normal launch) must activate the GUI"


def test_on_command_line_internal_failure_returns_defined_code():
    """Audit BUG #2: a raise inside the dispatch path must not escape.

    The guardrails can all pass and dispatch can still raise (handler error,
    audit-log write failure) — and by then side effects may already exist.
    Without a guard the exception escapes on_command_line, set_exit_status
    never runs, and GTK picks an arbitrary status, breaking the §3.3
    exit-code contract exactly when something went wrong.
    """
    import main as main_mod

    app = CrabcakesApp()
    handler = FakeHandler()
    app._main_window = FakeWindow(handler)
    handler.send_to_special_agent = _raise_dispatch_error

    stub = StubCommandLine(["--nudge", "@Supervisor", "boom"])
    rc = app.on_command_line(app, stub)

    assert rc == main_mod._EXIT_INTERNAL, (
        f"a dispatch-path failure must return the internal code, got {rc}"
    )
    assert stub.exit_status == main_mod._EXIT_INTERNAL, (
        f"exit status must be set to the internal code, got {stub.exit_status}"
    )
    assert app._main_window is not None, "the failure must not dismantle the app"
