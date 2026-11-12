# tests/test_env_provider_keys.py
# SPEC-63 §2.5 — industry-standard env-key detection: map pinned, blank
# treated as absent, DEVELCAKES_-prefixed names NOT honored (documented
# deviation from utils.config.get_env), value never surfaced.

from __future__ import annotations

from utils.env_provider_keys import ENV_KEY_MAP, detect_env_keys


class TestEnvKeyMap:
    def test_map_pinned(self):
        assert ENV_KEY_MAP == {
            "OPENAI_API_KEY": "openai",
            "ANTHROPIC_API_KEY": "anthropic",
            "OPENROUTER_API_KEY": "openrouter",
        }


class TestDetectEnvKeys:
    def test_empty_env(self):
        assert detect_env_keys({}) == []

    def test_missing_var_absent(self):
        assert detect_env_keys({"SOMETHING_ELSE": "x"}) == []

    def test_present_non_blank(self):
        out = detect_env_keys({"OPENAI_API_KEY": "sk-abc"})
        assert out == [("OPENAI_API_KEY", "openai", "sk-abc")]

    def test_blank_string_is_absent(self):
        assert detect_env_keys({"OPENAI_API_KEY": ""}) == []

    def test_whitespace_only_is_absent(self):
        assert detect_env_keys({"OPENAI_API_KEY": "   "}) == []

    def test_deterministic_order_all_three(self):
        env = {"OPENROUTER_API_KEY": "k3", "ANTHROPIC_API_KEY": "k2",
               "OPENAI_API_KEY": "k1"}
        assert [name for name, _key, _v in detect_env_keys(env)] == [
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
        ]

    def test_develcakes_prefix_not_honored(self):
        # Documented deviation (§2.5): the app's own prefixed env reader
        # is utils.config.get_env; this module honors industry-standard
        # BARE names only.
        out = detect_env_keys({"DEVELCAKES_OPENAI_API_KEY": "sk-abc"})
        assert out == []


class TestValueNeverSurfaced:
    def test_value_present_in_tuple_but_not_in_names(self):
        secret = "sk-SECRET-123"
        out = detect_env_keys({"OPENAI_API_KEY": secret})
        # The contract: value is returned (for api_key wiring) but callers
        # must never render it. The module itself returns it exactly once,
        # third slot — names/keys never embed it.
        assert out[0][0] == "OPENAI_API_KEY"
        assert out[0][1] == "openai"
        assert secret not in out[0][0] and secret not in out[0][1]

    def test_purity_no_os_environ_read(self, monkeypatch):
        # Passing a dict must not consult process state.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-process")
        out = detect_env_keys({})
        assert out == []
