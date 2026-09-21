# Phase 1 — File Tree Enhancements: Fix Crash + Leaf Wiring

**Target:** Restore a working project-open (currently CRASHES) + add the
 GObject properties, module helpers, CSS classes, and icon/color binding that
later phases depend on. **Do NOT add sort/filter, dropdowns, or new factories
in this phase — that is Phase 2/3.**

**Spec of record:** `docs/specs/SPEC-FILE-TREE-ENHANCEMENTS.md`
Read it before editing. BUG numbers below refer to that spec's §9.

---

## Context — the app is currently broken

A previous build created the leaf modules but never wired them:
- `utils/projects.py::scan_directory` returns 5-tuples
  `(name, full_path, is_dir, size_bytes, mtime_ns)`
- `ui/views/file_tree.py` still unpacks 3-tuples in THREE places

Result: **ValueError: too many values to unpack on project open.**

## Files to edit

1. `ui/views/file_tree.py` (MODIFY)
2. `tests/test_projects.py` (FIX — 2 broken tests)
3. `tests/test_git_ops.py` (EXTEND — status_porcelain tests)
4. `ui/styles.py` (EXTEND — CSS classes only)

## Tasks

### Task 1 — Fix 3-tuple → 5-tuple unpacking (3 sites in file_tree.py)

Use `grep -n "for entry_name, full_path, is_dir in" ui/views/file_tree.py` to
find all three. They are in:

- `_show_tree` (~line 643): the root-population loop.
- `_expand_directory`'s `_do()` closure (~line 1455): the error fallback
  `entries = [(f"[error: ...]", "", False)]` — change to a 5-tuple with two
  trailing zeros.
- `_on_directory_loaded` (~line 1497): the child-insert loop.

For `_show_tree` and `_on_directory_loaded`, update the loop to unpack 5
values and pass them through to `FileTreeRow(...)`. For `_show_tree` use the
new properties (see Task 3). For `_on_directory_loaded` do the same. Both
must compute `icon = get_icon_for_path(full_path, is_dir)` and set
`icon_name` / `icon_color_class`.

Wrap the `scan_directory` call so that an exception produces a 5-tuple error
entry: `[(f"[error: {type(e).__name__}: {e}]", "", False, 0, 0)]`.

Compute mtime seconds via INTEGER division `mtime_ns // 1_000_000_000`
(BUG #14). Do NOT use `int(mtime_ns / 1e9)`.

### Task 2 — Fix the 2 broken tests in tests/test_projects.py

Two tests still unpack 3-tuples:
- `TestScanDirectory::test_skips_pycache` → `names = [n for n, _, _ in result]`
- `TestScanDirectory::test_skips_dotfiles` → same pattern

Change both to `for n, _, _, _, _ in result`. (The third test
`test_returns_tuples_with_three_elements` already asserts `len(item) == 5` —
leave it; only its docstring mentions "three" — update the docstring/name to
say "five" for accuracy.)

### Task 3 — Add 9 new GObject properties to FileTreeRow (§3.4.1)

Append after `history_loaded` in the class body AND extend `__init__` to
accept and assign them. The 9 properties (all default-valued):

```python
file_size = GObject.Property(type=int, default=0)
file_size_display = GObject.Property(type=str, default="—")
modified_time = GObject.Property(type=int, default=0)
modified_display = GObject.Property(type=str, default="—")
git_status = GObject.Property(type=str, default="")
git_status_display = GObject.Property(type=str, default="")
mime_type = GObject.Property(type=str, default="")
icon_name = GObject.Property(type=str, default="text-x-generic-symbolic")
icon_color_class = GObject.Property(type=str, default="file-icon-default")
parent_full_path = GObject.Property(type=str, default="")  # BUG #26
```

All `__init__` kwargs are optional with those defaults so existing callers
that pass only the original 12 keep working. Do NOT remove or reorder the
existing 12 parameters.

### Task 4 — Add module-level helper functions to file_tree.py (§3.4.15)

Add these as PUBLIC functions (no leading underscore — BUG #15) at module
scope, near the top of the file after the imports:

```python
def format_size(bytes_: int) -> str:
    """Human-readable file size. Float division for fractional KB/MB."""
    if bytes_ <= 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    val = float(bytes_)
    for unit in units:
        if val < 1024:
            if unit == "B":
                return f"{int(val)} B"
            return f"{val:.1f} {unit}".replace(".0 ", " ")
        val /= 1024.0
    return f"{val:.1f} PB"

def format_mtime(mtime_ns: int) -> str:
    """Relative time from nanosecond timestamp. Integer division (BUG #14)."""
    if mtime_ns <= 0:
        return "—"
    from datetime import datetime
    dt = datetime.fromtimestamp(mtime_ns // 1_000_000_000)
    now = datetime.now()
    diff = now - dt
    if diff.days == 0:
        if diff.seconds < 60:
            return "just now"
        if diff.seconds < 3600:
            return f"{diff.seconds // 60}m ago"
        return f"{diff.seconds // 3600}h ago"
    if diff.days == 1:
        return "yesterday"
    if diff.days < 7:
        return f"{diff.days}d ago"
    if diff.days < 30:
        return f"{diff.days // 7}w ago"
    return dt.strftime("%b %d")

def git_status_to_display(status_code: str) -> str:
    """2-char porcelain → single-char badge. Index col has precedence."""
    if not status_code or len(status_code) < 2:
        return ""
    char = status_code[0] if status_code[0] != ' ' else status_code[1]
    return {'M':'M','A':'A','D':'D','R':'R','C':'C','?':'?','!':'!'}.get(char, "")
```

Do NOT add `guess_mime` here — it already lives in `utils/file_icons.py`
as `guess_mime`. Import it: `from utils.file_icons import get_icon_for_path, guess_mime`.

### Task 5 — Update FileTreeRowWidget for icon_name + color (§3.4.2)

Replace the existing `set_icon(self, is_dir, is_drawer)` with a version that
takes `icon_name`:

```python
def set_icon(self, icon_name: str, is_dir: bool, is_drawer: bool) -> None:
    if is_drawer:
        self._icon.set_visible(False)
    else:
        self._icon.set_visible(True)
        self._icon.set_from_icon_name(icon_name)
```

Add a new method:

```python
def set_icon_color(self, color_class: str) -> None:
    """Remove previous file-icon-* class, add the new one."""
    for cls in list(self._icon.get_css_classes()):
        if cls.startswith("file-icon-"):
            self._icon.remove_css_class(cls)
    if color_class:
        self._icon.add_css_class(color_class)
```

### Task 6 — Update FileTreeFactory._on_bind (§3.4.4)

In `_on_bind`, after the existing `widget.set_label(...)`, replace
`widget.set_icon(row.props.is_dir, row.props.is_drawer)` with:

```python
widget.set_icon(row.props.icon_name, row.props.is_dir, row.props.is_drawer)
widget.set_icon_color(row.props.icon_color_class)
```

Keep the rest of `_on_bind` (drawer visibility logic, expander wiring) exactly
as-is.

### Task 7 — Populate new properties in _show_tree (§3.4.6 — metadata ONLY)

For each root entry, build the row with the new metadata. For Phase 1 use a
STUB git status — pass `status_map = {}` (the handler wiring comes in Phase 4).
Do NOT add the sort dropdown, sort/filter model, or column layout changes yet
— those are Phase 2/3. Just populate the new row properties so the icon
binding has data.

```python
icon = get_icon_for_path(full_path, is_dir)
row = FileTreeRow(
    display_name=entry_name,
    full_path=full_path,
    is_dir=is_dir,
    depth=0,
    has_children=is_dir,
    expanded=False,
    file_size=0 if is_dir else size_bytes,
    file_size_display=format_size(size_bytes) if not is_dir else "—",
    modified_time=mtime_ns // 1_000_000_000 if mtime_ns else 0,
    modified_display=format_mtime(mtime_ns) if mtime_ns else "—",
    git_status="",
    git_status_display="",
    mime_type=guess_mime(full_path),
    icon_name=icon.icon_name,
    icon_color_class=icon.color_class,
)
```

Do the same in `_on_directory_loaded` for child rows.

### Task 8 — CSS classes (ui/styles.py)

Append to `APP_CSS` (before the closing `"""`):

```css
.file-tree-status-badge { padding: 1px 4px; border-radius: 3px; font-size: 10px; font-weight: 600; min-width: 18px; text-align: center; }
.file-tree-status-modified { background: #f59e0b; color: #1e1e1e; }
.file-tree-status-staged { background: #22c55e; color: #1e1e1e; }
.file-tree-status-untracked { background: #6366f1; color: #fff; }
.file-tree-status-deleted { background: #ef4444; color: #fff; }
.file-tree-status-renamed { background: #a855f7; color: #fff; }
.file-tree-status-ignored { background: #6b7280; color: #fff; }
.file-tree-size-column { padding-right: 8px; font-size: 12px; color: #a0a0b0; }
.file-tree-modified-column { padding-right: 8px; font-size: 12px; color: #a0a0b0; }
.file-icon-python { color: #f0c674; }
.file-icon-js { color: #e5c07b; }
.file-icon-ts { color: #61afef; }
.file-icon-json { color: #e5c07b; }
.file-icon-yaml { color: #e06c75; }
.file-icon-md { color: #98c379; }
.file-icon-rust { color: #e06c75; }
.file-icon-go { color: #61afef; }
.file-icon-cpp { color: #61afef; }
.file-icon-c { color: #61afef; }
.file-icon-sh { color: #98c379; }
.file-icon-png, .file-icon-jpg, .file-icon-gif { color: #c678dd; }
.file-icon-pdf { color: #e06c75; }
.file-icon-zip, .file-icon-tar, .file-icon-gz { color: #e5c07b; }
.file-icon-folder { color: #f0c674; }
.file-icon-default { color: #6b6b7a; }
.file-tree-sort-dropdown { min-width: 140px; margin-left: 8px; }
```

### Task 9 — status_porcelain tests (tests/test_git_ops.py)

Add a `TestStatusPorcelain` class. Cover at minimum:
- empty/non-repo path → returns `{}`
- a tmp_path repo with one modified file → contains that path
- a rename line `R  old -> new` → key is `new` (BUG #5)
- a copy line `C  old -> new` → key is `new` (BUG #17)
- a too-short line (< 4 chars) is skipped (BUG #4)
- both status positions checked (BUG #25) — feed a worktree-rename ` R old -> new`

Use `gitpython.Repo.init(str(tmp_path))` to set up. Write files, `git add -A`,
commit, then modify to produce `M`/`A`/etc. For rename, use
`repo.git.mv("old.txt", "new.txt")`.

## What NOT to do in Phase 1

- Do NOT add the sort dropdown widget
- Do NOT add SortListModel / FilterListModel / CustomSorter / CustomFilter
- Do NOT add the Status/Size/Modified factories or the 4-column layout
- Do NOT create `file_tree_handler.py` or modify `left_panel.py`
- Do NOT change `scan_directory`'s signature (already correct)
- Do NOT modify `GitResult` (already correct — status_porcelain returns dict)
- Do NOT update ARCHITECTURE.md yet (Phase 4)

## Verification (run ALL of these)

```bash
cd /path/to/projects/crabcakes

# 1. Unit tests — all green
python3 -m pytest tests/test_file_icons.py tests/test_projects.py \
  tests/test_git_ops.py -q

# 2. Module helpers importable
python3 -c "from ui.views.file_tree import format_size, format_mtime, git_status_to_display; print(format_size(1500), format_mtime(0), git_status_to_display('M '))"

# 3. FileTreeRow has 22 properties
python3 -c "
import gi; gi.require_version('Gtk','4.0')
from gi.repository import GObject, Gio
import sys; sys.path.insert(0,'.')
# Bypass GTK windowing by listing the class properties directly
from ui.views.file_tree import FileTreeRow
r = FileTreeRow()
props = [GObject.properties(r, p).name for p in dir(r.props)]
for needed in ['file_size','file_size_display','modified_time','modified_display','git_status','git_status_display','mime_type','icon_name','icon_color_class','parent_full_path']:
    assert hasattr(r.props, needed), f'missing {needed}'
print('all 9 new props present')
"

# 4. status_porcelain works on this repo
python3 -c "from utils.git_ops import status_porcelain; r = status_porcelain('.'); print(type(r), len(r))"
```

## Report back with

1. `git diff --stat` output
2. Output of all 4 verification commands above
3. The COMPLETENESS checklist (every Task 1–9 marked done/skipped with a one-line note)
