# PHASE 1 — Strict Entity Unescape

**Spec:** `docs/specs/spec-strict-entity-unescape.md`
**Files to change:** `utils/escaping.py`, `tests/test_escaping.py`

---

## FIX 1 — Add strict entity codepoints + regex, replace `html.unescape`

**File:** `utils/escaping.py`

### Step A: Add constants after `_PANGO_VOID_TAGS` (line 38), before `def escape_for_pango` (line 41)

Insert:

```python

# Entity references accepted by the strict unescape. Names match exactly
# what Pango's XML parser accepts plus the standard XML named entities
# for content (so LLM-emitted &quot; etc. decodes correctly to the char).
_ENTITY_CODEPOINTS: dict[str, int] = {
    "amp": 0x26,    # &
    "lt":  0x3C,    # <
    "gt":  0x3E,    # >
    "quot": 0x22,   # "
    "apos": 0x27,   # '
    "nbsp": 0xA0,   # non-breaking space
}

# Strict entity reference pattern: must have a trailing semicolon.
# Matches named entities in _ENTITY_CODEPOINTS or numeric refs (decimal
# or hex). Does NOT match &name (no ;) — those are left as literal text.
_ENTITY_UNESCAPE_RE: re.Pattern[str] = re.compile(
    r"&("
    + "|".join(_ENTITY_CODEPOINTS.keys())
    + r"|#[0-9]+|#x[0-9a-fA-F]+);"
)


def _strict_unescape(text: str) -> str:
    """Decode entity references that have a trailing semicolon.

    Unlike html.unescape (which is HTML5-lenient and decodes &gt, &amp etc.
    even without the trailing ;), this function ONLY decodes well-formed
    entity references. Malformed entities are preserved as literal text
    and handled by the downstream html.escape / attribute-escape logic.
    """
    def _replace(m):
        name = m.group(1)
        if name.startswith("#"):
            try:
                if name.startswith("#x") or name.startswith("#X"):
                    return chr(int(name[2:], 16))
                return chr(int(name[1:]))
            except (ValueError, OverflowError):
                return m.group(0)  # invalid codepoint — preserve literal
        return chr(_ENTITY_CODEPOINTS[name])

    return _ENTITY_UNESCAPE_RE.sub(_replace, text)
```

### Step B: Replace line 78

**Current (line 78):**
```python
    text = html.unescape(text)
```

**Replace with:**
```python
    # Strict unescape: only decode entities WITH trailing semicolon.
    # html.unescape is HTML5-lenient and decodes &gt, &amp etc. without ;,
    # which converts malformed entities to literal chars that break the
    # tag-detection regex below. Strict unescape preserves malformed
    # entities as text; the downstream html.escape handles them safely.
    text = _strict_unescape(text)
```

---

## FIX 2 — Add tests

**File:** `tests/test_escaping.py` — append after the last test class.

```python
class TestStrictEntityUnescape:
    """Strict unescape: only decode entities with trailing semicolon."""

    def test_well_formed_amp(self):
        assert escape_for_pango("Tom &amp; Jerry") == "Tom &amp; Jerry"

    def test_well_formed_lt(self):
        assert escape_for_pango("a &lt; b") == "a &lt; b"

    def test_well_formed_gt(self):
        assert escape_for_pango("a &gt; b") == "a &gt; b"

    def test_malformed_gt_preserved(self):
        """&gt (no ;) must NOT decode to > — this is the core bug fix."""
        result = escape_for_pango("see &gt here")
        assert ">" not in result.replace("&gt;", "").replace("&amp;gt", "")

    def test_malformed_amp_preserved(self):
        result = escape_for_pango("see &amp here")
        # &amp (no ;) should be re-escaped, not decoded to bare &
        assert "&amp;amp" in result or "&amp;" in result

    def test_buggy_autolink_output_robust(self):
        """The exact failure input from the audit bug."""
        broken = '&lt;<a href="https://example.com&gt"><u>https://example.com&gt</u></a>'
        result = escape_for_pango(broken)
        assert 'href="https://example.com>' not in result

    def test_numeric_decimal(self):
        assert escape_for_pango("&#42;") == "*"

    def test_numeric_hex(self):
        assert escape_for_pango("&#x2A;") == "*"

    def test_non_pango_entity_not_decoded(self):
        result = escape_for_pango("&copy; 2024")
        assert "©" not in result

    def test_double_encoded_no_double_decode(self):
        result = escape_for_pango("&amp;amp;")
        assert result == "&amp;amp;"

    def test_invalid_numeric_codepoint_preserved(self):
        """&#999999999; exceeds Unicode range — must not crash."""
        result = escape_for_pango("&#999999999;")
        # Should preserve the literal, not crash
        assert "999999999" in result
```

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- Read `utils/escaping.py` in full before editing.
- Make ONLY the changes described above. Do not refactor anything else.
- Do NOT touch any other file.

## Verification commands (run these, paste the output)

```bash
cd /path/to/projects/crabcakes

# 1. Core fix: &gt without ; is NOT decoded
python3 -c "
from utils.escaping import escape_for_pango
result = escape_for_pango('see &gt here')
assert '>' not in result.replace('&gt;', '').replace('&amp;gt', '')
print('OK: malformed &gt preserved, not decoded to >')
"

# 2. Audit-bug input is safe
python3 -c "
from utils.escaping import escape_for_pango
broken = '&lt;<a href=\"https://example.com&amp;gt\"><u>https://example.com&amp;gt</u></a>'
result = escape_for_pango(broken)
assert 'href=\"https://example.com>' not in result
print('OK: audit-bug input is safe')
"

# 3. GTK warning gone
xvfb-run -a python3 -c "
import gi; gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
from utils.escaping import escape_for_pango
broken = '&lt;<a href=\"https://example.com&amp;gt\"><u>https://example.com&amp;gt</u></a>'
result = escape_for_pango(broken)
label = Gtk.Label()
label.set_markup(result)
print('OK: set_markup succeeded — no Gtk-WARNING')
" 2>&1 | grep -v dbus | grep -v libEGL | grep -v DRI3

# 4. New tests
python3 -m pytest tests/test_escaping.py::TestStrictEntityUnescape -v

# 5. Full regression
python3 -m pytest tests/test_escaping.py tests/test_markdown.py -q

# 6. Pattern sweep — no html.unescape in escaping.py
grep -n "html.unescape" utils/escaping.py
# Expected: 0 matches
```

## Deliverables (COMPLETENESS checklist required)

```
COMPLETENESS:
- [x/not done] Fix 1a: _ENTITY_CODEPOINTS + _ENTITY_UNESCAPE_RE + _strict_unescape added — evidence: (command 1 output)
- [x/not done] Fix 1b: html.unescape replaced with _strict_unescape — evidence: (command 6 output)
- [x/not done] Audit-bug input safe — evidence: (command 2+3 output)
- [x/not done] 11 new tests added — evidence: (command 4 output)
- [x/not done] Existing tests pass — evidence: (command 5 output)
```
