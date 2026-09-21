# Phase 1b Instructions: ui/views/diff_card.py — Promote Helper + Extract render_diff_hunks()

**Spec:** SPEC-ONE-CLICK-DIFF.md (§2.2)
**Phase:** 2 of 3 (git_ops → diff_card → diff_viewer → wiring)
**Target files:** 2 files max (diff_card.py + test_diff_parser.py or test_diff_card.py)

---

## Changes Required

### 1. Promote `_get_lang_from_path` → `get_lang_from_path` (public)

**Current:** Line 14 (private, underscore prefix)
**Action:** Rename to `get_lang_from_path` (no underscore)
**Update internal callers:** Line 252 (inside `build_file_diff_card`) — only caller in codebase

```bash
# Verify no other callers before rename:
grep -rn "_get_lang_from_path" ui/
# Should return only diff_card.py internal references
```

### 2. Extract `render_diff_hunks()` — NEW PUBLIC FUNCTION

**Location:** Insert BEFORE `build_file_diff_card()` (around line 160)

```python
def render_diff_hunks(hunks: list[DiffHunk], lang: str | None = None) -> Gtk.Widget:
    """Render diff hunks as a Gtk.Box. Shared by diff_card and diff_viewer.

    Pure renderer — does NOT handle binary files. Caller must check
    FileDiff.is_binary before calling and render the "Binary file — not shown"
    label itself.

    Args:
        hunks: List of DiffHunk objects from parse_diff().
        lang: Language string for syntax highlighting (from get_lang_from_path).

    Returns:
        Gtk.Box containing rendered hunks.
    """
    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    for hunk in hunks:
        vbox.append(_build_hunk_view(hunk, lang))
    return vbox
```

### 3. Simplify `build_file_diff_card()` to Use `render_diff_hunks()`

**Current lines 248-254:**
```python
    if file_diff.is_binary:
        bin_lbl = Gtk.Label(label="  Binary file — not shown")
        bin_lbl.add_css_class("diff-line-context")
        body_box.append(bin_lbl)
    else:
        lang = get_lang_from_path(file_diff.display_path)
        for hunk in file_diff.hunks:
            body_box.append(_build_hunk_view(hunk, lang))
```

**Replace with:**
```python
    if file_diff.is_binary:
        bin_lbl = Gtk.Label(label="  Binary file — not shown")
        bin_lbl.add_css_class("diff-line-context")
        body_box.append(bin_lbl)
    else:
        lang = get_lang_from_path(file_diff.display_path)
        body_box.append(render_diff_hunks(file_diff.hunks, lang))
```

**Binary handling stays in `build_file_diff_card()`** — `render_diff_hunks()` is never called for binary files (H8 fix).

### 4. Write Tests

**File:** `tests/test_diff_card.py` (or `tests/test_diff_parser.py` if that's where diff_card tests live)

Test:
- `test_render_diff_hunks` — renders hunks correctly, returns Gtk.Box
- `test_render_diff_hunks_empty` — empty list returns empty Box
- `test_get_lang_from_path` — various extensions map to correct langs
- Verify existing diff card tests still pass (no regression)

---

## Rules (steelFramedCodeWriter.md)

- Read `ui/views/diff_card.py` in full before editing
- Read test file in full before editing
- Verify every claim with evidence (pytest output, grep, wc -l)
- No fabricated APIs — use existing `_build_hunk_view`, `_build_diff_line`, `DiffHunk`, `get_lang_from_path`
- Hard part first: extract function, then simplify caller, then tests
- Wire it or delete it — no stubs

---

## Deliverable Expectations

Report back with:

```
Files changed:
- ui/views/diff_card.py:XX-YY (promoted get_lang_from_path)
- ui/views/diff_card.py:AA-BB (added render_diff_hunks)
- ui/views/diff_card.py:CC-DD (simplified build_file_diff_card)
- tests/test_diff_card.py:EE-FF (new tests)

Verification:
pytest tests/test_diff_card.py -v
→ [paste full output]
grep -n "render_diff_hunks\|get_lang_from_path" ui/views/diff_card.py
→ [paste output]

COMPLETENESS:
- [x/not done] Edit 1: _get_lang_from_path → get_lang_from_path — evidence: grep
- [x/not done] Edit 2: render_diff_hunks extracted — evidence: grep + pytest
- [x/not done] Edit 3: build_file_diff_card uses render_diff_hunks — evidence: grep
- [x/not done] Edit 4: tests for new public API — evidence: pytest output
- [x/not done] Regression: all existing diff_card tests pass — evidence: pytest output
```

---

## Word Marker

**please write**