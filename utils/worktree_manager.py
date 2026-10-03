# utils/worktree_manager.py
# One git worktree per writer agent IN A GIVEN REPO (SPEC-09 SP2, brief v3).
#
# Ruling BLOCKING-3(a): PROJECT-PARAMETERIZED — the manager is constructed
# with the ACTIVE PROJECT's repo path; worktrees land at
# <repo>/.worktrees/<worktree_id>. Self-host special case: when repo_path IS
# the running app's repo, worktrees sit under the SP0 gate's
# <_APP_ROOT>/.worktrees and pass _is_app_worktree (direct realpath
# children); foreign projects' worktrees are ordinary git worktrees with no
# app-venv benefits (by design — pinned both ways by tests).
#
# SP0 obligation (re-audit BUG#14, discharged here): every path this module
# returns is os.path.realpath'd — never a symlink into .worktrees.
#
# Architecture: pure Python + GitPython + os. No GTK, no agent/ imports
# (utils layer; mirrors git_ops.py's `import git as gitpython` style).
#
# Probe-informed (Debugger, 2026-10-02):
#   J — [a-zA-Z0-9._-]+ admits '.'/'..' (os.path.join escapes) → explicit
#       dot-segment rejection (RULING-3, load-bearing).
#   L — revival after rm -rf: plain re-add is rc 128 'missing but already
#       registered'; re-add -b is rc 255 'branch exists' → prune FIRST,
#       then add with -b only when the branch is genuinely new.
#   M — GitPython 3.1.62 has NO Repo.worktrees → parse
#       `git worktree list --porcelain`.
#   K-D — .gitignore presence must be PER-LINE EXACT ('.worktrees-old/'
#       must not satisfy a substring check).
#   N — detached-HEAD worktrees: registration checks use MEMBERSHIP, not
#       truthiness (a detached worktree's branch value is '').

import logging
import os
import re
import threading

import git as gitpython

_logger = logging.getLogger(__name__)

WORKTREES_DIR_NAME = ".worktrees"

# Filesystem-id class (RULING-3 note: this class still contains '.', so the
# dot-segment check in _validate_agent_id is load-bearing, not redundant).
_AGENT_ID_RE = re.compile(r"[a-zA-Z0-9._-]+")

# .gitignore entry (per-line exact match accepts both common spellings).
_GITIGNORE_FORMS = (".worktrees/", ".worktrees")


class WorktreeError(RuntimeError):
    """A git worktree operation failed (stderr detail inside)."""


def _validate_agent_id(agent_id: str) -> str:
    """Validate a worktree id: [a-zA-Z0-9._-]+ AND no dot-segments.

    BLOCKING-1 (probe J): '..' normpaths OUT of .worktrees to the repo root;
    '.' maps to .worktrees itself — both refused at the boundary before any
    path is built. Separators, whitespace, NUL and '~' are refused for
    path/branch-name safety. Raises ValueError on any violation.
    """
    if not isinstance(agent_id, str) or not agent_id:
        raise ValueError("agent_id must be a non-empty string")
    if agent_id == "." or agent_id == ".." or ".." in agent_id:
        raise ValueError(f"agent_id must not contain dot-segments: {agent_id!r}")
    if not _AGENT_ID_RE.fullmatch(agent_id):
        raise ValueError(
            "agent_id must match [a-zA-Z0-9._-]+ "
            f"(no separators/whitespace/tilde), got: {agent_id!r}"
        )
    return agent_id


def is_worktree_of(project_path: str, candidate: str | None) -> bool:
    """True when *candidate* is a direct child of *project_path*'s .worktrees
    (realpath discipline on both sides — the SP0 gate's rule, parameterized
    by project). Used by the ARH lease-expiry reset to recognize stale
    worktree cwds; never raises."""
    if not project_path or not candidate:
        return False
    real_parent = os.path.realpath(
        os.path.join(project_path, WORKTREES_DIR_NAME)
    )
    real_child = os.path.realpath(os.path.abspath(candidate))
    try:
        return os.path.dirname(real_child) == real_parent
    except (ValueError, OSError):
        return False


class WorktreeManager:
    """One git worktree per writer agent in a given repo (the active project).

    A NON-GIT repo degrades to a DISABLED manager (brief v3 probe-corrected
    bullet: the ctor must not raise into the turn path) — ``enabled`` is
    False, ``ensure_worktree``/``path_for`` return None, ``list_worktrees``
    returns {}.

    Idempotent ensure; branch ``agent/<id>`` from HEAD at first ensure;
    re-ensure returns the existing path untouched (no branch switching).
    Revival after external deletion: prune the stale registration, then
    re-create reusing the existing branch when it exists (probe L).
    """

    def __init__(self, repo_path: str):
        if not isinstance(repo_path, str) or not repo_path.strip():
            raise ValueError("repo_path must be a non-empty string")
        # Realpath ONCE at the boundary; every computed path derives from the
        # normalized root (the SP0 discipline lives HERE).
        self._repo_path = os.path.realpath(os.path.abspath(repo_path))
        self._repo: gitpython.Repo | None = None
        try:
            self._repo = gitpython.Repo(self._repo_path)
        except (gitpython.InvalidGitRepositoryError, gitpython.NoSuchPathError):
            self._repo = None  # degraded/disabled — see class docstring
        self._worktrees_root = os.path.join(self._repo_path, WORKTREES_DIR_NAME)
        # Serializes ensure/remove within one manager (git worktree ops take
        # index locks; distinct ids contend harmlessly but serializing is
        # cheap and keeps the porcelain reads consistent with the writes).
        self._op_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """False when repo_path is not a git repo (manager degraded)."""
        return self._repo is not None

    # ── session-key derivation (RULING-2, probe J order) ────────────────────

    @staticmethod
    def worktree_id_for_session(session_key: str) -> str:
        """Derive + validate the filesystem-safe worktree id for a session
        key: ':' -> '-' (the LOW-2 filesystem sanitization),
        ``special:coder`` -> ``special-coder``; then class-validate and
        reject dot-segments (order matters: the class admits '.', so the
        explicit rejection is load-bearing).

        The lease holder remains the FULL session_key in work.json; only the
        filesystem/branch id is derived. Raises ValueError on unusable keys.
        """
        if not isinstance(session_key, str) or not session_key.strip():
            raise ValueError("session_key must be a non-empty string")
        worktree_id = session_key.strip().replace(":", "-")
        return _validate_agent_id(worktree_id)

    # ── .gitignore (MED-1, probe K-D) ───────────────────────────────────────

    def _gitignore_has_entry(self, content: str) -> bool:
        """PER-LINE EXACT check — a substring test sees '.worktrees-old/'
        and wrongly skips the append (probe K-D)."""
        return any(
            line.strip() in _GITIGNORE_FORMS for line in content.splitlines()
        )

    def _ensure_gitignore(self) -> None:
        """Ensure .gitignore carries the .worktrees entry (create/append;
        missing trailing newline repaired). Best-effort: a read-only repo
        must still be able to create worktrees, so failures log, never raise."""
        gitignore = os.path.join(self._repo_path, ".gitignore")
        try:
            existing = ""
            if os.path.isfile(gitignore):
                with open(gitignore, "r", encoding="utf-8", errors="replace") as f:
                    existing = f.read()
                if self._gitignore_has_entry(existing):
                    return
            with open(gitignore, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(_GITIGNORE_FORMS[0] + "\n")
        except OSError as e:
            _logger.warning(
                "worktree manager: could not update .gitignore at %s: %s",
                gitignore, e,
            )

    # ── paths ────────────────────────────────────────────────────────────────

    def _worktree_path(self, worktree_id: str) -> str:
        """Join + realpath (validates the id first — traversal refuses here)."""
        return os.path.realpath(
            os.path.join(self._worktrees_root, _validate_agent_id(worktree_id))
        )

    # ── git plumbing (probe M: porcelain, no Repo.worktrees) ────────────────

    def _registered_worktrees(self) -> dict[str, str]:
        """`git worktree list --porcelain` -> {realpath: branch-ref}.

        Values are the checked-out branch refs; a DETACHED worktree
        registers with '' (probe N — never truthiness-test the values;
        test membership). {} when the manager is disabled."""
        if self._repo is None:
            return {}
        try:
            out = self._repo.git.worktree("list", "--porcelain")
        except (gitpython.GitCommandError, ValueError) as e:
            _logger.warning(
                "worktree list failed at %s: %s", self._repo_path, e
            )
            return {}
        result: dict[str, str] = {}
        current: str | None = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                current = os.path.realpath(line[len("worktree "):].strip())
                result[current] = ""
            elif line.startswith("branch ") and current is not None:
                result[current] = line[len("branch "):].strip()
        return result

    def _branch_exists(self, repo: gitpython.Repo, branch: str) -> bool:
        return any(head.name == branch for head in repo.heads)

    def list_worktrees(self) -> dict[str, str]:
        """Managed worktrees: worktree_id -> realpath'd path. Managed =
        git-registered paths that are DIRECT children of this repo's
        .worktrees (the manager's namespace — nothing else)."""
        real_root = os.path.realpath(self._worktrees_root)
        out: dict[str, str] = {}
        for wt_path in self._registered_worktrees():
            entry = os.path.basename(wt_path)
            if os.path.dirname(wt_path) == real_root and _AGENT_ID_RE.fullmatch(
                entry
            ):
                out[entry] = wt_path
        return out

    def path_for(self, worktree_id: str) -> str | None:
        """REAL path of the worktree, or None when absent (dir gone or not
        registered — a detached HEAD still counts: MEMBERSHIP, not
        truthiness, probe N)."""
        if self._repo is None:
            return None
        try:
            path = self._worktree_path(worktree_id)
        except ValueError:
            return None
        if os.path.isdir(path) and path in self._registered_worktrees():
            return path
        return None

    # ── mutations ────────────────────────────────────────────────────────────

    def ensure_worktree(self, worktree_id: str) -> str | None:
        """Create <repo>/.worktrees/<id> if missing (branch agent/<id> from
        HEAD); return the REALPATH'd path; idempotent (check-before-add — a
        second `worktree add` on an existing worktree is rc 128).

        Revival (probe L): prune stale registrations FIRST, then `add`
        WITHOUT -b when agent/<id> already exists, WITH -b only for a truly
        new branch.

        Returns None when the manager is disabled (non-git repo). Raises
        WorktreeError on git failure (stderr detail inside), ValueError on a
        malformed id."""
        worktree_id = _validate_agent_id(worktree_id)
        if self._repo is None:
            return None  # disabled manager — caller falls back to the repo
        path = self._worktree_path(worktree_id)
        branch = f"agent/{worktree_id}"
        with self._op_lock:
            registered = self._registered_worktrees()
            if os.path.isdir(path) and path in registered:
                return path  # idempotent re-ensure: present + registered
            if not os.path.isdir(self._worktrees_root):
                try:
                    os.makedirs(self._worktrees_root, exist_ok=True)
                except OSError as e:
                    # LOW-7: .worktrees exists as a FILE -> clean error
                    raise WorktreeError(
                        f"cannot create worktrees root "
                        f"{self._worktrees_root}: {e}"
                    ) from e
            self._ensure_gitignore()
            try:
                self._repo.git.worktree("prune")  # stale registrations first
                if self._branch_exists(self._repo, branch):
                    self._repo.git.worktree("add", path, branch)
                else:
                    self._repo.git.worktree("add", "-b", branch, path)
            except gitpython.GitCommandError as e:
                raise WorktreeError(
                    f"git worktree add failed for {worktree_id!r}: {e}"
                ) from e
        return path

    def remove_worktree(self, worktree_id: str) -> None:
        """`git worktree remove --force`; NO-OP when absent (dir gone or not
        registered — membership, not truthiness). The branch is left in
        place (recoverable uncommitted work is a removal of the CHECKOUT,
        not the history)."""
        worktree_id = _validate_agent_id(worktree_id)
        if self._repo is None:
            return
        path = self._worktree_path(worktree_id)
        with self._op_lock:
            if not (
                os.path.isdir(path) and path in self._registered_worktrees()
            ):
                return  # absent = no-op
            try:
                self._repo.git.worktree("remove", "--force", path)
            except gitpython.GitCommandError as e:
                raise WorktreeError(
                    f"git worktree remove failed for {worktree_id!r}: {e}"
                ) from e
