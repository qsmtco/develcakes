# Phase 3a of 4 — Wire `is_in_container` into `chat_render_handler.py`

**Spec:** `docs/specs/SPEC-GTK-CONTAINER-MEMBERSHIP-FIX.md` (§3.2)
**Master prompt:** `prompts/steelFramedCodeWriter.md` — invoke it. Read it first.
**Scope:** ONE file: `ui/handlers/chat_render_handler.py`. No other files.

## Task — three edits in `ui/handlers/chat_render_handler.py`

### Edit 1: Add imports (near top of file)

Find the existing import block. After the last existing `import` / `from` line near
the top of the file (BEFORE any class definition), add:

```python
import logging
from utils.gtk_containers import is_in_container
```

Place `import logging` with the other stdlib imports (alphabetical/conventional
ordering). Place `from utils.gtk_containers import is_in_container` with the
other `from utils...` imports (or at the end of the import block if there are
none yet). Read the file first to find the right spot.

### Edit 2: Add module logger

Immediately AFTER the imports block and BEFORE the first class definition (or
first module-level statement that isn't a comment/import), add:

```python
_logger = logging.getLogger(__name__)
```

### Edit 3: Replace site 1 — the `_finalize` membership check (line ~570)

Find this exact line inside the `_finalize` closure (within `end_streaming`):

```python
            if sb.bubble in sb.container:
```

Replace with:

```python
            if is_in_container(sb.bubble, sb.container):
```

(Only this one line changes. The `sb.container.remove(sb.bubble)` line below it
stays as-is.)

### Edit 4: Wrap `_dispatch`'s `_wrap` in try/except (lines ~747-755)

Find the `_dispatch` method (currently):

```python
    def _dispatch(self, fn):
        """Call fn on the GTK main thread."""
        if self._GLib is not None:
            def _wrap():
                fn()
                return False
            self._GLib.idle_add(_wrap)
        else:
            fn()
```

Replace the ENTIRE method with:

```python
    def _dispatch(self, fn):
        """Call fn on the GTK main thread.

        Uses GLib.idle_add to dispatch to the GTK main thread when
        GLib is available. Wraps the callback in try/except so that
        exceptions are logged rather than silently swallowed by GLib's
        main loop exception handler.

        KeyboardInterrupt and SystemExit are intentionally re-raised
        (not caught by the generic except Exception).
        """
        if self._GLib is not None:
            def _wrap():
                try:
                    fn()
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception:
                    _logger.exception("Unhandled exception in _dispatch callback")
                return False
            self._GLib.idle_add(_wrap)
        else:
            fn()
```

## Rules

- **One file only:** `ui/handlers/chat_render_handler.py`. Do not touch feed_tab.py (that's Phase 3b).
- **Read the file first.** Confirm the exact text of the import block, the `_finalize`
  line, and the `_dispatch` method before editing. Anchor on the exact strings above.
- **Do not change `_dispatch` semantics for the non-GLib path** (`else: fn()` stays
  direct — no try/except there; tests rely on exceptions propagating synchronously).
- **Do not remove any other code.** This is purely additive + the one-line site-1 fix.

## Verify (run these, paste full output)

1. Compile check:
   ```
   python3 -m py_compile ui/handlers/chat_render_handler.py && echo COMPILE_OK
   ```
   Expected: `COMPILE_OK`

2. Site 1 is gone, helper is used:
   ```
   grep -n "sb.bubble in sb.container" ui/handlers/chat_render_handler.py
   ```
   Expected: zero matches (exit 1).

   ```
   grep -n "is_in_container(sb.bubble, sb.container)" ui/handlers/chat_render_handler.py
   ```
   Expected: 1 match.

3. Imports + logger present:
   ```
   grep -nE "^import logging|^from utils.gtk_containers import is_in_container|^_logger = logging.getLogger" ui/handlers/chat_render_handler.py
   ```
   Expected: 3 matches.

4. `_dispatch` has the try/except + BaseException guard:
   ```
   grep -nE "except \(KeyboardInterrupt, SystemExit\)|_logger.exception" ui/handlers/chat_render_handler.py
   ```
   Expected: 2 matches (one for each pattern).

5. Non-GLib path unchanged (no try/except in the `else` branch):
   ```
   grep -A2 "else:" ui/handlers/chat_render_handler.py | grep "fn()"
   ```
   (Just confirm visually that the `else: fn()` line exists and is bare.)

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] import logging + from utils.gtk_containers import is_in_container added — evidence: <paste grep output>
- [x/not done] _logger = logging.getLogger(__name__) added — evidence: <paste grep output>
- [x/not done] Site 1 (sb.bubble in sb.container) replaced with is_in_container(...) — evidence: <paste both greps>
- [x/not done] _dispatch _wrap wrapped in try/except with (KeyboardInterrupt, SystemExit) re-raise + _logger.exception — evidence: <paste grep output>
- [x/not done] Non-GLib else-branch unchanged (else: fn() bare) — evidence: <paste grep output>
- [x/not done] py_compile passes — evidence: <paste COMPILE_OK>
```

Report back with files changed, all verification outputs, and the COMPLETENESS block. Please write per the steelFramedCodeWriter prompt.
