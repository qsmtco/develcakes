# PHASE 1 — Render Pipeline Invariants

**Spec:** `docs/specs/SPEC-RENDER-PIPELINE-INVARIANTS.md`
**Files to change:** `utils/escaping.py`, `utils/markdown.py`, `tests/test_escaping.py`, `tests/test_markdown.py`

---

## PATCH A — Opening tag: always build from lowercased tag_name

**File:** `utils/escaping.py`

**Current code (around lines 180-184):**
```python
                    full_tag = match.group(0)
                    if attrs.strip():
                        # Escape bare ampersands in attributes only
                        def _escape_attr_ampersands(m):
                            amp = m.group(0)
                            return amp.replace("&", "&amp;")
                        attrs_escaped = re.sub(r'&(?![a-zA-Z#0-9]+;)', _escape_attr_ampersands, attrs)
                        full_tag = f"<{tag_name}{attrs_escaped}>"
                    result.append(full_tag)
```

**Replace with:**
```python
                    # Always build from lowercased tag_name (Pango is case-sensitive).
                    if attrs.strip():
                        # Lowercase attribute NAMES (Pango is case-sensitive on attrs too).
                        # Preserve attribute VALUES exactly.
                        def _lower_attr_names(m):
                            return m.group(1).lower() + m.group(2) + m.group(3)
                        lowered_attrs = re.sub(
                            r'(\s+[a-zA-Z][a-zA-Z0-9_.-]*)(=)("[^"]*"|\'[^\']*\'|[^\s>]*)',
                            _lower_attr_names,
                            attrs,
                        )
                        # Escape bare ampersands in attribute values
                        def _escape_attr_ampersands(m):
                            amp = m.group(0)
                            return amp.replace("&", "&amp;")
                        attrs_escaped = re.sub(r'&(?![a-zA-Z#0-9]+;)', _escape_attr_ampersands, lowered_attrs)
                        full_tag = f"<{tag_name}{attrs_escaped}>"
                    else:
                        full_tag = f"<{tag_name}>"
                    result.append(full_tag)
```

**Key change:** `full_tag = match.group(0)` is removed. ALL tags are built from `f"<{tag_name}...>"` using the already-lowercased `tag_name`. Attribute names are lowercased via regex; attribute values preserved.

---

## PATCH B — Closing tag: emit lowercased tag name

**File:** `utils/escaping.py`

**Current code (line 157):**
```python
                    result.append(match.group(0))
```

**Replace with:**
```python
                    result.append(f"</{tag_name}>")
```

**Context:** This is inside the `if tag_name in _PANGO_KNOWN_TAGS and open_tags and open_tags[-1] == tag_name:` block. The `tag_name` variable is already lowercased (line 153: `tag_name = match.group(1).lower()`).

---

## PATCH C — `_auto_link` group fallback

**File:** `utils/markdown.py`

**Current code (around line 292):**
```python
    def _auto_link(m):
        url = m.group(1)
```

**Replace with:**
```python
    def _auto_link(m):
        # _AUTO_LINK_RE has two alternatives: scheme:// (group 1) and
        # bare.host (group 2). Only one matches; fall back to group 2.
        url = m.group(1) or m.group(2)
        if not url:
            return m.group(0)
```

---

## PATCH D — Delete wrong test, add correct tests

**File:** `tests/test_escaping.py`

**Delete** the `test_uppercase_tag_pair_preserved` test (the one that claims Pango is case-insensitive).

**Add** these tests to `TestOrphanTagSweep` class (or a new `TestPangoCaseSensitivity` class):

```python
class TestPangoCaseSensitivity:
    """Pango is CASE-SENSITIVE on tag names and attribute names."""

    def test_uppercase_tag_pair_normalized(self):
        assert escape_for_pango("<B>orphan</B>") == "<b>orphan</b>"

    def test_mixed_case_tag_normalized(self):
        assert escape_for_pango("<B>x</b>") == "<b>x</b>"
        assert escape_for_pango("<b>x</B>") == "<b>x</b>"

    def test_uppercase_closing_tag_normalized(self):
        assert escape_for_pango("<b>x</B>") == "<b>x</b>"

    def test_uppercase_attribute_name_normalized(self):
        result = escape_for_pango('<span FOREGROUND="red">x</span>')
        assert 'foreground="red"' in result
        assert 'FOREGROUND' not in result

    def test_attribute_value_case_preserved(self):
        result = escape_for_pango('<span foreground="RED">x</span>')
        assert '<span foreground="RED">x</span>' == result

    def test_nested_uppercase_normalized(self):
        assert escape_for_pango("<B><I>nested</I></B>") == "<b><i>nested</i></b>"
```

---

## PATCH E — Bare hostname tests

**File:** `tests/test_markdown.py` — add new class after `TestAutoLinkAttributeProtection`.

```python
class TestAutoLinkBareHostname:
    """Bare hostnames (scheme-less URLs) must auto-link without crashing."""

    def test_bare_hostname_links(self):
        result = format_markdown("visit httpbin.org/help for info")
        assert '<a href="httpbin.org/help">' in result

    def test_bare_hostname_simple(self):
        result = format_markdown("see example.com today")
        assert '<a href="example.com">' in result

    def test_bare_hostname_strips_trailing_period(self):
        result = format_markdown("see example.com.")
        assert '<a href="example.com">' in result
        assert 'example.com.' not in result.split('"')[1] if '"' in result else True

    def test_scheme_url_still_works(self):
        result = format_markdown("see https://x.com")
        assert '<a href="https://x.com">' in result
```

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- **Read each file before editing.** Line numbers may have drifted.
- **PATCH A is the most complex** — the attribute-name lowercasing regex. Test it carefully.
- Do NOT touch any file other than the 4 listed.

## Verification commands

```bash
cd /path/to/projects/crabcakes

# 1. Uppercase tags normalized
python3 -c "
from utils.escaping import escape_for_pango
assert escape_for_pango('<B>orphan</B>') == '<b>orphan</b>', repr(escape_for_pango('<B>orphan</B>'))
assert escape_for_pango('<b>x</B>') == '<b>x</b>', repr(escape_for_pango('<b>x</B>'))
print('OK: uppercase tags normalized')
"

# 2. Attribute names lowercased
python3 -c "
from utils.escaping import escape_for_pango
result = escape_for_pango('<span FOREGROUND=\"red\">x</span>')
assert 'foreground=\"red\"' in result, repr(result)
assert 'FOREGROUND' not in result
print('OK: attribute names lowercased')
"

# 3. Bare hostname works
python3 -c "
from utils.markdown import format_markdown
result = format_markdown('visit httpbin.org/help for info')
assert '<a href=\"httpbin.org/help\">' in result, repr(result)
print('OK: bare hostname links')
"

# 4. Scheme URLs still work
python3 -c "
from utils.markdown import format_markdown
result = format_markdown('see https://x.com')
assert '<a href=\"https://x.com\">' in result
print('OK: scheme URLs still link')
"

# 5. All tests
python3 -m pytest tests/test_escaping.py tests/test_markdown.py -v -k "not test_markup_passes_pango_validation"

# 6. Syntax check
python3 -c "import ast; ast.parse(open('utils/escaping.py').read()); ast.parse(open('utils/markdown.py').read()); print('SYNTAX OK')"
```

## Deliverables

```
COMPLETENESS:
- [x/not done] Patch A: opening tags built from lowercased tag_name + attr names lowered — evidence: (command 1+2)
- [x/not done] Patch B: closing tags emit lowercased name — evidence: (command 1)
- [x/not done] Patch C: _auto_link group fallback — evidence: (command 3)
- [x/not done] Patch D: wrong test deleted, 6 case-sensitivity tests added — evidence: (command 5)
- [x/not done] Patch E: 4 bare-hostname tests added — evidence: (command 5)
- [x/not done] Existing tests pass — evidence: (command 5)
```
