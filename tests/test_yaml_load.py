"""SPEC-21 SP4: CSafeLoader when present, SafeLoader fallback."""
from __future__ import annotations

import yaml

from utils.yaml_load import safe_load_yaml


def test_safe_load_yaml_parses_mapping():
    assert safe_load_yaml("a: 1\nb: two\n") == {"a": 1, "b": "two"}


def test_falls_back_to_safe_loader_when_c_absent(monkeypatch):
    monkeypatch.setattr(yaml, "CSafeLoader", None, raising=False)
    import utils.yaml_load as yl

    monkeypatch.setattr(yl, "_YAML_LOADER", yaml.SafeLoader)
    assert yl.safe_load_yaml("k: v") == {"k": "v"}


def test_csafeloader_used_when_present():
    import utils.yaml_load as yl

    if hasattr(yaml, "CSafeLoader") and yaml.CSafeLoader is not None:
        assert yl._YAML_LOADER is yaml.CSafeLoader
