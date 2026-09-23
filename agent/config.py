# agent/config.py
# LLM provider configuration for the agent runtime.
#
# Manifest:
#   - Reads <config_dir>/agent.json on load
#   - Validates file permissions (warns if group/world-readable)
#   - No network, no GTK
#
# Architecture: agent/ specific config — API keys and model selection.
# Path resolution uses utils/config.get_config_dir() — never hardcoded paths.

from __future__ import annotations

import dataclasses
import json
import logging
import os
import stat
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Dataclasses ────────────────────────────────────────────────────────────────


@dataclass
class LLMProviderConfig:
    """Configuration for a single LLM API provider."""
    name: str
    base_url: str
    api_key: str
    default_model: str
    caller: str = ""                    # API caller key (openai|minimax|anthropic|openrouter|zai)
    supports_tools: bool = True
    supports_streaming: bool = True
    max_tokens: int = 128_000          # context window size
    compaction_threshold: float = 0.80  # fraction of max_tokens that triggers compaction
    enabled: bool = True
    last_verified_at: str | None = None
    last_error: str | None = None


@dataclass
class EnforcementConfig:
    """Configuration for the enforcement layer."""
    enabled: bool = True
    syntax_check: bool = True
    test_run: bool = True
    lint_check: bool = True
    syntax_timeout_seconds: int = 10
    test_timeout_seconds: int = 60
    lint_timeout_seconds: int = 15
    max_output_chars: int = 2000
    skip_patterns: list[str] = field(default_factory=lambda: [
        "*.md", "*.txt", "*.rst", "*.adoc",
        "*.json", "*.yaml", "*.yml", "*.toml",
        "*.cfg", "*.ini", "*.conf",
        "*.css", "*.scss", "*.less",
        "*.html", "*.htm", "*.xml", "*.svg",
        "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.webp",
        "*.woff", "*.woff2", "*.ttf", "*.eot",
        "*.lock", "*.map",
        "LICENSE*", "README*",
    ])


@dataclass
class AgentConfig:
    """Top-level agent runtime configuration."""
    providers: dict[str, LLMProviderConfig] = field(default_factory=dict)
    # Empty defaults — no provider is auto-seeded; the user configures one via
    # Settings → Providers or the first-run wizard.
    default_provider: str = ""
    default_model: str = ""
    max_tool_iterations: int = 50
    tool_timeout_seconds: int = 120
    auto_save_conversations: bool = True
    cost_limit: float | None = None    # per-conversation USD limit
    step_limit: int | None = None      # per-conversation turn limit
    review_staging_dirname: str = ".crabcakes_review_staging"  # shadow dir for review-mode writes
    enforcement: EnforcementConfig = field(default_factory=EnforcementConfig)
    fallback_provider: str | None = None   # plain provider fallback (user-set, e.g. "openrouter")
    fallback_model: str | None = None      # plain provider fallback model
    user_id: str = ""                     # A-4: user identity for audit log traceability


def _check_permissions(path: str) -> None:
    """
    Warn if config file is group/world-readable.

    Logs a warning but does NOT block startup.
    A group-readable agent.json means other users on the same machine
    can read the API keys. This is a security concern worth surfacing.
    """
    try:
        file_stat = os.stat(path)
        mode = file_stat.st_mode
        # Check group or other read bits
        concerning = (
            (mode & stat.S_IRGRP) or   # group readable
            (mode & stat.S_IWGRP) or    # group writable
            (mode & stat.S_IROTH) or   # others readable
            (mode & stat.S_IWOTH)       # others writable
        )
        if concerning:
            octal = oct(mode)[-4:]
            logger.warning(
                "agent.json is readable by other users (mode=%s). "
                "Run: chmod 600 %s",
                octal, path,
            )
    except OSError:
        pass  # File doesn't exist yet — not a permission problem


def _fix_config_dir_permissions(dir_path: str) -> None:
    """Ensure config directory is owner-only (0o700).

    The directory contains agent.json with API keys in plaintext.
    Restricting to owner-only matches the protection SSH uses for ~/.ssh/.
    This is a best-effort fix — we log a warning if we can't tighten permissions.
    """
    try:
        current = os.stat(dir_path).st_mode & 0o777
        if current != 0o700:
            os.chmod(dir_path, 0o700)
            logger.info("Tightened config dir permissions to 0o700: %s", dir_path)
    except OSError as e:
        logger.warning("Could not fix config dir permissions on %s: %s", dir_path, e)


def _to_llm_provider(p) -> LLMProviderConfig:
    """Convert a models.providers.ProviderConfig to agent.config.LLMProviderConfig."""
    return LLMProviderConfig(
        name=p.name,
        base_url=p.base_url,
        api_key=p.api_key,
        default_model=p.default_model,
        caller=p.caller,
        supports_tools=p.supports_tools,
        supports_streaming=p.supports_streaming,
        max_tokens=p.max_tokens,
        compaction_threshold=getattr(p, "compaction_threshold", 0.80),
        enabled=p.enabled,
        last_verified_at=p.last_verified_at,
        last_error=p.last_error,
    )


def _load_providers_from_yaml(
    config_path: str, raw: dict[str, Any]
) -> dict[str, LLMProviderConfig]:
    """Load providers from providers.yaml — the SOLE source of provider config.

    1. Call utils/providers_store.load_providers() (reads providers.yaml).
    2. If non-empty: convert each ProviderConfig → LLMProviderConfig, return dict.
       The agent.json `providers` field is no longer consulted.
    3. If empty (file missing or empty list): return empty dict.
    """
    try:
        from utils.providers_store import load_providers
        yaml_providers = load_providers()
        if yaml_providers:
            result = {}
            for p in yaml_providers:
                # Key by provider ID (derived from default_model prefix)
                # e.g. "minimax/MiniMax-M2.7" → "minimax"
                # Falls back to display name if no slash in default_model.
                pid = p.default_model.split("/")[0] if p.default_model and "/" in p.default_model else p.name
                result[pid] = _to_llm_provider(p)
                # Also register by display name for UI lookups
                result[p.name] = _to_llm_provider(p)
            return result
    except Exception as e:
        logger.debug("providers.yaml load failed: %s", e)

    return {}


def load_agent_config(config_path: str | None = None) -> AgentConfig:
    """
    Load agent configuration from <config_dir>/agent.json.

    Uses utils/config.get_config_dir() for path resolution, which respects
    $XDG_CONFIG_HOME and falls back to ~/.config/crabcakes.

    Args:
        config_path: Optional override for testing. If None, uses <config_dir>/agent.json.

    Returns:
        AgentConfig with defaults for any missing fields.

    Creates agent.json with example content if it doesn't exist.
    Logs a warning if agent.json is group/world-readable.
    """
    if config_path is None:
        import importlib
        utils_config = importlib.import_module("utils.config")
        config_path = os.path.join(utils_config.get_config_dir(), "agent.json")

    # Check permissions before reading
    if os.path.isfile(config_path):
        _check_permissions(config_path)
        _fix_config_dir_permissions(os.path.dirname(config_path))

    if not os.path.isfile(config_path):
        _create_default_config(config_path)
        # Fall through to parse the newly-created file.

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw: dict[str, Any] = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("agent.json is not valid JSON: %s — using defaults", e)
        return AgentConfig()

    # Parse providers from providers.yaml
    providers = _load_providers_from_yaml(config_path, raw)

    # Parse enforcement config
    enf_raw = raw.get("enforcement", {})
    if isinstance(enf_raw, dict):
        enforcement = EnforcementConfig(
            enabled=enf_raw.get("enabled", True),
            syntax_check=enf_raw.get("syntax_check", True),
            test_run=enf_raw.get("test_run", True),
            lint_check=enf_raw.get("lint_check", True),
            syntax_timeout_seconds=enf_raw.get("syntax_timeout_seconds", 10),
            test_timeout_seconds=enf_raw.get("test_timeout_seconds", 60),
            lint_timeout_seconds=enf_raw.get("lint_timeout_seconds", 15),
            max_output_chars=enf_raw.get("max_output_chars", 2000),
            skip_patterns=enf_raw.get("skip_patterns", EnforcementConfig().skip_patterns),
        )
    else:
        enforcement = EnforcementConfig()

    return AgentConfig(
        providers=providers,
        default_provider=raw.get("default_provider", ""),
        default_model=raw.get("default_model", ""),
        max_tool_iterations=raw.get("max_tool_iterations", 50),
        tool_timeout_seconds=raw.get("tool_timeout_seconds", 120),
        auto_save_conversations=raw.get("auto_save_conversations", True),
        cost_limit=raw.get("cost_limit"),
        step_limit=raw.get("step_limit"),
        enforcement=enforcement,
        fallback_provider=raw.get("fallback_provider"),
        fallback_model=raw.get("fallback_model"),
        user_id=raw.get("user_id", ""),  # A-4: wire user_id from agent.json into config
    )


def _create_default_config(path: str) -> None:
    """Create agent.json with minimal default config.

    No provider is seeded — default_provider/default_model start empty and
    the user configures a provider via Settings → Providers or the
    first-run wizard.
    """
    example = {
        "_comment": "CrabCakes agent configuration. Providers are managed in providers.yaml.",
        "_security": "chmod 600 agent.json — this file may contain API keys",
        "default_provider": "",
        "default_model": "",
        "max_tool_iterations": 50,
        "tool_timeout_seconds": 120,
        "cost_limit": 5.0,
        "step_limit": 100,
        "fallback_provider": None,
        "fallback_model": None,
        "user_id": "",  # A-4: user identity for audit log traceability
    }

    try:
        dir_path = os.path.dirname(path)
        os.makedirs(dir_path, exist_ok=True)
        os.chmod(dir_path, 0o700)  # owner-only: config dir contains API keys
        with open(path, "w", encoding="utf-8") as f:
            json.dump(example, f, indent=4)
        os.chmod(path, 0o600)  # owner-only: file contains API keys
        logger.info("Created example agent.json at %s — add your API keys", path)
    except OSError as e:
        logger.warning("Could not create example agent.json at %s: %s", path, e)


def ensure_providers_yaml_exists(config_path: str) -> str:
    """Ensure providers.yaml exists in the same directory as agent.json.

    Called on startup if providers.yaml doesn't exist. Creates an empty
    providers.yaml so the UI's Settings dialog has a file to write to.

    Returns the path to providers.yaml.
    """
    dir_path = os.path.dirname(config_path)
    yaml_path = os.path.join(dir_path, "providers.yaml")

    if os.path.isfile(yaml_path):
        return yaml_path

    # Create empty providers.yaml
    try:
        os.makedirs(dir_path, exist_ok=True)
        # Empty list — matches _serialize([]) output. _parse() requires a YAML/JSON
        # list at the top level (not a mapping), so this format is load-safe.
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write("[]\n")
        os.chmod(yaml_path, 0o600)
        logger.info("Created empty providers.yaml at %s", yaml_path)
    except OSError as e:
        logger.warning("Could not create providers.yaml at %s: %s", yaml_path, e)

    return yaml_path


def get_api_key(provider_name: str) -> str | None:
    """Get the API key for a specific provider."""
    config = load_agent_config()
    provider = config.providers.get(provider_name)
    return provider.api_key if provider else None
