# SPEC-45 PHASE 3 of 9 — install the callback in `ui/window.py`

**Master spec:** `docs/specs/SPEC-45-FEED-CARD-TOOL.md` — read §2.3 and §2.9 (the
`6046` paragraph) before writing.

**Scope:** TWO files. `ui/window.py` and the new `tests/test_feed_card_tool.py`.
Do not touch any other file. In particular **do NOT edit `tests/test_agent_runtime.py`**
— deleting the two dead tests and re-pointing the reply-key tests is Phase 8 (step 8).

**Already landed, do not redo:**
- Phase 1: `agent/group_tools.py` has `_feed_card_cb`, `set_feed_card_callback`,
  `feed_card()`.
- Phase 2: `agent/tools.py` has the `set_feed_card_callback` shim (line ~120) and
  the `_TOOLS["feed_card"]` registration (line ~2241).

## Rules that override the generic loop
- You do NOT run pytest. The project's PM ruling stands: Coder writes code and
  runs `py_compile` only. The Supervisor runs the tests.
- Use `prompts/steelFramedCodeWriter.md`.

---

## Edit 1 — add `set_feed_card_callback` to the import block

`ui/window.py:1059-1064`, inside `_wire_group_tool_callbacks`. Current block:

```python
        from agent.tools import (
            ToolResult,
            cancel_all_processes,
            set_agent_context_callback,
            set_canvas_callback,
            set_talk_callback,
        )
```

Add `set_feed_card_callback` keeping alphabetical order, i.e. between
`cancel_all_processes` and `set_agent_context_callback`:

```python
        from agent.tools import (
            ToolResult,
            cancel_all_processes,
            set_feed_card_callback,
            set_agent_context_callback,
            set_canvas_callback,
            set_talk_callback,
        )
```

## Edit 2 — install it

`ui/window.py:1184-1186`. Append one line after `set_talk_callback(_talk)`:

```python
        set_agent_context_callback(_agent_context)
        set_canvas_callback(_canvas)
        set_talk_callback(_talk)
        set_feed_card_callback(self._post_feed_card)
```

Also update the method's docstring first line (currently
`"""Install agent_context, canvas, and talk callbacks.`) to mention feed_card:
`"""Install agent_context, canvas, talk, and feed_card callbacks.`

## Edit 3 — the `_post_feed_card` method

A **named method on `MainWindow`**, placed immediately after
`_wire_group_tool_callbacks` ends (currently line 1186), i.e. directly before
`def _clear_chat_box`.

**Do NOT write this as a closure inside `_wire_group_tool_callbacks`.** A closure is
unreachable from tests, and the two metadata stamps it sets are exactly the
contracts §2.9 requires us to pin. A named method is testable with a stub `self`.

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
        from agent.tools import ToolResult
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

### Non-obvious constraints — do not "improve" these

1. **No `threading.Event`, no `GLib.idle_add`.** `FeedHandler.add_card` returns
   the id synchronously and defers its own GTK work internally. Do not copy
   `_canvas`'s hop-and-wait shape.
2. **`from datetime import UTC, datetime`**, not `timezone.utc`. This is the exact
   form `_emit_bridge_card` uses (`ui/window.py:1432`); keep the file consistent
   with itself.
3. **All four imports go INSIDE the method.** `ui/window.py` has no module-scope
   `datetime` or `FeedCardData` import. `get_special_agent` and `ToolResult` are
   imported method-locally inside `_wire_group_tool_callbacks` and are therefore
   NOT in scope here — re-import them. The quoted `"ToolResult"` return annotation
   is fine because `from __future__ import annotations` is in effect; still import
   `ToolResult` for the two `return ToolResult(...)` calls.
4. **`self._agent_runtime_handler._reply_key(...)` is a private reach.** That
   follows existing precedent — `ui/window.py` already calls `runtime_for`,
   `cancel_peer`, and `clear_conversation` on that object.
5. **Keep `metadata["session_key"]` AND `metadata["tab_key"]`.** Both are pinned by
   the moved divergence test.

## Edit 4 — new file `tests/test_feed_card_tool.py`

Create it with the **moved divergence test** plus a small set of direct-method
tests. Drive `_post_feed_card` **unbound against a stub**, e.g.
`MainWindow._post_feed_card(stub, "special:coder", fields)` — this avoids
constructing a real GTK window.

Required test:

```python
def test_divergence():
    """DESIGN PIN — moved from test_agent_runtime.py::TestCrabcardTabStamping
    ::test_multiproject_divergence_is_deliberate (SPEC-45 §2.9).

    project_name and tab_key come from DIFFERENT sources BY DESIGN. project_name
    stamps the ACTIVE project (the card feeds where the PM is looking); tab_key
    resolves the emitting agent's own routed tab (where the turn rendered). With
    the agent routed to project A while project B is active, the divergence
    project_name="B" / tab_key="project:A" is deliberate — do NOT unify the
    sources.
    """
```

Stub shape: an object with `_project_handler.get_active_project_name()` returning
`"B"`, `_agent_runtime_handler._reply_key(...)` returning `"project:A"`, and
`_feed_handler.add_card(card)` capturing the card and returning `"card-id-1"`.
Assert `card.project_name == "B"`, `card.metadata["tab_key"] == "project:A"`,
`card.metadata["session_key"] == "special:coder"`, and that the returned
`ToolResult.success` is True.

Add these additional cases (all via the same stub):
- happy path: `card_type`/`title`/`body`/`file_path`/`additions`/`deletions`/`task_id`
  map onto the `FeedCardData` fields, `source == "agent"`, and
  `output == "posted diff card card-id-1"`.
- empty optional fields: `file_path` and `task_id` become `None`, not `""`.
- no open project: `get_active_project_name()` returns `None` → `success is False`
  and `add_card` is never called.
- author fallback: with no registered special agent, `author == "coder"`
  (the `session_key.split(":", 1)[-1]` half).

Note `FeedCardData.__post_init__` caps the body at 200_000 and that `card_type` is
a `Literal` but **not** runtime-enforced, so no test need cover it here.

## Verification (you run these — NOT pytest into the suite)

```bash
cd /home/mushy/projects/develcakes
.venv/bin/python -m py_compile ui/window.py tests/test_feed_card_tool.py && echo COMPILE_OK
.venv/bin/python -m ruff check ui/window.py --output-format=concise | tail -5
```

Then a direct, no-pytest probe of the method itself:

```bash
.venv/bin/python -c "
from ui.window import MainWindow
class PH:
    def get_active_project_name(self): return 'B'
class ARH:
    def _reply_key(self, sk): return 'project:A'
class FH:
    def __init__(self): self.cards=[]
    def add_card(self, c): self.cards.append(c); return 'card-id-1'
class Stub:
    _project_handler=PH(); _agent_runtime_handler=ARH()
    def __init__(self): self._feed_handler=FH()
s=Stub()
fields={'card_type':'diff','title':'t','body':'b','file_path':'x.py',
        'additions':1,'deletions':2,'task_id':'WU-9'}
r=MainWindow._post_feed_card(s,'special:coder',fields)
print('success:', r.success, '| output:', r.output)
c=s._feed_handler.cards[0]
print('project_name:', c.project_name, '| tab_key:', c.metadata['tab_key'])
print('author:', c.author, '| file_path:', c.file_path, '| task_id:', c.task_id)
"
```

**Expected:** `success: True | output: posted diff card card-id-1`,
`project_name: B | tab_key: project:A`, and `author: coder` (no special agent is
registered in a bare import — this is the fallback branch, and it is correct).
If `ui.window` cannot import without a display, say so in your report and rely on
`py_compile` plus the test file — do not install anything.

## Report back
- Files changed with line numbers
- The verification outputs pasted verbatim
- Any spec drift (line numbers moved) flagged
- Any related issue you noticed but did NOT fix

```
COMPLETENESS:
- [ ] Edit 1: `set_feed_card_callback` added to the import block — evidence
- [ ] Edit 2: `set_feed_card_callback(self._post_feed_card)` installed — evidence
- [ ] Edit 3: `_post_feed_card` named method added after `_wire_group_tool_callbacks` — evidence
- [ ] Edit 4: `tests/test_feed_card_tool.py` created with the moved divergence pin — evidence
- [ ] py_compile OK for both files — output pasted
- [ ] method probe output pasted
```
