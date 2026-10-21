# Coder Self-Improvement System — Proposal

**Date:** 2026-05-18
**Authors:** Qaster (with Captain JAQx)
**Status:** Superseded 2026-10-21 by `PROPOSAL-META.md` Program 3, which schedules Layer 5 only. Layers 1–4 are already in the tree (prompt injection, `utils/feedback_processor.py`, `agent/enforcement.py`). The June banner below is stale.
**Repository:** github.com/qsmtco/crabcakes
**Target Agent:** Coder (special:coder in CrabCakes)

> **Status (verified 2026-06-12):** ⚠️ **PARTIALLY DONE** — 
> **status:** `PARTIAL` — sortable tag for `ls | grep STATUS` The config layer is shipped (every agent YAML has a `self_improvement: { bug_journal, project_rules, enforcement, structured_feedback, dream_consolidation }` block, parsed by `agent/special_agents.py:42-52`). However, the **5-layer runtime system** is NOT built. There are no separate layer modules (no `bug_journal.py`, no `project_rules.py`, no `dream_consolidation.py` in `agent/` or `utils/`). The prompt files `prompts/system/coder.md` and `prompts/system/debugger.md` are role-specific system prompts, not the 5-layer runtime the proposal describes. **The proposal is largely aspirational; only the YAML config surface exists.**

---

## Problem Statement

Coder makes preventable mistakes. During crabwatch task 5, a single bug fix took 3 rounds because Coder didn't read the failing test before fixing, didn't run the full test suite after fixing, and didn't understand mock object behavior. After updating Coder's system prompt with process rules, the same class of bugs dropped to zero on tasks 6 and 7.

But the prompt update was manual. We (Qaster and Captain) had to observe the bugs, diagnose the patterns, and write the rules ourselves. This doesn't scale. Every new class of bug requires human intervention to update the prompt.

The core problem: **Coder has no mechanism to learn from its own mistakes.** The system prompt is static hand-authored text. It only improves when a human updates it.

## Vision

Build a layered self-improvement system where Coder gets progressively better at writing code through:

1. **Accumulated bug knowledge** — a growing journal of past mistakes
2. **Project-specific context** — rules that apply to the current codebase
3. **System-enforced verification** — structural guarantees, not just prompt requests
4. **Structured communication** — machine-parseable feedback between agents
5. **Autonomous prompt evolution** — Coder's system prompt improves itself based on review feedback

Each layer builds on the previous one. We implement and test incrementally.

---

## Research Foundation

This proposal draws from several external sources:

### Industry Practices

- **Claude Code** uses `CLAUDE.md` files (per-project instructions injected into context) and a "dream memory consolidation" system that synthesizes and prunes memories during idle time. Their general-purpose subagent prompt is remarkably short (~285 tokens) — the intelligence lives in the infrastructure around the model, not the prompt. Source: [Piebald-AI/claude-code-system-prompts](https://github.com/Piebald-AI/claude-code-system-prompts)

- **Augment Code** (SWE-bench #1 open source) emphasizes: context first, consistent tool definitions, present a complete picture of the world, be thorough in prompts (don't worry about length), and validate tool inputs server-side rather than trusting the model. Source: [Augment Code Blog — 11 Prompting Techniques](https://www.augmentcode.com/blog/how-to-build-your-agent-11-prompting-techniques-for-better-ai-agents)

- **VILA-Lab's analysis of Claude Code** (512K lines analyzed): Only 1.6% of Claude Code is AI decision logic. The other 98.4% is deterministic infrastructure — permission gates, context management, tool routing, recovery logic. The agent loop is a simple while-loop; the real engineering lives in the systems around it. Source: [Dive into Claude Code](https://github.com/VILA-Lab/Dive-into-Claude-Code)

### Self-Improvement Research

- **OpenAI Self-Evolving Agents Cookbook**: Demonstrates a feedback loop where an LLM-as-judge grades agent outputs, then a meta-prompter rewrites the system prompt based on what went wrong. Key finding: the loop works even with simple grading criteria. Source: [OpenAI Cookbook](https://developers.openai.com/cookbook/examples/partners/self_evolving_agents/autonomous_agent_retraining)

- **Imbue's Darwinian Evolver**: LLM-driven evolutionary optimization achieves 2-3x performance improvements. Key insight: "mutations targeted at specific failure cases are dramatically more effective than random perturbation." The system maintains a learning log of what was tried and what happened, preventing re-trying failed approaches. Source: [Imbue Research](https://imbue.com/research/2026-02-27-darwinian-evolver/)

- **Hermes Agent Evolutionary Self-Improvement Proposal**: Proposes native evolutionary optimization of skills and system prompts using batch evaluation as the fitness function. Identifies that the "instructions layer" is the sweet spot — instructions are text that LLMs can meaningfully mutate, changes can be evaluated against real tasks, and results are immediately deployable. Source: [Hermes Agent Issue #337](https://github.com/NousResearch/hermes-agent/issues/337)

### What Nobody Is Doing

Everyone else is either:
- Hand-tuning prompts (slow, doesn't scale)
- Building complex evolutionary optimization frameworks with custom evaluation harnesses (expensive, overkill for most use cases)

Nobody is using the **natural output of a multi-agent adversarial review loop** as the evolution signal. That's our angle.

---

## Architecture Overview

The system has 5 layers, implemented sequentially. Each layer is independently useful but enables the next.

```
┌─────────────────────────────────────────────┐
│ Layer 5: Dream Consolidation                │
│   Autonomous prompt evolution via            │
│   review feedback analysis                  │
│                                             │
│ ┌─────────────────────────────────────────┐ │
│ │ Layer 4: Structured Feedback Protocol   │ │
│ │   Machine-parseable audit reports       │ │
│ │   between reviewer and coder            │ │
│ │                                         │ │
│ │ ┌─────────────────────────────────────┐ │ │
│ │ │ Layer 3: Auto-Test Enforcement      │ │ │
│ │ │   System-guaranteed test execution  │ │ │
│ │ │   after every code write            │ │ │
│ │ │                                     │ │ │
│ │ │ ┌─────────────────────────────────┐ │ │ │
│ │ │ │ Layer 2: Project Rules          │ │ │ │
│ │ │ │   Per-codebase Coder context    │ │ │ │
│ │ │ │                                 │ │ │ │
│ │ │ │ ┌─────────────────────────────┐ │ │ │ │
│ │ │ │ │ Layer 1: Bug Journal        │ │ │ │ │
│ │ │ │ │   Accumulated mistake DB    │ │ │ │ │
│ │ │ │ └─────────────────────────────┘ │ │ │ │
│ │ │ └─────────────────────────────────┘ │ │ │
│ │ └─────────────────────────────────────┘ │ │
│ └─────────────────────────────────────────┘ │
└─────────────────────────────────────────────┘
```

**Dependency chain:** Each layer uses capabilities from the layers below it.
- Layer 2 depends on Layer 1's bug journal format
- Layer 3 is independent but benefits from Layer 1's patterns
- Layer 4 produces the structured data that Layer 5 consumes
- Layer 5 depends on Layers 1-4 all being in place

---

## Layer 1: Bug Journal

### What

A growing, git-tracked file of Coder's past mistakes. Each entry documents a specific bug, the root cause, the fix, and the lesson. This file gets injected into Coder's system prompt context at the start of every task.

### Why

The Common Pitfalls table in the current `coder.md` is static and generic. A bug journal is **personal** — it's Coder's actual mistakes, not hypothetical ones. LLMs pattern-match against concrete examples far more effectively than abstract rules. Imbue's research confirms: failure-driven mutations (showing what specifically went wrong) are dramatically more effective than random perturbation.

### How It Works

**File location:** `.crabcakes/coder-bugs.md` (per-project, git-tracked)

**Entry format:**
```markdown
## Bug #1 — 2026-05-18 — watcher.py

**Task:** Fix moved event detection in DebouncedHandler
**Mistake:** Used `if dest_path is not None` — MagicMock objects are always truthy
**Expected:** Only real moved events detected
**Actual:** Every event treated as moved (dest_path is a truthy MagicMock)
**Fix:** Changed to `isinstance(dest_path, str) and dest_path`
**Lesson:** Mock objects are always truthy — always check type, not truthiness
**Pattern:** mock-truthiness
```

**Integration point:** `utils/prompt_loader.py` → `compose_system_prompt()`. When building Coder's system prompt, read `.crabcakes/coder-bugs.md` and append it as a section. The prompt loader already handles reading `prompts/system/coder.md` — extend it to also read the project's bug journal.

**How entries get created:**
- Initially: manual. Qaster writes entries during adversarial review.
- Later (Layer 4+): structured feedback from reviewers auto-populates the journal.

**What stays in `coder.md`:** The generic Common Pitfalls table stays as universal rules. The bug journal adds project-specific scar tissue on top.

### Key Design Decisions

- **Per-project, not global.** Bug #7 about MagicMock truthiness in crabwatch doesn't help when Coder is working on a Python project that doesn't use mocks. Project-scoped journals keep context relevant and small.
- **Git-tracked.** Bug journals are part of the project knowledge. When someone clones the repo, Coder inherits the accumulated wisdom.
- **Size cap.** If the journal grows beyond ~50 entries, the dream consolidation layer (Layer 5) should distill older entries into higher-level patterns and prune the originals.
- **Pattern tags.** Each entry has a `Pattern:` field (e.g., `mock-truthiness`, `partial-test-run`, `type-confusion`). This enables the dream layer to cluster related bugs and generate meta-rules.

### Files to Create/Modify

- Create: `.crabcakes/coder-bugs.md` (per project, template)
- Modify: `utils/prompt_loader.py` — add bug journal injection
- Modify: `prompts/system/coder.md` — reference the bug journal in the Bug Fix Protocol

### Success Criteria

- Coder's system prompt includes bug journal content when working on a project that has one
- After a round of adversarial review where a bug is found, the reviewer can add an entry to the journal
- Coder references past bugs when encountering similar situations (observable in its reasoning)

---

## Layer 2: Project Rules

### What

A per-project rules file (like Claude Code's `CLAUDE.md`) that provides Coder with codebase-specific context, gotchas, and conventions. Automatically injected into Coder's system prompt when working on that project.

### Why

Coder's prompt is universal — it says "run tests after every change" but doesn't know *how* to run tests in this specific project. Does it need `source .venv/bin/activate` first? Does it use pytest or unittest? Are there specific test naming conventions?

Claude Code solved this with `CLAUDE.md`. We should do the same. Each project already has `.crabcakes/` — we add a Coder-specific rules file there.

### How It Works

**File location:** `.crabcakes/coder-rules.md` (per-project, git-tracked)

**Content structure:**
```markdown
# Coder Rules — [project-name]

## Environment
- Python 3.12.3, venv at `.venv/`
- Always activate: `source .venv/bin/activate`
- Test runner: `python3 -m pytest tests/ -v`

## Test Conventions
- Tests in `tests/test_{module}.py`
- Uses pytest with MagicMock fixtures
- Tests mock filesystem events — be aware of MagicMock truthiness

## Architecture
- watcher.py uses DebouncedHandler (watchdog)
- writer.py handles context.md I/O
- Entry points: `python3 -m crabwatch.watcher`, `python3 -m crabwatch.diary`

## Known Gotchas
- service file already has correct venv python paths — don't sed them
- .crabcakes/ must be filtered in filesystem watcher (infinite loop risk)
- diary.py uses `python3 -m crabwatch.diary` (module form), NOT `crabwatch-diary` (script)
```

**Integration point:** Same as Layer 1 — `utils/prompt_loader.py` → `compose_system_prompt()`. Load `.crabcakes/coder-rules.md` and inject as a "Project Context" section.

**How it gets created:**
- Initially: manual. When a new project is opened in CrabCakes, the PM (Captain or Qaster) writes the rules file.
- Could be auto-generated: A future enhancement could have CrabCakes analyze the project structure and generate a starter rules file (similar to Claude Code's `/init` command that generates `CLAUDE.md`).

### Relationship to Layer 1

The bug journal (Layer 1) captures *past mistakes*. Project rules (Layer 2) capture *project-specific knowledge*. They're complementary:

- Bug journal: "I once used `is not None` on a MagicMock and broke everything"
- Project rules: "This project uses MagicMock in tests — always check types, not truthiness"

Over time, patterns from the bug journal should be promoted into project rules if they apply broadly to the project.

### Files to Create/Modify

- Create: `.crabcakes/coder-rules.md` (per project, template)
- Modify: `utils/prompt_loader.py` — add project rules injection (can be done alongside Layer 1)

### Success Criteria

- Coder's system prompt includes project rules when working on a project that has them
- Coder follows project-specific conventions (activates venv, uses correct test commands) without being told each time
- New projects can be onboarded by creating a rules file

---

## Layer 3: Auto-Test Enforcement

### What

A system-enforced verification layer that automatically runs tests after Coder writes code. Not a prompt request — a structural guarantee. Coder physically cannot complete a task without tests passing.

### Why

The updated `coder.md` says "run the full test suite after every change." But that's a prompt instruction — the model can ignore it, forget it, or decide it's not necessary. The Augment Code research confirms: "if the model calls a tool incorrectly, do not raise an exception. Instead, return a tool result that explains what the error was." Enforcement belongs in the infrastructure, not the prompt.

CrabCakes already has `agent/enforcement.py` (591 lines) with:
- Tier 1: Syntax guard (runs `py_compile`, `node --check`, `bash -n`)
- Configurable per-project via `.crabcakes/enforcement.json`
- TTL-cached config loading
- Result formatting that appends to tool output

We extend this with a test tier.

### How It Works

**Trigger:** After Coder calls `write_file` or `edit_file` on a `.py` file.

**Flow:**
1. Enforcement layer detects a Python file was written
2. Find associated test file: `tests/test_{module}.py` (configurable convention)
3. If test file exists, run it: `source .venv/bin/activate && python3 -m pytest tests/test_{module}.py -v --tb=short`
4. Inject result into Coder's tool output: "⚠️ 2/5 tests failed in tests/test_watcher.py" with failure details
5. Coder sees the failures in its next turn and can fix them

**Configuration in `.crabcakes/enforcement.json`:**
```json
{
  "tiers": {
    "syntax": true,
    "tests": true,
    "lint": false
  },
  "test": {
    "runner": "pytest",
    "command": "source .venv/bin/activate && python3 -m pytest {test_file} -v --tb=short",
    "test_dir": "tests",
    "naming_pattern": "test_{module}.py",
    "timeout_seconds": 30,
    "run_full_suite": false
  }
}
```

**Key behaviors:**
- `run_full_suite: false` — only runs the associated test file (fast feedback). Coder can still run the full suite manually.
- If the test file doesn't exist, skip silently (no punishment for writing code without tests yet).
- Timeout protection — if tests hang, kill after 30 seconds and report the timeout.
- Results appended to tool output, not as a separate message. Coder sees them as part of the write_file response.

**Why not run the full suite every time?** Speed. A full suite might take 30+ seconds. Running just the associated test file gives sub-second feedback. The prompt already instructs Coder to run the full suite before declaring completion — this layer catches the most common failure mode (write code, break the adjacent test) without the latency penalty.

### Relationship to Layers 1 & 2

- Layer 1 (bug journal) would note patterns like "Coder broke test_watcher.py while fixing watcher.py" if it happens repeatedly
- Layer 2 (project rules) tells this layer where to find tests and how to run them
- This layer (Layer 3) makes it structurally impossible for Coder to "forget" to run tests

### Files to Create/Modify

- Modify: `agent/enforcement.py` — add `_check_tests()` tier
- Modify: `agent/enforcement.py` — update `check()` to run test tier for `.py` files
- Create: template `.crabcakes/enforcement.json` with test configuration

### Success Criteria

- After Coder writes a Python file, the associated test file runs automatically
- Test results appear in Coder's tool output within seconds
- Coder can see and react to test failures before reporting completion
- No performance degradation for non-Python files or projects without tests

---

## Layer 4: Structured Feedback Protocol

### What

A standardized, machine-parseable format for adversarial audit reports. When Qaster reviews Coder's work, the feedback follows a consistent structure that can be programmatically collected and analyzed.

### Why

Currently, when Qaster sends bug reports to Coder, they're written in natural language — conversational prose describing the problem. This works for the immediate fix but is lost after the conversation scrolls away. The feedback can't be:
- Automatically added to the bug journal (Layer 1)
- Analyzed for patterns by the dream layer (Layer 5)
- Used as training signal for prompt evolution

A structured format makes feedback a **first-class data artifact** in the system.

### How It Works

**Audit report format (embedded in agent messages):**

```
## Audit Report
**Task:** Task 7 — Install script
**File:** install.sh:57
**Severity:** bug (must-fix)
**Bug:** sed replaces all "python3" including inside venv path
**Expected:** .venv/bin/python3 stays intact
**Actual:** .venv/bin/.venv/bin/python3 (double-nested)
**Root cause:** sed expression matches all occurrences of "python3" substring
**Fix:** Remove the sed python3 replacement line entirely
**Pattern:** sed-overmatch
**Tests:** bash -n install.sh (syntax), manual verification of generated paths
```

**Fields explained:**
- `Task` — which task triggered this
- `File` — file path and line number
- `Severity` — `bug` (must fix), `issue` (should fix), `suggestion` (nice to have)
- `Bug` — one-sentence description
- `Expected` vs `Actual` — concrete observable behavior
- `Root cause` — why it happened (this is the gold for learning)
- `Fix` — what to do about it
- `Pattern` — categorization tag (matches Layer 1's pattern tags)
- `Tests` — how to verify the fix

**Integration points:**

1. **Agent command handler** — When Qaster uses `ask @Coder "..."` with an audit report, the command handler detects the structured format and:
   - Extracts the report
   - Appends it to `.crabcakes/coder-bugs.md` as a new bug journal entry (Layer 1 integration)
   - Passes the message to Coder unchanged

2. **Review log** — `.crabcakes/review-log.jsonl` — append-only log of all structured audit reports. Each line is a JSON object with the report fields plus timestamps and task metadata. This is the raw data that Layer 5 (dream consolidation) will consume.

### Relationship to Previous Layers

- Layer 1 (bug journal) is automatically populated from structured reports
- Layer 2 (project rules) can be updated if a pattern recurs frequently
- Layer 3 (auto-test) is referenced in the `Tests` field — the reviewer specifies how to verify
- This layer produces the data that makes Layer 5 possible

### Files to Create/Modify

- Modify: `ui/handlers/agent_command_handler.py` — detect structured audit format in messages
- Create: `.crabcakes/review-log.jsonl` — append-only audit report log
- Create: parsing utility in `utils/audit_parser.py` — extract structured reports from message text

### Success Criteria

- Structured audit reports are detected and parsed from agent messages
- Bug journal entries are auto-created from structured reports
- Review log accumulates a queryable history of all code review feedback
- Coder receives the structured report and can act on it

---

## Layer 5: Dream Consolidation

### What

An autonomous process that runs during idle time, analyzes accumulated review feedback, and evolves Coder's system prompt and bug journal. Coder gets better while everyone is asleep.

This is the experimental layer. It depends on Layers 1-4 being operational and having accumulated enough data to be useful.

### Why

Manual prompt updates got us from 3-round fixes to 1-round fixes. But someone (Qaster or Captain) still has to observe patterns, write rules, and update files. The self-evolving agent research shows this can be automated:

- OpenAI's cookbook demonstrates the loop works: grade → meta-prompt → rewrite
- Imbue's evolver shows failure-driven mutations are dramatically more effective than random ones
- Hermes Agent identifies the "instructions layer" as the sweet spot for optimization

Our unique advantage: **We already have a multi-agent adversarial review loop.** The review feedback from Layer 4 is a ready-made evaluation signal. No custom evaluation harness needed — the adversarial review IS the evaluation.

### How It Works

**Trigger:** Cron job, runs during idle time (configurable — suggested: nightly at 2 AM, or on-demand when Coder hasn't been used for 4+ hours).

**Phase 1: Gather**
- Read `.crabcakes/review-log.jsonl` — all structured audit reports since last dream cycle
- Read `.crabcakes/coder-bugs.md` — current bug journal
- Read `prompts/system/coder.md` — current system prompt
- Read `.crabcakes/coder-rules.md` — current project rules
- Read recent Coder session transcripts (if available via CrabCakes session history)

**Phase 2: Analyze**
- LLM call with a "dream analysis" prompt
- Input: all gathered data
- Task: identify patterns in the review feedback
  - "Coder has made 3 mock-truthiness bugs across 2 projects"
  - "Coder consistently forgets to activate venv in crabwatch"
  - "Partial test runs are no longer happening (fixed by prompt update)"
- Output: structured analysis with pattern clusters, frequency counts, and suggested actions

**Phase 3: Synthesize**
- For recurring patterns: generate new Common Pitfall entries for `coder.md`
- For project-specific patterns: generate new gotchas for `coder-rules.md`
- For resolved patterns: mark as "addressed" in the bug journal
- For novel patterns: create new bug journal entries with higher-level insights

**Phase 4: Write**
- Update `.crabcakes/coder-bugs.md` — add synthesized entries, prune resolved ones
- Update `.crabcakes/coder-rules.md` — add project-specific gotchas
- Potentially update `prompts/system/coder.md` — add new Common Pitfalls (requires approval — see safety below)

**Phase 5: Verify**
- If `coder.md` was modified, run the CrabCakes test suite to ensure nothing broke
- If `coder-rules.md` was modified, verify the format is parseable
- Log the dream cycle results to `.crabcakes/dream-log.jsonl`

### Safety Mechanisms

Dream consolidation modifies Coder's operating instructions. This needs guardrails:

1. **Human approval for prompt changes.** The dream can propose changes to `coder.md`, but they don't take effect until a human (Captain or Qaster) reviews and approves. Store proposed changes in `.crabcakes/dream-proposals/` with timestamps.

2. **Bug journal and project rules are auto-applied.** These are project-scoped, git-tracked, and easily revertable. Lower risk than prompt changes.

3. **No deletion without approval.** The dream can mark entries as "possibly stale" but cannot delete from the bug journal without human confirmation.

4. **Idempotency.** Running the dream twice with the same input data should produce the same output. The review-log has timestamps — the dream only processes entries since the last dream cycle.

5. **Size management.** If the bug journal exceeds 50 entries, the dream should:
   - Cluster related entries
   - Generate a synthesized "meta-lesson" entry
   - Archive the individual entries to `.crabcakes/coder-bugs-archive.md`
   - Keep the meta-lesson in the active journal

### The "Dream Analysis" Prompt

This is the core LLM call. It should be a separate prompt template stored at `prompts/system/dream-analysis.md`:

**Purpose:** Given a collection of bug reports, review feedback, and the current system prompt, identify patterns and suggest improvements.

**Input:**
- Current `coder.md` Common Pitfalls section
- Current bug journal entries
- New review-log entries since last dream
- Current project rules

**Output format (structured JSON):**
```json
{
  "patterns": [
    {
      "pattern": "mock-truthiness",
      "frequency": 3,
      "first_seen": "2026-05-18",
      "projects": ["crabwatch"],
      "status": "active|resolved|evolving",
      "suggested_action": "add_to_coder_md",
      "proposed_pitfall": "When checking if a value exists, verify its TYPE..."
    }
  ],
  "proposals": {
    "coder_md_additions": ["..."],
    "coder_rules_additions": ["..."],
    "bug_journal_updates": ["..."],
    "bug_journal_prune": [1, 5, 12]
  }
}
```

### Implementation as a Cron Job

The dream runs as an OpenClaw cron job using an isolated agent session:

- **Schedule:** Nightly at 2 AM, or on-demand
- **Type:** `agentTurn` in an isolated session
- **Tools needed:** `read_file`, `write_file`, `edit_file`, `exec_command`
- **Model:** Could use a cheaper/faster model for analysis — this doesn't need Coder-level intelligence
- **Delivery:** Announce results to the main session (notify Qaster or Captain)

### Relationship to All Previous Layers

This layer is the capstone:
- **Layer 1 (Bug Journal):** Dream reads it to understand history, writes to it with new synthesized entries
- **Layer 2 (Project Rules):** Dream updates project rules with codebase-specific insights
- **Layer 3 (Auto-Test):** Dream can identify patterns in test failures (e.g., "Coder keeps breaking test_watcher.py")
- **Layer 4 (Structured Feedback):** Dream's primary data source — the review-log provides the raw signal for pattern analysis

### Files to Create/Modify

- Create: `prompts/system/dream-analysis.md` — the dream analysis prompt
- Create: `utils/dream_engine.py` — orchestration logic (gather → analyze → synthesize → write → verify)
- Create: `.crabcakes/dream-log.jsonl` — log of dream cycle results
- Create: `.crabcakes/dream-proposals/` — directory for proposed prompt changes awaiting approval
- Modify: `utils/prompt_loader.py` — if needed for any new injection points

### Success Criteria

- Dream cycle runs successfully on a cron schedule
- Produces meaningful pattern analysis from accumulated review data
- Generates actionable proposals for prompt/rules improvements
- Does not break existing functionality when run
- Human approval gate works for sensitive changes
- Bug journal stays manageable in size through pruning and synthesis

---

## Implementation Sequence

### Phase A: Foundation (Layers 1 + 2) — ~2 hours

**Why together:** Both modify the same file (`prompt_loader.py`) and both add context injection. Do them in one pass.

1. Design the bug journal entry format and create a template `.crabcakes/coder-bugs.md`
2. Design the project rules format and create a template `.crabcakes/coder-rules.md`
3. Modify `utils/prompt_loader.py` to inject both files into Coder's context
4. Populate initial bug journal with the 3 bugs from crabwatch task 5
5. Create `coder-rules.md` for the crabwatch project
6. Test: Open crabwatch in CrabCakes, send Coder a task, verify it sees the bug journal and project rules in its context

**Checkpoint:** Run the crabwatch watcher tests with Coder having the new context. Does Coder's behavior improve on similar tasks?

### Phase B: Enforcement (Layer 3) — ~3 hours

1. Design the test tier configuration schema
2. Implement `_check_tests()` in `agent/enforcement.py`
3. Wire test tier into `check()` flow for Python files
4. Create template `enforcement.json` with test configuration
5. Test: Have Coder write a Python file in crabwatch with an intentional bug. Verify the associated test runs and the failure appears in Coder's tool output.

**Checkpoint:** Run a full Coder task cycle. Does the auto-test catch regressions before Coder reports done?

### Phase C: Structured Communication (Layer 4) — ~3 hours

1. Define the audit report format precisely (field names, required vs optional, severity levels)
2. Implement `utils/audit_parser.py` — extract structured reports from message text
3. Modify `agent_command_handler.py` — detect and process structured reports in agent messages
4. Implement auto-population of bug journal from structured reports
5. Create `.crabcakes/review-log.jsonl` and implement append logic
6. Test: Qaster sends a structured audit report to Coder. Verify it appears in the bug journal and review log.

**Checkpoint:** Run a full adversarial review cycle. Verify the feedback flows through the system correctly.

### Phase D: Dream (Layer 5) — ~1 day (prototype), ongoing refinement

1. Write the dream analysis prompt (`prompts/system/dream-analysis.md`)
2. Implement `utils/dream_engine.py` — the orchestration logic
3. Set up the cron job for nightly execution
4. Implement safety mechanisms (approval gates, pruning, idempotency)
5. Test with accumulated data from Layers 1-4
6. Iterate on the dream analysis prompt based on output quality

**Checkpoint:** After a week of Coder working with Layers 1-4 active, run the dream manually. Review the proposals. Are they meaningful? Do they improve Coder's behavior?

---

## Testing Strategy

Each layer has its own testing approach:

### Layer 1 & 2 — Context Injection Tests
- Unit test: `prompt_loader.py` correctly reads and injects bug journal and project rules
- Integration test: Coder's assembled prompt includes the project-specific sections
- Behavioral test: Coder references bug journal entries when encountering similar situations

### Layer 3 — Enforcement Tests
- Unit test: `_check_tests()` finds the right test file, runs it, parses results
- Integration test: Writing a Python file triggers the test tier
- Edge cases: What if no test file exists? What if the test file is malformed? What if tests timeout?

### Layer 4 — Structured Feedback Tests
- Unit test: `audit_parser.py` correctly extracts structured reports from various message formats
- Integration test: Structured report in an agent message gets processed and logged
- Edge cases: Malformed reports, reports mixed with prose, reports with special characters

### Layer 5 — Dream Tests
- Unit test: Dream engine gathers data from the right sources
- Integration test: Dream cycle runs end-to-end on test data
- Safety test: Proposed changes don't break existing functionality
- Quality test: Human evaluation of dream proposals — are they meaningful?

---

## Risks and Mitigations

### Token Budget
Bug journal + project rules + enforcement results add tokens to every Coder turn. If the journal grows large, context window pressure increases.
**Mitigation:** Size caps, pruning, and synthesis. Active journal stays under 50 entries. Archive older ones.

### Overfitting
Coder might overfit to specific bug patterns and become too cautious — refusing to make changes because "it looks like Bug #7."
**Mitigation:** The bug journal is advisory context, not hard rules. The Common Pitfalls section should describe patterns, not prescribe specific code patterns.

### Prompt Drift (Layer 5)
Dream consolidation modifies Coder's system prompt. Over time, accumulated changes could drift from the original intent.
**Mitigation:** Human approval gate for prompt changes. Git-tracked — any change can be reverted. Version the prompt and keep changelogs.

### False Patterns
The dream might identify patterns that aren't real — coincidental correlations in a small sample.
**Mitigation:** Require minimum frequency (3+ occurrences) before promoting to a rule. Human review for all proposals.

---

## Future Possibilities (Out of Scope for This Proposal)

These ideas emerged during research but are not part of the current plan:

1. **Cross-project learning** — Bug journals from different projects feeding into a global knowledge base. Coder learns from mistakes made on ANY project, not just the current one.

2. **Prompt A/B testing** — Run Coder with different prompt variants on similar tasks and measure which produces fewer bugs. Requires a task evaluation harness.

3. **Model-specific adaptation** — If CrabCakes switches Coder's underlying model, the dream layer could re-optimize the prompt for the new model's tendencies.

4. **Review quality scoring** — Rate the quality of adversarial reviews themselves. Are Qaster's reviews catching real bugs or being too nitpicky? The review-log enables this analysis.

5. **Coder writes its own rules** — Instead of a separate dream agent, Coder itself proposes rule changes at the end of each task ("I noticed I made X mistake — should I add this to my rules?"). This is simpler but risks confirmation bias.

---

## Appendix: Existing Codebase Context

### Files That Will Be Modified

| File | Purpose | Layers |
|------|---------|--------|
| `utils/prompt_loader.py` | System prompt assembly | 1, 2 |
| `agent/enforcement.py` | Post-write verification | 3 |
| `ui/handlers/agent_command_handler.py` | A2A command processing | 4 |
| `prompts/system/coder.md` | Coder's base system prompt | 1, 5 |

### Files That Will Be Created

| File | Purpose | Layer |
|------|---------|-------|
| `.crabcakes/coder-bugs.md` | Per-project bug journal | 1 |
| `.crabcakes/coder-rules.md` | Per-project coder rules | 2 |
| `.crabcakes/enforcement.json` | Enforcement configuration | 3 |
| `utils/audit_parser.py` | Structured report extraction | 4 |
| `.crabcakes/review-log.jsonl` | Append-only review history | 4 |
| `prompts/system/dream-analysis.md` | Dream analysis prompt | 5 |
| `utils/dream_engine.py` | Dream orchestration | 5 |
| `.crabcakes/dream-log.jsonl` | Dream cycle results | 5 |
| `.crabcakes/dream-proposals/` | Pending prompt changes | 5 |

### Current State of Coder

- **System prompt:** `prompts/system/coder.md` (updated 2026-05-18 with Bug Fix Protocol + Common Pitfalls)
- **Tools:** read_file, write_file, edit_file, exec_command, list_files, search_files, web_search, web_fetch
- **Session key:** `special:coder`
- **Enforcement:** Syntax checks only (no test tier yet)
- **Context injection:** Architecture + context.md via prompt_loader
- **Known good behaviors:** Read-before-write, run-full-suite, report-with-evidence (post-prompt update)
- **Known failure modes:** Mock object handling, sed over-matching, partial test runs (all addressed in prompt, but could recur)

---

*"98.4% of a good agent is deterministic infrastructure. The other 1.6% is the model. We're building the 98.4%."*
— Paraphrased from VILA-Lab's analysis of Claude Code
