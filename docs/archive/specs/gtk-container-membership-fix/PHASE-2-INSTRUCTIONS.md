# Phase 2 of 4 — Update `FakeChatBox` + fix stale assertion

**Spec:** `docs/specs/SPEC-GTK-CONTAINER-MEMBERSHIP-FIX.md` (§3.4)
**Master prompt:** `prompts/steelFramedCodeWriter.md` — invoke it. Read it first.
**Scope:** ONE file edited: `tests/test_chat_render_handler.py`. No other files.

## Why this phase comes before production wiring

After Phase 3, `_finalize()` will call `is_in_container(sb.bubble, sb.container)`.
That helper calls `container.get_first_child()` and (on each child)
`child.get_next_sibling()`. In the test suite, `sb.container` is a `FakeChatBox`
instance. Right now `FakeChatBox` has no `get_first_child()` / `get_next_sibling()`
methods, so the helper would raise `AttributeError` mid-test. This phase adds those
methods so Phase 3's production code is testable.

## Task — two edits in `tests/test_chat_render_handler.py`

### Edit 1: Extend `FakeChatBox` (currently at lines 397-409)

Current `FakeChatBox`:
```python
class FakeChatBox:
    """Minimal Gtk.Box stand-in for testing bubble append/remove."""
    def __init__(self):
        self._children = []

    def append(self, widget):
        self._children.append(widget)

    def remove(self, widget):
        self._children.remove(widget)

    def __contains__(self, widget):
        return widget in self._children
```

Add two methods so `is_in_container` can walk the children via the same API real
`Gtk.Widget` exposes. The final class should be:

```python
class FakeChatBox:
    """Minimal Gtk.Box stand-in for testing bubble append/remove."""
    def __init__(self):
        self._children = []

    def append(self, widget):
        self._children.append(widget)

    def remove(self, widget):
        self._children.remove(widget)

    def __contains__(self, widget):
        return widget in self._children

    def get_first_child(self):
        """Return the first child, or None if empty (mirrors Gtk.Widget)."""
        return self._children[0] if self._children else None

    def get_next_sibling(self):
        """
        FakeChatBox is a container stand-in, not a widget, so it has no
        siblings. Returns None — mirrors Gtk.Widget.get_next_sibling() on a
        root-level container. (The sibling walk in is_in_container calls
        get_next_sibling on each CHILD widget, not on the container.)
        """
        return None
```

IMPORTANT — read this before you write:
GTK4's real `Gtk.Widget.get_next_sibling()` takes **no arguments** and returns the
next sibling of the widget it is called on. In `FakeChatBox`, the container itself
has no siblings, so `get_next_sibling(self)` correctly returns `None`. The
`is_in_container` sibling walk calls `child.get_next_sibling()` on each CHILD
widget object — but in the test suite the children are also `FakeChatBox`-appended
stand-ins (e.g. simple objects returned by `_make_streaming_widget` / bubble
builders). Check what those children actually are: if the children are plain
`object()` instances or similar fakes with no `get_next_sibling`, the sibling walk
will `AttributeError`. **Read the test file and find out what `FakeChatBox._children`
actually contains before finalizing the method.** If the children are fakes that
lack `get_next_sibling`, you must ALSO add a minimal `get_next_sibling` to whatever
fake-widget class the children are, OR document precisely why the walk still works.
Report what you find.

### Edit 2: Fix the stale assertion at line 200

Current (inside `test_start_streaming_twice_idempotent`, around line 200):
```python
        assert len(self.fake_box._children) == 2  # old bubble not removed from FakeChatBox, only from real GTK container
```

Change to:
```python
        assert len(self.fake_box._children) == 1  # old bubble removed by _finalize via is_in_container, new bubble appended
```

Rationale (from spec §3.4.1): after Phase 3, calling `start_streaming` twice triggers
`_finalize` on the first bubble, which now succeeds at `is_in_container(sb.bubble,
sb.container)` → True (because FakeChatBox implements `get_first_child`), then calls
`sb.container.remove(sb.bubble)`. So after two `start_streaming` calls, only the
second (new) bubble remains: `len == 1`, not `2`.

NOTE: This assertion change is being made in Phase 2 but will only actually PASS once
Phase 3 wires the production code. So after this phase, this specific test may FAIL
(that's expected — it asserts post-Phase-3 behavior). Document the expected failure
in your report. All OTHER tests in the file must still pass.

## Rules

- **One file only:** `tests/test_chat_render_handler.py`. No production code.
- **Read the test file first** — understand what `_children` contains (plain
  objects? FakeWidget instances? Gtk.Label?) before writing the `get_next_sibling`
  method. This is critical (steelFramedCodeWriter Step 1: read before touch).
- **Do not change any test logic** other than the one assertion at line ~200.
- **Do not remove `__contains__`** from FakeChatBox — leave it; some tests may rely
  on it directly.

## Verify (run these, paste full output)

1. Run the full test file:
   ```
   python3 -m pytest tests/test_chat_render_handler.py -v
   ```
   Expected: all tests pass EXCEPT possibly `test_start_streaming_twice_idempotent`
   (which now asserts post-Phase-3 behavior). If that test fails, report the exact
   failure — it is expected.

2. Confirm FakeChatBox has the new methods:
   ```
   python3 -c "import tests.test_chat_render_handler as m; b=m.FakeChatBox(); print('first:', b.get_first_child()); print('sibling:', b.get_next_sibling())"
   ```
   Expected: `first: None` / `sibling: None` (empty box).

3. Confirm the assertion line changed:
   ```
   grep -n "len(self.fake_box._children)" tests/test_chat_render_handler.py
   ```

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] FakeChatBox has get_first_child + get_next_sibling — evidence: <paste cmd 2 output>
- [x/not done] Investigated what _children contains (report finding) — evidence: <one sentence>
- [x/not done] Stale assertion changed == 2 → == 1 with new comment — evidence: <paste grep output>
- [x/not done] All other tests pass (test_start_streaming_twice_idempotent may fail — expected) — evidence: <paste pytest summary line>
```

Report back with files changed, all verification outputs, the child-type finding,
and the COMPLETENESS block. Please write per the steelFramedCodeWriter prompt.
