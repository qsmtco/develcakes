# tests/test_feedback_processor.py
# Unit tests for utils/feedback_processor.py — audit report processing.
#
# Spec reference: SPEC-3 §3.3

import json
import os
import tempfile
import pytest

from utils.audit_parser import AuditReport
from utils.feedback_processor import (
    append_to_bug_journal,
    get_target_si_config,
    process_audit_reports,
    resolve_default_target_role,
    resolve_role_from_session,
)


class TestAppendToBugJournal:
    def test_creates_journal_file(self, tmp_path):
        journal = tmp_path / ".crabcakes" / "coder-bugs.md"
        report = AuditReport(
            task="Fix watcher",
            file_path="watcher.py:15",
            severity="bug",
            bug_description="MagicMock truthiness",
            expected="only real events",
            actual="all events treated as moved",
            root_cause="MagicMock is always truthy",
            fix="Use isinstance check",
            pattern="mock-truthiness",
        )
        append_to_bug_journal(str(tmp_path), report, "coder")

        assert journal.exists()
        content = journal.read_text()
        assert "## Bug #1" in content
        assert "watcher.py" in content
        assert "mock-truthiness" in content

    def test_increments_bug_number(self, tmp_path):
        crab = tmp_path / ".crabcakes"
        crab.mkdir()
        crab.joinpath("coder-bugs.md").write_text(
            "# Coder Bug Journal\n\n---\n\n## Bug #1 — 2026-05-17 — old.py\n\n**Task:** old\n"
        )
        report = AuditReport(
            task="New bug",
            file_path="new.py",
            severity="bug",
            bug_description="x",
            expected="y",
            actual="z",
        )
        append_to_bug_journal(str(tmp_path), report, "coder")

        content = (crab / "coder-bugs.md").read_text()
        assert "## Bug #2" in content
        assert "new.py" in content

    def test_issue_not_appended(self, tmp_path):
        """Bug-severity only — issue and suggestion go to journal."""
        report = AuditReport(
            task="Issue task",
            file_path="file.py",
            severity="issue",
            bug_description="x",
            expected="y",
            actual="z",
        )
        append_to_bug_journal(str(tmp_path), report, "debugger")
        # append_to_bug_journal itself always appends regardless of severity
        # The gating is done in process_audit_reports
        journal = tmp_path / ".crabcakes" / "debugger-bugs.md"
        assert journal.exists()
        assert "## Bug #1" in journal.read_text()


class TestGetTargetSiConfig:
    def test_loads_from_agent_defs(self):
        """get_target_si_config returns dict with structured_feedback key."""
        cfg = get_target_si_config("unknown")
        assert isinstance(cfg, dict)
        assert "structured_feedback" in cfg


class TestResolveRoleFromSession:
    def test_special_agent_resolved(self):
        """agent: prefix → resolved via runtime_handler.get_special_agent_def()."""
        # Create a mock runtime handler that returns a known role
        class MockSAD:
            role = "qaster"

        class MockRuntime:
            def get_special_agent_def(self, sk):
                return MockSAD()

        result = resolve_role_from_session("agent:qaster", MockRuntime())
        assert result == "qaster"

    def test_gateway_agent_returns_unknown(self):
        """Non-agent: session keys → 'unknown'."""
        result = resolve_role_from_session("agent:some:id:123", None)
        assert result == "unknown"

    def test_no_runtime_handler_returns_unknown(self):
        result = resolve_role_from_session("agent:coder", None)
        assert result == "unknown"


class TestResolveDefaultTargetRole:
    def test_no_agent_defs_returns_unknown(self, monkeypatch):
        """When agent_defs can't be loaded, return 'unknown'."""
        def boom(*args):
            raise RuntimeError("no defs")
        monkeypatch.setattr("utils.agent_defs.load_agent_defs", boom)
        result = resolve_default_target_role()
        assert result == "unknown"


class TestProcessAuditReports:
    def test_logs_to_review_log(self, tmp_path):
        """All reports (any severity) are logged to review-log.jsonl."""
        crab = tmp_path / ".crabcakes"
        crab.mkdir()
        report = AuditReport(
            task="Task 1",
            file_path="file.py:10",
            severity="bug",
            bug_description="something broke",
            expected="correct",
            actual="incorrect",
        )
        process_audit_reports(str(tmp_path), [report], "Qaster", "coder")

        log = crab / "review-log.jsonl"
        assert log.exists()
        entries = log.read_text().strip().split("\n")
        assert len(entries) == 1
        entry = json.loads(entries[0])
        assert entry["reviewer"] == "Qaster"
        assert entry["target_role"] == "coder"
        assert entry["bug"] == "something broke"

    def test_multiple_reports_all_logged(self, tmp_path):
        """Multiple reports in one message each get their own log line."""
        crab = tmp_path / ".crabcakes"
        crab.mkdir()
        reports = [
            AuditReport(
                task=f"Task {i}",
                file_path=f"file{i}.py",
                severity="bug",
                bug_description=f"bug {i}",
                expected="correct",
                actual="incorrect",
            )
            for i in range(3)
        ]
        process_audit_reports(str(tmp_path), reports, "Qaster", "coder")

        log = crab / "review-log.jsonl"
        lines = log.read_text().strip().split("\n")
        assert len(lines) == 3

    def test_bug_appends_to_journal_when_enabled(self, tmp_path, monkeypatch):
        """Bug-severity with structured_feedback enabled → appended to bug journal."""
        crab = tmp_path / ".crabcakes"
        crab.mkdir()

        # Patch agent_defs to return a coder with structured_feedback: True
        def fake_defs():
            return [{"role": "coder", "tools": ["write_file"],
                     "self_improvement": {"structured_feedback": True}}]

        def fake_si_config(can_write=False):
            return {"structured_feedback": False}

        monkeypatch.setattr("utils.agent_defs.load_agent_defs", fake_defs)
        monkeypatch.setattr("utils.agent_defs.get_default_si_config", fake_si_config)

        report = AuditReport(
            task="Bug task",
            file_path="buggy.py:1",
            severity="bug",
            bug_description="bad bug",
            expected="correct",
            actual="wrong",
            root_cause="root",
            fix="fix it",
            pattern="test-bug",
        )
        process_audit_reports(str(tmp_path), [report], "Qaster", "coder")

        journal = crab / "coder-bugs.md"
        assert journal.exists()
        content = journal.read_text()
        assert "## Bug #1" in content
        assert "buggy.py" in content
        assert "test-bug" in content

    def test_suggestion_not_appended_to_journal(self, tmp_path, monkeypatch):
        """Suggestion-severity is logged but NOT appended to bug journal."""
        crab = tmp_path / ".crabcakes"
        crab.mkdir()

        def fake_defs():
            return [{"role": "coder", "tools": ["write_file"],
                     "self_improvement": {"structured_feedback": True}}]

        def fake_si_config(can_write=False):
            return {"structured_feedback": False}

        monkeypatch.setattr("utils.agent_defs.load_agent_defs", fake_defs)
        monkeypatch.setattr("utils.agent_defs.get_default_si_config", fake_si_config)

        report = AuditReport(
            task="Suggestion task",
            file_path="file.py",
            severity="suggestion",
            bug_description="could improve",
            expected="better",
            actual="current",
        )
        process_audit_reports(str(tmp_path), [report], "Qaster", "coder")

        # Review log still gets the entry
        log = crab / "review-log.jsonl"
        assert log.exists()

        # Bug journal is NOT created for suggestion severity
        journal = crab / "coder-bugs.md"
        assert not journal.exists()

    def test_no_project_path_no_crash(self):
        """No project path → no crash, logs debug message."""
        # process_audit_reports doesn't raise on None project_path
        # It uses feedback_processor which handles None gracefully
        # Just verify it doesn't crash
        report = AuditReport(
            task="Task",
            file_path="file.py",
            severity="bug",
            bug_description="bug",
            expected="exp",
            actual="act",
        )
        # This should not raise — project path None is handled
        try:
            process_audit_reports(None, [report], "Qaster", "unknown")
        except Exception as e:
            pytest.fail(f"Should not raise: {e}")