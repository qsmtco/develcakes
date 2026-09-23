# tests/test_agent_config_yaml_fallback.py
# Tests for agent/config.py — verifies that load_agent_config uses providers.yaml
# (canonical) when present, falls back to agent.json providers section (with
# deprecation warning) when providers.yaml is empty, and creates an empty
# providers.yaml when neither source has providers.

import json
import os
import pytest

from agent.config import (
    _load_providers_from_yaml,
    ensure_providers_yaml_exists,
    load_agent_config,
)
from utils.providers_store import save_providers, load_providers
from models.providers import ProviderConfig


def _make_provider(name="openai", **overrides) -> ProviderConfig:
    defaults = dict(
        name=name,
        base_url=f"https://api.{name}.example.com/v1",
        api_key="test-key",
        default_model=f"openai/{name}-model",
    )
    defaults.update(overrides)
    return ProviderConfig(**defaults)


class TestProvidersYamlCanonical:
    def test_yaml_used_when_present(self, tmp_config_dir):
        """providers.yaml is the canonical source when it has providers."""
        save_providers([_make_provider("p1"), _make_provider("p2")])
        # Create a minimal agent.json
        agent_json = tmp_config_dir / "agent.json"
        agent_json.write_text(json.dumps({"providers": {"legacy": {}}}))
        # Load and check
        config = load_agent_config(str(agent_json))
        assert "p1" in config.providers
        assert "p2" in config.providers
        assert "legacy" not in config.providers  # agent.json ignored when yaml present

    def test_yaml_takes_precedence_over_agent_json(self, tmp_config_dir, caplog):
        """If both exist, yaml wins, agent.json is silently ignored (no warning)."""
        save_providers([_make_provider("yamlprov")])
        agent_json = tmp_config_dir / "agent.json"
        agent_json.write_text(json.dumps({
            "providers": {"jsonprov": {"base_url": "x", "api_key": "y"}}
        }))
        with caplog.at_level("WARNING"):
            config = load_agent_config(str(agent_json))
        # yaml wins
        assert "yamlprov" in config.providers
        assert "jsonprov" not in config.providers
        # No deprecation warning
        assert "deprecated" not in caplog.text


class TestAgentJsonFallback:
    def test_empty_providers_yaml_returns_empty(self, tmp_config_dir):
        """If providers.yaml is empty, return empty providers dict."""
        providers_yaml = tmp_config_dir / "providers.yaml"
        providers_yaml.write_text("providers: []\n")
        agent_json = tmp_config_dir / "agent.json"
        agent_json.write_text(json.dumps({}))
        config = load_agent_config(str(agent_json))
        assert config.providers == {}

    def test_missing_providers_yaml_returns_empty(self, tmp_config_dir):
        """If providers.yaml doesn't exist, return empty providers dict."""
        agent_json = tmp_config_dir / "agent.json"
        agent_json.write_text(json.dumps({}))
        config = load_agent_config(str(agent_json))
        assert config.providers == {}

    def test_empty_when_both_missing(self, tmp_config_dir):
        """If neither source has providers, return empty dict."""
        agent_json = tmp_config_dir / "agent.json"
        agent_json.write_text(json.dumps({}))
        config = load_agent_config(str(agent_json))
        assert config.providers == {}


class TestEnsureProvidersYamlExists:
    def test_creates_empty_yaml_when_neither_exists(self, tmp_config_dir):
        """First-run state: creates empty providers.yaml."""
        agent_json = tmp_config_dir / "agent.json"
        agent_json.write_text(json.dumps({}))
        yaml_path = ensure_providers_yaml_exists(str(agent_json))
        assert os.path.isfile(yaml_path)
        # Verify it's a valid empty list
        assert load_providers() == []

    def test_does_not_overwrite_existing_yaml(self, tmp_config_dir):
        """If providers.yaml already exists, do not touch it."""
        save_providers([_make_provider("existing")])
        agent_json = tmp_config_dir / "agent.json"
        agent_json.write_text(json.dumps({}))
        ensure_providers_yaml_exists(str(agent_json))
        providers = load_providers()
        assert len(providers) == 1
        assert providers[0].name == "existing"

    def test_always_creates_when_missing(self, tmp_config_dir):
        """providers.yaml is always created when it doesn't exist."""
        agent_json = tmp_config_dir / "agent.json"
        agent_json.write_text(json.dumps({
            "providers": {"legacy": {
                "base_url": "https://x", "api_key": "k", "default_model": "m",
            }}
        }))
        yaml_path = ensure_providers_yaml_exists(str(agent_json))
        assert os.path.isfile(yaml_path), "Should create yaml when it doesn't exist"

    def test_yaml_permissions(self, tmp_config_dir):
        """Created providers.yaml has 0o600 permissions."""
        agent_json = tmp_config_dir / "agent.json"
        agent_json.write_text(json.dumps({}))
        yaml_path = ensure_providers_yaml_exists(str(agent_json))
        mode = os.stat(yaml_path).st_mode & 0o777
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"

    def test_idempotent(self, tmp_config_dir):
        """Calling twice is safe — does not overwrite or error."""
        agent_json = tmp_config_dir / "agent.json"
        agent_json.write_text(json.dumps({}))
        path1 = ensure_providers_yaml_exists(str(agent_json))
        path2 = ensure_providers_yaml_exists(str(agent_json))
        assert path1 == path2
        assert load_providers() == []


class TestLoadAgentConfigIntegration:
    def test_creates_yaml_on_first_run(self, tmp_config_dir):
        """SettingsHandler.__init__ creates providers.yaml if neither source has providers."""
        # Create a minimal agent.json with no providers
        agent_json = tmp_config_dir / "agent.json"
        agent_json.write_text(json.dumps({"providers": {}}))
        # The yaml is created by SettingsHandler, not by load_agent_config
        from ui.handlers.settings_handler import SettingsHandler
        SettingsHandler()
        # providers.yaml should now exist (empty)
        assert load_providers() == []

    def test_load_returns_config_with_no_providers(self, tmp_config_dir):
        """After first-run, load_agent_config returns a valid config with empty providers."""
        agent_json = tmp_config_dir / "agent.json"
        agent_json.write_text(json.dumps({"providers": {}}))
        config = load_agent_config(str(agent_json))
        assert hasattr(config, "providers")
        assert config.providers == {}


class TestUserIdWireUp:
    def test_user_id_loaded_from_config(self, tmp_config_dir):
        """A-4: user_id from agent.json should populate AgentConfig.user_id."""
        agent_json = tmp_config_dir / "agent.json"
        agent_json.write_text(json.dumps({
            "default_provider": "openrouter",
            "default_model": "openrouter/gpt-4o",
            "user_id": "alice@example.com",
        }))
        os.chmod(agent_json, 0o600)
        config = load_agent_config(str(agent_json))
        assert config.user_id == "alice@example.com"

    def test_user_id_empty_when_missing(self, tmp_config_dir):
        """A-4: user_id is empty string when not present in agent.json."""
        agent_json = tmp_config_dir / "agent.json"
        agent_json.write_text(json.dumps({
            "default_provider": "openrouter",
            "default_model": "openrouter/gpt-4o",
        }))
        os.chmod(agent_json, 0o600)
        config = load_agent_config(str(agent_json))
        assert config.user_id == ""
