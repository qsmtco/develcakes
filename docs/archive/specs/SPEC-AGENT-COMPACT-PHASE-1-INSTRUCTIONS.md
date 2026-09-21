# PHASE 1 — Agent-Issued /compact and /clear

**Spec:** `docs/specs/SPEC-AGENT-ISSUED-COMPACT-CLEAR.md`
**File:** `ui/handlers/agent_command_handler.py`

---

## EDIT 1 — Add 'compact' to Pass 1 and Pass 2 filters

**Pass 1 (around line 95):**
```python
# Before:
if cmd not in ('ask', 'tell', 'delegate', 'stop'):
# After:
if cmd not in ('ask', 'tell', 'delegate', 'stop', 'compact'):
```

**Pass 2 (around line 122):**
```python
# Before:
if cmd not in ('ask', 'tell', 'delegate'):
# After:
if cmd not in ('ask', 'tell', 'delegate', 'compact'):
```

---

## EDIT 2 — Add Pass 6 for payload-free /compact and /clear

**Insert after the Pass 5 block (around line 165):**

```python
    # ── Pass 6: payload-free /compact and /clear (no quotes at all) ────────
    for m in re.finditer(r'(?:^|\s)/(compact|clear)\s+(@[^\s"]+)', text):
        if m.span() in seen_spans:
            continue
        _emit(m.group(1), m.group(2), '', m.span())
```

---

## EDIT 3 — Add response_text dispatch branch

**In the `if parsed_commands:` block in `on_agent_response`, after the broadcast_targets branch (around line 345), add:**

```python
                elif result.handled and result.response_text:
                    # Action result (e.g. /compact, /clear) — no forward target.
                    # Inject the result into the issuing agent's own conversation
                    # so the supervisor sees the outcome on its next turn.
                    self._record_action_result(session_key, result.response_text)
                    command_count += 1
```

---

## EDIT 4 — Add _record_action_result method

**Add after `on_agent_response` method (around line 350):**

```python
    def _record_action_result(self, source_sk: str, text: str) -> None:
        """Inject an action result into the issuing agent's own conversation.

        Used for agent-issued commands that mutate peer state (e.g. /compact,
        /clear) and return a response_text result with no forward target.
        The supervisor's next turn will include this text in its LLM context.
        """
        if self._agent_runtime_handler is None:
            logger.warning(
                "[agent-cmd] Cannot record action result for %s — "
                "agent_runtime_handler not wired",
                source_sk,
            )
            return

        agent_def = self._agent_runtime_handler.get_special_agent_def(source_sk)
        if agent_def is None:
            logger.debug(
                "[agent-cmd] _record_action_result: no SpecialAgentDef for %s",
                source_sk,
            )
            return

        try:
            rt = self._agent_runtime_handler._get_runtime(
                agent_def.display_name, agent_def=agent_def
            )
        except Exception as exc:
            logger.warning(
                "[agent-cmd] _record_action_result: failed to get runtime for %s: %s",
                source_sk, exc,
            )
            return

        try:
            conv = rt.get_conversation(source_sk)
        except Exception as exc:
            logger.warning(
                "[agent-cmd] _record_action_result: get_conversation failed for %s: %s",
                source_sk, exc,
            )
            return

        if conv is None:
            logger.debug(
                "[agent-cmd] _record_action_result: no conversation for %s",
                source_sk,
            )
            return

        try:
            with rt._lock:
                conv.add_user_message(f"[Action result]: {text}")
        except Exception as exc:
            logger.warning(
                "[agent-cmd] _record_action_result: add_user_message failed for %s: %s",
                source_sk, exc,
            )
```

---

## EDIT 5 — Add 5 tests

**File:** `tests/test_agent_command_handler.py` — append at end.

```python
class TestAgentIssuedCompactClear:
    """Tests for agent-issued /compact and /clear commands."""

    def test_extract_compact_with_quoted_focus(self):
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('text /compact @Coder "preserve auth" more')
        assert len(cmds) >= 1
        match = [c for c in cmds if c.command == "compact"]
        assert len(match) == 1
        assert match[0].agent == "@Coder"
        assert match[0].payload == "preserve auth"

    def test_extract_clear_payload_free(self):
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('text /clear @Coder more')
        assert len(cmds) >= 1
        match = [c for c in cmds if c.command == "clear"]
        assert len(match) == 1
        assert match[0].agent == "@Coder"
        assert match[0].payload == ""

    def test_extract_compact_payload_free(self):
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('text /compact @Coder more')
        assert len(cmds) >= 1
        match = [c for c in cmds if c.command == "compact"]
        assert len(match) == 1
        assert match[0].agent == "@Coder"
        assert match[0].payload == ""

    def test_ask_still_works(self):
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('/ask @Coder "what is the status?"')
        assert len(cmds) >= 1
        match = [c for c in cmds if c.command == "ask"]
        assert len(match) == 1

    def test_on_agent_response_no_crash_when_runtime_unwired(self):
        """When agent_runtime_handler is None, /compact still works (action fires)
        but feedback injection is skipped gracefully."""
        from ui.handlers.agent_command_handler import AgentCommandHandler
        handler = AgentCommandHandler(GLib_module=None)
        handler.set_command_handler(None)  # no command handler = scanner skips
        # Should not raise
        handler.on_agent_response("special:supervisor", "no commands here", None)
```

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- Read `ui/handlers/agent_command_handler.py` in full before editing.
- Do NOT touch any other file except the test file.

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. Syntax
python3 -c "import ast; ast.parse(open('ui/handlers/agent_command_handler.py').read()); print('SYNTAX OK')"

# 2. compact in filters
grep -n "'compact'" ui/handlers/agent_command_handler.py

# 3. Pass 6 exists
grep -n "Pass 6\|compact|clear" ui/handlers/agent_command_handler.py

# 4. _record_action_result exists
grep -n "_record_action_result" ui/handlers/agent_command_handler.py

# 5. response_text branch
grep -n "response_text" ui/handlers/agent_command_handler.py | grep -v "def \|return\|#"

# 6. New tests
python3 -m pytest tests/test_agent_command_handler.py -v -k "CompactClear or compact_clear"

# 7. All tests
python3 -m pytest tests/test_agent_command_handler.py -q
```
