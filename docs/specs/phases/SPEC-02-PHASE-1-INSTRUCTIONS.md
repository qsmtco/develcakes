# SPEC-02 Phase 1 Instructions — Error Surfacing Feed Card + Empty-Message Rollback

**Spec:** docs/specs/SPEC-02-ERROR-SURFACING.md (read it IN FULL first)
**Architecture:** .crabcakes/architecture.md §Error Handling ("turn-fatal provider errors
emit feed cards with code+message+provider; no empty assistant messages persisted on
failed turns")
**Phase 1 of 1** — this spec is one phase. Both changes + tests land together.

## Line-number drift vs spec (verified 2026-09-21)

| Spec says | Actual today |
|---|---|
| `_do_error` at :2287 | **:2399** in ui/handlers/agent_runtime_handler.py |
| `_run_loop` except ~:1900 | **:1932** in agent/runtime.py |
| streamed placeholder add :1604 | :1604 (adds NON-empty placeholder text — fine) |
| text-only add :1720 | :1720 |

## Scope — exactly these 3 files

1. `ui/handlers/agent_runtime_handler.py` — feed card in `_do_error`
2. `agent/runtime.py` — empty-message rollback in `_run_loop` except path
3. `tests/test_error_surfacing.py` — NEW file

Nothing else. No scope creep.

## Task 1 — `_do_error` feed card (handler :2399)

`_do_error` already renders the error into chat. ADD feed-card emission. Insert AFTER
the `if self._crh is not None:` chat-render block (i.e., after `self._mc.scroll_chat_to_bottom()`
at the same indent as that `if`), BEFORE the `_on_agent_end_cb` lifecycle fire.

Placement constraints (verified):
- AFTER the stale-token guard and duplicate-completion guard at the top of `_do_error`
  (:2401–2412) — a stale/duplicate error must NOT emit a card.
- `resolved_name` is already computed at :2431 (`agent_def.display_name if agent_def else None`).
- `display_msg` (user-friendly string) is already computed at :2428.
- The stored exception is at `self._last_error_exception.get(session_key)` (:2448 pattern);
  provider/model context via `getattr(exc_obj, "_crabcakes_context", None)` — the runtime
  attaches `{"provider", "model", "exception_type"}` (agent/runtime.py:2220, :2248).

Code:

```python
# SPEC-02: turn-fatal errors surface in the Project Feed too, not just
# chat + stderr. Same guard pattern as publish_cli_nudge_card (:538).
if self._fh is not None:
    provider_meta = None
    try:
        exc_obj = self._last_error_exception.get(session_key)
        if exc_obj is not None:
            provider_meta = getattr(exc_obj, "_crabcakes_context", None)
    except Exception:
        provider_meta = None
    from models.feed_card import FeedCardData
    card = FeedCardData(
        card_type="system",
        source="agent",
        title=f"Turn failed: {resolved_name or self.get_agent_name_for_session(session_key) or 'Agent'}",
        body=display_msg[:2000],
        author="Runtime",
        timestamp=datetime.now(timezone.utc),
        project_name=self._active_project[0] if self._active_project else "(none)",
        metadata={
            "session_key": session_key,
            "kind": "turn_error",
            "provider": (provider_meta or {}).get("provider"),
            "model": (provider_meta or {}).get("model"),
            "exception_type": (provider_meta or {}).get("exception_type"),
        },
    )
    self._fh.add_card(card)
```

Verified facts you can rely on:
- `datetime`/`timezone` already imported at module level (publish_cli_nudge_card uses
  `datetime.now(timezone.utc)` bare at :565).
- `FeedCardData` fields include `card_type, source, title, body, author, timestamp,
  project_name, metadata` (models/feed_card.py:80). `"system"` / `"agent"` are valid
  enum values (used by publish_cli_nudge_card).
- `self._fh.add_card(card)` is the FeedHandler API (:750 in ui/handlers/feed_handler.py).
- Minor spec deviation, sanctioned: guard on `self._fh is not None` ONLY (spec's extra
  `self._active_project is not None` guard is redundant — project_name has a "(none)"
  fallback, same as the nudge card). Document in your report.

## Task 2 — `_run_loop` rollback (runtime :1932)

The generic `except Exception as e:` block at :1932 routes to
`self._terminate_turn(TurnResult(status=FAILED...))` at :1944. Add the rollback BEFORE
that call (FAILED persists via `_auto_save` inside `_terminate_turn` — rollback must run
first, same thread, sequential):

```python
except Exception as e:
    logger.exception("Error in tool loop for %s", session_key)
    # SPEC-02: rollback trailing EMPTY assistant messages added by this
    # failed turn so they are never persisted. Only truly-empty (no content,
    # no tool_calls) messages are removed — partial content is real progress
    # and stays. Guarded: a rollback failure must never mask the original error.
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
    self._terminate_turn(TurnResult(   # existing call, unchanged
        ...
    ))
```

Verified facts you can rely on:
- Use `self._conversations.get(session_key)` — NOT the local `conv` binding (the
  exception may predate conv resolution; the existing comment block at :1937 says so).
- `Message` (models/conversation.py:119): `role: MessageRole` (str Enum, ASSISTANT="assistant"),
  `content: str`, `tool_calls: list[ToolCall]`. The defensive `getattr(m.role, "value", m.role)`
  handles both enum and plain-string roles.
- Empty-message add sites in the loop: :1604 (adds NON-empty placeholder), :1720, :1778,
  :1908, :1923. Only truly-empty trailing ones match the predicate.
- Cap of 5 guards pathological loops; edge case table in spec §7 is binding.

## Task 3 — tests/test_error_surfacing.py (NEW)

READ FIRST: tests/test_agent_runtime.py stream-error suite (:4729–5028) for the
mock-provider + _run_loop exception patterns, and tests/test_config_invalidation.py
(SPEC-01) for the handler-fixture pattern (fake `_fh`, stub objects).

Required tests (minimum 10):

Feed card:
1. `test_do_error_publishes_feed_card` — handler with recording fake `_fh`; call
   `_do_error(sk, "boom")`; assert one card, card_type "system", metadata
   kind=turn_error, session_key correct.
2. `test_do_error_card_carries_provider_context` — put an exception with
   `_crabcakes_context = {"provider": "openrouter", "model": "m1", "exception_type":
   "HTTPError"}` in `_last_error_exception`, call `_do_error(sk, exc_obj)`; assert
   metadata provider/model/exception_type land on the card, body is the friendly message.
3. `test_do_error_no_feed_handler_no_crash` — `_fh=None`; `_do_error` completes, no card.
4. `test_do_error_stale_token_no_card` — `_turn_tokens[sk]` set to a different token;
   call `_do_error(sk, msg, error_token=stale)`; assert NO card (early return).
5. `test_do_error_duplicate_completion_no_card` — pre-add sk to `_session_completed`;
   assert NO card.

Rollback:
6. `test_rollback_removes_trailing_empty_assistant` — conversation with [user, assistant(""),
   ]; force a mid-loop exception (model the mock-provider pattern from test_agent_runtime);
   assert after FAILED termination: trailing empty assistant gone, prior messages intact,
   turn status FAILED.
7. `test_rollback_keeps_partial_content` — trailing assistant("partial text") stays.
8. `test_rollback_keeps_tool_call_messages` — trailing assistant("", [ToolCall...]) stays.
9. `test_rollback_caps_at_five` — 6 trailing empties → exactly 5 removed, 1 remains.
10. `test_rollback_no_conversation_noop` — session not in `_conversations`: exception path
    completes, no crash.

Also (integration, recommended): `test_failed_turn_persists_no_empty_assistant` — with a
temp persistence dir, run a failing turn end-to-end through `_terminate_turn`'s
`_auto_save`; reload the saved JSON; assert no trailing empty assistant message. If the
persistence fixture is too heavy, document why and rely on test 6 + `_terminate_turn`'s
existing persistence tests.

## Verification (run + paste outputs)

```
/tmp/spec01venv2/bin/python -m pytest tests/test_error_surfacing.py -v
/tmp/spec01venv2/bin/python -m pytest tests/test_agent_runtime.py -q   # no stream-error regression
/tmp/spec01venv2/bin/python -m pytest tests/test_config_invalidation.py -q   # SPEC-01 still green
ruff check ui/handlers/agent_runtime_handler.py agent/runtime.py tests/test_error_surfacing.py
ruff format --check ui/handlers/agent_runtime_handler.py agent/runtime.py tests/test_error_surfacing.py
pyright ui/handlers/agent_runtime_handler.py agent/runtime.py 2>&1 | tail -5
```

**Baselines (pre-existing, ZERO new allowed):** runtime.py = 26 ruff + 18 pyright.
Known env: /tmp/spec01venv2/bin/python is the only pytest env on this box. Full-suite
GTK segfault (test_activity_bubbles) is pre-existing at clean HEAD — do NOT chase it.

## COMPLETENESS report (required in your reply)

- [ ] File 1 changed (insertion line range in `_do_error`)
- [ ] File 2 changed (rollback line range in except path)
- [ ] File 3 created (test count)
- [ ] All 6 verification outputs pasted
- [ ] Spec drift noted (any line drift >10 lines beyond the table above)
- [ ] Sanctioned deviation noted (fh-only guard) — any OTHER deviation flagged, not silent

Report "SPEC-02 PHASE-1 COMPLETE" with the checklist and outputs, or report blockers
with exact reproduction. Flag related issues — do NOT fix them silently.
