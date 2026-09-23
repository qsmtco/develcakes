# ui/handlers/project_handler.py
# Project handler — extracted from window.py Phase 3.
#
# Owns: active project name, agent-to-project routing lookup.
# Does NOT own: MainContent, LeftPanel, ChatHandler.
#
# Architecture rule: does NOT import other handlers. Window wires cross-handler
# communication via callbacks.
#
# Thread safety: all GTK operations are dispatched via GLib.idle_add().
# This handler has no background threads of its own — all entry points
# (open_project, toggle_agent) are called from the GTK main thread.

from typing import Callable
import os
import logging

from models.command import Command, CommandResult
from models import work_store
from utils.git_ops import init_repo, stage_all, commit
from utils.workflow_state import init_workflow
from utils.project_awareness import seed_project_prompts

_logger = logging.getLogger(__name__)


class ProjectHandler:
    """
    Handles project tab lifecycle and membership routing.

    Owns:
      _active_project_name — currently open project name (or None)
      _agent_to_project   — AgentRoutingTable instance; shared with ChatHandler

    Does NOT own: MainContent, LeftPanel, ChatHandler — receives them as deps.

    Thread safety: all GTK calls dispatched via GLib.idle_add() when GLib_module
    is provided. Safe to call from GTK main thread without GLib.

    Args:
        main_content:      DEPRECATED — no longer used (project view is in LeftPanel)
        left_panel:       LeftPanel instance — for refresh_agents_with_project()
        projects_module:  utils.projects module — for load_members() / save_members()
        agent_to_project: AgentRoutingTable — shared with ChatHandler (writes here, reads there)
        GLib_module:      gi.repository.GLib or None — for thread-safe GTK dispatch
    """

    def __init__(
        self,
        left_panel,
        projects_module,
        agent_to_project,  # AgentRoutingTable
        GLib_module=None,
        awareness_module=None,  # utils.project_awareness module (optional)
    ):
        self._lp = left_panel
        self._projects = projects_module
        self._GLib = GLib_module
        self._awareness = awareness_module  # utils.project_awareness for .crabcakes/ access

        # Shared routing table — AgentRoutingTable instance from window
        self._agent_to_project = agent_to_project
        self._agent_mgr = None  # injected via set_agent_manager() after gateway connect
        self._active_project_name: str | None = None
        self._active_project_path: str | None = None  # path for current project

        # ── Per-project solo DM target ────────────────────────────────────
        # Key: project_name, Value: member session_key or None (all = broadcast)
        self._solo_targets: dict[str, str | None] = {}
        # Round 2 BUG #4: fired after a solo-target change so the settings bar
        # can refresh immediately on right-click member selection.
        self._on_solo_target_changed: Callable[[str], None] | None = None

        # ── Cross-handler callbacks (set by window) ───────────────────────
        self._on_project_opened: list[Callable] = []   # window's callbacks
        self._on_project_closed: list[Callable] = []   # window's close callbacks
        self._on_members_changed: Callable | None = None   # window's callback
        # SOR §2.7: fired after a project is CREATED (not just opened) —
        # used by window.py for the project-created System bubble.
        self._on_project_created: Callable[[str, str], None] | None = None
        # /clear command callback — injected by window.py to call
        # AgentRuntimeHandler.clear_conversation(session_key). None means
        # the runtime handler hasn't been wired yet (e.g. test fixtures).
        self._clear_callback: Callable[[str], bool] | None = None
        # /clear — UI side effect callback. After the data-plane clear
        # (self._clear_callback) succeeds, the chat box for this session
        # is emptied so the user doesn't see stale bubbles. Wired by
        # window.py to a closure over self._main_content. Callable[[str], None].
        self._clear_chat_callback: Callable[[str], None] | None = None
        # /cost runtime usage callback — injected by window.py to call
        # AgentRuntimeHandler.get_session_usage() for gateway agents.
        # Returns a dict mapping session_key -> (total_tokens, total_cost).
        # None means the runtime handler hasn't been wired yet (e.g. test
        # fixtures); the in-memory cache path is then skipped.
        self._runtime_usage_fn: Callable[[], dict] | None = None
        # Phase B — /compact injection slots.
        self._compact_callback: Callable[[str, str], dict] | None = None
        self._compact_chat_callback: Callable[[str, dict], None] | None = None

    # ── Public API — for window / other handlers ───────────────────────────

    def open_project(self, name: str, path: str):
        """
        Create a project chat tab and activate it.
        Called by LeftPanel when user double-clicks a project directory.

        Args:
            name:  Project display name
            path:  Project directory path
        """
        self._active_project_name = name
        self._active_project_path = path

        # Initialize .crabcakes/ directory (migrates legacy config if needed)
        if self._awareness:
            self._awareness.init_project_config(path, name)

        # Auto-add onboarding agents if not already members
        self._auto_add_onboarding_agents(path)

        # Initialize workflow.md (idempotent — skips if already exists)
        try:
            init_workflow(path)
        except Exception:
            pass  # non-fatal

        # Create the project tab in main content
        # NOTE: No chat tab creation here. Project view lives in LeftPanel's Projects tab.

        # Refresh the agents list to show +/− buttons
        self._dispatch(lambda: self._lp.refresh_agents_with_project(name))

        # Populate agent → project routing lookup
        members = self._load_members(name)
        for member_key in members:
            self._agent_to_project.add(member_key, name)

        # Notify window (for any external side-effects)
        for cb in self._on_project_opened:
            cb(name, path)

    def create_project(self, name: str, path: str | None = None, pm_name: str = "", pm_id: str = "") -> str | None:
        """
        Create a new project directory and open it.
        Called by FileTree when user fills out the New Project form.

        Args:
            name:  Project display name (must be non-empty)
            path:  Optional path override. Defaults to $CRABCAKES_PROJECTS_DIR/<name>
            pm_name: Project manager display name (e.g. "Captain")
            pm_id:   Project manager identifier (e.g. "cli")

        Returns:
            The project path on success, or None on failure.
        """
        # Validate name
        if not name or not name.strip():
            return None
        name = name.strip()

        # Resolve default path from projects module
        if not path:
            projects_dir = self._projects._PROJECTS_DIR_REF[0]
            path = os.path.join(projects_dir, name)

        # Check if directory already exists
        if os.path.exists(path):
            return None

        # Create the directory
        try:
            os.makedirs(path, exist_ok=False)
        except OSError:
            return None

        # Initialize .crabcakes/ with awareness artifacts
        if self._awareness:
            self._awareness.init_project_config(path, name, pm_name, pm_id)

        # Auto-add onboarding agents (Crabcakes 🦀) to new project team
        self._auto_add_onboarding_agents(path)

        # Initialize workflow.md (idempotent — creates with onboarding as current)
        try:
            init_workflow(path)
        except Exception:
            pass  # non-fatal

        # Seed per-project prompts library (SPEC-PROJECT-PROMPTS-DIRECTORY
        # §2.5 — copy-only-if-missing, non-fatal on failure)
        try:
            seed_project_prompts(path)
        except Exception as e:
            _logger.warning("prompts seed failed for %s: %s", path, e)

        # Auto-initialize git repo with initial commit
        result = init_repo(path)
        if result.success:
            stage_all(path)
            commit(path, f"project {name} created via CrabCakes")
        else:
            _logger.warning("git init failed for %s: %s", path, result.error)

        # Refresh awareness snapshot AFTER git init so git state is captured
        if self._awareness:
            snapshot = self._awareness.build_awareness_snapshot(path)
            self._awareness.save_awareness_snapshot(path, snapshot)

        # Open the project (creates tab, refreshes agents, fires callbacks)
        self.open_project(name, path)

        # SOR §2.7: notify the composition root AFTER open_project completes,
        # dispatched via GLib.idle_add so the project tab exists before the
        # callback body resolves the chat box. Create-only — never fires for
        # open_project() of an existing project.
        if self._on_project_created is not None:
            _cb = self._on_project_created
            self._dispatch(lambda: _cb(name, path))

        return path

    def toggle_agent(self, session_key: str):
        """
        Add or remove an agent from the active project.
        Called by LeftPanel when user clicks +/− on an agent row.

        Args:
            session_key: Agent session key to add or remove
        """
        if not self._active_project_name:
            return

        members = self._load_members(self._active_project_name)
        if session_key in members:
            members.remove(session_key)
        else:
            members.append(session_key)

        self._save_members(self._active_project_name, members)

        # Rebuild routing table for this project
        self._agent_to_project.remove_project(self._active_project_name)
        for m in members:
            self._agent_to_project.add(m, self._active_project_name)

        # Refresh agents list (+/− button state)
        self._dispatch(lambda: self._lp.refresh_agents_with_project(self._active_project_name))

        # Notify window
        if self._on_members_changed:
            self._on_members_changed(self._active_project_name, members)

    def close_project(self, name: str):
        """
        Close a project: navigate left_panel file_tree back to picker.
        Does NOT close the tab — caller handles that.

        Args:
            name:  Project display name
        """
        if self._active_project_name is None:
            return
        # Capture name BEFORE clearing state — callbacks need the project name
        closing_name = name
        self._active_project_name = None
        self._active_project_path = None
        # Clear routing entries for this project
        self._agent_to_project.remove_project(name)
        self._dispatch(lambda: self._lp.refresh_agents_with_project(None))
        for cb in self._on_project_closed:
            cb(closing_name)

    def get_project_for_session(self, session_key: str) -> str | None:
        """Resolve project name from a session key.

        For project tabs (session_key starts with "project:"), extracts
        the project name directly. For agent sessions, looks up the
        routing table to find which project the agent belongs to.

        Args:
            session_key: Session key to resolve.

        Returns:
            Project name, or None if not associated with any project.
        """
        if session_key.startswith("project:"):
            return session_key.split(":", 1)[1]
        return self._agent_to_project.get_project(session_key)

    def is_project_session(self, session_key: str) -> bool:
        """
        True if session_key belongs to any known project.
        Used by ChatHandler to detect project tabs.
        """
        return self._agent_to_project.is_routed(session_key)

    def get_project_for_agent(self, session_key: str) -> str | None:
        """
        Return the project name this agent belongs to, or None.
        Used by ChatHandler to route responses to the correct project tab.
        """
        return self._agent_to_project.get_project(session_key)

    def get_project_members(self, project_name: str) -> list[str]:
        """
        Return the member session keys for a project.
        Used by ChatHandler for fan-out in project tabs.
        """
        return list(self._load_members(project_name))

    def get_agent_session_in_project(self, project_name: str, agent_name: str) -> str | None:
        """Return the session key that a named agent currently uses in a project.

        Cross-references project members with AgentManager sessions to find
        which of the agent's sessions is active in this project.

        Args:
            project_name:  Project to check.
            agent_name:    Display name of the agent.

        Returns:
            Session key, or None if the agent is not a member of this project.
        """
        members = self.get_project_members(project_name)
        if not self._agent_mgr:
            return None
        agent_sessions = self._agent_mgr.get_sessions(agent_name)
        for sk in members:
            if sk in agent_sessions:
                return sk
        return None

    def update_agent_session(self, project_name: str, old_session_key: str, new_session_key: str) -> None:
        """Replace an agent's session key within a project's member list.

        Updates members.json and the routing table atomically.

        Args:
            project_name:     Project to update.
            old_session_key:  Current session key for the agent.
            new_session_key:  New session key to switch to.
        """
        members = list(self._load_members(project_name))
        if old_session_key not in members:
            return  # Agent not in this project — nothing to do

        # Replace the old session key with the new one
        idx = members.index(old_session_key)
        members[idx] = new_session_key

        # Persist
        self._save_members(project_name, members)

        # Rebuild routing table for this project
        self._agent_to_project.remove_project(project_name)
        for m in members:
            self._agent_to_project.add(m, project_name)

        # Migrate solo target if it was pointing at the old session
        if self._solo_targets.get(project_name) == old_session_key:
            self._solo_targets[project_name] = new_session_key

    def get_active_project_name(self) -> str | None:
        """Return the currently active project name, or None."""
        return self._active_project_name

    # ── Solo DM target (Phase 5 — per-project direct message override) ─────

    @staticmethod
    def _extract_display_name(session_key: str) -> str:
        """Extract a human-readable name from a session key when AgentManager has no mapping.

        Examples:
          'special:tester'    → 'tester'
          'agent:qtr:telegram:direct:123' → 'qtr'
          'agent:qaster:telegram:direct:456' → 'Qaster'
        """
        parts = session_key.split(":")
        if parts[0] == "special" and len(parts) >= 2:
            return parts[1]
        if parts[0] == "agent" and len(parts) >= 2:
            return parts[1]
        return session_key

    def get_solo_target(self, project_name: str) -> str | None:
        """
        Return the solo DM target for a project, or None for group broadcast.

        Args:
            project_name: Name of the project.

        Returns:
            Member session_key if solo mode is active, None if all members receive messages.
        """
        return self._solo_targets.get(project_name)

    def set_solo_target(self, project_name: str, member_session_key: str | None):
        """
        Set or clear the solo DM target for a project.

        Args:
            project_name:          Name of the project (must be an existing project).
            member_session_key:    Session key of the solo recipient, or None to
                                   restore group broadcast (All members).

        Round 3 BUG #3 (Option A — strict): the project must exist. An unknown
        or deleted project name is a no-op (returns None, fires nothing) so a
        stale right-click selection cannot create orphaned solo-target state.
        Fires _on_solo_target_changed(project_name) only when the value actually
        changes (old == new -> no-op) to avoid redundant bar rebuilds.
        """
        if self._get_project_path(project_name) is None:
            return  # unknown project — no-op (BUG #3)
        old = self._solo_targets.get(project_name)
        if old == member_session_key:
            return  # no change — skip redundant callback
        self._solo_targets[project_name] = member_session_key
        if self._on_solo_target_changed is not None:
            self._on_solo_target_changed(project_name)

    # ── Setters for cross-handler callbacks ─────────────────────────────────

    def set_on_project_opened(self, cb: Callable):
        """Add a callback for when a project is opened. Supports multiple callbacks."""
        self._on_project_opened.append(cb)

    def set_on_project_created(self, cb: Callable[[str, str], None]) -> None:
        """Register a callback fired after a project is CREATED (not just opened).

        cb(name: str, path: str). Wired by ui/window.py for the System bubble.
        The callback is invoked AFTER open_project() completes, through the
        handler's GLib.idle_add dispatch so tab creation has completed before
        the callback body runs. Only fires for successful create_project(),
        not for open_project() of an existing project.
        """
        self._on_project_created = cb

    def set_on_project_closed(self, cb: Callable):
        """Add a callback for when a project is closed. Supports multiple callbacks."""
        self._on_project_closed.append(cb)

    def set_on_members_changed(self, cb: Callable):
        """Window calls this to receive membership-change notifications."""
        self._on_members_changed = cb

    def set_on_solo_target_changed(self, cb: Callable[[str], None] | None) -> None:
        """Register a callback fired after set_solo_target() changes.

        cb(project_name: str). Window wires this to refresh the project settings
        bar's agent name. Safe to call with None to unregister, or before any
        project open (no-op until set_solo_target is invoked).
        """
        self._on_solo_target_changed = cb

    def set_agent_manager(self, agent_mgr) -> None:
        """Inject the live AgentManager after gateway connect. Called by window.py."""
        self._agent_mgr = agent_mgr

    def set_clear_callback(self, fn: Callable[[str], bool] | None) -> None:
        """Inject callback for /clear command.

        Spec: docs/specs/STEP-COUNT-RESET-FIX.md Edit 3.

        Wired by window.py to AgentRuntimeHandler.clear_conversation. The
        callback takes a session_key (e.g. "special:coder") and returns
        True on success, False otherwise. cmd_clear invokes this callback
        to reset the in-memory conversation + delete the persisted JSON.

        Trigger: invoked synchronously from cmd_clear (which is called by
        CommandHandler.process_input when a user types /clear in a chat
        tab). set_clear_callback MUST be called before the /clear command
        can succeed.
        """
        self._clear_callback = fn

    def set_clear_chat_callback(self, fn: Callable[[str], None] | None) -> None:
        """Inject callback to empty the chat box after a /clear succeeds.

        Handoff: .crabcakes/handoffs/clear-ui-fix.md.

        Wired by window.py to a closure that resolves the chat box via
        self._main_content.get_chat_box_for_session(sk) and removes all
        its children. The callback takes a session_key (e.g. "special:coder")
        and returns None. cmd_clear invokes this AFTER the data-plane
        clear (self._clear_callback) succeeds, so a UI failure cannot
        roll back the data reset.

        Errors raised by the callback are caught and logged inside
        cmd_clear (the wrapper try/except) so a GTK exception does not
        break the /clear success path.
        """
        self._clear_chat_callback = fn

    def set_runtime_usage_fn(self, fn: Callable[[], dict] | None) -> None:
        """Inject callback for /cost command's in-memory usage cache.

        Spec: docs/specs/SPEC-token-tracking-fix.md AC-4 / AC-5.

        Wired by window.py to AgentRuntimeHandler.get_session_usage. The
        callback takes no arguments and returns a dict mapping
        session_key -> (total_tokens, total_cost) for the current
        process's runtime. cmd_cost uses this as a fallback for gateway
        agents (which do not have persisted conversation files).

        Trigger: invoked synchronously from cmd_cost for every project
        member at most once per /cost invocation. set_runtime_usage_fn
        MUST be called before the /cost command can read gateway-agent
        usage; until then, gateway agents display (0, 0.0).
        """
        self._runtime_usage_fn = fn

    # ── Phase 5: Project Onboarding ─────────────────────────────────────

    def _auto_add_onboarding_agents(self, project_path: str) -> None:
        """Auto-add agents with auto_add_to_projects=True to the project team.

        Called during project creation and first open. These agents serve as
        project onboarding guides — they receive the project-onboarding template
        via compose_system_prompt() when the project is not yet onboarded.
        """
        try:
            from agent.special_agents import get_project_onboarding_agents
            onboarding_agents = get_project_onboarding_agents()
        except Exception:
            return  # Non-fatal — special agents may not be loaded yet

        if not onboarding_agents or not self._awareness:
            return

        team = self._awareness.load_team(project_path)
        changed = False
        for agent_def in onboarding_agents:
            if not team.has_member(agent_def.conv_id_prefix):
                from models.team import TeamMember
                team.add_member(TeamMember(
                    session_key=agent_def.conv_id_prefix,
                    name=agent_def.display_name,
                    role=agent_def.role,
                    can_write=agent_def.can_write,
                ))
                changed = True
                # Also add to routing table if project is active
                if self._active_project_name:
                    self._agent_to_project.add(agent_def.conv_id_prefix, self._active_project_name)

        if changed:
            self._awareness.save_team(project_path, team)
            _logger.info("Auto-added onboarding agents to project at %s", project_path)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _load_members(self, project_name: str) -> list[str]:
        """Load member session keys. Uses awareness module if available, else legacy."""
        path = self._get_project_path(project_name)
        if path and self._awareness:
            team = self._awareness.load_team(path)
            return team.get_session_keys()
        # Fallback to legacy
        if self._projects:
            return list(self._projects.load_members(project_name))
        return []

    def _save_members(self, project_name: str, members: list[str]) -> None:
        """Save member session keys. Uses awareness module if available, else legacy."""
        path = self._get_project_path(project_name)
        if path and self._awareness:
            from models.team import TeamMember
            from agent.special_agents import get_special_agent
            team = self._awareness.load_team(path)
            new_members = []
            for sk in members:
                existing = team.get_member(sk)
                if existing is not None:
                    # Preserve existing member record exactly (order == input)
                    new_members.append(existing)
                elif sk.startswith("special:"):
                    agent_def = get_special_agent(sk)
                    if agent_def is not None:
                        new_members.append(TeamMember(
                            session_key=sk,
                            name=agent_def.display_name,
                            role=agent_def.role,
                            can_write=agent_def.can_write,
                        ))
                    else:
                        _logger.warning("special agent not in registry: %s", sk)
                        new_members.append(TeamMember(session_key=sk, name="", role="", can_write=False))
                else:
                    # Gateway/unknown agents have no local registry definition.
                    new_members.append(TeamMember(session_key=sk, name="", role="", can_write=False))
            team.members = new_members
            self._awareness.save_team(path, team)
            # Refresh awareness snapshot after team save (best-effort, non-fatal).
            # A snapshot failure must not prevent the roster write from having succeeded.
            try:
                snapshot = self._awareness.build_awareness_snapshot(path)
                self._awareness.save_awareness_snapshot(path, snapshot)
            except Exception:
                _logger.exception(
                    "project_handler: awareness snapshot refresh failed for %s; roster save already succeeded",
                    path,
                )
            return
        # Fallback to legacy
        if self._projects:
            self._projects.save_members(project_name, members)

    def _get_project_path(self, project_name: str) -> str | None:
        """Resolve project path from name. Uses cached active path or searches projects."""
        if self._active_project_name == project_name and self._active_project_path:
            return self._active_project_path
        # Search projects list
        if self._projects:
            for name, path in self._projects.load_projects():
                if name == project_name:
                    return path
        return None

    def get_active_project_path(self) -> str | None:
        """Return the currently active project path, or None."""
        return self._active_project_path

    # ── Command entry points (Phase 7) ────────────────────────────────────────


    def cmd_status(self, cmd: Command, session_key: str | None = None) -> CommandResult:
        """/status → project status summary

        Counts Work Units assigned to project members (SPEC-AUDIT-CLEANUP-2
        Phase 2a — migrated from the retired TaskStore). Buckets:
          pending  = status in {draft, spec-pending, spec-ready}
          active   = status in {in-progress, auditing}
          blocked  = blocked_reason non-empty (any non-cancelled status)
          done     = status == done
        Cancelled units are excluded from all counts.
        """
        sk = cmd.source_session_key
        if not sk.startswith("project:"):
            return CommandResult(handled=True, response_text="Open a project tab to check status.")
        project_name = sk.split(":", 1)[1]
        members = self.get_project_members(project_name)
        solo_target = self.get_solo_target(project_name)
        all_units = work_store.list_all()
        project_units = [u for u in all_units if u.assigned_builder in members]
        pending = sum(1 for u in project_units if u.status in ("draft", "spec-pending", "spec-ready"))
        active = sum(1 for u in project_units if u.status in ("in-progress", "auditing"))
        # Cancelled units are excluded from ALL counts, blocked included —
        # a cancelled unit may carry a stale blocked_reason.
        blocked = sum(1 for u in project_units if u.status != "cancelled" and u.blocked_reason)
        done = sum(1 for u in project_units if u.status == "done")
        review_state = self._review_handler.get_state(project_name) if hasattr(self, '_review_handler') and self._review_handler else None
        review_status = "active" if (review_state and review_state.is_active()) else "not started"
        solo_str = f"@{((self._agent_mgr.get_name(solo_target) if self._agent_mgr else "") or self._extract_display_name(solo_target))}" if solo_target else "none"
        lines = [
            f"Project: {project_name}",
            f"Members: {len(members)}",
            f"Work units: {pending} pending, {active} active, {blocked} blocked, {done} done",
            f"Review: {review_status}",
            f"Solo DM: {solo_str}",
        ]
        return CommandResult(handled=True, response_text="\n".join(lines))

    def cmd_agents(self, cmd: Command, session_key: str | None = None) -> CommandResult:
        """/agents → list project agents and their state"""
        sk = cmd.source_session_key
        if not sk.startswith("project:"):
            return CommandResult(handled=True, response_text="Open a project tab to list agents.")
        project_name = sk.split(":", 1)[1]
        members = self.get_project_members(project_name)
        solo_target = self.get_solo_target(project_name)
        lines = [f"Members in {project_name}:", ""]
        for m in members:
            name = (self._agent_mgr.get_name(m) if self._agent_mgr else "") or self._extract_display_name(m)
            solo_marker = " (solo DM target)" if m == solo_target else ""
            lines.append(f"• @{name} — {m}{solo_marker}")
        return CommandResult(handled=True, response_text="\n".join(lines))


    def cmd_cost(self, cmd: Command, session_key: str | None = None) -> CommandResult:
        """/cost — spending summary for current project

        Spec: docs/specs/SPEC-token-tracking-fix.md AC-1/2/3/5.

        Reads each project member's accumulated `total_tokens` and
        `total_cost` from the persisted conversation file (special agents)
        or the in-memory runtime usage cache (gateway agents). Falls back
        to (0, 0.0) when neither source has data.
        """
        sk = cmd.source_session_key
        if not sk.startswith("project:"):
            return CommandResult(handled=True, response_text="Open a project tab to check cost.")
        project_name = sk.split(":", 1)[1]
        members = self.get_project_members(project_name)
        if not members:
            return CommandResult(handled=True, response_text="No members in this project.")

        # Get in-memory usage cache if available (set by window.py wiring)
        mem_usage: dict = {}
        if self._runtime_usage_fn is not None:
            try:
                mem_usage = self._runtime_usage_fn() or {}
            except Exception:
                _logger.exception("cmd_cost: runtime_usage_fn raised; falling back to files only")
                mem_usage = {}

        lines = [
            f"Spending summary for {project_name}:",
            "",
            "Agent      Tokens   Cost",
            "────────────────────────",
        ]
        for member_sk in members:
            name = (self._agent_mgr.get_name(member_sk) if self._agent_mgr else "") or self._extract_display_name(member_sk)
            tokens, cost = self._read_agent_usage(member_sk, mem_usage)
            lines.append(f"  @{name}  {tokens:,} tokens  ${cost:.4f}")
        lines.extend([
            "────────────────────────",
        ])
        return CommandResult(handled=True, response_text="\n".join(lines))

    def _read_agent_usage(self, session_key: str, mem_usage: dict) -> tuple[int, float]:
        """Read token usage for an agent from conversation file or in-memory cache.

        Spec: docs/specs/SPEC-token-tracking-fix.md AC-2/3/5.

        Authoritative source: the persisted conversation file at
        ``<config_dir>/conversations/<session_key>.json`` (which the
        runtime updates via `record_usage` and saves to disk after every
        turn). Used for special:* session keys.

        Fallback: the in-memory ``_session_usage`` dict injected via
        ``set_runtime_usage_fn`` from the runtime handler. Used for
        gateway agents (agent:*) that do not persist conversation files.

        Returns (total_tokens, total_cost). Returns (0, 0.0) when neither
        source is available or both fail to parse — never raises.
        """
        # Authoritative: persisted conversation file
        try:
            from utils.config import get_config_dir
            import json, os
            conv_path = os.path.join(
                get_config_dir(), "conversations", f"{session_key}.json"
            )
            with open(conv_path, encoding="utf-8") as f:
                data = json.load(f)
            tokens = int(data.get("total_tokens", 0) or 0)
            cost = float(data.get("total_cost", 0.0) or 0.0)
            return (tokens, cost)
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, TypeError):
            pass

        # Fallback: in-memory cache (gateway agents)
        if session_key in mem_usage:
            entry = mem_usage[session_key]
            if isinstance(entry, tuple) and len(entry) == 2:
                try:
                    return (int(entry[0]), float(entry[1]))
                except (TypeError, ValueError):
                    pass

        return (0, 0.0)

    def cmd_clear(self, cmd: Command, session_key: str | None = None) -> CommandResult:
        """/clear — reset the current special agent's conversation.

        Spec: docs/specs/STEP-COUNT-RESET-FIX.md Edit 2.

        Clears messages, step_count, total_tokens, total_cost for the agent
        whose tab the user is typing in. Only operates on special agent
        tabs (session_key starts with "special:"). Project tabs are a
        no-op with a hint telling the user to use /clear in an agent tab.
        """
        sk = cmd.target_session_key or cmd.source_session_key or session_key
        if not sk:
            return CommandResult(
                handled=True,
                response_text="No active session to clear.",
            )

        # Project tabs: explain where /clear actually works. We don't
        # clear project-tab state because there isn't a single "the
        # conversation" for a project tab — each member has their own.
        if sk.startswith("project:"):
            return CommandResult(
                handled=True,
                response_text="Use /clear in an agent tab to reset that agent's conversation.",
            )

        # Special agent tabs: dispatch to the runtime handler via the
        # callback injected by window.py.
        if sk.startswith("special:"):
            agent_name = sk.split(":", 1)[1]
            if self._clear_callback is None:
                return CommandResult(
                    handled=True,
                    response_text=(
                        f"Clear unavailable — runtime handler not wired "
                        f"for {agent_name}. Restart the app and try again."
                    ),
                )
            try:
                ok = self._clear_callback(sk)
            except Exception as exc:
                _logger.exception("cmd_clear: callback raised for %s", sk)
                return CommandResult(
                    handled=True,
                    response_text=f"Clear failed for {agent_name}: {exc}",
                )
            if ok:
                # UI side effect: empty the chat box for this session so the
                # user doesn't see stale bubbles after the model was reset.
                # Wrapped in try/except so a GTK failure cannot roll back the
                # data-plane clear that already succeeded above.
                if self._clear_chat_callback is not None:
                    try:
                        self._clear_chat_callback(sk)
                    except Exception:
                        _logger.exception(
                            "cmd_clear: clear_chat_callback raised for %s; "
                            "data-plane clear already succeeded, continuing",
                            sk,
                        )
                return CommandResult(
                    handled=True,
                    response_text=(
                        f"Cleared {agent_name}'s conversation. "
                        f"Step count reset to 0."
                    ),
                )
            else:
                return CommandResult(
                    handled=True,
                    response_text=(
                        f"Could not clear {agent_name}: a tool loop is currently running. "
                        f"Wait for it to finish, then run /clear again."
                    ),
                )

        # Unknown session prefix — refuse cleanly.
        return CommandResult(
            handled=True,
            response_text=f"Cannot clear session of type '{sk.split(':', 1)[0]}'.",
        )

    def cmd_compact(self, cmd: Command, session_key: str | None = None) -> CommandResult:
        """/compact — force compaction of the current special agent's conversation.

        Spec: docs/specs/SPEC-CONTEXT-UI-COMPACT-LLM-2026-07-10.md §3.2.

        Forces a compact(conv, model_max // 2) call regardless of current
        size. Mirrors cmd_clear's structure: validate session, dispatch
        to injected callback.

        Optional body text (cmd.body) is passed to the callback as a focus
        instruction. Phase B (textual summary) ignores it; Phase C (LLM)
        includes it in the LLM prompt.

        Refuses to operate on project tabs (each member has its own
        conversation; compacting one would surprise the user).
        """
        sk = cmd.target_session_key or cmd.source_session_key or session_key
        if not sk:
            return CommandResult(
                handled=True,
                response_text="No active session to compact.",
            )

        if sk.startswith("project:"):
            return CommandResult(
                handled=True,
                response_text=(
                    "Use /compact in an agent tab to compact that agent's "
                    "conversation."
                ),
            )

        if sk.startswith("special:"):
            agent_name = sk.split(":", 1)[1]
            if self._compact_callback is None:
                return CommandResult(
                    handled=True,
                    response_text=(
                        f"Compact unavailable — runtime handler not wired "
                        f"for {agent_name}. Restart the app and try again."
                    ),
                )
            focus_text = (cmd.body or "").strip()
            try:
                result = self._compact_callback(sk, focus_text)
            except Exception as exc:
                _logger.exception("cmd_compact: callback raised for %s", sk)
                return CommandResult(
                    handled=True,
                    response_text=f"Compact failed for {agent_name}: {exc}",
                )
            removed = int(result.get("messages_removed", 0))
            freed = int(result.get("tokens_freed", 0))
            msg = (
                f"Compacted {agent_name}'s conversation. "
                f"Removed {removed} message"
                f"{'s' if removed != 1 else ''}, freed ~{freed:,} tokens."
            )
            if focus_text:
                msg += f"\nFocus: {focus_text!r}"

            if self._compact_chat_callback is not None:
                try:
                    self._compact_chat_callback(sk, result)
                except Exception:
                    _logger.exception(
                        "cmd_compact: chat_callback raised for %s; "
                        "data-plane compact already succeeded, continuing",
                        sk,
                    )

            return CommandResult(handled=True, response_text=msg)

        return CommandResult(
            handled=True,
            response_text=f"Cannot compact session of type '{sk.split(':', 1)[0]}'.",
        )

    def set_compact_callback(self, fn: Callable[[str, str], dict] | None) -> None:
        """Inject callback for /compact command.

        Phase B. Wired by window.py to AgentRuntimeHandler.compact_conversation.
        The callback takes (session_key, focus_text) and returns a dict:
            {"messages_removed": int, "tokens_freed": int, "summary_chars": int}
        MUST be called before /compact can succeed. None → no-op with hint.
        """
        self._compact_callback = fn

    def set_compact_chat_callback(self, fn: Callable[[str, dict], None] | None) -> None:
        """Inject callback for /compact UI side effect.

        Optional. Wired by window.py to insert a "🧹 Compacted" bubble
        into the chat box of ``session_key`` after the data-plane compact
        succeeds. Args: (session_key: str, result: dict) → None.
        """
        self._compact_chat_callback = fn

    def set_review_handler(self, review_handler) -> None:
        """"Inject ReviewHandler for review state queries in cmd_status."""
        self._review_handler = review_handler

    def get_review_state(self, project_name: str):
        """Get the ReviewState for a project, or None if no active review.

        Delegates to ReviewHandler.get_state(). Returns None if the review
        handler hasn't been wired yet (e.g. during startup or in tests).
        """
        if not hasattr(self, '_review_handler') or self._review_handler is None:
            return None
        return self._review_handler.get_state(project_name)

    def revert_file_to_sha(self, project_name: str, file_path: str, target_sha: str,
                           on_complete: Callable[[], None] | None = None) -> None:
        """Delegate file revert to ReviewHandler.

        Requires a wired review handler; no-op otherwise.
        on_complete: Forwarded to ReviewHandler.revert_file_to_sha().

        BUG #12: Reject file paths starting with '--' to prevent argument
        injection in GitPython's git.checkout() subprocess call.
        """
        if not hasattr(self, '_review_handler') or self._review_handler is None:
            return
        if not isinstance(file_path, str) or file_path.startswith("-"):
            return
        self._review_handler.revert_file_to_sha(project_name, file_path, target_sha,
                                                  on_complete=on_complete)

    # ── Git commit helper ───────────────────────────────────────────────

    def _git_commit_if_available(self, path: str, message: str) -> None:
        """Stage all and commit if git is available. Non-fatal on failure."""
        try:
            stage_all(path)
            commit(path, message)
        except Exception:
            _logger.debug("git commit failed for %s: %s", path, message)

    def _dispatch(self, fn: Callable):
        """Call fn on the GTK main thread. Direct call if GLib not available."""
        if self._GLib is not None:
            def _wrap():
                fn()
                return False
            self._GLib.idle_add(_wrap)
        else:
            fn()
