# utils/project_memory.py — long-term project memory that is not pasted.
#
# Bodies live under <project>/.crabcakes/memory/. Only INDEX.md enters the
# prompt (via project_awareness.MEMORY_INDEX).
#
# Manifest:
#   Reads/writes: <project>/.crabcakes/memory/**
#   Network: none
#   GTK: none

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import date

from agent.tools import ToolResult

_logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_INDEX_CAP = 80
_LINE_CAP = 200
_HOOK_CAP = 120
_READ_CAP = 8000
_SEARCH_CAP = 8
_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slugify(name: object, max_len: int = 40) -> str:
    """Lowercase slug of [a-z0-9-]. Empty when nothing survives."""
    if not isinstance(name, str):
        return ""
    slug = name.strip().lower()
    slug = _SLUG_RE.sub("-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].strip("-")
    return slug


def memory_root(project_path: str) -> str:
    return os.path.join(project_path, ".crabcakes", "memory")


def load_index(project_path: str) -> str:
    """INDEX.md text, capped at 80 lines. Missing or unreadable → ''."""
    if not isinstance(project_path, str) or not project_path:
        return ""
    path = os.path.join(memory_root(project_path), "INDEX.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    lines = text.splitlines()
    if len(lines) > _INDEX_CAP:
        lines = lines[:_INDEX_CAP]
    return "\n".join(lines)


def dispatch(
    project_path: str,
    session_key: str,
    role: str,
    action: str,
    *,
    name: str = "",
    body: str = "",
    query: str = "",
    scope: str = "shared",
) -> ToolResult:
    """Run one memory action. ``promote`` / ``supersede`` are caller-gated."""
    from agent.tools import ToolResult as TR

    del session_key
    if not isinstance(project_path, str) or not project_path:
        return TR(success=False, error="no project open")
    if scope not in ("shared", "private"):
        return TR(success=False, error="scope must be shared or private")
    role_slug = slugify(role, max_len=40)
    if scope == "private" and not role_slug:
        return TR(success=False, error="caller has no role")

    with _LOCK:
        if action == "log":
            return _log(project_path, role_slug, body, scope)
        if action == "search":
            return _search(project_path, role_slug, query, scope)
        if action == "read":
            return _read(project_path, role_slug, name, scope)
        if action == "promote":
            return _promote(project_path, name, body)
        if action == "supersede":
            return _supersede(project_path, name, body)
        return TR(success=False, error=f"unknown memory action {action!r}")


def _contained(root: str, path: str) -> bool:
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)
    return real_path == real_root or real_path.startswith(real_root + os.sep)


def _safe_join(root: str, *parts: str) -> str | None:
    path = os.path.join(root, *parts)
    parent = os.path.dirname(path)
    # realpath of a not-yet-created file walks the existing ancestor.
    if not _contained(root, parent if os.path.isdir(parent) else root):
        return None
    if os.path.lexists(path) and not _contained(root, path):
        return None
    return path


def _log(project_path: str, role_slug: str, body: str, scope: str) -> ToolResult:
    if not isinstance(body, str) or not body.strip():
        return ToolResult(success=False, error="log body is empty")
    root = memory_root(project_path)
    day = date.today().isoformat()  # noqa: DTZ011 — diary day is the machine's local date
    if scope == "private":
        rel = os.path.join("agents", role_slug, "log", f"{day}.md")
    else:
        rel = os.path.join("log", f"{day}.md")
    path = _safe_join(root, rel)
    if path is None:
        return ToolResult(success=False, error="memory path escapes the project")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(body.strip() + "\n")
    except OSError as exc:
        _logger.warning("project_memory log failed: %s", exc)
        return ToolResult(success=False, error=f"memory write failed: {exc}")
    return ToolResult(success=True, output=f"logged {rel}")


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta, text[end + 5:]


def _render(status: str, superseded_by: str, body: str) -> str:
    return (
        "---\n"
        f"status: {status}\n"
        f"superseded_by: {superseded_by}\n"
        "---\n"
        f"{body.strip()}\n"
    )


def _topic_path(project_path: str, slug: str, role_slug: str, scope: str) -> str | None:
    root = memory_root(project_path)
    if scope == "private":
        return _safe_join(root, "agents", role_slug, f"{slug}.md")
    return _safe_join(root, f"{slug}.md")


def _read(project_path: str, role_slug: str, name: str, scope: str) -> ToolResult:
    slug = slugify(name)
    if not slug:
        return ToolResult(success=False, error="memory name is empty")
    path = _topic_path(project_path, slug, role_slug, scope)
    if path is None:
        return ToolResult(success=False, error="memory path escapes the project")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return ToolResult(success=False, error=f"no memory named {slug}")
    except OSError as exc:
        return ToolResult(success=False, error=f"memory write failed: {exc}")
    _meta, body = _frontmatter(text)
    body = body.strip()
    if len(body) > _READ_CAP:
        body = body[:_READ_CAP] + "\n[truncated]"
    return ToolResult(success=True, output=body)


def _iter_topics(directory: str) -> list[tuple[str, str, dict]]:
    found: list[tuple[str, str, dict]] = []
    if not os.path.isdir(directory):
        return found
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return found
    for name in names:
        if not name.endswith(".md") or name == "INDEX.md":
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        meta, body = _frontmatter(text)
        found.append((name[:-3], body, meta))
    return found


def _search(project_path: str, role_slug: str, query: str, scope: str) -> ToolResult:
    if not isinstance(query, str) or not query.strip():
        return ToolResult(success=False, error="search needs a query")
    needle = query.strip().casefold()
    root = memory_root(project_path)
    if scope == "private":
        directories = [os.path.join(root, "agents", role_slug)]
        log_dir = os.path.join(root, "agents", role_slug, "log")
    else:
        directories = [root]
        log_dir = None
    hits: list[str] = []
    for directory in directories:
        for slug, body, meta in _iter_topics(directory):
            if meta.get("status") == "superseded":
                continue
            hay = (slug + "\n" + body).casefold()
            if needle in hay:
                hook = _hook(body)
                hits.append(f"{slug}: {hook}")
            if len(hits) >= _SEARCH_CAP:
                break
    if log_dir and os.path.isdir(log_dir) and len(hits) < _SEARCH_CAP:
        for dirpath, _dirs, files in os.walk(log_dir):
            for name in sorted(files):
                if not name.endswith(".md"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        text = f.read()
                except OSError:
                    continue
                if needle in text.casefold():
                    hits.append(f"log/{name}: {_hook(text)}")
                if len(hits) >= _SEARCH_CAP:
                    break
    if not hits:
        return ToolResult(success=True, output="No memories match.")
    return ToolResult(success=True, output="\n".join(hits[:_SEARCH_CAP]))


def _hook(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("---"):
            return stripped.replace("\n", " ")[:_HOOK_CAP]
    return ""


def _read_index_lines(path: str) -> list[str]:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return []


def _write_index(path: str, lines: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = "\n".join(lines[:_INDEX_CAP])
    if text:
        text += "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _index_has(lines: list[str], slug: str) -> int | None:
    prefix = f"- {slug} — "
    for i, line in enumerate(lines):
        if line.startswith(prefix) or line == f"- {slug}":
            return i
    return None


def _promote(project_path: str, name: str, body: str) -> ToolResult:
    slug = slugify(name)
    if not slug:
        return ToolResult(success=False, error="memory name is empty")
    if not isinstance(body, str) or not body.strip():
        return ToolResult(success=False, error="promote body is empty")
    root = memory_root(project_path)
    index_path = _safe_join(root, "INDEX.md")
    topic = _safe_join(root, f"{slug}.md")
    if index_path is None or topic is None:
        return ToolResult(success=False, error="memory path escapes the project")
    lines = _read_index_lines(index_path)
    existing = _index_has(lines, slug)
    if existing is None and len(lines) >= _INDEX_CAP:
        return ToolResult(
            success=False,
            error="memory index is full (80). Supersede one first.",
        )
    hook = _hook(body.strip())
    line = f"- {slug} — {hook}"
    if len(line) > _LINE_CAP:
        line = line[:_LINE_CAP]
    try:
        os.makedirs(root, exist_ok=True)
        with open(topic, "w", encoding="utf-8") as f:
            f.write(_render("live", "", body))
        if existing is None:
            lines.append(line)
        else:
            lines[existing] = line
        _write_index(index_path, lines)
    except OSError as exc:
        _logger.warning("project_memory promote failed: %s", exc)
        return ToolResult(success=False, error=f"memory write failed: {exc}")
    return ToolResult(success=True, output=f"promoted {slug}")


def _supersede(project_path: str, name: str, replacement: str) -> ToolResult:
    slug = slugify(name)
    repl = slugify(replacement)
    if not slug or not repl:
        return ToolResult(success=False, error="memory name is empty")
    root = memory_root(project_path)
    old = _safe_join(root, f"{slug}.md")
    new = _safe_join(root, f"{repl}.md")
    index_path = _safe_join(root, "INDEX.md")
    if old is None or new is None or index_path is None:
        return ToolResult(success=False, error="memory path escapes the project")
    if not os.path.isfile(old) or not os.path.isfile(new):
        return ToolResult(success=False, error=f"no memory named {slug}")
    try:
        with open(old, "r", encoding="utf-8") as f:
            text = f.read()
        _meta, body = _frontmatter(text)
        with open(old, "w", encoding="utf-8") as f:
            f.write(_render("superseded", repl, body))
        lines = _read_index_lines(index_path)
        idx = _index_has(lines, slug)
        if idx is not None:
            del lines[idx]
            _write_index(index_path, lines)
    except OSError as exc:
        _logger.warning("project_memory supersede failed: %s", exc)
        return ToolResult(success=False, error=f"memory write failed: {exc}")
    return ToolResult(success=True, output=f"superseded {slug} with {repl}")
