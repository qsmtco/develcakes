#!/usr/bin/env python3
"""R-3 window smoke (SPEC-47 §2.4).

Constructs ``ui.window.MainWindow`` headlessly — run under ``xvfb-run -a`` —
and asserts the agent runtime handler (ARH) was wired into its dependents.

Rules:
  * Sets ``XDG_CONFIG_HOME`` to a temp dir BEFORE importing the app, so a
    run can never write into ~/.config/develcakes.
  * Does NOT import ``main`` (its header documents deliberate import-time
    side effects); it imports ``ui.window`` directly.
  * Exit 0 = pass; non-zero = fail (traceback on stderr).

This script is a committed row node (``rows.yaml`` R-3, runner: xvfb).
"""
from __future__ import annotations

import os
import sys
import tempfile

# ── Environment isolation: MUST precede any app import ────────────────────────
_tmp_config = tempfile.TemporaryDirectory(prefix="window-smoke-config-")
os.environ["XDG_CONFIG_HOME"] = _tmp_config.name

# Repo-root bootstrap: run from any cwd, same convention as scripts/crab_status.py
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ui.window import MainWindow

_APP_ID = "com.develcakes.window-smoke"


def main() -> int:
    app = Gtk.Application(application_id=_APP_ID)
    win = MainWindow(application=app)

    # The R-3 claim: the ARH is constructed and every dependent received it.
    arh = win._agent_runtime_handler
    if arh is None:
        print("window-smoke: FAIL — _agent_runtime_handler is None", file=sys.stderr)
        return 1

    # The dependent wiring the 2026-10-08 half-wired crash class broke:
    # the chat handler must hold the SAME handler object the window owns.
    chat_arh = win._chat_handler._agent_runtime_handler
    if chat_arh is not arh:
        print(
            "window-smoke: FAIL — chat handler's ARH is not the window's: "
            f"chat={chat_arh!r} window={arh!r}",
            file=sys.stderr,
        )
        return 1

    # The other two dependents are wired through the same handler object.
    # LeftPanel.set_special_agents stores it as _agent_runtime_handler.
    if getattr(win._left_panel, "_agent_runtime_handler", None) is not arh:
        print("window-smoke: FAIL — left panel did not receive the ARH", file=sys.stderr)
        return 1
    if getattr(win._main_content, "_agent_runtime_handler", None) is not arh:
        print("window-smoke: FAIL — main content did not receive the ARH", file=sys.stderr)
        return 1

    win.destroy()
    print("window-smoke: PASS — MainWindow constructed, ARH wired to dependents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
