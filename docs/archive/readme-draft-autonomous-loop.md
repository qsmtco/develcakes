

## <img src="icons/emoji/refresh.png" width="80" height="80" alt="refresh" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Autonomous Coding Loop

**The flagship feature.** Three agents — a Supervisor, a Builder, and an Auditor — each with their own LLM, their own context window, and their own system prompt, work in a structured loop to implement specs autonomously. They don't share a brain. They don't share a session. They communicate through the same project chat you do — typing messages to each other like teammates.

You write the spec, the trio writes, audits, and ships the code. No human intervention needed inside the loop.

> Other tools give an agent a task and hope. CrabCakes gives three agents a **protocol** — a phased, audited, verified loop where no single agent's output is trusted without independent confirmation. The builder can't skip tests because the supervisor runs them independently. The supervisor can't skip the audit because the auditor is mandatory. The auditor can't fix code because that's the builder's job. **Separation of concerns, enforced structurally.**

### <img src="icons/emoji/team.png" width="80" height="80" alt="team" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> The Trio

Three specialized roles, each with its own prompt, its own responsibilities, and **its own LLM**:

| Role | Job | Prompt | What it owns | What it cannot do |
|------|-----|--------|--------------|-------------------|
| **Supervisor** | Read the spec, phase the work, delegate each phase, verify independently, write post-mortem | `implementationSupervisor.md` | Phasing, delegation, independent verification, post-mortem, commit/push | Write code, perform the adversarial audit, modify the spec mid-loop |
| **Builder** | Write code per phase instructions, report back with evidence | `steelFramedCodeWriter.md` | Reading phase instructions, writing code, running tests, reporting COMPLETENESS checklist | Phase the work, audit its own code, decide when the implementation is done |
| **Auditor** | Adversarial probe on every code-bearing turn — try to break the code before it ships | `adversarialDebugger.md` | 11-section adversarial probe, bug reporting in structured BUG format | Fix code, commit, decide phase completion, talk to the builder directly |

The Builder and the Auditor never communicate directly. All routing goes through the Supervisor. This isn't a convention — it's a **structural guarantee** that prevents collusion and groupthink.

### <img src="icons/emoji/network.png" width="80" height="80" alt="network" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Three Separate Brains

Every agent in the loop is a fully independent entity:

- **Separate LLM.** Each agent can run on a different model from a different provider. GPT-4o writes the code, Claude audits it, MiniMax supervises the whole thing.
- **Separate context window.** Each agent has its own conversation history. They don't see each other's scratch pad, reasoning chains, or tool outputs. The only thing that crosses between them is what gets typed into the chat.
- **Separate system prompt.** Each role loads its own instructions — the builder loads `steelFramedCodeWriter.md`, the auditor loads `adversarialDebugger.md`, the supervisor runs on `implementationSupervisor.md`. They never read each other's prompts.
- **Communicating via chat.** Agents talk to each other through the same project group chat you see. The supervisor types a delegation message, the builder reads it and responds, the auditor reads the response and posts its probe. **The chat is the only bridge between them** — exactly how human teammates collaborate.

```
  ┌───────────────────┐  chat  ┌───────────────────┐  chat  ┌───────────────────┐
  │    Supervisor     │ ──→    │      Builder      │ ──→    │      Auditor      │
  │                   │ ←──    │                   │ ←──    │                   │
  │  own LLM          │        │  own LLM          │        │  own LLM          │
  │  own context      │        │  own context      │        │  own context      │
  │  own system prompt│        │  own system prompt│        │  own system prompt│
  │                   │        │                   │        │                   │
  │  e.g. MiniMax M3  │        │  e.g. GPT-4o      │        │  e.g. Claude 3.5  │
  └───────────────────┘        └───────────────────┘        └───────────────────┘

         ↑ the only connection between them is what they type to each other ↑
```

This is what makes the loop fundamentally different from a single agent with multiple "hats" or a multi-turn conversation in one context window:

| | Single agent, multi-turn | **Three agents in the loop** |
|---|---|---|
| **Context** | One shared window — everything bleeds together | Three isolated windows — no context bleed |
| **Model** | One model — same blind spots everywhere | Three different models — different failure modes |
| **Self-audit** | Can it check its own work? (Hint: no) | Builder literally cannot audit — different agent, different model |
| **Communication** | Internal — thoughts are shared | External — typed messages, visible in the feed |
| **Human visibility** | You see one thread | You see every message between every agent |

**Why this matters:** A single LLM has shared blind spots. If the same model writes the code and audits the code, it will miss the same bugs in both passes — it literally cannot catch its own assumptions. Three different models from three different families bring different training data, different reasoning patterns, and different failure modes. Where GPT-4o might hallucinate an API, Claude might catch it. Where Claude might miss a type confusion, MiniMax might flag it. **Diversity is the defense — and separate contexts are what make the diversity real.**

A typical strong configuration:

| Role | Why this model | What it's good at |
|------|---------------|-------------------|
| **Supervisor** | MiniMax M3 or DeepSeek | Long-context reasoning, planning, reading specs and architecture docs |
| **Builder** | GPT-4o or MiniMax M2 | Code generation, following structured instructions, writing clean diffs |
| **Auditor** | Claude 3.5 Sonnet or DeepSeek | Adversarial reasoning, finding edge cases, challenging assumptions |

Configure per-agent in `~/.config/crabcakes/agents/`:

```yaml
# ~/.config/crabcakes/agents/supervisor.yaml
provider: openai-compatible
model: minimax/MiniMax-M3
# ... system prompt, tools, etc.

# ~/.config/crabcakes/agents/coder.yaml
provider: openai
model: gpt-4o
# ...

# ~/.config/crabcakes/agents/debugger.yaml
provider: anthropic
model: claude-3-5-sonnet
# ...
```

> **The principle:** never let the same brain write and audit the same code. If you only have one API key, the loop still works with a single model — but you lose the diversity advantage. Three models from three families is the gold standard.

### <img src="icons/emoji/flow.png" width="80" height="80" alt="flow" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> How the Loop Works

```
                 ┌────────────────────────────────────────────┐
                 │   You (the Captain)                        │
                 │   write a spec or feature request          │
                 └──────────────────┬─────────────────────────┘
                                    │
                                    ▼
                 ┌────────────────────────────────────────────┐
                 │   Supervisor                               │
                 │                                            │
                 │  1. Read spec + ARCHITECTURE.md             │
                 │  2. Phase the work (1-3 files per phase)    │
                 │  3. Write phase-instructions file to disk   │
                 │  4. Delegate build: /ask @Builder           │
                 │  5. Builder returns code + evidence         │
                 │  6. Delegate audit: /ask @Auditor           │
                 │  7. Auditor returns bug report?             │
                 │     ├─ Yes → route bugs to Builder → fix    │
                 │     │         (loop to step 5)              │
                 │     └─ No  → independent verification       │
                 │              (run tests, read diffs, grep)  │
                 │  8. All phases done?                        │
                 │     ├─ No  → next phase (loop to step 3)    │
                 │     └─ Yes → post-mortem + commit + report │
                 └──┬──────────────────┬──────────────────────┘
                    │ /ask (build)      │ /ask (audit)
                    ▼                   ▼
                 ┌──────────────┐  ┌──────────────────────┐
                 │   Builder    │  │      Auditor         │
                 │              │  │                      │
                 │ Reads phase  │  │ Loads adversarial-   │
                 │ instructions │  │ Debugger.md fresh    │
                 │ Writes code  │  │ each turn. Works     │
                 │ Reports back │  │ through 11 sections  │
                 │ w/ evidence  │  │ Reports bugs in BUG  │
                 │ Fixes bugs   │  │ #[N] format. Does    │
                 │ routed back  │  │ NOT fix, NOT commit  │
                 └──────────────┘  └──────────────────────┘
```

**One phase, end to end:**

```
  Supervisor  →  "Phase 3 of 7 — wire the chat handler"
      → /ask @Builder "please write per docs/specs/PHASE-3-INSTRUCTIONS.md"
      → Builder writes code, returns COMPLETENESS checklist + test output
      → /ask @Auditor "please audit — scope: handlers/chat_handler.py:120-180"
      → Auditor runs 11-section adversarial probe
      → Bug found? → route to Builder → fix → re-audit
      → Clean? → Supervisor runs tests independently, reads diff, greps
      → Sign off → next phase
```

### <img src="icons/emoji/checkmark.png" width="80" height="80" alt="checkmark" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Core Design Principles

**1. Never trust "done."**
The supervisor verifies everything independently. If the builder says "155/155 tests passing," the supervisor runs the tests. If the builder says "all files changed," the supervisor reads the diff. The builder's self-report is never the evidence.

**2. One phase at a time.**
Multi-file changes fail more often than single-file changes. Phases are kept to 1-3 files, independently verifiable. Integration steps get sub-phased even within a single file. A bug caught at phase N is a 5-minute fix; the same bug caught at phase N+3 is a half-day of cleanup.

**3. Audit every code-bearing turn.**
The adversarial audit is mandatory on every turn that touches code — pre-flight, between-phase, post-fix. Not optional. Not skippable. The auditor loads its prompt fresh each time and works through all 11 sections. Pattern-based spot checks don't count.

**4. The spec narrows the architecture. The code conforms to both.**
`ARCHITECTURE.md` is the floor — authoritative for both structure and behavior. The spec specializes it for one feature but may never override it. If the spec and architecture conflict, the architecture wins and the spec gets fixed.

**5. Separation of concerns is structural, not conventional.**
The builder cannot audit itself. The auditor cannot fix code. The supervisor cannot skip the audit. These aren't suggestions — they're enforced by the loop's delegation contracts. Each `/ask` payload is a contract with required parts; a delivery missing any of them is sent back.

### <img src="icons/emoji/blocks.png" width="80" height="80" alt="blocks" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> The Four-Prompt Composition

The loop is powered by four prompts that each own exactly one responsibility. No prompt duplicates another's content. The loop itself is defined by a fifth prompt that orchestrates them:

| Prompt | Loaded by | When | Responsibility |
|--------|-----------|------|----------------|
| `steelFramedSpecWriter.md` | Supervisor or Captain | Before the loop starts | Write the spec that specializes ARCHITECTURE.md for one feature |
| `steelFramedCodeWriter.md` | Builder | Every code-writing delegation | How to write code: read-before-touch, hard-part-first, verify-every-claim, wire-it-or-delete-it |
| `adversarialDebugger.md` | Auditor | Every code-bearing turn | How to audit: 11 sections from challenge-assumptions to verify-tests-match |
| `implementationSupervisor.md` | Supervisor | Continuous standing orders | How to phase, delegate, verify, manage context, and write the post-mortem |
| `implementationLoop.md` | — (meta) | Defines the loop itself | Loop diagram, role boundaries, authority hierarchy, entry/exit conditions, post-mortem format |

**Composable, not monolithic.** Each prompt is a self-contained module with a clear contract. Swap the builder's prompt for a domain-specific one. Swap the auditor for a security-focused probe. The loop's shape stays the same; the prompts are pluggable.

### <img src="icons/emoji/memo.png" width="80" height="80" alt="memo" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Post-Mortem as Institutional Memory

Every completed loop produces a **structured post-mortem** — not a summary, not a changelog, but an 11-section artifact that survives across loops and across agent pairs.

The post-mortem lives at `docs/post-mortems/YYYY-MM-DD-<FEATURE>-POST-MORTEM.md` and follows a mandatory format:

| Section | What it captures |
|---------|-----------------|
| **Code Quality Grade** | Scored rubric (correctness, architecture compliance, test coverage, docs, maintainability, DX) with letter grade |
| **What's Good** | Architectural wins, design decisions, defensive patterns — each cited to file:line |
| **What's Bad** | Code quality issues, scope creep, design debt — each with evolution path |
| **Bugs Found During Audit** | Full table: phase, severity, description, who found it (auditor probe vs. supervisor verification), who fixed it |
| **Process: What Worked** | Decisions that caught bugs early or saved time |
| **Process: What Didn't** | Failures, miscommunications, tooling issues — each with a lesson |
| **End-User Impact** | What a real user sees, clicks, and gets back — anchored to code paths |
| **Pre-Existing Issues** | Bugs found in the codebase during the loop that pre-existed and were intentionally left alone |
| **Evolution Suggestions** | Tier 2+ backlog with effort and impact estimates |
| **Lessons Learned** | Durable rules to carry forward into future loops' standing orders |
| **Sign-off** | Code pushed, verification run, captain notified |

> The post-mortem is the **only artifact that survives across loops.** Code gets refactored. Agents get cleared. Context windows reset. The post-mortem is how the system learns. Each one feeds the next loop's standing orders — recurring bug patterns become new entries in the builder's common pitfalls, process failures become new supervisor rules.

### <img src="icons/emoji/console.png" width="80" height="80" alt="console" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Authority Hierarchy

When sources disagree, the resolution order is strict:

```
   1. Captain's standing orders
      │   (highest — only the captain can override architecture)
      ▼
   2. ARCHITECTURE.md
      │   (the floor — authoritative for structure AND behavior)
      ▼
   3. The spec
      │   (narrows ARCHITECTURE.md for one feature; never contradicts it)
      ▼
   4. The code
          (the artifact; conforms to both architecture and spec)
```

If the spec contradicts `ARCHITECTURE.md`, the spec is wrong — fix the spec, don't bend the architecture. If the code contradicts either, the code is wrong — the auditor catches it, the builder fixes it.

### <img src="icons/emoji/key.png" width="80" height="80" alt="key" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> The `/ask` Protocol

Agents delegate to each other through the `/ask` slash command — the only sanctioned mechanism for inter-agent communication in the loop:

```
/ask @Builder "Phase 2 — implement the rate limiter. Please write per docs/specs/PHASE-2-INSTRUCTIONS.md"
/ask @Auditor "Please audit — scope: lib/ratelimit.py:1-85. Load adversarialDebugger.md fresh."
```

**Why a text command, not a tool call?** Because `/ask` routes through the same project chat that humans see. Every delegation, every audit, every bug report is **visible in the feed**. Nothing happens in a black box. You watch the trio work in real time, step in if needed, and let them run autonomously when you don't.

This is the key architectural decision: **the chat is the only bridge between agents.** There's no shared memory, no internal API, no hidden channel. The supervisor types a message, the builder reads it in its own separate session, responds in its own voice, and the auditor reads that response in its own separate session. Each agent reasons independently about what it received — it can't peek at the other's context, tools, or reasoning chain. **They collaborate the same way humans do: by talking to each other.**

**Context management:** The supervisor can reset an agent's conversation context with `/clear` between phases to prevent context bleed — but never mid bug-fix loop, because the builder needs the accumulated bug context to land the fix.

### <img src="icons/emoji/crosshair.png" width="80" height="80" alt="crosshair" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> When to Use the Loop

| Scenario | Use the loop? | Why |
|----------|--------------|-----|
| Multi-file feature implementation | **Yes** | Phased delegation + adversarial audit prevents compounding bugs |
| Bug fix touching 3+ files | **Yes** | The auditor catches regressions the builder wouldn't find alone |
| Refactor with behavioral changes | **Yes** | Phase-by-phase verification ensures no behavior drift |
| Quick 1-file fix | No — just ask Coder directly | The loop's overhead exceeds the task; direct delegation is faster |
| Exploratory prototyping | No | The loop is for verified, production-grade code, not spikes |
| Writing tests for existing code | Maybe | If the test surface is large, phase it. If it's one test file, direct ask |

### <img src="icons/emoji/stop.png" width="80" height="80" alt="stop" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Loop Stop Conditions

The supervisor aborts the loop and escalates to you when:

- The spec is fundamentally broken (self-contradictory, references nonexistent systems)
- The builder fails the same phase three times after full delegation cycles
- The auditor is unreachable for a full audit cycle (the supervisor cannot substitute its own audit)
- A pre-existing critical bug blocks the work
- You revoke authorization mid-loop

In all cases, the supervisor writes an abort note explaining why and what's needed to resume. You're never left wondering what happened.

---
