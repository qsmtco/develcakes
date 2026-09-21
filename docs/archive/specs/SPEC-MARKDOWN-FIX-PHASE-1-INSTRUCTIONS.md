# PHASE 1 of 10 — `make_safe_label` css_classes parameter (Bug #5 + #11)

**Spec:** `docs/specs/spec-markdown-header-fix.md` §2.1 (Bug #5) + §2.9 (Bug #11)
**File to change:** `utils/gtk_safe_link.py` (single file)

## Goal

Add a backward-compatible `css_classes` parameter (list[str]) to `make_safe_label()` so callers can apply multiple CSS classes. Today the function only accepts a single `css_class` string; GTK4's `add_css_class()` treats spaces as part of the name, so passing `"chat-heading chat-heading-2"` produces one invalid compound class. Also document the new parameter in the docstring.

## Exact change

### Current signature (verified at `utils/gtk_safe_link.py:77-83`)

```python
def make_safe_label(
    markup: str,
    *,
    xalign: float = 0,
    wrap: bool = True,
    selectable: bool = True,
    css_class: str | None = None,
) -> "Gtk.Label":
```

### New signature (add `css_classes` after `css_class`)

```python
def make_safe_label(
    markup: str,
    *,
    xalign: float = 0,
    wrap: bool = True,
    selectable: bool = True,
    css_class: str | None = None,           # backward compat: single class
    css_classes: list[str] | None = None,   # NEW: multiple classes
) -> "Gtk.Label":
```

### Body change (after the existing `if css_class:` block)

Current body (lines ~99-100):
```python
    if css_class:
        label.add_css_class(css_class)
    # HIGH-6: gate navigation on scheme allowlist
    label.connect("activate-link", on_activate_link)
    return label
```

New body — add the `css_classes` loop AFTER the existing `if css_class:` block and BEFORE the activate-link connect:
```python
    if css_class:
        label.add_css_class(css_class)
    if css_classes:
        for cls in css_classes:
            label.add_css_class(cls)
    # HIGH-6: gate navigation on scheme allowlist
    label.connect("activate-link", on_activate_link)
    return label
```

### Docstring update

Update the docstring to document the new parameter. Add this `Args:` block to the existing docstring (keep the existing prose, just add the structured args section):

```python
    """
    Create a Gtk.Label wired with the HIGH-6 activate-link guard.

    ... (existing prose unchanged) ...

    Args:
        markup: The Pango markup string to display (output of escape_for_pango
            + format_markdown).
        xalign: Horizontal alignment (0=left, 0.5=center, 1=right). Default 0.
        wrap: Whether to wrap text. Default True.
        selectable: Whether the text is selectable. Default True.
        css_class: A single CSS class to add. Backward compat with existing
            callers.
        css_classes: A list of CSS classes to add. Use this when you need to
            apply multiple classes (e.g., ["chat-heading", "chat-heading-2"]).
            GTK4's add_css_class() treats strings as single class names —
            spaces are NOT separators. See Bug #5 in spec-markdown-header-fix.md.

    Returns:
        A configured Gtk.Label with the markup applied and the activate-link
        handler connected (HIGH-6 defense-in-depth: non-allowlisted schemes
        like javascript: are blocked).
    """
```

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- **Backward compatibility is mandatory.** The existing `css_class` parameter MUST stay. Existing callers passing `css_class="chat-msg-label"` etc. MUST continue to work unchanged.
- Do NOT change any other function in the file (`on_activate_link`, `_is_safe_scheme` stay as-is).
- Do NOT touch any other file in this phase. Only `utils/gtk_safe_link.py`.

## Verification commands (run these, paste the output)

```bash
cd /path/to/projects/crabcakes

# 1. Confirm new parameter exists with correct default
python3 -c "
import inspect
from utils.gtk_safe_link import make_safe_label
sig = inspect.signature(make_safe_label)
print('signature:', sig)
assert 'css_classes' in sig.parameters, 'css_classes parameter missing'
assert sig.parameters['css_classes'].default is None, 'default must be None'
print('OK: css_classes param present with default None')
"

# 2. Confirm backward compat — existing css_class= call still works
python3 -c "
import gi; gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
from utils.gtk_safe_link import make_safe_label
label = make_safe_label('test', css_class='chat-msg-label')
classes = label.get_css_classes()
assert 'chat-msg-label' in classes, f'missing class: {classes}'
print('OK: css_class= backward compat works:', classes)
"

# 3. Confirm new css_classes= applies multiple classes as SEPARATE entries
python3 -c "
import gi; gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
from utils.gtk_safe_link import make_safe_label
label = make_safe_label('test', css_classes=['chat-heading', 'chat-heading-2'])
classes = label.get_css_classes()
assert 'chat-heading' in classes, f'missing chat-heading: {classes}'
assert 'chat-heading-2' in classes, f'missing chat-heading-2: {classes}'
assert 'chat-heading chat-heading-2' not in classes, f'compound class bug: {classes}'
print('OK: css_classes= applies separate classes:', classes)
"

# 4. Confirm both params can be passed together (both applied)
python3 -c "
import gi; gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
from utils.gtk_safe_link import make_safe_label
label = make_safe_label('test', css_class='a', css_classes=['b', 'c'])
classes = label.get_css_classes()
assert 'a' in classes and 'b' in classes and 'c' in classes, f'missing: {classes}'
print('OK: both params together:', classes)
"

# 5. Confirm docstring documents css_classes (Bug #11)
python3 -c "
import inspect
from utils.gtk_safe_link import make_safe_label
doc = inspect.getdoc(make_safe_label) or ''
assert 'css_classes' in doc, 'docstring missing css_classes'
assert 'add_css_class' in doc, 'docstring missing add_css_class reference'
print('OK: docstring documents css_classes')
"

# 6. Run existing tests — NO regressions
python3 -m pytest tests/test_gtk_safe_link.py -v 2>&1 | tail -25
```

## Deliverables (COMPLETENESS checklist required)

When done, report:
1. Files changed with line numbers
2. Full output of all 6 verification commands above
3. `git diff utils/gtk_safe_link.py` output
4. COMPLETENESS checklist:
```
COMPLETENESS:
- [x/not done] css_classes param added with default None — evidence: (signature output)
- [x/not done] body loops css_classes and applies each via add_css_class — evidence: (diff)
- [x/not done] existing css_class param + behavior preserved — evidence: (backward-compat test)
- [x/not done] docstring documents css_classes + add_css_class caveat — evidence: (docstring test)
- [x/not done] no other file touched — evidence: (git diff --stat)
- [x/not done] existing tests/test_gtk_safe_link.py pass — evidence: (pytest tail)
```
