# tests/test_startup_checks.py
# SPEC-63 §2.5 (v3) — the env-key warning: ADD-not-REPLACE the generic
# line, vendor-scoped trigger via the single preset_vendor_present helper,
# store-empty clause, hermetic env param, key values never rendered.

from __future__ import annotations

import pytest

from models.providers import ProviderConfig
from utils.startup_checks import startup_warnings


def _card(name="openai", base_url="https://api.openai.com/v1", **kw) -> ProviderConfig:
    return ProviderConfig(
        name=name, base_url=base_url, api_key="sk-test",
        default_model="gpt-4o", caller="openai", **kw,
    )


def _write_store(tmp_config_dir, cards):
    import utils.providers_store as ps
    if cards:
        ps.save_providers(cards)
    # empty: write nothing — load_providers returns [] for a missing file


def _env_warnings(env, tmp_config_dir):
    return startup_warnings(env=env)


class TestCleanInstallNoEnvKeys:
    def test_no_env_line_on_clean_install(self, tmp_config_dir):
        _write_store(tmp_config_dir, [])
        lines = _env_warnings({}, tmp_config_dir)
        assert not any("provider key" in w for w in lines)

    def test_generic_line_still_emitted_on_empty_store(self, tmp_config_dir):
        _write_store(tmp_config_dir, [])
        lines = _env_warnings({}, tmp_config_dir)
        assert "no enabled provider card" in lines


class TestEnvLineAddedNotReplaced:
    def test_env_key_and_empty_store_yields_both_lines(self, tmp_config_dir):
        _write_store(tmp_config_dir, [])
        lines = _env_warnings({"OPENAI_API_KEY": "sk-abc"}, tmp_config_dir)
        env_lines = [w for w in lines if "provider key" in w]
        assert len(env_lines) == 1
        assert "no enabled provider card" in lines
        assert "but no provider card is configured yet" in env_lines[0]

    def test_store_empty_clause_only_when_no_cards(self, tmp_config_dir):
        # Any card at all (even another vendor) drops the store-empty clause.
        _write_store(tmp_config_dir, [_card(name="mine", base_url="https://api.example.com/v1")])
        lines = _env_warnings({"OPENAI_API_KEY": "sk-abc"}, tmp_config_dir)
        env_lines = [w for w in lines if "provider key" in w]
        assert len(env_lines) == 1
        assert "but no provider card is configured yet" not in env_lines[0]

    def test_env_key_with_existing_other_vendor_card(self, tmp_config_dir):
        # A card exists (other vendor) → env line present (vendor missing)
        # but WITHOUT the store-empty clause; generic line untouched.
        _write_store(tmp_config_dir, [_card(name="mine", base_url="https://api.example.com/v1")])
        lines = _env_warnings({"OPENAI_API_KEY": "sk-abc"}, tmp_config_dir)
        env_lines = [w for w in lines if "provider key" in w]
        assert len(env_lines) == 1
        assert "but no provider card is configured yet" not in env_lines[0]
        assert "open Settings → Providers" in env_lines[0]


class TestVendorScopedTrigger:
    def test_openai_card_suppresses_env_line(self, tmp_config_dir):
        _write_store(tmp_config_dir, [_card()])
        lines = _env_warnings({"OPENAI_API_KEY": "sk-abc"}, tmp_config_dir)
        assert not any("provider key" in w for w in lines)

    def test_renamed_card_same_base_url_suppresses(self, tmp_config_dir):
        # Debugger #18 — rename must not resurrect the chip/warning.
        _write_store(tmp_config_dir, [_card(name="my-llm")])
        lines = _env_warnings({"OPENAI_API_KEY": "sk-abc"}, tmp_config_dir)
        assert not any("provider key" in w for w in lines)

    def test_disabled_card_still_counts_as_vendor_present(self, tmp_config_dir):
        _write_store(tmp_config_dir, [_card(enabled=False)])
        lines = _env_warnings({"OPENAI_API_KEY": "sk-abc"}, tmp_config_dir)
        assert not any("provider key" in w for w in lines)
        # the generic enabled-line still fires
        assert "no enabled provider card" in lines

    def test_two_keys_name_them(self, tmp_config_dir):
        _write_store(tmp_config_dir, [])
        lines = _env_warnings(
            {"OPENAI_API_KEY": "k1", "ANTHROPIC_API_KEY": "k2"},
            tmp_config_dir,
        )
        env_lines = [w for w in lines if "provider key" in w]
        assert len(env_lines) == 1
        assert "Found 2 provider key(s)" in env_lines[0]
        assert "OPENAI_API_KEY" in env_lines[0]  # first by ENV_KEY_MAP order


class TestKeyValuesNeverRendered:
    @pytest.mark.parametrize("env", [
        {"OPENAI_API_KEY": "sk-SECRET-123"},
        {"ANTHROPIC_API_KEY": "sk-ant-SECRET-xyz"},
        {"OPENROUTER_API_KEY": "sk-or-v1-SECRET"},
    ])
    def test_values_absent_from_all_lines(self, env, tmp_config_dir):
        _write_store(tmp_config_dir, [])
        lines = _env_warnings(env, tmp_config_dir)
        for secret in env.values():
            assert secret not in "\n".join(lines)
        # names ARE rendered
        assert any(name in "\n".join(lines) for name in env)


class TestBackCompatNoArgCall:
    def test_no_arg_call_uses_os_environ(self, tmp_config_dir, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        _write_store(tmp_config_dir, [])
        lines = startup_warnings()
        assert not any("provider key" in w for w in lines)

    def test_no_arg_call_with_env_key(self, tmp_config_dir, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-xyz")
        _write_store(tmp_config_dir, [])
        lines = startup_warnings()
        assert any("provider key" in w for w in lines)
        assert not any("sk-xyz" in w for w in lines)
