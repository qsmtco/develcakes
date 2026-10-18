# models/feed_card.py
# Feed card data models — pure Python, no GTK, no git, no network.
#
# Architecture: foundation that ui/ depends on — not the other way around.
# Zero imports from ui/, gateway/, agent/, subprocess.
#
# Files that import these:
#   - ui/handlers/feed_handler.py (FeedHandler owns card lifecycle)
#   - ui/handlers/chat_render_handler.py (crabcard extraction)
#   - ui/views/feed_card.py (factory — reads FeedCardData to build widgets)
#   - utils/crabcard_parser.py (parses raw text → FeedCardData)
#   - tests/ (unit tests)

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from models.conversation_snapshot import ConversationSnapshot

_logger = logging.getLogger(__name__)

# Storage cap for a card body (SPEC-UI-RESPONSIVENESS-2-T2 §1).
#
# STORAGE vs RENDER are separate concerns: this bound keeps feed.json finite
# (a 200k-char body per card, worst case), while the UI renders at most
# `ui.views.feed_card.RENDERED_BODY_LIMIT` (2,000) characters per card with a
# reveal control. Before T2 the only cap was 2,000 chars applied *before
# storage* on tool results, so the stored text — copy, crabcard export, audits —
# was silently lossy. Measured live (Qrusher 2026-09-12): 4 approval cards at
# 16–39 KB bodies dominated the main thread's pango measurement (60.1% of burn).
MAX_STORED_BODY = 200_000

# Supported card types — exhaustive list for Phase 1
CardType = Literal[
    "git_commit",
    "diff",
    "file_created",
    "file_modified",
    "file_deleted",
    "dir_created",
    "dir_deleted",
    "agent_action",
    "task",
    "system",
    "audit_report",
]

# Supported sources
CardSource = Literal["agent", "system", "git", "crabwatch"]


def format_feed_timestamp(ts) -> str:
    """Relative age for a feed card footer.

    None or under 60s → "just now"; under an hour → "Nm ago"; under a day
    → "Nh ago"; otherwise "Nd ago". A naive datetime is treated as UTC.
    """
    if ts is None:
        return "just now"
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    total_seconds = (now - ts).total_seconds()
    if total_seconds < 60:
        return "just now"
    if total_seconds < 3600:
        return f"{int(total_seconds / 60)}m ago"
    if total_seconds < 86400:
        return f"{int(total_seconds / 3600)}h ago"
    return f"{int(total_seconds / 86400)}d ago"


def cap_stored_body(text: str, site: str = "") -> str:
    """Bound a card body for storage at `MAX_STORED_BODY` (SPEC-UIRESP2-T2 §1).

    The single storage-side cap: every `body=` producer funnels through
    `FeedCardData.__post_init__` (or calls this directly when mutating an
    existing card), so a new producer cannot silently store an unbounded body.
    Logs a WARNING when it fires — a capped body means the stored copy of that
    card is incomplete, which the operator should know about.

    Non-str input is returned unchanged: the model does not coerce types, and
    `body` is typed `str` (Rule 6 — validation belongs at the edge, and every
    call site passes a string).
    """
    if not isinstance(text, str) or len(text) <= MAX_STORED_BODY:
        return text
    _logger.warning(
        "feed card body truncated for storage at MAX_STORED_BODY=%d "
        "(%d chars%s); feed.json is bounded, the full text is not retained",
        MAX_STORED_BODY, len(text), f", site={site}" if site else "",
    )
    return text[:MAX_STORED_BODY]


@dataclass
class FeedCardData:
    """Structured data for a project feed card.

    No GTK, no git calls, no network. Pure data container.
    """
    card_type: CardType
    source: CardSource
    title: str
    body: str
    author: str
    timestamp: datetime
    project_name: str

    # Optional context fields
    file_path: str | None = None
    commit_sha: str | None = None
    additions: int | None = None
    deletions: int | None = None
    task_id: str | None = None
    metadata: dict = field(default_factory=dict)

    # Conversation snapshot (set by FeedHandler at card creation time)
    conversation_snapshot: ConversationSnapshot | None = None

    # Runtime fields (set by FeedHandler, not by source)
    card_id: str | None = None
    reviewed: bool = False
    accepted: bool | None = None  # True=accepted, False=rejected, None=pending
    seq_num: int | None = None  # Sequential display number (per project)

    def __post_init__(self) -> None:
        """Apply the storage-side body cap at construction (UIRESP2-T2 §1).

        Every `FeedCardData` producer (agent runtime tool results, approval
        prompts, git-commit stdout, audit reports, crabcard parsing, crabwatch
        events, work units — plus `from_dict` on load) passes through here, so
        the cap cannot be bypassed by a new `body=` site. The RENDER cap is a
        separate, much smaller limit in `ui.views.feed_card`.
        """
        self.body = cap_stored_body(self.body, site=self.card_type)

    @staticmethod
    def css_class_for_type(card_type: CardType) -> str:
        """Return CSS class name for a card type."""
        mapping: dict[CardType, str] = {
            "git_commit": "feed-card-git",
            "diff": "feed-card-diff",
            "file_created": "feed-card-file-new",
            "file_modified": "feed-card-file-mod",
            "file_deleted": "feed-card-file-del",
            "dir_created": "feed-card-dir-new",
            "dir_deleted": "feed-card-dir-del",
            "agent_action": "feed-card-agent",
            "task": "feed-card-task",
            "system": "feed-card-system",
            "audit_report": "feed-card-audit",
        }
        return mapping.get(card_type, "feed-card-system")

    @staticmethod
    def is_actionable(card_type: CardType, metadata: dict | None = None) -> bool:
        """True if this card type requires user action (Accept/Reject/Approve/Deny)."""
        # Approval-request cards (exec_command needing approval)
        if metadata and metadata.get("needs_approval"):
            return True
        # File-change cards that have git backing (can be accepted/rejected)
        if card_type in ("diff", "file_created", "file_modified", "file_deleted"):
            return True
        return False

    @staticmethod
    def is_informational(card_type: CardType, metadata: dict | None = None) -> bool:
        """True if this card is purely informational — no buttons needed."""
        # Approval cards are actionable, not informational
        if metadata and metadata.get("needs_approval"):
            return False
        # git_commit: result of accept/reject — informational
        if card_type == "git_commit":
            return True
        # agent_action with status=running/complete/error: tool execution log.
        # Also include status=None: a crabcard-parsed or manually-created
        # agent_action card with no status field is still informational (a
        # tool execution log), not actionable. Without this, the card would
        # show Accept/Reject buttons that do nothing.
        if card_type == "agent_action":
            status = metadata.get("status") if metadata else None
            if status in (None, "running", "complete", "error"):
                return True
        # system events, audit reports, tasks: informational
        if card_type in ("system", "audit_report", "task", "dir_created", "dir_deleted"):
            return True
        return False

    # ── Serialization (for feed.json persistence) ───────────────────────

    def _serialize_metadata_with_snapshot(self) -> dict:
        """Serialize metadata dict, injecting snapshot if present."""
        meta = dict(self.metadata) if self.metadata else {}
        if self.conversation_snapshot is not None:
            meta["snapshot"] = self.conversation_snapshot.to_dict()
        else:
            meta.pop("snapshot", None)
        return meta

    def to_dict(self) -> dict:
        """
        Serialize to a JSON-compatible dict. Includes all fields.
        Runtime fields (card_id, reviewed, accepted) are included so that
        FeedHandler can persist state changes (accept/reject) back to feed.json.
        """
        return {
            "card_type": self.card_type,
            "source": self.source,
            "title": self.title,
            "body": self.body,
            "author": self.author,
            "timestamp": self.timestamp.isoformat(),
            "project_name": self.project_name,
            "file_path": self.file_path,
            "commit_sha": self.commit_sha,
            "additions": self.additions,
            "deletions": self.deletions,
            "task_id": self.task_id,
            "metadata": self._serialize_metadata_with_snapshot(),
            "card_id": self.card_id,
            "reviewed": self.reviewed,
            "accepted": self.accepted,
            "seq_num": self.seq_num,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FeedCardData":
        """
        Deserialize from a dict (e.g., loaded from feed.json).
        Handles both legacy dicts (no card_id/running fields) and current format.
        """
        ts = data.get("timestamp")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        elif not isinstance(ts, datetime):
            ts = datetime.now(timezone.utc)
        # Deserialize snapshot from metadata if present
        metadata = data.get("metadata", {})
        snapshot = None
        if metadata and "snapshot" in metadata:
            from models.conversation_snapshot import ConversationSnapshot
            snapshot = ConversationSnapshot.from_dict(metadata["snapshot"])

        return cls(
            card_type=data["card_type"],
            source=data["source"],
            title=data["title"],
            body=data.get("body", ""),
            author=data.get("author", "unknown"),
            timestamp=ts,
            project_name=data["project_name"],
            file_path=data.get("file_path"),
            commit_sha=data.get("commit_sha"),
            additions=data.get("additions"),
            deletions=data.get("deletions"),
            task_id=data.get("task_id"),
            metadata=metadata,
            card_id=data.get("card_id"),
            reviewed=data.get("reviewed", False),
            accepted=data.get("accepted"),
            seq_num=data.get("seq_num"),
            conversation_snapshot=snapshot,
        )


@dataclass
class FileChangePref:
    """Per-type auto-accept preference."""
    enabled: bool = False
    agent_scope: str = "first_author"  # "first_author" | "all_agents" | "<agent_name>"


@dataclass
class ExecCommandPref:
    """Exec command auto-accept preference."""
    mode: str = "off"  # "off" | "show" | "silent"
    agent_scope: str = "first_author"


@dataclass
class AutoAcceptPrefs:
    """V2 auto-accept preferences replacing the single toggle.

    Serialized to feed-prefs.json as version 2. The FeedHandler owns
    the canonical instance; FeedTab receives a copy via
    update_auto_accept_prefs().
    """
    file_changes: dict[str, FileChangePref] = field(default_factory=lambda: {
        "diff": FileChangePref(),
        "file_created": FileChangePref(),
        "file_modified": FileChangePref(),
        "file_deleted": FileChangePref(),
    })
    exec_command: ExecCommandPref = field(default_factory=ExecCommandPref)
    snoozed_card_ids: list[str] = field(default_factory=list)

    def any_enabled(self) -> bool:
        """True if any file-change type is enabled OR exec is not off."""
        return (
            any(fc.enabled for fc in self.file_changes.values())
            or self.exec_command.mode != "off"
        )

    def is_file_type_enabled(self, card_type: str) -> bool:
        """Check if a specific card type is auto-accept enabled."""
        pref = self.file_changes.get(card_type)
        return pref is not None and pref.enabled

    def to_dict(self) -> dict:
        """Serialize for feed-prefs.json persistence."""
        return {
            "version": 2,
            "auto_accept": {
                "file_changes": {
                    ct: {"enabled": fc.enabled, "agent_scope": fc.agent_scope}
                    for ct, fc in self.file_changes.items()
                },
                "exec_command": {
                    "mode": self.exec_command.mode,
                    "agent_scope": self.exec_command.agent_scope,
                },
                "snoozed_card_ids": list(self.snoozed_card_ids),
            },
        }

    @staticmethod
    def from_dict(raw: dict) -> "AutoAcceptPrefs":
        """Deserialize from feed-prefs.json. Tolerates missing keys."""
        prefs = AutoAcceptPrefs()
        auto = raw.get("auto_accept", {})
        fc_raw = auto.get("file_changes", {})
        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            fc = fc_raw.get(ct, {})
            prefs.file_changes[ct] = FileChangePref(
                enabled=bool(fc.get("enabled", False)),
                agent_scope=str(fc.get("agent_scope", "first_author")),
            )
        exec_raw = auto.get("exec_command", {})
        prefs.exec_command = ExecCommandPref(
            mode=str(exec_raw.get("mode", "off")),
            agent_scope=str(exec_raw.get("agent_scope", "first_author")),
        )
        snoozed = auto.get("snoozed_card_ids", [])
        prefs.snoozed_card_ids = list(snoozed) if isinstance(snoozed, list) else []
        return prefs

    def locked_agent(self) -> str | None:
        """Return the locked-in agent if any file_changes type uses a
        specific agent name as its scope (not 'all_agents' or
        'first_author'). Used during v1→v2 migration to preserve the
        persisted agent lock-in from the v1 'auto_accept_agent' field.
        """
        for fc in self.file_changes.values():
            if fc.agent_scope not in ("all_agents", "first_author"):
                return fc.agent_scope
        return None
