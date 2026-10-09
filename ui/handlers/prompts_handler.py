# ui/handlers/prompts_handler.py
# Prompts tab logic — favorites, search, last-used tracking.
#
# Thread safety: file I/O is fast and local; no background threads needed.
# All GTK calls stay in the view layer (left_panel.py).
#
# Architecture rule (Section 8.6):
#   - Does NOT import other handlers
#   - Receives callbacks at construction; does not reach out to find them
#   - Owns in-memory state: _favorites, _last_used, _search_query

import os
import time
from typing import Callable

import gi
gi.require_version('Gtk', '4.0')

from utils.prompt_paths import get_project_prompts_dir


class PromptsHandler:
    """
    Prompts tab data and logic — favorites, search, last-used.

    Owns: loaded prompts list (with metadata), favorites set, last-used timestamps.
    Does NOT build widgets — left_panel.py calls handler methods to get rendering
    data, then builds the GTK widgets.

    Args:
        on_refresh_ui:     callback() — rebuild prompt rows in the view
        on_prompt_loaded:  callback(filepath, name, content) — user loaded a prompt
        GLib_module:       GLib reference for future threading needs (unused for now)
    """

    def __init__(
        self,
        *,
        on_refresh_ui: Callable = None,
        on_prompt_loaded: Callable = None,
        GLib_module=None,
    ):
        self._on_refresh_ui = on_refresh_ui
        self._on_prompt_loaded = on_prompt_loaded
        self._GLib = GLib_module

        self._project_path: str | None = None   # set via set_project_path(); None = app-level fallback

        self._prompts = []          # full list with metadata, refreshed on load
        self._favorites = set()     # set of prompt name stems
        self._last_used = {}        # {filename: timestamp}
        self._search_query = ''     # active search filter

    # ── Public API ────────────────────────────────────────────────────────

    def load_prompts(self) -> list[dict]:
        """
        Scan prompts/ directory, sort (favorites first, then alpha),
        apply search filter, return list of prompt dicts.

        Each dict: {name, filepath, content, is_favorite, lines, size, last_used_str}
        """
        from utils import favorites as fav
        self._favorites = fav.load_favorites()
        self._prompts = self._scan_prompts()
        return self._sorted_filtered()

    def toggle_favorite(self, filepath: str) -> bool:
        """
        Toggle star on a prompt. Returns True if now favorited.
        Triggers UI refresh via on_refresh_ui callback.
        """
        from utils import favorites as fav
        stem = os.path.splitext(os.path.basename(filepath))[0]
        is_now_fav = fav.toggle_favorite(stem)
        self._favorites = fav.load_favorites()
        if self._on_refresh_ui:
            self._on_refresh_ui()
        return is_now_fav

    def search(self, query: str) -> list[dict]:
        """
        Set search filter and return filtered prompts list.
        Empty query returns all prompts (favorites first).
        """
        self._search_query = query.strip().lower()
        return self._sorted_filtered()

    def record_usage(self, filepath: str):
        """Track that a prompt was just loaded (for 'X ago' display)."""
        self._last_used[filepath] = time.time()

    def get_last_used_str(self, filepath: str) -> str:
        """Return human-readable 'X ago' string, or '' if never used."""
        if filepath not in self._last_used:
            return ''
        age = time.time() - self._last_used[filepath]
        if age < 60:
            return 'just now'
        elif age < 3600:
            mins = int(age // 60)
            return f'{mins}m ago'
        elif age < 86400:
            hrs = int(age // 3600)
            return f'{hrs}h ago'
        else:
            days = int(age // 86400)
            return f'{days}d ago'

    def get_prompt_content(self, filepath: str) -> tuple[str, str]:
        """
        Return (name, content) for a filepath.
        Used when user loads a prompt into chat input.
        """
        name = os.path.basename(filepath)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except OSError:
            content = ''
        return name, content

    def on_prompt_activated(self, filepath: str):
        """
        Called by view when user double-clicks/activates a prompt row.
        Loads content and fires on_prompt_loaded callback.
        """
        self.record_usage(filepath)
        name, content = self.get_prompt_content(filepath)
        if self._on_prompt_loaded:
            self._on_prompt_loaded(filepath, name, content)

    def set_project_path(self, project_path: str | None) -> None:
        """Update the active project path ('' or None resets to app fallback).

        Caller is expected to trigger load_prompts()/refresh after calling.
        """
        self._project_path = project_path or None

    def import_prompt(self, source_path: str) -> str | None:
        """
        Copy a .md file into the prompts directory.
        Returns the new filepath on success, None on failure or if name conflicts.
        Called by LeftPanel when user selects a file from the import picker.
        """
        import shutil
        from utils.prompt_paths import ensure_project_prompts_dir
        filename = os.path.basename(source_path)
        if not filename.endswith('.md'):
            return None
        # Write-side resolver: creates <project>/.crabcakes/prompts/ for
        # unseeded projects so the import lands in the project library
        # (same single-source-of-truth path as save_as_prompt).
        prompts_dir = ensure_project_prompts_dir(self._project_path)
        dest = os.path.join(prompts_dir, filename)
        if os.path.exists(dest):
            return None  # already exists — don't overwrite
        try:
            shutil.copy2(source_path, dest)
            return dest
        except OSError:
            return None

    # ── Private ────────────────────────────────────────────────────────────

    def _scan_prompts(self) -> list[dict]:
        """Scan prompts/ directory, return list of prompt metadata dicts."""
        prompts_dir = self._get_prompts_dir()
        if not os.path.isdir(prompts_dir):
            return []

        results = []
        for fname in sorted(os.listdir(prompts_dir)):
            if not fname.endswith('.md'):
                continue
            fpath = os.path.join(prompts_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                size = os.path.getsize(fpath)
                with open(fpath, 'r', encoding='utf-8') as f:
                    text = f.read()
                lines = text.count('\n') + (1 if text and not text.endswith('\n') else 0)
            except OSError:
                continue
            results.append({
                'name': fname[:-3],          # strip .md
                'filepath': fpath,
                'content': text,
                'lines': lines,
                'size': size,
            })
        return results

    def _sorted_filtered(self) -> list[dict]:
        """Sort favorites first, then alpha; apply search filter."""
        query = self._search_query
        items = []
        for p in self._prompts:
            name = p['name']
            fpath = p['filepath']
            is_fav = name in self._favorites
            if query and query not in name.lower():
                continue
            items.append({
                **p,
                'is_favorite': is_fav,
                'last_used_str': self.get_last_used_str(fpath),
            })
        # Sort: favorites first, then by name
        items.sort(key=lambda x: (not x['is_favorite'], x['name'].lower()))
        return items

    def _get_prompts_dir(self) -> str:
        """Prompts dir for the active project; app-level fallback when no
        project is wired or the project has no .crabcakes/prompts/ yet."""
        return get_project_prompts_dir(self._project_path)
