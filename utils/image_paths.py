# utils/image_paths.py — SPEC-20: the single policy for "may this process read or
# display this local path?". Neutral: no GTK, no ui/ imports.
#
# Extracted from ui/views/event_cards.py (LOW-7) so the render pipeline
# and the Pango viewer share ONE implementation. Threat model unchanged
# from LOW-7: resolve symlinks (realpath) BEFORE the containment check, so
# a link inside an allowed root that points outside it is refused.

from __future__ import annotations

import os

from utils.config import get_env

_ALLOWED_ROOTS_FALLBACK = (os.path.expanduser("~"), "/tmp")


def get_allowed_roots() -> tuple[str, ...]:
    """Active project root (DEVELCAKES_ACTIVE_PROJECT_PATH; old CRABCAKES_ via
    get_env) plus home and /tmp. Mirrors LOW-7's tuple exactly."""
    roots: list[str] = []
    project = (get_env("ACTIVE_PROJECT_PATH") or "").strip()
    if project:
        roots.append(project)
    roots.extend(_ALLOWED_ROOTS_FALLBACK)
    return tuple(roots)


def is_path_in_allowed_roots(file_path: str) -> bool:
    """True if realpath(file_path) is under one of get_allowed_roots()."""
    try:
        resolved = os.path.realpath(file_path)
    except OSError:
        return False
    for root in get_allowed_roots():
        try:
            root_resolved = os.path.realpath(root)
        except OSError:
            continue
        try:
            if os.path.commonpath([resolved, root_resolved]) == root_resolved:
                return True
        except ValueError:
            continue
    return False
