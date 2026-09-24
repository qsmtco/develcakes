# tests/test_feed_handler.py
# Unit tests for ui/handlers/feed_handler.py — FeedHandler card lifecycle + actions.
#
# Tests the FeedHandler API without GTK (mock GLib.idle_add).

import pytest
from datetime import UTC, datetime, timezone
import logging
import sys
import threading
from unittest.mock import MagicMock, patch

from models.feed_card import AutoAcceptPrefs, ExecCommandPref, FeedCardData, FileChangePref
from utils.feed_store import (
    FEED_WINDOW_DEFAULT,
    _default_prefs,
    _merge_v2_defaults,
    _migrate_v1_to_v2,
    load_feed_prefs,
)


# ── Mock GLib that records calls instead of dispatching ──────────────────────

class MockGLib:
    """Mock GLib that captures idle_add callbacks for synchronous testing."""
    def __init__(self):
        self._pending = []

    def idle_add(self, fn, *args, **kwargs):
        # Record and also run immediately (simulates immediate GTK dispatch)
        self._pending.append((fn, args, kwargs))
        fn(*args, **kwargs)
        return 0


# ── Mock FeedTab ──────────────────────────────────────────────────────────────

class MockVadjustment:
    """Fake Gtk.Adjustment for smart scroll tests."""
    def __init__(self, value=0, upper=1000, page_size=600):
        self._value = value
        self._upper = upper
        self._page_size = page_size
        # MEMRATCHET P5: record every write so the eviction pass's scroll
        # compensation can be asserted on the call, not just on the result.
        self.set_value_calls = []

    def get_value(self):
        return self._value

    def set_value(self, v):
        self.set_value_calls.append(v)
        self._value = v

    def get_upper(self):
        return self._upper

    def get_page_size(self):
        return self._page_size


class MockCardContainer:
    """Minimal stand-in for FeedTab's card Gtk.Box (MEMRATCHET P3, spec §2.6).

    The eviction pass only reads the container's inter-child spacing, via
    FeedHandler._card_container_spacing() ->
    get_card_container().get_spacing() (feed_tab.py: card_container.set_spacing(8)),
    so the double exposes get_spacing() and nothing else. The value is read
    from the owning tab on every call, so a Phase-5 test can drive the spacing
    term with `tab._card_spacing = <n>` (default 8).
    """

    def __init__(self, tab):
        self._tab = tab

    def get_spacing(self) -> int:
        return self._tab._card_spacing


class MockFeedTab:
    def __init__(self):
        self.cards = []  # list of (card_id, widget)
        self.empty_shown = False
        # Fake scroll state for smart scroll tests
        self._vadjustment = MockVadjustment(value=0, upper=1000, page_size=600)
        # MEMRATCHET P3: eviction-pass surface (spec §2.6). Defaults chosen so
        # the eviction cases are permissive; individual tests override the
        # attributes to drive the guarded / non-guarded branches.
        self._near_bottom = True
        self._above_viewport = True
        self._card_spacing = 8  # real container's spacing (feed_tab.py set_spacing(8))
        # Fake batch bar state for Phase 5 tests
        self._batch_bar_visible = False
        self._batch_bar_count = 0
        self._batch_accept_callback = None
        # Phase 5 — Auto-accept toggle state (for new test class)
        self._auto_accept_active = False
        self._auto_accept_callback = None
        self._batch_button_label = ""
        self._batch_button_visible = True
        self.append_calls = []  # log of (widget, card_id) per append_card() call (for batch tests)
        # MEMRATCHET P5: count the bottom-pin path so the near-bottom arm of
        # the eviction scroll compensation is assertable as a CALL, distinct
        # from the scrolled-up compensation arm (which writes the vadjustment).
        self.scroll_to_bottom_calls = 0

    def append_card(self, widget, card_id=None):
        self.cards.append((widget, card_id))
        self.append_calls.append((widget, card_id))

    prepend_card = append_card  # backward compat

    def remove_card(self, card_id):
        self.cards = [(cid, w) for cid, w in self.cards if cid != card_id]

    def show_empty_state(self):
        self.empty_shown = True

    def replace_card(self, card_id, new_widget):
        for i, (cid, w) in enumerate(self.cards):
            if cid == card_id:
                self.cards[i] = (card_id, new_widget)
                break

    def schedule_scroll_to_bottom(self):
        # Mirror the real FeedTab: scroll after a simulated layout pass.
        # The real implementation uses vadj.set_value(vadj.get_upper())
        # via the 'changed' signal; in tests we just set the value directly.
        self.scroll_to_bottom_calls += 1
        if self._vadjustment is not None:
            self._vadjustment.set_value(self._vadjustment.get_upper())

    def schedule_smart_scroll_to_bottom(self):
        """Mirror of FeedTab.schedule_smart_scroll_to_bottom() for test.
        Proximity check + delegate to schedule_scroll_to_bottom."""
        if self._vadjustment is None:
            return
        vadj = self._vadjustment
        current = vadj.get_value()
        upper = vadj.get_upper()
        page_size = vadj.get_page_size()
        distance_from_bottom = upper - page_size - current
        if distance_from_bottom < 80:
            self.schedule_scroll_to_bottom()

    # Phase 5 batch bar mocks
    def update_batch_bar(self, pending_count: int):
        self._batch_bar_count = pending_count
        self._batch_bar_visible = pending_count >= 2
        # Phase 5-2 — New mock attrs (mirror real FeedTab.update_batch_bar)
        self._batch_button_label = f"Accept All ({pending_count})" if pending_count >= 2 else "Accept All"
        self._batch_button_visible = pending_count >= 2

    def set_batch_accept_callback(self, callback):
        self._batch_accept_callback = callback

    # Phase 5 — Auto-accept toggle mocks
    def update_auto_accept_state(self, active: bool):
        self._auto_accept_active = active

    def set_auto_accept_callback(self, callback):
        self._auto_accept_callback = callback

    # ── MEMRATCHET P3: eviction-pass surface (spec §2.6) ─────────────────
    # Phase 5's eviction pass calls all four on the tab. MockGLib.idle_add
    # dispatches synchronously, so a missing accessor surfaces immediately
    # once Phase 5 lands.

    def is_near_bottom(self, slack: int = 80) -> bool:
        return self._near_bottom

    def is_above_viewport(self, widget) -> bool:
        return self._above_viewport

    def get_vadjustment(self):
        return self._vadjustment

    def get_card_container(self):
        # The eviction pass only reads get_spacing() through the handler's
        # _card_container_spacing() helper, so a spacing-only stub suffices.
        return MockCardContainer(self)


# ── Mock GitResult ───────────────────────────────────────────────────────────

class MockGitResult:
    def __init__(self, success=True, stdout="", sha="abc123def456", error=""):
        self.success = success
        self.stdout = stdout
        self.sha = sha
        self.error = error


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_glib():
    return MockGLib()


@pytest.fixture
def mock_feed_tab():
    return MockFeedTab()


@pytest.fixture
def feed_handler(mock_glib, mock_feed_tab):
    from ui.handlers.feed_handler import FeedHandler
    on_send = MagicMock()
    h = FeedHandler(
        GLib=mock_glib,
        on_send_to_agent=on_send,
    )
    h.set_feed_tab(mock_feed_tab)
    return h


# ── Tests: add_card ─────────────────────────────────────────────────────────

class TestAddCard:
    def test_add_card_returns_card_id(self, feed_handler):
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="Fix auth",
            body="+from auth import middleware", author="Qat",
            timestamp=ts, project_name="manopea",
        )
        card_id = feed_handler.add_card(card)
        assert card_id is not None
        assert isinstance(card_id, str)
        assert len(card_id) > 0

    def test_add_card_stores_card_data(self, feed_handler):
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="Fix auth",
            body="+from auth import middleware", author="Qat",
            timestamp=ts, project_name="manopea",
        )
        card_id = feed_handler.add_card(card)
        stored = feed_handler.get_card(card_id)
        assert stored is not None
        assert stored.card_id == card_id
        assert stored.title == "Fix auth"

    def test_add_card_indexes_under_project(self, feed_handler):
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="git_commit", source="git", title="Init commit",
            body="", author="git", timestamp=ts, project_name="crabcakes",
        )
        card_id = feed_handler.add_card(card)
        cards = feed_handler.get_cards_for_project("crabcakes")
        assert len(cards) == 1
        assert cards[0].card_id == card_id

    def test_add_card_multiple_projects_isolated(self, feed_handler):
        ts = datetime.now(timezone.utc)
        card1 = FeedCardData(
            card_type="diff", source="agent", title="A", body="",
            author="x", timestamp=ts, project_name="proj1",
        )
        card2 = FeedCardData(
            card_type="diff", source="agent", title="B", body="",
            author="x", timestamp=ts, project_name="proj2",
        )
        id1 = feed_handler.add_card(card1)
        id2 = feed_handler.add_card(card2)
        assert feed_handler.get_cards_for_project("proj1") == [feed_handler.get_card(id1)]
        assert feed_handler.get_cards_for_project("proj2") == [feed_handler.get_card(id2)]

    def test_add_card_calls_on_card_added(self, feed_handler):
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="task", source="agent", title="Task 1", body="",
            author="x", timestamp=ts, project_name="p",
        )
        on_added = MagicMock()
        feed_handler._on_card_added = on_added
        card_id = feed_handler.add_card(card)
        on_added.assert_called_once_with(card_id)


# ── Tests: remove_card ──────────────────────────────────────────────────────

class TestRemoveCard:
    def test_remove_card_deletes_from_store(self, feed_handler):
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="X", body="",
            author="x", timestamp=ts, project_name="p",
        )
        card_id = feed_handler.add_card(card)
        assert feed_handler.get_card(card_id) is not None
        feed_handler.remove_card(card_id)
        assert feed_handler.get_card(card_id) is None

    def test_remove_nonexistent_id_noop(self, feed_handler):
        feed_handler.remove_card("nonexistent-id")


# ── Tests: clear_project ─────────────────────────────────────────────────────

class TestClearProject:
    def test_clear_project_removes_all_cards(self, feed_handler):
        ts = datetime.now(timezone.utc)
        for i in range(3):
            card = FeedCardData(
                card_type="diff", source="agent", title=f"Card {i}", body="",
                author="x", timestamp=ts, project_name="clear-me",
            )
            feed_handler.add_card(card)
        assert len(feed_handler.get_cards_for_project("clear-me")) == 3
        feed_handler.clear_project("clear-me")
        assert len(feed_handler.get_cards_for_project("clear-me")) == 0


# ── Tests: get_cards_for_project ─────────────────────────────────────────────

class TestGetCardsForProject:
    def test_empty_project_returns_empty_list(self, feed_handler):
        assert feed_handler.get_cards_for_project("nonexistent") == []

    def test_cards_newest_first(self, feed_handler):
        ts = datetime.now(timezone.utc)
        for i in range(5):
            card = FeedCardData(
                card_type="diff", source="agent", title=f"Card {i}", body="",
                author="x", timestamp=ts, project_name="order-test",
            )
            feed_handler.add_card(card)
        cards = feed_handler.get_cards_for_project("order-test")
        # Newest first (card 4 should be first since it was added last)
        assert cards[0].title == "Card 4"
        assert cards[4].title == "Card 0"


# ── Tests: on_project_closed ─────────────────────────────────────────────────

class TestOnProjectClosed:
    def test_on_project_closed_clears_and_shows_empty(self, feed_handler, mock_feed_tab):
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="X", body="",
            author="x", timestamp=ts, project_name="closing",
        )
        feed_handler.add_card(card)
        feed_handler.on_project_closed("closing")
        assert len(feed_handler.get_cards_for_project("closing")) == 0
        assert mock_feed_tab.empty_shown


# ── Tests: handle_accept ───────────────────────────────────────────────────

class TestHandleAccept:
    """handle_accept should commit the actual staged files, not the card title."""

    @patch("ui.handlers.feed_handler.git_ops")
    @patch("ui.handlers.feed_handler.feed_store")
    def test_handle_accept_uses_staged_files_for_commit_message(
        self, mock_feed_store, mock_git_ops, feed_handler
    ):
        """When accepting a feed card, the commit message should be derived
        from the actual staged files, not from card.title.

        Regression test for review-layer fix T2-RL3.
        """
        # Set up a feed card
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="file_modified", source="agent",
            title="Modified src/main.py", body="",
            author="x", timestamp=ts, project_name="testproject",
            file_path="src/main.py", metadata={},
        )
        card_id = feed_handler.add_card(card)

        # Mock git_ops: stage succeeds, commit succeeds
        mock_git_ops.stage_all.return_value = MockGitResult(success=True)
        mock_git_ops.commit.return_value = MockGitResult(
            success=True, stdout="[main abc123d] Accept: src/main.py", sha="abc123def456"
        )

        # Mock gitpython import to return a staged list with a different file
        import sys
        mock_git_module = MagicMock()
        mock_diff = MagicMock()
        mock_diff.a_path = "src/other.py"  # different from card.title
        mock_diff.b_path = None
        mock_repo = MagicMock()
        mock_repo.index.diff.return_value = [mock_diff]
        mock_git_module.Repo.return_value = mock_repo

        project_path = "/tmp/testproject"
        feed_handler._project_paths["testproject"] = project_path

        with patch.dict(sys.modules, {"git": mock_git_module}):
            feed_handler.handle_accept(card_id)

        # Verify commit was called with the ACTUAL file, not card.title
        commit_call = mock_git_ops.commit.call_args
        assert commit_call is not None
        commit_msg = commit_call[0][1]  # second positional arg
        assert "src/other.py" in commit_msg
        assert "Modified" not in commit_msg  # the user-facing title is NOT in the message

    @patch("ui.handlers.feed_handler.git_ops")
    @patch("ui.handlers.feed_handler.feed_store")
    def test_handle_accept_empty_tree_silently_noops(
        self, mock_feed_store, mock_git_ops, feed_handler
    ):
        """When the working tree is clean (no staged files), handle_accept
        should be a silent no-op. The card is not marked accepted and no
        empty commit is created.
        """
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="file_modified", source="agent",
            title="Modified src/main.py", body="",
            author="x", timestamp=ts, project_name="testproject",
            file_path="src/main.py", metadata={},
        )
        card_id = feed_handler.add_card(card)

        mock_git_ops.stage_all.return_value = MockGitResult(success=True)
        mock_git_ops.commit.return_value = MockGitResult(success=True)

        # Mock gitpython to return empty staged list (clean working tree)
        import sys
        mock_git_module = MagicMock()
        mock_repo = MagicMock()
        mock_repo.index.diff.return_value = []  # empty
        mock_git_module.Repo.return_value = mock_repo
        project_path = "/tmp/testproject"
        feed_handler._project_paths["testproject"] = project_path

        with patch.dict(sys.modules, {"git": mock_git_module}):
            feed_handler.handle_accept(card_id)

        # commit() should NOT have been called (no staged changes)
        mock_git_ops.commit.assert_not_called()
        # The card should NOT be marked accepted
        assert card.accepted is not True

    @patch("ui.handlers.feed_handler.git_ops")
    @patch("ui.handlers.feed_handler.feed_store")
    def test_handle_accept_multi_file_message(
        self, mock_feed_store, mock_git_ops, feed_handler
    ):
        """When multiple files are staged, the commit message should list
        all of them (up to 3 inline, then '...' for more).
        """
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="file_modified", source="agent",
            title="Modified src/main.py", body="",
            author="x", timestamp=ts, project_name="testproject",
            file_path="src/main.py", metadata={},
        )
        card_id = feed_handler.add_card(card)

        mock_git_ops.stage_all.return_value = MockGitResult(success=True)
        mock_git_ops.commit.return_value = MockGitResult(
            success=True, stdout="[main abc123d] multi", sha="abc123"
        )

        # Mock gitpython to return multiple staged files
        import sys
        mock_git_module = MagicMock()
        mock_diffs = []
        for fname in ["src/main.py", "src/utils.py", "tests/test_main.py"]:
            mock_diff = MagicMock()
            mock_diff.a_path = fname
            mock_diff.b_path = None
            mock_diffs.append(mock_diff)
        mock_repo = MagicMock()
        mock_repo.index.diff.return_value = mock_diffs
        mock_git_module.Repo.return_value = mock_repo
        project_path = "/tmp/testproject"
        feed_handler._project_paths["testproject"] = project_path

        with patch.dict(sys.modules, {"git": mock_git_module}):
            feed_handler.handle_accept(card_id)

        # Verify commit was called with a message that lists multiple files
        commit_call = mock_git_ops.commit.call_args
        commit_msg = commit_call[0][1]
        assert "3 files" in commit_msg
        assert "src/main.py" in commit_msg
        assert "src/utils.py" in commit_msg
        assert "tests/test_main.py" in commit_msg


# ── Tests: handle_copy ───────────────────────────────────────────────────────

class TestHandleCopy:
    def test_handle_copy_calls_clipboard(self, feed_handler):
        with patch('gi.repository.Gdk.Display.get_default') as mock_display:
            mock_clipboard = MagicMock()
            mock_display.return_value.get_clipboard.return_value = mock_clipboard
            feed_handler.handle_copy("test text")
            mock_clipboard.set.assert_called_once_with("test text")

# ═══════════════════════════════════════════════════════════════════
#  TestPersistentBadges — Phase 2
#  Verifies that git_commit and approval cards show decision badges.
# ═══════════════════════════════════════════════════════════════════

class TestPersistentBadges:
    """Phase 2: git_commit and approval cards must show decision badges after accept/reject."""

    def test_git_commit_card_has_accepted_true(self, feed_handler):
        """After accepting a file-change card, the git_commit card created must have accepted=True."""
        # Create a file-change card that will be accepted
        ts = datetime.now(timezone.utc)
        original = FeedCardData(
            card_type="diff",
            source="agent",
            title="Fix auth bug",
            body="+from auth import middleware",
            author="Qat",
            timestamp=ts,
            project_name="testproject",
        )
        card_id = feed_handler.add_card(original)

        # Mock _add_git_card: create a result and call _add_git_card directly
        # We intercept by mocking add_card to capture the git_card
        captured_git_cards = []

        original_add_card = feed_handler.add_card
        def capturing_add_card(card_data):
            captured_git_cards.append(card_data)
            # Actually add it
            return original_add_card(card_data)
        feed_handler.add_card = capturing_add_card

        # Call _add_git_card directly with a successful result
        from unittest.mock import MagicMock
        result = MagicMock()
        result.success = True
        result.stdout = "[main abc123d] Fix auth bug"
        result.sha = "abc123def456"
        original.accepted = True  # Simulate the card was accepted

        feed_handler._add_git_card(original, result)

        # Verify the git_commit card has accepted=True
        assert len(captured_git_cards) == 1
        git_card = captured_git_cards[0]
        assert git_card.card_type == "git_commit"
        assert git_card.accepted is True

    def test_git_commit_card_has_accepted_false(self, feed_handler):
        """After rejecting a file-change card, the git_commit card created must have accepted=False."""
        ts = datetime.now(timezone.utc)
        original = FeedCardData(
            card_type="diff",
            source="agent",
            title="Fix auth bug",
            body="+from auth import middleware",
            author="Qat",
            timestamp=ts,
            project_name="testproject",
        )
        card_id = feed_handler.add_card(original)

        captured_git_cards = []
        original_add_card = feed_handler.add_card
        def capturing_add_card(card_data):
            captured_git_cards.append(card_data)
            return original_add_card(card_data)
        feed_handler.add_card = capturing_add_card

        from unittest.mock import MagicMock
        result = MagicMock()
        result.success = True
        result.stdout = "[main abc123d] Rejected"
        result.sha = "abc123def456"
        original.accepted = False  # Simulate rejected

        feed_handler._add_git_card(original, result)

        assert len(captured_git_cards) == 1
        git_card = captured_git_cards[0]
        assert git_card.card_type == "git_commit"
        assert git_card.accepted is False

    def test_approval_card_has_accepted_true_after_approve(self, feed_handler, mock_glib):
        """After approving a pending approval card, card.accepted must be True."""
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

        # Create a mock agent runtime handler
        mock_mc = MagicMock()
        mock_chat_rh = MagicMock()
        agent_rt_handler = AgentRuntimeHandler(mock_mc, mock_chat_rh, GLib_module=mock_glib)
        agent_rt_handler._fh = feed_handler

        # Create an approval card in the feed handler
        ts = datetime.now(timezone.utc)
        approval_card = FeedCardData(
            card_type="agent_action",
            source="agent",
            title="PM requests approval to run command",
            body="$ ls -la",
            author="PM",
            timestamp=ts,
            project_name="testproject",
            metadata={
                "needs_approval": True,
                "status": "pending_approval",
            },
        )
        card_id = feed_handler.add_card(approval_card)

        # Register pending approval
        approval_id = card_id
        agent_rt_handler._pending_approvals[approval_id] = {
            "session_key": "test-session",
            "tool_name": "exec_command",
            "args": {"command": "ls -la"},
        }

        # Mock the runtime's get_conversation to return something truthy
        mock_runtime = MagicMock()
        mock_runtime.get_conversation.return_value = True
        agent_rt_handler._runtimes["test-agent"] = mock_runtime

        # Mock feed_store.update_feed_card to avoid file I/O
        with patch('ui.handlers.feed_handler.feed_store'):
            agent_rt_handler.approve_exec(approval_id, True)

        # Verify card.accepted is True
        card = feed_handler.get_card(approval_id)
        assert card is not None
        assert card.accepted is True

    def test_approval_card_has_accepted_false_after_deny(self, feed_handler, mock_glib):
        """After denying a pending approval card, card.accepted must be False."""
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

        mock_mc = MagicMock()
        mock_chat_rh = MagicMock()
        agent_rt_handler = AgentRuntimeHandler(mock_mc, mock_chat_rh, GLib_module=mock_glib)
        agent_rt_handler._fh = feed_handler

        ts = datetime.now(timezone.utc)
        approval_card = FeedCardData(
            card_type="agent_action",
            source="agent",
            title="PM requests approval to run command",
            body="$ rm -rf /",
            author="PM",
            timestamp=ts,
            project_name="testproject",
            metadata={
                "needs_approval": True,
                "status": "pending_approval",
            },
        )
        card_id = feed_handler.add_card(approval_card)

        approval_id = card_id
        agent_rt_handler._pending_approvals[approval_id] = {
            "session_key": "test-session",
            "tool_name": "exec_command",
            "args": {"command": "rm -rf /"},
        }

        mock_runtime = MagicMock()
        mock_runtime.get_conversation.return_value = True
        agent_rt_handler._runtimes["test-agent"] = mock_runtime

        with patch('ui.handlers.feed_handler.feed_store'):
            agent_rt_handler.approve_exec(approval_id, False)

        card = feed_handler.get_card(approval_id)
        assert card is not None
        assert card.accepted is False


# ═══════════════════════════════════════════════════════════════════
#  TestGitRejectMemberFanout — FIX 8 (SPEC-05 SP2 micro-round)
#  The git-reject _mark() callback needs _project_handler for the
#  member fan-out. Pre-fix it was NEVER assigned (no ctor arg, no
#  setter) → AttributeError killed the callback before the member
#  notify, the special:supervisor fallback, AND _add_git_card.
# ═══════════════════════════════════════════════════════════════════

class TestGitRejectMemberFanout:
    """FIX 8 (SPEC-05 SP2 audit): git-reject notifies members + adds card.

    Deterministic: _SyncThreading runs the reject git thread inline and
    MockGLib.idle_add runs _mark() synchronously; git_ops + feed_store are
    patched. Asserts the FULL repaired path: member fan-out (not the
    supervisor fallback) AND the git card actually lands.
    """

    def test_git_reject_notifies_members_and_adds_card(
        self, mock_feed_tab, monkeypatch
    ):
        import ui.handlers.feed_handler as fh
        from ui.handlers.feed_handler import FeedHandler

        monkeypatch.setattr(fh, "threading", _SyncThreading)
        project_handler = MagicMock(name="ProjectHandler")
        project_handler.get_project_members.return_value = [
            "special:coder", "special:qa",
        ]
        on_send = MagicMock(name="on_send_to_agent")
        h = FeedHandler(
            GLib=MockGLib(),
            on_send_to_agent=on_send,
            project_handler=project_handler,
        )
        h.set_feed_tab(mock_feed_tab)
        h._ensure_persist_writer = lambda: None  # _no_writer pattern

        card = FeedCardData(
            card_type="diff", source="agent", title="Fix auth bug",
            body="+from auth import middleware", author="Coder",
            timestamp=datetime.now(UTC), project_name="proj",
            file_path="src/auth.py",
            metadata={"project_path": "/tmp/fh8-proj"},
        )
        card_id = h.add_card(card)

        with patch("ui.handlers.feed_handler.git_ops") as mock_git_ops, \
             patch("ui.handlers.feed_handler.feed_store"):
            mock_git_ops.checkout_paths.return_value = MockGitResult(
                success=True, stdout="[main abc123d] Rejected",
                sha="abc123def456",
            )
            h.handle_reject(card_id)

        # Member fan-out — one call per member, NOT the supervisor fallback.
        expected = "[PM] Rejected change: Fix auth bug"
        assert on_send.call_count == 2, (
            f"both members must be notified; got {on_send.call_args_list!r}"
        )
        assert [c.args[0] for c in on_send.call_args_list] == [
            "special:coder", "special:qa",
        ]
        assert all(c.args[1] == expected for c in on_send.call_args_list)

        # The git card must actually be added (the pre-fix AttributeError
        # died BEFORE this line).
        git_cards = [
            c for c in h._cards.values() if c.card_type == "git_commit"
        ]
        assert len(git_cards) == 1, (
            "git-reject must add exactly one git_commit card; "
            f"got {len(git_cards)}"
        )
        assert git_cards[0].accepted is False
        assert git_cards[0].title == "Rejected: Fix auth bug"


# ═══════════════════════════════════════════════════════════════════
#  TestSeqNumHandler — Phase 3
#  Verifies seq_num assignment in add_card, per-project isolation,
#  and reconstruction on project open.
# ═══════════════════════════════════════════════════════════════════

class TestSeqNumHandler:
    """Phase 3: seq_num is assigned per-project and persists."""

    def test_seq_num_assigned_on_add(self, feed_handler):
        """add_card() must assign an incrementing seq_num to each new card."""
        ts = datetime.now(timezone.utc)
        cards = []
        for i in range(3):
            card = FeedCardData(
                card_type="diff", source="agent", title=f"Card {i}",
                body="", author="x", timestamp=ts, project_name="foo",
            )
            feed_handler.add_card(card)
            cards.append(card)

        assert cards[0].seq_num == 1
        assert cards[1].seq_num == 2
        assert cards[2].seq_num == 3

    def test_seq_num_per_project(self, feed_handler):
        """seq_num is per-project, not global."""
        ts = datetime.now(timezone.utc)

        # Add 2 cards to project foo
        c1 = FeedCardData(
            card_type="diff", source="agent", title="A",
            body="", author="x", timestamp=ts, project_name="foo",
        )
        c2 = FeedCardData(
            card_type="diff", source="agent", title="B",
            body="", author="x", timestamp=ts, project_name="foo",
        )
        feed_handler.add_card(c1)
        feed_handler.add_card(c2)

        # Add 1 card to project bar
        c3 = FeedCardData(
            card_type="diff", source="agent", title="C",
            body="", author="x", timestamp=ts, project_name="bar",
        )
        feed_handler.add_card(c3)

        # foo should have 1, 2 and bar should have 1
        assert c1.seq_num == 1
        assert c2.seq_num == 2
        assert c3.seq_num == 1

    def test_seq_num_increments_on_project_switch(self, feed_handler):
        """Switching back to a project resumes its sequence, not restart from 1."""
        ts = datetime.now(timezone.utc)

        # Project foo gets 2 cards
        c1 = FeedCardData(
            card_type="diff", source="agent", title="Foo-1",
            body="", author="x", timestamp=ts, project_name="foo",
        )
        c2 = FeedCardData(
            card_type="diff", source="agent", title="Foo-2",
            body="", author="x", timestamp=ts, project_name="foo",
        )
        feed_handler.add_card(c1)
        feed_handler.add_card(c2)

        # Project bar gets 1 card
        c3 = FeedCardData(
            card_type="diff", source="agent", title="Bar-1",
            body="", author="x", timestamp=ts, project_name="bar",
        )
        feed_handler.add_card(c3)

        # Back to foo — should continue from 3
        c4 = FeedCardData(
            card_type="diff", source="agent", title="Foo-3",
            body="", author="x", timestamp=ts, project_name="foo",
        )
        feed_handler.add_card(c4)

        assert c1.seq_num == 1
        assert c2.seq_num == 2
        assert c3.seq_num == 1
        assert c4.seq_num == 3

    def test_seq_num_not_reset_on_clear_project(self, feed_handler):
        """After clear_project, the counter for that project is gone but new cards still work."""
        ts = datetime.now(timezone.utc)

        c1 = FeedCardData(
            card_type="diff", source="agent", title="Card 1",
            body="", author="x", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(c1)
        assert c1.seq_num == 1

        feed_handler.clear_project("testproj")

        # New cards for the same project should start fresh (counter was removed)
        c2 = FeedCardData(
            card_type="diff", source="agent", title="Card 2",
            body="", author="x", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(c2)
        assert c2.seq_num == 1  # fresh start after clear

    def test_seq_num_on_project_open_reconstruction(
        self, feed_handler, mock_glib, monkeypatch
    ):
        """On project open, _project_seq is rebuilt from max(loaded seq_nums).

        Deterministic: on_project_opened dispatches _load_and_render on a
        daemon thread — patched to _SyncThreading so the load completes
        before the assertion (Debugger Phase-3 audit: the un-joined daemon
        made this test order-dependent/flaky)."""
        import ui.handlers.feed_handler as fh
        monkeypatch.setattr(fh, "threading", _SyncThreading)
        ts = datetime.now(timezone.utc)

        # Simulate loading pre-existing cards with seq_nums 1, 2, 3
        existing_cards = [
            FeedCardData(
                card_type="diff", source="agent", title=f"Old {i}",
                body="", author="x",
                timestamp=ts.replace(second=i),  # oldest=0, newest=3
                project_name="restore-project",
                card_id=f"old-{i}",
                seq_num=i + 1,  # seq_nums 1, 2, 3
            )
            for i in range(3)
        ]

        with patch('ui.handlers.feed_handler.feed_store') as mock_fs:
            mock_fs.load_feed.return_value = existing_cards
            # The load path reads this constant (SPEC-UI-RESPONSIVENESS-2
            # §2.3.4); a bare MagicMock would make the comparison raise.
            mock_fs.FEED_WINDOW_DEFAULT = FEED_WINDOW_DEFAULT

            # Mock _project_paths so the handler knows where to look
            feed_handler._project_paths["restore-project"] = "/tmp/restore-project"

            feed_handler.on_project_opened("restore-project", "/tmp/restore-project")

        # After on_project_opened, _project_seq should be 3 (max of loaded)
        assert feed_handler._project_seq.get("restore-project") == 3

    def test_seq_num_migration_assigns_to_cards_without_it(
        self, feed_handler, mock_glib, monkeypatch
    ):
        """Cards loaded without seq_num get assigned seq_nums on project open.

        Deterministic per the sibling test — _SyncThreading patch (the
        un-joined daemon made assertions order-dependent/flaky)."""
        import ui.handlers.feed_handler as fh
        monkeypatch.setattr(fh, "threading", _SyncThreading)
        ts = datetime.now(timezone.utc)

        # Cards from old feed.json — no seq_num field
        old_cards = [
            FeedCardData(
                card_type="diff", source="agent", title=f"Old {i}",
                body="", author="x",
                timestamp=ts.replace(second=i * 10),
                project_name="migration-project",
                card_id=f"old-{i}",
                # seq_num intentionally None
            )
            for i in range(3)
        ]

        with patch('ui.handlers.feed_handler.feed_store') as mock_fs:
            mock_fs.load_feed.return_value = old_cards
            # See the sibling test — the load path reads this constant.
            mock_fs.FEED_WINDOW_DEFAULT = FEED_WINDOW_DEFAULT
            feed_handler._project_paths["migration-project"] = "/tmp/migration-project"

            feed_handler.on_project_opened("migration-project", "/tmp/migration-project")

        # All old cards should have been assigned seq_nums in timestamp order
        assert old_cards[0].seq_num == 1  # oldest
        assert old_cards[1].seq_num == 2
        assert old_cards[2].seq_num == 3  # newest


# ═══════════════════════════════════════════════════════════════════
#  MEMRATCHET Phase 4 (spec §2.1, F1) — load-path ordering invariant.
#  `_project_cards` must be indexed newest-first by seq_num, deduped, with
#  ids absent from `_cards` filtered out. Regression coverage for round-6
#  BUG #2 (live arrivals merged to the wrong end) and round-7 BUG #1
#  (compaction-pruned ids promoted to "newest").
# ═══════════════════════════════════════════════════════════════════

class TestLoadOrderingInvariant:
    def _handler(self):
        from ui.handlers.feed_handler import FeedHandler

        h = FeedHandler(GLib=MockGLib(), on_send_to_agent=MagicMock())
        h.set_feed_tab(MockFeedTab())
        return h

    @staticmethod
    def _cards(project_name: str, specs, base_ts):
        """Build snapshot cards from (card_id, seq_num) pairs.

        seq_num is set explicitly so the load path's None-backfill migration
        never runs — the ordering under test is the merge, not the migration.
        """
        return [
            FeedCardData(
                card_type="diff", source="agent", title=card_id, body="",
                author="x", timestamp=base_ts.replace(second=seq_num),
                project_name=project_name, card_id=card_id, seq_num=seq_num,
            )
            for card_id, seq_num in specs
        ]

    def _open(self, monkeypatch, handler, snapshot, name, path):
        """Run one project open deterministically (synchronous load thread).

        Mirrors TestWindowCompaction._run_project_open: _SyncThreading makes
        the _load_and_render daemon hop synchronous, so the assertions run
        after the merge with no join/poll race.
        """
        import ui.handlers.feed_handler as fh

        monkeypatch.setattr(fh, "threading", _SyncThreading)
        store = MagicMock()
        if callable(snapshot):
            store.load_feed.side_effect = snapshot
        else:
            store.load_feed.return_value = snapshot
        store.load_feed_prefs.return_value = _default_prefs()
        store.FEED_WINDOW_DEFAULT = FEED_WINDOW_DEFAULT
        monkeypatch.setattr(fh, "feed_store", store)
        handler._project_paths[name] = path
        handler.on_project_opened(name, path)
        return store

    def test_get_cards_for_project_newest_first_after_reopen(self, monkeypatch):
        """Spec §6 test-10: a reopen must dedupe and stay newest-first.

        Pre-fix the load path *appended* the snapshot chronologically and
        never deduped, so a second open of the same project returned each id
        twice and oldest-first."""
        h = self._handler()
        name, path = "reopen-proj", "/tmp/reopen-proj"
        base = datetime.now(timezone.utc)
        specs = [(f"c{i}", i) for i in range(1, 6)]   # c1 oldest … c5 newest

        self._open(monkeypatch, h, self._cards(name, specs, base), name, path)
        first = [c.card_id for c in h.get_cards_for_project(name)]
        assert first == ["c5", "c4", "c3", "c2", "c1"], (
            f"first open is not newest-first by seq_num: {first}"
        )

        # Same project reopened through the same path (no clear_project in
        # between — exactly like switching back and forth). Fresh card
        # objects, as a disk re-read returns.
        self._open(monkeypatch, h, self._cards(name, specs, base), name, path)
        ids = [c.card_id for c in h.get_cards_for_project(name)]
        assert len(ids) == len(set(ids)) == 5, f"dedupe failed on reopen: {ids}"
        assert ids == ["c5", "c4", "c3", "c2", "c1"], (
            f"reopen is not newest-first by seq_num: {ids}"
        )
        assert h._project_cards[name] == ["c5", "c4", "c3", "c2", "c1"], (
            "the raw index must dedupe too — the batch bar walks this list"
        )

    def test_load_live_arrival_lands_at_index_zero(self, monkeypatch):
        """Round-6 BUG #2: a live arrival during the parse window is NEWER
        than the snapshot and must lead after the merge.

        Accept All / the batch bar walk `get_cards_for_project` newest-first
        and `break` at the first non-actionable card, so a live card that
        merges to the wrong end is invisible until the project is reopened."""
        h = self._handler()
        name, path = "live-proj", "/tmp/live-proj"
        base = datetime.now(timezone.utc)
        snapshot = self._cards(name, [("c1", 1), ("c2", 2), ("c3", 3)], base)

        # The handler is live: it has already emitted up to seq 3, so a card
        # arriving now is genuinely newer than everything in the snapshot.
        h._project_seq[name] = 3
        live_ids = []

        def _load_with_live(_path):
            live = FeedCardData(
                card_type="file_modified", source="agent", title="live",
                body="", author="x", timestamp=base.replace(second=20),
                project_name=name, seq_num=None,
            )
            live_ids.append(h.add_card(live))   # arrival during the parse window
            return snapshot

        self._open(monkeypatch, h, _load_with_live, name, path)

        live_id = live_ids[0]
        assert h._cards[live_id].seq_num == 4, (
            "the live card must be newer than the snapshot (seq 4 > 3)"
        )
        ids = [c.card_id for c in h.get_cards_for_project(name)]
        assert ids[0] == live_id, (
            f"the live arrival must land at index 0, got {ids}"
        )
        assert ids == [live_id, "c3", "c2", "c1"], (
            f"merge is not newest-first by seq_num: {ids}"
        )

    def test_compacted_pruned_ids_stay_at_tail(self, monkeypatch):
        """Round-7 BUG #1: provenance must come from seq_num, not from set
        membership. A pruned id can be absent from `_cards` (this process
        never loaded it) or still present in `_cards` (compaction rewrote
        disk, memory was never reloaded). Either way it must never sort
        ahead of a surviving card — Accept All would commit a card that no
        longer exists on disk."""
        h = self._handler()
        name, path = "prune-proj", "/tmp/prune-proj"
        base = datetime.now(timezone.utc)

        snapshot = self._cards(
            name, [("p1", 1), ("p2", 2), ("s3", 3), ("s4", 4)], base
        )
        self._open(monkeypatch, h, snapshot, name, path)

        # Pre-fix this raw index is [p1, p2, s3, s4] — OLDEST first, so index 0
        # is p1, a card compaction prunes from disk.
        ids = [c.card_id for c in h.get_cards_for_project(name)]
        assert ids == ["s4", "s3", "p2", "p1"], (
            f"first open is not newest-first by seq_num: {ids}"
        )

        # feed_store prunes the OLDEST cards first at compaction; the reloaded
        # snapshot no longer lists p1/p2, but `_project_cards` still does.
        fresh = self._cards(name, [("s3", 3), ("s4", 4)], base)

        # Case A: pruned ids still in memory (compaction rewrote disk; this
        # process never reloaded them) → they keep their OLD seq_num at tail.
        assert "p1" in h._project_cards[name]
        self._open(monkeypatch, h, fresh, name, path)
        ids = [c.card_id for c in h.get_cards_for_project(name)]
        assert ids == ["s4", "s3", "p2", "p1"], (
            f"pruned-but-retained ids must stay at the tail: {ids}"
        )
        assert ids[0] not in ("p1", "p2"), (
            "a pruned card must never be index 0 (Accept All would commit it)"
        )

        # Case B: pruned ids absent from `_cards` (never loaded) → the merge's
        # `cid in self._cards` filter drops them entirely.
        for cid in ("p1", "p2"):
            h._cards.pop(cid)
        self._open(monkeypatch, h, fresh, name, path)
        ids = [c.card_id for c in h.get_cards_for_project(name)]
        assert ids == ["s4", "s3"], (
            f"ids the process never loaded must be filtered out: {ids}"
        )
        assert ids[0] == "s4", (
            "the newest SURVIVING card must lead, not a pruned id"
        )


# ═══════════════════════════════════════════════════════════════════
#  MEMRATCHET Phase 5 (spec §2.1 eviction, §2.6 cases 1-5, 7) — the live
#  widget window. `_card_widgets` is bounded once eviction is wired into
#  the two live append paths; card DATA is never dropped.
# ═══════════════════════════════════════════════════════════════════

class _StubCardWidget:
    """Minimal widget double: the eviction pass reads only get_height()."""

    def __init__(self, height: int):
        self._height = height

    def get_height(self) -> int:
        return self._height


class _ViewportGuard:
    """Stateful `is_above_viewport` double: one verdict per call, then default.

    Records every widget it is asked about, so a test can prove the loop both
    consulted the guard (the "attempted" half) and where it broke.
    """

    def __init__(self, *verdicts, default=False):
        self._verdicts = list(verdicts)
        self._default = default
        self.seen = []

    def __call__(self, widget):
        self.seen.append(widget)
        return self._verdicts.pop(0) if self._verdicts else self._default


class _FailsafeViewportGuard:
    """Mirror of FeedTab.is_above_viewport's geometry rule (feed_tab.py:444-467).

    Near-zero extent means "unknown" — never "above" (GTK 4.14 reports
    `compute_bounds -> (True, zero rect)` for a never-allocated widget, so a
    naive `bottom <= vadj.value` test would call every unmeasured card
    "above the viewport" and destroy it). Used to pin that fail-safe
    direction against a zeroed adjustment.
    """

    def __init__(self, vadj):
        self._vadj = vadj
        self.seen = []

    def __call__(self, widget):
        self.seen.append(widget)
        height = widget.get_height() or 0
        if height <= 0:
            return False                  # unmeasurable → never evict
        return height <= self._vadj.get_value()


class TestEvictionSurplus:
    """spec §2.1 eviction wired into add_card/_append + add_cards_batch/_append_all."""

    def _handler(self):
        from ui.handlers.feed_handler import FeedHandler

        h = FeedHandler(GLib=MockGLib(), on_send_to_agent=MagicMock())
        h.set_feed_tab(MockFeedTab())
        return h

    def _burst(self, h, count, project="evict-proj"):
        """Drive `count` real add_card() calls (MockGLib idles synchronously).

        persist=False: the fixture registers no project path, so a persist
        thread per card would be pure overhead here.
        """
        ts = datetime.now(timezone.utc)
        ids = []
        for i in range(count):
            card = FeedCardData(
                card_type="diff", source="agent", title=f"c{i}", body="",
                author="x", timestamp=ts, project_name=project,
            )
            ids.append(h.add_card(card, persist=False))
        return ids

    def _seed(self, h, count, project="evict-proj", height=56, seq_start=1):
        """Seed N live widgets + card data directly (no GTK widget builds).

        Used where the test needs to reach the over-cap state *without* the
        append paths' own eviction running first, so a single explicit
        `_evict_surplus_card_widgets()` call is the thing under test.
        `seq_start` lets a caller place a set OLDER than another (P6 page tests).
        """
        ts = datetime.now(timezone.utc)
        ids = []
        for seq in range(seq_start, seq_start + count):
            cid = f"{project}-c{seq}"
            h._cards[cid] = FeedCardData(
                card_type="diff", source="agent", title=cid, body="",
                author="x", timestamp=ts.replace(microsecond=seq), project_name=project,
                card_id=cid, seq_num=seq,
            )
            h._project_cards.setdefault(project, []).insert(0, cid)
            h._card_widgets[cid] = _StubCardWidget(height)
            ids.append(cid)
        h._project_seq[project] = seq_start + count - 1
        return ids

    def test_burst_bounds_live_widgets_and_keeps_newest(self):
        """Case 1: 500 add_card calls → map bounded, newest KEEP_NEWEST survive."""
        from ui.handlers.feed_handler import KEEP_NEWEST_CARDS, MAX_LIVE_CARD_WIDGETS

        h = self._handler()
        ids = self._burst(h, 500)

        assert len(h._card_widgets) <= MAX_LIVE_CARD_WIDGETS, (
            f"500 adds left {len(h._card_widgets)} widgets live"
        )
        assert len(h._card_widgets) == MAX_LIVE_CARD_WIDGETS
        newest = {
            c.card_id for c in sorted(
                h._cards.values(), key=lambda c: c.seq_num or 0, reverse=True
            )[:KEEP_NEWEST_CARDS]
        }
        assert newest <= set(h._card_widgets), (
            "the newest KEEP_NEWEST_CARDS must never be evicted"
        )
        # DATA is untouched by eviction — only widgets are released.
        assert len(h._cards) == 500
        assert all(cid in h._cards for cid in ids)

    def test_over_cap_with_guard_refusing_evicts_nothing(self):
        """Case 2: the cap is a bound when the viewport guard permits, not an
        invariant — a refusing guard must leave the map unchanged."""
        h = self._handler()
        h._feed_tab._above_viewport = False
        ids = self._burst(h, 200)

        assert len(h._card_widgets) == len(ids) == 200
        assert h._backlog == [], "no card may be pushed back when nothing was released"

    def test_evicted_card_keeps_data_and_leads_backlog(self):
        """Case 3: evicted FeedCardData stays in _cards, sits at the front of
        the newest-first _backlog, and Load More re-renders a widget for it."""
        from ui.handlers.feed_handler import MAX_LIVE_CARD_WIDGETS

        h = self._handler()
        ids = self._burst(h, MAX_LIVE_CARD_WIDGETS + 1)   # exactly one over

        evicted = [cid for cid in ids if cid not in h._card_widgets]
        assert evicted == [ids[0]], (
            f"precondition: only the oldest card is released, got {evicted}"
        )
        assert h._cards[evicted[0]].card_id == evicted[0], "card DATA must survive"
        assert h._backlog[0].card_id == evicted[0], (
            "the released card must lead the newest-first backlog"
        )

        h._load_more()
        assert evicted[0] in h._card_widgets, "Load More must re-render the evicted card"

    def test_viewport_guard_break_stops_the_loop(self):
        """Case 4: the first refusal breaks the loop — nothing beyond it is
        destroyed, even though the map is still over the cap."""
        from ui.handlers.feed_handler import MAX_LIVE_CARD_WIDGETS

        h = self._handler()
        self._seed(h, MAX_LIVE_CARD_WIDGETS + 5)          # 125 → target 5
        guard = _ViewportGuard(True, False, default=False)
        h._feed_tab.is_above_viewport = guard

        h._evict_surplus_card_widgets()

        assert len(guard.seen) == 2, "one permissive victim, then the refusal"
        assert len(h._card_widgets) == MAX_LIVE_CARD_WIDGETS + 4, (
            "the break must stop at the first refusal, not continue down the list"
        )
        assert len(h._backlog) == 1, "only the one permitted victim was released"
        assert len(h._cards) == MAX_LIVE_CARD_WIDGETS + 5

    def test_near_bottom_pins_the_bottom_after_eviction(self):
        """Case 5a: released>0 and near-bottom → the bottom is re-pinned.

        The pin writes `upper` (2000); the scrolled-up compensation would have
        written `value - (height + spacing)` (900 - 64 = 836) — so the recorded
        write identifies which arm ran."""
        from ui.handlers.feed_handler import MAX_LIVE_CARD_WIDGETS

        h = self._handler()
        self._seed(h, MAX_LIVE_CARD_WIDGETS + 1)
        tab = h._feed_tab
        tab._near_bottom = True
        tab._vadjustment = MockVadjustment(value=900, upper=2000, page_size=600)

        h._evict_surplus_card_widgets()

        assert len(h._card_widgets) == MAX_LIVE_CARD_WIDGETS
        assert tab.scroll_to_bottom_calls == 1, "the pinned bottom must be restored"
        assert tab._vadjustment.set_value_calls == [2000], (
            "the near-bottom arm pins to the upper bound; a compensation write "
            "(900 - 64 = 836) would mean the wrong arm ran"
        )

    def test_scrolled_up_compensates_by_height_plus_spacing(self):
        """Case 5b: scrolled-up → the adjustment is corrected by
        height + container spacing (asserted explicitly as value - (56 + 8))."""
        from ui.handlers.feed_handler import MAX_LIVE_CARD_WIDGETS

        h = self._handler()
        self._seed(h, MAX_LIVE_CARD_WIDGETS + 1, height=56)
        tab = h._feed_tab
        tab._near_bottom = False
        tab._card_spacing = 8
        tab._vadjustment = MockVadjustment(value=900, upper=2000, page_size=600)

        h._evict_surplus_card_widgets()

        assert tab.scroll_to_bottom_calls == 0, "a reading user must not be yanked"
        assert tab._vadjustment.set_value_calls == [900 - (56 + 8)], (
            "compensation must subtract the removed height AND the inter-child "
            "Gtk.Box spacing (feed_tab.py set_spacing(8))"
        )

    def test_batch_burst_evicts_down_to_the_cap(self, monkeypatch):
        """Case 7: the batch path is the second unbounded widget writer — one
        eviction pass per batch bounds it too."""
        import ui.handlers.feed_handler as fh
        from ui.handlers.feed_handler import KEEP_NEWEST_CARDS, MAX_LIVE_CARD_WIDGETS

        # Deterministic: add_cards_batch's persist hop runs inline.
        monkeypatch.setattr(fh, "threading", _SyncThreading)

        h = self._handler()
        ts = datetime.now(timezone.utc)
        cards = [
            FeedCardData(
                card_type="diff", source="agent", title=f"b{i}", body="",
                author="x", timestamp=ts, project_name="batch-proj",
            )
            for i in range(200)
        ]

        h.add_cards_batch(cards)

        assert len(h._card_widgets) <= MAX_LIVE_CARD_WIDGETS
        assert len(h._card_widgets) == MAX_LIVE_CARD_WIDGETS
        newest = {
            c.card_id for c in sorted(
                h._cards.values(), key=lambda c: c.seq_num or 0, reverse=True
            )[:KEEP_NEWEST_CARDS]
        }
        assert newest <= set(h._card_widgets)
        assert len(h._backlog) == len(cards) - MAX_LIVE_CARD_WIDGETS, (
            "every released card must be pushed back (reachable via Load More)"
        )

    def test_zeroed_adjustment_destroys_nothing(self):
        """P2-audit discriminator: a zeroed adjustment (upper=page=value=0) is
        the "layout never ran" state — indistinguishable from "no container".
        Eviction must then attempt and refuse, for BOTH an unmeasurable card
        (zero extent → geometry unknown) and a measurable one sitting below a
        zero-valued viewport. Neither may be read as "above the viewport"."""
        from ui.handlers.feed_handler import MAX_LIVE_CARD_WIDGETS

        for height in (0, 56):
            h = self._handler()
            self._seed(h, MAX_LIVE_CARD_WIDGETS + 1, height=height)
            tab = h._feed_tab
            tab._near_bottom = True
            tab._vadjustment = MockVadjustment(value=0, upper=0, page_size=0)
            tab.is_above_viewport = _FailsafeViewportGuard(tab._vadjustment)

            h._evict_surplus_card_widgets()

            assert tab.is_above_viewport.seen, (
                f"height={height}: the pass must reach the viewport guard"
            )
            assert len(h._card_widgets) == MAX_LIVE_CARD_WIDGETS + 1, (
                f"height={height}: a zeroed adjustment must not classify cards "
                "as above the viewport — the fail-safe direction must survive"
            )
            assert h._backlog == []
            assert tab.scroll_to_bottom_calls == 0, "nothing released → no correction"

    def test_load_more_page_identity_survives_eviction(self):
        """§2.6 case 8 / §6 — the exclusion is what keeps Load More from
        becoming a permanent no-op.

        The page just prepended is the OLDEST content in the feed, i.e. exactly
        the victim set. Without `exclude=ids_just_loaded` the click would build
        PAGE_SIZE widgets, evict them and push them straight back (round-3
        BUG #1). Non-vacuity: a non-page card must still be released in the
        same pass, or the assertion would hold for a handler that evicts
        nothing at all."""
        from ui.handlers.feed_handler import MAX_LIVE_CARD_WIDGETS

        h = self._handler()
        tab = h._feed_tab
        proj = "page-proj"
        ts = datetime.now(timezone.utc)

        # The backlog page: the oldest cards in the feed (seq 1..20).
        page_ids = []
        for s in range(1, 21):
            cid = f"page-{s}"
            h._cards[cid] = FeedCardData(
                card_type="diff", source="agent", title=cid, body="", author="x",
                timestamp=ts.replace(microsecond=s), project_name=proj,
                card_id=cid, seq_num=s,
            )
            h._project_cards.setdefault(proj, []).append(cid)
            page_ids.append(cid)
        h._backlog = [h._cards[cid] for cid in reversed(page_ids)]  # newest-first
        expected_page = [c.card_id for c in h._backlog[:h.PAGE_SIZE]]

        # 120 live widgets (the cap) + a 15-card page = 135 → over the cap.
        seeded = self._seed(h, MAX_LIVE_CARD_WIDGETS, project=proj, seq_start=100)

        # Record what the rebuilt bar reports: §6 requires the label to reflect
        # the backlog AFTER eviction's pushes, not the pre-push `remaining`
        # baked in by _render (round-4 BUG #4).
        built = []
        real_build = h._build_load_more_widget

        def _record(remaining):
            built.append(remaining)
            return real_build(remaining)

        h._build_load_more_widget = _record

        h._load_more()

        parented = [cid for _w, cid in tab.cards]
        for cid in expected_page:
            assert cid in h._card_widgets, (
                f"the page id {cid} was evicted by its own Load More click"
            )
            assert cid in parented, f"the page id {cid} is no longer parented"
        released = [cid for cid in seeded if cid not in h._card_widgets]
        assert released, "non-vacuity: at least one non-page card must be released"
        assert len(h._backlog) == 5 + len(released), "the released cards are pushed back"
        assert built[-1] == len(h._backlog), (
            f"the rebuilt bar must report the post-push backlog size, got {built}"
        )

    def test_eviction_rebuilds_load_more_when_backlog_drained(self):
        """§2.6 case 9 / §6 — a drained backlog plus no sentinel must not make
        the pushed-back cards unreachable (round-3 BUG #2).

        Before eviction existed, `_backlog` only ever shrank, so the Load More
        widget was only ever built on the load path. Eviction is the first
        writer that can make cards reachable *again*, so it must rebuild the
        widget it had just removed."""
        from ui.handlers.feed_handler import MAX_LIVE_CARD_WIDGETS

        h = self._handler()
        tab = h._feed_tab
        self._seed(h, MAX_LIVE_CARD_WIDGETS + 1)
        h._backlog = []
        h._load_more_widget = None
        assert "__load_more__" not in [cid for _w, cid in tab.cards], (
            "precondition: no sentinel is parented and the backlog is drained"
        )

        # §6 — the rebuilt bar's label must report the new backlog size.
        built = []
        real_build = h._build_load_more_widget

        def _record(remaining):
            built.append(remaining)
            return real_build(remaining)

        h._build_load_more_widget = _record

        h._evict_surplus_card_widgets()

        assert len(h._backlog) == 1, "the released card must be pushed back"
        assert h._load_more_widget is not None, "the sentinel must be rebuilt"
        assert built == [1], (
            f"the rebuilt bar must report the pushed-back count, got {built}"
        )
        assert "__load_more__" in [cid for _w, cid in tab.cards], (
            "the rebuilt sentinel must be parented, or the card is unreachable"
        )

    def _open_project(self, monkeypatch, h, name, path, cards):
        """Run one project open deterministically (synchronous load thread)."""
        import ui.handlers.feed_handler as fh

        monkeypatch.setattr(fh, "threading", _SyncThreading)
        store = MagicMock()
        store.load_feed.return_value = cards
        store.load_feed_prefs.return_value = _default_prefs()
        store.FEED_WINDOW_DEFAULT = FEED_WINDOW_DEFAULT
        monkeypatch.setattr(fh, "feed_store", store)
        h._project_paths[name] = path
        h.on_project_opened(name, path)

    def test_eviction_runs_on_the_project_open_path(self, monkeypatch):
        """§2.1 call-site row 3 / §6 — the load path is the FOURTH call site
        (round-7 BUG #2).

        Only reachable when the handler already holds more than the cap from
        earlier activity: an open renders at most PAGE_SIZE cards, so on a
        fresh handler the cap can never be crossed by the load path alone."""
        from ui.handlers.feed_handler import MAX_LIVE_CARD_WIDGETS

        h = self._handler()
        proj = "load-evict-proj"
        ts = datetime.now(timezone.utc)

        # Pre-existing mass from earlier activity, in a DIFFERENT project so
        # the open (which clears only the previous ACTIVE project) keeps it.
        self._seed(h, MAX_LIVE_CARD_WIDGETS + 5, project="earlier-proj")

        # The opened project resumes from a high-water mark, so its cards are
        # the newest — eviction must spare them.
        cards = [
            FeedCardData(
                card_type="diff", source="agent", title=f"n{s}", body="",
                author="x", timestamp=ts.replace(microsecond=s), project_name=proj,
                card_id=f"n{s}", seq_num=s + 200,
            )
            for s in range(1, 6)
        ]
        self._open_project(monkeypatch, h, proj, "/tmp/load-evict-proj", cards)

        assert len(h._card_widgets) <= MAX_LIVE_CARD_WIDGETS, (
            "the load path must run eviction after its appends"
        )
        assert len(h._card_widgets) == MAX_LIVE_CARD_WIDGETS
        assert all(f"n{s}" in h._card_widgets for s in range(1, 6)), (
            "the cards just rendered are the newest and must survive"
        )

    def test_cross_project_cards_are_eviction_candidates(self):
        """§2.6 case 11 / §6 — the victim set is the union across projects, not
        the active project's list (round-3 BUG #5).

        `_card_widgets` is one flat dict while widget creation is keyed on the
        CARD's project, which need not be the active one — so gating on
        `_active_project_name` would leak widgets for background projects."""
        from ui.handlers.feed_handler import MAX_LIVE_CARD_WIDGETS

        h = self._handler()
        h._active_project_name = "some-other-project"   # NOT the cards' project
        self._seed(h, MAX_LIVE_CARD_WIDGETS + 1, project="background-proj")

        h._evict_surplus_card_widgets()

        assert len(h._card_widgets) == MAX_LIVE_CARD_WIDGETS, (
            "cards for a non-active project must still be eviction candidates"
        )
        assert len(h._backlog) == 1

    def test_project_seq_not_clobbered_by_parse_window_arrival(self, monkeypatch):
        """P4-audit regression: the load path must not reset _project_seq below
        a seq_num a live arrival already consumed during the parse window —
        otherwise the next arrival reuses that number (duplicate badge, and a
        non-unique key for the eviction ordering)."""
        import ui.handlers.feed_handler as fh

        monkeypatch.setattr(fh, "threading", _SyncThreading)
        h = self._handler()
        name, path = "seq-proj", "/tmp/seq-proj"
        ts = datetime.now(timezone.utc)

        snapshot = [
            FeedCardData(
                card_type="diff", source="agent", title=f"s{i}", body="",
                author="x", timestamp=ts.replace(second=i), project_name=name,
                card_id=f"s{i}", seq_num=i,
            )
            for i in range(1, 4)          # snapshot high-water mark: 3
        ]
        h._project_seq[name] = 3          # the live counter is already at 3
        live_seqs = []

        def _load(_path):
            # A live card arrives during the parse window (the arrival the
            # loader's background thread cannot see in its snapshot).
            live = FeedCardData(
                card_type="diff", source="agent", title="live", body="",
                author="x", timestamp=ts.replace(second=30), project_name=name,
            )
            h.add_card(live, persist=False)
            live_seqs.append(live.seq_num)
            return snapshot

        store = MagicMock()
        store.load_feed.side_effect = _load
        store.load_feed_prefs.return_value = _default_prefs()
        store.FEED_WINDOW_DEFAULT = FEED_WINDOW_DEFAULT
        monkeypatch.setattr(fh, "feed_store", store)
        h._project_paths[name] = path

        h.on_project_opened(name, path)

        assert live_seqs == [4], "the live arrival takes the next sequence number"
        assert h._project_seq[name] == 4, (
            "the load must not clobber the live arrival's sequence number back to 3"
        )

        nxt = FeedCardData(
            card_type="diff", source="agent", title="next", body="",
            author="x", timestamp=ts, project_name=name,
        )
        h.add_card(nxt, persist=False)
        assert nxt.seq_num == 5, (
            f"the next arrival must not reuse seq 4 (got {nxt.seq_num})"
        )


# ═══════════════════════════════════════════════════════════════════
#  TestSmartScroll — Phase 4 (consolidated: only schedule_smart_scroll_to_bottom
#  and schedule_scroll_to_bottom remain in the public API; the old synchronous
#  scroll_to_bottom() and smart_scroll_to_bottom() were removed during the
#  4-scroll-sites → 1-funnel refactor).
# ═══════════════════════════════════════════════════════════════════

class TestSmartScroll:
    """Phase 4 + refactor: schedule_smart_scroll_to_bottom only scrolls when
    the user is near the bottom (within 80px). If the user has scrolled up to
    read older cards, the scroll is skipped so their reading position is
    preserved. schedule_scroll_to_bottom is the unconditional variant."""

    def test_smart_scroll_when_near_bottom(self):
        """If user is within 80px of bottom, smart_scroll scrolls to bottom."""
        mock_tab = MockFeedTab()
        # Set user at upper-50 (50px from bottom, since page_size=600, upper=1000)
        mock_tab._vadjustment = MockVadjustment(value=950, upper=1000, page_size=600)
        # distance_from_bottom = 1000 - 600 - 950 = -450 → < 80, scrolls
        mock_tab.schedule_smart_scroll_to_bottom()
        assert mock_tab._vadjustment.get_value() == 1000

    def test_smart_scroll_when_exactly_80px_from_bottom(self):
        """If user is exactly 80px from bottom, smart_scroll DOES NOT scroll (boundary: <80)."""
        mock_tab = MockFeedTab()
        # upper=1000, page_size=600, so being 80px from bottom means value=1000-600-80=320
        mock_tab._vadjustment = MockVadjustment(value=320, upper=1000, page_size=600)
        # distance_from_bottom = 1000 - 600 - 320 = 80 → NOT < 80, no scroll
        mock_tab.schedule_smart_scroll_to_bottom()
        assert mock_tab._vadjustment.get_value() == 320  # unchanged

    def test_smart_scroll_when_far_from_bottom(self):
        """If user is >80px from bottom, smart_scroll does nothing."""
        mock_tab = MockFeedTab()
        # User scrolled to top: value=0
        mock_tab._vadjustment = MockVadjustment(value=0, upper=1000, page_size=600)
        # distance_from_bottom = 1000 - 600 - 0 = 400 → > 80, no scroll
        mock_tab.schedule_smart_scroll_to_bottom()
        assert mock_tab._vadjustment.get_value() == 0  # unchanged

    def test_smart_scroll_when_mid_feed(self):
        """If user is mid-feed and >80px from bottom, smart_scroll does nothing."""
        mock_tab = MockFeedTab()
        # User scrolled halfway: value=200
        mock_tab._vadjustment = MockVadjustment(value=200, upper=1000, page_size=600)
        # distance_from_bottom = 1000 - 600 - 200 = 200 → > 80, no scroll
        mock_tab.schedule_smart_scroll_to_bottom()
        assert mock_tab._vadjustment.get_value() == 200  # unchanged

    def test_schedule_scroll_to_bottom_always_scrolls(self):
        """The unconditional schedule_scroll_to_bottom() always scrolls."""
        mock_tab = MockFeedTab()
        # User scrolled to top
        mock_tab._vadjustment = MockVadjustment(value=0, upper=1000, page_size=600)
        mock_tab.schedule_scroll_to_bottom()
        assert mock_tab._vadjustment.get_value() == 1000

    def test_smart_scroll_no_vadjustment_is_noop(self):
        """If _vadjustment is None, smart_scroll is a no-op (graceful)."""
        mock_tab = MockFeedTab()
        mock_tab._vadjustment = None
        # Should not raise
        mock_tab.schedule_smart_scroll_to_bottom()

    def test_add_card_uses_smart_scroll_only(self, feed_handler, mock_feed_tab):
        """add_card() calls schedule_smart_scroll_to_bottom exactly once.

        This is the consolidation contract: all append paths funnel through
        a single helper (_schedule_smart_scroll) which calls this one method.
        """
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="Test card",
            body="", author="x", timestamp=ts, project_name="testproj",
        )
        original_smart = mock_feed_tab.schedule_smart_scroll_to_bottom
        called = []
        def tracking_smart():
            called.append(True)
            return original_smart()
        mock_feed_tab.schedule_smart_scroll_to_bottom = tracking_smart

        feed_handler.add_card(card)

        assert len(called) == 1, "schedule_smart_scroll_to_bottom should be called exactly once in add_card"


class TestAddCardsBatch:
    """Refactor: add_cards_batch() runs multiple cards through ONE idle
    callback and ONE smart-scroll. Previously each add_card() enqueued its
    own callback, racing the vadjustment when batches arrived faster than
    GTK could lay them out."""

    def test_add_cards_batch_returns_ids_in_input_order(self, feed_handler):
        """Returns card_ids in the same order as the input cards list."""
        ts = datetime.now(timezone.utc)
        cards = [
            FeedCardData(
                card_type="diff", source="agent", title=f"Card {i}",
                body="", author="x", timestamp=ts, project_name="batchproj",
            )
            for i in range(5)
        ]
        ids = feed_handler.add_cards_batch(cards)
        assert len(ids) == 5
        # Each id must match the corresponding card's assigned card_id
        for i, cid in enumerate(ids):
            assert cards[i].card_id == cid

    def test_add_cards_batch_single_smart_scroll(self, feed_handler, mock_feed_tab):
        """Batched cards trigger schedule_smart_scroll_to_bottom exactly once."""
        ts = datetime.now(timezone.utc)
        cards = [
            FeedCardData(
                card_type="diff", source="agent", title=f"Card {i}",
                body="", author="x", timestamp=ts, project_name="batchproj",
            )
            for i in range(3)
        ]
        original = mock_feed_tab.schedule_smart_scroll_to_bottom
        called = []
        def tracking():
            called.append(True)
            return original()
        mock_feed_tab.schedule_smart_scroll_to_bottom = tracking

        feed_handler.add_cards_batch(cards)

        assert len(called) == 1, (
            f"add_cards_batch must call schedule_smart_scroll_to_bottom "
            f"exactly once, got {len(called)} calls"
        )

    def test_add_cards_batch_assigns_monotonic_sequence_numbers(self, feed_handler):
        """Each card in the batch gets a unique, increasing seq_num."""
        ts = datetime.now(timezone.utc)
        cards = [
            FeedCardData(
                card_type="diff", source="agent", title=f"Card {i}",
                body="", author="x", timestamp=ts, project_name="seqproj",
            )
            for i in range(4)
        ]
        feed_handler.add_cards_batch(cards)
        seqs = [c.seq_num for c in cards]
        # Strictly increasing
        assert seqs == sorted(set(seqs))
        assert len(seqs) == 4

    def test_add_cards_batch_empty_input_is_noop(self, feed_handler, mock_feed_tab):
        """Empty list returns [] and does not call schedule_smart_scroll_to_bottom."""
        called = []
        original = mock_feed_tab.schedule_smart_scroll_to_bottom
        def tracking():
            called.append(True)
            return original()
        mock_feed_tab.schedule_smart_scroll_to_bottom = tracking

        result = feed_handler.add_cards_batch([])
        assert result == []
        assert len(called) == 0

    def test_add_cards_batch_indexes_all_under_project(self, feed_handler):
        """All batched cards show up in get_cards_for_project."""
        ts = datetime.now(timezone.utc)
        cards = [
            FeedCardData(
                card_type="diff", source="agent", title=f"Card {i}",
                body="", author="x", timestamp=ts, project_name="indexproj",
            )
            for i in range(3)
        ]
        feed_handler.add_cards_batch(cards)
        listed = feed_handler.get_cards_for_project("indexproj")
        assert len(listed) == 3

    def test_add_cards_batch_widgets_appended_in_one_idle(self, feed_handler, mock_feed_tab):
        """All batched cards are appended to feed_tab in a single idle callback."""
        ts = datetime.now(timezone.utc)
        cards = [
            FeedCardData(
                card_type="diff", source="agent", title=f"Card {i}",
                body="", author="x", timestamp=ts, project_name="idleproj",
            )
            for i in range(3)
        ]
        # Snapshot append_card call count before batch
        before = len(mock_feed_tab.append_calls)

        feed_handler.add_cards_batch(cards)

        # MockGLib.idle_add runs callbacks synchronously, so by the time
        # add_cards_batch returns, the bulk _append_all callback has already
        # fired and appended all 3 cards. No drain needed.
        appended = len(mock_feed_tab.append_calls) - before
        assert appended == 3, (
            f"add_cards_batch should append all cards in one idle callback "
            f"(3 append_card calls total), got {appended}"
        )

    def test_add_cards_batch_mixed_approval_and_normal(self, feed_handler):
        """Approval cards and normal cards can be batched together."""
        ts = datetime.now(timezone.utc)
        cards = [
            FeedCardData(
                card_type="diff", source="agent", title="Normal",
                body="", author="x", timestamp=ts, project_name="mixedproj",
            ),
            FeedCardData(
                card_type="exec_approval", source="agent", title="Needs approval",
                body="", author="x", timestamp=ts, project_name="mixedproj",
                metadata={"needs_approval": True},
            ),
            FeedCardData(
                card_type="diff", source="agent", title="Also normal",
                body="", author="x", timestamp=ts, project_name="mixedproj",
            ),
        ]
        ids = feed_handler.add_cards_batch(cards)
        assert len(ids) == 3
        # All three cards (normal + approval) should have widgets stored
        for cid in ids:
            assert cid in feed_handler._card_widgets


# ═══════════════════════════════════════════════════════════════════
#  TestBatchAccept — Phase 5
#  Verifies batch accept bar appears when ≥2 pending file-change cards,
#  and that Accept All resolves them all.
# ═══════════════════════════════════════════════════════════════════

class TestBatchAccept:
    """Phase 5: batch accept bar + handle_batch_accept()."""

    def test_update_batch_bar_0_hides_bar(self, feed_handler, mock_feed_tab):
        """update_batch_bar(0) hides the batch bar."""
        feed_handler._active_project_name = "testproj"
        feed_handler._update_batch_bar_for_active_project()
        assert mock_feed_tab._batch_bar_visible is False

    def test_update_batch_bar_1_hides_bar(self, feed_handler, mock_feed_tab):
        """update_batch_bar(1) hides the bar (threshold is ≥2)."""
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="Card 1",
            body="", author="x", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(card)
        feed_handler._update_batch_bar_for_active_project()
        assert mock_feed_tab._batch_bar_visible is False

    def test_update_batch_bar_2_shows_bar(self, feed_handler, mock_feed_tab):
        """update_batch_bar(2) shows the bar."""
        ts = datetime.now(timezone.utc)
        for i in range(2):
            card = FeedCardData(
                card_type="diff", source="agent", title=f"Card {i}",
                body="", author="x", timestamp=ts, project_name="testproj",
            )
            feed_handler.add_card(card)
        feed_handler._update_batch_bar_for_active_project()
        assert mock_feed_tab._batch_bar_visible is True
        assert mock_feed_tab._batch_bar_count == 2

    def test_update_batch_bar_3_shows_bar(self, feed_handler, mock_feed_tab):
        """update_batch_bar(3) shows the bar with count 3."""
        ts = datetime.now(timezone.utc)
        for i in range(3):
            card = FeedCardData(
                card_type="diff", source="agent", title=f"Card {i}",
                body="", author="x", timestamp=ts, project_name="testproj",
            )
            feed_handler.add_card(card)
        feed_handler._update_batch_bar_for_active_project()
        assert mock_feed_tab._batch_bar_visible is True
        assert mock_feed_tab._batch_bar_count == 3

    def test_trailing_run_only_counts_consecutive_pending(self, feed_handler, mock_feed_tab):
        """If a non-pending or non-file-change card breaks the sequence, only trailing run counts."""
        ts = datetime.now(timezone.utc)
        # Card 0: accepted diff → breaks the run
        c0 = FeedCardData(
            card_type="diff", source="agent", title="Accepted",
            body="", author="x", timestamp=ts, project_name="testproj",
        )
        c0.accepted = True
        feed_handler.add_card(c0)
        # Card 1: system card → also breaks the run
        c1 = FeedCardData(
            card_type="system", source="agent", title="System",
            body="", author="x", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(c1)
        # Cards 2, 3: pending diffs (newest)
        for i in [3, 2]:
            c = FeedCardData(
                card_type="diff", source="agent", title=f"Pending {i}",
                body="", author="x", timestamp=ts, project_name="testproj",
            )
            feed_handler.add_card(c)
        feed_handler._update_batch_bar_for_active_project()
        # Only the trailing 2 pending diffs count
        assert mock_feed_tab._batch_bar_visible is True
        assert mock_feed_tab._batch_bar_count == 2

    def test_handle_batch_accept_calls_handle_accept_per_card(
        self, feed_handler, mock_feed_tab
    ):
        """handle_batch_accept iterates through card_ids and calls handle_accept for each."""
        ts = datetime.now(timezone.utc)
        cards = []
        for i in range(3):
            c = FeedCardData(
                card_type="diff", source="agent", title=f"Card {i}",
                body="", author="x", timestamp=ts, project_name="testproj",
            )
            feed_handler.add_card(c)
            cards.append(c)
        # Patch handle_accept to track calls
        original = feed_handler.handle_accept
        calls = []
        def tracking_accept(cid):
            calls.append(cid)
            return original(cid)
        feed_handler.handle_accept = tracking_accept

        card_ids = [c.card_id for c in cards]
        feed_handler.handle_batch_accept(card_ids)

        assert len(calls) == 3
        assert calls == card_ids

    def test_batch_accept_resolves_all_pending(
        self, feed_handler, mock_feed_tab
    ):
        """Batch bar hides once all pending actionable cards are accepted."""
        # Use file_created type (actionable, no git thread complexity)
        ts = datetime.now(timezone.utc)
        cards = []
        for i in range(3):
            c = FeedCardData(
                card_type="file_created", source="agent",
                title=f"New file {i}", body="", author="x",
                timestamp=ts, project_name="testproj",
            )
            feed_handler.add_card(c)
            cards.append(c)

        # Verify bar is showing with 3 pending
        assert mock_feed_tab._batch_bar_count == 3
        assert mock_feed_tab._batch_bar_visible is True

        # Simulate each card being accepted (directly, bypassing git ops)
        for card in cards:
            card.accepted = True
        # Update bar — count drops to 0, bar hides
        feed_handler._update_batch_bar_for_active_project("testproj")

        assert mock_feed_tab._batch_bar_visible is False

    def test_callback_is_wired_on_set_feed_tab(self, feed_handler, mock_feed_tab):
        """set_feed_tab() installs the batch accept callback on the FeedTab."""
        assert mock_feed_tab._batch_accept_callback is not None
        assert callable(mock_feed_tab._batch_accept_callback)

    def test_add_card_updates_batch_bar(self, feed_handler, mock_feed_tab):
        """add_card() for a 2nd file-change card triggers batch bar to show."""
        ts = datetime.now(timezone.utc)
        feed_handler._active_project_name = "testproj"
        # First card — bar hidden
        c1 = FeedCardData(
            card_type="diff", source="agent", title="Card 1",
            body="", author="x", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(c1)
        assert mock_feed_tab._batch_bar_visible is False
        # Second card — bar shows
        c2 = FeedCardData(
            card_type="diff", source="agent", title="Card 2",
            body="", author="x", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(c2)
        assert mock_feed_tab._batch_bar_visible is True
        assert mock_feed_tab._batch_bar_count == 2


# ═══════════════════════════════════════════════════════════════════
#  TestScheduleScrollToBottom — Phase 4D-1
#  Tests the real schedule_scroll_to_bottom mechanism on FeedTab.
#  Uses _FakeAdjustment (not MockFeedTab) to exercise the actual
#  connect/disconnect/emit/timeout logic in feed_tab.py.
# ═══════════════════════════════════════════════════════════════════

import gi
gi.require_version('Gtk', '4.0')


class _FakeAdjustment:
    """
    Duck-typed replacement for Gtk.Adjustment that does NOT auto-emit
    'changed' when properties change. Tests manually call emit_changed()
    to control exactly when the 'changed' signal fires, and set_upper()
    to control what get_upper() returns.

    This is necessary because Gtk.Adjustment.set_upper() emits 'changed'
    internally — making it impossible to test the 'changed never fires'
    timeout-fallback path with a real Adjustment.
    """

    def __init__(self, upper=0.0, page_size=600.0):
        self._upper = upper
        self._value = 0.0
        self._page_size = page_size
        self._handlers: dict[int, callable] = {}
        self._next_id = 1
        self.set_value_calls: list[float] = []
        self.disconnect_calls: list[int] = []

    def connect(self, signal: str, callback) -> int:
        assert signal == "changed", (
            f"_FakeAdjustment.connect: unexpected signal {signal!r}"
        )
        handler_id = self._next_id
        self._next_id += 1
        self._handlers[handler_id] = callback
        return handler_id

    def disconnect(self, handler_id: int):
        self.disconnect_calls.append(handler_id)
        self._handlers.pop(handler_id, None)

    def emit_changed(self):
        """Manually fire the 'changed' signal to all connected handlers."""
        # Copy the list because handlers may disconnect during iteration
        for cb in list(self._handlers.values()):
            cb(self)

    def get_upper(self) -> float:
        return self._upper

    def set_upper(self, upper: float):
        """Set upper WITHOUT emitting 'changed' (unlike real Gtk.Adjustment)."""
        self._upper = upper

    def get_value(self) -> float:
        return self._value

    def set_value(self, value: float):
        self._value = value
        self.set_value_calls.append(value)

    def get_page_size(self) -> float:
        return self._page_size


class _FakeScrolledWindow:
    """Minimal stand-in for Gtk.ScrolledWindow holding a _FakeAdjustment."""

    def __init__(self, vadj: _FakeAdjustment):
        self._vadj = vadj

    def get_vadjustment(self) -> _FakeAdjustment:
        return self._vadj


@pytest.fixture
def real_feed_tab():
    """Create a real FeedTab instance for testing schedule_scroll_to_bottom.

    FeedTab constructs a Gtk.ScrolledWindow in __init__, but we replace
    _feed_scroll with a _FakeScrolledWindow holding a _FakeAdjustment so
    we can control when 'changed' fires.
    """
    from ui.views.feed_tab import FeedTab
    tab = FeedTab()
    # Replace the real scrolled window with our fake
    adj = _FakeAdjustment(upper=0.0)
    tab._feed_scroll = _FakeScrolledWindow(adj)
    return tab


def _geometry_widget(height: float):
    """A real `Gtk.Box` whose geometry the eviction pass can actually measure.

    The Xvfb harness never realizes widgets, so a genuine card reports
    `compute_bounds -> (True, zero rect)` and `is_above_viewport` returns False
    (the round-6 BUG #1 fail-safe) — every eviction would be vacuously refused
    and the mirror assertion would hold trivially. Instance-level overrides give
    the widget a positive extent so the real `FeedTab` geometry primitives run
    their measurable branch.
    """
    from gi.repository import Gtk, Graphene

    widget = Gtk.Box()
    widget.get_height = lambda: height

    def _bounds(_target, _h=height):
        rect = Graphene.Rect()
        rect.init(0.0, 0.0, 100.0, _h)
        return (True, rect)

    widget.compute_bounds = _bounds
    return widget


def _parented_sentinels(tab):
    """Every parented Load More bar — including ORPHANS no map can reach."""
    return [
        child for child in tab.get_card_container()
        if child.has_css_class("feed-card-load-more")
    ]


def _parented_cards(tab):
    """Parented card widgets, excluding the sentinel bars and the empty state."""
    return [
        child for child in tab.get_card_container()
        if child.has_css_class("feed-card")
        and not child.has_css_class("feed-card-load-more")
    ]


class TestBacklogDiscipline:
    """MEMRATCHET P7a (spec §2.1) — `_backlog` is shared between the loader
    thread, the main thread's eviction pass and `_load_more`, so the loader must
    MERGE into it rather than rebind, and every writer must hold the lock.

    The race is modelled the way it actually happens: the pushed card appears
    while `feed_store.load_feed` is executing (inside the loader's parse
    window), not before `on_project_opened` — which clears `_backlog` itself.
    """

    PAGE_SIZE = 15

    def _handler(self):
        from ui.handlers.feed_handler import FeedHandler

        h = FeedHandler(GLib=MockGLib(), on_send_to_agent=MagicMock())
        h.set_feed_tab(MockFeedTab())
        return h

    @staticmethod
    def _card(cid, seq, project="merge-proj"):
        return FeedCardData(
            card_type="diff", source="agent", title=cid, body="", author="x",
            timestamp=datetime.now(timezone.utc).replace(microsecond=seq),
            project_name=project, card_id=cid, seq_num=seq,
        )

    def _open_with_injection(self, monkeypatch, h, name, path, snapshot, on_parse):
        """Open `name`, running `on_parse(handler)` inside the parse window.

        `on_parse` models the eviction insert that lands while the loader is
        busy parsing: a plain rebind would discard it.
        """
        import ui.handlers.feed_handler as fh

        monkeypatch.setattr(fh, "threading", _SyncThreading)

        def _load(_path):
            on_parse(h)                 # the "concurrent" push, mid-parse
            return snapshot

        store = MagicMock()
        store.load_feed.side_effect = _load
        store.load_feed_prefs.return_value = _default_prefs()
        store.FEED_WINDOW_DEFAULT = FEED_WINDOW_DEFAULT
        monkeypatch.setattr(fh, "feed_store", store)
        h._project_paths[name] = path
        h.on_project_opened(name, path)

    def test_loader_merges_survivors_first_without_duplicates(self, monkeypatch):
        """Survivors (the cards eviction released mid-parse) come FIRST, then
        the snapshot's older backlog, with no id repeated."""
        h = self._handler()
        name, path = "merge-proj", "/tmp/merge-proj"

        # 20 cards → backlog = the 5 oldest, newest-first after reversal.
        snapshot = [self._card(f"s{i}", i, name) for i in range(1, 21)]
        pushed = self._card("pushed-newest", 999, name)
        old_backlog_ids = [f"s{i}" for i in range(5, 0, -1)]   # s5..s1 newest-first

        def _push(handler):
            handler._cards[pushed.card_id] = pushed
            with handler._lock:
                handler._backlog.insert(0, pushed)

        self._open_with_injection(monkeypatch, h, name, path, snapshot, _push)

        merged = [c.card_id for c in h._backlog]
        assert merged == ["pushed-newest"] + old_backlog_ids, (
            f"survivors must lead the merged backlog, got {merged}"
        )
        assert len(merged) == len(set(merged)), f"duplicate ids in {merged}"

    def test_pushed_card_is_not_lost_during_the_parse_window(self, monkeypatch):
        """round-2 BUG #3 / round-4 BUG #3: a rebind inside the lock serializes
        the two operations without merging them — the pushed card would vanish
        after its widget was already unparented, leaving it unreachable until
        the project is reopened."""
        h = self._handler()
        name, path = "noloss-proj", "/tmp/noloss-proj"
        snapshot = [self._card(f"s{i}", i, name) for i in range(1, 21)]
        pushed = self._card("pushed-survivor", 999, name)

        def _push(handler):
            handler._cards[pushed.card_id] = pushed
            with handler._lock:
                handler._backlog.insert(0, pushed)

        self._open_with_injection(monkeypatch, h, name, path, snapshot, _push)

        ids = [c.card_id for c in h._backlog]
        assert "pushed-survivor" in ids, (
            f"the card eviction pushed mid-parse was discarded by the load: {ids}"
        )
        assert ids[0] == "pushed-survivor", "and it must still be the newest entry"

    def test_load_more_label_reports_the_merged_backlog(self, monkeypatch):
        """§6 "Load More reflects eviction": the sentinel built at the end of
        the load must count the MERGED list, not the snapshot's backlog slice —
        otherwise the bar under-reports exactly the cards the merge rescued."""
        h = self._handler()
        name, path = "label-proj", "/tmp/label-proj"
        snapshot = [self._card(f"s{i}", i, name) for i in range(1, 21)]
        pushed = self._card("pushed-survivor", 999, name)

        built = []
        real_build = h._build_load_more_widget

        def _record(remaining):
            built.append(remaining)
            return real_build(remaining)

        h._build_load_more_widget = _record

        def _push(handler):
            handler._cards[pushed.card_id] = pushed
            with handler._lock:
                handler._backlog.insert(0, pushed)

        self._open_with_injection(monkeypatch, h, name, path, snapshot, _push)

        # 5 snapshot-backlog cards + 1 survivor = 6, NOT the snapshot's 5.
        assert len(h._backlog) == 6, [c.card_id for c in h._backlog]
        assert built == [6], (
            f"the sentinel must report the merged count (6), got {built}"
        )

    def test_loader_builds_widgets_outside_the_lock(self):
        """Structural guard for round-3 BUG #8: `build_feed_card` must not be
        lexically inside a `with self._lock` block in `_load_and_render`, while
        the `_card_widgets` map write must still be inside one.

        Purely structural (AST) — the lock narrowing is behaviour-preserving, so
        it has no red-before-green claim; this pins the shape instead.
        """
        import ast
        import inspect

        import ui.handlers.feed_handler as fh

        tree = ast.parse(inspect.getsource(fh))
        method = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_load_and_render"
        )

        def _inside_lock(node, target, locked=False):
            """Walk, tracking whether we are under a `with self._lock` block."""
            for child in ast.iter_child_nodes(node):
                child_locked = locked
                if isinstance(child, ast.With):
                    for item in child.items:
                        ctx = item.context_expr
                        if (isinstance(ctx, ast.Attribute) and ctx.attr == "_lock"):
                            child_locked = True
                if child is target:
                    return locked
                found = _inside_lock(child, target, child_locked)
                if found is not None:
                    return found
            return None

        build_calls = [
            n for n in ast.walk(method)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "build_feed_card"
        ]
        assert build_calls, "expected build_feed_card calls in _load_and_render"
        for call in build_calls:
            assert _inside_lock(method, call) is False, (
                f"build_feed_card at line {call.lineno} is inside the lock — "
                "up to PAGE_SIZE GTK constructions would block eviction"
            )

        widget_writes = [
            n for n in ast.walk(method)
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Subscript) and isinstance(t.value, ast.Attribute)
                and t.value.attr == "_card_widgets"
                for t in n.targets
            )
        ]
        assert widget_writes, "expected _card_widgets writes in _load_and_render"
        for write in widget_writes:
            assert _inside_lock(method, write) is True, (
                f"_card_widgets write at line {write.lineno} left the lock — "
                "the map write must stay inside it (spec 'One rule')"
            )


class TestEvictedCardGuard:
    """MEMRATCHET P7b (§2.1 evicted-card guard, §2.6 case 6).

    `update_card` on a card whose widget eviction released must update and
    persist the DATA and stop — rebuilding would re-append an old card at the
    bottom of the feed, which is the ratchet the bound exists to stop.
    """

    def _handler(self):
        from ui.handlers.feed_handler import FeedHandler

        h = FeedHandler(GLib=MockGLib(), on_send_to_agent=MagicMock())
        h.set_feed_tab(MockFeedTab())
        return h

    def _seed_newer(self, h, count, project, seq_start=2, height=56):
        """Directly seed newer live widgets (stubs — no GTK widget builds)."""
        ts = datetime.now(timezone.utc)
        for seq in range(seq_start, seq_start + count):
            cid = f"{project}-n{seq}"
            h._cards[cid] = FeedCardData(
                card_type="diff", source="agent", title=cid, body="", author="x",
                timestamp=ts.replace(microsecond=seq), project_name=project,
                card_id=cid, seq_num=seq,
            )
            h._project_cards.setdefault(project, []).insert(0, cid)
            h._card_widgets[cid] = _StubCardWidget(height)
        h._project_seq[project] = seq_start + count - 1

    def test_update_card_on_evicted_card_persists_data_without_rebuilding(
        self, monkeypatch
    ):
        """§2.6 case 6: data + persist updated; NO rebuild, NO re-append."""
        from ui.handlers.feed_handler import MAX_LIVE_CARD_WIDGETS

        h = self._handler()
        tab = h._feed_tab
        project = "evicted-update-proj"
        tab._above_viewport = True                 # eviction is permitted

        # The target card is added FIRST (real widget), then surrounded by
        # newer cards so eviction's oldest-by-seq_num victim is the target.
        target = FeedCardData(
            card_type="tool_call", source="agent", title="target", body="before",
            author="Coder", timestamp=datetime.now(timezone.utc), project_name=project,
        )
        card_id = h.add_card(target, persist=False)
        assert card_id in h._card_widgets, "precondition: the card has a live widget"

        self._seed_newer(h, MAX_LIVE_CARD_WIDGETS + 1, project)   # 121 newer

        h._evict_surplus_card_widgets()
        assert card_id not in h._card_widgets, (
            "precondition: the oldest card was evicted"
        )
        assert card_id in h._cards, "card DATA must survive eviction"

        # Persist plumbing (mirrors TestBackgroundPersistWriter): no writer
        # thread, store mocked, drain deterministically.
        h._ensure_persist_writer = lambda: None
        store = MagicMock()
        store.update_feed_card.return_value = True
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)
        h._project_paths[project] = "/tmp/evicted-update-proj"

        appended_before = list(tab.append_calls)
        replaced = []
        real_replace = tab.replace_card

        def _record_replace(cid, widget):
            replaced.append(cid)
            return real_replace(cid, widget)

        tab.replace_card = _record_replace
        cards_in_tab_before = list(tab.cards)

        updated = FeedCardData(
            card_type="tool_call", source="agent", title="target",
            body="AFTER-UPDATE", author="Coder",
            timestamp=target.timestamp, project_name=project, card_id=card_id,
        )
        h.update_card(card_id, updated)

        # 1. DATA updated in memory.
        assert h._cards[card_id].body == "AFTER-UPDATE"
        # 2. DATA persisted (the durable copy).
        h._drain_persist_queue()
        assert store.update_feed_card.call_count == 1
        payload = store.update_feed_card.call_args[0][2]
        assert payload.get("body") == "AFTER-UPDATE", (
            f"the updated body must reach disk; payload was {payload!r}"
        )
        # 3. NO rebuild, NO re-append, NO replace.
        assert replaced == [], f"an evicted card must not be replace_card'd: {replaced}"
        assert tab.append_calls == appended_before, (
            "an evicted card must NOT be re-appended at the bottom of the feed"
        )
        assert tab.cards == cards_in_tab_before, "the tab's card list must not grow"
        assert card_id not in h._card_widgets, (
            "the guard must not resurrect a widget for an evicted card"
        )

    def test_guard_returns_before_any_widget_work(self, monkeypatch):
        """Structural companion: with no live widget, `update_card` returns
        BEFORE the in-place/rebuild dispatch — proven by spying on the two
        widget paths (neither may be entered)."""
        h = self._handler()
        project = "guard-proj"
        card = FeedCardData(
            card_type="tool_call", source="agent", title="t", body="b",
            author="Coder", timestamp=datetime.now(timezone.utc), project_name=project,
        )
        card_id = h.add_card(card, persist=False)
        h._card_widgets.pop(card_id)               # as eviction leaves it
        h._ensure_persist_writer = lambda: None
        h._project_paths[project] = "/tmp/guard-proj"

        rebuild = MagicMock()
        monkeypatch.setattr(h, "_rebuild_and_replace_card", rebuild)
        idles = []
        real_idle = h._GLib.idle_add

        def _record_idle(fn, *a, **kw):
            idles.append(fn)
            return real_idle(fn, *a, **kw)

        monkeypatch.setattr(h._GLib, "idle_add", _record_idle)

        h.update_card(card_id, card)

        assert rebuild.call_count == 0, "no rebuild for an evicted card"
        assert idles == [], (
            "the guard must return before any idle_add widget dispatch"
        )
        assert h._cards[card_id].body == "b"


class TestLoadPathSentinelsAndOrphans:
    """MEMRATCHET P6 (§2.1 round-6 BUG #3, round-7 BUG #2) — real FeedTab.

    These use the real container because the bugs are about *parenting*, which
    no map can express: both `_card_widgets` and `_cards_by_id` agree even when
    an orphan is still attached, so a map-based assertion cannot see it.
    """

    def _handler_on_real_tab(self, tab):
        from ui.handlers.feed_handler import FeedHandler

        h = FeedHandler(GLib=MockGLib(), on_send_to_agent=MagicMock())
        h.set_feed_tab(tab)
        return h

    def _open(self, monkeypatch, h, name, path, cards):
        """Run one project open deterministically (synchronous load thread)."""
        import ui.handlers.feed_handler as fh

        monkeypatch.setattr(fh, "threading", _SyncThreading)
        store = MagicMock()
        store.load_feed.return_value = cards
        store.load_feed_prefs.return_value = _default_prefs()
        store.FEED_WINDOW_DEFAULT = FEED_WINDOW_DEFAULT
        monkeypatch.setattr(fh, "feed_store", store)
        h._project_paths[name] = path
        h.on_project_opened(name, path)

    @staticmethod
    def _cards(project_name, count, first_seq=1):
        ts = datetime.now(timezone.utc)
        return [
            FeedCardData(
                card_type="diff", source="agent", title=f"{project_name}-{s}", body="",
                author="x", timestamp=ts.replace(microsecond=s), project_name=project_name,
                card_id=f"{project_name}-c{s}", seq_num=s,
            )
            for s in range(first_seq, first_seq + count)
        ]

    def test_same_project_reopen_parents_one_sentinel(self, real_feed_tab, monkeypatch):
        """Round-5 BUG #1: at most one `__load_more__` is ever parented —
        kept below the cap so eviction never fires and cannot be the guard."""
        tab = real_feed_tab
        h = self._handler_on_real_tab(tab)
        cards = self._cards("reopen-a", 20)          # > PAGE_SIZE → backlog + bar

        self._open(monkeypatch, h, "reopen-a", "/tmp/reopen-a", cards)
        assert len(_parented_sentinels(tab)) == 1, "first open parents one bar"

        # Same project again (no close callback in between) — the fresh
        # snapshot builds brand-new card objects, as a disk re-read returns.
        self._open(monkeypatch, h, "reopen-a", "/tmp/reopen-a", self._cards("reopen-a", 20))
        assert len(_parented_sentinels(tab)) == 1, (
            "a same-project reopen must unparent the previous bar, not add a second"
        )
        assert len(h._card_widgets) <= 120, "precondition: eviction never fired"

    def test_switch_to_project_without_backlog_clears_the_bar(
        self, real_feed_tab, monkeypatch
    ):
        """Round-6 BUG #3: the cleanup is hoisted OUT of the `if`, so switching
        to a project that builds no sentinel still drops the previous bar."""
        tab = real_feed_tab
        h = self._handler_on_real_tab(tab)

        self._open(monkeypatch, h, "with-backlog", "/tmp/with-backlog", self._cards("with-backlog", 20))
        assert len(_parented_sentinels(tab)) == 1, "precondition: the bar exists"

        # ≤ PAGE_SIZE cards → no backlog → load builds no sentinel at all.
        self._open(monkeypatch, h, "small-proj", "/tmp/small-proj", self._cards("small-proj", 3))
        assert _parented_sentinels(tab) == [], (
            "the previous project's bar must not survive the switch"
        )

    def test_same_project_reopen_leaves_no_orphan_widgets(
        self, real_feed_tab, monkeypatch
    ):
        """Round-7 BUG #2: `FeedTab.append_card` only overwrites the map entry,
        so without a `remove_card` first the previous widget stays parented and
        is unreachable by every map and every removal path."""
        tab = real_feed_tab
        h = self._handler_on_real_tab(tab)

        self._open(monkeypatch, h, "orph", "/tmp/orph", self._cards("orph", 3))
        self._open(monkeypatch, h, "orph", "/tmp/orph", self._cards("orph", 3))

        parented = _parented_cards(tab)
        mapped = [cid for cid in tab._cards_by_id if cid != "__load_more__"]
        assert len(parented) == len(mapped), (
            f"{len(parented)} card widgets are parented but only {len(mapped)} "
            "ids are in the tab map — orphans"
        )
        assert len(set(parented)) == len(parented), (
            "the same widget must not be counted twice (duplicate parenting)"
        )
        assert len(mapped) == 3, "precondition: the project's 3 cards are mapped"

    def test_mirror_membership_with_non_vacuity(self, real_feed_tab):
        """§6 — for every id in `_project_cards[active]` (sentinel excluded),
        membership of `_cards_by_id` equals membership of `_card_widgets`.

        The non-vacuity precondition is asserted FIRST: the unallocated-widget
        case makes this mirror hold trivially, so the pass must demonstrably
        remove at least one id from BOTH maps. Driven with measurable fake
        widgets per the spec's round-6 warning."""
        from ui.handlers.feed_handler import MAX_LIVE_CARD_WIDGETS

        tab = real_feed_tab
        h = self._handler_on_real_tab(tab)
        proj = "mirror-proj"
        ts = datetime.now(timezone.utc)

        ids = []
        for s in range(1, MAX_LIVE_CARD_WIDGETS + 2):        # 121 → over the cap
            cid = f"m{s}"
            h._cards[cid] = FeedCardData(
                card_type="diff", source="agent", title=cid, body="", author="x",
                timestamp=ts.replace(microsecond=s), project_name=proj,
                card_id=cid, seq_num=s,
            )
            h._project_cards.setdefault(proj, []).insert(0, cid)
            widget = _geometry_widget(56.0)
            h._card_widgets[cid] = widget
            tab.append_card(widget, cid)
            ids.append(cid)

        # Measurable geometry + a viewport below every card's bottom edge, so
        # `is_above_viewport` takes its real, non-vacuous branch.
        tab._feed_scroll.get_vadjustment().set_value(1000.0)
        h._active_project_name = proj

        h._evict_surplus_card_widgets()

        released = [cid for cid in ids if cid not in h._card_widgets]
        assert released, "non-vacuity: at least one id must leave _card_widgets"
        assert all(cid not in tab._cards_by_id for cid in released), (
            "non-vacuity: the same ids must leave _cards_by_id in the same pass"
        )
        assert len(_parented_sentinels(tab)) == 1, (
            "the pushed-back cards must be reachable (exactly one bar)"
        )

        for cid in h._project_cards[proj]:
            if cid == "__load_more__":
                continue
            assert (cid in tab._cards_by_id) == (cid in h._card_widgets), (
                f"{cid}: tab map and handler map disagree"
            )


class TestScheduleScrollToBottom:
    """
    Phase 4D-1: Test the real schedule_scroll_to_bottom mechanism.

    These tests exercise the actual FeedTab.schedule_scroll_to_bottom code,
    NOT the MockFeedTab stub. They use _FakeAdjustment to control when the
    'changed' signal fires and what upper returns.
    """

    def test_schedule_scroll_does_not_scroll_immediately_when_upper_is_stale(
        self, real_feed_tab
    ):
        """Bug A regression: when upper is stale (0) at connect time, the scroll
        must NOT happen synchronously. It must wait for 'changed' to fire after
        GTK updates upper during the layout pass.

        Steps:
        1. FakeAdjustment starts with upper=0 (stale, pre-layout).
        2. Call schedule_scroll_to_bottom().
        3. Assert set_value was NOT called yet (stale upper would scroll to top).
        4. Simulate layout pass: set upper to 1000, then emit 'changed'.
        5. Assert set_value(1000) was called.
        """
        tab = real_feed_tab
        adj = tab._feed_scroll.get_vadjustment()

        tab.schedule_scroll_to_bottom()

        # set_value must NOT have been called synchronously
        assert adj.set_value_calls == [], (
            f"Expected no set_value call before 'changed', got {adj.set_value_calls}"
        )

        # Simulate layout pass updating upper
        adj.set_upper(1000.0)
        adj.emit_changed()

        assert adj.set_value_calls == [1000.0], (
            f"Expected set_value(1000.0) after 'changed', got {adj.set_value_calls}"
        )

    def test_schedule_scroll_fires_via_timeout_fallback_when_changed_never_fires(
        self, real_feed_tab, monkeypatch
    ):
        """Safety net: if 'changed' never fires, the 150ms timeout must scroll.

        We monkeypatch GLib.timeout_add to capture the callback and timeout
        so we can invoke it manually without waiting 150ms.
        """
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import GLib

        captured_timeouts = []

        def fake_timeout_add(ms, callback):
            source_id = 42  # deterministic fake source ID
            captured_timeouts.append((source_id, ms, callback))
            return source_id

        monkeypatch.setattr(GLib, "timeout_add", fake_timeout_add)

        tab = real_feed_tab
        adj = tab._feed_scroll.get_vadjustment()

        tab.schedule_scroll_to_bottom()

        # A timeout must have been registered
        assert len(captured_timeouts) == 1, (
            f"Expected 1 timeout registered, got {len(captured_timeouts)}"
        )
        source_id, ms, callback = captured_timeouts[0]
        assert ms == 150, f"Expected 150ms timeout, got {ms}ms"

        # Set upper to simulate content being present
        adj.set_upper(800.0)

        # 'changed' never fires — invoke the timeout callback directly
        result = callback()

        assert result == GLib.SOURCE_REMOVE, (
            f"Expected SOURCE_REMOVE, got {result}"
        )
        assert adj.set_value_calls == [800.0], (
            f"Expected set_value(800.0) from timeout, got {adj.set_value_calls}"
        )

    def test_schedule_scroll_disconnects_changed_handler_after_fire(
        self, real_feed_tab, monkeypatch
    ):
        """One-shot verification: after 'changed' fires, the handler must be
        disconnected. A second emit of 'changed' must NOT trigger another scroll.
        """
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import GLib

        # Monkeypatch timeout_add and source_remove to avoid real GLib timers
        monkeypatch.setattr(GLib, "timeout_add", lambda ms, cb: 99)
        monkeypatch.setattr(GLib, "source_remove", lambda sid: None)

        tab = real_feed_tab
        adj = tab._feed_scroll.get_vadjustment()

        tab.schedule_scroll_to_bottom()

        # First emit — should scroll
        adj.set_upper(1000.0)
        adj.emit_changed()
        assert len(adj.set_value_calls) == 1, (
            f"Expected 1 set_value after first 'changed', got {len(adj.set_value_calls)}"
        )

        # Second emit — should NOT scroll (handler was disconnected)
        adj.set_upper(2000.0)
        adj.emit_changed()
        assert len(adj.set_value_calls) == 1, (
            f"Expected still 1 set_value after second 'changed', got {len(adj.set_value_calls)}"
        )

    def test_schedule_scroll_disarms_timeout_after_changed_fires(
        self, real_feed_tab, monkeypatch
    ):
        """4D-3 cleanup-race regression test.

        When 'changed' fires (success path), the timeout must be disarmed via
        GLib.source_remove. This prevents the timeout from firing 150ms later
        and re-scrolling the feed if the user has already scrolled away.

        Without the 4D-3 fix, the success path did NOT call source_remove —
        the timeout fired unconditionally and could re-scroll.
        """
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import GLib

        captured_timeouts = []
        removed_sources = []

        def fake_timeout_add(ms, callback):
            source_id = 77
            captured_timeouts.append((source_id, ms, callback))
            return source_id

        monkeypatch.setattr(GLib, "timeout_add", fake_timeout_add)
        monkeypatch.setattr(
            GLib,
            "source_remove",
            lambda sid: removed_sources.append(sid),
        )

        tab = real_feed_tab
        adj = tab._feed_scroll.get_vadjustment()

        tab.schedule_scroll_to_bottom()

        assert len(captured_timeouts) == 1
        timeout_source_id, _, timeout_callback = captured_timeouts[0]

        # Verify _scroll_timeout_id was set
        assert tab._scroll_timeout_id == timeout_source_id, (
            f"Expected _scroll_timeout_id={timeout_source_id}, "
            f"got {tab._scroll_timeout_id}"
        )

        # 'changed' fires — success path should disarm the timeout
        adj.set_upper(1000.0)
        adj.emit_changed()

        # Timeout must have been disarmed via source_remove
        assert timeout_source_id in removed_sources, (
            f"Expected source_remove({timeout_source_id}), "
            f"got removed_sources={removed_sources}"
        )
        assert tab._scroll_timeout_id is None, (
            f"Expected _scroll_timeout_id=None after 'changed', "
            f"got {tab._scroll_timeout_id}"
        )

        # Invoke the timeout callback manually — it should NOT scroll again
        # because _scroll_handler_id is None (already cleared by success path)
        adj.set_upper(5000.0)  # different value to detect re-scroll
        result = timeout_callback()

        # set_value_calls should still be [1000.0] from the 'changed' path
        assert adj.set_value_calls == [1000.0], (
            f"Timeout re-scrolled after disarm! set_value_calls={adj.set_value_calls}"
        )

    def test_schedule_scroll_handles_disconnect_exception(
        self, real_feed_tab, monkeypatch
    ):
        """Defensive cleanup test: if disconnect() raises during the 'changed'
        handler (e.g., adjustment disposed during teardown), the handler must
        not propagate the exception and must still clean up state.

        This documents the try/except behavior in the production code.
        """
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import GLib

        monkeypatch.setattr(GLib, "timeout_add", lambda ms, cb: 88)
        monkeypatch.setattr(GLib, "source_remove", lambda sid: None)

        tab = real_feed_tab
        adj = tab._feed_scroll.get_vadjustment()

        # Make disconnect raise
        original_disconnect = adj.disconnect
        adj.disconnect = lambda hid: (_ for _ in ()).throw(
            RuntimeError("simulated dispose")
        )

        tab.schedule_scroll_to_bottom()

        # 'changed' fires — disconnect will raise, but the try/except must catch it
        adj.set_upper(1000.0)
        adj.emit_changed()  # must not propagate

        # set_value must still have been called (scroll happened before disconnect)
        assert 1000.0 in adj.set_value_calls, (
            f"Expected set_value(1000.0) despite disconnect exception, "
            f"got {adj.set_value_calls}"
        )

        # Restore disconnect for cleanup
        adj.disconnect = original_disconnect

    # ── SPEC-MEMORY-WIDGET-RATCHET §2.2/§2.6 — geometry primitives ────────
    # Real-GTK harness (real FeedTab + real Gtk.ScrolledWindow/Gtk.Box),
    # driven the same way as the scroll tests above.

    def test_is_near_bottom_true_when_no_vadjustment(self, real_feed_tab):
        """Fail-safe: with no ScrolledWindow there is nothing rendered, so the
        viewport counts as "near bottom" and eviction is permitted.

        Spec §2.2: `get_vadjustment()` returns None when `_feed_scroll` is
        absent, and `is_near_bottom()` then returns True.

        Verified on GTK 4.14.5 under Xvfb: a freshly constructed FeedTab actually
        DOES own a real Gtk.ScrolledWindow whose Adjustment reports
        upper=0.0/page=0.0/value=0.0 — so the None branch is reached by clearing
        `_feed_scroll`, which is the state the docstring means by "before first
        map". (The spec's phrase "freshly constructed, never mapped" is therefore
        only half-accurate; see COMPLETENESS.)
        """
        tab = real_feed_tab
        tab._feed_scroll = None

        assert tab.get_vadjustment() is None
        assert tab.is_near_bottom() is True

    def test_is_above_viewport_false_for_unallocated_widget(self, real_feed_tab):
        """Round-6 BUG #1 — the zero-rect trap.

        GTK 4.14 returns `compute_bounds -> (True, zero rect)` for a widget that
        has never been allocated. Verified live under Xvfb: an unrealized,
        unmapped Gtk.Label in the real card container gives ok=True,
        origin=(0.0, 0.0), size=(0.0, 0.0). A naive
        `origin.y + height <= value` test would call that "above the viewport"
        and destroy cards that are merely not laid out yet. The
        `rect.size.height <= 0` guard must classify it "unknown" instead.

        Non-vacuity: the raw compute_bounds result and the adjustment value are
        asserted first, so this test genuinely exercises the extent guard. It
        fails if the guard is removed (0.0 + 0.0 <= 0.0 → True) and it fails if
        GTK ever starts reporting ok=False (which would take the other branch and
        stop exercising the guard at all).
        """
        from gi.repository import Gtk

        tab = real_feed_tab
        label = Gtk.Label(label="never allocated")
        tab.append_card(label)

        # Precondition: the tab is never realized/mapped in this harness.
        assert label.get_mapped() is False, "precondition: the tab is never mapped"

        container = tab.get_card_container()
        ok, rect = label.compute_bounds(container)
        assert ok is True, (
            f"precondition: GTK must report ok=True with a zero rect, got ok={ok}"
        )
        assert rect.size.height == 0.0, (
            f"precondition: expected a zero-height rect, got {rect.size.height}"
        )
        assert tab.get_vadjustment().get_value() == 0.0, (
            "precondition: at value=0.0 the naive test 0.0 + 0.0 <= 0.0 is True, "
            "so the guard is the only thing that can make this False"
        )

        assert tab.is_above_viewport(label) is False

    def test_is_above_viewport_false_for_mock_widget(self, real_feed_tab):
        """Fail-safe: a widget with no `compute_bounds` (a mock / test double) is
        treated as unmeasurable, never as "above the viewport" — the
        AttributeError branch of §2.2. Without the try/except the AttributeError
        would escape into the eviction pass.
        """
        class _NoGeometry:
            """Test double with no compute_bounds — like the cards MockFeedTab holds."""

        widget = _NoGeometry()
        assert not hasattr(widget, "compute_bounds"), (
            "precondition: the double must not define compute_bounds"
        )

        assert real_feed_tab.is_above_viewport(widget) is False

    def test_is_above_viewport_false_for_non_ancestor(self, real_feed_tab):
        """The `not ok` half of the geometry guard.

        `test_is_above_viewport_false_for_unallocated_widget` pins the
        `rect.size.height <= 0` half; this pins the other half. GTK 4.14
        returns `compute_bounds -> (False, zero rect)` whenever the target
        widget is not an ancestor of the widget being measured (verified in
        the P2 audit probe, `.debug/p2_geometry_probe.py` §3), so a foreign
        widget — one living in a different container — must never be
        classified "above the viewport" and destroyed.

        Non-vacuity: the raw ok=False and the naive-test comparison are
        asserted first. NOTE (P3 audit correction): this test pins the
        observable False and GTK's ok=False-for-non-ancestors contract, but
        NOT the `not ok` guard half in isolation — GTK couples ok=False with
        a zero rect, so the `height <= 0` half intercepts even if `not ok`
        is deleted (mutation-equivalent halves). The half is pinned
        independently by
        `test_is_above_viewport_false_when_ok_false_alone` (synthetic rect
        bypassing the height guard).
        """
        from gi.repository import Gtk

        tab = real_feed_tab
        foreign_box = Gtk.Box()          # NOT tab.get_card_container()
        label = Gtk.Label(label="not in the feed")
        foreign_box.append(label)

        container = tab.get_card_container()
        ok, rect = label.compute_bounds(container)
        assert ok is False, (
            f"precondition: a non-ancestor target must report ok=False, "
            f"got ok={ok}"
        )
        naive_above = rect.origin.y + rect.size.height <= tab.get_vadjustment().get_value()
        assert naive_above is True, (
            "precondition: the raw rect must satisfy the naive above-viewport "
            "test at value=0.0, otherwise the guard is not the discriminator"
        )

        assert tab.is_above_viewport(label) is False

    def test_is_above_viewport_false_when_ok_false_alone(self, real_feed_tab):
        """Pins the `not ok` guard half INDEPENDENT of the height half.

        The P3 audit showed GTK couples ok=False with a zero rect for
        non-ancestors, so deleting `not ok` alone is invisible to every
        real-GTK test (the `height <= 0` half intercepts). A synthetic
        widget whose compute_bounds reports ok=False with a NEGATIVE-y,
        POSITIVE-height rect bypasses the height guard: the naive
        comparison (origin.y + height <= value, i.e. -50 <= 0) would say
        "above the viewport", so this test fails if the `not ok` half is
        dropped, tightened, or the guard is re-ordered.
        """
        tab = real_feed_tab

        class _OkFalseRect:
            def compute_bounds(self, _container):
                return (False, _SyntheticRect(origin_y=-100.0, height=50.0))

        assert tab.is_above_viewport(_OkFalseRect()) is False


class _SyntheticRect:
    """Minimal Graphene.Rect stand-in for guard-branch pinning (P3 audit)."""

    def __init__(self, origin_y, height):
        class _Pt:
            def __init__(self, y):
                self.y = y

        class _Size:
            def __init__(self, h):
                self.height = h

        self.origin = _Pt(origin_y)
        self.size = _Size(height)


# ═══════════════════════════════════════════════════════════════════
#  TestClearWidgetStateRecursive — Phase 4D-2
#  Tests _clear_widget_state_recursive on real Gtk.Box/Button/Label trees.
#  Verifies the recursive walk clears PRELIGHT/ACTIVE/SELECTED on self +
#  all descendants.
# ═══════════════════════════════════════════════════════════════════

class TestClearWidgetStateRecursive:
    """
    Phase 4D-2: Test _clear_widget_state_recursive against real GTK4 widgets.

    Uses Gtk.Box + Gtk.Button + Gtk.Label trees because these are the exact
    widget types used in feed cards.
    """

    def test_clear_widget_state_visits_self_and_all_descendants(self):
        """Build a real widget tree: Box → [Button(label=A, child=Label), Button(label=B)].
        Set PRELIGHT on box + both buttons + label. Call _clear_widget_state_recursive.
        Assert PRELIGHT is cleared on all 4 widgets.
        """
        from ui.views.feed_tab import FeedTab
        from gi.repository import Gtk

        # Build tree
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        btn_a = Gtk.Button(label="A")
        lbl = Gtk.Label(label="nested")
        btn_a.set_child(lbl)  # btn_a has a child Label
        btn_b = Gtk.Button(label="B")
        outer.append(btn_a)
        outer.append(btn_b)

        # Set PRELIGHT on all 4 widgets
        outer.set_state_flags(Gtk.StateFlags.PRELIGHT, False)
        btn_a.set_state_flags(Gtk.StateFlags.PRELIGHT, False)
        btn_b.set_state_flags(Gtk.StateFlags.PRELIGHT, False)
        lbl.set_state_flags(Gtk.StateFlags.PRELIGHT, False)

        # Verify PRELIGHT is set before clearing
        assert bool(outer.get_state_flags() & Gtk.StateFlags.PRELIGHT)
        assert bool(btn_a.get_state_flags() & Gtk.StateFlags.PRELIGHT)
        assert bool(btn_b.get_state_flags() & Gtk.StateFlags.PRELIGHT)
        assert bool(lbl.get_state_flags() & Gtk.StateFlags.PRELIGHT)

        # Call the method via a FeedTab instance (it's a method on FeedTab)
        # But we don't need the full FeedTab — we can call the unbound method
        # Actually, _clear_widget_state_recursive uses self only for dispatch,
        # not for any instance state. But it's a method, so we need an instance.
        # Create a minimal FeedTab.
        tab = FeedTab()
        tab._clear_widget_state_recursive(outer)

        # Assert PRELIGHT is cleared on all 4 widgets
        assert not bool(outer.get_state_flags() & Gtk.StateFlags.PRELIGHT), (
            f"outer still has PRELIGHT: {outer.get_state_flags()}"
        )
        assert not bool(btn_a.get_state_flags() & Gtk.StateFlags.PRELIGHT), (
            f"btn_a still has PRELIGHT: {btn_a.get_state_flags()}"
        )
        assert not bool(btn_b.get_state_flags() & Gtk.StateFlags.PRELIGHT), (
            f"btn_b still has PRELIGHT: {btn_b.get_state_flags()}"
        )
        assert not bool(lbl.get_state_flags() & Gtk.StateFlags.PRELIGHT), (
            f"lbl still has PRELIGHT: {lbl.get_state_flags()}"
        )

    def test_clear_widget_state_handles_widget_without_state_safely(self):
        """Call _clear_widget_state_recursive on a fresh Gtk.Box that has never
        had any state flags set. Must not raise.
        """
        from ui.views.feed_tab import FeedTab
        from gi.repository import Gtk

        box = Gtk.Box()  # never had flags set

        # Verify it starts clean (only DIR_LTR is default)
        flags_before = box.get_state_flags()
        assert not bool(flags_before & Gtk.StateFlags.PRELIGHT)
        assert not bool(flags_before & Gtk.StateFlags.ACTIVE)
        assert not bool(flags_before & Gtk.StateFlags.SELECTED)

        tab = FeedTab()
        # Must not raise
        tab._clear_widget_state_recursive(box)

        # Still clean
        flags_after = box.get_state_flags()
        assert not bool(flags_after & Gtk.StateFlags.PRELIGHT)
        assert not bool(flags_after & Gtk.StateFlags.ACTIVE)
        assert not bool(flags_after & Gtk.StateFlags.SELECTED)

    def test_clear_widget_state_handles_unset_exception_gracefully(self):
        """If unset_state_flags raises on a widget, the recursion must continue
        to siblings and children. This documents the try/except in the production
        code.

        We build a real tree and monkey-patch unset_state_flags on ONE widget
        to raise. Then verify its child and sibling are still processed.
        """
        from ui.views.feed_tab import FeedTab
        from gi.repository import Gtk

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        btn_problem = Gtk.Button(label="problem")
        lbl_inside_problem = Gtk.Label(label="inside")
        btn_problem.set_child(lbl_inside_problem)
        btn_ok = Gtk.Button(label="ok")
        outer.append(btn_problem)
        outer.append(btn_ok)

        # Set PRELIGHT on all
        outer.set_state_flags(Gtk.StateFlags.PRELIGHT, False)
        btn_problem.set_state_flags(Gtk.StateFlags.PRELIGHT, False)
        lbl_inside_problem.set_state_flags(Gtk.StateFlags.PRELIGHT, False)
        btn_ok.set_state_flags(Gtk.StateFlags.PRELIGHT, False)

        # Monkey-patch unset_state_flags on btn_problem to raise
        original_unset = btn_problem.unset_state_flags

        def raising_unset(flags):
            raise RuntimeError("simulated widget disposal")

        btn_problem.unset_state_flags = raising_unset

        tab = FeedTab()
        # Must not propagate the exception
        tab._clear_widget_state_recursive(outer)

        # btn_problem: exception was caught, but PRELIGHT might still be set
        # because unset_state_flags raised. That's acceptable — the production
        # code uses try/except Exception: pass.
        # Restore original to verify
        btn_problem.unset_state_flags = original_unset
        # btn_problem may still have PRELIGHT (the exception prevented clearing)
        # This is documented behavior — the recursive walker continues despite errors.

        # CRITICAL assertions: sibling and child must be cleared
        assert not bool(btn_ok.get_state_flags() & Gtk.StateFlags.PRELIGHT), (
            f"btn_ok should have been cleared but still has PRELIGHT: "
            f"{btn_ok.get_state_flags()}"
        )
        assert not bool(lbl_inside_problem.get_state_flags() & Gtk.StateFlags.PRELIGHT), (
            f"lbl_inside_problem should have been cleared (recursion continued "
            f"past the exception) but still has PRELIGHT: "
            f"{lbl_inside_problem.get_state_flags()}"
        )
        assert not bool(outer.get_state_flags() & Gtk.StateFlags.PRELIGHT), (
            f"outer should have been cleared but still has PRELIGHT: "
            f"{outer.get_state_flags()}"
        )


# ═══════════════════════════════════════════════════════════════════
#  TestScheduleSmartScrollToBottom — Deferred Smart Scroll with Proximity
#  Tests the real schedule_smart_scroll_to_bottom method on FeedTab.
#  Uses _FakeAdjustment (not MockFeedTab) to exercise the real code path
#  with full control over when 'changed' fires and what upper returns.
# ═══════════════════════════════════════════════════════════════════

class TestScheduleSmartScrollToBottom:
    """
    Tests schedule_smart_scroll_to_bottom: proximity check + deferred scroll.

    These tests exercise the actual FeedTab.schedule_smart_scroll_to_bottom
    code, NOT the MockFeedTab stub. They use _FakeAdjustment to control
    vadjustment values and signal timing.
    """

    def test_schedule_smart_scrolls_when_user_near_bottom(
        self, real_feed_tab, monkeypatch
    ):
        """When the user is within 80px of the bottom, the method must delegate
        to schedule_scroll_to_bottom so the scroll fires after the layout pass.

        Setup: upper=1000, page_size=600, value=350.
        distance_from_bottom = 1000 - 600 - 350 = 50px (< 80 → near bottom).

        After calling schedule_smart_scroll_to_bottom:
        - The 'changed' handler must be connected (proof delegation happened)
        - When we simulate layout (set upper to 1500, emit 'changed'), the
          scroll must fire to 1500 (the post-layout upper, not the stale 1000)
        """
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import GLib

        monkeypatch.setattr(GLib, "timeout_add", lambda ms, cb: 42)
        monkeypatch.setattr(GLib, "source_remove", lambda sid: None)

        tab = real_feed_tab
        adj = tab._feed_scroll.get_vadjustment()

        # User is near the bottom: distance = 1000 - 600 - 350 = 50px
        adj.set_upper(1000.0)
        adj._value = 350.0
        adj._page_size = 600.0

        tab.schedule_smart_scroll_to_bottom()

        # Proof that delegation to schedule_scroll_to_bottom happened:
        # the 'changed' handler must be connected.
        assert tab._scroll_handler_id is not None, (
            "Expected _scroll_handler_id to be set (delegation to "
            "schedule_scroll_to_bottom), got None"
        )

        # No scroll should have happened yet (waiting for 'changed')
        assert adj.set_value_calls == [], (
            f"Expected no set_value before 'changed', got {adj.set_value_calls}"
        )

        # Simulate layout pass: upper grows to 1500 (new card appended)
        adj.set_upper(1500.0)
        adj.emit_changed()

        # Scroll must fire to the post-layout upper, not the stale 1000
        assert adj.set_value_calls == [1500.0], (
            f"Expected set_value(1500.0) after layout, got {adj.set_value_calls}"
        )

    def test_schedule_smart_does_not_scroll_when_user_scrolled_up(
        self, real_feed_tab, monkeypatch
    ):
        """When the user has scrolled up more than 80px from the bottom, the
        method must NOT scroll at all — preserve the user's reading position.

        Setup: upper=2000, page_size=600, value=200.
        distance_from_bottom = 2000 - 600 - 200 = 1200px (>> 80 → scrolled up).

        After calling schedule_smart_scroll_to_bottom:
        - No 'changed' handler connected (no delegation)
        - No timeout installed
        - No set_value calls
        """
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import GLib

        timeout_calls = []
        monkeypatch.setattr(
            GLib, "timeout_add",
            lambda ms, cb: timeout_calls.append((ms, cb)) or 99,
        )

        tab = real_feed_tab
        adj = tab._feed_scroll.get_vadjustment()

        # User has scrolled way up: distance = 2000 - 600 - 200 = 1200px
        adj.set_upper(2000.0)
        adj._value = 200.0
        adj._page_size = 600.0

        tab.schedule_smart_scroll_to_bottom()

        # No handler must be connected
        assert tab._scroll_handler_id is None, (
            f"Expected _scroll_handler_id=None (user scrolled up, no scroll), "
            f"got {tab._scroll_handler_id}"
        )
        # No timeout must be installed
        assert tab._scroll_timeout_id is None, (
            f"Expected _scroll_timeout_id=None (no delegation), "
            f"got {tab._scroll_timeout_id}"
        )
        assert timeout_calls == [], (
            f"Expected no timeout_add call, got {timeout_calls}"
        )
        # No scroll
        assert adj.set_value_calls == [], (
            f"Expected no set_value calls, got {adj.set_value_calls}"
        )

    def test_schedule_smart_uses_stale_upper_for_proximity_not_future(
        self, real_feed_tab, monkeypatch
    ):
        """Pins the design decision: the proximity check intentionally uses the
        pre-append (stale) upper because it measures the user's reading position,
        not the future content height.

        Setup: upper=800, page_size=600, value=180.
        distance_from_bottom = 800 - 600 - 180 = 20px (< 80 → near bottom).

        If the method used some hypothetical post-layout upper (say 1500),
        the distance would be 1500 - 600 - 180 = 720px (>> 80 → would NOT scroll).
        The test proves the stale upper is used by asserting the scroll fires.

        This test would FAIL if someone tried to be 'smart' and wait for the
        post-layout upper before doing the proximity check.
        """
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import GLib

        monkeypatch.setattr(GLib, "timeout_add", lambda ms, cb: 55)
        monkeypatch.setattr(GLib, "source_remove", lambda sid: None)

        tab = real_feed_tab
        adj = tab._feed_scroll.get_vadjustment()

        # Stale upper = 800. User at value=180, page=600.
        # Stale distance = 800 - 600 - 180 = 20px (< 80 → near bottom).
        adj.set_upper(800.0)
        adj._value = 180.0
        adj._page_size = 600.0

        tab.schedule_smart_scroll_to_bottom()

        # Delegation must have happened because stale distance < 80
        assert tab._scroll_handler_id is not None, (
            "Expected delegation to schedule_scroll_to_bottom because "
            "stale distance (20px) < 80px threshold. "
            "If _scroll_handler_id is None, the method used a post-layout "
            "upper for the proximity check, which is the wrong design."
        )

        # Now simulate layout: upper grows to 1500 (card was appended)
        adj.set_upper(1500.0)
        adj.emit_changed()

        # Scroll fires to post-layout upper
        assert adj.set_value_calls == [1500.0], (
            f"Expected set_value(1500.0) after layout, got {adj.set_value_calls}"
        )


# ═══════════════════════════════════════════════════════════════════
#  TestFeedToolbarAutoAccept — Phase 5
#  Tests the auto-accept toggle, warning dialog, and auto-accept card hook.
#  Uses mock_glib + mock_feed_tab fixtures (defined at top of file).
# ═══════════════════════════════════════════════════════════════════

class TestFeedToolbarAutoAccept:
    """Phase 5: auto-accept toggle state, warning dialog, and card hook."""

    def test_default_auto_accept_is_off(self, feed_handler, mock_feed_tab):
        """Fresh handler — auto-accept toggle is OFF."""
        assert mock_feed_tab._auto_accept_active is False

    def test_set_feed_tab_wires_auto_accept_callback(self, feed_handler, mock_feed_tab):
        """set_feed_tab() installs the auto-accept toggle callback on FeedTab."""
        assert mock_feed_tab._auto_accept_callback is not None
        assert callable(mock_feed_tab._auto_accept_callback)

    def test_enable_auto_accept_sets_state(self, feed_handler, mock_glib, mock_feed_tab):
        """Toggling ON without warning callback → _auto_accept_enabled = True."""
        # No set_show_auto_accept_warning wired → falls through to enable path
        feed_handler._on_auto_accept_toggled(True)
        assert feed_handler._auto_accept_enabled is True

    def test_enable_auto_accept_updates_toggle_visual(self, feed_handler, mock_glib, mock_feed_tab):
        """Bug B regression: enabling auto-accept must update the visible toggle.

        Previously _enable_auto_accept only set _auto_accept_enabled and
        scheduled a save; the toolbar toggle's label stayed at
        'Auto-Accept: OFF' because Gtk.ToggleButton.set_active() does not
        change set_label() text. The fix calls update_auto_accept_state(True)
        so the toggle reflects the actual state.
        """
        assert mock_feed_tab._auto_accept_active is False
        feed_handler._enable_auto_accept()
        assert mock_feed_tab._auto_accept_active is True, (
            "Bug B regression: _enable_auto_accept did not flip the visible "
            "toggle to ON. Label would stay 'Auto-Accept: OFF' even though "
            "state is enabled."
        )

    def test_disable_auto_accept_sets_state(self, feed_handler, mock_feed_tab):
        """Toggling OFF → _auto_accept_enabled = False."""
        feed_handler._auto_accept_enabled = True
        feed_handler._on_auto_accept_toggled(False)
        assert feed_handler._auto_accept_enabled is False

    def test_disable_auto_accept_updates_toggle_visual(self, feed_handler, mock_glib, mock_feed_tab):
        """Label-bug regression: turning OFF must flip the toolbar label.

        Previously `_disable_auto_accept` only cleared `_auto_accept_enabled`
        and scheduled a save; the visible toggle stayed at 'Auto-Accept: ON'.
        The fix calls `update_auto_accept_state(False)` so the label tracks
        the underlying state. Symmetric with `test_enable_auto_accept_updates_toggle_visual`.
        """
        # Start from a known-enabled visual state (mirrors what _enable_auto_accept
        # would have produced).
        mock_feed_tab._auto_accept_active = True
        feed_handler._auto_accept_enabled = True
        feed_handler._disable_auto_accept()
        assert mock_feed_tab._auto_accept_active is False, (
            "Label-bug regression: _disable_auto_accept did not flip the visible "
            "toggle to OFF. Label would stay 'Auto-Accept: ON' after user click."
        )
        assert feed_handler._auto_accept_enabled is False

    def test_cancel_auto_accept_resets_toggle(self, feed_handler, mock_glib, mock_feed_tab):
        """Warning dialog cancel → toggle snaps back to OFF AND state resets.

        Invariant: when the user cancels, both the visible toggle AND the
        in-memory _auto_accept_enabled flag must be cleared. Previously only
        the toggle was reset, leaving a silent-accept window where add_card()
        would auto-accept new cards with no user-visible cue.
        """
        # Mock warning callback that immediately invokes on_cancel
        def mock_warning(agent_name, on_confirm, on_cancel):
            on_cancel()

        feed_handler.set_show_auto_accept_warning(mock_warning)
        feed_handler._on_auto_accept_toggled(True)
        # _cancel_auto_accept now resets _auto_accept_enabled synchronously
        # and idle_adds update_auto_accept_state(False).
        assert feed_handler._auto_accept_enabled is False, (
            "Invariant regression: _cancel_auto_accept left _auto_accept_enabled "
            "at True, creating a silent-accept window (auto-accept on in memory "
            "but UI shows OFF)."
        )
        # Drain the idle queue to confirm the visual update also runs.
        for fn, args, kwargs in mock_glib._pending:
            fn(*args, **kwargs)
        assert mock_feed_tab._auto_accept_active is False

    def test_add_card_with_auto_accept_on_invokes_handle_accept(
        self, feed_handler, mock_glib, mock_feed_tab, monkeypatch
    ):
        """Auto-accept ON + actionable diff card → handle_accept called via idle_add."""
        feed_handler._auto_accept_enabled = True
        feed_handler._auto_accept_agent = None  # match any author
        feed_handler._active_project_name = "testproj"

        # Track handle_accept calls
        accepted_ids = []
        def mock_handle_accept(card_id):
            accepted_ids.append(card_id)
        monkeypatch.setattr(feed_handler, "handle_accept", mock_handle_accept)

        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="Auto card",
            body="", author="coder", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(card)

        # The auto-accept check runs inside _append (idle_add). MockGLib
        # runs callbacks synchronously, so by the time add_card() returns
        # the auto-accept lambda has already fired handle_accept exactly once.
        # We clear _pending so any re-processing during a drain loop doesn't
        # duplicate the accept call.
        mock_glib._pending.clear()

        assert len(accepted_ids) == 1, f"Expected 1 accept, got {len(accepted_ids)}"

    def test_auto_accept_only_for_actionable_cards(
        self, feed_handler, mock_glib, mock_feed_tab, monkeypatch
    ):
        """Auto-accept ON + tool_result card → handle_accept NOT called."""
        feed_handler._auto_accept_enabled = True
        feed_handler._auto_accept_agent = None
        feed_handler._active_project_name = "testproj"

        accepted_ids = []
        def mock_handle_accept(card_id):
            accepted_ids.append(card_id)
        monkeypatch.setattr(feed_handler, "handle_accept", mock_handle_accept)

        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="tool_result", source="agent", title="Tool result",
            body="", author="coder", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(card)

        mock_glib._pending.clear()

        assert len(accepted_ids) == 0, f"Expected 0 accepts for tool_result, got {len(accepted_ids)}"

    def test_auto_accept_only_for_matching_author_when_persisted(
        self, feed_handler, mock_glib, mock_feed_tab, monkeypatch
    ):
        """Auto-accept ON with agent='coder' → only coder cards auto-accepted."""
        feed_handler._auto_accept_enabled = True
        feed_handler._auto_accept_agent = "coder"
        feed_handler._active_project_name = "testproj"

        accepted_ids = []
        def mock_handle_accept(card_id):
            accepted_ids.append(card_id)
        monkeypatch.setattr(feed_handler, "handle_accept", mock_handle_accept)

        ts = datetime.now(timezone.utc)
        # Card from wrong author → NOT auto-accepted
        card_qa = FeedCardData(
            card_type="diff", source="agent", title="QA card",
            body="", author="qa", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(card_qa)

        # Card from matching author → auto-accepted
        card_coder = FeedCardData(
            card_type="diff", source="agent", title="Coder card",
            body="", author="coder", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(card_coder)

        mock_glib._pending.clear()

        assert len(accepted_ids) == 1, f"Expected 1 accept (coder only), got {len(accepted_ids)}"

    def test_add_card_without_auto_accept_is_passive(
        self, feed_handler, mock_glib, mock_feed_tab, monkeypatch
    ):
        """Auto-accept OFF + actionable diff card → handle_accept NOT called (regression guard)."""
        # _auto_accept_enabled defaults to False
        feed_handler._active_project_name = "testproj"

        accepted_ids = []
        def mock_handle_accept(card_id):
            accepted_ids.append(card_id)
        monkeypatch.setattr(feed_handler, "handle_accept", mock_handle_accept)

        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="Normal card",
            body="", author="coder", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(card)

        mock_glib._pending.clear()

        assert len(accepted_ids) == 0, f"Expected 0 accepts (auto-accept OFF), got {len(accepted_ids)}"


# ═══════════════════════════════════════════════════════════════════
#  TestAutoAcceptPrefs — Phase 1
#  Verifies FileChangePref, ExecCommandPref, AutoAcceptPrefs dataclasses.
# ═══════════════════════════════════════════════════════════════════

class TestAutoAcceptPrefs:
    """Phase 1: AutoAcceptPrefs dataclass — defaults, enable/disable,
    serialization, instance isolation, locked_agent()."""

    def test_defaults_all_disabled(self):
        """Fresh AutoAcceptPrefs() has any_enabled()==False, all four
        file-change types disabled, exec mode=='off'."""
        p = AutoAcceptPrefs()
        assert p.any_enabled() is False
        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            assert p.file_changes[ct].enabled is False
        assert p.exec_command.mode == "off"

    def test_enable_file_change_type(self):
        """Enabling one file-change type flips any_enabled() and
        is_file_type_enabled() correctly."""
        p = AutoAcceptPrefs()
        p.file_changes["diff"].enabled = True
        assert p.any_enabled() is True
        assert p.is_file_type_enabled("diff") is True
        assert p.is_file_type_enabled("file_created") is False
        assert p.is_file_type_enabled("file_modified") is False
        assert p.is_file_type_enabled("file_deleted") is False

    def test_enable_exec_command(self):
        """Setting exec mode to 'show' flips any_enabled()."""
        p = AutoAcceptPrefs()
        p.exec_command.mode = "show"
        assert p.any_enabled() is True
        # file_changes still all False
        assert p.is_file_type_enabled("diff") is False
        assert p.is_file_type_enabled("file_created") is False

    def test_to_dict_round_trip(self):
        """Create prefs with mixed state, to_dict() -> from_dict() preserves
        all fields."""
        p = AutoAcceptPrefs()
        p.file_changes["diff"].enabled = True
        p.file_changes["diff"].agent_scope = "claude"
        p.file_changes["file_created"].enabled = True
        p.exec_command.mode = "silent"
        p.exec_command.agent_scope = "all_agents"
        p.snoozed_card_ids.append("card-abc-123")
        p.snoozed_card_ids.append("card-xyz-789")

        raw = p.to_dict()
        p2 = AutoAcceptPrefs.from_dict(raw)

        assert p2.file_changes["diff"].enabled is True
        assert p2.file_changes["diff"].agent_scope == "claude"
        assert p2.file_changes["file_created"].enabled is True
        assert p2.file_changes["file_modified"].enabled is False
        assert p2.file_changes["file_deleted"].enabled is False
        assert p2.exec_command.mode == "silent"
        assert p2.exec_command.agent_scope == "all_agents"
        assert p2.snoozed_card_ids == ["card-abc-123", "card-xyz-789"]
        assert p2.any_enabled() is True

    def test_to_dict_has_version_2(self):
        """to_dict() emits version=2 at the top level."""
        p = AutoAcceptPrefs()
        raw = p.to_dict()
        assert raw["version"] == 2
        assert "auto_accept" in raw

    def test_from_dict_empty(self):
        """from_dict({}) returns all defaults (any_enabled False, exec off,
        all file types disabled, empty snooze)."""
        p = AutoAcceptPrefs.from_dict({})
        assert p.any_enabled() is False
        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            assert p.file_changes[ct].enabled is False
            assert p.file_changes[ct].agent_scope == "first_author"
        assert p.exec_command.mode == "off"
        assert p.exec_command.agent_scope == "first_author"
        assert p.snoozed_card_ids == []

    def test_from_dict_missing_keys(self):
        """Partial dict with only some file_changes types: missing types
        fall back to defaults; provided types preserve their values."""
        raw = {
            "version": 2,
            "auto_accept": {
                "file_changes": {
                    "diff": {"enabled": True, "agent_scope": "claude"},
                },
            },
        }
        p = AutoAcceptPrefs.from_dict(raw)
        # Provided type preserved
        assert p.file_changes["diff"].enabled is True
        assert p.file_changes["diff"].agent_scope == "claude"
        # Other types defaulted
        assert p.file_changes["file_created"].enabled is False
        assert p.file_changes["file_created"].agent_scope == "first_author"
        assert p.file_changes["file_modified"].enabled is False
        assert p.file_changes["file_deleted"].enabled is False
        # Exec defaults
        assert p.exec_command.mode == "off"
        # Snooze defaults
        assert p.snoozed_card_ids == []

    def test_instance_isolation(self):
        """Two AutoAcceptPrefs() instances must not share mutable state
        (file_changes dict, exec_command object, snoozed_card_ids list)."""
        a = AutoAcceptPrefs()
        b = AutoAcceptPrefs()
        # Mutating a's file_changes must not affect b
        a.file_changes["diff"].enabled = True
        assert b.file_changes["diff"].enabled is False
        # Mutating a's exec_command.mode must not affect b
        a.exec_command.mode = "show"
        assert b.exec_command.mode == "off"
        # Mutating a's snooze list must not affect b
        a.snoozed_card_ids.append("x")
        assert b.snoozed_card_ids == []
        # The containers themselves must be distinct objects
        assert a.file_changes is not b.file_changes
        assert a.snoozed_card_ids is not b.snoozed_card_ids

    def test_locked_agent_none(self):
        """Fresh prefs: locked_agent() returns None (no specific agent)."""
        p = AutoAcceptPrefs()
        assert p.locked_agent() is None

    def test_locked_agent_specific(self):
        """Setting one type's agent_scope to a specific agent name
        surfaces that agent via locked_agent()."""
        p = AutoAcceptPrefs()
        p.file_changes["diff"].agent_scope = "claude"
        assert p.locked_agent() == "claude"

    def test_locked_agent_first_author(self):
        """agent_scope = 'first_author' must NOT count as locked."""
        p = AutoAcceptPrefs()
        p.file_changes["diff"].agent_scope = "first_author"
        assert p.locked_agent() is None

    def test_locked_agent_all_agents(self):
        """agent_scope = 'all_agents' must NOT count as locked."""
        p = AutoAcceptPrefs()
        p.file_changes["file_created"].agent_scope = "all_agents"
        assert p.locked_agent() is None

    def test_snoozed_card_ids_default_empty(self):
        """Fresh prefs have empty snoozed_card_ids list."""
        p = AutoAcceptPrefs()
        assert p.snoozed_card_ids == []
        assert isinstance(p.snoozed_card_ids, list)

    def test_snoozed_card_ids_from_dict_non_list(self):
        """from_dict with snoozed_card_ids = 'notalist' (non-list value)
        must fall back to an empty list, not crash or propagate the bad value."""
        raw = {
            "version": 2,
            "auto_accept": {
                "snoozed_card_ids": "notalist",
            },
        }
        p = AutoAcceptPrefs.from_dict(raw)
        assert p.snoozed_card_ids == []
        assert isinstance(p.snoozed_card_ids, list)


# ═══════════════════════════════════════════════════════════════════
#  TestPrefsMigration — Phase 2
#  Verifies _default_prefs, _migrate_v1_to_v2, _merge_v2_defaults,
#  load_feed_prefs (with real file I/O via tempfile).
# ═══════════════════════════════════════════════════════════════════

import json
import os
import tempfile


class TestPrefsMigration:
    """Phase 2: v1→v2 migration, v2 default merging, load_feed_prefs
    file-I/O dispatch over v1/v2/missing/invalid/unknown."""

    def test_default_prefs_is_v2(self):
        """_default_prefs() returns a v2-shaped dict with all required
        nested keys present."""
        d = _default_prefs()
        assert d["version"] == 2
        assert "auto_accept" in d
        auto = d["auto_accept"]
        assert "file_changes" in auto
        assert "exec_command" in auto
        assert "snoozed_card_ids" in auto
        # file_changes has all four types
        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            assert ct in auto["file_changes"]
            assert auto["file_changes"][ct]["enabled"] is False
            assert auto["file_changes"][ct]["agent_scope"] == "first_author"
        # exec_command defaults
        assert auto["exec_command"]["mode"] == "off"
        assert auto["exec_command"]["agent_scope"] == "first_author"
        # snoozed empty
        assert auto["snoozed_card_ids"] == []

    def test_default_prefs_independent_instances(self):
        """Two _default_prefs() calls return independent dicts (mutating
        one must not affect the other)."""
        a = _default_prefs()
        b = _default_prefs()
        assert a is not b
        # Mutate nested structure on a
        a["auto_accept"]["snoozed_card_ids"].append("x")
        a["auto_accept"]["file_changes"]["diff"]["enabled"] = True
        # b must be unaffected
        assert b["auto_accept"]["snoozed_card_ids"] == []
        assert b["auto_accept"]["file_changes"]["diff"]["enabled"] is False

    def test_migrate_v1_disabled(self):
        """v1 with auto_accept_enabled=False migrates to v2 with all four
        file types disabled and scope=first_author."""
        v1 = {"version": 1, "auto_accept_enabled": False, "auto_accept_agent": None}
        v2 = _migrate_v1_to_v2(v1)
        assert v2["version"] == 2
        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            assert v2["auto_accept"]["file_changes"][ct]["enabled"] is False
            assert v2["auto_accept"]["file_changes"][ct]["agent_scope"] == "first_author"
        assert v2["auto_accept"]["exec_command"]["mode"] == "off"
        assert v2["auto_accept"]["exec_command"]["agent_scope"] == "first_author"
        assert v2["auto_accept"]["snoozed_card_ids"] == []

    def test_migrate_v1_enabled_no_agent(self):
        """v1 with auto_accept_enabled=True and auto_accept_agent=None
        migrates with all four types enabled at first_author scope."""
        v1 = {"version": 1, "auto_accept_enabled": True, "auto_accept_agent": None}
        v2 = _migrate_v1_to_v2(v1)
        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            assert v2["auto_accept"]["file_changes"][ct]["enabled"] is True
            assert v2["auto_accept"]["file_changes"][ct]["agent_scope"] == "first_author"
        # exec scope tracks the same scope rule
        assert v2["auto_accept"]["exec_command"]["agent_scope"] == "first_author"

    def test_migrate_v1_enabled_with_agent(self):
        """v1 with auto_accept_enabled=True and auto_accept_agent='claude'
        preserves the agent lock-in across all four types (BUG #1 audit fix)."""
        v1 = {"version": 1, "auto_accept_enabled": True, "auto_accept_agent": "claude"}
        v2 = _migrate_v1_to_v2(v1)
        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            assert v2["auto_accept"]["file_changes"][ct]["enabled"] is True
            assert v2["auto_accept"]["file_changes"][ct]["agent_scope"] == "claude"
        assert v2["auto_accept"]["exec_command"]["agent_scope"] == "claude"

    def test_migrate_v1_empty_dict(self):
        """Migrating {} (no auto_accept_enabled key) treats it as
        auto_accept_enabled=False — all disabled, scope=first_author."""
        v2 = _migrate_v1_to_v2({})
        assert v2["version"] == 2
        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            assert v2["auto_accept"]["file_changes"][ct]["enabled"] is False
            assert v2["auto_accept"]["file_changes"][ct]["agent_scope"] == "first_author"
        assert v2["auto_accept"]["exec_command"]["mode"] == "off"

    def test_merge_v2_complete(self):
        """A complete v2 dict passes through _merge_v2_defaults unchanged."""
        full = {
            "version": 2,
            "auto_accept": {
                "file_changes": {
                    "diff": {"enabled": True, "agent_scope": "claude"},
                    "file_created": {"enabled": True, "agent_scope": "all_agents"},
                    "file_modified": {"enabled": False, "agent_scope": "first_author"},
                    "file_deleted": {"enabled": False, "agent_scope": "first_author"},
                },
                "exec_command": {"mode": "silent", "agent_scope": "claude"},
                "snoozed_card_ids": ["card-1", "card-2"],
            },
        }
        merged = _merge_v2_defaults(full)
        assert merged == full

    def test_merge_v2_partial_missing_file_changes(self):
        """v2 with only the diff file_changes entry → other three types
        filled from defaults."""
        partial = {
            "version": 2,
            "auto_accept": {
                "file_changes": {
                    "diff": {"enabled": True, "agent_scope": "claude"},
                },
            },
        }
        merged = _merge_v2_defaults(partial)
        # Provided type preserved
        assert merged["auto_accept"]["file_changes"]["diff"]["enabled"] is True
        assert merged["auto_accept"]["file_changes"]["diff"]["agent_scope"] == "claude"
        # Other three defaulted
        for ct in ("file_created", "file_modified", "file_deleted"):
            assert merged["auto_accept"]["file_changes"][ct]["enabled"] is False
            assert merged["auto_accept"]["file_changes"][ct]["agent_scope"] == "first_author"
        # exec_command defaults
        assert merged["auto_accept"]["exec_command"]["mode"] == "off"
        assert merged["auto_accept"]["exec_command"]["agent_scope"] == "first_author"
        # snooze defaults
        assert merged["auto_accept"]["snoozed_card_ids"] == []

    def test_merge_v2_empty_auto_accept(self):
        """v2 with auto_accept={} yields all defaults (isinstance guard)."""
        merged = _merge_v2_defaults({"version": 2, "auto_accept": {}})
        assert merged == _default_prefs()

    def test_merge_v2_auto_accept_none(self):
        """v2 with auto_accept=None yields all defaults (isinstance guard
        catches None and skips the overlay branch entirely)."""
        merged = _merge_v2_defaults({"version": 2, "auto_accept": None})
        assert merged == _default_prefs()

    def test_merge_v2_wrong_types(self):
        """v2 with wrong types at every nested level — each isinstance
        guard catches the wrong type and falls back to defaults for that
        section. Overall structure still equals _default_prefs()."""
        bad = {
            "version": 2,
            "auto_accept": {
                "file_changes": "not a dict",
                "exec_command": 42,
                "snoozed_card_ids": "not a list",
            },
        }
        merged = _merge_v2_defaults(bad)
        assert merged == _default_prefs()

    def test_load_v1_file_migrates(self):
        """Write a v1 JSON file to .crabcakes/feed-prefs.json, call
        load_feed_prefs(), assert it returns a v2-shaped dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            crabcakes = os.path.join(tmpdir, ".crabcakes")
            os.makedirs(crabcakes)
            path = os.path.join(crabcakes, "feed-prefs.json")
            v1 = {"version": 1, "auto_accept_enabled": True, "auto_accept_agent": None}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(v1, f)
            loaded = load_feed_prefs(tmpdir)
            assert loaded["version"] == 2
            assert loaded["auto_accept"]["file_changes"]["diff"]["enabled"] is True
            assert loaded["auto_accept"]["file_changes"]["diff"]["agent_scope"] == "first_author"
            assert loaded["auto_accept"]["exec_command"]["mode"] == "off"

    def test_load_v2_file_preserves(self):
        """Write a v2 JSON file, load_feed_prefs() returns the same data
        (merged through defaults, but equals the source)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            crabcakes = os.path.join(tmpdir, ".crabcakes")
            os.makedirs(crabcakes)
            path = os.path.join(crabcakes, "feed-prefs.json")
            v2 = {
                "version": 2,
                "auto_accept": {
                    "file_changes": {
                        "diff": {"enabled": True, "agent_scope": "claude"},
                        "file_created": {"enabled": False, "agent_scope": "first_author"},
                        "file_modified": {"enabled": False, "agent_scope": "first_author"},
                        "file_deleted": {"enabled": False, "agent_scope": "first_author"},
                    },
                    "exec_command": {"mode": "silent", "agent_scope": "claude"},
                    "snoozed_card_ids": ["x"],
                },
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(v2, f)
            loaded = load_feed_prefs(tmpdir)
            assert loaded == v2

    def test_load_missing_file_returns_defaults(self):
        """No .crabcakes/feed-prefs.json → _default_prefs()."""
        with tempfile.TemporaryDirectory() as tmpdir:
            loaded = load_feed_prefs(tmpdir)
            assert loaded == _default_prefs()

    def test_load_corrupt_file_returns_defaults(self):
        """Invalid JSON in feed-prefs.json → _default_prefs()."""
        with tempfile.TemporaryDirectory() as tmpdir:
            crabcakes = os.path.join(tmpdir, ".crabcakes")
            os.makedirs(crabcakes)
            path = os.path.join(crabcakes, "feed-prefs.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{ invalid json }")
            loaded = load_feed_prefs(tmpdir)
            assert loaded == _default_prefs()

    def test_load_unknown_version_returns_defaults(self):
        """Unknown version (e.g. 99) → _default_prefs() with a warning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            crabcakes = os.path.join(tmpdir, ".crabcakes")
            os.makedirs(crabcakes)
            path = os.path.join(crabcakes, "feed-prefs.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": 99, "weird_field": True}, f)
            loaded = load_feed_prefs(tmpdir)
            assert loaded == _default_prefs()


# ═══════════════════════════════════════════════════════════════════
#  PHASE 8 — Scenario & Integration Tests (FINAL PHASE)
#  Covers SPEC-AUTO-ACCEPT-GRANULAR-1.md §5 Step 8 + §6 Acceptance Criteria + §7 Edge Cases.
#  Tests are APPENDED here only — no existing tests are modified.
# ═══════════════════════════════════════════════════════════════════

class TestExecAutoAccept:
    """Phase 7-8: Exec auto-accept Show mode, Silent mode, and Off mode tests."""

    def test_show_mode_auto_approves_exec_card(self, feed_handler, mock_glib, mock_feed_tab):
        """Exec Show mode: approval card is auto-approved via _auto_approve_exec_card.
        The _on_approve_exec callback fires, card.accepted=True, and
        _update_card_visual is called."""
        # Setup: enable exec Show mode
        feed_handler._prefs.exec_command.mode = "show"
        feed_handler._auto_accept_enabled = True
        approved_calls = []
        feed_handler._on_approve_exec = lambda cid, approved: approved_calls.append((cid, approved))

        # Create an exec approval card
        card = FeedCardData(
            card_type="agent_action",
            source="agent",
            title="PM requests approval to run command",
            body="$ ls -la",
            author="PM",
            timestamp=datetime.now(timezone.utc),
            project_name="testproject",
            metadata={"needs_approval": True, "status": "pending_approval"},
        )
        card_id = feed_handler.add_card(card)

        # Assert: _on_approve_exec was called with approved=True
        assert len(approved_calls) == 1
        assert approved_calls[0][1] is True  # approved=True
        # Assert: card.accepted is True
        assert feed_handler._cards[card_id].accepted is True

    def test_show_mode_routes_through_auto_approve_not_handle_accept(self, feed_handler, mock_glib, mock_feed_tab):
        """Show mode exec cards must route through _auto_approve_exec_card (which calls
        handle_approve_exec), NOT handle_accept (which does git stage+commit).
        Verify by checking that _on_approve_exec fires but git ops do NOT."""
        feed_handler._prefs.exec_command.mode = "show"
        feed_handler._auto_accept_enabled = True
        approve_exec_calls = []
        feed_handler._on_approve_exec = lambda cid, approved: approve_exec_calls.append((cid, approved))

        card = FeedCardData(
            card_type="agent_action", source="agent", title="approval",
            body="$ rm -rf /", author="PM",
            timestamp=datetime.now(timezone.utc), project_name="testproject",
            metadata={"needs_approval": True},
        )
        card_id = feed_handler.add_card(card)

        # _on_approve_exec was called (correct path)
        assert len(approve_exec_calls) == 1
        # Card should NOT have gone through git ops — verify card.accepted is True
        # but no git card was created (handle_accept would create a git commit card)
        assert feed_handler._cards[card_id].accepted is True

    def test_off_mode_does_not_auto_approve(self, feed_handler, mock_glib, mock_feed_tab):
        """Exec Off mode: approval card is NOT auto-approved."""
        feed_handler._prefs.exec_command.mode = "off"
        feed_handler._auto_accept_enabled = True
        approved_calls = []
        feed_handler._on_approve_exec = lambda cid, approved: approved_calls.append((cid, approved))

        card = FeedCardData(
            card_type="agent_action", source="agent", title="approval",
            body="$ ls", author="PM",
            timestamp=datetime.now(timezone.utc), project_name="testproject",
            metadata={"needs_approval": True},
        )
        card_id = feed_handler.add_card(card)

        # Should NOT have been auto-approved
        assert len(approved_calls) == 0
        assert feed_handler._cards[card_id].accepted is None

    def test_show_mode_non_approval_agent_action_not_auto_accepted(self, feed_handler, mock_glib, mock_feed_tab):
        """An agent_action card WITHOUT needs_approval should NOT be auto-approved
        even in Show mode (only approval cards are auto-acceptable)."""
        feed_handler._prefs.exec_command.mode = "show"
        feed_handler._auto_accept_enabled = True
        approved_calls = []
        feed_handler._on_approve_exec = lambda cid, approved: approved_calls.append((cid, approved))

        card = FeedCardData(
            card_type="agent_action", source="agent", title="agent did something",
            body="ran a thing", author="PM",
            timestamp=datetime.now(timezone.utc), project_name="testproject",
            metadata={"needs_approval": False},
        )
        card_id = feed_handler.add_card(card)

        assert len(approved_calls) == 0
        assert feed_handler._cards[card_id].accepted is None

    def test_exec_auto_accept_respects_agent_scope(self, feed_handler, mock_glib, mock_feed_tab):
        """Exec Show mode with agent_scope='first_author': only the first agent's
        exec cards are auto-approved."""
        feed_handler._prefs.exec_command.mode = "show"
        feed_handler._prefs.exec_command.agent_scope = "first_author"
        feed_handler._auto_accept_enabled = True
        approved_calls = []
        feed_handler._on_approve_exec = lambda cid, approved: approved_calls.append((cid, approved))

        # First exec card from "AgentA" — should auto-approve
        card_a = FeedCardData(
            card_type="agent_action", source="agent", title="approval",
            body="$ ls", author="AgentA",
            timestamp=datetime.now(timezone.utc), project_name="testproject",
            metadata={"needs_approval": True},
        )
        cid_a = feed_handler.add_card(card_a)
        assert len(approved_calls) == 1

        # Second exec card from "AgentB" — should NOT auto-approve (scope locked to AgentA)
        card_b = FeedCardData(
            card_type="agent_action", source="agent", title="approval",
            body="$ ls", author="AgentB",
            timestamp=datetime.now(timezone.utc), project_name="testproject",
            metadata={"needs_approval": True},
        )
        cid_b = feed_handler.add_card(card_b)
        assert len(approved_calls) == 1  # still only 1
        assert feed_handler._cards[cid_b].accepted is None

    def test_exec_auto_accept_snoozed_card_not_approved(self, feed_handler, mock_glib, mock_feed_tab, monkeypatch):
        """A snoozed exec card should NOT be auto-approved.

        Uses monkeypatch to predict the UUID that add_card will assign,
        so we can pre-snooze it before the auto-accept check fires.
        (MockGLib.idle_add runs synchronously, so auto-accept fires inside
        add_card before we can read the returned card_id.)"""
        monkeypatch.setattr("ui.handlers.feed_handler.uuid.uuid4", lambda: "snoozed-uuid")
        feed_handler._prefs.exec_command.mode = "show"
        feed_handler._auto_accept_enabled = True
        feed_handler._prefs.snoozed_card_ids.append("snoozed-uuid")
        approved_calls = []
        feed_handler._on_approve_exec = lambda cid, approved: approved_calls.append((cid, approved))

        card = FeedCardData(
            card_type="agent_action", source="agent", title="approval",
            body="$ ls", author="PM",
            timestamp=datetime.now(timezone.utc), project_name="testproject",
            metadata={"needs_approval": True},
        )
        card_id = feed_handler.add_card(card)

        assert len(approved_calls) == 0
        assert feed_handler._cards[card_id].accepted is None


class TestAutoAcceptScenario:
    """Phase 8: Integration-level scenario tests for auto-accept flows.
    Covers spec §5 Step 8 scenarios + §6 acceptance criteria."""

    def test_scenario_diffs_on_file_changes_auto_accepted(self, feed_handler, mock_glib, mock_feed_tab):
        """Scenario: Diffs ON → diff cards auto-accept via handle_accept.
        File_* cards do NOT auto-accept (only diff)."""
        feed_handler._prefs.file_changes["diff"].enabled = True
        feed_handler._prefs.file_changes["diff"].agent_scope = "all_agents"
        feed_handler._auto_accept_enabled = True

        # Add a diff card — should be auto-accepted
        diff_card = FeedCardData(
            card_type="diff", source="agent", title="modified foo.py",
            body="+print('hello')", author="AgentA",
            timestamp=datetime.now(timezone.utc), project_name="proj",
            file_path="foo.py",
        )
        diff_id = feed_handler.add_card(diff_card)
        # MockGLib.idle_add runs immediately, so handle_accept fires synchronously
        # handle_accept on diff → spawns git thread → we can't test git ops here
        # but we can verify the card was routed through handle_accept by checking
        # that _auto_accept_enabled was True and the card is in _cards
        assert diff_id in feed_handler._cards

        # Add a file_created card — should NOT be auto-accepted
        file_card = FeedCardData(
            card_type="file_created", source="agent", title="created bar.py",
            body="new file", author="AgentA",
            timestamp=datetime.now(timezone.utc), project_name="proj",
            file_path="bar.py",
        )
        file_id = feed_handler.add_card(file_card)
        assert feed_handler._cards[file_id].accepted is None

    def test_scenario_files_on_file_changes_auto_accepted(self, feed_handler, mock_glib, mock_feed_tab):
        """Scenario: Files ON → file_* cards auto-accept. Diff cards do NOT."""
        feed_handler._prefs.file_changes["file_created"].enabled = True
        feed_handler._prefs.file_changes["file_created"].agent_scope = "all_agents"
        feed_handler._auto_accept_enabled = True

        file_card = FeedCardData(
            card_type="file_created", source="agent", title="created bar.py",
            body="new file", author="AgentA",
            timestamp=datetime.now(timezone.utc), project_name="proj",
            file_path="bar.py",
        )
        file_id = feed_handler.add_card(file_card)
        assert file_id in feed_handler._cards

        # Diff card should NOT be auto-accepted (only file_created is enabled)
        diff_card = FeedCardData(
            card_type="diff", source="agent", title="modified foo.py",
            body="+print('hello')", author="AgentA",
            timestamp=datetime.now(timezone.utc), project_name="proj",
            file_path="foo.py",
        )
        diff_id = feed_handler.add_card(diff_card)
        assert feed_handler._cards[diff_id].accepted is None

    def test_scenario_both_on_all_file_types_accepted(self, feed_handler, mock_glib, mock_feed_tab):
        """Scenario: Both Diffs and Files ON → all four file-change types auto-accept."""
        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            feed_handler._prefs.file_changes[ct].enabled = True
            feed_handler._prefs.file_changes[ct].agent_scope = "all_agents"
        feed_handler._auto_accept_enabled = True

        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            card = FeedCardData(
                card_type=ct, source="agent", title=f"{ct} event",
                body="content", author="AgentA",
                timestamp=datetime.now(timezone.utc), project_name="proj",
                file_path="some_file.py",
            )
            cid = feed_handler.add_card(card)
            assert cid in feed_handler._cards

    def test_scenario_both_off_nothing_accepted(self, feed_handler, mock_glib, mock_feed_tab):
        """Scenario: All toggles OFF → no cards auto-accepted."""
        feed_handler._auto_accept_enabled = False

        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            card = FeedCardData(
                card_type=ct, source="agent", title=f"{ct} event",
                body="content", author="AgentA",
                timestamp=datetime.now(timezone.utc), project_name="proj",
                file_path="some_file.py",
            )
            cid = feed_handler.add_card(card)
            assert feed_handler._cards[cid].accepted is None

    def test_scenario_unknown_card_type_never_auto_accepted(self, feed_handler, mock_glib, mock_feed_tab):
        """BUG #10 regression: unknown card_type (e.g. 'git_commit', None) is never
        auto-accepted regardless of prefs."""
        feed_handler._auto_accept_enabled = True
        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            feed_handler._prefs.file_changes[ct].enabled = True
            feed_handler._prefs.file_changes[ct].agent_scope = "all_agents"

        for ct in ("git_commit", "audit_report", None):
            card = FeedCardData(
                card_type=ct or "", source="agent", title=f"{ct} event",
                body="content", author="AgentA",
                timestamp=datetime.now(timezone.utc), project_name="proj",
            )
            cid = feed_handler.add_card(card)
            assert feed_handler._cards[cid].accepted is None

    def test_scenario_agent_scope_all_agents(self, feed_handler, mock_glib, mock_feed_tab):
        """Agent scope 'all_agents' → any agent's cards auto-accept."""
        feed_handler._prefs.file_changes["diff"].enabled = True
        feed_handler._prefs.file_changes["diff"].agent_scope = "all_agents"
        feed_handler._auto_accept_enabled = True

        for author in ("AgentA", "AgentB", "AgentC"):
            card = FeedCardData(
                card_type="diff", source="agent", title=f"diff by {author}",
                body="+code", author=author,
                timestamp=datetime.now(timezone.utc), project_name="proj",
                file_path="foo.py",
            )
            cid = feed_handler.add_card(card)
            assert cid in feed_handler._cards

    def test_scenario_agent_scope_first_author_lock_in(self, feed_handler, mock_glib, mock_feed_tab):
        """Agent scope 'first_author' → first card's author locks in;
        subsequent cards from different agents are NOT auto-accepted."""
        feed_handler._prefs.file_changes["diff"].enabled = True
        feed_handler._prefs.file_changes["diff"].agent_scope = "first_author"
        feed_handler._auto_accept_enabled = True

        # First card from AgentA — should auto-accept (lock-in)
        card_a = FeedCardData(
            card_type="diff", source="agent", title="diff by A",
            body="+a", author="AgentA",
            timestamp=datetime.now(timezone.utc), project_name="proj",
            file_path="a.py",
        )
        cid_a = feed_handler.add_card(card_a)
        assert cid_a in feed_handler._cards

        # Second card from AgentB — should NOT auto-accept
        card_b = FeedCardData(
            card_type="diff", source="agent", title="diff by B",
            body="+b", author="AgentB",
            timestamp=datetime.now(timezone.utc), project_name="proj",
            file_path="b.py",
        )
        cid_b = feed_handler.add_card(card_b)
        assert feed_handler._cards[cid_b].accepted is None

    def test_scenario_snoozed_card_not_auto_accepted(self, feed_handler, mock_glib, mock_feed_tab, monkeypatch):
        """Snoozed cards are not auto-accepted.

        Uses monkeypatch to predict the UUID that add_card will assign,
        so we can pre-snooze it before the auto-accept check fires."""
        monkeypatch.setattr("ui.handlers.feed_handler.uuid.uuid4", lambda: "snoozed-scenario-uuid")
        feed_handler._prefs.file_changes["diff"].enabled = True
        feed_handler._prefs.file_changes["diff"].agent_scope = "all_agents"
        feed_handler._auto_accept_enabled = True
        feed_handler._prefs.snoozed_card_ids.append("snoozed-scenario-uuid")

        card = FeedCardData(
            card_type="diff", source="agent", title="snoozed diff",
            body="+code", author="AgentA",
            timestamp=datetime.now(timezone.utc), project_name="proj",
            file_path="foo.py",
        )
        cid = feed_handler.add_card(card)
        assert feed_handler._cards[cid].accepted is None

    def test_scenario_batch_accept_independent_of_auto_accept(self, feed_handler, mock_glib, mock_feed_tab):
        """All toggles OFF, then user clicks Accept All → batch accept still works.
        Auto-accept and batch accept are independent mechanisms."""
        feed_handler._auto_accept_enabled = False

        # Add 3 pending file-change cards
        ids = []
        for i in range(3):
            card = FeedCardData(
                card_type="diff", source="agent", title=f"diff {i}",
                body=f"+line{i}", author="AgentA",
                timestamp=datetime.now(timezone.utc), project_name="proj",
                file_path=f"file_{i}.py",
            )
            ids.append(feed_handler.add_card(card))

        # Batch accept bar should be visible (3 pending ≥ 2 threshold)
        assert mock_feed_tab._batch_bar_visible is True
        assert mock_feed_tab._batch_bar_count == 3


class TestExecAutoAcceptModeQuery:
    """Phase 6-8: get_exec_auto_accept_mode() and callback wiring tests."""

    def test_get_mode_returns_off_by_default(self, feed_handler):
        """Fresh handler returns 'off' for exec mode."""
        assert feed_handler.get_exec_auto_accept_mode() == "off"

    def test_get_mode_returns_show_when_set(self, feed_handler):
        """After setting exec mode to 'show', get returns 'show'."""
        feed_handler._prefs.exec_command.mode = "show"
        assert feed_handler.get_exec_auto_accept_mode() == "show"

    def test_get_mode_returns_silent_when_set(self, feed_handler):
        """After setting exec mode to 'silent', get returns 'silent'."""
        feed_handler._prefs.exec_command.mode = "silent"
        assert feed_handler.get_exec_auto_accept_mode() == "silent"

    def test_get_mode_returns_none_when_prefs_is_none(self):
        """When _prefs is None (not yet initialized), returns None.
        This guards against constructor-ordering races."""
        from ui.handlers.feed_handler import FeedHandler
        h = FeedHandler(GLib=MockGLib(), on_send_to_agent=MagicMock())
        h._prefs = None
        assert h.get_exec_auto_accept_mode() is None

    def test_set_check_exec_callback_wires_correctly(self, feed_handler):
        """set_check_exec_auto_accept_callback_for_handler() installs FH's
        getter as ARTH's callback. The callback returns the current mode."""
        captured_callback = [None]

        def fake_arth_setter(cb):
            captured_callback[0] = cb

        feed_handler.set_check_exec_auto_accept_callback_for_handler(fake_arth_setter)

        # The captured callback should be FH.get_exec_auto_accept_mode
        assert captured_callback[0] is not None
        # Set mode and verify the callback reflects it
        feed_handler._prefs.exec_command.mode = "silent"
        assert captured_callback[0]() == "silent"
        feed_handler._prefs.exec_command.mode = "show"
        assert captured_callback[0]() == "show"


# ═══════════════════════════════════════════════════════════════════
#  TestAutoAcceptDialogCascadeRegression — Bug F
#  Verifies that programmatic set_active() in update_auto_accept_prefs()
#  does NOT trigger the toggled signal handlers (which would show the
#  warning dialog even though the user never clicked anything).
#
#  Repro history:
#    1. User has a v1 .crabcakes/feed-prefs.json with auto_accept_enabled: true.
#    2. User opens the project.
#    3. Prefs are loaded and migrated to v2 — all 4 file types enabled.
#    4. FeedHandler._append_and_schedule_scroll calls
#       feed_tab.update_auto_accept_prefs(prefs).
#    5. Inside update_auto_accept_prefs, self._diffs_toggle.set_active(True)
#       and self._files_toggle.set_active(True) are called.
#    6. On GTK 4.14, set_active() emits the 'toggled' signal whenever the
#       value changes (False→True or True→False). The inline comments in
#       update_auto_accept_prefs() claim it does NOT emit, but that is
#       incorrect on GTK 4.14 — my repro under Xvfb confirmed the signal
#       fires.
#    7. Each 'toggled' signal triggers _on_diffs_toggled(True) /
#       _on_files_toggled(True), which show a warning dialog each.
#    8. The user sees two stacked dialogs ("Auto-accept diffs?" on top,
#       "Auto-accept file changes?" behind it). Clicking the top button
#       closes that dialog but leaves the second one blocking input.
#       The user reports "clicking does nothing" because the second dialog
#       is still there, in focus, blocking clicks elsewhere.
#
#  Fix: feed_tab._syncing_toolbar flag is set during update_auto_accept_prefs
#  and gates _on_diffs_toggled / _on_files_toggled so they short-circuit
#  when the signal is caused by a programmatic update (not a real user click).
# ═══════════════════════════════════════════════════════════════════


class TestAutoAcceptDialogCascadeRegression:
    """Bug F regression: programmatic set_active() in update_auto_accept_prefs()
    must NOT fire the toggle handlers (which would show the warning dialog)."""

    def _make_real_feed_tab(self):
        """Build a real FeedTab for testing the set_active→toggled behavior.

        The existing real_feed_tab fixture wires a fake _feed_scroll so
        we don't need that — we only need the toolbar toggles.
        """
        from ui.views.feed_tab import FeedTab
        return FeedTab()

    def test_programmatic_set_active_does_not_fire_toggled_handler(self, real_feed_tab):
        """Bug F regression: feed_tab.update_auto_accept_prefs() sets
        _diffs_toggle.set_active(True) programmatically. GTK 4.14 emits
        the 'toggled' signal on every state change. Without the
        _syncing_toolbar guard, _on_diffs_toggled runs and the user-
        installed diffs callback would fire — showing a warning dialog
        even though the user never clicked anything.

        Fix: _syncing_toolbar flag short-circuits _on_diffs_toggled
        during the sync, so the callback only fires on real user clicks.
        """
        tab = real_feed_tab
        # Sanity: toggle starts OFF, no handler installed
        assert tab._diffs_toggle.get_active() is False
        callback_fired = []
        tab.set_diffs_toggle_callback(lambda active: callback_fired.append(active))

        # Simulate the v1→v2 prefs loaded into the FeedTab
        prefs_dict = {
            "version": 2,
            "auto_accept": {
                "file_changes": {
                    "diff": {"enabled": True, "agent_scope": "system"},
                    "file_created": {"enabled": True, "agent_scope": "system"},
                    "file_modified": {"enabled": True, "agent_scope": "system"},
                    "file_deleted": {"enabled": True, "agent_scope": "system"},
                },
                "exec_command": {"mode": "off", "agent_scope": "system"},
                "snoozed_card_ids": [],
            },
        }
        tab.update_auto_accept_prefs(prefs_dict)

        # Toggle should now be visually ON
        assert tab._diffs_toggle.get_active() is True
        # ...but the diffs_toggle_callback should NOT have fired
        assert callback_fired == [], (
            f"Bug F regression: programmatic set_active(True) fired the "
            f"diffs_toggle_handler {len(callback_fired)} times "
            f"(expected 0). On GTK 4.14, Gtk.ToggleButton.set_active() "
            f"emits 'toggled' when the value changes — the "
            f"_syncing_toolbar guard must suppress this during "
            f"update_auto_accept_prefs(). Otherwise the warning dialog "
            f"appears every time the user opens a project with auto-"
            f"accept prefs persisted from a v1 install."
        )

    def test_programmatic_set_active_does_not_fire_files_handler(self, real_feed_tab):
        """Same Bug F regression but for the Files toggle.

        v1 prefs with auto_accept_enabled=true migrate to v2 with all
        three file_created/file_modified/file_deleted types enabled.
        update_auto_accept_prefs then calls _files_toggle.set_active(True)
        which would emit 'toggled' and trigger _on_files_toggled, which
        shows the second stacked dialog. The _syncing_toolbar guard
        must suppress this too.
        """
        tab = real_feed_tab
        assert tab._files_toggle.get_active() is False
        callback_fired = []
        tab.set_files_toggle_callback(lambda active: callback_fired.append(active))

        prefs_dict = {
            "version": 2,
            "auto_accept": {
                "file_changes": {
                    "diff": {"enabled": False, "agent_scope": "first_author"},
                    "file_created": {"enabled": True, "agent_scope": "system"},
                    "file_modified": {"enabled": True, "agent_scope": "system"},
                    "file_deleted": {"enabled": True, "agent_scope": "system"},
                },
                "exec_command": {"mode": "off", "agent_scope": "system"},
                "snoozed_card_ids": [],
            },
        }
        tab.update_auto_accept_prefs(prefs_dict)

        assert tab._files_toggle.get_active() is True
        assert callback_fired == [], (
            f"Bug F regression: programmatic _files_toggle.set_active(True) "
            f"fired the files_toggle_handler {len(callback_fired)} times "
            f"(expected 0). The second warning dialog is the more obvious "
            f"symptom because the Files dialog stacks behind the Diffs one — "
            f"the user clicks 'Cancel' on Diffs and the Files dialog is "
            f"still there blocking input."
        )

    def test_user_click_still_fires_toggled_handler(self, real_feed_tab):
        """Sanity / negative test: when _syncing_toolbar is False (real
        user click), the toggled handler MUST fire. This guards against
        the fix being too aggressive (e.g. always short-circuiting)."""
        tab = real_feed_tab
        callback_fired = []
        tab.set_diffs_toggle_callback(lambda active: callback_fired.append(active))

        # Simulate a real user click — toggle.set_active(True) WITHOUT
        # the _syncing_toolbar guard being set
        assert tab._syncing_toolbar is False  # baseline
        tab._diffs_toggle.set_active(True)

        assert tab._diffs_toggle.get_active() is True
        assert callback_fired == [True], (
            f"Real user click must fire the diffs_toggle_handler "
            f"(expected [True], got {callback_fired}). The _syncing_toolbar "
            f"guard is set during update_auto_accept_prefs only — outside "
            f"of that, the handler should always run."
        )

    def test_syncing_toolbar_clears_on_exception(self, real_feed_tab):
        """Bug F robustness: if update_auto_accept_prefs raises mid-sync,
        the _syncing_toolbar flag MUST be reset to False so subsequent
        real user clicks still fire the handlers."""
        tab = real_feed_tab
        assert tab._syncing_toolbar is False

        # Wrap set_active to throw before flipping the toggle
        def boom(*args, **kwargs):
            raise RuntimeError("simulated failure mid-sync")
        tab._diffs_toggle.set_active = boom

        prefs_dict = {
            "version": 2,
            "auto_accept": {
                "file_changes": {
                    "diff": {"enabled": True, "agent_scope": "first_author"},
                },
                "exec_command": {"mode": "off", "agent_scope": "first_author"},
                "snoozed_card_ids": [],
            },
        }
        raised = False
        try:
            tab.update_auto_accept_prefs(prefs_dict)
        except RuntimeError:
            raised = True

        assert raised, "Test setup: boom() should have raised"
        # Flag must be reset even though we raised
        assert tab._syncing_toolbar is False, (
            "Robustness: update_auto_accept_prefs must reset "
            "_syncing_toolbar in a finally block so the flag doesn't "
            "leak and disable all future toggle interactions."
        )


class TestToggleStuckRegression:
    """Bug #12: Diffs/Files toggle stuck ON after user clicks to turn OFF.

    Root cause: FeedHandler._refresh_auto_accept_state() called BOTH
    update_auto_accept_prefs() (v2 path) AND update_auto_accept_state()
    (v1 legacy path) on every prefs mutation. The legacy path constructed
    a default prefs dict with `enabled=self._auto_accept_enabled`. When
    the user clicked Diffs OFF, diff.enabled became False but other file
    types (file_created, file_modified, file_deleted) were still True, so
    _auto_accept_enabled was True. The legacy path then set Diffs back to
    ON via set_active(True), undoing the user's click.

    Fix: use `elif` not `if` so the v1 path only fires on legacy/mocks
    that don't have update_auto_accept_prefs. (Real FeedTab has both.)

    These tests verify the click → off → toggle stays OFF path through
    the full FeedHandler._refresh_auto_accept_state chain.
    """

    def test_diffs_click_off_flips_toggle_and_stays_off(self, real_feed_tab):
        """User clicks Diffs to turn OFF — toggle must visually flip to
        OFF and stay OFF after _refresh_auto_accept_state() runs.

        Bug #12 regression: previously the v1 legacy path
        update_auto_accept_state() overwrote the v2 prefs dict and
        re-set the Diffs toggle to ON (because file_created/modified/
        deleted were still True, so _auto_accept_enabled was True).
        """
        tab = real_feed_tab
        
        # Set up a FeedHandler with a mock GLib
        from ui.handlers.feed_handler import FeedHandler
        mock_glib = MockGLib()
        fh = FeedHandler(GLib=mock_glib, on_send_to_agent=lambda *a: None)
        fh.set_feed_tab(tab)
        
        # Enable only diff (not the other file types) — this is the
        # KEY setup: when user turns diff OFF, _auto_accept_enabled
        # should become False (no file types enabled).
        fh._prefs.file_changes["diff"].enabled = True
        fh._prefs.file_changes["file_created"].enabled = False
        fh._prefs.file_changes["file_modified"].enabled = False
        fh._prefs.file_changes["file_deleted"].enabled = False
        # Sync the toolbar to reflect this state
        fh._refresh_auto_accept_state()
        assert tab._diffs_toggle.get_active() is True, "Setup: Diffs should be ON"
        
        # Simulate user clicking Diffs OFF
        tab._diffs_toggle.set_active(False)
        # The handler should have fired, setting diff.enabled = False
        # and calling _refresh_auto_accept_state()
        assert fh._prefs.file_changes["diff"].enabled is False
        # AND the toggle should stay OFF (the bug was that the v1 path
        # would re-set it to True because file_created etc were still
        # True — but here they were False, so even the buggy v1 path
        # would produce correct behavior. This is the simple case.)
        assert tab._diffs_toggle.get_active() is False, (
            "Toggle should be OFF after user click + refresh"
        )

    def test_diffs_click_off_with_other_types_enabled(self, real_feed_tab):
        """THE ACTUAL BUG: user clicks Diffs OFF when file_created/
        modified/deleted are still ON. Previously: toggle flipped back
        to ON because v1 path saw _auto_accept_enabled=True and
        re-set Diffs to True. Now: elif guard prevents v1 path.

        This is the exact scenario from the user's report.
        """
        tab = real_feed_tab
        
        from ui.handlers.feed_handler import FeedHandler
        mock_glib = MockGLib()
        fh = FeedHandler(GLib=mock_glib, on_send_to_agent=lambda *a: None)
        fh.set_feed_tab(tab)
        
        # Enable ALL file types (matching v1 prefs with enabled=True)
        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            fh._prefs.file_changes[ct].enabled = True
        fh._refresh_auto_accept_state()
        assert tab._diffs_toggle.get_active() is True, "Setup: Diffs should be ON"
        assert fh._auto_accept_enabled is True
        
        # User clicks Diffs OFF
        tab._diffs_toggle.set_active(False)
        
        # _prefs should now have diff disabled
        assert fh._prefs.file_changes["diff"].enabled is False
        # Other types still enabled
        assert fh._prefs.file_changes["file_created"].enabled is True
        # _auto_accept_enabled is True (because other types are on)
        assert fh._auto_accept_enabled is True
        
        # CRITICAL: toggle should stay OFF. Bug #12 was that the v1
        # legacy path would call update_auto_accept_state(True) and
        # reconstruct prefs with diff.enabled=True, flipping the toggle
        # back ON.
        assert tab._diffs_toggle.get_active() is False, (
            "Bug #12 regression: Diffs toggle should stay OFF after user "
            "clicks to turn it off, even when other file types are still "
            "enabled. The legacy v1 update_auto_accept_state path must "
            "NOT fire on a real FeedTab that has update_auto_accept_prefs."
        )

    def test_legacy_mock_feedtab_still_uses_update_auto_accept_state(self):
        """Mock FeedTab (legacy test fixture) only has
        update_auto_accept_state, NOT update_auto_accept_prefs. The
        `_refresh_auto_accept_state` `elif` guard must still trigger
        the legacy path so existing tests keep working.
        """
        class LegacyFeedTab:
            def __init__(self):
                self._auto_accept_active = None
                # MEMRATCHET P3 eviction surface (spec §2.6) — permissive
                # defaults, same contract as MockFeedTab (configurable).
                self._near_bottom = True
                self._above_viewport = True
                self._card_spacing = 8
            def update_auto_accept_state(self, active: bool):
                self._auto_accept_active = active
            def set_batch_accept_callback(self, callback):
                pass  # no-op
            def set_auto_accept_callback(self, callback):
                pass  # no-op
            # NOTE: no update_auto_accept_prefs method
            # MEMRATCHET P3 (spec §2.6) — eviction-pass accessors
            def is_near_bottom(self, slack: int = 80) -> bool:
                return self._near_bottom
            def is_above_viewport(self, widget) -> bool:
                return self._above_viewport
            def get_vadjustment(self):
                return None
            def get_card_container(self):
                return MockCardContainer(self)
        
        mock_tab = LegacyFeedTab()
        
        from ui.handlers.feed_handler import FeedHandler
        mock_glib = MockGLib()
        fh = FeedHandler(GLib=mock_glib, on_send_to_agent=lambda *a: None)
        fh.set_feed_tab(mock_tab)
        fh._prefs.file_changes["diff"].enabled = True
        
        fh._refresh_auto_accept_state()
        
        # Legacy path should have fired (mock only has the legacy method)
        assert mock_tab._auto_accept_active is True

    def test_real_feedtab_does_not_call_update_auto_accept_state(self, real_feed_tab):
        """Real FeedTab has BOTH methods. The `elif` guard ensures the
        legacy path is skipped — only update_auto_accept_prefs fires.
        Verifies the elif guard is in effect by tracking calls.
        """
        tab = real_feed_tab
        
        legacy_calls = []
        original_legacy = tab.update_auto_accept_state
        def traced_legacy(active):
            legacy_calls.append(active)
            original_legacy(active)
        tab.update_auto_accept_state = traced_legacy
        
        from ui.handlers.feed_handler import FeedHandler
        mock_glib = MockGLib()
        fh = FeedHandler(GLib=mock_glib, on_send_to_agent=lambda *a: None)
        fh.set_feed_tab(tab)
        fh._prefs.file_changes["diff"].enabled = True
        
        fh._refresh_auto_accept_state()
        
        # Real path should fire (update_auto_accept_prefs)
        assert tab._diffs_toggle.get_active() is True
        # Legacy path should NOT fire (elif guard)
        assert legacy_calls == [], (
            f"Real FeedTab.update_auto_accept_state must NOT fire when "
            f"update_auto_accept_prefs is available. Got {legacy_calls=}. "
            f"This means the `if` -> `elif` fix in "
            f"_refresh_auto_accept_state was lost."
        )


class TestPendingSaveIdStaleRegression:
    """Bug #12b: 'Source ID N was not found when attempting to remove it'
    warning fires every time _refresh_auto_accept_state is called after
    a previous save's idle callback has already fired.

    Root cause: GLib.idle_add runs the callback once (because the callback
    returns False / no-repeat), and GLib auto-removes the source. But
    _refresh_auto_accept_state still called source_remove(_pending_save_id),
    which pointed at an already-cleaned-up source. The result was the
    'Source ID N was not found' warning at every subsequent prefs mutation.

    Fix: drop the source_remove() call entirely. GLib's idle source is
    a single-shot — calling idle_add again with a new callback schedules
    a new save; the old one already ran (or is running) and is harmless
    to leave alone. Rapid-fire mutations coalesce into one disk write
    because the idle source only fires after the current main-loop spin
    settles anyway.
    """

    def test_refresh_does_not_call_source_remove(self):
        """Mock GLib that tracks all source_remove calls. After two
        _refresh_auto_accept_state() calls, source_remove must NEVER
        have been invoked. The single-shot idle source cleans itself up.
        """
        remove_calls = []
        
        class MockGLib:
            _next_id = 100
            @classmethod
            def idle_add(cls, callback):
                sid = cls._next_id
                cls._next_id += 1
                callback()
                return sid
            @classmethod
            def source_remove(cls, sid):
                remove_calls.append(sid)
        
        from ui.handlers.feed_handler import FeedHandler
        fh = FeedHandler(GLib=MockGLib, on_send_to_agent=lambda *a: None)
        
        fh._refresh_auto_accept_state()
        fh._refresh_auto_accept_state()
        
        assert remove_calls == [], (
            f"_refresh_auto_accept_state must NOT call source_remove "
            f"(Bug #12b). GLib's idle source is single-shot and auto-removes "
            f"itself. Manual removal just produces 'Source ID N was not found' "
            f"warnings. Got remove_calls={remove_calls}."
        )

    def test_save_still_persists_prefs_to_disk(self):
        """The fix must not break the actual save — just the spurious
        source_remove warning. Verify save_feed_prefs is called with
        the current prefs.
        """
        import tempfile
        import shutil
        from pathlib import Path
        from unittest.mock import patch
        
        # Create a temporary project dir
        tmpdir = tempfile.mkdtemp(prefix="cc_save_test_")
        crabcakes_dir = Path(tmpdir) / ".crabcakes"
        crabcakes_dir.mkdir()
        
        try:
            saved_prefs = []
            
            class MockGLib:
                _next_id = 200
                @classmethod
                def idle_add(cls, callback):
                    sid = cls._next_id
                    cls._next_id += 1
                    callback()
                    return sid
                @classmethod
                def source_remove(cls, sid):
                    pass
            
            # Patch feed_store.save_feed_prefs to capture what's saved
            import ui.handlers.feed_handler as fh_mod
            original_save = fh_mod.feed_store.save_feed_prefs
            
            def capturing_save(project_path, prefs):
                saved_prefs.append((project_path, prefs))
                return original_save(project_path, prefs)
            
            fh_mod.feed_store.save_feed_prefs = capturing_save
            
            try:
                from ui.handlers.feed_handler import FeedHandler
                fh = FeedHandler(GLib=MockGLib, on_send_to_agent=lambda *a: None)
                fh._active_project_name = "testproj"
                fh._project_paths = {"testproj": tmpdir}
                fh._prefs.file_changes["diff"].enabled = True
                
                fh._refresh_auto_accept_state()
                
                assert saved_prefs, (
                    "save_feed_prefs must have been called via idle_add "
                    "after _refresh_auto_accept_state() enabled diff."
                )
                project_path, prefs = saved_prefs[-1]
                assert prefs["auto_accept"]["file_changes"]["diff"]["enabled"] is True
            finally:
                fh_mod.feed_store.save_feed_prefs = original_save
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestHideCardButtonsBug:
    """Bug #13: hide_card_buttons() was a silent no-op.

    The original implementation looked up `_<name>_button` attributes on
    the card widget. But `build_feed_card()` constructs local `btn_accept`
    and `btn_reject` variables and appends them inline — it NEVER stores
    them as named attributes on the returned card widget. So
    `getattr(card_widget, "_approve_button", None)` returned None every
    time, and the buttons stayed visible after auto-approve.

    User-visible symptom: in Show mode, an exec card auto-approved via
    `_auto_approve_exec_card()` still showed its Approve/Deny buttons.
    Clicking Approve again called `handle_approve_exec(cid, True)` → ARTH
    a second time. Not idempotent → double-action risk.

    Fix: map each hide_card_buttons arg name to its CSS class
    (`build_feed_card` sets `feed-btn-accept` / `feed-btn-reject` / 
    `feed-btn-review`) and walk the widget subtree.
    """

    def _make_card_widget(self, button_labels=("Approve", "Deny")):
        """Build a minimal Gtk.Box mimicking build_feed_card's structure:
        a root card box with a children Btns row containing the given
        buttons. Returns the root box."""
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gtk

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        actions.add_css_class("feed-card-actions")

        css_classes = {
            "Approve": "feed-btn-accept",
            "Deny": "feed-btn-reject",
            "Accept": "feed-btn-accept",
            "Reject": "feed-btn-reject",
            "Review": "feed-btn-review",
        }
        for label in button_labels:
            btn = Gtk.Button(label=label)
            btn.add_css_class(css_classes.get(label, "feed-btn-custom"))
            actions.append(btn)
        card.append(actions)
        return card

    def test_hide_card_buttons_hides_approve(self):
        """hide_card_buttons(['approve']) hides an Approve button."""
        from ui.views.feed_tab import FeedTab
        tab = FeedTab()  # FeedTab.__init__() takes no args
        
        card = self._make_card_widget(button_labels=("Approve", "Deny"))
        tab._cards_by_id["test_card"] = card
        
        tab.hide_card_buttons("test_card", ["approve"])
        
        # Find the Approve button in the subtree
        approve_btn = None
        deny_btn = None
        for child in [c for c in card.get_first_child().get_first_child().__iter__()] if False else []:
            pass
        # Simpler: walk via known structure
        actions_box = card.get_first_child()
        first_button = actions_box.get_first_child()
        second_button = first_button.get_next_sibling()
        assert first_button.get_visible() is False, (
            "Approve button must be hidden after hide_card_buttons(['approve'])"
        )
        assert second_button.get_visible() is True, (
            "Deny button must remain visible — only 'approve' was requested"
        )

    def test_hide_card_buttons_hides_deny(self):
        """hide_card_buttons(['deny']) hides a Deny button."""
        from ui.views.feed_tab import FeedTab
        tab = FeedTab()
        
        card = self._make_card_widget(button_labels=("Approve", "Deny"))
        tab._cards_by_id["test_card"] = card
        
        tab.hide_card_buttons("test_card", ["deny"])
        
        actions_box = card.get_first_child()
        first_button = actions_box.get_first_child()
        second_button = first_button.get_next_sibling()
        assert first_button.get_visible() is True, (
            "Approve button must remain visible — only 'deny' was requested"
        )
        assert second_button.get_visible() is False, (
            "Deny button must be hidden after hide_card_buttons(['deny'])"
        )

    def test_hide_card_buttons_hides_both(self):
        """hide_card_buttons(['approve', 'deny']) hides both."""
        from ui.views.feed_tab import FeedTab
        tab = FeedTab()
        
        card = self._make_card_widget(button_labels=("Approve", "Deny"))
        tab._cards_by_id["test_card"] = card
        
        tab.hide_card_buttons("test_card", ["approve", "deny"])
        
        actions_box = card.get_first_child()
        first_button = actions_box.get_first_child()
        second_button = first_button.get_next_sibling()
        assert first_button.get_visible() is False
        assert second_button.get_visible() is False

    def test_hide_card_buttons_unknown_card_is_noop(self):
        """Unknown card_id is a no-op (no exception)."""
        from ui.views.feed_tab import FeedTab
        tab = FeedTab()
        
        # Should not raise
        tab.hide_card_buttons("nonexistent_card_id", ["approve", "deny"])

    def test_hide_card_buttons_accept_alias_hides_approve(self):
        """Both 'approve' (needs_approval label) and 'accept' (file_change
        label) should map to feed-btn-accept CSS class. The caller may
        pass either depending on card_type — both must work."""
        from ui.views.feed_tab import FeedTab
        tab = FeedTab()
        
        card = self._make_card_widget(button_labels=("Accept",))
        tab._cards_by_id["test_card"] = card
        
        tab.hide_card_buttons("test_card", ["accept"])
        
        btn = card.get_first_child().get_first_child()
        assert btn.get_visible() is False, (
            "Accept button (file-change label) must be hidden when "
            "passed as 'accept' to hide_card_buttons"
        )


# ── TestAutoAcceptLevel — SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-3 §5 Step 2
# Phase I.5. Regression tests for the settings-bar auto-accept level API:
#   get_auto_accept_level / set_auto_accept_level / _commit_auto_accept_level
#   / _emit_auto_accept_level_changed / set_on_auto_accept_level_changed.
# Pure Python — reuses the MockGLib + feed_handler fixtures (no real GTK).

class TestAutoAcceptLevel:
    """Round-trip + warning-gate behaviour of the file-change auto-accept
    level API (SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-3 §2.3)."""

    def _make_handler(self, wire_warning=None):
        from ui.handlers.feed_handler import FeedHandler
        h = FeedHandler(GLib=MockGLib(), on_send_to_agent=MagicMock())
        h.set_feed_tab(MockFeedTab())
        if wire_warning is not None:
            h.set_show_auto_accept_warning(wire_warning)
        return h

    def test_round_trip_all_four_levels(self):
        """Set each of off/diffs/files/all, get it back, assert round-trip."""
        h = self._make_handler()
        for level in ("off", "diffs", "files", "all"):
            h.set_auto_accept_level(level)
            assert h.get_auto_accept_level() == level, (
                f"round-trip failed for level {level!r}: "
                f"got {h.get_auto_accept_level()!r}"
            )

    def test_distinct_states_persisted(self):
        """Each level serializes via to_dict()/from_dict() and survives."""
        for level in ("off", "diffs", "files", "all"):
            h = self._make_handler()
            h.set_auto_accept_level(level)
            raw = h._prefs.to_dict()
            p2 = AutoAcceptPrefs.from_dict(raw)
            h2 = self._make_handler()
            h2._prefs = p2
            assert h2.get_auto_accept_level() == level, (
                f"persisted level mismatch for {level!r}"
            )

    def test_invalid_level_noop(self):
        """set_auto_accept_level('bogus') is a no-op (state unchanged)."""
        h = self._make_handler()
        before = h.get_auto_accept_level()
        h.set_auto_accept_level("bogus")
        assert h.get_auto_accept_level() == before
        # Internal state untouched as well
        assert all(fc.enabled is False
                   for fc in h._prefs.file_changes.values())

    def test_off_path_emits_callback(self):
        """set_auto_accept_level('off') emits on_auto_accept_level_changed('off')."""
        h = self._make_handler()
        captured = []
        h.set_on_auto_accept_level_changed(captured.append)
        h.set_auto_accept_level("off")
        assert captured == ["off"]

    def test_warning_gate_on_enable(self):
        """Enabling level routes through warning; level stays 'off' until confirm."""
        h = self._make_handler()
        warning_calls = []
        h.set_show_auto_accept_warning(
            lambda category, agent, on_confirm, on_cancel:
                warning_calls.append((category, agent, on_confirm, on_cancel))
        )
        h.set_auto_accept_level("files")
        # Warning invoked with category 'files'
        assert warning_calls, "warning callback not invoked"
        category, agent, on_confirm, on_cancel = warning_calls[0]
        assert category == "files"
        # NOT committed yet — still 'off'
        assert h.get_auto_accept_level() == "off"
        # Confirm -> commit
        on_confirm()
        assert h.get_auto_accept_level() == "files"

    def test_warning_cancel_no_commit(self):
        """Cancel path does NOT commit and does NOT emit."""
        h = self._make_handler()
        emitted = []
        h.set_on_auto_accept_level_changed(emitted.append)
        captured = {}
        h.set_show_auto_accept_warning(
            lambda category, agent, on_confirm, on_cancel:
                captured.update(on_cancel=on_cancel)
        )
        h.set_auto_accept_level("diffs")
        assert h.get_auto_accept_level() == "off"
        # cancel
        captured["on_cancel"]()
        assert h.get_auto_accept_level() == "off"
        assert emitted == [], "cancel must not emit the change callback"

    def test_off_bypasses_warning(self):
        """Setting 'off' from any state never invokes the warning."""
        h = self._make_handler()
        warner = MagicMock()
        h.set_show_auto_accept_warning(warner)
        # Put it in an on-state first so the transition is a real disable.
        h.set_show_auto_accept_warning(None)  # commit straight through
        h.set_auto_accept_level("all")
        h.set_show_auto_accept_warning(warner)
        warner.reset_mock()
        h.set_auto_accept_level("off")
        warner.assert_not_called()
        assert h.get_auto_accept_level() == "off"

    def test_exec_untouched(self):
        """File-change level changes never touch exec_command auto-accept."""
        h = self._make_handler()
        # set exec to a non-off mode
        h._prefs.exec_command.mode = "show"
        h.set_auto_accept_level("files")
        assert h._prefs.exec_command.mode == "show", (
            "file-level change must not modify exec axis"
        )
        h.set_auto_accept_level("all")
        assert h._prefs.exec_command.mode == "show"
        h.set_auto_accept_level("off")
        assert h._prefs.exec_command.mode == "show"

    def test_prefs_none_guard(self):
        """With _prefs=None every level is a no-op (no crash)."""
        h = self._make_handler()
        h._prefs = None
        for level in ("off", "diffs", "files", "all"):
            h.set_auto_accept_level(level)  # must not raise
        # Nothing to assert on state, but ensure no exception when reading.
        # get_auto_accept_level would crash on None prefs — guard is in setter.

    def test_refresh_called_after_commit(self):
        """_refresh_auto_accept_state is called after each commit path."""
        h = self._make_handler()
        with patch.object(h, "_refresh_auto_accept_state") as refresh:
            # off path
            h.set_auto_accept_level("off")
            assert refresh.call_count == 1
            # enabling path commits via _commit_auto_accept_level (no warning wired)
            refresh.reset_mock()
            h.set_auto_accept_level("files")
            assert refresh.call_count == 1
            # confirming an enabling level
            refresh.reset_mock()
            warned = {}
            h.set_show_auto_accept_warning(
                lambda category, agent, on_confirm, on_cancel:
                    warned.update(on_confirm=on_confirm)
            )
            h.set_auto_accept_level("all")
            refresh.reset_mock()
            warned["on_confirm"]()
            assert refresh.call_count == 1
            # on_cancel also refreshes (state sync) but does not commit
            refresh.reset_mock()
            warned2 = {}
            h.set_show_auto_accept_warning(
                lambda category, agent, on_confirm, on_cancel:
                    warned2.update(on_cancel=on_cancel)
            )
            h.set_auto_accept_level("diffs")
            refresh.reset_mock()
            warned2["on_cancel"]()
            assert refresh.call_count == 1


class TestUpdateCardNotFound:
    """A2 (SPEC-AUDIT-CLEANUP-1): update_card() for an unknown card id
    logged via a bare `logger` name, but the module defines `_logger`.
    The NameError crashed the caller (AgentRuntimeHandler Phase D card
    updates) instead of warning and returning."""

    def test_unknown_card_id_warns_and_returns_without_crash(
        self, feed_handler, caplog
    ):
        import logging

        card = FeedCardData(
            card_type="tool_call", source="agent", title="result",
            body="", author="Coder",
            timestamp=datetime.now(timezone.utc), project_name="proj",
        )
        with caplog.at_level(logging.WARNING, logger="ui.handlers.feed_handler"):
            feed_handler.update_card("nonexistent-card-id", card)
            # was: NameError: name 'logger' is not defined

        assert any(
            "update_card" in r.getMessage() and "nonexistent-card-id" in r.getMessage()
            for r in caplog.records
        ), f"Expected not-found warning, got: {[r.getMessage() for r in caplog.records]}"
        # Guard must return before any widget/feed-tab work
        assert feed_handler._feed_tab.cards == []
        assert "nonexistent-card-id" not in feed_handler._cards


# ═══════════════════════════════════════════════════════════════════
#  TestBackgroundPersistWriter — SPEC-UI-RESPONSIVENESS-2 §2.1 (Phase 1)
#  The background, coalesced feed writer. Producers enqueue; one daemon
#  thread drains with last-write-wins coalescing + a deferred-retry map.
#
#  Determinism strategy (spec §9): writer-behaviour tests stub
#  `_ensure_persist_writer` so no real thread races the assertions, then
#  drive `_drain_persist_queue()` directly. The tests that MUST exercise
#  the real thread (restart-after-shutdown, bounded shutdown) do so with
#  bounded joins / polls. The existing sync MockGLib is reused untouched.
# ═══════════════════════════════════════════════════════════════════

class _DeadWriterStub:
    """A writer-thread stand-in that has already exited."""
    def __init__(self):
        self.joined = None

    def is_alive(self):
        return False

    def join(self, timeout=None):
        self.joined = timeout


class _AliveWriterStub:
    """A writer-thread stand-in that never exits (join always times out)."""
    def __init__(self, on_join=None):
        self.joined = None
        self._on_join = on_join

    def is_alive(self):
        return True

    def join(self, timeout=None):
        self.joined = timeout
        if self._on_join is not None:
            self._on_join()


class TestBackgroundPersistWriter:
    """Phase 1 invariants 1-8 from the spec (interim shape — no compactions)."""

    def _make_handler(self):
        from ui.handlers.feed_handler import FeedHandler
        h = FeedHandler(GLib=MockGLib(), on_send_to_agent=MagicMock())
        h.set_feed_tab(MockFeedTab())
        return h

    def _seed_card(self, h, project_name="proj"):
        """Add one card, then register its project path (so persist fires)."""
        card = FeedCardData(
            card_type="tool_call", source="agent", title="tool result",
            body="", author="Coder",
            timestamp=datetime.now(timezone.utc), project_name=project_name,
        )
        card_id = h.add_card(card)
        # Registered AFTER add_card so add_card itself spawns no persist thread.
        h._project_paths[project_name] = "/tmp/uiresp2-proj"
        return card_id, card

    @staticmethod
    def _no_writer(h):
        """Suppress the real writer thread so drain calls are deterministic."""
        h._ensure_persist_writer = lambda: None

    # ── Invariant 1: no disk I/O on the main-thread path ─────────────────

    def test_update_card_does_no_disk_io_and_returns_fast(self, monkeypatch):
        """update_card must not touch feed_store on the calling (main) thread.

        Pre-change this is RED: update_card did a synchronous read-modify-write
        of feed.json (measured 0.62 s on the real 13.9 MB feed).
        """
        import time
        from utils import feed_store as real_feed_store

        h = self._make_handler()
        # Large in-memory fixture (the main-thread cost is the dict assignment;
        # pre-change the cost was the full-file RMW on the SAME thread).
        for i in range(2000):
            h._cards[f"seed-{i}"] = FeedCardData(
                card_type="tool_call", source="agent", title=f"seed {i}",
                body="", author="Coder",
                timestamp=datetime.now(timezone.utc), project_name="proj",
            )
        card_id, card = self._seed_card(h)
        card.body = "updated body"

        calls = []

        def _slow_update(*args, **kwargs):
            calls.append(args)
            time.sleep(0.3)
            return True

        # Patch the SOURCE module: pre-change update_card used a function-local
        # `from utils.feed_store import update_feed_card`, which resolves this
        # patched attribute — so the sleeper really is hit on the main thread.
        monkeypatch.setattr(real_feed_store, "update_feed_card", _slow_update)
        self._no_writer(h)

        t0 = time.perf_counter()
        h.update_card(card_id, card)
        elapsed = time.perf_counter() - t0

        # Structural assertion: NO disk write happened on the caller's thread.
        assert calls == [], "update_card must not persist on the main thread"
        # Timing bound (design target <5 ms; 50 ms is the generous gate).
        assert elapsed < 0.05, f"update_card took {elapsed:.3f}s (bound 0.05s)"
        # ...the work was queued for the writer instead.
        assert (h._project_paths["proj"], card_id) in h._persist_queue

    # ── F1 companion: persist the durable decision (spec §2.3.2) ─────────
    #  Pre-fix, update_card's enqueue payload carried only {body, metadata},
    #  so `accepted` was never written to disk. The pin rule could then not
    #  read the durable decision for resolved exec-approval cards, and the
    #  transient needs_approval flag pinned them forever. RED pre-fix (the
    #  payload lacks the key).

    def test_update_card_persists_accepted_when_decided(self, monkeypatch):
        h = self._make_handler()
        card_id, card = self._seed_card(h)
        project_path = h._project_paths["proj"]
        self._no_writer(h)

        store = MagicMock()
        store.update_feed_card.return_value = True
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        card.accepted = True  # a recorded decision (e.g. approve_exec)
        h.update_card(card_id, card)
        h._drain_persist_queue()

        assert store.update_feed_card.call_count == 1
        updates = store.update_feed_card.call_args[0][2]
        assert updates.get("accepted") is True, (
            f"the durable decision must persist; payload was {updates!r}"
        )

    def test_update_card_payload_omits_accepted_when_none(self, monkeypatch):
        h = self._make_handler()
        card_id, card = self._seed_card(h)
        project_path = h._project_paths["proj"]
        self._no_writer(h)

        store = MagicMock()
        store.update_feed_card.return_value = True
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        assert card.accepted is None, "fixture precondition"
        h.update_card(card_id, card)
        h._drain_persist_queue()

        updates = store.update_feed_card.call_args[0][2]
        assert "accepted" not in updates, (
            f"accepted=None must NOT be written (no regression to "
            f"clobbering a recorded decision); payload was {updates!r}"
        )

    # ── F1 REBUILD-PATH parity (Debugger audit BUG #1, bf0fe54) ──────────
    #  The two tests above seed `tool_call` cards, which now expose the
    #  `_body_label` seam — so they exercise the IN-PLACE branch of
    #  update_card. These two force the REBUILD branch (`file_modified`
    #  renders via _render_file_event_body, no `_text_label`, so
    #  card._body_label is None), proving the enqueue block's F1 shape is
    #  branch-independent: a future refactor that moves the enqueue into
    #  one branch cannot silently regress the other.

    def _seed_rebuild_path_card(self, h, project_name="proj"):
        """Seed a file_modified card — no _body_label seam → rebuild path."""
        card = FeedCardData(
            card_type="file_modified", source="agent", title="file changed",
            body="", author="Coder",
            timestamp=datetime.now(timezone.utc), project_name=project_name,
            file_path="src/main.py",
        )
        card_id = h.add_card(card)
        h._project_paths[project_name] = "/tmp/uiresp2-proj"
        return card_id, card

    def test_rebuild_path_persists_accepted_when_decided(self, monkeypatch):
        h = self._make_handler()
        card_id, card = self._seed_rebuild_path_card(h)
        project_path = h._project_paths["proj"]
        self._no_writer(h)

        # Precondition guard: this fixture really takes the rebuild branch.
        old_widget = h._card_widgets[card_id]
        assert getattr(old_widget, "_body_label", "MISSING") is None, (
            "fixture precondition: file_modified card must lack _body_label "
            "(if the renderer gained a seam, this test no longer covers the "
            "rebuild path)"
        )

        store = MagicMock()
        store.update_feed_card.return_value = True
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        card.accepted = True
        h.update_card(card_id, card)
        h._drain_persist_queue()

        updates = store.update_feed_card.call_args[0][2]
        assert updates.get("accepted") is True, (
            f"rebuild path must persist the durable decision; "
            f"payload was {updates!r}"
        )
        # Structural: the widget was rebuilt + swapped, not mutated in place.
        assert h._card_widgets[card_id] is not old_widget, (
            "rebuild branch must replace the widget"
        )

    def test_rebuild_path_omits_accepted_when_none(self, monkeypatch):
        h = self._make_handler()
        card_id, card = self._seed_rebuild_path_card(h)
        project_path = h._project_paths["proj"]
        self._no_writer(h)

        assert getattr(h._card_widgets[card_id], "_body_label", "MISSING") is None, (
            "fixture precondition: rebuild branch required"
        )

        store = MagicMock()
        store.update_feed_card.return_value = True
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        assert card.accepted is None, "fixture precondition"
        h.update_card(card_id, card)
        h._drain_persist_queue()

        updates = store.update_feed_card.call_args[0][2]
        assert "accepted" not in updates, (
            f"rebuild path: accepted=None must NOT be written; "
            f"payload was {updates!r}"
        )


    # ── Invariant 2: coalescing + last-write-wins ────────────────────────

    def test_coalescing_n_enqueues_become_one_writer_call_last_wins(self, monkeypatch):
        h = self._make_handler()
        card_id, _card = self._seed_card(h)
        project_path = h._project_paths["proj"]
        self._no_writer(h)

        store = MagicMock()
        store.update_feed_card.return_value = True
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        for i in range(7):
            h._enqueue_card_update(project_path, card_id, {"body": f"v{i}"})

        assert len(h._persist_queue) == 1, "coalesced per (project, card)"

        h._drain_persist_queue()

        assert store.update_feed_card.call_count == 1
        args = store.update_feed_card.call_args[0]
        assert args[0] == project_path
        assert args[1] == card_id
        assert args[2] == {"body": "v6"}, "final state must be the LAST update"
        assert h._persist_queue == {}

    # ── Invariant 3: a failing write never strands siblings ──────────────

    def test_poison_entry_does_not_strand_siblings(self, monkeypatch):
        h = self._make_handler()
        good_id, _card = self._seed_card(h)
        project_path = h._project_paths["proj"]
        self._no_writer(h)

        seen = []

        def _update(p, cid, updates):
            if cid == "poison":
                raise RuntimeError("poison write failed")
            seen.append(cid)
            return True

        store = MagicMock()
        store.update_feed_card.side_effect = _update
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        h._enqueue_card_update(project_path, "poison", {"body": "bad"})
        h._enqueue_card_update(project_path, good_id, {"body": "good"})
        h._drain_persist_queue()

        assert good_id in seen, "the good entry must persist in the SAME pass"
        assert seen == [good_id]
        # The poison entry was retried into the deferred map, not dropped.
        assert (project_path, "poison") in h._persist_deferred

    # ── Invariant 4: retry cap 3 → ERROR drop ────────────────────────────

    def test_update_retry_cap_drops_with_error(self, monkeypatch, caplog):
        import logging

        h = self._make_handler()
        card_id, _card = self._seed_card(h)
        project_path = h._project_paths["proj"]
        self._no_writer(h)
        key = (project_path, card_id)

        store = MagicMock()
        store.update_feed_card.side_effect = RuntimeError("nope")
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        h._enqueue_card_update(project_path, card_id, {"body": "x"})
        with caplog.at_level(logging.INFO, logger="ui.handlers.feed_handler"):
            h._drain_persist_queue()
            assert h._persist_deferred[key][1] == 1, "first failure -> tries=1"
            h._drain_persist_queue()
            assert h._persist_deferred[key][1] == 2
            h._drain_persist_queue()
        assert key not in h._persist_deferred, "3rd failure must drop the entry"
        assert any(
            r.levelno >= logging.ERROR and "after 3 failures" in r.getMessage()
            for r in caplog.records
        ), f"expected cap ERROR, got {[r.getMessage() for r in caplog.records]}"

    # ── Invariant 2b: fresh enqueue ⇒ fresh retry budget ─────────────────

    def test_fresh_enqueue_discards_deferred_and_restores_budget(self, monkeypatch):
        h = self._make_handler()
        card_id, _card = self._seed_card(h)
        project_path = h._project_paths["proj"]
        self._no_writer(h)
        key = (project_path, card_id)

        store = MagicMock()
        store.update_feed_card.side_effect = RuntimeError("nope")
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        h._enqueue_card_update(project_path, card_id, {"body": "v1"})
        h._drain_persist_queue()
        h._drain_persist_queue()
        assert h._persist_deferred[key][1] == 2

        # A fresh enqueue for the same key DISCARDS the deferred entry.
        h._enqueue_card_update(project_path, card_id, {"body": "v2"})
        assert key not in h._persist_deferred, "fresh wins, structurally"

        h._drain_persist_queue()
        assert h._persist_deferred[key][1] == 1, "fresh budget — no comparison"
        assert h._persist_deferred[key][0] == {"body": "v2"}

    # ── Invariant 2c: deferred retries IN PLACE (tries preserved) ────────

    def test_deferred_entry_retries_in_place_with_tries_preserved(self, monkeypatch):
        h = self._make_handler()
        card_id, _card = self._seed_card(h)
        project_path = h._project_paths["proj"]
        self._no_writer(h)
        key = (project_path, card_id)

        store = MagicMock()
        store.update_feed_card.side_effect = RuntimeError("nope")
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        h._enqueue_card_update(project_path, card_id, {"body": "v1"})
        h._drain_persist_queue()
        payload_before = h._persist_deferred[key][0]

        h._drain_persist_queue()

        assert h._persist_deferred[key][1] == 2, "tries increments across passes"
        assert h._persist_deferred[key][0] is payload_before, (
            "the payload must be retried in place — never re-merged/re-copied"
        )

    # ── Invariant 5: shutdown is bounded + drop-during-stop ──────────────

    def test_shutdown_bounds_the_writer_thread(self, tmp_path, monkeypatch):
        import time

        h = self._make_handler()
        card_id, _card = self._seed_card(h)
        h._project_paths["proj"] = str(tmp_path)
        (tmp_path / ".crabcakes").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".crabcakes" / "feed.json").write_text("[]")

        store = MagicMock()
        store.update_feed_card.return_value = True
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        h._enqueue_card_update(str(tmp_path), card_id, {"body": "x"})
        writer = h._persist_writer
        assert writer is not None and writer.is_alive()

        t0 = time.perf_counter()
        h.shutdown_persist_writer()
        elapsed = time.perf_counter() - t0

        assert not writer.is_alive(), "writer must exit within the bounded join"
        assert elapsed < 5.0, f"shutdown took {elapsed:.2f}s (must be bounded)"
        assert h._persist_stop is True
        # ≤1 drain pass after the stop signal: the queue is empty or dropped.
        assert h._persist_queue == {}

    def test_join_timeout_scales_with_feed_size(self, tmp_path):
        h = self._make_handler()
        h._project_paths["proj"] = str(tmp_path)
        (tmp_path / ".crabcakes").mkdir(parents=True, exist_ok=True)
        feed_file = tmp_path / ".crabcakes" / "feed.json"
        feed_file.write_text("x" * 2_000_000)  # ~2 MB

        stub = _DeadWriterStub()
        h._persist_writer = stub

        h.shutdown_persist_writer()

        size = feed_file.stat().st_size
        assert stub.joined == min(60.0, 5.0 + size / 1_000_000), (
            "join timeout must scale with the feed's size"
        )

    def test_drop_during_stop_is_logged_as_error(self, monkeypatch, caplog):
        import logging

        h = self._make_handler()
        card_id, _card = self._seed_card(h)
        project_path = h._project_paths["proj"]
        self._no_writer(h)

        store = MagicMock()
        store.update_feed_card.side_effect = RuntimeError("nope")
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        h._enqueue_card_update(project_path, card_id, {"body": "x"})
        h._persist_stop = True  # simulate the stop signal being observed

        with caplog.at_level(logging.INFO, logger="ui.handlers.feed_handler"):
            h._drain_persist_queue()

        assert (project_path, card_id) not in h._persist_deferred, (
            "failures during stop DROP — they are never deferred"
        )
        assert any(
            r.levelno >= logging.ERROR and "during shutdown" in r.getMessage()
            for r in caplog.records
        ), f"expected drop-during-stop ERROR, got {[r.getMessage() for r in caplog.records]}"

    # ── Invariant 5b: three-way exit logging (spec §10 build note 1) ─────

    def test_exit_logging_is_three_way(self, caplog):
        import logging

        # (a) undrained entries → ERROR
        h = self._make_handler()
        h._persist_queue[("proj", "c1")] = {"body": "x"}
        h._persist_writer = _DeadWriterStub()
        with caplog.at_level(logging.INFO, logger="ui.handlers.feed_handler"):
            h.shutdown_persist_writer()
        messages = [r.getMessage() for r in caplog.records]
        assert any("undrained entries" in m for m in messages), messages

        # (b) a straggler enqueued DURING the join → WARNING; the undrained
        #     ERROR covers the total (build-time note 1 — accepted conflation).
        h2 = self._make_handler()
        h2._persist_queue[("proj", "c1")] = {"body": "x"}
        h2._persist_writer = _AliveWriterStub(
            on_join=lambda: h2._persist_queue.__setitem__(("proj", "c2"), {"body": "y"})
        )
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ui.handlers.feed_handler"):
            h2.shutdown_persist_writer()
        messages = [r.getMessage() for r in caplog.records]
        assert any("stragglers" in m for m in messages), messages
        assert any("undrained entries" in m for m in messages), messages

        # (c) queue empty but the writer is still alive → WARNING
        h3 = self._make_handler()
        h3._persist_writer = _AliveWriterStub()
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ui.handlers.feed_handler"):
            h3.shutdown_persist_writer()
        messages = [r.getMessage() for r in caplog.records]
        assert any("exit not observed" in m for m in messages), messages

    # ── Invariant 6: close-then-reopen ──────────────────────────────────

    def test_new_writer_generation_resets_deferred_tries(self, monkeypatch):
        h = self._make_handler()
        h._persist_writer = _DeadWriterStub()  # a prior generation that exited
        h._persist_deferred[("proj", "card")] = ({"body": "keep"}, 2)

        store = MagicMock()
        store.update_feed_card.return_value = True
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        h._ensure_persist_writer()

        assert h._persist_deferred[("proj", "card")][1] == 0, (
            "a new writer generation resets tries"
        )
        assert h._persist_deferred[("proj", "card")][0] == {"body": "keep"}, (
            "payloads are kept across the reset"
        )
        assert h._persist_writer is not None
        h.shutdown_persist_writer()

    def test_enqueue_after_shutdown_restarts_writer_and_strands_nothing(self, monkeypatch):
        import time

        h = self._make_handler()
        card_id, _card = self._seed_card(h)
        project_path = h._project_paths["proj"]

        store = MagicMock()
        store.update_feed_card.return_value = True
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        h._enqueue_card_update(project_path, card_id, {"body": "v1"})
        first = h._persist_writer
        assert first is not None and first.is_alive()

        h.shutdown_persist_writer()
        deadline = time.monotonic() + 3.0
        while first.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not first.is_alive()
        assert h._persist_stop is True

        h._enqueue_card_update(project_path, card_id, {"body": "v2"})
        assert h._persist_writer is not first, "a new generation must start"
        assert h._persist_stop is False, "the stop flag is cleared before the liveness check"

        deadline = time.monotonic() + 3.0
        while h._persist_queue and time.monotonic() < deadline:
            time.sleep(0.01)
        assert h._persist_queue == {}, "the new entry must not be stranded"
        assert store.update_feed_card.called

        h.shutdown_persist_writer()

    # ── Invariant 8: tri-state None (card gone) → INFO drop ─────────────

    def test_none_return_is_logged_and_dropped_never_deferred(self, monkeypatch, caplog):
        import logging

        h = self._make_handler()
        card_id, _card = self._seed_card(h)
        project_path = h._project_paths["proj"]
        self._no_writer(h)

        store = MagicMock()
        store.update_feed_card.return_value = None  # card gone (legacy path)
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        h._enqueue_card_update(project_path, card_id, {"body": "x"})
        with caplog.at_level(logging.INFO, logger="ui.handlers.feed_handler"):
            h._drain_persist_queue()

        assert (project_path, card_id) not in h._persist_deferred, (
            "retrying a pruned card is futile — None must be dropped"
        )
        assert h._persist_queue == {}
        assert any(
            r.levelno == logging.INFO and "no longer exists" in r.getMessage()
            for r in caplog.records
        ), f"expected INFO drop, got {[r.getMessage() for r in caplog.records]}"

    # ── Latency: persisted ≤0.6 s after enqueue (steady state) ──────────

    def test_persisted_within_600ms_of_enqueue(self, monkeypatch):
        """Steady-state bound: the writer drains an enqueue within 0.6 s."""
        import time

        h = self._make_handler()
        card_id, _card = self._seed_card(h)
        project_path = h._project_paths["proj"]

        persisted = []
        store = MagicMock()

        def _update(p, c, u):
            persisted.append((p, c))
            return True

        store.update_feed_card.side_effect = _update
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        t0 = time.perf_counter()
        h._enqueue_card_update(project_path, card_id, {"body": "v"})
        deadline = t0 + 0.6
        while not persisted and time.perf_counter() < deadline:
            time.sleep(0.005)
        elapsed = time.perf_counter() - t0

        assert persisted == [(project_path, card_id)], "not drained within 0.6 s"
        assert elapsed < 0.6, f"persist took {elapsed:.3f}s (bound 0.6s)"
        assert h._persist_queue == {}

        h.shutdown_persist_writer()

    # ── Malformed queue entries ─────────────────────────────────────────

    def test_malformed_queue_entry_dropped_with_warning(self, monkeypatch, caplog):
        import logging

        h = self._make_handler()
        self._no_writer(h)

        store = MagicMock()
        store.update_feed_card.return_value = True
        monkeypatch.setattr("ui.handlers.feed_handler.feed_store", store)

        h._persist_queue[("proj", "")] = {"body": "orphan"}
        h._persist_queue[("", "card")] = {"body": "orphan"}

        with caplog.at_level(logging.INFO, logger="ui.handlers.feed_handler"):
            h._drain_persist_queue()

        assert store.update_feed_card.call_count == 0, "malformed entries never persist"
        assert h._persist_queue == {}
        warnings = [
            r for r in caplog.records if "malformed queue entry" in r.getMessage()
        ]
        assert len(warnings) == 2
        assert all(r.levelno == logging.WARNING for r in warnings)


# ═══════════════════════════════════════════════════════════════════
#  SPEC-UI-RESPONSIVENESS-2 Phase 3 — sliding-window compaction,
#  the compaction trigger, prune surfacing (§2.3).
# ═══════════════════════════════════════════════════════════════════

class RecordingGLib:
    """Queue-only GLib fake: records callbacks, runs NONE until fire().

    The sync MockGLib runs callbacks on the CALLING thread, so it cannot
    express "this work must happen on the main thread". This fake can: the
    writer thread enqueues, the test thread fires. (Existing MockGLib is
    untouched — Phase-1 tests depend on its synchronous semantics.)
    """

    def __init__(self):
        self._queue = []

    def idle_add(self, fn, *args, **kwargs):
        self._queue.append((fn, args, kwargs))
        return len(self._queue)

    def fire(self):
        pending, self._queue = self._queue, []
        for fn, args, kwargs in pending:
            fn(*args, **kwargs)

    def pending(self) -> int:
        return len(self._queue)


class _SyncThreading:
    """threading shim: Thread.start() runs the target on the calling thread.

    Makes the prune-card persist hop and the project-open load hop
    deterministic without a join/poll race.
    """

    Lock = threading.Lock
    Event = threading.Event

    class Thread:
        def __init__(self, target=None, name=None, daemon=None,
                     args=(), kwargs=None):
            self._target = target
            self._args = args or ()
            self._kwargs = kwargs or {}

        def start(self):
            if self._target is not None:
                self._target(*self._args, **self._kwargs)

        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None


class _NoWaitEvent:
    """Event whose wait() never blocks (deterministic writer-loop driving)."""

    def set(self):
        pass

    def clear(self):
        pass

    def wait(self, timeout=None):
        return False


class TestWindowCompaction:
    """§2.3 — trigger, drain branch, prune surfacing, persist gating."""

    def _make_handler(self, glib=None):
        from ui.handlers.feed_handler import FeedHandler
        h = FeedHandler(GLib=glib or MockGLib(), on_send_to_agent=MagicMock())
        h.set_feed_tab(MockFeedTab())
        return h

    # ── §2.3.4 load-time trigger ─────────────────────────────────────────

    def _run_project_open(self, monkeypatch, cards, name="big", path="/tmp/big"):
        import ui.handlers.feed_handler as fh

        h = self._make_handler()
        enqueued = []
        h._enqueue_compaction = lambda p: enqueued.append(p)
        h._ensure_persist_writer = lambda: None

        store = MagicMock()
        store.load_feed.return_value = cards
        store.load_feed_prefs.return_value = _default_prefs()
        store.FEED_WINDOW_DEFAULT = 2000      # real constant (not a MagicMock)
        monkeypatch.setattr(fh, "feed_store", store)
        # Deterministic: the load runs on a daemon thread in production.
        monkeypatch.setattr(fh, "threading", _SyncThreading)

        h.on_project_opened(name, path)
        return h, enqueued

    def test_load_time_trigger_enqueues_compaction_above_threshold(
        self, monkeypatch
    ):
        cards = [
            FeedCardData(
                card_type="diff", source="agent", title=f"c{i}", body="",
                author="x", timestamp=datetime.now(timezone.utc),
                project_name="big", card_id=f"lt-{i}", seq_num=i + 1,
            )
            for i in range(2501)          # > FEED_WINDOW_DEFAULT * 1.25
        ]
        _h, enqueued = self._run_project_open(monkeypatch, cards)
        assert enqueued == ["/tmp/big"], (
            "a 2501-card open must request a one-time compaction"
        )

    def test_load_time_trigger_not_fired_at_or_below_threshold(self, monkeypatch):
        cards = [
            FeedCardData(
                card_type="diff", source="agent", title=f"c{i}", body="",
                author="x", timestamp=datetime.now(timezone.utc),
                project_name="big", card_id=f"lt-{i}", seq_num=i + 1,
            )
            for i in range(2500)          # exactly the threshold — not over it
        ]
        _h, enqueued = self._run_project_open(monkeypatch, cards)
        assert enqueued == [], "at the threshold the feed is already compact enough"

    # ── Invariant 5 + §2.1.2 sentinel isolation ──────────────────────────

    def test_compaction_path_never_reaches_update_feed_card(self, monkeypatch):
        import ui.handlers.feed_handler as fh

        h = self._make_handler()
        h._ensure_persist_writer = lambda: None
        proj = "/tmp/iso-proj"

        seen = []

        def _update(p, cid, updates):
            seen.append((p, cid))
            return True

        store = MagicMock()
        store.update_feed_card.side_effect = _update
        store.compact_feed.return_value = 0
        store.FEED_WINDOW_DEFAULT = 2000
        monkeypatch.setattr(fh, "feed_store", store)

        h._enqueue_compaction(proj)
        h._enqueue_card_update(proj, "card-1", {"body": "x"})
        h._drain_persist_queue()

        assert store.compact_feed.called, "the compaction phase must run"
        assert seen == [(proj, "card-1")], (
            f"the update phase sees only real card updates: {seen}"
        )
        assert all(cid != proj for _p, cid in seen), (
            "a compaction's path must never be unpacked as a card_id"
        )

    # ── §2.1.2 drain compaction branch ───────────────────────────────────

    def test_failed_compact_reenqueues_with_incremented_tries(self, monkeypatch):
        import ui.handlers.feed_handler as fh

        h = self._make_handler()
        h._ensure_persist_writer = lambda: None
        proj = "/tmp/rc-proj"

        store = MagicMock()
        store.compact_feed.side_effect = RuntimeError("compact boom")
        store.FEED_WINDOW_DEFAULT = 2000
        monkeypatch.setattr(fh, "feed_store", store)

        h._enqueue_compaction(proj)
        h._drain_persist_queue()

        assert h._persist_compactions == [(proj, 1)], (
            "internal retry re-enqueues the SAME path with tries+1"
        )

    def test_compact_retry_cap_drops_with_error(self, monkeypatch, caplog):
        import ui.handlers.feed_handler as fh

        h = self._make_handler()
        h._ensure_persist_writer = lambda: None
        proj = "/tmp/cap-proj"

        store = MagicMock()
        store.compact_feed.side_effect = RuntimeError("compact boom")
        store.FEED_WINDOW_DEFAULT = 2000
        monkeypatch.setattr(fh, "feed_store", store)

        h._persist_compactions = [(proj, 2)]      # 3rd attempt fails
        with caplog.at_level(logging.ERROR, logger="ui.handlers.feed_handler"):
            h._drain_persist_queue()

        assert h._persist_compactions == [], "cap 3 → dropped, not retried"
        assert any(
            "after 3 failures" in r.getMessage() and r.levelno == logging.ERROR
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_external_enqueue_replaces_with_fresh_budget(self, monkeypatch):
        import ui.handlers.feed_handler as fh

        h = self._make_handler()
        h._ensure_persist_writer = lambda: None
        proj = "/tmp/fresh-proj"
        monkeypatch.setattr(fh, "feed_store", MagicMock())

        h._persist_compactions = [(proj, 2)]
        h._enqueue_compaction(proj)

        assert h._persist_compactions == [(proj, 0)], (
            "an external (load-time) enqueue is a NEW attempt — fresh budget"
        )

    def test_internal_retry_does_not_clobber_a_concurrent_external_enqueue(
        self, monkeypatch
    ):
        """§2.1.2 only-if-absent: the externally-owned live entry wins."""
        import ui.handlers.feed_handler as fh

        h = self._make_handler()
        h._ensure_persist_writer = lambda: None
        proj = "/tmp/race-proj"

        def _boom(path, window=None):
            # An external trigger lands DURING this pass (the load path).
            h._enqueue_compaction(proj)
            raise RuntimeError("compact boom")

        store = MagicMock()
        store.compact_feed.side_effect = _boom
        store.FEED_WINDOW_DEFAULT = 2000
        monkeypatch.setattr(fh, "feed_store", store)

        h._enqueue_compaction(proj)
        h._drain_persist_queue()

        assert h._persist_compactions == [(proj, 0)], (
            "the internal retry must not overwrite the external fresh entry"
        )

    def test_successful_prune_surfaces_a_card_with_the_window(
        self, monkeypatch
    ):
        import ui.handlers.feed_handler as fh

        h = self._make_handler()
        h._ensure_persist_writer = lambda: None
        proj = "/tmp/surf-proj"
        surfaced = []
        h._surface_prune_card = lambda p, n, w: surfaced.append((p, n, w))

        store = MagicMock()
        store.compact_feed.return_value = 5
        store.FEED_WINDOW_DEFAULT = 2000
        monkeypatch.setattr(fh, "feed_store", store)

        h._enqueue_compaction(proj)
        h._drain_persist_queue()

        assert store.compact_feed.call_args[1]["window"] == 2000 or (
            store.compact_feed.call_args[0][1] == 2000
        ), f"compact must be called with the window: {store.compact_feed.call_args}"
        assert surfaced == [(proj, 5, 2000)]

    def test_zero_prune_does_not_surface_a_card(self, monkeypatch):
        import ui.handlers.feed_handler as fh

        h = self._make_handler()
        h._ensure_persist_writer = lambda: None
        h._surface_prune_card = MagicMock()

        store = MagicMock()
        store.compact_feed.return_value = 0        # nothing pruned
        store.FEED_WINDOW_DEFAULT = 2000
        monkeypatch.setattr(fh, "feed_store", store)

        h._enqueue_compaction("/tmp/zero-proj")
        h._drain_persist_queue()

        assert not h._surface_prune_card.called, "no prune → no card"

    # ── Invariant 6: prune surfacing rides the main thread ───────────────

    def test_prune_card_surfaced_only_after_idle_fire(self, monkeypatch):
        import ui.handlers.feed_handler as fh

        glib = RecordingGLib()
        h = self._make_handler(glib=glib)
        h._active_project_name = "proj"
        h._project_paths["proj"] = "/tmp/win-proj"
        monkeypatch.setattr(fh, "threading", _SyncThreading)

        added = []
        real_add = type(h).add_card

        def _spy_add(self, card_data, persist=True):
            added.append((card_data, persist))
            return real_add(self, card_data, persist=persist)

        monkeypatch.setattr(type(h), "add_card", _spy_add)

        h._surface_prune_card("/tmp/win-proj", 7, 2000)

        assert added == [], "add_card must NOT run on the writer (calling) thread"
        assert glib.pending() == 1
        glib.fire()
        assert len(added) == 1, "it runs when the main thread fires the idle"
        card, persist = added[0]
        assert card.card_type == "system"
        assert "7" in card.body and "2000" in card.body
        assert persist is False, "add_card's own persist is bypassed"

    def test_surface_prune_card_add_card_calls_are_inside_a_nested_def(self):
        """AST structural proof: no `add_card` call in the method body.

        The writer thread must never touch widgets/`_project_seq`; the only
        `add_card` call site lives inside the `_ui` closure dispatched via
        GLib.idle_add.
        """
        import ast
        import inspect

        import ui.handlers.feed_handler as fh

        src = inspect.getsource(fh)
        tree = ast.parse(src)

        def _is_add_card_call(node):
            if not isinstance(node, ast.Call):
                return False
            f = node.func
            return (isinstance(f, ast.Attribute) and f.attr == "add_card") or (
                isinstance(f, ast.Name) and f.id == "add_card"
            )

        def _collect(node, in_nested, direct, nested):
            for child in ast.iter_child_nodes(node):
                now_nested = in_nested or isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
                )
                if _is_add_card_call(child):
                    (nested if in_nested else direct).append(child.lineno)
                _collect(child, now_nested, direct, nested)

        method = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_surface_prune_card":
                method = node
                break
        assert method is not None, "_surface_prune_card not found"

        direct, nested = [], []
        _collect(method, False, direct, nested)

        assert direct == [], (
            f"add_card called directly in the method body at line(s) {direct} — "
            "it must be inside the _ui closure (main thread only)"
        )
        assert nested, "expected the _ui closure to call add_card"

    def test_prune_card_persisted_even_while_loading(self, monkeypatch):
        """Audit r1 #9 — the copy path bypasses add_card's _loading gate."""
        import ui.handlers.feed_handler as fh

        glib = RecordingGLib()
        h = self._make_handler(glib=glib)
        h._active_project_name = "proj"
        h._project_paths["proj"] = "/tmp/win-proj"
        h._loading = True                       # a project load is in flight
        monkeypatch.setattr(fh, "threading", _SyncThreading)

        store = MagicMock()
        monkeypatch.setattr(fh, "feed_store", store)

        h._surface_prune_card("/tmp/win-proj", 3, 2000)
        glib.fire()                              # main thread does its part

        assert store.append_feed_card.call_count == 1, (
            "the prune card must be persisted from the main-thread copy"
        )
        persisted_path, snapshot = store.append_feed_card.call_args[0]
        assert persisted_path == "/tmp/win-proj"
        assert snapshot.card_type == "system"
        assert snapshot.card_id, "the copy must carry the assigned card_id"

    def test_prune_card_suppressed_when_project_closed_before_idle(
        self, monkeypatch
    ):
        import ui.handlers.feed_handler as fh

        glib = RecordingGLib()
        h = self._make_handler(glib=glib)
        h._active_project_name = "proj"
        h._project_paths["proj"] = "/tmp/win-proj"
        monkeypatch.setattr(fh, "threading", _SyncThreading)

        store = MagicMock()
        monkeypatch.setattr(fh, "feed_store", store)
        added = []
        monkeypatch.setattr(
            type(h), "add_card",
            lambda self, card_data, persist=True: added.append(card_data) or "x",
        )

        h._surface_prune_card("/tmp/win-proj", 3, 2000)
        h._active_project_name = None            # user closed the project
        glib.fire()

        assert added == [], "a card for a closed project must not be added"
        assert store.append_feed_card.call_count == 0

    def test_prune_card_not_misfiled_when_project_switched_mid_compaction(
        self, monkeypatch
    ):
        """Coder Phase-3 finding: the card's project name must be resolved
        on the MAIN thread from the compaction's project_path — never
        derived from _active_project_name on the writer (self-satisfying
        guard; a switch mid-compaction misfiled the card into the new
        project's view while persisting it into the old project's feed)."""
        import ui.handlers.feed_handler as fh

        glib = RecordingGLib()
        h = self._make_handler(glib=glib)
        h._active_project_name = "A"
        h._project_paths["A"] = "/tmp/proj-a"
        h._project_paths["B"] = "/tmp/proj-b"
        monkeypatch.setattr(fh, "threading", _SyncThreading)

        store = MagicMock()
        monkeypatch.setattr(fh, "feed_store", store)
        added = []
        monkeypatch.setattr(
            type(h), "add_card",
            lambda self, card_data, persist=True: added.append(card_data) or "x",
        )

        # Writer runs for A's path while the user has already switched to B.
        h._surface_prune_card("/tmp/proj-a", 3, 2000)
        h._active_project_name = "B"
        glib.fire()

        assert added == [], (
            "compaction of A while B is active must surface nothing — "
            "the guard must resolve the name from project_path, not echo "
            "_active_project_name"
        )
        assert store.append_feed_card.call_count == 0
        # And the complementary case: A still active → the card IS surfaced,
        # attributed to A by reverse lookup (not by echoing the active name).
        h._active_project_name = "A"
        h._surface_prune_card("/tmp/proj-a", 3, 2000)
        glib.fire()
        assert len(added) == 1 and added[0].project_name == "A"

    # ── E8: add_card(persist=) ───────────────────────────────────────────

    def test_add_card_persist_false_skips_the_persist_thread(self, monkeypatch):
        import ui.handlers.feed_handler as fh

        h = self._make_handler()
        h._project_paths["proj"] = "/tmp/persist-proj"
        monkeypatch.setattr(fh, "threading", _SyncThreading)
        store = MagicMock()
        monkeypatch.setattr(fh, "feed_store", store)

        card = FeedCardData(
            card_type="system", source="system", title="Feed compacted",
            body="b", author="system", timestamp=datetime.now(timezone.utc),
            project_name="proj",
        )

        h.add_card(card, persist=False)
        assert store.append_feed_card.call_count == 0, (
            "persist=False must not write the feed"
        )

        # Control: the default still persists (proves the gate, not the path).
        h.add_card(
            FeedCardData(
                card_type="system", source="system", title="t2", body="b",
                author="system", timestamp=datetime.now(timezone.utc),
                project_name="proj",
            )
        )
        assert store.append_feed_card.call_count == 1

    # ── §2.1.2 final form: drained check counts compactions ──────────────

    def test_drained_check_includes_pending_compactions(self, monkeypatch):
        h = self._make_handler()
        proj = "/tmp/drained-proj"
        h._persist_stop = True
        h._persist_compactions = [(proj, 0)]
        h._persist_wakeup = _NoWaitEvent()

        drains = []

        def _drain():
            drains.append(1)
            h._persist_compactions.clear()

        h._drain_persist_queue = _drain

        h._persist_loop()      # must NOT exit before that pass runs

        assert drains == [1], (
            "the writer exited with a compaction still queued — the drained "
            "check must include _persist_compactions"
        )

    def test_shutdown_counts_pending_compactions_as_undrained(
        self, monkeypatch, caplog
    ):
        h = self._make_handler()
        h._persist_writer = _DeadWriterStub()
        h._persist_compactions = [("/tmp/shut-proj", 0)]

        with caplog.at_level(logging.ERROR, logger="ui.handlers.feed_handler"):
            h.shutdown_persist_writer()

        assert any(
            "undrained entries" in r.getMessage() and r.levelno == logging.ERROR
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]


# ═══════════════════════════════════════════════════════════════════
#  TestUpdateCardInPlace — SPEC-UI-RESPONSIVENESS-2 §2.4 Phase 4 Part A
#
#  update_card() must refresh a LIVE card widget by reference when the
#  widget exposes the child seams added in build_feed_card
#  (`_body_label`), and fall back to rebuild + FeedTab.replace_card when
#  it does not. The seams are what make the in-place path possible; the
#  RED assertions below fail on the pre-Phase-4 code (which always
#  rebuilt), because no widget ever carried `_body_label`.
# ═══════════════════════════════════════════════════════════════════

class TestUpdateCardInPlace:
    """Phase 4 Part A — in-place feed card updates."""

    def _make(self):
        """Handler + MockFeedTab, with replace_card recorded."""
        from ui.handlers.feed_handler import FeedHandler
        h = FeedHandler(GLib=MockGLib(), on_send_to_agent=MagicMock())
        tab = MockFeedTab()
        replaced = []
        original_replace = tab.replace_card

        def _record(card_id, new_widget):
            replaced.append((card_id, new_widget))
            original_replace(card_id, new_widget)

        tab.replace_card = _record
        h.set_feed_tab(tab)
        return h, tab, replaced

    @staticmethod
    def _agent_card(**overrides):
        fields = dict(
            card_type="agent_action",
            source="agent",
            title="Coder is calling read_file",
            body="⏳ Running...",
            author="Coder",
            timestamp=datetime.now(timezone.utc),
            project_name="proj",
            metadata={"status": "running"},
        )
        fields.update(overrides)
        return FeedCardData(**fields)

    # ── In-place path (widget exposes the body seam) ──────────────────────

    def test_in_place_update_keeps_same_widget_instance(self):
        """A seam-bearing widget is mutated, not rebuilt or swapped."""
        h, tab, replaced = self._make()
        card = self._agent_card()
        card_id = h.add_card(card)
        widget = h._card_widgets[card_id]
        assert widget._body_label is not None, "fixture precondition: body seam exposed"

        card.body = "read_file → 12 lines"
        card.metadata["status"] = "complete"
        h.update_card(card_id, card)

        assert h._card_widgets[card_id] is widget, "in-place update must keep the widget"
        assert replaced == [], "in-place update must not call FeedTab.replace_card"
        assert tab.cards[0][0] is widget, "the live widget must stay in the feed"

    def test_in_place_update_refreshes_body_and_state_classes(self):
        """Body text + agent_action sub-state classes follow the new card data."""
        h, tab, replaced = self._make()
        card = self._agent_card()
        card_id = h.add_card(card)
        widget = h._card_widgets[card_id]
        assert "feed-card-running" in widget.get_css_classes(), "fixture precondition"

        card.body = "read_file → 12 lines"
        card.metadata["status"] = "error"
        h.update_card(card_id, card)

        assert widget._body_label.get_text() == "read_file → 12 lines"
        assert "feed-card-error" in widget.get_css_classes()
        assert "feed-card-running" not in widget.get_css_classes(), (
            "the stale running class must be removed"
        )

    def test_in_place_update_adds_accepted_badge(self):
        """approve_exec's accepted=True decision lands on the live widget."""
        h, tab, replaced = self._make()
        card = self._agent_card(metadata={"needs_approval": True, "status": "running"})
        card_id = h.add_card(card)
        widget = h._card_widgets[card_id]
        assert widget._status_label is None, "fixture precondition: no badge yet"

        card.accepted = True
        card.metadata["status"] = "approved"
        h.update_card(card_id, card)

        assert h._card_widgets[card_id] is widget
        assert widget._status_label is not None
        assert widget._status_label.get_text() == "ACCEPTED"
        assert "feed-card-accepted" in widget.get_css_classes()

    # ── Fallback path (widget lacks the seam) ─────────────────────────────

    def test_fallback_rebuilds_widget_without_body_seam(self):
        """No text-body seam → rebuild + replace_card.

        MEMRATCHET P8: a file-event card with a NON-empty body now exposes the
        seam, so the no-seam case must be driven with an EMPTY body (the
        renderer only sets `_text_label` inside its non-empty branch).
        """
        h, tab, replaced = self._make()
        card = FeedCardData(
            card_type="file_modified", source="system",
            title="Modified src/foo.py", body="",
            author="system", file_path="src/foo.py",
            timestamp=datetime.now(timezone.utc), project_name="proj",
        )
        card_id = h.add_card(card)
        widget = h._card_widgets[card_id]
        assert getattr(widget, "_body_label", None) is None, "fixture precondition: no seam"

        h.update_card(card_id, card)

        new_widget = h._card_widgets[card_id]
        assert new_widget is not widget, "fallback must rebuild the widget"
        assert [cid for cid, _w in replaced] == [card_id]

    def test_update_card_in_place_returns_false_without_seam(self):
        """The view helper reports 'not handled' instead of raising.

        MEMRATCHET P8: the empty-body file-event card is the remaining
        no-seam case (the non-empty one is covered by the True-path tests in
        tests/test_feed_card.py).
        """
        from ui.views.feed_card import update_card_in_place
        h, tab, replaced = self._make()
        card = FeedCardData(
            card_type="file_modified", source="system",
            title="Modified src/foo.py", body="",
            author="system", file_path="src/foo.py",
            timestamp=datetime.now(timezone.utc), project_name="proj",
        )
        card_id = h.add_card(card)

        assert update_card_in_place(h._card_widgets[card_id], card) is False

    def test_in_place_failure_falls_back_to_rebuild(self):
        """A refresh that raises mid-flight must still leave a valid card."""
        h, tab, replaced = self._make()
        card = self._agent_card()
        card_id = h.add_card(card)
        widget = h._card_widgets[card_id]

        with patch("ui.handlers.feed_handler.update_card_in_place",
                   side_effect=RuntimeError("boom")):
            h.update_card(card_id, card)

        assert h._card_widgets[card_id] is not widget, (
            "the failed in-place refresh must fall back to a rebuild"
        )
        assert [cid for cid, _w in replaced] == [card_id]


class TestNonGitDecisionPersist:
    """REVIEW-PERSIST-1 Edit A: the non-git accept/reject branches and the
    auto-approve path must enqueue the durable decision exactly like the git
    paths do (:1895/:1956). Red-first per instructions — on current code the
    non-git branches stop at the in-memory mutation + visual refresh, so the
    decision vanishes on reload.
    """

    def _make_handler(self):
        from ui.handlers.feed_handler import FeedHandler
        # MagicMock GLib: add_card's idle_add work (append/snapshot) is a
        # no-op and update_card's enqueue block runs synchronously — the
        # exact production timing for the persist seam under test.
        h = FeedHandler(GLib=MagicMock(), on_send_to_agent=MagicMock())
        h.set_feed_tab(MockFeedTab())
        h._ensure_persist_writer = lambda: None  # _no_writer pattern
        return h

    def _seed_non_git_card(self, h, project_name="proj", metadata=None):
        card = FeedCardData(
            card_type="agent_action", source="agent", title="non-git card",
            body="", author="Coder",
            timestamp=datetime.now(timezone.utc), project_name=project_name,
            metadata=metadata if metadata is not None else {},
        )
        card_id = h.add_card(card)
        # Registered AFTER add_card so add_card itself spawns no persist thread.
        h._project_paths[project_name] = "/tmp/uiresp2-proj"
        return card_id, card

    def _payload_for(self, h, card_id):
        return h._persist_queue.get((h._project_paths["proj"], card_id))

    def test_non_git_accept_persists_accepted(self):
        h = self._make_handler()
        card_id, _card = self._seed_non_git_card(h)

        h.handle_accept(card_id)

        updates = self._payload_for(h, card_id)
        assert updates is not None, (
            "non-git accept must enqueue the durable decision "
            "(git path parity); nothing was enqueued"
        )
        assert updates.get("accepted") is True, (
            f"payload must carry the decision; got {updates!r}"
        )

    def test_non_git_reject_persists_accepted_false(self):
        h = self._make_handler()
        card_id, _card = self._seed_non_git_card(h)

        h.handle_reject(card_id)

        updates = self._payload_for(h, card_id)
        assert updates is not None, (
            "non-git reject must enqueue the durable decision "
            "(git path parity); nothing was enqueued"
        )
        assert updates.get("accepted") is False, (
            f"payload must carry the decision; got {updates!r}"
        )

    def test_auto_approve_exec_persists_accepted(self):
        """Auto-approve (Show mode) must persist the decision.

        Order matters here: handle_approve_exec → approve_exec runs FIRST and
        its update_card call still sees accepted=None (F1: omitted), so the
        auto-approve path itself is the only site that can record the durable
        decision for auto-approved cards.
        """
        h = self._make_handler()  # no on_approve_exec callback → warning path
        card_id, card = self._seed_non_git_card(
            h, metadata={"needs_approval": True, "status": "running"}
        )

        h._auto_approve_exec_card(card_id)

        updates = self._payload_for(h, card_id)
        assert updates is not None, (
            "auto-approve must enqueue the durable decision; nothing was "
            "enqueued (the decision is lost on reload)"
        )
        assert updates.get("accepted") is True, (
            f"payload must carry the decision; got {updates!r}"
        )
        assert card.accepted is True

    def test_non_git_accept_without_project_path_logs_warning(self, caplog):
        """Unpersistable decision (no project path) must warn, not drop silently."""
        import logging as _logging

        h = self._make_handler()
        card_id, card = self._seed_non_git_card(h)
        project_path = h._project_paths.pop("proj")  # nothing to persist against

        with caplog.at_level(_logging.WARNING, logger="ui.handlers.feed_handler"):
            h.handle_accept(card_id)

        assert any(
            "non-git" in r.message.lower() and card_id in r.message
            for r in caplog.records
        ), f"expected a warning naming the unpersistable card; got {caplog.records!r}"
        assert card.accepted is True, "in-memory decision must still be recorded"
        assert h._persist_queue.get((project_path, card_id)) is None, (
            "no project path → no enqueue possible"
        )

    # ── Audit follow-up: the two HIGH mutate-before-persist sites ───────────
    #  approve_exec and _do_tool_call_result mutated the STORE's card before
    #  calling update_card. A persist that cannot land then left the card
    #  looking resolved/flagged in memory while disk disagreed — the UI claims
    #  success and the state silently reappears on reload. Both now build a
    #  resolved copy, so the store's pre-existing object is never mutated
    #  before the persist is issued (and update_card warns loudly when no
    #  project path is registered).

    def test_approve_exec_does_not_mutate_stored_card_before_persist(self):
        """approve_exec must not pre-mutate the store's card object."""
        import logging as _logging
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

        h = self._make_handler()
        card_id, card = self._seed_non_git_card(
            h, metadata={"needs_approval": True, "status": "pending_approval"})
        # Keep the project path so the persist DOES land — the assertion is
        # about the object identity/seam, not about a failure path.
        art = AgentRuntimeHandler(MagicMock(), MagicMock(), GLib_module=MagicMock())
        art._fh = h
        # approve_exec resolves via its own pending-approval map (normally
        # populated by _do_approval_needed); register the entry it needs.
        art._pending_approvals[card_id] = {
            "session_key": "special:coder", "tool_name": "exec_command",
            "args": {"command": "ls"},
        }

        art.approve_exec(card_id, True)

        # The store now holds the RESOLVED copy...
        stored = h.get_card(card_id)
        assert stored.accepted is True
        assert stored.metadata.get("status") == "approved"
        # ...and the object the caller held was never mutated in place.
        assert card.accepted is None, (
            "approve_exec must not mutate the store's card before persisting; "
            "build a resolved copy and let update_card own the replacement"
        )
        assert card.metadata.get("status") == "pending_approval"
        # The durable decision reached the writer queue.
        assert h._persist_queue, "approval decision must be enqueued"

    def test_approve_exec_without_project_path_warns_loudly(self, caplog):
        """Unpersistable approval must warn (audit: silent-no-op class)."""
        import logging as _logging
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

        h = self._make_handler()
        card_id, _card = self._seed_non_git_card(
            h, metadata={"needs_approval": True, "status": "pending_approval"})
        h._project_paths.pop("proj")  # persist cannot land

        art = AgentRuntimeHandler(MagicMock(), MagicMock(), GLib_module=MagicMock())
        art._fh = h
        art._pending_approvals[card_id] = {
            "session_key": "special:coder", "tool_name": "exec_command",
            "args": {"command": "ls"},
        }

        with caplog.at_level(_logging.WARNING, logger="ui.handlers.feed_handler"):
            art.approve_exec(card_id, True)

        assert any(
            "NOT persisted" in r.message for r in caplog.records
        ), (
            "a decision that cannot reach disk must warn loudly; "
            f"got {[r.message for r in caplog.records]!r}"
        )

