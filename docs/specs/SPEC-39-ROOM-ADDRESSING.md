# SPEC-39: Room Addressing — everyone hears, JEV decides who answers

**Date:** 2026-10-18
**Author:** Supervisor (PM direction, 2026-10-18)
**Status:** Draft — for implementation
**Implements:** PM direction confirmed 2026-10-18. Related, not the source of the rules: `docs/proposals/DEVELCAKES-V2-CHANGE-LIST.md` §1.3 (JEV as a Choice over the roster). SPEC-12 left this as post-MVP.
**Depends on:** SPEC-12 (one project surface; solo send and group send both reply into `project:<name>`), SPEC-22 (`post_decision` fail-open contract)
**Target branch:** main

> Architecture compliance: `agent/` stays free of GTK. The decision call lives in
> `agent/llm/jev_client.py`. Conversation mutation lives in `agent/runtime.py`.
> `ui/handlers/chat_handler.py` decides when to call them. The project chat
> surface (`ChatRenderHandler`, display key `project:<name>`) is unchanged:
> the human line is still one bubble, and a reply is still one named box.
> This spec does not add a second transcript store. There is no `transcript.db`
> (`docs/ARCHITECTURE.md` §3.21m). Each agent keeps their own
> `Conversation.messages`, and a room line is a `user` message inside that list.

---

## DISCOVERY

- Read `ui/handlers/chat_handler.py`: `on_send` (`:181`) returns immediately on an empty buffer (`:201`). Slash commands run first via `CommandHandler.process_input` (`:206-276`) and return when `result.handled`. Inline `@` mentions run next, only for `project:` tabs, via `resolve_inline_mention` (`:317-383`) and return on error, one target, or broadcast. The plain-text project send is `:402-433`. `get_solo_target(project_name)` (`project_handler.py:386`) returns `str | None`. A non-empty solo target calls `_send_local(solo_target, text, reply_target=f"project:{project_name}")` (`:417-418`). `None` fans out: one `_send_local(member, text, reply_target=f"project:{project_name}")` per member (`:427-433`). `_send_local` (`:153`) calls `AgentRuntimeHandler.send_to_special_agent(session_key, text, reply_target=reply_target)` and returns `None`. `_dispatch` (`:669`) is `GLib.idle_add` when `self._GLib` is set, otherwise a direct call. The You bubble is rendered inside `_show_and_send` before the send branch (`:388-401`). The input buffer is cleared after `_dispatch` (`:439`).
- Read `ui/window.py`: `_on_agent_cycle_clicked` (`:1772`) cycles `None` (the bar label `ALL`) through `get_project_members` and back to `None`, and stores it with `set_solo_target`. `ChatHandler` is constructed at `:170` and receives the project handler at `:449`.
- Read `ui/views/main_content.py`: `update_project_settings` (`:281`) sets the bar text to the member display name when `solo_target` is set, and to the literal `ALL` when it is not (`:324`).
- Read `ui/handlers/project_handler.py`: `get_project_members(project_name) -> list[str]` (`:305`) returns member session keys. `get_solo_target` (`:386`) returns `self._solo_targets.get(project_name)`, and a missing key is `None`.
- Read `ui/agent_runtime/session.py`: `send_to_special_agent(session_key, text, reply_target=None) -> None` (`:226`). Unregistered key: log and return (`:244-250`). No active project: pop `_turn_reply_target[session_key]` and `_do_error` (`:254-266`). Otherwise it sets `_turn_reply_target[session_key]` (`:275-278`), discards ended flags, assigns `self._turn_tokens[session_key] = new_token` and `rt._turn_token` (`:316-324`), then `rt.send_message(session_key, text, prepare=_prepare_turn)` (`:342`). It does not return the token. The token is readable on the handler as `_turn_tokens[session_key]` after the call returns, because the assignment happens before `send_message`.
- Read `ui/handlers/agent_runtime_handler.py`: `_get_runtime(name, agent_def=None)` (`:849`) returns the per-display-name `AgentRuntime`, creating it. It raises `RuntimeError` when no provider is configured (`:881`) or the provider has no API key (`:884`). `_prep_lock(session_key)` (`:911`) returns a per-session `threading.Lock`. `_prepare_turn_conversation` (`:978`) holds that lock, loads via `rt.load_conversation` (`:1039`), and on a second miss calls `rt.create_conversation(...)` with `defer_prompt_build=True` (`:1052-1068`). It then sets `conv.step_count = 0` (`:1125`). A witness must not call this method: the step reset is for a real user turn. `get_agent_name_for_session(session_key) -> str` (`:749`) returns `display_name` or `""`. `_active_project` is `tuple[str, str] | None` (`:107`).
- Read `ui/agent_runtime/provider.py`: `_resolve_agent_model(agent_def) -> str | None` (`:14`). It catches `Exception` internally and returns `None` or a model string. It does not raise.
- Read `agent/special_agents.py`: `SpecialAgentDef.display_name` (`:37`), `role` (`:38`), `get_self_improvement_config() -> dict` (`:53`). Session keys are `special:<role>` unless the YAML sets `session_key` (`:90`).
- Read `agent/runtime.py`: `send_message` (`:1271`) starts a daemon thread running `_run_loop` and returns `None`. `_run_loop` (`:1616`) adds `session_key` to `_active_loops` under `_lock` before `prepare` (`:1628-1629`). After prepare it calls `conv.add_user_message(text)` once (`:1741`). Each later LLM call re-reads `conv.messages` through `to_api_messages()` (`:3594`). `is_loop_active(session_key) -> bool` (`:2396`) takes `_lock`. The `finally` (`:2203-2208`) discards `_active_loops` under `_lock` and does nothing else. `get_conversation` (`:1267`) does not take the lock. `load_conversation(session_key) -> bool` (`:3999`) returns `False` when `load_conversation_from_disk` returns `None`; on success it stores the conv under `_lock`. `create_conversation(...) -> str` (`:1029`) replaces any existing conv for that key (it disconnects MCP first, `:1060-1063`) and stores under `_lock` (`:1112`). `save_conversation(session_key) -> str` (`:3990`) raises `ValueError` when the conv is missing, and holds `_lock` across `save_conversation_to_disk`. `_auto_save` (`:3980`) catches `Exception` from the save and does not raise. `_active_loops` is initialized at `:589`.
- Read `models/conversation.py`: `MessageRole.USER = "user"`, `ASSISTANT = "assistant"` (`:77-82`). `add_user_message(content) -> Message` (`:302`) appends a user message and does not touch `step_count`. `add_assistant_message` (`:310`) increments `step_count`. A room line must use `add_user_message`. An assistant-role line would be spoken in that agent's own voice and would burn a step.
- Read `agent/persistence.py`: `save_conversation_to_disk(conv, session_key) -> str` (`:42`) writes `<conversations_dir>/<session_key>.json` via temp file plus `os.replace`. It can raise `OSError`. It does not take a lock. `load_conversation_from_disk` (`:222`) returns `None` on a missing file, `JSONDecodeError`, or `OSError`. Colons in `special:coder` are already legal in that filename.
- Read `agent/llm/jev_client.py`: `post_decision(state: str, questions: dict) -> tuple[dict | None, str]` (`:111`). Never raises. `answers` is `None` whenever `error` is non-empty. Timeout is 2.0 s (`_DECISION_TIMEOUT_S`, `:51`). Confidence floor used by the classifiers is `_DECISION_MIN_CONFIDENCE = 0.6` (`:54`). `classify_mask_candidates` (`:224`) is the validation shape to copy: reject a non-dict answer, a non-`choice` type, a choice outside the allowed set, a `bool` confidence, a non-numeric confidence, and a confidence below 0.6. `classify_unknown` (`:165`) does not reject `bool` (`bool` is an `int` subclass). This spec rejects `bool`, matching the mask function. The wire question is `{"type": "choice", "instructions": str, "criteria": {option: description}}`. The answer object carries `choice`, `probabilities`, and `confidence`.
- Read `ui/agent_runtime/turn_ui.py`: `_do_response_complete(session_key, text, complete_token=None)` (`:448`) runs on the main thread. A non-`None` `complete_token` that differs from `_turn_tokens[session_key]` returns immediately (`:469-473`). Duplicate completion returns at `:488-490`. Cancelled and failed turns go to `_do_error`, not here. The display name is `self._agents[session_key].display_name` (`:519-520`).
- Read `ui/handlers/command_handler.py`: `resolve_inline_mention(text, session_key="") -> MentionResolution` (`:234`). No `@` token returns a resolution with empty target fields (`:256-258`). This path stays as it is. A plain `hi debugger` has no `@`, so it falls through to the project send.
- Architecture owner: `ChatHandler` owns who a project send is delivered to. `AgentRuntime` owns `Conversation.messages` for one agent. `jev_client.post_decision` owns the decision HTTP call. `ProjectHandler._solo_targets` owns the ALL-versus-one-member switch.
- Existing pattern to copy: the group branch already renders one You bubble and passes `reply_target=f"project:{project_name}"` so the reply paints on the project surface. The witness path copies `add_user_message` plus `save_conversation_to_disk`, not `send_message`.

---

## 1. Overview

**Problem.** With Chat set to ALL, a project send wakes every member (`chat_handler.py:420-433`). Naming someone in the sentence does not matter. A solo selection already talks to one agent and must stay that way.

**Solution.** ALL keeps one shared room.

- The human line is shown once, as it is today.
- Every project member can later recall that line. A member who was not asked to answer still has it in their conversation.
- JEV chooses who answers. `hi debugger` resolves to the Debugger member, and only Debugger runs a turn. `hi`, which names nobody, resolves to `all`, and every member runs a turn, which is today's fan-out.
- The answering agent's reply is heard by the other members the same way: it is written into their conversations, and it is not a second chat bubble. The project surface already shows the reply.

JEV does not join the chat and does not rewrite the text. The addressee receives the same string the human typed.

**Scope**

| In | Out |
|---|---|
| Plain-text send while `get_solo_target` is `None` | Solo send (`solo_target` set). Same `_send_local` as today |
| One `post_decision` choice: a member session key, or `all` | Changing `post_decision`, the DecisionModel card, or the 0.6 floor |
| Witness lines in non-speakers' conversations | A new transcript database, or rendering witness lines in the chat surface |
| Reply witness after a room turn completes | `/ask`, `/delegate`, `/tell`, `/stop`, and inline `@` mentions |
| Fail-open to `all` (everyone answers) | A Supervisor fallback. Unresolved means the current fan-out |

**Principles**

1. Hearing and answering are different. Hearing is a stored `user` line. Answering is `send_to_special_agent`.
2. Fail-open matches SPEC-22: JEV absent is today's ALL fan-out, not a dropped message.
3. A room line is never `role=assistant` on someone else's conversation.
4. A room line never calls `send_message` and never resets `step_count`.
5. The decision call does not run on the GTK main thread. `post_decision` blocks for up to 2.0 s.

---

## 2. Changes by File

### 2.1 `agent/llm/jev_client.py` — `resolve_room_addressee`

Add one function. Do not change `post_decision`.

```python
_ROOM_INSTRUCTIONS = (
    "The human just spoke in a group chat. Decide who should answer. "
    "Choose all when the message names nobody, greets the room, or addresses "
    "more than one participant. Choose one participant when the message "
    "addresses that participant by name or role. For example, \"hi debugger\" "
    "addresses the participant named Debugger. Choose exactly one option."
)


def resolve_room_addressee(text: str, roster: list[tuple[str, str]]) -> str:
    """Return ``\"all\"`` or one ``session_key`` from ``roster``.

    ``roster`` entries are ``(session_key, display_name)``. An empty roster,
    a non-string ``text``, any ``post_decision`` failure, a choice outside
    the roster, a bool confidence, or confidence below
    ``_DECISION_MIN_CONFIDENCE`` returns ``\"all\"``. Never raises.
    The return is the session key to wake, or ``\"all\"`` to wake every
    member. The human text is not modified.
    """
```

Behavior, traced against `post_decision` (`:111-162`) and the mask validator (`:250-266`):

1. If `roster` is not a non-empty `list`, or `text` is not a `str`, return `"all"` without calling `post_decision`.
2. Build `criteria: dict[str, str]`. Key `"all"` maps to `"The message addresses the whole room, or names nobody, or names more than one participant."` For each `(session_key, display_name)` with both values non-empty strings, key `session_key` maps to `f"{display_name} ({session_key}) — choose this when the message addresses {display_name}."` Skip a blank entry. If no member key was added, return `"all"` without a call.
3. `state` is the literal prefix `message:\n`, then `text[:4000]`, then `\n\nparticipants:\n`, then one line `- {display_name} ({session_key})` per kept entry. The 4000-character cut is only this state string. Callers still deliver the full `text`.
4. Call `post_decision(state, {"addressee": {"type": "choice", "instructions": _ROOM_INSTRUCTIONS, "criteria": criteria}})`. Ignore the error string; `answers is None` already covers it.
5. Read `answers["addressee"]`. Accept it only when it is a `dict`, `type == "choice"`, `choice` is a `str` and is a key of `criteria`, `confidence` is an `int` or `float` and is not a `bool`, and `confidence >= _DECISION_MIN_CONFIDENCE`. Otherwise return `"all"`.
6. Return `choice`.

`post_decision` never raises. The function still has no `raise`. A `KeyError`, `TypeError`, or `AttributeError` while reading the answer returns `"all"`.

Imports: none beyond what the module already imports. `_DECISION_MIN_CONFIDENCE` is module-local.

Line count: about 70.

### 2.2 `agent/runtime.py` — queue or append a room line

Next to `_active_loops` (`:589`):

```python
self._pending_room_lines: dict[str, list[str]] = {}
```

Add:

```python
def note_room_line(self, session_key: str, line: str) -> str:
    """Store one overheard room line on this session.

    Returns ``\"queued\"``, ``\"stored\"``, or ``\"skipped\"``.
    ``\"queued\"``: a ``_run_loop`` for this session holds ``_active_loops``.
    The line is appended to ``_pending_room_lines[session_key]`` and is not
    visible to the in-flight loop's ``to_api_messages()`` calls.
    ``\"stored\"``: no loop is active and a conversation exists. The line is
    ``add_user_message``'d and ``save_conversation_to_disk`` runs, both under
    ``_lock``.
    ``\"skipped\"``: blank line, blank session key, or no conversation.
    Never raises. Does not call ``send_message``. Does not change
    ``step_count`` (``add_user_message`` does not).
    """
```

Inside the lock, the active-loop test and the append are one critical section. Do not call `is_loop_active` and then append: the `finally` can discard the marker between the two.

`save_conversation_to_disk` raises `OSError`. Catch `OSError` inside the lock after the append, log it, and still return `"stored"` (the in-memory line is kept). Do not call `save_conversation`: it takes `_lock` again (`:3992`) and would deadlock, and it raises `ValueError` when the conv is missing.

Flush helper, called only from `_run_loop`:

```python
def _flush_pending_room_lines(self, session_key: str) -> None:
    """Move queued room lines onto the conversation, under ``_lock``.

    No conversation: leave the queue. Otherwise append each queued line with
    ``add_user_message``, clear the queue entry, and
    ``save_conversation_to_disk``. ``OSError`` is logged and swallowed.
    """
```

Call sites in `_run_loop`:

1. After `prepare` returns and `conv` is known (`:1688` has assigned `conv`), and before `conv.add_user_message(text)` (`:1741`). This covers a queue that survived a turn whose `finally` ran before the conversation existed.
2. In the `finally` (`:2207`), under the same `_lock` that discards `_active_loops`, after the discard. A `note_room_line` that arrives while the marker is still set queues. A `note_room_line` that arrives after the discard stores directly. Holding `_lock` across discard plus flush makes those two outcomes exclusive.

The `finally` must look the conv up with `self._conversations.get(session_key)`. The local `conv` may not exist when prepare failed. Do not reference that local in `finally`.

Line count: about 55.

### 2.3 `ui/handlers/agent_runtime_handler.py` — ensure a conversation, then note

`note_room_line` returns `"skipped"` when the runtime has no conv. The handler creates one the same way a first send does, without the step reset.

```python
def record_room_line(self, session_key: str, line: str) -> None:
    """Persist one overheard line for a special agent. Never starts a turn.

    Unregistered session key: return. ``_get_runtime`` raises ``RuntimeError``
    when no provider or API key is configured: catch it, log, return.
    Under ``_prep_lock(session_key)``: if ``get_conversation`` is ``None``,
    ``load_conversation``; if it is still ``None``, ``create_conversation``
    with the same arguments as ``_prepare_turn_conversation``'s create call
    (``defer_prompt_build=True``, model from ``_resolve_agent_model``,
    ``si_enforcement`` from ``get_self_improvement_config().get("enforcement")``).
    Do not set ``step_count``. Do not rotate ``_turn_tokens``.
    Then call ``rt.note_room_line``. Catch ``Exception`` around the whole
    body, log, and return. A witness failure must not block the addressee's send.
    """
```

`create_conversation` replaces an existing conv (`runtime.py:1060`). The `get_conversation is None` guard is what stops that. Do not call `create_conversation` on any other condition.

Project path for the create call: `self._active_project[1]` when `self._active_project` is not `None`, else `None`. Do not call `_worktree_for_turn` here. A witness is not a writer turn. The next real `_prepare_turn_conversation` repoints `project_path` (`:1082-1099`).

Also:

```python
def mark_room_turn(self, session_key: str) -> None:
    """Remember that the turn ``send_to_special_agent`` just started is a room turn.

    Copies ``self._turn_tokens.get(session_key)`` into
    ``self._room_turn_tokens[session_key]``. No token: do not write.
    """

def take_room_turn(self, session_key: str, token: object) -> bool:
    """Return True when ``token`` is the marked room turn, and pop it.

    A mismatch leaves the mark in place (a newer room send owns it).
    """
```

Initialize `self._room_turn_tokens: dict[str, object] = {}` beside `_turn_tokens` (`agent_runtime_handler.py:188`). `_turn_reply_target` at `:197` is a different dict. Do not store the room-turn mark there.

`mark_room_turn` must run on the thread that just returned from `send_to_special_agent`, before any other send to that session key. `send_to_special_agent` assigns the token before it returns (`session.py:322-342`).

Line count: about 90.

### 2.4 `ui/handlers/chat_handler.py` — ALL branch only

Replace the `else` fan-out at `:419-433`. Leave the `if solo_target:` branch (`:410-418`) unchanged, including `reply_target=f"project:{project_name}"`.

The You bubble above the branch (`:388-401`) stays. The human still sees exactly the text they typed, once.

New delivery, after that bubble:

1. Build `members` the same way the current `else` does (`:421-426`): project handler, else `self._projects.load_members`, else `[]`.
2. Build `roster: list[tuple[str, str]]` from those members. Display name comes from `self._agent_runtime_handler.get_agent_name_for_session(member)` when the handler is not `None`. Drop a member whose name is `""` from the roster only. They remain in `members` for the `all` fan-out.
3. Resolve off the main thread when `self._GLib` is not `None`. `threading.Thread(target=..., daemon=True).start()`. The thread calls `resolve_room_addressee(text, roster)` and then `self._GLib.idle_add` of a one-shot that performs delivery and returns `False`. When `self._GLib` is `None` (tests), resolve and deliver on the calling thread. `resolve_room_addressee` does not raise; still catch `Exception` in the thread, log, and deliver with choice `"all"`.
4. Delivery:
   - `choice == "all"` or `choice not in members`: for each `member` in `members`, `_send_local(member, text, reply_target=f"project:{project_name}")`, then `mark_room_turn(member)` if the runtime handler is not `None`. Do not `record_room_line` the human text. Each addressee stores it via `add_user_message` inside `_run_loop`.
   - `choice` is one member: `_send_local(choice, text, reply_target=f"project:{project_name}")`, then `mark_room_turn(choice)`. For every other `member` in `members`, `record_room_line(member, f"[room] PM: {text}")`.

`_send_local` returns `None`. Ignore that. `record_room_line` returns `None`. A witness of the human line uses the prefix `[room] PM: ` plus the original text, so a later turn sees who spoke. The addressee does not get that prefix; they are being asked, and their user message is the raw text.

Do not render a `→ @name` echo. That echo belongs to the `@` path (`:336`), which this branch does not use.

Import `threading`. Import `resolve_room_addressee` inside the worker, not at module import, so a chat-handler import does not load provider config. `jev_client` already loads the DecisionModel card lazily inside `post_decision`.

Line count: about 70.

### 2.5 `ui/agent_runtime/turn_ui.py` — reply witness

At the end of the successful path of `_do_response_complete`, after the reply has been handed to the chat surface (the method already renders before it returns), and only when `take_room_turn(session_key, complete_token)` returns `True`:

- If `text` is not a `str` or `text.strip()` is empty, stop. A tool-only completion has nothing for the room to hear.
- Speaker name: `resolved_name` from `:520`, or `session_key` when that is `None`.
- Line: `f"[room] {speaker}: {text}"`.
- Other members: `self._active_project[0]` is the project name (`:510`). Members come from a callback, not from a new `ProjectHandler` import inside the runtime handler.

Add on `AgentRuntimeHandler`:

```python
def set_on_room_members(self, cb) -> None:
    """``cb(project_name: str) -> list[str]``. None clears it."""
```

`window.py` assigns it next to the existing project-handler wire (`window.py:449`):

```python
self._agent_runtime_handler.set_on_room_members(
    self._project_handler.get_project_members
)
```

`get_project_members(project_name) -> list[str]` (`project_handler.py:305`) matches that signature. No project, no callback, or a callback that raises: log and skip the witness. `Exception` from the callback is caught.

For each member other than `session_key`, call `record_room_line(member, line)` on a daemon thread when `self._GLib` is not `None`, so the completion handler does not write N conversation files on the GTK thread. When `self._GLib` is `None`, call `record_room_line` directly. Do not render the room line. The project surface already shows the reply.

`take_room_turn` is false for a solo send, an `/ask` private turn, and a completion whose token was never marked. Those replies are not copied into anyone else's conversation.

A stale or duplicate completion returns before this point (`:469-490`). Do not witness those.

Line count: about 40 in `turn_ui.py`, about 15 in `window.py`.

### 2.6 Tests

New `tests/test_room_addressing.py`.

Resolver, with `post_decision` monkeypatched the way `tests/test_jev_ask_tool.py` does (`monkeypatch.setattr("agent.llm.jev_client.post_decision", ...)`):

| Case | `post_decision` result | Return |
|---|---|---|
| Choice `special:debugger`, confidence `0.91` | that answer | `special:debugger` |
| Choice `all`, confidence `0.91` | that answer | `all` |
| `(None, "down")` | | `all` |
| Confidence `0.59` | | `all` |
| Confidence `True` | | `all` |
| Choice `special:nope` (not in criteria) | | `all` |
| Empty roster | function is not called | `all` |

Assert the state passed into `post_decision` contains the human text and each display name. Assert `_ROOM_INSTRUCTIONS` contains `hi debugger`.

`note_room_line` on a runtime with a conversation and no active loop: the conv gains one `user` message, `step_count` is unchanged, return is `"stored"`. With the session in `_active_loops`: conv length unchanged, return is `"queued"`, and `_flush_pending_room_lines` then appends it. `save_conversation_to_disk` patched to raise `OSError`: return is still `"stored"` and the message is in memory.

Chat handler, extending the fakes in `tests/test_chat_handler.py` (`Fake` gateway, `FakeMainContent`, `make_handler`). Project tab `project:foo`, members `special:coder` and `special:debugger`, `get_solo_target` returning `None`:

- Monkeypatched resolver returns `special:debugger`. One `_send_local` / `send_to_special_agent` to `special:debugger` with the raw text and `reply_target="project:foo"`. `record_room_line("special:coder", "[room] PM: hi debugger")` is called. `special:debugger` is not passed to `record_room_line` for that human line. `post_decision` is not invoked on the main path when the resolver is patched at `resolve_room_addressee`.
- Resolver returns `all`. Both members are sent. `record_room_line` is not called with the human text.
- `get_solo_target` returns `special:coder`. Resolver is not called. One send to `special:coder`. This is the existing solo branch.

Reply witness: `take_room_turn` true, text `hey`, members the two keys. `record_room_line` is called for the other member with `[room] Debugger: hey` and not for the speaker. `take_room_turn` false: `record_room_line` is not called. Empty text: not called.

### Files NOT changed

- `ui/handlers/chat_handler.py` solo branch (`:410-418`), slash-command branch (`:206-276`), and inline `@` branch (`:314-383`).
- `ui/handlers/command_handler.py` — `@` resolution stays lexical.
- `ui/handlers/collab_handler.py` — `/ask`, `/delegate`, `/tell`, `/stop` stay.
- `ui/views/main_content.py` and `ui/window.py` `_on_agent_cycle_clicked` — the ALL label and the cycle stay. `window.py` gains one callback assignment only.
- `ui/views/chat_surface.py` and `ui/handlers/chat_render_handler.py` — no new bubble for a room line.
- `agent/llm/jev_client.py` `post_decision` — call it, do not change it.
- `models/conversation.py` — `add_user_message` already has the right effect.
- `ProjectHandler._solo_targets` — `None` remains ALL.

---

## 3. Data Flow

ALL, sentence names one member:

1. Bar label is `ALL` because `ProjectHandler.get_solo_target(name)` is `None` (`project_handler.py:396`, `main_content.py:324`).
2. `ChatHandler.on_send` (`:181`) reads the buffer, misses the slash and `@` paths, renders one You bubble (`:395-401`).
3. Worker calls `resolve_room_addressee("hi debugger", [("special:debugger", "Debugger"), ("special:coder", "Coder")])`.
4. `post_decision` returns a choice. A confident `special:debugger` is the addressee. Anything else is `"all"`.
5. Main thread: `send_to_special_agent("special:debugger", "hi debugger", reply_target="project:foo")` (`session.py:226`). That assigns `_turn_tokens["special:debugger"]` and starts `_run_loop`, which `add_user_message`s the raw text (`runtime.py:1741`) and calls the model. `mark_room_turn` stores that token.
6. Worker or the delivery callback: `record_room_line("special:coder", "[room] PM: hi debugger")`. Under `_prep_lock`, load or `create_conversation` (`defer_prompt_build=True`). `note_room_line` either `add_user_message`s and saves, or queues because `special:coder` is in `_active_loops`.
7. Debugger's `_do_response_complete` renders the reply into `project:foo` via the existing reply key. `take_room_turn` matches the token. A daemon thread `record_room_line("special:coder", "[room] Debugger: hey")`.
8. Later, Chat is still ALL and the human asks Coder something JEV routes to `special:coder`. Coder's `_run_loop` flushes any queue, then appends the new user message. `to_api_messages` includes `[room] PM: hi debugger` and `[room] Debugger: hey`. Coder can say they saw it. Coder did not run a turn on the greeting.

ALL, sentence names nobody:

1. Resolver returns `"all"` (JEV said so, or JEV was absent).
2. Delivery is the current loop: `_send_local` for every member, `mark_room_turn` on each, no human-line `record_room_line`.
3. Each completion witnesses `[room] {Name}: {reply}` onto the other members only.

Solo:

1. `get_solo_target` is `special:coder`.
2. The existing branch sends only to that member. `resolve_room_addressee` is not called. `mark_room_turn` is not called. A later completion does not witness.

---

## 4. File Change Summary

| File | Change | Lines | Risk |
|---|---|---|---|
| `agent/llm/jev_client.py` | Add `resolve_room_addressee` | ~70 | Low. New function, `post_decision` untouched |
| `agent/runtime.py` | Pending queue, `note_room_line`, flush in `_run_loop` | ~55 | Medium. `finally` and the pre-`add_user_message` site are on the hot path |
| `ui/handlers/agent_runtime_handler.py` | `record_room_line`, room-turn token | ~90 | Medium. Create-if-missing must not replace a live conv |
| `ui/handlers/chat_handler.py` | ALL branch consults JEV off the main thread | ~70 | Medium. Solo, slash, and `@` must stay byte-for-byte in behavior |
| `ui/agent_runtime/turn_ui.py` | Reply witness after a marked completion | ~40 | Low. Gated on `take_room_turn` |
| `ui/window.py` | `set_on_room_members(get_project_members)` | ~5 | Low |
| `tests/test_room_addressing.py` | New | ~250 | Low |
| `docs/ARCHITECTURE.md` | §3.14 and §3.21m paragraphs | ~30 | Low |

---

## 5. Implementation Order

1. `resolve_room_addressee` and the resolver tests. Verification: `python -m pytest tests/test_room_addressing.py -q -k resolver` (name the tests so this selects them). No UI.
2. `note_room_line` and `_flush_pending_room_lines`, plus the two `_run_loop` call sites. Verification: the queue and `OSError` tests. Confirm `grep -n "add_user_message(text)" agent/runtime.py` still shows the turn's own user message, and the flush call is above it.
3. `record_room_line` and the room-turn token methods. Verification: a unit test with a fake runtime whose `get_conversation` returns `None` then a conv, asserting `create_conversation` is called once and `step_count` is not assigned by `record_room_line`.
4. `ChatHandler` ALL branch. Verification: the three handler tests in §2.6. `python -m pytest tests/test_chat_handler.py tests/test_room_addressing.py -q`.
5. Reply witness and the `window.py` callback. Verification: the reply-witness tests.
6. `docs/ARCHITECTURE.md` paragraphs in §8 of this spec.
7. Full `python -m pytest tests/test_room_addressing.py tests/test_chat_handler.py tests/test_jev_ask_tool.py -q`.

---

## 6. Acceptance Criteria

- [ ] Chat is ALL. The human sends `hi debugger`. One agent turn starts, on the member whose display name is Debugger. Coder is not sent that text through `send_to_special_agent`. Coder's conversation contains a user message `[room] PM: hi debugger`. The project surface shows one You bubble with `hi debugger` and, after the turn, Debugger's reply. It does not show the `[room]` line.
- [ ] After that reply, Coder's conversation also contains `[room] Debugger: ` plus the reply text. Debugger's own conversation stores the reply as their assistant message, not as a second `[room] Debugger:` user line.
- [ ] A later ALL send that JEV routes to Coder, asking whether they saw `hi debugger`, is answered from a context that contains that room line.
- [ ] Chat is ALL. The human sends `hi`. Every project member is sent the raw text. No `[room] PM: hi` line is written in addition to those turns.
- [ ] JEV returns `None`, times out, returns confidence below 0.6, or returns an unknown choice. Delivery matches the `hi` case (every member).
- [ ] Chat is a named member. The send goes only to that member. `resolve_room_addressee` is not called. No room line is written.
- [ ] An inline `@debugger` send and a `/ask` send do not call `resolve_room_addressee`.
- [ ] A room line does not increment `step_count` and does not start `_run_loop` on the listener.
- [ ] A listener who is inside `_run_loop` does not see the room line in that turn's `to_api_messages()` calls. The line is on the conversation after that loop's `finally`.
- [ ] `post_decision` is not invoked on the GTK thread. With `GLib` set, the ALL branch's decision runs on a daemon thread.

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| No project members | Same as today: no sends. Resolver is not called when the roster is empty |
| Member with no display name | Left out of the JEV roster. Still sent when the choice is `all`. Cannot be chosen individually |
| Two members, same display name | Criteria keys are session keys, so both are distinct options. Descriptions include the session key |
| Choice is a session key that is not in `members` | Treat as `all` |
| Decision thread raises | Log, deliver as `all` |
| `record_room_line` raises, or `_get_runtime` raises `RuntimeError` | Log, continue. The addressee's send still happens |
| Listener has never been spoken to | `load_conversation` then `create_conversation(..., defer_prompt_build=True)`. The next real turn finds the conv and does not create a second one |
| `create_conversation` called when a conv already exists | Forbidden. It replaces the conv and disconnects MCP (`runtime.py:1060`) |
| Listener is mid-turn | Queue. Flush in that loop's `finally` after `_active_loops.discard`, under the same `_lock` |
| Save raises `OSError` | Log. Keep the in-memory line. Return `"stored"` |
| Addressee's turn fails or is cancelled | `_do_error` runs, not `_do_response_complete`. No reply room line. The human room line on the listeners stays |
| Empty completion text | No reply room line |
| Stale `complete_token` | Existing early return. `take_room_turn` is not called |
| Compaction later summarizes old user messages | Room lines are ordinary user messages and may be summarized by the existing strategy. This spec does not pin them |
| Process dies while a line is only queued | The queued line is memory-only until flush. Acceptable. Idle lines are saved inside `note_room_line` |
| `@` mention and this spec on the same sentence | The `@` path returns first (`chat_handler.py:331`). JEV does not see it |
| Solo DM of `hi debugger` | Unchanged. Other members do not get a room line. The PM asked for the new behavior only while ALL is selected |

---

## 8. ARCHITECTURE.md Updates Required

**§3.14 `ChatHandler`.** After the special-agent routing paragraph, add: when the project bar is ALL (`get_solo_target` is `None`) and the text is not a slash command or an inline `@` mention, `on_send` asks `resolve_room_addressee` on a worker thread. The choice is one member session key or `all`. `all` is the current fan-out. One member wakes only that member; every other member gets a `[room] PM: ...` user line through `record_room_line` and does not start a turn. A solo target skips the decision.

**§3.21m `agent/runtime.py`.** In the `jev_client` paragraph, add `resolve_room_addressee(text, roster) -> str`. It uses `post_decision` with one choice question named `addressee`. Failure returns `all`. Note `note_room_line`: a user-role overheard line, queued while `_active_loops` contains the session, otherwise appended and saved under `_lock`.

**§3.19 `ProjectHandler`.** The existing sentence that ALL (`None`) broadcasts to every member stays true for the `all` decision and for JEV failure. Add that a confident single-member choice narrows who answers and does not narrow who can later recall the line.

---

## 9. Self-audit

1. `post_decision` signature `(state: str, questions: dict) -> tuple[dict | None, str]`, never raises, 2.0 s timeout. The sample calls it with one question key `addressee`.
2. `add_user_message` does not change `step_count`. `add_assistant_message` does. Room lines use the first.
3. `create_conversation` replaces an existing conv. The spec forbids calling it unless `get_conversation` returned `None` under `_prep_lock`.
4. `save_conversation` takes `_lock` and raises `ValueError`. The spec calls `save_conversation_to_disk` under the lock already held, and catches `OSError`.
5. `send_to_special_agent` assigns `_turn_tokens[session_key]` before returning, so `mark_room_turn` can copy it. Early returns (unknown agent, no project) do not assign a token; `mark_room_turn` writes nothing when the token is missing.
6. `_run_loop` re-reads `conv.messages` on later iterations. Queuing while the session is in `_active_loops` is what keeps a witness out of the in-flight turn.
7. Solo, `@`, and slash paths are listed as unchanged and are above the branch this spec edits.
8. The ALL label is `solo_target is None`. The spec does not invent a second flag for the bar.
