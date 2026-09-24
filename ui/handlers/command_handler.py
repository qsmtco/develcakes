# ui/handlers/command_handler.py
# Command handler — parses slash commands, routes results.
#
# Manifest:
#   reads:   nothing
#   writes:  nothing
#   network: (SPEC-05 R1) local runtime path only — no gateway transport
#   GTK:     on_display_card(), on_display_text() callbacks only
#
# Owns:
#   - Command prefix detection and parsing
#   - @mention → session_key resolution via AgentManager
#   - --flag parsing
#   - CommandRegistry (owns the handler map)
#   - Result routing: forward / display_card / display_text
#
# Does NOT own:
#   - GTK widgets (only calls back to window for display)
#   - Other handlers (window wires cross-handler communication)
#   - Gateway connection lifecycle
#
# Thread safety: All GTK operations dispatched via GLib.idle_add().
# Public entry point process_input() may be called from main thread (PM input)
# or from gateway background thread — GLib dispatch handles both.


import re
from typing import Callable

from models.command import Command, CommandResult, CommandRegistry, MentionResolution
from utils.config import COMMAND_PREFIX   # BUG #9 fix: config is source of truth
from utils.quoting import _parse_quoted_payload, _PAYLOAD_MAX_CHARS   # A2A_QUOTED_PAYLOAD_SPEC §5.1


class CommandHandler:
    """Parses and executes slash commands.

    Architecture:
        - process_input() is the single public entry point called by ChatHandler
        - Owns a CommandRegistry for handler lookup
        - Resolves @mentions via AgentManager
        - Returns CommandResult; ChatHandler and window act on the result

    Integration with ChatHandler:
        ChatHandler.on_send() calls process_input() before its own send logic.
        If result.handled is True, ChatHandler skips gateway send —
        CommandHandler dispatched the forward/display itself.
    """

    def __init__(
        self,
        agent_manager,            # AgentManager — for @mention resolution
        project_handler,          # ProjectHandler — for project member lookups
        GLib_module=None,         # gi.repository.GLib or None
        on_display_card=None,     # callback(card_dict) — render a card in chat
        on_display_text=None,     # callback(session_key, text) — display text in chat
        collab_handler=None,     # CollabHandler — for ask/delegate/stop/tell
        work_handler=None,       # WorkHandler — for work/tasks/start/done/blocked/cancel/assign/priority
        review_handler=None,     # ReviewHandler — for review/check/accept/reject
        session_handler=None,    # SessionHandler — for session
    ):
        self._agent_mgr = agent_manager
        self._project_handler = project_handler
        self._GLib = GLib_module
        self._on_display_card = on_display_card
        self._on_display_text = on_display_text
        self._prefix = COMMAND_PREFIX   # BUG #9 fix: read from config at construction
        self._registry = CommandRegistry()
        self._special_agents: dict[str, str] = {}  # {session_key: display_name}


        # Store handler references for command registration
        self._collab_handler = collab_handler
        self._work_handler = work_handler
        self._review_handler = review_handler
        self._session_handler = session_handler

        # ── Register built-in commands ──────────────────────────────────────────

        # Help — owned by CommandHandler itself (always registered)
        self.register_command("help", self.cmd_help, aliases=["?"],
            help_text="List all commands or help for a specific command",
            payload_free=True)

        # Collaboration — requires CollabHandler
        if collab_handler is not None:
            self.register_command("ask", collab_handler.cmd_ask, aliases=["a"],
                help_text="Ask an agent a question: /ask @agent — question")
            self.register_command("delegate", collab_handler.cmd_delegate, aliases=["d"],
                help_text="PM delegates to agent: /delegate @agent — task")
            self.register_command("stop", collab_handler.cmd_stop,
                help_text="PM stops the current collaboration: /stop @agent",
                payload_free=True)
            self.register_command("tell", collab_handler.cmd_tell,
                help_text="One agent shares information with another: /tell @agent — info")

        # Work — requires WorkHandler (SPEC-TASK-SYSTEM-FULL-REDESIGN §5.1)
        # CRITICAL: register every legacy name as a SEPARATE canonical command.
        # Do NOT use aliases= for any work command — CommandRegistry.get() checks
        # _commands before _aliases, so registering /work with aliases=["task"]
        # would orphan the legacy /task command. payload_free=True for all.
        if work_handler is not None:
            self.register_command("work", work_handler.cmd_work,
                help_text="Work units: create, list, start, spec-ready, status, unblock, done, blocked, cancel, assign, priority",
                payload_free=True)
            self.register_command("task", work_handler.cmd_work,
                help_text="Create a Work Unit (legacy alias)", payload_free=True)
            self.register_command("tasks", work_handler.cmd_work_list,
                help_text="List Work Units (legacy alias)", payload_free=True)
            self.register_command("start", work_handler.cmd_work_start,
                help_text="Start a Work Unit (legacy alias)", payload_free=True)
            self.register_command("done", work_handler.cmd_work_done,
                help_text="Complete a Work Unit (legacy alias)", payload_free=True)
            self.register_command("blocked", work_handler.cmd_work_blocked,
                help_text="Block a Work Unit (legacy alias)", payload_free=True)
            self.register_command("cancel", work_handler.cmd_work_cancel,
                help_text="Cancel a Work Unit (legacy alias)", payload_free=True)
            self.register_command("assign", work_handler.cmd_work_assign,
                help_text="Assign a Work Unit (legacy alias)", payload_free=True)
            self.register_command("priority", work_handler.cmd_work_priority,
                help_text="Set Work Unit priority (legacy alias)", payload_free=True)

        # Review — requires ReviewHandler
        if review_handler is not None:
            self.register_command("review", review_handler.cmd_review,
                help_text="Start a review checkpoint",
                payload_free=True)
            self.register_command("check", review_handler.cmd_check,
                help_text="Show diff of changes since checkpoint",
                payload_free=True)
            self.register_command("accept", review_handler.cmd_accept,
                help_text="Accept all changes (or single file)",
                payload_free=True)
            self.register_command("reject", review_handler.cmd_reject,
                help_text="Reject all pending changes",
                payload_free=True)

        # Project — requires ProjectHandler (always provided in production; None in tests)
        # Use hasattr guards so test fixtures with fake handlers don't crash.
        if project_handler is not None:
            if hasattr(project_handler, "cmd_status"):
                self.register_command("status", project_handler.cmd_status, aliases=["st"],
                    help_text="Project status summary",
                    payload_free=True)
            if hasattr(project_handler, "cmd_agents"):
                self.register_command("agents", project_handler.cmd_agents,
                    help_text="List project agents and current state",
                    payload_free=True)
            if hasattr(project_handler, "cmd_cost"):
                self.register_command("cost", project_handler.cmd_cost,
                    help_text="Spending summary for this project",
                    payload_free=True)
            if hasattr(project_handler, "cmd_clear"):
                # Spec: docs/specs/STEP-COUNT-RESET-FIX.md Edit 1.
                # /clear resets the current special agent's conversation
                # (messages, step_count, total_tokens, total_cost) so the
                # user can start fresh when step_count would otherwise
                # hit step_limit=100 and kill the agent.
                self.register_command("clear", project_handler.cmd_clear,
                    help_text="Clear agent conversation history and reset step count",
                    payload_free=True)

            # Phase B — /compact command. Mirror /clear pattern.
            if hasattr(project_handler, "cmd_compact"):
                self.register_command("compact", project_handler.cmd_compact,
                    help_text="Compact conversation: /compact [focus-instructions]",
                    payload_free=True)

        # Session — requires SessionHandler
        if session_handler is not None:
            self.register_command("session", session_handler.cmd_session, aliases=["s"],
                help_text="Switch agent session in project: /session list @agent | /session <ref> @agent",
                payload_free=True)

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_agent_manager(self, agent_mgr) -> None:
        """Inject the live AgentManager after connect. Called by window.py."""
        self._agent_mgr = agent_mgr

    def set_special_agents(self, agents: dict[str, str]) -> None:
        """Set special agent registry for @mention resolution.
        Called by window.py after AgentRuntimeHandler is created.
        Dict format: {session_key: display_name} e.g. {"special:coder": "Coder"}"""
        self._special_agents = agents

    def register_command(
        self,
        name: str,
        handler: Callable[[Command], CommandResult],
        *,
        aliases: list[str] | None = None,
        help_text: str = "",
        payload_free: bool = False,
    ) -> None:
        """Register a command handler. Called by window during setup."""
        self._registry.register(name, handler, aliases=aliases, help_text=help_text, payload_free=payload_free)

    def set_prefix(self, char: str) -> None:
        """Change the command prefix character. Default: slash."""
        self._prefix = char

    def cmd_help(self, cmd: Command) -> CommandResult:
        """Handle `help [command] — returns command list card."""
        if cmd.args:
            name = cmd.args[0].lstrip("@")
            help_text = self.get_help(name)
            if help_text is None:
                help_text = f"Unknown command: /{name}"
            else:
                help_text = f"/{name} — {help_text}"
            return CommandResult(handled=True, response_text=help_text)
        lines = [" CrabCakes Commands", ""]
        for name in self._registry.list_commands():
            alias_list = [al for al, cn in self._registry.list_aliases().items() if cn == name]
            alias_str = f" (/{', /'.join(alias_list)})" if alias_list else ""
            lines.append(f"  /{name}{alias_str}")
        lines.extend(["", f"Type /help <command> for details."])
        return CommandResult(handled=True, response_text="\n".join(lines))

    def get_help(self, name: str) -> str | None:   # BUG #10 fix: public API for help
        """Return help text for a command, or None if not registered."""
        return self._registry.get_help(name)

    def get_command_names(self) -> set[str]:
        """Return registered command names as a set.

        Used by AgentCommandHandler to determine if a scanned slash-prefixed token
        is a known command (vs. arbitrary quoted text that looks like a command).
        Spec §4.4: returns set[str] for O(1) membership checks.
        """
        return set(self._registry.list_commands())

    def resolve_inline_mention(self, text: str, session_key: str = "") -> "MentionResolution":
        """Resolve @mentions from plain text (no slash prefix required).

        This is the public API used by ChatHandler for inline @ routing in
        project tabs. Reuses the same parsing and resolution logic as the
        slash command path but without requiring a command prefix.

        Args:
            text:         Raw input text from the user.
            session_key:  Source session key for project context resolution.

        Returns:
            MentionResolution with target info and cleaned text.
        """
        from models.command import MentionResolution

        if not isinstance(text, str) or not text.strip():
            return MentionResolution(clean_text=text)

        tokens = text.split()
        mentions, remaining = self._parse_mentions(tokens)

        if not mentions:
            # No @mention found — not our concern, return clean
            return MentionResolution(clean_text=text)

        if len(mentions) > 1:
            return MentionResolution(
                clean_text=text,
                error=f"Only one @mention allowed. Found: {', '.join(mentions)}",
            )

        # Resolve the single mention
        resolved = self._resolve_mention(mentions[0], session_key)

        if isinstance(resolved, CommandResult):
            # Error from resolution
            return MentionResolution(
                clean_text=" ".join(remaining),
                error=resolved.response_text,
            )
        elif isinstance(resolved, list):
            # Broadcast (@ alone → all project members)
            return MentionResolution(
                broadcast_targets=resolved,
                clean_text=" ".join(remaining),
                is_broadcast=True,
            )
        elif isinstance(resolved, str):
            # Single agent target
            return MentionResolution(
                target_session_key=resolved,
                clean_text=" ".join(remaining),
            )

        # Shouldn't reach here, but defensive
        return MentionResolution(clean_text=text, error="Unexpected resolution result")

    def process_input(self, session_key: str, text: str,
                       skip_dispatch: bool = False) -> CommandResult:
        """Parse and execute a command from input text.

        Decision tree:
          - Text does not start with prefix → not a command, pass through
          - Command not found in registry    → pass through (unknown command)
          - Command found                  → execute handler → return result

        Called by ChatHandler.on_send() before gateway send.
        May be called from background threads — all GTK calls go through
        GLib.idle_add().

        Returns:
            CommandResult with the routing decision.
            handled=False means: not a command OR unknown command → pass through.
        """
        # BUG #2 fix: type safety — text must be a string
        if not isinstance(text, str):
            return CommandResult(handled=False)

        if not text.startswith(self._prefix):
            return CommandResult(handled=False)

        raw = text[len(self._prefix):].strip()
        if not raw:
            return CommandResult(handled=False)

        # ── A2A quoted payload parsing (A2A_QUOTED_PAYLOAD_SPEC §5.3) ──
        # Canonical format: `cmd @Agent "payload"
        # No em-dash fallback. No unquoted body. Strict format = strict validation.
        tokens = raw.split()
        if not tokens:
            return CommandResult(handled=False)
        cmd_name = tokens[0].lower()
        rest_tokens = tokens[1:]

        # Bug #1 fix: implicit ask — `@Agent message` → `ask @Agent message`
        if cmd_name.startswith("@"):
            rest_tokens = [cmd_name] + rest_tokens
            cmd_name = "ask"

        # Look up handler first — unknown command → pass through
        handler = self._registry.get(cmd_name)
        if handler is None:
            return CommandResult(handled=False)

        # Parse @mentions from rest_tokens (strips @tokens, returns remaining)
        mentions_from_rest, args_after_mentions = self._parse_mentions(rest_tokens)
        # BUG #6 fix: reject multiple @mentions explicitly
        if len(mentions_from_rest) > 1:
            return CommandResult(
                handled=True,
                response_text=f"Only one @mention allowed. Found: {', '.join(mentions_from_rest)}",
            )

        # Resolve single @mention
        target_sk = None
        is_broadcast = False
        broadcast_targets = None
        if mentions_from_rest:
            resolved = self._resolve_mention(mentions_from_rest[0], session_key)
            if isinstance(resolved, str):
                target_sk = resolved
            elif isinstance(resolved, list):
                is_broadcast = True
                broadcast_targets = resolved
                target_sk = resolved[0] if resolved else None
            else:
                return resolved  # CommandResult error from _resolve_mention

        # Payload-free commands: check via registry (derives from register_command payload_free=True)
        # Commands marked payload_free: stop, tasks, review, check, accept, reject,
        # status, agents, cost, help, done, start, blocked, cancel, clear, session

        # After @mention, require quoted payload (unless command is payload-free)
        rest_text = " ".join(args_after_mentions)
        ws_end = 0
        while ws_end < len(rest_text) and rest_text[ws_end].isspace():
            ws_end += 1
        after_ws = rest_text[ws_end:]

        body = ""
        error_msg = None

        if after_ws and after_ws[0] == '"':
            payload, q_pos = _parse_quoted_payload(after_ws, 0)
            if payload is None:
                # "" → empty payload error; missing closing " → unclosed quote error
                if len(after_ws) >= 2 and after_ws[1] == '"':
                    error_msg = 'Empty payload — provide a message: /' + cmd_name + ' @Agent "your message"'
                else:
                    error_msg = 'Unclosed quote — missing closing ": /' + cmd_name + ' @Agent "your message"'
            else:
                body = payload
                # Enforce 4K payload cap per spec §4.5
                if len(body) > _PAYLOAD_MAX_CHARS:
                    body = body[:_PAYLOAD_MAX_CHARS] + '…'
        elif self._registry.is_payload_free(cmd_name):
            # Payload-free command: no payload required, no error
            pass
        else:
            error_msg = 'Malformed command — payload must be quoted: /' + cmd_name + ' @Agent "your message"'

        if error_msg:
            return CommandResult(handled=True, response_text=error_msg)

        # Build Command object
        cmd = Command(
            name=cmd_name,
            args=args_after_mentions,  # stripped of @mentions, used for --flags etc.
            flags={},                 # filled below
            raw_text=text[len(self._prefix):].strip(),
            body=body,                # A2A quoted payload
            source_session_key=session_key,
            target_session_key=target_sk,
            is_broadcast=is_broadcast,
            broadcast_targets=broadcast_targets,
            # LOW-1: human-readable user identity from session key
            user=self._human_label_for_session(session_key),
        )

        # Re-parse flags from args_after_mentions for the Command object
        flags, remaining_args = self._parse_flags(args_after_mentions)
        cmd.flags = flags
        cmd.args = remaining_args

        # Execute handler — guard against non-CommandResult returns
        try:
            result = handler(cmd)
        except Exception as exc:
            return CommandResult(
                handled=True,
                response_text=f"Error: {exc}",
            )

        if not isinstance(result, CommandResult):
            return CommandResult(
                handled=True,
                response_text=f"Error: handler returned {type(result).__name__}",
            )

        # Dispatch GTK side effects via GLib if needed
        # (skip when called from AgentCommandHandler — it handles routing itself)
        if result.handled and not skip_dispatch:
            self._dispatch_result(result, session_key)

        return result

    # ── Internal ─────────────────────────────────────────────────────────────

    def _parse_flags(self, tokens: list[str]) -> tuple[dict[str, str], list[str]]:
        """Extract --flag value pairs from tokens.

        Returns (flags_dict, remaining_tokens) with --flags removed.
        --verbose (no value) stores as flags["verbose"] = "".
        --flag value stores as flags["flag"] = "value".
        """
        flags: dict[str, str] = {}
        out: list[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith("--") and len(tok) > 2:
                key = tok[2:]
                if key in flags:   # BUG #11 fix: warn on duplicate flag
                    import logging
                    logging.warning(f"Duplicate flag --{key}, overwriting previous value")
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                    flags[key] = tokens[i + 1]
                    i += 2
                else:
                    flags[key] = ""
                    i += 1
            else:
                out.append(tok)
                i += 1
        return flags, out

    _EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[a-z]{2,}$', re.IGNORECASE)

    def _parse_mentions(self, tokens: list[str]) -> tuple[list[str], list[str]]:
        """Extract @mentions from tokens, returning (mentions, remaining_args).

        Finds the first contiguous run of @tokens. All other tokens (before,
        between, and after) are preserved in remaining in original order.
        Only the first run is collected; subsequent @tokens after a break
        are treated as regular args.

        Skips tokens that look like email addresses (defensive).

        Returns:
            mentions:  List of @mention tokens from the first run.
            remaining: All non-collected tokens in original order.
        """
        mentions: list[str] = []
        remaining: list[str] = []
        state = "pre"  # pre | collecting | post

        for tok in tokens:
            if tok.startswith("@") and state != "post":
                # Skip email-like tokens
                if self._EMAIL_RE.match(tok[1:]):
                    remaining.append(tok)
                    state = "post"
                    continue
                mentions.append(tok)
                state = "collecting"
            else:
                remaining.append(tok)
                if state == "collecting":
                    state = "post"  # non-@ token ends the mention run

        return mentions, remaining

    def _resolve_mention(self, mention: str, session_key: str = "") -> str | list[str] | CommandResult:
        """Resolve @mention to session_key(s).

        - @        → all project members (list) OR error if no project_handler
        - @name    → exact or partial name match via AgentManager
        - No match → CommandResult error (handled=True, response_text=error)

        Args:
            mention:      The @token to resolve (e.g. "@Qaster" or "@")
            session_key:  Source session key for project context resolution.
                          Used to determine which project @ broadcast targets.
        """
        name = mention[1:]  # strip leading @

        if not name:
            # Empty @ → project broadcast
            proj_name = self._resolve_project_from_session(session_key)
            if proj_name and self._project_handler is not None:
                members = self._project_handler.get_project_members(proj_name)
                if members:
                    return members
            return CommandResult(
                handled=True,
                response_text="No active project for @ broadcast.",
            )

        # Exact match via AgentManager
        if self._agent_mgr is not None:
            # AgentManager.get_names_ref() → {session_key: name}
            names_ref = self._agent_mgr.get_names_ref()
            # Try exact name match first
            for sk, n in names_ref.items():
                if n.lower() == name.lower():
                    return sk
            # Try prefix match (starts-with, not contains)
            if len(name) >= 2:
                matches = [sk for sk, n in names_ref.items() if n.lower().startswith(name.lower())]
                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    get_name = getattr(self._agent_mgr, 'get_name', lambda sk: sk)
                    names = [get_name(sk) for sk in matches]
                    return CommandResult(
                        handled=True,
                        response_text=f"Multiple agents match @{name}: {', '.join(names)}",
                    )

        # Search special agents (Coder, Debugger, etc.)
        for sk, display_name in self._special_agents.items():
            if display_name.lower() == name.lower():
                return sk
        if len(name) >= 2:
            matches = [sk for sk, dn in self._special_agents.items()
                       if dn.lower().startswith(name.lower())]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                names = [self._special_agents[sk] for sk in matches]
                return CommandResult(
                    handled=True,
                    response_text=f"Multiple agents match @{name}: {', '.join(names)}",
                )

        return CommandResult(
            handled=True,
            response_text=f"Unknown agent: @{name}",
        )

    def _resolve_project_from_session(self, session_key: str) -> str | None:
        """Resolve project name from a session key.

        For project tabs (session_key starts with "project:"), extracts
        the project name from the key. Falls back to the global active
        project name for backward compatibility.

        Args:
            session_key: Source session key to resolve project from.

        Returns:
            Project name or None.
        """
        # Project tab: extract directly from session key
        if session_key.startswith("project:"):
            return session_key.split(":", 1)[1]
        # Fallback: global active project (backward compat)
        if self._project_handler is not None:
            return self._project_handler.get_active_project_name()
        return None

    def _human_label_for_session(self, session_key: str) -> str:
        """Return a human-readable label for a session key (LOW-1).

        Tries, in order:
        1. The agent display name from AgentManager (gateway agents)
        2. The display name from _special_agents (special agents)
        3. The last segment of the session key (e.g. 'telegram' from
           'agent:qaster:telegram:direct:7478874934')
        """
        if self._agent_mgr is not None:
            name = self._agent_mgr.get_name(session_key)
            if name:
                return name
        if self._special_agents:
            name = self._special_agents.get(session_key)
            if name:
                return name
        segments = session_key.split(":")
        return segments[-1] if segments else session_key

    def _dispatch_result(self, result: CommandResult, session_key: str) -> None:
        """Dispatch GTK side effects of a handled CommandResult.

        Note: forward_to/forward_text routing is handled by ChatHandler, not here.
        This avoids double-send (ChatHandler routes forward commands via the
        local runtime path after process_input returns).
        """
        def _do():
            try:
                if result.response_card and self._on_display_card:
                    self._on_display_card(result.response_card)
                if result.response_text and self._on_display_text:
                    self._on_display_text(session_key, result.response_text)
            except Exception as exc:
                import logging
                logging.exception("Error dispatching command result")

        if self._GLib is not None:
            self._GLib.idle_add(_do)
        else:
            _do()

    def _dispatch(self, fn: Callable) -> None:
        """Call fn on the GTK main thread. Direct call if GLib not available."""
        if self._GLib is not None:
            def _wrap():
                fn()
                return False
            self._GLib.idle_add(_wrap)
        else:
            fn()
