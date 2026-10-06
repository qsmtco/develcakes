# SPEC-12 SP4b+SP4c — chat_handler: fan-out reply_target, special-agent branch, render_async mount_key

**Spec:** `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` §2e, §2f (BUG#19), §2b (BUG#4+#10)
**Pre-flight:** `docs/specs/phases/SPEC-12-PREFLIGHT-DECISIONS.md` (D6)
**Base:** `docs/specs/phases/SPEC-12-SP4A-INSTRUCTIONS.md` (forward_to branch done)
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** ONE file — `ui/handlers/chat_handler.py` + `tests/test_chat_handler.py`
(only for the send-path assertions). Read the file first.

---

## Part B — send-path reply targets

### B1 — `_send_local` gains a `reply_target` kwarg

```python
    def _send_local(self, session_key: str, text: str,
                    reply_target: str | None = None) -> None:
        ...
        self._agent_runtime_handler.send_to_special_agent(
            session_key, text, reply_target=reply_target)
```
Default None → unchanged behavior for every existing caller.

### B2 — group fan-out sets `reply_target=project:<name>` (BUG#11 core)

In the `else:` (group broadcast) branch's loop:
```python
                    for member in members:
                        # SPEC-12 BUG#11: member replies land in the PROJECT
                        # surface (one group chat), not per-agent surfaces.
                        self._send_local(
                            member, text, reply_target=f"project:{project_name}")
```

### B3 — solo DM branch: same project target (one group surface)

The `if solo_target:` branch also targets the project surface (the solo DM just
narrows the SEND fan-out; the reply still renders in the group surface):
```python
                    self._send_local(
                        solo_target, text, reply_target=f"project:{project_name}")
```

### B4 — special-agent branch (BUG#19): private tab sends stay private

The branch at ~:284: when the tab is agent-keyed (a private view) pass
`reply_target=session_key`; a project-tab special-agent send (no direct tab)
passes None so it routes to the project. Replace the tail
`self._send_local(session_key, text)`:

```python
            # BUG#19: typing in an open private (agent-keyed) tab stays
            # private; a group send reaches the project branch below.
            reply_target = (
                session_key
                if self._mc.get_chat_box_for_session(session_key) is not None
                else None
            )

            def _show_and_route_to_agent():
                ...  # unchanged echo render_async
                self._send_local(session_key, text, reply_target=reply_target)
```

### B5 — non-project send branch (~:434): KEEP (R6)

`self._send_local(session_key, text)` stays (it is the private view's send path).
No change.

---

## Part C — the 6 `render_async` call sites gain `mount_key`

`render_async` gained `mount_key` in SP2a. Thread it at ALL 6 sites (spec §2b
BUG#4+#10 — threading a param no caller sets is not a fix). The sites are the
"echo" renders (the "You" row) at ~:230, :267, :295, :344, :372, :399.

**Rule:** the echoed "You" row renders in the SAME surface as the send it
accompanies. Pass:
- **project-tab sites** (group/solo/broadcast echoes, session_key starts
  `project:`) → `mount_key=session_key` (already the display key).
- **special-agent private-tab site** (~:295) → `mount_key=session_key` (display
  key = the agent key when the tab is agent-keyed; the send's reply_target logic
  B4 mirrors this).
- **forward_to echo** (~:230) → `mount_key=result.forward_to` (the private tab
  the reply renders in — matches the SP4a send target).

Implement as `mount_key=<the same key the accompanying send targets>`. Do NOT
add a `mount_key` that the send does not use, or the echo and reply diverge.

**Docstring guard:** add to `render_async`'s docstring (SP2a may already note it):
*"pass mount_key for any non-project caller; the surface cache is display-keyed."*

---

## Tests (append/extend in `tests/test_chat_handler.py`, RED-first)

1. `test_group_send_passes_project_reply_target` — a `project:alpha` tab with 2
   members → each `send_to_special_agent` called with
   `reply_target="project:alpha"`; use the fake's `get_reply_targets()`.
2. `test_special_agent_private_tab_send_stays_private` — a tab whose key is a
   special agent AND `get_chat_box_for_session(session_key)` is not None →
   `reply_target == session_key`; when it IS None → `reply_target is None`.
3. `test_render_async_call_sites_pass_mount_key` — assert the echo
   `render_async` for a project send is called with `mount_key` == the project
   key (via the render-handler MagicMock's call kwargs).

## Verification battery (paste outputs)
- `cd /home/mushy/projects/develcakes && xvfb-run -a .venv/bin/python -m pytest tests/test_chat_handler.py -q` → all green
- `~/.local/bin/ruff check ui/handlers/chat_handler.py tests/test_chat_handler.py` → ≤ baseline (9 / 23)
- `.venv/bin/pyright ui/handlers/chat_handler.py` → 0

## Report back (mandatory COMPLETENESS)
```
COMPLETENESS:
- [x/not done] B1: _send_local reply_target kwarg — evidence: diff hunk
- [x/not done] B2/B3: group + solo fan-out reply_target — evidence: diff hunks
- [x/not done] B4: special-agent branch reply_target (BUG#19) — evidence: diff hunk
- [x/not done] C: 6 render_async mount_key sites — evidence: grep + diff hunks
- [x/not done] 3 tests appended — evidence: pytest
- [x/not done] ruff / pyright — pasted
- [x/not done] Related issues found, NOT fixed
```

Invoke `prompts/steelFramedCodeWriter.md` before writing. Please write the change
per this brief and report when done.