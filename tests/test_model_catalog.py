# tests/test_model_catalog.py
# SPEC-63 §2.2 — /models ID discovery: LOCAL opener (never install_opener
# — audit BUG #1), no git_ops import (GitPython at module scope), the _open
# seam as the only patch point, redirect parity with provider_test's
# handler, dedupe/order/cap, total-function error paths, key redaction.

from __future__ import annotations

import dataclasses
import json as jsonlib
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

import utils.model_catalog as mc
from utils.model_catalog import MAX_MODELS, ModelListResult, fetch_model_ids


def _ctx_manager(obj):
    """Wrap any object as a context manager (tests fake the response)."""
    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp(obj)


# ── opener discipline ──────────────────────────────────────────────────────


class TestLocalOpenerDiscipline:
    def test_global_opener_untouched_by_fetch(self, monkeypatch):
        before = urllib.request._opener
        monkeypatch.setattr(mc, "_open", lambda req, t: _ctx_manager(b"{}"))
        fetch_model_ids("https://api.example.com", "sk-x", "openai")
        assert urllib.request._opener is before

    def test_install_opener_never_called(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("install_opener must never be called")
        monkeypatch.setattr(urllib.request, "install_opener", boom)
        monkeypatch.setattr(mc, "_open", lambda req, t: _ctx_manager(b"{}"))
        fetch_model_ids("https://api.example.com", "sk-x", "openai")

    def test_build_opener_receives_local_redirect_handler(self, monkeypatch):
        seen = {}

        def fake_build_opener(*handlers):
            seen["handlers"] = handlers
            opener = type("Opener", (), {})()
            opener.open = lambda req, timeout=None: _ctx_manager(b"{}")
            return opener

        monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
        mc._open(urllib.request.Request("https://x.example"), 1.0)
        assert any(
            h is mc._NoAuthRedirectHandler  # passed as the class
            or isinstance(h, mc._NoAuthRedirectHandler)
            for h in seen["handlers"]
        )


class TestRedirectHandlerParity:
    """model_catalog's handler must behave identically to
    provider_test's — direct invocation, zero sockets."""

    def _both(self):
        from utils.provider_test import _NoAuthRedirectHandler as PT
        return mc._NoAuthRedirectHandler(), PT()

    def test_cross_host_strips_authorization(self):
        for handler in self._both():
            req = urllib.request.Request("https://a.example/v1/models")
            req.add_header("Authorization", "Bearer sk-SECRET-123")
            newurl = "https://b.example/v1/models"
            result = handler.redirect_request(
                req, None, 302, "Found", {}, newurl
            )
            assert result is not None
            auth = result.get_header("Authorization")
            assert auth is None or auth == ""

    def test_same_host_keeps_authorization(self):
        for handler in self._both():
            req = urllib.request.Request("https://a.example/v1/models")
            req.add_header("Authorization", "Bearer sk-SECRET-123")
            newurl = "https://a.example/v2/models"
            result = handler.redirect_request(
                req, None, 302, "Found", {}, newurl
            )
            assert result is not None
            assert result.get_header("Authorization") == "Bearer sk-SECRET-123"

    def test_handlers_agree_on_a_brief(self):
        req = urllib.request.Request("https://a.example/v1/models")
        req.add_header("Authorization", "Bearer sk-SECRET-123")
        results = []
        for handler in self._both():
            r = handler.redirect_request(
                req, None, 302, "Contains sk-xyz", {}, "https://b.example/x"
            )
            results.append(r.full_url if r is not None else None)
        assert results[0] == results[1]


class TestOpenAICompatibleDuplicationInvariant:
    def test_local_set_matches_provider_test(self):
        from utils.provider_test import _OPENAI_COMPATIBLE
        assert mc._OPENAI_COMPATIBLE_LOCAL == _OPENAI_COMPATIBLE


# ── no-git-import hygiene ─────────────────────────────────────────────────


class TestNoGitImport:
    def test_importing_model_catalog_does_not_pull_gitpython(self):
        code = (
            "import sys; import utils.model_catalog; "
            "assert 'git' not in sys.modules, sorted(m for m in sys.modules if 'git' in m); "
            "print('clean')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            cwd=str(pytest_root_path()), check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "clean" in proc.stdout


def pytest_root_path():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent


# ── fetch semantics ────────────────────────────────────────────────────────


class TestFetchHappyPath:
    def test_dedupe_order_blank_drop(self, monkeypatch):
        body = {"data": [
            {"id": "b"}, {"id": "a"}, {"id": "b"}, {"id": ""},
            {"id": None}, {"id": "a"}, "not-a-dict",
        ]}
        monkeypatch.setattr(mc, "_open", lambda req, t: _ctx_manager(
            jsonlib.dumps(body).encode()
        ))
        result = fetch_model_ids("https://api.example.com", "sk-x", "openai")
        assert result.ok is True
        assert result.model_ids == ("b", "a")

    def test_url_gets_models_path(self, monkeypatch):
        seen = {}

        def fake_open(req, t):
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            return _ctx_manager(b'{"data": []}')

        monkeypatch.setattr(mc, "_open", fake_open)
        fetch_model_ids("https://api.example.com/v1/", "sk-x", "openai")
        assert seen["url"] == "https://api.example.com/v1/models"
        assert seen["auth"] == "Bearer sk-x"

    def test_cap_500(self, monkeypatch):
        body = {"data": [{"id": f"m{i}"} for i in range(600)]}
        monkeypatch.setattr(mc, "_open", lambda req, t: _ctx_manager(
            jsonlib.dumps(body).encode()
        ))
        result = fetch_model_ids("https://api.example.com", "sk-x", "openai")
        assert result.ok is True
        assert len(result.model_ids) == MAX_MODELS == 500


class TestFetchErrorPaths:
    def test_anthropic_short_circuits_no_request(self, monkeypatch):
        def boom(req, t):
            raise AssertionError("no request may be made for anthropic")
        monkeypatch.setattr(mc, "_open", boom)
        result = fetch_model_ids("https://api.anthropic.com", "sk-x",
                                 "anthropic")
        assert result.ok is False
        assert "does not expose" in (result.error or "")
        assert result.model_ids == ()

    def test_http_error_status_in_string(self, monkeypatch):
        err = urllib.error.HTTPError(
            "https://api.example.com/models", 401, "Unauthorized",
            hdrs=None, fp=None,
        )
        monkeypatch.setattr(mc, "_open", lambda req, t: (_ for _ in ()).throw(err))
        result = fetch_model_ids("https://api.example.com", "sk-x", "openai")
        assert result.ok is False
        assert "HTTP 401" in (result.error or "")

    def test_urlerror_path(self, monkeypatch):
        err = urllib.error.URLError(TimeoutError("timed out"))
        monkeypatch.setattr(mc, "_open", lambda req, t: (_ for _ in ()).throw(err))
        result = fetch_model_ids("https://api.example.com", "sk-x", "openai")
        assert result.ok is False
        # timeout → network-class error string, redacted and path-stripped
        assert result.error is not None


class TestSafeError:
    def test_key_redacted(self):
        out = mc._safe_error(ValueError("<urlopen error [Errno 8] sk-SECRET123456>"))
        assert "sk-SECRET123456" not in out
        assert "<redacted>" in out

    def test_absolute_path_stripped(self):
        out = mc._safe_error(ValueError("cannot open /home/mushy/secret/file.yaml now"))
        assert "/home/mushy/secret/file.yaml" not in out
        assert "<path>" in out

    def test_bearer_redacted(self):
        out = mc._safe_error(ValueError("sent Bearer abc123"))
        assert "abc123" not in out


class TestModelListResultShape:
    def test_frozen(self):
        r = ModelListResult(ok=True, model_ids=("a",), error=None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.ok = False
