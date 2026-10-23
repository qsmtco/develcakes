# tests/test_improve.py
# Tests for utils/improve.py — prompt improvement via the configured Improve
# Model provider card (SPEC-55). No MiniMax, no config.json.
#
# Principle: test the failure modes that would break callers.
# Mock urllib at the network layer to simulate API errors and malformed responses.

import json
import time
import urllib.error
import urllib.request

import pytest
from utils.improve import improve_prompt, load_improve_card


# ── Helpers ────────────────────────────────────────────────────────────────────

class FakeResponse:
    """Wraps a dict as a file-like HTTP response for urllib.urlopen."""

    def __init__(self, data: dict, status: int = 200):
        self._data = data
        self._status = status

    def read(self):
        return json.dumps(self._data).encode()

    def getcode(self):
        return self._status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeCard:
    """A minimal stand-in for the ImproveModel ProviderConfig."""

    def __init__(self, *, api_key="test-key",
                 base_url="https://api.example.com/v1",
                 default_model="openai/gpt-4o-mini"):
        self.api_key = api_key
        self.base_url = base_url
        self.default_model = default_model


def _use_card(monkeypatch, card=FakeCard()):
    """Point utils.improve.load_improve_card at a fake card (or None)."""
    monkeypatch.setattr("utils.improve.load_improve_card", lambda *a, **k: card)


@pytest.fixture(autouse=True)
def _route_http_through_urlopen(monkeypatch):
    """Make the per-test `urlopen` patches actually intercept the HTTP call.

    improve_prompt performs its request via `urllib.request.build_opener(...)
    .open(...)` (the MED-5 redirect handler), NOT via the module-level
    `urlopen`. Patching only `urlopen` therefore left the real opener live and
    these tests talked to the REAL provider API using the key from providers.yaml.

    This fixture swaps the opener for one that delegates to
    `urllib.request.urlopen` at call time — the boundary each test fakes — so
    the suite is hermetic and the existing per-test patches take effect.
    """
    class _UrlopenOpener:
        def open(self, req, timeout=None):
            return urllib.request.urlopen(req, timeout=timeout)

    monkeypatch.setattr(
        "utils.improve.urllib.request.build_opener",
        lambda *args, **kwargs: _UrlopenOpener(),
    )


def call_improve(raw_text: str, callback_store: list, timeout: float = 5.0):
    """
    Call improve_prompt and wait (bounded) for the background thread's callback.
    Stores (text, error) in callback_store for assertion.

    improve_prompt ALWAYS runs its work on a daemon thread (utils/improve.py);
    the GLib argument only controls where the callback is *scheduled*. With
    GLib=None the callback is invoked inline on that worker thread, so the test
    must poll until it lands. A bounded wait (not a sleep) keeps the suite fast
    and turns a genuine "callback never fires" regression into a clear failure
    instead of an IndexError.
    """
    def capture(text, error):
        callback_store.append((text, error))

    improve_prompt(raw_text, capture, GLib=None)

    deadline = time.monotonic() + timeout
    while not callback_store and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(callback_store) == 1, (
        f"callback not invoked within {timeout}s "
        f"(got {len(callback_store)} calls)"
    )


# ── Card Configuration Tests ───────────────────────────────────────────────────

class TestCardConfiguration:
    """The Improve Model card drives the request; missing card / fields error."""

    def test_no_card_calls_callback_with_error(self, monkeypatch):
        """No ImproveModel card → callback receives the 'not configured' hint."""
        callback_store: list = []
        _use_card(monkeypatch, card=None)

        call_improve("test prompt", callback_store)

        assert len(callback_store) == 1
        text, error = callback_store[0]
        assert text is None
        assert error is not None
        assert "not configured" in error

    def test_missing_api_key_calls_callback_with_error(self, monkeypatch):
        """Card with blank api_key → error names the field."""
        callback_store: list = []
        _use_card(monkeypatch, FakeCard(api_key=""))

        call_improve("test prompt", callback_store)

        assert len(callback_store) == 1
        text, error = callback_store[0]
        assert text is None
        assert error is not None
        assert "api_key" in error

    def test_whitespace_only_api_key_calls_callback_with_error(self, monkeypatch):
        """api_key that is all whitespace must be treated as missing."""
        callback_store: list = []
        _use_card(monkeypatch, FakeCard(api_key="   "))

        call_improve("test prompt", callback_store)

        assert len(callback_store) == 1
        _, error = callback_store[0]
        assert error is not None
        assert "api_key" in error

    def test_missing_base_url_calls_callback_with_error(self, monkeypatch):
        """Card with blank base_url → error names the field."""
        callback_store: list = []
        _use_card(monkeypatch, FakeCard(base_url=""))

        call_improve("test prompt", callback_store)

        assert len(callback_store) == 1
        _, error = callback_store[0]
        assert error is not None
        assert "base_url" in error

    def test_missing_default_model_calls_callback_with_error(self, monkeypatch):
        """Card with blank default_model → error names the field."""
        callback_store: list = []
        _use_card(monkeypatch, FakeCard(default_model=""))

        call_improve("test prompt", callback_store)

        assert len(callback_store) == 1
        _, error = callback_store[0]
        assert error is not None
        assert "default_model" in error


# ── HTTP Error Tests ───────────────────────────────────────────────────────────

class TestHttpErrors:
    """Network errors and HTTP error responses must call callback with error string."""

    def test_connection_error_calls_callback_with_error(self, monkeypatch):
        """urllib error (connection refused, DNS fail) must be caught and reported."""
        callback_store: list = []

        def raise_urlerror(*args, **kwargs):
            raise urllib.error.URLError("connection refused")

        _use_card(monkeypatch)
        monkeypatch.setattr("utils.improve.urllib.request.urlopen", raise_urlerror)

        call_improve("test prompt", callback_store)

        assert len(callback_store) == 1
        _, error = callback_store[0]
        assert error is not None
        assert "connection refused" in error

    def test_http_403_calls_callback_with_error(self, monkeypatch):
        """HTTP 403 Forbidden must not crash — must call callback with error."""
        callback_store: list = []

        def fake_open(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {}, None
            )

        _use_card(monkeypatch)
        monkeypatch.setattr("utils.improve.urllib.request.urlopen", fake_open)

        call_improve("test prompt", callback_store)

        assert len(callback_store) == 1
        _, error = callback_store[0]
        assert error is not None

    def test_http_500_calls_callback_with_error(self, monkeypatch):
        """HTTP 500 Internal Server Error must not crash."""
        callback_store: list = []

        def fake_open(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 500, "Internal Server Error", {}, None
            )

        _use_card(monkeypatch)
        monkeypatch.setattr("utils.improve.urllib.request.urlopen", fake_open)

        call_improve("test prompt", callback_store)

        assert len(callback_store) == 1
        _, error = callback_store[0]
        assert error is not None


# ── Malformed Response Tests ───────────────────────────────────────────────────

class TestMalformedResponses:
    """API responses that don't match the expected shape must call callback with error."""

    def test_missing_choices_key_calls_callback_with_error(self, monkeypatch):
        """Response with no 'choices' key must be caught and reported, not crash."""
        callback_store: list = []

        def fake_open(req, timeout=None):
            return FakeResponse({"id": "abc", "choices": None})

        _use_card(monkeypatch)
        monkeypatch.setattr("utils.improve.urllib.request.urlopen", fake_open)

        call_improve("test prompt", callback_store)

        assert len(callback_store) == 1
        _, error = callback_store[0]
        assert error is not None
        assert "choices" in error.lower() or "api" in error.lower()

    def test_empty_choices_list_calls_callback_with_error(self, monkeypatch):
        """Response with choices=[] must report an error — nothing to extract."""
        callback_store: list = []

        def fake_open(req, timeout=None):
            return FakeResponse({"id": "abc", "choices": []})

        _use_card(monkeypatch)
        monkeypatch.setattr("utils.improve.urllib.request.urlopen", fake_open)

        call_improve("test prompt", callback_store)

        assert len(callback_store) == 1
        _, error = callback_store[0]
        assert error is not None

    def test_missing_message_in_choice_calls_callback_with_error(self, monkeypatch):
        """choices[0] has no 'message' key must be caught, not crash."""
        callback_store: list = []

        def fake_open(req, timeout=None):
            return FakeResponse({"id": "abc", "choices": [{"no": "message here"}]})

        _use_card(monkeypatch)
        monkeypatch.setattr("utils.improve.urllib.request.urlopen", fake_open)

        call_improve("test prompt", callback_store)

        assert len(callback_store) == 1
        _, error = callback_store[0]
        assert error is not None

    def test_missing_content_in_message_calls_callback_with_error(self, monkeypatch):
        """choices[0].message has no 'content' key — must call callback with error."""
        callback_store: list = []

        def fake_open(req, timeout=None):
            return FakeResponse({
                "id": "abc",
                "choices": [{"message": {"role": "assistant"}}]
            })

        _use_card(monkeypatch)
        monkeypatch.setattr("utils.improve.urllib.request.urlopen", fake_open)

        call_improve("test prompt", callback_store)

        assert len(callback_store) == 1
        _, error = callback_store[0]
        assert error is not None
        assert "content" in error.lower()

    def test_content_is_none_returns_empty_string(self, monkeypatch):
        """choices[0].message.content is None — must return '', not crash."""
        callback_store: list = []

        def fake_open(req, timeout=None):
            return FakeResponse({
                "id": "abc",
                "choices": [{"message": {"content": None}}]
            })

        _use_card(monkeypatch)
        monkeypatch.setattr("utils.improve.urllib.request.urlopen", fake_open)

        call_improve("test prompt", callback_store)

        assert len(callback_store) == 1
        text, error = callback_store[0]
        assert error is None
        assert text == ""

    def test_content_is_int_becomes_string(self, monkeypatch):
        """choices[0].message.content is an int — must become string."""
        callback_store: list = []

        def fake_open(req, timeout=None):
            return FakeResponse({
                "id": "abc",
                "choices": [{"message": {"content": 12345}}]
            })

        _use_card(monkeypatch)
        monkeypatch.setattr("utils.improve.urllib.request.urlopen", fake_open)

        call_improve("test prompt", callback_store)

        assert len(callback_store) == 1
        text, error = callback_store[0]
        assert error is None
        assert text == "12345"


# ── Happy Path ─────────────────────────────────────────────────────────────────

class TestHappyPath:
    """Confirm the API response parsing works when response is well-formed."""

    def test_valid_response_returns_improved_text(self, monkeypatch):
        """A valid API response must call callback with the improved text."""
        callback_store: list = []

        def fake_open(req, timeout=None):
            return FakeResponse({
                "id": "abc",
                "choices": [{"message": {"content": "This is the improved prompt."}}]
            })

        _use_card(monkeypatch)
        monkeypatch.setattr("utils.improve.urllib.request.urlopen", fake_open)

        call_improve("improve this", callback_store)

        assert len(callback_store) == 1
        text, error = callback_store[0]
        assert error is None
        assert text == "This is the improved prompt."

    def test_request_comes_from_card_and_has_no_minimax(self, monkeypatch):
        """URL/headers/model come from the card; zero MiniMax anywhere."""
        callback_store: list = []
        captured = {}

        def fake_open(req, timeout=None):
            captured["url"] = req.full_url
            captured["authorization"] = req.get_header("Authorization")
            captured["body"] = json.loads(req.data.decode())
            return FakeResponse({
                "id": "abc",
                "choices": [{"message": {"content": "improved"}}]
            })

        _use_card(
            monkeypatch,
            FakeCard(
                api_key="card-key-123",
                base_url="https://my.provider.example/v1",
                default_model="my-vendor/my-model-7b",
            ),
        )
        monkeypatch.setattr("utils.improve.urllib.request.urlopen", fake_open)

        call_improve("improve this", callback_store)

        assert callback_store[0][1] is None
        # SPEC-55 BUG #1: the POST goes to <base_url>/chat/completions (the
        # OpenAI-compatible chat endpoint), never the bare base_url verbatim.
        assert captured["url"] == "https://my.provider.example/v1/chat/completions"
        assert captured["authorization"] == "Bearer card-key-123"
        assert captured["body"]["model"] == "my-vendor/my-model-7b"
        # No MiniMax reference anywhere in the request.
        assert "minimax" not in json.dumps(captured).lower()

    def test_trailing_slash_base_url_is_normalized(self, monkeypatch):
        """A base_url ending in '/' must not produce a double slash."""
        callback_store: list = []
        captured = {}

        def fake_open(req, timeout=None):
            captured["url"] = req.full_url
            return FakeResponse({
                "choices": [{"message": {"content": "improved"}}]
            })

        _use_card(
            monkeypatch,
            FakeCard(base_url="https://my.provider.example/v1/"),
        )
        monkeypatch.setattr("utils.improve.urllib.request.urlopen", fake_open)

        call_improve("improve this", callback_store)

        assert callback_store[0][1] is None
        assert captured["url"] == "https://my.provider.example/v1/chat/completions"

    def test_unprefixed_model_still_posts_to_chat_endpoint(self, monkeypatch):
        """Improve cards with unprefixed models (e.g. gpt-4o) work — no
        caller-prefix logic is involved (SPEC-55 BUG #3 shape)."""
        callback_store: list = []
        captured = {}

        def fake_open(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return FakeResponse({
                "choices": [{"message": {"content": "improved"}}]
            })

        _use_card(monkeypatch, FakeCard(default_model="gpt-4o"))
        monkeypatch.setattr("utils.improve.urllib.request.urlopen", fake_open)

        call_improve("improve this", callback_store)

        assert callback_store[0][1] is None
        assert captured["url"] == "https://api.example.com/v1/chat/completions"
        assert captured["body"]["model"] == "gpt-4o"

    def test_multiline_improved_text_preserved(self, monkeypatch):
        """Multiline improved output must be preserved exactly."""
        callback_store: list = []

        multiline = "Line one.\nLine two.\nLine three."
        def fake_open(req, timeout=None):
            return FakeResponse({
                "id": "abc",
                "choices": [{"message": {"content": multiline}}]
            })

        _use_card(monkeypatch)
        monkeypatch.setattr("utils.improve.urllib.request.urlopen", fake_open)

        call_improve("improve this", callback_store)

        text, _ = callback_store[0]
        assert text == multiline


# ── load_improve_card ──────────────────────────────────────────────────────────

class TestLoadImproveCard:
    """load_improve_card never raises — a bad store degrades to None."""

    def test_corrupt_store_returns_none(self, tmp_config_dir):
        (tmp_config_dir / "providers.yaml").write_text("{{{ not: yaml ][")
        assert load_improve_card() is None

    def test_store_that_raises_returns_none(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("store exploded")

        monkeypatch.setattr("utils.providers_store.load_providers", boom)
        assert load_improve_card() is None

    def test_missing_store_returns_none(self):
        # No providers.yaml at all (isolated config dir) — deterministic floor.
        assert load_improve_card() is None

    def test_finds_named_card(self, tmp_config_dir):
        from utils.providers_store import save_providers
        from models.providers import ProviderConfig
        save_providers([
            ProviderConfig(name="Chat", base_url="https://c/v1",
                           api_key="k", default_model="m"),
            ProviderConfig(name="ImproveModel", base_url="https://i/v1",
                           api_key="ik", default_model="im"),
        ])
        card = load_improve_card()
        assert card is not None
        assert card.name == "ImproveModel"
        assert card.base_url == "https://i/v1"


# ── Source-level residuals ─────────────────────────────────────────────────────

class TestNoMiniMaxOrConfigJsonResiduals:
    """SPEC-55: improve source has zero MiniMax / config.json references."""

    def test_source_has_no_minimax_or_get_config_file(self):
        import pathlib
        src = pathlib.Path(__file__).resolve().parent.parent / "utils" / "improve.py"
        text = src.read_text(encoding="utf-8").lower()
        assert "minimax" not in text
        assert "get_config_file" not in text
        assert "config.json" not in text
