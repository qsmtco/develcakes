"""SPEC-63 §2.2 — /models ID discovery (pure network I/O, stdlib only).

Opener discipline (v3): a LOCAL opener built per call — NEVER
urllib.request.install_opener (process-global mutation; audit BUG #1 in
utils/provider_test.py). This module defines its OWN _NoAuthRedirectHandler
(no cross-module private import of provider_test's), and does NOT import
utils.git_ops: it pulls GitPython at module scope (127 modules measured),
and its _safe_error does NOT redact keys (sk-SECRET-123 survives verbatim).
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

MAX_MODELS = 500  # a runaway list must not freeze GTK

# Duplication-invariant twin of utils/provider_test.py:43 (_OPENAI_COMPATIBLE).
# Keep in sync — enforced by tests/test_model_catalog.py
# (TestOpenAICompatibleDuplicationInvariant), same pattern as
# utils/providers_store.py:35-41 (_VALID_CALLERS).
_OPENAI_COMPATIBLE_LOCAL = {"openai", "openrouter", "zai", "minimax"}


class _NoAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strips Authorization on cross-host redirects. Behavior-identical to
    utils/provider_test._NoAuthRedirectHandler — the regression test
    compares both classes' redirect behavior so they cannot drift."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        result = urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl
        )
        if result is not None:
            old_host = urllib.parse.urlparse(req.full_url).hostname
            new_host = urllib.parse.urlparse(newurl).hostname
            if old_host and new_host and old_host != new_host:
                if "Authorization" in result.headers:
                    del result.headers["Authorization"]
                if "authorization" in result.unredirected_hdrs:
                    del result.unredirected_hdrs["authorization"]
        return result


_KEY_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|Bearer \S+)")


def _safe_error(e: Exception) -> str:
    """str(e), absolute paths stripped, key-shaped substrings redacted."""
    s = str(e)
    s = re.sub(r"(?<=\s)/[^\s\"']+", "<path>", s)
    return _KEY_RE.sub("<redacted>", s)


@dataclass(frozen=True)
class ModelListResult:
    ok: bool
    model_ids: tuple[str, ...]   # ordered as returned; deduped, blanks dropped
    error: str | None            # raw-safe string (paths stripped, keys redacted)


def _open(req, timeout_seconds: float):
    """Single network seam — tests patch THIS, not urllib globally.
    Builds a fresh LOCAL opener every call; never install_opener."""
    opener = urllib.request.build_opener(_NoAuthRedirectHandler)
    return opener.open(req, timeout=timeout_seconds)


def fetch_model_ids(
    base_url: str,
    api_key: str,
    caller: str,
    timeout_seconds: float = 8.0,
) -> ModelListResult:
    """Fetch {base_url}/models; return deduplicated IDs (≤ MAX_MODELS).
    Total function — never raises. Anthropic short-circuits, no request."""
    if caller not in _OPENAI_COMPATIBLE_LOCAL:
        return ModelListResult(
            ok=False, model_ids=(),
            error="This provider does not expose a /models endpoint",
        )
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with _open(req, timeout_seconds) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        ids: list[str] = []
        seen: set[str] = set()
        for obj in body.get("data", []):
            if not isinstance(obj, dict):
                continue
            model_id = obj.get("id", "")
            if model_id and model_id not in seen:
                seen.add(model_id)
                ids.append(model_id)
            if len(ids) >= MAX_MODELS:
                break
        return ModelListResult(ok=True, model_ids=tuple(ids), error=None)
    except urllib.error.HTTPError as e:  # subclass of URLError — caught FIRST
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except (OSError, http.client.HTTPException, ValueError,
                UnicodeDecodeError):
            detail = "(could not read body)"
        return ModelListResult(ok=False, model_ids=(),
                               error=f"HTTP {e.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError,
            json.JSONDecodeError, ValueError, KeyError, UnicodeDecodeError) as e:
        return ModelListResult(ok=False, model_ids=(), error=_safe_error(e))
