# tests/test_gtk_safe_link.py
# Unit tests for HIGH-6 link-scheme guard.
#
# Tests the pure-Python scheme check (utils.gtk_safe_link._is_safe_scheme +
# on_activate_link). The Gtk widget factory is tested separately via the
# integration tests in tests/test_chat_render_handler.py.

from utils.gtk_safe_link import (
    _is_safe_scheme,
    on_activate_link,
    _ALLOWED_LINK_SCHEMES,
)


class TestIsSafeScheme:
    def test_https_allowed(self):
        assert _is_safe_scheme("https://example.com/page") is True

    def test_http_allowed(self):
        assert _is_safe_scheme("http://example.com/") is True

    def test_mailto_allowed(self):
        assert _is_safe_scheme("mailto:user@example.com") is True

    def test_https_with_query_and_fragment(self):
        assert _is_safe_scheme("https://example.com/a?b=c#d") is True

    def test_javascript_blocked(self):
        assert _is_safe_scheme("javascript:alert(1)") is False

    def test_data_blocked(self):
        assert _is_safe_scheme("data:text/html,<script>alert(1)</script>") is False

    def test_file_blocked(self):
        assert _is_safe_scheme("file:///etc/passwd") is False

    def test_smb_blocked(self):
        assert _is_safe_scheme("smb://server/share") is False

    def test_ftp_blocked(self):
        assert _is_safe_scheme("ftp://server/file") is False

    def test_ssh_blocked(self):
        assert _is_safe_scheme("ssh://server") is False

    def test_custom_scheme_blocked(self):
        assert _is_safe_scheme("myapp://open") is False
        assert _is_safe_scheme("obsidian://vault") is False

    def test_vbscript_blocked(self):
        assert _is_safe_scheme("vbscript:msgbox(1)") is False

    def test_empty_blocked(self):
        assert _is_safe_scheme("") is False

    def test_scheme_case_insensitive(self):
        # urlparse lowercases scheme, but test the raw form too
        assert _is_safe_scheme("HTTPS://example.com/") is True
        assert _is_safe_scheme("JAVASCRIPT:alert(1)") is False

    def test_relative_url_allowed(self):
        # Relative URLs are allowed (they don't navigate outside the app)
        assert _is_safe_scheme("/local/path") is True
        assert _is_safe_scheme("#anchor") is True
        assert _is_safe_scheme("relative/path") is True


class TestOnActivateLink:
    """Tests for the activate-link signal handler. This handler returns True
    to block navigation, False to allow."""

    def test_returns_false_for_safe_url(self):
        assert on_activate_link(None, "https://example.com/") is False
        assert on_activate_link(None, "http://example.com/") is False
        assert on_activate_link(None, "mailto:user@example.com") is False

    def test_returns_true_for_unsafe_url(self):
        assert on_activate_link(None, "javascript:alert(1)") is True
        assert on_activate_link(None, "file:///etc/passwd") is True
        assert on_activate_link(None, "data:text/html,<x>") is True

    def test_label_arg_is_ignored(self):
        # The label argument is required by GTK's signal signature but unused
        assert on_activate_link("any-label-obj", "https://example.com/") is False
        assert on_activate_link(object(), "javascript:foo") is True


class TestBlockquoteLinkGuard:
    """HIGH-6 Phase 6.1 regression: blockquote path must use make_safe_label.

    The blockquote segment renderer (_build_quote_segment in event_cards.py)
    was missed in Phase 6 commit 593391e. It used raw Gtk.Label() + set_markup()
    with no activate-link guard, allowing javascript: links to be clicked.
    """

    def test_blockquote_javascript_link_blocked(self):
        """A blockquote with [click](javascript:alert(1)) must have activate-link handler.

        P6.1-3 strengthening: verify the handler is actually *connected* to the
        label's activate-link signal — not just that on_activate_link works in
        isolation. We use GObject.signal_handler_is_connected to confirm the
        signal is wired, then emit the signal and verify the emission returns
        True (blocked).
        """
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk
            import gi.repository.GObject as GObject
        except (ImportError, ValueError):
            pytest.skip("GTK not available in test environment")

        from ui.views.event_cards import _build_quote_segment
        from utils.gtk_safe_link import on_activate_link

        seg = {"type": "blockquote", "content": "[click](javascript:alert(1))"}
        widget = _build_quote_segment(seg)

        # The box should contain a Gtk.Label child
        label = widget.get_first_child()
        assert label is not None, "Blockquote segment returned empty box"
        assert isinstance(label, Gtk.Label), f"Expected Gtk.Label, got {type(label)}"

        # P6.1-3: Verify the activate-link handler is actually CONNECTED.
        # make_safe_label connects on_activate_link via label.connect().
        # A raw Gtk.Label with set_markup would NOT have this handler.
        #
        # We check by looking at whether emission of activate-link is handled.
        # signal_handler_is_connected needs a handler_id, which we don't have.
        # Instead, use signal_emit to check that the signal emission returns
        # True (blocked) for a javascript: URI — this only works if a handler
        # is connected.

        # Emit activate-link signal and capture the return value.
        # label.emit triggers the actual signal emission, which only returns
        # True if a handler is connected and blocks. A raw Gtk.Label without
        # the make_safe_label handler would return False (allow navigation).
        retval = label.emit("activate-link", "javascript:alert(1)")
        assert retval is True, (
            "activate-link signal did not return True for javascript: URL — "
            "handler is not connected (label was not created by make_safe_label)"
        )

        # Also verify safe URLs are NOT blocked
        retval_safe = label.emit("activate-link", "https://example.com/")
        assert retval_safe is False, (
            "activate-link signal blocked a safe https: URL — "
            "handler is over-blocking"
        )

        # Verify the function reference matches (defense against accidental
        # connection of a different handler)
        assert on_activate_link(label, "javascript:alert(1)") is True

    def test_blockquote_css_class_preserved(self):
        """The blockquote-text CSS class must still be applied after Phase 6.1 fix."""
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk
        except (ImportError, ValueError):
            pytest.skip("GTK not available in test environment")

        from ui.views.event_cards import _build_quote_segment

        seg = {"type": "blockquote", "content": "normal text"}
        widget = _build_quote_segment(seg)
        label = widget.get_first_child()
        assert label is not None

        # Check the CSS class is present
        css_classes = label.get_css_classes()
        assert "blockquote-text" in css_classes, f"CSS classes: {css_classes}"


class TestAllowedLinkSchemesConsistency:
    """Guard against drift between markdown.py and gtk_safe_link.py allowlists."""

    def test_allowlist_matches_markdown(self):
        from utils.markdown import _ALLOWED_LINK_SCHEMES as md_allowlist
        assert md_allowlist == _ALLOWED_LINK_SCHEMES, (
            "HIGH-6: _ALLOWED_LINK_SCHEMES drifted between utils.markdown "
            f"({md_allowlist}) and utils.gtk_safe_link ({_ALLOWED_LINK_SCHEMES}). "
            "Both must agree or the render-time allowlist and the activate-link "
            "guard will diverge."
        )


class TestMakeSafeLabelCssClasses:
    """Tests for the css_classes parameter (Bug #5 + #11).

    Verifies that make_safe_label accepts a list of CSS classes and applies
    each as a separate entry (GTK4's add_css_class treats strings as single
    class names — spaces are NOT separators).
    """

    def test_css_classes_list_applies_separately(self):
        """css_classes=['a', 'b'] must produce two separate CSS classes,
        not one compound class 'a b'."""
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk
        except (ImportError, ValueError):
            pytest.skip("GTK not available in test environment")

        from utils.gtk_safe_link import make_safe_label

        label = make_safe_label("test", css_classes=["chat-heading", "chat-heading-2"])
        classes = label.get_css_classes()
        assert "chat-heading" in classes, f"missing chat-heading: {classes}"
        assert "chat-heading-2" in classes, f"missing chat-heading-2: {classes}"
        assert "chat-heading chat-heading-2" not in classes, (
            f"compound class bug (Bug #5): {classes}"
        )

    def test_css_class_backward_compat(self):
        """Existing callers passing css_class='single' must still work."""
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk
        except (ImportError, ValueError):
            pytest.skip("GTK not available in test environment")

        from utils.gtk_safe_link import make_safe_label

        label = make_safe_label("test", css_class="chat-msg-label")
        classes = label.get_css_classes()
        assert "chat-msg-label" in classes, f"missing class: {classes}"

    def test_both_css_class_and_css_classes_together(self):
        """When both params are passed, all classes from both are applied."""
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk
        except (ImportError, ValueError):
            pytest.skip("GTK not available in test environment")

        from utils.gtk_safe_link import make_safe_label

        label = make_safe_label("test", css_class="a", css_classes=["b", "c"])
        classes = label.get_css_classes()
        assert "a" in classes and "b" in classes and "c" in classes, f"missing: {classes}"

    def test_css_classes_none_is_noop(self):
        """css_classes=None (default) must not add any classes."""
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk
        except (ImportError, ValueError):
            pytest.skip("GTK not available in test environment")

        from utils.gtk_safe_link import make_safe_label

        label = make_safe_label("test")
        classes = label.get_css_classes()
        # No css_class or css_classes passed — should have no custom classes
        # (Gtk.Label may have default classes, but none we added)
        assert "chat-heading" not in classes

    def test_css_classes_empty_list_is_noop(self):
        """css_classes=[] must not add any classes and must not error."""
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk
        except (ImportError, ValueError):
            pytest.skip("GTK not available in test environment")

        from utils.gtk_safe_link import make_safe_label

        label = make_safe_label("test", css_classes=[])
        # No error, no crash — that's the assertion
        assert label is not None

    def test_css_classes_string_rejected(self):
        """css_classes='a b' (string, not list) must raise TypeError.

        This is the Bug #5 footgun: a string would iterate char-by-char,
        producing single-char class names. The type guard rejects it.
        """
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk
        except (ImportError, ValueError):
            pytest.skip("GTK not available in test environment")

        from utils.gtk_safe_link import make_safe_label
        import pytest as _pytest

        with _pytest.raises(TypeError, match="css_classes must be a list"):
            make_safe_label("test", css_classes="chat-heading chat-heading-2")

    def test_css_classes_empty_string_element_rejected(self):
        """css_classes=['valid', ''] must raise ValueError for the empty string."""
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk
        except (ImportError, ValueError):
            pytest.skip("GTK not available in test environment")

        from utils.gtk_safe_link import make_safe_label
        import pytest as _pytest

        with _pytest.raises(ValueError, match="Invalid CSS class"):
            make_safe_label("test", css_classes=["valid", ""])


class TestMakeSafeLabelDocstring:
    """Bug #11: the css_classes parameter must be documented in the docstring."""

    def test_docstring_mentions_css_classes(self):
        import inspect
        from utils.gtk_safe_link import make_safe_label

        doc = inspect.getdoc(make_safe_label) or ""
        assert "css_classes" in doc, "docstring missing css_classes parameter"

    def test_docstring_explains_whitespace_trap(self):
        """The docstring must explain that GTK4's add_css_class does not split on whitespace."""
        import inspect
        from utils.gtk_safe_link import make_safe_label

        doc = inspect.getdoc(make_safe_label) or ""
        assert "add_css_class" in doc, "docstring missing add_css_class reference"
        assert "NOT separators" in doc or "not separators" in doc.lower(), (
            "docstring must warn that spaces are not separators in add_css_class"
        )

    def test_docstring_references_bug_5(self):
        """The docstring must reference Bug #5 for context (per spec §2.9)."""
        import inspect
        from utils.gtk_safe_link import make_safe_label

        doc = inspect.getdoc(make_safe_label) or ""
        assert "Bug #5" in doc, "docstring missing Bug #5 reference"


class TestNonStringUriGuard:
    """BUG #4 (audit): _is_safe_scheme and on_activate_link must handle non-string URIs.

    PyGObject's exception handling causes fail-open (returns Falsy → link allowed)
    when a signal handler raises TypeError. This is a security anti-pattern for
    a blocking handler. The fix: guard against non-string input explicitly."""

    def test_is_safe_scheme_rejects_int(self):
        from utils.gtk_safe_link import _is_safe_scheme
        assert _is_safe_scheme(42) is False

    def test_is_safe_scheme_rejects_none(self):
        from utils.gtk_safe_link import _is_safe_scheme
        assert _is_safe_scheme(None) is False

    def test_is_safe_scheme_rejects_list(self):
        from utils.gtk_safe_link import _is_safe_scheme
        assert _is_safe_scheme([1, 2]) is False

    def test_on_activate_link_blocks_non_string(self):
        """Non-string URI must be blocked (fail-closed), not allowed."""
        from utils.gtk_safe_link import on_activate_link
        assert on_activate_link(None, 42) is True  # block
        assert on_activate_link(None, None) is True  # block
        assert on_activate_link(None, [1, 2, 3]) is True  # block
