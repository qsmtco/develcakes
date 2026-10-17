# agent/runtime.py
# AgentRuntime — LLM API client with tool loop.
#
# Manifest:
#   - Reads: config.json, conversations/*.json, project files
#   - Writes: conversations/*.json
#   - Network: LLM API (OpenAI, MiniMax, Anthropic)
#   - No GTK; callbacks are dispatched via GLib.idle_add if GLib is provided
#
# Architecture: this is the core agent loop. It owns conversations, calls LLM APIs,
# executes tools, and manages cost tracking. All GTK/netscape calls go through
# callbacks dispatched to the caller.

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any, Self, TypedDict

if TYPE_CHECKING:
    from models.conversation import Conversation
    from agent.config import LLMProviderConfig

from agent.audit import AuditLog
# Typed callback protocols (Phase 1 — SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION
# §2.1). The handler's `_on_*` methods already satisfy these structurally;
# the type hints in `__init__` document the contract.
from agent.callbacks import (
    OnEnforcementStatus,
    OnError,
    OnResponseComplete,
    OnTextDelta,
    OnTokenBreakdown,
    OnTokenUsage,
    OnToolCallApprovalNeeded,
    OnToolCallResult,
    OnToolCallStart,
    OnTurnStart,
)
from agent.enforcement import check as _enforcement_check
from agent.tool_middleware import (
    EnforcementMiddleware,
    StuckDetectionMiddleware,
    ToolContext,
    ToolMiddlewareChain,
)
from agent.persistence import (
    conversations_dir,
    load_conversation_from_disk,
    resolve_session_workspace,
    save_conversation_to_disk,
)

# ── Streaming call interface (PHASE-FOLLOWUP-1) ──────────────────────────────────

class StreamingCallKwargs(TypedDict, total=False):
    """Single source of truth for `_call_llm_streaming` parameters.

    Both the method signature and the regression test reference this TypedDict.
    If a field is added or removed here, the test will fail until the method
    and all call sites are updated to match.
    """
    session_key: str
    base_url: str
    api_key: str
    model: str
    caller_key: str
    messages: list[dict]
    tools: list[dict] | None
    timeout: float
    x_title: str
    turn_token: object | None
    reasoning_effort: str


# Public API — symbols explicitly exported for external use (PHASE-FOLLOWUP-5)
__all__ = [
    "AgentRuntime",
    "SSEEvent",
    "StreamingCallKwargs",
    # _PROVIDER_CALLERS retained: used by _call_llm's non-streaming
    # dispatch (line ~2125) and by get_valid_callers() for the
    # provider-caller taxonomy. _PROVIDER_STREAMERS removed: dead
    # since Phase B6; consumers migrated to OpenAIProvider(stream=...)/MiniMaxProvider(stream=...)
    # /AnthropicProvider(stream=...) or to get_provider(caller_key).stream.
    # (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.3 Edit J.)
    "_PROVIDER_CALLERS",
    # Turn state machine (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.2 Edit A;
    # added in Phase 2a; full terminal-path routing is Phase 2b).
    "TurnStatus",
    "TurnResult",
    # SPEC-02: single source of truth for the cancel dispatch text — the
    # handler skips the turn-error feed card for this message (a deliberate
    # cancel is not a turn-fatal provider error).
    "CANCEL_MESSAGE",
]

logger = logging.getLogger(__name__)

# ── Turn state machine (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.2 Edit A) ──
# Per-turn state machine. All terminal transitions in _run_loop funnel
# through a single chokepoint function (`_terminate_turn`, Edit C) which
# reads/writes `_turn_state` and `_turn_results` under `_state_lock` (Edit B).
# Phase 2a adds the data structures only; Phase 2b wires the existing
# terminal dispatch sites to the chokepoint.
#
# Invariant: a turn transitions RUNNING → STREAMING → exactly one of
# {COMPLETED, FAILED, CANCELLED}. STREAMING is non-terminal.

# SPEC-02: the exact text `cancel()` dispatches for a deliberate user
# cancellation. A cancel is NOT a turn-fatal provider error (spec §1), so
# the handler's `_do_error` skips the "Turn failed" feed card for this
# message — otherwise stop-all (SPEC-09) would spray one failure card per
# stopped agent. Runtime-internal dispatches MUST emit this constant, never
# a bare literal, so the skip comparison can't drift. Chat-bubble rendering
# of the message is unchanged.
CANCEL_MESSAGE = "Cancelled by user"


class TurnInactive(Exception):
    """This turn must not call the provider.

    ``reason`` is ``"superseded"`` when a newer turn already owns the
    session, or ``"cancelled"`` when this turn's token is still current
    and a cancel epoch is armed. The tool loop catches it before the
    generic failure handler.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _FailureMessage(str):
    """A provider-error message that rides its ProviderFailure through dispatch.

    `TurnResult.error` is a STRING (SPEC-22 fix round JOB 1). When the
    failure was carried in `metadata["provider_failure"]` rather than on a
    raised exception, the UI's `_do_error(sk, message, token)` still needs to
    find it. This str subclass keeps the message's string contract intact
    (`str(message)`, `.lower()`, substring checks all work) while exposing
    `.provider_failure` for the class-line/feed-card branches. The alternative
    — threading metadata through the callback signature — was a larger diff.
    """

    provider_failure: object

    def __new__(cls, message: str, provider_failure: object) -> Self:
        obj = super().__new__(cls, message)
        obj.provider_failure = provider_failure
        return obj


class TurnStatus(Enum):
    """Per-turn state. Transitions are owned by `_terminate_turn`.

    RUNNING: Turn started, no LLM call has been made yet.
    STREAMING: At least one LLM call returned; text deltas or tool
        calls may be in flight. Non-terminal.
    COMPLETED: Terminal success — assistant text dispatched to handler,
        no further LLM calls will be made this turn.
    FAILED: Terminal failure — error dispatched to handler, partial
        state persisted. The conversation remains consistent.
    CANCELLED: Terminal user-initiated cancellation — error dispatched,
        tool history cleaned. The conversation remains consistent.
    """

    RUNNING = "running"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclasses.dataclass
class TurnResult:
    """Everything a terminal callback needs in one struct.

    Built by callers of `_terminate_turn` (Phase 2b) from the runtime's
    per-iteration state. The struct is dispatched to the appropriate
    handler callback (`on_response_complete` for COMPLETED, `on_error`
    for FAILED/CANCELLED) and stored in `_turn_results[(sk, tk)]` so
    tests and the handler can inspect the terminal state after the
    fact.

    Fields:
        status: Terminal status (COMPLETED / FAILED / CANCELLED).
            RUNNING and STREAMING are not valid here; `_terminate_turn`
            rejects them.
        session_key: The session whose turn ended.
        turn_token: Identity object set at `_run_loop` time. Used by
            `_terminate_turn` to reject stale results from a prior turn
            (BUG #4).
        text: Final assistant text (for COMPLETED). Empty for
            FAILED / CANCELLED.
        error: The error that caused termination. None for COMPLETED.
            May be a string (user-friendly) or a `BaseException` (raw,
            for the handler to translate via `friendly_error_message`).
        metadata: Free-form dict. Keys vary by status:
            - COMPLETED: ``{"stream_error": dict|None}``
            - FAILED: ``{"reason": str, "iteration": int, ...}``
            - CANCELLED: ``{"reason": "user"|"shutdown", "iteration": int,
              "persist": bool}``  (``persist`` defaults False; only True
              when the caller explicitly wants the partial state on disk.)
    """

    status: TurnStatus
    session_key: str
    turn_token: object
    text: str = ""
    error: str | BaseException | None = None
    metadata: dict = dataclasses.field(default_factory=dict)


# ── Audit Log (A-4) ──────────────────────────────────────────────────────────

# ── Cost calculation (lives in agent/llm/cost.py, Phase B1; direct import since Phase 4) ──
from agent.llm.cost import cost_for_model

# ── Anthropic converters (extracted to agent/llm/convert.py, Phase B2) ──────

# ── LLM providers (extracted to agent/llm/, Phase B4) ───────────────────────
# Re-exported under legacy names for backward compatibility.
from agent.llm.openai_provider import OpenAIProvider
from agent.llm.minimax_provider import MiniMaxProvider
from agent.llm.anthropic_provider import AnthropicProvider
from agent.llm.registry import get_provider as _get_provider

# ── Provider dispatch (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.3 Edit I) ──
# _call_llm's non-streaming path uses _get_provider(caller_key).call(...).
# _call_llm_streaming uses _get_provider(caller_key).stream.
# The previous bound-method aliases _call_openai / _call_minimax /
# _call_anthropic were preserved for test-patch compatibility but have
# been removed (see Edit J below for the stream-side equivalents). The
# values in _PROVIDER_CALLERS below are now direct lookups into the
# provider registry. Tests in tests/test_agent_runtime.py that
# `from agent.runtime import _call_minimax` / `_call_anthropic` have
# been rewritten to call MiniMaxProvider().call(...) /
# AnthropicProvider().call(...) directly (Edit O).
#
# _PROVIDER_CALLERS is retained for backward compatibility with test
# patches and get_valid_callers(). Do not add new dispatch logic here —
# use agent.llm.registry.get_provider().
# SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.3 Edit I: values are
# direct lookups into the provider registry (not bound-method aliases).
_PROVIDER_CALLERS: dict[str, Any] = {
    "openai": OpenAIProvider("openai").call,
    "minimax": MiniMaxProvider().call,
    "anthropic": AnthropicProvider().call,
    "openrouter": OpenAIProvider("openrouter").call,
    "zai": OpenAIProvider("zai").call,
}


def get_valid_callers() -> frozenset[str]:
    """Return the frozenset of valid caller keys for ProviderConfig.caller.

    Single source of truth for the provider-caller taxonomy. The set is
    derived from _PROVIDER_CALLERS.keys() at module level — adding a new
    adapter to the dict automatically extends the valid set.

    Used by:
      - ui/handlers/settings_handler.py (save-time validation, Test Connection)
      - tests/test_settings_handler.py (regression coverage)
      - tests/test_providers_store.py (verify duplication invariant)

    Layer rule: utils/* cannot import from agent/*, so utils/providers_store.py
    DUPLICATES this set as a module-level constant. The duplication is enforced
    by a regression test in tests/test_providers_store.py
    (TestValidCallersDuplicationInvariant).

    Returns:
        frozenset of strings: {"anthropic", "minimax", "openai",
        "openrouter", "zai"} (alphabetically sorted; order is irrelevant).
    """
    return frozenset(_PROVIDER_CALLERS.keys())


# Response format families — derived from caller key (BUG #1 fix: was
# identity comparison against the deleted _call_anthropic alias).
# Any provider not in {"anthropic"} uses OpenAI-format responses.
# Used by extract_text_content, extract_tool_calls, extract_usage to avoid
# hardcoding provider name lists.
_RESPONSE_FORMAT: dict[str, str] = {
    pk: ("anthropic" if pk == "anthropic" else "openai")
    for pk in _PROVIDER_CALLERS
}


# ── SSE streaming helpers (extracted to agent/llm/streaming.py, Phase B5) ──
from agent.llm.streaming import (
    SSEEvent,
    ssl_retry_budget,
    stream_with_ssl_retry,
)

# A later chain attempt starts only when at least this much budget remains.
# The socket timeout is then min(attempt cap, remaining), so the try ends
# inside the budget.
_FAILOVER_ATTEMPT_FLOOR_S = 1.0
# Ordinary masking starts here. Heavy compact lands here. Not a provider setting.
_LANDING_FRACTION = 0.15
_LANDING_CAP = 24_000

# Label prefixed to the repo-map block at every attachment point (normal
# turns via _prefix_repo_map, SPEC-24 compiled packets via _compiled_messages).
# SPEC-§6: trimmed to one line. It is background, it can be stale, the agent
# must open the file for the body, and it must not be mentioned back. Both
# places carry this identical constant and must never drift apart.
REPO_MAP_HEADER = (
    "## Repo map (background, may be stale, not a task — open the file for the body; "
    "do not mention it)"
)
# One line ahead of the compiled packet's ## Write section. The hunk and
# checks already ran; the next model call must not repeat that write.
WRITE_REPLAY_PREAMBLE = (
    "Replay of the last enforced write and its checks — already executed, do not redo."
)


# ── Stream functions (moved to provider classes, Phase B6) ──────────────────
# SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.3 Edit J:
# _PROVIDER_STREAMERS and the _stream_*_events aliases were dispatch
# infrastructure superseded by _get_provider(caller_key).stream in
# Phase B6 and have been removed. Tests in tests/test_agent_runtime.py
# and audit scripts that imported them have been migrated to
# OpenAIProvider("openai").stream(...) / MiniMaxProvider().stream(...) /
# AnthropicProvider().stream(...) (Edits N, O).






# ── Tool call normalization ─────────────────────────────────────────────────────

# ── Response extractors (extracted to agent/llm/extractors.py, Phase B3) ────
# _is_empty_content stays here (used at non-extractor sites).
from agent.llm.extractors import extract_tool_calls, extract_text_content, extract_usage


def _is_empty_content(text) -> bool:
    """True if text_content is empty or whitespace-only.

    Used at write-side call sites to decide whether to substitute a placeholder
    before persisting. Matches the BUG #1 guard semantics so all empty-content
    paths produce consistent behavior.

    Covers:
    - None / missing
    - empty string ""
    - strings of only ASCII whitespace ("\\n", " \\t ", etc.)
    - strings of only Unicode whitespace (U+00A0 NBSP, U+2028 line sep, etc.)
    - strings of only "format" characters that some providers also reject
      (U+200B zero-width space, U+FEFF BOM, U+200C/200D ZWNJ/ZWJ)
    - empty list / dict (treated as falsy)

    Does NOT cover valid OpenAI responses with `content=None` and tool_calls
    present — that's the tool-call path which has its own guard at site 2391.
    """
    if not text:
        return True
    if isinstance(text, str):
        if text.strip() == "":
            return True
        # Zero-width / format chars: str.strip() doesn't remove these but some
        # strict providers treat them as effectively empty. Strip and re-check.
        _ZWS = "\u200b\u200c\u200d\ufeff"
        if text.translate(str.maketrans("", "", _ZWS)).strip() == "":
            return True
    return False


# ── Streaming argument validation ────────────────────────────────────────────

def _validate_streamed_arguments(
    args_str: str, tool_name: str, session_key: str
) -> bool:
    """Validate that accumulated streaming arguments form complete JSON.

    Returns True if the arguments are valid JSON (or empty — empty is
    allowed, some providers send arguments as a separate frame that may
    not have arrived). Returns False if the arguments are malformed
    (truncated stream), in which case the caller should skip the tool call.

    Logs a warning on failure so truncated streams are observable.
    """
    if not args_str:
        return True  # empty is valid — no arguments fragment arrived yet
    try:
        json.loads(args_str)
        return True
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "[stream] sk=%s skipping tool=%s with incomplete JSON arguments "
            "(stream truncated): %.200r",
            session_key, tool_name, args_str,
        )
        return False


# ── AgentRuntime ──────────────────────────────────────────────────────────────

class AgentRuntime:
    """
    Core agent loop: manages conversations, calls LLM APIs, executes tools.

    Threading model (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.2 Edit H;
    Phase 2a — scaffolding):

      The runtime operates on TWO threads:
        1. Main thread (UI / GTK): calls create_conversation, send_message,
           cancel, approve_exec, get_turn_state, get_last_turn_result.
        2. Background thread per turn: runs _run_loop, _call_llm,
           _call_llm_streaming, tool execution, persistence.

      Synchronized state (under self._lock):
        - _conversations (read in many places, written in create_conversation)
        - _cancelled, _cancel_requested (cancellation signals)
        - _cancelled_epochs (SP3 fix round 2, BUG#2: per-session
          cancellation epochs — the approval-registration guard reads
          THESE, never _cancelled, because the loop's cancellation branch
          discards its _cancelled entry. Armed by cancel() and by
          _terminate_turn's CANCELLED terminal; cleared at the next
          _run_loop start and on COMPLETED/FAILED terminals; never
          touched by the loop's _cancelled.discard bookkeeping.)
        - _active_loops (per-session in-flight marker)
        - _pending_approvals (read in _dispatch_approval, written in
          cancel/approve_exec)
        - _running (lifecycle flag)

      Synchronized state (under self._state_lock, a SEPARATE lock):
        - _turn_tokens: session_key → active turn_token. Written by
          _run_loop at start; read by _terminate_turn and cancel().
        - _turn_state: (session_key, turn_token) → current TurnStatus.
          Written only by _terminate_turn; read by _terminate_turn, the
          STREAMING transition in _run_loop, and the public accessors
          get_turn_state() / get_last_turn_result().
        - _turn_results: (session_key, turn_token) → most recent
          TurnResult. Same access pattern as _turn_state.

      Why a separate _state_lock: the GIL does NOT make the compound
      "read previous state → decide → write terminal state" operation
      atomic. Two threads calling _terminate_turn for the same session
      could both observe a non-terminal state and both dispatch. The
      state lock serializes the compound operation. The lock is NOT
      held during the dispatch callback (which is slow and may invoke
      GLib.idle_add); the lock is only held for the state mutation.

      Per-instance locks (separate from self._lock and _state_lock):
        - _tool_history_lock: protects _tool_history (stuck detection).
        - _compaction_lock: protects _compaction_events (telemetry).

      NOT synchronized (read-mostly, written once at init):
        - _runtimes, _agents, _config, _GLib
        - All callback references (on_text_delta etc.)
        - _pending_stuck_messages: per-session dict; written by _check_stuck
          (background), read by _call_llm (same thread, sequential).
          Cross-turn races are possible if the user hits /clear mid-turn;
          see FIX-CLEAR-ASK-RACE for the active-loop guard that mitigates.

      Known race (BUG #4): a result for a stale turn_token (one that has
      been rotated by a new send_message()) is rejected by _terminate_turn.
      The rejection is logged but the dispatch is NOT made. The handler
      must be prepared for: a turn may dispatch on_error / on_response_complete
      for the OLD token, then a NEW turn's RUNNING state begins, then the
      OLD token's stale result is dropped. This is the desired behavior
      (a previous turn must not abort a current turn), but it is observable
      in tests as: "the dispatched callback for the old turn fires, but
      no TurnResult is recorded for the new turn."

      Phase 2a NOTE: this threading model section is the authoritative
      description of the state-machine synchronization. The accessors
      (get_turn_state, get_last_turn_result) and the RUNNING / STREAMING
      transitions in _run_loop are wired; the actual _terminate_turn
      call sites for the 5+ ad-hoc terminal blocks in _run_loop are
      added in Phase 2b. Until Phase 2b lands, the existing terminal
      dispatch sites continue to fire self._dispatch(self._on_*) +
      self._auto_save directly; the state machine observes the
      transitions but does not own them yet.

    Args:
        config: AgentConfig with provider credentials and limits.
        GLib: Optional GLib module for thread-safe GTK dispatch.
        on_text_delta: (session_key, delta_text) — streaming text delta (Phase 1.3b).
        on_turn_start: (session_key) — fired once at the top of _run_loop, before any LLM call (BUG #21 redesign).
        on_tool_call_start: (session_key, tool_name, args) — tool call started.
        on_tool_call_result: (session_key, tool_name, result) — tool completed.
        on_tool_call_approval_needed: (session_key, tool_name, args) → bool | None — approval needed.
        on_response_complete: (session_key, full_text) — final response ready.
        on_token_usage: (session_key, tokens, cost) — usage info.
        on_token_breakdown: (session_key, breakdown_dict) — §4.15 per-turn token budget breakdown.
            The breakdown dict includes three additional keys when the context-bloat
            fix (BUG #1, Phase CB-1) has shipped:
              - trimmed_this_turn (bool): True if compaction removed messages this iteration.
                False on no-op iterations (where compact() was called but freed nothing).
                When True, "compaction_event" dict is also included with details.
              - messages_remaining (int): post-trim message count
              - messages_removed_this_turn (int): number of messages removed (0 if none)
        on_error: (session_key, error_message) — error occurred.
    """

    _RESPONSE_RESERVE_TOKENS = 4096  # reserve for model output tokens

    def __init__(
        self,
        config: Any,            # AgentConfig — imported lazily to avoid circular
        *,
        GLib=None,
        on_text_delta: OnTextDelta | None = None,
        on_turn_start: OnTurnStart | None = None,
        on_tool_call_start: OnToolCallStart | None = None,
        on_tool_call_result: OnToolCallResult | None = None,
        on_tool_call_approval_needed: OnToolCallApprovalNeeded | None = None,
        on_response_complete: OnResponseComplete | None = None,
        on_token_usage: OnTokenUsage | None = None,
        on_token_breakdown: OnTokenBreakdown | None = None,
        on_error: OnError | None = None,
        on_enforcement_status: OnEnforcementStatus | None = None,
    ):
        self._config = config
        self._GLib = GLib
        self._on_text_delta = on_text_delta
        self._on_turn_start = on_turn_start
        self._on_tool_call_start = on_tool_call_start
        self._on_tool_call_result = on_tool_call_result
        self._on_tool_call_approval_needed = on_tool_call_approval_needed
        self._on_response_complete = on_response_complete
        self._on_token_usage = on_token_usage
        self._on_token_breakdown = on_token_breakdown
        # §2.8: Telemetry — rolling CompactionEvent history (capped at 100) +
        # per-iteration flag for breakdown callbacks. Replaces the old scalar
        # _last_trim_removed field. The _last_trim_removed property below reads
        # the most recent layer==2 event from this history.
        self._compaction_events: list = []
        self._compaction_this_iteration: bool = False
        # Audit-Fix-26 (Bug #3): tracks the session_key of the most recent
        # breakdown dispatch so _last_trim_removed can filter _compaction_events
        # by session. Read+written only inside _run_loop's breakdown block.
        self._last_breakdown_session: str = ""
        self._on_error = on_error
        self._on_enforcement_status = on_enforcement_status
        self._on_provider_failover = None
        self._last_stream_text: dict[str, str] = {}

        # Phase CB-3: per-session list of pending stuck messages to send as
        # transient prefixes on the next LLM call.
        # See SPEC-CONTEXT-BLOAT-PHASE-3.md §2.3 (BUG #4 fix).
        self._pending_stuck_messages: dict[str, list[str]] = {}

        self._running = False
        self._stopped = False

        # conversation_key → Conversation
        self._conversations: dict[str, Any] = {}
        # session_key → pending_approval {tool_name, args, result_event, result_ref}
        self._pending_approvals: dict[str, dict] = {}
        self._cancelled: set[str] = set()  # cancelled session keys
        # SP3 fix round 2 (BUG#2): per-session cancellation epochs. cancel()
        # and the CANCELLED terminal transition ARM these; the NEXT turn's
        # start and COMPLETED/FAILED terminals CLEAR them. _dispatch_approval's
        # registration guard reads THIS, never _cancelled — the loop's own
        # cancellation branch DISCARDS its _cancelled entry (and the global
        # _cancel_requested flag is shared across sessions), so _cancelled
        # bookkeeping cannot survive long enough to gate late registrations.
        # A SET, not a counter: only membership matters (the epoch either
        # gates the session or it does not); monotonicity buys nothing.
        self._cancelled_epochs: set[str] = set()
        self._cancel_requested: bool = False  # immediate cancel signal for running thread
        self._lock = threading.Lock()
        # (_running is initialized at :561, BEFORE the migration call — the
        # BUG#2 fix-round-2 init-order requirement; this line left in place
        # would otherwise be the first assignment.)
        # RACE-FIX v4b: turn token set by the handler before send_message.
        # Captured by _dispatch at call time (background thread, stable per turn).
        # Passed to handler callbacks so they can reject stale cross-turn events.
        self._turn_token: object = object()

        # FIX-CLEAR-ASK-RACE: sessions with an in-flight _run_loop. Used by
        # is_loop_active() and maintained by _run_loop's try/finally.
        self._active_loops: set[str] = set()

        # Turn state machine (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.2
        # Edit B). All four attributes are read and written under
        # `self._state_lock` (a dedicated lock, separate from `self._lock`).
        # The GIL does NOT make the compound
        #   read previous state → decide → write terminal state
        # operation atomic; the lock does (BUG #3).
        #
        # Keying by (session_key, turn_token) tuple (NOT just session_key)
        # lets two turns for the same session coexist briefly during the
        # cancel race without overwriting each other (BUG #4).
        # _terminate_turn rejects results whose token does not match the
        # session's currently active token (stale-result rejection).
        self._state_lock = threading.Lock()
        self._turn_tokens: dict[str, object] = {}
        self._turn_state: dict[tuple[str, object], TurnStatus] = {}
        self._turn_results: dict[tuple[str, object], TurnResult] = {}

        # §E: Stuck detection — per-session tool call history for detecting loops
        # session_key → list[dict{"tool", "args_hash", "iteration"}]
        self._tool_history: dict[str, list[dict]] = {}
        self._tool_history_lock = threading.Lock()
        # session_key → (path, offset, limit) → (mtime_ns, size).
        # Cleared at turn end and on clear_conversation. Same-turn identical
        # reads become a one-line unchanged notice while a live read_file
        # body for that key is still in the transcript. A mask, compact
        # stub, or missing body returns the file body again (SPEC-32).
        self._read_fingerprints: dict[str, dict[tuple, tuple]] = {}
        # Conversation-scoped, unlike _read_fingerprints (cleared every turn).
        # Cleared only by drop_read_cache.
        self._read_counts: dict[str, dict[str, int]] = {}
        self._hot_pins: dict[str, dict[str, tuple[int, int]]] = {}
        self._edited_paths: dict[str, set[str]] = {}
        # session_key → compiled wire packet after an enforced write.
        # The stored transcript is kept; this dict is what the model sees.
        self._compile: dict[str, dict] = {}
        # session_key → rendered repo-map block for the current user goal.
        self._repo_map_block: dict[str, str] = {}
        self._repo_map_key: dict[str, tuple] = {}
        # Bumped after a write_file/edit_file that lands, so the map cache
        # misses and rebuilds against the files just changed. The lock makes
        # the increment atomic across sessions; a lost update would let the
        # next build cache a map that missed one of the writes.
        self._repo_map_lock = threading.Lock()
        self._repo_map_tree_version = 0
        # Audit-Fix-8: Guard _compaction_events against concurrent append+truncate.
        self._compaction_lock = threading.Lock()

        # A-4: Audit log for tool executions
        self._audit_log = AuditLog()

        # Track-A: Tool middleware chain (enforcement + stuck detection).
        # Approval gating stays inline in _run_loop (temporal ordering:
        # must fire before on_tool_call_start). The chain wraps only the
        # execution phase. See spec §A.2.3-§A.2.4.
        self._tool_chain = ToolMiddlewareChain([
            EnforcementMiddleware(
                enforcement_check_fn=_enforcement_check,
                on_status=self._dispatch_enforcement_status,
            ),
            StuckDetectionMiddleware(
                stuck_check_fn=self._check_stuck,
                pending_messages=self._pending_stuck_messages,
            ),
        ])

        # §0: Pluggable context management strategy.
        # DefaultContextStrategy owns the trim/prune/summary algorithm
        # (extracted from the former Conversation trim/summary shims,
        # removed in SPEC-AUDIT-CLEANUP-2 Phase 4). Future: configurable
        # via AgentConfig.context_strategy.
        from agent.context_strategy import DefaultContextStrategy
        self._context_strategy = DefaultContextStrategy()

    # ── Dispatch helpers ───────────────────────────────────────────────────────

    def _dispatch(self, callback: Callable | None, *args: Any, _turn_token: object = None, **kwargs: Any) -> None:
        """Dispatch a callback thread-safely via GLib.idle_add or directly.

        If _turn_token is provided, it is passed as a keyword argument to the
        callback. Callbacks that don't accept it will receive it via **kwargs
        (if they have **kwargs) or ignore it (the handler's _on_* methods all
        accept it explicitly).
        """
        if callback is None:
            return
        # Capture token in local var for closure (already an argument, but
        # explicit for readability).
        token = _turn_token
        def inner():
            try:
                if token is not None:
                    callback(*args, **kwargs, _turn_token=token)
                else:
                    callback(*args, **kwargs)
            except Exception:
                logger.exception("Callback %s raised", callback)
        if self._GLib is not None:
            self._GLib.idle_add(inner)
        else:
            inner()

    def _dispatch_enforcement_status(
        self, session_key: str, tool_name: str, status: dict
    ) -> None:
        """Dispatch a per-check enforcement status to the callback.

        Called by EnforcementMiddleware for each EnforcementCheck result.
        Wraps the existing _dispatch(self._on_enforcement_status, ...) pattern
        that was inline in _run_loop (spec §A.2.3).
        """
        self._dispatch(self._on_enforcement_status, session_key, tool_name, status)

    def _terminate_turn(self, result: TurnResult) -> TurnResult | None:
        """Single terminal transition function for all turn endings.

        Replaces the 5+ ad-hoc patterns of::

            self._dispatch(self._on_response_complete, ..., _turn_token=...)
            self._auto_save(...)
            return

        scattered across `_run_loop`. All terminal paths funnel through here.

        This is the only function that:
          * sets the terminal `TurnStatus` in `_turn_state`,
          * dispatches the appropriate handler callback (on_response_complete
            for COMPLETED, on_error for FAILED / CANCELLED),
          * calls `_auto_save` (for COMPLETED and FAILED; CANCELLED only
            persists when ``result.metadata.get("persist", False)`` is True),
          * cleans up `_tool_history` for FAILED / CANCELLED,
          * records the result in `_turn_results` for later inspection.

        Threading: this method runs on the background thread (`_run_loop`'s
        thread) and may also run on the main thread (from `cancel()` in
        Phase 2b Edit D.7). All state-mutation paths acquire
        `self._state_lock`. All callback dispatches go through `_dispatch`
        which schedules `GLib.idle_add` for the main thread when GLib is
        available; the lock is NOT held during the dispatch (a slow handler
        must not block other state transitions).

        Returns:
            The result if the transition was accepted; ``None`` if the
            result was rejected (invalid status, stale token, or duplicate
            terminal). Callers can use the return value for tests and for
            cancellation dedup — `cancel()` and the background thread may
            both attempt to call `_terminate_turn` for the same turn; only
            the first wins.

        Invariant: at most ONE accepted terminal transition per
        `(session_key, turn_token)` tuple. This function is the only writer
        of terminal state.
        """
        if result.status not in (
            TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED,
        ):
            logger.error(
                "_terminate_turn: invalid status %r (must be terminal); ignoring",
                result.status,
            )
            return None

        sk = result.session_key
        tk = result.turn_token
        state_key = (sk, tk)

        with self._state_lock:
            # Stale-token check (BUG #4): if a new send_message() rotated
            # the active token for this session, this result is from an
            # old turn. Reject it.
            #
            # Membership check (`sk in`) rather than truthiness / `.get()`
            # because a turn_token of None is a valid registered token
            # (Debugger BUG #2: none-sentinel-confusion). `_run_loop`'s
            # default turn_token is None, so a key present with value None
            # must NOT be treated as "no active token."
            if sk in self._turn_tokens:
                active_token = self._turn_tokens[sk]
                if active_token is not tk:
                    logger.error(
                        "_terminate_turn: stale turn_token for %s "
                        "(active=%r, result=%r); result rejected",
                        sk, active_token, tk,
                    )
                    return None

            # Duplicate-terminal check (BUG #3, #4): if a terminal state
            # already exists for this (sk, tk), this is a duplicate
            # transition. Reject it.
            prev = self._turn_state.get(state_key)
            if prev in (
                TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED,
            ):
                logger.error(
                    "_terminate_turn: duplicate terminal transition for %s "
                    "(prev=%s, new=%s); second call ignored",
                    sk, prev, result.status,
                )
                return None

            # Accepted transition — record state + result under the lock.
            # Also record the active token for this session (BUG #4): if no
            # token was previously set (e.g. _terminate_turn is called
            # before _run_loop's RUNNING init — as in tests, or in a
            # direct external call), the accessors get_turn_state() and
            # get_last_turn_result() need to know which token is the
            # "active" one. Without this, get_turn_state(sk) would
            # return None even after a successful transition.
            self._turn_state[state_key] = result.status
            self._turn_results[state_key] = result
            if sk not in self._turn_tokens:
                self._turn_tokens[sk] = tk
            # SP3 fix round 2 (BUG#2): epoch maintenance at the terminal
            # transition, under _state_lock ONLY (this method must never
            # take _lock — one caller already holds it). A CANCELLED
            # terminal ARMS the epoch: it is the only durable trace of the
            # cancellation (the loop's own branch discards _cancelled, and
            # cancel() may have run while this transition's result was in
            # flight) and it must keep gating late approval registrations.
            # COMPLETED/FAILED terminals and the next turn's start CLEAR
            # the epoch — a session that ran a turn to completion accepts
            # approvals again (no permanent poisoning by the fix itself).
            if result.status == TurnStatus.CANCELLED:
                self._cancelled_epochs.add(sk)
            else:
                self._cancelled_epochs.discard(sk)

        # Dispatch the appropriate callback. This happens OUTSIDE the
        # state lock so a slow handler does not block other state
        # transitions on the same session.
        if result.status == TurnStatus.COMPLETED:
            self._dispatch(
                self._on_response_complete, sk, result.text,
                _turn_token=result.turn_token,
            )
        else:  # FAILED or CANCELLED
            err_msg = result.error
            if err_msg is None:
                err_msg = "Turn ended without error message"
            # SPEC-22 fix round JOB 1: read the failure from metadata FIRST
            # (the stream/empty-content carrier), then fall back to the
            # exception carrier the narrow handlers attach (`exc._failure`).
            # `TurnResult.error` stays a STRING — the metadata path never
            # stuffs an exception into it. Record happens ONCE here.
            provider_failure = None
            if isinstance(result.metadata, dict):
                provider_failure = result.metadata.get("provider_failure")
            if provider_failure is None:
                provider_failure = getattr(err_msg, "_failure", None)
            if result.status == TurnStatus.FAILED:
                self._record_attached_failure(provider_failure)
                # JOB 1 carrier bridge: `_do_error` receives `err_msg` (a
                # string) and metadata does not reach it. When a failure rode
                # in metadata, hand it off on a small str subclass so the UI
                # path can read `.provider_failure` without changing the
                # callback signature. The smaller-diff choice (documented):
                # subclass over threading metadata into `_do_error`.
                if (
                    provider_failure is not None
                    and not isinstance(err_msg, BaseException)
                    and not isinstance(err_msg, _FailureMessage)
                ):
                    err_msg = _FailureMessage(str(err_msg), provider_failure)
            self._dispatch(
                self._on_error, sk, err_msg,
                _turn_token=result.turn_token,
            )

        # Persist (except for CANCELLED unless explicitly requested).
        # FAILED always persists (partial state is better than lost state).
        # COMPLETED always persists (next turn must see the assistant message).
        # CANCELLED persists only if metadata["persist"] is True.
        should_persist = (
            result.status in (TurnStatus.COMPLETED, TurnStatus.FAILED)
            or result.metadata.get("persist", False)
        )
        if should_persist:
            try:
                conv = self._conversations.get(sk)
                if conv is not None:
                    self._auto_save(sk, conv)
            except Exception:
                logger.exception(
                    "_terminate_turn: auto_save failed for %s (status=%s)",
                    sk, result.status,
                )

        # Bug 3 (SPEC-AUDIT-CLEANUP-1): auto-flush the audit log on EVERY
        # terminal outcome — deliberately OUTSIDE `if should_persist:` and
        # outside the state lock, so CANCELLED turns flush too and a slow
        # flush never blocks other state transitions. An audit flush must
        # never break the turn, hence the blanket except.
        try:
            self._audit_log.flush_audit_log()
        except Exception:
            logger.exception("_terminate_turn: audit flush failed for %s", sk)

        # Clean up stuck-detection history on terminal transitions.
        # Previously only `cancel()` did this; moved here so FAILED and
        # COMPLETED also reset the detector for the next turn.
        if result.status in (TurnStatus.FAILED, TurnStatus.CANCELLED):
            self._cleanup_tool_history(sk)

        logger.debug(
            "_terminate_turn: sk=%s tk=%r status=%s text_len=%d has_error=%s",
            sk, tk, result.status.value, len(result.text or ""),
            result.error is not None,
        )
        return result

    def _attach_provider_failure(
        self,
        exc: BaseException,
        session_key: str,
        stage: str,
    ) -> object | None:
        """Classify once and stash the result on the exception. Does not record.

        The narrow provider-call handlers attach every exception they catch
        (that boundary is a provider call). The generic turn handler attaches
        only when `is_provider_exception` is true, so a tool-loop KeyError is
        not reported as a provider failure. Recording happens once, in
        `_terminate_turn`.
        """
        existing = getattr(exc, "_failure", None)
        if existing is not None:
            return existing
        from agent.llm.failure_classifier import (
            ProviderFailure,
            classify_exception,
            is_provider_exception,
            sanitize_detail,
            status_code_of,
        )
        if stage != "request_boundary" and not is_provider_exception(exc):
            return None
        ctx = getattr(exc, "_crabcakes_context", None)
        ctx = ctx if isinstance(ctx, dict) else {}
        provider = str(ctx.get("provider") or "")
        model = str(ctx.get("model") or "")
        if not provider or not model:
            conv = self._conversations.get(session_key)
            conv_model = getattr(conv, "model", "") or ""
            if not model:
                model = conv_model
            if not provider and "/" in conv_model:
                provider = conv_model.split("/", 1)[0]
        failure = ProviderFailure(
            session_key=session_key,
            provider=provider,
            model=model,
            failure_class=classify_exception(exc, provider, model),
            status_code=status_code_of(exc),
            detail=sanitize_detail(exc),
            stage="request" if stage == "request_boundary" else stage,
            attempt=1,
            timestamp=time.time(),
        )
        try:
            exc._failure = failure  # type: ignore[attr-defined]
        except Exception:
            logger.exception("could not attach provider failure for %s", session_key)
            return None
        return failure

    def _classified_stream_error(
        self,
        session_key: str,
        stream_err: object | None,
        stage: str,
    ):
        """Build the ProviderFailure for a stream/empty-content failure.

        SPEC-22 §2.2: this returns the failure object (or None) so the caller
        stashes it in `TurnResult.metadata["provider_failure"]`. It must NOT be
        routed through `TurnResult.error` — that field's contract is a STRING
        (see the fix-round brief JOB 1). `ClassifiedProviderError` remains for
        the raised-exception carrier used by `_stream_failure_exception`, which
        is a genuinely different path (a BaseException for the chat translator).
        """
        from agent.llm.failure_classifier import failure_for_stream
        conv = self._conversations.get(session_key)
        conv_model = getattr(conv, "model", "") or ""
        provider = conv_model.split("/", 1)[0] if "/" in conv_model else ""
        return failure_for_stream(
            session_key=session_key,
            provider=provider,
            model=conv_model,
            stream_err=stream_err,
            stage=stage,
        )

    def _record_attached_failure(self, failure: object) -> None:
        """Record one ProviderFailure. Accepts a failure OR a carrier object.

        Callers pass either the resolved ProviderFailure (the metadata-first
        path in `_terminate_turn`) or a carrier that holds one under
        `._failure` (the raised-exception / ClassifiedProviderError path).
        Normalizing here keeps every call site correct without each having to
        know which shape it holds.
        """
        if failure is None:
            return
        from agent.llm.failure_classifier import ProviderFailure
        if not isinstance(failure, ProviderFailure):
            failure = getattr(failure, "_failure", None)
            if not isinstance(failure, ProviderFailure):
                return
        try:
            from utils.provider_failure_store import record
            record(failure)
        except Exception:
            logger.exception("provider failure record failed")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the runtime. Loads saved conversations from disk."""
        self._running = True
        self._stopped = False
        logger.info("AgentRuntime started")

    def stop(self) -> None:
        """Stop the runtime. Saves all conversations."""
        with self._lock:
            self._stopped = True
            self._running = False
            for sk, conv in list(self._conversations.items()):
                try:
                    save_conversation_to_disk(conv, sk)
                except Exception:
                    logger.exception("Failed to save conversation %s", sk)
        logger.info("AgentRuntime stopped")

    def is_running(self) -> bool:
        return self._running

    # ── Conversation management ─────────────────────────────────────────────────

    def create_conversation(
        self,
        agent_name: str,
        session_key: str,
        project_path: str | None = None,
        model: str | None = None,
        allowed_tools: list[str] | None = None,  # NEW
        mcp_servers: list[str] = None,  # NEW: Phase B MCP servers
        agent_role: str = "",
        si_enforcement: bool | None = None,      # per-agent enforcement override
        api_key: str | None = None,             # per-agent API key override
        app_title: str = "",                    # app identifier (e.g. "Coder:develcakes")
        fallback_provider: str | None = None,
        fallback_model: str | None = None,
        defer_prompt_build: bool = False,        # NEW: build system prompt on background thread
    ) -> str:
        """
        Create a new conversation for an agent.

        Returns the session_key (same as the argument).

        Args:
            allowed_tools: If provided, only these tool names are available to
                          the agent. If None, all tools are available.
            mcp_servers: List of MCP server names to connect for this conversation.
            si_enforcement: If True/False, overrides global enforcement for this
                           agent. If None, uses global config.
            app_title: App identifier — flows from gateway displayName into
                      the conversation so agents know the source application.
        """
        # Phase B BUG #22: Clean up existing MCP connections before replacing conversation
        if session_key in self._conversations:
            try:
                from utils.mcp_client import disconnect_all
                disconnect_all(session_key)  # Clean up MCP for this conversation
            except Exception as e:
                # Phase 9: log instead of silently passing. MCP cleanup is
                # best-effort during conversation replacement, but a failure
                # may indicate resource leaks that warrant investigation.
                logger.debug("MCP best-effort cleanup failed for %s: %s", session_key, e)

        from agent.context import build_system_prompt
        from models.conversation import Conversation

        if model is None:
            model = self._config.default_model

        # SPEC-38: construct the conversation first, then resolve the budget
        # window FROM IT via the shared resolver — the conversation's provider
        # (model prefix), never a raw max_tokens off the default provider.
        conv = Conversation(
            agent_name=agent_name,
            agent_role=agent_role,
            project_path=project_path,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers if mcp_servers else [],
            model=model,
            system_prompt="",
            si_enforcement=si_enforcement,
            api_key=api_key,
            app_title=app_title,
            fallback_provider=fallback_provider or self._config.fallback_provider,
            fallback_model=fallback_model or self._config.fallback_model,
        )
        model_max_for_budget, context_mode = self._budget_window_and_mode(conv)
        tool_names = self._prompt_tool_names(conv)

        if defer_prompt_build:
            system_prompt = ""
        else:
            system_prompt = build_system_prompt(
                agent_name, project_path, tool_names,
                review_mode="off",
                agent_role=agent_role,
                model_max_tokens=model_max_for_budget,
                context_mode=context_mode,
            )
        # Rebuilt at the start of a user turn when the prompt inputs change
        # (_rebuild_system_prompt_if_inputs_changed). Not inside the tool loop.
        conv.system_prompt = system_prompt
        if system_prompt:
            conv._system_prompt_fingerprint = self._system_prompt_fingerprint(conv)

        with self._lock:
            self._conversations[session_key] = conv

        logger.info("Created conversation %s for agent %s", session_key, agent_name)
        return session_key

    def _ensure_system_prompt(self, session_key: str) -> None:
        """Ensure the conversation has a system prompt built.

        Called from _run_loop at the start, before any LLM call.
        If the conversation already has a system prompt, this is a no-op.

        Thread safety: uses double-checked-locking with an identity check
        on the conv object. Fast path: check without lock (common case —
        prompt already built or deferred-build not enabled). Slow path:
        re-fetch under self._lock with identity check to ensure we only
        write to the same object we checked (prevents TOCTOU race with
        clear_conversation or load_conversation replacing the conversation).
        """
        # Fast path: check without lock (common case)
        conv = self._conversations.get(session_key)
        if conv is None or conv.system_prompt:
            return

        from agent.context import build_system_prompt

        # Same arguments create_conversation uses. Fingerprint is taken
        # after the build so the turn-start check sees the same inputs and
        # does not build a second time.
        model_max_for_budget, context_mode = self._budget_window_and_mode(conv)
        tool_names = self._prompt_tool_names(conv)

        # build_system_prompt is pure file I/O — safe outside the lock
        new_prompt = build_system_prompt(
            conv.agent_name,
            conv.project_path,
            tool_names,
            review_mode="off",
            agent_role=conv.agent_role or "",
            model_max_tokens=model_max_for_budget,
            context_mode=context_mode,
        )
        fingerprint = self._system_prompt_fingerprint(conv)

        # Slow path: acquire lock, re-fetch conv, identity check, write
        with self._lock:
            conv_now = self._conversations.get(session_key)
            if conv_now is conv and not conv_now.system_prompt:
                conv_now.system_prompt = new_prompt
                conv_now._system_prompt_fingerprint = fingerprint
                conv_now._token_estimate_cache = None
                logger.info("System prompt built for %s in background thread (len=%d)",
                            session_key, len(new_prompt))

    def _prompt_tool_names(self, conv: "Conversation") -> list[str]:
        """Tool names create_conversation passes to build_system_prompt.

        ``allowed_tools is None`` means every registered tool. A list is
        filtered in registry order.
        """
        from agent.tools import _API_SCHEMA_HIDDEN, get_all_tools
        # C1: the prompt TOOL_LIST must show exactly what the API schema
        # offers. Schema-hidden tools (file_search) stay executable via the
        # allowed_tools permit (B1 replay) but must not be advertised here.
        all_tools = [t for t in get_all_tools() if t.name not in _API_SCHEMA_HIDDEN]
        if conv.allowed_tools is None:
            return [t.name for t in all_tools]
        allowed = conv.allowed_tools
        return [t.name for t in all_tools if t.name in allowed]

    def _system_prompt_fingerprint(self, conv: "Conversation") -> tuple:
        """Inputs that change the system prompt.

        Project path, resolved context mode, budget window, role, tool
        names, review mode, the awareness cache token (``.crabcakes`` mtime
        plus content hash), and the file-context slot: the project-root
        mtime plus the tree fingerprint (file counts per top-level
        directory), so add/remove inside subdirectories — invisible to the
        root-only stat — still rebuild the prompt (audit BUG #1).
        """
        from agent.context import (
            _project_root_mtime,
            _project_tree_fingerprint,
            resolve_context_mode,
        )
        from utils.project_awareness import (
            _awareness_dir_mtime,
            awareness_content_fingerprint,
        )

        model_max, raw_mode = self._budget_window_and_mode(conv)
        resolved = resolve_context_mode(raw_mode, model_max)
        project = conv.project_path or ""
        if project and os.path.isdir(project):
            awareness = (
                _awareness_dir_mtime(project),
                awareness_content_fingerprint(project),
            )
            # Root mtime catches content edits to the fixed files; the tree
            # fingerprint catches add/remove inside subdirectories, which a
            # root-only stat never sees (audit BUG #1, bugfix B2).
            file_slot = (_project_root_mtime(project), _project_tree_fingerprint(project))
        else:
            awareness = (0.0, "")
            file_slot = 0.0
        return (
            project,
            resolved,
            model_max,
            conv.agent_role or "",
            tuple(self._prompt_tool_names(conv)),
            "off",
            awareness,
            file_slot,
        )

    def _rebuild_system_prompt_if_inputs_changed(self, session_key: str) -> None:
        """Rebuild once, at the start of a user turn, when inputs changed.

        A matching fingerprint skips ``build_system_prompt``. A failed build
        keeps the previous string and the previous fingerprint. Callers must
        not invoke this inside the tool loop.
        """
        conv = self.get_conversation(session_key)
        if conv is None:
            return
        fingerprint = self._system_prompt_fingerprint(conv)
        if conv.system_prompt and fingerprint == conv._system_prompt_fingerprint:
            return
        previous_prompt = conv.system_prompt
        previous_fp = conv._system_prompt_fingerprint
        try:
            from agent.context import build_system_prompt
            model_max, context_mode = self._budget_window_and_mode(conv)
            new_prompt = build_system_prompt(
                conv.agent_name,
                conv.project_path,
                self._prompt_tool_names(conv),
                review_mode="off",
                agent_role=conv.agent_role or "",
                model_max_tokens=model_max,
                context_mode=context_mode,
            )
        except Exception:
            logger.exception(
                "Failed to rebuild system prompt for %s; keeping previous prompt",
                session_key,
            )
            conv.system_prompt = previous_prompt
            conv._system_prompt_fingerprint = previous_fp
            return
        conv.system_prompt = new_prompt
        conv._system_prompt_fingerprint = fingerprint
        conv._token_estimate_cache = None

    def get_conversation(self, session_key: str) -> Any | None:  # Conversation | None
        """Get a conversation by session key."""
        return self._conversations.get(session_key)

    def send_message(self, session_key: str, text: str, prepare: Callable[[], None] | None = None) -> None:
        """
        Send a user message. Runs the tool loop in a background thread.

        Loop:
        0. Run the optional `prepare` callback (handler-supplied turn
           preparation) — Phase 4 Part B: this runs on the loop thread so
           the GTK main thread never blocks on conversation disk I/O.
        1. Append user message
        2. Build API messages (system + history)
        3. Call LLM API
        4. If tool calls: execute (with approval gating for exec_command)
        5. If text: fire on_response_complete
        6. Check cost_limit / step_limit

        Args:
            prepare: Optional zero-argument callable run on the loop thread
                BEFORE the conversation lookup. It is expected to load or
                create the conversation when one does not exist yet. An
                exception it raises terminates the turn as FAILED with
                metadata reason "prepare_failed".

        Option C+ lazy reconciliation: the runtime does not know the active
        project. The handler (AgentRuntimeHandler.send_to_special_agent)
        supplies `prepare`, which calls _rebuild_conversation_context BEFORE
        the loop body reads the conversation. If the handler is bypassed
        (tests, future callers), the conversation may have a stale
        project_path. In that case the FIRST call from a cold context fires
        _rebuild_conversation_context here — the short-circuit in that
        method makes this O(1) when already in sync.
        """
        t = threading.Thread(
            target=self._run_loop,
            args=(session_key, text, self._turn_token, prepare),
            daemon=True,
        )
        t.start()

    def cancel(self, session_key: str) -> None:
        """Cancel an in-progress conversation.

        Signals the running thread and dispatches a user-facing cancellation
        message. Uses the active turn token from ``_turn_tokens[session_key]``
        (not the runtime's global ``_turn_token`` which may be stale). The
        background thread's ``_run_loop`` will call ``_terminate_turn(CANCELLED)``
        from its cancellation check; the dispatch here is the UX path so the
        user sees the message immediately. ``_terminate_turn``'s dedup ensures
        only one terminal transition is recorded.

        Audit fix (BUG #13): the previous version dispatched with
        ``_turn_token=self._turn_token`` (the runtime's global, possibly
        stale) which would cause the handler to receive a token for the
        wrong turn and silently drop the message. The fix reads the
        session-scoped active token under ``_state_lock``.
        """
        with self._lock:
            # Mark as cancelled so _run_loop's check will catch it
            self._cancelled.add(session_key)
            # SP3 fix round 2 (BUG#2): arm the cancellation epoch. This —
            # with the loop's registration-time arm on the CANCELLED
            # terminal — is what the approval registration guard reads;
            # _cancelled alone is too fragile (the loop's cancellation
            # branch DISCARDS its entry; _cancel_requested is a shared
            # global). Cleared at the next turn's start and on
            # COMPLETED/FAILED terminals; never cleared by the loop's
            # _cancelled.discard bookkeeping.
            self._cancelled_epochs.add(session_key)
            # Signal the running thread to break out of the loop immediately
            self._cancel_requested = True
            for sk in list(self._pending_approvals):
                if sk.startswith(session_key):
                    ev = self._pending_approvals[sk]["event"]
                    self._pending_approvals[sk]["result"] = None
                    ev.set()
                    # SP3 fix round 2 (BUG#2c): POP the entry we just
                    # denied. The old set-only flush left the entry behind
                    # — a stale phantom approve_exec would resolve FIRST
                    # (the hijack: a PM click resolved the phantom while
                    # the live waiter hung out its 60s) and stop_all's
                    # denied count read dead entries (zrate's false 12/12
                    # on the PM's card). The waiter's result_ref already
                    # carries the denial; the dict need not.
                    self._pending_approvals.pop(sk, None)
            logger.info("Cancelled session %s (UX dispatch follows)", session_key)
        # §E: Clean up stuck-detection history when conversation ends.
        # _terminate_turn will also call _cleanup_tool_history (idempotent),
        # but doing it here ensures cleanup even if the background thread
        # is wedged.
        self._cleanup_tool_history(session_key)
        # Dispatch the user-facing cancellation message using the ACTIVE
        # token for this session (BUG #13: not the runtime's global token).
        # Done OUTSIDE _lock so a slow handler doesn't block other state
        # mutations on the same session.
        with self._state_lock:
            active_tk = self._turn_tokens.get(session_key, self._turn_token)
        self._dispatch(
            self._on_error, session_key, CANCEL_MESSAGE,
            _turn_token=active_tk,
        )

    def stop_all(self) -> dict[str, str]:
        """Cancel every in-flight turn + kill every registered process. sk -> outcome.

        SPEC-09 SP3 stop-all core. Per session: cancel() FIRST (the existing
        per-session machinery — cancelled-set + _cancel_requested + the
        approval deny-flush that unblocks _dispatch_approval waiters as
        DENIED), THEN tools.cancel_all_processes(session_key) so a killed
        process returns into a turn already marked cancelled (no zombie
        dispatch: _terminate_turn's duplicate-terminal dedup absorbs the
        late result).

        Outcomes: "no_turn" (no in-flight loop), "cancelled" (cancel()
        dispatched; process kill result appended), "cancelled+killed:N",
        or "cancelled+killed:N+M-unkillable" (SP3 fix round BUG#3: a group
        that SURVIVED SIGKILL — hostile-process territory, must be visible).
        Runs synchronously; the killed groups' SIGTERM→2s→SIGKILL escalation
        caps this method at roughly 4s per live process group.
        """
        with self._lock:
            session_keys = list(self._active_loops)
        from agent.tools import cancel_all_processes  # lazy, matches :1789 idiom
        outcomes: dict[str, str] = {}
        for sk in session_keys:
            # Approvals deny-flushed by cancel() = matching pending keys at
            # cancel time (cancel sets result=None + event and POPS — SP3
            # fix round 2 (BUG#2c): the old no-pop flush left stale phantoms
            # that approve_exec resolved FIRST (the hijack) and that the
            # denied count read as real; count BEFORE — the same startswith
            # prefix semantics cancel itself uses).
            with self._lock:
                denied = sum(1 for k in self._pending_approvals if k.startswith(sk))
            self.cancel(sk)
            killed, unkillable = cancel_all_processes(sk)
            parts = ["cancelled"]
            if denied:
                parts.append(f"denied:{denied}")
            if killed:
                parts.append(f"killed:{killed}")
            if unkillable:
                parts.append(f"{unkillable}-unkillable")
            outcomes[sk] = "+".join(parts)
        # ALL sessions (this runtime's scope) — a process can outlive its
        # loop's registry visibility only via races; sweep the rest too.
        killed_other, unkillable_other = cancel_all_processes(None)
        if killed_other or unkillable_other:
            parts = []
            if killed_other:
                parts.append(f"killed:{killed_other}")
            if unkillable_other:
                parts.append(f"{unkillable_other}-unkillable")
            outcomes["*"] = "+".join(parts)
        # Final deny-flush: approvals registered after their session's
        # cancel ran (register/cancel race) or orphaned from loops already
        # gone. cancel() only flushes sessions it cancels; any waiter left
        # behind here would hang out its full 60s — the exact hang
        # stop-all exists to prevent. Same mechanics as cancel()'s flush,
        # targeted at the ACTUAL waiter state (result_ref, which
        # _dispatch_approval reads). SP3 fix round 2 (BUG#2c): entries are
        # POPPED, not left — a stale phantom is the approve_exec hijack
        # (PM click resolves the phantom; the live waiter hangs 60s) and
        # poisons every later denied count.
        with self._lock:
            for key in list(self._pending_approvals):
                pending = self._pending_approvals[key]
                pending["result_ref"][0] = None
                pending["event"].set()
                self._pending_approvals.pop(key, None)
        return outcomes

    # ── Tool loop ─────────────────────────────────────────────────────────────

    def _budget_window_and_mode(self, conv: "Conversation") -> tuple[int, str]:
        """SPEC-38: one resolution path for the system-prompt budget window.

        Returns (model_max_tokens, context_mode) where the window comes from
        the shared _compute_model_max resolver for THIS conversation's provider
        (model prefix; default provider only as fallback). Every system-prompt
        build site — create_conversation, _ensure_system_prompt,
        _rebuild_conversation_context — must derive its budget here, never
        from a raw max_tokens read off the default provider.
        """
        provider_name = (
            conv.model.split("/")[0]
            if conv.model and "/" in conv.model
            else self._config.default_provider
        )
        provider_cfg = (
            self._config.providers.get(provider_name) if provider_name else None
        )
        context_mode = getattr(provider_cfg, "context_mode", "auto") or "auto"
        return self._compute_model_max(conv), context_mode

    def _compute_model_max(self, conv: "Conversation") -> int:
        """Return the model's context window for the current conversation's provider.

        Resolution order:
          1. provider context_window when set and > 0 (uncapped opt-in)
          2. provider max_tokens when > 0 (uncapped)
          3. caller_default_max_tokens(provider.caller) — uncapped
             (e.g. MiniMax = 1_048_576, Anthropic = 200_000)
          4. 128_000 global fallback

        Returns 128_000 when:
          - conv.model is None and self._config.default_provider is not configured
          - the resolved provider config has no positive window AND the
            caller is not in CALLER_DEFAULT_MAX_TOKENS
          - any exception during provider lookup
        """
        from models.providers import caller_default_max_tokens
        FALLBACK = 128_000
        try:
            provider_name = (
                conv.model.split("/")[0]
                if conv.model and "/" in conv.model
                else self._config.default_provider
            )
            if not provider_name:
                return FALLBACK
            provider_cfg = self._config.providers.get(provider_name)
            if provider_cfg is None:
                return FALLBACK
            window = getattr(provider_cfg, "context_window", 0)
            if isinstance(window, bool) or not isinstance(window, int):
                window = 0
            if window > 0:
                return window
            max_tokens = getattr(provider_cfg, "max_tokens", None)
            if max_tokens and not isinstance(max_tokens, bool) and int(max_tokens) > 0:
                return int(max_tokens)
            # BUG #3 fix: use caller-specific default before falling back to 128K.
            # The provider config exists but its max_tokens is unset — use the
            # per-caller static table to avoid wasting 87% of MiniMax-M3's
            # 1M context window (or 50% of Claude's 200K).
            return caller_default_max_tokens(getattr(provider_cfg, "caller", ""))
        except Exception:
            logger.exception("[model-max] failed to resolve provider max_tokens; using fallback")
            return FALLBACK

    def _compute_compaction_threshold(self, conv: "Conversation") -> tuple[int, int]:
        """Return (soft_ceiling, hard_ceiling) tuple for the conversation's provider.

        Resolution order for the threshold fraction:
          1. conv.model's provider's compaction_threshold (when set and in (0, 1])
          2. 0.80 default

        Returns:
            tuple[int, int]: (soft_ceiling, hard_ceiling) where:
                - soft_ceiling = int(hard_ceiling * threshold) — compaction trigger point
                - hard_ceiling = _compute_model_max(conv) — provider's max_tokens or 128_000 fallback

        Fallback: (102_400, 128_000) when provider resolution fails.
        """
        DEFAULT_THRESHOLD = 0.80
        try:
            provider_name = (
                conv.model.split("/")[0]
                if conv.model and "/" in conv.model
                else self._config.default_provider
            )
            threshold = DEFAULT_THRESHOLD
            if provider_name:
                provider_cfg = self._config.providers.get(provider_name)
                if provider_cfg is not None:
                    cfg_threshold = getattr(provider_cfg, "compaction_threshold", None)
                    if cfg_threshold is not None and 0 < cfg_threshold <= 1:
                        threshold = float(cfg_threshold)
        except Exception as e:
            # Defensive coding should not hide programming errors. The default
            # 0.80 is used as fallback. Operators can enable DEBUG logging to
            # see the underlying cause. A misconfigured provider shouldn't
            # crash compaction, but it shouldn't be silently invisible either.
            logger.debug(
                "_compute_compaction_threshold: failed to resolve per-provider "
                "threshold, using default %s. Error: %s",
                DEFAULT_THRESHOLD,
                e,
            )
        hard_ceiling = self._compute_model_max(conv)
        soft_ceiling = int(hard_ceiling * threshold)
        return (soft_ceiling, hard_ceiling)

    def _compute_landing_goal(self, hard_ceiling: int) -> int:
        """Token budget ordinary masking starts at, and heavy compact lands at.

        ``min(15% of the real window, 24_000)``. Not the provider threshold.
        """
        if not isinstance(hard_ceiling, int) or isinstance(hard_ceiling, bool):
            return 0
        if hard_ceiling <= 0:
            return 0
        return min(int(hard_ceiling * _LANDING_FRACTION), _LANDING_CAP)

    @property
    def _last_trim_removed(self) -> int:
        """Backward-compat accessor: count from latest trim-layer event.

        Derived from _compaction_events so existing read sites (breakdown
        callback) keep working without modification. Returns 0 when no
        layer==2 (trim) events have been recorded.

        Audit-Fix-24: Acquire _compaction_lock before iterating to guard against
        concurrent rebind via the append+truncate critical section.

        Audit-Fix-26 (Bug #3): _compaction_events is shared across sessions on
        a single runtime. Without per-session filtering, session A's trim
        count bleeds into session B's breakdown. Use the breakdown session
        context (passed via the breakdown callback) to filter.
        """
        # The breakdown caller knows the session_key; we read it via the
        # _last_breakdown_session helper set by the dispatch in _run_loop.
        target_session = self._last_breakdown_session
        with self._compaction_lock:
            for ev in reversed(self._compaction_events):
                if ev.layer != 2:
                    continue
                # Empty session_key on event = unscoped (back-compat with
                # pre-Audit-Fix-26 events). Match against either empty or
                # matching session_key.
                if not ev.session_key or ev.session_key == target_session:
                    return ev.messages_removed
        return 0

    def _run_loop(self, session_key: str, text: str, turn_token: object = None,
                  prepare: Callable[[], None] | None = None) -> None:
        """Background thread: run the full tool loop for one user message.

        Args:
            prepare: Optional handler-supplied preparation callback, run on
                THIS thread before the conversation lookup (Phase 4 Part B).
            turn_token: RACE-FIX v4 turn token for this turn.
        """
        # FIX-CLEAR-ASK-RACE: mark this session as having an active loop so
        # clear_conversation() can refuse to wipe it mid-turn. Cleared in the
        # finally block at the end of this function.
        with self._lock:
            self._active_loops.add(session_key)
        # Turn state machine (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.2
        # Edit F): register the active turn_token for this session and init
        # the per-turn state to RUNNING. This happens BEFORE the
        # missing-conversation and prompt-build-failure early-exit paths
        # so every terminal path has a well-defined starting state
        # (BUG #2 fix). The actual `_terminate_turn` calls for those
        # early-exit paths are added in Phase 2b; Phase 2a only registers
        # the state so observability (get_turn_state) works.
        with self._state_lock:
            self._turn_tokens[session_key] = turn_token
            self._turn_state[(session_key, turn_token)] = TurnStatus.RUNNING
            # SP3 fix round 2 (BUG#2): a NEW turn for this session ends the
            # prior cancellation epoch — from here, this turn's approvals
            # register normally (the no-permanent-poisoning control). The
            # user cancelled the PREVIOUS turn, not the one they just sent.
            self._cancelled_epochs.discard(session_key)
        try:
            with self._lock:
                if not self._running:
                    # Runtime stopped between thread startup and loop body.
                    # Route through _terminate_turn so the state machine
                    # records a terminal state (Debugger BUG #2: otherwise
                    # get_turn_state() would stay RUNNING forever and no
                    # callback would fire).
                    self._terminate_turn(TurnResult(
                        status=TurnStatus.CANCELLED,
                        session_key=session_key,
                        turn_token=turn_token,
                        error="Runtime stopped before turn started",
                        metadata={"reason": "runtime_shutdown"},
                    ))
                    return

            # Phase 4 Part B (SPEC-UI-RESPONSIVENESS-2 §2.4): the handler's
            # turn preparation runs HERE, on the loop thread, so the GTK main
            # thread never blocks on the conversation disk load / prompt
            # reconciliation / per-agent state sync (~300–500 ms per send).
            # It runs AFTER `_active_loops.add` above, so an in-flight prep
            # inherits the FIX-CLEAR-ASK-RACE guard that makes
            # clear_conversation() refuse to wipe an active loop.
            if prepare is not None:
                try:
                    prepare()
                except Exception as e:
                    logger.exception(
                        "_run_loop: prepare callback failed for %s", session_key
                    )
                    self._terminate_turn(TurnResult(
                        status=TurnStatus.FAILED,
                        session_key=session_key,
                        turn_token=turn_token,
                        error=e,
                        metadata={"reason": "prepare_failed",
                                  "exception_type": type(e).__name__},
                    ))
                    return

            with self._lock:
                conv = self._conversations.get(session_key)
                if conv is None:
                    self._terminate_turn(TurnResult(
                        status=TurnStatus.FAILED,
                        session_key=session_key,
                        turn_token=turn_token,
                        error="No conversation found",
                        metadata={"reason": "no_conversation"},
                    ))
                    return

            # BUG #13 — Deferred prompt build. If create_conversation was called
            # with defer_prompt_build=True (system_prompt == ""), build it now on
            # the background thread. This eliminates ~300ms of main-thread blocking
            # on every new agent conversation.
            try:
                self._ensure_system_prompt(session_key)
            except Exception as e:
                self._terminate_turn(TurnResult(
                    status=TurnStatus.FAILED,
                    session_key=session_key,
                    turn_token=turn_token,
                    error=e,
                    metadata={"reason": "prompt_build_failed", "exception_type": type(e).__name__},
                ))
                return

            # Once per user turn, before the first model call. The tool loop
            # below does not call this again, so the prompt stays stable for
            # a provider prompt cache. An empty prompt was just filled by
            # _ensure_system_prompt; a matching fingerprint skips a second build.
            self._rebuild_system_prompt_if_inputs_changed(session_key)

            # BUG #21 (redesigned): Fire a dedicated turn-start signal BEFORE
            # any LLM call or tool processing. This guarantees the handler
            # starts the streaming bubble + emits the drawer lifecycle-start
            # separator for EVERY turn — including tool-only turns (LLM
            # streams zero text_delta events). The old mechanism (an empty
            # on_text_delta dispatch) never reached the handler's start-bubble
            # logic: _do_text_delta_inner's empty-return fired first, so the
            # BUG #21 regression tests shipped failing (see
            # docs/specs/SPEC-TEST-DEBT-1.md).
            if self._on_turn_start:
                self._dispatch(self._on_turn_start, session_key, _turn_token=turn_token)

            try:
                # A new user message starts a new turn. Read fingerprints
                # must not survive from the previous turn:
                # _cleanup_tool_history runs on cancel and failure, not on
                # a completed turn.
                with self._tool_history_lock:
                    self._read_fingerprints.pop(session_key, None)
                # Step 1: add user message
                conv.add_user_message(text)
                logger.debug("[tool-loop] sk=%s starting user_msg_len=%d model=%s",
                             session_key, len(text), conv.model or self._config.default_model)

                # Step 2: loop until no tool calls or limit hit
                iteration = 0
                max_iter = self._config.max_tool_iterations

                while iteration < max_iter:
                    # Check immediate cancel signal first
                    if self._cancel_requested:
                        self._cancel_requested = False
                        self._terminate_turn(TurnResult(
                            status=TurnStatus.CANCELLED,
                            session_key=session_key,
                            turn_token=turn_token,
                            # SPEC-02 fix round 2 (audit #6): CANCEL_MESSAGE,
                            # never a bare literal — the handler skips the
                            # "Turn failed" card by exact constant compare, so
                            # the short form would emit a spurious failure
                            # card on every deliberate mid-loop cancel
                            # (probe: "Cancelled" → 1 card, constant → 0).
                            error=CANCEL_MESSAGE,
                            metadata={"reason": "shutdown", "iteration": iteration},
                        ))
                        return
                    # Check cancellation before each iteration
                    with self._lock:
                        if session_key in self._cancelled:
                            self._cancelled.discard(session_key)
                            self._terminate_turn(TurnResult(
                                status=TurnStatus.CANCELLED,
                                session_key=session_key,
                                turn_token=turn_token,
                                # SPEC-02 fix round 2 (audit #6): same as the
                                # _cancel_requested site above — CANCEL_MESSAGE
                                # constant, not the short form, so the handler
                                # skips the turn-error card for user cancels
                                # reaching the loop's own cancellation check.
                                error=CANCEL_MESSAGE,
                                metadata={"reason": "user", "iteration": iteration},
                            ))
                            return
                    iteration += 1
                    logger.debug("[tool-loop] sk=%s iteration=%d/%d", session_key, iteration, max_iter)

                    # §0: Pluggable context strategy — compaction before each LLM call.
                    # The strategy lives in agent/context_strategy.py; the former
                    # Conversation trim/summary delegation shim was removed in
                    # SPEC-AUDIT-CLEANUP-2 Phase 4 (strategy is the sole owner).
                    #
                    # _compute_compaction_threshold returns (soft_ceiling, hard_ceiling)
                    # where soft_ceiling = int(hard_ceiling * threshold) and
                    # threshold defaults to 0.80 (configurable per-provider).
                    soft_ceiling, hard_ceiling = self._compute_compaction_threshold(conv)
                    model_max = hard_ceiling  # preserve for breakdown dispatch below
                    self._compact_before_llm(session_key, conv, soft_ceiling, hard_ceiling)
                    # §2.8: Telemetry — read strategy.last_result, append to history.
                    # Audit-Fix-7: Patch hard_ceiling — strategy doesn't know the real value
                    # (computed by _compute_compaction_threshold at the runtime level).
                    # Audit-Fix-8: Guard append+truncate with _compaction_lock.
                    # Audit-Fix-19: Only mark iteration as having compacted when messages or
                    # tokens were actually freed (filter out no-op compact() calls).
                    # Audit-Fix-26 (Bug #1): capture the result into a LOCAL variable.
                    # Reading self._compaction_this_iteration in the breakdown block
                    # below was a TOCTOU race: another session's thread could overwrite
                    # the flag between compact() and the breakdown dispatch.
                    # Audit-Fix-26 (Bug #3): tag the event with session_key so
                    # _last_trim_removed can filter per-session when called from
                    # the breakdown block (events from other sessions on the same
                    # runtime are no longer mixed into this session's breakdown).
                    ev = self._context_strategy.last_result
                    _compaction_happened = False
                    _ev_for_breakdown = None
                    if ev is not None and (ev.messages_removed > 0 or ev.tokens_freed > 0):
                        if ev.hard_ceiling is None:
                            ev.hard_ceiling = hard_ceiling
                        # Tag the event with the originating session_key. Reuse the
                        # event object directly (it's a fresh per-call dataclass).
                        if not ev.session_key:
                            ev.session_key = session_key
                        _compaction_happened = True
                        _ev_for_breakdown = ev
                        with self._compaction_lock:
                            self._compaction_events.append(ev)
                            # Cap history at 100 events (prevents unbounded growth).
                            if len(self._compaction_events) > 100:
                                self._compaction_events = self._compaction_events[-100:]
                    # NOTE: self._compaction_this_iteration is intentionally NO LONGER
                    # written here. Bug #1 was caused by treating a per-runtime flag as
                    # if it were per-session/per-iteration; the breakdown block now
                    # uses the local _compaction_happened instead. The attribute is
                    # retained on the instance for backward-compat reads (e.g. tests)
                    # but no longer carries meaningful state. See tests for the
                    # deprecation notice.

                    # Get tools for this agent (filtered by allowed_tools if set)
                    from agent.tools import get_tool_definitions_for_api
                    tools = get_tool_definitions_for_api(conv.allowed_tools)

                    # Phase B: Merge MCP tools if configured
                    if conv.mcp_servers:
                        try:
                            from utils.mcp_client import get_tools_for_api
                            mcp_tools = get_tools_for_api(
                                conv.mcp_servers,
                                session_key if session_key != "_unknown" else None,
                            )
                            tools.extend(mcp_tools)
                        except Exception as e:
                            logger.warning(f"Failed to load MCP tools for {session_key}: {e}")

                    # Build API messages AFTER compact so the wire payload reflects
                    # the trimmed conversation. Bug fix: was captured before compact().

                    # Pre-call budget guard: if the conversation still exceeds
                    # the model's context window after compaction, raise a clear
                    # error before serializing or sending. This prevents mid-stream
                    # HTTP 400 rejections (which corrupt conversation state because
                    # the assistant message is already added by the time the error
                    # surfaces). Response reserve accounts for output tokens.
                    messages = self._messages_for_model(
                        session_key, conv, model_max, tools,
                    )

                    # Turn state machine (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION
                    # §2.2 Edit G): transition RUNNING → STREAMING (non-terminal)
                    # before the first LLM call. Keyed by (sk, tk) tuple (BUG #3,
                    # #4) and read/written under _state_lock (the GIL does not
                    # make the compound op atomic).
                    with self._state_lock:
                        if self._turn_state.get((session_key, turn_token)) == TurnStatus.RUNNING:
                            self._turn_state[(session_key, turn_token)] = TurnStatus.STREAMING
                    try:
                        response = self._call_llm(
                            session_key, messages, tools, turn_token=turn_token,
                        )
                    except TurnInactive as inactive:
                        self._exit_inactive_turn(
                            session_key, turn_token, inactive.reason, iteration,
                        )
                        return

                    # Extract content and tool calls
                    # Determine provider from conversation model
                    model = conv.model or self._config.default_model
                    loop_provider = model.split("/")[0] if "/" in model else model
                    loop_fmt = _RESPONSE_FORMAT.get(loop_provider, "openai")
                    text_content = extract_text_content(response, response_format=loop_fmt)
                    tool_calls_raw = extract_tool_calls(response, response_format=loop_fmt)

                    # Record usage
                    prompt_tok, comp_tok = extract_usage(response, response_format=loop_fmt)
                    cost = cost_for_model(conv.model, prompt_tok, comp_tok)
                    conv.record_usage(prompt_tok + comp_tok, cost)
                    self._dispatch(self._on_token_usage, session_key, prompt_tok + comp_tok, cost)

                    # §4.15 — Token budget breakdown for observability.
                    # Reuses the model_max that the trim call above already computed.
                    # Audit-Fix-26 (Bugs #1, #2, #3): use LOCAL variables
                    # (_compaction_happened, _ev_for_breakdown) instead of re-reading
                    # the shared _compaction_this_iteration flag or
                    # self._context_strategy.last_result. The shared state could be
                    # mutated by another session's thread between the gate and the
                    # breakdown dispatch, causing this session to report the wrong
                    # compaction state.
                    if self._on_token_breakdown is not None:
                        breakdown = conv.get_token_breakdown(model_max)
                        breakdown["trimmed_this_turn"] = _compaction_happened
                        breakdown["messages_remaining"] = len(conv.messages)
                        # Tag the most recent breakdown so _last_trim_removed knows
                        # which session_key to filter on. _last_trim_removed reads
                        # this attribute, so this must happen BEFORE the dispatch.
                        # Bug #3 fix: filter _compaction_events by session_key to
                        # avoid cross-session contamination.
                        self._last_breakdown_session = session_key
                        breakdown["messages_removed_this_turn"] = (
                            self._last_trim_removed if _compaction_happened else 0
                        )
                        # §0.4 + §2.8: Compaction telemetry from the strategy.
                        # Audit-Fix-20: Only include compaction_event when actual compaction
                        # occurred. Bug #2 fix: use the LOCAL _ev_for_breakdown instead
                        # of re-reading self._context_strategy.last_result, which could
                        # have been overwritten by another session's compact() call.
                        if _compaction_happened and _ev_for_breakdown is not None:
                            breakdown["compaction_event"] = {
                                "trigger": _ev_for_breakdown.trigger,
                                "layer": _ev_for_breakdown.layer,
                                "tokens_before": _ev_for_breakdown.tokens_before,
                                "tokens_after": _ev_for_breakdown.tokens_after,
                                "tokens_freed": _ev_for_breakdown.tokens_freed,
                                "soft_ceiling": _ev_for_breakdown.soft_ceiling,
                                "hard_ceiling": _ev_for_breakdown.hard_ceiling,
                                "summary_tokens_injected": _ev_for_breakdown.summary_tokens_injected,
                            }
                        self._dispatch(self._on_token_breakdown, session_key, breakdown)
                        # Bug #1 fix: no longer reset self._compaction_this_iteration
                        # here — the breakdown used the local _compaction_happened,
                        # so there's no shared flag to reset. The attribute is kept
                        # for backward-compat (read by tests) but is no longer the
                        # source of truth for breakdown state.

                    logger.debug("[tool-loop] sk=%s llm response: text_len=%d tool_calls=%d tokens=%d cost=%.4f",
                                 session_key, len(text_content or ""), len(tool_calls_raw),
                                 prompt_tok + comp_tok, cost)

                    if not tool_calls_raw:
                        # Text-only response — but check for empty/missing content
                        # which may indicate a provider error that wasn't raised (e.g. body-level
                        # error that slipped through, or malformed response with no choices)
                        # Whitespace-only counts as empty: some strict providers (Cohere,
                        # Anthropic strict mode) treat blank assistant content as a 400
                        # the same as a missing/empty payload. _is_empty_content covers
                        # falsy values (None, ""), empty lists, and strings that strip to
                        # nothing (e.g. " \n\u200b"). Same predicate used at the
                        # tool-call path below for consistency.
                        if _is_empty_content(text_content):
                            logger.warning("[tool-loop] sk=%s LLM returned no content (with tool_calls=%d, choices=%d) — treating as error",
                                           session_key, len(tool_calls_raw), len(response.get("choices") or []))

                            # OpenRouter mid-stream error check: if the SSE stream
                            # delivered finish_reason="error" with error details,
                            # surface the actual provider error message instead of
                            # the generic "no content" placeholder. This happens when
                            # the provider hits rate limits, content filters,
                            # or other mid-stream errors.
                            stream_err = response.get("_stream_error")
                            if stream_err:
                                err_code = stream_err.get("code", 0)
                                err_msg = stream_err.get("message", "Unknown provider error")
                                if err_code == 0:
                                    error_text = f"Provider error: {err_msg}"
                                else:
                                    error_text = f"Provider error (code={err_code}): {err_msg}"
                                # OpenRouter errors often include a metadata field
                                # with the underlying provider name (e.g., nvidia).
                                # Surface it when available for debugging.
                                metadata = stream_err.get("metadata")
                                if isinstance(metadata, dict) and metadata:
                                    provider_name = metadata.get("provider_name")
                                    if provider_name:
                                        error_text += f" (underlying provider: {provider_name})"
                            else:
                                error_text = "Agent returned no content. This may indicate a configuration error or an issue with the LLM provider."

                            # Defense in depth: instead of persisting a corrupt empty
                            # assistant message that downstream providers (Cohere,
                            # strict OpenAI tool-loop, Anthropic strict mode) reject
                            # with HTTP 400 "must have non-empty content or tool calls",
                            # record a descriptive placeholder. The on_error dispatch
                            # below still fires so the user sees the error; this just
                            # prevents the corrupt entry from being saved and re-sent
                            # on subsequent calls.
                            # Trigger covers: missing choices, empty choices,
                            # choices-present-but-empty-content (e.g. nemotron-3-ultra
                            # returning finish_reason="stop" with empty content after
                            # a tool execution completes), and whitespace-only content
                            # (single newline, zero-width space, etc.) that strict
                            # providers also reject.
                            conv.add_assistant_message(
                                "[LLM returned no content — provider error or malformed response]",
                                [],
                            )
                            # Route through _terminate_turn (Phase 2b Edit D.2):
                            # the dispatch is subsumed by _terminate_turn's internal
                            # _dispatch (which already catches handler exceptions),
                            # and _terminate_turn handles the _auto_save for us.
                            self._terminate_turn(TurnResult(
                                status=TurnStatus.FAILED,
                                session_key=session_key,
                                turn_token=turn_token,
                                error=error_text,
                                metadata={
                                    "reason": "empty_content",
                                    "iteration": iteration,
                                    "provider_failure": self._classified_stream_error(
                                        session_key, stream_err, "empty_content",
                                    ),
                                },
                            ))
                            return

                        # Text-only response — but check for _stream_error first.
                        # OpenRouter mid-stream error with non-empty partial content:
                        # the stream was cut short by a provider error but some text
                        # was already delivered. Surface a warning to the user.
                        stream_err = response.get("_stream_error")
                        if stream_err:
                            err_code = stream_err.get("code", 0)
                            err_msg = stream_err.get("message", "Unknown provider error")
                            if err_code == 0:
                                error_text = f"Provider error: {err_msg}"
                            else:
                                error_text = f"Provider error (code={err_code}): {err_msg}"
                            metadata = stream_err.get("metadata")
                            if isinstance(metadata, dict) and metadata:
                                provider_name = metadata.get("provider_name")
                                if provider_name:
                                    error_text += f" (underlying provider: {provider_name})"
                            # BUG #3 fix: dispatch via _on_error instead of appending
                            # to text_content. Appending the warning to the conversation
                            # history causes it to be re-sent to the LLM on the next turn,
                            # polluting the context. Use the same pattern as the empty-content
                            # error path (see lines ~1145-1155).
                            logger.warning("[tool-loop] sk=%s stream error with non-empty content: %s",
                                           session_key, error_text)
                            # Phase 2b Edit D.3: route through _terminate_turn.
                            # CRITICAL: the explicit `return` after this call is
                            # MANDATORY (BUG #5). Without it, the code falls through
                            # to the text-success path below and dispatches BOTH
                            # on_error AND on_response_complete for the same turn.
                            # _terminate_turn handles the dispatch (subsumed — it
                            # already catches handler exceptions in _dispatch) and
                            # the _auto_save.
                            self._terminate_turn(TurnResult(
                                status=TurnStatus.FAILED,
                                session_key=session_key,
                                turn_token=turn_token,
                                error=error_text,
                                metadata={
                                    "reason": "stream_error_with_content",
                                    "iteration": iteration,
                                    "provider_failure": self._classified_stream_error(
                                        session_key, stream_err, "stream",
                                    ),
                                },
                            ))
                            return

                        logger.debug("[tool-loop] sk=%s text-only response, dispatching on_response_complete len=%d",
                                     session_key, len(text_content or ""))
                        conv.add_assistant_message(text_content, [])
                        # Phase 2b Edit D.4: _check_and_stop_on_limit is now a
                        # pure predicate (Edit Q). If a limit is hit, terminate
                        # FAILED; otherwise terminate COMPLETED. The dispatch
                        # and _auto_save are subsumed by _terminate_turn.
                        limit_result = self._check_and_stop_on_limit(session_key, conv)
                        if limit_result is not None:
                            stopped_reason, reason_msg = limit_result
                            conv.add_assistant_message(f"[stopped: {reason_msg}]", [])
                            self._terminate_turn(TurnResult(
                                status=TurnStatus.FAILED,
                                session_key=session_key,
                                turn_token=turn_token,
                                error=reason_msg,
                                metadata={"reason": stopped_reason, "iteration": iteration},
                            ))
                            return
                        self._terminate_turn(TurnResult(
                            status=TurnStatus.COMPLETED,
                            session_key=session_key,
                            turn_token=turn_token,
                            text=text_content,
                            metadata={
                                "stream_error": response.get("_stream_error"),
                            },
                        ))
                        return

                    if self._execute_tool_batch(
                        session_key,
                        turn_token,
                        conv,
                        text_content,
                        tool_calls_raw,
                        iteration,
                    ):
                        return

                    # Check cost/step limits after tool execution
                    # Phase 2b Edit D.7: _check_and_stop_on_limit is now a pure
                    # predicate (Edit Q). On hit, route through _terminate_turn
                    # with FAILED status. The conv.add_assistant_message +
                    # _auto_save are subsumed by _terminate_turn's terminal
                    # transition (FAILED always persists).
                    limit_result = self._check_and_stop_on_limit(session_key, conv)
                    if limit_result is not None:
                        stopped_reason, reason_msg = limit_result
                        conv.add_assistant_message(f"[stopped: {reason_msg}]", [])
                        self._terminate_turn(TurnResult(
                            status=TurnStatus.FAILED,
                            session_key=session_key,
                            turn_token=turn_token,
                            error=reason_msg,
                            metadata={"reason": stopped_reason, "iteration": iteration},
                        ))
                        return

                # Max iterations reached
                # Phase 2b Edit D.5: route through _terminate_turn. No explicit
                # return needed — control falls out of the while loop and the
                # function ends after the finally block. The original code had
                # no return here either.
                conv.add_assistant_message("[max tool iterations reached]", [])
                self._terminate_turn(TurnResult(
                    status=TurnStatus.FAILED,
                    session_key=session_key,
                    turn_token=turn_token,
                    error="Max tool iterations reached",
                    metadata={"reason": "max_iterations", "iterations": max_iter},
                ))

            except Exception as e:
                logger.exception("Error in tool loop for %s", session_key)
                self._attach_provider_failure(e, session_key, "request")
                # Phase 2b Edit D.6: route through _terminate_turn. The
                # _auto_save is subsumed (FAILED always persists per
                # _terminate_turn's contract) and the dispatch is subsumed
                # (handler exceptions are caught by _terminate_turn's internal
                # _dispatch wrapper). Conv may be undefined if the exception
                # happened before the conv resolution — guard with the local
                # name rebind.
                # The original QTR-FIX noted that partial progress must be
                # persisted; _terminate_turn's persistence path handles this
                # via _auto_save on FAILED.
                # SPEC-02: rollback trailing EMPTY assistant messages (no
                # content, no tool_calls) so a failed turn never persists
                # them (FAILED's _auto_save runs below, same thread,
                # sequential — so this must run first). The strip is
                # ORIGIN-AGNOSTIC — the predicate never asks which turn
                # added a message. In the normal mid-loop failure the
                # turn's own user message (Step 1) shields pre-existing
                # history from the tail-up strip; only a failure before
                # that user message lands (e.g. add_user_message itself
                # raising) exposes a pre-existing trailing empty, which is
                # then also cleaned on the failed turn. Note the earlier
                # terminal paths (prepare_failed, prompt_build_failed,
                # no_conversation) return BEFORE this block — they run no
                # rollback at all. Partial content is real progress and
                # stays. Looked up via _conversations, NOT the local conv
                # binding (the exception may predate conv resolution).
                # Guarded: a rollback failure must never mask the original
                # error.
                try:
                    conv_rb = self._conversations.get(session_key)
                    if conv_rb is not None and conv_rb.messages:
                        removed = 0
                        while conv_rb.messages and removed < 5:
                            m = conv_rb.messages[-1]
                            is_empty_assistant = (
                                str(getattr(m.role, "value", m.role)) == "assistant"
                                and not (m.content or "").strip()
                                and not (m.tool_calls or [])
                            )
                            if not is_empty_assistant:
                                break
                            conv_rb.messages.pop()
                            removed += 1
                        if removed:
                            logger.info(
                                "[turn-fail] rolled back %d empty assistant message(s) for %s",
                                removed, session_key,
                            )
                except Exception:
                    logger.exception("empty-message rollback failed for %s", session_key)
                self._terminate_turn(TurnResult(
                    status=TurnStatus.FAILED,
                    session_key=session_key,
                    turn_token=turn_token,
                    error=e,
                    metadata={"reason": "exception", "exception_type": type(e).__name__},
                ))
        finally:
            # FIX-CLEAR-ASK-RACE: always release the active-loop marker, even
            # on exception or early return, so a crashed loop doesn't block
            # /clear for this session permanently.
            with self._lock:
                self._active_loops.discard(session_key)

    def _execute_tool_batch(
        self,
        session_key: str,
        turn_token: object,
        conv,
        text_content: str,
        tool_calls_raw,
        iteration: int,
    ) -> bool:
        """Run one iteration's tool calls.

        Returns True if the turn was terminated and the caller must return
        immediately. Returns False if the caller should continue with the
        post-tool limit check.
        """
        del turn_token  # part of the turn identity; this batch does not end the turn
        # Tool calls — execute each
        logger.debug("[tool-loop] sk=%s executing %d tool calls", session_key, len(tool_calls_raw))
        from models.conversation import ToolCall
        from agent.tools import execute_tool

        # Create assistant message once, attach all tool calls — fixes data corruption
        # (was: conv.messages[-1].tool_calls.append(tc) — appended to USER message)
        tool_call_objects = [
            ToolCall(call_id=call_id, tool_name=tool_name, arguments=args)
            for call_id, tool_name, args in tool_calls_raw
        ]

        # BUG #3 sweep: tool-call response with empty/whitespace text_content.
        # OpenAI spec allows content=null with tool_calls, but strict providers
        # (Cohere, Anthropic strict mode) require non-empty content even when
        # tool_calls are present. If a model returns tool_calls with empty
        # content (e.g. provider bug, malformed streaming response), substitute
        # a meaningful placeholder so the next LLM call doesn't 400.
        # The read-side filter at models/conversation.py:262 already handles
        # the no-content-no-tool_calls case; this fills the gap for the
        # tool_calls-present-but-content-empty case.
        if _is_empty_content(text_content):
            logger.warning(
                "[tool-loop] sk=%s tool-call response has empty content "
                "(tool_calls=%d) — substituting placeholder for strict-provider safety",
                session_key, len(tool_call_objects),
            )
            text_content = "[calling tools]"

        conv.add_assistant_message(text_content, tool_call_objects)

        # Import once per loop iteration (avoid repeated import overhead)
        import agent.tools as agent_tools_module

        for call_id, tool_name, args in tool_calls_raw:
            tc = next(tc for tc in tool_call_objects if tc.call_id == call_id)

            # Approval gating for exec_command — fires BEFORE tool_call_start
            # so the approval card appears first. Non-approval tools skip this.
            if tool_name == "exec_command":
                approved = self._dispatch_approval(session_key, tool_name, args)
                logger.debug("[tool-loop] sk=%s exec_command approval: %s", session_key, approved)
                if approved is False or approved is None:  # None = timeout = denial
                    tc.mark_failed("exec_command requires PM approval — request denied or timed out")
                    text = tc.result or "denied"
                    tc.result = None
                    conv.add_tool_result(call_id, text)
                    self._dispatch(self._on_tool_call_result, session_key, tool_name, text, False)
                    self._audit_log.record(tool_name, args, approved=False,
                                            user=getattr(self._config, "user_id", ""),
                                            result="denied")  # A-4
                    continue

            # HIGH-1: Sensitive-path write/edit also requires PM approval.
            # Fires before tool_call_start so the PM sees the card.
            if tool_name in ("write_file", "edit_file"):
                path_arg = args.get("path", "")
                if agent_tools_module.is_sensitive_path(path_arg):
                    approved = self._dispatch_approval(session_key, tool_name, args)
                    logger.debug("[tool-loop] sk=%s %s sensitive approval: %s",
                                 session_key, tool_name, approved)
                    if approved is False or approved is None:
                        tc.mark_failed(
                            f"{tool_name} blocked: {path_arg} is a sensitive path\n"
                            "PM approval denied or timed out."
                        )
                        text = tc.result or "denied"
                        tc.result = None
                        conv.add_tool_result(call_id, text)
                        self._dispatch(self._on_tool_call_result, session_key, tool_name, text, False)
                        self._audit_log.record(tool_name, args, approved=False,
                                                user=getattr(self._config, "user_id", ""),
                                                result="denied")  # A-4
                        continue

            # Tool call start — fires AFTER approval (for exec_command and sensitive write/edit)
            # so the "running" card is truthful: the tool is actually about to run.
            self._dispatch(self._on_tool_call_start, session_key, tool_name, args)
            tc.mark_executing()

            # Execute tool
            logger.debug("[tool-loop] sk=%s executing tool: %s args_keys=%s",
                         session_key, tool_name, list(args.keys()))
            # Bypass exec_command's internal approval check — the runtime already
            # confirmed PM approval via _dispatch_approval above (returned True).
            # HIGH-1: write_file/edit_file with sensitive paths — runtime already
            # dispatched to PM above, so bypass the tool's internal check.
            # MED-1: Use per-call approval_callback (bypass = lambda True, normal = None).
            bypass_approval = (tool_name == "exec_command" or
                               (tool_name in ("write_file", "edit_file") and
                                agent_tools_module.is_sensitive_path(args.get("path", ""))))
            per_call_cb = (lambda *a: True) if bypass_approval else None
            # LOW-2: validate the session workspace. The return value
            # is unread (dead binding removed) — the required side
            # effects are the LOW-2 validation (raises ValueError if
            # project_path is empty or session_key is malformed) and
            # the per-session scratch-dir creation with 0o700
            # permissions (.crabcakes/tmp/<session>/).
            resolve_session_workspace(conv.project_path, session_key)
            # project_path is the sandbox base for all tools AND exec_command cwd.
            # The scratch dir above exists for future use but no longer
            # overrides exec_command CWD — see exec-cwd-fix spec.
            # Allowed-tools enforcement gate (§3.21n).
            # Forward conv.allowed_tools so execute_tool can deny tools the agent
            # was configured without. conv.allowed_tools is the single source of
            # truth — set in create_conversation() from agent_def["tools"] and
            # persisted on the conversation object.
            #
            # Execute through the tool middleware chain.
            # The chain wraps execute_tool with EnforcementMiddleware
            # (post-write verification) and StuckDetectionMiddleware
            # (loop detection). Approval was already resolved inline
            # above (before on_tool_call_start) per spec §A.2.4.
            ctx = ToolContext(
                session_key=session_key,
                project_path=conv.project_path,
                iteration=iteration,
                bypass_approval=bypass_approval,
                audit_log=self._audit_log,
                user_id=getattr(self._config, "user_id", ""),
                enforcement_config=self._config.enforcement,
                si_enforcement=conv.si_enforcement,
            )
            result = self._tool_chain.run(
                tool_name=tool_name,
                args=args,
                ctx=ctx,
                executor=lambda: execute_tool(
                    tool_name, args, conv.project_path, session_key,
                    approval_callback=per_call_cb,
                    allowed_tools=conv.allowed_tools,
                ),
            )
            logger.debug("[tool-loop] sk=%s tool %s result: success=%s output_len=%d",
                         session_key, tool_name, result.success, len(result.output or ""))

            # Record tool result — ToolResult dataclass stays clean.
            # mark_completed sets status and completed_at. The tool message
            # is the copy that is kept; tc.result is cleared before
            # add_tool_result so the body is not stored twice.
            tc.mark_completed(result.output if result.success else result.error or "")
            tool_result_text = tc.result or ""
            if tool_name == "read_file" and result.success:
                tool_result_text = self._dedup_read_output(
                    session_key, conv, args, tool_result_text,
                )
            if result.success:
                self._note_tool_path(session_key, conv, tool_name, args)
            tc.result = None

            conv.add_tool_result(call_id, tool_result_text)
            self._maybe_compile(session_key, conv, tool_name, args, result)
            if result.success and tool_name in ("write_file", "edit_file"):
                with self._repo_map_lock:
                    self._repo_map_tree_version += 1
            self._dispatch(self._on_tool_call_result, session_key, tool_name, tool_result_text, result.success)

            # A-4: Record in audit log
            _audit_user = getattr(self._config, "user_id", "")
            self._audit_log.record(
                tool_name=tool_name,
                args=args,
                approved=True if bypass_approval else None,
                user=_audit_user,
                result=tool_result_text,
                exit_code=result.exit_code,
            )

        return False

    def is_loop_active(self, session_key: str) -> bool:
        """Return True if a _run_loop thread is currently active for this session.

        FIX-CLEAR-ASK-RACE: used by AgentRuntimeHandler.clear_conversation() to
        refuse wiping a conversation that an in-flight loop is still reading.
        Thread-safe via _lock. A session marked active stays active until the
        loop's finally block discards it — including through exceptions and
        early returns, so a crashed loop cannot permanently block /clear.
        """
        with self._lock:
            return session_key in self._active_loops

    def get_last_turn_result(self, session_key: str) -> TurnResult | None:
        """Return the most recent terminal ``TurnResult`` for ``session_key``.

        Returns the ``TurnResult`` for the session's currently active turn
        token. If no terminal transition has occurred for the active token
        (turn is still RUNNING or STREAMING, or no turn has been
        attempted), returns ``None``.

        Used by the handler for observability and by tests to assert on
        terminal state. Thread-safe via `_state_lock`.

        Note: results for stale tokens (a prior turn that has since been
        superseded by a new ``send_message()``) are NOT returned; only
        the active token's result is exposed.
        """
        with self._state_lock:
            # Membership check (Debugger BUG #2: none-sentinel-confusion):
            # a turn_token of None is valid when _run_loop is called with
            # its default. `session_key in` distinguishes "no turn
            # registered" from "turn registered with None token."
            if session_key not in self._turn_tokens:
                return None
            tk = self._turn_tokens[session_key]
            return self._turn_results.get((session_key, tk))

    def get_turn_state(self, session_key: str) -> TurnStatus | None:
        """Return the current ``TurnStatus`` for ``session_key``'s active turn
        token, or ``None`` if no turn is active.

        Thread-safe via `_state_lock`. Returns the status of the session's
        currently active token only.
        """
        with self._state_lock:
            # Membership check (Debugger BUG #2: none-sentinel-confusion).
            if session_key not in self._turn_tokens:
                return None
            tk = self._turn_tokens[session_key]
            return self._turn_state.get((session_key, tk))


    def _dispatch_approval(self, session_key: str, tool_name: str, args: dict) -> bool | None:
        """
        Dispatch approval request. Returns True/False if callback resolves immediately,
        or None if the callback is async (waits for PM).
        """
        if self._on_tool_call_approval_needed is None:
            return False

        result_ref: list = [None]
        event = threading.Event()

        approval_key = f"{session_key}:{uuid.uuid4().hex[:8]}"
        with self._lock:
            self._pending_approvals[approval_key] = {
                "event": event,
                "result_ref": result_ref,
            }

        # Dispatch to callback — PM must click Approve/Deny to resolve.
        # do_approval() MUST NOT set event or result_ref. Those are only set
        # by approve_exec() when the PM clicks. Setting them here causes the
        # event to fire immediately (before PM clicks), making approval meaningless.
        def do_approval():
            try:
                self._on_tool_call_approval_needed(session_key, tool_name, args)
            except Exception:
                logger.exception("Approval callback raised exception")

        if self._GLib is not None:
            self._GLib.idle_add(do_approval)
        else:
            t = threading.Thread(target=do_approval, daemon=True)
            t.start()

        # SP3 fix round 2 (BUG#2): registration is EPOCH-aware. The guard
        # keys on _cancelled_epochs — armed by cancel(), re-armed by the
        # CANCELLED terminal transition (the only durable trace when the
        # loop's own branch already discarded _cancelled — the auditor's
        # bypass probe), cleared at the next turn's start and on
        # COMPLETED/FAILED terminals. It must NOT read _cancelled: the
        # loop's cancellation branch discards that entry, which is exactly
        # how a late registration bypassed the round-1 guard and hung 60s
        # (dbg_sp3fix_bug2bypass). Pops its own entry: the early-return
        # must not leak (dbg_sp3fix_regleak: 5/5 leaked, inflating
        # stop_all's denied count forever).
        with self._lock:
            already_cancelled = session_key in self._cancelled_epochs
            if already_cancelled:
                self._pending_approvals.pop(approval_key, None)
        if already_cancelled:
            logger.info(
                "Approval for %s refused at registration: cancellation epoch active",
                session_key,
            )
            return None  # None = denial in every consumer of this method

        # Wait for approval (with timeout).
        # approve_exec() sets event and result_ref when PM clicks.
        # If timeout expires, treat as denial so the tool loop doesn't execute.
        timed_out = not event.wait(timeout=60)
        if timed_out:
            result_ref[0] = False
        return result_ref[0]

    @staticmethod
    def _resolve_caller_key(provider_cfg: "LLMProviderConfig | None", model: str) -> str:
        """Return the API caller key for a provider.

        Uses provider_cfg.caller (explicit, persisted in providers.yaml).
        If empty, returns empty string — the caller will then fail with a
        clear "no caller" error.
        """
        if provider_cfg is not None and provider_cfg.caller:
            return provider_cfg.caller.lower()
        return ""

    def set_on_provider_failover(self, cb) -> None:
        """Store the callback fired when a call advances to the fallback provider.

        Trigger: `_call_llm` chain walk, only while `provider_failover_enabled`
        is true, after `decide()` returns `advance_chain`. The callback is
        `(session_key, reason)`. A missing callback just logs.
        """
        self._on_provider_failover = cb

    def _call_llm(
        self,
        session_key: str,
        messages: list[dict],
        tools: list[dict],
        turn_token: object | None = None,
    ) -> dict:
        """One provider call, or the SPEC-22 chain walk when failover is enabled.

        The flag defaults on. Off means this is exactly one `_call_llm_once`
        at the tool timeout. On means retry / advance / halt per `decide()`,
        each attempt bounded by `provider_failover_attempt_timeout_s`.
        """
        if not getattr(self._config, "provider_failover_enabled", True):
            return self._call_llm_once(session_key, messages, tools, turn_token=turn_token)
        return self._call_llm_chain(session_key, messages, tools, turn_token)

    def _failover_limits(self) -> tuple[float, float]:
        """Return (budget seconds, per-attempt socket timeout)."""
        try:
            budget = float(getattr(self._config, "provider_failover_budget_s", 30.0) or 30.0)
        except (TypeError, ValueError):
            budget = 30.0
        try:
            cap = float(getattr(self._config, "provider_failover_attempt_timeout_s", 8.0) or 8.0)
        except (TypeError, ValueError):
            cap = 8.0
        if budget != budget or budget <= 0:  # NaN or non-positive
            budget = 30.0
        if cap != cap or cap <= 0:
            cap = 8.0
        return budget, cap

    def _chain_socket_timeout(
        self,
        budget_started: float | None,
        budget: float,
        cap: float,
    ) -> float | None:
        """Idle timeout for the next chain attempt.

        The first attempt of a chain step uses `cap` and does not consult
        the budget. After a classified failure the clock is running: the
        next attempt starts only when at least `_FAILOVER_ATTEMPT_FLOOR_S`
        remains, and its socket timeout is the lesser of `cap` and that
        remainder so the try ends inside the budget.
        """
        if budget_started is None:
            return cap
        remaining = budget - (time.monotonic() - budget_started)
        if remaining < _FAILOVER_ATTEMPT_FLOOR_S:
            return None
        return min(cap, remaining)

    def _call_llm_chain(self, session_key: str, messages: list[dict], tools: list[dict], turn_token) -> dict:
        from agent.llm.failure_policy import RetryConfig, decide

        budget, cap = self._failover_limits()
        budget_started: float | None = None
        attempt = 1
        on_fallback = False
        seen_messages = list(messages)
        pending: tuple[BaseException, object] | None = None
        for _step in range(8):
            inactive = self._inactive_turn_reason(session_key, turn_token)
            if inactive is not None:
                raise TurnInactive(inactive)
            timeout = self._chain_socket_timeout(budget_started, budget, cap)
            if timeout is None:
                if pending is None:
                    raise RuntimeError("provider failover budget cannot cover another attempt")
                exc, failure = pending
                outcome = self._apply_chain_decision(
                    session_key, exc, failure, attempt, on_fallback,
                    budget_started, budget, cap, turn_token, decide, RetryConfig,
                )
                if outcome == "advance":
                    on_fallback = True
                    attempt = 1
                    budget_started = None
                    pending = None
                    continue
                # Halt and a failed advance already released the lease.
                # A retry with no time left would spin, so stop the turn.
                if outcome == "retry":
                    self._release_session_lease(session_key)
                raise exc
            try:
                with ssl_retry_budget(0):
                    response = self._call_llm_once(
                        session_key, seen_messages, tools,
                        turn_token=turn_token, request_timeout=timeout,
                    )
            except Exception as exc:
                self._attach_provider_failure(exc, session_key, "request")
                failure = getattr(exc, "_failure", None)
                if failure is None:
                    raise
                if budget_started is None:
                    budget_started = time.monotonic()
                self._record_attached_failure(exc)
                pending = (exc, failure)
                outcome = self._apply_chain_decision(
                    session_key, exc, failure, attempt, on_fallback,
                    budget_started, budget, cap, turn_token, decide, RetryConfig,
                )
                if outcome == "retry":
                    attempt += 1
                    continue
                if outcome == "advance":
                    on_fallback = True
                    attempt = 1
                    budget_started = None
                    pending = None
                    continue
                raise
            stream_exc = self._stream_failure_exception(session_key, response)
            if stream_exc is None:
                self._last_stream_text.pop(session_key, None)
                return response
            failure = stream_exc._failure
            if budget_started is None:
                budget_started = time.monotonic()
            self._record_attached_failure(stream_exc)
            pending = (stream_exc, failure)
            outcome = self._apply_chain_decision(
                session_key, stream_exc, failure, attempt, on_fallback,
                budget_started, budget, cap, turn_token, decide, RetryConfig,
            )
            if outcome == "retry":
                attempt += 1
                continue
            if outcome == "advance":
                on_fallback = True
                attempt = 1
                budget_started = None
                pending = None
                continue
            raise stream_exc
        raise RuntimeError("provider failover stopped after 8 steps")

    def _apply_chain_decision(
        self,
        session_key: str,
        exc: BaseException,
        failure: object,
        attempt: int,
        on_fallback: bool,
        budget_started: float | None,
        budget: float,
        cap: float,
        turn_token,
        decide,
        retry_config_cls,
    ) -> str:
        """Return 'retry', 'advance', or 'raise'."""
        action = self._failover_action(
            failure, attempt, on_fallback, budget_started, budget, cap, decide, retry_config_cls,
        )
        if action == "retry":
            self._failover_sleep(budget_started, budget, retry_config_cls)
            return "retry"
        if action == "advance":
            fallback_name = self._switch_to_fallback(session_key)
            if not fallback_name:
                self._release_session_lease(session_key)
                return "raise"
            self._note_failover(session_key, failure, fallback_name, turn_token)
            return "advance"
        self._release_session_lease(session_key)
        return "raise"

    def _failover_action(
        self, failure, attempt, on_fallback, budget_started, budget, cap, decide, retry_config_cls,
    ):
        cfg = retry_config_cls(
            provider_retries=int(getattr(self._config, "provider_retries", 2) or 2),
            backoff_base_s=float(getattr(self._config, "provider_retry_backoff_s", 1.0) or 0.0),
            provider_failover_budget_s=budget,
        )
        conv = self._conversations.get(failure.session_key)
        primary = getattr(conv, "model", "") or failure.provider or "primary"
        primary_name = primary.split("/", 1)[0] if "/" in primary else primary
        fallback = (getattr(conv, "fallback_provider", None) or "").strip()
        if on_fallback or not fallback or fallback == primary_name:
            chain = [primary_name]
        else:
            chain = [primary_name, fallback]
        if (
            budget_started is not None
            and self._chain_socket_timeout(budget_started, budget, cap) is None
            and attempt <= cfg.provider_retries
        ):
            attempt = cfg.provider_retries + 1
        decision = decide(failure, attempt, chain, cfg)
        return {
            "retry": "retry",
            "advance_chain": "advance",
            "halt": "halt",
        }.get(decision.action, "halt")

    def _failover_sleep(self, budget_started, budget, retry_config_cls) -> None:
        cfg = retry_config_cls(
            backoff_base_s=float(getattr(self._config, "provider_retry_backoff_s", 1.0) or 0.0),
        )
        wait = cfg.backoff_base_s
        if wait <= 0:
            return
        if budget_started is not None:
            remaining = budget - (time.monotonic() - budget_started)
            # Leave at least the floor so the next attempt is allowed to start.
            max_sleep = remaining - _FAILOVER_ATTEMPT_FLOOR_S
            if max_sleep <= 0:
                return
            wait = min(wait, max_sleep)
        if wait > 0:
            time.sleep(wait)

    def _exit_inactive_turn(
        self,
        session_key: str,
        turn_token: object,
        reason: str,
        iteration: int,
    ) -> None:
        """Leave a turn that must not call the provider.

        A replaced token returns without a terminal record: the new turn
        owns the session, and ``_terminate_turn`` would reject this result.
        A cancel whose token is still current uses the same CANCELLED
        result as the checks at the top of the tool loop.
        """
        if reason == "superseded":
            logger.debug(
                "[tool-loop] sk=%s superseded before provider call; old turn stopped",
                session_key,
            )
            return
        self._terminate_turn(TurnResult(
            status=TurnStatus.CANCELLED,
            session_key=session_key,
            turn_token=turn_token,
            error=CANCEL_MESSAGE,
            metadata={"reason": "user", "iteration": iteration},
        ))

    def _inactive_turn_reason(self, session_key: str, turn_token) -> str | None:
        """Why this turn must stop, or None if it should call the provider.

        A replaced token is ``"superseded"`` even when a cancel epoch is
        also armed: the new turn already cleared or is about to clear that
        epoch, and a terminal for the old token would be stale. ``"cancelled"``
        is the epoch while this token is still the active one.
        """
        tokens = getattr(self, "_turn_tokens", None)
        replaced = (
            isinstance(tokens, dict)
            and session_key in tokens
            and turn_token is not None
            and tokens[session_key] is not turn_token
        )
        if replaced:
            return "superseded"
        if session_key in getattr(self, "_cancelled_epochs", ()):
            return "cancelled"
        return None

    def _turn_still_active(self, session_key: str, turn_token) -> bool:
        return self._inactive_turn_reason(session_key, turn_token) is None

    def _switch_to_fallback(self, session_key: str) -> str:
        """Point the conversation at the fallback card. Returns the card name, or ''."""
        conv = self._conversations.get(session_key)
        if conv is None:
            return ""
        name = (getattr(conv, "fallback_provider", None) or "").strip()
        if not name:
            return ""
        card = self._config.providers.get(name)
        if card is None:
            try:
                from agent.config import _to_llm_provider
                from utils.providers_store import load_providers
                for item in load_providers():
                    if getattr(item, "name", "") == name:
                        card = _to_llm_provider(item)
                        break
            except Exception:
                logger.exception("fallback card lookup failed for %s", name)
                return ""
        if card is None:
            return ""
        model = getattr(card, "default_model", "") or ""
        caller = getattr(card, "caller", "") or name
        if model and "/" not in model:
            model = f"{caller}/{model}"
        if not model:
            return ""
        prefix = model.split("/", 1)[0]
        self._config.providers[prefix] = card
        self._config.providers[name] = card
        conv.model = model
        return name

    def _note_failover(self, session_key: str, failure, fallback_name: str, turn_token) -> None:
        streamed = bool(self._last_stream_text.get(session_key))
        self._discard_partial_assistant(session_key)
        if streamed and self._on_text_delta is not None:
            marker = f"\n[stream interrupted — provider failed; retrying on {fallback_name}]\n"
            self._dispatch(self._on_text_delta, session_key, marker, _turn_token=turn_token)
        reason = (
            f"{failure.provider or 'provider'} {failure.status_code or failure.failure_class.value}"
            f" — failed over to {fallback_name}"
        )
        logger.info("[failover] %s %s", session_key, reason)
        cb = self._on_provider_failover
        if cb is not None:
            self._dispatch(cb, session_key, reason)

    def _discard_partial_assistant(self, session_key: str) -> None:
        conv = self._conversations.get(session_key)
        partial = self._last_stream_text.get(session_key) or ""
        if conv is None or not getattr(conv, "messages", None) or not partial:
            return
        last = conv.messages[-1]
        role = str(getattr(getattr(last, "role", ""), "value", getattr(last, "role", "")))
        if role == "assistant" and (getattr(last, "content", "") or "") == partial:
            conv.messages.pop()
        self._last_stream_text.pop(session_key, None)

    def _stream_failure_exception(self, session_key: str, response: dict):
        if not isinstance(response, dict):
            return None
        err = response.get("_stream_error")
        if not err:
            return None
        from agent.llm.failure_classifier import ClassifiedProviderError, failure_for_stream
        conv = self._conversations.get(session_key)
        model = getattr(conv, "model", "") or ""
        provider = model.split("/", 1)[0] if "/" in model else model
        content = ""
        choices = response.get("choices") or []
        if choices and isinstance(choices[0], dict):
            content = ((choices[0].get("message") or {}).get("content")) or ""
        if content:
            self._last_stream_text[session_key] = content
        failure = failure_for_stream(
            session_key=session_key,
            provider=provider,
            model=model,
            stream_err=err,
            stage="stream" if content else "empty_content",
        )
        return ClassifiedProviderError(str(err), failure)

    def _release_session_lease(self, session_key: str) -> None:
        conv = self._conversations.get(session_key)
        project_path = getattr(conv, "project_path", None) or ""
        if not project_path:
            return
        try:
            from utils.work_persistence import find_live_lease, release_work
            lease = find_live_lease(project_path, session_key)
            if lease is not None:
                release_work(project_path, lease.unit_id, session_key)
        except Exception:
            logger.exception("work-claim release failed for %s", session_key)

    def _call_llm_once(
        self,
        session_key: str,
        messages: list[dict],
        tools: list[dict],
        turn_token: object | None = None,
        request_timeout: float | None = None,
    ) -> dict:
        """
        Make a single LLM API call. Uses SSE streaming when on_text_delta is set
        (Phase 1.3b), otherwise falls back to blocking.
        """
        # Phase CB-3: prepend pending stuck messages as transient prefixes.
        # See SPEC-CONTEXT-BLOAT-PHASE-3.md §2.3 (BUG #4 fix).
        pending = self._pending_stuck_messages.pop(session_key, [])
        if pending:
            stuck_prefix = {
                "role": "user",
                "content": (
                    "[Stuck-detection intervention — please consider a different approach]\n\n"
                    + "\n\n---\n\n".join(pending)
                ),
            }
            # QTR-FIX: inject the stuck prefix AFTER the system prompt, not
            # before it. `to_api_messages()` puts the system message at index 0
            # when `conv.system_prompt` is set; prepending the stuck message at
            # index 0 caused role:user to appear before role:system, which some
            # providers reject and which always loses the system's priority.
            if messages and messages[0].get("role") == "system":
                messages = [messages[0], stuck_prefix] + messages[1:]
            else:
                messages = [stuck_prefix] + messages
            logger.debug("[stuck-injection] sk=%s: prepended %d stuck message(s)", session_key, len(pending))

        # Use self._config (already loaded once at startup) — Bug #12 fix
        config = self._config

        conv = self._conversations.get(session_key)
        if conv is None:
            raise ValueError("No conversation found")

        model = conv.model or config.default_model
        provider_name = model.split("/")[0] if "/" in model else model

        provider_cfg = config.providers.get(provider_name)
        if provider_cfg is None:
            # If the agent specified a provider explicitly (model has a prefix like
            # "openrouter/"), don't silently fall back to the wrong provider — raise
            # a clear error so the user knows to configure it.
            if "/" in model and config.providers:
                raise ValueError(
                    f"Provider '{provider_name}' is not configured. "
                    f"Add it to Settings → Providers (or agent.json), "
                    f"or set an API key in the agent editor. "
                    f"Available providers: {', '.join(sorted(config.providers.keys()))}"
                )
            if config.providers:
                provider_name = list(config.providers.keys())[0]
                provider_cfg = config.providers[provider_name]
            else:
                raise ValueError(f"No LLM provider configured for {model}")

        # Use per-agent API key if set, otherwise fall back to the LIVE
        # providers.yaml store. The frozen self._config snapshot (provider_cfg.api_key)
        # can hold a stale token if the user edited providers in Settings after the
        # runtime was created (runtimes are cached and never reconfigured on
        # provider change). Re-reading providers.yaml on every call means a token
        # update takes effect immediately without requiring an app restart.
        # (See BUG-fix: "token expired or incorrect" 401 despite a valid key.)
        effective_api_key = conv.api_key  # per-agent key wins (authoritative)
        # SPEC-01 Phase 2: resolve the live provider CARD once — base_url/caller
        # refresh must run even when a per-agent key is set (the old block was gated
        # on `not effective_api_key`, so a corrected base_url never reached agents
        # with per-agent keys). api_key precedence is UNCHANGED.
        live_card = None
        try:
            from utils.providers_store import load_providers
            # Match by display name (p.name) OR by the provider-prefix derived from
            # its default_model (e.g. name 'glm5.2' with default_model 'zai/glm-5.2'
            # → prefix 'zai'). The match does NOT require a key: a keyless card
            # still carries a valid base_url fix.
            live_providers = load_providers()
            for p in live_providers:
                if p.name == provider_name:
                    live_card = p
                    break
            if live_card is None:
                for p in live_providers:
                    pm = (p.default_model or "")
                    live_prefix = pm.split("/")[0] if "/" in pm else pm
                    if live_prefix == provider_name:
                        live_card = p
                        break
        except Exception as e:
            logger.warning("Cannot load providers.yaml for %s: %s", provider_name, e)

        if live_card is not None:
            # Live base_url/caller override (SPEC-01). provider_cfg is this
            # runtime's OWN clone (Phase 1 BUG 6 fix), so in-place mutation
            # cannot leak across runtimes. Invalid or empty live values never
            # clobber the snapshot (caller validated against _PROVIDER_CALLERS;
            # base_url stripped before compare); api_key precedence is
            # untouched (per-agent key > live card key > frozen snapshot key).
            live_base_url = live_card.base_url.strip()
            if live_base_url and live_base_url != provider_cfg.base_url:
                logger.info("[call-llm] live base_url override for %s: %s -> %s",
                            provider_name, provider_cfg.base_url, live_base_url)
                provider_cfg.base_url = live_base_url
            # SPEC-01 fix round: validate the live caller against
            # _PROVIDER_CALLERS BEFORE mutating. A truthy-but-invalid value
            # (unknown key, whitespace-only) must NOT poison the frozen
            # snapshot — warn and keep it instead, so _resolve_caller_key /
            # _PROVIDER_CALLERS.get still see the last known-good caller.
            new_caller = live_card.caller.strip().lower()
            caller_is_valid = bool(new_caller) and new_caller in _PROVIDER_CALLERS
            if caller_is_valid and new_caller != provider_cfg.caller:
                logger.info("[call-llm] live caller override for %s: %s -> %s",
                            provider_name, provider_cfg.caller, new_caller)
                provider_cfg.caller = new_caller
            elif live_card.caller and not caller_is_valid:
                logger.warning(
                    "[call-llm] live caller %r for %s is not a valid caller "
                    "(expected one of %s); keeping frozen caller %r",
                    live_card.caller, provider_name,
                    sorted(_PROVIDER_CALLERS), provider_cfg.caller,
                )
            if not effective_api_key and live_card.api_key:
                effective_api_key = live_card.api_key
            # Reasoning: a Settings save must affect the next message without
            # restarting the agent. Valid levels (incl. "off") apply; a
            # non-empty invalid string warns and keeps the frozen level.
            from models.providers import _VALID_REASONING_LEVELS
            live_level = getattr(live_card, "reasoning_effort", "off") or "off"
            live_level = live_level.strip().lower() if isinstance(live_level, str) else "off"
            if live_level in _VALID_REASONING_LEVELS:
                provider_cfg.reasoning_effort = live_level
            else:
                logger.warning(
                    "[call-llm] live reasoning_effort %r for %s is not a valid "
                    "level; keeping frozen %r",
                    live_level, provider_name,
                    getattr(provider_cfg, "reasoning_effort", "off"),
                )
            live_flag = getattr(live_card, "supports_reasoning", None)
            if isinstance(live_flag, bool):
                provider_cfg.supports_reasoning = live_flag
            else:
                logger.warning(
                    "[call-llm] live supports_reasoning %r for %s is not a "
                    "bool; keeping frozen %r",
                    live_flag, provider_name,
                    getattr(provider_cfg, "supports_reasoning", False),
                )
        if not effective_api_key:
            # Last resort: the (possibly stale) frozen runtime snapshot.
            effective_api_key = provider_cfg.api_key
        # Use app_title as X-Title header for OpenRouter attribution
        x_title = conv.app_title or ""
        effective_effort = (
            getattr(provider_cfg, "reasoning_effort", "off")
            if getattr(provider_cfg, "supports_reasoning", False)
            else "off"
        )

        # Use streaming when on_text_delta is registered AND the provider supports it
        use_streaming = (
            self._on_text_delta is not None
            and (provider_cfg.supports_streaming if provider_cfg else True)
        )
        if use_streaming:
            logger.debug("[call-llm] sk=%s streaming=True provider=%s model=%s msg_count=%d",
                         session_key, provider_name, model, len(messages))
            caller_key = self._resolve_caller_key(provider_cfg, model)
            try:
                return self._call_llm_streaming(
                    session_key=session_key,
                    base_url=provider_cfg.base_url,
                    api_key=effective_api_key,
                    model=model,
                    caller_key=caller_key,
                    messages=messages,
                    tools=tools if tools else None,
                    timeout=(
                        float(request_timeout)
                        if request_timeout is not None
                        else float(self._config.tool_timeout_seconds)
                    ),
                    x_title=x_title,
                    turn_token=turn_token,
                    reasoning_effort=effective_effort,
                )
            except (IndexError, KeyError, TypeError, ValueError) as e:
                e._crabcakes_context = {
                    "provider": caller_key,
                    "model": model,
                    "exception_type": type(e).__name__,
                }
                self._attach_provider_failure(e, session_key, "request_boundary")
                raise

        caller_key = self._resolve_caller_key(provider_cfg, model)
        caller = _PROVIDER_CALLERS.get(caller_key)
        if caller is None:
            raise ValueError(
                f"No caller for provider {provider_cfg.name if provider_cfg else provider_name} "
                f"(caller_key={caller_key!r}). "
                f"Set the 'caller' field in Settings → Providers."
            )

        try:
            provider = _get_provider(caller_key)
            return provider.call(
                base_url=provider_cfg.base_url,
                api_key=effective_api_key,
                model=model,
                messages=messages,
                tools=tools if tools else None,
                timeout=(
                    float(request_timeout)
                    if request_timeout is not None
                    else float(self._config.tool_timeout_seconds)
                ),
                x_title=x_title,
                reasoning_effort=effective_effort,
            )
        except (IndexError, KeyError, TypeError, ValueError) as e:
            e._crabcakes_context = {
                "provider": caller_key,
                "model": model,
                "exception_type": type(e).__name__,
            }
            self._attach_provider_failure(e, session_key, "request_boundary")
            raise

    def _call_llm_streaming(
        self,
        session_key: str,
        base_url: str,
        api_key: str,
        model: str,
        caller_key: str,
        messages: list[dict],
        tools: list[dict] | None,
        timeout: float,
        x_title: str = "",
        turn_token: object | None = None,
        reasoning_effort: str = "off",
    ) -> dict:
        """
        Call the LLM with streaming. Fires on_text_delta as chunks arrive,
        on_tool_call_start when a tool call is complete, and returns the
        assembled response dict when done.

        Parameter contract: see StreamingCallKwargs — the fields there must
        match this method's parameters exactly. The regression test
        (TestStreamingSignature) derives expected_params from the TypedDict.

        Returns:
            Assembled response dict compatible with extract_tool_calls / extract_text_content.
        """
        # PHASE-11: caller_key is resolved by _call_llm before calling this method
        # (explicit caller > default_model prefix > model prefix). Symmetric with
        # the non-streaming path.
        provider = _get_provider(caller_key)
        streamer = provider.stream
        if streamer is None:
            raise ValueError(
                f"No streaming caller for caller_key={caller_key!r} "
                f"(model={model!r}). Check provider's 'caller' field in Settings → Providers."
            )

        full_content = ""
        # tool_call_index → {name, arguments, done}
        tool_calls_partial: dict[int, dict] = {}
        # Phase CB-3: usage captured from SSE "usage" event (BUG #3 fix).
        captured_usage: dict = {}
        # OpenRouter mid-stream error details (finish_reason="error").
        captured_error: dict = {}

        _stream = stream_with_ssl_retry(
            streamer,
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            tools=tools,
            timeout=timeout,
            x_title=x_title,
            reasoning_effort=reasoning_effort,
        )

        for ev in _stream:
            if ev.type == "text_delta":
                text = ev.data.get("content") or ""
                full_content += text
                self._last_stream_text[session_key] = full_content
                if self._on_text_delta:
                    self._dispatch(self._on_text_delta, session_key, text, _turn_token=turn_token)

            elif ev.type == "tool_call_delta":
                # PHASE-11.5: default to 0 if streamer omits 'index' (e.g. Anthropic
                # single-tool responses). Without this, the runtime crashes mid-stream.
                # STREAM-ID-PRES: capture provider-assigned id from first delta;
                # subsequent deltas (which carry argument fragments) do not overwrite.
                idx = ev.data.get("index", 0)
                if idx not in tool_calls_partial:
                    tool_calls_partial[idx] = {"name": "", "arguments": "", "id": ""}
                tc = tool_calls_partial[idx]
                if ev.data.get("name"):
                    tc["name"] = ev.data["name"]
                if ev.data.get("arguments"):
                    tc["arguments"] += ev.data["arguments"]
                incoming_id = ev.data.get("id") or ""
                if incoming_id and not tc["id"]:
                    tc["id"] = incoming_id

            elif ev.type == "usage":
                # Provider sent a usage chunk (e.g., OpenAI's "final" frame).
                # Capture the most recent one; the final response uses it.
                # See SPEC-CONTEXT-BLOAT-PHASE-3.md §2.2 (BUG #3 fix).
                usage_data = ev.data.get("usage", {})
                if isinstance(usage_data, dict) and usage_data:
                    captured_usage = usage_data

            elif ev.type == "error":
                # OpenRouter mid-stream error (finish_reason="error").
                # Captures the error details so they can be surfaced
                # to the user instead of the generic "no content" message.
                error_data = ev.data.get("error", {})
                if isinstance(error_data, dict) and error_data:
                    captured_error = error_data
                    logger.warning(
                        "[stream] sk=%s OpenRouter mid-stream error: code=%s message=%s",
                        session_key,
                        error_data.get("code", "?"),
                        error_data.get("message", "")[:200],
                    )

            elif ev.type == "done":
                # Build final tool_calls list from accumulated partials.
                # STREAM-ID-PRES: use the provider-assigned id captured during
                # SSE assembly; fall back to synthetic only if absent.
                tool_calls = []
                for idx in sorted(tool_calls_partial.keys()):
                    tc = tool_calls_partial[idx]
                    if tc["name"] and _validate_streamed_arguments(
                        tc["arguments"], tc["name"], session_key
                    ):
                        tool_calls.append({
                            "id": tc["id"] or f"call_{idx}",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"]
                            }
                        })
                logger.debug("[stream] sk=%s done: text_len=%d tool_calls=%d usage_captured=%s error_captured=%s",
                             session_key, len(full_content), len(tool_calls),
                             bool(captured_usage), bool(captured_error))
                result = {
                    "choices": [{"message": {"content": full_content, "tool_calls": tool_calls}}],
                    "usage": captured_usage,
                }
                if captured_error:
                    result["_stream_error"] = captured_error
                # Drain remaining events after done (defense in depth).
                # Some providers may send trailing usage frames, error events,
                # or duplicate done events. Drain and capture late error events
                # so they don't get silently discarded.
                for ev in _stream:
                    if ev.type == "error":
                        error_data = ev.data.get("error", {})
                        if isinstance(error_data, dict) and error_data and not captured_error:
                            captured_error = error_data
                    elif ev.type == "usage":
                        usage_data = ev.data.get("usage", {})
                        if isinstance(usage_data, dict) and usage_data:
                            captured_usage = usage_data
                # Write back late-captured values — the drain updates local
                # variables only; the `result` dict was built before the drain.
                if captured_error and "_stream_error" not in result:
                    result["_stream_error"] = captured_error
                if captured_usage and not result.get("usage"):
                    result["usage"] = captured_usage
                return result

        # Fallback — stream ended without explicit done event (e.g. provider doesn't send [DONE])
        # STREAM-ID-PRES: same id-preservation logic as the done-event path.
        tool_calls = []
        for idx in sorted(tool_calls_partial.keys()):
            tc = tool_calls_partial[idx]
            if tc["name"] and _validate_streamed_arguments(
                tc["arguments"], tc["name"], session_key
            ):
                tool_calls.append({
                    "id": tc["id"] or f"call_{idx}",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"]
                    }
                })
        logger.debug("[stream-fallback] sk=%s text_len=%d tool_calls=%d error_captured=%s (no done event)",
                     session_key, len(full_content), len(tool_calls), bool(captured_error))
        result = {"choices": [{"message": {"content": full_content, "tool_calls": tool_calls}}], "usage": captured_usage}
        if captured_error:
            result["_stream_error"] = captured_error
        return result

    def _check_stuck(self, session_key: str, tool_name: str, args: dict, iteration: int) -> str | None:
        """
        §E — Stuck detection.

        Monitor tool call history for signs the agent is looping:
        - Same tool + same args 3+ times in last 10 calls → intervention
        - 8+ write_file calls with no exec_command in last 8 → intervention

        Returns an intervention message string, or None if not stuck.
        """
        with self._tool_history_lock:
            history = self._tool_history.setdefault(session_key, [])
            args_str = str(sorted(args.items()))
            args_hash = hashlib.md5(args_str.encode()).hexdigest()[:8]
            history.append({"tool": tool_name, "args_hash": args_hash, "iteration": iteration})

            # Keep only last 20 entries
            if len(history) > 20:
                history[:] = history[-20:]

            # Check 1: same tool + same args 3+ times in last 10
            recent = history[-10:]
            same_count = sum(
                1 for e in recent
                if e["tool"] == tool_name and e["args_hash"] == args_hash
            )
            if same_count >= 3:
                return (
                    f"[stuck-detection] You've called {tool_name} with the same arguments "
                    f"{same_count} times in recent iterations. You appear to be stuck. "
                    f"Consider: re-reading the file, checking the error message carefully, "
                    f"or trying a completely different approach. "
                    f"If you've tried 3+ approaches without progress, report as blocked."
                )

            # Check 2: 8+ write operations with no verification commands
            recent_tools = [e["tool"] for e in recent]
            write_ops = recent_tools.count("write_file") + recent_tools.count("edit_file")
            if write_ops >= 8 and "exec_command" not in recent_tools[-8:]:
                return (
                    "[stuck-detection] You've written files 8+ times without running any "
                    "commands to verify. Run tests or check syntax before continuing."
                )

            return None

    def drop_compile(self, session_key: str) -> None:
        """Drop this session's compiled wire packet. A missing key is a no-op.

        Trigger: clear_conversation's reset path, not the active-loop refusal.
        """
        self._compile.pop(session_key, None)

    def _format_checks(self, checks: list) -> str:
        blocks = []
        for item in checks:
            if not isinstance(item, dict):
                continue
            block = f"[{item.get('tier', '')}] passed={item.get('passed')} {item.get('detail', '')}"
            output = item.get("output") or ""
            if isinstance(output, str) and output:
                block = block + "\n" + output
            blocks.append(block)
        return "\n".join(blocks)

    def drop_repo_map(self, session_key: str) -> None:
        """Drop the cached repo-map block. A missing key is a no-op.

        Trigger: clear_conversation's reset path. A tool iteration must
        reuse the block; only a cleared conversation drops it.
        """
        self._repo_map_block.pop(session_key, None)
        self._repo_map_key.pop(session_key, None)

    def _repo_map_block_for(self, session_key: str, conv) -> str:
        from models.conversation import MessageRole
        goal = ""
        for msg in reversed(conv.messages):
            if msg.role == MessageRole.USER:
                goal = msg.content or ""
                break
        project = conv.project_path or ""
        if not project or not isinstance(goal, str) or not goal:
            return ""
        with self._repo_map_lock:
            tree_version = self._repo_map_tree_version
        key = (project, hash(goal), tree_version)
        if self._repo_map_key.get(session_key) == key:
            return self._repo_map_block.get(session_key, "")
        try:
            from agent.repo_map import build_repo_map
            block = build_repo_map(project, goal)
        except Exception:
            logger.exception("build_repo_map failed for %s", session_key)
            block = ""
        if not isinstance(block, str):
            block = ""
        self._repo_map_key[session_key] = key
        self._repo_map_block[session_key] = block
        return block

    def _prefix_repo_map(self, messages: list[dict], block: str) -> list[dict]:
        if not block:
            return messages
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                content = messages[i].get("content") or ""
                if not isinstance(content, str):
                    content = str(content)
                messages[i] = {
                    "role": "user",
                    "content": REPO_MAP_HEADER + "\n\n" + block + "\n\n" + content,
                }
                break
        return messages

    def _compiled_messages(self, conv, packet: dict, map_block: str = "") -> list[dict]:
        from models.conversation import serialize_messages
        tail = conv.messages[packet["after"]:]
        fact_lines = ""
        if map_block:
            fact_lines = REPO_MAP_HEADER + "\n\n" + map_block + "\n\n"
        body = (
            packet["goal"]
            + "\n\n"
            + fact_lines
            + WRITE_REPLAY_PREAMBLE
            + "\n"
            + "## Write\n"
            + packet["hunk"]
            + "\n\n## Checks\n"
            + self._format_checks(packet["checks"])
        )
        messages: list[dict] = []
        if conv.system_prompt:
            messages.append({"role": "system", "content": conv.system_prompt})
        messages.append({"role": "user", "content": body})
        messages.extend(serialize_messages(tail, index_base=len(messages)))
        return messages

    def _wire_char_tokens(self, messages: list[dict]) -> int:
        total = 0
        for entry in messages:
            content = entry.get("content") or ""
            if isinstance(content, str):
                total += len(content)
            for tc in entry.get("tool_calls") or []:
                fn = tc.get("function") or {}
                raw = fn.get("arguments") or ""
                if not isinstance(raw, str):
                    raw = json.dumps(raw)
                total += len(raw)
        return total // 4

    def _wire_text(self, messages: list[dict]) -> str:
        """The exact text _wire_char_tokens measures, as one string.

        Split out of _wire_char_tokens so the tiktoken path encodes the
        same bytes the chars//4 path counts. Keep the two in step: a
        divergence here is exactly the BUG #6 skew.
        """
        chunks: list[str] = []
        for entry in messages:
            content = entry.get("content") or ""
            if isinstance(content, str):
                chunks.append(content)
            for tc in entry.get("tool_calls") or []:
                fn = tc.get("function") or {}
                raw = fn.get("arguments") or ""
                if not isinstance(raw, str):
                    raw = json.dumps(raw)
                chunks.append(raw)
        return "".join(chunks)

    def _compile_token_count(
        self, conv, messages: list[dict], tools: list | None,
    ) -> int:
        """Tokens for one compiled call: wire text + schemas, ONE estimator.

        Debugger audit BUG #5 and BUG #6 (SPEC-24 compiled branch).

        BUG #6 — the two halves used different estimators. The base was
        always chars//4 (_wire_char_tokens) while _schema_tokens went
        through tiktoken when installed, so the same JSON was charged at
        two different rates (measured 2,023 vs 1,019 tokens on one schema
        blob — a 2x over-count) and the total drifted toward a false
        over-budget error. The estimator is resolved ONCE here and applied
        to base and addons together.

        BUG #5 — the task card was never counted on this branch.
        to_api_messages() prefixes the card onto the last user message
        and get_token_estimate() counts it, so the plain branch was
        correct; _compiled_messages() builds its own wire list and does
        not, so a 2,000-char card rode the wire uncounted.

        The card is counted HERE as an addon rather than folded into
        _compiled_messages() for one reason: _compiled_messages() is
        what actually goes on the wire, and it does not carry the card.
        Counting it here keeps that function honest about what it emits,
        and the wire stays byte-identical to today's compiled wire.
        (The repo map needs no addon — _compiled_messages() already
        embeds it in fact_lines, so the wire text already contains it and
        the base count covers it. Adding a map addon here would be the
        double-count requirement (4) warns about.)
        """
        from models.conversation import _tiktoken_encoding_for
        schema_json = json.dumps(tools) if tools else ""
        card = getattr(conv, "task_card", "") or ""
        encoding = _tiktoken_encoding_for(getattr(conv, "model", "") or "")
        if encoding is None:
            return (
                self._wire_char_tokens(messages)
                + len(card) // 4
                + len(schema_json) // 4
            )
        wire = self._wire_text(messages)
        return (
            conv._count_text(encoding, wire)
            + conv._count_text(encoding, card)
            + conv._count_text(encoding, schema_json)
        )

    def _over_budget_error(self, used: int, model_max: int) -> RuntimeError:
        usage_pct = int((used / model_max) * 100) if model_max > 0 else 0
        return RuntimeError(
            f"Conversation is at {used:,}/{model_max:,} tokens "
            f"({usage_pct}%) after compaction — exceeds model context window. "
            f"Use /clear to reset or /compact to summarize the conversation."
        )

    def _addon_tokens(self, conv, text: str) -> int:
        """Token count for wire text that is not already in the estimate.

        Uses the conversation's tiktoken path when that path is available,
        and chars // 4 otherwise. Same split as get_token_estimate.
        """
        if not text:
            return 0
        from models.conversation import _tiktoken_encoding_for
        encoding = _tiktoken_encoding_for(getattr(conv, "model", "") or "")
        if encoding is None:
            return len(text) // 4
        return conv._count_text(encoding, text)

    def _schema_tokens(self, conv, tools: list | None) -> int:
        """Tokens for the tool-schema JSON sent beside the messages.

        The schemas are not part of conv.system_prompt and must stay a
        separate API field. None means the caller is not sending tools.
        """
        if not tools:
            return 0
        return self._addon_tokens(conv, json.dumps(tools))

    def _messages_for_model(
        self,
        session_key: str,
        conv,
        model_max: int,
        tools: list | None = None,
    ) -> list[dict]:
        """Wire list for the next model call.

        After an enforced write, this is the compiled packet plus messages
        appended after that write. Otherwise it is to_api_messages().

        Plain branch: get_token_estimate() already counts the task card,
        so the map and the tool schemas are added on top of it and the card
        is not counted again.

        Compiled branch: the wire list is built by _compiled_messages(),
        which carries the repo map inside fact_lines but no task card, so
        the count comes from _compile_token_count() — the card added once,
        the map covered by the wire, one estimator for all of it.
        """
        effective = model_max - self._RESPONSE_RESERVE_TOKENS
        block = self._repo_map_block_for(session_key, conv)
        packet = self._compile.get(session_key)
        if packet is None:
            schema_tokens = self._schema_tokens(conv, tools)
            map_text = ""
            if block:
                map_text = REPO_MAP_HEADER + "\n\n" + block + "\n\n"
            post = conv.get_token_estimate() + self._addon_tokens(conv, map_text) + schema_tokens
            if post >= effective:
                raise self._over_budget_error(post, model_max)
            return self._prefix_repo_map(conv.to_api_messages(), block)
        messages = self._compiled_messages(conv, packet, block)
        # The repo map rides inside the packet's fact_lines, so the wire
        # count already covers it; the card and the schema JSON are the
        # two addons, both at the same estimator rate as the wire.
        compiled = self._compile_token_count(conv, messages, tools)
        if compiled >= effective:
            raise self._over_budget_error(compiled, model_max)
        return messages

    def _maybe_compile(self, session_key: str, conv, tool_name: str, args, result) -> None:
        """Store a compiled packet after an enforced write. No checks means no change."""
        if tool_name not in ("write_file", "edit_file"):
            return
        checks = getattr(result, "enforcement_checks", None) or []
        if not checks:
            return
        from models.conversation import MessageRole
        goal = None
        for msg in reversed(conv.messages):
            if msg.role == MessageRole.USER:
                goal = msg.content or ""
                break
        if goal is None:
            return
        if not isinstance(args, dict):
            args = {}
        path = args.get("path", "")
        if not isinstance(path, str):
            path = ""
        if tool_name == "edit_file":
            old = args.get("old_text", "")
            new = args.get("new_text", "")
            if not isinstance(old, str):
                old = "" if old is None else str(old)
            if not isinstance(new, str):
                new = "" if new is None else str(new)
            hunk = f"--- old_text\n{old[:4000]}\n--- new_text\n{new[:4000]}"
        else:
            content = args.get("content", "")
            if not isinstance(content, str):
                content = "" if content is None else str(content)
            byte_count = len(content.encode("utf-8"))
            hunk = f"write_file replaced {path} ({byte_count} bytes)"
        stored = []
        for check in checks:
            output = getattr(check, "output", "") or ""
            if not isinstance(output, str):
                output = str(output)
            detail = getattr(check, "detail", "") or ""
            if not isinstance(detail, str):
                detail = str(detail)
            stored.append({
                "tier": getattr(check, "tier", ""),
                "passed": bool(getattr(check, "passed", False)),
                "detail": detail,
                "output": output[:2000],
            })
        self._compile[session_key] = {
            "goal": goal,
            "path": path,
            "hunk": hunk,
            "checks": stored,
            "after": len(conv.messages),
        }
        try:
            from utils.project_awareness import record_enforcement_fact
            record_enforcement_fact(
                conv.project_path or "",
                session_key,
                path,
                checks,
            )
        except Exception:
            logger.exception("record_enforcement_fact failed for %s", session_key)

    def drop_read_cache(self, session_key: str) -> None:
        """Drop this session's read_file fingerprints. A missing key is a no-op.

        Trigger: clear_conversation's reset path (not the active-loop refusal).
        A cleared transcript must not answer "unchanged" for a file it never saw.
        """
        with self._tool_history_lock:
            self._read_fingerprints.pop(session_key, None)
            self._read_counts.pop(session_key, None)
            self._hot_pins.pop(session_key, None)
            self._edited_paths.pop(session_key, None)

    def _note_tool_path(self, session_key: str, conv, tool_name: str, args) -> None:
        """Count a successful read or exec, or remember a successful edit.

        A read with an empty project path or a failed stat does not count.
        The first counted read of a path stores ``(mtime_ns, size)``. A later
        read of a changed file drops that pin and re-pins the new bytes at
        count 1. ``exec_command`` is counted under ``<exec:session>`` and is
        not a file pin.
        """
        if not isinstance(args, dict):
            return
        if tool_name == "exec_command":
            self._note_exec_pin(session_key, conv)
            return
        path = args.get("path", "")
        if not isinstance(path, str) or not path:
            return
        if tool_name in ("edit_file", "write_file"):
            with self._tool_history_lock:
                self._edited_paths.setdefault(session_key, set()).add(path)
            return
        if tool_name != "read_file":
            return
        project = getattr(conv, "project_path", None) or ""
        if not project:
            return
        try:
            st = os.stat(os.path.join(project, path))
        except OSError as exc:
            logger.debug("hot-path pin skipped for %s: %s", path, exc)
            return
        fp = (st.st_mtime_ns, st.st_size)
        with self._tool_history_lock:
            pins = self._hot_pins.setdefault(session_key, {})
            counts = self._read_counts.setdefault(session_key, {})
            stored = pins.get(path)
            if stored is not None and stored != fp:
                pins.pop(path, None)
                counts[path] = 1
                pins[path] = fp
                return
            if stored is not None:
                return
            counts[path] = counts.get(path, 0) + 1
            if counts[path] >= 1:
                pins[path] = fp

    def _note_exec_pin(self, session_key: str, conv) -> None:
        """Count a successful exec under ``<exec:session>``.

        There is no file to stat. The pin is stored so ``_protect_paths``
        can drop it on ``OSError`` — an exec result is not path-protected.
        """
        project = getattr(conv, "project_path", None) or ""
        if not project:
            return
        path = f"<exec:{session_key}>"
        with self._tool_history_lock:
            pins = self._hot_pins.setdefault(session_key, {})
            counts = self._read_counts.setdefault(session_key, {})
            counts[path] = counts.get(path, 0) + 1
            if counts[path] >= 3:
                # Not a file fingerprint. _protect_paths stats this key and drops it.
                pins[path] = (0, 0)

    def _protect_paths(self, session_key: str, conv) -> tuple[str, ...]:
        """Edited paths plus pins whose mtime and size still match."""
        project = getattr(conv, "project_path", None) or ""
        with self._tool_history_lock:
            edited = set(self._edited_paths.get(session_key, ()))
            pins = dict(self._hot_pins.get(session_key, {}))
        live: list[str] = []
        drop: list[str] = []
        for path, fp in pins.items():
            if not project:
                drop.append(path)
                continue
            try:
                st = os.stat(os.path.join(project, path))
            except OSError:
                drop.append(path)
                continue
            if (st.st_mtime_ns, st.st_size) != fp:
                drop.append(path)
                continue
            live.append(path)
        if drop:
            with self._tool_history_lock:
                bucket = self._hot_pins.get(session_key)
                counts = self._read_counts.setdefault(session_key, {})
                if bucket is not None:
                    for path in drop:
                        bucket.pop(path, None)
                        counts[path] = 0
        return tuple(edited | set(live))

    def _compact_before_llm(
        self,
        session_key: str,
        conv,
        soft_ceiling: int,
        hard_ceiling: int,
    ) -> None:
        """Under the provider line, compact toward that line (usually a no-op).

        Over the line, stub older tool results and compact toward the landing goal
        without deleting the current turn or a protected path.
        """
        landing_goal = self._compute_landing_goal(hard_ceiling)
        estimate = conv.get_token_estimate()
        if estimate > soft_ceiling and soft_ceiling > landing_goal:
            protect = self._protect_paths(session_key, conv)
            self._context_strategy.panic_prune(conv, protect_paths=protect)
            self._context_strategy.compact(
                conv,
                landing_goal,
                protect_paths=protect,
                preserve_current_turn=True,
            )
            return
        self._context_strategy.compact(conv, soft_ceiling)

    def _task_line(self, tool_name: str, args: dict, result) -> str:
        """One progress line for the session task card, or "" to skip."""
        if not isinstance(args, dict):
            args = {}
        path = args.get("path", "")
        if not isinstance(path, str):
            path = ""
        if tool_name == "read_file":
            offset = args.get("offset")
            return f"read {path} offset={offset or 0}"
        if tool_name in ("edit_file", "write_file"):
            return f"{tool_name} {path}"
        if tool_name == "exec_command":
            command = args.get("command", "")
            if not isinstance(command, str):
                command = ""
            command = command.replace("\n", " ")[:80]
            return f"exec exit={result.exit_code} {command}"
        return ""

    def _note_task(self, session_key: str, line: str) -> None:
        """Append one progress line to the session task card (max 500 chars).

        The card is a scratch list for this conversation, not project memory.
        An empty line is a no-op. A missing conversation is a no-op. The cut
        keeps the tail, on a newline boundary, so the newest lines stay.
        """
        if not isinstance(line, str):
            return
        line = line.replace("\n", " ").strip()
        if not line:
            return
        conv = self.get_conversation(session_key)
        if conv is None:
            return
        card = conv.task_card or ""
        if not isinstance(card, str):
            card = ""
        card = f"{card}\n{line}" if card else line
        if len(card) > 500:
            tail = card[-500:]
            nl = tail.find("\n")
            if nl != -1:
                tail = tail[nl + 1:]
            card = tail
        conv.task_card = card
        conv._token_estimate_cache = None

    def _key_body_in_transcript(
        self, conv: Any, path: str, offset: int | None, limit: int | None,
    ) -> bool:
        """SPEC-32 wrapper. Key is ``(path, offset or 0, limit)`` for read_file."""
        return self._body_live_in_transcript(
            conv, "read_file", (path, offset or 0, limit),
        )

    def _body_live_in_transcript(self, conv: Any, tool: str, key: tuple) -> bool:
        """SPEC-33b: does a real body for this (tool, key) still live here?

        Newest to oldest. Skips [unchanged \u2014 notices and any other tool.
        The first remaining result for the key decides: a [masked \u2014 or
        [compacted \u2014 stub is False; any other content is True.
        Nothing left, or any exception, is False.
        ``key`` is ``(path, offset or 0, limit)`` for read_file and
        ``("", 0, None)`` for exec_command.
        """
        try:
            from models.conversation import MessageRole
            strategy = self._context_strategy
            for i in range(len(conv.messages) - 1, -1, -1):
                msg = conv.messages[i]
                if getattr(msg, "role", None) != MessageRole.TOOL_RESULT:
                    continue
                content = msg.content or ""
                if content.startswith("[unchanged \u2014"):
                    continue
                name, rpath, roffset, _parent = strategy._tool_result_meta(conv, i)
                if name != tool:
                    continue
                rlimit = strategy._tool_result_limit(conv, i)
                if (rpath, roffset, rlimit) != key:
                    continue
                return not (
                    content.startswith("[masked \u2014")
                    or content.startswith("[compacted \u2014")
                )
        except Exception:
            try:
                logger.debug("recovery walk failed for %s", key, exc_info=True)
            except Exception:
                pass
        return False

    def _dedup_read_output(self, session_key: str, conv, args: dict, output: str) -> str:
        """Return the unchanged notice when this turn already read the same bytes.

        Key is (path, offset or 0, limit). Value is (st_mtime_ns, st_size).
        An empty project_path or a failed stat skips dedup and returns output.
        A fingerprint match returns output when no live read_file body for
        that key remains in the transcript.
        """
        if not isinstance(args, dict):
            return output
        path = args.get("path", "")
        offset = args.get("offset")
        limit = args.get("limit")
        if not isinstance(path, str):
            return output
        if offset is not None and (isinstance(offset, bool) or not isinstance(offset, int)):
            return output
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)):
            return output
        project = conv.project_path or ""
        if not project:
            return output
        try:
            st = os.stat(os.path.join(project, path))
        except OSError as exc:
            logger.debug("read_file dedup skipped for %s: %s", path, exc)
            return output
        key = (path, offset or 0, limit)
        fp = (st.st_mtime_ns, st.st_size)
        with self._tool_history_lock:
            bucket = self._read_fingerprints.setdefault(session_key, {})
            if bucket.get(key) == fp:
                if not self._key_body_in_transcript(conv, path, offset, limit):
                    # SPEC-32: the body is masked, compacted, evicted, or
                    # the walk could not prove a live read_file body.
                    # Re-sending once is cheaper than an exec_command loop.
                    bucket[key] = fp
                    return output
                return (
                    f"[unchanged \u2014 read_file {path} offset={offset or 0} "
                    f"limit={limit}]"
                )
            bucket[key] = fp
        return output

    def _cleanup_tool_history(self, session_key: str) -> None:
        """Remove tool history and pending stuck messages for a session when conversation ends."""
        with self._tool_history_lock:
            self._tool_history.pop(session_key, None)
            self._read_fingerprints.pop(session_key, None)
        # Phase CB-3: also clean up pending stuck messages
        self._pending_stuck_messages.pop(session_key, None)

    def _check_and_stop_on_limit(
        self, session_key: str, conv: Any,
    ) -> tuple[str, str] | None:
        """Check cost and step limits. Pure predicate — no side effects.

        Returns:
            ``None`` if the turn should continue.
            ``(stopped_reason, error_message)`` if a limit is exceeded, where
            ``stopped_reason`` is ``"cost_limit"`` or ``"step_limit"``.

        Audit fixes (BUG #6, #11): the previous version dispatched
        ``on_error`` (with an undefined ``turn_token`` — NameError on any
        path that actually hit a limit), called ``_auto_save``, and added
        an assistant message placeholder. All side effects removed; the
        caller (``_run_loop``) builds the ``TurnResult`` and routes
        through ``_terminate_turn``.
        """
        if self._config.cost_limit is not None and conv.total_cost > self._config.cost_limit:
            reason = (
                f"Cost limit exceeded: ${conv.total_cost:.4f} "
                f"> ${self._config.cost_limit:.4f}"
            )
            return ("cost_limit", reason)
        if self._config.step_limit is not None and conv.step_count > self._config.step_limit:
            reason = (
                f"Step limit exceeded: {conv.step_count} > {self._config.step_limit}"
            )
            return ("step_limit", reason)
        return None

    def _auto_save(self, session_key: str, conv: Any) -> None:
        """Save conversation if auto_save is enabled."""
        if self._config.auto_save_conversations:
            try:
                save_conversation_to_disk(conv, session_key)
            except Exception:
                logger.exception("Failed to auto-save conversation %s", session_key)

    # ── Persistence ────────────────────────────────────────────────────────────

    def save_conversation(self, session_key: str) -> str:
        """Save a conversation to disk. Returns the file path."""
        with self._lock:
            conv = self._conversations.get(session_key)
            if conv is None:
                raise ValueError(f"No conversation found for {session_key}")
            path = save_conversation_to_disk(conv, session_key)
        return path

    def load_conversation(self, session_key: str) -> bool:
        """Load a conversation from disk into the runtime. Returns True if found."""
        result = load_conversation_from_disk(session_key)
        if result is None:
            return False
        conv, _ = result
        with self._lock:
            self._conversations[session_key] = conv
        return True

    def _rebuild_conversation_context(
        self,
        session_key: str,
        project_path: str | None,
        agent_role: str = "",
    ) -> None:
        """Re-apply the active project to a loaded conversation and rebuild its system prompt.

        Fix for the stale-project-context bug: conversations persisted to disk
        carry a `project_path` and `system_prompt` snapshot from the session
        that last wrote them. If the user switched projects between sessions,
        those persisted values are stale — the agent would see the wrong
        project's docs and tools would be sandboxed to the wrong directory.

        This method is idempotent and cheap (one build_system_prompt call +
        one attribute write). It is called from:
          - load_conversation: after restoring from disk, before any send
          - send_message: lazy reconciliation on first send after a project switch
          - AgentRuntimeHandler.set_active_project: eager reconciliation for
            all agents when the user opens a project

        Args:
            session_key: The conversation to reconcile.
            project_path: The currently-active project path. If None, the
                conversation's project_path is cleared and the system prompt
                is rebuilt without project context.
            agent_role: Role identifier (e.g. "coder", "debugger", "helper").
                Used to select the right per-role template in build_system_prompt.

        Concurrency contract (BUG #2 audit, 2026-07-02): `self._lock` is a
        `threading.Lock`, not an RLock — it is NOT re-entrant. The locked
        block below is intentionally narrow (a single dict.get on
        `self._conversations`). Do NOT widen it: do not call other
        AgentRuntime methods from inside the block, do not write to
        `self._conversations[session_key]` here, and do not invoke any
        callback that might re-enter the runtime. If you need to mutate
        `_conversations` under the lock, do the lookup here and the
        mutation outside (the existing pattern in `get_conversation`,
        `load_conversation`, `send_message`, etc.). Changing `self._lock`
        to `threading.RLock` would require auditing every lock site for
        correctness — out of scope for this fix.
        """
        with self._lock:
            conv = self._conversations.get(session_key)
        if conv is None:
            return  # Nothing to rebuild; load_conversation not called yet.

        if conv.project_path == project_path and conv.system_prompt:
            return  # Already in sync; skip the rebuild (cheap short-circuit).

        # SPEC-38: resolve the budget window via the shared resolver for THIS
        # conversation's provider — same path as create_conversation() and
        # _ensure_system_prompt(), so all three produce equivalent prompts.
        model_max_for_budget, context_mode = self._budget_window_and_mode(conv)

        failed = False
        try:
            from agent.context import build_system_prompt
            new_prompt = build_system_prompt(
                conv.agent_name,
                project_path,
                self._prompt_tool_names(conv),
                review_mode="off",
                agent_role=agent_role or conv.agent_role or "",
                model_max_tokens=model_max_for_budget,
                context_mode=context_mode,
            )
        except Exception:
            # build_system_prompt is non-critical — fall back to whatever
            # was persisted, with a logged warning. The agent still works
            # (just with a potentially stale prompt) instead of crashing.
            # The previous fingerprint stays with the previous string.
            logger.exception(
                "Failed to rebuild system prompt for %s; keeping persisted prompt",
                session_key,
            )
            new_prompt = conv.system_prompt
            failed = True

        conv.project_path = project_path
        conv.system_prompt = new_prompt
        if not failed:
            conv._system_prompt_fingerprint = self._system_prompt_fingerprint(conv)
            conv._token_estimate_cache = None
        logger.info(
            "Reconciled conversation context for %s: project_path=%r",
            session_key, project_path,
        )

    def list_conversations(self) -> list[tuple[str, str]]:
        """List all saved conversations: [(session_key, agent_name)]."""
        d = conversations_dir()
        try:
            files = [f for f in os.listdir(d) if f.endswith(".json")]
        except OSError:
            return []

        result = []
        for fname in files:
            sk = fname[:-5]  # strip .json
            # W13: lightweight read — only extract agent_name, skip full
            # Conversation/Message deserialization + api_key re-resolution.
            agent_name = "unknown"
            try:
                path = os.path.join(d, fname)
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                agent_name = data.get("agent_name", "unknown")
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                pass
            result.append((sk, agent_name))
        return result

    def approve_exec(self, session_key: str, tool_name: str, args: dict, approved: bool) -> None:
        """
        Resolve a pending approval when the PM clicks Approve or Deny.

        Called by AgentRuntimeHandler.approve_exec() via the feed UI.
        Sets result_ref so _dispatch_approval's waiting thread unblocks,
        then removes the entry from _pending_approvals.
        """
        with self._lock:
            for key, pending in list(self._pending_approvals.items()):
                if key.startswith(session_key):
                    pending["result_ref"][0] = approved
                    pending["event"].set()
                    self._pending_approvals.pop(key, None)
                    logger.info("Approval resolved for %s: %s", session_key, approved)
                    return

    def force_compact(self, conv: "Conversation", token_budget: int) -> None:
        """Public wrapper around self._context_strategy.compact().

        Spec: docs/specs/SPEC-CONTEXT-UI-COMPACT-LLM-2026-07-10.md §3.2.
        Allows external callers (like compact_conversation in
        agent_runtime_handler) to invoke compaction without poking at
        the private _context_strategy attribute.

        token_budget <= 0 is silently ignored (matches the strategy's
        own defensive behavior — see context_strategy.py:130).
        """
        with self._compaction_lock:
            self._context_strategy.compact(conv, token_budget)

    def force_llm_compact(
        self,
        conv: "Conversation",
        token_budget: int,
        focus_text: str = "",
        agent_def: Any = None,
    ) -> dict:
        """Force an LLM-summarization compact on ``conv``.

        Spec: docs/specs/SPEC-CONTEXT-UI-COMPACT-LLM-2026-07-10.md §3.3.2.

        FIX-BUG-3: swap self._context_strategy to the LLM strategy for
        the duration of the call, run compact, then swap back. This
        ensures self._context_strategy.last_result reflects the LLM
        compaction, which the runtime's breakdown dispatcher (line 2116)
        reads into self._compaction_events.
        """
        from agent.context_strategy import LLMSummarizeStrategy

        with self._compaction_lock:
            original_strategy = self._context_strategy

            # Resolve model_id with precedence: agent_def.llm_name > conv.model > global default.
            resolved_model = None
            if agent_def is not None:
                llm_name = getattr(agent_def, "llm_name", None)
                if llm_name:
                    prov_cfg = self._config.providers.get(llm_name)
                    if prov_cfg and prov_cfg.default_model:
                        if "/" in prov_cfg.default_model:
                            resolved_model = prov_cfg.default_model
                        else:
                            resolved_model = f"{llm_name}/{prov_cfg.default_model}"
            if not resolved_model:
                resolved_model = conv.model

            strat = LLMSummarizeStrategy(
                llm_provider=lambda sys_p, user_p, model_id=None:
                    self._call_for_summary(
                        system_prompt=sys_p,
                        user_prompt=user_p,
                        model_id=model_id or resolved_model,
                        conv=conv,
                    ),
            )
            self._context_strategy = strat

            original_sp = conv.system_prompt
            if focus_text:
                conv.system_prompt = (
                    f"{original_sp}\n\n## Focus for compaction\n{focus_text}"
                )
            try:
                strat.compact(conv, token_budget)
            finally:
                self._context_strategy = original_strategy
                conv.system_prompt = original_sp

        ev = strat.last_result
        if ev is None:
            return {
                "messages_removed": 0,
                "tokens_freed": 0,
                "summary_chars": 0,
                "layer": 0,
            }
        return {
            "messages_removed": ev.messages_removed,
            "tokens_freed": ev.tokens_freed,
            "summary_chars": ev.summary_tokens_injected,
            "layer": ev.layer,
        }

    def _call_for_summary(
        self,
        system_prompt: str,
        user_prompt: str,
        model_id: str | None = None,
        conv: "Conversation | None" = None,
    ) -> str:
        """Single non-streaming chat completion for LLMSummarizeStrategy.

        Spec: docs/specs/SPEC-CONTEXT-UI-COMPACT-LLM-2026-07-10.md §3.3.2.
        FIX-BUG-2: reuses agent/runtime.py's real provider caller
        dispatch (_PROVIDER_CALLERS, _resolve_caller_key) — does NOT
        create a new sync_chat_completion helper that does not exist.

        Returns the assistant text. Raises any provider error.
        """
        if not model_id and self._config.default_provider:
            model_id = f"{self._config.default_provider}/{self._config.default_model}"
        if not model_id:
            raise RuntimeError(
                "_call_for_summary: no model_id and no default configured"
            )
        if "/" not in model_id:
            raise RuntimeError(
                f"_call_for_summary: model_id must be 'provider/model', "
                f"got {model_id!r}"
            )
        provider_name, model = model_id.split("/", 1)
        provider_cfg = self._config.providers.get(provider_name)
        if provider_cfg is None:
            raise RuntimeError(
                f"_call_for_summary: provider {provider_name!r} not configured"
            )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        caller_key = self._resolve_caller_key(provider_cfg, model)
        caller = _PROVIDER_CALLERS.get(caller_key)
        if caller is None:
            raise ValueError(
                f"_call_for_summary: no caller for {caller_key!r}"
            )

        api_key = ""
        if conv is not None and getattr(conv, "api_key", None):
            api_key = conv.api_key
        if not api_key:
            api_key = getattr(provider_cfg, "api_key", "") or ""
        if not api_key:
            logger.warning(
                "_call_for_summary: empty api_key for %s; check Settings",
                provider_name,
            )

        provider = _get_provider(caller_key)
        summary_session = getattr(conv, "session_key", None) or "summary"
        try:
            response_dict = provider.call(
                base_url=provider_cfg.base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                tools=None,
                timeout=float(self._config.tool_timeout_seconds),
                x_title="crabcakes-summary",
                reasoning_effort="off",
            )
        except Exception as exc:
            # D7: compaction stays non-fatal at the caller. Record, then re-raise
            # so LLMSummarizeStrategy can fall back to a textual summary.
            self._attach_provider_failure(exc, summary_session, "summary")
            self._record_attached_failure(exc)
            raise
        from agent.llm.extractors import extract_text_content
        fmt = _RESPONSE_FORMAT.get(provider_name, "openai")
        text = extract_text_content(response_dict, response_format=fmt)
        return text
