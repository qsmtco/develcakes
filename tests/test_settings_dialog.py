# tests/test_settings_dialog.py
# Tests for ui/views/settings_dialog.py — pure view smoke tests.
# Pattern: construct the dialog with a real handler (using tmp_config_dir),
# inspect the widget tree, do not actually open a window (no Gtk.Window present).

import pytest

try:
    from gi.repository import Gtk
    GTK_AVAILABLE = True
except (ImportError, ValueError):
    GTK_AVAILABLE = False

from models.providers import ProviderConfig
from ui.handlers.settings_handler import SettingsHandler
from ui.views.settings_dialog import SettingsDialog
from utils.provider_test import TestResult


pytestmark = pytest.mark.skipif(not GTK_AVAILABLE, reason="GTK not available")


def _make_provider(name: str = "test", **overrides) -> ProviderConfig:
    defaults = dict(
        name=name,
        base_url=f"https://api.{name}.example.com/v1",
        api_key="test-key",
        default_model=f"openai/{name}-model",
    )
    defaults.update(overrides)
    return ProviderConfig(**defaults)


class TestEmptyState:
    def test_no_providers_shows_empty_state(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        assert d._empty_state is not None
        assert d._empty_state.get_visible() is True

    def test_with_providers_hides_empty_state(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        d = SettingsDialog(parent=None, handler=h)
        assert d._empty_state.get_visible() is False


class TestProviderCards:
    def test_one_provider_renders_one_card(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        d = SettingsDialog(parent=None, handler=h)
        assert len(d._cards) == 1
        assert d._cards[0].get_widget().get_visible() is True

    def test_two_providers_render_two_cards(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        h.add_or_update(_make_provider("p2"))
        d = SettingsDialog(parent=None, handler=h)
        assert len(d._cards) == 2

    def test_add_provider_button_appends_card(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        initial = len(d._cards)
        d._on_add_provider_clicked(None)
        assert len(d._cards) == initial + 1

    def test_card_has_all_widgets(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        assert card._name_entry is not None
        assert card._base_url_entry is not None
        assert card._model_entry is not None
        assert card._api_key_entry is not None
        assert card._test_btn is not None
        assert card._remove_btn is not None
        assert card._save_btn is not None

    def test_api_key_entry_is_password(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        assert card._api_key_entry.get_visibility() is False
        assert card._api_key_entry.get_input_purpose() == Gtk.InputPurpose.PASSWORD


class TestRemoveCallback:
    def test_remove_unsaved_card_directly(self, tmp_config_dir):
        """Removing an unsaved card just removes the widget, no handler call."""
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_provider_clicked(None)
        assert len(d._cards) == 1
        card = d._cards[0]
        assert card._is_new is True
        card._on_remove_clicked(None)
        assert len(d._cards) == 0

    def test_remove_saved_card_calls_handler(self, tmp_config_dir, monkeypatch):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        d = SettingsDialog(parent=None, handler=h)

        # We can't easily run the GTK main loop to process the MessageDialog's
        # idle_add callback. Instead, verify that handler.remove() actually
        # removes the provider and the dialog's on_providers_changed refreshes.
        h.remove("p1")
        d.refresh_providers(h.list_providers())
        assert len(d._cards) == 0
        assert h.list_providers() == []


class TestSaveFlow:
    def test_save_valid_calls_handler_add_or_update(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_provider_clicked(None)
        new_card = d._cards[-1]
        new_card._name_entry.set_text("newprov")
        new_card._base_url_entry.set_text("https://x.example.com/v1")
        new_card._api_key_entry.set_text("test-api-key")
        # caller-validation spec: default_model must be "<vendor>/<model>"
        # so the auto-detect can derive a caller.
        new_card._model_entry.set_text("openai/newprov-model")
        new_card._on_save_clicked(None)
        names = [p.name for p in h.list_providers()]
        assert "newprov" in names

    def test_save_invalid_shows_error_in_status_label(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_provider_clicked(None)
        new_card = d._cards[-1]
        new_card._name_entry.set_text("")  # empty -> ValueError
        new_card._base_url_entry.set_text("https://x.example.com/v1")
        new_card._api_key_entry.set_text("test-api-key")
        new_card._model_entry.set_text("model")
        new_card._on_save_clicked(None)
        status = new_card._status_label.get_text().lower()
        assert "required" in status or "name" in status


class TestRefreshProviders:
    def test_refresh_rebuilds_cards(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("a"))
        d = SettingsDialog(parent=None, handler=h)
        assert len(d._cards) == 1
        h.add_or_update(_make_provider("b"))
        d.refresh_providers(h.list_providers())
        assert len(d._cards) == 2

    def test_refresh_repopulates_from_saved_data(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("a"))
        d = SettingsDialog(parent=None, handler=h)
        # User edits the entry without saving — card is now dirty
        d._cards[0]._name_entry.set_text("edited-but-unsaved")
        # Refresh from handler (which still has "a")
        d.refresh_providers(h.list_providers())
        # Dirty card preserves the unsaved edit (new behavior)
        assert d._cards[0]._name_entry.get_text() == "edited-but-unsaved"
        # But the stored provider ref is updated
        assert d._cards[0]._provider.name == "a"

    def test_refresh_updates_clean_card_in_place(self, tmp_config_dir):
        """A clean card (no unsaved edits) gets updated in place on refresh."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("a"))
        d = SettingsDialog(parent=None, handler=h)
        assert d._cards[0]._name_entry.get_text() == "a"
        # Update the provider via handler (simulates external change).
        # caller-validation spec: default_model must be "<vendor>/<model>" so
        # the auto-detect can derive a caller. Use openai/ prefix.
        h.add_or_update(_make_provider("a", default_model="openai/new-model"))
        d.refresh_providers(h.list_providers())
        # Clean card should reflect the new data
        assert d._cards[0]._model_entry.get_text() == "openai/new-model"


class TestRefreshProvidersPreservesUnsavedEdits:
    """BUG #4 fix: unsaved edits on one card survive a refresh triggered
    by saving or testing another card."""

    def test_unsaved_edits_survive_save_on_other_card(self, tmp_config_dir):
        """Saving card B should not wipe unsaved edits on card A."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("alpha"))
        h.add_or_update(_make_provider("beta"))
        d = SettingsDialog(parent=None, handler=h)

        assert len(d._cards) == 2
        alpha_card = d._cards[0]
        beta_card = d._cards[1]

        # User edits alpha's name without saving — card is dirty
        alpha_card._name_entry.set_text("alpha-edited")
        assert alpha_card._is_dirty()

        # Save beta (fires on_providers_changed → refresh_providers)
        beta_card._on_save_clicked(None)
        d.refresh_providers(h.list_providers())

        # Alpha's unsaved edit must survive
        assert alpha_card._name_entry.get_text() == "alpha-edited"

    def test_unsaved_edits_survive_remove_of_other_card(self, tmp_config_dir):
        """Removing card B should not wipe unsaved edits on card A."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("alpha"))
        h.add_or_update(_make_provider("beta"))
        d = SettingsDialog(parent=None, handler=h)

        alpha_card = d._cards[0]

        # User edits alpha's base_url without saving
        alpha_card._base_url_entry.set_text("https://edited.example.com/v1")
        assert alpha_card._is_dirty()

        # Remove beta via handler (fires on_providers_changed)
        h.remove("beta")
        d.refresh_providers(h.list_providers())

        # Alpha's unsaved edit survives, and beta is gone
        assert len(d._cards) == 1
        assert d._cards[0]._base_url_entry.get_text() == "https://edited.example.com/v1"

    def test_clean_card_updates_on_refresh(self, tmp_config_dir):
        """A clean card (no edits) gets updated with fresh data on refresh."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("alpha"))
        d = SettingsDialog(parent=None, handler=h)

        # Card is clean — no edits
        assert not d._cards[0]._is_dirty()

        # Externally update the provider. caller-validation spec: default_model
        # must be "<vendor>/<model>" so auto-detect can derive a caller.
        h.add_or_update(_make_provider("alpha", default_model="openai/updated-model"))
        d.refresh_providers(h.list_providers())

        # Clean card reflects the new data
        assert d._cards[0]._model_entry.get_text() == "openai/updated-model"


class TestMaxTokensSpinButton:
    def test_spin_button_renders(self, tmp_config_dir):
        """Every provider card has a Context Window spin button."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        assert card._max_tokens_spin is not None

    def test_spin_button_populated_from_provider(self, tmp_config_dir):
        """Spin button shows the stored max_tokens value."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1", max_tokens=200_000))
        d = SettingsDialog(parent=None, handler=h)
        assert d._cards[0]._max_tokens_spin.get_value() == 200_000

    def test_collect_from_form_includes_max_tokens(self, tmp_config_dir):
        """Save flow persists the spin button value to YAML."""
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_provider_clicked(None)
        card = d._cards[-1]
        card._name_entry.set_text("p1")
        card._base_url_entry.set_text("https://x.example.com/v1")
        card._api_key_entry.set_text("test-key")
        # caller-validation spec: default_model must be "<vendor>/<model>"
        # so the auto-detect can derive a caller.
        card._model_entry.set_text("openai/p1-model")
        card._max_tokens_spin.set_value(500_000)
        card._on_save_clicked(None)
        saved = h.list_providers()
        assert saved[0].max_tokens == 500_000

    def test_is_dirty_flips_when_spin_edited(self, tmp_config_dir):
        """Editing the spin button makes the card dirty."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        assert not card._is_dirty()
        card._max_tokens_spin.set_value(200_000)
        assert card._is_dirty()

    def test_on_test_result_prefills_spin(self, tmp_config_dir):
        """TestResult with context_window pre-fills spin when stored is default."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1", max_tokens=128_000))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        result = TestResult(ok=True, latency_ms=200, error=None,
                            model_used="p1/model-v1", context_window=1_000_000)
        card._on_test_result(result)
        assert card._max_tokens_spin.get_value() == 1_000_000

    def test_on_test_result_does_not_overwrite_customized(self, tmp_config_dir):
        """If user has customized max_tokens, Test Connection doesn't overwrite."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1", max_tokens=500_000))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        result = TestResult(ok=True, latency_ms=200, error=None,
                            model_used="p1/model-v1", context_window=1_000_000)
        card._on_test_result(result)
        assert card._max_tokens_spin.get_value() == 500_000

    def test_on_test_result_no_prefill_when_context_window_none(self, tmp_config_dir):
        """TestResult without context_window does not touch the spin button."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        result = TestResult(ok=True, latency_ms=200, error=None,
                            model_used="p1/model-v1", context_window=None)
        card._on_test_result(result)
        assert card._max_tokens_spin.get_value() == 128_000

    def test_spin_button_does_not_affect_new_card_default(self, tmp_config_dir):
        """A new (unsaved) card starts with the default 128_000."""
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_provider_clicked(None)
        card = d._cards[-1]
        assert card._max_tokens_spin.get_value() == 128_000

    # ── BUG #6 + #9 regression tests ─────────────────────────────────────────
    # The dialog's _on_test_result() used to:
    #   (a) update the spin button to the discovered context_window, but
    #       leave self._provider.max_tokens stale (audit BUG #6) → future
    #       _is_dirty() calls kept returning True even after no user edit.
    #   (b) never update self._provider.last_verified_at (audit BUG #9) →
    #       the dialog stayed "Untested" until refresh_providers() was called
    #       by the parent window.

    def test_on_test_result_updates_provider_ref_to_match_spin(self, tmp_config_dir):
        """After Test Connection succeeds with context_window=1M, the dialog's
        _provider.max_tokens must be 1_000_000 (matching the spin)."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1", max_tokens=128_000))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        # Sanity: pre-test _provider.max_tokens is sentinel
        assert card._provider.max_tokens == 128_000
        result = TestResult(ok=True, latency_ms=200, error=None,
                            model_used="p1/model-v1", context_window=1_000_000)
        card._on_test_result(result)
        # After test: spin AND _provider must both reflect the discovered value
        assert card._max_tokens_spin.get_value() == 1_000_000
        assert card._provider.max_tokens == 1_000_000, (
            f"BUG #6: _provider.max_tokens is stale: {card._provider.max_tokens!r}"
        )

    def test_on_test_result_updates_last_verified_at(self, tmp_config_dir):
        """After Test Connection succeeds, _provider.last_verified_at is set
        (audit BUG #9)."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        # Pre-test: last_verified_at is None
        assert card._provider.last_verified_at is None
        result = TestResult(ok=True, latency_ms=200, error=None,
                            model_used="p1/model-v1", context_window=None)
        card._on_test_result(result)
        assert card._provider.last_verified_at is not None, (
            "BUG #9: last_verified_at must be set after a successful test"
        )
        # And it's a valid ISO 8601 timestamp
        from datetime import datetime
        # Should parse without raising
        parsed = datetime.fromisoformat(card._provider.last_verified_at)
        assert parsed is not None

    def test_on_test_result_clears_last_error_on_success(self, tmp_config_dir):
        """A successful test should clear any prior error."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1", last_error="old error"))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        result = TestResult(ok=True, latency_ms=200, error=None,
                            model_used="p1/model-v1", context_window=None)
        card._on_test_result(result)
        assert card._provider.last_error is None

    def test_on_test_result_failure_stamps_error(self, tmp_config_dir):
        """A failed test should stamp the error on _provider.last_error so
        refresh_providers() doesn't revert to 'Untested'."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        result = TestResult(ok=False, latency_ms=200,
                            error="HTTP 401: bad key",
                            model_used="p1/model-v1")
        card._on_test_result(result)
        assert card._provider.last_error == "HTTP 401: bad key"

    def test_on_test_result_does_not_change_max_tokens_when_not_sentinel(self, tmp_config_dir):
        """If user has customized max_tokens, the spin value must NOT change
        AND _provider.max_tokens must stay in sync."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1", max_tokens=500_000))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        result = TestResult(ok=True, latency_ms=200, error=None,
                            model_used="p1/model-v1", context_window=1_000_000)
        card._on_test_result(result)
        assert card._max_tokens_spin.get_value() == 500_000
        assert card._provider.max_tokens == 500_000

    # BUG #7 regression (dialog side): if default_max_tokens is stamped (by
    # agent builder), the dialog's pre-fill must NOT overwrite max_tokens
    # even when it equals the 128K sentinel — same sentinel as settings_handler.

    def test_on_test_result_respects_default_max_tokens_sentinel(self, tmp_config_dir):
        """BUG #7: provider with default_max_tokens=128_000 (wizard-stamped)
        must NOT have its max_tokens overwritten by a discovered 1M context."""
        from models.providers import ProviderConfig
        h = SettingsHandler()
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
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        result = TestResult(ok=True, latency_ms=200, error=None,
                            model_used="openrouter/free", context_window=1_000_000)
        card._on_test_result(result)
        # Wizard's choice must be preserved
        assert card._max_tokens_spin.get_value() == 128_000
        assert card._provider.max_tokens == 128_000, (
            f"BUG #7 (dialog): wizard-stamped value was overwritten: "
            f"max_tokens={card._provider.max_tokens}"
        )
