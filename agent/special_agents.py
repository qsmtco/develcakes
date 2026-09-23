# agent/special_agents.py
# Special agent registry for the agent runtime.
#
# Loads agent definitions from ~/.config/crabcakes/agents/*.yaml (or .json).
# Built-in defaults (Coder, Debugger) are seeded from prompts/default_agents/
# on first launch.
#
# System prompts are loaded from prompts/system/{role}.md via prompt_loader.
#
# Adding a new special agent: create a YAML file in the agents config dir,
# or use the Agent Builder UI. No code changes needed — EXCEPT when the new
# agent shares a `role` with an existing one. The session key defaults to
# "special:<role>", so same-role agents collide here and one replaces the
# other. `role` also selects the prompt template (agent/context.py
# build_system_prompt), so it cannot be changed to dodge the collision —
# declare an explicit `session_key:` in that agent's YAML instead.

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

__all__ = ["SPECIAL_AGENTS", "SpecialAgentDef", "get_special_agents", "get_special_agent", "unregister_special_agent", "get_auto_open_agents", "get_project_onboarding_agents"]


@dataclass
class SpecialAgentDef:
    """Definition of a Crabcakes Special Agent.

    Can be loaded from YAML/JSON config files or created programmatically.
    Fields llm_name and self_improvement are optional overrides —
    when None, the global agent config defaults apply.
    """
    conv_id_prefix: str           # e.g. "special:coder" — used as session_key
    display_name: str             # e.g. "Coder"
    role: str                     # e.g. "coder" — matches prompts/system/{role}.md
    emoji: str                    # e.g. "🛠️"
    tools: list[str]              # tool names this agent can use
    can_write: bool               # whether write_file is in the default tool set
    llm_name: str | None = None   # per-agent provider card name (None → global default)
    fallback_provider: str | None = None   # plain provider fallback (user-set, e.g. "openrouter")
    fallback_model: str | None = None      # plain provider fallback model (e.g. "openrouter/owl-alpha")
    api_key: str | None = None    # per-agent API key override (None → provider config)
    app_title: str | None = None   # OpenRouter X-Title header (e.g. "Coder:Crabcakes")
    self_improvement: dict = field(default_factory=dict)  # SI layer toggles
    mcp_servers: list[str] = field(default_factory=list)  # MCP servers for Phase B
    auto_open: bool = False           # open tab automatically on every app launch
    auto_add_to_projects: bool = False  # auto-add to every new project's team
    compaction_strategy: str = "textual"  # Phase C — "textual" | "llm"

    def get_self_improvement_config(self) -> dict:
        """Return self_improvement config with defaults applied.

        Delegates to utils.agent_defs.get_default_si_config() for the
        canonical defaults. The YAML-level self_improvement overrides
        the defaults.
        """
        from utils.agent_defs import get_default_si_config
        defaults = get_default_si_config(can_write=self.can_write)
        return {**defaults, **self.self_improvement}




# ── Agent registry ───────────────────────────────────────────────────────────

# Lazy-loaded from YAML/JSON config files. None means "not yet loaded".
SPECIAL_AGENTS: dict[str, SpecialAgentDef] | None = None


def _load_registry() -> dict[str, SpecialAgentDef]:
    """Load agent definitions from config files and build the registry."""
    from utils.agent_defs import load_agent_defs

    defs = load_agent_defs()
    registry: dict[str, SpecialAgentDef] = {}

    for agent_def in defs:
        role = agent_def.get("role", "").lower()
        name = agent_def.get("name", "Agent")
        # Session key resolution. Default is derived from the role (legacy
        # behaviour, preserved so existing keys and conversations are stable).
        # Because the key is role-derived, two agents that share a role collide
        # and the later one silently replaces the earlier one. The `role` cannot
        # simply be changed to fix that — it selects the prompt template in
        # agent/context.py build_system_prompt(). Such an agent needs an
        # explicit `session_key:` in its YAML instead.
        session_key = str(agent_def.get("session_key") or "").strip() or f"special:{role}"
        tools = agent_def.get("tools", [])

        # BUG #30: Coerce mcp_servers to list if YAML gave a string
        raw_mcp = agent_def.get("mcp_servers", [])
        if isinstance(raw_mcp, str):
            raw_mcp = [raw_mcp]  # Coerce single string to list
        elif not isinstance(raw_mcp, list):
            raw_mcp = []  # Invalid type → treat as empty

        if session_key in registry:
            logger.warning(
                "Duplicate session key %r — agent %r replaces %r. Two agents "
                "may not share a role unless one declares an explicit "
                "'session_key' in its YAML.",
                session_key, name, registry[session_key].display_name,
            )

        registry[session_key] = SpecialAgentDef(
            conv_id_prefix=session_key,
            display_name=name,
            role=role,
            emoji=agent_def.get("emoji", "🤖"),
            tools=tools,
            can_write="write_file" in tools or "edit_file" in tools,
            llm_name=agent_def.get("llm_name"),
            fallback_provider=agent_def.get("fallback_provider"),
            fallback_model=agent_def.get("fallback_model"),
            # Per Phase B: keys are resolved from providers.yaml at runtime, not stored on the agent.
            api_key=None,
            app_title=agent_def.get("app_title"),
            self_improvement=agent_def.get("self_improvement", {}),
            mcp_servers=raw_mcp,  # Phase B: MCP server list (coerced)
            auto_open=agent_def.get("auto_open", False),
            auto_add_to_projects=agent_def.get("auto_add_to_projects", False),
            compaction_strategy=agent_def.get("compaction_strategy", "textual"),
        )

    return registry


def _ensure_loaded() -> dict[str, SpecialAgentDef]:
    """Ensure the registry is loaded, then return it."""
    global SPECIAL_AGENTS
    if SPECIAL_AGENTS is None:
        SPECIAL_AGENTS = _load_registry()
        logger.info("Loaded %d agent definitions", len(SPECIAL_AGENTS))
    return SPECIAL_AGENTS


def reload_registry() -> None:
    """Force reload the registry from config files.

    Call after creating/editing/deleting agent definitions.
    """
    global SPECIAL_AGENTS
    SPECIAL_AGENTS = None
    _ensure_loaded()


def get_special_agents() -> list[SpecialAgentDef]:
    """Return all special agent definitions, in display order."""
    return list(_ensure_loaded().values())


def get_special_agent(prefix: str) -> SpecialAgentDef | None:
    """Look up a special agent by its session key prefix."""
    return _ensure_loaded().get(prefix)


def unregister_special_agent(prefix: str) -> bool:
    """Remove a special agent from the registry by its session key prefix.

    Args:
        prefix: The agent's conv_id_prefix (e.g. "special:coder").

    Returns:
        True if the agent was found and removed, False otherwise.
    """
    registry = _ensure_loaded()
    if prefix in registry:
        del registry[prefix]
        logger.info("special_agents: unregistered agent %s", prefix)
        return True
    return False


def get_auto_open_agents() -> list[SpecialAgentDef]:
    """Return all agents with auto_open=True.

    Used by window.py at startup to open tabs for agents that should
    be present on every launch.
    """
    return [agent for agent in get_special_agents() if agent.auto_open]


def get_project_onboarding_agents() -> list[SpecialAgentDef]:
    """Return all agents with auto_add_to_projects=True.

    Used by ProjectHandler when creating or opening a project to
    auto-add agents to the team roster.
    """
    return [agent for agent in get_special_agents() if agent.auto_add_to_projects]
