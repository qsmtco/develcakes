# tests/test_settings_dialog.py
# Tests for ui/views/settings_dialog.py — pure view smoke tests.
# Pattern: construct the dialog with a real handler (using tmp_config_dir),
# inspect the widget tree, do not actually open a window (no Gtk.Window present).

import json

import pytest

try:
    from gi.repository import Gtk
    GTK_AVAILABLE = True
except (ImportError, ValueError):
    GTK_AVAILABLE = False

from agent.config import get_agent_setting
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


class TestContextWindowCopy:
    def test_save_dual_writes_spin_into_both_fields_on_disk(self, tmp_config_dir):
        """SPEC-38: the Context Window spin is the single source of truth.

        Stored provider has context_window=200_000 (the stale pre-SPEC-38
        state). User sets the spin to 1_000_000 and saves: BOTH max_tokens
        AND context_window on disk must read 1_000_000 — no save may leave
        a stale context_window behind a changed max_tokens.
        """
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1", context_window=200_000))
        d = SettingsDialog(parent=None, handler=h)
        d._cards[0]._max_tokens_spin.set_value(1_000_000)
        d._cards[0]._on_save_clicked()
        stored = {p.name: p for p in h.list_providers()}["p1"]
        assert stored.max_tokens == 1_000_000
        assert stored.context_window == 1_000_000, (
            "SPEC-38: dual-write violated — context_window not moved with max_tokens"
        )

    def test_untouched_asymmetric_card_saves_both_fields_unchanged(
        self, tmp_config_dir
    ):
        """Data-loss regression: an untouched card must not collapse the window.

        Stored card is asymmetric: max_tokens=128_000 (the output-intent
        default) but context_window=1_000_000 (the window the resolver
        actually reads, context_window-first). The spin displays the
        EFFECTIVE window, so it opens on 1_000_000.

        The user presses Save without touching the spin. Both stored fields
        must still read 1_000_000. Before the _effective_context_window fix
        the spin opened on the raw max_tokens (128_000), and the single-field
        write silently DOWNGRADED a 1M-window provider to 128K — the card
        displayed one number while the resolver used another.
        """
        h = SettingsHandler()
        h.add_or_update(
            _make_provider(
                "p1", max_tokens=128_000, context_window=1_000_000
            )
        )
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]

        # The spin must show the effective window, not the max_tokens cap.
        assert int(card._max_tokens_spin.get_value()) == 1_000_000, (
            "BUG #1: spin must display the context_window-first effective window"
        )

        # Save WITHOUT touching the spin.
        card._on_save_clicked()
        stored = {p.name: p for p in h.list_providers()}["p1"]
        assert stored.max_tokens == 1_000_000, (
            f"data loss: max_tokens downgraded to {stored.max_tokens:,} "
            "on an untouched save"
        )
        assert stored.context_window == 1_000_000, (
            f"data loss: context_window downgraded to {stored.context_window:,} "
            "on an untouched save"
        )

    def test_asymmetric_card_is_not_dirty_on_construction(self, tmp_config_dir):
        """Dirty-state trap: a clean asymmetric card must not read as dirty.

        _is_dirty() compares the spin against the stored value. Comparing
        against raw max_tokens while the spin displays the effective
        (context_window-first) window made every asymmetric card look
        permanently edited — a spurious unsaved-changes state the moment
        the dialog opened.
        """
        h = SettingsHandler()
        h.add_or_update(
            _make_provider(
                "p1", max_tokens=128_000, context_window=1_000_000
            )
        )
        d = SettingsDialog(parent=None, handler=h)
        assert d._cards[0]._is_dirty() is False, (
            "BUG #1: untouched asymmetric card reported as dirty on open"
        )

    def test_collect_from_form_dual_writes_spin(self, tmp_config_dir):
        """SPEC-38: _collect_from_form mirrors the spin into both fields."""
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        d = SettingsDialog(parent=None, handler=h)
        d._cards[0]._max_tokens_spin.set_value(1_000_000)
        collected = d._cards[0]._collect_from_form()
        assert collected.max_tokens == 1_000_000
        assert collected.context_window == 1_000_000

    def test_save_form_keeps_context_mode(self, tmp_config_dir):
        from utils.providers_store import load_providers
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1", context_mode="preload"))
        d = SettingsDialog(parent=None, handler=h)
        collected = d._cards[0]._collect_from_form()
        assert collected.context_mode == "preload"
        h.add_or_update(collected)
        assert load_providers()[0].context_mode == "preload"

    def test_save_form_invalid_context_mode_becomes_auto(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1", context_mode="nope"))
        d = SettingsDialog(parent=None, handler=h)
        # The stored card was coerced to auto on the way into the file.
        # Put a bad value on the in-memory provider the form copies from.
        d._cards[0]._provider.context_mode = "nope"
        assert d._cards[0]._collect_from_form().context_mode == "auto"


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
        assert card._supports_reasoning_check is not None
        assert card._reasoning_dropdown is not None
        assert card._reasoning_dropdown.get_sensitive() is False

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
        """A failed test surfaces the error on the status label.
        SPEC-63 delta #11/#12: the VIEW no longer writes _provider.last_error
        — the handler (_persist_test_result) is the SINGLE writer and
        persists the EXPLAINED string (pinned in tests/test_settings_handler.py).
        """
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        result = TestResult(ok=False, latency_ms=200,
                            error="HTTP 401: bad key",
                            model_used="p1/model-v1")
        card._on_test_result(result)
        # Raw string never lands on the in-memory card; the label shows the
        # explained message (§2.6c), the persisted string is the handler's.
        assert card._provider.last_error is None
        assert "bad key" not in card._status_label.get_text()

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


class TestReasoningCard:
    """T3: checkbox + dropdown survive the mirror chain; dropdown is inert."""

    def test_round_trip_checkbox_and_dropdown(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider(
            "p1", reasoning_effort="high", supports_reasoning=True,
        ))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        assert card._supports_reasoning_check.get_active() is True
        assert card._reasoning_dropdown.get_selected() == 3
        assert card._reasoning_dropdown.get_sensitive() is True
        collected = card._collect_from_form()
        assert collected.reasoning_effort == "high"
        assert collected.supports_reasoning is True
        h.add_or_update(collected)
        reloaded = h.list_providers()
        assert reloaded[0].reasoning_effort == "high"
        assert reloaded[0].supports_reasoning is True

    def test_is_dirty_flips_on_checkbox_and_dropdown(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        assert not card._is_dirty()
        card._supports_reasoning_check.set_active(True)
        assert card._is_dirty()
        card._supports_reasoning_check.set_active(False)
        assert not card._is_dirty()
        # Dropdown value is persisted even while the box is unchecked.
        card._reasoning_dropdown.set_selected(2)
        assert card._is_dirty()
        assert card._collect_from_form().reasoning_effort == "medium"

    def test_dropdown_inert_until_checked(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        assert card._reasoning_dropdown.get_sensitive() is False
        card._supports_reasoning_check.set_active(True)
        assert card._reasoning_dropdown.get_sensitive() is True
        card._supports_reasoning_check.set_active(False)
        assert card._reasoning_dropdown.get_sensitive() is False

    def test_changing_default_model_unchecks(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider(
            "p1", reasoning_effort="high", supports_reasoning=True,
        ))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        assert card._supports_reasoning_check.get_active() is True
        card._model_entry.set_text("openrouter/some-other-model")
        assert card._supports_reasoning_check.get_active() is False
        assert card._reasoning_dropdown.get_sensitive() is False
        # Level is kept so re-checking does not lose High.
        assert card._reasoning_dropdown.get_selected() == 3


# ═══════════════════════════════════════════════════════════════════
#  SPEC-22 §2.8 — Decision Model settings page
# ═══════════════════════════════════════════════════════════════════

class TestDecisionModelPage:
    def test_page_exists_and_name_is_locked(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        # The page is in the settings stack.
        assert d._stack.get_child_by_name("decision-model") is not None
        card = d._decision_card
        # The card is pre-named with the constant, and the name is FIXED.
        assert card._name_entry.get_text() == "DecisionModel"
        assert card._name_entry.get_editable() is False

    def test_present_card_hidden_from_general_providers_list(self, tmp_config_dir):
        h = SettingsHandler()
        # A real DecisionModel card stored via the same yaml store.
        h.add_or_update(_make_provider(
            "DecisionModel",
            base_url="https://jev.example/v1",
            default_model="typesafe/system-one",
        ), allow_empty_caller=True)
        h.add_or_update(_make_provider("Chat"))
        d = SettingsDialog(parent=None, handler=h)
        # Only the chat provider appears in the general list.
        assert [c._provider.name for c in d._cards] == ["Chat"]
        # The decision card reflects the stored card.
        assert d._decision_card._provider.name == "DecisionModel"
        assert d._decision_card._base_url_entry.get_text() == "https://jev.example/v1"

    def test_round_trips_through_save_load(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        card = d._decision_card
        card._base_url_entry.set_text("https://jev.example/v1")
        card._api_key_entry.set_text("jev-key")
        card._model_entry.set_text("typesafe/system-one")
        card._on_save_clicked()
        reloaded = {p.name: p for p in h.list_providers()}
        assert "DecisionModel" in reloaded
        stored = reloaded["DecisionModel"]
        assert stored.base_url == "https://jev.example/v1"
        assert stored.api_key == "jev-key"
        assert stored.default_model == "typesafe/system-one"
        # caller stays empty (allow_empty_caller) — the adapter sets its own.
        assert stored.caller == ""

    def test_absent_card_leaves_page_placeholder_editable(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        card = d._decision_card
        assert card._name_entry.get_text() == "DecisionModel"
        # No stored card yet — index lookups on the store see nothing.
        assert all(p.name != "DecisionModel" for p in h.list_providers())


# ═══════════════════════════════════════════════════════════════════
#  SPEC-55 — Improve Model settings page
# ═══════════════════════════════════════════════════════════════════

class TestImproveModelPage:
    def test_page_exists_and_name_is_locked(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        # The page is in the settings stack.
        assert d._stack.get_child_by_name("improve-model") is not None
        card = d._improve_card
        # The card is pre-named with the constant, and the name is FIXED.
        assert card._name_entry.get_text() == "ImproveModel"
        assert card._name_entry.get_editable() is False

    def test_page_reachable_from_group_navigation(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        names = [name for name, _btn in d._group_buttons]
        assert "improve-model" in names

    def test_present_card_hidden_from_general_providers_list(self, tmp_config_dir):
        h = SettingsHandler()
        # A real ImproveModel card stored via the same yaml store.
        h.add_or_update(_make_provider(
            "ImproveModel",
            base_url="https://improve.example/v1",
            default_model="openai/gpt-4o-mini",
        ), allow_empty_caller=True)
        h.add_or_update(_make_provider("Chat"))
        d = SettingsDialog(parent=None, handler=h)
        # Only the chat provider appears in the general list.
        assert [c._provider.name for c in d._cards] == ["Chat"]
        # The improve card reflects the stored card.
        assert d._improve_card._provider.name == "ImproveModel"
        assert d._improve_card._base_url_entry.get_text() == "https://improve.example/v1"

    def test_round_trips_through_save_load(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        card = d._improve_card
        card._base_url_entry.set_text("https://improve.example/v1")
        card._api_key_entry.set_text("improve-key")
        card._model_entry.set_text("openai/gpt-4o-mini")
        card._on_save_clicked()
        reloaded = {p.name: p for p in h.list_providers()}
        assert "ImproveModel" in reloaded
        stored = reloaded["ImproveModel"]
        assert stored.base_url == "https://improve.example/v1"
        assert stored.api_key == "improve-key"
        assert stored.default_model == "openai/gpt-4o-mini"
        # caller stays empty (allow_empty_caller) — improve uses the card verbatim.
        assert stored.caller == ""

    def test_absent_card_leaves_page_placeholder_editable(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        card = d._improve_card
        assert card._name_entry.get_text() == "ImproveModel"
        assert all(p.name != "ImproveModel" for p in h.list_providers())


# ═══════════════════════════════════════════════════════════════════
#  SPEC-53 §3 — Agents page: JEV room-fallback combo field
# ═══════════════════════════════════════════════════════════════════

class TestAgentsRoomFallback:
    """SPEC-53 Phase 3: one combo field, persisted to agent.json key
    ``jev_room_fallback`` (values supervisor|all|drop, default supervisor)."""

    def _agent_json(self, tmp_config_dir):
        return tmp_config_dir / "agent.json"

    def test_page_exists(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        assert d._stack.get_child_by_name("agents") is not None
        assert d._agents_fallback_dropdown is not None

    def test_default_on_missing_selects_supervisor(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        assert h.get_room_fallback() == "supervisor"
        # Index 0 is "Route to Supervisor".
        assert d._agents_fallback_dropdown.get_selected() == 0

    def test_save_load_round_trip(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        # Index 1 → "Broadcast to all" → value "all". Selecting persists now.
        d._agents_fallback_dropdown.set_selected(1)
        assert get_agent_setting("jev_room_fallback") == "all"
        assert h.get_room_fallback() == "all"
        # The raw agent.json holds the key.
        raw = json.loads(self._agent_json(tmp_config_dir).read_text())
        assert raw["jev_room_fallback"] == "all"
        # A fresh dialog loads the saved selection.
        h2 = SettingsHandler()
        d2 = SettingsDialog(parent=None, handler=h2)
        assert d2._agents_fallback_dropdown.get_selected() == 1
        assert h2.get_room_fallback() == "all"

    def test_drop_value_round_trip(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._agents_fallback_dropdown.set_selected(2)  # "Don't route" → "drop"
        assert h.get_room_fallback() == "drop"
        raw = json.loads(self._agent_json(tmp_config_dir).read_text())
        assert raw["jev_room_fallback"] == "drop"

    def test_corrupt_value_selects_default(self, tmp_config_dir):
        self._agent_json(tmp_config_dir).write_text(
            json.dumps({"jev_room_fallback": "not-a-valid-mode"})
        )
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        assert h.get_room_fallback() == "supervisor"
        assert d._agents_fallback_dropdown.get_selected() == 0

    def test_invalid_value_rejected_by_handler(self, tmp_config_dir):
        h = SettingsHandler()
        with pytest.raises(ValueError):
            h.set_room_fallback("nope")
        # The store is untouched.
        assert get_agent_setting("jev_room_fallback") is None

    def test_save_preserves_existing_agent_json_keys(self, tmp_config_dir):
        self._agent_json(tmp_config_dir).write_text(
            json.dumps({"default_model": "openai/gpt-4o", "user_id": "u-7"})
        )
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._agents_fallback_dropdown.set_selected(1)
        raw = json.loads(self._agent_json(tmp_config_dir).read_text())
        assert raw["jev_room_fallback"] == "all"
        # Unrelated keys survive the write.
        assert raw["default_model"] == "openai/gpt-4o"
        assert raw["user_id"] == "u-7"


# ═══════════════════════════════════════════════════════════════════
#  SPEC-63 §2.6 — preset menu, preset card, unsaved survival, model
#  read path, error display, env chips. Requires a display (xvfb-run -a)
#  — same GTK guard as the rest of this file.
# ═══════════════════════════════════════════════════════════════════

from utils.model_catalog import ModelListResult  # noqa: E402
from utils.provider_presets import PRESETS  # noqa: E402

_ENV_NAMES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")


class TestSpec63PresetMenu:
    """§2.6(a): 4 presets + Custom…, activated through the RESOLUTION path
    (v3 delta #9/#10 — lookup_action bypasses GTK name resolution and cannot
    catch the dead-menu bug this criterion exists for)."""

    def test_menu_model_has_four_presets_plus_custom(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        model = d._add_btn.get_popover().get_menu_model()
        assert model.get_n_items() == len(PRESETS) + 1
        actions = [
            model.get_item_attribute_value(i, "action").get_string()
            for i in range(model.get_n_items())
        ]
        assert actions[: len(PRESETS)] == [
            f"settings.add-preset.{p.key}" for p in PRESETS
        ]
        assert actions[-1] == "settings.add-custom"

    def test_resolution_path_activation_creates_preset_card(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        assert d._add_btn.activate_action("settings.add-preset.openai", None) is True
        assert len(d._cards) == 1
        card = d._cards[0]
        assert card._name_entry.get_text() == "openai"
        assert card._base_url_entry.get_text() == "https://api.openai.com/v1"
        assert card._model_entry.get_text() == "gpt-4o"

    def test_every_preset_menu_item_resolves(self, tmp_config_dir):
        h = SettingsHandler()
        for preset in PRESETS:
            d = SettingsDialog(parent=None, handler=h)
            assert d._add_btn.activate_action(
                f"settings.add-preset.{preset.key}", None) is True
            card = d._cards[-1]
            assert card._name_entry.get_text() == preset.name
            assert card._provider.base_url == preset.base_url

    def test_custom_reproduces_old_blank_card(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_custom_clicked()
        assert len(d._cards) == 1
        card = d._cards[0]
        # Old blank-card behavior, byte-for-byte: all fields empty, unsaved,
        # not dirty (nothing typed), Remove just removes the card.
        assert card._provider.name == ""
        assert card._model_entry.get_text() == ""
        assert card._is_new is True
        assert card._saved is False
        assert card._is_dirty() is False
        # The old entry point still exists (spec: preserve it).
        assert callable(d._on_add_provider_clicked)


class TestSpec63PresetCard:
    """§2.6(b): the executed-trace contract — fresh preset card is NOT dirty;
    caller lands in _provider; key is placeholder-only."""

    def test_fresh_preset_card_is_not_dirty(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openai")
        card = d._cards[-1]
        assert card._is_dirty() is False, (
            "preset card dirty before the user typed anything (executed-trace regression)"
        )

    def test_key_typing_makes_card_dirty(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openai")
        card = d._cards[-1]
        card._api_key_entry.set_text("sk-test")
        assert card._is_dirty() is True

    def test_preset_key_is_placeholder_only(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openai")
        card = d._cards[-1]
        assert card._api_key_entry.get_text() == ""  # never a VALUE
        assert card._api_key_entry.get_placeholder_text() == "sk-…"

    def test_collected_caller_comes_from_preset(self, tmp_config_dir):
        """Without caller in _provider, an unprefixed model ('gpt-4o') raises
        'Cannot auto-detect caller' at Save (settings_handler :113-121)."""
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("google")
        card = d._cards[-1]
        card._api_key_entry.set_text("AIzaTest")
        collected = card._collect_from_form()
        assert collected.caller == "openai"
        assert collected.default_model == "openai/gemini-2.0-flash"

    def test_anthropic_card_has_no_load_models_button(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("anthropic")
        assert d._cards[-1]._load_models_btn is None
        assert d._cards[-1]._key_link is not None

    def test_get_a_key_link_carries_catalog_uri(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openai")
        link = d._cards[-1]._key_link
        assert link.get_uri() == "https://platform.openai.com/api-keys"

    def test_save_preset_card_round_trips_catalog_fields(self, tmp_config_dir):
        """Save on a fresh preset card persists catalog values + typed key."""
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openai")
        card = d._cards[-1]
        card._api_key_entry.set_text("sk-saved")
        card._on_save_clicked(None)
        stored = h.list_providers()
        assert len(stored) == 1
        assert stored[0].name == "openai"
        assert stored[0].default_model == "gpt-4o"
        assert stored[0].caller == "openai"
        assert stored[0].api_key == "sk-saved"
        assert stored[0].max_tokens == 128_000
        assert card._saved is True


class TestSpec63UnsavedSurvival:
    """§2.6(b2) Debugger #2 (CRITICAL): saving provider B must not delete
    unsaved preset card A — along with the API key the user just typed."""

    def test_saving_b_keeps_unsaved_preset_card_a(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("beta"))
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openai")
        card_a = d._cards[-1]
        card_a._api_key_entry.set_text("sk-typed-key")

        d._cards[0]._on_save_clicked(None)      # save B
        d.refresh_providers(h.list_providers())  # wiring's refresh

        assert card_a in d._cards, "unsaved preset card A was reconciled away"
        assert card_a._api_key_entry.get_text() == "sk-typed-key"
        assert len(d._cards) == 2

    def test_saved_flag_set_before_add_or_update(self, tmp_config_dir, monkeypatch):
        """delta #4/#5: _saved flips BEFORE add_or_update — the handler fires
        on_providers_changed SYNCHRONOUSLY inside the call."""
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openai")
        card = d._cards[-1]
        card._api_key_entry.set_text("sk-x")

        seen = {}

        real_add = h.add_or_update

        def spy_add(provider, **kw):
            seen["saved_during_call"] = card._saved
            return real_add(provider, **kw)

        monkeypatch.setattr(h, "add_or_update", spy_add)
        card._on_save_clicked(None)
        assert seen["saved_during_call"] is True
        assert card._saved is True

    def test_failed_save_resets_saved_flag(self, tmp_config_dir):
        """delta #4: ValueError from add_or_update must reset _saved False —
        the card stays unsaved and reconcilable-away."""
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_custom_clicked()
        card = d._cards[-1]
        card._saved = True   # simulate the flag having been set optimistically
        card._on_save_clicked(None)  # empty fields → ValueError inside
        assert card._saved is False


class TestSpec63ModelReadPath:
    """§2.6(d): _current_model_text is the ONLY model read path; the
    re-prefix rule anchors at READ time, gated on base_url (delta #7)."""

    def test_openrouter_raw_id_gets_prefixed_at_read(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openrouter")
        card = d._cards[-1]
        card._model_entry.set_text("qwen/qwen3-max")
        assert card._current_model_text() == "openrouter/qwen/qwen3-max"
        collected = card._collect_from_form()
        assert collected.default_model == "openrouter/qwen/qwen3-max"

    def test_already_anchored_value_not_double_prefixed(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openrouter")
        card = d._cards[-1]
        # populate wrote the stored 'openrouter/auto'
        assert card._current_model_text() == "openrouter/auto"

    def test_non_openrouter_base_url_never_prefixed(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("x", base_url="https://api.example.com/v1"))
        d = SettingsDialog(parent=None, handler=h)
        card = d._cards[0]
        card._model_entry.set_text("myorg/model")
        assert card._current_model_text() == "myorg/model"

    def test_loaded_models_dropdown_writes_raw_id_to_entry(self, tmp_config_dir, monkeypatch):
        """delta #8: the dropdown is display-only; the ENTRY is the authority.
        Picking a RAW vendor id stores the raw id; the prefix appears only at
        read time."""
        from ui.handlers import settings_handler as sh
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openrouter")
        card = d._cards[-1]
        # Type a value that matches a RAW /models id so the preselection
        # branch fires ('auto' from the catalog default matches nothing).
        card._model_entry.set_text("qwen/qwen3-max")

        ids = ("qwen/qwen3-max", "anthropic/claude-x", "openai/gpt-4o")
        monkeypatch.setattr(
            sh.SettingsHandler, "list_models",
            lambda self_, base_url, api_key, caller, on_result: on_result(
                ModelListResult(ok=True, model_ids=tuple(ids), error=None)),
        )
        card._on_load_models_clicked()
        # The dropdown took over display; the entry holds the picked RAW id.
        assert card._model_entry.get_text() == "qwen/qwen3-max"
        assert card._model_dropdown.get_visible() is True
        # Read time applies the anchor.
        assert card._current_model_text() == "openrouter/qwen/qwen3-max"
        # The user changed the model away from the stored catalog default —
        # the card IS dirty (delta #8's non-dirty claim is about the refresh
        # path, not about a real user edit).
        assert card._is_dirty() is True

    def test_edit_manually_restores_entry_never_blanks(self, tmp_config_dir, monkeypatch):
        from ui.handlers import settings_handler as sh
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openrouter")
        card = d._cards[-1]
        card._model_entry.set_text("qwen/qwen3-max")
        ids = ("qwen/qwen3-max",)
        monkeypatch.setattr(
            sh.SettingsHandler, "list_models",
            lambda self_, base_url, api_key, caller, on_result: on_result(
                ModelListResult(ok=True, model_ids=tuple(ids), error=None)),
        )
        card._on_load_models_clicked()
        assert card._model_dropdown.get_visible() is True
        card._on_edit_manually_clicked()
        assert card._model_entry.get_visible() is True
        # "Current value" restored = the RAW selected id (delta #8: the
        # prefix exists only at read time, so the entry holds raw), and the
        # value is never blanked.
        assert card._model_entry.get_text() == "qwen/qwen3-max"
        assert card._current_model_text() == "openrouter/qwen/qwen3-max"

    def test_loaded_models_with_no_match_defers_to_entry(self, tmp_config_dir, monkeypatch):
        """Catalog default 'auto' matches no raw id → dropdown stays hidden,
        the user's entry value is kept (never blanked)."""
        from ui.handlers import settings_handler as sh
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openrouter")
        card = d._cards[-1]  # entry holds 'openrouter/auto' from the preset
        monkeypatch.setattr(
            sh.SettingsHandler, "list_models",
            lambda self_, base_url, api_key, caller, on_result: on_result(
                ModelListResult(ok=True, model_ids=("qwen/qwen3-max",), error=None)),
        )
        card._on_load_models_clicked()
        assert card._model_dropdown.get_visible() is False
        assert card._model_entry.get_visible() is True
        assert card._current_model_text() == "openrouter/auto"
        assert card._is_dirty() is False


class TestSpec63ErrorDisplay:
    """§2.6(c): the view translates ONLY the immediate label; the persisted
    string is the handler's explained stamp (Wave B)."""

    def test_401_label_explained_and_link_visible(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openai")
        card = d._cards[-1]
        card._on_test_result(TestResult(
            ok=False, latency_ms=5, error='HTTP 401: {"error":"bad key"}',
            model_used="gpt-4o"))
        assert card._status_label.get_text() == (
            "❌ Key rejected — check or regenerate it."
        )
        assert card._key_link.get_visible() is True
        assert card._provider.last_error is None  # view never writes it

    def test_non_auth_failure_hides_get_a_key_link(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openai")
        card = d._cards[-1]
        card._on_test_result(TestResult(
            ok=False, latency_ms=5, error="HTTP 404: Not Found",
            model_used="gpt-4o"))
        assert card._key_link.get_visible() is False
        assert "Base URL looks wrong" in card._status_label.get_text()

    def test_success_hides_get_a_key_link(self, tmp_config_dir):
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openai")
        card = d._cards[-1]
        card._on_test_result(TestResult(
            ok=True, latency_ms=5, error=None, model_used="gpt-4o"))
        assert card._key_link.get_visible() is False

    def test_model_class_404_shows_model_message(self, tmp_config_dir):
        """Debugger #17: an OpenAI-compatible 404 whose body names the model
        is a MODEL error, not a wrong-Base-URL error."""
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        d._on_add_preset_clicked("openai")
        card = d._cards[-1]
        card._on_test_result(TestResult(
            ok=False, latency_ms=5,
            error="HTTP 404: The model 'qwen/qwen3-max' does not exist",
            model_used="qwen/qwen3-max"))
        assert "Model ID not accepted" in card._status_label.get_text()


class TestSpec63EnvChips:
    """§2.6(e): chips only for vendor-missing env keys; the VALUE never
    surfaces; click creates the card via create_from_env."""

    def _clear_env(self, monkeypatch):
        for name in _ENV_NAMES:
            monkeypatch.delenv(name, raising=False)

    def test_no_env_keys_no_chips(self, tmp_config_dir, monkeypatch):
        self._clear_env(monkeypatch)
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        assert d._env_chips == []
        assert d._env_chip_box.get_visible() is False

    def test_chip_appears_for_vendor_missing_key(self, tmp_config_dir, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-SUPER-SECRET-123")
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        assert len(d._env_chips) == 1
        label = d._env_chips[0][1].get_label()
        # VAR NAME + preset label — never the VALUE (security invariant).
        assert "OPENAI_API_KEY" in label
        assert "OpenAI" in label
        assert "sk-SUPER-SECRET-123" not in label
        for widget in (d._env_chip_box,):
            assert "sk-SUPER-SECRET-123" not in str(widget)

    def test_chip_suppressed_when_vendor_card_exists(self, tmp_config_dir, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        h = SettingsHandler()
        # Debugger #18: a RENAMED card still suppresses (base_url half of the
        # SINGLE preset_vendor_present helper).
        h.add_or_update(_make_provider(
            "my-openai",
            base_url="https://api.openai.com/v1",
            default_model="gpt-4o", caller="openai",
        ))
        d = SettingsDialog(parent=None, handler=h)
        assert d._env_chips == []

    def test_chip_click_creates_card_then_disappears(self, tmp_config_dir, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-click-key")
        h = SettingsHandler()
        d = SettingsDialog(parent=None, handler=h)
        chip = d._env_chips[0][1]
        chip.emit("clicked")
        stored = h.list_providers()
        assert len(stored) == 1
        assert stored[0].name == "openai"
        assert stored[0].api_key == "sk-click-key"  # delivered via the closure
        assert get_agent_setting("default_provider") == "openai"
        # wiring fires refresh_providers after a provider change → chip hides.
        d.refresh_providers(h.list_providers())
        assert chip.get_visible() is False
