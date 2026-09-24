# utils/config.py — Centralized configuration path resolution
#
# Manifest: reads environment variables only, no file I/O, no network
# Single source of truth for all config and data directory paths.
#
# Architecture: this module is intentionally dependency-free. No GTK, no network.
# Any package that needs a config path should call helpers from here instead
# of computing paths inline. If the config root location ever changes, update
# this module and all callers are automatically correct.

import os


def get_config_dir() -> str:
    """Return the CrabCakes config directory.

    Respects $XDG_CONFIG_HOME if set, otherwise ~/.config/crabcakes.
    Does NOT create the directory.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return os.path.join(xdg, "crabcakes")
    return os.path.join(os.path.expanduser("~"), ".config", "crabcakes")


def get_config_file() -> str:
    """Return path to config.json (API keys, base URLs, etc.)."""
    return os.path.join(get_config_dir(), "config.json")


def get_projects_config_dir() -> str:
    """Return path to projects config directory (members.json files live here).

    Located inside the CrabCakes config dir, NOT inside the browsable projects root.
    """
    return os.path.join(get_config_dir(), "projects")


def get_projects_dir() -> str:
    """Return the browsable projects directory (actual project folders).

    Controlled by $CRABCAKES_PROJECTS_DIR, defaults to ~/projects.
    This is the root that the FileTree widget navigates.
    """
    return os.environ.get(
        "CRABCAKES_PROJECTS_DIR",
        os.path.join(os.path.expanduser("~"), "projects"),
    )


def get_project_root() -> str:
    """Return the CrabCakes repository root (the directory containing main.py).

    Derived from this file's location (utils/config.py -> parent of utils/), so
    it is correct regardless of the current working directory and regardless of
    where the checkout lives. Use this instead of hardcoding an absolute path
    to the repo when locating bundled assets (icons/, prompts/, knowledge/).

    Note: an editable install (pip install -e .) keeps the repo layout, so this
    resolves to the checkout. A non-editable install resolves to the installed
    package directory, where bundled data files are only present if declared as
    package data.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Command system configuration
# Backtick prefix — triggers command parsing in ChatHandler.on_send().
# Distinct from slash commands (/approve, /status, etc.) which use "/".
COMMAND_PREFIX = "/"

