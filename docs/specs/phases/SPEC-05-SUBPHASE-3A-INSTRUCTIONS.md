# SPEC-05 Sub-Phase 3a Instructions — window.py Gateway Unwiring

**Spec:** docs/specs/SPEC-05-R1-GATEWAY-STRIP.md §2 ("window.py GatewayHandler wiring +
set_gateway_client(None) lambda shim + connection_sync construction" — the shims already
died in SP2; THIS phase kills the construction itself)
**Parent plan:** docs/specs/phases/SPEC-05-SUBPHASES.md (as re-carved 2026-09-23)
**PM sizing directive:** this is a MICRO-phase. **Scope: exactly 2 files** —
`ui/window.py` + `tests/test_no_gateway_residuals.py` (pin updates ONLY). Nothing else.
If you find yourself needing a 3rd file, STOP and report — that's a discovery, not a
scope amendment.

**Tool-call budget: ~12 calls total** (edits + 4 gate runs + report). If you'll exceed
it, stop at a clean state and report position — the loop recovers cleanly from precise
state reports (SP2 precedent).

## Pre-verified facts (supervisor grep, 2026-09-23 — trust these)

- window.py is 1,621 lines with exactly **16 gateway refs** (listed below — re-locate
  by pattern, lines drift)
- `gateway_handler` is PASSED to other handlers at :745 and :754 — ForwardHandler takes
  it (for `agent_mgr.get_name()` only, post-SP2) and ConnectionSyncHandler takes it.
  **This phase changes those constructor args**: ForwardHandler's gateway_handler param
  becomes None (it must tolerate None — its own docstring says ARH does the sends now),
  and ConnectionSyncHandler construction is DELETED entirely (dies in SP3b, but its
  construction site dies NOW because its whole job was gateway-connect syncing).
- `agent_list_handler`/`agent_card_handler` comments reference ConnectionSync syncing —
  comment-only updates, sanctioned.
- NO live `from gateway` imports exist outside the SP3b delete set (probe-verified) —
  window.py's `from ui.handlers.gateway_handler import GatewayHandler` (:43) is the
  only import to cut.

## The 16 refs, dispositioned

| Line (approx) | Ref | Disposition |
|---|---|---|
| :13 | header comment "GatewayHandler — owns..." | delete line |
| :18-19 | "gateway callbacks"/"GatewayHandler.dispatch" comment | reword to GLib-dispatch reality |
| :43 | `from ui.handlers.gateway_handler import GatewayHandler` | DELETE (only import) |
| :77 | `self._gateway_handler = None` | delete |
| :142 | "set after gateway connects" comment | reword (AgentManager arrives via ConnectionSync — which is dying; see Ruling 1) |
| :174 | "agent_mgr set in ConnectionSyncHandler.sync()" comment | reword |
| :261 | `self._gateway_handler = GatewayHandler(...)` construction | DELETE (the block, incl. kwargs) |
| :521 | `card.metadata[...] # agent's gateway key` comment | reword "session key" |
| :612/:635 | "synced in/after ConnectionSyncHandler.sync()" comments | reword |
| :745 | `gateway_handler=self._gateway_handler` (ForwardHandler ctor) | pass None |
| :749-754 | ConnectionSyncHandler import + construction | DELETE block |
| :768-769 | `set_sync_callback(self._connection_sync_handler.sync)` | DELETE |
| :1049 region | `gh = self._gateway_handler; if gh.is_connected(): gh.disconnect()` — the Connect-button handler | REWIRE: see Ruling 2 |

## Rulings

**R1 — AgentManager wiring gap (the known consequence).** ConnectionSyncHandler's job
was injecting AgentManager (from the gateway) into agent_list/agent_card/command
handlers on connect. With it gone, those handlers have `agent_manager=None` forever.
In MVP there are no gateway agents, so AgentManager is inert — **accept the gap**:
agents list falls back to the local special-agent registry (verify agent_list_handler
tolerates None mgr; if it doesn't, that's a STOP-and-report). Do NOT build replacement
wiring. Record in the report for SP4/SP5 follow-up.

**R2 — the Connect button handler (:1049 region).** SP3c gives it the transport stub.
THIS phase: replace the `gh.is_connected()/gh.disconnect()` body with an honest
no-op: log INFO "Connect pressed — no transport configured (SPEC-05 SP3c wires the
transport toggle)" and return. Keep the button + handler alive.

**R3 — no tombstones.** Comments describing gateway wiring die with the code; reworded
comments describe what EXISTS.

**R4 — the pin file updates.** (a) Delete `test_excluded_files_still_exist_for_now` —
its own docstring says "SP3 landed? fold the exclusion out" — but note
gateway_handler.py/connection_sync_handler.py FILES still exist until SP3b; instead
update `SP3_DELETE_TARGETS` handling: the sweep now covers window.py properly since
the construction is gone — move `gateway_handler.py` + `connection_sync_handler.py`
OUT of the exclusion only in SP3b when the files die. For NOW: keep exclusions, add
ONE new pin: `test_window_has_no_gateway_construction` — window.py source contains
neither "GatewayHandler(" nor "ConnectionSyncHandler(" nor the :43 import.
(b) Existing 9 pins must stay green (the SP2 sweep covers ui/handlers/*.py — window.py
is ui/, so the broadened needle sweep doesn't cover it; the new pin does).

## Verification (paste ALL, real runs)

```
.venv/bin/python -m pytest tests/test_no_gateway_residuals.py -q        # 10 expected (9+1)
xvfb-run -a .venv/bin/python -m pytest tests/test_window_settings_wiring.py tests/test_window_auto_accept_warning.py -q 2>&1 | tail -1
.venv/bin/python -m pytest tests/ --collect-only -q 2>&1 | tail -1       # 0 errors (import cut cleanly)
.venv/bin/python -m ruff check ui/window.py tests/test_no_gateway_residuals.py
.venv/bin/pyright ui/window.py 2>&1 | tail -1
```

Baselines (pre-existing, ZERO new): window.py ruff 11 (verify + report old→new —
deletions should only drop). pyright window.py: measure first.

## COMPLETENESS
- [ ] All 16 refs dispositioned per table (report any drift)
- [ ] R1 gap verified-tolerated or STOPPED-and-reported
- [ ] R2 Connect handler honest no-op
- [ ] 1 new pin; 10/10 green
- [ ] All 5 outputs pasted
- [ ] Deviations flagged (3rd-file need = STOP)
