# Quick Start Guide

## How do I install Develcakes?

See the detailed guide: **[Installing Develcakes](install.md)**

```bash
git clone https://github.com/qsmtco/develcakes.git
cd develcakes
sudo apt install python3-gi python3-gi-cairo libgtk-4-dev libadwaita-1-dev
pip install -e .
```

**Requirements:** Python 3.12+, GTK 4.0+, PyGObject. Linux only.

## How do I configure providers?

See **[Configuration Guide](configuration.md)** and **[Providers Guide](providers.md)**.

**The easy way (no YAML):** open **Settings → Providers → `+ Add Provider`**, pick a vendor preset (OpenAI, Anthropic, OpenRouter, Google Gemini), paste your API key, click **Test Connection**, then **Save**. That's the whole setup — Base URL, caller, and default model are pre-filled by the preset, and the preset's model dropdown can load real model IDs from your account. If `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENROUTER_API_KEY` is set in your environment, a one-click chip on the Providers page creates the card for you. A **Custom…** entry covers any OpenAI-compatible endpoint presets don't know.

**The advanced way:** edit `~/.config/develcakes/providers.yaml` directly (the preset UI writes the same file):

```bash
nano ~/.config/develcakes/providers.yaml
```

Add your API key and set the caller field (e.g. `openai`, `openrouter`, `minimax`).

## How do I start Develcakes?

```bash
cd ~/projects/develcakes
python3 main.py
```

Or with debug logging:

```bash
DEVELCAKES_DEBUG=1 python3 main.py
```

## What happens on first launch?

1. `~/.config/develcakes/` is created with default config
2. Add a provider (see above) — every agent, including Auxilium, answers
   through the provider you configure
3. The project/agent panel opens empty until you create a project

## How do I connect to the OpenClaw gateway?

See **[Gateway Guide](gateway.md)**.

1. Start the gateway: `openclaw gateway start`
2. Gateway agents appear in the left panel once the gateway is reachable

## How do I create a project?

1. Click **New Project** in the left panel
2. Enter a project name and path
3. A `.crabcakes/` directory is initialized with project config
4. Add agents with the **+** button
5. Start chatting in the project tab

See **[Projects Guide](projects.md)** for details.

## Where do I get help?

- Ask **Auxilium** (🦀) — click its tab and type your question
- See **[Troubleshooting](troubleshooting.md)** for common errors
- Check logs at `~/.config/develcakes/logs/`
