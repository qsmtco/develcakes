# tests/test_live_bridge.py — SPEC-19 SP4 (two-phase action bridge).
#
# Layer 1: pure LiveBridge unit tests (no GTK).
# Layer 2: real-WebKit e2e (a live section awaits call(); approval resolves it).

import pytest

from utils.live_bridge import (
    CONSEQUENTIAL_METHODS,
    LiveBridge,
)


# ── 1. Pure unit tests ───────────────────────────────────────────────────


class SpyApprover:
    def __init__(self):
        self.calls = []

    def __call__(self, method, params, call_id):
        self.calls.append((method, params, call_id))


def test_consequential_methods_registry():
    assert CONSEQUENTIAL_METHODS == frozenset(
        {"exec_command", "write_file", "edit_file", "approve_exec"}
    )


def test_unknown_method_errors_without_approver():
    spy = SpyApprover()
    b = LiveBridge(approver=spy)
    r = b.dispatch("launch_missiles", {"x": 1})
    assert r["status"] == "error"
    assert "unknown method" in r["data"]["reason"]
    assert spy.calls == []  # approver NOT called for unknown methods
    assert r["id"]


def test_consequential_dispatch_is_pending_and_calls_approver():
    spy = SpyApprover()
    b = LiveBridge(approver=spy)
    r = b.dispatch("exec_command", {"cmd": "ls"})
    assert r["status"] == "pending"
    assert r["id"]
    assert len(spy.calls) == 1
    method, params, call_id = spy.calls[0]
    assert method == "exec_command"
    assert params == {"cmd": "ls"}
    assert call_id == r["id"]


def test_dispatch_never_executes_anything():
    """The bridge has NO execution branch — prove by spy: dispatch touches
    only the approver, nothing else."""
    spy = SpyApprover()
    b = LiveBridge(approver=spy)
    for m in ("exec_command", "write_file", "edit_file", "approve_exec"):
        r = b.dispatch(m, {"a": 1})
        assert r["status"] == "pending"
    assert len(spy.calls) == 4


def test_reject_unknown_id_is_silently_ignored():
    b = LiveBridge(approver=SpyApprover())
    b.dispatch("exec_command", {"c": 1})
    # Unknown id → no raise, recorded nothing.
    assert b.resolve("does-not-exist", True) is None


def test_resolve_records_outcome():
    spy = SpyApprover()
    b = LiveBridge(approver=spy)
    r = b.dispatch("exec_command", {"c": 1})
    pending_events = []
    b.resolve(r["id"], True, {"out": "ok"}, on_result=pending_events.append)
    assert pending_events and pending_events[0]["status"] == "ok"
    assert pending_events[0]["id"] == r["id"]


def test_resolve_deny_records_refused():
    b = LiveBridge(approver=SpyApprover())
    r = b.dispatch("write_file", {"p": "x"})
    events = []
    b.resolve(r["id"], False, None, on_result=events.append)
    assert events[0]["status"] == "refused"
    assert events[0]["id"] == r["id"]


def test_g6_no_approver_stays_pending_forever():
    """G6: with no approver wired, a consequential call is pending and NO
    execution/no crash happens — it never self-resolves."""
    b = LiveBridge(approver=None)
    r = b.dispatch("exec_command", {"cmd": "rm -rf /"})
    assert r["status"] == "pending"
    # Nothing to resolve it; the registry still holds it (bounded).
    assert r["id"] in b.pending_ids()


def test_pending_cap_55_to_50():
    spy = SpyApprover()
    b = LiveBridge(approver=spy, cap=50)
    ids = [b.dispatch("exec_command", {"n": i})["id"] for i in range(55)]
    assert len(b.pending_ids()) == 50
    # Oldest 5 dropped (FIFO).
    for old in ids[:5]:
        assert old not in b.pending_ids()
    for new in ids[5:]:
        assert new in b.pending_ids()


def test_dropped_pending_is_reported_failed():
    """The 5 dropped calls should be resolvable-as-failed, not silently
    vanished (the page's Promise must not hang on a dropped call)."""
    dropped = []
    b = LiveBridge(approver=SpyApprover(), cap=2, on_drop=dropped.append)
    b.dispatch("exec_command", {"n": 1})["id"]
    b.dispatch("exec_command", {"n": 2})["id"]
    b.dispatch("exec_command", {"n": 3})["id"]
    assert len(dropped) == 1
    assert dropped[0]["status"] == "error"


def test_approver_exception_does_not_crash_dispatch():
    def boom(method, params, call_id):
        raise RuntimeError("approver exploded")

    b = LiveBridge(approver=boom)
    r = b.dispatch("exec_command", {"c": 1})
    assert r["status"] == "pending"  # still pending; approver failure is isolated

# ── 2. Real-WebKit e2e (live section awaits call(); approval resolves it) ─

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import Gtk, GLib  # noqa: F401
except (ValueError, ImportError):  # pragma: no cover
    pytest.skip("WebKit 6.0 unavailable", allow_module_level=True)

import ui.views.chat_surface as cs_module
from ui.views.chat_surface import ChatSurface


def _present(surface):
    win = Gtk.Window()
    win.set_child(surface)
    win.set_default_size(640, 480)
    win.present()
    return win


def _pump(sec):
    ctx = GLib.MainContext.default()
    import time
    end = time.time() + sec
    while time.time() < end:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.02)


def _eval_main(surface, code, timeout=6):
    box = {}
    loop = GLib.MainLoop()

    def cb(v, r, data):
        try:
            box["v"] = v.evaluate_javascript_finish(r).to_string()
        except Exception:  # noqa: BLE001
            box["v"] = None
        loop.quit()

    v = surface._ensure_webview()
    v.evaluate_javascript(code, -1, None, None, None, cb, None)
    GLib.timeout_add_seconds(timeout, lambda: (loop.quit(), False)[1])
    loop.run()
    return box.get("v")


class _RecordingApprover:
    def __init__(self):
        self.calls = []

    def __call__(self, method, params, call_id):
        self.calls.append((method, params, call_id))


class TestE2EApproval:
    def test_pending_then_approve_sets_page_flag(self, monkeypatch):
        """A live section awaits window.develcakes.call('exec_command', ...);
        the bridge returns pending; the human approval resolves it; the
        Promise resolves and the page sets a flag."""
        monkeypatch.setattr(cs_module, "_live_js_enabled", lambda: True)
        approver = _RecordingApprover()
        s = ChatSurface(live_bridge_approver=approver)
        win = _present(s)
        try:
            s.append_live(
                "<div id='out'>waiting</div>\n"
                "<script>window.__approved=null;"
                "window.develcakes.call('exec_command',{cmd:'ls'})"
                ".then(function(r){document.getElementById('out').textContent='done:'+r.status;"
                "window.__approved=r.status;});</script>",
                "Coder",
            )
            s._drain_renders()
            _pump(1.5)
            # Let the poll drain the queue → dispatch → approver called.
            for _ in range(20):
                _pump(0.3)
                if approver.calls:
                    break
            assert approver.calls, "bridge approver never called"
            call_id = approver.calls[0][2]
            # No self-approval: still pending.
            got = _eval_main(s, "window.__approved === null ? 'pending' : window.__approved")
            assert got == "pending", f"self-approved! {got}"
            # Human approves via the wired resolution path.
            s.resolve_bridge_call(call_id, True, {"out": "ok"})
            _pump(1.0)
            got2 = _eval_main(s, "window.__approved || null")
            assert got2 == "ok", f"promise did not resolve ok: {got2}"
            assert _eval_main(s, "document.getElementById('out').textContent") == "done:ok"
        finally:
            win.close()
            s.destroy()

    def test_deny_resolves_refused(self, monkeypatch):
        monkeypatch.setattr(cs_module, "_live_js_enabled", lambda: True)
        approver = _RecordingApprover()
        s = ChatSurface(live_bridge_approver=approver)
        win = _present(s)
        try:
            s.append_live(
                "<div id='o2'>x</div>\n"
                "<script>window.__res=null;"
                "window.develcakes.call('write_file',{p:'x'})"
                ".then(function(r){window.__res=r.status;});</script>",
                "Coder",
            )
            s._drain_renders()
            for _ in range(20):
                _pump(0.3)
                if approver.calls:
                    break
            assert approver.calls
            call_id = approver.calls[0][2]
            s.resolve_bridge_call(call_id, False, None)
            _pump(1.0)
            assert _eval_main(s, "window.__res || null") == "refused"
        finally:
            win.close()
            s.destroy()

# ── F2 prompt source-shape guards (SPEC-19 SP4 Part D) ────────────────────

def test_agent_prompts_document_the_live_protocol():
    """All three agent prompts carry the SPEC-19 live-section protocol:
    the ```live fence, the no-src rule, and the develcakes.call bridge."""
    import pathlib
    for name in ("coder.md", "debugger.md", "supervisor.md"):
        src = pathlib.Path("prompts/system", name).read_text()
        assert "SPEC-19" in src, f"{name}: no SPEC-19 section"
        assert "```live" in src, f"{name}: live fence not documented"
        assert "develcakes.call" in src, f"{name}: bridge not documented"
        assert "<script src>" in src, f"{name}: no-src rule not documented"


# ── /clear store-plane (SPEC-08 /clear-store fix) ─────────────────────────


def test_clear_conversation_deletes_store_rows(tmp_path, monkeypatch):
    """The /clear data plane must delete transcript-store rows: the SP4A
    store-mode load hydrates any session whose JSON is absent — a JSON-only
    clear resurrects cleared history on the next load."""
    import sqlite3

    from utils.transcript_store import TranscriptStore
    db = tmp_path / "transcript.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY, session_key TEXT, seq INTEGER, epoch INTEGER DEFAULT 0, role TEXT, content TEXT, tool_calls TEXT, tool_call_id TEXT, tokens_used INTEGER DEFAULT 0, timestamp TEXT)")
    con.execute("CREATE TABLE sessions (session_key TEXT PRIMARY KEY, watermark INTEGER, epoch INTEGER DEFAULT 0, updated_at TEXT)")
    for i in range(5):
        con.execute("INSERT INTO turns (session_key, seq, role, content) VALUES (?, ?, 'user', 'x')", ("special:t", i))
    con.execute("INSERT INTO sessions (session_key, watermark) VALUES ('special:t', 4)")
    con.commit(); con.close()

    from agent import persistence
    real = TranscriptStore(str(db))
    monkeypatch.setattr(persistence, "_get_store", lambda: real)
    assert real.load_all("special:t"), "precondition: rows present"
    n = persistence.delete_session_rows("special:t")
    assert n == 5
    assert real.load_all("special:t") == []
    real.close()


def test_arh_clear_conversation_deletes_store_rows_behaviorally(tmp_path, monkeypatch):
    """BEHAVIORAL: /clear (clear_conversation) must call delete_session_rows —
    rows deleted on success; on store refusal the in-memory reset still lands
    (best-effort tolerance) and the rows stay intact."""
    import sqlite3, types
    from utils.transcript_store import TranscriptStore

    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY, session_key TEXT, seq INTEGER, epoch INTEGER DEFAULT 0, role TEXT, content TEXT, tool_calls TEXT, tool_call_id TEXT, tokens_used INTEGER DEFAULT 0, timestamp TEXT)")
    con.execute("CREATE TABLE sessions (session_key TEXT PRIMARY KEY, watermark INTEGER, epoch INTEGER DEFAULT 0, updated_at TEXT)")
    for i in range(3):
        con.execute("INSERT INTO turns (session_key, seq, role, content) VALUES ('special:sk', ?, 'user', 'x')", (i,))
    con.commit(); con.close()
    store = TranscriptStore(str(db))

    from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
    arh = AgentRuntimeHandler.__new__(AgentRuntimeHandler)
    # minimal attrs clear_conversation touches
    arh._agents = {"special:sk": types.SimpleNamespace(display_name="T")}
    arh._turn_attr = {}
    arh._runtimes = {}
    arh._fh = None

    conv = types.SimpleNamespace(messages=[1, 2, 3], step_count=2, total_tokens=9,
                                 total_cost=1.0, _token_estimate_cache=None)
    rt = types.SimpleNamespace(
        is_loop_active=lambda sk: False,
        get_conversation=lambda sk: conv)
    arh._runtimes = {"T": rt}

    monkeypatch.setattr("agent.persistence._get_store", lambda: store)
    monkeypatch.setattr(
        "utils.config.get_config_dir", lambda: str(tmp_path))  # no JSON file → FileNotFoundError path

    assert arh.clear_conversation("special:sk") is True
    assert conv.messages == [] and conv.step_count == 0
    assert store.load_all("special:sk") == []  # THE regression pin: store rows GONE

    # refusal path: active loop → rows INTACT (best-effort tolerance)
    rt.is_loop_active = lambda sk: True
    conv.messages = [1]
    store.append_turn("special:sk", "user", "keepme")
    assert arh.clear_conversation("special:sk") is False
    assert conv.messages == [1]
    assert store.load_all("special:sk"), "refusal must not delete rows"
    store.close()
