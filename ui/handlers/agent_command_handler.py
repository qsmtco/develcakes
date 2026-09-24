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
from utils.quoting import _parse_quoted_payload

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


def _extract_quoted_commands(text: str, command_names: set[str] | None = None) -> list[ParsedCommand]:
    """Extract A2A commands in quoted-payload format: `cmd @Agent "payload"`
    
    Per A2A_QUOTED_PAYLOAD_SPEC §3:
    - `ask`, `tell`, `delegate` require quoted payloads ("payload")
    - `stop` is payload-free (no quotes needed)
    - Unquoted payloads for cmd requiring quotes are silently skipped (§4.3)
    - Bare `@Agent` at command position → implicit `ask` (§3.1)
    - Inner quotes in payload are parsed until closing " (§3.2)
    
    Returns list of ParsedCommand namedtuples.
    """
    results = []
    seen_spans: set[tuple[int, int]] = set()  # avoid double-matching

    def _emit(cmd: str, agent: str, payload: str, span: tuple[int, int]):
        if span in seen_spans:
            return
        seen_spans.add(span)
        if command_names and cmd not in command_names:
            return
        results.append(ParsedCommand(
            command=cmd,
            agent=agent,
            payload=payload,
            raw_start=span[0],
            raw_end=span[1],
        ))

    # ── Pass 1: explicit commands with closing quote ────────────────────────
    # Pattern: /cmd @Agent "payload" where payload may contain / (file paths)
    # and escaped quotes (\" \\). The closing " must be unescaped.
    # Inner payload: (non-quote/non-backslash) | (backslash + any char)
    for m in re.finditer(
        r'(?:^|\s)/([a-zA-Z]+)\s+(@[^\s"]+)\s+"((?:[^"\\]|\\.)*)"',
        text,
    ):
        cmd = m.group(1).lower()
        agent = m.group(2)
        raw_payload = m.group(3)
        if cmd not in ('ask', 'tell', 'delegate', 'stop', 'compact', 'clear'):
            continue
        if cmd == 'stop':
            _emit(cmd, agent, '', m.span())
            continue
        # Unescape \" and \\ in payload
        try:
            payload, _ = _parse_quoted_payload('"' + raw_payload + '"', 0)
        except Exception:
            payload = None
        if payload is None:
            continue
        if len(payload) > _QUOTED_PAYLOAD_MAX:
            payload = payload[:_QUOTED_PAYLOAD_MAX] + '…'
        _emit(cmd, agent, payload, m.span())

    # ── Pass 2: explicit commands with auto-close (unclosed trailing quote) ──
    # Only matches when no closing " exists before end-of-string. Avoids
    # duplicating matches already found in Pass 1.
    for m in re.finditer(
        r'(?:^|\s)/([a-zA-Z]+)\s+(@[^\s"]+)\s+"([^"]*)$',
        text,
    ):
        if m.span() in seen_spans:
            continue
        cmd = m.group(1).lower()
        agent = m.group(2)
        if cmd not in ('ask', 'tell', 'delegate', 'compact', 'clear'):
            continue
        raw_payload = m.group(3)
        if not raw_payload:
            continue  # bare " → silently drop per spec §4.5
        if len(raw_payload) > _QUOTED_PAYLOAD_MAX:
            raw_payload = raw_payload[:_QUOTED_PAYLOAD_MAX] + '…'
        _emit(cmd, agent, raw_payload, m.span())

    # ── Pass 3: implicit ask /@Agent "payload" (with closing quote) ─────────
    for m in re.finditer(
        r'(?:^|\s)/(@[^\s"]+)\s+"((?:[^"\\]|\\.)*)"',
        text,
    ):
        agent = m.group(1)
        raw_payload = m.group(2)
        try:
            payload, _ = _parse_quoted_payload('"' + raw_payload + '"', 0)
        except Exception:
            payload = None
        if payload is None:
            continue
        if len(payload) > _QUOTED_PAYLOAD_MAX:
            payload = payload[:_QUOTED_PAYLOAD_MAX] + '…'
        _emit('ask', agent, payload, m.span())

    # ── Pass 4: implicit ask /@Agent "payload" (auto-close) ─────────────────
    for m in re.finditer(
        r'(?:^|\s)/(@[^\s"]+)\s+"([^"]*)$',
        text,
    ):
        if m.span() in seen_spans:
            continue
        agent = m.group(1)
        raw_payload = m.group(2)
        if not raw_payload:
            continue
        if len(raw_payload) > _QUOTED_PAYLOAD_MAX:
            raw_payload = raw_payload[:_QUOTED_PAYLOAD_MAX] + '…'
        _emit('ask', agent, raw_payload, m.span())

    # ── Pass 5: payload-free /stop @Agent (no quotes at all) ────────────────
    for m in re.finditer(r'(?:^|\s)/stop\s+(@[^\s"]+?)(?=\s|$)(?!\s*")', text):
        if m.span() in seen_spans:
            continue
        _emit('stop', m.group(1), '', m.span())

    # ── Pass 6: payload-free /compact and /clear (no quotes at all) ────────
    for m in re.finditer(r'(?:^|\s)/(compact|clear)\s+(@[^\s"]+?)(?=\s|$)(?!\s*")', text):
        if m.span() in seen_spans:
            continue
        _emit(m.group(1), m.group(2), '', m.span())

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

        # For audit report processing — see _process_audit_reports()
        self._project_path_provider: Any = None   # Callable[] → str | None
        self._agent_defs_loader: Any = None     # Callable[] → list[dict]
        self._on_audit_report: Any = None         # Callable(dict) → None

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

    def set_on_audit_report(self, callback: Any) -> None:
        """Callback for audit report feed cards. Signature: callback(report_dict) -> None.

        report_dict keys: severity, file_path, task, bug_description, pattern,
        reviewer, target_role, project_path, logged (bool), journal_appended (bool).
        """
        self._on_audit_report = callback

    # ── Core entry point ──────────────────────────────────────────────────────

    def on_agent_response(self, session_key: str, text: str,
                          project_name: str | None) -> None:
        """Called after an agent's final response is rendered.

        Three responsibilities:
        1. AUDIT: Detect and process structured audit reports (SPEC-3).
        2. RELAY: If this agent has a pending ask, relay response to asker.
        3. COMMAND: Scan for slash A2A commands.

        Args:
            session_key: The responding agent's session key
                         (e.g. "special:coder" or "agent:qaster:...")
            text: The agent's full response text
            project_name: Active project name, or None
        """
        if not text:
            return

        # ── Step 0: Process structured audit reports (SPEC-3) ───────────────
        self._process_audit_reports(session_key, text)

        # ── Step 1: Relay answer back to asking agent ────────────────────────


        source_sk = self._pending_asks.pop(session_key, None)
        if source_sk is not None:
            self._relay_response(source_sk, session_key, text, project_name)

        # ── Step 2: Scan for new commands ─────────────────────────────────────

        # Command scanning is only available when command_handler is set
        if not self._command_handler:
            # No command handler — clear depth and return (relay already done above)
            self._chain_depth.pop(session_key, None)
            return

        # Chain depth guard — prevent runaway command chains.
        # Must check BEFORE any command processing to respect the limit.
        depth = self._chain_depth.get(session_key, 0)
        if depth >= _MAX_CHAIN_DEPTH:
            logger.warning(
                "[agent-cmd] Chain depth limit (%d) reached for %s — dropping commands",
                _MAX_CHAIN_DEPTH, session_key
            )
            self._chain_depth.pop(session_key, None)
            return

        # Strip fenced code blocks to avoid false positives
        clean_text = self._strip_fenced_blocks(text)

        # Extract A2A commands in quoted-payload format (A2A_QUOTED_PAYLOAD_SPEC §5.2)
        command_names = self._command_handler.get_command_names() if self._command_handler else None
        parsed_commands = _extract_quoted_commands(clean_text, command_names)

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

                if result.handled and result.forward_to and result.forward_text:
                    self._route_command(result, project_name, depth, source_sk=session_key,
                                        command_name=pc.command)
                    command_count += 1
                elif result.handled and result.broadcast_targets and result.forward_text:
                    for target in result.broadcast_targets:
                        self._route_to_target(
                            target, result.forward_text, project_name
                        )
                    command_count += 1
                elif result.handled and result.response_text:
                    # Action result (e.g. /compact, /clear) — no forward target.
                    # Inject the result into the issuing agent's own conversation
                    # so the supervisor sees the outcome on its next turn.
                    self._record_action_result(session_key, result.response_text)
                    command_count += 1

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
                       command_name: str = "") -> None:
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
            self._pending_asks[record_key] = source_sk

        # Increment chain depth for the target
        self._chain_depth[depth_key] = current_depth + 1

        # Route the message to the target — prefix with asking agent's identity
        # so the target knows who's asking (not the human, but another agent)
        route_key = resolved_sk if is_special else target_sk
        sender_name = self._resolve_display_name(source_sk)
        forward_text = f"[{sender_name} asks]: {result.forward_text}"
        self._route_to_target(route_key, forward_text, project_name)

    def _route_to_target(self, target_sk: str, text: str,
                         project_name: str | None) -> None:
        """Send message to a target agent via the correct transport."""
        # Check if target_sk is already a session key (direct lookup in special_agents).
        # This handles relay calls where source_sk is already a session key like "special:coder".
        if self._agent_runtime_handler is not None and \
           target_sk in self._agent_runtime_handler.get_special_agents():
            self._agent_runtime_handler.send_to_special_agent(target_sk, text)
            return

        # Try resolving display_name → session_key (CommandHandler returns display names)
        resolved_sk = self._resolve_special_agent_sk(target_sk)
        if resolved_sk is not None:
            self._agent_runtime_handler.send_to_special_agent(resolved_sk, text)
            return

        # Local path only (SPEC-05 R1): receiver no-ops with a warning for
        # unregistered/remote keys — that no-op IS the former gateway branch.
        if self._agent_runtime_handler is not None:
            self._agent_runtime_handler.send_to_special_agent(target_sk, text)
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
        clean_text = self._strip_fenced_blocks(text)
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
    def _strip_fenced_blocks(text: str) -> str:
        """Remove fenced code blocks (```...```) from text.

        Prevents false-positive command detection on code examples.
        Handles both ```language\ncode``` and ```inline``` forms.
        """
        return re.sub(r"```.*?```", "", text, flags=re.DOTALL)