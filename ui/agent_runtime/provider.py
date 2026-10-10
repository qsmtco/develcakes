"""Provider model resolution and providers.yaml refresh."""
from __future__ import annotations

import copy
import json
import os
from dataclasses import is_dataclass, replace
from typing import Any

from ui.agent_runtime._facade import _mod


class RuntimeProviderMixin:
    def _resolve_agent_model(self, agent_def: Any) -> str | None:
        """Resolve the model string for an agent definition.

        Uses agent-specific llm_name to look up the provider in providers.yaml,
        then resolves the model from the provider's default_model.

        Returns:
            Full model string like "minimax/MiniMax-M2.7", or None to use
            the runtime's default_model.
        """
        llm_name = getattr(agent_def, "llm_name", None)

        if not llm_name:
            return None

        try:
            from agent.config import load_agent_config
            config = load_agent_config()
            prov_cfg = config.providers.get(llm_name)
            if prov_cfg and prov_cfg.default_model:
                if "/" in prov_cfg.default_model:
                    return prov_cfg.default_model
                return f"{llm_name}/{prov_cfg.default_model}"
        except Exception:
            _mod().logger.warning("Cannot resolve provider default model for %s", llm_name)
        return llm_name  # fallback — runtime will try to resolve

    @staticmethod
    def _parse_providers_file_strict(path: str) -> list[Any] | None:
        """Strictly parse providers.yaml, distinguishing valid-empty from corrupt.

        utils.providers_store.load_providers/_parse swallow every parse error
        (warning + []), so "missing file", "corrupt file" and "file is []" all
        look identical through the public API. BUG 2 (SPEC-01 Phase 1): the
        refresh guard must fire only for the first two — an existing file that
        parses to an empty list is an intentional removal (Settings deleted
        the last provider) and must apply.

        Mirrors _parse's deserialization chain: PyYAML when importable, json
        fallback otherwise. Returns the parsed list ([] for a valid empty
        document) or None when the content is unreadable or unparseable, so a
        hostile file can never abort a Settings save's refresh side effect.
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return None  # unreadable — treated as missing by the caller

        try:
            import yaml

            from utils.yaml_load import safe_load_yaml

            raw: Any = safe_load_yaml(text)
        except ImportError:
            try:
                raw = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return None
        except yaml.YAMLError:
            # PyYAML parser errors (ScannerError/ParserError/…) — corrupt content.
            return None

        # Same shape contract as providers_store._parse: anything that is not
        # a list is malformed (its per-item skip-warnings stay in _parse).
        return raw if isinstance(raw, list) else None

    def refresh_provider_config(self) -> None:
        """Reload providers.yaml and update every cached runtime's config in place.

        Called when Settings saves a provider (on_providers_changed → wire_settings_handler
        → this). Conversations and runtimes are NOT recreated — only the provider dict
        is swapped so base_url/caller/max_tokens edits take effect on the next call
        without an app restart. (SPEC-01: stale-cache-divergence fix.)

        Semantics (SPEC-01 Phase 1):
          * Missing or unparseable providers.yaml → keep cached snapshots + warn.
            An EXISTING file that parses to an empty list is an intentional
            removal (Settings deleted the last provider) and applies.
          * Every runtime receives its own clone of the fresh provider values:
            Phase 2 resolves base_url/caller live by mutating the runtime's
            provider dict in place, so one shared object would leak one
            agent's resolution into every other agent.
          * One failing runtime logs and is skipped; the batch continues.
        """
        from agent.config import load_agent_config
        from utils.providers_store import get_providers_path

        # BUG 2: distinguish valid-empty from missing/corrupt. load_providers()
        # maps all three to [], so probe the file directly: it must exist AND
        # parse. Missing/unparseable keeps the old snapshot; existing-empty
        # falls through and applies.
        yaml_path = get_providers_path()
        file_is_loadable = os.path.isfile(yaml_path) and (
            self._parse_providers_file_strict(yaml_path) is not None
        )
        fresh = load_agent_config()
        if not fresh.providers and not file_is_loadable:
            _mod().logger.warning(
                "refresh_provider_config: providers.yaml is missing or corrupt "
                "— keeping existing runtime snapshots"
            )
            return
        # Iterate a snapshot: _get_runtime() may insert into _runtimes while
        # this loop runs — mutating a dict during iteration raises
        # RuntimeError. list() copies once; the loop body touches only the
        # runtime objects, never the dict itself. (Both callers run on the
        # main thread; the copy is defensive hardening, not a race guard.)
        updated = 0
        for name, rt in list(self._runtimes.items()):
            try:
                cfg = rt._config
                providers = cfg.providers
                # BUG 6: clone each fresh value into this runtime's own dict —
                # never share the fresh dict or its entries across runtimes
                # (live base_url/caller resolution mutates the dict in place,
                # and one shared object would leak one agent's resolution into
                # every other agent). dataclasses.replace() clones a
                # ProviderConfig-style dataclass; deepcopy covers any other
                # type. The clone is built BEFORE mutating `providers` so a
                # clone failure leaves the old snapshot intact.
                cloned = {
                    key: replace(value)
                    if is_dataclass(value) and not isinstance(value, type)
                    else copy.deepcopy(value)
                    for key, value in fresh.providers.items()
                }
                providers.clear()
                providers.update(cloned)
                updated += 1
            except Exception:
                _mod().logger.exception(
                    "refresh_provider_config: failed to update runtime %r — skipped",
                    name,
                )
        if updated:
            _mod().logger.info("refresh_provider_config: updated %d runtime(s)", updated)

