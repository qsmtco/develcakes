"""SPEC-22 startup sanity check. Pure: returns warning strings, never raises."""

from __future__ import annotations

import logging
import os
from typing import Mapping

_logger = logging.getLogger(__name__)


def startup_warnings(env: Mapping[str, str] | None = None) -> list[str]:
    """Configuration faults that used to stay silent until a call failed.

    SPEC-63 §2.5 (fix-round 2, delta #8): ``env`` defaults to ``os.environ``
    AT CALL TIME, making the env-key-warning tests hermetic — they pass a
    dict instead of monkeypatching process state. Still a pure function:
    no I/O beyond the passed-in mapping.
    """
    if env is None:
        env = os.environ
    warnings: list[str] = []
    try:
        from utils.providers_store import load_providers
        providers = load_providers()
    except Exception as exc:  # noqa: BLE001 — a bad store is itself a warning
        return [f"providers.yaml could not be read: {type(exc).__name__}"]

    enabled = [p for p in providers if getattr(p, "enabled", True)]
    if not enabled:
        warnings.append("no enabled provider card")

    # SPEC-63 §2.5 (v3): the env line is ADDED alongside the generic line
    # above — the two conditions are independent (zero-enabled vs
    # missing-vendor), so neither may replace the other. Fires only when a
    # mapped env var is set AND no stored card serves that preset's vendor.
    try:
        from utils.env_provider_keys import detect_env_keys
        from utils.provider_presets import get_preset, preset_vendor_present
        detected = [
            (env_name, preset_key)
            for env_name, preset_key, _value in detect_env_keys(env)
            if not preset_vendor_present(providers, get_preset(preset_key))
        ]
    except ImportError:
        detected = []
    if detected:
        first_env_name = detected[0][0]
        line = (
            f"Found {len(detected)} provider key(s) in your environment "
            f"(e.g. {first_env_name})"
        )
        # v3: when the store is empty the env line itself carries BOTH facts
        # (keys found + nothing configured); when any card exists, drop the
        # second clause so the disabled-card story stays on the generic line.
        if not providers:
            line += (
                " — but no provider card is configured yet; open Settings "
                "→ Providers to add one in a click."
            )
        else:
            line += " — open Settings → Providers to add one in a click."
        warnings.append(line)

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
            elif isinstance(primary, str) and primary and fallback == primary:
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
