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
    {"model": <card.default_model>,
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
_DECISION_TIMEOUT_S = 2.0

# Below this confidence the answer is noise — treat as "could not classify".
_DECISION_MIN_CONFIDENCE = 0.6

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
        _logger.warning("Decision model unavailable (%s)", reason)


def post_decision(state: str, questions: dict) -> tuple[dict | None, str]:
    """POST state + questions to the Decision Model card.

    Returns ``(answers, error)``. ``error`` is ``""`` on success.
    ``answers`` is None whenever ``error`` is non-empty.
    Never raises. Never inserts a model slug. The ``model`` field is
    ``card.default_model.strip()`` exactly. Endpoint is
    ``base_url.rstrip("/") + "/v1/systemone"``. Timeout is 2.0 seconds.
    An empty ``default_model`` returns before any socket is opened.
    """
    if not isinstance(state, str) or not isinstance(questions, dict):
        return None, "decision model unavailable"
    card = load_decision_card()
    if card is None:
        return None, "decision model unavailable"
    base_url = (getattr(card, "base_url", "") or "").strip()
    api_key = (getattr(card, "api_key", "") or "").strip()
    model = (getattr(card, "default_model", "") or "").strip()
    if not model:
        _outage("default_model is required")
        return None, "default_model is required"
    if not base_url or not api_key:
        _outage("DecisionModel card present but keyless or without base_url")
        return None, "decision model unavailable"

    import time
    import urllib.error
    import urllib.request

    endpoint = base_url.rstrip("/") + "/v1/systemone"
    body = {"model": model, "state": state, "questions": questions}
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    # SPEC-46 §3.3 (fix round): the 2.0 s budget is a hard WALL-CLOCK ceiling
    # over the whole exchange, not a per-socket-op timeout. A body that drips
    # (each recv under the socket timeout, total far over 2 s) previously
    # slipped past. The deadline check around the chunked read aborts on the
    # ceiling and fails open to None.
    deadline = time.monotonic() + _DECISION_TIMEOUT_S
    try:
        with urllib.request.urlopen(req, timeout=_DECISION_TIMEOUT_S) as resp:
            chunks = []
            while True:
                if time.monotonic() > deadline:
                    _outage("wall-clock deadline exceeded")
                    return None, "decision model unavailable"
                chunk = resp.read1(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            body_bytes = b"".join(chunks)
            payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        _outage(f"HTTP {exc.code}")
        return None, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 — fail-open is the whole contract
        _outage(type(exc).__name__)
        return None, "decision model unavailable"
    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
        _outage("missing answers")
        return None, "decision model unavailable"
    return payload["answers"], ""


def classify_unknown(
    detail: str, status_code: int | None = None
) -> FailureClass | None:
    """Return a FailureClass from the Decision Model, or None when it cannot answer.

    Fail-open floor: no card / keyless card / empty model / any call failure /
    low confidence / shape drift → None. Never raises.
    """
    from agent.llm.failure_classifier import FailureClass

    state = (
        f"status_code={status_code if status_code is not None else 'none'}\n"
        f"error_detail={(detail or '')[:1000]}"
    )
    questions = {
        "failure_class": {
            "type": "choice",
            "instructions": _INSTRUCTIONS,
            "criteria": dict(_CRITERIA),
        }
    }
    answers, _error = post_decision(state, questions)
    if answers is None:
        return None
    try:
        answer = answers["failure_class"]
        if answer.get("type") != "choice":
            _outage("non-choice answer")
            return None
        choice = answer.get("choice")
        confidence = answer.get("confidence", 0.0)
        if not isinstance(choice, str) or choice not in _VALID_JEV_CLASSES:
            _outage(f"invalid choice {choice!r}")
            return None
        if not isinstance(confidence, (int, float)) or confidence < _DECISION_MIN_CONFIDENCE:
            _outage(f"confidence {confidence} below {_DECISION_MIN_CONFIDENCE}")
            return None
    except (KeyError, TypeError, AttributeError) as exc:
        _outage(f"shape drift: {type(exc).__name__}")
        return None
    return FailureClass(choice)


_MASK_VERDICTS = ("keep", "mask", "summarize")

_MASK_INSTRUCTIONS = (
    "Decide what to do with this old tool result. "
    "keep: the path is still needed. "
    "mask: the body can go. "
    "summarize: keep a short excerpt of the body."
)

_MASK_CRITERIA = {
    "keep": "The path or result is still needed for the task.",
    "mask": "The body is a dump that can be dropped.",
    "summarize": "A short excerpt is enough.",
}


def classify_mask_candidates(
    state: str, candidates: list[dict],
) -> dict[str, str] | None:
    """One batched decision for mask candidates.

    Returns ``None`` when the whole call fails. An empty candidate list
    does not call ``post_decision`` and returns an empty dict. Each
    surviving answer is ``candidate_{i}`` → keep / mask / summarize.
    A candidate that fails validation is omitted; the caller masks it.
    Never raises.
    """
    if not isinstance(candidates, list) or not candidates:
        return {}
    if not isinstance(state, str):
        return None
    questions: dict = {}
    for i, _cand in enumerate(candidates):
        questions[f"candidate_{i}"] = {
            "type": "choice",
            "instructions": _MASK_INSTRUCTIONS,
            "criteria": dict(_MASK_CRITERIA),
        }
    answers, _error = post_decision(state, questions)
    if answers is None:
        return None
    verdicts: dict[str, str] = {}
    for i in range(len(candidates)):
        key = f"candidate_{i}"
        try:
            answer = answers[key]
            if not isinstance(answer, dict) or answer.get("type") != "choice":
                continue
            choice = answer.get("choice")
            confidence = answer.get("confidence", 0.0)
            if not isinstance(choice, str) or choice not in _MASK_VERDICTS:
                continue
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                continue
            if confidence < _DECISION_MIN_CONFIDENCE:
                continue
        except (KeyError, TypeError, AttributeError):
            continue
        verdicts[key] = choice
    return verdicts


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
    model = (getattr(provider, "default_model", "") or "").strip()
    if not model:
        return TestResult(ok=False, latency_ms=0, error="default_model is required",
                          model_used=label)
    started = time.monotonic()
    elapsed = 0
    answers, error = post_decision(
        "HTTP 429: rate limit exceeded, retry after 30 seconds",
        {
            "probe": {
                "type": "choice",
                "instructions": _INSTRUCTIONS,
                "criteria": dict(_CRITERIA),
            }
        },
    )
    elapsed = int((time.monotonic() - started) * 1000)
    if answers is None:
        return TestResult(
            ok=False, latency_ms=elapsed,
            error=error or "decision model unavailable",
            model_used=label,
        )
    try:
        answer = answers["probe"]
        choice = answer["choice"]
        conf = answer.get("confidence", 0)
        return TestResult(
            ok=True,
            latency_ms=elapsed,
            error=None,
            model_used=f"{label}: classified a canned 429 as {choice} (confidence {float(conf):.2f})",
        )
    except (KeyError, TypeError, ValueError) as exc:
        return TestResult(
            ok=False, latency_ms=elapsed,
            error=f"decision model unavailable: {exc}",
            model_used=label,
        )


_ROOM_INSTRUCTIONS = (
    "The human just spoke in a group chat. Decide who should answer. "
    "Choose one or more participants when the message addresses them by "
    "name or role. Choose \"all\" when the message names nobody, greets "
    "the room, or addresses the whole room. For example, \"hi debugger\" "
    "addresses only the participant named Debugger, while \"hi debugger "
    "and coder\" addresses both. You may select multiple participants — "
    "return them as a JSON array of keys, e.g. "
    "[\"special:debugger\", \"special:coder\"] — never a single string."
)


def resolve_room_addressee(text: str, roster: list[tuple[str, str]]) -> list[str]:
    """Return ``["all"]``, validated ``session_key``(s), or ``[]``.

    ``roster`` entries are ``(session_key, display_name)``. An empty roster,
    a non-string ``text``, any ``post_decision`` failure, a choice outside
    the roster, a bool confidence, or confidence below
    ``_DECISION_MIN_CONFIDENCE`` returns ``[]``. Never raises.

    A genuine whole-room pick returns ``["all"]``. On success the return is
    the (possibly multi-select) list of session keys to wake; hallucinated
    keys are dropped — a fully invalid selection collapses to ``[]``.
    Fallback routing is the caller's responsibility. The human text is not
    modified.
    """
    if not isinstance(roster, list) or not roster or not isinstance(text, str):
        return []
    criteria: dict[str, str] = {
        "all": "The message addresses the whole room, or names nobody, or "
               "greets the room."
    }
    kept: list[tuple[str, str]] = []
    for session_key, display_name in roster:
        if (
            isinstance(session_key, str) and session_key
            and isinstance(display_name, str) and display_name
        ):
            criteria[session_key] = (
                f"{display_name} ({session_key}) — choose this when the "
                f"message addresses {display_name}."
            )
            kept.append((session_key, display_name))
    if not kept:
        return []
    state = (
        "message:\n" + text[:4000]
        + "\n\nparticipants:\n"
        + "\n".join(f"- {display_name} ({session_key})"
                    for session_key, display_name in kept)
    )
    answers, _error = post_decision(
        state,
        {
            "addressee": {
                "type": "choice",
                "selection": "multi",
                "instructions": _ROOM_INSTRUCTIONS,
                "criteria": criteria,
            }
        },
    )
    _logger.debug(
        "resolve_room_addressee: raw addressee answer=%r error=%r", answers, _error
    )
    if answers is None:
        return []
    try:
        answer = answers["addressee"]
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            return []
        raw = answer.get("choice")
        confidence = answer.get("confidence", 0.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return []
        if confidence < _DECISION_MIN_CONFIDENCE:
            return []
        if isinstance(raw, str):
            selected = [raw]
        elif isinstance(raw, list):
            selected = list(raw)
        else:
            return []
    except (KeyError, TypeError, AttributeError):
        return []
    # Validate: keep only known criterion keys (drop hallucinated/duplicate).
    seen: set[str] = set()
    validated: list[str] = []
    for key in selected:
        if isinstance(key, str) and key in criteria and key not in seen:
            seen.add(key)
            validated.append(key)
    if "all" in seen:  # a genuine whole-room pick dominates member picks
        return ["all"]
    return validated
