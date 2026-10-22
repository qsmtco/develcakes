"""Typed callback protocols for AgentRuntime → UI handler.

The runtime accepts 10 callbacks in its constructor and dispatches them via
its private ``_dispatch()`` helper (see ``agent/runtime.py``). This module
formalizes the contract: the protocol signatures here are the source of
truth, and the handler's ``_on_*`` methods (in
``ui/handlers/agent_runtime_handler.py``) must match them structurally.

Architecturally: ``agent/callbacks.py`` is the boundary between the
runtime's orchestration logic and the UI's render pipeline. Both sides
reference the protocols; neither imports from the other. This is the same
pattern as ``agent/llm/protocol.py`` (the ``LLMProvider`` Protocol that
all provider classes implement).

Layer rules: this module imports only from ``typing``. It MUST NOT import
from ``agent.runtime``, ``ui/``, ``gateway/``, or ``models/`` (per
``docs/ARCHITECTURE.md`` §8.6). It also MUST NOT use
``@runtime_checkable`` — we don't need ``isinstance()`` checks at
runtime; the protocols are documentation, not enforcement.

Keyword contract: every ``__call__`` signature accepts a keyword-only
``_turn_token: object | None = None`` (note the leading underscore). The
runtime's ``_dispatch`` helper at ``agent/runtime.py:420`` passes
callbacks with ``_turn_token=token`` (see production dispatch sites at
``agent/runtime.py:673, 892, 902, 913, 934, 940, 1169, 1257, 1265,
1421, 1434, 1705, 1884``). The leading underscore signals "internal use
only; receivers may ignore it" without making it positional. A previous
draft of this module used ``turn_token`` (no underscore) which broke
``create_autospec`` validation against the real callback signatures —
this is the exact contract drift that BUG #7 in
SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.1 was created to prevent.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol


class OnTextDelta(Protocol):
    """Streaming text chunk callback.

    Fires once per SSE chunk (throttled to ~20 calls/sec in the handler).
    Empty strings are valid (providers may send empty content deltas); the
    turn-start signal is the separate ``OnTurnStart`` callback.

    Args:
        session_key: Conversation session key (e.g. "agent:coder").
        text: The chunk text (may be empty).
        _turn_token: Identity object set by the runtime at ``send_message``
            time. Used by the handler to reject stale cross-turn events.
            May be provided by the runtime's ``_dispatch`` helper on some
            (not all) dispatch paths; receivers must tolerate omission (the
            signature default is ``None``). Because the parameter is after
            ``*``, it is keyword-only — pass it as a keyword argument or
            accept it via ``**kwargs``.
    """

    def __call__(
        self,
        session_key: str,
        text: str,
        *,
        _turn_token: object | None = None,
    ) -> None: ...


class OnTurnStart(Protocol):
    """Turn-start callback.

    Fires once at the top of ``_run_loop`` BEFORE any LLM call or tool
    processing — for every turn, including tool-only turns (which stream
    zero text deltas). Replaces the BUG #21 empty-delta signal (an
    ``on_text_delta`` dispatch with ``""``) that never reached the
    handler's start-bubble logic (``_do_text_delta_inner``'s empty-return
    fired first, so the regression tests shipped failing).

    Args:
        session_key: Conversation session key (e.g. "agent:coder").
        _turn_token: See ``OnTextDelta``.
    """

    def __call__(
        self,
        session_key: str,
        *,
        _turn_token: object | None = None,
    ) -> None: ...


class OnToolCallStart(Protocol):
    """Tool execution start callback.

    Fires AFTER approval (for ``exec_command`` and sensitive
    ``write_file``/``edit_file``), BEFORE the tool actually runs.

    Args:
        session_key: Conversation session key.
        name: Tool name (e.g. "read_file", "exec_command").
        args: Tool arguments dict.
        _turn_token: See ``OnTextDelta``.
    """

    def __call__(
        self,
        session_key: str,
        name: str,
        args: dict[str, Any],
        *,
        _turn_token: object | None = None,
    ) -> None: ...


class OnToolCallResult(Protocol):
    """Tool execution result callback.

    Fires after ``mark_completed`` or ``mark_failed`` in the tool
    execution pipeline.

    Args:
        session_key: Conversation session key.
        name: Tool name.
        result: Either a ``ToolResult`` dataclass or a string (legacy
            callers).
        success: True if the tool succeeded; False if it failed or was
            denied by the approval gate.
        _turn_token: See ``OnTextDelta``.
    """

    def __call__(
        self,
        session_key: str,
        name: str,
        result: Any,
        success: bool = True,
        *,
        _turn_token: object | None = None,
    ) -> None: ...


class OnToolCallApprovalNeeded(Protocol):
    """Approval-needed callback.

    Fires when ``exec_command`` or a sensitive write tool requires PM
    approval BEFORE the tool can run. The runtime blocks the background
    thread on an ``Event`` until ``approve_exec`` (or
    ``reject_exec``) is called from the main thread.

    Args:
        session_key: Conversation session key.
        tool_name: Tool name (always "exec_command" or a write tool).
        args: Tool arguments dict (for exec, contains "command").
        _turn_token: See ``OnTextDelta``.
    """

    def __call__(
        self,
        session_key: str,
        tool_name: str,
        args: dict[str, Any],
        *,
        _turn_token: object | None = None,
    ) -> None: ...


class OnResponseComplete(Protocol):
    """Final response callback.

    Fires when the LLM returns text with no tool calls, or when the tool
    loop completes without producing more tool calls.

    Args:
        session_key: Conversation session key.
        text: Final assistant message text (cumulative; may be empty for
            tool-only turns or empty-content errors).
        _turn_token: See ``OnTextDelta``.
    """

    def __call__(
        self,
        session_key: str,
        text: str,
        *,
        _turn_token: object | None = None,
    ) -> None: ...


class OnTokenUsage(Protocol):
    """Token usage and cost callback.

    Fires after each LLM call (and after each fallback provider call).
    Drives the cost / token display in the activity drawer and the
    compact button's hint.

    Args:
        session_key: Conversation session key.
        total_tokens: Total tokens (prompt + completion) for this call.
        cost: USD cost for this call.
        _turn_token: See ``OnTextDelta``.
    """

    def __call__(
        self,
        session_key: str,
        total_tokens: int,
        cost: float,
        *,
        _turn_token: object | None = None,
    ) -> None: ...


class OnTokenBreakdown(Protocol):
    """Per-iteration token budget breakdown callback.

    Fires before each LLM call (after compaction). Drives the context
    meter in the activity drawer.

    Args:
        session_key: Conversation session key.
        breakdown: Dict with keys: ``system_prompt_tokens``,
            ``conversation_tokens``, ``total_used_tokens``,
            ``model_max_tokens``, ``remaining_tokens``,
            ``usage_percent``, ``trimmed_this_turn``,
            ``messages_remaining``, ``messages_removed_this_turn``,
            and an optional ``compaction_event``.
        _turn_token: See ``OnTextDelta``.
    """

    def __call__(
        self,
        session_key: str,
        breakdown: dict,
        *,
        _turn_token: object | None = None,
    ) -> None: ...


class OnError(Protocol):
    """Error callback.

    Fires for any error condition: missing conversation, cancellation,
    max-iterations, provider errors, exceptions in the loop.

    Args:
        session_key: Conversation session key.
        message: Either a string (user-friendly) or a ``BaseException``
            (raw; the handler translates via ``friendly_error_message``).
        _turn_token: See ``OnTextDelta``.
    """

    def __call__(
        self,
        session_key: str,
        message: str | BaseException,
        *,
        _turn_token: object | None = None,
    ) -> None: ...


class OnEnforcementStatus(Protocol):
    """Post-write enforcement status callback.

    Fires once per enforcement check (syntax / tests / lint) with the
    check result. Drives the audit-report card in the project feed.

    Args:
        session_key: Conversation session key.
        tool_name: Tool that triggered the check (e.g. "write_file").
        status: Dict with keys: ``tier``, ``file``, ``passed``, ``detail``.
        _turn_token: See ``OnTextDelta``.
    """

    def __call__(
        self,
        session_key: str,
        tool_name: str,
        status: dict,
        *,
        _turn_token: object | None = None,
    ) -> None: ...


# Convenience type alias for the full callback bundle.
# Maps runtime attribute name → callable (or None if not registered).
# Used as a structural type for code that wants to pass all 10 callbacks
# as a single dict (e.g. test fixtures, dependency-injection setups).
#
# This is a LOOSE helper type, NOT the callback contract. It does not
# reference the On* Protocols above and does not enforce per-key typing —
# a dict with a typo'd key (e.g. "on_text_dleta") type-checks against
# this alias. The authoritative contract is the individual On* Protocol
# classes; use them directly when you want per-callback type safety.
AgentRuntimeCallbacks = dict[str, Callable | None]
