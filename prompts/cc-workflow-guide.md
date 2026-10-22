<!-- 🦀 CRABCAKES WORKFLOW PROMPT -->
<!-- This prompt is part of the CrabCakes development workflow. -->
<!-- Do not rename or delete — referenced by the workflow guide. -->

# Workflow Guide

Welcome to the CrabCakes workflow. This guide explains the phases for structured development.

Use this workflow for any project that needs planning. Go through the phases in order — they build on each other.

---

## The 7 Phases

| # | Phase | When to Use | Produces |
|---|-------|-------------|----------|
| 0 | Onboarding | Project creation (auto) | `project.md`, `context.md` |
| 1 | Discovery | New project, unclear requirements | `requirements.md` |
| 2 | Architecture | Design before code | `architecture.md` |
| 3 | Spec Planning | Propose approved specs with required spec files | Approved specs (recorded in chat + `.crabcakes/workflow.md`) |
| 4 | Implementation | PM delegates in chat (Supervisor) | Code, commits |
| 5 | Testing | Comprehensive testing after build | Test results |
| 6 | Ship | Deploy, release, handoff | Release artifacts |

Phases 0–3 are prompts you load from the Prompts tab. Phase 4 is triggered manually by PM delegation in chat, which hands off to the Supervisor (who loads `prompts/implementationLoop.md`). Phases 5–6 will be addressed in a future proposal.

---

## Phase Status

Read `.crabcakes/workflow.md` and display the current phase status below. If the file doesn't exist yet, say so.

Show a table of all 7 phases with their current status (✅ done / 🔄 current / ⏳ pending).

---

## What Each Phase Produces

### Discovery (`cc-discovery`)
Reads `project.md`, asks you questions one at a time, produces `requirements.md` covering:
- Problem statement
- MVP scope
- Out of scope
- User stories
- Acceptance criteria
- Edge cases

### Architecture & Design (`cc-architecture-design`)
Reads `project.md` + `requirements.md`, discusses the design with you, produces `architecture.md` covering:
- Module breakdown
- Data flow
- File structure
- Dependencies
- Design patterns
- API surfaces
- Error handling

### Spec Planning (`cc-spec-planning`)
Reads `project.md` + `requirements.md` + `architecture.md`, proposes specs with required
spec files (under `docs/specs/`), and records the approved spec list in chat and in
`.crabcakes/workflow.md` after you approve.

---

## Phase Gates

Each phase ends with a **gate** — the agent must stop and get explicit confirmation before proceeding to the next phase. No phase crossing without PM approval.

## How to Use This Guide

1. After onboarding, the agent will suggest loading this guide
2. Click **cc-workflow-guide** from the Prompts tab to load it
3. Review the phases and work through them in order — discovery → architecture → spec planning → implementation
4. Each phase produces the input for the next
5. Load each phase prompt from the Prompts tab when you're ready for that step
6. The agent will **always** stop at the gate and ask before moving to the next phase

**Prompts tab search:** Type `cc` to find all workflow prompts.
