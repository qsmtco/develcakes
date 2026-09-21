# SPEC-AUDIT-CLEANUP-3 Phase 1 (single phase) — Delta Coalescing + Render Throttle + Single Status Ticker

**Spec:** `docs/specs/SPEC-AUDIT-CLEANUP-3-PERF-QUICK-WINS.md` — READ IN FULL. All three sections are authoritative; anchors re-pinned 2026-09-07 post-AC2 (drift noted below).
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load fresh, Discovery block first, every rule.
**Supervisor:** special:supervisor | **Builder:** special:coder | **Auditor:** special:debugger
**Baseline:** tree clean at `361da0a` (pushed). Standing rules in force (no resets, xvfb-run -a, pyflakes gate 0, no scope creep).

**Why one phase:** the three changes share correctness invariants (§"Correctness invariants" in the spec). Splitting them across phases would put invariant-checking in different commits and force the auditor to reconstruct cross-commit reasoning. One phase, one commit per concern (3 commits), audited together.

## Anchor drift vs spec (re-pinned 2026-09-07)

| Spec says | Live tree now |
|---|---|
| `_on_text_delta` :995-1002 | :994-1002 (unchanged shape) |
| `_do_text_delta` accumulate :1047 | :1046 |
| throttle check :1070-1076 | :1072-1075 |
| `update_streaming` print | :509 (was :508) |
| `_stream_throttle_sec = 0.15` :209 | :209 ✓ |
| `_live_update` timer :604 | :604 ✓ |
| `_idle_pulse` timer :699 | :699 ✓ |

## Correctness invariants (from spec — PRESERVE ALL, test each)

1. `_streaming_text` accumulation always complete (final render + crabcard extraction depend on it — `_do_response_complete` reads via CRH `get_streaming_text`).
2. `sb.plain_text` updated BEFORE every throttle-return path (completion reads it).
3. Stale/ended guards (`_ended_sessions`, `_turn_tokens` mismatch) keep working against deferred dispatches.
4. Empty-delta skip preserved.
5. Producer→idle→completion ordering preserved (deltas scheduled before completion's dispatch run before it).
6. Trailing guarantee: the last delta batch always produces one final `update_streaming` before completion reads `plain_text`.

## Part A — Producer-side delta coalescing (`ui/handlers/agent_runtime_handler.py`)

1. Move accumulation from `_do_text_delta` (:1046) into `_on_text_delta` (producer/runtime thread): `self._streaming_text[session_key] = self._streaming_text.get(session_key, "") + text` runs on the producer; per-session single producer makes this race-free (GIL-atomic str assignment).
2. Add `self._delta_dispatch_pending: set[str] = set()`.
3. In `_on_text_delta` (after the empty-text skip, before idle_add): schedule idle_add ONLY when (`session_key not in self._delta_dispatch_pending`) AND (`now - self._last_delta_dispatch.get(sk, 0.0) >= self._delta_throttle_sec`). Record pending. Pass the CURRENT `_turn_tokens.get(session_key)` — NOT the delta's token (the dispatch may outlive multiple deltas; it must carry the turn it will render).
4. At the END of `_do_text_delta` (after processing, after all guards/returns — use try/finally or restructure so every return path clears pending): `self._delta_dispatch_pending.discard(session_key)`; if any delta arrived during processing (check accumulation length or a dirty flag), schedule one trailing dispatch.
5. `_do_text_delta` no longer receives/appends delta text — it reads `self._streaming_text[session_key]` for `update_streaming`. Keep ALL guards (crh-None, ended, token-mismatch — token check now compares the DISPATCH's token vs current).
6. Keep `_delta_throttle_sec = 0.05`.

**Tests (new, in the existing agent-runtime-handler test file — find it by grepping tests/ for `AgentRuntimeHandler`):** simulate ≥100 rapid deltas via `_on_text_delta` with a stubbed GLib (existing DeferredGLib-style pattern); assert (a) `_do_text_delta` invocations ≤ ~deltas/20 + 2, (b) final accumulated text contains every delta exactly once, (c) a pending dispatch exists for the last delta (trailing guarantee), (d) a delta arriving with a MISMATCHED token while pending is dropped and does not corrupt accumulation, (e) delta after `_ended_sessions` set is dropped (no new bubble).

## Part B — Render throttle + unchanged guard (`ui/handlers/chat_render_handler.py`)

1. `_stream_throttle_sec`: 0.15 → **0.5**.
2. Unchanged-skip: track per-session last-rendered text (`self._last_rendered_text: dict[str, str]`); in `update_streaming`, after the `sb.plain_text = delta_text` assignment and BEFORE the throttle check: if `delta_text == self._last_rendered_text.get(session_key)` → return early (no set_text). On every actual `set_text`, record it. NOTE: the label shows `text + " ▍"` — compare the STORED plain text, not the cursor'd display string.
3. Replace the `print(f"[STREAM] ...")` at :509 with `_logger.debug(...)` (module already has `_logger`).
4. Clear `_last_rendered_text[session_key]` wherever `_last_stream_update` is cleared (find all sites — grep `_last_stream_update`) so a new streaming session doesn't inherit a stale skip.

**Tests:** throttle still bounds set_text calls (stubbed monotonic or injected clock); identical consecutive texts → zero additional set_text; `sb.plain_text` still updates when set_text is skipped (invariant 2).

## Part C — Single status ticker (`ui/handlers/activity_handler.py`)

1. Consolidate `_live_update` (200ms) + `_idle_pulse` (200ms) into ONE 250ms ticker: `_set_state` starts/stops the single timer; the tick branches on `self._state` (streaming/reasoning/tool_use → live-update branch; idle → idle-pulse branch).
2. Skip-when-unchanged: cache `(state, phase, hop_bucket, elapsed_bucket)` per tick; if identical to last rendered, skip the `_update_feedbar` markup rebuild AND `_streaming_label()` construction.
3. Hoist `_resolve_agent_name(payload)` in `on_gateway_event` to ONE call at the top; pass the resolved name down (currently 6× with the same payload — verify count first, adjust if drifted).
4. Preserve timer lifecycle semantics: `_stop_live_update`, `_stop_idle_pulse`, done-flash, send-initiated timers keep their public behavior (they now stop the single ticker). Update their docstrings, not their call sites.

**Tests:** (a) two consecutive ticks with unchanged state → no markup rebuild (spy on `_update_feedbar`/`_streaming_label`), (b) state transition restarts the ticker correctly, (c) `_resolve_agent_name` called ≤1× per event, (d) existing activity-handler tests stay green (find the suite — grep tests/ for `ActivityHandler`).

## Verification (all pasted)

1. All new tests green, with red-first evidence where the spec demands it (Parts A/B/C behavioral tests must fail on pre-change code — paste one failing example per part minimum).
2. Existing suites green: the agent-runtime-handler suite, chat-render suite (`test_chat_render_handler.py`, `test_render_error_callbacks.py`), activity suites (`test_activity_bubbles.py`, `test_activity_wiring_handler.py`), `test_agent_runtime.py` (chunked per AC1 OOM constraint).
3. Invariant checks: your Part-A test (e) and the completion-path test from the existing suite (a full streaming session's final text must be byte-identical to delta concatenation — extend an existing completion test if none asserts this).
4. pyflakes: undefined-name 0; no new findings on the 3 files.
5. LOC accounting per commit.

## Commits (3, one per part, in order A → B → C)

1. `perf(runtime-handler): coalesce text-delta dispatches — idle_add at most ~20/sec per session (trailing guaranteed)`
2. `perf(render): 500ms streaming-label throttle + unchanged-skip + debug print cleanup`
3. `perf(activity): single 250ms status ticker + skip-when-unchanged + hoisted agent-name resolution`

## COMPLETENESS checklist
- [ ] Discovery block (3 files read in full; the 6× _resolve_agent_name claim re-verified)
- [ ] Red-first evidence for at least one test per part
- [ ] All invariants preserved + tested (the 6 from spec)
- [ ] All suite outputs pasted
- [ ] pyflakes clean
- [ ] Related issues flagged, not silently fixed

**STOP after this phase.** This is the only phase — after audit, supervisor runs unit gates + post-mortem and closes Unit #3.
