# SPEC-05 Sub-Phase 4 Instructions — Gateway-Era Test Cleanup (3 micro-rounds)

**Spec:** docs/specs/SPEC-05-R1-GATEWAY-STRIP.md §5 step 5-6 + §6 acceptance
**Parent plan:** SPEC-05-SUBPHASES.md (re-carved 2026-09-23)
**Post-survey reality (supervisor grep 2026-09-23):** 18 test files carry gateway
references. Most are HARMLESS (comments, FakeGateway receiver-spies, `gateway_handler=None`
ctor args — all post-SP2 shapes that keep tests green). The actual cleanup set is small.
Three micro-rounds, one Coder delegation each, ~10 calls each.

## Round 4A — tests/test_chat_handler.py (18 refs, the only file with real dead tests)

The only file with tests asserting GONE behavior:
- `test_inline_mention_to_gateway_agent_routes_to_gw` (:772) — asserts routing to
  gateway; post-R1 the receiver no-ops unregistered keys. REWRITE to pin the new
  behavior (receiver called with the key, warning logged, no raise) or DELETE if the
  rewritten shape duplicates an existing receiver pin. Coder's call; document.
- `test_noop_when_no_gateway` (:233) — name lies (there's no gateway to be None);
  rename `test_noop_when_arh_none` if it still tests the None-guard, else delete.
- The remaining refs are comments + the `make_handler(gateway_client=None,...)`
  compat shim (:196-224) — the shim keeps 30+ tests green; KEEP it (renaming its
  param would touch every call site — scope creep). Reword its docstring to say
  the arg is a legacy alias for the ARH spy.
- Enumerate every other ref disposition (comment / shim / kept-test).

Gates: full file green under xvfb; ruff baseline 0; enumerate deletions/renames.

## Round 4B — naming-hygiene file (light-touch, ≤2 files)

Files whose refs are comment-only or param-name-only (NO test behavior changes):
- tests/test_missing_message_fix.py (22 refs — survey shows comments/shapes)
- tests/test_command_handler.py (12)
- tests/test_agent_command_handler.py (7)
Disposition per ref: comment reword ONLY where the comment asserts dead behavior
("routes via gateway" → "routes via local runtime"); param/fixture names stay
(FakeGateway-as-receiver-spies are the post-SP2 pattern). Zero test deletions
expected. If ANY test actually asserts gateway behavior, STOP that file and report.
Gates: both files green (xvfb where GTK); ruff baselines measured.

## Round 4C — test_activity_bubbles.py (60 refs, own round)

Largest file. Survey says the refs are mostly event-payload fixtures driving
`on_gateway_event` — the R4/SPEC-07-pending ingestion path (register item, NOT dead).
Disposition: keep fixtures; reword comments that say "from gateway events" only where
misleading. ZERO behavioral deletions unless a test imports a deleted module
(check for `from gateway` — should be zero; any hit = STOP).
Gates: full file green under xvfb (65 baseline); ruff baseline measured.

## The remaining 12 files — adjudicated NO-TOUCH (supervisor ruling)

test_activity_drawer (9), test_activity_wiring (1), test_agent_runtime (3),
test_agents (2), test_architecture (4), test_chat_render (2), test_context (2),
test_feedback_processor (1), test_project_handler (6), test_prompt_loader (4),
test_special_agents (1), test_tools (3): all refs are comments/fixtures/None-args
at these densities — cleaning them is churn without coverage change. Excluded
from SP4; noted for SP5's post-mortem residue table.

## Verification (each round, paste ALL)

```
xvfb-run -a .venv/bin/python -m pytest [round's file(s)] -q 2>&1 | tail -1
.venv/bin/python -m ruff check [round's file(s)]
```

## COMPLETENESS (per round)
- [ ] Per-ref disposition table (kept/reworded/deleted/renamed — enumerated)
- [ ] Gates pasted, baselines old→new
- [ ] Any unexpected gateway-BEHAVIOR assertion = STOP and report
