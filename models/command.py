# models/command.py
# Command system data models — pure Python, no GTK, no network.
#
# Manifest: reads nothing, writes nothing, no network
# Architecture: these are the foundation that ui/ depends on — not the other way around.
# Zero imports from ui/ or gateway/.
#
# Files that import these:
#   - ui/handlers/command_handler.py (CommandHandler owns the registry)
#   - ui/window.py (wires CommandHandler during setup)
#   - tests/test_command_models.py (unit tests)


from dataclasses import dataclass, field
from typing import Callable


# ── Data models ────────────────────────────────────────────────────────────────


@dataclass
class MentionResolution:
    """Result of resolving @mentions from arbitrary text.

    Returned by CommandHandler.resolve_inline_mention().
    Used by both the backtick command path and the plain-text send path.

    Fields:
        target_session_key:  Single agent session key, or None.
        broadcast_targets:   All project member session keys (when @ alone = broadcast).
        clean_text:          Original text with @mention tokens removed.
        error:               Error message if resolution failed, or None.
        is_broadcast:        True when bare @ resolved to all project members.
    """
    target_session_key: str | None = None
    broadcast_targets: list[str] = field(default_factory=list)
    clean_text: str = ""
    error: str | None = None
    is_broadcast: bool = False


@dataclass
class Command:
    """Parsed command after stripping the prefix and extracting parts.

    Fields:
        name:               Command name, lowercased (e.g. "ask", "task", "status")
        args:               Positional arguments (e.g. ["@debugger"] for `ask @debugger)
        flags:              --flag value pairs, e.g. {"verbose": ""} for --verbose
        raw_text:           Original input string after the prefix (e.g. "ask @debugger — hello")
        source_session_key: Session key of the sender (agent or PM)
        target_session_key: Resolved target agent session key, or None for broadcast
    """
    name: str
    args: list[str] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)
    raw_text: str = ""
    body: str = ""          # quoted payload, e.g. "what is the status?" in `/ask @Debugger "what is the status?"`
    source_session_key: str = ""
    target_session_key: str | None = None   # single target
    is_broadcast: bool = False              # True when @ mentions all project members
    broadcast_targets: list[str] = field(default_factory=list)   # BUG #4 fix: all fan-out targets for @ broadcast
    user: str = ""                        # LOW-1: human-readable user identity for traceability


@dataclass
class CommandResult:
    """Result of processing a command.

    Fields:
        handled:        True = command was handled, do NOT send to gateway.
                        False = unknown command, pass through as plain text.
        response_text:  Text to display in chat (None = silent / already forwarded).
        response_card:  Card data dict for special rendering (task card, status card, etc.)
        forward_to:    Session key to send forward_text to (for routing to a specific agent).
        forward_text:  Text content to forward (when forward_to is set).
        broadcast_targets: List of session keys for @-all broadcast fan-out.
    """
    handled: bool = False
    response_text: str | None = None
    response_card: dict | None = None
    forward_to: str | None = None
    forward_text: str | None = None
    broadcast_targets: list[str] = field(default_factory=list)   # BUG #4 fix: fan-out targets for @ broadcast


# ── Command registry ───────────────────────────────────────────────────────────


class CommandRegistry:
    """Maps command names to handler callables. Extensible.

    New commands are added by calling register(). No modification to the handler
    that owns the registry is needed — the registry is the extension point.

    Example:
        def handle_ask(cmd: Command) -> CommandResult:
            return CommandResult(handled=True, response_text="asking...")

        reg = CommandRegistry()
        reg.register("ask", handle_ask, aliases=["a"], help_text="Ask an agent a question")
    """

    def __init__(self) -> None:
        self._commands: dict[str, Callable[[Command], CommandResult]] = {}
        self._aliases: dict[str, str] = {}   # alias → canonical name
        self._help: dict[str, str] = {}      # canonical name → help text
        self._payload_free: set[str] = set() # canonical names that don't require a quoted payload

    def register(
        self,
        name: str,
        handler: Callable[[Command], CommandResult],
        *,
        aliases: list[str] | None = None,
        help_text: str = "",
        payload_free: bool = False,
    ) -> None:
        """Register a command handler.

        Args:
            name:     Canonical command name (lower-cased by convention).
            handler:  Callable that receives a Command and returns a CommandResult.
            aliases:  Alternative names that resolve to this handler.
            help_text: Help string shown by `help <name>`.
            payload_free: If True, the command does not require a quoted payload.
        """
        canonical = name.lower()
        self._commands[canonical] = handler
        self._help[canonical] = help_text
        if payload_free:
            self._payload_free.add(canonical)
        if aliases:
            for alias in aliases:
                al = alias.lower()
                if al in self._aliases:   # BUG #8 fix: warn on collision
                    import logging
                    logging.warning(f"Alias '{al}' already registered for {self._aliases[al]}, overwriting")
                self._aliases[al] = canonical

    def get(self, name: str) -> Callable[[Command], CommandResult] | None:
        """Return the handler for a command name, or None if not registered."""
        name_lower = name.lower()
        if name_lower in self._commands:
            return self._commands[name_lower]
        if name_lower in self._aliases:
            return self._commands[self._aliases[name_lower]]
        return None

    def resolve_name(self, name: str) -> str | None:
        """Return canonical command name for a registered command or alias, or None."""
        if not isinstance(name, str) or not name:
            return None
        name_lower = name.lower()
        if name_lower in self._commands:
            return name_lower
        if name_lower in self._aliases:
            return self._aliases[name_lower]
        return None

    def list_commands(self) -> list[str]:
        """Return sorted list of canonical command names."""
        return sorted(self._commands.keys())

    def list_aliases(self) -> dict[str, str]:   # BUG #12 fix: help shows aliases
        """Return mapping of alias → canonical name for all registered aliases."""
        return dict(self._aliases)

    def is_payload_free(self, name: str) -> bool:
        """Return True if the command is registered as payload-free.

        Resolves aliases to canonical names before checking.
        """
        name_lower = name.lower()
        if name_lower in self._aliases:
            name_lower = self._aliases[name_lower]
        return name_lower in self._payload_free

    def get_help(self, name: str) -> str | None:
        """Return help text for a command, or None if not registered.

        If looked up by canonical name, includes registered aliases.
        """
        name_lower = name.lower()
        alias_of = None
        if name_lower in self._aliases:
            alias_of = name_lower
            name_lower = self._aliases[name_lower]
        base_help = self._help.get(name_lower)
        if base_help is None:
            return None
        # Append aliases only when looking up by canonical name (not via alias)
        if alias_of is None:
            aliases_for_cmd = [al for al, cn in self._aliases.items() if cn == name_lower]
            if aliases_for_cmd:
                return f"{base_help}  [aliases: {', '.join(aliases_for_cmd)}]"
        return base_help
