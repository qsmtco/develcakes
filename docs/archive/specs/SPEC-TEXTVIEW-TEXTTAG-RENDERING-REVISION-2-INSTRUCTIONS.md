# Spec Revision Instructions: SPEC-TEXTVIEW-TEXTTAG-RENDERING (Round 2)

**To:** Coder
**Task:** Revise the spec at `/path/to/projects/crabcakes/docs/specs/SPEC-TEXTVIEW-TEXTTAG-RENDERING.md` based on adversarial audit + supervisor verification. Load `prompts/steelFramedSpecWriter.md` fresh again — Rule 1 (read every file before referencing) and Rule 3 (verify every signature) were violated in round 1.

**Your round-1 spec had fabricated line counts, a wrong ARCHITECTURE.md section reference, and silently omitted two pipeline stages.** This is exactly what steelFramedSpecWriter exists to prevent. Re-read the source files and correct the numbers.

---

## Verified Bugs to Fix (all confirmed by supervisor against actual source)

### CRITICAL — must fix or spec is unbuildable

#### BUG #1 — Wrong ARCHITECTURE.md section reference
- **Spec says:** "conforms to §3.17" for link safety / gtk_safe_link
- **Actual:** §3.17 is `utils/icons.py` (SVG Icon Rendering). Link safety is **§3.14b.1**.
- **Fix:** Replace all "§3.17" references with "§3.14b.1" in the header and §8 (ARCHITECTURE.md updates table).

#### BUG #2 — `utils/block_parser.py` (`extract_blocks`) omitted from scope — THE BIGGEST BUG
- **Spec claims:** "one parser, one source of truth" replacing `escape_for_pango` + `format_markdown`
- **Actual pipeline (verified):** `extract_blocks()` → `escape_for_pango()` → `format_markdown()` → `set_markup()`. There are **THREE** pre-render stages, not two.
- **Evidence:**
  - `utils/block_parser.py` exists (10,745 bytes). Public function: `extract_blocks(text: str) -> list[dict]` at line 31.
  - Called at `ui/views/chat_bubble.py:40` (import) and `:190` (`segments = extract_blocks(text_chunk)`).
  - Documented in ARCHITECTURE.md §3.14g.
  - `chat_render_handler.py:153` comment documents the full 3-stage pipeline.
- **Impact:** The spec's "one parser" thesis is structurally false. `extract_blocks` splits raw text into typed block dicts (code, terminal, quote, table, etc.) BEFORE escape/markdown run per-block. The new `parse_message` must subsume `extract_blocks`'s behavior, OR `extract_blocks` stays and feeds `parse_message`.
- **Fix required:** Decide and document one of:
  - **(A) Subsume:** `chat/parser.py`'s `parse_message` replaces BOTH `extract_blocks` AND `format_markdown`. The block-splitting logic moves into the mistune-based parser. `utils/block_parser.py` is deleted in Phase 4. This is the cleanest "one parser" path.
  - **(B) Keep:** `extract_blocks` stays as a pre-filter; `parse_message` operates on its output. Document the two-stage parser explicitly and drop the "one parser" claim.
  - **Recommend (A)** — it matches the proposal's intent and the spec's thesis. But you must read `utils/block_parser.py` IN FULL and enumerate which block types it produces, then map each to a `Segment` type. The Segment data model may need new types or fields to capture what `extract_blocks` currently encodes in dicts.
- **Add `utils/block_parser.py` to the File Change Summary table** (either DELETED in Phase 4 if option A, or UNCHANGED if option B).

#### BUG #2b (supervisor-found) — `utils/syntax_highlight.py` (`highlight`) omitted — SAME CLASS AS BUG #2
- **Spec:** Never mentions code syntax highlighting.
- **Actual:** `utils/syntax_highlight.py` exists. `highlight()` is imported at `chat_bubble.py:48` and called in the code-block pipeline. ARCHITECTURE.md §3.14h documents it. `chat_render_handler.py:156` comment: "code → syntax_highlight() (HTML-escapes internally)".
- **Impact:** Code blocks today get Pygments-based syntax highlighting (token spans with color TextTags). The spec's CodeBlock Segment just has `lang` + `content` — no highlighting. Phase 3 will regress code rendering to monochrome.
- **Fix required:** Read `utils/syntax_highlight.py` in full. Decide:
  - **(A)** `CodeBlock` rendering applies Pygments highlighting via per-token TextTags (preserve current behavior). Document the highlight→TextTag mapping.
  - **(B)** `CodeBlock` rendering drops highlighting (regression). Document explicitly as an accepted trade-off with rationale.
  - **Recommend (A)** — highlighting is a visible feature; dropping it is a regression the captain will reject.
- **Add `utils/syntax_highlight.py` to scope** (either MODIFIED if the TextTag adapter is new, or UNCHANGED if `highlight()` output is consumed as-is).

#### BUG #3 — `models/streaming.py` (`StreamingBubble`) omitted from changes
- **Spec §2 says:** `build_streaming_bubble()` returns `(container, buffer, text_view)` instead of `(container, label)`
- **Actual:** `models/streaming.py:12` defines `class StreamingBubble` (dataclass) with field `label: object` at line 27. Used at `chat_render_handler.py:471` (`sb.label.set_text(sb.plain_text + " ▍")`).
- **Impact:** Changing the return shape of `build_streaming_bubble` without updating `StreamingBubble` causes `AttributeError` on every streaming delta.
- **Fix:** Add `models/streaming.py` to §2 (MODIFIED). Define the new schema: replace `label` with `text_view` + `buffer` (or whatever shape matches your streaming design from BUG #4 resolution). Show the exact dataclass.

#### BUG #4 — Streaming model is internally contradictory + cost-unanalyzed
- **Spec Discovery §6 says:** "Streaming path uses `set_text()` directly (no markup during stream)"
- **Spec §3 Data Flow says:** "Delta → parse_message(accumulated) → clear TextBuffer → render_segments" (parse on every delta)
- **Actual:** `chat_render_handler.py:471` uses `sb.label.set_text()` — a Gtk.Label, not TextBuffer.
- **Contradiction:** If parse_message runs on every delta (150ms throttle = ~6.7x/sec), and mistune parses the full accumulated text each time, that's O(n²) over the stream. Spec's "<2s for 1000 deltas" budget (S7) is unverified and likely wrong.
- **Fix required:** Pick ONE streaming model and specify it end-to-end:
  - **(A) Parse-on-end:** During streaming, append raw text to a plain TextBuffer (no parse, no formatting). On `end_streaming`, parse the final text and re-render with formatting. Simple, fast, but streaming shows unformatted text.
  - **(B) Parse-on-every-delta (throttled):** Re-parse on each throttle tick. Shows formatting live but O(n²) cost. Must include a measured performance budget (write a probe, measure mistune parse time on 1KB/10KB/50KB inputs, paste numbers).
  - **(C) Incremental:** Parse only the new delta, append segments. Complex (markdown is not incremental-friendly — an unclosed `**` changes meaning of prior text). Likely infeasible without a streaming-aware parser.
  - **Recommend (A)** with a note that (B) is a Phase 5 enhancement if users want live formatting. Document the trade-off explicitly. Whichever you pick, update §3 Data Flow and §7 Edge Cases to be consistent.

#### BUG #14 — `parse_message` failure mode causes silent data loss
- **Spec §2 says:** "Failure mode: Returns empty list on any parse error (never raises)"
- **Impact:** If mistune throws on malformed input, the entire LLM response renders as nothing (empty bubble). This is the B3 bug class the proposal was written to eliminate — the spec reintroduces it.
- **Fix:** Change failure mode to: on any exception, log warning, return `[TextSeg(text=original_raw_input)]` (single segment with the raw text, no formatting). Add a test: `test_parse_malformed_input_falls_back_to_raw_text`.

### HIGH — must fix for spec correctness

#### BUG #5 — `mistune` dependency ordering
- Phase 0 probe needs mistune installed, but `pyproject.toml` edit is listed under Phase 1.
- **Fix:** Move the `pyproject.toml` edit (add `mistune>=3.0,<4.0`) to Phase 0. State: "Phase 0a: edit pyproject.toml, run `pip install -e .`, then run probe."

#### BUG #6 — mistune SegmentRenderer API is wrong
- **Spec §2 shows:** `class SegmentRenderer(mistune.HTMLRenderer)` with methods returning `str` ("string return is unused").
- **Actual:** mistune 3.x `HTMLRenderer.block_quote(text)` receives ALREADY-RENDERED inner HTML — it does NOT call back for children. To walk the AST you need `mistune.AstRenderer` or the v3 token-based `BaseRenderer`.
- **Fix:** Mark the entire `chat/parser.py` §2 section as **"SUBJECT TO PHASE 0 PROBE — API UNVERIFIED"**. Remove the word "Verified contract." The Phase 0a probe's job is to determine the actual API. Do not document a specific subclass pattern as if confirmed.

#### BUG #8 — Test corpus port is infeasible as stated
- **Spec §2 says:** "The entire `test_markdown.py` corpus (82 tests) must be reimplemented as parser tests."
- **Actual:** `test_markdown.py` asserts on Pango MARKUP STRINGS (e.g., `"<b>bold</b>"`), not Segment ASTs. "Reimplementing" means rewriting every assertion from string-match to segment-structure-match — a 1-2 week effort, not a Phase 1 deliverable.
- **Fix:** Split into two acceptance tiers:
  - **Phase 1:** `test_markdown.py` still passes UNCHANGED (old path intact, parser not wired). New `test_chat_parser.py` has ~20-30 unit tests on segment structure for representative inputs.
  - **Phase 3:** Visual parity test (`test_textview_parity.py`) renders each `test_markdown.py` FIXTURE through both paths and compares TextTag ranges. This is the real migration gate — not re-asserting 82 tests.

#### BUG #9 — Scope contradiction on non-chat migrations
- **Spec §1 Out-of-scope says:** "file tree (already use xml_escape_text for app text)"
- **Spec §2 Phase 4 says:** migrate file_tree, diff_card, feed_card escape_for_pango → xml_escape_text.
- **These contradict.** The spec simultaneously says file_tree is out of scope and in scope.
- **Fix:** Resolve the contradiction. Two coherent options:
  - **(A) Keep migrations, fix the scope table:** Update §1 Out-of-scope to say "file tree rendering logic (but escape_for_pango→xml_escape_text migration IS in scope as part of escape_for_pango deletion)." The 8 sites must be migrated to delete escape_for_pango.
  - **(B) Drop migrations, keep escape_for_pango as leaf:** Revise S3/S6 — escape_for_pango stays as a leaf utility for app-controlled text; only format_markdown and chat call sites are removed. escaping.py stays >100 lines.
  - **Recommend (A)** — cleaner end state. But the scope table must be internally consistent.

### MEDIUM — fix for completeness

#### BUG #7 — Void-tag defense rationale must be preserved in ARCH §8 update
- `escape_for_pango`'s void-tag escaping (`<br>`, `<hr>`, `<img>`, `<wbr>`) is documented in ARCH §3.14a as a defense against a real bug class. The new pipeline doesn't need it (no markup parse), but ARCH §8 update must document WHY it's no longer needed, not just delete the section.
- **Fix:** In §8 ARCHITECTURE.md updates, add: "§3.14a void-tag defense is obsolete because the new pipeline never calls set_markup() with LLM-derived content. Document the rationale for future readers."

#### BUG #10 — Fenced code block with `javascript:` URI needs explicit test
- **Fix:** Add to §6 acceptance: `test_fenced_code_javascript_uri_not_linkable` — assert that text inside a CodeBlock segment is never wrapped in a link TextTag.

#### BUG #11 — `chat/` package name collision risk
- `chat/` at project root may collide conceptually with `ui/handlers/chat_handler.py`, `chat_render_handler.py`. Also `from chat import ...` may shadow.
- **Fix:** Consider `chat_render/` or `rendering/` instead. If you keep `chat/`, add a one-line justification in §1 and confirm no import shadowing via grep.

#### BUG #12 — Parse-on-delta vs parse-on-end ambiguity (resolved by BUG #4 fix)
- Once BUG #4 is resolved (pick one streaming model), this is resolved. Ensure §3 and §7 are consistent with the choice.

#### BUG #13 — `xml_template` usage audit
- `xml_template` is used inside `chat_bubble.py` (lines 38, 714, 733, 741, 749) — not just non-chat views. The Phase 4 migration table should account for these if they're in chat code paths being refactored.
- **Fix:** Grep `xml_template` across all files in scope. List actual usage. Don't claim "non-chat views already use xml_escape_text correctly" without verifying.

### Additional fixes required (supervisor-found, not in Debugger report)

#### SUP-1 — Line counts in Discovery are fabricated
- **Spec Discovery says:** escaping.py 187 lines, markdown.py 279 lines, gtk_safe_link.py 107 lines.
- **Actual (verified by `wc -l`):** escaping.py **302**, markdown.py **338**, gtk_safe_link.py **148**.
- **Fix:** Correct all line counts in Discovery and §2. steelFramedSpecWriter Rule 3 requires verifying against source — the numbers were off by ~40%.

#### SUP-2 — Segment data model inconsistencies
- `Heading` fields are `(level, text, inline)` but the parser callback example is `heading(self, text, level)` — arg order vs field order mismatch.
- `Heading.inline` and `BulletItem.inline` lack default `= ()` (unlike `TextSeg` and `TaskItem` which have it). Inconsistent dataclass ergonomics.
- `Image` segment type from the proposal §3.3 was silently dropped. Either include it or document why it's excluded.
- **Fix:** Standardize field order (text-bearing first), add `= ()` defaults to all `inline`/`blocks` tuple fields, decide on `Image` inclusion.

#### SUP-3 — `block_parser.py` block types must map to Segment types
- Read `utils/block_parser.py` and enumerate the dict keys/types that `extract_blocks` produces. Each must map to a `Segment` type. If `extract_blocks` produces block types not in the current Segment union, add them. This is part of BUG #2 resolution (A).

#### SUP-4 — Fuzz strategy alphabet is too narrow
- Spec's fuzz test uses `whitelist_characters="*_~\`<>[]()#-+\n.=|:;/\\"` — omits `&`, `%`, `{`, `}`, `!`, `?`, `"`, `'`.
- These are exactly the characters that caused incidents B2 (`&quot;`) and B5. The fuzz test won't catch the bug class it's meant to prevent.
- **Fix:** Expand the alphabet to include `&%{}!?"'` and other punctuation. The goal is to test the chars that break parsers.

#### SUP-5 — Phase 0b "headless" GTK probe may be infeasible
- Spec says run probe "with `GDK_BACKEND=gl` or headless."
- GTK4 requires a display connection. Without `$DISPLAY` (or Wayland), `Gtk.init()` fails.
- **Fix:** Specify the probe environment: either (a) requires `$DISPLAY` (document it as a manual probe, not CI), or (b) use `Gtk.test_init()` or a broadway backend. State which. Don't leave it ambiguous.

---

## How to Revise

1. Re-read every source file listed in BUG #2, BUG #2b, BUG #3, SUP-1, SUP-3. Paste actual line counts and signatures in Discovery.
2. Resolve BUG #2 (extract_blocks), BUG #2b (syntax_highlight), BUG #4 (streaming model) — these three determine the architecture. State your resolution at the top of the revised spec in a "Key Decisions" section.
3. Correct all line counts (SUP-1).
4. Update the File Change Summary table to include `utils/block_parser.py`, `utils/syntax_highlight.py`, `models/streaming.py`.
5. Update §6 acceptance criteria for the test-port reality (BUG #8).
6. Mark all mistune API references as "SUBJECT TO PHASE 0 PROBE" (BUG #6).
7. Fix the Segment data model (SUP-2).

## Deliverable

Revised spec at the same path. Report back with:

1. A "Key Decisions" block stating your resolution to BUG #2, BUG #2b, BUG #4, BUG #9 (one sentence each).
2. Updated Discovery block with CORRECT line counts.
3. COMPLETENESS checklist — every bug listed above, marked fixed or flagged with rationale:
   - [x/not done] BUG #1 (§3.17 → §3.14b.1)
   - [x/not done] BUG #2 (extract_blocks scope)
   - [x/not done] BUG #2b (syntax_highlight scope)
   - [x/not done] BUG #3 (StreamingBubble model)
   - [x/not done] BUG #4 (streaming model)
   - [x/not done] BUG #5 (pyproject.toml Phase 0)
   - [x/not done] BUG #6 (mistune API marked unverified)
   - [x/not done] BUG #7 (void-tag rationale)
   - [x/not done] BUG #8 (test corpus tiered)
   - [x/not done] BUG #9 (scope contradiction)
   - [x/not done] BUG #10 (javascript URI test)
   - [x/not done] BUG #11 (package name)
   - [x/not done] BUG #12 (streaming consistency)
   - [x/not done] BUG #13 (xml_template audit)
   - [x/not done] BUG #14 (failure mode raw-text fallback)
   - [x/not done] SUP-1 (line counts corrected)
   - [x/not done] SUP-2 (Segment model consistency)
   - [x/not done] SUP-3 (block types mapped)
   - [x/not done] SUP-4 (fuzz alphabet expanded)
   - [x/not done] SUP-5 (Phase 0b display requirement)
4. `wc -l` of revised spec.
5. Any blockers.

**Word marker:** please revise the spec when ready.
