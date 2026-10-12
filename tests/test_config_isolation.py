# tests/test_config_isolation.py
# Regression net for the 2026-10-11 live-config leak (SPEC-22 era).
#
# These tests intentionally do NOT set HOME / XDG_CONFIG_HOME themselves —
# they depend on the autouse `_isolate_config_resolution` fixture in
# conftest.py. If that fixture is ever removed or weakened, they fail
# loudly: no suite may resolve config/data paths to the real user config
# dir. That is exactly how fixture events ended up in the live
# audit-log.jsonl and session files in the live conversations dir.
#
# Canary design: every assertion anchors on tmp_path, which the autouse
# fixture uses per-test — resolution must stay underneath it.

from __future__ import annotations

import os


def _under(tmp: str, path: str) -> bool:
    return os.path.abspath(path).startswith(os.path.abspath(tmp) + os.sep)


def test_config_dir_resolves_inside_test_tmp(tmp_path):
    from utils.config import get_config_dir
    assert _under(str(tmp_path), get_config_dir())


def test_v1_config_dir_resolves_inside_test_tmp(tmp_path):
    from utils.config import get_v1_config_dir
    assert _under(str(tmp_path), get_v1_config_dir())


def test_provider_failure_audit_path_inside_test_tmp(tmp_path):
    from utils import provider_failure_store
    assert _under(str(tmp_path), provider_failure_store._audit_log_path())


def test_conversations_dir_inside_test_tmp(tmp_path):
    from agent import persistence
    assert _under(str(tmp_path), persistence.conversations_dir())


def test_tool_audit_flush_path_inside_test_tmp(tmp_path):
    from agent.audit import AuditLog
    from utils.config import get_config_dir
    os.makedirs(get_config_dir(), exist_ok=True)
    log = AuditLog()
    log.record("isolation-canary", {"probe": True}, True, "", "")
    out = log.flush_audit_log()
    assert out is not None and _under(str(tmp_path), out)