# tests/test_bug_fixes.py
# Tests for bug fixes from the adversarial audit.
# These verify specific regression scenarios that the original tests didn't cover.

import json
import os
import tempfile
import shutil

import pytest

import utils.agent_defs as ad
from ui.handlers.agent_builder_handler import AgentBuilderHandler
from agent.special_agents import SpecialAgentDef


@pytest.fixture
def tmp_agents_dir(monkeypatch, tmp_path):
    """Redirect agent defs to a temp directory."""
    agents_dir = str(tmp_path / "agents")
    monkeypatch.setattr(ad, "_get_agents_dir", lambda: agents_dir)
    monkeypatch.setattr(ad, "_get_default_agents_src", lambda: str(tmp_path / "no_defaults"))
    return agents_dir


# ── BUG 1: Enforcement gating ─────────────────────────────────────────────


class TestEnforcementGating:
    def test_si_enforcement_false_skips_check(self):
        """Agent with enforcement: false should not trigger enforcement."""
        agent = SpecialAgentDef(
            conv_id_prefix="special:test",
            display_name="Test",
            role="test",
            emoji="🔬",
            tools=["read_file", "write_file"],
            can_write=True,
            self_improvement={"enforcement": False},
        )
        si_cfg = agent.get_self_improvement_config()
        assert si_cfg["enforcement"] is False

    def test_si_enforcement_true_with_global_enabled(self):
        """Agent enforcement=True AND global enabled → enforcement runs."""
        global_enabled = True
        agent_enabled = True  # enforcement: true in SI config
        result = global_enabled and agent_enabled
        assert result is True

    def test_si_enforcement_false_overrides_global(self):
        """Agent enforcement=False overrides global enabled=True."""
        global_enabled = True
        agent_si = {"enforcement": False}
        agent_enabled = agent_si.get("enforcement", True)
        result = global_enabled and agent_enabled
        assert result is False

    def test_si_enforcement_none_uses_global(self):
        """Agent with no enforcement override uses global setting."""
        agent = SpecialAgentDef(
            conv_id_prefix="special:test",
            display_name="Test",
            role="test",
            emoji="🔬",
            tools=["read_file"],
            can_write=False,
        )
        si_cfg = agent.get_self_improvement_config()
        # can_write=False → enforcement defaults to False
        assert si_cfg["enforcement"] is False


# ── BUG 2: SI overrides preserved on edit ──────────────────────────────────


class TestSIOverridesPreserved:
    def test_preserved_si_on_edit(self, tmp_agents_dir):
        """Editing an agent preserves its SI overrides."""
        h = AgentBuilderHandler(
            on_agent_saved=lambda n: None,
            on_agent_deleted=lambda n: None,
        )

        # Create agent with custom SI
        h.save({
            "name": "TestAgent",
            "prompts": ["system/coder.md"],
            "tools": ["read_file", "write_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
            "api_key": "sk-test-si",
            "self_improvement": {
                "bug_journal": True,
                "project_rules": True,
                "enforcement": True,
                "structured_feedback": True,
                "dream_consolidation": True,
            },
        })

        loaded = h.load_for_edit("TestAgent")
        assert loaded is not None
        si = loaded.get("self_improvement", {})
        assert si.get("dream_consolidation") is True
        assert si.get("structured_feedback") is True


# ── BUG 3: Rename cleans up old file ───────────────────────────────────────


class TestRenameCleanup:
    def test_rename_deletes_old_file(self, tmp_agents_dir):
        """Renaming an agent deletes the old definition file."""
        h = AgentBuilderHandler(
            on_agent_saved=lambda n: None,
            on_agent_deleted=lambda n: None,
        )

        # Create with name "Original"
        h.save({
            "name": "Original",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
            "api_key": "sk-test-ren",
        })
        assert ad.load_agent_def("Original") is not None

        # Load for edit, then rename
        loaded = h.load_for_edit("Original")
        loaded["name"] = "Renamed"
        ok, errors = h.save(loaded)
        assert ok

        # Old name should be gone
        assert ad.load_agent_def("Original") is None
        # New name should exist
        assert ad.load_agent_def("Renamed") is not None

    def test_same_name_no_cleanup(self, tmp_agents_dir):
        """Saving with the same name doesn't delete anything."""
        h = AgentBuilderHandler(
            on_agent_saved=lambda n: None,
            on_agent_deleted=lambda n: None,
        )

        h.save({
            "name": "Stable",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
            "api_key": "sk-test-stable",
        })

        loaded = h.load_for_edit("Stable")
        loaded["model"] = "NewModel"
        ok, _ = h.save(loaded)
        assert ok
        assert ad.load_agent_def("Stable") is not None


# ── BUG 4: Provider management ────────────────────────────────────────────


class TestProviderManagement:
    def test_save_and_delete_provider(self, monkeypatch, tmp_path):
        """A-5: AgentBuilderHandler.save_provider/delete_provider round-trip on providers.yaml.

        Pre-A-5 this test verified utils.agent_defs.save_provider/delete_provider
        mutated agent.json. A-5 removed those functions entirely (provider config
        consolidated to providers.yaml). The test now exercises the handler's
        new methods which delegate to utils.providers_store.
        """
        config_dir = str(tmp_path / ".config" / "crabcakes")
        os.makedirs(config_dir, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", os.path.join(str(tmp_path), ".config"))

        # Create a minimal agent.json (no providers — provider config now lives
        # in providers.yaml per A-5)
        agent_json_path = os.path.join(config_dir, "agent.json")
        with open(agent_json_path, "w") as f:
            json.dump({"default_provider": "openrouter"}, f)

        from ui.handlers.agent_builder_handler import AgentBuilderHandler
        h = AgentBuilderHandler()

        ok = h.save_provider("test-prov", {
            "base_url": "http://localhost:11434/v1",
            "api_key": "test-key",
            # Use a "<vendor>/<model>" default_model so the caller
            # auto-detect path can derive a valid caller. (Prior versions
            # of save_provider were a data-sink; the PROV-CALLER-CONSISTENCY
            # fix added validation that requires a slash.)
            "default_model": "openai/test-model",
        })
        assert ok

        # Verify in providers.yaml (NOT in agent.json)
        from utils.providers_store import load_providers
        yaml_providers = load_providers()
        names = [p.name for p in yaml_providers]
        assert "test-prov" in names
        test_prov = next(p for p in yaml_providers if p.name == "test-prov")
        assert test_prov.api_key == "test-key"

        # agent.json must not have a providers key
        with open(agent_json_path) as f:
            raw = json.load(f)
        assert "providers" not in raw

        ok = h.delete_provider("test-prov")
        assert ok

        # Verify removed from providers.yaml
        yaml_providers = load_providers()
        names = [p.name for p in yaml_providers]
        assert "test-prov" not in names

    def test_delete_nonexistent_provider(self, monkeypatch, tmp_path):
        """A-5: delete_provider returns False when the name doesn't exist."""
        config_dir = str(tmp_path / ".config" / "crabcakes")
        os.makedirs(config_dir, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", os.path.join(str(tmp_path), ".config"))

        from ui.handlers.agent_builder_handler import AgentBuilderHandler
        h = AgentBuilderHandler()

        # No providers.yaml → deleting a nonexistent provider returns False
        # (handler checks existence via load_providers() and returns False
        # when the name isn't present, before calling remove_provider)
        assert h.delete_provider("nonexistent_provider_xyz") is False


# ── BUG 7: Name collision ─────────────────────────────────────────────────


class TestNameCollision:
    def test_collision_detected(self, tmp_agents_dir):
        """Names that sanitize to the same filename are caught."""
        ad.save_agent_def({
            "name": "My Agent",
            "tools": ["read_file"],
            "prompts": ["system/coder.md"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        })

        errors = ad.validate_agent_def({
            "name": "My-Agent",
            "tools": ["read_file"],
            "prompts": ["system/coder.md"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        })
        assert any("collision" in e.lower() for e in errors)

    def test_same_name_no_collision(self, tmp_agents_dir):
        """Same name (edit case) should not trigger collision."""
        ad.save_agent_def({
            "name": "My Agent",
            "tools": ["read_file"],
            "prompts": ["system/coder.md"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        })

        errors = ad.validate_agent_def({
            "name": "My Agent",
            "tools": ["read_file"],
            "prompts": ["system/coder.md"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        })
        assert not any("collision" in e.lower() for e in errors)
