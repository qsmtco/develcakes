# SPEC-05: R1 — OpenClaw Gateway Strip + `transport/` Package

**Date:** 2026-09-20
**Author:** Supervisor (develcakes v2)
**Status:** Draft — for implementation
**Implements:** docs/proposals/DEVELCAKES-V2-CHANGE-LIST.md §5 R1 (with [SUP-REV] notes)
**Depends on:** SPEC-04 (R5 before R1 — gateway work must not touch dying Auxilium files)
**Target branch:** main

> Architecture compliance: new `transport/` package (PM ruling #7); runtime becomes the
> only send path; Connect button survives as transport on/off.

---

## 1. Overview

**Problem.** Eleven `send_message` call sites route through the OpenClaw gateway client
(`self._gw.send_message`). The gateway package, its two handlers, and Ed25519 identity
paths exist only to serve it.

**Solution.**
1. Create `transport/base.py` (Transport ABC) + `transport/openclaw.py` (cleaned core
   from gateway/client.py: connection lifecycle, reconnect/backoff, framing — minus
   OpenClaw device-auth handshake, event catalog, `_gw` handler wiring).
2. Repoint all send sites to `agent/runtime.send_message` (the local path
   agent_runtime_handler.py already demonstrates).
3. Delete `gateway/`, `gateway_handler.py`, `connection_sync_handler.py`; strip `_gw`
   from chat/review/forward/agent_command handlers; Connect button rewires to transport
   on/off (inert in MVP — no gateway behind it).

**Verified send sites (grep, 2026-09-20):**
chat_handler.py :95, :243, :272, :383, :418, :467, :496, :499 (8);
review_handler.py :573 (1); agent_command_handler.py :503 (1). forward_handler.py :168
routes via chat_handler's `_gw` reference (1 indirect) — total **11 sites, 6 files**
(window.py wiring + command_handler ctor arg also touched).

## 2. Changes by File

### transport/ (NEW)

`transport/base.py`:

```python
class Transport(ABC):
    """Connection lifecycle contract for remote transports (TG post-MVP)."""
    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def disconnect(self) -> None: ...
    @abstractmethod
    async def send(self, payload: dict) -> None: ...
    @abstractmethod
    def status_signals(self) -> tuple: ...  # (on_connect, on_disconnect, on_error)
```

`transport/openclaw.py`: extracted from gateway/client.py (26,257 bytes) — keep
websocket connect/reconnect/backoff loop, message framing, graceful shutdown. Delete:
Ed25519 device-auth handshake, OpenClaw event catalog, agent-manager coupling. Target
≤400 lines.

### Repoint pattern (chat_handler.py :95 exemplar)

```python
# before
self._gw.send_message(session_key, text)
# after
self._agent_runtime_handler.send_to_special_agent(session_key, text)
```

Verified receiver exists: `send_to_special_agent(self, session_key: str, text: str)`
(agent_runtime_handler.py, "Public: send a message to a special agent"). For remote
(non-special) session keys the local path is a no-op with a warning (existing behavior
when agent_def is None) — in MVP there are no remote agents, by definition.

Also strip: `set_gateway_client()`, `gateway_client` ctor params (chat_handler,
command_handler :52, review_handler), `window.py` GatewayHandler wiring +
`set_gateway_client(None)` lambda shim + connection_sync construction.

### DELETE

gateway/client.py, gateway/__init__.py, ui/handlers/gateway_handler.py,
ui/handlers/connection_sync_handler.py, tests/test_gateway.py,
test_gateway_handler.py, test_connection_sync_handler.py,
test_low345_gateway_hardening.py. Partial edits: test_config.py (drop
get_gateway_url/get_identity_dir cases), test_tools.py, test_kb_lookup.py (already
deleted by SPEC-04).

### utils/config.py

Remove `get_gateway_url()` + `get_identity_dir()` (verified present, lines ~60-75).

### ui/toolbar.py — Connect button

Widget + `● Connecting`/`● Connected` state machine **survives**; action rewires to a
stub `transport_on_off` (logs "no transport configured" in MVP; future Telegram switch).

## 3. Data Flow

Post-R1 send: input → ChatHandler.on_send → `send_to_special_agent(sk, text)` →
runtime.send_message → provider stream → chat surface + feed + transcript append. No
gateway object exists anywhere in the graph.

## 4. File Change Summary

| Change | Files | ~Lines | Risk |
|---|---|---|---|
| transport/ package | 2 new | +450 | med |
| Send-site repoints | 6 handlers | ~80 edits | med-high (routing correctness) |
| Deletions | 4 src + 4 test | −1,800 | low |
| utils/config.py strip | 1 | −20 | low |

## 5. Implementation Order

1. `transport/base.py` + `transport/openclaw.py` extracted (suite green with gateway
   still wired — new package dormant).
2. Repoint the 11 sites file-by-file (chat → review → forward → agent_command), suite
   after each.
3. Delete gateway package + handlers + their tests; fix window.py wiring.
4. Connect-button rewire + config strip.
5. `grep -rn "_gw\b\|gateway_client\|GatewayHandler\|ConnectionSync" --include="*.py"`
   → zero in ui/ + main.py.
6. Full suite + ruff + pyright.

## 6. Acceptance Criteria

- [ ] All sends route through the local runtime; chat works with no OpenClaw anywhere
- [ ] `transport/` ABC compiles; openclaw.py carries connect/backoff only (no auth handshake)
- [ ] Connect button present, toggles, honest "no transport" state
- [ ] Gateway grep sweep returns zero source matches
- [ ] Full pytest green (minus 4 deleted test files), ruff clean, pyright clean

## 7. Edge Cases

| Case | Behavior |
|---|---|
| Message addressed to a remote-agent session key | Local path no-ops with warning (MVP has no remote agents) |
| Connect pressed in MVP | Status cycles to "no transport configured"; no crash |
| Persisted conversations from gateway era | Load unchanged (no gateway fields consulted) |
| `models/activity.py` event catalog comments | Update doc-comment to "locally defined" (comment-only) |

## 8. ARCHITECTURE.md Updates

§Modules/transport — mark implemented; §Data Flow already reflects post-R1 shape.
