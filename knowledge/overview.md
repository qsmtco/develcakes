# What Is DevelCakes?

DevelCakes is a GTK4 desktop application for multi-agent AI software development. It brings human developers and AI agents together in a real IDE-like environment — not just a chat window, but a workspace where agents can read files, write code, run commands, review diffs, and collaborate on projects.

**Platform:** Linux only (GTK4 + PyGObject). macOS and Windows are not supported.

**Tech stack:** Python 3.11+, GTK4 (via PyGObject), WebSocket client for OpenClaw gateway connectivity, Ed25519 device authentication.

---

## Why does DevelCakes exist?

Most AI coding tools are browser-based chat windows. You paste code in, you paste code out. DevelCakes exists because real software development needs more than that:

- **Agents need filesystem access** — they should be able to read your project, write files, run tests, and make git commits, not just suggest snippets.
- **Agents need to collaborate** — a Coder writes code, a Debugger analyzes it, a reviewer checks it. DevelCakes makes this a first-class workflow.
- **Developers need review control** — when an agent writes to your project, you should be able to see the diff, accept or reject it, before it lands. DevelCakes has a git-backed review layer for exactly this.
- **Context matters** — agents work better when they can see the project structure, understand team membership, and share awareness of what's happening.

DevelCakes is built for developers who want AI agents that participate in real projects, not just answer questions in a vacuum.

---

## What makes DevelCakes different from a chat app?

| Feature | Chat App (ChatGPT, etc.) | DevelCakes |
|---------|--------------------------|-----------|
| **Project tabs** | No project concept | Open multiple projects as tabs; each has its own team, feed, and chat |
| **File access** | Upload files manually | Agents read/write files directly from your project directory |
| **Code review** | Copy-paste diffs | File-change feed cards with accept/reject, plus standalone git-backed revert |
| **Agent collaboration** | One assistant | Multiple agents (Coder, Debugger, gateway agents) work in the same project |
| **Feed cards** | Linear chat | Structured feed cards for audit reports, file diffs, tool calls, and task assignments |
| **Multi-agent teams** | Not supported | Add/remove agents to project teams; fan-out messages to all members |
| **Slash commands** | Limited or none | Full command system: `/ask`, `/status`, and more |
| **Offline help** | Requires API | Auxilium answers from local knowledge base without any external LLM call |

---

## What are the three built-in special agents?

DevelCakes includes three special agents that run locally — no gateway required. They are defined in YAML files under `~/.config/develcakes/agents/` and seeded from `prompts/default_agents/` on first launch.

### 🛠️ Coder

The Coder is a full-stack code writing agent. It can:

- Read, write, and edit files in your project
- Execute shell commands (`exec_command`)
- Search files and the web
- Run with enforcement enabled (syntax check, test runner, lint check after writes)
- Track bugs in a bug journal and learn project-specific rules

**Tools:** `read_file`, `write_file`, `edit_file`, `exec_command`, `list_files`, `search_files`, `web_search`, `web_fetch`

**Default provider:** MiniMax (configurable)

### 🐛 Debugger

The Debugger is a diagnostics and adversarial-audit agent. It can:

- Read files and execute commands
- Write and edit files via write_file/edit_file (never exec_command heredocs)
- Search files and the web
- Analyze stack traces, run diagnostics, suggest fixes
- Track bugs in a bug journal

**Tools:** `read_file`, `exec_command`, `list_files`, `search_files`, `web_search`, `web_fetch`

**Default provider:** MiniMax (configurable)

### 🦀 Auxilium

Auxilium is the always-on help assistant and project onboarding guide. It can:

- Answer questions about DevelCakes itself (installation, configuration, features)
- Search and read files
- Fetch web pages
**Tools:** `read_file`, `list_files`, `write_file`, `web_search`, `web_fetch`

**Default provider:** none on fresh install — configure one in Settings → Providers (any preset works)

**Special properties:**
- `auto_open: true` — opens a tab automatically on every launch
- `auto_add_to_projects: true` — added to every project team automatically

---

## What's the difference between gateway agents and special agents?

| Aspect | Special Agents (Coder, Debugger, Auxilium) | Gateway Agents |
|--------|---------------------------------------------|----------------|
| **Where they run** | Locally inside DevelCakes | On a remote OpenClaw gateway |
| **How they connect** | Direct API calls to your configured LLM provider | WebSocket connection to `ws://localhost:18789` (default) |
| **Authentication** | Your API keys in `providers.yaml` | Ed25519 device auth via `~/.openclaw/identity/` |
| **Config location** | `~/.config/develcakes/agents/*.yaml` | Discovered dynamically from gateway |
| **Tools** | Defined in agent YAML | Provided by the gateway runtime |
| **Appearance** | Always available (even offline) | Appear in the Agents tab after connecting |

Both types can participate in project teams. You can mix gateway agents and special agents in the same project — messages fan out to all team members regardless of type.

---

## Who is DevelCakes for?

DevelCakes is for developers who:

- Want AI agents that can **actually work in their codebase** — not just suggest snippets, but open files, edit code, run tests, and make commits
- Work on **multiple projects** and want AI assistance scoped to each project's context
- Want **review control** over what agents write before it lands in their codebase
- Are already using (or interested in) **OpenClaw** and want a rich desktop client
- Prefer a **native Linux desktop app** over a browser-based tool
- Want **multi-agent collaboration** — different agents with different specialties working together

---

## What can I do with DevelCakes?

Here's a quick tour of major features:

### Project-based workflows
- Open a project directory as a tab
- DevelCakes creates a `.crabcakes/` directory with project config (team, workflow, context)
- Git is auto-initialized with an initial commit
- Add agents to the project team with `+` / `−` buttons
- Messages fan out to all team members; responses route back to the project tab

### Code review
- Agent file changes arrive as feed cards — these cards are the accept/reject surface
- Enforcement results appear in those cards' text but are not themselves actionable
- File diffs can also be reverted to an earlier commit from the file drawer
  (`ProjectHandler.revert_file_to_sha`; see knowledge/projects.md,
  "Checkpoint-Based Review — removed")

### Slash commands
- `/ask @agent question` — ask a specific agent something

### Prompt library
- Load `.md` files from `prompts/` as reusable prompts
- System prompts in `prompts/system/` define agent behavior templates
- Favorites, search, and usage tracking built in

### Activity tracking
- Collapsible Agents panel (collapsed by default) with one row per agent:
  colored dot, name and role, usage percent with a context meter, and a
  working/idle state, plus Compact and Clear buttons per agent
- Collapsed header shows a one-line summary ("name N% · …") per agent
- Shared activity pill on the project bar mirrors the live agent state machine
  (idle, sending, reasoning, streaming, tool_use, done)

### Voice input
- Push-to-talk speech-to-text via faster-whisper
- Configurable model size via `STT_MODEL_SIZE` environment variable

---

## Where does DevelCakes store its data?

| Path | Purpose |
|------|---------|
| `~/.config/develcakes/` | Main config directory (chmod 0o700) |
| `~/.config/develcakes/agent.json` | Legacy provider config (being replaced by `providers.yaml`) |
| `~/.config/develcakes/providers.yaml` | Canonical provider configuration (chmod 0o600) |
| `~/.config/develcakes/agents/*.yaml` | Agent definitions (Coder, Debugger, Auxilium, custom) |
| `~/.config/develcakes/favorites.json` | Prompt library favorites |
| `~/.config/develcakes/projects/` | Per-project config (members, etc.) |
| `$CRABCAKES_PROJECTS_DIR` (default: `~/projects`) | Browsable project root for the file tree |
| `~/.openclaw/identity/` | OpenClaw device identity (Ed25519 keys) |
| `.crabcakes/` (per project) | Project-specific team, workflow, and context data |

---

## How is DevelCakes different from other AI IDEs?

Most AI IDEs (Cursor, GitHub Copilot, etc.) integrate AI as a feature inside an existing editor. DevelCakes takes a different approach:

- **Agent-first, not editor-first** — The primary interface is agent interaction, not a code editor. Agents do the editing; you review and direct.
- **Multi-agent, not single-assistant** — Multiple specialized agents work simultaneously in each project, each with distinct roles and tools.
- **Local-first architecture** — Special agents run locally with direct API calls. No need for a complex server setup just to get started.
- **OpenClaw integration** — Connects to the OpenClaw gateway for remote agent teams, device auth, and cross-machine workflows.

---

## What if I just want to chat with an AI?

That works too! Open the Auxilium 🦀 tab and start typing. Auxilium answers through whichever provider you've configured (Settings → Providers). You don't need a project open or a gateway connection to talk to Auxilium.
