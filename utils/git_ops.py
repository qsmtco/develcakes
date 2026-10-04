# utils/git_ops.py
# GitPython wrapper for all git operations.
# Returns structured GitResult objects. Never raises unhandled exceptions.
#
# New dependency: gitpython (pip install gitpython).
# Import: import git (as gitpython to avoid shadowing the module name).

import re
import git as gitpython
from dataclasses import dataclass
from typing import Optional


# LOW-9: path patterns for _safe_error
_PATH_PATTERNS = [
    re.compile(r"/home/[^/\s'\"\\]+"),
    re.compile(r"/Users/[^/\s'\"\\]+"),
    re.compile(r"C:\\\\Users\\\\[^\\\s'\"\\]+"),
    re.compile(r"/tmp/[^/\s'\"\\]+"),
]


def _safe_error(e: Exception, *, max_len: int = 200) -> str:
    """LOW-9: convert an exception to a safe, truncated error string.

    - Uses the exception class name + a sanitized message
    - Strips absolute paths (replaces with ~ or ...)
    - Truncates to max_len
    - Never includes the full repr/args
    """
    cls = type(e).__name__
    msg = str(e) or ""
    for pat in _PATH_PATTERNS:
        msg = pat.sub("~", msg)
    # Remove newlines and control chars
    msg = "".join(c for c in msg if c.isprintable() and c not in "\r\n\t")
    prefix = f"{cls}: "
    if len(msg) > max_len:
        msg = msg[: max(0, max_len - len(prefix) - 3)] + "..."
    return f"{prefix}{msg}" if msg else cls


# MED-11: Validate git commit SHA to prevent argument injection
_VALID_SHA_RE = re.compile(r"^(HEAD|[0-9a-fA-F]{4,40})$")


@dataclass
class GitResult:
    """Result of a git operation."""
    success: bool
    stdout: str          # textual output (diff, log, status)
    error: str           # error message if success=False
    sha: Optional[str] = None  # commit SHA when applicable


def is_repo(project_path: str) -> bool:
    """True if project_path contains a valid git repository."""
    try:
        gitpython.Repo(project_path)
        return True
    except Exception:
        return False


def init_repo(project_path: str) -> GitResult:
    """Initialize a git repo if not already one."""
    try:
        gitpython.Repo.init(project_path)
        return GitResult(success=True, stdout="", error="", sha=None)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)


def get_head_sha(project_path: str) -> GitResult:
    """Get current HEAD commit SHA."""
    try:
        repo = gitpython.Repo(project_path)
        if repo.head.is_detached:
            sha = repo.head.commit.hexsha
        else:
            sha = repo.head.commit.hexsha
        return GitResult(success=True, stdout=sha, error="", sha=sha)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)


def get_branch(project_path: str) -> GitResult:
    """Get current git branch name. Returns 'HEAD detached' if detached."""
    try:
        repo = gitpython.Repo(project_path)
        if repo.head.is_detached:
            return GitResult(success=True, stdout="(detached HEAD)", error="")
        return GitResult(success=True, stdout=repo.active_branch.name, error="")
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e))


def stage_all(project_path: str) -> GitResult:
    """Stage all changes (equivalent to git add -A)."""
    try:
        repo = gitpython.Repo(project_path)
        # Use git add -A via the git command to stage everything
        repo.git.add("-A")
        return GitResult(success=True, stdout="", error="", sha=None)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)


def commit(project_path: str, message: str, allow_empty: bool = False,
           agent_trailer: str | None = None) -> GitResult:
    """Commit staged changes. Returns SHA in result.sha.

    Args:
        project_path: Path to the git repository.
        message: Commit message.
        allow_empty: If True, allow empty commits (no staged changes). Use only
            for checkpoint markers where the SHA itself is the desired output.
            Default is False — refuse to create empty commits, since they
            pollute the git log with the captain's signature on nothing.
        agent_trailer: Optional session key appended as a literal
            "Agent: <key>" line in the commit message body. Must be a str
            that is a single line by Python's own definition — values
            containing any splitlines() boundary (\n, \r, \r\n, \v, \f,
            \x1c, \x1d, \x1e, \x85, U+2028, U+2029 — ten single-character
            boundaries plus the \r\n pair), NUL (\x00), the NON-boundary
            unit separator (\x1f), or empty-after-strip are REJECTED
            fail-closed (success=False, error explains the rejection) rather
            than silently dropping attribution. Non-str values are rejected
            the same way (never raise).

    Returns:
        GitResult. If allow_empty is False and the working tree is clean,
        returns success=False with error="nothing to commit (working tree clean)".
    """
    # SPEC-10 SP1 (D1): agent attribution trailer, fail-closed sanitization.
    # SPEC-10 SP1 fix round (BUG#1): gate on Python's OWN line-boundary
    # definition — str.splitlines() recognises ten single-character
    # boundaries plus the \r\n pair (\n \r \r\n \v \f \x1c \x1d \x1e \x85
    # U+2028 U+2029); a fixed reject-set let 8 of them forge additional
    # trailer lines for any Python-side body consumer.
    # NUL and \x1f are NOT splitlines boundaries, so they stay explicitly
    # checked (\x1f: file_log BUG #1 family; \x00: git plumbing hazard).
    # Empty-after-strip is rejected too: an "Agent: " line with no value is
    # ambiguous attribution. NEVER silently drop the trailer.
    # SPEC-10 SP1 fix round (BUG#2): the guard runs BEFORE any value access
    # and non-str values fail closed — this function's contract is to never
    # raise unhandled exceptions.
    if agent_trailer is not None:
        if not isinstance(agent_trailer, str):
            return GitResult(
                success=False,
                stdout="",
                error="agent_trailer rejected: must be a string",
                sha=None,
            )
        trailer_value = agent_trailer.strip()
        if (
            not trailer_value
            or len(trailer_value.splitlines()) != 1
            or any(c in trailer_value for c in ("\x00", "\x1f"))
        ):
            return GitResult(
                success=False,
                stdout="",
                error=(
                    "agent_trailer rejected: must be a non-empty single line "
                    "without control characters or line separators"
                ),
                sha=None,
            )
        message = f"{message}\n\nAgent: {trailer_value}"
    try:
        repo = gitpython.Repo(project_path)
        # Empty-check: refuse to commit if there's nothing staged (unless caller
        # explicitly allows it via allow_empty=True).
        # On a fresh repo with no commits, HEAD doesn't resolve — treat that as
        # "has changes" since the first commit is always valid.
        if not allow_empty:
            try:
                has_staged = bool(repo.index.diff("HEAD"))
            except Exception:
                # HEAD doesn't resolve (no commits yet) — allow the commit
                has_staged = True
            if not has_staged:
                return GitResult(
                    success=False, stdout="", error="nothing to commit (working tree clean)",
                    sha=None,
                )
        commit_obj = repo.index.commit(message)
        return GitResult(success=True, stdout=str(commit_obj.hexsha), error="", sha=commit_obj.hexsha)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)


def diff_against(project_path: str, sha: str) -> GitResult:
    """Full unified diff of commit sha vs current HEAD commit.
    Shows what changed between the checkpoint (sha) and now (HEAD)."""
    try:
        repo = gitpython.Repo(project_path)
        diff_text = repo.git.diff(sha, "HEAD")
        return GitResult(success=True, stdout=diff_text, error="", sha=repo.head.commit.hexsha)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)


def diff_stat_against(project_path: str, sha: str) -> GitResult:
    """Stat summary of diff (--stat output) between sha and HEAD."""
    try:
        repo = gitpython.Repo(project_path)
        stat_text = repo.git.diff(sha, "HEAD", "--stat")
        return GitResult(success=True, stdout=stat_text, error="", sha=repo.head.commit.hexsha)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)


def diff_file_against(project_path: str, sha: str, file_path: str) -> GitResult:
    """Diff for a single file between commit sha and HEAD."""
    try:
        repo = gitpython.Repo(project_path)
        diff_text = repo.git.diff(sha, "HEAD", "--", file_path)
        return GitResult(success=True, stdout=diff_text, error="", sha=repo.head.commit.hexsha)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)


def diff_file_against_working_tree(project_path: str, sha: str, file_path: str) -> GitResult:
    """Diff for a single file between commit sha and working tree.

    This is a 2-way diff: shows what changed between ``sha`` and the current
    working tree (including uncommitted staged AND unstaged changes).
    Unlike diff_file_against() which compares sha→HEAD only, this shows
    all local edits regardless of staging state.

    Equivalent to: git diff <sha> -- <file_path>

    Use during active review when agents have edited files but not committed.
    """
    # BUG #5: Guard against non-string sha (TypeError from re.match)
    if not isinstance(sha, str) or (sha != "HEAD" and not _VALID_SHA_RE.match(sha)):
        return GitResult(success=False, stdout="", error=f"Invalid git ref: {sha}", sha=None)

    try:
        repo = gitpython.Repo(project_path)
        diff_text = repo.git.diff(sha, "--", file_path)
        return GitResult(success=True, stdout=diff_text, error="", sha=repo.head.commit.hexsha)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)


def checkout_paths(project_path: str, sha: str, paths: list[str]) -> GitResult:
    """Revert file(s) to their state at sha. Equivalent to git checkout <sha> -- <paths>.

    MED-11: Validates sha before passing to git to prevent argument injection.
    """
    # BUG #5: Guard against non-string sha (TypeError from re.match)
    if not isinstance(sha, str) or (sha != "HEAD" and not _VALID_SHA_RE.match(sha)):
        return GitResult(success=False, stdout="", error=f"Invalid git ref: {sha}", sha=None)
    try:
        repo = gitpython.Repo(project_path)
        repo.git.checkout(sha, "--", *paths)
        return GitResult(success=True, stdout="", error="", sha=sha)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)


def log(project_path: str, count: int = 10) -> GitResult:
    """Recent commit log as text."""
    try:
        repo = gitpython.Repo(project_path)
        log_text = repo.git.log(f"-{count}", "--oneline", "--all")
        return GitResult(success=True, stdout=log_text, error="", sha=None)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)


def file_log(project_path: str, file_path: str, count: int = 20) -> GitResult:
    """Commit history for a single file.

    Returns: GitResult with stdout = lines of "SHA\\x1FISO_DATE\\x1FMESSAGE"
    Uses --follow to track renames. Caps at count entries.

    Format: fields separated by ASCII Unit Separator (\\x1f) to avoid
    collisions with pipe characters in commit messages.

    BUG #1: Emits lines via Python parsing of git.log output, rejecting
    subjects containing \\x1f (unsafe separator).
    BUG #3: count is coerced via int() with clear error for non-int; bool rejected.
    """
    # BUG #3: Reject bool (subclass of int) before coercion
    if isinstance(count, bool):
        return GitResult(
            success=False, stdout="",
            error="file_log: count must be an integer, got bool",
            sha=None,
        )
    # Coerce count to int — reject non-int types with clear error
    try:
        count = int(count)
    except (TypeError, ValueError):
        return GitResult(
            success=False, stdout="",
            error=f"file_log: count must be an integer, got {type(count).__name__}",
            sha=None,
        )
    # Clamp count to safe bounds (1..100)
    count = max(1, min(count, 100))

    try:
        repo = gitpython.Repo(project_path)
        # Use git.log with --follow and NUL separator.  %s is subject-only
        # (single line), so each commit produces exactly one output line:
        #   "SHA<NUL>ISO_DATE<NUL>SUBJECT"
        # NUL bytes cannot appear in commit messages, so this is safe.
        raw = repo.git.log(
            "--follow",
            f"-{count}",
            "--format=%H%x00%cI%x00%s",
            "--", file_path,
        )
        if not raw.strip():
            return GitResult(success=True, stdout="", error="", sha=None)

        lines = []
        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("\x00")
            if len(parts) != 3:
                continue  # malformed, skip
            commit_sha, date, subject = parts
            # BUG #1: Reject subjects containing \\x1f (unsafe separator)
            if "\x1f" in subject:
                return GitResult(
                    success=False, stdout="",
                    error=f"file_log: commit {commit_sha[:8]} subject contains unsafe \\x1f character",
                    sha=None,
                )
            lines.append(f"{commit_sha}\x1f{date}\x1f{subject}")
        return GitResult(success=True, stdout="\n".join(lines), error="", sha=None)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)


def push(project_path: str, remote: str = "origin", branch: str = "main") -> GitResult:
    """Push to remote."""
    try:
        repo = gitpython.Repo(project_path)
        origin = repo.remote(remote)
        info = origin.push(refspec=f"HEAD:{branch}")
        # Check if push succeeded
        push_info = info[0]
        if push_info.flags & push_info.ERROR:
            return GitResult(success=False, stdout="", error=f"Push rejected: {push_info.summary}", sha=None)
        return GitResult(success=True, stdout=push_info.summary, error="", sha=None)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)


def diff_working_tree(project_path: str, file_path: str | None = None) -> GitResult:
    """Diff of working tree against HEAD (unstaged + staged changes).

    Equivalent to: git diff HEAD -- [file_path]
    If file_path is None, diffs all files.
    """
    try:
        repo = gitpython.Repo(project_path)
        args = ["HEAD"]
        if file_path:
            args.extend(["--", file_path])
        diff_text = repo.git.diff(*args)
        return GitResult(success=True, stdout=diff_text, error="", sha=None)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)


def status(project_path: str) -> GitResult:
    """git status --porcelain output."""
    try:
        repo = gitpython.Repo(project_path)
        status_text = repo.git.status("--porcelain")
        return GitResult(success=True, stdout=status_text, error="", sha=None)
    except Exception as e:
        return GitResult(success=False, stdout="", error=_safe_error(e), sha=None)


def status_porcelain(project_path: str) -> dict[str, str]:
    """Returns parsed git status map: {rel_path: status_code}.

    Uses subprocess (not GitPython) so it works when project_path is a
    subdirectory of a git repo — git CLI walks up to find .git.

    status_code is 2-char porcelain string per `git status --porcelain` format.
    See git-status(1) for format details.

    Handles rename lines ('R  old -> new') by emitting the destination path.
    Returns empty dict on any error (caught and suppressed).
    """
    import subprocess as _subprocess
    try:
        result = _subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=project_path,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {}
        raw = result.stdout
        status_map: dict[str, str] = {}
        for line in raw.splitlines():
            # Minimum valid porcelain line: 'XY path' (4 chars: 2 status + 1 space + 1 path)
            if len(line) < 4:
                continue
            status_code = line[:2]
            rest = line[3:]  # skip space separator at index 2
            # Handle rename/copy format: 'R  old_path -> new_path' or 'C  old_path -> new_path'
            # Check BOTH status positions — index column (R_, C_) and worktree column (_R, _C) (BUG #25)
            # BUG #13: status_porcelain using subprocess handles subdirs of git repos
            if (status_code[0] in ('R', 'C') or
                (len(status_code) >= 2 and status_code[1] in ('R', 'C'))):
                if ' -> ' in rest:
                    # 'old_path -> new_path' — take the right side
                    rest = rest.split(' -> ', 1)[1]
            # Normalize: strip trailing slash so directory keys match
            # os.path.relpath() output (BUG #3 — inter-layer consistency)
            rest = rest.rstrip('/')
            status_map[rest] = status_code
        return status_map
    except Exception:
        return {}
