# agent/context.py
# System prompt + file context builder for the agent runtime.
#
# Manifest:
#   - Reads: project files, .gitignore, prompts/system/ templates
#   - No network, no GTK, no config files outside the project
#   - No stateful mutations — pure functions
#
# Architecture: this module is intentionally dependency-free (stdlib only).
# Any runtime that needs context should call helpers from here.

from __future__ import annotations

import logging
import os
import re
from fnmatch import translate
from typing import Callable

from utils.config import get_env  # SPEC-11 SP2: renamed env family (D2)

# ── System prompts now loaded from prompts/system/ via utils/prompt_loader.py ──

# ── Gitignore ─────────────────────────────────────────────────────────────────


def _load_gitignore_patterns(project_path: str) -> list[str]:
    """
    Load .gitignore patterns from project root.
    Returns a list of pattern strings (not fnmatch-based - actual gitignore syntax).
    """
    gitignore_path = os.path.join(project_path, ".gitignore")
    if not os.path.isfile(gitignore_path):
        return []

    try:
        with open(gitignore_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []

    patterns: list[str] = []
    for line in lines:
        stripped = line.strip()
        # Skip empty lines and comments
        if not stripped or stripped.startswith("#"):
            continue
        # Negation — store as-is, fnmatch doesn't support this directly
        # but we handle it separately
        patterns.append(stripped)
    return patterns


def _compile_gitignore(patterns: list[str]) -> Callable[[str], bool]:
    """Compile .gitignore patterns into one predicate equal to _match_gitignore.

    fnmatch() normalizes with os.path.normcase, the identity on POSIX, so
    re.match(translate(p)) is the same test it performed per pattern.
    """
    rules: list[tuple[bool, str]] = []
    for pattern in patterns:
        negated = pattern.startswith("!")
        active = pattern[1:] if negated else pattern
        if active.endswith("/"):
            active = active[:-1]
            if "/" in active:
                continue
        rules.append((negated, active))
    if not any(negated for negated, _ in rules):
        if not rules:
            return lambda name: False
        combined = re.compile("|".join(translate(active) for _, active in rules))
        return lambda name: combined.match(name) is not None
    compiled = [(negated, re.compile(translate(active))) for negated, active in rules]

    def match(name: str) -> bool:
        for negated, rx in compiled:
            if rx.match(name):
                return not negated
        return False

    return match


# SPEC-21 SP2: compile-once matcher + per-name memo, keyed on the pattern
# tuple so a .gitignore edit produces a new entry. Concurrent first use
# may compile twice; both predicates are equivalent.
_GITIGNORE_MATCHERS: dict[tuple[str, ...], tuple[Callable[[str], bool], dict[str, bool]]] = {}


def _match_gitignore(name: str, patterns: list[str], anchored: bool = False) -> bool:
    """
    Return True if name matches any non-negated .gitignore pattern.
    Patterns are compiled once (SPEC-21 SP2) with the same truth table as
    the previous per-call fnmatch loop. `anchored` is unused; kept for the
    public signature.
    """
    key = tuple(patterns)
    entry = _GITIGNORE_MATCHERS.get(key)
    if entry is None:
        entry = _GITIGNORE_MATCHERS[key] = (_compile_gitignore(patterns), {})
    matcher, seen = entry
    hit = seen.get(name)
    if hit is None:
        if len(seen) >= 50_000:
            seen.clear()
        hit = seen[name] = matcher(name)
    return hit


def _is_ignored(rel_path: str, project_path: str, patterns: list[str]) -> bool:
    """
    Check if a relative path should be ignored based on .gitignore patterns.
    For a project-root .gitignore, patterns with / are anchored to depth 1.
    Patterns without / match any path segment at any depth.
    """
    parts = rel_path.replace("\\", "/").split("/")
    depth = len(parts)

    for i, part in enumerate(parts):
        # Check segment at its depth level (for anchored patterns like src/)
        if i == 0 and "/" not in rel_path:
            # Single segment — check against patterns with /
            if _match_gitignore(part + "/", patterns):
                return True
        # Check without anchor (unanchored patterns match anywhere)
        if _match_gitignore(part, patterns):
            return True
    return False


# ── File context builder ──────────────────────────────────────────────────────


def _build_directory_tree(project_path: str, max_lines: int = 200) -> str:
    """
    Build a directory tree string for the project.
    Skips ignored files/directories. Respects .gitignore.
    Returns a multi-line string like tree(1) output.
    """
    patterns = _load_gitignore_patterns(project_path)
    lines: list[str] = []
    prefix_skip = {"__pycache__", ".git", "node_modules", ".pytest_cache", ".mypy_cache", ".tox", "docs", ".docs", "documentation"}

    def add_dir(rel_dir: str, indent: str) -> None:
        """Add entries for one directory level and recurse into subdirs."""
        if len(lines) >= max_lines:
            return
        try:
            entries = sorted(
                os.listdir(os.path.join(project_path, rel_dir) if rel_dir else project_path)
            )
        except OSError:
            return

        for name in entries:
            full = os.path.join(rel_dir, name) if rel_dir else name
            if _is_ignored(full, project_path, patterns):
                continue
            if name in prefix_skip or name.startswith("."):
                continue
            full_path = os.path.join(project_path, full)
            if os.path.isdir(full_path):
                lines.append(f"{indent}{name}/")
                add_dir(full, indent + "  ")
            else:
                lines.append(f"{indent}{name}")

    add_dir("", "")
    return "\n".join(lines)


def _read_crabcakes_docs(project_path: str, max_size: int = 50 * 1024) -> str:
    """
    Read all .crabcakes/ project documentation files.

    These are always small and always relevant — architecture, requirements,
    context, tasks, team, workflow. Included first in every file context
    so the agent always has project docs before exploring the tree.

    Args:
        project_path: Absolute path to the project root.
        max_size: Skip files larger than this.

    Returns:
        Concatenated sections, each prefixed with ``## .crabcakes/{name}``.
        Empty string if .crabcakes/ does not exist.
    """
    crab_dir = os.path.join(project_path, ".crabcakes")
    if not os.path.isdir(crab_dir):
        return ""

    DOC_NAMES = (
        "architecture.md", "requirements.md", "context.md",
        "tasks.md", "team.json", "workflow.md",
        "awareness.json", "project.md",
    )

    sections: list[str] = []
    try:
        entries = os.listdir(crab_dir)
    except OSError:
        return ""

    for name in entries:
        if name not in DOC_NAMES:
            continue
        file_path = os.path.join(crab_dir, name)
        if not os.path.isfile(file_path):
            continue
        try:
            size = os.path.getsize(file_path)
            if size > max_size:
                content = f"[File too large to display — {size // 1024}KB]"
            else:
                with open(file_path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
        except OSError:
            continue
        sections.append(f"## .crabcakes/{name}\n\n{content}\n")

    return "\n".join(sections)


_PROJECT_PROMPTS_CONTEXT_CAP = 20 * 1024  # 20KB per prompt
_PROJECT_PROMPTS_MAX_FILES = 30


def _load_project_prompts_context(project_path: str) -> str:
    """
    Read .crabcakes/prompts/*.md into the agent's file context.

    Each file becomes a ``## .crabcakes/prompts/{stem}`` section, capped at
    20KB per file and 30 files total. The size filter runs BEFORE the count
    cap, so oversized files never consume a slot — a project with 30 eligible
    files always sees all 30 (Phase 7 audit BUG #5). Subdirectories (e.g.
    default_agents/) are NOT included here — those are loaded by other code
    paths and including them would double the context size.

    Returns empty string if the directory does not exist.
    """
    prompts_dir = os.path.join(project_path, ".crabcakes", "prompts")
    if not os.path.isdir(prompts_dir):
        return ""
    sections: list[str] = []
    try:
        files = sorted(
            f for f in os.listdir(prompts_dir)
            if f.endswith(".md") and os.path.isfile(os.path.join(prompts_dir, f))
        )
    except OSError:
        return ""
    # Size filter FIRST, then count cap — oversized files are skipped without
    # burning one of the 30 slots (Phase 7 audit BUG #5, supervisor override
    # of the doc-only recommendation).
    kept: list[str] = []
    for fname in files:
        try:
            if os.path.getsize(
                os.path.join(prompts_dir, fname)
            ) <= _PROJECT_PROMPTS_CONTEXT_CAP:
                kept.append(fname)
        except OSError as e:
            # Deleted/unreadable between listdir and stat (TOCTOU) — skip,
            # but log so the silent-drop window is at least visible.
            logging.getLogger(__name__).debug(
                "Skipping prompt %s: %s", fname, e
            )
            continue
    for fname in kept[:_PROJECT_PROMPTS_MAX_FILES]:
        fpath = os.path.join(prompts_dir, fname)
        try:
            if os.path.getsize(fpath) > _PROJECT_PROMPTS_CONTEXT_CAP:
                continue
            with open(fpath, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        sections.append(f"## .crabcakes/prompts/{fname[:-3]}\n\n{content}\n")
    return "\n".join(sections)


# Directories excluded from file context by default.
# Set DEVELCAKES_INCLUDE_DOCS=1 to override (old CRABCAKES_ name rides the
# one-release fallback — utils.config.get_env).
EXCLUDED_DIRS = frozenset({"docs", ".docs", "documentation"})


def _include_docs() -> bool:
    """Whether to include the docs/ directory in file context. Defaults False."""
    return get_env("INCLUDE_DOCS") == "1"

def _read_key_files(project_path: str) -> str:
    """
    Read key project files for context: README, ARCHITECTURE, package.json, etc.
    Skips files > 50KB. Respects .gitignore.
    """
    patterns = _load_gitignore_patterns(project_path)
    key_names = {
        "README.md", "README.txt", "README",
        "ARCHITECTURE.md", "ARCHITECTURE.txt",
        "package.json", "pyproject.toml", "Cargo.toml",
        "Makefile", "Dockerfile", ".env.example",
        "AGENTS.md",
    }
    MAX_FILE_SIZE = 50 * 1024  # 50 KB per file
    sections: list[str] = []

    try:
        entries = os.listdir(project_path)
    except OSError:
        return ""

    include_docs = _include_docs()
    for name in entries:
        if name not in key_names:
            continue
        rel_path = name
        # Skip docs/ unless explicitly enabled
        if not include_docs and name in EXCLUDED_DIRS:
            continue
        if _is_ignored(rel_path, project_path, patterns):
            continue
        file_path = os.path.join(project_path, name)
        if not os.path.isfile(file_path):
            continue
        try:
            size = os.path.getsize(file_path)
            if size > MAX_FILE_SIZE:
                content = f"[File too large to display — {size // 1024}KB]"
            else:
                with open(file_path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
        except OSError:
            continue

        sections.append(f"## {name}\n\n{content}\n")

    return "\n".join(sections)


_FILE_CONTEXT_CACHE: dict[str, tuple[float, str]] = {}


def _project_root_mtime(project_path: str) -> float:
    """Max mtime of files affecting file context cache."""
    try:
        m = os.stat(project_path).st_mtime
        for name in (".crabcakes", "README.md", "ARCHITECTURE.md", "AGENTS.md"):
            full = os.path.join(project_path, name)
            if os.path.exists(full):
                m = max(m, os.stat(full).st_mtime)
        # Per-project prompts directory (SPEC-PROJECT-PROMPTS-DIRECTORY §2.9)
        prompts_dir = os.path.join(project_path, ".crabcakes", "prompts")
        if os.path.isdir(prompts_dir):
            try:
                for entry in os.listdir(prompts_dir):
                    full = os.path.join(prompts_dir, entry)
                    if os.path.isfile(full):
                        m = max(m, os.stat(full).st_mtime)
            except OSError:
                pass
        return m
    except OSError:
        return 0.0


def build_file_context(
    project_path: str,
    query: str | None = None,
    max_chars: int = 50_000,
) -> str:
    """
    Build a file context block for an LLM prompt.

    Strategy:
    - With query: include files matching the query (by name)
    - Without query: directory tree + key files (README, ARCHITECTURE, etc.)
    - Respects .gitignore
    - Total context capped at ~max_chars

    Args:
        project_path: Absolute path to the project root.
        query: Optional search query — if provided, only matching files are included.
        max_chars: Maximum total context length (default 50K).

    Returns:
        Formatted text block, truncated to max_chars if needed.
    """
    if not project_path or not os.path.isdir(project_path):
        return ""

    # Cache check (only for non-query calls)
    if not query:
        cache_key = f"{project_path}::{max_chars}"
        mtime = _project_root_mtime(project_path)
        cached = _FILE_CONTEXT_CACHE.get(cache_key)
        if cached and cached[0] >= mtime:
            return cached[1]

    patterns = _load_gitignore_patterns(project_path)
    parts: list[str] = []

    # §4.4a quick win: always include .crabcakes/ project docs first
    crab_docs = _read_crabcakes_docs(project_path)
    if crab_docs:
        parts.append(f"## Project docs\n\n{crab_docs}\n\n")

    # Include per-project prompts library (SPEC-PROJECT-PROMPTS-DIRECTORY
    # §2.9) — project prompt library after the core docs: methodology first,
    # then reference material, then the tree.
    prompts_ctx = _load_project_prompts_context(project_path)
    if prompts_ctx:
        parts.append(f"## Project prompts\n\n{prompts_ctx}\n\n")

    if query:
        # Query mode: find files matching the query string
        matches = _find_matching_files(project_path, query, patterns)
        if not matches:
            return f"(No files match query: {query})"
        parts.append("## Matching files\n")
        for rel_path in matches:
            content = _read_file_safe(os.path.join(project_path, rel_path))
            if content is None:
                continue
            parts.append(f"## {rel_path}\n\n{content}\n")
    else:
        # No query: directory tree + key files
        tree = _build_directory_tree(project_path)
        parts.append(f"## Project tree\n\n{tree}\n\n")
        key_files = _read_key_files(project_path)
        if key_files:
            parts.append(f"## Key files\n\n{key_files}\n")

    full = "".join(parts)

    # Truncate if needed — preserve header if possible
    if len(full) <= max_chars:
        if not query:
            cache_key = f"{project_path}::{max_chars}"
            _FILE_CONTEXT_CACHE[cache_key] = (_project_root_mtime(project_path), full)
        return full

    # Truncate, keeping as much as possible from the end (most recent context)
    truncated = full[:max_chars] + f"\n\n[... file context truncated to {max_chars} chars ...]"
    if not query:
        cache_key = f"{project_path}::{max_chars}"
        _FILE_CONTEXT_CACHE[cache_key] = (_project_root_mtime(project_path), truncated)
    return truncated


# Core files that are never truncated from the file context.
# Per the Phase CB-2 spec, these are hard-coded for v1.
# The order here is the order they appear in the file context.
CORE_FILES = [
    "README.md",
    "AGENTS.md",
    "CONVENTIONS.md",
    "ARCHITECTURE.md",
]


def resolve_context_mode(
    explicit_mode: str,
    model_max_tokens: int | None,
) -> str:
    """Resolve the effective context mode based on provider configuration.

    v1: resolves at conversation-creation time only, using model_max_tokens.
    Mid-session escalation (turn_count, token_estimate) is deferred to P10.8.

    Args:
        explicit_mode: One of "auto", "preload", "jit", "hybrid".
            Case-insensitive; whitespace is stripped. "auto" is resolved by
            this function.
        model_max_tokens: Model context window from ProviderConfig.
            If None, 0, or negative, defaults to 128_000 for heuristics.

    Returns:
        One of "preload", "hybrid", "jit".
    """
    # Normalize input via the shared validator (case-insensitive, strips whitespace)
    from models.providers import validate_provider_context_mode
    explicit_mode = validate_provider_context_mode(explicit_mode)

    if explicit_mode in ("preload", "jit", "hybrid"):
        return explicit_mode
    # explicit_mode is "auto" — resolve by model context window size
    if model_max_tokens is not None and model_max_tokens <= 0:
        # Negative/zero window → unknown, use balanced default
        return "hybrid"
    window = model_max_tokens or 128_000
    if window >= 500_000:
        return "preload"   # large window — convenience wins
    if window <= 32_000:
        return "jit"       # small window — every token counts
    return "hybrid"        # typical 128K–256K — balanced


def build_file_index(
    project_path: str,
    max_entries: int = 200,
    include_line_counts: bool = True,
) -> str:
    """Build a compact file index for the system prompt.

    Walks the project tree (respecting .gitignore via _load_gitignore_patterns
    and the EXCLUDED_DIRS frozenset), groups files by extension, shows path +
    size + (optionally) line count. Capped at max_entries; if more files exist,
    appends a directory-level summary.

    Args:
        project_path: Absolute path to the project root.
        max_entries: Maximum number of files to list (default 200).
        include_line_counts: Whether to count lines per file (default True).
            Setting False reduces I/O for very large projects.

    Returns:
        Formatted text block. Empty string if project_path is invalid.
    """
    if not project_path or not os.path.isdir(project_path):
        return ""

    patterns = _load_gitignore_patterns(project_path)

    # Collect all files
    all_files: list[tuple[str, int]] = []  # (rel_path, size)
    dir_stats: dict[str, list[int]] = {}   # top_dir -> [file_count, total_size]

    for root, dirs, files in os.walk(project_path):
        rel_root = os.path.relpath(root, project_path)
        if _is_ignored(rel_root, project_path, patterns):
            dirs[:] = []
            continue

        dirs[:] = [
            d for d in dirs
            if not d.startswith(".") and d not in {
                "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".tox",
            }
        ]

        for name in files:
            if name.startswith("."):
                continue
            rel_path = os.path.join(rel_root, name) if rel_root != "." else name
            if _is_ignored(rel_path, project_path, patterns):
                continue
            try:
                size = os.path.getsize(os.path.join(root, name))
            except OSError:
                size = 0
            all_files.append((rel_path, size))

            # Track directory stats for large-project summary
            top_dir = rel_path.split("/")[0] if "/" in rel_path else "(root)"
            if top_dir not in dir_stats:
                dir_stats[top_dir] = [0, 0]
            dir_stats[top_dir][0] += 1
            dir_stats[top_dir][1] += size

    if not all_files:
        return ""

    # Sort by size descending
    all_files.sort(key=lambda x: x[1], reverse=True)

    # Determine which files to show and which are truncated
    shown_files = all_files[:max_entries]
    omitted_files = all_files[max_entries:]

    # Group shown files by extension
    ext_groups: dict[str, list[tuple[str, int]]] = {}
    for rel_path, size in shown_files:
        if "." in os.path.basename(rel_path):
            ext = rel_path.rsplit(".", 1)[-1].lower()
        else:
            ext = "Other"
        if ext not in ext_groups:
            ext_groups[ext] = []
        ext_groups[ext].append((rel_path, size))

    # Build output
    total_count = len(all_files)
    lines: list[str] = [f"## File index ({total_count:,} files)"]

    # Order extension groups: Python first, then alphabetical
    ext_order = sorted(ext_groups.keys(), key=lambda e: (e != "py", e))

    for ext in ext_order:
        files_in_group = ext_groups[ext]
        group_label = ext.upper() if len(ext) <= 4 else ext.capitalize()
        lines.append(f"### {group_label} ({len(files_in_group)} files)")
        for rel_path, size in files_in_group:
            # Size formatting
            if size < 1024 * 1024:
                size_str = f"{size // 1024}KB"
            else:
                size_str = f"{size // (1024 * 1024)}MB"

            # Line count (best-effort) — skip for large files to avoid I/O cost
            line_str = ""
            if include_line_counts and size <= 1_000_000:
                try:
                    full_path = os.path.join(project_path, rel_path)
                    with open(full_path, "rb") as f:
                        data = f.read()
                    lc = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
                    line_str = f"{lc:,} lines / "
                except (OSError, UnicodeDecodeError):
                    pass  # binary or unreadable — skip line count

            # Format: path ............. N lines / size
            # Pad path for alignment
            display_path = rel_path
            if len(display_path) > 50:
                display_path = "..." + display_path[-47:]
            pad = max(2, 50 - len(display_path))
            lines.append(f"{display_path}{'.' * pad}{line_str}{size_str}")
        lines.append("")

    # Directory summary for large projects
    if omitted_files:
        omitted_count = len(omitted_files)
        # Recompute dir stats for omitted files only
        omitted_dirs: dict[str, list[int]] = {}
        for rel_path, size in omitted_files:
            top_dir = rel_path.split("/")[0] if "/" in rel_path else "(root)"
            if top_dir not in omitted_dirs:
                omitted_dirs[top_dir] = [0, 0]
            omitted_dirs[top_dir][0] += 1
            omitted_dirs[top_dir][1] += size

        num_dirs = len(omitted_dirs)
        lines.append(f"[... and {omitted_count:,} more files across {num_dirs} directories. Top directories:]")

        sorted_dirs = sorted(omitted_dirs.items(), key=lambda x: x[1][0], reverse=True)
        for dir_name, stats in sorted_dirs[:10]:
            fc, ts = stats
            if ts < 1024 * 1024:
                ts_str = f"{ts // 1024}KB"
            else:
                ts_str = f"{ts // (1024 * 1024)}MB"
            pad = max(2, 20 - len(dir_name))
            lines.append(f"{dir_name}/{'.' * pad}{fc:,} files / {ts_str}")
        lines.append('[Use file_search("symbol") to find specific files within these directories.]')
        lines.append("")

    return "\n".join(lines)


def build_file_context_with_core_files(
    project_path: str,
    query: str | None = None,
    max_chars: int = 50_000,
    *,
    context_mode: str = "preload",
) -> str:
    """
    Build a file context block with core files preserved at the end.

    Mode behavior:
    - "preload" (default): existing behavior — full file context + core files
    - "jit": replace base context with file index; do NOT include core files
    - "hybrid": core files + file index (replacing base file context)

    All other behavior (gitignore, .crabcakes/ docs, CB-5 core file
    preservation) is unchanged.
    """
    # Validate and normalize mode (case-insensitive)
    from models.providers import validate_provider_context_mode
    context_mode = validate_provider_context_mode(context_mode)
    if context_mode == "auto":
        # Should have been resolved by caller; treat as hybrid fallback
        context_mode = "hybrid"

    if not project_path or not os.path.isdir(project_path):
        return ""

    if context_mode == "preload":
        # Existing behavior — unchanged
        base_context = build_file_context(project_path, query=query, max_chars=max_chars)
        if not base_context:
            return ""
        core_sections = []
        for core_file in CORE_FILES:
            core_path = os.path.join(project_path, core_file)
            content = _read_file_safe(core_path)
            if content:
                core_sections.append(f"## {core_file}\n\n{content}\n")
            elif os.path.isfile(core_path):
                size = os.path.getsize(core_path)
                core_sections.append(
                    f"## {core_file}\n\n[{size // 1024}KB — too large for inline; "
                    f'use read_file("{core_file}") to read in full]\n'
                )
        if not core_sections:
            return base_context
        core_block = "\n".join(core_sections)
        return base_context + "\n\n" + core_block

    # jit or hybrid
    file_index = build_file_index(project_path)
    if context_mode == "jit":
        # JIT: index only, no core files
        return file_index

    # hybrid: core files + index
    core_sections = []
    for core_file in CORE_FILES:
        core_path = os.path.join(project_path, core_file)
        content = _read_file_safe(core_path)
        if content:
            core_sections.append(f"## {core_file}\n\n{content}\n")
        elif os.path.isfile(core_path):
            size = os.path.getsize(core_path)
            core_sections.append(
                f"## {core_file}\n\n[{size // 1024}KB — too large for inline; "
                f'use read_file("{core_file}") to read in full]\n'
            )
    if not core_sections:
        return file_index
    return "\n".join(core_sections) + "\n\n" + file_index


def _find_matching_files(
    project_path: str,
    query: str,
    patterns: list[str],
    max_files: int = 20,
    max_total_chars: int = 40_000,
) -> list[str]:
    """Find files matching query (by name) within project."""
    query_lower = query.lower()
    matches: list[tuple[str, int]] = []  # (rel_path, size)
    total = 0

    for root, dirs, files in os.walk(project_path):
        # Prune ignored dirs in-place
        rel_root = os.path.relpath(root, project_path)
        if _is_ignored(rel_root, project_path, patterns):
            dirs[:] = []
            continue

        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {
            "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".tox",
            "docs", ".docs", "documentation"
        }]

        for name in files:
            if name.startswith("."):
                continue
            rel_path = os.path.join(rel_root, name) if rel_root != "." else name
            if _is_ignored(rel_path, project_path, patterns):
                continue
            if query_lower in name.lower():
                try:
                    size = os.path.getsize(os.path.join(root, name))
                except OSError:
                    size = 0
                if total + size > max_total_chars and len(matches) >= 3:
                    continue
                matches.append((rel_path, size))
                total += size
                if len(matches) >= max_files:
                    return [m[0] for m in matches]

    # Sort by size descending (larger/more important files first)
    matches.sort(key=lambda x: x[1], reverse=True)
    return [m[0] for m in matches]


def _read_file_safe(path: str, max_size: int = 50 * 1024) -> str | None:
    """Read a file safely, returning None on error or if too large."""
    try:
        if os.path.getsize(path) > max_size:
            return None
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


# ── System prompt builder ─────────────────────────────────────────────────────


def build_system_prompt(
    agent_name: str,
    project_path: str | None,
    tools: list[str],
    review_mode: str = "off",
    agent_role: str = "",
    model_max_tokens: int | None = None,
    *,
    context_mode: str = "auto",
) -> str:
    """
    Build the system prompt for an agent.

    Uses templates from prompts/system/ composed by utils/prompt_loader.
    Falls back to a minimal hardcoded prompt if templates are unavailable.

    Args:
        agent_name: Display name of the agent (e.g. "Coder").
        project_path: Absolute path to the project root.
        tools: List of tool names this agent can use.
        review_mode: "off" | "review" — controls write permission awareness.
        agent_role: Explicit role override (one of "coder", "debugger",
            "helper", "supervisor"; or "" for gateway agents). When empty,
            derives from agent_name as a fallback.
        context_mode: Context discovery mode — "auto", "preload", "jit", or "hybrid".
            "auto" resolves based on model_max_tokens at creation time.

    Returns:
        Formatted system prompt string.
    """
    # Build awareness dict if project active
    awareness_dict = None
    if project_path:
        try:
            from utils.project_awareness import build_awareness_dict
            awareness_dict = build_awareness_dict(project_path)
        except Exception:
            pass  # Non-fatal

    # Use template system
    try:
        from utils.prompt_loader import compose_system_prompt
        prompt = compose_system_prompt(
            agent_name=agent_name,
            agent_role=agent_role or (
                "coder" if "coder" in agent_name.lower() else
                "debugger" if "debugger" in agent_name.lower() else
                "supervisor" if "supervisor" in agent_name.lower() else ""
            ),
            project_path=project_path,
            project_awareness=awareness_dict,
            tools=tools,
            review_mode=review_mode,
            model_max_tokens=model_max_tokens,
            context_mode=context_mode,
        )
        if prompt:
            return prompt
    except Exception:
        pass  # Fall through to minimal fallback

    # Minimal fallback if template system unavailable
    tool_list = "\n".join(f"  - {t}" for t in tools) if tools else "  (no tools)"
    return f"You are {agent_name}.\n\nTools:\n{tool_list}"
