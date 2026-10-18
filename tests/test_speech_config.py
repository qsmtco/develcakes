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
        "voice": "",
        "format": "mp3",
    }


def test_voice_absent_normalizes_to_empty(tmp_path, monkeypatch):
    target = _path(tmp_path, monkeypatch)
    target.write_text(
        "endpoint: https://speech.example/v1\n"
        "model: voice-1\n"
        "token: sekret\n"
        "voice: '  '\n"
        "format: pcm\n",
        encoding="utf-8",
    )
    cfg, error = load_speech_config()
    assert error is None
    assert cfg["voice"] == ""
    assert cfg["format"] == "pcm"


def test_voice_present_is_stripped(tmp_path, monkeypatch):
    target = _path(tmp_path, monkeypatch)
    target.write_text(
        "endpoint: https://speech.example/v1\n"
        "model: voice-1\n"
        "token: sekret\n"
        "voice: ' alto '\n",
        encoding="utf-8",
    )
    cfg, error = load_speech_config()
    assert error is None
    assert cfg["voice"] == "alto"
    assert cfg["format"] == "mp3"


def test_non_string_voice_is_a_store_error(tmp_path, monkeypatch):
    target = _path(tmp_path, monkeypatch)
    target.write_text(
        "endpoint: https://speech.example/v1\n"
        "model: voice-1\n"
        "token: sekret\n"
        "voice: 7\n",
        encoding="utf-8",
    )
    cfg, error = load_speech_config()
    assert cfg is None
    assert "voice" in error


def test_invalid_format_is_a_store_error(tmp_path, monkeypatch):
    target = _path(tmp_path, monkeypatch)
    target.write_text(
        "endpoint: https://speech.example/v1\n"
        "model: voice-1\n"
        "token: sekret\n"
        "format: wav\n",
        encoding="utf-8",
    )
    cfg, error = load_speech_config()
    assert cfg is None
    assert "format" in error


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
