# tests/test_no_gateway_residuals.py
# SPEC-05 SP2 — durable pins for the R1 gateway strip (send-site repointing).
# Pattern mirror: tests/test_no_kb_residuals.py (SPEC-04).
#
# Pins:
#   1. No gateway send sites / client refs remain anywhere in ui/handlers/
#      (SP3b deleted gateway_handler.py + connection_sync_handler.py — the
#      sweep covers the whole directory with no exclusions).
#   2. send_raw_message is gone from chat_handler (SP2 R2 dead-code deletion).
#   3. Source-shape: on_send routes via send_to_special_agent and no longer
#      touches _gw (behavioral-through-GTK is covered by the receiver pins).
#   4. Behavioral receiver semantics: unknown session key → warning + no-op,
#      no raise (spec §3: MVP has no remote agents).

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

REPOINTED_FILES = [
    "ui/handlers/chat_handler.py",
    "ui/handlers/review_handler.py",
    "ui/handlers/forward_handler.py",
    "ui/handlers/agent_command_handler.py",
    "ui/handlers/command_handler.py",
]

class TestNoGatewaySendSites:
    def test_repointed_handlers_have_no_gw_sends(self):
        """All repointed handlers are free of _gw sends / gateway_client refs."""
        for rel in REPO.glob("ui/handlers/*.py"):
            src = rel.read_text(encoding="utf-8")
            for needle in ("_gw.send_message", "gateway_client", "self._gw"):
                assert needle not in src, f"{rel.name}: found {needle!r}"

    def test_gateway_package_is_gone(self):
        """SPEC-05 SP3b: the gateway package and the two deleted handlers are
        gone from the tree — nothing can re-import them. (Subsumes the old
        exclusion-set guard and the deleted connection_sync source pin.)"""
        import importlib.util

        assert importlib.util.find_spec("gateway") is None, (
            "gateway/ package must be deleted (SPEC-05 R1)"
        )
        assert not (REPO / "ui/handlers" / "gateway_handler.py").exists()
        assert not (REPO / "ui/handlers" / "connection_sync_handler.py").exists()

    def test_window_has_no_gateway_construction(self):
        """SPEC-05 SP3a: window.py no longer constructs GatewayHandler /
        ConnectionSyncHandler and no longer imports GatewayHandler. The
        handler FILES still exist until SP3b — this pins the window side only."""
        src = (REPO / "ui/window.py").read_text(encoding="utf-8")
        assert "GatewayHandler(" not in src, "window.py must not construct GatewayHandler"
        assert "ConnectionSyncHandler(" not in src, (
            "window.py must not construct ConnectionSyncHandler"
        )
        assert "from ui.handlers.gateway_handler import GatewayHandler" not in src, (
            "window.py must not import GatewayHandler"
        )

    def test_send_raw_message_gone(self):
        src = (REPO / "ui/handlers/chat_handler.py").read_text(encoding="utf-8")
        assert "send_raw_message" not in src, (
            "send_raw_message was deleted as dead code (SP2 R2) — its return "
            "here means someone re-added it without a caller"
        )


class TestRepointedSendShape:
    """Source-shape pins: the chat send path routes locally, never via _gw."""

    def test_on_send_routes_via_local_receiver(self):
        src = (REPO / "ui/handlers/chat_handler.py").read_text(encoding="utf-8")
        assert "send_to_special_agent" in src, (
            "on_send must route through AgentRuntimeHandler.send_to_special_agent"
        )
        assert "self._gw" not in src, "chat_handler must not reference _gw post-R1"

    def test_window_wiring_has_no_gw_shims(self):
        src = (REPO / "ui/window.py").read_text(encoding="utf-8")
        assert "set_gateway_client" not in src, (
            "window.py lambda shims must die with SP2 (R3); gateway handler "
            "construction itself is SP3"
        )


# FIX 5 (SP2 audit BUG #5): theDebugger-specified broadened needle set —
# set_gateway_client JOINS the sweep so FIX 1 (connection_sync's 3 deleted
# calls) is pinned at source level. Exclusion list unchanged (SP3 targets).
PIN_NEEDLES = ("_gw\\b", r"\.send_message\(", "GatewayClient", "gateway_client", "set_gateway_client")

import re as _re


class TestBroadenedNeedleSweep:
    """Regex sweep over ui/handlers/*.py with the broadened needle set."""

    # The local receiver API — `rt.send_message(...)` on AgentRuntime — is the
    # post-R1 SEND PATH ITSELF (agent_runtime_handler.py:1134). Debugger's
    # needle would ban the repoint destination, so that exact call shape is
    # the ONLY exemption. Any OTHER `.send_message(` still fails the sweep.
    # FIX 9 (SP2 audit BUG #9): the exemption previously skipped the WHOLE
    # line before needle matching, so a smuggled `conn.send_message(...)` on
    # the same line as an `rt.send_message(...)` evaded the sweep. Now every
    # allowed substring is REMOVED from the line first; needles run on what
    # remains.
    ALLOWED_SEND_SITES = ("rt.send_message(",)

    def test_all_handlers_match_no_needles(self):
        for rel in sorted(REPO.glob("ui/handlers/*.py")):
            src = rel.read_text(encoding="utf-8")
            for line in src.splitlines():
                # Substring removal, NOT a whole-line skip: an allowed call
                # no longer shields anything else on its line.
                remainder = line
                for ok in self.ALLOWED_SEND_SITES:
                    remainder = remainder.replace(ok, "")
                for needle in PIN_NEEDLES:
                    hits = _re.findall(needle, remainder)
                    assert not hits, (
                        f"{rel.name}: needle {needle!r} matched {hits!r} "
                        f"in: {line.strip()[:90]}"
                    )

    def test_chat_handler_has_no_set_gateway_client_attr(self):
        """Debugger's explicit first pin (FIX 1/BUG #1)."""
        from ui.handlers.chat_handler import ChatHandler

        assert not hasattr(ChatHandler, "set_gateway_client")


class TestReceiverSemantics(unittest.TestCase):
    """Behavioral: the receiver (the repoint target) handles unknown keys."""

    def test_remote_key_noops_gracefully(self):
        """Unknown session key → warning + return, no raise (spec §3: MVP has
        no remote agents; the receiver's no-op IS the remote branch)."""
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

        h = AgentRuntimeHandler.__new__(AgentRuntimeHandler)
        h._agents = {}
        h._active_project = None
        h._GLib = None
        with self.assertLogs("ui.handlers.agent_runtime_handler", level="WARNING"):
            h.send_to_special_agent("unknown:session", "text")  # must not raise
