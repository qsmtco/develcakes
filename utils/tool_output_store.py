# utils/tool_output_store.py
# Offloaded tool-output store (SPEC-48).
#
# Manifest:
#   - Reads: <project>/.crabcakes/tool-outputs/*.txt
#   - Writes: <project>/.crabcakes/tool-outputs/*.txt
#   - Network: none
#   - Imports: stdlib only (hashlib, os, re)
#   - No GTK, no LLM calls
from __future__ import annotations

import hashlib
import os
import re

# The tool name as it appears in the filename. Sanitized to [A-Za-z0-9_.-].
_SAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]")

# How much of the body the stub's preview carries. Small on purpose: the
# stub lives in the conversation, so it must stay cheap.
PREVIEW_CHARS = 200

# The store lives under the project's .crabcakes/ directory.
_STORE_SUBDIR = os.path.join(".crabcakes", "tool-outputs")
_STORE_PREFIX = ".crabcakes/tool-outputs/"


def tool_outputs_dir(project_path: str) -> str:
    """Absolute path of <project>/.crabcakes/tool-outputs (not created)."""
    return os.path.abspath(os.path.join(project_path, _STORE_SUBDIR))


def _store_realdir_inside_project(project_path: str) -> str | None:
    """Realpath of the store dir when it stays inside the project, else None.

    A symlinked .crabcakes/ or tool-outputs/ pointing outside the project
    is a misconfiguration: refuse rather than follow (Debugger BUG #3).
    """
    proj = os.path.realpath(os.path.abspath(project_path))
    store = os.path.realpath(tool_outputs_dir(project_path))
    if store != proj and not store.startswith(proj + os.sep):
        return None
    return store


def offload_tool_output(
    project_path: str,
    tool_name: str,
    content: str,
) -> dict | None:
    """Write ``content`` to the store; return its metadata, or None on failure.

    Returns a dict with keys:
        path      (str)  — project-relative path, e.g.
                           ".crabcakes/tool-outputs/exec_command-ab12cd34.txt"
        bytes     (int)  — size of the written file
        lines     (int)  — number of lines written
        preview   (str)  — first PREVIEW_CHARS chars of the content
    Returns None when the content could not be written (the caller then
    falls back to the plain lossy stub).
    """
    store = _store_realdir_inside_project(project_path)
    if store is None:
        return None          # symlinked store dir escapes the project — refuse
    data = content.encode("utf-8", errors="replace")
    digest = hashlib.sha256(data).hexdigest()[:8]
    safe = _SAFE_NAME_CHARS.sub("_", tool_name or "tool")
    name = f"{safe}-{digest}.txt"
    rel_path = f".crabcakes/tool-outputs/{name}"
    tmp = None
    try:
        os.makedirs(store, exist_ok=True)
        final = os.path.join(store, name)
        if os.path.lexists(final) and os.path.realpath(final) != final:
            return None      # final name is a symlink — refuse
        tmp = os.path.join(store, f".{name}.tmp")
        if os.path.lexists(tmp) and os.path.realpath(tmp) != tmp:
            return None      # tmp name is a symlink — refuse
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
    except OSError:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass          # best-effort cleanup — swallow secondary failure
        return None
    return {
        "path": rel_path,
        "bytes": len(data),
        "lines": content.count("\n") + (0 if content.endswith("\n") or not content else 1),
        "preview": content[:PREVIEW_CHARS],
    }


def _is_store_path(rel_path: str) -> bool:
    """True when ``rel_path`` has exactly the store form
    ``.crabcakes/tool-outputs/<name>.txt`` with a flat name."""
    if not isinstance(rel_path, str) or not rel_path:
        return False
    if "\x00" in rel_path:
        return False
    norm = rel_path.replace("\\", "/")
    if not norm.startswith(_STORE_PREFIX):
        return False
    name = norm[len(_STORE_PREFIX):]
    if not name or "/" in name or name in (".", ".."):
        return False
    return name.endswith(".txt")


def _miss(error: str) -> dict:
    return {"found": False, "output": "", "line": 0, "lines": 0,
            "total_lines": 0, "truncated": False, "bytes": 0, "error": error}


def read_tool_output(
    project_path: str,
    rel_path: str,
    *,
    line: int = 1,
    max_lines: int = 200,
    max_bytes: int = 16384,
) -> dict:
    """Read a line window of an offloaded output back.

    Returns a dict with keys:
        found    (bool)  — the file exists and is inside the store
        output   (str)   — the windowed, unnumbered body
        line     (int)   — the first line returned (echoes the request)
        lines    (int)   — number of lines returned
        total_lines (int) — total lines in the file
        truncated (bool) — the window did not reach the end of the file
        bytes    (int)   — total size of the file on disk
        error    (str)   — non-empty when the read failed

    ``max_bytes`` is accepted for callers that want a hard cap but is not
    used to truncate: store files are bounded by the offload threshold, so
    reading whole keeps the line math exact and past-the-end precise.
    """
    store = _store_realdir_inside_project(project_path)
    if store is None:
        return _miss(f"Store dir escapes the project: {rel_path}")
    if not _is_store_path(rel_path):
        return _miss(f"Not a store path: {rel_path}")
    name = rel_path.replace("\\", "/").split("/")[-1]
    target = os.path.realpath(os.path.join(store, name))
    if os.path.dirname(target) != store:
        return _miss(f"Path escapes the store: {rel_path}")
    if not os.path.isfile(target):
        return _miss(f"Not a file in the store: {rel_path}")
    try:
        with open(target, "rb") as fh:
            raw = fh.read()          # store files are offloaded bodies:
                                     # written once by us, capped by the
                                     # offload threshold — reading whole is
                                     # bounded and keeps line math exact
        size = len(raw)
        text = raw.decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        total = len(all_lines)
        if total == 0:
            return {"found": True, "output": "", "line": line, "lines": 0,
                    "total_lines": 0, "truncated": False, "bytes": size, "error": ""}
        if line < 1 or line > total:
            return _miss(f"line {line} is past the end of {rel_path} ({total} lines)")
        window = min(max_lines, 200) if max_lines and max_lines > 0 else 200
        got = all_lines[line - 1: line - 1 + window]
        return {"found": True, "output": "\n".join(got), "line": line,
                "lines": len(got), "total_lines": total,
                "truncated": line - 1 + len(got) < total, "bytes": size, "error": ""}
    except OSError as exc:
        return _miss(f"Cannot read {rel_path}: {exc}")
