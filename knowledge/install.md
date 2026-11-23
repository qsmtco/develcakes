# Installing DevelCakes

This guide walks you through installing DevelCakes on Linux, verifying all dependencies, and fixing common install errors.

**Audience:** First-time users on a fresh system.

**Platform support:** DevelCakes is a **Linux-only** application. The GTK4 toolchain and OpenClaw gateway client target Linux. macOS and Windows are not supported. If you need to run on those platforms, use a Linux VM or WSL2.

---

## Quick Start

If you have a modern Linux system with Python 3.11+ and GTK4 already installed:

```bash
git clone https://github.com/qsmtco/develcakes.git
cd develcakes
pip install -e .
develcakes
```

That's it. The first launch creates `~/.config/develcakes/` with default configs and seeds the three built-in agents (Coder, Debugger, Auxilium).

---

## System Requirements

### Required system packages

DevelCakes depends on GTK4 and GObject introspection. These are **system packages** — they cannot be installed via pip alone. You must install them with your distribution's package manager before running `pip install`.

#### Debian / Ubuntu (12 Bookworm, 22.04+)

```bash
sudo apt update
sudo apt install -y \
    python3 python3-pip python3-venv \
    libgirepository1.0-dev libgtk-4-1 libgtk-4-dev \
    gir1.2-gtk-4.0 gobject-introspection \
    libgirepository-2.0-0
```

#### Fedora (38+)

```bash
sudo dnf install -y \
    python3 python3-pip python3-devel \
    gtk4 gtk4-devel \
    gobject-introspection-devel \
    python3-gobject
```

#### Arch Linux

```bash
sudo pacman -S --needed \
    python python-pip \
    gtk4 gobject-introspection \
    python-gobject
```

### Python version

DevelCakes requires **Python 3.11 or later**. This is enforced in `pyproject.toml`:

```toml
requires-python = ">=3.11"
```

Check your version:

```bash
python3 --version    # must be 3.11 or higher
```

### Python dependencies (installed automatically by pip)

These are listed in `pyproject.toml` and installed when you run `pip install -e .`:

| Package | Minimum version | Why |
|---------|-----------------|-----|
| **PyGObject** | 3.48+ | Python bindings for GTK4 / GObject |
| **websockets** | 12.0+ | WebSocket client for OpenClaw gateway communication |
| **cryptography** | 41.0+ | Ed25519 device authentication for gateway |
| **GitPython** | 3.1+ | Git operations for the code review layer |

### Optional (recommended) packages

| Tool | Install command | Why |
|------|----------------|-----|
| **xvfb** | `sudo apt install xvfb` | Run DevelCakes in headless environments (CI, Docker, servers) |
| **Ollama** | See [ollama.com](https://ollama.com) | Local LLM runtime — lets you run agents without a paid API key |
| **ripgrep** | `sudo apt install ripgrep` | Faster `search_files` tool for the Coder/Debugger agents |
| **PyYAML** | `pip install pyyaml` | Pretty-formatting of `providers.yaml` (falls back to JSON without it) |
| **faster-whisper** | `pip install faster-whisper` | Push-to-talk speech-to-text (voice input) |
| **pyenchant** | `pip install pyenchant` | Spell check in the chat input |
| **Pygments** | `pip install pygments` | Syntax highlighting in code blocks |

---

## Installation Steps

### 1. Clone the repository

```bash
git clone https://github.com/qsmtco/develcakes.git
cd develcakes
```

### 2. (Recommended) Create a virtual environment

This avoids the `externally-managed-environment` error (PEP 668) on modern Linux distributions:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install DevelCakes

```bash
pip install -e .
```

This installs the `develcakes` entry-point script and all Python dependencies.

### 4. Run DevelCakes

```bash
develcakes
```

Or, to run directly from source without installing the entry point:

```bash
python3 main.py
```

---

## First-Launch Experience

When you run `develcakes` for the first time, several things happen automatically:

### 1. Config directory creation

DevelCakes creates `~/.config/develcakes/` with permissions `0o700` (owner-only). This follows the same security model as `~/.ssh/` — the directory contains API keys and should not be readable by other users.

### 2. Default agent seeding

DevelCakes copies built-in agent definitions from `prompts/default_agents/` into `~/.config/develcakes/agents/`:

- `coder.yaml` — The 🛠️ Coder agent
- `debugger.yaml` — The 🐛 Debugger agent
- `auxilium.yaml` — The 🦀 Auxilium agent

**Note:** If you already have agent YAML files in the `agents/` directory, defaults are NOT seeded — your existing configuration is preserved.

### 3. Agent configuration creation

An `agent.json` file is created with example provider configurations (OpenAI, MiniMax). This file gets `chmod 0o600` permissions.

### 4. Auxilium tab opens

The Auxilium 🦀 tab opens automatically (`auto_open: true`). If no verified provider is configured, a first-run wizard may appear.

---

## Verification Steps

After installation, run these checks to confirm everything works:

### Step 1: Check Python and GTK4

```bash
python3 --version
# Expected: 3.11.x or higher

python3 -c "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk; print('GTK OK', Gtk.MAJOR_VERSION, Gtk.MINOR_VERSION)"
# Expected: GTK OK 4 <minor> (e.g., GTK OK 4 12)
```

### Step 2: Check DevelCakes imports

```bash
python3 -c "from ui.window import MainWindow; print('Import OK')"
# Expected: Import OK (no errors, no GUI launches)
```

### Step 3: Launch the app

```bash
develcakes
```

**Expected behavior:**
- The DevelCakes window opens
- The Auxilium 🦀 tab is visible
- The left panel shows Prompts, Agents, and Projects tabs
- No error messages in the terminal

### Step 4: Ask Auxilium a question

In the Auxilium 🦀 tab, type: "How do I configure a provider?"

Auxilium answers through whichever provider you configured in the first-run wizard (Settings → Providers).

### Headless verification (no display)

For CI, Docker, or remote servers:

```bash
xvfb-run -a python3 -c "from ui.window import MainWindow; MainWindow(application=None); print('GUI constructs OK')"
```

---

## Common Install Errors

### `ModuleNotFoundError: No module named 'gi'`

**Cause:** PyGObject is not installed, or the system `libgirepository` is missing.

**Fix (Debian/Ubuntu):**
```bash
sudo apt install libgirepository1.0-dev python3-gi gobject-introspection
pip install --user pygobject
```

**Fix (Fedora):**
```bash
sudo dnf install python3-gobject gobject-introspection-devel
```

### `externally-managed-environment` (PEP 668)

**Cause:** Modern Debian/Ubuntu and Fedora mark the system Python as "externally managed," preventing `pip install` from modifying it.

**Fix (recommended):** Use a virtual environment:
```bash
python3 -m venv ~/.crabcakes-venv
source ~/.crabcakes-venv/bin/activate
pip install -e /path/to/develcakes
develcakes
```

**Fix (quick, not recommended):**
```bash
pip install --user --break-system-packages -e .
```

### `Gtk-CRITICAL **: cannot open display`

**Cause:** No X11 or Wayland display available (headless server).

**Fix:** Run with Xvfb:
```bash
xvfb-run -a develcakes
```

**Fix (Wayland issues):** Force X11 backend:
```bash
GDK_BACKEND=x11 develcakes
```

### `ImportError: libgtk-4.so.1: cannot open shared object file`

**Cause:** GTK 4 runtime libraries not installed.

**Fix (Debian/Ubuntu):**
```bash
sudo apt install libgtk-4-1 libgtk-4-common
sudo ldconfig
```

**Fix (Fedora):**
```bash
sudo dnf install gtk4
```

### `develcakes: command not found`

**Cause:** The entry-point script is in `~/.local/bin` (or your venv `bin/`) but that directory is not on `$PATH`.

**Fix:** Add to your shell config:
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

Or run directly:
```bash
python3 main.py
```

### PyGObject introspection cache errors

**Cause:** Stale or missing GObject introspection typelib cache.

**Fix:**
```bash
rm -rf ~/.cache/g-ir-*
sudo ldconfig
```

---

## Running in a Virtual Environment (Recommended)

A venv isolates DevelCakes and its dependencies from your system Python.

```bash
# Create (one-time)
python3 -m venv ~/develcakes-venv

# Activate (every session)
source ~/develcakes-venv/bin/activate

# Install
pip install -e /path/to/develcakes

# Run
develcakes
```

**Auto-activation alias** (add to `~/.bashrc`):
```bash
alias develcakes='~/develcakes-venv/bin/develcakes'
```

**Cleanup:**
```bash
rm -rf ~/develcakes-venv           # remove the venv
rm -rf ~/.config/develcakes        # reset config (optional)
```

---

## Installing for Development

```bash
git clone https://github.com/qsmtco/develcakes.git
cd develcakes

python3 -m venv .venv
source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run the app
python3 main.py
```

Dev dependencies (`[dev]` extra):
- `pytest>=8.0` — test runner
- `ruff>=0.4` — linter


---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CRABCAKES_PROJECTS_DIR` | `~/projects` | Root directory for the Projects file tree |
| `CRABCAKES_GATEWAY_URL` | `ws://localhost:18789` | OpenClaw gateway WebSocket URL |
| `DEVELCAKES_DEBUG` | (unset) | Set to `1` for verbose debug logging (legacy `CRABCAKES_DEBUG` still honored) |
| `STT_MODEL_SIZE` | `tiny.en` | faster-whisper model size for voice input |
| `XDG_CONFIG_HOME` | `~/.config` | Base config directory (DevelCakes uses `$XDG_CONFIG_HOME/develcakes/`) |

---

## When to Ask for Help

If the install doesn't work after trying the fixes above:

1. Set `DEVELCAKES_DEBUG=1` for verbose logs: `DEVELCAKES_DEBUG=1 develcakes`
2. Check `knowledge/troubleshooting.md` for known issues
3. Search existing issues: https://github.com/qsmtco/develcakes/issues
4. File a new issue with: OS + version, Python version, GTK version, full error output
