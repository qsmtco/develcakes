# tests/test_review_queues.py
# SPEC-10 SP2: ReviewHandler per-agent review queues + batch accept (D3 REV 2,
# D7b, D8, D9, D9b). RED-first: every test failed against pre-SP2 HEAD.
#
# Doubles are COPIED from tests/test_review_handler_feed_card.py (not
# imported — tests/ has no __init__.py and conftest puts only the project
# root on sys.path, so cross-test-module imports don't resolve).

import os
import shutil
import tempfile
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from models.review_state import QueueEntry, ReviewState

# ── Doubles (copied from test_review_handler_feed_card.py) ──────────────────

class MockGLib:
    def idle_add(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return 0


def _wait_until(cond, timeout=5.0, poll=0.01):
    """Poll cond() until truthy or timeout. Returns True on success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(poll)
    return cond()


class MockGitResult:
    def __init__(self, success=True, stdout="", sha="abc123def456", error=""):
        self.success = success
        self.stdout = stdout
        self.sha = sha
        self.error = error


def _make_handler(on_feed_card=None):
    """Create a ReviewHandler with all dependencies mocked."""
    from ui.handlers.review_handler import ReviewHandler
    if on_feed_card is None:
        on_feed_card = MagicMock()
    handler = ReviewHandler(
        GLib=MockGLib(),
        main_content=MagicMock(),
        project_handler=MagicMock(),
        on_review_started=MagicMock(),
        on_review_ended=MagicMock(),
        on_display_card=MagicMock(),
        on_display_text=MagicMock(),
        on_feed_card=on_feed_card,
    )
    return handler


# ── Real-git fixtures (SP2 touches REAL git_ops for start_review/accept) ────

@pytest.fixture
def temp_repo():
    """Temporary directory with a fresh git repo (one initial commit)."""
    tmpdir = tempfile.mkdtemp(prefix="spec10_queues_")
    import git as gitpython
    repo = gitpython.Repo.init(tmpdir)
    repo.config_writer().set_value("user", "name", "Test User").release()
    repo.config_writer().set_value("user", "email", "test@test.com").release()
    fpath = os.path.join(tmpdir, "hello.txt")
    with open(fpath, "w") as f:
        f.write("Hello\n")
    repo.index.add(["hello.txt"])
    repo.index.commit("Initial commit")
    yield os.path.realpath(tmpdir)
    shutil.rmtree(tmpdir, ignore_errors=True)


def _activate(handler, project_path, project_name="testproject"):
    """Register an open project with review mode ON, no active checkpoint."""
    handler._states[project_name] = ReviewState(
        project_path=project_path, review_mode="review")
    return handler


def _entry(agent_key, sha, path_used):
    return QueueEntry(
        agent_key=agent_key, sha=sha, path_used=path_used,
        ts=datetime.now(UTC))


# ── Tests (RED-first at pre-SP2 HEAD) ───────────────────────────────────────

class TestStartReviewEnqueuesPm:
    def test_start_review_enqueues_pm(self, temp_repo):
        """start_review → the 'pm' queue holds one entry: sha == checkpoint
        sha, path_used == project_path (GAP-3/D8)."""
        handler = _activate(_make_handler(), temp_repo)
        handler.start_review("testproject")

        got = _wait_until(lambda: getattr(handler, "_queues", {})
                          .get("testproject", {}).get("pm"))
        assert got, "start_review never enqueued the PM checkpoint"
        entries = handler._queues["testproject"]["pm"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry.agent_key == "pm"
        head = __import__("utils.git_ops", fromlist=["git_ops"]).get_head_sha(temp_repo)
        assert entry.sha == head.sha, "queued sha != checkpoint sha"
        assert entry.path_used == temp_repo
        assert isinstance(entry.ts, datetime)

    def test_trailer_on_pm_checkpoint(self, temp_repo):
        """The PM checkpoint commit itself carries 'Agent: pm' (D1/SP1)."""
        import git as gitpython
        handler = _activate(_make_handler(), temp_repo)
        handler.start_review("testproject")

        assert _wait_until(lambda: handler.get_state("testproject").checkpoint_sha), (
            "checkpoint sha never landed")
        body = gitpython.Repo(temp_repo).git.log("--format=%B", "-1")
        assert "Agent: pm" in body.splitlines(), (
            f"PM checkpoint missing 'Agent: pm' trailer: {body!r}")


class TestQueueStoreSurface:
    def test_agents_with_pending_and_pending_count(self):
        """D9b surface: empty → []; one agent; two agents; per-agent counts."""
        handler = _make_handler()
        assert handler.agents_with_pending("proj") == []
        assert handler.pending_count("proj", "special:coder") == 0

        handler._enqueue("proj", "special:coder", _entry("special:coder", "a" * 40, "/p"))
        handler._enqueue("proj", "special:debugger", _entry("special:debugger", "b" * 40, "/p"))
        handler._enqueue("proj", "special:coder", _entry("special:coder", "c" * 40, "/p"))

        assert handler.agents_with_pending("proj") == [
            "special:coder", "special:debugger"]
        assert handler.pending_count("proj", "special:coder") == 2
        assert handler.pending_count("proj", "special:debugger") == 1
        assert handler.pending_count("proj", "nobody") == 0

    def test_queue_cap_drops_oldest(self):
        """D9: 51st enqueue drops the OLDEST entry, keeps 50, emits a card."""
        captured = []
        handler = _make_handler(on_feed_card=captured.append)
        shas = [f"{i:040x}" for i in range(51)]
        for sha in shas:
            handler._enqueue("proj", "special:coder", _entry("special:coder", sha, "/p"))

        assert handler.pending_count("proj", "special:coder") == 50
        remaining = [e.sha for e in handler._queues["proj"]["special:coder"]]
        assert remaining == shas[1:], "oldest entry was not dropped FIFO"
        assert _wait_until(lambda: len(captured) >= 1), "no cap-overflow card"
        assert any("cap" in c.title.lower() for c in captured), (
            f"cap card title off: {[c.title for c in captured]}")


class TestAcceptAgentQueue:
    def test_accept_agent_queue_worktree_mark_reviewed(self, temp_repo):
        """D3 REV 2 step 2: worktree item accept = mark-reviewed + dequeue.
        NO new commit anywhere: project HEAD unchanged, worktree HEAD
        unchanged. Summary card emitted."""
        import git as gitpython
        wt_root = os.path.join(temp_repo, ".worktrees")
        wt_dir = os.path.join(wt_root, "coder-1")
        os.makedirs(wt_dir)
        wt_repo = gitpython.Repo.init(wt_dir)
        wt_repo.config_writer().set_value("user", "name", "T").release()
        wt_repo.config_writer().set_value("user", "email", "t@t.com").release()
        with open(os.path.join(wt_dir, "w.txt"), "w") as f:
            f.write("agent work\n")
        wt_repo.index.add(["w.txt"])
        wt_repo.index.commit("agent checkpoint")
        wt_sha_before = wt_repo.head.commit.hexsha
        proj_sha_before = gitpython.Repo(temp_repo).head.commit.hexsha

        captured = []
        handler = _activate(_make_handler(on_feed_card=captured.append), temp_repo)
        handler._enqueue("testproject", "special:coder", _entry(
            "special:coder", wt_sha_before, os.path.realpath(wt_dir)))

        handler.accept_agent_queue("special:coder", "testproject")
        assert _wait_until(lambda: len(captured) >= 1), "no summary card"

        assert handler.pending_count("testproject", "special:coder") == 0, (
            "worktree entry not dequeued")
        assert gitpython.Repo(temp_repo).head.commit.hexsha == proj_sha_before, (
            "project HEAD changed on a worktree mark-reviewed accept")
        assert gitpython.Repo(wt_dir).head.commit.hexsha == wt_sha_before, (
            "worktree HEAD changed — accept committed when it must not (D3 BUG#4)")
        assert any("special:coder" in c.title for c in captured), (
            f"summary card missing agent attribution: {[c.title for c in captured]}")

    def test_accept_agent_queue_stale_worktree_dropped(self, temp_repo):
        """D3 REV 2 step 4 (BUG#5): a stale worktree entry is dropped with an
        error card and does NOT abort remaining items; the later root entry
        for the same agent still lands."""
        import git as gitpython
        wt_dir = os.path.join(temp_repo, ".worktrees", "coder-2")
        os.makedirs(wt_dir)
        with open(os.path.join(temp_repo, "fresh.txt"), "w") as f:
            f.write("root work\n")

        captured = []
        handler = _activate(_make_handler(on_feed_card=captured.append), temp_repo)
        handler._enqueue("testproject", "special:coder", _entry(
            "special:coder", "1" * 40, os.path.realpath(wt_dir)))
        handler._enqueue("testproject", "special:coder", _entry(
            "special:coder", "2" * 40, temp_repo))
        shutil.rmtree(wt_dir)  # worktree vanishes before accept

        handler.accept_agent_queue("special:coder", "testproject")
        assert _wait_until(
            lambda: handler.pending_count("testproject", "special:coder") == 0), (
            "stale entry never dequeued")

        titles = [c.title for c in captured]
        assert any("stale" in t.lower() for t in titles), (
            f"no stale-drop card: {titles}")
        body = gitpython.Repo(temp_repo).git.log("--format=%B", "-1")
        assert "Agent: special:coder" in body.splitlines(), (
            "root entry after the stale one was not accepted")
        assert any("1 stale" in t for t in titles), (
            f"summary must report the stale count: {titles}")

    def test_accept_agent_queue_mid_batch_failure_aborts_remaining(self):
        """D3 REV 2 step 4: item-level git failure aborts remaining items;
        the abort card reports exactly how many succeeded first."""
        captured = []
        handler = _activate(_make_handler(on_feed_card=captured.append), "/tmp/spec10-not-a-repo")
        entries = [_entry("special:coder", f"{i:040x}", "/tmp/spec10-not-a-repo")
                   for i in range(3)]
        for e in entries:
            handler._enqueue("testproject", "special:coder", e)

        with patch("ui.handlers.review_handler.git_ops") as mock_git:
            mock_git.stage_all.return_value = MockGitResult(success=True)
            mock_git.commit.side_effect = [
                MockGitResult(success=True, sha="a" * 40),
                MockGitResult(success=False, error="boom"),
            ]
            handler.accept_agent_queue("special:coder", "testproject")
            assert _wait_until(lambda: any("PARTIAL" in c.title for c in captured)), (
                f"no partial-abort card: {[c.title for c in captured]}")

        remaining = handler._queues["testproject"]["special:coder"]
        assert remaining == entries[1:], (
            f"expected entries 2+3 to remain queued: {[e.sha[:7] for e in remaining]}")
        partial = next(c for c in captured if "PARTIAL" in c.title)
        assert "1" in partial.title, f"abort card must report 1 success: {partial.title}"

    def test_mid_batch_enqueue_lands_next_batch(self):
        """D3 REV 2 step 6: the batch iterates a SNAPSHOT — an entry enqueued
        while the batch runs (simulated: from inside the commit mock, the
        moment a checkpoint would land mid-batch) is NOT accepted in this
        batch and remains queued for the next one."""
        captured = []
        handler = _activate(_make_handler(on_feed_card=captured.append), "/tmp/spec10-not-a-repo")
        late = _entry("special:coder", "f" * 40, "/tmp/spec10-not-a-repo")

        commit_calls = {"n": 0}

        def _commit_with_racing_enqueue(project_path, message, **kwargs):
            # The 2nd item's git work is where a real mid-batch checkpoint
            # would land (checkpoint thread interleaves the batch) — enqueue
            # `late` at exactly that moment.
            commit_calls["n"] += 1
            if commit_calls["n"] == 2:
                handler._enqueue("testproject", "special:coder", late)
            return MockGitResult(success=True, sha="a" * 40)

        handler._enqueue("testproject", "special:coder", _entry("special:coder", "1" * 40, "/tmp/spec10-not-a-repo"))
        handler._enqueue("testproject", "special:coder", _entry("special:coder", "2" * 40, "/tmp/spec10-not-a-repo"))

        with patch("ui.handlers.review_handler.git_ops") as mock_git:
            mock_git.stage_all.return_value = MockGitResult(success=True)
            mock_git.commit.side_effect = _commit_with_racing_enqueue
            handler.accept_agent_queue("special:coder", "testproject")
            assert _wait_until(lambda: len(captured) >= 1), "no summary card"

        remaining = handler._queues["testproject"]["special:coder"]
        assert remaining == [late], (
            "mid-batch enqueue must land in the NEXT batch, not this one")

    def test_accept_all_queues_excludes_pm(self):
        """D8: accept_all_queues drains every agent queue and NEVER the 'pm'
        queue."""
        handler = _activate(_make_handler(), "/tmp/spec10-not-a-repo")
        handler._enqueue("testproject", "pm", _entry("pm", "p" * 40, "/tmp/spec10-not-a-repo"))
        handler._enqueue("testproject", "special:coder", _entry("special:coder", "c" * 40, "/tmp/spec10-not-a-repo"))
        handler._enqueue("testproject", "special:debugger", _entry("special:debugger", "d" * 40, "/tmp/spec10-not-a-repo"))

        with patch("ui.handlers.review_handler.git_ops") as mock_git:
            mock_git.stage_all.return_value = MockGitResult(success=True)
            mock_git.commit.return_value = MockGitResult(success=True, sha="a" * 40)
            handler.accept_all_queues("testproject")
            assert _wait_until(
                lambda: handler.agents_with_pending("testproject") == ["pm"]), (
                f"agent queues did not drain to pm-only: "
                f"{handler.agents_with_pending('testproject')}")

        assert handler.pending_count("testproject", "pm") == 1, (
            "D8 violation: batch accept touched the pm queue")


class TestQueueLifecycle:
    def test_queues_survive_project_close(self):
        """GAP-5b: closing the project tab pops ReviewState but KEEPS queue
        entries (in-memory persistence across reopen)."""
        handler = _make_handler()
        handler.on_project_opened("testproject", "/tmp/spec10-not-a-repo")
        handler._enqueue("testproject", "special:coder", _entry("special:coder", "c" * 40, "/tmp/spec10-not-a-repo"))

        handler.on_project_closed("testproject")

        assert handler.get_state("testproject") is None, "state should be popped"
        assert handler.agents_with_pending("testproject") == ["special:coder"], (
            "queues must survive project close (GAP-5b)")


# ── SP2 fix round (BUG#1–#5; D3 REV 3 + D8 REV 2) ───────────────────────────

class TestCleanTreeSemantics:
    """D3 REV 3: a root item on a CLEAN tree is the normal D2 end-state —
    accept = bookkeeping + dequeue, NEVER a fabricated empty commit."""

    def test_accept_root_clean_tree_dequeues(self, temp_repo):
        """Real git, clean tree: entry dequeued, NO commit created (HEAD
        count unchanged — proves no allow_empty fabrication), reviewed
        outcome in the summary card."""
        import git as gitpython
        commits_before = len(list(gitpython.Repo(temp_repo).iter_commits()))

        captured = []
        handler = _activate(_make_handler(on_feed_card=captured.append), temp_repo)
        handler._enqueue("testproject", "special:coder", _entry(
            "special:coder", "3" * 40, temp_repo))

        handler.accept_agent_queue("special:coder", "testproject")
        assert _wait_until(
            lambda: handler.pending_count("testproject", "special:coder") == 0), (
            "clean-tree root entry stranded in the queue (BUG#1)")

        commits_after = len(list(gitpython.Repo(temp_repo).iter_commits()))
        assert commits_after == commits_before, (
            f"clean-tree accept fabricated a commit ({commits_before}→{commits_after})")
        titles = [c.title for c in captured]
        bodies = [c.body for c in captured]
        assert any("reviewed" in t.lower() or "reviewed" in b.lower()
                   for t, b in zip(titles, bodies)), (
            f"no reviewed outcome in cards: {titles} / {bodies}")
        assert not any("PARTIAL" in t for t in titles), (
            f"clean tree must not abort the batch: {titles}")

    def test_accept_agent_queue_root_commit(self, temp_repo):
        """D3 REV 2 step 3: project-root item accept = stage + commit with
        'Agent: <key>' trailer, dequeue on success. Strengthened: HEAD count
        exactly +1 (one real commit, no empties)."""
        import git as gitpython
        with open(os.path.join(temp_repo, "new.txt"), "w") as f:
            f.write("uncommitted\n")
        commits_before = len(list(gitpython.Repo(temp_repo).iter_commits()))

        captured = []
        handler = _activate(_make_handler(on_feed_card=captured.append), temp_repo)
        handler._enqueue("testproject", "special:coder", _entry(
            "special:coder", "0" * 40, temp_repo))

        handler.accept_agent_queue("special:coder", "testproject")
        assert _wait_until(
            lambda: handler.pending_count("testproject", "special:coder") == 0), (
            "root entry not dequeued after accept")

        commits_after = len(list(gitpython.Repo(temp_repo).iter_commits()))
        assert commits_after == commits_before + 1, (
            f"expected exactly one real commit, got {commits_before}→{commits_after}")
        body = gitpython.Repo(temp_repo).git.log("--format=%B", "-1")
        assert "[review] accepted:" in body
        assert "Agent: special:coder" in body.splitlines(), (
            f"accept commit missing agent trailer: {body!r}")
        assert any("accepted" in c.title.lower() for c in captured), (
            f"no success card: {[c.title for c in captured]}")


class TestAcceptAllSerialization:
    """BUG#2 (SP2 fix): accept_all_queues runs ONE worker thread — never
    concurrent stage/commit on one repo."""

    def test_accept_all_two_agents_no_concurrency(self, temp_repo):
        """Real git, two agents: both drain, no PARTIAL cards, and every
        stage_all call happened on the SAME thread id (single worker). A
        sleep inside the mocked boundary widens the window: pre-fix, two
        spawned threads are provably both alive mid-stage (distinct live
        idents); post-fix one thread runs both agents sequentially."""
        import threading as th

        import git as gitpython

        # agent B has a dirty tree → its accept exercises the real commit path
        with open(os.path.join(temp_repo, "b_work.txt"), "w") as f:
            f.write("debugger work\n")
        commits_before = len(list(gitpython.Repo(temp_repo).iter_commits()))

        captured = []
        handler = _activate(_make_handler(on_feed_card=captured.append), temp_repo)
        handler._enqueue("testproject", "special:coder", _entry(
            "special:coder", "a" * 40, temp_repo))
        handler._enqueue("testproject", "special:debugger", _entry(
            "special:debugger", "b" * 40, temp_repo))

        # BUG#5 (fix round 4) daemon-bleed hardening: path-filtered counting
        # stage — a prior test's outliving _do daemon must NOT land in this
        # mock and fake a second thread id (dbg_sp2fix3_daemonbleed: pair
        # run 8/8 FAILED unfiltered; alone passed). BOTH-ORDERS REGRESSION:
        # this test must pass after ANY daemon-spawning test in this file —
        # run full-file order (alphabetical + reversed) when touching the
        # accept paths; the round-3-only_path pattern is the guard.
        thread_idents = set()
        real_stage = __import__("utils.git_ops", fromlist=["git_ops"]).stage_all

        def _recording_stage(project_path):
            if os.path.realpath(project_path) == os.path.realpath(temp_repo):
                thread_idents.add(th.get_ident())
            time.sleep(0.15)  # widen the window: pre-fix both threads are alive mid-stage
            return real_stage(project_path)

        with patch("ui.handlers.review_handler.git_ops") as mock_git:
            mock_git.stage_all.side_effect = _recording_stage
            real_commit_recorder: list = []

            def _real_commit(project_path, message, **kwargs):
                from utils import git_ops as real_ops
                result = real_ops.commit(project_path, message, **kwargs)
                real_commit_recorder.append(result)
                return result

            mock_git.commit.side_effect = _real_commit
            handler.accept_all_queues("testproject")
            assert _wait_until(
                lambda: handler.agents_with_pending("testproject") == []), (
                f"queues did not drain: {handler.agents_with_pending('testproject')}")

        titles = [c.title for c in captured]
        assert not any("PARTIAL" in t for t in titles), f"partial cards: {titles}"
        assert len(thread_idents) == 1, (
            f"BUG#2: stage_all ran on {len(thread_idents)} distinct threads — "
            f"concurrent workers on one repo: {thread_idents}")
        # The FIRST-queued agent hits the dirty tree and commits (with its
        # trailer); the second agent's accept then finds a clean tree and
        # drains via REV 3 bookkeeping — exactly one real commit, total.
        first_agent = "special:coder"
        made = [r for r in real_commit_recorder if r.success]
        assert len(made) == 1, f"expected exactly 1 real commit, got {len(made)}"
        body = gitpython.Repo(temp_repo).git.log("--format=%B", "-1")
        assert f"Agent: {first_agent}" in body.splitlines(), (
            f"commit trailer wrong: {body!r}")
        assert commits_before + 1 == len(list(gitpython.Repo(temp_repo).iter_commits()))


class TestPmQueueDrain:
    """D8 REV 2: the PM's own /accept and /reject IS the drain of "pm"."""

    def test_pm_queue_drained_by_accept_changes_commit_branch(self, temp_repo):
        """start_review (real) → pm enqueued; dirty tree; accept_changes →
        real commit branch resolves the session AND drains pm."""
        handler = _activate(_make_handler(), temp_repo)
        handler.start_review("testproject")
        assert _wait_until(
            lambda: handler.get_state("testproject").checkpoint_sha), "no checkpoint"
        assert handler.pending_count("testproject", "pm") == 1, "fixture precondition"

        with open(os.path.join(temp_repo, "pm_edit.txt"), "w") as f:
            f.write("pm work\n")
        handler.accept_changes("testproject", "approved")
        assert _wait_until(
            lambda: handler.get_state("testproject").checkpoint_sha is None), (
            "accept never resolved the session")
        assert handler.pending_count("testproject", "pm") == 0, (
            "BUG#4: accept_changes left the pm queue populated")

    def test_pm_queue_drained_by_accept_changes_clean_branch(self, temp_repo):
        """start_review on a CLEAN tree → 'Nothing to commit' branch also
        resolves the session → pm must drain there too."""
        handler = _activate(_make_handler(), temp_repo)
        handler.start_review("testproject")
        assert _wait_until(
            lambda: handler.get_state("testproject").checkpoint_sha), "no checkpoint"
        assert handler.pending_count("testproject", "pm") == 1

        handler.accept_changes("testproject", "approved")
        assert _wait_until(
            lambda: handler.get_state("testproject").checkpoint_sha is None), (
            "clean-tree accept never resolved the session")
        assert handler.pending_count("testproject", "pm") == 0, (
            "BUG#4: 'Nothing to commit' branch left the pm queue populated")

    def test_pm_queue_cleared_by_reject_changes(self, temp_repo):
        """reject_changes success path clears the project's pm entries
        (a rejected session's checkpoints are moot — D8 REV 2)."""
        handler = _activate(_make_handler(), temp_repo)
        handler.start_review("testproject")
        assert _wait_until(
            lambda: handler.get_state("testproject").checkpoint_sha), "no checkpoint"
        assert handler.pending_count("testproject", "pm") == 1

        handler.reject_changes("testproject", "not good")
        assert _wait_until(
            lambda: handler.get_state("testproject").checkpoint_sha is None), (
            "reject never resolved the session")
        assert handler.pending_count("testproject", "pm") == 0, (
            "BUG#4: reject_changes left the pm queue populated")

    def test_accept_changes_commit_has_pm_trailer(self, temp_repo):
        """BUG#5 / GAP-4: the accept_changes commit carries 'Agent: pm'."""
        import git as gitpython
        handler = _activate(_make_handler(), temp_repo)
        handler.start_review("testproject")
        assert _wait_until(
            lambda: handler.get_state("testproject").checkpoint_sha), "no checkpoint"

        with open(os.path.join(temp_repo, "pm_edit2.txt"), "w") as f:
            f.write("pm work\n")
        handler.accept_changes("testproject", "approved")
        assert _wait_until(
            lambda: handler.get_state("testproject").checkpoint_sha is None), (
            "accept never resolved the session")

        body = gitpython.Repo(temp_repo).git.log("--format=%B", "-1")
        assert "Agent: pm" in body.splitlines(), (
            f"BUG#5: accept commit missing pm trailer: {body!r}")


# ── SP2 fix round 2 (Finding A: D3 REV 4 serialization; Finding B: D8 REV 3) ─

def _make_counting_stage(counter, real_stage, delay=0.15, only_path=None):
    """Pass-through stage_all that records the MAX concurrent callers.

    only_path: when set, ONLY calls for that exact project path are counted.
    Previous tests' internal _do daemons can outlive their patch window and
    land in the NEXT test's mock — counting them would fake max>1 (found in
    fix round 3: deterministic full-file-only failure). Path filtering makes
    the counter immune: every test uses its own tmpdir repo."""
    def _wrapped(project_path):
        counted = only_path is None or os.path.realpath(project_path) == only_path
        if counted:
            counter["cur"] += 1
            counter["max"] = max(counter["max"], counter["cur"])
        time.sleep(delay)  # widen the race window (probe stress technique)
        try:
            return real_stage(project_path)
        finally:
            if counted:
                counter["cur"] -= 1
    return _wrapped


class TestCrossInvocationSerialization:
    """D3 REV 4 (fix round 2, Finding A): concurrent PUBLIC accept calls
    (double accept_all_queues; accept_all + per-agent) must never overlap
    their stage/commit sections on one repo."""

    def _two_agents_one_dirty(self, handler, temp_repo):
        with open(os.path.join(temp_repo, "dbg_work.txt"), "w") as f:
            f.write("debugger work\n")
        handler._enqueue("testproject", "special:coder", _entry(
            "special:coder", "a" * 40, temp_repo))
        handler._enqueue("testproject", "special:debugger", _entry(
            "special:debugger", "b" * 40, temp_repo))

    def test_double_accept_all_no_race(self, temp_repo):
        """Two concurrent accept_all_queues calls: both queues drain, no
        PARTIAL card, and stage_all NEVER overlaps (max concurrency == 1)."""
        import threading as th

        import git as gitpython
        commits_before = len(list(gitpython.Repo(temp_repo).iter_commits()))

        captured = []
        handler = _activate(_make_handler(on_feed_card=captured.append), temp_repo)
        self._two_agents_one_dirty(handler, temp_repo)

        from utils import git_ops as real_ops
        counter = {"cur": 0, "max": 0}
        commits = []
        real_commit = real_ops.commit

        def _recording_commit(project_path, message, **kwargs):
            result = real_commit(project_path, message, **kwargs)
            commits.append((message, result))
            return result

        with patch("ui.handlers.review_handler.git_ops") as mock_git:
            mock_git.stage_all.side_effect = _make_counting_stage(
                counter, real_ops.stage_all,
                only_path=os.path.realpath(temp_repo))
            mock_git.commit.side_effect = _recording_commit
            t1 = th.Thread(target=handler.accept_all_queues, args=("testproject",))
            t2 = th.Thread(target=handler.accept_all_queues, args=("testproject",))
            t1.start(); t2.start(); t1.join(); t2.join()
            assert _wait_until(lambda: handler.agents_with_pending("testproject") in ([], ["pm"])), (
                f"queues did not drain: {handler.agents_with_pending('testproject')}")

        titles = [c.title for c in captured]
        assert counter["max"] == 1, (
            f"D3 REV 4: stage_all overlapped on one repo (max={counter['max']})")
        assert not any("PARTIAL" in t for t in titles), (
            f"PARTIAL cards under concurrent accept: {titles}")
        assert handler.pending_count("testproject", "special:coder") == 0
        assert handler.pending_count("testproject", "special:debugger") == 0
        # Exactly one real commit (the FIRST-queued agent's loop hits the
        # dirty tree; the second pass finds it clean and drains via REV 3).
        # The commit's files belong to the OTHER agent's edit — that shared-
        # root cross-attribution is registered BUG C (pre-existing, bounded
        # by the D2 superset property, explicitly out of scope this round);
        # this test pins the serialization, not file attribution.
        made = [m for m in commits if m[1].success]
        assert len(made) == 1, (
            f"expected exactly 1 real commit, got {len(made)}: "
            f"{[m[0][:50] for m in commits]}")
        assert "Agent: special:coder" in gitpython.Repo(temp_repo).git.log(
            "--format=%B", "-1").splitlines()
        assert commits_before + 1 == len(list(gitpython.Repo(temp_repo).iter_commits()))

    def test_accept_all_plus_per_agent_no_race(self, temp_repo):
        """accept_all_queues racing accept_agent_queue(other agent): same
        serialization asserts (probe shape: accept_all + per-agent)."""
        import threading as th

        captured = []
        handler = _activate(_make_handler(on_feed_card=captured.append), temp_repo)
        self._two_agents_one_dirty(handler, temp_repo)

        from utils import git_ops as real_ops
        counter = {"cur": 0, "max": 0}

        with patch("ui.handlers.review_handler.git_ops") as mock_git:
            mock_git.stage_all.side_effect = _make_counting_stage(
                counter, real_ops.stage_all,
                only_path=os.path.realpath(temp_repo))
            mock_git.commit.side_effect = real_ops.commit
            t1 = th.Thread(target=handler.accept_all_queues, args=("testproject",))
            t2 = th.Thread(
                target=handler.accept_agent_queue,
                args=("special:debugger", "testproject"))
            t1.start(); t2.start(); t1.join(); t2.join()
            assert _wait_until(lambda: handler.agents_with_pending("testproject") in ([], ["pm"])), (
                f"queues did not drain: {handler.agents_with_pending('testproject')}")

        titles = [c.title for c in captured]
        assert counter["max"] == 1, (
            f"D3 REV 4: stage_all overlapped (max={counter['max']})")
        assert not any("PARTIAL" in t for t in titles), (
            f"PARTIAL cards under mixed concurrent accept: {titles}")
        assert handler.pending_count("testproject", "special:coder") == 0
        assert handler.pending_count("testproject", "special:debugger") == 0


class TestDiffReadErrorDrainsPm:
    """D8 REV 3 (fix round 2, Finding B): EVERY session-resolving exit drains
    pm — including accept_changes' diff-read-error branch."""

    def test_diff_read_error_drains_pm(self, temp_repo):
        """start_review → pm==1 → force the diff-read error (sys.modules git
        mock whose Repo.index.diff raises — the technique the existing
        feed-card tests use, HELD OPEN until the session resolves because
        accept_changes works on a daemon thread; branch proven via its error
        text; disclosed) → session resets AND pm drains.
        RED pre-fix: pm stays populated while the session resolves."""
        import sys
        handler = _activate(_make_handler(), temp_repo)
        handler.start_review("testproject")
        assert _wait_until(
            lambda: handler.get_state("testproject").checkpoint_sha), "no checkpoint"
        assert handler.pending_count("testproject", "pm") == 1, "fixture precondition"

        mock_git_module = MagicMock()
        mock_repo = MagicMock()
        mock_repo.index.diff.side_effect = Exception("corrupt index")
        mock_git_module.Repo.return_value = mock_repo
        with patch.dict(sys.modules, {"git": mock_git_module}):
            handler.accept_changes("testproject", "approved")
            # Hold the mock active until _do() finishes — otherwise the patch
            # can lose the race and the real import takes the clean branch.
            assert _wait_until(
                lambda: handler.get_state("testproject").checkpoint_sha is None), (
                "diff-read error never resolved the session")

        # Prove the ERROR branch ran (not the clean-tree branch, which
        # round 1 already drains — this assert makes the test self-verifying).
        text_calls = [str(c) for c in handler._on_display_text.call_args_list]
        assert any("Failed to read diff" in t and "corrupt index" in t
                   for t in text_calls), (
            f"error branch never ran — test proved nothing: {text_calls}")
        assert handler.pending_count("testproject", "pm") == 0, (
            "D8 REV 3: diff-read-error branch left pm entries orphaned")


# ── SP2 fix round 3 (BUG#1: REV 4a all-entry-point locking; BUG#2: stale
#    card emit outside the project lock; BUG#3: no-eviction ruling) ─────────

def _no_lock_errors(captured, text_calls):
    """No index.lock error anywhere in cards or display text."""
    blob = " ".join([c.title + " " + c.body for c in captured] + text_calls)
    assert "index.lock" not in blob and "index file" not in blob.lower(), (
        f"index.lock contention surfaced: {blob[:400]}")


class TestAllEntryPointsSerialized:
    """D3 REV 4a: EVERY root git critical section holds the per-project
    accept lock — accept_changes, start_review, reject_changes — not just
    _accept_agent_queue_sync. Nesting order: project_lock → queue_lock only."""

    def test_accept_all_vs_accept_changes_no_race(self, temp_repo):
        """accept_all_queues ∥ accept_changes: max concurrent stage_all == 1,
        no PARTIAL, no index.lock, both resolve."""
        import threading as th

        captured = []
        handler = _activate(_make_handler(on_feed_card=captured.append), temp_repo)
        # agent work (queue-driven) + PM work (accept_changes-driven)
        with open(os.path.join(temp_repo, "dbg_work.txt"), "w") as f:
            f.write("agent work\n")
        handler._enqueue("testproject", "special:debugger", _entry(
            "special:debugger", "b" * 40, temp_repo))
        with open(os.path.join(temp_repo, "pm_edit.txt"), "w") as f:
            f.write("pm work\n")

        from utils import git_ops as real_ops
        counter = {"cur": 0, "max": 0}
        with patch("ui.handlers.review_handler.git_ops") as mock_git:
            mock_git.stage_all.side_effect = _make_counting_stage(
                counter, real_ops.stage_all, delay=0.2,
                only_path=os.path.realpath(temp_repo))
            mock_git.commit.side_effect = real_ops.commit
            t1 = th.Thread(target=handler.accept_all_queues,
                           args=("testproject",))
            t2 = th.Thread(target=handler.accept_changes,
                           args=("testproject", "approved"))
            t1.start(); t2.start(); t1.join(); t2.join()
            assert _wait_until(
                lambda: handler.pending_count("testproject", "special:debugger") == 0), (
                "agent queue did not drain")
            assert _wait_until(
                lambda: handler.get_state("testproject").checkpoint_sha is None), (
                "accept_changes session did not resolve")

        titles = [c.title for c in captured if hasattr(c, "title")]
        assert counter["max"] == 1, (
            f"REV 4a: stage_all overlapped across entry points (max={counter['max']})")
        assert not any("PARTIAL" in t for t in titles)

    def test_double_accept_changes_no_race(self, temp_repo):
        """Two concurrent accept_changes on one project: max stage_all == 1,
        no index.lock error in any emitted text/card (auditor: ×13/20 pre-fix)."""
        import threading as th

        captured: list = []  # on_feed_card capture (audit SUGGESTION #6 —
        # the former getattr(handler, "_captured_cards", []) was always [],
        # making the docstring's card half vacuous)
        handler = _activate(_make_handler(on_feed_card=captured.append), temp_repo)
        with open(os.path.join(temp_repo, "pm_edit.txt"), "w") as f:
            f.write("pm work\n")

        from utils import git_ops as real_ops
        counter = {"cur": 0, "max": 0}
        with patch("ui.handlers.review_handler.git_ops") as mock_git:
            mock_git.stage_all.side_effect = _make_counting_stage(
                counter, real_ops.stage_all, delay=0.2,
                only_path=os.path.realpath(temp_repo))
            mock_git.commit.side_effect = real_ops.commit
            t1 = th.Thread(target=handler.accept_changes,
                           args=("testproject", "approved"))
            t2 = th.Thread(target=handler.accept_changes,
                           args=("testproject", "approved"))
            t1.start(); t2.start(); t1.join(); t2.join()
            assert _wait_until(
                lambda: handler.get_state("testproject").checkpoint_sha is None), (
                "sessions did not resolve")

        assert counter["max"] == 1, (
            f"REV 4a: double accept_changes overlapped (max={counter['max']})")
        text_calls = [str(c) for c in handler._on_display_text.call_args_list]
        _no_lock_errors(captured, text_calls)

    def test_start_review_vs_accept_all_no_race(self, temp_repo):
        """start_review ∥ accept_all_queues: max stage_all == 1, checkpoint
        lands AND the agent queue drains."""
        import threading as th

        handler = _activate(_make_handler(), temp_repo)
        with open(os.path.join(temp_repo, "dbg_work.txt"), "w") as f:
            f.write("agent work\n")
        handler._enqueue("testproject", "special:debugger", _entry(
            "special:debugger", "b" * 40, temp_repo))

        from utils import git_ops as real_ops
        counter = {"cur": 0, "max": 0}
        with patch("ui.handlers.review_handler.git_ops") as mock_git:
            mock_git.stage_all.side_effect = _make_counting_stage(
                counter, real_ops.stage_all, delay=0.2,
                only_path=os.path.realpath(temp_repo))
            mock_git.commit.side_effect = real_ops.commit
            t1 = th.Thread(target=handler.accept_all_queues,
                           args=("testproject",))
            t2 = th.Thread(target=handler.start_review, args=("testproject",))
            t1.start(); t2.start(); t1.join(); t2.join()
            assert _wait_until(
                lambda: handler.get_state("testproject").checkpoint_sha), (
                "start_review never produced a checkpoint")
            assert _wait_until(
                lambda: handler.pending_count("testproject", "special:debugger") == 0), (
                "agent queue did not drain")

        assert counter["max"] == 1, (
            f"REV 4a: start_review ∥ accept_all overlapped (max={counter['max']})")


class TestStaleCardEmitsUnlocked:
    """BUG#2 (fix round 3): the stale-drop card must emit AFTER the project
    lock is released — same pattern as the summary card."""

    def test_no_card_emitted_inside_project_lock(self, temp_repo):
        """Instrument _emit_feed_card with a lock-held probe: EVERY card
        (summary AND stale-drop) fires with the project lock UNLOCKED."""
        captured = []
        handler = _activate(_make_handler(on_feed_card=captured.append), temp_repo)
        lock_held_at_emit = []
        real_emit = handler._emit_feed_card

        def probing_emit(card_dict):
            lock = handler._project_lock_for("testproject")
            lock_held_at_emit.append(lock.locked())
            real_emit(card_dict)

        handler._emit_feed_card = probing_emit

        wt_dir = os.path.join(temp_repo, ".worktrees", "coder-9")
        os.makedirs(wt_dir)
        handler._enqueue("testproject", "special:coder", _entry(
            "special:coder", "9" * 40, os.path.realpath(wt_dir)))
        shutil.rmtree(wt_dir)  # stale before accept

        handler.accept_agent_queue("special:coder", "testproject")
        assert _wait_until(
            lambda: handler.pending_count("testproject", "special:coder") == 0), (
            "stale entry never dequeued")

        assert any("stale" in c.title.lower() for c in captured), (
            "stale card never emitted — fixture precondition failed")
        assert lock_held_at_emit, "no cards emitted at all"
        held = [h for h in lock_held_at_emit if h]
        assert not held, (
            f"BUG#2: {len(held)}/{len(lock_held_at_emit)} card(s) emitted while "
            f"holding the project lock (re-entrancy hazard)")


# ── SP2 fix round 4 (BUG#4: reject_file/revert_file_to_sha locks, REV 4b;
#    BUG#5: daemon-bleed hardening of the round-2 two-agent test) ────────────

def _make_counting_checkout(counter, real_checkout, delay=0.2, only_path=None):
    """Path-filtered max-concurrency recorder for checkout_paths-shaped calls
    (project_path is the first positional arg). Same discipline as
    _make_counting_stage — see that helper's docstring for the daemon-bleed
    rationale."""
    def _wrapped(project_path, *args, **kwargs):
        counted = only_path is None or os.path.realpath(project_path) == only_path
        if counted:
            counter["cur"] += 1
            counter["max"] = max(counter["max"], counter["cur"])
        time.sleep(delay)
        try:
            return real_checkout(project_path, *args, **kwargs)
        finally:
            if counted:
                counter["cur"] -= 1
    return _wrapped


def _counting_git_fn(counter, real_fn, delay=0.2, only_path=None):
    """Generic path-filtered max-concurrency wrapper (first arg = path)."""
    def _wrapped(project_path, *args, **kwargs):
        counted = only_path is None or os.path.realpath(project_path) == only_path
        if counted:
            counter["cur"] += 1
            counter["max"] = max(counter["max"], counter["cur"])
        time.sleep(delay)
        try:
            return real_fn(project_path, *args, **kwargs)
        finally:
            if counted:
                counter["cur"] -= 1
    return _wrapped


class TestCheckoutPathsSerialized:
    """D3 REV 4b: the two remaining mutating-git paths — reject_file and
    revert_file_to_sha — hold the per-project accept lock. (reject_changes
    was locked in round 3.)"""

    def _active_session_with_dirty_files(self, handler, temp_repo, files):
        """start_review (real checkpoint) + dirty tracked files to revert."""
        import git as gitpython
        handler.start_review("testproject")
        assert _wait_until(
            lambda: handler.get_state("testproject").checkpoint_sha), "no checkpoint"
        for name in files:
            with open(os.path.join(temp_repo, name), "a") as f:
                f.write("dirty change\n")
        return gitpython

    def test_reject_file_vs_reject_changes_no_race(self, temp_repo):
        """reject_file ∥ reject_changes: max concurrent checkout == 1
        (path-filtered), no index.lock in any emitted text, session resolves."""
        import threading as th

        captured = []
        handler = _activate(_make_handler(on_feed_card=captured.append), temp_repo)
        self._active_session_with_dirty_files(handler, temp_repo, ["hello.txt"])

        from utils import git_ops as real_ops
        counter = {"cur": 0, "max": 0}
        with patch("ui.handlers.review_handler.git_ops") as mock_git:
            mock_git.checkout_paths.side_effect = _make_counting_checkout(
                counter, real_ops.checkout_paths,
                only_path=os.path.realpath(temp_repo))
            t1 = th.Thread(target=handler.reject_file,
                           args=("testproject", "hello.txt"))
            t2 = th.Thread(target=handler.reject_changes,
                           args=("testproject", "bad code"))
            t1.start(); t2.start(); t1.join(); t2.join()
            assert _wait_until(
                lambda: handler.get_state("testproject").checkpoint_sha is None), (
                "reject_changes never resolved the session")

        assert counter["max"] == 1, (
            f"REV 4b: checkout overlapped reject_changes ∥ reject_file "
            f"(max={counter['max']})")
        text_calls = [str(c) for c in handler._on_display_text.call_args_list]
        _no_lock_errors(captured, text_calls)

    def test_reject_file_vs_reject_file_no_race(self, temp_repo):
        """Two concurrent reject_file on different tracked files: max checkout
        == 1, no index.lock (auditor RED: ×14/20 'index.lock: File exists')."""
        import threading as th

        import git as gitpython
        # second tracked file so both rejects revert real content
        repo = gitpython.Repo(temp_repo)
        with open(os.path.join(temp_repo, "second.txt"), "w") as f:
            f.write("v1\n")
        repo.index.add(["second.txt"])
        repo.index.commit("add second.txt")

        handler = _activate(_make_handler(), temp_repo)
        self._active_session_with_dirty_files(
            handler, temp_repo, ["hello.txt", "second.txt"])

        from utils import git_ops as real_ops
        counter = {"cur": 0, "max": 0}
        with patch("ui.handlers.review_handler.git_ops") as mock_git:
            mock_git.checkout_paths.side_effect = _make_counting_checkout(
                counter, real_ops.checkout_paths,
                only_path=os.path.realpath(temp_repo))
            t1 = th.Thread(target=handler.reject_file,
                           args=("testproject", "hello.txt"))
            t2 = th.Thread(target=handler.reject_file,
                           args=("testproject", "second.txt"))
            t1.start(); t2.start(); t1.join(); t2.join()

        assert counter["max"] == 1, (
            f"REV 4b: reject_file ∥ reject_file overlapped (max={counter['max']})")
        text_calls = [str(c) for c in handler._on_display_text.call_args_list]
        blob = " ".join(text_calls)
        assert "index.lock" not in blob, f"index.lock contention: {blob[:300]}"

    def test_revert_file_vs_batch_accept_no_race(self, temp_repo):
        """revert_file_to_sha ∥ accept_agent_queue (root entry): max
        concurrent MUTATING git (checkout + stage combined) == 1."""
        import threading as th

        handler = _activate(_make_handler(), temp_repo)
        self._active_session_with_dirty_files(handler, temp_repo, ["hello.txt"])
        with open(os.path.join(temp_repo, "agent_work.txt"), "w") as f:
            f.write("agent work\n")
        handler._enqueue("testproject", "special:debugger", _entry(
            "special:debugger", "d" * 40, temp_repo))

        from utils import git_ops as real_ops
        counter = {"cur": 0, "max": 0}
        only = os.path.realpath(temp_repo)
        with patch("ui.handlers.review_handler.git_ops") as mock_git:
            mock_git.checkout_paths.side_effect = _counting_git_fn(
                counter, real_ops.checkout_paths, only_path=only)
            mock_git.stage_all.side_effect = _counting_git_fn(
                counter, real_ops.stage_all, only_path=only)
            mock_git.commit.side_effect = real_ops.commit
            t1 = th.Thread(target=handler.revert_file_to_sha,
                           args=("testproject", "hello.txt",
                                 handler.get_state("testproject").checkpoint_sha))
            t2 = th.Thread(target=handler.accept_agent_queue,
                           args=("special:debugger", "testproject"))
            t1.start(); t2.start(); t1.join(); t2.join()
            assert _wait_until(
                lambda: handler.pending_count("testproject", "special:debugger") == 0), (
                "batch accept never drained")

        assert counter["max"] == 1, (
            f"REV 4b: revert ∥ batch-accept mutated git concurrently "
            f"(max={counter['max']})")
