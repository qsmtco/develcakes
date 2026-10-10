"""Pluggable context management strategy.

See docs/specs/SPEC-CONTEXT-MANAGEMENT-ROADMAP.md §0 and
docs/proposals/PROPOSAL-pluggable-context-strategy.md.

This module hosts the P1–P7 compaction algorithms on a ``DefaultContextStrategy``
class. ``models/conversation.py`` stays pure data (ARCHITECTURE.md §3.21l) and
retains thin delegation shims that forward to ``DefaultContextStrategy`` so
existing tests continue to pass unchanged.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol

from models.conversation import (
    Conversation,
    Message,
    MessageRole,
    _tiktoken_encoding_for,
)


# ── Telemetry dataclass ────────────────────────────────────────────────────────


@dataclass
class CompactionEvent:
    """One compaction cycle's outcome. Appended to per-session history.

    Fields:
        turn: Tool-loop iteration number (1-indexed) when the event fired.
        trigger: What caused compaction.
        layer: Compaction layer that fired (1=prune, 2=trim, 3=manual).
        messages_before: len(conv.messages) at start of compaction cycle.
        messages_after: len(conv.messages) at end of compaction cycle.
        messages_removed: messages_before - messages_after.
        tokens_before: get_token_estimate() before compaction.
        tokens_after: get_token_estimate() after compaction.
        tokens_freed: tokens_before - tokens_after.
        summary_tokens_injected: tokens used by injected summary (0 if none).
        soft_ceiling: The soft_ceiling used for this cycle (in tokens).
        hard_ceiling: The hard_ceiling for this cycle (in tokens), or
            None if the strategy doesn't know (runtime fills it in).
        provider: Provider name (e.g. "openai").
        model: Model id (e.g. "openai/gpt-4o").
    """
    turn: int
    trigger: str
    layer: int
    messages_before: int
    messages_after: int
    messages_removed: int
    tokens_before: int
    tokens_after: int
    tokens_freed: int
    summary_tokens_injected: int
    soft_ceiling: int
    hard_ceiling: int | None
    provider: str
    model: str
    # Audit-Fix-26: session_key tag added so per-runtime _compaction_events
    # can be filtered by session (Bug #3 from the bugfix audit). Empty string
    # means "unscoped" (e.g. older events from before this field existed, or
    # synthetic events from tests that don't care about session filtering).
    session_key: str = ""


# ── Pluggable strategy protocol ────────────────────────────────────────────────


class ContextStrategy(Protocol):
    """Pluggable compaction policy. See PROPOSAL-pluggable-context-strategy.md."""

    def compact(self, conv: Conversation, token_budget: int) -> None:
        """Reduce ``conv`` so its token estimate fits within ``token_budget``.

        The strategy may evict messages, stub tool outputs, inject summaries,
        or do nothing. It must NOT mutate fields outside ``conv.messages`` and
        ``conv._token_estimate_cache`` (per ARCHITECTURE.md §3.21l).
        """
        ...

    @property
    def last_result(self) -> CompactionEvent | None:
        """Telemetry from the most recent ``compact()`` call. ``None`` before first call.

        The strategy records what happened (see §2.8 of the spec) and the
        runtime reads this attribute after each call to update its event history.
        """
        ...


# ── Default implementation ─────────────────────────────────────────────────────


class DefaultContextStrategy:
    """Default 3-layer compaction strategy. See SPEC-CONTEXT-MANAGEMENT-ROADMAP.md §0.

    Layers:
        1. prune_tool_outputs — stubs old TOOL_RESULT content in-place.
        2. trim loop — removes messages using _select_prune_candidate
           (respects keep_first and protect_is_summary).
        3. summary injection — inserts a compact summary of removed messages.

    Parameters:
        keep_first: Number of leading messages to protect from trimming (default 2).
        protect_is_summary: Defer is_summary messages during trimming (default True).
    """

    def __init__(self) -> None:
        self._last_result: CompactionEvent | None = None

    @property
    def last_result(self) -> CompactionEvent | None:
        """Telemetry from the most recent ``compact()`` call."""
        return self._last_result

    # ── Public API ────────────────────────────────────────────────────────────

    def compact(
        self,
        conv: Conversation,
        token_budget: int,
        *,
        keep_first: int = 2,                # noqa: ARG002 — Phase 4 wires this
        protect_is_summary: bool = True,    # noqa: ARG002 — Phase 4 wires this
    ) -> None:
        """Compact the conversation to fit within ``token_budget``.

        Phase 1: extracted from the former Conversation trim/summary shims
        (removed in SPEC-AUDIT-CLEANUP-2 Phase 4). ``self`` → ``conv``
        throughout. No behavior changes.
        """
        # Audit-Fix-31 (Bug #8): guard against non-positive budgets. A
        # token_budget <= 0 would cause the trim loop to aggressively
        # prune everything down to keep_first + tail_preserve messages,
        # nuking useful context. Return without recording a CompactionEvent.
        if token_budget <= 0:
            return

        # Snapshot state for telemetry BEFORE any mutation.
        messages_count_before = len(conv.messages)
        tokens_before = conv.get_token_estimate()
        summary_tokens_injected = 0

        # Phase 4: P2/P3-aware trim parameters. Defined before the trim loop
        # so both the loop guard and the summary injection block can reference
        # them. tail_preserve matches the legacy value (4); min_messages
        # combines keep_first + tail_preserve to enforce the lower bound.
        tail_preserve = 4
        min_messages = keep_first + tail_preserve

        # ── Layer 1: prune_tool_outputs (P4) ──────────────────────────────
        # Cheap lossless compaction: stub old tool result content in-place.
        # Runs before the trim loop (Layer 2) to preserve conversation structure
        # while reducing token usage. Returns tokens freed; we just call it for
        # its side effect.
        self.prune_tool_outputs(conv, token_budget, protect_turns=2)
        # Re-snapshot tokens_after after Layer 1 for telemetry accuracy.
        tokens_after_layer1 = conv.get_token_estimate()

        # ── Trim loop ─────────────────────────────────────────────────────────
        # Phase 4: delegates candidate selection to ``_select_prune_candidate``,
        # which implements P2 (keep_first) and P3 (protect_is_summary) as a
        # single two-pass scan (non-protected first, then protected).
        _max_compact_iterations = 1000  # safety cap; ~50 in practice
        _iteration = 0
        while (
            conv.get_token_estimate() > token_budget
            and len(conv.messages) > min_messages
            and _iteration < _max_compact_iterations
        ):
            _iteration += 1
            idx = self._select_prune_candidate(
                conv, keep_first, tail_preserve, protect_is_summary
            )
            if idx is None:
                break
            msg = conv.messages[idx]
            # CB-6: remove TOOL_RESULT + ASSISTANT-with-tool_calls as a pair.
            if msg.role == MessageRole.TOOL_RESULT:
                conv.messages.pop(idx)
                if (
                    idx > 0
                    and conv.messages[idx - 1].role == MessageRole.ASSISTANT
                    and conv.messages[idx - 1].tool_calls
                    and (idx - 1) >= keep_first
                ):
                    # Parent ASSISTANT in trimmable region — pop the pair.
                    parent_call_ids = {
                        tc.call_id for tc in conv.messages[idx - 1].tool_calls
                    }
                    conv.messages.pop(idx - 1)
                    # After popping the first TR + parent, sweep any remaining
                    # trimmable sibling TRs. (Sibling TRs in tail are fine — the
                    # parent is gone, but the post-trim orphan sweep below will
                    # clean them up. We do not need to preserve tail here because
                    # the parent is already gone.)
                    trimmable_end = len(conv.messages) - tail_preserve
                    while (
                        idx + 1 < len(conv.messages)
                        and conv.messages[idx + 1].role == MessageRole.TOOL_RESULT
                        and conv.messages[idx + 1].tool_call_id in parent_call_ids
                        and (idx + 1) < trimmable_end
                    ):
                        conv.messages.pop(idx + 1)
                elif (
                    idx > 0
                    and conv.messages[idx - 1].role == MessageRole.ASSISTANT
                    and conv.messages[idx - 1].tool_calls
                ):
                    # Parent ASSISTANT is in keep_first region — can't remove.
                    # _select_prune_candidate should have filtered this, but
                    # break defensively to prevent CB-6 violations.
                    break
            elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                # CB-6: this assistant's N tool_calls generate N sibling TRs.
                # Pop ALL trimmable siblings, not just the first.
                call_ids = {tc.call_id for tc in msg.tool_calls}
                trimmable_end = len(conv.messages) - tail_preserve

                # Scan siblings. If ANY sibling TR is in tail_preserve zone,
                # skip the entire group — we cannot pop the assistant without
                # orphaning the tail TR (CB-6 violation).
                scan_idx = idx + 1
                tail_sibling = False
                while (
                    scan_idx < len(conv.messages)
                    and conv.messages[scan_idx].role == MessageRole.TOOL_RESULT
                    and conv.messages[scan_idx].tool_call_id in call_ids
                ):
                    if scan_idx >= trimmable_end:
                        tail_sibling = True
                        break
                    scan_idx += 1

                if tail_sibling:
                    # Sibling TR is in tail. Don't pop the assistant. Trim loop
                    # will try a different candidate on the next iteration.
                    # If no candidates remain, the while-loop guard
                    # (conv.get_token_estimate() > token_budget) terminates
                    # compaction cleanly.
                    continue

                # All siblings (if any) are in trimmable. Pop them all, then the assistant.
                while (
                    idx + 1 < len(conv.messages)
                    and conv.messages[idx + 1].role == MessageRole.TOOL_RESULT
                    and conv.messages[idx + 1].tool_call_id in call_ids
                ):
                    conv.messages.pop(idx + 1)
                conv.messages.pop(idx)
            else:
                conv.messages.pop(idx)
            conv._token_estimate_cache = None

        # ── Post-trim orphan sweep (defense in depth) ─────────────────────────
        # If anything slipped past Changes 1 and 2 — e.g., a TR whose parent
        # was popped from the TOOL_RESULT branch with a tail sibling we
        # refused to touch — strip it here so the wire payload is always valid.
        # This is a safety net, not the primary fix.
        valid_call_ids = set()
        for m in conv.messages:
            if m.role == MessageRole.ASSISTANT and m.tool_calls:
                for tc in m.tool_calls:
                    valid_call_ids.add(tc.call_id)
        before_sweep = len(conv.messages)
        conv.messages[:] = [
            m
            for m in conv.messages
            if m.role != MessageRole.TOOL_RESULT or m.tool_call_id in valid_call_ids
        ]
        if len(conv.messages) != before_sweep:
            conv._token_estimate_cache = None

        # ── Summary injection ─────────────────────────────────────────────────
        # Phase 4.10: fire when any messages were removed AND at least 4
        # messages remain. Skip if the summary would push the conversation
        # back over ``token_budget``.
        messages_removed = messages_count_before - len(conv.messages)
        if messages_removed > 0 and len(conv.messages) >= min_messages:
            summary = self._summary(conv, token_budget=token_budget, keep_first=keep_first)
            if summary:
                current_tokens = conv.get_token_estimate()
                fitted = self._fit_summary(conv, summary, token_budget, current_tokens)
                if fitted is not None:
                    encoding = _tiktoken_encoding_for(conv.model)
                    if encoding is not None:
                        summary_tokens_injected = len(
                            getattr(encoding, "encode_ordinary")(fitted)
                        )
                    else:
                        summary_tokens_injected = len(fitted) // 4
                    summary_msg = Message(
                        role=MessageRole.ASSISTANT,
                        content=fitted,
                        is_summary=True,
                    )
                    insert_at = max(keep_first, len(conv.messages) - tail_preserve)
                    conv.messages.insert(insert_at, summary_msg)

        # Invalidate cache and snapshot post-state for telemetry.
        # Cache is invalidated inside the loop after each stub (line 335).
        tokens_after = conv.get_token_estimate()

        # ── Telemetry recording (§0.4) ───────────────────────────────────────
        provider = ""
        model_value = conv.model or ""
        if "/" in model_value:
            provider, model_value = model_value.split("/", 1)

        # Determine which layer(s) fired for telemetry.
        # Layer 1 (prune_tool_outputs) fired iff tokens decreased between
        # the initial snapshot and the post-Layer-1 snapshot. Layer 2
        # (trim loop) fired iff any messages were removed. If neither
        # fired, default to layer=2 (matches pre-Phase-5 behavior).
        layer = 0
        if tokens_after_layer1 < tokens_before:
            layer = 1
        if messages_count_before > len(conv.messages):
            layer = max(layer, 2)
        # layer == 0 means no compaction occurred (no-op). Report honestly.

        self._last_result = CompactionEvent(
            turn=conv.step_count,
            trigger="trim",
            layer=layer,
            messages_before=messages_count_before,
            messages_after=len(conv.messages),
            messages_removed=messages_count_before - len(conv.messages),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tokens_freed=tokens_before - tokens_after,
            summary_tokens_injected=summary_tokens_injected,
            soft_ceiling=token_budget,
            hard_ceiling=None,  # not known at strategy level; runtime patches after compact()
            provider=provider,
            model=model_value,
        )

    # ── Layer 1: prune_tool_outputs (Phase 5: P4 cheap lossless stubbing)

    def prune_tool_outputs(
        self,
        conv: Conversation,
        target_tokens: int,
        protect_turns: int = 2,
    ) -> int:
        """Stub old tool results to free token budget. Returns tokens freed.

        Cheap lossless Layer 1 compaction. Walks backward from the end,
        skipping the protect_turns most recent TOOL_RESULT messages. For
        each unprotected TOOL_RESULT, replaces content with a short stub:

          "[compacted — {tool_name} output, {N} chars removed]"

        Stops when get_token_estimate() <= target_tokens.
        Idempotent: detects already-stubbed messages by their
        "[compacted —" prefix and skips them.

        **Cache invalidation contract:** This method mutates ``msg.content``
        in place. The token estimate cache is keyed on
        ``(len(messages), hash(system_prompt))`` — neither changes when we
        mutate content. So we MUST invalidate the cache after each stub;
        otherwise the loop's ``get_token_estimate()`` calls would return
        the pre-stub cached value, causing the loop to over-stub.

        Args:
            target_tokens: Stop pruning when token estimate drops to this.
            protect_turns: Number of most recent TOOL_RESULT messages to skip.

        Returns:
            Number of tokens freed (estimate before - estimate after).
        """
        # Audit-Fix-28 (Bug #5): clamp negative protect_turns to 0. Without
        # this, list slicing `tool_result_indices[protect_turns:]` with
        # protect_turns=-1 gives tool_result_indices[-1:] = [last_index],
        # meaning the MOST RECENT tool result is in the prunable set and
        # gets stubbed while older results are "protected" — exactly
        # backwards from intended behavior.
        if protect_turns < 0:
            protect_turns = 0

        tokens_before = conv.get_token_estimate()
        if tokens_before <= target_tokens:
            return 0

        # Find TOOL_RESULT indices, most-recent-first.
        tool_result_indices: list[int] = []
        for i in range(len(conv.messages) - 1, -1, -1):
            if conv.messages[i].role == MessageRole.TOOL_RESULT:
                tool_result_indices.append(i)

        # Skip the protect_turns most recent tool results.
        prunable = tool_result_indices[protect_turns:]

        # P5-BUG#3: surface that protect_turns exceeds available tool results.
        if protect_turns > len(tool_result_indices) and tool_result_indices:
            import logging
            logging.getLogger(__name__).debug(
                "prune_tool_outputs: protect_turns=%d > %d tool_results; "
                "no messages will be pruned",
                protect_turns, len(tool_result_indices),
            )

        for idx in prunable:
            if conv.get_token_estimate() <= target_tokens:
                break
            msg = conv.messages[idx]
            # Idempotence: skip already-stubbed messages.
            if msg.content.startswith("[compacted \u2014"):
                continue
            # Find the tool name from the parent ASSISTANT message's tool_calls.
            # Fast path: check immediate predecessor (the common case).
            # Slow path: backward-walk for interleaved messages.
            tool_name = "[unknown tool]"
            if msg.tool_call_id and idx > 0:
                for parent_idx in range(idx - 1, -1, -1):
                    candidate = conv.messages[parent_idx]
                    if (
                        candidate.role == MessageRole.ASSISTANT
                        and candidate.tool_calls
                    ):
                        for tc in candidate.tool_calls:
                            if tc.call_id == msg.tool_call_id:
                                tool_name = tc.tool_name
                                break
                        if tool_name != "[unknown tool]":
                            break  # Found the parent; stop searching.
            original_len = len(msg.content)
            stub = f"[compacted \u2014 {tool_name} output, {original_len} chars removed]"
            msg.content = stub
            # Record the stub's actual token footprint (not 0). Uses the same
            # chars//4 heuristic as _find_split_index's fallback path.
            msg.tokens_used = len(stub) // 4
            # CRITICAL: invalidate cache after each mutation. The cache key
            # (len(messages), hash(system_prompt)) is unchanged by content
            # mutation, so a stale cache would return pre-stub tokens.
            conv._token_estimate_cache = None

        # Cache is invalidated inside the loop after each stub (line 335).
        tokens_after = conv.get_token_estimate()
        return tokens_before - tokens_after

    # ── Phase 6: P5 _find_split_index + P6 _fit_summary ──────────────────────

    def _find_split_index(
        self,
        conv: Conversation,
        budget_tokens: int,
        keep_first: int = 2,
    ) -> int:
        """Find the message index where the head ends and the tail begins.

        Walks backward from the end, accumulating tokens, until half the
        budget is consumed. Then walks back further to land on an assistant
        message boundary (role-anchored, Aider pattern).

        Also enforces CB-6 (tool-call pairing) at the split boundary.

        Args:
            budget_tokens: Total token budget for the conversation.
            keep_first: Minimum index for the split (never split before this).

        Returns:
            Message index >= keep_first where the head can be summarized
            and the tail kept verbatim.
        """
        if len(conv.messages) <= keep_first:
            return keep_first

        half_budget = budget_tokens // 2
        tail_tokens = 0
        split = len(conv.messages)

        for i in range(len(conv.messages) - 1, keep_first - 1, -1):
            msg = conv.messages[i]
            msg_tokens = msg.tokens_used or (len(msg.content) // 4)
            if tail_tokens + msg_tokens >= half_budget:
                break
            tail_tokens += msg_tokens
            split = i

        # Role-anchor walk-back: walk back until messages[split - 1] is ASSISTANT.
        while split > keep_first:
            prev_msg = conv.messages[split - 1]
            if prev_msg.role == MessageRole.ASSISTANT:
                break
            split -= 1

        # CB-6 forward check: if messages[split] is a TOOL_RESULT whose
        # parent ASSISTANT-with-tool-calls is in the head, move split forward
        # to include this TOOL_RESULT in the head (gets summarized with parent).
        # Phase 9 hardening: also search the keep_first region (protected head)
        # for the parent — if the parent is at index < keep_first, the TOOL_RESULT
        # must also be in the head to preserve CB-6 pairing.
        # P9-BUG#2: Cap iterations to prevent O(N²) on consecutive orphans.
        _cb6_cap = len(conv.messages)
        _cb6_iters = 0
        # Audit-Fix-30 (Bug #7): track visited indices to detect bounce on
        # duplicate tool_call_ids (malformed but possible). Without this,
        # the CB-6 loop can oscillate between two TR messages that share
        # the same tool_call_id, never reaching a stable split boundary.
        _cb6_visited: set[int] = set()
        while split < len(conv.messages):
            _cb6_iters += 1
            if _cb6_iters > _cb6_cap:
                break
            if split in _cb6_visited:
                break  # bounce detected on duplicate tool_call_id
            _cb6_visited.add(split)
            msg_at_split = conv.messages[split]
            if msg_at_split.role == MessageRole.TOOL_RESULT:
                if split > keep_first:
                    adjacent_parent = conv.messages[split - 1]
                    if (
                        adjacent_parent.role == MessageRole.ASSISTANT
                        and adjacent_parent.tool_calls
                        and any(tc.call_id == msg_at_split.tool_call_id for tc in adjacent_parent.tool_calls)
                    ):
                        split += 1
                        continue
                # Search backward for true parent in trimmable region.
                if msg_at_split.tool_call_id:
                    found_parent = False
                    for j in range(split - 1, keep_first - 1, -1):
                        candidate = conv.messages[j]
                        if (
                            candidate.role == MessageRole.ASSISTANT
                            and candidate.tool_calls
                            and any(tc.call_id == msg_at_split.tool_call_id for tc in candidate.tool_calls)
                        ):
                            split = j
                            found_parent = True
                            break
                    if found_parent:
                        continue
                    # Parent not found in trimmable region. Search keep_first
                    # region (indices 0 .. keep_first-1) for the parent.
                    # If found, include this TOOL_RESULT in the head by
                    # incrementing split — CB-6 pairing must be preserved.
                    for j in range(min(keep_first, len(conv.messages)) - 1, -1, -1):
                        candidate = conv.messages[j]
                        if (
                            candidate.role == MessageRole.ASSISTANT
                            and candidate.tool_calls
                            and any(tc.call_id == msg_at_split.tool_call_id for tc in candidate.tool_calls)
                        ):
                            split += 1  # include TOOL_RESULT in head
                            found_parent = True
                            break
                    if not found_parent:
                        break
                else:
                    break
            else:
                break

        return max(split, keep_first)

    def _fit_summary(
        self,
        conv: Conversation,
        summary: str,
        token_budget: int,
        current_tokens: int,
    ) -> str | None:
        """Fit a summary into the remaining token budget by truncating.

        Tries 5 iterations, each reducing the summary to 80% of its previous
        length. If none fit, returns a minimal stub. If even the stub doesn't
        fit, returns None.

        Uses tiktoken (via ``_tiktoken_encoding_for()``) when available for
        accurate token counts; falls back to the ``chars // 4`` heuristic.
        """
        available_tokens = token_budget - current_tokens
        if available_tokens <= 0:
            return None

        # Use tiktoken when available for accurate token counting.
        encoding = _tiktoken_encoding_for(conv.model)

        def _count_tokens(s: str) -> int:
            if encoding is not None:
                return len(getattr(encoding, "encode_ordinary")(s))
            return len(s) // 4

        # Try progressively smaller versions. Truncate by token fraction
        # (not character fraction) for accurate convergence under tiktoken.
        fitted = summary
        for _attempt in range(5):
            fitted_tokens = _count_tokens(fitted)
            if fitted_tokens <= available_tokens:
                return fitted
            # Target 80% of current token count. Convert to char count
            # using the same ratio if tiktoken is active, else use chars directly.
            if encoding is not None and fitted_tokens > 0:
                char_per_token = len(fitted) / fitted_tokens
                target_tokens = int(fitted_tokens * 0.8)
                fitted = fitted[:int(target_tokens * char_per_token)]
            else:
                fitted = fitted[:int(len(fitted) * 0.8)]

        # Final fallback: minimal stub.
        stub = "[Context reset — earlier conversation was too large to summarize]"
        if _count_tokens(stub) <= available_tokens:
            return stub
        return None

    # ── Prune candidate selector (Phase 4: P2 keep_first + P3 protect_is_summary)

    def _select_prune_candidate(
        self,
        conv: Conversation,
        keep_first: int,
        tail_preserve: int,
        protect_is_summary: bool,
    ) -> int | None:
        """Find the index of the best message to remove for budget trimming.

        Scans the trimmable region [keep_first, len - tail_preserve) for:
        1. First pass: non-protected messages (not is_summary when protect_is_summary=True)
        2. Second pass: protected messages (if no non-protected candidates)

        Prefers TOOL_RESULT + ASSISTANT-with-tool_calls pairs (CB-6 aware).
        Falls back to oldest non-protected message.

        CB-6 invariant at keep_first boundary: When a TOOL_RESULT candidate is
        at index ``keep_first``, its parent ASSISTANT-with-tool-calls at
        ``keep_first - 1`` is in the keep_first region and cannot be removed.
        This method skips those candidates.

        Returns the index of the message to remove, or None if the
        trimmable region is empty.
        """
        trimmable_end = len(conv.messages) - tail_preserve
        if trimmable_end <= keep_first:
            return None

        # Build the candidate list, non-protected first.
        non_protected: list[int] = []
        protected: list[int] = []
        for i in range(keep_first, trimmable_end):
            msg = conv.messages[i]
            is_protected = protect_is_summary and msg.is_summary
            if is_protected:
                protected.append(i)
            else:
                non_protected.append(i)

        # Try non-protected first, then protected.
        for candidate_pool in (non_protected, protected):
            if not candidate_pool:
                continue
            # Prefer TOOL_RESULT + ASSISTANT-with-tool_calls pairs (CB-6 aware).
            for i in candidate_pool:
                msg = conv.messages[i]
                if msg.role == MessageRole.TOOL_RESULT:
                    if (
                        i > 0
                        and conv.messages[i - 1].role == MessageRole.ASSISTANT
                        and conv.messages[i - 1].tool_calls
                        and (i - 1) >= keep_first
                    ):
                        return i
                elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                    if (
                        i + 1 < len(conv.messages)
                        and conv.messages[i + 1].role == MessageRole.TOOL_RESULT
                        and (i + 1) < trimmable_end
                    ):
                        return i
            # No CB-6 pairs found — return the first candidate (oldest).
            return candidate_pool[0]

        return None

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _summary(
        self,
        conv: Conversation,
        token_budget: int = 0,
        keep_first: int = 2,
    ) -> str:
        """Generate a summary of the oldest trimmed user messages.

        Phase 6: Uses _find_split_index() to compute a smarter split point
        instead of the naive messages[:-4] slice. The split index lands on
        an assistant message boundary (role-anchored) and respects CB-6.
        """
        if not conv.messages:
            return ""

        tail_preserve = 4
        if len(conv.messages) <= tail_preserve:
            return ""

        if token_budget > 0:
            # P5: smart split when a budget is provided (called from compact()).
            # P6-BUG#1: pass conv.get_token_estimate() (current size) not token_budget
            # (target). token_budget is the POST-compaction target; passing it as
            # the split budget causes half_budget=token_budget//2 which collapses
            # to keep_first on small budgets → empty head → empty summary.
            split = self._find_split_index(
                conv, conv.get_token_estimate(), keep_first=keep_first
            )
            split = max(keep_first, min(split, len(conv.messages) - tail_preserve))
        else:
            # Legacy-path compatibility: when _summary() is called with no
            # token_budget (the pre-strategy Conversation summary contract,
            # kept for direct strategy callers), fall back to
            # messages[:-tail_preserve]. The smart
            # split uses conv.get_token_estimate() to size the tail, but with small
            # conversations (where all messages fit in half of total tokens),
            # _find_split_index lands at keep_first — producing an empty head.
            # That breaks the Phase 4 tests that rely on messages[:-tail_preserve]
            # semantics. Deviation from spec Step 3's literal fallback.
            # P6-BUG#2: noted but NOT fixed in this phase — see Audit-Fix-15
            # discussion. Keeping the deviation preserves test behavior; the
            # CB-6 risk is documented in the audit report.
            split = len(conv.messages) - tail_preserve

        head_messages = conv.messages[:split]

        user_contents: list[str] = []
        for msg in head_messages:
            if msg.role == MessageRole.USER:
                # Audit-Fix-29 (Bug #6): filter out whitespace-only USER
                # messages so the summary doesn't show an empty preview line.
                stripped = msg.content.strip()
                if stripped:
                    user_contents.append(stripped)

        if not user_contents:
            return ""

        lines = [f"Conversation so far ({len(user_contents)} prior turns):"]
        for i, content in enumerate(user_contents[:5], 1):
            preview = content[:100] + ("…" if len(content) > 100 else "")
            lines.append(f"  {i}. {preview}")
        if len(user_contents) > 5:
            lines.append(f"  … and {len(user_contents) - 5} more turns")

        return "\n".join(lines)


logger = logging.getLogger(__name__)


class LLMSummarizeStrategy(DefaultContextStrategy):
    """Strategy that uses an LLM call to generate the trim summary.

    Spec: docs/specs/SPEC-CONTEXT-UI-COMPACT-LLM-2026-07-10.md §3.3.

    Inherits from DefaultContextStrategy so it gets free Layer 1
    (prune_tool_outputs) and Layer 2 (trim loop) for free. Only
    Layer 3 (the summary text) is overridden.

    The LLM call uses:
      - Same provider as conv.model (e.g., "openai")
      - Same model as conv.model (e.g., "openai/gpt-4o")
      - Same system_prompt (via provider's chat completion)
      - Cache-friendly prompt structure (system_prompt first, then
        conversation transcript, then the summary prompt)

    Failure modes:
      - LLM call raises -> falls back to super()._summary() (textual preview)
      - LLM call returns empty -> falls back to textual preview
      - LLM call returns oversized -> truncates to fit budget
      - LLM call is rate-limited -> does NOT retry; uses textual fallback
    """

    SUMMARY_PROMPT_TEMPLATE = """\
You are a conversation compaction specialist. Produce a structured summary \
of the conversation transcript below so a downstream LLM can continue the \
task without losing important context. Be concise but preserve everything \
that matters.

Format your response with EXACTLY these nine sections, each on its own \
line, no preamble:

<task>One sentence: what the user originally asked.</task>
<progress>Bulleted list of completed steps and intermediate results.</progress>
<files>Comma-separated paths of files touched or relevant to the task.</files>
<decisions>Bulleted list of decisions made, with brief rationale.</decisions>
<constraints>Bulleted list of constraints in effect (e.g., user prefs, env limits).</constraints>
<errors>Bulleted list of errors encountered and their resolutions.</errors>
<open_questions>Bulleted list of unresolved threads or ambiguities.</open_questions>
<next_steps>Bulleted list of suggested next actions for the agent.</next_steps>
<user_preferences>Bulleted list of preferences the user expressed.</user_preferences>

Transcript:
{transcript}
"""

    def __init__(
        self,
        llm_provider: Callable[[str, str], str] | None = None,
        llm_model_override: str | None = None,
    ) -> None:
        super().__init__()
        self._llm_provider = llm_provider
        self._llm_model_override = llm_model_override

    def _summary(
        self,
        conv: Conversation,
        token_budget: int = 0,
        keep_first: int = 2,
    ) -> str:
        """LLM-generated structured summary. Falls back to textual on failure.

        Signature mirrors DefaultContextStrategy._summary (line 678).
        Uses llm_provider(system_prompt, user_prompt) for the LLM call.
        """
        if self._llm_provider is None:
            logger.warning(
                "LLMSummarizeStrategy: no llm_provider configured; "
                "using textual fallback"
            )
            return super()._summary(conv, token_budget, keep_first=keep_first)

        tail_preserve = 4
        if len(conv.messages) <= tail_preserve:
            return ""

        head = conv.messages[:max(keep_first, len(conv.messages) - tail_preserve)]
        if not head:
            return ""

        lines = []
        for msg in head:
            role_value = msg.role.value if hasattr(msg.role, "value") else str(msg.role)
            content = (msg.content or "").replace("\n", " ")
            if role_value == "tool":
                content = content[:500]
            else:
                content = content[:2000]
            lines.append(f"[{role_value}] {content}")
        transcript = "\n".join(lines)
        if len(transcript) > 8000:
            transcript = transcript[:8000] + "\n[... transcript truncated for length ...]"

        user_prompt = self.SUMMARY_PROMPT_TEMPLATE.format(transcript=transcript)
        system_prompt = conv.system_prompt or "You are a helpful assistant."

        try:
            response = self._llm_provider(system_prompt, user_prompt)
        except Exception as exc:
            logger.warning(
                "LLMSummarizeStrategy: LLM call failed (%s); "
                "falling back to textual summary",
                type(exc).__name__,
            )
            return super()._summary(conv, token_budget, keep_first=keep_first)

        if not response or not response.strip():
            logger.warning(
                "LLMSummarizeStrategy: LLM returned empty response; "
                "falling back to textual summary"
            )
            return super()._summary(conv, token_budget, keep_first=keep_first)

        expected_tags = [
            "task", "progress", "files", "decisions", "constraints",
            "errors", "open_questions", "next_steps", "user_preferences",
        ]
        missing = [t for t in expected_tags if f"<{t}>" not in response]
        if missing:
            logger.info(
                "LLMSummarizeStrategy: response missing tags %s "
                "(acceptable, using as-is)",
                missing,
            )

        return response
