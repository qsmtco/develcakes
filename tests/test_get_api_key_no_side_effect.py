# tests/test_get_api_key_no_side_effect.py
# Verifies that get_api_key() is a pure read — does NOT create providers.yaml
# as a side effect. The file-creation responsibility belongs to SettingsHandler,
# not to the config loader.

import os
import pathlib
import pytest

from agent.config import get_api_key


def test_get_api_key_does_not_create_providers_yaml(tmp_config_dir):
    """get_api_key on a fresh config must not create providers.yaml."""
    # Fresh config: agent.json exists with empty providers, no providers.yaml
    # (SPEC-11 D4: config dir is develcakes now — was crabcakes; the seed
    # must land where get_config_dir() actually reads).
    config_dir = pathlib.Path(os.environ['HOME']) / ".config" / "develcakes"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "agent.json").write_text('{"providers": {}}')
    yaml_path = config_dir / "providers.yaml"
    # Pre-condition: no providers.yaml
    assert not yaml_path.exists()
    # Call get_api_key
    result = get_api_key("nonexistent")
    # Post-condition: still no providers.yaml
    assert not yaml_path.exists(), \
        f"get_api_key created providers.yaml as side effect! result={result}"
    assert result is None
