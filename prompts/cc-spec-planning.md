<!-- 🦀 CRABCAKES WORKFLOW PROMPT -->
<!-- This prompt is part of the CrabCakes development workflow. -->
<!-- Do not rename or delete — referenced by the workflow guide. -->

# Spec Planning Phase (`cc-spec-planning`)

Turn the architecture into concrete, orderable **specs**. Each spec is the
atomic implementation contract — a builder will execute it and an auditor will
verify it.

---

## What You Do

1. Read `.crabcakes/project.md`, `.crabcakes/requirements.md`, and `.crabcakes/architecture.md`
2. Propose specs ordered by dependency, each covering behavior, acceptance criteria, and edge cases
3. Present the plan to me for review
4. I approve, modify, or reject specs
5. Record the approved spec list in chat and in `.crabcakes/workflow.md` (the spec-planning row)
6. Wait for approval before any creation or readiness transition
7. Update `.crabcakes/workflow.md` — mark spec-planning as done, only after the approved specs are recorded
8. Append to `.crabcakes/context.md`

---

## Spec Guidelines

### One Concern Per Spec

Each spec covers one thing. Don't combine unrelated changes.

### Ordered by Dependency

What blocks what? The spec list must be ordered by dependency so implementation can proceed in execution order.

### A Required Spec Per Item

Every approved item MUST have a spec file — the relative path under `docs/specs/` — the atomic implementation contract. The spec file defines behavior, acceptance criteria, and edge cases before any build.

---

## My Review

I will:
- Approve specs as-is
- Modify scope or priority
- Reject specs that don't fit the architecture
- Ask you to split oversized specs before approval

Wait for my approval before recording the approved spec list.

---

## Implementation Handoff

Implementation is **not** an autonomous engine. It is triggered **manually** by
PM delegation in chat, which hands off to the Supervisor (who loads
`prompts/implementationLoop.md`). The Supervisor orchestrates the
builder/auditor trio per spec.

---

## After Completion

1. Update `.crabcakes/workflow.md` — find the spec-planning row, change its status to ✅ done, set started/completed dates
2. Append to `context.md`: "Spec Planning phase complete. {N} specs approved."
3. Suggest: "Specs approved. Delegate implementation to the Supervisor (or ask me to) when you're ready."
