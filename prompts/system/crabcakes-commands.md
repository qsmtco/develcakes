# CrabCakes Commands Reference

Slash commands typed in the project feed chat input — **not** shell commands; never run them in a terminal. All start with `/`: `/command args`, `@agent` mentions, `#task` refs, `— text` for a freeform body.

| Cmd | Args | What |
|-----|------|------|
| `/ask` | `@agent "q"` | Ask an agent a question. Alias `/a`. |
| `/delegate` | `@agent "task"` | Assign work. Alias `/d`. |
| `/tell` | `@agent "info"` | Share info; no response expected. |
| `/stop` | (none) | Stop the current agent. |
| `/work` | `subcommand …` | Work Unit commands (below). |
| `/review` | (none) | Git checkpoint. |
| `/check` | (none) | Show diff since checkpoint. |
| `/accept` | `[--file path]` | Accept changes. |
| `/reject` | `— reason` | Reject all. |
| `/status` | `[--verbose]` | Project status. Alias `/s`. |
| `/agents` | (none) | List project agents. |
| `/cost` | (none) | Spending summary. |
| `/help` | `[command]` | Help. Alias `/?`. |

## Work Units (`/work`)

State lives in `.crabcakes/work.json`; `.crabcakes/tasks.md` is a generated summary, not a source of truth.

Subcommands: `/work "Title"` (create, status `draft`) · `/work list` · `/work start #N` (validate the spec file + deps, then hand off to the Supervisor) · `/work done #N — notes` · `/work blocked #N — reason` · `/work unblock #N` (clear a blocker, restore to spec-ready; PM/Supervisor only) · `/work cancel #N` · `/work assign #N @agent` (supervisor/builder/auditor) · `/work priority #N level` (low/med/high/critical) · `/work spec-ready #N <path>` (validate the spec path; PM/Supervisor only) · `/work status #N`

**There is no `/task add` — that command does not exist.** `@mentions` in a command route to a specific agent; they do **not** trigger @mention notifications.

### Legacy aliases

The legacy task-centric commands are still accepted and route to the Work Handler: `/task` → `/work` (create), `/tasks` → `/work list`, `/start` → `/work start`, `/done` → `/work done`, `/blocked` → `/work blocked`, `/cancel` → `/work cancel`, `/assign` → `/work assign`, `/priority` → `/work priority`.

The legacy `/t` alias for `/task` is **gone** (SPEC-TASK-SYSTEM-FULL-REDESIGN §5.1) — use `/work`.

## Crabcards

Emit a crabcard when you modify a project file, so the PM can review it in the Project Feed.

    ```crabcard
    type: diff
    title: Short description
    file: path/to/file.py
    additions: 5
    deletions: 2
    ---
    - old line
    + new line
    ```

- **Required:** `type` (always `diff` when changing files) and `title`.
- **Optional:** `file`, `additions`, `deletions`, `commit_sha`, `task_id`.
- Emit `diff` for a write **or** a delete (show the removed lines). `agent_action` is optional for task start/complete, decisions, and findings.
- Do **not** emit for `file_created/modified/deleted` (CrabWatch detects those), `git_commit` (auto), or `system` (internal).
- Diff body: unified diff, `+`/`-` prefixes, context lines unprefixed.
