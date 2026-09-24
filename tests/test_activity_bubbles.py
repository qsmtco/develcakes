# tests/test_activity_bubbles.py
# Tests for Phase 2 of SPEC-smarter-chat-ux — Activity Bubbles.
#
# Architecture:
#   ActivityHandler.on_gateway_event() → fires _activity_bubble_callback(ActivityBubble)
#   ChatHandler._render_activity_bubble(bubble) → calls _render_activity_bubble_impl on main thread
#   ChatRenderHandler.render_sync("System", text, ...) → build_role_bubble("System", text)
#   build_role_bubble assigns .chat-bubble-System CSS class for activity bubble styling

import gi
gi.require_version('Gtk', '4.0')

import time

import pytest
from unittest.mock import MagicMock


class TestActivityBubbleModel:
    """ActivityBubble dataclass — format_text() produces correct bubble text."""

    def test_lifecycle_start(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="lifecycle_start", session_key="sk-1")
        assert b.format_text() == "thinking..."

    def test_tool_start(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="tool_start", session_key="sk-1", tool_name="web_search")
        assert b.format_text() == "search"

    def test_tool_end(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="tool_end", session_key="sk-1", tool_name="web_search", duration_ms=1247)
        assert b.format_text() == "search  1,247ms"

    def test_tool_error(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="tool_error", session_key="sk-1", tool_name="read_file")
        assert b.format_text() == "read file  failed"

    def test_plan(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="plan", session_key="sk-1", title="Refactor auth", steps=["Step 1", "Step 2", "Step 3"])
        text = b.format_text()
        assert "plan: Refactor auth" in text
        assert "3 steps" in text

    def test_approval_request(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="approval_request", session_key="sk-1", command="rm -rf /")
        assert b.format_text() == "approve: rm -rf /"

    def test_command_output(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="command_output", session_key="sk-1", tool_name="git diff", exit_code=0, duration_ms=4521)
        assert b.format_text() == "git diff  4,521ms"

    def test_patch(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="patch", session_key="sk-1", tool_name="edit_file", added=3, modified=7, deleted=1)
        text = b.format_text()
        assert "+3" in text
        assert "~7" in text
        assert "-1" in text

    def test_patch_partial(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="patch", session_key="sk-1", tool_name="edit_file", added=1, modified=0, deleted=0, icon="✏️")
        text = b.format_text()
        assert "+1" in text
        assert "~0" not in text
        assert "-0" not in text


class TestActivityHandlerActivityBubbles:
    """ActivityHandler fires _activity_bubble_callback for events ingested via on_gateway_event."""

    def test_lifecycle_start_fires_callback(self, fake_glib):
        from ui.handlers.activity_handler import ActivityHandler
        from models.activity import ActivityBubble
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        handler.on_gateway_event("agent", {
            "stream": "lifecycle",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "start", "startedAt": 12345}
        })

        cb.assert_called_once()
        bubble = cb.call_args[0][0]
        assert isinstance(bubble, ActivityBubble)
        assert bubble.type == "lifecycle_start"
        assert bubble.icon == "⏳"
        assert bubble.session_key == "sk-1"

    def test_tool_start_fires_callback(self, fake_glib):
        from ui.handlers.activity_handler import ActivityHandler
        from models.activity import ActivityBubble
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        handler.on_gateway_event("agent", {
            "stream": "item",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "start", "kind": "tool", "name": "web_search", "title": "web_search", "status": "running"}
        })

        cb.assert_called_once()
        bubble = cb.call_args[0][0]
        assert isinstance(bubble, ActivityBubble)
        assert bubble.type == "tool_start"
        assert bubble.tool_name == "web_search"
        assert bubble.icon == "🔧"

    def test_tool_end_fires_callback(self, fake_glib):
        from ui.handlers.activity_handler import ActivityHandler
        from models.activity import ActivityBubble
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        handler.on_gateway_event("agent", {
            "stream": "item",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "end", "kind": "tool", "name": "read_file", "title": "read_file", "status": "completed", "startedAt": 1000, "endedAt": 1083}
        })

        cb.assert_called_once()
        bubble = cb.call_args[0][0]
        assert bubble.type == "tool_end"
        assert bubble.tool_name == "read_file"
        assert bubble.duration_ms == 83
        assert bubble.icon == "✅"

    def test_tool_end_error_detected(self, fake_glib):
        from ui.handlers.activity_handler import ActivityHandler
        from models.activity import ActivityBubble
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        handler.on_gateway_event("agent", {
            "stream": "item",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "end", "kind": "tool", "name": "exec", "title": "exec", "status": "failed", "startedAt": 1000, "endedAt": 1500}
        })

        bubble = cb.call_args[0][0]
        assert bubble.type == "tool_error"
        assert bubble.icon == "❌"

    def test_plan_fires_callback(self, fake_glib):
        from ui.handlers.activity_handler import ActivityHandler
        from models.activity import ActivityBubble
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        handler.on_gateway_event("agent", {
            "stream": "plan",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {
                "phase": "start",
                "title": "Refactor auth module",
                "steps": [{"title": "Extract login logic"}, {"title": "Add tests"}]
            }
        })

        bubble = cb.call_args[0][0]
        assert bubble.type == "plan"
        assert bubble.title == "Refactor auth module"
        assert len(bubble.steps) == 2
        assert bubble.icon == "📋"

    def test_approval_requested_fires_callback(self, fake_glib):
        from ui.handlers.activity_handler import ActivityHandler
        from models.activity import ActivityBubble
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        handler.on_gateway_event("agent", {
            "stream": "approval",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "requested", "command": "rm -rf /", "reason": "cleanup", "approvalId": "ap-1"}
        })

        bubble = cb.call_args[0][0]
        assert bubble.type == "approval_request"
        assert bubble.command == "rm -rf /"
        assert bubble.reason == "cleanup"
        assert bubble.approval_id == "ap-1"
        assert bubble.icon == "🔒"

    def test_patch_end_fires_callback(self, fake_glib):
        from ui.handlers.activity_handler import ActivityHandler
        from models.activity import ActivityBubble
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        handler.on_gateway_event("agent", {
            "stream": "patch",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "end", "name": "edit_file", "added": ["a.py"], "modified": ["b.py"], "deleted": []}
        })

        bubble = cb.call_args[0][0]
        assert bubble.type == "patch"
        assert bubble.tool_name == "edit_file"
        assert bubble.added == 1
        assert bubble.modified == 1
        assert bubble.deleted == 0
        assert bubble.icon == "✏️"

    def test_command_output_end_fires_callback(self, fake_glib):
        """stream=command_output phase=end fires a command_output ActivityBubble."""
        from ui.handlers.activity_handler import ActivityHandler
        from models.activity import ActivityBubble
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        handler.on_gateway_event("agent", {
            "stream": "command_output",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {
                "phase": "end",
                "name": "exec",
                "title": "exec ls -la",
                "output": "total 42\ndrwxr-xr-x",
                "exitCode": 0,
                "durationMs": 2345,
                "status": "completed",
            }
        })

        cb.assert_called_once()
        bubble = cb.call_args[0][0]
        assert isinstance(bubble, ActivityBubble)
        assert bubble.type == "command_output"
        assert bubble.tool_name == "exec"
        assert bubble.command == "exec ls -la"
        assert bubble.output == "total 42\ndrwxr-xr-x"
        assert bubble.exit_code == 0
        assert bubble.duration_ms == 2345
        assert bubble.icon == "💻"
        assert bubble.agent_name == ""  # no agentName in payload, no AgentManager resolution

    def test_command_output_end_error_fires_callback(self, fake_glib):
        """stream=command_output phase=end with non-zero exit_code fires error bubble."""
        from ui.handlers.activity_handler import ActivityHandler
        from models.activity import ActivityBubble, ToolStatus
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        handler.on_gateway_event("agent", {
            "stream": "command_output",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {
                "phase": "end",
                "name": "exec",
                "title": "exec rm -rf /",
                "exitCode": 1,
                "durationMs": 100,
                "status": "failed",
            }
        })

        cb.assert_called_once()
        bubble = cb.call_args[0][0]
        assert isinstance(bubble, ActivityBubble)
        assert bubble.type == "command_output"
        assert bubble.exit_code == 1
        assert bubble.status == ToolStatus.ERROR

    def test_command_output_string_exit_code_zero_is_success(self, fake_glib):
        """BUGFIX-1 audit BUG A: string "0" exitCode must be treated as success.
        JSON serialization can deliver "0" as a string; int coercion handles it.
        """
        from ui.handlers.activity_handler import ActivityHandler
        from models.activity import ToolStatus
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        handler.on_gateway_event("agent", {
            "stream": "command_output",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {
                "phase": "end",
                "name": "exec",
                "title": "exec ls",
                "exitCode": "0",  # string, not int
                "status": "completed",
            }
        })

        bubble = cb.call_args[0][0]
        assert bubble.exit_code == 0  # coerced to int
        assert bubble.status == ToolStatus.SUCCESS

    def test_command_output_string_exit_code_one_is_error(self, fake_glib):
        """BUGFIX-1 audit BUG A: string "1" exitCode must be treated as error."""
        from ui.handlers.activity_handler import ActivityHandler
        from models.activity import ToolStatus
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        handler.on_gateway_event("agent", {
            "stream": "command_output",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {
                "phase": "end",
                "name": "exec",
                "title": "exec false",
                "exitCode": "1",  # string, not int
                "status": "failed",
            }
        })

        bubble = cb.call_args[0][0]
        assert bubble.exit_code == 1  # coerced to int
        assert bubble.status == ToolStatus.ERROR

    def test_command_output_status_failed_with_no_exit_code_is_error(self, fake_glib):
        """BUGFIX-1 audit BUG B: status='failed' with missing exitCode must show ERROR.
        Upstream may send status='failed' with no exitCode (timeout, killed signal).
        The error determination must honor status even when exit_code is 0/absent.
        """
        from ui.handlers.activity_handler import ActivityHandler
        from models.activity import ToolStatus
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        handler.on_gateway_event("agent", {
            "stream": "command_output",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {
                "phase": "end",
                "name": "exec",
                "title": "exec long-running-cmd",
                "status": "failed",
                # no exitCode field at all
            }
        })

        bubble = cb.call_args[0][0]
        assert bubble.status == ToolStatus.ERROR  # not SUCCESS

    def test_command_output_delta_does_not_fire(self, fake_glib):
        """stream=command_output phase=delta should NOT fire a bubble (ignored, same as spec)."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        handler.on_gateway_event("agent", {
            "stream": "command_output",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {
                "phase": "delta",
                "name": "exec",
                "output": "streaming...",
                "status": "running",
            }
        })

        cb.assert_not_called()

    def test_no_crash_when_callback_not_set(self, fake_glib):
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        # Don't call set_on_activity_bubble — should not crash
        handler.on_gateway_event("agent", {
            "stream": "item",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "start", "kind": "tool", "name": "web_search", "status": "running"}
        })

    def test_lifecycle_end_still_fires_lifecycle_callback(self, fake_glib):
        """lifecycle phase=end must still fire the lifecycle-completed callback."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        lc_cb = MagicMock()
        handler.set_on_lifecycle_completed(lc_cb)

        # Buffer some text first
        handler.on_gateway_event("agent", {
            "stream": "assistant",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"text": "Final response text"}
        })

        lc_cb.reset_mock()
        handler.on_gateway_event("agent", {
            "stream": "lifecycle",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "end"}
        })

        lc_cb.assert_called_once_with("sk-1", "Final response text")

    def test_tool_start_bubble_has_agent_name(self, fake_glib):
        """Tool bubbles carry agent_name from the event payload (spec §2.4).

        Regression test for PHASE 4 — guards against removing the
        agent_name=_agent_name keyword arg from the tool_start ActivityBubble
        construction site. Without it, the drawer would show "[Agent]" instead
        of the actual agent name.
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)
        handler.on_gateway_event("agent", {
            "stream": "item",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "start", "kind": "tool", "name": "web_search", "status": "running", "agentName": "Coder"},
        })
        bubble = cb.call_args[0][0]
        assert bubble.agent_name == "Coder"

    def test_tool_end_bubble_has_agent_name(self, fake_glib):
        """Tool end bubbles also carry agent_name (spec §2.4).

        Companion to test_tool_start_bubble_has_agent_name — covers the second
        ActivityBubble construction site (tool_end/tool_error) added in PHASE 4.
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)
        handler.on_gateway_event("agent", {
            "stream": "item",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {
                "phase": "end", "kind": "tool", "name": "web_search",
                "status": "ok", "startedAt": 1000, "endedAt": 2247,
                "agentName": "Debugger",
            },
        })
        bubble = cb.call_args[0][0]
        assert bubble.agent_name == "Debugger"
        # Sanity: duration_ms still works (regression check that we didn't break the line)
        assert bubble.duration_ms == 1247

    def test_tool_bubble_agent_name_defaults_to_empty(self, fake_glib):
        """When the payload omits agentName on tool events, agent_name defaults to ''.

        Defensive — per audit TODO, upstream may not always send agentName
        on stream=item events. The drawer falls back to "[Agent]" for unknown
        agents, which is acceptable.
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)
        handler.on_gateway_event("agent", {
            "stream": "item",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "start", "kind": "tool", "name": "web_search", "status": "running"},
            # Note: no agentName key
        })
        bubble = cb.call_args[0][0]
        assert bubble.agent_name == ""

    def test_tool_bubble_falls_back_to_agent_manager(self, fake_glib):
        """When data.agentName is empty, fall back to AgentManager.get_name(session_key)."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        # Mock AgentManager
        agent_mgr = MagicMock()
        agent_mgr.get_name = MagicMock(return_value="Coder")
        handler.set_agent_manager(agent_mgr)

        # Payload WITHOUT agentName — bug-trigger scenario
        handler.on_gateway_event("agent", {
            "stream": "item",
            "sessionKey": "agent:coder",
            "runId": "run-1",
            "data": {"phase": "start", "kind": "tool", "name": "web_search", "status": "running"},
            # NO "agentName" key
        })

        bubble = cb.call_args[0][0]
        assert bubble.agent_name == "Coder", f"expected AgentManager fallback, got {bubble.agent_name!r}"
        agent_mgr.get_name.assert_called_once_with("agent:coder")

    def test_lifecycle_falls_back_to_agent_manager(self, fake_glib):
        """Lifecycle event without agentName also falls back to AgentManager.

        Mirror of test_tool_bubble_falls_back_to_agent_manager for the
        lifecycle branch — both extraction sites use the same helper.
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        lifecycle_cb = MagicMock()
        handler.set_on_agent_lifecycle(lifecycle_cb)

        agent_mgr = MagicMock()
        agent_mgr.get_name = MagicMock(return_value="Debugger")
        handler.set_agent_manager(agent_mgr)

        handler.on_gateway_event("agent", {
            "stream": "lifecycle",
            "sessionKey": "agent:debugger",
            "runId": "run-1",
            "data": {"phase": "start"},  # no agentName
        })

        lifecycle_cb.assert_called_once_with("agent:debugger", "Debugger", "start")

    def test_agent_manager_exception_is_swallowed(self, fake_glib):
        """If AgentManager.get_name() raises, the resolution silently falls through to ''.

        Defensive — AgentManager may not be ready, may not have the session,
        may raise for any reason. The handler should not crash; the drawer
        will show "[Agent]" for the unknown agent.
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        agent_mgr = MagicMock()
        agent_mgr.get_name = MagicMock(side_effect=RuntimeError("not ready"))
        handler.set_agent_manager(agent_mgr)

        # Should not raise
        handler.on_gateway_event("agent", {
            "stream": "item",
            "sessionKey": "agent:coder",
            "runId": "run-1",
            "data": {"phase": "start", "kind": "tool", "name": "web_search", "status": "running"},
        })
        bubble = cb.call_args[0][0]
        assert bubble.agent_name == ""  # fallback chain exhausted

    def test_agent_manager_returns_empty_string(self, fake_glib):
        """If AgentManager.get_name() returns '', fall through to '' (drawer shows [Agent]).

        Edge case — AgentManager knows the session but has no display name
        (e.g., the agent hasn't been registered yet). agent_name stays "".
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        agent_mgr = MagicMock()
        agent_mgr.get_name = MagicMock(return_value="")
        handler.set_agent_manager(agent_mgr)

        handler.on_gateway_event("agent", {
            "stream": "item",
            "sessionKey": "agent:coder",
            "runId": "run-1",
            "data": {"phase": "start", "kind": "tool", "name": "web_search", "status": "running"},
        })
        bubble = cb.call_args[0][0]
        assert bubble.agent_name == ""

    def test_data_null_does_not_crash(self, fake_glib):
        """Event payload with data=None must not crash the handler (PHASE 7 Bug #4).

        Bug #4 root cause: payload.get('data', {}) returns the default {} only
        when the key is MISSING. When the key is present-but-null (data: None),
        the default is bypassed and downstream .get() crashes with AttributeError.
        _safe_data() helper coerces any non-dict to {} so all call sites are safe.
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        # Should not raise for any stream type when data is None
        for stream in ("assistant", "lifecycle", "item", "plan", "approval", "patch"):
            try:
                handler.on_gateway_event("agent", {
                    "stream": stream,
                    "sessionKey": "sk-1",
                    "runId": "r-1",
                    "data": None,  # explicit null
                })
            except Exception as e:
                pytest.fail(f"handler crashed on data=None for stream={stream!r}: {e}")

    def test_data_missing_does_not_crash(self, fake_glib):
        """Event payload with data key MISSING must not crash the handler.

        Defensive complement to test_data_null_does_not_crash — covers the
        case where upstream omits the 'data' key entirely.
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        for stream in ("assistant", "lifecycle", "item", "plan", "approval", "patch"):
            try:
                handler.on_gateway_event("agent", {
                    "stream": stream,
                    "sessionKey": "sk-1",
                    "runId": "r-1",
                    # NO "data" key at all
                })
            except Exception as e:
                pytest.fail(f"handler crashed on missing-data for stream={stream!r}: {e}")

    def test_data_non_dict_does_not_crash(self, fake_glib):
        """Event payload with data=42 (or other non-dict) must not crash the handler.

        Defensive — _safe_data() coerces any non-dict value to {}.
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)
        cb = MagicMock()
        handler.set_on_activity_bubble(cb)

        for bad_value in (42, "string-not-dict", [1, 2, 3], True, None):
            try:
                handler.on_gateway_event("agent", {
                    "stream": "item",
                    "sessionKey": "sk-1",
                    "runId": "r-1",
                    "data": bad_value,
                })
            except Exception as e:
                pytest.fail(f"handler crashed on data={bad_value!r}: {e}")

    def test_set_on_command_output_5_args(self, fake_glib):
        """AgentRuntimeHandler.set_on_command_output accepts 5 args (PHASE 7 Bug #7).

        Bug #7: spec §2.5 promised 5-arg signature
        (session_key, command, output, exit_code, duration_ms) but the
        implementation used 3 args, losing exit_code and duration_ms.
        """
        import inspect
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
        src = inspect.getsource(AgentRuntimeHandler)
        # Type annotation: should declare Callable[[str, str, str, int, int], None]
        assert "Callable[[str, str, str, int, int]" in src, (
            "set_on_command_output signature should be 5 args per spec §2.5"
        )
        # Firing site: should call with 5 args
        assert "_on_command_output(session_key, cmd, tail, exit_code, duration_ms)" in src, (
            "firing site should pass 5 args (session_key, command, output, exit_code, duration_ms)"
        )

    # ── Phase 4E: streaming token count field-name fix ────────────────
    # BUG: activity_handler.py:469 used to read payload["text"], but text arrives
    # at payload["message"]["content"]. Helper _extract_chat_text
    # normalizes the field. These tests pin the regression and the helper contract.

    def test_chat_delta_increments_token_count_from_string_content(self, fake_glib):
        """Regression test for Phase 4E: a chat delta with message.content='Hello, world!'
        must increment _streaming_token_count to 13. Pre-fix it stayed at 0 because
        the dispatcher read payload['text'] (which is not in the payload shape).
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)

        handler.on_gateway_event("chat", {
            "state": "delta",
            "sessionKey": "agent:test:1",
            "message": {"content": "Hello, world!"},
        })

        assert handler._streaming_token_count == 13, (
            f"Expected _streaming_token_count == 13 for 'Hello, world!', got {handler._streaming_token_count}"
        )

    def test_chat_delta_increments_token_count_across_multiple_deltas(self, fake_glib):
        """Three deltas with DISTINCT text must each contribute their length to the running
        counter. This pins the per-delta accumulation via the helper field path.

        Design note: per models/streaming.py:28 upstream sends cumulative text in
        production, so real deltas would be 'Hello', 'Hello world', 'Hello world!'
        and the += accumulator would over-count (5+11+12=28 for a 12-char final).
        That cumulative-vs-delta semantics quirk is out of scope for Phase 4E —
        use distinct delta texts here to keep the assertion deterministic and
        focused on the field-name fix.
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)

        handler.on_gateway_event("chat", {"state": "delta", "sessionKey": "agent:test:1", "message": {"content": "Hello"}})
        handler.on_gateway_event("chat", {"state": "delta", "sessionKey": "agent:test:1", "message": {"content": " world"}})
        handler.on_gateway_event("chat", {"state": "delta", "sessionKey": "agent:test:1", "message": {"content": "!"}})

        # 5 + 6 + 1 = 12 — sum of distinct delta lengths
        assert handler._streaming_token_count == 12, (
            f"Expected _streaming_token_count == 12 (sum of distinct delta lengths), "
            f"got {handler._streaming_token_count}"
        )

    def test_chat_delta_handles_list_of_blocks_form(self, fake_glib):
        """A chat delta with message.content as a list of text blocks must yield
        the concatenated text length. Two 'abc'/'def' blocks → 'abcdef' → 6.
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)

        handler.on_gateway_event("chat", {
            "state": "delta",
            "sessionKey": "agent:test:1",
            "message": {
                "content": [
                    {"type": "text", "text": "abc"},
                    {"type": "text", "text": "def"},
                ]
            },
        })

        assert handler._streaming_token_count == 6, (
            f"Expected _streaming_token_count == 6 for concatenated 'abcdef', "
            f"got {handler._streaming_token_count}"
        )

    def test_chat_delta_handles_input_image_block(self, fake_glib):
        """input_image blocks are skipped by the helper (no text content) and a
        trailing text block contributes its length. Image contributes 0.
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)

        handler.on_gateway_event("chat", {
            "state": "delta",
            "sessionKey": "agent:test:1",
            "message": {
                "content": [
                    {"type": "input_image", "image_url": "https://example.com/foo.png"},
                    {"type": "text", "text": "look at this"},
                ]
            },
        })

        assert handler._streaming_token_count == 12, (
            f"Expected _streaming_token_count == 12 (length of 'look at this'), "
            f"got {handler._streaming_token_count}"
        )

    def test_chat_delta_handles_missing_message_field(self, fake_glib):
        """A chat delta with no 'message' key must not raise and must not
        increment the counter. The helper safely returns ''.
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)

        # Should not raise
        handler.on_gateway_event("chat", {
            "state": "delta",
            "sessionKey": "agent:test:1",
        })

        assert handler._streaming_token_count == 0, (
            f"Expected _streaming_token_count == 0 when message key is absent, "
            f"got {handler._streaming_token_count}"
        )

    def test_chat_delta_handles_string_message_field(self, fake_glib):
        """Some hypothetical upstream variant might send message as a raw string.
        The helper's `else: content = msg_obj` branch handles this — the string
        is treated as the content directly and contributes its length.
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)

        handler.on_gateway_event("chat", {
            "state": "delta",
            "sessionKey": "agent:test:1",
            "message": "hello",
        })

        assert handler._streaming_token_count == 5, (
            f"Expected _streaming_token_count == 5 for raw-string message='hello', "
            f"got {handler._streaming_token_count}"
        )

    def test_extract_chat_text_returns_empty_for_empty_payload(self, fake_glib):
        """Direct unit test on the helper: _extract_chat_text({}) must return ''.
        Pins the helper's contract for the edge case where the dispatcher might
        call it with an empty dict.
        """
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)

        assert handler._extract_chat_text({}) == ""


# ── Class: TestActivityHandlerStateMachineGuard — BUGFIX-4 ──────


class TestActivityHandlerStateMachineGuard:
    """BUGFIX-4: state machine transitions (on_agent_start/end/error) must
    only fire for `stream == "lifecycle"` events. Other stream types
    (item, plan, approval, patch, command_output) must NOT trigger the
    state machine even if they carry a top-level `phase` field, because
    a future upstream payload could surface a `phase: "end"` on a non-
    lifecycle event and prematurely end the agent session.

    Pre-BUGFIX-4: the second `if event == "agent":` block fell to
    `else: phase = payload.get("phase", "")` for non-lifecycle streams,
    so a `stream="item"` with `phase="end"` would call `on_agent_end`.
    Post-BUGFIX-4: only `stream="lifecycle"` is processed.
    """

    def _make_handler(self, fake_glib):
        """Construct an ActivityHandler with main_content.get_current_session_key
        returning "sk-1" so the _is_ui_active() guard in _set_state passes.
        """
        from ui.handlers.activity_handler import ActivityHandler
        mc = MagicMock()
        mc.get_current_session_key.return_value = "sk-1"
        handler = ActivityHandler(
            feedbar=MagicMock(), main_content=mc, GLib_module=fake_glib,
        )
        return handler

    def test_item_end_does_not_trigger_state_machine(self, fake_glib):
        """stream=item phase=end must NOT trigger on_agent_end state transition."""
        handler = self._make_handler(fake_glib)
        # Start a session first — enters "reasoning" state
        handler.on_gateway_event("agent", {
            "stream": "lifecycle",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "start"},
        })
        assert handler._state == "reasoning", (
            f"lifecycle phase=start should transition to 'reasoning', got {handler._state!r}"
        )

        # Send a stream=item event with phase=end at the top level.
        # Pre-BUGFIX-4: this would call on_agent_end() → _state = "done".
        # Post-BUGFIX-4: state stays "reasoning".
        handler.on_gateway_event("agent", {
            "stream": "item",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "phase": "end",  # top-level phase — the latent trap
            "data": {"phase": "end", "kind": "tool", "name": "exec", "status": "completed"},
        })

        assert handler._state == "reasoning", (
            f"stream=item phase=end must NOT trigger on_agent_end state transition, "
            f"got _state={handler._state!r}"
        )

    def test_lifecycle_end_triggers_state_machine(self, fake_glib):
        """stream=lifecycle phase=end MUST trigger on_agent_end state transition (regression guard)."""
        handler = self._make_handler(fake_glib)
        handler.on_gateway_event("agent", {
            "stream": "lifecycle",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "start"},
        })
        assert handler._state == "reasoning"

        handler.on_gateway_event("agent", {
            "stream": "lifecycle",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "end"},
        })
        assert handler._state == "done", (
            f"stream=lifecycle phase=end MUST transition to 'done', got {handler._state!r}"
        )

    def test_item_start_does_not_trigger_state_machine(self, fake_glib):
        """stream=item phase=start (top-level) must NOT trigger on_agent_start.
        Variant of the regression guard — start is just as dangerous as end.
        """
        handler = self._make_handler(fake_glib)
        # Initial state: idle. Send a stream=item with phase=start.
        handler.on_gateway_event("agent", {
            "stream": "item",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "phase": "start",  # top-level phase on a non-lifecycle event
            "data": {"phase": "start", "kind": "tool", "name": "exec", "status": "starting"},
        })
        assert handler._state == "idle", (
            f"stream=item phase=start must NOT trigger on_agent_start, got {handler._state!r}"
        )

    def test_command_output_end_does_not_trigger_state_machine(self, fake_glib):
        """stream=command_output phase=end must NOT trigger on_agent_end.
        Real-world BUGFIX-1+ scenario: upstream sends command_output end events
        with a phase field. They must not end the agent session.
        """
        handler = self._make_handler(fake_glib)
        # Start the session
        handler.on_gateway_event("agent", {
            "stream": "lifecycle",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "start"},
        })
        assert handler._state == "reasoning"

        # Send command_output with phase=end (real upstream payload shape)
        handler.on_gateway_event("agent", {
            "stream": "command_output",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "phase": "end",  # top-level
            "data": {"phase": "end", "name": "exec", "exitCode": 0, "output": "ok"},
        })
        assert handler._state == "reasoning", (
            f"stream=command_output phase=end must NOT trigger on_agent_end, "
            f"got _state={handler._state!r}"
        )


class TestChatHandlerActivityBubbleRender:
    """ChatHandler integration tests for activity/lifecycle routing (Phase 2 SPEC-smarter-chat-ux).

    The 4 _render_activity_bubble tests were REMOVED in SPEC-activity-drawer Phase 1
    (those methods are deleted; activity now flows to ActivityDrawer, not chat).
    """

    def test_lifecycle_fallback_routes_to_project_tab(self, fake_glib):
        """Lifecycle fallback resolves to project tab when agent has no direct tab."""
        from ui.handlers.chat_handler import ChatHandler

        class FakeRouting:
            def get_project(self, sk):
                return "crabwatch" if sk == "agent:qaster" else None
        
        routing = FakeRouting()
        mock_project_chat_box = MagicMock()
        mock_mc = MagicMock()
        mock_mc.get_chat_box_for_session = lambda sk: (
            mock_project_chat_box if sk == "project:crabwatch" else None
        )
        mock_mc.get_current_session_key = MagicMock(return_value="project:crabwatch")

        handler = ChatHandler(
            main_content=mock_mc,
            agent_to_project=routing,
            projects_module=MagicMock(),
            GLib_module=fake_glib,
        )

        fake_render = MagicMock()
        fake_render.is_streaming.return_value = False
        fake_render.render_sync.return_value = MagicMock()
        handler._chat_render_handler = fake_render

        # Fire lifecycle fallback: agent key "agent:qaster" has project "crabwatch"
        handler._handle_lifecycle_completed("agent:qaster", "Response text from fallback")

        # Should have called _handle_final_response with project:crabwatch tab
        fake_render.render_sync.assert_called_once()
        mock_project_chat_box.append.assert_called_once()

    def test_is_ui_active_resolves_project_tab_for_agent(self, fake_glib):
        """_is_ui_active returns True when active tab is the project tab for the agent."""
        from ui.handlers.activity_handler import ActivityHandler

        class FakeRouting:
            def get_project(self, sk):
                return "crabwatch" if sk == "agent:qaster" else None
        
        routing = FakeRouting()
        mc = MagicMock()
        mc.get_current_session_key = MagicMock(return_value="project:crabwatch")

        ah = ActivityHandler(feedbar=MagicMock(), main_content=mc, GLib_module=fake_glib)
        ah.set_agent_routing(routing)

        # Agent key belongs to active project tab → considered active
        assert ah._is_ui_active("agent:qaster") is True
        # Agent key has no project routing → not active
        assert ah._is_ui_active("agent:unknown") is False
        # Direct tab match still works
        assert ah._is_ui_active("project:crabwatch") is True
        # None is always active
        assert ah._is_ui_active(None) is True

    def test_is_ui_active_no_routing_table(self, fake_glib):
        """Without routing table, _is_ui_active falls back to direct key comparison."""
        from ui.handlers.activity_handler import ActivityHandler

        mc = MagicMock()
        mc.get_current_session_key = MagicMock(return_value="project:crabwatch")

        ah = ActivityHandler(feedbar=MagicMock(), main_content=mc, GLib_module=fake_glib)
        # No routing table set
        ah._agent_to_project = None

        # Agent key → project tab (no routing table to resolve it)
        assert ah._is_ui_active("agent:qaster") is False
        # Direct match still works
        assert ah._is_ui_active("project:crabwatch") is True


class TestSystemBubbleCSS:
    """System bubbles get the .chat-bubble-System CSS class."""

    def _walk_css_classes(self, widget):
        """Walk all CSS classes from widget and its descendants (GTK4 compatible)."""
        classes = []
        if hasattr(widget, 'get_css_classes'):
            classes.extend(widget.get_css_classes())
        if hasattr(widget, 'get_first_child'):
            child = widget.get_first_child()
            while child is not None:
                classes.extend(self._walk_css_classes(child))
                sibling = child.get_next_sibling() if hasattr(child, 'get_next_sibling') else None
                child = sibling
        return classes

    def test_system_role_uses_system_css_class(self):
        from ui.views.chat_bubble import build_role_bubble
        widget = build_role_bubble("System", "test activity bubble")
        classes = self._walk_css_classes(widget)
        assert "chat-bubble-System" in classes

    def test_agent_role_uses_agent_css_class(self):
        from ui.views.chat_bubble import build_role_bubble
        widget = build_role_bubble("Agent", "hello")
        classes = self._walk_css_classes(widget)
        assert "chat-bubble-agent" in classes
        assert "chat-bubble-System" not in classes

    def test_you_role_uses_you_css_class(self):
        from ui.views.chat_bubble import build_role_bubble
        widget = build_role_bubble("You", "hello")
        classes = self._walk_css_classes(widget)
        assert "chat-bubble-you" in classes
        assert "chat-bubble-System" not in classes

class TestActivityPerfGuard:
    """AC3 Phase 1 Part C — single 250ms status ticker + skip-when-unchanged.

    Guards three perf behaviors on ActivityHandler:
      1. One GLib timer drives both live-update and idle-pulse production
         (250ms), not two 200ms timers.
      2. Consecutive ticks with unchanged state/phase/hops/elapsed-bucket skip
         the _update_feedbar markup rebuild AND _streaming_label construction.
      3. _resolve_agent_name fires at most ONCE per event, resolved
         lazily — per-delta assistant events must pay zero resolution cost.

    NOTE (flagged, not fixed here — outside Phase 1 scope): ActivityHandler
    .__init__ never initializes _agent_to_project; set_agent_routing() is the
    only setter. window.py wires set_agent_routing at build time, so
    production is safe, but a handler constructed without it crashes with
    AttributeError in _is_ui_active. Known trigger: any direct _set_state
    call in a fresh handler (pre-existing; not introduced by Part C).
    """

    def _make_handler(self, fake_glib):
        from ui.handlers.activity_handler import ActivityHandler
        mc = MagicMock()
        mc.get_current_session_key.return_value = "sk-1"  # _is_ui_active guard
        h = ActivityHandler(
            feedbar=MagicMock(), main_content=mc, GLib_module=fake_glib
        )
        # _is_ui_active reads _agent_to_project.get_project(sk); a bare MagicMock
        # would auto-return a truthy mock and fail the project-tab comparison.
        routing = MagicMock()
        routing.get_project.return_value = None
        h.set_agent_routing(routing)
        return h

    # ── Single ticker ──────────────────────────────────────────────────

    def test_single_ticker_started_at_250ms(self, fake_glib):
        """Entering an active state arms exactly ONE 250ms timer."""
        h = self._make_handler(fake_glib)
        armed = fake_glib.armed
        h._set_state("streaming", "sk-1")
        assert len(armed) == 1
        source_id, delay_ms, _cb = armed[0]
        assert delay_ms == 250

    def test_state_transition_restarts_single_ticker(self, fake_glib):
        """Spec C1/C4: transitioning streaming → tool_use stops the old ticker
        and arms a new one (public _stop_live_update/_stop_idle_pulse semantics
        preserved: the net effect of both stops plus one start)."""
        h = self._make_handler(fake_glib)
        h._set_state("streaming", "sk-1")
        id_after_streaming = set(fake_glib.armed_ids)
        h._set_state("tool_use", "sk-1")
        # Old ticker source was removed, exactly one new timer armed
        assert id_after_streaming.isdisjoint(set(fake_glib.armed_ids))
        assert len(fake_glib.armed) == 2  # one per _set_state call

    def test_idle_transition_uses_single_ticker(self, fake_glib):
        """Idle state also arms the single ticker (pulse branch), at 250ms.
        Transition from an active state first — the handler STARTS in idle and
        same-state transitions early-return without re-arming."""
        h = self._make_handler(fake_glib)
        h._set_state("streaming", "sk-1")
        h._set_state("idle", "sk-1")
        assert len(fake_glib.armed) == 2  # one ticker per transition
        assert fake_glib.armed[-1][1] == 250

    def test_stop_live_update_stops_single_ticker(self, fake_glib):
        """Spec C4: _stop_live_update keeps its public behavior — it now stops
        the single ticker (source removed, attribute cleared)."""
        h = self._make_handler(fake_glib)
        h._set_state("streaming", "sk-1")
        source_id = h._live_update_timer
        assert source_id is not None
        assert source_id in fake_glib.armed_ids  # ticker armed before stop
        h._stop_live_update()
        assert source_id not in fake_glib.armed_ids  # source_remove was called
        assert h._live_update_timer is None

    def test_stop_idle_pulse_stops_single_ticker_and_pulse(self, fake_glib):
        """Spec C4: _stop_idle_pulse keeps its public behavior — stops the
        single ticker AND clears the feedbar pulse flag."""
        h = self._make_handler(fake_glib)
        h._set_state("streaming", "sk-1")  # leave initial idle first
        h._set_state("idle", "sk-1")       # arm the idle branch of the ticker
        source_id = h._idle_pulse_timer
        assert source_id is not None
        assert source_id in fake_glib.armed_ids  # ticker armed before stop
        h._stop_idle_pulse()
        assert source_id not in fake_glib.armed_ids  # source_remove was called
        assert h._idle_pulse_timer is None
        h._feedbar.set_progress_pulse.assert_called_with(False)

    def test_live_update_tick_returns_true_when_active(self, fake_glib):
        """The tick callable keeps the GLib contract: True = keep ticking while
        state is active, False = stop."""
        h = self._make_handler(fake_glib)
        h._set_state("streaming", "sk-1")
        tick = fake_glib.armed[0][2]
        assert tick() is True  # streaming state → live-update branch continues
        h._set_state("done", "sk-1")
        assert tick() is False  # no longer active → tick out

    # ── Skip-when-unchanged (live-update branch) ───────────────────────

    def test_two_unchanged_ticks_skip_feedbar_rebuild(self, fake_glib, monkeypatch):
        """Spec C2: two consecutive ticks with unchanged state/phase/hops/
        elapsed-bucket → no _update_feedbar markup rebuild, no
        _streaming_label construction."""
        h = self._make_handler(fake_glib)
        monkeypatch.setattr(time, "monotonic", lambda: 1000.0)  # frozen clock
        h._set_state("streaming", "sk-1")  # tick 0: transition itself renders
        h._feedbar.set_status_text.reset_mock()
        tick = fake_glib.armed[0][2]

        tick()  # first tick: renders (cold skip-cache)
        assert h._feedbar.set_status_text.call_count == 1
        tick()  # second tick: nothing changed → must skip
        assert h._feedbar.set_status_text.call_count == 1, (
            "unchanged second tick must not rebuild feedbar markup"
        )

    def test_tick_with_changed_hops_rebuilds(self, fake_glib, monkeypatch):
        """""A hop-count change (upstream event progress) must rebuild the markup."""
        h = self._make_handler(fake_glib)
        monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
        h._set_state("reasoning", "sk-1")
        h._feedbar.set_status_text.reset_mock()
        tick = fake_glib.armed[0][2]
        tick()
        assert h._feedbar.set_status_text.call_count == 1
        h._event_hop_count["sk-1"] = 5  # progress moved
        tick()
        assert h._feedbar.set_status_text.call_count == 2, (
            "changed hop count must rebuild the feedbar markup"
        )

    def test_streaming_label_not_constructed_on_unchanged_tick(self, fake_glib, monkeypatch):
        """Spec C2 explicitly: _streaming_label() must not even be CONSTRUCTED
        on an unchanged tick (spy via monkeypatched instance attribute)."""
        h = self._make_handler(fake_glib)
        monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
        h._set_state("streaming", "sk-1")
        tick = fake_glib.armed[0][2]
        label_calls = []
        original = h._streaming_label
        h._streaming_label = lambda: (label_calls.append(1), original())[1]

        tick()
        assert len(label_calls) == 1
        tick()  # unchanged
        assert len(label_calls) == 1, (
            "unchanged tick must not construct the streaming label"
        )

    def test_idle_pulse_still_fires_every_tick(self, fake_glib):
        """The idle-pulse branch is an ANIMATION — it must keep pulsing on
        every tick even when nothing else changed (never skip-gated)."""
        h = self._make_handler(fake_glib)
        h._set_state("streaming", "sk-1")  # leave initial idle first
        h._set_state("idle", "sk-1")
        tick = fake_glib.armed[-1][2]
        tick()
        tick()
        tick()
        assert h._feedbar.pulse_progress.call_count == 3

    # ── Hoisted agent-name resolution ──────────────────────────────────

    def test_resolve_agent_name_at_most_once_per_event(self, fake_glib):
        """Spec C3: on_gateway_event resolves the agent name at most once —
        _resolve_agent_name must not fire per activity-bubble branch."""
        h = self._make_handler(fake_glib)
        resolve_calls = []
        original = h._resolve_agent_name
        h._resolve_agent_name = lambda p: (resolve_calls.append(1), original(p))[1]

        payload = {
            "sessionKey": "sk-1",
            "stream": "item",
            "data": {"kind": "tool", "phase": "start", "name": "read_file",
                     "agentName": "Coder"},
        }
        h.on_gateway_event("agent", payload)
        assert len(resolve_calls) <= 1, (
            f"_resolve_agent_name fired {len(resolve_calls)}x for one event"
        )
        assert resolve_calls, "a tool bubble needs a resolved agent name"

    def test_assistant_delta_event_pays_no_resolution_cost(self, fake_glib):
        """Spec C3 laziness clause: per-delta assistant events must NOT call
        _resolve_agent_name (they are the highest-frequency event type)."""
        h = self._make_handler(fake_glib)
        resolve_calls = []
        original = h._resolve_agent_name
        h._resolve_agent_name = lambda p: (resolve_calls.append(1), original(p))[1]

        payload = {
            "sessionKey": "sk-1",
            "stream": "assistant",
            "data": {"text": "some streaming text"},
        }
        h.on_gateway_event("agent", payload)
        assert len(resolve_calls) == 0, (
            "assistant deltas must not resolve agent names (hot path)"
        )

    def test_tool_bubble_still_carries_resolved_name(self, fake_glib):
        """Behavior preservation: the tool-start bubble still receives the
        payload agentName after the hoist (single resolution, same value)."""
        h = self._make_handler(fake_glib)
        bubbles = []
        h.set_on_activity_bubble(lambda b: bubbles.append(b))

        payload = {
            "sessionKey": "sk-1",
            "stream": "item",
            "data": {"kind": "tool", "phase": "start", "name": "read_file",
                     "agentName": "Coder"},
        }
        h.on_gateway_event("agent", payload)
        assert len(bubbles) == 1
        assert bubbles[0].agent_name == "Coder"
        assert bubbles[0].type == "tool_start"
