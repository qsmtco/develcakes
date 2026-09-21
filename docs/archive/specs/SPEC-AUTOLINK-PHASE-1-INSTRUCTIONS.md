# PHASE 1 — Angle-Bracket Auto-Link Fix

**Spec:** `docs/specs/spec-angle-bracket-autolink.md`
**Files to change:** `utils/markdown.py`, `tests/test_markdown.py`

Single phase. One insertion + tests.

---

## FIX 1 — Add Step 3a angle-bracket auto-link pre-processing

**File:** `utils/markdown.py`

**Insertion point:** After line 234 (`protected = re.sub(r'\[([^\]]+)\]\(...` — the Step 3 markdown link regex) and before line 236 (`# ── Step 4: Auto-link bare URLs`).

**Insert this code between lines 234 and 236:**

```python

    # ── Step 3a: Convert angle-bracket auto-links to anchor placeholders ────
    # CommonMark/GFM auto-link syntax: <https://example.com>
    # After escape_for_pango(), this is &lt;https://example.com&gt;
    # If we let Step 4's auto-link regex run, it would capture &gt; as part
    # of the URL, and _strip_trailing_punct would then strip the trailing
    # semicolon from &gt;, producing the invalid entity &gt (Gtk warning).
    # We pre-process here: extract the URL between the escaped brackets,
    # build an <a> tag, and protect it with the same \x00ANCHOR{N}\x00
    # placeholder that Step 3 uses for markdown links — so Step 6 restores
    # both kinds together.
    def _angle_link_replace(m):
        url = m.group(1)
        anchor_html = f'<a href="{url}"><u>{url}</u></a>'
        if not _validate_link_url(url):
            anchor_html = _WARNING_PREFIX + anchor_html
        anchor_spans.append(anchor_html)
        return f'\x00ANCHOR{len(anchor_spans) - 1}\x00'

    angle_link_re = re.compile(
        r'&lt;((?:https?|ftp|mailto)://(?:[^\s&]|&(?:amp|lt|gt|quot|#\d+|#x[0-9a-f]+);)+)&gt;'
    )
    protected = angle_link_re.sub(_angle_link_replace, protected)
```

**Key points:**
- `anchor_spans` is defined at line 220 (Step 3). The callback appends to it.
- `_validate_link_url` and `_WARNING_PREFIX` are defined at lines 59 and 55.
- The URL is kept in escaped form (already has `&amp;` for `&`) — no `urllib.parse.quote` needed.
- Step 6 (restore anchors at line ~268) already restores all `\x00ANCHOR{N}\x00` placeholders.

---

## FIX 2 — Add tests

**File:** `tests/test_markdown.py` — append a new test class at the end.

```python
class TestAngleBracketAutoLink:
    """Tests for CommonMark/GFM angle-bracket auto-link syntax: <URL>.

    These inputs go through escape_for_pango() BEFORE format_markdown(),
    so angle brackets arrive as &lt; and &gt;. The auto-link regex must
    not capture &gt; as part of the URL, and _strip_trailing_punct must
    not strip the semicolon from the entity.
    """

    def test_angle_bracket_basic(self):
        """<https://example.com> renders as clickable link."""
        from utils.escaping import escape_for_pango
        result = format_markdown(escape_for_pango("see <https://example.com>"))
        assert 'href="https://example.com"' in result
        assert "&gt\"" not in result  # no truncated entity in href
        assert "&gt<" not in result   # no truncated entity before tag

    def test_angle_bracket_standalone(self):
        """Standalone <https://example.com> works."""
        from utils.escaping import escape_for_pango
        result = format_markdown(escape_for_pango("<https://example.com>"))
        assert 'href="https://example.com"' in result

    def test_angle_bracket_with_query_params(self):
        """<https://test.com?a=1&b=2> preserves full query string."""
        from utils.escaping import escape_for_pango
        result = format_markdown(escape_for_pango("see <https://test.com?a=1&b=2>"))
        # href should contain the full URL with &amp; for &
        assert "test.com?a=1&amp;b=2" in result
        assert "&gt" not in result.replace("&gt;", "", 1)  # no broken entities

    def test_angle_bracket_trailing_period(self):
        """go to <https://example.com>. works (period after bracket)."""
        from utils.escaping import escape_for_pango
        result = format_markdown(escape_for_pango("go to <https://example.com>."))
        assert 'href="https://example.com"' in result

    def test_angle_bracket_embedded_in_sentence(self):
        """see <https://example.com> out works."""
        from utils.escaping import escape_for_pango
        result = format_markdown(escape_for_pango("see <https://example.com> out"))
        assert 'href="https://example.com"' in result

    def test_plain_url_still_works(self):
        """Regression: plain URL without angle brackets still auto-links."""
        result = format_markdown("check https://example.com for info")
        assert '<a href="https://example.com">' in result

    def test_markdown_link_still_works(self):
        """Regression: [label](url) still works."""
        result = format_markdown("[label](https://example.com)")
        assert '<a href="https://example.com">' in result

    def test_no_broken_entities_in_output(self):
        """Output must not contain &gt without semicolon (would crash Pango)."""
        from utils.escaping import escape_for_pango
        import re
        result = format_markdown(escape_for_pango("see <https://example.com>"))
        # Look for &gt not followed by ;
        broken = re.findall(r'&gt(?!;)', result)
        assert not broken, f"Broken entities found: {broken}"
```

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- Read `utils/markdown.py` before editing.
- Make ONLY the changes described above. Do not refactor anything else.
- Do NOT touch any other file.

## Verification commands (run these, paste the output)

```bash
cd /path/to/projects/crabcakes

# 1. Bug is fixed — no broken entities
python3 -c "
from utils.escaping import escape_for_pango
from utils.markdown import format_markdown
result = format_markdown(escape_for_pango('see <https://example.com>'))
print('Result:', repr(result))
import re
broken = re.findall(r'&gt(?!;)', result)
assert not broken, f'Broken entities: {broken}'
assert 'href=\"https://example.com\"' in result
print('OK: angle-bracket auto-link works')
"

# 2. Query params preserved
python3 -c "
from utils.escaping import escape_for_pango
from utils.markdown import format_markdown
result = format_markdown(escape_for_pango('<https://test.com?a=1&b=2>'))
assert 'test.com?a=1&amp;b=2' in result
print('OK: query params preserved')
"

# 3. GTK warning gone
xvfb-run -a python3 -c "
import gi; gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
from utils.escaping import escape_for_pango
from utils.markdown import format_markdown
markup = format_markdown(escape_for_pango('see <https://example.com>'))
label = Gtk.Label()
label.set_markup(markup)
print('OK: set_markup succeeded — no Gtk-WARNING')
" 2>&1 | grep -v dbus | grep -v libEGL

# 4. New tests
python3 -m pytest tests/test_markdown.py::TestAngleBracketAutoLink -v

# 5. Full regression
python3 -m pytest tests/test_markdown.py -v
```

## Deliverables (COMPLETENESS checklist required)

```
COMPLETENESS:
- [x/not done] Fix 1: Step 3a angle-bracket pre-processing inserted — evidence: (command 1 output)
- [x/not done] Query params preserved — evidence: (command 2 output)
- [x/not done] No Gtk-WARNING — evidence: (command 3 output)
- [x/not done] 8 new tests added — evidence: (command 4 output)
- [x/not done] Existing tests pass — evidence: (command 5 output)
```
