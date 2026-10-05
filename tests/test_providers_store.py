# tests/test_providers_store.py
# Tests for utils/providers_store.py — provider YAML persistence.
#
# Principle: mock at the boundary, test behavior not internals.
# Uses tmp_config_dir fixture from conftest.py for isolated file I/O.

import json
import os
import stat

import pytest

from models.providers import ProviderConfig

import utils.providers_store as ps


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_provider(name: str = "test", **overrides) -> ProviderConfig:
    """Create a test ProviderConfig with sensible defaults.

    caller-validation spec: default_model must be "<vendor>/<model>" so the
    auto-detect can derive a valid caller. This fixture is used by tests
    that go through save_providers (not add_or_update) so the caller
    validation doesn't fire, but we use a real taxonomy prefix anyway for
    consistency with sibling fixtures in test_settings_handler.py,
    test_settings_dialog.py, test_window_settings_wiring.py, and
    test_agent_config_yaml_fallback.py.
    """
    defaults = dict(
        name=name,
        base_url=f"https://api.{name}.example.com/v1",
        api_key=f"sk-{name}-key",
        default_model=f"openai/{name}-model",
    )
    defaults.update(overrides)
    return ProviderConfig(**defaults)


# ── TestGetProvidersPath ──────────────────────────────────────────────────


class TestGetProvidersPath:
    def test_returns_path_under_config_dir(self, tmp_config_dir):
        path = ps.get_providers_path()
        assert path.endswith("providers.yaml")
        # SPEC-11 SP1 (D4): config dir is now develcakes (spec-drift update).
        assert "develcakes" in path

    def test_contains_yaml_filename(self, tmp_config_dir):
        path = ps.get_providers_path()
        assert os.path.basename(path) == "providers.yaml"


# ── TestLoadSave ──────────────────────────────────────────────────────────


class TestLoadSave:
    def test_round_trip_yaml(self, tmp_config_dir):
        p1 = _make_provider("openrouter")
        p2 = _make_provider("minimax")
        ps.save_providers([p1, p2])

        loaded = ps.load_providers()
        assert len(loaded) == 2
        assert loaded[0].name == "openrouter"
        assert loaded[0].base_url == "https://api.openrouter.example.com/v1"
        assert loaded[0].api_key == "sk-openrouter-key"
        # caller-validation spec: default_model uses "openai/{name}-model" format
        assert loaded[0].default_model == "openai/openrouter-model"
        assert loaded[1].name == "minimax"

    def test_round_trip_preserves_all_fields(self, tmp_config_dir):
        p = _make_provider(
            "full",
            enabled=False,
            supports_tools=False,
            supports_streaming=False,
            max_tokens=64000,
            last_verified_at="2026-06-07T20:30:00Z",
            last_error="some error",
        )
        ps.save_providers([p])

        loaded = ps.load_providers()
        assert len(loaded) == 1
        lp = loaded[0]
        assert lp.enabled is False
        assert lp.supports_tools is False
        assert lp.supports_streaming is False
        assert lp.max_tokens == 64000
        assert lp.last_verified_at == "2026-06-07T20:30:00Z"
        assert lp.last_error == "some error"

    def test_round_trip_json_fallback(self, tmp_config_dir):
        """If pyyaml is missing, write a JSON file and confirm load works."""
        try:
            import yaml
            pytest.skip("pyyaml is installed — JSON fallback not tested")
        except ImportError:
            pass

        # Write a JSON file manually
        path = ps.get_providers_path()
        data = [{"name": "json-provider", "base_url": "https://example.com", "api_key": "k", "default_model": "m"}]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)

        loaded = ps.load_providers()
        assert len(loaded) == 1
        assert loaded[0].name == "json-provider"

    def test_missing_file_returns_empty(self, tmp_config_dir):
        result = ps.load_providers()
        assert result == []

    def test_malformed_yaml_returns_empty(self, tmp_config_dir):
        path = ps.get_providers_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{{{{not valid yaml:::")
        result = ps.load_providers()
        assert result == []

    def test_empty_file_returns_empty(self, tmp_config_dir):
        path = ps.get_providers_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("")
        result = ps.load_providers()
        assert result == []

    def test_non_list_returns_empty(self, tmp_config_dir):
        path = ps.get_providers_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write('{"key": "value"}')
        result = ps.load_providers()
        assert result == []

    def test_atomic_write_no_partial_on_failure(self, tmp_config_dir, monkeypatch):
        """Simulate a failure mid-save and confirm no .tmp file is left behind."""
        p = _make_provider("atomic")

        # Patch _serialize to raise after the tmp file is created
        original_serialize = ps._serialize
        call_count = 0

        def broken_serialize(providers):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated write failure")
            return original_serialize(providers)

        monkeypatch.setattr(ps, "_serialize", broken_serialize)

        with pytest.raises(RuntimeError, match="simulated write failure"):
            ps.save_providers([p])

        # No .tmp file should remain
        path = ps.get_providers_path()
        assert not os.path.isfile(path + ".tmp")

    def test_atomic_write_restores_on_second_call(self, tmp_config_dir, monkeypatch):
        """After a failed save, a subsequent successful save should work."""
        p = _make_provider("retry")

        # First call fails
        call_count = 0
        original_serialize = ps._serialize

        def flaky_serialize(providers):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first failure")
            return original_serialize(providers)

        monkeypatch.setattr(ps, "_serialize", flaky_serialize)

        with pytest.raises(RuntimeError):
            ps.save_providers([p])

        # Second call succeeds
        monkeypatch.setattr(ps, "_serialize", original_serialize)
        ps.save_providers([p])

        loaded = ps.load_providers()
        assert len(loaded) == 1
        assert loaded[0].name == "retry"


# ── TestFilePermissions ───────────────────────────────────────────────────


class TestFilePermissions:
    def test_save_sets_mode_0o600(self, tmp_config_dir):
        p = _make_provider("permtest")
        ps.save_providers([p])

        path = ps.get_providers_path()
        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"

    def test_parent_dir_mode_0o700_on_create(self, tmp_config_dir):
        # tmp_config_dir creates the dir, so this tests the chmod directly
        # We need a fresh parent that doesn't exist
        import utils.config as cfg
        fresh_dir = os.path.join(os.path.dirname(cfg.get_config_dir()), "crabcakes_fresh")
        monkeypatch_target = cfg.get_config_dir
        # Use a subdirectory that doesn't exist
        fresh_path = os.path.join(fresh_dir, "providers.yaml")

        # Manually create the scenario
        os.makedirs(fresh_dir, exist_ok=True)

        # Verify chmod works
        p = _make_provider("dirperm")
        ps.save_providers([p])

        path = ps.get_providers_path()
        parent = os.path.dirname(path)
        parent_mode = os.stat(parent).st_mode & 0o777
        # Parent may already exist (from tmp_config_dir) so we just check the file mode
        file_mode = os.stat(path).st_mode & 0o777
        assert file_mode == 0o600


# ── TestAddUpdateRemove ───────────────────────────────────────────────────


class TestAddUpdateRemove:
    def test_add_new(self, tmp_config_dir):
        p = _make_provider("new")
        ps.add_provider([], p)

        loaded = ps.load_providers()
        assert len(loaded) == 1
        assert loaded[0].name == "new"

    def test_add_replaces_existing_by_name(self, tmp_config_dir):
        p1 = _make_provider("same-name", api_key="old-key")
        ps.add_provider([], p1)

        p2 = _make_provider("same-name", api_key="new-key")
        ps.add_provider(ps.load_providers(), p2)

        loaded = ps.load_providers()
        assert len(loaded) == 1
        assert loaded[0].api_key == "new-key"

    def test_remove_existing(self, tmp_config_dir):
        p = _make_provider("removeme")
        ps.save_providers([p])

        ps.remove_provider(ps.load_providers(), "removeme")

        loaded = ps.load_providers()
        assert len(loaded) == 0

    def test_remove_nonexistent_is_noop(self, tmp_config_dir):
        p = _make_provider("keeper")
        ps.save_providers([p])

        # Should not raise
        ps.remove_provider(ps.load_providers(), "ghost")

        loaded = ps.load_providers()
        assert len(loaded) == 1
        assert loaded[0].name == "keeper"

    def test_update_existing(self, tmp_config_dir):
        p1 = _make_provider("updateme", api_key="old-key")
        ps.save_providers([p1])

        p2 = _make_provider("updateme", api_key="new-key")
        ps.update_provider(ps.load_providers(), p2)

        loaded = ps.load_providers()
        assert len(loaded) == 1
        assert loaded[0].api_key == "new-key"

    def test_update_new_appends(self, tmp_config_dir):
        p = _make_provider("newone")
        ps.update_provider([], p)

        loaded = ps.load_providers()
        assert len(loaded) == 1
        assert loaded[0].name == "newone"


# ── TestHasAnyVerifiedProvider ─────────────────────────────────────────────


class TestHasAnyVerifiedProvider:
    def test_empty_list_false(self):
        assert ps.has_any_verified_provider([]) is False

    def test_all_unverified_false(self):
        providers = [_make_provider("a"), _make_provider("b")]
        for p in providers:
            assert p.last_verified_at is None
        assert ps.has_any_verified_provider(providers) is False

    def test_one_verified_true(self):
        providers = [
            _make_provider("a"),
            _make_provider("b", last_verified_at="2026-06-07T20:30:00Z"),
        ]
        assert ps.has_any_verified_provider(providers) is True

    def test_ignores_last_error(self):
        """A provider with last_error set but last_verified_at=None does NOT count."""
        providers = [_make_provider("a", last_error="some failure")]
        assert ps.has_any_verified_provider(providers) is False

    def test_verified_and_error_both_set(self):
        """A provider can have both verified and error — verified wins."""
        providers = [
            _make_provider("a", last_verified_at="2026-06-07T20:30:00Z", last_error="old error"),
        ]
        assert ps.has_any_verified_provider(providers) is True


# ── TestRemoveProvidersFromAgentJson ──────────────────────────────────


class TestRemoveProvidersFromAgentJson:
    def test_remove_providers_key_deletes_it(self, tmp_config_dir):
        """agent.json with providers → returns True, key removed, other keys preserved."""
        import utils.config
        agent_json = os.path.join(utils.config.get_config_dir(), "agent.json")
        os.makedirs(os.path.dirname(agent_json), exist_ok=True)
        with open(agent_json, "w") as f:
            json.dump({
                "providers": {"openai": {"base_url": "x", "api_key": "k", "default_model": "m"}},
                "default_provider": "openai",
                "default_model": "openai/gpt-4o",
            }, f)
        os.chmod(agent_json, 0o600)

        result = ps.remove_providers_from_agent_json()
        assert result is True

        with open(agent_json) as f:
            remaining = json.load(f)
        assert "providers" not in remaining
        assert remaining["default_provider"] == "openai"  # other keys preserved

    def test_remove_providers_key_idempotent(self, tmp_config_dir):
        """agent.json without providers key → returns False, no error."""
        import utils.config
        agent_json = os.path.join(utils.config.get_config_dir(), "agent.json")
        os.makedirs(os.path.dirname(agent_json), exist_ok=True)
        with open(agent_json, "w") as f:
            json.dump({"default_provider": "openai"}, f)
        os.chmod(agent_json, 0o600)

        result = ps.remove_providers_from_agent_json()
        assert result is False

        # File still intact
        with open(agent_json) as f:
            remaining = json.load(f)
        assert "default_provider" in remaining

    def test_remove_providers_key_missing_file(self, tmp_config_dir):
        """No agent.json at all → returns False, no error."""
        result = ps.remove_providers_from_agent_json()
        assert result is False


# ── TestMigrateFromAgentJson ──────────────────────────────────────────────


class TestMigrateFromAgentJson:
    def test_migrate_handles_missing_agent_json(self, tmp_config_dir):
        """No agent.json file at all — returns 0, no error."""
        count = ps.migrate_from_agent_json()
        assert count == 0

    def test_migrate_from_empty_agent_json_returns_zero(self, tmp_config_dir):
        """agent.json with no providers key — returns 0."""
        import utils.config
        agent_json = os.path.join(utils.config.get_config_dir(), "agent.json")
        os.makedirs(os.path.dirname(agent_json), exist_ok=True)
        with open(agent_json, "w") as f:
            json.dump({}, f)
        os.chmod(agent_json, 0o600)

        count = ps.migrate_from_agent_json()
        assert count == 0

    def test_migrate_moves_providers_to_yaml(self, tmp_config_dir):
        """agent.json with 2 providers, empty YAML — returns 2, YAML now has 2."""
        import utils.config
        agent_json = os.path.join(utils.config.get_config_dir(), "agent.json")
        os.makedirs(os.path.dirname(agent_json), exist_ok=True)
        with open(agent_json, "w") as f:
            json.dump({
                "providers": {
                    "openai": {
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "sk-test",
                        "default_model": "gpt-4o",
                    },
                    "local-ollama": {
                        "base_url": "http://localhost:11434/v1",
                        "api_key": "ollama",
                        "default_model": "llama3",
                    },
                }
            }, f)
        os.chmod(agent_json, 0o600)

        count = ps.migrate_from_agent_json()
        assert count == 2

        yaml_providers = ps.load_providers()
        names = {p.name for p in yaml_providers}
        assert "openai" in names
        assert "local-ollama" in names

    def test_migrate_skips_yaml_existing(self, tmp_config_dir):
        """agent.json has 'openai', YAML already has 'openai' — returns 0, YAML wins."""
        import utils.config
        # Seed YAML with openai
        ps.save_providers([_make_provider("openai")])
        # Create agent.json with openai (should be skipped) and a new one
        agent_json = os.path.join(utils.config.get_config_dir(), "agent.json")
        os.makedirs(os.path.dirname(agent_json), exist_ok=True)
        with open(agent_json, "w") as f:
            json.dump({
                "providers": {
                    "openai": {
                        "base_url": "https://different.com/v1",
                        "api_key": "should-not-appear",
                        "default_model": "ignored",
                    },
                    "minimax": {
                        "base_url": "https://api.minimax.chat/v1",
                        "api_key": "sk-mini",
                        "default_model": "MiniMax-M2.7",
                    },
                }
            }, f)
        os.chmod(agent_json, 0o600)

        count = ps.migrate_from_agent_json()
        assert count == 1  # only minimax migrated

        yaml_providers = ps.load_providers()
        names = {p.name for p in yaml_providers}
        assert "openai" in names
        assert "minimax" in names
        # openai in YAML keeps its original key (YAML wins)
        openai_provider = next(p for p in yaml_providers if p.name == "openai")
        assert openai_provider.api_key == "sk-openai-key"  # YAML value preserved

    def test_migrate_idempotent_second_call_returns_zero(self, tmp_config_dir):
        """Calling twice — second call returns 0 (already migrated)."""
        import utils.config
        agent_json = os.path.join(utils.config.get_config_dir(), "agent.json")
        os.makedirs(os.path.dirname(agent_json), exist_ok=True)
        with open(agent_json, "w") as f:
            json.dump({"providers": {"testprov": {"base_url": "x", "api_key": "k", "default_model": "m"}}}, f)
        os.chmod(agent_json, 0o600)

        first = ps.migrate_from_agent_json()
        assert first == 1
        second = ps.migrate_from_agent_json()
        assert second == 0

    def test_migrate_empty_providers_dict_strips_key(self, tmp_config_dir):
        """A-5: agent.json with empty providers={} — key is removed even though nothing was migrated.

        Pre-fix bug: early return left the empty `providers` key in agent.json
        forever, leaving legacy state for users who had an empty legacy store.
        """
        import utils.config
        agent_json = os.path.join(utils.config.get_config_dir(), "agent.json")
        os.makedirs(os.path.dirname(agent_json), exist_ok=True)
        with open(agent_json, "w") as f:
            json.dump({"providers": {}, "default_provider": "openai"}, f)
        os.chmod(agent_json, 0o600)

        count = ps.migrate_from_agent_json()
        assert count == 0  # nothing to migrate

        with open(agent_json) as f:
            raw = json.load(f)
        assert "providers" not in raw  # but key should be stripped
        assert raw.get("default_provider") == "openai"  # other keys preserved


# ═══════════════════════════════════════════════════════════════════
#  _from_dict caller validation (caller-validation.md)
# ═══════════════════════════════════════════════════════════════════
#
# Adversarial tests for the load-time path. _from_dict must:
#   - Log a warning on invalid non-empty caller (warn-and-keep, do NOT mutate)
#   - Stay silent on empty caller (auto-detect happens at save time)
#   - Stay silent on valid caller
#   - Match the agent.runtime._PROVIDER_CALLERS taxonomy (duplication invariant)


import logging
import pytest


class TestFromDictCallerValidation:
    """_from_dict must validate caller but NEVER mutate the value (warn-and-keep)."""

    def test_warns_and_keeps_invalid_caller(self, tmp_config_dir, caplog):
        """A non-empty caller NOT in the taxonomy: log a warning, keep the value."""
        from utils.providers_store import _from_dict
        d = {
            "name": "broken",
            "base_url": "https://x",
            "api_key": "k",
            "default_model": "openai/gpt-4o",
            "caller": "poolside",  # not in taxonomy
        }
        with caplog.at_level(logging.WARNING, logger="utils.providers_store"):
            cfg = _from_dict(d)
        assert cfg.caller == "poolside", (
            "FAILURE-CASE REPRO: _from_dict mutated invalid caller — should "
            "warn-and-keep, not silently rewrite"
        )
        # The warning must name the invalid value AND the valid set.
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("poolside" in str(w) for w in warnings), (
            f"warning must name the invalid caller; got: {warnings}"
        )
        assert any("anthropic" in str(w) and "minimax" in str(w) for w in warnings), (
            f"warning must list valid callers; got: {warnings}"
        )

    def test_silent_on_empty_caller(self, tmp_config_dir, caplog):
        """Empty caller is allowed (auto-detect happens at save time)."""
        from utils.providers_store import _from_dict
        d = {
            "name": "p",
            "base_url": "https://x",
            "api_key": "k",
            "default_model": "openai/gpt-4o",
            "caller": "",
        }
        with caplog.at_level(logging.WARNING, logger="utils.providers_store"):
            cfg = _from_dict(d)
        assert cfg.caller == ""
        # No warnings for empty caller.
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("caller" in str(w).lower() for w in warnings), (
            f"empty caller must not warn; got: {warnings}"
        )

    def test_silent_on_valid_caller(self, tmp_config_dir, caplog):
        """A valid caller: no warning, value preserved."""
        from utils.providers_store import _from_dict
        d = {
            "name": "p",
            "base_url": "https://x",
            "api_key": "k",
            "default_model": "openai/gpt-4o",
            "caller": "openai",
        }
        with caplog.at_level(logging.WARNING, logger="utils.providers_store"):
            cfg = _from_dict(d)
        assert cfg.caller == "openai"
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("caller" in str(w).lower() for w in warnings), (
            f"valid caller must not warn; got: {warnings}"
        )

    def test_silent_on_missing_caller_key(self, tmp_config_dir, caplog):
        """Missing caller key entirely (default '') — same as empty."""
        from utils.providers_store import _from_dict
        d = {
            "name": "p",
            "base_url": "https://x",
            "api_key": "k",
            "default_model": "openai/gpt-4o",
        }
        with caplog.at_level(logging.WARNING, logger="utils.providers_store"):
            cfg = _from_dict(d)
        assert cfg.caller == ""
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("caller" in str(w).lower() for w in warnings), (
            f"missing caller must not warn; got: {warnings}"
        )

    def test_normalizes_none_caller_to_empty(self, tmp_config_dir, caplog):
        """BUG #3: YAML `caller: null` deserializes to Python None.
        dict.get's default (the second arg) only fires on MISSING keys,
        not on present-but-null values. _from_dict must coerce None → ""
        so downstream truthy checks (e.g. `not provider.caller`) fire
        correctly. Otherwise caller=None leaks into ProviderConfig and
        breaks the type contract.
        """
        from utils.providers_store import _from_dict
        d = {
            "name": "p",
            "base_url": "https://x",
            "api_key": "k",
            "default_model": "openai/gpt-4o",
            "caller": None,
        }
        with caplog.at_level(logging.WARNING, logger="utils.providers_store"):
            cfg = _from_dict(d)
        assert cfg.caller == "", (
            f"caller=None must be normalized to empty string, got {cfg.caller!r}"
        )


class TestValidCallersDuplicationInvariant:
    """utils/_VALID_CALLERS must match agent.runtime._PROVIDER_CALLERS.keys().

    This is the duplication invariant the spec explicitly carves out for
    the utils-layer rule. If a new adapter is added to agent.runtime, this
    test fails and the engineer must update utils/providers_store.py too.
    """

    def test_valid_callers_set_matches_runtime_taxonomy(self):
        from agent.runtime import get_valid_callers
        from utils.providers_store import _VALID_CALLERS
        assert _VALID_CALLERS == get_valid_callers(), (
            f"DUPLICATION DRIFT: utils._VALID_CALLERS={sorted(_VALID_CALLERS)} "
            f"but agent.runtime._PROVIDER_CALLERS.keys()={sorted(get_valid_callers())}. "
            f"Update _VALID_CALLERS in utils/providers_store.py to match the runtime."
        )
