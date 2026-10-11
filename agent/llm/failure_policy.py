"""SPEC-22 Stage 2 — the failure policy table. Pure. No model call.

`attempt` is how many times the current provider has already failed this step
(1 = first classified failure). `failure.attempt` is the chain slot
(1 = primary, 2 = fallback) and is not re-read here; the caller passes `chain`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agent.llm.failure_classifier import FailureClass, ProviderFailure


@dataclass
class RetryConfig:
    provider_retries: int = 2
    backoff_base_s: float = 1.0
    provider_failover_budget_s: float = 30.0


@dataclass
class Decision:
    action: Literal["retry", "advance_chain", "halt"]
    provider: str
    backoff_s: float
    reason: str


def decide(
    failure: ProviderFailure,
    attempt: int,
    chain: list[str],
    cfg: RetryConfig | None = None,
) -> Decision:
    """Five rows from SPEC-22 §1. Enumerated classes never consult JEV."""
    if cfg is None:
        cfg = RetryConfig()
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        attempt = 1
    names = [name for name in (chain or []) if isinstance(name, str) and name.strip()]
    current = names[0] if names else (failure.provider or "")
    nxt = names[1] if len(names) > 1 else ""
    if nxt == current:
        nxt = ""
    kind = failure.failure_class

    if kind == FailureClass.BAD_REQUEST:
        return Decision(
            action="halt",
            provider=current,
            backoff_s=0.0,
            reason=f"malformed request on {current} — develcakes bug, no failover",
        )

    if kind in (FailureClass.PERMANENT_AUTH, FailureClass.PERMANENT_MODEL):
        if nxt:
            return Decision(
                action="advance_chain",
                provider=nxt,
                backoff_s=0.0,
                reason=f"{kind.value} on {current} (attempt {attempt}) → advance to {nxt}",
            )
        return Decision(
            action="halt",
            provider=current,
            backoff_s=0.0,
            reason=f"{kind.value} on {current} and no distinct fallback",
        )

    if kind == FailureClass.TRANSIENT:
        if attempt <= cfg.provider_retries:
            return Decision(
                action="retry",
                provider=current,
                backoff_s=cfg.backoff_base_s,
                reason=f"transient on {current} (attempt {attempt}) → retry",
            )
        if nxt:
            return Decision(
                action="advance_chain",
                provider=nxt,
                backoff_s=0.0,
                reason=f"transient retries exhausted on {current} → advance to {nxt}",
            )
        return Decision(
            action="halt",
            provider=current,
            backoff_s=0.0,
            reason=f"transient retries exhausted on {current} and no fallback",
        )

    # UNKNOWN, including an ambiguous 400. One same-provider retry, then halt.
    # Does not advance the chain (that would hide an unclassified payload bug).
    if attempt <= 1:
        return Decision(
            action="retry",
            provider=current,
            backoff_s=cfg.backoff_base_s,
            reason=f"unknown on {current} (attempt {attempt}) → single retry",
        )
    return Decision(
        action="halt",
        provider=current,
        backoff_s=0.0,
        reason=f"unknown on {current} (attempt {attempt}) → halt",
    )
