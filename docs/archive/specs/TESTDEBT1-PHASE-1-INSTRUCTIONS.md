# TESTDEBT1 Phase 1 — Implementation Instructions (Coder)

**Contract:** `docs/specs/SPEC-TEST-DEBT-1.md` (read IN FULL — §2 is your edit
list, §2.4 the tests, §5 the order, §7 the edge cases). This file records the
**drift deltas** between the spec (authored at base `64c73f4`) and today's
tree (post-AC3 + post-UIRESP2-P4/5). Where this file and the spec conflict on
CURRENT line numbers, THIS file wins; where they conflict on substance, the
spec wins — flag anything that looks substantive.

Work style: steelFramedCodeWriter. Red-first where the spec demands it.
One commit at the end (message in §5 below). No resets, no `git add -A`.

---

## DRIFT DELTAS (verified by Supervisor 2026-09-12, tree at 1b6bf53+)

**D1 — handler line numbers moved (substance unchanged).** Spec line anchors
are stale. Live anchors (verified today):

| Spec anchor | Live line |
|---|---|
| `_get_runtime` :741 | **:815** (wiring edit A goes here) |
| `send_to_special_agent` :760 | **:873** |
| `_on_text_delta` :1005 | **:1201** (insert `_on_turn_start`/`_do_turn_start` BEFORE it) |
| `_do_text_delta_inner` :1106 | **:1302** |
| `_started_turn_sessions` init :129-131 | **:151** |
| discard sites :1158/:1701/:1986 | **:1354 / :1901 / :2189** |
| `_do_tool_call_start` :1189 | **:1378** |
| `_do_response_complete` :1697 | **:1745** |
| `_do_compaction_bubble` :1811 | **:2002** |
| `_do_usage_warning` :1859 | **:2036** |
| `_do_error` (end_streaming :1957) | **:2121** (end_streaming somewhere inside) |
| `_do_error` discard :1984 | **:2189** |
| runtime ctor `on_text_delta` :461 | **:461** (unchanged!) |
| `self._on_text_delta =` :473 | **:473** (unchanged!) |
| BUG#21 dispatch :1281-1287 | **:1327-1335** |

Test anchors (§2.4) — live:
`TestStreaming` :1429 · `test_text_delta_fires_incrementally` :1436 ·
`TestLocalAgentDrawerEmissions` :3870 ·
`test_tool_only_turn_tool_starts_not_suppressed` :4253 ·
`test_tool_only_turn_no_empty_chat_bubble` :4290 ·
`test_started_turn_sessions_clears_ended_flag_on_fresh_tool_start` :4321 ·
`test_callbacks_module_exports_protocols` :5493 · `_QueueGLib` :5561 ·
`TestDeltaCoalescing` :5594 · `test_empty_delta_still_reaches_main_thread` :5699.

Anchor to identifiers, not lines (Rule 5, spec-drift). These numbers are for
navigation only.

**D2 — SUBSTANTIVE: `rt.send_message` signature changed (P4/5 Phase 4 Part B).**
`send_to_special_agent` now calls `rt.send_message(session_key, text,
prepare=_prepare_turn)` (live :960). Spec Edit E's assertion

    rt.send_message.assert_called_once_with("special:coder", "hello")

MUST become:

    rt.send_message.assert_called_once_with(
        "special:coder", "hello", prepare=handler._prepare_turn
    )

(verify the closure's real name on the live tree — it should be
`_prepare_turn`; if named differently, use the actual name and note it in
your report). Also note `send_to_special_agent` no longer loads the
conversation inline — Part B moved that into the prepare closure that runs
on the loop thread. The mock rt absorbs `load_conversation`/`create_conversation`
the same way as before, so the spec's Edit E trace holds otherwise.

**D3 — empty-delta dispatch comment block moved.** The spec's Edit D
(runtime) replaces the BUG #21 comment + dispatch at the live lines
:1327-1335 (quoted in full in the spec; text unchanged, location moved).

**D4 — `test_agent_runtime.py` grew (~5737 → ~5800+ lines) from AC3 Part A
(TestDeltaCoalescing) and the 2c hermeticity fixture.** All spec-referenced
identifiers verified present (list above). No action — just don't panic at
offsets.

---

## ENVIRONMENT (standing)

- GTK suites: `PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3 -m pytest ...`
- `test_agent_runtime.py` is run PER-CLASS ONLY (pre-existing OOM at
  TestEndStreaming* classes — do not attempt full-file single-process runs;
  if the enforcement hook trips one, note it and move on)
- pyflakes: `/tmp/pf-venv3/bin/pyflakes` (gate: 0 undefined on touched files)
- Red-first evidence REQUIRED (spec §5 step 5): the 3 render-guard tests +
  the 2 new-method tests must be shown failing BEFORE the source edits land.
  The AC3 2c hermeticity fixture (module-scoped flush_audit_log mock) exists
  in the file — leave it intact.
- Hermeticity: no writes to real ~/.config/crabcakes or real .crabcakes/feed*.

## COMMIT

One commit at the end, message:
`fix(runtime): dedicated on_turn_start callback replaces empty-delta turn-start signal (BUG-21)`

Do NOT commit `docs/specs/SPEC-DNS-RETRY-1.md` if it's still untracked (it
may already be committed by supervisor — check git status first; leave docs/
commit decisions to supervisor).

## REPORT

COMPLETENESS checklist (every spec edit A→L + test edits A→H, with evidence),
pasted red-then-green outputs for the mandated tests, per-class suite results
(TestLocalAgentDrawerEmissions FULL class, TestStreaming, TestDeltaCoalescing,
TestRuntimeStructure), pyflakes output, grep sweeps per spec §6 AC
(`_started_turn_sessions` zero in *.py; `_dispatch(self._on_text_delta, session_key, ""` zero; `on_turn_start=self._on_turn_start` present in `_get_runtime`),
related-issues-flagged list (flag, don't fix). Then STOP.
