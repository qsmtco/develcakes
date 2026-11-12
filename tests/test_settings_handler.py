# tests/test_settings_handler.py
# Tests for ui/handlers/settings_handler.py — Settings dialog logic.
#
# Pattern: synchronous tests by passing GLib_module=None; test_provider uses
# threading.Event to wait for the daemon thread to finish.
#
# No GTK imports — pure handler logic.

import json
import threading

import pytest

from agent.config import get_agent_setting
from models.providers import ProviderConfig
from ui.handlers.settings_handler import SettingsHandler
from utils.provider_test import TestResult
from utils.providers_store import load_providers


def _make_provider(name: str = "test", **overrides) -> ProviderConfig:
    """Create a test ProviderConfig with sensible defaults.

    Default model uses the 'openai' prefix so the auto-detect
    caller-validation (caller-validation spec) accepts it. Callers
    that need to exercise different prefixes should pass
    `default_model=...` explicitly.
    """
    defaults = dict(
        name=name,
        base_url=f"https://api.{name}.example.com/v1",
        api_key=f"sk-{name}-key",
        default_model="openai/test-model-v1",
    )
    defaults.update(overrides)
    return ProviderConfig(**defaults)


class TestListProviders:
    def test_empty_when_no_yaml(self, tmp_config_dir):
        h = SettingsHandler()
        assert h.list_providers() == []

    def test_returns_saved_providers(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("a"))
        h.add_or_update(_make_provider("b"))
        names = [p.name for p in h.list_providers()]
        assert names == ["a", "b"]


class TestAddOrUpdate:
    def test_adds_new_provider(self, tmp_config_dir):
        changed = []
        h = SettingsHandler(on_providers_changed=lambda plist: changed.append(plist))
        h.add_or_update(_make_provider("newprov"))
        assert len(h.list_providers()) == 1
        assert changed and len(changed[0]) == 1

    def test_replaces_existing_same_name(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p", api_key="old-key"))
        h.add_or_update(_make_provider("p", api_key="new-key"))
        providers = h.list_providers()
        assert len(providers) == 1
        assert providers[0].api_key == "new-key"

    def test_empty_name_raises(self, tmp_config_dir):
        h = SettingsHandler()
        with pytest.raises(ValueError, match="name"):
            h.add_or_update(ProviderConfig(
                name="", base_url="https://x", api_key="k", default_model="m",
            ))

    def test_empty_base_url_raises(self, tmp_config_dir):
        h = SettingsHandler()
        with pytest.raises(ValueError, match="Base URL"):
            h.add_or_update(ProviderConfig(
                name="p", base_url="", api_key="k", default_model="m",
            ))

    def test_empty_api_key_raises(self, tmp_config_dir):
        h = SettingsHandler()
        with pytest.raises(ValueError, match="API key"):
            h.add_or_update(ProviderConfig(
                name="p", base_url="https://x", api_key="", default_model="m",
            ))

    def test_empty_default_model_raises(self, tmp_config_dir):
        h = SettingsHandler()
        with pytest.raises(ValueError, match="Default model"):
            h.add_or_update(ProviderConfig(
                name="p", base_url="https://x", api_key="k", default_model="",
            ))

    def test_fires_status_changed_on_add(self, tmp_config_dir):
        statuses = []
        h = SettingsHandler(on_status_changed=lambda b: statuses.append(b))
        h.add_or_update(_make_provider("p"))
        # New provider has last_verified_at=None → status is False
        assert statuses == [False]


class TestRemove:
    def test_removes_existing(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p"))
        h.remove("p")
        assert h.list_providers() == []

    def test_no_op_when_not_found(self, tmp_config_dir):
        h = SettingsHandler()
        h.remove("ghost")  # must not raise
        assert h.list_providers() == []

    def test_fires_callbacks(self, tmp_config_dir):
        changed, statuses = [], []
        h = SettingsHandler(
            on_providers_changed=lambda p: changed.append(p),
            on_status_changed=lambda b: statuses.append(b),
        )
        h.add_or_update(_make_provider("p"))
        changed.clear()
        statuses.clear()
        h.remove("p")
        assert changed and changed[0] == []
        assert statuses == [False]


class TestGlobalDefaultPersistence:
    """SPEC-62 item 4 → fix-round 4 RULING 3: agent.json default_provider /
    default_model maintenance is owned by the save_providers choke point
    (add_or_update passes prefer=provider.name); RULING 2: the stored model
    is the BARE name (runtime composes f"{provider}/{bare}")."""

    def test_first_chat_card_save_becomes_default(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        assert get_agent_setting("default_provider", "") == "p1"
        # RULING 2: bare model stored.
        assert get_agent_setting("default_model", "") == "test-model-v1"

    def test_decision_model_first_does_not_become_default(self, tmp_config_dir):
        h = SettingsHandler()
        dm = _make_provider(
            "DecisionModel",
            base_url="https://dm.example.com/v1",
            default_model="typesafe/s1",
        )
        h.add_or_update(dm, allow_empty_caller=True)
        # Non-chat card: defaults stay empty…
        assert get_agent_setting("default_provider", "") == ""
        assert get_agent_setting("default_model", "") == ""
        # …until a real chat card is saved.
        h.add_or_update(_make_provider("p1"))
        assert get_agent_setting("default_provider", "") == "p1"

    def test_still_live_default_not_switched_on_every_save(self, tmp_config_dir):
        # "If the current default still names an existing card, LEAVE it" —
        # a second card save must not re-point the default.
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        h.add_or_update(_make_provider("p2"))
        assert get_agent_setting("default_provider", "") == "p1"

    def test_default_model_refreshed_when_default_card_model_edited(self, tmp_config_dir):
        # Fix-round item B → RULING 3 branch (4): a same-name replace of the
        # DEFAULT card with a new default_model must refresh default_model,
        # BARE (RULING 2), via the choke point (add_or_update passes prefer).
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        assert get_agent_setting("default_model", "") == "test-model-v1"
        h.add_or_update(_make_provider("p1", default_model="openai/new-model-v9"))
        assert get_agent_setting("default_provider", "") == "p1"  # card unchanged
        assert get_agent_setting("default_model", "") == "new-model-v9"

    def test_remove_default_retargets_to_first_chat_card(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        h.add_or_update(_make_provider("p2"))
        h.remove("p1")
        assert get_agent_setting("default_provider", "") == "p2"
        # RULING 2: bare model stored.
        assert get_agent_setting("default_model", "") == "test-model-v1"

    def test_remove_last_card_clears_defaults(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        h.remove("p1")
        assert get_agent_setting("default_provider", "") == ""
        assert get_agent_setting("default_model", "") == ""

    def test_remove_default_retargets_to_callerless_prefixed_card(self, tmp_config_dir):
        # Fix-round 3 fold-in → RULING 3 branch (2): the retarget is the
        # choke point's, on derivation — chat cards that reach disk with
        # caller="" + a recognized model prefix (the uncovered write paths)
        # are visible to it.
        from utils.providers_store import save_providers as sp_raw
        from agent.config import get_agent_setting as gas

        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))  # becomes the default
        assert gas("default_provider", "") == "p1"

        # Add a caller=""+prefixed card via a raw (uncovered-path-shaped)
        # save; the live default stays p1 (still-live-default guard).
        sp_raw([
            _make_provider("p1"),
            _make_provider("derived-card", caller="", default_model="openai/gpt-4o-mini"),
        ])

        h.remove("p1")
        # NOT cleared: the derivable chat card was adopted as the new default.
        assert gas("default_provider", "") == "derived-card"
        # RULING 2: bare model stored.
        assert gas("default_model", "") == "gpt-4o-mini"


class TestTestProvider:
    def test_success_stamps_last_verified_at(self, tmp_config_dir, monkeypatch):
        from ui.handlers import settings_handler as sh
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=True, latency_ms=42, error=None, model_used=kw["model"]))

        captured = []
        callback = threading.Event()
        def on_result(r):
            captured.append(r)
            callback.set()

        h = SettingsHandler()  # GLib_module=None → synchronous dispatch
        p = _make_provider("p")
        h.add_or_update(p)
        h.test_provider(p, on_result)

        assert callback.wait(timeout=2.0), "test_provider callback never fired"
        assert captured[0].ok is True
        # Provider in yaml should now have last_verified_at set
        providers = h.list_providers()
        assert providers[0].last_verified_at is not None
        assert providers[0].last_error is None

    def test_failure_stamps_last_error(self, tmp_config_dir, monkeypatch):
        from ui.handlers import settings_handler as sh
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=False, latency_ms=10, error="401 unauthorized", model_used=kw["model"]))

        captured, callback = [], threading.Event()
        def on_result(r):
            captured.append(r)
            callback.set()

        h = SettingsHandler()
        p = _make_provider("p")
        h.add_or_update(p)
        h.test_provider(p, on_result)
        assert callback.wait(timeout=2.0)

        assert captured[0].ok is False
        providers = h.list_providers()
        # SPEC-63 Debugger #7: the PERSISTED last_error is the EXPLAINED
        # string (the raw "401 unauthorized" stays on the TestResult).
        assert providers[0].last_error == "Key rejected — check or regenerate it."
        assert providers[0].last_verified_at is None  # unchanged

    def test_test_connection_raises_wrapped_as_failure(self, tmp_config_dir, monkeypatch):
        from ui.handlers import settings_handler as sh
        def boom(**kw):
            raise ValueError("unknown provider")
        monkeypatch.setattr(sh, "test_connection", boom)

        captured, callback = [], threading.Event()
        def on_result(r):
            captured.append(r)
            callback.set()

        h = SettingsHandler()
        p = _make_provider("p")
        h.add_or_update(p)
        h.test_provider(p, on_result)
        assert callback.wait(timeout=2.0)
        assert captured[0].ok is False
        assert "unknown provider" in captured[0].error

    def test_fires_status_changed_on_success(self, tmp_config_dir, monkeypatch):
        from ui.handlers import settings_handler as sh
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=True, latency_ms=1, error=None, model_used=kw["model"]))

        statuses = []
        status_event = threading.Event()
        def on_result(r):
            pass  # just acknowledge

        h = SettingsHandler(on_status_changed=lambda b: (statuses.append(b), status_event.set()))
        p = _make_provider("p")
        h.add_or_update(p)
        statuses.clear()
        status_event.clear()  # reset so we wait for the test_provider's callback
        h.test_provider(p, on_result)
        assert status_event.wait(timeout=2.0), "on_status_changed never fired"
        # After successful test, status should be True
        assert True in statuses

    def test_preserves_caller_on_success(self, tmp_config_dir, monkeypatch):
        """Regression: test_provider's success-path must not strip the caller field.
        Without the fix, _worker rebuilds ProviderConfig without caller=, silently
        dropping it on save and breaking subsequent runtime calls.
        """
        from ui.handlers import settings_handler as sh
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=True, latency_ms=42, error=None, model_used=kw["model"]))

        callback = threading.Event()
        h = SettingsHandler()
        # caller="minimax" is set explicitly (not auto-detected from default_model).
        # Caller matches the model prefix so the prefix-consistency block
        # does not correct it (added in the Sonnet-5 regression fix).
        p = _make_provider("p", default_model="minimax/M3", caller="minimax")
        h.add_or_update(p)
        h.test_provider(p, lambda r: callback.set())
        assert callback.wait(timeout=2.0), "test_provider callback never fired"

        providers = h.list_providers()
        assert providers[0].caller == "minimax", (
            f"test_provider stripped caller on success; got {providers[0].caller!r}"
        )
        # Verify the success path also ran (last_verified_at is set)
        assert providers[0].last_verified_at is not None

    def test_preserves_caller_on_failure(self, tmp_config_dir, monkeypatch):
        """Regression: test_provider's failure-path must not strip the caller field.
        Both success and failure branches rebuild ProviderConfig — both must preserve caller.
        """
        from ui.handlers import settings_handler as sh
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=False, latency_ms=10, error="401 unauthorized", model_used=kw["model"]))

        callback = threading.Event()
        h = SettingsHandler()
        # Caller matches the model prefix so the prefix-consistency block
        # does not correct it (added in the Sonnet-5 regression fix).
        p = _make_provider("p", default_model="minimax/M3", caller="minimax")
        h.add_or_update(p)
        h.test_provider(p, lambda r: callback.set())
        assert callback.wait(timeout=2.0), "test_provider callback never fired"

        providers = h.list_providers()
        assert providers[0].caller == "minimax", (
            f"test_provider stripped caller on failure; got {providers[0].caller!r}"
        )
        # SPEC-63 Debugger #7: persisted last_error is the EXPLAINED string.
        assert providers[0].last_error == "Key rejected — check or regenerate it."

    def test_auto_detects_caller_from_model_prefix(self, tmp_config_dir, monkeypatch):
        """Self-heal: when caller is empty and default_model has a slash,
        the worker auto-fills caller from the prefix. This lets broken YAML
        entries (post-regression state) recover on next Test Connection.
        Caller validation (caller-validation spec) also lowercases the
        prefix to match the taxonomy.
        """
        from ui.handlers import settings_handler as sh
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=True, latency_ms=1, error=None, model_used=kw["model"]))

        callback = threading.Event()
        h = SettingsHandler()
        # Note: caller defaults to "" (ProviderConfig default). Mimics a broken YAML entry.
        # Use 'minimax/M3' (valid taxonomy prefix) to test the auto-detect
        # path: prefix gets lowercased to "minimax" and matches the taxonomy.
        p = _make_provider("minimax-test", default_model="minimax/M3", caller="")
        h.add_or_update(p)
        # Sanity: add_or_update's auto-detect should already have set caller,
        # lowercased to match the taxonomy (caller-validation spec).
        assert h.list_providers()[0].caller == "minimax"

        # Now simulate the broken-state scenario: caller explicitly empty.
        broken = _make_provider("minimax-test", default_model="minimax/M3", caller="")
        h.test_provider(broken, lambda r: callback.set())
        assert callback.wait(timeout=2.0), "test_provider callback never fired"

        providers = h.list_providers()
        assert providers[0].caller == "minimax", (
            f"test_provider did not auto-detect caller; got {providers[0].caller!r}"
        )


class TestStatusHasVerified:
    def test_false_when_no_providers(self, tmp_config_dir):
        h = SettingsHandler()
        assert h.status_has_verified() is False

    def test_false_when_no_verified(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p"))  # last_verified_at=None
        assert h.status_has_verified() is False

    def test_true_after_verification(self, tmp_config_dir, monkeypatch):
        from ui.handlers import settings_handler as sh
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=True, latency_ms=1, error=None, model_used=kw["model"]))

        callback = threading.Event()
        h = SettingsHandler()
        p = _make_provider("p")
        h.add_or_update(p)
        h.test_provider(p, lambda r: callback.set())
        assert callback.wait(timeout=2.0)
        assert h.status_has_verified() is True


class TestTestProviderPrefillsMaxTokens:
    def test_success_with_context_window_prefills_default(self, tmp_config_dir, monkeypatch):
        """When max_tokens == 128_000 default and context_window is set, pre-fill."""
        from ui.handlers import settings_handler as sh
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=True, latency_ms=200, error=None,
                       model_used=kw["model"], context_window=500_000))

        callback = threading.Event()
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))  # max_tokens defaults to 128_000

        h.test_provider(_make_provider("p1"), lambda r: callback.set())
        assert callback.wait(timeout=2.0), "test_provider callback never fired"

        providers = load_providers()
        assert providers[0].max_tokens == 500_000

    def test_success_does_not_overwrite_customized(self, tmp_config_dir, monkeypatch):
        """When max_tokens has been customized, Test Connection doesn't overwrite."""
        from ui.handlers import settings_handler as sh
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=True, latency_ms=200, error=None,
                       model_used=kw["model"], context_window=1_000_000))

        callback = threading.Event()
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1", max_tokens=300_000))

        h.test_provider(_make_provider("p1"), lambda r: callback.set())
        assert callback.wait(timeout=2.0), "test_provider callback never fired"

        providers = load_providers()
        assert providers[0].max_tokens == 300_000  # preserved

    def test_failure_does_not_change_max_tokens(self, tmp_config_dir, monkeypatch):
        """Failure path leaves max_tokens untouched."""
        from ui.handlers import settings_handler as sh
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=False, latency_ms=0,
                       error="401 Unauthorized",
                       model_used=kw["model"], context_window=None))

        callback = threading.Event()
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1", max_tokens=200_000))

        h.test_provider(_make_provider("p1"), lambda r: callback.set())
        assert callback.wait(timeout=2.0), "test_provider callback never fired"

        providers = load_providers()
        assert providers[0].max_tokens == 200_000  # untouched

    def test_success_without_context_window_preserves_max_tokens(self, tmp_config_dir, monkeypatch):
        """When context_window is None, max_tokens stays unchanged."""
        from ui.handlers import settings_handler as sh
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=True, latency_ms=200, error=None,
                       model_used=kw["model"], context_window=None))

        callback = threading.Event()
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1", max_tokens=200_000))

        h.test_provider(_make_provider("p1"), lambda r: callback.set())
        assert callback.wait(timeout=2.0), "test_provider callback never fired"

        providers = load_providers()
        assert providers[0].max_tokens == 200_000  # unchanged

    # BUG #7 regression: when the agent builder sets default_max_tokens=N AND
    # max_tokens == N (the sentinel value), Test Connection's pre-fill check
    # `p.max_tokens == 128_000` must NOT overwrite — because default_max_tokens
    # is non-zero, indicating a deliberate wizard-set choice.

    def test_wizard_default_max_tokens_protects_against_overwrite(self, tmp_config_dir, monkeypatch):
        """BUG #7: provider with default_max_tokens != 0 must NOT be overwritten
        by Test Connection pre-fill even when max_tokens == 128_000 sentinel."""
        from models.providers import ProviderConfig
        from ui.handlers import settings_handler as sh
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=True, latency_ms=200, error=None,
                       model_used=kw["model"], context_window=1_000_000))

        callback = threading.Event()
        h = SettingsHandler()
        # Simulate agent-builder output for openrouter: sentinel max_tokens=128K
        # but default_max_tokens=128_000 (the wizard stamped it)
        wizard_provider = ProviderConfig(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="***",
            default_model="openrouter/free",
            caller="openrouter",
            max_tokens=128_000,
            default_max_tokens=128_000,  # wizard stamped this
        )
        h.add_or_update(wizard_provider)

        h.test_provider(wizard_provider, lambda r: callback.set())
        assert callback.wait(timeout=2.0)

        providers = load_providers()
        # max_tokens must remain 128_000 (the wizard's deliberate choice).
        # Before the BUG #7 fix, the pre-fill check would have overwritten
        # to 1_000_000.
        assert providers[0].max_tokens == 128_000, (
            f"BUG #7: wizard default was overwritten: max_tokens={providers[0].max_tokens}"
        )


# ═══════════════════════════════════════════════════════════════════
#  Strict caller validation (caller-validation.md)
# ═══════════════════════════════════════════════════════════════════
#
# Adversarial tests for the auto-detect + validation logic. The OLD code
# trusted whatever prefix was on default_model and the whatever value the
# user hand-set on caller. The NEW code lowercases auto-detected prefixes
# and validates both auto-detected and hand-set callers against the
# taxonomy in agent.runtime._PROVIDER_CALLERS.

import pytest


class TestAddOrUpdateCallerValidation:
    """add_or_update must reject unknown callers (auto-detected OR hand-set)."""

    def test_add_or_update_lowercases_capitalized_prefix(self, tmp_config_dir):
        """OpenRouter/x → caller 'openrouter' (lowercased, accepted).

        Pre-fix bug: 'OpenRouter' was NOT in the taxonomy lookup (the dict
        has 'openrouter' lowercase). The call would silently fail at runtime.
        """
        h = SettingsHandler()
        p = ProviderConfig(
            name="p", base_url="https://x", api_key="k",
            default_model="OpenRouter/free", caller="",
        )
        h.add_or_update(p)
        assert h.list_providers()[0].caller == "openrouter"

    def test_add_or_update_raises_on_capitalized_prefix(self, tmp_config_dir):
        """OpenRouter/x with a name that has 'OpenRouter' as the prefix
        AND nothing in caller. The lowercased form 'openrouter' IS valid,
        so the test verifies the lowercasing happens, not that it raises.
        But Poolside/x (a non-taxonomy prefix) MUST raise.
        """
        # The non-taxonomy case: this is the actual rejection path.
        h = SettingsHandler()
        p = ProviderConfig(
            name="p", base_url="https://x", api_key="k",
            default_model="poolside/free", caller="",
        )
        with pytest.raises(ValueError, match="Invalid caller 'poolside'"):
            h.add_or_update(p)

    def test_add_or_update_raises_on_unknown_vendor_prefix(self, tmp_config_dir):
        """poolside/x is not in the taxonomy — must raise."""
        h = SettingsHandler()
        p = ProviderConfig(
            name="p", base_url="https://x", api_key="k",
            default_model="poolside/free", caller="",
        )
        with pytest.raises(ValueError, match="Invalid caller 'poolside'"):
            h.add_or_update(p)

    def test_add_or_update_raises_on_explicitly_invalid_caller(self, tmp_config_dir):
        """User hand-sets caller='foo' — must raise even though auto-detect
        would have produced a valid caller. Use a non-taxonomy prefix
        ('mycompany') so the prefix-consistency block does not pre-empt.
        """
        h = SettingsHandler()
        p = ProviderConfig(
            name="p", base_url="https://x", api_key="k",
            default_model="mycompany/foo", caller="foo",
        )
        with pytest.raises(ValueError, match="Invalid caller 'foo'"):
            h.add_or_update(p)

    @pytest.mark.parametrize("caller", ["anthropic", "minimax", "openai", "openrouter", "zai"])
    def test_add_or_update_accepts_all_valid_callers(self, tmp_config_dir, caller):
        """All 5 valid callers must be accepted (explicitly set)."""
        h = SettingsHandler()
        p = ProviderConfig(
            name=f"p-{caller}", base_url="https://x", api_key="k",
            default_model=f"{caller}/model-v1", caller=caller,
        )
        h.add_or_update(p)  # must NOT raise
        assert h.list_providers()[0].caller == caller

    def test_add_or_update_lowercases_valid_capitalized_prefix(self, tmp_config_dir):
        """Openai/x → caller 'openai' (lowercased, valid)."""
        h = SettingsHandler()
        p = ProviderConfig(
            name="p", base_url="https://x", api_key="k",
            default_model="Openai/gpt-4o", caller="",
        )
        h.add_or_update(p)
        assert h.list_providers()[0].caller == "openai"

    def test_add_or_update_does_not_save_on_invalid_caller(self, tmp_config_dir):
        """If validation raises, the provider is NOT saved (no half-saved state)."""
        h = SettingsHandler()
        p = ProviderConfig(
            name="p", base_url="https://x", api_key="k",
            default_model="poolside/free", caller="",
        )
        with pytest.raises(ValueError):
            h.add_or_update(p)
        assert h.list_providers() == []  # nothing was saved

    def test_add_or_update_existing_valid_provider_still_works(self, tmp_config_dir):
        """Regression: the Bug #2/#3 fix that preserves caller=p.caller
        in test_provider._worker must NOT be affected by caller validation.
        A valid existing provider must still save and re-save without error.
        """
        h = SettingsHandler()
        # Save a valid provider first
        p1 = ProviderConfig(
            name="p", base_url="https://x", api_key="k1",
            default_model="openai/gpt-4o", caller="openai",
        )
        h.add_or_update(p1)
        # Re-save with a different API key
        p2 = ProviderConfig(
            name="p", base_url="https://x", api_key="k2",
            default_model="openai/gpt-4o", caller="openai",
        )
        h.add_or_update(p2)
        assert h.list_providers()[0].api_key == "k2"


class TestTestProviderCallerValidation:
    """test_provider must return a failed TestResult (NOT raise) when the
    resolved caller is invalid. The worker runs in a daemon thread — raising
    would silently swallow the error.
    """

    def test_test_provider_returns_failed_result_on_invalid_caller(
        self, tmp_config_dir, monkeypatch
    ):
        """Auto-detect from 'poolside/x' fails validation. The worker must
        return a failed TestResult with a clear error message and dispatch it
        via the on_result callback.
        """
        # The worker uses test_connection internally — but we want the
        # validation to fire BEFORE that call. Mock it to assert it was
        # NOT called when the caller is invalid.
        from ui.handlers import settings_handler as sh
        test_connection_called = []
        def fake_test_connection(**_kw):
            test_connection_called.append(_kw)
            return TestResult(ok=True, latency_ms=0, error=None, model_used=_kw["model"])
        monkeypatch.setattr(sh, "test_connection", fake_test_connection)

        callback = threading.Event()
        captured_result: list = []
        h = SettingsHandler()
        p = _make_provider("bad", default_model="poolside/free", caller="")
        h.test_provider(p, lambda r: (captured_result.append(r), callback.set()))
        assert callback.wait(timeout=2.0), "test_provider callback never fired"
        assert len(captured_result) == 1
        result = captured_result[0]
        assert result.ok is False
        assert "Invalid caller 'poolside'" in result.error
        # The invalid-caller branch returns before calling test_connection.
        assert test_connection_called == []


# ═══════════════════════════════════════════════════════════════════
#  Empty-caller gap fix (caller-validation.md, rework)
# ═══════════════════════════════════════════════════════════════════
#
# If default_model has no "/" AND caller is empty, neither the slash-guarded
# auto-detect nor the taxonomy check fires, and caller="" gets saved → runtime
# "No streaming caller" error. Surface a clear error at save time instead.

class TestAddOrUpdateEmptyCallerGap:
    """default_model without '/' + caller='' must raise at save time."""

    def test_add_or_update_no_slash_no_caller_raises(self, tmp_config_dir):
        """The exact failure mode the gap fix addresses."""
        h = SettingsHandler()
        p = ProviderConfig(
            name="p", base_url="https://x", api_key="k",
            default_model="gpt-4o",  # no slash — auto-detect cannot derive
            caller="",                # caller not set
        )
        with pytest.raises(ValueError, match="Cannot auto-detect caller"):
            h.add_or_update(p)

    def test_add_or_update_no_slash_does_not_save(self, tmp_config_dir):
        """The empty-caller raise must NOT half-save the provider."""
        h = SettingsHandler()
        p = ProviderConfig(
            name="p", base_url="https://x", api_key="k",
            default_model="gpt-4o", caller="",
        )
        with pytest.raises(ValueError):
            h.add_or_update(p)
        assert h.list_providers() == []

    def test_add_or_update_explicit_valid_caller_with_no_slash_works(self, tmp_config_dir):
        """If caller is explicitly set to a valid value, the slash guard
        is irrelevant — the explicit value is trusted (per spec).
        """
        h = SettingsHandler()
        p = ProviderConfig(
            name="p", base_url="https://x", api_key="k",
            default_model="gpt-4o",  # no slash, but caller is set
            caller="openai",
        )
        h.add_or_update(p)  # must NOT raise
        assert h.list_providers()[0].caller == "openai"


class TestTestProviderEmptyCallerGap:
    """test_provider must return a failed TestResult (NOT raise) on the
    empty-caller gap. Same daemon-thread exception-swallowing concern.
    """

    def test_test_provider_returns_failed_result_on_no_slash_no_caller(
        self, tmp_config_dir, monkeypatch
    ):
        """default_model='gpt-4o' + caller='' triggers the gap-fail path."""
        from ui.handlers import settings_handler as sh
        test_connection_called = []
        def fake_test_connection(**_kw):
            test_connection_called.append(_kw)
            return TestResult(ok=True, latency_ms=0, error=None, model_used=_kw["model"])
        monkeypatch.setattr(sh, "test_connection", fake_test_connection)

        callback = threading.Event()
        captured_result: list = []
        h = SettingsHandler()
        p = _make_provider("bad", default_model="gpt-4o", caller="")
        h.test_provider(p, lambda r: (captured_result.append(r), callback.set()))
        assert callback.wait(timeout=2.0), "test_provider callback never fired"
        assert len(captured_result) == 1
        result = captured_result[0]
        assert result.ok is False
        assert "Cannot auto-detect caller" in result.error
        assert test_connection_called == []


class TestAddOrUpdatePrefixConsistency:
    """Sonnet-5 regression: caller must match default_model prefix when
    both are present. The taxonomy check above accepts any valid caller,
    so a hand-set wrong-but-valid caller (e.g. anthropic on an OpenRouter
    URL) silently slipped through — and routed the request to /messages,
    which 404s on the OpenAI-compat API. add_or_update must correct such
    mismatches by deferring to the model prefix as the source of truth.
    """

    def test_correction_on_known_prefix_mismatch(self, tmp_config_dir):
        """The original Sonnet 5 case: base_url=openrouter.ai, model=
        openrouter/claude-sonnet-5, caller=anthropic. After save,
        caller must be openrouter.
        """
        h = SettingsHandler()
        p = ProviderConfig(
            name="Sonnet 5",
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-v1-fake",
            default_model="openrouter/claude-sonnet-5",
            caller="anthropic",        # the wrong-but-valid value
        )
        h.add_or_update(p)
        assert h.list_providers()[0].caller == "openrouter"

    def test_no_correction_when_caller_matches_prefix(self, tmp_config_dir):
        """Idempotent: when caller already equals the prefix, do nothing."""
        h = SettingsHandler()
        p = ProviderConfig(
            name="p", base_url="https://openrouter.ai/api/v1", api_key="k",
            default_model="openrouter/foo", caller="openrouter",
        )
        h.add_or_update(p)
        assert h.list_providers()[0].caller == "openrouter"

    def test_no_correction_when_model_has_no_slash(self, tmp_config_dir):
        """Anthropic native: model='claude-sonnet-4-5' (no slash).
        My block must not fire — caller stays as user set.
        """
        h = SettingsHandler()
        p = ProviderConfig(
            name="p", base_url="https://api.anthropic.com", api_key="k",
            default_model="claude-sonnet-4-5", caller="anthropic",
        )
        h.add_or_update(p)
        assert h.list_providers()[0].caller == "anthropic"

    def test_no_correction_when_prefix_not_in_taxonomy(self, tmp_config_dir):
        """Custom prefix like 'mycompany/foo' — prefix is NOT in the valid
        caller set, so the block must not fire. We don't know which adapter
        the user wants; respect the explicit caller.
        """
        h = SettingsHandler()
        p = ProviderConfig(
            name="p", base_url="https://mycompany.example.com", api_key="k",
            default_model="mycompany/foo", caller="openai",
        )
        h.add_or_update(p)
        assert h.list_providers()[0].caller == "openai"

    def test_still_raises_on_totally_invalid_caller(self, tmp_config_dir):
        """The taxonomy check below the correction block must still fire
        for callers that aren't in the valid set. Use a non-taxonomy prefix
        (mycompany) so the correction block doesn't pre-empt the test.
        """
        h = SettingsHandler()
        p = ProviderConfig(
            name="p", base_url="https://x", api_key="k",
            default_model="mycompany/foo", caller="foo",
        )
        with pytest.raises(ValueError, match="Invalid caller 'foo'"):
            h.add_or_update(p)

    @pytest.mark.parametrize("prefix,caller", [
        ("openrouter", "anthropic"),
        ("openai", "minimax"),
        ("anthropic", "openai"),
        ("zai", "anthropic"),
        ("minimax", "openrouter"),
    ])
    def test_correction_table(self, tmp_config_dir, prefix, caller):
        """Each mismatched (prefix, caller) pair from the taxonomy must
        be corrected to the prefix.
        """
        h = SettingsHandler()
        p = ProviderConfig(
            name=f"p-{prefix}-{caller}", base_url="https://x", api_key="k",
            default_model=f"{prefix}/model-v1", caller=caller,
        )
        h.add_or_update(p)
        assert h.list_providers()[0].caller == prefix

    def test_correction_is_case_insensitive_on_prefix(self, tmp_config_dir):
        """default_model prefix is lowercased before the taxonomy check."""
        h = SettingsHandler()
        p = ProviderConfig(
            name="p", base_url="https://x", api_key="k",
            default_model="OpenRouter/free", caller="anthropic",
        )
        h.add_or_update(p)
        assert h.list_providers()[0].caller == "openrouter"

    def test_correction_warning_logged(self, tmp_config_dir, caplog):
        """The correction must log a warning so the audit trail shows
        that a caller was changed.
        """
        import logging
        h = SettingsHandler()
        p = ProviderConfig(
            name="p", base_url="https://openrouter.ai/api/v1", api_key="k",
            default_model="openrouter/foo", caller="anthropic",
        )
        with caplog.at_level(logging.WARNING, logger="ui.handlers.settings_handler"):
            h.add_or_update(p)
        assert any(
            "correcting caller" in rec.message
            and "anthropic" in rec.message
            and "openrouter" in rec.message
            for rec in caplog.records
        ), f"no correction warning found in {[r.message for r in caplog.records]}"


# ═══════════════════════════════════════════════════════════════════
#  SPEC-53 §3 — JEV room-fallback setting (handler/persistence)
# ═══════════════════════════════════════════════════════════════════

class TestRoomFallbackSetting:
    """SPEC-53 Phase 3: handler + agent.json persistence for
    ``jev_room_fallback`` (supervisor|all|drop, default supervisor).
    Headless — pure handler logic, no GTK, runs without a display."""

    def _agent_json(self, tmp_config_dir):
        return tmp_config_dir / "agent.json"

    def test_default_on_missing(self, tmp_config_dir):
        h = SettingsHandler()
        assert h.get_room_fallback() == "supervisor"
        # Nothing is written just by reading.
        assert get_agent_setting("jev_room_fallback") is None

    def test_save_load_round_trip(self, tmp_config_dir):
        h = SettingsHandler()
        h.set_room_fallback("all")
        assert get_agent_setting("jev_room_fallback") == "all"
        assert h.get_room_fallback() == "all"
        # A fresh handler (fresh dialog) reads the saved value.
        assert SettingsHandler().get_room_fallback() == "all"
        raw = json.loads(self._agent_json(tmp_config_dir).read_text())
        assert raw["jev_room_fallback"] == "all"

    def test_drop_value_round_trip(self, tmp_config_dir):
        h = SettingsHandler()
        h.set_room_fallback("drop")
        assert h.get_room_fallback() == "drop"
        raw = json.loads(self._agent_json(tmp_config_dir).read_text())
        assert raw["jev_room_fallback"] == "drop"

    def test_corrupt_value_selects_default(self, tmp_config_dir):
        self._agent_json(tmp_config_dir).write_text(
            json.dumps({"jev_room_fallback": "not-a-valid-mode"})
        )
        h = SettingsHandler()
        assert h.get_room_fallback() == "supervisor"

    def test_invalid_value_rejected(self, tmp_config_dir):
        h = SettingsHandler()
        with pytest.raises(ValueError):
            h.set_room_fallback("nope")
        # The store is untouched.
        assert get_agent_setting("jev_room_fallback") is None

    def test_save_preserves_existing_agent_json_keys(self, tmp_config_dir):
        self._agent_json(tmp_config_dir).write_text(
            json.dumps({"default_model": "openai/gpt-4o", "user_id": "u-7"})
        )
        SettingsHandler().set_room_fallback("all")
        raw = json.loads(self._agent_json(tmp_config_dir).read_text())
        assert raw["jev_room_fallback"] == "all"
        # Unrelated keys survive the write.
        assert raw["default_model"] == "openai/gpt-4o"
        assert raw["user_id"] == "u-7"


# ═══════════════════════════════════════════════════════════════════
#  SPEC-63 §2.4 — handler-owned persistence (a), list_models (b),
#  create_from_env (c). Headless: pure handler logic, GLib_module=None.
# ═══════════════════════════════════════════════════════════════════

def _providers_yaml_bytes(tmp_config_dir):
    """Raw providers.yaml content, or b'' when absent. Delta #8: assert on
    CONTENT identity, not existence — SettingsHandler.__init__ creates the
    file via ensure_providers_yaml_exists, so existence is always True here.
    """
    path = tmp_config_dir / "providers.yaml"
    return path.read_bytes() if path.is_file() else b""


def _install_save_spy(monkeypatch):
    """Wrap settings_handler.save_providers, counting every call. Delegates
    to the real function so a (failing) write stays store-consistent."""
    from ui.handlers import settings_handler as sh
    calls = []
    real_save = sh.save_providers

    def spy(providers, **kw):
        calls.append(kw)
        return real_save(providers, **kw)

    monkeypatch.setattr(sh, "save_providers", spy)
    return calls


def test_legacy_401_probe_raw_body_classification():
    """Wave B integration seam: the EXACT error string provider_test produces
    on a 401 (f"HTTP {code}: {body}") must persist as the EXPLAINED message
    through explain_test_error — the Debugger #7 boundary. classify_failure
    (SPEC-02) reads the raw string; the CARD stores the explained one."""
    from utils.provider_errors import explain_test_error, error_class
    raw = "HTTP 401: {\"error\": {\"message\": \"Incorrect API key provided\"}}"
    assert error_class(raw) == "auth"
    assert explain_test_error(raw) == "Key rejected — check or regenerate it."


class TestSpec63NoWriteOnUnsavedCard:
    """§2.4a invariant (a) on ALL THREE worker exit paths: a Test on an
    UNSAVED card writes NOTHING — save_providers is never called and
    providers.yaml content is byte-identical before/after (delta #8)."""

    def _run_unsaved(self, tmp_config_dir, monkeypatch, provider):
        h = SettingsHandler()
        # Snapshot AFTER handler construction: __init__ may create the empty
        # providers.yaml once (ensure_providers_yaml_exists). Delta #8 pins
        # the TEST's effect, not the constructor's.
        before = _providers_yaml_bytes(tmp_config_dir)
        save_calls = _install_save_spy(monkeypatch)

        callback = threading.Event()
        h.test_provider(provider, lambda r: callback.set())
        assert callback.wait(timeout=2.0), "test_provider callback never fired"

        return save_calls, before, _providers_yaml_bytes(tmp_config_dir)

    def test_unsaved_valid_caller_test_writes_nothing(self, tmp_config_dir, monkeypatch):
        from ui.handlers import settings_handler as sh
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=True, latency_ms=5, error=None, model_used=kw["model"]))

        save_calls, before, after = self._run_unsaved(
            tmp_config_dir, monkeypatch, _make_provider("burner"),
        )
        assert save_calls == [], (
            f"unsaved-card green Test called save_providers: {save_calls}"
        )
        assert after == before, "providers.yaml content changed on an unsaved-card Test"
        assert load_providers() == []

    def test_unsaved_empty_caller_gap_writes_nothing(self, tmp_config_dir, monkeypatch):
        from ui.handlers import settings_handler as sh
        tc_called = []

        def fake_tc(**kw):
            tc_called.append(kw)
            return TestResult(ok=True, latency_ms=0, error=None, model_used=kw["model"])

        monkeypatch.setattr(sh, "test_connection", fake_tc)

        save_calls, before, after = self._run_unsaved(
            tmp_config_dir, monkeypatch,
            _make_provider("bad", default_model="gpt-4o", caller=""),
        )
        assert save_calls == [], (
            f"unsaved-card empty-caller Test called save_providers: {save_calls}"
        )
        assert after == before
        assert tc_called == []  # early return BEFORE the probe

    def test_unsaved_invalid_caller_writes_nothing(self, tmp_config_dir, monkeypatch):
        from ui.handlers import settings_handler as sh
        tc_called = []

        def fake_tc(**kw):
            tc_called.append(kw)
            return TestResult(ok=True, latency_ms=0, error=None, model_used=kw["model"])

        monkeypatch.setattr(sh, "test_connection", fake_tc)

        save_calls, before, after = self._run_unsaved(
            tmp_config_dir, monkeypatch,
            _make_provider("bad", default_model="poolside/free", caller=""),
        )
        assert save_calls == [], (
            f"unsaved-card invalid-caller Test called save_providers: {save_calls}"
        )
        assert after == before
        assert tc_called == []


class TestSpec63SavedCardStamping:
    """§2.4a invariant (b): a SAVED card's green Test stamps last_verified_at
    on the STORED card — via exactly one save through the shared helper."""

    def test_saved_card_green_test_stamps_and_saves_once(self, tmp_config_dir, monkeypatch):
        from ui.handlers import settings_handler as sh
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=True, latency_ms=7, error=None,
                       model_used=kw["model"], context_window=500_000))

        h = SettingsHandler()
        h.add_or_update(_make_provider("p"))
        save_calls = _install_save_spy(monkeypatch)  # AFTER add_or_update

        callback = threading.Event()
        h.test_provider(_make_provider("p"), lambda r: callback.set())
        assert callback.wait(timeout=2.0), "test_provider callback never fired"

        providers = h.list_providers()
        assert providers[0].last_verified_at is not None
        assert providers[0].last_error is None
        # The moved stamping body still pre-fills the discovered window
        # (max_tokens == 128_000 default, no wizard stamp → adopt).
        assert providers[0].max_tokens == 500_000
        # Exactly ONE write on the Test path. It is the helper's plain
        # positional save — the VERBATIM move of the former worker save (the
        # prefer path belongs to add_or_update, i.e. create_from_env, not to
        # result stamping).
        assert len(save_calls) == 1, f"expected one save, got {save_calls!r}"

    def test_saved_card_401_persists_explained_last_error(self, tmp_config_dir, monkeypatch):
        """§2.4a invariant (c) / Debugger #7: the persisted last_error is the
        EXPLAINED string, never the raw probe output."""
        from ui.handlers import settings_handler as sh
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=False, latency_ms=3,
                       error="HTTP 401: Unauthorized", model_used=kw["model"]))

        h = SettingsHandler()
        # Preset-shaped stored card so preset_for() joins on base_url and the
        # explained message is the catalog auth row.
        h.add_or_update(_make_provider(
            "openai",
            base_url="https://api.openai.com/v1",
            default_model="gpt-4o",
            caller="openai",
        ))

        callback = threading.Event()
        h.test_provider(
            _make_provider("openai",
                           base_url="https://api.openai.com/v1",
                           default_model="gpt-4o", caller="openai"),
            lambda r: callback.set(),
        )
        assert callback.wait(timeout=2.0), "test_provider callback never fired"

        last_error = h.list_providers()[0].last_error
        assert last_error == "Key rejected — check or regenerate it."
        assert "HTTP 401" not in last_error  # raw string never persisted


class TestSpec63ListModels:
    """§2.4b: off-thread fetch_model_ids, dispatched back on the handler's
    dispatch path; never persists."""

    def test_list_models_dispatches_result(self, tmp_config_dir, monkeypatch):
        from utils.model_catalog import ModelListResult
        from ui.handlers import settings_handler as sh

        seen = {}

        def fake_fetch(base_url, api_key, caller):
            seen["args"] = (base_url, api_key, caller)
            return ModelListResult(ok=True, model_ids=("m-1", "m-2"), error=None)

        monkeypatch.setattr(sh, "fetch_model_ids", fake_fetch)

        got, evt = [], threading.Event()
        h = SettingsHandler()
        h.list_models("https://x/v1", "sk-k", "openai",
                      lambda r: (got.append(r), evt.set()))
        assert evt.wait(timeout=2.0), "list_models callback never fired"
        assert seen["args"] == ("https://x/v1", "sk-k", "openai")
        assert got[0].ok is True
        assert got[0].model_ids == ("m-1", "m-2")
        assert load_providers() == []  # never persists

    def test_list_models_wraps_worker_exception(self, tmp_config_dir, monkeypatch):
        from ui.handlers import settings_handler as sh

        def boom(*_a):
            raise RuntimeError("network down")

        monkeypatch.setattr(sh, "fetch_model_ids", boom)

        got, evt = [], threading.Event()
        h = SettingsHandler()
        h.list_models("https://x/v1", "sk-k", "openai",
                      lambda r: (got.append(r), evt.set()))
        assert evt.wait(timeout=2.0), "list_models callback never fired"
        assert got[0].ok is False
        assert got[0].error == "RuntimeError: network down"


class TestSpec63CreateFromEnv:
    """§2.4c: env one-click card — vendor guard via the SINGLE
    preset_vendor_present helper, saved through add_or_update (prefer path),
    so agent.json default maintenance stays in save_providers."""

    def test_creates_and_saves_openai_card(self, tmp_config_dir):
        h = SettingsHandler()
        card = h.create_from_env("openai", "  sk-env-key  ")
        assert card is not None
        assert card.name == "openai"
        assert card.api_key == "sk-env-key"  # stripped
        assert card.default_model == "gpt-4o"
        assert card.caller == "openai"
        assert card.max_tokens == 128_000
        assert card.context_window == 128_000
        # RULING 3 adoption via add_or_update's prefer path (no handler-side
        # default maintenance).
        assert get_agent_setting("default_provider") == "openai"
        stored = load_providers()
        assert len(stored) == 1 and stored[0].name == "openai"

    def test_openrouter_prefix_stays_on_card_bare_in_agent_json(self, tmp_config_dir):
        """Debugger #23: the openrouter preset's prefixed default_model keeps
        the FULL value on the card; agent.json gets the BARE model."""
        h = SettingsHandler()
        card = h.create_from_env("openrouter", "sk-or")
        assert card is not None
        assert card.default_model == "openrouter/auto"
        assert card.caller == "openrouter"
        assert get_agent_setting("default_provider") == "openrouter"
        assert get_agent_setting("default_model") == "auto"
        assert load_providers()[0].default_model == "openrouter/auto"

    def test_vendor_guard_blocks_same_name_twice(self, tmp_config_dir):
        h = SettingsHandler()
        assert h.create_from_env("openai", "sk-1") is not None
        assert h.create_from_env("openai", "sk-2") is None
        assert len(load_providers()) == 1
        assert load_providers()[0].api_key == "sk-1"  # never clobbered

    def test_vendor_guard_blocks_renamed_card_same_vendor(self, tmp_config_dir):
        """Debugger #18: a renamed card must NOT free the vendor — the guard
        is preset_vendor_present (base_url OR name), not a name check. The
        stored card's NAME differs from preset.name; the base_url matches."""
        h = SettingsHandler()
        # Simulates the user renaming the card in the UI (stored entry now
        # "my-openai" with the OpenAI preset base_url) — no "openai"-named
        # card exists, so a name-only guard would let the chip through.
        h.add_or_update(_make_provider(
            "my-openai",
            base_url="https://api.openai.com/v1",
            default_model="gpt-4o",
            caller="openai",
        ))
        assert h.create_from_env("openai", "sk-2") is None
        names = [p.name for p in load_providers()]
        assert names == ["my-openai"], (
            f"vendor guard leaked a second same-vendor card: {names}"
        )
        assert load_providers()[0].api_key == "sk-my-openai-key"  # untouched

    def test_unknown_preset_returns_none(self, tmp_config_dir):
        h = SettingsHandler()
        # MiniMax was deferred out of the v1 catalog (§2.1 ruling) — unknown.
        assert h.create_from_env("minimax", "sk-x") is None
        assert load_providers() == []
