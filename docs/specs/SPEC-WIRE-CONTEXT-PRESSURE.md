# SPEC: Wire-sized context pressure

**Date:** 2026-10-22
**Author:** Micah (handoff spec)
**Status:** Draft — for implementation
**Implements:** none (no proposal doc)
**Depends on:** SPEC-24 (compiled wire), SPEC-40 (hot windows), the context meter in `ui/window.py`
**Target branch:** main

> Architecture: `Conversation.get_token_estimate()` stays the transcript counter in `models/conversation.py`. `AgentRuntime` owns the wire list (`_messages_for_model` / `_compile`) and the compaction trigger. `AgentRuntimeHandler._last_breakdown` stays the per-session cache. `MainContent` owns the one meter widget. This spec does not move compaction into `models/` and does not give the meter a second widget.

DISCOVERY:
- Read `agent/runtime.py`: `_compact_before_llm(session_key, conv, soft_ceiling, hard_ceiling)` panics when `conv.get_token_estimate() > soft_ceiling` and `soft_ceiling > landing_goal`, else calls `compact(conv, soft_ceiling)` with no kwargs. `_messages_for_model(session_key, conv, model_max, tools=None)` builds the list actually sent and raises `_over_budget_error` when its count `>= model_max - _RESPONSE_RESERVE_TOKENS` (`_RESPONSE_RESERVE_TOKENS = 4096`). Compiled packets live in `self._compile[session_key]` with keys `goal`, `path`, `hunk`, `checks`, `after`, `wire_from`, `hot`. `drop_compile` is only called from clear. A new user turn does not drop the packet. `_run_loop` compacts, then loads tools, then calls `_messages_for_model`, then after the response calls `conv.get_token_breakdown(model_max)` and dispatches it. `send_message(session_key, text, prepare=None)` always starts a thread with `self._turn_token`. `is_loop_active` is `session_key in self._active_loops` under `self._lock`. `_run_loop` adds that set before its `try` and discards it in `finally`. `_terminate_turn` rejects a second terminal for the same `(session_key, turn_token)`.
- Read `models/conversation.py`: `get_token_estimate` caches on `(len(messages), hash(system_prompt), hash(task_card))` and writes the cache after counting. `get_token_breakdown(model_max_tokens)` returns `system_prompt_tokens`, `conversation_tokens`, `total_used_tokens`, `model_max_tokens`, `remaining_tokens`, `usage_percent`. The chars//4 path does not use the cache.
- Read `agent/context_strategy.py`: `compact(conv, token_budget, *, keep_first=2, protect_is_summary=True, protect_paths=(), preserve_current_turn=False)`. `prune_tool_outputs` returns 0 immediately when `get_token_estimate() <= target_tokens`. `panic_prune(conv, protect_paths=())`. The trim stop condition is `get_token_estimate() > token_budget`.
- Read `ui/agent_runtime/session.py`: `send_to_special_agent` writes `_turn_reply_target`, then loads the runtime, then publishes a new handler turn token, then calls `rt.send_message`. Runtimes are one per agent display name (`_get_runtime`).
- Read `ui/window.py`: `_on_context_meter(sk, breakdown)` always calls `set_context_meter` and `_update_agent_display(sk)`. Tab change calls `_update_agent_display` only. `get_last_breakdown` is never read by the UI.
- Read `ui/views/main_content.py`: `get_current_session_key()` returns the active tab's session key or `None`. `set_context_meter(session_key, usage_percent)` ignores `session_key` for filtering. `usage_percent < 0` clears the bar.
- Read `ui/handlers/agent_runtime_handler.py`: `_on_token_breakdown` caches `_last_breakdown[session_key]` and logs `total_used_tokens` / `usage_percent`. The extra listener runs inside that callback. The runtime dispatches the callback through `_dispatch`, which uses `GLib.idle_add` when GLib is set, so the listener runs on the GTK thread.
- Read `tests/test_panic_prune.py` and `tests/test_context_strategy_tier0.py`: under-threshold tests replace `get_token_estimate` with `lambda: 1_000` and assert `compact(conv, soft)` with `kwargs == {}` and no panic. Over-threshold tests use `lambda: 10_000_000` and assert panic plus `compact(..., landing_goal, preserve_current_turn=True)`.
- Read `tests/test_spec24_compiled_turn.py`: `test_compiled_budget_counts_the_task_card_once_with_one_estimator` pins that `_compile_token_count` charges `conv.task_card` once, on the same estimator as the wire, even though `_compiled_messages` does not emit the card.
- Architecture owner: wire list and compaction trigger — `AgentRuntime`. Transcript estimate — `Conversation`. Meter widget — `MainContent`. Per-session breakdown cache — `AgentRuntimeHandler`.
- Existing patterns to copy: hot-drop and the `>= effective` raise inside `_messages_for_model`; bool rejection on `wire_from` (`isinstance(wire_from, int) and not isinstance(wire_from, bool)`); `_active_loops` updates under `self._lock`; turn-token publish on the handler before the loop can dispatch UI.

---

## 1. Overview

The context meter, the compaction trigger, and the OpenRouter request are three different numbers. The meter and `_compact_before_llm` use `Conversation.get_token_estimate()`, which counts every stored message. After an enforced `write_file` or `edit_file`, `_messages_for_model` sends a compiled packet plus `conv.messages[wire_from:]` and leaves the earlier transcript in memory. That packet stays until `/clear`. Compaction still treats the full transcript as pressure. Crossing 80% of the window panic-compacts toward `min(15% of the window, 24_000)` even when the request is small.

The meter makes this worse with two coders. There is one bar. Every breakdown overwrites it, and the same callback changes the name beside the bar to whichever agent just finished an iteration. The visible tab does not get its own cached percent back. `get_last_breakdown` already stores one dict per session key and nothing reads it.

A second `send_message` on a session whose loop is already running starts a second `_run_loop` on the same `Conversation`. Both threads compact and both call `get_token_estimate`. The cache key ignores message bodies, and the count is stored after it is computed, so a stub can clear the cache and then lose the race to the pre-stub total. Later reads stay high. Two coders each have their own runtime (keyed by display name) and their own session key. The overlap is two sends to one session, which is what happens when both coders message the Supervisor.

### Solution

1. Compaction pressure is the token count of the list `_messages_for_model` would send, including the hot-window drop it already does. When that count is at or under `soft_ceiling`, do not panic and do not trim. Still call `compact` with a budget that cannot trim, so `last_result` is a fresh no-op instead of the previous iteration's event.
2. The breakdown's `usage_percent` and `total_used_tokens` become that same wire count. The transcript total is kept on `transcript_tokens`.
3. The meter updates only when the breakdown's session key is the visible tab. Tab switches load `get_last_breakdown` for that tab.
4. One loop per session key. A send that arrives while that session's loop is active is queued and appended with `add_user_message` on that loop's thread. It does not publish a new handler turn token and it does not start a second thread. Text that arrives after the turn has already finished is saved and started as a normal new turn from the completion (or error) callback, after that callback has accepted the old token. Two idle coders still start two loops.

### Scope

| In | Out |
|---|---|
| Wire count as the compaction trigger and the meter percent | Changing `_compiled_messages` bytes |
| Meter follows the visible tab | A second meter, or a meter for `project:` tabs |
| One `_run_loop` per session key | Merging coder runtimes or session keys |
| Token-cache generation so a late write cannot stick | Rewriting `compact()` to skip indexes below `wire_from` |
| Tests listed in §6 | Bubble routing in `_resolve_chat_box` |

### Why compact() is not taught about `wire_from`

`_compile[session_key]["after"]` and `wire_from` are absolute indexes into `conv.messages`. Deleting a message below `after` makes the next `_compiled_messages` slice the wrong range. Keeping the off-wire audit log while trimming only the tail requires repairing those indexes on every pop. That is a separate change. This spec stops the panic when the wire is under the ceiling. When the wire itself is over the ceiling, the existing full-transcript panic stays.

### Group chat

`_get_runtime(name)` is one `AgentRuntime` per display name. A project send calls `send_to_special_agent` once per member, each with its own session key. `begin_send` claims `_active_loops` per session key. Coder and Alpha Coder both run. A broadcast to two idle coders starts both loops. A second message to a coder who is already in a turn queues on that coder only.

### Architecture principles

- The transcript remains the audit log. The wire list remains what the model sees.
- `usage_percent` means the next request, not the audit log.
- Turn tokens stay one active token per session. A queued send does not rotate the token of the running turn. A new token is published only by `send_to_special_agent` on the GTK thread, after `begin_send` has claimed an idle session.
- `self._lock` remains the lock for `_active_loops`. The new inbox uses that same lock. `_terminate_turn` still must not take `self._lock` (it can run while a caller holds it).

---

## 2. Changes by File

### 2.1 `agent/runtime.py`

#### `_wire_messages_and_count`

New method. Same arguments as `_messages_for_model`:

```python
def _wire_messages_and_count(
    self,
    session_key: str,
    conv,
    model_max: int,
    tools: list | None = None,
) -> tuple[list[dict], int]:
```

Behavior, in order, copied from today's `_messages_for_model` (do not fork the rules):

1. `block = self._repo_map_block_for(session_key, conv)`. If `getattr(conv, "agent_role", "") == "coder"`, set `block = ""`.
2. `packet = self._compile.get(session_key)`.
3. Plain branch (`packet is None`):
   - `schema_tokens = self._schema_tokens(conv, tools)`.
   - If `block` is non-empty, `map_text = REPO_MAP_HEADER + "\n\n" + block + "\n\n"`, else `map_text = ""`.
   - `count = conv.get_token_estimate() + self._addon_tokens(conv, map_text) + schema_tokens`.
   - `messages = self._prefix_repo_map(conv.to_api_messages(), block)`.
   - Return `(messages, count)`. Do not raise.
4. Compiled branch:
   - `messages = self._compiled_messages(conv, packet, block)`.
   - `count = self._compile_token_count(conv, messages, tools)`.
   - `effective = model_max - self._RESPONSE_RESERVE_TOKENS`.
   - If `count >= effective` and `packet.get("hot")`: rebuild with `hot=False` and recount. Same condition as today.
   - Return `(messages, count)`. Do not raise.

`_compile_token_count` keeps charging `conv.task_card` once. Do not remove that addon. `tests/test_spec24_compiled_turn.py::test_compiled_budget_counts_the_task_card_once_with_one_estimator` pins it. The card is at most 500 characters (`_note_task`). It is not the bug.

#### `_messages_for_model`

Becomes a wrapper. Signature unchanged. Return type stays `list[dict]`.

```python
def _messages_for_model(self, session_key, conv, model_max, tools=None):
    messages, count = self._wire_messages_and_count(
        session_key, conv, model_max, tools,
    )
    if count >= model_max - self._RESPONSE_RESERVE_TOKENS:
        raise self._over_budget_error(count, model_max)
    return messages
```

`_over_budget_error(used, model_max)` already returns a `RuntimeError`. Callers that catch it keep working. The comparison stays `>=`, matching today.

#### `_compact_before_llm`

Add a keyword argument at the end so existing positional calls keep working:

```python
def _compact_before_llm(
    self,
    session_key: str,
    conv,
    soft_ceiling: int,
    hard_ceiling: int,
    tools: list | None = None,
) -> None:
```

`hard_ceiling` is the model max (`_run_loop` sets `model_max = hard_ceiling`). Pressure:

```python
_messages, pressure = self._wire_messages_and_count(
    session_key, conv, hard_ceiling, tools,
)
```

The list is discarded. Compaction may change `conv.messages`, so the list must be built again after compaction. Do not cache it on `self`.

Replace the trigger. `landing_goal = self._compute_landing_goal(hard_ceiling)` stays.

```python
if pressure <= soft_ceiling:
    # Fresh no-op last_result. Budget >= the transcript estimate means
    # prune_tool_outputs returns 0 and the trim loop does not run.
    # max(..., soft_ceiling) keeps the under-threshold call at `soft`
    # when the transcript is already under the line (existing tests).
    ceiling = max(soft_ceiling, conv.get_token_estimate())
    self._context_strategy.compact(conv, ceiling)
    return
if soft_ceiling > landing_goal:
    protect = self._protect_paths(session_key, conv)
    self._context_strategy.panic_prune(conv, protect_paths=protect)
    self._context_strategy.compact(
        conv,
        landing_goal,
        protect_paths=protect,
        preserve_current_turn=True,
    )
    return
self._context_strategy.compact(conv, soft_ceiling)
```

Trace against `tests/test_context_strategy_tier0.py::test_compact_under_threshold_uses_soft_ceiling`: no compile packet, monkeypatched estimate `1_000`, no user goal so `_repo_map_block_for` returns `""`, `tools is None` so schema tokens are 0. `pressure == 1_000`. `soft_ceiling` is 80% of the provider window, which is greater than 1_000. Branch 1 runs. `ceiling = max(soft, 1_000) == soft`. `compact(conv, soft)` with no kwargs. Panic is not called. The test's `kwargs == {}` still holds.

Trace against `tests/test_panic_prune.py::test_over_threshold_lands_on_the_goal`: estimate `10_000_000`, no packet, pressure is that large, `pressure > soft_ceiling`, panic path, `compact` budget is `landing_goal`, `preserve_current_turn` is `True`.

When a compile packet exists and the wire count is under `soft_ceiling` while `get_token_estimate()` is over it, branch 1 runs. `ceiling` is the transcript estimate. `prune_tool_outputs` hits `tokens_before <= target_tokens` and returns 0. The trim `while` does not run. Off-wire messages stay. `last_result.messages_removed` is 0 and `tokens_freed` is 0, so `_run_loop` does not treat this as a compaction.

`panic_prune` and `compact` signatures are unchanged. Do not add `not_before`.

#### `_run_loop` tool order and breakdown

Move the tool-resolution block that today sits after compaction (the `get_tool_definitions_for_api(conv.allowed_tools)` call and the `conv.mcp_servers` / `get_tools_for_api` block) to immediately before `_compact_before_llm`. That block reads `conv.allowed_tools` and `conv.mcp_servers`. It does not read `conv.messages`. The MCP `except` stays a `logger.warning` and does not fail the turn.

Then:

```python
self._compact_before_llm(
    session_key, conv, soft_ceiling, hard_ceiling, tools=tools,
)
```

After compaction, build the wire once:

```python
messages, wire_tokens = self._wire_messages_and_count(
    session_key, conv, model_max, tools,
)
if wire_tokens >= model_max - self._RESPONSE_RESERVE_TOKENS:
    raise self._over_budget_error(wire_tokens, model_max)
```

Pass `messages` to `_call_llm` as today. Keep `wire_tokens` in a local. Do not recompute it after `add_assistant_message`. OpenRouter `prompt_tokens` is the request, not the request plus the completion.

Where the breakdown is built (`conv.get_token_breakdown(model_max)`), overwrite the request fields. Leave the transcript on its own key:

```python
breakdown = conv.get_token_breakdown(model_max)
breakdown["transcript_tokens"] = breakdown["total_used_tokens"]
system_tokens = breakdown["system_prompt_tokens"]
breakdown["total_used_tokens"] = wire_tokens
breakdown["conversation_tokens"] = max(0, wire_tokens - system_tokens)
breakdown["remaining_tokens"] = max(0, model_max - wire_tokens)
breakdown["usage_percent"] = (
    round(wire_tokens / model_max * 100, 1) if model_max > 0 else 0
)
```

`trimmed_this_turn`, `messages_remaining`, `messages_removed_this_turn`, and `compaction_event` stay as they are. Warnings at 80% and 95% in the handler read `usage_percent`, so they follow the wire.

Do not switch the meter to `extract_usage`'s `prompt_tokens`. Anthropic `input_tokens` omits cache reads. The trigger is the local wire count.

#### One loop per session

Do not publish a new turn token from the loop thread. `_do_response_complete` (`ui/agent_runtime/turn_ui.py`) drops a completion whose token is not `self._turn_tokens[session_key]`. A follow-up that rotated the token before that idle callback ran would discard the reply that just finished. The loop thread only queues. The GTK thread starts the next turn, and only after the old completion has passed its token check.

New state, initialized in `AgentRuntime.__init__` next to `_active_loops`:

```python
self._inbox: dict[str, list[str]] = {}
self._stranded: dict[str, list[str]] = {}
```

`_inbox` is text for the running loop. `_stranded` is text that arrived too late for that loop and is waiting for a new turn.

```python
def begin_send(self, session_key: str, text: str) -> bool:
    """Return True when the caller must start the loop.

    On True, `session_key` is already in `_active_loops`. Any stranded
    texts are moved to `_inbox` so the loop drains them before it appends
    `text`. On False, `text` (and any stranded texts) were appended to the
    running loop's inbox. The caller must not publish a turn token.
    """
    with self._lock:
        prior = self._stranded.pop(session_key, [])
        if session_key in self._active_loops:
            bucket = self._inbox.setdefault(session_key, [])
            bucket.extend(prior)
            bucket.append(text)
            return False
        self._active_loops.add(session_key)
        if prior:
            self._inbox[session_key] = prior
        return True

def take_stranded(self, session_key: str) -> list[str]:
    """Pop texts that missed the last loop. Empty when there are none."""
    with self._lock:
        return self._stranded.pop(session_key, [])
```

```python
def start_claimed_loop(
    self,
    session_key: str,
    text: str,
    turn_token: object,
    prepare: Callable[[], None] | None = None,
) -> None:
    try:
        threading.Thread(
            target=self._run_loop,
            args=(session_key, text, turn_token, prepare),
            daemon=True,
        ).start()
    except Exception:
        with self._lock:
            self._active_loops.discard(session_key)
            self._inbox.pop(session_key, None)
            self._stranded.pop(session_key, None)
        raise
```

`Thread.start` raises `RuntimeError` if the thread cannot be created. The `except` releases the claim. The primary `text` was not queued, so a failed start does not leave a hidden copy. The exception propagates to `send_to_special_agent`.

`send_message` keeps its signature:

```python
def send_message(self, session_key: str, text: str, prepare: Callable[[], None] | None = None) -> None:
    token = self._turn_token
    if not self.begin_send(session_key, text):
        return
    self.start_claimed_loop(session_key, text, token, prepare)
```

Return value stays `None`.

`_run_loop` still does `self._active_loops.add(session_key)` at the top. Direct test calls do not go through `begin_send`, so that add stays.

Replace the `finally` discard with `_finish_loop`. It always releases the claim. Texts still in the inbox become stranded. It does not start another thread.

```python
finally:
    self._finish_loop(session_key)

def _finish_loop(self, session_key: str) -> None:
    with self._lock:
        pending = self._inbox.pop(session_key, [])
        self._active_loops.discard(session_key)
        if pending:
            self._stranded[session_key] = pending
```

Direct `_run_loop` calls with an empty inbox still leave `is_loop_active` false.

```python
def _drain_inbox(self, session_key: str, conv) -> int:
    with self._lock:
        pending = self._inbox.pop(session_key, [])
    for item in pending:
        conv.add_user_message(item)
    return len(pending)
```

`add_user_message` runs outside the lock. After §2.2 it also bumps the token generation.

Two drain points, in this order:

1. After the conversation is loaded and **before** the existing `conv.add_user_message(text)`. This is the stranded list `begin_send` parked for a new turn. Those messages are older than `text`.
2. At the top of the `while iteration` loop, after the cancel checks and before `_compact_before_llm`. This is text that arrived during the turn. It is newer than the turn's own user message. Same turn token. `prepare` is not called again.

On the text-only success path, immediately before `_terminate_turn(TurnStatus.COMPLETED)`:

```python
if self._drain_inbox(session_key, conv):
    continue
```

`continue` stays in this `_run_loop`. Do not complete on that pass. A second COMPLETED for the same `(session_key, turn_token)` is rejected and the reply never reaches the UI.

Do not `continue` on FAILED paths. `_finish_loop` turns whatever is still in the inbox into `_stranded`. The error callback on the GTK thread starts a new turn for it (§2.6), after the error has passed its own token check.

`cancel()` already holds `self._lock`. Inside that block, also:

```python
self._inbox.pop(session_key, None)
self._stranded.pop(session_key, None)
```

Do not discard `_active_loops` inside `cancel`. The loop's `finally` does that.

#### Imports

`Callable` is already imported in `agent/runtime.py`. `threading` is already imported. No new import.

Line count: about +160.

### 2.2 `models/conversation.py`

Add a field next to `_token_estimate_cache`:

```python
_token_gen: int = field(default=0, repr=False, compare=False)
```

```python
def invalidate_token_estimate(self) -> None:
    """Drop the cached estimate and bump the generation.

    get_token_estimate writes the cache only when the generation is unchanged
    from the start of the count. A stub that lands during the count cannot
    store the pre-stub total under the post-stub key.
    """
    self._token_estimate_cache = None
    self._token_gen += 1
```

In `get_token_estimate`, on the tiktoken path only (the chars//4 path does not cache):

```python
gen = self._token_gen
result = self._count_tokens_accurate(encoding)
if gen == self._token_gen:
    self._token_estimate_cache = (cache_key, result)
return result
```

`cache_key` is still computed before the count, as today. If `gen` changed, this call still returns `result` (it may include a body that was stubbed mid-count). It does not store it. The next call recomputes. The compact loop already invalidates before it recounts, so the single-threaded loop is unchanged.

Replace every `conv._token_estimate_cache = None` and `self._token_estimate_cache = None` on a `Conversation` with `invalidate_token_estimate()`. Sites verified by search:

| File | What |
|---|---|
| `models/conversation.py` | `add_user_message`, `add_assistant_message`, `add_tool_result` |
| `agent/context_strategy.py` | after each trim pop, after the orphan sweep, after each `prune_tool_outputs` stub, after each `panic_prune` stub |
| `agent/runtime.py` | `_ensure_system_prompt`, `_rebuild_system_prompt_if_inputs_changed` (both the success write and leave the failure path alone — it restores the previous prompt and does not clear today), `_note_task`, `_rebuild_conversation_context` success path |
| `agent/context_edit.py` | `persist_conversation` `finally`, only in the `if swapped` branch that already clears |
| `ui/agent_runtime/session.py` | `clear_conversation` reset, and the two clears in the compact/edit path around the existing assignments |

`force_llm_compact` restores `conv.system_prompt` in `finally` and does not clear the cache. The cache key includes `hash(system_prompt)`, so the enlarged-prompt entry is not reused. Also call `conv.invalidate_token_estimate()` after the restore so the temporary entry cannot match a later hash collision with the restored string. One call, in the `finally`, after the prompt is restored.

No new imports. Line count: about +25.

### 2.3 `ui/agent_runtime/session.py`

`send_to_special_agent` today writes `_turn_reply_target` and then publishes a new token before `send_message`. A queued send must do neither. The running turn keeps its reply target and its token, so its deltas still match `self._turn_tokens`.

After the no-project guard (that guard stays first and still clears `_turn_reply_target` before the error), obtain the runtime, then claim:

```python
rt = self._get_runtime(agent_def.display_name, agent_def=agent_def)
if not rt.begin_send(session_key, text):
    return
```

`_get_runtime` raises `RuntimeError` when no provider or no API key is configured. That raise now happens before the reply-target write. A failed lookup no longer leaves a new reply target on a send that did not start. That is the intended order.

Only when `begin_send` returns `True`:

1. Set `_turn_reply_target[session_key]` exactly as today (`reply_target` if it is not `None`, else `_resolve_mount_key(session_key)`).
2. Discard `_ended_sessions` and `_session_completed` for this key.
3. `new_token = object()`, store it on `self._turn_tokens[session_key]` and `rt._turn_token`.
4. Build the same `_prepare_turn` closure `send_message` receives today.
5. Call `rt.start_claimed_loop(session_key, text, new_token, _prepare_turn)`.

Do not call `rt.send_message` from this method. `send_message` would `begin_send` again. The session is already claimed.

There is no loop-thread token callback. The next turn, if any, is started in §2.6 from the GTK completion and error callbacks.

Line count: about +35.

### 2.4 `ui/handlers/agent_runtime_handler.py`

Extend the log line so a later mismatch is visible. Use `.get` for the new key so a test dict that omits it does not raise inside the log (the empty-dict test still fails on the existing required keys and is caught):

```python
logger.info(
    "[token-breakdown] sk=%s system_prompt=%d conv=%d total=%d/%d "
    "remaining=%d (%.1f%%) transcript=%d trimmed=%s removed=%d",
    session_key,
    breakdown["system_prompt_tokens"],
    breakdown["conversation_tokens"],
    breakdown["total_used_tokens"],
    breakdown["model_max_tokens"],
    breakdown["remaining_tokens"],
    breakdown["usage_percent"],
    breakdown.get("transcript_tokens", breakdown.get("total_used_tokens", 0)),
    breakdown.get("trimmed_this_turn", False),
    breakdown.get("messages_removed_this_turn", 0),
)
```

`get_last_breakdown` stays as it is: return `self._last_breakdown.get(session_key)`.

Do not change `_resolve_chat_box` or `_do_compaction_bubble`.

Line count: about +2.

### 2.5 `ui/window.py`

`_on_context_meter` updates the bar only for the visible tab and does not change the agent name:

```python
def _on_context_meter(sk: str, breakdown: dict) -> None:
    visible = self._main_content.get_current_session_key()
    if sk != visible:
        return
    usage_pct = breakdown.get("usage_percent", 0.0)
    self._main_content.set_context_meter(sk, usage_pct)
```

Remove the `_update_agent_display(sk)` call from this function. The name stays with the tab.

`_update_agent_display` is the tab-change callback. After the canvas/welcome early return, load that tab's cached breakdown. Change the early return so those tabs clear the bar instead of leaving the previous agent's percent up:

```python
def _update_agent_display(sk: str):
    if isinstance(sk, str) and sk.startswith(("canvas:", "welcome:")):
        self._main_content.set_context_meter(sk, -1)
        return
    agent_name, agent_color = _resolve_agent_info(sk)
    if agent_name:
        self._main_content.update_agent_context_display(agent_name, agent_color or "#6366f1")
        self.update_agent_id_display(agent_name)
    else:
        self.update_agent_id_display("—")
    handler = self._agent_runtime_handler
    breakdown = handler.get_last_breakdown(sk) if handler is not None else None
    if breakdown is None:
        self._main_content.set_context_meter(sk, -1)
    else:
        self._main_content.set_context_meter(
            sk, breakdown.get("usage_percent", 0.0),
        )
```

`set_context_meter` treats `usage_percent < 0` as idle (fraction 0, empty label). `get_current_session_key` returns `None` when there is no current page. `sk != None` is true for an agent key, so a breakdown does not paint the bar when nothing is selected.

A `project:<name>` tab has no breakdown of its own. Switching to it clears the bar. Coder breakdowns do not paint it, because their keys are `agent:<slug>`. The coder's percent is on the coder's tab.

`set_on_session_changed(_update_agent_display)` stays. `MainContent` only calls it when `new_sk` is truthy (`ui/views/main_content.py`, the switch handler).

Line count: about +20.

### 2.6 `ui/agent_runtime/turn_ui.py`

`_do_response_complete` and `_do_error` live on `RuntimeTurnUiMixin`. Both reject a stale token before their `try`. Both pop `_turn_reply_target` in `finally` only when `self._turn_tokens.get(session_key) is my_token`.

After the `try`/`finally`, not inside the `try`, call:

```python
self._flush_stranded_sends(session_key)
```

A `return` inside the `try` runs `finally` and then returns, so a duplicate completion does not flush. A stale-token `return` is before the `try`, so it does not flush either. The flush runs only for the callback that accepted this token.

```python
def _flush_stranded_sends(self, session_key: str) -> None:
    rt = self.runtime_for(session_key)
    if rt is None:
        return
    for text in rt.take_stranded(session_key):
        self.send_to_special_agent(session_key, text)
```

`runtime_for` (`AgentRuntimeHandler`) returns `None` for a non-`agent:` key, a missing agent, or a `_get_runtime` failure (it logs and swallows that failure). `send_to_special_agent` is on `RuntimeSessionMixin`. Both mixins are bases of `AgentRuntimeHandler`, so both calls resolve on `self`.

The first text finds the session idle, because `_finish_loop` already removed it from `_active_loops`. That call publishes a new token and starts one loop. Further texts in the same `for` see the session active and queue. The old completion's token check has already passed, because this flush is after that check.

Do not flush inside the `try`. The `finally` must pop the old reply slot before `send_to_special_agent` writes the new one.

Line count: about +25.

### 2.7 `tests/test_wire_context_pressure.py` (new)

No GTK. Use `AgentRuntime` the same way `tests/test_panic_prune.py` does (`_make_cfg`, `create_conversation`, monkeypatch `compact` / `panic_prune`).

1. **Compiled wire under the ceiling does not panic.** Build a conversation whose `get_token_estimate` is large (monkeypatch to `10_000_000` after a real `_compile[sk]` packet is installed, or leave the real estimate large and make the packet's wire small). `tools=None`. Assert `panic_prune` is not called. Assert `compact` is called once with a budget `>= 10_000_000` and without `preserve_current_turn=True`. Assert the pre-`wire_from` message body is still present.
2. **Compiled wire over the ceiling still panics.** A packet whose `_compile_token_count` is over `soft_ceiling` (huge `goal` or huge hot body) takes the existing panic path: `preserve_current_turn is True`, budget `== _compute_landing_goal(hard)`.
3. **Plain under-ceiling call is unchanged.** Estimate `1_000`, no packet. `compact` budget equals `soft`, kwargs empty, panic not called. This can live in the new file as a duplicate of the tier0 test or the tier0 test can stay the pin. Do not delete the tier0 test.
4. **`begin_send` while active queues.** Call `begin_send` once (True). Call it again with `"second"` (False). Assert `rt._inbox[sk] == ["second"]` and `is_loop_active(sk)` is true. Call `_finish_loop`. Assert `is_loop_active(sk)` is false and `rt.take_stranded(sk) == ["second"]`. A second `take_stranded` returns `[]`.
5. **Drain appends in order.** `begin_send` claims. Manually append `"a"` and `"b"` to `_inbox[sk]`. `_drain_inbox` on a real conversation. Assert the two new user messages are `"a"` then `"b"`, and a second drain returns 0.
6. **Generation drops a stale store.** On a tiktoken-capable model string (`"openai/gpt-4o"`), start `get_token_estimate` logic by calling `invalidate_token_estimate` between a manual count and the cache write is not required if a direct test does: call `get_token_estimate`, then replace a tool-result body with a short stub without going through `invalidate_token_estimate` and show the cache can stick (that is the bug). Then stub through `invalidate_token_estimate` and show the next `get_token_estimate` dropped. The assertion is: after `invalidate_token_estimate`, the following `get_token_estimate()` is within a small delta of the stubbed transcript, not the pre-stub total.

Install a compile packet in tests with the real keys, not a partial dict:

```python
rt._compile[sk] = {
    "goal": "small goal",
    "path": "a.py",
    "hunk": "hunk",
    "checks": [],
    "after": len(conv.messages),
    "wire_from": wire_from,  # index after the goal user message
    "hot": (),
}
```

`wire_from` must be an `int`, not `True`. `_compiled_messages` rejects bools because `bool` is a subclass of `int`.

Line count: about +200.

### Files NOT changed

- `agent/context_strategy.py` method signatures — `compact` / `panic_prune` / `prune_tool_outputs` stay. Only their cache clears call `invalidate_token_estimate`.
- `ui/views/main_content.py` `set_context_meter` — the negative-percent idle path is already correct. The filter lives in the window callback, which knows the visible tab.
- `_resolve_chat_box`, `_do_compaction_bubble`, `_do_usage_warning` — still route by session key and reply target.
- `drop_compile` — still only on clear. A new turn still keeps the packet.
- `_compile_token_count`'s task-card addon.
- `_get_runtime`'s key (display name). Group fan-out loops that call `send_to_special_agent` per member.
- `extract_usage` — still returns `(prompt_tokens, completion_tokens)` or Anthropic `(input_tokens, output_tokens)`. Not the meter.

---

## 3. Data Flow

### Compaction and the meter

1. `_run_loop` loads tools (`get_tool_definitions_for_api`, then MCP `get_tools_for_api` when `conv.mcp_servers` is non-empty).
2. `_compute_compaction_threshold(conv)` returns `(soft_ceiling, hard_ceiling)`.
3. `_compact_before_llm` calls `_wire_messages_and_count`. Plain sessions count `get_token_estimate()` plus repo-map addon plus schema JSON. Compiled sessions count `_compile_token_count` of `_compiled_messages`, after the same hot drop `_messages_for_model` does.
4. If that count is `<= soft_ceiling`, `compact(conv, max(soft_ceiling, get_token_estimate()))` records a no-op and returns. The off-wire transcript is not trimmed.
5. If that count is over `soft_ceiling` and `soft_ceiling > landing_goal`, `panic_prune` plus `compact(..., landing_goal, preserve_current_turn=True)` run, same as today.
6. `_wire_messages_and_count` runs again. If the count is `>= hard_ceiling - 4096`, `_over_budget_error` raises and the turn fails through the existing `except` in `_run_loop`.
7. `_call_llm` sends that list.
8. The breakdown copied from `get_token_breakdown` has `total_used_tokens`, `conversation_tokens`, `remaining_tokens`, and `usage_percent` replaced from `wire_tokens`. `transcript_tokens` keeps the transcript total.
9. `_dispatch` → `_on_token_breakdown` stores `_last_breakdown[session_key]` and calls `_on_context_meter`.
10. `_on_context_meter` paints the bar only when `session_key == get_current_session_key()`.
11. A tab switch calls `_update_agent_display(new_sk)`, which reads `get_last_breakdown(new_sk)` and paints that percent, or clears the bar when the cache is empty.

### A second message while a coder is running

1. `send_to_special_agent` passes the no-project guard, gets that coder's runtime, calls `begin_send`.
2. The coder's session is in `_active_loops`, so the text is appended to `_inbox[session_key]` and `begin_send` returns `False`. The handler returns. The handler turn token and `_turn_reply_target` stay as they were.
3. At the next iteration, or immediately before COMPLETED, `_drain_inbox` calls `add_user_message` for each queued string on the loop thread.
4. If the drain happens before COMPLETED, the loop `continue`s. One turn, one token, one completion.
5. If the text arrives after that drain and during `_terminate_turn`, `_finish_loop` moves it to `_stranded` and drops the session from `_active_loops`. It does not start a thread and it does not change the handler token.
6. After `_do_response_complete` or `_do_error` has accepted the old token and its `finally` has run, `_flush_stranded_sends` calls `take_stranded` and `send_to_special_agent` once per text. The first call finds the session idle, publishes a new token, and starts one loop. The rest find the session active and queue. The old completion already passed `complete_token is current_token` before the new token existed.
7. The other coder's runtime is a different object. Its `_active_loops` does not contain this session key. Its `begin_send` returns `True` and starts its own thread.

### Keys

| Dict | Lock | Key | Value |
|---|---|---|---|
| `AgentRuntime._conversations` | `_lock` for replace; reads are unlocked today | `session_key` (`agent:<slug>`) | `Conversation` |
| `AgentRuntime._compile` | none (whole-dict replace per key) | `session_key` | packet dict (`goal`, `path`, `hunk`, `checks`, `after`, `wire_from`, `hot`) |
| `AgentRuntime._active_loops` | `_lock` | set of `session_key` | — |
| `AgentRuntime._inbox` | `_lock` | `session_key` | `list[str]` in arrival order |
| `AgentRuntime._stranded` | `_lock` | `session_key` | `list[str]` left by `_finish_loop` for the GTK thread |
| `AgentRuntimeHandler._last_breakdown` | none (GTK thread via `_dispatch`) | `session_key` | breakdown dict |
| `AgentRuntimeHandler._turn_tokens` | GTK thread only | `session_key` | `object()` token |
| `AgentRuntimeHandler._runtimes` | none | **display name**, not session key | `AgentRuntime` |

`send_to_special_agent` is the only writer of the handler turn token, and only after `begin_send` returns `True`. A queued send does not write it. The loop thread does not write it.

---

## 4. File Change Summary

| File | Change | Lines (about) | Risk |
|---|---|---|---|
| `agent/runtime.py` | Wire count, compaction trigger, single-flight queue | +160 | High |
| `models/conversation.py` | Estimate generation | +25 | Medium |
| `agent/context_strategy.py` | Cache clears call `invalidate_token_estimate` | +4 | Low |
| `agent/context_edit.py` | Same cache clear | +1 | Low |
| `ui/agent_runtime/session.py` | Claim before token publish | +30 | High |
| `ui/agent_runtime/turn_ui.py` | Flush stranded texts after completion and error | +25 | High |
| `ui/handlers/agent_runtime_handler.py` | Log `transcript_tokens` | +5 | Low |
| `ui/window.py` | Meter follows the visible tab | +20 | Medium |
| `tests/test_wire_context_pressure.py` | New | +200 | Low |
| `docs/ARCHITECTURE.md` | §8 | +15 | Low |

---

## 5. Implementation Order

1. `invalidate_token_estimate` and the call-site sweep. Run `tests/test_token_memo.py tests/test_compact_cache_discipline.py`. The cache key is unchanged, so those tests stay green. Add the generation assertion from §2.7 item 6.
2. `_wire_messages_and_count` and make `_messages_for_model` a wrapper. Run `tests/test_spec24_compiled_turn.py`. The task-card estimator test must stay green without edits.
3. Change `_compact_before_llm` and move tool loading above it in `_run_loop`. Run `tests/test_panic_prune.py tests/test_context_strategy_tier0.py`. Add §2.7 items 1–3.
4. Overwrite the breakdown fields in `_run_loop`. Extend the handler log.
5. Meter filter and tab-switch restore in `ui/window.py`.
6. `begin_send` / `start_claimed_loop` / both drain points / `_finish_loop`. Point `send_to_special_agent` at them. Add `_flush_stranded_sends` at the end of `_do_response_complete` and `_do_error`. Add §2.7 items 4–5.
7. `cancel()` drops `_inbox` and `_stranded`.
8. Update `docs/ARCHITECTURE.md` per §8.
9. Run the acceptance command in §6.

Do not start step 6 before step 3. The queue is independent, but landing both in one `_run_loop` edit avoids a broken intermediate trigger.

---

## 6. Acceptance Criteria

- [ ] With a compile packet installed, a transcript estimate above `soft_ceiling` and a wire count below it does not call `panic_prune` and does not delete messages before `wire_from`.
- [ ] With a compile packet whose wire count is above `soft_ceiling`, panic still runs with `preserve_current_turn=True` and budget `_compute_landing_goal(hard_ceiling)`.
- [ ] No compile packet, estimate `1_000`: `compact` is called with `soft_ceiling` and no kwargs. Panic is not called.
- [ ] No compile packet, estimate `10_000_000`: panic path unchanged.
- [ ] `usage_percent` and `total_used_tokens` on the dispatched breakdown equal the wire count from the list passed to `_call_llm`. `transcript_tokens` equals the pre-overwrite `get_token_breakdown` total.
- [ ] The log line includes both `total` (wire) and `transcript`.
- [ ] A breakdown for session A does not call `set_context_meter` while `get_current_session_key()` is session B.
- [ ] Switching to session B paints `get_last_breakdown(B)`, or clears the bar when that cache is missing.
- [ ] Switching to `canvas:` or `welcome:` clears the bar.
- [ ] A `project:` tab does not show a coder's percent.
- [ ] `begin_send` during an active loop returns `False`, does not start a thread, and does not change the handler turn token or `_turn_reply_target`.
- [ ] Two `begin_send` calls on two different session keys, both idle, both return `True`.
- [ ] Queued text becomes user messages, in order, on the active loop, before the next model call.
- [ ] A queue that arrives before the COMPLETED terminal continues the same `_run_loop` (`continue`), one completion, same token.
- [ ] A queue that arrives after that drain is left in `_stranded` with the session no longer active. `_do_response_complete` then `send_to_special_agent`s it, which publishes a new token only after the old completion's token check has passed.
- [ ] `cancel()` drops `_inbox` and `_stranded`.
- [ ] `tests/test_spec24_compiled_turn.py::test_compiled_budget_counts_the_task_card_once_with_one_estimator` passes unchanged.
- [ ] After `invalidate_token_estimate`, the next `get_token_estimate` matches the stubbed bodies.

Acceptance command:

```bash
python -m pytest \
  tests/test_wire_context_pressure.py \
  tests/test_panic_prune.py \
  tests/test_context_strategy_tier0.py \
  tests/test_spec24_compiled_turn.py \
  tests/test_token_memo.py \
  tests/test_compact_cache_discipline.py \
  tests/test_context_ui.py \
  tests/test_agent_runtime.py \
  -q
```

`test_agent_runtime.py` calls `_run_loop` and `send_message` directly. Those calls must still finish with `is_loop_active` false and must still dispatch one completion.

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| Two idle coders, one project broadcast | Two runtimes, two `begin_send` True, two loops. Neither waits for the other. |
| Both coders message the Supervisor while the Supervisor's loop is active | Both texts append to the Supervisor's `_inbox`. One Supervisor loop drains them in order. No second thread. The Supervisor's handler token does not change for those queues. |
| Second message arrives, loop then hits COMPLETED with an empty drain | `_finish_loop` stores the text on `_stranded` and clears `_active_loops`. After the completion callback accepts the old token, `_flush_stranded_sends` starts one new turn through `send_to_special_agent`. |
| Second message arrives, then the user cancels | `cancel` pops `_inbox` and `_stranded`. The queued text is not sent. |
| User send and the completion flush both see the same stranded texts | `begin_send` and `take_stranded` pop `_stranded` under `self._lock`. One gets the list. The other gets `[]`. |
| `begin_send` returns True, `Thread.start` raises | Claim is discarded, inbox for that key is popped, the exception propagates. The session is not left active. |
| Compiled packet, wire under the ceiling, transcript over it | No panic, no deleted messages. `compact` is called with a budget equal to the transcript estimate so `last_result` is a fresh no-op. |
| Compiled packet, wire over the ceiling | Existing panic. Off-wire messages may be deleted because `compact` still measures `get_token_estimate`. Accepted. |
| Packet missing `wire_from` (older in-memory packet) | `_compiled_messages` already falls through to `turn = []` and sends `tail = conv.messages[after:]`. Pressure uses that same function. Do not invent a second cutoff. |
| `wire_from is True` | Treated as missing. `bool` is an `int`. The existing `isinstance(..., int) and not isinstance(..., bool)` check stays inside `_compiled_messages`. |
| Coder `agent_role == "coder"` | Repo-map block forced to `""` inside `_wire_messages_and_count`, same as `_messages_for_model` today. |
| Supervisor (not `coder`) | Repo map still prefixed on the plain path and still counted in pressure. |
| No tools (`tools is None`) | `_schema_tokens` returns 0. Direct `_compact_before_llm` test calls stay on the estimate. |
| Hot windows push the compiled count `>= effective` | Hot is dropped and the count is recomputed before it is compared to `soft_ceiling` and before the raise. Same order as today. |
| Task card on a compiled session | Still added by `_compile_token_count`. Meter can be ahead of OpenRouter by the card (≤ 500 chars) plus tokenizer skew. Do not "fix" this in this change. |
| Visible tab is `project:<name>` | Bar clears on switch. Agent breakdowns do not paint it. |
| Visible tab is `canvas:` or `welcome:` | Bar clears. Name update still returns without an agent label. |
| `get_current_session_key()` is `None` | Breakdown does not paint. |
| `clear_conversation` while the loop is active | Still refused (`is_loop_active`). Unchanged. |
| Chars//4 estimate (tiktoken missing or model rejected) | No cache write. Generation does not change that path. |
| `force_llm_compact` focus text | Prompt restored, then `invalidate_token_estimate()`. |

---

## 8. ARCHITECTURE.md Updates Required

In §3.21 the token-estimation paragraphs (the block that starts "**Token estimation (Phase CB-4).**" and "**Token estimate caching (Phase CB-5).**"):

- Say `get_token_estimate()` is the stored transcript.
- Say the compaction trigger and `usage_percent` use the wire count from `AgentRuntime._wire_messages_and_count`: plain path is the transcript estimate plus the repo-map addon plus tool-schema JSON; compiled path is `_compile_token_count` of the compiled list, after the hot drop.
- Say the breakdown keeps the transcript total on `transcript_tokens`.
- Say the estimate cache write is skipped when `_token_gen` changes during the count, and that invalidation goes through `Conversation.invalidate_token_estimate()`.

In the runtime section (§3.21m or the current `agent/runtime.py` section):

- Say `_active_loops` is claimed in `begin_send` before the thread starts.
- Say a send for a session already in that set appends to `_inbox` and does not start a second loop and does not rotate the handler turn token.
- Say coders do not share a runtime: `_runtimes` is keyed by display name.

In the UI section that mentions the context meter, say the bar shows `_last_breakdown` for `get_current_session_key()` and ignores breakdowns for other keys.

---

## 9. Implementer self-check

Before calling the work done:

1. Every file in §4 is either changed or listed under "Files NOT changed" and was left unchanged on purpose.
2. Paste the pytest output from the §6 command. Not a summary.
3. `grep -n "_token_estimate_cache = None" models agent ui` returns no remaining assignments on a `Conversation`. `invalidate_token_estimate` is the only clearer. A comment that mentions the attribute is fine.
4. `grep -n "def _messages_for_model" -A 20 agent/runtime.py` shows the wrapper raising on `>= model_max - self._RESPONSE_RESERVE_TOKENS` and returning `list[dict]`.
5. Do not declare the group broken. Two session keys, two display names, two `begin_send` results of `True`.
