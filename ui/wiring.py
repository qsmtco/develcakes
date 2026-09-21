# ui/wiring.py
# Small wiring helpers for the Settings integration.
# Extracted from ui/window.py to make the SettingsHandler ↔ Toolbar ↔ SettingsDialog
# callback wiring testable in isolation (without constructing the full window).

from __future__ import annotations

import logging
import os
from typing import Callable

from ui.handlers.settings_handler import SettingsHandler
from utils.providers_store import has_any_verified_provider, load_providers

logger = logging.getLogger(__name__)

# LOW-7: process-global env var that the image viewer in chat_bubble.py reads
# to determine the active project root. Set by set_active_project_path() on
# project open, cleared by clear_active_project_path() on project close.
# Without this wiring, the viewer only ever has the home + /tmp fallback
# roots — see test_low7_image_viewer.py for the threat model.
ACTIVE_PROJECT_ENV = "CRABCAKES_ACTIVE_PROJECT_PATH"


def set_active_project_path(project_path: str) -> None:
    """LOW-7 wiring: publish the active project path for the image viewer.

    Overwrites any prior value. Process-global (env vars are not per-window);
    this is the documented limitation — only the most recently opened project
    is in scope. The viewer falls back to home + /tmp if the env var is empty.

    Normalizes the path: expands ~ and resolves to an absolute path. This is
    required so chat_bubble.py's _is_path_in_allowed_roots (which calls
    os.path.realpath, which does NOT expand ~) can correctly check whether
    a file lives under this root.
    """
    if not project_path:
        logger.warning(
            "LOW-7: set_active_project_path called with empty path; "
            "viewer will fall back to home + /tmp"
        )
        return
    normalized = os.path.abspath(os.path.expanduser(project_path))
    os.environ[ACTIVE_PROJECT_ENV] = normalized


def clear_active_project_path() -> None:
    """LOW-7 wiring: clear the active project path on project close.

    Uses pop with a default so a stale value from a prior session cannot
    leak (and so the call is safe even if no project was ever opened).
    """
    os.environ.pop(ACTIVE_PROJECT_ENV, None)


def wire_settings_handler(
    handler: SettingsHandler,
    toolbar,
    *,
    settings_dialog_factory: Callable | None = None,
    agent_builder_factory: Callable | None = None,
    on_runtimes_refresh: Callable[[], None] | None = None,
) -> SettingsHandler:
    """Wire the SettingsHandler's callbacks to the toolbar and (optionally) the dialogs.

    - on_status_changed  → toolbar.set_settings_status(verified)
    - on_providers_changed → settings_dialog_factory().refresh_providers(providers)
      and agent_builder_factory().set_provider_options(providers)
      (both no-op if the factory is None or returns None)
    - on_providers_changed also invokes ``on_runtimes_refresh()`` (SPEC-01) after the
      dialog refreshes, so cached AgentRuntimes pick up edited provider config without
      an app restart. No-op when not provided.
    - Sets initial toolbar status from the current state of providers.yaml.

    Idempotent: calling twice on the same handler is a no-op.

    Returns the (now-wired) handler, so callers can keep a reference.
    """
    # BUG #4: make double-wiring a no-op
    if getattr(handler, "_wired", False):
        logger.debug("Handler already wired; skipping re-wire")
        return handler
    handler._wired = True

    def _on_status_changed(verified: bool) -> None:
        toolbar.set_settings_status(verified)

    def _on_providers_changed(providers) -> None:
        # BUG #2: type guard — providers must be a list
        if not isinstance(providers, list):
            logger.warning(
                "on_providers_changed called with non-list (type=%s); ignoring",
                type(providers).__name__,
            )
            return

        if on_runtimes_refresh is not None:
            try:
                on_runtimes_refresh()
            except Exception as e:
                logger.warning(
                    "Runtime provider refresh failed after provider save: %s", e
                )

        if settings_dialog_factory is not None:
            try:
                dialog = settings_dialog_factory()
                # BUG #3: hasattr check — reject truthy non-dialog returns
                if dialog is not None and hasattr(dialog, "refresh_providers"):
                    dialog.refresh_providers(providers)
                else:
                    logger.debug("Settings dialog factory returned non-dialog: %r", dialog)
            except Exception as e:
                logger.warning(
                    "Settings dialog refresh failed (dialog may not be open): %s", e
                )
        if agent_builder_factory is not None:
            try:
                builder = agent_builder_factory()
                # BUG #3: hasattr check — reject truthy non-dialog returns
                if builder is not None and hasattr(builder, "set_provider_options"):
                    builder.set_provider_options(providers)
                else:
                    logger.debug("Agent builder factory returned non-dialog: %r", builder)
            except Exception as e:
                logger.warning(
                    "Agent builder refresh failed (dialog may not be open): %s", e
                )

    # Mutate the handler's private callback slots (per the handler's __init__ API)
    handler._on_status_changed = _on_status_changed
    handler._on_providers_changed = _on_providers_changed

    # BUG #1: wrap initial toolbar status in try/except
    try:
        toolbar.set_settings_status(has_any_verified_provider(load_providers()))
    except Exception as e:
        logger.warning("Initial toolbar status set failed: %s", e)

    return handler
