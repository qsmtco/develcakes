# AGENTCTRL1-PHASE-2 — Instructions (Coder)

**PM-approved capability grant.** Phase 2 = `--nudge`: deliver a message to the
**Supervisor only** through GTK's existing GApplication single-instance channel,
routing into the same `send_to_special_agent` path the chat box uses. No new
socket, no daemon, no reconnect loop.

Contract: `docs/specs/SPEC-AGENT-CONTROL-1.md` §3 (esp. §3.2 routing, §3.3
guardrails, §3.4 invariants) + the PM decisions table §11 (reach = Supervisor
only). Supervisor-verified anchors (tree at cb7f2b9):

- `main.py:25` `class CrabcakesApp(Gtk.Application)` — has `__init__` + `on_activate` only. **No `HANDLES_COMMAND_LINE` yet, no `do_command_line`/`on_command_line`.**
- `main.py:53` `def main()` → `app.run(None)` (passes default argv).
- `ui/window.py:186-187` constructs `AgentRuntimeHandler`; `:83` `self._agent_runtime_handler = None` before that.
- `ui/handlers/agent_runtime_handler.py:819` `_get_runtime(name, agent_def=None)` (internal); the public entry is `send_to_special_agent(session_key, text)` (:873).
- Existing convention to mirror: the `/ask @Agent "..."` argument form (`prompts/system/collab.md`).

## EDITS

**Edit A — `main.py`: enable GApplication command-line handling.**
- `CrabcakesApp.__init__`: `self.set_handles_command_line(True)` (the flag that
  routes a second invocation's argv to the RUNNING instance; GTK provides the
  session-bus forwarding — this is the "no socket we own" mechanism).
- Add `self._main_window = None`; in `on_activate` store
  `self._main_window = win` so the command-line handler can reach the handler
  graph.
- Add `def on_command_line(self, app, command_line) -> int:` implementing:
  - `argv = command_line.get_arguments()[1:]`
  - `--nudge @Supervisor "<text>"` (the ONLY accepted target).
  - Returns an exit status int; set it via `command_line.set_exit_status(code)`.
- Keep `main()` as-is (it already passes `None` → default argv).

**Edit B — the nudge entry point (new module `utils/cli_nudge.py` is NOT
needed; put the logic as small module-level functions in `main.py` OR a new
`ui/cli_nudge.py` if you judge it cleaner — your call, but NO new layer and no
handler→handler import).** Required behaviour, mirroring §3.2/§3.3:

| Guardrail | Behaviour |
|---|---|
| Reach | target must be exactly `Supervisor` (case-insensitive on the name, `@` required). `@Coder`, `@Debugger`, `@Auxilium`, unknown → exit **3** naming the permitted target, no dispatch |
| Payload cap | 4096 chars → exit **5** |
| Turn in flight | refuse if the runtime reports RUNNING/STREAMING for `special:supervisor` → exit **2**, message `turn in flight for Supervisor; retry when idle` |
| Session must exist | refuse if the conversation is not loaded/persisted → exit **6** (require explicit `--create` to make one) |
| App must be running | this path only executes inside a running instance; if not remote → exit **4**, never `present()` a GUI |
| Visible origin | the injected message must be distinguishable from a PM-typed message: set the feed card metadata `origin = "cli-nudge"` and render an origin marker (e.g. `via CLI`). The PM must be able to tell at a glance |
| Audit | write an `audit-log.jsonl` record `{origin: "cli-nudge", target, chars, text_sha256[:16], timestamp}` — hash, never the text (content policy §11.3) |
| Delivery | `send_to_special_agent("special:supervisor", text)` — the chat-box path, so turn tokens/`_ended_sessions` behave identically |

Exit codes: `0` delivered · `2` refused (turn in flight) · `3` unknown/unauthorised target · `4` app not running · `5` payload too long · `6` no such session.

Access the handler graph via the window created in `on_activate`
(`self._main_window`) — read `ui/window.py` for the attribute holding
`AgentRuntimeHandler`. Do NOT add a new attribute chain if one already exists.

**Edit C — tests (RED-FIRST).** Per §7 rows for Phase 2:
`test_argv_parse_nudge_forms` (quoting, missing agent, oversized payload),
`test_nudge_refused_for_non_supervisor_agent` (@Coder/@Debugger/@Auxilium/unknown → 3, no dispatch),
`test_nudge_refused_when_turn_in_flight` (fake runtime STREAMING → 2, no dispatch),
`test_nudge_logs_feed_card_and_audit_record` (both artefacts; audit carries hash only),
`test_nudge_card_is_marked_as_cli_origin` (metadata origin + marker; a typed message does NOT have it),
`test_nudge_does_not_start_gui_when_not_remote` (`get_is_remote()` False → 4, no `present()`).
Test the command-line handler in-process (no real second process required):
construct the app, call `on_command_line` with a stub `command_line` object
exposing `get_arguments()`/`set_exit_status()`. Keep GTK suites under xvfb.

## GATES

- RED-first: every §7 row fails before the implementation (paste).
- GREEN: the new suite ×2; plus `tests/test_architecture.py` still 6/6 (no
  handler→handler coupling) and the existing main/window suites green.
- pyflakes /tmp/pf-venv3: 0 undefined on touched files.
- Manual smoke (document the commands you ran): with the app running,
  `python3 main.py --nudge @Supervisor "ping"` → exit 0 and the message appears
  in the Supervisor tab with the `via CLI` marker. If the app is NOT running,
  the same command must exit 3/4 without launching a GUI (state which).
- Full suite → **0F** (baseline is 0F/3639P as of cb7f2b9). Report the count.
- One commit: `feat(cli): --nudge Supervisor-only command-line channel (AGENTCTRL1 Phase 2, PM-approved)`
- Flag (don't fix) anything adjacent.

Then STOP — audit next, then the cron-wiring unit.
