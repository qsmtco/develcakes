# SPEC-12 Sub-phases (Supervisor plan, 2026-10-05)

Spec: `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md`
Decisions: `docs/specs/phases/SPEC-12-PREFLIGHT-DECISIONS.md`
Loop: `prompts/implementationLoop.md` (supervisor + coder + debugger trio)

Ordering follows spec §5. One file per phase; SP4 (chat_handler integration) is
sub-phased because it carries ≥3 edits + ordering risk.

| # | Phase | File(s) | Change | Depends on |
|---|-------|---------|--------|-----------|
| SP1 | Grouped agent boxes | `ui/views/chat_surface.py`, `tests/test_chat_surface.py` | `_document` groups consecutive same-agent rows under one `.agent-box`; CSS rule; user-row `role-user` box class | — |
| SP2 | Display-keyed surfaces | `ui/handlers/chat_render_handler.py`, `tests/test_chat_render_handler.py`, `tests/test_welcome_html.py`, `tests/test_activity_pill_adapter.py` | full respec `_surface_for`/`_append_to_surface`/`close_session`/`render_welcome`/eviction; display-key tombstones; `render_async(mount_key=)` | SP1 |
| SP3 | Turn-scoped reply target | `ui/handlers/agent_runtime_handler.py`, `tests/test_agent_runtime.py` | `_turn_reply_target` slot; `send_to_special_agent(reply_target=)`; `_reply_key` helper; R7 resolvers; terminal clear; 11 mount sites | SP2 |
| SP4a | chat_handler: forward_to branch | `ui/handlers/chat_handler.py`, `tests/test_chat_handler.py` | `forward_to` branch → private tab + `reply_target=target` (BUG#14 None-guard, BUG#20c label) | SP3 |
| SP4b | chat_handler: group fan-out + special-agent branch | `ui/handlers/chat_handler.py`, `tests/test_chat_handler.py` | group fan-out `reply_target=project:<name>`; special-agent branch `reply_target` per BUG#19 | SP4a |
| SP4c | chat_handler: render_async call sites | `ui/handlers/chat_handler.py`, `tests/test_chat_handler.py` | 6 `render_async` call sites pass `mount_key=` (BUG#4+#10) | SP4b |
| SP5 | No auto-open; Chat→project | `ui/window.py`, `tests/*window*` | delete auto-open block + unused import; `_on_agent_selected` → project tab (R3) | SP3 |
| SP6 | Forward handler | `ui/handlers/forward_handler.py`, `tests/test_forward_handler.py` | tab-first ordering; `reply_target=target` | SP3 |
| SP7 | Close-out | ARCHITECTURE.md, post-mortem, battery | architecture §Modules/Chat surface + §Data Flow/Render revisions; full suite; post-mortem | all |

## Notes
- SP2 is the highest-risk phase (key-domain migration across 5 structures + streaming
  split). Sub-phase internally if the diff exceeds ~80 lines: 2a surface cache/tombstone,
  2b render_async threading, 2c test rewrites.
- SP4 is integration — if the builder fails once, the supervisor fixes it directly
  (implementationSupervisor §Anti-Patterns).
- Every phase delegation references `prompts/steelFramedCodeWriter.md`; every audit
  references `prompts/adversarialDebugger.md`.