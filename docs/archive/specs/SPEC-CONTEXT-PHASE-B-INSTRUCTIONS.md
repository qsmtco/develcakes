# PHASE B — /compact Slash Command

**Spec:** `docs/specs/SPEC-CONTEXT-UI-COMPACT-LLM-2026-07-10.md` §3.2
**Files to change:** `ui/handlers/project_handler.py`, `ui/handlers/command_handler.py`, `ui/handlers/agent_runtime_handler.py`, `ui/window.py`, `agent/runtime.py`

---

## EDIT 1 — project_handler.py: cmd_compact + setters + init

**File:** `ui/handlers/project_handler.py`

### Step A: Add init state (near `self._clear_callback = None`, around line 77)

```python
        self._compact_callback: Callable[[str, str], dict] | None = None
        self._compact_chat_callback: Callable[[str, dict], None] | None = None
```

### Step B: Add cmd_compact method (after cmd_clear, around line 730)

Mirror cmd_clear's structure. Validates session prefix, dispatches to `self._compact_callback(sk, focus_text)` for special: sessions. On success, fires `self._compact_chat_callback(sk, result)` for UI bubble. Returns CommandResult with removed/freed counts.

Read the spec §3.2.1 for exact code. The method signature is:
```python
    def cmd_compact(self, cmd: Command, session_key: str | None = None) -> CommandResult:
```

### Step C: Add setter methods

```python
    def set_compact_callback(self, fn: Callable[[str, str], dict] | None) -> None:
        self._compact_callback = fn

    def set_compact_chat_callback(self, fn: Callable[[str, dict], None] | None) -> None:
        self._compact_chat_callback = fn
```

---

## EDIT 2 — command_handler.py: register /compact

**File:** `ui/handlers/command_handler.py`

After the `/clear` registration (around line 165), add:

```python
            if hasattr(project_handler, "cmd_compact"):
                self.register_command("compact", project_handler.cmd_compact,
                    help_text="Compact conversation: /compact [focus-instructions]")
```

---

## EDIT 3 — agent_runtime_handler.py: compact_conversation method

**File:** `ui/handlers/agent_runtime_handler.py`

Add after `clear_conversation` (around line 415). Mirrors clear_conversation's pattern:

1. Validate session_key starts with "special:"
2. Get agent_def from self._agents
3. Get runtime via self._get_runtime(agent_def.display_name, agent_def=agent_def)
4. Get conversation from runtime
5. Compute hard_ceiling via rt._compute_compaction_threshold(conv)
6. target_budget = max(4_000, hard_ceiling // 2)
7. Check getattr(agent_def, "compaction_strategy", "textual") — if "llm", call rt.force_llm_compact (Phase C, not yet implemented — use getattr guard)
8. Call rt._context_strategy.compact(conv, target_budget)
9. Save via `from agent.runtime import _save_conversation_to_disk; _save_conversation_to_disk(conv, session_key)`
10. Read rt._context_strategy.last_result, return dict with messages_removed, tokens_freed, summary_chars, layer

Read spec §3.2.3 for exact code. Return `{"messages_removed": 0, "tokens_freed": 0, "summary_chars": 0, "layer": 0}` on any failure.

**IMPORTANT:** Use `_save_conversation_to_disk` (module-level function at runtime.py:1284), NOT `_save_conversation_now` (which doesn't exist — BUG #1 from spec audit).

---

## EDIT 4 — window.py: wire /compact callbacks

**File:** `ui/window.py`

After the `/clear` wiring (around line 628), add:

```python
        self._project_handler.set_compact_callback(
            self._agent_runtime_handler.compact_conversation
        )
        self._project_handler.set_compact_chat_callback(
            lambda sk, result: self._show_compact_bubble(sk, result)
        )
```

Add `_show_compact_bubble` method after `_clear_chat_box` (around line 856):

```python
    def _show_compact_bubble(self, session_key: str, result: dict) -> None:
        chat_box = self._main_content.get_chat_box_for_session(session_key)
        if chat_box is None:
            return
        removed = int(result.get("messages_removed", 0))
        freed = int(result.get("tokens_freed", 0))
        text = f"🧹 Compacted. Removed {removed} message{'s' if removed != 1 else ''}, freed ~{freed:,} tokens."
        bubble = self._chat_render_handler.render_sync("Agent", text, session_key, agent_name=None)
        if bubble is not None:
            chat_box.append(bubble)
            self._main_content.scroll_chat_to_bottom()
```

---

## EDIT 5 — agent/runtime.py: force_compact wrapper

**File:** `agent/runtime.py`

Add a thin public method on AgentRuntime:

```python
    def force_compact(self, conv: "Conversation", token_budget: int) -> None:
        """Public wrapper around self._context_strategy.compact()."""
        self._context_strategy.compact(conv, token_budget)
```

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- Read each file before editing.
- Read the spec §3.2 for exact code.
- Do NOT implement Phase C (LLM strategy) — just the getattr guard for "llm".

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. Syntax
python3 -c "import ast; [ast.parse(open(f).read()) for f in ['ui/handlers/project_handler.py', 'ui/handlers/command_handler.py', 'ui/handlers/agent_runtime_handler.py', 'ui/window.py', 'agent/runtime.py']]; print('SYNTAX OK')"

# 2. cmd_compact exists
grep -n "def cmd_compact" ui/handlers/project_handler.py

# 3. /compact registered
grep -n '"compact"' ui/handlers/command_handler.py

# 4. compact_conversation exists
grep -n "def compact_conversation" ui/handlers/agent_runtime_handler.py

# 5. Window wiring
grep -n "set_compact_callback\|_show_compact_bubble" ui/window.py

# 6. force_compact exists
grep -n "def force_compact" agent/runtime.py

# 7. Uses _save_conversation_to_disk (not _save_conversation_now)
grep -n "_save_conversation" ui/handlers/agent_runtime_handler.py | grep compact
```
