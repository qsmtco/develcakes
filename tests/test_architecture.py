"""
Architecture compliance tests.
Verifies: handler isolation, layer separation, no UI imports from models/gateway.
"""
import ast
import os
import sys


def test_handlers_do_not_import_each_other():
    """
    AST guard: handlers must NOT import other handlers.
    window.py importing handlers is correct and expected.
    This guard ensures handlers stay decoupled.

    TYPE_CHECKING-aware: imports inside an `if TYPE_CHECKING:` block are
    annotation-only (never evaluated at runtime) and are NOT violations,
    but any runtime import still is; the else-branch keeps the parent
    flag since it runs at runtime. Walks with an explicit flag instead of
    ast.walk because ast.walk loses parent context. (Same false-positive
    class as Debugger 2a BUG #1 in the views→handlers guard — flagged by
    Coder during the b4ab39a fix.)
    """
    handlers_dir = os.path.join(os.path.dirname(__file__), "..", "ui", "handlers")
    if not os.path.isdir(handlers_dir):
        pytest.skip("handlers/ directory does not exist yet")

    handler_files = [
        os.path.join(handlers_dir, f)
        for f in os.listdir(handlers_dir)
        if f.endswith(".py") and not f.startswith("_")
    ]

    violations = []

    def _flag_imports(node, in_type_checking, basename, our_name):
        """Recurse with the TYPE_CHECKING flag; flag runtime ui.handlers imports."""
        if isinstance(node, ast.If):
            guarded = isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
            for child in node.body:
                _flag_imports(child, in_type_checking or guarded, basename, our_name)
            for child in node.orelse:
                _flag_imports(child, in_type_checking, basename, our_name)
            return
        if isinstance(node, ast.ImportFrom):
            if (
                node.module
                and node.module.startswith("ui.handlers.")
                and not in_type_checking
            ):
                imported = node.module.split(".")[-1]
                if imported != our_name:  # same handler can import itself
                    violations.append(
                        f"{basename} imports ui.handlers.{imported}"
                    )
            return
        if isinstance(node, ast.Import):
            if not in_type_checking:
                for alias in node.names:
                    if alias.name.startswith("ui.handlers."):
                        imported = alias.name.split(".")[-1]
                        violations.append(
                            f"{basename} imports {alias.name}"
                        )
            return
        for child in ast.iter_child_nodes(node):
            _flag_imports(child, in_type_checking, basename, our_name)

    for filepath in handler_files:
        with open(filepath, "r") as f:
            tree = ast.parse(f.read(), filename=os.path.basename(filepath))

        our_name = os.path.basename(filepath)[:-3]  # e.g. "chat_handler"
        _flag_imports(tree, False, os.path.basename(filepath), our_name)

    assert not violations, "Handler isolation violated:\n  " + "\n  ".join(violations)


def test_models_and_gateway_do_not_import_ui():
    """
    Layer isolation: models/ and gateway/ must not import ui/ or gi.repository.Gtk.
    Entry points (main.py) and utils/icons.py are exempt (icons needs Gdk for textures).
    """
    violations = []
    root_dir = os.path.dirname(os.path.dirname(__file__))  # project root
    for subdir in ("models", "gateway"):
        subdir_path = os.path.join(root_dir, subdir)
        if not os.path.isdir(subdir_path):
            continue
        for fpath, dirs, files in os.walk(subdir_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                full_path = os.path.join(fpath, fname)
                rel = os.path.relpath(full_path, root_dir)
                with open(full_path) as f:
                    content = f.read()
                for lineno, line in enumerate(content.splitlines(), 1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if "from ui." in line or "import ui." in line:
                        violations.append(f"{rel}:{lineno}: {stripped}")
                    if "from gi.repository import Gtk" in line:
                        violations.append(f"{rel}:{lineno}: {stripped}")

    assert not violations, "Layer isolation violated:\n  " + "\n  ".join(violations)


def test_all_documented_public_apis_exist():
    """Verify every public method documented in ARCHITECTURE.md exists in code."""
    import importlib

    # AgentListHandler
    from ui.handlers.agent_list_handler import AgentListHandler

    arch_api = [
        "set_agent_mgr",
        "has_agent_mgr",
        "compute_initials",
        "get_agent_color",
        "get_sorted_agents",
        "on_chat_clicked",
        "on_toggle_clicked",
    ]
    missing = [m for m in arch_api if not hasattr(AgentListHandler, m)]
    assert not missing, f"AgentListHandler missing: {missing}"

    # LeftPanel
    from ui.views.left_panel import LeftPanel

    left_panel_api = [
        "set_agents",
        "set_agent_list_handler",
        "set_on_project_opened",
        "refresh_agents_with_project",
        "set_toggle_agent_callback",
    ]
    missing_lp = [m for m in left_panel_api if not hasattr(LeftPanel, m)]
    assert not missing_lp, f"LeftPanel missing: {missing_lp}"


def test_agent_does_not_import_ui_or_gtk():
    """Layer guard: agent/ must not import from ui/ or gi.repository."""
    violations = []
    root_dir = os.path.dirname(os.path.dirname(__file__))
    subdir = os.path.join(root_dir, "agent")
    for fpath, dirs, files in os.walk(subdir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            full_path = os.path.join(fpath, fname)
            rel = os.path.relpath(full_path, root_dir)
            with open(full_path) as f:
                content = f.read()
            for lineno, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "from ui." in line or "import ui." in line:
                    violations.append(f"{rel}:{lineno}: {stripped}")
                if "from gi.repository" in line:
                    violations.append(f"{rel}:{lineno}: {stripped}")
    assert not violations, "agent/ layer isolation violated:\n  " + "\n  ".join(violations)


def test_views_do_not_import_handlers():
    """Layer guard: ui/views/ must not import from ui/handlers/.

    AST-based and TYPE_CHECKING-aware: imports inside an
    `if TYPE_CHECKING:` block are annotation-only (never evaluated at
    runtime) and are NOT violations — but any runtime import
    (module level or function-local) still is. The else-branch of
    `if TYPE_CHECKING:` runs at runtime, so it keeps the parent flag.

    (Debugger 2a BUG #1: the previous line-scan flagged the
    annotation-only import in ui/views/settings_dialog.py — inside
    `if TYPE_CHECKING:` — as a false positive.)
    """
    violations = []
    root_dir = os.path.dirname(os.path.dirname(__file__))
    subdir = os.path.join(root_dir, "ui", "views")

    def _flag_imports(node, in_type_checking, rel):
        """Recursively flag runtime ui.handlers imports; skip TYPE_CHECKING bodies."""
        if isinstance(node, ast.If):
            guarded = isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
            for child in node.body:
                _flag_imports(child, in_type_checking or guarded, rel)
            for child in node.orelse:
                _flag_imports(child, in_type_checking, rel)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if not in_type_checking:
                if isinstance(node, ast.ImportFrom):
                    if node.module and (
                        node.module == "ui.handlers"
                        or node.module.startswith("ui.handlers.")
                    ):
                        names = ", ".join(a.name for a in node.names)
                        violations.append(
                            f"{rel}:{node.lineno}: from {node.module} import {names}")
                else:
                    for alias in node.names:
                        if alias.name == "ui.handlers" or alias.name.startswith("ui.handlers."):
                            violations.append(f"{rel}:{node.lineno}: import {alias.name}")
            return
        for child in ast.iter_child_nodes(node):
            _flag_imports(child, in_type_checking, rel)

    for fpath, dirs, files in os.walk(subdir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            full_path = os.path.join(fpath, fname)
            rel = os.path.relpath(full_path, root_dir)
            with open(full_path) as f:
                tree = ast.parse(f.read(), filename=full_path)
            _flag_imports(tree, False, rel)

    assert not violations, "views→handlers layer isolation violated:\n  " + "\n  ".join(violations)


def test_utils_gtk_imports_are_documented():
    """Guard: only documented GTK carve-out files in utils/ may import gi.repository.

    See ARCHITECTURE.md §2 table for the carve-out list.
    If a new file needs GTK, add it there and here.
    """
    documented_carve_outs = {
        "gtk_containers.py",
        "gtk_safe_link.py",
        "icons.py",
        "live_guard.py",
        "stt.py",
    }
    root_dir = os.path.dirname(os.path.dirname(__file__))
    subdir = os.path.join(root_dir, "utils")
    actual_gtk_imports = set()
    for fpath, dirs, files in os.walk(subdir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            full_path = os.path.join(fpath, fname)
            with open(full_path) as f:
                content = f.read()
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "from gi.repository" in line or "import gi\n" in line + "\n" or line.strip() == "import gi" or line.strip().startswith("import gi "):
                    actual_gtk_imports.add(fname)
                    break

    undocumented = actual_gtk_imports - documented_carve_outs
    assert not undocumented, (
        f"utils/ has undocumented GTK imports in: {undocumented}. "
        f"Add them to ARCHITECTURE.md §2 carve-out table and this test."
    )
