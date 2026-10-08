# ui/handlers/telegram_bridge_handler.py — Telegram remote bridge (SPEC-15 SP2/SP3/SP4).
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
# Scope: inbound phone → Supervisor routing (SP2) + exec approvals via inline
# callback buttons (SP3a) + app→phone reply mirror (SP3b) + remote command
# allowlist / typed stop-all confirmation (SP4).
# Foreign chat ⇒ one polite refusal, never processed.
#
# Trust boundary (binding): one paired chat_id; the phone gets conversation +
# approvals + the /status // /stop commands — NO shell, NO file access.

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any

from transport.telegram import TelegramAuthError, TelegramTransport, redact_log_preview

_logger = logging.getLogger(__name__)

SUPERVISOR_KEY = "special:supervisor"

# SP4: the exact word a phone user types to confirm a destructive remote
# stop-all (spec §SP4 "confirmation asymmetry" — the desk dialog is enough,
# a phone tap is not).
STOP_CONFIRM_WORD = "STOP"

# SP4: refusal/help listing the remote allowlist (spec §5).
_ALLOWLIST_HELP = (
    "Remote commands: /status (project summary), /stop (halt all agents, "
    "needs typed confirmation), /help. Any other message is forwarded to the "
    "Supervisor.")

# SP3b: a WHOLE-message ```html fence (SPEC-13 protocol) → the payload group.
# Mirrors render/html.py's anchored, non-greedy contract; a stray inner fence
# or any prose makes this None (the text is then sent unchanged).
_WHOLE_MESSAGE_HTML_FENCE_RE = re.compile(
    r"^```html[ \t]*\r?\n(.*?)\r?\n?```[ \t]*$",
    re.DOTALL | re.IGNORECASE,
)

# R2/R3 (audit): the HTML→text flatten is LINEAR (HTMLParser), not a regex —
# the old attribute-aware `<[^>]+>`-style regex was ~22–32x slower and O(N²)
# on '<'-dense input, and a strip-after-unescape ate real decoded text
# (`5 &lt; 6 &gt; 4` → `5  4`). The parser collects TEXT nodes (entities are
# decoded there, so a decoded `<` in text is never re-read as a tag), and
# block boundaries become newlines. A literal script/style element is dropped.
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr",
    "section", "article", "header", "footer", "nav", "figure",
})
_SKIP_TAGS = frozenset({"script", "style"})


class _TextExtractor(HTMLParser):
    """Collect visible TEXT from an HTML fragment (R2/R3, linear).

    ``convert_charrefs=True`` decodes entities in text nodes, so
    ``&lt;script&gt;`` in TEXT stays a literal ``<script>`` and is NEVER
    re-interpreted as markup. A literal ``<script>…</script>`` element's body
    is dropped.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _strip_literal_tag_bodies(text: str) -> str:
    """Drop the bodies of literal ``<script>``/``<style>`` elements (linear).

    Runs on the DECODED text, so an entity-encoded ``&lt;script&gt;`` in TEXT
    (now a literal ``<script>``) is removed as markup-ish — matching the
    prior contract that an encoded script tag does not survive.
    """
    for tag in _SKIP_TAGS:
        open_tok, close_tok = f"<{tag}", f"</{tag}>"
        out: list[str] = []
        i = 0
        lower = text.lower()
        while True:
            j = lower.find(open_tok, i)
            if j == -1:
                out.append(text[i:])
                break
            k = lower.find(close_tok, j)
            if k == -1:
                out.append(text[i:])
                break
            out.append(text[i:j])
            i = k + len(close_tok)
        text = "".join(out)
    return text


def _html_fragment_to_text(payload: str) -> str:
    """HTML fragment → plain text (R2/R3). Linear; fail-safe at the caller."""
    parser = _TextExtractor()
    parser.feed(payload)
    parser.close()
    flat = _strip_literal_tag_bodies(parser.text())
    # Collapse >2 blank lines the block-boundary newlines may leave behind.
    return re.sub(r"\n{3,}", "\n\n", flat).strip()

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
        # R1 (audit): session GENERATION. A transport callback captured the
        # generation live when it was built; every start/stop bumps it, so a
        # SUPERSEDED transport's queued (deferred-dispatch) callback early-
        # returns instead of clobbering the current session's state.
        self._generation: int = 0
        # SPEC-15 SP3a: card_id → telegram message_id for approval cards with
        # inline buttons. FIFO cap 50 (review-queue discipline).
        self._approval_msgs: dict[str, int] = {}
        self._approval_order: list[str] = []
        self._feed_handler: Any | None = None
        # SPEC-15 SP4: remote stop-all. The bridge does NOT import window/ARH —
        # the window injects a callback that runs stop_all_agents()+stop_bridge()
        # without the GTK confirm dialog (the phone confirmed by typing the word).
        self._stop_all_handler: Callable[[], None] | None = None
        # SPEC-15 SP4: /status reads a compact summary from an injected provider
        # (window builds it from the same project/work data cmd_status reads).
        self._status_provider: Callable[[], str] | None = None
        # Two-step confirmation: True after a bare /stop until the exact word
        # arrives or any other reply aborts. Cleared on disconnect hygiene.
        self._pending_stop: bool = False

    def set_feed_handler(self, feed_handler: Any) -> None:
        """Late-bind the FeedHandler (SP3a — card re-read after approve).

        House setter-injection pattern (see set_agent_runtime_handler): this
        module never imports the feed handler module (handler isolation).
        Used to read an approval card's RESOLVED state after approve_exec.
        """
        self._feed_handler = feed_handler

    def set_stop_all_handler(self, cb: Callable[[], None] | None) -> None:
        """Late-bind the remote stop-all action (SPEC-15 SP4).

        Setter injection (handler isolation — the bridge never imports
        window/ARH). ``cb`` runs the SAME stop-all path the toolbar uses
        (stop_all_agents() + stop_bridge()) WITHOUT the GTK confirm dialog:
        the phone already confirmed by typing the exact word.
        """
        self._stop_all_handler = cb

    def set_status_provider(self, cb: Callable[[], str] | None) -> None:
        """Late-bind the /status text provider (SPEC-15 SP4).

        Setter injection: window builds a compact phone summary from the same
        project/work-unit data the in-app /status reads (single status model).
        Returns a short multi-line string; the bridge sends it verbatim.
        """
        self._status_provider = cb

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
        # R4 (audit): dedup — a flapping link must not re-fire on_state_change
        # (window emits an "offline" card per error). No-op re-sets are silent.
        if state == self._state:
            return
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

        F1a (audit): tear down any existing transport FIRST. A soft on_error
        leaves the old poll loop running; without this, start-after-error
        stacks a SECOND poll loop and the Supervisor receives every message
        twice. stop_bridge() is idempotent.

        R1 (audit): bump the session GENERATION before teardown so the old
        transport's (deferred) on_disconnect can never clobber this session's
        state — each transport lambda captures the generation live when built.
        """
        self._generation += 1  # R1: supersede any prior transport's callbacks
        self.stop_bridge()  # F1a: guarantee ONE live transport per session
        gen = self._generation  # R1: the current session generation

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

        # F7 (audit): an odd injected config could carry a non-numeric chat_id;
        # int() must never raise into the GTK click handler.
        try:
            paired_chat_id = int(chat_id)
        except (TypeError, ValueError):
            _logger.warning(
                "start_bridge: chat_id is not an integer (%s) — error state",
                type(chat_id).__name__,
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

        self._chat_id = paired_chat_id
        self._transport = self._transport_factory(
            token=token,
            chat_id=self._chat_id,
            # R1: capture the session generation; a superseded transport's
            # deferred callback early-returns instead of clobbering state.
            on_connect=lambda g=gen: self._dispatch(self._on_transport_connect, g),
            on_disconnect=lambda r, g=gen: self._dispatch(
                self._on_transport_disconnect, r, g),
            on_error=lambda m, g=gen: self._dispatch(
                self._on_transport_error, m, g),
            on_update=lambda u, g=gen: self._dispatch(self._handle_update, u, g),
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
        """Stop the transport (idempotent) and go DISCONNECTED.

        R1: bump the generation BEFORE disconnecting so any callback the
        teardown queues (deferred dispatch) is recognized as stale.
        """
        if self._state == BridgeState.DISCONNECTED and self._transport is None:
            return  # already stopped — no-op
        self._generation += 1  # R1: supersede the outgoing transport's callbacks
        transport = self._transport
        if transport is not None:
            try:
                import asyncio

                asyncio.run(transport.disconnect())
            except Exception as e:  # noqa: BLE001 — teardown is best-effort
                _logger.warning("bridge disconnect failed: %s", redact_log_preview(str(e)))
        self._transport = None
        self._clear_approval_msgs()
        self._clear_pending_stop()
        self._set_state(BridgeState.DISCONNECTED)

    # ── transport-thread callbacks (already dispatched to main) ──────────

    def _on_transport_connect(self, gen: int | None = None) -> None:
        if gen is not None and gen != self._generation:
            return  # R1: superseded transport — ignore
        self._set_state(BridgeState.CONNECTED)

    def _on_transport_disconnect(self, reason: str, gen: int | None = None) -> None:
        if gen is not None and gen != self._generation:
            return  # R1: a superseded transport's queued disconnect is ignored
        # SP3a §2.4: a stale card→message map must never drive an edit on a
        # dead session — clear it when the transport drops.
        self._clear_approval_msgs()
        # SP4: a stale stop confirmation must never fire on a later session.
        self._clear_pending_stop()
        self._set_state(BridgeState.DISCONNECTED)

    def _on_transport_error(self, message: str,
                            gen: int | None = None) -> None:
        if gen is not None and gen != self._generation:
            return  # R1: superseded transport — ignore
        _logger.warning("bridge error: %s", redact_log_preview(str(message)))
        # F2 (audit): an armed /stop confirmation must not survive an
        # ERROR→reconnect — a later bare STOP would fire stop-all on a "later
        # session" (the exact class §SP4 B4 forbids).
        self._clear_pending_stop()
        self._set_state(BridgeState.ERROR)

    # ── inbound routing (phone → app) ────────────────────────────────────

    def _handle_update(self, update: dict, gen: int | None = None) -> None:
        """Route one Telegram update. Runs on the main thread (dispatched).

        R1 (extended): a SUPERSEDED transport's queued on_update is ignored.
        This closes the duplicate-delivery race — after a reconnect the new
        transport re-fetches from offset=None, so without this guard one phone
        message could reach the Supervisor twice. Safe (no loss): the
        unconfirmed update is refetched by the new transport and routed once.
        """
        if gen is not None and gen != self._generation:
            return  # R1: a superseded transport's queued update is ignored
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
        # SP4 allowlist: a leading "/" is a remote command; ONLY /status, /stop
        # and /help are handled — anything else gets one refusal listing them.
        # Plain chat text (no leading "/") still forwards to the Supervisor.
        stripped = text.strip()
        if stripped.startswith("/"):
            self._handle_command(stripped)
            return
        if self._pending_stop:
            # A reply while a stop confirmation is armed: ONLY the exact word
            # proceeds; anything else aborts (spec §SP4).
            if stripped == STOP_CONFIRM_WORD:
                self._run_stop_all()
            else:
                self._abort_pending_stop()
            return
        if self._arh is None:
            self._send("Bridge unavailable: no agent runtime wired.")
            return
        try:
            self._arh.send_to_special_agent(SUPERVISOR_KEY, text)
        except Exception as e:  # noqa: BLE001 — never let routing kill the bridge
            _logger.error("route to supervisor failed: %s", redact_log_preview(str(e)))
            self._send("Bridge error: could not reach the Supervisor.")

    def _handle_command(self, text: str) -> None:
        """SP4 remote command allowlist — /status, /stop, /help.

        The word after /stop may be the confirmation word (``/stop STOP``).
        Everything else is refused with the allowlist; unknown /commands are
        NEVER forwarded to the Supervisor.
        """
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/status":
            self._pending_stop = False
            self._send_status()
            return
        if cmd == "/stop":
            self._handle_stop_command(arg)
            return
        if cmd == "/help":
            self._pending_stop = False
            self._send(_ALLOWLIST_HELP)
            return
        # Unknown /command → one polite refusal listing the allowlist.
        self._pending_stop = False
        _logger.info("refused remote command: %s", redact_log_preview(str(text)))
        self._send(
            "Unknown command. Available: /status, /stop, /help — or just type "
            "a message to reach the Supervisor.")

    def _send_status(self) -> None:
        """SP4 /status → compact phone-readable summary (≤ ~10 lines)."""
        cb = self._status_provider
        if cb is None:
            self._send("Status unavailable: no project data wired.")
            return
        try:
            summary = cb()
        except Exception as e:  # noqa: BLE001 — a provider fault must not kill the bridge
            _logger.warning("status provider failed: %s", redact_log_preview(str(e)))
            self._send("Status unavailable right now.")
            return
        if not summary:
            summary = "No active project."
        self._send(str(summary))

    def _handle_stop_command(self, arg: str) -> None:
        """SP4 /stop — TYPED two-step confirmation, then the real stop-all.

        ``/stop`` arms a confirmation and prompts for the exact word;
        ``/stop <word>`` (or a bare ``<word>`` on the next message — the
        ``_pending_stop`` branch in ``_handle_message``) runs it. Any other
        reply aborts. Destructive → never a single tap (spec §SP4).
        """
        if arg == STOP_CONFIRM_WORD:
            self._pending_stop = False
            self._run_stop_all()
            return
        if arg:
            # An explicit but wrong word never arms the confirmation.
            self._pending_stop = False
            self._send(
                f"Stop-all NOT run — confirmation word did not match. "
                f"Reply /stop {STOP_CONFIRM_WORD} to confirm.")
            return
        self._pending_stop = True
        self._send(
            f"⚠️ Stop ALL agents from your phone? This cancels every in-flight "
            f"turn and drops the bridge — it cannot be undone. Reply /stop "
            f"{STOP_CONFIRM_WORD} to confirm, or anything else to cancel.")

    def _run_stop_all(self) -> None:
        """Fire the injected stop-all action (window: stop_all_agents() +
        stop_bridge()). No-ops honestly when unwired.

        The ack is sent BEFORE the action because the action stops the bridge
        (transport gone) — a post-hoc ack would never reach the phone.
        """
        self._pending_stop = False
        cb = self._stop_all_handler
        if cb is None:
            self._send("Stop-all unavailable: not wired.")
            return
        self._send("■ Stop-all requested — every agent is being halted.")
        try:
            cb()
        except Exception as e:  # noqa: BLE001 — a control fault must not crash the bridge
            _logger.error("remote stop-all failed: %s", redact_log_preview(str(e)))

    def _abort_pending_stop(self) -> None:
        self._pending_stop = False
        self._send("Stop-all cancelled.")

    def _handle_callback_query(self, cbq: dict) -> None:
        """SP3a: resolve an exec approval from the phone.

        Guards (each failure → callback answered no-op, never a crash):
        foreign chat → current behavior (silent return); data must be exactly
        ``approve:<card_id>`` or ``deny:<card_id>`` (first-colon split);
        resolution is a REAL call to ``ARH.approve_exec`` (pops-first — an
        already-resolved id is a logged no-op there), then the card is
        RE-READ via the feed handler and the Telegram message edited to the
        ACTUAL state (the approve_exec return value is None in all cases).
        """
        chat_id = self._chat_id_of(cbq)
        if chat_id != self._chat_id:
            return  # foreign chat → never processed, no answer
        cbq_id = cbq.get("id")
        data = cbq.get("data", "")

        def _answer(text: str) -> None:
            if self._transport is None or cbq_id is None:
                return
            try:
                self._transport.answer_callback_query(cbq_id, text)
            except Exception as e:  # noqa: BLE001
                _logger.warning(
                    "answer_callback_query failed: %s", redact_log_preview(str(e)))

        action, _, card_id = (data or "").partition(":")
        if action not in ("approve", "deny") or not card_id:
            _logger.info("unrecognized callback payload: %s",
                         redact_log_preview(str(data)))
            _answer("Unrecognized action.")
            return
        approved = action == "approve"

        # Resolve through ARH (pops-first; unknown/already-resolved id is a
        # logged no-op there — safe to call regardless of the card's state).
        if self._arh is not None and hasattr(self._arh, "approve_exec"):
            try:
                self._arh.approve_exec(card_id, approved)
            except Exception as e:  # noqa: BLE001 — never kill the bridge
                _logger.error("approve_exec failed: %s", redact_log_preview(str(e)))

        # Re-read the card and edit the Telegram message to the ACTUAL state.
        status = "unknown"
        command = ""
        fh = self._feed_handler
        if fh is not None and hasattr(fh, "get_card"):
            try:
                card = fh.get_card(card_id)
                if card is not None:
                    status = (card.metadata or {}).get("status") or "unknown"
                    command = str(card.body or "")
            except Exception as e:  # noqa: BLE001
                _logger.warning("card re-read failed: %s", redact_log_preview(str(e)))
        if status == "approved":
            text = f"✅ Approved — {command}".strip()
        elif status == "denied":
            text = f"❌ Denied — {command}".strip()
        else:
            # Desk-first race / already-resolved / card evicted: the truth is
            # "resolved in the app", never the tap's intent.
            text = "Already resolved in the app."
        msg_id = self._approval_msgs.get(card_id)
        _answer("Already resolved in the app." if msg_id is None else "Done.")
        if msg_id is not None and self._transport is not None:
            try:
                self._transport.edit_message_text(msg_id, text)
            except Exception as e:  # noqa: BLE001
                _logger.warning("edit approval message failed: %s",
                                redact_log_preview(str(e)))
        self._approval_msgs.pop(card_id, None)

    def on_feed_card_added(self, card_id_or_data: Any) -> None:
        """SP3a: surface a PENDING exec-approval card on the phone (SP3b wires
        this from the window's ``on_card_added`` seam).

        Allowlist: ONLY cards with ``needs_approval is True`` AND
        ``status == "pending_approval"`` — never needs_review, never plain
        messages. Skips silently when disconnected/unpaired.
        """
        card = card_id_or_data
        fh = self._feed_handler
        if isinstance(card_id_or_data, str) and fh is not None:
            getter = getattr(fh, "get_card", None)
            if callable(getter):
                try:
                    card = getter(card_id_or_data)
                except Exception as e:  # noqa: BLE001
                    _logger.warning("on_feed_card_added lookup failed: %s",
                                    redact_log_preview(str(e)))
                    return
        if card is None or not isinstance(getattr(card, "metadata", None), dict):
            return
        meta = card.metadata
        if meta.get("needs_approval") is not True:
            return
        if meta.get("status") != "pending_approval":
            return
        if self._transport is None or self._chat_id is None:
            return  # disconnected/unpaired — skip silently
        card_id = getattr(card, "card_id", None) or ""
        command = str(card.body or "")
        markup = {"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve:{card_id}"},
            {"text": "❌ Deny", "callback_data": f"deny:{card_id}"},
        ]]}
        try:
            sent = self._transport.send_message(
                f"⚠️ Approval requested — {command}", reply_markup=markup)
            msg_id = sent.get("result", {}).get("message_id") if isinstance(sent, dict) else None
        except Exception as e:  # noqa: BLE001 — a failed mirror must not kill the bridge
            _logger.warning("approval mirror send failed: %s",
                            redact_log_preview(str(e)))
            return
        if msg_id is not None:
            # FIFO cap 50.
            self._approval_msgs[card_id] = msg_id
            self._approval_order.append(card_id)
            while len(self._approval_order) > 50:
                self._approval_msgs.pop(self._approval_order.pop(0), None)

    def _clear_approval_msgs(self) -> None:
        self._approval_msgs.clear()
        self._approval_order.clear()

    def _clear_pending_stop(self) -> None:
        """SP4 disconnect hygiene: drop an armed stop confirmation SILENTLY
        (no reply — there may be no live transport to carry one)."""
        self._pending_stop = False

    def _send_refusal(self, chat_id: int | None) -> None:
        """One polite refusal to the FOREIGN chat; never processed.

        F3 (audit): the refusal must reach the STRANGER, not the paired chat —
        pass the foreign id through to send_message's chat_id override.
        """
        _logger.info("foreign chat %s refused (redacted; not paired)", chat_id)
        if self._transport is None or chat_id is None:
            return
        try:
            self._transport.send_message(
                "This bot is paired to another chat. Sorry.", chat_id=chat_id)
        except Exception as e:  # noqa: BLE001
            _logger.warning(
                "foreign refusal send failed: %s", redact_log_preview(str(e)))

    def _send(self, text: str) -> None:
        if self._transport is None:
            return
        try:
            self._transport.send_message(text)
        except Exception as e:  # noqa: BLE001
            _logger.warning("bridge send failed: %s", redact_log_preview(str(e)))

    # ── app → phone (SP3b) ───────────────────────────────────────────────

    def on_supervisor_reply(self, session_key: str, text: str) -> None:
        """SP3b: forward a Supervisor turn's reply to the paired chat.

        No-op for any other session_key — the bridge is a Supervisor thin
        client only. An HTML-card reply is flattened to text first (Telegram
        is a text surface: send the card's visible content, not markup).
        """
        if session_key != SUPERVISOR_KEY:
            return
        self.forward_to_phone(self._telegram_text(text))

    @staticmethod
    def _telegram_text(text: str) -> str:
        """SP3b: flatten a Supervisor reply for a TEXT surface.

        The whole-message ```html fence (SPEC-13) is flattened to its visible
        text — the card's content, never markup. Any other message passes
        through unchanged. R2/R3 (audit): uses a LINEAR stdlib HTMLParser
        (`_html_fragment_to_text`), so decoded `<`/`>` in TEXT is preserved
        and angle-bracket-dense input cannot freeze the main thread. Fail-safe:
        if anything raises, the original text is returned.
        """
        if not isinstance(text, str) or not text:
            return text if isinstance(text, str) else ""
        try:
            m = _WHOLE_MESSAGE_HTML_FENCE_RE.match(text.strip())
            if not m:
                return text
            return _html_fragment_to_text(m.group(1))
        except Exception as e:  # noqa: BLE001 — never lose a reply to a strip bug
            _logger.warning("html-card flatten failed: %s", redact_log_preview(str(e)))
            return text

    def forward_to_phone(self, text: str) -> None:
        """SP3b: forward a Supervisor reply to the paired chat.

        4096-char message cap → split on paragraph boundaries (≤4096 each;
        blank-line-preferred, falling back to hard 4096). No-ops silently
        when disconnected/unpaired. A chunk send failure logs and continues
        (partial mirror beats none).
        """
        if self._transport is None or self._chat_id is None:
            return
        if not isinstance(text, str) or not text:
            return
        chunks: list[str] = []
        remaining = text
        while len(remaining) > 4096:
            window = remaining[:4096]
            split_at = window.rfind("\n\n")
            if split_at < 1000:  # no sane paragraph boundary — hard split
                split_at = 4096
            chunks.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip("\n")
        if remaining:
            chunks.append(remaining)
        for chunk in chunks:
            if not chunk.strip():
                continue  # F6 (audit): never send an empty/whitespace chunk
            try:
                self._transport.send_message(chunk)
            except Exception as e:  # noqa: BLE001 — keep mirroring later chunks
                _logger.warning("forward chunk failed: %s", redact_log_preview(str(e)))

    # ── defaults ─────────────────────────────────────────────────────────

    @staticmethod
    def _default_load_config() -> dict:
        from utils.telegram_store import load_bridge_config
        return load_bridge_config()

    @staticmethod
    def _default_transport_factory(**kwargs) -> TelegramTransport:
        return TelegramTransport(**kwargs)