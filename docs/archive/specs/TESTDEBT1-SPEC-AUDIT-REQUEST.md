# SPEC-TEST-DEBT-1 — Adversarial Spec Audit Request

**Target:** `docs/specs/SPEC-TEST-DEBT-1.md` (committed `a88ba75`, 551 lines, tree base `64c73f4`)
**Prompt to use:** `.crabcakes/prompts/adversarialDebugger.md`
**Requested by:** Supervisor

## Context

- This spec is the "complete the fix" path for the BUG-21 pair (2 of the 40-failure baseline, in `TestLocalAgentDrawerEmissions`).
- The PM chose option (b) — complete the fix — over option (a) fix-the-tests-only.
- Design: replaces the overloaded empty-delta turn-start signal with a dedicated `on_turn_start` callback dispatched at the top of `_run_loop`; adds BUG #22 render-guards to three `end_streaming` callers; deletes the dead `_started_turn_sessions` set (new finding this session: never written anywhere — init + 3 discards only, grep-verified); rewrites/replaces 4 tests, adds 6.
- Authoring discipline: steel-framed rules applied; all anchors re-verified against the live tree; all 15 code blocks `ast.parse`-verified across fragment contexts; both target failures reproduced red this session (2 failed, 1 passed on the trio).

## Priority probe areas (highest risk first)

1. **Cancel-race guard** (spec §2.3 Edit B, `_do_turn_start`'s ended-sessions guard). `cancel()` dispatches `_do_error` with the SAME turn token as the in-flight turn. If `_do_error` lands first, the ended-guard drops turn-start — intended. Trace the OTHER order: turn-start runs (bubble started), then cancel's `_do_error`. Does the BUG #22 guard in `_do_error` (Edit H) handle "bubble exists, empty text"? Critically: read `chat_render_handler.py:end_streaming` (:623+) and verify its `render=False` path still REMOVES the streaming widget from the container — if cleanup-only leaves the widget, the started bubble leaks.

2. **Degradation fallback** (Edit E): the start-bubble block in `_do_text_delta_inner` is KEPT as the fallback for `on_turn_start=None` legacy callers, while its `_started_turn_sessions.discard` line is deleted. With `on_turn_start` wired in production, does any real path still hit that fallback? If yes, is the block still correct without the discard?

3. **T3 test soundness** (`test_send_to_special_agent_clears_ended_sessions`, Edit E): mocks `_get_runtime` to return a MagicMock rt. Trace `send_to_special_agent`'s full body with that mock — first `get_conversation` (None) → `load_conversation` (None) → `create_conversation` absorbed → second `get_conversation` (None) → `step_count` reset skipped → clear (:878) → `send_message`. Any missed branch? (`_active_project` is `("test", "/tmp/test")` per the class fixture; agent registered; `_resolve_agent_model` → None.)

4. **Red-first claim for the 3 render-guard tests**: they should fail on current code because `render` kwarg is absent (`kwargs.get("render")` is None ≠ False). Verify against the current `_do_error`/`_do_compaction_bubble`/`_do_usage_warning` call sites. ORDERING CORRECTION (supersedes my earlier generalization): only `_do_compaction_bubble` currently calls `end_streaming` BEFORE `_resolve_chat_box`. `_do_usage_warning` has the OPPOSITE ordering — `resolve_chat_box` FIRST (with early-return if None), THEN `end_streaming` inside the `if self._crh is not None:` block. Edits I/J replace each `end_streaming` call IN PLACE — the implementer must NOT move the call across the `_resolve_chat_box` boundary in either function.

5. **Scope sweep**: grep for anything the spec missed — other `_on_text_delta` consumers, other 4-delta expectations (`len(deltas) == 4`), other `end_streaming` callers, `OnTextDelta` docstring empty-string claims, protocol-count claims ("9" in `agent/callbacks.py` module docstring AND ARCHITECTURE.md §3.21m.3 — Responsibility line, Owns line, Public API comment; spec §2.5 claims to catch all).

6. **`test_empty_delta_still_reaches_main_thread`** (Edit G, docstring-only change): with the runtime no longer sending empty deltas as turn-start, does any production path still send an empty text delta (provider empty-content deltas)? Is docstring-only the right change, or does the test's premise die?

7. **ARCHITECTURE.md :1797 ctor signature**: read the actual line; confirm the documented signature is current and `on_turn_start` belongs in it (drift check).

## Standard checks (per adversarialDebugger.md)

Challenge every code sample / line number / mechanism claim; trace failures backwards from "spec implemented — what still breaks?"; hidden assumptions the implementer will trust; type-system probes (`OnTurnStart` Protocol signature vs `_dispatch` kwarg mechanics — note `test_text_delta_fires_incrementally` calls `_run_loop` directly with `turn_token=None`, so the patched `lambda sk:` arity is intentional; production always passes the token); docstring/comment truth (several are REPLACED — verify replacements are true); verify tests match the change (mentally run all 10 rewrites/additions).

## Output

Structured audit report per adversarialDebugger.md format (BUG #N, severity, assumption violated, attack vector, reproduction, root cause, fix). If clean: "ACCEPT, 0 defects, N process findings." I'll iterate until the spec is clean.
