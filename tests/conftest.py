# tests/conftest.py
# Shared pytest fixtures — all tests use isolated temp directories.

import ast
import os
import sys
import pytest

# Ensure headless rendering works for GTK tests
# Use broadway backend for headless tests, unless another backend is already set
# (e.g., xvfb-run provides X11)
if 'WAYLAND_DISPLAY' in os.environ:
    # Under Wayland+weston, set the backend for headless rendering
    os.environ.setdefault('GDK_BACKEND', 'x11')
elif not os.environ.get('DISPLAY') and not os.environ.get('GDK_BACKEND'):
    os.environ.setdefault('GDK_BACKEND', 'broadway')

# Ensure crabcakes package is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# SPEC-08 SP4: production now defaults the migration flag ON (main.py
# setdefault) — pin it OFF for the whole pytest process BEFORE any test
# module is collected. Collection-time imports (e.g. test_cli_nudge imports
# `main`) pull in agent.runtime, which reads CRABCAKES_MIGRATE_STORE at
# module-import time and latches _MIGRATE_STORE_ON_INIT; a fixture-time pin
# would run too late. Without this, an AgentRuntime() built later in the
# process could sweep the REAL config dir (the 2026-10-01 incident class —
# the autouse store override cannot prevent a sweep: the flag path clears
# the override itself). Bare os.environ, not monkeypatch: the value must be
# correct for every import of agent.runtime in this process — there is
# nothing to restore. The flag-default-off unit test in test_migration.py
# pins THIS behavior (module-level seam, import-order-dependent).
os.environ["CRABCAKES_MIGRATE_STORE"] = "0"


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
def isolate_transcript_store(tmp_path, tmp_path_factory, monkeypatch):
    """Point the persistence wrapper's store seam at a per-test tmp DB.

    SPEC-08 SP2: save_conversation_to_disk now dual-writes to a transcript
    store. Without this fixture, any test that saves a conversation without
    patching get_config_dir (e.g. tests/test_conversation.py patches only
    conversations_dir) would create/append the REAL user DB under the real
    config dir — test writes leaking into live user state.
    """
    from agent import persistence
    from utils.transcript_store import TranscriptStore

    # DB lives OUTSIDE tmp_path (shared base-temp sibling dir): the store
    # constructor creates the file eagerly, and scan_directory()-style tests
    # assert on their own tmp_path contents — a DB file there breaks them.
    db_dir = tmp_path_factory.mktemp("store-db")  # numbered -> unique per test
    store = TranscriptStore(db_path=str(db_dir / "transcript-test.db"))
    monkeypatch.setattr(persistence, "_store_override", store)
    yield
    store.close()


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    """
    Patch ~/.config/crabcakes to point at an isolated temp directory.
    All tests that touch config files use this fixture.
    """
    config_dir = tmp_path / ".config" / "crabcakes"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
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
