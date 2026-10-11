"""SPEC-22 DecisionModel seam — the card lookup for JEV (SPEC-22 §2.8).

The card named `DECISION_MODEL_PROVIDER` lives in the ordinary
`providers.yaml` store, reached through this hard-coded name. Every lookup
uses the constant; nothing hardcodes the endpoint, key, or model slug.

`classify_unknown` is the sole runtime entry. It resolves the card lazily
(NEVER at module import — importing providers.yaml at import time would make
this module a config dependency, and the enumerated policy rows must never
touch it). With no card, or a keyless card, it returns None — the
deterministic floor is load-bearing (SPEC-22 §2.5/§2.8).

Wiring the REAL typed-decision call is Stage-2 adapter work (`registry.py`
`typesafe` caller). Until that adapter exists the interactive return stays
None even when a card is present; the honest pre-adapter probe below gives
the Settings page a truthful reachability signal without faking success.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.llm.failure_classifier import FailureClass

_logger = logging.getLogger(__name__)

DECISION_MODEL_PROVIDER = "DecisionModel"


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


def classify_unknown(
    detail: str, status_code: int | None = None
) -> FailureClass | None:
    """Return a FailureClass from JEV, or None when JEV is not configured.

    The deterministic floor: no card, or a keyless card, → None (the caller
    uses its heuristic). A present+keyed card WOULD route here once the
    Stage-2 `typesafe` adapter exists; the adapter shape is not yet pinned,
    so the interactive call stays None and this returns None — documented,
    never faked. `detail`/`status_code` are the future request body.
    """
    del detail, status_code
    card = load_decision_card()
    if card is None or not (getattr(card, "api_key", "") or "").strip():
        return None
    # Card present + keyed. Stage-2 adapter not shipped: keep the None stub.
    return None


def probe_decision_model(provider: object, timeout: float = 4.0):
    """Honest PRE-ADAPTER probe for the DecisionModel Test Connection button.

    There is no typed-decision adapter yet, so this must NOT claim the model
    works. It performs a base_url reachability check plus key presence and
    labels itself as such. Returns a `TestResult` (utils.provider_test) with
    `model_used` carrying the probe label so the UI can show it verbatim.
    """
    import time
    import urllib.error
    import urllib.request

    from utils.provider_test import TestResult

    base_url = (getattr(provider, "base_url", "") or "").strip()
    api_key = (getattr(provider, "api_key", "") or "").strip()
    label = "pre-adapter probe (reachability only — typed-decision call not yet implemented)"

    if not base_url:
        return TestResult(ok=False, latency_ms=0, error="Base URL is required",
                          model_used=label)
    if not api_key:
        return TestResult(ok=False, latency_ms=0, error="API key is required",
                          model_used=label)

    req = urllib.request.Request(base_url, headers={"Authorization": f"Bearer {api_key}"})
    started = time.monotonic()
    try:
        urllib.request.urlopen(req, timeout=timeout)
        elapsed = int((time.monotonic() - started) * 1000)
        return TestResult(ok=True, latency_ms=elapsed, error=None, model_used=label)
    except urllib.error.HTTPError as http_err:
        elapsed = int((time.monotonic() - started) * 1000)
        if http_err.code in (401, 403):
            return TestResult(ok=False, latency_ms=0,
                              error=f"reachable, but key rejected (HTTP {http_err.code})",
                              model_used=label)
        # Any other HTTP status still proves the endpoint answered.
        return TestResult(ok=True, latency_ms=elapsed, error=None, model_used=label)
    except Exception as exc:  # noqa: BLE001 — network failures are the probe's payload
        return TestResult(ok=False, latency_ms=0,
                          error=f"unreachable: {type(exc).__name__}", model_used=label)