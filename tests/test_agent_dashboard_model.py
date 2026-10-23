# tests/test_agent_dashboard_model.py — SPEC-AGENT-DASHBOARD §2.3
import math

from ui.agent_dashboard_model import collapsed_summary, local_member_keys


def test_empty_rows_summary():
    assert collapsed_summary([]) == "No agents"


def test_single_row_summary():
    assert collapsed_summary([{"name": "Coder", "usage_percent": 42.0}]) == "Coder 42%"


def test_multiple_rows_summary():
    rows = [
        {"name": "Coder", "usage_percent": 40.0},
        {"name": "Debugger", "usage_percent": 70.0},
    ]
    assert collapsed_summary(rows) == "Coder 40% · Debugger 70%"


def test_bool_percent_renders_em_dash():
    assert collapsed_summary([{"name": "Coder", "usage_percent": True}]) == "Coder —"
    assert collapsed_summary([{"name": "Coder", "usage_percent": False}]) == "Coder —"


def test_nan_percent_renders_em_dash():
    assert collapsed_summary([{"name": "Coder", "usage_percent": math.nan}]) == "Coder —"


def test_none_percent_renders_em_dash():
    assert collapsed_summary([{"name": "Coder", "usage_percent": None}]) == "Coder —"


def test_negative_percent_renders_em_dash():
    assert collapsed_summary([{"name": "Coder", "usage_percent": -1.0}]) == "Coder —"


def test_missing_name_defaults_to_agent():
    assert collapsed_summary([{"usage_percent": 10.0}]) == "Agent 10%"


def test_local_member_keys_filters_to_registered():
    members = ["agent:coder", "guest", "agent:debugger"]
    registered = {"agent:coder": "Coder", "agent:debugger": "Debugger"}
    assert local_member_keys(members, registered) == ["agent:coder", "agent:debugger"]