# Agents in CrabCakes

CrabCakes has two categories of agents: **special agents** (local, running inside the app via `AgentRuntime`) and **gateway agents** (remote, running on an OpenClaw gateway server). This page covers special agents in detail. For gateway agents, see [Gateway](gateway.md).

---

## What Are Special Agents?

Special agents are built-in and user-defined AI assistants that run locally within CrabCakes. Each one has its own `AgentRuntime` instance, conversation history, tool set, and LLM provider configuration. They operate within your project directory and can read, write, search, and execute commands (with approval).

Special agents are defined by YAML files stored in `~/.config/crabcakes/agents/`. Built-in defaults (Coder, Debugger, Auxilium) are seeded automatically on first launch from `prompts/default_agents/`.

---

## Built-in Special Agents

### Coder (🛠️)

The Coder agent is your primary development assistant. It has full tool access — reading, writing, editing files, running commands (with PM approval), and web search/fetch.

- **Role:** `coder`
- **Session key:** `special:coder`
- **Tools:** `list_files`, `read_file`, `search_files`, `edit_file`, `write_file`, `exec_command`, `web_fetch`, `web_search`
- **Default provider:** Set via `llm_name` in `coder.yaml` (e.g. `MiniMax M2.7`)
- **MCP servers:** `memory` (configured by default)
- **Self-improvement:** All layers enabled — `bug_journal`, `project_rules`, `enforcement`, `structured_feedback`, `dream_consolidation`

The Coder requires an active project to work — it operates sandboxed to the project directory.

### Debugger (🐛)

The Debugger is a diagnostics and adversarial-audit agent. It can read files, run commands, search, fetch web content, and **write/edit files via write_file and edit_file** (never via exec_command heredocs — those create oversized approval cards). Use it for investigating bugs, tracing code paths, running diagnostics, and writing audit artifacts.

- **Role:** `debugger`
- **Session key:** `special:debugger`
- **Tools:** `read_file`, `exec_command`, `list_files`, `search_files`, `web_search`, `web_fetch`
- **Self-improvement:** `bug_journal` and `project_rules` enabled; `enforcement`, `structured_feedback`, and `dream_consolidation` disabled (no write access)

### Auxilium (🦀)

Auxilium is the always-on help agent. It answers questions from the local **knowledge base** (the files in `knowledge/*.md`), falling back to a remote LLM provider when the KB can't answer.

- **Role:** `helper`
- **Session key:** `special:auxilium`
- **Tools:** `list_files`, `read_file` (minimal — for safety)
- **Provider:** `local-kb` (the KB HTTP server on `127.0.0.1:18790`)
- **Fallback:** Configurable via `fallback_provider` / `fallback_model` in the agent YAML
- **Self-improvement:** `project_rules` only
- **Auto-open:** Yes (`auto_open: true`) — opens a tab on every app launch
- **Auto-add to projects:** Yes (`auto_add_to_projects: true`) — added to every new project as an onboarding guide

Auxilium does **not** require an active project — it's the only special agent that works without one.

---

## The Auxilium KB Provider System

### How It Works

When you ask Auxilium a question, the system follows a fallback chain:

1. **KB HTTP Server** (`local-kb` provider): Your question is sent to a local HTTP server at `http://127.0.0.1:18790/v1/chat/completions`. This server wraps a semantic search over the project's `knowledge/*.md` files.
2. **Semantic search**: The server embeds your question using the `BAAI/bge-small-en-v1.5` model (384-dimensional, ~130MB, runs on CPU), then computes cosine similarity against pre-built chunk embeddings stored in `knowledge/.index/`.
3. **Confidence check**: The top-K chunks (K=5) must pass a minimum score threshold of 0.35, and the best chunk must score at least 0.55. If not, the server returns `[KB_OUT_OF_SCOPE]`.
4. **Fallback**: If the KB returns `[KB_OUT_OF_SCOPE]`, the runtime retries the question using the configured `fallback_provider` and `fallback_model` — typically a remote LLM like OpenRouter.

### KB Index Files

The KB index lives in `knowledge/.index/`:
- `chunks.json` — list of `{id, source, section, text}` objects
- `embeddings.npy` — float32 numpy array, shape `(N, 384)`, L2-normalized

The index is built offline by `scripts/rebuild_kb_index.py` and committed to the repo. The server only starts if the index is available.

### KB Server Lifecycle

- Started by `AgentRuntimeHandler.__init__()` if the KB index exists
- Stopped by `AgentRuntimeHandler.stop_all()` on window shutdown
- Binds to `127.0.0.1` only — no external access
- Uses pure Python stdlib (`http.server`) — no external dependencies

---

## Creating Custom Agents

### Via the Agent Builder UI

The Agent Builder is a GTK4 dialog accessible from the sidebar. It lets you create and edit agent definitions visually.

**To create a new agent:**

1. Open the Agent Builder (typically via a button in the left panel or settings)
2. **Name**: Enter a display name (e.g. "Reviewer"). This becomes the tab title.
3. **Role**: Enter a role identifier (e.g. `reviewer`). This maps to `prompts/system/{role}.md` for the system prompt.
4. **Provider**: Select an LLM provider from the dropdown. Options come from `providers.yaml`. If no providers are configured, open Settings first.
5. **Fallback Provider/Model**: Visible only when the primary provider is `local-kb`. Lets you specify a remote fallback (e.g. OpenRouter) for questions the KB can't answer.
6. **System Prompts**: Check one or more `.md` files from `prompts/system/`. These are concatenated to form the agent's system prompt.
7. **Tools**: Check the tools this agent can use. Preset buttons:
   - **Full Access** — all tools enabled
   - **Read Only** — read_file, list_files, search_files, web_search, web_fetch
   - **Custom** — leave current state as-is
   Tools are grouped into categories: Read, Write, Execute, Web.
8. **MCP Servers**: Check any configured MCP servers (from `~/.config/crabcakes/mcp-servers.json`).
9. Click **Create**. The agent is saved to `~/.config/crabcakes/agents/{name}.yaml`.

**To edit an existing agent:** Click the edit button on an agent's tab. The form pre-fills with current values. Save writes back to the same YAML file.

The **Save button** is disabled until all four required fields are filled: name, at least one prompt, at least one tool, and a provider.

### Via YAML Files

Agent definitions are stored in `~/.config/crabcakes/agents/*.yaml` (or `.json`). You can create or edit these files directly.

#### Full YAML Field Reference

```yaml
# Required fields
name: Coder                    # Display name shown in the UI
emoji: "🛠️"                    # Avatar emoji
role: coder                    # Role identifier — maps to prompts/system/{role}.md
prompts:                       # List of prompt file paths (relative to prompts/)
  - system/coder.md
tools:                         # List of tool names this agent can use
  - list_files
  - read_file
  - search_files
  - edit_file
  - write_file
  - exec_command
  - web_fetch
  - web_search
llm_name: MiniMax M2.7         # Provider name (must exist in providers.yaml)

# Optional fields
model: MiniMax-M2.7            # Model override (defaults to provider's default_model)
fallback_provider: openrouter  # Used when primary provider returns [KB_OUT_OF_SCOPE]
fallback_model: owl-alpha      # Fallback model
mcp_servers:                   # MCP server names (from mcp-servers.json)
  - memory
self_improvement:              # Self-improvement layer toggles
  bug_journal: true
  project_rules: true
  enforcement: true            # Only meaningful if agent has write_file/edit_file
  structured_feedback: true
  dream_consolidation: true
auto_open: false               # Open a tab automatically on every app launch
auto_add_to_projects: false    # Auto-add to every new project's team
app_title: "Coder:Crabcakes"   # OpenRouter X-Title header
api_key_built_in: false        # Reserved — agent ships with embedded key
```

#### Self-Improvement Defaults

The `self_improvement` field defaults are determined by whether the agent has write tools:

```python
# From utils/agent_defs.get_default_si_config():
{
    "bug_journal": True,
    "project_rules": True,
    "enforcement": can_write,        # True if write_file or edit_file in tools
    "structured_feedback": False,
    "dream_consolidation": False,
}
```

Any keys you specify in the YAML override these defaults.

---

## Available Tools

All tools are defined in `agent/tools.py`. The `agent/` package is the only code that knows tool capabilities — all tools are data + pure functions, no GTK, no LLM calls.

### Tool Reference

| Tool | Approval | Description |
|------|----------|-------------|
| `read_file` | No | Read a file's text content (max 50KB; binary files return error). Supports `offset`/`limit` for partial reads. |
| `write_file` | No | Write content to a file (max 2MB). Creates parent directories. Overwrites entirely. |
| `edit_file` | No | Replace exact text in a file. `old_text` must be unique in the file. No regex, no fuzzy matching. |
| `exec_command` | **Yes** | Run a shell command in the project directory. PM must approve each call. Hardcoded blocklist rejects catastrophic commands (`rm -rf /`, `mkfs`, fork bombs). Default timeout 30s, max 120s. Output truncated at 100KB. |
| `list_files` | No | List directory contents. Supports recursive listing. Skips `.git`, `node_modules`, `__pycache__`. |
| `search_files` | No | Grep for a pattern across files. Supports `file_type` filter and `path` subdirectory. |
| `web_search` | No | Search the web via Brave Search API. Requires `BRAVE_API_KEY` environment variable. |
| `web_fetch` | No | Fetch a URL and extract readable text content. Strips HTML tags. Max 10,000 chars. |

### Path Sandboxing

All file operations are sandboxed to the project directory. Paths are resolved with `os.path.realpath()` and verified to stay within `project_path`. Any attempt to escape the sandbox (e.g. `../../etc/passwd`) is rejected.

### Group Tools: `diary` vs `project_memory` (do not confuse them)

Both persist notes, but they reach agents differently:

| | `diary` | `project_memory` |
|---|---|---|
| File | `.crabcakes/context.md` | `.crabcakes/memory/` (topic files + `INDEX.md`) |
| System prompt | **Yes** — read as the context block, capped at 8,000 chars (`utils/project_awareness.py` `CONTEXT_READ_CAP`), injected into every agent's awareness | **Bodies: never.** Only the `MEMORY_INDEX` (slug + one-line hook, ≤80 lines, fenced untrusted) rides the prompt via `{{MEMORY_INDEX}}` in `prompts/system/agent-tools.md` |
| Fetch model | always visible | on demand — the agent must call `read`/`search` |
| Capacity | 50-entry FIFO, 50KB write cap — it evicts | disk-backed; grows indefinitely |
| Use for | session/phase state, handoffs, PM directives — things every agent should *always* see | durable decisions, gotchas, patterns worth surviving many sessions |

Rule of thumb: if it should change agent behavior *this session*, diary; if it's institutional knowledge, project_memory. Because the index is the only broadcast channel into prompts, anything written there must be treated as untrusted data (see the project-memory audit: private-scope isolation + `_untrusted_fence`).

### exec_command Approval Flow

1. Agent calls `exec_command` with a shell command
2. The tool checks the hardcoded blocklist first (always denied)
3. If not blocked, the approval callback fires — this triggers a **pending-approval feed card** in the UI
4. The PM (you) clicks **Approve** or **Deny**
5. If approved, the command runs via `subprocess.run()` with `cwd=project_path`
6. Results return to the agent as a `ToolResult` with `stdout`, `stderr`, `exit_code`, and `output`

---

## MCP Server Integration

Agents can connect to MCP (Model Context Protocol) servers for extended tool access. MCP servers are configured in `~/.config/crabcakes/mcp-servers.json`.

### How MCP Works in CrabCakes

- MCP tool calls are namespaced as `server_name/tool_name` (e.g. `memory/store`)
- The runtime routes any tool name containing `/` to the MCP client
- MCP servers connect lazily — the first call triggers `connect()`, subsequent calls reuse the connection
- Connections are per-conversation (keyed by the agent's session key)
- On agent reload, all MCP connections are disconnected and re-established

### Configuring MCP Servers

```json
// ~/.config/crabcakes/mcp-servers.json
{
  "memory": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-memory"],
    "description": "Persistent memory server",
    "enabled": true
  }
}
```

In an agent's YAML, reference MCP servers by name:

```yaml
mcp_servers:
  - memory
```

Only enabled servers appear in the Agent Builder UI checkboxes.

---

## How Special Agents Differ from Gateway Agents

| Aspect | Special Agents | Gateway Agents |
|--------|---------------|----------------|
| **Execution** | Local (`AgentRuntime` in CrabCakes process) | Remote (on OpenClaw gateway server) |
| **Authentication** | None — they're local | Ed25519 device auth via WebSocket |
| **Tools** | 8 local tools (file, exec, web) | Gateway-managed tool set |
| **Provider config** | Per-agent `llm_name` in YAML | Configured on the gateway server |
| **System prompt** | `prompts/system/{role}.md` | Managed by gateway agent config |
| **Conversation** | In-memory + JSON persistence | Managed by gateway server |
| **Availability** | Always available (offline-capable) | Requires gateway connection |

See [Gateway](gateway.md) for details on gateway agents.

---

## Session Keys and Routing

Each special agent has a session key following the pattern `special:{role}`:
- `special:coder` — Coder agent
- `special:debugger` — Debugger agent
- `special:auxilium` — Auxilium agent

When agents are added to projects, their session keys are stored in `.crabcakes/team.json` and routed via `AgentRoutingTable`.

---

## Agent Runtime Architecture

Each named agent gets its own `AgentRuntime` instance for conversation isolation. The runtime:

1. Manages conversation state (messages, token counts, cost tracking)
2. Builds API messages and calls the LLM provider
3. Executes tool calls in sequence (LLM → tool → result → LLM → ...)
4. Streams SSE text responses back to the UI
5. Runs the enforcement layer after write operations
6. Tracks tool call history for stuck detection (same tool+args 3× = intervention)

All callbacks from the runtime are dispatched to the GTK main thread via `GLib.idle_add()`.

### Supported LLM Providers

Providers are resolved by explicit `caller` field in `providers.yaml`, or by model-prefix:

- **OpenAI** (`openai/*`)
- **MiniMax** (`minimax/*`)
- **Anthropic** (`anthropic/*`)
- **OpenRouter** (`openrouter/*`)
- **ZAI** (`zai/*`)
- **local-kb** — the KB HTTP server (no external API calls)

---

## Troubleshooting

### Agent shows "Open a project first"

Special agents (except Auxilium) require an active project. Open a project from the Projects tab first.

### Agent returns no content

This usually means the LLM provider is misconfigured. Check:
1. `providers.yaml` has the correct `base_url`, `api_key`, and `default_model`
2. The agent's `llm_name` matches a provider name in `providers.yaml`
3. For `local-kb`, ensure `knowledge/.index/` exists

### exec_command always denied

The approval callback requires a feed handler. If no feed cards appear, the UI may not be fully initialized. Also check that the command isn't on the hardcoded blocklist.

### Auxilium can't answer my question

The KB confidence threshold is 0.55. If your question doesn't closely match content in `knowledge/*.md`, the KB returns `[KB_OUT_OF_SCOPE]` and falls through to the fallback provider. Make sure `fallback_provider` is set in `auxilium.yaml`.
