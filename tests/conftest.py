# tests/conftest.py
# Shared pytest fixtures — all tests use isolated temp directories.

import ast
import os
import sys
import pytest

# GTK4 widget construction requires a real display server. There is no true
# headless GTK4 backend: GDK_BACKEND=broadway does NOT help — it segfaults on
# the first widget construction (verified 2026-10-20, GTK 4.14.5 / PyGObject
# 3.48.2), which aborts the entire pytest process with SIGSEGV rather than
# reporting a test failure.
#
# This file used to set GDK_BACKEND=broadway when DISPLAY was unset. That
# turned a missing-display situation into a battery-killing core dump and was
# removed. The supported way to run the GTK suites is xvfb-run (see
# knowledge/install.md and the per-file "runs under xvfb-run" notes).
#
# So: never invent a backend here. If a display is present, leave the
# environment alone. If it is absent, pytest_collection_modifyitems below
# skips the widget-constructing test modules with a clear reason instead of
# letting the interpreter segfault.
if 'WAYLAND_DISPLAY' in os.environ and not os.environ.get('GDK_BACKEND'):
    # Under Wayland+weston, prefer X11 for the headless test battery.
    os.environ.setdefault('GDK_BACKEND', 'x11')

# Ensure crabcakes package is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _has_display() -> bool:
    """True when a display server is reachable for GTK4 widget construction."""
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Path → _imports_widget_construction verdict, shared across all collected
# items in one pytest run (see the memoization note inside the function).
_WIDGET_SCAN_CACHE: dict[str, bool] = {}


def _imports_widget_construction(path: str) -> bool:
    """True if `path` can reach a Gtk widget constructor through imports.

    A segfault, not a test failure, is what happens when GTK builds a widget
    with no display — so the module has to be identified BEFORE it is imported.
    That rules out trying to construct a probe widget here: the probe itself
    would take the interpreter down.

    Detection is textual and transitive: parse the test module, follow every
    intra-project import (``ui.views.event_cards``, ``from ui.views import x``,
    ``from ui.views.chat_surface import Y``) to a bounded depth, and look for a
    ``Gtk.Something(`` constructor call anywhere in that closure. A static
    hand-maintained list is not sufficient — it silently missed
    tests/test_activity_bubbles.py, whose widget is reached through
    ``ui.views.event_cards.build_role_bubble``.

    Dynamic ui imports are treated like a direct ui import: an ``ast.Call``
    to ``importlib.import_module(...)`` or ``__import__(...)`` whose first
    argument is a literal string starting with ``ui``/``ui.`` can pull widget
    constructors into the process invisibly to the static scan (that is how
    a naive copy of the timer-tool pattern could segfault the headless suite).

    Opt-out: a test module that is genuinely headless-safe may set
    ``GTK_HEADLESS_SAFE = True`` at module top. The guard then skips the
    widget-scan for that module entirely (pytest still collects it).
    """
    # Deterministic per path and immutable within a run: memoized so a
    # module's closure is walked once per pytest invocation instead of once
    # per collected item. Per-item cost made full-suite headless collection
    # take minutes (2026-10-24, SPEC-58 gate); the cache keeps identical
    # results — the function is pure over the filesystem state, which does
    # not change mid-run.
    real_path = os.path.realpath(path)
    cached = _WIDGET_SCAN_CACHE.get(real_path)
    if cached is not None:
        return cached
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            root_tree = ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError):
        return False
    for node in root_tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "GTK_HEADLESS_SAFE"
                        for t in node.targets)
                and isinstance(node.value, ast.Constant)
                and node.value.value is True):
            _WIDGET_SCAN_CACHE[real_path] = False
            return False

    seen: set[str] = set()

    def resolve(module: str) -> str | None:
        rel = module.replace(".", os.sep)
        for candidate in (os.path.join(_PROJECT_ROOT, rel + ".py"),
                          os.path.join(_PROJECT_ROOT, rel, "__init__.py")):
            if os.path.isfile(candidate):
                return candidate
        return None

    def walk(filename: str, depth: int) -> bool:
        if depth > 4:
            return False
        real = os.path.realpath(filename)
        if real in seen or not real.endswith(".py"):
            return False
        seen.add(real)
        try:
            with open(real, encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read(), filename=real)
        except (OSError, SyntaxError):
            return False

        for node in ast.walk(tree):
            # A widget constructor is being invoked in this file.
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "Gtk"
                    and node.func.attr[:1].isupper()):
                return True
            # A dynamic ui import (importlib.import_module / __import__ with a
            # literal "ui" argument) can pull widget constructors in invisibly
            # to the static import scan — treat it as constructs-widgets too.
            if isinstance(node, ast.Call):
                func = node.func
                is_dynamic_ui_import = (
                    (isinstance(func, ast.Attribute)
                     and isinstance(func.value, ast.Name)
                     and func.value.id == "importlib"
                     and func.attr == "import_module")
                    or (isinstance(func, ast.Name) and func.id == "__import__")
                )
                if is_dynamic_ui_import and node.args:
                    arg = node.args[0]
                    if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                            and (arg.value == "ui" or arg.value.startswith("ui."))):
                        return True
            # Follow intra-project imports out of this file.
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in ("ui", "agent", "models",
                                                 "render", "utils", "transport"):
                    targets.append(node.module)
                    targets.extend(f"{node.module}.{a.name}" for a in node.names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in ("ui", "agent", "models",
                                                    "render", "utils", "transport"):
                        targets.append(alias.name)
            for target in targets:
                found = resolve(target)
                if found and walk(found, depth + 1):
                    return True
        return False

    result = walk(path, 0)
    _WIDGET_SCAN_CACHE[real_path] = result
    return result


def pytest_collection_modifyitems(config, items):
    """Skip widget-constructing GTK suites when no display is available.

    A missing display used to be 'handled' by setting GDK_BACKEND=broadway,
    which does not provide real rendering and segfaults on the first widget.
    One segfault kills the whole pytest process, so a single unguarded module
    could abort a full battery. Skipping keeps the run honest: the GTK suites
    are reported as skipped-with-reason instead of taking the interpreter down.

    The check is per-ITEM rather than per-module because the skip must be
    attached before the module is imported; pytest imports the module during
    collection, so a segfault at import time would already have happened.
    """
    if _has_display():
        return
    reason = "GTK widget construction needs a display; run under xvfb-run -a"
    for item in items:
        try:
            path = str(item.path)
        except AttributeError:  # pragma: no cover — very old pytest
            path = str(getattr(item, "fspath", ""))
        if _imports_widget_construction(path):
            item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(autouse=True)
def _clear_http_keepalive_gate(request, monkeypatch):
    """SPEC-21 SP3: an exported DEVELCAKES_HTTP_KEEPALIVE=1 in a developer
    shell must not route urlopen-patched suites through HTTPSConnection.
    The keep-alive tests set the gate themselves.
    """
    path = str(getattr(request.node, "path", request.node.fspath))
    if path.endswith("test_http_keepalive.py"):
        return
    monkeypatch.delenv("DEVELCAKES_HTTP_KEEPALIVE", raising=False)
    monkeypatch.delenv("CRABCAKES_HTTP_KEEPALIVE", raising=False)


@pytest.fixture(autouse=True)
def _isolate_config_resolution(tmp_path, monkeypatch):
    """Keep EVERY test's config/data resolution inside a per-test tmp dir.

    Regression net (2026-10-11 incident): suites that terminate turns
    without requesting ``tmp_config_dir`` wrote fixture events into the
    REAL ~/.config/develcakes/audit-log.jsonl and session files into the
    real conversations dir. ``get_config_dir()`` prefers $XDG_CONFIG_HOME
    over $HOME — so BOTH inputs are redirected here; a HOME-only patch
    silently stops isolating whenever XDG_CONFIG_HOME is set in the
    ambient environment (found in the same audit). Do not remove.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    """
    Patch config resolution to an isolated temp directory.
    All tests that touch config files use this fixture.

    Sets BOTH HOME and XDG_CONFIG_HOME — get_config_dir() prefers
    $XDG_CONFIG_HOME, so a HOME-only patch silently stops isolating
    whenever XDG_CONFIG_HOME is set in the ambient environment. The
    created dir name matches get_config_dir()'s final component.
    """
    config_dir = tmp_path / ".config" / "develcakes"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    return config_dir


@pytest.fixture
def tmp_prompts_dir(tmp_path, monkeypatch):
    """
    Create an isolated temp prompts directory with some .md files.
    Patch PromptsHandler._get_prompts_dir to return it.
    """
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    # Create a couple of sample .md files
    (prompts_dir / "sample.md").write_text("# Sample\nHello world")
    (prompts_dir / "example.md").write_text("# Example\nTest content")
    return prompts_dir


def test_handlers_do_not_import_each_other():
    """
    Import guard: handlers must NOT import other handlers.
    window.py importing handlers is correct and expected.
    This guard ensures handlers stay decoupled — if one handler imports another,
    the coupling is explicit (window wires them) rather than implicit.
    """
    import os
    handlers_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "ui", "handlers"
    )
    if not os.path.isdir(handlers_dir):
        pytest.skip("handlers/ directory does not exist yet")

    handler_files = [
        os.path.join(handlers_dir, f)
        for f in os.listdir(handlers_dir)
        if f.endswith(".py") and f not in ("__init__.py", "conftest.py")
    ]

    violations = []
    for filepath in handler_files:
        with open(filepath) as f:
            tree = ast.parse(f.read(), filename=filepath)

        our_name = os.path.basename(filepath)[:-3]  # e.g. "chat_handler" from "chat_handler.py"
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("ui.handlers."):
                    imported = node.module.split(".")[-1]
                    if imported != our_name:  # same handler can import itself
                        violations.append(f"{os.path.basename(filepath)} imports ui.handlers.{imported}")

    assert violations == [], "Handler coupling violations:\n  " + "\n  ".join(violations)


@pytest.fixture
def fake_glib():
    """Provide a GLib-like object for handler tests that need it.

    timeout_add/timeout_add_seconds record (source_id, delay_ms, callback) in
    `armed` (and the source_id in `armed_ids`) and return source IDs starting
    at 2 — armed timers NEVER fire automatically. Tests that need a timer's
    effect call the recorded callback directly:
        source_id, delay_ms, cb = fake_glib.armed[0]; cb()

    source_remove discards the ID from `armed_ids` — a source ID still present
    in `armed_ids` is armed; one that vanished was stopped.
    """
    class FakeGLib:
        def __init__(self):
            self._next_source_id = 2
            self.armed = []   # (source_id, delay_ms, callback)
            self.armed_ids = set()

        def timeout_add(self, delay_ms, fn, *args, **kwargs):
            source_id = self._next_source_id
            self._next_source_id += 1
            self.armed.append((source_id, delay_ms, fn))
            self.armed_ids.add(source_id)
            return source_id

        def timeout_add_seconds(self, seconds, fn, *args, **kwargs):
            source_id = self._next_source_id
            self._next_source_id += 1
            self.armed.append((source_id, seconds * 1000, fn))
            self.armed_ids.add(source_id)
            return source_id

        def source_remove(self, timer_id):
            self.armed_ids.discard(timer_id)

        def idle_add(self, fn, *args, **kwargs):
            fn(*args)
            return 1

    return FakeGLib()
