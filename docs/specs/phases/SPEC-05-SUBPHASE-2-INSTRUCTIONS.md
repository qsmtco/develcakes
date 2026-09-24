# SPEC-05 Sub-Phase 2 Instructions — Repoint Send Sites + Strip `_gw` from Handlers

**Spec:** docs/specs/SPEC-05-R1-GATEWAY-STRIP.md §2 (repoint pattern) + §3
**Scope: exactly 6 files** — `ui/handlers/chat_handler.py`,
`ui/handlers/review_handler.py`, `ui/handlers/forward_handler.py`,
`ui/handlers/agent_command_handler.py`, `ui/handlers/command_handler.py`,
`ui/window.py`. NO deletions of gateway/ this phase (SP3), NO transport wiring yet.

## Rulings

**R1 — the repoint pattern.** `self._gw.send_message(sk, text)` →
`self._agent_runtime_handler.send_to_special_agent(sk, text)` (ARH :496). Remote
session keys no-op with warning inside the receiver (spec §3 — MVP has no remote
agents). Where the call site distinguishes special-vs-remote (chat :237-243 pattern),
collapse to the single local call — the receiver's own no-op IS the remote branch now.
**R2 — `send_raw_message` (chat :87-96):** survey found ZERO callers. Delete it as
dead code (spec's routing table doesn't include it; window callbacks use FeedHandler).
Report if grep finds a caller I missed.
**R3 — constructor/ctor plumbing dies with the sites:** `set_gateway_client()`,
`gateway_client` params (chat :34/:49/:85, command :52, review), window.py's
`set_gateway_client(None)` lambda shims. GatewayHandler itself stays until SP3 —
window's construction of it stays this phase (only the SHIMS to other handlers go).
**R4 — comment discipline:** no tombstones. Send-path comments describing gateway
routing die with the code; routing comments now name the local path.
**R5 — broadcast/multi-target sites (chat :467/:496):** loop bodies keep their
per-target structure; only the transport call inside changes. Don't refactor loops.
**R6 — is_connected() guards:** the `self._gw is not None and self._gw.is_connected()`
conditions die with the site (the local path is always "connected" — the receiver
handles agent-not-found itself). Don't preserve gateway-availability gating.

## Per-file work

1. `chat_handler.py` — 9 send sites + send_raw_message (R2) + ctor/setter plumbing.
   Keep the special-vs-else structure where it exists (R1) — else branch becomes the
   local call or a warning-log + no-op, matching receiver behavior.
2. `review_handler.py` — :573 site + gateway_client ctor/param strip.
3. `forward_handler.py` — indirect via chat_handler; strip its _gw reference.
4. `agent_command_handler.py` — :503 site + ctor arg (:52 area).
5. `command_handler.py` — ctor param + any _gw references (6 grep hits).
6. `window.py` — set_gateway_client(None) lambda shims + wiring args die; gateway
   handler construction STAYS (SP3).

## Tests

Add to tests/test_no_kb_residuals.py? NO — new file `tests/test_no_gateway_residuals.py`
(the pattern: one durable pin file per removal spec):
1. `test_source_tree_free_of_gw_sends` — grep ui/ (excluding gateway_handler.py +
   connection_sync_handler.py + window.py wiring, which SP3 deletes) for
   `_gw.send_message|gateway_client` → zero.
2. `test_send_raw_message_gone` — chat_handler source lacks send_raw_message.
3. `test_send_reaches_special_agent` — behavioral: stub ARH, call the repointed
   chat path, assert send_to_special_agent received (sk, text).
4. `test_remote_key_noops_gracefully` — behavioral: unknown session key → warning
   logged, no raise (receiver semantics, pinned through the repointed path).

## Verification (paste ALL, real runs)

```
.venv/bin/python -m pytest tests/test_no_gateway_residuals.py -q
.venv/bin/python -m pytest tests/test_chat_render_handler.py tests/test_agent_command_handler.py tests/test_window_auto_accept_warning.py -q 2>&1 | tail -1
.venv/bin/python -m pytest tests/ --collect-only -q 2>&1 | tail -1
.venv/bin/python -m ruff check ui/handlers/chat_handler.py ui/handlers/review_handler.py ui/handlers/forward_handler.py ui/handlers/agent_command_handler.py ui/handlers/command_handler.py ui/window.py tests/test_no_gateway_residuals.py
.venv/bin/pyright ui/handlers/chat_handler.py ui/window.py 2>&1 | tail -1
```

Measure + report each file's ruff/pyright BEFORE editing (chat_handler, window.py
measure; ARH untouched this phase). Zero new; drops expected.

## COMPLETENESS
- [ ] 12 sites dispositioned (repointed/deleted-with-reason, R2)
- [ ] R1–R6 each addressed
- [ ] 4 pin tests green
- [ ] All 5 outputs + baselines
- [ ] Deviations flagged (especially any caller found for send_raw_message)
