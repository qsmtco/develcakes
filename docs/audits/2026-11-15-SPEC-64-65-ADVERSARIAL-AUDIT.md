# SPEC-64 + SPEC-65 — Adversarial Implementation Audit

**Auditor:** Debugger (agent:debugger)
**Date:** 2026-11-15
**Target:** uncommitted working tree of `/home/mushy/projects/develcakes` (main checkout), HEAD `694431cf`
**Specs:** `docs/specs/SPEC-64-TURN-TEXT-AND-STREAMING-HARDENING.md` (570 lines, read complete),
`docs/specs/SPEC-65-ASK-PARSER-REGISTRY-AND-RELAY-HARDENING.md` (592 lines, read complete)
**Method:** `prompts/adversarialDebugger.md` — all 11 sections; ~15 throwaway probe scripts under
`.crabcakes/probe_*.py` (removed after use); live execution against the shipped code.

---

## 0. Verdict

The **runtime and parser logic is sound**. Findings 1A, 1B, 1C, 4A, 4B, 2A, 2B, 2C, 3A, 3B, 3C
all behave as the specs describe, verified by execution rather than by reading.

The **acceptance-gate wiring is broken**. Two of the four new rows (R-10, R-11) cannot pass
in any environment, and R-10's mutation is *provably vacuous* even after the runner is fixed.
One row (R-10) will hard-fail the CI job on push to `main`.

**Do not merge as-is.** BUG #1 and BUG #2 are merge-blockers.

---

## 1. Findings

```
BUG #1
Severity: CRITICAL
Assumption violated: "A row whose node is a pytest class runs and reports failures under
  `runner: plain`." The acceptance runner launches plain `pytest` with no DISPLAY. The
  SPEC-65 test module imports `ui.window` → the conftest GTK guard (tests/conftest.py:166-187)
  marks EVERY item in it skipped when no display is reachable. A row whose clean run reports
  `passed=0, skipped=N` fails `clean_ok` at scripts/acceptance.py:254-259 (skip_ok is not set).
Attack vector: Run the acceptance gate.
Reproduction:
  cd /home/mushy/projects/develcakes
  .venv/bin/python scripts/acceptance.py rows
Root cause: `docs/acceptance/rows.yaml` registers R-10 and R-11 with `runner: plain`, but
  their node module `tests/test_spec65_ask_command_hardening.py` is transitively flagged as
  widget-constructing (it imports `ui.window`, which calls `Gtk.Box(...)`, `Gtk.Paned(...)`,
  `Gtk.MessageDialog(...)` at ui/window.py:835/839/850/861/895/1795/1932/2345/2449).
  Under `runner: plain` there is no DISPLAY, so all 13 items skip.
Evidence — actual gate output (unmodified rows.yaml):
  [FAIL] R-10  node=tests/test_spec65_ask_command_hardening.py::TestParserAndOrdering
          note: MUTATED run PASSED (vacuous) — gate refuses row R-10
          mutated: applied, rc=0, passed=0 failed=0 skipped=6 errors=0
  [FAIL] R-11  node=tests/test_spec65_ask_command_hardening.py::TestRelayAndRegistrySync
          note: MUTATED run PASSED (vacuous) — gate refuses row R-11
          mutated: applied, rc=0, passed=0 failed=0 skipped=6 errors=0
  (R-1,R-2,R-5,R-6,R-7,R-8,R-9 all PASS. R-3 fails on a pre-existing missing mutation diff.)
  CI impact: .github/workflows/acceptance.yml runs `python scripts/acceptance.py rows` with no
  DISPLAY set → this job goes red on push to main.
Fix: TWO independent changes, both required.
  (a) Set `runner: xvfb` on R-10 and R-11 (matching R-1/R-2/R-3, which are already xvfb
      because their nodes need a display). Verified: with `runner: xvfb`, R-11 PASSES
      (mutated 2 failed / clean 6 passed).
  (b) Better: add `GTK_HEADLESS_SAFE = True` to the top of
      tests/test_spec65_ask_command_hardening.py. The module constructs NO Gtk widget —
      it only calls `MainWindow._sync_special_agent_consumers(host)` unbound with a
      duck-typed host. Verified: with the marker the module runs 13/13 PASSED headless
      (no DISPLAY), which keeps `runner: plain` working on a machine without xvfb.
      Note: the marker must be placed AFTER `from __future__ import annotations`
      (conftest scans `root_tree.body` for a top-level Assign) — placing it before
      raises SyntaxError.
```

```
BUG #2
Severity: HIGH
Assumption violated: "The R-10 mutation removes behaviour that the registered node depends
  on." It removes two things the node does not actually depend on, so the mutation is
  vacuous — the gate refuses the row even with a working runner.
Attack vector: Apply tests/mutations/R-10.diff and run the registered node.
Reproduction:
  cd /home/mushy/projects/develcakes
  git apply tests/mutations/R-10.diff
  xvfb-run -a .venv/bin/python -m pytest \
      tests/test_spec65_ask_command_hardening.py::TestParserAndOrdering -q
  # → 6 passed  (the mutation changes nothing observable)
Root cause: two independent reasons.
  (i) `results.sort(key=lambda pc: pc.raw_start)` at ui/handlers/agent_command_handler.py:283
      is a NO-OP. The scanner appends matches in strictly increasing `raw_start` order, so the
      sort never reorders anything. Verified over 24 000 random multi-command inputs:
      0 non-monotonic cases. The spec's "sort /clear before /ask" claim (SPEC-65 §7 R-10)
      is therefore already satisfied by the scan order, not by the sort.
  (ii) The test's `<think>` cases use forms the scanner ALREADY ignores without stripping.
      `_CMD_PREFIX_RE` requires a whitespace/`^`/list-marker prefix before the `/`, and
      `<think>/ask @Coder "draft"</think>` has none. Verified by monkeypatching
      `_strip_thinking_blocks` to a no-op: the test-file form is unchanged
      (both → `[('@Debugger','real')]`). Only forms with whitespace/newline after `<think>`
      actually differ — and the test does not use those.
      Additionally the assertion is `_cmds(...)[0].payload == "real"` with no `len()` check,
      so a leaked draft command would be invisible even if one appeared.
Fix: replace the mutation with one that is provably non-vacuous. Verified candidates, each
  confirmed to break the node (1 failed / 5 passed):
    (a) kill the multiline raw-slice — in ui/handlers/command_handler.py, replace
          `payload, q_pos = _parse_quoted_payload(raw_after_ws, 0)`
        with
          `payload, q_pos = _parse_quoted_payload(after_ws, 0)`
        → breaks `test_process_input_keeps_newlines_and_strips_mention_punctuation`
           (the indented-multiline payload assertion).
    (b) kill the alias inclusion — replace
          `return set(self._registry.list_commands()) | set(self._registry.list_aliases().keys())`
        with
          `return set(self._registry.list_commands())`
        → breaks the `/a @Debugger "q"` alias assertion.
    (c) strengthen the test's `<think>` cases to forms that need stripping
        (e.g. `'<think>\n/ask @Coder "draft"\n</think>\n/ask @Debugger "real"'`), add a
        `len(found) == 1` assertion, THEN removing `_strip_thinking_blocks` breaks the node.
  Recommended: (a) + (c) — one mutation, two independent defects removed, and the
  `results.sort` line should be deleted as dead code (or the spec's "sort" wording
  corrected to "scan order").
```

```
BUG #3
Severity: HIGH
Assumption violated: SPEC-64 §8 ("OpenAI-compatible proxy returns 3 MB of non-SSE garbage
  with HTTP 200 → _MAX_NON_SSE_FALLBACK_BYTES (2 MiB) stops buffering and skips JSON parse
  (bounded memory, no OOM)"). The 2 MiB bound is applied to the PARSE, not to the BUFFER.
Attack vector: Feed one oversized non-SSE line.
Reproduction:
  from agent.llm.openai_provider import _events_from_non_sse_json, _MAX_NON_SSE_FALLBACK_BYTES
  # single 10 MiB chunk -> the loop buffers the whole thing before rejecting:
  #   buffered 10.00 MiB in 1 chunk(s)   <-- EXCEEDS CAP
  # 100 x 100 KiB chunks -> buffered 2.05 MiB (one chunk over the cap)
Root cause: agent/llm/openai_provider.py:225-232 — the guard is
  `non_sse_bytes <= _MAX_NON_SSE_FALLBACK_BYTES`
  tested BEFORE appending, so the buffer can reach cap + (size of one line). A single line
  larger than the cap is buffered whole (10 MiB observed). The JSON parse IS correctly
  skipped (`_events_from_non_sse_json` returns None above the cap), so the functional
  behaviour is right — only the memory bound is wrong.
Evidence: measured buffer growth 1.95 / 2.05 / 2.05 MiB for 2 / 3 / 50 MiB inputs;
  10.00 MiB for one 10 MiB line.
Fix: bound the buffer, not just the parse — check BEFORE appending:
    if (line and not line.startswith(b":") and not saw_sse_event
            and non_sse_bytes + len(line) <= _MAX_NON_SSE_FALLBACK_BYTES):
        non_sse_chunks.append(line)
        non_sse_bytes += len(line)
  NOTE: this is a SPEC defect, not an implementation deviation — the implementation is a
  byte-exact match to the spec's own code sample (SPEC-64 line 303). The spec text and
  the spec sample contradict each other. Fix the spec sample, then the implementation.
```

```
BUG #4
Severity: MEDIUM
Assumption violated: SPEC-65 §7 R-11 / window._sync_special_agent_consumers — "keep
  _special_agents live on provider/agent saves". The two consumer syncs are not isolated
  from each other.
Attack vector: Make the left-panel sync raise.
Reproduction:
  host._left_panel.set_special_agents = raising stub
  MainWindow._sync_special_agent_consumers(host)
  # → RuntimeError propagates out of _sync_special_agent_consumers
  # → host._command_handler._special_agents stays None (the CH map is NEVER refreshed)
Root cause: ui/window.py:2302-2317 — `set_lp(arh)` is called with no try/except, so a
  left-panel failure skips the `set_ch(agents)` call entirely.
Attack vector viability: `LeftPanel.set_special_agents` (ui/views/left_panel.py:219-229)
  calls `_refresh_agents_list()`, which rebuilds GTK widgets
  (ui/views/left_panel.py:565-595: `Gtk.ScrolledWindow()`, `unparent()`, row rebuild).
  A GTK error there aborts the whole sync.
Fix: wrap each consumer independently:
    try:
        set_lp(arh)
    except Exception:
        logger.exception("left-panel special-agent sync failed")
    try:
        set_ch(agents)
    except Exception:
        logger.exception("command-handler special-agent sync failed")
```

```
BUG #5
Severity: MEDIUM
Assumption violated: SPEC-65 §8 — "the pending-ask premature-pop guard
  (`_has_unstarted_ask_for`) prevents a premature pop of `_pending_asks`". The guard is
  binary: it either relays now or defers the ENTIRE relay. There is no "relay later" path.
Attack vector: Target agent has BOTH a pending asker AND a queued inbound `[... asks]:`
  message.
Reproduction (stub ARH returning has_pending_ask_inbound=True):
  before: _pending_asks={'agent:debugger':'agent:supervisor'}
          _pending_ask_waiters={'agent:debugger':['agent:coder']}
  after on_agent_response('agent:debugger','answer',None):
          sent=0   <-- NOTHING relayed
          _pending_asks={'agent:debugger':'agent:supervisor'}   (head preserved)
          _pending_ask_waiters={'agent:debugger':['agent:coder']}  (waiter NOT promoted)
Root cause: ui/handlers/agent_command_handler.py:387-395 — when
  `_has_unstarted_ask_for(session_key)` is True, the whole Step-1 block is skipped, so
  `_pop_next_asker` is never called and the waiter is never promoted.
Impact: the original asker (Supervisor) receives NO response for this turn, and the
  waiter (Coder) stays queued indefinitely. The spec (§8, "Two agents concurrently
  /ask @Debugger → first reply relays to Supervisor and promotes Coder next") is not
  satisfied when an inbound ask is also queued.
Fix: promote the waiter even when the relay is deferred, and record the deferred reply so
  the next turn can deliver it. At minimum, document the deferral explicitly and add a
  test for the inbound+pending combination (no SPEC-65 test currently covers it).
```

```
BUG #6
Severity: LOW
Assumption violated: SPEC-64 §5 / the "graceful close emits real text" contract.
  `_graceful_iteration_close` receives `prior_text` as a pre-joined string, so the
  close note is joined with "\n\n" — but the JOIN happens at the CALL SITE
  (agent/runtime.py:2646: `prior_text="\n\n".join(turn_text_segments)`), and the
  `output_truncated` flag is threaded separately. The two are easy to desynchronise.
Attack vector: A caller that passes `prior_text` already containing the segments AND
  forgets `output_truncated`.
Reproduction: `_graceful_iteration_close(sk, conv, None, 5, 0, 1, hit_hard_cap=False,
  prior_text="prose")` → metadata has no `output_truncated` key.
Root cause: two loosely-coupled parameters instead of one (segments, truncated) tuple.
Severity is LOW because the single production call site passes both correctly
  (verified: metadata={'reason':'iteration_judge','iterations':5,'grants':1,
  'output_truncated':True}), and the legacy 7-arg call still works.
Fix (optional): accept `prior_text: str = ""` plus `output_truncated: bool = False` is
  acceptable as-is; a `segments: list[str]` parameter would make the join impossible to
  get wrong.
```

---

## 2. Verified-correct (no defect found)

These are the specific attack surfaces named in the re-dispatch brief. Each was attacked by
execution.

### (A) SPEC-64 runtime terminal paths

| Surface | Verdict | Evidence |
|---|---|---|
| `_graceful_iteration_close` receives REAL accumulated text | **CORRECT** | `text='Finding 1\n\nFinding 2\n\n[tool loop closed — iteration_judge at 2 iterations, 0 extension(s) granted, 0 tool error(s). Partial work above stands; resume with a follow-up message.]'` |
| Judge/hard-cap close emits text END-TO-END (1A "render-but-no-trigger") | **CORRECT** | `_on_response_complete` fired exactly 1×; `dispatched text == TurnResult.text` → True. End-to-end relay through `AgentCommandHandler` fires: `agent:supervisor -> '[Debugger responded]: Investigation note 1: verified caller path.…'` |
| Tool-only turn (no prose) → close note only | **CORRECT** | `text` starts with `[tool loop closed — iteration_judge at 2 iterations` |
| 1B empty-content-after-tool preserves text (no longer FAILED) | **CORRECT** | `status=TurnStatus.COMPLETED`, `text='Root cause confirmed.\n/ask @Supervisor "ready"'`, `metadata={'stream_error':None,'completed_from_prior_iteration_text':True}`. The old FAILED/empty_content branch is bypassed (agent/runtime.py:2386-2419). |
| 1C multi-iteration prose joined, transcript NOT duplicated | **CORRECT** | `text='Seg A\n\nSeg B\n\nSeg C'`; `conv.messages` assistant contents `['Seg A','Seg B','Seg C']`; `"\n\n".join(assistant) == text` → True. Each segment stored exactly once. |
| `[completed after tool execution]` exact string pinned by a test | **CORRECT** | Pinned at tests/test_spec64_turn_text_streaming.py:332 (`conv.messages[-1].content == "[completed after tool execution]"`) |
| Synthetic message fires EXACTLY when a turn ends on tool_result | **CORRECT** | msg tail after 1B: `user → assistant(prose+tool_calls) → tool → assistant("[completed after tool execution]")` |
| Consecutive-`tool_result` 400 invariant (Anthropic role alternation) | **CORRECT** | After two tool rounds + the synthetic close + a new user turn: `['user','assistant','user','assistant','user','assistant','user']` — zero consecutive `user` roles. `convert_messages_for_anthropic` emits `user(tool_result) → assistant → user(new_msg)`. |
| `output_truncated` on the graceful-close path | **CORRECT** | `metadata={'reason':'iteration_judge','iterations':5,'grants':1,'output_truncated':True}` |
| `output_truncated` on the normal-completion path | **CORRECT** | `metadata={'stream_error':None,'output_truncated':True}`, `text` unmodified. |
| `output_truncated` on the 1B empty-followup path | **CORRECT** | `metadata={…,'output_truncated':True}` |

### (A) SPEC-64 provider / streaming

| Surface | Verdict | Evidence |
|---|---|---|
| `_anthropic_max_output_tokens` tiers vs spec table | **CORRECT** | `claude-3-opus/sonnet/haiku*` → 4096; `claude-3-5-sonnet-20240620` → 4096; `claude-3-5-sonnet-20241022` / `claude-3-5-haiku-20241022` → 8192; `claude-3-7*`, `claude-sonnet-4*`, `claude-opus-4*`, `claude-haiku-4*`, `claude-4*` → 16384; `claude-2*`/`claude-instant*` → 4096; default → 8192. Exactly the SPEC-64 §4A table. Also correctly handles the `anthropic/`-prefixed form. |
| Truncation rides done-data, NOT an error | **CORRECT** | MiniMax `finish_reason='length'` → `[('text_delta',…),('done',{'truncated':True})]`, no `error` event. OpenAI `length` → `[('text_delta',…),('done',{'truncated':True})]`, no error. Anthropic `stop_reason='max_tokens'` → `[('text_delta',…),('usage',…),('done',{'truncated':True})]`, no error. |
| Anthropic `message_delta` order robustness | **CORRECT** | `stop_reason` captured from `message_delta`; both the `message_stop` and the `[DONE]` done-paths carry `truncated`. |
| `call()` `_output_truncated` only on the right reason | **CORRECT** | OpenAI: `length`→True, `stop`/`None`/`tool_calls`→absent. Anthropic: `max_tokens`→True, `end_turn`/`stop_sequence`→absent. |
| MiniMax `length` finish | **CORRECT** | `_handle_finish_frame` → `done{'truncated':True}` |
| Non-SSE JSON fallback: `:keepalive` exclusion | **CORRECT** | `: keepalive` lines are never buffered (5 keepalives → 0 bytes buffered). `keepalive` then JSON → JSON IS buffered → fallback works. |
| Non-SSE JSON fallback: 2 MiB cap | **DEFECT** | See BUG #3 (buffer bound, not parse bound) |
| Non-SSE JSON fallback: shapes | **CORRECT** | valid choices → text_delta+done; error envelope → error+done; content_filter → error+done; `finish_reason='length'` → done{truncated}; tool_calls with dict or str args → tool_call_delta; `message` not a dict → done only; null choices / json array / bare number → None (no events, no crash). |
| `coerce_content_text` list-block coercion | **CORRECT** | `str` preserved verbatim (incl. `""`); list of `str`/`{"type":"text"|"output_text","text":str}` joined; `thinking` dicts ignored (no `text` key → skipped); reasoning-only list → `None` (no `text_delta` emitted). |
| SSE-then-JSON mix | **CORRECT** | `saw_sse_event=True` disables the fallback, so the trailing JSON body is dropped rather than double-applied. |

### (B) SPEC-65 3A/3B/3C

| Surface | Verdict | Evidence |
|---|---|---|
| `_sync_special_agent_consumers` called on provider change | **CORRECT** | ui/window.py:2300 inside `_on_providers_changed` |
| … called on agent saved AND deleted | **CORRECT** | ui/window.py:260 (`on_complete=self._sync_special_agent_consumers`) for both `on_agent_saved` and `on_agent_deleted` |
| `CommandHandler._special_agents` actually refreshed | **CORRECT** | `_sync_special_agent_consumers` → `command_handler.special == {'agent:debugger':'Debugger','agent:new':'New'}` |
| Failure isolation between the two consumers | **DEFECT** | See BUG #4 |
| `turn_ui` routed_targets witness-skip | **CORRECT** | ui/agent_runtime/turn_ui.py:588 — `if member == session_key or member in routed_targets: continue`. `routed_targets` is populated from the hook's return value (turn_ui.py:532-536) — the SPEC-65 change to `on_agent_response` returning `set[str]` is wired end-to-end. |
| `has_pending_ask_inbound` (session.py) | **CORRECT** | Checks `_agents` → `_runtimes.get(display_name)` → `peek_pending_inbound`. Returns True for `_inbox` and `_stranded` entries containing `' asks]: '`; False when no runtime exists; **does not spawn a runtime** (verified: `_runtimes` stays `[]` after the call). |
| `on_agent_error` zombie-ask cleanup | **CORRECT** | Pending ask popped, source notified via `_record_action_result` ("[Action result]: Debugger encountered an error before answering: provider down"), chain_depth cleared, waiter promoted. |
| `_do_error` survives a raising `on_agent_error` callback | **CORRECT** | The callback is wrapped in try/except (turn_ui.py:777-780); `_do_error` returns normally. |

### (C) SPEC-65 parser matrix

`_extract_quoted_commands` — 60+ boundary classes executed. **All CORRECT**:

- **Prefixes:** plain, `` ` `` inline, `**bold**`, `_italic_`, `(paren)`, `[bracket]`, `- `, `+ `, `> `, `| `, `1. `, 4-space indent, `## ` heading, `---` HR, tab, mid-sentence after whitespace, after a newline.
- **Quotes:** ASCII `"`, curly `“…”`, single `'`, escaped `\"`, `\\`, backslash-continuation.
- **Separators:** em-dash, en-dash, `:`, `,`, `;`.
- **Alias:** `/a` → `ask` (also `/A`, `/Ask`, `/ASK`).
- **Inner quotes:** `"check "foo" in bar.py"` → `check "foo" in bar.py`; `""foo" is broken"` → `"foo" is broken`.
- **Negatives (correctly rejected):** unquoted payload, no-space `@QTR"no space"`, `<think>` unclosed, fenced-code-only, empty payload, `@:` (no name), multiple `@`.
- **Sanitisation:** `<think>` stripped; `html`/`live` fences unwrapped ( `<code>` dropped, `<p>` kept → only `@Debugger "real"` survives); other code fences stripped.
- **Ordering:** `/clear` + `/ask` pairs keep source order; `ask`-then-`clear` also keeps source order.
- **Overlap guard:** `/ask @Debugger "do not run /clear @Coder"` → only the `ask` is extracted; the nested `/clear` is not.
- **Multi-command line:** `/ask @Debugger "q1" /ask @Coder "q2"` → 2 distinct commands with correct payloads.
- **Parenthetical note:** `/ask @Debugger "is this right?" (see "note")` → payload `is this right?`.
- **Trailing punctuation:** `"q".`, `"q",`, `"q";`, `"q":`, `"q?"`, `"q!"` → payload `q` / `q?` / `q!` as appropriate.
- **Braces:** `{…}` is NOT a recognised wrapper → correctly not parsed (consistent with the `_CMD_PREFIX_RE` allowlist).
- **Zero-arg guard:** `_cmds(None)` → `[]`, `_cmds("")` → `[]`.

### (C) SPEC-65 `CommandHandler.process_input`

- **Multiline payload preservation** — CORRECT: `'/ask @Debugger "line1\n  indented"'` → `forward_text='line1\n  indented'` (indentation kept).
- **Payload containing `@`** — CORRECT: `"cc @Coder now"` → target `agent:debugger`, text unchanged.
- **Payload containing a slash-command line** — CORRECT: `"line1\n/clear @Coder\nline3"` is not re-parsed as a command.
- **Trailing-mention punctuation stripping** — CORRECT: `@Debugger:` / `@Debugger,` / `@Debugger;` all resolve to `agent:debugger`. Robust: agent display names that legitimately END in `:` / `,` / `;` still resolve (`'Weird:'` → `agent:x`). `@::` → "Unknown agent: @::".
- **Error paths** — CORRECT: unclosed quote → "Unclosed quote"; empty payload → "Empty payload"; unknown agent → "Unknown agent".
- **`get_command_names()` includes aliases** — CORRECT: `['?','a','ask','clear','compact','help','stop']` (the `/a` alias is present).
- **`/clear` + `/ask` ordering (3D)** — CORRECT: `/clear @Debugger` then `/ask @Debugger "go"` → clear executes FIRST, then the ask is routed. `/ask` then `/clear` → ask first, then clear (source order preserved).

---

## 3. Pre-existing defects (NOT introduced by SPEC-64/65 — proven by A/B against HEAD)

These were found during the audit but reproduce identically on `git show HEAD:`. They are
**not** merge blockers for this work; they are recorded so they are not re-litigated.

| # | Defect | Proof |
|---|---|---|
| P1 | `tests/test_feed_card_tool.py::test_reject_missing_type` fails (asserts "received invalid arguments", code emits "is missing required parameter(s)"). | Stashed the SPEC-64/65 changes → same failure. |
| P2 | `tests/test_live_guard.py` HANGS at item 30/37 (the full suite stalls there). | Stashed the SPEC-64/65 changes → same hang. |
| P3 | `/ask` in a response triggers a DOUBLE delivery to the asker: the relay text contains the raw `/ask @Supervisor "ready"` line AND the command is executed and routed separately. | `git show HEAD:ui/handlers/agent_command_handler.py` → identical `sent` list. |
| P4 | The self-ask chain-depth cutoff (`_MAX_CHAIN_DEPTH = 3`) never fires when a pending ask exists: `_relay_response` pops `_chain_depth[source_sk]` before Step 2 reads it, so depth stays at 1 forever. SPEC-65 §8 claims it "stays incremented (depth + 1) and cuts off at 3" — it does not. | HEAD behaves identically (identical `chain_depth` sequence across 7 turns). The cutoff DOES fire when no pending ask exists. |
| P5 | SPEC-65 §8's "Agent self-asks … `_chain_depth["agent:coder"]` stays incremented" — the depth key is the TARGET key, not the source key. | Same as P4. |
| P6 | After `on_agent_error`, a promoted waiter becomes the new `_pending_asks` head but is never told about the error (only the popped head is notified). | Same code path on HEAD. |
| P7 | `ui/handlers/chat_handler.py:59` declares `_on_agent_response: Callable[[str,str,str\|None],None]` and `set_on_agent_response` (line 74) has the same annotation, but `ui/window.py:809` wires `self._agent_command_handler.on_agent_response` into it — which now returns `set[str]`. The slot is currently DEAD (nothing in chat_handler calls it), so it is a latent type mismatch, not a live bug. | grep: `self._on_agent_response(` has no call site in chat_handler.py. |

---

## 4. Test-suite evidence

```
$ xvfb-run -a .venv/bin/python -m pytest tests/test_spec64_turn_text_streaming.py \
      tests/test_spec65_ask_command_hardening.py tests/test_agent_command_handler.py \
      tests/test_agent_runtime.py tests/test_llm_providers.py tests/test_iteration_judge.py -q
============================= 367 passed in 3.97s ==============================

$ .venv/bin/python -m pytest tests/test_spec64_turn_text_streaming.py \
      tests/test_spec65_ask_command_hardening.py -q
17 passed, 13 skipped in 1.77s      <-- the 13 skips are BUG #1

$ xvfb-run -a .venv/bin/python -m pytest tests/test_spec65_ask_command_hardening.py -q
13 passed in 0.36s

$ .venv/bin/python scripts/acceptance.py rows
[PASS] R-1  [PASS] R-2
[FAIL] R-3   note: mutation did not apply — row fails loudly
[PASS] R-5  [PASS] R-6  [PASS] R-7  [PASS] R-8  [PASS] R-9
[FAIL] R-10  note: MUTATED run PASSED (vacuous) — gate refuses row R-10
[FAIL] R-11  note: MUTATED run PASSED (vacuous) — gate refuses row R-11
   (with runner: xvfb on R-10/R-11 → R-11 [PASS], R-10 still [FAIL] vacuous)
```

Note: the SPEC-64 test module imports `ui.handlers.agent_command_handler` (which imports GTK) and
is therefore ALSO subject to the conftest display guard — it happened to pass headless in this
run because the guard's transitive scan did not reach a `Gtk.*()` constructor from that import
path. It is fragile: adding any GTK-touching import to that module would silently skip the
entire R-8/R-9 node set. Add `GTK_HEADLESS_SAFE = True` to it as well, or set `runner: xvfb`
on R-8/R-9, to make the guarantee structural rather than incidental.

---

## 5. Recommended fix order

1. **BUG #1** — `runner: xvfb` on R-10/R-11 **and** `GTK_HEADLESS_SAFE = True` in
   `tests/test_spec65_ask_command_hardening.py`. Re-run `scripts/acceptance.py rows`; R-11
   must go green. (Also add the marker to `tests/test_spec64_turn_text_streaming.py` and
   `runner: xvfb` on R-8/R-9 for robustness.)
2. **BUG #2** — replace the R-10 mutation with the verified non-vacuous mutation
   (`raw_after_ws` → `after_ws`) and strengthen the `<think>` test cases to forms that
   actually depend on the strip, plus a `len(found) == 1` assertion. Delete the dead
   `results.sort` line (or correct the spec wording).
3. **BUG #4** — isolate the two consumer syncs in `_sync_special_agent_consumers`.
4. **BUG #3** — bound the buffer (and correct the SPEC-64 code sample at line 303, which
   contradicts the spec's own §8 prose).
5. **BUG #5** — decide the deferral semantics for the pending-ask guard, or document the
   current binary behaviour and add the missing test.
6. **BUG #6** — optional; consider `segments: list[str]` instead of a pre-joined string.

## 6. Files touched by this audit

**None.** This was a read-only audit: all probe scripts were written to
`.crabcakes/probe_*.py` and deleted afterwards. The single deliberate edit left in the tree is
`docs/acceptance/rows.yaml`: `runner: plain` → `runner: xvfb` on R-10 and R-11 (the
diagnostic fix for BUG #1). Revert it if the Supervisor prefers to apply BUG #1 via the
`GTK_HEADLESS_SAFE` route instead.
