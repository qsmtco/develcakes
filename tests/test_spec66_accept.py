"""SPEC-66 §2.9/§11.3 — Accept is path-limited (R-17).

`stage_paths` stages exactly the card's paths; `handle_accept` then compares
git's ``index.diff("HEAD")`` report to the REPO-RELATIVE NORMALIZED form of
the card's path — not the raw ``card.file_path``. So ``b.txt``, ``./b.txt``,
an absolute path and ``sub/../b.txt`` all match the same index entry.

Refusal cases: a path that escapes the project, an empty list, and any OTHER
file in the index (those stay staged — no ``git reset``).

No ``stage_all`` call on the accept path (§2.9). Uses a real git repo in
tmp_path; no GTK, no ui.window.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from models.feed_card import FeedCardData
from ui.feed.review import FeedReviewMixin
from utils import git_ops
from utils.git_ops import stage_paths


def _git(repo, *args):
    import git as gitpython
    return gitpython.Repo(str(repo)).git.execute(["git", *args], strip_newline_in_stdout=True)


@pytest.fixture
def repo(tmp_path):
    """A real repo with one committed file and an untracked b.txt."""
    gitpython = pytest.importorskip("git")
    root = tmp_path / "proj"
    root.mkdir()
    sub = root / "sub"
    sub.mkdir()
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    r = gitpython.Repo.init(str(root))
    with r.config_writer() as cw:
        cw.set_value("user", "name", "T")
        cw.set_value("user", "email", "t@example.com")
    r.index.add(["a.txt"])
    r.index.commit("init")
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    return root


NORMALIZED_FORMS = (
    ("plain relative", "b.txt"),
    ("dot relative", "./b.txt"),
    ("absolute", None),  # filled in below
    ("dotdot segment", "sub/../b.txt"),
)


class TestStagePaths:
    def test_stages_only_the_named_path(self, repo):
        res = stage_paths(str(repo), ["b.txt"])
        assert res.success is True
        staged = {d.a_path or d.b_path for d in _repo_index_diff(repo)}
        assert staged == {"b.txt"}

    def test_empty_list_refuses(self, repo):
        res = stage_paths(str(repo), [])
        assert res.success is False
        assert res.error == "no paths"

    def test_path_escaping_the_project_refuses_and_stages_nothing(self, tmp_path, repo):
        outside = tmp_path / "outside.txt"
        outside.write_text("x\n", encoding="utf-8")
        res = stage_paths(str(repo), [str(outside)])
        assert res.success is False
        assert _repo_index_diff(repo) == []


def _repo_index_diff(repo):
    import git as gitpython
    return gitpython.Repo(str(repo)).index.diff("HEAD")


class TestNormalizedIndexComparison:
    """The matrix §11.3 demands: every spelling matches the index report."""

    @pytest.mark.parametrize("label,raw", NORMALIZED_FORMS)
    def test_each_spelling_matches_the_repo_relative_index_entry(self, repo, label, raw):
        card_path = str(repo / "b.txt") if raw is None else raw
        res = stage_paths(str(repo), [card_path])
        assert res.success is True, label

        # This is the §11.3 normalization handle_accept uses to compare.
        resolved = os.path.realpath(os.path.join(os.path.realpath(str(repo)), card_path))
        staged_rel = os.path.relpath(
            resolved, os.path.realpath(str(repo))
        ).replace("\\", "/")
        assert staged_rel == "b.txt", label

        index_paths = {
            (d.a_path or d.b_path or "").replace("\\", "/")
            for d in _repo_index_diff(repo)
        }
        index_paths.discard("")
        others = sorted(index_paths - {staged_rel})
        assert others == [], f"{label}: normalization failed, others={others}"

    def test_a_second_staged_file_is_detected(self, repo):
        """The refusal condition: any OTHER path in the index report."""
        (repo / "c.txt").write_text("c\n", encoding="utf-8")
        import git as gitpython
        repo_obj = gitpython.Repo(str(repo))
        repo_obj.index.add(["c.txt"])

        res = stage_paths(str(repo), ["./b.txt"])
        assert res.success is True

        resolved = os.path.realpath(os.path.join(os.path.realpath(str(repo)), "./b.txt"))
        staged_rel = os.path.relpath(resolved, os.path.realpath(str(repo))).replace("\\", "/")
        index_paths = {
            (d.a_path or d.b_path or "").replace("\\", "/")
            for d in _repo_index_diff(repo)
        }
        index_paths.discard("")
        others = sorted(index_paths - {staged_rel})
        assert others == ["c.txt"]

    def test_stage_all_is_never_called_on_accept(self):
        """§2.9: the accept path must not call stage_all.

        Pins the source contract — a revert to stage_all is R-17's mutation.
        """
        import inspect

        from ui.feed import review

        src = inspect.getsource(review)
        handle_accept = src.split("def handle_accept", 1)[1].split("def handle_reject", 1)[0]
        # The CALL form, not the word: a comment legitimately says
        # "never stage_all".
        assert ".stage_all(" not in handle_accept, "handle_accept must not call stage_all"
        assert ".stage_paths(" in handle_accept


# ── SPEC-66 BUG #5: every failed accept tells the person on the card ─────────
#
# Two exits in `_git_accept` used to leave the card untouched: a clean tree
# (nothing to commit) and a diff read that raised. Both now record
# `metadata["accept_error"]` (+ `status = "error"`) through `_fail`, which is
# what the feed document renders. These tests drive the REAL `handle_accept`
# against a REAL repo — the two cases the external report reproduced.

class _InlineGLib:
    """idle_add runs the callback inline (no main loop in a test)."""

    def idle_add(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return 0


class _SyncThreading:
    """threading shim: Thread.start() runs the target on the calling thread.

    Same idiom as tests/test_feed_handler.py — the accept path hands
    `_git_accept` to a daemon thread, so without this the assertions would
    race the worker.
    """

    class Thread:
        def __init__(self, target=None, daemon=None, args=(), kwargs=None):
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


class _AcceptHost(FeedReviewMixin):
    """Minimal mixin host: the card map, the project map, and update_card.

    `update_card` is the (main-thread) surface the feed re-renders from, so
    recording its calls is how the tests observe the surfaced failure.
    """

    def __init__(self, repo, project_name="proj"):
        self._cards = {}
        self._project_paths = {project_name: str(repo)}
        self._recent_git_paths = {}
        self.updates = []

    def update_card(self, card_id, card_data):
        self.updates.append((card_id, card_data))


def _accept_in(monkeypatch, repo, file_path):
    """Run one real-repo accept; return (host, card, commit_calls)."""
    import ui.handlers.feed_handler as fh

    commits = []
    monkeypatch.setattr(git_ops, "commit", lambda *a, **k: commits.append(a))
    monkeypatch.setattr(fh, "threading", _SyncThreading)
    monkeypatch.setattr(fh, "_GLib", _InlineGLib(), raising=False)

    host = _AcceptHost(repo)
    card = FeedCardData(
        card_type="file_modified", source="agent", title="change",
        body="+x", author="agent", timestamp=datetime.now(timezone.utc),
        project_name="proj", file_path=file_path, metadata={},
    )
    card_id = "c1"
    host._cards[card_id] = card
    host.handle_accept(card_id)
    return host, card, commits


class TestHandleAcceptFailureSurfacing:
    """BUG #5: a failed accept is recorded on the card, never a dead silence."""

    def test_clean_tree_accept_records_accept_error(self, monkeypatch, repo):
        """The card's file already matches HEAD: no commit, no accept, but
        the card records why nothing committed."""
        # a.txt is committed and unmodified — the index diff is empty.
        host, card, commits = _accept_in(monkeypatch, repo, "a.txt")

        assert commits == [], "a clean tree must not produce a commit"
        assert card.accepted is not True
        assert card.accepted is None  # still pending: the failure never decides
        assert "nothing to commit" in card.metadata["accept_error"]
        assert card.metadata["status"] == "error"
        # ...and the surface was redrawn with that failure.
        assert host.updates and host.updates[-1][1] is card

    def test_diff_read_failure_records_accept_error(self, monkeypatch, repo):
        """Staging succeeds but the diff read raises (HEAD points at a
        nonexistent ref → BadName): the card records the failure."""
        (repo / "a.txt").write_text("a2\n", encoding="utf-8")
        # After the initial commit and a real working-tree change, so staging
        # succeeds and only `index.diff("HEAD")` blows up.
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/nonexistent\n", encoding="utf-8")

        host, card, commits = _accept_in(monkeypatch, repo, "a.txt")

        assert commits == []
        assert card.accepted is not True
        assert "diff read failed" in card.metadata["accept_error"]
        assert card.metadata["status"] == "error"
        assert host.updates and host.updates[-1][1] is card
