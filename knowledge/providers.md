# Configuring LLM Providers

Auxilium and the other built-in agents (Coder, Debugger) need an LLM provider to function. This guide walks you through configuring one of the six supported providers — including the built-in local KB provider that needs no API key at all.

**One-click presets (Settings → Providers → `+ Add Provider`)** — Base URL, caller, and a default model are pre-filled; you paste only the key:

| Preset | Base URL | Get a key |
|---|---|---|
| **OpenAI** | `https://api.openai.com/v1` | https://platform.openai.com/api-keys |
| **Anthropic** | `https://api.anthropic.com` | https://console.anthropic.com/settings/keys |
| **OpenRouter** | `https://openrouter.ai/api/v1` | https://openrouter.ai/keys |
| **Google Gemini** | `https://generativelanguage.googleapis.com/v1beta/openai` | https://aistudio.google.com |

MiniMax and Z.ai presets are deferred until their endpoints/model IDs can be live-verified (the repo currently mixes `api.minimax.chat` and `api.minimax.io` across docs and tests — flagged, not silently rewritten; see `knowledge/configuration.md:505` vs the runtime tests).

**Quick decision guide:**

| Provider | Best for | Cost | Setup time |
|---|---|---|---|
| **Local KB** | Zero-config help on fresh install | Free (no API key) | 0 minutes |
| **OpenRouter free** | Trying CrabCakes with an external LLM | Free tier | 1 minute |
| **Ollama (local)** | Privacy, offline use, no API key | Free (uses your hardware) | 5 minutes |
| **OpenAI** | Highest quality (GPT-4o) | Pay-per-token | 2 minutes |
| **Anthropic** | Best long-context (Claude 3.5) | Pay-per-token | 2 minutes |
| **Google Gemini** | Cheap high-volume | Free tier + paid | 2 minutes |

---

## How Provider Configuration Works

CrabCakes stores provider settings in `~/.config/crabcakes/providers.yaml` (with `agent.json` as a legacy fallback). Each provider has:

- `name` — display name
- `base_url` — API endpoint
- `api_key` — your secret key
- `default_model` — the model identifier
- `caller` — which API format to use (`openai` / `minimax` / `anthropic` / `openrouter` / `zai`)
- `supports_tools` / `supports_streaming` — capability flags
- `max_tokens` — context window size

The `caller` field determines which API client handles requests. Most modern providers (OpenRouter, Anthropic, Google) use the `openai` caller since they implement OpenAI-compatible APIs.

---

## Option 1: OpenRouter (Free Tier) — Recommended for Getting Started

**What it is:** A unified API that gives you access to dozens of models through a single API key, including a free tier.

**Why this option:** Zero cost, single signup, no credit card required, instant access.

### Setup

1. Go to https://openrouter.ai and create an account
2. Navigate to **Keys** and click **Create New Key**
3. Copy the key (starts with `sk-or-v1-...`)
4. In CrabCakes, run the first-run wizard and select **(a) OpenRouter free**
5. Paste the key when prompted

The wizard will write the configuration and verify the connection.

### Manual configuration (if not using the wizard)

Edit `~/.config/crabcakes/agent.json`:

```json
{
  "providers": {
    "openrouter": {
      "name": "openrouter",
      "base_url": "https://openrouter.ai/api/v1",
      "api_key": "sk-or-v1-YOUR_KEY_HERE",
      "default_model": "openrouter/auto",
      "caller": "openrouter",
      "supports_tools": true,
      "supports_streaming": true,
      "max_tokens": 128000,
      "enabled": true
    }
  },
  "default_provider": "openrouter",
  "default_model": "openrouter/auto"
}
```

### Free-tier models

`openrouter/auto` routes to the best free model available. Specific free models include:
- `openrouter/aurora` — free
- `openrouter/owl-alpha` — free (when available)

Free-tier rate limits: ~20 requests/minute, ~50 requests/day. Sufficient for getting started.

### Troubleshooting

**401 Unauthorized** — Your API key is invalid or expired. Generate a new key at https://openrouter.ai/keys.

**429 Too Many Requests** — You hit the free-tier rate limit. Wait a minute, or upgrade to a paid plan.

**Model not found** — The `default_model` doesn't exist. Check https://openrouter.ai/models for valid model IDs.

---

## Option 2: Ollama (Local, Free) — Best for Privacy

**What it is:** A local LLM runtime. Runs entirely on your machine — no API key, no internet, no data leaves your computer.

**Why this option:** Privacy-first, zero cost, works offline. Quality is lower than GPT-4o but improving fast.

### Setup

1. **Install Ollama:**
   ```bash
   # macOS
   brew install ollama

   # Linux
   curl -fsSL https://ollama.com/install.sh | sh

   # Windows
   # Download from https://ollama.com/download
   ```

2. **Start the Ollama server** (if not already running):
   ```bash
   ollama serve
   ```
   This runs in the foreground. For a background service, follow the Ollama docs for your OS.

3. **Pull a model:**
   ```bash
   ollama pull llama3.2:3b        # 2GB download, fast on most hardware
   # or
   ollama pull qwen2.5-coder:7b   # 4.4GB, better for code
   ```

4. **In CrabCakes:** Run the first-run wizard, select **(b) Ollama local**. The wizard detects `ollama` on PATH and verifies the endpoint at `http://localhost:11434`.

### Manual configuration

Edit `~/.config/crabcakes/agent.json`:

```json
{
  "providers": {
    "ollama": {
      "name": "ollama",
      "base_url": "http://localhost:11434/v1",
      "api_key": "ollama",
      "default_model": "llama3.2:3b",
      "caller": "openai",
      "supports_tools": true,
      "supports_streaming": true,
      "max_tokens": 32000,
      "enabled": true
    }
  },
  "default_provider": "ollama",
  "default_model": "llama3.2:3b"
}
```

**Note:** The `caller` is `openai` because Ollama implements the OpenAI-compatible API. The `api_key` is a placeholder (`"ollama"`) since Ollama doesn't require authentication.

### Recommended models

| Model | Size | Quality | Use case |
|---|---|---|---|
| `llama3.2:3b` | 2GB | OK | Light use, fast |
| `llama3.2:7b` | 4.4GB | Good | General purpose |
| `qwen2.5-coder:7b` | 4.4GB | Good for code | Coding agents |
| `mistral:7b` | 4.1GB | Good | General purpose |
| `llama3.1:70b` | 40GB | Excellent | Needs 64GB+ RAM |

### Troubleshooting

**Connection refused on `http://localhost:11434`** — Ollama isn't running. Start it with `ollama serve`.

**Model not found** — Pull it first: `ollama pull llama3.2:3b`. Check installed models with `ollama list`.

**Slow responses** — Your model is too large for your hardware. Try a smaller model (`llama3.2:3b` instead of `llama3.1:70b`).

**Out of memory** — Your model doesn't fit in RAM. Close other apps or use a smaller model.

---

## Option 3: OpenAI (GPT-4o) — Best Quality, Paid

**What it is:** OpenAI's API, the gold standard for general LLM quality.

**Why this option:** Highest quality responses, most reliable, best tool use. Costs money.

### Setup

1. Go to https://platform.openai.com and create an account
2. Add a payment method (API access is pay-as-you-go)
3. Navigate to **API Keys** and click **Create new secret key**
4. Copy the key (starts with `sk-...`)
5. In CrabCakes: first-run wizard → **(c) Bring your own key** → select **OpenAI** → paste key

### Pricing

GPT-4o: ~$2.50 per 1M input tokens, ~$10 per 1M output tokens.
GPT-4o-mini: ~$0.15 per 1M input tokens, ~$0.60 per 1M output tokens.

A typical Auxilium conversation uses ~5K tokens total. Cost: ~$0.01 with GPT-4o, ~$0.001 with GPT-4o-mini.

### Manual configuration

```json
{
  "providers": {
    "openai": {
      "name": "openai",
      "base_url": "https://api.openai.com/v1",
      "api_key": "sk-YOUR_KEY_HERE",
      "default_model": "gpt-4o",
      "caller": "openai",
      "supports_tools": true,
      "supports_streaming": true,
      "max_tokens": 128000,
      "enabled": true
    }
  },
  "default_provider": "openai",
  "default_model": "gpt-4o"
}
```

### Available models

- `gpt-4o` — flagship, best quality
- `gpt-4o-mini` — fast, cheap, surprisingly capable
- `gpt-4-turbo` — older, similar to 4o
- `o1` / `o1-mini` — reasoning models, slower but better at complex tasks

### Troubleshooting

**401 Incorrect API key** — Key is wrong or revoked. Regenerate at https://platform.openai.com/api-keys.

**429 Rate limit exceeded** — You're sending too many requests. Slow down or upgrade your tier.

**insufficient_quota** — Your account is out of credits. Add funds at https://platform.openai.com/account/billing.

---

## Option 4: Anthropic (Claude 3.5 Sonnet) — Best Long-Context

**What it is:** Anthropic's API, known for long-context windows and careful reasoning.

**Why this option:** Claude 3.5 Sonnet has 200K context, excellent at long documents, careful at tool use.

### Setup

1. Go to https://console.anthropic.com and create an account
2. Add a payment method
3. Navigate to **Settings → API Keys** and create a new key
4. Copy the key (starts with `sk-ant-...`)
5. In CrabCakes: first-run wizard → **(c) Bring your own key** → select **Anthropic** → paste key

### Pricing

Claude 3.5 Sonnet: ~$3 per 1M input tokens, ~$15 per 1M output tokens.
Claude 3.5 Haiku: ~$0.80 per 1M input tokens, ~$4 per 1M output tokens.

### Manual configuration

```json
{
  "providers": {
    "anthropic": {
      "name": "anthropic",
      "base_url": "https://api.anthropic.com",
      "api_key": "sk-ant-YOUR_KEY_HERE",
      "default_model": "claude-3-5-sonnet-20241022",
      "caller": "anthropic",
      "supports_tools": true,
      "supports_streaming": true,
      "max_tokens": 200000,
      "enabled": true
    }
  },
  "default_provider": "anthropic",
  "default_model": "claude-3-5-sonnet-20241022"
}
```

### Available models

- `claude-3-5-sonnet-20241022` — flagship, 200K context
- `claude-3-5-haiku-20241022` — fast, cheap
- `claude-3-opus-20240229` — older, more expensive, very capable

### Troubleshooting

**401 Invalid API Key** — Key is wrong. Regenerate at https://console.anthropic.com/settings/keys.

**404 Not Found** — Model name is wrong. Check https://docs.anthropic.com/en/docs/models-overview for current model IDs.

---

## Option 5: Google Gemini — Free Tier + Cheap Paid

**What it is:** Google's Gemini API, with a generous free tier.

**Why this option:** Free tier is more permissive than OpenRouter's. Gemini 2.0 Flash is fast and capable.

### Setup

1. Go to https://aistudio.google.com and create an account (or use existing Google account)
2. Click **Get API Key** in the left sidebar
3. Create a new key (or use the default)
4. Copy the key
5. In CrabCakes: first-run wizard → **(c) Bring your own key** → select **Google** → paste key

### Pricing

Gemini 2.0 Flash: **Free** up to 1,500 requests/day.
Gemini 1.5 Pro: Free tier available, then ~$1.25 per 1M input tokens.

### Manual configuration

```json
{
  "providers": {
    "google": {
      "name": "google",
      "base_url": "https://generativelanguage.googleapis.com/v1beta",
      "api_key": "YOUR_GEMINI_KEY_HERE",
      "default_model": "gemini-2.0-flash",
      "caller": "openai",
      "supports_tools": true,
      "supports_streaming": true,
      "max_tokens": 1000000,
      "enabled": true
    }
  },
  "default_provider": "google",
  "default_model": "gemini-2.0-flash"
}
```

**Note:** Google Gemini has an OpenAI-compatible endpoint at the same `base_url` with `/openai/` appended. If you prefer the native Gemini API, use `caller: "openai"` with the `base_url` above — CrabCakes handles the format translation.

### Available models

- `gemini-2.0-flash` — fast, free tier, recommended
- `gemini-1.5-pro` — more capable, slower
- `gemini-1.5-flash` — older fast model

### Troubleshooting

**403 Forbidden** — API key is invalid or the Generative Language API is not enabled in your Google Cloud project.

**429 Resource Exhausted** — Free tier rate limit hit. Wait or upgrade.

---

## Switching Providers Mid-Session

You can change providers without restarting CrabCakes:

1. Edit `~/.config/crabcakes/agent.json`
2. Update `default_provider` and `default_model`
3. In CrabCakes, click **Settings → Reload Config** (if available) or restart the app

Or use the first-run wizard's "reconfigure" option (if the wizard supports re-runs).

---

## Common Errors Across All Providers

### "No provider configured"

**Cause:** `agent.json` has no `providers` block, or `default_provider` is not in `providers`.

**Fix:** Run the first-run wizard, or manually add a provider block as shown in the examples above.

### "401 Unauthorized" / "Invalid API Key"

**Cause:** Wrong key, expired key, or key with insufficient permissions.

**Fix:**
1. Regenerate the key at the provider's dashboard
2. Update `agent.json` with the new key
3. Restart CrabCakes

### "429 Too Many Requests" / "Rate Limit Exceeded"

**Cause:** You've hit the provider's rate limit.

**Fix:**
- Free tiers: wait an hour, or upgrade
- Paid tiers: check the provider's rate limit dashboard, consider raising it

### "Network unreachable" / "Connection refused"

**Cause:** No internet connection, or the provider's endpoint is down.

**Fix:**
1. Check `curl https://<provider_base_url>/` from your terminal
2. If that fails, it's a network/provider issue, not a CrabCakes issue
3. Try a different provider to confirm CrabCakes is working

### "Model not found" / "Invalid model"

**Cause:** The `default_model` in your config doesn't exist (typo, deprecated model, etc.)

**Fix:** Check the provider's docs for current model names. The first-run wizard has a dropdown of valid models.

---

## Storing API Keys Securely

**CrabCakes does NOT encrypt `agent.json`.** The file is plaintext JSON with your API keys in it.

Best practices:

1. **Restrict file permissions:**
   ```bash
   chmod 600 ~/.config/crabcakes/agent.json
   ```
   CrabCakes warns at startup if the file is group/world-readable.

2. **Use a separate API key per provider.** Don't reuse keys across services.

3. **Set usage limits** at the provider's dashboard. Most providers let you cap monthly spend.

4. **Rotate keys** periodically. Most providers support multiple active keys.

5. **For shared/team machines**, use a secrets manager (e.g., `pass`, `vault`, `1password-cli`) and source keys via `agent.json` templating. CrabCakes does not currently support env-var interpolation in `agent.json` — if you need this, file a feature request.

---

## The Local KB Provider (Zero-Config Help)

**What it is:** A built-in knowledge base provider that serves answers from CrabCakes' own documentation — no external API, no API key, no internet required.

**How it works:**

1. The KB HTTP server (`agent/kb_server.py`) runs on `localhost:18790`
2. It exposes an OpenAI-compatible `/v1/chat/completions` endpoint
3. When Auxilium receives a question, the KB server searches indexed documentation using cosine similarity
4. The index is built from `knowledge/*.md` files using the `BAAI/bge-small-en-v1.5` embedding model
5. If the question matches with high confidence (score ≥ 0.55), the relevant KB chunks are returned as the response

**Auto-setup on fresh install:**

The `ensure_kb_provider()` function (in `utils/providers_store.py`) runs automatically when `AgentRuntimeHandler` initializes:
1. Seeds a `local-kb` provider entry into `providers.yaml` if missing
2. Patches the Auxilium agent to use `local-kb` as its primary provider if it has no provider set

This means **Auxilium works out of the box** — you can ask it questions about CrabCakes without configuring anything.

### The Fallback Chain

When the KB server cannot answer a question (score below the 0.55 confidence threshold), it returns the sentinel `[KB_OUT_OF_SCOPE]`. If a fallback provider is configured, the runtime automatically:

1. Detects the `[KB_OUT_OF_SCOPE]` sentinel
2. Swaps the conversation model to the fallback provider
3. Retries the LLM call with the external provider
4. Restores the original `local-kb` model after the call

This is guarded by a one-shot flag (`conv._fallback_attempted`) to prevent infinite loops.

### Configuring Fallback

In the Agent Builder UI, set the **Fallback Provider** dropdown to an external provider (e.g. `openrouter`). When Auxilium's KB can't answer, it will use the fallback provider instead.

You can also configure fallback in `agent.json`:
```json
{
  "fallback_provider": "openrouter",
  "fallback_model": "openrouter/auto"
}
```

### Rebuilding the KB Index

When KB content changes (e.g. after updating documentation), rebuild the index:

```bash
cd /path/to/crabcakes
python3 scripts/rebuild_kb_index.py
```

This re-embeds all `knowledge/*.md` files and writes:
- `knowledge/.index/chunks.json` — chunk metadata
- `knowledge/.index/embeddings.npy` — float32 embedding array

**Requirements:**
- `sentence-transformers` package
- `numpy` package
- ~130MB model download on first run

### local-kb Provider Configuration

The auto-seeded configuration in `providers.yaml`:

```yaml
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

Note: `caller: openai` because the KB server implements the OpenAI-compatible API format. `supports_tools: false` because the KB server never calls tools. `supports_streaming: false` because it returns blocking responses.

For full details on the KB provider architecture, see [kb-provider.md](kb-provider.md).

---

## Verifying Provider Configuration

After configuring a provider, test it:

1. Open Auxilium (or Coder) in CrabCakes
2. Send a simple test message: "Hello, who are you?"
3. If you get a response, the provider is working
4. If you get an error, check the error message against the **Common Errors** section above

For deeper debugging, run CrabCakes with `CRABCAKES_DEBUG=1`:

```bash
CRABCAKES_DEBUG=1 crabcakes
```

This shows API request/response logs in the terminal.

---

## Next Steps

- **First conversation with Auxilium:** Try asking "How do I create a project?" or "What does the Coder agent do?"
- **Configure multiple providers:** Add a second provider to `agent.json` and switch between them
- **Set up project-specific agents:** See `agents.md` for creating custom agents per project
- **Explore features:** See `features.md` for the full feature list

If something doesn't work, check `troubleshooting.md` for known issues.
