"""speech.yaml round-trip. A blank token keeps the stored credential."""

import os
import stat

import pytest

from utils.speech_config import load_speech_config, save_speech_config


def _path(tmp_path, monkeypatch):
    target = tmp_path / "speech.yaml"
    monkeypatch.setattr("utils.speech_config.get_speech_path", lambda: str(target))
    return target


def test_round_trip(tmp_path, monkeypatch):
    _path(tmp_path, monkeypatch)
    save_speech_config("https://speech.example/v1", "voice-1", "sekret")
    cfg, error = load_speech_config()
    assert error is None
    assert cfg == {
        "endpoint": "https://speech.example/v1",
        "model": "voice-1",
        "token": "sekret",
    }


def test_blank_token_on_first_save_raises(tmp_path, monkeypatch):
    target = _path(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="token is required"):
        save_speech_config("https://speech.example/v1", "voice-1", "  ")
    assert not target.exists()


def test_blank_token_keeps_stored_token(tmp_path, monkeypatch):
    _path(tmp_path, monkeypatch)
    save_speech_config("https://speech.example/v1", "voice-1", "sekret")
    save_speech_config("https://other.example/v1", "voice-2", "")
    cfg, error = load_speech_config()
    assert error is None
    assert cfg["endpoint"] == "https://other.example/v1"
    assert cfg["model"] == "voice-2"
    assert cfg["token"] == "sekret"


def test_file_mode_is_owner_only(tmp_path, monkeypatch):
    target = _path(tmp_path, monkeypatch)
    save_speech_config("https://speech.example/v1", "voice-1", "sekret")
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600
