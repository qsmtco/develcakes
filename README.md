<div align="center">

<img src="icons/logo-rounded.png" alt="Develcakes" width="280">

# Develcakes:PDE

### The first **Project Development Environment**.

*Where your project is the chat. Where your feed is the dashboard. Where agents are teammates you manage.*

[![GTK4](https://img.shields.io/badge/GTK4-native-4a86cf?style=for-the-badge&logo=gtk&logoColor=white)](https://docs.gtk.org/gtk4/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776ab?style=for-the-badge&logo=python&logoColor=ffdd54)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e?style=for-the-badge)](LICENSE)
[![Tests: 3,200+](https://img.shields.io/badge/tests-3,200%2B-22c55e?style=for-the-badge&logo=pytest&logoColor=white)](tests/)
[![Handlers: 26](https://img.shields.io/badge/handlers-26-8b5cf6?style=for-the-badge)](ui/handlers/)
[![Loop: 3-Agent](https://img.shields.io/badge/loop-3--agent-f59e0b?style=for-the-badge)](#-autonomous-coding-loop)
[![Zero Stale Failures](https://img.shields.io/badge/test_suite-0_failures-f43f5e?style=for-the-badge&logo=checkmarx&logoColor=white)](tests/)

<br>

> *"Other tools make agents do things. Develcakes orchestrates them as a team."*

<br>

[Quick Start](#-quick-start) · [Features](#-whats-inside) · [Autonomous Loop](#-autonomous-coding-loop) · [Architecture](#-architecture) · [The PDE Thesis](#-why-a-pde)

</div>

---

## <img src="icons/emoji/crab.png" width="80" height="80" alt="crab" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> What is Develcakes?

**Develcakes is a native Linux desktop app that reimagines software development as a group chat — where some of your teammates happen to be AI agents.** (v2 hard fork of CrabCakes:PDE — v1 lineage and history preserved below.)

Open a project. The team appears. You type a message, it fans out to every member. Agents respond, collaborate, write code, review each other's work, and you see all of it happen in real time in a **social-media-style Project Feed**.

```
 Project: CargoAPI · 4 online

  You     │  @Coder — implement the rate limiter
  Coder   │  On it. Starting with the token bucket approach.
  QTR     │  I left a draft in lib/ratelimit.py from last week
  Coder   │  Good catch. Building on that.
  ─────────────────────────────────────────
  [type a message...]               <img src="icons/emoji/classic_mic.png" width="80" height="80" alt="classic_mic" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" />  <img src="icons/emoji/paperclip.png" width="80" height="80" alt="paperclip" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" />  <img src="icons/emoji/sparkle.png" width="80" height="80" alt="sparkle" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" />
```

**It runs standalone.** Two built-in coding agents — **Coder** and **Debugger** — work locally on your machine with full file access and shell execution. No cloud, no account, no API key required to start. Connect to [OpenClaw](https://github.com/openclaw/openclaw) and your remote agents join seamlessly — but you don't have to.

This is the first **PDE** — a *Project Development Environment*.

> Develcakes is not a harness. A harness wraps API calls. Develcakes is where AI and humans build software together. The **project** is the first-class citizen. Agents, humans, git, files, reviews — those all orbit the project.

---

## <img src="icons/emoji/sparkles.png" width="80" height="80" alt="sparkles" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> What's Inside

### <img src="icons/emoji/chat.png" width="80" height="80" alt="chat" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Project Group Chat

Every member — you, Coder, Debugger, your remote agents — shares a single conversation. Messages fan out. Responses route back. You see the whole picture. Per-tab routing means project discussions stay in project tabs and agent queries stay in agent tabs, automatically.

- **@mentions** that resolve to specific agents or broadcast to the whole project
- **Threaded conversation history** persisted per project
- **Streaming responses** with typewriter-style incremental rendering
- **HTML-native chat** — agents author replies as HTML (a whole-message ```html fenced block renders through a fail-closed agent-author policy: styled cards, flex layouts, details toggles); everything else renders markdown → sanitized HTML in the WebKit surface. A ```image fence with a local png/jpeg/gif/webp path shows the picture inline. No escaping theater for agent content, no raw-HTML risk for tool results.

### <img src="icons/emoji/robot.png" width="80" height="80" alt="robot" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Built-In Coding Agents

**Coder** reads your architecture docs, your project context, your conventions. Writes production code. Every file change gets a diff card in the feed.

**Debugger** attaches to failed sessions, analyzes the error, proposes a fix, validates it against your test suite.

Both run locally against **OpenAI**, **MiniMax**, **Anthropic**, or any OpenAI-compatible API. No gateway required.

> Built-in agents are just YAML files in `prompts/default_agents/`. Drop a new one in `~/.config/develcakes/agents/` and it appears in your team — with your choice of provider, model, system prompt, emoji, color, and tool set. No code required. No fork needed.

### <img src="icons/emoji/shield.png" width="80" height="80" alt="shield" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Post-Write Enforcement

Every agent write goes through a **3-tier verification pipeline** automatically — not a prompt request, a structural guarantee:

| Tier | What runs | When |
|------|-----------|------|
| **Syntax guard** | `py_compile` · `node --check` · syntax-specific shell | Immediately on write |
| **Test runner** | Detects your framework (pytest, npm test, go test) · runs relevant tests | After syntax passes |
| **Lint check** | Runs your configured linter on changed files | After tests pass |

If anything fails, the agent gets the error and self-corrects. **No prompts, no reminders, no human intervention.** Infrastructure enforces what prompts cannot.

### <img src="icons/emoji/newspaper.png" width="80" height="80" alt="newspaper" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Project Feed

Every significant action generates a **card** — not a wall of text, not a terminal dump. A scannable, actionable signal. Eleven typed cards, each with a distinct visual signature:

| Card | What it signals |
|------|----------------|
| `git_commit` | Agent committed a change — message + author + SHA |
| `diff` | File edited — +/− counts, full unified diff, one-click review |
| `file_created` | New file landed in the project |
| `file_modified` | Existing file touched |
| `file_deleted` | Something was removed |
| `dir_created` | Directory added |
| `dir_deleted` | Directory removed |
| `task` | Created, started, completed, or blocked |
| `agent_action` | Consultation opened/closed, key decision logged |
| `audit_report` | Post-write enforcement result — syntax, test, lint verdict |
| `system` | Lifecycle events — connect, disconnect, agent join |

Agents emit cards. You read the feed. **Nothing slips through.**

### <img src="icons/emoji/magnifier.png" width="80" height="80" alt="magnifier" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Git-Backed Code Review

Every agent write is checkpointed before it reaches your codebase:

1. **Checkpoint** — `git add -A` against the last clean state
2. **Diff** — full unified diff, file by file, hunk by hunk
3. **Accept / Reject** — Accept commits. Reject resets to the checkpoint SHA.

Agents keep working while you review. Multiple agents can write simultaneously — each on its own branch. **Nothing touches your code until you approve it.**

### <img src="icons/emoji/bubbles.png" width="80" height="80" alt="bubbles" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Activity Bubbles

Real-time visibility into what your agents are doing. Centered, pill-shaped indicators appear as tools run:

```
              ┌──────────────┐
              │  thinking...  │
              └──────────────┘
                 ┌─────────┐
                 │  search  │
                 └─────────┘
              ┌──────────────┐
              │  read  83ms  │
              └──────────────┘
```

Eight activity types: lifecycle, tool start, tool end, tool error, plan updates, approval requests, command output, file patches. Color-coded by status. No emoji soup. No visual noise.

### <img src="icons/emoji/handshake.png" width="80" height="80" alt="handshake" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Agent-to-Agent Collaboration

Agents consult each other through the same command system you use:

```
  Coder     │  `ask @Debugger — is this edge case in the auth flow handled?
  Debugger  │  Looking... no, there's a gap when the token is expired
             │  but refresh hasn't failed.
  Coder     │  Good catch. Fixing now.
```

The `@mention` system, `ask`, `delegate`, `stop`, and `tell` commands work identically for humans and agents. Agents collaborate and you watch it happen. **The collaboration layer is uniform.**

### <img src="icons/emoji/clipboard.png" width="80" height="80" alt="clipboard" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Task System

Full task management baked into the project feed:

| Command | Result |
|---------|--------|
| `` `task @Coder — implement auth middleware `` | Creates + assigns task |
| `` `start #3 `` | Begins work, emits card to feed |
| `` `done #3 — all tests passing `` | Closes with notes |
| `` `blocked #7 — waiting on API spec `` | Escalates to feed |
| `` `tasks `` | Full project task board |

Every action emits a structured card. Full history, always visible.

### <img src="icons/emoji/padlock.png" width="80" height="80" alt="padlock" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Exec Approval Gate

Shell commands go through an approval gate. Dangerous operations — file deletions, system changes, network calls — surface for your review before execution. You see the exact command, the host, and the reason. **One click to approve or deny.** No silent shell access. No surprise side effects.

### <img src="icons/emoji/sparkle.png" width="80" height="80" alt="sparkle" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Prompt Improvement

Every prompt in the library can be refined before loading. The built-in prompt improver rewrites your template with better structure, clearer instructions, and sharper edge cases. Templates support variables (`{{PROJECT_NAME}}`, `{{TEAM}}`, `{{GIT_STATE}}`) that fill from project context.

### <img src="icons/emoji/studio_mic.png" width="80" height="80" alt="studio_mic" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Voice Input

Push-to-talk via **faster-whisper** — a Python-native speech-to-text engine. No separate CLI binary, no cloud, no latency. Hold a key, speak, release — your words land in the input box. Model size is configurable via `STT_MODEL_SIZE` (default: `tiny.en` for fastest CPU inference). Built for when you're mid-flow and reaching for the keyboard would break your concentration.

### <img src="icons/emoji/plug.png" width="80" height="80" alt="plug" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> MCP Server Integration

Connect any [Model Context Protocol](https://modelcontextprotocol.io/) server to your agents. GitHub, PostgreSQL, Sentry, Puppeteer, Filesystem, Memory — any MCP server becomes a tool library. Agents get structured, well-described tools with proper schemas instead of raw shell commands.

**What's implemented:**
- **stdio transport** — MCP clients launch as subprocesses; Develcakes talks to them over stdin/stdout
- **Tool discovery** — agents automatically discover all tools a server exposes (the Memory server exposes 9 tools: `create_entities`, `read_graph`, `search_nodes`, and more)
- **Hot-reload** — add or remove a server from an agent's config in the Edit Agent dialog; no restart needed. The runtime reconnects on the next message
- **Works for all agents** — any special agent (Coder, Debugger, Test Engineer, etc.) can be MCP-enabled per-agent in `~/.config/develcakes/agents/{agent}.yaml`

```yaml
# ~/.config/develcakes/agents/coder.yaml
mcp_servers:
  - memory      # Knowledge graph — 9 tools
  - filesystem  # Local file access
```

The **Memory server is verified working end-to-end** through the UI. Agents can create, query, and link entities in a persistent knowledge graph that survives across sessions.

### <img src="icons/emoji/folder.png" width="80" height="80" alt="folder" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Project Browser & Team Management

- **File tree** in the left panel with file-type icons, git status badges, size and modified-date columns, 6-mode sort (name, status, size, modified, type, depth), and live search/filter. Open any file, browse directories, see what's changed at a glance.
- **Project settings bar** — an actionable bar above the chat showing the project name, member count, active agent, file-change auto-accept level, and git branch. Click to cycle agents or toggle auto-accept. All state in one row, no hunting through menus.
- **Project creation** scaffolds `AGENTS.md` and `.crabcakes/` for you (per-project state dir keeps the v1 name until v2 self-hosts — the `.develcakes/` rename is the registered first post-MVP unit).
- **Membership toggles** — who do you need on this project? Add someone mid-sprint. Remove them when the work is done. Changes fan out immediately, no restart, no reconfigure.
- **Agent Discovery** — connect to an OpenClaw gateway and Develcakes pulls the full agent roster. Remote agents blend seamlessly into project group chats alongside your local Coder and Debugger. The split between local and remote is invisible to the user.

### <img src="icons/emoji/robot.png" width="80" height="80" alt="robot" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Remote-In from Your Phone

Connect the **Supervisor to your phone over Telegram** — click Connect in the toolbar and pair a bot in Settings. Your phone becomes a thin client: chat back and forth with the Supervisor, receive **exec-approval prompts as inline ✅/❌ buttons**, and run a small remote allowlist (`/status`, `/stop` with a typed confirmation, `/help`). No shell, no file access — the phone sees conversation and approvals only.

### <img src="icons/emoji/construction.png" width="80" height="80" alt="construction" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Custom Agent Builder

Coder and Debugger are just the starting point. Use the built-in **Agent Builder UI** to configure a new agent visually — pick a provider, model, system prompt role, emoji, color, tool set. Or drop a YAML file into `~/.config/develcakes/agents/`. **No code required. No fork needed.** Agents load at startup.

---

## <img src="icons/emoji/brain.png" width="80" height="80" alt="brain" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Self-Improvement System

Coder learns. Not metaphorically — **structurally.** A five-layer feedback engine that makes Coder measurably better every time it gets something wrong.

### Layer 1 — Bug Journal

Every mistake Coder makes is recorded in `.crabcakes/coder-bugs.md` — a per-project, git-tracked journal. Each entry captures the bug, the root cause, the fix, and the lesson. This isn't a generic pitfalls list. **It's Coder's actual scar tissue.**

```markdown
## Bug #7 — 2026-05-18 — watcher.py
**Task:** Fix moved event detection in DebouncedHandler
**Mistake:** Used `if dest_path is not None` — MagicMock objects are always truthy
**Fix:** Changed to `isinstance(dest_path, str) and dest_path`
**Pattern:** mock-truthiness
```

This file is injected into Coder's system prompt at the start of every task. The pattern tags (`mock-truthiness`, `sed-overmatch`, `partial-test-run`, `off-by-one`, `race-condition`, `type-confusion`) are the seed data that upper layers consume.

### Layer 2 — Project Rules

Like Claude Code's `CLAUDE.md` — a per-project rules file that tells Coder how this specific codebase is wired. Python version, venv activation, test naming conventions, known gotchas. Injected alongside the bug journal so Coder starts every session knowing the terrain.

### Layer 3 — Auto-Test Enforcement

**Not a prompt request. A structural guarantee.** After Coder writes any `.py` file, the system automatically finds and runs the associated test file, injects the result into Coder's tool output, and forces a response before the task can close. Coder physically cannot forget to run tests.

### Layer 4 — Structured Feedback Protocol

When a reviewer finds a bug, the report follows a machine-parseable format:

```
## Audit Report
**File:** install.sh:57
**Severity:** bug
**Bug:** sed replaces all "python3" including inside venv path
**Pattern:** sed-overmatch
```

This format auto-populates the bug journal, appends to the review log (`.crabcakes/review-log.jsonl`), and feeds directly into Layer 5. **Feedback becomes a data artifact, not a conversation that scrolls away.**

### Layer 5 — Dream Consolidation

The system runs a nightly autonomous cycle during idle time. It reads the accumulated bug journal, review log, and recent session data, identifies patterns across reviews, and evolves Coder's prompts accordingly:

- Recurring `mock-truthiness` bugs → new Common Pitfall entry in `coder.md`
- Repeated `sed-overmatch` → new gotcha in `coder-rules.md`
- Resolved patterns → archived, not deleted

Proposed changes to `coder.md` require human approval. Project rules and bug journal updates apply automatically. **The system gets smarter while everyone sleeps.**

> **Research basis:** This system draws from OpenAI's self-evolving agents cookbook (grade → meta-prompt → rewrite), Imbue's Darwinian evolver (failure-driven mutations outperform random perturbation by 2–3x), and VILA-Lab's finding that 98.4% of a capable agent is deterministic infrastructure, not the model itself.

---

## <img src="icons/emoji/refresh.png" width="80" height="80" alt="refresh" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Autonomous Coding Loop

**The flagship feature.** Three agents — a Supervisor, a Builder, and an Auditor — each with their own LLM, their own context window, and their own system prompt, work in a structured loop to implement specs autonomously. They don't share a brain. They don't share a session. They communicate through the same project chat you do — typing messages to each other like teammates.

You write the spec, the trio writes, audits, and ships the code. No human intervention needed inside the loop.

> Other tools give an agent a task and hope. Develcakes gives three agents a **protocol** — a phased, audited, verified loop where no single agent's output is trusted without independent confirmation. The builder can't skip tests because the supervisor runs them independently. The supervisor can't skip the audit because the auditor is mandatory. The auditor can't fix code because that's the builder's job. **Separation of concerns, enforced structurally.**

### <img src="icons/emoji/team.png" width="80" height="80" alt="team" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> The Trio

Three specialized roles, each with its own prompt, its own responsibilities, and **its own LLM**:

| Role | Job | What it owns | What it cannot do |
|------|-----|--------------|-------------------|
| **Supervisor** | Read the spec, phase the work, delegate each phase, verify independently, write post-mortem | Phasing, delegation, independent verification, post-mortem, commit/push | Write code, perform the adversarial audit, modify the spec mid-loop |
| **Builder** | Write code per phase instructions, report back with evidence | Reading phase instructions, writing code, running tests, reporting COMPLETENESS checklist | Phase the work, audit its own code, decide when the implementation is done |
| **Auditor** | Adversarial probe on every code-bearing turn — try to break the code before it ships | 11-section adversarial probe, bug reporting in structured BUG format | Fix code, commit, decide phase completion, talk to the builder directly |

The Builder and the Auditor never communicate directly. All routing goes through the Supervisor. This isn't a convention — it's a **structural guarantee** that prevents collusion and groupthink.

### <img src="icons/emoji/network.png" width="80" height="80" alt="network" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Three Separate Brains

Every agent in the loop is a fully independent entity:

- **Separate LLM.** Each agent can run on a different model from a different provider. GPT-4o writes the code, Claude audits it, MiniMax supervises the whole thing.
- **Separate context window.** Each agent has its own conversation history. They don't see each other's scratch pad, reasoning chains, or tool outputs. The only thing that crosses between them is what gets typed into the chat.
- **Separate system prompt.** Each role loads its own instructions. They never read each other's prompts.
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

Configure per-agent in `~/.config/develcakes/agents/`:

```yaml
# ~/.config/develcakes/agents/supervisor.yaml
provider: openai-compatible
model: minimax/MiniMax-M3
# ... system prompt, tools, etc.

# ~/.config/develcakes/agents/coder.yaml
provider: openai
model: gpt-4o
# ...

# ~/.config/develcakes/agents/debugger.yaml
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
/ask @Builder "Phase 2 — implement the rate limiter per the phase instructions"
/ask @Auditor "Please audit — scope: lib/ratelimit.py:1-85"
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

## <img src="icons/emoji/rocket.png" width="80" height="80" alt="rocket" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Quick Start

### Prerequisites

```bash
# System packages (Ubuntu / Debian)
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 libgirepository1.0-dev

# Python packages
pip install pygobject websockets cryptography gitpython \
    tiktoken faster-whisper sentence-transformers httpx pyyaml
```

### Run

```bash
git clone https://github.com/qsmtco/develcakes.git
cd develcakes

# Launch — works immediately, no gateway required
python main.py

# Optional: connect to OpenClaw for remote agents
openclaw gateway start
# Then click Connect in the Develcakes toolbar
```

### First Steps

1. **Browse prompts** in the left sidebar — click any system prompt to load it
2. **Start a local conversation** — the Coder agent is ready out of the box
3. **Open a project** — click Projects tab → select a directory → the team assembles
4. **Try voice input** — push and hold the mic button, speak, release
5. **Add an MCP server** — right-click an agent card → Edit → check a server

---

## <img src="icons/emoji/gear.png" width="80" height="80" alt="gear" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `DEVELCAKES_PROJECTS_DIR` | `~/projects` | Root directory for projects |
| `DEVELCAKES_DEBUG` | _(off)_ | Set truthy for verbose logging |
| `DEVELCAKES_WEB_FETCH_RESTRICT` | _(off)_ | Restrict web fetch to allowlisted domains |
| `DEVELCAKES_INCLUDE_DOCS` | _(off)_ | Include docs/ in file context |
| `DEVELCAKES_NO_WEBKIT` | _(off)_ | Force the TextView fallback chat surface |
| `DEVELCAKES_MIGRATE_STORE` | `1` | JSON→store migration at launch (0 skips) |
| `DEVELCAKES_PROMPT_DEBUG` | _(off)_ | Dump the full composed prompt to stderr |
| `STT_MODEL_SIZE` | `tiny.en` | faster-whisper model size (tiny, base, small, ...) |

Every `DEVELCAKES_*` variable also answers to its legacy `CRABCAKES_*` name
for one release (config migration included: your v1 `~/.config/crabcakes/`
is copied to `~/.config/develcakes/` on first launch). `STT_MODEL_SIZE` is
not part of the renamed family — it keeps its name.

Agent configs live in `~/.config/develcakes/`. LLM provider settings in `agent.json`. MCP server registry in `mcp-servers.json`. Everything is plain files — version-controllable, diffable, greppable.

---

## <img src="icons/emoji/construction.png" width="80" height="80" alt="construction" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Architecture

**Strict layer separation.** The codebase is readable, testable, and trustworthy:

```
┌─────────────────────────────────────────────────────────┐
│  ui/                                                    │
│  GTK4 widgets · handlers · views                        │
│  Never imports gateway/ or models/                      │
├─────────────────────────────────────────────────────────┤
│  gateway/        models/           agent/               │
│  WebSocket       Pure data         Local agent          │
│  v3 device auth  No UI deps        runtime              │
│  No UI deps      No gateway deps   No UI deps           │
├─────────────────────────────────────────────────────────┤
│  utils/                                                 │
│  Pure Python · file I/O · prompts · STT · MCP · config  │
└─────────────────────────────────────────────────────────┘
```

### The 26-Handler Pattern

Every handler follows the same pattern: **receives dependencies via setters, never imports from other handlers.** `window.py` is the composition root — it creates everything and wires the callbacks. No circular dependencies. No hidden state.

```
ui/handlers/
├── activity_handler.py            6-state activity machine
├── activity_wiring_handler.py     Activity state → widget wiring
├── agent_builder_handler.py       Custom agent config UI
├── agent_command_handler.py       Audit reports · enforcement
├── agent_list_handler.py          Agent roster
├── agent_runtime_handler.py       Local agent bridge
├── chat_handler.py                Send · fan-out · routing
├── chat_render_handler.py         Markdown · bubbles · streaming
├── collab_handler.py              ask / delegate / stop / tell
├── command_handler.py             Backtick command parser
├── crabwatch_handler.py           File system watcher
├── feed_handler.py                Feed card lifecycle
├── file_tree_handler.py           File tree sort · filter · prefs
├── forward_handler.py             Agent-to-agent forwarding
├── input_toolbar_handler.py       Chat input controls
├── media_handler.py               STT + prompt improvement
├── project_handler.py             Project open/close/create · onboarding
├── project_list_handler.py        Project browser
├── prompts_handler.py             Prompt library
├── review_handler.py              Review lifecycle
├── session_handler.py             Session management
├── settings_handler.py            Settings dialog
└── task_handler.py                Task CRUD
```

### Project Layout

```
develcakes/
├── main.py                          # Entry point
├── models/                          # Pure data — no UI deps
│   ├── agents.py                    # Agent manager · colors · sessions
│   ├── routing.py                   # Session → project routing
│   ├── command.py                   # Command parsing · registry
│   ├── conversation.py              # Conversation + Message dataclasses
│   ├── conversation_snapshot.py     # Serializable snapshots
│   ├── feed_card.py                 # Feed card data
│   ├── activity.py                  # Activity bubble data
│   ├── review_state.py              # Review session state
│   ├── streaming.py                 # Streaming bubble state
│   ├── colors.py                    # Agent color rotation
│   ├── providers.py                 # Provider config data
│   └── team.py                      # Team membership
├── agent/                           # Local agent runtime
│   ├── runtime.py                   # Tool loop · streaming · cost tracking
│   ├── tools.py                     # 8 built-in tools
│   ├── context.py                   # System prompt builder
│   ├── context_strategy.py          # Context-window budget strategy
│   ├── config.py                    # Provider config
│   ├── enforcement.py               # Post-write verification
│   ├── special_agents.py            # Coder + Debugger definitions
│   ├── tool_middleware.py           # Middleware chain (enforcement + stuck detection)
│   ├── callbacks.py                 # Typed callback protocols
│   ├── audit.py                     # AuditEntry + AuditLog
│   ├── persistence.py               # Conversation save/load
│   └── llm/                         # Provider adapters (extracted from runtime)
│       ├── protocol.py              # LLMProvider Protocol
│       ├── openai_provider.py       # OpenAI call + stream
│       ├── minimax_provider.py      # MiniMax call + stream
│       ├── anthropic_provider.py    # Anthropic call + stream
│       ├── registry.py              # Provider dispatch by caller key
│       ├── cost.py                  # Token cost calculation
│       ├── convert.py               # Message/tool format conversion (Anthropic)
│       ├── extractors.py            # Tool-call + usage extraction
│       └── streaming.py             # SSE parsing + SSL retry
├── utils/                           # Pure Python utilities
│   ├── escaping.py                  # Pango-aware XML escape (Pango surfaces)
│   ├── markdown.py                  # Markdown → Pango markup (Pango surfaces)
│   ├── block_parser.py              # Block-aware text chunking (render input)
│   ├── git_ops.py                   # GitPython wrapper
│   ├── diff_parser.py               # Unified diff parser
│   ├── prompt_loader.py             # System prompt composer
│   ├── stt.py                       # Voice input (faster-whisper)
│   ├── mcp_client.py                # MCP stdio transport
│   ├── mcp_config.py                # MCP server registry
│   ├── agent_defs.py                # User agent YAML loader
│   ├── feedback_processor.py        # Audit report processing
│   ├── review_log.py                # Review log writer
│   ├── project_awareness.py         # Project context scanner
│   ├── projects.py                  # Project CRUD
│   ├── improve.py                   # Prompt improvement API
│   ├── audit_parser.py              # Audit report parser
│   ├── workflow_state.py            # Task workflow state
│   ├── spellcheck.py                # Inline spellcheck
│   ├── syntax_highlight.py          # Code syntax highlighting
│   ├── file_icons.py                # File-type icon registry (60+ extensions)
│   ├── gtk_containers.py            # GTK4 container membership helper
│   ├── gtk_safe_link.py             # Safe label with link-scheme guard
│   ├── block_parser.py              # Code-block aware text chunking
│   ├── crabcard_parser.py           # Feed-card block extraction
│   ├── providers_store.py           # Provider config persistence
│   ├── conversation_store.py        # Conversation persistence
│   ├── favorites.py                 # Prompt favorites
│   └── feed_store.py                # Feed persistence
├── prompts/                           # System prompts & loop definitions
│   ├── system/                      # System prompt templates
│   │   ├── coder.md                 # Coder agent instructions
│   │   ├── debugger.md              # Debugger agent instructions
│   │   ├── collab.md                # A2A collaboration protocol
│   │   ├── project-onboarding.md    # New project interview
│   │   ├── project-awareness.md     # Project context injection
│   │   ├── code-review.md           # Review mode instructions
│   │   ├── improve.md               # Prompt improver system prompt
│   │   ├── default.md               # Default agent prompt
│   │   ├── supervisor.md            # Supervisor agent instructions
│   │   ├── cc-implementation.md     # Implementation loop context
│   │   ├── crabcakes-commands.md    # Slash-command reference
│   │   └── crabcakes-context.md     # Project awareness template
│   └── default_agents/              # Built-in agent YAMLs
│       ├── coder.yaml
│       ├── debugger.yaml
│       └── supervisor.yaml
├── .crabcakes/                      # Per-project config (git-tracked)
│   ├── project.md                   # Project manifest
│   ├── workflow.md                  # Phase history
│   ├── team.json                    # Team roster
│   ├── context.md                   # Session context (shared notepad)
│   ├── coder-bugs.md                # Coder bug journal (self-improvement)
│   ├── enforcement.json             # Per-project enforcement overrides
│   └── review-log.jsonl             # Review audit trail
├── tests/                           # 3,200+ tests · 130 test files
├── scripts/                         # Audit + maintenance scripts
└── docs/                            # Specs · post-mortems · research
```

---

## <img src="icons/emoji/test_tube.png" width="80" height="80" alt="test_tube" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Testing

```bash
pytest                    # run all tests
pytest tests/test_*.py    # run specific suite
pytest -x                 # stop on first failure
pytest -k pattern         # filter by name
```

**3,200+ tests** across 130 test files covering all handlers, models, rendering, MCP, the agent runtime, and the full event pipeline. **Zero stale failures** — the suite is kept clean. When an API changes, the tests change with it in the same commit.

---

## <img src="icons/emoji/target.png" width="80" height="80" alt="target" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> Why a PDE?

Every tool today makes the same trade-off: agents work alone (fast, uncontrolled) or humans supervise every action (safe, unscalable). The moment you want two agents to collaborate, you're duct-taping prompts together and hoping nothing breaks.

The real problem isn't making agents do things. **It's orchestrating them as a team.**

Develcakes solves this by making the **project itself the social context.** Not a prompt you feed them. Not a task list you manage. The actual project — its files, its history, its team — is what they operate inside. You see everything. You control what gets merged. Agents collaborate with each other and you watch it happen in real time.

> **The project IS the chat. The feed IS the dashboard. Agents are teammates you manage.**

---

## <img src="icons/emoji/crab.png" width="80" height="80" alt="crab" style="vertical-align:middle; margin-top:-0.5em; margin-bottom:-0.5em" /> About the Name

Named after the Chesapeake Bay delicacy — sweet, rich, and built from parts other people throw away.

---

<div align="center">

*Part of the [Qontinuum Bridge](https://github.com/qsmtco) project.*

**Develcakes — where your project is the chat.**

*The first PDE.*

<br>

[![GTK4](https://img.shields.io/badge/GTK4-native-4a86cf?style=flat-square)](https://docs.gtk.org/gtk4/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776ab?style=flat-square)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e?style=flat-square)](LICENSE)
[![Tests: 3,200+](https://img.shields.io/badge/tests-3,200%2B-22c55e?style=flat-square)](tests/)

</div>
