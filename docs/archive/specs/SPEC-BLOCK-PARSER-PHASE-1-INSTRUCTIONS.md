# PHASE 1 — Block Parser Mixed-Content Paragraph Splitting

**Spec:** `docs/specs/spec-block-parser-mixed-content.md`
**Files to change:** `utils/block_parser.py`, `tests/test_block_parser.py` (additions)

This is a single-phase implementation. The function rewrite and its two call sites are tightly coupled — they must change together.

---

## FIX 1 — Rewrite `_classify_paragraph` to return `list[dict]`

**File:** `utils/block_parser.py`

**Current function starts at line 193.** Read the full current function first (lines 193-251) before editing.

**What changes:**
1. Signature: `-> dict | None` becomes `-> list[dict]`
2. Heading: after extracting the heading segment, recursively classify remaining lines instead of discarding them
3. Quote: replace `all(...)` with a `while` loop that extracts the contiguous `>` prefix, then recursively classify the remainder
4. Task: replace `all(...)` with a `while` loop that extracts the contiguous task-line prefix, then recursively classify the remainder
5. Terminal: unchanged (already absorbs all lines)
6. Table: unchanged
7. Plain text base case: return `[{"type": "text", "content": para}]` (wrapped in a list)

**Full replacement code** (replace the entire current `_classify_paragraph` function from `def _classify_paragraph` through the final `return {"type": "text", "content": para}`):

```python
def _classify_paragraph(para: str) -> list[dict]:
    """
    Classify a paragraph into one or more block segments.

    A paragraph is text between blank lines (no internal blank lines).
    If the first line(s) match a block type (heading, quote, task, terminal),
    extract them as a typed segment, then recursively classify the remaining
    lines. This prevents data loss when a heading is followed by body text
    without a blank-line separator.

    Returns:
        List of segment dicts (always non-empty for non-empty input).
        Empty input returns [{"type": "text", "content": ""}].
    """
    lines = para.split('\n')
    first = lines[0].strip()

    # Heading: starts with # (1-6 levels)
    if first.startswith('#'):
        m = re.match(r'^(#{1,6})(?!#)(.*)$', first)
        if m:
            level = len(m.group(1))
            rest = m.group(2)
            if rest.startswith(' ') or rest.startswith('\t'):
                content = rest[1:]
            else:
                content = rest
            heading_seg = {"type": "heading", "content": content.strip(), "level": level}
            # Recursively classify remaining lines (the body after the heading)
            remaining = '\n'.join(lines[1:]).strip()
            if remaining:
                return [heading_seg] + _classify_paragraph(remaining)
            return [heading_seg]

    # Blockquote: extract contiguous run of lines starting with >
    quote_lines: list[str] = []
    i = 0
    while i < len(lines) and lines[i].lstrip().startswith('>'):
        quote_lines.append(lines[i])
        i += 1
    if quote_lines:
        # Strip > prefixes
        content_lines = []
        for line in quote_lines:
            line = re.sub(r'^>\s?', '', line)
            content_lines.append(line)
        quote_seg = {"type": "quote", "content": "\n".join(content_lines).strip()}
        remaining = '\n'.join(lines[i:]).strip()
        if remaining:
            return [quote_seg] + _classify_paragraph(remaining)
        return [quote_seg]

    # Terminal: first non-empty line starts with $ (output lines may follow without $)
    non_empty = [l for l in lines if l.strip()]
    if non_empty and non_empty[0].lstrip().startswith('$'):
        # Terminal absorbs ALL lines (command + output) — no trailing content to split
        content_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('$'):
                content_lines.append(stripped[1:].lstrip())
            else:
                content_lines.append(stripped)
        return [{"type": "terminal", "content": "\n".join(content_lines).strip()}]

    # Task list: extract contiguous run of task lines
    task_line_re = re.compile(r'^\s*-\s*\[[ xX]\]\s+')
    task_lines: list[str] = []
    i = 0
    while i < len(lines) and task_line_re.match(lines[i]):
        task_lines.append(lines[i])
        i += 1
    if task_lines:
        items = []
        for line in task_lines:
            m = re.match(r'^\s*-\s*\[([ xX])\]\s+(.*)', line)
            if m:
                checked = m.group(1).lower() == 'x'
                items.append({"content": m.group(2).strip(), "checked": checked})
        content = "\n".join(
            f"[{'x' if item['checked'] else ' '}] {item['content']}"
            for item in items
        )
        task_seg = {"type": "task", "content": content}
        remaining = '\n'.join(lines[i:]).strip()
        if remaining:
            return [task_seg] + _classify_paragraph(remaining)
        return [task_seg]

    # Markdown table: at least 2 lines, first has pipes, second is separator
    if '|' in first and _is_markdown_table(lines):
        return [_parse_table(lines)]

    # Plain text — entire paragraph is one text segment
    return [{"type": "text", "content": para}]
```

---

## FIX 2 — Update call site in `extract_blocks` (line 65)

**File:** `utils/block_parser.py`

**Current code at lines 65-67:**
```python
        seg = _classify_paragraph(stripped)
        if seg:
            segments.append(seg)
```

**New code:**
```python
        segs = _classify_paragraph(stripped)
        segments.extend(segs)
```

---

## FIX 3 — Update call site in `_extract_fenced_code_blocks` (line 93)

**File:** `utils/block_parser.py`

**Current code at lines 93-95:**
```python
            seg = _classify_paragraph(para)
            if seg:
                segments.append(seg)
```

**New code:**
```python
            segs = _classify_paragraph(para)
            segments.extend(segs)
```

---

## FIX 4 — Add tests to `tests/test_block_parser.py`

**File:** `tests/test_block_parser.py` — append a new test class at the end of the file.

```python
class TestMixedContentParagraphs:
    """Heading/quote/task followed by non-matching lines within the same paragraph.

    These are the cases that caused data loss (heading) or format degradation
    (quote/task) before the fix. The root cause: _classify_paragraph returned
    a single dict, discarding lines after the first block-type match.
    """

    def test_heading_then_body_single_newline(self):
        """### X\nbody must produce heading + text, not heading only."""
        result = extract_blocks("### X\nbody")
        assert len(result) == 2
        assert result[0]["type"] == "heading"
        assert result[0]["content"] == "X"
        assert result[1]["type"] == "text"
        assert result[1]["content"] == "body"

    def test_heading_then_multiline_body(self):
        """## X\nl2\nl3\nl4 must preserve all body lines."""
        result = extract_blocks("## X\nl2\nl3\nl4")
        assert len(result) == 2
        assert result[0]["type"] == "heading"
        assert result[1]["type"] == "text"
        assert "l2" in result[1]["content"]
        assert "l3" in result[1]["content"]
        assert "l4" in result[1]["content"]

    def test_heading_then_body_no_data_loss(self):
        """The word 'body' must appear somewhere in the output segments."""
        result = extract_blocks("### Heading\nbody text here")
        all_content = " ".join(seg.get("content", "") for seg in result)
        assert "body text here" in all_content, "body text was lost!"

    def test_quote_then_text(self):
        """> quote\nnon-quote must produce quote + text, not text only."""
        result = extract_blocks("> quote\nnon-quote-line")
        assert len(result) == 2
        assert result[0]["type"] == "quote"
        assert "quote" in result[0]["content"]
        assert result[1]["type"] == "text"
        assert result[1]["content"] == "non-quote-line"

    def test_task_then_text(self):
        """- [ ] task\nnot-task must produce task + text, not text only."""
        result = extract_blocks("- [ ] task\nnot-task")
        assert len(result) == 2
        assert result[0]["type"] == "task"
        assert "task" in result[0]["content"]
        assert result[1]["type"] == "text"
        assert result[1]["content"] == "not-task"

    def test_recursive_nesting_heading_quote_text(self):
        """### Heading\n> quote\nbody → [heading, quote, text]."""
        result = extract_blocks("### Heading\n> quote\nbody")
        assert len(result) == 3
        assert result[0]["type"] == "heading"
        assert result[0]["content"] == "Heading"
        assert result[1]["type"] == "quote"
        assert result[1]["content"] == "quote"
        assert result[2]["type"] == "text"
        assert result[2]["content"] == "body"

    def test_two_headings_single_newline(self):
        """## A\n## B → [heading(A), heading(B)]."""
        result = extract_blocks("## A\n## B")
        assert len(result) == 2
        assert result[0]["type"] == "heading"
        assert result[0]["content"] == "A"
        assert result[1]["type"] == "heading"
        assert result[1]["content"] == "B"

    def test_heading_only_no_body(self):
        """### Heading (no body) → single heading segment, no recursion."""
        result = extract_blocks("### Heading")
        assert len(result) == 1
        assert result[0]["type"] == "heading"
        assert result[0]["content"] == "Heading"

    def test_quote_multiline_then_text(self):
        """> line1\n> line2\nplain → [quote(2 lines), text]."""
        result = extract_blocks("> line1\n> line2\nplain")
        assert len(result) == 2
        assert result[0]["type"] == "quote"
        assert "line1" in result[0]["content"]
        assert "line2" in result[0]["content"]
        assert result[1]["type"] == "text"
        assert result[1]["content"] == "plain"

    def test_task_multiple_then_text(self):
        """- [ ] a\n- [x] b\nplain → [task(2 items), text]."""
        result = extract_blocks("- [ ] a\n- [x] b\nplain")
        assert len(result) == 2
        assert result[0]["type"] == "task"
        assert "a" in result[0]["content"]
        assert "b" in result[0]["content"]
        assert result[1]["type"] == "text"
        assert result[1]["content"] == "plain"


class TestMixedContentRegressions:
    """Ensure existing behavior is preserved after the rewrite."""

    def test_blank_line_heading_body_still_works(self):
        """Regression: # Title\n\nbody still produces 2 segments."""
        result = extract_blocks("# Title\n\nSome body text")
        assert result[0]["type"] == "heading"
        assert result[1]["type"] == "text"

    def test_multiline_quote_all_lines(self):
        """Regression: > l1\n> l2\n> l3 → 1 quote segment."""
        result = extract_blocks("> Line one\n> Line two\n> Line three")
        assert len(result) == 1
        assert result[0]["type"] == "quote"
        assert "Line one" in result[0]["content"]
        assert "Line three" in result[0]["content"]

    def test_mixed_tasks_all_lines(self):
        """Regression: - [ ] a\n- [x] b → 1 task segment."""
        result = extract_blocks("- [ ] Item one\n- [x] Item two")
        assert len(result) == 1
        assert result[0]["type"] == "task"

    def test_empty_string(self):
        """Regression: empty input returns [{"type": "text", "content": ""}]."""
        result = extract_blocks("")
        assert result == [{"type": "text", "content": ""}]

    def test_plain_text_multiline(self):
        """Regression: single-newline plain text = 1 text segment."""
        result = extract_blocks("Line one\nLine two")
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "Line one" in result[0]["content"]
        assert "Line two" in result[0]["content"]
```

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- **Read `utils/block_parser.py` in full before editing.** You are replacing the entire `_classify_paragraph` function.
- Make ONLY the changes described above. Do not refactor, rename, or reformat anything else.
- Do NOT touch `ui/views/chat_bubble.py`, `ui/handlers/chat_render_handler.py`, or any other file. The consumer already handles lists of segments.

## Verification commands (run these, paste the output)

```bash
cd /path/to/projects/crabcakes

# 1. Confirm signature changed
python3 -c "
import inspect
from utils.block_parser import _classify_paragraph
sig = inspect.signature(_classify_paragraph)
print('signature:', sig)
assert 'list' in str(sig.return_annotation), 'return type must be list[dict]'
print('OK: returns list[dict]')
"

# 2. Confirm heading body is no longer lost
python3 -c "
from utils.block_parser import extract_blocks
result = extract_blocks('### X\nbody')
assert len(result) == 2, f'Expected 2 segments, got {len(result)}: {result}'
assert result[0]['type'] == 'heading'
assert result[1]['type'] == 'text'
assert result[1]['content'] == 'body'
print('OK: heading body preserved')
"

# 3. Confirm quote+text splits correctly
python3 -c "
from utils.block_parser import extract_blocks
result = extract_blocks('> quote\nplain')
assert len(result) == 2, f'Expected 2, got {len(result)}: {result}'
assert result[0]['type'] == 'quote'
assert result[1]['type'] == 'text'
print('OK: quote+text splits')
"

# 4. Confirm task+text splits correctly
python3 -c "
from utils.block_parser import extract_blocks
result = extract_blocks('- [ ] task\nplain')
assert len(result) == 2, f'Expected 2, got {len(result)}: {result}'
assert result[0]['type'] == 'task'
assert result[1]['type'] == 'text'
print('OK: task+text splits')
"

# 5. Full test suite (existing + new)
python3 -m pytest tests/test_block_parser.py -v

# 6. Broader regression — rendering pipeline
xvfb-run -a python3 -m pytest tests/test_chat_heading.py tests/test_chat_task_segment.py tests/test_chat_terminal_segment.py tests/test_chat_render_handler.py tests/test_markdown.py tests/test_escaping.py -q

# 7. Pattern sweep — both call sites use extend
grep -n 'classify_paragraph\|segments.extend\|segments.append' utils/block_parser.py
```

## Deliverables (COMPLETENESS checklist required)

When done, report:
1. Files changed with line numbers
2. Full output of all 7 verification commands above
3. `git diff utils/block_parser.py` output (the actual changes)
4. COMPLETENESS checklist:
```
COMPLETENESS:
- [x/not done] Fix 1: _classify_paragraph rewritten to return list[dict] with recursive trailing classification — evidence: (command 1+2 output)
- [x/not done] Fix 2: extract_blocks call site uses extend (line 65) — evidence: (command 7 output)
- [x/not done] Fix 3: _extract_fenced_code_blocks call site uses extend (line 93) — evidence: (command 7 output)
- [x/not done] Fix 4: new tests added covering all acceptance criteria — evidence: (command 5 output)
- [x/not done] Existing tests pass (no regressions) — evidence: (command 5 output)
- [x/not done] Broader regression passes — evidence: (command 6 output)
```
