# tests/test_agent_defs.py
# Tests for utils/agent_defs.py — agent definition I/O and validation.
#
# Principle: mock at the boundary, test behavior not internals.
# Uses temp directories for file I/O tests.

import json
import logging
import os
import sys

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
        assert loaded["session_key"] == "agent:my-agent"


def _coder_body(name, role="coder"):
    return {
        "name": name,
        "role": role,
        "prompts": ["system/coder.md"],
        "tools": ["read_file", "write_file"],
        "llm_name": "openrouter",
        "fallback_provider": "openrouter",
    }


class TestSessionKeyAllocation:
    def test_builtin_name_refused_session_key(self, tmp_agents_dir):
        """A name whose slug collides with a built-in key mints NO key."""
        ad.save_agent_def(_coder_body("Coder"))
        loaded = ad.load_agent_def("Coder")
        assert loaded["session_key"] == ""

    def test_non_builtin_name_mints_agent_key(self, tmp_agents_dir):
        """A non-builtin name slug mints agent:<name-slug>."""
        ad.save_agent_def(_coder_body("AlphaCoder"))
        loaded = ad.load_agent_def("AlphaCoder")
        assert loaded["session_key"] == "agent:alphacoder"

    def test_distinct_name_mints_own_agent_key(self, tmp_agents_dir):
        os.makedirs(tmp_agents_dir, exist_ok=True)
        import yaml
        with open(os.path.join(tmp_agents_dir, "coder.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(_coder_body("Coder"), f)

        ad.save_agent_def(_coder_body("Coder 2"))
        assert ad.load_agent_def("Coder 2")["session_key"] == "agent:coder-2"
        # The hand-written primary (builtin name) still owns no session_key.
        with open(os.path.join(tmp_agents_dir, "coder.yaml"), encoding="utf-8") as f:
            primary = yaml.safe_load(f)
        assert primary.get("session_key") in (None, "")

    def test_third_distinct_name_gets_own_key(self, tmp_agents_dir):
        ad.save_agent_def(_coder_body("Coder"))
        ad.save_agent_def(_coder_body("Coder 2"))
        ad.save_agent_def(_coder_body("Coder 3"))
        assert ad.load_agent_def("Coder 3")["session_key"] == "agent:coder-3"

    def test_edit_preserves_assigned_key(self, tmp_agents_dir):
        ad.save_agent_def(_coder_body("Coder"))
        ad.save_agent_def(_coder_body("Coder 2"))
        edited = _coder_body("Coder 2")
        edited["emoji"] = "🧪"
        ad.save_agent_def(edited)
        loaded = ad.load_agent_def("Coder 2")
        assert loaded["session_key"] == "agent:coder-2"
        assert loaded["emoji"] == "🧪"

    def test_explicit_session_key_is_kept(self, tmp_agents_dir):
        agent = _coder_body("Coder 2")
        agent["session_key"] = "agent:alpha"
        ad.save_agent_def(_coder_body("Coder"))
        ad.save_agent_def(agent)
        assert ad.load_agent_def("Coder 2")["session_key"] == "agent:alpha"

    def test_edit_preserves_auto_open(self, tmp_agents_dir):
        os.makedirs(tmp_agents_dir, exist_ok=True)
        import yaml
        body = _coder_body("Coder")
        body["auto_open"] = True
        body["session_key"] = "agent:coder"
        with open(os.path.join(tmp_agents_dir, "coder.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(body, f)
        ad.save_agent_def(_coder_body("Coder"))
        loaded = ad.load_agent_def("Coder")
        assert loaded["auto_open"] is True
        assert loaded["session_key"] == "agent:coder"


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

    def test_validate_accepts_schema_hidden_permit_b4(self):
        """B4 Option B: the validator gates what the runtime can execute, so
        the B1 permit shape (file_search listed in tools) validates clean,
        while the Builder surface (get_available_tools) still hides it."""
        agent = {
            "name": "PermitAgent",
            "prompts": ["system/coder.md"],
            "tools": ["read_file", "file_search"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        }
        errors = ad.validate_agent_def(agent)
        assert errors == []
        names = [t["name"] for t in ad.get_available_tools()]
        assert "file_search" not in names
        assert "search_files" in names

    def test_import_failure_skips_tool_validation_c5(self, monkeypatch, tmp_path, caplog):
        """Bugfix C5 (audit BUG #4): with agent.tools unimportable,
        get_available_tools() swallows its own ImportError and returns [],
        so validating against it would flag every tool "Unknown tool".
        The fallback must skip the name check (and log why) instead."""
        monkeypatch.setitem(sys.modules, "agent.tools", None)
        assert ad.get_available_tools() == []  # the bug's premise: empty set

        # Hermetic provider check: validate_agent_def's llm_name validation
        # reads providers.yaml, so "openrouter" must exist there regardless
        # of the developer's real config (same setup as test_unknown_provider).
        monkeypatch.setenv("HOME", str(tmp_path))
        from models.providers import ProviderConfig
        from utils.providers_store import save_providers
        save_providers([
            ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1",
                           api_key="sk-test", default_model="deepseek/deepseek-v4-pro"),
        ])

        agent = {
            "name": "BrokenInstall",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        }
        with caplog.at_level(logging.WARNING, logger="utils.agent_defs"):
            errors = ad.validate_agent_def(agent)
        assert errors == []
        assert not any("Unknown tool" in e for e in errors)
        assert any("skipping tool-name validation" in r.message for r in caplog.records)

    def test_unknown_provider(self, monkeypatch, tmp_path):
        # Set up providers.yaml so get_available_providers returns known names
        config_dir = str(tmp_path / ".config" / "develcakes")  # SPEC-11 D4
        os.makedirs(config_dir, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        from models.providers import ProviderConfig
        from utils.providers_store import save_providers
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

    def test_get_available_tools_hides_schema_hidden_b4(self):
        """B4: file_search is _API_SCHEMA_HIDDEN, so the Builder surface
        (which renders get_available_tools as checkboxes) must not list it.
        search_files — the visible replacement — must remain."""
        names = [t["name"] for t in ad.get_available_tools()]
        assert "search_files" in names
        assert "file_search" not in names

    def test_get_available_prompts(self):
        prompts = ad.get_available_prompts()
        assert len(prompts) > 0
        assert all("name" in p and "filepath" in p for p in prompts)

    def test_get_available_providers(self, monkeypatch, tmp_path):
        # get_available_providers now reads from providers.yaml
        config_dir = str(tmp_path / ".config" / "develcakes")  # SPEC-11 D4
        os.makedirs(config_dir, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        from models.providers import ProviderConfig
        from utils.providers_store import save_providers
        save_providers([
            ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1",
                           api_key="sk-test", default_model="deepseek/deepseek-v4-pro"),
        ])
        providers = ad.get_available_providers()
        assert len(providers) > 0
        assert all("name" in p and "base_url" in p and "default_model" in p for p in providers)
        assert providers[0]["name"] == "openrouter"
