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
# Hybrid and JIT append names only. The section is the header plus stems,
# cut to the alphabetically first names when it would pass this cap.
_PROJECT_PROMPT_NAME_SECTION_CAP = 2000
_PROMPT_NAME_MORE = (
    "[\u2026 {n} more prompts; open .crabcakes/prompts/ with read_file]"
)


def _eligible_project_prompt_files(project_path: str) -> list[str]:
    """Sorted ``.crabcakes/prompts/*.md`` names that pass the size cap.

    ``listdir`` and ``stat`` only. Oversized files are skipped and do not
    consume one of the 30 slots. The files are not opened.
    """
    prompts_dir = os.path.join(project_path, ".crabcakes", "prompts")
    if not os.path.isdir(prompts_dir):
        return []
    try:
        names = os.listdir(prompts_dir)
    except OSError:
        return []
    kept: list[str] = []
    for fname in names:
        if not fname.endswith(".md"):
            continue
        full = os.path.join(prompts_dir, fname)
        if not os.path.isfile(full):
            continue
        try:
            size = os.path.getsize(full)
        except OSError as e:
            logging.getLogger(__name__).debug(
                "Skipping prompt %s: %s", fname, e
            )
            continue
        if size <= _PROJECT_PROMPTS_CONTEXT_CAP:
            kept.append(fname)
    kept.sort()
    return kept


def _load_project_prompts_context(
    project_path: str,
    max_chars: int | None = None,
) -> str:
    """
    Read .crabcakes/prompts/*.md into the agent's file context.

    Each file becomes a ``## .crabcakes/prompts/{stem}`` section, capped at
    20KB per file and 30 files total. The size filter runs BEFORE the count
    cap, so oversized files never consume a slot — a project with 30 eligible
    files always sees all 30 (Phase 7 audit BUG #5). Subdirectories (e.g.
    default_agents/) are NOT included here — those are loaded by other code
    paths and including them would double the context size.

    ``max_chars`` (CONTEXT-EFFICIENCY-INSTRUCTIONS §7) is the file-context
    budget still available to prompt bodies. A file is only opened when the
    remaining budget can hold it, and the walk stops as soon as the running
    total reaches the budget. Without it this still read 30 files (124KB on
    this repo) and let the caller discard all but the head. ``None`` means
    no budget, which is the historical behavior.

    Returns empty string if the directory does not exist.
    """
    prompts_dir = os.path.join(project_path, ".crabcakes", "prompts")
    if not os.path.isdir(prompts_dir):
        return ""
    sections: list[str] = []
    # Size filter FIRST, then count cap — oversized files are skipped without
    # burning one of the 30 slots (Phase 7 audit BUG #5, supervisor override
    # of the doc-only recommendation). The list is listdir + stat; this
    # function is the one that opens bodies, and only on the preload path.
    kept = _eligible_project_prompt_files(project_path)
    # Budgeted read: a prompt file is only opened when the budget still has
    # room for it, and the walk stops the moment the budget is spent. This
    # is what stops preload from reading 124KB and keeping the wrong half.
    remaining = max_chars
    for fname in kept[:_PROJECT_PROMPTS_MAX_FILES]:
        if remaining is not None and remaining <= 0:
            break
        fpath = os.path.join(prompts_dir, fname)
        try:
            size = os.path.getsize(fpath)
            if size > _PROJECT_PROMPTS_CONTEXT_CAP:
                continue
            if remaining is not None and size > remaining:
                # Not enough room for this file — skip it and keep walking.
                # The walk is sorted by NAME, not size, so a smaller file
                # later in the alphabet can still fit the remaining budget;
                # breaking here dropped every later file too (on this repo a
                # 7,651-byte README.md sorted first and zero prompt sections
                # survived). The budget-exhausted case above is the only one
                # that stops the walk.
                continue
            with open(fpath, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        sections.append(f"## .crabcakes/prompts/{fname[:-3]}\n\n{content}\n")
        if remaining is not None:
            # Account for the section header and separators, not just the
            # body, or the running total under-counts by ~30 chars a file.
            remaining -= len(sections[-1]) + 1
    return "\n".join(sections)


def _project_prompt_name_section(project_path: str) -> str:
    """Stems of eligible project prompts. No file bodies.

    Missing ``prompts/`` directory, or no file under the size cap, yields
    an empty string and no header. The section keeps the alphabetically
    first names, at most 30, and at most 2,000 characters.
    """
    stems = [name[:-3] for name in _eligible_project_prompt_files(project_path)]
    if not stems:
        return ""
    header = "## Project prompts"
    limit = min(len(stems), _PROJECT_PROMPTS_MAX_FILES)
    while True:
        omitted = len(stems) - limit
        lines = [header, *stems[:limit]]
        if omitted:
            lines.append(_PROMPT_NAME_MORE.format(n=omitted))
        text = "\n".join(lines)
        if len(text) <= _PROJECT_PROMPT_NAME_SECTION_CAP or limit == 0:
            return text
        limit -= 1


def _append_project_prompt_names(base: str, project_path: str) -> str:
    """Append the prompt-name section. Preload does not call this."""
    section = _project_prompt_name_section(project_path)
    if not section:
        return base
    if not base:
        return section
    return f"{base}\n\n{section}"


# Directories excluded from file context by default.
# Set DEVELCAKES_INCLUDE_DOCS=1 to override (old CRABCAKES_ name rides the
# one-release fallback — utils.config.get_env).
EXCLUDED_DIRS = frozenset({"docs", ".docs", "documentation"})


def _include_docs() -> bool:
    """Whether to include the docs/ directory in file context. Defaults False."""
    return get_env("INCLUDE_DOCS") == "1"


# Non-text extensions: build_file_index neither lists nor line-counts these
# (CONTEXT-EFFICIENCY-INSTRUCTIONS §1). Newline counts on binaries were
# garbage ("A PNG was reported as 139 lines") and the reads were pure waste.
BINARY_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "gif", "webp", "ico", "pdf", "zip", "gz", "tar",
    "wasm", "so", "dll", "exe",
})

# Top-level directories that lose file-index slots to source directories
# (CONTEXT-EFFICIENCY-INSTRUCTIONS §1): they are listed only after every
# other top-level directory, and size is a tiebreak within — not across — a
# directory. docs/ is normally walked away entirely (EXCLUDED_DIRS); it stays
# deprioritized when explicitly included via _include_docs().
LOW_PRIORITY_DIRS = frozenset({"tests", "docs", "prompts"})


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
    - Total context capped at ~max_chars. On overflow the drop order is
      prompt bodies first, then the tree/key files, project docs last
      (§4.4a always includes project docs)

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

    # §7: the assembly cap reserves the tail. Prompt bodies sit at the front
    # and are what a tail cut drops first. Docs stay ahead of the tree
    # (§4.4a). The tree and key files are last, so both this cut and the
    # later end-preserving smart truncator keep them.
    if query:
        patterns = _load_gitignore_patterns(project_path)
        matches = _find_matching_files(project_path, query, patterns)
        if not matches:
            return f"(No files match query: {query})"
        match_sections = ["## Matching files\n"]
        for rel_path in matches:
            content = _read_file_safe(os.path.join(project_path, rel_path))
            if content is None:
                continue
            match_sections.append(f"## {rel_path}\n\n{content}\n")
        tail = "".join(match_sections)
    else:
        tree_section = f"## Project tree\n\n{_build_directory_tree(project_path)}\n\n"
        key_files = _read_key_files(project_path)
        key_section = f"## Key files\n\n{key_files}\n" if key_files else ""
        tail = tree_section + key_section

    crab_docs = _read_crabcakes_docs(project_path)
    docs_section = f"## Project docs\n\n{crab_docs}\n\n" if crab_docs else ""

    # Prompt files are opened only when the leftover budget can hold them.
    # The wrapper around the loader's sections is reserved here so a file
    # that fits the loader budget still fits after the heading is added.
    _PROMPT_WRAP = len("## Project prompts\n\n\n\n")
    prompt_budget = max_chars - len(tail) - len(docs_section) - _PROMPT_WRAP
    prompts_ctx = _load_project_prompts_context(
        project_path, max_chars=max(0, prompt_budget),
    )

    # Docs stay ahead of prompts (the prompts-directory tests require that
    # order). Prompts stay ahead of the tree, so a tail cut drops prompt
    # bodies before it touches the tree or the key files. If the prompt
    # block still does not fit, it is omitted entirely before that cut.
    prompt_block = f"## Project prompts\n\n{prompts_ctx}\n\n" if prompts_ctx else ""
    if prompt_block and len(docs_section) + len(prompt_block) + len(tail) > max_chars:
        prompt_block = ""
    full = docs_section + prompt_block + tail

    if len(full) <= max_chars:
        if not query:
            cache_key = f"{project_path}::{max_chars}"
            _FILE_CONTEXT_CACHE[cache_key] = (_project_root_mtime(project_path), full)
        return full

    # Keep the tail. A head slice would drop the tree the smart truncator
    # also keeps from the end.
    #
    # The marker is part of the budget, not an addition to it. Slicing to
    # max_chars and appending after returned max_chars + len(marker) — 47-49
    # chars over at every budget, since the marker length itself scales with
    # the digits of max_chars. Slice to what is left after reserving it.
    marker = f"\n\n[... file context truncated to {max_chars} chars ...]"
    keep = max(0, max_chars - len(marker))
    # When max_chars cannot even hold the marker, emit the marker alone
    # rather than a slice of content plus an over-budget marker.
    truncated = (full[-keep:] if keep else "") + marker
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
            None, 0, or negative is an unknown window and resolves to
            "hybrid". Those values are not passed through the 128_000
            default. A known window from 1 through 128_000 resolves to
            "jit". A window larger than 128_000 resolves to "hybrid".

    Returns:
        One of "preload", "hybrid", "jit".
    """
    # Normalize input via the shared validator (case-insensitive, strips whitespace)
    from models.providers import validate_provider_context_mode
    explicit_mode = validate_provider_context_mode(explicit_mode)

    if explicit_mode in ("preload", "jit", "hybrid"):
        return explicit_mode
    # "auto". An unknown window stays hybrid. Do not substitute 128_000
    # first: that default would then classify as JIT.
    if model_max_tokens is None or model_max_tokens <= 0:
        return "hybrid"
    if model_max_tokens <= 128_000:
        return "jit"
    return "hybrid"


def build_file_index(
    project_path: str,
    max_entries: int = 200,
    include_line_counts: bool = False,
) -> str:
    """Build a compact file index for the system prompt.

    Walks the project tree (respecting .gitignore via _load_gitignore_patterns
    and the EXCLUDED_DIRS frozenset), groups files by extension, shows path +
    size + (optionally) line count. Capped at max_entries; if more files exist,
    appends a directory-level summary.

    Per CONTEXT-EFFICIENCY-INSTRUCTIONS §1:
      - EXCLUDED_DIRS (docs/) are pruned DURING the walk unless
        DEVELCAKES_INCLUDE_DOCS=1, so an excluded subtree never consumes a
        slot in the max_entries cut.
      - Non-text files (BINARY_EXTENSIONS) are neither listed nor line-counted.
      - Ranking is tiered: source directories first, then repo-root files,
        then LOW_PRIORITY_DIRS (tests/, docs/, prompts/). Size is a tiebreak
        WITHIN a directory, not the global sort key. Root files sit in their
        own middle tier so a scratch file at the repo root cannot outrank a
        large source file just because '(' sorts below every letter.
      - The default path does NOT open file bodies: sizes come from stat only.
        Line counts cost a full read per file (up to 1MB each) to produce
        numbers the model does not act on, and they were what put a 305KB test
        file at the top of the list.

    Args:
        project_path: Absolute path to the project root.
        max_entries: Maximum number of files to list (default 200).
        include_line_counts: Whether to count lines per file (default False).
            OFF by default so the system-prompt path stays stat-only. Callers
            that genuinely need per-file line counts opt in explicitly and pay
            the read cost.

    Returns:
        Formatted text block. Empty string if project_path is invalid.
    """
    if not project_path or not os.path.isdir(project_path):
        return ""

    patterns = _load_gitignore_patterns(project_path)
    include_docs = _include_docs()

    # Directory-level pruning happens DURING the walk, so an excluded subtree
    # can never consume a slot in the max_entries cut
    # (CONTEXT-EFFICIENCY-INSTRUCTIONS §1). Matches _build_directory_tree, so
    # the index and the tree no longer disagree about docs/.
    skip_dirs = {"__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".tox"}
    if not include_docs:
        skip_dirs |= EXCLUDED_DIRS

    # Collect all files
    all_files: list[tuple[str, int, str]] = []  # (rel_path, size, top_dir)

    for root, dirs, files in os.walk(project_path):
        rel_root = os.path.relpath(root, project_path)
        if _is_ignored(rel_root, project_path, patterns):
            dirs[:] = []
            continue

        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skip_dirs]

        for name in files:
            if name.startswith("."):
                continue
            # Non-text files are neither listed nor line-counted. A PNG read
            # for newlines was reported as "139 lines" — garbage that cost a
            # full file read per image.
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext in BINARY_EXTENSIONS:
                continue
            rel_path = os.path.join(rel_root, name) if rel_root != "." else name
            if _is_ignored(rel_path, project_path, patterns):
                continue
            try:
                size = os.path.getsize(os.path.join(root, name))
            except OSError:
                size = 0

            top_dir = rel_path.split("/")[0] if "/" in rel_path else "(root)"
            all_files.append((rel_path, size, top_dir))

    if not all_files:
        return ""

    # Rank in three tiers (CONTEXT-EFFICIENCY BUG #1 fix):
    #   tier 0 — source directories (not "(root)", not in LOW_PRIORITY_DIRS)
    #   tier 1 — repo-root files (top_dir == "(root)")
    #   tier 2 — LOW_PRIORITY_DIRS (tests/, docs/, prompts/)
    # Within a tier, size is a tiebreak WITHIN a directory, not the global
    # sort key. The previous two-tier key ranked "(root)" ahead of every
    # source directory because '(' (ASCII 40) sorts below all letters AND the
    # sentinel is not in LOW_PRIORITY_DIRS — so a 1KB scratch file at the repo
    # root outranked agent/runtime.py (192KB).
    def _tier(top_dir: str) -> int:
        if top_dir == "(root)":
            return 1
        if top_dir in LOW_PRIORITY_DIRS:
            return 2
        return 0

    all_files.sort(key=lambda x: (_tier(x[2]), x[2], -x[1]))

    # Determine which files to show and which are truncated
    shown_files = all_files[:max_entries]
    omitted_files = all_files[max_entries:]

    # Group shown files by extension
    ext_groups: dict[str, list[tuple[str, int]]] = {}
    for rel_path, size, _top_dir in shown_files:
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
        for rel_path, size, _top_dir in omitted_files:
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
        # search_files, not file_search: Coder and Debugger have search_files
        # and do NOT have file_search, so naming file_search here pointed the
        # agent at a tool it cannot call
        # (CONTEXT-EFFICIENCY-INSTRUCTIONS §1).
        lines.append('[Use search_files to grep these directories, or list_files to walk them.]')
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
    - "preload" (default): existing behavior — full file context + core files.
      Prompt bodies are inlined here, under the file-context budget.
    - "jit": file index, then the names of .crabcakes/prompts/*.md. No bodies.
    - "hybrid": one stub line per existing core file, the file index, then
      the same prompt-name list. No prompt bodies.

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
        # JIT: index, then prompt names. No core files, no prompt bodies.
        return _append_project_prompt_names(file_index, project_path)

    # hybrid: one stub line per existing core file, then the index.
    # File bodies are not inlined; the model calls read_file for a window.
    core_sections = []
    for core_file in CORE_FILES:
        core_path = os.path.join(project_path, core_file)
        if not os.path.isfile(core_path):
            continue
        try:
            size = os.path.getsize(core_path)
        except OSError:
            continue
        core_sections.append(
            f"## {core_file}\n"
            f"[{size} bytes \u2014 use read_file(\"{core_file}\") to read a window]\n"
        )
    if not core_sections:
        base = file_index
    else:
        base = "\n".join(core_sections) + "\n\n" + file_index
    return _append_project_prompt_names(base, project_path)


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
