# utils/feedback_processor.py
# Audit report processing — file I/O and role resolution.
#
# All file I/O for structured audit report processing lives here.
# The handler only provides runtime context; this module does all writes.
#
# Architecture: pure utility — no GTK, no network.
#
# Spec reference: SPEC-3 §3.3

from __future__ import annotations

import dataclasses
import datetime
import fcntl
import logging
import os
import re
from typing import Any

from utils.audit_parser import AuditReport
from utils.review_log import append_review_entry

logger = logging.getLogger(__name__)

# LOW-12: stable session identifier
_SESSION_ID: str | None = None


def get_session_id() -> str:
    """LOW-12: Return a stable session identifier (cached uuid4 hex)."""
    global _SESSION_ID
    if _SESSION_ID is None:
        import uuid as _uuid
        _SESSION_ID = _uuid.uuid4().hex[:16]
    return _SESSION_ID


# ── Agent self-improvement config ─────────────────────────────────────────────


def get_target_si_config(target_role: str) -> dict:
    """Return the self_improvement config for the target agent role.

    Loads agent definitions, finds the matching role, merges its
    self_improvement config with the defaults. Returns defaults if
    the role is not found or defs can't be loaded.

    Args:
        target_role: Role identifier (e.g. "coder", "debugger").
    """
    try:
        from utils.agent_defs import get_default_si_config, load_agent_defs

        defs = load_agent_defs()
        for d in defs:
            role = d.get("role", d.get("name", "").lower().replace(" ", "-"))
            if role == target_role:
                can_write = "write_file" in d.get("tools", [])
                defaults = get_default_si_config(can_write=can_write)
                si = d.get("self_improvement", {})
                return {**defaults, **si}
    except Exception:
        pass

    # Fallback — use defaults for a non-writing agent
    try:
        from utils.agent_defs import get_default_si_config

        return get_default_si_config(can_write=False)
    except Exception:
        return {"structured_feedback": False, "bug_journal": True}


# ── Role resolution ────────────────────────────────────────────────────────────


def resolve_role_from_session(
    session_key: str, runtime_handler: Any = None
) -> str:
    """Resolve an agent role from a session key.

    For local agents (session_key starts with "agent:"), looks up
    the SpecialAgentDef via the runtime handler. For gateway agents,
    returns 'unknown'.

    Args:
        session_key: Agent session key.
        runtime_handler: AgentRuntimeHandler instance (optional).
    """
    if session_key.startswith("agent:"):
        if runtime_handler is not None:
            sad = runtime_handler.get_special_agent_def(session_key)
            if sad is not None and hasattr(sad, "role"):
                return sad.role
    return "unknown"


def resolve_default_target_role() -> str:
    """Return the single writing agent's role, or 'unknown'.

    Looks up all agent definitions. If exactly one writing agent exists,
    returns its role. Otherwise returns 'unknown' to avoid mis-filing
    audit reports.
    """
    try:
        from utils.agent_defs import load_agent_defs

        all_defs = load_agent_defs()
        writers = [
            d for d in all_defs if "write_file" in d.get("tools", [])
        ]
        if len(writers) == 1:
            return writers[0].get("role", "unknown")
    except Exception:
        pass
    return "unknown"


# ── Bug journal ───────────────────────────────────────────────────────────────


def append_to_bug_journal(
    project_path: str, report: AuditReport, target_role: str
) -> None:
    """Append an audit report as a new entry in the target agent's bug journal.

    Uses an exclusive file lock to prevent duplicate bug numbers when
    multiple threads or processes write concurrently.

    Args:
        project_path: Absolute path to the project root.
        report: Parsed AuditReport to append.
        target_role: Role identifier (determines {role}-bugs.md filename).
    """
    journal_path = os.path.join(
        project_path, ".crabcakes", f"{target_role}-bugs.md"
    )

    os.makedirs(os.path.dirname(journal_path), exist_ok=True)

    # Exclusive lock — held from read-through-write to prevent duplicate numbers
    with open(journal_path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            # Re-read after acquiring lock to get latest state
            if os.path.isfile(journal_path) and os.path.getsize(journal_path) > 0:
                with open(journal_path, "r", encoding="utf-8") as rf:
                    existing = rf.read()
                nums = re.findall(r"## Bug #(\d+)", existing)
                next_num = max(int(n) for n in nums) + 1 if nums else 1
            else:
                next_num = 1

            # MED-7 FIX: Apply sanitization to user-controlled field values,
            # NOT to the generated heading. The heading '## Bug #N' is always
            # safe (generated by code), so only strip headings that DON'T match
            # the expected generated pattern.
            def _sanitize_field(text: str) -> str:
                """Strip fence breaks and instruction-override patterns from a text field."""
                lines = []
                for line in text.split("\n"):
                    if "```" in line:
                        line = line.replace("```", "")
                    if re.search(r"(?i)(ignore|disregard|forget)\s+(previous|prior|above|all)", line):
                        continue
                    if re.search(r"(?i)new instructions:", line):
                        continue
                    lines.append(line)
                return "\n".join(lines)

            sanitized_report = dataclasses.replace(
                report,
                task=_sanitize_field(report.task),
                bug_description=_sanitize_field(report.bug_description),
                expected=_sanitize_field(report.expected),
                actual=_sanitize_field(report.actual),
                pattern=_sanitize_field(report.pattern) if report.pattern else None,
            )

            today = datetime.date.today().isoformat()
            entry_text = sanitized_report.to_bug_journal_entry(next_num, today)

            # MED-7: Sanitize the generated entry — but preserve '## Bug #N' headings.
            # Strip any line that starts with a markdown heading marker ('#' at column 0),
            # except the safe generated '## Bug #N' pattern. Also strip field-value lines
            # (bold-key lines) where the VALUE portion starts with '#' to prevent
            # field-value injection (e.g. '**Task:** # Injected' → renders as heading).
            _heading_pattern = re.compile(r"^## Bug #\d+")
            _field_heading_pattern = re.compile(r"^\*\*(Task|Mistake|Expected|Actual|Fix|Lesson|Pattern):\*\*\s+#")
            sanitized_lines = []
            for line in entry_text.split("\n"):
                stripped = line.strip()
                if stripped.startswith("#"):
                    if _heading_pattern.match(stripped):
                        sanitized_lines.append(line)
                    continue
                if _field_heading_pattern.match(stripped):
                    # Field value starts with '#' — strip the '#' prefix from the value
                    line = _field_heading_pattern.sub(r"**\1:**", line)
                if "```" in line:
                    line = line.replace("```", "")
                if re.search(r"(?i)(ignore|disregard|forget)\s+(previous|prior|above|all)", line):
                    continue
                if re.search(r"(?i)new instructions:", line):
                    continue
                sanitized_lines.append(line)
            entry_text = "\n".join(sanitized_lines)
            # LOW-12: append session_id for traceability
            entry_text += f"\n**Session:** {get_session_id()}\n"
            f.seek(0, 2)  # paranoia — stay at end before writing
            if os.path.getsize(journal_path) > 0:
                f.write("\n")
            f.write(entry_text + "\n")
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    logger.info("[feedback] Appended Bug #%d to %s", next_num, journal_path)


# ── Main entry point ───────────────────────────────────────────────────────────


def process_audit_reports(
    project_path: str,
    reports: list[AuditReport],
    reviewer: str,
    target_role: str,
) -> None:
    """Process audit reports: log to review-log.jsonl and optionally append to bug journal.

    For each report:
    1. Always log to .crabcakes/review-log.jsonl (with target_role).
    2. Auto-append to .crabcakes/{role}-bugs.md if severity='bug' AND
       the target agent has structured_feedback enabled.

    Args:
        project_path: Absolute path to the project root.
        reports: List of AuditReport objects from extract_audit_reports().
            May be empty — passes silently.
        reviewer: Display name of the reviewing agent.
        target_role: Role identifier of the agent being reviewed.
    """
    if not reports:
        return

    # Check target agent's structured_feedback flag
    si_config = get_target_si_config(target_role)
    structured_feedback_enabled = si_config.get("structured_feedback", False)

    for report in reports:
        # 1. Always log to review-log.jsonl
        try:
            entry = report.to_review_log_entry(
                reviewer, project_path, target_role=target_role
            )
            append_review_entry(project_path, entry)
            logger.info(
                "[feedback] Logged audit report: %s %s (%s) target=%s",
                report.severity,
                report.file_path,
                report.pattern or "no-pattern",
                target_role,
            )
        except Exception as e:
            logger.warning("[feedback] Failed to log audit report: %s", e)

        # 2. Append to bug journal only for bug-severity with structured_feedback on
        if report.is_bug and structured_feedback_enabled:
            try:
                append_to_bug_journal(project_path, report, target_role)
            except Exception as e:
                logger.warning(
                    "[feedback] Failed to append to bug journal: %s", e
                )