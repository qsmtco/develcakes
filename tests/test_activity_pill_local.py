# SPEC-16 SP2 — local text slices and tool starts drive the activity pill.

from unittest.mock import MagicMock

from ui.handlers.activity_handler import ActivityHandler
from ui.handlers.agent_runtime_handler import AgentRuntimeHandler


class _GLib:
    def timeout_add(self, _ms, _cb):
        return 1

    def timeout_add_seconds(self, _sec, _cb):
        return 1

    def source_remove(self, _sid):
        return True


def _pill(session="sk"):
    mc = MagicMock()
    mc.get_current_session_key.return_value = session
    target = MagicMock()
    h = ActivityHandler(target, mc, GLib_module=_GLib())
    return h, target


def test_first_delta_enters_streaming():
    h, target = _pill()
    h.on_agent_start("sk")
    h.on_chat_delta("Hello", "sk")
    assert h._state == "streaming"
    target.set_status_text.assert_called()
    text = target.set_status_text.call_args[0][0]
    assert "Generating" in text


def test_tool_then_more_text_returns_to_streaming():
    h, target = _pill()
    h.on_agent_start("sk")
    h.on_chat_delta("Hi", "sk")
    h.on_tool_use("read_file", "sk")
    assert h._state == "tool_use"
    assert "read_file" in target.set_status_text.call_args[0][0]
    before = h._streaming_token_count
    h.on_chat_delta(" more", "sk")
    assert h._state == "streaming"
    assert h._streaming_token_count == before + len(" more")


def test_empty_delta_does_not_leave_reasoning():
    h, _target = _pill()
    h.on_agent_start("sk")
    h.on_chat_delta("", "sk")
    assert h._state == "reasoning"


def test_end_still_done():
    h, target = _pill()
    h.on_agent_start("sk")
    h.on_chat_delta("x", "sk")
    h.on_agent_end("sk")
    assert h._state == "done"
    assert "Done" in target.set_status_text.call_args[0][0]


def _runtime():
    rt = AgentRuntimeHandler.__new__(AgentRuntimeHandler)
    rt._ended_sessions = set()
    rt._turn_tokens = {}
    rt._crh = MagicMock()
    rt._streaming_text = {}
    rt._delta_dispatch_pending = set()
    rt._delta_dirty = set()
    rt._last_delta_dispatch = {}
    rt._delta_throttle_sec = 0
    rt._GLib = None
    rt._on_stream_delta_cb = None
    rt._on_tool_start_cb = None
    rt._agents = {}
    rt._fh = None
    rt._active_project = None
    rt._pending_tool_args = {}
    rt._pending_exec_commands = {}
    rt._on_activity_bubble = None
    return rt


def test_text_delta_fires_new_slice_only():
    rt = _runtime()
    seen = []
    rt.set_on_stream_delta(lambda sk, delta: seen.append((sk, delta)))
    rt._on_text_delta("sk", "Hello")
    rt._on_text_delta("sk", " world")
    assert seen == [("sk", "Hello"), ("sk", " world")]
    assert rt._streaming_text["sk"] == "Hello world"


def test_text_delta_skips_empty_and_unset_callback():
    rt = _runtime()
    rt._on_text_delta("sk", "")
    assert rt._streaming_text == {}
    rt._on_stream_delta_cb = None
    rt._on_text_delta("sk", "x")
    assert rt._streaming_text["sk"] == "x"


def test_text_delta_callback_raise_still_accumulates():
    rt = _runtime()

    def boom(_sk, _delta):
        raise RuntimeError("pill down")

    rt.set_on_stream_delta(boom)
    rt._on_text_delta("sk", "kept")
    assert rt._streaming_text["sk"] == "kept"


def test_stale_delta_does_not_fire_pill():
    rt = _runtime()
    seen = []
    rt.set_on_stream_delta(lambda sk, delta: seen.append(delta))
    rt._turn_tokens["sk"] = object()
    rt._on_text_delta("sk", "stale", object())
    assert seen == []
    assert "sk" not in rt._streaming_text


def test_tool_start_fires_without_project_and_none_is_safe():
    rt = _runtime()
    seen = []
    rt._do_tool_call_start("sk", "read_file", {})
    assert seen == []
    rt.set_on_tool_start(lambda sk, name: seen.append((sk, name)))
    rt._do_tool_call_start("sk", "read_file", {})
    assert seen == [("sk", "read_file")]


def test_ended_session_tool_start_does_not_fire():
    rt = _runtime()
    seen = []
    rt.set_on_tool_start(lambda sk, name: seen.append(name))
    rt._ended_sessions.add("sk")
    rt._do_tool_call_start("sk", "read_file", {})
    assert seen == []


def test_tool_start_callback_raise_still_returns():
    rt = _runtime()

    def boom(_sk, _name):
        raise RuntimeError("pill down")

    rt.set_on_tool_start(boom)
    rt._do_tool_call_start("sk", "exec_command", {"command": "ls"})
    assert rt._pending_exec_commands["sk"] == "ls"
