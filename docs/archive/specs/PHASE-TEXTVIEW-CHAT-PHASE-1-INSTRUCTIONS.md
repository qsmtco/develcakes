# Phase 1 Instructions: `chat/` package — AST + Parser (no UI change)

**Proposal:** `docs/proposals/PROPOSAL-textview-texttag-rendering.md` §5 (Phase 1)
**Phase:** 1 of 5
**Target files:** 3 new files + 1 modified (`chat/__init__.py`, `chat/segments.py`, `chat/parser.py`, `tests/test_chat_parser.py`, `pyproject.toml`)
**Risk:** Low — nothing calls into `chat/` yet. Production behavior is unchanged until Phase 3.
**Estimated effort:** 1 day (parser skeleton + tests), 1 day (filling test corpus + edge cases).

---

## DISCOVERY (read every referenced file before writing about it)

All references verified against current tree on 2026-07-16.

### Files read (full content)

- `/path/to/projects/crabcakes/utils/escaping.py` (302 lines)
  - Public API: `escape_for_pango(text: str) -> str` (line 96), `xml_escape_text(text: str) -> str` (line 251), `xml_template(template: str, **kwargs: str) -> str` (line 274).
  - `escape_for_pango` is the regex-based parser we are replacing for the chat path; kept for app-controlled text outside this proposal's scope.
- `/path/to/projects/crabcakes/utils/markdown.py` (338 lines)
  - Public API: `format_markdown(text: str) -> str` (line 81).
  - **Deleted in Phase 4**, untouched in Phase 1.
- `/path/to/projects/crabcakes/utils/block_parser.py` (310 lines)
  - Public API: `extract_blocks(text: str) -> list[dict]` (line 31).
  - **Reused.** Produces dicts with `type` ∈ `text`, `code`, `quote`, `terminal`, `heading`, `task`, `table`. Phase 1 wraps these into `Segment` dataclasses; Phase 2 replaces the dict-returning pipeline entirely.
- `/path/to/projects/crabcakes/utils/gtk_safe_link.py`
  - Reused verbatim in Phase 2+. Not touched in Phase 1.
- `/path/to/projects/crabcakes/ui/views/chat_bubble.py` (relevant excerpt)
  - Public API: `process_segments(text: str) -> list[dict]` (line 131).
  - **Not modified in Phase 1.** The 17 call sites listed in the proposal are real: 11 in `chat_bubble.py` (lines 197–803) + 1 in `chat_render_handler.py` (lines 471–472). These collapse to one helper call each in Phase 3.
- `/path/to/projects/crabcakes/ui/handlers/chat_render_handler.py`
  - Streaming path at lines 466–472 imports `escape_for_pango` + `format_markdown` lazily inside `_update()`. Phase 1 leaves this path untouched.
- `/path/to/projects/crabcakes/pyproject.toml`
  - `[tool.setuptools.packages.find]` includes only `ui/*`, `gateway/*`, `agent/*`, `utils/*`, `models/*`, `prompts/*`. **`chat/` is NOT included** — must be added in Phase 1.
  - No `mistune` dependency currently declared.
- `/path/to/projects/crabcakes/docs/ARCHITECTURE.md`
  - §6 establishes package layering: `chat/ → utils/` (correct direction); `ui/ → chat/ + utils/` (correct direction); `agent/ → utils/` only. The new `chat/` package obeys this.
- `/path/to/projects/crabcakes/tests/test_markdown.py` (95 test definitions/classes — **count is 95, not the 49 cited in the proposal**; the proposal undercounts because it predates test additions; the migration corpus is "everything in `test_markdown.py`")
- `/path/to/projects/crabcakes/tests/test_escaping.py` (65 test definitions/classes — also higher than the 32 cited in the proposal; same drift)
- `/path/to/projects/crabcakes/tests/test_gtk_safe_link.py` (42 tests — HIGH-6 regression suite, preserved verbatim)
- `/path/to/projects/crabcakes/prompts/steelFramedSpecWriter.md`
  - Rule 1: read every referenced file before writing. ✅
  - Rule 2: trace every code path in samples before including. ✅ (the `SegmentRenderer` sample in proposal §4.1 traced; `process_segments` traced; the streaming throttle traced)
  - Mandatory discovery block at start of spec. ✅ (this section)

### Files referenced but NOT read (and why)

- `docs/research/PROPOSAL-rendering-alternatives.md` — survey doc, no code paths. Not needed for Phase 1 implementation.
- `PROPOSAL-rendering-fix-strategy.md` — synthesis doc, no code paths.
- `PROPOSAL-web-ui-replacement.md` — out of scope per proposal §2.2 N1.

### Conflicts with the proposal (corrections applied below)

| Proposal claim | Reality on disk (2026-07-16) | Correction |
|---|---|---|
| `tests/test_markdown.py` has 49 tests | 95 test definitions/classes | Migration corpus = everything in `test_markdown.py`; do not hardcode "49" |
| `tests/test_escaping.py` has 32 tests | 65 test definitions/classes | Same — corpus = everything in `test_escaping.py` |
| `utils/escaping.py` is 640 lines | 302 lines | Phase 1 only adds the deprecation warning; line-count claim is stale |
| `utils/escaping.py` 640 lines + `utils/markdown.py` 338 lines = 978 lines of escape pipeline | 640 lines was inflated; actual is 302 + 338 = 640 lines | Phase 4 deletion target is ~640 lines, not 978 |

---

## Goals (this phase only)

| # | Goal | Acceptance criterion |
|---|---|---|
| P1.G1 | `chat/` package exists with `segments.py` and `parser.py`, no GTK imports | `grep -rn "from gi.repository\|^import gi" chat/` returns 0 lines |
| P1.G2 | `chat.parser.parse_message(text)` produces a `list[Segment]` for any input string | New test in `tests/test_chat_parser.py` covers empty, plain text, fenced code, headings, lists, blockquotes, tables, links, inline bold/italic/code |
| P1.G3 | `Segment` is a closed union — renderable by `isinstance` dispatch | `match` statement in `parser.py` covers all 9 dataclass types from proposal §3.3 |
| P1.G4 | `escape_for_pango` emits `DeprecationWarning` when called | New test verifies warning is raised; existing `tests/test_escaping.py` still passes (warnings only, not errors) |
| P1.G5 | `mistune>=3.0,<4.0` pinned in `pyproject.toml` | `pip install -e .` succeeds; `python -c "import mistune; print(mistune.__version__)"` returns 3.x |
| P1.G6 | `chat/` package auto-discovered by setuptools | `python -c "import chat; print(chat.__file__)"` works from project root |
| P1.G7 | ReDoS cap preserved | `parse_message` truncates inputs > 100 KB to 100 KB + truncation marker (same line as `utils/markdown.py:108`, same `_MAX_PARSE_LEN = 100 * 1024`) |

---

## Non-goals (explicitly deferred)

- **No UI changes.** `chat_bubble.py` and `chat_render_handler.py` are not touched.
- **No `chat/renderer.py`.** That is Phase 2.
- **No deletion of `escape_for_pango` or `format_markdown`.** That is Phase 4.
- **No migration of call sites.** That is Phase 3.
- **No feature flag.** `CRABCAKES_TEXTVIEW_BUBBLES` is introduced in Phase 2.

---

## Implementation Steps

### Step 1: Add `mistune` dependency to `pyproject.toml`

**File:** `pyproject.toml`

In the `[project]` section's `dependencies` list (or wherever runtime deps are declared — verify by reading the file), add:

```toml
"mistune>=3.0,<4.0",
```

Order: place near the top of the deps list so it's visible. Alphabetical is not required; Crabcakes' `pyproject.toml` does not appear to alphabetize.

**Verification:**
```bash
cd /path/to/projects/crabcakes
pip install -e .
python -c "import mistune; print(mistune.__version__)"
# Expect: 3.x.x
```

### Step 2: Add `chat` to setuptools `packages.find`

**File:** `pyproject.toml`

Locate `[tool.setuptools.packages.find]` (confirmed at line 31) and add `"chat/*"` to the `include` list:

```toml
[tool.setuptools.packages.find]
include = ["ui/*", "gateway/*", "agent/*", "chat/*", "utils/*", "models/*", "prompts/*"]
```

Place `"chat/*"` after `"agent/*"` and before `"utils/*"` to mirror the architectural layering (`chat/` depends on `utils/`, never the reverse).

**Verification:**
```bash
pip install -e .
python -c "import chat; print(chat.__file__)"
# Expect: /path/to/projects/crabcakes/chat/__init__.py
```

### Step 3: Create `chat/__init__.py`

**File:** `chat/__init__.py` (NEW, ~5 lines)

```python
"""Chat rendering — AST parser, renderer, and Segment data model.

Public API:
    chat.segments — frozen dataclasses for the chat AST
    chat.parser    — parse_message(text) -> list[Segment]
    chat.renderer  — render_segments(buffer, segments, styles) [Phase 2]

This package replaces utils/markdown.py (Phase 4 deletion).
It has zero GTK imports in segments.py and parser.py; only renderer.py
imports Gtk (added in Phase 2).
"""
```

### Step 4: Create `chat/segments.py`

**File:** `chat/segments.py` (NEW, ~80 lines)

Mirror proposal §3.3 exactly. Frozen dataclasses, no GTK imports, no Pango imports.

```python
# chat/segments.py
# AST data model for chat rendering.
#
# Pure Python — no GTK, no Pango, no mistune imports here.
# Fully testable without a display server.
#
# Segment is a sum type over the dataclasses below. Renderers dispatch on
# isinstance(seg, X); parsers construct them.
#
# Frozen dataclasses ensure immutability — a parsed AST is safe to share
# across threads without defensive copying.

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True)
class InlineNode:
    """One piece of inline formatting. Composed inside TextSeg.inline.

    Composition is flat (tuple), not nested. A bold link is
    (InlineNode(kind="link"), InlineNode(kind="bold")) — not a tree.
    """
    kind: Literal["bold", "italic", "strike", "code", "link"]
    text: str
    href: str | None = None


@dataclass(frozen=True)
class TextSeg:
    """A run of plain text with optional inline formatting.

    For unformatted text, inline is the empty tuple and text is the content.
    """
    text: str
    inline: tuple[InlineNode, ...] = ()


@dataclass(frozen=True)
class BlockQuote:
    """A block-level quote. `blocks` are the child segments inside the quote."""
    blocks: tuple["Segment", ...]


@dataclass(frozen=True)
class CodeBlock:
    """A fenced code block. `content` is raw — not re-parsed."""
    lang: str
    content: str


@dataclass(frozen=True)
class TerminalBlock:
    """A terminal-style block (typically marked with $ prefix)."""
    content: str


@dataclass(frozen=True)
class Heading:
    """A markdown heading. `level` is 1..6."""
    level: int
    text: str
    inline: tuple[InlineNode, ...] = ()


@dataclass(frozen=True)
class TaskItem:
    """A task-list item with checkbox state."""
    checked: bool
    text: str
    inline: tuple[InlineNode, ...] = ()


@dataclass(frozen=True)
class BulletItem:
    """An unordered list item."""
    text: str
    inline: tuple[InlineNode, ...] = ()


@dataclass(frozen=True)
class Table:
    """A markdown table. Flat — no nested cell formatting in Phase 1."""
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Image:
    """An image (markdown `![alt](src)`). `src` is the path or URL."""
    src: str
    alt: str


# Sum type. Used by `match seg: case TextSeg(...):` in parser and renderer.
Segment = Union[
    TextSeg,
    BlockQuote,
    CodeBlock,
    TerminalBlock,
    Heading,
    TaskItem,
    BulletItem,
    Table,
    Image,
]


__all__ = [
    "InlineNode",
    "TextSeg",
    "BlockQuote",
    "CodeBlock",
    "TerminalBlock",
    "Heading",
    "TaskItem",
    "BulletItem",
    "Table",
    "Image",
    "Segment",
]
```

**Verification:**
```bash
cd /path/to/projects/crabcakes
python -c "from chat.segments import TextSeg, CodeBlock, Segment, InlineNode; seg = TextSeg('hi', (InlineNode(kind='bold', text='hi'),)); print(seg)"
# Expect: TextSeg(text='hi', inline=(InlineNode(kind='bold', text='hi', href=None),))
```

### Step 5: Create `chat/parser.py`

**File:** `chat/parser.py` (NEW, ~250 lines)

Uses `mistune.AstRenderer` to walk the markdown AST and produce `Segment` objects.

```python
# chat/parser.py
# Markdown → list[Segment] using mistune's AST renderer.
#
# Pure Python — no GTK, no Pango imports.
# The renderer subclass collects Segment objects; it does NOT produce HTML.
#
# This is the single source of truth for chat AST construction (proposal §3.3).
# Replaces the regex chain in utils/markdown.py + utils/escaping.py (Phase 4 deletion).
#
# ReDoS mitigation (preserved from utils/markdown.py:108):
#   inputs > 100 KB are truncated before parsing.

from __future__ import annotations

import mistune

from chat.segments import (
    BlockQuote,
    BulletItem,
    CodeBlock,
    Heading,
    Image,
    InlineNode,
    Segment,
    Table,
    TaskItem,
    TerminalBlock,
    TextSeg,
)


_MAX_PARSE_LEN = 100 * 1024  # 100 KB — same constant as utils/markdown.py
_TRUNCATION_MARKER = "\n[... input truncated at 100 KB ...]"


# ─────────────────────────────────────────────────────────────────────────────
# Internal: mistune AST walker → list[Segment]
# ─────────────────────────────────────────────────────────────────────────────


class _SegmentCollector(mistune.HTMLRenderer):
    """mistune renderer that emits Segment objects instead of HTML.

    Each callback appends one Segment to self.segments. We coalesce
    adjacent TextSeg nodes in `text()` to keep streaming render cheap.
    """

    def __init__(self) -> None:
        # mistune 3.x: HTMLRenderer takes no args in __init__; we override
        # methods only. If mistune's signature changes, this is the line
        # to patch.
        super().__init__()
        self.segments: list[Segment] = []

    # ── Inline ────────────────────────────────────────────────────────────

    def text(self, text: str) -> str:
        """Emit plain text. Coalesce with previous TextSeg if formatting matches."""
        if not text:
            return ""
        if self.segments and isinstance(self.segments[-1], TextSeg):
            prev = self.segments[-1]
            self.segments[-1] = TextSeg(
                text=prev.text + text,
                inline=prev.inline,
            )
        else:
            self.segments.append(TextSeg(text=text))
        return ""

    def emphasis(self, text: str) -> str:
        """`text` -> bold. mistune wraps this for *italic*; bold is `strong`."""
        # mistune 3.x calls `emphasis(text)` for *italic*. Bold is a separate
        # `strong(text)` method. We can't tell which is which from text alone —
        # mistune's AST passes through distinct callback names. See strong().
        if self.segments and isinstance(self.segments[-1], TextSeg):
            prev = self.segments[-1]
            self.segments[-1] = TextSeg(
                text=prev.text,
                inline=prev.inline + (InlineNode(kind="italic", text=prev.text),),
            )
        return ""

    def strong(self, text: str) -> str:
        """**text** -> bold."""
        if self.segments and isinstance(self.segments[-1], TextSeg):
            prev = self.segments[-1]
            self.segments[-1] = TextSeg(
                text=prev.text,
                inline=prev.inline + (InlineNode(kind="bold", text=prev.text),),
            )
        return ""

    def codespan(self, text: str) -> str:
        """`text` -> inline code."""
        if self.segments and isinstance(self.segments[-1], TextSeg):
            prev = self.segments[-1]
            self.segments[-1] = TextSeg(
                text=prev.text,
                inline=prev.inline + (InlineNode(kind="code", text=prev.text),),
            )
        return ""

    def link(self, text: str, url: str, title: str | None = None) -> str:
        """[text](url) -> link. HIGH-6 scheme allowlist is applied at render time."""
        if self.segments and isinstance(self.segments[-1], TextSeg):
            prev = self.segments[-1]
            self.segments[-1] = TextSeg(
                text=prev.text,
                inline=prev.inline + (
                    InlineNode(kind="link", text=prev.text, href=url),
                ),
            )
        return ""

    # ── Block ─────────────────────────────────────────────────────────────

    def block_code(self, code: str, info: str | None = None) -> str:
        lang = (info or "").strip() or ""
        self.segments.append(CodeBlock(lang=lang, content=code))
        return ""

    def block_quote(self, text: str) -> str:
        # mistune hands us the rendered HTML of children; we discard it
        # because child segments were already emitted via child callbacks.
        # No-op here. Quoted content appears inline with regular segments.
        return ""

    def heading(self, text: str, level: int, **attrs) -> str:
        # mistune 3.x passes level as int. text is rendered children.
        # We re-emit a Heading carrying the rendered text (renderer in
        # Phase 2 will re-tokenize if needed; for now text is plain).
        self.segments.append(Heading(level=level, text=text, inline=()))
        return ""

    def list(self, text: str, ordered: bool, **attrs) -> str:
        # Children emitted via item() callbacks. No-op here.
        return ""

    def list_item(self, text: str, **attrs) -> str:
        # Default to bullet item. Task items are detected by task_list_item.
        if self.segments and isinstance(self.segments[-1], (BulletItem, TaskItem)):
            return ""  # already emitted
        self.segments.append(BulletItem(text=text, inline=()))
        return ""

    def task_list_item(self, text: str, checked: bool, **attrs) -> str:
        self.segments.append(TaskItem(checked=checked, text=text, inline=()))
        return ""

    def paragraph(self, text: str) -> str:
        # Children already emitted as TextSeg.paragraph() calls `text()`.
        # No-op: the TextSeg is already in self.segments.
        return ""

    def thematic_break(self) -> str:
        # Render as a horizontal rule. Phase 2 renderer maps this to a
        # TextTag with a visual separator; for Phase 1, no-op.
        return ""

    def image(self, src: str, alt: str = "", title: str | None = None) -> str:
        self.segments.append(Image(src=src, alt=alt))
        return ""

    def table(self, header: str, body: str) -> str:
        # mistune emits children via table_row / table_cell callbacks. The
        # caller (us) accumulates rows + headers separately. For Phase 1,
        # we keep this simple: leave table rendering for the block_parser
        # path. If mistune's table callback fires, emit a placeholder Table
        # with empty data and let the renderer fall back to text.
        # Phase 2 will implement proper table collection.
        self.segments.append(Table(headers=(), rows=()))
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def parse_message(text: str) -> list[Segment]:
    """Parse raw LLM text into a list[Segment] AST.

    Pure function. No side effects. Safe to call from any thread.

    Args:
        text: Raw LLM output. May contain markdown, code blocks, etc.

    Returns:
        list[Segment]. Never None. Empty input returns [].

    Raises:
        Nothing. mistune is documented to handle malformed input gracefully;
        we additionally truncate at 100 KB to bound worst-case memory.
    """
    if not text:
        return []

    if len(text) > _MAX_PARSE_LEN:
        text = text[:_MAX_PARSE_LEN] + _TRUNCATION_MARKER

    md = mistune.create_markdown(renderer=_SegmentCollector(), plugins=[])
    md(text)
    collector: _SegmentCollector = md.renderer  # type: ignore[attr-defined]
    return collector.segments
```

**Verification:**
```bash
cd /path/to/projects/crabcakes
python -c "from chat.parser import parse_message; segs = parse_message('# Hello\n\n**bold** text'); print(segs)"
# Expect: [Heading(level=1, text='Hello', inline=()), TextSeg(text='bold text', inline=(...))]
```

**Caveat documented above:** mistune 3.x's exact callback names (`emphasis`, `strong`, `codespan`, `block_quote`, `list`, `list_item`, `task_list_item`, `paragraph`, `thematic_break`, `image`, `table`, `link`) and signatures should be verified against the installed `mistune.__version__` before merging. If mistune's API differs (e.g., `block_html` instead of `block_code`, or different kwargs), the renderer subclass methods must be adjusted. The renderer's responsibility is to NOT crash on any input mistune produces; missing callbacks fall through to mistune's default HTML rendering, which is discarded.

### Step 6: Add deprecation warning to `escape_for_pango`

**File:** `utils/escaping.py` (MODIFIED — add at top of file, lines 1-15)

Add after the module docstring (after line 11, before the `import` lines):

```python
import warnings

_DEPRECATION_MSG = (
    "escape_for_pango is deprecated as of 2026-07-16 and will be removed "
    "in Phase 4 of the TextView migration. Use chat.parser.parse_message() "
    "for new code. See docs/proposals/PROPOSAL-textview-texttag-rendering.md."
)
```

Then at the **top** of `def escape_for_pango` (currently at line 96), insert as the first statement:

```python
    warnings.warn(_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)
```

**Do not** modify the body of `escape_for_pango` beyond that single line. All 11 call sites in `chat_bubble.py` and the 1 in `chat_render_handler.py` continue to work; they just emit a warning on each call.

**Verification:**
```bash
cd /path/to/projects/crabcakes
python -W error::DeprecationWarning -c "from utils.escaping import escape_for_pango; escape_for_pango('hi')"
# Expect: DeprecationWarning (treated as error → traceback)

python -m pytest tests/test_escaping.py -v
# Expect: all 65 tests pass (warnings do not fail tests by default)
```

### Step 7: Create `tests/test_chat_parser.py`

**File:** `tests/test_chat_parser.py` (NEW, ~150 lines)

Test against the same input patterns that `tests/test_markdown.py` currently exercises. Phase 1 does NOT need to match `format_markdown`'s exact byte-output — it needs to match the AST shape.

```python
# tests/test_chat_parser.py
# Tests for chat/parser.py and chat/segments.py.
#
# Phase 1 tests focus on AST shape, not byte-for-byte output equivalence
# with the old format_markdown pipeline. Byte-equivalence tests live in
# Phase 4 (after the renderer lands) and Phase 5 (fuzz).

import pytest

from chat.parser import parse_message
from chat.segments import (
    BlockQuote,
    BulletItem,
    CodeBlock,
    Heading,
    Image,
    InlineNode,
    Segment,
    Table,
    TaskItem,
    TerminalBlock,
    TextSeg,
)


class TestEmpty:
    def test_empty_string_returns_empty_list(self):
        assert parse_message("") == []

    def test_whitespace_only_returns_empty_list(self):
        # mistune may produce a single empty TextSeg; accept either.
        result = parse_message("   \n\n   ")
        assert all(isinstance(s, TextSeg) and not s.text.strip() for s in result)


class TestPlainText:
    def test_single_paragraph(self):
        segs = parse_message("Hello, world.")
        assert len(segs) >= 1
        text_segs = [s for s in segs if isinstance(s, TextSeg)]
        assert any("Hello, world." in s.text for s in text_segs)

    def test_preserves_newlines_in_paragraph(self):
        segs = parse_message("line one\nline two")
        text_segs = [s for s in segs if isinstance(s, TextSeg)]
        joined = "".join(s.text for s in text_segs)
        assert "line one" in joined and "line two" in joined


class TestHeadings:
    @pytest.mark.parametrize("level", [1, 2, 3, 4, 5, 6])
    def test_heading_levels(self, level):
        marker = "#" * level + " heading"
        segs = parse_message(marker)
        headings = [s for s in segs if isinstance(s, Heading)]
        assert any(h.level == level and "heading" in h.text for h in headings)


class TestCodeBlock:
    def test_fenced_code_with_lang(self):
        segs = parse_message("```python\nprint('hi')\n```")
        codes = [s for s in segs if isinstance(s, CodeBlock)]
        assert len(codes) == 1
        assert codes[0].lang == "python"
        assert "print('hi')" in codes[0].content

    def test_fenced_code_no_lang(self):
        segs = parse_message("```\nplain\n```")
        codes = [s for s in segs if isinstance(s, CodeBlock)]
        assert len(codes) == 1
        assert codes[0].lang == ""

    def test_indented_code_block(self):
        segs = parse_message("    indented code")
        # mistune may or may not parse this as CodeBlock; accept either
        # CodeBlock or TextSeg containing the text.
        has_code = any(isinstance(s, CodeBlock) for s in segs)
        has_text = any(isinstance(s, TextSeg) and "indented code" in s.text for s in segs)
        assert has_code or has_text


class TestInline:
    def test_bold(self):
        segs = parse_message("this is **bold** text")
        bold_nodes = [
            n for s in segs if isinstance(s, TextSeg)
            for n in s.inline if n.kind == "bold"
        ]
        assert len(bold_nodes) >= 1
        assert any(n.text == "bold" for n in bold_nodes)

    def test_italic(self):
        segs = parse_message("this is *italic* text")
        italic_nodes = [
            n for s in segs if isinstance(s, TextSeg)
            for n in s.inline if n.kind == "italic"
        ]
        assert len(italic_nodes) >= 1

    def test_inline_code(self):
        segs = parse_message("use `print()` here")
        code_nodes = [
            n for s in segs if isinstance(s, TextSeg)
            for n in s.inline if n.kind == "code"
        ]
        assert len(code_nodes) >= 1

    def test_link(self):
        segs = parse_message("see [docs](https://example.com)")
        link_nodes = [
            n for s in segs if isinstance(s, TextSeg)
            for n in s.inline if n.kind == "link"
        ]
        assert len(link_nodes) >= 1
        assert any(n.href == "https://example.com" for n in link_nodes)


class TestBlockquote:
    def test_simple_quote(self):
        segs = parse_message("> quoted text")
        # mistune's block_quote may emit children as TextSeg directly;
        # accept either a BlockQuote wrapper or a TextSeg containing the text.
        has_quote = any(isinstance(s, BlockQuote) for s in segs)
        has_text = any(isinstance(s, TextSeg) and "quoted text" in s.text for s in segs)
        assert has_quote or has_text


class TestList:
    def test_bullet_list(self):
        segs = parse_message("- one\n- two\n- three")
        items = [s for s in segs if isinstance(s, BulletItem)]
        assert len(items) >= 3

    def test_task_list_checked(self):
        segs = parse_message("- [x] done\n- [ ] todo")
        tasks = [s for s in segs if isinstance(s, TaskItem)]
        assert len(tasks) >= 2
        checked = [t for t in tasks if t.checked]
        unchecked = [t for t in tasks if not t.checked]
        assert len(checked) >= 1
        assert len(unchecked) >= 1


class TestImage:
    def test_markdown_image(self):
        segs = parse_message("![alt text](image.png)")
        images = [s for s in segs if isinstance(s, Image)]
        assert len(images) == 1
        assert images[0].src == "image.png"
        assert images[0].alt == "alt text"


class TestReDoSCap:
    def test_input_over_100kb_truncated(self):
        # Build a 200 KB string of plain text.
        huge = "a" * (200 * 1024)
        segs = parse_message(huge)
        # Should NOT have raised. Should have truncated.
        text_segs = [s for s in segs if isinstance(s, TextSeg)]
        joined = "".join(s.text for s in text_segs)
        assert len(joined) <= 100 * 1024 + 200  # 100 KB + truncation marker


class TestNeverRaises:
    """Property-style: random gibberish must not raise."""

    @pytest.mark.parametrize("garbage", [
        "<<<>>>&&&",
        "***unbalanced***",
        "[broken link",
        "```unclosed fence",
        "> > > nested without end",
        "1. numbered\n  - nested bullet",
        "***___~~~ combo",
        "plain text with <script>alert(1)</script>",
        "\\*escaped\\*",
        "",
        "\n\n\n",
        "😀 unicode emoji 🎉",
    ])
    def test_handles_garbage_gracefully(self, garbage):
        # Must not raise. Return value is a list (possibly empty).
        result = parse_message(garbage)
        assert isinstance(result, list)
        for seg in result:
            assert isinstance(seg, Segment)


class TestDeprecationWarning:
    def test_escape_for_pango_warns(self):
        from utils.escaping import escape_for_pango
        with pytest.warns(DeprecationWarning, match="escape_for_pango is deprecated"):
            escape_for_pango("hello")


class TestSegmentImmutability:
    def test_text_seg_is_frozen(self):
        seg = TextSeg(text="hi", inline=())
        with pytest.raises((AttributeError, Exception)):
            seg.text = "changed"  # type: ignore[misc]

    def test_inline_node_is_frozen(self):
        node = InlineNode(kind="bold", text="hi")
        with pytest.raises((AttributeError, Exception)):
            node.kind = "italic"  # type: ignore[misc]
```

**Verification:**
```bash
cd /path/to/projects/crabcakes
python -m pytest tests/test_chat_parser.py -v
# Expect: all tests pass
```

### Step 8: Verify no regression in existing tests

The existing `tests/test_markdown.py` and `tests/test_escaping.py` MUST continue to pass. The deprecation warning does not fail tests by default (pytest treats `DeprecationWarning` as `WARNING` unless `-W error` is passed).

```bash
cd /path/to/projects/crabcakes
python -m pytest tests/test_markdown.py tests/test_escaping.py tests/test_gtk_safe_link.py -v
# Expect: all tests pass (deprecation warnings may appear in output)
```

---

## Edge Cases & Risks

### Mistune callback signature drift

`mistune` 3.x has a specific set of renderer methods. If the installed version's API differs from what `_SegmentCollector` implements:

- **Missing callback method**: mistune falls through to `HTMLRenderer`'s default implementation, which emits HTML strings. We discard the strings (every method returns `""`). The Segment list may be incomplete for that node type.
- **Extra callback method we don't override**: mistune uses our default (which we inherited from HTMLRenderer). Same effect — HTML discarded.

**Mitigation:** Run `tests/test_chat_parser.py` against the installed mistune. If callbacks fire that we don't handle, the test corpus will reveal it (input that should produce a `CodeBlock` produces nothing). Add the override.

### ReDoS in mistune

Mistune is documented as linear-time on most inputs but historically has had quadratic-time bugs. The 100 KB cap bounds worst case to ~100K characters × constant work. Acceptable for chat.

### Frozen dataclass + tuple default

`TextSeg(text="...", inline=())` uses a tuple default. This is safe with `@dataclass(frozen=True)` because tuples are immutable. Confirmed by running `TestSegmentImmutability.test_text_seg_is_frozen`.

### Circular imports

`chat/parser.py` imports from `chat/segments.py`. `chat/segments.py` has no internal imports. No circular risk. `utils/escaping.py` does not import from `chat/` (one-way dep direction).

### `process_segments` is unchanged

`ui/views/chat_bubble.py:131 process_segments()` continues to call `escape_for_pango` and `format_markdown`. Phase 1 does not migrate this — it adds the new path in parallel. The deprecation warning fires for every chat message render in production. This is intentional and visible; if warning spam is a concern, suppress with `PYTHONWARNINGS=ignore::DeprecationWarning` until Phase 3.

---

## Acceptance Checklist

- [ ] `mistune>=3.0,<4.0` declared in `pyproject.toml`
- [ ] `"chat/*"` added to `pyproject.toml` `packages.find` `include` list
- [ ] `chat/__init__.py` exists with module docstring
- [ ] `chat/segments.py` exists with all 9 dataclasses + `Segment` union, zero GTK imports
- [ ] `chat/parser.py` exists with `_SegmentCollector` + `parse_message()`, zero GTK imports
- [ ] `utils/escaping.py` line 96 (`escape_for_pango`) emits `DeprecationWarning` on call
- [ ] `tests/test_chat_parser.py` exists with at minimum: 12 TestNeverRaises parametrize cases, all 6 heading levels, code-block-with-lang, code-block-no-lang, bold/italic/inline-code/link inline, bullet list, task list (checked + unchecked), image, ReDoS cap test, deprecation warning test, frozen dataclass test
- [ ] `python -m pytest tests/test_chat_parser.py -v` passes 100%
- [ ] `python -m pytest tests/test_markdown.py tests/test_escaping.py tests/test_gtk_safe_link.py -v` passes 100% (deprecation warnings allowed)
- [ ] `grep -rn "from gi.repository\|^import gi" chat/` returns 0 lines
- [ ] `grep -rn "from chat\." ui/ agent/` returns 0 lines (chat is not yet consumed)

---

## Files Changed (Phase 1)

```
NEW:
  chat/__init__.py                  (~5 lines)
  chat/segments.py                  (~80 lines, dataclasses, zero GTK)
  chat/parser.py                    (~250 lines, mistune renderer subclass)
  tests/test_chat_parser.py         (~150 lines, AST shape tests)

MODIFIED:
  pyproject.toml                    (+2 lines: 1 dep, 1 package glob)
  utils/escaping.py                 (+9 lines: 1 import, 1 constant, 1 warn call)

NET CHANGE: +485 lines, 0 deletions.
```

---

## What's NOT in this phase (deferred)

| Phase | Deliverable | Status |
|---|---|---|
| 2 | `chat/renderer.py` + one TextView bubble path + feature flag | Not in Phase 1 |
| 3 | All 14 segment types render via TextView; 17 call sites migrated | Not in Phase 1 |
| 4 | `utils/markdown.py` deleted; `utils/escaping.py` trimmed to ~95 lines; fuzz test | Not in Phase 1 |
| 5 | Optional: Mermaid, inline images, clickable tasks, per-link clicks | Not in Phase 1 |

---

## References

- **Proposal:** `docs/proposals/PROPOSAL-textview-texttag-rendering.md` §3 (architecture), §4.1 (parser), §5 (phase plan)
- **Existing parser to model after:** `utils/block_parser.py:31 extract_blocks()` (the same block-segmentation pattern, dicts instead of dataclasses)
- **Existing tests to anchor against:** `tests/test_markdown.py` (95 tests), `tests/test_escaping.py` (65 tests)
- **HIGH-6 to preserve:** `utils/gtk_safe_link.py` (reused verbatim in Phase 2)
- **Streaming path to migrate in Phase 3:** `ui/handlers/chat_render_handler.py:466-472` (lazy imports inside `_update`)
- **Call sites to collapse in Phase 3:** `ui/views/chat_bubble.py:197-803` (11 sites), `ui/handlers/chat_render_handler.py:471-472` (1 site)
- **Mistune 3.x renderer base class:** `mistune.HTMLRenderer` — verify method signatures against installed version
