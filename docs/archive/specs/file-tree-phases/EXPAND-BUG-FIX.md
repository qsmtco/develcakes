# FIX: Sort comparator breaks folder expansion (CRITICAL)

## Problem
Clicking a folder: children appear at the bottom of the tree, separated from parent. The expander arrow may not visually update. Root cause: the comparator sorts by depth globally — ALL depth-0 items before ALL depth-1 items — instead of keeping children grouped under their parent.

## Fix: Hierarchical sort key

Replace the depth-based comparator with a parent-path-based one. Each row's primary sort key is its "family path" — the path of its parent directory (or empty for root items). This keeps siblings together under their parent.

### File: `ui/views/file_tree.py`

#### Change 1: Set `parent_full_path` on ALL child rows (not just drawers)

In `_on_directory_loaded` (~line 1975), the child `FileTreeRow(...)` construction needs `parent_full_path=parent_row.props.full_path`:

```python
            child = FileTreeRow(
                display_name=entry_name,
                full_path=full_path,
                is_dir=is_dir,
                depth=parent_depth + 1,
                has_children=is_dir,
                expanded=False,
                parent_full_path=parent_row.props.full_path,   # ADD THIS
                file_size=0 if is_dir else size_bytes,
                ...
            )
```

#### Change 2: Set `parent_full_path` on root-level rows in `_show_tree`

In `_show_tree` (~line 1075), root-level rows need `parent_full_path=""` (empty = root family):

The root rows already default to `parent_full_path=""` (the GObject property default), so this is already correct. No change needed here.

#### Change 3: Rewrite `_build_sorter` with parent-path grouping

Replace the entire `_build_sorter` method with this version. The key insight: sort by `(parent_full_path, depth, group_rank, sort_value)` — this keeps all children of the same parent together, then sorts within that group.

```python
    @staticmethod
    def _build_sorter(sort_mode: str) -> Gtk.Sorter:
        """Build comparator-based sorter that preserves tree hierarchy.

        Sort is parent-aware: children group under their parent directory.
        Uses parent_full_path as the primary grouping key so siblings stay
        together. Drawers sort immediately after their parent file.
        """

        import os as _os

        def _family_key(row):
            """Primary sort key: groups siblings under their parent.

            - Root items (depth 0): key = "" (empty string)
            - Children of a dir: key = parent dir's full_path
            - Drawers: key = parent file's full_path (same as the file they belong to)
            """
            if row.props.is_drawer:
                return row.props.parent_full_path or ""
            # For non-drawer rows, parent_full_path is set to the parent DIR's path
            # for directory children, and "" for root-level items.
            return row.props.parent_full_path or ""

        def _group_rank(row):
            """0=dirs, 1=files, 2=drawers. Within a family, dirs first, then files, then drawers."""
            if row.props.is_dir:
                return 0
            if row.props.is_drawer:
                return 2
            return 1

        def _sort_name(row):
            """The name used for alphabetical sorting within a group."""
            if row.props.is_drawer:
                # Drawer uses parent file's basename so it lands at the same spot
                return _os.path.basename(row.props.parent_full_path or "").casefold()
            return (row.props.display_name or "").casefold()

        def cmp(a, b, _ud=None):
            # Rule 1: Family grouping — children stay under their parent.
            # Compare parent_full_path first. This keeps all siblings together.
            fa, fb = _family_key(a), _family_key(b)
            if fa != fb:
                return -1 if fa < fb else 1

            # Rule 2: Within the same family, sort by group rank (dirs < files < drawers)
            ga, gb = _group_rank(a), _group_rank(b)
            if ga != gb:
                return -1 if ga < gb else 1

            # Rule 3: Apply the sort mode within the group
            name_a, name_b = _sort_name(a), _sort_name(b)

            if sort_mode in ("name_asc", "name_desc"):
                if name_a != name_b:
                    if sort_mode == "name_asc":
                        return -1 if name_a < name_b else 1
                    else:
                        return 1 if name_a < name_b else -1
                return 0

            if sort_mode in ("modified_asc", "modified_desc"):
                ta, tb = a.props.modified_time, b.props.modified_time
                if ta != tb:
                    if sort_mode == "modified_asc":
                        return -1 if ta < tb else 1
                    else:
                        return 1 if ta < tb else -1
                return -1 if name_a < name_b else (1 if name_a > name_b else 0)

            if sort_mode in ("size_asc", "size_desc"):
                sa, sb = a.props.file_size, b.props.file_size
                if sa != sb:
                    if sort_mode == "size_asc":
                        return -1 if sa < sb else 1
                    else:
                        return 1 if sa < sb else -1
                return -1 if name_a < name_b else (1 if name_a > name_b else 0)

            return -1 if name_a < name_b else (1 if name_a > name_b else 0)

        return Gtk.CustomSorter.new(cmp)
```

**Key difference from the old comparator:**
- OLD: Rule 1 compared `depth` — ALL depth-0 items before ALL depth-1 items globally
- NEW: Rule 1 compares `parent_full_path` — children of `/src` stay together, children of `/tests` stay together, root items stay together

This means:
- Root items (parent_full_path="") sort first as a group
- When you expand `/src`, its children (parent_full_path="/src") sort as a group right after `/src`
- When you expand `/tests`, its children (parent_full_path="/tests") sort as a group right after `/tests`

**Wait — there's a subtlety.** Root items have `parent_full_path=""`. The directory `/src` is a root item with `parent_full_path=""`. Its children have `parent_full_path="/src"`. Empty string `""` sorts before `"/src"` alphabetically. So root items come first (correct), then `/src`'s children (correct). But what about `/tests`'s children — they have `parent_full_path="/tests"` which sorts AFTER `"/src"`. If `/src` is expanded but `/tests` is not, the order would be:

```
"" family: src, tests, main.py     (root items, sorted)
"/src" family: file1.py, file2.py   (src's children)
```

But `/tests` (a root item with `parent_full_path=""`) sorts in the `""` family. Its children (if expanded) would sort in the `"/tests"` family, which comes AFTER `"/src"`. This is correct IF `/src` comes before `/tests` alphabetically. Since the `""` family sorts `src` before `tests`, and then `"/src"` family comes before `"/tests"` family, the tree renders correctly.

**BUT:** What if root has `zebra.py` (a file) and `alpha_dir` (expanded with children)? The `""` family sorts `alpha_dir` before `zebra.py` (dirs first). Then `"/alpha_dir"` family sorts next. Then... `zebra.py` is in the `""` family and already sorted. So the order is:
```
"" family: alpha_dir, zebra.py
"/alpha_dir" family: child1, child2
```

This means `zebra.py` appears BEFORE `alpha_dir`'s children — but `alpha_dir`'s children should appear right after `alpha_dir`, before `zebra.py`. **THIS IS WRONG.**

The family-key approach doesn't work for nested expansion because it doesn't preserve the parent's position relative to its siblings AND its children.

### Correct approach: Full path prefix as sort key

Each row's sort key should be its **full hierarchical position path** — the chain of ancestors. For a file `/src/sub/file.py`, the key is the list `["src", "sub", "file.py"]`. For `/src/main.py`, it's `["src", "main.py"]`. Comparing these lexicographically keeps the tree structure intact.

But we don't store ancestor chains. We do store `full_path` and `depth`. We can reconstruct the hierarchy position from `full_path` by splitting on `/`.

**Simpler correct approach:** Use `full_path` itself as the primary sort key (for tree structure), then apply the sort mode within sibling groups.

Actually, the SIMPLEST correct fix that preserves the visual tree: **don't use a global sorter at all.** Sort only happens at insertion time (in `_show_tree` and `_on_directory_loaded`), and the store maintains insertion order. Remove the SortListModel entirely and sort the data before inserting.

But that breaks the sort dropdown (user changes sort mode → needs re-sort).

### Correct approach that works with SortListModel:

Use a **path-based prefix key**: for each row, compute a sort key from its `full_path` that encodes the tree structure. The key is the directory portion of the path (the parent), which naturally groups children under their parent.

For the comparator, the primary key is NOT depth or parent_full_path — it's the **full ancestor path**. Since `full_path` already encodes this (`/src/sub/file.py`), we can use the parent directory of `full_path` as the family key, but we need to ensure that within each family, the parent directory itself sorts before its children.

The issue is: `/src` (the dir) has `full_path="/src"`, `parent_full_path=""`. Its children have `parent_full_path="/src"`. The dir `/src` is in family `""` but its children are in family `"/src"`. The dir needs to sort right before its children.

**Fix:** For directory rows, use the directory's OWN `full_path` as its family key (not its parent's). This way, the directory `/src` has family key `"/src"` — the same family as its children. Then within the `"/src"` family, the directory itself (group_rank=0) sorts before its children (group_rank=1).

```python
        def _family_key(row):
            """For dirs: their own full_path (so they group with their children).
            For files: their parent_full_path (the parent dir).
            For drawers: their parent_full_path (the parent file).
            For root items with no parent: empty string."""
            if row.props.is_dir:
                # A directory's family is itself + its children.
                # But we also need the dir to sort relative to its OWN siblings.
                # The dir's position among siblings is determined by its parent.
                # So: the dir's family key is its PARENT's path (same as its children's siblings).
                # Wait no...
```

This is getting circular. Let me think more carefully.

The fundamental issue: a flat SortListModel cannot represent a tree. The correct solution per the proposal research is Gtk.TreeListModel. But that's a major refactor.

**Pragmatic fix:** Remove the SortListModel. Sort children locally at insertion time (in `_on_directory_loaded` and `_show_tree`). The sort dropdown re-sorts by clearing and re-inserting in sorted order. This is simpler and correct.

## REVISED FIX PLAN

### Remove SortListModel, sort at insertion time

1. Remove `_init_sort_filter()` call and the SortListModel/FilterListModel chain
2. Sort children in `_on_directory_loaded` before inserting
3. Sort root items in `_show_tree` before inserting
4. Sort dropdown change → re-sort the entire store by rebuilding it (or by local sorts per depth group)
5. Keep FilterListModel for search (it doesn't reorder, just filters)

This is a significant change but it's the correct architecture for a tree in a flat list.
