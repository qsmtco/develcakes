"""Tool middleware chain for AgentRuntime.

Provides composable middleware classes that wrap tool execution with
cross-cutting concerns: post-write enforcement verification and stuck-loop
detection. Middleware is composed in onion order via ToolMiddlewareChain.

Architecture: agent/ layer — imports only from stdlib and agent.tools.
No imports from ui/, gateway/, or agent.runtime.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from agent.tools import ToolResult

logger = logging.getLogger(__name__)


class ToolMiddleware(Protocol):
    """Middleware that wraps tool execution.

    Each middleware receives the tool name, args, execution context,
    and a ``next`` callable. It may:

    - Short-circuit (return a ``ToolResult`` without calling ``next``)
    - Modify args before calling ``next``
    - Modify the result after calling ``next``
    - Raise on error (caught by the chain)

    Middlewares are composed in onion order: the first-registered wraps
    all others.
    """

    def __call__(
        self,
        tool_name: str,
        args: dict,
        context: ToolContext,
        next: Callable[[], ToolResult],
    ) -> ToolResult: ...


@dataclass
class ToolContext:
    """Per-tool-call context shared across the middleware chain.

    Fields:
        session_key: Conversation session key (e.g. ``"agent:coder"``).
        project_path: Absolute path to the project root (sandbox base).
        iteration: Current tool-loop iteration (0-indexed).
        bypass_approval: When True, the approval middleware skips PM
            dispatch (runtime already obtained approval before entering
            the chain).
        audit_log: Optional AuditLog instance for recording tool executions.
        user_id: User identity for audit trail (from AgentConfig).
        enforcement_config: The ``EnforcementConfig`` from ``AgentConfig``,
            or None if enforcement is globally disabled.
        si_enforcement: Per-conversation self-improvement enforcement flag
            (None means default True).
    """
    session_key: str
    project_path: str
    iteration: int
    bypass_approval: bool = False
    audit_log: Any = None
    user_id: str = ""
    enforcement_config: Any = None
    si_enforcement: bool | None = None


class EnforcementMiddleware:
    """Post-execution enforcement check for write tools.

    Calls ``agent.enforcement.check()`` after ``write_file`` / ``edit_file``
    succeeds. Appends the enforcement result to the ``ToolResult`` output
    and dispatches per-check status callbacks.

    No-ops for non-write tools and failed writes.
    """

    def __init__(
        self,
        enforcement_check_fn: Callable,
        on_status: Callable[[str, str, dict], None] | None = None,
    ) -> None:
        """
        Args:
            enforcement_check_fn: ``agent.enforcement.check``.
            on_status: Callback for each check result. Called as
                ``on_status(session_key, tool_name, {"tier": ..., "file": ...,
                "passed": ..., "detail": ...})``. May be None (no dispatch).
        """
        self._check = enforcement_check_fn
        self._on_status = on_status

    def __call__(
        self,
        tool_name: str,
        args: dict,
        ctx: ToolContext,
        next: Callable[[], ToolResult],
    ) -> ToolResult:
        result = next()

        # Only run on successful writes
        if tool_name not in ("write_file", "edit_file"):
            return result
        if not result.success:
            return result

        # Check global + per-agent enforcement flags
        global_enabled = (
            ctx.enforcement_config is not None
            and ctx.enforcement_config.enabled
        )
        agent_enabled = (
            ctx.si_enforcement if ctx.si_enforcement is not None else True
        )
        if not (global_enabled and agent_enabled):
            return result

        try:
            enf_result = self._check(
                tool_name, args, result,
                ctx.project_path,
                ctx.enforcement_config,
            )
            if enf_result.checks:
                suffix = (
                    "\n" + enf_result.appended_message
                    if enf_result.appended_message else ""
                )
                result = dataclasses.replace(
                    result,
                    output=(result.output or "") + suffix,
                    enforcement_checks=list(enf_result.checks),
                )
            elif enf_result.appended_message:
                result = dataclasses.replace(
                    result,
                    output=(result.output or "") + "\n" + enf_result.appended_message,
                )
            if enf_result.appended_message and self._on_status is not None:
                for check_record in enf_result.checks:
                    self._on_status(ctx.session_key, tool_name, {
                        "tier": check_record.tier,
                        "file": check_record.file,
                        "passed": check_record.passed,
                        "detail": check_record.detail,
                    })
        except Exception:
            logger.exception(
                "Enforcement check failed for %s (session=%s):",
                tool_name, ctx.session_key,
            )
            return result

        return result


class StuckDetectionMiddleware:
    """Records tool calls and detects stuck loops.

    Delegates to a stuck-check function (mirrors the runtime's
    ``_check_stuck`` method). If a stuck message is produced, stores it
    in the provided pending-messages dict for the next LLM call.
    """

    def __init__(
        self,
        stuck_check_fn: Callable[[str, str, dict, int], str | None],
        pending_messages: dict[str, list[str]],
    ) -> None:
        """
        Args:
            stuck_check_fn: Callable invoked as
                ``stuck_check_fn(session_key, tool_name, args, iteration)``.
                Returns an intervention message or None.
            pending_messages: Shared dict keyed by session key. Stuck
                messages are appended here for the next LLM call.
        """
        self._check_stuck = stuck_check_fn
        self._pending = pending_messages

    def __call__(
        self,
        tool_name: str,
        args: dict,
        ctx: ToolContext,
        next: Callable[[], ToolResult],
    ) -> ToolResult:
        result = next()
        try:
            stuck_msg = self._check_stuck(
                ctx.session_key, tool_name, args, ctx.iteration,
            )
        except Exception:
            logger.exception(
                "Stuck check failed for %s (session=%s):",
                tool_name, ctx.session_key,
            )
            return result

        if stuck_msg:
            self._pending.setdefault(ctx.session_key, []).append(stuck_msg)
        return result


class ToolMiddlewareChain:
    """Composes middleware into a single callable.

    Middlewares wrap the executor in registration order: ``middlewares[0]``
    is the outermost wrapper. An empty middleware list calls the executor
    directly.
    """

    def __init__(self, middlewares: list[ToolMiddleware]) -> None:
        self._middlewares = middlewares

    def run(
        self,
        tool_name: str,
        args: dict,
        ctx: ToolContext,
        executor: Callable[[], ToolResult],
    ) -> ToolResult:
        """Execute the middleware chain.

        Middlewares wrap each other in registration order:

            middlewares[0] wraps middlewares[1] wraps ... wraps executor.

        Args:
            tool_name: The tool being executed.
            args: Tool arguments dict.
            ctx: Tool execution context.
            executor: The innermost callable (typically ``execute_tool``).

        Returns:
            The ``ToolResult`` from the chain (possibly modified by
            middleware).
        """

        def make_next(index: int) -> Callable[[], ToolResult]:
            if index >= len(self._middlewares):
                return executor
            mw = self._middlewares[index]
            return lambda: mw(tool_name, args, ctx, make_next(index + 1))

        return make_next(0)()
