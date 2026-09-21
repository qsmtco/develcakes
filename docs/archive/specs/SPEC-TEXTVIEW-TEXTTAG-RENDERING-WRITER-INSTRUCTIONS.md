# Spec Writer Instructions: SPEC-TEXTVIEW-TEXTTAG-RENDERING

**To:** Coder (spec writer this round)
**Prompt:** Load `prompts/steelFramedSpecWriter.md` fresh. Follow every rule. This is a SPEC-WRITING task, not a code-writing task — but the steel-framed rules about reading source, verifying signatures, and tracing code paths apply identically.
**Source proposal:** `/path/to/projects/crabcakes/docs/proposals/PROPOSAL-textview-texttag-rendering.md` (already read by supervisor)
**Target output:** `/path/to/projects/crabcakes/docs/specs/SPEC-TEXTVIEW-TEXTTAG-RENDERING.md` (NEW — verify with `ls` before writing; do NOT overwrite)

---

## Your Task

Write a build-ready implementation spec that converts the architecture proposal into a phased, verifiable contract. The proposal is strong on architecture but has 5 unresolved issues (listed below) that the spec MUST resolve concretely — a spec with open questions is not build-ready.

## Pre-Write Discovery (MANDATORY — steelFramedSpecWriter Rule 1)

Before writing any spec content, read these files and report what you learned in a DISCOVERY block at the top of the spec:

1. `docs/proposals/PROPOSAL-textview-texttag-rendering.md` — the source proposal (full read)
2. `docs/ARCHITECTURE.md` — at minimum §3.14a (escaping.py), §3.14b (markdown.py), §3.17 (chat_bubble.py / chat_render_handler.py pipeline). Confirm the current documented pipeline.
3. `utils/escaping.py` — full read. Document the actual public API: `escape_for_pango`, `xml_escape_text`, `xml_template`, and any others. Note line counts per function.
4. `utils/markdown.py` — full read. Document `format_markdown` signature, the `_PANGO_KNOWN_TAGS` interaction, the 100KB ReDoS cap location, and the link-scheme allowlist.
5. `ui/views/chat_bubble.py` — full read. Enumerate every `_build_*_segment` method and the exact escape_for_pango + format_markdown call pairs. There are 7 paired call sites (lines ~197/198, 606/607, 637/638, 703/704, 757/758, 783/784, 803/804).
6. `ui/handlers/chat_render_handler.py` — full read. The streaming path. Note the throttle and the `set_markup` calls.
7. `utils/gtk_safe_link.py` — full read. Document `on_activate_link`, `_is_safe_scheme`, and how it's wired today.
8. `tests/test_markdown.py` and `tests/test_escaping.py` — count tests (CURRENT: 82 and 61 respectively — the proposal's "49/32" is stale). These are the migration corpus.
9. `pyproject.toml` — confirm `mistune` is NOT currently a dependency.

## The 5 Unresolved Issues the Spec MUST Resolve

The proposal leaves these open or wrong. The spec must pick a concrete answer for each, with justification rooted in actual source:

### Issue 1 — `escape_for_pango` has 8 out-of-scope call sites that block S3/S6

The proposal's success criteria S3 ("escaping.py ≤100 lines, only xml_escape_text + xml_template") and S6 ("no escape_for_pango in production code") are **unreachable as written** because 8 call sites live outside chat rendering:

- `ui/views/file_tree.py:217` — `self._label.set_markup(escape_for_pango(display_name))`
- `ui/views/file_tree.py:1089` — `safe_name = escape_for_pango(name)`
- `ui/views/main_content.py:299` — `safe_name = escape_for_pango(project_name)`
- `ui/views/diff_card.py:134, 136, 138` — 3 call sites for diff line content
- `ui/views/feed_card.py:140, 317` — 2 call sites for feed card text

These are app-controlled text (not LLM output), so per the proposal's own §2.2 N2 they are "out of scope." But the proposal's success criteria assume the function is deleted. **The spec must resolve this contradiction.** Pick one:

- **(a) Expand scope** — add a phase that migrates these 8 sites to `xml_escape_text` (they don't need Pango tag preservation; they're plain text). Then `escape_for_pango` can be deleted.
- **(b) Revise success criteria** — keep `escape_for_pango` as a leaf utility for app-controlled text; only delete `format_markdown` and the chat-bubble call sites. Update S3/S6 accordingly.

Recommend (a) because it's the cleaner end state and these sites are low-risk (plain text, no markdown). But justify your choice against actual source.

### Issue 2 — mistune AST feasibility is unverified

The proposal's renderer subclass example (§4.1) is pseudocode. `mistune.HTMLRenderer` methods return strings, not objects. Whether a custom renderer can cleanly emit `Segment` objects is unverified.

**The spec must specify one of:**
- **(a) mistune-based parser** — include a concrete, tested code sample (write a throwaway script, run it, paste the output) proving the callback model produces `list[Segment]`. Cite the mistune version.
- **(b) hand-rolled walker** — the proposal's P2 fallback. Specify the parser as a recursive-descent parser we own, with the grammar enumerated.
- **(c) spike-first phase** — add a "Phase 0: feasibility spike" that produces a working `parse_message` against mistune before any UI work. Gate Phase 1 on its success.

Recommend (c) — it's the lowest-risk path and matches the loop's "one phase at a time" discipline.

### Issue 3 — GTK4 TextTag property API is unverified

The proposal's StyleTable example uses `tag.set_property("weight", Pango.Weight.BOLD)` and `tag.set_property("background", "rgba(...)")`. The file-tree loop just taught us GTK4 Python binding signatures diverge from docs (see context.md KEY LESSON). The spec must verify:

- Does `Gtk.TextTag.set_property("weight", ...)` work in PyGObject, or is it `tag.props.weight = ...`?
- Does `tag.set_property("background", "rgba(127,127,127,0.15)")` accept RGBA strings, or only hex `#rrggbb`?
- Does `buffer.insert_with_tags(iter, text, tag1, tag2)` accept varargs tags, or is there a `tags=` kwarg?

**The spec must include a verified code sample.** Write a 20-line GTK4 probe script, run it (set `GDK_BACKEND=gl` or use a headless check), and paste the output. If a property doesn't work, document the workaround.

### Issue 4 — Anchor vs hybrid blocks (§4.6 contradiction)

The proposal's §3.2 thesis is "single TextBuffer, plain Unicode." But §4.6 shows code blocks and tables rendered as `Gtk.Box` wrapping a `Gtk.TextView` — which is NOT a single TextBuffer. Either:

- **(a) Pure TextBuffer** — code blocks/tables are inline text with TextTags (no child widgets). Loses the copy button and per-block CSS classes.
- **(b) Child anchors** — code blocks/tables are `Gtk.TextChildAnchor` + child widgets inside the main TextBuffer. Preserves copy button and CSS. This is the GTK4-native way to embed widgets in text.
- **(c) Hybrid** — the bubble is a `Gtk.Box` of multiple `Gtk.TextView`s (one per block), not a single TextBuffer. This is closest to today's architecture.

The spec must pick one and trace the visual-equivalence implications. Recommend (b) child anchors — it preserves the single-TextBuffer thesis AND the copy button.

### Issue 5 — Visual parity test algorithm is undefined

§6.1 asserts `_attrs_equivalent(old_attrs, new_attrs)` but never defines it. This is the acceptance gate. The spec must define a concrete algorithm:

- What attributes are compared (weight, style, family, foreground, background, strikethrough, underline)?
- How are ranges normalized (start/end byte offsets? character offsets?)?
- What's the tolerance (exact match? subset match?)?
- How does it handle the old path (Gtk.Label) which has no queryable tag table — do we parse the Pango markup back into ranges?

**Recommend:** define parity as "same visible glyphs + same set of (start, end, formatting-attributes) tuples" where formatting-attributes is a frozen set of property key-values. For the old path, parse the generated Pango markup with a small parser to extract ranges. Include pseudocode.

## Spec Structure (use steelFramedSpecWriter template)

Follow the `steelFramedSpecWriter.md` Spec Structure Template. Required sections:

1. Overview (problem, solution, scope in/out table)
2. Changes by File (every file, exact signatures, verified code samples)
3. Data Flow (LLM text → parser → segments → renderer → TextBuffer → pixels)
4. File Change Summary (table: file, change type, lines, risk)
5. Implementation Order (phased — see below)
6. Acceptance Criteria (testable, mapped to proposal G1–G7 with corrections)
7. Edge Cases (table: case → expected behavior)
8. ARCHITECTURE.md Updates Required (§3.14a, §3.14b, §3.17, package diagram)

## Phasing Requirements

The spec MUST phase the work into 1-3 file phases per the implementationLoop. Suggested structure (adjust if your discovery warrants):

- **Phase 0:** Feasibility spike — mistune AST → segments probe + GTK4 TextTag property probe. No production code. Deliverable: two probe scripts + pass/fail report.
- **Phase 1:** `chat/segments.py` + `chat/parser.py` + unit tests. No UI change.
- **Phase 2:** `chat/renderer.py` + StyleTable + ONE bubble segment type migrated behind feature flag.
- **Phase 3:** All 7 chat_bubble call sites migrated + streaming path. Feature flag ON.
- **Phase 4:** Delete `utils/markdown.py`. Migrate the 8 out-of-scope `escape_for_pango` sites (Issue 1a) OR revise success criteria (Issue 1b). Trim `utils/escaping.py`.
- **Phase 5 (optional):** New TextTag-enabled features (clickable links, inline images, mermaid).

Each phase must be independently shippable and independently verifiable.

## Hard Rules

- **No "should work" code samples** (steelFramedSpecWriter Rule 7). Every sample traced or probed.
- **Cite current test counts** (82 / 61), not the proposal's stale numbers.
- **Reference ARCHITECTURE.md sections by number** (§3.14a, §3.14b, §3.17).
- **Do not contradict ARCHITECTURE.md.** If the proposal does, flag it and conform to ARCHITECTURE.md (loop authority hierarchy §5).
- **Spec drift rule:** anchor to identifiers (function names, class names), not line numbers. Line numbers drift.
- **Name the spec file** `SPEC-TEXTVIEW-TEXTTAG-RENDERING.md`. Verify with `ls` before first write — do not overwrite.

## Deliverable

Write the spec to `/path/to/projects/crabcakes/docs/specs/SPEC-TEXTVIEW-TEXTTAG-RENDERING.md`. Report back with:

1. The DISCOVERY block output (what you learned from each file)
2. The 5 issue resolutions (your chosen answer + justification for each)
3. COMPLETENESS checklist:
   - [x/not done] Read all 9 discovery files
   - [x/not done] Resolved Issue 1 (escape_for_pango scope)
   - [x/not done] Resolved Issue 2 (mistune feasibility)
   - [x/not done] Resolved Issue 3 (GTK4 TextTag API)
   - [x/not done] Resolved Issue 4 (anchor vs hybrid)
   - [x/not done] Resolved Issue 5 (parity test algorithm)
   - [x/not done] Spec file written to target path
   - [x/not done] All 8 spec sections present
   - [x/not done] Phasing defined (Phase 0 through Phase 5)
4. The full `wc -l` output for the spec file
5. Any issues or blockers

**Word marker:** please write the spec when ready.
