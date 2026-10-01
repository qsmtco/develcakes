# utils/status_report.py
# develcakes status reporter — read-only observability over the files the app
# already writes. AGENTCTRL1 Phase 1a (SPEC-AGENT-CONTROL-1 §2.1–§2.4, §4.4).
#
# Architecture: pure Python, headless, NO GTK, NO imports from ui/. Read-only
# against existing files: the reporter never mutates app state, never takes the
# feed flock, and never writes outside its own cache directory. It is correct
# with the app running or closed.
#
# Content policy (spec §2.1 / PM decision §11.3):
#   * message bodies are capped at BODY_CAP_DEFAULT (120) chars by default and
#     BODY_CAP_FULL (2000) when the caller asks for full bodies;
#   * tool arguments, exec commands, and tool/command outputs are NEVER stored
#     or rendered in full at any verbosity — only the tool NAME is reported.
#     The commands live in feed-card titles/bodies and in `metadata.tool_args`,
#     so neither is ever copied into the report.
#
# Exit-code contract (see `assess`): 0 healthy / 2 attention / 3 app not running.
# An "approval entry" in the audit log is a record carrying a recorded decision
# (`approved` is true/false) — the spec is silent on this predicate; records with
# `approved: null` belong to tools that never needed an approval.
# Stall classes (§2.1): turn_stalled, blocked_on_sendback, app_spinning,
# approvals_pending, crash_after_start.
# Read-safety (§2.1): tolerant parse of torn conversation JSON, no feed flock.
# Content policy (§11.3): message bodies capped; tool args/commands never shown.

import hashlib
import json
import math
import os
import re
import subprocess
import time
import uuid
from datetime import datetime

from utils import feed_store
from utils.config import get_config_dir
from utils.git_ops import get_branch, get_head_sha, is_repo, status_porcelain

_logger = __import__("logging").getLogger(__name__)


# ── Tunables (spec §2.1/§2.4; module constants so callers/tests can pin them) ─

STALL_THRESHOLD_MINUTES = 10          # turn_stalled idle floor
BODY_CAP_DEFAULT = 120                # message bodies, default verbosity
BODY_CAP_FULL = 2000                  # message bodies, --full (BODIES ONLY)
COMMAND_CAP = 120                     # commands/outputs: cap at EVERY verbosity
FEED_TAIL_CARDS = 20                  # activity cards summarised per report
APPROVAL_WINDOW_MINUTES = 30          # approvals_pending window
APPROVAL_PENDING_MIN = 3              # approvals_pending entry threshold
SAMPLE_INTERVAL_SEC = 0.5             # second CPU sample spacing (spinning check)
ALERT_REALERT_HOURS = 1.0             # §2.4 re-alert floor, per episode
WALK_FILE_CAP = 5000                  # bounded repo write-scan (ruling 4)
AUDIT_TAIL_BYTES = 512 * 1024         # audit-log.jsonl tail read
CRASH_HEAD_BYTES = 64 * 1024          # /var/crash/*.crash head read (files are ~14 MB)
CRASH_MAX_REPORTS = 5
CONVERSATION_MAX_BYTES = 64 * 1024 * 1024   # per-conversation read cap (BUG #5)
HEAD_META_BYTES = 8192                # head scan for project_path/agent_name
CACHE_FILENAME = "status-feed.json"
STATE_FILENAME = "status-state.json"
SESSION_FILE_PREFIX = "special:"      # agent session files in <config>/conversations
PROC_ROOT = "/proc"                   # pinned by tests
CRASH_DIR = "/var/crash"              # pinned by tests

# Declared entry points for this app (audit BUG #1). pyproject.toml:
#   [project.scripts]
#   crabcakes = "main:main"
# so the sanctioned launcher is a file named `crabcakes`, NOT `main.py` — the
# old `main.py`-only match reported a running app as "app not running". Kept as
# a module constant (rather than parsing pyproject on every scan) so it is
# pinnable by tests; it must stay in sync with pyproject's [project.scripts].
APP_SCRIPT_NAMES = frozenset({"main.py", "crabcakes"})

# Worst-first for `assess()`: a stopped app is fully wedged, then a spinning
# app, then an idle pipeline.
STALL_CLASS_ORDER = (
    "app_frozen",
    "app_spinning",
    "turn_stalled",
    "blocked_on_sendback",
    "approvals_pending",
    "crash_after_start",
)

# Write-scan exclusions. `.git`/`.crabcakes` per the phase instructions;
# bytecode/cache trees and vendored dependency trees are excluded too, because
# interpreter/packager churn (a test run writes thousands of .pyc files; an
# install rewrites .venv) would otherwise mask a genuinely idle agent and push
# the scan into its file cap (audit BUG #4).
WRITE_SCAN_EXCLUDED_DIRS = frozenset({
    ".git", ".crabcakes", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".venv", "venv", "node_modules", ".tox",
})


# ── Small pure helpers ───────────────────────────────────────────────────────

def _require_number(value, name):
    """Coerce a numeric argument, rejecting bool/None/str/non-finite (Rule 6).

    NaN/±Infinity are rejected deliberately (audit BUG #8): a single NaN in
    `now` would persist as `last_alert: NaN` and the `now - last_alert >= floor`
    comparison would be False forever, permanently muting that episode's alerts.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return float(value)


def _cap(text, limit):
    """Hard-cap `text` to `limit` characters. Never adds an ellipsis (the cap is
    the cap — callers that need to know it truncated compare lengths)."""
    if not isinstance(text, str):
        return ""
    if limit is None or limit < 0:
        return text
    return text[:limit]


def _sha16(text):
    """Stable short hash for safe reporting / episode identity (no content)."""
    if text is None:
        return ""
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()[:16]


def _iso(ts):
    """ISO-8601 local timestamp for a float epoch (or None)."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _parse_message_ts(value):
    """Parse a conversation message timestamp (ISO, local-naive) → epoch float."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _head_metadata(path, max_bytes=None):
    """Best-effort `(project_path, agent_name)` from the head of a huge file.

    `save_conversation_to_disk` writes both keys *before* `messages` (measured:
    byte ~64 in every live session file), so the head carries them even when the
    body is far past `CONVERSATION_MAX_BYTES`. (SPEC-08 SP2 dual-write does not
    change this: the JSON write order is unchanged; the store append happens
    after, and delta-based.) Only the oversize path uses this
    (audit BUG #5): without it an over-cap session loses its project attribution
    and silently drops out of stall detection.
    """
    if max_bytes is None:
        max_bytes = HEAD_META_BYTES
    text = _read_text_head(path, max_bytes)
    if not text:
        return None, None
    values = []
    for key in ("project_path", "agent_name"):
        match = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % key, text)
        value = None
        if match is not None:
            try:
                value = json.loads('"%s"' % match.group(1))
            except (ValueError, json.JSONDecodeError):
                value = None
        values.append(value)
    return values[0], values[1]


def _read_text_head(path, max_bytes):
    """Read at most `max_bytes` of a text file. Returns None when unreadable."""
    try:
        with open(path, "rb") as fh:
            return fh.read(max_bytes).decode("utf-8", "replace")
    except OSError:
        return None


def _ensure_dir(path, mode=0o700):
    """mkdir -p with `mode` on directories we create. Returns True on success."""
    try:
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            try:
                os.chmod(path, mode)
            except OSError:
                pass
        return True
    except OSError as exc:
        _logger.warning("status_report: cannot create %s: %s", path, exc)
        return False


def _atomic_write_text(path, text, mode=0o600):
    """Write `text` to `path` via a per-writer tmp + os.replace, chmod `mode`.

    The tmp name is unique per call (pid + random suffix) — audit BUG #3. A
    fixed `<path>.tmp` made concurrent writers collide: one writer consumed the
    tmp, the peer's `os.chmod`/`os.replace` then raised FileNotFoundError. Two
    schedulers (§2.4 cron at 15 min, §4.5 auto-resume at 5 min) plus a `--watch`
    run all write this cache directory, so the collision is reachable in normal
    operation (measured: 88/150 calls failed at 6 writers).

    Raises OSError (or FileNotFoundError) on failure; callers decide whether to
    retry or degrade.
    """
    parent = os.path.dirname(os.path.abspath(path))
    _ensure_dir(parent)
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _run_git(project_path, args, timeout=10):
    """Read-only git invocation. Returns stdout or None on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(project_path), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _logger.debug("status_report: git %s failed: %s", args, exc)
        return None
    if result.returncode != 0:
        return None
    return result.stdout


# ── Alert state (§2.4 dedupe / §4.4 episode identity) ────────────────────────

def cache_dir():
    """`<XDG_CACHE_HOME or ~/.cache>/crabcakes`, created mkdir -p chmod 0o700.

    Ruling 1: the cache lives under the *user* cache home, not inside the
    project — one reporter serves many projects.
    """
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    path = os.path.join(base, "crabcakes")
    _ensure_dir(path, 0o700)
    return path


def state_path():
    """Default alert/auto-resume state file (consumed by the §2.4 cron path)."""
    return os.path.join(cache_dir(), STATE_FILENAME)


def _coerce_alert_state(state):
    """Structural copy of an alert-state dict. The caller's dict is never
    mutated (copy-on-return, per the should_alert contract)."""
    if not isinstance(state, dict):
        return {"episodes": {}}
    episodes = state.get("episodes")
    if not isinstance(episodes, dict):
        episodes = {}
    out = dict(state)
    out["episodes"] = {
        key: (dict(value) if isinstance(value, dict) else value)
        for key, value in episodes.items()
    }
    return out


def load_alert_state(path):
    """Load the alert-state file. Missing / unreadable / corrupt → `{}`.

    Tolerant by design (invariant 4): a half-written or hand-mangled state file
    must degrade to "no memory", never raise.
    """
    if not isinstance(path, (str, os.PathLike)):
        raise ValueError("state_path must be a path")
    text = _read_text_head(str(path), 1 * 1024 * 1024)
    if text is None:
        return {}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    if "episodes" in parsed and not isinstance(parsed["episodes"], dict):
        return {}
    return _coerce_alert_state(parsed)


def save_alert_state(state_path, state):
    """Atomically persist alert state (tmp + os.replace) with mode 0o600.

    Validates both arguments (audit BUG #13 aligned this with the loader: the
    old code accepted `None` and wrote a file literally named `None` in the CWD)
    and refuses non-finite floats so invalid JSON cannot reach disk (BUG #8).
    Retries once on FileNotFoundError, the residual shape of a tmp-file race
    between two concurrent invocations (BUG #3).
    """
    if not isinstance(state, dict):
        raise ValueError(f"alert state must be a dict, got {type(state).__name__}")
    if isinstance(state_path, str) and not state_path.strip():
        raise ValueError("state_path must be a non-empty path")
    if not isinstance(state_path, (str, os.PathLike)):
        raise ValueError(
            f"state_path must be a path, got {type(state_path).__name__}")
    payload = json.dumps(state, indent=2, sort_keys=True, allow_nan=False)
    for attempt in (1, 2):
        try:
            _atomic_write_text(str(state_path), payload, mode=0o600)
            return
        except FileNotFoundError:
            if attempt == 2:
                raise
            _logger.warning("status_report: state write raced for %s, retrying",
                            state_path)


def episode_id(session_key, last_ts, last_sha):
    """§4.4 episode identity: sha256(session_key|last_ts|last_sha)[:16].

    Stable across invocations while a stall persists; changes the moment the
    agent produces new activity (new timestamp or new message hash).
    """
    if not isinstance(session_key, str) or not session_key:
        raise ValueError("session_key must be a non-empty string")
    if isinstance(last_ts, (int, float)) and not isinstance(last_ts, bool):
        ts_part = f"{float(last_ts):.3f}"
    elif last_ts is None:
        ts_part = ""
    else:
        ts_part = str(last_ts)
    return _sha16(f"{session_key}|{ts_part}|{last_sha if last_sha is not None else ''}")


def stall_episode_id(stall):
    """Episode identity for ANY of the five §2.1 stall classes.

    Generalizes the §4.4 turn_stalled formula: each class hashes the evidence
    that CHANGES when the underlying condition changes, so the returned id is
    stable across invocations while a stall persists and different the moment
    new activity appears (that stability is the contract — the §2.4 dedupe
    floor is keyed on it).

      turn_stalled         sha256(session_key|last_message_ts|last_message_sha)
      blocked_on_sendback  sha256(sendback|<newest SENDBACK file>|<its mtime>)
      app_spinning/
      app_frozen           sha256(<class>|<app pid>|<starttime>) — same id while
                           the SAME process stays in the same posture
      crash_after_start    sha256(crash|<crash file name>)
      approvals_pending    sha256(approvals|<project_path>)

    Pure and tolerant: missing/mistyped evidence falls back to a class-only
    hash (still distinct per class, still stable) — never raises.
    """
    if not isinstance(stall, dict):
        stall = {}
    stall_class = stall.get("class")
    if not isinstance(stall_class, str) or not stall_class:
        stall_class = "unknown"

    if stall_class == "turn_stalled":
        session_key = stall.get("session") or stall.get("session_key")
        if not isinstance(session_key, str) or not session_key:
            session_key = ""
        ts = stall.get("last_message_ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            ts_part = f"{float(ts):.3f}"
        elif ts is None:
            ts_part = ""
        else:
            ts_part = str(ts)
        sha = stall.get("last_message_sha")
        sha_part = sha if isinstance(sha, str) else ("" if sha is None else str(sha))
        return _sha16(f"{session_key}|{ts_part}|{sha_part}")

    if stall_class == "blocked_on_sendback":
        name = stall.get("sendback_file")
        mtime = stall.get("sendback_mtime")
        if isinstance(mtime, (int, float)) and not isinstance(mtime, bool):
            mtime_part = f"{float(mtime):.6f}"
        else:
            mtime_part = "" if mtime is None else str(mtime)
        return _sha16(f"sendback|{name if isinstance(name, str) else ''}|{mtime_part}")

    if stall_class in ("app_spinning", "app_frozen"):
        pid = stall.get("app_pid")
        starttime = stall.get("app_starttime")
        pid_part = str(pid) if pid is not None else ""
        start_part = (f"{float(starttime):.3f}"
                      if isinstance(starttime, (int, float))
                      and not isinstance(starttime, bool) else "")
        return _sha16(f"{stall_class}|{pid_part}|{start_part}")

    if stall_class == "crash_after_start":
        crash_file = stall.get("crash_file")
        return _sha16(
            "crash|" + (crash_file if isinstance(crash_file, str) else ""))

    if stall_class == "approvals_pending":
        project = stall.get("project_path")
        return _sha16(
            "approvals|" + (project if isinstance(project, str) else ""))

    # Unknown class → class-only hash (still stable, still distinct).
    return _sha16(f"{stall_class}")


def mark_alerted(state, episode, now):
    """Return an updated state with `episode` stamped as alerted at `now`.

    Pure: the caller's dict is never mutated. `attempts` counts alerts emitted
    for that episode (the §4.4/G4 record shape).
    """
    if not isinstance(episode, str) or not episode:
        raise ValueError("episode must be a non-empty string")
    now = _require_number(now, "now")
    updated = _coerce_alert_state(state)
    record = updated["episodes"].get(episode)
    record = dict(record) if isinstance(record, dict) else {}
    record["first_seen"] = record.get("first_seen", now)
    record["last_alert"] = now
    try:
        record["attempts"] = int(record.get("attempts") or 0) + 1
    except (TypeError, ValueError):
        record["attempts"] = 1
    updated["episodes"][episode] = record
    return updated


def should_alert(state, episode, now, realert_hours=None):
    """Decide whether to emit a cron alert for `episode` at `now` (§2.4).

    Contract:
      * returns `(alert, updated_state)`; `updated_state` is a copy — the
        caller's dict is not mutated, and on `alert` it has already been
        stamped via `mark_alerted`;
      * `alert` is True for a first-sighting episode (state change
        healthy → attention) or once the re-alert floor has elapsed;
      * `episode` of `None`/`""` means "healthy, nothing to alert" →
        `(False, copy)` with no episode recorded;
      * the caller persists `updated_state` (see `save_alert_state`) and stamps
        `report["alert"]` from the returned flag.

    `realert_hours` defaults to the `ALERT_REALERT_HOURS` module constant
    (resolved per call, so the constant is pinnable) and must be > 0 —
    a zero floor would alert on every invocation.
    """
    if realert_hours is None:
        realert_hours = ALERT_REALERT_HOURS
    realert_hours = _require_number(realert_hours, "realert_hours")
    if realert_hours <= 0:
        raise ValueError("realert_hours must be > 0")
    now = _require_number(now, "now")
    if episode is None or episode == "":
        return False, _coerce_alert_state(state)
    if not isinstance(episode, str):
        raise ValueError(f"episode must be a string, got {type(episode).__name__}")

    updated = _coerce_alert_state(state)
    record = updated["episodes"].get(episode)
    if not isinstance(record, dict):
        return True, mark_alerted(updated, episode, now)
    last_alert = record.get("last_alert")
    if not isinstance(last_alert, (int, float)) or isinstance(last_alert, bool):
        return True, mark_alerted(updated, episode, now)
    if now - float(last_alert) >= realert_hours * 3600.0:
        return True, mark_alerted(updated, episode, now)
    return False, updated


# ── Feed summary + cache (§2.1) ──────────────────────────────────────────────

def _feed_path(project_path):
    """`<project>/.crabcakes/feed.json`.

    Deliberately computed here rather than via `feed_store.load_feed`: that
    reader takes the feed flock, which the reporter must never do (invariant 3).
    Snapshot writes are atomic (`os.replace`), so a plain read is consistent.
    """
    return os.path.join(str(project_path), ".crabcakes", feed_store.FEED_FILENAME)


def _summarize_card(card):
    """Content-policy-safe summary of one feed card, or None if unusable.

    Only whitelisted fields survive. `title` is dropped entirely whenever the
    card carries `metadata.tool_name`, because this app puts raw exec commands
    in titles ("Coder is running: cd …"). `body` and `metadata.tool_args` are
    never copied into the report at any verbosity (spec §2.1 / §11.3).
    """
    if not isinstance(card, dict):
        return None
    metadata = card.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    tool_name = metadata.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        tool_name = None
    # Titles are never "message bodies": they stay capped at COMMAND_CAP at every
    # verbosity, so --full (which raises BODIES only) cannot widen a title that
    # happens to carry a command.
    title = "" if tool_name else _cap(card.get("title") or "", COMMAND_CAP)
    ts = _parse_message_ts(card.get("timestamp"))
    return {
        "timestamp": card.get("timestamp") if isinstance(card.get("timestamp"), str) else _iso(ts),
        "ts": ts,
        "source": card.get("source"),
        "author": card.get("author"),
        "card_type": card.get("card_type"),
        "tool_name": tool_name,
        "title": title,
        "needs_approval": bool(metadata.get("needs_approval")),
    }


def _parse_feed_cards(path):
    """Parse `.crabcakes/feed.json` into a bounded, policy-safe summary.

    Returns `{"cards": [...newest FEED_TAIL_CARDS...], "total_cards": n}`.
    Raises `json.JSONDecodeError`/`OSError` on unreadable input — the caller
    degrades the activity section only (invariant 4).
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if isinstance(raw, dict):
        raw = raw.get("cards") or []
    if not isinstance(raw, list):
        raw = []
    summaries = [s for s in (_summarize_card(c) for c in raw) if s is not None]
    return {"cards": summaries[-FEED_TAIL_CARDS:], "total_cards": len(summaries)}


def _feed_cache_key(path):
    st = os.stat(path)
    return {"path": os.path.abspath(path), "size": st.st_size,
            "mtime": round(st.st_mtime, 6)}


def _load_feed_cache(key):
    """Return the cached payload when it matches `key`, else None. Tolerant."""
    path = os.path.join(cache_dir(), CACHE_FILENAME)
    text = _read_text_head(path, 8 * 1024 * 1024)
    if text is None:
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("key") != key:
        return None
    if not isinstance(payload.get("cards"), list):
        return None
    return payload


def _save_feed_cache(key, payload):
    """Best-effort cache write (atomic, 0o600). Cache failures never break a report."""
    try:
        _atomic_write_text(
            os.path.join(cache_dir(), CACHE_FILENAME),
            json.dumps({"key": key, "cards": payload["cards"],
                        "total_cards": payload["total_cards"]}),
            mode=0o600,
        )
    except OSError as exc:
        _logger.warning("status_report: feed cache write failed: %s", exc)


def _collect_activity(project_path, include_feed):
    """§2.1 `activity` section, backed by the mtime-keyed feed cache."""
    section = {"cards": [], "count": 0, "total_cards": None,
               "cache": "skipped", "tail": FEED_TAIL_CARDS,
               "degraded": False, "degraded_reason": None}
    if not include_feed:
        return section
    path = _feed_path(project_path)
    if not os.path.isfile(path):
        section["degraded_reason"] = "no feed snapshot for this project"
        return section
    try:
        key = _feed_cache_key(path)
    except OSError as exc:
        section.update(degraded=True, degraded_reason=f"feed unreadable: {exc}")
        return section
    payload = _load_feed_cache(key)
    if payload is not None:
        section["cache"] = "hit"
    else:
        section["cache"] = "miss"
        try:
            payload = _parse_feed_cards(path)
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            section.update(degraded=True,
                           degraded_reason=f"feed parse failed: {type(exc).__name__}")
            return section
        _save_feed_cache(key, payload)
    section["cards"] = payload["cards"]
    section["count"] = len(payload["cards"])
    section["total_cards"] = payload["total_cards"]
    return section


# ── /proc sampling (ruling 3: no psutil dependency) ──────────────────────────

_IDLE_PROC_STATES = frozenset({"S", "D", "I"})
_STOPPED_PROC_STATES = frozenset({"T", "t"})   # SIGSTOP / Ctrl-Z / traced
_CPU_BUSY_PERCENT = 80.0


def _hz():
    """Clock ticks per second (SC_CLK_TCK), with a 100 Hz fallback."""
    try:
        return float(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError):
        return 100.0


def _proc_root(proc_root=None):
    return proc_root if proc_root is not None else PROC_ROOT


def _iter_proc_pids(proc_root=None):
    """Numeric PIDs under /proc. Raises OSError when the tree is unreadable."""
    root = _proc_root(proc_root)
    pids = []
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.name.isdigit():
                pids.append(int(entry.name))
    return pids


def _read_proc_stat(pid, proc_root=None):
    """Parse `/proc/<pid>/stat` → {state, utime, stime, num_threads, starttime}.

    Field offsets are relative to the text after `)` (field 3 → index 0):
    state=0, utime=11, stime=12, num_threads=17, starttime=19.
    """
    root = _proc_root(proc_root)
    text = _read_text_head(os.path.join(root, str(pid), "stat"), 4096)
    if not text or ")" not in text:
        return None
    try:
        _, rest = text.rsplit(")", 1)
        fields = rest.split()
        return {
            "state": fields[0],
            "utime": float(fields[11]),
            "stime": float(fields[12]),
            "num_threads": int(fields[17]),
            "starttime": float(fields[19]),
        }
    except (IndexError, ValueError):
        return None


def _read_proc_cmdline(pid, proc_root=None):
    """`/proc/<pid>/cmdline` as an argv list (empty when unreadable)."""
    root = _proc_root(proc_root)
    try:
        with open(os.path.join(root, str(pid), "cmdline"), "rb") as fh:
            raw = fh.read(64 * 1024)
    except OSError:
        return []
    return [part for part in raw.decode("utf-8", "replace").split("\x00") if part]


def _read_proc_cwd(pid, proc_root=None):
    root = _proc_root(proc_root)
    try:
        return os.readlink(os.path.join(root, str(pid), "cwd"))
    except OSError:
        return None


def _read_proc_exe(pid, proc_root=None):
    """`/proc/<pid>/exe` target, or None when unreadable.

    A deleted-but-running binary reports `"… (deleted)"`; the caller only needs
    the basename prefix, which survives that suffix.
    """
    root = _proc_root(proc_root)
    try:
        return os.readlink(os.path.join(root, str(pid), "exe"))
    except OSError:
        return None


def _is_python_exe(exe):
    """True when `exe` looks like a python interpreter (audit BUG #2).

    This is the cheapest available identity check: a `less main.py` / `cat
    main.py` / editor argv can be made to look like the app, but its executable
    is not python. Unreadable `exe` (None) also returns False — an unverifiable
    process is not the app (same-user `/proc/<pid>/exe` is always readable, so
    this only rejects genuinely foreign permissions).
    """
    if not isinstance(exe, str) or not exe:
        return False
    return os.path.basename(exe).startswith("python")


def _read_proc_wchan(pid, proc_root=None):
    root = _proc_root(proc_root)
    text = _read_text_head(os.path.join(root, str(pid), "wchan"), 256)
    return text.strip() if text else None


def _read_proc_rss_kb(pid, proc_root=None):
    root = _proc_root(proc_root)
    text = _read_text_head(os.path.join(root, str(pid), "statm"), 512)
    if not text:
        return None
    try:
        pages = int(text.split()[1])
    except (IndexError, ValueError):
        return None
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError):
        page_size = 4096
    return pages * page_size // 1024


def _read_proc_thread_count(pid, proc_root=None):
    root = _proc_root(proc_root)
    try:
        return len(os.listdir(os.path.join(root, str(pid), "task")))
    except OSError:
        return None


def _proc_boot_time(proc_root=None):
    """System boot epoch from `/proc/stat`'s `btime` line, or None."""
    root = _proc_root(proc_root)
    text = _read_text_head(os.path.join(root, "stat"), 64 * 1024)
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _app_matches(cmdline, cwd, project_path, exe=None):
    """True when an argv + executable look like THIS app for `project_path`.

    Identity is name + interpreter + location (audit BUG #1/#2):
      * the argv must contain a declared entry point (`APP_SCRIPT_NAMES`), so
        both `python3 main.py` and the console launcher `crabcakes` match;
      * the executable must be python, so `less main.py`, `cat main.py`,
        `sed -n … main.py` and an editor opened on main.py do not;
      * and the process must belong to the project — cwd OR the script's own
        directory — so both `cd <project> && python3 main.py` and
        `python3 <project>/main.py` hit.

    TODO(AGENTCTRL1 P1 audit BUG #1/#2): this is still inference from /proc.
    The durable fix is an app-written pidfile/heartbeat (authoritative, survives
    launcher renames and `/proc` mounted with hidepid). Deferred — it is an
    app-side change and needs PM sign-off.
    """
    if not any(os.path.basename(token) in APP_SCRIPT_NAMES for token in cmdline):
        return False
    if exe is not None and not _is_python_exe(exe):
        return False
    if cwd and os.path.realpath(cwd) == project_path:
        return True
    for token in cmdline:
        if os.path.basename(token) not in APP_SCRIPT_NAMES:
            continue
        token_dir = os.path.dirname(token)
        if token_dir and os.path.realpath(token_dir) == project_path:
            return True
    return False


def _cpu_sample(pid, proc_root, interval):
    """Two-sample CPU% for one process. Returns (percent|None, second_state|None).

    A sleeping process skips the second sample: there is nothing to confirm, so
    an idle app costs the reporter no wall time.
    """
    first = _read_proc_stat(pid, proc_root)
    if first is None:
        return None, None
    if first["state"] != "R" or interval is None or interval <= 0:
        return None, first["state"]
    t0 = time.monotonic()
    cpu0 = first["utime"] + first["stime"]
    time.sleep(interval)
    second = _read_proc_stat(pid, proc_root)
    elapsed = time.monotonic() - t0
    if second is None or elapsed <= 0:
        return None, None
    cpu1 = second["utime"] + second["stime"]
    percent = (cpu1 - cpu0) / _hz() / elapsed * 100.0
    return max(0.0, min(100.0, percent)), second["state"]


def _classify_main_thread(state, second_state, cpu_percent):
    """`frozen` (stopped/traced) · `idle` (sleeping) · `busy` (>80 % CPU, R).

    `T`/`t` are SIGSTOP/Ctrl-Z/debugger-stopped states (audit BUG #7): the app
    cannot make progress in any of them, so they are a stall condition rather
    than an unclassifiable "unknown".
    """
    if state in _STOPPED_PROC_STATES:
        return "frozen"
    if state in _IDLE_PROC_STATES and second_state in _IDLE_PROC_STATES:
        return "idle"
    if (state == "R" and second_state == "R" and cpu_percent is not None
            and cpu_percent > _CPU_BUSY_PERCENT):
        return "busy"
    return "unknown"


def _collect_app(project_path, now, sample_interval):
    """§2.1 `app` section: pid, uptime, RSS, threads, main-thread posture."""
    section = {"running": False, "pid": None, "uptime_seconds": None,
               "starttime": None,
               "rss_kb": None, "thread_count": None, "cpu_percent": None,
               "main_thread": "unknown", "state": None, "wchan": None,
               "degraded": False, "app_running_degraded": False,
               "degraded_reason": None}
    try:
        pids = _iter_proc_pids()
    except OSError as exc:
        section.update(degraded=True, app_running_degraded=True,
                       degraded_reason=f"cannot read {PROC_ROOT}: {exc}")
        return section

    best_pid, best_start = None, None
    for pid in pids:
        cmdline = _read_proc_cmdline(pid)
        if not cmdline:
            continue
        stat = _read_proc_stat(pid)
        # A zombie is a dead app (audit BUG #7): it must not be reported as
        # running, however much its argv still looks like the launcher.
        if stat is not None and stat["state"] == "Z":
            continue
        if not _app_matches(cmdline, _read_proc_cwd(pid), project_path,
                            _read_proc_exe(pid)):
            continue
        start = stat["starttime"] if stat else -1.0
        if best_start is None or start > best_start:
            best_pid, best_start = pid, start
    if best_pid is None:
        return section

    section["running"] = True
    section["pid"] = best_pid
    stat = _read_proc_stat(best_pid)
    if stat is not None:
        section["state"] = stat["state"]
        section["thread_count"] = stat.get("num_threads")
        # Raw /proc starttime — episode identity for app_frozen/app_spinning
        # (§2.4: the same id while the same process stays in the same posture).
        section["starttime"] = stat.get("starttime")
        boot = _proc_boot_time()
        if boot is not None:
            section["uptime_seconds"] = max(0.0, now - boot - stat["starttime"] / _hz())
    section["wchan"] = _read_proc_wchan(best_pid)
    section["rss_kb"] = _read_proc_rss_kb(best_pid)
    count = _read_proc_thread_count(best_pid)
    if count is not None:
        section["thread_count"] = count

    percent, second_state = _cpu_sample(best_pid, None, sample_interval)
    section["cpu_percent"] = percent
    section["main_thread"] = _classify_main_thread(
        section["state"], second_state, percent)
    return section


# ── work section ─────────────────────────────────────────────────────────────

_CRASH_MONTHS = {name: idx for idx, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}


def _newest_spec(project_path):
    """Newest `.md` in `<project>/docs/specs`, as (name, relpath, mtime)."""
    specs_dir = os.path.join(str(project_path), "docs", "specs")
    newest = (None, None, None)
    try:
        names = os.listdir(specs_dir)
    except OSError:
        return newest
    for name in names:
        if not name.lower().endswith(".md"):
            continue
        mtime = _mtime(os.path.join(specs_dir, name))
        if mtime is None:
            continue
        if newest[2] is None or mtime > newest[2]:
            newest = (name, os.path.join("docs", "specs", name), mtime)
    return newest


def _parse_tasks_summary(project_path):
    """First work-unit heading + status from the generated `.crabcakes/tasks.md`."""
    text = _read_text_head(
        os.path.join(str(project_path), ".crabcakes", "tasks.md"), 256 * 1024)
    if not text:
        return None, None
    unit = status = None
    for line in text.splitlines():
        if unit is None and line.startswith("## "):
            unit = line[3:].strip()
            continue
        if unit is not None and "- **Status:**" in line:
            status = line.split("- **Status:**", 1)[1].strip()
            break
    return unit, status


def _find_sendback(project_path):
    """Newest `*SENDBACK*.md` in docs/specs (any case) → (relpath, mtime)."""
    specs_dir = os.path.join(str(project_path), "docs", "specs")
    newest = (None, None)
    try:
        names = os.listdir(specs_dir)
    except OSError:
        return newest
    for name in names:
        if "SENDBACK" not in name.upper() or not name.lower().endswith(".md"):
            continue
        mtime = _mtime(os.path.join(specs_dir, name))
        if mtime is None:
            continue
        if newest[1] is None or mtime > newest[1]:
            newest = (os.path.join("docs", "specs", name), mtime)
    return newest


def _scan_repo_writes(project_path, cap=None):
    """Newest mtime + file count under the project, bounded at `cap` files.

    Excludes VCS/app bookkeeping and bytecode caches (see
    WRITE_SCAN_EXCLUDED_DIRS). `cap=None` resolves `WALK_FILE_CAP` per call so
    the constant stays pinnable. Returns (newest_mtime, files, truncated).
    """
    if cap is None:
        cap = WALK_FILE_CAP
    newest = None
    count = 0
    for root, dirs, files in os.walk(str(project_path)):
        dirs[:] = [d for d in dirs if d not in WRITE_SCAN_EXCLUDED_DIRS]
        for name in files:
            mtime = _mtime(os.path.join(root, name))
            if mtime is None:
                continue
            count += 1
            if newest is None or mtime > newest:
                newest = mtime
            if count >= cap:
                return newest, count, True
    return newest, count, False


def _newest_commit(project_path):
    """(committed_ts, subject) for HEAD, or (None, None).

    Ruling 6 names `git_ops.log(project, 1)` for the commit-newer check, but
    that helper runs `--oneline --all` and carries no timestamp; the epoch
    needed for the comparison comes from one read-only `git log -1` instead.
    """
    out = _run_git(project_path, ["log", "-1", "--format=%ct%x1f%s"])
    if not out:
        return None, None
    line = out.splitlines()[0] if out.splitlines() else ""
    ts_part, _, subject = line.partition("\x1f")
    try:
        return float(ts_part), subject
    except ValueError:
        return None, subject or None


def _unpushed_count(project_path):
    """Commits ahead of the upstream, or None when there is no upstream."""
    out = _run_git(project_path, ["rev-list", "--count", "@{u}..HEAD"])
    if out is None:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def _collect_work(project_path):
    """§2.1 `work` section: spec, work unit, git posture, write-scan evidence."""
    section = {"spec": None, "spec_path": None, "unit": None, "unit_status": None,
               "is_repo": False, "branch": None, "head_sha": None,
               "head_subject": None, "newest_commit_ts": None,
               "dirty_count": None, "unpushed_count": None,
               "sendback_file": None, "sendback_mtime": None,
               "newest_write_ts": None, "write_scan": {"files": 0, "truncated": False},
               "degraded": False, "degraded_reason": None}
    if not os.path.isdir(str(project_path)):
        section.update(degraded=True, degraded_reason="project path not found")
        return section

    name, rel, _ = _newest_spec(project_path)
    section["spec"], section["spec_path"] = name, rel
    section["unit"], section["unit_status"] = _parse_tasks_summary(project_path)

    newest_write, files, truncated = _scan_repo_writes(project_path)
    section["newest_write_ts"] = newest_write
    section["write_scan"] = {"files": files, "truncated": truncated}
    if truncated:
        section.update(degraded=True,
                       degraded_reason=f"write scan truncated at {files} files")

    try:
        section["is_repo"] = bool(is_repo(str(project_path)))
    except Exception as exc:  # gitpython raises assorted errors on odd trees
        _logger.debug("status_report: is_repo failed: %s", exc)
        section["is_repo"] = False
    if not section["is_repo"]:
        return section

    branch = get_branch(str(project_path))
    if branch.success:
        section["branch"] = branch.stdout.strip() or None
    head = get_head_sha(str(project_path))
    if head.success:
        section["head_sha"] = head.stdout.strip() or None
    ts, subject = _newest_commit(project_path)
    section["newest_commit_ts"], section["head_subject"] = ts, subject
    try:
        section["dirty_count"] = len(status_porcelain(str(project_path)))
    except Exception as exc:
        _logger.debug("status_report: status_porcelain failed: %s", exc)
    section["unpushed_count"] = _unpushed_count(project_path)
    sendback_file, sendback_mtime = _find_sendback(project_path)
    if sendback_file and ts is not None and sendback_mtime is not None \
            and sendback_mtime > ts:
        section["sendback_file"] = sendback_file
        section["sendback_mtime"] = sendback_mtime
    return section


# ── agents section ───────────────────────────────────────────────────────────

def _collect_agents(project_path, config_dir, body_cap, now):
    """§2.1 `agents` section — one entry per `<config>/conversations/special:*.json`.

    Discovery is filtered to the `special:` session files the app writes: the
    live config dir holds tens of thousands of unrelated `.json` files (test
    debris), and opening them all would blow the <1 s report budget.
    """
    section = {"sessions": [], "count": 0, "in_project_count": 0,
               "degraded": False, "degraded_reason": None}
    conv_dir = os.path.join(str(config_dir), "conversations")
    try:
        names = sorted(n for n in os.listdir(conv_dir)
                       if n.startswith(SESSION_FILE_PREFIX) and n.endswith(".json"))
    except OSError as exc:
        section["degraded_reason"] = f"no conversations dir ({type(exc).__name__})"
        return section

    degraded = False
    for name in names:
        session_key = name[:-len(".json")]
        path = os.path.join(conv_dir, name)
        mtime = _mtime(path)
        entry = {"session_key": session_key, "agent_name": None, "model": None,
                 "project_path": None, "in_project": False, "mtime": mtime,
                 "idle_seconds": (None if mtime is None else max(0.0, now - mtime)),
                 "message_count": 0, "last_role": None, "last_message": "",
                 "last_message_sha": "", "last_message_ts": None,
                 "content_chars": 0, "tool_names": [], "pending_tool_calls": False,
                 "unreadable": False, "oversize": False, "error": None}
        # Reader-side cap, not a writer-side tear (audit BUG #5): anything past
        # CONVERSATION_MAX_BYTES is truncated by US, so the parse failure would
        # otherwise be blamed on save_conversation_to_disk's write. (SPEC-08 SP2:
        # still true under dual-write — the JSON body is written exactly as
        # before; the store append is delta-based and touches no JSON file.)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None
        if size is not None and size > CONVERSATION_MAX_BYTES:
            head_project, head_agent = _head_metadata(path)
            entry["project_path"] = head_project
            entry["agent_name"] = head_agent
            if isinstance(head_project, str) and head_project:
                entry["in_project"] = os.path.realpath(head_project) == project_path
            entry.update(
                unreadable=True, oversize=True,
                error=(f"file exceeds the {CONVERSATION_MAX_BYTES // (1024 * 1024)}MB "
                       f"read cap (last message not parsed)"))
            degraded = True
            section["sessions"].append(entry)
            continue
        text = _read_text_head(path, CONVERSATION_MAX_BYTES)
        if text is None:
            entry.update(unreadable=True, error="unreadable file")
            degraded = True
            section["sessions"].append(entry)
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            # save_conversation_to_disk writes the JSON non-atomically
            # (agent/persistence.py:126) — a torn tail is expected, not
            # exceptional. SPEC-08 SP2: unchanged by dual-write; the store
            # append happens after the JSON write and touches no JSON file.
            entry.update(unreadable=True, error="invalid JSON (writer active)")
            degraded = True
            section["sessions"].append(entry)
            continue
        if not isinstance(data, dict):
            entry.update(unreadable=True, error="unexpected JSON shape")
            degraded = True
            section["sessions"].append(entry)
            continue

        entry["agent_name"] = data.get("agent_name")
        entry["model"] = data.get("model")
        entry["project_path"] = data.get("project_path")
        project = data.get("project_path")
        if isinstance(project, str) and project:
            entry["in_project"] = os.path.realpath(project) == project_path
        messages = data.get("messages")
        messages = messages if isinstance(messages, list) else []
        entry["message_count"] = len(messages)
        last = messages[-1] if messages else None
        if isinstance(last, dict):
            role = last.get("role")
            content = last.get("content")
            content = content if isinstance(content, str) else ""
            entry["last_role"] = role
            entry["content_chars"] = len(content)
            entry["last_message_sha"] = _sha16(content)
            entry["last_message_ts"] = _parse_message_ts(last.get("timestamp"))
            # content policy: tool output is never copied into the report
            entry["last_message"] = "" if role == "tool" else _cap(content, body_cap)
            if role == "assistant":
                calls = last.get("tool_calls")
                if isinstance(calls, list):
                    entry["tool_names"] = [
                        c.get("tool_name") for c in calls
                        if isinstance(c, dict) and isinstance(c.get("tool_name"), str)
                    ][:5]
                entry["pending_tool_calls"] = bool(entry["tool_names"])
        section["sessions"].append(entry)

    section["sessions"].sort(key=lambda s: (not s["in_project"],
                                            -(s["idle_seconds"] or 0.0)))
    section["count"] = len(section["sessions"])
    section["in_project_count"] = sum(1 for s in section["sessions"] if s["in_project"])
    section["degraded"] = degraded
    if degraded:
        section["degraded_reason"] = "one or more conversation files unreadable"
    return section


# ── approvals section ────────────────────────────────────────────────────────

def _read_tail_lines(path, max_bytes):
    """Last complete lines of a growing JSONL file (tail read, bounded)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    try:
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # discard the partial first record
            raw = fh.read()
    except OSError:
        return None
    return raw.decode("utf-8", "replace").splitlines()


def _collect_approvals(config_dir, now, newest_write_ts):
    """§2.1 `approvals` section from the audit-log tail (tool names only)."""
    window_seconds = APPROVAL_WINDOW_MINUTES * 60
    section = {"window_minutes": APPROVAL_WINDOW_MINUTES, "entries": 0,
               "grants": 0, "denials": 0, "last_ts": None, "tool_names": {},
               "pending": False, "degraded": False, "degraded_reason": None}
    path = os.path.join(str(config_dir), "audit-log.jsonl")
    if not os.path.isfile(path):
        section["degraded_reason"] = "no audit log for this user"
        return section
    lines = _read_tail_lines(path, AUDIT_TAIL_BYTES)
    if lines is None:
        section.update(degraded=True, degraded_reason="audit log unreadable")
        return section

    last_ts = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # torn tail line — skip, never fail the section
        if not isinstance(record, dict):
            continue
        ts = record.get("timestamp")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        if now - float(ts) > window_seconds or float(ts) > now:
            continue
        if record.get("approved") is None:      # not an approval-bearing tool
            continue
        section["entries"] += 1
        if record.get("approved") is True:
            section["grants"] += 1
        else:
            section["denials"] += 1
        tool = record.get("tool_name")
        if isinstance(tool, str) and tool:
            section["tool_names"][tool] = section["tool_names"].get(tool, 0) + 1
        if last_ts is None or float(ts) > last_ts:
            last_ts = float(ts)
    section["last_ts"] = last_ts
    section["pending"] = bool(
        section["entries"] >= APPROVAL_PENDING_MIN
        and last_ts is not None
        and (newest_write_ts is None or newest_write_ts < last_ts)
    )
    return section


# ── health section ───────────────────────────────────────────────────────────

def _parse_crash_date(value):
    """apport `Date:` (ctime-style or epoch) → epoch float, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.isdigit():
        try:
            return float(text)
        except ValueError:
            return None
    text = text.split(" (", 1)[0].strip()
    for fmt in ("%a %b %d %H:%M:%S %Y", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    # locale-proof fallback for the ctime form: "Sat Sep 12 08:51:00 2026"
    parts = text.split()
    if len(parts) == 5 and parts[1] in _CRASH_MONTHS:
        try:
            return datetime(int(parts[4]), _CRASH_MONTHS[parts[1]], int(parts[2]),
                            *[int(x) for x in parts[3].split(":")]).timestamp()
        except (ValueError, TypeError):
            return None
    return None


def _collect_health(now, app_start_ts):
    """§2.1 `health` section: apport crash reports after the app started."""
    section = {"crashes": [], "count": 0, "after_app_start": 0,
               "degraded": False, "degraded_reason": None}
    if not os.path.isdir(CRASH_DIR):
        section.update(degraded=True,
                       degraded_reason=f"crash dir not found: {CRASH_DIR}")
        return section
    try:
        names = [n for n in os.listdir(CRASH_DIR) if n.endswith(".crash")]
    except OSError as exc:
        section.update(degraded=True, degraded_reason=f"crash dir unreadable: {exc}")
        return section

    crashes = []
    for name in names:
        path = os.path.join(CRASH_DIR, name)
        head = _read_text_head(path, CRASH_HEAD_BYTES)
        if head is None:
            continue
        fields = {}
        for line in head.splitlines():
            key, sep, value = line.partition(":")
            if sep and key in ("Date", "Signal", "ExecutablePath", "ProcCmdline"):
                fields.setdefault(key, value.strip())
        crash_ts = _parse_crash_date(fields.get("Date"))
        executable = os.path.basename(fields.get("ExecutablePath", "")) or None
        crashes.append({
            "path": path,
            "crash_time": crash_ts,
            "crash_time_iso": _iso(crash_ts),
            "signal": fields.get("Signal"),
            "executable": executable,
            # command line is a COMMAND: capped at every verbosity (§2.1)
            "proc_cmdline": _cap(fields.get("ProcCmdline", ""), COMMAND_CAP),
            "after_app_start": bool(crash_ts is not None and app_start_ts is not None
                                    and crash_ts > app_start_ts),
        })
    crashes.sort(key=lambda c: c["crash_time"] or 0.0, reverse=True)
    section["count"] = len(crashes)
    section["crashes"] = crashes[:CRASH_MAX_REPORTS]
    section["after_app_start"] = sum(1 for c in crashes if c["after_app_start"])
    return section


# ── stall assessment (§2.1 heuristics) ───────────────────────────────────────

def _stall_rank(stall):
    try:
        return STALL_CLASS_ORDER.index(stall.get("class"))
    except ValueError:
        return len(STALL_CLASS_ORDER)


def _detect_stalls(report, now=None):
    """Derive the five §2.1 stall classes from a report's sections.

    Pure and idempotent: it reads the derived section fields (idle_seconds,
    newest_write_ts, approvals.pending, health.after_app_start, app posture) so
    `collect()` and a later `assess()` agree on the same evidence.
    """
    app = report.get("app") or {}
    work = report.get("work") or {}
    agents = report.get("agents") or {}
    approvals = report.get("approvals") or {}
    health = report.get("health") or {}
    threshold = STALL_THRESHOLD_MINUTES * 60.0
    stalls = []

    if app.get("running") and app.get("main_thread") == "frozen":
        stalls.append({
            "class": "app_frozen", "session": None,
            "app_pid": app.get("pid"), "app_starttime": app.get("starttime"),
            "detail": (f"pid {app.get('pid')} is stopped (state "
                       f"{app.get('state')}, wchan {app.get('wchan')}) — the app "
                       f"cannot make progress"),
        })

    if app.get("running") and app.get("main_thread") == "busy":
        percent = app.get("cpu_percent")
        detail = (f"pid {app.get('pid')} main thread busy"
                  + (f" at {percent:.0f}% CPU" if isinstance(percent, (int, float)) else ""))
        stalls.append({"class": "app_spinning", "session": None,
                       "app_pid": app.get("pid"),
                       "app_starttime": app.get("starttime"),
                       "detail": detail})

    newest_write = work.get("newest_write_ts")
    scan = work.get("write_scan") or {}
    scan_truncated = bool(scan.get("truncated"))
    for session in agents.get("sessions") or []:
        if not session.get("in_project"):
            continue
        # Oversize conversations (BUG #5) are unparsed but still assessable from
        # their mtime; only genuinely torn/unreadable files are skipped.
        oversize = bool(session.get("oversize"))
        if session.get("unreadable") and not oversize:
            continue
        if not oversize and session.get("last_role") != "assistant":
            continue
        idle = session.get("idle_seconds")
        if idle is None or idle < threshold:
            continue
        mtime = session.get("mtime")
        # A truncated write scan is not evidence of activity (BUG #4): its
        # maximum is computed over an arbitrary subset that may exclude the
        # agent's own writes, so it must not suppress the stall.
        if (not scan_truncated and newest_write is not None and mtime is not None
                and newest_write >= mtime):
            continue  # something in the repo moved after the message: not idle
        agent = session.get("agent_name") or session.get("session_key")
        if oversize:
            reason = (f"last message unparsed (file exceeds the "
                      f"{CONVERSATION_MAX_BYTES // (1024 * 1024)}MB read cap)")
        else:
            reason = "assistant-final message, no tool call"
        if scan_truncated:
            reason += (f"; repo writes not verifiable (scan truncated at "
                       f"{scan.get('files')} files)")
        else:
            reason += ", no repo writes since"
        detail = (f"{session.get('session_key')} (agent {agent}) idle "
                  f"{int(idle // 60)}m: {reason}")
        stalls.append({"class": "turn_stalled",
                       "session": session.get("session_key"), "detail": detail,
                       "last_message_ts": session.get("last_message_ts"),
                       "last_message_sha": session.get("last_message_sha")})

    if work.get("sendback_file"):
        stalls.append({
            "class": "blocked_on_sendback", "session": None,
            "sendback_file": work.get("sendback_file"),
            "sendback_mtime": work.get("sendback_mtime"),
            "detail": (f"{work['sendback_file']} is newer than HEAD "
                       f"({work.get('head_sha') or 'unknown'})"),
        })

    if approvals.get("pending"):
        stalls.append({
            "class": "approvals_pending", "session": None,
            "project_path": report.get("project_path"),
            "detail": (f"{approvals.get('entries')} approval entries in the last "
                       f"{approvals.get('window_minutes')}m with no repo writes after them"),
        })

    if health.get("after_app_start"):
        first = next((c for c in (health.get("crashes") or [])
                      if c.get("after_app_start")), {})
        stalls.append({
            "class": "crash_after_start", "session": None,
            "crash_file": first.get("path"),
            "detail": (f"crash at {first.get('crash_time_iso')} "
                       f"signal {first.get('signal')} ({first.get('executable')})"),
        })

    stalls.sort(key=_stall_rank)
    return stalls


def assess(report, now=None):
    """§2.2 exit-code contract → (summary_line, exit_code).

    `0` healthy · `2` attention (a stall class fired) · `3` app not running.
    App-down outranks any stall: the report is still emitted for the
    filesystem side. When several classes fire, the summary names the worst one
    in `STALL_CLASS_ORDER` (frozen app → idle pipeline → …).
    """
    if not isinstance(report, dict):
        raise ValueError(f"report must be a dict, got {type(report).__name__}")
    stalls = report.get("stalls")
    if stalls is None:
        # Derived locally: assess() is a reader, it must not stamp state into the
        # caller's report (audit BUG #12).
        stalls = _detect_stalls(report, now)
    app = report.get("app") or {}
    if not app.get("running"):
        reason = app.get("degraded_reason") or "no main.py found for this project"
        return f"app_not_running: {reason}", 3
    stalls = [s for s in stalls if isinstance(s, dict) and s.get("class")]
    if stalls:
        worst = min(stalls, key=_stall_rank)
        return f"{worst['class']}: {worst.get('detail')}", 2
    return "healthy: no stall class detected", 0


# ── rendering ────────────────────────────────────────────────────────────────

def _fmt_duration(seconds):
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds < 0:
        # A negative delta means the report has no usable clock (audit BUG #11);
        # rendering "-1700000000s ago" is noise, not information.
        return "?"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def _render_card(card):
    ts = card.get("timestamp") or "?"
    if len(ts) > 19:
        ts = ts[:19]
    label = card.get("tool_name") or card.get("title") or ""
    flag = " [needs approval]" if card.get("needs_approval") else ""
    return f"            - {ts} {card.get('source')}/{card.get('card_type')} {card.get('author')}: {label}{flag}"


def render_text(report, *, show_content=False):
    """Human-readable report (cron delivers only the EXIT line).

    Message bodies are re-capped here: `show_content=True` raises the cap to
    `BODY_CAP_FULL` but never above the cap the report was *collected* with, so
    a report collected at the 120-char default cannot be widened at render time.
    """
    if not isinstance(report, dict):
        raise ValueError(f"report must be a dict, got {type(report).__name__}")
    collected_cap = report.get("body_cap")
    if not isinstance(collected_cap, int) or collected_cap <= 0:
        collected_cap = BODY_CAP_DEFAULT
    cap = min(BODY_CAP_FULL if show_content else BODY_CAP_DEFAULT, collected_cap)

    app = report.get("app") or {}
    work = report.get("work") or {}
    agents = report.get("agents") or {}
    activity = report.get("activity") or {}
    approvals = report.get("approvals") or {}
    health = report.get("health") or {}

    lines = [f"crabcakes status — {report.get('project_path')}",
             f"generated {report.get('generated_at')}"]

    if app.get("running"):
        cpu = app.get("cpu_percent")
        lines.append(
            "APP         running pid={pid} uptime={up} rss={rss}kB threads={thr} "
            "cpu={cpu} main={main} state={state} wchan={wchan}".format(
                pid=app.get("pid"), up=_fmt_duration(app.get("uptime_seconds")),
                rss=app.get("rss_kb"), thr=app.get("thread_count"),
                cpu=(f"{cpu:.0f}%" if isinstance(cpu, (int, float)) else "n/a"),
                main=app.get("main_thread"), state=app.get("state"),
                wchan=app.get("wchan")))
    elif app.get("degraded"):
        lines.append(f"APP         unknown ({app.get('degraded_reason')})")
    else:
        lines.append("APP         not running")

    repo = "repo" if work.get("is_repo") else "no-repo"
    lines.append(
        f"WORK        {repo} branch={work.get('branch')} head={work.get('head_sha')} "
        f"dirty={work.get('dirty_count')} unpushed={work.get('unpushed_count')}")
    lines.append(
        f"            spec={work.get('spec')} unit={work.get('unit')!r} "
        f"[{work.get('unit_status')}]")
    if work.get("sendback_file"):
        lines.append(f"            sendback={work['sendback_file']} (newer than HEAD)")
    scan = work.get("write_scan") or {}
    lines.append(
        f"            writes: newest={_iso(work.get('newest_write_ts'))} "
        f"files={scan.get('files')} truncated={scan.get('truncated')}")

    lines.append(f"AGENTS      {agents.get('count', 0)} session(s), "
                 f"{agents.get('in_project_count', 0)} for this project")
    for session in agents.get("sessions") or []:
        head = ("{key} agent={agent} last={role} idle={idle} msgs={n}".format(
            key=session.get("session_key"), agent=session.get("agent_name"),
            role=session.get("last_role"),
            idle=_fmt_duration(session.get("idle_seconds")),
            n=session.get("message_count")))
        if session.get("unreadable"):
            lines.append(f"            {head} — unreadable ({session.get('error')})")
            continue
        if not session.get("in_project"):
            head += " [other project]"
        lines.append(f"            {head}")
        if session.get("last_role") == "tool":
            lines.append("                msg: (tool output omitted)")
        elif session.get("last_message"):
            lines.append(f"                msg: {_cap(session['last_message'], cap)}")

    lines.append(f"ACTIVITY    showing {activity.get('count', 0)} of "
                 f"{activity.get('total_cards')} card(s) (cache={activity.get('cache')})")
    for card in activity.get("cards") or []:
        lines.append(_render_card(card))

    if approvals.get("entries"):
        lines.append(f"APPROVALS   {approvals['entries']} entr(ies) in "
                     f"{approvals.get('window_minutes')}m "
                     f"({approvals.get('grants')} grant / {approvals.get('denials')} deny), "
                     f"last {_fmt_duration(None if approvals.get('last_ts') is None else
                                           report.get('now', 0) - approvals['last_ts'])} ago")
    else:
        lines.append(f"APPROVALS   none in the last {approvals.get('window_minutes')}m")

    if health.get("count"):
        lines.append(f"HEALTH      {health['count']} crash report(s), "
                     f"{health.get('after_app_start')} after app start")
        for crash in health.get("crashes") or []:
            lines.append(
                f"            - {crash.get('crash_time_iso')} signal={crash.get('signal')} "
                f"{crash.get('executable')} cmd={crash.get('proc_cmdline')}")
    elif health.get("degraded"):
        lines.append(f"HEALTH      unknown ({health.get('degraded_reason')})")
    else:
        lines.append("HEALTH      no crash reports")

    for section_name in report.get("degraded_sections") or []:
        reason = (report.get(section_name) or {}).get("degraded_reason")
        lines.append(f"DEGRADED    {section_name}: {reason}")

    stalls = report.get("stalls") or []
    if stalls:
        for stall in stalls:
            lines.append(f"STALLS      {stall.get('class')}: {stall.get('detail')}")
    else:
        lines.append("STALLS      none")

    if report.get("alert"):
        lines.append("ALERT       yes (episode dedupe applied)")

    summary, code = assess(report)
    lines.append(f"EXIT        {code} ({summary})")
    return "\n".join(lines) + "\n"


def render_json(report):
    """Machine-readable report (spec §2.2) — `json.dumps(report, indent=2)`."""
    if not isinstance(report, dict):
        raise ValueError(f"report must be a dict, got {type(report).__name__}")
    return json.dumps(report, indent=2)


# ── collect() ────────────────────────────────────────────────────────────────

_SECTION_NAMES = ("app", "work", "agents", "activity", "approvals", "health")


def collect(project_path=".", config_dir=None, *, include_feed=True,
            body_cap=BODY_CAP_DEFAULT, now=None,
            sample_interval=SAMPLE_INTERVAL_SEC):
    """Gather the raw status report. Never raises; degrades per section.

    Keyword extras beyond the spec signature exist for the CLI shim and tests:
    `include_feed` backs `--no-feed`, `body_cap` backs `--full` (bodies only),
    `now` pins the clock, `sample_interval` bounds the CPU two-sample window.
    """
    if not isinstance(project_path, (str, os.PathLike)):
        raise ValueError("project_path must be a path")
    if config_dir is not None and not isinstance(config_dir, (str, os.PathLike)):
        raise ValueError("config_dir must be a path")
    if isinstance(body_cap, bool) or not isinstance(body_cap, int) or body_cap <= 0:
        raise ValueError("body_cap must be a positive int")
    now = time.time() if now is None else _require_number(now, "now")
    project = os.path.realpath(os.path.abspath(os.path.expanduser(str(project_path))))
    if config_dir is None:
        config_dir = get_config_dir()
    config_dir = str(config_dir)

    report = {
        "project_path": project,
        "project_exists": os.path.isdir(project),
        "generated_at": _iso(now),
        "now": now,
        "body_cap": body_cap,
        # Cron alert condition (§2.4 / ruling 5). Default False = silence; the
        # caller flips it to True from should_alert() once dedupe is applied.
        "alert": False,
    }

    report["app"] = _collect_app(project, now, sample_interval)
    report["work"] = _collect_work(project)
    report["agents"] = _collect_agents(project, config_dir, body_cap, now)
    report["activity"] = _collect_activity(project, include_feed)
    report["approvals"] = _collect_approvals(
        config_dir, now, report["work"].get("newest_write_ts"))
    app_start_ts = None
    if report["app"].get("running") and report["app"].get("uptime_seconds") is not None:
        app_start_ts = now - report["app"]["uptime_seconds"]
    report["health"] = _collect_health(now, app_start_ts)

    report["stalls"] = _detect_stalls(report, now)
    report["degraded_sections"] = [
        name for name in _SECTION_NAMES if (report.get(name) or {}).get("degraded")
    ]
    report["degraded"] = bool(report["degraded_sections"])
    # §2.4 delivery needs the summary line and exit code as fields, not as a
    # string the caller has to reconstruct from the rendered text (BUG #10).
    report["summary"], report["exit_code"] = assess(report)
    return report
