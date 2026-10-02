#!/usr/bin/env python3
# main.py
# Application entry point — creates and runs the DevelcakesApp

import sys
import os
import hashlib
import json
import gi
import logging
from datetime import datetime, timezone

# Configure logging early — before any module imports that might use logging
_log_level = logging.DEBUG if os.environ.get("CRABCAKES_DEBUG") else logging.WARNING
logging.basicConfig(
    level=_log_level,
    format="%(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
# Require GTK 4.0 — must be called before importing Gtk
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, Gtk

# Import the main window (assembles all UI components)
from ui.window import MainWindow
from ui.styles import apply_styles

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
            application_id='com.crabcakes.app',
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        # Connect the 'activate' signal — fired when app is first started
        self.connect('activate', self.on_activate)
        # Connect 'command-line' — fired (in the primary instance) for every
        # invocation, including the very first one after flag registration.
        self.connect('command-line', self.on_command_line)
        # Set application icon for taskbar/dock (installed in hicolor icon theme)
        Gtk.Window.set_default_icon_name('crabcakes')
        # Set in on_activate; lets the command-line handler reach the handler
        # graph of the running instance. None until the GUI is built.
        self._main_window = None

    def on_activate(self, app):
        """
        Called when the application is activated.
        Creates the main window and displays it.
        """
        apply_styles()  # Register global CSS before any widgets are created
        win = MainWindow(application=app)  # Pass app as the application instance
        win.present()  # Show the window (GTK4 uses present() instead of show_all())
        self._main_window = win  # CLI handler reaches the handler graph through this

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
    # SPEC-08 SP3: the launch-time JSON→store migration is wired end-to-end
    # (agent/runtime.py) but DISABLED by default pending a cross-phase ruling:
    # SP2 made conversation load pure-JSON, so a migrated (renamed) session
    # loads as None → fresh conversation → its first save diverged-flags the
    # store and suspends the delta permanently. Do NOT set
    # CRABCAKES_MIGRATE_STORE=1 until the load-path ruling lands (see
    # .crabcakes/context.md 2026-10-01 and the SP3 phase report).
    app = DevelcakesApp()  # Create application instance
    return app.run(sys.argv)  # Explicit argv — see docstring; run(None) lies.


# Standard Python entry point guard
# Runs only when this file is executed directly (not imported)
if __name__ == "__main__":
    sys.exit(main())
