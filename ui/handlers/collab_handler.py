# ui/handlers/collab_handler.py
# Collaboration command implementations — extracted from window.py Phase 7.
#
# Manifest:
#   reads:   models.command (Command, CommandResult)
#   writes:  nothing
#   network: nothing
#   GTK:     nothing
#
# Owns: 2 collaboration commands — ask, stop
# Does NOT own: GTK widgets, gateway client, agent manager
#
# Architecture: pure Python. No imports from ui/, gateway/, or agent/.


from models.command import Command, CommandResult


class CollabHandler:
    """
    Handles collaboration command execution.

    Both commands (ask, stop) are pure pass-through
    returning CommandResult with routing decisions. The actual routing
    (sending messages to agents) is done by ChatHandler after it receives
    the CommandResult.

    Args:
        None — no dependencies on UI or network state.
    """

    def __init__(self):
        pass

    # ── Collaboration commands ──────────────────────────────────────────────────

    def cmd_ask(self, cmd: Command) -> CommandResult:
        """/ask @agent — question → forward question to agent (or all if `@`)"""
        if cmd.is_broadcast:
            return CommandResult(handled=True, broadcast_targets=cmd.broadcast_targets, forward_text=cmd.body)
        if not cmd.target_session_key:
            return CommandResult(handled=True, response_text="No target agent. Usage: /ask @agent — question")
        return CommandResult(handled=True, forward_to=cmd.target_session_key, forward_text=cmd.body)

    def cmd_stop(self, cmd: Command) -> CommandResult:
        """/stop @agent → send stop signal to agent."""
        if not cmd.target_session_key:
            return CommandResult(handled=True, response_text="No target agent. Usage: /stop @agent")
        return CommandResult(handled=True, forward_to=cmd.target_session_key, forward_text="stop")
