# SPEC-05 Post-Mortem — R1 Gateway Strip + transport/ Package

**Date:** 2026-09-23 · **Commits:** plan `ffe90428` → SP1 `ca048abe` → SP2 `46884caf`
(docs) + `54b4d85b` + `e86edcc7` → SP3 `071d6bb0`/`4c9d9224` + `37ea9578` + `50c1af92`/`de0c6684`
→ SP4 `f0945ac6`/`b119734a`/`d5b42430`/`58f4225a` → close-out (this)
**Spec:** docs/specs/SPEC-05-R1-GATEWAY-STRIP.md · **Status:** COMPLETE — 5 sub-phases

## What shipped

**SP1 — transport/ package (dormant at birth):** Transport ABC (base.py) +
WebSocketTransport (openclaw.py, 357 lines) extracted from gateway/client.py's 1,064 —
connect/reconnect-with-backoff, keepalive tick, req/res correlation, redaction kept;
Ed25519 device-auth, OpenClaw event catalog, GLib/UI coupling deleted. Audit round
(7 findings, ALL fixed): the reconnect storm preserved-from-v1 (63 accepts/2s hot loop
on clean server close — now backoff'd + on_disconnect fires), dead public send
correlation (both channels now live), fire-and-forget connect (bounded wait),
send-while-down silent drop (on_error), packaging gap, name-pins → mechanism-pins
(behavioral backoff test via local ws server), dead symbols.

**SP2 — the 12 send sites repointed.** chat (9 + dead send_raw_message), review,
forward, agent_command, command + window wiring → send_to_special_agent; receiver's
no-op IS the remote branch (R1). Fix rounds: connection_sync_handler was the missed
7th caller (live AttributeError silently swallowed — sync() dead at statement 1,
toolbar still said connected; test green was a MagicMock artifact); 115-red test
blast radius updated in-step (10 enumerated deletions); awareness-prefix machinery
RETIRED (supervisor ruling: injected only into remote sends, dead in MVP — future
transports re-implement at the transport layer); _send_local contract; per-member
error isolation + GLib marshaling restored; pin needles broadened then hardened
(evadable whole-line exemption → substring-removal, falsifier-proven).

**SP3 — deletion in 3 micro-phases:** (a) window.py unwiring (16+2 refs; Connect =
honest no-op; AgentManager None-gap ACCEPTED — handlers verified tolerant);
(b) gateway/ + 2 handlers + 4 test files deleted (−2,449 lines, −82 tests; zero live
importers pre-verified; retro-audit ACCEPT); (c) get_gateway_url/get_identity_dir
stripped + toolbar honest ("● No transport").

**SP4 — test cleanup in 3 micro-rounds:** 5 renames, 1 false-docstring fix
(test_command_handler :441 asserted the OPPOSITE of its own body), ~30 comment
rewords, 0 deletions. 12 no-touch files adjudicated by density survey.

## Acceptance criteria (§6) — final state

- [x] All sends route through the local runtime — 11 broadened-needle pins + behavioral
      receiver pins; zero `_gw`/gateway_client/GatewayClient in live code
- [x] transport/ ABC compiles; openclaw.py connect/backoff only (no auth handshake —
      source-pin) — 10 transport pins
- [x] Connect button present, honest "no transport" state (SP3c)
- [x] Gateway grep sweep: zero source matches (unfiltered sweep post-SP3b; window
      pinned separately)
- [x] Full suite green — pin batteries 11 + 9-transport + blast-radius files; the
      xvfb-run standard (SPEC-04) held throughout; 12 pre-existing failures
      (test_mcp_config 9 + test_enforcement 3) unchanged from SPEC-04's gate

## PM sizing directive (mid-spec, 2026-09-23) — validated

Turn/tool limits bit twice early (SP2's 7-file round → 7 findings + 2 fix rounds +
test blast radius; SP1's audit needed 6 probes). After the directive: SP3a/3b/3c/4a/4b/4c
all landed at 6–14 calls, ZERO turn-limit deaths, ZERO fix rounds. The pattern that
worked: pre-dispositioned ref tables, tool-call budgets in briefs, code-vs-tests split,
STOP-on-discovery clauses, and supervisor pre-grepping blast radius before each brief.

## Audit trail: 17 findings across 5 rounds, 0 misreads

SP1: 7 (all fixed). SP2 r1: 7 (all fixed/accepted). SP2 r2: 3 fix-round-introduced
(HIGH unguarded _project_handler — probe-verified crash on the reject path; evadable
exemption; lost guard — all fixed). SP3b retro: ACCEPT + 2 register + 1 rider.
SP4: 0 (clean rounds; Coder caught the supervisor's stale :772 premise).

## Banked register additions

- **pending-RPC expiry is traffic-driven** (v1 semantic inherited by transport/;
  idle connections never time out pending) — relevant when Telegram lands
- **AgentManager: live class, zero production construction sites** (sole instantiator
  died with gateway_handler; 13 tests keep it green) — R1-accepted None-gap; wiring
  point if gateway-sourced agents return
- **on_gateway_event: zero production callers post-SP3a** (SP4 kept ~90 test fixtures
  driving it) — R4/SPEC-07-pending ingestion path, NOT dead; disposition rides the
  feedbar-removal spec
- **knowledge/gateway.md now stale** (SPEC-04 flagged this would happen)
- **test_chat_handler :241** test_noop_when_disconnected name-lie (body asserts
  delivery) — cosmetic rider
- **forward_handler._gateway_handler ctor param** kept (defensive truthiness; source_name
  only) — future cleanup candidate

## SP5 riders (this close-out's commits)

1. **Redaction tests ported** (SP3b retro-audit): the 11 LOW-4 redact cases whose
   file died with test_low345_gateway_hardening.py — the surviving
   `transport.redact_log_preview` gets its behavioral coverage back in
   tests/test_transport_package.py.
2. **pyproject.toml pyright include**: drop the dead `gateway*` entry.

## Next

**SPEC-06 (R2 Phase A — HTML chat surface)** — WebKit chat, nh3 sanitizer, Pango
guard tests. The render pipeline's biggest change; sub-phase carving per the sizing
directive from the start.
