# models package
# Pure data layer — no GTK, no network, no file I/O.
#
# Architecture rule: models/ must NEVER import from ui/, gateway/, or agent/.
# All imports are stdlib only (dataclasses, datetime, enum, typing).

from .agents import AgentManager
from .colors import (
    next_agent_color,
    reset_color_indices,
    # NOTE: `color_for_special_agent` is intentionally NOT re-exported here.
    # It is an internal lookup keyed by special-agent role, called only via
    # deferred import from `ui/handlers/agent_list_handler.py`. External
    # callers (other handlers, tests) should use `next_agent_color()` for
    # round-robin assignment instead. See ARCHITECTURE.md §3.18.
)
from .command import Command, CommandResult, CommandRegistry
from .routing import AgentRoutingTable
from .streaming import StreamingBubble
from .feed_card import FeedCardData
from .activity import ActivityBubble, ToolStatus
from .conversation import Conversation, Message, MessageRole, ToolCall, ToolCallStatus
from .conversation_snapshot import ConversationSnapshot, SnapshotMessage
from .team import TeamMember, ProjectTeam

__all__ = [
    # agents
    "AgentManager",
    # routing
    "AgentRoutingTable",
    # command
    "Command",
    "CommandResult",
    "CommandRegistry",
    # feed
    "FeedCardData",
    # streaming
    "StreamingBubble",
    # activity (Phase 2 — SPEC-smarter-chat-ux)
    "ActivityBubble",
    "ToolStatus",
    # conversation
    "Conversation",
    "Message",
    "MessageRole",
    "ToolCall",
    "ToolCallStatus",
    # conversation_snapshot
    "ConversationSnapshot",
    "SnapshotMessage",
    # team
    "TeamMember",
    "ProjectTeam",
    # colors
    "next_agent_color",
    "reset_color_indices",
]
