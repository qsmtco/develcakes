# ui/handlers/telegram_bridge_handler.py — Telegram remote bridge (SPEC-15 SP2).
#
# The phone is a thin client for the Supervisor. This handler owns the bridge
# STATE MACHINE and the inbound (phone → app) routing. It is the ONLY writer
# of bridge state (single-writer rule).
#
# Thread discipline: transport signals (on_connect/on_disconnect/on_error) and
# on_update fire ON THE TRANSPORT THREAD. This handler marshals every
# state/routing action onto the GTK main thread via the injected `dispatch`
# seam (production passes GLib.idle_add; tests pass an inline runner).
#
# Scope (SP2): inbound direction ONLY (paired-chat text → Supervisor). The
# app→phone reply direction is SP3 — `forward_to_phone` is a documented stub.
# Callback-query approvals are logged + answered "not yet wired" (real ARH
# exec wiring is SP3). Foreign chat ⇒ one polite refusal, never processed.
#
# Trust boundary (binding): one paired chat_id; the phone gets conversation +
# (later) approvals — NO shell, NO file access.

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from transport.telegram import TelegramAuthError, TelegramTransport, redact_log_preview

_logger = logging.getLogger(__name__)

SUPERVISOR_KEY = "special:supervisor"


class BridgeState:
    """Bridge lifecycle states (string constants — the toolbar maps them)."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class TelegramBridgeHandler:
    """Owns the Telegram bridge session: state machine + inbound routing.

    Args:
        arh: the AgentRuntimeHandler (or None). Used for
            send_to_special_agent("special:supervisor", text).
        transport_factory: (**kwargs) -> TelegramTransport-like. Injected so
            tests use a fake. Production default constructs the real transport.
        load_config: () -> {bot_token, chat_id, paired_handle}. Injected
            (defaults to utils.telegram_store.load_bridge_config).
        dispatch: (fn, *args) -> None. Runs fn on the main thread (production:
            GLib.idle_add; tests: inline). Every state mutation + outbound send
            goes through this so transport-thread callbacks never touch GTK.
        on_state_change: optional (state: str) -> None, fired on each change.
        on_feed_card: optional (title: str, body: str) -> None, fired when the
            bridge needs an honest, SPECIFIC feed card (currently only the
            BUG#1 supervisor-not-registered refusal). Distinct from the generic
            "offline" card so the reason is never misattributed to the token.
    """

    def __init__(
        self,
        arh: Any = None,
        transport_factory: Callable[..., Any] | None = None,
        load_config: Callable[[], dict] | None = None,
        dispatch: Callable[..., None] | None = None,
        on_state_change: Callable[[str], None] | None = None,
        on_feed_card: Callable[[str, str], None] | None = None,
    ) -> None:
        self._arh = arh
        self._transport_factory = transport_factory or self._default_transport_factory
        self._load_config = load_config or self._default_load_config
        self._dispatch = dispatch or (lambda fn, *a: fn(*a))
        self._on_state_change = on_state_change
        self._on_feed_card = on_feed_card
        self._transport: Any | None = None
        self._state = BridgeState.DISCONNECTED
        self._chat_id: int | None = None

    # ── state ────────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    def is_connected(self) -> bool:
        return self._state == BridgeState.CONNECTED

    def set_agent_runtime_handler(self, arh: Any) -> None:
        """Late-bind the AgentRuntimeHandler (SPEC-15 SP2 part D).

        Setter injection (house pattern — see ARH.set_review_handler /
        set_feed_handler): window.py builds the bridge handler in its
        composition root and injects the ARH reference AFTER ARH exists,
        so this module never imports ui.handlers.agent_runtime_handler
        (handler-isolation guard). Safe to call before or after start.
        """
        self._arh = arh

    def _set_state(self, state: str) -> None:
        self._state = state
        if self._on_state_change is not None:
            try:
                self._on_state_change(state)
            except Exception as e:  # noqa: BLE001 — a UI callback must not break the bridge
                _logger.error("on_state_change raised: %s", redact_log_preview(str(e)))

    def _emit_feed_card(self, title: str, body: str) -> None:
        """Fire a SPECIFIC bridge feed card (BUG#1 supervisor refusal).

        Distinct from the generic "bridge offline" card driven by the ERROR
        state, so the reason the bridge refused is never misattributed to a
        bad token. A UI callback must never break the bridge.
        """
        if self._on_feed_card is None:
            return
        try:
            self._on_feed_card(title, body)
        except Exception as e:  # noqa: BLE001 — a UI callback must not break the bridge
            _logger.error("on_feed_card raised: %s", redact_log_preview(str(e)))

    def _supervisor_registered(self) -> bool:
        """True when the injected ARH has the Supervisor agent registered.

        Reads the ARH's real API: `get_special_agents()` → {session_key:
        display_name} (falls back to the `_agents` dict). When the ARH exposes
        neither, we cannot verify — fail-open (return True) so a future ARH
        shape is not silently blocked by this guard.
        """
        arh = self._arh
        if arh is None:
            return False
        getter = getattr(arh, "get_special_agents", None)
        if callable(getter):
            try:
                return SUPERVISOR_KEY in getter()
            except Exception as e:  # noqa: BLE001 — unknown ARH surface
                _logger.warning(
                    "start_bridge: could not read ARH special agents: %s",
                    redact_log_preview(str(e)),
                )
                return True
        agents = getattr(arh, "_agents", None)
        if isinstance(agents, dict):
            return SUPERVISOR_KEY in agents
        return True  # unverifiable ARH shape — do not block

    # ── lifecycle ────────────────────────────────────────────────────────

    def start_bridge(self) -> None:
        """Validate the store, construct the transport, and connect.

        Fail-closed: no token or no paired chat_id → ERROR state, no transport.
        The connect() call runs on the transport's own thread; state advances
        to CONNECTING now and to CONNECTED on the on_connect signal.
        """
        cfg = self._load_config()
        token = cfg.get("bot_token") or ""
        chat_id = cfg.get("chat_id")
        if not token or chat_id is None:
            _logger.warning(
                "start_bridge: not configured (token=%s, chat_id=%s) — error state",
                "set" if token else "missing",
                "set" if chat_id is not None else "missing",
            )
            self._set_state(BridgeState.ERROR)
            return

        # BUG#1 (SPEC-15 SP2 audit, CRITICAL): the bridge routes phone text to
        # the Supervisor, but supervisor.yaml ships auto_open:false /
        # auto_add_to_projects:false — a DELIBERATE manual-add design (user
        # clicks "+" in the Agents tab). On most installs the Supervisor is
        # therefore NOT registered, so every phone message would be silently
        # dropped by ARH.send_to_special_agent ("not a registered special
        # agent") with only a generic error on the phone. Refuse up front,
        # BEFORE starting the transport, so no poll loop is brought up for a
        # bridge that cannot route. We do NOT auto-register — that would
        # violate the deliberate manual-add decision recorded in
        # supervisor.yaml.
        if self._arh is not None and not self._supervisor_registered():
            _logger.warning(
                "start_bridge: Supervisor agent (%s) is not registered — "
                "refusing to start the bridge (add it from the Agents tab)",
                SUPERVISOR_KEY,
            )
            self._emit_feed_card(
                "Telegram bridge: Supervisor agent not registered",
                "The Telegram bridge routes messages to the Supervisor, but no "
                "Supervisor agent is registered. Add it from the Agents tab, "
                "then Connect again.",
            )
            self._set_state(BridgeState.ERROR)
            return

        self._chat_id = int(chat_id)
        self._transport = self._transport_factory(
            token=token,
            chat_id=self._chat_id,
            on_connect=lambda: self._dispatch(self._on_transport_connect),
            on_disconnect=lambda r: self._dispatch(self._on_transport_disconnect, r),
            on_error=lambda m: self._dispatch(self._on_transport_error, m),
            on_update=lambda u: self._dispatch(self._handle_update, u),
        )
        self._set_state(BridgeState.CONNECTING)
        try:
            import asyncio

            asyncio.run(self._transport.connect())
        except TelegramAuthError as e:
            _logger.warning("bridge connect failed (auth): %s", redact_log_preview(str(e)))
            self._set_state(BridgeState.ERROR)
        except Exception as e:  # noqa: BLE001 — any connect failure is an error state
            _logger.warning("bridge connect failed: %s", redact_log_preview(str(e)))
            self._set_state(BridgeState.ERROR)

    def stop_bridge(self) -> None:
        """Stop the transport (idempotent) and go DISCONNECTED."""
        if self._state == BridgeState.DISCONNECTED and self._transport is None:
            return  # already stopped — no-op
        transport = self._transport
        if transport is not None:
            try:
                import asyncio

                asyncio.run(transport.disconnect())
            except Exception as e:  # noqa: BLE001 — teardown is best-effort
                _logger.warning("bridge disconnect failed: %s", redact_log_preview(str(e)))
        self._transport = None
        self._set_state(BridgeState.DISCONNECTED)

    # ── transport-thread callbacks (already dispatched to main) ──────────

    def _on_transport_connect(self) -> None:
        self._set_state(BridgeState.CONNECTED)

    def _on_transport_disconnect(self, reason: str) -> None:
        self._set_state(BridgeState.DISCONNECTED)

    def _on_transport_error(self, message: str) -> None:
        _logger.warning("bridge error: %s", redact_log_preview(str(message)))
        self._set_state(BridgeState.ERROR)

    # ── inbound routing (phone → app) ────────────────────────────────────

    def _handle_update(self, update: dict) -> None:
        """Route one Telegram update. Runs on the main thread (dispatched)."""
        if not isinstance(update, dict):
            return
        msg = update.get("message")
        cbq = update.get("callback_query")
        if msg is not None:
            self._handle_message(msg)
        elif cbq is not None:
            self._handle_callback_query(cbq)

    def _chat_id_of(self, container: dict) -> int | None:
        chat = container.get("message") if "message" in container else container.get("chat")
        if not isinstance(chat, dict):
            return None
        # callback_query carries its chat under ["message"]["chat"].
        if "message" in container and isinstance(container["message"], dict):
            chat = container["message"].get("chat")
        elif isinstance(container.get("chat"), dict):
            chat = container["chat"]
        if not isinstance(chat, dict):
            return None
        cid = chat.get("id")
        try:
            return int(cid) if cid is not None else None
        except (TypeError, ValueError):
            return None

    def _handle_message(self, msg: dict) -> None:
        chat_id = self._chat_id_of(msg)
        if chat_id != self._chat_id:
            self._send_refusal(chat_id)
            return
        text = msg.get("text")
        if not text:
            return  # non-text (photo/sticker/etc.) — nothing to route in SP2
        if self._arh is None:
            self._send("Bridge unavailable: no agent runtime wired.")
            return
        try:
            self._arh.send_to_special_agent(SUPERVISOR_KEY, text)
        except Exception as e:  # noqa: BLE001 — never let routing kill the bridge
            _logger.error("route to supervisor failed: %s", redact_log_preview(str(e)))
            self._send("Bridge error: could not reach the Supervisor.")

    def _handle_callback_query(self, cbq: dict) -> None:
        chat_id = self._chat_id_of(cbq)
        if chat_id != self._chat_id:
            return  # foreign chat → never processed, no answer
        cbq_id = cbq.get("id")
        data = cbq.get("data", "")
        # SP3 will dispatch approvals to ARH.approve_exec. SP2: log + answer.
        _logger.info("callback_query received (not yet wired): data=%s",
                     redact_log_preview(str(data)))
        if self._transport is not None and cbq_id is not None:
            try:
                self._transport.answer_callback_query(
                    cbq_id, "Approvals are not yet wired (SP3).")
            except Exception as e:  # noqa: BLE001
                _logger.warning("answer_callback_query failed: %s", redact_log_preview(str(e)))

    def _send_refusal(self, chat_id: int | None) -> None:
        """One polite refusal to a foreign chat; never processed."""
        _logger.info("foreign chat %s refused (redacted; not paired)", chat_id)
        self._send("This bot is paired to another chat. Sorry.")

    def _send(self, text: str) -> None:
        if self._transport is None:
            return
        try:
            self._transport.send_message(text)
        except Exception as e:  # noqa: BLE001
            _logger.warning("bridge send failed: %s", redact_log_preview(str(e)))

    # ── app → phone (SP3 seam) ───────────────────────────────────────────

    def forward_to_phone(self, text: str) -> None:
        """REGISTER (SPEC-15 SP3): app→phone reply direction — NOT WIRED.

        When the Supervisor completes a turn, the bridge must forward the reply
        text to the paired chat. SP3 wires this from the ARH completion path
        (with the 4096-char paragraph split, §5 edge row). For SP2 it is an
        intentionally inert seam: the transport's send_message is ready, the
        radio (paired chat_id + live transport) exists — only the trigger is
        missing.
        """
        if self._transport is None or self._chat_id is None:
            return
        # SP3: self._transport.send_message(text) once the trigger is wired.
        _logger.debug("forward_to_phone: SP3 seam (reply length=%d), not yet wired", len(text))

    # ── defaults ─────────────────────────────────────────────────────────

    @staticmethod
    def _default_load_config() -> dict:
        from utils.telegram_store import load_bridge_config
        return load_bridge_config()

    @staticmethod
    def _default_transport_factory(**kwargs) -> TelegramTransport:
        return TelegramTransport(**kwargs)