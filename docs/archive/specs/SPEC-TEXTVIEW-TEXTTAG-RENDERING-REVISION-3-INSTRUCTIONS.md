# Spec Revision Instructions: SPEC-TEXTVIEW-TEXTTAG-RENDERING (Round 3)

**To:** Coder
**Task:** Fix 8 bugs found in round-2 audit (BUG #15–#22). All empirically verified by supervisor. The architecture is settled — these are localized fixes. Load `prompts/steelFramedSpecWriter.md` fresh.

**Output:** Revise `/path/to/projects/crabcakes/docs/specs/SPEC-TEXTVIEW-TEXTTAG-RENDERING.md` in place.

---

## Verified Bugs (all confirmed by supervisor against actual GTK4 + source)

### BUG #15 — `tag.props.items()` does not exist (HIGH, blocks S1)
- **Where:** §6 visual parity test pseudocode (spec lines ~702–717).
- **Empirical proof (supervisor ran it):** `Gtk.TextTag().props.items()` → `AttributeError: 'gi._gi.GProps' object has no attribute 'items'`
- **Fix:** Rewrite `_text_attrs_from_buffer` to extract properties via GObject introspection. The correct pattern:
  ```python
  # List installed properties on the TextTag class
  props = GObject.list_properties(Gtk.TextTag)
  for p in props:
      name = p.name
      value = tag.get_property(name)
      # ...include name+value in the frozen attrs set
  ```
  OR, simpler and more robust: drop the per-tag property extraction entirely and compare by **tag name + applied (start, end) ranges only**. Two buffers are "equivalent" if the same set of tag names covers the same (start, end) offset ranges. This sidesteps the GProps introspection quagmire entirely and is sufficient for parity (the StyleTable assigns well-known names like "bold", "italic", "code-inline" — comparing names is enough).
- **Recommend the simpler tag-name+ranges approach.** Document it clearly.

### BUG #16 — `TextTagTable` is not iterable (HIGH, blocks S1)
- **Where:** §6 visual parity test pseudocode `for tag in tag_table:` (spec line ~706).
- **Empirical proof:** `for t in Gtk.TextBuffer().get_tag_table(): pass` → `TypeError: 'TextTagTable' object is not iterable`
- **Fix:** Use the indexed API:
  ```python
  size = tag_table.get_size()
  for i in range(size):
      tag = tag_table.get_nth_tag(i)
      ...
  ```

### BUG #17 — 7 paired call-site line numbers are fabricated (HIGH — BUG #13 redux)
- **Where:** Discovery §8 (spec lines ~68–74) and the image block line claim.
- **Verified actual line numbers (from `grep -n "escape_for_pango" ui/views/chat_bubble.py`):**
  ```
  197/198  _process_text_chunk (text flush path)   [spec claimed 294]
  606/607  _make_table_cell                         [spec claimed 648]
  637/638  _build_text_segment                      [spec claimed 681]
  703/704  _build_quote_segment                     [spec claimed 722]
  757/758  _build_terminal_segment (per-line)       [spec claimed 786]
  783/784  _build_heading_segment                   [spec claimed 812]
  804/805  _build_task_segment                      [spec claimed 833]
  ```
- **Image block line:** `seg_type == "image"` is at **line 338**, NOT 330 (line 330 is `"code"`).
- **Fix:** Replace the spec's call-site table with the actual numbers above. Update the image-block reference to line 338.
- **Reminder (steelFramedSpecWriter Rule 3):** every line number must be verified by `grep -n` or `sed -n '<LINE>p'`. Do NOT copy line numbers from the round-2 spec. Re-run `grep -n "escape_for_pango" ui/views/chat_bubble.py` yourself and paste the output into Discovery.

### BUG #18 — Streaming cursor design contradicts itself (MEDIUM, internal contradiction)
- **Where:** Key Decisions BUG #4 row says cursor is "a plain Gtk.Label"; StyleTable.create() in §2 defines a `streaming_cursor: Gtk.TextTag` field with `foreground="#888888"`.
- **These contradict.** If cursor is a Label, the TextTag is dead code. If cursor is a TextTag, BUG #4's decision text is wrong.
- **Fix:** Pick ONE and make the spec consistent. Recommend: **drop `streaming_cursor` from StyleTable entirely** (the BUG #4 decision is "plain Gtk.Label during streaming" — parse-on-end means no TextTags are applied during streaming, so a cursor TextTag is pointless). Remove the `streaming_cursor` field from StyleTable and the `streaming_cursor=make(...)` line from `create()`.

### BUG #19 — Visual parity assertion is a tautology (MEDIUM, S1 is meaningless)
- **Where:** §6 `test_visual_parity` pseudocode asserts `assert len(rendered) >= 0` — always true.
- **Fix:** With BUG #15/#16 fixed (tag-name+ranges comparison), the assertion becomes meaningful. Concretely:
  ```python
  def test_visual_parity(fixture_name):
      text = load_fixture(fixture_name)
      new_attrs = _text_attrs_from_buffer(render_new_path(text))  # [(start, end, tag_name), ...]
      # Compare against expected ranges from a precomputed golden file OR
      # assert that expected formatting (e.g. bold on "X") is present
      assert ("bold" in {name for _, _, name in new_attrs}) == fixture_has_bold(fixture_name)
  ```
  If full old-vs-new parity comparison is too complex for CI (needs display), downgrade S1 to: **"For each fixture, the new path renders without exception AND the expected tag-name set is present."** Remove the tautology. Add at least one negative test (a fixture that should NOT have bold does not produce a "bold" tag).

### BUG #20 — Speculative fallback caveat is dead text (LOW, cleanup)
- **Where:** §2 renderer StyleTable note: "If `set_property` doesn't accept GEnum values directly, use `tag.props.weight = Pango.Weight.BOLD` instead."
- **Empirical proof:** `tag.set_property("weight", Pango.Weight.BOLD)` works, returns weight=700. `insert_with_tags` varargs works.
- **Fix:** Remove the "if it doesn't accept GEnum" fallback text. State plainly: "Phase 0b confirms `set_property` accepts Pango enums (weight, style, scale, underline) and RGBA background strings." Keep the Phase 0b probe but focus it on edge cases (does `rgba()` with alpha < 0.1 round-trip? does `scale=Pango.Scale.XX_LARGE` work or does it need a float?).

### BUG #21 — Streaming diagram vs Phase 3 insertion strategy mismatch (LOW)
- **Where:** §3 streaming path says `buffer.insert(accumulated)` (re-inserts whole text — O(n²)); Phase 3 says `buffer.insert(end_iter, delta_text)` (inserts only delta — O(1)).
- **Fix:** Update §3 diagram to match Phase 3: **`buffer.insert(end_iter, delta_text)`** — incremental append. The accumulated string is tracked in `plain_text` for the final `parse_message` call on `end_streaming`, but the buffer only receives the delta.

### BUG #22 — StreamingBubble schema has no cursor field (LOW, schema gap)
- **Where:** §2 StreamingBubble schema has `container, text_view, buffer, role, plain_text, bubble` — no `cursor` field. BUG #4 says cursor is "a plain Gtk.Label."
- **Fix:** Add `cursor: object = None  # Gtk.Label for the ▍ character, packed into container` to the StreamingBubble dataclass. OR document that the cursor is packed directly into `container` without a dedicated field (acceptable if the cursor is created/destroyed inline in start/end_streaming). Recommend the field for clarity.

---

## Additional Verification Requirement

Per steelFramedSpecWriter Rule 3, you must re-run `grep -n "escape_for_pango" ui/views/chat_bubble.py` and paste the ACTUAL output into your Discovery block. Do not transcribe the numbers from this instructions file — verify them yourself.

## Deliverable

Revised spec (round 3) at the same path. Report back with:

1. COMPLETENESS checklist (all 8 bugs):
   - [x/not done] BUG #15 (tag.props.items() → tag-name+ranges)
   - [x/not done] BUG #16 (TextTagTable iteration → get_size/get_nth_tag)
   - [x/not done] BUG #17 (call-site line numbers corrected; paste grep output)
   - [x/not done] BUG #18 (drop streaming_cursor from StyleTable)
   - [x/not done] BUG #19 (parity assertion meaningful or S1 downgraded)
   - [x/not done] BUG #20 (remove speculative fallback caveat)
   - [x/not done] BUG #21 (§3 streaming diagram → incremental insert)
   - [x/not done] BUG #22 (StreamingBubble cursor field or documented placement)
2. `wc -l` of revised spec.
3. Paste the output of `grep -n "escape_for_pango" ui/views/chat_bubble.py` to prove the call-site numbers are verified.

**Word marker:** please revise the spec when ready.
