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
    """Redirect agent defs to a temp directory.

    SPEC-62 Fix Round 1 (Debugger #5): creates the directory, not just the
    path string — callers like test_migration_is_line_surgical_and_noop_when_clean
    write coder.yaml straight into it and hit FileNotFoundError otherwise
    (the redirect monkeypatch never materialized the dir).
    """
    agents_dir = str(tmp_path / "agents")
    os.makedirs(agents_dir, exist_ok=True)
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
        """SPEC-62: llm_name is OPTIONAL (absent → global default at
        runtime). Every other required field still errors, and an absent
        llm_name produces no llm_name error at all."""
        errors = ad.validate_agent_def({})
        assert any("name" in e for e in errors)
        assert any("prompts" in e for e in errors)
        assert any("tools" in e for e in errors)
        assert any("fallback_provider" in e for e in errors)
        assert not any("llm_name" in e for e in errors), (
            f"llm_name must be optional — got errors: {errors}"
        )

    def test_absent_llm_name_no_errors_when_prompt_and_tools_exist(self):
        """A def without any llm_name key validates clean (SPEC-62)."""
        agent = {
            "name": "NoLlmName",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "fallback_provider": "openrouter",
        }
        errors = ad.validate_agent_def(agent)
        assert errors == [], f"unexpected errors: {errors}"

    def test_empty_llm_name_is_also_accepted(self):
        """Both falsy spellings are unset: an EMPTY-STRING llm_name must be
        treated the same as an absent key (SPEC-62 edge-case table)."""
        agent = {
            "name": "EmptyLlmName",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "",
            "fallback_provider": "openrouter",
        }
        errors = ad.validate_agent_def(agent)
        assert not any("llm_name" in e for e in errors), (
            f"empty-string llm_name must not error — got: {errors}"
        )

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


# ── SPEC-62 fresh-install regressions ──────────────────────────────────────

_BUILTIN_SEED_SPECS = (
    ("coder.yaml", "Coder", "system/coder.md", "coder"),
    ("debugger.yaml", "Debugger", "system/debugger.md", "debugger"),
    ("supervisor.yaml", "Supervisor", "system/supervisor.md", "supervisor"),
)


def _write_default_agents(src_dir: str, *, with_placeholder: bool = False) -> None:
    """Write the three built-in default YAMLs into a redirected src dir.

    with_placeholder=True reproduces the PRE-0.2 shipped shape (llm_name
    naming the legacy placeholder) so the migration test starts from the
    real historical on-disk state, not an invented one.
    """
    import yaml
    for fname, name, prompt, role in _BUILTIN_SEED_SPECS:
        agent = {
            "name": name,
            "emoji": "x",
            "role": role,
            "prompts": [prompt],
            "tools": ["read_file"],
            "fallback_provider": "openrouter",
        }
        if with_placeholder:
            agent["llm_name"] = ad._LEGACY_PLACEHOLDER_LLM_NAME
        with open(os.path.join(src_dir, fname), "w", encoding="utf-8") as f:
            yaml.dump(agent, f)


class TestSpec62FreshInstall:
    """SPEC-62 Phase 1: built-in defs load on a fresh install and the
    pre-0.2 placeholder llm_name is migrated in already-seeded configs.

    Config resolution is isolated by the conftest autouse
    _isolate_config_resolution fixture (HOME + XDG_CONFIG_HOME → tmp_path);
    the agents dir itself is redirected via tmp_agents_dir/default_agents_src
    like the suites above.
    """

    def _seed_one_provider(self, tmp_path) -> None:
        from models.providers import ProviderConfig
        from utils.providers_store import save_providers
        save_providers([
            ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1",
                           api_key="sk-test", default_model="deepseek/deepseek-v4-pro"),
        ])

    def test_fresh_install_seeds_three_valid_defs(self, tmp_agents_dir, default_agents_src):
        """Fresh temp config, NO llm_name keys anywhere → load_agent_defs()
        seeds the three built-ins and all three validate clean."""
        _write_default_agents(default_agents_src)
        defs = ad.load_agent_defs()
        assert len(defs) == 3
        assert {d["name"] for d in defs} == {"Coder", "Debugger", "Supervisor"}
        for d in defs:
            assert not d.get("llm_name"), (
                f"{d['name']} must load with a falsy llm_name, got {d.get('llm_name')!r}"
            )

    def test_fresh_install_with_one_provider_loads_all_three(
        self, tmp_agents_dir, default_agents_src, tmp_path
    ):
        """Fresh install + ONE configured card → all three built-ins load and
        load_agent_def() returns each by display name. The seeded defaults
        reproduce the PRE-FIX shipped shape (placeholder llm_name), which is
        the RED case: on unmodified HEAD the placeholder is rejected the
        moment a provider exists, every def is dropped, and these asserts
        fail."""
        _write_default_agents(default_agents_src, with_placeholder=True)
        self._seed_one_provider(tmp_path)

        assert ad.load_agent_def("Coder") is not None
        assert ad.load_agent_def("Debugger") is not None
        assert ad.load_agent_def("Supervisor") is not None

    def test_fresh_install_with_zero_providers_still_loads(
        self, tmp_agents_dir, default_agents_src
    ):
        """Fresh install, NO providers configured, PRE-FIX placeholder
        defaults → the three defs still load. Green on HEAD BY CONSTRUCTION:
        with zero cards the validator's display_names set is empty, so the
        unknown-provider branch (the only thing that rejects the placeholder)
        is skipped. Pinned so a no-provider install can never regress into a
        broken boot."""
        _write_default_agents(default_agents_src, with_placeholder=True)
        assert ad.load_agent_def("Coder") is not None
        assert ad.load_agent_def("Debugger") is not None
        assert ad.load_agent_def("Supervisor") is not None

    def test_validator_minimal_dict_and_unknown_provider(
        self, tmp_path, monkeypatch
    ):
        """Validator contract: a minimal def without llm_name produces no
        llm_name error; a non-empty unknown llm_name WITH one card seeded
        still errors Unknown provider (the display_names guard makes the
        no-card variant vacuous, so a card is seeded first)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

        minimal = {
            "name": "Minimal",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "fallback_provider": "openrouter",
        }
        errors = ad.validate_agent_def(minimal)
        assert not any("llm_name" in e for e in errors), (
            f"absent llm_name must be optional — got: {errors}"
        )

        self._seed_one_provider(tmp_path)
        unknown = dict(minimal, llm_name="not-a-provider")
        errors = ad.validate_agent_def(unknown)
        assert any("Unknown provider" in e for e in errors), (
            f"non-empty unknown llm_name must still error — got: {errors}"
        )

    def test_migration_rewrites_placeholder_and_is_idempotent(
        self, tmp_agents_dir, default_agents_src, tmp_path
    ):
        """End-to-end layer: pre-seed a config copy carrying the legacy
        placeholder, run _seed_defaults(), and assert the placeholder is
        gone, fallback_provider/name survive, the def then LOADS with one
        card seeded, a second run is a byte-level no-op, and a customized
        llm_name is never rewritten. (The one-line-surgical guarantee is
        pinned separately by test_migration_is_line_surgical_and_noop_when_clean;
        here ensure_builtin_tool legitimately extends tools in the same pass.)
        RED on HEAD: the placeholder copy simply survives (no migration, no
        relaxation), so the rewritten-on-disk and load asserts fail."""
        import yaml

        _write_default_agents(default_agents_src, with_placeholder=True)

        # Simulate the EXISTING-INSTALL state: a user copy seeded from the
        # old defaults, plus a customized file that must never be touched.
        self._seed_one_provider(tmp_path)
        ad._seed_defaults()  # first pass: copies + should migrate in one run

        coder_path = os.path.join(tmp_agents_dir, "coder.yaml")
        with open(coder_path, encoding="utf-8") as f:
            migrated_text = f.read()
        assert f"llm_name: {ad._LEGACY_PLACEHOLDER_LLM_NAME}" not in migrated_text
        assert 'llm_name: ""' in migrated_text

        migrated = yaml.safe_load(migrated_text)
        assert migrated["fallback_provider"] == "openrouter"
        assert migrated["name"] == "Coder"
        assert ad.load_agent_def("Coder") is not None

        # Byte-level idempotence: the second _seed_defaults() run must not
        # rewrite the file at all (no new mtime/content churn).
        before = os.stat(coder_path)
        snapshot = migrated_text
        ad._seed_defaults()
        after = os.stat(coder_path)
        with open(coder_path, encoding="utf-8") as f:
            assert f.read() == snapshot
        assert (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size)

        # A copy whose llm_name is any OTHER value is user customization.
        custom_path = os.path.join(tmp_agents_dir, "debugger.yaml")
        with open(custom_path, encoding="utf-8") as f:
            custom_before = f.read()
        assert f"llm_name: {ad._LEGACY_PLACEHOLDER_LLM_NAME}" not in custom_before

        text = custom_before.replace(
            'llm_name: ""', "llm_name: my-card  # user's own card"
        )
        with open(custom_path, "w", encoding="utf-8") as f:
            f.write(text)
        ad._seed_defaults()
        with open(custom_path, encoding="utf-8") as f:
            custom_after = f.read()
        assert "llm_name: my-card" in custom_after, (
            "user-customized llm_name must NEVER be rewritten"
        )
        # And the validator still requires that custom card to exist.
        custom_def = yaml.safe_load(custom_after)
        errors = ad.validate_agent_def(custom_def)
        assert any("Unknown provider" in e for e in errors), (
            "customized unknown llm_name must still validate as unknown"
        )

    def test_migration_is_line_surgical_and_noop_when_clean(self, tmp_agents_dir, tmp_path):
        """Unit layer: _migrate_legacy_placeholder_llm_name rewrites ONLY the
        llm_name line (every other line preserved byte-for-byte), refuses a
        longer value like ``llm_name: local-kb-v2`` (user customization), and
        is a no-op when the exact placeholder is absent. RED-on-HEAD as an
        AttributeError (the helper does not exist there); the NEGATIVE
        branches inside (customize / no-op / idempotence) are trivially true
        on HEAD because HEAD had no migration at all — pinned so the negative
        guarantees survive the introduction of one."""
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "prompts", "default_agents", "coder.yaml",
        )
        with open(src_path, encoding="utf-8") as f:
            real_yaml = f.read()

        # 1) Rewrite preserves every other line byte-for-byte. Build the
        # synthetic PRE-FIX copy by reinserting the historical llm_name line
        # in the position it shipped in (between the role line and the
        # fallback_provider line), so the file under test is the real
        # pre-0.2 shape, not an invented one.
        historical = real_yaml.replace(
            "fallback_provider:",
            "llm_name: %s  # placeholder; user configures their own in Settings\nfallback_provider:"
            % ad._LEGACY_PLACEHOLDER_LLM_NAME,
            1,
        )
        assert f"llm_name: {ad._LEGACY_PLACEHOLDER_LLM_NAME}" in historical, (
            "premise: the synthetic historical copy must carry the placeholder"
        )
        target = os.path.join(tmp_agents_dir, "coder.yaml")
        with open(target, "w", encoding="utf-8") as f:
            f.write(historical)

        ad._migrate_legacy_placeholder_llm_name(target)
        with open(target, encoding="utf-8") as f:
            migrated = f.read()
        assert f"llm_name: {ad._LEGACY_PLACEHOLDER_LLM_NAME}" not in migrated
        assert 'llm_name: ""' in migrated
        old_lines = historical.splitlines()
        new_lines = migrated.splitlines()
        assert len(old_lines) == len(new_lines), (
            "line-level rewrite must not add or drop lines"
        )
        changed = [
            (a, b) for a, b in zip(old_lines, new_lines) if a != b
        ]
        assert len(changed) == 1, f"exactly one line may change, got: {changed}"
        assert changed[0][1].strip().startswith('llm_name: ""')

        # 2) Idempotent: second call changes nothing (string gate).
        mtime_before = os.stat(target).st_mtime_ns
        ad._migrate_legacy_placeholder_llm_name(target)
        with open(target, encoding="utf-8") as f:
            assert f.read() == migrated
        assert os.stat(target).st_mtime_ns == mtime_before

        # 3) A longer value sharing the prefix is user customization: NEVER
        # rewritten.
        customized = historical.replace(
            f"llm_name: {ad._LEGACY_PLACEHOLDER_LLM_NAME}",
            "llm_name: local-kb-v2  # user's own card",
            1,
        )
        with open(target, "w", encoding="utf-8") as f:
            f.write(customized)
        ad._migrate_legacy_placeholder_llm_name(target)
        with open(target, encoding="utf-8") as f:
            assert f.read() == customized

        # 4) No exact placeholder (migrated already) → no-op, no rewrite.
        mtime_before = os.stat(target).st_mtime_ns
        ad._migrate_legacy_placeholder_llm_name(target)
        with open(target, encoding="utf-8") as f:
            assert f.read() == customized
        assert os.stat(target).st_mtime_ns == mtime_before

    def test_migration_matches_all_three_quoted_spellings(self, tmp_agents_dir):
        """SPEC-62 Fix Round 1 (Debugger #6): the placeholder VALUE must match
        across the three YAML scalar spellings — unquoted, double-quoted,
        single-quoted — by exact equality AFTER unquoting. Near-misses sharing
        the prefix (``local-kb-v2``) or the name (``local_kb``) are user
        customization and stay untouched. JSON agent files are NOT a target:
        _BUILTIN_AGENT_FILES is the three YAMLs only. RED on the pre-round
        code, where a quoted placeholder survived the migration untouched."""
        import yaml

        template = (
            "name: Coder\n"
            "role: coder\n"
            "llm_name: {value}\n"
            "fallback_provider: openrouter\n"
            "tools:\n  - read_file\n"
        )
        for spelling in (
            ad._LEGACY_PLACEHOLDER_LLM_NAME,                     # unquoted
            '"%s"' % ad._LEGACY_PLACEHOLDER_LLM_NAME,            # double-quoted
            "'%s'" % ad._LEGACY_PLACEHOLDER_LLM_NAME,            # single-quoted
        ):
            target = os.path.join(tmp_agents_dir, "coder.yaml")
            with open(target, "w", encoding="utf-8") as f:
                f.write(template.format(value=spelling))

            ad._migrate_legacy_placeholder_llm_name(target)
            with open(target, encoding="utf-8") as f:
                text = f.read()
            assert ad._LEGACY_PLACEHOLDER_LLM_NAME not in text, (
                f"placeholder spelling {spelling!r} survived the migration"
            )
            assert 'llm_name: ""' in text
            parsed = yaml.safe_load(text)
            assert parsed["llm_name"] == ""
            assert parsed["name"] == "Coder"
            assert parsed["fallback_provider"] == "openrouter"
            assert parsed["tools"] == ["read_file"]

        # Near-misses: exact-equality matching, never a substring/prefix hit.
        for near_miss in ("local-kb-v2", "local_kb"):
            target = os.path.join(tmp_agents_dir, "coder.yaml")
            with open(target, "w", encoding="utf-8") as f:
                f.write(template.format(value=near_miss))
            ad._migrate_legacy_placeholder_llm_name(target)
            with open(target, encoding="utf-8") as f:
                after = f.read()
            assert after == template.format(value=near_miss), (
                f"near-miss {near_miss!r} must never be rewritten"
            )
            assert yaml.safe_load(after)["llm_name"] == near_miss

    def test_shipped_defaults_load_directly(self, tmp_path, monkeypatch):
        """The REAL shipped prompts/default_agents/*.yaml (not a fixture
        copy) load as falsy-llm_name defs: redirects only the agents dir so
        _get_default_agents_src() reads the repo files byte-for-byte."""
        agents_dir = str(tmp_path / "agents")
        monkeypatch.setattr(ad, "_get_agents_dir", lambda: agents_dir)
        defs = ad.load_agent_defs()
        by_name = {d["name"]: d for d in defs}
        for _fname, name, _prompt, _role in _BUILTIN_SEED_SPECS:
            d = by_name.get(name)
            assert d is not None, f"shipped {name} def did not load: {by_name.keys()}"
            assert not d.get("llm_name"), (
                f"shipped {name} must carry a falsy llm_name, got {d.get('llm_name')!r}"
            )

    def test_no_kb_allowlist_gates_the_migration_constant(self):
        """The whole-tree kb-string scan accepts the migration constant ONLY
        via the SPEC-62 adjudicated allowlist: with the allowlist the scan is
        green WITH the constant present; drop the allowlist and it FAILS
        naming utils/agent_defs.py (proves the entry does real gating work,
        not decoration)."""
        import tests.test_no_kb_residuals as kb

        needle = ad._LEGACY_PLACEHOLDER_LLM_NAME
        with open(ad.__file__, encoding="utf-8") as f:
            src = f.read()
        assert needle in src, "premise: the constant must exist in agent_defs.py"

        assert hasattr(kb, "_SPEC62_ADJUDICATED"), "adjudicated allowlist missing"
        expected = os.path.normcase(os.path.abspath(ad.__file__))
        assert expected in kb._SPEC62_ADJUDICATED, (
            f"allowlist must name utils/agent_defs.py, got {list(kb._SPEC62_ADJUDICATED)}"
        )
        assert needle in kb._SPEC62_ADJUDICATED[expected], (
            "allowlist must pin exactly the legacy-placeholder needle"
        )

        # With the allowlist: the real scan over the product tree is green.
        kb.test_source_tree_free_of_kb_strings()

        # Without it: the same scan fails NAMING agent_defs.py.
        saved = dict(kb._SPEC62_ADJUDICATED)
        try:
            kb._SPEC62_ADJUDICATED.clear()
            with pytest.raises(AssertionError) as excinfo:
                kb.test_source_tree_free_of_kb_strings()
        finally:
            kb._SPEC62_ADJUDICATED.update(saved)
        assert "utils" in str(excinfo.value) and "agent_defs.py" in str(excinfo.value)
