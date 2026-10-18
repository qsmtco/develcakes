"""speak fails closed without settings and does not treat JSON as audio."""

import subprocess
import urllib.request

from utils import speech


def test_unconfigured_does_not_open_a_socket(monkeypatch):
    monkeypatch.setattr(
        "utils.speech_config.load_speech_config", lambda: (None, None),
    )

    def _boom(*_a, **_k):
        raise AssertionError("socket opened")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    result = speech.speak("hello")
    assert result.success is False
    assert result.error == "speech is not configured"


def test_audio_response_reports_character_count(monkeypatch):
    monkeypatch.setattr(
        "utils.speech_config.load_speech_config",
        lambda: ({"endpoint": "https://speech.example", "model": "m", "token": "t"}, None),
    )

    class _Resp:
        def __init__(self):
            self.headers = {"Content-Type": "audio/mpeg"}

        def read(self):
            return b"\x00\x01audio"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(speech.urllib.request, "urlopen", lambda *_a, **_k: _Resp())
    monkeypatch.setattr(speech.shutil, "which", lambda name: "/usr/bin/pw-play" if name == "pw-play" else None)
    monkeypatch.setattr(
        speech.subprocess, "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0),
    )
    result = speech.speak("hello")
    assert result.success is True
    assert result.output == "spoke 5 characters"
    assert "t" not in result.output
    assert "speech.example" not in result.output


def test_json_body_is_not_audio(monkeypatch):
    monkeypatch.setattr(
        "utils.speech_config.load_speech_config",
        lambda: ({"endpoint": "https://speech.example", "model": "m", "token": "t"}, None),
    )

    class _Resp:
        def __init__(self):
            self.headers = {"Content-Type": "application/json"}

        def read(self):
            return b'{"error":"no"}'

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(speech.urllib.request, "urlopen", lambda *_a, **_k: _Resp())
    result = speech.speak("hello")
    assert result.success is False
    assert result.error == "speech request failed"
    assert "no" not in (result.error or "")
