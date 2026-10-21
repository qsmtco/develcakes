# Implementation Engine — Formal Proposal

**Date:** 2026-04-26
**Author:** Qaster
**Status:** Superseded 2026-10-21 by `PROPOSAL-META.md` Program 4. The PICK→BUILD→TEST→REVIEW→RECORD cycle is scheduled against work units. This file's `tasks.md` layout and `` `task `` command set are not.
**Affects:** `models/`, `utils/`, `ui/handlers/`, `ui/window.py`

> **Status (verified 2026-07-12):** ❌ **NOT IMPLEMENTED** — No `utils/implementation_engine.py` or equivalent found in the codebase. The proposal remains an unimplemented design., `prompts/`

> **Status (verified 2026-06-12):** ⚠️ **PARTIALLY DONE** — 
> **status:** `PARTIAL` — sortable tag for `ls | grep STATUS` The task system is shipped (8 task commands in `ui/handlers/task_handler.py` — `task`, `done`, `start`, `blocked`, `cancel`, `tasks`, `assign`, `priority`; `TaskStore` data model in `models/task.py`). However, the **deterministic PICK→BUILD→TEST→REVIEW→RECORD engine** described here is NOT built — there is no `utils/implementation_engine.py`, no automatic `task run` cycle, no per-task agent dispatch logic. The task system is manual-only today: PMs create and assign tasks, agents pick them up, but there's no automated engine that runs the cycle.

---

## 1. Objective

The task system IS the implementation engine. When the PM creates tasks via `` `task add ``, they are creating units of work for the engine to execute. When the PM runs `` `task run ``, the engine processes every pending task through a deterministic cycle until all are complete.

The engine replaces ad-hoc "build stuff" sessions with a structured, trackable, resumable process. It runs the same way whether one agent works solo or multiple agents collaborate. All subsequent CrabCakes systems — review layer, agent collaboration, code review — depend on the task system. Without tasks, nothing else works.

---

## 2. Design Principles

1. **Tasks ARE the engine.** Tasks exist → engine can run. No tasks → engine is idle. Creating a task is creating potential energy. Running tasks converts it to kinetic energy.
2. **One command namespace.** All task operations live under `` `task <subcommand> ``. Consistent, predictable, no ambiguity.
3. **Deterministic cycle.** PICK → BUILD → TEST → REVIEW → RECORD → repeat. No skipped strokes. No shortcuts.
4. **Same engine, solo or collaborative.** Collaboration is determined by who's available, not by a different code path.
5. **State is persisted to `.crabcakes/`.** Engine state survives app restarts, session changes, and agent switches. Any agent can resume by reading state files.
6. **The PM drives.** The engine suggests but never blocks. The PM can pause, resume, run specific tasks, or amend the plan at any time.
7. **Architecture compliance.** Models in `models/` (pure data), logic in `utils/` (pure Python), orchestration in a handler (ui/handlers/), wiring in window.py.

---

## 3. Command Reference

All task operations use the `` `task <subcommand> `` pattern. No backwards compatibility with old flat commands — clean break.

### Task Management

| Command | What | Example |
|---------|------|---------|
| `` `task add <description> `` | Create task, unassigned | `` `task add Build the file watcher core `` |
| `` `task add @agent — <description> `` | Create + assign to agent | `` `task add @QTR — Build the file watcher core `` |
| `` `task list `` | Show all tasks with status | `` `task list `` |
| `` `task done <id> `` | Mark task complete | `` `task done 00000003 `` |
| `` `task start <id> `` | Manually start a task | `` `task start 00000003 `` |
| `` `task assign <id> @agent `` | Reassign task | `` `task assign 00000003 @Coder `` |
| `` `task priority <id> <level> `` | Set priority (low/medium/high/critical) | `` `task priority 00000003 high `` |
| `` `task block <id> — <reason> `` | Mark task blocked with reason | `` `task block 00000003 — waiting on API key `` |
| `` `task cancel <id> `` | Cancel a task | `` `task cancel 00000003 `` |

### Engine Control

| Command | What | Example |
|---------|------|---------|
| `` `task run `` | Run all pending tasks continuously | `` `task run `` |
| `` `task run <id> `` | Run one specific task, then stop | `` `task run 00000003 `` |
| `` `task pause `` | Pause engine after current stroke | `` `task pause `` |
| `` `task resume `` | Resume engine from where paused | `` `task resume `` |

### Command Parsing

The `task` command handler uses subcommand routing:

```python
TASK_SUBCOMMANDS = {
    "add", "list", "done", "start", "assign", "priority",
    "block", "cancel", "run", "pause", "resume",
}

def _cmd_task(self, cmd: Command) -> CommandResult:
    if cmd.args and cmd.args[0] in TASK_SUBCOMMANDS:
        subcmd = cmd.args.pop(0)
        return self._route_task_subcommand(subcmd, cmd)
    # Default: treat as 'task add' for convenience
    return self._task_add(cmd)
```

This means `` `task Build the watcher `` (no subcommand) still works as a shorthand for `` `task add Build the watcher ``.

---

## 4. Engine Cycle — The Five Strokes

```
For each pending task (triggered by `task run):
  ┌──────────────────────────────────────────────────────────┐
  │                                                          │
  │  1. PICK   — Select next pending task from TaskStore     │
  │  2. BUILD  — Agent implements the task (code, files)     │
  │  3. TEST   — Run tests for what was just built           │
  │      │                                                   │
  │      ├── pass  → advance to REVIEW                       │
  │      └── fail  → back to BUILD (fix → retest)            │
  │                                                          │
  │  4. REVIEW — Commit + review against architecture/spec   │
  │      │                                                   │
  │      ├── solo:       self-review, auto-approve           │
  │      ├── collab:     stage for another agent to review    │
  │      └── review-mode: auto-pause for PM approval          │
  │                                                          │
  │  5. RECORD — Mark task done in tasks.md, update           │
  │              workflow.md, append to context.md            │
  │      │                                                   │
  │      └── more tasks? → PICK next task                     │
  │          no more?    → ENGINE COMPLETE                    │
  │                                                          │
  └──────────────────────────────────────────────────────────┘
```

### Stroke Details

**PICK:** Read TaskStore. Select first task with status `pending`. Sort by priority (critical → high → medium → low), then creation order. If task has an assigned agent and that agent is available (in the project team), assign it. If unassigned, assign to first available agent based on role priority. If no agents available, pause and notify PM.

**BUILD:** Send the task description + architecture context + project awareness to the assigned agent. Agent implements the code using the checkpoint code writer pattern — small verified steps. Output: code files written to disk.

**TEST:** Run project tests (`pytest` by default, configurable in `.crabcakes/project.md` conventions section). If tests fail, feed failure output back to the agent and return to BUILD. Max 3 retries on the same task before escalating to PM with `blocked` status.

**REVIEW:**
- **Solo mode:** Agent self-reviews. Check: does the code match the architecture? Does it meet the task's acceptance criteria? If yes, commit and proceed.
- **Collaborative mode:** Building agent commits to staging. Engine identifies a reviewing agent (different from builder, based on role — reviewer/committer role). Sends the diff for review. Reviewer approves or requests changes. If changes requested, back to BUILD.
- **Review mode on:** Engine auto-pauses after commit. PM reviews via the existing ReviewHandler accept/reject flow. PM approves → proceed. PM rejects → back to BUILD with feedback.

**RECORD:** Mark task `done` in TaskStore and `tasks.md`. Update `workflow.md` engine state (completed count, remaining count). Append a dated entry to `context.md` summarizing what was done (task ID, files changed, commit SHA). Git commit if state files changed. Then check: more pending tasks? → PICK. No more? → ENGINE COMPLETE.

### Pause Behavior

When the PM sends `` `task pause ``:
1. Set internal flag: `_pausing = True`
2. Engine finishes the current stroke (never interrupts mid-BUILD)
3. After stroke completes, engine writes state to `workflow.md` and stops
4. Display: "⏸ Engine paused. Task 00000004 at BUILD stroke. Use `task resume to continue."

When the PM sends `` `task resume ``:
1. Read engine state from `workflow.md`
2. Resume from the exact task and stroke where it paused
3. Continue as if `task run` was issued

---

## 5. State Files

### `.crabcakes/workflow.md`

High-level phase and engine state. Any agent can read this and know exactly where the project stands.

```markdown
# Workflow State

## Project: crabwatch
## Current Phase: implementation
## Engine Status: paused
## Last Updated: 2026-04-26T14:30:00

## Phase History
| # | Phase | Status | Started | Completed | Notes |
|---|-------|--------|---------|-----------|-------|
| 0 | onboarding | ✅ done | 2026-04-26 | 2026-04-26 | Qaster populated project.md |
| 1 | discovery | ✅ done | 2026-04-26 | 2026-04-26 | Requirements captured |
| 2 | architecture | ✅ done | 2026-04-26 | 2026-04-26 | Architecture doc written |
| 3 | task-planning | ✅ done | 2026-04-26 | 2026-04-26 | 6 tasks defined |
| 4 | implementation | 🔄 running | 2026-04-26 | — | 3/6 done, paused at task 4 |
| 5 | testing | ⏳ pending | — | — | — |
| 6 | code-review | ⏳ pending | — | — | — |
| 7 | ship | ⏳ pending | — | — | — |

## Engine State
| Field | Value |
|-------|-------|
| Status | paused |
| Current Task ID | 00000004 |
| Current Stroke | build |
| Tasks Completed | 3 |
| Tasks Remaining | 3 |
| Total Cycles | 3 |
| Last Cycle | 2026-04-26T14:25:00 |
```

### `.crabcakes/tasks.md`

Granular task tracking. Persistent, human-readable, agent-readable. The ground truth on project load.

```markdown
# Tasks

## Task 00000001: File watcher core — ✅ done
- **Assigned:** QTR
- **Priority:** high
- **Started:** 2026-04-26T13:00:00
- **Completed:** 2026-04-26T13:15:00
- **Commits:** a1b2c3d
- **Tests:** passing
- **Notes:** Watchdog observer + event handlers for create/modify/delete

## Task 00000002: Context writer — ✅ done
- **Assigned:** QTR
- **Priority:** high
- **Started:** 2026-04-26T13:15:00
- **Completed:** 2026-04-26T13:30:00
- **Commits:** e4f5g6h
- **Tests:** passing
- **Notes:** Appends timestamped entries to .crabcakes/context.md

## Task 00000003: Git log parser — ✅ done
- **Assigned:** QTR
- **Priority:** high
- **Started:** 2026-04-26T13:30:00
- **Completed:** 2026-04-26T14:00:00
- **Commits:** i7j8k9l
- **Tests:** passing
- **Notes:** Parses git log --stat into structured summary

## Task 00000004: Daily summary cron — 🔄 in_progress
- **Assigned:** Coder
- **Priority:** medium
- **Started:** 2026-04-26T14:05:00
- **Stroke:** build
- **Retries:** 0/3
- **Notes:** Writing cron scheduler

## Task 00000005: Integration tests — ⏳ pending
- **Priority:** high

## Task 00000006: CLI entry point and README — ⏳ pending
- **Priority:** medium
```

---

## 6. New Modules

### `utils/engine.py` — Implementation Engine

Pure Python. No GTK, no network. Owns the cycle logic, state transitions, and task selection.

**Architecture:** `utils/` per Section 2 — pure Python utilities, no imports from ui/agent/gateway.

**Public API:**

```python
@dataclass
class EngineState:
    """Snapshot of engine state for display and persistence."""
    status: str              # "idle" | "running" | "paused" | "complete" | "error"
    current_task_id: str     # task being processed
    current_stroke: str      # "pick" | "build" | "test" | "review" | "record"
    tasks_completed: int
    tasks_remaining: int
    total_cycles: int
    last_cycle_at: str       # ISO timestamp
    error_message: str       # non-empty if status == "error"

@dataclass
class StrokeResult:
    """Result of a single stroke execution."""
    success: bool
    output: str              # human-readable summary
    files_changed: list[str] # for TEST and REVIEW strokes
    retry: bool = False      # True → go back to BUILD

class ImplementationEngine:
    """
    The implementation engine. Runs the PICK → BUILD → TEST → REVIEW → RECORD cycle.
    
    Does NOT directly interact with agents. Emits callbacks for each stroke,
    and the handler layer (EngineHandler) connects those callbacks to agent
    communication (gateway sends, runtime calls, etc.).
    
    Pure Python — no GTK, no network.
    """
    
    def __init__(
        self,
        project_path: str,
        task_store: TaskStore,
        on_pick: Callable[[Task], None],
        on_build: Callable[[Task], StrokeResult],
        on_test: Callable[[Task, list[str]], StrokeResult],
        on_review: Callable[[Task, StrokeResult], StrokeResult],
        on_record: Callable[[Task, StrokeResult], None],
        on_error: Callable[[Task, str], None],
        on_complete: Callable[[EngineState], None],
    ):
        ...
    
    def run_all(self) -> EngineState:
        """Run all pending tasks. Blocks until complete, paused, or error."""
    
    def run_task(self, task_id: str) -> EngineState:
        """Run a single task by ID. Returns state after completion."""
    
    def pause(self) -> None:
        """Signal pause. Engine finishes current stroke, then stops."""
    
    def resume(self) -> EngineState:
        """Resume from where the engine paused."""
    
    def get_state(self) -> EngineState:
        """Return current engine state."""
    
    def load_state(self, project_path: str) -> EngineState:
        """Read engine state from .crabcakes/workflow.md."""
    
    def save_state(self, project_path: str, state: EngineState) -> None:
        """Write engine state to .crabcakes/workflow.md."""
```

**Stroke callbacks are I/O-free.** The engine calls `on_build(task)` but doesn't know HOW the build happens. It could be a gateway agent, a special agent runtime, or even a local script. The handler layer decides. This keeps the engine pure and testable.

### `ui/handlers/engine_handler.py` — Engine Handler

Orchestrates the engine from the UI layer. Connects engine callbacks to agent communication.

**Architecture:** Handler pattern per Section 8.6. No cross-handler imports. Window wires dependencies.

**Public API:**

```python
class EngineHandler:
    def __init__(
        self,
        project_handler,        # for project path, team lookup
        chat_handler,           # for sending messages to agents
        agent_runtime_handler,  # for special agent tasks
        command_handler,        # for registering `task subcommands
        agent_manager,          # for agent name resolution
        task_store,             # shared TaskStore
        GLib_module,            # for GTK thread dispatch
    ):
        ...
    
    def cmd_task(self, cmd: Command) -> CommandResult:
        """Main `task command handler. Routes to subcommand handlers."""
    
    def _task_add(self, cmd: Command) -> CommandResult:
        """Create a new task."""
    
    def _task_list(self, cmd: Command) -> CommandResult:
        """List all tasks with status."""
    
    def _task_done(self, cmd: Command) -> CommandResult:
        """Mark task complete."""
    
    def _task_start(self, cmd: Command) -> CommandResult:
        """Manually start a task."""
    
    def _task_assign(self, cmd: Command) -> CommandResult:
        """Reassign task to agent."""
    
    def _task_priority(self, cmd: Command) -> CommandResult:
        """Set task priority."""
    
    def _task_block(self, cmd: Command) -> CommandResult:
        """Mark task blocked."""
    
    def _task_cancel(self, cmd: Command) -> CommandResult:
        """Cancel a task."""
    
    def _task_run(self, cmd: Command) -> CommandResult:
        """Run all pending tasks (or single task if ID provided)."""
    
    def _task_pause(self, cmd: Command) -> CommandResult:
        """Pause engine after current stroke."""
    
    def _task_resume(self, cmd: Command) -> CommandResult:
        """Resume engine from paused state."""
```

**Callback wiring — how strokes connect to agents:**

| Stroke | Gateway Agent | Special Agent (Coder/Debugger) |
|--------|--------------|-------------------------------|
| PICK | Engine selects from TaskStore | Same |
| BUILD | `chat_handler.send_message(agent, task_prompt)` | `agent_runtime_handler.send_message(agent, task_prompt)` |
| TEST | `chat_handler.send_message(agent, "run tests")` | Agent runtime exec_command("pytest") |
| REVIEW | Self-review via message | Self-review via runtime |
| RECORD | Agent writes to tasks.md/context.md | Agent writes via file tools |

The handler decides which path based on the agent type (gateway session key vs. `special:` prefix).

---

## 7. Task Persistence

**Current state:** `TaskStore` is in-memory only. Tasks are lost on restart.

**Proposal:** Add persistence to `.crabcakes/tasks.md` alongside the in-memory store.

**`utils/task_persistence.py`** — new utility:

```python
def load_tasks_from_file(project_path: str) -> list[Task]:
    """Parse .crabcakes/tasks.md into Task objects."""

def save_tasks_to_file(project_path: str, tasks: list[Task]) -> None:
    """Write Task objects to .crabcakes/tasks.md in human-readable format."""

def sync_task_store(store: TaskStore, project_path: str) -> None:
    """Bidirectional sync: reconcile in-memory store with tasks.md."""
```

**Format:** The markdown format from Section 5. Parseable by both humans and agents. The `load_tasks_from_file` parser handles the structured markdown sections.

**When synced:**
- On project open: `sync_task_store()` loads tasks.md → TaskStore
- On task create/update: save to both TaskStore and tasks.md
- On engine RECORD: update task status in tasks.md

---

## 8. Workflow Phase Tracking

**`utils/workflow_state.py`** — new utility:

```python
def load_workflow_state(project_path: str) -> dict:
    """Read .crabcakes/workflow.md. Returns parsed state dict."""

def save_workflow_state(project_path: str, state: dict) -> None:
    """Write .crabcakes/workflow.md."""

def advance_phase(project_path: str, phase_name: str) -> None:
    """Mark current phase complete, start next phase."""

def get_current_phase(project_path: str) -> str:
    """Return current phase name."""

def is_phase_complete(project_path: str, phase_name: str) -> bool:
    """Check if a specific phase is marked done."""
```

**Phase names:** `onboarding`, `discovery`, `architecture`, `task-planning`, `implementation`, `testing`, `code-review`, `ship`.

**The agent suggests phase transitions.** After onboarding, the agent reads workflow state and says "Next step: discovery. Load the discovery prompt?" The agent doesn't force it — it suggests.

---

## 9. Command Registration

The old flat task commands are removed. A single `task` command with subcommand routing replaces them all.

```python
# In window.py _register_commands():

# REMOVE old registrations:
#   "task", "done", "start", "blocked", "cancel",
#   "tasks", "assign", "priority"

# ADD new unified registration:
self._command_handler.register_command(
    "task",
    self._engine_handler.cmd_task,
    aliases=["t"],
    help_text="Task management: add, list, run, pause, resume, done, start, assign, priority, block, cancel"
)
```

---

## 10. Prompt Library Additions

Not auto-injected system prompts. User-loadable prompts from `prompts/`.

| File | Purpose |
|------|---------|
| `prompts/workflow-guide.md` | Meta-prompt: present workflow phases, help PM decide which to use |
| `prompts/discovery.md` | Requirements gathering, acceptance criteria, scope definition |
| `prompts/architecture-design.md` | Module design, data flow, file structure, pattern selection |
| `prompts/task-planning.md` | Break design into ordered, assignable tasks |
| `prompts/implementation.md` | Engine-aware: build, test, review, record for a single task |
| `prompts/testing.md` | Comprehensive test pass against requirements (post-implementation) |
| `prompts/ship.md` | Push, tag, update docs, close out |

Each prompt is self-contained. It reads from `.crabcakes/` state files and instructs the agent on what to do for that phase. The PM loads them manually from the Prompts tab.

---

## 11. Files Changed

| File | Action | Purpose |
|------|--------|---------|
| `utils/engine.py` | NEW | Engine cycle logic — pure Python |
| `utils/task_persistence.py` | NEW | TaskStore ↔ tasks.md sync |
| `utils/workflow_state.py` | NEW | Workflow phase read/write |
| `ui/handlers/engine_handler.py` | NEW | Engine orchestration + all `task` subcommand handlers |
| `ui/window.py` | MODIFY | Wire EngineHandler, replace old task commands with unified `task` |
| `models/task.py` | MODIFY | Add `acceptance_criteria`, `complexity` fields |
| `docs/ARCHITECTURE.md` | MODIFY | Add new modules to Sections 2, 3, 11 |

**Removed (absorbed into EngineHandler):**
- Old `_cmd_task`, `_cmd_done`, `_cmd_start`, `_cmd_blocked`, `_cmd_cancel`, `_cmd_tasks`, `_cmd_assign`, `_cmd_priority` methods from `window.py`

---

## 12. Implementation Order

| Phase | What | Depends On |
|-------|------|-----------|
| 1 | `utils/workflow_state.py` — read/write workflow.md | Nothing |
| 2 | `utils/task_persistence.py` — TaskStore ↔ tasks.md | models/task.py |
| 3 | `models/task.py` — add `acceptance_criteria`, `complexity` fields | Nothing |
| 4 | `utils/engine.py` — EngineState + cycle logic (stroke callbacks only) | workflow_state, task_persistence |
| 5 | `ui/handlers/engine_handler.py` — subcommand routing + all task handlers + engine orchestration | engine.py, chat_handler, project_handler |
| 6 | `ui/window.py` — wire EngineHandler, replace old command registrations | engine_handler |
| 7 | Prompt library — workflow-guide, discovery, architecture, task-planning, implementation, testing, ship | All above |
| 8 | Integration test on crabwatch | All above |

---

## 13. What Does NOT Change

| Component | Reason |
|-----------|--------|
| `models/command.py` | Command/CommandResult/CommandRegistry unchanged — `task` is just another command |
| `models/task.py` core | Task/TaskStore unchanged except new optional fields |
| TaskStore in-memory model | Persists alongside, not replaced |
| ReviewHandler | Engine REVIEW stroke integrates with existing review flow |
| ChatHandler | Engine uses it for agent communication, doesn't modify it |
| AgentRuntimeHandler | Engine uses it for special agent tasks, doesn't modify it |
| `prompts/system/` templates | No changes to auto-injected system prompts |

---

## 14. Architecture Compliance Checklist

- [x] `utils/engine.py` — pure Python, no GTK, no network
- [x] `utils/task_persistence.py` — pure Python file I/O
- [x] `utils/workflow_state.py` — pure Python file I/O
- [x] `models/task.py` — pure data (new optional fields only)
- [x] Handler pattern (Section 8.6) — EngineHandler owns all task logic
- [x] No cross-handler imports — window wires dependencies
- [x] GTK calls via `GLib.idle_add()` — EngineHandler dispatches to main thread
- [x] Command pattern — single `task` command with subcommand routing via CommandRegistry
- [x] State persisted to `.crabcakes/` — survives restarts
- [x] Config paths via `utils/config.py` — no hardcoded paths

---

## 15. Open Questions

1. **How does the engine know when BUILD is complete for a gateway agent?** Gateway agents respond asynchronously. Need a response correlation mechanism (message comes back with task context). This is the hardest integration problem. Options:
   - Include task ID in the message sent to the agent; agent references it in response
   - Use gateway request IDs to correlate response to task
   - Engine watches for git commits as a proxy for "build done"

2. **Engine runs in background thread or main thread?** Background thread with GTK dispatch for long-running builds. Main thread for short tasks. Callback-based — the handler decides.

3. **Should tasks.md be the source of truth or TaskStore?** TaskStore for runtime, tasks.md for persistence. Sync bidirectionally. Tasks.md is the ground truth on load; TaskStore is the ground truth during runtime.

4. **Max concurrent tasks?** Phase 1: sequential only. One task at a time. Parallel execution (multiple cylinders) is future work.

---

## 16. Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Gateway response correlation — engine doesn't know when agent finishes building | High | Use task ID in message prefix. Agent responds with task context. |
| Engine blocks GTK main thread | Medium | Run engine cycle in background thread, dispatch UI updates via GLib.idle_add |
| tasks.md format drift — agent edits it manually and breaks the parser | Medium | Parser is lenient — ignores sections it can't parse, preserves them on write |
| Test runner not configured for the project | Low | Default to `pytest`. Configurable in `.crabcakes/project.md` conventions section |
| Engine retries infinitely on stuck task | Low | Max 3 retries per task, then mark `blocked` and escalate to PM |
| Old command muscle memory — PM types `` `done 3 `` | Low | Graceful error: "Unknown command. Use `task done <id>`" |

---

*Upon approval, implementation follows the checkpoint discipline.*
