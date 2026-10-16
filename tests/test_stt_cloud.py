# tests/test_stt_cloud.py
"""Unit tests for the SPEC-36 OpenRouter cloud STT engine (utils/stt.py).

No network (requests faked via sys.modules), no GTK (GLib faked), no arecord
subprocess spawned (threads/subprocess faked or not started). Follows the
FakeSTTEngine pattern from tests/test_media_handler.py.
"""

import logging
import sys
import threading
import types

import utils.stt as stt_module
from utils.stt import STTEngine, _resolve_phrases

# ── Fakes (boundary mocks only — network, GLib, subprocess) ─────────────────


class FakeResponse:
    """Fake requests.Response — status_code, text, json()."""

    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


def install_fake_requests(monkeypatch, response):
    """Replace sys.modules['requests'] with a fake whose post() returns `response`.

    Returns the list of captured post() calls (dicts: url/json/headers/timeout).
    Must be installed before _transcribe_frames runs — it imports lazily.
    """
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return response

    fake = types.ModuleType("requests")
    fake.post = fake_post
    fake.RequestException = type("RequestException", (Exception,), {})
    monkeypatch.setitem(sys.modules, "requests", fake)
    return calls


class FakeGLib:
    """Fake gi.repository.GLib — idle_add runs the callback immediately."""

    @staticmethod
    def idle_add(fn, *args, **kwargs):
        fn(*args, **kwargs)
        return 1


def install_fake_glib(monkeypatch):
    """Make `from gi.repository import GLib` resolve to FakeGLib regardless of
    whether PyGObject is installed in the environment."""
    repo = types.ModuleType("gi.repository")
    repo.GLib = FakeGLib
    gi = types.ModuleType("gi")
    gi.repository = repo
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repo)


class FakeMainContent:
    """Fake MainContent — records update_stt_state calls (no GTK)."""

    def __init__(self):
        self._stt_state = "idle"

    def update_stt_state(self, state):
        self._stt_state = state


class FakeImprove:
    """Fake improve module — never called by STT tests."""

    @staticmethod
    def improve_prompt(text, callback, GLib=None):
        raise AssertionError("improve_prompt must not be called by STT tests")


class RecordingSTTEngine:
    """Fake engine whose start() records (API key present)."""

    def __init__(self, on_result=None):
        self._on_result = on_result
        self._is_recording = False

    @property
    def is_recording(self):
        return self._is_recording

    def start(self):
        self._is_recording = True

    def stop_async(self, on_done=None):
        self._is_recording = False


class DecliningSTTEngine(RecordingSTTEngine):
    """Fake engine whose start() refuses (no API key) — stays idle."""

    def start(self):
        self._is_recording = False  # refusal: state stays idle


def make_engine(monkeypatch, api_key="test-key", phrases=None):
    """STTEngine with an env-resolved API key and optional STT_PHRASES."""
    monkeypatch.setenv("OPENROUTER_API_KEY", api_key)
    if phrases is not None:
        monkeypatch.setenv("STT_PHRASES", phrases)
    else:
        monkeypatch.delenv("STT_PHRASES", raising=False)
    return STTEngine(on_result=lambda _text: None)


# ── _resolve_phrases (BUG #7) ────────────────────────────────────────────────


class TestResolvePhrases:
    def test_unset_returns_default_list(self, monkeypatch):
        monkeypatch.delenv("STT_PHRASES", raising=False)
        assert _resolve_phrases() == ["DevelCakes", "Crabcakes", "crabcakes"]

    def test_empty_string_disables_biasing(self, monkeypatch):
        monkeypatch.setenv("STT_PHRASES", "")
        assert _resolve_phrases() == []

    def test_whitespace_only_disables_biasing(self, monkeypatch):
        monkeypatch.setenv("STT_PHRASES", "   ")
        assert _resolve_phrases() == []

    def test_commas_only_disable_biasing(self, monkeypatch):
        monkeypatch.setenv("STT_PHRASES", ",,")
        assert _resolve_phrases() == []

    def test_comma_space_only_disables_biasing(self, monkeypatch):
        monkeypatch.setenv("STT_PHRASES", " , ")
        assert _resolve_phrases() == []

    def test_mixed_blanks_dropped_from_real_list(self, monkeypatch):
        monkeypatch.setenv("STT_PHRASES", "alpha, , beta ,, ")
        assert _resolve_phrases() == ["alpha", "beta"]

    def test_entries_are_stripped(self, monkeypatch):
        monkeypatch.setenv("STT_PHRASES", "  DevelCakes , crabcakes  ")
        assert _resolve_phrases() == ["DevelCakes", "crabcakes"]


# ── _build_request_body ──────────────────────────────────────────────────────


class TestBuildRequestBody:
    def test_phrases_present_land_in_azure_phaelist(self, monkeypatch):
        engine = make_engine(monkeypatch, phrases="alpha, beta")
        body = engine._build_request_body("QUJD")

        assert body["model"] == "microsoft/mai-transcribe-2"
        assert body["input_audio"] == {"data": "QUJD", "format": "wav"}
        assert (
            body["provider"]["options"]["azure"]["phraseList"]["phrases"]
            == ["alpha", "beta"]
        )

    def test_phrases_empty_means_no_provider_key(self, monkeypatch):
        engine = make_engine(monkeypatch, phrases="")
        body = engine._build_request_body("QUJD")

        assert "provider" not in body
        assert body["model"] == "microsoft/mai-transcribe-2"
        assert body["input_audio"]["format"] == "wav"


# ── _transcribe_frames payload guards (BUG #3) ──────────────────────────────


class TestTranscribePayloadGuards:
    def frames(self):
        # Fresh list per test — mutable default on a class attribute (RUF012).
        return [b"\x01\x02" * 8000]  # 16KB raw PCM ≈ 0.5s — wrapped in WAV by the engine

    def test_happy_path_returns_stripped_text(self, monkeypatch):
        engine = make_engine(monkeypatch)
        calls = install_fake_requests(
            monkeypatch, FakeResponse({"text": "  hello world  ", "usage": {"seconds": 0.5, "cost": 0.001}})
        )

        assert engine._transcribe_frames(self.frames()) == "hello world"

        assert calls[0]["url"] == stt_module._STT_URL
        assert calls[0]["headers"]["Authorization"] == "Bearer test-key"
        assert calls[0]["json"]["input_audio"]["format"] == "wav"

    def test_non_dict_json_returns_empty(self, monkeypatch, caplog):
        engine = make_engine(monkeypatch)
        install_fake_requests(monkeypatch, FakeResponse(["unexpected"], text='["unexpected"]'))

        with caplog.at_level(logging.WARNING, logger="utils.stt"):
            assert engine._transcribe_frames(self.frames()) == ""
        assert "non-object JSON" in caplog.text

    def test_non_str_text_returns_empty(self, monkeypatch, caplog):
        engine = make_engine(monkeypatch)
        install_fake_requests(monkeypatch, FakeResponse({"text": 42}, text='{"text": 42}'))

        with caplog.at_level(logging.WARNING, logger="utils.stt"):
            assert engine._transcribe_frames(self.frames()) == ""
        assert "expected str" in caplog.text

    def test_missing_text_key_returns_empty(self, monkeypatch):
        engine = make_engine(monkeypatch)
        install_fake_requests(monkeypatch, FakeResponse({"transcript": "elsewhere"}))

        assert engine._transcribe_frames(self.frames()) == ""

    def test_empty_frames_short_circuits_without_network(self, monkeypatch):
        engine = make_engine(monkeypatch)
        calls = install_fake_requests(monkeypatch, FakeResponse({"text": "nope"}))

        assert engine._transcribe_frames([]) == ""
        assert calls == []  # no POST for empty capture


# ── start() API-key gate ─────────────────────────────────────────────────────


class TestStartApiKeyGate:
    def test_start_refuses_without_api_key(self, monkeypatch, caplog):
        monkeypatch.setattr(stt_module, "_resolve_api_key", lambda: None)
        engine = STTEngine(on_result=lambda _text: None)

        with caplog.at_level(logging.WARNING, logger="utils.stt"):
            engine.start()

        assert engine.is_recording is False
        assert engine._capture_thread is None  # no capture thread spawned
        assert "refusing to record" in caplog.text

    def test_start_records_with_api_key(self, monkeypatch):
        monkeypatch.setattr(stt_module, "_resolve_api_key", lambda: "k")
        # Fake Thread so no real arecord subprocess is ever spawned; start() only
        # constructs and starts it — the capture loop body never runs here.
        started = []

        class FakeThread:
            def __init__(self, target=None, daemon=False):
                self.target = target
                started.append(target)

            def start(self):
                pass

        monkeypatch.setattr(stt_module.threading, "Thread", FakeThread)
        engine = STTEngine(on_result=lambda _text: None)

        engine.start()

        assert engine.is_recording is True
        assert len(started) == 1  # exactly one capture thread constructed


# ── _worker exception path (BUG #1b) ────────────────────────────────────────


class TestWorkerExceptionPath:
    def _make_recording_engine(self, monkeypatch):
        engine = make_engine(monkeypatch)
        engine._recording = True  # enter "recording" state without spawning arecord
        return engine

    def test_transcribe_exception_still_fires_callback_with_empty(self, monkeypatch):
        install_fake_glib(monkeypatch)
        engine = self._make_recording_engine(monkeypatch)
        engine._frames = [b"\x01\x02"]

        def boom(_frames):
            raise RuntimeError("transcription exploded")

        engine._transcribe_frames = boom
        done = threading.Event()
        received = []

        engine.stop_async(on_done=lambda text: (received.append(text), done.set()))

        assert done.wait(timeout=5), "callback never fired after transcription crash"
        assert received == [""]

    def test_worker_success_fires_callback_with_text(self, monkeypatch):
        install_fake_glib(monkeypatch)
        engine = self._make_recording_engine(monkeypatch)
        engine._frames = [b"\x01\x02"]
        engine._transcribe_frames = lambda _frames: "spoken words"

        done = threading.Event()
        received = []

        engine.stop_async(on_done=lambda text: (received.append(text), done.set()))

        assert done.wait(timeout=5), "callback never fired on success path"
        assert received == ["spoken words"]


# ── MediaHandler.on_stt_click sync (BUG #2) ─────────────────────────────────


class TestOnSttClickStateSync:
    def _make_handler(self, engine_class):
        from ui.handlers.media_handler import MediaHandler

        mc = FakeMainContent()
        handler = MediaHandler(
            main_content=mc,
            improve_module=FakeImprove(),
            GLib_module=None,
            stt_engine_class=engine_class,
        )
        return handler, mc

    def test_declined_start_leaves_button_idle(self):
        handler, mc = self._make_handler(DecliningSTTEngine)

        handler.on_stt_click()

        assert handler._stt_engine.is_recording is False
        assert mc._stt_state == "idle"  # old code stuck on "recording" here

    def test_accepted_start_sets_recording(self):
        handler, mc = self._make_handler(RecordingSTTEngine)

        handler.on_stt_click()

        assert handler._stt_engine.is_recording is True
        assert mc._stt_state == "recording"
