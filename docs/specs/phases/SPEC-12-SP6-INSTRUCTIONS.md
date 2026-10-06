# SPEC-12 SP6 — forward_handler: tab-first ordering + reply_target

**Spec:** `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` §2f (BUG#28)
**Pre-flight:** `docs/specs/phases/SPEC-12-PREFLIGHT-DECISIONS.md`
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** ONE file — `ui/handlers/forward_handler.py` (+ `tests/test_forward_handler.py`
for the send-shape asserts). Read the file first.

---

## Edit 1 — reorder: tab create/select BEFORE the send (BUG#28)

**Current order:** `send_to_special_agent(...)` runs at the top of the routing
block, THEN the tab is created/selected (later in the method). Per spec §2f BUG#28,
create/select the tab FIRST so the private tab exists when the reply is produced.

Move the "Check if target agent already has an open tab" block to BEFORE the
`is_special` routing block. New order:

```python
        # SPEC-12 BUG#28: create/select the target's private tab FIRST, so the
        # reply (produced by the send below) has a live box to render into.
        target_tab_exists = None
        for page_idx, sk in self._main_content._tab_sessions.items():
            if sk == target_session_key:
                target_tab_exists = page_idx
                break

        is_special = (...)
        if is_special:
            target_name = self._agent_runtime_handler.get_special_agents()[target_session_key]
            target_tab_exists = self._ensure_target_tab(
                target_session_key, target_name, target_tab_exists)
            self._agent_runtime_handler.send_to_special_agent(
                target_session_key, text, reply_target=target_session_key)
        else:
            target_name = (...)
            target_tab_exists = self._ensure_target_tab(
                target_session_key, target_name, target_tab_exists)
            self._agent_runtime_handler.send_to_special_agent(
                target_session_key, text, reply_target=target_session_key)

        # Append forwarded bubble to the target tab (unchanged).
        chat_box = self._main_content.get_chat_box(target_tab_exists)
        ...
```

Where `_ensure_target_tab` is a small private helper (avoids duplicating the
create-or-select block across both branches):

```python
    def _ensure_target_tab(self, session_key, name, existing_page):
        """Create the private tab if absent (else select it); return its page."""
        if existing_page is None:
            existing_page = self._main_content.create_chat_tab(session_key, name)
        else:
            self._main_content._chat_notebook.set_current_page(existing_page)
        return existing_page
```

## Edit 2 — BOTH send sites pass `reply_target=target_session_key`

Per spec §2f: the forwarded message is a PRIVATE (agent-keyed) send — its reply
renders in the target's own tab, not the project surface:
```python
            self._agent_runtime_handler.send_to_special_agent(
                target_session_key, text, reply_target=target_session_key)
```
(the `is_special` branch AND the else branch).

## What must NOT change
- `show_forward_popover` (popover construction).
- The forwarded-bubble `render_sync` call + GLib scroll deferral.
- The ARH-None guard (FIX 11).
- `source_name` resolution.

## Tests (`tests/test_forward_handler.py`, RED-first)
1. `test_forward_send_passes_reply_target` — the target's
   `send_to_special_agent` is called with `reply_target=target_session_key`.
2. `test_forward_tab_created_before_send` — assert the ordering: the tab is
   created/selected before the send (e.g. record call order via a shared list,
   or assert `create_chat_tab` is called then `send_to_special_agent`).
3. `test_forward_existing_tab_selected_not_recreated` — an existing target tab
   is selected (`set_current_page`), not recreated.

## Verification (paste outputs)
- `cd /home/mushy/projects/develcakes && xvfb-run -a .venv/bin/python -m pytest tests/test_forward_handler.py -q` → all green
- `~/.local/bin/ruff check ui/handlers/forward_handler.py tests/test_forward_handler.py` → vs HEAD baseline
- `.venv/bin/pyright ui/handlers/forward_handler.py` → vs HEAD

## Report back (mandatory COMPLETENESS)
```
COMPLETENESS:
- [x/not done] Edit 1: tab-first reorder + _ensure_target_tab — evidence: diff hunk
- [x/not done] Edit 2: reply_target on both send sites — evidence: diff hunks
- [x/not done] 3 tests — evidence: pytest
- [x/not done] ruff / pyright vs HEAD — pasted
- [x/not done] Related issues found, NOT fixed
```

Invoke `prompts/steelFramedCodeWriter.md` before writing. Please write the change per
this brief and report when done.