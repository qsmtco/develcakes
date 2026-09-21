# Phase 1: Segments + Parser + Unit Tests (no UI change)

**Spec:** `docs/specs/SPEC-TEXTVIEW-TEXTTAG-RENDERING.md` §5 Phase 1
**Loop:** SPEC-TEXTVIEW-TEXTTAG-RENDERING
**Phase:** 1 of 4
**Status:** READY — Phase 0 probes PASSED (see PROBE-REPORT below)

## Objective

Create the new `chat/` package with the data model (`segments.py`) and the
parser (`parser.py`). NO UI changes. The feature flag
`CRABCAKES_TEXTVIEW_BUBBLES` defaults OFF. The old pipeline
(`utils/markdown.py`, `utils/escaping.py:escape_for_pango`, `utils/block_parser.py`)
stays untouched and fully functional.

## Files to create (3 new)

1. `chat/__init__.py` — package marker, exports public symbols
2. `chat/segments.py` — frozen dataclasses for the Segment model
3. `chat/parser.py` — `parse_message(text) -> list[Segment]`

## Files to create (2 new tests)

4. `tests/test_chat_segments.py` — data model tests (~15 tests)
5. `tests/test_chat_parser.py` — parser tests (~25 tests)

## Files to modify (1)

6. `pyproject.toml` — add `mistune>=3.0,<4.0` to dependencies (if not already added)

## PROBE-REPORT (Phase 0 results — verified, do not re-probe)

These were verified empirically by the Supervisor against the live environment.

### Phase 0a — mistune API (PASSED)

```
mistune version: 3.3.4
```

**CRITICAL:** `mistune.AstRenderer` does NOT exist. Use:
```python
md = mistune.create_markdown(renderer='ast', plugins=['table', 'task_lists', 'strikethrough'])
```
This returns a `list[dict]` of token dicts.

**Token type → Segment mapping (VERIFIED):**

| mistune token type | dict keys | Segment produced |
|--------------------|-----------|------------------|
| `heading` | `attrs.level`, `children` | `Heading(level, text, inline)` |
| `paragraph` | `children` | `TextSeg(text, inline)` |
| `block_quote` | `children` (recursively parsed) | `BlockQuote(blocks)` |
| `block_code` | `attrs.info` (=lang), `raw` | `CodeBlock(lang, content)` OR `Image(src)` if info=="image" |
| `list` (with `task_list_item` children) | `children[].attrs.checked`, `children[].children` | `TaskItem(checked, text, inline)` |
| `list` (with `list_item` children) | `children[].children` | `BulletItem(text, inline)` |
| `table` | `children` (table_head → table_cell, table_body → table_row → table_cell) | `Table(headers, rows)` |
| `strong` | `children` | `InlineNode(kind="bold", text)` |
| `emphasis` | `children` | `InlineNode(kind="italic", text)` |
| `codespan` | `raw` | `InlineNode(kind="code", text)` |
| `strikethrough` | `children` | `InlineNode(kind="strike", text)` |
| `link` | `attrs.url`, `children` | `InlineNode(kind="link", text, href=url)` |
| `text` | `raw` | (raw text — concatenated into parent) |
| `softbreak` | (none) | → literal `\n` inserted into text |
| `blank_line` | (none) | SKIP — produces no segment |

**Inline node extraction:** For `strong`/`emphasis`/`strikethrough`/`link`, the
children are a list of `text` tokens. Concatenate the `raw` of all child `text`
tokens into the InlineNode's `text`. For `codespan`, the text IS the `raw` field
directly (no children).

**Code block raw text:** `block_code` token has `raw` field containing the code
content INCLUDING a trailing `\n` (e.g. `"x=1\n"`). Strip trailing `\n` in the
parser so CodeBlock.content does not have a spurious trailing newline.

**Image detection (BUG #28):** When `block_code.attrs.info == "image"`, the
`raw` field is the file path. Emit `Image(src=raw.strip())`. Do NOT emit
`CodeBlock`.

**Table extraction:** The table token structure is:
```
table
  └── table_head
        └── table_cell (attrs.head=True, children=[text]) × N
  └── table_body
        └── table_row
              └── table_cell (children=[text]) × N
```
Headers = text of each cell in table_head. Rows = list of tuples of text of
each cell in each table_row. Cell text = concatenate `raw` of all child tokens.

### Phase 0b — GTK4 TextTag API (PASSED, headless)

All confirmed working WITHOUT a display:
- `Gtk.TextTag(name=...)` constructs headless ✓
- `tag.set_property("weight", Pango.Weight.BOLD)` → 700 ✓
- `tag.set_property("strikethrough", True)` ✓
- `tag.set_property("background", "rgba(127,127,127,0.15)")` ✓
- `Gtk.TextTagTable()` + `.add(tag)` ✓
- `Gtk.TextBuffer()` constructs headless ✓
- `Gtk.TextBuffer.new(table)` ✓
- `tag.href = "uri"` (Python attribute) + `getattr(tag, "href", None)` ✓
- `Gtk.GestureClick()` constructs headless ✓
- `buf.insert_with_tags(iter, text, tag)` ✓
- `iter.has_tag(tag)` ✓
- `buf.get_iter_at_offset(n)` ✓

**CRITICAL (Pango.Scale):** `Pango.Scale` does NOT exist. `Pango.SCALE` is the
integer constant 1024. For heading scale properties, use FLOAT values directly:
- h1: `scale=1.8` (approx XX_LARGE)
- h2: `scale=1.44` (approx X_LARGE)
- h3: `scale=1.2` (approx LARGE)
- h4: no scale (bold only)

(This affects Phase 2, not Phase 1 — documented here for forward reference.)

## CONTRACT for `parse_message(text: str, max_len: int = 100 * 1024) -> list[Segment]`

1. **Truncate** input at `max_len` (100KB) with marker
   `"[... input truncated at 100 KB ...]"` appended if truncated. (Preserve the
   existing ReDoS cap from `utils/markdown.py`.)
2. **Parse** using `mistune.create_markdown(renderer='ast', plugins=[...])`.
3. **Walk** the token list. For each token, produce zero or more Segments per
   the mapping table above. Skip `blank_line` tokens.
4. **Failure mode (BUG #14 fix — MANDATORY):** On ANY exception during parse,
   log a warning to stderr and return `[TextSeg(text=original_raw_input)]` —
   the RAW untruncated input as a single unformatted segment. NEVER return an
   empty list. NEVER let the exception propagate.
5. **Coalescing:** Consecutive `paragraph` tokens should NOT be merged — each
   becomes its own `TextSeg`. (Mistune already separates paragraphs with
   `blank_line` tokens which we skip.)

## CONTRACT for `chat/segments.py`

Follow the spec's dataclass definitions EXACTLY (§2 `chat/segments.py`). Key
points:
- All dataclasses are `@dataclass(frozen=True)`
- `InlineNode.kind` is a `Literal["bold", "italic", "strike", "code", "link"]`
- Default `= ()` on all `inline`/`blocks` tuple fields
- `Segment = Union[...]` type alias at module level
- `Image` is included (src + alt fields)
- NO GTK imports in this file

## CONTRACT for `chat/__init__.py`

```python
from chat.segments import (
    TextSeg, InlineNode, BlockQuote, CodeBlock, TerminalBlock,
    Heading, TaskItem, BulletItem, Table, Image, Segment,
)
from chat.parser import parse_message

__all__ = [
    "parse_message", "Segment", "TextSeg", "InlineNode", "BlockQuote",
    "CodeBlock", "TerminalBlock", "Heading", "TaskItem", "BulletItem",
    "Table", "Image",
]
```

## Feature flag

Define `CRABCAKES_TEXTVIEW_BUBBLES` as an environment-variable-checked flag
(default OFF / falsy). In Phase 1 this is NOT yet consumed by any UI code —
just define it in `chat/__init__.py` as a module-level constant:

```python
import os
TEXTVIEW_BUBBLES_ENABLED = os.environ.get("CRABCAKES_TEXTVIEW_BUBBLES", "0") == "1"
```

## Test requirements

### `tests/test_chat_segments.py` (~15 tests)

- Each dataclass constructs with required fields
- Each dataclass is frozen (mutation raises `FrozenInstanceError`)
- `inline`/`blocks` default to `()` when not provided
- `InlineNode.kind` accepts all 5 Literal values
- `Segment` Union accepts all 9 member types

### `tests/test_chat_parser.py` (~25 tests) — ALL must be able to FAIL

Cover these inputs (use parametrize where natural):
- Empty string → `[]`
- Plain text (no markdown) → `[TextSeg]`
- Bold `**bold**` → TextSeg with InlineNode(kind="bold")
- Italic `*italic*` → InlineNode(kind="italic")
- Strikethrough `~~strike~~` → InlineNode(kind="strike")
- Code span `` `code` `` → InlineNode(kind="code")
- Link `[click](http://example.com)` → InlineNode(kind="link", href=...)
- Heading `# H1`, `## H2`, `### H3`, `#### H4` → Heading(level=...)
- Code block ```` ```python\ncode\n``` ```` → CodeBlock(lang="python", content="code")
  (assert content has NO trailing newline — the raw had `\n`, parser strips it)
- Code block with `lang="image"` → Image(src=...) NOT CodeBlock (BUG #28)
- Block quote `> quoted` → BlockQuote(blocks=(TextSeg,))
- Task list `- [ ] undone\n- [x] done` → TaskItem(checked=False), TaskItem(checked=True)
- Bullet list `- item1\n- item2` → BulletItem, BulletItem
- Table (from §6 TEST_CASES) → Table(headers, rows)
- Mixed inline `**bold** and `code` and *italic*` → all 3 InlineNodes in one TextSeg
- Nested `***bold+italic***` → two overlapping InlineNodes (bold + italic)
- Plain text `<script>alert(1)</script>` → single TextSeg, no HTML processing
- 100KB+ input → truncated with marker
- **Malformed/unparseable input (BUG #14):** mock mistune to raise → assert
  `[TextSeg(text=original_input)]` returned (never empty, never raises)
- Softbreak in paragraph → `\n` appears in text

## Verification commands (run after implementation)

```bash
# New tests pass
python3 -m pytest tests/test_chat_parser.py tests/test_chat_segments.py -v

# Old tests UNCHANGED — all 143 still pass
python3 -m pytest tests/test_markdown.py tests/test_escaping.py tests/test_block_parser.py -q

# Import smoke test
python3 -c "from chat import parse_message, TextSeg, CodeBlock; print(parse_message('**hi**'))"

# No escape_for_pango / format_markdown / extract_blocks in chat/
grep -rn "escape_for_pango\|format_markdown\|extract_blocks" chat/ && echo "FAIL: old pipeline in new package" || echo "OK: clean"
```

## COMPLETENESS checklist (mandatory — use steelFramedCodeWriter Step 6.5 format)

After implementation, list EVERY item with evidence:
- chat/__init__.py created (wc -l, exports)
- chat/segments.py created (wc -l, all 10 dataclasses + Segment Union)
- chat/parser.py created (wc -l, parse_message signature)
- tests/test_chat_segments.py created (pytest count)
- tests/test_chat_parser.py created (pytest count)
- pyproject.toml mistune added (grep output)
- Old tests still pass (pytest output)
- No old-pipeline symbols in chat/ (grep output)
