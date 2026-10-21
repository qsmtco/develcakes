# tests/test_acceptance_tripwire.py
"""SPEC-47 R-6: the gate's own isolation tripwire.

Every subprocess spawned by scripts/acceptance.py must have XDG_CONFIG_HOME
pointed at a throwaway temp dir — never the real user config — otherwise a
gate run could write live state (the R-6 claim). This self-check asserts the
runner builds exactly that environment. The R-6 mutation (tests/mutations/
R-6.diff) deletes the redirect, which makes this test fail.
"""

from __future__ import annotations

import importlib.util
import pathlib
import tempfile

_ACCEPTANCE = pathlib.Path("scripts") / "acceptance.py"


def _load_acceptance():
    spec = importlib.util.spec_from_file_location(
        "acceptance_gate", _ACCEPTANCE.resolve())
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_runner_isolates_xdg_config_home():
    mod = _load_acceptance()
    tmp = tempfile.mkdtemp(prefix="tripwire-xdg-")
    env = mod._isolated_env(tmp)
    assert env.get("XDG_CONFIG_HOME") == tmp
    assert env["XDG_CONFIG_HOME"].startswith(tmp)