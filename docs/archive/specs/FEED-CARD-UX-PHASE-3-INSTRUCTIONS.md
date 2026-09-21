# Phase 3 of 5 — Sequence Numbers on Cards (+ Migration for Existing feed.json)

**Master spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-FEED-CARD-UX.md` (read it in full, especially the "Spec Revision History" at the top and Section 2.5/2.6/2.7)

**Phase 1 status:** COMPLETE (model helpers + button logic + sub-state CSS + 28 tests, 1694 passed baseline)
**Phase 2 status:** COMPLETE (decision badges on git_commit + approval cards, 4 new tests, no scope creep)

**Scope of this phase:** Section 2.5 + 2.6 + 2.7 of the spec (3 files). Medium complexity — touches model, handler, and view.

---

## ⚠️ MANDATORY: Read every file in full before writing any code

Per `prompts/steelFramedCodeWriter.md` Rule 1, before writing ANY code, read EVERY file you will touch. ALL of it. Not snippets — the whole file. The spec's line numbers are stale — anchor edits to identifiers, NOT line numbers.

**Files to read in full before writing any code:**

1. `models/feed_card.py` (161 lines) — find the `FeedCardData` dataclass. Note the existing `metadata: dict = field(default_factory=dict)` pattern, `to_dict()` method, `from_dict()` classmethod. The new `seq_num` field follows the same pattern.
2. `ui/handlers/feed_handler.py` (977 lines) — find:
   - `__init__` (where the new `_project_seq` counter goes, near other `_project_*` dicts)
   - `add_card()` (where the seq_num is assigned, after the existing `card_data.card_id = card_id` line)
   - `on_project_opened()` (where the migration happens, in `_load_and_render` after loading cards)
   - `clear_project()` (where the counter is cleaned up, near other `self._project_*` cleanups)
3. `ui/views/feed_card.py` (581 lines) — find `_make_feed_card_header()` (the function that builds the card header). The seq badge goes in the header.
4. `ui/styles.py` (1185 lines) — find the existing feed-card CSS block. The new `.feed-card-seq` CSS goes there.
5. `tests/test_feed_card.py` (already modified in Phase 1, has new test patterns to follow)
6. `tests/test_feed_handler.py` (already modified in Phase 2, has new test patterns to follow)
7. `docs/ARCHITECTURE.md` Section 3.22a, 3.22b, 3.22c (public API reference)

**Output a discovery block before writing code** (per steelFramedCodeWriter Step 0).

---

## The feature

**User complaint #3:** "No visible card ID number — impossible to tell at a glance which cards have been reviewed vs. which are new."

**What we're building:** Every card gets a sequential display number (`#1`, `#2`, `#3`, ...). The number is per-project, persists in `feed.json`, and survives app restart. After Phase 3, the first run on an existing project will:
1. Load all existing cards
2. Assign seq_nums to cards that don't have one (in order of creation timestamp)
3. Rewrite `feed.json` with the new seq_nums

---

## Edits

### Edit 1: `models/feed_card.py` — Add `seq_num: int | None = None` field

**a) Add the field** to the `FeedCardData` dataclass, after the existing `metadata: dict = field(default_factory=dict)` field:

```python
    seq_num: int | None = None  # Sequential display number (per project)
```

**b) Update `to_dict()`** to include `seq_num`. Find the `to_dict()` method (search for `def to_dict`) and add `"seq_num": self.seq_num,` to the returned dict (near the other field entries).

**c) Update `from_dict()`** to read `seq_num`. Find the `from_dict()` classmethod and add `seq_num=data.get("seq_num"),` to the `cls(...)` call.

**Use the EXACT code from spec section 2.5** for the field signature and serialization. Note: if `to_dict()` has a special handling for nested snapshot metadata (look for `_serialize_metadata_with_snapshot`), the seq_num goes in the OUTER dict, not in metadata.

### Edit 2: `ui/handlers/feed_handler.py` — Add `_project_seq` counter

**a) Add new state in `__init__`** — find the `__init__` method and look for where other `self._project_*` dicts are initialized. Add:

```python
        # Per-project sequence counter for display numbers (Phase 3)
        self._project_seq: dict[str, int] = {}
```

**b) Assign seq_num in `add_card()`** — find the `add_card()` method and locate where `card_data.card_id = card_id` is set. AFTER that line, add the seq_num assignment:

```python
        # Assign sequence number (Phase 3)
        proj = card_data.project_name
        if proj not in self._project_seq:
            self._project_seq[proj] = 0
        self._project_seq[proj] += 1
        card_data.seq_num = self._project_seq[proj]
```

**c) Reconstruct counter in `on_project_opened()`** — find the `_load_and_render()` function (called by `on_project_opened`). AFTER cards are loaded, BEFORE the per-card widget rendering loop, add the migration code:

```python
            # Migrate old cards: assign seq_nums to cards with seq_num=None,
            # in order of creation timestamp. This ensures every project gets
            # a clean sequence from #1 on first load after the seq_num field
            # is added. Without this migration, old projects would show a mix
            # of cards with seq badges and cards without, which is confusing.
            cards_sorted_by_timestamp = sorted(cards, key=lambda c: c.timestamp)
            next_seq = 1
            for card in cards_sorted_by_timestamp:
                if card.seq_num is None:
                    card.seq_num = next_seq
                next_seq = max(next_seq, card.seq_num + 1)

            # Rebuild sequence counter from loaded cards (now all have seq_num)
            max_seq = max((card.seq_num for card in cards if card.seq_num), default=0)
            self._project_seq[project_name] = max_seq
```

**d) Clean up counter in `clear_project()`** — find the `clear_project()` method and add:

```python
        self._project_seq.pop(project_name, None)
```

(Add it near other `self._project_*` cleanups like `self._project_paths.pop(...)`.)

### Edit 3: `ui/views/feed_card.py` — Display seq badge in header

Find `_make_feed_card_header()` (search for the function name). Find where the title label is added to the header. Insert the seq badge BEFORE the title:

```python
    # Sequence number badge (if assigned) (Phase 3)
    if card_data.seq_num is not None:
        seq_label = Gtk.Label(label=f"#{card_data.seq_num}")
        seq_label.add_css_class("feed-card-seq")
        header.append(seq_label)
```

**Important:** The seq badge goes first (left), then the title (expand), then any copy button. Use `header.append(seq_label)` to add to the existing header box.

### Edit 4: `ui/styles.py` — Add `.feed-card-seq` CSS

Find the existing feed-card CSS block (search for `.feed-card-audit` — the new CSS goes AFTER that block). Add:

```css
/* Sequence number badge */
.feed-card-seq {
    background: rgba(99, 102, 241, 0.3);
    color: #c7d2fe;
    border-radius: 4px;
    padding: 1px 6px;
    font-size: 10px;
    font-weight: bold;
    min-width: 20px;
    text-align: center;
}
```

The colors (`#6366f1` indigo family) match the existing palette — no new colors introduced.

### Edit 5: `tests/test_feed_card.py` and `tests/test_feed_handler.py` — Add Phase 3 tests

Add the `TestSeqNum` test class from spec section 9 (Phase 3 Tests). The tests cover:
- `seq_num` is assigned in `add_card()` and increments per project
- `seq_num` is per-project (not global)
- `seq_num` persists through `to_dict()` and `from_dict()`
- `seq_num` is reconstructed on project open from loaded cards
- Cards loaded without `seq_num` (old format) get `None`, not auto-assigned at the data layer (the migration in the handler assigns them)

Split the tests between the two test files:
- `tests/test_feed_card.py`: serialization tests (seq_num round-trips through to_dict/from_dict)
- `tests/test_feed_handler.py`: handler tests (seq_num assigned in add_card, per-project, reconstructed on project open)

**Total: 5 edits in 4 files. ~30 lines of production code + ~100-150 lines of test code.**

---

## Rules

- Use the `prompts/steelFramedCodeWriter.md` prompt
- Read every file in full before editing
- Anchor edits to identifiers, NOT line numbers
- Scope is exactly the 5 edits above. Do NOT touch any other file.
- Do NOT silently expand scope. If you find a related issue, note it in the COMPLETENESS checklist as "Related issue found, not fixed in this phase" and stop.
- Do NOT touch the ARCHITECTURE.md doc updates — those come after all phases ship.
- Do NOT touch Phase 4 or Phase 5 work (smart scroll, batch accept). Strictly Phase 3 only.
- Do NOT refactor existing serialization code beyond adding the seq_num field. If `to_dict()` has a snapshot handling pattern, follow it.
- The migration is one-way: once a card has `seq_num=N`, it stays. Do NOT add code to "rebalance" seq_nums.
- Do NOT add a global counter (e.g., `self._global_seq`). seq_num is strictly per-project.

## Verification commands to run (in order)

1. **`FeedCardData.seq_num` field exists:**
   ```bash
   cd /path/to/projects/crabcakes && python3 -c "
   from models.feed_card import FeedCardData
   from dataclasses import fields
   field_names = [f.name for f in fields(FeedCardData)]
   assert 'seq_num' in field_names, 'seq_num field missing'
   print('seq_num field present')
   "
   ```
   Expect: `seq_num field present`

2. **`_project_seq` counter exists in FeedHandler:**
   ```bash
   cd /path/to/projects/crabcakes && grep -n "self._project_seq" ui/handlers/feed_handler.py
   ```
   Expect: ≥ 4 matches (init, add_card, project open, clear_project)

3. **Seq badge CSS was added:**
   ```bash
   cd /path/to/projects/crabcakes && grep -c "feed-card-seq" ui/styles.py
   ```
   Expect: ≥ 1 (the CSS definition; note the search term also matches the view code that uses the class, so ≥ 2 is fine)

4. **Seq badge display in header:**
   ```bash
   cd /path/to/projects/crabcakes && grep -n "feed-card-seq" ui/views/feed_card.py
   ```
   Expect: ≥ 1 match in `_make_feed_card_header` (the `seq_label.add_css_class("feed-card-seq")` line)

5. **Serialization round-trip:**
   ```bash
   cd /path/to/projects/crabcakes && python3 -c "
   from models.feed_card import FeedCardData
   from datetime import datetime, timezone
   card = FeedCardData(
       card_type='diff', source='crabwatch', title='t', body='b', author='a',
       timestamp=datetime.now(timezone.utc), project_name='p', seq_num=42,
   )
   d = card.to_dict()
   assert d.get('seq_num') == 42, f'to_dict missing seq_num: {d}'
   c2 = FeedCardData.from_dict(d)
   assert c2.seq_num == 42, f'from_dict lost seq_num: {c2.seq_num}'
   print('Serialization round-trip OK')
   "
   ```
   Expect: `Serialization round-trip OK`

6. **New tests pass:**
   ```bash
   cd /path/to/projects/crabcakes && python3 -m pytest tests/test_feed_card.py tests/test_feed_handler.py -v
   ```
   Expect: all existing tests + new TestSeqNum tests pass

7. **Full test suite (sanity — should be no new failures):**
   ```bash
   cd /path/to/projects/crabcakes && python3 -m pytest -x -q
   ```
   Expect: ≥ 1694 passed (Phase 1+2 baseline) + new Phase 3 tests, 1 skipped, 4 warnings

8. **No accidental scope creep:**
   ```bash
   cd /path/to/projects/crabcakes && git diff HEAD --stat | grep -v "Phase 1\|Phase 2"
   ```
   Expect: only Phase 3 changes are NEW in this commit. Phase 1+2 changes from prior phases are present but were not re-modified.

## Report

When done, send back a completion report with:
- Files changed with line numbers (of the actual edits, not the spec's claimed line numbers)
- Output of all 8 verification commands
- Full pytest output for the test_feed_card.py and test_feed_handler.py runs
- COMPLETENESS checklist (per steelFramedCodeWriter Step 6.5)
- Any related issues found (flagged, not silently fixed)

**Required word marker for /ask acknowledgment: "please write"** — include it in your response so the channel knows your acknowledgment is canonical.

**Do not skip the COMPLETENESS checklist.** Include every edit with `[x]` or `[NOT DONE] WHY` and paste the evidence. A response without the literal `**COMPLETENESS:** [x]` block is a missing deliverable.

**LESSON FROM PHASE 1:** The previous /ask for Phase 1 included a clear "do NOT touch ARCHITECTURE.md or out-of-scope files" instruction. QTR still modified ARCHITECTURE.md and added seq/batch CSS in styles.py. To prevent this, **strictly limit your diff to the 5 files in scope: `models/feed_card.py`, `ui/handlers/feed_handler.py`, `ui/views/feed_card.py`, `ui/styles.py`, `tests/test_feed_card.py`, `tests/test_feed_handler.py`**. If you find yourself adding CSS for batch-accept or anything else from a different phase, STOP — that is a different phase. Do not pre-emptively add code for future phases.
