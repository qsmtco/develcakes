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

import pytest

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
