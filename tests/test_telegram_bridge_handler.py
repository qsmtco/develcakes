# tests/test_telegram_bridge_handler.py — SPEC-15 SP2 (bridge handler).
#
# Pure-Python state machine + routing tests. The transport is a FAKE (signals
# captured, never a real poll loop); ARH is a fake with send_to_special_agent
# recorded. No GTK (the handler marshals via an injected dispatch seam).


from ui.handlers.telegram_bridge_handler import (
    BridgeState,
    TelegramBridgeHandler,
)


class FakeTransport:
    """Records outbound calls; exposes captured signal callbacks so the test
    can fire them the way the real transport thread would."""

    def __init__(self, **kw):
        self.kw = kw
        self.sent = []
        self.answered = []
        self.edited = []
        self.chat_id = kw.get("chat_id")
        self.token = kw.get("token")
        self._on_update = kw.get("on_update")

    def send_message(self, text, reply_markup=None):
        self.sent.append({"text": text, "reply_markup": reply_markup})
        return {"ok": True}

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    def answer_callback_query(self, callback_query_id, text=None):
        self.answered.append({"id": callback_query_id, "text": text})
        return {"ok": True}

    def edit_message_text(self, message_id, text, reply_markup=None):
        self.edited.append({"id": message_id, "text": text})
        return {"ok": True}


class FakeARH:
    def __init__(self, special_agents=("special:supervisor",)):
        self.calls = []
        # Default: the Supervisor IS registered, so the existing state-machine
        # tests exercise the normal path. Pass an explicit list WITHOUT
        # "special:supervisor" to test the BUG#1 refusal.
        self._special_agents = special_agents

    def get_special_agents(self):
        return {sk: sk for sk in (self._special_agents or [])}

    def send_to_special_agent(self, session_key, text, reply_target=None):
        self.calls.append({"session_key": session_key, "text": text,
                           "reply_target": reply_target})


def _handler(**kw):
    """Bridge handler with fakes injected; dispatch runs inline (no GLib)."""
    transport_holder = {}
    arh = kw.pop("arh", FakeARH())
    store = kw.pop("store", {"bot_token": "123:abc", "chat_id": 42,
                             "paired_handle": "@me"})
    cards = kw.pop("cards", [])

    def factory(**tkw):
        t = FakeTransport(**tkw)
        transport_holder["t"] = t
        return t

    h = TelegramBridgeHandler(
        arh=arh,
        transport_factory=factory,
        load_config=lambda: dict(store),
        dispatch=lambda fn, *a: fn(*a),  # inline main-thread dispatch
        on_feed_card=lambda title, body: cards.append((title, body)),
        **kw,
    )
    return h, transport_holder, arh


# ── state machine ────────────────────────────────────────────────────────


def test_initial_state_disconnected():
    h, _t, _a = _handler()
    assert h.state == BridgeState.DISCONNECTED
    assert h.is_connected() is False


def test_start_without_token_goes_error():
    h, t, _a = _handler(store={"bot_token": "", "chat_id": None, "paired_handle": ""})
    h.start_bridge()
    assert h.state == BridgeState.ERROR
    assert "t" not in t  # no transport constructed


def test_start_without_chat_id_goes_error():
    h, t, _a = _handler(store={"bot_token": "123:abc", "chat_id": None,
                               "paired_handle": ""})
    h.start_bridge()
    assert h.state == BridgeState.ERROR
    assert "t" not in t


def test_start_goes_connecting_then_connected_on_signal():
    h, t, _a = _handler()
    h.start_bridge()
    assert h.state == BridgeState.CONNECTING
    # Simulate the transport thread firing on_connect.
    t["t"].kw["on_connect"]()
    assert h.state == BridgeState.CONNECTED
    assert h.is_connected() is True


def test_start_bridge_refuses_when_supervisor_not_registered():
    """BUG#1 (SP2 audit, CRITICAL): supervisor.yaml ships manual-add
    (auto_open:false), so on most installs it is NOT registered. A phone
    message would then be silently dropped by ARH. start_bridge must refuse
    UP FRONT — ERROR state + an honest feed card — and NOT create a transport
    (no poll loop for a bridge that cannot route)."""
    cards = []
    arh = FakeARH(special_agents=())  # no supervisor registered
    h, t, _a = _handler(arh=arh, cards=cards)
    h.start_bridge()
    assert h.state == BridgeState.ERROR
    assert "t" not in t, "no transport may be constructed when carrying cannot route"
    assert len(cards) == 1, cards
    title, body = cards[0]
    assert "not registered" in title.lower()
    assert "agents tab" in body.lower()


def test_start_bridge_proceeds_when_supervisor_registered():
    """The guard must not block the normal path (supervisor present)."""
    h, t, _a = _handler(arh=FakeARH(special_agents=("special:supervisor",)))
    h.start_bridge()
    assert "t" in t
    assert h.state == BridgeState.CONNECTING


def test_error_signal_goes_error_state():
    h, t, _a = _handler()
    h.start_bridge()
    t["t"].kw["on_error"]("boom")
    assert h.state == BridgeState.ERROR


def test_stop_goes_disconnected():
    h, t, _a = _handler()
    h.start_bridge()
    t["t"].kw["on_connect"]()
    assert h.state == BridgeState.CONNECTED
    h.stop_bridge()
    assert h.state == BridgeState.DISCONNECTED
    assert h.is_connected() is False


def test_stop_idempotent_when_disconnected():
    h, _t, _a = _handler()
    h.stop_bridge()  # must not raise
    assert h.state == BridgeState.DISCONNECTED


# ── inbound routing (phone → app) ────────────────────────────────────────


def test_paired_chat_text_routes_to_supervisor():
    h, t, arh = _handler()
    h.start_bridge()
    t["t"].kw["on_connect"]()
    update = {"update_id": 1, "message": {"chat": {"id": 42}, "text": "status?"}}
    t["t"].kw["on_update"](update)
    assert len(arh.calls) == 1
    assert arh.calls[0]["session_key"] == "special:supervisor"
    assert arh.calls[0]["text"] == "status?"


def test_foreign_chat_text_refused_never_routed():
    h, t, arh = _handler()
    h.start_bridge()
    t["t"].kw["on_connect"]()
    update = {"update_id": 2, "message": {"chat": {"id": 999}, "text": "hack"}}
    t["t"].kw["on_update"](update)
    assert arh.calls == []  # never processed
    # Exactly one polite refusal, and it never contains the user's text.
    assert len(t["t"].sent) == 1, t["t"].sent
    assert "paired" in t["t"].sent[0]["text"].lower()
    assert "hack" not in t["t"].sent[0]["text"]


def test_routing_when_arh_missing_refuses():
    h, t, _a = _handler(arh=None)
    h.start_bridge()
    t["t"].kw["on_connect"]()
    update = {"update_id": 3, "message": {"chat": {"id": 42}, "text": "hi"}}
    t["t"].kw["on_update"](update)
    assert any("unavailable" in s["text"].lower() or "refus" in s["text"].lower()
               for s in t["t"].sent), t["t"].sent


def test_callback_query_answered_not_wired():
    h, t, _a = _handler()
    h.start_bridge()
    t["t"].kw["on_connect"]()
    update = {"update_id": 4, "callback_query": {
        "id": "cbq-1", "data": "approve:1",
        "message": {"chat": {"id": 42}},
    }}
    t["t"].kw["on_update"](update)
    assert len(t["t"].answered) == 1
    assert t["t"].answered[0]["id"] == "cbq-1"
    assert "not yet wired" in (t["t"].answered[0]["text"] or "").lower()


def test_foreign_chat_callback_refused():
    h, t, _a = _handler()
    h.start_bridge()
    t["t"].kw["on_connect"]()
    update = {"update_id": 5, "callback_query": {
        "id": "cbq-2", "data": "x", "message": {"chat": {"id": 999}},
    }}
    t["t"].kw["on_update"](update)
    assert t["t"].answered == []  # foreign → never answered


def test_forward_to_phone_stub_registered():
    """SP3 seam: the app→phone direction is a documented stub."""
    h, _t, _a = _handler()
    h.start_bridge()
    assert hasattr(h, "forward_to_phone")
    h.forward_to_phone("reply")  # must not raise (stub)