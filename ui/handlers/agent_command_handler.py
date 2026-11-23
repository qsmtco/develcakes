# ui/handlers/agent_command_handler.py
# Agent Response Command Parser — agent-initiated A2A with relay (Phase 6.2).
#
# Manifest:
#   reads:   nothing
#   writes:  nothing
#   network: (SPEC-05 R1) local runtime path only — send_to_special_agent
#   GTK:     none (callbacks dispatched by callers on main thread)
#
# Architecture:
#   - Follows handler pattern: receives all dependencies via setters
#   - Never imports from ui/handlers/
#   - Thread safety: on_agent_response() is called from main thread via
#     GLib.idle_add() in both ChatHandler._handle_final_response() and
#     AgentRuntimeHandler._do_response_complete() — no additional dispatch needed.
#
# Wire points (window.py):
#   - set_command_handler()      → CommandHandler instance
#   - set_agent_runtime_handler() → AgentRuntimeHandler instance
#   - set_agent_manager()        → AgentManager instance
#   - set_agent_routing()        → AgentRoutingTable instance
#   - set_project_handler()       → ProjectHandler instance

import re
import logging
from collections import namedtuple
from typing import Any

from utils.audit_parser import extract_audit_reports

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

# Max nested command chains before cutoff.
_MAX_CHAIN_DEPTH = 3

# Max commands parsed from a single agent response.
_MAX_COMMANDS_PER_RESPONSE = 3

# Command keywords recognized in slash-prefixed agent responses.
_COMMAND_KEYWORDS = frozenset({'ask', 'delegate', 'stop', 'tell'})

# Maximum characters in a quoted A2A payload (per spec §5.4).
_QUOTED_PAYLOAD_MAX = 4096

# Parsed A2A command from quoted-payload format.
# Fields: command, agent, payload, raw_start, raw_end
ParsedCommand = namedtuple('ParsedCommand', ['command', 'agent', 'payload', 'raw_start', 'raw_end'])

# Commands the agent scanner recognizes before the registry filter.
_SCAN_COMMANDS = frozenset({"ask", "tell", "delegate", "stop", "compact", "clear"})

# Opening quote characters accepted on the agent path (ASCII, curly, single).
_QUOTE_OPEN = frozenset({'"', "\u201c", "\u201d", "'"})

# Prefix before `/`: start, whitespace, or a line-leading list/quote marker,
# then optional inline Markdown wrappers (backtick, bold, paren, bracket).
_CMD_PREFIX_RE = re.compile(
    r"(?:^|\s|(?:(?<=^)|(?<=\n))[ \t]*[-+>][ \t]*)[`*_(\[]*/",
)

# `@Agent` plus optional trailing `[:,;]` and an em-dash / en-dash / hyphen
# separator. `$` is accepted so payload-free `/stop @Agent` at end of string
# still matches (the spec's `\s+` would drop it).
_AGENT_TOKEN_RE = re.compile(
    r"(@[^\s\"`\u201c\u201d'*_)\]:;,—–]+)"
    r"[:;,]*"
    r"(?:[ \t]+(?:[—–-]+[ \t]+)?|(?=\s|$))"
)

_CLOSING_FOLLOWERS = frozenset("`*_).,:;!?]")
_NEXT_CMD_RE = re.compile(
    r"\s+[`*_(\[]*/(?:ask|a|tell|delegate|stop|compact|clear|@[^\s\"`]+)\b",
    re.IGNORECASE,
)


def _scan_agent_quoted_payload(
    text: str, open_pos: int, *, wrapped_backtick: bool = False
) -> tuple[str | None, int, bool]:
    """Scan a quoted A2A payload starting at ``open_pos``.

    Supports ASCII ``"..."``, typographic ``“...”``, single ``'...'``,
    ``\\"`` and ``\\\\`` escapes, ``\\\\\\n`` continuations, single-line
    unescaped inner quotes (e.g. ``"check "foo" in bar.py"``), and agent
    end-of-string auto-close.
    Returns ``(payload_or_none, end_pos, was_closed)``.
    """
    n = len(text)
    if open_pos >= n:
        return None, open_pos, False
    open_ch = text[open_pos]
    close_map = {'"': '"', "\u201c": "\u201d", "\u201d": "\u201d", "'": "'"}
    if open_ch not in close_map:
        return None, open_pos, False
    close_ch = close_map[open_ch]
    close_set = {close_ch, '"'} if open_ch in ("\u201c", "\u201d") else {close_ch}

    nl_pos = text.find("\n", open_pos + 1)
    line_limit = nl_pos if nl_pos != -1 else n
    next_cmd = _NEXT_CMD_RE.search(text, open_pos + 1, line_limit)
    seg_limit = next_cmd.start() if next_cmd else line_limit

    def _remaining_unescaped_closes(from_idx: int, limit_idx: int) -> int:
        cnt = 0
        k = from_idx
        while k < limit_idx:
            if text[k] == "\\" and k + 1 < limit_idx:
                k += 2
                continue
            if text[k] in close_set:
                cnt += 1
            k += 1
        return cnt

    parts: list[str] = []
    i = open_pos + 1
    inner_depth = 0
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt in close_set or nxt == '"':
                parts.append(nxt)
                i += 2
                continue
            if nxt == "\\":
                parts.append("\\")
                i += 2
                continue
            parts.append("\\")
            i += 1
            continue
        if ch in close_set:
            if inner_depth > 0:
                inner_depth -= 1
                parts.append(ch)
                i += 1
                continue
            if (
                i < seg_limit
                and i + 1 < seg_limit
                and not text[i + 1].isspace()
                and text[i + 1] not in _CLOSING_FOLLOWERS
                and _remaining_unescaped_closes(i + 1, seg_limit) >= 2
            ):
                inner_depth += 1
                parts.append(ch)
                i += 1
                continue
            payload = "".join(parts)
            if not payload:
                return None, open_pos, True
            return payload, i + 1, True
        parts.append(ch)
        i += 1

    payload = "".join(parts)
    if wrapped_backtick and payload.endswith("`"):
        payload = payload[:-1]
    if not payload:
        return None, open_pos, False
    return payload, n, False


def _extract_quoted_commands(text: str, command_names: set[str] | None = None) -> list[ParsedCommand]:
    """Extract A2A commands in quoted-payload format.

    Sanitizes ``<think>`` blocks and code fences first, accepts Markdown and
    list prefixes, curly/single quotes, the ``/a`` alias, and em-dash
    separators, then returns matches in source order.
    """
    if not isinstance(text, str) or not text:
        return []
    clean = AgentCommandHandler._sanitize_for_command_scan(text)
    if not isinstance(clean, str) or not clean:
        return []

    results: list[ParsedCommand] = []
    occupied: list[tuple[int, int]] = []

    def _overlaps(start: int, end: int) -> bool:
        return any(not (end <= s0 or start >= e0) for s0, e0 in occupied)

    def _allowed(cmd: str) -> bool:
        if cmd not in _SCAN_COMMANDS:
            return False
        return command_names is None or cmd in command_names

    pos = 0
    n = len(clean)
    while pos < n:
        match = _CMD_PREFIX_RE.search(clean, pos)
        if match is None:
            break
        cursor = match.end()
        if cursor <= pos:
            pos += 1
            continue

        cmd = ""
        agent = ""
        agent_end = cursor
        if cursor < n and clean[cursor] == "@":
            cmd = "ask"
            agent_match = _AGENT_TOKEN_RE.match(clean, cursor)
            if agent_match is None:
                pos = cursor
                continue
            agent = agent_match.group(1)
            agent_end = agent_match.end()
        else:
            cmd_match = re.match(r"[A-Za-z]+", clean[cursor:])
            if cmd_match is None:
                pos = cursor
                continue
            cmd = cmd_match.group(0).lower()
            probe = cursor + cmd_match.end()
            while probe < n and clean[probe].isspace():
                probe += 1
            agent_match = _AGENT_TOKEN_RE.match(clean, probe)
            if agent_match is None:
                pos = probe if probe > pos else cursor
                continue
            agent = agent_match.group(1)
            agent_end = agent_match.end()

        if cmd == "a" and (
            command_names is None or "a" in command_names or "ask" in command_names
        ):
            cmd = "ask"
        if not _allowed(cmd):
            pos = agent_end if agent_end > pos else cursor
            continue

        quote_at = agent_end
        has_quote = quote_at < n and clean[quote_at] in _QUOTE_OPEN
        wrapped = "`" in clean[match.start():match.end()]

        if cmd == "stop":
            end = agent_end
            if has_quote:
                _payload, end_pos, _closed = _scan_agent_quoted_payload(
                    clean, quote_at, wrapped_backtick=wrapped,
                )
                if end_pos > quote_at:
                    end = end_pos
            span = (match.start(), end)
            if not _overlaps(span[0], span[1]):
                occupied.append(span)
                results.append(ParsedCommand("stop", agent, "", span[0], span[1]))
            pos = max(end, pos + 1)
            continue

        if has_quote:
            payload, end_pos, _closed = _scan_agent_quoted_payload(
                clean, quote_at, wrapped_backtick=wrapped,
            )
            if payload is None:
                pos = max(quote_at + 1, pos + 1)
                continue
            if len(payload) > _QUOTED_PAYLOAD_MAX:
                payload = payload[:_QUOTED_PAYLOAD_MAX] + "…"
            span = (match.start(), end_pos)
            if _overlaps(span[0], span[1]):
                pos = max(end_pos, pos + 1)
                continue
            occupied.append(span)
            results.append(ParsedCommand(cmd, agent, payload, span[0], span[1]))
            pos = max(end_pos, pos + 1)
            continue

        if cmd in ("compact", "clear"):
            span = (match.start(), agent_end)
            if not _overlaps(span[0], span[1]):
                occupied.append(span)
                results.append(ParsedCommand(cmd, agent, "", span[0], span[1]))
            pos = max(agent_end, pos + 1)
            continue

        pos = max(agent_end, pos + 1)

    results.sort(key=lambda pc: pc.raw_start)
    return results




class AgentCommandHandler:
    """Parses slash commands from agent response text, routes them to target
    agents, and relays responses back to the asking agent.

    Wired via window.py. Receives all dependencies through setters.
    """

    def __init__(self, *, GLib_module=None):
        self._command_handler = None       # CommandHandler
        self._agent_runtime_handler = None  # AgentRuntimeHandler
        self._agent_mgr = None              # AgentManager
        self._agent_to_project = None       # AgentRoutingTable
        self._project_handler = None        # ProjectHandler

        # Chain depth: session_key → depth counter
        self._chain_depth: dict[str, int] = {}

        # Pending asks: target_session_key → source_session_key
        # When Agent A asks Agent B, we record _pending_asks[B] = A.
        # When B responds, we relay B's answer back to A.
        # Only set for response-expecting commands (ask, delegate). `tell` is
        # one-way and does NOT create a pending ask.
        self._pending_asks: dict[str, str] = {}
        # Extra concurrent askers for the same target. The head stays in
        # _pending_asks so the dict shape remains {target_sk: source_sk}.
        self._pending_ask_waiters: dict[str, list[str]] = {}

        # For audit report processing — see _process_audit_reports()
        self._project_path_provider: Any = None   # Callable[] → str | None
        self._agent_defs_loader: Any = None     # Callable[] → list[dict]
        self._on_audit_report: Any = None         # Callable(dict) → None
        # SPEC-66 §2.8: /stop sink. __new__-constructed test doubles lack this
        # attribute, so every read uses getattr(self, "_on_cancel_session", None).
        self._on_cancel_session: Any = None       # Callable(session_key) → None

    # ── Setters (wired by window.py) ─────────────────────────────────────────

    def set_command_handler(self, handler) -> None:
        """CommandHandler — provides process_input() and get_command_names()."""
        self._command_handler = handler

    def set_agent_runtime_handler(self, handler) -> None:
        """AgentRuntimeHandler — for special agent routing."""
        self._agent_runtime_handler = handler

    def set_agent_manager(self, mgr) -> None:
        """AgentManager — for display name resolution."""
        self._agent_mgr = mgr

    def set_agent_routing(self, routing_table) -> None:
        """AgentRoutingTable — for project→agent lookups."""
        self._agent_to_project = routing_table

    def set_project_handler(self, handler) -> None:
        """ProjectHandler — for project lookups in agent-initiated commands."""
        self._project_handler = handler

    def set_project_path_provider(self, provider: Any) -> None:
        """Callable that returns the active project path, or None."""
        self._project_path_provider = provider

    def set_agent_defs_loader(self, loader: Any) -> None:
        """Callable that loads agent definitions for self_improvement lookup."""
        self._agent_defs_loader = loader

    def set_on_cancel_session(self, callback: Any) -> None:
        """Callback for /stop issued by an agent. Signature: callback(session_key) -> None.

        Wired by window.py to AgentRuntimeHandler.cancel_agent (SPEC-66 §2.8).
        """
        self._on_cancel_session = callback

    def set_on_audit_report(self, callback: Any) -> None:
        """Callback for audit report feed cards. Signature: callback(report_dict) -> None.

        report_dict keys: severity, file_path, task, bug_description, pattern,
        reviewer, target_role, project_path, logged (bool), journal_appended (bool).
        """
        self._on_audit_report = callback

    # ── Core entry point ──────────────────────────────────────────────────────

    def on_agent_response(self, session_key: str, text: str,
                          project_name: str | None) -> set[str]:
        """Called after an agent's final response is rendered.

        Three responsibilities:
        1. AUDIT: Detect and process structured audit reports (SPEC-3).
        2. RELAY: If this agent has a pending ask, relay response to asker.
        3. COMMAND: Scan for slash A2A commands.

        Returns the set of target session keys routed by commands in this
        response. Empty when nothing was forwarded.

        Args:
            session_key: The responding agent's session key
                         (e.g. "agent:coder" or "agent:qaster:...")
            text: The agent's full response text
            project_name: Active project name, or None
        """
        routed_targets: set[str] = set()
        if not isinstance(text, str) or not text:
            return routed_targets

        # ── Step 0: Process structured audit reports (SPEC-3) ───────────────
        self._process_audit_reports(session_key, text)

        # ── Step 1: Relay answer back to asking agent ────────────────────────
        # A stranded "[... asks]: " inbound is about to start as the next
        # turn. Leave the pending ask in place for that turn.
        if not self._has_unstarted_ask_for(session_key):
            source_sk = self._pop_next_asker(session_key)
            if source_sk is not None:
                self._relay_response(source_sk, session_key, text, project_name)

        # ── Step 2: Scan for new commands ─────────────────────────────────────

        # Command scanning is only available when command_handler is set
        if not self._command_handler:
            # No command handler — clear depth and return (relay already done above)
            self._chain_depth.pop(session_key, None)
            return routed_targets

        # Chain depth guard — prevent runaway command chains.
        # Must check BEFORE any command processing to respect the limit.
        depth = self._chain_depth.get(session_key, 0)
        if depth >= _MAX_CHAIN_DEPTH:
            logger.warning(
                "[agent-cmd] Chain depth limit (%d) reached for %s — dropping commands",
                _MAX_CHAIN_DEPTH, session_key
            )
            self._chain_depth.pop(session_key, None)
            return routed_targets

        # Extract A2A commands. Sanitizing (<think>, html/live fences, code
        # fences) happens inside _extract_quoted_commands.
        command_names = self._command_handler.get_command_names() if self._command_handler else None
        parsed_commands = _extract_quoted_commands(text, command_names)

        # Process commands if any are found
        if parsed_commands:
            command_count = 0

            for pc in parsed_commands:
                if command_count >= _MAX_COMMANDS_PER_RESPONSE:
                    logger.warning(
                        "[agent-cmd] Per-response command limit (%d) reached — skipping remaining",
                        _MAX_COMMANDS_PER_RESPONSE
                    )
                    break

                # Rebuild in canonical quoted-payload format for process_input.
                # Spec §5.4: escape backslashes then quotes before wrapping.
                # Order matters — escape \\ first to avoid double-escaping.
                # Payload-free commands (e.g. stop) get no payload.
                if pc.payload:
                    escaped = pc.payload.replace('\\', '\\\\').replace('"', '\\"')
                    candidate = f"/{pc.command} {pc.agent} \"{escaped}\""
                else:
                    candidate = f"/{pc.command} {pc.agent}"

                result = self._command_handler.process_input(session_key, candidate,
                                                             skip_dispatch=True)

                # SPEC-66 §2.8(c): /stop from an agent — cancel the target and
                # count the command, then CONTINUE. A break here would drop the
                # other commands in the same response.
                if result.handled and result.cancel_session_key:
                    cb = getattr(self, "_on_cancel_session", None)
                    if cb is not None:
                        try:
                            cb(result.cancel_session_key)
                            command_count += 1
                            continue
                        except Exception:
                            logger.exception(
                                "[agent-cmd] cancel callback failed for %r",
                                result.cancel_session_key,
                            )
                    # No sink wired: fall through to the action-result path so
                    # the issuing agent still sees the outcome.
                if result.handled and result.forward_to and result.forward_text:
                    route_key = self._route_command(
                        result, project_name, depth, source_sk=session_key,
                        command_name=pc.command,
                    )
                    if route_key:
                        routed_targets.add(route_key)
                    command_count += 1
                elif result.handled and result.broadcast_targets and result.forward_text:
                    for target in result.broadcast_targets:
                        self._route_to_target(
                            target, result.forward_text, project_name
                        )
                        if isinstance(target, str) and target:
                            routed_targets.add(target)
                    command_count += 1
                elif result.handled and result.response_text:
                    # Action result (e.g. /compact, /clear) — no forward target.
                    # Inject the result into the issuing agent's own conversation
                    # so the supervisor sees the outcome on its next turn.
                    logger.warning(
                        "[agent-cmd] Command %r from %s returned non-forward message: %s",
                        candidate, session_key, result.response_text,
                    )
                    self._record_action_result(session_key, result.response_text)
                    command_count += 1

        # A self-ask keeps this session in routed_targets so its depth stays
        # incremented. Every other completion clears the chain counter.
        if session_key not in routed_targets:
            self._chain_depth.pop(session_key, None)
        return routed_targets

    def _has_unstarted_ask_for(self, session_key: str) -> bool:
        """True when a stranded ``[... asks]: `` message is still queued."""
        if not isinstance(session_key, str) or not session_key:
            return False
        arh = self._agent_runtime_handler
        checker = getattr(arh, "has_pending_ask_inbound", None)
        if callable(checker):
            try:
                return bool(checker(session_key))
            except Exception:
                logger.debug(
                    "has_pending_ask_inbound check failed for %s",
                    session_key, exc_info=True,
                )
        return False

    def _push_pending_ask(self, target_sk: str, source_sk: str) -> None:
        """Record an asker. The first asker is the head; later ones wait."""
        if not isinstance(target_sk, str) or not target_sk:
            return
        if not isinstance(source_sk, str) or not source_sk:
            return
        head = self._pending_asks.get(target_sk)
        if head is None:
            self._pending_asks[target_sk] = source_sk
            return
        if source_sk == head:
            return
        waiters = self._pending_ask_waiters.setdefault(target_sk, [])
        if source_sk not in waiters:
            waiters.append(source_sk)

    def _pop_next_asker(self, session_key: str) -> str | None:
        """Pop the head asker and promote the next waiter, if any."""
        if not isinstance(session_key, str) or not session_key:
            return None
        head = self._pending_asks.pop(session_key, None)
        waiters = self._pending_ask_waiters.get(session_key)
        if waiters:
            nxt = waiters.pop(0)
            if waiters:
                self._pending_ask_waiters[session_key] = waiters
            else:
                self._pending_ask_waiters.pop(session_key, None)
            if isinstance(nxt, str) and nxt:
                self._pending_asks[session_key] = nxt
        return head

    def on_agent_error(self, session_key: str, error_text: str) -> None:
        """Clear zombie pending-ask / chain-depth state when a target turn fails."""
        if not isinstance(session_key, str) or not session_key:
            return
        if self._has_unstarted_ask_for(session_key):
            return
        self._chain_depth.pop(session_key, None)
        source_sk = self._pop_next_asker(session_key)
        if source_sk is not None:
            display_name = self._resolve_display_name(session_key)
            shown = error_text if isinstance(error_text, str) else str(error_text)
            logger.warning(
                "[agent-cmd] Clearing pending ask %s -> %s after target turn error: %s",
                source_sk, session_key, shown,
            )
            self._record_action_result(
                source_sk,
                f"{display_name} encountered an error before answering: {shown}",
            )

    def _record_action_result(self, source_sk: str, text: str) -> None:
        """Inject an action result into the issuing agent's own conversation.

        Used for agent-issued commands that mutate peer state (e.g. /compact,
        /clear) and return a response_text result with no forward target.
        The supervisor's next turn will include this text in its LLM context.
        """
        if self._agent_runtime_handler is None:
            logger.warning(
                "[agent-cmd] Cannot record action result for %s — "
                "agent_runtime_handler not wired",
                source_sk,
            )
            return

        agent_def = self._agent_runtime_handler.get_special_agent_def(source_sk)
        if agent_def is None:
            logger.debug(
                "[agent-cmd] _record_action_result: no SpecialAgentDef for %s",
                source_sk,
            )
            return

        try:
            rt = self._agent_runtime_handler._get_runtime(
                agent_def.display_name, agent_def=agent_def
            )
        except Exception as exc:
            logger.warning(
                "[agent-cmd] _record_action_result: failed to get runtime for %s: %s",
                source_sk, exc,
            )
            return

        try:
            conv = rt.get_conversation(source_sk)
        except Exception as exc:
            logger.warning(
                "[agent-cmd] _record_action_result: get_conversation failed for %s: %s",
                source_sk, exc,
            )
            return

        if conv is None:
            logger.debug(
                "[agent-cmd] _record_action_result: no conversation for %s",
                source_sk,
            )
            return

        try:
            with rt._lock:
                conv.add_user_message(f"[Action result]: {text}")
        except Exception as exc:
            logger.warning(
                "[agent-cmd] _record_action_result: add_user_message failed for %s: %s",
                source_sk, exc,
            )

    # ── Relay ─────────────────────────────────────────────────────────────────

    def _relay_response(self, source_sk: str, target_sk: str,
                        text: str, project_name: str | None) -> None:
        """Relay an agent's response back to the agent that asked it a question.

        Args:
            source_sk: Session key of the agent that asked the question (recipient)
            target_sk: Session key of the agent that answered (responder)
            text: The responder's full response text
            project_name: Active project name, or None
        """
        # Resolve display name of the answering agent
        display_name = self._resolve_display_name(target_sk)
        relay_text = f"[{display_name} responded]: {text}"

        logger.info(
            "[agent-cmd] Relaying response from %s (%s) back to %s",
            display_name, target_sk, source_sk
        )

        # Clear chain depth for source — the relay is a new context message,
        # not a command chain hop from source
        self._chain_depth.pop(source_sk, None)

        self._route_to_target(source_sk, relay_text, project_name)

    # ── Routing ───────────────────────────────────────────────────────────────

    def _route_command(self, result, project_name: str | None,
                       current_depth: int, source_sk: str,
                       command_name: str = "") -> str:
        """Route a single CommandResult to the target agent and record pending ask.

        Only records a pending ask for response-expecting commands (ask, delegate).
        `tell` is one-way and does NOT set up a relay.
        """
        target_sk = result.forward_to

        # Resolve display_name → session_key for special agents.
        # CommandHandler returns display_name (e.g. "Debugger"), not session_key.
        resolved_sk = self._resolve_special_agent_sk(target_sk)
        is_special = resolved_sk is not None

        # Use resolved_sk if special agent, else original target_sk
        record_key = resolved_sk if is_special else target_sk
        depth_key = resolved_sk if is_special else target_sk

        # Record pending ask ONLY for response-expecting commands
        if command_name != "tell":
            self._push_pending_ask(record_key, source_sk)

        # Increment chain depth for the target
        self._chain_depth[depth_key] = current_depth + 1

        # Route the message to the target — prefix with asking agent's identity
        # so the target knows who's asking (not the human, but another agent)
        route_key = resolved_sk if is_special else target_sk
        sender_name = self._resolve_display_name(source_sk)
        forward_text = f"[{sender_name} asks]: {result.forward_text}"
        self._route_to_target(route_key, forward_text, project_name)
        return route_key if isinstance(route_key, str) else ""

    def _send_with_guard(self, target_sk: str, text: str) -> None:
        """SPEC-58 r4 chunk 2: a guarded send_to_special_agent call.

        The receiver's _get_runtime guard can raise (RuntimeError "No
        provider configured — ..." / "No API key configured for provider
        ..."); an unguarded call inside the agent-response pipeline kills
        response processing for every later listener. This handler has no
        UI surface wired (the composition root hands it the command
        handler, the runtime handler, and the response callback — nothing
        render-capable), so the failure is surfaced the way THIS handler
        surfaces everything: the warning log. Logged, never raised.
        """
        if self._agent_runtime_handler is None:
            return
        try:
            self._agent_runtime_handler.send_to_special_agent(target_sk, text)
        except Exception as exc:
            logger.warning(
                "[agent-cmd] Send to %s failed: %s", target_sk, exc)

    def _route_to_target(self, target_sk: str, text: str,
                         project_name: str | None) -> None:
        """Send message to a target agent via the correct transport."""
        # Check if target_sk is already a session key (direct lookup in special_agents).
        # This handles relay calls where source_sk is already a session key like "agent:coder".
        if self._agent_runtime_handler is not None and \
           target_sk in self._agent_runtime_handler.get_special_agents():
            self._send_with_guard(target_sk, text)
            return

        # Try resolving display_name → session_key (CommandHandler returns display names)
        resolved_sk = self._resolve_special_agent_sk(target_sk)
        if resolved_sk is not None:
            self._send_with_guard(resolved_sk, text)
            return

        # Local path only (SPEC-05 R1): receiver no-ops with a warning for
        # unregistered/remote keys — that no-op IS the former gateway branch.
        if self._agent_runtime_handler is not None:
            self._send_with_guard(target_sk, text)
        else:
            logger.debug(
                "[agent-cmd] No runtime handler — cannot route to %s", target_sk
            )

    def _resolve_special_agent_sk(self, display_name: str) -> str | None:
        """Resolve a display name (e.g. "Debugger") to a special agent session key.

        CommandHandler.process_input() returns display_name as forward_to, not
        session_key. Reverse-map to find the session_key for routing.
        """
        if self._agent_runtime_handler is None:
            return None
        specials = self._agent_runtime_handler.get_special_agents()
        display_to_sk = {v: k for k, v in specials.items()}
        return display_to_sk.get(display_name)

    def _process_audit_reports(self, session_key: str, text: str) -> None:
        """Detect and process structured audit reports in an agent message.

        Delegates to utils.feedback_processor for all file I/O.
        Side effects (never raises): log to review-log.jsonl,
        optionally append to {role}-bugs.md.
        """
        from utils.feedback_processor import process_audit_reports

        # Strip fenced blocks BEFORE extraction (prevent false positives from
        # audit report examples inside ```...``` code blocks)
        clean_text = self._strip_fenced_blocks(self._strip_thinking_blocks(text))
        reports = extract_audit_reports(clean_text)
        if not reports:
            return

        project_path = None
        if self._project_path_provider is not None:
            project_path = self._project_path_provider()

        if not project_path:
            logger.debug(
                "[agent-cmd] Audit reports found but no active project — skipping"
            )
            return

        reviewer = self._resolve_display_name(session_key)
        target_role = self._resolve_target_role(session_key)

        process_audit_reports(
            project_path=project_path,
            reports=reports,
            reviewer=reviewer,
            target_role=target_role,
        )

        # Emit feed card(s) for each processed report
        if self._on_audit_report is not None:
            for report in reports:
                try:
                    self._on_audit_report({
                        "severity": report.severity,
                        "file_path": report.file_path,
                        "task": report.task,
                        "bug_description": report.bug_description,
                        "pattern": report.pattern,
                        "reviewer": reviewer,
                        "target_role": target_role,
                        "project_path": project_path,
                    })
                except Exception as e:
                    logger.warning("[agent-cmd] Failed to emit audit report feed card: %s", e)

    def _resolve_target_role(self, reviewer_session_key: str) -> str:
        """Determine the target agent role from the review context.

        Strategy:
        1. If there's a pending ask for this reviewer, the target is the asker.
        2. Otherwise delegate to feedback_processor for agent-def lookup.
        3. Fallback to 'unknown'.

        Args:
            reviewer_session_key: Session key of the agent that sent the audit.
        """
        from utils.feedback_processor import (
            resolve_default_target_role,
            resolve_role_from_session,
        )

        # Check if there's a pending ask — the reviewer was asked by someone
        if reviewer_session_key in self._pending_asks:
            asker_sk = self._pending_asks[reviewer_session_key]
            return resolve_role_from_session(
                asker_sk, self._agent_runtime_handler
            )

        # No pending ask — delegate to utility for agent-def lookup
        return resolve_default_target_role()

    def _resolve_display_name(self, session_key: str) -> str:
        """Resolve a session key to a human-readable display name."""
        if self._agent_runtime_handler is not None:
            specials = self._agent_runtime_handler.get_special_agents()
            if session_key in specials:
                return specials[session_key]
        if self._agent_mgr is not None:
            name = self._agent_mgr.get_name(session_key)
            if name:
                return name
        return session_key.split("/")[-1]

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _strip_thinking_blocks(text: str) -> str:
        """Remove <think>...</think> reasoning blocks (and unclosed trailing <think>)."""
        if not isinstance(text, str) or not text:
            return "" if not isinstance(text, str) else text
        text = re.sub(r"<think\b[^>]*>.*?</think>", "\n", text, flags=re.DOTALL | re.IGNORECASE)
        return re.sub(r"<think\b[^>]*>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)

    @classmethod
    def _sanitize_for_command_scan(cls, text: str) -> str:
        """Strip <think> blocks, unwrap WebKit html/live fences, then strip code fences."""
        if not isinstance(text, str):
            return ""
        text = cls._strip_thinking_blocks(text)

        def _unwrap_html_fence(m: re.Match) -> str:
            body = m.group(1)
            body = re.sub(
                r"<(script|style|pre|code)\b[^>]*>.*?</\1>",
                " ",
                body,
                flags=re.DOTALL | re.IGNORECASE,
            )
            body = re.sub(r"<[^>]+>", "\n", body)
            return "\n" + body.replace("&quot;", '"').replace("&#34;", '"') + "\n"

        text = re.sub(
            r"```(?:html|live)[ \t]*\r?\n(.*?)```",
            _unwrap_html_fence,
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        return cls._strip_fenced_blocks(text)

    @staticmethod
    def _strip_fenced_blocks(text: str) -> str:
        """Remove fenced code blocks (```...```) from text.

        Prevents false-positive command detection on code examples.
        Handles both ```language\ncode``` and ```inline``` forms.
        """
        return re.sub(r"```.*?```", "", text, flags=re.DOTALL)