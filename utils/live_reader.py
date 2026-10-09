# utils/live_reader.py — SPEC-20a: the read-only reader for the live bridge.
#
# The bridge stays pure (it routes). This module is the injected reader:
# validate a caller-supplied path, then read bytes. No GTK, no network.
#
# Policy, in order (fail closed, never raises):
#   1. realpath + LOW-7 containment (utils.image_paths.is_path_in_allowed_roots)
#      so a symlink inside a root that points outside is refused before open.
#   2. Extension allowlist on BOTH the requested name and the resolved name:
#      .png .jpg .jpeg .gif .webp only. No .svg, no text, no extensionless.
#   3. Dotfiles refused (basename starts with "."), requested and resolved.
#   4. 8 MB cap — the same ceiling as render/html.py _MAX_IMAGE_BYTES (SPEC-20).
#      Size is checked before open, and again on the bytes actually read.

from __future__ import annotations

import base64
import logging
import os

from utils.image_paths import is_path_in_allowed_roots

_logger = logging.getLogger(__name__)

# Match SPEC-20 render/html.py _MAX_IMAGE_BYTES. A 6 MB PNG is ~8 MB base64.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

_IMAGE_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def read_local_image(params: dict) -> dict:
    """Read one allowlisted local image.

    Success: {"mime": <mime>, "base64": <ascii>}
    Refusal: {"reason": <str>}
    Never raises.
    """
    try:
        return _read_local_image(params)
    except Exception:
        _logger.exception("live_reader: read_file failed")
        return {"reason": "read failed"}


def _read_local_image(params: dict) -> dict:
    if not isinstance(params, dict):
        return {"reason": "missing path"}
    path = params.get("path")
    if not isinstance(path, str) or not path.strip():
        return {"reason": "missing path"}
    candidate = os.path.expanduser(path.strip())
    # Containment BEFORE any read. realpath happens inside the helper, so
    # traversal and a symlink that leaves the root die here.
    if not is_path_in_allowed_roots(candidate):
        return {"reason": "path not allowed"}
    resolved = os.path.realpath(candidate)
    blocked = _name_block_reason(candidate) or _name_block_reason(resolved)
    if blocked:
        return {"reason": blocked}
    if not os.path.isfile(resolved):
        return {"reason": "not a file"}
    if os.path.getsize(resolved) > MAX_IMAGE_BYTES:
        return {"reason": "file too large"}
    with open(resolved, "rb") as fh:
        raw = fh.read()
    if len(raw) > MAX_IMAGE_BYTES:
        return {"reason": "file too large"}
    ext = os.path.splitext(os.path.basename(resolved))[1].lower()
    mime = _IMAGE_MIME.get(ext)
    if mime is None:
        return {"reason": "extension not allowed"}
    return {"mime": mime, "base64": base64.b64encode(raw).decode("ascii")}


def _name_block_reason(path: str) -> str | None:
    """Dotfile and extension gates. None means the name is allowed."""
    base = os.path.basename(path)
    if not base or base.startswith("."):
        return "dotfile not allowed"
    ext = os.path.splitext(base)[1].lower()
    if ext not in _IMAGE_MIME:
        return "extension not allowed"
    return None
