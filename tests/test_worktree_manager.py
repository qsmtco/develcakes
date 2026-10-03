# tests/test_worktree_manager.py
# SPEC-09 SP2 — WorktreeManager on tmp git repos (git init + initial commit
# in tmp_path; GitPython ops). Covers brief v2/v3 + probes J/I/K/L/M/N:
# creation+branch+realpath, idempotency, revival after rm -rf (branch REUSE,
# probe L), gitignore per-line exactness (B4), BLOCKING-1 dot-segment trio,
# session derivation (BLOCKING-2), two-agent isolation, symlinked-repo
# realpath discipline (SP0 BUG#14), non-git degrade (B3/v3),
# file-at-worktrees-root (LOW-7).

import os
import shutil

import git as gitpython
import pytest

from utils.worktree_manager import (
    _GITIGNORE_FORMS,
    WORKTREES_DIR_NAME,
    WorktreeError,
    WorktreeManager,
    is_worktree_of,
)

# The canonical form the manager writes (first of the accepted spellings).
GITIGNORE_LINE = _GITIGNORE_FORMS[0]


# ── fixtures ─────────────────────────────────────────────────────────────────

def _git_repo(tmp_path):
    """A minimal git repo with one commit (HEAD must exist for -b from HEAD)."""
    repo = gitpython.Repo.init(str(tmp_path))
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test User")
        cw.set_value("user", "email", "test@test.com")
    (tmp_path / "seed.txt").write_text("seed\n")
    repo.index.add(["seed.txt"])
    repo.index.commit("init")
    return repo


@pytest.fixture()
def repo(tmp_path):
    return _git_repo(tmp_path)


@pytest.fixture()
def mgr(repo):
    return WorktreeManager(str(repo.working_tree_dir))


# ── creation / idempotency / queries ─────────────────────────────────────────

def test_ensure_creates_worktree_and_branch(mgr, repo):
    path = mgr.ensure_worktree("coder")
    assert path is not None
    assert os.path.isdir(path)
    assert path == os.path.realpath(path)          # SP0 BUG#14: real path
    expected = os.path.join(
        str(repo.working_tree_dir), WORKTREES_DIR_NAME, "coder"
    )
    assert path == os.path.realpath(expected)      # under <repo>/.worktrees
    assert any(h.name == "agent/coder" for h in repo.heads)
    assert "coder" in repo.git.worktree("list")    # git registers it


def test_ensure_idempotent(mgr, repo):
    first = mgr.ensure_worktree("coder")
    second = mgr.ensure_worktree("coder")
    assert first == second
    # no duplicate branch, no error
    assert sum(1 for h in repo.heads if h.name == "agent/coder") == 1


def test_path_for_absent_none(mgr):
    assert mgr.path_for("never-created") is None


def test_remove_then_path_none(mgr):
    mgr.ensure_worktree("coder")
    assert mgr.path_for("coder") is not None
    mgr.remove_worktree("coder")
    assert mgr.path_for("coder") is None


def test_remove_absent_noop(mgr):
    mgr.remove_worktree("ghost")  # must not raise
    assert mgr.path_for("ghost") is None


def test_revival_after_external_deletion(mgr, repo):
    """rm -rf the worktree dir (registration stays) -> ensure revives on the
    SAME branch. Probe L: naive re-add -b is rc 255 ('branch already
    exists'); naive plain re-add is rc 128 ('missing but already
    registered'); prune-then-branch-check is the working revival. Documented
    semantics: the existing agent/<id> branch is REUSED with its history."""
    path = mgr.ensure_worktree("coder")

    # Leave a commit on the worktree branch so reuse is provable.
    wt_repo = gitpython.Repo(path)
    wt_path = os.path.join(path, "w.txt")
    with open(wt_path, "w", encoding="utf-8") as f:
        f.write("w\n")
    wt_repo.index.add(["w.txt"])
    wt_repo.index.commit("on-branch")
    branch_head_before = repo.commit("agent/coder").hexsha

    shutil.rmtree(path)
    assert mgr.path_for("coder") is None           # reads as absent

    revived = mgr.ensure_worktree("coder")
    assert revived == path                         # same real path
    assert repo.commit("agent/coder").hexsha == branch_head_before  # REUSED


def test_two_agents_distinct_paths(mgr, repo):
    a = mgr.ensure_worktree("coder-1")
    b = mgr.ensure_worktree("coder-2")
    assert a is not None and b is not None and a != b
    assert any(h.name == "agent/coder-1" for h in repo.heads)
    assert any(h.name == "agent/coder-2" for h in repo.heads)
    listed = mgr.list_worktrees()
    assert set(listed) == {"coder-1", "coder-2"}
    assert listed["coder-1"] == a
    assert listed["coder-2"] == b


# ── BLOCKING-1/2: validation + derivation ────────────────────────────────────

@pytest.mark.parametrize(
    "bad",
    ["..", ".", "a/b", "a\\b", "", "a b", "a~b", "a\0b", "x/../y"],
)
def test_agent_id_validation_rejects(mgr, bad):
    with pytest.raises(ValueError):
        mgr.ensure_worktree(bad)
    with pytest.raises(ValueError):
        mgr.remove_worktree(bad)


def test_agent_id_validation_accepts(mgr):
    path = mgr.ensure_worktree("ok.name-1")        # valid class, no raise
    assert path is not None
    assert mgr.path_for("ok.name-1") is not None


def test_worktree_id_for_session_derivation():
    # BLOCKING-2: ':' -> '-' over the whole key (probe J: the raw session
    # key FAILS the class — the substitution is load-bearing).
    assert WorktreeManager.worktree_id_for_session("special:coder") == (
        "special-coder"
    )
    assert WorktreeManager.worktree_id_for_session("special:coder-2") == (
        "special-coder-2"
    )


def test_worktree_id_for_session_rejects_dot_segments():
    # RULING-3 is load-bearing AFTER substitution: the class still has '.',
    # so 'special:..' -> 'special-..' is fine, but a raw '..' key would
    # derive to itself — refused.
    for bad in ("..", ".", "no-separators/a"):
        with pytest.raises(ValueError):
            WorktreeManager.worktree_id_for_session(bad)


# ── gitignore (MED-1 / B4 per-line exactness) ────────────────────────────────

def test_gitignore_appended_when_missing(mgr, repo):
    gi_path = os.path.join(str(repo.working_tree_dir), ".gitignore")
    assert not os.path.exists(gi_path)
    mgr.ensure_worktree("coder")
    with open(gi_path, "r", encoding="utf-8") as f:
        assert GITIGNORE_LINE in f.read().splitlines()


def test_gitignore_substring_lookalike_does_not_skip_append(mgr, repo):
    """B4 (probe K-D): '.worktrees-old/' must NOT satisfy the per-line-exact
    check — a substring guard would skip the needed append."""
    gi_path = os.path.join(str(repo.working_tree_dir), ".gitignore")
    with open(gi_path, "w", encoding="utf-8") as f:
        f.write(".worktrees-old/\n*.pyc\n")
    mgr.ensure_worktree("coder")
    with open(gi_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert GITIGNORE_LINE in lines                 # appended despite lookalike
    assert ".worktrees-old/" in lines              # existing content preserved
    assert "*.pyc" in lines


def test_gitignore_existing_content_preserved_with_repair(mgr, repo):
    gi_path = os.path.join(str(repo.working_tree_dir), ".gitignore")
    with open(gi_path, "w", encoding="utf-8") as f:
        f.write("*.pyc")                           # NO trailing newline
    mgr.ensure_worktree("coder")
    with open(gi_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert lines == ["*.pyc", GITIGNORE_LINE]      # newline repaired + appended


# ── realpath discipline (SP0 BUG#14) ─────────────────────────────────────────

def test_realpath_discipline_symlinked_repo(tmp_path):
    """Repo reached via a SYMLINKED path: the manager's paths still resolve
    to the REAL repo's .worktrees (never a symlink into .worktrees)."""
    real_repo_dir = tmp_path / "real-repo"
    real_repo_dir.mkdir()
    _git_repo(real_repo_dir)
    link = tmp_path / "link-repo"
    link.symlink_to(real_repo_dir, target_is_directory=True)

    mgr = WorktreeManager(str(link))
    path = mgr.ensure_worktree("coder")
    assert path is not None
    assert path == os.path.realpath(path)
    assert not os.path.islink(path)
    assert path.startswith(str(os.path.realpath(real_repo_dir)))
    # direct child of the REAL .worktrees (the gate's rule)
    assert os.path.dirname(path) == os.path.realpath(
        os.path.join(str(real_repo_dir), WORKTREES_DIR_NAME)
    )


# ── non-git degrade (B3/v3) + file-at-worktrees-root (LOW-7) ────────────────

def test_non_git_repo_degrades_disabled(tmp_path):
    (tmp_path / "file.txt").write_text("not a repo\n")
    mgr = WorktreeManager(str(tmp_path))           # must NOT raise
    assert mgr.enabled is False
    assert mgr.path_for("x") is None
    assert mgr.list_worktrees() == {}
    assert mgr.ensure_worktree("x") is None        # disabled -> None, no raise


def test_worktrees_root_as_file_raises_cleanly(tmp_path):
    """LOW-7: <repo>/.worktrees existing as a FILE must surface as
    WorktreeError (a catchable contract), not a raw OSError crash."""
    _git_repo(tmp_path)
    (tmp_path / WORKTREES_DIR_NAME).write_text("i am a file\n")
    mgr = WorktreeManager(str(tmp_path))
    with pytest.raises(WorktreeError):
        mgr.ensure_worktree("coder")


# ── detached-HEAD membership (probe N item 1) ───────────────────────────────

def test_detached_head_worktree_stays_visible(mgr, repo):
    """A detached-HEAD worktree registers with branch value '' (falsy) —
    the manager must use MEMBERSHIP, not truthiness (probe N): path_for
    still returns it and ensure stays idempotent."""
    path = mgr.ensure_worktree("coder")
    assert path is not None
    wt_repo = gitpython.Repo(path)
    wt_repo.git.checkout("--detach")
    assert mgr.path_for("coder") == path           # membership, not truthiness
    assert mgr.ensure_worktree("coder") == path    # still idempotent
    assert mgr.list_worktrees() == {"coder": path}


# ── is_worktree_of (the ARH reset's recognizer) ──────────────────────────────

def test_is_worktree_of_shapes(tmp_path):
    repo_dir = tmp_path / "r"
    repo_dir.mkdir()
    _git_repo(repo_dir)
    mgr = WorktreeManager(str(repo_dir))
    wt = mgr.ensure_worktree("coder")
    assert wt is not None

    assert is_worktree_of(str(repo_dir), wt) is True
    assert is_worktree_of(str(repo_dir), str(repo_dir)) is False
    deep = os.path.join(wt, "sub")                 # probe I: NOT a direct child
    os.makedirs(deep, exist_ok=True)
    assert is_worktree_of(str(repo_dir), deep) is False
    assert is_worktree_of("", wt) is False
    assert is_worktree_of(str(repo_dir), None) is False
