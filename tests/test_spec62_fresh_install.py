# tests/test_spec62_fresh_install.py
# SPEC-62 Phase 2 — fresh-install regression tests for BOTH orderings.
#
# Ordering (a) LAUNCH-FIRST: the default agents seed on first load; the user
#   then configures ONE provider. The provider save must leave all 3 defs
#   loadable and all 3 rows registered.
# Ordering (b) PROVIDER-BEFORE-FIRST-LAUNCH: providers.yaml + agent.json
#   defaults exist BEFORE the first-ever load; that first load must yield
#   all 3 defs and the registry 3 rows. THIS is the ordering the Phase-2
#   product fixes (items 4-7) target; it was previously untested.
#
# Joint-contract tests: they drive utils.agent_defs (Phase-1 files) and
# agent.special_agents through REAL code paths and assert the POST-MERGE
# contract (llm_name optional; shipped YAMLs without llm_name: local-kb).
# On a base predating Phase 1 they fail — that is what a fresh-install
# regression net is for. Supervisor runs the full battery:
#   xvfb-run -a .venv/bin/python -m pytest tests/test_spec62_fresh_install.py
#
# GTK_HEADLESS_SAFE: this module constructs NO widgets. It imports
# AgentRuntimeHandler (whose module contains a Gtk.MessageDialog
# constructor) only to drive refresh_agent_registry on a plain object —
# the conftest AST guard would skip the whole file headless without this
# opt-out (same marker as tests/test_perf_feed.py).

GTK_HEADLESS_SAFE = True

import pytest

from agent.config import get_agent_setting, set_agent_setting
from models.providers import ProviderConfig
from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
from utils.providers_store import load_providers, save_providers


_BUILTIN_NAMES = {"Coder", "Debugger", "Supervisor"}


def _chat_card(name="solo"):
    return ProviderConfig(
        name=name,
        base_url=f"https://api.{name}.example.com/v1",
        api_key=f"sk-{name}-key",
        default_model=f"openai/{name}-model-v1",
    )


def _agent_defs_module():
    """Lazy import — keeps module import free of utils.agent_defs side
    effects and stays invisible to collection."""
    import utils.agent_defs
    return utils.agent_defs


class TestFreshInstallLaunchFirst:
    """Ordering (a): defaults seeded (first launch), THEN one provider."""

    def test_seed_then_one_provider_keeps_three_defs(self, tmp_config_dir):
        agent_defs = _agent_defs_module()
        agent_defs._seed_defaults()

        save_providers([_chat_card("solo")])
        assert get_agent_setting("default_provider", "") == "solo"

        defs = agent_defs.load_agent_defs()
        assert {d["name"] for d in defs} == _BUILTIN_NAMES

    def test_seed_then_provider_rows_registered(self, tmp_config_dir):
        agent_defs = _agent_defs_module()
        agent_defs._seed_defaults()
        save_providers([_chat_card("solo")])

        from agent.special_agents import get_special_agents
        rows = get_special_agents()
        assert {r.display_name for r in rows} == _BUILTIN_NAMES

    def test_provider_save_refresh_keeps_rows_in_handler(self, tmp_config_dir):
        # The window seam end-to-end: provider save → refresh_agent_registry
        # keeps the 3 rows registered on the runtime handler (no MCP teardown
        # — pinned in tests/test_config_invalidation.py).
        agent_defs = _agent_defs_module()
        agent_defs._seed_defaults()
        save_providers([_chat_card("solo")])

        h = AgentRuntimeHandler(main_content=None, chat_render_handler=None)
        h.refresh_agent_registry()
        assert set(h.get_special_agents().values()) == _BUILTIN_NAMES


class TestFreshInstallProviderBeforeLaunch:
    """Ordering (b): provider configured BEFORE the first-ever launch — the
    untested ordering whose product fix lives in Phase-2 items 4-7."""

    def test_provider_first_launch_loads_three_defs(self, tmp_config_dir):
        agent_defs = _agent_defs_module()
        save_providers([_chat_card("solo")])
        set_agent_setting("default_provider", "solo")
        set_agent_setting("default_model", "openai/solo-model-v1")

        defs = agent_defs.load_agent_defs()  # first-ever load: seeds + validates
        assert {d["name"] for d in defs} == _BUILTIN_NAMES
        # Post-Phase-1 contract pin: the shipped defs carry a FALSY llm_name
        # (the global default replaces the local-kb placeholder).
        for d in defs:
            assert not d.get("llm_name")

    def test_provider_first_launch_yields_three_rows(self, tmp_config_dir):
        agent_defs = _agent_defs_module()
        save_providers([_chat_card("solo")])

        from agent.special_agents import get_special_agents, reload_registry
        reload_registry()  # the first-ever boot path builds the registry now
        rows = get_special_agents()
        assert {r.display_name for r in rows} == _BUILTIN_NAMES

    def test_ordering_b_rows_survive_second_provider_save(self, tmp_config_dir):
        # The in-app invalidation loop: a provider save after boot must leave
        # the 3 rows registered (items 4-7 regression for this ordering).
        agent_defs = _agent_defs_module()
        save_providers([_chat_card("solo")])

        h = AgentRuntimeHandler(main_content=None, chat_render_handler=None)
        h.refresh_agent_registry()
        save_providers([_chat_card("solo"), _chat_card("second")])
        h.refresh_agent_registry()
        assert set(h.get_special_agents().values()) == _BUILTIN_NAMES
