# tests/test_provider_presets.py
# SPEC-63 §2.1 — the 4-preset catalog: table pinned verbatim, caller/model
# invariant enforced TEST-SIDE against agent.runtime.get_valid_callers()
# (the taxonomy is deliberately not copied into the utils module), lookup
# and vendor-presence semantics.

from __future__ import annotations

import ast
from pathlib import Path
from typing import ClassVar

import pytest

from utils.provider_presets import (
    PRESETS,
    get_preset,
    preset_for,
    preset_vendor_present,
    presets_from_env,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── §2.1 table pinned field-by-field ───────────────────────────────────────


class TestPresetCatalog:
    def test_exactly_four_presets(self):
        assert len(PRESETS) == 4

    def test_keys_in_catalog_order(self):
        assert [p.key for p in PRESETS] == [
            "openai", "anthropic", "openrouter", "google",
        ]

    @pytest.mark.parametrize("key,expected", [
        ("openai", {
            "label": "OpenAI", "name": "openai",
            "base_url": "https://api.openai.com/v1",
            "default_model": "gpt-4o", "caller": "openai",
            "api_key_url": "https://platform.openai.com/api-keys",
            "key_prefix_hint": "sk-…", "context_window": 128_000,
            "supports_models_list": True}),
        ("anthropic", {
            "label": "Anthropic", "name": "anthropic",
            "base_url": "https://api.anthropic.com",  # NO /v1
            "default_model": "claude-3-5-sonnet-20241022", "caller": "anthropic",
            "api_key_url": "https://console.anthropic.com/settings/keys",
            "key_prefix_hint": "sk-ant-…", "context_window": 200_000,
            "supports_models_list": False}),
        ("openrouter", {
            "label": "OpenRouter", "name": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "default_model": "openrouter/auto", "caller": "openrouter",
            "api_key_url": "https://openrouter.ai/keys",
            "key_prefix_hint": "sk-or-v1-…", "context_window": 128_000,
            "supports_models_list": True}),
        ("google", {
            "label": "Google Gemini", "name": "google",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "default_model": "openai/gemini-2.0-flash",  # prefixed (v2 ruling)
            "caller": "openai",
            "api_key_url": "https://aistudio.google.com",
            "key_prefix_hint": "AIza…", "context_window": 128_000,
            "supports_models_list": True}),
    ])
    def test_preset_row_pinned(self, key, expected):
        preset = get_preset(key)
        assert preset is not None
        for field, value in expected.items():
            assert getattr(preset, field) == value, f"{key}.{field}"


# ── caller/model invariants (test-side taxonomy import) ────────────────────


class TestPresetCallerInvariant:
    def test_every_caller_in_taxonomy(self):
        from agent.runtime import get_valid_callers  # test-side ONLY (§2.1)
        valid = set(get_valid_callers())
        for preset in PRESETS:
            assert preset.caller in valid, preset.key

    def test_prefixed_default_models_carry_their_own_caller(self):
        """Unconditional prefix rule: any '/' in default_model must be
        '<valid-caller>/<model>'. The openrouter and google rows alone
        exercise this (Debugger #23) — a bare-prefix key like 'qwen/'
        would fail here."""
        from agent.runtime import get_valid_callers
        valid = set(get_valid_callers())
        for preset in PRESETS:
            if "/" in preset.default_model:
                prefix = preset.default_model.split("/", 1)[0]
                assert prefix == preset.caller, preset.key
                assert prefix in valid, preset.key

    def test_only_two_rows_are_prefixed(self):
        prefixed = {p.key for p in PRESETS if "/" in p.default_model}
        assert prefixed == {"openrouter", "google"}


# ── lookup semantics ───────────────────────────────────────────────────────


class TestPresetLookup:
    def test_get_preset_known(self):
        assert get_preset("openai").name == "openai"

    def test_get_preset_unknown_is_none(self):
        assert get_preset("minimax") is None      # deferred out of v1
        assert get_preset("zai") is None          # deferred out of v1
        assert get_preset("") is None
        assert get_preset("nope") is None


class _Card:
    """Minimal duck-typed card — preset_for/preset_vendor_present read
    base_url/name only."""

    def __init__(self, name, base_url):
        self.name = name
        self.base_url = base_url


class TestPresetFor:
    def test_exact_join(self):
        assert preset_for(_Card("openai", "https://api.openai.com/v1")).key == "openai"

    def test_trailing_slash_tolerance(self):
        assert preset_for(_Card("x", "https://api.openai.com/v1/")).key == "openai"

    def test_custom_is_none(self):
        assert preset_for(_Card("mine", "https://api.example.com/v1")) is None

    def test_renamed_card_still_resolves_by_base_url(self):
        # delta #7: rename loses _preset on the view object — base_url is
        # the durable join key.
        assert preset_for(_Card("my-llm", "https://api.openai.com/v1")).key == "openai"


class TestPresetVendorPresent:
    """D#5/FLG-D7: ONE vendor predicate, base_url OR exact preset name."""

    def test_empty_store(self):
        assert preset_vendor_present([], get_preset("openai")) is False

    def test_name_match(self):
        cards = [_Card("openai", "https://elsewhere.example/v1")]
        assert preset_vendor_present(cards, get_preset("openai")) is True

    def test_base_url_match_on_renamed_card(self):
        # Debugger #18: a renamed card must still count as vendor-present,
        # else the chip resurrects and creates a duplicate same-vendor card.
        cards = [_Card("renamed", "https://api.openai.com/v1")]
        assert preset_vendor_present(cards, get_preset("openai")) is True

    def test_unrelated_card_is_not_present(self):
        cards = [_Card("other", "https://api.example.com/v1")]
        assert preset_vendor_present(cards, get_preset("openai")) is False

    def test_trailing_slash_on_stored_card(self):
        cards = [_Card("openai", "https://api.openai.com/v1/")]
        assert preset_vendor_present(cards, get_preset("openai")) is True


class TestPresetsFromEnv:
    def test_empty_env(self):
        assert presets_from_env({}) == []

    def test_detection_order_follows_env_key_map(self):
        env = {"OPENROUTER_API_KEY": "k", "OPENAI_API_KEY": "k",
               "ANTHROPIC_API_KEY": "k"}
        assert [p.key for p in presets_from_env(env)] == [
            "openai", "anthropic", "openrouter",
        ]

    def test_blank_values_are_absent(self):
        assert presets_from_env({"OPENAI_API_KEY": "   "}) == []

    def test_values_never_surface_in_result_repr(self):
        secret = "sk-SECRET-DO-NOT-PRINT-123"
        out = presets_from_env({"OPENAI_API_KEY": secret})
        assert len(out) == 1
        assert secret not in repr(out)


# ── §6 import-hygiene criterion (AST, no execution needed) ────────────────


class TestUtilsModuleImportHygiene:
    """No agent/* or gi import at module scope in any new utils module."""

    MODULES: ClassVar[list[str]] = [
        "utils/provider_presets.py",
        "utils/provider_errors.py",
        "utils/env_provider_keys.py",
        "utils/model_catalog.py",
    ]

    @pytest.mark.parametrize("rel", MODULES)
    def test_no_agent_or_gi_module_scope_import(self, rel):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        for node in tree.body:  # top level only — function-local is allowed
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            for name in names:
                assert not name.startswith("agent"), f"{rel}: {name}"
                assert not name.startswith("gi"), f"{rel}: {name}"
