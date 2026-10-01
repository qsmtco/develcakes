"""Conversation persistence — disk I/O for conversation state.

Extracted from agent/runtime.py (Phase 6). Stateless module-level helpers
for saving/loading conversations to ~/.config/crabcakes/conversations/.

Security:
  - HIGH-3: api_key is NEVER serialized. Re-resolved from providers.yaml on load.
  - LOW-2: session workspace validation prevents path escapes.
  - Conversation files are chmod 0600 after write.

Pure Python — no GTK, no network, no agent.runtime imports.
"""

import json
import logging
import os
import re
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from models.conversation import Conversation
    from utils.transcript_store import TranscriptStore


# ── Transcript store access (SPEC-08 SP2, D3 dual-write) ─────────────────────

# Test seam: when set, _get_store() returns this instance instead of creating
# the process-wide singleton. Set by tests/conftest.py's autouse fixture (every
# test gets an isolated tmp_path store) and by tests that need a fake (e.g.
# raising-store fallback). Production code never touches it.
_store_override: "TranscriptStore | None" = None
_store_singleton: "TranscriptStore | None" = None


def _get_store() -> "TranscriptStore":
    """Return the process-wide TranscriptStore (global per install, D1=(c)).

    Lazy call-time import: persistence.py is imported before tests patch
    utils.config.get_config_dir, so the store (which resolves its default DB
    path at construction) must not be imported or built at module top.

    D4 epoch note: this release NEVER calls bump_epoch — the wrapper has no
    /clear trigger (no runtime delete API exists pre-group-chat). SP3+ must
    not reinvent epoch bumping here; delete_session/bump_epoch stay
    store-level surfaces (exercised by store tests, not by this wrapper).
    """
    global _store_singleton
    if _store_override is not None:
        return _store_override
    if _store_singleton is None:
        from utils.transcript_store import TranscriptStore

        _store_singleton = TranscriptStore()
    return _store_singleton


# ── Conversation persistence ──────────────────────────────────────────────────

def conversations_dir() -> str:
    """Return the conversations directory, creating it if needed.

    HIGH-3: parent dir is chmod 0o700 (owner only). Each conversation file
    is chmod 0o600 after write (in save_conversation_to_disk).
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

    HIGH-3: api_key is NOT serialized. The api_key is re-resolved on load
    from providers.yaml (atomic+0600) keyed by conv.model/conv.provider.
    Conversation files should never contain raw secrets.
    """
    path = os.path.join(conversations_dir(), f"{session_key}.json")
    data = {
        "session_key": session_key,
        "agent_name": conv.agent_name,
        "project_path": conv.project_path,
        "model": conv.model,
        "provider": getattr(conv, "provider", None),  # HIGH-3: for api_key re-resolution on load
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
        "total_tokens": conv.total_tokens,
        "total_cost": conv.total_cost,
        "step_count": conv.step_count,
        "allowed_tools": conv.allowed_tools,
        # HIGH-3: api_key NOT serialized — re-resolved from providers.yaml on load
        "mcp_servers": list(conv.mcp_servers) if conv.mcp_servers else [],
        "si_enforcement": conv.si_enforcement,
        "agent_role": conv.agent_role,
        "fallback_provider": conv.fallback_provider,
        "fallback_model": conv.fallback_model,
        "app_title": conv.app_title,
        "created_at": conv.created_at.isoformat() if hasattr(conv.created_at, "isoformat") else conv.created_at,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    # HIGH-3: chmod 0600 after write — conversation files contain model/provider
    # but must NOT contain raw api_key.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # non-POSIX filesystem
    # D3 dual-write (SPEC-08 SP2): the JSON file above stays authoritative for
    # one release; the store gets the DELTA of new messages. The delta compare
    # is INDEX/watermark-based ONLY — never value-based (an audit register
    # flagged that a value compare would diff-loop on tool_calls [] vs None).
    # Explicit seq = message index keeps store seqs aligned with JSON order
    # (and UNIQUE(session_key, epoch, seq) makes a duplicate an IntegrityError,
    # never a silent double-append). Store failure never breaks the save: JSON
    # is already on disk — log and return the JSON path (D3 fallback contract).
    #
    # SP2 fix round (BUG#4) — the delta append is ONE atomic append_delta call:
    # one lock + one BEGIN IMMEDIATE, with the watermark RE-READ INSIDE the tx
    # (BUG#2/#3 ruling: wm is "appended through"). The `session_watermark()` read
    # inside _append_conversation_delta is an OUTSIDE-the-lock pre-trim
    # optimization only — correctness is owned by the in-tx re-read, so a stale
    # wm there merely over-trims. When it under-trims (the JSON-only restart:
    # wm=-1 in the store, JSON has 3), append_delta writes the full JSON history
    # back — that IS D3's gradual self-migration (SP3's batched backfill
    # converges to the same rows).
    try:
        _append_conversation_delta(conv, session_key)
    except Exception:
        logger.warning(
            "[persistence] transcript store append failed for %s — JSON fallback "
            "is intact at %s (SP3 migration will backfill)",
            session_key,
            path,
            exc_info=True,
        )
    return path


def _same_turn(row: dict, msg) -> bool:
    """Store row vs Message identity probe for the append-only guard (SP2
    fix round 3): role + content compared with the delta shaper's exact
    normalization. Any trim in [0..wm] shifts messages[wm] off the boundary
    row; appends above wm never do."""
    row_role = str(row.get("role", ""))
    msg_role = msg.role.value if hasattr(msg.role, "value") else str(msg.role)
    return row_role == msg_role and row.get("content") == msg.content


def _shape_message(m) -> dict:
    """Message -> the store's persistence row shape (HIGH-3: NO api_key).

    The tool_calls shape matches the JSON body: call_id/tool_name/arguments.
    `m.tool_calls or []` keeps the empty-tool_calls → None conflation explicit
    (an audit register: the delta compare must stay seq-based, not value-based
    — [] and None both persist as NULL in the store).
    """
    return {
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
    }


def _append_conversation_delta(conv: "Conversation", session_key: str) -> None:
    """Append every message past the store's watermark (index-based delta).

    Guarded by the append-only check (SP2 fix rounds 2–3): context
    compaction FRONT-TRIMS conv.messages, which shifts every index — an
    index-based delta would then mis-attribute and silently drop turns. On
    any shape violation (empty list, list shorter than the store's tail,
    or the dual anchors — seq-wm boundary row and seq-0 — no longer
    matching), the session goes diverged-FLAGGED and
    the store delta is SUSPENDED: JSON stays authoritative (working file), the
    store's existing rows are retained as the append-only audit ledger and
    NEVER rebuilt (rebuilding would delete the only surviving copies of the
    trimmed turns; re-sync is post-MVP stable-id work).

    The `session_watermark()` read is outside the lock — a best-effort pre-trim
    only; the in-tx wm re-read owns correctness. The warning fires ONLY on the
    flag TRANSITION (is_diverged short-circuits afterwards — no warn spam).
    Raises on store failure — save_conversation_to_disk is the caller and
    owns the fallback contract. Never touches api_key (HIGH-3): the
    persistence shape carries only role/content/tool_calls/tool_call_id/
    tokens_used, exactly what the JSON body writes.

    SP2 fix round 3 — dual-anchor guard: (1) emptiness handled FIRST (BUG#2:
    the old guard indexed messages[0] before the emptiness clause, so a full
    clear IndexErrored into the generic fallback and never flagged); (2) the
    length floor `len >= wm+1`; (3) anchors on the LAST COMMITTED row
    (row_at(seq=wm) vs messages[wm]) plus the seq-0 front anchor (BUG#1:
    the round-2 seq-0-only guard was bypassed by middle trims — the real
    DefaultContextStrategy().compact() keep_first=2 preserves index 0 while
    shifting every higher index). Content-based anchor; in-place content
    stubbing at the anchors is a known false-positive (prune_tool_outputs) —
    stable-id keying is the post-MVP fix. Known false-NEGATIVE (re-audit
    residual, registered): a middle trim whose shifted messages[wm]
    coincidentally reproduces the old boundary CONTENT (repeated content —
    e.g. identical prune stubs) slips both anchors. Full-prefix anchoring
    closes it at O(n)-per-save cost; stable-ids close it properly. Post-MVP.
    """
    store = _get_store()
    wm = store.session_watermark(session_key)
    if wm >= 0:
        # BUG#2 ordering: emptiness FIRST — indexing messages[0]/[wm] on a
        # fully-cleared conversation must flag divergence, not IndexError.
        if not conv.messages:
            if not store.is_diverged(session_key):
                store.mark_diverged(session_key)
                logger.warning(
                    "[persistence] %s: conversation history cleared — store "
                    "delta suspended; session stays JSON-backed. Existing "
                    "store rows retained as audit ledger.",
                    session_key,
                )
            return
        if len(conv.messages) >= wm + 1:
            last_committed = store.row_at(session_key, seq=wm)
            boundary = conv.messages[wm]
            anchor0 = store.row_at(session_key, seq=0)
            append_only_ok = (
                last_committed is not None
                and anchor0 is not None
                and _same_turn(last_committed, boundary)
                and _same_turn(anchor0, conv.messages[0])
            )
        else:
            # Non-empty but shorter than wm+1: a front-trim shrink is also
            # divergence — flag + return, same path as the anchor mismatch.
            append_only_ok = False
        if not append_only_ok:
            if not store.is_diverged(session_key):
                store.mark_diverged(session_key)
                logger.warning(
                    "[persistence] %s: conversation history changed shape "
                    "(context compaction?) — store delta suspended; session "
                    "stays JSON-backed (post-MVP stable-id work will re-sync). "
                    "Existing store rows retained as audit ledger.",
                    session_key,
                )
            return
    # The 0-clamp prevents a negative seq on a corrupt wm (< -1) but does NOT
    # heal it — the misaligned append IntegrityErrors into the JSON fallback;
    # the wm==max-row invariant pin is the real defense.
    base_idx = max(wm + 1, 0)
    shaped_tail = [_shape_message(m) for m in conv.messages[base_idx:]]
    store.append_delta(session_key, base_idx, shaped_tail)


def resolve_api_key_for_conversation(data: dict) -> str | None:
    """Resolve the api_key for a loaded conversation from providers.yaml.

    HIGH-3: never read api_key from the saved file. Re-resolve from the
    provider store keyed by `provider` field (or extracted from `model`).
    Returns None if no matching provider is configured.

    Args:
        data: The raw JSON data dict loaded from the conversation file.
              Expected to have a "model" key (e.g., "openai/gpt-4o") and
              optionally a "provider" key.
    """
    try:
        from utils.providers_store import load_providers
        providers = load_providers()
        if not providers:
            return None
        # Prefer explicit provider field
        provider_name = data.get("provider")
        if not provider_name:
            model = data.get("model", "")
            if "/" in model:
                provider_name = model.split("/")[0]
        if not provider_name:
            return None
        # Look up matching provider
        for p in providers:
            if p.name == provider_name:
                return p.api_key
        return None
    except Exception:
        logger.exception("[persistence] failed to resolve api_key for conversation")
        return None


def load_conversation_from_disk(session_key: str) -> tuple["Conversation", dict] | None:
    """Load a conversation from disk. Returns (Conversation, metadata) or None.

    HIGH-3: api_key is re-resolved from providers.yaml (atomic+0600) keyed
    by conv.model. Saved api_key in old files is ignored (and stripped
    on next save by the one-time migration).
    """
    path = os.path.join(conversations_dir(), f"{session_key}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    from models.conversation import Conversation, Message, MessageRole

    messages = []
    for mdata in data.get("messages", []):
        from models.conversation import ToolCall
        tool_calls = []
        for tcdata in mdata.get("tool_calls", []):
            tool_calls.append(
                ToolCall(
                    call_id=tcdata["call_id"],
                    tool_name=tcdata["tool_name"],
                    arguments=tcdata.get("arguments", {}),
                )
            )
        msg = Message(
            role=MessageRole(mdata["role"]),
            content=mdata.get("content", ""),
            tool_calls=tool_calls,
            tool_call_id=mdata.get("tool_call_id"),
            tokens_used=mdata.get("tokens_used", 0),
        )
        messages.append(msg)

    # HIGH-3: re-resolve api_key from providers.yaml, NOT from saved data
    api_key = resolve_api_key_for_conversation(data)

    conv = Conversation(
        agent_name=data["agent_name"],
        # Option C+: project_path and system_prompt are NOT loaded from disk.
        # The persisted values may be stale (from a previous project the user
        # had open). They are re-applied by _rebuild_conversation_context
        # on first send, against the currently-active project. The persisted
        # values are still written on save so a manual audit can read them
        # back, but the runtime never trusts them.
        project_path=None,
        model=data.get("model", ""),
        provider=data.get("provider"),  # HIGH-3: stored so we can re-resolve api_key
        system_prompt="",
        messages=messages,
        total_tokens=data.get("total_tokens", 0),
        total_cost=data.get("total_cost", 0.0),
        step_count=data.get("step_count", 0),
        allowed_tools=data.get("allowed_tools"),
        api_key=api_key,  # HIGH-3: re-resolved from providers.yaml
        app_title=data.get("app_title", ""),
        mcp_servers=data.get("mcp_servers", []),
        si_enforcement=data.get("si_enforcement"),
        agent_role=data.get("agent_role", ""),
        fallback_provider=data.get("fallback_provider"),
        fallback_model=data.get("fallback_model"),
    )
    # allowed_tools fallback: if the persisted conversation has no
    # allowed_tools (pre-fix conversations or post-YAML-edit), fall back
    # to the live agent definition's tools list. Mirrors the HIGH-3
    # api_key re-resolution pattern: do not trust persisted state when
    # live config is available. Without this, the execute_tool gate is
    # a no-op for any conversation created before the gate shipped.
    if conv.allowed_tools is None:
        try:
            from agent.special_agents import get_special_agent
            agent_def = get_special_agent(session_key)
            if agent_def is not None and agent_def.tools:
                conv.allowed_tools = list(agent_def.tools)
        except Exception:
            pass  # Best-effort: leave None if lookup fails (gate skips)

    # D3 dual-write hydration DELETED (SP2 fix round, BUG#2/#3): the old sync
    # of the store watermark UP to len(messages)-1 violated the ruling — the
    # watermark is "appended through", never "acknowledged through". No store
    # interaction on load: load is pure JSON this release. The restart case
    # self-heals on the next save: with wm=-1, append_delta backfills ALL
    # JSON messages (D3's gradual self-migration).

    return conv, data


# ── HIGH-3: One-time migration ──────────────────────────────────────────────────

# Module-level flag — migration runs once per process
_CONVERSATION_MIGRATION_DONE: bool = False


def migrate_conversation_files() -> int:
    """One-time sweep: remove api_key from existing conversation files.

    HIGH-3: scans ~/.config/crabcakes/conversations/*.json, removes the
    "api_key" field if present, writes back atomically with chmod 0600.
    New saves never include api_key. Idempotent — safe to call multiple times.

    Returns the number of files migrated.
    """
    global _CONVERSATION_MIGRATION_DONE
    if _CONVERSATION_MIGRATION_DONE:
        return 0
    _CONVERSATION_MIGRATION_DONE = True

    d = conversations_dir()
    count = 0
    try:
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            path = os.path.join(d, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if "api_key" in data:
                    del data["api_key"]
                    # Atomic write
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
                    os.replace(tmp, path)
                    # Ensure 0600
                    try:
                        os.chmod(path, 0o600)
                    except OSError:
                        pass
                    count += 1
            except (OSError, json.JSONDecodeError):
                # Skip unreadable files — don't crash the migration
                continue
    except OSError:
        pass
    if count > 0:
        logger.info(
            "[persistence] HIGH-3 migration: removed api_key from %d conversation file(s)",
            count,
        )
    return count


# ── LOW-2: Per-session secure workspace ─────────────────────────────────────


def resolve_session_workspace(project_path: str | None, session_key: str) -> str:
    """Return a per-session secure workspace under the project's .crabcakes/ dir.

    LOW-2: never fall back to /tmp — raise if project_path is empty.
    The workspace dir is created with 0o700 permissions (owner-only).

    Args:
        project_path: The project directory (must not be empty).
        session_key: Must be non-empty, whitespace-free, and contain only
            [a-zA-Z0-9._:-]. Path separators and ".." are rejected.
            Colons (e.g. "special:coder") are allowed and sanitized for
            filesystem safety.

    Returns:
        Absolute path to the session's scratch workspace directory.
    """
    if not project_path:
        raise ValueError(
            f"LOW-2: project_path is empty for session {session_key!r}; "
            "refusing to use a world-writable default"
        )
    # session_key validation — prevent empty keys, path escapes, and traversal
    if not session_key or not session_key.strip():
        raise ValueError(f"LOW-2: session_key must be non-empty and contain no whitespace: {session_key!r}")
    if ".." in session_key:
        raise ValueError(f"LOW-2: session_key must not contain '..': {session_key!r}")
    if not re.fullmatch(r"[a-zA-Z0-9._:-]+", session_key):
        raise ValueError(f"LOW-2: session_key must match [a-zA-Z0-9._:-]+, got: {session_key!r}")
    # Sanitize colon for filesystem safety (e.g. "special:coder" → "special-coder")
    fs_safe_key = session_key.replace(":", "-")
    workspace = os.path.join(project_path, ".crabcakes", "tmp", fs_safe_key)
    os.makedirs(workspace, mode=0o700, exist_ok=True)
    return workspace