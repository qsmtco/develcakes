# agent/context_edit.py — choose which conversation messages stay.
#
# Pure. No GTK, no runtime. The caller mutates the live Conversation and
# writes it back through persist_conversation.

from __future__ import annotations

_PREVIEW_CHARS = 72
_MAX_RANGE = 10_000


def role_name(msg) -> str:
    role = getattr(msg, "role", "")
    value = getattr(role, "value", role)
    return str(value)


def parse_selectors(value) -> tuple[list[int] | None, str | None]:
    """Indexes from a tool argument.

    Accepts a list of ints or numeric strings, inclusive ranges like
    ``"4-12"``, a comma-separated string, or one integer. ``None`` means
    the argument was omitted.
    """
    if value is None:
        return None, None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, "drop and keep indexes must be integers or ranges like 4-12"
        value = [part.strip() for part in text.split(",") if part.strip()]
    elif isinstance(value, bool):
        return None, "drop and keep indexes must be integers or ranges like 4-12"
    elif isinstance(value, int):
        value = [value]
    elif not isinstance(value, (list, tuple)):
        return None, "drop and keep must be a list of indexes"
    out: list[int] = []
    for item in value:
        parsed, error = _one_selector(item)
        if error is not None:
            return None, error
        out.extend(parsed or [])
    return out, None


def _one_selector(item) -> tuple[list[int] | None, str | None]:
    if isinstance(item, bool) or item is None:
        return None, "drop and keep indexes must be integers or ranges like 4-12"
    if isinstance(item, int):
        if item < 0:
            return None, "indexes start at 0"
        return [item], None
    if isinstance(item, str):
        text = item.strip()
        if text.isdigit():
            return [int(text)], None
        if "-" in text:
            left, right = text.split("-", 1)
            if left.strip().isdigit() and right.strip().isdigit():
                start = int(left.strip())
                end = int(right.strip())
                if end < start:
                    return None, f"range {text} is backwards"
                if end - start > _MAX_RANGE:
                    return None, "range is too large"
                return list(range(start, end + 1)), None
    return None, "drop and keep indexes must be integers or ranges like 4-12"


def index_groups(messages) -> list[set[int]]:
    """Each index shares a set with the tool call it belongs to.

    An assistant message that carries tool calls and every tool result
    with one of those call ids move together. Anything else is alone.
    """
    groups = [{i} for i in range(len(messages))]
    parent: dict[str, int] = {}
    for i, msg in enumerate(messages):
        if role_name(msg) != "assistant":
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            call_id = getattr(tc, "call_id", None)
            if call_id and call_id not in parent:
                parent[call_id] = i
    for i, msg in enumerate(messages):
        if role_name(msg) != "tool":
            continue
        call_id = getattr(msg, "tool_call_id", None)
        owner = parent.get(call_id) if call_id else None
        if owner is None:
            continue
        groups[owner].add(i)
        groups[i] = groups[owner]
    for i, msg in enumerate(messages):
        if role_name(msg) != "assistant" or not (getattr(msg, "tool_calls", None) or []):
            continue
        members = groups[i]
        for j in list(members):
            groups[j] = members
    return groups


def open_turn_start(messages) -> int:
    """Index of the turn that is still being answered.

    That is the last user message. When the transcript has no user
    message, it is the last assistant tool call, which is the call
    running right now.
    """
    last_user = None
    for i, msg in enumerate(messages):
        if role_name(msg) == "user":
            last_user = i
    if last_user is not None:
        return last_user
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if role_name(msg) == "assistant" and (getattr(msg, "tool_calls", None) or []):
            return i
    return len(messages)


def incomplete_assistant(messages) -> int | None:
    """Nearest assistant tool call that does not yet have every result.

    Walks back from the end through tool results. A finished exchange
    returns None. The call running ``agent_context`` itself is incomplete
    until this tool returns.
    """
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        calls = getattr(msg, "tool_calls", None) or []
        if role_name(msg) == "assistant" and calls:
            ids = {
                tc.call_id
                for tc in calls
                if getattr(tc, "call_id", None)
            }
            if not ids:
                return None
            have = {
                getattr(other, "tool_call_id", None)
                for other in messages
                if role_name(other) == "tool" and getattr(other, "tool_call_id", None)
            }
            if ids <= have:
                return None
            return i
        if role_name(msg) != "tool":
            return None
    return None


def locked_indexes(messages) -> set[int]:
    """The in-flight tool call and any results already stored for it.

    Finished reads in this turn are not locked. The running call is,
    because the loop still has to attach its result.
    """
    incomplete = incomplete_assistant(messages)
    if incomplete is None:
        return set()
    return set(index_groups(messages)[incomplete])


def plan_clear_before_open_turn(messages) -> set[int]:
    """Indexes to drop on a self-clear.

    Keeps the current request (the last user message) and the in-flight
    tool call. Finished tool output, including earlier in this turn, goes.
    A tool group that overlaps what we keep stays whole.
    """
    keep = set(locked_indexes(messages))
    start = open_turn_start(messages)
    if start < len(messages) and role_name(messages[start]) == "user":
        keep.add(start)
    groups = index_groups(messages)
    expanded: set[int] = set()
    for i in keep:
        expanded |= groups[i]
    return set(range(len(messages))) - expanded


def plan_context_edit(
    messages,
    *,
    drop: list[int] | None = None,
    keep: list[int] | None = None,
    protect_open_turn: bool = False,
) -> tuple[set[int], str | None]:
    """Return indexes to remove. Does not mutate ``messages``.

    ``drop`` and ``keep`` are exclusive. Tool groups expand so a call and
    its results stay together. When ``protect_open_turn`` is set, the
    turn being answered cannot be removed.
    """
    if drop is not None and keep is not None:
        return set(), "pass drop or keep, not both"
    if drop is None and keep is None:
        return set(), "edit needs drop or keep indexes"
    n = len(messages)
    groups = index_groups(messages)
    locked = locked_indexes(messages) if protect_open_turn else set()
    if keep is not None:
        error = _bounds(keep, n)
        if error is not None:
            return set(), error
        chosen: set[int] = set(locked)
        for i in keep:
            chosen |= groups[i]
        return set(range(n)) - chosen, None
    error = _bounds(drop or [], n)
    if error is not None:
        return set(), error
    remove: set[int] = set()
    for i in drop or []:
        group = groups[i]
        overlap = group & locked
        if overlap:
            return set(), (
                f"index {i} belongs with the in-flight tool call "
                f"({format_indexes(overlap)}); that call stays until it returns"
            )
        remove |= group
    return remove, None


def _bounds(indexes, n: int) -> str | None:
    for i in indexes:
        if i < 0 or i >= n:
            if n == 0:
                return f"index {i} is outside an empty conversation"
            return f"index {i} is outside 0-{n - 1}"
    return None


def kept_messages(messages, remove: set[int]) -> list:
    return [msg for i, msg in enumerate(messages) if i not in remove]


def format_indexes(indexes) -> str:
    ordered = sorted(set(indexes))
    if not ordered:
        return "none"
    spans: list[str] = []
    start = prev = ordered[0]
    for n in ordered[1:]:
        if n == prev + 1:
            prev = n
            continue
        spans.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    spans.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(spans)


def outline_messages(messages, *, protect_open_turn: bool = False) -> str:
    """Short index the model can choose from. Previews stay small."""
    groups = index_groups(messages)
    locked = locked_indexes(messages) if protect_open_turn else set()
    total = sum(_weight(msg) for msg in messages)
    lines = [
        (
            f"{len(messages)} messages, about {total} tokens. "
            "edit with drop or keep. A tool call and its results share a group "
            "and move together."
        )
    ]
    if locked:
        lines.append(
            f"In-flight tool call is {format_indexes(locked)} "
            "and stays until this call returns."
        )
    request_at = None
    if protect_open_turn:
        request = open_turn_start(messages)
        if request < len(messages) and role_name(messages[request]) == "user":
            request_at = request
    for i, msg in enumerate(messages):
        group = groups[i]
        group_bit = f" group {format_indexes(group)}" if len(group) > 1 else ""
        if i in locked:
            open_bit = " [in flight]"
        elif i == request_at:
            open_bit = " [request]"
        else:
            open_bit = ""
        lines.append(
            f"{i} {role_name(msg)} ~{_weight(msg)}tok"
            f"{group_bit}{open_bit} | {_preview(msg)}"
        )
    return "\n".join(lines)


def _weight(msg) -> int:
    n = len(getattr(msg, "content", "") or "")
    for tc in getattr(msg, "tool_calls", None) or []:
        n += len(str(getattr(tc, "arguments", "") or ""))
    return n // 4


def _preview(msg) -> str:
    calls = getattr(msg, "tool_calls", None) or []
    if role_name(msg) == "assistant" and calls:
        bits = []
        for tc in calls:
            name = getattr(tc, "tool_name", "") or "tool"
            args = getattr(tc, "arguments", None)
            hint = ""
            if isinstance(args, dict):
                for key in ("path", "command", "agent", "action", "query"):
                    val = args.get(key)
                    if isinstance(val, str) and val.strip():
                        hint = " " + " ".join(val.split())[:40]
                        break
            bits.append(name + hint)
        return ", ".join(bits) or "(empty)"
    text = " ".join((getattr(msg, "content", "") or "").split())
    if len(text) > _PREVIEW_CHARS:
        text = text[: _PREVIEW_CHARS - 3] + "..."
    return text or "(empty)"


def snapshot_messages(messages) -> list:
    """Messages safe to store while a tool call has not returned yet.

    The live list keeps the in-flight assistant message so the loop can
    attach its result. The file omits that message, and any partial
    results already stored for it, so a restart does not load a tool
    call with no result.
    """
    incomplete = incomplete_assistant(messages)
    if incomplete is None:
        return messages
    ids = {
        tc.call_id
        for tc in (getattr(messages[incomplete], "tool_calls", None) or [])
        if getattr(tc, "call_id", None)
    }
    drop = {incomplete}
    for j, other in enumerate(messages):
        if role_name(other) == "tool" and getattr(other, "tool_call_id", None) in ids:
            drop.add(j)
    return [msg for j, msg in enumerate(messages) if j not in drop]


def persist_conversation(conv, session_key: str) -> None:
    """Write ``conv`` to the conversation file.

    Swaps in the durable snapshot for the write, then puts the live
    messages back. The in-memory list the loop is using does not change.
    """
    original = conv.messages
    snapshot = snapshot_messages(original)
    swapped = snapshot is not original
    if swapped:
        conv.messages = snapshot
    try:
        from agent.persistence import save_conversation_to_disk
        save_conversation_to_disk(conv, session_key)
    finally:
        if swapped:
            conv.messages = original
            conv.invalidate_token_estimate()
