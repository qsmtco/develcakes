# CrabCakes Commands Reference

Slash commands typed in the project feed chat input — **not** shell commands; never run them in a terminal. All start with `/`. A quoted payload uses ASCII double quotes: `/ask @agent "question"`.

| Cmd | Args | What |
|-----|------|------|
| `/ask` | `@agent "question"` | Ask an agent a question. Alias `/a`. |
| `/stop` | `@agent` | Stop that agent. |
| `/clear` | `@agent` | Clear that agent's conversation and reset its step count. |
| `/compact` | `@agent` | Compact that agent's conversation. |
| `/status` | `[--verbose]` | Project status. Alias `/st`. |
| `/agents` | (none) | List project agents. |
| `/cost` | (none) | Spending summary. |
| `/session` | `list @agent` or `<ref> @agent` | Switch an agent's session. Alias `/s`. |
| `/help` | `[command]` | Help. Alias `/?`. |

> The work-unit command set (`/work`, and the legacy `/task` `/tasks` `/start`
> `/done` `/blocked` `/cancel` `/assign` `/priority` aliases) was removed in
> SPEC-49. Those tokens now fall through to normal chat send like any
> unregistered command. Track work through specs, phases, and
> `.crabcakes/workflow.md` instead.
