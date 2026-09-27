# Features Guide

CrabCakes provides a rich set of features for multi-agent development. This guide covers each feature in detail.

## Multi-Agent Collaboration in Project Tabs

### How Project Tabs Work

When you open a project from the left panel's Projects tab, a shared chat tab is created. All project members (agents you have added) can see each other's responses in this shared space.

Messages sent in a project tab are **broadcast** to all project members via fan-out. Each member receives the message and responds independently. Responses are routed back to the project tab.

### Team Management

Click **+** (green) or **−** (red) on agent rows in the left panel's Agents tab to add or remove agents from the active project. Membership is persisted per-project.

- **`+`** adds the agent to the project — they receive broadcast messages
- **`−`** removes the agent — they stop receiving project messages

Team membership is managed by `ProjectHandler` and stored in `.crabcakes/team.json`.

### Solo DM Mode

Right-click a project tab to open the project member menu. Select a single agent to switch to **solo DM mode** — only that agent receives your messages. Select "All" to return to broadcast mode.

Solo DM state is tracked per-project in `ProjectHandler._solo_targets`.

### Agent Routing

Agent-to-project routing is managed by `AgentRoutingTable` (in `models/routing.py`). When a gateway agent responds, `ChatHandler` looks up which project the agent belongs to and routes the response to the correct project tab.

Special agents (Coder, Debugger, custom agents) are routed through `AgentRuntimeHandler.send_to_special_agent()` instead of the gateway, but the project tab routing works the same way.

---

## Code Review System

CrabCakes includes a git-backed code review system for agent file writes. When review mode is enabled, agent code changes are tracked through a checkpoint → diff → accept/reject workflow.

### Review Lifecycle

1. **`/review`** — Creates a git checkpoint (auto-commits current state). Review mode activates and a ReviewBar widget appears above the chat.
2. **Agent writes code** — The Coder or other file-writing agents make changes to project files.
3. **`/check`** — Shows the diff of all changes since the checkpoint, file by file.
4. **`/accept`** — Keeps all changes (commits them). Or `/accept <filename>` for a single file.
5. **`/reject`** — Reverts all changes to the checkpoint state via `git checkout`. A rejection message is sent to all project member agents.
6. **`/review stop`** — Ends the review session and removes the ReviewBar.

### Review State

Review state is tracked per-project in `ReviewState` dataclass instances (from `models/review_state.py`):

- `review_mode`: `"off"` or `"review"`
- `checkpoint_sha`: The git commit SHA at checkpoint time
- `is_dirty`: Whether changes exist since the checkpoint
- `last_check_files`: List of files changed since last `/check`

### Review Bar

The `ReviewBar` widget (in `ui/views/review_bar.py`) provides a dropdown to toggle review mode and buttons to start review, check changes, accept, or reject.

---

## Feed Cards

The project feed is a visual timeline of events in a project tab. Feed cards are managed by `FeedHandler` (in `ui/handlers/feed_handler.py`) and rendered by `feed_card.py`.

### Card Types

| Type | Source | Description |
|------|--------|-------------|
| `git_commit` | git | Shows commit SHA, message, and files changed |
| `file_edit` | agent | Shows file path and edit details |
| `review` | review | Shows review events (accept/reject) |
| `task` | agent | Shows task creation and status updates |
| `audit_report` | agent | Shows structured audit report with severity |

### Feed Persistence

Feed cards are persisted to `.crabcakes/feed.json` per project. On project reopen, the feed is restored from disk.

### CrabCard Blocks

Agents can emit structured feed cards by including ` ```crabcard ` code blocks in their responses. These are parsed by `utils/crabcard_parser.py:extract_crabcards()` into `FeedCardData` instances.

---

## Activity Indicators and Drawer

### Activity Pill (formerly FeedBar / Response Status Bar)

Activity status renders as a per-tab **activity pill** on each chat surface
(`ui/views/chat_surface.py` — `set_activity_status(text, state)` + CSS class swap).
The old `FeedBar` widget (`ui/views/feedbar.py`) was deleted in SPEC-07 (R4);
`ActivityHandler` now renders through the `status_target` duck-type
(`ActivityPillAdapter`), which resolves the active tab's surface.

### Activity Handler

`ActivityHandler` (in `ui/handlers/activity_handler.py`) implements a 6-state activity machine that tracks what each agent is doing:

1. **Idle** — No activity
2. **Thinking** — Agent is processing (LLM call in progress)
3. **Tool Call** — Agent is executing a tool
4. **Streaming** — Agent response is streaming
5. **Complete** — Agent finished responding
6. **Error** — An error occurred

### Activity Drawer

The `ActivityDrawer` (in `ui/views/activity_drawer.py`) is a collapsible panel below the chat that displays detailed activity events:

- Tool calls with names and arguments
- Plan summaries
- Approval requests
- Command output (click to expand last 10 lines)
- Patches and diffs
- Lifecycle separators with per-agent stats

**Counter-collapse:** Consecutive events with the same `(agent_name, activity_type)` are merged — count increments and duration sums. The first event of a new pair opens a new row.

**Filters:** Two dropdowns (agent, type) with AND semantics filter the event list. Empty set = all pass.

---

## Self-Improvement System

CrabCakes agents can learn from their mistakes and adapt to your project over time.

### Bug Journal

Each agent maintains a bug journal at `.crabcakes/{role}-bugs.md` (e.g. `.crabcakes/coder-bugs.md`). When the Debugger agent produces an audit report with severity `bug`, the report is automatically appended to the target agent's bug journal.

Entries follow this format:

```markdown
## Bug #3 — 2025-06-14 — src/parser.py:42

**Task:** Parse configuration file
**Mistake:** Used `int()` instead of `float()` for decimal config values
**Expected:** `3.14` as a float
**Actual:** `3` (truncated to int)
**Lesson:** Always check the expected type before parsing
**Pattern:** type-mismatch

---
```

Bug journals are loaded into the agent's system prompt via `compose_system_prompt()` so the agent sees past mistakes when working on new tasks.

### Project Rules

Per-project rule files at `.crabcakes/{role}-rules.md` define project-specific coding standards and rules. These are composed into the system prompt alongside bug journals.

### Enforcement

The enforcement system (in `agent/enforcement.py`) runs post-write verification checks:

1. **Syntax guard** — Verifies Python files parse without syntax errors after writes
2. **Test runner** — Runs the project's test suite and checks for regressions
3. **Lint check** — Runs configured linters on changed files

Enforcement configuration is stored in `.crabcakes/enforcement.json` per project. The `EnforcementConfig` dataclass (from `agent/config.py`) controls which tiers are enabled.

### Structured Feedback

When an agent (typically Debugger) produces `## Audit Report` sections in its response, `utils/feedback_processor.py:process_audit_reports()` processes them:

1. **Extract** — `utils/audit_parser.py:extract_audit_reports()` parses the reports
2. **Classify** — Reports are classified by severity (`bug`, `issue`, `suggestion`)
3. **Append** — Bug-severity reports are appended to the target agent's bug journal

### Dream Consolidation

Periodic review of bug journals identifies recurring patterns and consolidates them into higher-level lessons. This helps agents recognize and avoid categories of mistakes, not just specific instances.

---

## Improve Prompt (💡)

The Improve Prompt button rewrites your draft message for better AI responses. It sends your text to the MiniMax API with a system prompt template from `prompts/system/improve.md`.

### How It Works

1. Click the 💡 button next to the chat input
2. Your draft text is injected into the template at the `{{USER_INPUT}}` marker
3. The assembled prompt is sent to MiniMax (`MiniMax-M2.5-Lightning` model)
4. The improved version replaces your input text

The template instructs the LLM to expand vague prompts into detailed, specific instructions with context, constraints, and expected output format.

---

## Speech-to-Text (Ctrl+Space)

Push-to-talk voice input powered by `faster-whisper` (CTranslate2 + int8 CPU).

### How It Works

1. Hold `Ctrl+Space` to start recording — the button label changes to "🔴 Recording"
2. Release to stop — audio is transcribed via faster-whisper
3. The transcript is appended to the chat input

### Configuration

The model size is controlled by the `STT_MODEL_SIZE` environment variable (default: `tiny.en`):

| Model | Size | Speed | Accuracy |
|-------|------|-------|----------|
| `tiny.en` | ~75MB | Fastest | Basic English |
| `base.en` | ~145MB | Fast | Good English |
| `small.en` | ~480MB | Moderate | Better English |

Audio is captured at 16kHz mono via `arecord` (ALSA). The model is loaded lazily on first use and cached for all subsequent calls.

---

## Project Context Injection

When a project is active, agents automatically receive rich context about the project through the system prompt composition system (`utils/prompt_loader.py:compose_system_prompt()`).

### Context Sources

| File | Purpose |
|------|---------|
| `.crabcakes/project.md` | Project description, metadata, tech stack |
| `.crabcakes/workflow.md` | Current workflow phase and status |
| `.crabcakes/context.md` | Recent activity log |
| `.crabcakes/context-snapshot.json` | Structured state snapshot (git state, file listing) |
| `.crabcakes/team.json` | Team membership and agent assignments |
| `.crabcakes/{role}-bugs.md` | Agent's bug journal (self-improvement) |
| `.crabcakes/{role}-rules.md` | Project-specific rules for the agent |

### Prompt Composition Order

The system prompt is assembled in layers:

1. `prompts/system/default.md` — Base instructions
2. `prompts/system/collab.md` — Collaboration protocol
3. `prompts/system/crabcakes-context.md` — CrabCakes-specific context
4. `prompts/system/project-awareness.md` — Project awareness block
5. `prompts/system/crabcakes-commands.md` — Command reference
6. `prompts/system/project-onboarding.md` — Onboarding context
7. `prompts/system/code-review.md` — Code review protocol
8. Role-specific prompt (e.g. `coder.md`, `debugger.md`)
9. Role-specific bugs and rules files

---

## KB Provider and Fallback Chain

### Local KB Provider

CrabCakes includes a built-in knowledge base (KB) provider that works without any external LLM API key. This is the **local-kb provider**, which serves answers from the indexed CrabCakes documentation.

### How Auxilium Works Without an LLM

On fresh install, the `ensure_kb_provider()` function (in `utils/providers_store.py`) automatically:

1. Seeds a `local-kb` provider entry into `providers.yaml`
2. Patches the Auxilium (helper) agent to use `local-kb` as its primary provider

The KB HTTP server (`agent/kb_server.py`) runs on `localhost:18790` and presents an OpenAI-compatible `/v1/chat/completions` endpoint. When Auxilium receives a question, it calls the KB server just like any other LLM provider.

### KB Lookup

The KB server uses `agent/kb_lookup.py` for retrieval:
- Embeds the user's question using `BAAI/bge-small-en-v1.5` (384-dim, ~130MB)
- Performs cosine similarity against pre-indexed KB chunks
- Returns the top 5 chunks with score ≥ 0.35
- Requires top score ≥ 0.55 (confidence threshold) to return results

### Fallback Chain

When the KB server cannot answer a question (score below confidence threshold), it returns the sentinel `[KB_OUT_OF_SCOPE]`. If a fallback provider is configured, the runtime automatically:

1. Detects the `[KB_OUT_OF_SCOPE]` sentinel in the response
2. Swaps the conversation model to the fallback provider
3. Retries the LLM call with the external provider
4. Restores the original model after the fallback call

The one-shot guard (`conv._fallback_attempted`) prevents infinite fallback loops.

See the dedicated [kb-provider.md](kb-provider.md) guide for full details.

---

## MCP Server Integration

CrabCakes supports Model Context Protocol (MCP) servers for extending agent capabilities with external tools.

### Configuration

MCP servers are configured in `~/.config/crabcakes/mcp-servers.json`:

```json
{
  "filesystem": {
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/projects"],
    "env": {}
  }
}
```

### How It Works

- `utils/mcp_config.py` loads and validates server configurations
- `utils/mcp_client.py` provides an asyncio-to-threading bridge with connection pooling
- MCP tools are merged into the agent's tool list via `get_tools_for_api()`
- Each agent can have its own set of MCP servers (configured in the agent YAML)
- Connections are pre-warmed on agent registration and cleaned up on shutdown

---

## Agent Builder

The Agent Builder is a modal dialog for creating and editing custom agents. It is implemented in `ui/views/agent_builder.py` (view) and `ui/handlers/agent_builder_handler.py` (logic).

### Creating a Custom Agent

1. Click the **+** button in the Agents tab or use the agent builder button
2. Fill in the agent definition:
   - **Name** — Display name for the agent
   - **Role** — Agent role identifier (e.g. `coder`, `debugger`, `helper`, `tester`)
   - **System Prompt** — Custom instructions for the agent
   - **LLM Provider** — Which provider to use (dropdown from configured providers)
   - **Model** — Specific model identifier
   - **API Key** — Optional per-agent API key override
   - **Fallback Provider** — Provider to use when KB returns out-of-scope
   - **Tools** — Which tools the agent can use (read_file, write_file, edit_file, exec_command, etc.)
   - **MCP Servers** — MCP server configurations for external tools
3. Click **Save** — The agent is saved to `agents/*.yaml` and immediately available

### Editing an Agent

Click the edit button on an existing agent card to modify its configuration. Changes take effect immediately for new conversations — existing conversations update their model and API key on the next message.

### Deleting an Agent

Use the delete button with confirmation. The agent definition is removed from the YAML file.

### Agent YAML Format

Agent definitions are stored in `agents/*.yaml`:

```yaml
name: My Custom Agent
role: coder
llm_name: openrouter
model: openrouter/auto
api_key: ""
tools:
  - read_file
  - write_file
  - edit_file
  - exec_command
auto_open: true
auto_add_to_projects: true
```
