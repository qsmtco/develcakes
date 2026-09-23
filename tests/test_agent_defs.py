# tests/test_agent_defs.py
# Tests for utils/agent_defs.py — agent definition I/O and validation.
#
# Principle: mock at the boundary, test behavior not internals.
# Uses temp directories for file I/O tests.

import json
import os
import tempfile
import shutil

import pytest

import utils.agent_defs as ad


@pytest.fixture
def tmp_agents_dir(monkeypatch, tmp_path):
    """Redirect agent defs to a temp directory."""
    agents_dir = str(tmp_path / "agents")
    monkeypatch.setattr(ad, "_get_agents_dir", lambda: agents_dir)
    return agents_dir


@pytest.fixture
def default_agents_src(monkeypatch, tmp_path):
    """Redirect default agent source to a temp directory.

    Also patches os.path.isfile so prompt file existence checks pass
    (validate_agent_def checks that prompt files exist on disk).
    """
    src_dir = str(tmp_path / "default_agents")
    os.makedirs(src_dir, exist_ok=True)
    monkeypatch.setattr(ad, "_get_default_agents_src", lambda: src_dir)
    # Make validate_agent_def's prompt-file existence checks pass
    import os as _os
    original_isfile = _os.path.isfile
    def fake_isfile(path):
        path_str = str(path)
        if "system/" in path_str:
            return True  # fake existence for system/ prompt paths
        return original_isfile(path)
    monkeypatch.setattr(_os.path, "isfile", fake_isfile)
    return src_dir


# ── get_default_si_config ──────────────────────────────────────────────────


class TestGetDefaultSiConfig:
    def test_writer_gets_enforcement(self):
        cfg = ad.get_default_si_config(can_write=True)
        assert cfg["bug_journal"] is True
        assert cfg["project_rules"] is True
        assert cfg["enforcement"] is True
        assert cfg["structured_feedback"] is False
        assert cfg["dream_consolidation"] is False

    def test_reader_no_enforcement(self):
        cfg = ad.get_default_si_config(can_write=False)
        assert cfg["enforcement"] is False

    def test_returns_fresh_dict(self):
        """Mutating the result should not affect subsequent calls."""
        cfg1 = ad.get_default_si_config(can_write=True)
        cfg1["enforcement"] = False
        cfg2 = ad.get_default_si_config(can_write=True)
        assert cfg2["enforcement"] is True


# ── save / load round-trip ─────────────────────────────────────────────────


class TestSaveLoad:
    def test_save_and_load_yaml(self, tmp_agents_dir):
        agent = {
            "name": "TestAgent",
            "emoji": "🔬",
            "role": "tester",
            "prompts": ["system/coder.md"],
            "tools": ["read_file", "list_files"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        }
        path = ad.save_agent_def(agent)
        assert os.path.isfile(path)
        assert path.endswith(".yaml")

        loaded = ad.load_agent_def("TestAgent")
        assert loaded is not None
        assert loaded["name"] == "TestAgent"
        assert loaded["role"] == "tester"
        assert loaded["tools"] == ["read_file", "list_files"]

    def test_save_sanitizes_filename(self, tmp_agents_dir):
        agent = {"name": "My Cool Agent!", "tools": ["read_file"], "prompts": ["system/coder.md"], "llm_name": "openrouter", "fallback_provider": "openrouter"}
        path = ad.save_agent_def(agent)
        basename = os.path.basename(path)
        assert " " not in basename
        assert "!" not in basename

    def test_load_nonexistent_returns_none(self, tmp_agents_dir):
        result = ad.load_agent_def("NoAgent")
        assert result is None

    def test_load_by_role(self, tmp_agents_dir):
        agent = {
            "name": "MyAgent",
            "role": "custom-role",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        }
        ad.save_agent_def(agent)
        loaded = ad.load_agent_def_by_role("custom-role")
        assert loaded is not None
        assert loaded["name"] == "MyAgent"

    def test_load_by_role_case_insensitive(self, tmp_agents_dir):
        agent = {
            "name": "MyAgent",
            "role": "Custom-Role",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        }
        ad.save_agent_def(agent)
        loaded = ad.load_agent_def_by_role("custom-role")
        assert loaded is not None

    def test_role_derived_from_name(self, tmp_agents_dir):
        agent = {
            "name": "My Agent",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        }
        ad.save_agent_def(agent)
        loaded = ad.load_agent_def("My Agent")
        assert loaded["role"] == "my-agent"


# ── load_agent_defs ────────────────────────────────────────────────────────


class TestLoadAgentDefs:
    def test_empty_dir_returns_empty(self, tmp_agents_dir, default_agents_src):
        os.makedirs(tmp_agents_dir, exist_ok=True)
        # default_agents_src is a temp dir with no files — so no seeding happens
        defs = ad.load_agent_defs()
        assert defs == []

    def test_seeds_from_default_agents(self, tmp_agents_dir, default_agents_src):
        # Create a default agent file
        coder = {
            "name": "Coder",
            "role": "coder",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        }
        with open(os.path.join(default_agents_src, "coder.yaml"), "w") as f:
            import yaml
            yaml.dump(coder, f)

        defs = ad.load_agent_defs()
        assert len(defs) == 1
        assert defs[0]["name"] == "Coder"

    def test_does_not_overwrite_existing(self, tmp_agents_dir, default_agents_src):
        # User already has a custom agent
        os.makedirs(tmp_agents_dir, exist_ok=True)
        custom = {"name": "Custom", "tools": ["read_file"], "prompts": ["system/coder.md"], "llm_name": "openrouter", "fallback_provider": "openrouter"}
        with open(os.path.join(tmp_agents_dir, "custom.yaml"), "w") as f:
            import yaml
            yaml.dump(custom, f)

        # Default source has a different file
        with open(os.path.join(default_agents_src, "coder.yaml"), "w") as f:
            yaml.dump({"name": "Coder", "tools": ["read_file"], "prompts": ["system/coder.md"], "llm_name": "openrouter", "fallback_provider": "openrouter"}, f)

        defs = ad.load_agent_defs()
        names = [d["name"] for d in defs]
        assert "Custom" in names
        # Per-file seeding: coder.yaml is a missing built-in and IS now
        # seeded alongside the unrelated existing custom.yaml, but the
        # existing custom.yaml itself is never overwritten.
        assert "Coder" in names

    def test_deduplicates_by_name(self, tmp_agents_dir):
        os.makedirs(tmp_agents_dir, exist_ok=True)
        agent = {"name": "Dup", "tools": ["read_file"], "prompts": ["system/coder.md"], "llm_name": "openrouter", "fallback_provider": "openrouter"}
        with open(os.path.join(tmp_agents_dir, "dup.yaml"), "w") as f:
            import yaml
            yaml.dump(agent, f)
        with open(os.path.join(tmp_agents_dir, "dup.json"), "w") as f:
            json.dump(agent, f)

        defs = ad.load_agent_defs()
        names = [d["name"] for d in defs]
        assert names.count("Dup") == 1  # first file wins


# ── delete_agent_def ───────────────────────────────────────────────────────


class TestDeleteAgentDef:
    def test_delete_existing(self, tmp_agents_dir):
        agent = {"name": "ToDelete", "tools": ["read_file"], "prompts": ["system/coder.md"], "llm_name": "openrouter", "fallback_provider": "openrouter"}
        ad.save_agent_def(agent)
        assert ad.load_agent_def("ToDelete") is not None

        assert ad.delete_agent_def("ToDelete") is True
        assert ad.load_agent_def("ToDelete") is None

    def test_delete_nonexistent_returns_false(self, tmp_agents_dir):
        assert ad.delete_agent_def("NoAgent") is False


# ── validate_agent_def ─────────────────────────────────────────────────────


class TestValidateAgentDef:
    def test_valid_agent_no_errors(self):
        agent = {
            "name": "ValidAgent",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        }
        errors = ad.validate_agent_def(agent)
        assert errors == []

    def test_missing_required_fields(self):
        errors = ad.validate_agent_def({})
        assert any("name" in e for e in errors)
        assert any("prompts" in e for e in errors)
        assert any("tools" in e for e in errors)
        assert any("llm_name" in e for e in errors)
        assert any("fallback_provider" in e for e in errors)

    def test_missing_fallback_provider(self):
        """Every agent must have a fallback_provider."""
        agent = {
            "name": "NoFallback",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
        }
        errors = ad.validate_agent_def(agent)
        assert any("fallback_provider" in e for e in errors)

    def test_none_fallback_provider_rejected(self):
        """None is not a valid fallback_provider value."""
        agent = {
            "name": "NullFallback",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": None,
        }
        errors = ad.validate_agent_def(agent)
        assert any("fallback_provider" in e for e in errors)

    def test_unknown_tool_name(self):
        agent = {
            "name": "BadTools",
            "prompts": ["system/coder.md"],
            "tools": ["read_file", "not_a_real_tool"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        }
        errors = ad.validate_agent_def(agent)
        assert any("not_a_real_tool" in e for e in errors)

    def test_unknown_provider(self, monkeypatch, tmp_path):
        # Set up providers.yaml so get_available_providers returns known names
        config_dir = str(tmp_path / ".config" / "crabcakes")
        os.makedirs(config_dir, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        from utils.providers_store import save_providers
        from models.providers import ProviderConfig
        save_providers([
            ProviderConfig(name="minimax", base_url="https://api.minimax.chat/v1",
                           api_key="sk-test", default_model="MiniMax-M2.7"),
        ])
        agent = {
            "name": "BadProvider",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "nonexistent_provider",
            "fallback_provider": "openrouter",
        }
        errors = ad.validate_agent_def(agent)
        assert any("nonexistent_provider" in e for e in errors)

    def test_prompt_file_not_found(self):
        agent = {
            "name": "BadPrompt",
            "prompts": ["nonexistent_prompt.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        }
        errors = ad.validate_agent_def(agent)
        assert any("nonexistent_prompt.md" in e for e in errors)

    def test_empty_name_invalid(self):
        agent = {
            "name": "",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        }
        errors = ad.validate_agent_def(agent)
        assert any("name" in e for e in errors)


# ── get_available_tools / prompts / providers ──────────────────────────────


class TestAvailableOptions:
    def test_get_available_tools(self):
        tools = ad.get_available_tools()
        assert len(tools) > 0
        assert all("name" in t and "description" in t for t in tools)
        names = [t["name"] for t in tools]
        assert "read_file" in names
        assert "write_file" in names

    def test_get_available_prompts(self):
        prompts = ad.get_available_prompts()
        assert len(prompts) > 0
        assert all("name" in p and "filepath" in p for p in prompts)

    def test_get_available_providers(self, monkeypatch, tmp_path):
        # get_available_providers now reads from providers.yaml
        config_dir = str(tmp_path / ".config" / "crabcakes")
        os.makedirs(config_dir, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        from utils.providers_store import save_providers
        from models.providers import ProviderConfig
        save_providers([
            ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1",
                           api_key="sk-test", default_model="deepseek/deepseek-v4-pro"),
        ])
        providers = ad.get_available_providers()
        assert len(providers) > 0
        assert all("name" in p and "base_url" in p and "default_model" in p for p in providers)
        assert providers[0]["name"] == "openrouter"
