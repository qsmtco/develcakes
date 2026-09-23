# tests/test_agent_builder_fallback.py
# Tests for the fallback provider fields in the agent builder.
#
# Tests cover:
#   - Handler create_new() includes fallback_provider (and NOT fallback_model)
#   - Handler save/load round-trips fallback_provider through YAML
#   - _normalize_fallback_fields ensures fallback_provider key exists after parsing
#   - Old YAMLs with fallback_model still load (backward compat)

from __future__ import annotations

import os
from unittest import mock

import pytest

from ui.handlers.agent_builder_handler import AgentBuilderHandler
from utils.agent_defs import (
    _normalize_fallback_fields,
    _parse_agent_file,
    save_agent_def,
    load_agent_def,
)


@pytest.fixture
def temp_config_dir(tmp_path, monkeypatch):
    """Redirect config dir to temp."""
    config_dir = tmp_path / "crabcakes-config"
    config_dir.mkdir()

    def mock_get_config_dir():
        return str(config_dir)

    monkeypatch.setattr("utils.config.get_config_dir", mock_get_config_dir)
    return config_dir


class TestHandlerCreateNew:
    def test_create_new_includes_fallback_provider(self):
        """create_new() returns dict with fallback_provider (and no fallback_model)."""
        handler = AgentBuilderHandler()
        template = handler.create_new()
        assert "fallback_provider" in template
        assert template["fallback_provider"] is None
        assert "fallback_model" not in template


class TestNormalizeFallbackFields:
    def test_adds_missing_provider_key(self):
        """_normalize_fallback_fields adds fallback_provider if missing."""
        d = {"name": "TestAgent"}
        _normalize_fallback_fields(d)
        assert "fallback_provider" in d
        assert d["fallback_provider"] is None

    def test_preserves_existing_provider(self):
        """_normalize_fallback_fields preserves existing fallback_provider value."""
        d = {"name": "TestAgent", "fallback_provider": "openrouter"}
        _normalize_fallback_fields(d)
        assert d["fallback_provider"] == "openrouter"

    def test_does_not_add_fallback_model(self):
        """_normalize_fallback_fields does not add fallback_model (removed in 2026-06-15)."""
        d = {"name": "TestAgent"}
        _normalize_fallback_fields(d)
        assert "fallback_model" not in d


class TestYamlRoundTrip:
    def test_save_load_fallback_fields(self, temp_config_dir):
        """Agent YAML with fallback_provider survives save → load round-trip."""
        agent_def = {
            "name": "TestAgent",
            "emoji": "🤖",
            "role": "test",
            "prompts": ["system/coder.md"],
            "tools": ["read_file", "list_files"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
            "self_improvement": {},
        }

        save_agent_def(agent_def)

        loaded = load_agent_def("TestAgent")
        assert loaded is not None
        assert loaded.get("fallback_provider") == "openrouter"

    def test_save_load_without_fallback_fields(self, temp_config_dir):
        """Agent YAMLs without fallback_provider are skipped at load time.

        Every agent must have a fallback. load_agent_defs() filters out
        invalid defs; load_agent_def() returns None for them.
        """
        agent_def = {
            "name": "SimpleAgent",
            "emoji": "🤖",
            "role": "simple",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "self_improvement": {},
        }

        save_agent_def(agent_def)

        loaded = load_agent_def("SimpleAgent")
        assert loaded is None  # filtered out — missing fallback_provider

    def test_save_load_fallback_none(self, temp_config_dir):
        """Explicitly None fallback_provider is filtered at load time.

        A literal null/None is treated the same as a missing field — both
        are invalid under the "all agents must have a fallback" contract.
        """
        agent_def = {
            "name": "KBOnly",
            "emoji": "🤖",
            "role": "kb",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": None,
            "self_improvement": {},
        }

        save_agent_def(agent_def)

        loaded = load_agent_def("KBOnly")
        assert loaded is None  # filtered out — fallback_provider is None

    def test_save_does_not_emit_fallback_model(self, temp_config_dir):
        """save_agent_def() never writes fallback_model to the YAML file."""
        agent_def = {
            "name": "NoModel",
            "emoji": "🤖",
            "role": "nomodel",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
            "self_improvement": {},
        }

        filepath = save_agent_def(agent_def)

        # Read the raw file and assert fallback_model is not a key
        with open(filepath, encoding="utf-8") as f:
            raw = f.read()
        assert "fallback_model" not in raw

    def test_old_yaml_with_fallback_model_loads(self, temp_config_dir):
        """Old agent YAMLs with fallback_model load without error (field is ignored)."""
        agents_dir = temp_config_dir / "agents"
        agents_dir.mkdir(exist_ok=True)
        legacy_path = agents_dir / "legacy-agent.yaml"
        legacy_path.write_text(
            "name: LegacyAgent\n"
            "emoji: 🤖\n"
            "role: legacy\n"
            "prompts: [system/coder.md]\n"
            "tools: [read_file]\n"
            "llm_name: openrouter\n"
            "fallback_provider: openrouter\n"
            "fallback_model: openrouter/owl-alpha\n"
            "self_improvement: {}\n"
        )
        loaded = load_agent_def("LegacyAgent")
        assert loaded is not None
        assert loaded.get("fallback_provider") == "openrouter"


class TestHandlerSaveLoad:
    def test_handler_save_load_round_trip(self, temp_config_dir):
        """Handler.save() and load_for_edit() round-trip fallback_provider."""
        handler = AgentBuilderHandler()

        agent_def = {
            "name": "RoundTrip",
            "emoji": "🤖",
            "role": "roundtrip",
            "prompts": ["system/coder.md"],
            "tools": ["read_file", "list_files", "search_files"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
            "self_improvement": {},
        }

        ok, errors = handler.save(agent_def)
        assert ok, f"Save failed: {errors}"

        loaded = handler.load_for_edit("RoundTrip")
        assert loaded is not None
        assert loaded.get("fallback_provider") == "openrouter"
