# SPEC-65: `/ask` Command Parser, Prompt Alignment, Registry Sync & Relay Hardening

**Date:** 2026-11-14
**Author:** Supervisor / Diagnostics (per PM direction)
**Status:** Draft — for implementation
**Implements:** `docs/audits/2026-11-14-ASK-COMMAND-STREAMING-INVESTIGATION-READ-ONLY.md` Findings **2A, 2B, 2C, 3A, 3B, 3C, 3D** (companion spec `docs/specs/SPEC-64-TURN-TEXT-AND-STREAMING-HARDENING.md` covers Findings **1A, 1B, 1C, 4A, 4B** with zero file overlap)
**Depends on:** None — zero file overlap with `SPEC-64`; may land before, after, or in parallel worktrees with `SPEC-64`
**Target branch:** main
**Spec authoring prompt:** `.crabcakes/prompts/steelFramedSpecWriter.md` (Rules 1–10 applied; discovery block + self-audit below)

> Architecture compliance: Respects the strict §8.6 handler-isolation rule (`docs/ARCHITECTURE.md` §3.21a, §3.21e, §3.21f): `AgentCommandHandler` never imports from `ui/handlers/` and receives collaborators solely via setters wired in `ui/window.py`; `models/command.py` remains pure Python with zero `ui/` or GTK imports; `CollabHandler` remains pure pass-through. `_pending_asks: dict[str, str]` retains its `{target_sk: source_sk}` shape for full backward compatibility with all existing callers and tests.

---

## DISCOVERY

Starting Spec Discovery — reading all referenced source files before writing any spec content.

- Read `ui/handlers/agent_command_handler.py` (`ParsedCommand` `:50`, `_extract_quoted_commands` `:53-173`, `AgentCommandHandler` `:178-621`):
  - Verified `_extract_quoted_commands(text: str, command_names: set[str] | None = None) -> list[ParsedCommand]`.
  - Passes 1–4 require `(?:^|\s)/` immediately before the command name (`:86, 113, 131, 148`), so inline backticks (`` `/ask ...` ``), bold (`**/ask ...**`), list bullets (`-/ask ...`), and blockquotes (`>/ask ...`) extract `0` commands.
  - Pass 1 (`:86`) omits `re.DOTALL` (so `\\.` fails on `\\\n`), rejects curly quotes (`“...”`) and single quotes (`'...'`), stops early on unescaped inner double quotes (`"check "foo" in bar.py"`), rejects `/a` alias (`:93` hardcodes `('ask', 'tell', 'delegate', 'stop', 'compact', 'clear')`), rejects em-dash `/ask @Debugger — "..."`, and captures trailing punctuation on `@Agent:` (`@[^\s"]+` → `@Debugger:`).
  - Pass 1 runs before Pass 5 (`/stop`) and Pass 6 (`/compact`, `/clear`) and returns `results` **unsorted** (`:173`), executing `/clear @Debugger\n/ask @Debugger "..."` backwards (`/ask` first, `/clear` second).
  - `_strip_fenced_blocks` (`:615-621`) runs `re.sub(r"```.*?```", "", text, flags=re.DOTALL)`, stripping WebKit ```` ```html ```` and ```` ```live ```` cards (`prompts/system/crabcakes-context.md:16-24`), while `<think>...</think>` blocks are **never** stripped at all.
  - `on_agent_response` (`:247-341`): when `self._command_handler` is set (always `True` in production via `ui/window.py:806`) and `parsed_commands` is empty, `self._chain_depth.pop(session_key, None)` is **never** called (masked because `tests/test_agent_command_handler.py:396-420` omitted `set_command_handler`). Also note self-ask (`Coder` → `@Coder` in `test_multiple_commands_capped` `:493`): `session_key` must ONLY pop `_chain_depth` when `session_key not in routed_targets`, otherwise a self-ask loop would reset its own depth every turn.
  - Verified `_pending_asks` key/value contract across 16 assertions in `tests/test_agent_command_handler.py` (`:124, 188-189, 232, 249-250, 266, 287, 290, 299, 324, 341, 394, 561, 575, 591`) and `_resolve_target_role` (`:591-595`): tests directly read and assign `handler._pending_asks["agent:debugger"] = "agent:coder"`. Therefore `_pending_asks` MUST remain `dict[str, str]`; multi-asker queueing and stranded-turn protection must layer alongside it without changing its type.
- Read `ui/handlers/command_handler.py` (`__init__` `:50-125`, `get_command_names` `:176-182`, `process_input` `:243-389`, `_resolve_mention` `:457-523`):
  - `get_command_names` (`:182`) returns `set(self._registry.list_commands())`, omitting aliases (`"a"`).
  - `process_input` (`:274, 318`) runs `tokens = raw.split()` then `rest_text = " ".join(args_after_mentions)`, flattening all `\n`, `\t`, and indentation inside quoted payloads.
  - `_resolve_mention` (`:469`) sets `name = mention[1:]` without stripping trailing `[:,;]`, causing `@Debugger:` to fail with `"Unknown agent: @Debugger:"`.
- Read `models/command.py` (`Command`, `CommandResult`, `CommandRegistry` `:1-191`) and `ui/handlers/collab_handler.py` (`:1-50`):
  - `CommandRegistry` owns `_commands` and `_aliases` (`alias -> canonical`), exposing `list_commands()` (`:150`) and `list_aliases()` (`:154`).
  - `CollabHandler.cmd_ask` (`:42`) and `CommandHandler.__init__` (`:84`) still show pre-quoted syntax `/ask @agent — question` in help/usage text.
- Read `prompts/system/collab.md` (`:1-64`) and `prompts/system/crabcakes-commands.md` (`:1-23`):
  - Every inline `/ask` example in `collab.md` (`:25-27, 35, 54, 63`) is wrapped in single backticks (`` `/ask ...` ``).
  - `crabcakes-commands.md` (`:3, 7-16`) still instructs `` `— text` for a freeform body ``, `#task` refs, and deleted review commands (`/review`, `/check`, `/accept`, `/reject`), and lists `Alias /s` on `/status` (actual alias in `command_handler.py:93` is `/st`; `/s` is `/session` at `:122`).
- Read `ui/window.py` (`_wire_agents` `:258-264`, `_build` `:736-814`, `_on_agent_response` `:1633-1648`, `_on_providers_changed` `:2279-2299`):
  - `self._command_handler.set_special_agents(...)` is called **only once** at `:737`. Neither `on_agent_saved`/`on_agent_deleted` (`:259-264`) nor `_on_providers_changed` (`:2286-2299`) refreshes `CommandHandler._special_agents`.
- Read `ui/handlers/agent_runtime_handler.py` (`:145, 272-279`), `ui/agent_runtime/session.py` (`clear_conversation` `:15-131`, `compact_conversation` `:133-240`, `send_to_special_agent` `:394-521`), `ui/agent_runtime/turn_ui.py` (`_do_response_complete` `:530-610`, `_do_error` `:612-794`), and `ui/handlers/project_handler.py` (`cmd_clear` `:761-841`, `cmd_compact` `:843-920`):
  - Note: tests (`tests/test_compact_command.py:102`, `tests/test_spec62_fresh_install.py:21`) instantiate `AgentRuntimeHandler.__new__(AgentRuntimeHandler)` without calling `__init__`. Any new attribute read on `self` in `RuntimeSessionMixin` or `RuntimeTurnUiMixin` **must** use `getattr(self, ..., None)` so `__new__` test doubles never raise `AttributeError`.
  - In `clear_conversation` (`session.py:52, 75`), `self._turn_attr.pop(session_key, None)` runs **before** `if rt.is_loop_active(session_key): return False`, wiping active-turn attribution on a refused clear.
  - In `compact_conversation` (`session.py:133-180`), `if not protect_open_turn and rt.is_loop_active(session_key):` is missing (unlike `edit_conversation` at `:321` and `clear_conversation` at `:75`).
  - In `_do_response_complete` (`turn_ui.py:533, 584-592`), `on_agent_response` runs **before** `_flush_stranded_sends(session_key)` (`:610`), so an inbound `"[Supervisor asks]: ..."` sitting in `rt._inbox` or `rt._stranded` has its `_pending_asks` entry prematurely popped by the finishing prior turn; and `[room]` witness lines (`:584-592`) duplicate-send to targets already addressed via `/ask` in the same turn.
- Architecture owners: `AgentCommandHandler` (`ui/handlers/agent_command_handler.py`) owns A2A scanning, `_pending_asks`, and `_chain_depth`; `CommandHandler` (`ui/handlers/command_handler.py`) owns slash-command parsing and `@mention` resolution; `ui/window.py` is the composition root keeping registries synchronized; `prompts/system/*` owns system prompt templates.

---

## 1. Overview

### Problem
Seven defects in the A2A command/relay layer cause `/ask` to fail across models and multi-machine setups:
- **2A:** `prompts/system/collab.md` wraps every `/ask` example in inline backticks or triple-backtick fences, which `_extract_quoted_commands` (`(?:^|\s)/`) and `_strip_fenced_blocks` (including WebKit ```` ```html ```` / ```` ```live ```` cards) drop 100% of the time.
- **2B:** `<think>...</think>` reasoning blocks streamed in `content` are never stripped before scanning, causing draft commands inside `<think>` to execute, exhaust the 3-command cap, or corrupt post-`</think>` quotes.
- **2C:** Six syntax/prompt traps silently drop or corrupt `/ask`: unescaped inner quotes (`"check "foo" in bar.py"`), curly/single quotes, em-dash `/ask @Debugger — "..."` (taught by `crabcakes-commands.md`), `@Debugger:` trailing punctuation, `/a` alias (taught by `crabcakes-commands.md` but rejected by `_extract_quoted_commands`), and `\\\n` + newline/indentation flattening in `CommandHandler.process_input`.
- **3A:** `CommandHandler._special_agents` is snapshotted once at boot (`window.py:737`) and never refreshed when providers or agents are saved on a second machine.
- **3B:** `_pending_asks` is prematurely popped by an already-running Debugger turn when `"[Supervisor asks]: ..."` is stranded in `_inbox`/`_stranded` awaiting `_flush_stranded_sends`, leaks on turn errors, overwrites concurrent askers, and collides with `[room]` witness broadcasts.
- **3C:** `_chain_depth` is never cleared when `CommandHandler` is wired and `parsed_commands` is empty, eventually hitting `_MAX_CHAIN_DEPTH = 3` and dropping future `/ask` commands.
- **3D:** `_extract_quoted_commands` returns `/ask` before `/clear` / `/compact` (unsorted across passes), breaking mandatory `/clear @Debugger` + `/ask @Debugger` pairs; `clear_conversation` pops `_turn_attr` before its active-loop check; and `compact_conversation` lacks an `is_loop_active` guard for peer compaction.

### Solution Summary
1. **Sanitizer + escape- & inner-quote-aware scanner in `AgentCommandHandler` (2A, 2B, 2C, 3D):**
   - Strip `<think>...</think>` (and truncated trailing `<think>...$`) before both audit extraction and command scanning.
   - Unwrap WebKit ```` ```html ```` / ```` ```live ```` fences (after stripping inner `<script>`, `<style>`, `<pre>`, `<code>` blocks) before stripping code fences.
   - Accept inline Markdown/list/quote prefixes (`` ` ``, `*`, `_`, `(`, `[`, leading `-`/`+`/`>`), optional `[:,;]` and `—` after `@Agent`, curly/single quotes, `\\\n`, `/a` → `ask` alias normalization, and single-line unescaped inner quotes; deduplicate overlapping spans; and sort `ParsedCommand` results by `raw_start`.
2. **Exact-slice payload parsing & alias inclusion in `CommandHandler` (2C):**
   - Include `list_aliases()` in `get_command_names()`.
   - Strip trailing `[:,;]` in `_resolve_mention`.
   - Parse quoted payloads directly from the unsplit `raw` command string in `process_input` so newlines and indentation survive intact.
3. **Prompt & help-text alignment (2A, 2C):**
   - Remove inline backticks around `/ask` examples in `prompts/system/collab.md` and document plain-line ASCII double-quote syntax.
   - Fix `prompts/system/crabcakes-commands.md`, `collab_handler.py`, `command_handler.py`, and `models/command.py` to remove stale `—` body claims and deleted commands.
4. **Live registry sync, turn-safe `/clear`+`/compact`, and relay hardening (3A, 3B, 3C, 3D):**
   - Add `Window._sync_special_agent_consumers()` wired into `on_agent_saved`, `on_agent_deleted`, and `_on_providers_changed`.
   - Guard `_pending_asks` pop when `has_pending_ask_inbound(session_key)` is `True` (leaving the pending ask for the stranded `[... asks]:` turn about to flush), maintain a companion FIFO waiter list for concurrent askers while keeping `_pending_asks` as `dict[str, str]`, clean up stranded `_pending_asks` on turn failure via `on_agent_error`, and skip duplicate `[room]` witness lines for targets routed via `/ask` in the same turn.
   - Clear `self._chain_depth.pop(session_key, None)` at the end of `on_agent_response` whenever `session_key not in routed_targets`, and fix the two unwired tests in `tests/test_agent_command_handler.py`.
   - Guard `compact_conversation` with `if not protect_open_turn and rt.is_loop_active(session_key):` and move `_turn_attr.pop` below `rt.is_loop_active` in `clear_conversation`.

### Scope

| In Scope | Out of Scope |
|---|---|
| `ui/handlers/agent_command_handler.py`, `command_handler.py`, `collab_handler.py`, `project_handler.py` | Replacing `/ask` slash scraping with an LLM tool-call schema |
| `ui/handlers/agent_runtime_handler.py`, `ui/agent_runtime/session.py`, `ui/agent_runtime/turn_ui.py`, `ui/window.py` | `agent/runtime.py` and `agent/llm/*` (covered in `SPEC-64`) |
| `models/command.py`, `prompts/system/collab.md`, `prompts/system/crabcakes-commands.md` | Re-introducing removed `/work` or `/review` command families |
| `tests/test_agent_command_handler.py`, `tests/test_spec65_ask_command_hardening.py`, SPEC-47 rows `R-10`, `R-11` | Changing `_pending_asks` dict value type (stays `dict[str, str]`) |

---

## 2. Changes by File

### 2.1 `models/command.py` (~12 lines changed)

Add `resolve_name(self, name: str) -> str | None` to `CommandRegistry` (after `get` at `:148`) and update stale `—` comment on `Command.body` (`:58`):

```python
    def resolve_name(self, name: str) -> str | None:
        """Return canonical command name for a registered command or alias, or None."""
        name_lower = name.lower()
        if name_lower in self._commands:
            return name_lower
        if name_lower in self._aliases:
            return self._aliases[name_lower]
        return None
```

---

### 2.2 `ui/handlers/collab_handler.py` (~4 lines changed)

Update `cmd_ask` docstring and missing-target usage text (`:38-42`) from `/ask @agent — question` to `/ask @agent "question"`:
```python
    def cmd_ask(self, cmd: Command) -> CommandResult:
        """/ask @agent "question" → forward question to agent (or all if `@`)"""
        if cmd.is_broadcast:
            return CommandResult(handled=True, broadcast_targets=cmd.broadcast_targets, forward_text=cmd.body)
        if not cmd.target_session_key:
            return CommandResult(handled=True, response_text='No target agent. Usage: /ask @agent "question"')
        return CommandResult(handled=True, forward_to=cmd.target_session_key, forward_text=cmd.body)
```

---

### 2.3 `ui/handlers/command_handler.py` (~35 lines changed)

1. **`__init__` (`:83-84`):** Update `/ask` `help_text` from `"Ask an agent a question: /ask @agent — question"` to `'Ask an agent a question: /ask @agent "question"'`.
2. **`get_command_names(self) -> set[str]` (`:176-182`):** Include registered aliases so `/a` passes `AgentCommandHandler`'s `command_names` membership check:
   ```python
   def get_command_names(self) -> set[str]:
       """Return registered command names and aliases as a set."""
       return set(self._registry.list_commands()) | set(self._registry.list_aliases().keys())
   ```
3. **`process_input` (`:273-348`):** Preserve multiline whitespace and indentation inside quoted payloads by reading from the unsplit `raw` string at the exact token span of the opening quote:
   ```python
   started_with_at = tokens[0].startswith("@")
   ...
   if after_ws and after_ws[0] == '"':
       raw_spans = list(re.finditer(r"\S+", raw))
       quote_tok_idx = (0 if started_with_at else 1) + len(mentions_from_rest)
       if quote_tok_idx < len(raw_spans) and raw[raw_spans[quote_tok_idx].start()] == '"':
           raw_after_ws = raw[raw_spans[quote_tok_idx].start():]
       else:
           raw_after_ws = after_ws
       payload, q_pos = _parse_quoted_payload(raw_after_ws, 0)
       if payload is None:
           if len(raw_after_ws) >= 2 and raw_after_ws[1] == '"':
               error_msg = 'Empty payload — provide a message: /' + cmd_name + ' @Agent "your message"'
           else:
               error_msg = 'Unclosed quote — missing closing ": /' + cmd_name + ' @Agent "your message"'
       else:
           body = payload
           if len(body) > _PAYLOAD_MAX_CHARS:
               body = body[:_PAYLOAD_MAX_CHARS] + '…'
   ```
4. **`_resolve_mention` (`:469`):** Strip trailing separator punctuation `[:,;]` on `@name` (while keeping bare `@` intact for broadcast):
   ```python
   raw_name = mention[1:]
   name = raw_name.rstrip(":,;") if raw_name else ""
   if raw_name and not name:
       return CommandResult(handled=True, response_text=f"Unknown agent: {mention}")
   ```

---

### 2.4 `ui/handlers/agent_command_handler.py` (~180 lines changed/added)

1. **Sanitizers (`_strip_thinking_blocks` and `_sanitize_for_command_scan`):**
   - Keep `@staticmethod def _strip_fenced_blocks(text: str) -> str` (`:615-621`) unchanged (`re.sub(r"```.*?```", "", text, flags=re.DOTALL)`) so external callers and audit-report tests keep exact compatibility.
   - Add:
     ```python
     @staticmethod
     def _strip_thinking_blocks(text: str) -> str:
         """Remove <think>...</think> reasoning blocks (and unclosed trailing <think>)."""
         text = re.sub(r"<think\b[^>]*>.*?</think>", "\n", text, flags=re.DOTALL | re.IGNORECASE)
         return re.sub(r"<think\b[^>]*>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)

     @classmethod
     def _sanitize_for_command_scan(cls, text: str) -> str:
         """Strip <think> blocks, unwrap WebKit html/live fences (excluding code/pre/script/style), then strip code fences."""
         text = cls._strip_thinking_blocks(text)

         def _unwrap_html_fence(m: re.Match) -> str:
             body = m.group(1)
             body = re.sub(
                 r"<(script|style|pre|code)\b[^>]*>.*?</\1>",
                 " ",
                 body,
                 flags=re.DOTALL | re.IGNORECASE,
             )
             body = re.sub(r"<[^>]+>", "\n", body)
             return "\n" + body.replace("&quot;", '"').replace("&#34;", '"') + "\n"

         text = re.sub(
             r"```(?:html|live)[ \t]*\r?\n(.*?)```",
             _unwrap_html_fence,
             text,
             flags=re.DOTALL | re.IGNORECASE,
         )
         return cls._strip_fenced_blocks(text)
     ```
   - In `_process_audit_reports` (`:532`), change `clean_text = self._strip_fenced_blocks(text)` to `clean_text = self._strip_fenced_blocks(self._strip_thinking_blocks(text))`.

2. **Escape-, smart-quote-, and inner-quote-aware scanner + sorted `_extract_quoted_commands`:**
   ```python
   _CMD_PREFIX_RE = r"(?:^|\s|(?:(?<=^)|(?<=\n))[ \t]*[-+>][ \t]*)[`*_(\[]*/"
   _AGENT_TOKEN_RE = r"(@[^\s\"`\u201c\u201d'*_)\]:;,—–]+)[:;,]*\s+(?:[—–-]+\s+)?"
   _CLOSING_FOLLOWERS = frozenset("`*_).,:;!?]")
   _NEXT_CMD_RE = re.compile(
       r"\s+[`*_(\[]*/(?:ask|a|tell|delegate|stop|compact|clear|@[^\s\"`]+)\b",
       re.IGNORECASE,
   )


   def _scan_agent_quoted_payload(
       text: str, open_pos: int, *, wrapped_backtick: bool = False
   ) -> tuple[str | None, int, bool]:
       """Scan a quoted A2A payload starting at ``open_pos``.

       Supports ASCII ``"..."``, typographic ``“...”``, single ``'...'``,
       ``\\"`` and ``\\\\`` escapes, ``\\\\\\n`` continuations, single-line
       unescaped inner quotes (e.g. ``"check "foo" in bar.py"``), and agent
       end-of-string auto-close.
       Returns ``(payload_or_none, end_pos, was_closed)``.
       """
       n = len(text)
       if open_pos >= n:
           return None, open_pos, False
       open_ch = text[open_pos]
       close_map = {'"': '"', "\u201c": "\u201d", "\u201d": "\u201d", "'": "'"}
       if open_ch not in close_map:
           return None, open_pos, False
       close_ch = close_map[open_ch]
       close_set = {close_ch, '"'} if open_ch in ("\u201c", "\u201d") else {close_ch}

       nl_pos = text.find("\n", open_pos + 1)
       line_limit = nl_pos if nl_pos != -1 else n
       next_cmd = _NEXT_CMD_RE.search(text, open_pos + 1, line_limit)
       seg_limit = next_cmd.start() if next_cmd else line_limit

       def _remaining_unescaped_closes(from_idx: int, limit_idx: int) -> int:
           cnt = 0
           k = from_idx
           while k < limit_idx:
               if text[k] == "\\" and k + 1 < limit_idx:
                   k += 2
                   continue
               if text[k] in close_set:
                   cnt += 1
               k += 1
           return cnt

       parts: list[str] = []
       i = open_pos + 1
       inner_depth = 0
       while i < n:
           ch = text[i]
           if ch == "\\" and i + 1 < n:
               nxt = text[i + 1]
               if nxt in close_set or nxt == '"':
                   parts.append(nxt)
                   i += 2
                   continue
               if nxt == "\\":
                   parts.append("\\")
                   i += 2
                   continue
               parts.append("\\")
               i += 1
               continue
           if ch in close_set:
               if inner_depth > 0:
                   inner_depth -= 1
                   parts.append(ch)
                   i += 1
                   continue
               if (
                   i < seg_limit
                   and i + 1 < seg_limit
                   and not text[i + 1].isspace()
                   and text[i + 1] not in _CLOSING_FOLLOWERS
                   and _remaining_unescaped_closes(i + 1, seg_limit) >= 2
               ):
                   inner_depth += 1
                   parts.append(ch)
                   i += 1
                   continue
               payload = "".join(parts)
               if not payload:
                   return None, open_pos, True
               return payload, i + 1, True
           parts.append(ch)
           i += 1

       payload = "".join(parts)
       if wrapped_backtick and payload.endswith("`"):
           payload = payload[:-1]
       if not payload:
           return None, open_pos, False
       return payload, n, False
   ```
   In `_extract_quoted_commands(text: str, command_names: set[str] | None = None) -> list[ParsedCommand]`:
   - Run `clean = AgentCommandHandler._sanitize_for_command_scan(text)` at entry (idempotent when `on_agent_response` also passes raw/clean text).
   - Replace exact-tuple `span in seen_spans` with interval-overlap deduplication (`not (e <= s0 or s >= e0)`), preventing any payload-free or nested substring inside a quoted payload from matching in a later pass.
   - Normalize `cmd == "a"` → `"ask"` when `command_names is None or "a" in command_names or "ask" in command_names`.
   - Sort `results.sort(key=lambda pc: pc.raw_start)` before returning (`Finding 3D` fix: `/clear @Debugger\n/ask @Debugger "..."` always yields `clear` before `ask`).

3. **Relay queue, stranded-turn guard, `_chain_depth` cleanup, and `on_agent_error` (`AgentCommandHandler`):**
   - Keep `self._pending_asks: dict[str, str] = {}` as `{target_sk: head_source_sk}`; add `self._pending_ask_waiters: dict[str, list[str]] = {}` for any additional concurrent askers targeting the same `target_sk`.
   - Add helper `_has_unstarted_ask_for(self, session_key: str) -> bool`:
     ```python
     def _has_unstarted_ask_for(self, session_key: str) -> bool:
         arh = self._agent_runtime_handler
         checker = getattr(arh, "has_pending_ask_inbound", None)
         if callable(checker):
             try:
                 return bool(checker(session_key))
             except Exception:
                 logger.debug("has_pending_ask_inbound check failed for %s", session_key, exc_info=True)
         return False
     ```
   - Add `_pop_next_asker(self, session_key: str) -> str | None` and `_push_pending_ask(self, target_sk: str, source_sk: str) -> None`:
     - `_push_pending_ask`: if `target_sk not in self._pending_asks`, set `self._pending_asks[target_sk] = source_sk`; else if `source_sk != self._pending_asks[target_sk]` and `source_sk not in waiters`, append `source_sk` to `self._pending_ask_waiters.setdefault(target_sk, [])`.
     - `_pop_next_asker`: pops `head = self._pending_asks.pop(session_key, None)`; if `self._pending_ask_waiters.get(session_key)` is non-empty, promotes `waiters.pop(0)` into `self._pending_asks[session_key]` (and deletes empty list).
   - In `on_agent_response(self, session_key: str, text: str, project_name: str | None) -> set[str]`:
     - Return `set[str]` of resolved target session keys routed during Step 2 (`set()` on early return or when no commands forward).
     - **Step 1 (Relay):** Only pop and relay when `not self._has_unstarted_ask_for(session_key)`! If `_has_unstarted_ask_for(session_key)` is `True` (an inbound `"[... asks]: "` missed the finishing loop and is about to start via `_flush_stranded_sends`), leave `_pending_asks[session_key]` intact for that next turn.
     - **Step 2 (Scan):** Track `routed_targets: set[str] = set()`. Have `_route_command` return `route_key: str` and add it to `routed_targets` (and add `broadcast_targets` when broadcast fires). On `elif result.handled and result.response_text:`, log `logger.warning("[agent-cmd] Command %r from %s returned non-forward message: %s", candidate, session_key, result.response_text)` in addition to `self._record_action_result(session_key, result.response_text)`.
     - **End of `on_agent_response` (`Finding 3C` fix):**
       ```python
       if session_key not in routed_targets:
           self._chain_depth.pop(session_key, None)
       return routed_targets
       ```
   - Add `on_agent_error(self, session_key: str, error_text: str) -> None`:
     ```python
     def on_agent_error(self, session_key: str, error_text: str) -> None:
         """Clear zombie pending-ask / chain-depth state when a target turn fails."""
         if self._has_unstarted_ask_for(session_key):
             return
         self._chain_depth.pop(session_key, None)
         source_sk = self._pop_next_asker(session_key)
         if source_sk is not None:
             display_name = self._resolve_display_name(session_key)
             logger.warning(
                 "[agent-cmd] Clearing pending ask %s -> %s after target turn error: %s",
                 source_sk, session_key, error_text,
             )
             self._record_action_result(
                 source_sk,
                 f"{display_name} encountered an error before answering: {error_text}",
             )
     ```

---

### 2.5 `ui/handlers/agent_runtime_handler.py`, `ui/agent_runtime/session.py`, `ui/agent_runtime/turn_ui.py`, & `ui/handlers/project_handler.py` (~60 lines changed/added)

1. **`ui/handlers/agent_runtime_handler.py` (`:145, 278`):**
   - Initialize `self._on_agent_error_cb: Callable[[str, str], None] | None = None` in `__init__`.
   - Add setter `def set_on_agent_error(self, cb: Callable[[str, str], None] | None) -> None: self._on_agent_error_cb = cb`.

2. **`ui/agent_runtime/session.py`:**
   - **`clear_conversation` (`:52, 75-81` — Finding 3D):** Move `self._turn_attr.pop(session_key, None)` **after** `if rt.is_loop_active(session_key): return False` so a refused clear never deletes active-turn attribution.
   - **`compact_conversation` (`:177` — Finding 3D):** Immediately after acquiring `rt`, guard peer compaction identically to `edit_conversation` (`:321`):
     ```python
     if not protect_open_turn and rt.is_loop_active(session_key):
         _mod().logger.warning(
             "compact_conversation: refusing compact for %s — tool loop is active; retry after it completes",
             session_key,
         )
         return {
             "messages_removed": 0,
             "tokens_freed": 0,
             "summary_chars": 0,
             "layer": 0,
             "error": "tool loop is active",
         }
     ```
   - **Add `has_pending_ask_inbound(self, session_key: str) -> bool` (read-only, never spawns a runtime):**
     ```python
     def has_pending_ask_inbound(self, session_key: str) -> bool:
         """True if an unstarted '[... asks]: ' message is queued in _inbox or _stranded."""
         agents = getattr(self, "_agents", None) or {}
         runtimes = getattr(self, "_runtimes", None) or {}
         agent_def = agents.get(session_key)
         if agent_def is None:
             return False
         rt = runtimes.get(agent_def.display_name)
         if rt is None:
             return False
         peek = getattr(rt, "peek_pending_inbound", None)
         if callable(peek):
             pending = peek(session_key)
         elif hasattr(rt, "_lock"):
             with rt._lock:
                 pending = (
                     list(getattr(rt, "_inbox", {}).get(session_key, ()))
                     + list(getattr(rt, "_stranded", {}).get(session_key, ()))
                 )
         else:
             pending = []
         return any(isinstance(m, str) and " asks]: " in m for m in pending)
     ```

3. **`ui/handlers/project_handler.py` (`cmd_compact`, `:887-896`):**
   If `result.get("error")` is truthy, return a refusal `CommandResult` mirroring `cmd_clear` (`:829-835`):
   ```python
   if result.get("error"):
       return CommandResult(
           handled=True,
           response_text=(
               f"Could not compact {agent_name}: a tool loop is currently running. "
               f"Wait for it to finish, then run /compact again."
           ),
       )
   ```

4. **`ui/agent_runtime/turn_ui.py` (`_do_response_complete` `:530-592`, `_do_error` `:771`):**
   - In `_do_response_complete` (`:530-534`), capture `routed_targets`:
     ```python
     routed_targets: set[str] = set()
     if self._on_agent_response is not None and text:
         project_name = self._active_project[0] if self._active_project else None
         hook_res = self._on_agent_response(session_key, text, project_name)
         if isinstance(hook_res, (set, frozenset)):
             routed_targets = set(hook_res)
     ```
     And in the `[room]` witness loop (`:584-586`), skip members already directly routed via `/ask` in this turn:
     ```python
     for member in members:
         if member == session_key or member in routed_targets:
             continue
     ```
   - In `_do_error` (before lifecycle end at `:771`), notify `_on_agent_error_cb` using `getattr` (safe on `__new__` test doubles):
     ```python
     err_cb = getattr(self, "_on_agent_error_cb", None)
     if callable(err_cb):
         try:
             err_cb(session_key, display_msg)
         except Exception:
             _mod().logger.exception("on_agent_error callback failed for %s", session_key)
     ```

---

### 2.6 `ui/window.py` (~25 lines changed/added)

1. Add `_sync_special_agent_consumers(self) -> None` (near `_on_providers_changed` at `:2279`):
   ```python
   def _sync_special_agent_consumers(self) -> None:
       """Keep LeftPanel and CommandHandler special-agent maps synchronized with ARH."""
       arh = getattr(self, "_agent_runtime_handler", None)
       if arh is None:
           return
       left_panel = getattr(self, "_left_panel", None)
       set_lp = getattr(left_panel, "set_special_agents", None)
       if callable(set_lp):
           set_lp(arh)
       cmd_handler = getattr(self, "_command_handler", None)
       set_ch = getattr(cmd_handler, "set_special_agents", None)
       if callable(set_ch):
           set_ch(arh.get_special_agents())
   ```
2. Wire `self._sync_special_agent_consumers` into:
   - `on_agent_saved` and `on_agent_deleted` (`:259-264`), replacing the `left_panel`-only lambdas.
   - `_on_providers_changed` (`:2296-2298`), replacing the `left_panel`-only block so provider saves on a fresh/second machine immediately update `CommandHandler._special_agents` (`Finding 3A`).
3. Wire `self._agent_runtime_handler.set_on_agent_error(self._agent_command_handler.on_agent_error)` at `:814` (right after `set_on_agent_response`).
4. Update `_on_agent_response` (`:1633-1648`) to return the `routed_targets` set from `self._agent_command_handler.on_agent_response(session_key, text, project_name)`.

---

### 2.7 `prompts/system/collab.md` & `prompts/system/crabcakes-commands.md` (~35 lines changed)

1. **`prompts/system/collab.md`:**
   - Remove inline backticks around every `/ask` example (`:25-27, 35, 54, 63`) so models copying examples emit raw `/ask @AgentName "question"` at line start instead of `` `/ask ...` ``.
   - Add explicit rule under `### DO:` / `### DO NOT:`: write `/ask @AgentName "question"` on its own line using standard ASCII double quotes (`"`), never inside code fences or backticks, and escape any inner double quotes as `\"`.
   - Keep the heading `# Agent Collaboration` intact (`tests/test_context.py:90-104` asserts `"Agent Collaboration" in prompt`).
2. **`prompts/system/crabcakes-commands.md`:**
   - Replace line 3's stale `` `#task` refs, `— text` for a freeform body `` with accurate quoted-payload syntax (`/ask @agent "question"`).
   - Remove deleted review commands (`/review`, `/check`, `/accept`, `/reject` — removed in SPEC-58), fix `/status` alias from `/s` to `/st`, and list `/clear`, `/compact`, and `/session` (alias `/s`).

---

### 2.8 `tests/test_agent_command_handler.py` & NEW `tests/test_spec65_ask_command_hardening.py` (~280 lines)

1. **`tests/test_agent_command_handler.py` (`:396-420`):** Wire `handler.set_command_handler(FakeCommandHandler(commands={"ask"}))` in `test_chain_depth_cleared_after_response` and `test_no_pending_ask_just_chain_depth_cleared` so both tests exercise the production path instead of the dead `if not self._command_handler:` early return.
2. **NEW `tests/test_spec65_ask_command_hardening.py`:**
   - Tests all 12 formatting cases from Probe 1 (inline backticks, ```` ```html ```` cards vs `<code>` inside HTML, `**/ask ...**`, bullets/blockquotes, curly quotes, single quotes, em-dash, `/a` alias, `@Debugger:`, unescaped inner quotes `"check "foo" in bar.py"` and `""foo" is broken"`, `\\\n` continuation, `<think>` draft/unclosed-quote stripping, and multiline indentation preservation in `CommandHandler.process_input`).
   - Tests `/clear @Debugger\n/ask @Debugger "..."` and `/compact @Debugger\n/ask @Debugger "..."` source-order execution, active-loop refusal in `compact_conversation`, and `_turn_attr` preservation on refused `clear_conversation`.
   - Tests `Window._sync_special_agent_consumers` updating `CommandHandler._special_agents` on provider and agent saves.
   - Tests stranded `"[Supervisor asks]: ..."` inbound guarding `_pending_asks` across Turn 1 completion so Turn 2 relays to Supervisor, multi-asker FIFO promotion, `on_agent_error` zombie-ask cleanup, `[room]` witness suppression for routed `/ask` targets, and self-ask `_chain_depth` cutoff.

---

### Files NOT Changed (Rule 8)

- `utils/quoting.py` — `_parse_quoted_payload` remains the strict parser for user-typed slash commands in `CommandHandler.process_input` (where human typos must still return clear `"Unclosed quote"` / `"Empty payload"` errors per `tests/test_command_handler.py:571-595`).
- `agent/runtime.py` — untouched here (covered in `SPEC-64`; `has_pending_ask_inbound` uses `peek_pending_inbound` when present and falls back to `rt._lock` + `_inbox`/`_stranded` so `SPEC-65` has zero ordering dependency on `SPEC-64`).

---

## 3. Data Flow

1. **Agent emits `/ask` (with `<think>`, Markdown formatting, or `/clear` pairing):**
   `RuntimeTurnUiMixin._do_response_complete()` → `Window._on_agent_response()` → `AgentCommandHandler.on_agent_response()`:
   - Step 1: If `not _has_unstarted_ask_for(session_key)`, `_pop_next_asker(session_key)` relays `"[Responder responded]: ..."` to `source_sk`.
   - Step 2: `_extract_quoted_commands()` runs `_sanitize_for_command_scan()` (`<think>` stripped → `html`/`live` unwrapped → code fences stripped), scans commands via `_scan_agent_quoted_payload()`, deduplicates overlapping spans, normalizes `/a` → `ask`, and sorts by `raw_start`.
   - `/clear @Debugger` executes **first** (while Debugger is idle), then `/ask @Debugger "..."` executes **second** via `CommandHandler.process_input(..., skip_dispatch=True)` (preserving newlines/indentation from `raw` and resolving `@Debugger` against synced `_special_agents`).
   - `_route_command()` pushes `"agent:supervisor"` onto `_pending_asks["agent:debugger"]`, increments `_chain_depth["agent:debugger"]`, calls `send_to_special_agent("agent:debugger", "[Supervisor asks]: ...")`, and returns `routed_targets = {"agent:debugger"}`.
   - `_do_response_complete()` skips `[room]` witness delivery to `"agent:debugger"` while delivering witness lines to uninvolved room members.

---

## 4. File Change Summary

| File | Change Type | Est. Lines | Risk Level |
|---|---|---|---|
| `models/command.py` | Modify (`CommandRegistry.resolve_name` + docstring) | +12 / -1 | Low |
| `ui/handlers/collab_handler.py` | Modify (usage string alignment) | +3 / -3 | Low |
| `ui/handlers/command_handler.py` | Modify (`get_command_names` aliases, raw-slice multiline payload, `@name:` strip) | +28 / -10 | Low-Medium |
| `ui/handlers/agent_command_handler.py` | Modify (`<think>`/`html` sanitizer, `_scan_agent_quoted_payload`, sorted spans, `_pending_asks` guard/FIFO, `_chain_depth` pop, `on_agent_error`) | +180 / -65 | Medium |
| `ui/handlers/agent_runtime_handler.py` | Modify (`_on_agent_error_cb` slot + setter) | +10 / -0 | Low |
| `ui/agent_runtime/session.py` | Modify (`clear_conversation` `_turn_attr` order, `compact_conversation` active-loop guard, `has_pending_ask_inbound`) | +38 / -4 | Low-Medium |
| `ui/agent_runtime/turn_ui.py` | Modify (`routed_targets` witness skip + `_on_agent_error_cb` fire) | +18 / -3 | Low |
| `ui/handlers/project_handler.py` | Modify (`cmd_compact` active-loop refusal message) | +9 / -0 | Low |
| `ui/window.py` | Modify (`_sync_special_agent_consumers` + `set_on_agent_error` + `_on_agent_response` return) | +25 / -8 | Low |
| `prompts/system/collab.md` | Modify (unbackticked examples + explicit quote/line rules) | +16 / -10 | Low |
| `prompts/system/crabcakes-commands.md` | Modify (remove stale `—` body & deleted commands; fix aliases) | +12 / -12 | Low |
| `tests/test_agent_command_handler.py` | Modify (wire `set_command_handler` in 2 chain-depth tests) | +4 / -0 | Low |
| `tests/test_spec65_ask_command_hardening.py` | New (comprehensive tests for `2A–2C`, `3A–3D`) | +280 | Low |
| `docs/acceptance/rows.yaml` + `tests/mutations/R-10.diff`, `R-11.diff` | Modify / New (SPEC-47 rows `R-10`, `R-11`) | +35 | Low |

---

## 5. Implementation Order

1. **`models/command.py`, `ui/handlers/collab_handler.py`, `ui/handlers/command_handler.py`:** Add `resolve_name`, alias inclusion in `get_command_names()`, `@name:` punctuation stripping, and unsplit `raw` payload slicing. Run `.venv/bin/python -m pytest tests/test_command_models.py tests/test_command_handler.py -q`.
2. **`ui/handlers/agent_command_handler.py`:** Implement `_strip_thinking_blocks`, `_sanitize_for_command_scan`, `_scan_agent_quoted_payload`, sorted/overlap-safe `_extract_quoted_commands`, `_chain_depth` cleanup, `_pending_ask_waiters` + `_has_unstarted_ask_for`, and `on_agent_error`. Wire `set_command_handler` in the two `test_agent_command_handler.py` chain-depth tests and run `.venv/bin/python -m pytest tests/test_agent_command_handler.py -q`.
3. **`ui/handlers/agent_runtime_handler.py`, `ui/agent_runtime/session.py`, `ui/agent_runtime/turn_ui.py`, `ui/handlers/project_handler.py`, `ui/window.py`:** Wire `has_pending_ask_inbound`, `clear_conversation`/`compact_conversation` active-loop guards, `_on_agent_error_cb`, `routed_targets` witness skip, and `_sync_special_agent_consumers`. Run `.venv/bin/python -m pytest tests/test_compact_command.py tests/test_live_bridge.py tests/test_context.py -q`.
4. **`prompts/system/collab.md`, `prompts/system/crabcakes-commands.md`, `tests/test_spec65_ask_command_hardening.py`, and SPEC-47 rows `R-10`, `R-11`:** Update prompt files, add the new test suite and mutation diffs, and run `.venv/bin/python scripts/acceptance.py rows`.

---

## 6. Acceptance Criteria

- [ ] Inline backticks (`` `/ask @Debugger "q"` ``), bold (`**/ask ...**`), list/quote prefixes, ```` ```html ```` / ```` ```live ```` cards (excluding `<code>`/`<pre>` examples inside them), curly quotes, single quotes, `/ask @Debugger — "q"`, `/ask @Debugger: "q"`, `/a @Debugger "q"`, `\\\n` continuations, and single-line unescaped inner quotes all extract and forward cleanly; fenced code blocks (```` ```python ```` / ```` ``` ````) and unquoted payloads still extract `0` commands (Findings 2A, 2C).
- [ ] `<think>...</think>` blocks (and truncated unclosed `<think>...`) are stripped before command and audit-report scanning (Finding 2B).
- [ ] `CommandHandler.process_input` preserves `\n` and indentation inside quoted payloads (Finding 2C).
- [ ] `prompts/system/collab.md` contains zero backtick-wrapped `/ask` examples and `"Agent Collaboration" in prompt` remains true in `tests/test_context.py`; `crabcakes-commands.md` matches live registered commands and aliases (Findings 2A, 2C).
- [ ] Saving providers in Settings or saving/deleting agents in Agent Builder immediately updates `CommandHandler._special_agents` via `Window._sync_special_agent_consumers` without app restart (Finding 3A).
- [ ] When `"[Supervisor asks]: ..."` is queued/stranded in `_inbox`/`_stranded` while Debugger finishes a prior turn, Turn 1 does NOT pop `_pending_asks["agent:debugger"]`; Turn 2 (flushed from `_stranded`) relays Debugger's answer to Supervisor. Failed target turns clear zombie `_pending_asks` via `on_agent_error` **except when a stranded inbound ask is pending** (the guard at §2.5 keeps it for the flush turn), and `[room]` witness skips targets routed via `/ask` in that turn (Finding 3B).
- [ ] `AgentCommandHandler.on_agent_response` clears `_chain_depth[session_key]` when `CommandHandler` is wired and `session_key not in routed_targets`, while self-asks (`session_key in routed_targets`) still increment and halt at `_MAX_CHAIN_DEPTH` (Finding 3C).
- [ ] `/clear @Debugger\n/ask @Debugger "..."` extracts and executes `clear` before `ask`; a refused `clear_conversation` leaves `_turn_attr` intact; and a peer `compact_conversation` while `rt.is_loop_active` is `True` refuses cleanly without mutating `conv.messages` (Finding 3D).

---

## 7. Acceptance rows (SPEC-47)

| Row | Claim | Node | Mutation | Registered |
|---|---|---|---|---|
| R-10 | `_extract_quoted_commands` + `CommandHandler.process_input` strip `<think>` blocks, accept prompt-style formatting/aliases/inner quotes, preserve multiline indentation, and sort `/clear` before `/ask`. | `tests/test_spec65_ask_command_hardening.py::TestParserAndOrdering` | `tests/mutations/R-10.diff` (removes `results.sort(key=lambda pc: pc.raw_start)` and `_strip_thinking_blocks` call) | 2026-11-14 |
| R-11 | `_pending_asks` survives a prior turn's completion when a stranded `"[... asks]: "` message is pending, `_chain_depth` resets when `CommandHandler` is wired, and `_sync_special_agent_consumers` updates `CommandHandler._special_agents`. | `tests/test_spec65_ask_command_hardening.py::TestRelayAndRegistrySync` | `tests/mutations/R-11.diff` (bypasses `_has_unstarted_ask_for` check and removes `_chain_depth.pop(session_key, None)`) | 2026-11-14 |

---

## 8. Edge Cases

| Case | Expected Behavior |
|---|---|
| Agent self-asks (`/ask @Coder "step 2"` from `"agent:coder"`) | `"agent:coder" in routed_targets` → `_chain_depth["agent:coder"]` stays incremented (`depth + 1`) and cuts off at `_MAX_CHAIN_DEPTH = 3` (no infinite self-ask loop). |
| Multiple `/ask` commands on one line (`/ask @Debugger "q1" /ask @Coder "q2"`) | `_NEXT_CMD_RE` bounds inner-quote lookahead to each command segment → extracts 2 distinct commands (`q1`, `q2`). |
| Command followed by parenthetical quoted note (`/ask @Debugger "is this right?" (see "note")`) | Character after `"is this right?"` is whitespace (`' '`), so scanner closes at `"is this right?"` without swallowing `(see "note")`. |
| HTML card includes `<code>/ask @Coder "example"</code>` AND `<p>/ask @Debugger "real"</p>` | `_sanitize_for_command_scan` strips `<code>...</code>` before unwrapping tags → only `@Debugger "real"` executes. |
| Quoted payload mentions another command (`/ask @Debugger "do not run /clear @Coder"`) | Overlap guard (`_overlaps`) sees `/clear @Coder` lies inside the span of `/ask @Debugger "..."` → `/clear` is NOT extracted. |
| Two agents (`Supervisor` and `Coder`) concurrently `/ask @Debugger` | `_pending_asks["agent:debugger"]` holds `"agent:supervisor"` (dict shape preserved) and `_pending_ask_waiters["agent:debugger"]` holds `["agent:coder"]`; first reply relays to Supervisor and promotes Coder next. |
| `AgentRuntimeHandler.__new__(AgentRuntimeHandler)` used in legacy unit tests | `getattr(self, "_on_agent_error_cb", None)` and `getattr(self, "_agents", None)` prevent `AttributeError` when `__init__` was bypassed. |

---

## 9. ARCHITECTURE.md Updates Required

- **`§3.21a` (`ui/handlers/command_handler.py`):** Document alias inclusion in `get_command_names()`, raw-slice multiline preservation in `process_input()`, `@mention` trailing-punctuation stripping, and `Window._sync_special_agent_consumers()` keeping `_special_agents` live on provider/agent saves.
- **`§3.21f` (`ui/handlers/agent_command_handler.py`):** Document `_sanitize_for_command_scan()` (`<think>` removal + `html`/`live` unwrap), `_scan_agent_quoted_payload()`, `raw_start` sorting for `/clear`+`/ask` pairs, `_has_unstarted_ask_for` stranded-turn protection, `_pending_ask_waiters` FIFO, `on_agent_error`, and `routed_targets` `[room]` witness suppression.

---

## 10. Spec Self-Audit (Rule 9)

1. **Does every code sample work against the current codebase?** Verified: the complete `_strip_thinking_blocks` + `_sanitize_for_command_scan` + `_scan_agent_quoted_payload` + `_extract_quoted_commands` implementation was executed against 28 cases (all existing negative/positive tests in `test_agent_command_handler.py` plus all 12 Probe 1 & Probe 2 failure cases) and passed 28/28.
2. **Did I enumerate all exception types?** Verified: `_has_unstarted_ask_for` and `_on_agent_error_cb` wrap callback invocations in `try/except Exception` with logging so handler errors never abort `_do_response_complete` or `_do_error`; `__new__`-constructed test doubles are guarded with `getattr`.
3. **Did I verify key structures?** Verified: `self._pending_asks` stays `dict[str, str]` (`target_sk -> source_sk`), preserving all 16 direct dict-access assertions in `tests/test_agent_command_handler.py` and `_resolve_target_role`.
4. **Did I trace data flow end-to-end?** Verified: traced self-ask `_chain_depth` termination, `/ask` containing `/clear` inside quotes (overlap guard), `Window._on_agent_response` forwarding `routed_targets` back to `turn_ui.py`, and `SPEC-65` running cleanly with or without `SPEC-64` (`peek_pending_inbound` vs `_lock` fallback).
5. **Zero file overlap with `SPEC-64`?** Verified: `SPEC-65` touches zero files under `agent/`.
