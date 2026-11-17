# `/ask` Command & Streaming Reliability Investigation (Read-Only)

**Date:** 2026-11-14  
**Scope:** `develcakes` @ HEAD `694431cf`  
**Product code modified:** None (investigation report only)

---

## 1. Problem Statement & Symptom

On a second computer running the exact same version of DevelCakes (`694431cf`), the `/ask` agent-to-agent (A2A) command intermittently fails for certain LLM models. Specifically:

> Supervisor sends the `/ask` command to Debugger, Debugger spits out some output, and `/ask` does not trigger (either Debugger's response never relays back to Supervisor, Debugger's own `/ask` never fires, or Supervisor's `/ask` appears in chat while Debugger only speaks via room traffic without an active `/ask` relay).

---

## 2. Baseline Architecture: How `/ask` Actually Works

`/ask` is **not** a tool schema or function call. It is a two-leg **post-turn prose-scraping and auto-relay** pipeline wired across four handlers:

1. **Outbound leg (Supervisor → Debugger):**
   - `AgentRuntime._run_loop` (`agent/runtime.py:1955`) finishes an assistant turn with no tool calls and calls `self._terminate_turn(TurnResult(status=TurnStatus.COMPLETED, text=text_content, ...))` (`agent/runtime.py:2505-2513`).
   - `_terminate_turn` (`agent/runtime.py:976-980`) dispatches `self._on_response_complete(sk, result.text, _turn_token=...)` onto the GTK main thread via `GLib.idle_add`.
   - `RuntimeTurnUiMixin._do_response_complete` (`ui/agent_runtime/turn_ui.py:415-610`) finalizes the WebKit streaming row and, **only when `text` is truthy** (`line 531`), invokes `self._on_agent_response(session_key, text, project_name)`.
   - `Window._on_agent_response` (`ui/window.py:1632-1648`) calls `self._agent_command_handler.on_agent_response(session_key, text, project_name)`.
   - `AgentCommandHandler.on_agent_response` (`ui/handlers/agent_command_handler.py:247-341`):
     1. Strips fenced code blocks via `_strip_fenced_blocks(text)` (`line 295`).
     2. Extracts slash commands via `_extract_quoted_commands(clean_text, command_names)` (`line 299`).
     3. Re-serializes each command as `/{command} {agent} "{escaped}"` and calls `CommandHandler.process_input(session_key, candidate, skip_dispatch=True)` (`lines 317-324`).
     4. `CommandHandler.process_input` (`ui/handlers/command_handler.py:243-389`) resolves `@Debugger` against `self._special_agents` (`lines 504-518`), parses the quoted payload, and invokes `CollabHandler.cmd_ask` (`ui/handlers/collab_handler.py:37-43`), returning `CommandResult(handled=True, forward_to="agent:debugger", forward_text=body)`.
     5. `AgentCommandHandler._route_command` (`ui/handlers/agent_command_handler.py:431-462`) records `self._pending_asks["agent:debugger"] = "agent:supervisor"`, increments `self._chain_depth["agent:debugger"]`, and calls `send_to_special_agent("agent:debugger", "[Supervisor asks]: ...")`.

2. **Return / relay leg (Debugger → Supervisor):**
   - When Debugger's turn completes with `TurnStatus.COMPLETED` **and non-empty `text`**, `_do_response_complete` calls `AgentCommandHandler.on_agent_response("agent:debugger", text, project_name)`.
   - Step 1 (`ui/handlers/agent_command_handler.py:271-273`) runs `source_sk = self._pending_asks.pop("agent:debugger", None)` and calls `_relay_response("agent:supervisor", "agent:debugger", text, project_name)` (`lines 404-427`), sending `"[Debugger responded]: {text}"` back to `"agent:supervisor"` (and then scans `text` in Step 2 for any nested `/ask` emitted by Debugger).

---

## 3. Summary of Findings

| ID | Layer | Severity | Summary |
|---|---|---|---|
| **1A** | Streaming + Tool Loop | **CRITICAL** | `_graceful_iteration_close` completes with `TurnResult.text=""`; `_do_response_complete` renders all streamed deltas in chat ("Debugger spits output") and skips `_on_agent_response` (`if ... and text:`). Triggered at 50 iterations when `DecisionModel` (JEV) is unconfigured/unreachable on the second machine. |
| **1B** | Streaming + Tool Loop | **HIGH** | Models that stream their full answer/command alongside their last tool call (Iteration $N$) and emit empty `content: ""` on Iteration $N+1$ hit `TurnStatus.FAILED` (`empty_content`). `_do_error` renders Iteration $N$'s streamed text in chat, skips `_on_agent_response`, and leaks `_pending_asks`. |
| **1C** | Streaming + Tool Loop | **HIGH** | `_run_loop` passes **only** the final iteration's `text_content` into `TurnResult.text`. Any `/ask` streamed during an earlier tool-calling iteration (e.g. alongside `diary`/`feed_card`) is wiped from the UI bubble by `set_streaming_text` and never seen by `AgentCommandHandler`. |
| **2A** | Prompt vs. Parser | **HIGH** | Every example in `prompts/system/collab.md` wraps `/ask` in inline backticks (`` `/ask ...` ``) or triple-backtick fences; `_extract_quoted_commands` requires `(?:^|\s)/` (0 matches on inline backticks/bold/bullets) and `_strip_fenced_blocks` deletes ```` ```html ```` cards and code blocks before scanning. |
| **2B** | Reasoning Streams | **HIGH** | `<think>...</think>` blocks streamed inside `delta.content` are never stripped before command scanning: draft `/ask` commands inside `<think>` execute, burn the 3-command cap, or swallow the real post-`</think>` `/ask` via unclosed quotes/fences. |
| **2C** | Parser Edge Cases | **MEDIUM-HIGH** | Six silent parser drops: (1) unescaped inner `"`, (2) curly/single quotes, (3) em-dash `/ask @Debugger — "..."` (contradicted by `crabcakes-commands.md`), (4) `@Debugger:` punctuation silently logged as `[Action result]`, (5) `/a` alias documented in `crabcakes-commands.md` but rejected by extractor, (6) `\\\n` in multi-line payloads + whitespace flattening in `process_input`. |
| **3A** | Cross-Machine Setup | **HIGH** | `CommandHandler._special_agents` is snapshotted **once** at window init (`ui/window.py:737`) and never updated in `_on_providers_changed` or `on_agent_saved`/`on_agent_deleted`. Agents whose providers are configured after launch on a new machine fail `@mention` resolution silently. |
| **3B** | Concurrency & Room | **HIGH** | `_pending_asks` is a single session-keyed slot (`dict[str, str]`) with no turn token. If Debugger is already running when `/ask @Debugger` fires, Debugger's *prior* turn pops `_pending_asks` on completion, leaving the queued `[Supervisor asks]` turn with no relay. Separately, `[room]` witness lines let Debugger see and answer a malformed `/ask` without `_pending_asks` ever arming. |
| **3C** | State Leak + Test Bug | **MEDIUM** | `AgentCommandHandler._chain_depth` is never cleared when `self._command_handler` is wired and `parsed_commands` is empty (`agent_command_handler.py:278-303`), eventually hitting `_MAX_CHAIN_DEPTH = 3` and dropping `/ask`. Masked because unit tests omit `set_command_handler`. |
| **3D** | Command Ordering Race | **MEDIUM-HIGH** | `_extract_quoted_commands` runs Pass 1 (`/ask`) before Pass 5/6 (`/stop`, `/clear`, `/compact`) without sorting by `raw_start`. Mandatory `/clear @Debugger` + `/ask @Debugger` pairs execute **backwards**: `/ask` starts the loop first, `/clear` fails AND deletes `_turn_attr["agent:debugger"]`, and `/compact` mutates `conv.messages` with no `is_loop_active` guard. |
| **4A** | Provider Transport | **MEDIUM** | `AnthropicProvider` hardcodes `"max_tokens": 4096` (`anthropic_provider.py:80, 134`), and `OpenAIProvider`/`MiniMaxProvider` treat `finish_reason == "length"` as normal completion with no warning — silently cutting off trailing `/ask` commands on long outputs. |
| **4B** | Provider Transport | **LOW-MEDIUM** | `parse_sse_line` drops non-SSE JSON bodies if an OpenAI-compatible endpoint ignores `stream: True`; structured `delta.content` lists (`list[dict]`) pass `if content is not None:` and raise `TypeError` on `"".join(parts)`. |

---

## 4. Detailed Findings & Code Traces

### Layer 1 — Streaming + Tool-Loop Bugs

#### Finding 1A (`CRITICAL`): `_graceful_iteration_close` sets `TurnResult.text = ""`, rendering streamed output while bypassing `_on_agent_response`

**Files & lines:**
- `agent/config.py:87-88` (`max_tool_iterations: int = 50`, `tool_iteration_hard_cap: int = 100`)
- `agent/runtime.py:212-218` (`TurnResult.text: str = ""`)
- `agent/runtime.py:2557-2577` (soft-limit exhaustion branch in `_run_loop`)
- `agent/runtime.py:3928-3969` (`_iteration_judge`)
- `agent/runtime.py:3971-3988` (`_graceful_iteration_close`)
- `ui/agent_runtime/turn_ui.py:485-504, 530-533` (`_do_response_complete`)

**Mechanism:**
1. During an investigation/audit turn, Debugger streams reasoning/progress text across tool iterations (`_on_text_delta` accumulates into `self._streaming_text[sk]` and buffers into `ChatRenderHandler._stream_text[sk]`).
2. When Debugger reaches `iteration == 50` (`max_tool_iterations`), `_run_loop` consults `self._iteration_judge(...)` (`agent/runtime.py:2559`), which calls `post_decision` on `DecisionModel` (JEV) (`agent/runtime.py:3957`).
   - **Cross-computer amplifier:** If `DecisionModel` is not configured or unreachable on the second machine, `post_decision` returns an error and `_iteration_judge` immediately returns `("stop", 0.0)` (`lines 3958-3960`) with **0 extensions granted**. Verbose/granular-reading models hit 50 tool calls routinely.
3. `_run_loop` calls `_graceful_iteration_close` (`agent/runtime.py:3971-3988`):
   ```python
   def _graceful_iteration_close(self, session_key: str, conv, turn_token,
                                 iteration: int, tool_errors: int,
                                 grants_used: int, hit_hard_cap: bool) -> None:
       """Close the turn COMPLETED with a synthetic summary instead of FAILED."""
       reason = "hard_cap" if hit_hard_cap else "iteration_judge"
       conv.add_assistant_message(
           f"[tool loop closed — {reason} at {iteration} iterations, "
           f"{grants_used} extension(s) granted, {tool_errors} tool error(s). "
           f"Partial work above stands; resume with a follow-up message.]",
           [],
       )
       self._terminate_turn(TurnResult(
           status=TurnStatus.COMPLETED,
           session_key=session_key,
           turn_token=turn_token,
           error=None,
           metadata={"reason": reason, "iterations": iteration, "grants": grants_used},
       ))
   ```
   Notice `text=` is **not passed** to `TurnResult`, so `result.text` defaults to `""`.
4. `_terminate_turn` dispatches `self._on_response_complete(sk, "", _turn_token=...)` (`agent/runtime.py:977`).
5. In `RuntimeTurnUiMixin._do_response_complete` (`ui/agent_runtime/turn_ui.py:485-533`):
   ```python
   if was_streaming and text:
       self._crh.set_streaming_text(session_key, text)

   streaming_text = self._crh.get_streaming_text(session_key) or ""
   self._crh.end_streaming(
       session_key,
       agent_name=resolved_name,
       render=bool(streaming_text.strip()),
       mount_key=reply_key,
   )
   ...
   # Agent command parsing hook (Phase 6.2) — fire after bubble render, before lifecycle
   if self._on_agent_response is not None and text:
       project_name = self._active_project[0] if self._active_project else None
       self._on_agent_response(session_key, text, project_name)
   ```
   - Because `text` is `""`, `set_streaming_text` is **skipped**, leaving all streamed deltas from iterations 1–50 inside `_crh._stream_text`.
   - `end_streaming` sees `bool(streaming_text.strip()) == True` and **renders Debugger's accumulated streamed output as a normal completed chat bubble** (while the `[tool loop closed — ...]` message is never rendered in UI at all because `was_streaming` is `True`!).
   - `if self._on_agent_response is not None and text:` evaluates to **`False`**.
   - **Outcome:** Debugger visibly spits output in chat with zero error cards, **never triggers the `_pending_asks` relay back to Supervisor**, never triggers any `/ask` inside Debugger's streamed text, and leaves `_pending_asks["agent:debugger"] = "agent:supervisor"` leaked in memory.

---

#### Finding 1B (`HIGH`): Streaming prose + tool call in Iteration $N$, followed by empty `content` in Iteration $N+1$, renders streamed output and drops `/ask` + relay

**Files & lines:**
- `agent/runtime.py:2352-2431` (`_is_empty_content(text_content)` check when `not tool_calls_raw`)
- `agent/runtime.py:3781-3785` (`_call_llm_streaming` dispatching `_on_text_delta`)
- `ui/agent_runtime/turn_ui.py:64, 183` (`_streaming_text` accumulation across the whole turn)
- `ui/agent_runtime/turn_ui.py:676-705` (`_do_error` finalizing `end_streaming`)

**Mechanism:**
1. Many streaming models (e.g., Nemotron, Qwen, DeepSeek, GLM/ZAI, Gemini via OpenRouter — `agent/runtime.py:2405` explicitly cites `nemotron-3-ultra` doing this) stream their full textual report or `/ask` command **in the same LLM response as their final tool call** (Iteration $N$, e.g. calling `feed_card`, `diary`, or `write_file`).
2. `_on_text_delta` buffers all of Iteration $N$'s streamed prose in `ChatRenderHandler._stream_text[session_key]`.
3. The tool executes and returns `"OK"`. On Iteration $N+1$, the model has nothing left to say and emits `finish_reason="stop"` with `content=""` (or whitespace) and `tool_calls=[]`.
4. `_run_loop` (`agent/runtime.py:2362-2430`) treats `_is_empty_content(text_content)` with no tool calls as `TurnStatus.FAILED` (`reason: "empty_content"`), dispatching `_on_error`.
5. `_do_error` (`ui/agent_runtime/turn_ui.py:680-687`) runs:
   ```python
   streaming_text = self._crh.get_streaming_text(session_key) or ""
   self._crh.end_streaming(
       session_key,
       agent_name=resolved_name,
       render=bool(streaming_text.strip()),
       mount_key=reply_key,
   )
   ```
   `end_streaming` renders Iteration $N$'s streamed text as a normal agent message bubble (followed by `[Error] Agent returned no content...`), and because `_do_error` **never invokes `_on_agent_response`**, neither Debugger's relay back to Supervisor nor any `/ask` in Iteration $N$'s output ever fires. `_pending_asks["agent:debugger"]` also stays leaked.

---

#### Finding 1C (`HIGH`): Multi-iteration tool loop overwrites earlier iterations' streamed text and drops any `/ask` emitted before the final iteration

**Files & lines:**
- `agent/runtime.py:2505-2514` (`TurnResult` receives only the final iteration's `text_content`)
- `ui/agent_runtime/turn_ui.py:64, 485-486, 531-533`

**Mechanism:**
1. During a multi-step turn, `self._streaming_text[session_key]` (`turn_ui.py:64`) concatenates streamed `text_delta` chunks across **every** LLM call in the turn.
2. However, when the turn completes normally at `agent/runtime.py:2505-2514`, `TurnResult(status=TurnStatus.COMPLETED, ..., text=text_content)` receives **only the last LLM call's `text_content`**.
3. In `_do_response_complete`:
   - `if was_streaming and text: self._crh.set_streaming_text(session_key, text)` (`turn_ui.py:485-486`) **overwrites** the cumulative buffer with only the final iteration's `text`.
   - `self._on_agent_response(session_key, text, project_name)` (`turn_ui.py:533`) scans **only** the final iteration's `text`.
4. **Model-specific trigger:** Suppose Supervisor (or Debugger) outputs `/ask @Debugger "audit X"` in Iteration 1 alongside a housekeeping tool call (`diary`, `project_memory`, `feed_card`, `talk` — required by the standing completion protocol). In Iteration 2, the model sees its own Iteration 1 assistant message (already containing `/ask @Debugger "audit X"`) in `conv.messages`, assumes the command was already emitted, and replies `"Recorded in diary."` with no tool calls. Iteration 1's `/ask` is wiped from the rendered bubble and never passed to `AgentCommandHandler`.

---

### Layer 2 — Prompt-vs-Parser Mismatches (Model-Dependent Formatting Drops)

#### Finding 2A (`HIGH`): `prompts/system/collab.md` teaches backtick-wrapped and fenced `/ask` syntax that `AgentCommandHandler` rejects 100% of the time

**Files & lines:**
- `prompts/system/collab.md:14-16, 25-27, 35, 54, 63`
- `prompts/system/crabcakes-context.md:16-32`
- `ui/handlers/agent_command_handler.py:85-159, 295, 615-621`

**Mechanism:**
1. Every non-Coder agent's system prompt includes the raw Markdown of `prompts/system/collab.md` (`utils/prompt_loader.py:218-220`). In `collab.md`:
   - Lines 14–16 show `/ask @AgentName "your question"` inside a triple-backtick fence ```` ``` ````.
   - **Every single inline example** (lines 25, 26, 27, 35, 54, 63) wraps `/ask` in single backticks:
     - `` - `/ask @Coder "should I use a generator or a list comprehension here?"` ``
     - `` - `/ask @Debugger "is this edge case covered by the existing tests?"` ``
2. In `AgentCommandHandler._extract_quoted_commands` (`ui/handlers/agent_command_handler.py:86-159`), Passes 1–4 require `(?:^|\s)/` immediately before the command:
   ```python
   r'(?:^|\s)/([a-zA-Z]+)\s+(@[^\s"]+)\s+"((?:[^"\\]|\\.)*)"'
   ```
   - When a model copies `collab.md` and outputs `` `/ask @Debugger "check this"` ``, the character preceding `/` is `` ` `` (neither start-of-string nor whitespace). **Result: 0 commands extracted.** In the WebKit UI, `` `/ask @Debugger "..."` `` renders cleanly as an inline `<code>` chip, making it look valid while nothing fires.
   - The same `(?:^|\s)/` guard silently drops `**/ask @Debugger "..."**`, `*/ask @Debugger "..."*`, `-/ask @Debugger "..."`, `>/ask @Debugger "..."`, and `(/ask @Debugger "...")`.
3. Before `_extract_quoted_commands` even runs, `on_agent_response` (`ui/handlers/agent_command_handler.py:295`) calls:
   ```python
   @staticmethod
   def _strip_fenced_blocks(text: str) -> str:
       return re.sub(r"```.*?```", "", text, flags=re.DOTALL)
   ```
   - If a model wraps `/ask` in a ```` ``` ```` block (copying `collab.md:14-16`), it is stripped.
   - Worse: `prompts/system/crabcakes-context.md:16-24` instructs agents that the chat surface is WebKit and encourages ```` ```html ```` and ```` ```live ```` fenced blocks for formatted replies. In WebKit, an ```` ```html ```` block renders as rich prose/cards (not a code block), **but `_strip_fenced_blocks` strips all ```` ```...``` ```` blocks indiscriminately**, deleting any `/ask` placed inside an HTML card or live block.

---

#### Finding 2B (`HIGH`): `<think>...</think>` reasoning blocks streamed in `content` are never stripped before scanning

**Files & lines:**
- `agent/llm/streaming.py:100-102`
- `agent/runtime.py:3781-3785`
- `ui/handlers/agent_command_handler.py:40, 294-311`

**Mechanism:**
Many models (MiniMax M2.5/M2.7, DeepSeek-R1, Qwen3, GLM/ZAI, local/OpenRouter reasoning models) stream chain-of-thought reasoning inside `delta.content` wrapped in `<think>...</think>`. `on_agent_response` scans the full `text` without stripping `<think>...</think>`:
1. **False-positive execution & 3-command cap exhaustion:** Any `/ask @Debugger "..."` the model rehearses inside `<think>...</think>` is extracted and executed for real, counting toward `_MAX_COMMANDS_PER_RESPONSE = 3` (`agent_command_handler.py:40, 305`). If the model drafts 3 variations in `<think>`, the real 4th `/ask` after `</think>` is silently dropped (`Per-response command limit (3) reached`).
2. **Quote-boundary corruption across `</think>`:** If `<think>` contains an unclosed quote after `/ask @Debugger "` (e.g. `<think>Should I write /ask @Debugger "check foo? No, better:</think>\n/ask @Debugger "check bar"`), Pass 1 matches from the `<think>` `/ask`'s opening `"` **across `</think>\n/ask @Debugger `** and closes on the opening `"` of `"check bar"` — forwarding the garbled thinking fragment and leaving `check bar"` unparsed.
3. **Unclosed triple-backtick inside `<think>`:** An unclosed ```` ``` ```` inside `<think>` pairs via `_strip_fenced_blocks` (`re.DOTALL`) with a ```` ``` ```` in the visible answer, stripping everything in between.

---

#### Finding 2C (`MEDIUM-HIGH`): Six strict quote, punctuation, alias, and newline traps (including prompt contradictions)

**Files & lines:**
- `ui/handlers/agent_command_handler.py:85-173, 316-341`
- `ui/handlers/command_handler.py:83-84, 176-182, 274-348, 469-523`
- `utils/quoting.py:14-78`
- `prompts/system/crabcakes-commands.md:3, 7`

**Verified traps:**
1. **Unescaped inner double quotes:** If a model writes `/ask @Debugger "check "foo" in bar.py"` without backslash-escaping `"foo"`, Pass 1 stops at the quote before `foo` and forwards only `"check "`. If the inner quote is at index 0 (`/ask @Debugger ""foo" is broken"`), Pass 1 captures `""` (rejected as empty by `_parse_quoted_payload` at `utils/quoting.py:71`), and Pass 2 (`"([^"]*)$`) fails because later quotes exist → **0 commands extracted**.
2. **Curly quotes or single quotes:** `/ask @Debugger “check this”` (typographic quotes emitted by several prose-tuned LLMs) or `/ask @Debugger 'check this'` → **0 commands extracted**.
3. **Em-dash separator before quote (prompt contradiction):** `prompts/system/crabcakes-commands.md:3` tells every agent: ``All start with `/`: `/command args`, `@agent` mentions, `#task` refs, `— text` for a freeform body`` (and `command_handler.py:84` / `collab_handler.py:42` help strings say `/ask @agent — question`). If an agent writes `/ask @Debugger — "check this"` or `/ask @Debugger — check this`, Pass 1 expects `"` immediately after `@Agent\s+` → **0 commands extracted**.
4. **Punctuation attached to `@Agent` (`/ask @Debugger: "check this"`):** `@[^\s"]+` captures `@Debugger:`. `CommandHandler._resolve_mention` (`command_handler.py:469-523`) fails to match `"Debugger:"` against `"Debugger"` and returns `CommandResult(handled=True, response_text="Unknown agent: @Debugger:")`. Because `AgentCommandHandler` calls `process_input(..., skip_dispatch=True)` (`agent_command_handler.py:322-323`), **no error is shown in chat** — instead `lines 336-341` (`elif result.handled and result.response_text:`) silently inject `[Action result]: Unknown agent: @Debugger:` as a user message into Supervisor's `conv.messages` without starting a turn!
5. **Documented `/a` alias rejected by agent scanner (prompt contradiction):** `prompts/system/crabcakes-commands.md:7` advertises: `| /ask | @agent "q" | Ask an agent a question. Alias /a. |`. `CommandHandler` registers `aliases=["a"]` (`command_handler.py:83`), **but** `get_command_names()` (`command_handler.py:176-182`) returns `set(self._registry.list_commands())` (omitting aliases), **and** `_extract_quoted_commands` (`agent_command_handler.py:93, 120`) hardcodes `if cmd not in ('ask', 'tell', 'delegate', 'stop', 'compact', 'clear'):`. Any agent using `/a @Debugger "..."` produces **0 commands**.
6. **Backslash-newline (`\\\n`) in multi-line payloads & whitespace flattening:** Pass 1's regex (`r'(?:^|\s)/([a-zA-Z]+)\s+(@[^\s"]+)\s+"((?:[^"\\]|\\.)*)"'`) is compiled **without `re.DOTALL`**. While `[^"\\]` matches `\n`, `\\.` does **not** match `\` followed by `\n` (line continuation inside a quoted snippet) — Pass 1 fails, and Pass 2 (`"([^"]*)$`) fails because of the closing `"` → **0 commands extracted**. Furthermore, for multi-line payloads without `\\\n` that *do* match Pass 1, `CommandHandler.process_input` (`command_handler.py:274, 318`) runs `tokens = raw.split()` followed by `rest_text = " ".join(args_after_mentions)`, **destroying all newlines and indentation** inside the quoted payload before `_parse_quoted_payload` reads it.

---

### Layer 3 — Cross-Machine Setup, Concurrency, & State Leaks

#### Finding 3A (`HIGH`): `CommandHandler._special_agents` is snapshotted ONCE at startup and never updated

**Files & lines:**
- `ui/window.py:258-264` (`on_agent_saved` / `on_agent_deleted`)
- `ui/window.py:737` (one-time `self._command_handler.set_special_agents(...)`)
- `ui/window.py:2279-2299` (`_on_providers_changed`)
- `utils/agent_defs.py:780-793` (`validate_agent_def` `llm_name` check)

**Mechanism:**
At app startup (`ui/window.py:737`), `CommandHandler` is given a **value-copy dict** (`{sk: ad.display_name}`) of currently loaded special agents:
```python
self._command_handler.set_special_agents(self._agent_runtime_handler.get_special_agents())
```
`set_special_agents` is **never called anywhere else in the codebase**:
- In `ui/window.py:2279-2299` (`_on_providers_changed`, triggered when saving providers in Settings), `arh.refresh_agent_registry()` reloads `~/.config/develcakes/agents/*.yaml` and updates `left_panel.set_special_agents(arh)`, **but does NOT update `self._command_handler`**.
- In `ui/window.py:258-264` (`on_agent_saved` / `on_agent_deleted`, triggered when creating, renaming, or editing an agent in Agent Builder), `arh.reload_agents_and_mcp(...)` updates `arh._agents` and `left_panel`, **but does NOT update `self._command_handler`**.

**Why this bites on a second computer:**
If you copy or sync agent YAMLs that carry `llm_name: <provider>` onto a second machine where at least one other provider exists (e.g. `DecisionModel` or `openrouter`) before `<provider>` is added in Settings, `validate_agent_def` (`utils/agent_defs.py:790`) skips that agent at boot (`Unknown provider`). Once you add `<provider>` in Settings, `refresh_agent_registry()` makes the agent appear in the Left Panel and work in chat — **until you restart the app**, `CommandHandler._special_agents` still lacks it! Every `/ask @Debugger` (or `/ask @Flagger` for a newly created agent) fails `@mention` lookup and silently injects `[Action result]: Unknown agent: @...` into Supervisor's conversation (`agent_command_handler.py:340`).

---

#### Finding 3B (`HIGH`): Concurrent turn race on `_pending_asks` + Room Reply Witness (`[room]`) illusion

**Files & lines:**
- `ui/handlers/agent_command_handler.py:200, 271-273, 451-462`
- `agent/runtime.py:1500-1518, 2503, 2666-2671`
- `ui/agent_runtime/turn_ui.py:553-592, 610`

**Two distinct multi-agent interactions produce the exact visual symptom ("Supervisor sends `/ask` to Debugger, Debugger spits output, `/ask` doesn't trigger"):**

1. **Premature `_pending_asks.pop` when Debugger already has a turn in flight:**
   - `_pending_asks` (`agent_command_handler.py:200`) is a bare `dict[target_sk, source_sk]` with **no turn token or message correlation**.
   - Suppose a room message wakes **both** Supervisor and Debugger (for instance, on the second computer `DecisionModel` JEV is offline and `jev_room_fallback` is `"all"`, or JEV multi-select picks both `["agent:supervisor", "agent:debugger"]` on *"Supervisor, ask Debugger to check X"*, or Debugger is still finishing an earlier turn).
   - Supervisor finishes first and emits `/ask @Debugger "..."`.
   - `_route_command` (`agent_command_handler.py:452`) immediately sets `self._pending_asks["agent:debugger"] = "agent:supervisor"` and calls `send_to_special_agent("agent:debugger", "[Supervisor asks]: ...")`.
   - Because `"agent:debugger"` is already in `rt._active_loops`, `rt.begin_send` (`agent/runtime.py:1510-1514`) appends `"[Supervisor asks]: ..."` to `self._inbox["agent:debugger"]` and returns `False`.
   - If Debugger's **already-running** turn finishes its final LLM call (or hits an late inbox race into `_stranded` at `agent/runtime.py:2670`), its `_do_response_complete` fires `on_agent_response("agent:debugger", ...)` and **pops `_pending_asks["agent:debugger"]` on the old turn** (`agent_command_handler.py:271`).
   - `_flush_stranded_sends` (`turn_ui.py:610`) then starts Debugger's new turn for `"[Supervisor asks]: ..."`. Debugger runs, **spits out its answer in chat**, and when `_do_response_complete` runs, **`_pending_asks["agent:debugger"]` is already gone** — so Debugger's answer is never relayed back to Supervisor.
   - *(Note also the inverse overwrite: if both Supervisor and Coder `/ask @Debugger` concurrently, `_pending_asks["agent:debugger"]` is a single key — the second asker overwrites the first.)*

2. **Room Reply Witness (`[room] Supervisor: ...`) when `/ask` failed to parse:**
   - When Supervisor responds to a room turn and formats `/ask` in a way `_extract_quoted_commands` ignores (e.g. `` `/ask @Debugger "..."` `` in inline backticks per Finding 2A), `on_agent_response` (`turn_ui.py:533`) extracts `0` commands and sets **no** `_pending_asks` entry.
   - Immediately afterward (`turn_ui.py:553-592`), `take_room_turn` succeeds and broadcasts `"[room] Supervisor: ... `/ask @Debugger \"...\"`"` into every room member's conversation via `record_room_line` — including Debugger's!
   - On the next room message or broadcast, Debugger sees `[room] Supervisor: ...` in its history and **spits out a response**, while `/ask` never actually triggered and nothing relays back to Supervisor.

---

#### Finding 3C (`MEDIUM`): `_chain_depth` leaks across the entire session (masked by an unwired unit test)

**Files & lines:**
- `ui/handlers/agent_command_handler.py:278-303, 425, 455`
- `tests/test_agent_command_handler.py:397-420`

**Mechanism:**
In `AgentCommandHandler.on_agent_response` (`ui/handlers/agent_command_handler.py:278-303`):
```python
if not self._command_handler:
    # No command handler — clear depth and return (relay already done above)
    self._chain_depth.pop(session_key, None)
    return

depth = self._chain_depth.get(session_key, 0)
if depth >= _MAX_CHAIN_DEPTH:
    logger.warning(
        "[agent-cmd] Chain depth limit (%d) reached for %s — dropping commands",
        _MAX_CHAIN_DEPTH, session_key
    )
    self._chain_depth.pop(session_key, None)
    return

clean_text = self._strip_fenced_blocks(text)
command_names = self._command_handler.get_command_names() if self._command_handler else None
parsed_commands = _extract_quoted_commands(clean_text, command_names)

if parsed_commands:
    ...
```
When `self._command_handler` **is** set (always `True` in production via `ui/window.py:806`) and `parsed_commands` is empty, **`self._chain_depth.pop(session_key, None)` is never called!** (`_relay_response` at `line 425` pops `source_sk`, not the responder's `session_key`.)
- If `Debugger` is asked (`_chain_depth["agent:debugger"] = 1`), answers without issuing a command (`_chain_depth["agent:debugger"]` stays `1`), and in a later chain is asked at depth `2` (`_chain_depth["agent:debugger"] = 3`), then the next time `Debugger` tries to issue `/ask` in a standalone turn, `depth >= _MAX_CHAIN_DEPTH` (`3 >= 3`) fires and **drops Debugger's `/ask` command**.
- **Why tests missed it:** `TestMultiHop.test_chain_depth_cleared_after_response` and `test_no_pending_ask_just_chain_depth_cleared` (`tests/test_agent_command_handler.py:397-420`) never call `handler.set_command_handler(...)`, so both tests exit via the `if not self._command_handler:` early-return on line 278 instead of testing the production path!

---

#### Finding 3D (`MEDIUM-HIGH`): `_extract_quoted_commands` executes `/clear @Debugger` + `/ask @Debugger` in reverse order

**Files & lines:**
- `prompts/implementationSupervisor.md:270-280` (mandatory `/clear` + `/ask` pairing rule)
- `ui/handlers/agent_command_handler.py:85-173`
- `ui/agent_runtime/session.py:52, 75-80, 133-240`

**Mechanism:**
`prompts/implementationSupervisor.md:270-280` instructs Supervisor:
```text
/clear @Debugger
/ask @Debugger "please audit per adversarialDebugger.md — scope: ..."
```
In `_extract_quoted_commands` (`ui/handlers/agent_command_handler.py:85-173`), Pass 1 (quoted commands: `/ask`) runs **before** Pass 5 (`/stop`) and Pass 6 (payload-free `/compact` and `/clear`), and **`results` is returned without sorting by `raw_start`**:
1. `on_agent_response` executes `/ask @Debugger "..."` **first**, which calls `send_to_special_agent("agent:debugger", ...)`, claiming `rt._active_loops.add("agent:debugger")` and spawning Debugger's loop thread.
2. `on_agent_response` executes `/clear @Debugger` **second**:
   - `clear_conversation("agent:debugger")` (`ui/agent_runtime/session.py:52`) **unconditionally pops `self._turn_attr.pop("agent:debugger", None)` BEFORE checking `rt.is_loop_active("agent:debugger")`** (`line 75`), wiping the dispatch attribution snapshot if `_prepare_turn_conversation` already wrote it, and then refuses the clear (`tool loop is active`).
3. Worse, if Supervisor pairs `/compact @Debugger` before `/ask @Debugger "..."`, `/ask` starts Debugger's background loop thread first and `/compact` runs second — and **`compact_conversation` (`ui/agent_runtime/session.py:133-240`) has NO `rt.is_loop_active` guard at all**, mutating `conv.messages` concurrently on the main thread while Debugger's `_run_loop` is reading/preparing `conv.messages`.

---

### Layer 4 — Provider Streaming Transport & Output-Cap Edge Cases

#### Finding 4A (`MEDIUM`): Hardcoded 4,096 `max_tokens` in `AnthropicProvider` & silent `finish_reason == "length"` truncation

**Files & lines:**
- `agent/llm/anthropic_provider.py:80, 134`
- `agent/llm/openai_provider.py:181-202`
- `agent/llm/minimax_provider.py:50-66`

**Mechanism:**
- `AnthropicProvider.call` and `AnthropicProvider.stream` hardcode `"max_tokens": 4096`.
- `OpenAIProvider.stream` and `MiniMaxProvider.stream` treat `finish_reason == "length"` identically to `"stop"`, emitting `SSEEvent(type="done", data={})` with no truncation indicator or warning.
- Because Supervisor and Debugger place `/ask @... "..."` at the **very end** of detailed specs/audits (and reasoning models sharing `max_tokens` with `<think>` output consume thousands of tokens before visible prose), hitting the output token ceiling silently truncates the message right before `/ask` (or cuts off `/ask` before the `@Agent "` prefix is reached).

---

#### Finding 4B (`LOW-MEDIUM`): Non-SSE HTTP 200 responses and structured `delta.content` list chunks

**Files & lines:**
- `agent/llm/streaming.py:60-81, 100-102`
- `agent/runtime.py:3781-3785, 3831`
- `ui/agent_runtime/turn_ui.py:64`

**Mechanism:**
1. **Non-SSE JSON response to `stream: True`:** Some OpenAI-compatible proxies/gateways ignore `"stream": True` for certain models and return a standard `application/json` ChatCompletion body with HTTP 200. `OpenAIProvider.stream` feeds every line to `parse_sse_line` (`streaming.py:60-70`), which returns `None` for any line not starting with `data:`, yielding `full_content == ""` and failing the turn with `Agent returned no content`.
2. **Structured `delta.content` list chunks:** In `parse_sse_delta` (`streaming.py:100-102`), `content = delta.get("content"); if content is not None:` emits `text_delta` unchanged even when an OpenAI-compatible provider returns `delta.content` as a `list[dict]` (e.g. `[{"type": "text", "text": "..."}]` on multimodal/reasoning endpoints). In `_call_llm_streaming` (`agent/runtime.py:3782-3785`), `text = ev.data.get("content") or ""` keeps the `list`, dispatches it to `_on_text_delta` (raising `TypeError: can only concatenate str (not "list") to str` inside `turn_ui.py:64`), and crashes `_call_llm_streaming` at `"".join(parts)` (`agent/runtime.py:3831`).

---

## 5. Reproducible Verification Probes (Read-Only)

Another agent can verify the deterministic parser, ordering, alias, and state-leak findings immediately from the repo root with these read-only one-liners:

### Probe 1 — Verify Layer 2A / 2C formatting drops & whitespace flattening
```bash
python3 -c '
from ui.handlers.agent_command_handler import _extract_quoted_commands, AgentCommandHandler
from ui.handlers.command_handler import CommandHandler
from ui.handlers.collab_handler import CollabHandler

ch = CommandHandler(agent_manager=None, project_handler=None, collab_handler=CollabHandler())
ch.set_special_agents({"agent:debugger": "Debugger", "agent:supervisor": "Supervisor", "agent:coder": "Coder"})

cases = [
    ("collab_md_inline_backtick", "`/ask @Debugger \"check this\"`"),
    ("html_fenced_card", "```html\n<div>Done</div>\n/ask @Debugger \"check this\"\n```"),
    ("markdown_bold", "**/ask @Debugger \"check this\"**"),
    ("bullet_no_space", "-/ask @Debugger \"check this\""),
    ("curly_quotes", "/ask @Debugger \u201ccheck this\u201d"),
    ("emdash_before_quote", "/ask @Debugger \u2014 \"check this\""),
    ("documented_alias_slash_a", "/a @Debugger \"check this\""),
    ("colon_after_mention", "/ask @Debugger: \"check this\""),
    ("unescaped_inner_quotes", "/ask @Debugger \"check \"foo\" in bar.py\""),
    ("backslash_newline", "/ask @Debugger \"line1 \\\nline2\""),
    ("think_unclosed_quote", "<think>Maybe /ask @Debugger \"draft</think>\n/ask @Debugger \"real question\""),
    ("multiline_flattened", "/ask @Debugger \"line1\n    indented_line2\""),
]
for name, sample in cases:
    clean = AgentCommandHandler._strip_fenced_blocks(sample)
    cmds = _extract_quoted_commands(clean, ch.get_command_names())
    res = None
    if cmds:
        pc = cmds[0]
        esc = pc.payload.replace("\\", "\\\\").replace("\"", "\\\"")
        cand = "/" + pc.command + " " + pc.agent + " \"" + esc + "\""
        res = ch.process_input("agent:supervisor", cand, skip_dispatch=True)
    fwd_to = getattr(res, "forward_to", None)
    fwd_txt = getattr(res, "forward_text", None)
    err_txt = getattr(res, "response_text", None)
    print(f"{name:27s} | cmds={[(c.command, c.agent, c.payload) for c in cmds]} | forward_to={fwd_to} forward_text={fwd_txt!r} err={err_txt!r}")
'
```

### Probe 2 — Verify Layer 3C (`_chain_depth` leak when `CommandHandler` is wired) & Layer 3D (`/clear` + `/ask` reverse execution order)
```bash
python3 -c '
from ui.handlers.agent_command_handler import _extract_quoted_commands, AgentCommandHandler
from ui.handlers.command_handler import CommandHandler
from ui.handlers.collab_handler import CollabHandler

# 3D: /clear + /ask order
paired = "/clear @Debugger\n/ask @Debugger \"please audit scope\""
print("3D extracted order:", [(c.command, c.raw_start) for c in _extract_quoted_commands(paired)])

# 3C: _chain_depth leak when command_handler IS wired (production path)
ach = AgentCommandHandler()
ch = CommandHandler(agent_manager=None, project_handler=None, collab_handler=CollabHandler())
ch.set_special_agents({"agent:debugger": "Debugger", "agent:supervisor": "Supervisor"})
ach.set_command_handler(ch)
ach._chain_depth["agent:debugger"] = 2
ach.on_agent_response("agent:debugger", "Plain response with no commands.", "develcakes")
print("3C _chain_depth after plain response (expected None, actual):", ach._chain_depth.get("agent:debugger"))
'
```

---

## 6. Diagnostic Checklist for the Second Computer (Read-Only)

When reproducing on the other machine, inspect these files (no code edits required) to pinpoint which finding fired:

1. **Inspect `~/.config/develcakes/conversations/agent:supervisor.json`:**
   - **Check the assistant message containing `/ask`:**
     - Wrapped in `` `/ask ...` ``, `**/ask ...**`, ```` ```html ````, curly quotes, `/a @Debugger`, `/ask @Debugger —`, or preceded by `<think>...</think>`? → **Findings 2A / 2B / 2C**.
     - Was `/ask @Debugger "..."` in an **earlier** assistant message that also had non-empty `tool_calls`, followed by a final assistant message in the same turn without `/ask`? → **Finding 1C**.
   - **Check for injected `[Action result]` user messages right after Supervisor's turn:**
     - `[Action result]: Unknown agent: @Debugger` → **Finding 3A** (stale boot-time `CommandHandler._special_agents`) or **Finding 2C.4** (`@Debugger:` punctuation).
     - `[Action result]: Could not clear debugger: a tool loop is currently running...` right after a `/clear` + `/ask` pair → **Finding 3D**.

2. **Inspect `~/.config/develcakes/conversations/agent:debugger.json`:**
   - **Last assistant message is `[tool loop closed — iteration_judge at 50 iterations, 0 extension(s) granted...]`:** → **Finding 1A** (confirm whether `DecisionModel` is missing/broken in `~/.config/develcakes/providers.yaml` on that computer).
   - **Last assistant message is `[LLM returned no content — provider error or malformed response]` immediately after a tool-calling assistant message that already contained Debugger's prose:** → **Finding 1B**.
   - **User message in Debugger's history is `[room] Supervisor: ...` rather than `[Supervisor asks]: ...`:** → **Finding 3B.2** + **Layer 2** (Supervisor's `/ask` didn't parse, but the room witness copied it into Debugger's history).
   - **User message `[Supervisor asks]: ...` arrived immediately adjacent to a prior Debugger turn completing:** → **Finding 3B.1** (`_pending_asks` popped by the prior turn).

3. **Check application logs for `[agent-cmd]`:**
   - `Chain depth limit (3) reached for ... — dropping commands` → **Finding 3C**.
