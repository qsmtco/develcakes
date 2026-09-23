# utils/agent_defs.py
# Agent definition file I/O for user-defined local agents.
#
# Manifest:
#   - Reads/writes YAML and JSON files from <config_dir>/agents/
#   - Validates required fields, tool names, prompt file existence
#   - No GTK, no network
#
# Architecture: pure utility following utils/projects.py pattern.
# Path resolution uses utils/config.get_config_dir().
# One exception: get_available_tools() imports agent/tools.py for
# tool metadata — a read-only call for UI dropdown population.

from __future__ import annotations

import json
import logging
import os
import shutil
from typing import Any

logger = logging.getLogger(__name__)

# ── Path helpers ──────────────────────────────────────────────────────────────


def _get_agents_dir() -> str:
    """Return the agent definitions directory."""
    from utils.config import get_config_dir
    return os.path.join(get_config_dir(), "agents")


def _normalize_fallback_fields(data: dict) -> None:
    """Ensure fallback_provider key exists in the agent def dict.

    Reads from YAML/JSON if present, defaults to None if absent.
    Called after parsing every agent definition file.

    Note: fallback_model was removed on 2026-06-15 (see
    SPEC-AGENT-FALLBACK-MODEL-DROPDOWN-REMOVAL.md). Old YAMLs with
    fallback_model retain the key in the loaded dict, but it is ignored
    by the runtime.
    """
    if "fallback_provider" not in data:
        data["fallback_provider"] = None


def _get_default_agents_src() -> str:
    """Return the source directory for built-in default agent YAML files."""
    # prompts/default_agents/ ships with CrabCakes
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts", "default_agents")


# ── Parsing ──────────────────────────────────────────────────────────────────


def _parse_agent_file(filepath: str) -> dict | None:
    """Parse a single agent definition file (YAML or JSON).

    Tries YAML first, falls back to JSON if pyyaml is not installed.
    Returns None on any parse failure.
    """
    # Try YAML first
    if filepath.endswith((".yaml", ".yml")):
        try:
            import yaml
            with open(filepath, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                _normalize_fallback_fields(data)
                return data
            logger.warning("Agent file %s did not contain a mapping — skipping", filepath)
            return None
        except ImportError:
            # pyyaml not installed — try JSON fallback for .yaml/.yml files
            # only if the file is also valid JSON
            try:
                with open(filepath, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    logger.info(
                        "pyyaml not installed — parsed %s as JSON fallback", filepath
                    )
                    _normalize_fallback_fields(data)
                    return data
            except (json.JSONDecodeError, OSError):
                pass
            logger.warning(
                "pyyaml not installed — cannot parse %s. Install with: pip install pyyaml",
                filepath,
            )
            return None

    # JSON files
    if filepath.endswith(".json"):
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _normalize_fallback_fields(data)
                return data
            logger.warning("Agent file %s did not contain a mapping — skipping", filepath)
            return None
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Failed to parse agent file %s: %s", filepath, e)
            return None

    return None


def _derive_role(agent_def: dict) -> str:
    """Derive the role identifier from an agent definition.

    Uses explicit 'role' field if present. Otherwise derives from 'name':
    lowercase, spaces replaced with hyphens.
    """
    if agent_def.get("role"):
        return agent_def["role"].lower().strip()
    name = agent_def.get("name", "agent")
    return name.lower().replace(" ", "-")


# ── Default seeding ──────────────────────────────────────────────────────────


def _seed_defaults() -> None:
    """Copy any missing built-in default agent YAML files to the agents dir.

    Only copies files from prompts/default_agents/ that don't already exist
    in <config_dir>/agents/. Never overwrites existing user files.
    """
    agents_dir = _get_agents_dir()
    src_dir = _get_default_agents_src()

    if not os.path.isdir(src_dir):
        return

    try:
        os.makedirs(agents_dir, exist_ok=True)
    except OSError as e:
        logger.warning("Cannot create agents directory %s: %s", agents_dir, e)
        return

    # Per-file seeding: copy each missing built-in default into the user
    # agents dir. Unrelated user agent files do not suppress seeding of
    # missing built-ins. The per-file isfile(dst) guard below ensures we
    # never overwrite an existing user file (including a customized
    # supervisor.yaml).
    for fname in sorted(os.listdir(src_dir)):
        if fname.endswith((".yaml", ".yml", ".json")):
            src = os.path.join(src_dir, fname)
            dst = os.path.join(agents_dir, fname)
            if not os.path.isfile(dst):
                try:
                    shutil.copy2(src, dst)
                    logger.info("Seeded default agent definition: %s", fname)
                except OSError as e:
                    logger.warning("Failed to seed default agent %s: %s", fname, e)


# ── Public API ────────────────────────────────────────────────────────────────


def get_default_si_config(can_write: bool = False) -> dict:
    """Return the canonical self-improvement defaults dict.

    Single source of truth — used by prompt_loader.py, feedback_processor.py,
    and SpecialAgentDef.get_self_improvement_config().

    Args:
        can_write: Whether the agent has write tools. Affects enforcement default.

    Returns:
        Dict with all self_improvement keys and their default values.
    """
    return {
        "bug_journal": True,
        "project_rules": True,
        "enforcement": can_write,
        "structured_feedback": False,
        "dream_consolidation": False,
    }


def load_agent_defs() -> list[dict]:
    """Scan ~/.config/crabcakes/agents/ for definition files. Parse and validate.

    Seeds built-in defaults (Coder, Debugger) if directory is empty.
    Returns list of agent definition dicts. Empty list if dir missing.
    Skips files that fail to parse.
    """
    _seed_defaults()

    agents_dir = _get_agents_dir()
    if not os.path.isdir(agents_dir):
        return []

    # Scan in deterministic order: .yaml first, then .yml, then .json
    filenames: list[str] = []
    for fname in sorted(os.listdir(agents_dir)):
        if fname.endswith((".yaml", ".yml", ".json")):
            filenames.append(fname)

    # Sort: .yaml before .yml before .json, then alphabetically
    ext_order = {".yaml": 0, ".yml": 1, ".json": 2}
    filenames.sort(key=lambda f: (ext_order.get(os.path.splitext(f)[1], 3), f))

    defs: list[dict] = []
    seen_names: set[str] = set()
    for fname in filenames:
        filepath = os.path.join(agents_dir, fname)
        agent_def = _parse_agent_file(filepath)
        if agent_def is None:
            continue

        # Ensure role field is populated
        if "role" not in agent_def:
            agent_def["role"] = _derive_role(agent_def)

        # LOW-11: validate at load time; skip invalid defs with a WARNING
        errors = validate_agent_def(agent_def)
        # Role-aware exemptions ---
        # (a) helper: llm_name/fallback_provider may be empty — helper-role
        #     agents resolve to the global default provider at runtime.
        #     All other validation (tools, prompts, unknown providers) stays strict.
        # (b) all roles: mcp_servers string values are tolerated here because
        #     _load_registry in agent/special_agents.py handles the coercion
        #     (BUG #30: single-string → list).  The validation error for non-list
        #     types is still filtered so the coercion can run.
        if agent_def.get("role") == "helper":
            # Helper-role agents are not required to name a provider.
            errors = [e for e in errors if "llm_name" not in e and "fallback_provider" not in e]
        errors = [e for e in errors if not (
            e == "Field 'mcp_servers' must be a list" and
            agent_def.get("mcp_servers") is not None and
            not isinstance(agent_def.get("mcp_servers"), list)
        )]
        if errors:
            logger.warning(
                "LOW-11: skipping invalid agent def %s (%s): %s",
                fname, agent_def.get("name", "?"), "; ".join(errors),
            )
            continue

        # Track the source file
        agent_def["_source_file"] = fname

        # Deduplicate by name (first file wins)
        name = agent_def.get("name", "")
        if name and name in seen_names:
            logger.warning(
                "Duplicate agent name '%s' in %s — skipping (first definition wins)",
                name, fname,
            )
            continue
        if name:
            seen_names.add(name)

        defs.append(agent_def)

    return defs


def load_agent_def(name: str) -> dict | None:
    """Load a single agent definition by display name.

    Returns the first matching definition, or None if not found.
    """
    for agent_def in load_agent_defs():
        if agent_def.get("name") == name:
            return agent_def
    return None


def load_agent_def_by_role(role: str) -> dict | None:
    """Load an agent definition by its role field.

    Used by self-improvement code to look up config by role identifier.
    Returns None if not found.
    """
    role_lower = role.lower()
    for agent_def in load_agent_defs():
        if agent_def.get("role", "").lower() == role_lower:
            return agent_def
    return None


def save_agent_def(agent_def: dict) -> str:
    """Write an agent definition to ~/.config/crabcakes/agents/<name>.yaml.

    Creates directory if needed. Returns file path.
    Uses YAML if pyyaml is available, otherwise JSON.
    """
    agents_dir = _get_agents_dir()
    os.makedirs(agents_dir, exist_ok=True)

    name = agent_def.get("name", "unnamed-agent")
    # Sanitize filename: lowercase, replace spaces with hyphens
    safe_name = name.lower().replace(" ", "-")
    # Remove any non-alphanumeric characters except hyphens and underscores
    safe_name = "".join(c for c in safe_name if c.isalnum() or c in "-_")

    filepath = os.path.join(agents_dir, f"{safe_name}.yaml")

    # Try YAML first
    try:
        import yaml
        # Build a clean copy without internal keys
        export = {k: v for k, v in agent_def.items() if not k.startswith("_")}

        # Preserve fields not controlled by the Agent Builder UI.
        # The form doesn't send auto_open or auto_add_to_projects,
        # so editing an agent through the UI would strip them. Merge from existing file.
        _PRESERVED_KEYS = {"auto_open", "auto_add_to_projects"}
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as ef:
                    existing = yaml.safe_load(ef) or {}
                for key in _PRESERVED_KEYS:
                    if key not in export and key in existing:
                        export[key] = existing[key]
            except Exception:
                pass  # If we can't read existing, just write what we have

        with open(filepath, "w", encoding="utf-8") as f:
            yaml.dump(export, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return filepath
    except ImportError:
        pass

    # Fallback to JSON
    filepath = os.path.join(agents_dir, f"{safe_name}.json")
    export = {k: v for k, v in agent_def.items() if not k.startswith("_")}
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(export, f, indent=2, ensure_ascii=False)
    return filepath


def delete_agent_def(name: str) -> bool:
    """Delete an agent definition file by name.

    Finds the file by scanning all definitions, then removes it.
    Returns True if a file was deleted.
    """
    agents_dir = _get_agents_dir()
    if not os.path.isdir(agents_dir):
        return False

    for fname in os.listdir(agents_dir):
        if not fname.endswith((".yaml", ".yml", ".json")):
            continue
        filepath = os.path.join(agents_dir, fname)
        agent_def = _parse_agent_file(filepath)
        if agent_def and agent_def.get("name") == name:
            try:
                os.remove(filepath)
                logger.info("Deleted agent definition: %s (%s)", name, fname)
                return True
            except OSError as e:
                logger.warning("Failed to delete agent file %s: %s", filepath, e)
                return False
    return False


VALID_COMPACTION_STRATEGIES: tuple[str, ...] = ("textual", "llm")


def validate_agent_def(agent_def: dict) -> list[str]:
    """Validate an agent definition dict.

    Checks required fields, prompt file existence, tool name validity.
    Returns list of error strings (empty if valid).
    """
    errors: list[str] = []

    # Required fields
    if not agent_def.get("name"):
        errors.append("Missing required field: name")
    if not agent_def.get("prompts"):
        errors.append("Missing required field: prompts (must be a non-empty list)")
    if not agent_def.get("tools"):
        errors.append("Missing required field: tools (must be a non-empty list)")
    if not agent_def.get("llm_name"):
        errors.append("Missing required field: llm_name")
    if not agent_def.get("fallback_provider"):
        errors.append("Missing required field: fallback_provider (every agent must have a fallback)")

    # Type checks
    prompts = agent_def.get("prompts")
    if prompts is not None and not isinstance(prompts, list):
        errors.append("Field 'prompts' must be a list")
    if prompts is not None and isinstance(prompts, list):
        for p in prompts:
            if not isinstance(p, str):
                errors.append(f"Prompt entry must be a string, got: {type(p).__name__}")
                break

    tools = agent_def.get("tools")
    if tools is not None and not isinstance(tools, list):
        errors.append("Field 'tools' must be a list")

    # Validate tool names against known tools
    if isinstance(tools, list) and tools:
        known_names = {t["name"] for t in get_available_tools()}
        for tool_name in tools:
            if tool_name not in known_names:
                errors.append(f"Unknown tool: {tool_name}")

    # Validate prompt files exist
    if isinstance(prompts, list):
        prompts_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts")
        for p in prompts:
            if isinstance(p, str):
                # Check prompts/ directory (could be system/ or root)
                candidates = [
                    os.path.join(prompts_dir, p),
                    os.path.join(prompts_dir, "system", p),
                ]
                if not any(os.path.isfile(c) for c in candidates):
                    errors.append(f"Prompt file not found: {p}")

    # Validate llm_name exists in providers.yaml
    llm_name = agent_def.get("llm_name")
    if llm_name:
        providers = get_available_providers()
        valid_ids = set()
        display_names = set()
        for p in providers:
            display_names.add(p["name"])
            valid_ids.add(p["name"])
            if p.get("default_model") and "/" in p["default_model"]:
                valid_ids.add(p["default_model"].split("/")[0])
        if display_names and llm_name not in valid_ids:
            errors.append(
                f"Unknown provider: {llm_name}. Available: {', '.join(sorted(display_names))}"
            )

    # Per Phase B: API keys are validated at config time (Test Connection in Settings),
    # not at agent-def-save time. The agent YAML stores provider+model only.

    # Check for filename collision (sanitized names may collide)
    name = agent_def.get("name", "")
    if name:
        safe_name = name.lower().replace(" ", "-")
        safe_name = "".join(c for c in safe_name if c.isalnum() or c in "-_")
        agents_dir = _get_agents_dir()
        if os.path.isdir(agents_dir):
            for fname in os.listdir(agents_dir):
                if fname.endswith((".yaml", ".yml", ".json")):
                    stem = os.path.splitext(fname)[0]
                    if stem == safe_name:
                        # Check if this file belongs to a different agent
                        existing = _parse_agent_file(os.path.join(agents_dir, fname))
                        if existing and existing.get("name") != name:
                            errors.append(
                                f"Name collision: '{name}' and '{existing.get('name')}' "
                                f"would both write to {fname}. Choose a different name."
                            )
                        break

    # BUG #30: Validate mcp_servers field
    mcp_servers = agent_def.get("mcp_servers")
    if mcp_servers is not None and not isinstance(mcp_servers, list):
        errors.append("Field 'mcp_servers' must be a list")
    elif isinstance(mcp_servers, list):
        for name in mcp_servers:
            if not isinstance(name, str):
                errors.append("mcp_servers entries must be strings")
            elif "/" in name or any(c.isspace() for c in name):
                errors.append(f"Invalid MCP server name '{name}': must not contain '/' or whitespace")

    # Phase C — Validate compaction_strategy
    cs = agent_def.get("compaction_strategy", "textual")
    if not isinstance(cs, str) or cs not in VALID_COMPACTION_STRATEGIES:
        errors.append(
            f"Invalid compaction_strategy: {cs!r}. "
            f"Must be one of: {', '.join(VALID_COMPACTION_STRATEGIES)}."
        )

    return errors


def get_available_tools() -> list[dict]:
    """Return available tool names and descriptions.

    Wraps agent/tools.py get_all_tools() → [{name, description}].
    Used by the UI to show tool checkboxes.
    """
    try:
        from agent.tools import get_all_tools
        return [{"name": t.name, "description": t.description} for t in get_all_tools()]
    except ImportError:
        logger.warning("Cannot import agent.tools — returning empty tool list")
        return []


def get_available_prompts() -> list[dict]:
    """Scan prompts/ directory for .md files → [{name, filepath}].

    Used by the UI to show prompt selector.
    """
    prompts_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts")
    if not os.path.isdir(prompts_dir):
        return []

    results: list[dict] = []

    # Scan prompts/system/ first (these are the main system prompts)
    system_dir = os.path.join(prompts_dir, "system")
    if os.path.isdir(system_dir):
        for fname in sorted(os.listdir(system_dir)):
            if fname.endswith(".md"):
                filepath = os.path.join(system_dir, fname)
                if os.path.isfile(filepath):
                    results.append({
                        "name": fname,
                        "filepath": os.path.join("system", fname),
                    })

    # Also scan prompts/ root level
    for fname in sorted(os.listdir(prompts_dir)):
        if fname.endswith(".md") and os.path.isfile(os.path.join(prompts_dir, fname)):
            results.append({
                "name": fname,
                "filepath": fname,
            })

    return results


def get_available_providers() -> list[dict]:
    """Load providers from providers.yaml → [{name, base_url, default_model}].

    Used by the UI to show provider dropdown. Returns empty list when no
    providers.yaml exists or it's empty (first-run state).
    """
    try:
        from utils.providers_store import load_providers
        return [
            {
                "name": p.name,
                "base_url": p.base_url,
                "default_model": p.default_model,
            }
            for p in load_providers()
        ]
    except Exception as e:
        logger.debug("Cannot load providers.yaml: %s", e)
        return []


