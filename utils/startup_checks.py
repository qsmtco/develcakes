"""SPEC-22 startup sanity check. Pure: returns warning strings, never raises."""

from __future__ import annotations

import logging
import os

_logger = logging.getLogger(__name__)


def startup_warnings() -> list[str]:
    """Configuration faults that used to stay silent until a call failed."""
    warnings: list[str] = []
    try:
        from utils.providers_store import load_providers
        providers = load_providers()
    except Exception as exc:  # noqa: BLE001 — a bad store is itself a warning
        return [f"providers.yaml could not be read: {type(exc).__name__}"]

    enabled = [p for p in providers if getattr(p, "enabled", True)]
    if not enabled:
        warnings.append("no enabled provider card")

    names = {getattr(p, "name", "") for p in providers}
    try:
        from utils.agent_defs import load_agent_defs
        agents = load_agent_defs()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("startup check could not load agents: %s", exc)
        agents = []

    uses_web = False
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        tools = agent.get("tools") or []
        if "web_search" in tools or "web_fetch" in tools:
            uses_web = True
        fallback = agent.get("fallback_provider")
        primary = agent.get("llm_name") or ""
        if isinstance(fallback, str) and fallback.strip():
            if fallback not in names:
                warnings.append(
                    f"{agent.get('name') or 'agent'} fallback {fallback!r} "
                    "does not match a provider card name"
                )
            elif isinstance(primary, str) and "/" not in primary and fallback == primary:
                warnings.append(
                    f"{agent.get('name') or 'agent'} fallback is the same card as the primary"
                )

    if uses_web and not (
        os.environ.get("BRAVE_API_KEY") or os.environ.get("OPENCLAW_BRAVE_API_KEY")
    ):
        warnings.append(
            "BRAVE_API_KEY is not set; web_search will fail until it is"
        )

    for provider in providers:
        if getattr(provider, "name", "") != "DecisionModel":
            continue
        if not (getattr(provider, "api_key", "") or "").strip():
            warnings.append("DecisionModel card is present but has no API key")
    return warnings
