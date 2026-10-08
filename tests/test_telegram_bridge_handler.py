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


def test_stop_bridge_clears_approval_map():
    """SP3a §2.4 (disconnect hygiene): stop_bridge drops the card→message map
    so a stale map can never drive an edit against a dead session."""
    h, t, _a = _handler()
    h.start_bridge()
    t["t"].kw["on_connect"]()
    h._approval_msgs["old"] = 55
    h.stop_bridge()
    assert h._approval_msgs == {}


def test_transport_disconnect_clears_approval_map():
    """SP3a §2.4: a transport drop ALSO clears the map — the transport object
    is still referenced, so without the clear a stale tap would edit a dead
    message."""
    h, t, _a = _handler()
    h.start_bridge()
    t["t"].kw["on_connect"]()
    h._approval_msgs["old"] = 55
    t["t"].kw["on_disconnect"]("dropped")
    assert h._approval_msgs == {}
    t["t"].kw["on_update"]({"update_id": 61, "callback_query": {
        "id": "cbq-old2", "data": "deny:old",
        "message": {"chat": {"id": 42}},
    }})
    assert t["t"].edited == [], t["t"].edited


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


def test_callback_query_approve_resolves_and_edits():
    """SP3a: a paired-chat approve callback resolves through ARH.approve_exec
    and edits the Telegram message to the card's ACTUAL state (re-read via
    the feed handler — the approve_exec return is None in all cases)."""
    class RecordingARH:
        def __init__(self):
            self.approve_calls = []
            self._special_agents = ("special:supervisor",)
        def get_special_agents(self):
            return {sk: sk for sk in self._special_agents}
        def approve_exec(self, cid, approved):
            self.approve_calls.append((cid, approved))

    h, t, a = _handler(arh=RecordingARH())
    h.set_feed_handler(type("FH", (), {
        "get_card": staticmethod(lambda cid: type("C", (), {
            "card_id": "1",
            "metadata": {"status": "approved"},
            "body": "$ ls",
        })()),
    })())
    h.start_bridge()
    t["t"].kw["on_connect"]()
    h._approval_msgs["1"] = 77
    update = {"update_id": 4, "callback_query": {
        "id": "cbq-1", "data": "approve:1",
        "message": {"chat": {"id": 42}},
    }}
    t["t"].kw["on_update"](update)
    assert a.approve_calls == [("1", True)], "approve_exec not called"
    assert len(t["t"].answered) == 1
    assert t["t"].answered[0]["id"] == "cbq-1"
    assert any(e["id"] == 77 and "Approved" in e["text"]
               for e in t["t"].edited), t["t"].edited


def test_callback_query_unknown_state_says_already_resolved():
    """SP3a desk-first race: card missing/resolved → the edit must say
    'Already resolved in the app.' — never the tap's intent — and approve_exec
    is still safe (pops-first no-op)."""
    h, t, _a = _handler()
    h.set_feed_handler(type("FH", (), {"get_card": staticmethod(lambda cid: None)})())
    h.start_bridge()
    t["t"].kw["on_connect"]()
    # Desk-first race premise: the approval WAS surfaced to the phone first, so
    # its message is registered — otherwise there is no message to edit.
    h._approval_msgs["zz"] = 99
    update = {"update_id": 41, "callback_query": {
        "id": "cbq-9", "data": "approve:zz",
        "message": {"chat": {"id": 42}},
    }}
    t["t"].kw["on_update"](update)
    assert any("Already resolved" in e["text"]
               for e in t["t"].edited), t["t"].edited


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