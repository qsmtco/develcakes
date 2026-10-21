# SPEC-45 Adversarial Audit Brief — full uncommitted diff

**Auditor:** load `prompts/adversarialDebugger.md` fresh and work all 11 sections
against the code in scope below. Report findings as BUG #N with severity and
file:line. You are read-only: no edits, no mutations, no git state changes.

## Context you need

SPEC-45 replaces the `crabcard` fenced-block feed-card mechanism with a
`feed_card` tool. Master spec: `docs/specs/SPEC-45-FEED-CARD-TOOL.md` (read §2,
§6, §7). Base commit: `985666f`. The work spans phases 1–10 of the spec's
implementation order (§5).

**Authorship disclosure (important for your adversarial posture):** phases 1–5
were built by Coder. Phases 6–10 were built by the Supervisor, who violated the
loop by building instead of delegating. Do not extend trust on that account —
if anything, probe the Supervisor-built files harder. The audit gates the code,
not the author.

## Code in scope (the entire uncommitted diff)

Tool chain:
- `agent/group_tools.py` — `_feed_card_cb`, `set_feed_card_callback`,
  `feed_card()` (validation: type/title/body/file/count checks, never raises)
- `agent/tools.py` — `set_feed_card_callback` shim + `_TOOLS["feed_card"]`
  registration with the forwarding lambda
- `ui/window.py` — `_post_feed_card` + install in `_wire_group_tool_callbacks`

Deletions:
- `utils/crabcard_parser.py`, `tests/test_crabcard_parser.py` (deleted)
- `ui/agent_runtime/turn_ui.py` — both extraction sites gone; Edit C adds
  `and name != "feed_card"` to the agent_action guard at ~:211
- `ui/views/event_cards.py` — placeholder pre-scan, chip builder, dispatch
  branch deleted; `process_segments` now a 3-liner

Tests:
- `tests/test_agent_runtime.py` — 2 tests deleted, 2 re-pointed onto
  `_reply_key`, 1 moved; class renamed `TestCrabcardTabStamping` →
  `TestReplyKeyAndTurnSlot`
- `tests/test_feed_card_tool.py` — NEW. **Known hazard:** the Supervisor ran
  the same append edit twice (enforcement flagged a duplicate). VERIFY FILE
  INTEGRITY: no duplicated test classes/functions, no doubled blocks, no
  unused imports. §6.1/§6.2 acceptance tests may be present but were authored
  outside the loop — audit them like any other code.
- `tests/test_spec24_facts.py` — `test_no_new_tool_name` literal now includes
  `feed_card` (stays red by design: 6 pre-existing missing names)
- `tests/test_chat_render_handler.py`, `tests/test_feed_body_cap.py`,
  `tests/test_activity_bubble_batching.py` — comment/fixture wording only
  (crabcard → neutral). One fixture string changed: `"with crabcard blocks"`
  → `"original text"` with a matching REPLACE assertion.
- `tests/conftest.py` — SEPARATE task, same tree: broadway branch removed +
  transitive-AST GTK-module skip when no display. In scope for findings.

Prompts/docs:
- `prompts/system/crabcakes-commands.md` §Crabcards → §Feed cards
- `prompts/default_agents/{coder,debugger,supervisor}.yaml` — `- feed_card`
  appended to `tools:`
- `README.md`, `docs/ARCHITECTURE.md` (§3.22e rewritten as the tool),
  `knowledge/features.md`, `knowledge/projects.md` — factual sweep

## Specific probes requested (in addition to your 11 sections)

1. **The forwarding lambda** (`agent/tools.py` `_TOOLS["feed_card"]`): are all
   eight parameters forwarded positionally? A missed forward lands in `**kwargs`
   silently. Also: does the `type` parameter shadow bite anywhere in this chain
   (`type(exc)` vs `exc.__class__.__name__`)?
2. **The duplicate-card guard** (`turn_ui.py` ~:211): does `name != "feed_card"`
   suppress exactly the agent_action card and nothing else? Trace
   `_do_tool_call_result`'s `self._tool_card_ids` pop for the feed_card path.
   What happens on a feed_card call when NO project is open (the guard's
   `_active_project is not None` half)?
3. **`_post_feed_card`** (`ui/window.py`): runs on the tool-loop thread. Claims
   `FeedHandler.add_card` is thread-safe by construction. Verify that claim
   against `feed_handler.py` (id assignment, `_lock`, GTK deferral). Check the
   `session_key.split(":", 1)[-1]` author fallback against keys without a
   colon. Check `fields["additions"]`/`["deletions"]` can never be missing
   (KeyError → the generic catch).
4. **Stale producers**: grep for anything still emitting
   `\x00CRABCARD_REF:` placeholders or calling `add_cards_batch` expecting
   crabcard shapes. `add_cards_batch` itself survives (other producers) —
   confirm that's actually true (who still calls it?).
5. **`process_segments` simplification**: the old code had malformed-marker
   tolerance. Confirm nothing in the render path can still receive a
   placeholder marker and now silently swallows text.
6. **The YAMLs**: `- feed_card` appended after `- talk` in all three — does any
   schema validator (utils/agent_defs.py) reject unknown tool names? Does
   `get_tool_definitions_for_api` filter on the YAML list such that feed_card
   now appears for all three agents?
7. **test integrity**: run collection counts. `tests/test_agent_runtime.py`
   should be 247 items; `-k "reply_key or divergence or unrouted or slot"`
   collects 11. Report actuals.
8. **Doc accuracy**: does `docs/ARCHITECTURE.md` §3.22e (rewritten) match the
   shipped chain (group_tools → window._post_feed_card → FeedHandler.add_card)?
   Any remaining ARCHITECTURE/README reference to `crabcard_parser.py` as live?

## Known pre-existing red (do NOT spend budget re-deriving)

- `tests/test_tools.py::TestToolSchemaSizeBudget` — 3 failures, byte-identical
  at HEAD (stash-verified). The new tool adds ~500 chars to an already-over
  budget; spec §2.2 explicitly leaves TOOL_PAYLOAD_BUDGET out of scope.
- `tests/test_chat_render_handler.py` — 24 failures byte-identical at HEAD
  (GTK/surface-mount class, pre-existing).
- `tests/test_spec24_facts.py::test_no_new_tool_name` — red by design (6
  pre-existing missing names; SPEC-45 §6.4 says report, don't fix).

## Environment mandate

GTK suites under `xvfb-run -a` (bare-run segfaults are environmental). pytest
via `.venv/bin/python -m pytest`. Do not run the full suite.

## Verdict format

Per section: CLEAN or BUG #N (severity, file:line, one-paragraph repro). End
with: SHIP / FIX-FIRST, and a per-phase table (phases 1–5 Coder, 6–10
Supervisor) so the post-mortem can attribute findings.
