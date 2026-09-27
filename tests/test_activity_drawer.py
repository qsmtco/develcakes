# tests/test_activity_drawer.py
# Tests for SPEC-activity-drawer — Phase 3.
#
# Architecture:
#   ActivityBubble.to_drawer_row() — pure-Python dict builder
#   ActivityDrawer — pure GTK view; receives dicts via append_event()
#   ActivityHandler.set_on_agent_lifecycle — fires (sk, agent_name, phase)
#
# These tests cover:
#   1. TestToDrawerRow — 5 tests on ActivityBubble.to_drawer_row()
#   2. TestActivityDrawer — 6 tests on drawer state mutation
#   3. TestActivityHandlerLifecycleCallback — 3 tests on lifecycle firing
#
# GTK initialization: the ActivityDrawer test class patches
# _build_header and _build_list to no-ops so the drawer can be
# constructed in a headless test environment without a display.

import gi
gi.require_version('Gtk', '4.0')

import re
from unittest.mock import MagicMock, patch

import pytest


# ── Class 1: TestToDrawerRow — pure-Python dataclass tests ────────


class TestToDrawerRow:
    """ActivityBubble.to_drawer_row() returns a flat dict the drawer consumes."""

    def test_basic_fields_present(self):
        """All 12 spec-required keys exist in the returned dict."""
        from models.activity import ActivityBubble
        b = ActivityBubble(type="tool_start", session_key="sk-1", tool_name="web_search")
        row = b.to_drawer_row()
        required_keys = {
            "agent", "agent_name", "session_key", "activity_type", "icon",
            "type_label", "command", "file_path", "output", "exit_code",
            "duration", "duration_ms", "timestamp", "raw_text",
        }
        missing = required_keys - set(row.keys())
        assert not missing, f"missing required keys: {missing}"
        # Spot-check values for sanity
        assert row["session_key"] == "sk-1"
        assert row["activity_type"] == "tool_start"
        assert row["type_label"] == "tool"

    def test_agent_name_default_is_Agent(self):
        """When agent_name='', the dict's 'agent' key is 'Agent' (the fallback label)."""
        from models.activity import ActivityBubble
        b = ActivityBubble(type="tool_start", session_key="sk-1", tool_name="search")
        row = b.to_drawer_row()
        assert row["agent_name"] == ""
        assert row["agent"] == "Agent"

    def test_agent_name_propagates(self):
        """When agent_name is set, the dict's 'agent' key matches."""
        from models.activity import ActivityBubble
        b = ActivityBubble(
            type="tool_start", session_key="sk-1", tool_name="search",
            agent_name="Coder",
        )
        row = b.to_drawer_row()
        assert row["agent_name"] == "Coder"
        assert row["agent"] == "Coder"

    def test_timestamp_format_is_hms(self):
        """The 'timestamp' field is HH:MM:SS — three 2-digit groups separated by colons."""
        from models.activity import ActivityBubble
        b = ActivityBubble(type="tool_start", session_key="sk-1")
        row = b.to_drawer_row()
        ts = row["timestamp"]
        assert isinstance(ts, str)
        assert re.match(r"^\d{2}:\d{2}:\d{2}$", ts), f"timestamp {ts!r} is not HH:MM:SS"

    def test_duration_formatting(self):
        """format_duration rules: <1000ms → Nms; <60_000ms → N.Ns; >=60_000ms → Nm Ns; <=0 → ''."""
        from models.activity import ActivityBubble
        # 1247ms → "1.2s"
        b1 = ActivityBubble(type="tool_end", session_key="sk-1", duration_ms=1247)
        assert b1.to_drawer_row()["duration"] == "1.2s"
        # 60000ms → "1m 0s"
        b2 = ActivityBubble(type="tool_end", session_key="sk-1", duration_ms=60000)
        assert b2.to_drawer_row()["duration"] == "1m 0s"
        # 0ms → "" (per models/activity.py: ms <= 0 returns "")
        b3 = ActivityBubble(type="tool_end", session_key="sk-1", duration_ms=0)
        assert b3.to_drawer_row()["duration"] == ""
        # 847ms → "847ms"
        b4 = ActivityBubble(type="tool_end", session_key="sk-1", duration_ms=847)
        assert b4.to_drawer_row()["duration"] == "847ms"

    def test_exit_code_only_for_command_output(self):
        """For non-command_output types, exit_code is None; for command_output, it's the bubble's value."""
        from models.activity import ActivityBubble
        # non-command_output → None
        b1 = ActivityBubble(type="tool_end", session_key="sk-1", exit_code=42)
        assert b1.to_drawer_row()["exit_code"] is None
        # command_output → preserves value (0 is kept, non-zero too)
        b2 = ActivityBubble(type="command_output", session_key="sk-1", exit_code=0)
        assert b2.to_drawer_row()["exit_code"] == 0
        b3 = ActivityBubble(type="command_output", session_key="sk-1", exit_code=127)
        assert b3.to_drawer_row()["exit_code"] == 127

    def test_type_label_mapping(self):
        """command_output → 'exec', lifecycle_start → 'lifecycle', plan → 'plan', etc.

        Bug 2 (SPEC-AUDIT-CLEANUP-1): the label mapping has ONE home —
        models.activity.activity_type_label — covering all 9 gateway event
        types, and the drawer must use it (see the behavioral drawer test in
        TestActivityDrawer).
        """
        from models.activity import ActivityBubble
        cases = [
            ("command_output", "exec"),
            ("lifecycle_start", "lifecycle"),
            ("lifecycle_end", "lifecycle"),
            ("plan", "plan"),
            ("approval_request", "approval"),
            ("patch", "patch"),
            ("tool_start", "tool"),
            ("tool_end", "tool"),
            ("tool_error", "tool"),
        ]
        for activity_type, expected_label in cases:
            b = ActivityBubble(type=activity_type, session_key="sk-1")
            assert b.to_drawer_row()["type_label"] == expected_label, \
                f"{activity_type} should map to {expected_label!r}"
        # Direct: the public single-source-of-truth helper (Bug 2 rename).
        from models.activity import activity_type_label
        for activity_type, expected_label in cases:
            assert activity_type_label(activity_type) == expected_label, \
                f"activity_type_label({activity_type!r}) should be {expected_label!r}"
        # Unknown types pass through verbatim; empty string maps to empty string.
        assert activity_type_label("unknown_type") == "unknown_type"
        assert activity_type_label("") == ""

    def test_format_duration_none_and_zero_guard(self):
        """Bug 2: format_duration must return '' for None / 0 / negative.

        The drawer's old local copy raised TypeError on None (no guard);
        the single models implementation must keep the guard.
        """
        from models.activity import format_duration
        assert format_duration(None) == ""
        assert format_duration(0) == ""
        assert format_duration(-5) == ""
        # Envelope (Debugger 2b obs): non-numeric external input → "", no crash.
        assert format_duration("") == ""
        assert format_duration([]) == ""
        # Sanity: positive values still format per the documented rules.
        assert format_duration(847) == "847ms"
        assert format_duration(1247) == "1.2s"
        assert format_duration(60000) == "1m 0s"


# ── Class 2: TestActivityDrawer — drawer state mutation tests ────


class TestActivityDrawer:
    """ActivityDrawer state mutation: append_event, counter-collapse, filters, clear.

    GTK widget construction is patched to no-ops so the tests run
    headless. We test the data-state methods directly: append_event,
    on_agent_start, on_agent_end, clear_events, _passes_filter.
    """

    @pytest.fixture
    def drawer(self, monkeypatch):
        """Construct an ActivityDrawer with GTK widget builders patched to no-ops."""
        # Patch the GTK widget builders to no-ops before any drawer code runs
        # This avoids the need for an actual GTK display.
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._build_header",
            lambda self: None,
        )
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._build_list",
            lambda self: None,
        )
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._apply_expanded_state",
            lambda self: None,
        )
        # Patch the helper methods that touch GTK widgets
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._update_count_label",
            lambda self: None,
        )
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._trim_old_rows_if_needed",
            lambda self: None,
        )
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._auto_scroll_to_bottom",
            lambda self: None,
        )
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._build_separator_widget",
            lambda self, text: MagicMock(),
        )

        # Mock Gtk widgets: provide just enough surface for the drawer to track state
        fake_list = MagicMock()
        fake_list.get_row_at_index = MagicMock(return_value=None)
        fake_list.append = MagicMock()
        fake_list.remove = MagicMock()

        # Patch Gtk.Box to return a mock that the drawer's __init__ can super() into
        from gi.repository import Gtk
        monkeypatch.setattr(Gtk, "Box", MagicMock)

        from ui.views.activity_drawer import ActivityDrawer
        d = ActivityDrawer()
        # Inject the fake list — drawer reads self._list for append_event logic
        d._list = fake_list
        # FILTERFIX-1: inject the popover-box attributes that _build_header
        # would normally create. Tests patch _build_header to a no-op, so
        # the production code that sets _agent_popover_box / _type_popover_box
        # never runs. We set them here so the new _refresh_filter_popovers()
        # call in append_event doesn't AttributeError.
        d._agent_popover = MagicMock()
        d._agent_popover_box = MagicMock()
        d._agent_popover_box.get_first_child.return_value = None
        d._type_popover = MagicMock()
        d._type_popover_box = MagicMock()
        d._type_popover_box.get_first_child.return_value = None
        # Filter buttons are also injected as MagicMocks so _refresh_filter_popovers
        # (which passes them as label_widget) doesn't AttributeError either.
        # get_popover() must return the popover that set_popover() was called with —
        # this is the contract that makes Gtk.MenuButton open the dropdown on click.
        d._agent_filter_btn = MagicMock()
        d._agent_filter_btn.get_popover.return_value = d._agent_popover
        d._type_filter_btn = MagicMock()
        d._type_filter_btn.get_popover.return_value = d._type_popover
        return d

    def test_append_event_new_row(self, drawer):
        """Fresh drawer: append_event → 1 row, _last_row_key set, _total_count=1."""
        row = {
            "agent": "Coder",
            "activity_type": "tool_start",
            "type_label": "tool",
            "icon": "🔧",
        }
        drawer.append_event(row)
        assert drawer._total_count == 1
        assert drawer._last_row_key == ("Coder", "tool_start")
        assert drawer._list.append.called

    def test_append_event_counter_collapse(self, drawer):
        """Two same-(agent, type) events → counter collapsed, but only 1 list.append call."""
        row = {
            "agent": "Coder",
            "activity_type": "tool_start",
            "type_label": "tool",
            "icon": "🔧",
        }
        drawer.append_event(row)
        drawer.append_event(row)  # same key
        # First call appends; second call mutates in place (no new list.append)
        assert drawer._list.append.call_count == 1, \
            f"expected 1 list.append, got {drawer._list.append.call_count}"
        assert drawer._total_count == 2  # both events counted

    def test_append_event_different_type_new_row(self, drawer):
        """Same agent, different activity_type → 2 list.append calls (counter chain broken)."""
        row_tool = {
            "agent": "Coder", "activity_type": "tool_start", "type_label": "tool", "icon": "🔧",
        }
        row_plan = {
            "agent": "Coder", "activity_type": "plan", "type_label": "plan", "icon": "📋",
        }
        drawer.append_event(row_tool)
        drawer.append_event(row_plan)
        assert drawer._list.append.call_count == 2
        assert drawer._total_count == 2
        assert drawer._last_row_key == ("Coder", "plan")

    def test_summary_uses_single_label_mapping(self, drawer, monkeypatch):
        """Bug 2 (SPEC-AUDIT-CLEANUP-1): the drawer must render the SAME
        labels as models.activity for ALL 9 gateway event types.

        Red pre-fix: the drawer's local _type_label copy missed
        lifecycle_end/tool_start/tool_end/tool_error and fell through to the
        raw string (e.g. "tool_error" rendered verbatim instead of "tool").
        """
        captured = []
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._build_row_widget",
            lambda self, row, count: captured.append(
                self._format_summary(row, count=count)) or MagicMock(),
        )
        for activity_type, expected_label in [
            ("command_output", "exec"),
            ("lifecycle_start", "lifecycle"),
            ("lifecycle_end", "lifecycle"),
            ("plan", "plan"),
            ("approval_request", "approval"),
            ("patch", "patch"),
            ("tool_start", "tool"),
            ("tool_end", "tool"),
            ("tool_error", "tool"),
            ("unknown_type", "unknown_type"),  # passthrough
            ("", ""),                          # empty → empty
        ]:
            row = {
                "agent": "Coder",
                "activity_type": activity_type,
                "type_label": "",  # force the fallback path that was drifted
                "icon": "",
            }
            summary = drawer._format_summary(row, count=1)
            parts = summary.split("  ")
            assert expected_label in parts, (
                f"summary for {activity_type!r} must contain label "
                f"{expected_label!r}; got parts {parts}"
            )

    def test_counter_summary_survives_none_duration(self, drawer, monkeypatch):
        """Bug 2: the models format_duration None-guard must hold on the
        drawer's counter-summary path (on_agent_end), where the old local
        _format_duration copy raised TypeError on None.
        """
        captured_text = []
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._build_separator_widget",
            lambda self, text: captured_text.append(text) or MagicMock(),
        )
        # Draw the None through the same call the drawer makes: int() of a
        # missing counter value would be a TypeError — exercise the helper
        # exactly as on_agent_end does, including a None total.
        from ui.views.activity_drawer import format_duration
        assert format_duration(None) == ""
        # And the end-to-end path: a counter with total_duration_ms=None.
        drawer._agent_counters["Coder"] = {"count": 2, "total_duration_ms": None}
        drawer.on_agent_end("sk-1", "Coder")
        assert len(captured_text) == 1
        assert "2 events" in captured_text[0], (
            f"summary {captured_text[0]!r} should report 2 events even with a "
            f"None total_duration_ms (Bug 2 None-guard)"
        )
        assert "ended" not in captured_text[0]

    def test_filter_drop_unmatched(self, drawer):
        """When _visible_agents = {'Coder'}, an event with agent='Debugger' is dropped (not appended)."""
        drawer._visible_agents = {"Coder"}
        row = {
            "agent": "Debugger",
            "activity_type": "tool_start",
            "type_label": "tool",
            "icon": "🔧",
        }
        drawer.append_event(row)
        # Row was dropped, not appended
        assert drawer._list.append.call_count == 0
        # But total_count still increments
        assert drawer._total_count == 1

    def test_filter_pass_matched(self, drawer):
        """When _visible_agents = {'Coder'}, an event with agent='Coder' is appended."""
        drawer._visible_agents = {"Coder"}
        row = {
            "agent": "Coder",
            "activity_type": "tool_start",
            "type_label": "tool",
            "icon": "🔧",
        }
        drawer.append_event(row)
        assert drawer._list.append.call_count == 1
        assert drawer._total_count == 1

    def test_filter_type_set_blocks_non_matching(self, drawer):
        """Type filter is also AND — non-matching type drops the row even if agent matches."""
        drawer._visible_types = {"tool_start"}
        row = {
            "agent": "Coder",
            "activity_type": "plan",  # not in the filter set
            "type_label": "plan",
            "icon": "📋",
        }
        drawer.append_event(row)
        assert drawer._list.append.call_count == 0
        assert drawer._total_count == 1

    def test_clear_events_resets_state(self, drawer):
        """clear_events() empties _total_count, _last_row_key, _agent_counters AND iterates the list."""
        row = {
            "agent": "Coder", "activity_type": "tool_start", "type_label": "tool", "icon": "🔧",
        }
        drawer.append_event(row)
        drawer.append_event(row)
        assert drawer._total_count == 2
        assert drawer._last_row_key is not None
        # Mock get_row_at_index to return a real-looking row, then None to terminate the loop
        fake_row = MagicMock()
        drawer._list.get_row_at_index.side_effect = [fake_row, fake_row, None]
        drawer.clear_events()
        # NEW: assert the list was actually iterated
        assert drawer._list.remove.call_count == 2, \
            f"expected clear_events to call .remove() 2 times, got {drawer._list.remove.call_count}"
        # Existing state assertions
        assert drawer._total_count == 0
        assert drawer._last_row_key is None
        assert drawer._agent_counters == {}

    def test_passes_filter_empty_set_passes_all(self, drawer):
        """Empty filter sets pass everything (default behavior)."""
        assert drawer._visible_agents == set()
        assert drawer._visible_types == set()
        assert drawer._passes_filter("Coder", "tool_start") is True
        assert drawer._passes_filter("Anything", "any_type") is True

    def test_passes_filter_agent_filter_active(self, drawer):
        """Non-empty _visible_agents only passes rows in the set."""
        drawer._visible_agents = {"Coder", "Debugger"}
        assert drawer._passes_filter("Coder", "tool_start") is True
        assert drawer._passes_filter("Debugger", "plan") is True
        assert drawer._passes_filter("Crabcakes", "tool_start") is False

    def test_passes_filter_handles_non_string_agent(self, drawer):
        """BUGFIX-9: _passes_filter must not crash on non-string agent values.

        Pre-BUGFIX-9, `agent not in self._visible_agents` would raise TypeError
        when agent was None or an int. Post-fix, non-string agents are coerced
        to a sensible string (None -> "Agent"; int -> str(int)) before the
        membership test.
        """
        drawer._visible_agents = {"Coder", "Agent"}
        # String agent still works
        assert drawer._passes_filter("Coder", "tool_start") is True
        # None is coerced to "Agent" — matches the drawer fallback for missing
        # agent names, so it lands in the "Agent" bucket.
        assert drawer._passes_filter(None, "tool_start") is True
        # int is coerced to its str form — no crash, and the resulting str
        # is unlikely to match any agent, so this should fail the filter
        # rather than the membership check itself.
        assert drawer._passes_filter(42, "tool_start") is False
        # Empty filter set still passes everything regardless of type
        drawer._visible_agents = set()
        assert drawer._passes_filter(None, "tool_start") is True
        assert drawer._passes_filter(42, "tool_start") is True

    def test_passes_filter_handles_non_string_activity_type(self, drawer):
        """BUGFIX-9: _passes_filter must not crash on non-string activity_type.

        Symmetric guard to the agent check — malformed activity_type values
        (None, int) must be coerced before the `in self._visible_types` test.
        """
        drawer._visible_types = {"tool_start"}
        # String type matches
        assert drawer._passes_filter("Coder", "tool_start") is True
        # None is coerced to "" — won't match the filter set, so it drops.
        assert drawer._passes_filter("Coder", None) is False
        # int is coerced to "42" — also won't match.
        assert drawer._passes_filter("Coder", 42) is False
        # Empty filter set passes regardless of type
        drawer._visible_types = set()
        assert drawer._passes_filter("Coder", None) is True
        assert drawer._passes_filter("Coder", 42) is True

    def test_filter_buttons_have_popovers(self, drawer):
        """FILTERFIX-1: filter buttons must be configured with set_popover()
        so GTK4 auto-opens the dropdown on click. The previous implementation
        relied on the broken 'activate' signal which never fires on
        Gtk.MenuButton in GTK4.

        We verify the API contract: each filter button exposes get_popover()
        returning the popover that was set via set_popover(). The fixture
        injects MagicMock for the buttons (Gtk.MenuButton requires a display
        to construct in tests), so we can't do an isinstance check, but
        MagicMock supports the full MenuButton API surface.
        """
        # Popovers should be set on the buttons (this is what makes them open).
        assert drawer._agent_filter_btn.get_popover() is not None, (
            "FILTERFIX-1: _agent_filter_btn.get_popover() must return the popover, "
            "not None — set_popover() is what makes MenuButton open on click"
        )
        assert drawer._type_filter_btn.get_popover() is not None, (
            "FILTERFIX-1: _type_filter_btn.get_popover() must return the popover, "
            "not None — set_popover() is what makes MenuButton open on click"
        )
        # The popovers should contain the inner boxes that hold the checkboxes.
        assert drawer._agent_popover is drawer._agent_filter_btn.get_popover()
        assert drawer._type_popover is drawer._type_filter_btn.get_popover()

    def test_append_event_refreshes_filter_popovers(self, drawer):
        """FILTERFIX-1: append_event must call _refresh_filter_popovers so
        newly-seen agents/types appear in the filter popover content.

        Pre-FILTERFIX-1, the popovers were built only on first click (via
        _show_filter_popover, which is now removed). Post-fix, _build_filter_popover_content
        is called from append_event and clears + rebuilds the box.
        """
        # Before any events, the popover box has no children (fixture sets
        # get_first_child.return_value = None).
        assert drawer._agent_popover_box.get_first_child() is None
        # Spy on the refresh method to verify it runs.
        refresh_calls = []
        original_refresh = drawer._refresh_filter_popovers
        drawer._refresh_filter_popovers = lambda: (refresh_calls.append(1) or original_refresh())
        try:
            drawer.append_event({
                "agent": "Coder",
                "activity_type": "tool_start",
                "type_label": "tool",
                "icon": "🔧",
            })
            # _refresh_filter_popovers was called from append_event
            assert len(refresh_calls) == 1
            # _known_agents / _known_types were updated as a side effect
            assert "Coder" in drawer._known_agents
            assert "tool_start" in drawer._known_types
        finally:
            drawer._refresh_filter_popovers = original_refresh

    def test_build_filter_popover_content_clears_and_repopulates(self, drawer):
        """FILTERFIX-1: _build_filter_popover_content must clear the box
        and rebuild the checkbox list with the current known/visible sets.
        Guards against stale state if the same drawer is reused across
        multiple sessions.
        """
        # Pre-seed the box with a "stale" child to verify clearing works.
        stale = MagicMock()
        # get_first_child returns stale on first call, None on second
        drawer._agent_popover_box.get_first_child.side_effect = [stale, None]
        # Simulate: "All agents" + "Coder" + "Debugger" should be appended.
        # The fixture's Gtk.Box = MagicMock, so box.append is a MagicMock.
        drawer._build_filter_popover_content(
            drawer._agent_popover_box, "agent",
            {"Coder", "Debugger"}, set(),  # all known, none visible (filter off)
            drawer._agent_filter_btn,
            new_label_fn=lambda n: f"Agent: {n}" if n else "Agent: all",
        )
        # The stale child was removed
        assert drawer._agent_popover_box.remove.called
        # get_first_child was called at least twice (once to find stale, once to confirm None)
        assert drawer._agent_popover_box.get_first_child.call_count >= 2
        # Three appends: "All agents" + "Coder" + "Debugger" (sorted)
        assert drawer._agent_popover_box.append.call_count == 3

    def test_popovers_not_refreshed_when_known_sets_unchanged(self, drawer, monkeypatch):
        """FILTERFIX-1 audit: _refresh_filter_popovers must only be called when
        _known_agents or _known_types actually changes.

        Pre-audit, every append_event destroyed and recreated ~18 widgets per
        popover, even when the agent/type was already known. This test guards
        the perf bug from regressing.
        """
        refresh_spy = MagicMock()
        monkeypatch.setattr(drawer, "_refresh_filter_popovers", refresh_spy)

        # First event — new agent/type, refresh expected
        drawer.append_event({"agent": "Coder", "activity_type": "tool_start", "type_label": "tool", "icon": "🔧"})
        assert refresh_spy.call_count == 1, "First event with new agent/type should refresh"

        # Second event — same agent AND same type, no refresh expected
        drawer.append_event({"agent": "Coder", "activity_type": "tool_start", "type_label": "tool", "icon": "🔧"})
        assert refresh_spy.call_count == 1, (
            "Should NOT refresh when both agent and type are unchanged "
            "(FILTERFIX-1 audit: avoid widget churn on every event)"
        )

        # Third event — new agent, refresh expected
        drawer.append_event({"agent": "Debugger", "activity_type": "plan", "type_label": "plan", "icon": "📋"})
        assert refresh_spy.call_count == 2, "New agent should trigger a refresh"

        # Fourth event — same agent as #3, but new type, refresh expected
        drawer.append_event({"agent": "Debugger", "activity_type": "approval", "type_label": "approval", "icon": "✅"})
        assert refresh_spy.call_count == 3, "New type (with known agent) should trigger a refresh"

        # Fifth event — back to "Coder" agent, "tool_start" type, both already known, no refresh
        drawer.append_event({"agent": "Coder", "activity_type": "tool_start", "type_label": "tool", "icon": "🔧"})
        assert refresh_spy.call_count == 3, "Re-using known agent+type should not refresh"

    def test_known_sets_updated_even_when_filter_blocks_event(self, drawer):
        """FILTERFIX-2: events that fail the filter check must still update
        _known_agents / _known_types so they appear in the dropdown.

        Pre-FILTERFIX-2, the known-set update happened AFTER the early-return
        in append_event. If a filter blocked a new agent's first event, that
        agent was never added to _known_agents and could never be re-enabled
        from the dropdown.
        """
        # Set a filter that blocks all events (only "Coder"/"tool_start" pass)
        drawer._visible_agents = {"Coder"}
        drawer._visible_types = {"tool_start"}

        # Send a Debugger/plan event — blocked by the filter
        drawer.append_event({
            "agent": "Debugger",
            "activity_type": "plan",
            "type_label": "plan",
            "icon": "📋",
        })

        # _known_agents / _known_types MUST include "Debugger" and "plan"
        # even though the row was filtered out and not appended to the list.
        assert "Debugger" in drawer._known_agents, (
            "FILTERFIX-2: filtered-out events must still update _known_agents "
            "so the agent is discoverable in the dropdown"
        )
        assert "plan" in drawer._known_types, (
            "FILTERFIX-2: filtered-out events must still update _known_types "
            "so the type is discoverable in the dropdown"
        )

    def test_filtered_event_does_not_increment_visible_rows_but_still_counts(self, drawer):
        """FILTERFIX-2: a filtered-out event does not add a visible row,
        but does increment _total_count (existing behavior). Companion to
        test_known_sets_updated_even_when_filter_blocks_event — verifies
        we preserved the original counting semantics after the reorder.
        """
        drawer._visible_agents = {"Coder"}
        drawer._visible_types = {"tool_start"}

        initial_total = drawer._total_count
        # Send a blocked event
        drawer.append_event({
            "agent": "Debugger",
            "activity_type": "plan",
            "type_label": "plan",
            "icon": "📋",
        })
        # _total_count went up (existing semantics — count is global, not filtered)
        assert drawer._total_count == initial_total + 1
        # But no row was appended to the list (filter blocks it)
        # fake_list is a MagicMock; we just verify append was NOT called
        assert not drawer._list.append.called, (
            "FILTERFIX-2: a filtered-out event must not append a row to the list"
        )

    def test_on_agent_start_inserts_separator(self, drawer):
        """on_agent_start appends a separator row, breaks the counter chain, tracks state."""
        # Baseline: a prior tool_end row sets _last_row_key
        drawer.append_event({
            "agent": "Coder", "activity_type": "tool_end", "type_label": "tool", "icon": "🔧",
        })
        assert drawer._last_row_key == ("Coder", "tool_end")
        # Agent start fires
        drawer.on_agent_start("sk-1", "Coder")
        # 2 appends total: 1 from the tool_end above, 1 from the separator
        assert drawer._list.append.call_count == 2
        # State tracked
        assert drawer._last_separator_agent == ("Coder", "start")
        # Counter chain broken — next same-(agent, type) event will append, not collapse
        assert drawer._last_row_key is None
        assert drawer._last_row_widget is None

    def test_on_agent_end_inserts_summary(self, drawer):
        """on_agent_end appends a summary row, pops the per-agent counter, tracks state."""
        # Seed an _agent_counters entry by appending events with explicit counters;
        # the public append_event path doesn't populate _agent_counters (that happens
        # inside _mutate_counter_row), so set it directly for this test.
        drawer._agent_counters["Coder"] = {"count": 3, "total_duration_ms": 1247}
        drawer.on_agent_end("sk-1", "Coder")
        # 1 append (the summary separator)
        assert drawer._list.append.call_count == 1
        # Counter was popped
        assert "Coder" not in drawer._agent_counters
        # State tracked
        assert drawer._last_separator_agent == ("Coder", "end")
        # Counter chain broken
        assert drawer._last_row_key is None
        assert drawer._last_row_widget is None

    def test_mixed_types_produce_accurate_agent_end_summary(self, drawer, monkeypatch):
        """BUGFIX-3: 3 events from the same agent with DIFFERENT activity_types
        (no counter-collapse) must still produce an accurate 'N events in Xms'
        summary separator on on_agent_end — not 'ended'.
        """
        # Re-patch _build_separator_widget to capture the summary text it
        # receives, so we can assert what on_agent_end() emitted.
        captured_text = []
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._build_separator_widget",
            lambda self, text: captured_text.append(text) or MagicMock(),
        )

        # Three different activity types from "Coder" — no two share a key,
        # so no counter-collapse fires; only the new-row path runs.
        for activity_type, duration in [
            ("tool_start", 100),
            ("plan", 250),
            ("tool_end", 415),
        ]:
            drawer.append_event({
                "agent": "Coder",
                "activity_type": activity_type,
                "type_label": activity_type.split("_")[0],
                "icon": "🔧",
                "duration_ms": duration,
            })

        # After 3 new-row appends, the counter must reflect all 3 events.
        assert drawer._agent_counters["Coder"]["count"] == 3
        assert drawer._agent_counters["Coder"]["total_duration_ms"] == 765  # 100+250+415

        drawer.on_agent_end("sk-1", "Coder")

        # Without BUGFIX-3, captured_text would contain "...ended...".
        # With BUGFIX-3, it must contain the accurate count + duration.
        assert len(captured_text) == 1
        summary = captured_text[0]
        assert "Coder" in summary
        assert "3 events" in summary, f"summary {summary!r} should report 3 events"
        assert "765" in summary or "765ms" in summary, (
            f"summary {summary!r} should report 765ms total duration"
        )
        assert "ended" not in summary, (
            f"summary {summary!r} should not say 'ended' (BUGFIX-3 fix)"
        )

    def test_mixed_new_row_and_collapse_total_count(self, drawer, monkeypatch):
        """BUGFIX-3: mixed scenario — 1 new-row event + 2 counter-collapsed events
        + 1 new-row event of a different type = 4 total events reported on
        on_agent_end. Verifies the interaction between BUGFIX-3's new-row
        counter and _mutate_counter_row's setdefault (which must NOT
        double-count the first collapsed event).
        """
        captured_text = []
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._build_separator_widget",
            lambda self, text: captured_text.append(text) or MagicMock(),
        )

        # Event 1: type A, new row. BUGFIX-3 path → count=1.
        drawer.append_event({
            "agent": "Debugger",
            "activity_type": "tool_start",
            "type_label": "tool",
            "icon": "🔧",
            "duration_ms": 100,
        })
        # Event 2: type A, counter-collapse. _mutate_counter_row runs:
        #   setdefault finds existing (count=1), count += 1 → 2.
        drawer.append_event({
            "agent": "Debugger",
            "activity_type": "tool_start",
            "type_label": "tool",
            "icon": "🔧",
            "duration_ms": 200,
        })
        # Event 3: type A, counter-collapse. setdefault still finds existing
        #   (count=2), count += 1 → 3.
        drawer.append_event({
            "agent": "Debugger",
            "activity_type": "tool_start",
            "type_label": "tool",
            "icon": "🔧",
            "duration_ms": 300,
        })
        # Event 4: type B (plan), new row. Different (agent, type) key breaks
        #   the chain — new-row path. BUGFIX-3 → count += 1 → 4.
        drawer.append_event({
            "agent": "Debugger",
            "activity_type": "plan",
            "type_label": "plan",
            "icon": "📋",
            "duration_ms": 50,
        })

        # After all 4 events: count=4, duration=100+200+300+50=650.
        assert drawer._agent_counters["Debugger"]["count"] == 4
        assert drawer._agent_counters["Debugger"]["total_duration_ms"] == 650

        drawer.on_agent_end("sk-1", "Debugger")

        assert len(captured_text) == 1
        summary = captured_text[0]
        assert "4 events" in summary, (
            f"summary {summary!r} should report 4 total events (the BUGFIX-3 + "
            f"_mutate_counter_row interaction must not double-count)"
        )
        assert "ended" not in summary

    def test_toggle_flips_state(self, drawer):
        """toggle() flips self._expanded; _apply_expanded_state is patched to no-op."""
        # Initial state: _expanded = False (set in __init__)
        assert drawer._expanded is False
        drawer.toggle()
        assert drawer._expanded is True
        drawer.toggle()
        assert drawer._expanded is False

    def test_on_agent_start_is_idempotent_for_same_agent(self, drawer):
        """Sad-path: on_agent_start called twice for the same agent inserts only 1 separator.

        The double-separator guard prevents a glitchy gateway from flooding the drawer
        with redundant separator rows for the same (agent, "start") tuple.
        """
        drawer.on_agent_start("sk-1", "Coder")
        drawer.on_agent_start("sk-1", "Coder")  # second call: should be a no-op
        assert drawer._list.append.call_count == 1, \
            f"double-separator guard failed: expected 1 append, got {drawer._list.append.call_count}"
        # State unchanged after the no-op second call
        assert drawer._last_separator_agent == ("Coder", "start")


# ── Class 3: TestActivityDrawerTrim — BUGFIX-2 ─────────────────


class TestActivityDrawerTrim:
    """BUGFIX-2: _trim_old_rows_if_needed must clear _last_row_widget when
    the widget's parent row was among the trimmed rows. Otherwise the next
    counter-collapse call mutates a detached GTK widget and may crash
    PyGObject.

    The default `drawer` fixture patches _trim_old_rows_if_needed to a
    no-op, so this class uses a fresh drawer fixture that leaves trim
    intact and lets the test set up the list state directly.
    """

    @pytest.fixture
    def trim_drawer(self, monkeypatch):
        """Construct a drawer with _trim_old_rows_if_needed UN-patched (real method)."""
        # Same GTK builder patches as the default fixture
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._build_header",
            lambda self: None,
        )
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._build_list",
            lambda self: None,
        )
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._apply_expanded_state",
            lambda self: None,
        )
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._update_count_label",
            lambda self: None,
        )
        # NOTE: _trim_old_rows_if_needed is NOT patched — the test invokes
        # the real method to exercise BUGFIX-2's fix.
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._auto_scroll_to_bottom",
            lambda self: None,
        )
        monkeypatch.setattr(
            "ui.views.activity_drawer.ActivityDrawer._build_separator_widget",
            lambda self, text: MagicMock(),
        )

        # List mock: returns None by default (empty listbox). Test sets up
        # get_row_at_index side_effect per scenario.
        fake_list = MagicMock()
        fake_list.get_row_at_index = MagicMock(return_value=None)
        fake_list.append = MagicMock()
        fake_list.remove = MagicMock()

        from gi.repository import Gtk
        monkeypatch.setattr(Gtk, "Box", MagicMock)

        from ui.views.activity_drawer import ActivityDrawer
        d = ActivityDrawer()
        d._list = fake_list
        return d

    def test_trim_clears_last_row_widget_when_parent_was_removed(self, trim_drawer):
        """BUGFIX-2: when the row containing _last_row_widget is among those
        trimmed, _last_row_widget (and _last_row_key) must be cleared to
        prevent the next counter-collapse from mutating a detached widget.
        """
        drawer = trim_drawer
        # Build 26 listbox rows: first 25 are "old" event rows (exceed MAX_ROWS=100
        # trigger by setting non_sep_count > 100 in a real scenario, but for this
        # unit test we directly construct to_remove semantics via the trim loop).
        # Easier: directly construct a listbox with 26 child rows where rows
        # 1-25 are non-separator event rows and row 26 is the current
        # _last_row_widget's parent. Then call trim with MAX_ROWS=0 to force
        # removal of the first 25 rows, leaving the 26th.
        # Even easier: build 26 listbox rows, set MAX_ROWS=0 to make
        # non_sep_count > MAX_ROWS immediately for row 1, and let the trim
        # process the first 25. We need the LAST row (row 26) to be the parent
        # of _last_row_widget — but trim removes the OLDEST (lowest index), not
        # the newest, so row 26 stays. The bug fires only when _last_row_widget's
        # parent is at a low index. Adjust: set _last_row_widget's parent to
        # row 1 (one of the to-be-removed), and assert it gets cleared.
        old_max = drawer.MAX_ROWS
        drawer.MAX_ROWS = 0  # force every row to be "over the cap"
        try:
            # Build 26 listbox rows. Row 0 is the parent of _last_row_widget;
            # rows 1-25 are filler.
            rows = []
            for _ in range(26):
                lb_row = MagicMock()
                child = MagicMock()
                child.get_css_classes.return_value = []  # not a separator
                lb_row.get_child.return_value = child
                rows.append(lb_row)
            # Wire up the listbox
            drawer._list.get_row_at_index.side_effect = (
                lambda i: rows[i] if i < len(rows) else None
            )
            # Set _last_row_widget to a Box whose get_parent() returns row[0]
            # (which is among the rows that will be trimmed).
            last_widget = MagicMock()
            last_widget.get_parent.return_value = rows[0]
            drawer._last_row_widget = last_widget
            drawer._last_row_key = ("Coder", "tool_start")

            drawer._trim_old_rows_if_needed()

            # BUGFIX-2 expected: _last_row_widget and _last_row_key are cleared
            assert drawer._last_row_widget is None, (
                "_last_row_widget should be cleared when its parent row was trimmed"
            )
            assert drawer._last_row_key is None, (
                "_last_row_key should be cleared when its row was trimmed"
            )
        finally:
            drawer.MAX_ROWS = old_max

    def test_trim_preserves_last_row_widget_when_parent_not_removed(self, trim_drawer):
        """BUGFIX-2: when the row containing _last_row_widget is NOT among the
        trimmed rows, both _last_row_widget and _last_row_key must be preserved.
        (Regression guard: the fix should not over-clear.)
        """
        drawer = trim_drawer
        old_max = drawer.MAX_ROWS
        drawer.MAX_ROWS = 0  # force trim
        try:
            # Build 30 listbox rows. _last_row_widget's parent is row 29
            # (the LAST one, not among the first 25 trimmed).
            rows = []
            for _ in range(30):
                lb_row = MagicMock()
                child = MagicMock()
                child.get_css_classes.return_value = []
                lb_row.get_child.return_value = child
                rows.append(lb_row)
            drawer._list.get_row_at_index.side_effect = (
                lambda i: rows[i] if i < len(rows) else None
            )
            last_widget = MagicMock()
            last_widget.get_parent.return_value = rows[29]
            drawer._last_row_widget = last_widget
            drawer._last_row_key = ("Coder", "tool_start")

            drawer._trim_old_rows_if_needed()

            # Not trimmed → not cleared
            assert drawer._last_row_widget is last_widget, (
                "_last_row_widget should be preserved when its parent was not trimmed"
            )
            assert drawer._last_row_key == ("Coder", "tool_start")
        finally:
            drawer.MAX_ROWS = old_max

    def test_trim_clears_when_last_row_widget_has_no_parent(self, trim_drawer):
        """BUGFIX-2 edge case: if _last_row_widget.get_parent() returns None
        (already detached from before), the cleanup must not crash. The
        `parent_row in removed_set` check is False for None, so no clearing
        happens — and that's the safe no-op.
        """
        drawer = trim_drawer
        last_widget = MagicMock()
        last_widget.get_parent.return_value = None  # already detached
        drawer._last_row_widget = last_widget
        drawer._last_row_key = ("Coder", "tool_start")

        # No list setup needed — trim sees an empty listbox, removes nothing,
        # and the parent_row check is False.
        drawer._trim_old_rows_if_needed()

        # No crash, no clearing
        assert drawer._last_row_widget is last_widget
        assert drawer._last_row_key == ("Coder", "tool_start")


# ── Class 4: TestActivityHandlerLifecycleCallback ───────────────


class TestActivityHandlerLifecycleCallback:
    """ActivityHandler fires set_on_agent_lifecycle on lifecycle events.

    Callback signature: cb(session_key, agent_name, phase) where phase
    is "start" or "end". agent_name comes from payload.data.agentName,
    defaulting to "" if the gateway doesn't supply it.
    """

    def test_lifecycle_start_fires_callback(self, fake_glib):
        """stream=lifecycle phase=start → cb(sk, agent_name, "start")."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            status_target=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib,
        )
        cb = MagicMock()
        handler.set_on_agent_lifecycle(cb)

        handler.on_gateway_event("agent", {
            "stream": "lifecycle",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "start", "startedAt": 12345, "agentName": "Coder"},
        })

        cb.assert_called_once()
        args = cb.call_args[0]
        assert args == ("sk-1", "Coder", "start")

    def test_lifecycle_end_fires_callback(self, fake_glib):
        """stream=lifecycle phase=end → cb(sk, agent_name, "end")."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            status_target=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib,
        )
        cb = MagicMock()
        handler.set_on_agent_lifecycle(cb)

        handler.on_gateway_event("agent", {
            "stream": "lifecycle",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "end", "agentName": "Debugger"},
        })

        cb.assert_called_once()
        args = cb.call_args[0]
        assert args == ("sk-1", "Debugger", "end")

    def test_lifecycle_end_without_agent_name(self, fake_glib):
        """When payload has no agentName, cb is called with empty string for agent_name."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            status_target=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib,
        )
        cb = MagicMock()
        handler.set_on_agent_lifecycle(cb)

        handler.on_gateway_event("agent", {
            "stream": "lifecycle",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "end"},  # no agentName field
        })

        cb.assert_called_once()
        args = cb.call_args[0]
        # agent_name defaults to "" — drawer will show "[Agent]" for unknown agents
        assert args == ("sk-1", "", "end")

    def test_lifecycle_error_fires_end_callback(self, fake_glib):
        """stream=lifecycle phase=error → cb(sk, agent_name, "end") (error reuses end path)."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            status_target=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib,
        )
        cb = MagicMock()
        handler.set_on_agent_lifecycle(cb)

        handler.on_gateway_event("agent", {
            "stream": "lifecycle",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "error", "agentName": "Coder"},
        })

        cb.assert_called_once()
        args = cb.call_args[0]
        assert args == ("sk-1", "Coder", "end")

    def test_lifecycle_callback_not_set_does_not_crash(self, fake_glib):
        """If set_on_agent_lifecycle was never called, lifecycle events must not raise."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            status_target=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib,
        )
        # No set_on_agent_lifecycle call
        try:
            handler.on_gateway_event("agent", {
                "stream": "lifecycle",
                "sessionKey": "sk-1",
                "runId": "run-1",
                "data": {"phase": "start", "agentName": "Coder"},
            })
        except Exception as e:
            pytest.fail(f"lifecycle event crashed when callback unset: {e}")
