# SPEC-12 SP4a — chat_handler: forward_to branch (private view)

**Spec:** `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` §2f
**Pre-flight:** `docs/specs/phases/SPEC-12-PREFLIGHT-DECISIONS.md` (D6)
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** ONE file — `ui/handlers/chat_handler.py` (the `forward_to` branch only).
No test edits here (SP4 tests land after SP4c). No other branch.

Read the file's `on_send` in full first.

---

## The change — `chat_handler.on_send`, the `forward_to` branch (~:216-236)

**Current:** the branch renders the echo via `render_async` and routes the send via
`self._send_local(result.forward_to, result.forward_text)`. `_send_local` calls
`send_to_special_agent(session_key, text)` with NO reply_target → the `/ask` reply
would route to the PROJECT (the BUG#11 defect: `/ask` must go to a private view).

**New:** open a private (agent-keyed) tab for the target and pass
`reply_target=target` so THIS send's reply renders there (R5 REV 3).

Replace the `if result.forward_to and result.forward_text:` block's routing tail:

```python
                if result.forward_to and result.forward_text:
                    target = result.forward_to
                    agent_name = target.split("/")[-1]
                    echo_text = f"→ @{agent_name}: {result.forward_text}"

                    def _show_echo_and_forward():
                        chat_box = self._mc.get_chat_box()
                        if chat_box is not None:
                            if self._chat_render_handler is not None:
                                def _on_bubble(bubble):
                                    if bubble is not None:
                                        chat_box.append(bubble)
                                    self._mc.scroll_chat_to_bottom()
                                self._chat_render_handler.render_async(
                                    "You", echo_text, session_key,
                                    on_bubble_ready=_on_bubble,
                                    on_forward_click=self._on_forward_message,
                                    agent_name="You",
                                )
                        # SPEC-12 §2f (BUG#2/#11/#14): /ask + /delegate open a
                        # PRIVATE (agent-keyed) tab and route THIS send's reply
                        # there. Idempotent: an existing tab is focused, not
                        # recreated.
                        if self._agent_runtime_handler is None:
                            # ARH not wired (early startup): keep the local
                            # fallback so the send is not silently lost.
                            self._send_local(target, result.forward_text)
                            return
                        if self._mc.get_chat_box_for_session(target) is None:
                            # BUG#20c: "special:coder" has no "/", split(...,1)
                            # still yields the whole string — strip the
                            # "special:" prefix for a clean tab label.
                            label = target.split(":", 1)[-1]
                            self._mc.create_chat_tab(target, label)
                        self._agent_runtime_handler.send_to_special_agent(
                            target, result.forward_text, reply_target=target)
                    self._dispatch(_show_echo_and_forward)
```

**Note (BUG#23):** `create_chat_tab` selects the new tab itself
(`main_content.py` calls `set_current_page`) — the spec's "select on create" is
already satisfied; when the tab already exists we deliberately do NOT steal focus.

## What must NOT change
- The `broadcast_targets` branch (separate; SP4b).
- The special-agent branch (~:267) and the group fan-out (~:400) — SP4b.
- `_send_local` (unless you choose to add a `reply_target` kwarg — see SP4b; NOT here).
- The echo's `render_async` call (its own `mount_key` wiring is SP4c).

## Verification (paste outputs)
- `cd /home/mushy/projects/develcakes && xvfb-run -a .venv/bin/python -m pytest tests/test_chat_handler.py -q` → all green (no test asserts this branch's routing yet)
- `~/.local/bin/ruff check ui/handlers/chat_handler.py` → compare to HEAD baseline (measure first)
- `.venv/bin/pyright ui/handlers/chat_handler.py` → compare to HEAD

## Report back (mandatory COMPLETENESS)
```
COMPLETENESS:
- [x/not done] forward_to branch: private tab + reply_target=target — evidence: diff hunk
- [x/not done] none-guard fallback preserved — evidence: diff hunk
- [x/not done] pytest + ruff + pyright (vs HEAD baselines) — pasted
- [x/not done] Related issues found, NOT fixed
```

Invoke `prompts/steelFramedCodeWriter.md` before writing. Please write the change per
this brief and report when done.