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

## Feed cards

Post to the project Feed with the `feed_card` tool. Never write a `crabcard`
block — that format is gone.

- `type` — `diff` when you change a file, `agent_action` for a decision or a
  finding, `audit_report` for an audit result.
- `title` — one line, max 200 characters. `body` — the card text, max 100,000.
- `file` — project-relative path. `additions` / `deletions` — line counts.
  `task_id` — optional work unit id.
- The tool returns the new card id. A rejected call returns an error — read it
  and fix the field; do not retry the same arguments unchanged.
- Do **not** post for `file_created/modified/deleted` (CrabWatch detects those),
  `git_commit` (automatic), or `system` (internal). Those types are not accepted.
- `diff` body: unified diff, `+`/`-` prefixes, context lines unprefixed.
