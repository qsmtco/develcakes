# ui/views/settings_dialog.py
# GTK4 dialog for managing LLM provider settings.
#
# Pure view — receives data from SettingsHandler, emits user actions
# back through handler methods. No direct file I/O or network calls.
#
# Architecture rule (ARCHITECTURE.md Section 9):
#   - Uses add_css_class() only, no inline CssProvider
#   - No business logic — delegates to handler for validation/persistence
#   - CSS classes: settings-*

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

import gi

gi.require_version('Gtk', '4.0')

from gi.repository import Gtk

from agent.config import ROOM_FALLBACK_VALUES as _ROOM_FALLBACK_VALUES
from agent.llm.jev_client import DECISION_MODEL_PROVIDER as _DECISION_MODEL_PROVIDER
from models.providers import (
    ProviderConfig,
    coerce_provider_context_mode,
    validate_provider_reasoning_effort,
)
from utils import stt_config as _stt_config

# SPEC-22 §2.8: the structural handle the code looks the JEV card up by. The
# Settings page pre-names its card with this; the general Providers list and
# agent-provider dropdowns filter it out (it is not a chat model).
_REASONING_LEVELS = ("off", "low", "medium", "high")

# SPEC-53 §3: the agents-section combo. Order matches ROOM_FALLBACK_VALUES
# in agent/config.py (supervisor | all | drop), so dropdown index i maps to
# _ROOM_FALLBACK_VALUES[i].
_AGENTS_FALLBACK_LABELS = (
    "Route to Supervisor",
    "Broadcast to all",
    "Don't route",
)

# SPEC-37: the "STT Model" page reads through the _stt_config MODULE (never
# `from … import` names) so tests can monkeypatch stt_config.* cleanly.
if TYPE_CHECKING:
    from ui.handlers.settings_handler import SettingsHandler
    from utils.provider_test import TestResult

logger = logging.getLogger(__name__)

# Stack child name, button label. Order matches the old header switcher.
_SETTINGS_GROUPS = (
    ("providers", "Providers"),
    ("telegram", "Telegram Bridge"),
    ("decision-model", "Decision Model"),
    ("stt", "STT Model"),
    ("speech", "Speech"),
    ("agents", "Agents"),
)


class _ProviderCard:
    """A single provider's edit form. Pure view — delegates to handler."""

    def __init__(self, dialog: SettingsDialog, provider: ProviderConfig | None, *,
                 fixed_name: bool = False, allow_empty_caller: bool = False,
                 test_runner=None):
        """If provider is None, this is a new (unsaved) card with empty fields.

        ``fixed_name`` locks the Name entry (used for the DecisionModel card,
        whose name is the code's structural handle — SPEC-22 §2.8).
        ``allow_empty_caller`` lets a card save with caller="" (the JEV adapter
        sets its own; we do not invent caller detection — fix-round JOB 2).
        ``test_runner`` overrides Test Connection (the DecisionModel probe).
        """
        self._dialog = dialog
        self._is_new = provider is None
        self._fixed_name = fixed_name
        self._allow_empty_caller = allow_empty_caller
        self._test_runner = test_runner
        self._provider = provider or ProviderConfig(
            name="", base_url="", api_key="", default_model="", caller="",
        )
        self._build_widgets()
        if provider is not None:
            self._populate_from_provider()

    def _build_widgets(self) -> None:
        self._frame = Gtk.Frame()
        self._frame.add_css_class("settings-provider-card")

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)
        vbox.set_margin_top(10)
        vbox.set_margin_bottom(10)

        # Name
        self._name_entry = Gtk.Entry()
        self._name_entry.set_placeholder_text("Provider name")
        self._name_entry.set_hexpand(True)
        if self._fixed_name:
            self._name_entry.set_editable(False)
            self._name_entry.set_can_focus(False)
            self._name_entry.set_tooltip_text(
                "Fixed name — this card is looked up by the code via a "
                "structural constant; only its contents are user data."
            )
        name_row = self._labeled("Name", self._name_entry)
        vbox.append(name_row)

        # Base URL
        self._base_url_entry = Gtk.Entry()
        self._base_url_entry.set_placeholder_text("https://api.example.com/v1")
        self._base_url_entry.set_hexpand(True)
        url_row = self._labeled("Base URL", self._base_url_entry)
        vbox.append(url_row)

        # Default model
        self._model_entry = Gtk.Entry()
        self._model_entry.set_placeholder_text("model-id")
        self._model_entry.set_hexpand(True)
        model_row = self._labeled("Default Model", self._model_entry)
        vbox.append(model_row)

        # API key (password + reveal toggle)
        self._api_key_entry = Gtk.Entry()
        self._api_key_entry.set_placeholder_text("API key")
        self._api_key_entry.set_hexpand(True)
        self._api_key_entry.set_visibility(False)
        self._api_key_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)

        self._reveal_btn = Gtk.Button(label="👁")
        self._reveal_btn.add_css_class("flat")
        self._reveal_btn.set_size_request(36, -1)
        self._reveal_btn.connect("clicked", self._on_reveal_clicked)

        api_key_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        api_key_box.append(self._api_key_entry)
        api_key_box.append(self._reveal_btn)
        api_key_row = self._labeled("API Key", api_key_box)
        vbox.append(api_key_row)

        # Read-only caller label — shows the resolved API caller (openai|minimax|...).
        # Caller is auto-detected by settings_handler.add_or_update when saving.
        self._caller_label = Gtk.Label()
        self._caller_label.set_xalign(0.0)
        self._caller_label.add_css_class("dim-label")
        caller_row = self._labeled("Caller", self._caller_label)
        vbox.append(caller_row)

        # Context window — editable; pre-filled by Test Connection.
        # Default 128_000 matches the dataclass default and runtime fallback.
        # Show the EFFECTIVE window: the resolver is context_window-first,
        # so a card holding max_tokens=128k + context_window=1M must display
        # 1M — not the max_tokens output cap (retro-audit BUG #1).
        self._max_tokens_spin = Gtk.SpinButton.new_with_range(1_000, 10_000_000, 1_000)
        self._max_tokens_spin.set_value(self._effective_context_window(self._provider))
        self._max_tokens_spin.set_hexpand(True)
        max_tokens_row = self._labeled("Context Window", self._max_tokens_spin)
        vbox.append(max_tokens_row)

        # Phase A — Editable compaction_threshold.
        # Range 0.50 — 0.95, step 0.05 (10% to 95% of context window).
        # Default 0.80 matches the dataclass default and runtime fallback.
        self._compaction_threshold_spin = Gtk.SpinButton.new_with_range(0.50, 0.95, 0.05)
        self._compaction_threshold_spin.set_value(
            self._provider.compaction_threshold or 0.80
        )
        self._compaction_threshold_spin.set_hexpand(True)
        threshold_row = self._labeled(
            "Compaction threshold", self._compaction_threshold_spin
        )
        vbox.append(threshold_row)

        # Supports reasoning — send-side guard. Default off so a non-reasoning
        # OpenRouter model cannot 400 on first Save. The dropdown is inert
        # unless this box is checked.
        self._supports_reasoning_check = Gtk.CheckButton()
        self._supports_reasoning_check.set_active(False)
        self._supports_reasoning_check.connect(
            "toggled", self._on_supports_reasoning_toggled
        )
        supports_row = self._labeled(
            "Supports reasoning", self._supports_reasoning_check
        )
        vbox.append(supports_row)

        self._reasoning_dropdown = Gtk.DropDown.new_from_strings(
            ["Off", "Low", "Medium", "High"]
        )
        self._reasoning_dropdown.set_selected(0)
        self._reasoning_dropdown.set_hexpand(True)
        self._reasoning_dropdown.set_sensitive(False)
        reasoning_row = self._labeled("Reasoning", self._reasoning_dropdown)
        vbox.append(reasoning_row)

        # Changing Default Model drops the opt-in — a DeepSeek card pointed
        # at GLM must not stay flagged. Populate sets a guard so loading
        # a stored card does not trip this.
        self._populating = False
        self._model_entry.connect("changed", self._on_model_changed_reset_reasoning)

        # Status label
        self._status_label = Gtk.Label(label="Untested")
        self._status_label.add_css_class("settings-status-untested")
        self._status_label.set_halign(Gtk.Align.START)
        self._status_label.set_wrap(True)
        vbox.append(self._status_label)

        # Button row: Test | Save | Remove
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        self._test_btn = Gtk.Button(label="Test Connection")
        self._test_btn.add_css_class("settings-test-btn")
        self._test_btn.connect("clicked", self._on_test_clicked)
        btn_row.append(self._test_btn)

        self._save_btn = Gtk.Button(label="Save")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.connect("clicked", self._on_save_clicked)
        btn_row.append(self._save_btn)

        self._remove_btn = Gtk.Button(label="Remove")
        self._remove_btn.add_css_class("settings-remove-btn")
        self._remove_btn.connect("clicked", self._on_remove_clicked)
        btn_row.append(self._remove_btn)

        vbox.append(btn_row)
        self._frame.set_child(vbox)

    @staticmethod
    def _effective_context_window(p: ProviderConfig | None) -> int:
        """The window the resolver actually uses: context_window-first.

        The "Context Window" spin displays and writes the EFFECTIVE window
        (retro-audit BUG #1): a card holding max_tokens=128k (output intent)
        plus context_window=1M (window) must display and preserve 1M — the
        resolver reads context_window first, so the window is the value Save
        must not destroy. Falls back to max_tokens, then the dataclass/
        runtime default of 128_000.
        """
        if p is None:
            return 128_000
        return int(p.context_window or p.max_tokens or 128_000)

    def _reasoning_level_from_dropdown(self) -> str:
        idx = int(self._reasoning_dropdown.get_selected())
        if 0 <= idx < len(_REASONING_LEVELS):
            return _REASONING_LEVELS[idx]
        return "off"

    def _on_supports_reasoning_toggled(self, *_args) -> None:
        self._reasoning_dropdown.set_sensitive(
            self._supports_reasoning_check.get_active()
        )

    def _on_model_changed_reset_reasoning(self, *_args) -> None:
        """Uncheck Supports reasoning when Default Model diverges from stored."""
        if self._populating:
            return
        current = self._model_entry.get_text().strip()
        stored = (self._provider.default_model or "").strip()
        if current != stored:
            self._supports_reasoning_check.set_active(False)

    def _labeled(self, text: str, widget: Gtk.Widget) -> Gtk.Box:
        """Label above the control so the row fits the settings sidebar."""
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        label = Gtk.Label(label=text)
        label.set_halign(Gtk.Align.START)
        label.add_css_class("dim-label")
        widget.set_hexpand(True)
        row.append(label)
        row.append(widget)
        return row

    def _populate_from_provider(self) -> None:
        p = self._provider
        self._populating = True
        try:
            self._name_entry.set_text(p.name or "")
            self._base_url_entry.set_text(p.base_url or "")
            self._model_entry.set_text(p.default_model or "")
            self._api_key_entry.set_text(p.api_key or "")
            self._caller_label.set_text(
                f"  {p.caller}" if p.caller else "  (auto-detected on save)"
            )
            # Effective window, not raw max_tokens — see
            # _effective_context_window (retro-audit BUG #1).
            self._max_tokens_spin.set_value(self._effective_context_window(p))
            self._compaction_threshold_spin.set_value(p.compaction_threshold or 0.80)
            self._supports_reasoning_check.set_active(bool(p.supports_reasoning))
            level = validate_provider_reasoning_effort(
                getattr(p, "reasoning_effort", "off")
            )
            self._reasoning_dropdown.set_selected(
                _REASONING_LEVELS.index(level) if level in _REASONING_LEVELS else 0
            )
            self._reasoning_dropdown.set_sensitive(bool(p.supports_reasoning))
        finally:
            self._populating = False

    def _is_dirty(self) -> bool:
        """True if any entry field differs from the stored provider values.
        Used by refresh_providers to decide whether to update a card in place
        or leave it alone (preserving the user's unsaved edits)."""
        p = self._provider
        return (
            self._name_entry.get_text().strip() != (p.name or "")
            or self._base_url_entry.get_text().strip() != (p.base_url or "")
            or self._model_entry.get_text().strip() != (p.default_model or "")
            or self._api_key_entry.get_text().strip() != (p.api_key or "")
            # Compare against the EFFECTIVE window: the spin displays
            # context_window-or-max_tokens, so an untouched asymmetric card
            # must not read as dirty (retro-audit BUG #1).
            or int(self._max_tokens_spin.get_value())
            != self._effective_context_window(p)
            or float(self._compaction_threshold_spin.get_value()) != (p.compaction_threshold or 0.80)
            or bool(self._supports_reasoning_check.get_active()) != bool(p.supports_reasoning)
            or self._reasoning_level_from_dropdown() != (
                validate_provider_reasoning_effort(getattr(p, "reasoning_effort", "off"))
            )
        )

    def _update_provider_ref(self, provider: ProviderConfig) -> None:
        """Replace the stored provider reference and refresh the status label.
        Entry fields are NOT touched — that's the caller's responsibility."""
        self._provider = provider
        if provider.last_verified_at:
            self._set_status("✅ Verified", ok=True)
        elif provider.last_error:
            self._set_status(f"❌ {provider.last_error}", fail=True)
        else:
            self._set_status("Untested")

    def _collect_from_form(self) -> ProviderConfig:
        """Collect current form values into a ProviderConfig."""
        existing = self._provider
        return ProviderConfig(
            name=self._name_entry.get_text().strip(),
            base_url=self._base_url_entry.get_text().strip(),
            api_key=self._api_key_entry.get_text().strip(),
            default_model=self._model_entry.get_text().strip(),
            caller=existing.caller if existing else "",
            enabled=existing.enabled if existing else True,
            supports_tools=existing.supports_tools if existing else True,
            supports_streaming=existing.supports_streaming if existing else True,
            max_tokens=int(self._max_tokens_spin.get_value()),
            default_max_tokens=existing.default_max_tokens if existing else 0,
            # SPEC-38: dual write — the "Context Window" spin IS the window,
            # and max_tokens and context_window are irrevocably unified: every
            # save writes BOTH fields to the spin's value, so an inverted-
            # asymmetric card (max_tokens > context_window) collapses to the
            # window on save. That narrowing is accepted by design — do NOT
            # "restore" a separate output-cap control on the assumption the
            # old max_tokens value survives; it does not.
            context_window=int(self._max_tokens_spin.get_value()),
            compaction_threshold=float(
                self._compaction_threshold_spin.get_value()
            ),  # Phase A
            last_verified_at=existing.last_verified_at if existing else None,
            last_error=existing.last_error if existing else None,
            context_mode=coerce_provider_context_mode(
                getattr(existing, "context_mode", "auto") if existing else "auto"
            ),
            reasoning_effort=self._reasoning_level_from_dropdown(),
            supports_reasoning=bool(self._supports_reasoning_check.get_active()),
        )

    def _on_reveal_clicked(self, *args) -> None:
        """Toggle API key visibility."""
        current = self._api_key_entry.get_visibility()
        self._api_key_entry.set_visibility(not current)

    def _on_save_clicked(self, *args) -> None:
        """Save the card's current form values via handler."""
        provider = self._collect_from_form()
        try:
            self._dialog._handler.add_or_update(
                provider, allow_empty_caller=self._allow_empty_caller,
            )
        except ValueError as e:
            self._set_status(str(e), fail=True)

    def _on_test_clicked(self, *args) -> None:
        """Run Test Connection via handler. Does not block."""
        provider = self._collect_from_form()
        self._status_label.set_text("Testing...")
        self._status_label.remove_css_class("settings-status-ok")
        self._status_label.remove_css_class("settings-status-fail")
        self._status_label.add_css_class("settings-status-untested")
        if self._test_runner is not None:
            # DecisionModel: an honest pre-adapter probe (reachability), not a
            # chat completion. Runs off-thread exactly like test_provider.
            self._test_runner()
            return
        self._dialog._handler.test_provider(provider, self._on_test_result)

    def _apply_test_result_inline(self, result: TestResult) -> None:
        """Update status + spin from a TestResult without touching the store.

        The DecisionModel probe is not a stored chat provider, so it must not
        stamp `last_verified_at` in providers.yaml; this mirrors only the
        presentation half of `_on_test_result`.
        """
        if result.ok:
            self._set_status(f"✅ {result.latency_ms}ms · {result.model_used}", ok=True)
        else:
            self._set_status(f"❌ {result.error or 'unknown error'}", fail=True)

    def _on_remove_clicked(self, *args) -> None:
        """Remove this provider. Shows confirmation first."""
        if self._is_new:
            # Unsaved card — just remove from the dialog
            self._dialog._remove_card(self)
            return

        name = self._provider.name
        dialog = Gtk.MessageDialog(
            transient_for=self._dialog._parent,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f'Remove provider "{name}"?',
        )
        dialog.set_property(
            "secondary-text",
            "This cannot be undone.",
        )

        def on_response(_dlg, response_id):
            _dlg.close()
            if response_id == Gtk.ResponseType.YES:
                self._dialog._handler.remove(name)

        dialog.connect("response", on_response)
        dialog.show()

    def _on_test_result(self, result: TestResult) -> None:
        """Called on the GTK main thread with the test result."""
        if result.ok:
            self._set_status(f"✅ {result.latency_ms}ms", ok=True)
            # Pre-fill context window if discovered and user hasn't customized.
            # Sentinel matches settings_handler: max_tokens == 128_000 AND
            # default_max_tokens == 0 (no wizard stamp). If the wizard
            # stamped default_max_tokens, the value is intentional and we
            # leave it alone (audit BUG #7).
            new_max_tokens = self._provider.max_tokens
            new_context_window = self._provider.context_window
            user_has_customized = (
                self._provider.max_tokens != 128_000
                or (self._provider.default_max_tokens or 0) > 0
            )
            if result.context_window and not user_has_customized:
                self._max_tokens_spin.set_value(result.context_window)
                new_max_tokens = result.context_window
                # Mirror the handler's dual-write (settings_handler dual-writes
                # BOTH max_tokens and context_window). Updating only max_tokens
                # here left the in-memory context_window stale, so the spin —
                # which displays the EFFECTIVE window, context_window-first —
                # no longer matched _is_dirty()'s comparison after a successful
                # test on an asymmetric card: the card read as dirty forever
                # (retro-audit BUG #2).
                new_context_window = result.context_window
                self._status_label.set_text(
                    f"✅ {result.latency_ms}ms · context: {result.context_window:,}"
                )
            # BUG #6 + #9 fix: update self._provider to reflect what the
            # handler has now written to disk. Without this, _is_dirty() stays
            # True forever (spin != p.max_tokens), and the next refresh shows
            # a stale "Untested" label. Use a fresh ISO timestamp since the
            # test succeeded just now — the handler's exact timestamp will be
            # reconciled on the next refresh_providers() call.
            from datetime import UTC, datetime
            self._provider = replace(
                self._provider,
                max_tokens=new_max_tokens,
                # Mirror the handler's dual-write on the in-memory copy too —
                # omitting it leaves context_window stale, so _effective_context_window()
                # keeps returning the old number while the spin shows the new
                # one, and the card reads as permanently dirty (retro-audit BUG #2).
                context_window=new_context_window,
                last_verified_at=datetime.now(UTC).isoformat(),
                last_error=None,
            )
        else:
            error_msg = result.error or "unknown error"
            self._set_status(f"❌ {error_msg}", fail=True)
            # Stamp the error on the in-memory provider too so refresh doesn't
            # revert to "Untested".
            self._provider = replace(
                self._provider,
                last_error=error_msg,
            )

    def _set_status(self, text: str, *, ok: bool = False, fail: bool = False) -> None:
        self._status_label.set_text(text)
        self._status_label.remove_css_class("settings-status-ok")
        self._status_label.remove_css_class("settings-status-fail")
        self._status_label.remove_css_class("settings-status-untested")
        if ok:
            self._status_label.add_css_class("settings-status-ok")
        elif fail:
            self._status_label.add_css_class("settings-status-fail")
        else:
            self._status_label.add_css_class("settings-status-untested")

    def get_widget(self) -> Gtk.Frame:
        return self._frame


class SettingsDialog:
    """Settings surface hosted in the left-notebook Settings tab.

    Pure view — delegates all persistence to SettingsHandler.
    Not a window. LeftPanel parents ``widget()`` into the Settings page.

    Args:
        parent: Parent Gtk.Window for transient MessageDialogs (may be None).
        handler: SettingsHandler — the data gateway.
        on_close: Optional callback invoked from release(). Idempotent.
        telegram_controller: Optional TelegramSettingsController — the data
            gateway for the "Telegram Bridge" section (SPEC-15 SP2). Injected
            (never imported by the view — layer rule) so the section stays
            honest/inert when not wired. Source-compatible: an existing
            `SettingsDialog(parent=..., handler=...)` call is unchanged.
    """

    def __init__(
        self,
        parent: Gtk.Window | None,
        *,
        handler: SettingsHandler,
        on_close=None,
        telegram_controller=None,
    ):
        self._handler = handler
        self._on_close = on_close
        self._parent = parent
        self._telegram = telegram_controller
        self._cards: list[_ProviderCard] = []
        self._group_buttons: list[tuple[str, Gtk.Button]] = []

        self._root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._root.set_vexpand(True)

        # Sectioned layout: a Gtk.Stack. The vertical group list below
        # replaces the old header StackSwitcher. Page names are unchanged.
        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)
        self._stack.set_hexpand(True)
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        # ── Providers page (existing content, byte-identical behavior) ─
        providers_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Scrollable body
        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_vexpand(True)
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._list_box.set_margin_start(16)
        self._list_box.set_margin_end(16)
        self._list_box.set_margin_top(12)
        self._list_box.set_margin_bottom(12)

        # Empty state
        self._empty_state = Gtk.Label(
            label="No providers configured.\nAdd your first provider below."
        )
        self._empty_state.add_css_class("settings-empty-state")
        self._empty_state.set_justify(Gtk.Justification.CENTER)
        self._list_box.append(self._empty_state)

        self._scrolled.set_child(self._list_box)
        providers_page.append(self._scrolled)

        # + Add Provider button (bottom)
        add_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        add_btn_box.set_margin_start(16)
        add_btn_box.set_margin_end(16)
        add_btn_box.set_margin_top(4)
        add_btn_box.set_margin_bottom(8)
        self._add_btn = Gtk.Button(label="+ Add Provider")
        self._add_btn.add_css_class("suggested-action")
        self._add_btn.set_hexpand(True)
        self._add_btn.connect("clicked", self._on_add_provider_clicked)
        add_btn_box.append(self._add_btn)
        providers_page.append(add_btn_box)

        self._stack.add_titled(providers_page, "providers", "Providers")

        # ── Telegram Bridge page (SPEC-15 SP2) ────────────────────────
        telegram_page = self._build_telegram_page()
        self._stack.add_titled(telegram_page, "telegram", "Telegram Bridge")

        # ── Decision Model page (SPEC-22 §2.8) ────────────────────────
        self._decision_page = self._build_decision_page()
        self._stack.add_titled(self._decision_page, "decision-model", "Decision Model")

        # ── STT Model page (SPEC-37) ─────────────────────────────────
        self._stt_page = self._build_stt_page()
        self._stack.add_titled(self._stt_page, "stt", "STT Model")

        # ── Speech page (talk tool) ──────────────────────────────────
        self._speech_page = self._build_speech_page()
        self._stack.add_titled(self._speech_page, "speech", "Speech")

        # ── Agents page (SPEC-53 §3) ─────────────────────────────────
        self._agents_page = self._build_agents_page()
        self._stack.add_titled(self._agents_page, "agents", "Agents")

        self._group_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._group_box.set_margin_start(8)
        self._group_box.set_margin_end(8)
        self._group_box.set_margin_top(8)
        self._group_box.set_margin_bottom(4)
        for name, title in _SETTINGS_GROUPS:
            button = Gtk.Button()
            button.add_css_class("settings-group-btn")
            button.set_hexpand(True)
            label = Gtk.Label(label=title)
            label.set_wrap(True)
            label.set_xalign(0)
            button.set_child(label)
            button.connect("clicked", self._on_group_button_clicked, name)
            self._group_box.append(button)
            self._group_buttons.append((name, button))
        # Providers is the stack's first page. Highlight it without a
        # set_visible_child_name call so the default stays "providers".
        self._group_buttons[0][1].add_css_class("settings-group-active")

        page_scroll = Gtk.ScrolledWindow()
        page_scroll.set_vexpand(True)
        page_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        page_scroll.set_child(self._stack)

        self._root.append(self._group_box)
        self._root.append(page_scroll)

        # Populate from current handler state
        self.refresh_providers(handler.list_providers())

    # ── Public API ────────────────────────────────────────────────────

    def widget(self) -> Gtk.Widget:
        """The box LeftPanel parents into the Settings page. No window."""
        return self._root

    def release(self) -> None:
        """Cancel an in-flight Telegram pairing poll. Idempotent.

        Does not reload the token entry — _refresh_telegram_section writes
        the stored token back and would wipe an unsaved edit.
        """
        if self._telegram is not None:
            try:
                self._telegram.cancel_pairing()
            except Exception as e:  # noqa: BLE001 — teardown must not block close
                logger.warning("telegram pairing teardown failed: %s", e)
            pair_btn = getattr(self, "_telegram_pair_btn", None)
            if pair_btn is not None:
                pair_btn.set_sensitive(bool(self._telegram.has_saved_token()))
            if getattr(self, "_telegram_status_label", None) is not None:
                self._telegram_set_status("")
        if self._on_close is not None:
            self._on_close()

    def _on_group_button_clicked(self, _button, name: str) -> None:
        self._select_group(name)

    def _select_group(self, name: str) -> None:
        """Show one existing stack page and highlight its button."""
        self._stack.set_visible_child_name(name)
        for group_name, button in self._group_buttons:
            if group_name == name:
                button.add_css_class("settings-group-active")
            else:
                button.remove_css_class("settings-group-active")

    def refresh_providers(self, providers: list[ProviderConfig]) -> None:
        """Incrementally update the card list from the given provider list.

        Strategy: match cards by provider name. Existing cards are updated
        in place (dirty cards are left alone to preserve unsaved edits),
        new providers get new cards, removed providers have their cards
        deleted.

        Clean cards (no unsaved edits) are updated with fresh data from
        the provider list. Dirty cards keep their entry values but get
        their provider reference updated so status labels reflect test results.

        SPEC-22 §2.8: the DecisionModel card is NOT a chat provider — it is
        filtered out of the general Providers list (it lives on its own page).
        """
        # SPEC-22 §2.8: sync the Decision Model page from the UNFILTERED list
        # (its card lives on its own page), then filter it out of the general
        # Providers list — it is not a chat provider.
        self._refresh_decision_card(providers)
        providers = [
            p for p in providers
            if getattr(p, "name", "") != _DECISION_MODEL_PROVIDER
        ]

        # Build lookup of current cards by provider name
        current_by_name: dict[str, _ProviderCard] = {}
        for card in self._cards:
            name = card._provider.name
            if name:
                current_by_name[name] = card

        new_names = {p.name for p in providers}
        current_names = set(current_by_name.keys())

        # Remove cards for providers that no longer exist in yaml
        for name in current_names - new_names:
            card = current_by_name[name]
            self._list_box.remove(card.get_widget())
            self._cards.remove(card)

        # Update existing cards or add new ones
        for provider in providers:
            if provider.name in current_by_name:
                card = current_by_name[provider.name]
                if card._is_dirty():
                    # Dirty card — preserve unsaved edits, just update
                    # the provider ref so status labels stay current
                    card._update_provider_ref(provider)
                else:
                    # Clean card — update in place from new data
                    card._provider = provider
                    card._populate_from_provider()
            else:
                # New provider — create a card
                card = _ProviderCard(self, provider)
                self._cards.append(card)
                self._list_box.append(card.get_widget())

        # Toggle empty state
        self._empty_state.set_visible(len(self._cards) == 0)

    # ── Internal ──────────────────────────────────────────────────────

    def _on_add_provider_clicked(self, *args) -> None:
        """Append a new empty card for the user to fill in."""
        card = _ProviderCard(self, None)
        self._cards.append(card)
        self._list_box.append(card.get_widget())
        # Hide empty state
        self._empty_state.set_visible(False)

    def _remove_card(self, card: _ProviderCard) -> None:
        """Remove a card from the list (for unsaved new cards)."""
        if card in self._cards:
            self._cards.remove(card)
            self._list_box.remove(card.get_widget())
        self._empty_state.set_visible(len(self._cards) == 0)

    # ── Telegram Bridge section (SPEC-15 SP2) ─────────────────────────

    def _build_telegram_page(self) -> Gtk.Box:
        """Build the "Telegram Bridge" section widgets.

        Pure view: reads initial state from the injected controller and
        delegates every action back to it. When no controller is wired, the
        section renders inert (honest, no dead controls).
        """
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_start(16)
        page.set_margin_end(16)
        page.set_margin_top(12)
        page.set_margin_bottom(12)

        intro = Gtk.Label()
        intro.set_xalign(0.0)
        intro.set_wrap(True)
        intro.add_css_class("dim-label")
        intro.set_text(
            "Bridge this project to Telegram so the Supervisor can reach you "
            "while you're away. The bot token is a credential — it is stored "
            "owner-only and never logged."
        )
        page.append(intro)

        # Bot token (password + reveal toggle, cloned from _ProviderCard).
        self._telegram_token_entry = Gtk.Entry()
        self._telegram_token_entry.set_placeholder_text("123456:ABC-DEF…")
        self._telegram_token_entry.set_hexpand(True)
        self._telegram_token_entry.set_visibility(False)
        self._telegram_token_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self._telegram_token_entry.set_tooltip_text(
            "Bot token from @BotFather (123456:ABC-DEF…). Stored owner-only, "
            "never logged. Get one by messaging @BotFather on Telegram.")

        self._telegram_reveal_btn = Gtk.Button(label="👁")
        self._telegram_reveal_btn.add_css_class("flat")
        self._telegram_reveal_btn.set_size_request(36, -1)
        self._telegram_reveal_btn.connect(
            "clicked", self._on_telegram_reveal_clicked
        )

        token_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        token_box.append(self._telegram_token_entry)
        token_box.append(self._telegram_reveal_btn)
        page.append(self._labeled_row("Bot token", token_box))

        # Paired-chat status row.
        self._telegram_paired_label = Gtk.Label()
        self._telegram_paired_label.set_xalign(0.0)
        self._telegram_paired_label.set_wrap(True)
        page.append(self._labeled_row("Paired chat", self._telegram_paired_label))

        # Result / status line (Test + Pairing feedback).
        self._telegram_status_label = Gtk.Label(label="")
        self._telegram_status_label.set_xalign(0.0)
        self._telegram_status_label.set_wrap(True)
        page.append(self._telegram_status_label)

        # Button row: Test | Save | Start Pairing
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._telegram_test_btn = Gtk.Button(label="Test")
        self._telegram_test_btn.set_tooltip_text(
            "Verify the token with Telegram (getMe) without saving it.")
        self._telegram_test_btn.connect("clicked", self._on_telegram_test_clicked)
        btn_row.append(self._telegram_test_btn)

        self._telegram_save_btn = Gtk.Button(label="Save")
        self._telegram_save_btn.add_css_class("suggested-action")
        self._telegram_save_btn.connect("clicked", self._on_telegram_save_clicked)
        btn_row.append(self._telegram_save_btn)

        self._telegram_pair_btn = Gtk.Button(label="Start Pairing")
        self._telegram_pair_btn.set_tooltip_text(
            "Pair this bot to ONE phone chat: send the shown code from "
            "Telegram, then confirm. Binds the bridge to that chat only.")
        self._telegram_pair_btn.connect(
            "clicked", self._on_telegram_pair_clicked
        )
        btn_row.append(self._telegram_pair_btn)
        page.append(btn_row)

        self._refresh_telegram_section()
        return page

    # ── Decision Model section (SPEC-22 §2.8) ─────────────────────────

    def _build_decision_page(self) -> Gtk.Box:
        """Build the "Decision Model" page: ONE fixed-name provider card.

        The card is a REAL ProviderConfig in providers.yaml (same store, same
        round-trip) reached by the code through `DECISION_MODEL_PROVIDER`. Its
        name is locked; every other field is user-editable. It reuses the
        _ProviderCard widget so the save/round-trip path cannot drift from the
        general provider path (the reasoning-effort field-strip lesson).

        The stored card (if any) is injected here lazily; when absent this page
        shows an empty placeholder card the user can fill and Save to create it.
        """
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_start(16)
        page.set_margin_end(16)
        page.set_margin_top(12)
        page.set_margin_bottom(12)

        intro = Gtk.Label()
        intro.set_xalign(0.0)
        intro.set_wrap(True)
        intro.add_css_class("dim-label")
        intro.set_text(
            "The Decision Model is an optional service. Its name is "
            "fixed — the code looks it up structurally — but the endpoint, key, "
            "and model are yours. Point it at any compatible service. The failure "
            "classifier and the repo-map reranker both use this card, and either "
            "one falls back to its non-network behavior when the card is missing "
            "or the call fails. The rest of the app is unaffected."
        )
        page.append(intro)

        self._decision_card = _ProviderCard(
            self,
            # Placeholder; synced from the store by _refresh_decision_card.
            ProviderConfig(
                name=_DECISION_MODEL_PROVIDER,
                base_url="", api_key="", default_model="", caller="",
            ),
            fixed_name=True,
            allow_empty_caller=True,  # the JEV adapter sets its own caller
            test_runner=self._decision_test_runner,
        )
        page.append(self._decision_card.get_widget())
        return page

    def _refresh_decision_card(self, providers: list[ProviderConfig]) -> None:
        """Sync the Decision Model card with the stored card (if present).

        ``providers`` is the full unfiltered list; the caller filters only the
        general list. A dirty card keeps the user's unsaved edits.
        """
        card = getattr(self, "_decision_card", None)
        if card is None:
            return
        stored = next(
            (p for p in providers
             if getattr(p, "name", "") == _DECISION_MODEL_PROVIDER),
            None,
        )
        if stored is None:
            # No stored card — leave the empty placeholder editable but keep
            # the fixed name present so Save can create it.
            card._name_entry.set_text(_DECISION_MODEL_PROVIDER)
            return
        if card._is_dirty():
            card._update_provider_ref(stored)
        else:
            card._provider = stored
            card._populate_from_provider()

    def _decision_test_runner(self) -> None:
        """Run the honest pre-adapter DecisionModel probe off-thread.

        Not a stored chat provider, so it must NOT stamp providers.yaml. The
        result updates the card's status label only (see
        `_ProviderCard._apply_test_result_inline`).
        """
        card = self._decision_card
        provider = card._collect_from_form()

        def _worker():
            try:
                from agent.llm.jev_client import probe_decision_model
                result = probe_decision_model(provider)
            except Exception as exc:  # noqa: BLE001 — probe failure is its payload
                from utils.provider_test import TestResult
                result = TestResult(
                    ok=False, latency_ms=0, error=f"probe raised: {exc}",
                    model_used="pre-adapter probe",
                )

            def _dispatch():
                try:
                    card._apply_test_result_inline(result)
                except Exception:
                    logger.exception("decision-model probe result dispatch failed")

            glib = getattr(card._dialog._handler, "_GLib", None)
            if glib is not None and hasattr(glib, "idle_add"):
                glib.idle_add(_dispatch)
            else:
                _dispatch()

        import threading
        threading.Thread(target=_worker, daemon=True, name="decision-model-probe").start()

    # ── STT Model section (SPEC-37) ─────────────────────────────────────

    def _build_stt_page(self) -> Gtk.Box:
        """Build the "STT Model" page: dropdown + Custom… free-text entry.

        Pure view — every read/write goes through the ``_stt_config`` module
        (imported as a module handle, never `from … import`, so tests can
        monkeypatch stt_config.*). When the store is degraded the section
        renders inert (Telegram pattern: honest, no dead controls).
        """
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_start(16)
        page.set_margin_end(16)
        page.set_margin_top(12)
        page.set_margin_bottom(12)

        intro = Gtk.Label()
        intro.set_xalign(0.0)
        intro.set_wrap(True)
        intro.add_css_class("dim-label")
        intro.set_text(
            "The model used for push-to-talk dictation (OpenRouter cloud "
            "STT, ~$0.0004 per 15s typical). The choice applies on the NEXT "
            "dictation — no app restart needed."
        )
        page.append(intro)

        # Model dropdown: measured choices + a trailing "Custom…" entry that
        # reveals a free-text field. Selection is stored as an index; the
        # mapping back to model ids lives in self._stt_model_ids.
        self._stt_model_ids: list[str] = [
            m for m, _label in _stt_config.STT_MODEL_CHOICES
        ]
        labels = [label for _m, label in _stt_config.STT_MODEL_CHOICES]
        labels.append("Custom…")
        self._stt_dropdown = Gtk.DropDown.new_from_strings(labels)
        self._stt_dropdown.set_hexpand(True)
        self._stt_custom_entry = Gtk.Entry()
        self._stt_custom_entry.set_placeholder_text("provider/model-id")
        self._stt_custom_entry.set_hexpand(True)
        self._stt_apply_btn = Gtk.Button(label="Apply")
        self._stt_apply_btn.add_css_class("suggested-action")
        self._stt_apply_btn.connect("clicked", self._on_stt_apply_clicked)
        custom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        custom_box.append(self._stt_custom_entry)
        custom_box.append(self._stt_apply_btn)
        page.append(self._labeled_row("Model", self._stt_dropdown))
        page.append(self._labeled_row("Custom", custom_box))

        self._stt_hint_label = Gtk.Label()
        self._stt_hint_label.set_xalign(0.0)
        self._stt_hint_label.set_wrap(True)
        self._stt_hint_label.add_css_class("dim-label")
        self._stt_hint_label.set_text(
            "Keyword biasing (DevelCakes/Crabcakes) applies only to "
            "mai-transcribe-2."
        )
        page.append(self._stt_hint_label)

        # Status line: save feedback, or the degradation reason when inert.
        self._stt_status_label = Gtk.Label(label="")
        self._stt_status_label.set_xalign(0.0)
        self._stt_status_label.set_wrap(True)
        page.append(self._stt_status_label)

        self._stt_dropdown_handler_id = self._stt_dropdown.connect(
            "notify::selected", self._on_stt_dropdown_changed
        )
        self._refresh_stt_section()
        return page

    def _build_speech_page(self) -> Gtk.Box:
        """Speech endpoint, model, and token for the talk tool.

        The form stays hidden until the user clicks Add/Edit Speech.
        A blank token on a later save keeps the stored token.
        """
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_start(16)
        page.set_margin_end(16)
        page.set_margin_top(12)
        page.set_margin_bottom(12)

        intro = Gtk.Label()
        intro.set_xalign(0.0)
        intro.set_wrap(True)
        intro.add_css_class("dim-label")
        intro.set_text(
            "The talk tool sends text to this endpoint. The token stays on "
            "this machine. The choice applies on the next talk — no app "
            "restart needed."
        )
        page.append(intro)

        self._speech_add_btn = Gtk.Button(label="+ Add Speech")
        self._speech_add_btn.add_css_class("suggested-action")
        self._speech_add_btn.set_hexpand(True)
        self._speech_add_btn.connect("clicked", self._on_speech_add_clicked)
        page.append(self._speech_add_btn)

        self._speech_form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._speech_endpoint_entry = Gtk.Entry()
        self._speech_endpoint_entry.set_hexpand(True)
        self._speech_endpoint_entry.set_placeholder_text("https://host/api/v1/audio/speech")
        self._speech_model_entry = Gtk.Entry()
        self._speech_model_entry.set_hexpand(True)
        self._speech_model_entry.set_placeholder_text("provider/model-id")
        self._speech_voice_entry = Gtk.Entry()
        self._speech_voice_entry.set_hexpand(True)
        self._speech_voice_entry.set_placeholder_text("algenib (optional)")
        self._speech_formats: list[str] = ["mp3", "pcm"]
        self._speech_format_dropdown = Gtk.DropDown.new_from_strings(
            self._speech_formats
        )
        self._speech_format_dropdown.set_hexpand(True)
        self._speech_token_entry = Gtk.Entry()
        self._speech_token_entry.set_hexpand(True)
        self._speech_token_entry.set_visibility(False)
        self._speech_token_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self._speech_token_entry.set_placeholder_text("Bearer token")
        reveal = Gtk.Button(label="Show")
        reveal.connect("clicked", self._on_speech_reveal_clicked)
        token_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        token_box.append(self._speech_token_entry)
        token_box.append(reveal)
        self._speech_form.append(self._labeled_row("Endpoint", self._speech_endpoint_entry))
        self._speech_form.append(self._labeled_row("Model", self._speech_model_entry))
        self._speech_form.append(self._labeled_row("Voice", self._speech_voice_entry))
        self._speech_form.append(
            self._labeled_row("Format", self._speech_format_dropdown)
        )
        self._speech_form.append(self._labeled_row("Token", token_box))
        self._speech_save_btn = Gtk.Button(label="Save")
        self._speech_save_btn.add_css_class("suggested-action")
        self._speech_save_btn.connect("clicked", self._on_speech_save_clicked)
        self._speech_form.append(self._speech_save_btn)
        self._speech_form.set_visible(False)
        page.append(self._speech_form)

        self._speech_status_label = Gtk.Label(label="")
        self._speech_status_label.set_xalign(0.0)
        self._speech_status_label.set_wrap(True)
        page.append(self._speech_status_label)
        self._speech_has_token = False
        self._refresh_speech_section()
        return page

    def _refresh_speech_section(self) -> None:
        from utils.speech_config import load_speech_config
        cfg, error = load_speech_config()
        inert = error is not None
        self._speech_add_btn.set_sensitive(not inert)
        self._speech_endpoint_entry.set_sensitive(not inert)
        self._speech_model_entry.set_sensitive(not inert)
        self._speech_voice_entry.set_sensitive(not inert)
        self._speech_format_dropdown.set_sensitive(not inert)
        self._speech_token_entry.set_sensitive(not inert)
        self._speech_save_btn.set_sensitive(not inert)
        if inert:
            self._speech_status_label.set_text(f"⚠ Speech settings unusable: {error}")
            self._speech_status_label.remove_css_class("settings-status-ok")
            self._speech_status_label.add_css_class("settings-status-fail")
            return
        self._speech_status_label.remove_css_class("settings-status-fail")
        self._speech_has_token = bool(cfg and cfg.get("token"))
        if cfg:
            self._speech_add_btn.set_label("Edit Speech")
            self._speech_endpoint_entry.set_text(cfg.get("endpoint", ""))
            self._speech_model_entry.set_text(cfg.get("model", ""))
            self._speech_voice_entry.set_text(cfg.get("voice", ""))
            fmt = (cfg.get("format") or "mp3").strip().lower()
            self._speech_format_dropdown.set_selected(
                self._speech_formats.index(fmt) if fmt in self._speech_formats else 0
            )
            self._speech_token_entry.set_text("")
            self._speech_token_entry.set_placeholder_text("leave blank to keep the saved token")
        else:
            self._speech_add_btn.set_label("+ Add Speech")
            self._speech_token_entry.set_placeholder_text("Bearer token")

    def _on_speech_add_clicked(self, _btn) -> None:
        self._speech_form.set_visible(True)

    def _on_speech_reveal_clicked(self, btn) -> None:
        current = self._speech_token_entry.get_visibility()
        self._speech_token_entry.set_visibility(not current)
        btn.set_label("Hide" if not current else "Show")

    def _on_speech_save_clicked(self, _btn) -> None:
        from utils.speech_config import save_speech_config
        try:
            selected = self._speech_format_dropdown.get_selected()
            fmt = (
                self._speech_formats[selected]
                if 0 <= selected < len(self._speech_formats)
                else self._speech_formats[0]
            )
            save_speech_config(
                self._speech_endpoint_entry.get_text(),
                self._speech_model_entry.get_text(),
                self._speech_token_entry.get_text(),
                voice=self._speech_voice_entry.get_text(),
                fmt=fmt,
            )
        except ValueError as e:
            self._speech_status_label.set_text(f"❌ {e}")
            self._speech_status_label.remove_css_class("settings-status-ok")
            self._speech_status_label.add_css_class("settings-status-fail")
            return
        except OSError as e:
            self._speech_status_label.set_text(f"❌ write failed: {e}")
            self._speech_status_label.remove_css_class("settings-status-ok")
            self._speech_status_label.add_css_class("settings-status-fail")
            return
        self._speech_status_label.remove_css_class("settings-status-fail")
        self._speech_status_label.add_css_class("settings-status-ok")
        self._speech_status_label.set_text("✓ Saved. Applies on the next talk.")
        self._speech_token_entry.set_text("")
        self._refresh_speech_section()

    # ── Agents section (SPEC-53 §3) ────────────────────────────────────

    def _build_agents_page(self) -> Gtk.Box:
        """Build the "Agents" page: ONE JEV room-fallback combo field.

        Pure view — every read/write goes through SettingsHandler, which owns
        agent.json persistence. Selecting an option persists it immediately
        (STT-dropdown pattern); refresh-time selection blocks the notify
        handler, so constructing the dialog never writes.
        """
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_start(16)
        page.set_margin_end(16)
        page.set_margin_top(12)
        page.set_margin_bottom(12)

        intro = Gtk.Label()
        intro.set_xalign(0.0)
        intro.set_wrap(True)
        intro.add_css_class("dim-label")
        intro.set_text(
            "When the JEV decision model cannot route a group message "
            "(timeout, low confidence, empty result), this setting chooses "
            "where the message goes."
        )
        page.append(intro)

        self._agents_fallback_dropdown = Gtk.DropDown.new_from_strings(
            list(_AGENTS_FALLBACK_LABELS)
        )
        self._agents_fallback_dropdown.set_hexpand(True)
        self._agents_fallback_handler_id = self._agents_fallback_dropdown.connect(
            "notify::selected", self._on_agents_fallback_changed
        )
        page.append(
            self._labeled_row(
                "When JEV can't route a group message:",
                self._agents_fallback_dropdown,
            )
        )

        caption = Gtk.Label()
        caption.set_xalign(0.0)
        caption.set_wrap(True)
        caption.add_css_class("dim-label")
        caption.set_text(
            "Options: Route to Supervisor (default) — send to the project's "
            "Supervisor member when present; Broadcast to all — send to every "
            "member; Don't route — show the note in the room, wake nobody."
        )
        page.append(caption)

        self._agents_status_label = Gtk.Label(label="")
        self._agents_status_label.set_xalign(0.0)
        self._agents_status_label.set_wrap(True)
        page.append(self._agents_status_label)

        self._refresh_agents_section()
        return page

    def _refresh_agents_section(self) -> None:
        """Sync the dropdown with the stored agent.json value.

        A missing or out-of-vocabulary stored value selects the default
        (index 0, "Route to Supervisor"). The notify handler is blocked
        during set_selected so refresh never writes.
        """
        if getattr(self, "_agents_fallback_dropdown", None) is None:
            return
        value = self._handler.get_room_fallback()
        index = (
            _ROOM_FALLBACK_VALUES.index(value)
            if value in _ROOM_FALLBACK_VALUES
            else 0
        )
        self._agents_fallback_dropdown.handler_block(
            self._agents_fallback_handler_id
        )
        try:
            self._agents_fallback_dropdown.set_selected(index)
        finally:
            self._agents_fallback_dropdown.handler_unblock(
                self._agents_fallback_handler_id
            )

    def _on_agents_fallback_changed(self, dropdown, _pspec) -> None:
        """Persist the chosen fallback mode immediately (STT-dropdown pattern)."""
        del dropdown  # signal payload; the index is read from the dropdown
        selected = int(self._agents_fallback_dropdown.get_selected())
        if not (0 <= selected < len(_ROOM_FALLBACK_VALUES)):
            return
        value = _ROOM_FALLBACK_VALUES[selected]
        try:
            self._handler.set_room_fallback(value)
        except Exception as e:  # noqa: BLE001 — surface save failures to the user
            self._agents_status_label.set_text(f"❌ save failed: {e}")
            self._agents_status_label.remove_css_class("settings-status-ok")
            self._agents_status_label.add_css_class("settings-status-fail")
            return
        self._agents_status_label.remove_css_class("settings-status-fail")
        self._agents_status_label.add_css_class("settings-status-ok")
        self._agents_status_label.set_text(
            f"✓ Saved: {_AGENTS_FALLBACK_LABELS[selected]}. "
            "Applies on the next unrouted group message."
        )

    def _refresh_stt_section(self) -> None:
        """Sync dropdown/entry with stt.yaml; render inert on degraded store.

        ``load_stt_model_state()`` returns (model, error). error is not None
        means the stored file exists but is unusable — controls go
        insensitive with the reason in the status label (honest UI).
        """
        if getattr(self, "_stt_dropdown", None) is None:
            return
        model, error = _stt_config.load_stt_model_state()
        inert = error is not None
        self._stt_dropdown.set_sensitive(not inert)
        self._stt_custom_entry.set_sensitive(not inert)
        self._stt_apply_btn.set_sensitive(not inert)
        if inert:
            self._stt_status_label.set_text(f"⚠ STT model store unusable: {error}")
            self._stt_status_label.remove_css_class("settings-status-ok")
            self._stt_status_label.add_css_class("settings-status-fail")
            return
        self._stt_status_label.remove_css_class("settings-status-fail")

        index = (
            self._stt_model_ids.index(model)
            if model in self._stt_model_ids
            else len(self._stt_model_ids)  # unknown/custom id → "Custom…"
        )
        self._stt_dropdown.handler_block(self._stt_dropdown_handler_id)
        try:
            self._stt_dropdown.set_selected(index)
        finally:
            self._stt_dropdown.handler_unblock(self._stt_dropdown_handler_id)
        # ROUND 2 (SPEC-37 audit BUG #2): BOTH custom-row widgets share ONE
        # visibility flag. A visible Apply wired to a hidden empty entry would
        # surface a raw ValueError on click. This holds on fresh open
        # (build → _refresh_stt_section) and after the handler_block-wrapped
        # set_selected above.
        show_custom = index == len(self._stt_model_ids)
        if show_custom:
            self._stt_custom_entry.set_text(model)
        self._stt_custom_entry.set_visible(show_custom)
        self._stt_apply_btn.set_visible(show_custom)

    def _on_stt_dropdown_changed(self, dropdown, _pspec) -> None:
        """Custom… reveals the free-text row; a KNOWN model persists now.

        ROUND 2 (SPEC-37 audit BUG #1): the dropdown is the primary control —
        picking a known model must persist it immediately, not merely toggle
        the Custom row. Refresh-time selections pass through here only while
        the handler is unblocked (see _refresh_stt_section).
        """
        del dropdown  # signal payload; index is read from self._stt_dropdown
        selected = self._stt_dropdown.get_selected()
        is_custom = selected == len(self._stt_model_ids)
        self._stt_custom_entry.set_visible(is_custom)
        self._stt_apply_btn.set_visible(is_custom)
        if is_custom:
            return
        # Known model index → persist (same write path as the Apply button).
        try:
            _stt_config.set_stt_model(self._stt_model_ids[selected])
        except ValueError as e:
            self._stt_status_label.set_text(f"❌ {e}")
            self._stt_status_label.remove_css_class("settings-status-ok")
            self._stt_status_label.add_css_class("settings-status-fail")
            return
        except OSError as e:
            self._stt_status_label.set_text(f"❌ write failed: {e}")
            self._stt_status_label.remove_css_class("settings-status-ok")
            self._stt_status_label.add_css_class("settings-status-fail")
            return
        self._stt_status_label.remove_css_class("settings-status-fail")
        self._stt_status_label.add_css_class("settings-status-ok")
        self._stt_status_label.set_text(
            f"✓ Saved: {self._stt_model_ids[selected]}. Applies on the next dictation."
        )

    def _on_stt_apply_clicked(self, _btn) -> None:
        """Persist the Custom… entry through stt_config; surface the result."""
        raw = self._stt_custom_entry.get_text()
        try:
            _stt_config.set_stt_model(raw)
        except ValueError as e:
            self._stt_status_label.set_text(f"❌ {e}")
            self._stt_status_label.remove_css_class("settings-status-ok")
            self._stt_status_label.add_css_class("settings-status-fail")
            return
        except OSError as e:
            self._stt_status_label.set_text(f"❌ write failed: {e}")
            self._stt_status_label.remove_css_class("settings-status-ok")
            self._stt_status_label.add_css_class("settings-status-fail")
            return
        self._stt_status_label.remove_css_class("settings-status-fail")
        self._stt_status_label.add_css_class("settings-status-ok")
        self._stt_status_label.set_text("✓ Saved. Applies on the next dictation.")
        self._refresh_stt_section()

    def _labeled_row(self, text: str, widget: Gtk.Widget) -> Gtk.Box:
        """Label above the control (mirrors _ProviderCard._labeled)."""
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        label = Gtk.Label(label=text)
        label.set_halign(Gtk.Align.START)
        label.add_css_class("dim-label")
        widget.set_hexpand(True)
        row.append(label)
        row.append(widget)
        return row

    def _refresh_telegram_section(self) -> None:
        """Reload token + paired status from the controller and gate buttons."""
        if self._telegram is None:
            self._telegram_paired_label.set_text("Telegram bridge not wired.")
            self._telegram_token_entry.set_sensitive(False)
            self._telegram_test_btn.set_sensitive(False)
            self._telegram_save_btn.set_sensitive(False)
            self._telegram_pair_btn.set_sensitive(False)
            return
        data = self._telegram.load()
        # Pre-fill the masked token field; the user must click reveal to view it.
        self._telegram_token_entry.set_text(data.get("bot_token") or "")
        self._telegram_paired_label.set_text(self._telegram.paired_label(data))
        # Start Pairing is enabled only when a token is saved.
        self._telegram_pair_btn.set_sensitive(self._telegram.has_saved_token(data))

    def _telegram_set_status(self, text: str, *, ok: bool = False,
                             fail: bool = False) -> None:
        self._telegram_status_label.set_text(text)
        self._telegram_status_label.remove_css_class("settings-status-ok")
        self._telegram_status_label.remove_css_class("settings-status-fail")
        self._telegram_status_label.remove_css_class("settings-status-untested")
        if ok:
            self._telegram_status_label.add_css_class("settings-status-ok")
        elif fail:
            self._telegram_status_label.add_css_class("settings-status-fail")
        else:
            self._telegram_status_label.add_css_class("settings-status-untested")

    def _on_telegram_reveal_clicked(self, *args) -> None:
        """Toggle bot-token visibility (clone of _ProviderCard reveal)."""
        current = self._telegram_token_entry.get_visibility()
        self._telegram_token_entry.set_visibility(not current)

    def _on_telegram_save_clicked(self, *args) -> None:
        """Persist the token (preserving any existing pairing)."""
        if self._telegram is None:
            return
        token = self._telegram_token_entry.get_text().strip()
        try:
            self._telegram.save_token(token)
        except Exception as e:  # noqa: BLE001 — surface save failures to the user
            self._telegram_set_status(f"Save failed: {e}", fail=True)
            return
        self._telegram_set_status("Saved.", ok=True)
        self._refresh_telegram_section()

    def _on_telegram_test_clicked(self, *args) -> None:
        """Validate the entered token via getMe (off-thread)."""
        if self._telegram is None:
            return
        self._telegram_set_status("Testing…")
        token = self._telegram_token_entry.get_text().strip()
        self._telegram.test_token(token, self._on_telegram_test_result)

    def _on_telegram_test_result(self, ok: bool, message: str) -> None:
        """Main-thread callback: show the (redacted) Test result."""
        self._telegram_set_status(message, ok=ok, fail=not ok)

    def _on_telegram_pair_clicked(self, *args) -> None:
        """Enter pairing mode: poll for the first inbound message."""
        if self._telegram is None:
            return
        self._telegram_set_status("Pairing… send any message to the bot from your phone.")
        self._telegram_pair_btn.set_sensitive(False)
        self._telegram.start_pairing(
            self._on_telegram_pair_candidate, self._on_telegram_pair_error
        )

    def _on_telegram_pair_error(self, message: str) -> None:
        self._telegram_pair_btn.set_sensitive(True)
        self._telegram_set_status(message, fail=True)

    def _on_telegram_pair_candidate(self, chat_id: int, handle: str) -> None:
        """Main-thread callback: offer the first message's chat as the pairing."""
        if self._telegram is None:
            return
        shown = f"{handle} ({chat_id})" if handle else f"chat {chat_id}"
        dialog = Gtk.MessageDialog(
            transient_for=self._parent,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Pair with {shown}?",
        )
        # BUG#4 (SP2 audit): pairing accepts the first message in the window —
        # whoever messages first wins. Make the identity + exclusivity warning
        # explicit so the human is a real gate, not a rubber stamp.
        dialog.set_property(
            "secondary-text",
            f"Telegram will pair with {shown}. This will be the ONLY chat "
            "allowed to reach the Supervisor. Confirm only if this is you.",
        )

        def on_response(dlg, response_id):
            dlg.close()
            if response_id == Gtk.ResponseType.YES:
                if self._telegram.confirm_pairing(chat_id, handle):
                    self._telegram_set_status(f"Paired with {shown}.", ok=True)
                else:
                    self._telegram_set_status(
                        "Could not pair — save a bot token first.", fail=True
                    )
            else:
                self._telegram_set_status("Pairing cancelled.")
            self._refresh_telegram_section()
            self._telegram_pair_btn.set_sensitive(
                self._telegram.has_saved_token()
            )

        dialog.connect("response", on_response)
        dialog.show()
