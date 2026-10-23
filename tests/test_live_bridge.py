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
    from gi.repository import GLib, Gtk
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
    """The shared context prompt carries the SPEC-19 live-section protocol:
    the ```live fence, the no-src rule, and the develcakes.call bridge.

    Context-efficiency item 3: the HTML/live rules live once, in
    crabcakes-context.md (loaded by every agent via prompt_loader identity
    set), instead of being duplicated in each role file.
    """
    import pathlib
    src = pathlib.Path("prompts/system", "crabcakes-context.md").read_text()
    assert "SPEC-19" in src, "no SPEC-19 section"
    # 898aaa54 dropped the literal ```live token from the prompt (printing a
    # triple-backtick run inside prompt text is itself a fence-discipline
    # hazard). The protocol is documented in prose now: assert that.
    assert "live-tagged fenced block" in src, "live fence not documented"
    assert "develcakes.call" in src, "bridge not documented"
    assert "<script src>" in src, "no-src rule not documented"
    # Role files must NOT duplicate it (single-copy rule).
    for name in ("coder.md", "debugger.md", "supervisor.md"):
        role = pathlib.Path("prompts/system", name).read_text()
        assert "SPEC-19" not in role, f"{name}: duplicate live-section copy"


def test_clear_conversation_deletes_json_only(tmp_path, monkeypatch):
    """/clear removes the session JSON. An active tool loop refuses and keeps it."""
    import types

    from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

    conv_dir = tmp_path / "conversations"
    conv_dir.mkdir()
    json_path = conv_dir / "agent:sk.json"
    json_path.write_text("{}", encoding="utf-8")

    arh = AgentRuntimeHandler.__new__(AgentRuntimeHandler)
    arh._agents = {"agent:sk": types.SimpleNamespace(display_name="T")}
    arh._turn_attr = {}
    arh._fh = None

    conv = types.SimpleNamespace(
        messages=[1, 2, 3],
        step_count=2,
        total_tokens=9,
        total_cost=1.0,
        _token_estimate_cache=None,
        task_card="notes",
        # SPEC-48 Phase 3 API: session reset invalidates the estimate cache.
        invalidate_token_estimate=lambda: None,
    )
    dropped: list[str] = []
    rt = types.SimpleNamespace(
        is_loop_active=lambda sk: False,
        get_conversation=lambda sk: conv,
        drop_read_cache=lambda sk: dropped.append(sk),
        drop_compile=lambda sk: dropped.append("compile:" + sk),
        drop_repo_map=lambda sk: dropped.append("map:" + sk),
    )
    arh._runtimes = {"T": rt}
    monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))

    assert arh.clear_conversation("agent:sk") is True
    assert conv.messages == [] and conv.step_count == 0
    assert conv.task_card == ""
    assert not json_path.exists()
    assert dropped == ["agent:sk", "compile:agent:sk", "map:agent:sk"]

    json_path.write_text("{}", encoding="utf-8")
    rt.is_loop_active = lambda sk: True
    conv.messages = [1]
    conv.task_card = "stay"
    dropped.clear()
    assert arh.clear_conversation("agent:sk") is False
    assert conv.messages == [1]
    assert conv.task_card == "stay"
    assert dropped == []
    assert json_path.exists()
