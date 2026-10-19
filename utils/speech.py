# utils/speech.py — talk tool: POST text, play the audio bytes.
#
# Manifest:
#   Reads:  speech.yaml via speech_config (token stays out of logs and results)
#   Writes: a mode-0o600 temp .mp3/.raw, deleted after playback
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
_DEFAULT_PCM_RATE = 24000
_SUFFIXES = {"mp3": ".mp3", "pcm": ".raw"}


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
        fetched = _fetch_audio(cfg, text)
        if isinstance(fetched, str):
            return ToolResult(success=False, error=fetched)
        audio, rate = fetched
        fmt = cfg.get("format") or "mp3"
        path = _write_temp(audio, fmt)
        played = _play(path, fmt, rate)
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


def _parse_rate(content_type: str) -> int:
    """Sample rate from ``audio/pcm;rate=NNNN``. Default 24000 when absent."""
    for param in content_type.split(";")[1:]:
        key, _, value = param.partition("=")
        if key.strip().lower() != "rate":
            continue
        try:
            rate = int(value.strip().strip('"'))
        except ValueError:
            break
        if rate > 0:
            return rate
        break
    return _DEFAULT_PCM_RATE


def _fetch_audio(cfg: dict, text: str) -> tuple[bytes, int] | str:
    """Return (audio bytes, pcm rate), or an error string. Never raises."""
    payload = {
        "model": cfg["model"],
        "input": text,
        "response_format": cfg.get("format") or "mp3",
    }
    voice = (cfg.get("voice") or "").strip()
    if voice:
        payload["voice"] = voice
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        cfg["endpoint"],
        data=body,
        headers={
            "Authorization": f"Bearer {cfg['token']}",
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
    rate = _DEFAULT_PCM_RATE
    if payload["response_format"] == "pcm":
        rate = _parse_rate(content_type)
    return data, rate


def _write_temp(audio: bytes, fmt: str = "mp3") -> str:
    """Write audio to an owner-only temp file; suffix follows the format."""
    suffix = _SUFFIXES.get(fmt, _SUFFIXES["mp3"])
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(audio)
        handle.flush()
        path = handle.name
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _play(path: str, fmt: str = "mp3", rate: int = _DEFAULT_PCM_RATE) -> str | None:
    """Play path. None on success, an error string otherwise."""
    if fmt == "pcm":
        return _play_pcm(path, rate)
    player = next((name for name in _PLAYERS if shutil.which(name)), None)
    if player is None:
        return "no audio player (pw-play, paplay, or ffplay)"
    if player == "ffplay":
        cmd = [player, "-nodisp", "-autoexit", "-loglevel", "quiet", path]
    else:
        cmd = [player, path]
    return _run_player(cmd)


def _play_pcm(path: str, rate: int) -> str | None:
    """Raw 16-bit mono PCM: ffplay if present, else pw-play with explicit flags."""
    ffplay = shutil.which("ffplay")
    if ffplay is not None:
        cmd = [
            ffplay, "-f", "s16le", "-ar", str(rate), "-ch_layout", "mono",
            "-nodisp", "-autoexit", "-loglevel", "quiet", path,
        ]
    else:
        pw_play = shutil.which("pw-play")
        if pw_play is None:
            return "no audio player for pcm (ffplay or pw-play)"
        cmd = [pw_play, "--format=s16", f"--rate={rate}", "--channels=1", path]
    return _run_player(cmd)


def _run_player(cmd: list[str]) -> str | None:
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
