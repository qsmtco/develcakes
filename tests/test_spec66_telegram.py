# tests/test_spec66_telegram.py — SPEC-66 §2.10: sends leave the GTK thread.
#
# R-21 row node: test_send_leaves_the_caller_thread
#
# PLAIN runner (§12.7): the closure is render/transport only — no GTK widget is
# constructed. The bridge takes an injected transport factory, so no socket and
# no token are needed.
#
# Pre-fix `_send` called `transport.send_message` inline, on whatever thread
# called it — in production the GTK main thread — so every Telegram round-trip
# froze the UI until the socket answered. Now each send is enqueued to ONE
# daemon worker; `flush_outbound()` is the synchronous seam.

from __future__ import annotations

import threading
import time

from ui.handlers.telegram_bridge_handler import TelegramBridgeHandler

GTK_HEADLESS_SAFE = True


class _FakeTransport:
    """Records (text, chat_id, thread) for each send_message call."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, int | None, threading.Thread]] = []
        self.lock = threading.Lock()

    def send_message(self, text: str, chat_id: int | None = None) -> None:
        with self.lock:
            self.sent.append((text, chat_id, threading.current_thread()))

    def texts(self) -> list[str]:
        with self.lock:
            return [t for t, _c, _th in self.sent]


def _bridge() -> tuple[TelegramBridgeHandler, _FakeTransport]:
    """A handler with a fake transport already connected."""
    transport = _FakeTransport()
    bridge = TelegramBridgeHandler(transport_factory=lambda **_k: transport)
    bridge._transport = transport
    bridge._chat_id = 12345
    return bridge, transport


def test_send_leaves_the_caller_thread():
    """R-21 NODE: the send must NOT happen on the caller's thread, and the
    worker must be a daemon (a hung socket must not keep the app alive)."""
    bridge, transport = _bridge()
    caller = threading.current_thread()

    bridge._send("hello")
    bridge.flush_outbound()

    assert transport.sent, "nothing was sent at all"
    _text, _chat, work_thread = transport.sent[0]
    assert work_thread is not caller, (
        "send_message ran on the caller thread — the GTK main thread would "
        "block for the length of the socket call (SPEC-66 §2.10)"
    )
    assert bridge._send_thread is not None
    assert bridge._send_thread.daemon is True, (
        "the send worker is not a daemon — a hung socket would outlive the app"
    )


def test_flush_outbound_drains_in_order():
    """Order inside one call is preserved: chunks arrive in the order given,
    on one worker, before flush_outbound returns."""
    bridge, transport = _bridge()
    first = "alpha " * 900   # > 4096 chars, forces a split
    second = "omega " * 900
    bridge.forward_to_phone(first + "\n\n" + second)
    bridge.flush_outbound()

    texts = transport.texts()
    assert len(texts) >= 2, f"expected the long text to split, got {texts!r}"
    # Order is what matters: the concatenation must reproduce the input's
    # chunk sequence, so no chunk may jump ahead of another.
    assert texts[0].startswith("alpha"), f"first chunk out of order: {texts[0]!r}"
    assert texts[-1].endswith("omega "), f"last chunk out of order: {texts[-1]!r}"
    # Every chunk was sent by the SAME worker (one _deliver call → one thread).
    workers = {th for _t, _c, th in transport.sent}
    assert len(workers) == 1, f"chunks went out on {len(workers)} threads"


def test_flush_outbound_joins_before_returning():
    """The seam is synchronous: after flush_outbound the transport has already
    been called. Without the join this test would race."""
    bridge, transport = _bridge()
    bridge._send("sync check")
    bridge.flush_outbound()
    assert transport.texts() == ["sync check"]


def test_send_errors_do_not_propagate():
    """A dead socket must not break the caller's turn."""
    class _Boom:
        def send_message(self, *_a, **_k):
            raise RuntimeError("socket is dead")

    bridge = TelegramBridgeHandler(transport_factory=lambda **_k: _Boom())
    bridge._transport = _Boom()
    bridge._chat_id = 1
    bridge._send("will fail")  # must not raise
    bridge.flush_outbound()
    bridge.forward_to_phone("also fails")  # must not raise
    bridge.flush_outbound()


def test_refusal_goes_to_the_foreign_chat_off_thread():
    """The refusal send keeps its chat_id override AND rides the worker."""
    bridge, transport = _bridge()
    caller = threading.current_thread()
    bridge._send_refusal(999)
    bridge.flush_outbound()

    assert transport.sent, "the refusal was never sent"
    _text, chat_id, work_thread = transport.sent[0]
    assert chat_id == 999, "the refusal did not target the foreign chat"
    assert work_thread is not caller


def test_many_sends_serialize_without_interleaving():
    """The lock serializes deliveries: two overlapping _deliver calls never
    interleave their chunks."""
    bridge, transport = _bridge()
    threads = [
        threading.Thread(target=bridge._send, args=(f"msg{i}",))
        for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    bridge.flush_outbound()

    texts = transport.texts()
    assert len(texts) == 5, texts
    assert sorted(texts) == [f"msg{i}" for i in range(5)]


def test_slow_transport_latency_does_not_hide_pending_sends():
    """SPEC-66 §2.10 / W3-audit HIGH #1: a REALISTIC transport latency must
    not make a send invisible. The pre-existing handler tests read
    `transport.sent` right after a send and passed only because a
    zero-latency fake won the race; with a 3 ms socket they saw nothing.

    Two sends, then flush_outbound(): both payloads must be present, in the
    order given, and neither may have run on the caller's thread.
    """
    class _SlowTransport(_FakeTransport):
        def send_message(self, text, chat_id=None):
            time.sleep(0.003)  # a realistic socket round-trip
            super().send_message(text, chat_id)

    transport = _SlowTransport()
    bridge = TelegramBridgeHandler(transport_factory=lambda **_k: transport)
    bridge._transport = transport
    bridge._chat_id = 12345
    caller = threading.current_thread()

    bridge._send("first payload")
    bridge._send("second payload")
    # Both sends are outstanding right now; the reader must order them.
    bridge.flush_outbound()

    texts = transport.texts()
    assert texts == ["first payload", "second payload"], (
        f"payloads lost or reordered under transport latency: {texts!r}"
    )
    workers = {th for _t, _c, th in transport.sent}
    assert caller not in workers, (
        "a send ran on the caller thread — the GTK main thread would block "
        "for the length of the socket call (SPEC-66 §2.10)"
    )

