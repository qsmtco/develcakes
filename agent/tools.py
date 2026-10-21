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
import shutil
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
    # In-process only. to_dict does not emit this. The middleware copies
    # EnforcementCheck objects here after a successful write.
    enforcement_checks: list = dataclasses.field(default_factory=list)

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


def set_agent_context_callback(cb) -> None:
    """Install the context edit callback.

    Trigger: the ``agent_context`` tool handler, after target resolution.
    Signature ``(caller_session_key, target_session_key, action, focus, spec) -> ToolResult``.
    ``spec`` is the drop/keep selection for ``edit``, otherwise None.
    """
    from agent.group_tools import set_agent_context_callback as _set
    _set(cb)


def set_canvas_callback(cb) -> None:
    """Install the canvas tab callback.

    Trigger: the ``canvas`` tool handler, after the slug and size checks.
    Signature ``(session_key, slug, html) -> ToolResult``.
    """
    from agent.group_tools import set_canvas_callback as _set
    _set(cb)


def set_talk_callback(cb) -> None:
    """Install the talk callback.

    Trigger: the ``talk`` tool handler. Signature ``(text,) -> ToolResult``.
    """
    from agent.group_tools import set_talk_callback as _set
    _set(cb)


def set_feed_card_callback(cb) -> None:
    """Install the project-feed card callback.

    Trigger: the ``feed_card`` tool handler, after every field is validated.
    Signature ``(session_key, fields) -> ToolResult``.
    """
    from agent.group_tools import set_feed_card_callback as _set
    _set(cb)


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

MAX_READ_SIZE = 16 * 1024       # 16 KB — matches CONV_OUTPUT_CAP
                                 # (CONTEXT-EFFICIENCY-INSTRUCTIONS §5).
                                 # read_file is in _ALREADY_WINDOWED, so the
                                 # window is never head/tail-bounded and its
                                 # offset= continuation notice survives; the
                                 # window itself no longer overhangs what the
                                 # conversation keeps.
MAX_READ_LINES = 2000
LINE_WINDOW = 200
# A search with one content hit shows this many lines on either side.
# Two or three hits show the smaller window. More than three keep the
# 40-line preview and do not open the file.
SEARCH_CONTEXT_ONE = 50
SEARCH_CONTEXT_FEW = 15
# Per-file ceiling for one emitted hit block (matches + context). A single
# file must never be able to fill the conversation cap, or bound_tool_output
# drops the middle of the block, which is where the match lines are.
SEARCH_FILE_CHARS = 4_000
# Ceiling for the WHOLE emitted search output, in characters of the joined
# string. Sits below CONV_OUTPUT_CAP so bound_tool_output never fires and
# never has to drop the middle (it would drop match lines).
SEARCH_OUTPUT_CHARS = 14_000
# file_search forwards any max_results; clamp it so a huge match set cannot
# push the joined output over CONV_OUTPUT_CAP through headers alone.
MAX_SEARCH_FILES = 20
# Small reserve charged per planned content-hit file by the dynamic listing
# cap (Phase 3b): a file with hits always emits at least one match line, so
# the cap must leave room for it or the last listed file would lose it.
SEARCH_MATCH_LINE_ALLOWANCE = 40
MAX_LIST_ENTRIES = 400
MAX_WRITE_SIZE = 2 * 1024 * 1024  # 2 MB
MAX_EXEC_OUTPUT = 100 * 1024     # 100 KB
# Conversation cap for exec/search/list/mcp output. Process capture stays
# at MAX_EXEC_OUTPUT (100KB) so test_exec_output_truncation_intact holds.
CONV_OUTPUT_CAP = 16 * 1024
CONV_OUTPUT_HEAD = 2 * 1024
CONV_OUTPUT_TAIL = 8 * 1024
# read_file already returns a byte/line window whose continuation offset
# must survive. Head/tail bounding would hide offset=.
_ALREADY_WINDOWED = frozenset({"read_file"})

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


def _start_byte_for_line(path: str, line: int) -> int | None:
    """Byte offset of a 1-based line, or None if the file ends first.

    Line 1 is byte 0, including an empty file. A request past the last
    line is None so the caller does not return a window from byte 0.
    """
    if line <= 1:
        return 0
    need = line - 1
    pos = 0
    with open(path, "rb") as fh:
        while need > 0:
            chunk = fh.read(65536)
            if not chunk:
                return None
            start = 0
            while need > 0:
                nl = chunk.find(b"\n", start)
                if nl < 0:
                    break
                need -= 1
                start = nl + 1
                if need == 0:
                    return pos + start
            pos += len(chunk)
    return None


def _numbered_line_block(lines: list[str], start: int) -> str:
    """Render file lines as ``N|text``. The number and bar are not file content."""
    rendered: list[str] = []
    for i, line in enumerate(lines):
        rendered.append(f"{start + i}|{line.rstrip(chr(10)).rstrip(chr(13))}")
    return "\n".join(rendered)


def _read_file(
    path: str,
    project_path: str,
    offset: int | None = None,
    limit: int | None = None,
    line: int | None = None,
    max_lines: int | None = None,
) -> ToolResult:
    """Read a file relative to project_path.

    Coordinates are lines. ``offset`` is accepted and ignored so an old
    byte index cannot slice the file.
    """
    del offset
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

        # Every read is a line window. offset is ignored: it used to be a
        # byte index, and models passed it as a line. limit is a line count
        # when max_lines is omitted. The byte cap only stops one huge line
        # from filling the result; the continuation is still a line number.
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            return ToolResult(success=False, error="limit must be an integer >= 1")
        if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
            return ToolResult(success=False, error="line must be an integer >= 1")
        if max_lines is not None and (
            isinstance(max_lines, bool) or not isinstance(max_lines, int) or max_lines < 1
        ):
            return ToolResult(success=False, error="max_lines must be an integer >= 1")
        start_line = 1 if line is None else line
        if max_lines is not None:
            window = min(max_lines, LINE_WINDOW)
        elif limit is not None:
            window = min(limit, LINE_WINDOW)
        else:
            window = LINE_WINDOW
        start = _start_byte_for_line(resolved, start_line)
        if start is None or (start_line > 1 and start >= size):
            return ToolResult(
                success=False,
                error=f"line {start_line} is past the end of {path}",
            )
        with open(resolved, "rb") as f:
            f.seek(start)
            raw = f.read(MAX_READ_SIZE)
        decoded = raw.decode("utf-8", errors="replace")
        lines = decoded.splitlines(keepends=True)
        if not lines and size == 0:
            return ToolResult(
                success=True,
                output=f"[line {start_line} of {path}]\n(empty)",
            )
        got = lines[:window]
        body = _numbered_line_block(got, start_line)
        content = f"[line {start_line} of {path}]\n" + body
        file_continues = (start + len(raw)) < size or len(lines) > len(got)
        last_partial = bool(got) and not got[-1].endswith("\n")
        if file_continues and last_partial:
            next_line = start_line + len(got) - 1
            content += (
                f"\n[... truncated; line window cut after {len(raw)} bytes; "
                f"continue with line={next_line} ...]"
            )
        elif file_continues:
            next_line = start_line + len(got)
            content += f"\n[... truncated; continue with line={next_line} ...]"
        return ToolResult(success=True, output=content)

    except PermissionError:
        return ToolResult(success=False, error=f"Permission denied: {path}")
    except UnicodeDecodeError:
        return ToolResult(success=False, error=f"File is not valid UTF-8: {path}")
    except OSError as e:
        return ToolResult(success=False, error=f"Cannot read {path}: {e}")


def _is_context_md(path: str, project_path: str) -> bool:
    """True when path resolves to this project's .crabcakes/context.md."""
    resolved = _resolve_project_path(path, project_path)
    if resolved is None or not project_path:
        return False
    expected = os.path.realpath(os.path.join(project_path, ".crabcakes", "context.md"))
    return resolved == expected


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

    if _is_context_md(path, project_path):
        if isinstance(content, str) and content.lstrip().startswith("## "):
            from utils.project_awareness import append_project_context, load_project_context
            append_project_context(project_path, content.strip())
            loaded = load_project_context(project_path)
            heading = content.lstrip().splitlines()[0].strip()
            if heading not in loaded:
                return ToolResult(success=False, error="context.md append did not persist")
            return ToolResult(
                success=True,
                output="OK — appended an entry to .crabcakes/context.md",
            )
        return ToolResult(
            success=False,
            error="context.md is append-only. write_file a single ## entry. The file was not changed.",
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


def _leading_indent(line: str) -> str:
    i = 0
    while i < len(line) and line[i] in " \t":
        i += 1
    return line[:i]


def _reindent_block(new_text: str, file_indent: str) -> str:
    """Shift new_text so its first line uses the file's indentation.

    Relative indentation inside the block is kept. Blank lines stay blank.
    """
    lines = new_text.split("\n")
    base = ""
    for line in lines:
        if line.strip():
            base = _leading_indent(line)
            break
    out: list[str] = []
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        ind = _leading_indent(line)
        rel = ind[len(base):] if ind.startswith(base) else ""
        out.append(file_indent + rel + line.lstrip(" \t"))
    return "\n".join(out)


def _file_line_spans(content: str) -> list[tuple[int, int, str]]:
    """(start, end, text without the newline) for each line. end includes the newline."""
    spans: list[tuple[int, int, str]] = []
    i = 0
    while i < len(content):
        nl = content.find("\n", i)
        if nl < 0:
            spans.append((i, len(content), content[i:]))
            break
        spans.append((i, nl + 1, content[i:nl]))
        i = nl + 1
    return spans


def _indent_flexible_spans(content: str, old_text: str) -> list[tuple[int, int, str]]:
    """Spans where each line matches old_text once leading indentation is removed.

    Returns (start, end, indent of the first file line). An empty old_text
    matches nothing.
    """
    old_lines = old_text.split("\n")
    if old_text.endswith("\n"):
        old_lines = old_lines[:-1]
    if not old_lines or not any(line.strip() for line in old_lines):
        return []
    stripped = [line.lstrip(" \t") for line in old_lines]
    file_lines = _file_line_spans(content)
    n = len(old_lines)
    hits: list[tuple[int, int, str]] = []
    for start_i in range(len(file_lines) - n + 1):
        window = file_lines[start_i:start_i + n]
        if all(window[k][2].rstrip("\r").lstrip(" \t") == stripped[k].rstrip("\r") for k in range(n)):
            hits.append((window[0][0], window[-1][1], _leading_indent(window[0][2])))
    return hits


def _occurrence_line_numbers(content: str, needle: str) -> list[int]:
    nums: list[int] = []
    start = 0
    step = max(len(needle), 1)
    while True:
        found = content.find(needle, start)
        if found < 0:
            break
        nums.append(content.count("\n", 0, found) + 1)
        start = found + step
    return nums


def _nearby_edit_lines(content: str, old_text: str) -> str:
    """Up to three close spots, numbered, so a miss can be retried."""
    file_lines = content.splitlines()
    needle = ""
    for line in old_text.splitlines() or [old_text]:
        if line.strip():
            needle = line.strip()
            break
    if not needle or not file_lines:
        return ""
    hits = [i for i, line in enumerate(file_lines) if needle in line]
    if not hits:
        import difflib
        stripped = [line.strip() for line in file_lines]
        choices = difflib.get_close_matches(needle, stripped, n=3, cutoff=0.6)
        for choice in choices:
            for i, line in enumerate(stripped):
                if line == choice and i not in hits:
                    hits.append(i)
                    break
    if not hits:
        return ""
    shown: list[str] = []
    for i in hits[:3]:
        lo = max(0, i - 2)
        hi = min(len(file_lines), i + 3)
        shown.append(_numbered_line_block(file_lines[lo:hi], lo + 1))
    return "Nearby lines:\n" + "\n\n".join(shown)


def _snippet_around(content: str, index: int, radius: int = 5) -> str:
    lines = content.splitlines()
    if not lines:
        return "(empty)"
    line_no = content.count("\n", 0, index) + 1
    lo = max(1, line_no - radius)
    hi = min(len(lines), line_no + radius)
    return _numbered_line_block(lines[lo - 1:hi], lo)


def _edit_file(
    path: str,
    old_text: str,
    new_text: str,
    project_path: str,
    session_key: str = "",
    approval_callback=None,
    replace_all: bool = False,
) -> ToolResult:
    """Replace old_text with new_text. Exact and unique, unless replace_all.

    When the exact text is missing but the same lines match once with the
    indentation removed, that one match is applied and new_text is shifted
    to the file's indentation. A miss includes nearby numbered lines.
    """
    resolved = _resolve_project_path(path, project_path)
    if resolved is None:
        return ToolResult(success=False, error=f"Path escapes project sandbox: {path}")

    if not isinstance(old_text, str) or old_text == "":
        return ToolResult(success=False, error="old_text must be a non-empty string")
    if not isinstance(new_text, str):
        return ToolResult(success=False, error="new_text must be a string")
    replace_all, replace_error = _coerce_bool(replace_all, "replace_all")
    if replace_error is not None:
        return ToolResult(success=False, error=replace_error)

    if _is_context_md(path, project_path):
        return ToolResult(
            success=False,
            error="context.md is append-only. write_file a single ## entry. The file was not changed.",
        )

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
        size = os.path.getsize(resolved)
        if size > 50 * 1024 * 1024:
            return ToolResult(success=False, error=f"File too large (>50MB): {path}")
        with open(resolved, "r", encoding="utf-8", newline="") as f:
            content = f.read()
    except PermissionError:
        return ToolResult(success=False, error=f"Permission denied: {path}")
    except UnicodeDecodeError:
        return ToolResult(success=False, error=f"File is not valid UTF-8: {path}")
    except OSError as e:
        return ToolResult(success=False, error=f"Cannot read {path}: {e}")

    needle = old_text
    if old_text not in content and "\r\n" in content and "\r\n" not in old_text:
        crlf = old_text.replace("\n", "\r\n")
        if crlf in content:
            needle = crlf
    replacement_text = new_text if needle == old_text else new_text.replace("\n", "\r\n")
    count = content.count(needle)
    new_content = content
    replaced = 0
    anchor = 0
    if count >= 1 and replace_all:
        anchor = content.find(needle)
        new_content = content.replace(needle, replacement_text)
        replaced = count
    elif count == 1:
        anchor = content.find(needle)
        new_content = content.replace(needle, replacement_text, 1)
        replaced = 1
    elif count > 1:
        sites = _occurrence_line_numbers(content, needle)
        listed = ", ".join(str(n) for n in sites[:8])
        extra = "" if len(sites) <= 8 else f" (+{len(sites) - 8} more)"
        return ToolResult(
            success=False,
            error=(
                f"old_text is not unique in {path} — found {count} occurrences "
                f"at lines {listed}{extra}.\n"
                f"Pass replace_all=true to change every one, or include more "
                f"surrounding lines so only one matches."
            ),
        )
    else:
        spans = _indent_flexible_spans(content, old_text)
        if len(spans) == 1:
            span_start, span_end, file_indent = spans[0]
            # Keep a trailing newline the file line already had.
            replacement = _reindent_block(new_text.replace("\r\n", "\n"), file_indent)
            if "\r\n" in content[span_start:span_end]:
                replacement = replacement.replace("\n", "\r\n")
            if content[span_start:span_end].endswith("\n") and not replacement.endswith("\n"):
                replacement += "\r\n" if "\r\n" in content[span_start:span_end] else "\n"
            new_content = content[:span_start] + replacement + content[span_end:]
            anchor = span_start
            replaced = 1
        elif len(spans) > 1:
            sites = [content.count("\n", 0, start) + 1 for start, _end, _ind in spans]
            listed = ", ".join(str(n) for n in sites[:8])
            return ToolResult(
                success=False,
                error=(
                    f"old_text matches {len(spans)} places in {path} once indentation "
                    f"is ignored, at lines {listed}.\n"
                    f"Include more surrounding lines so only one matches."
                ),
            )
        else:
            nearby = _nearby_edit_lines(content, old_text)
            detail = f"\n{nearby}" if nearby else ""
            return ToolResult(
                success=False,
                error=(
                    f"old_text not found in {path}.\n"
                    f"The number and bar in a read_file line are not part of the file. "
                    f"Whitespace and newlines matter."
                    f"{detail}"
                ),
            )

    try:
        byte_count = len(new_content.encode("utf-8"))
        if byte_count > MAX_WRITE_SIZE:
            return ToolResult(success=False, error=f"Resulting file too large ({byte_count} bytes, max {MAX_WRITE_SIZE})")
        with open(resolved, "w", encoding="utf-8", newline="") as f:
            f.write(new_content)
        word = "occurrence" if replaced == 1 else "occurrences"
        snippet = _snippet_around(new_content, anchor)
        return ToolResult(
            success=True,
            output=(
                f"OK — edited {path}\n"
                f"[enforcement:edit] {replaced} {word} replaced\n"
                f"{snippet}"
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
        lines: list[str] = []
        notice = f"[... truncated at {MAX_LIST_ENTRIES} entries ...]"

        def _at_cap() -> bool:
            # Checked before the next append so a directory of exactly
            # MAX_LIST_ENTRIES files is not labeled truncated.
            if len(lines) >= MAX_LIST_ENTRIES:
                lines.append(notice)
                return True
            return False

        if recursive:
            capped = False
            for root, dirs, files in os.walk(resolved):
                # Skip common noise directories
                dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".pytest_cache")]
                for name in sorted(files):
                    if _at_cap():
                        dirs[:] = []
                        capped = True
                        break
                    full = os.path.join(root, name)
                    try:
                        size = os.path.getsize(full)
                        rel = os.path.relpath(full, resolved)
                        lines.append(f"{rel}  {size}")
                    except OSError:
                        lines.append(f"{name}  [cannot stat]")
                if capped:
                    break
        else:
            for name in sorted(os.listdir(resolved)):
                if _at_cap():
                    break
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


def _rg_path() -> str | None:
    """Ripgrep binary, or None when the host does not have one."""
    return shutil.which("rg")


def _run_grep(
    pattern: str,
    search_root: str,
    file_type: str | None = None,
    timeout: int = 10,
    target: str = ".",
) -> tuple[int, str, str]:
    """Run ripgrep, or GNU grep when ripgrep is absent.

    Shared by _search_files and _file_search. Output is ``path:line:content``
    either way. ``target`` is ``.`` for a directory and the file's basename
    when ``path`` is a file (a file cannot be the working directory).
    Ripgrep is preferred because its regex is the one models write and a
    rejected pattern comes back as its diagnostic. It also skips hidden
    files. ``.gitignore`` is applied again by the caller, because ripgrep
    honors it only inside a git work tree. ``--`` keeps a pattern that
    starts with ``-`` from becoming a flag. ``-r`` is recursive only for
    GNU grep; ripgrep uses ``-r`` to mean replace, so that flag is not shared.
    """
    if not search_root or not isinstance(search_root, str):
        raise ValueError(f"search_root must be a non-empty string, got {search_root!r}")
    if not target or not isinstance(target, str):
        raise ValueError(f"target must be a non-empty string, got {target!r}")
    rg = _rg_path()
    if rg:
        cmd = [
            rg, "-n", "-H", "--color=never", "--no-config",
            "--max-columns", "500", "--max-columns-preview",
        ]
        if file_type:
            cmd += ["--glob", "*." + file_type.lstrip(".")]
    else:
        cmd = ["grep", "-n", "-H", "--directories=skip", "-r", "--exclude-dir=.git"]
        if file_type:
            cmd += ["--include=*." + file_type.lstrip(".")]
    cmd += ["--", pattern, target]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            cwd=search_root,
            text=True,
        )
    except FileNotFoundError:
        if not rg:
            raise
        cmd = ["grep", "-n", "-H", "--directories=skip", "-r", "--exclude-dir=.git"]
        if file_type:
            cmd += ["--include=*." + file_type.lstrip(".")]
        cmd += ["--", pattern, target]
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            cwd=search_root,
            text=True,
        )
    return result.returncode, result.stdout, result.stderr


def _norm_search_rel(path: str) -> str:
    """Project-relative path with a leading ``./`` stripped."""
    rel = path.replace("\\", "/")
    if rel.startswith("./"):
        rel = rel[2:]
    return rel


def _search_root_rel(search_root: str, project_path: str) -> str:
    """``search_root`` relative to the project, or ``""`` at the project root."""
    root_rel = os.path.relpath(search_root, os.path.realpath(project_path))
    root_rel = _norm_search_rel(root_rel)
    if root_rel == ".":
        return ""
    return root_rel


def _under_search_root(rel_path: str, root_rel: str) -> bool:
    if not root_rel:
        return True
    return rel_path == root_rel or rel_path.startswith(root_rel + "/")


def _matches_file_type(rel_path: str, file_type: str | None) -> bool:
    if not file_type:
        return True
    ext = file_type.lower().lstrip(".")
    return rel_path.lower().endswith("." + ext)


def _project_rel_from_grep(grep_path: str, root_rel: str) -> str:
    rel = _norm_search_rel(grep_path)
    if not root_rel or rel == root_rel or rel.startswith(root_rel + "/"):
        return rel
    return f"{root_rel}/{rel}"


def _match_count_header(rel: str, size_str: str, shown: int, total: int,
                        name_only: bool = False) -> str:
    """Per-file group header with the true match count.

    ``shown`` is how many match lines the preview cap let through; ``total``
    is the file's true count of matching LINES (not occurrences). When the
    cap truncated, the header says so: "3 of 57 matches". Otherwise:
    "3 matches".

    ``name_only`` is True for a file that matched only by filename. Those
    files have no content matches; they are labeled "name match" rather
    than "0 matches" so a case-insensitive name hit is not misread as a
    file with zero hits.
    """
    if name_only:
        return f"{rel} ({size_str}, name match)"
    if total <= 0:
        return f"{rel} ({size_str}, 0 matches)"
    if shown < total:
        return f"{rel} ({size_str}, {shown} of {total} matches)"
    return f"{rel} ({size_str}, {total} matches)"


def _coerce_bool(value, name: str) -> tuple[bool, str | None]:
    """Parse a tool boolean. The string ``\"false\"`` is false, not true."""
    if isinstance(value, bool):
        return value, None
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value), None
        return False, f"{name} must be a boolean"
    if isinstance(value, str):
        folded = value.strip().lower()
        if folded in ("true", "1"):
            return True, None
        if folded in ("false", "0"):
            return False, None
    return False, f"{name} must be a boolean"


def _search_summary(n_matches: int, n_files: int, unlisted: int) -> str:
    """First output line. Literal plurals, marker only when files were dropped."""
    line = f"[{n_matches} matches in {n_files} files]"
    if unlisted > 0:
        line += f" (+{unlisted} more files not listed)"
    return line


def _directory_search_skips(rel: str, project_path: str, patterns: list[str]) -> bool:
    """True when a directory search must not report this path.

    Hidden names and ``.git`` are skipped on both engines. ``.gitignore``
    uses the same matcher as filename search. An explicit file path is not
    passed through here, so a caller can still open an ignored file.
    """
    from agent.context import _is_ignored

    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p.startswith(".") for p in parts):
        return True
    return _is_ignored(rel, project_path, patterns)


def _search_mtime_key(project_real: str, rel: str) -> tuple[float, str]:
    """Newest file first. Equal times break ties by path so the order is stable."""
    full = os.path.join(project_real, *rel.split("/"))
    try:
        mtime = os.stat(full).st_mtime
    except OSError:
        mtime = float("-inf")
    return (-mtime, rel)


def _merged_line_windows(linenos: list[int], radius: int) -> list[tuple[int, int]]:
    """1-based inclusive windows. Overlapping or touching windows become one."""
    merged: list[tuple[int, int]] = []
    for lineno in sorted(linenos):
        start, end = max(1, lineno - radius), lineno + radius
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _context_slice(path: str, start_line: int, end_line: int) -> dict[int, str] | None:
    """Lines ``start_line``..``end_line`` without reading the rest of the file.

    None when the file cannot be opened. A line past EOF is absent. A line
    longer than 500 characters is cut and marked, matching ripgrep's column cap.
    """
    start_line = max(1, start_line)
    if end_line < start_line:
        return {}
    try:
        offset = _start_byte_for_line(path, start_line)
    except OSError:
        return None
    if offset is None:
        return {}
    out: dict[int, str] = {}
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            lineno = start_line
            while lineno <= end_line:
                raw = fh.readline(4096)
                if not raw:
                    break
                if len(raw) >= 4096 and not raw.endswith(b"\n"):
                    while True:
                        more = fh.readline(4096)
                        if not more or more.endswith(b"\n"):
                            break
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if len(text) > 500:
                    text = text[:500] + " [... line cut ...]"
                out[lineno] = text
                lineno += 1
    except OSError:
        return None
    return out


def _cut_search_line(text: str) -> str:
    """Cap a displayed line. Ripgrep's own cut marker is left intact."""
    if len(text) <= 500 or "omitted end of long line" in text:
        return text
    return text[:500] + " [... line cut ...]"


def _emit_search_hits(
    rel: str,
    full_path: str,
    previewed: list[tuple[int, str]],
    radius: int,
    budget: int = SEARCH_FILE_CHARS,
) -> list[str]:
    """Match lines, plus neighbors when ``radius`` is positive.

    A match line keeps grep's stripped text. A neighbor keeps the file's
    leading whitespace and is marked with a dash so it is not a ``Line`` hit.
    If the file cannot be read, only the match lines are returned. The
    context radius shrinks to fit ``budget``; match lines are never dropped
    to make room for context.
    """
    match_text = {
        lineno: _cut_search_line(content.strip())
        for lineno, content in previewed
    }
    plain = [f"  Line {n}: {match_text[n]}" for n in sorted(match_text)]
    if radius <= 0 or not match_text:
        return plain
    # Read the file once at the largest radius. A smaller radius's windows
    # are subsets of this one, so shrinking only trims this already-read
    # slice down to fewer lines — no re-open, no re-seek.
    windows = _merged_line_windows(list(match_text), radius)
    sliced: dict[int, str] = {}
    for start, end in windows:
        got = _context_slice(full_path, start, end)
        if got is None:
            return plain
        sliced.update(got)
    # Every merged window gets its own allowance so a wide window cannot
    # starve a narrow one in the same file. Each window renders at its own
    # radius and shrinks only that radius until it fits its allowance.
    allowance = budget // len(windows) if windows else budget
    parts: list[list[str]] = []
    for start, end in windows:
        cluster = [n for n in sorted(match_text) if start <= n <= end]
        r = radius
        while r > 0:
            sub_windows = _merged_line_windows(cluster, r)
            out: list[str] = []
            for s, e in sub_windows:
                for n in range(s, e + 1):
                    if n in match_text:
                        out.append(f"  Line {n}: {match_text[n]}")
                    elif n in sliced:
                        out.append(f"  {rel}-{n}:{sliced[n]}")
            if len("\n".join(out)) <= allowance:
                break
            r -= 1
        if r == 0:
            parts.append([f"  Line {n}: {match_text[n]}" for n in cluster])
        else:
            parts.append(out)
    return [line for part in parts for line in part]


def _search_files(
    pattern: str,
    project_path: str,
    path: str | None = None,
    file_type: str | None = None,
    max_results: int = 20,
    preview_lines: int = 40,
    count_only: bool = False,
) -> ToolResult:
    """Name-or-content search. One grep, grouped by file.

    Content matches come from ``_run_grep``. Filename matches come from
    ``_find_matching_files`` (a name fragment). A file ``path`` is grepped
    as that file; it is not used as the working directory. Each hit is path, size from
    ``stat``, a match count, and at most 40 preview lines taken from the grep hit.
    The count is the file's true number of matching LINES, reported in the header
    even when the preview cap hides the rest. A file is opened only when
    it has one to three content matches. Surrounding lines are shown when
    the search-wide budget allows; match lines are always shown.
    ``count_only=True`` returns the headers without
    any content lines and without the footer. ``file_search`` calls this function.
    Sandbox, timeout, and ``file_type`` stay on this path.
    """
    if not isinstance(pattern, str) or not pattern.strip():
        return ToolResult(success=False, error="empty query")
    if not isinstance(max_results, int) or isinstance(max_results, bool) or max_results <= 0:
        max_results = 20
    if not isinstance(preview_lines, int) or isinstance(preview_lines, bool) or preview_lines <= 0:
        preview_lines = 40
    preview_lines = min(preview_lines, 40)
    count_only, count_error = _coerce_bool(count_only, "count_only")
    if count_error is not None:
        return ToolResult(success=False, error=count_error)

    search_root = _resolve_project_path(path or ".", project_path)
    if search_root is None:
        return ToolResult(
            success=False,
            error=f"Path escapes project sandbox: {path or '.'}",
        )

    from agent.context import _find_matching_files, _load_gitignore_patterns

    # A file path cannot be grep's cwd (Errno 20). Search that file from
    # its parent, and do not walk sibling names into the result.
    file_rel: str | None = None
    if os.path.isfile(search_root):
        file_rel = _search_root_rel(search_root, project_path)
        grep_cwd = os.path.dirname(search_root)
        grep_target = os.path.basename(search_root)
        root_rel = _search_root_rel(grep_cwd, project_path)
    elif os.path.isdir(search_root):
        grep_cwd = search_root
        grep_target = "."
        root_rel = _search_root_rel(search_root, project_path)
    else:
        return ToolResult(
            success=False,
            error=f"Not a file or directory: {path or '.'}",
        )

    if file_rel is not None:
        base = os.path.basename(file_rel)
        name_matches = []
        if pattern.lower() in base.lower() and _matches_file_type(file_rel, file_type):
            name_matches = [_norm_search_rel(file_rel)]
    else:
        patterns = _load_gitignore_patterns(project_path)
        name_matches = [
            _norm_search_rel(rel)
            for rel in _find_matching_files(
                project_path, pattern, patterns, max_files=max_results,
                file_type=file_type, root_rel=root_rel,
            )
        ]
        name_matches = [
            rel for rel in name_matches
            if _under_search_root(rel, root_rel) and _matches_file_type(rel, file_type)
        ]

    try:
        if file_rel is not None and not _matches_file_type(file_rel, file_type):
            # --include drops nothing useful here: the only candidate is
            # this file, and it failed the extension filter.
            returncode, stdout, stderr = 1, "", ""
        else:
            # An explicit file plus --include is grep-version-dependent.
            # The extension check above already applied file_type.
            returncode, stdout, stderr = _run_grep(
                pattern,
                grep_cwd,
                None if file_rel is not None else file_type,
                target=grep_target,
            )
    except subprocess.TimeoutExpired:
        return ToolResult(success=False, error="Search timed out after 10s")
    except FileNotFoundError:
        return ToolResult(success=False, error="grep not found on this system")
    except OSError as e:
        return ToolResult(success=False, error=f"Search failed: {e}")

    if returncode not in (0, 1):
        detail = (stderr or "").strip()[:400]
        if detail:
            return ToolResult(success=False, error=detail)
        return ToolResult(
            success=False,
            error=f"search exited with {returncode}",
        )

    grep_hits: dict[str, list[tuple[int, str]]] = {}
    if returncode == 0 and stdout:
        for line in stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            fpath, lineno_s, content = parts
            try:
                lineno = int(lineno_s)
            except ValueError:
                continue
            rel = _project_rel_from_grep(fpath, root_rel)
            grep_hits.setdefault(rel, []).append((lineno, content))

    if file_rel is None:
        grep_hits = {
            rel: hits
            for rel, hits in grep_hits.items()
            if not _directory_search_skips(rel, project_path, patterns)
        }

    ordered: list[str] = []
    seen: set[str] = set()
    for rel in list(name_matches) + list(grep_hits):
        if rel in seen:
            continue
        seen.add(rel)
        ordered.append(rel)
    project_real = os.path.realpath(project_path)
    # file_search forwards any max_results unchecked; cap the EMITTED file
    # count (MAX_SEARCH_FILES) so a huge match set cannot push the joined
    # output over CONV_OUTPUT_CAP through headers alone. Names are still
    # gathered with the caller's max_results so a small explicit cap (e.g. 30
    # over 26 candidates) is honoured for sorting before the emission cap.
    ordered = sorted(
        ordered, key=lambda rel: _search_mtime_key(project_real, rel)
    )[:min(max_results, MAX_SEARCH_FILES)]
    if not ordered:
        return ToolResult(success=True, output="(no matches)")

    name_only_count = sum(1 for rel in name_matches if not grep_hits.get(rel))
    total_matched = len(grep_hits) + name_only_count
    n_matches = sum(len(v) for v in grep_hits.values()) + name_only_count
    unlisted = total_matched - len(ordered)

    # Hold back room for the trailing blank line and footer so they never tip
    # the joined output over SEARCH_OUTPUT_CHARS. The summary is short enough
    # to sit inside the remaining headroom below CONV_OUTPUT_CAP.
    tail_reserve = len("\n[Open with read_file line=N only to edit.]") + 1
    # Precompute every header cost. Deep rel prefixes appear in headers and in
    # every context-line prefix; reserving only what is spent so far lets the
    # headers still to come push the joined output over SEARCH_OUTPUT_CHARS
    # again. The block budget therefore reserves every header up front.
    header_costs: list[int] = []
    plan: list[tuple[str, str, str, list[tuple[int, str]], bool]] = []
    for rel in ordered:
        full_path = os.path.join(project_real, *rel.split("/"))
        try:
            size = os.stat(full_path).st_size
        except OSError:
            size_str = "?KB"
        else:
            if size < 1024 * 1024:
                size_str = f"{size // 1024}KB"
            else:
                size_str = f"{size // (1024 * 1024)}MB"
        hits = grep_hits.get(rel) or []
        total = len(hits)
        shown = total if count_only else len(hits[:preview_lines])
        name_only = (not hits) and (rel in name_matches)
        header = _match_count_header(rel, size_str, shown, total, name_only=name_only)
        header_costs.append(len(header) + 1)
        plan.append((rel, full_path, header, hits, name_only))

    # Dynamic file cap (Phase 3b): derive how many planned files fit inside
    # the joined-output budget from the header costs already computed, instead
    # of a fixed number. A static MAX_SEARCH_FILES is safe at depth 8 but
    # throws away reportable files at depth 1 (56 fit there). Conservative:
    # charge every file the largest per-file minimum (header + match line for
    # content-hit entries, header alone for count_only / name-only entries).
    if plan:
        if count_only:
            per_file_min = max(header_costs)
        else:
            per_file_min = max(
                cost + (0 if entry[4] else SEARCH_MATCH_LINE_ALLOWANCE)
                for cost, entry in zip(header_costs, plan)
            )
        if per_file_min > 0:
            budget = (
                SEARCH_OUTPUT_CHARS
                - tail_reserve
                - len(_search_summary(n_matches, total_matched, unlisted))
                - 1
            )
            affordable = max(1, min(len(plan), budget // per_file_min))
            if affordable < len(plan):
                plan = plan[:affordable]
                header_costs = header_costs[:affordable]

    # RECOMPUTE the omission count and summary AFTER the dynamic slice so the
    # (+N more files not listed) marker reflects the derived listing count.
    unlisted = total_matched - len(plan)
    lines: list[str] = [_search_summary(n_matches, total_matched, unlisted)]
    spent = 0
    for idx, (rel, full_path, header, hits, name_only) in enumerate(plan):
        lines.append(header)
        spent += len(header) + 1
        previewed = hits[:preview_lines]
        if count_only or not previewed or len(previewed) > 3:
            radius = 0
        elif len(previewed) == 1:
            radius = SEARCH_CONTEXT_ONE
        else:
            radius = SEARCH_CONTEXT_FEW
        if count_only:
            continue
        if hits:
            # Match lines are non-negotiable and always emit; only context is
            # sacrificed once the whole joined output approaches
            # SEARCH_OUTPUT_CHARS. The headers still to come are reserved too,
            # so deep rel prefixes cannot push the emitted string over
            # CONV_OUTPUT_CAP and make bound_tool_output drop match lines from
            # the middle.
            remaining = (
                SEARCH_OUTPUT_CHARS - spent - sum(header_costs[idx + 1:]) - tail_reserve
            )
            block_budget = min(SEARCH_FILE_CHARS, max(0, remaining))
            if remaining <= 0:
                radius = 0
            block = _emit_search_hits(rel, full_path, previewed, radius, block_budget)
            lines.extend(block)
            spent += sum(len(s) + 1 for s in block)
        else:
            lines.append("  [name match only — use read_file for content]")

    if not count_only:
        lines.append("")
        lines.append("[Open with read_file line=N only to edit.]")
    output = "\n".join(lines)
    if len(output) > MAX_EXEC_OUTPUT:
        output = output[:MAX_EXEC_OUTPUT] + "\n[... truncated ...]"
    return ToolResult(success=True, output=output)


def _file_search(
    query: str,
    project_path: str,
    file_type: str | None = None,
    max_results: int = 20,
    preview_lines: int = 40,
) -> ToolResult:
    """Old name for ``_search_files``. One code path, no second grep."""
    return _search_files(
        query,
        project_path,
        path=None,
        file_type=file_type,
        max_results=max_results or 20,
        preview_lines=preview_lines,
    )


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
                "Read a file as lines. line is the 1-based start (default 1). "
                "max_lines is the count (1-200, default 200) and overrides limit. "
                "limit is the line count when max_lines is omitted. "
                "Each line is N|text. The number and the bar are not part of the file. "
                "A cut window says continue with line=N."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path"},
                    "line": {"type": "integer", "description": "1-based start line (default 1)"},
                    "max_lines": {"type": "integer", "description": "Line count, 1-200 (default 200). Overrides limit"},
                    "limit": {"type": "integer", "description": "Line count when max_lines is omitted, 1-200"},
                },
                "required": ["path"],
            },
            # NOTE: descriptions below stay one line each; do not expand.
            requires_approval=False,
        ),
        lambda path, project_path, limit=None, line=None, max_lines=None, **kwargs:  # type: ignore
            _read_file(path, project_path, None, limit, line, max_lines),
    )

    # write_file
    _TOOLS["write_file"] = (
        ToolDefinition(
            name="write_file",
            description=(
                "Write a file (max 2MB), creating parent dirs; overwrites. "
                "Full rewrites only — read_file returns a window, so use "
                "edit_file for targeted changes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path"},
                    "content": {"type": "string", "description": "Text to write"},
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
                "Replace old_text with new_text. Exact and unique unless "
                "replace_all is true. A miss shows nearby lines. If the lines "
                "match once with different indentation, that one match is applied. "
                "The result includes numbered lines around the change. "
                "read_file returns a window — use write_file for a full "
                "rewrite you already have in hand."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path"},
                    "old_text": {"type": "string", "description": "Exact text to replace; must be unique unless replace_all"},
                    "new_text": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace every exact match. Default false."},
                },
                "required": ["path", "old_text", "new_text"],
            },
            requires_approval=False,
        ),
        lambda path, old_text, new_text, project_path, replace_all=False, **kwargs:  # type: ignore
            _edit_file(path, old_text, new_text, project_path, session_key=kwargs.get("session_key", ""), approval_callback=kwargs.get("approval_callback"), replace_all=replace_all),
    )

    # exec_command
    _TOOLS["exec_command"] = (
        ToolDefinition(
            name="exec_command",
            description=(
                "Run a shell command; needs PM approval per call (dangerous "
                "ones denied). Returns output, stdout, stderr, exit_code, "
                "error. Timeout 30s (max 120); output truncated at 100KB. "
                "Coder is limited to git status/diff/log, py_compile, and pytest on one tests/ path."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Seconds; default 30, max 120"},
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
                "List files/subdirs with sizes; recursive=True for full tree. "
                "Skips .git, node_modules, __pycache__, .pytest_cache."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path (default '.')"},
                    "recursive": {"type": "boolean", "description": "Recurse (default False)"},
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
                "Find files by name or content. Skips ignored and hidden files. "
                "Files with one to three hits include surrounding lines, budget permitting. "
                "First line is the true total, "
                "including files not listed. Per file: path, size, match count, "
                "up to 40 matching lines. count_only: counts only, no lines. "
                "Times out after 10s."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Text or regex"},
                    "path": {"type": "string", "description": "File or directory to search (default '.')"},
                    "file_type": {"type": "string", "description": "Extension filter, e.g. 'py'"},
                    "count_only": {"type": "boolean", "description": "Counts only, no lines. Header shows true total."},
                },
                "required": ["pattern"],
            },
            requires_approval=False,
        ),
        lambda pattern, project_path, path=None, file_type=None, count_only=False, **kwargs:  # type: ignore
            _search_files(pattern, project_path, path, file_type, count_only=count_only),
    )

    # file_search
    _TOOLS["file_search"] = (
        ToolDefinition(
            name="file_search",
            description=(
                "Find files by name or content; grouped per file with size and up to\n"
                "40 matching lines. Reads no full bodies — open with read_file line=N only to edit.\n"
                "search_files is plain grep."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Filename fragment or regex"},
                    "file_type": {"type": "string", "description": "Extension filter, e.g. 'py'"},
                    "max_results": {"type": "integer", "description": "Max files to return (default 20)"},
                },
                "required": ["query"],
            },
            requires_approval=False,
        ),
        lambda query, project_path, file_type=None, max_results=None, **kwargs:  # type: ignore
            _search_files(
                query, project_path, file_type=file_type, max_results=max_results or 20,
            ),
    )

    # web_search
    _TOOLS["web_search"] = (
        ToolDefinition(
            name="web_search",
            description=(
                "Search the web via Brave. Returns title, URL, and snippet per\n"
                "result, 5 by default and 10 max. Needs BRAVE_API_KEY. For\n"
                "project files use search_files; to read a URL use web_fetch."
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
                "Fetch a URL and return its readable text (HTML tags stripped, "
                "10,000 chars by default). HTML/text pages only — not PDFs or "
                "binaries. Local files: read_file."
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

    # provider_events — SPEC-22 read-only failure log for Supervisor / Coder
    _TOOLS["provider_events"] = (
        ToolDefinition(
            name="provider_events",
            description=(
                "Read-only provider failure log: auth, rate limit, model rejection, "
                "malformed request. Answers is-an-agent-alive / why-a-turn-stopped."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_key": {
                        "type": "string",
                        "description": "Session key filter, e.g. special:coder",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Recent events to return (default 20)",
                    },
                },
                "required": [],
            },
            requires_approval=False,
        ),
        lambda session_key=None, limit=20, **kwargs: _provider_events(session_key, limit),  # type: ignore
    )

    from agent import group_tools as _group

    _TOOLS["agent_context"] = (
        ToolDefinition(
            name="agent_context",
            description=(
                "Edit this agent's own working context, or a peer's when the "
                "caller is the supervisor. inspect lists message indexes and "
                "sizes. edit drops or keeps those indexes. A tool call and its "
                "results move together. On yourself the running tool call stays, "
                "and the edited conversation is written back to the conversation file. "
                "clear and compact still reset or summarize a peer."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "self, or a peer display name or role",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["clear", "compact", "inspect", "edit"],
                    },
                    "focus": {"type": "string", "description": "What compact should keep"},
                    "drop": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "edit only. Indexes to remove. Integers or inclusive "
                            "ranges like \"4-12\". Do not pass with keep."
                        ),
                    },
                    "keep": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "edit only. Indexes to keep. Other unlocked messages "
                            "are removed. Do not pass with drop."
                        ),
                    },
                },
                "required": ["agent", "action"],
            },
            requires_approval=False,
        ),
        lambda agent, action, focus="", **kwargs: _group.agent_context(  # type: ignore
            agent, action, focus, **kwargs
        ),
    )

    _TOOLS["diary"] = (
        ToolDefinition(
            name="diary",
            description=(
                "Append or search the project diary (.crabcakes/context.md). "
                "remember needs a ## heading. recall searches entries."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["remember", "recall"]},
                    "heading": {"type": "string", "description": "Must start with ## "},
                    "body": {"type": "string"},
                    "query": {"type": "string"},
                },
                "required": ["action"],
            },
            requires_approval=False,
        ),
        lambda action, heading="", body="", query="", **kwargs: _group.diary(  # type: ignore
            action, heading, body, query, **kwargs
        ),
    )

    _TOOLS["project_memory"] = (
        ToolDefinition(
            name="project_memory",
            description=(
                "Long-term project memory. log and search are open. "
                "promote and supersede are supervisor-only. "
                "For supersede, name is the old slug and body is the replacement slug."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["log", "search", "read", "promote", "supersede"],
                    },
                    "name": {"type": "string"},
                    "body": {"type": "string"},
                    "query": {"type": "string"},
                    "scope": {"type": "string", "enum": ["shared", "private"]},
                },
                "required": ["action"],
            },
            requires_approval=False,
        ),
        lambda action, name="", body="", query="", scope="shared", **kwargs: (  # type: ignore
            _group.project_memory(action, name, body, query, scope, **kwargs)
        ),
    )

    _TOOLS["jev_ask"] = (
        ToolDefinition(
            name="jev_ask",
            description=(
                "Ask the decision model one closed question. "
                "2–5 options. decisive false or no decision means do not treat it as a verdict."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "state": {"type": "string", "description": "The situation, max 1000 chars"},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "meaning": {"type": "string"},
                            },
                            "required": ["name", "meaning"],
                        },
                    },
                },
                "required": ["state", "options"],
            },
            requires_approval=False,
        ),
        lambda state, options=None, **kwargs: _group.jev_ask(state, options, **kwargs),  # type: ignore
    )

    _TOOLS["canvas"] = (
        ToolDefinition(
            name="canvas",
            description=(
                "Open or focus a notebook tab and run an HTML, CSS, and "
                "JavaScript document there. The html is not returned."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "html": {"type": "string"},
                },
                "required": ["name", "html"],
            },
            requires_approval=False,
        ),
        lambda name, html, **kwargs: _group.canvas(name, html, **kwargs),  # type: ignore
    )

    _TOOLS["talk"] = (
        ToolDefinition(
            name="talk",
            description=(
                "Speak one line through the speech endpoint saved in Settings. "
                "Fails when speech is not configured."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
            requires_approval=False,
        ),
        lambda text, **kwargs: _group.talk(text, **kwargs),  # type: ignore
    )

    _TOOLS["feed_card"] = (
        ToolDefinition(
            name="feed_card",
            description=(
                "Post a card to the project Feed. type is diff, agent_action, "
                "or audit_report. Returns the new card id."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["diff", "agent_action", "audit_report"],
                    },
                    "title": {"type": "string", "description": "One line, max 200 chars"},
                    "body": {"type": "string", "description": "Card text, max 100,000 chars"},
                    "file": {"type": "string", "description": "Project-relative path"},
                    "additions": {"type": "integer", "description": "Lines added"},
                    "deletions": {"type": "integer", "description": "Lines removed"},
                    "task_id": {"type": "string", "description": "Optional work unit id"},
                },
                "required": ["type", "title"],
            },
            requires_approval=False,
        ),
        lambda type, title, body="", file="", additions=None, deletions=None,  # type: ignore
               task_id="", **kwargs: _group.feed_card(
            type, title, body, file, additions, deletions, task_id, **kwargs
        ),
    )


def _provider_events(session_key: str | None = None, limit: int = 20):
    """Format the provider-failure store for an agent tool call."""
    from utils.provider_failure_store import recent, summary
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 20
    cap = min(max(cap, 1), 100)
    key = session_key.strip() if isinstance(session_key, str) and session_key.strip() else None
    events = recent(limit=cap, session_key=key)
    totals = summary()
    if not events:
        return ToolResult(success=True, output="No provider failures recorded.")
    lines = [
        (
            f"Provider failures in memory: {totals.get('last_24h', 0)} in the last 24h "
            "(store cap 100)."
        ),
    ]
    for item in events:
        code = item.status_code if item.status_code is not None else "-"
        lines.append(
            f"- {item.failure_class.value} provider={item.provider or '?'} "
            f"model={item.model or '?'} status={code} stage={item.stage} "
            f"session={item.session_key} detail={item.detail}"
        )
    return ToolResult(success=True, output="\n".join(lines))


_register_tools()


# ── Public API ─────────────────────────────────────────────────────────────────

def bound_tool_output(result: ToolResult, name: str = "") -> ToolResult:
    """Head/tail a model-facing output that exceeds CONV_OUTPUT_CAP.

    stdout, stderr, error, and exit_code are left alone so exec_command's
    100KB process-capture contract stays on those fields. read_file is
    already a byte window; bounding it would hide the continuation offset.
    """
    if not isinstance(result, ToolResult):
        raise ValueError(f"result must be a ToolResult, got {type(result).__name__}")
    if not isinstance(name, str):
        raise ValueError(f"name must be a str, got {type(name).__name__}")
    if name in _ALREADY_WINDOWED:
        return result
    output = result.output
    if not isinstance(output, str) or len(output) <= CONV_OUTPUT_CAP:
        return result
    omitted = len(output) - CONV_OUTPUT_HEAD - CONV_OUTPUT_TAIL
    bounded = (
        output[:CONV_OUTPUT_HEAD]
        + f"\n[... {omitted} chars omitted ...]\n"
        + output[-CONV_OUTPUT_TAIL:]
    )
    return dataclasses.replace(result, output=bounded)


def get_all_tools() -> list[ToolDefinition]:
    """Return all available tool definitions."""
    return [defn for defn, _ in _TOOLS.values()]


# file_search stays registered so execute_tool can run calls already stored
# in old transcripts. The model sees search_files only.
_API_SCHEMA_HIDDEN = frozenset({"file_search"})


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
    tools_to_include = [t for t in tools_to_include if t.name not in _API_SCHEMA_HIDDEN]

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
            return bound_tool_output(
                ToolResult(
                    success=mcp_result.success,
                    output=mcp_result.output,
                    error=mcp_result.error,
                    duration_ms=mcp_result.duration_ms,
                ),
                name,
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
        return bound_tool_output(result, name)

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
