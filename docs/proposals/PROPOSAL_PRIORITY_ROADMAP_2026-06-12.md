# Proposal Priority Roadmap — 2026-06-12

**Author:** Qaster
**Date:** 2026-06-12
**Status:** SUPERSEDED 2026-10-21 by `PROPOSAL-META.md`. Do not pick work from this roadmap.
**Source data:** 28 proposals audited + status banners added in commits `0de3a25` and `d1903d3`
**Companion files:** All `docs/proposals/*.md` files with sortable `status:` field

## Context

After auditing all 28 proposal files in `docs/proposals/`, we have:

| Status | Count | Meaning |
|---|---:|---|
| DONE | 11 | Shipped, no further work needed |
| PARTIAL | 12 | Core shipped, but a specific item remains open |
| PENDING | 3 | Not started, no decision made |
| SUPERSEDED | 1 | Abandoned in favor of a different approach |
| **Total open** | **15** | PARTIAL + PENDING = 12 + 3 |

*Update 2026-06-12 (initial): Tier 1.1 (`if not final_text: return` bug claim) was investigated and found to be a false alarm. `PROPOSAL-smarter-chat-ux.md` is now `DONE`. The `Total open` count above is now 17 (was 18).*

*Update 2026-06-13: Tier 1.2 and Tier 1.3 have shipped.*

- **Tier 1.2 — DONE** — `56a6cee feat(review-handler): emit git_commit feed cards on accept/reject` (the 11-line fix from the proposal, committed on 2026-06-12/13, was originally scoped as a QTR delegation but actually landed from a direct fix). Done count: 9 → 10.
- **Tier 1.3 — DONE** — `7a3d8c9 fix(prompt-loader): gate project-onboarding template to coder agents only`. Done count: 10 → 11. Total open: 17 → 15 (both items were PARTIAL in the source banner audit; their proposals now need their own `status:` fields updated to DONE as a follow-up, see Tier 2 / Tier 3 follow-ups below).
- **Tier 2.2 — changed from DONE to PARTIAL** (per Captain 2026-06-13) — the help-agent code shipped in `d6ed198` but the SPEC doc `docs/specs/SPEC-crabcakes-always-on-help-agent.md` is still in Draft state and has unbuilt items; needs a per-item audit before promotion. Total open unchanged (was 15, still 15 — the SPEC doc remains a PARTIAL deliverable).

This document ranks the 18 open items by **leverage** (signal × shipability ÷ cost), not by chronology or by how long the proposal has been sitting open. The goal is to convert as many PARTIALs to DONE/REJECTED/DEFERRED as cheaply as possible, leaving only the items that genuinely need a dedicated sprint.

**How to use this file:** when picking up a new piece of work, start at Tier 1.1 and work down. When you finish a tier, update the "Status" column. When a tier is exhausted, promote the next tier.

---

## Tier 1 — Quick wins (high signal, low risk, ≤ 1 day each)

These are the highest-leverage items. They were discovered during the audit and the design work is already done. They are all bug fixes or pure glue, with no design decisions remaining.

### 1.1 — ~~`PROPOSAL-smarter-chat-ux.md` — fix `if not final_text: return` bug~~ RESOLVED (false alarm)

- **Original claim:** `ui/handlers/chat_handler.py:568` `if not final_text: return` is a latent bug — empty `chat final` events get silently dropped.
- **Audit result (2026-06-12):** **NOT A BUG.** The early-return is intentional and is one half of a two-part recovery flow. When `chat final` arrives empty, the lifecycle-end event that arrives next fires `_handle_lifecycle_completed` (wired in `ui/handlers/connection_sync_handler.py:168-169`), which renders a fallback bubble from `_assistant_text_buffer` (populated at `activity_handler.py:285`). A `_chat_final_rendered` guard prevents double-render. 5 tests pass (`tests/test_missing_message_fix.py`, `tests/test_chat_handler.py:380`).
- **Lesson learned:** grep-only audits miss callback wiring across handlers. The setter `set_on_lifecycle_completed` exists in `activity_handler.py:134`, but the *wire-up* is in `connection_sync_handler.py:168-169`, not `window.py` — the place I originally looked.
- **Status:** [x] DONE (verified 2026-06-12 — audit finding invalidated, not a coding task)

### 1.2 — `PROPOSAL-feed-card-wiring.md` — wire `review_handler.py` to feed (REVISED 2026-06-12: 11-line fix, not 52-line merge)

- **Location:** `ui/handlers/review_handler.py`
- **What:** the original proposal's "❌ Review handler NOT yet wired to feed" flag is still accurate. `task_handler.py:51-84` has the pattern (`on_feed_card` constructor param + `_emit_feed_card()` helper).
- **Fix (option a — small):**
  1. Add `on_feed_card` ctor param to `ReviewHandler` (1 line)
  2. Store `self._on_feed_card = on_feed_card` (1 line)
  3. Add `_emit_feed_card(card_dict)` helper that builds a `FeedCardData` with `card_type="git_commit"`, mirroring `task_handler._emit_feed_card` (6 lines)
  4. Call it from `accept_changes` after successful commit (1 line)
  5. Call it from `reject_changes` after successful checkout (1 line)
  6. Wire `on_feed_card=self._feed_handler.add_card` in `window.py:450` (1 line)
  7. Add 1 regression test that exercises `/accept` and confirms a card is emitted
- **Why option (a) and not the merge:** the "merge two handlers" idea was technically correct but cost 5× the scope for a minor UX consistency gap that no user has reported. The 11-line fix achieves the user-visible consistency (a card appears in the feed for `/accept`) without touching `feed_handler.handle_accept/handle_reject`. The full unification becomes a follow-up Tier 3 item if you want it later.
- **Estimated effort:** ~11 lines + 1 test (1 day)
- **Status:** [x] DONE (committed `56a6cee` 2026-06-12, the 11-line fix landed from a direct implementation; the proposal's `status:` field still reads PARTIAL and needs a follow-up update)

### 1.3 — `PROPOSAL-project-onboarding.md` — fix agent-role gating bug

- **Location:** `utils/prompt_loader.py` — `compose_system_prompt()` at line 117
- **What:** the onboarding template loads for ALL agents, not just Coder. The fix is a one-line condition: `if agent_role != "coder": skip onboarding template` (or restrict to agents in a specific allowlist).
- **Estimated effort:** 1 line + 1 test
- **Why third:** correctness bug, affects every non-Coder agent's first message
- **Status:** [x] DONE (committed `7a3d8c9` 2026-06-12, `fix(prompt-loader): gate project-onboarding template to coder agents only`; the proposal's `status:` field still reads PARTIAL and needs a follow-up update)

---

## Tier 2 — Decisions, not code (1-2 hours each, mostly paperwork)

These items don't need engineering work — they need explicit Go/No-Go decisions written down. The proposals have been sitting in limbo because the decision wasn't made, not because the work is hard.

### 2.1 — `PROPOSAL_TELEGRAM_REMOTE_INPUT.md` — formally reject

- **Decision:** REJECT
- **Rationale:** the spec was drafted and then deleted. Zero Telegram code exists. The use case ("I'm away from my laptop") has better solutions (PWA, Tailscale, remote desktop). The security model (one configured chat_id rejecting all others) is fragile.
- **Action:** add a "REJECTED" status banner to the proposal with a 1-paragraph rationale. Update the `status:` field to `REJECTED`.
- **Estimated effort:** 10 minutes
- **Why now:** this is a PENDING that will never get worked on, but it's burning mental cycles. Closing it with a written reason is the deliverable.
- **Status:** [ ] TODO

### 2.2 — `PROPOSAL-crabcakes-always-on-help-agent.md` — promote SPEC to Shipped (PARTIAL, per Captain 2026-06-13)

- **Decision:** PARTIAL → SPEC promotion is paperwork, not a one-step DONE
- **Rationale:** the *code* is shipped (`d6ed198` feat: Crabcakes help agent — Phases 1-3, includes the data model, built-in provider, and agent definition). However, `docs/specs/SPEC-crabcakes-always-on-help-agent.md` is still in Draft state and contains items that have not been audited against the current code. Per Captain 2026-06-13, this is PARTIAL, not DONE — promoting without a per-item audit would let "spec says X, code does Y" drift re-emerge.
- **Shipped (verified by code inspection):**
  - `prompts/system/crabcakes.md` — help agent's system prompt
  - `~/.config/crabcakes/agents/crabcakes.yaml` has `auto_open: true` and `auto_add_to_projects: true`
  - `agent/special_agents.py:44-46` has the `auto_open` / `auto_add_to_projects` fields
  - `ui/window.py:167-179` opens tabs for `auto_open` agents
- **Remaining work (the PARTIAL):**
  1. Re-read `docs/specs/SPEC-crabcakes-always-on-help-agent.md` end-to-end
  2. For each "Acceptance criteria" / "Out of scope" / numbered item, mark it `✅ shipped (commit X)`, `⏳ deferred`, or `❌ not built`
  3. Promote the SPEC frontmatter from "Draft — for implementation" to "✅ Shipped (PARTIAL — see audit annotations)" with a 2026-06-13 date stamp
  4. Update the proposal's `status:` field to DONE only when step 3 is complete
- **Estimated effort:** 1 hour (audit + annotate + status update)
- **Status:** [~] PARTIAL (code shipped, SPEC audit pending)

### 2.3 — `PROPOSAL-implementation-engine.md` — explicitly defer

- **Decision:** DEFERRED to a future phase
- **Rationale:** the 8 task commands (`task`, `done`, `start`, `blocked`, `cancel`, `tasks`, `assign`, `priority`) are shipped in `ui/handlers/task_handler.py` and are working. The "deterministic PICK→BUILD→TEST→REVIEW→RECORD engine" is the missing piece, but the current "agents pick up tasks" model is sufficient for the current workflow. Adding automation now would add complexity without clear user value.
- **Trigger to revisit:** when agent task success rate crosses a threshold (TBD) or when the Captain has 3+ complaints about agents not picking up tasks.
- **Action:** write `docs/proposals/STATUS-implementation-engine.md` with the deferral rationale and revisit triggers. Update the `status:` field to `DEFERRED`.
- **Estimated effort:** 30 minutes
- **Why now:** converts an open PENDING into a decided-not-now.
- **Status:** [ ] TODO

---

## Tier 3 — Verification (30 minutes - 1 day each)

These items are PARTIAL with claims that need a quick test before they can be marked DONE. No design work, just verification.

### 3.1 — `PROPOSAL-mcp-agent-tools-hot-reload.md` and `PROPOSAL-mcp-client-integration.md` — verify "no restart" claim

- **Two proposals share the same unverified claim:** MCP server changes take effect on the next message without restart.
- **Visual UI:** shipped (`_build_mcp_section()` in `ui/views/agent_builder.py:534`, `mcp_servers: list[str]` field on `SpecialAgentDef` at `agent/special_agents.py:50`).
- **Runtime behavior:** needs a manual test.
- **Test plan:**
  1. Launch app, add an MCP server via the agent builder dialog
  2. Send a message to the agent
  3. Confirm the new tools are available
  4. If yes: mark both DONE. If no: file a real bug and prioritize it.
- **Estimated effort:** 30 minutes
- **Status:** [ ] TODO

### 3.2 — `PROPOSAL-user-defined-local-agents.md` — ship delete-agent

- **What's missing:** delete agent operation. The `agent_builder.py` (27K) supports create/edit, but no `delete_agent` method was found.
- **Action:** add a delete button + confirmation dialog. Wire to `utils/agent_defs.py` to remove the YAML file from `~/.config/crabcakes/agents/`.
- **Estimated effort:** 1 day
- **Secondary audit:** the "per-agent context injection + enforcement" parts of the proposal are abstract — audit whether they're actually used or aspirational. If aspirational, add a note to the proposal.
- **Status:** [ ] TODO

### 3.3 — `FIX-enforcement-stuck-misc.md` — per-bug audit

- **What:** the proposal lists 5 specific bugs (3, 4, 5, 6, 7) in `agent/enforcement.py`. `enforcement.py` is 29K and active. However, no commit in the visible log explicitly closes "Bugs 3-7" with this proposal as the fix; the proposal pre-dates several other enforcement rewrites.
- **Action:** read each of the 5 bugs, check if the code currently has the bug, either close it or re-file it.
- **Estimated effort:** half-day
- **Status:** [ ] TODO

### 3.4 — `FIX-identity-override.md` — per-bug audit

- **What:** the proposal's specific claim — that gateway agents receive identity-bearing system prompts as user message content — requires reading `agent/context.py` and the gateway integration to confirm. The bug doc reference (`docs/bugs/BUG_REPORT-identity-override.md`) was not located in this audit.
- **Action:** locate the bug doc, verify the claim against current code, close or re-file.
- **Estimated effort:** half-day
- **Status:** [ ] TODO

---

## Tier 4 — Substantive engineering (multi-day to multi-week, schedule explicitly)

These items have real design work and need dedicated sprints. They should not be picked up as side-tasks. The order below is my recommendation; budget accordingly.

### 4.1 — `PROPOSAL-graph-enhanced-self-improvement.md` — knowledge graph SI

- **Status:** PARTIAL. MCP memory server is wired. Typed/weighted/temporal graph nodes are NOT built. No `agent/knowledge_graph.py` or `utils/memory_graph.py` exists.
- **Why it's here:** the graph fabric is research-grade work. Either commit to a 2-week sprint or deprioritize for 6 months.
- **Estimated effort:** 2 weeks
- **Status:** [ ] TODO (scheduled for Q3 2026 TBD)

### 4.2 — `PROPOSAL-agent-package-restructure.md` — `agent/` split

- **Status:** PENDING. `agent/` is 7 files / 376K. `agent/runtime.py` is 1,627 lines (worse than the proposal's 1,575-line baseline). The proposed split into `llm/`, `domain/`, `policies/` packages was not done.
- **Why it's here:** big refactor with no user-facing benefit. Schedule it for "next time you're in there for something else."
- **Estimated effort:** 1 week
- **Status:** [ ] TODO (no committed date)

### 4.3 — `PROPOSAL-security-remediation-roadmap.md` — 46 findings

- **Status:** PENDING. 46 findings from `docs/SECURITY_ARCHITECTURE_REVIEW.md` (781 lines). No implementation evidence in the codebase.
- **Why it's here:** security work needs its own dedicated sprint, not a side-task. The Captain should review the 46 findings and decide which 5-10 to prioritize.
- **Estimated effort:** 1 sprint (2-3 weeks)
- **Status:** [ ] TODO (no committed date)

### 4.4 — `neural-memory-fabric.md` — speculative research

- **Status:** PENDING. This is an "extreme" speculative proposal with no implementation. No code uses knowledge graphs as the storage layer.
- **Why it's here:** probably never. If the Captain wants to commit to this, it needs its own roadmap.
- **Estimated effort:** research project, unbounded
- **Status:** [ ] TODO (lowest priority)

---

## Tier 5 — Already-superseded (no work needed)

### 5.1 — `PROPOSAL-activity-bubble-ux.md` — superseded by activity-drawer

- **Status:** SUPERSEDED
- **What happened:** Phase 2 (inline chat bubbles) was abandoned. The production approach is now `ui/views/activity_drawer.py` (32K, 2026-06-07).
- **Action:** none. The proposal's `status:` field already says SUPERSEDED.
- **Status:** ✅ done

---

## Summary

**Tier 1 (3 items, all DONE as of 2026-06-13):** fixes that closed audit-driven findings and latent bugs in shipped code. Tier 1.1 was a false alarm; Tier 1.2 (review_handler feed wiring) shipped in `56a6cee`; Tier 1.3 (agent-role gating) shipped in `7a3d8c9`. The proposals' own `status:` fields still need a follow-up update — see the 2026-06-13 header note.

**Tier 2 (3 items, ~2 hours total):** paperwork — Go/No-Go decisions, status promotions, deferral notes. Zero code. Item 2.2 is now correctly classified as PARTIAL (code shipped, SPEC audit pending) per Captain 2026-06-13.

**Tier 3 (4 items, ~3 days total):** verification work to convert PARTIALs to DONEs.

**Tier 4 (4 items, multi-week):** substantive engineering that needs dedicated sprints.

**Tier 5 (1 item):** already handled.

**Total open items:** 15 currently (down from 18 on 2026-06-12; Tier 1 fully closed = 3 items off the list). After Tier 2 + Tier 3 work, down to **8 open** (3 from Tier 2 paperwork, 4 from Tier 3 verification if none pass, plus 4 from Tier 4). Tier 1 is fully closed (0 open).

**Recommended sequence:**
1. ~~Tier 1 in one sitting (~1.5 days)~~ ✅ **TIER 1 CLOSED** (all 3 items resolved as of 2026-06-13)
2. Tier 2 in one sitting (~2 hours) — the next open work
3. Tier 3 one item at a time, between other work
4. Tier 4 as dedicated sprints

---

## How to update this file

When you finish an item:
- Mark `[ ] TODO` → `[x] DONE` (or `[~] DEFERRED` / `[!] REJECTED` if not DONE)
- Update the proposal's `status:` field in its banner
- Add a one-line note below the item describing what was done and which commit

When a tier is exhausted:
- Bump the remaining items to a "Tier 4.5" or "Backlog" section so the active tiers stay short

When new items appear (new proposals, audit findings):
- Slot them into the appropriate tier based on the leverage criteria, not chronologically
