# tests/test_forward_handler.py
# Tests for ui/handlers/forward_handler.py — Phase 3b extraction.
#
# What this tests:
#   ForwardHandler.show_forward_popover builds a Gtk.Popover of every
#   other agent (special + gateway-from-open-tabs) and routes forwarded
#   text to the chosen target. ForwardHandler.forward_to_agent dispatches
#   the text to special-agent or gateway-client, creates/selects the target
#   tab, and renders a "forwarded from <source>" bubble into it.
#
# Principle: mock at the boundary, test behavior not internals.
# The 5 handler dependencies (main_content, chat_handler, chat_render_handler,
# agent_runtime_handler, gateway_handler) are MagicMock instances. Gtk is
# real — the test env has DISPLAY=:0 and Gtk widgets instantiate fine — but
# Gtk.Popover is wrapped with a small subclass that suppresses popup()/
# popdown() (which require a toplevel window) and tracks every instance so
# tests can walk the popover's child box and inspect the buttons.
#
# See ARCHITECTURE.md §3.6 (window.py is the composition root, handlers
# are self-contained) and §8.6 (handlers do not import other handlers).

import inspect
import pytest
from unittest.mock import MagicMock

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from ui.handlers.forward_handler import ForwardHandler  # noqa: E402


# ── Gtk capture infrastructure ───────────────────────────────────────────────


class _CapturingPopover(Gtk.Popover):
    """Subclass of Gtk.Popover that suppresses popup/popdown and tracks instances.

    The handler body calls ``Gtk.Popover()`` (no parent) and then
    ``popover.popup()`` / ``popover.popdown()`` on the resulting widget.
    Both of those need a real toplevel window and would emit Gtk-CRITICAL
    warnings in a headless test. Suppressing them lets us verify the
    popover was *constructed* and populated correctly without actually
    trying to show it on a non-existent display.
    """

    instances: list = []  # class-level; reset per-test by the fixture

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _CapturingPopover.instances.append(self)

    def popup(self) -> None:
        # Suppressed — would require a toplevel window in the display server.
        pass

    def popdown(self) -> None:
        # Suppressed for the same reason.
        pass


@pytest.fixture(autouse=True)
def _reset_capturing_popover():
    """Patch Gtk.Popover with _CapturingPopover for the duration of each test.

    autouse=True so every test gets a clean instance list and a real
    Gtk.Popover is restored at teardown (so other tests in the same
    process aren't affected).
    """
    _CapturingPopover.instances.clear()
    original = Gtk.Popover
    Gtk.Popover = _CapturingPopover
    try:
        yield
    finally:
        Gtk.Popover = original
        _CapturingPopover.instances.clear()


# ── GTK traversal helpers ───────────────────────────────────────────────────


def _walk_buttons(box: Gtk.Box):
    """Yield each direct child of a Gtk.Box in insertion order.

    forward_handler builds a vertical Gtk.Box, appends one Gtk.Button per
    agent, then sets that box as the popover's child. This generator lets
    tests iterate the buttons the same way GTK itself does.
    """
    child = box.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


def _last_popover_and_buttons():
    """Pull (popover, menu_box, [buttons]) from the most recent popover.

    Raises AssertionError if the handler did not create a popover — that
    is the expected failure mode for test_returns_early_when_no_agents.
    """
    assert _CapturingPopover.instances, "no popover was created"
    popover = _CapturingPopover.instances[-1]
    box = popover.get_child()
    assert box is not None, "popover had no child box"
    buttons = list(_walk_buttons(box))
    return popover, box, buttons


# ── Test data ───────────────────────────────────────────────────────────────


# Source-of-truth name lookups used by gw_agent_mgr.get_name(...)
AGENT_NAMES = {
    "sk-qa": "QA Bot",
    "sk-coder": "Coder",
    "sk-tab1": "Tab Agent 1",
    "sk-tab2": "Tab Agent 2",
    "sk-both": "In Both Lists",  # for the dedup test
}


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def gw_agent_mgr() -> MagicMock:
    """A mock gateway AgentManager that maps session_key -> display name."""
    mgr = MagicMock(name="AgentManager")
    mgr.get_name = MagicMock(side_effect=lambda sk: AGENT_NAMES.get(sk))
    return mgr


@pytest.fixture
def handler(gw_agent_mgr):
    """A ForwardHandler with realistic mocks for all 5 dependencies.

    The default fixture has:
      - 2 special agents: "sk-qa" / "QA Bot", "sk-coder" / "Coder"
      - 2 gateway tabs:   "sk-tab1" / "Tab Agent 1", "sk-tab2" / "Tab Agent 2"
      - no overlap between the two lists (so the dedup test gets a
        separate fixture, see make_handler_with_overlap below)
    """
    arh = MagicMock(name="AgentRuntimeHandler")
    arh.get_special_agents.return_value = {
        "sk-qa": "QA Bot",
        "sk-coder": "Coder",
    }
    arh.send_to_special_agent = MagicMock(name="send_to_special_agent")

    gh = MagicMock(name="GatewayHandler")
    gh.agent_mgr = gw_agent_mgr
    # The live GatewayClient (used for gw.send_message, gw.is_connected)
    gh._gw = MagicMock(name="GatewayClient")
    gh._gw.is_connected.return_value = True
    gh._gw.send_message = MagicMock(name="send_message")

    mc = MagicMock(name="MainContent")
    mc._tab_sessions = {0: "sk-tab1", 1: "sk-tab2"}
    mc.create_chat_tab = MagicMock(name="create_chat_tab", return_value=2)
    mc._chat_notebook = MagicMock(name="chat_notebook")
    mc.get_chat_box = MagicMock(name="get_chat_box", return_value=MagicMock(name="chat_box"))
    mc.scroll_chat_to_bottom = MagicMock(name="scroll_chat_to_bottom")

    crh = MagicMock(name="ChatRenderHandler")
    crh.render_sync = MagicMock(name="render_sync", return_value=MagicMock(name="bubble"))
    # The latent "may be None" edge case — the handler reads this directly
    crh._on_forward_message = MagicMock(name="on_forward_message")

    return ForwardHandler(
        main_content=mc,
        chat_handler=MagicMock(name="ChatHandler"),
        chat_render_handler=crh,
        agent_runtime_handler=arh,
        gateway_handler=gh,
    )


def make_handler(
    *,
    special_agents=None,
    tab_sessions=None,
    target_in_tabs=True,
    target_is_special=False,
    target_sk="sk-target",
    target_name="Target",
):
    """Build a ForwardHandler with custom state for one-off tests.

    Centralized so individual tests don't repeat 30 lines of mock setup.
    """
    if special_agents is None:
        special_agents = {"sk-qa": "QA Bot", "sk-coder": "Coder"}
    if tab_sessions is None:
        tab_sessions = {0: "sk-tab1", 1: "sk-tab2"}
        if not target_in_tabs:
            # If target is not in tabs, add another tab so the
            # gateway branch is exercised but the target isn't found
            pass
        else:
            # Include the target in the tab list so the
            # "select existing" path is exercised
            tab_sessions[2] = target_sk

    arh = MagicMock()
    arh.get_special_agents.return_value = special_agents
    arh.send_to_special_agent = MagicMock()

    gh = MagicMock()
    gh.agent_mgr = MagicMock()
    gh.agent_mgr.get_name = MagicMock(side_effect=lambda sk: AGENT_NAMES.get(sk) or target_name if sk == target_sk else None)
    gh._gw = MagicMock()
    gh._gw.is_connected.return_value = True
    gh._gw.send_message = MagicMock()

    mc = MagicMock()
    mc._tab_sessions = tab_sessions
    mc.create_chat_tab = MagicMock(return_value=2)
    mc._chat_notebook = MagicMock()
    mc.get_chat_box = MagicMock(return_value=MagicMock())
    mc.scroll_chat_to_bottom = MagicMock()

    crh = MagicMock()
    crh.render_sync = MagicMock(return_value=MagicMock())
    crh._on_forward_message = MagicMock()

    return ForwardHandler(
        main_content=mc,
        chat_handler=MagicMock(),
        chat_render_handler=crh,
        agent_runtime_handler=arh,
        gateway_handler=gh,
    )


# ── Tests: TestShowForwardPopover ───────────────────────────────────────────


class TestShowForwardPopover:
    """show_forward_popover(text, anchor_widget, source_session_key) builds
    a popover of every agent the user could forward to."""

    def test_includes_special_agents(self, handler):
        """Both special agents appear as buttons in the popover."""
        handler.show_forward_popover("hello", Gtk.Label(), source_session_key=None)
        _, _, buttons = _last_popover_and_buttons()
        labels = [b.get_label() for b in buttons]
        assert "→ QA Bot" in labels
        assert "→ Coder" in labels

    def test_includes_gateway_agents(self, handler):
        """Both gateway agents from open tabs appear as buttons."""
        handler.show_forward_popover("hello", Gtk.Label(), source_session_key=None)
        _, _, buttons = _last_popover_and_buttons()
        labels = [b.get_label() for b in buttons]
        assert "→ Tab Agent 1" in labels
        assert "→ Tab Agent 2" in labels

    def test_excludes_source_session_key(self, handler):
        """If source_session_key matches a special agent, that agent is excluded."""
        handler.show_forward_popover("hello", Gtk.Label(), source_session_key="sk-qa")
        _, _, buttons = _last_popover_and_buttons()
        labels = [b.get_label() for b in buttons]
        assert "→ QA Bot" not in labels
        # The other agents (special and gateway) are still present
        assert "→ Coder" in labels
        assert "→ Tab Agent 1" in labels
        assert "→ Tab Agent 2" in labels

    def test_excludes_duplicate_sessions(self):
        """If a session_key is in BOTH special_agents and gateway tabs,
        it appears only once (gateway dedup pass on the special list)."""
        h = make_handler(
            special_agents={"sk-both": "In Both Lists"},
            tab_sessions={0: "sk-both"},
        )
        h.show_forward_popover("hello", Gtk.Label(), source_session_key=None)
        _, _, buttons = _last_popover_and_buttons()
        labels = [b.get_label() for b in buttons]
        # "In Both Lists" appears exactly once
        assert labels.count("→ In Both Lists") == 1

    def test_returns_early_when_no_agents(self, handler):
        """If there are no special agents AND no open gateway tabs,
        no popover is created (silent skip)."""
        handler._agent_runtime_handler.get_special_agents.return_value = {}
        handler._main_content._tab_sessions = {}
        handler.show_forward_popover("hello", Gtk.Label(), source_session_key=None)
        assert _CapturingPopover.instances == [], (
            "expected no popover, but one was created"
        )


# ── Tests: TestForwardToAgent ──────────────────────────────────────────────


class TestForwardToAgent:
    """forward_to_agent(target_sk, text, source_sk, popover) routes the
    forwarded text to the target, opens/selects the target tab, and renders
    a forwarded bubble."""

    def test_routes_to_special_agent(self, handler):
        """When target is in get_special_agents(), send_to_special_agent
        is called with (target_sk, text)."""
        popover = MagicMock()
        handler.forward_to_agent(
            target_session_key="sk-qa",
            text="forwarded text",
            source_session_key="sk-coder",
            popover=popover,
        )
        handler._agent_runtime_handler.send_to_special_agent.assert_called_once_with(
            "sk-qa", "forwarded text"
        )
        # (SPEC-05 R1: no gateway path exists to assert against anymore.)

    def test_creates_new_tab_if_none_exists(self, handler):
        """If the target_session_key is not in _tab_sessions, create_chat_tab
        is called with the resolved target name."""
        # _tab_sessions in the fixture has sk-tab1 and sk-tab2 but NOT sk-coder
        # (sk-coder is a special agent, not an open tab)
        popover = MagicMock()
        handler.forward_to_agent(
            target_session_key="sk-coder",
            text="text",
            source_session_key="sk-qa",
            popover=popover,
        )
        handler._main_content.create_chat_tab.assert_called_once_with(
            "sk-coder", "Coder"  # resolved from get_special_agents
        )
        # The "select existing" path was NOT taken
        handler._main_content._chat_notebook.set_current_page.assert_not_called()

    def test_selects_existing_tab_if_one_exists(self, handler):
        """If the target_session_key is in _tab_sessions, _chat_notebook
        .set_current_page is called with that page index. create_chat_tab
        is NOT called."""
        # _tab_sessions has {0: sk-tab1, 1: sk-tab2}; sk-tab1 is at page 0
        popover = MagicMock()
        handler.forward_to_agent(
            target_session_key="sk-tab1",
            text="text",
            source_session_key="sk-qa",
            popover=popover,
        )
        handler._main_content._chat_notebook.set_current_page.assert_called_once_with(0)
        handler._main_content.create_chat_tab.assert_not_called()

    def test_renders_forwarded_bubble_with_forwarded_from(self, handler):
        """render_sync is called with forwarded_from set to the resolved
        source_name, and agent_name="You" (forwarded bubbles are always
        'You' from the user's perspective)."""
        popover = MagicMock()
        handler.forward_to_agent(
            target_session_key="sk-qa",
            text="forwarded text",
            source_session_key="sk-coder",
            popover=popover,
        )
        handler._chat_render_handler.render_sync.assert_called_once()
        call = handler._chat_render_handler.render_sync.call_args
        # The handler passes ("You", text, target_session_key) positionally,
        # and forwarded_from / agent_name / on_forward_click as kwargs.
        assert call.args == ("You", "forwarded text", "sk-qa"), (
            f"unexpected positional args: {call.args}"
        )
        assert call.kwargs["forwarded_from"] == "Coder"  # resolved from get_special_agents
        assert call.kwargs["agent_name"] == "You"
        # And the rendered bubble is appended to the target tab's chat box
        handler._main_content.get_chat_box.return_value.append.assert_called_once()

    def test_returns_early_when_text_empty(self, handler):
        """If text is empty (or falsy), forward_to_agent pops down the popover
        and returns without routing anything or rendering anything."""
        popover = MagicMock()
        handler.forward_to_agent(
            target_session_key="sk-qa",
            text="",
            source_session_key="sk-coder",
            popover=popover,
        )
        popover.popdown.assert_called_once()
        handler._agent_runtime_handler.send_to_special_agent.assert_not_called()
        handler._gateway_handler._gw.send_message.assert_not_called()
        handler._chat_render_handler.render_sync.assert_not_called()

    def test_popsdown_popover_before_routing(self, handler):
        """The popover.popdown() call happens FIRST in the body — even if
        routing or rendering later fails, the popover will close."""
        popover = MagicMock()
        # Make render_sync raise to simulate a downstream failure
        handler._chat_render_handler.render_sync.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError, match="boom"):
            handler.forward_to_agent(
                target_session_key="sk-qa",
                text="text",
                source_session_key="sk-coder",
                popover=popover,
            )
        # popdown still got called (it's the very first line of the body)
        popover.popdown.assert_called_once()

    def test_resolves_source_name_from_special_agents(self, handler):
        """If source_session_key is in get_special_agents(), the special-agent
        name is used as forwarded_from."""
        popover = MagicMock()
        handler.forward_to_agent(
            target_session_key="sk-tab1",
            text="text",
            source_session_key="sk-coder",
            popover=popover,
        )
        kwargs = handler._chat_render_handler.render_sync.call_args.kwargs
        assert kwargs["forwarded_from"] == "Coder"

    def test_falls_back_to_gateway_for_source_name(self, handler):
        """If source_session_key is NOT in special_agents, the gateway's
        agent_mgr.get_name() is used as forwarded_from."""
        popover = MagicMock()
        # sk-tab1 is a gateway tab; source name comes from gw.agent_mgr.get_name
        handler.forward_to_agent(
            target_session_key="sk-tab1",
            text="text",
            source_session_key="sk-tab2",
            popover=popover,
        )
        kwargs = handler._chat_render_handler.render_sync.call_args.kwargs
        assert kwargs["forwarded_from"] == "Tab Agent 2"


# ── Tests: TestForwardHandlerConstruction ───────────────────────────────────


class TestForwardHandlerConstruction:
    """The constructor's signature and attribute storage."""

    def test_kwargs_only_init(self):
        """__init__ accepts only keyword arguments (the * in the signature)."""
        sig = inspect.signature(ForwardHandler.__init__)
        # Find the var-positional (*args) marker; if it exists, kwargs-only
        # is broken. Otherwise every named param after * is keyword-only.
        assert sig.parameters.keys() and not any(
            p.kind == inspect.Parameter.VAR_POSITIONAL
            for p in sig.parameters.values()
        ), f"__init__ should not accept *args, got: {sig}"
        # Every parameter after `self` must be KEYWORD_ONLY
        for name, p in sig.parameters.items():
            if name == "self":
                continue
            assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
                f"{name} should be KEYWORD_ONLY, got {p.kind}"
            )

    def test_stores_all_five_deps(self):
        """All 5 constructor dependencies are stored as self attributes."""
        main_content = MagicMock()
        chat_handler = MagicMock()
        chat_render_handler = MagicMock()
        arh = MagicMock()
        gh = MagicMock()
        h = ForwardHandler(
            main_content=main_content,
            chat_handler=chat_handler,
            chat_render_handler=chat_render_handler,
            agent_runtime_handler=arh,
            gateway_handler=gh,
        )
        assert h._main_content is main_content
        assert h._chat_handler is chat_handler
        assert h._chat_render_handler is chat_render_handler
        assert h._agent_runtime_handler is arh
        assert h._gateway_handler is gh
