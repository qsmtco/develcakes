# Phase 4 of 4 — Create `tests/test_gtk_container_membership.py`

**Spec:** `docs/specs/SPEC-GTK-CONTAINER-MEMBERSHIP-FIX.md` (§3.5) — **READ THE SPEC FIRST; it was revised 2026-07-28 to fix a bug in the original fake-widget design.**
**Master prompt:** `prompts/steelFramedCodeWriter.md` — invoke it. Read it first.
**Scope:** ONE new file: `tests/test_gtk_container_membership.py`. No other files.

## Spec revision note (critical)

The spec's §3.5.1 was revised. The original `FakeGtkBoxNoContains.get_next_sibling(self, child)`
took a `child` argument — **WRONG**. Production `is_in_container` calls
`child.get_next_sibling()` with **no arguments** (GTK4 API). The original
`FakeChildWidget` was bare `pass`, so the sibling walk would `AttributeError` on
multi-child tests. The revised fakes (in the spec) give `FakeChildWidget` a
back-reference to its container and a no-arg `get_next_sibling()`. **Use the
revised fakes from the spec, not any older version.**

## Task — create one new test file

Create `tests/test_gtk_container_membership.py` with the two fake classes and
three test groups below. The test file must have **NO GTK dependency** — it uses
only the pure-Python fakes and imports `is_in_container` from `utils.gtk_containers`.
Because `utils/gtk_containers.py` does `gi.require_version('Gtk', '4.0')` at import
time, GTK must be importable — but the tests themselves never instantiate real GTK
widgets. (The known sandbox segfault is on `Gtk.Box()` *construction* in
`chat_bubble.py`, not on `gi` import. If `import gi` itself segfaults in your
environment, report it — but it should not, since Phase 1's import check passed.)

### Imports at top of file

```python
import pytest
from utils.gtk_containers import is_in_container
```

### Fakes (place after imports, before test classes)

Use EXACTLY these (from the revised spec §3.5.1):

```python
class FakeChildWidget:
    """Minimal widget stand-in with identity-based comparison.

    Models GTK4's sibling chain: each widget holds a back-reference to its
    parent container and implements the no-arg get_next_sibling() by looking
    up its position in the container's child list.
    """
    def __init__(self):
        self._parent_container = None

    def get_next_sibling(self):
        """Return the next sibling after self, or None (mirrors Gtk.Widget)."""
        if self._parent_container is None:
            return None
        children = self._parent_container._children
        try:
            idx = children.index(self)
        except ValueError:
            return None
        return children[idx + 1] if idx + 1 < len(children) else None


class FakeGtkBoxNoContains:
    """
    Reproduces the real GTK bug: no ``__contains__``, no ``__iter__``.

    ``widget in fake_box`` raises ``TypeError``, exactly like ``widget in Gtk.Box()``
    does in PyGObject. This proves the bug class without mocking the symptom.
    """
    def __init__(self):
        self._children = []

    def append(self, widget):
        widget._parent_container = self
        self._children.append(widget)

    def remove(self, widget):
        widget._parent_container = None
        self._children.remove(widget)

    def get_first_child(self):
        return self._children[0] if self._children else None
```

### Group A — Document the bug class (1 test)

```python
def test_widget_in_gtk_box_raises_type_error():
    """
    ``widget in gtk_box`` raises TypeError because PyGObject does not
    wire ``__contains__`` onto GTK containers. Use ``is_in_container()``
    instead.
    """
    box = FakeGtkBoxNoContains()
    widget = FakeChildWidget()
    box.append(widget)
    with pytest.raises(TypeError):
        _ = widget in box
```

### Group B — Unit tests for `is_in_container` (9 tests)

```python
class TestIsInContainer:
    """Tests for utils.gtk_containers.is_in_container()."""

    def test_widget_present_first_child(self):
        """Widget is the first (and only) child → True."""
        box = FakeGtkBoxNoContains()
        w = FakeChildWidget()
        box.append(w)
        assert is_in_container(w, box) is True

    def test_widget_present_middle_child(self):
        """Widget is the 3rd of 5 children → True."""
        box = FakeGtkBoxNoContains()
        widgets = [FakeChildWidget() for _ in range(5)]
        for w in widgets:
            box.append(w)
        assert is_in_container(widgets[2], box) is True

    def test_widget_present_last_child(self):
        """Widget is the last child → True (walk reaches the end)."""
        box = FakeGtkBoxNoContains()
        w1, w2 = FakeChildWidget(), FakeChildWidget()
        box.append(w1)
        box.append(w2)
        assert is_in_container(w2, box) is True

    def test_widget_absent(self):
        """Container has children, widget not among them → False."""
        box = FakeGtkBoxNoContains()
        box.append(FakeChildWidget())
        box.append(FakeChildWidget())
        other = FakeChildWidget()
        assert is_in_container(other, box) is False

    def test_widget_after_remove(self):
        """Widget was once a child but was removed → False."""
        box = FakeGtkBoxNoContains()
        w = FakeChildWidget()
        box.append(w)
        box.remove(w)
        assert is_in_container(w, box) is False

    def test_none_widget(self):
        """widget=None → False."""
        box = FakeGtkBoxNoContains()
        assert is_in_container(None, box) is False

    def test_none_container(self):
        """container=None → False."""
        w = FakeChildWidget()
        assert is_in_container(w, None) is False

    def test_both_none(self):
        """Both None → False."""
        assert is_in_container(None, None) is False

    def test_empty_container(self):
        """Container has no children → False."""
        box = FakeGtkBoxNoContains()
        w = FakeChildWidget()
        assert is_in_container(w, box) is False
```

### Group C — Static regression checks (5 tests)

These read source files from disk to confirm the old broken patterns are gone and
the new helpers/imports are present. They are NOT dependent on GTK.

```python
class TestStaticRegression:
    """Source-level checks that the old broken patterns are gone."""

    CHAT_RENDER_PATH = "ui/handlers/chat_render_handler.py"
    FEED_TAB_PATH = "ui/views/feed_tab.py"

    def test_chat_render_handler_no_old_pattern(self):
        """The 'if sb.bubble in sb.container' pattern is gone."""
        src = self._read(self.CHAT_RENDER_PATH)
        assert "sb.bubble in sb.container" not in src, \
            f"Old pattern 'sb.bubble in sb.container' still present in {self.CHAT_RENDER_PATH}"

    def test_feed_tab_no_old_patterns(self):
        """All 5 'in self._card_container' patterns are gone."""
        src = self._read(self.FEED_TAB_PATH)
        for pattern in ["widget in self._card_container",
                        "self._empty_widget in self._card_container",
                        "old_widget not in self._card_container"]:
            assert pattern not in src, \
                f"Old pattern '{pattern}' still present in {self.FEED_TAB_PATH}"

    def test_is_in_container_imported_in_chat_render(self):
        """is_in_container is imported in chat_render_handler.py."""
        src = self._read(self.CHAT_RENDER_PATH)
        assert "from utils.gtk_containers import is_in_container" in src

    def test_is_in_container_imported_in_feed_tab(self):
        """is_in_container is imported in feed_tab.py."""
        src = self._read(self.FEED_TAB_PATH)
        assert "from utils.gtk_containers import is_in_container" in src

    def test_dispatch_has_exception_logging(self):
        """_dispatch's _wrap has try/except with _logger.exception."""
        src = self._read(self.CHAT_RENDER_PATH)
        assert "try:" in src and "_logger.exception" in src, \
            f"_dispatch missing try/except + _logger.exception in {self.CHAT_RENDER_PATH}"

    @staticmethod
    def _read(path):
        with open(path) as f:
            return f.read()
```

## Rules

- **One file only:** `tests/test_gtk_container_membership.py`. Do not edit any other file.
- **No GTK widget construction in the tests.** Only the pure-Python fakes.
- **15 tests total:** Group A (1) + Group B (9) + Group C (5).
- **Use the revised fakes from the spec**, NOT any older `pass`-only FakeChildWidget.

## Verify (run these, paste full output)

1. Compile check:
   ```
   python3 -m py_compile tests/test_gtk_container_membership.py && echo COMPILE_OK
   ```

2. Run the full test file:
   ```
   python3 -m pytest tests/test_gtk_container_membership.py -v
   ```
   Expected: 15 passed. If GTK import segfaults (not expected — import only, no
   widget construction), report it. The tests use pure-Python fakes.

3. Confirm test count:
   ```
   grep -c "def test_" tests/test_gtk_container_membership.py
   ```
   Expected: 15.

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] FakeChildWidget with back-ref + no-arg get_next_sibling — evidence: <paste class from file>
- [x/not done] FakeGtkBoxNoContains (no __contains__, no __iter__) — evidence: <paste>
- [x/not done] Group A: 1 test (widget in box raises TypeError) — evidence: <pytest line>
- [x/not done] Group B: 9 tests (first/middle/last/absent/after-remove/3x-None/empty) — evidence: <pytest summary>
- [x/not done] Group C: 5 static regression tests — evidence: <pytest summary>
- [x/not done] 15/15 tests pass — evidence: <paste pytest summary line>
- [x/not done] py_compile passes — evidence: COMPILE_OK
```

Report back with files changed, all verification outputs, and the COMPLETENESS block. Please write per the steelFramedCodeWriter prompt.
