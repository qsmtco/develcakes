"""jev_ask formats post_decision and does not call it for a bad question."""

from agent.tools import execute_tool


def test_none_is_no_decision(monkeypatch):
    monkeypatch.setattr(
        "agent.llm.jev_client.post_decision",
        lambda state, questions: (None, "down"),
    )
    result = execute_tool(
        "jev_ask",
        {"state": "clear or compact", "options": [
            {"name": "clear", "meaning": "wipe"},
            {"name": "compact", "meaning": "summarize"},
        ]},
        "/tmp",
        session_key="special:coder",
    )
    assert result.success is True
    assert result.output == "no decision"


def test_low_confidence_is_not_decisive(monkeypatch):
    def _post(state, questions):
        return ({
            "decision": {
                "type": "choice",
                "choice": "compact",
                "confidence": 0.5,
                "probabilities": {"compact": 0.5, "clear": 0.5},
            }
        }, "")

    monkeypatch.setattr("agent.llm.jev_client.post_decision", _post)
    result = execute_tool(
        "jev_ask",
        {"state": "fork", "options": [
            {"name": "clear", "meaning": "wipe"},
            {"name": "compact", "meaning": "summarize"},
        ]},
        "/tmp",
        session_key="special:coder",
    )
    assert result.success is True
    assert "decisive: false" in result.output
    assert "choice: compact" in result.output
    assert "confidence: 0.50" in result.output


def test_high_confidence_is_decisive(monkeypatch):
    def _post(state, questions):
        return ({
            "decision": {
                "type": "choice",
                "choice": "clear",
                "confidence": 0.9,
                "probabilities": {"clear": 0.9, "compact": 0.1},
            }
        }, "")

    monkeypatch.setattr("agent.llm.jev_client.post_decision", _post)
    result = execute_tool(
        "jev_ask",
        {"state": "fork", "options": [
            {"name": "clear", "meaning": "wipe"},
            {"name": "compact", "meaning": "summarize"},
        ]},
        "/tmp",
        session_key="special:supervisor",
    )
    assert result.success is True
    assert "decisive: true" in result.output
    assert "probabilities: clear=0.90, compact=0.10" in result.output


def test_one_option_does_not_call(monkeypatch):
    def _post(state, questions):
        raise AssertionError("post_decision must not run")

    monkeypatch.setattr("agent.llm.jev_client.post_decision", _post)
    result = execute_tool(
        "jev_ask",
        {"state": "fork", "options": [{"name": "clear", "meaning": "wipe"}]},
        "/tmp",
        session_key="special:coder",
    )
    assert result.success is False
    assert "2 to 5" in result.error
