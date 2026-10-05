# tests/test_env_divergence.py — SPEC-11 SP2: env-var divergence (D2/D2b).
#
# RED-first: written BEFORE get_env existed — the get_env unit tests failed
# at collection with ImportError; the site tests failed on the old literals.
#
# Contract under test (D2, docs/specs/phases/SPEC-11-PREFLIGHT-DECISIONS.md):
#   - get_env(name): DEVELCAKES_<name> if set (EMPTY STRING IS SET — never
#     falls back), else CRABCAKES_<name> (one-release fallback), else None.
#   - All renamed-family reads route through get_env — no direct
#     os.environ.get of the CRABCAKES_* family in non-test code.
#   - ACTIVE_PROJECT_ENV constant VALUE renames to the new literal.
#   - conftest's CRABCAKES_MIGRATE_STORE=0 pin rides the fallback.
#
# Env manipulation via monkeypatch only — no mocks. The MIGRATE_STORE latch
# is tested via subprocess (real import path, isolated env): conftest pins
# the OLD name process-wide at collection, so an in-process assert could
# never distinguish converted from unconverted code.

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class TestGetEnv:
    def test_get_env_new_wins(self, monkeypatch):
        """Both names set with different values → the NEW name wins; the two
        names are never mixed. RED (pre-SP2): get_env did not exist."""
        monkeypatch.setenv("DEVELCAKES_PROJECTS_DIR", "/new")
        monkeypatch.setenv("CRABCAKES_PROJECTS_DIR", "/old")

        from utils.config import get_env

        assert get_env("PROJECTS_DIR") == "/new"

    def test_get_env_old_fallback(self, monkeypatch):
        """Only the old name set → the old value rides the one-release
        fallback (conftest's MIGRATE_STORE pin and the legacy test battery
        depend on this path)."""
        monkeypatch.setenv("CRABCAKES_PROJECTS_DIR", "/old")
        monkeypatch.delenv("DEVELCAKES_PROJECTS_DIR", raising=False)

        from utils.config import get_env

        assert get_env("PROJECTS_DIR") == "/old"

    def test_get_env_neither(self, monkeypatch):
        """Neither name set → None (call sites keep their own defaults)."""
        monkeypatch.delenv("DEVELCAKES_PROJECTS_DIR", raising=False)
        monkeypatch.delenv("CRABCAKES_PROJECTS_DIR", raising=False)

        from utils.config import get_env

        assert get_env("PROJECTS_DIR") is None

    def test_get_env_empty_string_new(self, monkeypatch):
        """NEW name set to EMPTY STRING → "" returned, NO fallback. "Set" is
        presence, not truthiness — an operator's explicit DEVELCAKES_X=
        (empty) must not resurrect the old name's value. Pins get_env's
        is-not-None branch (a truthiness check would fall back here)."""
        monkeypatch.setenv("DEVELCAKES_PROJECTS_DIR", "")
        monkeypatch.setenv("CRABCAKES_PROJECTS_DIR", "/old")

        from utils.config import get_env

        assert get_env("PROJECTS_DIR") == ""

    def test_get_env_empty_string_old_only(self, monkeypatch):
        """OLD name set-but-empty with new unset → "" returned (the fallback
        honors presence symmetrically — same never-treat-empty-as-unset
        rule on both names)."""
        monkeypatch.delenv("DEVELCAKES_PROJECTS_DIR", raising=False)
        monkeypatch.setenv("CRABCAKES_PROJECTS_DIR", "")

        from utils.config import get_env

        assert get_env("PROJECTS_DIR") == ""

    def test_get_env_unrelated_suffix(self, monkeypatch):
        """A DEVELCAKES_ var with a DIFFERENT suffix must not leak into an
        unrelated get_env name (exact-suffix construction, no prefix-only
        matching)."""
        monkeypatch.setenv("DEVELCAKES_DEBUG", "1")
        monkeypatch.delenv("DEVELCAKES_PROJECTS_DIR", raising=False)
        monkeypatch.delenv("CRABCAKES_PROJECTS_DIR", raising=False)

        from utils.config import get_env

        assert get_env("PROJECTS_DIR") is None


class TestSites:
    def test_projects_dir_reads_env(self, monkeypatch):
        """Site 1: get_projects_dir honors the NEW name, and the OLD name
        alone still resolves (fallback). RED (pre-SP2): the getter read only
        CRABCAKES_PROJECTS_DIR, so the NEW-name half failed."""
        from utils.config import get_projects_dir

        monkeypatch.setenv("DEVELCAKES_PROJECTS_DIR", "/new-projects")
        monkeypatch.delenv("CRABCAKES_PROJECTS_DIR", raising=False)
        assert get_projects_dir() == "/new-projects"

        monkeypatch.delenv("DEVELCAKES_PROJECTS_DIR", raising=False)
        monkeypatch.setenv("CRABCAKES_PROJECTS_DIR", "/old-projects")
        assert get_projects_dir() == "/old-projects"

    def test_migrate_store_latch_reads_new_and_fallback(self, monkeypatch):
        """Site 3: agent.runtime latches _MIGRATE_STORE_ON_INIT through
        get_env at module import — the NEW name enables (True), the OLD
        name alone also enables via fallback (True).

        Subprocess, not in-process: conftest pins the OLD name to "0"
        process-wide BEFORE collection, so this process's latch is already
        False regardless of what we monkeypatch — an in-process assert
        could not distinguish converted from unconverted code. Each
        subprocess runs one env shape through the REAL import path.

        RED (pre-SP2): NEW=1 left the latch False (runtime read only the
        old name).
        """
        base_env = dict(os.environ)
        base_env["PYTHONPATH"] = str(REPO)
        base_env.pop("CRABCAKES_MIGRATE_STORE", None)
        base_env.pop("DEVELCAKES_MIGRATE_STORE", None)

        def latch_with(**extra):
            env = dict(base_env)
            env.update({k: v for k, v in extra.items()})
            probe = (
                "import agent.runtime as r; print(r._MIGRATE_STORE_ON_INIT)"
            )
            run = subprocess.run(
                [sys.executable, "-c", probe],
                env=env, capture_output=True, text=True, timeout=60, check=False,
            )
            assert run.returncode == 0, run.stderr[-500:]
            return run.stdout.strip()

        assert latch_with(DEVELCAKES_MIGRATE_STORE="1") == "True"
        assert latch_with(CRABCAKES_MIGRATE_STORE="1") == "True"

    def test_main_setdefault_respects_old_name_kill_switch(self):
        """SP2 FIX ROUND (HIGH, Debugger audit + Supervisor probe): main.py's
        default-ON write must honor an OLD-name kill-switch. The SP2 draft
        replaced the setdefault NAME but kept setdefault semantics — with
        the operator (or conftest) holding CRABCAKES_MIGRATE_STORE=0 and the
        NEW name unset, setdefault wrote DEVELCAKES_MIGRATE_STORE=1 anyway,
        get_env read the NEW name first, and the documented kill-switch was
        silently defeated (probe: latch True; a subset test order can start
        a REAL store-migration thread).

        The fix gates the default-ON write on get_env: it fires ONLY when
        NEITHER name is set. Three shapes through `import main` (the real
        module, whose top-level run happens before any agent.runtime import
        — same order production gets):

        - OLD=0 (NEW unset)  → latch False (the defeated kill-switch — RED
          pre-fix: True)
        - neither set        → latch True (default ON preserved)
        - NEW=0              → latch False (explicit new-name opt-out)

        Subprocess because the latch reads at agent.runtime MODULE IMPORT
        and main.py's write must happen BEFORE that import — the exact
        production ordering, impossible to exercise in-process.
        """
        base_env = dict(os.environ)
        base_env["PYTHONPATH"] = str(REPO)
        base_env.pop("CRABCAKES_MIGRATE_STORE", None)
        base_env.pop("DEVELCAKES_MIGRATE_STORE", None)

        def latch_after_main(**extra):
            env = dict(base_env)
            env.update({k: v for k, v in extra.items()})
            probe = (
                "import main; "
                "import agent.runtime as r; "
                "print(r._MIGRATE_STORE_ON_INIT)"
            )
            run = subprocess.run(
                [sys.executable, "-c", probe],
                env=env, capture_output=True, text=True, timeout=120, check=False,
            )
            assert run.returncode == 0, run.stderr[-500:]
            return run.stdout.strip()

        # The HIGH: old-name kill-switch must hold through main's default-ON.
        assert latch_after_main(CRABCAKES_MIGRATE_STORE="0") == "False"
        # Default ON when the operator says nothing.
        assert latch_after_main() == "True"
        # New-name opt-out (worked pre-fix; pinned so the gate can't drop it).
        assert latch_after_main(DEVELCAKES_MIGRATE_STORE="0") == "False"

    def test_active_project_env_constant_and_reader(self, monkeypatch):
        """Site 6: the wiring constant's VALUE is the NEW literal, and
        event_cards' allowed-roots reader resolves it via get_env — the NEW
        name is honored. RED (pre-SP2): the constant held the old literal
        and the reader read os.environ directly for it."""
        from ui import wiring
        from ui.views import event_cards

        assert wiring.ACTIVE_PROJECT_ENV == "DEVELCAKES_ACTIVE_PROJECT_PATH"

        monkeypatch.setenv("DEVELCAKES_ACTIVE_PROJECT_PATH", "/new/proj")
        monkeypatch.delenv("CRABCAKES_ACTIVE_PROJECT_PATH", raising=False)
        assert "/new/proj" in event_cards._get_allowed_roots()
