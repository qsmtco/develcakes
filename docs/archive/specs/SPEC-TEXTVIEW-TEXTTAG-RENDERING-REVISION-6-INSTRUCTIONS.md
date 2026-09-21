# Spec Revision Instructions: SPEC-TEXTVIEW-TEXTTAG-RENDERING (Round 6 — FINAL)

**To:** Coder
**Task:** Fix 3 bugs found in round-5 audit (BUG #29, #30, #31). All small, targeted. This should be the final revision before sign-off.

**Output:** Revise `/path/to/projects/crabcakes/docs/specs/SPEC-TEXTVIEW-TEXTTAG-RENDERING.md` in place.

---

## Verified Bugs

### BUG #29 — Plain-text negative assertion overreaches (HIGH, 5/12 test cases fail)
- **Where:** §6 `test_visual_parity` (spec line ~804): `if text.strip() and "**" not in text and "`" not in text: assert tag_names == set()`
- **Impact:** 5 of 12 `TEST_CASES` entries trigger the plain-text branch but are NOT plain text: `italic` (`*italic*`), `quote` (`> quoted text`), `heading` (`## heading 2`), `strikethrough` (`~~struck~~`), `link` (`[click](http://example.com)`). None contain `**` or backtick, so they fall into the negative assertion, but they all produce format tags → assertion fails.
- **Fix:** Replace the substring-guessing logic with an explicit per-case `expected_tags` dict. This is cleaner than adding more substring checks. Replace the entire assertion block with:

  ```python
  # Per-case expected tag sets — explicit, not substring-guessed (BUG #29)
  EXPECTED_TAGS = {
      "bold":          {"bold"},
      "italic":        {"italic"},
      "code_inline":   {"code-inline"},
      "plain":         set(),
      "code_block":    {"code-block"},
      "quote":         {"quote"},
      "heading":       {"heading-2"},
      "strikethrough": {"strike"},
      "link":          {"link"},
      "mixed":         {"bold", "code-inline", "italic"},
      "empty":         set(),
      "only_whitespace": set(),
  }

  @pytest.mark.parametrize("name,text", list(TEST_CASES.items()))
  def test_visual_parity(name, text):
      buffer = Gtk.TextBuffer()
      segments = parse_message(text)
      styles = StyleTable.create(buffer.get_tag_table())
      render_segments(buffer, segments, styles, lambda uri: False)

      rendered_text = buffer.get_text(
          buffer.get_start_iter(), buffer.get_end_iter(), False
      )
      assert len(rendered_text) > 0 or not text.strip()

      attrs = _text_attrs_from_buffer(buffer)
      tag_names = {n for _, _, n in attrs}
      expected = EXPECTED_TAGS[name]
      # Assert every expected tag is present (subset check — renderer may
      # add extra tags like monospace family, that's fine)
      assert expected <= tag_names, \
          f"{name}: expected {expected}, got {tag_names}"
      # For plain/empty/whitespace cases, assert NO formatting tags
      if expected == set():
          assert tag_names == set(), \
              f"{name}: expected no tags, got {tag_names}"
  ```

  Remove the inline `if "**" in text` / `if "\`" in text` / `if text.strip() and ...` blocks entirely — the `EXPECTED_TAGS` dict replaces them. Update the docstring to reference `EXPECTED_TAGS`.

### BUG #30 — `_build_image_block` not enumerated in deletion list (LOW)
- **Where:** §2 "MODIFIED: ui/views/chat_bubble.py" lists 7 `_build_*_segment` methods but `_build_image_block` (chat_bubble.py:396) is not explicitly named.
- **Fix:** Add `_build_image_block (chat_bubble.py:396) — Image block → Image segment via renderer (TextChildAnchor with Gtk.Image)` as an explicit bullet in the Phase 3 method enumeration. Also add it to §8 ARCHITECTURE.md updates table §3.14c–3.14i row as part of the deletion count (now 8 methods, not 7).

### BUG #31 — Missing dict-ordering comment (LOW, cosmetic)
- **Where:** §6 `@pytest.mark.parametrize` line.
- **Fix:** Add `# Python 3.7+ dict ordering is guaranteed; parametrize order matches TEST_CASES insertion order` as a comment above the `@pytest.mark.parametrize` line.

---

## Deliverable

Revised spec (round 6) at the same path. Report back with:

1. COMPLETENESS checklist:
   - [x/not done] BUG #29 (substring-guessing → EXPECTED_TAGS dict)
   - [x/not done] BUG #30 (_build_image_block enumerated)
   - [x/not done] BUG #31 (dict-ordering comment)
2. `wc -l` of revised spec.

**Word marker:** please revise the spec when ready.
