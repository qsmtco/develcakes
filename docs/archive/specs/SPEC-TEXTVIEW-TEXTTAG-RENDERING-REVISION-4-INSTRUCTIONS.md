# Spec Revision Instructions: SPEC-TEXTVIEW-TEXTTAG-RENDERING (Round 4)

**To:** Coder
**Task:** Fix 3 bugs found in round-3 audit (BUG #16-redux, #23, #24, #25). All empirically verified by supervisor.

**Important admission from supervisor:** BUG #16 was caused by MY round-3 instructions, which told you to use `tag_table.get_nth_tag(i)`. That API does not exist. I recommended an unprobed GTK4 API while citing the project's "always probe GTK4" lesson. The correct API is `tag_table.foreach(callback)`. Apologies — verified working: `tt.foreach(lambda tag: collected.append(tag.get_property('name')))` returned `['bold']` in supervisor's probe.

**Lesson reinforced:** Do NOT trust ANY GTK4 API suggestion (including from supervisor) without probing it yourself. The `foreach` fix below is verified, but probe it again when you implement Phase 0b.

**Output:** Revise `/path/to/projects/crabcakes/docs/specs/SPEC-TEXTVIEW-TEXTTAG-RENDERING.md` in place.

---

## Verified Bugs

### BUG #16-redux — `TextTagTable.get_nth_tag()` does not exist (HIGH, my fault)
- **Where:** §6 `_text_attrs_from_buffer` pseudocode (spec line ~730): `tag = tag_table.get_nth_tag(i)`
- **Empirical proof (supervisor ran it):** `hasattr(tt, 'get_nth_tag')` → `False`. The method does not exist in PyGObject 3.48 / GTK4.
- **Verified-working API:** `tt.foreach(callback)` — callback receives each tag. Probe confirmed: `tt.foreach(lambda tag: collected.append(tag.get_property('name')))` returned `['bold']`.
- **Fix:** Rewrite `_text_attrs_from_buffer` to use `foreach`:
  ```python
  def _text_attrs_from_buffer(buffer: Gtk.TextBuffer) -> list[tuple]:
      attrs = []
      tag_table = buffer.get_tag_table()
      def collect(tag):
          start = buffer.get_start_iter()
          while start.forward_to_tag_toggle(tag):
              end = start.copy()
              end.forward_to_tag_toggle(tag)
              attrs.append((start.get_offset(), end.get_offset(), tag.get_property("name")))
      tag_table.foreach(collect)
      return sorted(attrs)
  ```
- **Update the explanatory comment** that previously said "uses get_size() + get_nth_tag() (NOT iteration)" — change to "uses foreach() callback (NOT direct iteration — TextTagTable is not iterable in PyGObject; get_nth_tag does not exist)".

### BUG #23 — `fixture_has_bold` etc. helpers are undefined (HIGH, NameError on test run)
- **Where:** §6 `test_visual_parity` calls `fixture_has_bold(fixture_name)`, `fixture_has_code(fixture_name)`, `fixture_has_italic(fixture_name)`, `fixture_is_plain_text(fixture_name)` — none defined.
- **Fix:** Inline the classification directly in `test_visual_parity` (no helper functions). The classification is content-based, not fixture-name-based:
  ```python
  def test_visual_parity(fixture_name):
      text = load_fixture(fixture_name)
      buffer = Gtk.TextBuffer()
      segments = parse_message(text)
      styles = StyleTable.create(buffer.get_tag_table())
      render_segments(buffer, segments, styles, lambda uri: False)
      rendered_text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
      assert len(rendered_text) > 0 or not text.strip()  # empty input → empty output OK

      attrs = _text_attrs_from_buffer(buffer)
      tag_names = {name for _, _, name in attrs}

      # Content-based assertions (no fixture-name lookup needed)
      if "**" in text:
          assert "bold" in tag_names, f"{fixture_name}: input has ** but no bold tag"
      if "`" in text:
          assert "code-inline" in tag_names, f"{fixture_name}: input has backtick but no code-inline tag"
      if text.strip() and "**" not in text and "`" not in text:
          # Plain-text fixture: no formatting tags expected
          assert tag_names == set(), f"{fixture_name}: plain text but got tags {tag_names}"
  ```
  Remove the prose paragraph about `fixture_has_*` helpers entirely.

### BUG #24 — `Pango.Style.ITALIS` typo (CRITICAL, crashes StyleTable.create)
- **Where:** §2 StyleTable.create() line ~331: `quote=make("quote", style=Pango.Style.ITALIS, ...)`
- **Empirical proof:** `hasattr(Pango.Style, 'ITALIS')` → `False`. `hasattr(Pango.Style, 'ITALIC')` → `True`. Members: ITALIC, NORMAL, OBLIQUE.
- **Fix:** Change `Pango.Style.ITALIS` → `Pango.Style.ITALIC` (remove the extra S).

### BUG #25 — `follow-link` signal does not exist in GTK4 (CRITICAL, security regression)
- **Where:** 6 places in the spec claim `Gtk.TextView` has a "follow-link" signal for link handling:
  - §1 in-scope row: "link gating moves into renderer via TextView.follow-link"
  - §2 "Link handling" paragraph
  - §3 "Link click" diagram
  - §3.14b.1 ARCH update row
  - Phase 3 list
  - (possibly others — grep for "follow-link" and "follow_link")
- **Empirical proof:** `dir(Gtk.TextView)` filtered for link/follow members returns only `reset_cursor_blink` (unrelated). No `follow-link` signal, no `follow_link` method. The signal existed in GTK3 for embedded `GtkLinkButton` widgets; both were removed in GTK4.
- **Impact:** Implementing the spec literally would connect a handler to a non-existent signal. GObject accepts the `connect()` call but it never fires. HIGH-6 link gate (protection against `javascript:`/`data:` URIs) becomes **silent dead code** — a security regression.
- **Fix:** This requires a DESIGN DECISION, not just a text fix. Two GTK4-native options (you must probe BOTH in Phase 0b and pick the verified-working one — do NOT trust either without probing):
  - **(A) `Gtk.GestureClick` + `iter_at_location()`:** Attach a `GestureClick` controller to the `TextView`. On click release, call `buffer.get_iter_at_location(x, y)` to find the clicked position. Check if a `link` TextTag is applied at that iter via `iter.has_tag(link_tag)`. Extract the href from the tag's stored data (`tag.get_data("href")`). Call `on_activate_link(href)` to gate. Keeps links as inline styled text (best UX).
  - **(B) `Gtk.TextChildAnchor` + `Gtk.LinkButton`:** Insert a `LinkButton` widget as a child anchor at each link range. `LinkButton` has an `activate-link` signal that `on_activate_link` can gate. BUT LinkButton renders as a button (box around the text), changing visual appearance from today's inline colored links.
- **My recommendation: (A)** because it preserves the inline-link visual. BUT probe it first — `iter_at_location`, `has_tag`, and `get_data` on TextTag are all unverified in PyGObject. If (A) doesn't work, use (B).
- **Action required in the spec:**
  1. Replace ALL 6 "follow-link" references with the chosen strategy (probe-confirmed).
  2. Add a Phase 0b probe item: "Verify GestureClick + iter_at_location + has_tag + get_data for link click handling. If fails, fall back to TextChildAnchor + LinkButton."
  3. Add an acceptance test: `test_link_click_blocked_javascript_uri` — render `[x](javascript:alert(1))`, simulate click, assert `on_activate_link` returned True (blocked) and no alert fires.
  4. Update §3.14b.1 ARCH row: replace "follow-link signal" with the verified mechanism.

---

## Verification Commands (run these yourself, do not trust supervisor's probes)

```bash
# Verify get_nth_tag absent, foreach works:
python3 -c "
import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk
tt = Gtk.TextBuffer().get_tag_table()
print('get_nth_tag:', hasattr(tt, 'get_nth_tag'))  # should be False
t = Gtk.TextTag(name='x'); tt.add(t)
out = []; tt.foreach(lambda tag: out.append(tag.get_property('name')))
print('foreach collected:', out)  # should be ['x']
"

# Verify Pango.Style members:
python3 -c "
import gi; gi.require_version('Pango','1.0'); from gi.repository import Pango
print([m for m in dir(Pango.Style) if m.isupper()])
"  # ITALIC, NORMAL, OBLIQUE — no ITALIS

# Verify follow-link absent from TextView:
python3 -c "
import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk
print([m for m in dir(Gtk.TextView) if 'link' in m.lower() or 'follow' in m.lower()])
"  # should NOT contain follow-link or follow_link

# Probe link-handling options (REQUIRED for BUG #25 — pick the working one):
python3 -c "
import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk
# Option A: GestureClick
gc = Gtk.GestureClick()
print('GestureClick exists:', gc is not None)
# iter_at_location
tv = Gtk.TextView()
buf = tv.get_buffer()
# Can we get an iter at a location?
try:
    it = buf.get_iter_at_offset(0)
    print('get_iter_at_offset works')
except Exception as e:
    print('FAIL:', e)
# has_tag
t = Gtk.TextTag(name='link'); buf.get_tag_table().add(t)
print('iter.has_tag:', hasattr(it, 'has_tag'))
# get_data / set_data on TextTag
try:
    t.set_data('href', 'http://x'); print('set_data works:', t.get_data('href'))
except Exception as e:
    print('set_data FAIL:', e)
"
```

## Deliverable

Revised spec (round 4) at the same path. Report back with:

1. Output of all 4 verification commands (paste the actual output).
2. COMPLETENESS checklist:
   - [x/not done] BUG #16-redux (get_nth_tag → foreach)
   - [x/not done] BUG #23 (helpers inlined, no fixture_has_* calls)
   - [x/not done] BUG #24 (ITALIS → ITALIC)
   - [x/not done] BUG #25 (follow-link replaced; probe-confirmed strategy; 6 references updated; acceptance test added)
3. `wc -l` of revised spec.
4. Which link-handling option you chose (A or B) and why (cite your probe output).

**Word marker:** please revise the spec when ready.
