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
from typing import TYPE_CHECKING

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

from models.providers import ProviderConfig
if TYPE_CHECKING:
    from ui.handlers.settings_handler import SettingsHandler
    from utils.provider_test import TestResult

logger = logging.getLogger(__name__)


class _ProviderCard:
    """A single provider's edit form. Pure view — delegates to handler."""

    def __init__(self, dialog: SettingsDialog, provider: ProviderConfig | None):
        """If provider is None, this is a new (unsaved) card with empty fields."""
        self._dialog = dialog
        self._is_new = provider is None
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

        # Context window (max_tokens) — editable; pre-filled by Test Connection.
        # Default 128_000 matches the dataclass default and runtime fallback.
        self._max_tokens_spin = Gtk.SpinButton.new_with_range(1_000, 10_000_000, 1_000)
        self._max_tokens_spin.set_value(self._provider.max_tokens or 128_000)
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

    def _labeled(self, text: str, widget: Gtk.Widget) -> Gtk.Box:
        """Create a label + widget row."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label = Gtk.Label(label=text)
        label.set_size_request(100, -1)
        label.set_halign(Gtk.Align.START)
        label.set_valign(Gtk.Align.CENTER)
        row.append(label)
        row.append(widget)
        return row

    def _populate_from_provider(self) -> None:
        p = self._provider
        self._name_entry.set_text(p.name or "")
        self._base_url_entry.set_text(p.base_url or "")
        self._model_entry.set_text(p.default_model or "")
        self._api_key_entry.set_text(p.api_key or "")
        self._caller_label.set_text(
            f"  {p.caller}" if p.caller else "  (auto-detected on save)"
        )
        self._max_tokens_spin.set_value(p.max_tokens or 128_000)
        self._compaction_threshold_spin.set_value(p.compaction_threshold or 0.80)

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
            or int(self._max_tokens_spin.get_value()) != (p.max_tokens or 128_000)
            or float(self._compaction_threshold_spin.get_value()) != (p.compaction_threshold or 0.80)
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
            compaction_threshold=float(
                self._compaction_threshold_spin.get_value()
            ),  # Phase A
            last_verified_at=existing.last_verified_at if existing else None,
            last_error=existing.last_error if existing else None,
        )

    def _on_reveal_clicked(self, *args) -> None:
        """Toggle API key visibility."""
        current = self._api_key_entry.get_visibility()
        self._api_key_entry.set_visibility(not current)

    def _on_save_clicked(self, *args) -> None:
        """Save the card's current form values via handler."""
        provider = self._collect_from_form()
        try:
            self._dialog._handler.add_or_update(provider)
        except ValueError as e:
            self._set_status(str(e), fail=True)

    def _on_test_clicked(self, *args) -> None:
        """Run Test Connection via handler. Does not block."""
        provider = self._collect_from_form()
        self._status_label.set_text("Testing...")
        self._status_label.remove_css_class("settings-status-ok")
        self._status_label.remove_css_class("settings-status-fail")
        self._status_label.add_css_class("settings-status-untested")
        self._dialog._handler.test_provider(provider, self._on_test_result)

    def _on_remove_clicked(self, *args) -> None:
        """Remove this provider. Shows confirmation first."""
        if self._is_new:
            # Unsaved card — just remove from the dialog
            self._dialog._remove_card(self)
            return

        name = self._provider.name
        dialog = Gtk.MessageDialog(
            transient_for=self._dialog._window,
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
            user_has_customized = (
                self._provider.max_tokens != 128_000
                or (self._provider.default_max_tokens or 0) > 0
            )
            if result.context_window and not user_has_customized:
                self._max_tokens_spin.set_value(result.context_window)
                new_max_tokens = result.context_window
                self._status_label.set_text(
                    f"✅ {result.latency_ms}ms · context: {result.context_window:,}"
                )
            # BUG #6 + #9 fix: update self._provider to reflect what the
            # handler has now written to disk. Without this, _is_dirty() stays
            # True forever (spin != p.max_tokens), and the next refresh shows
            # a stale "Untested" label. Use a fresh ISO timestamp since the
            # test succeeded just now — the handler's exact timestamp will be
            # reconciled on the next refresh_providers() call.
            from datetime import datetime, timezone
            self._provider = ProviderConfig(
                name=self._provider.name,
                base_url=self._provider.base_url,
                api_key=self._provider.api_key,
                default_model=self._provider.default_model,
                caller=self._provider.caller,
                enabled=self._provider.enabled,
                supports_tools=self._provider.supports_tools,
                supports_streaming=self._provider.supports_streaming,
                max_tokens=new_max_tokens,
                default_max_tokens=self._provider.default_max_tokens,
                last_verified_at=datetime.now(timezone.utc).isoformat(),
                last_error=None,
            )
        else:
            error_msg = result.error or "unknown error"
            self._set_status(f"❌ {error_msg}", fail=True)
            # Stamp the error on the in-memory provider too so refresh doesn't
            # revert to "Untested".
            self._provider = ProviderConfig(
                name=self._provider.name,
                base_url=self._provider.base_url,
                api_key=self._provider.api_key,
                default_model=self._provider.default_model,
                caller=self._provider.caller,
                enabled=self._provider.enabled,
                supports_tools=self._provider.supports_tools,
                supports_streaming=self._provider.supports_streaming,
                max_tokens=self._provider.max_tokens,
                default_max_tokens=self._provider.default_max_tokens,
                last_verified_at=self._provider.last_verified_at,
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
    """GTK4 dialog for managing LLM provider settings.

    Pure view — delegates all persistence to SettingsHandler.
    Called from the toolbar ⚙ button (wired in Phase 7).

    Args:
        parent: Parent Gtk.Window for transient setting.
        handler: SettingsHandler — the data gateway.
        on_close: Optional callback when the dialog is closed.
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
        self._telegram = telegram_controller
        self._cards: list[_ProviderCard] = []

        # ── Window setup ──────────────────────────────────────────────
        self._window = Gtk.Window(title="Settings")
        if parent is not None:
            self._window.set_transient_for(parent)
        self._window.set_modal(True)
        self._window.set_default_size(560, 480)
        self._window.add_css_class("settings-dialog")
        self._window.connect("close-request", self._on_close_request)

        # ── Build layout ──────────────────────────────────────────────
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Sectioned layout (SPEC-15 SP2): a Gtk.Stack switched by a
        # Gtk.StackSwitcher placed in the header bar. Tab "providers" holds
        # the byte-identical legacy provider content; tab "telegram" is the
        # new Telegram Bridge section.
        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack_switcher = Gtk.StackSwitcher()
        self._stack_switcher.set_stack(self._stack)

        # Header bar
        header = Gtk.HeaderBar()
        header.set_title_widget(self._stack_switcher)
        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda *_: self.close())
        header.pack_end(close_btn)
        content.append(header)

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

        content.append(self._stack)
        self._window.set_child(content)

        # Populate from current handler state
        self.refresh_providers(handler.list_providers())

    # ── Public API ────────────────────────────────────────────────────

    def show(self) -> None:
        """Present the settings dialog."""
        self._window.present()

    def close(self) -> None:
        """Close the settings dialog."""
        self._window.close()

    def refresh_providers(self, providers: list[ProviderConfig]) -> None:
        """Incrementally update the card list from the given provider list.

        Strategy: match cards by provider name. Existing cards are updated
        in place (dirty cards are left alone to preserve unsaved edits),
        new providers get new cards, removed providers have their cards
        deleted.

        Clean cards (no unsaved edits) are updated with fresh data from
        the provider list. Dirty cards keep their entry values but get
        their provider reference updated so status labels reflect test results.
        """
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

    def _on_close_request(self, *args) -> bool:
        """Handle window close-request signal.

        SPEC-15 SP2: leaving the dialog tears down any in-flight pairing poll
        (a throwaway transport must never outlive the settings window).
        """
        if self._telegram is not None:
            try:
                self._telegram.cancel_pairing()
            except Exception as e:  # noqa: BLE001 — teardown must not block close
                logger.warning("telegram pairing teardown on close failed: %s", e)
        if self._on_close is not None:
            self._on_close()
        return False  # allow close

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

    def _labeled_row(self, text: str, widget: Gtk.Widget) -> Gtk.Box:
        """Label + widget row (mirrors _ProviderCard._labeled)."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label = Gtk.Label(label=text)
        label.set_size_request(100, -1)
        label.set_halign(Gtk.Align.START)
        label.set_valign(Gtk.Align.CENTER)
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
            transient_for=self._window,
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
