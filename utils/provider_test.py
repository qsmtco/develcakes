# utils/provider_test.py
# Test Connection — verify an LLM provider API key by sending a minimal request.
# Pure network I/O — no GTK, no logging, returns a result dataclass.
#
# Manifest:
#   - Reads: nothing
#   - Writes: nothing
#   - Network: yes (HTTPS POST to provider)
#   - Imports: stdlib urllib, json, time; dataclasses
#
# Architecture: standalone — does NOT import agent.* or ui.*.
# Mirrors agent/runtime.py provider call patterns without pulling in the runtime.

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from utils.git_ops import _safe_error  # LOW-9

# MED-5: Custom redirect handler that strips Authorization on cross-host redirects
class _NoAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strips Authorization header when following redirects to a different host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlparse
        result = urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl
        )
        if result is not None:
            old_host = urlparse(req.full_url).hostname
            new_host = urlparse(newurl).hostname
            if old_host and new_host and old_host != new_host:
                if "Authorization" in result.headers:
                    del result.headers["Authorization"]
                if "authorization" in result.unredirected_hdrs:
                    del result.unredirected_hdrs["authorization"]
        return result

# Provider names that use the OpenAI-compatible chat/completions endpoint
_OPENAI_COMPATIBLE = {"openai", "openrouter", "zai", "minimax"}


# Prevent pytest from collecting test_connection as a test function
__test__ = False


@dataclass
class TestResult:  # noqa: N801 — pytest safe: has __init__, won't be collected as test class
    """Result of a Test Connection attempt."""
    ok: bool
    latency_ms: int           # round-trip time; 0 on failure
    error: str | None         # provider's error message; None on success
    model_used: str           # the model string that was passed in
    context_window: int | None = None  # context window in tokens, if discoverable via /v1/models


def _model_id(model: str) -> str:
    """Strip the provider prefix from a model string.
    'openrouter/qwen/qwen3.7-max' -> 'qwen/qwen3.7-max'
    'MiniMax-M2.7' -> 'MiniMax-M2.7' (no slash → returned as-is)
    """
    if "/" in model:
        return model.split("/", 1)[1]
    return model


def _provider_name(model: str) -> str:
    """Extract provider name from model string.
    'openrouter/qwen/qwen3.7-max' -> 'openrouter'
    'MiniMax-M2.7' -> 'MiniMax-M2.7'
    """
    if "/" in model:
        return model.split("/", 1)[0]
    return model


def test_connection(
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float = 8.0,
    caller: str | None = None,          # PHASE-10: explicit caller key from ProviderConfig
) -> TestResult:
    """
    Send a 1-token completion to the provider. Returns TestResult.

    Provider detection: model.split("/")[0] if "/" in model, else default.
    OpenAI-compatible: POST {base_url}/chat/completions with Bearer auth.
    Anthropic: POST {base_url}/messages with x-api-key auth.
    MiniMax: same as OpenAI-compatible but checks body-level errors.

    On MiniMax, a body-level error (HTTP 200 with base_resp.status_code != 0)
    is treated as failure — mirroring agent.llm.minimax_provider.MiniMaxProvider().call.
    """
    # PHASE-10: prefer explicit caller when provided; fall back to model prefix derivation
    if caller:
        provider = caller.lower()
    else:
        provider = _provider_name(model).lower()
    bare_model = _model_id(model)

    if provider in _OPENAI_COMPATIBLE:
        # MED-5: Validate non-loopback provider URL must use https://
        try:
            from utils.provider_url import validate_provider_url
            validate_provider_url(base_url)
        except ImportError:
            pass
        except ValueError:
            return TestResult(
                ok=False,
                latency_ms=0,
                error=f"Provider URL is not HTTPS for non-loopback host: {base_url}",
                model_used=model,
            )
        return _test_openai_compat(base_url, api_key, model, bare_model, provider, timeout_seconds)
    elif provider == "anthropic":
        return _test_anthropic(base_url, api_key, model, bare_model, timeout_seconds)
    else:
        raise ValueError(f"No adapter for provider: {provider}")


# Prevent pytest from collecting this as a test function
test_connection.__test__ = False  # type: ignore[attr-defined]


def _test_openai_compat(
    base_url: str,
    api_key: str,
    model: str,
    bare_model: str,
    provider: str,
    timeout_seconds: float,
) -> TestResult:
    """Test an OpenAI-compatible provider (openai, openrouter, zai, minimax)."""
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": bare_model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    return _do_request(endpoint, body, headers, model, provider, timeout_seconds,
                         base_url=base_url, api_key=api_key)


def _test_anthropic(
    base_url: str,
    api_key: str,
    model: str,
    bare_model: str,
    timeout_seconds: float,
) -> TestResult:
    """Test an Anthropic provider."""
    endpoint = f"{base_url.rstrip('/')}/messages"
    payload = {
        "model": bare_model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    return _do_request(endpoint, body, headers, model, "anthropic", timeout_seconds)


def _do_request(
    endpoint: str,
    body: bytes,
    headers: dict[str, str],
    model: str,
    provider: str,
    timeout_seconds: float,
    base_url: str = "",
    api_key: str = "",
) -> TestResult:
    """Execute the HTTP request and return a TestResult."""
    start = time.monotonic()
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")

    # MED-5: Custom redirect handler strips Authorization on cross-host redirect.
    # We install a custom opener globally so that BOTH the POST and the
    # /v1/models GET probe use it. The previous implementation forgot to
    # restore the module-global urllib.request._opener, leaking the redirect
    # handler into every subsequent urlopen() call in the process (audit BUG #1).
    # We now save the previous value and restore it in finally — the mutation
    # is bounded to the duration of this request.
    _saved_opener = urllib.request._opener
    _opener = urllib.request.build_opener(_NoAuthRedirectHandler)
    urllib.request.install_opener(_opener)
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read()
            elapsed_ms = int((time.monotonic() - start) * 1000)

            try:
                result = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as e:
                return TestResult(
                    ok=False,
                    latency_ms=elapsed_ms,
                    error=f"Invalid JSON response: {str(e)[:150]}",
                    model_used=model,
                )

            # MiniMax body-level error check (HTTP 200 with base_resp.status_code != 0)
            if provider == "minimax":
                base_resp = result.get("base_resp")
                if isinstance(base_resp, dict):
                    status_code = base_resp.get("status_code", 0)
                    if status_code != 0:
                        status_msg = base_resp.get("status_msg", "unknown error")
                        return TestResult(
                            ok=False,
                            latency_ms=elapsed_ms,
                            error=f"status_code={status_code}: {status_msg}",
                            model_used=model,
                        )

            # ── Probe /v1/models for context window (best-effort) ────────
            # If the successful POST happened, try to discover the model's
            # context window from the OpenAI-compatible /v1/models endpoint.
            # Failures here are non-fatal — many providers don't expose this.
            #
            # BUG #10: Only probe for OpenAI-compatible callers. Anthropic
            # does not expose /v1/models; the request would always fail.
            # BUG #2: Try BOTH the full model string AND the stripped version
            # (OpenRouter keeps vendor prefix in id; OpenAI strips it).
            context_window: int | None = None
            if base_url and api_key and provider in _OPENAI_COMPATIBLE:
                try:
                    models_url = base_url.rstrip("/") + "/models"
                    models_req = urllib.request.Request(models_url, method="GET")
                    models_req.add_header("Authorization", f"Bearer {api_key}")
                    with urllib.request.urlopen(models_req, timeout=10) as mresp:
                        models_body = json.loads(mresp.read().decode("utf-8"))
                    # Try matching both the full model string and the bare
                    # (provider-prefix-stripped) variant. Some providers keep
                    # the prefix (OpenRouter: id="openai/gpt-4o"); others strip
                    # it (OpenAI direct: id="gpt-4o").
                    model_id_full = model
                    model_id_bare = model.split("/", 1)[-1]
                    for model_obj in models_body.get("data", []):
                        obj_id = model_obj.get("id", "")
                        if obj_id == model_id_full or obj_id == model_id_bare:
                            for field in ("context_window", "max_context_length",
                                          "context_length", "max_tokens",
                                          "max_model_len"):
                                if field in model_obj and isinstance(model_obj[field], int):
                                    context_window = int(model_obj[field])
                                    break
                            break
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                        json.JSONDecodeError, KeyError, OSError, ValueError,
                        socket.timeout):
                    pass  # Non-fatal — leave context_window as None

            return TestResult(
                ok=True,
                latency_ms=elapsed_ms,
                error=None,
                model_used=model,
                context_window=context_window,
            )

    except urllib.error.HTTPError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            error_body = "(could not read body)"
        return TestResult(
            ok=False,
            latency_ms=elapsed_ms,
            error=f"HTTP {e.code}: {error_body}",
            model_used=model,
        )

    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return TestResult(
            ok=False,
            latency_ms=elapsed_ms,
            error=_safe_error(e),
            model_used=model,
        )
    finally:
        # Restore module-global urllib state to what it was before this call.
        # Critical: urllib.request.install_opener() mutates urllib.request._opener
        # for the entire process — without this restore, our _NoAuthRedirectHandler
        # would leak into every subsequent urlopen() in agent/runtime.py and
        # tests/generate_synthetic_conversations.py
        # (audit BUG #1).
        urllib.request._opener = _saved_opener
