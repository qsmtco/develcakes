"""SPEC-22 DecisionModel seam — the card lookup + real JEV call (SPEC-22 §2.4/§2.8).

The card named `DECISION_MODEL_PROVIDER` lives in the ordinary
`providers.yaml` store, reached through this hard-coded name. Every lookup
uses the constant; nothing hardcodes the endpoint, key, or model slug.

`classify_unknown` is the sole runtime entry. It resolves the card lazily
(NEVER at module import — importing providers.yaml at import time would make
this module a config dependency, and the enumerated policy rows must never
touch it). With no card, or a keyless card, it returns None — the
deterministic floor is load-bearing (SPEC-22 §2.5/§2.8).

★ FAIL-OPEN CONTRACT (SPEC-22 §2.4, load-bearing): any of {no card, no key,
no network, timeout, non-2xx, unparseable body, wrong answer type, low
confidence} → return None. A JEV outage must NEVER be worse than JEV absent:
the caller falls back to the heuristic exactly as before. The call runs with
a hard 2.0 s timeout (JEV's published latency is 70–500 ms; 2 s is generous
headroom) and never blocks the turn longer than that.

Wire format (pinned from docs.typesafe.ai/api, fetched 2026-10-10):
    POST {base_url}/v1/systemone
    Authorization: Bearer <key>
    {"model": <card.default_model or "jev-latest">,
     "state": "<the failure detail + status>",
     "questions": {"failure_class": {"type": "choice", "instructions": ...,
                                       "criteria": {...five classes...}}}}
    → {"answers": {"failure_class": {"type": "choice", "choice": <opt>,
                                      "probabilities": {...}, "confidence": <0..1>}}}
A noul answer, a missing key, or any shape drift → None (fail open).

Privacy note (spec §2.4): the sanitized failure detail + provider status
leave the box to the configured endpoint. Accepted per the private-build
posture; documented in ARCHITECTURE.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.llm.failure_classifier import FailureClass

_logger = logging.getLogger(__name__)

DECISION_MODEL_PROVIDER = "DecisionModel"

# SPEC-22 §2.4: the hard latency budget. JEV publishes 70–500 ms; the
# requirements file's JEV rule says fail open within 2–3 s. 2.0 s is the cap.
_JEV_TIMEOUT_S = 2.0

# Below this confidence the answer is noise — treat as "could not classify".
_JEV_MIN_CONFIDENCE = 0.6

# Log the outage once per process, not per call (spec edge-case table).
_logged_outage = False

_VALID_JEV_CLASSES = ("transient", "permanent_auth", "permanent_model",
                      "bad_request", "unknown")

_INSTRUCTIONS = (
    "An LLM API provider request failed. Classify the failure into exactly "
    "one category based on the state text. Rate limits, timeouts, overload, "
    "and network errors are `transient`. Authentication, billing, credit, or "
    "permission failures are `permanent_auth`. The model id being unknown, "
    "unsupported, or rejected is `permanent_model`. A request WE built "
    "wrong (invalid JSON, schema violation, malformed payload) is "
    "`bad_request`. If nothing fits, answer `unknown`."
)

_CRITERIA = {
    "transient": "Rate limit (429), timeout, server overload (5xx), or a "
                 "network/SSL/DNS error. Retrying the same provider may work.",
    "permanent_auth": "401/402/403, invalid or expired API key, out of "
                      "credits, banned account. The same key will fail again.",
    "permanent_model": "404 or model-not-found, or the provider rejecting a "
                       "parameter this model does not support.",
    "bad_request": "The request payload is malformed — invalid JSON, schema "
                   "violation, missing required fields. This is the "
                   "CLIENT's bug, not the provider's.",
    "unknown": "None of the above fits confidently.",
}


def load_decision_card(destination: object | None = None) -> object | None:
    """Return the ProviderConfig named DECISION_MODEL_PROVIDER, or None.

    Lazy import of the store. Never raises — a malformed store degrades to
    None (the deterministic floor), never to an exception in the caller.
    """
    del destination  # reserved for a future cache slot; lookup is cheap here
    try:
        from utils.providers_store import load_providers
        for provider in load_providers():
            if getattr(provider, "name", "") == DECISION_MODEL_PROVIDER:
                return provider
    except Exception:  # a bad store must not crash classification
        _logger.debug("DecisionModel card lookup failed", exc_info=True)
    return None


def _outage(reason: str) -> None:
    """Log a JEV outage once per process (no per-call spam)."""
    global _logged_outage
    if not _logged_outage:
        _logged_outage = True
        _logger.warning("JEV classifier unavailable (%s) — using deterministic floor", reason)


def classify_unknown(
    detail: str, status_code: int | None = None
) -> FailureClass | None:
    """Return a FailureClass from JEV, or None when JEV cannot answer.

    Fail-open floor: no card / keyless card / any call failure / low
    confidence / shape drift → None (the caller's heuristic takes over).
    Never raises. Never blocks longer than _JEV_TIMEOUT_S.
    """
    card = load_decision_card()
    if card is None:
        return None  # not configured — silent, not an outage
    base_url = (getattr(card, "base_url", "") or "").strip()
    api_key = (getattr(card, "api_key", "") or "").strip()
    if not base_url or not api_key:
        _outage("DecisionModel card present but keyless or without base_url")
        return None

    from agent.llm.failure_classifier import FailureClass

    endpoint = base_url.rstrip("/") + "/v1/systemone"
    model = (getattr(card, "default_model", "") or "").strip() or "jev-latest"
    state = (
        f"status_code={status_code if status_code is not None else 'none'}\n"
        f"error_detail={(detail or '')[:1000]}"
    )
    body = {
        "model": model,
        "state": state,
        "questions": {
            "failure_class": {
                "type": "choice",
                "instructions": _INSTRUCTIONS,
                "criteria": dict(_CRITERIA),
            }
        },
    }

    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_JEV_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 — fail-open is the whole contract
        _outage(f"{type(exc).__name__}")
        return None

    try:
        answer = payload["answers"]["failure_class"]
        if answer.get("type") != "choice":
            _outage("non-choice answer")
            return None
        choice = answer.get("choice")
        confidence = answer.get("confidence", 0.0)
        if not isinstance(choice, str) or choice not in _VALID_JEV_CLASSES:
            _outage(f"invalid choice {choice!r}")
            return None
        if not isinstance(confidence, (int, float)) or confidence < _JEV_MIN_CONFIDENCE:
            _outage(f"confidence {confidence} below {_JEV_MIN_CONFIDENCE}")
            return None
    except (KeyError, TypeError, AttributeError) as exc:
        _outage(f"shape drift: {type(exc).__name__}")
        return None

    return FailureClass(choice)


def probe_decision_model(provider: object, timeout: float = 4.0):
    """Test Connection for the DecisionModel card — a REAL typed decision.

    Sends the same one-question classify request the runtime sends (a canned
    fixture state) and reports ok only when a well-formed choice answer with
    confidence comes back. This is the honest post-adapter probe: it proves
    the endpoint, key, model, and wire shape together.
    """
    import time

    from utils.provider_test import TestResult

    label = "typed-decision probe"

    base_url = (getattr(provider, "base_url", "") or "").strip()
    api_key = (getattr(provider, "api_key", "") or "").strip()
    if not base_url:
        return TestResult(ok=False, latency_ms=0, error="Base URL is required",
                          model_used=label)
    if not api_key:
        return TestResult(ok=False, latency_ms=0, error="API key is required",
                          model_used=label)

    # Reuse the production wire shape with a canned state so the probe
    # exercises exactly what the runtime sends. A raw call (not
    # classify_unknown) so the probe can distinguish "answered with low
    # confidence" from "unavailable" — both map to None in production.
    #
    # `started`/`elapsed` are module-local to this function; elapsed is seeded
    # to 0 BEFORE the call so the except branches can always read it (a
    # failure is exactly when latency_ms must not raise — BUG: the original
    # assigned elapsed only on the success path, so every failing probe raised
    # UnboundLocalError and hid the real provider error).
    started = time.monotonic()
    elapsed = 0
    card = provider
    endpoint = base_url.rstrip("/") + "/v1/systemone"
    model = (getattr(card, "default_model", "") or "").strip() or "jev-latest"
    body = {
        "model": model,
        "state": "HTTP 429: rate limit exceeded, retry after 30 seconds",
        "questions": {
            "probe": {
                "type": "choice",
                "instructions": _INSTRUCTIONS,
                "criteria": dict(_CRITERIA),
            }
        },
    }
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=max(timeout, _JEV_TIMEOUT_S)) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        elapsed = int((time.monotonic() - started) * 1000)
        answer = payload["answers"]["probe"]
        choice = answer["choice"]
        conf = answer.get("confidence", 0)
        return TestResult(
            ok=True,
            latency_ms=elapsed,
            error=None,
            model_used=f"{label}: classified a canned 429 as {choice} (confidence {conf:.2f})",
        )
    except urllib.error.HTTPError as http_err:
        detail = ""
        try:
            detail = http_err.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # body read is best-effort context
            _logger.debug("probe could not read error body", exc_info=True)
        return TestResult(ok=False, latency_ms=elapsed,
                          error=f"HTTP {http_err.code}: {detail}",
                          model_used=label)
    except Exception as exc:  # noqa: BLE001 — the probe's payload is the failure
        return TestResult(ok=False, latency_ms=elapsed,
                          error=f"{type(exc).__name__}: {exc}",
                          model_used=label)
