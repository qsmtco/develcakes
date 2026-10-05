# agent/tools.py
# Tool definitions and execution for the agent runtime.
#
# Manifest:
#   - File operations: only paths within project_path (sandboxed)
#   - Exec: requires PM approval via registered callback
#   - Web: httpx calls to Brave Search API and arbitrary URLs
#   - No GTK, no LLM calls
#
# Architecture: agent/ is the only package that knows tool capabilities.
# All tools are data + pure functions. No GTK, no state, no network at import time.

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from utils.config import get_env  # SPEC-11 SP2: renamed env family (D2)

# ── Dataclasses ────────────────────────────────────────────────────────────────


@dataclass
class ToolDefinition:
    """Definition of a tool available to agents."""
    name: str
    description: str
    parameters: dict          # JSON Schema for LLM function-calling
    requires_approval: bool = False


@dataclass
class ToolResult:
    """Result of executing a tool."""
    success: bool
    output: str = ""              # primary text output shown to the model
    error: str | None = None       # error message (non-empty on failure)
    duration_ms: int = 0
    stdout: str = ""              # raw stdout from exec_command
    stderr: str = ""              # raw stderr from exec_command
    exit_code: int | None = None   # raw exit code from exec_command

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
        }


# ── Internal state ──────────────────────────────────────────────────────────────

# Approval callback: called before exec_command. Returns True to allow, False to deny.
_approval_callback: Callable[[str, str, dict], bool] | None = None
# "special:coder" or "special:debugger", tool_name, {"command": "...", "cwd": "..."}


def set_approval_callback(cb: Callable[[str, str, dict], bool] | None) -> None:
    """Register a callback that asks the PM for exec_command approval.

    Args:
        cb: callback(session_key, tool_name, arguments) -> bool.
            True = allow execution.
            False = deny (tool returns error result).
            None = unregister (exec_command will always be denied without a callback).
    """
    global _approval_callback
    _approval_callback = cb


def _get_approval(session_key: str, tool_name: str, arguments: dict, *, approval_callback=None) -> bool:
    """Return True if exec_command is approved, False otherwise.

    MED-1: If a per-call callback is provided, it takes precedence over the global.
    """
    cb = approval_callback or _approval_callback
    if cb is None:
        return False
    try:
        return cb(session_key, tool_name, arguments)
    except Exception:
        return False


# ── Process registry (SPEC-09 SP3 stop-all) ───────────────────────────────────
#
# session_key → list of live Popen handles from _exec_command. Stop-all
# (runtime.stop_all → cancel_all_processes) group-kills these so a runaway
# shell command cannot outlive its cancelled turn. Every handle in the
# registry was started with start_new_session=True, making proc.pid a
# PROCESS-GROUP leader — os.killpg reaches the whole tree (shells, sleeps,
# compilers), which plain proc.terminate() (the shell only) would not.

_PROCESS_REGISTRY: dict[str, list[subprocess.Popen]] = {}
_PROCESS_REGISTRY_LOCK = threading.Lock()

# SIGTERM → grace → SIGKILL escalation window (seconds).
_KILL_ESCALATION_GRACE_SEC = 2.0


def _group_alive(pgid: int) -> bool:
    """True when the process GROUP still has a live member (killpg 0-probe).

    SP3 fix round (BUG#1): group liveness is probed via killpg(pgid, 0) —
    NOT the leader's reaping. A process group OUTLIVES its leader: any
    member (or none) may remain, so the leader's exit proves nothing about
    the group. ProcessLookupError → no member remains → False.
    """
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Group exists but is not ours to signal — conservatively "alive".
        return True


def _group_kill(proc: subprocess.Popen) -> bool:
    """SIGTERM → grace → SIGKILL one process GROUP; returns confirmed-dead.

    SP3 fix round (BUG#1): escalation is gated on GROUP liveness (killpg
    0-probe), never on the leader's reaping — a group outlives its leader,
    so "the shell exited on SIGTERM" (probe C: bash -c 'trap "" TERM;
    sleep & wait' under a TERM-honoring outer sh) used to skip escalation
    and orphan the TERM-immune child holding the exec pipe. ProcessLookupError
    at any probe means no member remains — that is success. Permission/state
    errors fall through to the next escalation step rather than aborting
    stop-all; the group that cannot be signalled is almost always already
    gone (PermissionError on the 0-probe alone means it is not ours —
    conservatively reported as unkillable, see cancel_all_processes).

    SP3 fix round 2 (BUG#1): proc.poll() runs at the TOP of every wait
    iteration, BEFORE the _group_alive probe. An unreaped ZOMBIE leader
    keeps killpg(pgid, 0) succeeding, so without an in-window reap both
    2s windows spun on a genuinely-empty group: every exec timeout ran
    ~4s long (auditor: 5.01s vs 1.00s) and cancel_all reported FALSE
    unkillable 12/12 (zrate probe) — the no-reaper windows (exec timeout
    path; registry→communicate gap) have no communicate() reaper running.
    poll() reaps; the zombie becomes waitable-dead; killpg then sees the
    true (empty) group. Auditor's causation probe: 5.01s → 0.05s.
    """
    pgid = proc.pid  # start_new_session=True → pid IS the group leader id
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True  # group already gone — done
    except (PermissionError, OSError):
        # Cannot signal this group (permissions / reaped / exotic fs).
        # Fall through to the probe; SIGKILL attempt follows.
        pass
    deadline = time.monotonic() + _KILL_ESCALATION_GRACE_SEC
    while time.monotonic() < deadline:
        proc.poll()  # reap the leader first — a zombie holds the probe truthy
        if not _group_alive(pgid):
            # Group confirmed dead (leader may have been reaped mid-wait).
            try:
                proc.wait(timeout=0)  # reap if we are the parent; else ignore
            except (subprocess.TimeoutExpired, ChildProcessError, OSError, ValueError):
                pass
            return True
        time.sleep(0.05)

    # Still alive after the SIGTERM grace → SIGKILL the GROUP (probe B:
    # killpg reaches ORPHANED members whose leader is gone).
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass  # gone between probe and signal, or unsignallable — probe decides
    deadline = time.monotonic() + _KILL_ESCALATION_GRACE_SEC
    while time.monotonic() < deadline:
        proc.poll()  # reap-before-probe, same zombie hazard as the SIGTERM window
        if not _group_alive(pgid):
            try:
                proc.wait(timeout=0)  # reap if we are the parent; else ignore
            except (subprocess.TimeoutExpired, ChildProcessError, OSError, ValueError):
                pass
            return True
        time.sleep(0.05)
    return False  # group SURVIVED SIGKILL — hostile-process territory


def cancel_all_processes(session_key: str | None = None) -> tuple[int, int]:
    """Group-kill registered processes (SIGTERM → 2s → SIGKILL). All sessions
    if None. Returns (killed, unkillable) — SP3 fix round BUG#3: only
    group-confirmed-dead targets count as killed; survivors after SIGKILL
    surface as unkillable (the stop-all card reports them; a group that
    survives SIGKILL is a hostile-process situation the PM must SEE).
    Escalation per process-group (start_new_session).

    SPEC-09 SP3: the stop-all kill path. Registered handles are removed from
    the registry under the lock; SP3 fix round (BUG#1): the former
    `poll() is None` skip is GONE — a leader that already exited does not
    prove its group is gone (probe I: `sleep 300 & echo started` leaves a
    lingering orphan), so every registered group is probed+escalated
    unconditionally; the dead-group case raises ProcessLookupError inside
    _group_kill and resolves True harmlessly.
    """
    with _PROCESS_REGISTRY_LOCK:
        if session_key is None:
            keys = list(_PROCESS_REGISTRY.keys())
        else:
            keys = [session_key] if session_key in _PROCESS_REGISTRY else []
        targets: list[tuple[str, subprocess.Popen]] = [
            (sk, proc) for sk in keys for proc in _PROCESS_REGISTRY.get(sk, [])
        ]
        for sk in keys:
            _PROCESS_REGISTRY.pop(sk, None)

    killed = 0
    unkillable = 0
    for _sk, proc in targets:
        if _group_kill(proc):
            killed += 1
        else:
            unkillable += 1
    return killed, unkillable


# ── Path sandbox ───────────────────────────────────────────────────────────────

MAX_READ_SIZE = 50 * 1024       # 50 KB
MAX_WRITE_SIZE = 2 * 1024 * 1024  # 2 MB
MAX_EXEC_OUTPUT = 100 * 1024     # 100 KB

# Hardcoded blocklist — always denied even with approval.
# Commands matching these patterns (substring match) are rejected before
# the approval callback fires. This catches catastrophic cases.
_BLOCKLIST = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=/dev/zero of=/dev/sda",
    "dd if=/dev/zero of=/dev/nvme",
    "cat /dev/sda",
    "> /dev/sda",
    "chattr -i",
    "wipefs",
    ":(){:|:&};:",    # fork bomb
]

# HIGH-1: Sensitive path patterns — write/edit to these paths requires PM approval
# even if the project sandbox would technically allow them.
# Patterns use glob-style matching (fnmatch) relative to project root.
# HIGH-1: Sensitive path patterns — write/edit to these requires PM approval.
# Per security audit (HIGH-1) and Q5 user decision (2026-06-18).
# Format: (kind, pattern) where kind is "prefix" or "glob".
_SENSITIVE_PATH_PATTERNS: tuple[tuple[str, str], ...] = (
    # Prefix matches — path components sensitive at any depth
    ("prefix", ".git/"),
    ("prefix", ".crabcakes/"),
    ("prefix", ".github/"),
    # Basename glob matches
    ("glob", "Makefile"),
    ("glob", "*.toml"),
    ("glob", "*.yml"),
    ("glob", "*.yaml"),
    ("glob", "*hook*"),
    ("glob", "*venv*"),
    # Leading-dot files handled separately in is_sensitive_path below.
)


def is_sensitive_path(path: str) -> bool:
    """Return True if `path` requires PM approval before write/edit.

    HIGH-1 (per security audit): gates writes to build/CI infrastructure
    (.git/, .crabcakes/, .github/, Makefile, *.toml, *.yml, *.yaml,
    *hook*, *venv*) and leading-dot files in any directory.
    These files can affect the enforcement pipeline, shell environment, or
    build/test execution graph. Tampering achieves RCE or supply-chain compromise.
    Per audit and Q5 user decision (2026-06-18).
    """
    import fnmatch
    if not path:
        return False
    # Normalize to forward-slash
    norm = path.replace("\\", "/")
    basename = norm.split("/")[-1]
    if not basename:
        return False
    for kind, pattern in _SENSITIVE_PATH_PATTERNS:
        if kind == "prefix" and norm.startswith(pattern):
            return True
        if kind == "glob":
            # For *venv* and *hook*, check full path since these names
            # can appear in directory components (e.g. .venv/bin/activate).
            # Use substring containment (not fnmatch) so "post-receive" matches "*hook*".
            if pattern in ("*venv*", "*hook*"):
                if pattern.replace("*", "") in norm:
                    return True
            elif fnmatch.fnmatch(basename, pattern):
                return True
    # Leading-dot files in any directory (but not . or ..)
    if basename.startswith(".") and basename not in (".", ".."):
        return True
    return False


def _is_blocked(command: str) -> bool:
    """Return True if command matches the hardcoded blocklist."""
    lower = command.lower()
    for pattern in _BLOCKLIST:
        if pattern.lower() in lower:
            return True
    return False


def _resolve_project_path(path: str, project_path: str) -> str | None:
    """
    Resolve a path relative to project_path and verify it stays within the sandbox.

    Returns the absolute resolved path if within sandbox, or None if it escapes.
    """
    if os.path.isabs(path):
        # Absolute path — must be under project_path
        resolved = os.path.realpath(path)
        project_real = os.path.realpath(project_path)
        try:
            common = os.path.commonpath([resolved, project_real])
            if common == project_real:
                return resolved
        except ValueError:
            # Different drives on Windows — commonpath raises
            pass
        return None
    else:
        # Relative path — resolved relative to project_path
        resolved = os.path.realpath(os.path.join(project_path, path))
        project_real = os.path.realpath(project_path)
        try:
            common = os.path.commonpath([resolved, project_real])
            if common == project_real:
                return resolved
        except ValueError:
            pass
        return None


# ── Tool implementations ──────────────────────────────────────────────────────


def _read_file(path: str, project_path: str, offset: int | None = None, limit: int | None = None) -> ToolResult:
    """Read a file relative to project_path."""
    resolved = _resolve_project_path(path, project_path)
    if resolved is None:
        return ToolResult(success=False, error=f"Path escapes project sandbox: {path}")

    if not os.path.isfile(resolved):
        return ToolResult(success=False, error=f"Not a file: {path}")

    try:
        size = os.path.getsize(resolved)
        if size > 50 * 1024 * 1024:
            return ToolResult(success=False, error=f"File too large (>50MB): {path}")

        # Binary check
        with open(resolved, "rb") as f:
            header = f.read(512)
        if b"\x00" in header:
            return ToolResult(success=False, error=f"Binary file (not readable as text): {path}")

        # Read with optional offset/limit using binary mode
        # (to correctly handle byte offsets in UTF-8 multi-byte files)
        with open(resolved, "rb") as f:
            if offset is not None:
                f.seek(offset)
            raw = f.read(limit)
        content = raw.decode("utf-8", errors="replace")

        if len(content) >= MAX_READ_SIZE:
            content += f"\n[... truncated at {MAX_READ_SIZE} bytes ...]"

        return ToolResult(success=True, output=content)

    except PermissionError:
        return ToolResult(success=False, error=f"Permission denied: {path}")
    except UnicodeDecodeError:
        return ToolResult(success=False, error=f"File is not valid UTF-8: {path}")
    except OSError as e:
        return ToolResult(success=False, error=f"Cannot read {path}: {e}")


def _write_file(path: str, content: str, project_path: str, session_key: str = "", approval_callback=None) -> ToolResult:
    """Write content to a file relative to project_path."""
    resolved = _resolve_project_path(path, project_path)
    if resolved is None:
        return ToolResult(success=False, error=f"Path escapes project sandbox: {path}")

    # HIGH-1: Sensitive-path guard — require PM approval before writing
    if is_sensitive_path(path):
        approved = _get_approval(session_key, "write_file", {"path": path}, approval_callback=approval_callback)
        if not approved:
            return ToolResult(
                success=False,
                error=(
                    f"write_file blocked: {path} is a sensitive path\n"
                    f"(credential, secret, SSH key, or cloud config file).\n"
                    f"PM approval is required before writing to this file."
                ),
            )

    # Create parent directories if needed
    parent = os.path.dirname(resolved)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return ToolResult(success=False, error=f"Cannot create directory {os.path.dirname(path)}: {e}")

    try:
        byte_count = len(content.encode("utf-8"))
        if byte_count > MAX_WRITE_SIZE:
            return ToolResult(success=False, error=f"Content too large ({byte_count} bytes, max {MAX_WRITE_SIZE})")
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
        return ToolResult(success=True, output=f"OK — wrote {byte_count} bytes to {path}")
    except PermissionError:
        return ToolResult(success=False, error=f"Permission denied: {path}")
    except OSError as e:
        return ToolResult(success=False, error=f"Cannot write {path}: {e}")


def _edit_file(path: str, old_text: str, new_text: str, project_path: str, session_key: str = "", approval_callback=None) -> ToolResult:
    """Replace exact old_text with new_text in a file within project_path.

    Both old_text and new_text are matched exactly — no regex, no fuzzy matching.
    old_text must be unique in the file; otherwise the edit is rejected.
    Falls back to a full file rewrite if the file has only one occurrence.
    """
    resolved = _resolve_project_path(path, project_path)
    if resolved is None:
        return ToolResult(success=False, error=f"Path escapes project sandbox: {path}")

    # HIGH-1: Sensitive-path guard — require PM approval before editing
    if is_sensitive_path(path):
        approved = _get_approval(session_key, "edit_file", {"path": path}, approval_callback=approval_callback)
        if not approved:
            return ToolResult(
                success=False,
                error=(
                    f"edit_file blocked: {path} is a sensitive path\n"
                    f"(credential, secret, SSH key, or cloud config file).\n"
                    f"PM approval is required before editing this file."
                ),
            )

    if not os.path.isfile(resolved):
        return ToolResult(success=False, error=f"Not a file: {path}")

    try:
        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
    except PermissionError:
        return ToolResult(success=False, error=f"Permission denied: {path}")
    except UnicodeDecodeError:
        return ToolResult(success=False, error=f"File is not valid UTF-8: {path}")
    except OSError as e:
        return ToolResult(success=False, error=f"Cannot read {path}: {e}")

    # Count occurrences of old_text in the file
    count = content.count(old_text)
    if count == 0:
        return ToolResult(
            success=False,
            error=(
                f"old_text not found in {path}.\n"
                f"Verify the exact text you want to replace — whitespace and newlines matter.\n"
                f"Use write_file if you need a full file rewrite."
            ),
        )
    if count > 1:
        return ToolResult(
            success=False,
            error=(
                f"old_text is not unique in {path} — found {count} occurrences.\n"
                f"Specify a larger, unique portion of surrounding context so the replacement\n"
                f"is unambiguous. Use write_file if you need a full file rewrite."
            ),
        )

    # Exactly one match — safe to replace
    new_content = content.replace(old_text, new_text, 1)
    try:
        byte_count = len(new_content.encode("utf-8"))
        if byte_count > MAX_WRITE_SIZE:
            return ToolResult(success=False, error=f"Resulting file too large ({byte_count} bytes, max {MAX_WRITE_SIZE})")
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(new_content)
        return ToolResult(
            success=True,
            output=(
                f"OK — edited {path}\n"
                f"[enforcement:edit] {count} occurrence replaced"
            ),
        )
    except PermissionError:
        return ToolResult(success=False, error=f"Permission denied: {path}")
    except OSError as e:
        return ToolResult(success=False, error=f"Cannot write {path}: {e}")


def _exec_command(command: str, project_path: str, timeout: int = 30, session_key: str = "_unknown", approval_callback=None, scratch_dir: str | None = None) -> ToolResult:
    """Run a shell command in the project directory. Requires PM approval.

    Args:
        scratch_dir: Deprecated/ignored. Previously used as exec_command working directory.
            Now ignored — exec_command always runs in project_path. Retained for API
            compatibility; will be removed in a future cleanup.

    MED-2 (Phase 6): env= is now scrubbed to the same allowlist used by
    agent/enforcement.py. The shell semantics (shell=True, pipes, redirects,
    globs) are intentional for this tool — the PM approval dialog is the
    primary defense. env scrubbing is defense-in-depth: a command like
    `echo $OPENAI_API_KEY` or `env | grep KEY` will see only PATH/HOME/LANG/etc,
    never the user's provider secrets.
    """
    # Hard blocklist check first — always denied before callback fires
    if _is_blocked(command):
        return ToolResult(success=False, error=f"Command blocked by safety policy: {command}")

    # PM approval check via registered callback (Bug #3 fix)
    if not _get_approval(session_key, "exec_command", {"command": command, "cwd": project_path}, approval_callback=approval_callback):
        return ToolResult(success=False, error="exec_command requires PM approval", duration_ms=0)

    # MED-2: Scrub env so secrets (provider keys, gateway tokens) don't leak
    # into shell tool output (e.g. via `env`, `echo $OPENAI_API_KEY`, etc.).
    from utils.env_security import get_scrubbed_env

    start = time.monotonic()
    # exec_command always runs in project_path — the scratch_dir must NOT
    # override the CWD because the model expects commands to run in the project
    # root (as advertised by {{PROJECT_PATH}} in the system prompt).
    # scratch_dir is accepted as a parameter for API compatibility but ignored.
    exec_cwd = project_path
    # SPEC-09 SP3: Popen + start_new_session (NOT subprocess.run). The process
    # becomes its own process-group leader, registered for stop-all group
    # kills; output, truncation, scrubbed-env and cwd semantics are
    # byte-identical to the former subprocess.run call (binary pipes +
    # explicit utf-8/replace decode, NOT text=True — text mode would decode
    # with the locale codec under strict errors and change the contract).
    proc: subprocess.Popen | None = None
    registered = False
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=exec_cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=get_scrubbed_env(),
            start_new_session=True,
        )
        with _PROCESS_REGISTRY_LOCK:
            _PROCESS_REGISTRY.setdefault(session_key, []).append(proc)
        registered = True
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # SPEC-09 SP3: the OLD subprocess.run timeout killed only the
            # shell — children (sleeps, servers) survived as orphans. Now
            # group-kill the whole session (SIGTERM → 2s → SIGKILL, SP3 fix
            # round BUG#1: group-liveness gated, so a TERM-immune child
            # under a TERM-honoring leader cannot outlive its own timeout)
            # + communicate() reaper so the pipes close and no zombie
            # survives, then return the SAME timed-out ToolResult shape as
            # before (empty streams, exit None). The outer finally
            # unregisters.
            _group_kill(proc)
            try:
                proc.communicate(timeout=_KILL_ESCALATION_GRACE_SEC)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                pass  # reaper best-effort; the group is dead either way
            return ToolResult(success=False, error=f"Command timed out after {timeout}s", duration_ms=int((time.monotonic() - start) * 1000))
        duration_ms = int((time.monotonic() - start) * 1000)

        # Separate stdout/stderr for caller inspection; combined for output
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        # Truncate individual streams to prevent memory bloat
        max_stream = MAX_EXEC_OUTPUT
        if len(stdout) > max_stream:
            stdout = stdout[:max_stream] + f"\n[... truncated at {max_stream} bytes ...]"
        if len(stderr) > max_stream:
            stderr = stderr[:max_stream] + f"\n[... truncated at {max_stream} bytes ...]"

        combined = stdout + stderr
        if len(combined) > MAX_EXEC_OUTPUT:
            combined = combined[:MAX_EXEC_OUTPUT] + f"\n[... truncated at {MAX_EXEC_OUTPUT} bytes ...]"

        if proc.returncode != 0:
            return ToolResult(
                success=False,
                output=combined,
                error=f"Exit {proc.returncode}",
                duration_ms=duration_ms,
                stdout=stdout,
                stderr=stderr,
                exit_code=proc.returncode,
            )

        return ToolResult(
            success=True,
            output=combined,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode,
        )

    except subprocess.TimeoutExpired:  # pragma: no cover — dead guard: the inner communicate-timeout handler above catches first and returns; kept minimal per the phase sketch, never reachable via Popen()/registry ops.
        if proc is not None:
            _group_kill(proc)
            try:
                proc.communicate(timeout=_KILL_ESCALATION_GRACE_SEC)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                pass
        return ToolResult(success=False, error=f"Command timed out after {timeout}s", duration_ms=int((time.monotonic() - start) * 1000))
    except OSError as e:
        return ToolResult(success=False, error=f"Cannot execute: {e}", duration_ms=int((time.monotonic() - start) * 1000))
    finally:
        # SPEC-09 SP3: unregister on EVERY exit path (normal, timeout —
        # which returns from inside the try — and exceptions). A registry
        # entry must never outlive its process's tool call, or stop-all
        # would signal dead groups forever after.
        if registered and proc is not None:
            with _PROCESS_REGISTRY_LOCK:
                procs = _PROCESS_REGISTRY.get(session_key)
                if procs is not None and proc in procs:
                    procs.remove(proc)
                if procs is not None and not procs:
                    _PROCESS_REGISTRY.pop(session_key, None)


def _list_files(path: str, project_path: str, recursive: bool = False) -> ToolResult:
    """List files in a directory relative to project_path."""
    resolved = _resolve_project_path(path, project_path)
    if resolved is None:
        return ToolResult(success=False, error=f"Path escapes project sandbox: {path}")

    if not os.path.isdir(resolved):
        return ToolResult(success=False, error=f"Not a directory: {path}")

    try:
        lines = []
        if recursive:
            for root, dirs, files in os.walk(resolved):
                # Skip common noise directories
                dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".pytest_cache")]
                for name in sorted(files):
                    full = os.path.join(root, name)
                    try:
                        size = os.path.getsize(full)
                        rel = os.path.relpath(full, resolved)
                        lines.append(f"{rel}  {size}")
                    except OSError:
                        lines.append(f"{name}  [cannot stat]")
        else:
            for name in sorted(os.listdir(resolved)):
                full = os.path.join(resolved, name)
                try:
                    size = os.path.getsize(full) if os.path.isfile(full) else 0
                    marker = "/" if os.path.isdir(full) else ""
                    lines.append(f"{name}{marker}  {size}")
                except OSError:
                    lines.append(name)

        output = "\n".join(lines) if lines else "(empty)"
        return ToolResult(success=True, output=output)

    except PermissionError:
        return ToolResult(success=False, error=f"Permission denied: {path}")
    except OSError as e:
        return ToolResult(success=False, error=f"Cannot list {path}: {e}")


def _run_grep(
    pattern: str,
    search_root: str,
    file_type: str | None = None,
    timeout: int = 10,
) -> tuple[int, str, str]:
    """Run grep and return (returncode, stdout, stderr).

    Shared by _search_files (tool) and _file_search (tool) to guarantee
    identical grep behavior: same flags (-n -H --directories=skip -r),
    same --include filter, same -- separator, same timeout.
    """
    if not search_root or not isinstance(search_root, str):
        raise ValueError(f"search_root must be a non-empty string, got {search_root!r}")
    cmd = ["grep", "-n", "-H", "--directories=skip", "-r"]
    if file_type:
        cmd += ["--include=*." + file_type]
    cmd += ["--", pattern, "."]
    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        cwd=search_root,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def _search_files(pattern: str, project_path: str, path: str | None = None, file_type: str | None = None) -> ToolResult:
    """Search file contents using grep/ripgrep."""
    search_root = _resolve_project_path(path or ".", project_path)
    if search_root is None:
        return ToolResult(success=False, error=f"Path escapes project sandbox: {path or '.'}")

    try:
        returncode, stdout, stderr = _run_grep(pattern, search_root, file_type)
    except subprocess.TimeoutExpired:
        return ToolResult(success=False, error="Search timed out after 10s")
    except FileNotFoundError:
        return ToolResult(success=False, error="grep not found on this system")
    except OSError as e:
        return ToolResult(success=False, error=f"Search failed: {e}")

    if returncode == 1:
        return ToolResult(success=True, output="(no matches)")
    elif returncode != 0:
        return ToolResult(success=False, error=f"grep exited with {returncode}: {stderr[:200]}")

    output = stdout
    if len(output) > MAX_EXEC_OUTPUT:
        output = output[:MAX_EXEC_OUTPUT] + f"\n[... truncated ...]"
    return ToolResult(success=True, output=output)


def _file_search(
    query: str,
    project_path: str,
    file_type: str | None = None,
    max_results: int = 20,
    preview_lines: int = 5,
) -> ToolResult:
    """Find files by name OR content pattern. Returns grouped, previewed results.

    Combines:
    - Filename matching via agent.context._find_matching_files
    - Content matching via _run_grep (shared with _search_files)
    """
    if not query or not query.strip():
        return ToolResult(success=False, error="empty query")

    search_root = _resolve_project_path(".", project_path)
    if search_root is None:
        return ToolResult(success=False, error=f"Path escapes project sandbox: {project_path}")

    # Filename matching via _find_matching_files
    from agent.context import _find_matching_files, _load_gitignore_patterns
    patterns = _load_gitignore_patterns(project_path)
    name_matches = _find_matching_files(
        project_path, query, patterns, max_files=max_results
    )

    # Content matching via _run_grep
    grep_hits: dict[str, list[tuple[int, str]]] = {}
    try:
        returncode, stdout, stderr = _run_grep(query, search_root, file_type)
        if returncode == 0 and stdout:
            for line in stdout.splitlines():
                # Parse grep output: file:line:content
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    fpath, lineno, content = parts[0], int(parts[1]), parts[2]
                    # Normalize grep paths: strip leading "./" so they match
                    # the format from _find_matching_files (relative, no prefix)
                    if fpath.startswith("./"):
                        fpath = fpath[2:]
                    if fpath not in grep_hits:
                        grep_hits[fpath] = []
                    grep_hits[fpath].append((lineno, content))
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass  # Non-fatal — name matches still usable

    # Merge results: files from both name + content matches
    all_files = set(name_matches)
    all_files.update(grep_hits.keys())
    all_files = sorted(all_files)[:max_results]

    if not all_files:
        return ToolResult(success=True, output="(no matches)")

    # Build output
    lines: list[str] = []
    for fpath in all_files:
        full_path = os.path.join(search_root, fpath)
        try:
            size = os.path.getsize(full_path)
            size_str = f"{size // 1024}KB" if size < 1024 * 1024 else f"{size // (1024 * 1024)}MB"
        except OSError:
            size_str = "?KB"

        # Best-effort line count
        try:
            with open(full_path, "rb") as f:
                lc = sum(1 for _ in f)
            lc_str = f"{lc:,} lines"
        except (OSError, UnicodeDecodeError):
            lc_str = "?"

        lines.append(f"{fpath} ({lc_str}, {size_str})")

        if fpath in grep_hits:
            for lineno, content in grep_hits[fpath][:preview_lines]:
                lines.append(f"  Line {lineno}: {content.strip()}")
        else:
            lines.append("  [name match only — use read_file for content]")

    lines.append("")
    lines.append('[Use read_file("path") to read full contents. Use list_files(".") to browse directory tree.]')

    output = "\n".join(lines)
    if len(output) > MAX_EXEC_OUTPUT:
        output = output[:MAX_EXEC_OUTPUT] + f"\n[... truncated ...]"
    return ToolResult(success=True, output=output)


def _get_brave_api_key() -> str | None:
    """Get Brave Search API key from environment."""
    return os.environ.get("BRAVE_API_KEY") or os.environ.get("OPENCLAW_BRAVE_API_KEY")


def _web_search(query: str, count: int = 5) -> ToolResult:
    """Search the web via Brave Search API."""
    api_key = _get_brave_api_key()
    if not api_key:
        return ToolResult(success=False, error="web_search requires BRAVE_API_KEY environment variable")

    try:
        params = {"q": query, "count": min(count, 10), "freshness": "y"}
        headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers=headers,
            timeout=10.0,
        )
        if resp.status_code == 401:
            return ToolResult(success=False, error="Brave API key is invalid or expired")
        resp.raise_for_status()
        data = resp.json()

        results = data.get("web", {}).get("results", [])
        if not results:
            return ToolResult(success=True, output="(no results)")

        lines = []
        for item in results[:count]:
            title = item.get("title", "")
            url = item.get("url", "")
            snippet = item.get("description", "")
            lines.append(f"## {title}\n{url}\n{snippet}\n")

        return ToolResult(success=True, output="\n".join(lines))

    except httpx.HTTPStatusError as e:
        return ToolResult(success=False, error=f"Brave API error {e.response.status_code}: {e.response.text[:200]}")
    except httpx.RequestError as e:
        return ToolResult(success=False, error=f"web_search failed: {e}")


def _is_web_fetch_restricted() -> bool:
    """MED-3 opt-in check: returns True when WEB_FETCH_RESTRICT=1
    (DEVELCAKES_WEB_FETCH_RESTRICT; the old CRABCAKES_ name rides the
    one-release fallback — utils.config.get_env). Default off per Q3."""
    return get_env("WEB_FETCH_RESTRICT") == "1"


def _reject_restricted_url(url: str) -> ToolResult | None:
    """MED-3: If restricted mode is on, validate URL against private IP ranges.
    Returns ToolResult(failure) if blocked, None if allowed.

    Known limitation (P6.1-4): DNS rebinding TOCTOU. This function resolves the
    hostname via socket.getaddrinfo, but httpx.get makes a separate TCP
    connection that triggers a NEW DNS resolution. A malicious DNS server can
    return a public IP here and a private IP on the actual connection. This is
    inherent to DNS-based SSRF prevention without a custom transport that pins
    the resolved IP. The Phase 6.1 manual redirect loop narrows the window
    (validation happens before each connection) but does not eliminate it.
    """
    if not _is_web_fetch_restricted():
        return None

    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(url)
    hostname = parsed.hostname
    if hostname is None:
        return ToolResult(success=False, error=f"MED-3: No hostname in URL: {url}")

    # Check hostname match first (fast path)
    host_lower = hostname.lower()
    if host_lower in ("localhost", "127.0.0.1", "::1"):
        return ToolResult(success=False, error=f"MED-3: Refusing loopback request: {url}")
    if host_lower.startswith("169.254."):
        return ToolResult(success=False, error=f"MED-3: Refusing link-local request: {url}")
    if host_lower.startswith("fe80:"):
        return ToolResult(success=False, error=f"MED-3: Refusing link-local request: {url}")

    # Try IP address resolution for private ranges
    try:
        import socket
        addr = socket.getaddrinfo(hostname, None)
        for family, _, _, _, sockaddr in addr:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private:
                return ToolResult(success=False, error=f"MED-3: Refusing private IP request: {url} → {ip}")
            if ip.is_loopback:
                return ToolResult(success=False, error=f"MED-3: Refusing loopback request: {url} → {ip}")
            if ip.is_link_local:
                return ToolResult(success=False, error=f"MED-3: Refusing link-local request: {url} → {ip}")
    except socket.gaierror:
        # Hostname doesn't resolve — refuse (could be SSRF attempt)
        return ToolResult(success=False, error=f"MED-3: Hostname does not resolve: {hostname}")
    except Exception as e:
        # Resolution failed — refuse for safety
        return ToolResult(success=False, error=f"MED-3: Failed to resolve {hostname}: {e}")

    return None


def _web_fetch(url: str, max_chars: int = 10000) -> ToolResult:
    """Fetch and extract readable content from a URL."""
    # MED-3: Opt-in host allowlist check (initial URL)
    blocked = _reject_restricted_url(url)
    if blocked is not None:
        return blocked

    # MED-3 (Phase 6.1): Handle redirects manually so we can validate each
    # Location header BEFORE making a TCP connection to the target.
    # Previously, httpx.get(follow_redirects=True) would connect to every
    # hop (including private/loopback hosts) before the post-hoc re-check ran.
    current_url = url
    for _ in range(10):  # max 10 redirects
        try:
            resp = httpx.get(current_url, timeout=10.0, follow_redirects=False)
        except httpx.RequestError as e:
            return ToolResult(success=False, error=f"web_fetch failed: {e}")

        if 300 <= resp.status_code < 400:
            location = resp.headers.get("location", "")
            if not location:
                return ToolResult(success=False, error="web_fetch: redirect without Location header")
            # Resolve relative redirect URLs against the current URL
            next_url = str(httpx.URL(current_url).join(location))
            # MED-3: validate the redirect target BEFORE following it
            blocked = _reject_restricted_url(next_url)
            if blocked is not None:
                return ToolResult(
                    success=False,
                    error=f"MED-3: Redirected to blocked URL: {next_url}",
                )
            current_url = next_url
            continue
        break
    else:
        # Loop completed without break — all 10 iterations were redirects
        return ToolResult(success=False, error="web_fetch: exceeded max redirects (10)")

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return ToolResult(success=False, error=f"web_fetch HTTP {e.response.status_code}: {current_url}")

    # Defense in depth: re-validate the final response URL
    if _is_web_fetch_restricted():
        final_check = _reject_restricted_url(str(resp.url))
        if final_check is not None:
            return final_check

    content_type = resp.headers.get("content-type", "")
    if "text/html" not in content_type and "text/plain" not in content_type:
        return ToolResult(success=False, error=f"Cannot fetch non-text content type: {content_type}")

    text = resp.text
    # Basic extraction: strip HTML tags
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[... truncated at {max_chars} chars ...]"

    return ToolResult(success=True, output=text)


# ── Tool registry ───────────────────────────────────────────────────────────────

# _TOOLS maps name -> (ToolDefinition, handler_fn)
_TOOLS: dict[str, tuple[ToolDefinition, Callable[..., ToolResult]]] = {}


def _register_tools() -> None:
    """Register all available tools. Called once at module load."""

    # read_file
    _TOOLS["read_file"] = (
        ToolDefinition(
            name="read_file",
            description=(
                "Read a file's text content from the project directory.\n\n"
                "WHEN TO USE: Always read a file BEFORE modifying it. Use to understand\n"
                "existing code, check imports, verify structure, or review tests.\n\n"
                "WHEN NOT TO USE: For listing directory contents (use list_files).\n"
                "For searching across files (use search_files).\n\n"
                "BEHAVIOR: Returns UTF-8 text. Binary files return an error.\n"
                "Truncates at 50KB. Use offset/limit for reading specific sections\n"
                "of large files without loading everything.\n\n"
                "COMMON PATTERNS:\n"
                "- Read a file before writing: understand context, imports, style\n"
                "- Read tests before implementing: understand expected behavior\n"
                "- Read architecture.md first when starting a new task"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path within the project directory"},
                    "offset": {"type": "integer", "description": "Byte offset to start reading from (optional)"},
                    "limit": {"type": "integer", "description": "Maximum bytes to read (optional)"},
                },
                "required": ["path"],
            },
            requires_approval=False,
        ),
        lambda path, project_path, offset=None, limit=None, **kwargs:  # type: ignore
            _read_file(path, project_path, offset, limit),
    )

    # write_file
    _TOOLS["write_file"] = (
        ToolDefinition(
            name="write_file",
            description=(
                "Write content to a file in the project directory.\n\n"
                "WHEN TO USE: To create new files, or rewrite an entire file after\n"
                "reading it first. Always read a file before overwriting it.\n\n"
                "WHEN NOT TO USE: For running commands (use exec_command).\n"
                "For reading files (use read_file).\n\n"
                "BEHAVIOR: Creates parent directories if needed. Overwrites existing\n"
                "files entirely — there is no partial edit. Max 2MB.\n"
                "All paths are sandboxed to the project directory.\n\n"
                "IMPORTANT: This replaces the ENTIRE file. Always read_file first,\n"
                "then write back the full content with your changes applied."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path within the project directory"},
                    "content": {"type": "string", "description": "Text content to write"},
                },
                "required": ["path", "content"],
            },
            requires_approval=False,
        ),
        lambda path, content, project_path, **kwargs:  # type: ignore
            _write_file(path, content, project_path, session_key=kwargs.get("session_key", ""), approval_callback=kwargs.get("approval_callback")),
    )

    # edit_file
    _TOOLS["edit_file"] = (
        ToolDefinition(
            name="edit_file",
            description=(
                "Make a targeted edit to a file by replacing exact text with new text.\n\n"
                "WHEN TO USE: To change a specific section of a file without rewriting\n"
                "the whole thing. Use when you know the exact text surrounding the change.\n\n"
                "WHEN NOT TO USE: For creating new files (use write_file).\n"
                "For large rewrites where the surrounding context is complex (use write_file\n"
                "after reading the full file).\n\n"
                "BEHAVIOR: Finds old_text exactly in the file (no regex, no fuzzy matching).\n"
                "Replaces only the first unique occurrence. Must be unique in the file\n"
                "or the edit is rejected. All paths are sandboxed to the project directory.\n\n"
                "IMPORTANT: Both old_text and new_text are matched literally.\n"
                "Copy the exact surrounding context (including whitespace and newlines)\n"
                "to ensure the match succeeds. Use write_file if the text is not unique\n"
                "or you need a full file rewrite.\n\n"
                "COMMON PATTERNS:\n"
                "- Change a function body: include the def line so context is unique\n"
                "- Add a new import: include adjacent imports so the match is unique\n"
                "- Fix a bug: include the buggy lines plus a few lines of surrounding context"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path within the project directory"},
                    "old_text": {"type": "string", "description": "Exact text to find and replace — must be unique in the file"},
                    "new_text": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old_text", "new_text"],
            },
            requires_approval=False,
        ),
        lambda path, old_text, new_text, project_path, **kwargs:  # type: ignore
            _edit_file(path, old_text, new_text, project_path, session_key=kwargs.get("session_key", ""), approval_callback=kwargs.get("approval_callback")),
    )

    # exec_command
    _TOOLS["exec_command"] = (
        ToolDefinition(
            name="exec_command",
            description=(
                "Run a shell command in the project directory.\n\n"
                "WHEN TO USE: Running tests, linters, build scripts, git commands,\n"
                "checking environment (python version, installed packages).\n\n"
                "WHEN NOT TO USE: Creating files (use write_file). Reading files\n"
                "(use read_file). Listing directories (use list_files).\n\n"
                "BEHAVIOR: PM must approve each call before execution.\n"
                "Blocked commands (rm -rf /, mkfs, fork bombs) are always denied.\n"
                "Default timeout 30s, max 120s. Output is truncated at 100KB.\n\n"
                "RESULT FORMAT (exec_command always returns all four fields):\n"
                "  - output: stdout + stderr combined (shown to the model)\n"
                "  - stdout: raw stdout text\n"
                "  - stderr: raw stderr text\n"
                "  - exit_code: integer exit code (0 = success)\n"
                "  - error: non-empty on failure (contains exit_code reason)\n\n"
                "COMMON PATTERNS:\n"
                "- Run tests: exec_command with 'pytest' or 'python -m pytest'\n"
                "- Check git status: exec_command with 'git status'\n"
                "- Install deps: exec_command with 'pip install -e .'"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30, max 120)"},
                },
                "required": ["command"],
            },
            requires_approval=True,
        ),
        lambda command, project_path, timeout=30, session_key="_unknown", scratch_dir=None, **kwargs:  # type: ignore
            _exec_command(command, project_path, min(timeout, 120), session_key, approval_callback=kwargs.get("approval_callback"), scratch_dir=scratch_dir),
    )

    # list_files
    _TOOLS["list_files"] = (
        ToolDefinition(
            name="list_files",
            description=(
                "List files and subdirectories in a project directory.\n\n"
                "WHEN TO USE: Understanding project structure before diving into code.\n"
                "Finding test files related to a module. Exploring an unfamiliar project.\n\n"
                "WHEN NOT TO USE: Reading file contents (use read_file).\n"
                "Searching for text patterns (use search_files).\n\n"
                "BEHAVIOR: Non-recursive by default. Use recursive=True for full tree.\n"
                "Skips .git, node_modules, __pycache__, .pytest_cache.\n"
                "Shows file sizes for context."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path within the project (default '.')"},
                    "recursive": {"type": "boolean", "description": "Recurse into subdirectories (default False)"},
                },
                "required": [],
            },
            requires_approval=False,
        ),
        lambda project_path, path=".", recursive=False, **kwargs:  # type: ignore
            _list_files(path, project_path, recursive),
    )

    # search_files
    _TOOLS["search_files"] = (
        ToolDefinition(
            name="search_files",
            description=(
                "Search for text patterns in project files using grep.\n\n"
                "WHEN TO USE: Finding all callers of a function. Finding all imports\n"
                "of a module. Finding usages of a variable or class. Finding error\n"
                "strings to locate where they originate.\n\n"
                "WHEN NOT TO USE: Finding files by name (use list_files).\n"
                "Reading a specific file (use read_file).\n\n"
                "BEHAVIOR: Returns matching lines with file paths and line numbers.\n"
                "Supports regex. Use file_type to filter by extension (e.g. 'py').\n"
                "Times out after 10s.\n\n"
                "COMMON PATTERNS:\n"
                "- Find function definition: search_files with 'def my_function'\n"
                "- Find all usages: search_files with 'my_function(' file_type='py'\n"
                "- Find import references: search_files with 'from mymodule import'"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Text pattern or regex to search for"},
                    "path": {"type": "string", "description": "Relative path to search within (default '.')"},
                    "file_type": {"type": "string", "description": "Only match files with this extension (e.g. 'py', 'js')"},
                },
                "required": ["pattern"],
            },
            requires_approval=False,
        ),
        lambda pattern, project_path, path=None, file_type=None, **kwargs:  # type: ignore
            _search_files(pattern, project_path, path, file_type),
    )

    # file_search
    _TOOLS["file_search"] = (
        ToolDefinition(
            name="file_search",
            description=(
                "Find files by name or content pattern. Returns grouped results\n"
                "with file metadata and preview lines.\n\n"
                "WHEN TO USE: Discovering which files contain a function, class,\n"
                "or concept before reading them. Replaces browsing the file index.\n\n"
                "BEHAVIOR: Groups matches by file. Shows line count + size per file.\n"
                "Returns up to 5 preview lines per match.\n"
                "Use read_file() to get full contents after finding the right file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Filename fragment or text/regex pattern"},
                    "file_type": {"type": "string", "description": "Filter by extension (e.g. 'py', 'md')"},
                    "max_results": {"type": "integer", "description": "Max files to return (default 20)"},
                },
                "required": ["query"],
            },
            requires_approval=False,
        ),
        lambda query, project_path, file_type=None, max_results=None, **kwargs:  # type: ignore
            _file_search(query, project_path, file_type, max_results or 20),
    )

    # web_search
    _TOOLS["web_search"] = (
        ToolDefinition(
            name="web_search",
            description=(
                "Search the web using Brave Search.\n\n"
                "WHEN TO USE: Looking up API documentation, library references,\n"
                "error message solutions, or current best practices.\n\n"
                "WHEN NOT TO USE: Searching project files (use search_files).\n"
                "Reading a URL's content (use web_fetch).\n\n"
                "BEHAVIOR: Returns title, URL, and snippet for each result.\n"
                "Default 5 results, max 10. Requires BRAVE_API_KEY env var."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "count": {"type": "integer", "description": "Maximum number of results (default 5, max 10)"},
                },
                "required": ["query"],
            },
            requires_approval=False,
        ),
        lambda query, count=5, **kwargs: _web_search(query, count),  # type: ignore
    )

    # web_fetch
    _TOOLS["web_fetch"] = (
        ToolDefinition(
            name="web_fetch",
            description=(
                "Fetch and extract readable text from a URL.\n\n"
                "WHEN TO USE: Reading documentation pages, API references,\n"
                "or GitHub repos that you found via web_search.\n\n"
                "WHEN NOT TO USE: Searching the web (use web_search).\n"
                "Reading local files (use read_file).\n\n"
                "BEHAVIOR: Strips HTML tags, returns plain text.\n"
                "Truncated at 10,000 chars by default (adjustable via max_chars).\n"
                "Only works on HTML/text pages, not PDFs or binaries."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "max_chars": {"type": "integer", "description": "Maximum characters to return (default 10,000)"},
                },
                "required": ["url"],
            },
            requires_approval=False,
        ),
        lambda url, max_chars=10000, **kwargs: _web_fetch(url, max_chars),  # type: ignore
    )


_register_tools()


# ── Public API ─────────────────────────────────────────────────────────────────

def get_all_tools() -> list[ToolDefinition]:
    """Return all available tool definitions."""
    return [defn for defn, _ in _TOOLS.values()]


def get_tool_definitions_for_api(allowed_tools: list[str] | None = None) -> list[dict]:
    """
    Return tool definitions in OpenAI function-calling format.

    Args:
        allowed_tools: If None, returns all tools. If a list, returns only
                      tools whose names are in allowed_tools (order preserved).
    Used by AgentRuntime when calling the LLM API.
    """
    all_tools = get_all_tools()
    if allowed_tools is not None:
        allowed_set = set(allowed_tools)
        tools_to_include = [t for t in all_tools if t.name in allowed_set]
    else:
        tools_to_include = all_tools

    result = []
    for defn in tools_to_include:
        entry: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": defn.name,
                "description": defn.description,
                "parameters": defn.parameters,
            },
        }
        result.append(entry)
    return result


def execute_tool(
    name: str,
    arguments: dict,
    project_path: str,
    session_key: str = "_unknown",
    approval_callback: Callable[[str, str, dict], bool] | None = None,
    scratch_dir: str | None = None,
    allowed_tools: list[str] | None = None,
) -> ToolResult:
    """
    Execute a tool by name with the given arguments.

    Args:
        name: Tool name (e.g. "read_file")
        arguments: Parsed arguments dict (e.g. {"path": "src/main.py"})
        project_path: Absolute path to the project working directory (sandbox base)
        session_key: Session key of the agent (for exec_command approval)
        approval_callback: Per-call approval callback (MED-1). Takes precedence
            over the global _approval_callback.
        scratch_dir: Deprecated/ignored. Previously used as exec_command cwd.
            All tools now use project_path. Retained for API compatibility.
        allowed_tools: If provided, only tools whose name is in this list are
            permitted. Tools outside the list are denied with a ToolResult
            (success=False) BEFORE the handler runs. If None (default), all
            registered tools are permitted (back-compat). This is the
            enforcement gate; the API-schema filter in
            get_tool_definitions_for_api() is advisory only.

    Returns:
        ToolResult with output or error.

    All file paths are sandboxed to project_path.
    exec_command requires PM approval via the registered callback.
    """
    # §3.21n — allowed_tools enforcement gate (MOVED before MCP routing).
    # When the caller (agent runtime) supplies an explicit allow-list,
    # we honor it here for BOTH built-in and MCP tools. The API-schema
    # filter in get_tool_definitions_for_api() is advisory only; this gate
    # is the law. Previously this gate ran AFTER the MCP branch, allowing
    # MCP tools to bypass it entirely (BUG: gate-ordering-bypass).
    if allowed_tools is not None and name not in allowed_tools:
        return ToolResult(
            success=False,
            error=(
                f"Tool '{name}' is not in the agent's allowed_tools "
                f"(permitted: {sorted(allowed_tools)})"
            ),
        )

    # MCP tool routing. Wire names use "__" as the separator (provider-safe).
    from utils.mcp_client import _from_wire_name
    server_tool = _from_wire_name(name)
    if server_tool is not None:
        server_name, tool_name = server_tool
        # Route to MCP client
        try:
            from utils.mcp_client import call_tool as mcp_call_tool, is_connected
            # Use session_key as conversation_key (or default)
            conv_key = session_key if session_key != "_unknown" else None
            if not is_connected(conv_key, server_name):
                from utils.mcp_client import connect as mcp_connect
                try:
                    mcp_connect(server_name, conv_key)
                except Exception as e:
                    return ToolResult(success=False, error=f"Failed to connect to MCP server '{server_name}': {e}")
            mcp_result = mcp_call_tool(server_name, tool_name, arguments, conv_key)
            return ToolResult(
                success=mcp_result.success,
                output=mcp_result.output,
                error=mcp_result.error,
                duration_ms=mcp_result.duration_ms,
            )
        except Exception as e:
            return ToolResult(success=False, error=f"MCP client error: {e}")

    entry = _TOOLS.get(name)
    if entry is None:
        return ToolResult(success=False, error=f"Unknown tool: {name}")

    defn, handler = entry
    start = time.monotonic()

    try:
        # Inject project_path, session_key, approval_callback, and scratch_dir into arguments
        args = dict(arguments)
        args["project_path"] = project_path
        args["session_key"] = session_key
        args["approval_callback"] = approval_callback
        args["scratch_dir"] = scratch_dir

        result = handler(**args)
        duration_ms = int((time.monotonic() - start) * 1000)
        if result.duration_ms == 0:
            result = dataclasses.replace(result, duration_ms=duration_ms)
        return result

    except TypeError as e:
        # Missing or unexpected argument
        return ToolResult(
            success=False,
            error=f"Tool {name} received invalid arguments: {e}",
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    except Exception as e:
        return ToolResult(
            success=False,
            error=f"Tool {name} raised {type(e).__name__}: {e}",
            duration_ms=int((time.monotonic() - start) * 1000),
        )
