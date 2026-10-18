# utils/speech.py — talk tool: POST text, play the audio bytes.
#
# Manifest:
#   Reads:  speech.yaml via speech_config (token stays out of logs and results)
#   Writes: a mode-0o600 temp mp3, deleted after playback
#   Network: POST to the user-configured speech endpoint
#   External: pw-play, paplay, or ffplay

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request

_logger = logging.getLogger(__name__)

_PLAY_LOCK = threading.Lock()
_PLAYERS = ("pw-play", "paplay", "ffplay")
_HTTP_TIMEOUT_S = 30
_PLAY_TIMEOUT_S = 60
_LOCK_TIMEOUT_S = 60


def speak(text: str):
    """Synthesize ``text`` and play it. One speaker at a time.

    Returns a ToolResult. Never includes the token, the endpoint, or audio
    bytes. ``speech is not configured`` when speech.yaml is absent.
    """
    from agent.tools import ToolResult

    if not isinstance(text, str) or not text.strip():
        return ToolResult(success=False, error="talk text is empty")
    text = text.strip()
    if len(text) > 1000:
        return ToolResult(success=False, error="talk text exceeds 1000 characters")

    from utils.speech_config import load_speech_config
    cfg, error = load_speech_config()
    if error is not None:
        return ToolResult(success=False, error=f"speech settings unusable: {error}")
    if cfg is None:
        return ToolResult(success=False, error="speech is not configured")

    if not _PLAY_LOCK.acquire(timeout=_LOCK_TIMEOUT_S):
        return ToolResult(success=False, error="another talk is still playing")
    path = ""
    try:
        audio = _fetch_audio(cfg["endpoint"], cfg["model"], cfg["token"], text)
        if isinstance(audio, str):
            return ToolResult(success=False, error=audio)
        path = _write_temp(audio)
        played = _play(path)
        if played is not None:
            return ToolResult(success=False, error=played)
        return ToolResult(success=True, output=f"spoke {len(text)} characters")
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                _logger.warning("speech: could not delete temp audio")
        _PLAY_LOCK.release()


def _fetch_audio(endpoint: str, model: str, token: str, text: str) -> bytes | str:
    """Return audio bytes, or an error string. Never raises."""
    body = json.dumps({
        "model": model,
        "input": text,
        "response_format": "mp3",
    }).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read()
    except urllib.error.HTTPError as exc:
        _logger.warning("speech: HTTP %s", exc.code)
        return "speech request failed"
    except Exception as exc:  # noqa: BLE001 — URLError, timeout, bad URL
        _logger.warning("speech: request failed: %s", type(exc).__name__)
        return "speech request failed"
    if not isinstance(content_type, str) or not content_type.lower().startswith("audio/"):
        _logger.warning("speech: content-type %r is not audio", content_type)
        return "speech request failed"
    if not data:
        return "speech request failed"
    return data


def _write_temp(audio: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
        handle.write(audio)
        handle.flush()
        path = handle.name
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _play(path: str) -> str | None:
    """Play path. None on success, an error string otherwise."""
    player = next((name for name in _PLAYERS if shutil.which(name)), None)
    if player is None:
        return "no audio player (pw-play, paplay, or ffplay)"
    if player == "ffplay":
        cmd = [player, "-nodisp", "-autoexit", "-loglevel", "quiet", path]
    else:
        cmd = [player, path]
    try:
        result = subprocess.run(
            cmd, timeout=_PLAY_TIMEOUT_S, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        _logger.warning("speech: player timed out")
        return "could not play audio"
    except OSError as exc:
        _logger.warning("speech: player failed: %s", type(exc).__name__)
        return "could not play audio"
    if result.returncode != 0:
        _logger.warning("speech: player exit %s", result.returncode)
        return "could not play audio"
    return None
