# PROPOSAL: Meta Program

**Date:** 2026-10-21
**Status:** ACTIVE — this is the only priority document in `docs/proposals/`
**Supersedes:** every other file in this directory, as a source of what to build next. Those files stay on disk as design notes and history. Their status banners are not maintained and are not a queue.

A survey of this folder on 2026-10-21 found the banners months out of date. Telegram, the WebKit chat and feed, the diff viewer, the file tree, per-coder worktrees, same-role session keys, stop-all, `jev_ask`, OpenClaw gateway removal, and Auxilium removal are already in the tree. This document records the five programs that are still open and worth building, in the order they should be built.

`DEFERRED-ITEMS.md` still holds. Nothing in this program reopens A-11, HIGH-2, or HIGH-4.

---

## 0. How to use this file

Pick work from the programs below, in order. A program may start once the one ahead of it has its "done when" line, except where a section says a later program may be specced in parallel.

A new idea does not get its own proposal. It amends this file, or it waits. Opening another `PROPOSAL-*.md` and treating it as scheduled is how this folder accumulated fifty documents that no longer describe the app.

Shipped behavior is not rebuilt and not re-argued here. The June 2026 roadmap (`PROPOSAL_PRIORITY_ROADMAP_2026-06-12.md`) is retired.

---

## 1. Order

| # | Program | Why this slot |
|---|---|---|
| 1 | Acceptance gate | The next four will be declared done. This is the mechanism that can disagree. |
| 2 | Retrievable memory | Long sessions already lose tool output. More agents make that worse. |
| 3 | Dream consolidation | The journal already records mistakes. Nothing turns them into a prompt change. |
| 4 | Work-unit run loop | Units exist and do not advance unless a person types the next command. |
| 5 | Group chat | Largest structural change. Last, because it multiplies context, feed volume, and false "done". |

Group chat is the biggest product idea in the folder. It is last on purpose. The v2 change list's own chain said not to multiply speakers before memory and retention are real, and program 1 has to exist before a shared transcript can be called finished.

---

## 2. Program 1 — Acceptance gate

**Source:** `PROPOSAL-acceptance-gate.md` (2026-10-09). That file remains the design. This program adopts it whole.

**Outcome.** One command in the repo can refuse a claim that was marked done. Each load-bearing claim is a row with two runnable parts: a test that must pass on the clean tree, and a mutation diff that must make that test fail. A mutation that still passes means the test cannot see the behavior, and the gate fails.

**Build.** As written in the source, SP1 then SP2:

- `scripts/acceptance.py` with `rows`, `suite`, `smokes`, and `all`
- `docs/acceptance/rows.yaml`
- `tests/mutations/` for the row diffs
- First rows R-1, R-2, R-3, R-5, R-6 from the source §2.3
- R-4 lands with the stale-turn fix, not before it
- Spec skeleton gains an Acceptance rows table. A spec that cannot name its mutation has not stated its claim.
- `suite` diffs against `tests/known_failures.txt` and may only shrink that set

**Also in this program**, taken from `PROPOSAL-post-responsiveness-priorities.md` and not given a separate queue:

- CI that runs this gate (the source left CI to that September doc; `.github` is still empty)
- A small performance bound on feed card update and feed load, so the 0.62 s main-thread stall class cannot return unnoticed

**Left behind.** Coverage targets. Typing the whole UI. Burning the historical failure baseline as its own project. Retrofitting every old spec.

**Done when.** `scripts/acceptance.py rows` exits non-zero if any registered mutation still passes, and exits non-zero if any clean node fails. A clean checkout import smoke (R-5) and a `MainWindow` construction smoke (R-3) are in that run.

---

## 3. Program 2 — Retrievable memory

**Source:** `PROPOSAL-context-management-phase-2.md`. Take T1.2 and T1.3 only.

**Already shipped, do not rebuild.** Just-in-time file retrieval is in the tree under `PROPOSAL-jit-context-discovery.md`. P1–P7 compaction, including `prune_tool_outputs`, is in `agent/context_strategy.py`.

**The hole.** `prune_tool_outputs` replaces an old tool result with `[compacted — {tool} {N}]`. The method comment calls this lossless. The bytes are not kept. An exec can be run again (SPEC-33). Every other tool result is gone, and the summary that replaces trimmed turns is one free-text string.

**Build.**

1. **Offload, then stub.** When a tool result crosses a size threshold, write the full text under the project (`.crabcakes/tool-outputs/`) and put a stub in the conversation: path, byte count, line count, short preview. Add one read-only tool that returns that file. The same size rule applies to what the tool returns, so a read cannot paste the whole blob back into context.
2. **Typed digest.** When compaction writes a summary, store fields the agent can use without rereading prose: arc, decisions, constraints, open questions, paths touched, attempts rejected. Keep it in the summary message the strategy already injects.

**Left behind.** T1.1 hierarchical summary-of-summaries. T1.4, already shipped. T1.5 per-tool retention tables. Those can be an amendment after offload has been in use.

**Done when.** A tool result that was compacted can be read back by path without re-running the tool, and a compacted session's summary exposes decisions and open questions as fields. Both get acceptance rows under program 1.

---

## 4. Program 3 — Dream consolidation

**Status (2026-10-21): DEFERRED by PM.** Dream consolidation will be built on top of a native timer/scheduler substrate that does not exist yet (timers, cron, reminders). That substrate is to be built first, as one program with dream consolidation on top of it. The old SPEC-4 execution model (OpenClaw cron) is dead — the OpenClaw gateway client is gone — so the scheduling half must be rebuilt natively (GLib-driven, persistent, restart-safe) before the dream cycle has anything to trigger it. Design questions open: catch-up semantics for missed fires, whether timers are agent-facing (a tool) or config-driven, store location (`.crabcakes/` vs global), and whether `transport/openclaw.py` gets removed. When work resumes, this program splits into (a) timers/cron substrate, (b) dream consolidation on the dispatcher. PM to revisit later.

**Source:** `CODER_SELF_IMPROVEMENT_PROPOSAL.md`, Layer 5 only.

**Already shipped, do not rebuild.** Layers 1–4 are in the app under different file names than the 2026-05 proposal expected:

- Bug journal and project rules load from `.crabcakes/` in `utils/prompt_loader.py`
- `utils/feedback_processor.py` appends structured audit reports to the journal
- `agent/enforcement.py` is the verification layer
- The `self_improvement` block on agent YAML, including the `dream_consolidation` flag, is parsed

The flag defaults off. Nothing reads it. There is no dream prompt, no dream log, and no proposal queue.

**Build.** A cycle a person can run, and later schedule:

1. Read review-log and bug-journal entries since the last cycle
2. Call a model with `prompts/system/dream-analysis.md`
3. Write proposed edits to the role prompt as files under `.crabcakes/dream-proposals/`
4. Write the cycle to `.crabcakes/dream-log.jsonl`
5. Leave the live prompt unchanged until a person accepts a proposal

The source's safety rules stand: no prompt change without approval, no journal deletion without approval, a second run on the same input does not file a second proposal, and a journal over the source's size cap is distilled into patterns rather than growing forever.

**Left behind.** `PROPOSAL-graph-enhanced-self-improvement.md` and `neural-memory-fabric.md`. Same wish, with a graph schema the app does not have. If the dream proposals turn out to be useless, the graph is not the next step.

**Done when.** One manual cycle on a project that already has journal entries produces a reviewable prompt diff and does not modify the live prompt. Accepting one proposal is a separate, explicit action.

---

## 5. Program 4 — Work-unit run loop

**Source:** the cycle in `PROPOSAL-implementation-engine.md` (2026-04-26). The file layout and the `` `task `` command set in that document are obsolete.

**Already shipped.** `models/work_unit.py` is the unit of work. Statuses are `draft`, `spec-pending`, `spec-ready`, `in-progress`, `auditing`, `done`, `cancelled`. A unit has a builder, an auditor, a supervisor, dependencies, and a lease. `utils/workflow_state.py` tracks phase in `.crabcakes/workflow.md`. Extra writers already get a git worktree (`AgentRuntimeHandler._worktree_for_turn`).

**The hole.** A unit moves when a person types the next command. There is no cycle that picks the next unblocked unit and walks it.

**Build.** One command, on the work-unit store, not on a second task file:

1. **Pick** the highest-priority unit that is `spec-ready`, whose dependencies are `done`, and whose lease is free
2. **Build** — assign the builder, take the lease, run the turn in that unit's worktree
3. **Test** — the builder's turn is not eligible to leave `in-progress` until the unit's test command has been run and its result is stored on the unit
4. **Review** — move to `auditing` and hand the same unit to the auditor
5. **Record** — auditor outcome writes `done` or returns the unit to `in-progress` with the reason on the unit
6. **Repeat** until the queue is empty, a unit blocks, or a person pauses

Pause and resume persist on the project. A restart reads the store and continues at the current stroke. The person can pause, skip a unit, or amend the queue at any time. The loop suggests the next stroke and stops when it cannot take one honestly.

**Left behind.** `utils/engine.py` as specified, `tasks.md` as the engine's database, removing the current `/work` commands, and any design that runs build and audit in one undifferentiated chat with no lease.

**Done when.** A project with two `spec-ready` units can be started, stopped, restarted, and both units end `done` or explicitly blocked, with the test result and the audit outcome stored on the unit. Acceptance rows cover pick order, the test gate, and pause across a restart.

---

## 6. Program 5 — Group chat

**Source:** `DEVELCAKES-V2-CHANGE-LIST.md` §1.3 and §1.4. The rest of that change list is already in the tree or has been dropped on purpose. Do not reopen it.

**Already in the tree from that list.** HTML chat (`ui/views/chat_surface.py`) and HTML feed (`ui/views/feed_surface.py`). Telegram bridge into the running app. Per-agent worktrees. Unique `session_key`s for a second Coder or Debugger (`utils/agent_defs.py`). `stop_all_agents`. The OpenClaw gateway client is gone. Auxilium and its KB stack are gone (`tests/test_no_kb_residuals.py` keeps them gone).

**Build, in this order.**

1. **Transcript model.** One shared project transcript with a speaker on each turn, stored so two writers cannot silently drop each other's turns. The v2 note stands: whole-file rewrite of a shared JSON file loses updates. SQLite with WAL is the candidate named there. Per-agent conversation files remain the store for that agent's own model context. The shared transcript is the room, not a replacement for compaction.
2. **Addressing.** A turn in the room names who it is for. An unaddressed turn is for the room and does not start every agent.
3. **JEV routing.** Who speaks next is a JEV choice over the roster, with a confidence threshold and a versioned config. On timeout, 429, or 529, the turn falls back to a person choosing, or to a dumb rule in that same config (round-robin or keyword). Log every verdict: decision, confidence, input hash, timestamp, and what the app did with it. `agent/llm/jev_client.py` and the `jev_ask` tool are the client to extend, not a new stack.
4. **Completion check, advisory only.** For a named loop type (coder↔debugger audit, coder↔supervisor send-back), JEV may say the exchange looks finished. The app shows that and writes an audit row. It does not close the loop, advance a work unit, or stop the agents. A false "complete" is the expensive failure, and program 1's rows must prove the app still refuses to act on it.

**Amendment (2026-10-22, PM direction).** This program is being implemented as **`docs/specs/SPEC-39-ROOM-ADDRESSING.md`**, and that spec — not this list — is the plan of record. SPEC-39 takes items 2 (addressing) and 3 (JEV routing) in a deliberately thinner shape: no SQLite/WAL shared transcript store, no versioned fallback config, no completion check. The room is each agent's own conversation file — a member who overhears a line gets it as a `user` message via `note_room_line`, appended and saved under that runtime's lock, queued while the listener is mid-turn. There is no second writer, so the shared-transcript concern in item 1 does not apply, and that item stays here in this proposal as a design note, not a build order — it is not moved into SPEC-39 and it is not scheduled. Item 3's fail-open is simplified in SPEC-39: any doubtful decision (absent JEV, low confidence, unknown choice) fans out to every member, which is today's ALL behavior. Item 4 (advisory completion check) is deferred, gated on SPEC-39's decision log existing and showing that loop-closure confusion actually costs turns. Feed retention and per-agent filtering do not ride with SPEC-39; they are a separate future spec, not an amendment to it.

Feed retention and per-agent filtering ship in the same change as the shared transcript. The v2 list recorded on the order of 15k cards with one team. A room with several speakers will make the feed the product again, and an uncapped feed will make it unusable.

**Left behind.** Rebuilding Telegram, the WebKit surfaces, worktrees, or stop-all. JEV as an automatic approver of tools or of accept/reject (that is `PROPOSAL-jev-decision-substrate.md` §3.14, and it stays off the enforcement path). A detector that ends an audit by itself.

**Done when.** Two agents and a person can share one project transcript, a logged JEV decision chooses the next speaker or falls open to the person, and a completion verdict is visible and has no effect on work-unit status. Acceptance rows cover the fall-open path and the "verdict does not close the loop" rule.

---

## 7. Everything else

Not scheduled. Design notes stay where they are. If the code and a banner disagree, the code wins. In particular, nobody picks up:

| File | Why it is not in this program |
|---|---|
| `PROPOSAL_PRIORITY_ROADMAP_2026-06-12.md` | Retired by this file |
| `PROPOSAL-web-ui-replacement.md`, `PROPOSAL-textview-texttag-rendering.md`, `PROPOSAL-rendering-fix-strategy.md` | The chat and the feed already render in WebKit |
| `PROPOSAL-activity-bubble-ux.md`, `PROPOSAL-activity-drawer.md` | Drawer shipped; whether it stays is a product question, not a proposal queue |
| `PROPOSAL_TELEGRAM_REMOTE_INPUT.md` | A Telegram bridge is in the app |
| `PROPOSAL-runtime-modular-extraction.md`, `PROPOSAL-agent-package-restructure.md` | `agent/llm/`, `agent/tool_middleware.py`, `agent/persistence.py`, and `agent/audit.py` exist. Further splits stay under `DEFERRED-ITEMS.md` A-11 |
| `PROPOSAL-graph-enhanced-self-improvement.md`, `neural-memory-fabric.md` | Parked behind program 3 |
| `PROPOSAL-crabcakes-consensus-llm.md` | Unscoped. A local three-model server is not this program |
| `PROPOSAL-implementation-engine.md` | The cycle was taken. The command set and `tasks.md` were not |
| `PROPOSAL-auxilium-three-tier-help-agent.md` | Removed. `tests/test_no_kb_residuals.py` guards the removal |
| `PROPOSAL-jev-decision-substrate.md` | `classify_unknown` is wired and `jev_ask` exists. Further JEV ideas wait until program 5's log has data, and §3.14 stays unbuilt |
| `FIX-enforcement-stuck-misc.md`, `FIX-identity-override.md` | Bug audits, not programs. Re-file a live bug if the code still has it |
| `PROPOSAL-mcp-client-integration.md`, `PROPOSAL-mcp-agent-tools-hot-reload.md` | Basic MCP is in the app. Phase B waits |
| `PROPOSAL-user-defined-local-agents.md` | Create and edit shipped. Delete-agent is a small fix, not a program |
| `PROPOSAL_FILE_TREE_ENHANCEMENTS.md`, `PROPOSAL-ONE-CLICK-DIFF.md`, `PROPOSAL-auto-accept-granular-controls.md` | In the tree. A missing polish item is a bug, not a revival of the proposal |

---

## 8. Acceptance rows this program owes

Program 1 builds the gate. Each later program registers its rows when its spec is written, before implementation, using the source rule: a spec that cannot name the mutation has not defined the claim.

| Program | Claims the rows must be able to fail |
|---|---|
| 2 | Compacted tool text is recoverable by path. Digest fields survive compaction. |
| 3 | A dream cycle writes a proposal and does not touch the live prompt. |
| 4 | Pick respects priority and dependencies. `in-progress` cannot exit without a stored test result. Pause survives restart. |
| 5 | JEV failure falls open. A completion verdict does not change work-unit status. |
