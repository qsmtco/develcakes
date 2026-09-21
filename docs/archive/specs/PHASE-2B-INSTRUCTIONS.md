# Phase 2b Instructions: Wire _on_project_selected + Revert Handler

**Spec:** SPEC-ONE-CLICK-DIFF.md (§2.4, §2.7)
**Phase:** 2 of 3 (wiring integration)
**Target files:** 2 files (ui/window.py, ui/handlers/review_handler.py)

---

## Changes Required

### 1. `ui/handlers/review_handler.py` — Add `revert_file_to_sha()` method

**Insert after** `reject_file()` (around line 453).

```python
def revert_file_to_sha(self, project_name: str, file_path: str, target_sha: str) -> None:
    """Revert a single file to its state at an arbitrary commit SHA.

    Unlike reject_file() (which requires an active review session and reverts
    to checkpoint_sha), this method works on any commit.

    M17 fix: Requires an active review session for safety — reverting outside
    a review session risks losing untracked work without audit trail.

    H6 fix: Uses state.project_path (per-project lookup), not
    get_active_project_path() which could return a different project's path.

    SHA validation is handled by git_ops.checkout_paths() via _VALID_SHA_RE.
    """
    # M17 fix: require active review session
    state = self._states.get(project_name)
    if state is None or not state.is_active():
        session_key = f"project:{project_name}"
        self._GLib.idle_add(lambda sk=session_key: self._on_display_text(
            sk, "⚠ Revert requires an active review session. Run /review first."))
        return

    # H6 fix: use state.project_path, not get_active_project_path()
    project_path = state.project_path

    session_key = f"project:{project_name}"

    def _do():
        result = git_ops.checkout_paths(project_path, target_sha, [file_path])
        if not result.success:
            self._GLib.idle_add(lambda sk=session_key: self._on_display_text(
                sk, f"⚠ Failed to revert {file_path}: {result.error}"))
            return

        self._GLib.idle_add(lambda sk=session_key: self._on_display_text(
            sk, f"↩ {file_path} reverted to {target_sha[:7]}"))

    threading.Thread(target=_do, daemon=True).start()
```

**Verify:**
- `self._states` dict is ReviewHandler, has `_states`, `_GLib`, `_on_display_text`
- `git_ops` imported at top
- `threading` imported at top (line 3)

---

### 2. `ui/window.py` — Wire `_on_project_selected`

**Replace** the no-op at line 803 with:

```python
def _on_project_selected(self, path):
    """Handle file tree selection — open diff viewer for the clicked file."""
    project_path = self._project_handler.get_active_project_path()
    if project_path is None:
        return

    project_name = self._project_handler.get_active_project_name()
    if project_name is None:
        return

    import os
    rel_path = os.path.relpath(path, project_path)

    # M11 fix: reject paths that escape the project root
    if rel_path.startswith(".."):
        return

    review_state = self._review_handler.get_state(project_name)
    checkpoint_sha = review_state.checkpoint_sha if review_state and review_state.is_active() else None

    from ui.views.diff_viewer import DiffViewer

    # M22 fix: session_key captured in closure, not passed through DiffViewer
    def on_revert(file_path: str, target_sha: str):
        self._review_handler.revert_file_to_sha(project_name, file_path, target_sha)

    viewer = DiffViewer(
        file_path=rel_path,
        project_path=project_path,
        checkpoint_sha=checkpoint_sha,
        on_back=lambda: self._main_content.hide_diff_viewer(),
        on_revert=on_revert,
    )
    self._main_content.show_diff_viewer(viewer)
```

**Verify:**
- `self._project_handler.get_active_project_path()` exists
- `self._project_handler.get_active_project_name()` exists
- `self._review_handler.get_state(project_name)` exists
- `review_state.checkpoint_sha`, `review_state.is_active()` exist
- `self._main_content.show_diff_viewer()` exists (Phase 2a)
- `DiffViewer` imported from `ui.views.diff_viewer`

---

## Rules (steelFramedCodeWriter.md)

- Read both files in full before editing
- Verify every method/attribute exists before using
- Follow existing patterns in each file
- No fabricated APIs

---

## Deliverable Expectations

```
Files changed:
- ui/handlers/review_handler.py:XX-YY (revert_file_to_sha added)
- ui/window.py:XX-YY (_on_project_selected replaced)

Verification:
grep -n "def revert_file_to_sha" ui/handlers/review_handler.py
→ [paste output]
grep -n "def _on_project_selected" ui/window.py
→ [paste output]
python3 -c "from ui.handlers.review_handler import ReviewHandler; from ui.window import MainWindow; print('imports ok')"
→ imports ok
```

---

## Word Marker

**please write**