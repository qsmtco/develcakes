# SPEC-05 Sub-Phase 1 Instructions — transport/ Package (dormant)

**Spec:** docs/specs/SPEC-05-R1-GATEWAY-STRIP.md §2 "transport/ (NEW)" + §1
**Parent plan:** docs/specs/phases/SPEC-05-SUBPHASES.md
**Scope: exactly 2 NEW files + 1 test file.** Gateway stays fully wired — this package
is DORMANT (zero callers). SP2 does the repointing; SP3 deletes gateway/.

## Task 1 — transport/base.py

```python
class Transport(ABC):
    """Connection lifecycle contract for remote transports (Telegram post-MVP)."""
    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def disconnect(self) -> None: ...
    @abstractmethod
    async def send(self, payload: dict) -> None: ...
    @abstractmethod
    def status_signals(self) -> tuple: ...  # (on_connect, on_disconnect, on_error)
```
Module docstring: cite SPEC-05, decision #1 (transport core retained for Telegram
reuse), architecture ruling #7. No concrete methods beyond the ABC.

## Task 2 — transport/openclaw.py

Extract from gateway/client.py (READ IT FULLY FIRST — 1,064 lines with the handler
coupling). KEEP: websocket connect, reconnect-with-backoff loop, message framing,
gracious shutdown, the connection-state machinery. DELETE: Ed25519 device-auth
handshake, OpenClaw event catalog, agent-manager coupling, `_gw` handler wiring.
Target ≤400 lines. The class should implement Transport (import from .base) — if
the kept core needs small interface adaptations, make them and document each.
If a "cleaned core" extraction turns out to be impossible without dragging auth in
(entanglement), STOP and report — do not ship a half-cleaned file claiming compliance.

## Task 3 — tests/test_transport_package.py (NEW, ~6 tests)

1. `test_transport_abc_cannot_instantiate`
2. `test_openclaw_implements_transport` (issubclass)
3. `test_openclaw_has_no_auth_handshake` — source-text pin: "ed25519"/"device_auth"/
   "signing" absent from transport/openclaw.py (grep-as-test, SPEC-04 pattern)
4. `test_openclaw_has_backoff` — reconnect/backoff machinery present (pin a marker:
   the backoff constant/function name)
5. `test_package_imports_clean` — import transport.base + transport.openclaw in a
   fresh subprocess (timeout=30)
6. `test_line_budget` — transport/openclaw.py ≤400 lines (spec's target, pin it)

## Verification (paste ALL, real runs)

```
.venv/bin/python -m pytest tests/test_transport_package.py -q
.venv/bin/python -m pytest tests/ --collect-only -q 2>&1 | tail -1
.venv/bin/python -m pytest tests/test_gateway.py tests/test_gateway_handler.py -q 2>&1 | tail -1    # gateway UNTOUCHED this phase
.venv/bin/python -m ruff check transport/ tests/test_transport_package.py
.venv/bin/pyright transport/ 2>&1 | tail -1
```

Baselines: NEW files — ruff 0, pyright 0 required (greenfield, zero-excuse).
Gateway files untouched: their counts unchanged.

## COMPLETENESS
- [ ] base.py + openclaw.py (line counts reported)
- [ ] Extraction report: what was kept/deleted from client.py (function inventory)
- [ ] 6 pin tests
- [ ] All 5 outputs
- [ ] Deviations flagged (especially any interface adaptations)
