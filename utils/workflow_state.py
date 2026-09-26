"""
Workflow state tracker for develcakes projects.

Manages .crabcakes/workflow.md — tracks which workflow phases are done,
which is current, and timestamps.

Phase names: "onboarding", "discovery", "architecture", "spec-planning",
"implementation", "testing", "ship"

Each phase row includes a Prompt column that names the prompt file governing
that phase, so agents know exactly which prompt to load when a phase is active.

Usage:
    from utils.workflow_state import init_workflow, advance_phase, get_current_phase
    init_workflow("/path/to/project")
    advance_phase("/path/to/project", "discovery")
    phase = get_current_phase("/path/to/project")
"""

import logging
import os
import re
from datetime import datetime, timezone

from utils.project_awareness import get_crabcakes_dir

# ── Phase ordering ────────────────────────────────────────────────────────────

PHASES = [
    "onboarding",
    "discovery",
    "architecture",
    "spec-planning",   # was "task-planning" (SPEC-TASK-SYSTEM-FULL-REDESIGN §7.1)
    "implementation",
    "testing",
    "ship",
]

# Map phase name → index
_PHASE_INDEX = {name: i for i, name in enumerate(PHASES)}

# ── Phase prompt mapping ─────────────────────────────────────────────────────
# Each phase is governed by a specific prompt file. This is surfaced in the
# workflow.md table so agents see exactly which prompt to load for the active
# phase — no guessing, no searching.

PHASE_PROMPTS = {
    "onboarding":     "`prompts/system/project-onboarding.md`",
    "discovery":      "`prompts/cc-discovery.md`",
    "architecture":   "`prompts/cc-architecture-design.md`",
    "spec-planning":  "`prompts/cc-spec-planning.md`",   # was task-planning → cc-task-planning.md
    "implementation": "`prompts/implementationLoop.md`",
    "testing":        "`prompts/steelFramedCodeWriter.md`",
    "ship":           "`prompts/cc-workflow-guide.md`",
}

# ── Phase row helpers ─────────────────────────────────────────────────────────

_NEW_HEADER = "| # | Phase | Prompt | Status | Started | Completed | Notes |"

# Regex for phase name: matches word chars AND hyphens (e.g. "task-planning").
# BUGFIX: original code used \w+ which does NOT match hyphens, so
# "task-planning" could never be found or advanced.
_PHASE_NAME_RE = r"[\w-]+"


def _make_phase_row(
    idx: int,
    name: str,
    status: str,
    started: str,
    completed: str,
    notes: str,
) -> str:
    """Return one table row line (WITHOUT trailing newline — join handles that)."""
    prompt = PHASE_PROMPTS.get(name, "—")
    return f"| {idx} | {name} | {prompt} | {status} | {started} | {completed} | {notes} |"


def _initial_workflow_content() -> str:
    """Return the initial workflow.md content as a single string with proper newlines."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = [
        "## Phase History",
        _NEW_HEADER,
        "|---|-------|--------|--------|---------|-----------|-------|",
    ]
    rows += [
        _make_phase_row(i, name, "🔄 current" if i == 0 else "⏳ pending", now, "—", "...")
        for i, name in enumerate(PHASES)
    ]
    return "\n".join(rows) + "\n"


# ── Old-format detection + migration ─────────────────────────────────────────


def _is_old_format(lines: list[str]) -> bool:
    """True if the table header lacks the Prompt column (6-column old format)."""
    for line in lines:
        if line.strip().startswith("| # |") and "Prompt" not in line:
            return True
    return False


def _parse_old_row(line: str) -> tuple[int, str, str, str, str, str] | None:
    """Parse a 6-column old-format row.

    Old: | idx | name | status | started | completed | notes |
    Returns (idx, name, status, started, completed, notes) or None.
    """
    m = re.match(
        rf"\|\s*(\d+)\s*\|\s*({_PHASE_NAME_RE})\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.*?)\s*\|",
        line,
    )
    if not m:
        return None
    return (int(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6))


def _parse_new_row(line: str) -> tuple[int, str, str, str, str, str, str] | None:
    """Parse a 7-column new-format row.

    New: | idx | name | prompt | status | started | completed | notes |
    Returns (idx, name, prompt, status, started, completed, notes) or None.
    """
    m = re.match(
        rf"\|\s*(\d+)\s*\|\s*({_PHASE_NAME_RE})\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.*?)\s*\|",
        line,
    )
    if not m:
        return None
    return (int(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6), m.group(7))


def _migrate_old_format(lines: list[str]) -> list[str]:
    """Upgrade a 6-column workflow.md table to 7-column format in-place.

    - Replaces the header and separator with the new format
    - Re-emits every phase row with the Prompt column inserted
    - Preserves all existing status/started/completed/notes values
    """
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        # Replace header
        if stripped.startswith("| # | Phase | Status |"):
            new_lines.append(_NEW_HEADER)
            continue
        # Replace separator (matching 6-dash-column pattern)
        if stripped.startswith("|---|") and stripped.count("|") == 7:
            new_lines.append("|---|-------|--------|--------|---------|-----------|-------|")
            continue
        # Try old-row parse
        old = _parse_old_row(stripped)
        if old is not None:
            idx, name, status, started, completed, notes = old
            new_lines.append(_make_phase_row(idx, name, status, started, completed, notes))
            continue
        # Not a table row — pass through
        new_lines.append(line)
    return new_lines


# ── Read / write helpers ──────────────────────────────────────────────────────


def _read_workflow_lines(project_path: str) -> list[str] | None:
    """Read workflow.md as lines, stripping trailing newlines from each.

    If the file is in old 6-column format, transparently migrate to 7-column
    format and persist the upgrade so subsequent reads are fast.
    """
    path = os.path.join(get_crabcakes_dir(project_path), "workflow.md")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        lines = content.splitlines()
    except OSError:
        return None

    if not lines:
        return None

    # Auto-migrate old format on read (transparent, persisted)
    if _is_old_format(lines):
        lines = _migrate_old_format(lines)
        _write_workflow_lines(project_path, lines)

    # SPEC-TASK-SYSTEM-FULL-REDESIGN §7.1: migrate task-planning → spec-planning.
    # Runs AFTER the 6→7 column migration (which may emit task-planning rows).
    # Preserves status/started/completed/notes; only the phase-name and prompt
    # cells are rewritten (via _make_phase_row, which looks up PHASE_PROMPTS).
    migrated = False
    for index, line in enumerate(lines):
        parsed = _parse_new_row(line)
        if parsed is None or parsed[1] != "task-planning":
            continue
        phase_idx, _name, _prompt, status, started, completed, notes = parsed
        lines[index] = _make_phase_row(
            phase_idx, "spec-planning", status, started, completed, notes
        )
        migrated = True
    if migrated:
        _write_workflow_lines(project_path, lines)
        # Post-migration verification: re-parse every line that looks like a
        # phase row; a failure is logged (defensive — never crash the read).
        for line in lines:
            if line.strip().startswith("| ") and re.match(r"\|\s*\d+\s*\|", line):
                if _parse_new_row(line.strip()) is None:
                    logging.getLogger(__name__).warning(
                        "workflow_state: phase row failed to parse after "
                        "task-planning→spec-planning migration: %r",
                        line,
                    )

    return lines


def _write_workflow_lines(project_path: str, lines: list[str]) -> None:
    """Write lines to workflow.md, each line followed by \\n (last line too)."""
    path = os.path.join(get_crabcakes_dir(project_path), "workflow.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _find_phase_line_idx(lines: list[str], phase_idx: int, phase_name: str) -> int | None:
    """Find the table row index in lines matching phase by index and name."""
    for i, line in enumerate(lines):
        m = re.match(rf"\|\s*(\d+)\s*\|\s*({_PHASE_NAME_RE})\s*\|", line)
        if m and int(m.group(1)) == phase_idx and m.group(2) == phase_name:
            return i
    return None


def _replace_phase_row(
    lines: list[str],
    phase_idx: int,
    phase_name: str,
    new_status: str,
    new_completed: str,
) -> list[str]:
    """Find the matching phase row and replace its status + completed columns.

    Assumes 7-column format (post-migration). Preserves started + notes.
    """
    new_lines = list(lines)
    row_idx = _find_phase_line_idx(lines, phase_idx, phase_name)
    if row_idx is None:
        return new_lines

    parsed = _parse_new_row(lines[row_idx])
    if parsed is None:
        return new_lines  # unparseable — leave untouched

    _idx, _name, _prompt, _status, started, _completed, notes = parsed
    now_str = new_completed or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new_lines[row_idx] = _make_phase_row(phase_idx, phase_name, new_status, started, now_str, notes)
    return new_lines


# ── Public API ───────────────────────────────────────────────────────────────


def init_workflow(project_path: str) -> None:
    """
    Create .crabcakes/workflow.md with all phases. Onboarding is current;
    all others are pending. Safe to call on an already-initialized project.
    """
    workflow_path = os.path.join(get_crabcakes_dir(project_path), "workflow.md")
    if os.path.isfile(workflow_path):
        return
    os.makedirs(os.path.dirname(workflow_path), exist_ok=True)
    with open(workflow_path, "w", encoding="utf-8") as f:
        f.write(_initial_workflow_content())


def get_current_phase(project_path: str) -> str:
    """
    Return the name of the current (in-progress) phase.
    Returns PHASES[0] ("onboarding") if workflow.md doesn't exist.
    """
    lines = _read_workflow_lines(project_path)
    if not lines:
        return PHASES[0]

    for line in lines:
        if re.search(r"🔄\s*current", line):
            m = re.match(rf"\|\s*\d+\s*\|\s*({_PHASE_NAME_RE})\s*\|", line)
            if m:
                return m.group(1)

    # Fallback: first non-done phase
    for name in PHASES:
        if not is_phase_done(project_path, name):
            return name
    return PHASES[-1]


def is_phase_done(project_path: str, phase_name: str) -> bool:
    """
    Return True if the named phase is marked ✅ done in workflow.md.
    Returns False if not done or if workflow.md doesn't exist.
    """
    lines = _read_workflow_lines(project_path)
    if not lines:
        return False

    phase_idx = _PHASE_INDEX.get(phase_name, -1)
    for line in lines:
        m = re.match(rf"\|\s*(\d+)\s*\|\s*({_PHASE_NAME_RE})\s*\|", line)
        if m and int(m.group(1)) == phase_idx and m.group(2) == phase_name:
            if re.search(r"✅\s*done", line):
                return True
    return False


def advance_phase(project_path: str, phase_name: str) -> None:
    """
    Mark the named phase as ✅ done and set the next phase to 🔄 current.

    Initializes workflow.md if it doesn't exist.
    Raises ValueError if phase_name is not a valid phase.
    """
    if phase_name not in _PHASE_INDEX:
        raise ValueError(f"Unknown phase: {phase_name!r}. Valid: {PHASES}")

    lines = _read_workflow_lines(project_path)
    if lines is None:
        init_workflow(project_path)
        lines = _read_workflow_lines(project_path)
    if lines is None:
        return

    phase_idx = _PHASE_INDEX[phase_name]
    next_idx = phase_idx + 1
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Mark current phase done and set next to current
    lines = _replace_phase_row(lines, phase_idx, phase_name, "✅ done", now)
    if next_idx < len(PHASES):
        next_name = PHASES[next_idx]
        lines = _replace_phase_row(lines, next_idx, next_name, "🔄 current", "—")

    _write_workflow_lines(project_path, lines)

    # SOR §2.9: on onboarding completion, clean comment-only manifest sections.
    # Lazy import to keep workflow_state's module-level imports stable.
    # Non-fatal: a cleanup failure must not undo the workflow transition.
    if phase_name == "onboarding":
        try:
            from utils.project_awareness import clean_manifest_skeleton
            clean_manifest_skeleton(project_path)
        except Exception:
            logging.getLogger(__name__).debug(
                "clean_manifest_skeleton failed for %s; workflow transition unaffected",
                project_path,
                exc_info=True,
            )


def get_workflow_content(project_path: str) -> str:
    """
    Return the raw content of .crabcakes/workflow.md.
    Returns empty string if the file doesn't exist.
    """
    path = os.path.join(get_crabcakes_dir(project_path), "workflow.md")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""
