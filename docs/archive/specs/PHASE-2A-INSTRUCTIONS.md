# Phase 2a Instructions: ui/views/main_content.py — Diff Viewer Slot

**Spec:** SPEC-ONE-CLICK-DIFF.md (§2.5)
**Phase:** 2 of 3 (wiring integration)
**Target file:** 1 file (ui/views/main_content.py)

---

## Changes Required

Add three methods to `MainContent` class (after `get_review_bar()`, around line 921):

### 1. `show_diff_viewer(viewer_widget: Gtk.Widget) -> None`

Inserts diff viewer above the chat notebook, below the review bar (if present). Follows `set_review_bar()` pattern exactly.

```python
def show_diff_viewer(self, viewer_widget: Gtk.Widget) -> None:
    """Insert diff viewer above the chat notebook, below the review bar.

    Follows the same insert/remove pattern as set_review_bar().
    The diff viewer sits between the review bar (if present) and the chat notebook.
    Chat notebook remains visible — PM can see both diff and chat.
    """
    # Remove existing diff viewer if any (M21 fix: dispose old viewer)
    self.hide_diff_viewer()

    self._diff_viewer = viewer_widget

    paned = self.get_first_child()
    if paned is None:
        return
    top_box = paned.get_start_child()
    if top_box is None:
        return

    # Insert after review bar (if present), before chat notebook
    review_bar = getattr(self, '_review_bar', None)
    if review_bar is not None:
        # L1 sanity: verify review_bar is actually in top_box
        if review_bar.get_parent() == top_box:
            top_box.insert_after(viewer_widget, review_bar)
        else:
            top_box.prepend(viewer_widget)
    else:
        top_box.prepend(viewer_widget)
```

### 2. `hide_diff_viewer() -> None`

Removes diff viewer, marks as disposed (M21 fix).

```python
def hide_diff_viewer(self) -> None:
    """Remove diff viewer from main_content if present.

    M21 fix: marks the old viewer as disposed before unparenting,
    so its background threads' idle_add callbacks become no-ops.
    """
    viewer = getattr(self, '_diff_viewer', None)
    if viewer is not None:
        # H3/M21 fix: mark disposed before removing from widget tree
        if hasattr(viewer, '_disposed'):
            viewer._disposed = True
        viewer.unparent()
        self._diff_viewer = None
```

### 3. `get_diff_viewer() -> Gtk.Widget | None`

```python
def get_diff_viewer(self) -> Gtk.Widget | None:
    """Return the current diff viewer widget, or None."""
    return getattr(self, '_diff_viewer', None)
```

---

## Rules (steelFramedCodeWriter.md)

- Read `ui/views/main_content.py` in full before editing
- Follow `set_review_bar()` pattern exactly (lines 884-913)
- Verify every claim with evidence (pytest output, grep, wc -l)
- No fabricated APIs — use existing `_review_bar`, `top_box`, `paned` pattern

---

## Deliverable Expectations

```
Files changed:
- ui/views/main_content.py:XX-YY (3 methods added)

Verification:
grep -n "def show_diff_viewer\|def hide_diff_viewer\|def get_diff_viewer" ui/views/main_content.py
→ [paste output]
wc -l ui/views/main_content.py
→ [paste output]

COMPLETENESS:
- [x] show_diff_viewer: insert_after pattern with review_bar check
- [x] hide_diff_viewer: dispose flag + unparent
- [x] get_diff_viewer: returns _diff_viewer
- [x] Follows set_review_bar() pattern exactly
```

---

## Word Marker

**please write**