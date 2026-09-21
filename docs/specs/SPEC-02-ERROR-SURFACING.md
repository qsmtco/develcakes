# SPEC-02: Provider Error Surfacing + Empty-Message Rollback

**Date:** 2026-09-20
**Author:** Supervisor (develcakes v2)
**Status:** Draft — for implementation
**Implements:** .crabcakes/architecture.md (agent/ bug-fix delta #2)
**Depends on:** none
**Target branch:** main

> Architecture compliance: error-handling contract — "turn-fatal provider errors emit feed
> cards with code+message+provider; no empty assistant messages persisted on failed turns."

---

## 1. Overview

**Problem.** Two coupled defects proven on 2026-09-20:
1. Turn-fatal provider errors (HTTP 401/429, mid-stream `_stream_error`) surface only in
   stderr/journal; the chat shows a bare `[Error] ...` string and the Project Feed gets
   **no card** — making transient failures indistinguishable from config errors.
2. When a turn dies mid-stream **after** `conv.add_assistant_message(...)` ran
   (agent/runtime.py:1604 for streamed placeholder, :1720 text-only path), an **empty
   assistant message is persisted** into the conversation JSON
   (`save_conversation_to_disk` in agent/persistence.py:34) and is placeholder-masked on
   every later call (`models/conversation.py to_api_messages` warning observed 8+ times).

**Solution.**
1. `AgentRuntimeHandler._do_error` (ui/handlers/agent_runtime_handler.py:2287) already
   receives every turn-fatal error for special agents — extend it to also publish a feed
   card (card_type `"system"`, with provider code/message metadata).
2. Rollback: in `_run_loop`'s exception termination path, before `_terminate_turn`,
   remove trailing empty assistant messages (content empty AND no tool_calls) added by
   the failed turn.

**Scope**

| In | Out |
|---|---|
| `_do_error` → feed card | New card types (reuse `system`) |
| `_run_loop` empty-message rollback | Changing `to_api_messages` placeholder logic |
| Tests | Touching `save_conversation_to_disk` format |

## 2. Changes by File

### ui/handlers/agent_runtime_handler.py — `_do_error` (line 2287)

Current signature: `def _do_error(self, session_key: str, message: str, error_token: object = None) -> None:`
It renders the error into chat. **Add** feed-card emission after the chat render (only
when `self._fh` is set — same guard pattern as `publish_cli_nudge_card` at ~line 3490):

```python
if self._fh is not None and self._active_project is not None:
    from datetime import datetime, timezone
    from models.feed_card import FeedCardData
    card = FeedCardData(
        card_type="system",
        source="agent",
        title=f"Turn failed: {self._agent_display_name(session_key)}",
        body=message[:2000],
        author="Runtime",
        timestamp=datetime.now(timezone.utc),
        project_name=self._active_project[0] if self._active_project else "(none)",
        metadata={"session_key": session_key, "kind": "turn_error"},
    )
    self._fh.add_card(card)
```

Verified: `FeedCardData` fields (models/feed_card.py:80-105) include `card_type`
(CardType), `source` (CardSource), title/body/author/timestamp/project_name, optional
metadata dict. `"system"` and `"agent"` are valid enum values (CardType.SYSTEM,
CardSource.AGENT — used by publish_cli_nudge_card's `agent_action` card).
`self._fh` is the FeedHandler (used identically in `approve_exec` at ~line 395).
Helper `_agent_display_name` does not exist — use
`self.get_agent_name_for_session(session_key)` (verified, returns display name or "").

### agent/runtime.py — `_run_loop` exception path (~line 1900)

The generic `except Exception as e:` block routes to `_terminate_turn(TurnResult(status=FAILED...))`.
**Before** that call, add rollback:

```python
except Exception as e:
    logger.exception("Error in tool loop for %s", session_key)
    # Rollback: strip trailing empty assistant messages added by this failed
    # turn so they are never persisted (audit: partial-state-persist).
    try:
        conv = self._conversations.get(session_key)
        if conv is not None and conv.messages:
            removed = 0
            while conv.messages and removed < 5:
                m = conv.messages[-1]
                is_empty_assistant = (
                    getattr(m, "role", None) is not None
                    and str(getattr(m.role, "value", m.role)) == "assistant"
                    and not (m.content or "").strip()
                    and not (m.tool_calls or [])
                )
                if not is_empty_assistant:
                    break
                conv.messages.pop()
                removed += 1
            if removed:
                logger.info("[turn-fail] rolled back %d empty assistant message(s) for %s",
                            removed, session_key)
    except Exception:
        logger.exception("empty-message rollback failed for %s", session_key)
    self._terminate_turn(TurnResult(...))  # existing call, unchanged
```

Verified: `Message` dataclass (models/conversation.py) has `role` (MessageRole enum with
`.value`), `content: str`, `tool_calls: list[ToolCall]`. The runtime's local `conv`
binding in the except block may be undefined if the exception predated conversation
resolution — the `self._conversations.get(session_key)` lookup (not the local) handles
that. Cap of 5 guards against pathological loops. `_terminate_turn` FAILED persists via
`_auto_save` — rollback must run before it (same thread, sequential).

**Files NOT changed:**
- `models/conversation.py` — `to_api_messages` placeholder stays as defense-in-depth for
  pre-existing corrupted files
- `agent/persistence.py` — format unchanged
- `agent/llm/*` — error extraction unchanged (`_stream_error` already attached)

## 3. Data Flow

Provider HTTP error → `_call_llm_streaming` raises / `_stream_error` captured →
`_run_loop` except → **rollback empty messages** → `_terminate_turn(FAILED)` →
`on_error` dispatch → `_do_error` → chat render (existing) + **feed card** (new) →
`_auto_save` persists rolled-back history.

## 4. File Change Summary

| File | Change | ~Lines | Risk |
|---|---|---|---|
| ui/handlers/agent_runtime_handler.py | feed card in `_do_error` | +18 | low |
| agent/runtime.py | rollback in except path | +25 | med (hot path, guarded) |
| tests/test_error_surfacing.py | new | ~150 | — |

## 5. Implementation Order

1. `_do_error` card + test (assert card added with metadata kind=turn_error).
2. Rollback + test (simulate mid-turn exception after add_assistant_message; assert
   persisted JSON has no trailing empty assistant messages).
3. Full suite + ruff + pyright.

## 6. Acceptance Criteria

- [ ] A 401/429/mid-stream error produces a Project Feed card with provider message
- [ ] Failed turns persist **no** empty assistant messages (test-proven)
- [ ] Pre-existing corrupted conversations still load (placeholder path intact)
- [ ] No regression in the stream-error test suite
      (tests/test_agent_runtime.py:4729–5028)
- [ ] Full pytest green, ruff clean, pyright clean

## 7. Edge Cases

| Case | Behavior |
|---|---|
| Error before conversation creation | `_do_error` card still fires (session_key present); rollback no-ops |
| Multiple consecutive empty messages | Rollback removes up to 5 trailing; logs count |
| Error with partial real content + tool calls | Message kept (non-empty content or tool_calls) — only truly-empty removed |
| `_do_error` called for non-special session | `get_agent_name_for_session` returns "" — card still emits with generic title |
| Feed handler absent (headless tests) | `self._fh is None` guard skips card |

## 8. ARCHITECTURE.md Updates

None (implements recorded delta).
