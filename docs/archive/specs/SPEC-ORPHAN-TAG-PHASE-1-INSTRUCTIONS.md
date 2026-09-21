# PHASE 1 — Orphan Tag Sweep + Auto-Link Lookbehind

**Spec:** `docs/specs/spec-orphan-tag-autolink.md`
**Files to change:** `utils/escaping.py`, `utils/markdown.py`, `tests/test_escaping.py`, `tests/test_markdown.py`

---

## FIX 1 — Orphan tag sweep in `escape_for_pango`

**File:** `utils/escaping.py`

**Read the full `escape_for_pango` function first.** The function ends with `return "".join(result)` (around line 190). You are adding code BEFORE that return.

**Current end of function:**
```python
    return "".join(result)
```

**Replace with:**
```python
    # ── Orphan tag sweep ───────────────────────────────────────────────────
    # After the main loop, any tags still on the open_tags stack were opened
    # but never closed. These are orphan tags — plain text that looked like a
    # Pango tag (e.g. grep output containing <a href="...">). Pango would
    # reject an unclosed opening tag, so we escape orphan tags back to literal
    # text.
    output = "".join(result)
    for tag_name in reversed(open_tags):
        tag_pattern = re.compile(
            r'<' + re.escape(tag_name) + r'(?:\s[^>]*)?>',
            re.IGNORECASE
        )
        matches = list(tag_pattern.finditer(output))
        if matches:
            last_match = matches[-1]
            original = last_match.group(0)
            escaped = html.escape(original)
            output = output[:last_match.start()] + escaped + output[last_match.end():]

    return output
```

---

## FIX 2 — Auto-link lookbehind in `_AUTO_LINK_RE`

**File:** `utils/markdown.py`

**Current line ~38:**
```python
    r'(?<![a-zA-Z0-9/:])'  # not preceded by alphanum or ://
```

**Replace with:**
```python
    r'(?<![a-zA-Z0-9/:=])'  # not preceded by alphanum, ://, or = (href="URL")
```

One character added: `=` to the character class.

---

## FIX 3 — Tests for orphan tag sweep

**File:** `tests/test_escaping.py` — append after `TestStrictEntityUnescape` class.

```python
class TestOrphanTagSweep:
    """Orphan opening tags (no matching close) must be escaped."""

    def test_orphan_a_tag_escaped(self):
        result = escape_for_pango('renders <a href="..."> tags')
        assert '<a ' not in result
        assert '&lt;a' in result

    def test_orphan_b_tag_escaped(self):
        result = escape_for_pango('<b>bold')
        assert '<b>' not in result
        assert '&lt;b&gt;' in result

    def test_valid_tag_pair_preserved(self):
        assert escape_for_pango('<b>bold</b>') == '<b>bold</b>'

    def test_valid_a_tag_pair_preserved(self):
        result = escape_for_pango('<a href="https://x.com">link</a>')
        assert '<a href="https://x.com">link</a>' == result

    def test_nested_valid_tags_preserved(self):
        assert escape_for_pango('<b><i>nested</i></b>') == '<b><i>nested</i></b>'

    def test_grep_output_with_a_tag(self):
        """The exact crash trigger: plain text containing <a href="...">."""
        result = escape_for_pango('# ← renders <a href="..."> tags')
        assert '<a ' not in result
        assert '&lt;a' in result

    def test_no_orphan_when_all_closed(self):
        """When all tags are properly closed, sweep does nothing."""
        result = escape_for_pango('<b>one</b> <i>two</i>')
        assert result == '<b>one</b> <i>two</i>'
```

---

## FIX 4 — Tests for auto-link lookbehind

**File:** `tests/test_markdown.py` — append after `TestAngleBracketAutoLink` class.

```python
class TestAutoLinkAttributeProtection:
    """Auto-link regex must not match URLs inside href="..." attributes."""

    def test_url_in_href_not_double_linked(self):
        """A pre-existing <a href="URL"> must not get nested <a> tags."""
        result = format_markdown('<a href="https://example.com">link</a>')
        assert result.count('<a ') == 1, f"Nested <a> tags: {result}"

    def test_plain_url_still_links(self):
        """Regression: plain URLs without attributes still auto-link."""
        result = format_markdown("check https://example.com for info")
        assert '<a href="https://example.com">' in result

    def test_url_after_equals_not_linked(self):
        """URL preceded by = should not auto-link."""
        result = format_markdown('value=https://example.com')
        # Should NOT contain an <a> tag
        assert '<a ' not in result
```

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- Read both files before editing.
- Fix 1 is ~10 lines. Fix 2 is one character. Don't overcomplicate.
- Do NOT touch any other file.

## Verification commands (run these, paste the output)

```bash
cd /path/to/projects/crabcakes

# 1. Orphan tag fixed
python3 -c "
from utils.escaping import escape_for_pango
result = escape_for_pango('renders <a href=\"...\"> tags')
assert '<a ' not in result, f'Orphan <a> not escaped: {result}'
assert '&lt;a' in result
print('OK: orphan tag escaped')
"

# 2. Valid tags still work
python3 -c "
from utils.escaping import escape_for_pango
assert escape_for_pango('<b>bold</b>') == '<b>bold</b>'
assert escape_for_pango('<a href=\"https://x.com\">link</a>') == '<a href=\"https://x.com\">link</a>'
print('OK: valid tags preserved')
"

# 3. No nested <a> from auto-link
python3 -c "
from utils.markdown import format_markdown
result = format_markdown('<a href=\"https://example.com\">link</a>')
assert result.count('<a ') == 1, f'Nested tags: {result}'
print('OK: no nested <a> tags')
"

# 4. Plain URL still auto-links
python3 -c "
from utils.markdown import format_markdown
result = format_markdown('check https://example.com for info')
assert '<a href=\"https://example.com\">' in result
print('OK: plain URL still links')
"

# 5. Full test suites
python3 -m pytest tests/test_escaping.py tests/test_markdown.py -v

# 6. GTK warning gone (orphan tag case)
xvfb-run -a python3 -c "
import gi; gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
from utils.escaping import escape_for_pango
result = escape_for_pango('renders <a href=\"...\"> tags')
label = Gtk.Label()
label.set_markup(result)
print('OK: set_markup succeeded')
" 2>&1 | grep -v dbus | grep -v libEGL | grep -v DRI3
```

## Deliverables (COMPLETENESS checklist required)

```
COMPLETENESS:
- [x/not done] Fix 1: orphan tag sweep in escape_for_pango — evidence: (command 1 output)
- [x/not done] Fix 2: = added to _AUTO_LINK_RE lookbehind — evidence: (command 3 output)
- [x/not done] Fix 3: 7 orphan-tag tests added — evidence: (command 5 output)
- [x/not done] Fix 4: 3 auto-link protection tests added — evidence: (command 5 output)
- [x/not done] Existing tests pass — evidence: (command 5 output)
- [x/not done] GTK warning gone — evidence: (command 6 output)
```
