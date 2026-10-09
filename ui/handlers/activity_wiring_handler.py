# ui/handlers/activity_wiring_handler.py
"""
Owns ActivityDrawer event wiring for local agent turns.

Per ARCHITECTURE.md §8.6 this is a handler (not window.py logic). Per §3.21y
the wiring must NOT live in connection_sync_handler.sync() (post-connect only).
This handler is constructed in window.py._build() and .wire() is called
unconditionally at startup, so the drawer receives events from the first
local-agent tool call onward — no gateway required.

=== Dedup Invariant ===
Local agents route through AgentRuntimeHandler. The drawer receives command
output, tool bubbles, and start/end separators from that handler.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ui.handlers.activity_handler import ActivityHandler
    from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
    from ui.views.activity_drawer import ActivityDrawer


class ActivityWiringHandler:
    """Single owner of activity-source → drawer routing.

    Wires three sources:
    1. Gateway ActivityHandler bubbles/lifecycle → drawer (online path).
    2. Local AgentRuntimeHandler exec_command output → drawer (offline path).
    3. Local AgentRuntimeHandler tool lifecycle + agent start/end → drawer
       (NEW — tool_start/tool_end/tool_error/patch bubbles).

    Call .wire() once at startup (unconditionally). Idempotent — safe to
    call multiple times.
    """

    def __init__(
        self,
        *,
        activity_handler: ActivityHandler,
        agent_runtime_handler: AgentRuntimeHandler,
        activity_drawer: ActivityDrawer,
    ) -> None:
        self._activity_handler = activity_handler
        self._agent_runtime_handler = agent_runtime_handler
        self._drawer = activity_drawer
        self._wired = False

    def wire(self) -> None:
        """Wire all activity sources → drawer. Idempotent — safe to call twice."""
        if self._wired:
            return
        self._wired = True

        # Local exec_command output → drawer
        self._agent_runtime_handler.set_on_command_output(self._on_local_command_output)
        # 4. NEW: local tool lifecycle → drawer
        self._agent_runtime_handler.set_on_activity_bubble(self._on_local_activity_bubble)
        # 5. NEW: local agent start/end → drawer separators
        self._agent_runtime_handler.set_on_drawer_lifecycle(self._on_local_drawer_lifecycle)

    # ── Local path adapters ─────────────────────────────────────────────

    def _resolve_local_agent_name(self, session_key: str) -> str:
        """Resolve agent name for a LOCAL session key. Works offline (no AgentManager).

        Uses AgentRuntimeHandler.get_agent_name_for_session — the local registry.
        """
        if self._agent_runtime_handler is None:
            return "Agent"
        return self._agent_runtime_handler.get_agent_name_for_session(session_key) or "Agent"

    def _on_local_command_output(self, sk, command, output, exit_code, duration_ms) -> None:
        """Local exec_command result → drawer row (preserved from sync())."""
        if self._drawer is None:
            return
        from models.activity import ActivityBubble, ToolStatus
        agent_name = self._resolve_local_agent_name(sk)
        is_error = exit_code != 0
        bubble = ActivityBubble(
            type="command_output",
            session_key=sk,
            tool_name=command,
            command=command,
            output=output,
            exit_code=exit_code,
            duration_ms=duration_ms,
            icon="💻",
            status=ToolStatus.ERROR if is_error else ToolStatus.SUCCESS,
            agent_name=agent_name,
        )
        self._drawer.append_event(bubble.to_drawer_row())

    def _on_local_activity_bubble(self, bubble) -> None:
        """Local tool/patch bubble → drawer row."""
        if self._drawer is None:
            return
        # Enrich agent_name if the runtime didn't set it.
        if not bubble.agent_name:
            bubble.agent_name = self._resolve_local_agent_name(bubble.session_key)
        self._drawer.append_event(bubble.to_drawer_row())

    def _on_local_drawer_lifecycle(self, session_key: str, agent_name: str, phase: str) -> None:
        """Local agent start/end → drawer separator (mirrors gateway _on_agent_lifecycle)."""
        if self._drawer is None:
            return
        if phase == "start":
            self._drawer.on_agent_start(session_key, agent_name)
        elif phase == "end":
            self._drawer.on_agent_end(session_key, agent_name)
