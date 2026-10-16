# utils/stt.py
# Speech-to-text via OpenRouter cloud STT — push-to-talk with stop_async pattern.
#
#   arecord (ALSA/PipeWire, 16kHz mono WAV) → base64 → POST OpenRouter
#   /api/v1/audio/transcriptions (model from stt_config, SPEC-37) → callback.
#
# Local transcription (faster-whisper) is GONE. Capture stays local; audio leaves
# the machine for transcription (see SPEC-36 for the privacy/threat-model note).
#
# Security Manifest:
#   Reads:    ALSA device ("default" via PipeWire ALSA plugin)
#   Writes:   none
#   Network:  HTTPS POST to openrouter.ai (audio + API key leave the machine)
#   External: OpenRouter API
#
# Environment:
#   OPENROUTER_API_KEY — bearer token; falls back to get_api_key("openrouter").
#   STT_MODEL          — non-blank value overrides stt.yaml for THIS process
#                        (debug/testing); see utils/stt_config.py (SPEC-37).
#   STT_PHRASES        — comma-separated keyword-bias list. Overrides the default;
#                        empty string → send NO phraseList at all. The phraseList
#                        is also gated per model: only bias-supporting models
#                        (is_bias_supported) ever receive it.

import base64
import io
import logging
import os
import subprocess
import threading
import wave

_logger = logging.getLogger(__name__)

# Capture: 16kHz mono 16-bit raw PCM (arecord -t raw, headerless — BUG #1);
# _transcribe_frames wraps it in the WAV container the endpoint expects.
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit = 2 bytes

# OpenRouter cloud STT. The model is NOT a module constant (SPEC-37): it is
# resolved per transcription from stt_config (stt.yaml, env STT_MODEL override)
# so a Settings change applies on the NEXT dictation without an app restart.
_STT_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
_TIMEOUT_SECONDS = 20
_BODY_PREVIEW_CHARS = 500

# Default keyword-bias phrases (SPEC-36 §3 — overridable via STT_PHRASES)
_DEFAULT_PHRASES = ["DevelCakes", "Crabcakes", "crabcakes"]


def _resolve_api_key() -> str | None:
    """Resolve the OpenRouter API key: env first, then agent config. None if absent."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    try:
        from agent.config import get_api_key
    except ImportError:  # agent package unavailable (e.g. isolated use)
        return None
    return get_api_key("openrouter")


def _resolve_phrases() -> list[str]:
    """Resolve keyword-bias phrases from STT_PHRASES (BUG #7, SPEC-36 round 2).

    Unset → default list. Any all-blank value — empty string, whitespace only,
    commas only (",,", " , ") — yields no phrases, which disables biasing
    (no phraseList is sent). Blank entries inside a non-blank list are dropped.
    """
    raw = os.environ.get("STT_PHRASES")
    if raw is None:
        return list(_DEFAULT_PHRASES)
    return [p.strip() for p in raw.split(",") if p.strip()]


class STTEngine:
    """
    Push-to-talk STT using OpenRouter's cloud transcription endpoint.

    Lifecycle:
        stt = STTEngine(on_result=callback)
        stt.start()         → begin capturing audio (WARNING + stays idle if no key)
        stt.stop_async()    → stop capture, transcribe in background, callback(text)
        stt.stop()          → stop capture, transcribe, return text (blocking)

    Audio source: PipeWire ALSA plugin ("default" device).
    Capture is local; transcription is a cloud POST to OpenRouter.
    """

    def __init__(self, device="default", on_result=None):
        """device: ALSA capture device (arecord -D <device>; "default" = PipeWire
        ALSA plugin, ~5ms open vs ~230ms direct HW). on_result: Callback(text: str)
        fired with the transcript on stop_async."""
        self._device = device
        self._on_result = on_result
        self._recording = False
        self._capture_thread = None
        self._capture_proc = None
        self._frames = []  # raw WAV bytes captured during this session
        self._api_key = _resolve_api_key()
        self._phrases = _resolve_phrases()

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self):
        """Begin capturing audio from the microphone into memory. Refuses to record
        (state stays idle) and logs a WARNING when no OpenRouter API key is available."""
        if self._recording:
            return
        self._api_key = _resolve_api_key()
        if not self._api_key:
            _logger.warning(
                "STT: no OpenRouter API key — set OPENROUTER_API_KEY or configure the "
                "'openrouter' provider; refusing to record"
            )
            return
        self._recording = True
        self._frames.clear()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()

    def stop_async(self, on_done=None):
        """Stop capture and transcribe in a background thread. Calls on_done(text) on
        completion; GLib.idle_add dispatches to the GTK main thread when available.
        on_done overrides the ctor on_result."""
        if not self._recording:
            return
        self._recording = False
        if self._capture_thread is not None:
            self._capture_thread.join()

        frames = list(self._frames)  # copy before background thread reads
        callback = on_done or self._on_result

        def _worker():
            # BUG #1b (SPEC-36 round 2): _transcribe_frames must never kill this
            # daemon thread — an unguarded exception would silently drop the
            # callback and leave the caller's UI stuck. On failure, log and
            # deliver "" (same contract as an empty transcript).
            try:
                text = self._transcribe_frames(frames)
            except Exception as exc:  # noqa: BLE001 — transcription failure must never kill this worker thread
                _logger.warning("STT: transcription worker failed: %s", exc)
                text = ""
            if callback:
                try:
                    from gi.repository import GLib
                    GLib.idle_add(lambda: callback(text or ""))
                except ImportError:
                    callback(text or "")

        threading.Thread(target=_worker, daemon=True).start()

    def stop(self) -> str:
        """Stop capturing and transcribe synchronously; blocks until complete."""
        if not self._recording:
            return ""
        self._recording = False
        if self._capture_thread is not None:
            self._capture_thread.join()
        return self._transcribe_frames(self._frames)

    @property
    def is_recording(self) -> bool:
        """True while audio capture is active."""
        return self._recording

    # ── Internal ───────────────────────────────────────────────────────────────

    def _capture_loop(self):
        """Background thread: runs arecord at 16kHz mono, buffers raw PCM frames.

        BUG #1 (SPEC-36 round 2): -t raw (headerless PCM), NOT -t wav — a RIFF
        header here would be re-wrapped by _transcribe_frames into a second WAV
        header, corrupting the payload.
        """
        self._capture_proc = subprocess.Popen(
            ["arecord", "-D", self._device, "-f", "S16_LE", "-t", "raw", "-q",
             "-r", str(SAMPLE_RATE), "-c", str(CHANNELS)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        while self._recording:
            stdout = self._capture_proc.stdout
            if stdout is None:
                break
            # Read in 1-second chunks
            chunk = stdout.read(SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH)
            if not chunk:
                break
            self._frames.append(chunk)
        self._capture_proc.terminate()
        self._capture_proc.wait()

    def _build_request_body(self, wav_b64: str, model: str | None = None) -> dict:
        """Build the OpenRouter STT request body for `model` (SPEC-37).

        model=None resolves the model here via stt_config (env STT_MODEL →
        stt.yaml → default) so direct callers stay source-compatible. The
        phraseList block is emitted ONLY when phrases exist AND the model is
        measured to accept it (is_bias_supported) — every other model must
        never receive provider.options.azure (OpenRouter 400s or drops it).
        """
        # Lazy import inside the request path (SPEC-37): the model is resolved
        # per transcription, never cached on the engine, so a Settings change
        # applies on the NEXT dictation without an app restart.
        from utils import stt_config

        if model is None:
            model = stt_config.get_stt_model()
        body = {
            "model": model,
            "input_audio": {"data": wav_b64, "format": "wav"},
        }
        if self._phrases and stt_config.is_bias_supported(model):
            body["provider"] = {
                "options": {"azure": {"phraseList": {"phrases": list(self._phrases)}}}
            }
        return body

    def _transcribe_frames(self, frames) -> str:
        """
        Base64-encode captured frames as WAV and transcribe via OpenRouter.

        Returns "" on silent audio, missing API key, HTTP error, or empty transcript
        (same contract as the old engine on silent audio).
        """
        if not frames:
            return ""
        api_key = self._api_key or _resolve_api_key()
        if not api_key:
            _logger.warning("STT: no OpenRouter API key — skipping transcription")
            return ""

        # Assemble PCM frames into a valid in-memory WAV container, then base64 it.
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(b"".join(frames))
        body = self._build_request_body(base64.b64encode(buf.getvalue()).decode("ascii"))

        # Lazy import: keeps module import cheap and test-friendly (SPEC-36 §5).
        import requests

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                _STT_URL, json=body, headers=headers, timeout=_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            _logger.warning("STT: OpenRouter request failed: %s", exc)
            return ""

        if resp.status_code != 200:
            _logger.warning(
                "STT: OpenRouter HTTP %s — %s",
                resp.status_code, resp.text[:_BODY_PREVIEW_CHARS],
            )
            return ""

        try:
            data = resp.json()
        except ValueError:
            _logger.warning(
                "STT: OpenRouter returned non-JSON — %s",
                resp.text[:_BODY_PREVIEW_CHARS],
            )
            return ""

        # BUG #3 (SPEC-36 round 2): validate payload shape before use — a
        # non-object JSON body or non-str "text" must never crash the worker.
        if not isinstance(data, dict):
            _logger.warning(
                "STT: OpenRouter returned non-object JSON — %s",
                resp.text[:_BODY_PREVIEW_CHARS],
            )
            return ""
        text_val = data.get("text")
        if not isinstance(text_val, str):
            _logger.warning(
                "STT: OpenRouter 'text' is %s, expected str — %s",
                type(text_val).__name__, resp.text[:_BODY_PREVIEW_CHARS],
            )
            return ""
        text = text_val.strip()
        if not text:
            _logger.warning(
                "STT: OpenRouter HTTP %s returned empty text — %s",
                resp.status_code, resp.text[:_BODY_PREVIEW_CHARS],
            )
            return ""

        usage = data.get("usage") or {}
        _logger.info(
            "STT: transcribed %.2fs for $%s",
            usage.get("seconds", 0) or 0,
            usage.get("cost", "?"),
        )
        return text
