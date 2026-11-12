"""SPEC-63 §2.3 — provider Test-Connection error translation (pure, no I/O).

ONE classification implementation shared by explain_test_error and
error_class, so the message and the class can never disagree (Debugger #16).
The MODEL row evaluates BEFORE the HTTP 404 row (Debugger #17): an
OpenAI-compatible API answers HTTP 404 with body "The model `x` does not
exist" — a model problem, not a wrong-Base-URL problem.

No URL is ever embedded in a returned message (fix-round 2, D#12): the
"Get a key" link is the view's Gtk.LinkButton (ProviderPreset.api_key_url);
a persisted last_error must not embed URLs. Unknown errors pass through
UNCHANGED — never swallow information.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — keeps this module agent/ui/gi-free
    from utils.provider_presets import ProviderPreset

_CLASS_MESSAGES: dict[str, str] = {
    "auth": "Key rejected — check or regenerate it.",
    "model": ("Model ID not accepted. Use 'Load models' to pick a real one, "
              "or check the default model."),
    "url": "Base URL looks wrong — this endpoint doesn't exist. Expected {base_url}.",
    "rate": "Rate limit hit. Wait a minute (free tiers are ~20 req/min) and retry.",
    "network": ("Couldn't reach the provider — check your network / firewall "
                "and the Base URL."),
    "minimax": "Provider rejected the request: {error}. Check the key and model.",
    "caller": "Caller/model mismatch — pick a preset or use a `<vendor>/<model>` model ID.",
    "https": "Base URL must use https:// (except localhost).",
}

_AUTH_MARKERS = ("http 401", "http 403", "invalid api key",
                 "incorrect api key", "unauthorized")
_MODEL_MARKERS = ("model_not_found", "invalid_model", "unknown model",
                  "no such model")
_MODEL_NOT_FOUND_PHRASES = ("not found", "does not exist")


def error_class(error: str | None) -> str:
    """Classify a TestResult.error string: "auth" | "url" | "rate" |
    "network" | "model" | "minimax" | "caller" | "https" | "other"."""
    if not error:
        return "other"
    low = error.lower()

    # MODEL first (Debugger #17) — before auth AND before the 404 row.
    if "model" in low and any(p in low for p in _MODEL_NOT_FOUND_PHRASES):
        return "model"
    if any(m in low for m in _MODEL_MARKERS):
        return "model"

    if any(a in low for a in _AUTH_MARKERS):
        return "auth"
    if "http 404" in low:
        return "url"
    if "http 429" in low:
        return "rate"
    if any(m in low for m in ("timed out", "timeout", "urlopen error",
                              "nameresolutionerror", "connection refused")):
        return "network"
    if "status_code=" in low:
        return "minimax"
    if any(m in low for m in ("no adapter for provider",
                              "cannot auto-detect caller", "invalid caller")):
        return "caller"
    if "provider url is not https" in low:
        return "https"
    return "other"


def explain_test_error(error: str | None, *,
                       preset: ProviderPreset | None = None) -> str:
    """Human message for a TestResult.error. Unknown errors pass through."""
    if not error:
        return "Unknown error"
    cls = error_class(error)
    if cls == "other":
        return error
    msg = _CLASS_MESSAGES[cls]
    if "{base_url}" in msg:
        base = getattr(preset, "base_url", "") if preset is not None else ""
        if base:
            msg = msg.replace("{base_url}", base)
        else:
            msg = "Base URL looks wrong — this endpoint doesn't exist."
    if "{error}" in msg:
        msg = msg.replace("{error}", error)
    return msg
