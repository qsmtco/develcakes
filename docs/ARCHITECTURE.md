# Crabcakes — Architecture Document

**Purpose:** This document is the authoritative reference for the Crabcakes codebase. It defines the structure, patterns, and principles that all contributors — human or agent — must follow. Before writing any code, read this document. When in doubt, consult this document.

**Project root:** `/path/to/projects/crabcakes`
**Project status:** See `docs/PROJECT_STATUS.md` for current progress, completed phases, and planned work.

---

## 0. Keeping This Document Current

**This document is the law — and like the law, it must be kept current.**

When you change code, you **must** update this document in the same commit. If you don't, it becomes a lie, and future contributors will trust it and make wrong decisions.

**What to update after any code change:**

| If you... | Then update... |
|-----------|--------------|
| Add/remove/rename a module | Section 2 (directory structure) and Section 11 (file inventory) |
| Change a class's public API or responsibilities | Section 3 (module responsibilities) |
| Add/remove a public function or method | Section 3 (public API blocks) |
| Change environment variables | Section 10 |
| Change gateway protocol handling | Section 11 (protocol reference) |
| Change how events flow through the app | Section 4 (data flow) |
| Change a pattern or convention | Sections 5–7 |
| Change environment variables | Section 10 |

**Rule:** If the diff of your code change doesn't have a corresponding update to this file, the change is **incomplete**.

**Exception:** Minor refactors where nothing documented externally changes (e.g., renaming internal variables, extracting private methods, inlining simple helper functions) do not require ARCHITECTURE.md updates.

---

## 1. Project Overview

Crabcakes is a GTK4 desktop application that connects to an OpenClaw gateway via WebSocket, enabling multi-agent chat management. It provides:

- A split-panel UI: left sidebar (Prompts/Agents/Projects notebook) + right main content (chat tabs + input)
- Prompt library: load `.md` files from the `prompts/` directory (system prompts in `prompts/system/`)

### Per-Project Prompt Library (SPEC-PROJECT-PROMPTS-DIRECTORY)

Crabcakes maintains a **two-tier prompt storage** architecture:

| Tier | Location | Content | Consumers |
|------|----------|---------|-----------|
| App-level | `<crabcakes>/prompts/system/` | Agent personality templates (`coder.md`, `debugger.md`, etc.) | `utils/prompt_loader.py` → system prompt composition |
| Per-project | `<project>/.crabcakes/prompts/` | User-facing prompt library (`README.md`, `steelFramedCodeWriter.md`, subdirs) | `PromptsHandler` tab, `InputToolbarHandler` picker, agent file context |

**Seeding:** `seed_project_prompts(project_path)` (in `utils/project_awareness.py`) copies the app's user-facing prompt library into `<project>/.crabcakes/prompts/` on project create and on first open of legacy projects. Copy-only-if-missing semantics: existing project edits are never overwritten.

**Resolution:** `get_project_prompts_dir(project_path)` (`utils/prompt_paths.py`) returns the per-project dir when it exists, otherwise falls back to the app-level `prompts/` directory. This ensures agents in a project can read `prompts/steelFramedCodeWriter.md` via the sandbox-resolved project path.

**Write-side resolver:** `ensure_project_prompts_dir(project_path)` (also in `utils/prompt_paths.py`) is the write-side counterpart: unseeded-project writes CREATE `<project>/.crabcakes/prompts/` rather than falling back to the app-level library, so a user's saved/imported prompt always lands in the active project (Phase 3 audit BUG #1). Callers: `InputToolbarHandler.save_as_prompt`, `PromptsHandler.import_prompt`.

**Agent file context:** `agent/context.py:_load_project_prompts_context()` injects up to 30 `.md` files (≤20KB each) from `.crabcakes/prompts/` into the agent's file context as `## .crabcakes/prompts/{stem}` sections, appearing after the core `.crabcakes/` docs and before the project tree/key files; the size filter runs BEFORE the 30-file cap so oversized files never consume slots. This makes the prompt library visible to the agent without requiring an explicit `read_file` tool call.

**Favorites:** `utils/favorites.py` keys favorites by **stem** (not full path), so a favorited prompt persists across project switches and re-seeds.

**Out of scope:** `prompts/system/*` (app-level agent personality) and `prompts/claude-code-clean/` (human reference only) are never seeded into projects.

- Agent discovery: connect to gateway, discover agents, open chat tabs per agent
- Project browser: browse directories from `CRABCAKES_PROJECTS_DIR` via TreeView
- **Project group chat**: open a project → fan-out message to all project members → responses routed back to the project tab
- **Membership toggles**: +/− buttons in the Agents tab add/remove agents from the active project
- **Crabcake Special Agents**: local agent runtime — Coder and Debugger agents run directly against OpenAI/MiniMax/Anthropic APIs with file/exec tools, no gateway required
- **Review layer**: git-backed code review for agent writes — checkpoint → diff → accept/reject

**Technology stack:**
- Python 3, GTK4 (via PyGObject)
- WebSocket client (Python `websockets` library)
- Ed25519 device authentication (via `cryptography`)
- Threaded async I/O with GLib main thread dispatch

---

## 2. Directory Structure

```
crabcakes/
├── main.py                    # Entry point — creates CrabcakesApp, runs Gtk.main() (~56 lines)
│
├── gateway/                   # WebSocket client — self-contained, no UI dependencies
│   ├── __init__.py           # Exports: GatewayClient, SnapshotValidationError
│   └── client.py              # GatewayClient — threaded WebSocket + v3 device auth
│
├── models/                    # Data models — no UI dependencies
│   ├── __init__.py           # Exports: AgentManager, AgentRoutingTable, Command, CommandResult, CommandRegistry,
│   │                          # StreamingBubble, FeedCardData, ActivityBubble, ToolStatus, Conversation, Message,
│   │                          # MessageRole, ToolCall, ToolCallStatus, ConversationSnapshot, SnapshotMessage,
│   │                          # ReviewState, TeamMember, ProjectTeam, Task, TaskStore, TASK_STATUS_LABELS,
│   │                          # PRIORITY_LABELS, WorkUnit, WorkUnitStore, WORK_STATUS_LABELS,
│   │                          # WORK_PRIORITY_LABELS, next_agent_color, reset_color_indices
│   │                          # Also creates: work_store = WorkUnitStore() singleton (primary);
│   │                          # task_store = TaskStore() singleton (kept for backward compat)
│   ├── agents.py              # AgentManager — session_key → name, colors, sessions
│   ├── activity.py            # ActivityBubble dataclass — activity bubble state for ActivityHandler
│   ├── colors.py              # Color palettes + round-robin assignment
│   ├── routing.py             # AgentRoutingTable — session_key → project_name routing
│   ├── command.py             # Command + CommandResult + CommandRegistry data models (Phase 7)
│   ├── conversation.py        # Conversation + Message + ToolCall + MessageRole dataclasses (Agent Runtime Phase 1.1)
│   ├── conversation_snapshot.py # ConversationSnapshot + SnapshotMessage — conversation snapshot data
│   ├── feed_card.py           # FeedCardData dataclass + css_class_for_type() (Phase 5)
│   ├── providers.py           # ProviderConfig dataclass + caller_default_max_tokens() (per-provider context window resolution)
│   ├── review_state.py        # ReviewState dataclass — per-project review session data (Phase 7)
│   ├── streaming.py           # StreamingBubble dataclass — streaming bubble state (Phase 5)
│   ├── task.py                # Task + TaskStore + status/priority labels (Phase 3) — DEPRECATED, superseded by work_unit.py
│   ├── work_unit.py           # WorkUnit + WorkUnitStore + status/priority labels (replaces task.py as primary)
│   └── team.py                # TeamMember + ProjectTeam — project team membership data
│
├── agent/                     # Local agent runtime — no UI dependencies
│   ├── __init__.py           # Exports: AgentRuntime, LLMProviderConfig, EnforcementConfig, AgentConfig,
│   │                          # load_agent_config, get_api_key, SpecialAgentDef, SPECIAL_AGENTS,
│   │                          # get_special_agents, reload_registry, ToolDefinition, ToolResult,
│   │                          # build_system_prompt, build_file_context, check
│   ├── kb_lookup.py          # KB lookup — cosine-sim retrieval over indexed KB chunks (Auxilium Tier 1)
│   ├── kb_server.py          # KB HTTP server — wraps kb_lookup in OpenAI-compatible API on localhost:18790 (KB Provider Phase 1)
│   ├── runtime.py           # AgentRuntime — tool loop, streaming, cost tracking (2344 lines; cost/LLM/SSE/extractors extracted to llm/)
│   ├── tools.py              # Tool definitions + execution (read_file, write_file, edit_file, exec_command, etc.)
│   ├── config.py             # LLM provider config + EnforcementConfig dataclass
│   ├── context.py            # System prompt builder (via prompts/system/ templates) + file context builder + .gitignore parsing
│   ├── context_strategy.py   # Pluggable context compaction strategy (Phase 4–9; ContextStrategy protocol + DefaultContextStrategy)
│   ├── tool_middleware.py    # Tool middleware chain: EnforcementMiddleware + StuckDetectionMiddleware (Phase A1 extraction)
│   ├── special_agents.py     # Coder + Debugger + Crabcakes agent definitions (auto_open, api_key_built_in, auto_add_to_projects fields)
│   ├── enforcement.py        # Post-write verification: syntax guard, test runner, lint check (Phase 3)
│   └── llm/                  # LLM provider abstraction package (Phase B1–B6 extraction)
│       ├── __init__.py       # Public API: get_provider, list_providers, LLMProvider, LLMResponse
│       ├── protocol.py       # LLMProvider Protocol (call + stream) + LLMResponse dataclass
│       ├── registry.py       # Provider registry: get_provider(id) -> LLMProvider
│       ├── openai_provider.py # OpenAIProvider — handles openai, openrouter, zai (call + stream)
│       ├── minimax_provider.py # MiniMaxProvider — ChatCompletion v2 (call + stream)
│       ├── anthropic_provider.py # AnthropicProvider — Messages API (call + stream)
│       ├── streaming.py      # SSE helpers (sse_lines, parse_sse_line, parse_sse_delta) + SSL retry infra
│       ├── extractors.py     # Response extractors: extract_tool_calls, extract_text_content, extract_usage
│       ├── convert.py        # Anthropic message/tool format converters
│       └── cost.py           # Cost tables + model_id + cost_for_model
│
├── ui/                        # All UI components
│   ├── __init__.py
│   ├── constants.py           # Cross-cutting UI constants (STREAMING_ENABLED toggle)
│   ├── toolbar.py             # Toolbar widget — connect button + status label
│   ├── styles.py              # All CSS — single source of truth (APP_CSS + apply_styles)
│   ├── window.py              # MainWindow — assembles all components, wires callbacks
│   ├── wiring.py              # Stateless wiring helpers (SettingsHandler callbacks, LOW-7 project path)
│   ├── handlers/              # Handler modules (extracted from window.py)
│   │   ├── __init__.py
│   │   ├── prompts_handler.py  # PromptsHandler — favorites, search, last-used
│   │   ├── agent_list_handler.py  # AgentListHandler — avatar cards data
│   │   ├── chat_handler.py    # ChatHandler — send, fan-out, routing
│   │   ├── chat_render_handler.py  # ChatRenderHandler — escape + markdown + bubble pipeline
│   │   ├── gateway_handler.py # GatewayHandler — connect, agents, lifecycle
│   │   ├── media_handler.py   # MediaHandler — STT + improve
│   │   ├── project_handler.py  # ProjectHandler — active project + agent-to-project routing
│   │   ├── activity_handler.py  # ActivityHandler — 6-state activity machine rendering to the status_target duck-type (Phase 6; repointed to the activity pill in SPEC-07 R4)
│   │   ├── activity_wiring_handler.py  # ActivityDrawer event wiring — gateway + local, online + offline (SPEC-activity-drawer)
│   │   ├── command_handler.py   # CommandHandler — slash-prefix command parser (Phase 7)
│   │   ├── review_handler.py    # ReviewHandler — review session lifecycle (Phase 7)
│   │   ├── task_handler.py      # TaskHandler — task commands (Phase 7) — DEPRECATED, superseded by work_handler.py
│   │   ├── work_handler.py      # WorkHandler — /work commands + Supervisor handoff (replaces task_handler.py)
│   │   ├── collab_handler.py   # CollabHandler — collaboration commands: ask/delegate/stop/tell (Phase 7)
│   │   ├── agent_runtime_handler.py  # AgentRuntimeHandler — local agent UI bridge (Phase 1.4)
│   │   ├── project_list_handler.py  # ProjectListHandler — project card data + color round-robin
│   │   ├── crabwatch_handler.py  # ~364 lines — CrabWatchHandler — Gio.FileMonitor filesystem watcher (Phase 5)
│   │   ├── input_toolbar_handler.py # ~395 lines — InputToolbarHandler — find/replace, spell check, word count logic
│   │   ├── agent_builder_handler.py # ~199 lines — AgentBuilderHandler — agent create/edit form + delete_agent_with_confirmation() (Phase 5)
│   │   ├── agent_command_handler.py # AgentCommandHandler — agent response slash-command parser (Phase 6.2)
│   │   ├── feed_handler.py        # ~2276 lines — FeedHandler — feed card lifecycle, background persist writer, compaction + prune surfacing (SPEC-UI-RESPONSIVENESS-2 P1/P3)
│   │   ├── session_handler.py     # ~164 lines — SessionHandler — session switching commands (Phase 7)
│   │   ├── connection_sync_handler.py  # post-connect wiring (Phase 3a extraction)
│   │   ├── forward_handler.py     # 17 tests (Phase 3b extraction)
│   │   ├── settings_handler.py    # Settings dialog logic — provider CRUD, test connection, status
│   │   ├── auxilium_wizard_handler.py # Auxilium first-run wizard handler (Tier 1, D7)
│   │   └── file_tree_handler.py   # Sort prefs + git status cache (no GTK) — FileTree enhancements Phase 4
│   └── views/                 # View widgets
│       ├── __init__.py
│       ├── activity_drawer.py  # NEW (SPEC-activity-drawer) — collapsible activity event panel
│       ├── chat_bubble.py      # build_role_bubble() — chat bubble widget factories (Phase 1)
│       ├── chat_surface.py     # ChatSurface/TextViewFallback — WebKit chat surface + activity pill (SPEC-06/07; FeedBar deleted in R4)
│       ├── feed_card.py        # ~581 lines — feed_card widget factory (Phase 5)
│       ├── diff_card.py         # Diff card widget factories — build_file_diff_card, build_diff_summary_card (Phase 7)
│       ├── review_bar.py        # ReviewBar widget — review mode dropdown + action buttons (Phase 7)
│       ├── file_tree.py        # ~2272 lines — FileTree (ColumnView directory browser, 22-property model, sort/filter/search)
│       ├── left_panel.py         # ~1004 lines — LeftPanel (Prompts/Agents/Projects notebook + right-click copy menu)
│       ├── main_content.py     # MainContent — chat notebook + input + button bar
│       ├── session_menu.py     # Right-click session switcher popover
│       ├── chat_input_toolbar.py # ~480 lines — ChatInputToolbar — find/replace bar + spell check toggle (view only)
│       ├── feed_tab.py          # ~167 lines — FeedTab — project feed card container (view only)
│       ├── settings_dialog.py   # GTK4 dialog for managing LLM provider settings
│       ├── agent_builder.py     # AgentBuilderDialog — modal dialog for creating/editing agents
│       └── auxilium_wizard.py  # Auxilium first-run wizard view (Tier 1, D7)
│
├── knowledge/                # User-facing documentation files (read by Crabcakes agent via web_fetch)
│   ├── setup.md              # Installation and first-run guide
│   ├── configuration.md      # Configuration options and agent.json
│   ├── agents.md             # How agents work (Coder, Debugger, Crabcakes, custom)
│   ├── features.md           # Feature overview and how-tos
│   ├── commands.md           # Slash command reference
│   ├── gateway.md            # OpenClaw gateway connection
│   └── troubleshooting.md    # Common problems and solutions
│
├── scripts/                  # Standalone utility scripts
│   └── rebuild_kb_index.py    # offline indexer — builds knowledge/.index/ from knowledge/*.md
│
├── prompts/                  # System prompt templates and default agent definitions
│   ├── system/                # Agent system prompt templates (loaded by utils/prompt_loader.py)
│   │                          #   APP-LEVEL: not seeded per-project; defines agent personality
│   └── default_agents/        # Default agent YAML definitions
│
# NOTE: Per-project prompt library lives at <project>/.crabcakes/prompts/
# (see §X below). The app-level prompts/ directory above contains system
# templates only; the user-facing library is per-project.
│
├── tests/                    # Test suite (100 files; see §13 for partial listing)
│
├── docs/                     # Specs, post-mortems, audits, research
│
└── utils/                     # Pure Python utilities — no GTK (with documented exceptions), no network
    ├── __init__.py
    ├── agent_defs.py            # Agent definition I/O — load, validate, save, list user-defined agents from agents/*.yaml
    ├── audit_parser.py          # extract_audit_reports() — parse ## Audit Report sections into AuditReport dataclasses
    ├── block_parser.py          # extract_blocks() — split message text into typed segments (code, quote, terminal, heading, task)
    ├── config.py                # config path helpers — get_config_dir(), get_projects_dir(), get_gateway_url(), COMMAND_PREFIX
    ├── conversation_store.py    # Snapshot creation — build ConversationSnapshot from message lists and git diffs
    ├── crabcard_parser.py       # extract_crabcards() — parse ```crabcard blocks from agent chat into FeedCardData (Phase 5)
    ├── diff_parser.py           # parse_diff() — unified diff → FileDiff/ParsedDiff data (Phase 7)
    ├── env_security.py          # get_scrubbed_env() — allowlisted env dict for subprocesses (MED-2 / CRIT-2)
    ├── escaping.py              # escape_for_pango(), xml_escape_text() — Pango-aware XML escape
    ├── favorites.py             # favorites persistence (favorites.json)
    ├── feed_store.py            # Feed JSON persistence — load/save/append/update to .crabcakes/feed.json (Phase 5)
    ├── feedback_processor.py    # Audit report file I/O — write structured audit reports to agent bug journals
    ├── file_security.py         # assert_secure_file() — config file permission validation (MED-6)
    ├── git_ops.py               # GitPython wrapper — git add/commit/diff/checkout/log/status/push via GitResult (Phase 7)
    ├── gtk_containers.py        # is_in_container() — GTK4 sibling-walk containment checks (GTK carve-out; see §2 table)
    ├── gtk_safe_link.py         # HIGH-6: activate-link guard for non-allowlisted URL schemes (GTK carve-out; see §2 table)
    ├── icons.py                 # Gdk.Texture SVG rendering (agent avatars + folder icons) (GTK carve-out; see §2 table)
    ├── image_utils.py           # convert_logo_to_icons() — JPG to multi-size PNG conversion for app icons
    ├── improve.py               # improve_prompt() — MiniMax API for prompt improvement (template mode with {{USER_INPUT}} marker)
    ├── markdown.py              # format_markdown() — inline markdown → Pango Markup
    ├── mcp_client.py            # MCP client library — asyncio-to-threading bridge, stdio transport, connection pooling, tool discovery/call
    ├── mcp_config.py            # MCP server configuration loader — YAML/JSON deserialization with schema validation
    ├── project_awareness.py     # Project awareness system — manages .crabcakes/ directory per project (team, workflow, context; seed_project_prompts)
    ├── project_trust.py         # HIGH-5: per-project trust gate for .crabcakes/ rule/bug ingestion
    ├── prompt_loader.py         # System prompt template loader — loads/fills/composes prompts/system/*.md
    ├── prompt_paths.py          # Per-project prompts dir resolvers (get_project_prompts_dir read-side, ensure_project_prompts_dir write-side)
    ├── prompts.py               # load_prompts() — reads .md from prompts/
    ├── projects.py              # load_projects(), scan_directory() — load project directories; load_members()/save_members() deprecated
    ├── provider_url.py          # validate_provider_url() — https-only for non-loopback hosts (MED-5)
    ├── providers_store.py       # Provider YAML persistence — load/save/add/remove/update providers.yaml + legacy migration
    ├── quoting.py               # _parse_quoted_payload() — quoted-payload parsing with escape handling (A2A_QUOTED_PAYLOAD_SPEC)
    ├── review_log.py            # Review log persistence — append/retrieve review entries per project
    ├── spellcheck.py            # Spell check engine — Enchant-based misspelling detection and suggestion
    ├── stt.py                   # STTEngine — faster-whisper push-to-talk; respects STT_MODEL_SIZE env var (default tiny.en)
    ├── syntax_highlight.py      # highlight() — Pygments → Pango markup (Tokyo Night color scheme)
    ├── work_persistence.py      # .crabcakes/work.json source of truth + generated tasks.md + legacy migration
    └── workflow_state.py        # Workflow state tracker — manages .crabcakes/workflow.md per project
```

**Top-level packages and their rules:**

| Package | Responsibility | Dependencies |
|---------|---------------|--------------|
| `gateway/` | Network I/O, auth, event dispatch | `cryptography`, `websockets`, `gi.repository.GLib` |
| `models/` | Data structures, state management | None (pure Python) |
| `ui/` | GTK widgets, layout, user interaction | GTK4 only |
| `utils/` | File I/O for prompts, projects, membership | None (with documented GTK carve-outs; see below) |

**Critical rule:** `gateway/` and `models/` must NEVER import from `ui/`. They are the foundation that the UI depends on — not the other way around.

**`utils/` GTK carve-out (documented exceptions):** Four files in `utils/`
import `gi.repository` despite the "no GTK" rule above. These are
narrowly-scoped exceptions that have been reviewed and accepted:

| File | Import | Why it's in `utils/` | Justification |
|------|--------|---------------------|---------------|
| `utils/gtk_containers.py` | `gi.repository.Gtk` (module-level) | Pure widget-container queries — no widget construction, no signals, no windows | GTK4 removed `Gtk.Container`; this helper wraps sibling-walk containment checks (`is_in_container`) that only make sense against `Gtk.Widget`. No ui/agent/gateway/models imports |
| `utils/icons.py` | `gi.repository.Gdk` (module-level) | Pure texture rendering — no widget hierarchy, no layout, no signals | `Gdk.Texture` is the only way to render SVGs to pixel buffers; callers pass textures as plain objects |
| `utils/gtk_safe_link.py` | `gi.repository.Gtk, Pango` (lazy, inside functions) | Link-safety guard; callers are GTK-bound labels | Function-level import minimizes the GTK surface; the logic is scheme-string validation that happens to need Pango markup parsing |
| `utils/stt.py` | `gi.repository.GLib` (lazy, inside thread callback) | `GLib.idle_add` marshals STT results from the whisper thread to the main loop | Thread-dispatch mechanism only — no widgets, no windows, no signals. Has `except ImportError` fallback |

**Rule:** Do not add new GTK imports to `utils/` without documenting them
here and justifying why the file cannot live in `ui/`.

---

## 3. Module Responsibilities

### 3.1 `main.py` — Application Entry Point

**Responsibility:** Bootstrap the GTK application.

```python
class CrabcakesApp(Gtk.Application):
    def on_activate(self, app):
        win = MainWindow(application=app)
        win.present()
```

**Rules:**
- Must be thin. Only creates the application and the main window.
- All business logic lives in other modules.
- Never contains widget definitions.

### 3.2 `gateway/` — Network Layer

**Responsibility:** Handle all WebSocket communication with the OpenClaw gateway.

The `gateway/` package is the **only** place that knows about:
- WebSocket URLs (`ws://localhost:18789`)
- Device identity files (`~/.openclaw/identity/`)
- v3 device-auth handshake

**Key classes:**

| Class | File | Responsibility |
|-------|------|---------------|
| `GatewayClient` | `client.py` | Threaded WebSocket with reconnect, auth, message sending |

**Public API:**
```python
from gateway import GatewayClient

client = GatewayClient(
    url="ws://localhost:18789",
    on_connect=callback_fn,        # called when connected
    on_error=error_fn,            # called on connection error
    on_event=event_fn,            # called on gateway event (event_name, payload)
)
client.start()              # begins connecting in background thread
client.stop()               # disconnects
client.is_connected()       # True if connected
client.get_snapshot()       # returns hello-ok snapshot dict
client.send_message(session_key, text, on_sent=cb)
```



### 3.2a `transport/` + Telegram bridge (develcakes v2, SPEC-05/SPEC-15)

**Status:** the `gateway/` layer above is superseded by the v2 `transport/` package
(SPEC-05 R1 strip). `transport/base.py` is the `Transport` ABC; `transport/openclaw.py`
is the cleaned WebSocket core; **`transport/telegram.py`** is the ABC's first real
second implementation — a long-poll HTTP client (`connect`/`disconnect`/`send_message`/
`answer_callback_query`/`edit_message_text`), token in a 0600 store, token redacted
from every log line. The Connect button is now REAL (it toggles the Telegram bridge),
not a stub.

**`ui/handlers/telegram_bridge_handler.py`** — `TelegramBridgeHandler`, the remote-in
handler node. The phone is a **thin client for the Supervisor only**:

- Inbound: paired-chat text → `ARH.send_to_special_agent("special:supervisor", …)`.
- Allowlist: `/status` (compact project summary), `/stop` (typed two-step confirmation
  → the SAME stop-all path the toolbar uses), `/help`; any other `/command` gets one
  refusal listing the allowlist. Plain text still forwards.
- Exec approvals: pending-approval cards mirror to the phone with inline ✅/❌ buttons;
  a tap → `ARH.approve_exec` → the card is re-read and the message edited to the actual
  state ("Already resolved in the app." for a desk-first race).
- Outbound mirror (SP3b): Supervisor turn replies forward to the phone (4096-char
  paragraph split); a whole-message ```html card is flattened to its visible TEXT
  (Telegram is a text surface).
- Trust boundary: one paired chat_id; NO shell, NO file access. Foreign chat ⇒ one
  polite refusal, never processed.

**Handler isolation:** the bridge never imports window/ARH; the composition root
(`ui/window.py`) injects `set_agent_runtime_handler`, `set_feed_handler`,
`set_stop_all_handler`, and `set_status_provider`. The app→phone reply mirror is wired
by COMPOSING the ARH's single `set_on_agent_response` slot (window `_on_agent_response`
calls both the agent-command handler and `bridge.on_supervisor_reply`).



### 3.3 `models/` — Data Layer

**Responsibility:** Hold all application state and data structures. No UI code.

**Key classes:**

| Class | File | Responsibility |
|-------|------|---------------|
| `AgentManager` | `agents.py` | Tracks session_key → name, colors, sessions |
| `AgentRoutingTable` | `routing.py` | Maps session_key → project_name; shared between ProjectHandler and ChatHandler |
| `Command`, `CommandResult` | `command.py` | Parsed command input + result of command processing |
| `CommandRegistry` | `command.py` | Maps command names to handlers; extensible |
| `StreamingBubble` | `streaming.py` | Dataclass for streaming bubble state (Phase 5) |
| `FeedCardData` | `feed_card.py` | Dataclass for Project Feed cards (Phase 5) |
| `ActivityBubble` | `activity.py` | Dataclass for activity event state; `to_drawer_row()` builds the dict the ActivityDrawer consumes (SPEC-activity-drawer) |
| `Task`, `TaskStore` | `task.py` | Task data model + in-memory store (Phase 3) — **DEPRECATED**, superseded by `WorkUnit`, `WorkUnitStore`; kept for import compatibility |
| `WorkUnit`, `WorkUnitStore` | `work_unit.py` | Work Unit data model + in-memory store (primary); `spec_path`, supervisor/builder/auditor assignments, dependency graph, lifecycle statuses |
| `ReviewState` | `review_state.py` | Per-project review session data (Phase 7) |

### 3.3a `models/routing.py` — Agent Routing Table

**Responsibility:** Maps agent session keys to their active project name. Shared between
ProjectHandler (writes) and ChatHandler (reads). Replaces a raw shared dict with explicit
methods and a clear contract.

**Public API:**
```python
class AgentRoutingTable:
    def add(session_key, project_name) -> None
    def remove(session_key) -> None
    def remove_project(project_name) -> None
    def get_project(session_key) -> str | None
    def is_routed(session_key) -> bool
    def clear() -> None
```

**Rules:** Pure data container. No GTK, no network, no callbacks.

**Color system (`colors.py`):**
- `AGENT_COLORS` — round-robin palette for agents
- `next_agent_color()` — returns next color, advances counter
- `reset_color_indices()` — resets counters on reconnect

**Rules:**
- Models know nothing about GTK widgets.
- Models do not emit signals or have callbacks — they're plain data containers.
- UI code reads from models and responds to changes via callbacks.

### 3.3b `models/agents.py` — Agent Manager

**Public API:**
```python
class AgentManager:
    def register(session_key, agent_name) -> None    # register new agent session
    def get_name(session_key) -> str                  # get display name for session key
    def get_names_ref() -> dict[str, str]             # {session_key → name} for UI panels
    def get_sessions(agent_name) -> list[str]         # all session keys for an agent name
    def get_color(agent_name) -> str | None           # hex color for agent name
    def clear() -> None                              # clear all sessions (preserves colors)
```

### 3.3c `models/command.py` — Command Data Models

**Public API:**
```python
@dataclass Command:
    name, args, flags, raw_text, body, source_session_key, target_session_key
    is_broadcast, broadcast_targets

@dataclass CommandResult:
    handled, response_text, response_card, forward_to, forward_text, broadcast_targets

class CommandRegistry:
    def register(name, handler, aliases=None, help_text="") -> None
    def get(name) -> Callable | None
    def list_commands() -> list[str]
    def list_aliases() -> dict[str, str]
    def get_help(name) -> str | None
```

### 3.3d `models/task.py` — Task Data Model

> **DEPRECATED (SPEC-TASK-SYSTEM-FULL-REDESIGN §14):** `Task`/`TaskStore` are superseded by
> `WorkUnit`/`WorkUnitStore` (`models/work_unit.py`) and `models/task.py` is kept solely for import
> compatibility. New code must use Work Units.

**Public API:**
```python
@dataclass Task: id, title, description, assigned_to, created_by, status, priority,
                created_at, updated_at, blocked_reason

class TaskStore:
    def generate_id() -> str          # sequential 8-char zero-padded ID
    def create(task) -> Task
    def get(task_id) -> Task | None
    def update(task) -> Task
    def list_all() -> list[Task]
    def list_by_agent(session_key) -> list[Task]
    def delete(task_id) -> bool
```

### 3.3d1 `models/work_unit.py` — Work Unit Data Model

**Responsibility:** The primary task model. A Work Unit bundles a title, a required `spec_path`
(the atomic implementation contract), a status, a priority, dependency IDs, and
supervisor/builder/auditor assignments. Supersedes `models/task.py` (SPEC-TASK-SYSTEM-FULL-REDESIGN §3, §14).

**Statuses (`WORK_STATUSES`):** `draft`, `spec-pending`, `spec-ready`, `in-progress`, `auditing`,
`done`, `cancelled`.

**Priorities (`WORK_PRIORITIES`):** `low`, `medium`, `high`, `critical`.

**Public API:**
```python
@dataclass WorkUnit:
    id, title, spec_path, status, assigned_supervisor, assigned_builder, assigned_auditor,
    priority, depends_on: list[str], created_at, updated_at, completed_at,
    post_mortem_path, blocked_reason

    def to_dict() -> dict
    @classmethod from_dict(cls, data: dict) -> "WorkUnit"

class WorkUnitStore:
    def create(work) -> WorkUnit                 # assigns id/dates, validates dependency graph
    def get(work_id) -> WorkUnit | None
    def update(work) -> WorkUnit                 # re-validates dependencies
    def list_all() -> list[WorkUnit]             # sorted by (created_at, id)
    def list_by_status(status) -> list[WorkUnit]
    def delete(work_id) -> bool
    def replace_all(work_units) -> None          # load path (does NOT reset id counter)
```

**Helpers:**
- `_work_next_id()` — sequential zero-padded id generator.
- `WORK_STATUS_LABELS` / `WORK_PRIORITY_LABELS` — emoji display labels.
- `_work_init_counter(work_units)` — advance the module id counter past loaded ids (restart collision safety).
- `_validate_dependencies(work, existing_ids)` — rejects a Work Unit whose `depends_on` references an unknown id.

**Serialization:** `to_dict()` / `from_dict()` with type validation — `depends_on` is stored as a list
and `from_dict` coerces/validates field types. `WorkUnitStore` is a pure in-memory store; persistence is
owned by `utils/work_persistence.py`.

### 3.3e `models/review_state.py` — Review State

```python
@dataclass ReviewState:
    project_path: str
    review_mode: str        # "off" | "review"
    checkpoint_sha: str | None
    is_dirty: bool
    last_check_files: list[str]

    def is_active() -> bool      # checkpoint exists and not resolved
    def can_checkpoint() -> bool   # review mode on, no active session
```

### 3.4 `ui/toolbar.py` — Top Bar

**Responsibility:** App-level actions bar (Stream toggle, Settings button, status label, Connect button).

**Layout:** `[Stream | ⚙ Settings]  ←—expanding spacer—→  [status label | Connect]`

**Public API:**
```python
toolbar = Toolbar(on_connect_clicked=callback_fn, on_settings_clicked=callback_fn)
toolbar.update_connection_state("disconnected" | "connecting" | "connected" | "offline")
toolbar.set_settings_status(has_verified_provider: bool)  # shows/hides the red dot on ⚙ Settings
```

**Internal state:** Owns the Stream toggle, Settings button (wrapped in a `Gtk.Overlay` with a status dot), status label, and Connect button. Updates them based on calls to `update_connection_state()` and `set_settings_status()`.

### 3.5 `ui/styles.py` — Global CSS (~1045 lines)

**Responsibility:** Single source of truth for all application CSS.

**Owns:** `APP_CSS` constant (all CSS rules) and `apply_styles()` function (registers CSS provider globally).

**Public API:**
```python
from ui.styles import APP_CSS, apply_styles

apply_styles()  # Call once at startup, before any windows are created
```

**Rules:**
- No other file may call `Gtk.CssProvider().load_from_data()` or define CSS inline.
- Views use `widget.add_css_class("name")` only — they never define what classes look like.
- See Section 9 for full CSS conventions.

### 3.5a `ui/constants.py` — Cross-Cutting UI Constants

**Responsibility:** Neutral module for UI state shared between views and
handlers without violating §8.6 R7 (views must not import from handlers).

**Public API:**
```python
STREAMING_ENABLED: bool = False   # toggled by toolbar button; read by ChatHandler
```

**Architecture rule (§8.6 R7):** When both a view and a handler need to
read AND write the same mutable state, put it here. For one-way state,
pass via constructor or setter from `ui/window.py` instead.

### 3.6 `ui/window.py` — Main Window

**Responsibility:** Assemble all UI components and wire all callbacks. The **single place** where all modules are connected.

**Project group chat state:**
```python
self._active_project_name = None   # set when a project tab is opened
self._agent_to_project = AgentRoutingTable()  # shared with ProjectHandler (writes), ChatHandler (reads), and ActivityHandler (via set_agent_routing())
```

**Phase 4 (MediaHandler) wiring:** `_media_handler` created and wired in `_build()`:
- `on_stt_click` → `_media_handler.on_stt_click`
- `on_improve_click` → `_media_handler.on_improve_click`
- STT transcript append → `_chat_handler.on_send()` via sync callback

**Rules:**
- Window creates all sub-components and passes callbacks to each.
- Window holds references to gateway client and agent manager.
- Window creates and wires handler instances (ChatHandler, etc.) — see `ui/handlers/`.
- Window defines callback handlers not yet extracted (major business logic extracted to handlers per SPEC-window-business-logic-extraction.md).
- Window does NOT define GTK widgets directly — it composes sub-views.

**Phase 1 (ChatHandler) extracted:** `_on_send`, `_on_send_clicked`, `_switch_to_session_tab`, and chat.final routing are now in `ui/handlers/chat_handler.py`.
**Phase 5 extracted:** `_on_audit_report_card` (FeedHandler.add_audit_report_card), `_on_agent_saved`/`_on_agent_deleted` (AgentRuntimeHandler.reload_agents_and_mcp), `_confirm_delete_agent` (AgentBuilderHandler.delete_agent_with_confirmation), `_register_stub_commands` (CommandHandler auto-registration).

**SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS §2.7 — Project-created System bubble:** `window.py` is the composition root for project-created UI side effects. It wires `ProjectHandler.set_on_project_created(self._on_project_created_system_bubble)` at composition time. The callback defers all widget work through `GLib.idle_add` for main-thread safety. Inside the deferred body it resolves the `project:<name>` chat box, creating the tab first if needed via `MainContent.create_chat_tab()`, then renders a System bubble via `ChatRenderHandler.render_sync("System", text, session_key)` and scrolls. No-ops safely if the chat box is unavailable or the renderer returns `None`. See §4.3a for the full data flow.

**Right-click spell suggestions:** The `_on_input_right_click` closure (defined in `MainWindow._build()`) consumes `InputToolbarHandler.is_spell_enabled()` (FRAGILE-1) and `get_word_at_iter()` (STALE-1) from `InputToolbarHandler`. The closure captures the clicked word's text at right-click time and verifies it at suggestion-click time to avoid replacing a different word if the buffer changed in between. See `SPEC-SPELL-POPOVER-FOLLOWUP.md` for details.

**Project settings bar wiring (Spec PROJECT-SETTINGS-BAR-ENHANCED):** `window.py` wires the settings-bar callbacks in `_build()` (named lifecycle methods registered as ADDITIONAL `set_on_project_opened`/`set_on_project_closed` callbacks — the handler is append-based, so they coexist with the existing feed/crabwatch/review lambdas):

| Setter | Window impl | Action |
|--------|-------------|--------|
| `main_content.set_on_settings_clicked` | `_on_settings_btn_clicked` | ⚙ -> `_open_settings()` (fresh dialog each click) |
| `main_content.set_on_agent_cycle` | `_on_agent_cycle_clicked` | cycle solo member -> `set_solo_target` (bar refreshes via solo-changed callback) |
| `main_content.set_on_autoaccept_cycle` | `_on_autoaccept_cycle_clicked` | `FeedHandler.set_auto_accept_level(next)` — NO optimistic bar rebuild (BUG #4; bar updates post-confirmation) |
| `project_handler.set_on_solo_target_changed` | `_on_solo_target_changed` | bar refresh, active-project guarded (BUG #3) |
| `project_handler.set_on_project_opened` | `_on_project_opened` | invalidate in-flight branch worker + re-evaluate bar (Round 4 fix) |
| `project_handler.set_on_project_created` | `_on_project_created_system_bubble` | SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS: deferred System bubble in `project:<name>` tab instructing user to add Supervisor (§2.7) |
| `project_handler.set_on_project_closed` | `_on_project_closed` | invalidate in-flight branch worker (Round 3 BUG #1) |
| `feed_handler.set_on_auto_accept_level_changed` | `_on_auto_accept_level_changed` -> `_refresh_settings_bar_for_active` | bar refresh after async confirmation (BUG #4) |

**Token-guarded, path-keyed branch worker (Round 2 BUG #1/#2/#7 + Round 3 BUG #2/#5/#6):** `_on_feed_bar_update` schedules `_schedule_branch_refresh` when `needs_resolution` (active path not cached) and `not already_running` (two independent conditions, BUG #6). The worker `_resolve_branch_worker` reads only the CAPTURED path and dispatches `_on_branch_result` via `GLib.idle_add`. `_on_branch_result` runs ALL staleness + active-identity checks (token, path, active-name, active-path) BEFORE writing the cache (BUG #2), then re-runs `_on_feed_bar_update` with `branch_name=branch`.

**State fields (window `__init__`):**
```python
self._cached_branch_by_path: dict[str, str] = {}   # {project_path: branch_name} (BUG #5)
self._branch_request_token: int = 0                # monotonic request id (BUG #7)
self._branch_active_token: int | None = None       # token of in-flight worker (BUG #2)
self._branch_request_path: str | None = None       # project path captured at schedule time (BUG #2)
```

### 3.6a `ui/handlers/input_toolbar_handler.py` — Input Toolbar Handler

**Responsibility:** All input toolbar logic — find/replace, spell check, file I/O, word count. Pure data layer; imports no `Gtk.*` widget types in module scope. GTK types are accessed only via `self._mc.user_input.get_buffer()` which returns `Gtk.TextBuffer` (the established pattern).

**Owns:** Spell check state (`_spell_enabled`), find/replace state, debounced spell-check timer, spell-error TextTag.

**Public API:**
```python
class InputToolbarHandler:
    # File I/O
    def load_file(self, file_path: str) -> bool
    def save_to_file(self, file_path: str) -> bool

    # Spell check
    def toggle_spell_check(self) -> bool     # returns new state
    def is_spell_enabled(self) -> bool       # public read-only accessor
    def on_buffer_changed(self) -> None      # called by view on buffer change
    def get_suggestions_at_iter(text_iter) -> list[str]
    def get_word_at_iter(text_iter) -> str    # returns "" if not in a word; preserves case
    def replace_word_at_iter(text_iter, replacement: str) -> None

    # Find / replace
    def find(self, search_text: str) -> tuple[int, int]    # (current_idx, total)
    def find_next(self) -> tuple[int, int]
    def find_prev(self) -> tuple[int, int]
    def replace_current(self, replacement: str) -> tuple[int, int]   # (current_idx, total)
    def replace_all(self, replacement: str) -> int          # returns count

    # Word count
    def get_word_count(self) -> tuple[int, int, int]  # (words, chars, lines)
    def compute_count(self) -> tuple[int, int, int]   # public alias for get_word_count
```

**Rules:**
- Imports NO `Gtk.*` widget types at module scope. The single lazy import of `Pango`/`Gdk` inside `_apply_spell_tags` is for RGBA/tag property access, not widget construction.
- All GTK dispatch via `GLib.idle_add()` (uses `self._GLib` injected at construction).
- Public read-only accessors (`is_spell_enabled`, `get_word_count`, etc.) for view-layer consumption.
- Private fields prefixed with `_` and never accessed from outside the handler.
- `get_word_at_iter` includes a fix for the GTK `backward_word_start()` position-dependent bug (when iter is on the first char of a word, `backward_word_start()` can jump to the previous word; the method scans for intervening whitespace and corrects).

### 3.7 `ui/views/left_panel.py` — Left Sidebar

**Responsibility:** Three-tab notebook: Prompts, Agents, Projects.

**Prompts tab:** PromptsHandler-backed list with search, favorites, and rich metadata rows. Star/favorite persisted to `~/.config/crabcakes/favorites.json`. Double-click or `+` button calls `on_prompt_loaded(filepath, name, content)`, which loads content into chat input. Search filters by name (case-insensitive). Favorites sort to top. Right-click on a prompt row opens a 2-item popover menu ("Copy path" / "Copy prompt"); the selection is copied to the system clipboard via `Gdk.Display.get_clipboard()` and a transient "Copied path" / "Copied prompt" confirmation appears in the tab header for 2.5s (auto-cleared via `GLib.timeout_add`).

**Agents tab:** Initially empty placeholder. After `set_agents()` is called, builds avatar cards (colored circle + initials + name + +/− toggle button). Double-click calls `on_agent_selected(session_key, name)`. CSS for agent rows is scoped to `left_panel`. When a project is open, the toggle button is visible — `+` adds the agent to the project, `−` removes them. Toggle button uses `.agent-add-btn` (green) or `.agent-remove-btn` (red) CSS classes.

**Projects tab:** `FileTree` widget — `Gtk.TreeView` with `Gtk.TreeStore`, lazy-loading subdirectories, back button.

**Public API:**
```python
# window.py (composition root) builds the handler and injects it —
# ui/views must not import ui.handlers (arch guard).
panel = LeftPanel(on_prompt_selected=cb, on_project_selected=cb,
                  file_tree_handler=FileTreeHandler())
panel.set_agents(agent_names_dict, on_agent_selected_callback)
panel.set_agent_list_handler(handler)         # wires AgentListHandler for avatar cards
panel.set_prompts_handler(handler)             # wires PromptsHandler for prompt library
panel.refresh_prompts()                       # rebuilds the prompts list
panel.set_on_project_opened(cb)               # fires when project tab opens
panel.refresh_agents_with_project(name)      # rebuilds agents list with +/− buttons
panel.set_toggle_agent_callback(cb)            # wires +/− toggle to ProjectHandler.toggle_agent()
```

### 3.7a Prompts Tab Right-Click Copy Menu

**Responsibility:** View-layer (LeftPanel) feature. Right-click on a prompt row → 2-item popover (Copy path / Copy prompt) → clipboard write → transient status feedback.

**Architecture boundary (per §3.13):** All GTK/widget code lives in `left_panel.py` (view owner). `PromptsHandler` (data owner) is unchanged. No GTK imports in `prompts_handler.py`. The view consumes `prompt['filepath']` and `prompt['content']` from the handler's scan output and stashes them as row attributes (`_filepath`, `_prompt_content`) at build time.

**Wiring:**
- `LeftPanel._build_prompt_row()` attaches a `Gtk.GestureClick` controller with `button=Gdk.BUTTON_SECONDARY` to every prompt row. The `pressed` signal connects to `_on_prompt_row_right_click`.
- `_on_prompt_row_right_click(ctrl, n_press, x, y, row)` filters out multi-press (`n_press != 1`) and rows without `_filepath`, then constructs a `Gtk.Popover` with a 2-row `Gtk.ListBox`. Popover parent is the source row.
- `_on_prompt_menu_row_activated(_lb, menu_row, popover, source_row)` reads the child label text to dispatch "Copy path" or "Copy prompt", then `popdown()` + `unparent()`s the popover.
- `_on_copy_prompt_path(row)` and `_on_copy_prompt_content(row)` read `row._filepath` / `row._prompt_content` and call the local `_copy_text_to_clipboard()` helper.
- `_copy_text_to_clipboard(text)` uses `Gdk.Display.get_default().get_clipboard().set(text)` — no-op when display is unavailable (headless test env).
- `_show_prompt_copy_status(message)` writes the message into the status label appended to the Prompts tab header, then schedules a 2.5s `GLib.timeout_add` to clear it. Pending timeout is cancelled before a new one is scheduled.

**Status label location:** The transient status label (`_prompt_copy_status_label`) is appended to the Prompts tab header `[title, search, status_label]` and right-aligned via `set_xalign(1.0)`. Styled with `.dim-label` CSS class.

**Test coverage:** `tests/test_left_panel.py` — 8 tests in `TestPromptRowRightClick`:
- `test_prompt_row_has_filepath_and_content_attrs` — row attributes set from prompt dict
- `test_copy_path_calls_clipboard_with_filepath` — clipboard called with filepath
- `test_copy_prompt_calls_clipboard_with_content` — clipboard called with content
- `test_copy_path_skips_when_filepath_missing` — defensive skip
- `test_copy_prompt_skips_when_content_missing` — defensive skip
- `test_copy_status_label_shows_and_clears` — label set + closure clears it
- `test_right_click_handler_ignores_multipress` — n_press != 1 skipped
- `test_prompt_row_has_right_click_gesture_attached` — **regression-proof**: FAILS if `add_controller` is removed from `_build_prompt_row`

**Known follow-ups (not blocking):**
- Popover leak on ESC / click-outside dismissal (the `row-activated` path always `unparent()`s, but other dismiss paths don't). Fix: wire `popover.connect("closed", lambda *_: popover.unparent())`.
- Label-text dispatch ("Copy path" / "Copy prompt") would silently no-op on localized strings. Future: store an action key on each row instead of parsing label text.

### 3.8 `ui/views/file_tree.py` — FileTree Widget

**Responsibility:** Expandable directory browser with Gtk.ColumnView, lazy-loading, multi-column layout (Name, Status, Size, Modified). Used by the Projects tab.

**Architecture (FileTree Enhancements Phase 1–4):**

The FileTree underwent a phased enhancement transforming it from a simple Gtk.TreeView directory browser into a full-featured file explorer with sort, filter, search, git status badges, and per-project persistence:

**Phase 1–3 — View (widgets + data models):**
- `FileTreeRow`: GObject with 22 properties (12 original + 9 file metadata + parent_full_path)
- `ColumnView` + `SortListModel` + `FilterListModel` chain (in-place mutation pattern for sort/filter without rebuilding the store)
- 4-column layout: Name, Status (git badge), Size (human-readable), Modified (relative time)
- Sort: 6 modes (`name_asc`, `name_desc`, `modified_asc`, `modified_desc`, `size_asc`, `size_desc`), depth-aware comparator preserving tree hierarchy, drawer-adjacency invariant
- Search debounce (150ms `GLib.timeout_add`) and timeout lifecycle
- Background thread safety via generation counter + path capture
- Factory classes for row widgets, expand/collapse, diff drawer

**Phase 4 — Handler/view split:**

```
ui/handlers/file_tree_handler.py    (NO GTK)
├── sort preference persistence     (.crabcakes/file_tree_prefs.json, whitelist-validated)
└── git status cache                (status_porcelain, dirty flag, refresh/invalidate)

ui/views/file_tree.py               (GTK widgets)
├── ColumnView + SortListModel + FilterListModel chain
├── FileTreeRow widget (22 GObject properties)
├── depth-aware comparator (6 sort modes)
├── search debounce (150ms)
└── factory classes (drawer, row widget, expand/collapse)
```

The handler owns non-UI concerns (preferences I/O, git status caching). The view owns all GTK widgets and model chains. Communication is via callback setters on the view: `set_on_sort_changed`, `set_on_get_sort_mode`, `set_on_get_git_status`.

**Public API:**
```python
tree = FileTree(on_file_selected=cb)  # double-click file selection
tree.set_on_project_opened(cb)        # double-click on project row fires callback
tree.set_on_sort_changed(cb)          # sort mode changed: cb(mode_str)
tree.set_on_get_sort_mode(cb)         # fetch saved sort mode: cb() -> str
tree.set_on_get_git_status(cb)        # fetch git status: cb() -> dict[str, str]
```

### 3.9 `ui/views/main_content.py` — Main Content Area

**Responsibility:** Right panel — chat notebook + user input.

**Public API:**
```python
content = MainContent()
content.user_input              # property → Gtk.TextView
content.send_button             # property → Gtk.Button
content.notebook                # property → Gtk.Notebook (chat tabs)
content.create_chat_tab(session_key, agent_name)   # creates/returns to existing tab
content.get_chat_box(page_index=None)  # get the chat box for a tab (used by ChatHandler)
content.get_current_session_key()  # session_key of active tab, or None (used by ActivityHandler)
content.set_chat_render_handler(handler)  # inject ChatRenderHandler (called by window.py)
content.set_feed_bar_text(text)  # update the project feed bar
content.set_review_bar(bar)     # add/remove ReviewBar widget above chat (used by ReviewHandler)
content.get_review_bar()      # get current ReviewBar or None (Phase 7)
content.set_agent_manager(agent_mgr)  # set AgentManager for session switch lookup
content.close_tabs(page_indices)       # close multiple tabs, reindex once
content.set_on_stt_click(cb)     # STT button clicked
content.set_on_improve_click(cb) # Improve button clicked
content.replace_input_text(text) # replace input with improved text
content.append_stt_text(text)    # append STT partial transcript
content.update_stt_state(state) # "idle" | "recording" — button label/style

# Spec PROJECT-SETTINGS-BAR-ENHANCED: project settings bar (agent name,
# auto-accept level, git branch, ⚙ button) rebuilt from resolved primitives.
def update_project_settings(project_name, member_count, solo_target,
                            auto_accept_level, branch_name) -> None
    # Rebuild the settings bar with all 5 elements. Falsy project_name hides
    # the bar and returns. xml_escape_text()s the UNTRUSTED project_name and
    # branch (BUG #6). Re-appends the singleton gear button last.
def set_on_settings_clicked(cb) -> None   # ⚙ -> _on_settings_btn_clicked -> Settings dialog
def set_on_agent_cycle(cb) -> None        # agent label clicked -> cycle solo member
def set_on_autoaccept_cycle(cb) -> None   # auto label clicked -> cycle level (off->diffs->files->all->off)
# _clear_settings_bar() — sibling-walk child removal (get_first_child()/remove()
#   loop; NEVER `for child in list(box)` — gtk-container-membership bug class).
# Gear-preservation invariant (BUG #5): BOTH legacy methods
#   set_project_settings_text(text) and set_feed_bar_text(text) clear the bar,
#   append their label, then re-append _settings_btn so the gear is never lost
#   across rebuilds.
```

**Tab close:** Each tab has an × button (top-right of tab label) and responds to middle-click. Both call `_close_tab(page_idx)` which removes the page and re-indexes tracking dicts.

**Tab session switch:** Right-click → session switcher menu calls `_switch_tab_session(page_idx, new_session_key)`. This updates both `_tab_sessions[page_idx]` (the internal lookup dict) **and** `tab_label_box._session_key` (the GTK widget attribute). The widget attribute must stay in sync because `_find_page_by_session` reads it — a split-brain causes `_update_tab_dot` to miss the tab, breaking the unread dot indicator.

**Review bar integration (Phase 7):** ReviewHandler calls `set_review_bar(bar)` to insert a `ReviewBar` widget above the notebook, and `get_review_bar()` to retrieve the current bar for state updates without accessing MainContent internal state.

### 3.10 `utils/favorites.py` — Favorites Persistence

**Responsibility:** Read/write the favorites set for the prompt library.

**Public API:**
```python
load_favorites() -> set[str]        # may be empty set on error
save_favorites(set[str]) -> None
is_favorite(filepath) -> bool
toggle_favorite(filepath) -> bool   # True if now favorited
```

**Security:** No secrets. File I/O only — reads/writes favorites set to `~/.config/crabcakes/favorites.json`.

### 3.11 `utils/` — Utilities

**Responsibility:** File I/O helpers for prompts, projects, and project membership.

| Function | File | Responsibility |
|----------|------|---------------|
| `load_prompts()` | `prompts.py` | Returns `[(name, content), ...]` from `prompts/` |
| `load_projects()` | `projects.py` | Returns `[(name, full_path), ...]` from `CRABCAKES_PROJECTS_DIR` |
| `scan_directory(path)` | `projects.py` | Returns `[(name, full_path, is_dir), ...]` for one level, filtered (skips `__pycache__`, `.git`, etc.) |
| `load_members(project_name)` | `projects.py` | Returns `[{session_key}, ...]` from `~/.config/crabcakes/projects/<name>/members.json` |
| `save_members(project_name, members)` | `projects.py` | Writes members list to `members.json`, creates dir if needed |
| `improve_prompt(text, callback, GLib)` | `improve.py` | Loads template from `prompts/system/improve.md`, injects user text at `{{USER_INPUT}}` marker (or legacy split), sends to MiniMax API, calls `callback(improved, error)` with GLib dispatch |
| `load_prompt_template(name)` | `prompt_loader.py` | Load `prompts/system/<name>.md` template, return raw string or None |
| `fill_template(template, variables)` | `prompt_loader.py` | Replace `{{KEY}}` markers with values from dict |
| `compose_system_prompt(agent_name, ...)` | `prompt_loader.py` | Compose full system prompt from templates: (1) default.md, (1b) collab.md, (1c) crabcakes-context.md, (2) project-awareness.md, (3) crabcakes-commands.md, (4) project-onboarding.md, (5) code-review.md, (6) coder.md/debugger.md, (7) {role}-bugs.md/{role}-rules.md |
| `STTEngine` class | `stt.py` | Push-to-talk STT via faster-whisper — arecord → PCM buffer → faster-whisper (tiny.en model) → stop_async callback |
| `show_session_menu(parent, agent_name, sessions, on_select)` | `session_menu.py` | GTK popover menu listing sessions; clicking fires `on_select(session_key)` |

### 3.11a `utils/provider_test.py` — LLM Provider Connectivity Probe + Context Window Discovery

**Responsibility:** Test that an LLM provider is reachable and authenticated; optionally discover the model's context window. Used by Settings dialog "Test Connection" button and the auxilium wizard.

**Owns:** `_OPENAI_COMPATIBLE` set (`{"openai", "openrouter", "zai", "minimax"}`), `_NoAuthRedirectHandler` (strips `Authorization` header on cross-host redirect — prevents leaking keys to a different origin), `_CALLER_DEFAULT_MAX_TOKENS` static lookup table (per-caller fallback context window when probe fails), and the HTTP request lifecycle.

**Public API:**

```python
@dataclass
class TestResult:
    ok: bool
    latency_ms: int
    error: str | None
    model_used: str | None
    context_window: int | None  # discovered from /v1/models probe, or None

def test_connection(
    base_url: str,
    api_key: str,
    model: str,
    caller: str | None = None,  # optional, e.g. "minimax" for body-level error decoding
    timeout_seconds: int = 30,
) -> TestResult: ...
```

**Lifecycle of a Test Connection:**

1. **POST chat completion.** Builds an `urllib.request.Request` with `Authorization: Bearer <api_key>`. Uses a *local* `OpenerDirector` (built via `urllib.request.build_opener(_NoAuthRedirectHandler)`) so the redirect handler applies to BOTH the POST and the /v1/models GET probe. The previous global `install_opener` mutation was fixed (see `docs/research/ADVERSARIAL-AUDIT-SPEC-MODEL-CAPACITY-DISCOVERY.md` BUG #1).
2. **MiniMax body-level error decode.** For `caller == "minimax"`, HTTP 200 can still mean failure — the body has `base_resp.status_code != 0`. Decoded and surfaced as `TestResult.ok = False`.
3. **/v1/models probe (best-effort).** Only runs for callers in `_OPENAI_COMPATIBLE` (skipped for Anthropic). Sends `GET <base_url>/models` with the same auth header. Tries BOTH the full model string AND the bare (prefix-stripped) form against each model's `id` field (OpenRouter keeps `id="openai/gpt-4o"`; OpenAI direct strips to `id="gpt-4o"`).
4. **Field-name fallback chain.** `("context_window", "max_context_length", "context_length", "max_tokens", "max_model_len")` — first match wins. Non-int values (e.g. `None` or string) are skipped.
5. **All probe failures are non-fatal.** Probe is best-effort; on any error (`URLError`, `HTTPError`, `TimeoutError`, JSON decode, etc.) `context_window` stays `None` and `TestResult.ok` stays `True` (the chat POST succeeded).

**Caller-default fallback table (`_CALLER_DEFAULT_MAX_TOKENS`):**

| Caller | Default context window | Verified against |
|--------|-----------------------|-------------------|
| `openai` | 128_000 | gpt-4o, gpt-4-turbo published limits |
| `anthropic` | 200_000 | claude-3+, claude-4 published limits |
| `minimax` | 1_048_576 | MiniMax-M2.7, MiniMax-M3 docs |
| `openrouter` | 128_000 | most providers; outliers discoverable via /v1/models |
| `zai` | 128_000 | GLM-4.5+, glm-5 series |
| (unknown) | 128_000 | global safe default |

**Threading:** None inside `test_connection` itself. The settings handler (`ui/handlers/settings_handler.py::test_provider`) wraps the call in a `threading.Thread` and dispatches the result via `GLib.idle_add`. The dialog's `_on_test_result` runs on the GTK main thread.

**Spec:** `docs/specs/SPEC-MODEL-CAPACITY-DISCOVERY.md`. **Audit:** `docs/research/ADVERSARIAL-AUDIT-SPEC-MODEL-CAPACITY-DISCOVERY.md`.

### 3.11b `models/providers.py` — Per-Provider Context Window Resolution

**Resolution chain in `agent/runtime.py::_compute_model_max`:**

1. `provider_cfg.max_tokens` if `> 0` (explicit user set, wizard default, or successful probe pre-fill)
2. `caller_default_max_tokens(provider_cfg.caller)` (per-caller static fallback table — ensures MiniMax-M3 actually uses 1M context, not 128K)
3. `FALLBACK = 128_000` (global safe default for unknown callers)

The `default_max_tokens` field on `ProviderConfig` records the *configured intent* (set by auxilium wizard from `CALLER_DEFAULT_MAX_TOKENS`); it's used as a sentinel marker so Test Connection's pre-fill logic doesn't overwrite a wizard's deliberate choice. See audit BUG #7.

**Context mode (P10):** `ProviderConfig.context_mode` (default `"auto"`) stores
the context discovery strategy. `validate_provider_context_mode()` normalizes
input (case-insensitive, whitespace stripped) and validates against the
allowed set: `{"auto", "preload", "jit", "hybrid"}`. Called by
`resolve_context_mode()` in `agent/context.py`.

### 3.11c `utils/provider_url.py` — Provider URL Validation (MED-5)

**Responsibility:** Validates that non-loopback provider URLs use `https://`.
Loopback addresses (`localhost`, `127.0.0.1`, `::1`) may use `http://` for
local servers (e.g., Ollama).

**Public API:**
```python
def validate_provider_url(url: str) -> None   # raises ValueError for non-HTTPS non-loopback
```

**Architecture:** Pure utility — no GTK, no network, no imports beyond stdlib.

### 3.11d `utils/providers_store.py` — Provider YAML Persistence

**Responsibility:** Load, save, add, remove, and update provider configurations
from `<config_dir>/providers.yaml`. Handles atomic writes (temp file +
`os.replace`) with `chmod 0o600` for security. Also handles legacy migration
from `agent.json`'s `providers` key on first load.

**Public API:**
```python
def get_providers_path() -> str                          # path to providers.yaml
ndef load_providers() -> list[ProviderConfig]             # load from YAML (or JSON fallback)
def save_providers(providers: list[ProviderConfig]) -> None  # atomic write, chmod 0o600
def add_provider(p: ProviderConfig) -> None               # append + save
def remove_provider(name: str) -> None                    # filter + save
def update_provider(name: str, **fields) -> None          # in-place update + save
def has_any_verified_provider() -> bool                   # True if any provider has last_verified_at
def migrate_from_agent_json(agent_json_path: str) -> int  # legacy migration; returns count migrated
```

**Architecture:** Pure functions — no GTK, no state, no network. Imports
`models.providers.ProviderConfig` and stdlib only (`yaml` if available,
`json` fallback). Mirrors the patterns from `utils/feed_store.py` and
`utils/agent_defs.py`.

### 3.12 `ui/handlers/agent_list_handler.py` — Agent List Handler (Agent Cards)

**Responsibility:** Agent card rendering data — initials, colors, sorting. Does NOT build widgets (view does).

**Owns:** AgentManager reference (set after connect), color assignment per agent name, sorting/grouping logic.

**Public API:**
```python
def set_agent_mgr(agent_mgr): pass
def has_agent_mgr() -> bool: pass          # True if AgentManager is populated
def compute_initials(name: str) -> str: pass
def get_agent_color(name: str) -> str: pass
def get_sorted_agents(project_members=None) -> list[(sk, name, in_project)]: pass
def on_chat_clicked(session_key: str, name: str): pass
def on_toggle_clicked(session_key: str, name: str, in_project: bool): pass
```

### 3.13 `ui/handlers/prompts_handler.py` — Prompts Handler

**Responsibility:** Prompts tab data and logic — favorites, search, last-used tracking, prompt content loading. Does NOT build widgets (view does).

**Owns:** In-memory prompt list, favorites set, last-used timestamps, active search query.

**Public API:**
```python
def load_prompts() -> list[dict]:         # scan prompts/ dir, sort (favs first), apply filter
def toggle_favorite(filepath: str) -> bool:   # True if now favorited
def search(query: str) -> list[dict]:    # set filter + return filtered results
def record_usage(filepath: str):          # track last-used timestamp
def get_last_used_str(filepath: str) -> str:   # "just now", "5m ago", etc.
def get_prompt_content(filepath: str) -> tuple[str, str]:   # (name, content)
def on_prompt_activated(filepath: str):   # load + fire on_prompt_loaded callback
```

### 3.14 `ui/handlers/chat_handler.py` — Chat Handler (Phase 1)

**Responsibility:** All chat logic — sending, project fan-out, incoming message routing, tab switching. Extracted from `window.py` in Phase 1.

**Special agent routing:** In project fan-out (solo DM + group broadcast), special agents are detected via `AgentRuntimeHandler.get_special_agents()` and routed through `send_to_special_agent()` instead of `gw.send_message()`. Gateway agents receive awareness data via `_build_awareness_prefix()` (raw `build_awareness_block` — no identity injection).

**Key setters:** `set_gateway_client()`, `set_project_handler()`, `set_command_handler()`, `set_chat_render_handler()`, `set_agent_runtime_handler()`, `set_agent_manager()`, `set_on_forward_message()`, `set_on_send_initiated()`, `set_on_res_confirmed()`, `set_on_activity_bubble()`

**Bug fix (Phase 1 of SPEC-smarter-chat-ux) — missing message recovery:**

New state variables:
- `_assistant_text_buffer[session_key] → str`: last assistant text per session (populated by `_buffer_assistant_text()` callback from ActivityHandler)
- `_chat_final_rendered[session_key] → bool`: True when a final response has rendered — prevents double-render. Cleared on each new agent round via `_clear_render_guard()`.

New methods:
- `_buffer_assistant_text(session_key, text)`: callback target for ActivityHandler's `set_on_assistant_buffer()` — populates `_assistant_text_buffer`
- `_clear_render_guard(session_key)`: callback target for ActivityHandler's `set_on_agent_start()` — clears `_chat_final_rendered` so subsequent responses render. Receives RAW `session_key` from the gateway event (not resolved via `_active_session()`).
- `_handle_lifecycle_completed(session_key, text)`: fallback render path — called when ActivityHandler fires lifecycle end/error but no chat final has rendered; resolves project tab via `agent_to_project` routing table (same pattern as `on_chat_event`), then renders buffered assistant text via `_handle_final_response()` with resolved `target_tab` and original `session_key`. Guarded by `_chat_final_rendered`.
- `_render_activity_bubble(bubble)`: called by ActivityHandler via `set_on_activity_bubble()` — dispatches to `_render_activity_bubble_impl` on main thread. Activity bubbles are NOT guarded by `_chat_final_rendered`.
- `_render_activity_bubble_impl(session_key, text)`: thread-unsafe internal render — resolves project tab via `agent_to_project` routing table when no direct tab exists, then calls `render_sync(role="System", text=..., tight=True)`, appends to chat box, scrolls.

**Architecture:** ChatHandler owns all render decisions. ActivityHandler only tracks state. The lifecycle-completed callback is the fallback path when the gateway sends `stream=assistant` text but the corresponding `chat final` event carries no message body. The render guard is cleared when a new agent round starts (lifecycle phase=start) so that multiple responses per session work correctly. Activity bubbles are separate from the conversation message flow — they don't interact with `_chat_final_rendered`.

### 3.14a `utils/escaping.py` — Pango-Aware XML Escape

**Responsibility:** Escape XML/Pango specials while preserving known Pango markup tags.

**Public API:**
```python
from utils.escaping import escape_for_pango, xml_escape_text

# Escape specials, preserve known Pango tags (<b>, <i>, <u>, <s>, <tt>, <span>, <a>, <sub>, <sup>, <big>, <small>, <o>).
# Unknown tags (including HTML void elements <br>, <hr>, <img>) are escaped — prevents Pango from silently
# rendering the ENTIRE message as empty when it encounters an unknown tag, and prevents
# cascade-failure markup errors when literal <br>/<hr>/<img> appear inside <tt> code spans
# (regression fixed 2026-07-10; see Bug #N in .crabcakes/coder-bugs.md).
safe = escape_for_pango("<b>bold</b> and <script>x</script>")
# → "<b>bold</b> and &lt;script&gt;x&lt;/script&gt;"
safe = escape_for_pango("line1<br>line2")
# → "line1&lt;br&gt;line2"  (void tags escaped, not preserved)

# Simple XML entity escaping for plain text (no Pango markup)
xml_escape_text("Tom & Jerry")  # → "Tom &amp; Jerry"
```

**Key design:** Uses a Pango-known-tag whitelist (`_PANGO_KNOWN_TAGS`). Only tags in this set are preserved; everything else (HTML, `<script>`, `<div>`, AND HTML void elements `<br>`/`<hr>`/`<img>`/`<wbr>`) is escaped. This prevents the critical bug where Pango renders unknown tags as invisible, making the entire message content disappear, AND the cascade-failure bug where a literal `<br>` inside a `<tt>` code span (added by `format_markdown` for inline code) opened a real Pango `<br>` element that `</tt>` could not close, silently emptying the bubble. Void tags are intentionally escaped — line breaks in chat come from `\n` (GtkLabel wraps naturally); `<hr>`/`<img>` have no business in chat bubbles and are far more common in code snippets, terminal output, shell heredocs, and HTML examples than as genuine Pango layout directives.

### 3.14b `utils/markdown.py` — Inline Markdown → Pango Markup

**Responsibility:** Convert inline markdown formatting to Pango Markup for use in `Gtk.Label.set_markup()`.

**Public API:**
```python
from utils.markdown import format_markdown

# Conversion rules:
#   **bold**   → <b>bold</b>
#   *italic*   → <i>italic</i>
#   `code`     → <tt>code</tt>   (underscores inside code are protected)
#   ~~strike~~ → <s>strike</s>
#   [text](url)→ <a href="url"><u>text</u></a>
#   bare URL   → clickable <a href="..."> link (trailing punctuation stripped)
result = format_markdown("use `my_var` for **bold** and *italic*")
```

**Important:** Handles ONLY inline formatting. Block-level elements (code blocks, blockquotes) are handled by `utils/block_parser.py` in Phase 2.

### 3.14b.1 `utils/gtk_safe_link.py` — GTK Link Safety Guard (HIGH-6)

**Responsibility:** Creates `Gtk.Label` widgets with an `activate-link` handler
that gates navigation on the link scheme. Non-allowlisted schemes are blocked
from opening. This is defense-in-depth on top of the render-time allowlist in
`utils/markdown.py`.

**Allowed schemes:** `http`, `https`, `mailto` (matches
`_ALLOWED_LINK_SCHEMES` in markdown.py).

**Blocked schemes:** `file://`, `smb://`, `ftp://`, `javascript:`, `data:`,
custom URI schemes, and relative paths that resolve to `file://`.

**Public API:**
```python
def on_activate_link(_label, uri: str) -> bool    # True = block, False = allow
def make_safe_label(markup: str, ...) -> Gtk.Label  # pre-wired label
```

**Architecture:** This file imports `gi.repository.Gtk` and `Pango` (lazy,
inside functions). See §2 GTK carve-out for `utils/` for rationale.

### 3.14c `ui/views/chat_bubble.py` — Chat Bubble Widget Factories

**Responsibility:** Create styled GTK bubble widgets for chat messages.

**Public API:**
```python
from ui.views.chat_bubble import build_role_bubble

# "You" bubbles are right-aligned, agent bubbles left-aligned.
# CSS classes: .chat-bubble-you / .chat-bubble-agent
widget = build_role_bubble("Agent", "<b>Hello</b> and **bold** text")
```

**Architecture:** A **view** — only creates widgets. No state, no callbacks, no logic.

### 3.14d `ui/handlers/chat_render_handler.py` — Chat Render Orchestrator

**Responsibility:** Owns bubble creation. Calls `build_role_bubble()` which owns the full text processing pipeline (extract_blocks → per-segment escape/markdown/highlight). Exposes `render_sync()` for synchronous use and `render()` for thread-safe async use.

**Public API:**
```python
from ui.handlers.chat_render_handler import ChatRenderHandler

handler = ChatRenderHandler(GLib_module=GLib)

# Async (thread-safe): dispatch work to main thread, call callback with bubble
# session_key enables reentrancy guarding — concurrent renders for the same
# session_key are skipped to prevent visual glitches.
handler.render(role, text, session_key, on_bubble_ready, on_error=None)

# Sync (main thread only): return bubble immediately
widget = handler.render_sync(role, text, session_key=None)

# Phase 4: special event card (file_read, edit_proposal, tool_call, error, thinking)
handler.render_event_card(event_type, container, **fields)
```

**Phase 4 — `render_event_card(event_type, container, **fields)`:**
Dispatches to the appropriate event card factory in `chat_bubble.py`. Thread-safe via `_dispatch()`. Unknown event types are silently ignored.

**Reentrancy guard (`_ReentrancySet`):** Tracks which session keys are currently being rendered. If a render is already in-flight for a key, subsequent calls with that same key are skipped.

**Processing pipeline:**
1. `escape_for_pango(text)` — protect existing Pango markup tags
2. `format_markdown(text)` — convert markdown → Pango inline markup
3. `build_role_bubble(role, text)` — create styled GTK bubble widget

### 3.14e `ui/views/chat_bubble.py` — Bubble Widget Factories (Phase 2–5)

**Responsibility:** Pure widget factories — create GTK bubble widgets for all message types. No state, no callbacks beyond widget signals.

**Phase 5 additions:**
- `build_role_bubble(role, text, on_forward_click=None, tight=False)`:
  - `on_forward_click`: optional callback; when set, agent bubbles get Copy+Forward buttons
  - `tight`: reduces margin_top from 4→1 for consecutive same-role messages (message grouping)
  - Action buttons row (Copy/Forward) only on agent bubbles — revealed via CSS hover (opacity 0 → 1)
  - Copy button: `_copy_to_clipboard(text)` copies full bubble text
  - Forward button: calls `on_forward_click()` if registered, else logs stub

**Message grouping:** Consecutive messages from same role+session get tight spacing (1px top margin instead of 4px). Tracked in `ChatRenderHandler._last_message_key`.

### 3.14f `ui/views/main_content.py` — Scroll-to-Bottom Button (Phase 5)

**Phase 5 addition:** Floating ↓ button bottom-right of chat overlay.

**New components:**
- `self._scroll_btn` — Gtk.Button, opacity 0 by default
- `self._scroll_btn_box` — positioned bottom-right via `Gtk.Align.END`

**Wired in `create_chat_tab()`:**
- Scroll button box added to each tab's `chat_overlay`
- Each tab's `vadjustment.value-changed` → `_on_vadjustment_changed()`
- Shows (opacity=1) when scrolled >80px from bottom; hides when near bottom

**Scroll handler (`_on_vadjustment_changed`):**
```python
distance_from_bottom = upper - page_size - value
self._scroll_btn.set_opacity(1 if distance_from_bottom > 80 else 0)
```

**Click handler (`_on_scroll_to_bottom_clicked`):**
```python
self.scroll_chat_to_bottom()
self._scroll_btn.set_opacity(0)
```

### 3.14g `utils/block_parser.py` — Block Segment Extractor (Phase 2)

**Responsibility:** Split raw message text into typed block segments. Pure function, no GTK, no network.

**Public API:**
```python
from utils.block_parser import extract_blocks

segments = extract_blocks("Hello\n\n```python\nx = 1\n```")
# [{'type': 'text', 'content': 'Hello'},
#  {'type': 'code', 'content': 'x = 1', 'lang': 'python'}]
```

**Segment types produced:**

| Type | Description | Key fields |
|------|-------------|------------|
| `text` | Plain paragraph | `content` |
| `code` | Fenced code block | `content`, `lang` |
| `quote` | `>` blockquote | `content` |
| `terminal` | `$` command lines | `content` |
| `heading` | `#` heading | `content`, `level` (1-4) |
| `task` | `- [ ]` / `- [x]` | `content` |

**Processing order:** Fenced code blocks are extracted first (since they can contain `$`, `#`, `>` chars). Remaining text is split on blank lines and classified.

### 3.14h `utils/syntax_highlight.py` — Pygments → Pango Highlighter (Phase 2)

**Responsibility:** Convert source code to Pango Markup with syntax colors. Degrades gracefully if Pygments unavailable.

**Public API:**
```python
from utils.syntax_highlight import highlight

markup = highlight("def foo(): pass", "python")
# '<span foreground="#c792ea">def</span> ...'
```

**Color scheme:** Tokyo Night dark theme (16 token color mappings). Falls back to `<tt>escaped</tt>` if Pygments unavailable or lexer unknown.

**Security:** All output is HTML-escaped before span wrapping. Safe for untrusted code content.

### 3.14i `ui/views/chat_bubble.py` — Block-Aware Bubble Factory (Phase 2+4)

**Responsibility:** Build styled GTK bubble widgets for any message content. Handles both inline (Phase 1) and block-level (Phase 2) rendering.

**Phase 1** (unchanged API):
- `build_role_bubble(role, text)` — creates bubble, routes text through extract_blocks internally
- Text segments → `format_markdown()` → bold/italic/code links

**New helper (Phase 5 — shared header factory):**

```python
def _make_block_header(
    label_text: str,
    content_for_copy: str,
    header_css: str,
    copy_btn_css: str = "code-copy-btn",
) -> tuple[Gtk.Box, Gtk.Button]:
    """
    Shared header bar factory for code/terminal block widgets.
    Returns (header_box, copy_btn) — copy_btn is pre-wired.
    """
```

Used by `_build_code_segment` and `_build_terminal_segment`. Eliminates copy-paste between block builders.

**Phase 5 — Project Solo DM (per-project direct message override):**

Right-clicking a project tab now shows a project-specific menu (All / member entries) instead of the generic session switcher. This allows routing messages to a single project member rather than broadcasting to all.

- `ProjectHandler._solo_targets: dict[str, str | None]` — maps project_name → solo target session_key, or None for group broadcast
- `ProjectHandler.get_solo_target(project_name) -> str | None`
- `ProjectHandler.set_solo_target(project_name, member_session_key | None)`
- `ChatHandler.on_send()` queries `get_solo_target()` before fan-out; if set, sends to only that member
- `MainContent._on_tab_right_click()` detects `session_key.startswith("project:")` and routes to `show_project_menu()` instead of `show_session_menu()`
- `session_menu.show_project_menu(parent, project_name, member_names, current_solo, on_select)` — builds the popover with checkmark on current selection

**Phase 4 additions** — Event card widget factories:
- `create_file_card(file_path, snippet, line_range)` → `.bubble-file-read` card (green border, 📄 icon)
- `create_edit_card(file_path, diff)` → `.bubble-edit-proposal` card (amber border, ✏️ icon)
- `create_tool_card(tool_name, detail)` → `.bubble-tool-call` card (slate border, 🔧 icon)
- `create_error_bubble(error_msg)` → `.bubble-error` bubble (red border + tint, ❌ icon)
- All user content is Pango-escaped via `escape_for_pango()`
- All content is `set_selectable(True)` for copy

**Architecture:** Each segment becomes a child widget inside a vertical `Gtk.Box`. The bubble's CSS class (`.chat-bubble-you` / `.chat-bubble-agent`) controls bubble background.


### 3.14j `ui/views/activity_drawer.py` — Activity Drawer (SPEC-activity-drawer)

**Responsibility:** Collapsible GTK panel below the chat that displays activity events (tool calls, plans, approvals, command output, patches, lifecycle separators) in a scrollable `Gtk.ListBox`. Pure view — no business logic, no gateway calls, no state mutations beyond its own widget tree.

**Owns:** Internal widget tree (header bar, list, filter state, per-agent counters, separator tracking, expanded revealers). All state is private to the drawer.

**Public API:**
```python
class ActivityDrawer(Gtk.Box):
    def __init__(self) -> None
    def append_event(self, row: dict) -> None     # row from ActivityBubble.to_drawer_row()
    def on_agent_start(self, session_key: str, agent_name: str) -> None
    def on_agent_end(self, session_key: str, agent_name: str) -> None
    def clear_events(self) -> None                  # remove all rows, reset state
    def toggle(self) -> None                       # programmatic expand/collapse
```

**Counter-collapse behavior:** Consecutive events with the same `(agent_name, activity_type)` are merged in place — count increments, duration sums. The first event of a new `(agent, type)` pair opens a new row.

**Filter semantics:** Two filter dropdowns (agent, type) with AND semantics. Empty set = all pass. Filter state lives in `self._visible_agents` and `self._visible_types`. Filter resets on `clear_events()`.

**Click-to-expand:** Rows with `output` set (command_output, tool_end, tool_error) get a `Gtk.Revealer` that toggles on row click, showing the last 10 lines.

**Lifecycle separators:** `on_agent_start` / `on_agent_end` insert marker rows that break the counter chain for the named agent and show per-agent stats on end.

**Thread safety:** `append_event`, `on_agent_start`, `on_agent_end`, `clear_events`, `toggle` must all be called on the GTK main thread. ActivityHandler already dispatches via `GLib.idle_add()` before firing callbacks.

**Architecture rules:**
- Lives in `ui/views/` — no imports from `gateway/`, `agent/`, `ui/handlers/`
- Receives data as flat dicts (see `models/activity.py:ActivityBubble.to_drawer_row()`)
- Pure view — no business logic, no state beyond the widget tree
- Connected to ActivityHandler via `set_on_activity_bubble` (adapter in `activity_wiring_handler.py` converts `ActivityBubble` to dict via `to_drawer_row()`)


### 3.15 `ui/handlers/gateway_handler.py` — Gateway Handler (Phase 2)

**Responsibility:** All gateway lifecycle — connecting, disconnecting, agent discovery, error handling, and thread-safe state dispatch to GTK. Extracted from `window.py` in Phase 2.

**Owns:**
- `GatewayClient` instance (`_gw`)
- `AgentManager` instance (`_agent_mgr`)
- Sync callback (`_sync_callback`) — window uses this to sync `_gw` reference into `ChatHandler`

**Key invariant:** All GTK calls go through `GLib.idle_add()`. Gateway callbacks fire from the gateway's background thread; GTK is not thread-safe.

**Public API:**
```python
def connect() -> None:
    """Create GatewayClient, start it, set connection state to 'connecting'."""

def disconnect() -> None:
    """Stop GatewayClient, set connection state to 'disconnected', clear AgentManager."""

def is_connected() -> bool:
    """True if GatewayClient is running and connected."""

@property
def agent_mgr() -> AgentManager | None:
    """Returns AgentManager if connected, else None."""

def set_sync_callback(cb: Callable) -> None:
    """Window calls this to receive the live GatewayClient reference after connect succeeds."""

def dispatch(fn: Callable, *args, **kwargs) -> None:
    """Thread-safe dispatch to main thread via GLib.idle_add(fn, *args, **kwargs)."""
```

### 3.16 `ui/handlers/media_handler.py` — Media Handler (Phase 4)

**Responsibility:** All media I/O — STT (whisper.cpp push-to-talk) and prompt improvement. Extracted from `window.py` in Phase 4.

**Owns:**
- `STTEngine` instance (`_stt_engine`) — owns its own background capture thread
- Sync callback (`_sync_callback`) — window sets this to trigger `ChatHandler.on_send()` after voice input

**Thread safety:** `_on_stt_partial` fires from the STT background thread. GTK calls go through `GLib.idle_add()`.

**Public API:**
```python
def on_stt_click(_btn=None):
    """Toggle STT recording — start or stop. On stop, appends transcript and calls sync callback."""

def on_improve_click(_btn=None):
    """Send current input text to MiniMax improve API. Disables button, calls _on_improve_result on response."""

def set_on_send_callback(cb: Callable):
    """Window sets this so voice input automatically triggers ChatHandler.on_send()."""
```
```
**Rules:**
- Handler does NOT import other handlers -- window wires them together
- STTEngine runs its own background thread; handler dispatches all GTK calls via `GLib.idle_add()`
- improve_prompt() callback is already GLib-dispatched when `GLib_module` is provided

### 3.17 `utils/icons.py` — SVG Icon Rendering

**Responsibility:** Renders agent avatars and project folder icons as `Gdk.Texture`.

**Public API:**
```python
def render_agent_icon(color_hex: str, initials: str, size: int = 44) -> Gdk.Texture:
    """Colored circle with inscribed hexagon outline + 2-char initials."""

def render_folder_icon(color_hex: str, letter: str, size: int = 44) -> Gdk.Texture | None:
    """Colored folder SVG with tab notch + white letter. Returns None on error."""
```

### 3.17a `utils/improve.py` — Prompt Improvement (Template Mode)

**Responsibility:** Improve user prompt text via MiniMax API. Loads a system prompt template from `prompts/improve-system-prompt.md`, injects the user's input text at the `{{USER_INPUT}}` marker, and sends the assembled prompt to the API.

**Template mode:** If `{{USER_INPUT}}` is found in the loaded prompt file, the marker is replaced with the user's text and sent as a single `user` message. This gives the prompt file full control over structure and placement.

**Legacy mode:** If `{{USER_INPUT}}` is not found, falls back to two-message split (`system` + `user`). Backward compatible with prompt files that don't use the marker.

**Public API:**
```python
USER_INPUT_MARKER = "{{USER_INPUT}}"

def improve_prompt(raw_text, callback, GLib=None)
    # raw_text: user input box text
    # callback(improved_text, error) — GLib.idle_add dispatched if GLib provided
```

**Config:** Reads `apiKey`, `baseUrl`, `model` from `~/.config/crabcakes/config.json`. Defaults: MiniMax API, `MiniMax-M2.5-Lightning`.

### 3.18 `models/colors.py` — Color Palette

**Responsibility:** Agent and project color assignment via round-robin, plus stable per-role color lookup for special agents.

**Public API:**
```python
AGENT_COLORS: list[str]                           # 10-color palette
next_agent_color() -> str                         # round-robin for live agents; advances _agent_color_next
next_project_color() -> str                       # same palette, separate counter — used by ProjectListHandler
color_for_special_agent(role: str) -> str        # stable per-role cache; NEVER re-assigns once set
reset_color_indices()                             # resets _agent_color_next and _project_color_next to 0
                                                  # NOTE: does NOT reset _SPECIAL_AGENT_COLORS — that cache
                                                  # survives gateway reconnects by design (SPEC-agent-color-stability)
```

**Module-level state:**
- `_agent_color_next: int` — shared round-robin counter, advanced by both `next_agent_color()` and `color_for_special_agent()`. This means the palette is shared between live agents and special agents: if 3 special agents exist, the first live agent gets index 3 (`#f43f5e`). This is intentional — both populations should draw from the same color pool without colliding.
- `_project_color_next: int` — separate counter for `next_project_color()`.
- `_SPECIAL_AGENT_COLORS: dict[str, str]` — stable cache keyed by role string; populated on first `color_for_special_agent(role)` call. Survives `reload_registry()` (in `agent/special_agents.py`) and `reset_color_indices()` (this module). Designed for the AgentListHandler.get_agent_color(name) → display_name → role → color lookup chain.

**Cross-module call sites:**
- `agent/special_agents.py` — no longer imports from `models/colors` (Phase 4 of SPEC-agent-color-stability removed the dependency). The `color` field was deleted from `SpecialAgentDef`.
- `models/agents.py:35` — `AgentManager._assign_color()` calls `next_agent_color()` for live-agent registration. Cache `AgentManager._agent_colors` (per-name dict) survives `clear()` by design.
- `ui/handlers/agent_list_handler.py:59-83` — `get_agent_color(name)` calls `color_for_special_agent(role)` inside the method body (deferred import) to avoid circular dependency with `agent/special_agents`.

### 3.19 `ui/handlers/project_handler.py` — Project Handler (Phase 3)

**Responsibility:** Project tab lifecycle and agent-to-project membership routing. Extracted from `window.py` in Phase 3.

**Owns:**
- `_active_project_name` — currently open project name (or None)
- `_agent_to_project` — AgentRoutingTable instance; shared with ChatHandler (injected by window at construction)

**Does NOT own:** MainContent, LeftPanel, ChatHandler — received as dependencies.

**Thread safety:** All GTK operations dispatched via `GLib.idle_add()`. Entry points (`open_project`, `toggle_agent`) are called from the GTK main thread, so no background thread concerns.

**Public API:**
```python
def open_project(name: str, path: str):
    """Create a project tab and populate agent-to-project routing lookup."""

def toggle_agent(session_key: str):
    """Add or remove an agent from the active project membership."""

def is_project_session(session_key: str) -> bool:
    """True if session_key belongs to any known project. Used by ChatHandler."""

def get_project_for_agent(session_key: str) -> str | None:
    """Return project name for agent's session_key. Used by ChatHandler for response routing."""

def get_project_members(project_name: str) -> list[str]:
    """Return member session keys for a project. Used by ChatHandler for fan-out."""

def get_active_project_name() -> str | None:
    """Return currently active project name, or None."""

def get_agent_session_in_project(project_name: str, agent_name: str) -> str | None:
    """Return the session key an agent currently uses in a project."""

def update_agent_session(project_name: str, old_session_key: str, new_session_key: str):
    """Replace an agent's session key in a project. Updates members.json + routing table."""

def set_agent_manager(agent_mgr) -> None:
    """Inject AgentManager after gateway connect."""

def set_on_solo_target_changed(cb): pass
    # Spec PROJECT-SETTINGS-BAR-ENHANCED: fired after set_solo_target() changes
    # a project's solo DM target, cb(project_name: str). Only fires on a REAL
    # change (old == new -> no-op) and only for a VALIDATED existing project
    # (_get_project_path is not None, BUG #3 — unknown names are rejected).

def set_solo_target(project_name, member_session_key): pass
    # Set/clear the solo DM target for a project. Validates the project exists
    # (unknown -> no-op, no state, no callback). Fires _on_solo_target_changed
    # only when the value actually changes.

def set_on_members_changed(cb: Callable): pass
def set_on_navigate_back(cb: Callable): pass
def close_project(name: str): pass

def set_on_project_created(cb: Callable[[str, str], None]): pass
    # SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS §2.6: fired after create_project()
    # completes open_project(). cb(name, path). Registered ONLY for project
    # creation, not open_project(). Used by window.py to emit the System bubble
    # instructing the user to add Supervisor manually.
```

**SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS §2.6 — Project-created callback & membership changes:**

- `create_project()` fires `set_on_project_created` callbacks after `open_project()` completes, dispatched via `GLib.idle_add` so the project tab exists before the callback body runs.
- `_auto_add_onboarding_agents()` constructs `TeamMember` from `SpecialAgentDef` fields (`display_name`, `role`, `can_write`) instead of hardcoding `role="onboarding guide"`.
- `_save_members()` resolves `special:*` session keys via `get_special_agent()` to backfill `display_name`/`role`/`can_write`. Gateway/unknown keys get blank metadata (display-time resolution). Existing member records are preserved exactly. No implicit git commit on roster writes — snapshot refresh via `build_awareness_snapshot()`/`save_awareness_snapshot()` runs after `save_team()`.

### 3.20 `ui/handlers/project_list_handler.py` — Project List Handler

**Responsibility:** Project card data for the Projects tab — color assignment, project listing, click handling. Does NOT build widgets (view does).

**Owns:** Project color round-robin (separate counter from agent colors), in-memory project list.

**Public API:**
```python
class ProjectListHandler:
    def __init__(self, *, on_project_opened: Callable | None = None)
    def get_projects() -> list[tuple[str, str, str]]    # (name, path, color)
    def get_project_color(path: str) -> str
    def on_project_clicked(name: str, path: str)
```

### 3.21 `ui/views/session_menu.py` — Session Switcher Popover

**Responsibility:** GTK popover listing active sessions for an agent. Right-click to switch.

**Public API:**
```python
show_session_menu(parent, agent_name, sessions, on_select)
show_project_menu(parent, project_name, member_names, current_solo, on_select)
```

### 3.21a `ui/handlers/command_handler.py` — Command Parser — Slash Prefix (Phase 7)

**Responsibility:** Parse slash-prefixed commands, resolve `@mentions`, dispatch to command handlers.

**Quoted payloads:** A2A commands use quoted payloads per `A2A_QUOTED_PAYLOAD_SPEC` — the payload is wrapped in double quotes (`"payload"`). The parser (`_parse_quoted_payload()` in `utils/quoting.py`) handles `\"` and `\\` escapes. The canonical format is `/cmd @Agent "payload"`.

**Owns:** CommandRegistry, command prefix, `@mention` resolution.

**Public API:**
```python
CommandHandler(gateway_client, agent_manager, project_handler, GLib_module, on_display_card, on_display_text)

def process_input(session_key, text, skip_dispatch=False) -> CommandResult    # parse + execute command
def set_gateway_client(gw) -> None
def set_agent_manager(agent_mgr) -> None
def register_command(name, handler, aliases=None, help_text="") -> None
def set_prefix(char) -> None
def get_help(name) -> str | None
```

**Thread safety:** All GTK via `GLib.idle_add()`.

### 3.21b `ui/handlers/review_handler.py` — Review Session Handler (Phase 7) — *Superseded by Feed Cards*

**Responsibility:** Review session lifecycle — checkpoint, check changes, accept, reject. Coordinates git_ops, diff_parser, and GTK views.

**Status:** **Superseded by the project feed card system + SPEC-3 structured feedback protocol.** The `/review`/`check`/`accept`/`reject` commands and ReviewBar are kept in the codebase for potential future use, but the primary review workflow is now: enforcement layer catches issues on write → feed cards surface events in the feed bar → audit reports (`## Audit Report` blocks) log bugs to the target agent's bug journal. This is more intuitive and always-on compared to the mode-gated checkpoint flow. See `docs/specs/SPEC-3-structured-feedback.md`.

**Owns:** Per-project `ReviewState` dict.

**Public API:**
```python
ReviewHandler(GLib, main_content, project_handler, on_review_started, on_review_ended, on_display_card, on_display_text)

def set_review_mode(project_name, mode)        # "off" | "review"
def get_review_mode(project_name) -> str
def start_review(project_name)                   # git add -A && git commit → checkpoint SHA
def check_changes(project_name)                  # git diff <sha> → display diff cards
def accept_changes(project_name, message)       # git add -A && git commit
def reject_changes(project_name, reason)        # git checkout <sha> -- .
def reject_file(project_name, file_path)
def get_state(project_name) -> ReviewState | None
def on_project_opened(project_name, project_path)
def on_project_closed(project_name)
def set_chat_handler(chat_handler)
def set_gateway_client(gw)
```

**Thread safety:** All GTK via `GLib.idle_add()`. All git calls in background threads.

### 3.21c `ui/views/review_bar.py` — Review Bar Widget (Phase 7) — *Superseded by Feed Cards*

**Responsibility:** GTK widget for review mode controls — dropdown, status, action buttons.

**Status:** **Superseded.** See 3.21b. The ReviewBar was non-functional from creation (GTK3 `pack_start` call) until 2026-05-21 (fixed to `prepend()`). It works now but is not the primary review mechanism — feed cards serve that role.

**Public API:**
```python
ReviewBar(on_mode_changed, on_start_clicked, on_check_clicked)

def set_review_mode(mode)              # update dropdown without firing callback
def set_status(text)
def set_state_idle()
def set_state_reviewing(checkpoint_sha)
def set_state_has_changes(file_count, additions, deletions)
def set_loading(loading)
def set_accept_callback(cb)
def set_reject_callback(cb)
```

### 3.21d `ui/handlers/task_handler.py` — Task Commands (Phase 7)

> **DEPRECATED (SPEC-TASK-SYSTEM-FULL-REDESIGN §14):** `TaskHandler` is superseded by `WorkHandler`
> (`ui/handlers/work_handler.py`) in production wiring; `TaskHandler` is kept for import compatibility.

**Public API:**
```python
class TaskHandler:
    def __init__(self, on_display_card, on_display_text, GLib_module)
    def cmd_task(cmd) -> CommandResult
    def cmd_done(cmd) -> CommandResult
    def cmd_start(cmd) -> CommandResult
    def cmd_blocked(cmd) -> CommandResult
    def cmd_cancel(cmd) -> CommandResult
    def cmd_tasks(cmd) -> CommandResult
    def cmd_assign(cmd) -> CommandResult
    def cmd_priority(cmd) -> CommandResult
```

### 3.21d1 `ui/handlers/work_handler.py` — Work Handler

**Responsibility:** Owns the `/work` command semantics (and legacy aliases) for Work Unit lifecycle.
Replaces `TaskHandler` in production wiring. Follows the §8.6 handler pattern — receives all
dependencies via constructor, never imports other handlers, never raises out of `process_input()`.

**Constructor (injected deps):**
```python
class WorkHandler:
    def __init__(
        self,
        project_handler,            # resolve active project (name/path/members)
        work_store,                 # shared WorkUnitStore (injected by window.py)
        agent_runtime_handler=None, # /work start → send_to_special_agent() Supervisor handoff
        on_display_card=None,
        on_display_text=None,
        on_feed_card=None,
        GLib_module=None,
    )
```

**Lifecycle:** `load_for_project(project_path)` loads (or migrates) Work Units and binds the store
(called by `window.py` on project open); `close_project()` releases the binding (persisted data retained).

**`/work` command grammar (legacy aliases route here via CommandHandler):**
- `/work "Title"` (or `/work <unquoted args>`) → create a `draft` Work Unit (`spec_path=""`).
- `/work list`
- `/work start #N`
- `/work done #N [notes]`
- `/work blocked #N [reason]`
- `/work unblock #N`
- `/work cancel #N`
- `/work assign #N @agent`
- `/work priority #N level`
- `/work spec-ready #N <spec_path>`
- `/work status #N` (also carries the status-transition table + authorization)

Legacy aliases registered by `CommandHandler` route to the same `cmd_work_*` methods: `/task`, `/tasks`,
`/start`, `/done`, `/blocked`, `/cancel`, `/assign`, `/priority`. (The old `/t` alias is gone per spec §5.1.)

**`/work start #N` validation + Supervisor handoff (`cmd_work_start`):**
1. Resolves the Work Unit id (`#N`).
2. Authorizes the caller — only the PM or the unit's assigned Supervisor may start (spec §4.6).
3. Validates `spec_path` is set, resolves within the project (path containment), and the spec file exists.
4. Runs the dependency check — a Work Unit whose dependencies are not `done` cannot start.
5. Verifies the assigned Supervisor is a team member (`add the Supervisor agent to begin implementation`).
6. Hands off via `AgentRuntimeHandler.send_to_special_agent(session_key, text)` — **it does NOT call
   runtime internals** (spec §14 runtime boundary). This triggers the implementation loop; the Supervisor
   loads `prompts/implementationLoop.md`.

**Status transition table (enforced in `cmd_work_status`):**
- `draft` → `spec-pending` (only from `draft`)
- `spec-pending` → `spec-ready` (via `/work spec-ready`, PM/Supervisor only; spec file must exist)
- `spec-ready` → `in-progress` (via `/work start`)
- `in-progress` → `auditing` (assigned Supervisor only)
- `in-progress`|`auditing` → `done` (via `/work done`)
- `-> cancelled` (PM only; cannot cancel a `done` unit)
- `-> draft` revert paths guarded; `done` units cannot revert to `draft`

**Authorization (spec §4.6):** lifecycle triggers require the PM or the unit's assigned Supervisor.
`_is_supervisor_or_pm` / `_is_supervisor_only` / `_is_pm` gate cancel, auditing, and spec-ready transitions.

**Persistence:** every mutation calls `_persist()` → `utils/work_persistence.save_work_units()`; writes are
atomic and immediate. `on_feed_card` emits a `FeedCardData` card per action (created/started/done/blocked/
unblocked/cancelled/spec-ready/status).

### 3.21e `ui/handlers/collab_handler.py` — Collaboration Commands (Phase 7)

**Public API:**
```python
class CollabHandler:
    def cmd_ask(cmd) -> CommandResult
    def cmd_delegate(cmd) -> CommandResult
    def cmd_stop(cmd) -> CommandResult
    def cmd_tell(cmd) -> CommandResult
```

### 3.21f `ui/handlers/agent_command_handler.py` — Agent Response Command Parser (Phase 6.2)

**Responsibility:** Scan agent response text for slash-prefixed commands, route them through `CommandHandler.process_input()`, and relay agent-to-agent answers back to the asking agent via pending-ask tracking.

**Owns:** `_pending_asks` (target_sk → source_sk), `_chain_depth` (session_key → depth counter).

**Wiring:** `window.py` creates the handler and wires callbacks into both response pipelines:
- `ChatHandler.set_on_agent_response(ach.on_agent_response)` — gateway agent responses
- `AgentRuntimeHandler.set_on_agent_response(ach.on_agent_response)` — special agent responses

**Architecture:** Follows §8.6 handler pattern — receives all dependencies via setters, never imports from `ui/handlers/`.

**Thread safety:** `on_agent_response()` is called from main thread via `GLib.idle_add()` in both pipelines — no additional dispatch needed.

**Public API:**
```python
class AgentCommandHandler:
    def __init__(self, *, GLib_module=None)

    # Setters (wired by window.py)
    def set_command_handler(handler)             # CommandHandler — process_input() + get_command_names()
    def set_agent_runtime_handler(handler)       # AgentRuntimeHandler — for special agent routing
    def set_gateway_client(gw)                  # GatewayClient — gateway agent routing (may be None)
    def set_agent_manager(mgr)                  # AgentManager — display name resolution
    def set_agent_routing(routing_table)         # AgentRoutingTable — project→agent lookups
    def set_project_handler(handler)             # ProjectHandler — project_path for awareness prefix
    def set_awareness_sent(awareness_set)       # Shared set[str] from ChatHandler — deduplicate awareness

    # Entry point (wired into both response pipelines)
    def on_agent_response(session_key, text, project_name) -> None
        # Step 1: RELAY — if agent has pending ask, deliver response to asking agent
        # Step 2: SCAN — parse slash commands, route through CommandHandler.process_input()
```

**Constants:**
- `_MAX_CHAIN_DEPTH = 3` — max nested command hops before cutoff
- `_MAX_COMMANDS_PER_RESPONSE = 3` — max commands parsed per response
- `_extract_quoted_commands()` function — extracts A2A commands in quoted-payload format (`/cmd @Agent "payload"`) from agent response text. Uses `_parse_quoted_payload()` from `utils/quoting.py` for escape-aware payload extraction. Fenced code blocks (```` ```...``` ````) stripped first. Returns `ParsedCommand` namedtuples. Maximum 3 commands per response.

**Relay mechanism:** `/ask @B "question"` from A → `_pending_asks[B] = A`. When B responds → `_relay_response(A, B, text)` delivers B's answer wrapped as `"[{B} responded]: {text}"`. Only `/ask` and `/delegate` create pending asks — `/tell` is one-way.

**Sender identity:** Outbound messages to target agents are prefixed with `"[{sender_name} asks]: {question}"` so the target knows who's consulting them — not the human.

**Command canonicalization:** `ParsedCommand` results from `_extract_quoted_commands()` are rebuilt into canonical slash-prefix format (`/cmd @Agent "escaped_payload"`) before calling `process_input()`. Backslashes and quotes in the payload are escaped (`\` → `\\`, `"` → `\"`) per A2A_QUOTED_PAYLOAD_SPEC §5.4. Payload-free commands (e.g. `stop`) omit the payload.

**Dispatch suppression:** `process_input()` is called with `skip_dispatch=True` to prevent GTK UI side effects (error bubbles) from background agent-to-agent routing.

**Chain depth:** Each hop increments `_chain_depth[target_sk]`. At `_MAX_CHAIN_DEPTH`, commands dropped and depth cleared. Relay messages do NOT count as hops.

**Routing priority:** direct special agent session key → display name reverse-lookup → gateway send. Gateway sends inject project awareness prefix on first "project:agent" pair.

### 3.21g `ui/handlers/session_handler.py` — Session Switching (Phase 7)

**Public API:**
```python
class SessionHandler:
    def __init__(self, agent_manager, project_handler)
    def set_agent_manager(agent_mgr) -> None
    def set_project_handler(project_handler) -> None
    def cmd_session(cmd) -> CommandResult
```

### 3.21h `ui/views/diff_card.py` — Diff Card Widget Factories (Phase 7)

**Responsibility:** GTK widget factories for diff display in project chat tabs.


**Public API:**
```python
get_lang_from_path(file_path: str) -> str | None
render_diff_hunks(hunks: list[DiffHunk], lang: str | None = None) -> Gtk.Box
build_file_diff_card(file_diff, on_accept_file=None, on_reject_file=None) -> Gtk.Widget
build_diff_summary_card(parsed_diff, on_accept_all=None, on_reject_all=None) -> Gtk.Widget
```

### 3.21i `utils/diff_parser.py` — Diff Parser (Phase 7)

**Public API:**
```python
parse_diff(diff_text) -> ParsedDiff
parse_diff_stat(stat_text) -> list[(file_path, additions, deletions)]

@dataclass DiffLine: type, content, old_line_no, new_line_no
@dataclass DiffHunk: header, old_start, new_start, lines[DiffLine]
@dataclass FileDiff: old_path, new_path, display_path, is_binary, is_new, is_deleted, is_renamed, hunks, additions, deletions
@dataclass ParsedDiff: files, total_additions, total_deletions, summary
```

### 3.21j `utils/git_ops.py` — Git Operations (Phase 7)

**Public API:**
```python
is_repo(project_path) -> bool
init_repo(project_path) -> GitResult
get_head_sha(project_path) -> GitResult
stage_all(project_path) -> GitResult
commit(project_path, message) -> GitResult
diff_against(project_path, sha) -> GitResult
diff_stat_against(project_path, sha) -> GitResult
diff_file_against(project_path, sha, file_path) -> GitResult
checkout_paths(project_path, sha, paths) -> GitResult
log(project_path, count=10) -> GitResult
push(project_path, remote="origin", branch="main") -> GitResult
status(project_path) -> GitResult

@dataclass GitResult: success, stdout, error, sha
```

### 3.21k `utils/config.py` — Config Path Helpers (Phase 7)

**Public API:**
```python
get_config_dir() -> str                         # ~/.config/crabcakes (or $XDG_CONFIG_HOME)
get_config_file() -> str                       # config.json path
get_projects_config_dir() -> str               # ~/.config/crabcakes/projects
get_projects_dir() -> str                      # ~/projects (or $CRABCAKES_PROJECTS_DIR)
get_gateway_url() -> str                        # ws://localhost:18789 (or $CRABCAKES_GATEWAY_URL)
get_identity_dir() -> str                       # ~/.openclaw/identity/

COMMAND_PREFIX = "/"                            # slash command prefix
```

### 3.21l `models/conversation.py` — Conversation Data Models (Agent Runtime Phase 1.1)

**Responsibility:** Dataclasses for agent conversation state. Pure data — no GTK, no network, no LLM calls.

**Public API:**
```python
class MessageRole(str, Enum): SYSTEM = "system" | USER = "user" | ASSISTANT = "assistant" | TOOL_RESULT = "tool"
class ToolCallStatus(str, Enum): PENDING = "pending" | EXECUTING = "executing" | COMPLETED = "completed" | FAILED = "failed"

@dataclass ToolCall: call_id, tool_name, arguments, result, status, started_at, completed_at
@dataclass Message: role, content, tool_calls, tool_call_id, timestamp, tokens_used
@dataclass Conversation: agent_name, project_path, system_prompt, messages, model, fallback_provider, created_at, total_tokens, total_cost, step_count

    def add_user_message(content) -> Message
    def add_assistant_message(content, tool_calls) -> Message
    def add_tool_result(call_id, result) -> Message
    def to_api_messages() -> list[dict]
    def get_token_estimate() -> int
    def _count_char_tokens() -> tuple[int, int]  # shared char counter for estimate + breakdown
    def get_token_breakdown(model_max_tokens) -> dict  # Phase CB-4: tiktoken when available (BUG #5 fix)
        # Phase CB-1 additions: trimmed_this_turn (bool), messages_remaining (int), messages_removed_this_turn (int)
    # Trim/prune/summary lives in agent/context_strategy.DefaultContextStrategy
    # (compact/_summary) — the Conversation trim/summary shims were removed in
    # SPEC-AUDIT-CLEANUP-2 Phase 4 (models→agent layer rule).
```


**Token estimation (Phase CB-4).** `get_token_estimate()` and
`get_token_breakdown()` use `tiktoken.encoding_for_model()` for accurate
token counts (BUG #5 fix). Provider prefixes are stripped from model
names (e.g., `"openai/gpt-4o"` → `"gpt-4o"`). Unknown model names
fall back to the `cl100k_base` encoding. The `chars // 4` heuristic is
the final fallback when `tiktoken` is unavailable.

Requires `tiktoken>=0.7` (MIT-licensed, ~2MB).

**Token estimate caching (Phase CB-5).** `get_token_estimate()` caches its
tiktoken result, keyed on `(len(messages), hash(system_prompt))`. The cache
is invalidated on any message add/remove/trim operation. This prevents the
trim loop from re-encoding the full conversation on every iteration (which
took ~6s per call for a 100K-char system prompt before the cache).

**Rules:** No imports from `ui/`, `agent/`, `gateway/`, `subprocess`.

### 3.21m `agent/runtime.py` — Agent Runtime (Phase 1.3a)

**Responsibility:** Core agent loop — conversation management, LLM API calls, tool execution, streaming SSE responses, cost tracking, conversation persistence.

**Owns:** `AgentConfig`, conversation store, tool loop. Provider adapters, cost functions, SSE helpers, extractors, and converters have been extracted to `agent/llm/` (Phase B1–B6). Tool middleware (enforcement + stuck detection) extracted to `agent/tool_middleware.py` (Phase A1). AuditLog extracted to `agent/audit.py` (Phase 5). Conversation persistence extracted to `agent/persistence.py` (Phase 6). All pure re-export aliases removed (Phase 4 + Phase 8); imports now go directly to the canonical modules.

**Modular extraction (Phase 1 + Phases 4–8 + SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION):** `runtime.py` reduced from 3297 → 2714 lines. Cost re-exports removed (Phase 4). AuditLog extracted to `agent/audit.py` (Phase 5). Persistence extracted to `agent/persistence.py` (Phase 6). Pure re-export aliases removed (Phase 8). LLM dispatch uses `get_provider(caller_key).call()` / `.stream()`. Tool execution routes through `self._tool_chain.run()`. `_PROVIDER_CALLERS` retained for validation taxonomy (values are now direct provider lookups, not bound-method aliases). `_PROVIDER_STREAMERS` and the `_call_*` / `_stream_*_events` aliases **removed** (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.3 — superseded by registry dispatch in Phase B6). Per-turn state machine added: `TurnStatus` enum (5 states), `TurnResult` dataclass, `_terminate_turn()` chokepoint (all terminal paths funnel through one function), `_state_lock` for atomic state transitions. See post-mortems for all phases.

**Turn state machine (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION):** All terminal transitions in `_run_loop` funnel through a single `_terminate_turn(result: TurnResult)` method. The state machine tracks per-turn status: `RUNNING → STREAMING → {COMPLETED | FAILED | CANCELLED}`. State is keyed by `(session_key, turn_token)` tuple (not session_key alone) so cancellation races don't clobber a new turn's state. A dedicated `_state_lock` (separate from `self._lock`) protects the compound read-decide-write operation — the GIL does NOT make this atomic. `_terminate_turn` deduplicates terminal transitions (at most one accepted terminal per turn) and rejects stale-token results (a result whose turn_token doesn't match the session's active token). `_check_and_stop_on_limit` is now a pure predicate returning `tuple[str, str] | None` (was: side-effecting with an undefined `turn_token` NameError). Public accessors `get_turn_state(sk)` and `get_last_turn_result(sk)` provide observability.

**Public API:**
```python
class AgentRuntime:
    def __init__(config, GLib, on_text_delta, on_turn_start, on_tool_call_start, on_tool_call_result,
                 on_tool_call_approval_needed, on_response_complete, on_error, on_token_usage,
                 on_enforcement_status=None)
    def start() / def stop()
    def create_conversation(agent_name, session_key, project_path, model, allowed_tools=None, agent_role="") -> str
    def send_message(session_key, text)         # tool loop: user msg → [trim to model_max] → LLM → [stuck-detection as transient prefix] → tool calls → results → LLM → response
    def cancel(session_key)
    def get_conversation(session_key) -> Conversation | None
    def save_conversation(session_key) -> str    # → <config_dir>/conversations/<session_key>.json
    def load_conversation(session_key) -> bool
    def list_conversations() -> list[(session_key, agent_name)]

def get_valid_callers() -> frozenset[str]     # §caller-validation — single source of truth for ProviderConfig.caller taxonomy
# Returns frozenset of keys in _PROVIDER_CALLERS ({"anthropic", "minimax",
# "openai", "openrouter", "zai"}). Used by settings_handler.add_or_update
# (save-time validation) and test_provider._worker (Test Connection gate).
# Layer rule: utils/* cannot import from agent/*, so utils/providers_store.py
# DUPLICATES this set as a module-level constant — enforced by
# TestValidCallersDuplicationInvariant regression test.

# Turn state machine (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION):
TurnStatus          # Enum: RUNNING, STREAMING, COMPLETED, FAILED, CANCELLED
TurnResult          # Dataclass: status, session_key, turn_token, text, error, metadata

# Public accessors (added in SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION):
rt.get_turn_state(session_key) -> TurnStatus | None      # active turn's status
rt.get_last_turn_result(session_key) -> TurnResult | None # active turn's terminal result

# Removed (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.3):
# _call_openai, _call_minimax, _call_anthropic — use OpenAIProvider/MiniMaxProvider/AnthropicProvider directly
# _stream_openai_events, _stream_minimax_events, _stream_anthropic_events — use Provider.stream() directly
# _PROVIDER_STREAMERS — dead since Phase B6; use _get_provider(caller_key).stream
```

**Tool loop:** Append user message → build API messages → call LLM → if tool calls: execute each tool → append results → call LLM again → if text: append assistant message → fire callbacks → check cost/step limits.

**Enforcement hook (§F):** After each `write_file`/`edit_file` tool execution, the enforcement layer runs verification tiers (syntax, tests, lint). Results are appended to the tool result text and dispatched via `on_enforcement_status` callback.

**Stuck detection (§E):** `_check_stuck()` monitors tool call history for loops (same tool+args 3×, or 8+ writes without verification). Intervention messages are appended to the conversation's tool result with a `⚠️` separator. History is per-session, capped at 20 entries, cleaned up on `cancel()`. Thread-safe via `_tool_history_lock`.

**Providers:** OpenAI (`openai/*`), MiniMax (`minimax/*`), Anthropic (`anthropic/*`), OpenRouter (`openrouter/*`), ZAI (`zai/*`) — selected by explicit `caller` field on `LLMProviderConfig` (persisted in `providers.yaml`); falls back to model-prefix derivation for legacy configs without an explicit caller. See §12 for full resolution details. Tool calls normalized to internal `ToolCall` format regardless of provider.

**Context mode (P10):** `create_conversation()` reads `context_mode` from `ProviderConfig` (default `"auto"`) and passes it to `build_system_prompt()`. The mode is resolved once at conversation creation time via `resolve_context_mode()` and fixed for the session. `TODO: P10.8 — mid-session re-escalation` marker for future turn-count/token-estimate-based mode switching.

**Streaming:** SSE for supported providers. `on_text_delta` fires incrementally. `on_tool_call_start` fires when complete call is received. The provider-assigned tool_call `id` (e.g. `call_function_3679004591_1`) is preserved end-to-end through the streaming path — captured from the first SSE delta in `OpenAIProvider("openai").stream` / `MiniMaxProvider().stream` (from the `content_block_start` event in `AnthropicProvider().stream` for Anthropic), held in the accumulator's first-write-wins slot in `_call_llm_streaming`, and surfaced on the final assembled tool_call so the round-trip `tool_call_id` matches on the next LLM turn. Without this, providers reject the tool result with `status_code=2013: invalid params, tool call result does not follow tool call` (regression fixed by `STREAM-ID-PRES`, see `docs/specs/SPEC-STREAMING-TOOL-CALL-ID-PRESERVATION.md`).

**Cost tracking:** Provider-specific pricing tables. Fires `on_token_usage(session_key, tokens, cost)` after each LLM call. Stops loop if `cost_limit` exceeded.

**Thread safety:** All callbacks dispatched via `GLib.idle_add()`. `_tool_history` protected by dedicated `_tool_history_lock` (separate from `self._lock` to avoid deadlock with `cancel()`).

### 3.21m.1 `agent/tool_middleware.py` — Tool Middleware Chain (Phase A1)

**Responsibility:** Composable middleware that wraps tool execution with cross-cutting concerns (post-write enforcement verification and stuck-loop detection). Replaces inline policy blocks that were in `_run_loop`.

**Owns:** `ToolMiddleware` (Protocol), `ToolContext` (dataclass), `EnforcementMiddleware`, `StuckDetectionMiddleware`, `ToolMiddlewareChain`.

**Architecture:** `agent/` layer — imports only from `agent.tools` and stdlib. No UI, gateway, or `agent.runtime` imports. The chain is constructed in `AgentRuntime.__init__` and invoked via `self._tool_chain.run()` in `_run_loop`.

**Public API:**
```python
class ToolMiddlewareChain:
    def __init__(middlewares: list[ToolMiddleware])
    def run(tool_name, args, ctx: ToolContext, executor: Callable[[], ToolResult]) -> ToolResult

@dataclass ToolContext:
    session_key, project_path, iteration, bypass_approval, audit_log, user_id, enforcement_config, si_enforcement
```

### 3.21m.2 `agent/llm/` — LLM Provider Abstraction Package (Phase B1–B6)

**Responsibility:** Encapsulate LLM provider wire protocols (OpenAI, MiniMax, Anthropic) behind a uniform `call()` + `stream()` interface. Extracted from `agent/runtime.py` to reduce file size and enable per-provider testing.

**Public API:**
```python
get_provider(provider_id: str) -> LLMProvider    # registry lookup
list_providers() -> list[str]                     # sorted provider IDs
LLMProvider — Protocol with call() + stream() + provider_id + response_format
LLMResponse — normalized response dataclass
```

**Sub-modules:**
- `protocol.py` — LLMProvider Protocol (@runtime_checkable) + LLMResponse dataclass
- `registry.py` — provider registry (5 entries: openai, minimax, anthropic, openrouter, zai)
- `openai_provider.py` — OpenAIProvider (handles openai, openrouter, zai; call + stream)
- `minimax_provider.py` — MiniMaxProvider (ChatCompletion v2; call + stream)
- `anthropic_provider.py` — AnthropicProvider (Messages API; call + stream)
- `streaming.py` — SSE helpers (sse_lines, parse_sse_line, parse_sse_delta) + SSL retry infrastructure
- `extractors.py` — response extractors (tool_calls, text_content, usage); takes `response_format: str` parameter
- `convert.py` — Anthropic message/tool format converters
- `cost.py` — cost tables + model_id + cost_for_model

**Architecture:** `agent/` layer — imports from stdlib + `agent.tools` only. No UI or gateway imports. Provider classes import SSE helpers and cost functions from sibling modules within the package.

### 3.21m.3 `agent/callbacks.py` — Agent Runtime Callback Protocols (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION)

**Responsibility:** Typed `Protocol` classes formalizing the callback contract between `agent/runtime.py` and `ui/handlers/agent_runtime_handler.py`. The 10 protocols are the source of truth for callback signatures; the handler's `_on_*` methods satisfy them structurally. No `@runtime_checkable` (protocols are documentation, not runtime enforcement).

**Owns:** 10 Protocol classes (`OnTextDelta`, `OnTurnStart`, `OnToolCallStart`, `OnToolCallResult`, `OnToolCallApprovalNeeded`, `OnResponseComplete`, `OnTokenUsage`, `OnTokenBreakdown`, `OnError`, `OnEnforcementStatus`) + `AgentRuntimeCallbacks` type alias.

**Architecture:** `agent/` layer — imports only `typing`. No imports from `agent.runtime`, `ui/`, `gateway/`, or `models/`. Same pattern as `agent/llm/protocol.py` (the `LLMProvider` Protocol). The keyword `_turn_token: object | None = None` (leading underscore) appears on every protocol, matching production dispatch in `_dispatch()`.

**Public API:**
```python
# All 10 protocols have a single __call__ method with session_key as the
# first positional arg and _turn_token as a keyword-only arg:
class OnTextDelta(Protocol):
    def __call__(self, session_key: str, text: str, *, _turn_token: object | None = None) -> None: ...

class OnTurnStart(Protocol):
    def __call__(self, session_key: str, *, _turn_token: object | None = None) -> None: ...

AgentRuntimeCallbacks = dict[str, Callable | None]  # loose helper type for test fixtures
```

### 3.21n `agent/tools.py` — Tool Definitions + Execution (Phase 1.1)

**Responsibility:** 9 tools for local file/exec/web operations, sandboxed to `project_path`, with PM approval gating for `exec_command`.

**P10 additions:** `file_search` tool (find files by name OR content pattern,
returns grouped previews). Shared `_run_grep()` helper used by both
`search_files` and `file_search` to guarantee identical grep behavior.
`_run_grep` validates `search_root` (non-empty string required). Grep output
paths are normalized (leading `./` stripped) to prevent duplicate entries
when merging filename and content matches.


**Public API:**
```python
@dataclass ToolDefinition: name, description, parameters, requires_approval
@dataclass ToolResult: success, output, error, duration_ms, stdout, stderr, exit_code  # §4.13: separate stdout/stderr/exit_code

def get_all_tools() -> list[ToolDefinition]
def get_tool_definitions_for_api() -> list[dict]    # OpenAI function-calling format
def execute_tool(name, arguments, project_path, allowed_tools=None) -> ToolResult
# When allowed_tools is provided, only tools in the list are executable.
# The API-schema filter (get_tool_definitions_for_api) is advisory;
# this execution gate is the law.
def set_approval_callback(cb) -> None              # cb(session_key, tool_name, args) → bool
```

**Available tools:**

| Tool | Approval | Description |
|------|----------|-------------|
| `read_file` | No | Read file (max 50KB, binary → error) |
| `write_file` | No | Write file (sandboxed to project path) |
| `edit_file` | No | Replace exact text in file (sandboxed, unique match required) |
| `exec_command` | **Yes** | Run shell command (PM must approve; hardcoded blocklist rejects catastrophic calls first) |
| `list_files` | No | List directory contents |
| `search_files` | No | Grep/ripgrep for pattern |
| `file_search` | No | Find files by name OR content (P10 — uses `_find_matching_files` + `_run_grep`) |
| `web_search` | No | Brave Search API |
| `web_fetch` | No | Fetch URL as text |

**Sandbox:** All paths resolved relative to `project_path`. Escape attempt (`realpath` outside `project_path`) rejected with error result.

**Blocklist:** `rm -rf /`, `mkfs`, `dd if=/dev/zero of=/dev/sda` — always denied before approval callback fires.

**Allowed-tools enforcement gate:** `execute_tool` accepts an
`allowed_tools: list[str] | None` parameter. When non-None, only tools in
the list are executable; any other tool returns a denial `ToolResult`
without running the handler. The single caller in `agent/runtime.py`
forwards `conv.allowed_tools`, which is set on conversation creation from
the agent definition's `tools:` field. When `allowed_tools=None` (default),
all registered tools are permitted — preserves back-compat.

The API-schema filter in `get_tool_definitions_for_api(allowed_tools)`
remains as a *prompt-level* optimization. It is no longer the sole
authorization mechanism.

### 3.21o `agent/config.py` — LLM Provider Config (Phase 1.1)

**Public API:**
```python
@dataclass LLMProviderConfig: name, base_url, api_key, default_model, supports_tools, supports_streaming, max_tokens
@dataclass EnforcementConfig: enabled, syntax_check, test_run, lint_check, syntax_timeout_seconds, test_timeout_seconds, lint_timeout_seconds, max_output_chars, skip_patterns
@dataclass AgentConfig: providers, default_provider, default_model, max_tool_iterations, tool_timeout_seconds, auto_save_conversations, cost_limit, step_limit, enforcement, fallback_provider

def load_agent_config() -> AgentConfig      # reads <config_dir>/agent.json; checks chmod >600
def get_api_key(provider_name) -> str | None
```

### 3.21p `agent/context.py` — System Prompt + File Context Builder (Phase 1.2)

**Public API:**
```python
def build_system_prompt(agent_name, project_path, tools, review_mode, agent_role="", model_max_tokens=None, *, context_mode="auto") -> str
def build_file_context(project_path, query=None) -> str    # respects .gitignore, capped ~50K chars; §4.4a prepends .crabcakes/ docs
def build_file_context_with_core_files(project_path, query=None, max_chars=50_000, *, context_mode="preload") -> str  # Phase CB-2 + P10
def build_file_index(project_path, max_entries=200, include_line_counts=True) -> str  # P10: compact file index for JIT/hybrid
def resolve_context_mode(explicit_mode: str, model_max_tokens: int | None) -> str  # P10: resolves "auto" → preload|hybrid|jit
def _find_matching_files(project_path, query, patterns, max_files=20) -> list[str]  # P10: filename search for file_search tool
def _read_crabcakes_docs(project_path) -> str               # §4.4a — always include project docs in context
def load_custom_system_prompt(project_path) -> str | None  # .crabcakes/agent-system-prompt.md → AGENTS.md → None
```

**Context discovery (P10).** `resolve_context_mode()` resolves `"auto"` to one
of `"preload"`, `"hybrid"`, or `"jit"` based on `model_max_tokens`:
- `≥ 500,000` → `"preload"` (large window — full context)
- `≤ 32,000` → `"jit"` (small window — index only)
- Otherwise → `"hybrid"` (core files + index)
Negative/zero `model_max_tokens` defaults to `"hybrid"`. Input is normalized
via `validate_provider_context_mode()` (case-insensitive, whitespace stripped).

`build_file_index()` walks the project tree and produces a compact, grouped
file listing (extension-grouped, size + optional line count). Files >1MB skip
line counting for performance. Capped at `max_entries` with a directory summary
for large projects.

`build_file_context_with_core_files()` gains a `context_mode` parameter:
- `"preload"` — full file context + core files (existing behavior)
- `"jit"` — file index only, no core files
- `"hybrid"` — core files + file index

Oversized core files (>50KB) get a size placeholder with `read_file()` hint
instead of being silently dropped.

**Core files (Phase CB-2).** `build_file_context_with_core_files()` places the
following files at the END of the file context, so they are the last to be
truncated when the system prompt is over budget:
- `README.md`
- `AGENTS.md`
- `CONVENTIONS.md`
- `ARCHITECTURE.md`

### §4.4b System Prompt Budget (Phase CB-2)

The system prompt is budgeted to 15% of the model's context window, with a
16K-token (64K-char) hard cap fallback. This caps the file-context section
of the system prompt at `int(model_max_tokens * 0.15) * 4` chars (or the
hard cap when `model_max_tokens` is unknown/zero).

When the file context exceeds the budget, it is truncated from the end.
Core files (README, AGENTS, CONVENTIONS, ARCHITECTURE) are at the end of
the file context, so they are the last to be dropped.

The budget is enforced by `utils/prompt_loader.py:_apply_system_prompt_budget()`,
called from `compose_system_prompt()` when `model_max_tokens` is provided.
The runtime at `agent/runtime.py:create_conversation` (CB-2 wiring) passes
the default provider's `max_tokens` to `build_system_prompt()`.

**Backward compatibility:** `compose_system_prompt()` and `build_system_prompt()`
get a new optional keyword `model_max_tokens: int | None = None`. When `None`,
no budget is enforced. All existing call sites continue to work unchanged.

**Context mode (P10):** `compose_system_prompt()` and `build_system_prompt()`
gain a `context_mode` keyword-only parameter (default `"auto"`). The prompt
loader lazy-imports `resolve_context_mode` from `agent.context` and resolves
the effective mode before building the file context block. When `context_mode`
is `"jit"` or `"hybrid"`, `build_file_context_with_core_files()` produces a
compact file index (via `build_file_index()`) instead of full file contents,
significantly reducing prompt size for large projects.

### 3.21p.5 `agent/context_strategy.py` — Pluggable Context Compaction Strategy (Phase 4–9)

**Responsibility:** Hosts the pluggable context-compaction algorithms for the
agent runtime. Implements `ContextStrategy` (a `Protocol`) and the concrete
`DefaultContextStrategy` that performs the 3-layer compaction cycle: Layer 1
(lossless tool-output pruning), Layer 2 (message trimming with summary
injection), and Layer 3 (manual fallback). `models/conversation.py`
(§3.21l) stays pure data — it retains thin delegation shims that forward to
`DefaultContextStrategy` so existing callers and tests work unchanged.

**Spec reference:** `docs/specs/SPEC-CONTEXT-MANAGEMENT-ROADMAP.md` §0.

**Public API:**
```python
@dataclass
class CompactionEvent:        # telemetry record for one compaction cycle
    turn: int                  # tool-loop iteration (1-indexed)
    trigger: str               # what caused compaction
    layer: int                 # 1=prune, 2=trim, 3=manual
    messages_before: int
    messages_after: int
    messages_removed: int
    tokens_before: int
    tokens_after: int
    tokens_freed: int
    summary_tokens_injected: int
    soft_ceiling: int
    hard_ceiling: int
    provider: str
    model: str

class ContextStrategy(Protocol):
    def compact(self, conv: Conversation, token_budget: int) -> None: ...
    @property
    def last_result(self) -> CompactionEvent | None: ...

class DefaultContextStrategy:
    def compact(self, conv, token_budget, *, keep_first=2, protect_is_summary=True) -> None
    def prune_tool_outputs(self, conv, target_tokens, protect_turns=2) -> int
    # Private: _find_split_index, _fit_summary, _select_prune_candidate, _summary
```

**Compaction flow:**
1. **Layer 1 — prune_tool_outputs:** Replaces old `TOOL_RESULT` message
   content with `[pruned]` stubs (lossless structure, lossy content).
   Targets messages older than `protect_turns` turns.
2. **Layer 2 — trim:** If still over budget, finds a split index that
   preserves the system prompt + first `keep_first` messages, trims the
   middle, and injects a summary message.
3. **Layer 3 — manual fallback:** If the strategy cannot fit within budget,
   a final brute-force trim removes oldest messages.

**Telemetry:** Each `compact()` call produces a `CompactionEvent` stored on
`self._last_result`. The runtime reads this and appends it to
`_compaction_events` history for debugging and dashboards.

---

### 3.21q.5 `agent/kb_lookup.py` — Knowledge-Base Lookup (Auxilium Tier 1)

**Responsibility:** Semantic search over the project's `knowledge/*.md` files. Embeds the user's question with a local Sentence-Transformers model, computes cosine similarity against pre-built chunk embeddings, and returns the top-K most relevant chunks. Powers the Auxilium help agent's "KB-first" answer engine.

**Public API:**
```python
@dataclass
class KBChunk:
    id: str                # e.g. "setup.md#0.0"
    source: str            # e.g. "knowledge/setup.md"
    section: str           # e.g. "Installation"
    text: str              # chunk text
    score: float           # cosine similarity, 0..1

def kb_lookup(question: str, top_k: int = 3, min_score: float = 0.3,
              model_name: str = "BAAI/bge-small-en-v1.5") -> list[KBChunk]
def is_index_available() -> bool   # True if knowledge/.index/{chunks.json, embeddings.npy} exist
def reset_cache() -> None          # clears module-level state (for tests)
```

**Architecture:**
- Pure Python — no GTK, no network at import time.
- Lazy-loads the Sentence-Transformers model and index on first call. Module-level singleton state cached across calls.
- Default model: `BAAI/bge-small-en-v1.5` (130MB, MIT-licensed, 384-dim, runs on CPU).
- Index format: `knowledge/.index/chunks.json` (list of `{id, source, section, text}`) + `knowledge/.index/embeddings.npy` (float32, shape `(N, 384)`, L2-normalized).
- Index is built offline by `scripts/rebuild_kb_index.py` and committed to the repo.

**Fail-soft behavior:** Returns `[]` on missing index, missing model, no confident match, or any internal error. Logs at DEBUG/WARNING. The agent must treat empty list as "I don't have info on that" — never as a crash.

**Integration with AgentRuntime (KB Provider — Phases 1-5):** The KB lookup is wired into the runtime via the KB HTTP server (`agent/kb_server.py`) which wraps `kb_lookup()` in an OpenAI-compatible API. The server is registered as a `local-kb` provider in `providers.yaml`. When the primary provider returns `[KB_OUT_OF_SCOPE]`, the runtime fallback chain retries with the per-agent `conv.fallback_provider` (from `SpecialAgentDef`, wired through `create_conversation()`). Global `AgentConfig.fallback_provider` serves as a default when no per-agent value is set. If the fallback fires, `kb_lookup()` is called directly by the runtime to pre-fetch KB chunks, which are injected as context into the **fallback** LLM messages. Separately, when `conv.agent_role == "helper"`, the runtime also runs `kb_lookup()` on every user message and injects the chunks into the **primary** LLM call (Auxilium Tier 2 KB synthesis). Both paths use the same LLM-side instructions in `prompts/system/auxilium.md`. See §3.21q.5b for the primary-call path.

**No `ui/` imports.** Lives in `agent/` per §2.

### 3.21q.5b `agent/runtime.py:_inject_kb_context` — KB Synthesis for Auxilium (Tier 2)

**Responsibility:** When `conv.agent_role == "helper"`, `AgentRuntime._run_loop()` runs `kb_lookup()` on every user message and injects the resulting chunks into the primary LLM call. This is **separate from the KB fallback chain** (which fires only when the primary returns `KB_OUT_OF_SCOPE`). The LLM synthesizes a conversational answer from the chunks per `prompts/system/auxilium.md` Phase 2 instructions.

**Public API:**
```python
class AgentRuntime:
    def _inject_kb_context(self, messages: list[dict], kb_context: str, text: str) -> list[dict]:
        """Prepend KB context to the most recent user message.

        Returns a new list — does not mutate the input. If no user message
        is found in the list, returns the input unchanged (defensive).
        """
```

**Architecture:**
- Gate: `if conv.agent_role == "helper":` in `_run_loop` (replaces the previous `if conv.fallback_provider:` gate). Non-auxilium agents (`agent_role != "helper"`) skip KB synthesis entirely.
- Failure mode: `kb_lookup()` is fail-soft by design (`agent/kb_lookup.py`). The runtime wraps it in `try/except Exception: pass` — if the lookup raises, `kb_context` stays `None` and the primary LLM call proceeds without KB context.
- Empty result: `kb_lookup()` returns `[]` for low-confidence or missing-index cases. The runtime leaves `kb_context = None` and the primary call proceeds without injection. The LLM answers from general knowledge (or says "I don't have specific docs on this" per `prompts/system/auxilium.md`).
- Multi-turn: `kb_lookup()` runs **fresh on every message** with the current user message as the query. Follow-up questions ("and on Windows?") re-query the KB with the new query, not a cached result.
- Conversation dataclass: `Conversation` has a new `agent_role: str = ""` field. The `agent_runtime_handler.py:send_to_special_agent()` path passes `agent_role=agent_def.role` to `create_conversation()`, which propagates it to the `Conversation(...)` constructor. The `agent_role` value also round-trips through `_save_conversation_to_disk` and `_load_conversation_from_disk` so KB synthesis continues after a restart.
- KB fallback chain unchanged: lines ~1223-1250 in `agent/runtime.py` retain the existing `KB_OUT_OF_SCOPE && fallback_provider && !_fallback_attempted` gate. The two paths (primary synthesis + fallback synthesis) are independent.

**No `ui/` imports.** The synthesis logic is in `agent/runtime.py` per §2.

**Tests:** `tests/test_auxilium_tier2.py` (10 tests, 5 classes):
- `TestConversationAgentRole` — field exists, defaults to `""`
- `TestKBLookupFiresForAuxilium` — gate behavior (helper, non-helper, empty role, every-message)
- `TestKBContextInjection` — chunks prepended to last user; absent when lookup empty; exception does not break the call
- `TestMultiTurnSynthesis` — fresh `kb_lookup()` on follow-up
- `TestAgentRuntimeHandlerPassesRole` — `send_to_special_agent` passes `agent_role=agent_def.role`

### 3.21q.5a `agent/kb_server.py` — KB HTTP Server (KB Provider Phase 1)

**Responsibility:** Lightweight HTTP server on `localhost:18790` that wraps `kb_lookup()` and responds to `/v1/chat/completions` requests. Returns responses in OpenAI Chat Completions format so the runtime's OpenAI-compatible provider call can parse them without modification. When no KB chunks match, returns the `[KB_OUT_OF_SCOPE]` sentinel string.

**Public API:**
```python
KB_SERVER_PORT = 18790
KB_OUT_OF_SCOPE = "[KB_OUT_OF_SCOPE]"

def start_kb_server(port: int = KB_SERVER_PORT) -> threading.Thread | None
def stop_kb_server() -> None
def is_kb_server_running() -> bool
```

**Architecture:**
- Pure Python stdlib (`http.server`) — no external dependencies.
- Runs on daemon thread, started explicitly via `start_kb_server()` (not on import).
- Server lifecycle managed by `AgentRuntimeHandler`: starts in `__init__` if KB index available, stops in `stop_all()`.
- Registered as `local-kb` provider in `providers.yaml` via `ensure_kb_provider()` in `utils/providers_store.py`. Also patches the helper agent's `llm_name` to `local-kb` if empty (fresh-install auto-configuration). Called from `AgentRuntimeHandler.__init__` at every app start.
- KB lookup params: `top_k=5`, `min_score=0.35` (higher than kb_lookup default to reduce noise).

**Fail-soft behavior:** If `kb_lookup()` raises, returns `[KB_OUT_OF_SCOPE]` (graceful degradation). If index is unavailable, `start_kb_server()` returns `None` (server does not start).

**Synthesis layer (KB Enhancement):** Before returning formatted KB chunks, `do_POST` calls `_try_synthesize(question, chunks)` which POSTs to a free, no-auth Llama-3.2-3B endpoint (`_SYNTHESIS_ENDPOINT_URL`, default `https://devtoolbox-api.devtoolbox-api.workers.dev/ai/generate`) with a 1.5s timeout. If the synthesis returns a non-empty string, the response content is the synthesized answer. If synthesis fails for any reason (timeout, network error, HTTP error, body-level error, empty response, error-shaped response), the server falls back to the raw formatted chunks. The toggle `CRABCAKES_KB_SYNTHESIS=0` (env var) disables synthesis entirely, restoring the pre-enhancement behavior. Synthesis is opt-in by default; the response shape is unchanged from the OpenAI Chat Completions contract — only the `content` field differs in style.

**Synthesis-layer public API additions:**
```python
_SYNTHESIS_ENDPOINT_DEFAULT: str  # constant, overridden via CRABCAKES_KB_SYNTHESIS_URL
_SYNTHESIS_TIMEOUT_SECONDS: float  # 1.5
def _get_synthesis_url() -> str  # reads env var at call time
def _synthesis_enabled() -> bool  # reads CRABCAKES_KB_SYNTHESIS env var
def _try_synthesize(question: str, chunks: list, formatted_chunks: str | None = None) -> str | None
```

### 3.21q.6 `ui/handlers/auxilium_wizard_handler.py` — Auxilium First-Run Wizard Handler (Tier 1, D7)

**Responsibility:** Business logic for the Auxilium first-run wizard. Owns the install check (Python + GTK4 + websockets detection), the gateway WebSocket probe (3-second timeout, background thread), and the provider config write (via `utils.providers_store.save_providers()`). Does NOT touch GTK; the view polls `get_state()` for state changes.

**Public API:**
```python
class WizardStep(str, Enum):
    INSTALL_CHECK = "install_check"
    GATEWAY_CHECK = "gateway_check"
    PROVIDER_PICK = "provider_pick"
    WRITING_CONFIG = "writing_config"
    DONE = "done"

@dataclass
class WizardState:
    step: WizardStep
    install_check: dict  # {ok, platform, python, gtk4, websockets, missing, warnings}
    gateway_check: dict  # {ok, url, error}
    provider_pick: dict  # {choice, provider, model, api_key}

def is_auxilium_wizard_needed(config_dir: Path) -> bool
    # True if providers.yaml is missing or has no providers.

class AuxiliumWizardHandler:
    def __init__(config_dir, on_complete, on_error, on_step_changed=None)
    def get_state() -> WizardState               # deep copy — caller cannot mutate internal state
    def start() -> None                          # synchronous install check
    def advance_to_gateway() -> None             # spawns daemon thread to probe WebSocket
    def advance_to_provider() -> None            # sync; no I/O
    def set_provider_choice(choice, provider, model, api_key) -> None
                                                # writes providers.yaml; fires on_complete or on_error
```

**State machine:** 5 states (INSTALL_CHECK → GATEWAY_CHECK → PROVIDER_PICK → WRITING_CONFIG → DONE). Linear flow; no backward transitions. The `on_step_changed` callback fires on each transition.

**Architectural constraints:**
- No `ui/` imports. No `gateway/` imports. No `subprocess`. No GTK at import time.
- Threading only for the gateway probe; all other methods are synchronous (file I/O + dict ops).
- The handler does not call back into the view directly — the view polls `get_state()` on a `GLib.timeout_add` loop.
- `get_state()` returns a deep copy (`copy.deepcopy`) so view mutations cannot corrupt the handler.
- `set_provider_choice` validates the choice (one of `"openrouter_free"`, `"ollama"`, `"bring_your_own"`), normalizes Ollama's empty key to `"ollama"`, and writes `providers.yaml` atomically via `utils.providers_store.save_providers()`.
- On error, `set_provider_choice` fires `on_error(message)` and stays on `PROVIDER_PICK` so the user can retry.

**Provider config writes:** For `"openrouter_free"`, builds a `ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1", default_model="openrouter/free", api_key=user-supplied)`. For `"ollama"`, `ProviderConfig(name="ollama", base_url="http://localhost:11434/v1", default_model="llama3.2:7b", api_key="ollama")`. For `"bring_your_own"`, the `provider` argument drives the `name` field and `base_url` is synthesized from a lookup table (OpenAI, Anthropic, Google, MiniMax, ZAI).

### 3.21q.7 `ui/views/auxilium_wizard.py` — Auxilium First-Run Wizard View (Tier 1, D7)

**Responsibility:** GTK4 view widget for the Auxilium wizard. Renders 3 step frames (install check, gateway check, provider picker) in a `Gtk.Stack`, dispatches user actions to the handler, polls the handler for gateway probe completion. Embeds in the Auxilium chat tab (replaces the welcome bubble) when the user has no provider configured.

**Public API:**
```python
class AuxiliumWizard(Gtk.Box):
    def __init__(
        handler,                                # AuxiliumWizardHandler
        on_install_check_complete,              # fires on Continue click in install frame
        on_gateway_check_complete,              # fires on Continue click in gateway frame
        on_provider_selected,                   # fires on Finish click in provider frame
    )
    @property
    def current_step: str                       # 'install_check' | 'gateway_check' | 'provider_pick'

    def cleanup() -> None                       # removes GLib poll timer; call before destroy
```

**Layout:**
- Vertical `Gtk.Box` with 3 zones: step indicator (3 dots, top), `Gtk.Stack` with 3 named pages, button bar (Back + Continue, bottom).
- Stack page names match `WizardStep` values: `install_check`, `gateway_check`, `provider_pick`.
- Provider frame uses 3 `Gtk.CheckButton` radio buttons (grouped via `set_group()`) for the 3 choices, with a conditional `Gtk.Entry` for the API key (hidden for Ollama) and a `Gtk.DropDown` for the bring-your-own-key provider list.

**Polling model:** When the gateway frame is shown, `GLib.timeout_add(250, self._poll_gateway)` polls `handler.get_state().gateway_check` until the probe completes. The poll function returns `False` to stop the timer once a result is set.

**Architectural constraints:**
- No business logic in the view. No `sys.platform` checks, no `importlib.util.find_spec`, no WebSocket calls — all of that is in the handler.
- No imports of other `ui/views/*` or `ui/handlers/*` modules (except the handler received in `__init__`).
- No direct manipulation of `agent_runtime_handler` or any global state.
- The view must call `cleanup()` before destruction to remove the GLib timer source and avoid leaks.

**CSS classes** (defined in `ui/styles.py`): `auxilium-wizard` (root), `auxilium-wizard-frame` (each frame), `auxilium-wizard-step-dot` / `-active` / `-done` (step indicator dots), `auxilium-wizard-title` (frame titles).

### 3.21q `agent/special_agents.py` — Special Agent Definitions (Phase 1.4 → User-Defined Agents)

**Responsibility:** Agent definition registry — loads agent definitions from `~/.config/crabcakes/agents/*.yaml` (or `.json`). Built-in defaults (Coder, Debugger, Crabcakes) are seeded from `prompts/default_agents/` on first launch. New agents are created via the Agent Builder UI.

**Public API:**
```python
@dataclass SpecialAgentDef:
    conv_id_prefix, display_name, role, emoji, color, tools, can_write,
    llm_name: str | None,                 # per-agent provider card name (None → global default)
    fallback_provider: str | None,        # KB fallback provider (e.g. "openrouter")
    # fallback_model removed in 2026-06-15 — see SPEC-AGENT-FALLBACK-MODEL-DROPDOWN-REMOVAL.md
    api_key: str | None,                  # per-agent API key override
    app_title: str | None,                # per-agent app title override
    self_improvement: dict,
    mcp_servers: list[str],
    auto_open: bool = False,              # open tab on every app launch
    auto_add_to_projects: bool = False,   # auto-add to every new project
    def get_self_improvement_config() -> dict

def get_special_agents() -> list[SpecialAgentDef]
def get_special_agent(prefix) -> SpecialAgentDef | None
def get_auto_open_agents() -> list[SpecialAgentDef]      # agents to auto-open on launch
def get_project_onboarding_agents() -> list[SpecialAgentDef]  # auto-add to new projects
def reload_registry() -> None   # force reload after create/edit/delete
```

**Lazy loading:** Registry loads from config files on first access. `reload_registry()` clears and reloads.

**Self-improvement:** Each agent carries `self_improvement` toggles (bug_journal, project_rules, enforcement, structured_feedback, dream_consolidation). Defaults from `utils/agent_defs.get_default_si_config()`. Override via YAML.

**Auto-open agents:** Agents with `auto_open=True` (currently only Crabcakes 🦀) get a chat tab created automatically on every app launch. See `ui/window.py` Phase 4.

**Project onboarding agents:** Agents with `auto_add_to_projects=True` are automatically added to every new project's team. See `ui/handlers/project_handler.py` Phase 5. No current default agent opts into this; Supervisor is deliberately `auto_add_to_projects=False` so the user manually adds it from the Agents tab.

**SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS — Supervisor role:** Supervisor is the built-in project onboarding/orchestration agent. It is **not** auto-added to projects (`auto_add_to_projects: false`); the user adds it manually after project creation. The onboarding-template gate in `utils/prompt_loader.py` is controlled by `agent_role == "supervisor"` plus `is_project_onboarded(project_path) == False` — it does **not** use `get_project_onboarding_agents()` or the old Coder condition. This separation ensures that `auto_add_to_projects` (which controls team membership) and the prompt gate (which controls which agent receives the onboarding interview template) cannot contradict each other. See `docs/specs/SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS.md`.

**Default agents:**
- **Coder:** Full tool set, `can_write=True`, all SI layers on
- **Debugger:** Read-only tools, `can_write=False`, context-only SI
- **Supervisor:** Onboarding/orchestration agent, write-capable tools, `auto_add_to_projects=False`, `auto_open=False`. Role prompt at `prompts/system/supervisor.md`. Receives the separate `prompts/system/project-onboarding.md` template when the project is not yet onboarded.

**Per-agent model:** `llm_name` field specifies the provider card name for this agent (None → global default). `fallback_provider` specifies the KB fallback — when the KB returns `[KB_OUT_OF_SCOPE]`, the runtime retries with this provider. The model is derived from the selected provider card's `default_model` (same derivation as the primary path in `AgentRuntimeHandler._resolve_agent_model()`). Wired through `create_conversation()` → `Conversation` → runtime fallback chain. See `docs/specs/SPEC-AGENT-FALLBACK-MODEL-DROPDOWN-REMOVAL.md`.

**Default agents:**
- **Coder:** Full tool set, `can_write=True`, all SI layers on
- **Debugger:** Read-only tools, `can_write=False`, context-only SI

### 3.21r `utils/agent_defs.py` — Agent Definition I/O (User-Defined Agents)

**Responsibility:** Load, validate, save, and list agent definition files from `~/.config/crabcakes/agents/`. Pure Python — no GTK, no network. Follows the `utils/projects.py` pattern.

**Public API:**
```python
def load_agent_defs() -> list[dict]             # scan agents dir, seed defaults if empty
def load_agent_def(name: str) -> dict | None    # load by display name
def load_agent_def_by_role(role: str) -> dict | None  # load by role identifier
def save_agent_def(agent_def: dict) -> str      # write to YAML (or JSON fallback)
def delete_agent_def(name: str) -> bool         # remove definition file
def validate_agent_def(agent_def: dict) -> list[str]  # check required fields, tools, prompts
def get_available_tools() -> list[dict]         # [{name, description}] from agent/tools.py
def get_available_prompts() -> list[dict]       # [{name, filepath}] from prompts/ directory
def get_available_providers() -> list[dict]     # [{name, base_url, default_model}] from agent.json
def get_default_si_config(can_write: bool) -> dict  # canonical SI defaults — single source of truth
```

**Default seeding (SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS §2.4):** `_seed_defaults()` copies each built-in YAML/JSON file from `prompts/default_agents/` individually — it checks each built-in file independently and copies only when the same destination filename is absent in the user agents directory. This is a per-file missing-copy, not the old all-or-nothing directory guard. Existing user files are never overwritten, even if the built-in changes. This ensures a new default like `supervisor.yaml` reaches existing installations that already have unrelated agent files.

**Validation:** Checks required fields (name, prompts, tools, provider), verifies tool names against `agent/tools.py`, prompt file existence, and provider availability.

### 3.21s `ui/handlers/agent_builder_handler.py` — Agent Builder Logic (User-Defined Agents)

**Responsibility:** Form logic for the Create/Edit Agent flow. No GTK imports. Delegates I/O to `utils/agent_defs.py`.

**Public API:**
```python
class AgentBuilderHandler:
    def __init__(*, on_agent_saved: Callable, on_agent_deleted: Callable)
    def create_new() -> dict          # blank template with defaults
    def load_for_edit(name) -> dict | None
    def save(agent_def) -> (bool, list[str])  # validate + persist
    def delete(name) -> bool
    def get_tool_options() -> list[dict]
    def get_prompt_options() -> list[dict]
    def get_provider_options() -> list[dict]
```

### 3.21t `ui/views/agent_builder.py` — Agent Builder Dialog (User-Defined Agents)

**Responsibility:** GTK4 modal dialog for creating and editing agents. Pure view — receives data from `AgentBuilderHandler`, emits user actions via callbacks.

**Layout:** Name, Emoji, Role, **Provider dropdown (populated from `handler.get_provider_options()` at construction)**, Prompts multi-select, Tools checkboxes with presets (Full Access / Read Only / Custom).

**Simplifications (Phase 4 of SPEC-AGENT-BUILDER-PROVIDER-DROPDOWN):**
- **No Model dropdown** — the agent's model is resolved at runtime from `providers.yaml` using the provider's `default_model`. The runtime in `agent/runtime.py` handles this.
- **No Manual entry mode** — the user adds providers only via the Settings dialog.
- **No API key field** — API keys live in `providers.yaml`, never in agent definitions.

**Save button enables when:** name (non-empty) AND prompts (≥1 selected) AND tools (≥1 selected) AND provider (selected in dropdown).

**`get_values()` returns:** `{"name", "emoji", "role", "prompts", "tools", "provider", "model": "", "mcp_servers", "self_improvement"}`. The `model` field is always empty string for new agents; the runtime resolves it from the provider's `default_model`.

**Public API:**
```python
class AgentBuilderDialog:
    def __init__(parent, *, handler, agent_def=None, on_save=None, on_cancel=None)
    def get_values() -> dict
    def set_provider_options(providers: list[ProviderConfig]) -> None
    def show() -> None
    def close() -> None
    def show_errors(errors: list[str]) -> None
```

**Live updates:** When providers change in Settings while the dialog is open, the wiring (`ui/wiring.py`) calls `set_provider_options()` on this dialog. The dropdown rebuilds. The handler resolves the new model from `providers.yaml` at save time, not at dialog open time.

### 3.21u.a `ui/wiring.py` — SettingsHandler Callback Wiring (Phase 1 of SPEC-AGENT-BUILDER-PROVIDER-DROPDOWN)

**Responsibility:** Wire the `SettingsHandler`'s `on_status_changed` and `on_providers_changed` callbacks to the toolbar and dialogs. Pure composition — no business logic, no GTK widget creation.

**Owns:** None — this is a stateless wiring function. The handler is owned by the window; the toolbar is owned by the window; the dialogs are owned by the window.

**Public API:**
```python
def wire_settings_handler(
    handler: SettingsHandler,
    toolbar,
    *,
    settings_dialog_factory: Callable[[], Any] | None = None,
    agent_builder_factory: Callable[[], Any] | None = None,
) -> SettingsHandler
```

**Idempotency:** The function is idempotent — calling it twice on the same handler is a no-op (uses a `_wired` flag on the handler). The composition root (`ui/window.py`) calls it exactly once during `_build()`.

**Factories:** Both `settings_dialog_factory` and `agent_builder_factory` are LAZY factories that return a dialog or `None` if the dialog is not open. This is critical — the dialogs may not exist when the wiring is set up (e.g., the agent builder is only created when the user clicks the `+ Agent` button). The factory is called only when `on_providers_changed` fires.

**Behavior:**
- Initial call: `toolbar.set_settings_status(has_any_verified_provider(load_providers()))` is invoked (wrapped in try/except so a toolbar failure doesn't break the wiring).
- On `on_status_changed`: forwards to `toolbar.set_settings_status(has_any_verified_provider(providers))`.
- On `on_providers_changed`: calls both factories, forwards the providers list to whichever returned non-None.

**Architecture compliance:**
- Composition root pattern: the wiring is a function, not a class. It has no state.
- Lazy factories: dialogs are constructed on demand, not at wiring time. This matches the "dialogs are not widgets, they're ephemeral UI" principle in §3.6.
- Idempotency: makes the wiring safe to call from tests (multiple wire_settings_handler calls in a test do not produce double-callbacks).

**LOW-7 env-var helpers:** The module also contains the active-project path
wiring for the image viewer:
```python
ACTIVE_PROJECT_ENV = "CRABCAKES_ACTIVE_PROJECT_PATH"

def set_active_project_path(project_path: str) -> None   # publish for chat_bubble.py
def clear_active_project_path() -> None                   # clear on project close
```
These set/clear a process-global env var that `chat_bubble.py`'s
`_is_path_in_allowed_roots()` reads to determine the active project root.
Without this wiring, the image viewer only ever has the home + `/tmp`
fallback roots.

### 3.21u.a.1 `utils/env_security.py` — Subprocess Environment Scrubbing (MED-2 / CRIT-2)

**Responsibility:** Provides a minimal, allowlisted environment dict for
subprocess execution. Strips API keys, gateway tokens, and other sensitive
env vars so they don't leak to shell tools or post-write enforcement
processes.

**Public API:**
```python
ALLOWED_SUBPROCESS_ENV_VARS: frozenset[str]   # PATH, HOME, LANG, LC_ALL, LANGUAGES, TZ, TMPDIR, PWD

def get_scrubbed_env() -> dict[str, str]       # filter os.environ to allowlist only
```

**Architecture:** Pure utility — no GTK, no network, stdlib only.

### 3.21u `agent/enforcement.py` — Post-Write Verification (Phase 3)

**Responsibility:** Run automatic verification checks after every file write (write_file / edit_file). Three tiers: syntax guard, test runner, lint check. Pure logic — no UI imports, no GTK.

**Owns:** EnforcementCheck, EnforcementResult, TestConfig, SYNTAX_CHECKERS map, DEFAULT_SKIP_PATTERNS, tier detection logic, venv detection, test config loading.

**Public API:**
```python
@dataclass EnforcementCheck: tier, tool, file, passed, detail, output, duration_ms
@dataclass EnforcementResult: checks, appended_message
@dataclass TestConfig: command, full_suite_command, test_dir, naming_pattern, venv_path, run_full_suite, timeout_seconds, extra_args
    from_dict(data) -> TestConfig

def check(tool_name, tool_args, tool_result, project_path, config) -> EnforcementResult
```

**Internal functions:**
```python
def _detect_venv_prefix(project_path, venv_path) -> str   # POSIX "." activation prefix or ""
def _load_test_config(project_path) -> TestConfig | None   # TTL-cached loader from .crabcakes/enforcement.json test section
def _find_related_test(file_path, project_path, test_dir, naming_pattern) -> str | None
def _check_tests(file_path, project_path, config, syntax_passed) -> EnforcementCheck | None
```

**Tiers:**
1. Syntax guard (`_check_syntax`): py_compile, node --check, bash -n, etc. Per-extension mapping.
2. Test runner (`_check_tests`): detect framework (pytest, jest, make test), find related test file, run it. Skipped if syntax fails. Uses per-project `TestConfig` for custom command templates, venv activation (`_detect_venv_prefix`), configurable test directory and naming patterns, and timeout overrides.
3. Lint check (`_check_lint`): detect linter (ruff, mypy, eslint), run on changed file. Skipped if syntax fails.

**Per-project override (§F):** `.crabcakes/enforcement.json` overrides global enforcement config. Two caches:
- `_ENFORCEMENT_CONFIG_CACHE` — tier toggles (syntax_check, test_run, lint_check, skip_patterns)
- `_TEST_CONFIG_CACHE` — per-project test configuration (command, venv, naming, timeout)
Both use 30-second TTL. Applied BEFORE all tier checks so `syntax_check: false` actually skips syntax.

**Test config schema (`.crabcakes/enforcement.json` → `test` section):**
```json
"test": {
  "command": ". .venv/bin/activate && python3 -m pytest {test_file} -v --tb=short",
  "full_suite_command": ". .venv/bin/activate && python3 -m pytest tests/ -v --tb=short",
  "test_dir": "tests",
  "naming_pattern": "test_{module}.py",
  "venv_path": ".venv",
  "run_full_suite": false,
  "timeout_seconds": 30,
  "extra_args": "-x -q"
}
```
**Note:** venv activation uses POSIX `.` command (not `source`) with `shlex.quote()` on the activate path for `/bin/sh` compatibility and safe handling of spaces in venv paths.

**Configuration:** `EnforcementConfig` on `AgentConfig` — enabled/syntax_check/test_run/lint_check toggles, timeouts, skip patterns.

### 3.21v `ui/handlers/agent_runtime_handler.py` — Agent Runtime UI Bridge (Phase 1.4)

**Responsibility:** Bridge between CrabCakes UI and `AgentRuntime`. Creates conversations, routes messages, renders streamed responses in chat tabs.


**Public API:**
```python
class AgentRuntimeHandler:
    def __init__(GLib, main_content, chat_handler, project_handler)
    def start() / def stop()
    def is_running() -> bool
    def send_to_special_agent(session_key, text)              # routes through AgentRuntime, not gateway
    def send_message(session_key, text)
    def cancel(session_key)
    def approve_exec(session_key, tool_name, args, approved)  # PM Allow/Deny callback
    def on_project_opened(project_name, project_path)        # bind special agent conversations
    def on_project_closed(project_name)
    def restore_conversations()                                # reload saved from disk on startup
    def get_special_agents() -> dict[str, str]                # {session_key: display_name} for routing
    def get_special_agent_def(session_key) -> SpecialAgentDef | None
```

**Callback wiring:** All callbacks dispatch to GTK via `GLib.idle_add()`. `on_enforcement_status` is wired into per-agent runtimes for observability logging. `_on_tool_call_approval_needed` currently logs the approval request — the Allow/Deny card UI is not yet wired to a PM-clickable action.

**Special agent routing:** ChatHandler routes special agents through `send_to_special_agent()` (both solo DM and group broadcast paths). Gateway agents go through `gw.send_message()`. This ensures local AgentRuntime agents never hit the gateway.

### 3.21w `utils/mcp_config.py` — MCP Server Configuration (MCP Phase A)

**Responsibility:** Pure Python — loads MCP server configurations from YAML and JSON files. Validates transport type, command, args, environment variables, and enabled flag. No GTK, no network at import time.

**Schema (MCPServerConfig dataclass):**
```python
@dataclass
class MCPServerConfig:
    name: str
    transport: str        # "stdio" (v1), "http" (deferred to v2)
    command: str          # e.g. "npx" or "uvx" or system binary
    args: list[str]       # e.g. ["-y", "@mcp/server-memory"]
    env: dict[str, str]   # env vars to prepend
    enabled: bool = True
```

**Config location (actual code):**
- `get_mcp_servers_path()` returns `~/.config/crabcakes/mcp-servers.json` (exact path: see `utils/mcp_config.py:87`)
- `os.path.expanduser("~")/.config/crabcakes/mcp-servers.json`

**Rationale:** Single config file keeps MCP registration explicit and user-scoped. Projects can extend via `mcp_servers` field in agent definitions; servers are added/removed via config edits or future UI.

**Validation rules (validate_agent_def):**
- `mcp_servers` must be a `list[str]` (string entry gets coerced to single-element list)
- Server names must not contain `"/"` (reserved for namespacing separator)
- Invalid entries rejected with error messages

### 3.21w.1 `utils/file_security.py` — Config File Permission Validation (MED-6)

**Responsibility:** Validates that config files (e.g., `mcp-servers.json`,
`agent.json`, `device-auth.json`) are owned by the current user and have no
group/world write permissions.

**Public API:**
```python
def assert_secure_file(path: str, expected_owner: bool = True) -> None
    # Raises PermissionError for unsafe permissions or wrong owner.
    # Raises FileNotFoundError if the file does not exist.
```

**Architecture:** Pure utility — no GTK, no network, stdlib only.

### 3.21x `utils/mcp_client.py` — MCP Client Library (MCP Phase A/B)

**Responsibility:** asyncio-to-threading bridge for MCP stdio transport. Manages persistent background event loops per conversation key, connection pooling, tool discovery, tool invocation, and OpenAI-format conversion. All async SDK calls bridged to sync Python via `_MCPLoopThread.get_thread()`.

**Key design decisions:**
- **One loop thread per conversation key** (`_MCPLoopThread._instances`) - avoids `asyncio.run()` per-call failure
- **Two-phase connect** - fast dict ops under global lock (mus), slow MCP handshake unlocked; sentinel `("connecting", thread_name)` prevents TOCTOU races
- **Tool cache** (`_tools_cache`) - per-conversation, per-server; invalidated on new connect to avoid repeated `list_tools()` round-trips
- **Sentinel guard** - sentinel stored in `_conversations` until async future completes; `is_connected()` returns False while sentinel present

**Public API** (verified by `inspect.signature()`; parameter is `conversation_key`, default `None`):
```python
connect(server_name: str, conversation_key: str | None = None) -> None
    # On success, returns None. On error, raises RuntimeError (contain subprocess failure) or TimeoutError.
disconnect(server_name: str, conversation_key: str | None = None) -> None
disconnect_all(conversation_key: str | None = None) -> None
get_connected_servers(conversation_key: str | None = None) -> list[str]
is_connected(conversation_key: str | None, server_name: str) -> bool
    # Parameter order matches source: conversation_key first, server_name second.
discover_tools(server_name: str, conversation_key: str | None = None) -> list[MCPToolDefinition]
call_tool(server_name: str, tool_name: str, arguments: dict, conversation_key: str | None = None) -> MCPToolResult
get_tools_for_api(server_names: list[str], conversation_key: str | None = None) -> list[dict]
    # Returns OpenAI-format tool definitions: [{"type":"function","function":{"name":"memory/search_nodes",...}}]
```

**Internal types:**
```python
@dataclass
class MCPToolDefinition:
    name: str           # namespaced: "server_name/tool_name"
    description: str
    input_schema: dict  # JSON Schema for tool arguments

@dataclass
class MCPToolResult:
    output: str         # Text output from tool
    error: str | None   # Error if failed
```

**Cleanup:**
- `create_conversation()` calls `mcp_disconnect_all(session_key)` on replacement (same session_key)
- `stop_all()` calls `mcp_disconnect_all()` (default conversation_key=None) before stopping AgentRuntime
- Thread drain (`_drain_and_stop`) waits for pending ops before join

### 3.21y `ui/handlers/connection_sync_handler.py` — Connection Sync Handler (Phase 3a extraction)

**Responsibility:** Post-connect wiring of live references into all dependent handlers. Called once by `GatewayHandler` via `set_sync_callback()` after the gateway WebSocket handshake completes. Injects the live `GatewayClient` and `AgentManager` into every handler that needs them (chat, command, project, agent_command, session, activity, etc.).

**Owns:** None — this is a stateless wiring function. All references it sets are owned by the handlers it injects them into.

**Constructor:**
```python
class ConnectionSyncHandler:
    def __init__(
        self,
        *,
        chat_handler,                  # ChatHandler — gets gateway_client + agent_manager
        main_content,                  # MainContent — gets agent_manager
        agent_list_handler,            # AgentListHandler — gets agent_mgr
        gateway_handler,               # GatewayHandler — SOURCE of live agent_mgr
        project_handler,               # ProjectHandler — gets agent_manager + review_handler
        command_handler,               # CommandHandler — gets gateway_client + agent_manager
        agent_command_handler,         # AgentCommandHandler — gets 6 setters wired at once
        session_handler,               # SessionHandler — gets agent_manager
        feed_handler,                  # FeedHandler — receives audit-report callback
        left_panel,                    # LeftPanel — refreshed on connect if a project is open
        review_handler,                # ReviewHandler — wired into project_handler
        activity_handler,              # ActivityHandler — gets 4 lifecycle callbacks from chat_handler
        agent_to_project,              # AgentRoutingTable — shared with agent_command_handler
        on_forward_clicked: Callable,  # chat bubble forward-button → ForwardHandler.show_forward_popover
        project_path_provider: Callable[[], str | None],  # lambda for agent_command_handler
    ) -> None
```

**Public API:**
```python
    def sync(self, gw: GatewayClient) -> None
        # Inject live GatewayClient + AgentManager into all 16 dependencies.
        # Called by GatewayHandler via set_sync_callback() — fires once per connect.
        # Idempotent: callers may invoke multiple times safely.
```

**Thread safety:** Called on the main thread by `GatewayHandler.on_connected()` (which itself is dispatched via `GLib.idle_add` from the gateway's background thread). All downstream setter calls must therefore be main-thread safe.

**Extracted from:** `window._sync_gateway_to_chat_handler` (former location: ui/window.py). The original method was 73 lines and violated the §3.6 boundary between window.py (composition root) and handlers (business logic).

    # NOTE: ActivityDrawer wiring (set_on_activity_bubble, set_on_agent_lifecycle,
    # set_on_command_output) has moved to ui/handlers/activity_wiring_handler.py
    # (constructed at startup, not deferred to connect) so the drawer works offline.
    # sync() now only injects AgentManager via set_agent_manager().

### 3.21z `ui/handlers/forward_handler.py` — Forward Handler (Phase 3b extraction)

**Responsibility:** Agent-to-agent message forwarding flow. Builds a `Gtk.Popover` listing every other agent the user could forward to; on selection, routes the text to the target (special agent or gateway agent), creates/selects the target's chat tab, and renders a "forwarded from <source>" bubble into it.

**Owns:** The popover widget construction, target agent resolution (special vs gateway), and forwarded bubble rendering. Stateless otherwise.

**Constructor:**
```python
class ForwardHandler:
    def __init__(
        self,
        *,
        main_content,                  # MainContent — for create_chat_tab, get_chat_box, _chat_notebook, scroll_chat_to_bottom, _tab_sessions
        chat_handler,                  # ChatHandler — placeholder for future evolution; not currently read
        chat_render_handler,           # ChatRenderHandler — for render_sync (forwarded bubble) + _on_forward_message
        agent_runtime_handler,         # AgentRuntimeHandler — for get_special_agents() + send_to_special_agent()
        gateway_handler,               # GatewayHandler — for agent_mgr.get_name() + gw.send_message() + gw.is_connected()
    ) -> None
```

**Public API:**
```python
    def show_forward_popover(
        self,
        text: str,
        anchor_widget,                 # Gtk.Widget — the popover's parent (e.g. a chat bubble)
        source_session_key: str | None,
    ) -> None
        # Build a Gtk.Popover with one button per other agent.
        # Excludes source_session_key from the list.
        # Deduplicates between special-agent and gateway-tab lists.
        # Returns silently if no other agents exist.

    def forward_to_agent(
        self,
        target_session_key: str,
        text: str,
        source_session_key: str | None,
        popover,                       # Gtk.Popover — popped down at start
    ) -> None
        # Route text to special-agent (send_to_special_agent) or gateway (gw.send_message).
        # Resolve source_name for the forwarded_from label.
        # Create new tab if target not in _tab_sessions, else select existing.
        # Render "You" bubble with forwarded_from=<source_name> via chat_render_handler.
        # Defer scroll to bottom via GLib.timeout_add(16, ...).
```

**Thread safety:** Called only on the main thread (button click handler). The `GLib.timeout_add(16, ...)` deferral runs on the default main loop context.

**Extracted from:** `window._on_forward_clicked` and `window._forward_to_agent` (former location: ui/window.py, 100 combined lines). The methods were tightly coupled via a shared `popover` variable and were extracted as a single unit to preserve that coupling.

### 3.21za `ui/handlers/settings_handler.py` — Settings Dialog Logic Handler

**Responsibility:** Settings dialog business logic — provider CRUD operations,
test-connection dispatch, red-dot status tracking. Bridges the GTK view
(`settings_dialog.py`) and the data store (`utils/providers_store.py`).
Pure logic — no GTK widget imports (only optional `GLib.idle_add` for
thread dispatch).

**Public API:**
```python
class SettingsHandler:
    def __init__(self, *, GLib_module=None, parent_window=None,
                 on_providers_changed=None, on_status_changed=None): ...
    def list_providers(self) -> list[ProviderConfig]
    def add_or_update(self, provider: ProviderConfig) -> None
    def remove(self, name: str) -> None
    def test_connection(self, provider: ProviderConfig) -> None   # async (thread)
    def check_verified_status(self) -> bool                       # any provider verified?
```

**Callbacks:**
- `on_providers_changed(list[ProviderConfig])` — fired on add/remove/edit.
- `on_status_changed(bool)` — fired when verified status changes.

### 3.21zb `ui/views/settings_dialog.py` — Settings Dialog GTK View

**Responsibility:** GTK4 dialog for managing LLM provider settings. Pure view —
receives data from `SettingsHandler`, emits user actions back through handler
methods. No direct file I/O or network calls.

**Public API:**
```python
class SettingsDialog(Gtk.Dialog):
    def __init__(self, handler: SettingsHandler): ...
    def refresh_providers(self, providers: list[ProviderConfig]) -> None

class _ProviderCard:
    # Internal: one card per provider in the dialog's ListBox.
    # Holds name/base_url/model/api_key entries, reveal toggle, test button.
```

**CSS classes:** `settings-provider-card`, `settings-test-button`,
`settings-status-dot`.

### 3.21zd `ui/handlers/activity_wiring_handler.py` — Activity Wiring Handler (SPEC-activity-drawer offline + local)

**Responsibility:** Single owner of all ActivityDrawer event wiring — gateway AND local, online AND offline. Constructed in `window.py._build()` and `.wire()` called unconditionally at startup (no gateway required), so the drawer receives events from the first local-agent tool call onward.

**Owns:** The adapter logic that converts `ActivityBubble` dataclass instances to drawer row dicts (via `bubble.to_drawer_row()`), and the routing of local `AgentRuntimeHandler` tool/lifecycle/exec events into the drawer.

**Constructor dependencies:**
- `activity_handler`: ActivityHandler — source of gateway bubbles + lifecycle separators
- `agent_runtime_handler`: AgentRuntimeHandler — source of local exec_command output, tool lifecycle, agent start/end
- `activity_drawer`: ActivityDrawer — the target

**Public API:**
```python
class ActivityWiringHandler:
    def __init__(self, *, activity_handler, agent_runtime_handler, activity_drawer) -> None
    def wire(self) -> None   # idempotent — wires 5 callbacks (gateway bubble/lifecycle, local exec/tool/lifecycle)
```

**Dedup invariant:** Local bridges fire only for special-agent sessions (`special:*` keys via `AgentRuntimeHandler`); gateway bridges fire for gateway sessions. The namespaces are disjoint for built-in agents. No explicit dedup code is needed (documented invariant).

**Extracted from:** `connection_sync_handler.sync()` (the former home of the gateway-bubble adapter closures). Moved out because `sync()` only runs on gateway connect, which broke the drawer offline.

**Offline name resolution:** `_resolve_local_agent_name()` uses `AgentRuntimeHandler.get_agent_name_for_session()` (the local registry), not the gateway `AgentManager`, so agent names resolve correctly without a connection.

### 3.22 `ui/views/feedbar.py` — RETIRED (deleted in SPEC-07 R4)

**Responsibility (historical):** Horizontal bar between toolbar and main content. Pure
view — no business logic. **Deleted in SPEC-07 (R4):** the 6-state activity machine now
renders to the chat surface's activity pill via the `status_target` duck-type
(`ActivityPillAdapter`, `ui/views/chat_surface.py`). This section is retained for
history only; no code lives at this path.

**Owns:** Status label + progress bar GTK widgets.

**Does NOT own:** Any state. All updates come from ActivityHandler via public API.

**Layout:** Horizontal outer box containing a vertical inner box (label on top, progress bar below).

**Public API:**
```python
set_status_text(markup)           # Update the state label (Pango markup)
set_progress_fraction(fraction)    # Set 0.0..1.0 bar fill; stops any active pulse
set_progress_hidden(hidden)         # Show/hide the progress bar (opacity 0 or 1)
set_progress_pulse(enable)         # Start/stop GTK pulse animation
pulse_progress()                   # Advance the pulse by one step (call every ~100ms)
set_progress_opacity(opacity)      # Set bar opacity 0.0..1.0 (for subtle idle pulse)
```

**States driven by ActivityHandler:** idle | sending | reasoning | streaming | tool_use | done

### 3.22a `ui/views/feed_card.py` — Project Feed Card Widgets (Phase 5)

**Responsibility:** GTK widget factories for individual feed cards in the project tab. Pure view — no business logic.

**Owns:** Card widget layout and CSS class application. State comes from `FeedCardData` dicts passed at construction.

**Public API:**
```python
build_feed_card(card_data: FeedCardData, *, on_review, on_accept, on_reject, on_copy) -> Gtk.Widget
build_feed_reference_widget(card_data: FeedCardData, *, on_click) -> Gtk.Widget
build_empty_feed_widget() -> Gtk.Widget
update_card_badge(card_widget: Gtk.Widget, accepted: bool | None) -> None
    # Post-construction badge update — called by FeedHandler after accept/reject
```

**card_type key determines card variant and icon** (see `models/feed_card.py` for the authoritative `css_class_for_type()` mapping; `utils/crabcard_parser.py` for the wire format):
- `file_created` — green dot icon, CSS: `feed-card-file-new`
- `file_modified` — amber dot icon, CSS: `feed-card-file-mod`
- `file_deleted` — red dot icon, CSS: `feed-card-file-del`
- `dir_created` — folder icon, CSS: `feed-card-dir-new`
- `dir_deleted` — folder icon, CSS: `feed-card-dir-del`
- `git_commit` — git-commit icon, CSS: `feed-card-git`
- `diff` — diff icon, CSS: `feed-card-diff`
- `agent_action` — user icon, CSS: `feed-card-agent`
- `task` — task icon, CSS: `feed-card-task`
- `system` — info icon, CSS: `feed-card-system`

### 3.22b `models/feed_card.py` — FeedCardData Dataclass (Phase 5)

**Responsibility:** Plain data container for Project Feed card state. No GTK, no network.

**Public API:**
```python
@dataclass FeedCardData:
    card_type: str       # "file_created" | "file_modified" | "file_deleted" |
                         # "diff" | "dir_created" | "dir_deleted" | "git_commit" |
                         # "agent_action" | "audit_report" | "task" |
                         # "agent_joined" | "agent_left" |
                         # "member_joined" | "member_left" | "system"
    source: str          # "gateway" | "crabwatch" | "system" | "git" | "approval"
    title: str           # Short title text
    body: str            # Body subtitle text
    author: str          # Display name of actor
    timestamp: datetime  # UTC timestamp
    project_name: str | None   # Set for project-scoped cards
    file_path: str | None      # Set for file/dir change cards (crabwatch)
    commit_sha: str | None     # Set for commit cards
    metadata: dict       # Free-form per-card metadata (status, needs_approval, etc.)
    accepted: bool | None = None    # True=accepted, False=rejected, None=pending
    seq_num: int | None = None      # Sequential display number (per project)

    # Class methods (Phase 1)
    @staticmethod
    def is_actionable(card_type, metadata=None) -> bool:
        """True if this card requires user action (Accept/Reject/Approve/Deny).
        File-change cards and cards with needs_approval=True are actionable."""

    @staticmethod
    def is_informational(card_type, metadata=None) -> bool:
        """True if this card is read-only (no action buttons needed).
        git_commit, system, task, audit_report, dir_created, dir_deleted are informational.
        agent_action with status in (None, running, complete, error) is informational."""

    # Serialization
    def to_dict() -> dict
    @classmethod
    def from_dict(cls, data: dict) -> FeedCardData

def css_class_for_type(card_type: str) -> str:
    """Map card_type to CSS class name for feed card styling."""
```

---


### 3.22c `ui/handlers/feed_handler.py` — Project Feed Handler (Phase 5)

**Responsibility:** Manages project feed card lifecycle — add, remove, persist, accept/reject. Coordinates with `CrabWatchHandler` (filesystem events) and `ChatRenderHandler` (crabcard extraction). Delegates rendering to `feed_card.py`, persistence to `feed_store.py`, git ops to `git_ops.py`.

**Owns:** `_cards` (dict: card_id → FeedCardData), `_card_widgets` (dict: card_id → Gtk.Widget), `_project_cards` (dict: project_name → [card_ids]), `_project_paths` (dict: project_name → abs_path), `_recent_git_paths` (dict: file_path → monotonic timestamp for echo suppression), `_lock` (threading.Lock), plus the background-persistence state: `_persist_queue` (dict: (project_path, card_id) → coalesced updates), `_persist_deferred` (dict: same key → (payload, tries)), `_persist_compactions` (list: (project_path, tries)), `_persist_queue_lock`, `_persist_wakeup` (threading.Event), `_persist_writer` (threading.Thread | None), `_persist_stop` (bool).

**Background feed writer (SPEC-UI-RESPONSIVENESS-2 Phases 1 + 3):** the GTK main thread never does feed disk I/O. `update_card` mutates `_cards` under `_lock` and calls `_enqueue_card_update` (coalesced per `(project_path, card_id)`, last-write-wins, <1 ms, no disk); the daemon `crabcakes-feed-writer` thread drains it via `feed_store.update_feed_card`, moving failures to `_persist_deferred` (retry cap 3 → ERROR drop, in-place retries preserving the try count). Accept/reject clicks ride the same queue. `shutdown_persist_writer()` (wired to project-close and window close-request) joins with a size-scaled timeout, empties within one bounded drain pass, and classifies leftovers.

**Drain pass order (`_drain_persist_queue`):** compactions → deferred updates → queued updates. Compactions are **snapshotted at pass start** (≤1 attempt per path per pass, whatever the source) and run `feed_store.compact_feed(path, window=FEED_WINDOW_DEFAULT)`; a failure re-enqueues **only-if-absent** with `tries+1` so it can never clobber a concurrent external trigger, and drops at 3 failures. Compactions ride their own list — a sentinel inside the update-keyed dict would be unpacked as a `(project_path, card_id)` pair (invariant: a compaction path never reaches `update_feed_card` as a `card_id`).

**Compaction triggers:** journal threshold (`utils/feed_store.py`, 500 lines) and post-append rate-limited `_maybe_compact`; plus `_enqueue_compaction(project_path)` — the **external** trigger used by the open-time path (§2.3.4 one-time large-feed compaction: `_load_and_render` requests it when the loaded feed exceeds `FEED_WINDOW_DEFAULT * 1.25`, accepted one-time upgrade latency). An external enqueue **replaces** any existing entry for that path with a fresh `tries=0`.

**Prune surfacing (`_surface_prune_card`, §2.3.5):** when a compaction prunes >0 cards, the **writer thread** builds a `system` `FeedCardData` ("Feed compacted — N oldest cards pruned (window W)") and dispatches the UI work through `GLib.idle_add`. The writer never calls `add_card`, never touches `_cards` / `_project_seq` / widgets: the `_ui` closure runs on the main thread, re-checks `_active_project_name` (a project closed mid-compaction suppresses the card), calls `add_card(card, persist=False)` — seq assignment, widget build, indexing — then `copy.deepcopy`s the card and hands that point-in-time copy to a tiny daemon thread that calls `feed_store.append_feed_card`. The copy path deliberately bypasses `add_card`'s `_loading`-gated persist (audit r1 #9), so the prune card is persisted even during a project load, and is immune to post-add in-memory mutation (audit r3 #7).

**`add_card(card_data, persist=True)`:** `persist=False` skips the per-card persist thread entirely — used only by `_surface_prune_card`, which owns persistence from the main-thread copy. All other callers keep the default.

**Bounded live-widget window (`MAX_LIVE_CARD_WIDGETS` = 120, `KEEP_NEWEST_CARDS` = 40; SPEC-MEMORY-WIDGET-RATCHET §2.1):** the invariant is **card data is unbounded; card widgets are bounded**. `_cards` retains every card this process has seen; `_card_widgets` (and `FeedTab._cards_by_id`) retain at most `MAX_LIVE_CARD_WIDGETS` `Gtk.Widget`s — freed widget memory is **never returned** to the OS by GTK/glibc (measured: 0 MB returned; 897 MB → 2,177 MB over ~7 h), which is why the bound exists at all: an unbounded widget map ratchets monotonically for the life of the session. `_evict_surplus_card_widgets` runs after every widget-append path (`add_card`, `add_cards_batch`, the project-open render loop, `_load_more`) and releases only victims that lie **entirely above the viewport** (`FeedTab.is_above_viewport`), chosen by `seq_num` across **all** projects — `_card_widgets` is one flat dict while widget creation is keyed on the *card's* project, which need not be the active one. The newest `KEEP_NEWEST_CARDS` by `seq_num` are never candidates, and `_load_more` passes its own page as `exclude` so a click can never re-evict what it just built. Eviction never drops data: the released `FeedCardData` is pushed back onto `_backlog` newest-first (`insert(0, …)`) under `_lock`, so Load More re-renders it (rebuilding the Load More widget if the backlog had been fully drained), and the loader **merges** its rebuilt list with any push that landed during its parse window — survivors first — rather than rebinding. The cap is a bound *when the geometry guard permits*, not an unconditional invariant: an unmeasurable widget or a scrolled-up viewport legitimately leaves the map above the cap. Detail: `SPEC-MEMORY-WIDGET-RATCHET.md` §2.1 / §7.

**`_project_cards` ordering:** `_project_cards[project]` is **newest-first by `seq_num` at every insertion point** — `add_card` and `add_cards_batch` `insert(0, …)`, and the load path sorts the deduped `prev + new_ids` candidates by `seq_num` descending. The order is load-bearing: `get_cards_for_project` drives the batch-bar count and Accept All, both of which `break` at the first non-actionable card, so a card placed at the tail is invisible to both until the project is reopened. Ordering is by `seq_num`, never by provenance — a live arrival during the load's parse window lands at index 0, while ids the feed pruned at compaction sort to the tail and are never promoted to "newest" (a pruned card must not become the first entry Accept All commits).

**`_project_seq` concurrency:** `_project_seq[project]` is a per-project counter; the `add_card` increment and the loader's rebuild both run under `_lock`, and the rebuild is a `max()` against the LIVE counter so a card that arrived during the parse window cannot have its `seq_num` reused. Because the counter is per-project, two cards from different projects can share a `seq_num`; when such a tie reaches the eviction victim ordering the tie is broken arbitrarily — documented accepted behaviour (spec erratum #4).

**Constructor:**
```python
class FeedHandler:
    def __init__(
        self,
        *,
        GLib,                        # gi.repository.GLib — for idle_add dispatch
        on_send_to_agent,            # Callable[[str, str], None] — send to agent
        on_card_added=None,          # Callable[[str], None] | None — card_id after add
        get_chat_box_for_session=None,  # Callable[[str], Gtk.Box | None] — chat box lookup for snapshots
    )
```

**Note:** `feed_tab` is NOT a constructor parameter. Set via `set_feed_tab()` after construction.

**Public API:**
```python
    def set_feed_tab(self, feed_tab) -> None
    def add_card(self, card_data: FeedCardData) -> str    # returns card_id
    def remove_card(self, card_id: str) -> None
    def get_card(self, card_id: str) -> FeedCardData | None
    def get_cards_for_project(self, project_name: str) -> list[FeedCardData]
    def clear_project(self, project_name: str) -> None
    def on_project_opened(self, project_name: str, project_path: str) -> None
    def on_project_closed(self, project_name: str) -> None
    def on_filesystem_event(self, card_data: FeedCardData) -> None
    def handle_review(self, card_id: str) -> None
    def handle_accept(self, card_id: str) -> None
    def handle_reject(self, card_id: str) -> None
    def handle_copy(self, text: str) -> None
    def handle_batch_accept(self, card_ids: list[str]) -> None
        # Phase 5: accepts a list of file-change cards in one go

    # Spec PROJECT-SETTINGS-BAR-ENHANCED: settings-bar auto-accept level
    # (file-change scoped; exec_command is a SEPARATE axis and is never
    # collapsed into this label — the bar shows "⚡ files: ...").
    def get_auto_accept_level(self) -> str
        # Returns "off" | "diffs" | "files" | "all".
        #   off:   diff off AND file_created/modified/deleted all off
        #   diffs: diff on  AND file types all off
        #   files: diff off AND file types all on
        #   all:   all four on
    def set_auto_accept_level(self, level: str) -> None
        # Set file-change level; enabling routes through the warning gate
        # (_show_auto_accept_warning) and commits via _refresh_auto_accept_state.
        # Invalid level or _prefs is None -> no-op. exec_command untouched.
    def set_on_auto_accept_level_changed(self, cb) -> None
        # Register callback fired AFTER a level COMMITS (i.e. after the async
        # warning dialog's on_confirm). Used by window to rebuild the bar post-
        # confirmation; NOT fired on cancel (BUG #4). cb(level: str).
```

**Thread safety:** All GTK operations via `GLib.idle_add()`. Git ops in background threads. `_lock` protects dict mutations.

**Echo suppression:** Git accept/reject operations modify files on disk, causing CrabWatch to fire filesystem events that would create duplicate cards. FeedHandler suppresses these echoes by recording `file_path` + timestamp in `_recent_git_paths` before starting git threads. `on_filesystem_event()` checks this map and drops events for paths operated on within the last 3 seconds.

---

## 3.23 `ui/handlers/activity_handler.py` — Activity State Machine (Phase 6)

**Responsibility:** The 6-state activity machine that drives the activity pill (the
chat surface's status element — SPEC-07 R4; formerly the Response Status bar/FeedBar).
Manages state transitions, live timers, and status renders via the status_target.

**Owns:** All state machine state (timers, counters, timestamps). Does NOT own any GTK widgets — renders through the status_target duck-type's public API.

**Does NOT own:** The status target or MainContent — received as constructor dependencies.

**Constructor dependencies:**
- `status_target`: duck-type implementing `set_status_text(text, state=None)` (+ no-op progress quartet) — in production, `ActivityPillAdapter` over the active chat surface
- `main_content`: MainContent instance — used via `main_content.get_review_bar()` to read current ReviewBar state
- `GLib_module`: optional GLib reference for thread dispatch

**Thread safety:** All GTK calls via `GLib.idle_add()` / `timeout_add()`. Entry points are called from GTK main thread only.

**States:** idle | sending | reasoning | streaming | tool_use | done

**Public API (entry points — called from `window._on_ws_event`):**
```python
on_agent_start(session_key, data=None)           # agent phase=start → reasoning
on_agent_end(session_key, data=None)            # agent phase=end → done (+ 5s idle timer)
on_agent_error(session_key, data=None)          # agent phase=error → idle
on_tool_use(tool_name, session_key, data=None)  # tool_call event → tool_use
on_chat_delta(delta_text, session_key)           # first delta → streaming
on_agent_message_received(session_key)           # pre-flight → sending
on_send_initiated(session_key)                    # send button pressed → sending + 30s pre-flight timeout
on_res_confirmed(session_key)                     # gateway res confirmed → reasoning (phase 2 begins)
on_gateway_event(event, payload)                 # universal entry — routes agent/chat/tool_call/res events
on_chat_final(session_key)                       # chat final (no-op; on_agent_end handles completion)
```

**Bug fix (Phase 1 of SPEC-smarter-chat-ux) — missing message recovery:**

State tracked for recovery when chat final arrives with no message body:
- `_assistant_text_buffer[session_key] → str`: last assistant text per session

Public setters for the recovery callbacks:
```python
set_on_assistant_buffer(cb)                     # cb(session_key, text) — forward each assistant delta
set_on_lifecycle_completed(cb)                 # cb(session_key, text) — lifecycle end/error fires this; ChatHandler renders fallback
set_on_activity_bubble(cb)                     # cb(ActivityBubble) — tool/plan/approval/command_output/patch events; Phase 2 of SPEC-smarter-chat-ux
set_on_agent_lifecycle(cb)                      # NEW (SPEC-activity-drawer) — cb(session_key, agent_name, "start"|"end") for per-agent separator rows in the drawer
set_on_agent_start(cb)                          # cb(session_key) — clears ChatHandler render guard for next round. Receives RAW session_key from the gateway event (the agent key), not _active_session() key.
set_agent_routing(routing_table)                # Injected by window.py._build(). Enables _is_ui_active() to resolve project tabs for agent session keys.
```

**Activity Bubbles (Phase 2 of SPEC-smarter-chat-ux):**

ActivityHandler fires `_activity_bubble_callback` for each gateway event that warrants a visible status indicator in the chat:
- `stream=lifecycle phase=start` → ⏳ Agent started...
- `stream=tool phase=start` → 🔧 Running {name}...
- `stream=tool phase=end` → ✅ {name} ({durationMs}ms)  or  ❌ {name} — error
- `stream=plan` → 📋 Plan: {title} ({n} steps)
- `stream=approval phase=requested` → 🔒 Approval needed: {command}
- `stream=patch phase=end` → ✏️ {name}: +{added} ~{modified} -{deleted} files

Architecture: ActivityHandler only creates ActivityBubble dataclass instances and fires the callback. As of SPEC-activity-drawer Phase 1, the callback target is `ActivityDrawer.append_event(bubble.to_drawer_row())` — the adapter that converts the dataclass to the drawer's dict shape lives in `activity_wiring_handler.py` (a dedicated handler constructed at startup, not deferred to gateway connect, so the drawer works offline). ChatHandler no longer renders activity bubbles.

Activity bubbles (Phase 2 of SPEC-smarter-chat-ux):
- `on_gateway_event()` parses `stream` values `tool`, `plan`, `approval`, `patch`
- Constructs `ActivityBubble` (from `models/activity.py`) and fires the callback
- ChatHandler's `render_activity_bubble()` renders via `ChatRenderHandler.render_activity()`

Tab routing uses `agent_to_project` (AgentRoutingTable). In ChatHandler this is passed via constructor; in ActivityHandler it's injected via `set_agent_routing(routing_table)`. Lazy import of `models.activity` inside method body to avoid circular deps.

**Two-phase progress tracking:**
- Phase 1 (send-initiated): Time-driven progress bar — on_send_initiated starts 30s timer
- Phase 2 (event-driven): Event-driven hop counting — on_res_confirmed transitions to phase 2

**State transitions:**
- `agent phase=start` → `reasoning`
- `agent phase=end` → `done` (auto → `idle` after 5s)
- `agent phase=error` → `idle`
- `tool_call` event → `tool_use`
- First chat delta → `streaming`
- Agent message in history → `sending` (pre-flight)

### 3.22d `utils/feed_store.py` — Feed Persistence (Phase 5; read/write paths rebuilt in SPEC-UI-RESPONSIVENESS-2 Phases 2-3)

**Responsibility:** JSON + JSONL persistence for feed cards. Pure functions — no classes, no GTK, no state (one documented deviation: the compaction rate-limit table). Loads/saves `FeedCardData` lists from `.crabcakes/feed.json` per project, plus the append-only update journal `.crabcakes/feed-updates.jsonl`.

**Two-file model (§2.2):**
| File | Role |
|------|------|
| `.crabcakes/feed.json` | Snapshot — the full card list, written atomically (`.tmp` + `os.replace`, compact JSON, 0o600) |
| `.crabcakes/feed-updates.jsonl` | Update journal — one `{"card_id", "updates"}` JSON line per update, O(1) append, folded into the snapshot at compaction |

An update is recorded without the card having to exist yet (the journal is the source of truth for pending changes for a card); replay is idempotent and a torn final line is tolerated (replay stops at the first unparseable line).

**Lock discipline — the uniform rule (§2.2.3):** **every** `feed_store` mutation, snapshot or journal, holds the **feed flock** (`.crabcakes/feed.json.lock`) for its whole critical section. `fcntl.flock` is per-inode, so one lock inode for all mutations is what makes "a concurrent compaction can never truncate between an append and its fold" true. There is no separate journal lock. `_acquire_lock` is **bounded** (2 s default deadline, non-blocking attempts in a loop, returns `None`) — no unbounded blocking lock on any path reachable from the main thread; `load_feed` uses a size-scaled timeout (`min(60.0, 10.0 + size_bytes/1_000_000)`) because a large compaction is slow. On `None`: write paths skip + WARNING (the journal/queue keeps the data safe), read paths fall back to a lock-free read + WARNING (documented narrow residual race — the next load re-reads consistently).

**Retention (window + pins, §2.3.2):** `compact_feed` keeps the **newest `window`** cards (`FEED_WINDOW_DEFAULT = 2000`) by list order, then additionally keeps every **pinned** card from outside that slice. A card is pinned — never pruned — if any of: `accepted is not None` (a recorded decision; pruning would orphan it and the `seq_num` narrative), it is a `git_commit` card, or it is **genuinely undecided** — `metadata.needs_review` / `metadata.needs_approval` AND `accepted is None` AND `metadata.status not in ("approved", "denied")` (F1 amendment: the transient `needs_approval` flag is never cleared on resolution, so it pins only while no decision is recorded anywhere; the durable record `metadata.status` is written by `approve_exec`, and `update_card` also persists `accepted` when it is set). Pruning preserves chronological order and logs a WARNING with the prune count **and** how many outside-window cards the pins rescued.

**Compaction triggers (§2.3.3):** (1) journal threshold — `update_feed_card` compacts at `JOURNAL_COMPACT_THRESHOLD` (500) lines, with `window=FEED_WINDOW_DEFAULT`; (2) post-append `_maybe_compact` — rate-limited to at most one per project per `_COMPACT_MIN_INTERVAL` (60 s), fires when the snapshot exceeds `FEED_WINDOW_DEFAULT * 1.25`; (3) project open — `FeedHandler._load_and_render` requests a one-time compaction when a legacy feed exceeds the same 1.25× soft bound (see §3.22c). **`load_feed` never compacts.** Compaction order is crash-safe: snapshot `os.replace` FIRST, journal truncate SECOND (a crash between them leaves an unfoldable-but-idempotent journal).

**Public API:**
```python
def load_feed(project_path: str) -> list[FeedCardData]
    # Load cards from .crabcakes/feed.json + replay the journal. Chronological (oldest first).
    # Snapshot and journal are read under ONE feed-flock hold. Empty list if missing/invalid.

def save_feed(project_path: str, cards: list[FeedCardData]) -> None
    # Save cards to .crabcakes/feed.json. Creates .crabcakes/ if needed. Takes the feed lock.

def append_feed_card(project_path: str, card: FeedCardData) -> None
    # Append single card (lock → load → append → write), then a lock-free rate-limited
    # _maybe_compact check. Multiple caller threads serialize on the flock.

def update_feed_card(project_path: str, card_id: str, updates: dict) -> bool | None
    # Hot path: append_card_update (O(1) journal line) + threshold compaction.
    # True = recorded (journal-write); False = write failure (append AND legacy failed →
    # the caller defers and retries); None = legacy path ran and the card was gone (dropped).

def append_card_update(project_path: str, card_id: str, updates: dict) -> bool
    # One journal line, under the feed flock (with best-effort torn-tail repair).

def compact_feed(project_path: str, window: int | None = None) -> int
    # Fold journal → snapshot, apply the window + pins, truncate the journal. Returns pruned count.
    # window=None prunes nothing (returns 0) — the no-window path is retained.

def _maybe_compact(project_path: str) -> None
    # Rate-limited (60 s/project) post-append compaction trigger; timestamp recorded before
    # the size check so a sub-window feed still spends the interval (prevents re-check thrash).
```

**Architecture rules:**
- Lives in `utils/` — pure Python, no GTK, no network
- Thread-safe via the **feed flock** (called from background threads by FeedHandler)
- Imports `models.feed_card.FeedCardData` only
- No imports from `ui/` or `gateway/`
- **Documented deviation from the "pure functions — no state" contract:** `_compact_last` / `_compact_rl_lock` keep per-project compaction timestamps for the rate limit. File-I/O orchestration state only (no GTK, no handler state), guarded by its own lock, never evicted (≈100 B per project).
- **Documented pre-existing limitation:** `_ensure_gitignore_entry`'s read-modify-write is not under the feed flock (audit-accepted, not fixed — locking per-project `.gitignore` writes would be scope creep).

### 3.22e `utils/crabcard_parser.py` — Crabcard Block Parser (Phase 5)

**Responsibility:** Parse `` ```crabcard `` blocks from agent chat messages into `FeedCardData`. Pure function — no GTK, no state. Returns cleaned text with placeholder markers for downstream rendering.

**Public API:**
```python
def extract_crabcards(text: str, project_name: str, agent_name: str = "agent") -> tuple[str, list[FeedCardData]]
    # Parse ```crabcard blocks. Returns (cleaned_text, cards).
    # Cleaned text contains \x00CRABCARD_REF:N\x00 placeholders.

def is_crabcards_placeholder(text: str) -> bool
    # True if text contains a crabcard placeholder marker.

def get_placeholder_index(placeholder: str) -> int | None
    # Extract card index from placeholder string.
```

**Crabcard format:**
```
```crabcard
type: <card_type>
title: <title text>
file: <optional file path>
additions: <optional int>
deletions: <optional int>
commit_sha: <optional str>
task_id: <optional str>
---
<body content — diff text, description, etc.>
```
```

**Architecture rules:**
- Lives in `utils/` — pure Python, no GTK, no network
- Imports `models.feed_card.FeedCardData` only
- No imports from `ui/` or `gateway/`
- Called from `ui/handlers/chat_render_handler.py` during `render_sync()`

---

## 3.24 `ui/handlers/crabwatch_handler.py` — CrabWatch Filesystem Watcher (Phase 5)

**Responsibility:** Watch the active project directory for filesystem changes via `Gio.FileMonitor`. Route `FeedCardData` events to `FeedHandler.on_filesystem_event()` for display in the Project Feed.

**Architecture rules:**
- Lives in `ui/handlers/` — uses GLib only (no GTK widget creation)
- Thread-safe via GLib event callbacks; GTK dispatch via `GLib.idle_add()` where needed
- No IPC, no sockets, no external processes — pure GTK4/Gio integration

**Constructor dependencies:**
- `GLib_module`: GLib reference for dispatch
- `on_event`: callback receiving `FeedCardData` dicts — wired to `FeedHandler.on_filesystem_event()`

**Public API:**
```python
class CrabWatchHandler:
    def start_watching(project_path: str, project_name: str) -> None:
        """Start monitoring project_path. Stops any previous watch."""
    def stop_watching() -> None:
        """Stop monitoring."""
    def is_watching() -> bool
```

**Events fired as `FeedCardData`:**

| Gio event | card_type | title format |
|-----------|-----------|--------------|
| `CREATED` (file) | `file_created` | "Created <filename>" |
| `CHANGED` (file) | `file_modified` | "Modified <filename>" |
| `DELETED` (file) | `file_deleted` | "Deleted <filename>" |
| `CREATED` (dir) | `dir_created` | "Created directory <filename>" |
| `DELETED` (dir) | `dir_deleted` | "Deleted directory <filename>" |

**Ignored paths (no events):**
- `.crabcakes/`, `.git/`, `node_modules/`, `__pycache__/`
- `*.pyc` files, `.DS_Store`, dotfiles (names starting with `.`)

**Debouncing:** Events on the same relative path within 200ms are batched — only the final state fires a card.

**Wiring in `window.py`:**
- Created alongside `FeedHandler` in `_build()`
- `start_watching(p, n)` called in `set_on_project_opened` callback chain
- `stop_watching()` called in `set_on_project_tab_close` callback chain


### 3.25 `models/conversation_snapshot.py` — Conversation Snapshot Data

**Responsibility:** Plain data containers for conversation snapshots. No GTK, no network, no git.

**Public API:**
```python
@dataclass SnapshotMessage:
    role: str
    content: str
    timestamp: str | None

@dataclass ConversationSnapshot:
    session_key: str
    agent_name: str
    messages: list[SnapshotMessage]
    created_at: str
    token_count: int
```

**Used by:** `utils/conversation_store.py` (creation), `utils/feed_store.py` (persistence)

---

### 3.26 `models/team.py` — Project Team Data

**Responsibility:** Data models for project team membership. Pure data — no GTK, no network, no file I/O.

**Public API:**
```python
@dataclass TeamMember:
    session_key: str
    display_name: str
    role: str
    color: str

@dataclass ProjectTeam:
    project_path: str
    members: list[TeamMember]

    def get_session_keys() -> list[str]
    def to_dict() -> dict
    @staticmethod
    def from_dict(data: dict, project_path: str) -> ProjectTeam
```

**Used by:** `utils/project_awareness.py` (load/save team), `utils/projects.py` (deprecated shim)

---

### 3.27 `utils/project_awareness.py` — Project Awareness System

**Responsibility:** Manages the `.crabcakes/` directory per project. Reads/writes project metadata, team membership, workflow state, git context, and awareness documents. Pure Python — no GTK, no network. This is the authoritative source for project context; `utils/projects.py` deprecated its membership functions in favor of this module.

**Manifest:**
- Reads: `.crabcakes/*` (project.md, team.json, context.md, awareness.json, workflow.md)
- Writes: `.crabcakes/*` (same files)

**Public API:**
```python
def get_crabcakes_dir(project_path: str) -> str
def load_team(project_path: str) -> ProjectTeam
def save_team(project_path: str, team: ProjectTeam) -> None
def is_project_onboarded(project_path: str) -> bool
def get_project_context(project_path: str) -> str
def get_workflow_content(project_path: str) -> str
def build_awareness_block(project_path: str, ...) -> str
def load_custom_identity(project_path: str) -> dict | None
def clean_manifest_skeleton(project_path: str) -> bool    # SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS §2.8
```

**Architecture rules:**
- Lives in `utils/` — may import `models/` only (TeamMember, ProjectTeam, WorkUnitStore)
- Imports `utils/config.py`, `utils/git_ops.py`, `utils/workflow_state.py`
- No imports from `ui/` or `gateway/`

**Work Unit awareness counts (SPEC-TASK-SYSTEM-FULL-REDESIGN §14).** `_get_task_info(work_store)` reports
**Work Unit** status counts — the schema is `{total, spec_pending, spec_ready, in_progress, done}`
(replacing the old Task counts `{total, in_progress, blocked, pending, done}`):
- `total` = all units (including cancelled — reflects all work).
- `spec_pending` = `draft` OR `spec-pending`.
- `spec_ready` = `spec-ready`. `in_progress` = `in-progress` OR `auditing`. `done` = `done`.
- `cancelled` counts only in `total`, never in a bucket. `draft` folds into `spec_pending`.

`build_awareness_dict()` uses the **project-bound loader** `_work_units_for_awareness(project_path)` to load
the project's Work Units on demand into a throwaway `WorkUnitStore` (returns `None` on any error — awareness
is best-effort and never crashes the system-prompt build). It does NOT thread a `work_store` through its
callers and remains read-only (no snapshot writes; the `.crabcakes/` mtime cache governs invalidation).

**Phase CB-3 — Awareness variable caps (BUG #6 fix).** `build_awareness_dict()` caps
`TEAM_ROSTER` at 500 chars and `CURRENT_STATE` at 1,000 chars, matching the existing
`PROJECT_MEMORY` truncation pattern. See `TEAM_ROSTER_MAX_CHARS` and
`CURRENT_STATE_MAX_CHARS` constants. See SPEC-CONTEXT-BLOAT-PHASE-3.md §2.4.

**SPEC-CONTEXT-MD-SYSTEM-FIX — Read cap, CURRENT_TASK, lifecycle.** Three changes:
1. **Read cap:** `CONTEXT_READ_CAP = 8000` (was hardcoded `3000`). Applied in both `build_awareness_dict()` and `build_awareness_block()`. The old 3000-char cap dropped 69%+ of content; the current-task pointer was typically truncated away.
2. **`CURRENT_TASK` trusted directive:** `get_current_task(project_path)` extracts the last `## ` heading from `context.md`. Injected into `build_awareness_dict()` as `CURRENT_TASK` (a TRUSTED directive, NOT wrapped in `_untrusted_fence`). The `project-awareness.md` system prompt template renders it in a `## Current Task` section. `PROJECT_MEMORY` remains wrapped in the untrusted fence (HIGH-5 preserved). The `utils/prompt_loader.py` variables dict passes `CURRENT_TASK` through to the template.
3. **`append_project_context` lifecycle:** Supersedes stale "in progress"/"pending" entries (marks `[SUPERSEDED]`) when a "complete"/"done"/"✅" entry is appended. Word-boundary regex matching prevents suffix overmatch (Phase A1 ≠ Phase A10). FIFO eviction at `MAX_CONTEXT_ENTRIES = 50`. Code-block-aware state machine in `_split_entries()` handles `## ` inside triple-backtick fences. Cache key includes SHA1 content fingerprint (mtime + content hash) so same-second writes invalidate correctly. `build_awareness_dict()` returns a shallow copy (`dict(parts)`) so caller mutation cannot poison the cache.

See `docs/specs/SPEC-CONTEXT-MD-SYSTEM-FIX.md` and post-mortem `docs/post-mortems/2026-07-20-CONTEXT-MD-SYSTEM-FIX-POST-MORTEM.md`.

**SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS §2.8 — Manifest skeleton cleanup.** `clean_manifest_skeleton(project_path) -> bool` is a pure helper that removes manifest sections (under `^## ` headings) whose body contains only whitespace and HTML comments. The top-level `# Project` title is always preserved. Each section is evaluated independently — a comment containing `## ` text cannot swallow later sections because the splitter uses real line boundaries, not a whole-file `re.sub(DOTALL)`. Missing/unreadable files are a safe no-op returning `False`. Writes only when content changed. Called from `utils/workflow_state.py:advance_phase()` when `phase_name == "onboarding"` completes, ensuring the manifest left by the onboarding template's skeleton is cleaned of comment-only placeholders before subsequent reads. `build_awareness_dict()` remains read-only and does not invoke this or any write path.

### 3.27a `utils/project_trust.py` — Per-Project Trust Gate (HIGH-5)

**Responsibility:** Gates `.crabcakes/` rule/bug file ingestion behind a
per-project trust prompt on first open. After the user approves once, the
project path is recorded in the trust store and subsequent loads skip the
gate. If denied, the project's `.crabcakes/` content is silently skipped.

**Storage:** `~/.config/crabcakes/trusted_projects.json` — maps absolute
project path to `{trusted: bool, ts: ISO8601, reason: str}`.

**Public API:**
```python
def is_project_trusted(project_path: str) -> bool
def trust_project(project_path: str, reason: str = "") -> None
def untrust_project(project_path: str) -> None
def prompt_if_needed(project_path: str, prompt_fn: Callable) -> bool
    # Calls prompt_fn (UI callback) if no trust decision exists yet.
    # Returns True if trusted, False otherwise.
```

**Thread-safety:** Read-mostly. Writes go through a module-level lock to
avoid two concurrent first-opens both prompting.

**Architecture:** Pure utility — no GTK, no network, stdlib only. The UI
layer supplies the `prompt_fn` callback (e.g., a dialog).

---

### 3.27b `utils/work_persistence.py` — Work Unit Persistence

**Responsibility:** Owns `.crabcakes/work.json` as the **source of truth** for Work Units and the
generated `.crabcakes/tasks.md` summary. Handles legacy `tasks.md` → `work.json` migration.
Pure file I/O — no GTK, no network (SPEC-TASK-SYSTEM-FULL-REDESIGN §3.2, §14).

**Source-of-truth rule:** `.crabcakes/work.json` is authoritative. `.crabcakes/tasks.md` is a
**generated** summary (for humans only) and is never hand-edited — it is regenerated from
`work.json` on every save/load.

**Public API:**
```python
def work_json_path(project_path: str) -> str
def tasks_summary_path(project_path: str) -> str
def load_work_units(project_path: str) -> list[WorkUnit]      # missing/invalid → [] with logged warning
def save_work_units(project_path: str, work_units) -> None    # atomic JSON write, then regen tasks.md
def render_tasks_summary(work_units) -> str
def write_tasks_summary(project_path: str, work_units) -> None
def load_or_migrate_work_units(project_path: str) -> list[WorkUnit]  # authoritative load + one-shot legacy migrate
```

**Legacy migration (`load_or_migrate_work_units`):**
1. A valid, versioned `work.json` is authoritative — load it, regenerate `tasks.md`, return units.
2. No `work.json` → best-effort parse of a legacy `tasks.md` and persist once (migration).
3. No recognizable content → `[]`, nothing written.

**Atomicity / error handling (spec §3.1):** writes are atomic (temp file + `os.replace`); the JSON is
persisted before `tasks.md` is regenerated, so a failed summary write never corrupts or rolls back
`work.json`. A corrupt project state (`.crabcakes` is a regular file) raises `RuntimeError` that is
caught and logged — never crashes project open. All load paths are best-effort and never raise.

**Used by:** `ui/handlers/work_handler.py` (all `/work` mutations), `models/work_unit.py` consumers,
and `utils/project_awareness.py` (via the project-bound loader `_work_units_for_awareness` for awareness).

---

### 3.28 `utils/audit_parser.py` — Audit Report Extraction

**Responsibility:** Parse structured `## Audit Report` sections from agent messages into typed `AuditReport` dataclass instances. Pure functions — no GTK, no state.

**Public API:**
```python
@dataclass AuditReport:
    severity: str          # "bug" | "issue" | "suggestion"
    file_path: str
    task: str
    bug_description: str
    pattern: str | None
    reviewer: str
    target_role: str
    project_path: str | None

def extract_audit_reports(text: str) -> list[AuditReport]
```

**Used by:** `ui/handlers/agent_command_handler.py` (scan agent responses), `utils/feedback_processor.py` (persistence)

---

### 3.29 `utils/conversation_store.py` — Snapshot Creation Utilities

**Responsibility:** Create `ConversationSnapshot` data objects from plain Python data (message lists) and git diffs. Pure functions — no GTK, no network.

**Public API:**
```python
def build_snapshot(session_key, agent_name, messages, git_diff=None) -> ConversationSnapshot
def build_snapshot_from_conversation(conv, git_diff=None) -> ConversationSnapshot
```

**Architecture rules:**
- Lives in `utils/` — imports `models/` only (ConversationSnapshot, SnapshotMessage)
- May import `utils/git_ops.py` for git diff retrieval
- No imports from `ui/` or `gateway/`

---

### 3.30 `utils/feedback_processor.py` — Audit Report File I/O

**Responsibility:** All file I/O for structured audit report processing. Writes audit reports to agent bug journals, reads agent definitions for role resolution. Pure Python — no GTK, no network.

**Public API:**
```python
def process_audit_report(report: AuditReport, project_path: str) -> str
def get_bug_journal_path(project_path: str, target_role: str) -> str
def append_review_entry(project_path: str, entry: str) -> None
```

**Architecture rules:**
- Lives in `utils/` — imports `models/` only (AuditReport via audit_parser)
- Imports `utils/agent_defs.py` for role resolution, `utils/review_log.py` for persistence
- No imports from `ui/` or `gateway/`

---

### 3.31 `utils/review_log.py` — Review Log Persistence

**Responsibility:** Append and retrieve review log entries per project. Pure file I/O — no GTK, no network.

**Public API:**
```python
def append_review_entry(project_path: str, entry: str) -> None
def get_review_log(project_path: str) -> list[str]
```

---

### 3.32 `utils/spellcheck.py` — Spell Check Engine

**Responsibility:** Detect misspelled words and provide suggestions via Enchant. Pure logic — no GTK, no network.

**Public API:**
```python
def check_text(text: str) -> list[tuple[int, int, str]]
def get_suggestions(word: str) -> list[str]
def is_available() -> bool
```

**Used by:** `ui/handlers/input_toolbar_handler.py` (spell check logic)

---

### 3.33 `utils/workflow_state.py` — Workflow State Tracker

**Responsibility:** Manage `.crabcakes/workflow.md` per project. Tracks which workflow phases are complete, which is current, and timestamps. Pure file I/O — no GTK, no network.

**Public API:**
```python
def init_workflow(project_path: str) -> None
def advance_phase(project_path: str, phase: str) -> None
def get_current_phase(project_path: str) -> str | None
def get_workflow_content(project_path: str) -> str
```

**Architecture rules:**
- Lives in `utils/` — pure Python, no GTK, no network
- May import `utils/config.py` for project path helpers, `utils/project_awareness.py` for directory management
- No imports from `ui/` or `gateway/`

**SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS §2.9 — Onboarding completion hook.** When `advance_phase(project_path, "onboarding")` is called, the function invokes `clean_manifest_skeleton(project_path)` (lazy import from `utils/project_awareness.py`) to strip comment-only manifest sections left by the onboarding skeleton. Cleanup failures are non-fatal and logged — a successful workflow transition is never blocked by an I/O error in cleanup. The lazy import avoids a circular dependency at module load time. `ValueError` for invalid phase names is preserved.

---

### 3.34 `ui/views/chat_input_toolbar.py` — Chat Input Toolbar (View)

**Responsibility:** Compact toolbar for the chat input area — find/replace bar and spell check toggle button. Pure view — widgets only, no business logic. All logic lives in `InputToolbarHandler`.

**Public API:**
```python
class ChatInputToolbar(Gtk.Box):
    def __init__(self, on_find_changed=None, on_find_next=None, on_find_prev=None,
                 on_replace=None, on_replace_all=None, on_spell_toggled=None)
    def show_find_bar(self) -> None
    def hide_find_bar(self) -> None
    def get_find_text(self) -> str
    def get_search_text(self) -> str
    def get_replace_text(self) -> str
    def update_match_count(self, current: int, total: int) -> None
    def set_spell_active(self, active: bool) -> None
```

---

### 3.35 `ui/views/feed_tab.py` — Feed Tab View

**Responsibility:** Pure view container for the Projects notebook's "Feed" sub-tab. No business logic, no state mutations.

**Public API:**
```python
class FeedTab(Gtk.Box):
    def get_card_container() -> Gtk.Box
    def append_card(card_widget: Gtk.Widget, card_id: str | None) -> None
    def remove_card(card_id: str) -> None
    def scroll_to_bottom() -> None
        # Unconditional — used on project open to jump to newest
    def smart_scroll_to_bottom() -> None
        # Phase 4: only scrolls when user is within 80px of bottom (synchronous)
    def schedule_scroll_to_bottom() -> None
        # Deferred unconditional scroll — waits for vadjustment 'changed' signal
        # so the scroll target reflects post-layout content height
    def schedule_smart_scroll_to_bottom() -> None
        # Phase 4 deferred: proximity check (stale upper) + deferred scroll
        # Combines smart_scroll_to_bottom's proximity check with schedule_scroll_to_bottom's
        # deferred signal approach to avoid the stale-upper bug when appending cards
    def update_batch_bar(pending_count: int) -> None
        # Phase 5: shows/hides batch accept bar based on pending file-change cards
    def set_batch_accept_callback(callback: Callable[[], None]) -> None
        # Phase 5: wires FeedHandler's batch-accept handler into the bar's button
```

**Geometry primitives (SPEC-MEMORY-WIDGET-RATCHET §2.2):** `get_vadjustment()` returns the feed `ScrolledWindow`'s vadjustment, `is_near_bottom(slack=80)` reports whether the viewport sits within `slack` px of the feed bottom, and `is_above_viewport(widget)` reports whether a card widget lies **entirely** above the visible region — the three accessors `FeedHandler._evict_surplus_card_widgets` uses to decide what it may release. `is_near_bottom` returns `True` when geometry is unavailable (nothing rendered → eviction is safe); `is_above_viewport` returns `False` (never evict).

**Fail-safe contract — "unknown" is never "above".** A widget that has never been allocated is unmeasurable, and GTK 4.14 reports that as `compute_bounds -> (True, zero rect)`: verified under Xvfb, an unrealized/unmapped `Gtk.Label` in the card container gives `ok=True` with `origin.y=0.0, size.height=0.0`. The naive test `origin.y + height <= vadj.value` therefore answers **True — "above the viewport"** for every such widget and would destroy cards that merely have not been laid out yet (e.g. while the Feed sub-tab is unmapped). The guard is consequently `not ok` **or** a non-positive extent (`rect.size.height <= 0`), and the `<= 0` half is load-bearing, not defensive padding — the eviction loop relies on the same condition, since a zero-height victim contributes no height to the scroll compensation. The same `ok is False` path covers a **non-ancestor** target: a widget parented outside `_card_container` yields `ok=False` with a zero rect, so it too is classified "unknown" and never evicted. Widgets with no `compute_bounds` at all (test doubles) hit the `AttributeError`/`TypeError` branch and also return `False`. Detail: `SPEC-MEMORY-WIDGET-RATCHET.md` §2.2 / §7.

**`get_vadjustment` correction (P2-audit finding):** the method's "or None before first map" wording describes only the `_feed_scroll is None` case, not pre-layout state. A freshly constructed `FeedTab` **does** own a real `Gtk.ScrolledWindow`, so `get_vadjustment()` returns a real (zeroed: `upper == 0.0`) `Gtk.Adjustment` before any layout has run; `None` occurs only when `_feed_scroll` is absent (the explicit `None` branch, and `MockFeedTab`-style doubles). Both paths still land on the fail-safe answers above, because a zeroed adjustment yields the same "nothing rendered" arithmetic.

---

### 3.36 `ui/handlers/input_toolbar_handler.py` — Input Toolbar Handler

**See §3.6a for the authoritative public API and rules.**

**Responsibility:** Owns all input toolbar logic — find/replace, spell check, file I/O, word count. Does NOT import GTK. All GTK dispatch via `GLib.idle_add()`. Pattern copied from `MediaHandler`.

**Thread safety:** All GTK operations via `GLib.idle_add()`. Spell check runs synchronously on the main thread (Enchant is fast).

---

## 4. Data Flow

### 4.1 Gateway Connection Flow

```
User clicks Connect
  → window._on_connect_clicked()
    → window._connect_gateway()
      → creates AgentManager()
      → creates GatewayClient(on_connect=window._on_ws_connect, ...)
      → client.start()

Connected
  → window._on_ws_connect()
    → toolbar.update_connection_state("connected")
    → snapshot = gw.get_snapshot()
    → for each agent: agent_mgr.register(session_key, name)
    → left_panel.set_agents(names_ref, _on_agent_selected)

User clicks Disconnect
  → window._on_disconnect_gateway()
    → gw.stop()
    → toolbar.update_connection_state("disconnected")
```

### 4.2 Agent Selection Flow

```
User clicks agent row
  → left_panel._on_agent_row_activated()
    → _on_agent_selected(session_key, agent_name)
      → main_content.create_chat_tab(session_key, agent_name)
```

### 4.3 Project Group Chat — Open Project

```
User double-clicks project directory
  → FileTree._on_row_activated()
    → FileTree.on_project_opened(name, path) callback
      → window._on_project_opened(name, path)
        → _active_project_name = name
        → main_content.create_chat_tab(f"project:{name}", f"Project: {name}")
        → left_panel.refresh_agents_with_project(name)  # shows +/− buttons
        → for member_key in load_members(name): _agent_to_project.add(member_key, name)
```

### 4.3a Project Creation — System Bubble (SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS)

```
User creates project via ProjectHandler.create_project(name, path)
  → init_project_config() creates .crabcakes/ skeleton
  → init_workflow() creates workflow state
  → git init + initial commit
  → awareness refresh (build_awareness_snapshot / save_awareness_snapshot)
  → open_project(name, path) runs (tab creation, routing, opened callbacks)
  → set_on_project_created callback fires via GLib.idle_add:
    → window._on_project_created_system_bubble(name, path)
      → session_key = f"project:{name}"
      → chat_box = main_content.get_chat_box_for_session(session_key)
      → if None: create_chat_tab(session_key, "System"), re-resolve
      → if still None: return (no-op)
      → bubble = chat_render_handler.render_sync("System", <message>, session_key)
      → if bubble: chat_box.append(bubble); scroll_chat_to_bottom()
```

Message text: `New project '<name>' created. Add the Supervisor agent from the
Agents tab (click the +), then send it a message like 'I'm ready' to begin
onboarding.`

No Supervisor is auto-added. The `auto_add_to_projects` hook runs during
`open_project()` but is a no-op for all current default agents.

### 4.4 Project Group Chat — Fan-Out Send

```
User types message in project tab and clicks Send
  → ChatHandler.on_send()
    → session_key = main_content.get_current_session_key()
    → if session_key.startswith("project:"):
        project_name = session_key.split(":", 1)[1]
        members = load_members(project_name)
        for member_key in members: gw.send_message(member_key, text)
      else:
        gw.send_message(session_key, text)
    → get_chat_box().append(render_sync("You", text, session_key))
```

### 4.5 Project Group Chat — Response Routing (Phase 3)

```
Gateway sends events → ChatHandler.on_chat_event(event, payload)

  event="chat", state="delta":
    → _handle_streaming_delta(session_key, target_tab, delta_text)
      → ChatRenderHandler.start_streaming(session_key, chat_box)  # first delta only
        → build_streaming_bubble() — pending bubble with cursor (▍)
      → ChatRenderHandler.update_streaming(session_key, delta_text)
        → update label with gateway's cumulative delta text (no local accumulation)

  event="chat", state="final":
    → _handle_final_response(tab, session_key, final_text)
      → ChatRenderHandler.end_streaming(session_key)
        → remove cursor, replace with final bubble via build_role_bubble()
      → chat_box.record("Agent", final_text)
```

### 4.6 Special Event Cards — Phase 4

```
Gateway sends special events → ChatHandler.on_chat_event(event, payload)

  (non-"chat" events only)

  event in ("file_read", "edit_proposal", "tool_call", "error", "thinking"):
    → _handle_special_event(event, session_key, target_tab, payload)
      → switch_to_tab(target_tab)        # route to project or agent tab
      → chat_box = get_chat_box_for_session(target_tab)
      → ChatRenderHandler.render_event_card(event, chat_box, **fields)
        → route to factory:
            "file_read"       → create_file_card(file_path, snippet, line_range)
            "edit_proposal"   → create_edit_card(file_path, diff)
            "tool_call"       → create_tool_card(tool_name, detail)
            "error"           → create_error_bubble(error_msg)
            "thinking"        → build_role_bubble("Agent", thought_text)  # plain text
            (unknown)         → no-op (silently ignored)
```

**New widget factories** (Phase 4 additions to `ui/views/chat_bubble.py`):
- `create_file_card(file_path, snippet, line_range)` — green left border, 📄 icon
- `create_edit_card(file_path, diff)` — amber left border, ✏️ icon
- `create_tool_card(tool_name, detail)` — slate left border, 🔧 icon
- `create_error_bubble(error_msg)` — red left border + tint, ❌ icon

**New CSS classes** (`ui/styles.py`):
- `.bubble-file-read`, `.bubble-edit-proposal`, `.bubble-tool-call`
- `.bubble-error`, `.bubble-thinking`, `.bubble-streaming`

### 4.7 Forward Callback Wiring Chain

```
window._build()
  → ChatHandler.set_on_forward_message(cb)     # window provides the callback
    → ChatRenderHandler.set_on_forward_message(cb) # render handler stores it

On render_sync() call:
  → ChatHandler.render_sync(role, text, session_key, on_forward_click=self._on_forward_message)
    → ChatRenderHandler.render_sync(role, text, session_key, on_forward_click=cb)
      → build_role_bubble(role, text, on_forward_click=cb, tight=...)
        → creates Forward button → on click → cb(text, anchor_widget)

On forward click (user clicks Forward button on agent bubble):
  → bubble._on_forward_click(text, widget)
    → ChatHandler._on_forward_message(text, widget)
      → window._on_forward_message(text, widget)   # (future: popover to pick target)
```

### 4.8 Scroll-to-Bottom Button

```
User scrolls up in chat tab
  → vadjustment.value-changed → _on_vadjustment_changed()
    → if distance_from_bottom > 80px: _scroll_btn.set_opacity(1)
      else: _scroll_btn.set_opacity(0)

User clicks scroll button
  → _on_scroll_to_bottom_clicked()
    → scroll_chat_to_bottom()  # GLib deferred via idle_add
    → _scroll_btn.set_opacity(0)
```

### 4.9 Project Membership — Toggle Agent

```
User clicks +/− button on agent row
  → left_panel._on_agent_toggle_clicked(button)
    → members = load_members(active_project_name)
    → if session_key in members: members.remove(session_key)
      else: members.append(session_key)
    → _save_members(active_project_name, members)
        → preserve existing TeamMember records
        → for new special:* keys: get_special_agent(sk) → backfill display_name/role/can_write
        → for gateway/unknown keys: TeamMember(name="", role="", can_write=False)
        → save_team(path, team)
        → build_awareness_snapshot(path) + save_awareness_snapshot(path)  # refresh team_size
        → NO git commit (removed in SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS)
    → _on_project_members_changed(active_project_name, members)
      → window._on_project_members_changed(name, members)
        → rebuild _agent_to_project for this project
```

**SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS §2.6 changes:** `_save_members()` resolves `special:*` session keys through `get_special_agent()` to populate `TeamMember` metadata from the registry (display name, role, write capability). Gateway/unknown keys get blank metadata — the display layer resolves names at render time. No implicit git commit runs on roster writes; only explicit project-create and review-checkpoint commits remain. Awareness snapshot refresh after `save_team()` keeps `awareness.json` team_size current.

---

### 4.10 Activity State Machine (Phase 6)

```
Gateway events → window._on_ws_event() → ActivityHandler methods
```

**Phase routing logic (two distinct gateway event structures):**

Lifecycle events (`payload.stream == "lifecycle"`) — phase at `payload.data.phase`:
```json
{"type":"event","event":"agent","payload":{"stream":"lifecycle","data":{"phase":"end","livenessState":"working"}}}
```

Item-level events (`payload.stream == "item"`) — phase at `payload.phase`:
```json
{"type":"event","event":"agent","payload":{"stream":"item","data":{"itemId":"tool:call_function_...","phase":"end","kind":"tool"}}}
```

**Routing table in `window._on_ws_event`:**

| `event` | `payload.stream` | phase location | Handler method |
|---------|-----------------|----------------|---------------|
| `chat` | — | `payload.state` | ChatHandler routing |
| `chat` | — | `payload.state=delta` | streaming delta handler |
| `agent` | `lifecycle` | `payload.data.phase=start` | `on_agent_start()` |
| `agent` | `lifecycle` | `payload.data.phase=end` | `on_agent_end()` |
| `agent` | `lifecycle` | `payload.data.phase=error` | `on_agent_error()` |
| `agent` | `item` | `payload.phase` (kind=tool) | `on_tool_use()` |
| `agent` | `item` | `payload.phase` (kind=message) | streaming handler |


**State transitions:**
- `agent phase=start` → `reasoning`
- `agent phase=end` → `done` (auto → `idle` after 5s)
- `agent phase=error` → `idle`
- `tool_call` event → `tool_use`
- First chat delta → `streaming`
- Agent message in history → `sending` (pre-flight)

**ActivityHandler → status_target duck-type (SPEC-07 R4; production impl: ActivityPillAdapter):**
  set_status_text(text, state)         → pill label text + CSS class (plain text; no markup)

status_target → chat surface:
  set_activity_status(text, state)     → Gtk.Label set_text + pill-<state> CSS class swap
                                        (progress quartet retained as no-ops for duck-type parity)
```

### 4.11 Agent-to-Agent (A2A) Consultation — Command-Based (Phase 6.1)

Agent-to-agent consultation is entirely command-based. There are no automatic @mention
detectors, no relay threads, no convergence loops, and no capture_response cycles.

**The only mechanism:** `/ask @AgentName "question"` — typed by a human or emitted
by an agent in its response text.

**Commands** (see §3.21d for CollabHandler):
| Command | Effect |
|---------|--------|
| `/ask @Agent "question"` | Forward question to target agent; response appears in same tab |
| `` `delegate @Agent "task"` `` | Assign a task to an agent |
| `` `stop @Agent` `` | Send stop signal to a collaboration |
| `` `tell @Agent "info"` `` | Share information with an agent |

**Data flow — `` `ask @Coder "is this edge case valid?"` `` in a project tab:**
```
User types `ask @Coder "is this edge case valid?"`
         │
         ▼
ChatHandler.on_send() → CommandHandler.process_input()
         │
         ▼
CommandHandler resolves @Coder → Coder's session_key (via AgentManager + _special_agents)
         │
         ▼
CollabHandler.cmd_ask() → CommandResult(forward_to=coder_sk, forward_text=question)
         │
         ▼
ChatHandler echoes "→ @Coder: is this edge case valid?" in project tab
         │
         ▼
gw.send_message(coder_sk, "is this edge case valid?")
         │
         ▼
Coder responds → response appears in project tab
```

**Special agents (Coder, Debugger):** Forwarded via `AgentRuntimeHandler.send_to_special_agent()`
when the target is a registered special agent. ChatHandler checks `agent_runtime_handler.get_special_agents()`
before routing via gateway.

**What was removed (Phase 6.1):**
- CollabManager — automatic @mention detection, relay thread engine, response capture, convergence detection
- `[A2A relay from ...]` prefix handling in both ChatHandler and AgentRuntimeHandler
- `is_pending_relay()` / `capture_response()` / `start_relay()` / `detect_a2a_mention()` throughout
- §3.21n (CollabManager module) and the old §4.11 A2A data flow

**Architecture rule:** Agents that need another agent's input include a slash-prefixed command
in their response text. The slash command is parsed by CommandHandler like any other
human input — no special casing, no detection, no loops. Agent-initiated parsing is
handled by `AgentCommandHandler` (Phase 6.2) which hooks into the response pipeline
and routes commands through CommandHandler.process_input().

### 4.12 MCP Tool Execution Flow (MCP Phase B)

**When:** An agent with `mcp_servers: ["memory", "fetch"]` is loaded and a tool call arrives.

**Data flow:**
```
Agent YAML definition (mcp_servers: ["memory", "fetch"])
         │
         ▼
SpecialAgentDef(conv_id_prefix, ..., mcp_servers=["memory", "fetch"])
         │
         ▼
AgentRuntimeHandler.send_to_special_agent() → create_conversation(session_key, mcp_servers=["memory", "fetch"])
         │
         ▼
Conversation.mcp_servers = ["memory", "fetch"] (stored in Conversation dataclass)
         │
         ▼
AgentRuntime.create_run_for(conv) → get_tool_definitions_for_api(conv)
         │
         ▼
Tools.get_tool_definitions_for_api() → get_tools_for_api(mcp_servers, session_key)
         │
         ▼
utils/mcp_client.py:
    for each server in mcp_servers:
        if not is_connected(session_key, server): connect(server, session_key)
        discover_tools(server, session_key)  # calls session.list_tools(), caches result
        convert to OpenAI format: name = "server_name/tool_name"
         │
         ▼
Merge MCP tools + built-in tools → agent sees unified tool list
         │
         ▼
LLM requests tool call: "memory/search_nodes" with {"query": "test"}
         │
         ▼
AgentRuntime.on_tool_call() → execute_tool("memory/search_nodes", {"query": "test"}, project_path, session_key)
         │
         ▼
agent/tools.py execute_tool():
    if "/" in tool_name:  # MCP tool routing
        server_name, tool_name = tool_name.split("/", 1)
        result = call_tool(server_name, tool_name, arguments, session_key)
    else:
        # Built-in tool routing (read_file, write_file, etc.)
```

**Namespacing rule:** All MCP tool names are prefixed with `server_name/` using the `server_name/tool_name` convention. Built-in tools have no slash. This allows unambiguous routing.

**Cleanup on conversation end:** `create_conversation(session_key)` with same key → `mcp_disconnect_all(session_key)` to kill stale MCP subprocesses.

**Graceful degradation:** If MCP server fails (not found, can't spawn, network timeout), `execute_tool` returns `ToolResult(success=False, error=...)` and logs warning. Built-in tools still work.

### 4.13 Provider Change → Open Agent Builder Refresh

User adds a provider in Settings while the Agent Builder is open
  → SettingsDialog._on_add_provider()
    → handler.add_or_update(ProviderConfig)
      → handler._on_providers_changed(providers)  (if handler is the SettingsHandler)
        → wire_settings_handler._on_providers_changed(providers)  (closure)
          → settings_dialog_factory() → None (or settings dialog) → refresh_providers(providers)
          → agent_builder_factory() → AgentBuilderDialog or None
            → dialog.set_provider_options(providers)  (rebuilds dropdown)

User removes a provider in Settings while the Agent Builder is open (with that provider selected)
  → same chain as above
    → agent_builder._providers updates
      → _rebuild_provider_dropdown() runs
        → if no providers left: dropdown shows "(no providers — open Settings)"
        → if providers remain: dropdown shows remaining names
        → _get_selected_provider_id() returns "" if the selected index is now invalid
          → _update_save_button() disables Save

### 4.14 Feed Persistence — Background Writer, Journal, Compaction (SPEC-UI-RESPONSIVENESS-2)

The feed write path is asynchronous: no feed disk I/O ever runs on the GTK main thread.

```
Tool result (runtime thread)
  → _on_tool_call_result → GLib.idle_add → _do_tool_call_result [main]
    → feed_handler.update_card [main]
        mutate self._cards under _lock; rebuild/replace widget     [main, ~ms]
        _enqueue_card_update(project_path, card_id, {body, metadata})  [main, <1 ms, no disk]
  → crabcakes-feed-writer thread [background]
    → feed_store.update_feed_card
        append_card_update → 1 JSONL line (feed flock held briefly)   [O(1)]
        if journal ≥ 500 lines → compact_feed(window=2000)             [no lock held here]
            fold (shared _parse_cards/_apply_overlay) → prune (window + pins)
            → atomic compact snapshot write → truncate journal        [ONE feed-flock hold]

Project open [main: on_project_opened]
  → _load_and_render [daemon thread]
      load_feed: snapshot + journal under one feed-flock hold (size-scaled timeout)
      if len(cards) > 2500 (= FEED_WINDOW_DEFAULT * 1.25) → _enqueue_compaction  [writer runs it]

Compaction pruned >0 → feed_handler._surface_prune_card [writer thread]
  → GLib.idle_add → _ui [main]: active-project guard → add_card(persist=False) + deepcopy
      → tiny daemon thread: append_feed_card(copy) → rate-limited _maybe_compact
```

Accept/reject clicks follow the same update queue. `add_card` / `add_cards_batch` persist via their own per-card threads → `append_feed_card`, and a new card arriving during a project load persists through the writer. **Multiple threads call `append_feed_card` (per-card persist threads, batch thread, prune persist thread) — the feed flock serializes them; that is the concurrency contract, not "a single writer thread."**

## 5. Callback Pattern

**Primary pattern for all component communication.** A callback is a function reference passed to a component at construction time or via a setter. The component calls it when something happens. The component does NOT know what happens after.

**Rules:**
- Callbacks are always passed, never hardcoded.
- A component never imports another component's module for the purpose of calling it.
- Callbacks use the component's internal state only — no access to widgets outside the component.

**Setter pattern** (for data available after construction):
```python
panel.set_agents(agent_names, on_agent_selected)
panel.set_on_project_opened(cb)
panel.refresh_agents_with_project(name)
```

**Constructor pattern** (for data available at construction):
```python
toolbar = Toolbar(on_connect_clicked=self._on_connect_clicked)
file_tree = FileTree(on_file_selected=self._on_project_selected)
```

---

## 6. Naming Conventions

### 6.1 Variables and Functions

| Pattern | Example | Usage |
|---------|---------|-------|
| `snake_case` | `user_input`, `on_prompt_selected` | Variables, functions, methods |
| `_camelCase` | `_on_connect_clicked` | Private methods (single underscore prefix) |
| `ALL_CAPS` | `GATEWAY_URL`, `PROJECTS_DIR` | Module-level constants |
| `_ALL_CAPS` | `_IDENTITY_CACHE` | Module-level private constants |

### 6.2 Classes

| Pattern | Example | Usage |
|---------|---------|-------|
| `PascalCase` | `GatewayClient`, `LeftPanel` | Class names |
| `_PascalCase` | `_build_agents_list` | Private class methods |

### 6.3 GTK Widgets

| Pattern | Example | Usage |
|---------|---------|-------|
| `_widget_name` | `_user_input`, `_send_btn` | Instance-level widget references (via properties) |
| Local vars | `scroll`, `list_box`, `row` | Local variables in methods |

### 6.4 Files

| Pattern | Example | Usage |
|---------|---------|-------|
| `snake_case.py` | `left_panel.py`, `file_tree.py` | All Python files |

---

## 7. GTK4 Specific Patterns

### 7.1 Import Pattern

Every file that uses GTK must call `gi.require_version()` **before** importing Gtk:

```python
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
```

### 7.2 Widget Construction

```python
# Correct — pass kwargs to __init__
label = Gtk.Label(label="Hello", xalign=0)

# Correct — use setter methods
label.set_margin_top(8)
label.set_margin_start(8)
```

### 7.3 ListBox Row Activation

GTK4 `Gtk.ListBox` fires `row_activated` on **single click** by default. To require **double-click**:

```python
list_box.set_activate_on_single_click(False)
```

### 7.4 TreeView / TreeStore

`Gtk.TreeView` + `Gtk.TreeStore` is used for the Projects directory browser (`FileTree`). Pattern:

```python
# Column setup
renderer = Gtk.CellRendererText()
column = Gtk.TreeViewColumn(title, renderer, text=column_index)
tree_view.append_column(column)

# Store structure: (name, full_path, is_dir, is_expanded)
# Use None as parent to append top-level rows
self._store.append(None, ['item_name', '/path', True, True])
# Use iter as parent for children
self._store.append(parent_iter, ['subitem', '/path/sub', True, True])
```

### 7.5 Paned (Resizable Split)

```python
paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
paned.set_start_child(top_widget)
paned.set_end_child(bottom_widget)
paned.set_resize_start_child(True)
paned.set_resize_end_child(True)
paned.set_shrink_start_child(False)
paned.set_shrink_end_child(False)
paned.set_position(400)
```

### 7.6 Thread Safety

**All GTK operations must happen on the main thread.** Gateway client runs in a background thread. To safely call GTK from the gateway thread:

```python
from gi.repository import GLib
GLib.idle_add(callback_function, *args)
```

`GLib.idle_add()` schedules the callback to run on the next GTK idle cycle.

---

## 8. Rules for Adding New Code

### 8.1 Before Writing Any Code

1. **Identify ownership:** Which module owns this data or behavior?
2. **Find existing patterns:** Has something similar been done? Copy the pattern.
3. **Determine the right module:** Does it belong in `models/`, `utils/`, `ui/views/`, or a new module?
4. **Trace the wiring:** Where will this be called from? Verify the call path exists.

### 8.2 Adding a New UI Component

1. Create a new file in `ui/views/`
2. Component accepts callbacks at construction or via setters
3. Component **never** imports other UI components directly
4. Component exposes widget references via properties when needed
5. Update `ui/window.py` to create and wire the component

### 8.3 Adding a New Model

1. Create a new file in `models/`
2. Export it from `models/__init__.py`
3. Model is a plain Python class — no GTK imports
4. Document the class in this document

### 8.4 Adding a Utility

1. Add to an existing utility file in `utils/` if related, or create a new file
2. Keep utilities **stateless** when possible
3. If a utility needs to be stateful, it probably belongs in `models/`

### 8.5 Testing

Tests live in `tests/` — one file per module being tested.

**Run tests:**
```bash
cd /path/to/projects/crabcakes
pytest              # auto-discovers tests/ via pytest.ini
```

**Test coverage (descriptions for key tests — see Section 12 for full inventory):**
- `tests/test_architecture.py` — AST guard: handler isolation, models/gateway layer separation, public API existence
- `tests/test_favorites.py` — favorites persistence: missing file, empty list, round-trip, JSON corruption
- `tests/test_prompts_handler.py` — PromptsHandler: search/filter, favorites sort, last-used timestamps
- `tests/test_left_panel.py` — LeftPanel: right-click copy menu (gesture wiring, clipboard, status label), 8 tests
- `tests/test_agent_list_handler.py` — AgentListHandler: initials, colors, sorting, callbacks
- `tests/test_agents.py` — AgentManager: edge cases, unknown inputs, clear/reregister
- `tests/test_chat_handler.py` — ChatHandler: send, fan-out, routing, tab switching
- `tests/test_gateway_handler.py` — GatewayHandler: connect lifecycle, thread safety, GTK dispatch
- `tests/test_media_handler.py` — MediaHandler: STT toggle, improve API, GLib dispatch
- `tests/test_projects.py` — file I/O: missing files, empty dirs, JSON corruption, round-trip
- `tests/test_improve.py` — API calls: missing key, HTTP errors, malformed responses
- `tests/test_icons.py` — icons.py: import smoke test
- `tests/test_activity_bubbles.py` — ActivityHandler state transitions + activity bubble rendering
- `tests/test_agent_command_handler.py` — AgentCommandHandler: relay, command extraction, chain depth
- `tests/test_agent_defs.py` — agent_defs: load, validate, save, default seeding
- `tests/test_agent_runtime.py` — AgentRuntime: conversation lifecycle, tool loop, streaming
- `tests/test_auxilium_tier1.py` — Auxilium Tier 1 first-run wizard: handler imports, state machine
- `tests/test_auxilium_tier2.py` — Auxilium Tier 2 KB synthesis: agent_role field, kb_lookup gate, KB context injection, multi-turn, handler wiring
- `tests/test_audit_parser.py` — audit_parser: extract AuditReport from agent messages
- `tests/test_block_parser.py` — block_parser: extract_blocks() segment classification
- `tests/test_bug_fixes.py` — regression tests for specific fixed bugs
- `tests/test_chat_render_handler.py` — ChatRenderHandler: escape, markdown, highlight pipeline
- `tests/test_command_handler.py` — CommandHandler: slash parsing, @mention resolution, dispatch
- `tests/test_command_models.py` — Command, CommandResult, CommandRegistry data models
- `tests/test_config.py` — agent config loading, provider resolution
- `tests/test_context.py` — system prompt builder, file context, .gitignore parsing
- `tests/test_conversation.py` — Conversation, Message, ToolCall data models + token estimation
- `tests/test_crabcard_parser.py` — crabcard block extraction from agent messages
- `tests/test_crabwatch_handler.py` — CrabWatchHandler: filesystem event debouncing
- `tests/test_create_project.py` — project creation workflow
- `tests/test_diff_parser.py` — unified diff parsing → FileDiff/ParsedDiff
- `tests/test_enforcement.py` — enforcement tier execution: syntax, tests, lint
- `tests/test_escaping.py` — escape_for_pango(), xml_escape_text()
- `tests/test_feed_card.py` — FeedCardData dataclass + css_class_for_type()
- `tests/test_feed_handler.py` — FeedHandler: card lifecycle, echo suppression, background persist writer, compaction trigger + prune surfacing
- `tests/test_feed_store.py` — feed JSON + journal persistence: load/save/append/update, journal replay, bounded lock, window pruning + pins
- `tests/test_git_ops.py` — git operations: stage, commit, diff, checkout
- `tests/test_mcp_client.py` — MCP client: asyncio bridge, connection pooling, tool discovery
- `tests/test_mcp_config.py` — MCP server config loading + validation
- `tests/test_mcp_integration.py` — end-to-end MCP tool execution flow
- `tests/test_missing_message_fix.py` — regression test for missing message recovery
- `tests/test_phase4.py` — Phase 4 event card rendering
- `tests/test_project_awareness.py` — project awareness system: .crabcakes/ management
- `tests/test_project_handler.py` — ProjectHandler: tab lifecycle, membership routing
- `tests/test_project_list_handler.py` — ProjectListHandler: project card data
- `tests/test_project_search.py` — project directory search
- `tests/test_prompt_loader.py` — prompt template loading + composition
- `tests/test_quoting.py` — quoted-payload parsing with escapes
- `tests/test_review_log.py` — review log persistence
- `tests/test_review_state.py` — ReviewState dataclass
- `tests/test_routing.py` — AgentRoutingTable: add/remove/lookup
- `tests/test_special_agents.py` — SpecialAgentDef loading, auto-open, auto-add
- `tests/test_streaming.py` — StreamingBubble dataclass
- `tests/test_syntax_highlight.py` — Pygments → Pango highlighter
- `tests/test_task_redesign_prompts.py` — static-content checks for cc-spec-planning/redirect/workflow-guide/commands (SPEC-TASK-SYSTEM-FULL-REDESIGN §10)
- `tests/test_tasks.py` — TaskStore CRUD + status transitions (tests the deprecated TaskStore; still passes)
- `tests/test_tools.py` — tool execution: sandbox, blocklist, file ops
- `tests/test_work_handler.py` — WorkHandler: /work commands, lifecycle, authorization, persistence
- `tests/test_work_persistence.py` — work.json source of truth + generated tasks.md + legacy migration
- `tests/test_work_unit.py` — WorkUnitStore CRUD + status/priority + dependency validation

**Writing new tests:** aim to break the code, not confirm it works. Test:
- Unknown/missing inputs → what does the code do?
- Empty collections → does it return [] or crash?
- Corrupt data → does it fail gracefully?
- Type errors → does a dict arrive where a string was expected?

**Mock pattern:** use `unittest.mock.patch` to intercept network calls (`urllib.request.urlopen`) or config loading. Pass `GLib=None` to `improve_prompt` to call the callback synchronally in tests.

### 8.6 Handler Pattern — MANDATORY for All New Code

**All new UI logic must follow the handler pattern. No exceptions.**

The `ui/handlers/` directory contains self-contained logic modules extracted from `window.py`. This is not a suggestion — it is the architectural law. Every piece of new behavior must live in a handler, not in `window.py`.

**What goes in a handler:**
- Any logic that responds to user actions (clicks, input, gestures)
- Any logic that processes gateway events, STT results, or external callbacks
- Any logic that coordinates between models, gateway, and UI views
- Any logic involving state transitions (connecting, recording, sending)

**What stays in `window.py`:**
- Creating handler instances and passing them references to shared objects
- Wiring callbacks between handlers (e.g., MediaHandler's STT result → ChatHandler's send)
- Top-level GTK signal connections that delegate immediately to a handler method

**Handler rules:**
1. **One handler per subsystem.** Chat logic → `ChatHandler`. Gateway lifecycle → `GatewayHandler`. Media I/O → `MediaHandler`. New subsystem → new handler file.
2. **Handlers never import other handlers.** If ChatHandler needs something from GatewayHandler, `window.py` wires it via a callback or sync function.
3. **Handlers receive dependencies via constructor or setters.** They do not reach out to find them.
4. **All GTK calls from background threads go through `GLib.idle_add()`.** Every handler docstring must note this.
5. **Handlers own their state.** If a handler needs persistent state, it holds it internally. It does not store state on `window.py`.
6. **Tests go in `tests/test_<handler_name>.py`.** Every handler must have tests. See Section 8.5.

**Adding a new handler — checklist:**
1. Create `ui/handlers/<subsystem>_handler.py`
2. Define the class with constructor accepting needed callbacks/references
3. Add a docstring explaining responsibilities and thread safety
4. Implement logic, dispatching GTK calls via `GLib.idle_add()` where needed
5. Create `tests/test_<subsystem>_handler.py` with full test coverage
6. Wire the handler in `window.py` (`_build()` method)
7. Update this ARCHITECTURE.md (Section 3, Section 11, this section)

**Why this matters:**
`window.py` was once a 2,300-line monolith. The handler pattern exists to prevent that from happening again. Every line of logic added directly to `window.py` is technical debt. Extract it into a handler instead.

### 8.7 Anti-Patterns

| Anti-pattern | Correct approach |
|-------------|-----------------|
| Creating new globals instead of using models | Put state in `models/` |
| Parsing strings to extract data | Use the API that owns the data |
| Importing UI modules in gateway code | Keep layers separate |
| Large monolithic files | Split at natural boundaries |
| Writing code without wiring it | Every piece of code must be called somewhere |
| Skipping verification | Compile + test every checkpoint |
| Adding logic directly to `window.py` | Extract into a handler in `ui/handlers/` |
| A handler importing another handler | Use callbacks wired through `window.py` |
| New UI behavior without a handler file | Create one first, then implement |

---

## 9. CSS and Styling

### 9.1 Single Source of Truth

All CSS for the application lives in **`ui/styles.py`**. No other file may define CSS.

- `ui/styles.py` contains a single `APP_CSS` string constant with all style rules
- `apply_styles()` registers the CSS provider once at app startup
- This function is called from `main.py` before the window is created

### 9.2 Rule: Views Add Classes, Never Define Them

Views use `widget.add_css_class("class-name")` to apply styles. They **never** call `Gtk.CssProvider().load_from_data()` or define CSS inline.

```python
# ✅ Correct — view applies a class name
self._send_button.add_css_class("suggested-action")

# ❌ Wrong — view defines its own CSS
provider = Gtk.CssProvider()
provider.load_from_data(b".my-button { background: red; }")
```

The reasoning: a view decides *what* something is ("this is a send button"), not *how it looks* ("indigo with 6px border-radius"). Appearance belongs in one place.

### 9.3 Naming Conventions for CSS Classes

| Pattern | Example | Usage |
|---------|---------|-------|
| `component-element` | `agent-row`, `code-block-header` | Widget-specific styles |
| `chat-bubble-*` | `.chat-bubble-you`, `.chat-bubble-agent` | Chat message bubbles (Phase 1) |
| `chat-role-*` | `.chat-role-label` | Role label inside bubbles |
| `chat-msg-*` | `.chat-msg-label` | Message content inside bubbles |
| `chat-bubble-pending` | `.chat-bubble-pending` | Optimistic UI state (semi-transparent, Phase 1) |
| `component-element-state` | `agent-row:hover`, `agent-add-btn:hover` | Pseudo-states (in CSS, not the class name) |
| `component-element-variant` | `agent-avatar-3`, `lang-python` | Numbered or named variants |
| `semantic-role` | `suggested-action`, `destructive-action` | Reusable semantic roles (GTK convention) |
| `flat` | `flat` | Ghost/transparent button style |

### 9.4 Adding New CSS

1. Add the CSS rule to the `APP_CSS` constant in `ui/styles.py`
2. Add the corresponding `add_css_class()` call in the view that uses it
3. Document the class name in a comment above the CSS rule
4. If it's a new color, check `models/colors.py` — don't hardcode new palette entries in CSS

### 9.5 CSS Architecture Anti-Patterns

| Anti-pattern | Correct approach |
|-------------|-----------------|
| Inline `load_from_data()` in a view file | Add CSS to `ui/styles.py` instead |
| Multiple CSS providers across the app | One provider, applied once globally |
| Hardcoded hex colors in Python code | Use CSS classes or `models/colors.py` |
| View files containing CSS strings | All CSS in `ui/styles.py` |
| `set_background_color()` or `override_background_color()` | Use CSS classes instead |

### 9.6 Color Palette

The app uses a dark theme. Core colors defined in CSS:

| Role | Color | Variable |
|------|-------|----------|
| Background | `#1a1a20` | `CLR_BG` |
| Panel | `#2a2a35` | `CLR_PANEL` |
| Text | `#e8e8ec` | `CLR_TEXT` |
| Muted text | `#6b6b7a` | `CLR_MUTED` |
| Accent | `#6366f1` | `CLR_ACCENT` |
| Success | `#10b981` / `#6ee7b7` | green tones |
| Danger | `#f43f5e` / `#fda4af` | red tones |
| Warning | `#f59e0b` | amber |

Agent and project avatars use a 10-color round-robin palette defined in `models/colors.py`.

---

## 10. Environment Variables

| Variable | Default | Purpose |
|---------|---------|---------|
| `CRABCAKES_DEBUG` | (unset) | Set to `1` to enable `logging.DEBUG` for all modules — agent runtime, handlers, utilities |
| `CRABCAKES_GATEWAY_DEBUG` | (unset) | Set to `1` to enable raw WebSocket message dump in `gateway/client.py` — independent of `CRABCAKES_DEBUG` |
| `CRABCAKES_GATEWAY_URL` | `ws://localhost:18789` | OpenClaw gateway WebSocket URL |
| `CRABCAKES_PROJECTS_DIR` | `~/projects` | Directory containing project folders for the Projects tab |
| `STT_MODEL_SIZE` | `tiny.en` | faster-whisper model size — "tiny.en", "base.en", "small.en", "medium.en", etc. Respects env var or explicit param; defaults to "tiny.en". |

**External binaries required for STT:**
- `arecord` — ALSA audio capture (part of alsa-utils)
- `faster-whisper` Python package (already installed; downloads tiny.en model on first use)
- Model: `Systran/faster-whisper-tiny.en` (~75MB, HuggingFace cache)

---

## 11. Gateway Protocol Reference

**Events arrive as `(event_name, payload_dict)` tuples via `on_event` callback in `GatewayClient`.**

`window._on_ws_event` handles two distinct event structures:

**Lifecycle events** (`payload.stream == "lifecycle"`) — phase at `payload.data.phase`:
```json
{"type":"event","event":"agent","payload":{"stream":"lifecycle","data":{"phase":"end","livenessState":"working","endedAt":1776790638851}}}
```

**Item-level events** (`payload.stream == "item"`) — phase at `payload.phase`:
```json
{"type":"event","event":"agent","payload":{"stream":"item","data":{"itemId":"tool:call_function_...","phase":"end","kind":"tool"}}}
```

**Routing table:**

| event | stream | phase location | Meaning |
|-------|---------|----------------|---------|
| `"chat"` | — | `payload.state=final` | Complete response — route to ChatHandler |
| `"chat"` | — | `payload.state=delta` | Streaming delta — accumulate in bubble |
| `"agent"` | `"lifecycle"` | `payload.data.phase=start` | Agent started reasoning |
| `"agent"` | `"lifecycle"` | `payload.data.phase=end` | Agent finished — trigger idle |
| `"agent"` | `"lifecycle"` | `payload.data.phase=error` | Agent error — reset |
| `"agent"` | `"item"` | `payload.phase` (kind=`tool`) | Tool call complete |
| `"agent"` | `"item"` | `payload.phase` (kind=`message`) | Message delta |

**Phase routing logic:**
```python
stream = payload.get("stream", "")
if stream == "lifecycle":
    phase = payload.get("data", {}).get("phase", "")
else:
    phase = payload.get("phase", "")
```

**Snapshot structure (`get_snapshot()`):**
```python
{
  "health": {
    "agents": [
      {
        "agentId": "qat",
        "name": "Qat",
        "sessions": {
          "recent": [
            {"key": "agent:qat:main", "lastActive": 1234567890}
          ]
        }
      }
    ]
  }
}
```

**Project membership storage:**
- Path: `~/.config/crabcakes/projects/<project-name>/members.json`
- Format: `["agent:qat:main", "agent:qtr:telegram:direct:7478874934", ...]`
- Each entry is a session key string

---

## 12. Provider Resolution & API Caller

As of PHASE-10, the API caller for a provider is resolved via `provider_cfg.caller`, a per-provider attribute persisted in `providers.yaml`. The runtime's `_resolve_caller_key(provider_cfg, model)` helper returns the explicit `caller` if set, otherwise derives it from `provider_cfg.default_model.split("/")[0]`, and finally falls back to `model.split("/")[0]`. This decouples the model-string prefix structure (which is the API's contract — e.g. `openrouter/owl-alpha` for OpenRouter) from the caller's identity (which is one of the five built-in implementations: `openai`, `minimax`, `anthropic`, `openrouter`, `zai`).

**Resolution priority** (highest to lowest):
1. `provider_cfg.caller` (explicit, lowercased)
2. `provider_cfg.default_model.split("/")[0]` (derivation from configured model)
3. `model.split("/")[0]` (legacy fallback for callers without a `ProviderConfig`)

**Why explicit caller + derivation:** existing providers in `providers.yaml` (pre-PHASE-10) don't have a `caller` field. The derivation fallback (`default_model.split("/")[0]`) handles migration transparently — all 6 of the user's existing providers have slashed `default_model` values, so the runtime resolves the correct caller without requiring a re-save. For providers without slashed `default_model` (e.g. `local-kb`), the explicit `caller` field is required — `_to_llm_provider()` copies it from `ProviderConfig` to `LLMProviderConfig` during config loading.

**Why the model string is still slashed:** the API caller functions receive the model string verbatim. OpenRouter expects `vendor/model` (e.g. `openrouter/owl-alpha`); Anthropic expects a bare model name (e.g. `claude-3-5-sonnet`); OpenAI expects a bare model name. The slash in the model string is the API's contract, not a caller identifier. The runtime's `_resolve_agent_model` handler (P4) preserves the model string exactly as configured when `default_model` contains a slash.

**Streamer resolution:** the streaming path (`_call_llm_streaming` callers) uses the same `_resolve_caller_key` helper to look up the streamer via `get_provider(caller_key).stream`. Providers with `supports_streaming=False` (e.g. `local-kb`) always use the blocking path, even when `on_text_delta` is registered. **Tool-call id handling:** OpenAI/MiniMax/OpenRouter/ZAI all surface the provider-assigned id in the first SSE `tool_call_delta`; the streamer forwards it to the accumulator's first-write-wins slot. Anthropic's protocol differs — the id arrives in the `content_block_start` event (not in `content_block_delta`) — so `AnthropicProvider().stream` yields a `tool_call_delta` carrying only the id at block-start, then the name/argument deltas follow. In all cases, the final assembled `tool_call` round-trips the provider's id back to the API on the next LLM turn.

**Test Connection:** the Settings dialog's "Test" button calls `test_connection(base_url, api_key, model, caller=provider.caller)`. The `caller` kwarg (added in PHASE-10) overrides the legacy model-prefix derivation so the test uses the same caller the runtime would use at message-send time.

---

## 13. File Inventory

> **Line counts verified as of commit `8902696` (2026-06-27).**
> Counts drift over time — treat `~` values as approximate snapshots.

```
crabcakes/
├── main.py                         # 56 lines — bootstrap only
│
├── gateway/
│   ├── __init__.py                # 6 lines — exports GatewayClient, SnapshotValidationError
│   └── client.py                 # ~644 lines — GatewayClient (threaded WebSocket + v3 device auth)
│
├── models/
│   ├── __init__.py               # 69 lines — exports 28+ symbols
│   ├── activity.py               # 250 lines — ActivityBubble dataclass
│   ├── agents.py                 # 60 lines — AgentManager
│   ├── colors.py                 # 83 lines — AGENT_COLORS palette + round-robin
│   ├── command.py                # 171 lines — Command, CommandResult, CommandRegistry (Phase 7)
│   ├── conversation.py           # 437 lines — MessageRole, ToolCall, Message, Conversation + summary-on-trim + token breakdown
│   ├── conversation_snapshot.py  # 83 lines — ConversationSnapshot, SnapshotMessage
│   ├── feed_card.py              # 198 lines — FeedCardData + css_class_for_type() (Phase 5)
│   ├── providers.py              # 54 lines — ProviderConfig + caller_default_max_tokens()
│   ├── review_state.py           # 26 lines — ReviewState dataclass (Phase 7)
│   ├── routing.py                # 41 lines — AgentRoutingTable (session_key → project_name)
│   ├── streaming.py              # 30 lines — StreamingBubble dataclass (Phase 5)
│   ├── task.py                   # 105 lines — Task + TaskStore + labels (Phase 3) — DEPRECATED, superseded by work_unit.py
│   ├── work_unit.py              # 233 lines — WorkUnit + WorkUnitStore + status/priority labels (replaces task.py as primary)
│   └── team.py                   # 103 lines — TeamMember, ProjectTeam
│
├── agent/
│   ├── __init__.py               # 76 lines — exports: AgentRuntime, AgentConfig, EnforcementConfig, etc.
│   ├── config.py                 # ~319 lines — LLMProviderConfig, EnforcementConfig, AgentConfig, load_agent_config()
│   ├── context.py                # ~541 lines — build_system_prompt, build_file_context, .gitignore parsing
│   ├── context_strategy.py       # ~598 lines — Pluggable context compaction strategy (Phase 4–9)
│   ├── enforcement.py            # ~882 lines — Post-write verification: 3-tier checks + per-project override
│   ├── kb_lookup.py              # ~279 lines — KB cosine-sim retrieval (Auxilium Tier 1)
│   ├── kb_server.py              # ~457 lines — KB HTTP server on localhost:18790
│   ├── runtime.py                # ~1995 lines — AgentRuntime: tool loop, streaming, cost, compaction (cost/LLM/SSE/extractors/audit/persistence extracted)
│   ├── tool_middleware.py        # ~244 lines — Tool middleware chain: enforcement + stuck detection (Phase A1)
│   ├── special_agents.py         # ~172 lines — SpecialAgentDef, get_special_agents(), reload_registry()
│   ├── tools.py                  # ~1108 lines — 8+ tools: read_file, write_file, edit_file, exec_command, etc.
│   └── llm/                      # LLM provider abstraction package (Phase B1–B6)
│       ├── __init__.py           # Public API: get_provider, list_providers, LLMProvider, LLMResponse
│       ├── protocol.py           # LLMProvider Protocol (@runtime_checkable) + LLMResponse dataclass
│       ├── registry.py           # Provider registry: get_provider(id) -> LLMProvider
│       ├── openai_provider.py    # OpenAIProvider — handles openai, openrouter, zai (call + stream)
│       ├── minimax_provider.py   # MiniMaxProvider — ChatCompletion v2 (call + stream)
│       ├── anthropic_provider.py # AnthropicProvider — Messages API (call + stream)
│       ├── streaming.py          # SSE helpers + SSL retry infrastructure
│       ├── extractors.py         # Response extractors: tool_calls, text_content, usage
│       ├── convert.py            # Anthropic message/tool format converters
│       └── cost.py               # Cost tables + model_id + cost_for_model
│
├── scripts/
│   └── rebuild_kb_index.py       # offline indexer — builds knowledge/.index/ from knowledge/*.md
│
├── ui/
│   ├── __init__.py               # 1 line
│   ├── constants.py              # 16 lines — cross-cutting UI constants (STREAMING_ENABLED)
│   ├── toolbar.py                # ~142 lines — Toolbar widget (connect button + status label)
│   ├── styles.py                 # ~1245 lines — APP_CSS constant + apply_styles()
│   ├── window.py                 # ~957 lines — MainWindow — assembles all components, wires callbacks
│   ├── wiring.py                 # ~128 lines — settings callback wiring + LOW-7 project path helpers
│   ├── handlers/
│   │   ├── __init__.py           # 0 lines — package marker
│   │   ├── activity_handler.py   # ~767 lines — 6-state activity machine + two-phase progress (Phase 6)
│   │   ├── agent_builder_handler.py # ~221 lines — AgentBuilderHandler (Phase 5)
│   │   ├── agent_command_handler.py # ~578 lines — agent response command parser + relay (Phase 6.2)
│   │   ├── agent_list_handler.py # ~145 lines — agent card data
│   │   ├── agent_runtime_handler.py # ~1065 lines — AgentRuntime UI bridge (Phase 5)
│   │   ├── chat_handler.py       # ~810 lines — send, fan-out, routing
│   │   ├── chat_render_handler.py # ~729 lines — escape + markdown + highlight + bubble pipeline
│   │   ├── collab_handler.py     # ~65 lines — collaboration commands (Phase 7)
│   │   ├── command_handler.py    # ~623 lines — slash-prefix command parser (Phase 7)
│   │   ├── connection_sync_handler.py # ~234 lines — post-connect wiring (Phase 3a)
│   │   ├── crabwatch_handler.py  # ~364 lines — CrabWatchHandler filesystem watcher (Phase 5)
│   │   ├── feed_handler.py       # ~2276 lines — FeedHandler — feed card lifecycle, background persist writer, compaction + prune surfacing (SPEC-UI-RESPONSIVENESS-2 P1/P3)
│   │   ├── forward_handler.py    # ~194 lines — ForwardHandler (Phase 3b)
│   │   ├── gateway_handler.py    # ~234 lines — connect, agents, lifecycle (Phase 2)
│   │   ├── input_toolbar_handler.py # ~485 lines — find/replace, spell check, word count
│   │   ├── media_handler.py      # ~99 lines — STT + improve (Phase 4)
│   │   ├── project_handler.py    # ~568 lines — active project + routing
│   │   ├── project_list_handler.py # ~86 lines — project card data
│   │   ├── prompts_handler.py    # ~208 lines — favorites, search, last-used
│   │   ├── review_handler.py     # ~523 lines — review session lifecycle (Phase 7)
│   │   ├── session_handler.py    # ~164 lines — session switching (Phase 7)
│   │   ├── settings_handler.py   # ~231 lines — settings dialog logic (provider CRUD, test connection)
│   │   ├── work_handler.py       # ~1046 lines — WorkHandler /work commands + Supervisor handoff (replaces task_handler.py)
│   │   ├── auxilium_wizard_handler.py # ~449 lines — Auxilium wizard handler (Tier 1, D7)
│   │   └── file_tree_handler.py  # ~93 lines — FileTree sort prefs + git status cache (no GTK)
│   └── views/
│       ├── __init__.py           # 1 line
│       ├── activity_drawer.py    # ~764 lines — collapsible activity event panel
│       ├── agent_builder.py      # ~770 lines — AgentBuilderDialog
│       ├── auxilium_wizard.py    # ~452 lines — Auxilium wizard view (Tier 1, D7)
│       ├── chat_bubble.py        # ~1059 lines — build_role_bubble() factory (Phase 1 + 2)
│       ├── chat_input_toolbar.py # ~588 lines — find/replace bar + spell check (view only)
│       ├── diff_card.py          # ~356 lines — diff card factories (Phase 7)
│       ├── feed_card.py          # ~616 lines — feed_card widget factory (Phase 5)
│       ├── feed_tab.py           # ~387 lines — FeedTab (view only)
│       ├── file_tree.py          # ~2272 lines — FileTree (ColumnView directory browser, 22-property model, sort/filter/search)
│       ├── left_panel.py         # ~1004 lines — LeftPanel (Prompts/Agents/Projects notebook)
│       ├── main_content.py       # ~942 lines — MainContent (tabs + input + review bar)
│       ├── review_bar.py         # ~166 lines — ReviewBar widget (Phase 7)
│       ├── session_menu.py       # ~216 lines — session/project switcher popover
│       └── settings_dialog.py    # ~487 lines — GTK4 provider settings dialog
│
└── utils/
    ├── __init__.py               # 1 line
    ├── agent_defs.py             # ~552 lines — agent definition I/O
    ├── audit_parser.py           # ~182 lines — extract_audit_reports()
    ├── block_parser.py           # ~251 lines — extract_blocks() (Phase 2)
    ├── config.py                 # ~72 lines — config path helpers + COMMAND_PREFIX
    ├── conversation_store.py     # ~139 lines — ConversationSnapshot creation
    ├── crabcard_parser.py        # ~170 lines — parse crabcard blocks (Phase 5)
    ├── diff_parser.py            # ~321 lines — parse_diff() (Phase 7)
    ├── env_security.py           # ~44 lines — get_scrubbed_env() (MED-2 / CRIT-2)
    ├── escaping.py               # ~187 lines — escape_for_pango(), xml_escape_text()
    ├── favorites.py              # ~60 lines — favorites persistence
    ├── feed_store.py             # ~948 lines — feed JSON + update-journal persistence, compaction (SPEC-UI-RESPONSIVENESS-2 P2-P3)
    ├── feedback_processor.py     # ~274 lines — audit report file I/O
    ├── file_security.py          # ~36 lines — assert_secure_file() (MED-6)
    ├── git_ops.py                # ~263 lines — GitPython wrapper (Phase 7)
    ├── gtk_containers.py         # ~47 lines — is_in_container() sibling-walk check (GTK carve-out)
    ├── gtk_safe_link.py          # ~107 lines — HIGH-6 link safety guard (GTK carve-out)
    ├── icons.py                  # ~207 lines — Gdk.Texture SVG rendering (GTK carve-out)
    ├── image_utils.py            # convert_logo_to_icons()
    ├── improve.py                # ~212 lines — improve_prompt() MiniMax API
    ├── markdown.py               # ~279 lines — format_markdown() (Phase 1)
    ├── mcp_client.py             # ~541 lines — MCP asyncio-threading bridge
    ├── mcp_config.py             # ~295 lines — MCP server config loader
    ├── project_awareness.py      # ~641 lines — project awareness system
    ├── project_trust.py          # ~203 lines — HIGH-5 per-project trust gate
    ├── prompt_loader.py          # ~489 lines — system prompt template loader
    ├── prompts.py                # ~25 lines — load_prompts()
    ├── projects.py               # ~86 lines — load_projects(), scan_directory()
    ├── provider_url.py           # ~40 lines — validate_provider_url() (MED-5)
    ├── providers_store.py        # ~415 lines — provider YAML persistence
    ├── quoting.py                # ~78 lines — quoted-payload parsing
    ├── review_log.py             # ~146 lines — review log persistence
    ├── spellcheck.py             # ~100 lines — spell check engine (Enchant)
    ├── stt.py                    # ~219 lines — STTEngine (GTK carve-out: lazy GLib import)
    ├── syntax_highlight.py       # ~164 lines — Pygments → Pango (Tokyo Night)
    ├── work_persistence.py       # ~435 lines — work.json source of truth + generated tasks.md + legacy migration
    └── workflow_state.py         # ~226 lines — workflow state tracker

prompts/                         # System prompt templates for agent runtime
    └── system/
        ├── cc-implementation.md
        ├── claude-12rules.md
        ├── code-review.md
        ├── coder.md               # Coder agent system prompt
        ├── collab.md              # A2A collaboration protocol
        ├── crabcakes-commands.md  # Slash command reference
        ├── crabcakes-context.md   # Platform context for agents
        ├── crabcakes.md           # Crabcakes help agent context
        ├── debugger.md            # Debugger agent system prompt
        ├── default.md             # Default system prompt
        ├── improve.md             # Prompt improvement template
        ├── mcp-phase-b.md         # MCP Phase B implementation spec
        ├── project-awareness.md   # Project context awareness prompt
        └── project-onboarding.md  # Project onboarding prompt

knowledge/                       # User-facing documentation (read by Crabcakes agent via web_fetch)
    ├── agents.md
    ├── commands.md
    ├── configuration.md
    ├── features.md
    ├── gateway.md
    ├── setup.md
    └── troubleshooting.md

tests/                           # 100 files (verified 2026-06-27)
    ├── conftest.py
    ├── css_test.py               # CSS variant tests
    ├── format_test.py            # Format tests
    ├── generate_synthetic_conversations.py  # Test data generator
    ├── fixtures/
    ├── test_activity_bubbles.py
    ├── test_activity_drawer.py
    ├── test_agent_builder_handler.py
    ├── test_agent_command_handler.py
    ├── test_agent_defs.py
    ├── test_agent_list_handler.py
    ├── test_agent_runtime.py
    ├── test_auxilium_tier1.py
    ├── test_auxilium_tier2.py
    ├── test_agents.py
    ├── test_architecture.py
    ├── test_audit_parser.py
    ├── test_block_parser.py
    ├── test_bug_fixes.py
    ├── test_chat_handler.py
    ├── test_chat_render_handler.py
    ├── test_command_handler.py
    ├── test_command_models.py
    ├── test_config.py
    ├── test_context.py
    ├── test_context_strategy.py     # Context compaction strategy (41 tests)
    ├── test_connection_sync_handler.py
    ├── test_conversation.py
    ├── test_crabcard_parser.py
    ├── test_crabwatch_handler.py
    ├── test_create_project.py
    ├── test_diff_parser.py
    ├── test_enforcement.py
    ├── test_escaping.py
    ├── test_favorites.py
    ├── test_feedback_processor.py
    ├── test_feed_card.py
    ├── test_feed_handler.py
    ├── test_feed_store.py
    ├── test_forward_handler.py
    ├── test_gateway_handler.py
    ├── test_git_ops.py
    ├── test_icons.py
    ├── test_improve.py
    ├── test_left_panel.py
    ├── test_markdown.py
    ├── test_mcp_client.py
    ├── test_mcp_config.py
    ├── test_mcp_integration.py
    ├── test_media_handler.py
    ├── test_missing_message_fix.py
    ├── test_phase4.py
    ├── test_project_awareness.py
    ├── test_project_handler.py
    ├── test_project_list_handler.py
    ├── test_project_search.py
    ├── test_prompts_handler.py
    ├── test_prompt_loader.py
    ├── test_quoting.py
    ├── test_review_log.py
    ├── test_review_state.py
    ├── test_routing.py
    ├── test_runtime_compaction.py    # Runtime compaction integration
    ├── test_special_agents.py
    ├── test_streaming.py
    ├── test_syntax_highlight.py
    ├── test_tasks.py
    ├── test_task_redesign_prompts.py
    ├── test_tools.py
    ├── test_work_handler.py
    ├── test_work_persistence.py
    └── test_work_unit.py
```

**Test count:** 100 test files (as of 2026-06-27). For the current count and pass/fail status, run `pytest --co -q` and `pytest -q`. The explicit test-file enumeration in §13 is illustrative, not exhaustive — new tests are added with the features they cover and may not be retroactively enumerated.

---

## 14. Principles to Preserve

1. **Gateway is foundational.** It must remain independent of UI. Never import `ui/` from `gateway/`.

2. **Models are pure data.** They contain no GTK code. They are the single source of truth.

3. **UI is composed, not inherited.** Components are assembled in `window.py`. Each component is responsible for its own layout.

4. **Callbacks are the communication mechanism.** Components communicate through callbacks, not direct method calls on sibling components.

5. **Checkpoints over shortcuts.** Every significant piece of work should be verified (compiled, wired, tested) before moving on.

6. **Structure before features.** New features must fit the existing structure. If they don't, the structure must be updated — not circumvented.

7. **Comments for humans.** Every non-obvious decision, every non-standard pattern, every important constant — documented.

---

*This document is the law. Violations require discussion with the team before the code is merged.*

---

**For current project status — what's done, what's in progress, what's planned — see [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md).**
