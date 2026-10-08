# SPEC-16: Priority Fixes from the Maintainability Audit

**Date:** 2026-10-07
**Status:** READY — not started
**Implements:** The ordered fix list from the 2026-10-07 read-only audit
(status bar, activity pill, dead helpers, gateway ingress, `_build` split,
`_run_loop` tool-batch extract, then the three large classes).
**Depends on:** Nothing in flight. Do not edit `transport/telegram.py` or
SPEC-15 files.
**Target branch:** main

Line numbers below were true on 2026-10-07. Anchor on the symbol names.
If a line has moved, follow the symbol.

---

## 0. Rules for the implementing agent

Work the phases in order. Finish and test one phase before starting the next.
A later phase must not undo an earlier one.

- Handlers must not import other handlers. `window.py` is the only place that
  wires them. `tests/conftest.py::test_handlers_do_not_import_each_other` and
  `tests/test_architecture.py` enforce this. `AgentRuntimeHandler` must not
  import `ActivityHandler`.
- Do not change user-visible behavior except where a phase says to.
- Do not wire `/cost` to `get_session_usage`. After a turn is saved, `/cost`
  reads `total_tokens` and `total_cost` from the conversation JSON.
  `auto_save_conversations` defaults to true. The comment on
  `ProjectHandler.set_runtime_usage_fn` that says `window.py` wires it is
  stale. Correct that comment only if you are already editing that docstring.
  Do not add the wire.
- Do not touch `scratch/`, SPEC-15, or the Telegram bridge.
- Do not reorder object construction inside `MainWindow._build`. Several
  comments there record load-bearing order (FileTreeHandler before LeftPanel,
  AgentRuntimeHandler before the callbacks that close over it, FeedHandler
  before FeedTab).
- When a phase says "grep before deleting," search production code and
  `tests/`. Ignore `scratch/`.

Minimum check after every phase:

```bash
python -m pytest tests/test_architecture.py -q
```

plus the phase's own tests, listed in that phase. Run them from the repo root
with the project venv.

---

## 1. What is already true (do not rediscover this)

These facts were verified by reading callers. Trust them, then grep if a
symbol's callers might have changed since 2026-10-07.

**Status bar.** `MainWindow._build` creates `self._agent_id_label` with the
text `Agent: —` and appends it to the window status bar. `update_agent_id_display`
is the only method that sets that text. Nothing calls it. The agent name the
UI does update is the context meter: nested function `_update_agent_display`
inside `_build` calls `MainContent.update_agent_context_display`. That nested
function runs from the token-breakdown listener `_on_context_meter` and from
`set_on_session_changed`.

**Activity pill.** `window.py` wires only two callbacks:

```python
self._agent_runtime_handler.set_on_agent_start(
    lambda sk: self._activity_handler.on_agent_start(sk)
)
self._agent_runtime_handler.set_on_agent_end(
    lambda sk: self._activity_handler.on_agent_end(sk)
)
```

`on_agent_start` sets pill state `reasoning` (`◉ Reasoning…`).
`on_agent_end` sets `done` (`✓ Done`), then idle after 5 seconds.
`ActivityHandler.on_gateway_event` has no production caller. It is the only
caller of `on_chat_delta` and `on_tool_use`. Those two methods are what set
`streaming` (`⬇ Generating…`) and `tool_use` (`⚙ {tool name}`). A local turn
therefore stays on Reasoning until it ends. The chat bubble and the activity
drawer update on other paths and must keep doing so.

**Drawer.** `ActivityWiringHandler.wire()` connects five sources. Sources 3–5
listen to `AgentRuntimeHandler` (command output, tool bubbles, drawer
start/end) and are live. Sources 1–2 listen to `ActivityHandler` bubbles and
lifecycle, which only `on_gateway_event` emits.

**Text deltas are coalesced.** `AgentRuntimeHandler._on_text_delta` appends
the new slice onto `self._streaming_text[session_key]` and schedules a
render. `_do_text_delta_inner` renders the accumulated buffer and is also
invoked with an empty `text` argument for trailing dispatches. The pill
counter in `on_chat_delta` does `self._streaming_token_count += len(delta_text)`
on every call. Passing the accumulated buffer, or calling it from the render
method, double-counts.

**`on_chat_delta` only enters streaming on the first delta of the turn**
(`_first_delta_seen`). A later tool call sets `tool_use`. Further text would
stay on `tool_use` unless this is changed. Phase SP2 changes it.

---

## SP1 — Status bar shows the same agent as the context meter

**Goal.** The status label stops saying `Agent: —` for the whole session.
It shows the same name `_update_agent_display` already resolved, and it
returns to `Agent: —` when that resolution fails.

**File.** `ui/window.py` only.

**Edit.** Inside the nested `_update_agent_display(sk)` in `_build`:

- When `agent_name` is non-empty, call `self.update_agent_id_display(agent_name)`
  in addition to the existing `update_agent_context_display` call.
- When `agent_name` is empty, call `self.update_agent_id_display("—")`.
  `update_agent_id_display` already formats the string as `Agent: {agent_id}`.
  Do not format it a second time at the call site.

Do not add a second name-resolution path. Do not remove the context meter
update. Do not delete `update_agent_id_display`.

**Done when**

- A session whose name resolves updates both the context meter and the
  status label to that name.
- A session whose name does not resolve sets the status label back to
  `Agent: —`.
- Both the token-breakdown path and the tab-switch path hit this, because
  both already call `_update_agent_display`.

**Tests.** Add a focused test next to the existing window tests
(`tests/test_window_settings_bar.py` or a new `tests/test_window_agent_label.py`
if that file would be mixing concerns). Drive `_update_agent_display` or the
two callbacks that call it (`set_on_token_breakdown_extra` listener and
`set_on_session_changed`). Assert the label text. If constructing a full
`MainWindow` is impractical, extract nothing new: call `update_agent_id_display`
for the format assertion, and assert the source of `_update_agent_display`
contains the `update_agent_id_display` call. Prefer a behavioral test if the
existing window fixtures already build a window.

```bash
python -m pytest tests/test_window_settings_bar.py tests/test_window_agent_label.py tests/test_architecture.py -q
```

Skip a path that does not exist.

---

## SP2 — Local turns drive the streaming and tool pill states

**Goal.** While a local agent is streaming text, the pill says Generating.
While a tool is running, the pill shows that tool's name. When text resumes
after a tool, the pill returns to Generating. Turn end still flashes Done.
Do this with callbacks wired in `window.py`. Do not call `on_gateway_event`.

### SP2.1 Callbacks on `AgentRuntimeHandler`

**File.** `ui/handlers/agent_runtime_handler.py`

Add two optional callbacks, stored as `None` in `__init__` next to
`_on_agent_start_cb` / `_on_agent_end_cb`, with setters beside
`set_on_agent_start` / `set_on_agent_end`:

```python
def set_on_stream_delta(self, cb: Callable[[str, str], None]) -> None:
    """cb(session_key, delta_text). delta_text is the new slice only."""

def set_on_tool_start(self, cb: Callable[[str, str], None]) -> None:
    """cb(session_key, tool_name)."""
```

**Where to fire `set_on_stream_delta`.** In `_on_text_delta`, after the
empty-text early return and after the ended-session and stale-token guards,
and after the slice has been appended to `_streaming_text`. Pass the `text`
argument of `_on_text_delta` (the new slice), not
`_streaming_text[session_key]`. Skip when `text` is empty. Skip when the
callback is `None`.

Do not fire it from `_do_text_delta` or `_do_text_delta_inner`. Those run
for coalesced snapshots and for empty trailing dispatches.

Do not add another `GLib.idle_add`. `_on_text_delta` already runs in the
runtime's dispatch context, which production puts on the main thread.

**Where to fire `set_on_tool_start`.** In `_do_tool_call_start`, after the
`_ended_sessions` return, next to the existing tool_start drawer bubble.
Pass `session_key` and `name`. Skip when the callback is `None`. This method
is already marshalled to the main thread by `_on_tool_call_start`.

Do not fire the pill from the feed-card block. That block is skipped when
no project is open. The pill must still update offline, same as the drawer
bubble.

### SP2.2 Pill state machine

**File.** `ui/handlers/activity_handler.py`, method `on_chat_delta`.

Keep the length accumulation. Also enter `streaming` when the current state
is `tool_use`, not only on the first delta of the turn:

- If `_first_delta_seen` is false, or `self._state == "tool_use"`, set
  `_first_delta_seen = True` and call `_set_state("streaming", sk)`.
- Do not reset `_streaming_token_count` on the tool_use → streaming
  transition. `on_agent_start` already zeros it at turn start.

`on_tool_use` already sets `tool_use`. Leave it. `on_agent_start` /
`on_agent_end` stay the turn boundaries.

### SP2.3 Wire in `window.py`

Next to the existing `set_on_agent_start` / `set_on_agent_end` wires:

```python
self._agent_runtime_handler.set_on_stream_delta(
    lambda sk, delta: self._activity_handler.on_chat_delta(delta, sk)
)
self._agent_runtime_handler.set_on_tool_start(
    lambda sk, name: self._activity_handler.on_tool_use(name, sk)
)
```

Argument order matters. `on_chat_delta(delta_text, session_key)`.
`on_tool_use(tool_name, session_key, data=None)`.

**Do not**

- Import either handler from the other.
- Route through `on_gateway_event`.
- Change drawer bubbles, feed cards, or streaming bubble rendering.
- Call `on_chat_delta` with the accumulated buffer.

**Done when**

- First non-empty text slice of a turn moves the pill from reasoning to
  streaming.
- A tool start moves it to tool_use with that tool name.
- A later text slice moves it back to streaming.
- `on_agent_end` still ends on done.
- With both new callbacks left at `None`, existing runtime-handler tests
  behave as before.

**Tests.** Extend the activity-handler tests that already build a handler
with a fake GLib (see `tests/test_activity_bubbles.py` fixtures, or add
`tests/test_activity_pill_local.py`). Call the `ActivityHandler` methods
for the state transitions, and call `AgentRuntimeHandler._on_text_delta` /
`_do_tool_call_start` with the callbacks set and with them left `None`.

```bash
python -m pytest tests/test_activity_bubbles.py tests/test_activity_pill_local.py tests/test_activity_pill_adapter.py tests/test_activity_bubble_batching.py tests/test_agent_runtime.py tests/test_architecture.py -q
```

`test_agent_runtime.py` is large. If it is too slow for iteration, run the
new tests first, then the full file before marking SP2 done.

---

## SP3 — Delete confirmed-dead helpers

**Goal.** Remove symbols that have no caller in production or tests. Do not
widen this list.

Delete these, and only these, after a fresh grep confirms each still has no
caller outside its definition and comments:

| Symbol | File | Notes |
|---|---|---|
| `FileTree._update_drawer_prefix` | `ui/views/file_tree.py` | Returns `False`. Docstring says ColumnView replaced it. |
| `build_feed_reference_widget` | `ui/views/feed_card.py` | Also delete the matching line in the module header that lists it as public API. Leave `build_feed_card` and `build_context_panel`. |
| `get_current_phase` | `utils/workflow_state.py` | Update the module docstring example so it no longer tells readers to call it. Keep `init_workflow`, `advance_phase`, `get_workflow_content`, and `is_phase_done`. `is_phase_done` is used by tests. |
| `PromptsHandler.scan_prompts` | `ui/handlers/prompts_handler.py` | Alias of `load_prompts`. Keep `load_prompts` and `_scan_prompts`. |
| `AgentBuilderDialog._add_field` | `ui/views/agent_builder.py` | Form rows use `_add_labeled` and `_labeled_box`. |
| `ChatHandler._show_forward_menu` | `ui/handlers/chat_handler.py` | Forwarding passes `on_forward_click` into the renderer. |
| `ChatHandler.set_on_send_initiated` | `ui/handlers/chat_handler.py` | Also delete `self._on_send_initiated`, its `__init__` assignment, and every `if self._on_send_initiated:` block inside `on_send`. There are four. |

**Do not delete in this phase**

- `set_on_res_confirmed`, `on_chat_event`, `_buffer_assistant_text`,
  `_clear_render_guard`, `_handle_lifecycle_completed`. They are gateway
  leftovers, and `tests/test_chat_handler.py` plus
  `tests/test_missing_message_fix.py` still call them. SP4 does not delete
  them either. They are out of scope.
- `ActivityHandler.on_send_initiated`. Nothing calls it today. Leave it
  until SP4, which removes the gateway preflight as a set.
- `FeedHandler.snooze_card` / `unsnooze_card`, `add_audit_report_card`,
  `update_agent_id_display`, `on_chat_delta`, `on_tool_use`.
- CSS in `ui/styles.py`. Unused selectors are a separate cleanup.

**Done when** the symbols are gone, `is_phase_done` still imports, prompts
still load, and the forward button still receives `on_forward_click`.

**Tests.**

```bash
python -m pytest tests/test_workflow_state.py tests/test_chat_handler.py tests/test_prompt_loader.py tests/test_file_tree_handler.py tests/test_architecture.py -q
```

Add any test file that fails because it mentioned a deleted symbol. Fix the
test to match the deletion. Do not reintroduce the symbol to satisfy a test
that only existed to call it. `tests/test_workflow_state.py` must keep
passing without `get_current_phase`.

---

## SP4 — Remove the gateway event ingress

**Goal.** `ActivityHandler.on_gateway_event` is 226 lines and every caller
is a test. Delete the ingress and the tests that only exist to feed it.
The local pill path from SP2 and the local drawer path stay.

### Delete from `ActivityHandler`

After a fresh grep, delete `on_gateway_event` and every method whose only
production or test callers were that method or the tests you are deleting
in this phase. Expected set, re-verify before deleting each one:

- `on_gateway_event`
- `on_agent_message_received`
- `on_chat_final`
- `on_send_initiated` and `_on_preflight_timeout` / `_stop_send_initiated_timer`
  if nothing else calls them
- `on_res_confirmed` on `ActivityHandler` (not `ChatHandler.on_res_confirmed`)
- `on_agent_error` if its only caller was the lifecycle branch inside
  `on_gateway_event`. The local error path uses `on_agent_end`.
- `_extract_chat_text`, `_resolve_agent_name`, `_agent_name_for_event`
- `set_on_assistant_buffer`, `set_on_lifecycle_completed`,
  `ActivityHandler.set_on_agent_start` (this is the render-guard callback,
  not `AgentRuntimeHandler.set_on_agent_start`), `set_agent_manager` on
  `ActivityHandler` if it only served gateway name lookup

**Keep**

- `on_agent_start`, `on_agent_end`, `on_chat_delta`, `on_tool_use`
- `_set_state`, `_update_status`, `_streaming_label`, the status ticker,
  `_is_ui_active`, `set_agent_routing`
- The SP2 callbacks and the window wires from SP1 and SP2

If a helper is still used by a kept method, keep the helper.

### `ActivityWiringHandler`

In `wire()`, remove the two gateway registrations:

- `set_on_activity_bubble(self._on_activity_bubble)`
- `set_on_agent_lifecycle(self._on_agent_lifecycle)`

Remove those two adapter methods if nothing else calls them. Keep the three
local registrations: `set_on_command_output`, `set_on_activity_bubble` on
the **runtime** handler, and `set_on_drawer_lifecycle`. Update the module
docstring so it no longer says gateway events arrive through
`on_gateway_event`.

### Tests

- Delete test functions that call `ActivityHandler.on_gateway_event`.
  The bulk of `tests/test_activity_bubbles.py` is that. Keep tests that
  exercise the pill state machine, the local bubble batching
  (`tests/test_activity_bubble_batching.py`), and local drawer wiring
  (`tests/test_activity_wiring_handler.py`). Update wiring tests that
  assert the two gateway `set_on_*` calls were made.
- `tests/test_missing_message_fix.py` drives the ingress to test
  `ChatHandler` fallback rendering. Do not delete `ChatHandler` methods.
  Delete only the cases that exist to push events through
  `on_gateway_event`. Keep cases that call `ChatHandler` methods directly.
- `tests/test_activity_drawer.py` has a few `on_gateway_event` calls.
  Retarget those cases at the local drawer API (`ActivityDrawer.on_agent_start`
  / `on_agent_end` / `append_event`), or delete them if a local-path test
  already covers the same row.

**Do not**

- Delete `ChatHandler.on_chat_event` or `tests/test_chat_handler.py`.
- Stop emitting local tool bubbles from `AgentRuntimeHandler._emit_activity_bubble`.
- Remove SP2 pill behavior.

**Done when** a repo grep for `on_gateway_event` finds nothing under `ui/`
or `tests/`, the pill still reaches streaming and tool_use through the SP2
callbacks, and the drawer still receives local tool rows.

**Tests.**

```bash
python -m pytest tests/test_activity_bubbles.py tests/test_activity_bubble_batching.py tests/test_activity_wiring_handler.py tests/test_activity_drawer.py tests/test_activity_pill_adapter.py tests/test_missing_message_fix.py tests/test_architecture.py -q
```

---

## SP5 — Split `MainWindow._build` inside `window.py`

**Goal.** `_build` (about lines 112–827) becomes four private methods on
`MainWindow`, called in the current order from `_build`. Behavior and
construction order stay the same. No new modules.

**Shape.**

```python
def _build(self):
    self._wire_chat()
    self._wire_projects()
    self._wire_agents()
    self._wire_feed()
```

Move existing statements. Do not reorder them. Suggested contents, following
the comments already in `_build`:

| Method | What moves into it |
|---|---|
| `_wire_chat` | Chat render handler, `MainContent`, `ChatHandler`, send-button connect, input toolbar, spellcheck menu. The nested `_on_input_buffer_changed` and `_on_input_right_click` move with this method. |
| `_wire_projects` | `FileTreeHandler`, `LeftPanel`, project handler, project-list handler, feed bar hooks that are created before the feed handler. |
| `_wire_agents` | `AgentListHandler`, `AgentRuntimeHandler`, special-agent registration, agent builder, settings, prompts, activity handler, the SP1/SP2 pill wires, media handler. Nested `_update_agent_display` and `_on_context_meter` move with this method. |
| `_wire_feed` | Feed handler, feed tab, review handler, command handler, activity drawer reparent, status bar. Nested `_on_send_to_agent` moves with this method. |

If a statement does not fit a row, put it in the method that matches the
comment above it. The status bar (`_agent_id_label`) is created late in
`_build`, after a lot of wiring. Leave it where it is in the sequence even
if that puts it in `_wire_feed`. SP1 depends on `_update_agent_display`
calling `update_agent_id_display`. That call moves with the nested function.
It is safe: the label exists before any session-changed or token callback
can run, because those fire after the window is up. Do not call
`_update_agent_display` during `_build`.

**Do not**

- Move logic into handlers. This phase is a cut inside `window.py` only.
- Change callback signatures.
- Fix unrelated TODOs you pass.

**Done when** `_build` is the four calls plus any unavoidable local that
must be shared (pass it as an argument rather than reordering), and the
window tests pass without modification. If a test pinned source inside
`_build` via `inspect.getsource(MainWindow._build)`, retarget it at the
method that now holds the pinned text.

**Tests.**

```bash
python -m pytest tests/test_window_settings_bar.py tests/test_window_agent_label.py tests/test_window_stop_all_dialog.py tests/test_window_agent_selected.py tests/test_window_auto_accept_warning.py tests/test_window_project_created.py tests/test_window_settings_wiring.py tests/test_architecture.py -q
```

---

## SP6 — Extract the tool batch from `AgentRuntime._run_loop`

**Goal.** One new method on `AgentRuntime`. The LLM iteration stays in
`_run_loop`. No new module. `tests/test_agent_runtime.py` calls `_run_loop`
directly (about 82 times). Those calls must keep working.

**Cut.** The block that starts at the comment `Tool calls — execute each`
(the `tool_calls_raw` loop, assistant message with tool calls, approval
gating, `execute_tool`, audit record) and ends at the end of that `for`
loop, before the comment `Check cost/step limits after tool execution`.

The post-tool limit check, the max-iteration failure, and the
empty-assistant rollback in the `except` tail stay in `_run_loop`.

**Control flow.** A `return` inside that block today returns from
`_run_loop` after `_terminate_turn`. A `return` inside the new method
returns only from the method, and the loop would continue. That is a bug.

```python
def _execute_tool_batch(...) -> bool:
    """Run one iteration's tool calls.

    Returns True if the turn was terminated and the caller must return
    immediately. Returns False if the caller should continue with the
    post-tool limit check.
    """
```

Every path that currently `return`s from `_run_loop` inside the extracted
block must `return True`. The normal end of the for-loop must `return False`.
The caller:

```python
if self._execute_tool_batch(...):
    return
# existing post-tool limit check stays here
```

Name the parameters after the locals the block already uses (`session_key`,
`turn_token`, `conv`, `text_content`, `tool_calls_raw`, `iteration`). Do not
reach into unrelated state to avoid a parameter.

**Source-pinned tests.** These read `inspect.getsource(AgentRuntime._run_loop)`
and assert a substring that lives in the LLM iteration, which this phase
does not move:

- `tests/test_context_strategy_audit_fixes3.py` asserts
  `breakdown["trimmed_this_turn"] = _compaction_happened`
- `tests/test_context_strategy_audit_fixes2.py` asserts
  `_compute_compaction_threshold` appears in `_run_loop`

Leave those substrings in `_run_loop`. If a pin fails because the substring
moved, point `getsource` at the method that now contains it. Do not delete
the assertion.

**Do not** extract `_begin_turn` in this phase. The preamble's early
`return`s have the same control-flow trap, and the tool batch is the cut
that pays for itself. Do not move `_call_llm` or `_call_llm_streaming`.

**Done when** `_run_loop` still reaches a text-only completion, a tool
round-trip, cancel, and the max-iteration failure, and the two source pins
still pass.

**Tests.**

```bash
python -m pytest tests/test_agent_runtime.py tests/test_context_strategy_audit_fixes2.py tests/test_context_strategy_audit_fixes3.py tests/test_error_surfacing.py tests/test_turn_prep_off_thread.py tests/test_stop_all.py tests/test_tool_middleware.py -q
```

---

## SP7 — Split the three large classes

**Goal.** Smaller modules, same behavior. Do SP7 only after SP1–SP6 are
green. One class per sub-phase. After each sub-phase, run that class's
tests before starting the next.

Facade classes stay where callers already import them. New modules are
implementation details the facade calls. Do not make `window.py` import
three new types where it imported one, unless the facade would otherwise
become a pass-through with no methods.

Handlers still must not import other handlers. A handler may import a new
sibling module that is not itself a handler class in `ui/handlers/` (put
extracted feed pieces in `ui/handlers/feed/` or as functions in a module
that does not import sibling handlers). If the import guard flags a new
file because it lives in `ui/handlers/` and imports another file there,
put the extracted code in a package that the guard does not treat as a
handler, or keep the helper free of handler imports. Read
`test_handlers_do_not_import_each_other` before choosing the path. The
simplest compliant cut is: new modules under `ui/feed/` and `ui/file_tree/`
(not `ui/handlers/`), imported by the existing handler or view.

### SP7a — `FeedHandler` (`ui/handlers/feed_handler.py`, ~2,850 lines)

Split along the method groups already in the class. `FeedHandler` remains
the object `window.py` constructs.

| New home | Methods that move (names, not a full list) |
|---|---|
| Auto-accept prefs | `set_show_auto_accept_warning` through `_agent_scope_matches`, including snooze |
| Persist writer | `_ensure_persist_writer`, `_enqueue_*`, `_persist_loop`, `_drain_persist_queue`, `shutdown_persist_writer` |
| Load and eviction | `on_project_opened`, `on_project_closed`, `_load_more`, `_evict_surplus_card_widgets`, live-window helpers |
| Review actions | `handle_review`, `handle_accept`, `handle_reject`, batch accept |
| Snapshots | `_finalize_snapshot` through `_maybe_create_snapshot` |

`add_card` and `on_project_opened` stay as methods on `FeedHandler` even if
their bodies call a helper. `tests/test_feed_handler.py` is ~6,600 lines and
patches `ui.handlers.feed_handler.feed_store`. If you move a function, update
the patch target in the same commit. Prefer leaving the `feed_store` import
on the facade module if the tests patch that path.

### SP7b — `FileTree` (`ui/views/file_tree.py`, ~2,392 lines)

| New home | What moves |
|---|---|
| Row model | `FileTreeRow`, `FileTreeRowWidget`, and the column factories (the classes above `class FileTree`) |
| Drawer | `toggle_drawer_for_file` through the drawer diff, history, revert, and clipboard methods |

`FileTree` stays the widget `LeftPanel` embeds. Public methods
`load_project`, `navigate_back`, `toggle_drawer_for_file` stay on `FileTree`.

### SP7c — `AgentRuntimeHandler` (`ui/handlers/agent_runtime_handler.py`, ~3,066 lines)

| New home | What moves |
|---|---|
| Session lifecycle | `clear_conversation`, `compact_conversation`, `send_to_special_agent`, stop |
| Turn UI adapters | `_do_text_delta` / `_do_text_delta_inner`, `_do_tool_call_start`, `_do_tool_call_result`, `_do_response_complete`, `_do_error` |
| Provider config | `_resolve_agent_model`, `_parse_providers_file_strict`, `refresh_provider_config` |

The SP2 setters and the fires inside `_on_text_delta` and
`_do_tool_call_start` move with those methods. `window.py` still calls
`set_on_stream_delta` and `set_on_tool_start` on `AgentRuntimeHandler`.

Do not split `AgentRuntime` (`agent/runtime.py`) in this phase. SP6 was
that cut.

**Done when** each facade's public methods used by `window.py` still exist
under the same names, behavior is unchanged, and the paired test file is
green. Splitting a test file is allowed when it matches the new modules.
Do it in the same sub-phase as the code split. Do not split tests ahead of
the code.

**Tests.**

```bash
python -m pytest tests/test_feed_handler.py tests/test_feed_store.py tests/test_file_tree_handler.py tests/test_file_tree_columnview.py tests/test_agent_runtime.py tests/test_architecture.py -q
```

Run the matching file after each sub-phase, not only at the end.

---

## 8. Out of scope

- Wiring `ProjectHandler.set_runtime_usage_fn` to
  `AgentRuntimeHandler.get_session_usage`.
- Deleting `ChatHandler.on_chat_event` and the tests in
  `tests/test_chat_handler.py`.
- CSS selectors in `ui/styles.py` that nothing applies (welcome bubble,
  feed-ref, and the rest of that list).
- Splitting `agent/enforcement.py`, `agent/tools.py`, or
  `utils/project_awareness.py`.
- Splitting `agent/runtime.py` beyond the single method in SP6.
- `scratch/` probe scripts.
- SPEC-15 Telegram bridge.

---

## 9. Definition of done for the whole spec

1. SP1 through SP7 are each green on their test commands.
2. The status label tracks the context-meter agent name.
3. A local turn shows Reasoning, then Generating while text arrives, the
   tool name while a tool runs, Generating again if text resumes, then Done.
4. `on_gateway_event` is gone from `ui/` and `tests/`.
5. The dead-helper table in SP3 is gone.
6. `_build` delegates to the four `_wire_*` methods.
7. `_execute_tool_batch` exists on `AgentRuntime`, and `_run_loop` returns
   immediately when it returns true.
8. `FeedHandler`, `FileTree`, and `AgentRuntimeHandler` are facades over
   the extracted modules, and `window.py` still constructs the same classes.
