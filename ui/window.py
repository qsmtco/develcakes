# ui/window.py
# Main application window — assembles toolbar, left panel, and main content.
#
# ── Handler Organization ────────────────────────────────────────────────────────
# All callback handlers are defined as private methods on this class.
# They are grouped by subsystem:
#   _setup_keyboard_shortcuts  — keyboard input
#   _on_prompt_selected        — prompt library
#   _on_stt_*                 — speech-to-text
#   _on_improve_*             — moved to MediaHandler (Phase 4)
#   _on_stt_*                 — moved to MediaHandler (Phase 4)
#   _on_project_*             — project tab management
#   GatewayHandler            — owns GatewayClient + AgentManager (Phase 2)
#   ChatHandler               — owns send/fan-out/routing (Phase 1)
#   MediaHandler              — owns STT + improve (Phase 4)
#
# Thread safety: GTK calls from background threads MUST go through GLib.idle_add().
# This applies to: _on_improve_result, _on_stt_partial, gateway callbacks.
# GatewayHandler.dispatch is used internally for its own thread safety.
#
# Project fan-out: _on_send checks if current tab is "project:<name>".
# If so, it calls load_members() and sends to each member independently.
# Responses from agents are routed back to the project tab via _agent_to_project lookup.

import logging
from typing import Callable

import gi

logger = logging.getLogger(__name__)
# Require GTK 4.0 — must be called before importing Gtk
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, Gio, GLib

# Import UI components
from ui.toolbar import Toolbar
from ui.views.feedbar import FeedBar
from ui.views.left_panel import LeftPanel
from ui.views.main_content import MainContent
from ui.views.activity_drawer import ActivityDrawer
from ui.handlers.chat_handler import ChatHandler
from ui.handlers.file_tree_handler import FileTreeHandler
from ui.handlers.gateway_handler import GatewayHandler
from ui.handlers.media_handler import MediaHandler
from ui.handlers.project_handler import ProjectHandler
from ui.handlers.activity_handler import ActivityHandler
from ui.handlers.work_handler import WorkHandler
from ui.handlers.collab_handler import CollabHandler
from ui.handlers.session_handler import SessionHandler

from ui.wiring import set_active_project_path, clear_active_project_path
from utils.project_awareness import seed_project_prompts

from models import work_store

from utils.config import COMMAND_PREFIX


class MainWindow(Gtk.ApplicationWindow):
    """Main window for the Crabcakes application."""

    def __init__(self, application):
        super().__init__(application=application, title="Crabcakes")
        self.set_default_size(800, 600)

        # Connect realize signal — set_icon_list requires a valid surface
        self.connect("realize", self._on_realize)

        # SPEC-UI-RESPONSIVENESS-2 Phase 1: flush the background feed writer
        # before the window destroys. No other close-request handler exists on
        # MainWindow (the ones in views/ belong to separate windows).
        self.connect("close-request", self._on_close_request)

        # Chat handler — owns message sending, fan-out, and response routing (Phase 1)
        self._chat_handler = None
        # Gateway handler — owns GatewayClient + AgentManager (Phase 2)
        self._gateway_handler = None
        # Media handler — owns STT + improve (Phase 4)
        self._media_handler = None
        # Input toolbar handler — owns find/replace, spell check, file I/O (Phase 5)
        self._input_toolbar_handler = None
        # AgentRuntime handler — owns special agent runtimes (Phase 1.4)
        self._agent_runtime_handler = None
        # Agent-to-project routing table — shared between ProjectHandler (writes) and ChatHandler (reads)
        from models import AgentRoutingTable
        self._agent_to_project = AgentRoutingTable()

        # Task handler — owns task command logic (Phase 7)
        self._work_handler = None
        # Collab handler — owns collaboration command logic (Phase 7)
        self._collab_handler = None
        # Session handler — owns session switching logic (Phase 7)
        self._session_handler = None
        # Connection sync handler — owns post-connect wiring (Phase 3a extraction)
        self._connection_sync_handler = None
        # Forward handler — owns agent-to-agent message forwarding (Phase 3b extraction)
        self._forward_handler = None

        # ── Project settings bar — branch refresh state (SPEC-...-FIX-3 §2.2) ──
        # Cache keyed by project_path — a switch A->B->A reuses A's cached branch.
        # Round 2 BUG #2/#7 + Round 3 BUG #5.
        self._cached_branch_by_path: dict[str, str] = {}   # {project_path: branch_name}
        self._branch_request_token: int = 0           # monotonic request id (BUG #7)
        self._branch_active_token: int | None = None  # token of in-flight worker (BUG #2)
        self._branch_request_path: str | None = None  # project path captured at schedule time (BUG #2)

        self._build()
        self._setup_keyboard_shortcuts()

    def _on_close_request(self, *args) -> bool:
        """Flush the background feed writer before the window destroys."""
        try:
            self._feed_handler.shutdown_persist_writer()
        except Exception:
            logger.exception("close-request: feed writer shutdown failed")
        return False  # allow default close handling

    def _build(self):
        """Composition root — all handler and view wiring lives here.

        This method is intentionally dense. It is the single place where
        all components are instantiated and connected. New handlers receive
        their dependencies here. See ARCHITECTURE.md §3.6 for the pattern.
        """
        # Chat render handler — owns text→bubble pipeline (Phase 2 refactor)
        # Created here and injected into both MainContent and ChatHandler so neither
        # instantiates it directly. window.py is the composition root.
        from gi.repository import GLib
        from ui.handlers.chat_render_handler import ChatRenderHandler
        self._chat_render_handler = ChatRenderHandler(GLib_module=GLib)

        # Create UI components
        toolbar = Toolbar(
            on_connect_clicked=self._on_connect_clicked,
            on_settings_clicked=self._open_settings,
        )
        self._toolbar = toolbar
        self._toolbar.update_connection_state("offline")

        self._main_content = MainContent()

        # Session switch menu needs AgentManager — set after gateway connects
        self._main_content.set_agent_manager(None)
        self._main_content.set_chat_render_handler(self._chat_render_handler)
        self._chat_render_handler.set_main_content(self._main_content)

        # Chat handler — gateway_client is a lambda to avoid stale None reference
        # (self._gw is None at construction, only set when Connect is clicked)
        self._chat_handler = ChatHandler(
            main_content=self._main_content,
            gateway_client=None,  # synced after connect via set_sync_callback
            agent_to_project=self._agent_to_project,
            projects_module=__import__("utils.projects", fromlist=["projects"]),
            GLib_module=GLib,
        )

        # Inject ChatRenderHandler into ChatHandler (window.py is composition root)
        self._chat_handler.set_chat_render_handler(self._chat_render_handler)

        # Wire Send button
        self._main_content.send_button.connect("clicked", self._chat_handler.on_send_clicked)

        # Left panel — created BEFORE GatewayHandler. FileTreeHandler must be
        # built first: it is injected into LeftPanel, which wires sort/git-status
        # callbacks to it during __init__.
        file_tree_handler = FileTreeHandler()
        left_panel = LeftPanel(
            on_prompt_selected=self._on_prompt_selected,
            on_project_selected=self._on_project_selected,
            file_tree_handler=file_tree_handler,
        )
        self._file_tree_handler = file_tree_handler
        self._left_panel = left_panel
        self._left_panel.set_main_content(self._main_content)

        # Agent card handler — agent_mgr set in ConnectionSyncHandler.sync() after connect
        from ui.handlers.agent_list_handler import AgentListHandler
        self._agent_list_handler = AgentListHandler(
            agent_mgr=None,
            on_agent_chat=lambda sk, n: self._on_agent_selected(sk, n),
            on_agent_toggle=None,  # left_panel._on_agent_toggle_clicked handles membership directly
        )
        self._left_panel.set_agent_list_handler(self._agent_list_handler)

        # AgentRuntime handler — owns AgentRuntime instances for special agents (Phase 1.4)
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
        self._agent_runtime_handler = AgentRuntimeHandler(
            main_content=self._main_content,
            chat_render_handler=self._chat_render_handler,
            GLib_module=GLib,
            review_handler=None,  # ReviewHandler created later in _build; Phase 1.5 will wire via setter
        )

        # Register built-in special agents from the registry
        from agent.special_agents import get_special_agents, get_auto_open_agents
        for agent_def in get_special_agents():
            self._agent_runtime_handler.add_special_agent(agent_def)

        # Phase 4 — Auto-open Auxilium tab on every launch.
        # Creates a tab for each agent with auto_open=True.
        auto_open_agents = get_auto_open_agents()
        if auto_open_agents:
            for agent_def in auto_open_agents:
                self._main_content.create_chat_tab(
                    agent_def.conv_id_prefix, agent_def.display_name
                )
                logger.info(
                    "Auto-opened agent tab: %s",
                    agent_def.display_name,
                )

        # ── Auxilium first-run wizard (D7 Phase 3) ────────────────────────────
        # If the user has no provider configured, show the wizard in the
        # Auxilium tab. On completion, dismiss the wizard and reload agents
        # so the new provider is picked up.
        self._auxilium_wizard = None
        self._auxilium_wizard_handler = None
        try:
            from pathlib import Path as _Path
            from ui.handlers.auxilium_wizard_handler import is_auxilium_wizard_needed
            from utils.config import get_config_dir as _get_config_dir
            _config_dir = _Path(_get_config_dir())
        except Exception:
            _config_dir = None

        if _config_dir is not None and is_auxilium_wizard_needed(_config_dir):
            try:
                from ui.handlers.auxilium_wizard_handler import AuxiliumWizardHandler
                from ui.views.auxilium_wizard import AuxiliumWizard

                _wizard_chat_box = self._main_content.get_chat_box_for_session("special:helper")
                if _wizard_chat_box is not None:
                    self._auxilium_wizard_handler = AuxiliumWizardHandler(
                        config_dir=_config_dir,
                        on_complete=lambda: self._on_auxilium_wizard_complete(),
                        on_error=lambda msg: logger.error("Auxilium wizard error: %s", msg),
                    )
                    self._auxilium_wizard = AuxiliumWizard(
                        handler=self._auxilium_wizard_handler,
                        on_install_check_complete=lambda: self._auxilium_wizard_handler.advance_to_gateway(),
                        on_gateway_check_complete=lambda: self._auxilium_wizard_handler.advance_to_provider(),
                        on_provider_selected=lambda: None,
                    )
                    _wizard_chat_box.append(self._auxilium_wizard)
                    self._auxilium_wizard_handler.start()
                    logger.info("Auxilium wizard shown — no provider configured")
            except Exception:
                logger.exception("Failed to show Auxilium wizard — continuing without it")
                self._auxilium_wizard = None
                self._auxilium_wizard_handler = None

        # Inject into dependents after _agent_runtime_handler is assigned
        self._chat_handler.set_agent_runtime_handler(self._agent_runtime_handler)
        self._left_panel.set_special_agents(self._agent_runtime_handler)
        self._main_content.set_agent_runtime_handler(self._agent_runtime_handler)

        # Agent builder handler — manages create/edit/delete for user-defined agents
        from ui.handlers.agent_builder_handler import AgentBuilderHandler
        self._agent_builder_handler = AgentBuilderHandler(
            GLib_module=GLib,
            parent_window=self,
            on_agent_saved=lambda name: self._agent_runtime_handler.reload_agents_and_mcp(
                on_complete=lambda: self._left_panel.set_special_agents(self._agent_runtime_handler)
            ),
            on_agent_deleted=lambda name: self._agent_runtime_handler.reload_agents_and_mcp(
                on_complete=lambda: self._left_panel.set_special_agents(self._agent_runtime_handler)
            ),
        )

        # Wire left panel agent builder callbacks
        self._left_panel.set_on_create_agent(lambda: self._open_agent_builder())
        self._left_panel.set_on_edit_agent(lambda name: self._open_agent_builder(name))
        self._left_panel.set_on_delete_agent(lambda name: self._agent_builder_handler.delete_agent_with_confirmation(name))

        # Settings handler — manages provider list, save/delete/test operations
        from ui.handlers.settings_handler import SettingsHandler
        self._settings_handler = SettingsHandler(
            GLib_module=GLib,
            parent_window=self,
            on_providers_changed=None,  # wired via wire_settings_handler below
            on_status_changed=None,
        )

        # Wire the SettingsHandler callbacks to the toolbar (and lazily to the settings dialog)
        from ui.wiring import wire_settings_handler
        self._settings_handler = wire_settings_handler(
            self._settings_handler,
            self._toolbar,
            settings_dialog_factory=lambda: None,
            agent_builder_factory=lambda: getattr(self, "_builder_dialog", None),
            on_runtimes_refresh=self._on_providers_changed,  # SPEC-01
        )

        # Prompts handler — wired to left_panel after both are created
        from ui.handlers.prompts_handler import PromptsHandler
        self._prompts_handler = PromptsHandler(
            on_refresh_ui=lambda: self._left_panel.refresh_prompts(),
            on_prompt_loaded=lambda fp, name, content: self._on_prompt_selected(content),
        )
        self._left_panel.set_prompts_handler(self._prompts_handler)

        # Gateway handler — owns GatewayClient + AgentManager (Phase 2)
        # Note: connect button is wired via Toolbar(on_connect_clicked=...) — not here
        self._gateway_handler = GatewayHandler(
            toolbar=self._toolbar,
            left_panel=left_panel,
            on_agent_selected=self._on_agent_selected,
            on_event=self._on_ws_event,
            GLib_module=GLib,
        )
        # # Response Status bar (right side)
        self._response_status = FeedBar()

        # Activity handler — owns the Response Status state machine (Phase 6)
        self._activity_handler = ActivityHandler(
            feedbar=self._response_status,
            main_content=self._main_content,
            GLib_module=GLib,
        )
        # Wire AgentRoutingTable so _is_ui_active can resolve project tabs for agent keys
        self._activity_handler.set_agent_routing(self._agent_to_project)

        # Media handler — owns STT (whisper.cpp push-to-talk) + improve (Phase 4)
        self._media_handler = MediaHandler(
            main_content=self._main_content,
            improve_module=__import__("utils.improve", fromlist=["improve"]),
            GLib_module=GLib,
        )

        # Input toolbar handler — owns find/replace, spell check, file I/O (Phase 5)
        from ui.handlers.input_toolbar_handler import InputToolbarHandler
        self._input_toolbar_handler = InputToolbarHandler(
            main_content=self._main_content,
            GLib_module=GLib,
        )

        # Wire input toolbar callbacks to handler — verified against actual setter names
        # NOTE: uses 'input_toolbar' to avoid shadowing the app-level 'toolbar' variable
        input_toolbar = self._main_content.toolbar
        input_toolbar.set_on_spell_toggle(self._input_toolbar_handler.toggle_spell_check)
        input_toolbar.set_on_open_file(self._input_toolbar_handler.load_file)
        input_toolbar.set_on_save_file(self._input_toolbar_handler.save_to_file)
        input_toolbar.set_on_find(self._input_toolbar_handler.find)
        input_toolbar.set_on_find_next(self._input_toolbar_handler.find_next)
        input_toolbar.set_on_find_prev(self._input_toolbar_handler.find_prev)
        input_toolbar.set_on_replace(self._input_toolbar_handler.replace_current)
        input_toolbar.set_on_replace_all(self._input_toolbar_handler.replace_all)
        input_toolbar.set_on_select_all(self._on_select_all)
        # Wire input buffer's 'changed' signal to handler + count update.
        # The previous set_on_buffer_changed(...) was a no-op storage call
        # (chat_input_toolbar.set_on_buffer_changed just stores the cb).
        # Real wiring: main_content exposes its own buffer-changed signal
        # (added in Phase 8), and we bridge it to (a) handler.on_buffer_changed
        # for spell-check debounce and (b) toolbar.update_word_count for the
        # user-visible word/char count label.
        def _on_input_buffer_changed(_buf):
            self._input_toolbar_handler.on_buffer_changed()
            words, chars, tokens = self._input_toolbar_handler.compute_count()
            self._main_content.toolbar.update_word_count(words, chars, tokens)

        self._main_content.set_on_buffer_changed(_on_input_buffer_changed)

        # Right-click spell-check suggestions on the input TextView.
        # Uses Gtk.TextView.set_extra_menu() — the GTK4-native context menu.
        # The TextView handles showing the menu; we just populate it.
        # This avoids the GestureClick grab conflicts with manual popovers
        # that caused UI freezes on Wayland.
        def _on_input_right_click(n_press, x, y, menu: Gio.Menu, action_group: Gio.SimpleActionGroup):
            """Right-click on input TextView — populate extra menu with spell suggestions."""
            handler = self._input_toolbar_handler
            if not handler.is_spell_enabled():
                return
            text_view = self._main_content.user_input
            # Move cursor to click position so the menu appears in context
            result, iter_at_pos = text_view.get_iter_at_location(int(x), int(y))
            if not result:
                return
            # Check if the iter has the spell-error tag
            buf = text_view.get_buffer()
            tag_table = buf.get_tag_table()
            spell_tag = tag_table.lookup("spell-error")
            if spell_tag is None:
                return  # spell check never ran (no tag created yet)
            if not iter_at_pos.has_tag(spell_tag):
                return  # word is not misspelled — no suggestions
            # Fetch suggestions
            suggestions = handler.get_suggestions_at_iter(iter_at_pos)
            # STALE-1 fix: capture clicked word text for later verification
            clicked_word = handler.get_word_at_iter(iter_at_pos)
            offset = iter_at_pos.get_offset()

            # Build suggestion menu items with Gio actions
            suggestion_section = Gio.Menu()
            if not suggestions:
                suggestion_section.append("(no suggestions)", None)
            else:
                for i, suggestion in enumerate(suggestions):
                    action_name = f"suggest_{i}"
                    # Create a parameterless action for this suggestion
                    def _make_apply(sugg=suggestion, off=offset, word=clicked_word):
                        def _apply(_action, _param):
                            fresh_iter = buf.get_iter_at_offset(off)
                            if not fresh_iter.inside_word():
                                logger.warning(
                                    "spell-suggestion: clicked word no longer at offset %d; ignoring",
                                    off,
                                )
                                return
                            current_word = handler.get_word_at_iter(fresh_iter)
                            if current_word.lower() != word.lower():
                                logger.warning(
                                    "spell-suggestion: word at offset %d changed from %r to %r; ignoring",
                                    off, word, current_word,
                                )
                                return
                            handler.replace_word_at_iter(fresh_iter, sugg)
                        return _apply
                    action = Gio.SimpleAction.new(action_name, None)
                    action.connect("activate", _make_apply())
                    action_group.add_action(action)
                    suggestion_section.append(suggestion, f"spell.{action_name}")
            menu.append_section(None, suggestion_section)

        self._main_content.set_on_input_right_click(_on_input_right_click)

        # Project handler — owns active project state + agent-to-project routing (Phase 3)
        from ui.handlers.project_handler import ProjectHandler
        self._projects = __import__("utils.projects", fromlist=["projects"])
        self._awareness = __import__("utils.project_awareness", fromlist=["project_awareness"])
        self._project_handler = ProjectHandler(
            left_panel=self._left_panel,
            projects_module=self._projects,
            agent_to_project=self._agent_to_project,  # shared AgentRoutingTable — ProjectHandler writes, ChatHandler reads
            GLib_module=GLib,
            awareness_module=self._awareness,
        )
        # Wire left_panel project events → ProjectHandler
        # Project list handler — provides project cards with colors and data
        from ui.handlers.project_list_handler import ProjectListHandler
        self._project_list_handler = ProjectListHandler(
            on_project_opened=self._project_handler.open_project,
        )
        left_panel._file_tree.set_project_list_handler(self._project_list_handler)
        left_panel._file_tree.set_on_navigate_back(self._on_file_tree_navigate_back)
        left_panel._file_tree.set_on_project_opened(self._project_handler.open_project)
        left_panel._file_tree.set_on_create_project(
            lambda name: self._project_handler.create_project(name, pm_name="Captain", pm_id="cli")
        )
        left_panel._file_tree.set_project_handler(self._project_handler)
        self._main_content.set_file_tree(self._left_panel._file_tree)
        self._left_panel.set_toggle_agent_callback(self._project_handler.toggle_agent)

        # Wire MainContent → ProjectHandler (for right-click project tab menu)
        self._main_content.set_project_handler(self._project_handler)
        self._main_content.set_on_project_tab_closed(
            lambda name: self._close_project_tab(name)
        )
        self._chat_handler.set_project_handler(self._project_handler)

        # Wire feed bar — updates when project opens or members change
        self._main_content.set_on_project_settings_update(self._on_feed_bar_update)

        # ── Feed handler + feed tab (Phase 2) ────────────────────────────────
        # ── Feed handler + feed tab (Phase 2 — Project Feed) ─────────────────────
        #
        # Architecture: LeftPanel Projects tab owns the FeedTab view (created once).
        # FeedHandler manages card state. FeedHandler is told about FeedTab via set_feed_tab().
        # window wires project lifecycle → LeftPanel ↔ FeedHandler coordination.
        #
        # Order: FeedHandler → CrabWatch → FeedTab → wire callbacks

        from ui.handlers.feed_handler import FeedHandler

        def _on_send_to_agent(session_key: str, text: str):
            """Send a message to an agent tab (used for rejection notifications)."""
            self._chat_handler.send_raw_message(session_key, text)

        def _on_show_feed_subtab():
            """Switch Projects notebook to the Feed sub-tab."""
            self._left_panel.switch_to_feed_tab()

        # FeedHandler created before FeedTab — set_feed_tab() called after FeedTab exists
        self._feed_handler = FeedHandler(
            GLib=GLib,
            on_send_to_agent=_on_send_to_agent,
            get_chat_box_for_session=self._main_content.get_chat_box_for_session,
            on_approve_exec=self._agent_runtime_handler.approve_exec,  # Phase E
        )

        # CrabWatch — filesystem watcher for project feed
        from ui.handlers.crabwatch_handler import CrabWatchHandler
        self._crabwatch_handler = CrabWatchHandler(
            GLib_module=GLib,
            on_event=self._feed_handler.on_filesystem_event,
        )

        # FeedTab created here (once) — inject into LeftPanel's Projects "Feed" sub-tab
        from ui.views.feed_tab import FeedTab
        self._feed_tab = FeedTab()

        # Tell FeedHandler about the FeedTab (FeedHandler needs it to add/remove cards)
        self._feed_handler.set_feed_tab(self._feed_tab)

        # Phase 5 + Phase 6: wire auto-accept warning dialog callback.
        # Phase 6: signature expanded to (category, agent_name, on_confirm,
        # on_cancel) per SPEC-AUTO-ACCEPT-GRANULAR-1 §2.6 BUG #6 fix. The
        # legacy 3-arg form is still honored by FeedHandler.set_show_auto_accept_warning
        # (legacy wrapper _on_auto_accept_toggled) but no longer wired here.
        self._feed_handler.set_show_auto_accept_warning(
            lambda category, agent_name, on_confirm, on_cancel: self._show_auto_accept_warning_v2(
                category, agent_name, on_confirm, on_cancel
            )
        )

        # V2: wire exec auto-accept callback (Phase 6 / §2.5 + §2.6).
        # FeedHandler.set_check_exec_auto_accept_callback_for_handler() takes
        # ARTH's setter and connects it to FH's getter, breaking the import
        # cycle (§8.6 R2 no handler-to-handler imports). When ARTH's
        # _do_approval_needed runs, it queries FH.get_exec_auto_accept_mode()
        # to decide whether to bypass card creation in Silent mode.
        self._feed_handler.set_check_exec_auto_accept_callback_for_handler(
            self._agent_runtime_handler.set_check_exec_auto_accept_callback
        )

        # Populate agent scope dropdown with registered agent names.
        self._feed_handler.set_agent_options_for_dropdown()

        # ── Project settings bar — wire all callbacks (SPEC-...-FIX-3 §2.2) ──
        # Round 2 BUG #4: main_content clicks → window handlers.
        self._main_content.set_on_settings_clicked(self._on_settings_btn_clicked)
        self._main_content.set_on_agent_cycle(self._on_agent_cycle_clicked)
        self._main_content.set_on_autoaccept_cycle(self._on_autoaccept_cycle_clicked)

        # Round 2 BUG #4: wire ProjectHandler solo-change -> bar refresh.
        self._project_handler.set_on_solo_target_changed(self._on_solo_target_changed)

        # Round 3 BUG #1/#2: register NAMED lifecycle methods as additional
        # callbacks (project_handler supports multiple open/close callbacks).
        self._project_handler.set_on_project_opened(self._on_project_opened)
        self._project_handler.set_on_project_closed(self._on_project_closed)
        # SOR §2.7: project-created System bubble (composition-root side).
        # Named handler renders the "add Supervisor manually" instruction into
        # the new project's chat tab. Create-only — never fires for open_project.
        self._project_handler.set_on_project_created(self._on_project_created_system_bubble)

        # Round 3 BUG #4: after async auto-accept confirmation, refresh the bar.
        self._feed_handler.set_on_auto_accept_level_changed(
            self._on_auto_accept_level_changed
        )

        # Inject FeedTab into LeftPanel's Projects notebook "Feed" sub-tab
        self._left_panel.set_feed_tab(self._feed_tab)

        # Wire ChatRenderHandler → FeedHandler (crabcard interception)
        def _on_crabcards_extracted(cards: list, session_key: str, tab_key: str = ""):
            from ui.views.chat_bubble import _set_crabcards_registry
            _set_crabcards_registry(cards, _on_show_feed_subtab)
            for card in cards:
                card.metadata["session_key"] = session_key  # agent's gateway key
                card.metadata["tab_key"] = tab_key or session_key  # chat box key (project:xxx or agent:xxx)
                self._feed_handler.add_card(card)

        self._chat_render_handler.set_on_crabcard_extracted(_on_crabcards_extracted)
        self._chat_render_handler.set_project_name("")  # set per-project when project opens

        # ── Wire project lifecycle → FeedHandler + CrabWatch ──────────────────────────
        #
        # When project OPENS:
        #   1. FeedHandler loads feed.json for the project
        #   2. CrabWatch starts watching the project directory
        #   3. ChatRenderHandler gets the project name for crabcard context
        #   4. Feed bar is updated
        #
        # When project CLOSES:
        #   1. FeedHandler clears its state for the project
        #   2. CrabWatch stops watching
        #   3. Feed bar is cleared

        self._project_handler.set_on_project_opened(
            lambda n, p: (
                self._main_content.create_chat_tab(f"project:{n}", f"Project: {n}"),
                self._left_panel.open_project_view(self._feed_tab),
                self._feed_handler.on_project_opened(n, p),
                self._crabwatch_handler.start_watching(p, n),
                self._chat_render_handler.set_project_name(n),
                self._agent_runtime_handler.set_active_project(n, p),
                # SPEC-activity-drawer: clear stale events when switching projects
                self._activity_drawer.clear_events(),
                self._on_feed_bar_update(n, len(self._project_handler.get_project_members(n)) if n else 0),
                # LOW-7 wiring: publish the active project path so the image viewer
                # in chat_bubble.py can scope _open_in_viewer to the project root
                # (in addition to the home + /tmp fallbacks). Helper lives in
                # ui/wiring.py so it's testable in isolation.
                set_active_project_path(p),
                # PHASE-5: seed per-project prompts, wire handlers, refresh UI
                # (SPEC-PROJECT-PROMPTS-DIRECTORY §2.4 — lazy-seed on open,
                # reset to app fallback on close; appended last so a failure
                # here can never skip pre-existing lifecycle cleanup above)
                seed_project_prompts(p),
                self._prompts_handler.set_project_path(p),
                self._input_toolbar_handler.set_project_path(p),
                self._prompts_handler.load_prompts(),
                self._left_panel.refresh_prompts(),
            )
        )
        self._project_handler.set_on_project_closed(
            lambda name: (
                self._feed_handler.on_project_closed(name),
                self._crabwatch_handler.stop_watching(),
                self._chat_render_handler.set_project_name(""),
                self._agent_runtime_handler.clear_active_project(),
                self._on_feed_bar_update(None, 0),
                # LOW-7 wiring: clear the env var when no project is active so
                # the viewer falls back to home + /tmp only.
                clear_active_project_path(),
                # PHASE-5: reset handlers to app-level fallback and refresh UI
                # (SPEC-PROJECT-PROMPTS-DIRECTORY §2.4)
                self._prompts_handler.set_project_path(None),
                self._input_toolbar_handler.set_project_path(None),
                self._prompts_handler.load_prompts(),
                self._left_panel.refresh_prompts(),
                # SPEC-UI-RESPONSIVENESS-2 Phase 1: flush the background feed
                # writer on project close (appended last so a failure here can
                # never skip the pre-existing lifecycle cleanup above).
                self._feed_handler.shutdown_persist_writer(),
            )
        )
        self._project_handler.set_on_members_changed(
            lambda n, m: self._on_feed_bar_update(n, len(m))
        )
        # ── End Feed handler ──────────────────────────────────────────────
        # Work handler — work unit commands (SPEC-TASK-SYSTEM-FULL-REDESIGN)
        # Replaces the former flat-task command handler (retired in
        # SPEC-AUDIT-CLEANUP-2 Phase 2). Receives project_handler (for active
        # project path/name + member lookup), the global work_store, and the
        # agent_runtime_handler (for /work start → send_to_special_agent).
        self._work_handler = WorkHandler(
            project_handler=self._project_handler,
            work_store=work_store,
            agent_runtime_handler=self._agent_runtime_handler,
            on_display_card=self._on_command_card,
            on_display_text=self._on_command_text,
            on_feed_card=self._feed_handler.add_card,
        )
        # Collab handler — collaboration commands (Phase 7)
        self._collab_handler = CollabHandler()
        # Session handler — session switching (Phase 7)
        # Needs AgentManager and ProjectHandler injected via setters after connect
        self._session_handler = SessionHandler(
            agent_manager=None,   # synced in ConnectionSyncHandler.sync()
            project_handler=self._project_handler,
        )

        # Review handler — owns review session lifecycle (Phase 3)
        # Created BEFORE CommandHandler so it can be passed as a constructor param.
        from ui.handlers.review_handler import ReviewHandler
        self._review_handler = ReviewHandler(
            GLib=GLib,
            main_content=self._main_content,
            project_handler=self._project_handler,
            on_review_started=self._on_review_started,
            on_review_ended=self._on_review_ended,
            on_display_card=self._on_command_card,
            on_display_text=self._on_command_text,
            on_feed_card=self._feed_handler.add_card,
        )

        # Command handler — owns backtick command parsing + routing (Phase 0.2)
        # Created AFTER ProjectHandler and ReviewHandler are initialized.
        from ui.handlers.command_handler import CommandHandler
        self._command_handler = CommandHandler(
            gateway_client=None,   # synced after connect via ConnectionSyncHandler.sync()
            agent_manager=None,    # synced after connect via ConnectionSyncHandler.sync()
            project_handler=self._project_handler,
            GLib_module=GLib,
            on_display_card=self._on_command_card,
            on_display_text=self._on_command_text,
            collab_handler=self._collab_handler,
            work_handler=self._work_handler,
            review_handler=self._review_handler,
            session_handler=self._session_handler,
        )
        self._command_handler.set_prefix(COMMAND_PREFIX)   # BUG #9 fix: read prefix from config
        # Inject CommandHandler into ChatHandler (ChatHandler calls process_input before send)
        self._chat_handler.set_command_handler(self._command_handler)
        # Populate CommandHandler with special agent names for @mention resolution in ask/delegate/stop/tell commands
        self._command_handler.set_special_agents(self._agent_runtime_handler.get_special_agents())
        # Wire ReviewHandler into AgentRuntimeHandler (deferred to avoid circular dep in _build order)
        self._agent_runtime_handler.set_review_handler(self._review_handler)
        # Wire FeedHandler into ReviewHandler (REVIEW-PERSIST-1: accept_changes/
        # reject_changes persist the resolution back onto the needs_review cards).
        self._review_handler.set_feed_handler(self._feed_handler)
        # Wire FeedHandler into AgentRuntimeHandler (Phase D: tool call feed cards)
        self._agent_runtime_handler.set_feed_handler(self._feed_handler)
        # Wire AgentRoutingTable into AgentRuntimeHandler (solo DM response routing)
        self._agent_runtime_handler.set_agent_routing(self._agent_to_project)
        # Wire ProjectHandler → AgentRuntimeHandler for /clear command.
        # Spec: docs/specs/STEP-COUNT-RESET-FIX.md Edit 5. The /clear command
        # resets a special agent's in-memory conversation + deletes the
        # persisted JSON so step_count starts fresh.
        self._project_handler.set_clear_callback(self._agent_runtime_handler.clear_conversation)
        # Wire the /clear UI side effect: empty the chat box after the
        # data-plane clear succeeds. Handoff: clear-ui-fix.md. The closure
        # resolves the chat box via _main_content.get_chat_box_for_session
        # and removes all its children. Runs on the main thread (cmd_clear
        # is dispatched via CommandHandler which is on the main thread
        # when called from the chat input).
        self._project_handler.set_clear_chat_callback(
            lambda sk: self._clear_chat_box(sk)
        )
        # Wire local agent lifecycle → ActivityHandler (offline mode progress bar)
        self._agent_runtime_handler.set_on_agent_start(
            lambda sk: self._activity_handler.on_agent_start(sk)
        )
        self._agent_runtime_handler.set_on_agent_end(
            lambda sk: self._activity_handler.on_agent_end(sk)
        )

        # Phase A — Wire the context-meter callback via the
        # set_on_token_breakdown_extra() slot. The existing
        # _on_token_breakdown in agent_runtime_handler.py dispatches to
        # the logger.info (preserved) and to this extra listener.
        def _resolve_agent_info(sk: str) -> tuple[str | None, str | None]:
            """Resolve (agent_name, agent_color) for a session key."""
            agent_name = None
            agent_color = None
            if self._main_content._agent_mgr is not None:
                agent_name = self._main_content._agent_mgr.get_name(sk)
            if not agent_name:
                from agent.special_agents import get_special_agents
                from models.colors import color_for_special_agent
                for agent_def in get_special_agents():
                    if sk == agent_def.conv_id_prefix:
                        agent_name = agent_def.display_name
                        agent_color = color_for_special_agent(agent_def.role)
                        break
            if agent_name and not agent_color:
                if self._main_content._agent_mgr is not None:
                    agent_color = self._main_content._agent_mgr.get_color(agent_name)
            return agent_name, agent_color

        def _update_agent_display(sk: str):
            """Update context meter avatar/name for a session key."""
            agent_name, agent_color = _resolve_agent_info(sk)
            if agent_name:
                self._main_content.update_agent_context_display(agent_name, agent_color or "#6366f1")

        def _on_context_meter(sk: str, breakdown: dict) -> None:
            usage_pct = breakdown.get("usage_percent", 0.0)
            self._main_content.set_context_meter(sk, usage_pct)
            _update_agent_display(sk)
        self._agent_runtime_handler.set_on_token_breakdown_extra(_on_context_meter)

        # Update avatar/name on tab switch
        self._main_content.set_on_session_changed(_update_agent_display)

        # Phase B — Wire /compact data-plane + UI side-effect.
        self._project_handler.set_compact_callback(
            self._agent_runtime_handler.compact_conversation
        )
        self._project_handler.set_compact_chat_callback(
            lambda sk, result: self._show_compact_bubble(sk, result)
        )

        # ── Agent Command Handler (Phase 6.2) ─────────────────────────────────────
        # Scans agent responses for backtick commands, routes to target agents,
        # and relays answers back to the asking agent via pending-ask tracking.
        from ui.handlers.agent_command_handler import AgentCommandHandler
        self._agent_command_handler = AgentCommandHandler(GLib_module=GLib)
        self._agent_command_handler.set_command_handler(self._command_handler)
        self._agent_command_handler.set_agent_runtime_handler(self._agent_runtime_handler)
        # Wire callbacks into both agent response pipelines
        self._chat_handler.set_on_agent_response(self._agent_command_handler.on_agent_response)
        self._agent_runtime_handler.set_on_agent_response(self._agent_command_handler.on_agent_response)

        # Forward handler — owns agent-to-agent message forwarding (Phase 3b extraction)
        from ui.handlers.forward_handler import ForwardHandler
        self._forward_handler = ForwardHandler(
            main_content=self._main_content,
            chat_handler=self._chat_handler,
            chat_render_handler=self._chat_render_handler,
            agent_runtime_handler=self._agent_runtime_handler,
            gateway_handler=self._gateway_handler,
        )

        # Connection sync handler — owns post-connect wiring (Phase 3a extraction)
        from ui.handlers.connection_sync_handler import ConnectionSyncHandler
        self._connection_sync_handler = ConnectionSyncHandler(
            chat_handler=self._chat_handler,
            main_content=self._main_content,
            agent_list_handler=self._agent_list_handler,
            gateway_handler=self._gateway_handler,
            project_handler=self._project_handler,
            command_handler=self._command_handler,
            agent_command_handler=self._agent_command_handler,
            session_handler=self._session_handler,
            feed_handler=self._feed_handler,
            left_panel=self._left_panel,
            review_handler=self._review_handler,
            activity_handler=self._activity_handler,
            agent_to_project=self._agent_to_project,
            on_forward_clicked=self._forward_handler.show_forward_popover,
            project_path_provider=lambda: self._project_handler.get_active_project_path() if self._project_handler else None,
            main_window=self,
        )
        # Wire the sync callback to fire on gateway connect
        self._gateway_handler.set_sync_callback(self._connection_sync_handler.sync)

        # SPEC-activity-drawer Phase 1: construct the ActivityDrawer BEFORE the
        # connection sync handler tries to wire it. The drawer widget itself is
        # lightweight (a Gtk.Box shell), so constructing it here is safe; the
        # actual re-parenting of main_content into the vertical Paned happens
        # in the drawer block at the end of _build().
        self._activity_drawer = ActivityDrawer()
        # ActivityWiringHandler owns ALL activity→drawer routing (online + offline).
        # Wire() is called unconditionally at startup so the drawer works from the
        # first local-agent tool call — no gateway required.
        from ui.handlers.activity_wiring_handler import ActivityWiringHandler
        self._activity_wiring_handler = ActivityWiringHandler(
            activity_handler=self._activity_handler,
            agent_runtime_handler=self._agent_runtime_handler,
            activity_drawer=self._activity_drawer,
        )
        self._activity_wiring_handler.wire()

        # Wire project lifecycle → ReviewHandler
        self._project_handler.set_on_project_opened(
            lambda n, p: (self._review_handler.on_project_opened(n, p))
        )
        self._project_handler.set_on_project_closed(
            lambda name: (self._review_handler.on_project_closed(name))
        )

        # Work handler lifecycle: load/migrate Work Units on open, release on
        # close (SPEC-TASK-SYSTEM-FULL-REDESIGN §3.3, §11). Must bind the store
        # BEFORE any /work command or awareness snapshot can read it.
        # set_on_project_opened/closed APPEND callbacks, so existing wiring
        # (feed, review, etc.) is preserved.
        self._project_handler.set_on_project_opened(
            lambda n, p: self._work_handler.load_for_project(p)
        )
        self._project_handler.set_on_project_closed(
            lambda name: self._work_handler.close_project()
        )



        # Wire STT + improve buttons
        self._main_content.set_on_stt_click(self._media_handler.on_stt_click)
        self._main_content.set_on_improve_click(self._media_handler.on_improve_click)

        # Right-side vertical stack: feedbar above main content
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        right_box.append(self._response_status)
        right_box.append(self._main_content)

        # Horizontal paned split: left panel | right content
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_start_child(left_panel)
        paned.set_end_child(right_box)
        paned.set_resize_start_child(True)
        paned.set_resize_end_child(True)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)
        paned.set_position(250)

        # Vertical layout: toolbar → paned → status bar
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(toolbar)
        main_box.append(paned)

        # A-9: Status bar with agent_id display
        _status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        _status_bar.add_css_class("statusbar")
        _status_bar.set_margin_start(6)
        _status_bar.set_margin_end(6)
        _status_bar.set_margin_bottom(2)
        self._agent_id_label = Gtk.Label(label="Agent: —")
        self._agent_id_label.add_css_class("dim-label")
        self._agent_id_label.set_halign(Gtk.Align.START)
        self._agent_id_label.set_hexpand(True)
        _status_bar.append(self._agent_id_label)
        main_box.append(_status_bar)

        self.set_child(main_box)

        # ── Activity Drawer (SPEC-activity-drawer Phase 1) ───────────────
        # Wrap main_content in a vertical Paned with the drawer below.
        # The drawer is global (one per window), not per-tab.
        # NOTE: self._activity_drawer is constructed earlier in _build() so
        # ConnectionSyncHandler can hold a reference to it. This block now
        # only handles the re-parenting of main_content into the Paned.
        self._activity_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self._activity_paned.set_end_child(self._activity_drawer)
        # Default: most space to chat (chat ~600px of ~800px window)
        self._activity_paned.set_position(600)
        self._activity_paned.set_shrink_end_child(True)
        self._activity_paned.set_resize_end_child(False)
        # Remove main_content from right_box BEFORE re-parenting it into the
        # new vertical Paned (GTK4 paned asserts child is unparented).
        right_box.remove(self._main_content)
        self._activity_paned.set_start_child(self._main_content)
        right_box.append(self._activity_paned)

    # ── Keyboard shortcuts ───────────────────────────────────────────────────

    def _set_window_icon(self):
        """Set the window icon from the PNG icon set (GTK4 via GdkSurface approach)."""
        from pathlib import Path
        icon_path = str(Path(__file__).resolve().parent.parent / "icons" / "256.png")
        surface = self.get_surface()
        if surface is None:
            return
        try:
            texture = Gdk.Texture.new_from_filename(icon_path)
            surface.set_icon_list([texture])
        except Exception as e:
            logger.warning(f"Could not load window icon: {e}")

    def _on_realize(self, widget):
        """Called when the widget surface is created. Set the window icon here."""
        self._set_window_icon()

    def _setup_keyboard_shortcuts(self):
        """Bind Shift+Enter in the input box to send."""
        controller = Gtk.EventControllerKey()
        controller.connect("key-pressed", self._on_input_key_press)
        self._main_content.user_input.add_controller(controller)

    def _on_input_key_press(self, controller, keyval, keycode, state):
        """Shift+Enter sends the message."""
        if keyval == Gdk.KEY_Return and (state & Gdk.ModifierType.SHIFT_MASK):
            self._chat_handler.on_send()
            return True
        return False

    # ── Prompt callback ─────────────────────────────────────────────────────

    def _on_select_all(self):
        """Select all text in the input TextView."""
        buf = self._main_content.user_input.get_buffer()
        # select_range(insert, selection_bound) — place cursor at end so
        # Shift+click extends naturally from the end of the selection.
        buf.select_range(buf.get_end_iter(), buf.get_start_iter())
        self._main_content.user_input.grab_focus()

    def _on_prompt_selected(self, content):
        """Insert prompt content into the user input TextView at cursor position."""
        buffer = self._main_content.user_input.get_buffer()
        cursor_iter = buffer.get_iter_at_mark(buffer.get_insert())
        buffer.insert(cursor_iter, content)

    # ── Command handlers (Step 0.3) ────────────────────────────────────────

    def _on_command_text(self, session_key: str, text: str):
        """Display a command response text bubble in the current tab.

        Called by CommandHandler via on_display_text callback when a command
        returns response_text (e.g. error messages, help output).
        """
        chat_box = self._main_content.get_chat_box()
        if chat_box is None:
            return
        bubble = self._chat_render_handler.render_sync("CrabCakes", text, session_key)
        if bubble is not None:
            chat_box.append(bubble)
            self._main_content.scroll_chat_to_bottom()


    # ── Review callbacks (owned by ReviewHandler) ──────────────────────────────

    def _on_review_started(self, project_name: str, bar) -> None:
        """Called by ReviewHandler when a review session starts. Stub — no-op."""

    def _on_review_ended(self, project_name: str) -> None:
        """Called by ReviewHandler when a review session ends. Stub — no-op."""

    def _on_command_card(self, card: dict):
        """Render a command result card in the current tab.

        Called by CommandHandler via on_display_card callback when a command
        returns response_card (e.g. task card, status card).
        """
        # card = {type, ...fields} — rendered as a special bubble
        session_key = self._main_content.get_current_session_key() or ""
        chat_box = self._main_content.get_chat_box()
        if chat_box is None or self._chat_render_handler is None:
            return
        self._chat_render_handler.render_event_card(card["type"], chat_box, **card)
        self._main_content.scroll_chat_to_bottom()




    def _on_project_selected(self, path):
        """Handle file tree selection — open diff viewer for the clicked file."""
        project_path = self._project_handler.get_active_project_path()
        if project_path is None:
            return

        project_name = self._project_handler.get_active_project_name()
        if project_name is None:
            return

        import os
        rel_path = os.path.relpath(path, project_path)

        # M11 fix: reject paths that escape the project root
        if rel_path.startswith(".."):
            return

        review_state = self._review_handler.get_state(project_name)
        checkpoint_sha = review_state.checkpoint_sha if review_state and review_state.is_active() else None

        from ui.views.diff_viewer import DiffViewer

        # M22 fix: session_key captured in closure, not passed through DiffViewer
        def on_revert(file_path: str, target_sha: str, on_complete=None):
            self._review_handler.revert_file_to_sha(project_name, file_path, target_sha,
                                                    on_complete=on_complete)

        viewer = DiffViewer(
            file_path=rel_path,
            project_path=project_path,
            checkpoint_sha=checkpoint_sha,
            on_back=lambda: self._main_content.hide_diff_viewer(),
            on_revert=on_revert,
        )
        self._main_content.show_diff_viewer(viewer)

    def _clear_chat_box(self, session_key: str) -> None:
        """Empty the chat box for a session. /clear UI side effect.

        Handoff: .crabcakes/handoffs/clear-ui-fix.md.

        Resolves the chat box via _main_content.get_chat_box_for_session
        and removes all its children. No-ops if the session has no open
        tab (the user may have closed the tab before /clear ran, or the
        session is for an agent whose tab was never created).

        Runs on the main thread (the caller is cmd_clear, dispatched via
        CommandHandler which is on the main thread when called from the
        chat input). GTK widget removal is safe here.
        """
        chat_box = self._main_content.get_chat_box_for_session(session_key)
        if chat_box is None:
            logger.debug("_clear_chat_box: no chat box for session %s", session_key)
            return
        # Gtk.Box children iteration: get_first_child() returns the first
        # child or None when empty. Remove until None.
        while True:
            child = chat_box.get_first_child()
            if child is None:
                break
            chat_box.remove(child)
        logger.info("Cleared chat box for session %s", session_key)

    def _show_compact_bubble(self, session_key: str, result: dict) -> None:
        """Render a "🧹 Compacted" bubble into the session's chat box.

        Spec: docs/specs/SPEC-CONTEXT-UI-COMPACT-LLM-2026-07-10.md §3.2.

        Mirrors _clear_chat_box's pattern (resolves chat_box, appends).
        Uses ChatRenderHandler.render_sync to build the bubble. No-op
        if the chat box isn't available (user closed the tab).
        """
        chat_box = self._main_content.get_chat_box_for_session(session_key)
        if chat_box is None:
            logger.debug("_show_compact_bubble: no chat box for %s", session_key)
            return
        removed = int(result.get("messages_removed", 0))
        freed = int(result.get("tokens_freed", 0))
        text = (
            f"🧹 Compacted. Removed {removed} message"
            f"{'s' if removed != 1 else ''}, freed ~{freed:,} tokens."
        )
        bubble = self._chat_render_handler.render_sync(
            "Agent", text, session_key, agent_name=None
        )
        if bubble is not None:
            chat_box.append(bubble)
            self._main_content.scroll_chat_to_bottom()

    # ── Auxilium wizard completion ───────────────────────────────────────────

    def _on_auxilium_wizard_complete(self) -> None:
        """Called when the Auxilium first-run wizard finishes successfully.

        Removes the wizard widget from the Auxilium chat tab and reloads
        agent config so the new provider is picked up.
        """
        logger.info("Auxilium wizard complete — reloading agent config")

        # Remove wizard from chat_box
        if hasattr(self, "_auxilium_wizard") and self._auxilium_wizard is not None:
            self._auxilium_wizard.cleanup()
            chat_box = self._main_content.get_chat_box_for_session("special:helper")
            if chat_box is not None:
                chat_box.remove(self._auxilium_wizard)
            self._auxilium_wizard = None
        if hasattr(self, "_auxilium_wizard_handler"):
            self._auxilium_wizard_handler = None

        # Reload agent config so the new provider is picked up
        try:
            self._agent_runtime_handler.reload_agents_and_mcp(
                on_complete=lambda: logger.info("Agents reloaded after Auxilium wizard")
            )
        except Exception as e:
            logger.exception("Failed to reload agents after Auxilium wizard: %s", e)

    # ── Gateway toggle ──────────────────────────────────────────────────────

    def _on_connect_clicked(self, *args):
        """Toggle gateway connection — delegates to GatewayHandler."""
        gh = self._gateway_handler
        if gh.is_connected():
            gh.disconnect()
            self._chat_handler.set_gateway_client(None)
            self._main_content.set_agent_manager(None)
        else:
            gh.connect()

    def _close_project_tab(self, name: str):
        """
        Close a project: return Projects tab in LeftPanel to picker view,
        close the project tab in main content, reset project state.
        """
        # 1. Reset project state (clears _active_project_name, fires on_project_closed callbacks)
        self._project_handler.close_project(name)
        # 2. Reparent FileTree back to Stack picker, destroy nested Notebook
        self._left_panel.close_project_view()
        # 3. Close the project tab in main content
        self._main_content.close_project_tab(name)
        # 4. Clear the feed bar
        self._on_feed_bar_update(None, 0)

    def _on_file_tree_navigate_back(self, project_name):
        """← back button in FileTree — close project view in LeftPanel."""
        if project_name:
            self._close_project_tab(project_name)

    def _on_feed_bar_update(self, project_name: str, member_count: int,
                            *, solo_target=None, auto_accept_level=None,
                            branch_name=None):
        """Update the project settings bar with all per-project state.

        Backward compatible: the four lifecycle call sites pass
        (project_name, member_count) and the remaining state is resolved here.
        """
        if not project_name:
            self._main_content.update_project_settings("", 0, None, "off", None)
            return
        # Resolve solo target from ProjectHandler (source of truth).
        if solo_target is None and self._project_handler is not None:
            solo_target = self._project_handler.get_solo_target(project_name)
        # Resolve auto-accept level from FeedHandler (source of truth).
        if auto_accept_level is None and self._feed_handler is not None:
            auto_accept_level = self._feed_handler.get_auto_accept_level()
        # Branch scheduling — Round 3 BUG #6: separate the TWO distinct reasons
        # a branch refresh might be needed from "a worker is already running".
        #   needs_resolution: the ACTIVE project's branch is not yet cached
        #                    (checked against the path-keyed cache, BUG #5).
        #   already_running:  a worker is in flight for the CURRENT request.
        if branch_name is None and self._project_handler is not None:
            active_path = self._project_handler.get_active_project_path() or ""
            cached_for_active = self._cached_branch_by_path.get(active_path)
            needs_resolution = cached_for_active is None
            already_running = self._branch_active_token is not None
            if needs_resolution and not already_running:
                self._schedule_branch_refresh(
                    project_name, member_count, solo_target, auto_accept_level
                )
            branch_name = cached_for_active
        self._main_content.update_project_settings(
            project_name, member_count, solo_target,
            auto_accept_level or "off", branch_name,
        )

    def _schedule_branch_refresh(self, project_name, member_count,
                                 solo_target, auto_accept_level):
        """Start a background branch lookup, guarded by a monotonic request token.

        All state transitions happen on the GTK thread. The worker only reads a
        captured project_path and reports back; it never mutates window state.
        """
        if self._branch_active_token is not None:
            return  # a worker is already in flight — don't stack a second one

        import os
        path = self._project_handler.get_active_project_path()
        if not path:
            return

        self._branch_request_token += 1
        token = self._branch_request_token
        self._branch_active_token = token
        self._branch_request_path = path

        import threading
        t = threading.Thread(
            target=self._resolve_branch_worker,
            args=(token, path, project_name, member_count,
                  solo_target, auto_accept_level),
            daemon=True,
        )
        t.start()

    def _resolve_branch_worker(self, token, path, project_name, member_count,
                               solo_target, auto_accept_level):
        """Background worker: resolve the branch for the CAPTURED path.

        Reads only `path` (captured) — never the live active project. On
        completion, dispatches a main-thread callback. If the token no longer
        matches, the result is DISCARDED.
        """
        branch = None
        try:
            from utils.git_ops import get_branch
            result = get_branch(path)
            # get_branch returns success=True with "(detached HEAD)" for detached;
            # failure (non-git, unborn) -> success=False -> None -> "—".
            branch = result.stdout if result.success else None
        except Exception:
            import logging
            logging.getLogger(__name__).exception("branch lookup failed for %s", path)
            branch = None
        from gi.repository import GLib
        GLib.idle_add(
            lambda: self._on_branch_result(token, path, project_name, member_count,
                                           solo_target, auto_accept_level, branch)
        )

    def _on_branch_result(self, token, path, project_name, member_count,
                          solo_target, auto_accept_level, branch):
        """GTK-thread callback applying a branch result IF it is still current.

        Round 3 BUG #2: ALL staleness + active-identity checks run BEFORE any
        state is mutated. A stale result can never write _cached_branch_by_path
        for the wrong project.
        """
        # Clear the in-flight marker (always — the worker has reported back).
        if token == self._branch_active_token:
            self._branch_active_token = None

        # 1) Superseded by a newer request OR the project closed/switch -> discard.
        if token != self._branch_request_token:
            return
        if path != self._branch_request_path:
            return

        # 2) Active project must still be the one we resolved for — check name
        #    AND path BEFORE writing the cache (BUG #2 / BUG #5).
        current_name = self._project_handler.get_active_project_name() \
            if self._project_handler else None
        if current_name != project_name:
            return
        current_path = self._project_handler.get_active_project_path() \
            if self._project_handler else None
        if current_path != path:
            return

        # All checks pass — safe to commit to the path-keyed cache (BUG #5).
        self._cached_branch_by_path[path] = branch
        self._on_feed_bar_update(project_name, member_count,
                                 solo_target=solo_target,
                                 auto_accept_level=auto_accept_level,
                                 branch_name=branch)

    def _on_project_closed(self, name: str) -> None:
        """Invalidate any in-flight branch request when a project closes.

        Round 3 BUG #1: defined as a NAMED method (not inserted into the existing
        tuple lambda, which cannot contain assignment statements). Registered as an
        ADDITIONAL callback via set_on_project_closed — the handler supports
        multiple open/close callbacks, so this runs alongside (not instead of)
        the existing feed/crabwatch/review shutdown lambdas.
        """
        self._branch_request_token += 1   # invalidate any in-flight worker
        self._branch_active_token = None
        self._branch_request_path = None

    def _on_project_opened(self, name: str, path: str) -> None:
        """Invalidate in-flight branch state when a project opens or switches.

        Round 3 BUG #2: FIX-2 only invalidated on CLOSE. A project A->B switch
        without opening invalidation could let A's in-flight worker apply A's
        branch to B. This named method is registered as an ADDITIONAL
        set_on_project_opened callback (handler fires cb(name, path) at line 132).

        NOTE: the path-keyed cache (BUG #5) is deliberately NOT cleared here —
        switching back to A should reuse A's cached branch. Only the in-flight
        marker is invalidated so a stale worker result cannot land.

        BUILD-TIME FIX (Round 4 BUG #1): after invalidation, also re-run
        _on_feed_bar_update so the newly opened project gets its branch
        scheduled even if a previous worker was in flight.
        """
        self._branch_request_token += 1
        self._branch_active_token = None
        self._branch_request_path = None
        # Re-evaluate the bar for the newly active project (BUILD-TIME FIX Round 4).
        try:
            members = self._project_handler.get_project_members(name) \
                if self._project_handler else []
            self._on_feed_bar_update(name, len(members))
        except Exception:
            logger.exception("Failed to re-evaluate settings bar on project open")

    def _on_project_created_system_bubble(self, name: str, path: str) -> None:
        """Render the project-created System bubble (composition-root side).

        SOR §2.7: fired by ProjectHandler.set_on_project_created AFTER
        open_project completes (dispatched via the handler's GLib.idle_add).
        This method defers its widget work through GLib.idle_add so the
        project tab exists before the callback body resolves the chat box.
        Create-only — never fires for open_project of an existing project.
        """
        def _deferred():
            session_key = f"project:{name}"
            try:
                # Outer catch-all: any unguarded operation in the deferred
                # body (tab creation, chat box lookup, scroll) must not
                # escape to the GTK main loop unlogged.
                chat_box = self._main_content.get_chat_box_for_session(session_key)
                if chat_box is None:
                    self._main_content.create_chat_tab(session_key, "System")
                    chat_box = self._main_content.get_chat_box_for_session(session_key)
                if chat_box is None:
                    return False  # chat box unavailable — no-op safely
                text = (
                    f"New project '{name}' created. Add the Supervisor agent from the "
                    f"Agents tab (click the +), then send it a message like "
                    f"'I'm ready' to begin onboarding."
                )
                bubble = None
                try:
                    bubble = self._chat_render_handler.render_sync(
                        "System", text, session_key, tab_key=session_key
                    )
                    if bubble is not None:
                        chat_box.append(bubble)
                except Exception:
                    logger.exception(
                        "Failed to render project-created System bubble for %s", session_key
                    )
                self._main_content.scroll_chat_to_bottom()
            except Exception:
                logger.exception(
                    "Failed to process project-created System bubble for %s", session_key
                )
            return False  # don't repeat
        GLib.idle_add(_deferred)

    def _on_agent_cycle_clicked(self, current_solo):
        """Cycle agent label: ALL(None) -> member[0] -> ... -> member[N-1] -> ALL(None)."""
        project_name = self._project_handler.get_active_project_name() \
            if self._project_handler else None
        if not project_name:
            return
        members = self._project_handler.get_project_members(project_name)
        if not members:
            return
        if current_solo is None or current_solo not in members:
            next_solo = members[0]
        else:
            idx = members.index(current_solo)
            next_solo = members[idx + 1] if idx < len(members) - 1 else None
        self._project_handler.set_solo_target(project_name, next_solo)
        # set_solo_target fires _on_solo_target_changed -> bar refreshes.

    def _on_autoaccept_cycle_clicked(self, current_level):
        """Cycle auto-accept (file changes): off -> diffs -> files -> all -> off.

        Round 3 BUG #4: this does NOT optimistically rebuild the bar before
        confirmation. set_auto_accept_level() shows the warning gate on enable
        and only commits on confirm; the bar refresh happens in the
        on_auto_accept_level_changed callback AFTER _commit_auto_accept_level.
        """
        project_name = self._project_handler.get_active_project_name() \
            if self._project_handler else None
        if not project_name:
            return
        cycle = {"off": "diffs", "diffs": "files", "files": "all", "all": "off"}
        next_level = cycle.get(current_level, "off")
        if self._feed_handler is not None:
            self._feed_handler.set_auto_accept_level(next_level)
        # No bar rebuild here — the confirmation callback handles it (BUG #4).

    def _on_solo_target_changed(self, project_name: str):
        """ProjectHandler fired after a solo-target change.

        Round 3 BUG #3: guard against a stale/non-active project name. The
        ProjectHandler implementation validates the project exists; this window
        guard additionally enforces that it is the ACTIVE project so a stale
        right-click selection cannot rebuild the bar for a closed project.
        """
        if not project_name or self._project_handler is None:
            return
        if self._project_handler.get_active_project_name() != project_name:
            return  # stale/non-active project — ignore (BUG #3)
        members = self._project_handler.get_project_members(project_name)
        self._on_feed_bar_update(
            project_name,
            len(members),
            solo_target=self._project_handler.get_solo_target(project_name),
        )

    def _refresh_settings_bar_for_active(self, auto_accept_level=None):
        """Helper: rebuild the settings bar for the currently active project.

        Used by the on_auto_accept_level_changed callback (Round 3 BUG #4) so
        the bar reflects the new auto-accept level only AFTER async confirmation
        has committed it.
        """
        project_name = self._project_handler.get_active_project_name() \
            if self._project_handler else None
        if not project_name:
            self._on_feed_bar_update("", 0, auto_accept_level=auto_accept_level)
            return
        members = self._project_handler.get_project_members(project_name)
        self._on_feed_bar_update(
            project_name,
            len(members),
            solo_target=self._project_handler.get_solo_target(project_name),
            auto_accept_level=auto_accept_level,
        )

    def _on_auto_accept_level_changed(self, level: str):
        """Callback fired by FeedHandler after an auto-accept level COMMITS.

        Delegates to _refresh_settings_bar_for_active so the bar shows the
        newly confirmed level (Round 3 BUG #4).
        """
        self._refresh_settings_bar_for_active(level)

    def _on_settings_btn_clicked(self):
        """⚙ -> open the existing Settings dialog (fresh instance each call)."""
        self._open_settings()

    def _on_providers_changed(self) -> None:
        """SPEC-01: a provider save landed — refresh cached runtime provider config.

        Fired via wire_settings_handler's on_providers_changed closure after a
        SettingsHandler.add_or_update/remove. Guarded: Settings can save before the
        runtime handler is constructed (first-run wizard ordering).
        """
        arh = getattr(self, "_agent_runtime_handler", None)
        if arh is not None:
            arh.refresh_provider_config()

    def update_agent_id_display(self, agent_id: str) -> None:
        """A-9: Update the agent_id label in the status bar."""
        if hasattr(self, "_agent_id_label") and self._agent_id_label:
            self._agent_id_label.set_text(f"Agent: {agent_id}")

    def _on_ws_event(self, event, payload):
        """Handle incoming gateway events — route to handlers.

        All events go to ActivityHandler for progress tracking. Chat events also
        go to ChatHandler for bubble rendering (separate responsibility).
        """
        self._activity_handler.on_gateway_event(event, payload)
        if event == "chat":
            self._chat_handler.on_chat_event(event, payload)

    # ── Agent selection callback ────────────────────────────────────────────

    def _on_agent_selected(self, session_key, agent_name):
        """Called when an agent row is clicked — create/open chat tab."""
        self._main_content.create_chat_tab(session_key, agent_name)

    # ── Auto-Accept warning dialog (Phase 5) ───────────────────────────────

    def _show_auto_accept_warning(
        self,
        agent_name: str,
        on_confirm: Callable,
        on_cancel: Callable,
    ) -> None:
        """
        Show a warning dialog when the user toggles auto-accept ON.

        Explains that auto-accept will automatically approve all future
        file-change cards from the named agent. User can confirm or cancel.
        If canceled, the toggle snaps back to OFF. (Phase 5)

        Args:
            agent_name: Human-readable agent name for the dialog message.
            on_confirm: Callback to invoke if user clicks "Turn On".
            on_cancel: Callback to invoke if user clicks "Cancel".
        """
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gtk

        # FIX: GTK4 MessageDialog uses `secondary_text=` constructor kwarg.
        # GTK3's `format_secondary_text` method does not exist in GTK4.
        # (Pattern matches ui/views/chat_input_toolbar.py:336.)
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Enable Auto-Accept for {agent_name}?",
            secondary_text=(
                f"All future file-change cards from {agent_name} will be "
                f"automatically accepted without review. This cannot be "
                f"undone for cards already accepted."
            ),
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Turn On", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)

        # Bug A fix: one-shot dispatch guard. `dialog.close()` below triggers a
        # second `response` emission with DELETE_EVENT; without this guard the
        # cancel branch would fire after the confirm branch, subverting the
        # user's consent (state persisted ON while toggle flipped back OFF).
        # See .crabcakes/coder-bugs.md (auto-accept response cascade).
        _dispatched = [False]

        def _on_response(dialog, response):
            if _dispatched[0]:
                return
            _dispatched[0] = True
            if response == Gtk.ResponseType.OK:
                on_confirm()
            else:
                on_cancel()
            dialog.close()

        dialog.connect("response", _on_response)
        dialog.show()

    def _show_auto_accept_warning_v2(
        self,
        category: str,
        agent_name: str,
        on_confirm: Callable,
        on_cancel: Callable,
    ) -> None:
        """V2 warning dialog for per-type auto-accept activation. (Phase 6 / §2.6)

        Per SPEC-AUTO-ACCEPT-GRANULAR-1 §2.6 (BUG #6 fix): the v2 callback
        signature is (category, agent_name, on_confirm, on_cancel). The
        category drives the dialog title/body copy; agent_name is the
        human-readable name of the agent the auto-accept applies to.

        Args:
            category: One of "diffs" | "files" | "exec". Drives the
                title/body copy via the dicts below. Unknown categories
                fall back to a generic title/body so future categories
                don't break the dialog.
            agent_name: Human-readable agent identifier (resolved by
                FeedHandler._resolve_agent_name_for_dialog). Inserted
                into the body to give the user context on what they
                are enabling auto-accept FOR.
            on_confirm: Called if user clicks "Turn On".
            on_cancel: Called if user clicks "Cancel" or dismisses the
                dialog.

        Dialog infrastructure mirrors _show_auto_accept_warning (Phase 5):
        Gtk4 MessageDialog with WARNING type, OK/CANCEL buttons, default
        response CANCEL, _dispatched=[False] guard against double-emission
        on close, dialog.connect("response", _on_response), dialog.show().

        Why this method coexists with the legacy _show_auto_accept_warning:
        FeedHandler.set_show_auto_accept_warning accepts both 3-arg and
        4-arg callbacks. Phase 4 wired the legacy 3-arg lambda; Phase 6
        rewires it to this 4-arg method. Tests in
        tests/test_window_auto_accept_warning.py bind the legacy method
        directly via MainWindow._show_auto_accept_warning(self, ...) and
        continue to work without modification.
        """
        titles = {
            "diffs": "Auto-accept diffs?",
            "files": "Auto-accept file changes?",
            "exec":  "Auto-approve exec commands?",
        }
        bodies = {
            "diffs": (
                f"{agent_name} will silently auto-accept every diff it "
                f"writes. You will not see the diff before it is committed."
            ),
            "files": (
                f"{agent_name} will silently auto-accept every "
                f"file_created/file_modified/file_deleted card it produces. "
                f"You will not see the change before it is committed."
            ),
            "exec": (
                f"{agent_name} will silently auto-approve every shell "
                f"command it runs. This includes rm, git push, network "
                f"calls, anything. There is no undo."
            ),
        }
        title = titles.get(category, "Enable auto-accept?")
        body = bodies.get(category, f"Enable auto-accept for {category}?")

        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gtk

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=title,
            secondary_text=body,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Turn On", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)

        # Same one-shot guard as _show_auto_accept_warning (Bug A fix).
        _dispatched = [False]

        def _on_response(dialog, response):
            if _dispatched[0]:
                return
            _dispatched[0] = True
            if response == Gtk.ResponseType.OK:
                on_confirm()
            else:
                on_cancel()
            dialog.close()

        dialog.connect("response", _on_response)
        dialog.show()

    # ── Agent Builder integration ──────────────────────────────────────────

    def _open_agent_builder(self, edit_name: str | None = None) -> None:
        """Open the Agent Builder dialog for creating or editing an agent."""
        from ui.views.agent_builder import AgentBuilderDialog

        if edit_name:
            agent_def = self._agent_builder_handler.load_for_edit(edit_name)
            if agent_def is None:
                logger.warning("Agent not found for editing: %s", edit_name)
                return
            is_edit = True
        else:
            # New agent — use template with sensible defaults
            agent_def = self._agent_builder_handler.create_new()
            is_edit = False

        self._builder_dialog = AgentBuilderDialog(
            self,
            handler=self._agent_builder_handler,
            agent_def=agent_def,
            is_edit=is_edit,
            on_save=lambda values: self._on_builder_save(values),
            on_cancel=lambda: self._on_builder_cancel(),
        )
        self._builder_dialog.show()

    def _on_builder_save(self, values: dict) -> None:
        """Called when the builder dialog fires save."""
        ok, errors = self._agent_builder_handler.save(values)
        if not ok:
            self._builder_dialog.show_errors(errors)
            return
        self._builder_dialog.close()

    def _on_builder_cancel(self) -> None:
        """Called when the builder dialog is cancelled."""
        pass  # dialog already closes itself

    # ── Settings integration ─────────────────────────────────────────────

    def _open_settings(self) -> None:
        """Open the Settings dialog (fresh instance each time)."""
        from ui.views.settings_dialog import SettingsDialog
        dialog = SettingsDialog(
            parent=self,
            handler=self._settings_handler,
            on_close=lambda: None,
        )
        dialog.show()







