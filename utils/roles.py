# utils/roles.py
# User-defined role file I/O.
#
# Manifest:
#   - Reads/writes YAML and JSON files from <config_dir>/roles/
#   - One file per role: <slug>.yaml (JSON only when PyYAML is missing)
#   - No GTK, no network
#
# Architecture: pure utility following utils/agent_defs.py pattern.
# Path resolution uses utils.config.get_config_dir() lazily, like
# utils/agent_defs.py, so callers (and tests) can monkeypatch it.

from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

BUILTIN_ROLES: tuple[str, ...] = ("supervisor", "coder", "debugger")


def _get_roles_dir() -> str:
    """Return the user roles directory."""
    from utils.config import get_config_dir
    return os.path.join(get_config_dir(), "roles")


def role_slug(name: str) -> str:
    """Lowercase, spaces to hyphens, drop every other character.
    ASCII only: non-ASCII alphanumerics (e.g. é, ö) are dropped, because a
    role slug becomes a worktree/branch id. Runs of hyphens/underscores
    (and space-made hyphens) collapse into a single hyphen; a lone
    underscore is preserved. Strip leading and trailing hyphens and
    underscores. '' when nothing remains.
    """
    s = str(name).lower()
    s = "".join(c for c in s if (c.isascii() and c.isalnum()) or c in "-_ ")
    s = s.replace(" ", "-")
    s = re.sub(r"[-_]{2,}", "-", s)
    return s.strip("-_")


def _parse_role_file(filepath: str) -> dict | None:
    """Parse a single role file (YAML or JSON).

    Tries YAML first, falls back to JSON if pyyaml is not installed.
    Returns None on any parse failure or when the file is not a mapping.
    """
    if filepath.endswith((".yaml", ".yml")):
        try:
            import yaml
            from utils.yaml_load import safe_load_yaml
            with open(filepath, encoding="utf-8") as f:
                data = safe_load_yaml(f)
            if isinstance(data, dict):
                return data
            logger.warning("Role file %s did not contain a mapping — skipping", filepath)
            return None
        except ImportError:
            # pyyaml not installed — try JSON fallback for .yaml/.yml files
            # only if the file is also valid JSON.
            try:
                with open(filepath, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    logger.info(
                        "pyyaml not installed — parsed %s as JSON fallback", filepath
                    )
                    return data
            except (json.JSONDecodeError, OSError):
                pass
            logger.warning("pyyaml not installed — cannot parse %s", filepath)
            return None
        except OSError as e:
            logger.warning("Failed to read role file %s: %s", filepath, e)
            return None
        except yaml.YAMLError as e:
            logger.warning("Failed to parse role file %s: %s", filepath, e)
            return None

    if filepath.endswith(".json"):
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            logger.warning("Role file %s did not contain a mapping — skipping", filepath)
            return None
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Failed to parse role file %s: %s", filepath, e)
            return None

    return None


def _normalize_role(data: dict) -> dict | None:
    """Validate a parsed role mapping and resolve its identity slug.

    Returns the public {name, slug, prompt} dict, or None when the file has
    an empty name, an empty prompt, or claims a built-in role. The slug is
    the stored ``slug`` field, falling back to the slug of the name — the
    identity load_roles() exposes, independent of the file name.
    """
    name = str(data.get("name") or "").strip()
    prompt = data.get("prompt")
    if not name or not isinstance(prompt, str) or not prompt.strip():
        return None
    slug = str(data.get("slug") or "").strip() or role_slug(name)
    if not slug or slug in BUILTIN_ROLES or name.lower() in BUILTIN_ROLES:
        return None
    return {"name": name, "slug": slug, "prompt": prompt}


def load_roles() -> list[dict]:
    """User roles only, sorted by name. Each dict is
    {name: str, slug: str, prompt: str}. Skips a file that does not parse,
    has an empty name, or has an empty prompt. Does not include the three
    built-in names.
    """
    roles_dir = _get_roles_dir()
    if not os.path.isdir(roles_dir):
        return []

    roles: list[dict] = []
    for fname in sorted(os.listdir(roles_dir)):
        if not fname.endswith((".yaml", ".yml", ".json")):
            continue
        data = _parse_role_file(os.path.join(roles_dir, fname))
        if not isinstance(data, dict):
            continue
        role = _normalize_role(data)
        if role is not None:
            roles.append(role)

    roles.sort(key=lambda r: r["name"].lower())
    return roles


def load_role(slug: str) -> dict | None:
    """One user role, or None. Builtin slugs return None (they are not files).

    First tries the file named <slug>.yaml/.yml/.json; when a usable file
    exists, its STORED slug field is authoritative and is what the result
    dict carries (not the requested slug). When no such file exists or the
    file bearing the name is unusable, falls back to scanning every role
    file for the one whose stored slug equals the request. Either way the
    returned slug agrees with load_roles().
    """
    if slug in BUILTIN_ROLES:
        return None
    roles_dir = _get_roles_dir()
    if not os.path.isdir(roles_dir):
        return None

    # Fast path: a file named after the requested slug.
    for ext in (".yaml", ".yml", ".json"):
        filepath = os.path.join(roles_dir, f"{slug}{ext}")
        if os.path.isfile(filepath):
            data = _parse_role_file(filepath)
            if isinstance(data, dict):
                role = _normalize_role(data)
                if role is not None:
                    return role
            # The file bearing this name is unusable; fall through to the
            # stored-slug scan so lookup stays consistent with load_roles()
            # (which skips unparsable/invalid files).
            break

    # Fallback: locate by stored slug across all role files.
    for fname in sorted(os.listdir(roles_dir)):
        if not fname.endswith((".yaml", ".yml", ".json")):
            continue
        data = _parse_role_file(os.path.join(roles_dir, fname))
        if not isinstance(data, dict):
            continue
        role = _normalize_role(data)
        if role is not None and role["slug"] == slug:
            return role
    return None


def save_role(name: str, prompt: str) -> tuple[str | None, str | None]:
    """Write the role. Returns (slug, None) on success, (None, error) on refusal.
    Refuses an empty name, an empty prompt, a slug in BUILTIN_ROLES, or a slug
    that already has a file. Does not overwrite.
    OSError from the write is returned as the error string, not raised.
    """
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        return None, "Role name cannot be empty."

    prompt = prompt if isinstance(prompt, str) else ""
    if not prompt.strip():
        return None, "Role prompt cannot be empty."

    slug = role_slug(name)
    if not slug:
        return None, "Role name has no usable characters."
    if slug in BUILTIN_ROLES:
        return None, f"'{slug}' is a built-in role."

    roles_dir = _get_roles_dir()
    try:
        os.makedirs(roles_dir, exist_ok=True)
    except OSError as e:
        return None, str(e)

    for ext in (".yaml", ".yml", ".json"):
        if os.path.exists(os.path.join(roles_dir, f"{slug}{ext}")):
            return None, f"A role named '{slug}' already exists."

    export = {"name": name, "slug": slug, "prompt": prompt}
    try:
        # Try YAML first
        try:
            import yaml
            filepath = os.path.join(roles_dir, f"{slug}.yaml")
            with open(filepath, "w", encoding="utf-8") as f:
                yaml.dump(
                    export, f, default_flow_style=False, allow_unicode=True, sort_keys=False
                )
            return slug, None
        except ImportError:
            pass
        # Fallback to JSON
        filepath = os.path.join(roles_dir, f"{slug}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(export, f, indent=2, ensure_ascii=False)
        return slug, None
    except OSError as e:
        return None, str(e)
