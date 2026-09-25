# tests/test_render_error_callbacks.py
# Regression tests for A4/A5 (SPEC-AUDIT-CLEANUP-1 Class A), REPOINTED for
# SPEC-06 SP4: the Pango builders (process_segments/build_role_bubble) are
# retired from ChatRenderHandler — the composition seam is now
# render_document (markdown → HTML → sanitize). The DEFERRED-error contract
# is unchanged: a composition failure on the worker thread must deliver the
# error via on_error on the main loop (the original bug was a NameError from
# a lambda closing over the bare except-variable `exc`, deleted at block
# exit — the eager-bind `lambda err=exc:` in the handler keeps that closed).

from unittest.mock import patch

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk  # noqa: F401 — import must precede handler import

import ui.handlers.chat_render_handler as crh_module
from ui.handlers.chat_render_handler import ChatRenderHandler


class DeferredGLib:
    """GLib double that RECORDS idle_add callbacks without running them.

    Mirrors production timing: callbacks scheduled from inside an except
    block run on the main loop AFTER the block exits — at which point
    Python has deleted the bare except-variable. Running the recorded
    callbacks in the test body reproduces that timing.
    """

    def __init__(self):
        self.pending = []

    def idle_add(self, fn, *args, **kwargs):
        self.pending.append((fn, args, kwargs))
        return 0


def _wait_until(cond, timeout=5.0, poll=0.01):
    """Poll cond() until truthy or timeout. Returns True on success."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(poll)
    return cond()


class TestDeferredErrorCallbacks:
    """A4/A5 repoint (SPEC-06 SP4): render_document raising on the worker
    thread must deliver the error via the deferred on_error callback on the
    main loop — not NameError, not silence.

    Timing traps avoided (as in the original A4/A5 design):
    1. Callbacks are run INSIDE the patch context so the patched composer
       is still active when the deferred callback executes.
    2. render_async runs on a ThreadPoolExecutor; the test polls until the
       worker has scheduled the callback before running it.
    """

    def test_render_async_compose_error_reaches_on_error(self):
        """A4 (repointed): render_document() raising must deliver the error
        via the deferred on_error callback, not NameError."""
        glib = DeferredGLib()
        handler = ChatRenderHandler(GLib_module=glib)
        errors = []
        with patch.object(crh_module, "render_document",
                          side_effect=Exception("render exploded")):
            handler.render_async("Agent", "text", "sk-async",
                                 on_bubble_ready=lambda w: None,
                                 on_error=lambda msg: errors.append(msg))
            assert _wait_until(lambda: len(glib.pending) >= 1), (
                "render_async worker never scheduled the error callback"
            )
            for fn, args, kwargs in glib.pending:
                fn(*args, **kwargs)  # was: NameError: cannot access free variable 'exc' where it is not associated with a value in enclosing scope
        assert errors == ["render exploded"], (
            f"A4 NameError masked by _dispatch: expected error text, got {errors}"
        )

    def test_render_compose_error_reaches_on_error(self):
        """A5 (repointed): the legacy render() entry hitting a composition
        failure must deliver the error via the deferred on_error callback."""
        glib = DeferredGLib()
        handler = ChatRenderHandler(GLib_module=glib)
        errors = []
        with patch.object(crh_module, "render_document",
                          side_effect=Exception("compose exploded")):
            handler.render("Agent", "text", "sk-sync",
                           on_bubble_ready=lambda w: None,
                           on_error=lambda msg: errors.append(msg))
            assert _wait_until(lambda: len(glib.pending) >= 1), (
                "render worker never scheduled the error callback"
            )
            for fn, args, kwargs in glib.pending:
                fn(*args, **kwargs)  # was: NameError: cannot access free variable 'exc' where it is not associated with a value in enclosing scope
        assert errors == ["compose exploded"], (
            f"A5 NameError masked by _dispatch: expected error text, got {errors}"
        )
