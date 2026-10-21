# SPEC-45: `feed_card` tool — replace the crabcard fenced-block mechanism

**Date:** 2026-10-20
**Author:** Supervisor (PM request 2026-10-20 — "all this code needs to be removed cleanly, and then we build a feed_card tool")
**Status:** Draft — for implementation
**Depends on:** none (SPEC-44 and the SEARCH-CONTEXT-FIX phase series are landed and closed)
**Target branch:** main (base `985666fe`)
**Kind:** Removal + one new tool. No new package, no new send path.

> **Architecture compliance.** `agent/tools.py` owns the tool registry and every
> `ToolDefinition`; `agent/group_tools.py` owns the group-chat handlers whose UI
> effects go through callbacks installed from `ui/window.py`
> (`.crabcakes/architecture.md`, "agent/ — the engine"). `models/feed_card.py`
> owns `FeedCardData` and is a dependency of `ui/`, never the reverse.
> `ui/handlers/feed_handler.py` owns the card lifecycle. This spec ADDS one tool
> to the existing group-tool seam and DELETES `utils/crabcard_parser.py` plus its
> two call sites. It adds no package, no new send path, and no new file format.

---

## DISCOVERY

Read before any sample below was written.

- Read `utils/crabcard_parser.py` (172 lines, full source). Public surface: `extract_crabcards(text, project_name, agent_name="agent") -> tuple[str, list[FeedCardData]]`, `is_crabcards_placeholder(text) -> bool`, `get_placeholder_index(placeholder) -> int | None`, module constant `_CRABCARD_PLACEHOLDER = "\x00CRABCARD_REF:%d\x00"`. It builds `FeedCardData(card_type=..., source="agent", title=..., body=..., author=agent_name, timestamp=datetime.now(timezone.utc), project_name=..., file_path=..., additions=..., deletions=..., commit_sha=..., task_id=..., metadata={})`. `card_type` is **never validated** against `CardType` — any non-empty string passes. Malformed blocks are left in the returned text (not dropped).
- Read `models/feed_card.py` (full dataclass + helpers). `CardType = Literal["git_commit","diff","file_created","file_modified","file_deleted","dir_created","dir_deleted","agent_action","task","system","audit_report"]`. `CardSource = Literal["agent","system","git","crabwatch"]`. `FeedCardData` fields: `card_type, source, title, body, author, timestamp, project_name` (required) + `file_path, commit_sha, additions, deletions, task_id, metadata` (optional) + runtime fields `card_id, reviewed, accepted, seq_num, conversation_snapshot`. `MAX_STORED_BODY = 200_000`; `__post_init__` calls `cap_stored_body(self.body, site=self.card_type)`, which logs a WARNING and truncates at the cap — so a producer above 200k silently ships a lossy card.
- Read `agent/group_tools.py` (312 lines, full source). This is the house pattern the new tool must copy: module-level callback globals (`_agent_context_cb`, `_canvas_cb`, `_talk_cb`), a `set_*_callback(cb)` setter per tool, a handler that validates arguments and returns `ToolResult(success=False, error=...)` on every bad path, then calls the callback inside `try/except Exception` and re-checks `isinstance(result, ToolResult)`. `_require_supervisor(session_key, what)` exists but `feed_card` must NOT use it — every role can post cards. `canvas()` shows the size-guard shape; `talk()` shows the empty/too-long guard shape.
- Read `agent/tools.py` `_register_tools()` (`1831`–`2290`) and `_TOOLS` (`1828`). Registration shape is `_TOOLS[name] = (ToolDefinition(...), lambda ...: handler(...))`. The group-tool block starts at `2084` with `from agent import group_tools as _group` and registers `agent_context`, `diary`, `project_memory`, `jev_ask`, `canvas`, `talk` in that order. `get_all_tools()` returns `[defn for defn, _ in _TOOLS.values()]` — **16 tools today**: `read_file, write_file, edit_file, exec_command, list_files, search_files, file_search, web_search, web_fetch, provider_events, agent_context, diary, project_memory, jev_ask, canvas, talk`.
- Read `agent/tools.py:91-117` — the three `set_*_callback` re-export shims that lazily import from `agent.group_tools`. They exist because `ui/window.py` imports the setters from `agent.tools`.
- **CORRECTED finding — the `**kwargs` sink is still live.** `agent/tools.py:2425` calls `result = handler(**args)` after injecting `project_path`, `session_key`, `approval_callback`, `scratch_dir`. Every registered lambda ends in `**kwargs`, so **an unforwarded parameter is silently discarded, never raising**. Per SPEC-44 BUG #2 (re-verified there), a missed lambda edit is a silent wrong answer, not an error. Consequence: the public-path test through `execute_tool` is load-bearing for every new parameter in this spec.
- Read `agent/tools.py:395-423` `_resolve_project_path(path, project_path) -> str | None` — realpath + `os.path.commonpath` sandbox check; returns `None` when the path escapes. This is the exact helper a caller would reach for, but it returns the **absolute** resolved path, while `FeedCardData.file_path` is a display label (crabcard output was the literal relative string the agent wrote). **This spec does NOT call `_resolve_project_path`** — see §2.2 for why and what it does instead.
- Read `ui/handlers/feed_handler.py` `add_card(card_data, persist=True) -> str` (`359`). Thread-safe by construction: assigns `card_id = str(uuid.uuid4())` synchronously and returns it, takes `self._lock` for the seq counter and the store/index, then defers the document upsert to `self._GLib.idle_add(...)` and the `feed.json` append to a daemon thread. It handles `self._html_feed` false (card stored, no view) without error. **So a caller on the tool-loop thread can call it directly and use the returned id immediately.**
- Read `ui/handlers/feed_handler.py` `add_cards_batch(cards)` (`469`) and `get_card`/`get_cards_for_project`/`update_card`. `add_cards_batch` exists ONLY to batch N crabcards from one response into one idle callback (`477-484` explains it: "a single LLM response can contain N crabcards"). With per-call cards that rationale disappears; the method itself is still used by other producers and **must not be deleted in this spec**.
- Read `ui/agent_runtime/turn_ui.py` `_do_response_complete` (`448`–`639`). Two extraction sites: the streaming branch at `530-550` (`extract_crabcards`, then `metadata["session_key"] = session_key`, `metadata["tab_key"] = reply_key`, `add_cards_batch`, then `set_streaming_text(cleaned)`) and the non-streaming branch at `584-600` (same, minus the streaming overwrite). `reply_key = self._reply_key(session_key)` is bound once at `480`. The `set_streaming_text(session_key, text)` at `527-528` is a **RACE-FIX write that predates crabcards** and must stay.
- Read `ui/agent_runtime/turn_ui.py` `_do_tool_call_start` (`196`–`278`). **This is the collision site.** For every tool call it builds a `FeedCardData(card_type="agent_action", source="agent", title=f"{agent_name} is calling {name}", body="⏳ Running...", ...)`, calls `self._fh.add_card(card)`, and stores the id in `self._tool_card_ids[session_key]`. `_do_tool_call_result` (`280`) pops that id and updates the card. Without a guard, a `feed_card` tool call would emit TWO cards: its own `agent_action` running/complete row AND the card the tool posted.
- Read `ui/views/event_cards.py`. The crabcard surface is exactly three things: (1) the pre-scan loop in `process_segments` (`103`–`137`) that splits the text on the literal `"\x00CRABCARD_REF:"` and emits `{"type": "crabcard_placeholder", ...}` segments; (2) `_build_crabcard_placeholder_segment(seg)` (`462`–`479`), which renders a static `"📋 [card added to feed]"` label and already ignores its input (`card_index` is discarded, `_ = card_index`); (3) the dispatch branch `elif seg_type == "crabcard_placeholder":` at `496`–`497`. The module headers at `17`–`21` and `43`–`45` already document that the registry is gone and the placeholder is a static fallback.
- Read `ui/handlers/chat_render_handler.py`. `get_streaming_text` (`873`) and `set_streaming_text` (`885`) survive this spec — `get_streaming_text` has five other live callers (`turn_ui.py:558`, `:709`; `agent_runtime_handler.py:1736`, `1791`). Only the comments at `175`, `202`, `774`, `877`, `889-890`, `929` mention crabcards.
- Read `ui/window.py` `_wire_group_tool_callbacks` (`1047`–`1186`): imports the three setters from `agent.tools`, defines `_agent_context`, `_canvas`, `_talk` closures, then installs all three at `1184-1186`. `_canvas` documents the cross-thread pattern (tool-loop thread → `GLib.idle_add` → `threading.Event` wait) — **`feed_card` must NOT do that**: `FeedHandler.add_card` returns its id synchronously, so no hop and no wait are needed.
- Read `ui/window.py:488-497` — `self._feed_handler = FeedHandler(GLib=GLib, ...)` is constructed before `_wire_group_tool_callbacks()` is called at `803`, and `self._project_handler` is constructed at `387`. Both therefore exist by the time the new callback fires.
- Read `ui/handlers/project_handler.py:364` `get_active_project_name() -> str | None` and `:620` `get_active_project_path() -> str | None`. `ui/handlers/agent_runtime_handler.py:1232` `_reply_key(session_key) -> str` returns the turn-scoped reply target, else the routing project, else the raw session key — never `None`. There is **no public accessor** for it; `ui/window.py` already reaches into `self._agent_runtime_handler` for private members (`runtime_for`, `cancel_peer`, `clear_conversation`), so calling `_reply_key` from window.py follows existing precedent.
- Read `prompts/system/crabcakes-commands.md` — the `## Crabcards` section is lines `35`–`54`. It is loaded by `utils/prompt_loader.py:239` via `load_prompt_template("crabcakes-commands")` for **every** agent when a project is active.
- Read `prompts/default_agents/{coder,debugger,supervisor}.yaml`. Tool lists are flat YAML sequences. coder: 14 entries. debugger: 13. supervisor: 14 plus a commented `file_search` line. `tests/test_tools.py:2184` (`TestToolSchemaSizeBudget`, budget 4_000) currently measures coder **6940**, debugger **6505**, supervisor **6485** — **all three already fail, before this spec**.
- Read `tests/test_spec24_facts.py:164` `test_no_new_tool_name` — asserts the API-schema name set equals a 9-name literal. It **already fails at HEAD**: the set also contains `project_memory, canvas, jev_ask, agent_context, diary, talk`.
- Read `tests/test_crabcard_parser.py` (231 lines, 17 tests) and `tests/test_agent_runtime.py::TestCrabcardTabStamping`. **CORRECTED:** that class spans `5929`–`6228` and holds **14** tests, not 5 (per-method line numbers in §2.9). Only 5 of the 14 touch crabcards; the other 9 pin SPEC-12 reply-key / turn-slot / send-path behavior that is unrelated and must survive. The three reply-key tests inside it assert on `fh.add_cards_batch`, so they need re-pointing, not deleting.
- Read `tests/test_chat_render_handler.py::TestStreamingReplaceContract` (`372`–`390`). It pins `set_streaming_text` REPLACING the buffer, using the fixture string `"with crabcard blocks"` as arbitrary text. Behavior is unrelated to crabcards; only the wording is stale.
- Read `ui/views/left_panel.py:353-354`, `ui/handlers/feed_handler.py:477`, `models/feed_card.py:11,32,135,181` — remaining comments that say "crabcard". Comment-only.
- Read `docs/ARCHITECTURE.md`: `224`, `2940`, `3005`, `3220`–`3245` (§3.22e, the dedicated crabcard-parser section), `4242`, `4626`, `4717`. `knowledge/features.md:84`, `knowledge/projects.md:247,256`, `README.md:679`.
- **Architecture owner:** `agent/tools.py` owns the tool registry; `agent/group_tools.py` owns callback-backed tool handlers; `models/feed_card.py` owns the card dataclass; `ui/handlers/feed_handler.py` owns card lifecycle. No file in this spec crosses those boundaries.
- **Existing patterns copied:** the `canvas`/`talk` callback seam (setter in `group_tools`, shim in `tools.py`, install site in `window._wire_group_tool_callbacks`), and the argument-guard shape of `_validate_jev` / `canvas`. **Deviation, deliberate:** the callback body is a named window method (`_post_feed_card`) rather than a closure, so §2.9's preserved contracts are testable.

---

## 1. Overview

### Problem

Agents post to the project Feed by writing a ```crabcard fenced block into their
reply text. That design has four defects, all observed:

1. **Malformed cards vanish into prose.** `crabcard_parser` returns a malformed
   block verbatim in the chat text (never dropped), logs a warning nobody reads,
   and no card appears. The agent believes it posted.
2. **The card is only visible after the turn ends.** Extraction runs on the
   final response text (`turn_ui.py:530`, `:584`), so a card cannot be posted
   from a tool result mid-turn.
3. **`card_type` is unvalidated.** Any string passes into `FeedCardData`, where
   `css_class_for_type` falls back to `feed-card-system` (`models/feed_card.py:158`).
   A typo silently renders as a system card.
4. **The block is text, so it fights the renderer.** The parser injects
   `\x00CRABCARD_REF:N\x00` sentinels into the message, `process_segments` has a
   literal-string pre-scan to split them, and the resulting segment renders a
   hardcoded `"📋 [card added to feed]"` label that ignores its own index — a
   three-layer phrase-recognition Rube Goldberg for a card the agent already
   described structurally.

### Solution

One tool. `feed_card(type, title, body, file, additions, deletions, task_id)`.
The agent calls it; the tool validates every field; a window-installed callback
builds a `FeedCardData` and calls `FeedHandler.add_card`; the tool returns the
new `card_id`. Every crabcard artifact is deleted in the same change.

### Scope

| In scope | Out of scope |
|---|---|
| New `feed_card` tool (definition, handler, callback, wiring) | `FeedHandler` card lifecycle, accept/reject, auto-accept |
| Delete `utils/crabcard_parser.py` + its test file | `render/feed_html.py`, `models/feed_card.py` behavior |
| Delete both extraction sites in `turn_ui.py` | `get_streaming_text` / `set_streaming_text` (still used) |
| Delete the `crabcard_placeholder` segment path in `event_cards.py` | The Pango event-card pipeline generally |
| Suppress the duplicate `agent_action` card for `feed_card` calls | `add_cards_batch` (other producers still use it) |
| Replace `## Crabcards` in `crabcakes-commands.md` | `TestToolSchemaSizeBudget` (already red) |
| Add `feed_card` to the three default agent YAMLs | `test_no_new_tool_name`'s six pre-existing missing names |
| Update the docs/comments listed in §2.12 | `docs/ARCHITECTURE.md` rewrites beyond the crabcard sections |

### Architecture principles that apply

- One ownership boundary per file (see Discovery).
- UI effects from `agent/` go through callbacks installed by `ui/window.py`;
  `agent/` never imports `ui/`.
- A tool must validate at its own edge and return `ToolResult(success=False, ...)`
  for every rejectable input — never raise.
- Every new parameter needs a **public-path** test, because `**kwargs` swallows
  an unforwarded parameter silently.

---

## 2. Changes by File

### 2.1 `agent/group_tools.py` — new handler + setter

Add a module global beside the existing three, a setter, and the handler.

```python
_feed_card_cb = None


def set_feed_card_callback(cb) -> None:
    """Install the project-feed callback.

    Invoked from the feed_card handler after every field is validated.
    Signature: (session_key, fields: dict) -> ToolResult, where fields holds
    card_type, title, body, file_path, additions, deletions, task_id.
    None means the tool returns "feed_card is not wired".
    """
    global _feed_card_cb
    _feed_card_cb = cb
```

Handler:

```python
FEED_CARD_TYPES = ("diff", "agent_action", "audit_report")
FEED_CARD_TITLE_MAX = 200
FEED_CARD_BODY_MAX = 100_000


def feed_card(
    type: str,
    title: str,
    body: str = "",
    file: str = "",
    additions: int | None = None,
    deletions: int | None = None,
    task_id: str = "",
    **kwargs,
) -> ToolResult:
    session_key = kwargs.get("session_key", "")
    if not isinstance(type, str) or type not in FEED_CARD_TYPES:
        return ToolResult(
            success=False,
            error=(
                f"feed_card type must be one of {', '.join(FEED_CARD_TYPES)}"
            ),
        )
    if not isinstance(title, str) or not title.strip():
        return ToolResult(success=False, error="feed_card title is empty")
    title = title.strip()
    if len(title) > FEED_CARD_TITLE_MAX:
        return ToolResult(
            success=False,
            error=f"feed_card title exceeds {FEED_CARD_TITLE_MAX} characters",
        )
    if not isinstance(body, str):
        return ToolResult(success=False, error="feed_card body must be a string")
    body = body.strip()
    if len(body) > FEED_CARD_BODY_MAX:
        return ToolResult(
            success=False,
            error=f"feed_card body exceeds {FEED_CARD_BODY_MAX} characters",
        )
    file_path = ""
    if file:
        if not isinstance(file, str):
            return ToolResult(success=False, error="feed_card file must be a string")
        candidate = file.strip().replace("\\", "/")
        if (
            candidate.startswith(("/", "~"))
            or ":" in candidate.split("/", 1)[0]
            or ".." in candidate.split("/")
        ):
            return ToolResult(
                success=False,
                error="feed_card file must be a project-relative path",
            )
        file_path = candidate
    counts = {}
    for label, value in (("additions", additions), ("deletions", deletions)):
        if value is None:
            counts[label] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            return ToolResult(
                success=False, error=f"feed_card {label} must be an integer",
            )
        if value < 0:
            return ToolResult(
                success=False, error=f"feed_card {label} must not be negative",
            )
        counts[label] = value
    if not isinstance(task_id, str):
        return ToolResult(success=False, error="feed_card task_id must be a string")
    if _feed_card_cb is None:
        return ToolResult(success=False, error="feed_card is not wired")
    fields = {
        "card_type": type,
        "title": title,
        "body": body,
        "file_path": file_path,
        "additions": counts["additions"],
        "deletions": counts["deletions"],
        "task_id": task_id.strip(),   # safe: type-checked above, default ""
    }
    try:
        result = _feed_card_cb(session_key, fields)
    except Exception as exc:
        _logger.exception("feed_card callback failed")
        return ToolResult(
            success=False, error=f"feed_card failed: {exc.__class__.__name__}"
        )
    if not isinstance(result, ToolResult):
        return ToolResult(success=False, error="feed_card callback returned nothing")
    return result
```

**Do not "tidy" `exc.__class__.__name__` back into `type(exc).__name__`.** Inside
this function `type` is the `str` parameter, so `type(exc)` raises
`TypeError: 'str' object is not callable` — inside the `except` handler, which
turns a clean tool error into an unhandled crash. Verified against the real
interpreter. This is the one cost of the shadowed name and it is why the sample
spells the class lookup the long way.

Notes the implementer must not "improve":

- `type` shadows the builtin. It is deliberate: it is the name the model sees
  in the schema and the name used throughout the existing feed-card vocabulary.
  **Verified:** this project's resolved ruff config enables a large rule set
  (`linter.rules.enabled`, ~880 lines) but does **not** enable `flake8-builtins`
  (the `A` group) — a probe file `def f(type: str)` passes clean under the
  project config, and `--select A` on the same file reports `A002`. So this adds
  no new lint class. If `ruff check` shows a NEW rule id on this file, stop and
  report it — do not rename the parameter.
- The path check is a **rejection filter, not a sandbox.** `FeedCardData.file_path`
  is a display label on a card; nothing is opened from it. `_resolve_project_path`
  is the wrong tool here because it returns an absolute path and needs the file
  to exist. Reject anything that looks like an escape and otherwise trust the
  label.
- `body` is capped below `MAX_STORED_BODY` on purpose: 100_000 < 200_000, so this
  producer can never trigger `cap_stored_body`'s WARNING-and-truncate path.

### 2.2 `agent/tools.py` — setter shim + registration

Shim beside the existing three at `:91-117`:

```python
def set_feed_card_callback(cb) -> None:
    """Install the project-feed card callback.

    Trigger: the ``feed_card`` tool handler, after every field is validated.
    Signature ``(session_key, fields) -> ToolResult``.
    """
    from agent.group_tools import set_feed_card_callback as _set
    _set(cb)
```

Registration — append after the `talk` entry (currently the last group tool,
ends near `2290`), inside `_register_tools()`, where `_group` is already bound:

```python
    _TOOLS["feed_card"] = (
        ToolDefinition(
            name="feed_card",
            description=(
                "Post a card to the project Feed. type is diff, agent_action, "
                "or audit_report. Returns the new card id."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["diff", "agent_action", "audit_report"],
                    },
                    "title": {"type": "string", "description": "One line, max 200 chars"},
                    "body": {"type": "string", "description": "Card text, max 100,000 chars"},
                    "file": {"type": "string", "description": "Project-relative path"},
                    "additions": {"type": "integer", "description": "Lines added"},
                    "deletions": {"type": "integer", "description": "Lines removed"},
                    "task_id": {"type": "string", "description": "Optional work unit id"},
                },
                "required": ["type", "title"],
            },
            requires_approval=False,
        ),
        lambda type, title, body="", file="", additions=None, deletions=None,  # type: ignore
               task_id="", **kwargs: _group.feed_card(
            type, title, body, file, additions, deletions, task_id, **kwargs
        ),
    )
```

**Every one of the eight parameters must appear in the lambda and be forwarded
positionally.** Appending `**kwargs` alone and forwarding nothing is the exact
SPEC-44 BUG #2 failure mode: it is silent. The public-path test in §6 is what
proves it.

Payload cost: this entry serializes to roughly 450–500 characters, so coder's
schema goes ~6940 → ~7400. `TestToolSchemaSizeBudget` is already failing
(3 red, over by 2485–2940 against a 4_000 budget) and stays failing. Do not
touch `TOOL_PAYLOAD_BUDGET` or trim other descriptions — that is a separate unit
of work, explicitly out of scope (SPEC-44 flagged it too).

### 2.3 `ui/window.py` — install the callback

Inside `_wire_group_tool_callbacks()` (`1047`):

1. Add `set_feed_card_callback` to the existing `from agent.tools import (...)`
   block at `1058-1064`.
2. Add a new named method `_post_feed_card` on the window class, immediately
   after `_wire_group_tool_callbacks` (which ends at `1186`).

**Do NOT write this as a closure.** A closure inside `_wire_group_tool_callbacks`
is unreachable from tests, and the two metadata stamps it sets are exactly the
contracts §2.9 requires us to keep pinned. A named method is testable with a
stub `self`.

```python
    def _post_feed_card(self, session_key: str, fields: dict) -> "ToolResult":
        """feed_card tool callback — build a FeedCardData and add it to the feed.

        Runs on the caller's tool-loop thread. FeedHandler.add_card is
        thread-safe by construction (assigns the id synchronously, defers its
        own GTK work), so no idle hop and no event wait are needed here —
        unlike _canvas, which must wait for a surface to mount.

        project_name and tab_key come from DIFFERENT sources BY DESIGN
        (SPEC-12 R7 + the SP5b divergence pin): project_name is the ACTIVE
        project (the card feeds where the PM is looking); tab_key is the
        emitting agent's own mounted tab (where the turn rendered). Do not
        unify them.
        """
        from datetime import UTC, datetime

        from agent.special_agents import get_special_agent
        from models.feed_card import FeedCardData

        project_name = self._project_handler.get_active_project_name()
        if not project_name:
            return ToolResult(success=False, error="feed_card needs an open project")
        agent_def = get_special_agent(session_key)
        author = (
            agent_def.display_name
            if agent_def is not None and getattr(agent_def, "display_name", "")
            else session_key.split(":", 1)[-1]
        )
        card = FeedCardData(
            card_type=fields["card_type"],
            source="agent",
            title=fields["title"],
            body=fields["body"],
            author=author,
            timestamp=datetime.now(UTC),
            project_name=project_name,
            file_path=fields["file_path"] or None,
            additions=fields["additions"],
            deletions=fields["deletions"],
            task_id=fields["task_id"] or None,
            metadata={
                "session_key": session_key,
                "tab_key": self._agent_runtime_handler._reply_key(session_key),
            },
        )
        card_id = self._feed_handler.add_card(card)
        return ToolResult(
            success=True,
            output=f"posted {card.card_type} card {card_id}",
        )
```

3. Install it at `1186`: `set_feed_card_callback(self._post_feed_card)`.

Imports. `ui/window.py` has **no module-scope `datetime` or `FeedCardData`
import** (verified by search). Put all three imports at the top of
`_post_feed_card`, matching the pattern `_emit_bridge_card` already uses
(`ui/window.py:1432-1434`):

```python
        from datetime import UTC, datetime

        from agent.special_agents import get_special_agent
        from agent.tools import ToolResult
        from models.feed_card import FeedCardData
```

`UTC`/`datetime` and `FeedCardData` are the exact forms used by
`_emit_bridge_card`. Use `datetime.now(UTC)` — not `timezone.utc` — so the file
stays consistent with itself.

`get_special_agent` is imported inside `_wire_group_tool_callbacks` at `1057`
and `ToolResult` at `1059` — both **method-local**, so they are NOT in scope
inside a separate `_post_feed_card` method. Import `get_special_agent` locally as
shown. `ToolResult` is referenced in the return type and bodies: either add it to
the same local import block (`from agent.tools import ToolResult`) or drop the
quoted return annotation. Prefer importing it — the annotation is useful.

**No event wait, no idle hop.** `FeedHandler.add_card` returns the id
synchronously and defers its own GTK work internally (`feed_handler.py:359-467`),
so the tool-loop thread returns immediately. Do not copy `_canvas`'s
`threading.Event` shape.

### 2.4 `ui/agent_runtime/turn_ui.py` — delete both extraction sites, suppress the duplicate card

**Edit A — delete the streaming extraction block (`530`–`550`).** Everything
from the comment `# Phase C: Extract crabcards...` through
`self._crh.set_streaming_text(session_key, cleaned)` goes. The two lines above it
stay:

```python
            if was_streaming and text:
                self._crh.set_streaming_text(session_key, text)
```

That write is RACE-FIX, not crabcard-cleanup (`streaming_text` at `:558` depends
on it).

**Edit B — delete the non-streaming extraction block (`584`–`600`).** Replace
the whole `if not was_streaming and text:` body with the render only:

```python
            if not was_streaming and text:
                chat_box = self._resolve_chat_box(session_key)
                if chat_box is not None:
                    bubble = self._crh.render_sync(
                        "Agent", text, session_key, agent_name=resolved_name or "Agent",
                        mount_key=reply_key,
                    )
                    if bubble is not None:
                        chat_box.append(bubble)
```

Also update the method docstring (`448`–`466`): drop the "Phase C" paragraph and
replace it with one line stating cards are posted by the `feed_card` tool, not
extracted from text.

**Do NOT delete the `project_name` local at `510`.** After Edits A+B it no longer
appears in the response-complete path, which makes it look dead — it is not. It
is read again at `:613` by the agent-command hook
(`self._on_agent_response(session_key, text, project_name)`). Removing it breaks
that call. Same for `was_streaming` (still read at `:558`, `:573`, `:584`).

**Edit C — suppress the duplicate `agent_action` card.** At `211`:

```python
        if self._fh is not None and self._active_project is not None and name != "feed_card":
```

Nothing else changes: `_do_tool_call_result` pops `self._tool_card_ids[session_key]`,
finds nothing, and skips (`290-293`). The activity-drawer bubble at `269-278`
still fires — the drawer is a different surface and has no duplicate problem.

### 2.5 `ui/views/event_cards.py` — delete the placeholder path

**Edit A — `process_segments` (`90`–`138`).** Replace the entire body after the
docstring with:

```python
    processed: list = []
    _process_text_chunk(text, processed)
    return processed
```

Delete the docstring bullet `- crabcard_placeholder segments: 'index' key — rendered as feed reference chip`
(`98`) and the `# Phase 3: Pre-scan ...` comment block (`103`–`105`).

**Edit B — delete `_build_crabcard_placeholder_segment` (`462`–`479`).**

**Edit C — delete the dispatch branch (`496`–`497`):**
`elif seg_type == "crabcard_placeholder": return _build_crabcard_placeholder_segment(seg)`.

**Edit D — module header (`7`–`21`, `43`–`45`).** Remove
`_build_crabcard_placeholder_segment` from the relocated-verbatim list at `14`,
and replace the two B.0/B.2 comment blocks with one line noting the crabcard
placeholder path was deleted by SPEC-45.

### 2.6 `get_streaming_text` / `set_streaming_text` — KEEP

Both survive. `get_streaming_text` has five live non-crabcard callers.
`set_streaming_text` is the RACE-FIX write and the REPLACE-contract test's
subject. Only the docstrings change:

- `chat_render_handler.py:877` — "Used by AgentRuntimeHandler to extract
  crabcards..." → "Used by AgentRuntimeHandler to read the buffered text."
- `chat_render_handler.py:889-890` — same, drop the crabcard clause.
- `tests/test_chat_render_handler.py:372` — reword the class docstring to
  "set_streaming_text must REPLACE the pending buffer"; change the fixture
  string `"with crabcard blocks"` to any other text.
- `tests/test_chat_render_handler.py:385` — the assertion `"crabcard" not in
  ...` follows the fixture change; rewrite it as a REPLACE assertion (e.g.
  `assert "cleaned" in ... and "original" not in ...`).

### 2.7 DELETE `utils/crabcard_parser.py`

Delete the file. Zero live importers remain after §2.4 and §2.5.

### 2.8 DELETE `tests/test_crabcard_parser.py`

17 tests, all of them about the deleted module. Delete the file.

### 2.9 `tests/test_agent_runtime.py` — delete 2 tests, re-point 2, move 1

**Scope correction — read this twice.** `TestCrabcardTabStamping` spans
`5929`–`6228` and contains **fourteen** tests, not five. Only the first five are
crabcard tests:

| Line | Test | Action |
|---|---|---|
| `5935` | `test_streaming_crabcards_carry_tab_metadata` | **DELETE** |
| `5961` | `test_nonstreaming_crabcards_carry_tab_metadata` | **DELETE** |
| `5990` | `test_streaming_reply_key_uses_turn_target_over_routing` | **KEEP** |
| `6019` | `test_reply_key_falls_back_to_active_project` | **KEEP** |
| `6046` | `test_multiproject_divergence_is_deliberate` | **KEEP** |
| `6089` | `test_unrouted_agent_renders_into_open_project` | KEEP |
| `6107` | `test_turn_scoped_slot_group_then_private_then_group` | KEEP |
| `6126` | `test_send_to_special_agent_normalizes_slot_every_send` | KEEP |
| `6149` | `test_response_complete_clears_own_slot` | KEEP |
| `6160` | `test_stale_token_leaves_slot_intact` | KEEP |
| `6172` | `test_nested_send_slot_not_clobbered` | KEEP |
| `6191` | `test_none_token_completion_leaves_slot_intact` | KEEP |
| `6204` | `test_none_token_error_leaves_slot_intact` | KEEP |
| `6216` | `test_no_project_send_clears_stale_slot` | KEEP |

(The class runs to the SPEC-09 SP2 banner at `6229`. Deleting to `6090` or to
`6228` would destroy nine SPEC-12 race-fix pins.)

**The three "KEEP" reply-key tests at `5990`/`6019`/`6046` do not die with the
mechanism.** They are NOT crabcard tests in substance — they drive
`_do_response_complete` and assert on `fh.add_cards_batch`, which is why they
carry crabcard fixtures. After §2.4 removes the extraction, those assertions have
nothing to observe, so the tests must be **re-pointed**, not deleted:

- `5990`, `6019` — the `tab_key` half is a pure `_reply_key` property. Delete the
  crabcard fixture and assert `handler._reply_key("special:coder")` directly.
  Both cases are already independently pinned at `6100` and `6118`, so this is
  a de-duplication, not a loss.
- `6046` — **cannot** be reduced to `_reply_key`. It pins a two-source
  divergence: `project_name` is the ACTIVE project while `tab_key` is the
  agent's ROUTED tab. After §2.4 the `project_name` stamp no longer exists in
  `_do_response_complete`; it moves to `window._post_feed_card` (§2.3), which
  reads `self._project_handler.get_active_project_name()` while `tab_key` comes
  from `self._agent_runtime_handler._reply_key(session_key)`. Two different
  sources, same divergence. **Move this test into `tests/test_feed_card_tool.py`
  and drive `_post_feed_card` directly** with a stub window whose
  `_project_handler.get_active_project_name()` returns `"B"` and whose
  `_agent_runtime_handler._reply_key()` returns `"project:A"`. Assert both.

Absent that, the divergence pin — the only one of its kind — is lost.

The two `*_carry_tab_metadata` tests are genuinely dead: the stamps now happen in
one place, `window._post_feed_card` (§2.3), and are covered by §6.1.

### 2.10 `prompts/system/crabcakes-commands.md` — replace §Crabcards

Replace lines `35`–`54` (from `## Crabcards` to the end of the file's crabcard
section) with:

```markdown
## Feed cards

Post to the project Feed with the `feed_card` tool. Never write a `crabcard`
block — that format is gone.

- `type` — `diff` when you change a file, `agent_action` for a decision or a
  finding, `audit_report` for an audit result.
- `title` — one line, max 200 characters. `body` — the card text, max 100,000.
- `file` — project-relative path. `additions` / `deletions` — line counts.
  `task_id` — optional work unit id.
- The tool returns the new card id. A rejected call returns an error — read it
  and fix the field; do not retry the same arguments unchanged.
- Do **not** post for `file_created/modified/deleted` (CrabWatch detects those),
  `git_commit` (automatic), or `system` (internal). Those types are not accepted.
- `diff` body: unified diff, `+`/`-` prefixes, context lines unprefixed.
```

`tests/test_task_redesign_prompts.py::TestCrabcakesCommands` does not test the
crabcard section, so no prompt test needs changing.

### 2.11 Default agent YAMLs — add the tool

`prompts/default_agents/coder.yaml`, `debugger.yaml`, `supervisor.yaml`: append
`- feed_card` to the `tools:` list. Place it after `talk`.

All three need it: Coder posts `diff` cards on writes, Debugger posts
`audit_report`, Supervisor posts `agent_action` decisions.

### 2.12 Comment/doc updates (no behavior)

| File | Lines | Change |
|---|---|---|
| `models/feed_card.py` | `9-11`, `32`, `135`, `181` | drop "crabcard" from the importers list and comments |
| `ui/handlers/feed_handler.py` | `477` | "a single LLM response can contain N crabcards" → "a batch producer can hand over N cards" |
| `ui/handlers/chat_render_handler.py` | `175`, `202`, `774`, `877`, `889-890`, `929` | crabcard wording |
| `ui/views/left_panel.py` | `353-354` | crabcard wording |
| `ui/window.py` | `484-485`, `586-589` | crabcard wording |
| `ui/agent_runtime/turn_ui.py` | `325`, `452-456` | crabcard wording (docstrings/comments) |
| `docs/ARCHITECTURE.md` | `224`, `2940`, `3005`, `3220-3245`, `4242`, `4626`, `4717` | delete §3.22e; fix the five references |
| `knowledge/features.md` | `84` | replace the crabcard paragraph with the tool |
| `knowledge/projects.md` | `247`, `256` | same |
| `README.md` | `679` | drop the `crabcard_parser.py` tree entry |

### Files NOT changed (already correct, or deliberately out of scope)

- `models/feed_card.py` — `FeedCardData`, `CardType`, `MAX_STORED_BODY`,
  `css_class_for_type`, `is_actionable`, `is_informational`: **unchanged**. The
  tool feeds the same dataclass the parser did.
- `render/feed_html.py` — renders `FeedCardData`; no crabcard knowledge.
- `ui/handlers/feed_handler.py` — `add_card`, `add_cards_batch`, `update_card`,
  `get_card` unchanged. `add_cards_batch` is still used by other producers.
- `ui/handlers/chat_render_handler.py` — `get_streaming_text` /
  `set_streaming_text` behavior unchanged; comments only.
- `tests/test_tools.py::TestToolSchemaSizeBudget` — already red at HEAD
  (6940 / 6505 / 6485 vs 4_000). Not fixed here.
- `tests/test_spec24_facts.py::test_no_new_tool_name` — already red at HEAD
  (six names missing: `project_memory, canvas, jev_ask, agent_context, diary,
  talk`). §6.4 adds `feed_card` to the literal so the tool is covered; the
  other six stay missing and the test stays red for exactly the pre-existing
  reasons.
- `tests/test_agent_runtime.py:6070` and the surrounding `test_unrouted_*`
  tests — not crabcard tests, leave them.
- `agent/context.py`, `agent/runtime.py` — no crabcard knowledge.

---

## 3. Data Flow

**Agent posts a card mid-turn:**

```
model emits a tool call: feed_card(type="diff", title=..., file=...)
  → AgentRuntime tool loop (runtime.py:2525-2534)
    → _tool_chain.run(... executor=execute_tool(...))
      → execute_tool("feed_card", args, project_path, session_key, ...)      agent/tools.py:2332
        → allowed_tools gate (:2372)                    [deny if not permitted]
        → entry = _TOOLS["feed_card"] (:2410)
        → args injected with project_path/session_key/approval_callback/scratch_dir (:2419-2423)
        → lambda(type, title, body, file, additions, deletions, task_id, **kwargs)
          → group_tools.feed_card(...)                                        agent/group_tools.py
            → validate type/title/body/file/counts/task_id
            → _feed_card_cb(session_key, fields)
              → window._post_feed_card(session_key, fields)                   ui/window.py
                → project_handler.get_active_project_name()   [None → tool error]
                → get_special_agent(session_key).display_name → author
                → FeedCardData(card_type, source="agent", ..., metadata={
                      "session_key": session_key,
                      "tab_key": agent_runtime_handler._reply_key(session_key)})
                → feed_handler.add_card(card) → card_id                       ui/handlers/feed_handler.py:359
                    uuid4 → _lock: seq_num + _cards + _project_cards
                    → _GLib.idle_add(upsert document)      [main thread]
                    → thread(feed_store.append_feed_card)  [persist]
                → ToolResult(success=True, output=f"posted diff card {card_id}")
        → bound_tool_output(result, "feed_card")           [under 16 KB, no-op]
      → back in the loop: conv.add_tool_result(call_id, text)                  agent/runtime.py:2556
  → turn_ui._do_tool_call_start("feed_card", args)          ui/agent_runtime/turn_ui.py:211
      name == "feed_card" → the agent_action feed-card block is SKIPPED
      activity-drawer bubble still emitted (:269-278)
  → turn_ui._do_tool_call_result("feed_card", ...)
      _tool_card_ids.pop → None → no card update (`:290-293`)
```

**Agent replies with prose:** no extraction anywhere. `text` renders as-is
(`_append_to_surface`, `chat_render_handler.py:536`).

**Key structures:**
- `_TOOLS: dict[str, tuple[ToolDefinition, Callable[..., ToolResult]]]` — the
  registry key is the tool name.
- `FeedHandler._cards: dict[card_id, FeedCardData]`,
  `_project_cards: dict[project_name, list[card_id]]` (newest first),
  `_project_seq: dict[project_name, int]`.
- The `fields` dict passed to the callback is flat and pre-validated; the
  callback performs no validation of its own except the project check.

---

## 4. File Change Summary

| File | Change | Est. lines | Risk |
|---|---|---|---|
| `agent/group_tools.py` | add setter + `feed_card` handler | +105 | Low |
| `agent/tools.py` | setter shim + registration | +42 | Low |
| `ui/window.py` | new `_post_feed_card` method + install + import | +45 / −3 | Medium |
| `ui/agent_runtime/turn_ui.py` | delete 2 blocks, add guard, docstrings | −40 / +8 | **High** |
| `ui/views/event_cards.py` | delete placeholder path | −40 | Medium |
| `utils/crabcard_parser.py` | **DELETE** | −172 | Low |
| `tests/test_crabcard_parser.py` | **DELETE** | −231 | Low |
| `tests/test_agent_runtime.py` | delete 2 tests, re-point 2, move 1 out | −70 / +6 | **High** |
| `tests/test_feed_card_tool.py` | **NEW** | +330 | Low |
| `tests/test_chat_render_handler.py` | reword 2 docstrings, 2 asserts | ±6 | Low |
| `tests/test_spec24_facts.py` | add `feed_card` to the literal | +1 | Low |
| `prompts/system/crabcakes-commands.md` | replace §Crabcards | −20 / +16 | Low |
| `prompts/default_agents/*.yaml` | add `- feed_card` ×3 | +3 | Low |
| `models/feed_card.py` | comments only | ±4 | Low |
| `ui/handlers/feed_handler.py` | comment only | ±3 | Low |
| `ui/handlers/chat_render_handler.py` | comments/docstrings only | ±8 | Low |
| `ui/views/left_panel.py` | comment only | ±2 | Low |
| `docs/ARCHITECTURE.md` | delete §3.22e, fix 5 refs | −30 | Low |
| `knowledge/features.md`, `knowledge/projects.md`, `README.md` | wording | ±12 | Low |

Net: roughly **−700 / +600** lines.

---

## 5. Implementation Order

Each step ends with its own verification. Do not proceed on a red step.

1. **`agent/group_tools.py`** — add `_feed_card_cb`, `set_feed_card_callback`,
   `feed_card`. Verify: `.venv/bin/python -c "from agent.group_tools import feed_card, set_feed_card_callback; print(feed_card('diff','t'))"`
   prints a `ToolResult(success=False, error='feed_card is not wired')`.
2. **`agent/tools.py`** — shim + registration. Verify:
   `.venv/bin/python -c "from agent.tools import get_tool_definitions_for_api as g; d=[x['function']['name'] for x in g()]; assert 'feed_card' in d; print(len(d))"`
   → prints `17`.
3. **`ui/window.py`** — `_post_feed_card` + install. Verify: py_compile passes,
   and `tests/test_feed_card_tool.py::test_divergence` (the moved test) can drive
   the method with a stub window. App-start is a manual PM-side check.
4. **`ui/agent_runtime/turn_ui.py`** — Edit C (the duplicate-card guard) FIRST,
   while the old path still exists, so a regression is attributable.
   Verify: `tests/test_agent_runtime.py` passes unchanged.
5. **`ui/agent_runtime/turn_ui.py`** — Edits A + B (delete extraction).
   Verify: `tests/test_agent_runtime.py` fails in **exactly five** tests:
   `TestCrabcardTabStamping::test_streaming_crabcards_carry_tab_metadata`,
   `::test_nonstreaming_crabcards_carry_tab_metadata`,
   `::test_streaming_reply_key_uses_turn_target_over_routing`,
   `::test_reply_key_falls_back_to_active_project`, and
   `::test_multiproject_divergence_is_deliberate`. Any SIXTH failure, or any
   failure outside that class, is a regression — stop and report.

   (If only two fail, the three reply-key tests were re-pointed in step 5 rather
   than step 8 — that is fine and better; the exit condition is "no failure
   outside the five listed names".)
6. **`ui/views/event_cards.py`** — delete the placeholder path.
   Verify: `tests/test_activity_bubbles.py`, `tests/test_chat_terminal_segment.py`,
   `tests/test_render_error_callbacks.py` pass.
7. **Delete** `utils/crabcard_parser.py` and `tests/test_crabcard_parser.py`
   (Supervisor step — `rm` is blocked for Coder; write the files to 0 bytes and
   report, or hand the deletion to the Supervisor).
8. **Test surgery** (§2.9): delete the two `*_carry_tab_metadata` tests,
   re-point the two reply-key tests, move the divergence test into
   `tests/test_feed_card_tool.py`. Then write the rest of
   `tests/test_feed_card_tool.py`.
   Verify: `.venv/bin/python -m pytest tests/test_feed_card_tool.py -q` green;
   `tests/test_agent_runtime.py` green; and
   `.venv/bin/python -m pytest tests/test_agent_runtime.py -q -k "reply_key or divergence or unrouted or slot"`
   still collects ≥8 tests (proves no accidental mass deletion).
9. **`prompts/system/crabcakes-commands.md`** + the three agent YAMLs.
   Verify: `.venv/bin/python -m pytest tests/test_task_redesign_prompts.py -q` green.
10. **Comment/doc sweep** (§2.12). Verify the grep in §6.5 returns zero product hits.
11. **Final**: `.venv/bin/python -m pytest tests/test_feed_card_tool.py tests/test_agent_runtime.py tests/test_tools.py tests/test_chat_render_handler.py tests/test_activity_bubbles.py tests/test_jit_context_discovery.py tests/test_spec24_facts.py -q`
    and paste the output.

---

## 6. Acceptance Criteria

### 6.1 The tool

- [ ] `feed_card` appears in `get_tool_definitions_for_api()` and in
      `get_all_tools()`.
- [ ] `execute_tool("feed_card", {"type": "diff", "title": "T"}, proj)` reaches
      the handler **with both arguments forwarded** (public-path test — the
      `**kwargs` sink makes a missed forward silent).
- [ ] Every one of the eight parameters arrives at the handler when passed
      through `execute_tool` (one test that passes all eight and inspects the
      callback's `fields` dict).
- [ ] A successful call returns `success=True` and an output containing the
      returned `card_id`.
- [ ] `additions` / `deletions` reach `FeedCardData` as `int`; `None` when omitted.
- [ ] `metadata["session_key"]` is the caller's session key and
      `metadata["tab_key"]` is `_reply_key(session_key)`.
- [ ] `source` is `"agent"`.

### 6.2 The rejections (all return `success=False`, never raise)

| Input | Error contains |
|---|---|
| `type="system"` | `must be one of diff, agent_action, audit_report` |
| `type` omitted | `received invalid arguments` (the `TypeError` path in `execute_tool:2431`) |
| `title=""` | `title is empty` |
| `title` 201 chars | `title exceeds 200` |
| `body` 100_001 chars | `body exceeds 100000` |
| `file="/etc/passwd"` | `project-relative path` |
| `file="../../x.py"` | `project-relative path` |
| `file="C:/x.py"` | `project-relative path` |
| `additions="5"` | `additions must be an integer` |
| `additions=True` | `additions must be an integer` |
| `additions=-1` | `must not be negative` |
| callback not installed | `feed_card is not wired` |
| no project open | `feed_card needs an open project` |

### 6.3 The deleted contracts, re-pinned

Three of the five crabcard-era tests pin real design decisions that survive the
mechanism change. They must be re-expressed against `_post_feed_card` /
`_reply_key` semantics:

- [ ] **Turn slot beats routing.** With `_turn_reply_target[sk] = sk` and
      routing `sk → project:alpha`, `_reply_key(sk)` is `sk`, not
      `project:alpha` — so the card's `metadata["tab_key"]` inherits `sk`.
- [ ] **Active-project fallback.** With no `_agent_to_project` and an active
      project `alpha`, `_reply_key(sk)` is `"project:alpha"`, never the raw
      session key — so the card's `metadata["tab_key"]` inherits it.
- [ ] **Divergence is deliberate.** Drive `_post_feed_card` with
      `_project_handler.get_active_project_name() → "B"` and
      `_agent_runtime_handler._reply_key() → "project:A"`. Assert
      `card.project_name == "B"` and `card.metadata["tab_key"] == "project:A"`.
      Do not unify the two sources. (This test MOVES from
      `test_agent_runtime.py` into `test_feed_card_tool.py` — see §2.9.)

The other two tests (`*_carry_tab_metadata`) are subsumed by the tool tests —
the stamps now happen in one place, `_post_feed_card`.

### 6.4 The removal

- [ ] `import utils.crabcard_parser` raises `ModuleNotFoundError`.
- [ ] `tests/test_crabcard_parser.py` does not exist.
- [ ] `TestCrabcardTabStamping` does not exist **as a class** — but its nine
      non-crabcard tests must be preserved. Verify by count: the reply-key /
      slot / divergence tests that lived in it are either still in
      `test_agent_runtime.py` or moved to `test_feed_card_tool.py`, and
      `pytest tests/test_agent_runtime.py -q -k "reply_key or slot or divergence or unrouted" --collect-only`
      reports at least 8 items.
- [ ] `turn_ui._do_response_complete` contains no call to `extract_crabcards`,
      no `add_cards_batch`, and no `set_streaming_text(cleaned)`.
- [ ] `process_segments` contains no `CRABCARD_REF` literal.
- [ ] `_build_crabcard_placeholder_segment` does not exist and is not dispatched.
- [ ] A `feed_card` tool call produces exactly ONE feed card (its own), not two.
- [ ] `tests/test_spec24_facts.py::test_no_new_tool_name` has `feed_card` in its
      literal (it stays red for the six pre-existing missing names — that is
      expected and must be reported, not fixed).

### 6.5 Pattern sweep — must return zero product hits

```
search_files pattern="crabcard" path=. file_type=py
search_files pattern="crabcard" path=prompts
```

Allowed remaining hits: `knowledge/*.md` and `docs/` history notes marked with
SPEC-45 (i.e. sentences that say the format was removed), and nothing else. Zero
hits in `agent/`, `ui/`, `models/`, `utils/`, `render/`, `tests/`, `prompts/`.

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| No project open | Tool error `feed_card needs an open project`; no card, no crash |
| Project open, no feed surface attached | `add_card` stores the card and returns an id (`feed_handler.py:455-467`); tool succeeds |
| Two `feed_card` calls in one turn | Two cards, one `agent_action` suppression each, no duplicate rows |
| `feed_card` denied by `allowed_tools` | The gate at `tools.py:2372` rejects before the handler; error names the tool |
| Agent not in the special-agent registry | `author` falls back to `session_key.split(":", 1)[-1]` |
| `body` exactly 100_000 chars | Accepted; `cap_stored_body` does not fire (limit is 200_000) |
| `body` 100_001 chars | Rejected with the tool error; **no silent truncation** |
| `file` given but the file does not exist | Accepted — `file_path` is a display label, nothing is opened |
| `file` uses backslashes | Normalized to `/` before the escape checks |
| `task_id` empty string | Card stores `None`, not `""` |
| Card posted, then the turn fails | The card stays in the feed (it was persisted independently) |
| Callback raises | `feed_card failed: <ExceptionType>`; the loop continues |
| Callback returns a non-`ToolResult` | `feed_card callback returned nothing` |
| Malformed reply text (old transcripts replayed) | Plain prose; the old `crabcard` fenced block renders as an ordinary code block. No crash — `process_segments` has no special case. |

---

## 8. ARCHITECTURE.md Updates Required

`docs/ARCHITECTURE.md`:

1. **Delete §3.22e** (`3220`–`3245`) in full — `utils/crabcard_parser.py` is gone.
2. **Line `224`** — remove the `crabcard_parser.py` tree entry.
3. **Line `2940`** — `card_type` key paragraph: replace "`utils/crabcard_parser.py`
   for the wire format" with "`agent/group_tools.py:feed_card` for the tool
   contract".
4. **Line `3005`** — `FeedHandler` responsibility: replace "Coordinates with
   `CrabWatchHandler` (filesystem events) and `ChatRenderHandler` (crabcard
   extraction)" with "Coordinates with `CrabWatchHandler` (filesystem events) and
   the `feed_card` tool callback (agent-posted cards)".
5. **Line `4242`** — delete the `tests/test_crabcard_parser.py` line; add
   `tests/test_feed_card_tool.py`.
6. **Lines `4626`, `4717`** — remove both tree entries.

`.crabcakes/architecture.md` is git-ignored (`gitignore:37` in SPEC-44's note).
Do not edit it; the tracked `docs/ARCHITECTURE.md` is the one that ships.

Also add one sentence to the `agent/` section of `docs/ARCHITECTURE.md`: the
`feed_card` tool is the ONLY supported way for an agent to post to the project
Feed; the fenced-block format was removed by SPEC-45.

---

## 9. Rollback

Single revert. The deletion and the addition are in the same commit; the old
mechanism has no persisted state to migrate. `feed.json` cards written by either
mechanism are identical `FeedCardData` records, so existing feeds load unchanged.
