# SPEC-45 PHASE 1 of 9 — `feed_card` handler + setter

**Master spec:** `docs/specs/SPEC-45-FEED-CARD-TOOL.md` — read §2.1 before writing.

**Scope:** ONE file. `agent/group_tools.py` only. Do not touch any other file.

**Rules that override the generic loop:**
- You do NOT run pytest. This project's PM ruling is Coder writes code and runs
  `py_compile` only. The Supervisor runs the tests. Do not run pytest, do not
  run the test suite, do not "just check one test."
- Use `prompts/steelFramedCodeWriter.md` — read it before writing.

---

## Edit 1 — add the callback global + setter

Insert immediately after the existing `_talk_cb = None` line (currently line 16),
before `def set_agent_context_callback`. The three existing globals are
`_agent_context_cb`, `_canvas_cb`, `_talk_cb` — add a fourth in the same style.

```python
_feed_card_cb = None
```

Then insert this setter immediately after `set_talk_callback` ends (currently
line 43, before `def _caller_role`), matching the docstring style of its three
neighbours:

```python
def set_feed_card_callback(cb) -> None:
    """Install the project-feed card callback.

    Invoked from the feed_card handler after every field is validated.
    Signature: (session_key, fields) -> ToolResult, where fields is a dict
    holding card_type, title, body, file_path, additions, deletions, task_id.
    None means the tool returns "feed_card is not wired".
    """
    global _feed_card_cb
    _feed_card_cb = cb
```

## Edit 2 — add the handler

Append at the END of the file, after `talk()` (currently the last function,
ending at line 314). Nothing follows it.

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
            error=f"feed_card type must be one of {', '.join(FEED_CARD_TYPES)}",
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
        "task_id": task_id.strip(),
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

### Non-obvious constraints — do not "improve" these

1. **`type` shadows the builtin. Keep the name.** It is the schema name the
   model sees. This project's ruff config does not enable flake8-builtins
   (verified), so it adds no lint finding.
2. **`exc.__class__.__name__`, NOT `type(exc).__name__`.** Inside `feed_card`
   `type` is the `str` parameter, so `type(exc)` raises
   `TypeError: 'str' object is not callable` — inside the `except` handler, which
   converts a clean tool error into a crash. This was verified against the real
   interpreter. Do not "tidy" it.

   **Do not "fix" the neighbours.** `canvas()` (line ~293) and `talk()`
   (line ~310) both use `type(exc).__name__` and both are CORRECT — neither has
   a `type` parameter. Leave them alone. This is a local hazard of the `feed_card`
   signature only, not a file-wide pattern to unify. A file-wide
   `type(` → `.__class__` sweep would be scope creep.

3. **The `counts` dict collects both counts before `fields` is built.** Do not
   collapse it into two locals assigned inside the loop — the loop validates both
   values first, so the `fields` dict can never carry a half-validated pair.
4. **`task_id` is type-checked unconditionally (`if not isinstance(...)`), not
   `if task_id and not isinstance(...)`.** The guarded form lets a non-string
   falsy value (`0`, `False`, `[]`) through to `task_id.strip()` and crash. Do
   not "optimize" it back.
5. **`_logger` already exists** at module scope (line 12). Do not re-import it.

## Verification (you run these, not pytest)

```bash
cd /home/mushy/projects/develcakes
.venv/bin/python -m py_compile agent/group_tools.py && echo COMPILE_OK
.venv/bin/python -m ruff check agent/group_tools.py --output-format=concise
.venv/bin/python -c "
from agent.group_tools import feed_card, set_feed_card_callback
r = feed_card('diff', 't')
print(r.success, repr(r.error))
r2 = feed_card('system', 't')
print(r2.success, repr(r2.error))
"
```

**Expected:**
- `COMPILE_OK`
- ruff: `All checks passed!` — this file is currently clean (verified); it must
  stay clean. If ruff reports ANY finding, fix it before reporting.
- `False 'feed_card is not wired'`
- `False 'feed_card type must be one of diff, agent_action, audit_report'`

## Report back

- Files changed with line numbers
- The three command outputs pasted verbatim
- Any spec drift (line numbers moved) flagged
- Any related issue you noticed but did NOT fix

```
COMPLETENESS:
- [ ] Edit 1: `_feed_card_cb` global added after `_talk_cb` — evidence
- [ ] Edit 2: `set_feed_card_callback` added after `set_talk_callback` — evidence
- [ ] Edit 3: `FEED_CARD_TYPES`/`_TITLE_MAX`/`_BODY_MAX` constants added — evidence
- [ ] Edit 4: `feed_card()` handler appended at end of file — evidence
- [ ] py_compile OK — output pasted
- [ ] ruff clean — output pasted
- [ ] behaviour probe output pasted (both calls)
```
