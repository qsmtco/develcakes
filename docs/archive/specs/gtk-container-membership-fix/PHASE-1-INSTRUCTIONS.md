# Phase 1 of 4 — Create `utils/gtk_containers.py`

**Spec:** `docs/specs/SPEC-GTK-CONTAINER-MEMBERSHIP-FIX.md` (§3.1)
**Master prompt:** `prompts/steelFramedCodeWriter.md` — invoke it. Read it first.
**Scope:** ONE new file. Zero edits to existing files.

## Task

Create the file `utils/gtk_containers.py` with EXACTLY this content (this is the
spec-mandated implementation — copy it verbatim, including the docstring):

```python
"""
Utility functions for GTK container operations.

All functions in this module are pure GTK utilities — they depend only on
``gi.repository.Gtk`` and the Python standard library. No dependency on
``ui/``, ``agent/``, ``gateway/``, or ``models/``.
"""

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk


def is_in_container(widget: Gtk.Widget | None, container: Gtk.Container | None) -> bool:
    """
    Check if *widget* is a direct child of *container* using sibling walk.

    PyGObject does NOT wire Python's ``__contains__`` operator onto GTK
    containers. ``widget in gtk_box`` raises ``TypeError``. This function
    provides a safe alternative via ``Gtk.Widget.get_first_child()`` and
    ``Gtk.Widget.get_next_sibling()``.

    Args:
        widget: The widget to find (or None).
        container: The container to search (or None).

    Returns:
        True if *widget* is a direct child of *container*, False otherwise
        (including when either argument is None or the container is empty).
    """
    if widget is None or container is None:
        return False
    child = container.get_first_child()
    while child is not None:
        if child is widget:
            return True
        child = child.get_next_sibling()
    return False
```

## Rules

- **One file only.** Do not touch any other file. Do not edit `__init__.py`.
- **No new dependencies.** The module imports only `gi` / `gi.repository.Gtk`.
  No imports from `ui/`, `agent/`, `gateway/`, `models/`, or other utils modules.
- **Verbatim implementation.** Use the function body above exactly — identity
  comparison (`is`), `while child is not None:` loop, None guard on both args.
  Do not "improve" it (no early-return shortcuts, no type-narrowing changes).

## Verify (run these, paste full output)

1. Import check:
   ```
   python3 -c "from utils.gtk_containers import is_in_container; print('OK')"
   ```
   Expected: `OK`

2. Confirm no forbidden imports:
   ```
   grep -nE "from (ui|agent|gateway|models)" utils/gtk_containers.py
   ```
   Expected: no output (zero matches).

3. Confirm the file is syntactically valid:
   ```
   python3 -m py_compile utils/gtk_containers.py && echo COMPILE_OK
   ```
   Expected: `COMPILE_OK`

4. Line count:
   ```
   wc -l utils/gtk_containers.py
   ```
   Expected: ~40 lines.

## COMPLETENESS checklist (mandatory — include in your reply)

```
COMPLETENESS:
- [x/not done] utils/gtk_containers.py created with is_in_container() — evidence: <paste import-check output>
- [x/not done] No forbidden imports (ui/agent/gateway/models) — evidence: <paste grep output>
- [x/not done] py_compile passes — evidence: <paste COMPILE_OK>
- [x/not done] Line count ~40 — evidence: <paste wc -l output>
```

Report back with files changed, all four verification outputs pasted, and the
COMPLETENESS block. Please write per the steelFramedCodeWriter prompt.
