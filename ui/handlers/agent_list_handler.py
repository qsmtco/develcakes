# ui/handlers/agent_list_handler.py
# Agent card logic — initials, color assignment, sorting, button click delegation.
#
# Thread safety: all operations are synchronous; no background threads.
#
# Rules (Section 8.6):
#   - Does NOT import other handlers
#   - Receives AgentManager via constructor
#   - All GTK calls stay in the view layer (left_panel.py)

from typing import Callable


class AgentListHandler:
    """
    Owns agent card rendering data — initials, colors, grouping.

    Does NOT build widgets — left_panel.py calls handler methods to get
    rendering data, then builds the GTK widgets.
    """

    def __init__(
        self,
        *,
        agent_mgr=None,
        on_agent_chat: Callable = None,
        on_agent_toggle: Callable = None,
    ):
        """
        Args:
            agent_mgr:     AgentManager instance (from gateway_handler)
            on_agent_chat: callback(session_key, name) — Chat button clicked
            on_agent_toggle: callback(session_key, name, in_project) — +/− clicked
        """
        self._agent_mgr = agent_mgr
        self._on_agent_chat = on_agent_chat
        self._on_agent_toggle = on_agent_toggle

    # ── Public API ─────────────────────────────────────────────────────────

    def set_agent_mgr(self, agent_mgr):
        """Set or replace the AgentManager."""
        self._agent_mgr = agent_mgr

    def has_agent_mgr(self) -> bool:
        """True if AgentManager is set and populated."""
        return self._agent_mgr is not None

    def compute_initials(self, name: str) -> str:
        """
        Derive 2-letter initials from an agent name.
        Uses first letter of first two words, or first two chars if single word.
        """
        parts = name.split()
        if len(parts) >= 2:
            return (parts[0][0] + parts[1][0]).upper()
        return name[:2].upper()

    def get_agent_color(self, name: str) -> str:
        """
        Get hex color for an agent name.

        Priority:
          1. Live agent registered in AgentManager (gateway path).
          2. Special agent role lookup — returns the same color across reloads.
          3. Deterministic default "#6366f1" — never advances a counter.

        The special-agent path looks up the role from the registry by display
        name. Unknown names return the deterministic default without calling
        any counter-advancing function.
        """
        if self._agent_mgr is not None:
            color = self._agent_mgr.get_color(name)
            if color:
                return color
        # Special agent path — find the role for this display name.
        # NOTE: The `display_name == name` comparison is case-sensitive and
        # exact-match. Renaming a special agent's `name:` in its YAML will
        # cause the lookup to miss → the handler returns the deterministic
        # default "#6366f1" instead of the cached color. This is by design
        # (cache is keyed by role, not display_name), but worth noting for
        # operators editing ~/.config/develcakes/agents/*.yaml.
        from agent.special_agents import get_special_agents
        from models.colors import color_for_special_agent
        for agent_def in get_special_agents():
            if agent_def.display_name == name:
                return color_for_special_agent(agent_def.role)
        # Unknown name — deterministic default, no counter advance
        return "#6366f1"

    def get_sorted_agents(self, project_members=None) -> list[tuple[str, str, bool, int]]:
        """
        Return [(session_key, name, in_project, session_count)] for all agents.

        One row per UNIQUE agent name (not one per session). When an agent has
        multiple sessions, the :main session is used as the primary key — the
        session switcher (right-click → SessionMenu) handles the other sessions.
        project_members: list of session_keys currently in the project (for +/− state)
        session_count: number of active sessions for this agent name

        Returns:
            List of (session_key, name, in_project, session_count) tuples.
        """
        if self._agent_mgr is None:
            return []

        agents = {}  # name -> primary session_key
        for session_key, name in self._agent_mgr._agent_names.items():
            if name not in agents:
                agents[name] = session_key
            if ":main" in session_key:
                agents[name] = session_key

        result = []
        for name, sk in agents.items():
            in_project = bool(project_members) and (
                sk in project_members or
                any(s in project_members for s in self._agent_mgr.get_sessions(name))
            )
            session_count = len(self._agent_mgr.get_sessions(name))
            result.append((sk, name, in_project, session_count))

        return result

    def on_chat_clicked(self, session_key: str, name: str):
        """Handle Chat button click — delegate to registered callback."""
        if self._on_agent_chat:
            self._on_agent_chat(session_key, name)

    def on_toggle_clicked(self, session_key: str, name: str, in_project: bool):
        """Handle +/− button click — delegate to registered callback."""
        if self._on_agent_toggle:
            self._on_agent_toggle(session_key, name, in_project)

    def get_all_sessions_for_agent(self, name: str) -> list[str]:
        """Return all session_keys for a given agent name (public API)."""
        if self._agent_mgr is None:
            return []
        return self._agent_mgr.get_sessions(name)

    def get_primary_session(self, name: str) -> str | None:
        """Return canonical session key for an agent name (delegates to AgentManager)."""
        if self._agent_mgr is None:
            return None
        return self._agent_mgr.get_primary_session(name)
