# Configuring CrabCakes

CrabCakes stores all configuration under `~/.config/crabcakes/`. This guide covers every configuration file, the provider system, agent definitions, and how to set things up via the UI or by hand.

---

## Where is CrabCakes configuration stored?

All config lives under `~/.config/crabcakes/` (respecting `$XDG_CONFIG_HOME` if set). The directory is created on first launch with permissions `0o700` (owner-only), matching the `~/.ssh/` security model.

| File | Purpose | Permissions |
|------|---------|-------------|
| `providers.yaml` | **Canonical** provider configuration (LLM API keys, base URLs, models) | `0o600` |
| `agent.json` | Legacy provider config + enforcement/runtime settings | `0o600` |
| `agents/*.yaml` | Agent definitions (Coder, Debugger, Auxilium, custom agents) | `0o600` |
| `favorites.json` | Prompt library favorites | default |
| `projects/` | Per-project config (e.g., `members.json`) | default |
| `providers.yaml.tmp` | Atomic-write temp file (renamed on save) | `0o600` |

**Important:** The config directory contains API keys in plaintext. The `0o700` directory permissions and `0o600` file permissions ensure only your user can read them.

---

## providers.yaml — The Canonical Provider Config

The `providers.yaml` file is the **canonical** source for LLM provider configuration. It replaces the legacy `providers` section in `agent.json`.

### Why providers.yaml?

- **YAML is human-friendly** — easier to read and edit than nested JSON
- **Atomic writes** — `save_providers()` writes to a `.tmp` file then renames, preventing corruption
- **Managed by the UI** — the Settings → Providers dialog reads and writes this file directly
- **Backward compatible** — if `providers.yaml` is empty or missing, CrabCakes falls back to `agent.json`'s `providers` section (with a deprecation warning)

### File location

```
~/.config/crabcakes/providers.yaml
```

### Format

The file contains a YAML list of provider dictionaries:

```yaml
- name: openai
  base_url: https://api.openai.com/v1
  api_key: sk-your-key-here
  default_model: gpt-4o
  caller: openai
  enabled: true
  supports_tools: true
  supports_streaming: true
  max_tokens: 128000
  last_verified_at: "2026-06-14T23:26:46Z"
  last_error: null

- name: local-kb
  base_url: http://localhost:18790/v1
  api_key: "***"
  default_model: local-kb
  caller: openai
  enabled: true
  supports_tools: false
  supports_streaming: false
  max_tokens: 4096
  last_verified_at: null
  last_error: null
```

If PyYAML is not installed, CrabCakes falls back to JSON format automatically.

### ProviderConfig fields

Each provider entry maps to a `ProviderConfig` dataclass (defined in `models/providers.py`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | str | (required) | Display name, used as identifier throughout the app |
| `base_url` | str | (required) | API base URL (e.g., `https://api.openai.com/v1`) |
| `api_key` | str | (required) | API key for authentication |
| `default_model` | str | (required) | Default model ID (e.g., `gpt-4o`, `MiniMax-M2.7`) |
| `caller` | str | `""` | API caller key: `openai`, `minimax`, `anthropic`, `openrouter`, or `zai`. Determines which API format to use. |
| `enabled` | bool | `true` | If false, provider is skipped |
| `supports_tools` | bool | `true` | Whether this provider supports function/tool calling |
| `supports_streaming` | bool | `true` | Whether this provider supports streaming responses |
| `max_tokens` | int | `128000` | Context window size in tokens |
| `last_verified_at` | str \| null | `null` | ISO timestamp of last successful API verification (set by the UI) |
| `last_error` | str \| null | `null` | Last verification error message (set by the UI) |

### The `caller` field

The `caller` field tells CrabCakes which API format to use when making requests. This is essential because not all providers use the same request/response format:

| Caller | API format | Used by |
|--------|-----------|---------|
| `openai` | OpenAI Chat Completions | OpenAI, local-kb, any OpenAI-compatible endpoint |
| `minimax` | MiniMax API | MiniMax |
| `anthropic` | Anthropic Messages API | Anthropic (Claude) |
| `openrouter` | OpenRouter (OpenAI-compatible) | OpenRouter |
| `zai` | ZAI API | ZAI models |

If `caller` is empty, CrabCakes defaults to OpenAI format.

---

## agent.json — Runtime Settings + Legacy Providers

The `agent.json` file serves two purposes:

1. **Legacy provider config** — If `providers.yaml` is empty/missing, the `providers` section here is used as a fallback. You'll see a deprecation warning in the logs when this happens.
2. **Runtime settings** — Enforcement config, tool limits, cost limits, and other runtime behavior.

### Full agent.json structure

```json
{
    "_comment": "LLM provider configuration for Crabcakes agent runtime",
    "_security": "chmod 600 agent.json — this file contains API keys",
    "providers": {
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-your-key-here",
            "default_model": "gpt-4o",
            "max_tokens": 128000
        }
    },
    "default_provider": "openai",
    "default_model": "openai/gpt-4o",
    "max_tool_iterations": 50,
    "tool_timeout_seconds": 120,
    "cost_limit": null,
    "step_limit": 100,
    "provider_failover_attempt_timeout_s": 120.0,
    "provider_failover_budget_s": 400.0,
    "enforcement": {
        "enabled": true,
        "syntax_check": true,
        "test_run": true,
        "lint_check": true,
        "syntax_timeout_seconds": 10,
        "test_timeout_seconds": 60,
        "lint_timeout_seconds": 15,
        "max_output_chars": 2000,
        "skip_patterns": ["*.md", "*.txt", "*.json", "*.png"]
    }
}
```

### AgentConfig fields

These fields control the agent runtime behavior (defined in `agent/config.py`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `providers` | dict | `{}` | Legacy provider config (use `providers.yaml` instead) |
| `default_provider` | str | `"openai"` | Provider used when an agent doesn't specify one |
| `default_model` | str | `"openai/gpt-4o"` | Default model in `provider/model` format |
| `max_tool_iterations` | int | `50` | Maximum tool calls per conversation turn |
| `tool_timeout_seconds` | int | `120` | Timeout for each individual tool call |
| `auto_save_conversations` | bool | `true` | Save conversation history |
| `cost_limit` | float \| null | `null` | Per-conversation USD spending cap (null = no limit) |
| `step_limit` | int \| null | `null` | Per-conversation turn limit (null = no limit) |
| `enforcement` | object | (see below) | Post-write verification settings |
| `fallback_provider` | str \| null | `null` | KB provider fallback (e.g., `"openrouter"`) |
| `fallback_model` | str \| null | `null` | KB provider fallback model (e.g., `"openrouter/owl-alpha"`) |

### EnforcementConfig fields

The enforcement layer runs after an agent writes a file — it checks syntax, runs tests, and lints. Controlled via the `enforcement` section of `agent.json`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Master toggle for enforcement |
| `syntax_check` | bool | `true` | Run syntax check after writes |
| `test_run` | bool | `true` | Run test suite after writes |
| `lint_check` | bool | `true` | Run linter after writes |
| `syntax_timeout_seconds` | int | `10` | Max seconds for syntax check |
| `test_timeout_seconds` | int | `60` | Max seconds for test suite |
| `lint_timeout_seconds` | int | `15` | Max seconds for linter |
| `max_output_chars` | int | `2000` | Truncate enforcement output at this length |
| `skip_patterns` | list | (see below) | File patterns to skip enforcement on |

Default skip patterns (enforcement doesn't run on these):
```
*.md, *.txt, *.rst, *.adoc, *.json, *.yaml, *.yml, *.toml,
*.cfg, *.ini, *.conf, *.css, *.scss, *.less, *.html, *.htm,
*.xml, *.svg, *.png, *.jpg, *.jpeg, *.gif, *.ico, *.webp,
*.woff, *.woff2, *.ttf, *.eot, *.lock, *.map, LICENSE*, README*
```

---

## How providers.yaml and agent.json interact

CrabCakes uses a **fallback chain** for provider configuration:

1. **Try `providers.yaml` first** — `_load_providers_from_yaml_or_fallback()` calls `utils.providers_store.load_providers()` to read the YAML file.
2. **If non-empty** — Each `ProviderConfig` from YAML is converted to an `LLMProviderConfig` via `_to_llm_provider()` and added to the providers dict. The runtime uses these.
3. **If empty/missing** — Fall back to the `providers` section in `agent.json`. A deprecation warning is logged:
   ```
   agent.json: providers section is deprecated and will be ignored
   once providers.yaml is created. Use Settings → Providers to migrate.
   ```
4. **If both unavailable** — Returns an empty providers dict. Agents won't work until you configure at least one provider.

### The `_to_llm_provider` conversion

When loading from `providers.yaml`, each `ProviderConfig` (the storage format from `models/providers.py`) is converted to an `LLMProviderConfig` (the runtime format from `agent/config.py`). The fields map 1:1:

```python
def _to_llm_provider(p) -> LLMProviderConfig:
    return LLMProviderConfig(
        name=p.name,
        base_url=p.base_url,
        api_key=p.api_key,
        default_model=p.default_model,
        caller=p.caller,
        supports_tools=p.supports_tools,
        supports_streaming=p.supports_streaming,
        max_tokens=p.max_tokens,
        enabled=p.enabled,
        last_verified_at=p.last_verified_at,
        last_error=p.last_error,
    )
```

Providers are keyed two ways in the runtime dict:
- By provider ID (derived from `default_model` prefix, e.g., `"minimax"` from `"minimax/MiniMax-M2.7"`)
- By display name (e.g., `"MiniMax"`)

---

## The local-kb Provider

The `local-kb` provider is special — it wraps CrabCakes' built-in KB HTTP server, which presents an OpenAI-compatible API on `localhost:18790`.

### What it does

When the agent runtime sends a chat completion request to `local-kb`:

1. The KB server (`agent/kb_server.py`) receives the request
2. It extracts the last user message from the `messages` array
3. It embeds the question using the `BAAI/bge-small-en-v1.5` model
4. It computes cosine similarity against all indexed KB chunks
5. Top-5 chunks above score 0.35 are returned, formatted as a response
6. If the top chunk's score is below 0.55 (confidence threshold), returns `[KB_OUT_OF_SCOPE]`
7. The response is formatted as a standard OpenAI chat completion

### Configuration

```yaml
- name: local-kb
  base_url: http://localhost:18790/v1
  api_key: "***"           # placeholder — KB server doesn't check auth
  default_model: local-kb
  caller: openai            # OpenAI-compatible API format
  supports_tools: false     # KB server never calls tools
  supports_streaming: false # blocking only, no streaming
  max_tokens: 4096
```

### Auto-setup via ensure_kb_provider()

On every startup, `ensure_kb_provider()` in `utils/providers_store.py` runs. It is idempotent:

1. **Seeds the provider** — If no `local-kb` entry exists in `providers.yaml`, one is created
2. **Patches the Auxilium agent** — If Auxilium's `llm_name` is empty, sets it to `local-kb`

If you manually configure Auxilium with a different provider (e.g., OpenRouter), step 2 is skipped.

---

## agents/ Directory — Agent Definitions

Agent definitions live in `~/.config/crabcakes/agents/` as YAML files.

### Agent YAML schema

```yaml
# Required fields
name: Coder                    # Display name shown in UI
emoji: "🛠️"                    # Emoji for avatar
role: coder                    # Role identifier (matches prompts/system/{role}.md)
prompts:                       # System prompt templates to load
  - system/coder.md
tools:                         # Tool names this agent can use
  - read_file
  - write_file
  - edit_file
  - exec_command
  - list_files
  - search_files
  - web_search
  - web_fetch

# Provider configuration (optional — falls back to global default)
llm_name: minimax              # Provider name from providers.yaml
model: MiniMax-M2.7            # Model override (optional)

# Fallback LLM (optional — used when primary provider is unavailable)
fallback_provider: openrouter
fallback_model: openrouter/free

# MCP servers (optional)
mcp_servers:
  - memory

# Behavior flags
auto_open: false               # Open tab on every app launch
auto_add_to_projects: false    # Auto-add to every project team
api_key_built_in: false        # Reserved: agent ships with embedded key

# Self-improvement layer
self_improvement:
  bug_journal: true            # Track bugs in a per-project journal
  project_rules: true          # Learn project-specific rules
  enforcement: true            # Post-write verification (syntax/test/lint)
  structured_feedback: false   # Structured feedback processing
  dream_consolidation: true    # Periodic knowledge consolidation
```

### Per-provider overrides

Each agent can override the global provider:

- `llm_name` — Use a specific provider from `providers.yaml` (e.g., `"minimax"`, `"openai"`, `"local-kb"`)
- `model` — Use a specific model (e.g., `"MiniMax-M2.7"`, `"gpt-4o"`)
- If both are unset, the global `default_provider` and `default_model` from `agent.json` are used

### Self-improvement defaults

When `self_improvement` is not specified in the YAML, defaults come from `get_default_si_config()`:

| Key | Default (can_write=False) | Default (can_write=True) |
|-----|--------------------------|--------------------------|
| `bug_journal` | `true` | `true` |
| `project_rules` | `true` | `true` |
| `enforcement` | `false` | `true` |
| `structured_feedback` | `false` | `false` |
| `dream_consolidation` | `false` | `false` |

The `can_write` flag is determined by whether the agent has `write_file` or `edit_file` in its tools.

### Built-in agent definitions

Three default agents are seeded from `prompts/default_agents/` on first launch:

| Agent | File | Role | Key traits |
|-------|------|------|------------|
| 🦀 Auxilium | `auxilium.yaml` | `helper` | `auto_open: true`, `auto_add_to_projects: true`, uses `local-kb` |
| 🛠️ Coder | `coder.yaml` | `coder` | Has write tools, enforcement enabled, full self-improvement |
| 🐛 Debugger | `debugger.yaml` | `debugger` | Read-only (no write tools), enforcement disabled |

### Creating custom agents

**Via the UI:** Use the Agent Builder dialog (pencil icon in the Agents tab).

**Via YAML:** Create a new `.yaml` file in `~/.config/crabcakes/agents/`:

```yaml
name: Reviewer
emoji: "🔍"
role: reviewer
prompts:
  - system/reviewer.md
tools:
  - read_file
  - list_files
  - search_files
llm_name: openai
model: gpt-4o
auto_open: false
auto_add_to_projects: false
self_improvement:
  bug_journal: false
  project_rules: true
  enforcement: false
```

### Legacy migration

If you have an old `crabcakes.yaml` agent file (from before the Auxilium rename), it's automatically migrated to `auxilium.yaml` on next launch. The `name`, `role`, and prompt references are updated in-place.

---

## Configuring Providers via the UI

The Settings → Providers dialog provides a full UI for managing providers:

1. Click the **⚙ Settings** button in the toolbar
2. Navigate to the **Providers** tab
3. You'll see all providers from `providers.yaml` listed
4. Click **Add Provider** to create a new one, or click an existing provider to edit

### Fields in the UI

| UI Field | Maps to | Description |
|----------|---------|-------------|
| Name | `name` | Display name |
| Base URL | `base_url` | API endpoint |
| API Key | `api_key` | Your secret key |
| Default Model | `default_model` | Model ID |
| Caller | `caller` | API format (dropdown) |
| Supports Tools | `supports_tools` | Checkbox |
| Supports Streaming | `supports_streaming` | Checkbox |
| Max Tokens | `max_tokens` | Context window size |

### The red dot on Settings

If no provider has been verified (`last_verified_at` is null on all providers), a red dot appears on the ⚙ Settings button. This is driven by `has_any_verified_provider()` in `utils/providers_store.py`.

### Verification

The Settings dialog can verify a provider by making a test API call. On success, `last_verified_at` is set to the current ISO timestamp and `last_error` is cleared. On failure, `last_error` captures the error message.

---

## File Permissions

CrabCakes takes API key security seriously:

### Config directory

```bash
~/.config/crabcakes/    # chmod 0o700 (owner-only, like ~/.ssh/)
```

Created by `_create_default_config()` and enforced by `_fix_config_dir_permissions()` on every startup. If the directory has broader permissions, they're tightened automatically.

### Config files

```bash
~/.config/crabcakes/agent.json       # chmod 0o600
~/.config/crabcakes/providers.yaml   # chmod 0o600
```

Both are written with `0o600` permissions. The `save_providers()` function in `utils/providers_store.py` uses atomic writes (`.tmp` → rename) and then `os.chmod(path, 0o600)`.

### Permission warnings

On startup, `_check_permissions()` in `agent/config.py` checks if `agent.json` is group or world readable. If so, it logs:

```
WARNING: agent.json is readable by other users (mode=0644).
Run: chmod 600 /home/user/.config/crabcakes/agent.json
```

This is a warning only — CrabCakes does not refuse to start.

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `XDG_CONFIG_HOME` | `~/.config` | Base config directory |
| `CRABCAKES_PROJECTS_DIR` | `~/projects` | Root directory for the Projects file tree |
| `CRABCAKES_GATEWAY_URL` | `ws://localhost:18789` | OpenClaw gateway WebSocket URL |
| `CRABCAKES_DEBUG` | (unset) | Set to `1` for verbose debug logging |
| `STT_MODEL_SIZE` | `tiny.en` | faster-whisper model size for voice input |

These are resolved centrally in `utils/config.py`. All path lookups go through `get_config_dir()`, `get_projects_dir()`, `get_gateway_url()`, or `get_identity_dir()` — never hardcoded paths.

---

## Configuring the OpenClaw Gateway Connection

CrabCakes connects to an OpenClaw gateway via WebSocket for remote agent access.

### Default connection

- **URL:** `ws://localhost:18789` (configurable via `$CRABCAKES_GATEWAY_URL`)
- **Auth:** Ed25519 device keys from `~/.openclaw/identity/`
- **Protocol:** v3 device-auth handshake

### Connecting

1. Click the **Connect** button in the toolbar
2. The status label shows "connecting…" then "connected" or an error
3. Once connected, gateway agents appear in the left panel under the **Agents** tab

### Without a gateway

You do NOT need a gateway connection to use CrabCakes. The three special agents (Coder, Debugger, Auxilium) run locally with direct API calls. The gateway is only needed for remote OpenClaw agents.

---

## Quick Reference: Complete Configuration Example

Here's a complete setup with two providers, three agents, and enforcement:

### providers.yaml

```yaml
- name: openai
  base_url: https://api.openai.com/v1
  api_key: sk-your-openai-key
  default_model: gpt-4o
  caller: openai
  enabled: true
  supports_tools: true
  supports_streaming: true
  max_tokens: 128000

- name: minimax
  base_url: https://api.minimax.chat/v1
  api_key: your-minimax-key
  default_model: MiniMax-M2.7
  caller: minimax
  enabled: true
  supports_tools: true
  supports_streaming: true
  max_tokens: 1048576

- name: local-kb
  base_url: http://localhost:18790/v1
  api_key: "***"
  default_model: local-kb
  caller: openai
  enabled: true
  supports_tools: false
  supports_streaming: false
  max_tokens: 4096
```

### agent.json (runtime settings only — providers moved to YAML)

```json
{
    "default_provider": "openai",
    "default_model": "openai/gpt-4o",
    "max_tool_iterations": 50,
    "tool_timeout_seconds": 120,
    "cost_limit": null,
    "step_limit": 100,
    "enforcement": {
        "enabled": true,
        "syntax_check": true,
        "test_run": true,
        "lint_check": true
    }
}
```

This setup gives you:
- OpenAI for general-purpose agents
- MiniMax for the Coder (large 1M token context)
- Local KB for Auxilium (free, offline help)
- Enforcement enabled for write-capable agents
- A $5 per-conversation cost cap and 100-turn limit
