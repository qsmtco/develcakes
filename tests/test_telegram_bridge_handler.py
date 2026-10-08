# tests/test_telegram_bridge_handler.py — SPEC-15 SP2 (bridge handler).
#
# Pure-Python state machine + routing tests. The transport is a FAKE (signals
# captured, never a real poll loop); ARH is a fake with send_to_special_agent
# recorded. No GTK (the handler marshals via an injected dispatch seam).

from typing import ClassVar

from ui.handlers.telegram_bridge_handler import (
    BridgeState,
    TelegramBridgeHandler,
)


class FakeTransport:
    """Records outbound calls; exposes captured signal callbacks so the test
    can fire them the way the real transport thread would.

    F3 (audit): ``send_message`` MODELS its target chat — each entry carries
    ``chat_id`` (the paired chat unless overridden, e.g. a foreign refusal).
    """

    def __init__(self, **kw):
        self.kw = kw
        self.sent = []
        self.answered = []
        self.edited = []
        self.chat_id = kw.get("chat_id")
        self.token = kw.get("token")
        self._on_update = kw.get("on_update")

    def send_message(self, text, reply_markup=None, chat_id=None):
        target = chat_id if chat_id is not None else self.chat_id
        self.sent.append({"text": text, "reply_markup": reply_markup,
                          "chat_id": target})
        return {"ok": True}

    async def connect(self):
        pass

    async def disconnect(self):
        # Model the REAL transport: disconnect() fires on_disconnect (through
        # the same dispatch seam). Under deferred dispatch this QUEUES — which
        # is exactly the R1 stale-callback scenario.
        cb = self.kw.get("on_disconnect")
        if cb is not None:
            cb("disconnected")

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


# ── SP3b: app → phone reply mirror ───────────────────────────────────────


def _connected(**kw):
    h, t, a = _handler(**kw)
    h.start_bridge()
    t["t"].kw["on_connect"]()
    return h, t, a


def test_supervisor_reply_forwards_to_phone():
    h, t, _a = _connected()
    h.on_supervisor_reply("special:supervisor", "done — 3 files changed")
    assert [s["text"] for s in t["t"].sent] == ["done — 3 files changed"]


def test_non_supervisor_reply_is_not_forwarded():
    h, t, _a = _connected()
    h.on_supervisor_reply("special:coder", "coding…")
    assert t["t"].sent == []  # thin client: only the Supervisor mirrors


def test_supervisor_reply_when_disconnected_noop():
    h, _t, _a = _handler()  # never started → no transport
    h.on_supervisor_reply("special:supervisor", "hi")  # must not raise


def test_reply_over_4096_hard_split():
    h, t, _a = _connected()
    h.on_supervisor_reply("special:supervisor", "x" * 9000)
    chunks = [s["text"] for s in t["t"].sent]
    assert len(chunks) >= 3
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == "x" * 9000


def test_reply_splits_on_paragraph_boundary():
    h, t, _a = _connected()
    para_a = "a" * 2000
    para_b = "b" * 3000
    h.on_supervisor_reply("special:supervisor", f"{para_a}\n\n{para_b}")
    chunks = [s["text"] for s in t["t"].sent]
    assert len(chunks) == 2
    assert chunks[0] == para_a
    assert chunks[1] == para_b


def test_reply_empty_and_nonstr_no_send():
    h, t, _a = _connected()
    h.on_supervisor_reply("special:supervisor", "")
    h.on_supervisor_reply("special:supervisor", None)
    assert t["t"].sent == []


def test_html_card_reply_flattened_to_text():
    """SPEC-15 §5 last row: a whole-message ```html card → the phone gets the
    card's TEXT, never markup."""
    h, t, _a = _connected()
    card = '```html\n<div><b>Build OK</b><br>line two</div>\n```'
    h.on_supervisor_reply("special:supervisor", card)
    sent = t["t"].sent[-1]["text"]
    assert "Build OK" in sent
    assert "line two" in sent
    assert "<" not in sent and ">" not in sent


def test_plain_text_reply_passes_through_unchanged():
    h, t, _a = _connected()
    h.on_supervisor_reply("special:supervisor", "just **markdown**, no fence")
    assert t["t"].sent[-1]["text"] == "just **markdown**, no fence"


# ── SP4: remote command allowlist ────────────────────────────────────────


def _send_text(h, t, text):
    t["t"].kw["on_update"]({"update_id": 100, "message": {
        "chat": {"id": 42}, "text": text}})


def test_plain_text_still_forwards():
    h, t, arh = _connected()
    _send_text(h, t, "how is the build going?")
    assert arh.calls and arh.calls[-1]["text"] == "how is the build going?"


def test_unknown_slash_command_refused_not_forwarded():
    h, t, arh = _connected()
    _send_text(h, t, "/deploy")
    assert arh.calls == []
    assert any("unknown" in s["text"].lower() for s in t["t"].sent)


def test_status_command_handled_not_forwarded():
    h, t, arh = _connected()
    h.set_status_provider(lambda: "Project: demo\nWork units: 1 pending")
    _send_text(h, t, "/status")
    assert arh.calls == []
    assert any("Project: demo" in s["text"] for s in t["t"].sent)


def test_status_without_provider_is_honest():
    h, t, _a = _connected()
    _send_text(h, t, "/status")
    assert any("unavailable" in s["text"].lower() for s in t["t"].sent)


def test_help_lists_allowlist():
    h, t, _a = _connected()
    _send_text(h, t, "/help")
    blob = " ".join(s["text"] for s in t["t"].sent)
    assert "/status" in blob and "/stop" in blob


# ── SP4: /stop typed two-step confirmation ───────────────────────────────


def test_stop_prompts_and_does_not_fire_on_first_command():
    h, t, _a = _connected()
    fired = []
    h.set_stop_all_handler(lambda: fired.append(True))
    _send_text(h, t, "/stop")
    assert fired == []  # armed, NOT run
    assert h._pending_stop is True
    assert any("confirm" in s["text"].lower() for s in t["t"].sent)


def test_stop_confirmed_by_inline_word_fires_once():
    h, t, _a = _connected()
    fired = []
    h.set_stop_all_handler(lambda: fired.append(True))
    _send_text(h, t, "/stop")
    _send_text(h, t, "/stop STOP")
    assert fired == [True]
    assert h._pending_stop is False


def test_stop_confirmed_by_next_bare_word_fires_once():
    h, t, _a = _connected()
    fired = []
    h.set_stop_all_handler(lambda: fired.append(True))
    _send_text(h, t, "/stop")
    _send_text(h, t, "STOP")
    assert fired == [True]


def test_stop_wrong_word_aborts():
    h, t, _a = _connected()
    fired = []
    h.set_stop_all_handler(lambda: fired.append(True))
    _send_text(h, t, "/stop")
    _send_text(h, t, "nope")
    assert fired == []
    assert h._pending_stop is False
    assert any("cancel" in s["text"].lower() for s in t["t"].sent)


def test_stop_explicit_wrong_word_never_arms():
    h, t, _a = _connected()
    fired = []
    h.set_stop_all_handler(lambda: fired.append(True))
    _send_text(h, t, "/stop yes")
    assert fired == []
    assert h._pending_stop is False


def test_stop_unwired_is_honest_no_crash():
    h, t, _a = _connected()
    _send_text(h, t, "/stop")
    _send_text(h, t, "/stop STOP")  # no handler wired → honest, no raise
    assert any("unavailable" in s["text"].lower() for s in t["t"].sent)


def test_pending_stop_cleared_on_disconnect():
    h, t, _a = _connected()
    fired = []
    h.set_stop_all_handler(lambda: fired.append(True))
    _send_text(h, t, "/stop")
    assert h._pending_stop is True
    t["t"].kw["on_disconnect"]("dropped")
    assert h._pending_stop is False
    # A stale bare confirm after the drop must NOT fire stop-all.
    _send_text(h, t, "STOP")
    assert fired == []


# ── F1: transport stacking on ERROR → reconnect (audit) ─────────────────


class _TrackingFakeTransport(FakeTransport):
    """FakeTransport that records its own disconnect (F1 lifecycle proof)."""

    created: ClassVar[list] = []

    def __init__(self, **kw):
        super().__init__(**kw)
        self.disconnected = False
        _TrackingFakeTransport.created.append(self)

    async def disconnect(self):
        self.disconnected = True


def _tracking_handler():
    _TrackingFakeTransport.created = []
    holder = {}

    def factory(**tkw):
        t = _TrackingFakeTransport(**tkw)
        holder["t"] = t
        return t

    h = TelegramBridgeHandler(
        arh=FakeARH(),
        transport_factory=factory,
        load_config=lambda: {"bot_token": "123:abc", "chat_id": 42,
                             "paired_handle": "@me"},
        dispatch=lambda fn, *a: fn(*a),
        on_feed_card=lambda *a: None,
    )
    return h, holder


def test_restart_tears_down_previous_transport():
    """F1a RED proof: a soft on_error leaves the old poll loop running;
    start_bridge MUST tear it down or two poll loops deliver every message
    twice. Assert exactly ONE live transport after restart."""
    h, holder = _tracking_handler()
    h.start_bridge()
    first = holder["t"]
    h._on_transport_error("soft failure")  # poll loop would keep running
    h.start_bridge()  # reconnect
    second = holder["t"]
    assert first is not second, "a NEW transport must be constructed"
    assert first.disconnected is True, (
        "the previous transport must be torn down (else TWO poll loops)"
    )
    assert h._transport is second
    assert sum(1 for t in _TrackingFakeTransport.created if not t.disconnected) == 1


def test_restart_leaves_exactly_one_live_transport_delivering():
    """F1a: after an ERROR→reconnect, exactly ONE transport is live — so a
    phone message is delivered to the Supervisor exactly ONCE (two live poll
    loops would deliver it twice)."""
    h, holder = _tracking_handler()
    h.start_bridge()
    first = holder["t"]
    h._on_transport_error("soft failure")
    h.start_bridge()
    live = [t for t in _TrackingFakeTransport.created if not t.disconnected]
    assert len(live) == 1, "two live poll loops ⇒ every message delivered twice"
    assert live[0] is holder["t"]
    # The single live transport routes one message to the Supervisor once.
    live[0].kw["on_update"]({"update_id": 1, "message": {
        "chat": {"id": 42}, "text": "hi"}})
    assert h._arh.calls.count({"session_key": "special:supervisor",
                               "text": "hi", "reply_target": None}) == 1
    assert first.disconnected is True


def test_reconnect_after_soft_error_recovers_to_connected():
    """F1b: on_connect re-fires after a soft error (transport latch reset), so
    the handler recovers ERROR → CONNECTED instead of lying 'Offline' forever.
    (Transport-level latch covered in test_telegram_transport; here the handler
    responds to a re-fired on_connect.)"""
    h, t, _a = _connected()
    h._on_transport_error("soft failure")
    assert h.state == "error"
    t["t"].kw["on_connect"]()  # re-announced after recovery
    assert h.state == "connected"


# ── F2: armed /stop must not survive ERROR → reconnect (audit) ──────────


def test_pending_stop_cleared_on_transport_error():
    """F2 RED proof: an armed /stop confirmation surviving an ERROR→reconnect
    would let a later bare STOP fire stop-all on a 'later session' (§SP4 B4)."""
    h, t, _a = _connected()
    fired = []
    h.set_stop_all_handler(lambda: fired.append(True))
    _send_text(h, t, "/stop")
    assert h._pending_stop is True
    t["t"].kw["on_error"]("soft failure")
    assert h._pending_stop is False, "error must clear the armed confirmation"
    t["t"].kw["on_connect"]()  # reconnect
    _send_text(h, t, "STOP")  # stale bare confirm
    assert fired == [], "stop-all must NOT fire from a pre-error confirmation"


# ── F3: foreign-chat refusal targets the FOREIGN chat (audit) ─────────────


def test_foreign_refusal_targets_foreign_chat_not_paired():
    _h, t, _a = _connected()
    t["t"].kw["on_update"]({"update_id": 9, "message": {
        "chat": {"id": 999}, "text": "hack"}})
    assert len(t["t"].sent) == 1
    assert t["t"].sent[0]["chat_id"] == 999, (
        "refusal must go to the FOREIGN chat, not the paired chat (42)"
    )
    assert all(s["chat_id"] != 42 for s in t["t"].sent)


# ── F5/F6/F7 (audit suggestions) ────────────────────────────────────────


def test_flatten_strips_literal_and_encoded_script():
    """R2/R3: a '>' inside a quoted attribute must not truncate, and BOTH a
    literal and an entity-encoded `<script>` tag must be stripped (with the
    specified bodies dropped)."""
    h, _t, _a = _connected()
    out = h._telegram_text('```html\n<div title="a > b">x</div>\n```')
    assert out == 'x'
    # Literal script element → body dropped.
    out2 = h._telegram_text('```html\n<script>alert(1)</script>ok\n```')
    assert "<script>" not in out2 and "alert" not in out2 and out2.endswith("ok")
    # Entity-encoded script tag → still stripped.
    out3 = h._telegram_text('```html\n&lt;script&gt;alert(1)&lt;/script&gt;ok\n```')
    assert "<script>" not in out3 and out3.endswith("ok")


# ── R3: decoded angle-bracket TEXT must survive (audit) ─────────────────


def test_flatten_preserves_decoded_angle_bracket_text():
    """R3 (audit): entity-decoded `<` / `>` in TEXT must NOT be re-stripped as
    markup — the old strip-after-unescape ate real content."""
    h, _t, _a = _connected()
    cases = {
        '```html\n<p>5 &lt; 6 &gt; 4</p>\n```': "5 < 6 > 4",
        '```html\n<p>use &lt;value&gt; here</p>\n```': "use <value> here",
        '```html\n<p>git log &lt;branch&gt; --oneline</p>\n```':
            "git log <branch> --oneline",
    }
    for src, expected in cases.items():
        out = h._telegram_text(src)
        assert expected in out, f"lost decoded text: {out!r} (want {expected!r})"


def test_flatten_is_linear_on_angle_bracket_dense_input():
    """R2 (audit): the flatten must be LINEAR — 16K '<' well under 100ms
    (the old attribute-aware regex took ~4s at O(N²))."""
    import time

    h, _t, _a = _connected()
    dense = "```html\n" + ("<" * 16000) + "\n```"
    t0 = time.perf_counter()
    h._telegram_text(dense)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.1, f"flatten too slow on 16K '<': {elapsed:.3f}s"


def test_forward_whitespace_only_sends_nothing():
    """F6: a whitespace-only / all-newline reply must produce ZERO sends."""
    h, t, _a = _connected()
    h.on_supervisor_reply("special:supervisor", " " * 5000)
    h.on_supervisor_reply("special:supervisor", "\n" * 5000)
    assert t["t"].sent == []


def test_start_bridge_non_int_chat_id_is_honest_error():
    """F7: an odd injected chat_id must ERROR, never raise into the caller."""
    h, t, _a = _handler(store={"bot_token": "123:abc", "chat_id": "not-a-number",
                               "paired_handle": "@me"})
    h.start_bridge()  # must not raise
    assert h.state == "error"
    assert "t" not in t  # no transport constructed


# ── R1: deferred-dispatch (production GLib.idle_add) stale-callback ──────


class DeferredDispatch:
    """Queues callbacks like GLib.idle_add; drain() runs them in order.

    This is the harness the inline-dispatch fakes cannot emulate: a transport
    teardown queues on_disconnect, and start_bridge's synchronous connect runs
    BEFORE that queued callback drains.
    """

    def __init__(self):
        self.queue: list = []

    def __call__(self, fn, *args):
        self.queue.append((fn, args))

    def drain(self):
        while self.queue:
            fn, args = self.queue.pop(0)
            fn(*args)


def _deferred_handler(*, fail_connect=False):
    holder = {}
    disp = DeferredDispatch()

    def factory(**tkw):
        t = FakeTransport(**tkw)
        holder["t"] = t
        return t

    class ARH:
        def __init__(self):
            self.calls = []

        def get_special_agents(self):
            return {"special:supervisor": "special:supervisor"}

        def send_to_special_agent(self, session_key, text, reply_target=None):
            self.calls.append({"session_key": session_key, "text": text,
                               "reply_target": reply_target})

    h = TelegramBridgeHandler(
        arh=ARH(),
        transport_factory=factory,
        load_config=lambda: {"bot_token": "123:abc", "chat_id": 42,
                             "paired_handle": "@me"},
        dispatch=disp,
        on_feed_card=lambda *a: None,
    )
    return h, holder, disp


def test_deferred_stale_disconnect_does_not_clobber_error(monkeypatch):
    """R1 RED (deferred dispatch): teardown of the OLD transport queues
    on_disconnect; it drains AFTER a FAILED reconnect set ERROR — and must NOT
    rewrite it to DISCONNECTED (toolbar would lie, no error card)."""
    h, holder, disp = _deferred_handler()
    h.start_bridge()
    holder["t"].kw["on_connect"]()
    disp.drain()
    assert h.state == "connected"

    # Reconnect whose connect() FAILS with an auth error.
    def failing_connect(self):
        raise ValueError("revoked token")

    monkeypatch.setattr(type(holder["t"]), "connect", failing_connect, raising=False)
    h.start_bridge()  # stop_bridge queues OLD on_disconnect, then CONNECTING→ERROR
    assert h.state in ("connecting", "error")
    disp.drain()  # the stale disconnect lands HERE
    assert h.state == "error", (
        "a superseded transport's queued disconnect must not clobber ERROR "
        f"(got {h.state!r})"
    )


def test_deferred_stale_disconnect_never_observes_disconnected(monkeypatch):
    """R1 RED (deferred dispatch): a HEALTHY reconnect must never observe
    DISCONNECTED *between* CONNECTING and CONNECTED — the stale queued
    disconnect must be ignored."""
    h, holder, disp = _deferred_handler()
    h.start_bridge()
    holder["t"].kw["on_connect"]()
    disp.drain()

    seen: list = []
    h._on_state_change = lambda s: seen.append(s)
    h.start_bridge()  # queues OLD on_disconnect; sets CONNECTING synchronously
    disp.drain()  # stale disconnect drains here — must be ignored
    holder["t"].kw["on_connect"]()  # the NEW transport announces
    disp.drain()

    # The requirement: after CONNECTING is observed, the next state must be
    # CONNECTED — never a stale DISCONNECTED.
    assert "connecting" in seen, seen
    after_connecting = seen[seen.index("connecting") + 1:]
    assert "disconnected" not in after_connecting, (
        f"stale disconnect leaked DISCONNECTED after CONNECTING: {seen}"
    )
    assert after_connecting and after_connecting[0] == "connected", seen
    assert h.state == "connected"


def test_deferred_stale_update_not_routed_after_reconnect():
    """R1 (extended): a superseded transport's QUEUED on_update must not route
    — after a reconnect the new transport re-fetches from offset=None, so
    without the guard one phone message reaches the Supervisor TWICE."""
    h, holder, disp = _deferred_handler()
    h.start_bridge()
    t1 = holder["t"]
    t1.kw["on_connect"]()
    disp.drain()
    assert t1 is holder["t"]

    # Transport 1 fetches an update but its routing callback is QUEUED (not
    # yet drained) — the reconnect race.
    update = {"update_id": 7, "message": {"chat": {"id": 42}, "text": "deploy it"}}
    t1.kw["on_update"](update)

    # Reconnect BEFORE the queued update drains (supersedes transport 1).
    h.start_bridge()
    t2 = holder["t"]
    assert t2 is not t1
    t2.kw["on_connect"]()
    disp.drain()  # transport 1's stale on_update drains here — must be ignored

    # The NEW transport re-fetches the same update (offset=None) and routes it.
    t2.kw["on_update"](update)
    disp.drain()

    routed = [c for c in h._arh.calls
              if c["session_key"] == "special:supervisor"]
    assert len(routed) == 1, (
        f"the same update must reach the Supervisor exactly once, got {routed}"
    )
    assert routed[0]["text"] == "deploy it"


# ── R4: state-callback dedup ────────────────────────────────────────────


def test_noop_state_reset_does_not_refire_callback():
    """R4: re-setting the same state must not re-fire on_state_change (a
    flapping link would otherwise spam 'offline' cards)."""
    changes: list = []
    h, _t, _a = _handler(on_state_change=lambda s: changes.append(s))
    h._set_state(BridgeState.CONNECTED)
    h._set_state(BridgeState.CONNECTED)  # no-op re-set
    h._set_state(BridgeState.CONNECTED)
    assert changes == [BridgeState.CONNECTED]
    h._set_state(BridgeState.ERROR)
    assert changes == [BridgeState.CONNECTED, BridgeState.ERROR]