# models/activity.py
# ActivityBubble dataclass — Phase 2 of SPEC-smarter-chat-ux.
#
# Pure data container. No GTK, no gateway calls, no state.
# AgentRuntimeHandler (ui/agent_runtime/turn_ui.py) emits these on tool
# start/end/error and patch events to whatever callback is registered
# via set_on_activity_bubble (a dormant seam since SPEC-58 §2e removed
# the drawer; tests pin the emission and batching).

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

# Supported activity types — exhaustive list from gateway event catalog
ActivityType = Literal[
    "lifecycle_start",   # agent started
    "tool_start",        # tool call began
    "tool_end",          # tool call completed
    "tool_error",        # tool call failed
    "plan",              # plan update from update_plan
    "approval_request",  # exec approval needed
    "command_output",    # shell command finished
    "patch",             # file edit summary
]


class ToolStatus(Enum):
    """Status of a tool invocation — used for icon selection."""
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


@dataclass
class ActivityBubble:
    """
    Structured data for an activity event.

    Emitted by AgentRuntimeHandler (ui/agent_runtime/turn_ui.py) on tool
    start/end/error and patch events; delivered to the callback registered
    via set_on_activity_bubble.

    Attributes:
        type:       Which activity type this bubble represents
        session_key: Which session/tab this belongs to
        tool_name:  For tool_* types — which tool (e.g. "web_search")
        duration_ms: For tool_end / command_output — elapsed time
        status:     For tool_* — RUNNING / SUCCESS / ERROR
        icon:       Emoji icon prefix (e.g. "🔧", "✅", "❌")
        title:      For plan type — plan title
        steps:      For plan type — list of step descriptions
        command:    For approval_request — the command needing approval
        approval_id: For approval_request — gateway approval ID
        reason:     For approval_request — why approval is needed
        exit_code:  For command_output — shell exit code
        added:      For patch — number of files added
        modified:   For patch — number of files modified
        deleted:    For patch — number of files deleted
        raw_text:   Human-readable one-line description for the bubble text
    """

    type: ActivityType
    session_key: str
    tool_name: str = ""
    duration_ms: int = 0
    status: ToolStatus = ToolStatus.RUNNING
    icon: str = "▶"
    title: str = ""
    steps: list[str] = field(default_factory=list)
    command: str = ""
    approval_id: str = ""
    reason: str = ""
    exit_code: int = 0
    added: int = 0
    modified: int = 0
    deleted: int = 0
    raw_text: str = ""
    # SPEC-activity-drawer Phase 1: enrichment fields for the drawer view.
    # Defaulted to "" so existing call sites that don't supply them still work.
    agent_name: str = ""      # Display name of the agent that emitted the event (e.g. "Coder")
    file_path: str = ""       # Relative file path for read/write/edit events
    output: str = ""          # Last N lines of stdout/stderr for command_output click-to-expand

    def format_text(self) -> str:
        """
        Build the human-readable one-line text for this activity bubble.

        Called by ChatHandler when rendering. All formatting lives here —
        keeping ActivityBubble focused on data, ChatHandler focused on GTK.

        No emoji icons — uses subtle unicode and clean formatting.
        """
        if self.type == "lifecycle_start":
            return "thinking..."
        elif self.type == "tool_start":
            name = _friendly_tool_name(self.tool_name)
            return f"{name}"
        elif self.type == "tool_end":
            name = _friendly_tool_name(self.tool_name)
            ms = self.duration_ms
            if ms > 0:
                return f"{name}  {ms:,}ms"
            return f"{name}  done"
        elif self.type == "tool_error":
            name = _friendly_tool_name(self.tool_name)
            return f"{name}  failed"
        elif self.type == "plan":
            text = f"plan: {self.title}"
            if self.steps:
                text += f"  {len(self.steps)} steps"
            return text
        elif self.type == "approval_request":
            cmd = self.command
            if len(cmd) > 60:
                cmd = cmd[:57] + "..."
            return f"approve: {cmd}"
        elif self.type == "command_output":
            name = _friendly_tool_name(self.tool_name)
            ms = self.duration_ms
            if self.exit_code != 0:
                return f"{name}  exit {self.exit_code}  {ms:,}ms"
            return f"{name}  {ms:,}ms"
        elif self.type == "patch":
            parts = []
            if self.added:
                parts.append(f"+{self.added}")
            if self.modified:
                parts.append(f"~{self.modified}")
            if self.deleted:
                parts.append(f"-{self.deleted}")
            label = " ".join(parts) if parts else "no changes"
            return f"{self.tool_name}  {label}"
        else:
            return self.raw_text or self.type


def _friendly_tool_name(name: str) -> str:
    """Convert tool_name to a cleaner display name."""
    # Map of known tool names to friendly labels
    _names = {
        "web_search": "search",
        "web_fetch": "fetch",
        "exec": "exec",
        "read": "read",
        "write": "write",
        "edit": "edit",
        "image": "image",
        "image_generate": "generate image",
        "music_generate": "generate music",
        "video_generate": "generate video",
        "memory_search": "memory",
        "memory_get": "recall",
        "cron": "schedule",
        "sessions_spawn": "spawn",
        "sessions_send": "message",
        "sessions_list": "sessions",
        "update_plan": "plan",
        "session_status": "status",
    }
    return _names.get(name, name.replace("_", " "))
