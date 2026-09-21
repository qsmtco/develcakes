# Phase 8 — Prompts (cc-spec-planning + redirects + doc updates)

**Loop authority:** `prompts/implementationLoop.md` + `prompts/implementationSupervisor.md`
**Spec:** `docs/specs/SPEC-TASK-SYSTEM-FULL-REDESIGN.md` (READ §7.2 + §7.3 — authoritative)
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load fresh, follow every rule (especially Rule 3: verify every claim against source; Rule 8: don't modify what you weren't asked to modify).

## Files to change (4)

1. **`prompts/cc-spec-planning.md`** — NEW. The spec-planning phase prompt.
2. **`prompts/cc-task-planning.md`** — REWRITE as a compatibility redirect to `cc-spec-planning.md`.
3. **`prompts/cc-workflow-guide.md`** — MODIFY. Update phase 3 terminology (Task Planning → Spec Planning), outputs (tasks.md → generated tasks.md from work.json), and the implementation handoff (`/work start`).
4. **`prompts/system/crabcakes-commands.md`** — MODIFY. Document `/work` and its aliases, replacing the old `/task`-centric command table.

## Tests

Per spec §10: "Prompt/document static tests: `cc-spec-planning`, workflow guide, command reference, and project-awareness references contain no stale task-planning-only instructions where replacement is required."

Add a focused static-content test file `tests/test_task_redesign_prompts.py` that asserts:
- `prompts/cc-spec-planning.md` exists and contains the key concepts (Work Units, spec_path, `/work`, `cc-spec-planning`).
- `prompts/cc-task-planning.md` is a redirect (references `cc-spec-planning.md`, does NOT contain full task-planning instructions that conflict).
- `prompts/cc-workflow-guide.md` says "Spec Planning" (not "Task Planning"), references `cc-spec-planning` and `/work start`, and does NOT present phase 3 as "Task Planning" with flat `/task` tasks.
- `prompts/system/crabcakes-commands.md` documents `/work` and the legacy aliases.
- No stale `task-planning`-only instructions remain in these 4 files (the word "task-planning" may appear in migration/compat context, but not as an active phase name or prompt reference).

## Spec §7.2 — `prompts/cc-spec-planning.md` (NEW)

Per spec §7.2, this prompt must:
- read project manifest, requirements, and architecture;
- propose Work Units, each with a title, required spec path, status, priority, dependency IDs, and supervisor/builder/auditor assignments;
- wait for PM approval before creation/readiness transitions;
- use `/work` commands, not flat `/task` prose;
- write/update `.crabcakes/work.json` through the command system and treat `.crabcakes/tasks.md` as generated;
- mark `spec-planning` complete and append context only after approved Work Units/spec stubs are recorded.

Mirror the structure of the existing `cc-task-planning.md` (header comment, "What You Do" numbered list, guidelines, format, review, after-approval, after-completion) but adapted for Work Units. Key differences from the old task-planning prompt:
- "Work Unit" replaces "Task" throughout.
- Each proposed unit MUST have a `spec_path` (the atomic implementation contract). Creation starts at `status="draft"` with empty `spec_path`; the spec author marks it `spec-ready` only after the relative spec file exists.
- Use `/work` commands: `/work "Title"` to create a draft, `/work spec-ready #N` to mark readiness, `/work assign #N @agent` to set supervisor/builder/auditor, `/work priority #N level`, `/work start #N` to trigger the implementation loop.
- `.crabcakes/tasks.md` is GENERATED from `work.json` — do not hand-write it. State this explicitly.
- The implementation phase is triggered manually by `/work start #N`, which hands off to the Supervisor (who loads `prompts/implementationLoop.md`). There is no autonomous engine.

Include the header comment block (mirroring other cc-*.md prompts):
```
<!-- 🦀 CRABCAKES WORKFLOW PROMPT -->
<!-- This prompt is part of the CrabCakes development workflow. -->
<!-- Do not rename or delete — referenced by the workflow guide. -->
```

## Spec §7.2 — `prompts/cc-task-planning.md` (rewrite as redirect)

Per spec §7.2: "The preferred design is new `cc-spec-planning.md` plus a compatibility redirect/reference in the old file." Replace the entire content of `cc-task-planning.md` with a short redirect:

```markdown
<!-- 🦀 CRABCAKES WORKFLOW PROMPT -->
<!-- DEPRECATED: superseded by cc-spec-planning.md (SPEC-TASK-SYSTEM-FULL-REDESIGN §7.2). -->
<!-- Kept as a compatibility redirect. Do not delete — PHASE_PROMPTS historical references may point here. -->

# Task Planning → Spec Planning

This phase has been renamed to **Spec Planning**.

The authoritative prompt is now `prompts/cc-spec-planning.md`.

**Why:** Flat tasks have been replaced by Work Units whose required spec file is the atomic implementation contract. See `docs/specs/SPEC-TASK-SYSTEM-FULL-REDESIGN.md`.

**If you loaded this prompt:** Load `prompts/cc-spec-planning.md` instead and follow it. Use `/work` commands (not `/task`).
```

Keep the header comment block. Do NOT leave conflicting task-planning instructions in this file.

## Spec §7.3 — `prompts/cc-workflow-guide.md`

Read the file first. Update:
- The phase table row for phase 3: "Task Planning" → "Spec Planning"; "Break design into tasks" → "Propose Work Units with required specs"; "Tasks in TaskStore, `tasks.md`" → "Work Units in `.crabcakes/work.json`, generated `tasks.md`".
- The phase 3 section heading/description: rename to "Spec Planning (`cc-spec-planning`)" and describe Work Units, spec_path, `/work` commands, and the manual `/work start` handoff to the Supervisor.
- Any reference to an autonomous engine or "engine-run implementation" for phase 4 → replace with "manual `/work start #N` triggers the implementation loop (Supervisor loads `prompts/implementationLoop.md`)".
- Ensure phase 4 (implementation) references `/work start` and the Supervisor/builder/auditor trio, not a deterministic engine.

Do NOT change phases 0-2, 5-6, or any non-task-related content.

## Spec §7.3 — `prompts/system/crabcakes-commands.md`

Read the file first. Update the command table:
- Replace the `/task` row with `/work` (canonical) + note the legacy aliases.
- Document `/work` subcommands: `list`, `start`, `done`, `blocked`, `unblock`, `cancel`, `assign`, `priority`, `spec-ready`, `status`.
- Document the legacy aliases (`/task`, `/tasks`, `/start`, `/done`, `/blocked`, `/cancel`, `/assign`, `/priority`) routing to the Work Handler.
- Note that `/work start #N` is the implementation-loop trigger (hands off to the Supervisor).
- Remove or update the note about `/t` alias (it's gone per spec §5.1).
- Keep the existing structure/style of the file. Do NOT change non-work commands (ask, delegate, tell, stop, review, check, accept, reject, status, agents, cost, clear, compact, session, help).

## Rules
- `prompts/steelFramedCodeWriter.md` — Discovery block first (read all 4 files before editing).
- Do NOT modify Phase 1-7 code files.
- Verify after edit: `grep -RIn "task-planning" prompts/cc-spec-planning.md prompts/cc-task-planning.md prompts/cc-workflow-guide.md prompts/system/crabcakes-commands.md` — `task-planning` may appear ONLY in migration/redirect/compat context (e.g., "renamed from task-planning", "DEPRECATED"), NOT as an active phase name or the prompt to load.
- Verify `cc-spec-planning.md` exists: `ls prompts/cc-spec-planning.md`.
- Verify the redirect: `head -5 prompts/cc-task-planning.md` should show the DEPRECATED redirect header.
- Files must end with a trailing newline.

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x] Edit 1: prompts/cc-spec-planning.md created — evidence: <wc -l>, <ls>, <grep for key concepts>
- [x] Edit 2: prompts/cc-task-planning.md rewritten as redirect — evidence: <head -5>, <no conflicting instructions>
- [x] Edit 3: prompts/cc-workflow-guide.md updated (Spec Planning, /work start, generated tasks.md) — evidence: <grep>, <no "Task Planning" as active phase>
- [x] Edit 4: prompts/system/crabcakes-commands.md updated (/work + aliases) — evidence: <grep>
- [x] Edit 5: tests/test_task_redesign_prompts.py created — evidence: <test count>, <pytest output>
- [x] Edit 6: no stale task-planning-only instructions — evidence: <grep output>
- [x] Edit 7: no Phase 1-7 code files modified — evidence: git status
```

Report: diffs/file contents, grep outputs, pytest output, COMPLETENESS block. Flag related issues, don't silently fix. Write when done.
