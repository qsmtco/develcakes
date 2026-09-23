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


def test_source_tree_free_of_kb_strings() -> None:
    """Whole-tree grep: no auxilium/kb_server/kb_lookup/local-kb anywhere.

    Whitelists nothing — if a hit is ever legitimately needed, this test must
    be revisited with an explicit adjudication (SPEC-04 SP3 pin).
    """
    needles = ("auxilium", "kb_server", "kb_lookup", "local-kb")
    roots = [
        os.path.join(REPO_ROOT, "agent"),
        os.path.join(REPO_ROOT, "ui"),
        os.path.join(REPO_ROOT, "utils"),
        os.path.join(REPO_ROOT, "models"),
        os.path.join(REPO_ROOT, "main.py"),
    ]
    hits: list[str] = []
    for root in roots:
        paths = [root] if os.path.isfile(root) else [
            os.path.join(dirpath, name)
            for dirpath, _dirnames, filenames in os.walk(root)
            for name in filenames
            if name.endswith(".py")
        ]
        for path in paths:
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
            low = src.lower()
            for needle in needles:
                if needle in low:
                    hits.append(f"{path}: contains {needle!r}")
    assert not hits, "KB residue found:\n" + "\n".join(hits)


def test_special_auxilium_session_unknown() -> None:
    """The special-agents registry cannot contain special:auxilium.

    Checked by construction: no built-in default-agent YAML seeds an auxilium
    definition (the machine state on a dev box may still hold a stale seeded
    copy, so the live registry is deliberately not asserted here).
    """
    import yaml

    defaults_dir = os.path.join(REPO_ROOT, "prompts", "default_agents")
    assert os.path.isdir(defaults_dir)
    for fname in os.listdir(defaults_dir):
        assert not fname.lower().startswith("auxilium"), (
            f"{fname} re-seeds the removed auxilium agent"
        )
        if not fname.endswith((".yaml", ".yml")):
            continue
        with open(os.path.join(defaults_dir, fname), "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        role = str(data.get("role", "")).lower()
        assert role != "helper", (
            f"{fname} still declares role: helper — re-seeds special:helper"
        )


def test_window_has_no_wizard_refs() -> None:
    """ui/window.py carries no Auxilium wizard scaffolding (SP3 R7)."""
    src = _read(os.path.join(REPO_ROOT, "ui", "window.py"))
    assert "auxilium" not in src.lower()
    assert "is_auxilium_wizard_needed" not in src
