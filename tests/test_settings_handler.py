# tests/test_settings_handler.py
# Tests for ui/handlers/settings_handler.py — Settings dialog logic.
#
# Pattern: synchronous tests by passing GLib_module=None; test_provider uses
# threading.Event to wait for the daemon thread to finish.
#
# No GTK imports — pure handler logic.

import threading

import pytest

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
        assert providers[0].last_error == "401 unauthorized"
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
        assert providers[0].last_error == "401 unauthorized"

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
