# CrabCakes Commands Reference

Slash commands typed in the project feed chat input — **not** shell commands; never run them in a terminal. All start with `/`: `/command args`, `@agent` mentions, `#task` refs, `— text` for a freeform body.

| Cmd | Args | What |
|-----|------|------|
| `/ask` | `@agent "q"` | Ask an agent a question. Alias `/a`. |
| `/stop` | (none) | Stop the current agent. |
| `/review` | (none) | Git checkpoint. |
| `/check` | (none) | Show diff since checkpoint. |
| `/accept` | `[--file path]` | Accept changes. |
| `/reject` | `— reason` | Reject all. |
| `/status` | `[--verbose]` | Project status. Alias `/s`. |
| `/agents` | (none) | List project agents. |
| `/cost` | (none) | Spending summary. |
| `/help` | `[command]` | Help. Alias `/?`. |

> The work-unit command set (`/work`, and the legacy `/task` `/tasks` `/start`
> `/done` `/blocked` `/cancel` `/assign` `/priority` aliases) was removed in
> SPEC-49. Those tokens now fall through to normal chat send like any
> unregistered command. Track work through specs, phases, and
> `.crabcakes/workflow.md` instead.
