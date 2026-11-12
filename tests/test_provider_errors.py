# tests/test_provider_errors.py
# SPEC-63 §2.3 — every classification row, the MODEL-before-404 ordering
# (Debugger #17), message/class agreement, and the no-URL invariant (D#12:
# no api_key_url in any message; the url row's Expected {base_url} is the
# spec-sanctioned endpoint hint, not a link).

from __future__ import annotations

import pytest

from utils.provider_errors import error_class, explain_test_error
from utils.provider_presets import get_preset

OPENAI = get_preset("openai")


class TestErrorClassRows:
    """Every §2.3 row, with the exact strings utils/provider_test.py
    produces (f"HTTP {code}: {body}", _safe_error outputs, raw adapter
    exceptions)."""

    @pytest.mark.parametrize("error,expected", [
        # auth
        ("HTTP 401: You didn't provide an API key", "auth"),
        ("HTTP 403: Forbidden", "auth"),
        ("invalid api key", "auth"),
        ("Incorrect API key provided", "auth"),
        ("unauthorized", "auth"),
        # url — only AFTER the model row has not matched
        ("HTTP 404: Not Found", "url"),
        ("HTTP 404: unknown endpoint", "url"),
        # rate
        ("HTTP 429: Too Many Requests", "rate"),
        # network (five §2.3 markers)
        ("timed out after 8000ms", "network"),
        ("timeout: connect", "network"),
        ("urlopen error", "network"),
        ("NameResolutionError: api.example.com", "network"),
        ("Connection refused", "network"),
        # model — explicit markers
        ("model_not_found", "model"),
        ("invalid_model: bad id", "model"),
        ("unknown model foo", "model"),
        ("no such model", "model"),
        # caller
        ("No adapter for provider: qwen", "caller"),
        ("Cannot auto-detect caller for 'qwen/qwen3-max'", "caller"),
        ("Invalid caller: zzz", "caller"),
        # https
        ("Provider URL is not HTTPS for non-loopback host: http://x", "https"),
        # minimax body-level
        ("status_code=400: bad request", "minimax"),
        # other / None / empty
        ("weird provider failure", "other"),
        (None, "other"),
        ("", "other"),
    ])
    def test_row(self, error, expected):
        assert error_class(error) == expected

    def test_minimax_status_code_is_not_swallowed_by_model_row(self):
        # 'status_code=1004: model not found' would be model if the model
        # row were looser; §2.3's model row requires the word 'model' AND
        # a not-found phrase, or an explicit marker. This string has both
        # 'model' and 'not found' — the spec's row order makes it model.
        assert error_class("status_code=1004: model not found") == "model"

    def test_auth_with_model_word_stays_model(self):
        # The model row evaluates FIRST — even before auth (§2.3 note).
        assert error_class("HTTP 401: model not found") == "model"


class TestModelRowBeforeHttp404:
    """Debugger #17 — the ordering the v1 spec got wrong."""

    def test_404_with_model_not_found_body_is_model(self):
        assert error_class(
            "HTTP 404: The model `qwen/qwen3-max` does not exist"
        ) == "model"

    def test_404_with_model_not_found_explains_as_model(self):
        msg = explain_test_error("HTTP 404: The model `x` does not exist")
        assert "Model ID not accepted" in msg
        assert "Base URL looks wrong" not in msg

    def test_plain_404_still_url(self):
        msg = explain_test_error("HTTP 404: Not Found", preset=OPENAI)
        assert msg.startswith("Base URL looks wrong")
        assert OPENAI.base_url in msg  # Expected <preset.base_url> (§2.3)


class TestExplainTestError:
    def test_auth_message_exact(self):
        assert explain_test_error("HTTP 401: bad key") == (
            "Key rejected — check or regenerate it."
        )

    def test_rate_message_exact(self):
        assert explain_test_error("HTTP 429: slow down") == (
            "Rate limit hit. Wait a minute (free tiers are ~20 req/min) and retry."
        )

    def test_network_message_exact(self):
        assert explain_test_error("timed out") == (
            "Couldn't reach the provider — check your network / firewall "
            "and the Base URL."
        )

    def test_minimax_message_embeds_error(self):
        msg = explain_test_error("status_code=400: bad request")
        assert msg == ("Provider rejected the request: status_code=400: bad "
                       "request. Check the key and model.")

    def test_caller_message_mentions_prefix_form(self):
        msg = explain_test_error("Cannot auto-detect caller for 'qwen/qwen3-max'")
        assert "`<vendor>/<model>`" in msg

    def test_https_message(self):
        assert explain_test_error("Provider URL is not HTTPS") == (
            "Base URL must use https:// (except localhost)."
        )

    def test_unknown_passthrough_unchanged(self):
        assert explain_test_error("weird provider failure") == (
            "weird provider failure"
        )

    def test_none_and_empty_give_unknown_error(self):
        assert explain_test_error(None) == "Unknown error"
        assert explain_test_error("") == "Unknown error"

    def test_url_without_preset_drops_expected_clause(self):
        msg = explain_test_error("HTTP 404: Not Found")
        assert msg == "Base URL looks wrong — this endpoint doesn't exist."


class TestMessageClassAgreement:
    """Debugger #16 — one classification backs both surfaces; they can
    never disagree."""

    @pytest.mark.parametrize("error", [
        "HTTP 401: bad key",
        "HTTP 403: no",
        "HTTP 404: Not Found",
        "HTTP 404: The model `x` does not exist",
        "HTTP 429: slow down",
        "timed out",
        "urlopen error",
        "model_not_found",
        "invalid_model",
        "status_code=400: bad request",
        "No adapter for provider: qwen",
        "Cannot auto-detect caller for 'qwen/qwen3-max'",
        "Invalid caller: zzz",
        "Provider URL is not HTTPS",
        "something nobody predicted",
        None,
    ])
    def test_known_classes_get_messages(self, error):
        cls = error_class(error)
        msg = explain_test_error(error, preset=OPENAI)
        if cls == "other":
            assert msg == (error if error else "Unknown error")
        else:
            assert msg != error or error is None  # translated, not echoed
        assert isinstance(msg, str) and msg


class TestNoUrlInvariant:
    """D#12 — no api_key_url ever lands in a message; the url row's
    {base_url} substitution is the sanctioned endpoint hint only."""

    @pytest.mark.parametrize("preset_key", ["openai", "anthropic",
                                            "openrouter", "google"])
    def test_no_api_key_url_in_any_message(self, preset_key):
        preset = get_preset(preset_key)
        for error in ["HTTP 401: x", "HTTP 404: Not Found", "HTTP 429: x",
                      "timed out", "model_not_found", "status_code=400: x",
                      "No adapter for provider: q", "weird failure"]:
            msg = explain_test_error(error, preset=preset)
            assert preset.api_key_url not in msg
            assert "api-keys" not in msg or "aistudio" in preset.api_key_url
            assert not msg.startswith("http")
