# Projects in CrabCakes

Projects are the primary unit of work in CrabCakes. They organize code, agents, and conversations into self-contained workspaces. Every special agent (except Auxilium) requires an active project to operate.

---

## What Is a Project?

A project is a directory on your filesystem that CrabCakes opens as a workspace. It contains your code files, a `.crabcakes/` configuration directory, and optionally a git repository. Projects live under `~/projects/` by default (configurable via `$CRABCAKES_PROJECTS_DIR`).

When you open a project, CrabCakes:
1. Creates a project chat tab for team communication
2. Loads team members from `.crabcakes/team.json`
3. Refreshes the agent list with +/− membership buttons
4. Initializes the `.crabcakes/` directory if it doesn't exist
5. Auto-adds onboarding agents (those with `auto_add_to_projects: true`)
6. Sets the active project path for all special agents

---

## Project Lifecycle

### Creating a Project

You can create a new project from the file tree:

1. Right-click or use the New Project button
2. Enter a project name
3. Optionally specify a custom path (defaults to `$CRABCAKES_PROJECTS_DIR/<name>`)

CrabCakes then:
- Creates the directory (`os.makedirs`)
- Initializes `.crabcakes/` with awareness artifacts
- Auto-adds onboarding agents to the team
- Initializes `workflow.md` (via `utils/workflow_state`)
- Initializes a git repo with an initial commit
- Builds and saves an awareness snapshot
- Opens the project

### Opening an Existing Project

Double-click a project directory in the file tree. CrabCakes opens it as a project tab, loads the team, and populates the routing table.

### Closing a Project

Closing a project tab clears the active project state, removes routing entries, and fires `on_project_closed` callbacks. The team and all `.crabcakes/` files persist for next time.

---

## The `.crabcakes/` Directory

Each project has a `.crabcakes/` directory containing configuration and context files:

### `project.md`

The project manifest. Contains the project name, description, and any project-specific documentation. On first init, a skeleton is generated:

```markdown
# Project Name

## Description
<!-- Describe what this project does -->

## Context
<!-- Any project-specific context. -->
```

If a `crabcakes.md` file exists at the project root (legacy format), it's migrated to `.crabcakes/project.md`.

### `team.json`

The team roster — a JSON file listing all agent members. Each member has:

```json
{
  "members": [
    {
      "session_key": "special:coder",
      "name": "Coder",
      "role": "onboarding guide",
      "can_write": true
    }
  ]
}
```

Agents with `auto_add_to_projects: true` (like Auxilium) are automatically added here on project creation.

### `context.md`

Free-form context that agents receive in their system prompt. Capped at 50KB (`MAX_CONTEXT_SIZE = 50 * 1024`). Edit this to give agents persistent background information about the project.

### `awareness.json`

A snapshot of project state, including git status, file tree summary, and recent activity. Built by `build_awareness_snapshot()` and refreshed periodically. Used to give agents situational awareness.

### `workflow.md`

Tracks the project's current workflow state and onboarding progress. Initialized with the onboarding phase as "current". Managed by `utils/workflow_state.init_workflow()`.

### `agent-system-prompt.md` (optional)

If present, overrides the default system prompt for agents working in this project. The context builder checks for this file first, then falls back to `AGENTS.md` at the project root, then uses the built-in prompt.

---

## Team Management

### Adding and Removing Agents

When a project is open, the agent list in the sidebar shows all available agents with **+/− buttons** next to each one:

- Click **+** to add an agent to the project team
- Click **−** to remove an agent

This toggles membership in `.crabcakes/team.json` and updates the routing table.

### Auto-Added Onboarding Agents

Agents with `auto_add_to_projects: true` in their YAML definition are automatically added to every new project. By default, Auxilium (🦀) has this enabled. These agents serve as onboarding guides — they receive the project-onboarding prompt template when the project hasn't been fully onboarded yet.

### Solo DM Mode

Within a project chat, you can switch between two modes:

- **Broadcast (All):** Messages go to all team members — the default
- **Solo DM:** Messages go to a single agent only

The solo target is tracked per-project in `ProjectHandler._solo_targets`. Use the session menu (right-click on an agent) to switch between broadcast and solo DM.

---

## Project Chat

### Broadcast Mode

When you type in the project chat tab, your message is fan-out delivered to all team members. Each agent processes the message independently and responds in the shared project chat.

### Solo DM Mode

In solo mode, only the targeted agent receives your message. Other team members don't see it. The solo target's name is marked with "(solo DM target)" in `/agents` output.

### Agent-to-Agent Communication

Agents can communicate with each other using slash commands (see Collaboration Commands below). Agent responses are scanned for slash-prefixed commands by `AgentCommandHandler`, which routes them through the same command pipeline as user input.

---

## Collaboration Commands

Project chat supports slash-prefixed commands for directing work to agents. All commands support `@mentions` to target specific agents, or `@` (bare) for broadcast to all.

### `/ask @agent "question"`

Ask an agent a question. The question is forwarded to the target agent. If the agent responds, the response is relayed back to the asking agent via the pending-ask tracking system.

```
/ask @Coder "What does the auth module do?"
```

### `/stop @agent`

Send a stop signal to an agent. Used when an agent is running a long task and you want it to halt.

### Broadcast Variants

Both commands (`/ask`, `/stop`) support broadcast by using `@` instead of `@agent_name`:

```
/ask @ "What does the auth module do?"
```

This forwards the question to all team members.

### Other Project Commands

- `/status` — Show project status summary (members, tasks, review state, solo DM target)
- `/agents` — List project members and their current state
- `/cost` — Spending summary for the project

---

## Code Review Layer

### Overview

CrabCakes reviews agent writes through **feed cards**: enforcement results surface as cards and are accepted or rejected right on the project feed, with a standalone git-backed revert available from the file drawer. (The older checkpoint/diff/accept/reject review mode was removed — see "Checkpoint-Based Review — removed" below.)

### Feed Card Review (Primary)

When agents write files and the enforcement layer runs, feed cards surface these events in a feed bar. Each card shows:
- Which agent made the change
- What tool was used (write_file, exec_command, etc.)
- The result (success, error, pending approval)
- Duration and tool details

For `exec_command`, the feed card also captures command output, exit code, and duration.

### Enforcement Layer

After each `write_file` or `edit_file` tool execution, the enforcement layer runs verification tiers:
- **Syntax check** — Python syntax validation
- **Test run** — Run relevant tests
- **Lint check** — Code style validation

Results are appended to the tool result and dispatched via `on_enforcement_status`. Failed checks appear in the feed with ❌ icons.

### Checkpoint-Based Review — removed

The `/review` checkpoint system (`/review`, `/check`, `/accept`, `/reject`,
`ReviewState`, ReviewBar) was removed in SPEC-58. Reviewing agent writes now
happens on the project feed: accept/reject on feed cards, plus the standalone
`ProjectHandler.revert_file_to_sha` git checkout from the file drawer.

---

## Feed Cards

Feed cards are the primary UI element for surfacing agent activity, tool results, and system events in a project.

### Card Types

- **`agent_action`** — Agent tool calls (reading, writing, executing commands). Shows running → complete/error states.
- **`pending_approval`** — When an agent requests `exec_command` approval. Has Approve/Deny buttons.
- **`git_commit`** — Automatic git commits made by CrabWatch as agents change files.
- **`diff` / `audit_report`** — Posted by the `feed_card` tool (file changes and audit results).

### When Cards Appear

Cards are added to the feed whenever:
- An agent starts a tool call (`_on_tool_call_start`)
- A tool call completes (`_on_tool_call_result`)
- An agent requests exec approval (`_on_tool_call_approval_needed`)
- An agent calls the `feed_card` tool

### Approval Cards

When an agent calls `exec_command`, a pending-approval card appears with:
- The agent's name
- The shell command to be executed
- **Approve** and **Deny** buttons

Clicking either resolves the approval via `AgentRuntimeHandler.approve_exec()`, which forwards the decision to the runtime.

---

## Project Context Injection

When an agent works within a project, it receives rich context in its system prompt:

### What's Included

1. **Project docs** — `.crabcakes/project.md` content is prepended to file context (§4.4a)
2. **File tree** — `build_file_context()` respects `.gitignore`, caps at ~50K chars
3. **Custom system prompt** — `.crabcakes/agent-system-prompt.md` if it exists, otherwise `AGENTS.md` at project root
4. **Awareness snapshot** — Git status, file structure summary from `.crabcakes/awareness.json`
5. **Project context** — `.crabcakes/context.md` content (up to 50KB)

### How Context Updates

When the active project changes, `AgentRuntimeHandler.set_active_project()`:
1. Updates `project_path` on all existing conversations
2. Rebuilds the system prompt with `build_system_prompt()` using the new project context
3. Injects the new project path for all future conversations

---

## Audit Reports

Audit reports are structured feedback blocks (`## Audit Report`) that log bugs and issues to the target agent's bug journal. They're part of the SPEC-3 structured feedback protocol.

When the enforcement layer catches issues (syntax errors, test failures, lint violations):
1. Results appear in feed cards with ❌ icons
2. An audit report may be generated and sent to the agent's bug journal
3. The agent's self-improvement system uses these reports to learn from mistakes

---

## Project Files Reference

### Configuration Paths

- **Config directory:** `~/.config/crabcakes/` (respects `$XDG_CONFIG_HOME`)
- **Projects root:** `~/projects/` (configurable via `$CRABCAKES_PROJECTS_DIR`)
- **Per-project config:** `<project_path>/.crabcakes/`
- **Project membership (legacy):** `~/.config/crabcakes/projects/<name>/members.json`

### `.crabcakes/` File Summary

| File | Purpose | Managed By |
|------|---------|------------|
| `project.md` | Project manifest (name, description) | `project_awareness.py` |
| `team.json` | Agent team roster | `project_awareness.py` |
| `context.md` | Free-form context for agents (50KB cap) | `project_awareness.py` |
| `awareness.json` | Project state snapshot | `project_awareness.py` |
| `workflow.md` | Workflow/onboarding state | `workflow_state.py` |
| `agent-system-prompt.md` | Custom system prompt override | User-created |
| `context-snapshot.json` | Detailed context snapshot | `project_awareness.py` |

---

## Git Integration

Projects optionally use git for version control:

- **New projects:** Git repo initialized automatically with initial commit
- **Team changes:** Auto-committed via `_git_commit_if_available()`
- **Review sessions:** Checkpoint commits, diff checks, accept/reject commits
- **Review staging:** Agent `write_file` results can be staged to a shadow directory for review

Git operations use `utils/git_ops.py` (pure Python, no GTK). All git commands run in background threads to avoid blocking the UI.
