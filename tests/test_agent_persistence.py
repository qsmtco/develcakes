"""Tests for agent/persistence.py — one JSON file per session."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from agent.persistence import (
    conversations_dir,
    load_conversation_from_disk,
    resolve_session_workspace,
    save_conversation_to_disk,
)
from models.conversation import Conversation, Message, MessageRole, ToolCall


def _conv() -> Conversation:
    return Conversation(
        agent_name="Coder",
        model="openai/gpt-4o",
        provider="openai",
        system_prompt="You are Coder",
        messages=[
            Message(role=MessageRole.USER, content="hello"),
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[ToolCall(call_id="c1", tool_name="read_file", arguments={"path": "a.py"})],
            ),
            Message(role=MessageRole.TOOL_RESULT, content="file body", tool_call_id="c1"),
        ],
        api_key="sk-secret-12345",
    )


class TestConversationsDir:
    def test_creates_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        d = conversations_dir()
        assert os.path.isdir(d)


class TestSaveLoadRoundtrip:
    def test_save_and_load_preserves_messages(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        conv = _conv()
        path = save_conversation_to_disk(conv, "special:coder")
        assert os.path.isfile(path)
        assert os.stat(path).st_mode & 0o777 == 0o600
        result = load_conversation_from_disk("special:coder")
        assert result is not None
        loaded, _data = result
        assert loaded.agent_name == "Coder"
        assert loaded.project_path is None
        assert loaded.system_prompt == ""
        assert [m.role for m in loaded.messages] == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
            MessageRole.TOOL_RESULT,
        ]
        assert loaded.messages[0].content == "hello"
        assert loaded.messages[1].tool_calls[0].call_id == "c1"
        assert loaded.messages[1].tool_calls[0].arguments == {"path": "a.py"}
        assert loaded.messages[2].tool_call_id == "c1"
        assert loaded.messages[2].content == "file body"

    def test_saved_file_does_not_contain_api_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        path = save_conversation_to_disk(_conv(), "special:coder")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert "api_key" not in data

    def test_load_ignores_api_key_in_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        path = save_conversation_to_disk(_conv(), "special:coder")
        data = json.loads(open(path, encoding="utf-8").read())
        data["api_key"] = "sk-SECRET"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        provider = MagicMock()
        provider.name = "openai"
        provider.api_key = "sk-REAL"
        with patch("utils.providers_store.load_providers", return_value=[provider]):
            loaded, raw = load_conversation_from_disk("special:coder")
        assert loaded is not None
        assert loaded.api_key == "sk-REAL"
        assert raw["api_key"] == "sk-SECRET"

    def test_failed_write_keeps_previous_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        path = save_conversation_to_disk(_conv(), "special:coder")
        before = open(path, "rb").read()

        def boom(*_a, **_k):
            raise TypeError("dump failed")

        monkeypatch.setattr("agent.persistence.json.dump", boom)
        with pytest.raises(TypeError, match="dump failed"):
            save_conversation_to_disk(_conv(), "special:coder")
        assert open(path, "rb").read() == before
        assert not os.path.exists(path + ".tmp")

    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        assert load_conversation_from_disk("special:coder") is None

    def test_corrupt_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        d = conversations_dir()
        with open(os.path.join(d, "special:coder.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        assert load_conversation_from_disk("special:coder") is None


class TestResolveSessionWorkspace:
    def test_valid_session_key(self, tmp_path):
        ws = resolve_session_workspace(str(tmp_path), "special:coder")
        assert os.path.isdir(ws)

    def test_empty_project_path_raises(self):
        with pytest.raises(ValueError, match="LOW-2"):
            resolve_session_workspace("", "special:coder")

    def test_path_escape_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="LOW-2"):
            resolve_session_workspace(str(tmp_path), "../escape")

    def test_colon_sanitized(self, tmp_path):
        ws = resolve_session_workspace(str(tmp_path), "special:coder")
        assert "special-coder" in ws
