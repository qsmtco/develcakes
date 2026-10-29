# What Is CrabCakes?

CrabCakes is a GTK4 desktop application for multi-agent AI software development. It brings human developers and AI agents together in a real IDE-like environment — not just a chat window, but a workspace where agents can read files, write code, run commands, review diffs, and collaborate on projects.

**Platform:** Linux only (GTK4 + PyGObject). macOS and Windows are not supported.

**Tech stack:** Python 3.11+, GTK4 (via PyGObject), WebSocket client for OpenClaw gateway connectivity, Ed25519 device authentication.

---

## Why does CrabCakes exist?

Most AI coding tools are browser-based chat windows. You paste code in, you paste code out. CrabCakes exists because real software development needs more than that:

- **Agents need filesystem access** — they should be able to read your project, write files, run tests, and make git commits, not just suggest snippets.
- **Agents need to collaborate** — a Coder writes code, a Debugger analyzes it, a reviewer checks it. CrabCakes makes this a first-class workflow.
- **Developers need review control** — when an agent writes to your project, you should be able to see the diff, accept or reject it, before it lands. CrabCakes has a git-backed review layer for exactly this.
- **Context matters** — agents work better when they can see the project structure, understand team membership, and share awareness of what's happening.

CrabCakes is built for developers who want AI agents that participate in real projects, not just answer questions in a vacuum.

---

## What makes CrabCakes different from a chat app?

| Feature | Chat App (ChatGPT, etc.) | CrabCakes |
|---------|--------------------------|-----------|
| **Project tabs** | No project concept | Open multiple projects as tabs; each has its own team, feed, and chat |
| **File access** | Upload files manually | Agents read/write files directly from your project directory |
| **Code review** | Copy-paste diffs | Enforcement results as feed cards with accept/reject, plus standalone git-backed revert |
| **Agent collaboration** | One assistant | Multiple agents (Coder, Debugger, gateway agents) work in the same project |
| **Feed cards** | Linear chat | Structured feed cards for audit reports, file diffs, tool calls, and task assignments |
| **Multi-agent teams** | Not supported | Add/remove agents to project teams; fan-out messages to all members |
| **Slash commands** | Limited or none | Full command system: `/ask`, `/status`, and more |
| **Offline help** | Requires API | Auxilium answers from local knowledge base without any external LLM call |

---

## What are the three built-in special agents?

CrabCakes includes three special agents that run locally — no gateway required. They are defined in YAML files under `~/.config/crabcakes/agents/` and seeded from `prompts/default_agents/` on first launch.

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

- Answer questions about CrabCakes itself (installation, configuration, features)
- Search and read files
- Fetch web pages
- Respond using the **local knowledge base** — no external LLM required

**Tools:** `read_file`, `list_files`, `write_file`, `web_search`, `web_fetch`

**Default provider:** `local-kb` (the built-in KB server on `localhost:18790`)

**Special properties:**
- `auto_open: true` — opens a tab automatically on every launch
- `auto_add_to_projects: true` — added to every project team automatically

---

## What's the difference between gateway agents and special agents?

| Aspect | Special Agents (Coder, Debugger, Auxilium) | Gateway Agents |
|--------|---------------------------------------------|----------------|
| **Where they run** | Locally inside CrabCakes | On a remote OpenClaw gateway |
| **How they connect** | Direct API calls to your configured LLM provider | WebSocket connection to `ws://localhost:18789` (default) |
| **Authentication** | Your API keys in `providers.yaml` | Ed25519 device auth via `~/.openclaw/identity/` |
| **Config location** | `~/.config/crabcakes/agents/*.yaml` | Discovered dynamically from gateway |
| **Tools** | Defined in agent YAML | Provided by the gateway runtime |
| **Appearance** | Always available (even offline) | Appear in the Agents tab after connecting |

Both types can participate in project teams. You can mix gateway agents and special agents in the same project — messages fan out to all team members regardless of type.

---

## How does the KB provider system work?

One of CrabCakes' standout features is that the Auxilium agent can answer questions **without calling an external LLM**. Here's how:

### The local knowledge base

1. **KB files** — Markdown files in `knowledge/` (this directory) contain documentation: install guides, configuration references, feature explanations, etc.
2. **Embedding index** — `scripts/rebuild_kb_index.py` chunks each KB file, embeds each chunk using the `BAAI/bge-small-en-v1.5` sentence-transformer model (384-dim, runs on CPU, ~130MB), and saves to `knowledge/.index/`:
   - `chunks.json` — text chunks with source and section metadata
   - `embeddings.npy` — float32 array, shape (N, 384), L2-normalized
3. **KB HTTP server** — `agent/kb_server.py` starts a localhost HTTP server on port `18790` that mimics the OpenAI `/v1/chat/completions` API. When queried, it:
   - Embeds the incoming question using the same model
   - Computes cosine similarity against all indexed chunks
   - Returns the top-5 chunks above a minimum score threshold (0.35, with a confidence threshold of 0.55 on the top result)
   - If no chunks clear the thresholds, returns `[KB_OUT_OF_SCOPE]`
4. **Provider registration** — On startup, `ensure_kb_provider()` in `utils/providers_store.py` seeds a `local-kb` entry into `providers.yaml`:
   ```yaml
   - name: local-kb
     base_url: http://localhost:18790/v1
     api_key: "***"
     default_model: local-kb
     caller: openai
     supports_tools: false
     supports_streaming: false
     max_tokens: 4096
   ```
5. **Agent wiring** — The same `ensure_kb_provider()` call patches the Auxilium agent's `llm_name` to `local-kb` if it doesn't already have a provider set.

This means a fresh install of CrabCakes can answer "how do I install this?" or "how do I configure a provider?" immediately, before the user has added any API keys.

### Fallback to external LLM

For questions the KB can't answer (out-of-scope), Auxilium can fall back to an external LLM provider. This is controlled by the `fallback_provider` and `fallback_model` fields in `AgentConfig` and the agent YAML.

---

## Who is CrabCakes for?

CrabCakes is for developers who:

- Want AI agents that can **actually work in their codebase** — not just suggest snippets, but open files, edit code, run tests, and make commits
- Work on **multiple projects** and want AI assistance scoped to each project's context
- Want **review control** over what agents write before it lands in their codebase
- Are already using (or interested in) **OpenClaw** and want a rich desktop client
- Prefer a **native Linux desktop app** over a browser-based tool
- Want **multi-agent collaboration** — different agents with different specialties working together

---

## What can I do with CrabCakes?

Here's a quick tour of major features:

### Project-based workflows
- Open a project directory as a tab
- CrabCakes creates a `.crabcakes/` directory with project config (team, workflow, context)
- Git is auto-initialized with an initial commit
- Add agents to the project team with `+` / `−` buttons
- Messages fan out to all team members; responses route back to the project tab

### Code review
- Agents' writes produce enforcement results; problems surface as feed cards
- Accept or reject each change right on its feed card
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

## Where does CrabCakes store its data?

| Path | Purpose |
|------|---------|
| `~/.config/crabcakes/` | Main config directory (chmod 0o700) |
| `~/.config/crabcakes/agent.json` | Legacy provider config (being replaced by `providers.yaml`) |
| `~/.config/crabcakes/providers.yaml` | Canonical provider configuration (chmod 0o600) |
| `~/.config/crabcakes/agents/*.yaml` | Agent definitions (Coder, Debugger, Auxilium, custom) |
| `~/.config/crabcakes/favorites.json` | Prompt library favorites |
| `~/.config/crabcakes/projects/` | Per-project config (members, etc.) |
| `$CRABCAKES_PROJECTS_DIR` (default: `~/projects`) | Browsable project root for the file tree |
| `~/.openclaw/identity/` | OpenClaw device identity (Ed25519 keys) |
| `.crabcakes/` (per project) | Project-specific team, workflow, and context data |

---

## How is CrabCakes different from other AI IDEs?

Most AI IDEs (Cursor, GitHub Copilot, etc.) integrate AI as a feature inside an existing editor. CrabCakes takes a different approach:

- **Agent-first, not editor-first** — The primary interface is agent interaction, not a code editor. Agents do the editing; you review and direct.
- **Multi-agent, not single-assistant** — Multiple specialized agents work simultaneously in each project, each with distinct roles and tools.
- **Local-first architecture** — Special agents run locally with direct API calls. No need for a complex server setup just to get started.
- **OpenClaw integration** — Connects to the OpenClaw gateway for remote agent teams, device auth, and cross-machine workflows.
- **KB-backed help** — The built-in help agent works offline using a local embedding index — no API key needed for basic help.

---

## What if I just want to chat with an AI?

That works too! Open the Auxilium 🦀 tab and start typing. If your KB index is built, it'll answer from local docs. If you've configured an external provider, it can use that too. You don't need a project open or a gateway connection to talk to Auxilium.
