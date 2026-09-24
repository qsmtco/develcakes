# transport/base.py — Transport ABC.
#
# SPEC-05 Sub-Phase 1. Extracted as part of the R1 gateway strip (decision #1:
# the transport core — connection lifecycle, reconnect/backoff, framing — is
# retained and cleaned for reuse by the future Telegram transport; OpenClaw
# protocol and identity/auth are removed). Architecture ruling #7: this is a
# NEW `transport/` package (base.py ABC + openclaw.py cleaned core).
#
# Dormant in Sub-Phase 1: zero callers. SP2 repoints send sites onto it.

from abc import ABC, abstractmethod
from collections.abc import Callable


class Transport(ABC):
    """Connection lifecycle contract for remote transports (Telegram post-MVP).

    Implementations own their own event-loop/threading model; the ABC only
    pins the lifecycle surface. Callbacks (see status_signals) are invoked
    from the implementation's own thread — consumers marshal to the UI
    thread themselves.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Schedule the connection and wait for it to be established.

        Bounded wait (implementation-defined, ~10s): on timeout fires the
        on_error signal and returns without being connected. Poll
        is_connected() (implementation detail) or rely on the on_connect
        signal to know when the link is live.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close the connection and stop reconnect attempts."""

    @abstractmethod
    async def send(self, payload: dict, *, on_response: Callable[[dict], None] | None = None) -> None:
        """Send one JSON-serializable payload over the connection.

        on_response (optional, keyword-only): per-request callback fired
        with the response payload correlated by the payload's "id" field.
        """

    @abstractmethod
    def status_signals(self) -> tuple:
        """Return (on_connect, on_disconnect, on_error) signal callbacks."""
