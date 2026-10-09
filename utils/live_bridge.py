# utils/live_bridge.py — SPEC-19 SP4: the two-phase action bridge (pure).
#
# The page-side `window.develcakes.call(method, params)` API funnels into this
# module. It is DELIBERATELY pure: no GTK, no ARH/feed import, and it NEVER
# performs file I/O or exec itself.
#
# Consequential methods (CONSEQUENTIAL_METHODS) return {status:"pending", id}
# and hand (method, params, call_id) to an injected `approver` callback; the
# human decides via the EXISTING exec-approval card (SPEC-19 §5 SP4 F6: the
# page can never self-approve). Resolution arrives later via
# `resolve(call_id, ok, data)` — approvals take minutes, so there is NO
# timeout (a 30s Promise timeout would eat a real approval).
#
# Read-only methods (READ_ONLY_METHODS, SPEC-20a) are the registry the SP4
# header used to call "future": non-consequential. dispatch() calls the
# injected reader ONCE and returns {status:"ok"|"error", id, data}
# synchronously. No approver, no approval card. No reader wired →
# {status:"error", data:{reason:"no reader wired"}} (G6: never crash, never
# hang). A raising reader becomes an error result; it is never propagated.
#
# House style (SPEC-19 SP6 carry-over): setter-injection, no cross-module
# imports in utils/, BLE001-guarded boundaries.

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

_logger = logging.getLogger(__name__)

# Methods that REQUIRE human approval before anything happens.
CONSEQUENTIAL_METHODS: frozenset[str] = frozenset({
    "exec_command", "write_file", "edit_file", "approve_exec",
})

# Non-consequential registry (SPEC-20a). Exactly one method in v1: a
# validated local image read. Not an approval, not an exec.
READ_ONLY_METHODS: frozenset[str] = frozenset({"read_file"})

# Bounded pending registry (FIFO) — no unbounded state from page-driven calls.
DEFAULT_PENDING_CAP = 50


class LiveBridge:
    """Method registry + two-phase approval routing (pure; no I/O of its own).

    Args:
        approver: Callable[[str, dict, str], None] — (method, params, call_id).
            Routes to the EXISTING approval machinery (the exec-approval card).
            None → consequential calls stay pending forever (G6: no approver,
            no execution, no crash — never self-resolves).
        cap: max pending calls (FIFO; oldest dropped as error).
        on_drop: optional Callable[[dict], None] fired with the error-result of
            each dropped call (so the page's Promise can be failed, never left
            hanging).
    """

    def __init__(
        self,
        approver: Callable[[str, dict, str], None] | None = None,
        cap: int = DEFAULT_PENDING_CAP,
        on_drop: Callable[[dict], None] | None = None,
    ) -> None:
        self._approver = approver
        self._reader: Callable[[dict], dict] | None = None
        self._cap = max(1, int(cap))
        self._on_drop = on_drop
        # OrderedDict as a bounded FIFO registry: call_id -> {"method","params"}.
        self._pending: OrderedDict[str, dict] = OrderedDict()
        self._seq = 0

    def set_reader(self, cb: Callable[[dict], dict] | None) -> None:
        """Inject the read-only executor. The bridge does not import it.

        cb(params) -> dict. Success dicts carry mime + base64; refusals
        carry reason. None clears the reader (calls then error cleanly).
        """
        self._reader = cb

    # ── public API ───────────────────────────────────────────────────────

    def dispatch(self, method: str, params: dict | None = None) -> dict:
        """Route one page call. The ONLY entry point.

        - read-only → call the reader once; return ok/error now. Approver
          is NOT called and the call is NOT pending.
        - unknown method → error result (approver NOT called).
        - consequential → ALWAYS pending + approver(method, params, call_id).
          The bridge NEVER executes anything on this path.
        """
        params = params if isinstance(params, dict) else {}
        call_id = self._next_id()
        if method in READ_ONLY_METHODS:
            return self._finish_read_only(call_id, params)
        if method not in CONSEQUENTIAL_METHODS:
            return {
                "status": "error",
                "id": call_id,
                "data": {"reason": f"unknown method: {method!r}"},
            }
        self._record_pending(call_id, method, params)
        self._invoke_approver(method, params, call_id)
        return {"status": "pending", "id": call_id}

    def resolve(
        self,
        call_id: str,
        ok: bool,
        data: Any = None,
        on_result: Callable[[dict], None] | None = None,
    ) -> dict | None:
        """Record the human's decision for a pending call.

        Returns the result dict (and fires `on_result` if given), or None for an
        unknown id (silently ignored — a stale/duplicate resolution is a no-op).
        """
        if call_id not in self._pending:
            _logger.debug("live_bridge: resolve for unknown id %r ignored", call_id)
            return None
        self._pending.pop(call_id, None)
        result = {
            "status": "ok" if ok else "refused",
            "id": call_id,
            "data": data if data is not None else {},
        }
        if on_result is not None:
            try:
                on_result(result)
            except Exception:
                _logger.debug("live_bridge: on_result raised", exc_info=True)
        return result

    def pending_ids(self) -> list[str]:
        return list(self._pending.keys())

    def has_pending(self, call_id: str) -> bool:
        return call_id in self._pending

    # ── internals ────────────────────────────────────────────────────────

    def _finish_read_only(self, call_id: str, params: dict) -> dict:
        """One reader call. Exceptions become an error result; never raised."""
        if self._reader is None:
            _logger.debug(
                "live_bridge: no reader wired — call %s errors (G6: no read)",
                call_id,
            )
            return {
                "status": "error",
                "id": call_id,
                "data": {"reason": "no reader wired"},
            }
        try:
            data = self._reader(params)
        except Exception:
            _logger.exception("live_bridge: reader raised for %s", call_id)
            return {
                "status": "error",
                "id": call_id,
                "data": {"reason": "reader failed"},
            }
        if (
            isinstance(data, dict)
            and isinstance(data.get("mime"), str)
            and isinstance(data.get("base64"), str)
            and "reason" not in data
        ):
            return {
                "status": "ok",
                "id": call_id,
                "data": {"mime": data["mime"], "base64": data["base64"]},
            }
        reason = "reader failed"
        if isinstance(data, dict):
            got = data.get("reason")
            if isinstance(got, str) and got:
                reason = got
        return {"status": "error", "id": call_id, "data": {"reason": reason}}

    def _next_id(self) -> str:
        self._seq += 1
        return f"dc{self._seq}"

    def _record_pending(self, call_id: str, method: str, params: dict) -> None:
        self._pending[call_id] = {"method": method, "params": params}
        while len(self._pending) > self._cap:
            old_id, _entry = self._pending.popitem(last=False)  # FIFO drop
            dropped = {
                "status": "error",
                "id": old_id,
                "data": {"reason": "pending-call cap reached (dropped)"},
            }
            _logger.warning("live_bridge: dropped pending call %s (cap %d)",
                            old_id, self._cap)
            if self._on_drop is not None:
                try:
                    self._on_drop(dropped)
                except Exception:
                    _logger.debug("live_bridge: on_drop raised", exc_info=True)

    def _invoke_approver(self, method: str, params: dict, call_id: str) -> None:
        """Hand the call to the injected approver — the ONLY thing dispatch
        does for a consequential method. A raising approver is isolated: the
        call stays pending (G6), never executes, never crashes."""
        if self._approver is None:
            _logger.info(
                "live_bridge: no approver wired — call %s (%s) stays pending "
                "(G6: no execution)", call_id, method,
            )
            return
        try:
            self._approver(method, params, call_id)
        except Exception:
            _logger.exception("live_bridge: approver raised for %s", call_id)