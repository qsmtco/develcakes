# SPEC-45 Coder Instructions — repair + finish acceptance tests

## Situation

The Supervisor violated the loop and built phases 6–10 itself. You are being
handed the remainder. Two jobs only. Do not touch anything else.

**Master spec:** `docs/specs/SPEC-45-FEED-CARD-TOOL.md` — §6.1 (the tool) and
§6.2 (the rejection matrix) are your contract.

## Rules that override the generic loop

- You do NOT run pytest. PM ruling: Coder writes code, `py_compile` only.
  The Supervisor runs the tests.
- Use `prompts/steelFramedCodeWriter.md`.
- File writes via `edit_file`/`write_file`, never exec heredocs.

## Job 1 — repair `tests/test_feed_card_tool.py`

The Supervisor ran the same append edit twice; enforcement flagged a possible
duplicate. Inspect the file top to bottom:

- If any test function, fixture (`captured`), or the §6.1/§6.2 block appears
  twice, remove the duplicate — keep ONE copy of each, the later shape.
- The file currently imports `pytest`, `group_tools`, `ToolResult`,
  `execute_tool`, `get_tool_definitions_for_api`. If Job 2 keeps them all
  used, keep them; otherwise drop the unused ones. Final state must be
  ruff-clean.
- Do NOT delete the six `_post_feed_card` stub tests at the top
  (`test_divergence`, `test_happy_path_maps_fields`,
  `test_empty_optional_fields_become_none`,
  `test_no_open_project_rejects_without_calling_add_card`,
  `test_author_uses_display_name_when_agent_is_registered`,
  `test_author_fallback_uses_session_suffix`) — those are Coder's Phase 3
  work, already verified.

## Job 2 — write the §6.1/§6.2 acceptance tests in that same file

If the §6.1/§6.2 block survived Job 1 intact, REVIEW it line by line instead
of rewriting — it was authored outside the loop; you own it now, so make it
yours (fix anything you'd have written differently). If it's missing or
mangled, write it. Either way the finished file must pin, per §6.1:

1. `feed_card` appears in `get_tool_definitions_for_api()` names.
2. Public path: `execute_tool("feed_card", {...}, proj)` reaches the handler
   with **all eight parameters** forwarded — one test passing all eight and
   inspecting the callback's `fields` dict (a missed positional forward lands
   in `**kwargs` silently; this is the guard).
3. Successful call returns `success=True` with the card id in the output.
4. `additions`/`deletions` reach `FeedCardData` as int; None when omitted.
5. `metadata["session_key"]` is the caller's session key and
   `metadata["tab_key"]` is `_reply_key(session_key)` — these two live in the
   `_post_feed_card` stub tests already; do not duplicate, just don't regress.

And per §6.2, every row returns `success=False`, never raises:

| Input | Error contains |
|---|---|
| `type="system"` | `must be one of diff, agent_action, audit_report` |
| `type` omitted | `received invalid arguments` |
| `title=""` / whitespace | `title is empty` |
| `title` 201 chars | `title exceeds 200` |
| `body` 100_001 chars | `body exceeds 100000` |
| `file="/etc/passwd"`, `"../../x.py"`, `"C:/x.py"` | `project-relative path` |
| `additions="5"`, `additions=True` | `additions must be an integer` |
| `additions=-1` | `must not be negative` |
| callback not installed | `feed_card is not wired` |
| no project open | `feed_card needs an open project` |

Implementation notes (do not "improve" these):
- Callback capture: monkeypatch `agent.group_tools._feed_card_cb` directly —
  that's the module global the handler reads. The setter is for window wiring.
- The `no project open` row is a `_post_feed_card` stub test (already exists
  as `test_no_open_project_rejects_without_calling_add_card`) — verify it
  matches the §6.2 error string, don't duplicate.
- `execute_tool` signature: `(name, arguments, project_path, session_key=...)`.
  It injects `project_path`/`session_key`/`approval_callback`/`scratch_dir`
  into the args — the handler's `**kwargs` swallows them; your capturing
  callback should tolerate extra keys.

## Verification (you run these — NOT pytest)

```bash
cd /home/mushy/projects/develcakes
.venv/bin/python -m py_compile tests/test_feed_card_tool.py && echo COMPILE_OK
.venv/bin/python -m ruff check tests/test_feed_card_tool.py --output-format=concise
```

## Report back

- What the duplicate-damage actually was (or "none found")
- The final test list (names, one line each)
- py_compile + ruff output pasted
- COMPLETENESS checklist per the loop template, with the marker phrase
  "please proceed" somewhere in your reply so the acknowledgment is unambiguous
