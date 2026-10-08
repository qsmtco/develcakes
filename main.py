#!/usr/bin/env python3
# main.py
# Application entry point — creates and runs the DevelcakesApp

import sys
import os
import hashlib
import json
import gi
import logging
from datetime import UTC, datetime, timezone

from utils.config import get_env, migrate_v1_config  # migrate: SPEC-11 SP3 (D3)

# Configure logging early — before any module imports that might use logging
_log_level = logging.DEBUG if get_env("DEBUG") else logging.WARNING
logging.basicConfig(
    level=_log_level,
    format="%(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)

# SPEC-15b A1: floor the HTTP libraries. They log the request URL at
# INFO/DEBUG, and the Telegram bot token rides in the URL path
# (`/bot<token>/method`) — with DEBUG on, every poll would print the raw
# token. These libraries carry no signal we need above WARNING.
for _noisy in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# SPEC-15b A2: defense in depth — a RedactingFilter on the same loggers
# rewrites record.msg/record.args through the token redactor, so a future
# level change (or another transport logging a URL) still cannot leak it.
# Imported here (not at module top) so it lands AFTER basicConfig is set up.
from utils.log_redaction import install as _install_log_redaction

_install_log_redaction()

# SPEC-08 SP4A: store-mode load (agent/persistence.py) hydrates migrated
# sessions from transcript.db, so renaming the legacy JSON away no longer
# orphans a conversation — the one-time JSON→store migration sweep is safe
# to DEFAULT ON. The write is GATED on get_env (SPEC-11 SP2 fix round — a
# bare setdefault of the NEW name defeated the old-name kill-switch: with
# the operator holding CRABCAKES_MIGRATE_STORE=0 and the new name unset,
# setdefault wrote DEVELCAKES_MIGRATE_STORE=1 anyway and get_env read the
# new name first). Gated shape: the default lands ONLY when NEITHER name
# is set — an explicit =0 by the operator (either name) still skips the
# sweep. This MUST sit before the ui.window import below: agent.runtime
# reads the flag at MODULE-IMPORT time (its settings_handler importer pulls
# agent.runtime in at top level), so setting it inside main() would latch
# too late. Tests are isolated: tests/conftest.py pins
# CRABCAKES_MIGRATE_STORE=0 (old name — the gate must keep honoring it)
# before any agent.runtime import.
if get_env("MIGRATE_STORE") is None:
    os.environ["DEVELCAKES_MIGRATE_STORE"] = "1"

# Require GTK 4.0 — must be called before importing Gtk
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, Gtk

# Import the main window (assembles all UI components)
from ui.window import MainWindow
from ui.styles import apply_styles
from models.feed_card import FeedCardData  # SPEC-11 SP3: migration banner card

logger = logging.getLogger(__name__)

# ── CLI nudge channel (AGENTCTRL1 Phase 2 — SPEC-AGENT-CONTROL-1 §3) ──────────
#
# PM decision §11.1: the nudge channel reaches EXACTLY the Supervisor.
# The Supervisor is the orchestrator — it can address other agents through the
# normal in-app collaboration path, so direct CLI reach into Coder/Debugger
# buys nothing and widens the blast radius. Any other target exits 3.
_NUDGE_PERMITTED_TARGETS = ("supervisor",)
_NUDGE_SESSION_KEYS = {"supervisor": "special:supervisor"}
_NUDGE_MAX_CHARS = 4096  # mirrors the /ask payload cap (project-awareness.md)

# Exit codes (§3.3)
_EXIT_OK = 0
_EXIT_TURN_IN_FLIGHT = 2
_EXIT_BAD_TARGET = 3
_EXIT_NOT_RUNNING = 4
_EXIT_PAYLOAD_TOO_LONG = 5
_EXIT_NO_SESSION = 6
# Audit BUG #2: a failure INSIDE the dispatch path (handler raised after the
# guardrails passed, audit-log write failed, …). NOT in the spec's §3.3 table
# — added so the exit-code contract holds on the failure path too instead of
# letting the exception escape and GTK pick an arbitrary status. Spec needs a
# row for this code.
_EXIT_INTERNAL = 7

# States a turn may be in that make a nudge refuse to interleave (§3.4.3).
_TURN_BUSY_STATES = ("running", "streaming")


class DevelcakesApp(Gtk.Application):
    """
    Main application class.
    Gtk.Application handles:
      - Application lifecycle (startup, activate, shutdown)
      - Desktop integration (app menu, uniqueness, FAROS registration)
      - Command-line argument handling
    """

    def __init__(self):
        # Reverse-domain application ID — required by GTK for app identity
        # Convention: com.<org>.<app>
        #
        # HANDLES_COMMAND_LINE (AGENTCTRL1 Phase 2): a second invocation's
        # argv is forwarded over the session bus to the RUNNING instance and
        # the second process exits. That is the entire transport — no socket
        # of ours, no daemon, no reconnect loop.
        super().__init__(
            application_id='com.develcakes.app',
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        # Connect the 'activate' signal — fired when app is first started
        self.connect('activate', self.on_activate)
        # Connect 'command-line' — fired (in the primary instance) for every
        # invocation, including the very first one after flag registration.
        self.connect('command-line', self.on_command_line)
        # Set application icon for taskbar/dock. D5 (SPEC-11 SP3): the
        # THEME-NAME mechanism is preserved — GTK4 has no
        # Gtk.Window.set_default_icon (file-based is a GTK3 API, verified
        # absent); the name resolves via hicolor entries installed by
        # pyproject data-files (share/icons/hicolor/<size>/apps/develcakes.png).
        Gtk.Window.set_default_icon_name('develcakes')
        # Set in on_activate; lets the command-line handler reach the handler
        # graph of the running instance. None until the GUI is built.
        self._main_window = None
        # SPEC-11 SP3 (D3): migrate_v1_config() runs in main() BEFORE this
        # object is constructed; its report is parked here and the banner is
        # emitted in on_activate (the feed handler exists only after the
        # window build). None = nothing to report (fresh install, marker, or
        # both-dirs no-op).
        self._pending_migration_report: dict | None = None

    def on_activate(self, app):
        """
        Called when the application is activated.
        Creates the main window and displays it.
        """
        apply_styles()  # Register global CSS before any widgets are created
        win = MainWindow(application=app)  # Pass app as the application instance
        win.present()  # Show the window (GTK4 uses present() instead of show_all())
        self._main_window = win  # CLI handler reaches the handler graph through this
        # SPEC-11 SP3 (D3): the v1-config banner is DEFERRED to here — the
        # migration itself already ran at the top of main() (before the app
        # object existed), but the feed handler only exists once the window
        # is built. Emit once from the stored report; a None report (no-op
        # guard) emits nothing. report=None-check narrows for the type
        # checker; getattr keeps __init__-less test constructions safe.
        report = getattr(self, "_pending_migration_report", None)
        if report is not None:
            self._emit_migration_banner(report)
            self._pending_migration_report = None

    def _emit_migration_banner(self, report: dict) -> None:
        """Surface the one-time v1→v2 config-migration result as a feed card.

        SPEC-02 pattern (system card, no file snapshot). Success lists
        copied/skipped counts; failure names the failing entry AND states
        that the v1 dir is untouched (D3's non-destructive contract is
        user-facing). Defensive: a feed-less window shape must never crash
        startup — the report is dropped, not raised.
        """
        window = self._main_window
        feed = getattr(window, "_feed_handler", None) if window else None
        if feed is None:
            logger.warning("migration banner: no feed handler; report dropped")
            return
        failed = report.get("failed") or []
        if failed:
            title = "Config migration from v1 FAILED"
            body = (
                f"{failed[0]} — v1 untouched; develcakes starts fresh. "
                "Fix the v1 dir and relaunch to retry."
            )
        else:
            copied = report.get("copied") or []
            skipped = report.get("skipped") or []
            title = "Config migrated from v1"
            body = f"{len(copied)} copied, {len(skipped)} skipped."
        try:
            feed.add_card(
                FeedCardData(
                    card_type="system",
                    source="system",
                    title=title,
                    body=body,
                    author="system",
                    timestamp=datetime.now(UTC),
                    project_name="",
                )
            )
        except Exception:  # banner must never break startup (logged, dropped)
            logger.exception("migration banner card emission failed")

    def on_command_line(self, app, command_line) -> int:
        """
        GTK 'command-line' handler — receives every invocation's argv.

        In the PRIMARY instance this carries a second invocation's arguments
        (forwarded by GTK over the session bus). Empty argv (or a plain GUI
        launch) activates the window as before.
        """
        args = list(command_line.get_arguments())[1:]
        if not args:
            # Normal launch — show the GUI.
            self.activate()
            code = _EXIT_OK
        else:
            # Audit BUG #2: the dispatch path can raise (handler error, audit
            # write failure) AFTER guardrails pass and after side effects
            # (feed card / appends). Without this guard the exception escapes
            # on_command_line, set_exit_status never runs, and GTK picks an
            # arbitrary status — so the §3.3 exit-code contract silently
            # breaks exactly when something went wrong. Report it as internal.
            try:
                code, _msg = handle_cli_args(self, args)
            except Exception:
                logger.exception(
                    "on_command_line: handle_cli_args raised — reporting "
                    "internal failure (exit %d)", _EXIT_INTERNAL,
                )
                code = _EXIT_INTERNAL
        command_line.set_exit_status(code)
        return code


def _write_nudge_audit_record(target: str, text: str) -> None:
    """
    Append the CLI-nudge audit record to audit-log.jsonl (§3.3, §11.3).

    Content policy: the record carries a sha256 prefix of the payload —
    NEVER the raw text.
    """
    from utils.config import get_config_dir
    record = {
        "origin": "cli-nudge",
        "target": target,
        "chars": len(text),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    config_dir = get_config_dir()
    os.makedirs(config_dir, exist_ok=True)
    path = os.path.join(config_dir, "audit-log.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def handle_cli_args(app, args: list[str]) -> tuple[int, str]:
    """
    Handle a non-empty second-instance argument vector.

    AGENTCTRL1 Phase 2: --nudge is the only supported option.

    Guardrail order (§3.3): malformed form → app-running → reach →
    payload cap → turn-in-flight → session-exists → deliver.

    Returns (exit_code, message). The message is intended for the caller's
    terminal (GTK forwards the second process's stdout); the running
    instance's own log gets it too.
    """
    if args[0] != "--nudge":
        return _EXIT_BAD_TARGET, (
            f"unknown option: {args[0]!r} "
            '(supported: --nudge @Supervisor "<text>")'
        )

    # Form: --nudge @Agent "<text>" (mirrors /ask @Agent "..." — collab.md).
    if len(args) < 3:
        return _EXIT_BAD_TARGET, (
            'usage: --nudge @Supervisor "<text>" '
            "(only @Supervisor may be nudged)"
        )
    if len(args) > 3:
        return _EXIT_BAD_TARGET, (
            "payload must be ONE quoted argument "
            '(mirrors /ask @Agent "..." quoting)'
        )
    target_arg, text = args[1], args[2]

    # Guardrail: app must already be running (§3.1) — never start a GUI on a
    # nudge, never present() a window from this path.
    #
    # Mechanics note (deviation from the spec's literal `get_is_remote()`
    # wording, intent preserved): the forwarded nudge is handled BY the
    # primary instance, and g_application_get_is_remote() is False for a
    # primary — gating on it would refuse every nudge when the app IS
    # running. The correct discriminator is whether THIS instance has built
    # its UI: a --nudge-only launch with no instance running becomes a fresh,
    # windowless primary (activate is never called), so a missing window /
    # handler graph here means "crabcakes is not running".
    window = getattr(app, "_main_window", None)
    handler = getattr(window, "_agent_runtime_handler", None) if window is not None else None
    if handler is None:
        return _EXIT_NOT_RUNNING, "crabcakes is not running; start the app first"

    # Guardrail: reach — @Supervisor only (PM decision §11.1).
    if not target_arg.startswith("@"):
        return _EXIT_BAD_TARGET, (
            f"unknown or unauthorised target {target_arg!r}: "
            "only @Supervisor may be nudged"
        )
    target_name = target_arg[1:].lower()
    if target_name not in _NUDGE_PERMITTED_TARGETS:
        return _EXIT_BAD_TARGET, (
            f"unknown or unauthorised target {target_arg!r}: "
            "only @Supervisor may be nudged"
        )
    session_key = _NUDGE_SESSION_KEYS[target_name]

    # Guardrail: payload cap (4,096 chars, mirrors /ask).
    if len(text) > _NUDGE_MAX_CHARS:
        return _EXIT_PAYLOAD_TOO_LONG, (
            f"payload too long: {len(text)} chars (cap is {_NUDGE_MAX_CHARS})"
        )

    # Guardrail: turn in flight — a nudge never interleaves a live turn.
    state = handler.special_agent_turn_state(session_key)
    state_value = str(getattr(state, "value", state)).lower() if state is not None else None
    if state_value in _TURN_BUSY_STATES:
        return _EXIT_TURN_IN_FLIGHT, "turn in flight for Supervisor; retry when idle"

    # Guardrail: session must already exist (§3.3) — explicit --create comes
    # later; a nudge must not silently manufacture agent state.
    if not handler.special_agent_session_exists(session_key):
        return _EXIT_NO_SESSION, (
            "no Supervisor session in the active project — open the "
            "Supervisor tab first"
        )

    # Artefact 1 (always): feed card with visible CLI origin marker.
    handler.publish_cli_nudge_card(session_key, text)

    # Artefact 2 (always): audit record — hash only, never the text.
    _write_nudge_audit_record(session_key, text)

    # Delivery: the SAME path the chat box uses (§3.2) — turn tokens,
    # _ended_sessions and pre-loop preparation behave identically.
    handler.send_to_special_agent(session_key, text)
    logger.info("CLI nudge delivered to %s (%d chars)", session_key, len(text))
    return _EXIT_OK, "delivered"


def main():
    """
    Console entry point.

    Referenced by [project.scripts] in pyproject.toml, which is what makes
    the `crabcakes` command exist after `pip install -e .`. Kept separate from
    the __main__ guard below so the same code path serves both
    `crabcakes` and `python3 main.py`.

    AGENTCTRL1 Phase 2: sys.argv is passed EXPLICITLY. PyGObject's
    ``run(None)`` forwards an EMPTY argv to GApplication (not sys.argv), and
    with HANDLES_COMMAND_LINE an empty argv means "no command line" — GTK
    then emits plain 'activate' instead of 'command-line', so a
    `--nudge` invocation would try to build the full GUI (and its argv would
    be silently dropped). Verified empirically (GApplication probe +
    headless crash trace, 2026-09-13).
    """
    # SPEC-08 SP3→SP4: the launch-time JSON→store migration is wired
    # end-to-end (agent/runtime.py) and ENABLED by default since SP4 —
    # main.py writes DEVELCAKES_MIGRATE_STORE=1 above the window import,
    # gated on get_env so an explicit operator opt-out by EITHER name (new
    # or old) suppresses it. SP4's store-mode load closed the rename-safety
    # gap: a migrated (renamed) session hydrates from transcript.db rows
    # with the exact JSON shape, so its first save passes the dual-anchor
    # guard instead of diverged-flagging the store. Operator override:
    # launch with MIGRATE_STORE=0 (DEVELCAKES_ or CRABCAKES_ name) to skip
    # the sweep (JSON files stay authoritative; the banner card simply
    # never fires).
    # SPEC-11 SP3 (D3 ordering): one-time v1→v2 config migration — BEFORE
    # the app object, before any window/config-reading import side effects.
    # No-op on marker/content guards; a report is parked on the app and the
    # banner is DEFERRED to on_activate (the feed handler exists only after
    # the window build — verified: on_activate constructs MainWindow).
    report = migrate_v1_config()
    app = DevelcakesApp()
    app._pending_migration_report = report
    return app.run(sys.argv)  # Explicit argv — see docstring; run(None) lies.


# Standard Python entry point guard
# Runs only when this file is executed directly (not imported)
if __name__ == "__main__":
    sys.exit(main())
