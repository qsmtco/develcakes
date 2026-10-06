# SPEC-12 SP1 — Grouped agent boxes in the chat surface

**Spec:** `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` (§2a)
**Pre-flight:** `docs/specs/phases/SPEC-12-PREFLIGHT-DECISIONS.md`
**Plan:** `docs/specs/phases/SPEC-12-SUBPHASES.md`
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** TWO files — `ui/views/chat_surface.py` (render model) and
`tests/test_chat_surface.py` (tests). No handler/UI-wiring changes this phase.

---

## 1. The change — `_document` groups consecutive same-agent rows

Anchor: `def _document(rows: list[dict]) -> str:` in `ui/views/chat_surface.py`
(currently ~line 82). Read the file in full before editing (steelFramed
read-before-touch).

Current behavior: each row emits
`<div class="message-row role-X"><span class="agent-name">NAME</span><div
class="msg-body">HTML</div></div>` — the name header repeats on EVERY row.

New behavior (spec §2a verbatim): group the run of consecutive rows with the
SAME `row["agent"]` under ONE `.agent-box` with a single `.agent-name` header.
Rows with no agent name (system/welcome) render bare. Implement exactly the spec
§2a body:

```python
def _document(rows: list[dict]) -> str:
    """Render rows as grouped agent boxes — consecutive rows with the same
    agent collapse under ONE header (SPEC-12). Rows with no agent name render
    bare (system/welcome)."""
    blocks = []
    i = 0
    n = len(rows)
    while i < n:
        row = rows[i]
        agent = row.get("agent") or ""
        if not agent:
            blocks.append(
                f'<div class="message-row role-{row["role"]}">'
                f'<div class="msg-body">{row["html"]}</div></div>'
            )
            i += 1
            continue
        j = i
        while j < n and (rows[j].get("agent") or "") == agent:
            j += 1
        body = "".join(
            f'<div class="message-row role-{rows[k]["role"]}">'
            f'<div class="msg-body">{rows[k]["html"]}</div></div>'
            for k in range(i, j)
        )
        name = html.escape(agent)
        box_class = "role-user" if agent == "You" else "role-agent"
        blocks.append(
            f'<div class="agent-box {box_class}">'
            f'<span class="agent-name">{name}</span>{body}</div>'
        )
        i = j
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{_BASE_CSS}</style></head><body>"
        + "".join(blocks) + "</body></html>"
    )
```

## 2. CSS — add to `_BASE_CSS` (spec §2a)

The existing `.role-user .agent-name { color: #9ece6a; }` is a DESCENDANT
selector; under the new structure `.agent-name` is a SIBLING of the `.role-user`
rows, so it stops matching. Add the box-level rule (keep the old rule too — it
still matches any ungrouped bare user row):

```css
.agent-box { border-left: 2px solid #3b4261; padding-left: 8px; margin-bottom: 12px; }
.agent-box.role-user .agent-name { color: #9ece6a; }
```

## 3. `TextViewFallback`

No change beyond parity — it already prefixes `[name] line`. Do NOT alter it.

## 4. What must NOT change
- `_cap_row_html`, the windowing deque, streaming APIs, destroy semantics.
- The row dict shape written by `append_message` (`role`/`html`/`agent` keys).
- Existing tests in `tests/test_chat_surface.py` must pass unmodified (some pin
  `"message-row"` / `Coder` in the doc — confirm they still hold; the grouping
  keeps `message-row` on inner rows and the name in the box).

## 5. Tests (append to `tests/test_chat_surface.py`, RED-first)

Write these against the CURRENT code first and paste the failures, then land the
edit.

1. `test_document_groups_consecutive_same_agent` — three `agent="Coder"` rows,
   then one `agent="Debugger"` row: assert exactly TWO `class="agent-box` in the
   doc, `agent-name">Coder<` appears ONCE, `agent-name">Debugger<` once, and all
   four `msg-body` fragments present.
2. `test_document_repeated_agent_name_single_header` — two Coder rows: exactly
   one `Coder` header, one `.agent-box`.
3. `test_document_user_rows_group_and_class` — two `agent="You"` rows: one
   `.agent-box`, the box carries `role-user`, and `_BASE_CSS` contains
   `.agent-box.role-user .agent-name`.
4. `test_document_no_agent_rows_render_bare` — a row with `agent=""`: emitted as
   a bare `.message-row` (no `.agent-box` wrapping it).
5. `test_document_interleaved_agents_two_boxes` — Coder, Debugger, Coder (three
   runs) → three `.agent-box` blocks, name order Coder/Debugger/Coder.
6. `test_document_agent_name_escaped_still` — existing escape pin must still
   hold under the new box markup (`<b>evil</b>` header escaped).

## 6. Verification battery (paste all outputs)
- `python -m pytest tests/test_chat_surface.py -q` → all green (existing + 6 new)
- `ruff check ui/views/chat_surface.py tests/test_chat_surface.py` → 0
- `pyright ui/views/chat_surface.py` → 0 errors (measure baseline first on HEAD)
- `wc -l ui/views/chat_surface.py` → baseline + ~20

## 7. Report back (mandatory COMPLETENESS)
```
COMPLETENESS:
- [x/not done] Edit 1: _document grouped boxes — evidence: diff hunk
- [x/not done] Edit 2: CSS rules added — evidence: diff hunk
- [x/not done] Edit 3: 6 tests appended — evidence: pytest output count
- [x/not done] RED proofs: each new test's pre-edit failure pasted
- [x/not done] ruff / pyright / wc -l outputs pasted
- [x/not done] Related issues found, NOT fixed (flagged for supervisor)
```

Invoke `prompts/steelFramedCodeWriter.md` before writing anything. Please write
the change per this brief and report when done.