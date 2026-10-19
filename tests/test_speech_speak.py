"""speak fails closed without settings and does not treat JSON as audio."""

import json
import os
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
        lambda: ({"endpoint": "https://speech.example", "model": "m", "token": "sekret"}, None),
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
    assert "sekret" not in result.output
    assert "speech.example" not in result.output


def test_voice_and_format_reach_the_request(monkeypatch):
    monkeypatch.setattr(
        "utils.speech_config.load_speech_config",
        lambda: (
            {
                "endpoint": "https://speech.example",
                "model": "m",
                "token": "t",
                "voice": "algenib",
                "format": "pcm",
            },
            None,
        ),
    )

    captured = {}

    class _Resp:
        def __init__(self):
            self.headers = {"Content-Type": "audio/pcm;rate=24000"}

        def read(self):
            return b"\x00\x01audio"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _fake_urlopen(request, *_a, **_k):
        captured["payload"] = json.loads(request.data)
        return _Resp()

    monkeypatch.setattr(speech.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(speech.shutil, "which", lambda name: "/usr/bin/pw-play" if name == "pw-play" else None)
    monkeypatch.setattr(
        speech.subprocess, "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0),
    )
    result = speech.speak("hello")
    assert result.success is True
    payload = captured["payload"]
    assert payload["voice"] == "algenib"
    assert payload["response_format"] == "pcm"


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


def _stub_config(monkeypatch, fmt):
    monkeypatch.setattr(
        "utils.speech_config.load_speech_config",
        lambda: (
            {
                "endpoint": "https://speech.example",
                "model": "m",
                "token": "t",
                "format": fmt,
            },
            None,
        ),
    )


def _stub_response(monkeypatch, content_type):
    class _Resp:
        def __init__(self):
            self.headers = {"Content-Type": content_type}

        def read(self):
            return b"\x00\x01audio"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(
        speech.urllib.request, "urlopen", lambda *_a, **_k: _Resp(),
    )


def _record_player(monkeypatch, available, calls):
    monkeypatch.setattr(
        speech.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name in available else None,
    )

    def _run(cmd, *_a, **_k):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(speech.subprocess, "run", _run)


def test_temp_suffix_follows_format(tmp_path):
    pcm_path = speech._write_temp(b"\x00\x01", "pcm")
    mp3_path = speech._write_temp(b"\x00\x01", "mp3")
    try:
        assert pcm_path.endswith(".raw")
        assert mp3_path.endswith(".mp3")
        assert (os.stat(pcm_path).st_mode & 0o777) == 0o600
    finally:
        os.unlink(pcm_path)
        os.unlink(mp3_path)


def test_pcm_plays_via_ffplay_with_rate_from_content_type(monkeypatch):
    _stub_config(monkeypatch, "pcm")
    _stub_response(monkeypatch, "audio/pcm;rate=16000")
    calls = []
    _record_player(monkeypatch, {"ffplay"}, calls)

    result = speech.speak("hello")

    assert result.success is True
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "/usr/bin/ffplay"
    assert cmd[1:5] == ["-f", "s16le", "-ar", "16000"]
    assert cmd[5:7] == ["-ch_layout", "mono"]
    assert cmd[7:] == [
        "-nodisp", "-autoexit", "-loglevel", "quiet", cmd[-1],
    ]
    assert cmd[-1].endswith(".raw")


def test_pcm_rate_defaults_to_24000_without_param(monkeypatch):
    _stub_config(monkeypatch, "pcm")
    _stub_response(monkeypatch, "audio/pcm")
    calls = []
    _record_player(monkeypatch, {"ffplay"}, calls)

    result = speech.speak("hello")

    assert result.success is True
    assert calls[0][3:5] == ["-ar", "24000"]
    assert speech._parse_rate("audio/pcm;rate=24000") == 24000
    assert speech._parse_rate("audio/pcm;rate=abc") == 24000


def test_pcm_falls_back_to_pw_play(monkeypatch):
    _stub_config(monkeypatch, "pcm")
    _stub_response(monkeypatch, "audio/pcm;rate=24000")
    calls = []
    _record_player(monkeypatch, {"pw-play"}, calls)

    result = speech.speak("hello")

    assert result.success is True
    assert calls[0][:4] == [
        "/usr/bin/pw-play", "--format=s16", "--rate=24000", "--channels=1",
    ]
    assert calls[0][4].endswith(".raw")


def test_pcm_without_any_player_reports_error(monkeypatch):
    _stub_config(monkeypatch, "pcm")
    _stub_response(monkeypatch, "audio/pcm;rate=24000")
    calls = []
    _record_player(monkeypatch, set(), calls)

    result = speech.speak("hello")

    assert result.success is False
    assert result.error == "no audio player for pcm (ffplay or pw-play)"
    assert calls == []


def test_mp3_playback_command_is_unchanged(monkeypatch):
    _stub_config(monkeypatch, "mp3")
    _stub_response(monkeypatch, "audio/mpeg")
    calls = []
    _record_player(monkeypatch, {"pw-play"}, calls)

    result = speech.speak("hello")

    assert result.success is True
    assert len(calls) == 1
    assert calls[0][0] == "pw-play"
    assert calls[0][1].endswith(".mp3")


def test_mp3_ffplay_flags_unchanged(monkeypatch):
    _stub_config(monkeypatch, "mp3")
    _stub_response(monkeypatch, "audio/mpeg")
    calls = []
    _record_player(monkeypatch, {"ffplay"}, calls)

    result = speech.speak("hello")

    assert result.success is True
    assert calls[0][1:5] == ["-nodisp", "-autoexit", "-loglevel", "quiet"]
    assert calls[0][-1].endswith(".mp3")


def test_http_error_keeps_message_and_hides_token(monkeypatch):
    _stub_config(monkeypatch, "pcm")
    monkeypatch.setattr(
        speech.urllib.request, "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(
            urllib.error.HTTPError(
                "https://speech.example", 400, "Bad Request", {}, None,
            )
        ),
    )
    result = speech.speak("hello")
    assert result.success is False
    assert result.error == "speech request failed"
    assert "t" != result.error


def test_player_exit_code_is_a_playback_error(monkeypatch):
    _stub_config(monkeypatch, "mp3")
    _stub_response(monkeypatch, "audio/mpeg")
    monkeypatch.setattr(speech.shutil, "which", lambda name: "/usr/bin/pw-play")
    monkeypatch.setattr(
        speech.subprocess, "run",
        lambda cmd, *_a, **_k: subprocess.CompletedProcess(cmd, 1),
    )
    result = speech.speak("hello")
    assert result.success is False
    assert result.error == "could not play audio"
