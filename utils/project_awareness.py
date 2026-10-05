# utils/project_awareness.py
# Project awareness system — manages .crabcakes/ directory per project.
#
# Manifest:
#   - Reads: .crabcakes/* (project.md, team.json, context.md, awareness.json)
#   - Reads: git state via utils/git_ops.py
#   - Reads: package.json, pyproject.toml, etc. for tech stack detection
#   - Writes: .crabcakes/* (team.json, awareness.json, context.md)
#   - No GTK, no network, no secrets
#
# Architecture: pure Python utility. No imports from ui/, agent/, gateway/.
# File I/O only — reads/writes within the project's .crabcakes/ directory.
#
# Public API:
#   get_crabcakes_dir(project_path) -> str
#   init_project_config(project_path, project_name, pm_name, pm_id) -> None
#   load_project_manifest(project_path) -> str | None
#   load_team(project_path) -> ProjectTeam
#   save_team(project_path, team) -> None
#   load_project_context(project_path) -> str
#   save_project_context(project_path, content) -> None
#   build_awareness_snapshot(project_path, work_store=None) -> dict
#   save_awareness_snapshot(project_path, snapshot) -> None
#   is_project_onboarded(project_path) -> bool
#   build_awareness_block(project_path, work_store=None) -> str
#   detect_tech_stack(project_path) -> list[str]
#   generate_project_skeleton(project_path, project_name) -> None

import hashlib
import json
import logging
import os
import re
import shutil
import time
from typing import TYPE_CHECKING

from utils.prompt_loader import _untrusted_fence

if TYPE_CHECKING:
    from models.work_unit import WorkUnitStore

from models.team import ProjectTeam, TeamMember
from utils.config import get_projects_config_dir
from utils.prompt_paths import APP_USER_PROMPTS_DIR

_logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

CRABCAKES_DIR_NAME = ".crabcakes"
MANIFEST_FILENAME = "project.md"
TEAM_FILENAME = "team.json"
AWARENESS_FILENAME = "awareness.json"
CONTEXT_FILENAME = "context.md"

MAX_CONTEXT_SIZE = 50 * 1024  # 50 KB cap for context.md (write side)
CONTEXT_READ_CAP = 8000       # Max chars injected into agent prompts (read side)
MAX_CONTEXT_ENTRIES = 50      # Max entries before FIFO eviction

# Per-project prompts library seeding (SPEC-PROJECT-PROMPTS-DIRECTORY §2.1)
_USER_PROMPTS_SUBDIR = "prompts"
# Subdirs of <app>/prompts/ seeded into projects. system/ and claude-code-clean/
# are app-level (see utils.prompt_paths.APP_LEVEL_PROMPTS_SUBDIRS) and excluded.
_USER_PROMPTS_INCLUDE_SUBDIRS = ("default_agents",)

# ── Awareness mtime cache ─────────────────────────────────────────────────────
_AWARENESS_CACHE: dict[str, tuple[float, dict, str]] = {}
_AWARENESS_MAX_ENTRIES = 32


def _awareness_dir_mtime(project_path: str) -> float:
    """Max mtime of files in .crabcakes/ that affect awareness dict."""
    crab_dir = os.path.join(project_path, CRABCAKES_DIR_NAME)
    if not os.path.isdir(crab_dir):
        return 0.0
    try:
        m = os.stat(crab_dir).st_mtime
        for name in os.listdir(crab_dir):
            full = os.path.join(crab_dir, name)
            if os.path.isfile(full):
                m = max(m, os.stat(full).st_mtime)
        return m
    except OSError:
        return 0.0

# Phase CB-3: size caps for awareness variables (BUG #6 fix).
# See SPEC-CONTEXT-BLOAT-PHASE-3.md §2.4.
TEAM_ROSTER_MAX_CHARS = 500
CURRENT_STATE_MAX_CHARS = 1000


# ── Path helpers ─────────────────────────────────────────────────────────────


def get_crabcakes_dir(project_path: str) -> str:
    """Return the .crabcakes/ directory path for a project."""
    return os.path.join(project_path, CRABCAKES_DIR_NAME)


def _ensure_crabcakes_dir(project_path: str) -> str:
    """Create .crabcakes/ directory if it doesn't exist. Returns the path.
    Raises RuntimeError if .crabcakes exists as a file (not a directory).
    """
    d = get_crabcakes_dir(project_path)
    if os.path.isfile(d):
        raise RuntimeError(
            f"Cannot create .crabcakes/ directory: "
            f"a file named '.crabcakes' already exists at {project_path}"
        )
    os.makedirs(d, exist_ok=True)
    return d


# ── Initialization ───────────────────────────────────────────────────────────


def init_project_config(
    project_path: str,
    project_name: str = "",
    pm_name: str = "",
    pm_id: str = "",
) -> None:
    """
    Initialize .crabcakes/ for a project. Creates directory and skeleton files.

    Migration logic:
      1. If .crabcakes/ exists → do nothing (already initialized)
      2. If crabcakes.md at project root → copy to .crabcakes/project.md
      3. If legacy members.json in ~/.config/develcakes/projects/<name>/ → migrate
      4. Otherwise → generate skeleton
    """
    crab_dir = get_crabcakes_dir(project_path)
    # Guard: .crabcakes must not be a file
    if os.path.isfile(crab_dir):
        raise RuntimeError(
            f"Cannot create .crabcakes/ directory: "
            f"a file named '.crabcakes' already exists at {project_path}"
        )
    if os.path.isdir(crab_dir):
        # Already initialized — check team.json exists
        team_path = os.path.join(crab_dir, TEAM_FILENAME)
        if os.path.isfile(team_path):
            return
        # team.json missing — might be partial init, create it
        team = ProjectTeam(pm_name=pm_name, pm_id=pm_id)
        save_team(project_path, team)
        return

    _ensure_crabcakes_dir(project_path)

    # Migrate or create project.md
    manifest_path = os.path.join(crab_dir, MANIFEST_FILENAME)
    if not os.path.isfile(manifest_path):
        _create_project_manifest(project_path, project_name)

    # Migrate or create team.json
    team = ProjectTeam(pm_name=pm_name, pm_id=pm_id)
    save_team(project_path, team)

    # Create empty context.md
    context_path = os.path.join(crab_dir, CONTEXT_FILENAME)
    if not os.path.isfile(context_path):
        with open(context_path, "w", encoding="utf-8") as f:
            f.write("")

    # Create initial awareness.json
    snapshot = build_awareness_snapshot(project_path)
    save_awareness_snapshot(project_path, snapshot)


def _create_project_manifest(project_path: str, project_name: str) -> None:
    """Create .crabcakes/project.md with a basic structure if it doesn't exist."""
    manifest_path = os.path.join(get_crabcakes_dir(project_path), MANIFEST_FILENAME)
    if os.path.isfile(manifest_path):
        return  # Already exists, don't overwrite
    generate_project_skeleton(project_path, project_name)


def seed_project_prompts(project_path: str) -> bool:
    """Copy the app's user-facing prompt library into <project>/.crabcakes/prompts/.

    Copy-only-if-missing: existing project files are NEVER overwritten, so a
    project that customized a prompt keeps its local copy (the project is
    effectively branched after first seed — intentional, SPEC §2.1).

    Returns True on success (including no-op when dir exists), False on hard
    failure (source missing, cannot create dest, empty project_path). Idempotent.
    """
    if not project_path:
        _logger.warning("seed_project_prompts: empty project_path")
        return False
    if not os.path.isdir(APP_USER_PROMPTS_DIR):
        _logger.warning(
            "seed_project_prompts: app user-prompts dir missing: %s",
            APP_USER_PROMPTS_DIR,
        )
        return False

    dest_dir = os.path.join(get_crabcakes_dir(project_path), _USER_PROMPTS_SUBDIR)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        _logger.warning(
            "seed_project_prompts: cannot create %s: %s", dest_dir, e
        )
        return False

    # Top-level .md files
    try:
        entries = os.listdir(APP_USER_PROMPTS_DIR)
    except OSError as e:
        _logger.warning(
            "seed_project_prompts: cannot list %s: %s", APP_USER_PROMPTS_DIR, e
        )
        return False
    for fname in entries:
        src = os.path.join(APP_USER_PROMPTS_DIR, fname)
        if os.path.isfile(src) and fname.endswith(".md"):
            dst = os.path.join(dest_dir, fname)
            if os.path.isfile(dst):
                pass  # local copy exists — preserved (copy-only-if-missing)
            elif os.path.exists(dst):
                _logger.warning(
                    "seed: skipping %s — dest exists but is not a file", dst
                )
            else:
                try:
                    shutil.copy2(src, dst)
                except OSError as e:
                    _logger.warning("seed: copy %s failed: %s", src, e)

    # Whitelisted subdirs (one level deep, files only)
    for sub in _USER_PROMPTS_INCLUDE_SUBDIRS:
        src_sub = os.path.join(APP_USER_PROMPTS_DIR, sub)
        if not os.path.isdir(src_sub):
            continue
        dst_sub = os.path.join(dest_dir, sub)
        try:
            os.makedirs(dst_sub, exist_ok=True)
        except OSError as e:
            _logger.warning("seed: cannot create %s: %s", dst_sub, e)
            continue
        try:
            for fname in os.listdir(src_sub):
                src = os.path.join(src_sub, fname)
                dst = os.path.join(dst_sub, fname)
                if os.path.isfile(src):
                    if os.path.isfile(dst):
                        pass  # local copy exists — preserved (copy-only-if-missing)
                    elif os.path.exists(dst):
                        _logger.warning(
                            "seed: skipping %s — dest exists but is not a file", dst
                        )
                    else:
                        try:
                            shutil.copy2(src, dst)
                        except OSError as e:
                            _logger.warning("seed: copy %s failed: %s", src, e)
        except OSError as e:
            _logger.warning("seed: cannot list %s: %s", src_sub, e)

    return True


def generate_project_skeleton(project_path: str, project_name: str) -> None:
    """
    Create .crabcakes/project.md with a basic structure.
    """
    name = project_name or os.path.basename(project_path)
    crab_dir = _ensure_crabcakes_dir(project_path)
    manifest_path = os.path.join(crab_dir, MANIFEST_FILENAME)

    skeleton = f"""# {name}

## Purpose
<!-- Describe what this project does and why it exists. -->

## Stack
<!-- List languages, frameworks, key dependencies. -->
<!-- Detected: {', '.join(detect_tech_stack(project_path)) or 'unknown'} -->

## Entry Points
<!-- Main files and modules. -->

## Conventions
<!-- Tests: how to run them. Lint: how to check. Style: formatting rules. -->

## Notes
<!-- Any project-specific context. -->
"""
    with open(manifest_path, "w", encoding="utf-8") as f:
        f.write(skeleton)


# ── Manifest ─────────────────────────────────────────────────────────────────


def load_project_manifest(project_path: str) -> str | None:
    """
    Read .crabcakes/project.md. Returns raw markdown, or None if not found.
    """
    path = os.path.join(get_crabcakes_dir(project_path), MANIFEST_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _ends_line_in_comment(in_comment: bool, line: str) -> bool:
    """Return whether ``line`` ends while inside an HTML comment block.

    Tracks ``<!--`` open / ``-->`` close delimiters across a line (both may
    appear on the same line or on different lines). This mirrors the
    code-fence tracking in ``_split_entries``: a ``## `` heading inside a
    comment block must NOT be treated as a section boundary.

    Edge cases handled:
      - Same-line open+close: ``<!-- foo -->`` → False.
      - Open without close: ``<!-- foo`` → True.
      - Open on a prior line, closed here: ``end -->`` → False.
    """
    i = 0
    n = len(line)
    while i < n:
        if in_comment:
            close = line.find("-->", i)
            if close == -1:
                return True  # still open at end of line
            in_comment = False
            i = close + 3
        else:
            open_idx = line.find("<!--", i)
            if open_idx == -1:
                return False  # no open marker on the rest of the line
            in_comment = True
            i = open_idx + 4
    return in_comment


def clean_manifest_skeleton(project_path: str) -> bool:
    """Remove comment-only sections from .crabcakes/project.md.

    Splits the manifest into the preamble (before the first ``## `` heading)
    plus ``## ``-delimited sections using line boundaries. A section is dropped
    only when its body (after the heading line) is whitespace-only once HTML
    comments are removed. Sections with any real non-comment, non-whitespace
    content are preserved VERBATIM (comments left intact).

    Section parsing is comment-aware: a ``## `` line that appears INSIDE a
    ``<!-- ... -->`` comment block is NOT treated as a section boundary, so a
    comment like ``<!-- see ## Notes -->`` cannot swallow later sections.
    This is why cleanup is NOT a whole-file ``re.sub(..., flags=re.DOTALL)``
    (spec §2.8).

    The top-level ``# Title`` (preamble) is never removed.

    This is a read + conditional write. Missing/unreadable files are a safe
    no-op returning False (no exception). The file is written only when at
    least one section is removed; otherwise returns False.

    Args:
        project_path: Absolute path to the project root.

    Returns:
        True if the manifest was rewritten (a comment-only section was removed),
        False otherwise (no change / missing file / nothing to clean).
    """
    manifest = load_project_manifest(project_path)
    if manifest is None:
        return False

    lines = manifest.split("\n")

    # Group lines into the preamble and sections by '## ' heading boundaries.
    # A line is a heading only if it starts with '## ' AND we are not inside
    # an HTML comment block.
    in_comment = False
    sections: list[tuple[int, int]] = []  # (start_index, end_index) exclusive
    current_start: int | None = None

    for i, line in enumerate(lines):
        is_heading = line.startswith("## ") and not in_comment
        in_comment = _ends_line_in_comment(in_comment, line)
        if is_heading:
            if current_start is not None:
                # Close the prior section (runs up to, but not including, this heading)
                sections.append((current_start, i))
            current_start = i
    if current_start is not None:
        sections.append((current_start, len(lines)))

    # Decide which sections to drop: those whose body is whitespace-only once
    # HTML comments are removed. done per-section on the BODY only (not heading).
    drops: list[tuple[int, int]] = []
    for start, end in sections:
        body = "\n".join(lines[start + 1:end])
        stripped_body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).strip()
        if not stripped_body:
            drops.append((start, end))

    if not drops:
        return False  # nothing to clean — no write

    drop_indices = set()
    for start, end in drops:
        drop_indices.update(range(start, end))

    kept_lines = [line for idx, line in enumerate(lines) if idx not in drop_indices]
    result = "\n".join(kept_lines)

    if result == manifest:
        return False  # defensive — content unchanged, no write

    path = os.path.join(get_crabcakes_dir(project_path), MANIFEST_FILENAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(result)
    except OSError as e:
        _logger.error("clean_manifest_skeleton: failed to write %s: %s", path, e)
        return False
    return True


def is_project_onboarded(project_path: str) -> bool:
    """True if project has been onboarded (has real content in project.md or context.md).

    Detection: strip HTML comments from project.md. If no content lines remain
    beyond section headers, and context.md is empty, the project hasn't been onboarded.
    """
    manifest = load_project_manifest(project_path)
    if manifest is None:
        return False
    # Strip HTML comments — if nothing remains, it's still a skeleton
    stripped = re.sub(r'<!--.*?-->', '', manifest, flags=re.DOTALL).strip()
    # Check for any real content beyond section headers
    content_lines = [l for l in stripped.split('\n') if l.strip() and not l.startswith('#')]
    if content_lines:
        return True
    # Also check context.md
    context = load_project_context(project_path)
    return bool(context.strip())


# ── Team ──────────────────────────────────────────────────────────────────────


def load_team(project_path: str) -> ProjectTeam:
    """
    Read .crabcakes/team.json. Returns empty ProjectTeam if not found.
    """
    path = os.path.join(get_crabcakes_dir(project_path), TEAM_FILENAME)
    if not os.path.isfile(path):
        return ProjectTeam()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return ProjectTeam.from_dict(data)
    except (json.JSONDecodeError, OSError):
        return ProjectTeam()


def save_team(project_path: str, team: ProjectTeam) -> None:
    """
    Write ProjectTeam to .crabcakes/team.json.
    Logs error instead of raising on I/O failure.
    """
    try:
        _ensure_crabcakes_dir(project_path)
        path = os.path.join(get_crabcakes_dir(project_path), TEAM_FILENAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(team.to_dict(), f, indent=2)
    except OSError as e:
        _logger.error("save_team: failed to write team.json at %s: %s", project_path, e)


# ── Context memory ────────────────────────────────────────────────────────────


def load_project_context(project_path: str) -> str:
    """
    Read .crabcakes/context.md. Returns empty string if not found.
    """
    path = os.path.join(get_crabcakes_dir(project_path), CONTEXT_FILENAME)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def save_project_context(project_path: str, content: str) -> None:
    """
    Write content to .crabcakes/context.md. Enforces 50KB cap by truncating
    oldest content (from the top) if exceeded.
    Logs error instead of raising on I/O failure.
    """
    try:
        _ensure_crabcakes_dir(project_path)
        path = os.path.join(get_crabcakes_dir(project_path), CONTEXT_FILENAME)

        # Enforce size cap — trim from top (oldest content)
        if len(content) > MAX_CONTEXT_SIZE:
            content = content[len(content) - MAX_CONTEXT_SIZE:]

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        _logger.error("save_project_context: failed at %s: %s", project_path, e)


# Word-boundary completion detection. Prevents false positives like
# "abandoned", "undone", "incomplete", "condone" matching "done".
# See Phase 3 audit BUG #2.
_COMPLETION_RE = re.compile(
    r"\b(?:complete|completed|done|finished)\b|✅",
    re.IGNORECASE,
)


def _signals_completion(entry: str) -> bool:
    """Return True if an entry signals phase/task completion.

    Uses word-boundary matching so "abandoned", "undone", "incomplete",
    "condone" do NOT trigger completion. Detects: complete, completed, done,
    finished (case-insensitive), and the ✅ emoji.

    See SPEC-CONTEXT-MD-SYSTEM-FIX.md §3.1d + Phase 3 audit BUG #2.
    """
    return bool(_COMPLETION_RE.search(entry))


def _extract_phase_id(entry: str) -> str:
    """Extract a phase/task identifier from an entry's '## ' heading.

    Handles the standard context.md format: '## DATE — PHASE DESCRIPTION'.
    Returns the text after the em-dash (or the first '## ' heading's body if
    no em-dash), stripped of trailing status words and whitespace.
    Returns empty string if no '## ' heading is found.

    Examples:
      '## 2026-07-20 — Phase B6 complete'  -> 'phase b6'
      '## 2026-07-19 — Phase A1 complete'  -> 'phase a1'
      '## 2026-07-20 — In-flight loops'    -> 'in-flight loops'
      'no heading here'                    -> ''
    """
    # Find the first '## ' heading in the entry
    heading = None
    for line in entry.split("\n"):
        if line.startswith("## "):
            heading = line[3:].strip()
            break
    if not heading:
        return ""

    # Split on common date/phase separators. Em-dash is the canonical format;
    # en-dash and double-hyphen also appear. Single hyphen is EXCLUDED because
    # it matches the date portion (2026-07-20). See Phase 3 audit BUG #3.
    for sep in ("—", "–", "--", ":"):
        if sep in heading:
            # Take everything after the separator, strip status words
            after = heading.split(sep, 1)[1].strip().lstrip("—–-:").strip()
            # Remove trailing completion/status words for matching
            for word in ("complete", "completed", "done", "finished", "✅"):
                if after.lower().endswith(" " + word):
                    after = after[: -len(word)].rstrip()
                    break
            # Strip trailing punctuation (colons, periods, dashes) that would
            # break substring matching against differently-punctuated headings.
            return after.rstrip(":.-—").rstrip().lower()

    # No separator — use the whole heading body (minus status words)
    for word in ("complete", "completed", "done", "finished", "✅"):
        if heading.lower().endswith(" " + word):
            heading = heading[: -len(word)].rstrip()
            break
    return heading.rstrip(":.-—").rstrip().lower()


def _mark_superseded(existing: str, new_entry: str) -> str:
    """Mark 'in progress' / 'pending' entries as [SUPERSEDED] when a completion arrives.

    Extracts a phase identifier from the new entry's heading by splitting on
    the em-dash separator (the standard context.md format: '## DATE — PHASE').
    Then scans existing entry headings for entries that:
      (a) contain the same phase identifier (word-boundary match, case-insensitive), AND
      (b) indicate 'in progress', 'pending', or 'current task' status.

    Matching entries are marked '[SUPERSEDED]' (appended to the heading line).
    Already-superseded entries are not re-marked. Entries are never deleted.

    Returns the modified existing-content string. If no phase identifier can
    be extracted from the new entry, returns existing unchanged (conservative).
    """
    phase_id = _extract_phase_id(new_entry)
    if not phase_id:
        return existing  # No identifiable phase — don't touch anything

    # Build a word-boundary regex so 'phase a1' does NOT match 'phase a10'.
    # re.escape handles any regex metacharacters in the phase identifier.
    # The trailing \b prevents suffix matches (a1 matching a10); the leading
    # \b prevents prefix matches.
    try:
        phase_pattern = re.compile(r"\b" + re.escape(phase_id) + r"\b", re.IGNORECASE)
    except re.error:
        # If the phase_id is somehow un-escapable, fall back to no supersession
        # (conservative — don't risk false positives).
        return existing

    lines = existing.split("\n")
    result = []
    for line in lines:
        if line.startswith("## ") and phase_pattern.search(line):
            lower = line.lower()
            if any(w in lower for w in ("in progress", "pending", "current task")):
                if "[SUPERSEDED]" not in line:
                    line = line + " [SUPERSEDED]"
        result.append(line)
    return "\n".join(result)


def _split_entries(content: str) -> list[str]:
    """Split context.md content into individual entries by '## ' heading delimiter.

    Each entry starts with '## ' (the standard dated-heading format). Content
    before the first '## ' heading (if any) is prepended to the first entry's
    body, or dropped if it's only whitespace.

    Code-block awareness: '## ' inside a fenced code block (triple backticks)
    is NOT treated as a heading boundary. See Phase 3 audit BUG #5.

    Returns a list of entry strings, each starting with '## '. Empty list if
    content is empty or has no '## ' headings.
    """
    if not content.strip():
        return []

    lines = content.split("\n")
    preamble: list[str] = []
    entries: list[str] = []
    current: list[str] = []
    in_code_block = False
    seen_heading = False

    for line in lines:
        # Track code-block context (triple backtick fence).
        if line.lstrip().startswith("```"):
            in_code_block = not in_code_block
            if current:
                current.append(line)
            elif preamble:
                preamble.append(line)
            continue

        # A heading boundary is '## ' at line start, OUTSIDE a code block.
        if not in_code_block and line.startswith("## "):
            if not seen_heading:
                # First heading — flush preamble into the new entry's body
                seen_heading = True
                current = list(preamble) + [line] if preamble else [line]
                preamble = []
            else:
                # Subsequent heading — flush current entry, start new one
                if current:
                    entries.append("\n".join(current))
                current = [line]
        elif not seen_heading:
            # Before any heading — accumulate as preamble
            preamble.append(line)
        else:
            # Inside an entry body
            if current:
                current.append(line)

    # Flush the last entry
    if current:
        entries.append("\n".join(current))

    # If there was preamble but no headings at all, return empty
    # (append_project_context's fallback handles non-heading content).
    return [e.strip() for e in entries if e.strip()]


def append_project_context(project_path: str, entry: str) -> None:
    """Append an entry to .crabcakes/context.md with lifecycle management.

    - Supersedes stale "in progress" / "pending" entries for the same phase
      when a "complete" / "done" entry is appended. Marks them [SUPERSEDED]
      in place (does not delete — preserves audit trail).
    - Enforces MAX_CONTEXT_ENTRIES with FIFO eviction (oldest entries dropped).

    The entry is expected to start with a '## ' heading. Entries without a
    heading are appended without supersedure processing (format preserved).

    See SPEC-CONTEXT-MD-SYSTEM-FIX.md §3.1d.
    """
    existing = load_project_context(project_path)

    # Supersedure: if the new entry signals completion, mark matching
    # "in progress" entries as [SUPERSEDED].
    if _signals_completion(entry):
        existing = _mark_superseded(existing, entry)

    # FIFO eviction: split into entries, cap at MAX_CONTEXT_ENTRIES.
    # The new entry is appended AFTER eviction so it is never the one evicted.
    entries = _split_entries(existing)
    if not entries and existing.strip():
        # No '## ' headings found — preserve the entire existing content
        # as a single entry to avoid data loss.
        entries = [existing.strip()]
    if len(entries) >= MAX_CONTEXT_ENTRIES:
        entries = entries[-(MAX_CONTEXT_ENTRIES - 1):]
    entries.append(entry)

    save_project_context(project_path, "\n\n".join(entries))


def get_current_task(project_path: str) -> str:
    """Extract the most recent dated entry heading from context.md.

    Returns the heading text (e.g., "2026-07-20 — Phase B6 complete") or
    empty string if context.md is empty or has no '## ' headings.

    This is injected as a TRUSTED directive (outside the untrusted fence)
    so agents treat it as an operational instruction, not data.
    """
    context = load_project_context(project_path)
    if not context.strip():
        return ""
    # Find the last '## ' heading
    headings = [line for line in context.split("\n") if line.startswith("## ")]
    if not headings:
        return ""
    return headings[-1][3:].strip()  # strip "## " prefix (3 chars)


# ── Awareness snapshot ────────────────────────────────────────────────────────


def build_awareness_snapshot(
    project_path: str,
    work_store: "WorkUnitStore | None" = None,
) -> dict:
    """
    Build the awareness.json dict from live state.
    Gathers: git state, work unit summary, team size, tech stack, review mode.
    """
    # Git state
    git_info = _get_git_info(project_path)

    # Work unit summary (spec §6.1: Work Units, not TaskStore)
    task_info = _get_task_info(work_store)

    # Team info
    team = load_team(project_path)

    # Tech stack
    tech_stack = detect_tech_stack(project_path)

    return {
        "project_name": os.path.basename(project_path),
        "project_path": project_path,
        "opened_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git": git_info,
        "tasks": task_info,
        "team_size": len(team.members),
        "review_mode": "off",  # updated by ReviewHandler when review starts
        "tech_stack": tech_stack,
    }


def save_awareness_snapshot(project_path: str, snapshot: dict) -> None:
    """Write awareness.json. Logs error instead of raising on I/O failure."""
    try:
        _ensure_crabcakes_dir(project_path)
        path = os.path.join(get_crabcakes_dir(project_path), AWARENESS_FILENAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
    except OSError as e:
        _logger.error("save_awareness_snapshot: failed at %s: %s", project_path, e)


def _get_git_info(project_path: str) -> dict:
    """Extract git state for awareness snapshot."""
    try:
        from utils.git_ops import get_head_sha, get_branch, log, status

        sha_result = get_head_sha(project_path)
        if not sha_result.success:
            return {"available": False}

        log_result = log(project_path, count=5)
        recent = []
        if log_result.success and log_result.stdout:
            for line in log_result.stdout.strip().split("\n"):
                if line.strip():
                    parts = line.split(" ", 1)
                    recent.append({
                        "sha": parts[0] if parts else "",
                        "message": parts[1] if len(parts) > 1 else "",
                    })

        status_result = status(project_path)
        dirty = status_result.success and bool(status_result.stdout.strip())

        branch_result = get_branch(project_path)
        branch = branch_result.stdout.strip() if branch_result.success else "unknown"

        return {
            "available": True,
            "branch": branch,
            "head_sha": sha_result.sha or "",
            "recent_commits": recent,
            "dirty": dirty,
        }
    except Exception:
        return {"available": False}


def _get_task_info(work_store: "WorkUnitStore | None") -> dict:
    """Extract Work Unit counts for awareness snapshot (spec §6.1).

    Mapping (explicit and tested):
      - total        = all units (including cancelled — reflects all work).
      - spec_pending = status 'draft' OR 'spec-pending'.
      - spec_ready   = status 'spec-ready'.
      - in_progress  = status 'in-progress' OR 'auditing'.
      - done         = status 'done'.
      - cancelled units count ONLY in total, never in a bucket.

    Draft is folded into spec_pending for awareness purposes (spec §6.1).
    """
    if work_store is None:
        return {
            "total": 0,
            "spec_pending": 0,
            "spec_ready": 0,
            "in_progress": 0,
            "done": 0,
        }

    all_units = work_store.list_all()
    return {
        "total": len(all_units),
        "spec_pending": sum(
            1 for u in all_units if u.status in ("draft", "spec-pending")
        ),
        "spec_ready": sum(
            1 for u in all_units if u.status == "spec-ready"
        ),
        "in_progress": sum(
            1 for u in all_units if u.status in ("in-progress", "auditing")
        ),
        "done": sum(1 for u in all_units if u.status == "done"),
    }


def _work_units_for_awareness(project_path: str) -> "WorkUnitStore | None":
    """Load this project's Work Units into a throwaway store for awareness.

    Spec §6.1 'project-bound loader': build_awareness_dict is a read path that
    never threads a work_store through its callers, so the project's Work
    Units are loaded here on demand. Returns None on ANY error — awareness is
    best-effort and must never crash the system-prompt build (a corrupt
    work.json yields None, i.e. zero-count awareness, not a crash).
    """
    try:
        from utils.work_persistence import load_or_migrate_work_units
        from models.work_unit import WorkUnitStore
        units = load_or_migrate_work_units(project_path)
    except Exception:
        return None
    store = WorkUnitStore()
    store.replace_all(units)
    return store


# ── Awareness block builder (for injection) ──────────────────────────────────


def build_awareness_block(
    project_path: str,
    work_store: "WorkUnitStore | None" = None,
) -> str:
    """
    Assemble the full awareness text block for agent injection.

    Combines:
      1. Project manifest header (purpose, stack)
      2. Team roster (who's working on this project)
      3. Dynamic state summary (git, tasks, review mode)
      4. Persistent context memory (cross-session notes)

    Returns formatted string suitable for system prompt injection or gateway message.
    """
    parts: list[str] = []

    # 1. Project manifest
    manifest = load_project_manifest(project_path)
    if manifest:
        # HIGH-5: wrap in untrusted-data fence to mitigate prompt injection
        manifest_wrapped = _untrusted_fence(
            manifest[:2000], "project.md"
        )
        if len(manifest) > 2000:
            manifest_wrapped += "\n[... project manifest truncated ...]"
        parts.append(f"## Project Manifest\n\n{manifest_wrapped}")

    # 2. Team roster
    team = load_team(project_path)
    if team.members:
        lines = ["## Team"]
        if team.pm_name:
            lines.append(f"PM: {team.pm_name}")
        for m in team.members:
            role_str = f" — {m.role}" if m.role else ""
            write_str = " [write]" if m.can_write else ""
            lines.append(f"- {m.name} ({m.session_key}){role_str}{write_str}")
        parts.append("\n".join(lines))
    elif team.pm_name:
        parts.append(f"## Team\nPM: {team.pm_name}\nNo other members yet.")

    # 3. Dynamic state
    snapshot = build_awareness_snapshot(project_path, work_store)
    state_lines = ["## Current State"]
    state_lines.append(f"Project: {snapshot.get('project_name', 'unknown')}")
    state_lines.append(f"Path: {snapshot.get('project_path', '')}")

    git = snapshot.get("git", {})
    if git.get("available"):
        state_lines.append(f"Git: {git.get('head_sha', '?')[:7]} ({'dirty' if git.get('dirty') else 'clean'})")
        recent = git.get("recent_commits", [])
        if recent:
            for c in recent[:3]:
                state_lines.append(f"  {c.get('sha', '?')[:7]} {c.get('message', '')}")
    else:
        state_lines.append("Git: not available")

    tasks = snapshot.get("tasks", {})
    if tasks.get("total", 0) > 0:
        state_lines.append(
            f"Work: {tasks.get('in_progress', 0)} in progress, "
            f"{tasks.get('spec_ready', 0)} spec-ready, "
            f"{tasks.get('spec_pending', 0)} spec-pending, "
            f"{tasks.get('done', 0)} done"
        )

    state_lines.append(f"Review mode: {snapshot.get('review_mode', 'off')}")
    parts.append("\n".join(state_lines))

    # 4. Persistent context
    context = load_project_context(project_path)
    if context.strip():
        # HIGH-5: wrap in untrusted-data fence to mitigate prompt injection
        context_wrapped = _untrusted_fence(
            context[:CONTEXT_READ_CAP], "context.md"
        )
        if len(context) > CONTEXT_READ_CAP:
            context_wrapped += "\n[... context memory truncated ...]"
        parts.append(f"## Project Memory\n\n{context_wrapped}")

    return "\n\n".join(parts)


# ── Tech stack detection ─────────────────────────────────────────────────────


def detect_tech_stack(project_path: str) -> list[str]:
    """
    Detect technologies from project files.
    Returns list of detected tech identifiers.
    """
    stack: list[str] = []

    detectors = {
        "package.json": ["javascript", "node"],
        "pyproject.toml": ["python"],
        "setup.py": ["python"],
        "requirements.txt": ["python"],
        "Pipfile": ["python"],
        "Cargo.toml": ["rust"],
        "go.mod": ["go"],
        "Gemfile": ["ruby"],
        "pom.xml": ["java"],
        "build.gradle": ["java"],
        "CMakeLists.txt": ["c/c++"],
        "Makefile": ["c/c++"],
    }

    try:
        entries = os.listdir(project_path)
    except OSError:
        return stack

    for entry in entries:
        if entry in detectors:
            stack.extend(detectors[entry])

    # Check for GTK via Python imports
    for indicator in ["pyproject.toml", "setup.py", "requirements.txt"]:
        indicator_path = os.path.join(project_path, indicator)
        if os.path.isfile(indicator_path):
            try:
                with open(indicator_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read().lower()
                if "pygobject" in content or "pygtk" in content or "gi.require_version" in content:
                    if "gtk4" not in stack:
                        stack.append("gtk4")
                if "websockets" in content and "websocket" not in stack:
                    stack.append("websocket")
            except OSError:
                pass

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for s in stack:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


# ── Awareness dict (for prompt_loader template variables) ────────────────


def build_awareness_dict(project_path: str) -> dict[str, str]:
    """Return awareness data as a dict of template variables.

    Keys: PROJECT_NAME, TEAM_ROSTER, CURRENT_STATE, PROJECT_MEMORY, CURRENT_TASK, WORKFLOW_STATUS.
    Parallel to build_awareness_block() but returns structured data
    instead of formatted text.
    """
    # Mtime cache — skip if .crabcakes/ hasn't changed
    if project_path and os.path.isdir(project_path):
        mtime = _awareness_dir_mtime(project_path)
        cached = _AWARENESS_CACHE.get(project_path)
        # Cache key includes context.md content hash (SHA1) so same-second writes
        # with different content invalidate the cache (filesystem mtime is
        # typically 1-second granularity). See FIX audit BUG #2 + BUG #8.
        _ctx_for_fp = load_project_context(project_path)
        _content_fp = hashlib.sha1(_ctx_for_fp.encode("utf-8", errors="replace")).hexdigest()
        if cached and cached[0] >= mtime and cached[2] == _content_fp:
            return cached[1]

    parts: dict[str, str] = {}

    # Project name
    manifest = load_project_manifest(project_path)
    name_match = re.search(r"^#\s+(.+)$", manifest, re.MULTILINE) if manifest else None
    parts["PROJECT_NAME"] = name_match.group(1).strip() if name_match else os.path.basename(project_path)

    # Team roster
    team = load_team(project_path)
    if team.members:
        lines = []
        if team.pm_name:
            lines.append(f"PM: {team.pm_name}")
        for m in team.members:
            role_str = f" — {m.role}" if m.role else ""
            write_str = " [write]" if m.can_write else ""
            lines.append(f"- {m.name} ({m.session_key}){role_str}{write_str}")
        roster = "\n".join(lines)
        # Phase CB-3: cap TEAM_ROSTER at TEAM_ROSTER_MAX_CHARS (BUG #6 fix).
        if len(roster) > TEAM_ROSTER_MAX_CHARS:
            roster = roster[:TEAM_ROSTER_MAX_CHARS] + "\n[... team roster truncated ...]"
        parts["TEAM_ROSTER"] = roster
    else:
        parts["TEAM_ROSTER"] = "No team members yet."

    # Current state
    snapshot = build_awareness_snapshot(
        project_path, _work_units_for_awareness(project_path)
    )
    state_lines = [f"Project: {snapshot.get('project_name', 'unknown')}"]
    state_lines.append(f"Path: {snapshot.get('project_path', '')}")
    git = snapshot.get("git", {})
    if git.get("available"):
        state_lines.append(f"Git: {git.get('head_sha', '?')[:7]} ({'dirty' if git.get('dirty') else 'clean'})")
    else:
        state_lines.append("Git: not available")
    tasks = snapshot.get("tasks", {})
    if tasks.get("total", 0) > 0:
        state_lines.append(
            f"Work: {tasks.get('in_progress', 0)} in progress, "
            f"{tasks.get('spec_ready', 0)} spec-ready, "
            f"{tasks.get('spec_pending', 0)} spec-pending, "
            f"{tasks.get('done', 0)} done"
        )
    state_lines.append(f"Review mode: {snapshot.get('review_mode', 'off')}")
    state = "\n".join(state_lines)
    # Phase CB-3: cap CURRENT_STATE at CURRENT_STATE_MAX_CHARS (BUG #6 fix).
    if len(state) > CURRENT_STATE_MAX_CHARS:
        state = state[:CURRENT_STATE_MAX_CHARS] + "\n[... current state truncated ...]"
    parts["CURRENT_STATE"] = state

    # Project memory
    context = load_project_context(project_path)
    if context.strip():
        # HIGH-5: wrap in untrusted-data fence to mitigate prompt injection
        context_wrapped = _untrusted_fence(context[:CONTEXT_READ_CAP], "context.md")
        if len(context) > CONTEXT_READ_CAP:
            context_wrapped += "\n[... context memory truncated ...]"
        parts["PROJECT_MEMORY"] = context_wrapped
    else:
        parts["PROJECT_MEMORY"] = ""

    # Current task — extracted from the latest context.md heading.
    # Injected as TRUSTED data (not in the untrusted fence) because it is
    # an operational directive the agent must follow.
    task = get_current_task(project_path)
    parts["CURRENT_TASK"] = task if task else "(no current task recorded)"

    # Workflow status — lazy import to avoid circular dependency
    try:
        from utils.workflow_state import get_workflow_content
        wf = get_workflow_content(project_path)
        parts["WORKFLOW_STATUS"] = wf if wf.strip() else "(workflow.md not yet initialized)"
    except Exception:
        parts["WORKFLOW_STATUS"] = "(workflow state unavailable)"

    # Cache write
    if project_path and os.path.isdir(project_path):
        if len(_AWARENESS_CACHE) >= _AWARENESS_MAX_ENTRIES:
            oldest = min(_AWARENESS_CACHE, key=lambda k: _AWARENESS_CACHE[k][0])
            _AWARENESS_CACHE.pop(oldest, None)
        _AWARENESS_CACHE[project_path] = (mtime, parts, _content_fp)
    return dict(parts)  # shallow copy — values are immutable strings