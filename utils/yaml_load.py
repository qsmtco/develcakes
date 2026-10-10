# utils/yaml_load.py
# SPEC-21 SP4: one YAML loader for every hot-path parse.
# Uses libyaml's CSafeLoader when present; otherwise PyYAML's SafeLoader.
# Both are safe loaders — only plain YAML types are constructed.

from __future__ import annotations

import yaml

_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def safe_load_yaml(stream):
    """Parse YAML with CSafeLoader when libyaml is installed, else SafeLoader."""
    return yaml.load(stream, Loader=_YAML_LOADER)
