# tests/test_agent_config_yaml_fallback.py
# Tests for agent/config.py — verifies that load_agent_config uses providers.yaml
# (canonical) when present, falls back to agent.json providers section (with
# deprecation warning) when providers.yaml is empty, and creates an empty
# providers.yaml when neither source has providers.

import json
import os

from agent.config import (
    ensure_providers_yaml_exists,
    load_agent_config,
)
from models.providers import ProviderConfig
from utils.providers_store import load_providers, save_providers


def _make_provider(name="openai", **overrides) -> ProviderConfig:
    defaults = {
        "name": name,
        "base_url": f"https://api.{name}.example.com/v1",
        "api_key": "test-key",
        "default_model": f"openai/{name}-model",
    }
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


class TestLoadProvidersFromYamlPrefixKeys:
    """Provider-card fix: _load_providers_from_yaml must not let a bare prefix
    key override all same-prefix cards (last-writer-wins was the bug)."""

    @staticmethod
    def _load(monkeypatch, cards):
        from agent.config import _load_providers_from_yaml
        monkeypatch.setattr("utils.providers_store.load_providers", lambda: cards)
        return _load_providers_from_yaml("ignored-path", {})

    def test_two_shared_prefix_cards_register_by_display_and_full_model(self, monkeypatch):
        cards = [
            ProviderConfig(name="GLM 5.3", base_url="x", api_key="k",
                           default_model="zai/glm-5.3", caller="zai",
                           context_window=262_144),
            ProviderConfig(name="GLM 5.3 Flash", base_url="x", api_key="k",
                           default_model="zai/glm-5.3-flash", caller="zai",
                           context_window=32_768),
        ]
        result = self._load(monkeypatch, cards)
        assert "GLM 5.3" in result
        assert "GLM 5.3 Flash" in result
        assert "zai/glm-5.3" in result
        assert "zai/glm-5.3-flash" in result
        # Ambiguous bare prefix is NOT written — no last-writer-wins key.
        assert "zai" not in result
        assert result["GLM 5.3"].context_window == 262_144
        assert result["GLM 5.3 Flash"].context_window == 32_768
        assert result["zai/glm-5.3"].context_window == 262_144
        assert result["zai/glm-5.3-flash"].context_window == 32_768

    def test_single_owner_prefix_key_kept_for_backward_compat(self, monkeypatch):
        cards = [
            ProviderConfig(name="GLM 5.3", base_url="x", api_key="k",
                           default_model="zai/glm-5.3", caller="zai"),
        ]
        result = self._load(monkeypatch, cards)
        assert "zai" in result
        assert result["zai"] is result["GLM 5.3"]
        assert "zai/glm-5.3" in result

    def test_non_chat_fixed_card_shares_prefix_without_stealing_bare_key(
        self, monkeypatch
    ):
        """SPEC-55 BUG #2: an ImproveModel card sharing an openrouter/* prefix
        must not drop the chat card's bare-prefix key — get_api_key('openrouter')
        keeps resolving to the chat card."""
        from utils.improve import IMPROVE_MODEL_PROVIDER

        cards = [
            ProviderConfig(name="OpenRouter", base_url="https://or.example/v1",
                           api_key="sk-chat-key",
                           default_model="openrouter/some-model", caller="openrouter"),
            ProviderConfig(name=IMPROVE_MODEL_PROVIDER,
                           base_url="https://or.example/v1",
                           api_key="sk-improve-key",
                           default_model="openrouter/improve-model", caller=""),
        ]
        result = self._load(monkeypatch, cards)
        # The chat card is the single owner of the 'openrouter' prefix — the
        # non-chat ImproveModel card is excluded from ownership entirely.
        assert "openrouter" in result
        assert result["openrouter"] is result["OpenRouter"]
        assert result["openrouter"].api_key == "sk-chat-key"
        # ImproveModel still registers by fixed name and full model string.
        assert IMPROVE_MODEL_PROVIDER in result
        assert "openrouter/improve-model" in result
        # get_api_key('openrouter') still returns the chat card's key.
        import types
        monkeypatch.setattr(
            "agent.config.load_agent_config",
            lambda: types.SimpleNamespace(providers=result),
        )
        from agent.config import get_api_key
        assert get_api_key("openrouter") == "sk-chat-key"


class TestSetAgentSettingPreservesConfig:
    """SPEC-62 Fix Round 1 (Debugger #1): set_agent_setting on a MISSING or
    corrupt agent.json must seed raw from the same defaults
    _create_default_config writes, so the key write never collapses the file
    to one key. Existing valid files keep their byte-level preserve path.

    Config resolution is isolated by the conftest autouse
    _isolate_config_resolution fixture (HOME + XDG_CONFIG_HOME → tmp_path);
    set_agent_setting resolves the file through utils.config.get_config_dir().
    """

    @staticmethod
    def _agent_json_path() -> str:
        from utils.config import get_config_dir
        return os.path.join(get_config_dir(), "agent.json")

    @staticmethod
    def _read_json(path: str) -> dict:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_missing_file_set_writes_defaults_plus_key(self):
        """(a) Missing agent.json → set one key → the file equals the
        shipped defaults plus that key (never {key: value} alone)."""
        from agent.config import set_agent_setting

        path = self._agent_json_path()
        assert not os.path.exists(path)
        set_agent_setting("default_provider", "openrouter")

        raw = self._read_json(path)
        assert raw["default_provider"] == "openrouter"
        for key in (
            "default_model", "max_tool_iterations", "tool_iteration_hard_cap",
            "tool_timeout_seconds", "cost_limit", "step_limit",
            "fallback_provider", "fallback_model", "user_id",
            "_comment", "_security",
        ):
            assert key in raw, (
                f"missing-file key write collapsed agent.json: {key!r} absent"
            )
        assert raw["max_tool_iterations"] == 50
        assert raw["tool_iteration_hard_cap"] == 100
        assert raw["tool_timeout_seconds"] == 120
        assert raw["cost_limit"] == 5.0
        assert raw["step_limit"] == 100
        assert load_agent_config(path).max_tool_iterations == 50

    def test_corrupt_file_set_preserves_recognizable_values(self):
        """(b) Corrupt (non-dict / unreadable) agent.json → set one key → the
        rewrite carries the default set plus the key; the file is not
        collapsed to a single entry."""
        from agent.config import set_agent_setting

        path = self._agent_json_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ this is not valid JSON")  # corrupt payload

        set_agent_setting("default_provider", "openrouter")
        raw = self._read_json(path)
        assert raw["default_provider"] == "openrouter"
        assert raw["cost_limit"] == 5.0
        assert raw["max_tool_iterations"] == 50
        assert len(raw) > 1, "corrupt-file rewrite must not be a single-key file"

    def test_existing_valid_file_still_preserves_user_keys(self):
        """Regression pin for the already-correct path: a valid agent.json
        with user keys keeps them byte-level — only the target key changes,
        all other keys and values untouched."""
        from agent.config import set_agent_setting

        path = self._agent_json_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        user_cfg = {
            "default_provider": "zai",
            "default_model": "zai/glm-5.3",
            "cost_limit": 5.0,
            "step_limit": 100,
            "user_id": "alice@example.com",
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(user_cfg, f)
        before = self._read_json(path)

        set_agent_setting("default_provider", "openrouter")
        after = self._read_json(path)

        assert after["default_provider"] == "openrouter"
        for key, value in before.items():
            if key == "default_provider":
                continue
            assert after.get(key) == value, (
                f"valid-file path must preserve {key!r}"
            )
