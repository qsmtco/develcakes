# SPEC: Agent Runtime — Correctness, Speed, and Dead-Code Cleanup (SPEC-61)

**Date:** 2026-11-03
**Author:** Claude (read-only audit for Micah)
**Status:** Draft — for implementation
**Implements:** Agent-runtime efficiency audit (chat, 2026-11-03)
**Depends on:** SPEC-21 (HTTP keep-alive, token memo), SPEC-59 (context tiers), SPEC-60 (iteration judge) — no behavior of those specs is removed
**Target branch:** main

> Complies with docs/ARCHITECTURE.md §3.21m: `AgentRuntime` owns the turn lifecycle, cancellation, the wire build, and tool execution; `agent/llm/` owns transport (§3.21m.2); `utils/` imports nothing from `agent/` (layer rule, §3.11d). No UI behavior changes; the only `ui/` edit is one stale comment.

---

## DISCOVERY

> Starting Spec Discovery — reading all referenced source files before writing any spec content.

Every line number below was read on 2026-11-03 against the working tree. The probe results in A1 came from running the real `AgentRuntime` in-process with a fake `_call_llm` (`.venv/bin/python`, Python 3.12.3).

**Turn lifecycle and cancellation (`agent/runtime.py`, 5444 lines)**
- `begin_send` (1491-1509) claims `_active_loops` under `self._lock` before the thread starts. `start_claimed_loop` (1516-1534) starts `_run_loop` and releases the claim inline on thread-start failure. `_finish_loop` (1543-1548) exists but production never calls it; only one test does.
- `_run_loop` (1966-1985): adds `_active_loops` again (1970), then under `_state_lock` sets `_turn_tokens[sk] = turn_token`, `_turn_state[(sk, turn_token)] = RUNNING`, and `_cancelled_epochs.discard(sk)`.
- Loop top (2124-2157): first `if self._cancel_requested:` → reset to False, CANCELLED with `metadata={"reason": "shutdown", ...}`, `return` (2125-2140). Then under `self._lock`, `if session_key in self._cancelled:` → `discard`, CANCELLED with reason `"user"`, `return` (2142-2157). **2144 is the only place `_cancelled` is ever cleared.**
- `finally` (2666-2679), under `self._lock`: pops `_inbox`, discards `_active_loops`, moves pending text to `_stranded`, calls `_flush_pending_room_lines`. It does not touch `_cancelled`.
- `cancel()` (1611-1676): under `self._lock`, adds to `_cancelled` (1630) and `_cancelled_epochs` (1639), sets `_cancel_requested = True` (1641), drops `_inbox`/`_stranded`, flushes pending approvals. Outside the lock it dispatches `CANCEL_MESSAGE` with the active token. It does not check `_active_loops`.
- `cancel_peer()` (1678-1699): adds to `_cancelled` and `_cancelled_epochs`, but not `_cancel_requested`. No `_active_loops` check.
- `stop_all()` (1701-1768) calls `cancel(sk)` for each session in `_active_loops`.
- `_inactive_turn_reason` (3304-3323) returns `"superseded"` when the token was replaced, `"cancelled"` when `sk in _cancelled_epochs`, else `None`. It takes no lock (it reads with `getattr`). `TurnInactive(reason)` is defined at 129-140.
- `_call_llm_chain` (3115-3199) checks `_inactive_turn_reason` before every attempt (3125-3127) and catches `Exception` around `_call_llm_once` (3153). `_attach_provider_failure` (1055-1080) returns `None` for non-provider exceptions when `stage != "request_boundary"`, and the chain re-raises (3156-3157). `is_provider_exception` (`agent/llm/failure_classifier.py:261-276`) is False for a plain `TurnInactive` (no `_failure`, no status, no transient cause). `_run_loop` catches `TurnInactive` at 2280 and calls `_exit_inactive_turn` (3276-3302), which records CANCELLED with reason `"user"`.
- `_call_llm_once` (3488-3716) catches only `(IndexError, KeyError, TypeError, ValueError)` around the streaming call (3675) and the blocking call (3709). `TurnInactive` passes through untouched.
- `_terminate_turn` (858-1053) writes `_turn_state[state_key]` and `_turn_results[state_key]` (953-954, key = `(session_key, turn_token)`). No code removes old keys.
- No production code reads reason `"shutdown"`; the only other mention is the `TurnResult` docstring (211).

**Streaming (`agent/runtime.py` 3718-3868, `agent/llm/streaming.py`)**
- `_call_llm_streaming`: `full_content += text; self._last_stream_text[session_key] = full_content` on every `text_delta` (3780-3785). There is no cancel check anywhere in the event loop (3779-3858). The result is always OpenAI-shaped: `{"choices": [{"message": {"content", "tool_calls"}}], "usage": captured_usage}` (3833-3836, 3865).
- `_last_stream_text` readers are all on the loop thread after the stream returns: 3178 (pop on success), 3370 and 3386 (failover note and partial discard), 3393 (pop), and 3410 (set).
- `stream_with_ssl_retry` (streaming.py 695-790) is a generator. Its handlers catch specific connection errors, not `GeneratorExit`, so `.close()` ends it cleanly.
- Keep-alive: `_keepalive_enabled()` returns `get_env("HTTP_KEEPALIVE") == "1"` (215-221). `_eligible` (389-399) needs POST, https, and no https proxy (397-398). `_checkout` builds `http.client.HTTPSConnection(host, port, timeout=timeout)` (249). `_open` falls back to `urllib.request.urlopen(req, timeout=timeout)` (461-467).
- `tests/conftest.py:190-200` (autouse) deletes `DEVELCAKES_HTTP_KEEPALIVE` for every test except `test_http_keepalive.py`, so the suite relies on keep-alive being off by default. `test_http_keepalive.py:160-174` (`test_gate_off_never_pools`) deletes the variable and expects no pooling. The keep-alive tests trust a self-signed cert by patching `ssl._create_default_https_context` (150-151).
- Several fakes are `def fake_urlopen(req, timeout=None)` (test_agent_runtime.py 2571, 2614, 2672, 2736, 2784; test_failure_classifier.py 791; test_reasoning_effort.py 45). Passing a new keyword such as `context=` to `urlopen` would break them.

**Wire build and compaction**
- Per iteration, `_run_loop` resolves tools (2194-2207), calls `_compact_before_llm` (2209-2211), and then builds the wire **again** with `_wire_messages_and_count(session_key, conv, model_max, tools)` (2262-2264). `model_max = hard_ceiling` (2188).
- `_compact_before_llm` (4504-4642) starts by building the wire and keeping only `pressure` (4524-4526). Tests call it directly with four positional args (test_context_tiers.py, test_panic_prune.py, test_wire_context_pressure.py, test_context_strategy_tier0.py); none mock it.
- `_wire_messages_and_count` (4228-4276) calls `_repo_map_block_for` **before** blanking the block for `agent_role == "coder"` (4251-4253). `_repo_map_block_for` (4008-4033) runs `build_repo_map(project, goal)` whenever `(project, hash(goal), _repo_map_tree_version)` changes, and every write bumps the tree version.
- Every compaction write path calls `conv.invalidate_token_estimate()`, which bumps `Conversation._token_gen` (`models/conversation.py:360, 370-378`). That covers the trim loop (context_strategy.py 318), the orphan sweep (337, only when changed), `prune_tool_outputs` (539), `panic_prune` (646) and `offload_tool_results` (755). An under-budget `compact()` changes nothing (146-340 traced).
- `force_llm_compact` (5284-5361) assigns `self._context_strategy = strat` (5332) for the duration of the call and restores it in `finally` (5344). `_compact_before_llm` calls `self._context_strategy.compact/offload_tool_results/panic_prune` (4536-4637) without `_compaction_lock`, and `_run_loop` reads `self._context_strategy.last_result` (2226). `ui/agent_runtime/session.py:202` uses the return value of `force_llm_compact`. 223 reads `last_result` only after the non-LLM `force_compact`.

**Tool batch (`_execute_tool_batch`, 2681-2922)**
- The batch runs serially (2721). For read tools nothing happens before execution: the refusals at 2726, 2743, 2754 and 2770 are exec- or truncation-only, and the approval at 2786 is write-only. `bypass_approval` is False for read tools, so `per_call_cb` is `None` (2819-2822). Execution goes through `self._tool_chain.run(tool_name=, args=, ctx=, executor=lambda: execute_tool(...))` (2857-2866). There is no early `return` inside the loop; the only return is `return False` at 2922.
- No `contextvars` or `threading.local` use in `agent/tools.py`, `agent/context.py`, `agent/group_tools.py`, `agent/persistence.py`, or `utils/`.
- Tests that patch `agent.tools.execute_tool` (test_agent_runtime.py 332, 1545, 2305, 5562, 5598) use a fixed `return_value`; none use an ordered `side_effect` list. Batch tests send one call per batch.

**Providers, enforcement, Anthropic**
- `load_providers()` (`utils/providers_store.py:254-266`) reads and parses YAML on every call. `_parse` (191-223) has no side effects other than logging. `ProviderConfig` (`models/providers.py:39-62`) is a dataclass with primitive fields only. `save_providers` writes atomically (tmp file, then rename). The runtime calls `load_providers()` on every LLM call (3564-3569) and in `_live_provider_card` (3473-3474), which `_effective_api_key` uses (3442), and in `_switch_to_fallback` (3340-3341).
- `enforcement.check()` runs tests (1630-1633), then lint (1636-1639), with the same arguments. Both are gated on `syntax_gate` and nothing else. `EnforcementConfig.test_run` and `lint_check` default to True (`agent/config.py:60-61`). Coders turn both off (runtime.py 2844-2846).
- `TestConfig.timeout_seconds: int = 60` (enforcement.py 236), and `from_dict` uses `_int("timeout_seconds", 60)` (279). So line 1215's `if test_config.timeout_seconds is not None else config.test_timeout_seconds` can never use agent.json's `test_timeout_seconds` (`config.py:63, 288`).
- `_call_for_summary` (5363-5444): `provider_name, model = model_id.split("/", 1)` (5390), then `provider.call(model=model)` (5428). Every provider then applies `model_id(model)` again (`cost.py:25-32`; openai_provider 70/115, minimax 105/154, anthropic 63/117). `_call_llm_once` passes the full `conv.model` (3534, 3698). Format lookup: `_RESPONSE_FORMAT.get(provider_name, "openai")` (5442).
- `_run_loop` picks the parser format by **model prefix**, `_RESPONSE_FORMAT.get(loop_provider, "openai")` (2288-2290), but `_RESPONSE_FORMAT` (294-297) is keyed by **caller**. `_resolve_caller_key` (3040-3050) is a `@staticmethod` that returns `provider_cfg.caller.lower()` or `""`.
- `AnthropicProvider.stream` (92-177): `content_block_start` yields `"name": ""` (153-158); it handles a non-existent `tool_use_delta` delta type (164-171), while the real Anthropic event is `input_json_delta` with `partial_json`. It reads usage only from `message_delta` (172-175), never `message_start`. `max_tokens` is 4096 (119). The fixture in `tests/test_agent_runtime.py:1737-1749` uses the fake `tool_use_delta` shape.
- `extract_usage` (extractors.py 130-146) reads `input_tokens`/`output_tokens` for `"anthropic"` and `prompt_tokens`/`completion_tokens` otherwise.

**Architecture owners.** Turn and cancel state: `AgentRuntime` (§3.21m). Transport: `agent/llm/streaming.py` (§3.21m.2). Provider YAML: `utils/providers_store.py` (§3.11d). Post-write checks: `agent/enforcement.py` via `EnforcementMiddleware` (§3.21m.1).

**Existing patterns to copy.** `_inactive_turn_reason` is the lock-free cancel predicate the provider chain already uses. The `ssl_retry_budget` contextvar shows how transport behavior is scoped per call. `_TEST_CONFIG_CACHE`/`_ENFORCEMENT_CONFIG_CACHE` show the module-level cache style.

---

## 1. Overview

### Problem

The agent runtime has three kinds of debt:

1. **Correctness bugs that cost time.** Pressing Stop can cancel the user's *next* message before it reaches the model (confirmed by probe). Stop doesn't interrupt a reply that is already streaming. Per-turn records are never freed.
2. **Avoidable latency on the hot path.** Each provider call opens a fresh TCP and TLS connection. Each iteration builds and token-counts the full wire twice. Read-only tool calls in one batch run one after another. Coders rebuild a repo map that is then thrown away.
3. **Dead code and stale commentary.** This makes a redesign harder to scope.

### Solution summary

Four phases, each shippable by itself:

- **Phase A, correctness:** A1–A7 below.
- **Phase B, speed:** B1–B8.
- **Phase C, dead code and stale text:** a table of deletions, each with evidence.
- **Phase D, redesign proposals:** not implementation-ready; each needs its own spec.

| Priority | Item | Effect |
|---|---|---|
| High | A1 stale cancel | Next message silently cancelled after Stop |
| High | A2 cancel mid-stream | Stop waits for the whole reply (and pays for it) |
| High | B1 keep-alive on by default | Saves a TCP+TLS handshake per provider call |
| High | B3 parallel read-only tools | Multi-read batches finish in about the time of the slowest read |
| Medium | A3, A4, B2, B4, B5 | Memory leak, cross-session compaction race, duplicate wire build, wasted repo-map builds, serial test+lint |
| Low | A5, A6, B6, B7, B8 | Config fallback, OpenRouter summary id, YAML re-parse, stream string copies, compiled-path count |
| Latent | A7 | Anthropic streaming is broken, but no configured card uses the anthropic caller today |

### Scope

| In scope | Out of scope |
|---|---|
| `agent/runtime.py`, `agent/llm/streaming.py`, `agent/llm/anthropic_provider.py`, `agent/enforcement.py`, `utils/providers_store.py`, Phase C deletions across `agent/` | Any UI behavior; `ui/agent_runtime/turn_ui.py` streaming accumulation (Phase D) |
| Test edits listed per item, plus new `tests/test_spec61_runtime.py` | Provider retry-layer redesign, `SessionState` refactor, `runtime.py` split (Phase D) |
| `tests/conftest.py` keep-alive gate (B1 only) | Non-streaming provider calls cannot be interrupted mid-request; unchanged |
| docs/ARCHITECTURE.md updates (§8) | Changing iteration limits, the judge, or compaction tiers |

### Principles

- **A cancel belongs to the turn it was pressed in.** It must never outlive that turn.
- **Same results, less work.** Phase B changes cost, not output. A speed-up that changes a wire byte, a tool result, or tool-result order is a bug.
- **Order-preserving parallelism only.** Middleware, dedup, stuck detection, audit, and conversation appends stay in call order.
- **Tests that assert removed behavior are edited, named here, and not silently deleted.**

---

## 2. Changes by Item

Each item lists its files, the current code, the change, and the tests it touches. The file-level roll-up is in §4.

### A1 — A cancel outlives its turn and kills the next one (High)

**What happens today (probe-confirmed).**
- *Stop pressed mid-turn:* `cancel()` sets `_cancelled` and the shared `_cancel_requested`. At the next loop top, the `_cancel_requested` branch (2125-2140) ends the turn with reason `"shutdown"` and returns *without* discarding `_cancelled`. The user's next message starts a new loop, whose first check (2142-2157) finds the stale `_cancelled` entry and cancels it before any model call. The message after that works.
- *Stop pressed as a turn finishes:* the loop has already returned, so neither flag is consumed. The next message is cancelled by `_cancel_requested`, and the one after it by `_cancelled`. Two lost messages.
- `cancel_peer` on an idle peer leaves a `_cancelled` entry that cancels the peer's next turn.

**Fix.**

1. Remove the shared flag entirely, because every cancel is per-session.
   - Delete the `__init__` field (714) and the stale comment after it (716-718).
   - Delete the assignment in `cancel()` (1641).
   - Delete the whole loop-top branch at 2125-2140.
   - All cancels now end with reason `"user"`.
2. Mark `_cancelled` only for a live loop, in both `cancel()` and `cancel_peer()`. The epoch (`_cancelled_epochs`) stays armed unconditionally, as today. The approval guard reads it, and the next turn start clears it.
3. Clear `_cancelled` when the loop exits, in the `finally` and in `start_claimed_loop`'s failure path.

`cancel()`, lines 1628-1641 become:

```python
        with self._lock:
            # Only a live loop consumes _cancelled; a mark on an idle session
            # would cancel the user's next turn.
            if session_key in self._active_loops:
                self._cancelled.add(session_key)
            # SP3 fix round 2 (BUG#2): arm the cancellation epoch. The approval
            # registration guard reads it. Cleared at the next turn's start and
            # on COMPLETED/FAILED terminals.
            self._cancelled_epochs.add(session_key)
            # SPEC-WIRE-CONTEXT-PRESSURE §2.1: queued and stranded text is
            # dropped on cancel; the loop's finally releases the claim.
            self._inbox.pop(session_key, None)
            self._stranded.pop(session_key, None)
            # ... pending-approval flush unchanged ...
```

`cancel_peer()`, lines 1690-1692 become:

```python
        with self._lock:
            if session_key in self._active_loops:
                self._cancelled.add(session_key)
            self._cancelled_epochs.add(session_key)
            # ... approval flush unchanged ...
```

`_run_loop` `finally` (2674-2679):

```python
            with self._lock:
                pending = self._inbox.pop(session_key, [])
                self._active_loops.discard(session_key)
                self._cancelled.discard(session_key)
                if pending:
                    self._stranded[session_key] = pending
                self._flush_pending_room_lines(session_key)
```

`start_claimed_loop` failure path (1530-1533): add `self._cancelled.discard(session_key)` next to the existing `_active_loops.discard`.

**Traced cases.**
- Stop pressed during `prepare()` or before the first iteration: `begin_send` has already claimed `_active_loops`, so the session is marked. The turn-start `_cancelled_epochs.discard` (1985) doesn't touch `_cancelled`, so the first loop-top check (2142) ends the turn. Same as today.
- Stop pressed after the last check: the loop finishes (COMPLETED or FAILED), and `finally` clears the mark.
- Stop pressed while idle: nothing is marked, the epoch is armed, and the next turn start clears it.
- `stop_all` only cancels sessions in `_active_loops`, so they are always marked. It still works.

**Docstrings and comments to update (same commit).**
- `runtime.py`: 211 (`"user"|"shutdown"` becomes `"user"`), 559, 705-713 (drop the `_cancel_requested` clause), 1632-1638, 1679-1686 (`cancel_peer` docstring: the "global flag" rationale is gone), 1705, and 2149-2153.
- `ui/window.py:947`: the comment cites `_cancel_requested`; reword it to "cancel_peer marks only the peer session".

**Tests to edit.**

| Test | Edit |
|---|---|
| `tests/test_cancel_peer.py:9-19` | Before the two `cancel_peer` calls, add `rt._active_loops.update({"agent:coder", "agent:debugger"})`. Delete the five `_cancel_requested` asserts (11, 15, 19, 39, 47). Rename to `test_cancel_peer_marks_live_sessions`. Update the module docstring. |
| `tests/test_error_surfacing.py:699-719` | Delete `test_cancel_requested_site_dispatches_cancel_message`; the site no longer exists. Update the class docstring at 674. `test_cancelled_set_site_dispatches_cancel_message` (721+) stays valid as is: it pre-seeds `_cancelled`, the first check fires, and `finally` clears it. |
| `tests/test_agent_runtime.py:5572-5609` | In `trigger_cancel`, replace `rt._cancel_requested = True` with `rt.cancel(sk)`, and update the docstring. The session is in `_active_loops` (added at 1970), so it is marked, and the next iteration's check fires CANCELLED. |
| `tests/test_stop_all.py` | Delete lines 287, 472, 475 and 490, plus the comments that explain them (271-272, 284-286, 473-474, 486-487). Every assertion stays valid. |

**New tests** (`tests/test_spec61_runtime.py`):

```python
def test_stop_after_turn_end_does_not_cancel_next_turn(tmp_path):
    rt = AgentRuntime(_make_cfg(), GLib=None)
    rt.start()
    sk = "agent:spec61-a1"
    rt.create_conversation("Coder", sk, str(tmp_path))
    with patch.object(rt, "_call_llm", return_value=_resp("hi")) as llm:
        rt._run_loop(sk, "one")
        rt.cancel(sk)                       # Stop pressed after the turn ended
        rt._run_loop(sk, "two")
    assert llm.call_count == 2
    assert rt.get_last_turn_result(sk).status == TurnStatus.COMPLETED
    rt.stop()


def test_stop_mid_turn_cancels_only_that_turn(tmp_path):
    rt = AgentRuntime(_make_cfg(), GLib=None)
    rt.start()
    sk = "agent:spec61-a1b"
    rt.create_conversation("Coder", sk, str(tmp_path))
    calls = []

    def llm(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            rt.cancel(sk)
            return _resp("", tool_calls=[{
                "id": "c1",
                "function": {"name": "list_files", "arguments": '{"path": "."}'},
            }])
        return _resp("hi")

    with patch.object(rt, "_call_llm", side_effect=llm), \
         patch("agent.tools.execute_tool", return_value=ToolResult(success=True, output="ok")):
        rt._run_loop(sk, "one")
        first = rt.get_last_turn_result(sk)
        rt._run_loop(sk, "two")
    assert first.status == TurnStatus.CANCELLED
    assert first.metadata["reason"] == "user"
    assert rt.get_last_turn_result(sk).status == TurnStatus.COMPLETED
    assert len(calls) == 2
    rt.stop()


def test_cancel_peer_on_idle_session_leaves_no_mark():
    rt = AgentRuntime(config=AgentConfig())
    rt.cancel_peer("agent:coder")
    assert "agent:coder" not in rt._cancelled
    assert "agent:coder" in rt._cancelled_epochs
```

Copy `_resp` and `_make_cfg` from `tests/test_agent_runtime.py:59-66, 235-251`. Before the fix, both loop tests fail because turn two never calls the provider (one recorded call instead of two). The mid-turn test also fails its `reason == "user"` assert, because today's first check reports `"shutdown"`.

Estimate: runtime −22 / +8 lines.

---

### A2 — Stop doesn't stop a reply that is already streaming (High)

**Today.** `cancel()` arms the epoch, but `_call_llm_streaming` never checks it. The reply streams to the end (tokens are billed, and deltas keep reaching the UI after "Cancelled"). Only then does the loop-top check end the turn.

**Fix.** Check `_inactive_turn_reason` once per SSE event. It is lock-free and costs a few dict and set lookups. On a hit, close the generator, which runs the provider's `with urlopen... as resp` exit and closes the socket, then raise `TurnInactive`. The exception passes through:
- `_call_llm_once`'s narrow except (3675);
- `_call_llm_chain` (3153-3157: no `_failure`, so it is re-raised);
- `_run_loop` 2280, then `_exit_inactive_turn`, which records CANCELLED with reason `"user"`, or returns silently on `"superseded"`.

In `_call_llm_streaming`, at the top of the `for ev in _stream:` body (3779):

```python
        for ev in _stream:
            inactive = self._inactive_turn_reason(session_key, turn_token)
            if inactive is not None:
                _stream.close()
                self._last_stream_text.pop(session_key, None)
                raise TurnInactive(inactive)
            if ev.type == "text_delta":
                ...
```

The drain loop after `done` (3843) needs no check, because the reply is already complete.

**Limits (documented, not fixed).**
- The check runs only when an event arrives, so a stalled stream still waits for its socket timeout.
- Blocking `provider.call` requests (when `supports_streaming=False` or no `on_text_delta`) can't be interrupted. They are caught by the chain check (3125) before the next attempt.

**Tests.** `TurnInactive` is new to the streaming path. Existing streaming tests never cancel, so none change.

New test: call `rt._call_llm_streaming(...)` directly with `_get_provider` patched to return an object whose `stream(**kw)` is a generator yielding three `SSEEvent("text_delta", {"content": "x"})` events and recording each yield. Register `rt._on_text_delta` as a callback that calls `rt.cancel(sk)` on the first delta. With `GLib=None`, `_dispatch` runs it inline. Assert:
- `pytest.raises(TurnInactive)`;
- the fake generator yielded exactly 2 events (the check sits at the top of the next iteration);
- `sk not in rt._last_stream_text`.

Estimate: +6 lines.

---

### A3 — Per-turn records are never freed (Medium)

**Today.** `_turn_state` and `_turn_results` gain one `(session_key, turn_token)` entry per turn (1981, 953-954), and nothing removes them. Each `TurnResult` holds the reply text, so memory grows with every turn for the life of the app.

**Fix.** When a turn registers, drop that session's older entries under `_state_lock`. Nothing reads them afterwards:
- `_terminate_turn` rejects a stale token before touching `_turn_state`;
- `get_turn_state` and `get_last_turn_result` read only the active token;
- the STREAMING transition (2273-2275) on a superseded loop finds no entry and does nothing.

Lines 1979-1985:

```python
        with self._state_lock:
            for key in [k for k in self._turn_state if k[0] == session_key]:
                self._turn_state.pop(key, None)
                self._turn_results.pop(key, None)
            self._turn_tokens[session_key] = turn_token
            self._turn_state[(session_key, turn_token)] = TurnStatus.RUNNING
            self._cancelled_epochs.discard(session_key)
```

Both dicts use the same key and are written together (953-954). The scan is O(entries), and entries are now bounded by the number of sessions.

**Tests.** `tests/test_provider_failover.py:299-319` reads the current token's entries, so it is unaffected. New test: run three `_run_loop` turns with distinct tokens, then assert `len([k for k in rt._turn_state if k[0] == sk]) == 1`.

Estimate: +3 lines.

---

### A4 — `force_llm_compact` swaps the runtime-wide strategy (Medium)

**Today.** For the length of an LLM summary call, which can take seconds, `self._context_strategy` points at an `LLMSummarizeStrategy` whose summarizer is bound to *this* `conv` (5322-5332). Any other session on the same runtime that reaches `_compact_before_llm` in that window compacts through it. That means an unplanned LLM call billed to the wrong conversation's provider, and that session's `last_result` comes from the wrong strategy. `_compact_before_llm` doesn't take `_compaction_lock`, so the lock doesn't help.

**Fix.** Call `strat.compact(...)` directly, without assigning it. Delete 5306 (`original_strategy = ...`), 5332 (`self._context_strategy = strat`), and 5344 (`self._context_strategy = original_strategy`), and remove the FIX-BUG-3 paragraph from the docstring (5296-5301). The return value already comes from `strat.last_result` (5348). `session.py:202` uses that return value and doesn't read `rt._context_strategy`.

**Tests.** `tests/test_llm_summarize_strategy.py:102-124` (`test_strategy_swapped_and_restored`) builds the runtime with `AgentRuntime.__new__`. `_compaction_lock` therefore doesn't exist, the `AttributeError` is swallowed, and the final assert passes without exercising anything. Rewrite it as `test_shared_strategy_untouched_during_llm_compact`:
- build `AgentRuntime(_make_cfg())`;
- make the patched `_call_for_summary` side effect record `rt._context_strategy is original` *during* the call, then raise `RuntimeError`;
- assert the recorded value is True and the strategy is unchanged afterwards.

Estimate: −5 lines.

---

### A5 — `enforcement.json` can never fall back to agent.json's test timeout (Low)

**Today.** `TestConfig.timeout_seconds` is always an int (default 60), so the `None` fallback at enforcement.py 1215 is dead. `EnforcementConfig.test_timeout_seconds` from agent.json is ignored.

**Fix.** `agent/enforcement.py`:

```python
    timeout_seconds: int | None = None       # Per-project override; None uses EnforcementConfig.test_timeout_seconds
```

and in `from_dict` (279):

```python
            timeout_seconds=(
                _int("timeout_seconds", 60) if "timeout_seconds" in data else None
            ),
```

Line 1215 stays as it is and now works. When the key is present, an invalid value still falls back to 60 with the existing debug log. No test references `TestConfig.timeout_seconds` (grep: zero hits in `tests/`). New test: `TestConfig.from_dict({}).timeout_seconds is None`, and `from_dict({"timeout_seconds": 5}).timeout_seconds == 5`.

Estimate: +3 lines.

---

### A6 — Summary calls strip three-part model ids twice (Low; affects OpenRouter)

**Today.** For `model_id = "openrouter/deepseek/deepseek-v4-pro"`, `_call_for_summary` splits off `openrouter` and passes `deepseek/deepseek-v4-pro`. The provider's `model_id()` then strips `deepseek/`, and the API receives `deepseek-v4-pro`. The normal turn path passes the full `conv.model` and is stripped once. Two-part ids such as `zai/glm-4.6` are unaffected, because `model_id("glm-4.6")` returns its input.

**Fix.** At 5428, change `model=model,` to `model=model_id,`. Keep the split at 5390, because `provider_name` is still used (5397, 5419). `model` stays as the second argument to `_resolve_caller_key` at 5405 (a staticmethod that ignores it).

**Tests.** `tests/test_reasoning_effort.py:275-287` asserts only `reasoning_effort` and is unaffected. New test: `_CapturingProvider` (copy it from test_reasoning_effort.py) receives `model == "p1/m1"` for `model_id="p1/m1"`.

Estimate: ±1 line.

---

### A7 — Anthropic streaming is broken (Latent)

No configured card uses the `anthropic` caller today, so nobody has hit this. Fix it before anyone adds one.

1. **Tool name dropped.** `content_block_start` yields `"name": ""` (155). Use `block.get("name", "") or ""`.
2. **Arguments never arrive.** The real delta type is `input_json_delta` with `partial_json`; `tool_use_delta` (164) doesn't exist.
3. **Usage lost.** Input tokens arrive only in `message_start.message.usage`, which is never read. The streamed result is OpenAI-shaped, so emit OpenAI usage keys.
4. **Wrong parser for the shape.** Streamed responses are OpenAI-shaped for every provider, but `_run_loop` picks the `"anthropic"` parser by model prefix, so text and tool calls would come back empty. Pick by shape, then by caller, then by prefix.

`agent/llm/anthropic_provider.py`, a module helper:

```python
def _merge_usage(acc: dict, raw: dict) -> bool:
    """Fold Anthropic usage into OpenAI keys. True when a value changed."""
    changed = False
    if isinstance(raw.get("input_tokens"), int):
        acc["prompt_tokens"] = raw["input_tokens"]
        changed = True
    if isinstance(raw.get("output_tokens"), int):
        acc["completion_tokens"] = raw["output_tokens"]
        changed = True
    return changed
```

In `stream()`, add `usage: dict = {}` before the `with`, and replace the event branches at 150-175:

```python
                if etype == "message_start":
                    if _merge_usage(usage, (d.get("message") or {}).get("usage") or {}):
                        yield SSEEvent(type="usage", data={"usage": dict(usage)})
                elif etype == "content_block_start":
                    block = d.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        yield SSEEvent(type="tool_call_delta", data={
                            "index": d.get("index", 0),
                            "name": block.get("name", "") or "",
                            "arguments": "",
                            "id": block.get("id", "") or "",
                        })
                elif etype == "content_block_delta":
                    delta = d.get("delta") or {}
                    dtype = delta.get("type", "")
                    if dtype == "text_delta":
                        yield SSEEvent(type="text_delta", data={"content": delta.get("text", "")})
                    elif dtype == "input_json_delta":
                        yield SSEEvent(type="tool_call_delta", data={
                            "index": d.get("index", 0),
                            "name": "",
                            "arguments": delta.get("partial_json", "") or "",
                            "id": "",
                        })
                elif etype == "message_delta":
                    if _merge_usage(usage, d.get("usage") or {}):
                        yield SSEEvent(type="usage", data={"usage": dict(usage)})
                elif etype == "message_stop":
                    yield SSEEvent(type="done", data={})
                    return
```

Anthropic's `output_tokens` is cumulative, so it is assigned, not added. The runtime keeps the most recent usage event (3804-3810), which now carries both keys.

`agent/runtime.py` 2288-2290:

```python
                    model = conv.model or self._config.default_model
                    loop_provider = model.split("/")[0] if "/" in model else model
                    if "choices" in response:
                        loop_fmt = "openai"
                    else:
                        caller = self._resolve_caller_key(self._resolve_provider_cfg(conv), model)
                        loop_fmt = _RESPONSE_FORMAT.get(caller) or _RESPONSE_FORMAT.get(loop_provider, "openai")
```

For every card in use today the result is unchanged, because their responses carry `choices` and parse as `"openai"`. The prefix fallback keeps the old result when no card resolves.

In `_call_for_summary` (5442), make the same switch:

```python
        fmt = "openai" if "choices" in response_dict else _RESPONSE_FORMAT.get(caller_key, "openai")
```

`caller_key` is already computed at 5405.

**Also note:** `max_tokens` is fixed at 4096 (65, 119). That can cut off long file writes. Make it per-card (`ProviderConfig.default_max_tokens` exists; wiring it is out of scope here, see Open Question Q5). The `logger` at line 20 is unused (Phase C).

**Tests.** Update the fixture in `tests/test_agent_runtime.py:1737-1749` to the real event shapes. Its assertions (name `read_file`, arguments string, id) stay valid:

```python
            b'event: content_block_start\n'
            b'data: {"type":"content_block_start","index":0,"content_block":'
            b'{"type":"tool_use","id":"' + ANTHROPIC_ID.encode() + b'","name":"read_file","input":{}}}\n\n'
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":0,"delta":'
            b'{"type":"input_json_delta","partial_json":"{\\"path\\":\\"/tmp/bar.py\\"}"}}\n\n'
            b'event: message_stop\n'
            b'data: {"type":"message_stop"}\n\n'
```

New test: a `message_start` with `usage: {"input_tokens": 11, "output_tokens": 1}`, followed by a `message_delta` with `usage: {"output_tokens": 7}`, yields a final usage of `{"prompt_tokens": 11, "completion_tokens": 7}`.

Estimate: anthropic +20 / −12; runtime +5.

---

### B1 — Turn HTTP keep-alive on by default (High)

**Today.** Each provider call opens a new TCP connection and a full TLS handshake, which typically costs 50–150 ms per call (a 10-iteration turn pays it 10+ times). SPEC-21 already built and tested a per-host pool. It sits behind `DEVELCAKES_HTTP_KEEPALIVE=1` with the note "default OFF until measured against a live provider".

**Step 1 — measure.** Run one multi-tool turn against the live provider with `DEVELCAKES_HTTP_KEEPALIVE=1` and once without it, and compare per-call latency in the debug log. Proceed only if pooled calls are no slower and no stale-socket errors appear. `_try_pooled` already falls back to urllib on a stale reused socket.

**Step 2 — flip the default.** `agent/llm/streaming.py` 215-221:

```python
def _keepalive_enabled() -> bool:
    """SPEC-21 SP3 / SPEC-61 B1: pooled fast path unless DEVELCAKES_HTTP_KEEPALIVE=0."""
    return get_env("HTTP_KEEPALIVE") != "0"
```

`get_env` returns `None` when the variable is unset, which counts as on. Also update the module comment at 196-197.

**Tests (required in the same commit).** Otherwise the urlopen-patched suites would open real sockets:
- `tests/conftest.py:199`: replace `monkeypatch.delenv("DEVELCAKES_HTTP_KEEPALIVE", raising=False)` with `monkeypatch.setenv("DEVELCAKES_HTTP_KEEPALIVE", "0")`, keep the `CRABCAKES_` delenv, and update the docstring.
- `tests/test_http_keepalive.py:161`: replace the delenv with `monkeypatch.setenv("DEVELCAKES_HTTP_KEEPALIVE", "0")`.
- Add `test_gate_default_on`: with both variables deleted, `_keepalive_enabled()` is True.

**Considered and rejected:** a shared module-level `SSLContext`.
- Passing `context=` to `urlopen` breaks the seven `fake_urlopen(req, timeout=None)` fakes.
- A cached context ignores the test-time patch of `ssl._create_default_https_context`.
- With pooling on, new connections are rare anyway.

**Known limit.** `_eligible` skips the pool when an https proxy is configured (397-398), so proxied users see no change.

Estimate: ±3 lines, plus 3 test lines.

---

### B2 — Build the wire once per iteration (Medium)

**Today.** `_compact_before_llm` builds the full wire (to_api_messages, repo-map lookup, compiled packet, and a token count) only to read `pressure`. `_run_loop` then builds it again at 2262. In the common case compaction changes nothing, so the second build is pure repeat work.

**Fix.** Build the wire in `_run_loop` first and pass `pressure` in. Rebuild only when compaction bumped the token generation. Every compaction write path bumps `_token_gen` (see DISCOVERY).

`_compact_before_llm` gains an optional parameter. Direct test callers still pass four positional arguments and keep today's behavior.

```python
    def _compact_before_llm(
        self,
        session_key: str,
        conv,
        soft_ceiling: int,
        hard_ceiling: int,
        tools: list | None = None,
        pressure: int | None = None,
    ) -> None:
        ...
        landing = self._compute_landing_goal(hard_ceiling)
        middle = self._compute_middle_line(hard_ceiling)
        if pressure is None:
            _messages, pressure = self._wire_messages_and_count(
                session_key, conv, hard_ceiling, tools,
            )
```

The precondition is that `pressure` was computed with `model_max == hard_ceiling` and the same `tools`, which `_run_loop` guarantees (2188).

`_run_loop` 2209-2211 and 2262-2264:

```python
                    messages, wire_tokens = self._wire_messages_and_count(
                        session_key, conv, model_max, tools,
                    )
                    wire_gen = conv._token_gen
                    self._compact_before_llm(
                        session_key, conv, soft_ceiling, hard_ceiling,
                        tools=tools, pressure=wire_tokens,
                    )
                    # ... telemetry block 2212-2249 unchanged ...
                    if conv._token_gen != wire_gen:
                        messages, wire_tokens = self._wire_messages_and_count(
                            session_key, conv, model_max, tools,
                        )
                    if wire_tokens >= model_max - self._RESPONSE_RESERVE_TOKENS:
                        raise self._over_budget_error(wire_tokens, model_max)
```

Reading `conv._token_gen` from the runtime follows existing precedent: `_compile_token_count` already calls `conv._count_text`.

**Edge.** Another session's write can bump `_repo_map_tree_version` between the two former builds. The reused wire then carries a repo map that is a few milliseconds older, which is acceptable because the map is advisory. `test_repo_map.py:109-133` (`build_repo_map` called once over two iterations) still passes.

**Tests.** New test: patch `rt._wire_messages_and_count` with a counting wrapper (`wraps=`) and run one under-budget iteration, then assert one call. A second test forces compaction (a tiny `hard_ceiling`) and asserts two calls.

Estimate: +10 / −4 lines.

---

### B3 — Run adjacent read-only tool calls in parallel (High for multi-read turns)

**Today.** A batch of six `read_file` or `search_files` calls runs one after another. Two `web_fetch` calls wait on each other's network round trip.

**Design.** When the serial loop reaches the first call of a run of two or more adjacent read-only calls, it submits that whole run to a small pool. Each call in the run then goes through the existing serial path unchanged: start event, middleware, dedup, stuck detection, audit, and `add_tool_result`, all in order. Its executor is `future.result` instead of a fresh `execute_tool` call. Any other call ends the run, so a read after a write always sees the write.

Module level in `agent/runtime.py`, near `_RESPONSE_FORMAT`:

```python
from concurrent.futures import Future, ThreadPoolExecutor

# Handlers with no shared mutable state in agent/tools.py. web_search is
# excluded: the Brave free tier allows one query per second.
_PARALLEL_SAFE_TOOLS = frozenset({
    "read_file", "read_tool_output", "search_files", "list_files", "web_fetch",
})
_PARALLEL_TOOL_WORKERS = 4


def _parallel_runs(tool_calls_raw, is_truncated) -> dict[int, int]:
    """Map the start index of each run of 2+ adjacent read-only calls to its end (exclusive)."""
    runs: dict[int, int] = {}
    count = len(tool_calls_raw)
    start = 0
    while start < count:
        end = start
        while (
            end < count
            and tool_calls_raw[end][1] in _PARALLEL_SAFE_TOOLS
            and not is_truncated(tool_calls_raw[end][2])
        ):
            end += 1
        if end - start >= 2:
            runs[start] = end
        start = max(end, start + 1)
    return runs
```

Traces:
- `[read, read, write]` gives `{0: 2}`.
- `[write, read, read, read]` gives `{1: 4}`.
- `[read, write, read]` gives `{}`.
- `[read]` gives `{}`.

MCP tools (`server__tool`) aren't in the set, so they stay serial.

In `_execute_tool_batch`, after the import at 2719, wrap the existing loop. Its body is re-indented one level, and the only edit inside it is the executor:

```python
        runs = _parallel_runs(tool_calls_raw, is_truncated_arguments)
        prefetched: dict[int, Future] = {}
        pool: ThreadPoolExecutor | None = None
        try:
            for index, (call_id, tool_name, args) in enumerate(tool_calls_raw):
                run_end = runs.get(index)
                if run_end is not None:
                    if pool is None:
                        pool = ThreadPoolExecutor(
                            max_workers=_PARALLEL_TOOL_WORKERS,
                            thread_name_prefix="tool-read",
                        )
                    for k in range(index, run_end):
                        _k_id, k_name, k_args = tool_calls_raw[k]
                        prefetched[k] = pool.submit(
                            execute_tool, k_name, k_args, conv.project_path, session_key,
                            approval_callback=None,
                            allowed_tools=conv.allowed_tools,
                        )
                tc = next(tc for tc in tool_call_objects if tc.call_id == call_id)
                # ... existing body 2723-2856 unchanged ...
                future = prefetched.pop(index, None)
                result = self._tool_chain.run(
                    tool_name=tool_name,
                    args=args,
                    ctx=ctx,
                    executor=future.result if future is not None else (
                        lambda: execute_tool(
                            tool_name, args, conv.project_path, session_key,
                            approval_callback=per_call_cb,
                            allowed_tools=conv.allowed_tools,
                        )
                    ),
                )
                # ... existing body 2867-2921 unchanged ...
        finally:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
        return False
```

**Why this is equivalent to today.**
- Read tools reach the chain with `per_call_cb = None` (2822), so the prefetch passes the same `approval_callback=None`.
- No refusal or approval branch applies to a read tool, and truncated calls are excluded from runs, so every prefetched future is consumed.
- `future.result()` returns the same `ToolResult` or re-raises the same exception in the loop thread.
- `execute_tool` is the method-local import from 2700, so tests that patch `agent.tools.execute_tool` are honored.
- The tools touched hold no thread-local or contextvar state. `agent/context.py`'s module caches are dict get and set only.

**User-visible difference.** For prefetched calls, the "running" card (`on_tool_call_start`, 2807) may appear just as the result lands. Dedup (`_dedup_read_output`) may discard a prefetched result exactly as it discards a serial one; the I/O was spent either way.

**Tests.**
- New: a batch `[read_file a, read_file b, write_file c, read_file c]` with `execute_tool` patched to a function that sleeps 0.2 s for reads and records `(name, path, thread)`. Assert that the first two reads overlap in time (total under 0.35 s for them), that the `read_file c` call starts after `write_file c` returns, and that `conv.messages` tool results appear in call order.
- New: an exception raised by one prefetched read surfaces from `_execute_tool_batch` exactly as it does serially (a `pytest.raises` with the same type).
- Existing batch tests send single calls (for example `test_spec23_context_working_set.py:88-104`), so they never form a run.

Estimate: +45 lines (a 130-line block re-indented).

---

### B4 — Don't build the repo map for coders (Medium for coder sessions)

**Today.** `_wire_messages_and_count` calls `_repo_map_block_for` and only then blanks the result for coders (4251-4253). Every write bumps `_repo_map_tree_version`, so a coder's next iteration rebuilds the map (`build_repo_map` walks and parses the project), twice before B2, and throws it away.

**Fix.** 4251-4253:

```python
        if getattr(conv, "agent_role", "") == "coder":
            block = ""
        else:
            block = self._repo_map_block_for(session_key, conv)
```

**Tests.** `tests/test_repo_map.py:90-106` (`test_coder_role_wire_omits_the_repo_map`) still passes. `test_two_iterations_call_build_repo_map_once` (109-133) uses `create_conversation`, whose `agent_role` defaults to `""` (runtime.py 1231), so it is unaffected. New test: a coder conversation with `build_repo_map` patched; assert it is not called.

Estimate: ±2 lines.

---

### B5 — Run post-write tests and lint concurrently (Medium for non-coder writers)

**Today.** For roles other than coder (both checks default on), `check()` waits for the test run, up to 60 s, before starting lint, up to 15 s. Once the syntax gate has passed, the two are independent.

**Fix.** `agent/enforcement.py` 1629-1639, with `from concurrent.futures import ThreadPoolExecutor` at the top:

```python
    run_tests = config.test_run and syntax_gate
    run_lint = config.lint_check and syntax_gate
    tests_result = lint_result = None
    if run_tests and run_lint:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="enf-lint") as pool:
            lint_future = pool.submit(
                _check_lint, file_path, project_path, config, syntax_passed, verbose=verbose,
            )
            tests_result = _check_tests(file_path, project_path, config, syntax_passed, verbose=verbose)
            lint_result = lint_future.result()
    else:
        if run_tests:
            tests_result = _check_tests(file_path, project_path, config, syntax_passed, verbose=verbose)
        if run_lint:
            lint_result = _check_lint(file_path, project_path, config, syntax_passed, verbose=verbose)
    if tests_result is not None:
        checks.append(tests_result)
    if lint_result is not None:
        checks.append(lint_result)
```

**Behavior.**
- `checks` keeps the order tests, then lint, so `_format_result` output is unchanged.
- If `_check_tests` raises, the `with` waits for lint and the exception propagates. Today lint would not have run, so only the discarded lint work differs.
- If `_check_lint` raises, `result()` re-raises after tests finish, the same order as today.
- `check()` already runs off the main thread, so no signal-based code can be involved.

**Tests.** No test patches `_check_tests` or `_check_lint` with order assertions (grep: zero). New test: patch both to sleep 0.3 s and return a `CheckResult`; assert `check()` takes under 0.5 s and `[c.tier for c in result.checks]` keeps test before lint.

Estimate: +12 / −8 lines.

---

### B6 — Skip re-parsing providers.yaml on every LLM call (Low)

**Today.** `load_providers()` reads and YAML-parses the file one to three times per provider call (3564, 3473 via 3442, 3341).

**Fix.** Cache the parse inside `load_providers()`, keyed on the file text. The read stays, so an edit is always seen, even one that lands within the same mtime tick. Callers keep calling `load_providers()`, and tests that monkeypatch it bypass the cache by construction. `utils/providers_store.py`, with `import dataclasses` added:

```python
# (path, text, parsed). Content-keyed so an edit is always seen; callers get copies.
_PARSE_CACHE: tuple[str, str, list[ProviderConfig]] | None = None


def load_providers() -> list[ProviderConfig]:
    """Read providers.yaml → list[ProviderConfig]. Empty list if missing.
    Tolerates malformed lines (logs warning, skips)."""
    global _PARSE_CACHE
    path = get_providers_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        _logger.warning("providers_store: failed to read %s: %s", path, e)
        return []
    cached = _PARSE_CACHE
    if cached is not None and cached[0] == path and cached[1] == text:
        parsed = cached[2]
    else:
        parsed = _parse(text)
        _PARSE_CACHE = (path, text, parsed)
    return [dataclasses.replace(p) for p in parsed]
```

`ProviderConfig` fields are all primitives, so shallow copies isolate callers. Rebinding a module global is atomic under the GIL. A race at worst parses twice.

**Behavior change.** Warnings for a partly malformed file are logged once per distinct content, not on every call.

**Tests.**
- Every test resolves config under its own tmp dir (`conftest.py:203-216`), so cache entries never cross tests.
- `tests/test_config_invalidation.py:607` and `tests/test_reasoning_effort.py:307` monkeypatch `load_providers` itself, which bypasses the cache.
- New tests: two loads of the same file call `_parse` once (patch `utils.providers_store._parse` with `wraps=`); rewriting the file through `save_providers` returns the new values; mutating a returned card doesn't affect the next load.

Estimate: +8 lines.

---

### B7 — Stop copying the whole reply on every streamed chunk (Low; do it with A2)

**Today.** `full_content += text` runs on every chunk, and the result is stored in `_last_stream_text`. That second reference defeats CPython's in-place concatenation, so the work grows quadratically with reply length: roughly 50 ms of copying for a 100 KB reply. It's a small cost, but it touches the same loop as A2.

**Fix.** Collect chunks in a list, and write `_last_stream_text` once on exit. Its readers all run after the function returns (see DISCOVERY).

```python
        parts: list[str] = []
        ...
        try:
            for ev in _stream:
                inactive = self._inactive_turn_reason(session_key, turn_token)
                if inactive is not None:
                    parts.clear()
                    _stream.close()
                    self._last_stream_text.pop(session_key, None)
                    raise TurnInactive(inactive)
                if ev.type == "text_delta":
                    text = ev.data.get("content") or ""
                    parts.append(text)
                    if self._on_text_delta:
                        self._dispatch(self._on_text_delta, session_key, text, _turn_token=turn_token)
                # ... tool_call_delta / usage / error branches unchanged ...
                elif ev.type == "done":
                    full_content = "".join(parts)
                    # ... rest of the done branch unchanged ...
            full_content = "".join(parts)
            # ... fallback block unchanged ...
        finally:
            if parts:
                self._last_stream_text[session_key] = "".join(parts)
```

**Behavior.**
- A mid-stream network error leaves the partial text in `_last_stream_text`, exactly as today, so failover reads `streamed=True`.
- On success the chain pops the entry (3178), as it does today.
- On cancel the entry is gone.
- Per-chunk dispatch is unchanged, so `tests/test_agent_runtime.py:1502` (`len(deltas) == 3`) still holds.

**Not done here.** The UI-side copy (`ui/agent_runtime/turn_ui.py:64`, `get(...) + text` per delta) is the same pattern on the main thread and belongs in Phase D. Coalescing deltas on a timer was considered and rejected: with no trailing timer, the last few tokens before a pause would stall on screen.

Estimate: +8 / −3 lines.

---

### B8 — Compiled-path token count misses the per-string memo (Low; measure first)

`_compile_token_count` (4148-4194) encodes the whole compiled wire as one string through `conv._count_text`. That string changes every iteration, so the per-string memo never hits and every build re-encodes the full wire. Summing per-message counts would hit the memo for every unchanged message.

**Caveat.** Per-message sums differ from a whole-string count by a few tokens at the joins. Tests that assert exact compiled counts (`tests/test_spec24_compiled_turn.py`, `tests/test_spec40_wire_aware_guard.py`, `tests/test_context_tiers.py`) would need new baselines. No code sample is given. Measure the encode time on a long compiled session first, and do this only if it exceeds about 10 ms per build after B2.

---

## Phase C — Dead code and stale text

The evidence is reference counts over `agent ui utils models main.py gateway transport`, counted as production refs/test refs (a definition counts as one ref). Delete each group in its own commit, and run the suite after each.

**No references (safe to delete)**

| Symbol | Location | Prod/Test | Note |
|---|---|---|---|
| `_rank`, `_derive_graph` | `agent/repo_map.py:581-598, 600-640` | 2/0 each | `_rank` is the only caller of `_derive_graph`; nothing calls `_rank`. `_derive_graph` is a line-for-line copy of `_build_walk_inputs` 180-211 (diffed). |
| `edge_index`, `node_pos` | `agent/repo_map.py:90, 93, 116-119` | 3/0 each | slots + init, never read |
| `get_auto_open_agents`, `unregister_special_agent` | `agent/special_agents.py:194, 177` | 2/0 each | def + `__all__` (25) |
| `review_staging_dirname` | `agent/config.py:93` | 1/0 | |
| `llm_model_override` | `agent/context_strategy.py:1339, 1344` | 2/0 | stored, never read |
| `MAX_READ_LINES` | `agent/tools.py:300` | 1/0 | |
| `Decision.backoff_s`, `Decision.reason` | `agent/llm/failure_policy.py:27-28` | backoff_s 9/0 (all writes) | written, never read |
| `logger` | `agent/llm/anthropic_provider.py:20` | — | unused |
| `LLMResponse` | `agent/llm/protocol.py:9` | 4/0 | definition + re-exports only; see D4 before deleting |

**Unreachable**

| Code | Location | Why |
|---|---|---|
| `if streamer is None: raise ValueError(...)` | `agent/runtime.py:3753-3757` | `provider.stream` is a bound method |
| `raise last_exc` | `agent/llm/streaming.py:692` | every loop exit returns or raises first |
| defensive raise | `agent/llm/streaming.py:785-790` | same; the comment at 747-749 says so |
| `max_retries` parameter of `stream_with_ssl_retry` | `agent/llm/streaming.py:695+` | no caller passes it |

**Test-only (production never uses them).** Either delete them with their tests, or keep them as documented test seams; the choice is noted per row.

| Symbol | Location | Prod/Test | Recommendation |
|---|---|---|---|
| `_compaction_this_iteration` | `runtime.py:676` (+ NOTE 2243-2249) | 5/8 | delete; drop the 8 test reads |
| `_finish_loop` | `runtime.py:1543-1548` | 1/1 | delete; the `finally` and `start_claimed_loop` inline its body (ARCHITECTURE.md §3.21m says otherwise, see §8) |
| `_messages_for_model` | `runtime.py:4278-4295` | 3/20 | keep as a test seam, or move the 20 calls to `_wire_messages_and_count(...)[0]` |
| `classify_mask_candidates`, `_MASK_*` | `agent/llm/jev_client.py:225-241` | 1/7 | delete with tests, unless a planned feature needs it |
| `AgentRuntimeCallbacks` | `agent/callbacks.py:292` | 1/4 | delete with tests |
| `set_approval_callback`, `_approval_callback` | `agent/tools.py:74-78` | 1/18 | keep as a test seam (18 tests depend on it) |
| `ToolContext.bypass_approval`, `.audit_log`, `.user_id` | `agent/tool_middleware.py:66-72` | runtime writes (2851-2853), tests read | write-only in production; delete with `test_tool_middleware.py:320-345`, or wire a real consumer |
| `_PROVIDER_CALLERS` values | `runtime.py:256-262` | keys used; values only checked for `None` (3686, 5406) | replace with a frozenset of names after checking `tests/test_agent_builder_handler.py:371` (it simulates a new adapter) |
| `agent/__init__.py` re-exports | `agent/__init__.py` | 0 consumers | every `from agent import` targets a submodule |

**Looks dead but isn't.** `_file_search` / `file_search` stays registered on purpose, so that stored tool calls replay (`agent/tools.py:2501-2503`, `_API_SCHEMA_HIDDEN`). Keep it.

**Duplicated logic.** `_call_llm_once` 3562-3582 repeats `_live_provider_card` (3468-3486) line for line. Replace it with `live_card = self._live_provider_card(provider_name)`. One edge changes: an empty `provider_name` currently matches a card whose `default_model` is empty; the helper returns `None`, which is the safer result.

**Stale comments and docstrings**

| Location | Stale claim |
|---|---|
| `runtime.py:90-95` | cites "line ~2125" |
| `runtime.py:612-620` | class docstring says Phase 2b hasn't landed; it has |
| `runtime.py:716-718` | cites ":561" and "the migration call" |
| `runtime.py:1976-1978` | "Phase 2a only registers the state" |
| `context_strategy.py:151-152` | `noqa: ARG002 — Phase 4 wires this`; Phase 4 shipped |
| `tool_middleware.py:55` | describes an "approval middleware" that doesn't exist |
| cancel-flag comments | handled in A1 |

---

## Phase D — Redesign proposals (each needs its own spec)

- **D1. One `SessionState` object per session.** Today about 24 dicts and sets keyed by session sit behind five locks (`_lock`, `_state_lock`, `_tool_history_lock`, `_compaction_lock`, `_repo_map_lock`). A per-session dataclass owning the inbox, turn token, turn state, stream text, tool history, repo-map cache and compile packet, with one lock, would make leaks like A3 impossible (drop the object on close) and make ownership obvious.
- **D2. One cancel primitive.** After A1 there are still two structures (`_cancelled`, `_cancelled_epochs`). A per-turn `threading.Event` could be checked at the loop top, in the stream loop, in `_failover_sleep` (via `event.wait(timeout)` instead of `time.sleep`), between tool calls, and by the approval guard.
- **D3. Wire snapshot.** Build `WireSnapshot(messages, tokens, token_gen, map_version)` once per iteration, have compaction return a new snapshot, and let the meter and breakdown read it. That removes `get_token_breakdown`'s separate count (four of its fields are overwritten right after, 2310-2321).
- **D4. Thin provider layer.**
  - One retry layer: with failover off, the nested `stream_with_ssl_retry` → `urlopen_with_ssl_retry` allows up to 16 connection attempts and about 17.5 s of sleep.
  - One return type: `LLMResponse` already exists unused, and using it would delete `_RESPONSE_FORMAT` and the extractor format switches.
  - Consider one `httpx.Client` per host (httpx is already a dependency of the web tools).
  - Add Anthropic prompt caching (`cache_control` on the system prompt and tools) if Anthropic cards are adopted.
- **D5. Split `runtime.py`** (5444 lines) into turn loop, tool batch, LLM chain and failover, wire and compaction, approvals, and persistence.
- **D6. Move history out of comments.** 34% of `runtime.py` is comments or docstrings, with about 200 `SPEC-`/`BUG`/`Audit-Fix` tags. Keep the constraints; move the history to git and the specs.
- **D7. Return compaction events from `compact()`.** Don't use the shared `strategy.last_result`, which is shared by every session on a runtime and is also read by `ui/agent_runtime/session.py:223`.
- **D8. UI stream accumulation.** `turn_ui.py:64` copies the reply on every delta on the main thread.
- **D9. MCP reconnect storm.** A server that fails `connect` is retried on every iteration (`utils/mcp_client.py:562-567`). Add a negative cache with backoff. Not re-checked in depth.

**Explorer findings not re-checked by hand.** Verify these before acting on them:
- the `search_files` directory walk cost;
- repeated per-write work in `enforcement.py`, including the mypy branch and whether `python3 -m py_compile` (45, 730) could be an in-process `compile()`. That would only be safe when the project's Python matches the app's.
- unused imports across `agent/`;
- the unused `config_path`/`raw` parameters of `_load_providers_from_yaml` (`agent/config.py:180`).

---

## 3. Data Flow

**Cancel, after A1 and A2**

```
User presses Stop
  → AgentRuntimeHandler → rt.cancel(sk)
      under _lock: sk in _active_loops ? _cancelled.add(sk) : (no mark)
                   _cancelled_epochs.add(sk); drop _inbox/_stranded; flush approvals
      dispatch on_error(CANCEL_MESSAGE, active token)
  → worker thread, whichever comes first:
      (a) streaming: next SSE event → _inactive_turn_reason == "cancelled"
            → _stream.close() → raise TurnInactive → _call_llm_once (not caught)
            → _call_llm_chain (no _failure, re-raise) → _run_loop:2280
            → _exit_inactive_turn → _terminate_turn(CANCELLED, reason "user")
      (b) between calls: _call_llm_chain top check → TurnInactive → same path
      (c) loop top: sk in _cancelled → discard → _terminate_turn(CANCELLED, "user")
  → _run_loop finally (under _lock): _active_loops.discard, _cancelled.discard
Next user message → begin_send → _run_loop → _cancelled_epochs.discard at turn start
  → no stale mark → provider is called
```

**One iteration, after B2–B4**

```
_run_loop iteration
  → _compute_compaction_threshold(conv) → (soft, hard); model_max = hard
  → get_tool_definitions_for_api + MCP tools
  → _wire_messages_and_count(sk, conv, model_max, tools)      # build 1
        coder: no repo-map build (B4)
  → wire_gen = conv._token_gen
  → _compact_before_llm(..., pressure=wire_tokens)             # no internal build
  → conv._token_gen changed ? rebuild : reuse                  # build 2 only after compaction
  → over-budget guard → _call_llm (keep-alive socket reused, B1)
  → extract (format by shape, then caller, then prefix; A7)
  → _execute_tool_batch
        runs of 2+ adjacent read-only calls → pool.submit(execute_tool) (B3)
        serial pass in call order: start event → chain(executor=future.result) → dedup → add_tool_result
        write_file/edit_file → EnforcementMiddleware → check(): tests ∥ lint (B5)
```

---

## 4. File Change Summary

| File | Items | Change | Est. lines | Risk |
|---|---|---|---|---|
| `agent/runtime.py` | A1, A2, A3, A4, A6, A7, B2, B3, B4, B7, C | modify | +110 / −60 (plus about 130 re-indented) | Medium (cancel and tool-batch paths) |
| `agent/llm/streaming.py` | B1, C | modify | ±5 | Low (pool already tested) |
| `agent/llm/anthropic_provider.py` | A7, C | modify | +20 / −13 | Low (no live users) |
| `agent/enforcement.py` | A5, B5 | modify | +15 / −9 | Low |
| `utils/providers_store.py` | B6 | modify | +8 | Low |
| `agent/repo_map.py`, `agent/special_agents.py`, `agent/config.py`, `agent/context_strategy.py`, `agent/tools.py`, `agent/llm/failure_policy.py`, `agent/llm/protocol.py`, `agent/llm/jev_client.py`, `agent/callbacks.py`, `agent/tool_middleware.py`, `agent/__init__.py` | C | delete only | about −250 | Low |
| `ui/window.py` | A1 | comment only (947) | ±1 | None |
| `tests/test_cancel_peer.py`, `tests/test_error_surfacing.py`, `tests/test_stop_all.py`, `tests/test_agent_runtime.py`, `tests/test_llm_summarize_strategy.py`, `tests/test_http_keepalive.py`, `tests/conftest.py` | A1, A4, A7, B1 | edit as listed per item | about ±60 | Low |
| `tests/test_spec61_runtime.py` | all | new | about +350 | — |
| `docs/ARCHITECTURE.md` | §8 | modify | about ±25 | None |

**Files NOT changed** (considered):
- `agent/llm/failure_classifier.py`: `is_provider_exception` already returns False for `TurnInactive`, so A2 needs no change here.
- `agent/llm/extractors.py`: A7 fixes format selection at the call sites; the extractors themselves are correct.
- `agent/context_strategy.py` (logic): every write path already invalidates the token generation that B2 relies on. Only Phase C edits touch it.
- `models/conversation.py`: B2 reads `_token_gen` and needs no new API. The SPEC-21 memo already makes recounts cheap, which is why `get_token_estimate` was dropped from the speed list.
- `utils/mcp_client.py`: tool definitions are already cached per server (548-560). The reconnect cost is D9.
- `ui/agent_runtime/session.py`: A4 keeps `force_llm_compact`'s return contract that it reads at 202.
- `ui/agent_runtime/turn_ui.py`: B7 deliberately leaves the UI path alone (D8).

---

## 5. Implementation Order

Run `.venv/bin/python -m pytest -q` (the full suite) at the end of every step. Each step is one commit.

1. **A1.** Make the runtime edits, edit the four test files, and add the three new tests. Verify that `pytest -q tests/test_cancel_peer.py tests/test_stop_all.py tests/test_error_surfacing.py tests/test_spec61_runtime.py -k "cancel or stop"` passes, and that `rg -n '_cancel_requested' --type py .` (excluding `.venv`) returns zero matches.
2. **A2 + B7.** Same function. Add the streaming-cancel test, then confirm `tests/test_agent_runtime.py -k "stream"` still passes.
3. **A3.** Add the pruning test.
4. **A4.** Rewrite `test_strategy_swapped_and_restored` as specified.
5. **A5, A6.** Add their small tests.
6. **A7.** Update the fixture in `tests/test_agent_runtime.py:1737-1749` and add the usage test.
7. **B4**, then **B2.** Add the build-count tests, then run `tests/test_context_tiers.py tests/test_panic_prune.py tests/test_wire_context_pressure.py tests/test_repo_map.py`.
8. **B3.** Add the parallel-order and exception tests, then run the full tool-loop suites (`-k "tool or batch"`).
9. **B5**, then **B6.**
10. **B1.** Do this last, and only after the live measurement in B1 step 1 (record the numbers in the commit message). Make the conftest and keep-alive test edits in the same commit.
11. **Phase C.** One commit per module group, re-checking each reference count with `rg -n -w <symbol>` immediately before deleting.
12. **docs/ARCHITECTURE.md** updates (§8).

---

## 6. Acceptance Criteria

- [ ] After Stop at any point in a turn, the next user message calls the provider (both A1 probe scenarios pass as tests).
- [ ] All cancels report `metadata["reason"] == "user"`; `_cancel_requested` has zero matches in `agent/`, `ui/` and `tests/`.
- [ ] `cancel_peer` on an idle session leaves `_cancelled` empty.
- [ ] Stop during a stream ends the turn at the next SSE event and closes the socket; `_last_stream_text` has no entry for the session.
- [ ] `_turn_state` and `_turn_results` hold at most one entry per session after any number of turns.
- [ ] `self._context_strategy` is the same object before, during and after `force_llm_compact`.
- [ ] `TestConfig.from_dict({}).timeout_seconds is None`, and agent.json's `test_timeout_seconds` governs projects without an override.
- [ ] `_call_for_summary("…", model_id="openrouter/a/b")` passes `model="openrouter/a/b"` to the provider.
- [ ] An Anthropic stream with a tool call yields the tool name, the full arguments, and both usage counts. A streamed response parses with the `"openai"` extractor regardless of model prefix.
- [ ] With keep-alive on by default, two sequential calls to one host use one connection (`test_http_keepalive.py` pattern), and the suite opens no real sockets.
- [ ] An under-budget iteration calls `_wire_messages_and_count` exactly once.
- [ ] Two adjacent `read_file` calls overlap in time; a read after a write sees the write; tool results are appended in call order.
- [ ] Coder sessions never call `build_repo_map`.
- [ ] `check()` with tests and lint both on takes about max(tests, lint), not their sum, and keeps check order.
- [ ] Two `load_providers()` calls on unchanged content parse once; a `save_providers` change is visible on the next call.
- [ ] Full suite passes, with the test edits listed here and no others.

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| Stop pressed before the loop thread starts (claim held by `begin_send`) | Marked; the first loop-top check cancels; `finally` clears |
| Stop pressed after the loop's last check, before `finally` | Mark set; turn ends normally; `finally` clears it; the next turn is unaffected |
| Stop pressed twice | Same as once (set adds are idempotent; one CANCELLED terminal thanks to `_terminate_turn` dedup) |
| Stop while idle | No mark; epoch armed; cleared at the next turn start; approvals for that idle session are denied until then (as today) |
| Message sent right after Stop while the loop is still exiting | `begin_send` queues it to `_inbox`; the loop cancels; `finally` moves it to `_stranded`; the next send drains it (as today) |
| Turn superseded mid-stream (new token) | Stream check sees `"superseded"`; `_exit_inactive_turn` returns with no terminal record (as at the chain check) |
| Stream stalls with no events after Stop | Waits for the socket timeout, then the chain check stops further attempts |
| Prefetched read raises | `future.result()` re-raises in call order, exactly where serial execution would have |
| Batch `[read a, write a, read a]` | No run forms; fully serial |
| Read prefetched, then `_dedup_read_output` replaces it with the unchanged notice | Same output as serial; wasted I/O only |
| Two `web_search` calls in one batch | Serial (excluded, rate limits) |
| `web_fetch` to a slow host inside a run | Others finish meanwhile; the serial pass waits on that future at its turn; `pool.shutdown(wait=True)` bounds thread lifetime to the batch |
| Compaction ran but changed nothing | `_token_gen` unchanged; the first wire is reused |
| Another session's write between build and reuse | Reused wire carries a repo map milliseconds old (advisory content) |
| HTTPS proxy configured | Pool skipped (`_eligible`); urllib path as today |
| Stale pooled socket | `_try_pooled` returns `None`; urllib retry (existing SPEC-21 behavior) |
| `DEVELCAKES_HTTP_KEEPALIVE=0` | Pool off (new opt-out) |
| providers.yaml edited by hand within the same mtime tick | Content differs, so it is re-parsed (why the cache is keyed on text) |
| providers.yaml partly malformed | Parsed once per distinct content; the warning is logged once, not per call |
| Tests raise while lint runs (B5) | Exception propagates after lint finishes; lint result discarded |
| Anthropic `message_delta` without `usage` | No usage event; earlier usage stands |
| Non-streaming Anthropic response (`provider.call`) | No `choices` key, so the format comes from the caller (`"anthropic"`) |

---

## 8. ARCHITECTURE.md Updates Required

- **§3.21m `agent/runtime.py`, "One loop per session" (line 1631).** It says `_run_loop`'s `finally` "calls `_finish_loop`" and that `start_claimed_loop` "releases the claim in `_finish_loop` on failure". Both inline the body, and Phase C deletes `_finish_loop`. Rewrite it. Add: "`finally` also clears `_cancelled`; `cancel()`/`cancel_peer()` mark `_cancelled` only for a session in `_active_loops`."
- **§3.21m, turn state machine (1645).** Add that a new turn drops the session's older `_turn_state`/`_turn_results` entries, and that CANCELLED's only reason is `"user"`.
- **§3.21m, Streaming (1696).** Add the per-event cancel check (`_inactive_turn_reason` → `TurnInactive`). Fix the Anthropic description: the name and id come from `content_block_start`, arguments from `input_json_delta.partial_json`, and usage from `message_start` plus `message_delta`, emitted as OpenAI keys.
- **§3.21m, Thread safety (1700).** Add the B3 read pool: per batch, at most 4 workers, read-only tools only, results consumed in call order on the loop thread.
- **§3.21n `agent/tools.py` (1822).** "`cancel_peer` marks one session in `_cancelled` and does not set `_cancel_requested`" becomes "`cancel_peer` marks a live peer session in `_cancelled` and arms its epoch".
- **§3.21p.5, Hot-path costs (1991).** Keep-alive is now default-on with `DEVELCAKES_HTTP_KEEPALIVE=0` to opt out. Add: one wire build per iteration unless compaction changed messages; coders skip the repo map.
- **§3.11d `utils/providers_store.py` (870+).** `load_providers()` caches the parse by file text and returns copies.
- **§3.21m.1 Tool middleware (1702+).** If Phase C removes `ToolContext.bypass_approval`/`audit_log`/`user_id`, update the field list.
- **§12 Provider Resolution & API Caller (4237).** Response format is chosen by response shape first (streamed responses are always OpenAI-shaped), then by caller key.

---

## 9. Open Questions

- **Q1. OpenRouter card model ids.** `_resolve_agent_model` (`ui/agent_runtime/provider.py:14-39`) uses `default_model` as is when it contains `/`. A card with `default_model: deepseek/deepseek-v4-pro` (no `openrouter/` prefix) would send `deepseek-v4-pro` after `model_id()` strips once. This is unverified: providers.yaml holds API keys and was not read. Check the live cards' `default_model` values.
- **Q2. Keep-alive measurement.** B1 is gated on a live comparison. Who runs it, and against which provider?
- **Q3. Parallel worker count.** Is 4 right? Should `web_fetch` stay in the set if a user fetches several pages from one rate-limited host?
- **Q4. B8 count drift.** Is a few tokens of difference in compiled-path counts acceptable if it removes a full-wire encode per build?
- **Q5. Anthropic `max_tokens`.** Wire it to `ProviderConfig.default_max_tokens` when Anthropic cards are adopted.

---

## Spec Self-Audit (Rule 9)

1. **Do the samples match the current code?** Each sample was written against the lines quoted in DISCOVERY, and the call signatures were checked: `_wire_messages_and_count`, `_compact_before_llm`, `_inactive_turn_reason`, `_resolve_caller_key` (a staticmethod), `execute_tool`, `ToolMiddlewareChain.run`, `_check_tests`/`_check_lint` (as called at 1631/1637) and `load_providers`. B3's loop body is described as "unchanged" rather than reprinted, because it is 130 lines; the implementer re-indents it without edits.
2. **Exceptions.**
   - A2: traced through every `except` between the stream and `_run_loop`.
   - B3: `future.result()` re-raises the same exception as the serial call.
   - B5: both raise orders traced.
   - B6: `OSError` on read is handled as today; `_parse` never raises (it returns `[]` and logs).
3. **Key structures.** `_turn_state`/`_turn_results` keys are `(session_key, turn_token)` (953-954, 1981). `_PARSE_CACHE` is `(path, text, parsed)`. Runs are `{start_index: end_index_exclusive}`, traced on four inputs.
4. **Data flow.** §3 traces cancel and one iteration end to end.
5. **Can an implementer follow it exactly?** Yes for A1–A7 and B1–B7. B8, Phase C's test-only rows, and Phase D need a decision first, and are marked that way.

**Rule 10 status.** This document is the only file written. No source or test file was changed and no tests were run. The test edits above were checked by reading them, not by running them. The implementer owns the Rule 10 checks: the scope list in §4, the full `pytest` output per step in §5, and the `rg` sweeps for `_cancel_requested`, `tool_use_delta` and `_finish_loop`.
