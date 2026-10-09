"""Conversation persistence — disk I/O for conversation state.

Extracted from agent/runtime.py (Phase 6). Stateless module-level helpers
for saving/loading conversations to ~/.config/develcakes/conversations/.

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
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from models.conversation import Conversation, Message
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

    D4 epoch note (UPDATED by the /clear-store fix, 2026-10-08): the wrapper
    NOW exposes delete_session_rows() for /clear — the SP4A store-mode load
    made a JSON-only clear insufficient (the store re-hydrates cleared
    history when the JSON is absent). bump_epoch stays store-level
    (load_all reads ALL epochs, so a bump alone cannot empty a session).
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
    # SP4A: sessions-table metadata upsert — the store-mode loader reads
    # agent_name/model/provider back for the HIGH-3 api_key re-resolution
    # (and Conversation reconstruction) once the JSON file is renamed away.
    # Best-effort like every other store touch: JSON is already on disk.
    try:
        _get_store().set_session_meta(
            session_key,
            agent_name=conv.agent_name,
            model=conv.model,
            provider=getattr(conv, "provider", None),
        )
    except Exception:
        logger.warning(
            "[persistence] sessions-meta upsert failed for %s — store-mode "
            "load would rebuild with empty metadata (JSON fallback intact)",
            session_key,
            exc_info=True,
        )
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


def _naive_now() -> "datetime":
    """Naive datetime.now() — Message.timestamp's storage convention.

    DTZ005-anchored: the JSON body persists naive isoformat() strings
    (Message's default_factory is naive) — an aware now() would mix tz
    states within one conversation and skew isoformat round-trips. Parity
    with models/conversation.py's own baseline wins over local tz-correctness.
    """
    return datetime.now()  # noqa: DTZ005 — see docstring (baseline parity)


def _message_from_data(mdata: dict) -> "Message":
    """Saved message dict -> Message — THE deserialization shape (SP4A).

    Single definition shared by the JSON loader and the store fallback so
    the two paths cannot drift (the shape-equivalence test pins it): role,
    content, tool_calls (call_id/tool_name/arguments), tool_call_id,
    tokens_used, timestamp (fromisoformat when parseable, else now —
    the store's strftime('%f') format and the JSON isoformat both parse).
    """
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
        # BUG#2 (SP4A fix round): the store's _TS_NOW ends in 'Z' →
        # fromisoformat yields tz-AWARE, but Message.timestamp's convention
        # is NAIVE (models/conversation.py default_factory; the JSON path's
        # isoformat strings carry no offset). An aware row mixing into a
        # naive conversation breaks sort/arithmetic (TypeError) and the
        # aware form would leak into the JSON body on the next save.
        # Parse-side normalization: strip tzinfo, keep the wall-clock value
        # (covers legacy rows with no DB migration).
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
    """Saved conversation dict -> Conversation — the JSON body's shape.

    Extracted verbatim from load_conversation_from_disk (SP4A) so the store
    fallback rebuilds the EXACT same object shape: project_path/system_prompt
    forced stale-unsafe (Option C+: runtime rebuilds them), allowed_tools
    left None here — the live-def fallback below stays load-path-owned.
    """
    from models.conversation import Conversation
    messages = [_message_from_data(m) for m in data.get("messages", [])]
    return Conversation(
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
        app_title=data.get("app_title", ""),
        mcp_servers=data.get("mcp_servers", []),
        si_enforcement=data.get("si_enforcement"),
        agent_role=data.get("agent_role", ""),
        fallback_provider=data.get("fallback_provider"),
        fallback_model=data.get("fallback_model"),
    )


def _load_conversation_from_store(
    session_key: str, store: "TranscriptStore"
) -> "tuple[Conversation, dict] | None":
    """Store fallback for load_conversation_from_disk (SP4A load-gap ruling).

    SPEC-08 §2: load delegates to load_all once the JSON file is gone (the
    SP3 sweep renames it .migrated). Rebuilds the Conversation from rows
    through _conversation_from_data — the EXACT JSON deserialization shape —
    plus the sessions-table metadata (written by the dual-write save and the
    sweep). Store failure (corrupt DB) -> log + None: absence of JSON is not
    an error condition, and a broken store must never crash a send prep.

    HIGH-3: api_key is NEVER read from rows (the store schema has no such
    column, test-pinned) — it is re-resolved from providers.yaml exactly as
    the JSON path does, keyed by the metadata dict.

    D3 divergence note: store-hydrated sessions hold rows == messages by
    construction (the wrapper appends one row per message, explicit seq ==
    JSON index), so wm == len-1 and the SP2 dual-anchor guard passes
    naturally — the trap this ruling closes was a migrated session loading
    as None, fresh-converting, and its first save diverged-flagging the
    store permanently. Divergence can still arise only where compaction
    genuinely diverged — and those sessions stay JSON-backed (kept_on_json)
    and never take this path.
    """
    try:
        rows = store.load_all(session_key)
    except Exception:
        logger.warning(
            "[persistence] store-mode load failed for %s — returning None "
            "(JSON absent; fix or remove transcript.db to recover history)",
            session_key,
            exc_info=True,
        )
        return None
    if not rows:
        return None  # nothing in either place — today's None behavior
    data: dict = dict(store.session_meta(session_key))
    data.setdefault("session_key", session_key)
    data["agent_name"] = data.get("agent_name") or ""
    data["messages"] = [
        {
            "role": row.get("role", ""),
            "content": row.get("content", ""),
            "tool_calls": row.get("tool_calls") or [],
            "tool_call_id": row.get("tool_call_id"),
            "tokens_used": row.get("tokens_used") or 0,
            "timestamp": row.get("timestamp"),
        }
        for row in rows
    ]
    conv = _conversation_from_data(data)
    _apply_loaded_fallbacks(conv, session_key, data)
    return conv, data


def _apply_loaded_fallbacks(
    conv: "Conversation", session_key: str, data: dict
) -> None:
    """api_key re-resolution + allowed_tools live-def fallback (SP4A).

    The load-path tail both paths share — the HIGH-3 re-resolution keyed by
    the metadata dict (JSON body or sessions meta; the rows NEVER carry a
    key) and the allowed_tools fallback to the live agent definition. The
    except-pass here is the pre-existing best-effort contract (gate skips
    when lookup fails) — untouched.
    """
    api_key = resolve_api_key_for_conversation(data)
    conv.api_key = api_key  # HIGH-3: re-resolved from providers.yaml
    if conv.allowed_tools is None:
        try:
            from agent.special_agents import get_special_agent
            agent_def = get_special_agent(session_key)
            if agent_def is not None and agent_def.tools:
                conv.allowed_tools = list(agent_def.tools)
        except Exception:
            pass  # Best-effort: leave None if lookup fails (gate skips)


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


def delete_session_rows(session_key: str) -> int:
    """Delete a session's transcript-store rows + metadata (SPEC-08 D2).

    The /clear data-plane contract: the SP4A store-mode load hydrates any
    session whose JSON is absent from `transcript.db` rows — a JSON-only
    clear leaves the store to resurrect the cleared history on the next
    load. /clear calls this AFTER deleting the JSON so "cleared" means
    cleared in BOTH stores.

    Returns the number of turn rows removed. Raises on store failure —
    the caller (/clear) owns the best-effort tolerance.
    """
    return _get_store().delete_session(session_key)


def load_conversation_from_disk(session_key: str) -> tuple["Conversation", dict] | None:
    """Load a conversation from disk. Returns (Conversation, metadata) or None.

    SP4A (load-gap ruling, SPEC-08 §2): JSON present -> the JSON path
    (unchanged D3 fallback contract). JSON ABSENT -> the store fallback
    (_load_conversation_from_store) — a SP3-migrated session (file renamed
    .migrated) hydrates from store rows instead of loading as None.

    HIGH-3: api_key is re-resolved from providers.yaml (atomic+0600) keyed
    by conv.model. Saved api_key in old files is ignored (and stripped
    on next save by the one-time migration). The store path NEVER reads a
    key from rows (schema has no such column, test-pinned).
    """
    path = os.path.join(conversations_dir(), f"{session_key}.json")
    if not os.path.isfile(path):
        # SP4A store fallback: the JSON file's absence is not an error —
        # post-SP3 it usually means the sweep migrated this session. The
        # store ACQUISITION itself is guarded: a corrupt transcript.db
        # raises at construction (inside _get_store), and a broken store
        # must never crash a send prep — None reads as "no history".
        try:
            store = _get_store()
        except Exception:
            logger.warning(
                "[persistence] transcript store unavailable for load of %s "
                "— returning None (JSON absent; fix or remove transcript.db "
                "to recover history)",
                session_key,
                exc_info=True,
            )
            return None
        return _load_conversation_from_store(session_key, store)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    conv = _conversation_from_data(data)
    _apply_loaded_fallbacks(conv, session_key, data)

    # D3 dual-write hydration DELETED (SP2 fix round, BUG#2/#3): the old sync
    # of the store watermark UP to len(messages)-1 violated the ruling — the
    # watermark is "appended through", never "acknowledged through". No store
    # interaction on the JSON path: the restart case self-heals on the next
    # save: with wm=-1, append_delta backfills ALL JSON messages (D3's
    # gradual self-migration).

    return conv, data


# ── HIGH-3: One-time migration ──────────────────────────────────────────────────

# Module-level flag — migration runs once per process
_CONVERSATION_MIGRATION_DONE: bool = False


def migrate_conversation_files() -> int:
    """One-time sweep: remove api_key from existing conversation files.

    HIGH-3: scans ~/.config/develcakes/conversations/*.json, removes the
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


# ── SPEC-08 SP3: one-time JSON→store migration ───────────────────────────────

# Batch boundary: the sessions-loop heartbeat. store.append_delta commits per
# CALL (one transaction for a whole session's tail — not per-turn fsync), and
# on_progress fires at most every _MIGRATION_PROGRESS_INTERVAL sessions, so a
# 3,491-file sweep produces ~350 progress pings, not 3,491.
_MIGRATION_PROGRESS_INTERVAL = 10


def migrate_conversations_to_store(
    on_progress: "Callable[[int, int], None] | None" = None,
) -> dict:
    """One-time JSON→store migration. Idempotent + resumable.

    For each ``<sk>.json`` in conversations_dir(): if the store holds at
    least as many turns for sk as the file has messages (COUNT-based check,
    NOT the watermark — the SP2 audit's BUG#3 showed a wm that can run ahead
    of rows; COUNT is the belt-and-braces predicate) AND the session is NOT
    diverged-flagged, skip — dual-write already caught up. Otherwise append
    the missing tail (explicit-index appends via append_delta, rows[i] at
    JSON index base_idx+i — the same alignment _append_conversation_delta
    uses), then rename the file to ``<sk>.json.migrated``.

    Diverged sessions: NEVER renamed (fix-round-2 ruling — compacted sessions
    are JSON-only forever; renaming would cement a store missing turns) and
    NOT appended; counted in ``kept_on_json`` (the banner lists them).

    Batching: one append_delta commit per session; the batch boundary is the
    sessions-loop heartbeat (on_progress at most every 10 sessions).

    Returns::

        {"migrated": n_sessions, "turns": n_turns, "skipped": n,
         "seconds": t, "errors": [(session_key, repr(e)), ...],
         "kept_on_json": [session_key, ...]}

    Non-destructive: NO file is deleted. Unreadable files are counted in
    ``errors`` and LEFT AS-IS (never renamed) — a retry on the next launch
    can attempt them again. A session whose append fails is likewise left
    unrenamed: partial rows are harmless (append_delta is index-aligned and
    idempotent; the retry skips indexes the store already holds).

    ``on_progress(done, total)`` fires at most every 10 sessions plus a
    final call at completion. Exceptions raised BY the callback are ignored
    (a broken heartbeat must not abort the sweep).

    Failure posture: the store ACQUISITION itself (corrupt transcript.db) is
    inside the guarded region — on failure the function RETURNS a stats dict
    with ``aborted=True`` and ``errors=[("<store>", repr(e))]`` (never
    raises): JSON is untouched, nothing was renamed, and the next launch
    retries (run_store_migration_once unsets the latch on abort).
    """
    start = time.monotonic()
    result: dict = {
        "migrated": 0,
        "turns": 0,
        "skipped": 0,
        "seconds": 0.0,
        "errors": [],
        "kept_on_json": [],
        "aborted": False,
    }
    d = conversations_dir()
    try:
        names = sorted(
            f for f in os.listdir(d) if f.endswith(".json")
        )
    except OSError:
        return result  # no conversations dir yet — nothing to migrate
    total = len(names)
    try:
        store: TranscriptStore = _get_store()
    except Exception as exc:  # abort-and-retry is the contract
        # BUG#3 (SP3 fix round): a corrupt DB previously raised OUT of the
        # sweep with the latch already set — silent total abort, no card, no
        # retry. Now: return-and-retry. The SP4 banner renders "migration
        # failed — JSON untouched, will retry next launch" from `aborted`.
        logger.exception(
            "[persistence] transcript store unavailable — migration ABORTED, "
            "JSON untouched, will retry next launch"
        )
        result["aborted"] = True
        result["errors"].append(("<store>", repr(exc)))
        result["seconds"] = round(time.monotonic() - start, 3)
        return result
    for done, name in enumerate(names, start=1):
        sk = name[: -len(".json")]
        path = os.path.join(d, name)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            messages = data.get("messages", [])
            if store.is_diverged(sk):
                # Compacted session: JSON stays authoritative forever. Never
                # renamed, never appended — the store rows are an audit ledger.
                result["kept_on_json"].append(sk)
            elif not messages:
                # Empty history: nothing to append, but first store contact →
                # the file IS migrated (renamed + counted), not skipped.
                os.rename(path, path + ".migrated")
                result["migrated"] += 1
            else:
                # BUG#4 precision: the skip branch reads COVERAGE via
                # store.covers(); the append branch reads rows_before (its
                # precheck + the turns baseline) then re-verifies with
                # covers() post-append.
                rows_before = len(store.load_all(sk))
                if store.covers(sk, len(messages) - 1):
                    # Skip branch: COVERAGE, not count (SP3 fix round 2).
                    # covers() counts rows at seq <= file_len-1 in the CURRENT
                    # epoch only — phantom high-seq rows (cardinality matches,
                    # index missing) and multi-epoch load_all inflation
                    # (prior-epoch rows) both fail it where
                    # `rows_before >= len(messages)` passed.
                    # Trust boundary (round 3, register-not-code): this
                    # predicate is INDEX-coverage only — content equality is
                    # the wrapper guard's job at save time (dual anchor) and
                    # stable-ids post-MVP. Renaming asserts nothing about
                    # content.
                    os.rename(path, path + ".migrated")
                    result["skipped"] += 1
                elif rows_before >= len(messages):
                    # A collision shape (count matched, coverage didn't):
                    # exactly the wm-ahead class — error + keep the file +
                    # retry next launch. Never rename on a guess.
                    result["errors"].append(
                        (
                            sk,
                            f"count/coverage mismatch: count={rows_before} file={len(messages)}",
                        )
                    )
                    logger.warning(
                        "[persistence] %s: skip-branch coverage mismatch "
                        "(count=%d file=%d) — NOT migrated, file kept for retry",
                        sk,
                        rows_before,
                        len(messages),
                    )
                else:
                    # Store is behind (or absent): append the full JSON
                    # history at its exact indexes. append_delta re-reads the
                    # watermark IN-TX and skips already-committed indexes, so
                    # a session a crashed earlier launch half-migrated
                    # resumes cleanly.
                    shaped = [_shape_json_message(m) for m in messages]
                    store.append_delta(sk, 0, shaped)
                    if not store.covers(sk, len(messages) - 1):
                        # BUG#1 (SP3 fix round, round-2 repoint): the append
                        # left the session uncovered — a wm-ahead state
                        # (corrupt wm, phantom high-seq row) makes
                        # append_delta start past every row and write
                        # NOTHING. Renaming here would destroy the only copy
                        # of the missing turns and report success. The rename
                        # is EARNED by verified coverage: error + keep the
                        # file + retry next launch.
                        result["errors"].append(
                            (
                                sk,
                                f"watermark ahead of rows: wm={store.session_watermark(sk)} rows={rows_before} file={len(messages)}",
                            )
                        )
                        logger.warning(
                            "[persistence] %s: store watermark ahead of rows "
                            "(wm=%d rows=%d file=%d) — NOT migrated, file "
                            "kept for retry",
                            sk,
                            store.session_watermark(sk),
                            rows_before,
                            len(messages),
                        )
                    else:
                        # Rename ONLY now: the current epoch PROVABLY holds
                        # every seq the file holds.
                        # SP4A: metadata BEFORE the rename — once the file is
                        # gone, the sessions table is the only agent_name/
                        # model/provider source for store-mode load. A meta
                        # failure does NOT block the rename (the sweep's
                        # verdict is coverage-based): the loader rebuilds
                        # with empty metadata and HIGH-3 resolves None —
                        # logged, never silent.
                        try:
                            store.set_session_meta(
                                sk,
                                agent_name=str(data.get("agent_name", "")),
                                model=str(data.get("model", "")),
                                provider=data.get("provider"),
                            )
                        except Exception:  # meta is advisory, never blocks rename
                            logger.warning(
                                "[persistence] %s: sessions-meta upsert failed "
                                "during migration — store-mode load will use "
                                "empty metadata (api_key resolves None)",
                                sk,
                                exc_info=True,
                            )
                        os.rename(path, path + ".migrated")
                        result["migrated"] += 1
                        # BUG#4: rows actually written, not file size (a
                        # resumed session appends only its missing tail).
                        # max(0, …): rows_before is an all-epoch load_all
                        # count, so phantom/multi-epoch inflation can exceed
                        # the file size — the delta clamps at 0 rather than
                        # reporting a negative turn count. Coverage above is
                        # the correctness gate; this is a banner stat only.
                        result["turns"] += max(0, len(messages) - rows_before)
        except Exception as exc:  # noqa: BLE001 — per-session isolation IS the contract
            # One bad file never aborts the sweep: collected + retried next
            # launch (file left as-is).
            logger.warning(
                "[persistence] store migration failed for %s (left in place, "
                "will retry next launch): %r",
                sk,
                exc,
            )
            result["errors"].append((sk, repr(exc)))
        if on_progress is not None and (
            done % _MIGRATION_PROGRESS_INTERVAL == 0 or done == total
        ):
            try:
                on_progress(done, total)
            except Exception:  # heartbeat must not kill the sweep
                logger.exception("[persistence] migration on_progress callback raised")
    result["seconds"] = round(time.monotonic() - start, 3)
    if result["migrated"] or result["errors"] or result["kept_on_json"]:
        logger.info(
            "[persistence] JSON→store migration: %d session(s) migrated (%d turns), "
            "%d already current, %d kept on JSON (diverged), %d error(s) in %.3fs",
            result["migrated"],
            result["turns"],
            result["skipped"],
            len(result["kept_on_json"]),
            len(result["errors"]),
            result["seconds"],
        )
    return result


def _shape_json_message(m: dict) -> dict:
    """Saved-JSON message dict → the store's persistence row shape.

    The JSON body and _shape_message() produce the same fields; this is the
    JSON-dict twin (input is a dict from json.load, not a Message object).
    HIGH-3: no api_key field is read even if present in the file.
    """
    return {
        "role": str(m.get("role", "")),
        "content": str(m.get("content", "")),
        "tool_calls": [
            {
                "call_id": tc.get("call_id"),
                "tool_name": tc.get("tool_name"),
                "arguments": tc.get("arguments", {}),
            }
            for tc in (m.get("tool_calls") or [])
        ],
        "tool_call_id": m.get("tool_call_id"),
        "tokens_used": m.get("tokens_used") or 0,
    }


# ── SPEC-08 SP3: launch-time entry point (latch + banner gate) ────────────────

# Once-per-process latch (same pattern as _CONVERSATION_MIGRATION_DONE above).
_STORE_MIGRATION_DONE: bool = False


def run_store_migration_once(
    on_complete: "Callable[[dict], None] | None" = None,
    on_progress: "Callable[[int, int], None] | None" = None,
) -> "dict | None":
    """Run the JSON→store migration at most once per process (SP3 launch path).

    The production caller (runtime init) starts this on a daemon thread; the
    card itself is built by the on_complete receiver ON THE MAIN LOOP — this
    function never touches GTK.

    Banner gate (spec): on_complete fires ONLY when migrated > 0 — a clean
    install with zero legacy files must not produce a card. The full stats
    dict (migrated/turns/skipped/seconds/errors/kept_on_json/aborted) is
    passed so the card can report errors and kept-on-JSON sessions. When the
    store itself is unavailable, the stats carry ``aborted=True`` and the
    callback fires with it (the SP4 banner renders "migration failed — JSON
    untouched, will retry next launch").

    Returns the stats dict, or None when the latch had already fired (a
    second AgentRuntime in-process must not re-sweep). On ``aborted=True``
    the latch is UNSET — the next launch retries (a transient corrupt state
    must not permanently silence the feature).
    """
    global _STORE_MIGRATION_DONE
    if _STORE_MIGRATION_DONE:
        return None
    _STORE_MIGRATION_DONE = True
    stats = migrate_conversations_to_store(on_progress=on_progress)
    if stats["aborted"]:
        # Return-and-retry: unset so the next launch sweeps again.
        _STORE_MIGRATION_DONE = False
    if (
        stats["migrated"] > 0 or stats["aborted"] or stats["errors"]
    ) and on_complete is not None:
        try:
            on_complete(stats)
        except Exception:  # a broken card must not fail the sweep
            logger.exception("[persistence] store-migration on_complete callback raised")
    return stats


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