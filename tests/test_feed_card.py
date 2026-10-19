# tests/test_feed_card.py
# Unit tests for models/feed_card.py — FeedCardData dataclass.
#
# Principle: test the contract — dataclass fields, css_class_for_type,
# and any runtime field behavior.

import pytest
from datetime import datetime, timezone
from models.feed_card import FeedCardData, CardType, CardSource


# ═══════════════════════════════════════════════════════════════════
#  FeedCardData field initialization
# ═══════════════════════════════════════════════════════════════════

class TestFeedCardDataRequiredFields:
    """Required fields: all must be provided at construction."""

    def test_required_fields_set(self):
        ts = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
        card = FeedCardData(
            card_type="diff",
            source="agent",
            title="Fix auth bug",
            body="+from auth import middleware",
            author="Qat",
            timestamp=ts,
            project_name="manopea",
        )
        assert card.card_type == "diff"
        assert card.source == "agent"
        assert card.title == "Fix auth bug"
        assert card.body == "+from auth import middleware"
        assert card.author == "Qat"
        assert card.timestamp == ts
        assert card.project_name == "manopea"

    def test_required_fields_missing_raises(self):
        ts = datetime.now(timezone.utc)
        with pytest.raises(TypeError):
            FeedCardData(card_type="diff", source="agent", title="x",
                         body="y", author="z", timestamp=ts)
        # missing project_name


class TestFeedCardDataOptionalFields:
    """Optional context fields default to None."""

    def test_optional_context_fields_default_to_none(self):
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="x",
            body="y", author="z", timestamp=ts, project_name="p",
        )
        assert card.file_path is None
        assert card.commit_sha is None
        assert card.additions is None
        assert card.deletions is None
        assert card.task_id is None
        assert card.metadata == {}

    def test_optional_context_fields_can_be_set(self):
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="x",
            body="y", author="z", timestamp=ts, project_name="p",
            file_path="src/main.py",
            commit_sha="abc1234",
            additions=42,
            deletions=7,
            task_id="TASK-001",
            metadata={"key": "value"},
        )
        assert card.file_path == "src/main.py"
        assert card.commit_sha == "abc1234"
        assert card.additions == 42
        assert card.deletions == 7
        assert card.task_id == "TASK-001"
        assert card.metadata == {"key": "value"}


class TestFeedCardDataRuntimeFields:
    """Runtime fields (set by FeedHandler) default correctly."""

    def test_card_id_defaults_to_none(self):
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="git_commit", source="agent", title="x",
            body="y", author="z", timestamp=ts, project_name="p",
        )
        assert card.card_id is None

    def test_reviewed_defaults_to_false(self):
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="git_commit", source="agent", title="x",
            body="y", author="z", timestamp=ts, project_name="p",
        )
        assert card.reviewed is False

    def test_accepted_defaults_to_none(self):
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="git_commit", source="agent", title="x",
            body="y", author="z", timestamp=ts, project_name="p",
        )
        assert card.accepted is None  # pending

    def test_accepted_can_be_true(self):
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="system", title="x",
            body="y", author="z", timestamp=ts, project_name="p",
            accepted=True,
        )
        assert card.accepted is True

    def test_accepted_can_be_false(self):
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="system", title="x",
            body="y", author="z", timestamp=ts, project_name="p",
            accepted=False,
        )
        assert card.accepted is False


# ═══════════════════════════════════════════════════════════════════
#  css_class_for_type()
# ═══════════════════════════════════════════════════════════════════

class TestCssClassForType:
    """Map card types to CSS class names."""

    @pytest.mark.parametrize("card_type,expected", [
        ("git_commit", "feed-card-git"),
        ("diff", "feed-card-diff"),
        ("file_created", "feed-card-file-new"),
        ("file_deleted", "feed-card-file-del"),
        ("dir_created", "feed-card-dir-new"),
        ("agent_action", "feed-card-agent"),
        ("task", "feed-card-task"),
        ("system", "feed-card-system"),
        ("audit_report", "feed-card-audit"),
    ])
    def test_known_types_return_correct_class(self, card_type, expected):
        assert FeedCardData.css_class_for_type(card_type) == expected

    def test_unknown_type_returns_system_class(self):
        assert FeedCardData.css_class_for_type("unknown_type") == "feed-card-system"
        assert FeedCardData.css_class_for_type("") == "feed-card-system"
        assert FeedCardData.css_class_for_type("not_a_card") == "feed-card-system"


# ═══════════════════════════════════════════════════════════════════
#  All seven card types are constructable
# ═══════════════════════════════════════════════════════════════════

class TestAllCardTypes:
    """Every card_type value in CardType Literal is constructable."""

    @pytest.mark.parametrize("card_type", [
        "git_commit",
        "diff",
        "file_created",
        "file_deleted",
        "dir_created",
        "agent_action",
        "task",
        "system",
        "audit_report",
    ])
    def test_card_type_constructable(self, card_type):
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type=card_type,
            source="agent",
            title=f"Test {card_type}",
            body="body",
            author="test",
            timestamp=ts,
            project_name="test-project",
        )
        assert card.card_type == card_type


# ═══════════════════════════════════════════════════════════════════
#  All four card sources are constructable
# ═══════════════════════════════════════════════════════════════════

class TestAllCardSources:
    """Every CardSource value is constructable."""

    @pytest.mark.parametrize("source", ["agent", "system", "git", "crabwatch"])
    def test_card_source_constructable(self, source):
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff",
            source=source,
            title="Test",
            body="body",
            author="test",
            timestamp=ts,
            project_name="test-project",
        )
        assert card.source == source

# ═══════════════════════════════════════════════════════════════════
#  is_actionable() — Phase 1
# ═══════════════════════════════════════════════════════════════════

class TestIsActionable:
    """Phase 1: is_actionable() returns True for cards that need user action."""

    def test_diff_card_is_actionable(self):
        assert FeedCardData.is_actionable("diff", {}) is True

    def test_file_created_is_actionable(self):
        assert FeedCardData.is_actionable("file_created", {}) is True

    def test_file_modified_is_actionable(self):
        assert FeedCardData.is_actionable("file_modified", {}) is True

    def test_file_deleted_is_actionable(self):
        assert FeedCardData.is_actionable("file_deleted", {}) is True

    def test_needs_approval_makes_actionable(self):
        """Any card type with metadata.needs_approval=True is actionable."""
        assert FeedCardData.is_actionable("agent_action", {"needs_approval": True}) is True
        assert FeedCardData.is_actionable("system", {"needs_approval": True}) is True

    def test_git_commit_not_actionable(self):
        assert FeedCardData.is_actionable("git_commit", {}) is False

    def test_system_not_actionable(self):
        assert FeedCardData.is_actionable("system", {}) is False

    def test_none_metadata(self):
        """is_actionable must handle metadata=None gracefully."""
        assert FeedCardData.is_actionable("diff", None) is True
        assert FeedCardData.is_actionable("git_commit", None) is False


# ═══════════════════════════════════════════════════════════════════
#  is_informational() — Phase 1
# ═══════════════════════════════════════════════════════════════════

class TestIsInformational:
    """Phase 1: is_informational() returns True for read-only cards."""

    def test_git_commit_is_informational(self):
        assert FeedCardData.is_informational("git_commit", {}) is True

    def test_agent_action_running_is_informational(self):
        assert FeedCardData.is_informational("agent_action", {"status": "running"}) is True

    def test_agent_action_complete_is_informational(self):
        assert FeedCardData.is_informational("agent_action", {"status": "complete"}) is True

    def test_agent_action_error_is_informational(self):
        assert FeedCardData.is_informational("agent_action", {"status": "error"}) is True

    def test_agent_action_no_status_is_informational(self):
        """Issue 5 fix: status=None → still informational."""
        assert FeedCardData.is_informational("agent_action", {}) is True
        assert FeedCardData.is_informational("agent_action", None) is True

    def test_system_is_informational(self):
        assert FeedCardData.is_informational("system", {}) is True

    def test_audit_report_is_informational(self):
        assert FeedCardData.is_informational("audit_report", {}) is True

    def test_task_is_informational(self):
        assert FeedCardData.is_informational("task", {}) is True

    def test_dir_created_is_informational(self):
        assert FeedCardData.is_informational("dir_created", {}) is True

    def test_dir_deleted_is_informational(self):
        assert FeedCardData.is_informational("dir_deleted", {}) is True

    def test_needs_approval_not_informational(self):
        """Approval cards are actionable, not informational."""
        assert FeedCardData.is_informational("agent_action", {"needs_approval": True}) is False
        assert FeedCardData.is_informational("diff", {"needs_approval": True}) is False


# ═══════════════════════════════════════════════════════════════════
#  is_actionable and is_informational are mutually exclusive
# ═══════════════════════════════════════════════════════════════════

class TestActionableInformationalMutuallyExclusive:
    """For any card type + metadata combination, exactly one of
    is_actionable and is_informational returns True (never both)."""

    @pytest.mark.parametrize("card_type", [
        "git_commit", "diff", "file_created", "file_modified", "file_deleted",
        "dir_created", "dir_deleted", "agent_action", "task", "system",
        "audit_report",
    ])
    def test_not_both_true(self, card_type):
        result_actionable = FeedCardData.is_actionable(card_type, {})
        result_informational = FeedCardData.is_informational(card_type, {})
        assert not (result_actionable and result_informational), f"Both True for {card_type}"

    def test_needs_approval_actionable_not_info(self):
        result_actionable = FeedCardData.is_actionable("agent_action", {"needs_approval": True})
        result_informational = FeedCardData.is_informational("agent_action", {"needs_approval": True})
        assert result_actionable is True
        assert result_informational is False

    def test_needs_approval_on_diff_actionable_not_info(self):
        result_actionable = FeedCardData.is_actionable("diff", {"needs_approval": True})
        result_informational = FeedCardData.is_informational("diff", {"needs_approval": True})
        assert result_actionable is True
        assert result_informational is False


# ═══════════════════════════════════════════════════════════════════
#  TestSeqNum — Phase 3
#  Verifies seq_num serialization round-trips through to_dict/from_dict.
# ═══════════════════════════════════════════════════════════════════

class TestSeqNumSerialization:
    """Phase 3: seq_num persists through serialization."""

    def test_seq_num_field_defaults_to_none(self):
        """seq_num field defaults to None when not provided."""
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="x", body="y",
            author="z", timestamp=ts, project_name="p",
        )
        assert card.seq_num is None

    def test_seq_num_can_be_set(self):
        """seq_num can be set at construction time."""
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="x", body="y",
            author="z", timestamp=ts, project_name="p", seq_num=42,
        )
        assert card.seq_num == 42

    def test_seq_num_round_trips_through_to_dict(self):
        """seq_num is included in to_dict()."""
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="x", body="y",
            author="z", timestamp=ts, project_name="p", seq_num=7,
        )
        d = card.to_dict()
        assert d.get("seq_num") == 7

    def test_seq_num_round_trips_through_from_dict(self):
        """seq_num survives from_dict() deserialization."""
        ts = datetime.now(timezone.utc)
        original = FeedCardData(
            card_type="diff", source="agent", title="x", body="y",
            author="z", timestamp=ts, project_name="p", seq_num=99,
        )
        d = original.to_dict()
        restored = FeedCardData.from_dict(d)
        assert restored.seq_num == 99

    def test_seq_num_none_not_in_dict(self):
        """seq_num=None does not break from_dict."""
        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="x", body="y",
            author="z", timestamp=ts, project_name="p",
        )
        d = card.to_dict()
        # seq_num should be present in dict as None
        assert "seq_num" in d
        assert d["seq_num"] is None

    def test_seq_num_old_format_still_loads(self):
        """from_dict handles cards without seq_num key (old feed.json format)."""
        import json
        ts = datetime.now(timezone.utc)
        old_dict = {
            "card_type": "diff",
            "source": "agent",
            "title": "Old card",
            "body": "",
            "author": "x",
            "timestamp": ts.isoformat(),
            "project_name": "old-project",
            # no seq_num key
        }
        card = FeedCardData.from_dict(old_dict)
        assert card.seq_num is None


# ═══════════════════════════════════════════════════════════════════
#  RETIRED — the GTK body seam (MEMRATCHET P8 / §2.3, §2.6).
#
#  `TestBodySeamIsTotal` (x5) and `TestInPlaceUpdateFileEventAndTask` (x4)
#  pinned `build_feed_card()` exposing a `_body_label` seam so
#  `update_card_in_place()` could mutate a live widget instead of rebuilding
#  it. Both factories were deleted with the GTK feed path (WU feed-surface
#  chunk 3b): the feed is a WebKit document built by render/feed_html.py, and
#  an update is a document upsert — there is no widget to mutate in place.
#
#  The render-side equivalent (body capped at FEED_RENDERED_BODY_LIMIT with
#  an exact hidden count and a reveal control) is pinned on the HTML renderer
#  by tests/test_feed_html.py. The FeedCardData contract tests above stay.
# ═══════════════════════════════════════════════════════════════════
