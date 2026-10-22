# tests/test_agent_list_handler.py
"""Tests for AgentListHandler."""

import pytest
from unittest.mock import MagicMock

from ui.handlers.agent_list_handler import AgentListHandler
from models.agents import AgentManager


@pytest.fixture
def agent_mgr():
    m = AgentManager()
    m.register("agent:qat:main", "Qat")
    m.register("agent:qat:telegram:direct:7478874934", "Qat")
    m.register("agent:fl600:main", "FL600")
    return m


@pytest.fixture
def handler(agent_mgr):
    chat_mock = MagicMock()
    toggle_mock = MagicMock()
    return AgentListHandler(
        agent_mgr=agent_mgr,
        on_agent_chat=chat_mock,
        on_agent_toggle=toggle_mock,
    ), chat_mock, toggle_mock


class TestComputeInitials:
    def test_two_words(self):
        h = AgentListHandler()
        assert h.compute_initials("Qrusher Qat") == "QQ"

    def test_single_word(self):
        h = AgentListHandler()
        assert h.compute_initials("Qat") == "QA"

    def test_three_words(self):
        h = AgentListHandler()
        assert h.compute_initials("Qrusher The Android") == "QT"

    def test_empty_string(self):
        h = AgentListHandler()
        assert h.compute_initials("") == ""


class TestGetAgentColor:
    def test_returns_assigned_color(self, agent_mgr):
        h = AgentListHandler(agent_mgr=agent_mgr)
        color = h.get_agent_color("Qat")
        assert color is not None
        assert color.startswith("#")

    def test_fallback_when_no_agent_mgr(self):
        """Without agent_mgr and without special-agent match, returns deterministic default."""
        h = AgentListHandler()
        color = h.get_agent_color("DefinitelyNotARealAgent")
        assert color.startswith("#")
        # Without a matching special agent, the handler returns the deterministic
        # default — same as the old fallback, but stable across calls.
        assert color == h.get_agent_color("DefinitelyNotARealAgent")


class TestGetSortedAgents:
    def test_groups_by_name(self, agent_mgr):
        h = AgentListHandler(agent_mgr=agent_mgr)
        sorted_agents = h.get_sorted_agents()
        names = [name for _, name, _, _ in sorted_agents]
        assert set(names) == {"Qat", "FL600"}

    def test_prefers_main_session(self, agent_mgr):
        h = AgentListHandler(agent_mgr=agent_mgr)
        sorted_agents = h.get_sorted_agents()
        qat_entry = next((sk for sk, n, _, _ in sorted_agents if n == "Qat"), None)
        assert qat_entry == "agent:qat:main"

    def test_project_members_tracked(self, agent_mgr):
        h = AgentListHandler(agent_mgr=agent_mgr)
        sorted_agents = h.get_sorted_agents(project_members=["agent:qat:main"])
        qat_entry = next((sk, n, ip, _) for sk, n, ip, _ in sorted_agents if n == "Qat")
        assert qat_entry[2] is True  # in_project

    def test_no_agent_mgr_returns_empty(self):
        h = AgentListHandler()
        assert h.get_sorted_agents() == []


class TestCallbacks:
    def test_chat_callback(self, handler):
        h, chat_mock, _ = handler
        h.on_chat_clicked("agent:qat:main", "Qat")
        chat_mock.assert_called_once_with("agent:qat:main", "Qat")

    def test_toggle_callback(self, handler):
        h, _, toggle_mock = handler
        h.on_toggle_clicked("agent:qat:main", "Qat", True)
        toggle_mock.assert_called_once_with("agent:qat:main", "Qat", True)

    def test_chat_callback_not_set(self):
        h = AgentListHandler()
        h.on_chat_clicked("agent:qat:main", "Qat")  # no-op, no error

    def test_set_agent_mgr(self, agent_mgr):
        h = AgentListHandler()
        assert h.get_sorted_agents() == []
        h.set_agent_mgr(agent_mgr)
        names = [n for _, n, _, _ in h.get_sorted_agents()]
        assert "Qat" in names


class TestColorStability:
    def test_get_agent_color_stable_across_calls(self):
        """Same name → same color across repeated calls (no agent_mgr, special-agent path)."""
        from unittest.mock import patch
        from agent.special_agents import SpecialAgentDef
        h = AgentListHandler()  # no agent_mgr
        mock_def = SpecialAgentDef(
            conv_id_prefix="agent:mocktest",
            display_name="MockTest",
            role="mocktest",
            emoji="🧪",
            tools=["read_file"],
            can_write=False,
        )
        with patch("agent.special_agents.get_special_agents", return_value=[mock_def]):
            colors = [h.get_agent_color("MockTest") for _ in range(5)]
            assert len(set(colors)) == 1, f"Colors drifted: {colors}"

    def test_get_agent_color_uses_deterministic_default_for_unknown(self):
        """Unknown name returns '#6366f1' without advancing any counter."""
        from unittest.mock import patch
        h = AgentListHandler()
        with patch("agent.special_agents.get_special_agents", return_value=[]):
            c1 = h.get_agent_color("Unknown")
            c2 = h.get_agent_color("Unknown")
            assert c1 == "#6366f1"
            assert c1 == c2

    def test_get_agent_color_falls_through_when_agent_mgr_returns_falsy(self):
        """agent_mgr is set but get_color(name) returns falsy → falls through to special-agent path.

        This covers the live-agent path returning None or empty string, which should
        NOT prevent the special-agent lookup from running. Without this test, a
        regression that returns a falsy-but-truthy value from agent_mgr.get_color()
        (e.g. False, "", 0) would silently skip the special-agent lookup.
        """
        from unittest.mock import patch, MagicMock
        from agent.special_agents import SpecialAgentDef
        # Construct an agent_mgr-like object whose get_color() returns falsy.
        mgr = MagicMock()
        mgr.get_color.return_value = None
        h = AgentListHandler(agent_mgr=mgr)
        mock_def = SpecialAgentDef(
            conv_id_prefix="agent:fallthrough",
            display_name="FallThrough",
            role="fallthrough_role",
            emoji="🧪",
            tools=["read_file"],
            can_write=False,
        )
        with patch("agent.special_agents.get_special_agents", return_value=[mock_def]):
            c1 = h.get_agent_color("FallThrough")
            c2 = h.get_agent_color("FallThrough")
            assert c1.startswith("#")
            assert c1 == c2  # stable across calls — falls through to special-agent cache
            # Falsy cases that must also fall through
            for falsy_value in (None, "", 0, False):
                mgr.get_color.return_value = falsy_value
                c = h.get_agent_color("FallThrough")
                assert c == c1, f"get_color({falsy_value!r}) should fall through, got {c!r}"
