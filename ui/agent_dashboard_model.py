# ui/agent_dashboard_model.py — pure helpers for the agent dashboard (SPEC-AGENT-DASHBOARD §2.1)
from __future__ import annotations

__all__ = ["collapsed_summary", "local_member_keys"]


def local_member_keys(members: list[str], registered: dict[str, str]) -> list[str]:
    """Members that are registered local agents, in `members` order."""
    return [sk for sk in members if isinstance(sk, str) and sk in registered]


def collapsed_summary(rows: list[dict]) -> str:
    """One line for the drawer header.

    Each row needs `name` (str) and `usage_percent` (float or None).
    A missing or negative percent renders as an em dash.
    """
    if not rows:
        return "No agents"
    parts: list[str] = []
    for row in rows:
        name = row.get("name") or "Agent"
        pct = row.get("usage_percent")
        if (
            pct is None
            or isinstance(pct, bool)
            or not isinstance(pct, (int, float))
            or pct < 0
            or pct != pct  # noqa: PLR0124 — NaN check
        ):
            parts.append(f"{name} —")
        else:
            parts.append(f"{name} {pct:.0f}%")
    return " · ".join(parts)