"""SPEC-49: durable pins — zero work-unit / task-system residue.

The work-unit system (work_handler.py, work_unit.py, work_persistence.py and
every wiring/consumer site) was removed in SPEC-49. This file is the
permanent regression guard: it greps the production source tree (agent/,
models/, ui/, utils/, transport/, scripts/, main.py — skipping .venv,
scratch/, .crabcakes/ and tests/, which may legitimately mention the history)
for any removed symbol, and asserts the three deleted modules no longer exist.
"""

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REMOVED_NAMES = (
    "WorkUnit", "WorkLease", "WorkUnitStore", "work_store",
    "work_handler", "WorkHandler",
    "load_or_migrate_work_units", "save_work_units",
    "claim_work", "release_work", "find_live_lease", "assert_lease",
    "load_work_units", "render_tasks_summary", "write_tasks_summary",
    "cmd_work", "_release_session_lease", "_parse_tasks_summary",
    "_get_task_info", "_work_units_for_awareness",
)

ROOTS = ("agent", "models", "ui", "utils", "transport", "scripts", "main.py")
SKIP_DIRS = {".venv", "scratch", ".crabcakes", "tests", "__pycache__"}

DELETED_MODULES = (
    "ui/handlers/work_handler.py",
    "models/work_unit.py",
    "utils/work_persistence.py",
)


def _iter_source_files():
    for root in ROOTS:
        path = os.path.join(REPO_ROOT, root)
        if os.path.isfile(path):
            yield path
            continue
        if not os.path.isdir(path):
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)


def test_deleted_work_modules_do_not_exist():
    for rel in DELETED_MODULES:
        assert not os.path.exists(os.path.join(REPO_ROOT, rel)), (
            f"{rel} still exists after SPEC-49 removal"
        )


def test_no_work_residuals_in_source_tree():
    """Whole-source grep: no removed work-system symbol anywhere in code."""
    hits: list[str] = []
    for path in _iter_source_files():
        rel = os.path.relpath(path, REPO_ROOT)
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                for name in REMOVED_NAMES:
                    if name in line:
                        hits.append(f"{rel}:{lineno}: {name!r}")
    assert not hits, (
        "removed work-system names reappeared in source:\n" + "\n".join(hits)
    )
