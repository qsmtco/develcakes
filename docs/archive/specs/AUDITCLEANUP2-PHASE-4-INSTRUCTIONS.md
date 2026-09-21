# SPEC-AUDIT-CLEANUP-2 Phase 4 — Conversation Shims + Shim-Only Tests

**Spec:** `docs/specs/SPEC-AUDIT-CLEANUP-2-DEAD-CODE-SWEEP.md` — READ IN FULL. §"Phase 4" is authoritative.
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load fresh, Discovery block first, every rule.
**Supervisor:** special:supervisor | **Builder:** special:coder | **Auditor:** special:debugger
**Baseline:** tree clean at `b2d0fe8`. Standing rules in force (no resets, xvfb-run -a, pyflakes gate, re-grep before deletion, + NEW: grep docs/ARCHITECTURE.md for every deleted identifier).

## Background (verified by supervisor 2026-09-07 morning + re-verified this phase)

`models/conversation.py` has two deprecated delegation shims kept only for tests:
- `trim_to_token_limit` (:404) — defers to `agent.context_strategy.DefaultContextStrategy.compact`
- `_last_exchange_summary` (:448) — defers to the strategy's `_summary`

They violate the models→agent layer rule (deferred import crossing the boundary). The strategy owns the real logic: `agent/context_strategy.py` + suites `test_context_strategy*.py`, `test_llm_summarize_strategy.py`, `test_runtime_compaction.py`.

**The spec's coverage rule (READ TWICE):** before deleting any test, confirm the equivalent behavior is covered by a strategy-level test. If ANY assertion is unique, MIGRATE it to a strategy-level test FIRST, then delete. Report the coverage comparison.

## Step 1 — Coverage inventory (before touching anything)

1. List every assertion in the shim-only tests:
   - `tests/test_conversation.py` — the `trim_to_token_limit` block (~:411-510, 5 tests: :411,:420,:428,:434 + :484-510 if those are trim tests — read and classify each)
   - `tests/test_phase4.py` — §4.10 classes: `_last_exchange_summary` tests (~:203-275, class at :207) and trim-integration tests (~:280-385, class at :284)
2. For each assertion, grep the strategy suites (`test_context_strategy.py`, `test_context_strategy_audit_fixes*.py`, `test_llm_summarize_strategy.py`, `test_runtime_compaction.py`) for an equivalent. Build a table: assertion → covered-by (file::test) or UNIQUE.
3. **Migrate UNIQUE assertions** into the appropriate strategy suite as new tests (same behavior, called through `DefaultContextStrategy` — e.g. `strategy.compact(conv, ...)` / the summary path — not through the shim). These new tests must pass BEFORE the shims die (they don't touch the shims).

## Step 2 — Delete the shims

1. `models/conversation.py`: delete `trim_to_token_limit` (:404-446) and `_last_exchange_summary` (:448-470). Read the full span first — the docstring at :322 references `trim_to_token_limit`; update it to point at `DefaultContextStrategy.compact` (spec requirement).
2. **Re-grep both identifiers repo-wide** — expected refs: only the shim-only tests you're about to delete (plus possibly docstrings/comments — update or flag each).

## Step 3 — Delete the shim-only tests

Delete exactly the tests inventoried in Step 1 as shim-only (fully-covered + the ones you migrated). If a test class contains BOTH shim-only and non-shim tests, delete only the shim-only methods (read each test before deciding — do not delete a class wholesale unless every method is shim-only).

## Verification (all pasted)

1. The Step-1 coverage table (assertion → covered-by/UNIQUE) — this is the core deliverable.
2. Post-deletion greps: `trim_to_token_limit|_last_exchange_summary` → zero in `.py` (docs/specs/post-mortems may retain historical mentions — flag, don't edit history).
3. **NEW RULE check:** grep `docs/ARCHITECTURE.md` for both identifiers — if documented, update in the same commit (the doc's §0 rule).
4. Suites: `tests/test_conversation.py` (remaining tests green), `tests/test_phase4.py` (remaining), the strategy suites (incl. your migrated tests — green), `tests/test_runtime_compaction.py`, `tests/test_context_strategy_audit_fixes*.py`, `tests/test_llm_summarize_strategy.py`.
5. Import smoke: `python3 -c "import models.conversation; import agent.context_strategy; import agent.runtime"`.
6. pyflakes: undefined-name 0; no new findings.
7. LOC accounting pasted.

## Commit (1)

`refactor(conversation): remove deprecated trim/summary shims — migrate unique test coverage to strategy suites (SPEC-AUDIT-CLEANUP-2 Phase 4)`

## COMPLETENESS checklist
- [ ] Discovery block
- [ ] Coverage table pasted (the core deliverable — assertion → covered-by / UNIQUE / migrated)
- [ ] Migrated tests green BEFORE shims deleted (order in the single commit)
- [ ] Both shims + shim-only tests deleted; greps clean; ARCHITECTURE.md checked
- [ ] All suite outputs pasted
- [ ] Related issues flagged, not silently fixed

**STOP after this phase.** Phase 5 (scripts/comments/pycache) is the final sweep phase.
