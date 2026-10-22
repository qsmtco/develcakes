# ui/handlers/agent_builder_handler.py
# Agent Builder handler — form logic for creating and editing user-defined agents.
#
# Owns: form state, validation, persistence for the Create/Edit Agent flow.
# Does NOT own: GTK widgets, other handlers.
#
# Architecture rule: does NOT import other handlers. Window wires callbacks.
# Does NOT import GTK — purely logic and data.

from __future__ import annotations

import logging
from typing import Callable

from utils.agent_defs import (
    delete_agent_def,
    get_available_prompts,
    get_available_providers,
    get_available_tools,
    get_default_si_config,
    load_agent_def,
    save_agent_def,
    validate_agent_def,
)

logger = logging.getLogger(__name__)


class AgentBuilderHandler:
    """
    Logic handler for the Create/Edit Agent flow.

    Per Section 8.6: all UI logic lives in a handler, not in views or window.
    Delegates I/O to utils/agent_defs.py. No GTK imports.

    Args:
        on_agent_saved: Called after a successful save. Receives the agent name.
        on_agent_deleted: Called after a successful delete. Receives the agent name.
    """

    def __init__(
        self,
        *,
        on_agent_saved: Callable[[str], None] | None = None,
        on_agent_deleted: Callable[[str], None] | None = None,
        GLib_module=None,        # gi.repository.GLib — for idle_add dispatch
        parent_window=None,     # Gtk.Window — for transient_for on confirmation dialog
    ):
        self._on_agent_saved = on_agent_saved
        self._on_agent_deleted = on_agent_deleted
        self._GLib = GLib_module
        self._parent_window = parent_window
        self._editing_name: str | None = None  # track original name for rename detection

    # ── Form templates ──────────────────────────────────────────────────────

    def create_new(self) -> dict:
        """Return a blank agent definition template for the form.

        Populates defaults for self_improvement based on can_write assumption.
        """
        return {
            "name": "",
            "emoji": "🤖",
            "role": "coder",
            "prompts": [],
            "tools": ["read_file", "list_files", "search_files"],
            "provider": "",
            "model": "",
            "fallback_provider": None,
            "self_improvement": get_default_si_config(can_write=False),
        }

    def load_for_edit(self, name: str) -> dict | None:
        """Load an existing agent definition for editing.

        Tracks the original name for rename detection on save.
        Returns the agent def dict, or None if not found.
        """
        self._editing_name = name
        return load_agent_def(name)

    # ── Persistence ──────────────────────────────────────────────────────────

    def save(self, agent_def: dict) -> tuple[bool, list[str]]:
        """Validate and save an agent definition.

        If the name changed since load_for_edit(), deletes the old file.
        Returns (success, errors). On success, fires on_agent_saved callback.
        """
        errors = validate_agent_def(agent_def)
        if errors:
            return False, errors

        try:
            # Detect rename — delete old file if name changed
            new_name = agent_def.get("name", "")
            if (self._editing_name
                    and self._editing_name != new_name
                    and load_agent_def(self._editing_name) is not None):
                delete_agent_def(self._editing_name)
                logger.info("Renamed agent: %s → %s", self._editing_name, new_name)

            save_agent_def(agent_def)
        except Exception as e:
            logger.exception("Failed to save agent definition")
            return False, [f"Save failed: {e}"]

        self._editing_name = new_name  # update for next save
        if self._on_agent_saved:
            self._on_agent_saved(new_name)
        logger.info("Agent saved: %s", new_name)
        return True, []

    def delete(self, name: str) -> bool:
        """Delete an agent definition file.

        Returns True if deleted. Fires on_agent_deleted callback on success.
        """
        from utils.agent_defs import delete_agent_def as _delete
        success = _delete(name)
        if success and self._on_agent_deleted:
            self._on_agent_deleted(name)
        if success:
            logger.info("Agent deleted: %s", name)
        return success

    def delete_agent_with_confirmation(self, name: str) -> None:
        """
        Show a modal GTK confirmation dialog, then delete the agent if confirmed.


        Args:
            name: Agent name to delete.


        Flow:
          1. Build Gtk.MessageDialog transient_for parent_window
          2. Show dialog with warning text
          3. On YES response: call self.delete(name)
          4. On NO response: close dialog, no action

        Thread-safe: dialog.show() must be called from main thread.
        If GLib_module is available, dispatches via GLib.idle_add().
        """
        from gi.repository import Gtk

        def _show_dialog():
            parent = self._parent_window
            dialog = Gtk.MessageDialog(
                transient_for=parent,
                modal=True,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.YES_NO,
                text=f'Delete agent "{name}"?',
            )
            dialog.set_property(
                "secondary-text",
                "This cannot be undone. The agent definition file will be removed.",
            )

            def on_response(_dialog, response_id):
                _dialog.close()
                if response_id == Gtk.ResponseType.YES:
                    success = self.delete(name)
                    if not success:
                        logger.warning("Failed to delete agent: %s", name)


            dialog.connect("response", on_response)
            dialog.show()

        if self._GLib is not None:
            self._GLib.idle_add(_show_dialog)
        else:
            _show_dialog()

    # ── Options for UI dropdowns ─────────────────────────────────────────────

    def get_tool_options(self) -> list[dict]:
        """Available tools with descriptions. For UI checkboxes."""
        return get_available_tools()

    def get_prompt_options(self) -> list[dict]:
        """Available prompts from prompts/ directory. For UI selector."""
        return get_available_prompts()

    def get_provider_options(self) -> list[dict]:
        """Available providers from providers.yaml. For UI dropdown."""
        return get_available_providers()

    def get_role_options(self) -> list[tuple[str, str]]:
        """(label, value) pairs for the Role dropdown: the three built-in
        roles (Supervisor, Coder, Debugger) plus user-defined roles from
        utils.roles.load_roles()."""
        from utils.roles import BUILTIN_ROLES, load_roles
        options = [(r.capitalize(), r) for r in BUILTIN_ROLES]
        options.extend((role["name"], role["slug"]) for role in load_roles())
        return options

    def save_role(self, name: str, prompt: str) -> tuple[str | None, str | None]:
        """Persist a user-defined role. Passthrough to utils.roles.save_role;
        returns its (slug, error) tuple unchanged."""
        from utils.roles import save_role as _save_role
        return _save_role(name, prompt)

    def save_provider(self, name: str, config: dict) -> bool:
        """Add or update a provider in providers.yaml.

        Raises ValueError on invalid input (mirror of
        settings_handler.add_or_update). Bypassing validation here
        reintroduces the Sonnet 5 regression — so we apply the SAME
        auto-detect + prefix-correction + invalid-caller guard that
        settings_handler does. The provider path cannot be the
        weaker sister.
        """
        from utils.providers_store import add_provider, load_providers
        from models.providers import ProviderConfig
        from agent.runtime import get_valid_callers
        # PROV-CALLER-CONSISTENCY (regression for Sonnet 5): mirror the
        # settings_handler.add_or_update auto-detect + prefix-correction
        # block so this path can no longer bypass it.
        # None-normalize: dict.get's default only fires on MISSING keys,
        # not on YAML-null values (which deserialize to None). Without
        # this, `caller:` in YAML leaks None into ProviderConfig.caller.
        caller = config.get("caller") or ""
        default_model = config.get("default_model", "")
        if not caller and default_model and "/" in default_model:
            caller = default_model.split("/")[0].lower()
        elif not caller:
            # Gap fix: default_model has no "/" (e.g. "gpt-4o") and caller
            # is empty — auto-detect cannot derive a caller. Surface a
            # clear error at save time rather than letting an empty
            # caller slip through to runtime.
            raise ValueError(
                "Cannot auto-detect caller: default_model must be '<vendor>/<model>'. "
                "Set caller explicitly or use a prefixed model."
            )
        if caller and default_model and "/" in default_model:
            # Use the live taxonomy (get_valid_callers) — not a hardcoded
            # set literal — so a new adapter added to agent.runtime
            # automatically extends the gate. Otherwise the hardcoded
            # set drifts out of sync (regression risk).
            prefix = default_model.split("/")[0].lower()
            if prefix in get_valid_callers() and prefix != caller:
                logger.warning(
                    "Provider %r has caller=%r but default_model prefix=%r; "
                    "correcting caller to %r. To override, use a model whose "
                    "prefix matches the caller's taxonomy key.",
                    name, caller, prefix, prefix,
                )
                caller = prefix
        if caller and caller not in get_valid_callers():
            valid = sorted(get_valid_callers())
            raise ValueError(
                f"Invalid caller {caller!r}. "
                f"Valid callers: {', '.join(valid)}. "
                f"Set the caller field explicitly or use a model with a recognized prefix."
            )
        provider = ProviderConfig(
            name=name,
            base_url=config.get("base_url", ""),
            api_key=config.get("api_key", ""),
            default_model=default_model,
            caller=caller,
            supports_tools=config.get("supports_tools", True),
            supports_streaming=config.get("supports_streaming", True),
            max_tokens=config.get("max_tokens", 128_000),
        )
        providers = load_providers()
        add_provider(providers, provider)
        return True

    def delete_provider(self, name: str) -> bool:
        """Remove a provider from providers.yaml.

        Returns True if the provider was removed, False if it didn't exist.
        """
        from utils.providers_store import load_providers, remove_provider
        providers = load_providers()
        existed = any(p.name == name for p in providers)
        if not existed:
            return False
        remove_provider(providers, name)
        return True
