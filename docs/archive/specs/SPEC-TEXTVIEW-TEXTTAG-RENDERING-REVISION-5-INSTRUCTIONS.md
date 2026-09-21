# Spec Revision Instructions: SPEC-TEXTVIEW-TEXTTAG-RENDERING (Round 5)

**To:** Coder
**Task:** Fix 3 bugs found in round-4 audit (BUG #26, #27, #28). All empirically verified by supervisor. These are small, targeted fixes — the architecture is settled.

**Output:** Revise `/path/to/projects/crabcakes/docs/specs/SPEC-TEXTVIEW-TEXTTAG-RENDERING.md` in place.

---

## Verified Bugs

### BUG #26 — `load_fixture()` is undefined (HIGH, test will NameError)
- **Where:** §6 `test_visual_parity` calls `text = load_fixture(fixture_name)` (spec line ~751).
- **Empirical proof (supervisor ran it):** `grep -rn "def load_fixture\|load_fixture(" --include="*.py" .` → zero matches. `load_fixture` does not exist anywhere in the repo. `test_markdown.py` uses inline test data (`format_markdown("**bold text**")`), not externalized fixtures. `tests/fixtures/` contains only `unified.json`.
- **Fix:** Remove the `load_fixture` call. Replace with an inline `TEST_CASES` dict defined in the pseudocode, OR change the test to accept `text` as a parametrized pytest input. Recommended:
  ```python
  # tests/test_textview_parity.py
  TEST_CASES = {
      "bold": "**bold text**",
      "italic": "*italic*",
      "code_inline": "`code`",
      "plain": "just plain text",
      "code_block": "```python\nprint('hi')\n```",
      # ... add more from test_markdown.py patterns
  }

  @pytest.mark.parametrize("name,text", list(TEST_CASES.items()))
  def test_visual_parity(name, text):
      buffer = Gtk.TextBuffer()
      segments = parse_message(text)
      styles = StyleTable.create(buffer.get_tag_table())
      render_segments(buffer, segments, styles, lambda uri: False)
      rendered_text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
      assert len(rendered_text) > 0 or not text.strip()
      attrs = _text_attrs_from_buffer(buffer)
      tag_names = {n for _, _, n in attrs}
      if "**" in text:
          assert "bold" in tag_names, f"{name}: input has ** but no bold tag"
      if "`" in text:
          assert "code-inline" in tag_names or "code-block" in tag_names, f"{name}: input has backtick but no code tag"
      if text.strip() and "**" not in text and "`" not in text:
          assert tag_names == set(), f"{name}: plain text but got tags {tag_names}"
  ```
  Remove the `fixture_name` / `load_fixture` references entirely.

### BUG #27 — `forward_to_tag_toggle` drops tags applied at offset 0 (MEDIUM, false negatives)
- **Where:** §6 `_text_attrs_from_buffer` loop (spec line ~735): `while start.forward_to_tag_toggle(tag):`
- **Empirical proof (supervisor ran it):** Applied `code-block` tag to offsets 0–5. The `forward_to_tag_toggle` loop yielded `[(5, has_tag=False)]` — the tag-on range `(0, 5)` was **silently dropped**. This is because `forward_to_tag_toggle` from inside a tagged region jumps to the tag-OFF boundary, not the tag-ON start.
- **Impact:** Any tag applied at offset 0 (code blocks, quotes, headings, terminal blocks — the most common block tags) produces zero ranges. The parity test reports false negatives.
- **Fix:** Replace the `forward_to_tag_toggle` loop with a char-by-char walk using `iter.has_tag(tag)`:
  ```python
  def _text_attrs_from_buffer(buffer: Gtk.TextBuffer) -> list[tuple]:
      """Extract (start_offset, end_offset, tag_name) tuples via char-by-char walk.

      Uses forward_to_tag_toggle ONLY to skip untagged regions for efficiency,
      then has_tag() at each boundary to capture correct ranges. The naive
      forward_to_tag_toggle loop silently drops tags applied at offset 0
      (BUG #27) because it jumps to the tag-OFF point, not the tag-ON point.
      """
      attrs = []
      tag_table = buffer.get_tag_table()
      char_count = buffer.get_char_count()

      def collect(tag: Gtk.TextTag) -> None:
          in_tag = False
          range_start = 0
          for i in range(char_count):
              it = buffer.get_iter_at_offset(i)
              has = it.has_tag(tag)
              if has and not in_tag:
                  range_start = i
                  in_tag = True
              elif not has and in_tag:
                  attrs.append((range_start, i, tag.get_property("name")))
                  in_tag = False
          if in_tag:
              attrs.append((range_start, char_count, tag.get_property("name")))

      tag_table.foreach(collect)
      return sorted(attrs)
  ```
  Update the explanatory comment to note WHY the char-by-char walk is used (the `forward_to_tag_toggle` offset-0 bug). Note: this is O(n) per tag — acceptable for the parity test since buffer sizes are bounded (test fixtures are small).

### BUG #28 — Image block mapping in Key Decisions table is factually wrong (MEDIUM, spec-vs-source drift)
- **Where:** Key Decisions table BUG #2 row (spec line ~20): claims "Image blocks (handled today via CodeBlock(lang='image') in chat_bubble.py:330, no change needed)" and mapping table says `extract_blocks` emits a distinct image type.
- **Empirical proof (supervisor ran it):**
  - `utils/block_parser.py` has zero `image` matches — `extract_blocks` does NOT emit an image block type.
  - `chat_bubble.py:211` checks `if lang == "image":` on a CODE block (from `extract_blocks` emitting `{"type": "code", "lang": "image", "content": file_path}`), then reclassifies it to `{"type": "image", "file_path": ...}` at line 214 for downstream dispatch.
  - `chat_bubble.py:338` dispatches to `_build_image_block(file_path)` at line 341.
- **Actual flow:** `extract_blocks` emits a code block with `lang="image"` → `chat_bubble.py` detects `lang == "image"` and synthesizes an image block internally.
- **Fix:** Update the Key Decisions table BUG #2 row to accurately describe the flow:
  > "Image blocks: `extract_blocks` emits `{"type": "code", "lang": "image", "content": file_path}` (NOT a distinct image type). `chat_bubble.py:211` detects `lang == "image"` and reclassifies to `{"type": "image", "file_path": ...}` for `_build_image_block()` at line 341. The new parser must preserve this: when mistune encounters a fenced code block with `lang="image"`, emit an `Image(src=content)` segment (where `content` is the file path)."
  Also update the mapping table (§2 parser section) to show the actual source shape: `{"type": "code", "lang": "image"}` → `Image(src=content)`.

---

## Deliverable

Revised spec (round 5) at the same path. Report back with:

1. COMPLETENESS checklist:
   - [x/not done] BUG #26 (load_fixture removed; inline TEST_CASES dict or parametrized)
   - [x/not done] BUG #27 (forward_to_tag_toggle loop replaced with char-by-char has_tag walk)
   - [x/not done] BUG #28 (Key Decisions image block + mapping table corrected to match actual extract_blocks emission)
2. `wc -l` of revised spec.
3. Paste the corrected Key Decisions image-block text (BUG #28 fix).

**Word marker:** please revise the spec when ready.
