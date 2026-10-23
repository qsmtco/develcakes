"""Conversation persistence — one JSON file per session.

Security:
  - HIGH-3: api_key is NEVER serialized. Re-resolved from providers.yaml on load.
  - LOW-2: session workspace validation prevents path escapes.
  - Conversation files are mode 0600.

Pure Python — no GTK, no network, no agent.runtime imports.
"""

import json
import logging
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from models.conversation import Conversation, Message


def conversations_dir() -> str:
    """Return the conversations directory, creating it if needed.

    HIGH-3: the directory is chmod 0o700 (owner only) when first created.
    Each conversation file is created mode 0o600.
    """
    from utils.config import get_config_dir
    d = os.path.join(get_config_dir(), "conversations")
    parent_existed = os.path.isdir(d)
    os.makedirs(d, exist_ok=True)
    if not parent_existed:
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return d


def save_conversation_to_disk(conv: "Conversation", session_key: str) -> str:
    """Save a conversation to <conversations_dir>/<session_key>.json.

    Writes a temp file in the same directory, fsyncs it, then os.replace
    onto the live path. A crash before replace leaves the previous file.

    HIGH-3: api_key is NOT serialized. It is re-resolved on load from
    providers.yaml keyed by conv.model / conv.provider.
    """
    path = os.path.join(conversations_dir(), f"{session_key}.json")
    # F3 (SPEC-48 Phase 3 fix): null-safe AND type-safe — a plain dict (or
    # missing attribute) must not crash the save; write None in that case.
    from models.conversation import ConversationDigest
    _dg = getattr(conv, "digest", None)
    data = {
        "session_key": session_key,
        "agent_name": conv.agent_name,
        "project_path": conv.project_path,
        "model": conv.model,
        "provider": getattr(conv, "provider", None),
        "messages": [
            {
                "role": m.role.value if hasattr(m.role, "value") else str(m.role),
                "content": m.content,
                "tool_calls": [
                    {
                        "call_id": tc.call_id,
                        "tool_name": tc.tool_name,
                        "arguments": tc.arguments,
                    }
                    for tc in (m.tool_calls or [])
                ],
                "tool_call_id": getattr(m, "tool_call_id", None),
                "tokens_used": m.tokens_used,
                "timestamp": m.timestamp.isoformat() if hasattr(m.timestamp, "isoformat") else m.timestamp,
            }
            for m in conv.messages
        ],
        "system_prompt": conv.system_prompt,
        "task_card": conv.task_card if isinstance(getattr(conv, "task_card", ""), str) else "",
        "total_tokens": conv.total_tokens,
        "total_cost": conv.total_cost,
        "step_count": conv.step_count,
        "allowed_tools": conv.allowed_tools,
        "digest": _dg.to_dict() if isinstance(_dg, ConversationDigest) else None,
        "mcp_servers": list(conv.mcp_servers) if conv.mcp_servers else [],
        "si_enforcement": conv.si_enforcement,
        "agent_role": conv.agent_role,
        "fallback_provider": conv.fallback_provider,
        "fallback_model": conv.fallback_model,
        "app_title": conv.app_title,
        "created_at": conv.created_at.isoformat() if hasattr(conv.created_at, "isoformat") else conv.created_at,
    }
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def _naive_now() -> "datetime":
    """Naive datetime.now() — Message.timestamp's storage convention.

    The JSON body persists naive isoformat() strings (Message's
    default_factory is naive). An aware now() would mix tz states within
    one conversation.
    """
    return datetime.now()  # noqa: DTZ005 — naive, matching Message.timestamp


def _message_from_data(mdata: dict) -> "Message":
    """Saved message dict -> Message."""
    from models.conversation import Message, MessageRole, ToolCall
    tool_calls = []
    for tcdata in mdata.get("tool_calls") or []:
        tool_calls.append(
            ToolCall(
                call_id=tcdata["call_id"],
                tool_name=tcdata["tool_name"],
                arguments=tcdata.get("arguments", {}),
            )
        )
    raw_ts = mdata.get("timestamp")
    try:
        ts = (
            datetime.fromisoformat(raw_ts)
            if isinstance(raw_ts, str)
            else _naive_now()
        )
    except ValueError:
        ts = _naive_now()
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    return Message(
        role=MessageRole(mdata["role"]),
        content=mdata.get("content", ""),
        tool_calls=tool_calls,
        tool_call_id=mdata.get("tool_call_id"),
        timestamp=ts,
        tokens_used=mdata.get("tokens_used", 0) or 0,
    )


def _conversation_from_data(data: dict) -> "Conversation":
    """Saved conversation dict -> Conversation.

    project_path and system_prompt are not trusted from disk. The runtime
    rebuilds them against the active project on the next send.
    """
    from models.conversation import Conversation, ConversationDigest
    messages = [_message_from_data(m) for m in data.get("messages", [])]
    digest = ConversationDigest.from_dict(data.get("digest") or {})
    return Conversation(
        agent_name=data["agent_name"],
        project_path=None,
        model=data.get("model", ""),
        provider=data.get("provider"),
        system_prompt="",
        task_card=data.get("task_card", "") if isinstance(data.get("task_card", ""), str) else "",
        messages=messages,
        total_tokens=data.get("total_tokens", 0),
        total_cost=data.get("total_cost", 0.0),
        step_count=data.get("step_count", 0),
        digest=digest if any(digest.to_dict().values()) else None,
        allowed_tools=data.get("allowed_tools"),
        app_title=data.get("app_title", ""),
        mcp_servers=data.get("mcp_servers", []),
        si_enforcement=data.get("si_enforcement"),
        agent_role=data.get("agent_role", ""),
        fallback_provider=data.get("fallback_provider"),
        fallback_model=data.get("fallback_model"),
    )


def _apply_loaded_fallbacks(
    conv: "Conversation", session_key: str, data: dict
) -> None:
    """Re-resolve api_key and fill allowed_tools from the live agent def."""
    conv.api_key = resolve_api_key_for_conversation(data)
    # Always refresh allowed_tools from the live agent def when one exists.
    # The saved list is a creation-time cache; without this refresh a tool
    # added to the agent yaml (e.g. set_timer/cancel_timer in SPEC-52) can
    # never reach an existing conversation — the enforcement gate
    # (agent/tools.py allowed_tools check) would reject it forever.
    try:
        from agent.special_agents import get_special_agent
        agent_def = get_special_agent(session_key)
        if agent_def is not None and agent_def.tools:
            conv.allowed_tools = list(agent_def.tools)
    except Exception:
        pass


def resolve_api_key_for_conversation(data: dict) -> str | None:
    """Resolve the api_key for a loaded conversation from providers.yaml.

    HIGH-3: never read api_key from the saved file. Re-resolve from the
    provider store keyed by `provider` (or the prefix of `model`).
    Returns None if no matching provider is configured.
    """
    try:
        from utils.providers_store import load_providers
        providers = load_providers()
        if not providers:
            return None
        provider_name = data.get("provider")
        if not provider_name:
            model = data.get("model", "")
            if model:
                # Provider card identity is not stored on pre-fix conversations —
                # prefer an exact default_model match over a prefix split, so
                # two cards sharing a prefix resolve to the RIGHT card (and its
                # API key).
                for p in providers:
                    if getattr(p, "default_model", "") == model:
                        return p.api_key
            if "/" in model:
                provider_name = model.split("/")[0]
        if not provider_name:
            return None
        for p in providers:
            if p.name == provider_name:
                return p.api_key
        return None
    except Exception:
        logger.exception("[persistence] failed to resolve api_key for conversation")
        return None


def load_conversation_from_disk(session_key: str) -> tuple["Conversation", dict] | None:
    """Load a conversation from disk. Returns (Conversation, metadata) or None.

    HIGH-3: api_key is re-resolved from providers.yaml. A saved api_key
    field, if present, is ignored.
    """
    path = os.path.join(conversations_dir(), f"{session_key}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    conv = _conversation_from_data(data)
    _apply_loaded_fallbacks(conv, session_key, data)
    return conv, data


def resolve_session_workspace(project_path: str | None, session_key: str) -> str:
    """Return a per-session secure workspace under the project's .crabcakes/ dir.

    LOW-2: never fall back to /tmp — raise if project_path is empty.
    The workspace dir is created with 0o700 permissions (owner-only).

    Args:
        project_path: The project directory (must not be empty).
        session_key: Must be non-empty, whitespace-free, and contain only
            [a-zA-Z0-9._:-]. Path separators and ".." are rejected.
            Colons (e.g. "agent:coder") are allowed and sanitized for
            filesystem safety.

    Returns:
        Absolute path to the session's scratch workspace directory.
    """
    if not project_path:
        raise ValueError(
            f"LOW-2: project_path is empty for session {session_key!r}; "
            "refusing to use a world-writable default"
        )
    if not session_key or not session_key.strip():
        raise ValueError(f"LOW-2: session_key must be non-empty and contain no whitespace: {session_key!r}")
    if ".." in session_key:
        raise ValueError(f"LOW-2: session_key must not contain '..': {session_key!r}")
    if not re.fullmatch(r"[a-zA-Z0-9._:-]+", session_key):
        raise ValueError(f"LOW-2: session_key must match [a-zA-Z0-9._:-]+, got: {session_key!r}")
    fs_safe_key = session_key.replace(":", "-")
    workspace = os.path.join(project_path, ".crabcakes", "tmp", fs_safe_key)
    os.makedirs(workspace, mode=0o700, exist_ok=True)
    return workspace
