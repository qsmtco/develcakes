"""Durable pins: zero Auxilium/KB residue after SPEC-04 R5 removal.

The legacy per-file KB test cases are stripped in SP4; this file is the
permanent regression suite — grep-as-test over the three SP2-rewired sources
plus module-existence and import-health checks.

Part of SPEC-04 Sub-Phase 2 (runtime sentinel/synthesis/retry + config
defaults + provider store).
"""

import importlib.util
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.join(REPO_ROOT, "agent", "runtime.py")
CONFIG = os.path.join(REPO_ROOT, "agent", "config.py")
STORE = os.path.join(REPO_ROOT, "utils", "providers_store.py")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_runtime_source_has_no_kb_refs() -> None:
    src = _read(RUNTIME)
    for needle in (
        "KB_OUT_OF_SCOPE",
        "_inject_kb_context",
        "_prepare_kb_synthesis",
        "kb_server",
    ):
        assert needle not in src, f"agent/runtime.py still references {needle!r}"


def test_config_source_has_no_local_kb() -> None:
    src = _read(CONFIG)
    assert "local-kb" not in src, "agent/config.py still references 'local-kb'"


def test_providers_store_has_no_ensure_kb() -> None:
    src = _read(STORE)
    assert "ensure_kb_provider" not in src, (
        "utils/providers_store.py still defines or calls ensure_kb_provider"
    )


def test_no_kb_modules_exist() -> None:
    assert importlib.util.find_spec("agent.kb_lookup") is None
    assert importlib.util.find_spec("agent.kb_server") is None


def test_fresh_config_has_empty_provider_defaults() -> None:
    from agent.config import AgentConfig

    cfg = AgentConfig()
    assert cfg.default_provider == ""
    assert cfg.default_model == ""


def test_runtime_imports_clean() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import agent.runtime"],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
