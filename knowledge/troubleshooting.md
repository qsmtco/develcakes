# Troubleshooting Guide

This guide covers common issues in DevelCakes and how to resolve them. Each issue includes the actual error message you will see, the root cause, and step-by-step fixes.

---

## Provider Configuration Errors

### "No LLM provider configured for X"

**Where you see it:** Raised as `ValueError` in `agent/runtime.py` when `_call_llm()` cannot find a provider for the agent's model.

**Root cause:** The `agent.json` or `providers.yaml` has no provider entry matching the model prefix, or no providers are configured at all.

**Fix:**

1. Run the first-run wizard (Settings → Run Setup Wizard)
2. Or manually edit `~/.config/develcakes/providers.yaml` to add a provider:

```yaml
- name: openrouter
  base_url: https://openrouter.ai/api/v1
  api_key: sk-or-v1-YOUR_KEY
  default_model: openrouter/auto
  caller: openrouter
  enabled: true
  supports_tools: true
  supports_streaming: true
  max_tokens: 128000
```

3. Ensure the agent's `llm_name` in `agents/*.yaml` matches a configured provider name
4. Restart DevelCakes

### "Provider 'X' is not configured"

**Where you see it:** Raised in `agent/runtime.py` when the model has a provider prefix (e.g. `openrouter/model`) but that provider is not in the config.

**Root cause:** The agent's model field specifies a provider that doesn't exist in your config.

**Fix:**

1. Check which provider the agent uses: open the Agent Builder (edit the agent) and look at the LLM Provider field
2. Add that provider to `providers.yaml` (see above)
3. Or change the agent to use a provider you already have configured

### "No API key configured for provider X"

**Where you see it:** Error message when an agent tries to call an LLM API without authentication.

**Root cause:** The provider entry exists but `api_key` is empty, and no per-agent API key is set.

**Fix:**

1. Open Settings → click the ⚙ button in the toolbar
2. Find the provider and add your API key
3. Or edit `~/.config/develcakes/providers.yaml` directly and add the `api_key` field
4. For per-agent keys, use the Agent Builder to set an API key specific to one agent

The `providers.yaml` fallback scan: if neither the conversation nor the provider config has an API key, the runtime scans `providers.yaml` for a matching provider name with a non-empty key.

### "No caller for provider X (caller_key=Y)"

**Where you see it:** `ValueError` in `agent/runtime.py` when the non-streaming LLM call cannot find a caller function.

**Root cause:** The provider's `caller` field in `providers.yaml` does not match any registered caller. Valid caller values: `openai`, `minimax`, `anthropic`, `openrouter`, `zai`.

**Fix:**

1. Check the `caller` field in your provider configuration
2. Set it to the correct API format:
   - OpenAI → `caller: openai`
   - OpenRouter → `caller: openrouter`
   - Anthropic → `caller: anthropic`
   - MiniMax → `caller: minimax`
   - Ollama → `caller: openai` (Ollama is OpenAI-compatible)
   - Google Gemini → `caller: openai` (uses OpenAI-compatible endpoint)
3. Save and restart

### "No streaming caller for caller_key=X"

**Where you see it:** `ValueError` in `agent/runtime.py` when streaming is enabled but no streaming function is registered for the caller.

**Root cause:** The provider's `caller` field doesn't have a registered streaming function, or `supports_streaming` is true but the caller type is unknown.

**Fix:**

1. Set `supports_streaming: false` in the provider config to disable streaming
2. Or correct the `caller` field to a supported value (see above)

---

## Agent Not Responding

### Special Agents (Coder, Debugger, Supervisor, custom)

**Symptom:** Special agent shows no response or an error message.

**Check:**

1. **Provider configured:** Ensure the agent's LLM provider is set up with a valid API key
2. **API key valid:** Test with a simple message — if you get a 401 error, the key is invalid
3. **Check error messages:** Special agent errors appear in the chat as red error bubbles
4. **Review logs:** `DEVELCAKES_DEBUG=1` shows the full API request/response cycle

**Common errors:**
- `"Agent returned no content"` — The LLM provider returned an empty response. Check provider status and model name.
- `"OpenAI API error 429"` — Rate limited. Wait and retry.
- `"OpenAI API error 401"` — Invalid API key.

---

## App Won't Start

### Python Version

DevelCakes requires Python 3.12+. Check:

```bash
python3 --version
```

### GTK4 Installation

DevelCakes requires GTK4 and PyGObject. Verify:

```bash
python3 -c "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk; print('GTK4 OK')"
```

If this fails, install GTK4:

**Ubuntu/Debian:**
```bash
sudo apt install python3-gi gir1.2-gtk-4.0
```

**Fedora:**
```bash
sudo dnf install python3-gobject gtk4
```

**Arch Linux:**
```bash
sudo pacman -S python-gobject gtk4
```

### Dependencies

Install DevelCakes dependencies:

```bash
cd /path/to/projects/develcakes
pip install -e .
```

### Import Errors

If you see `ModuleNotFoundError` for DevelCakes modules, ensure you are running from the project root:

```bash
cd /path/to/projects/develcakes
python3 main.py
```

---

## Log Files and Debug Mode

### Log Location

DevelCakes logs to stderr (terminal output). There is no log file by default — logs appear in the terminal where DevelCakes was launched.

### Debug Mode

Enable verbose logging with the `DEVELCAKES_DEBUG` environment variable (the older `CRABCAKES_DEBUG` name still works for one release):

```bash
DEVELCAKES_DEBUG=1 python3 main.py
```

This sets the log level to `DEBUG` (default is `WARNING`). You will see:
- LLM API request/response details
- Tool execution details

---

## Config Reset Instructions

### Full Reset (Nuclear Option)

**Warning:** This removes all configuration, API keys, favorites, and project data.

```bash
# Backup first
cp -r ~/.config/develcakes ~/.config/develcakes.backup

# Remove config
rm -rf ~/.config/develcakes
```

DevelCakes will start fresh on next launch with the first-run wizard.

### Partial Reset

**Reset only providers (keep agents, projects, favorites):**

```bash
rm ~/.config/develcakes/providers.yaml
rm ~/.config/develcakes/agent.json
```

**Reset only agent definitions (keep providers):**

```bash
rm -rf ~/.config/develcakes/agents/
```

**Reset a single project's DevelCakes data:**

```bash
rm -rf /path/to/project/.crabcakes/
```

This removes team config, bug journals, rules, workflow state, and feed data for that project only.

### File Permissions

`providers.yaml` contains API keys. Ensure it is readable only by the owner:

```bash
chmod 600 ~/.config/develcakes/providers.yaml
```

DevelCakes warns at startup if this file is group or world-readable.

---

## Project Tab Shows Wrong Content

**Symptom:** Project tab shows messages from the wrong project or no messages at all.

**Fix:**

1. Close the project tab (click × on the tab)
2. Re-open the project from the left panel's Projects tab
3. If the issue persists, check `.crabcakes/team.json` for corrupted entries:

```bash
cat /path/to/project/.crabcakes/team.json
```

4. If corrupted, delete it and re-add agents via the +/− buttons

---

## MiniMax API Errors

### "MiniMax API error (status_code=1004): login fail..."

**Cause:** Invalid MiniMax API key.

**Fix:** Update the `api_key` field for the `minimax` provider in `providers.yaml`.

### "MiniMax API error (status_code=X)"

MiniMax returns body-level errors with HTTP 200. The status codes:
- `1004` — Authentication failure (invalid key)
- `1027` — Rate limit exceeded
- `1039` — Model not found or unavailable

---

## Getting Help

- **Built-in help:** Ask the **Supervisor** (🎩) in its tab — it answers through your configured provider
- **Command help:** Type `/help` in any chat input
- **Debug mode:** Always run with `DEVELCAKES_DEBUG=1` when reporting bugs — the logs are essential for diagnosis
