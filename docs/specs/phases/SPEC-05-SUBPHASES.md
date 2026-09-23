# SPEC-05 Sub-Phase Plan — R1 Gateway Strip + transport/ Package

**Spec:** docs/specs/SPEC-05-R1-GATEWAY-STRIP.md (§2 verified against tree 2026-09-22)

## Survey results (2026-09-22, this fork)

- gateway/ package: client.py + __init__.py = 1,064 lines (reconnect/backoff core +
  Ed25519 device-auth + OpenClaw event catalog + agent-manager coupling)
- Handlers to delete: gateway_handler.py, connection_sync_handler.py (+ their 3 test
  files + test_low345_gateway_hardening.py)
- 90 touch-points across 9 UI files (chat_handler 24, window.py 17, agent_command 7,
  review 6, command 6, forward 3, activity 1, agent_list 1 docstring, activity_drawer
  1 docstring) + connection_sync imports
- Send sites: the spec's 11 confirmed; **plus `send_raw_message` (chat_handler :87-96)
  — a 12th site with zero callers found** (verify, then delete as dead code)
- Test blast radius: test_activity_bubbles (61 refs), test_agent_command_handler (33),
  test_chat_render_handler (2), test_agent_runtime (3), test_window_auto_accept_warning (1),
  test_tools (3), test_config (6) — plus the 4 delete-target files
- receiver exists: `send_to_special_agent(sk, text)` (ARH) — repoint target confirmed;
  special/remote branch logic already in place at chat :237-243 pattern

## Sub-phases

### SP1 — transport/ package (dormant) + baseline capture
Create transport/base.py (Transport ABC) + transport/openclaw.py (cleaned connect/
reconnect/backoff/framing core, NO auth handshake, NO event catalog, NO agent-manager
coupling — target ≤400 lines). Gateway stays fully wired (dormant package, zero
callers). Gate: suite green, package imports, ABC complete.

### SP2 — Repoint the 12 send sites + strip `_gw` from handlers
chat_handler (9 sites + send_raw_message disposition), review_handler, forward_handler,
agent_command_handler, command_handler, window.py wiring. Repoint = send_to_special_agent
per the spec's pattern; remote-branch behavior = existing no-op-with-warning. Gate:
`_gw` grep zero in non-gateway handlers; suite green after each file.

### SP3 — Delete gateway + handlers + tests; window.py unwiring; config strip
Delete gateway/, gateway_handler, connection_sync_handler, 4 test files;
window.py GatewayHandler/ConnectionSync construction + set_gateway_client shims;
utils/config.py get_gateway_url/get_identity_dir. Gate: full grep zero in ui/ +
main.py; collection clean.

### SP4 — Connect button rewire + gateway-era test cleanup + full gate
toolbar.py stub transport_on_off (honest "no transport configured"); test_config/
test_tools/test_activity_bubbles/test_agent_command_handler/test_chat_render_handler/
test_agent_runtime/test_window_auto_accept_warning cleanup; models/activity.py
comment-only update. Gate: SPEC-05 §6 acceptance run (xvfb-run standard), ruff,
pyright, final greps.

### SP5 — Close-out
Spec status, ARCHITECTURE.md §transport implemented-note, post-mortem, context.md.

## Rules (unchanged)
Briefs via file; .venv + xvfb-run for GTK-touching runs; probe-before-adjudicate;
falsifiers on fix rounds; zero-new baselines; supervisor owns commits; audit every
code-bearing phase (SP1 retro-miss lesson holds).
