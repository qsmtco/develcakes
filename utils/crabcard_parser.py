# utils/crabcard_parser.py
# Crabcard block parser — extracts ```crabcard ``` blocks from chat text.
# Pure function, no GTK, no git, no network.
# Architecture: utils/ package, may import models/ only.
#
# Crabcard format:
#     ```crabcard
#     type: <card_type>
#     title: <title text>
#     file: <optional file path>
#     additions: <optional int>
#     deletions: <optional int>
#     commit_sha: <optional str>
#     task_id: <optional str>
#     ---
#     <body content — diff text, description, etc.>
#     ```
#
# Files that import this:
#   - ui/views/event_cards.py (placeholder-segment rendering path)

import re
from datetime import datetime, timezone

from models.feed_card import FeedCardData

# Placeholder marker used in cleaned text to mark where a crabcard was removed.
# The placeholder contains the index so build_role_bubble can swap it for the
# reference widget at the right position.
_CRABCARD_PLACEHOLDER = "\x00CRABCARD_REF:%d\x00"


def extract_crabcards(text: str, project_name: str, agent_name: str = "agent") -> tuple[str, list[FeedCardData]]:
    """
    Parse crabcard blocks from chat message text.

    Args:
        text:         Raw chat message text from agent.
        project_name: Project name to assign to parsed cards.
        agent_name:   Author name for parsed cards (default "agent").

    Returns:
        (cleaned_text, cards) where:
          - cleaned_text: original text with crabcard blocks replaced by
            placeholder markers. Placeholders are safe — they will be
            detected and replaced by feed reference widgets in build_role_bubble.
          - cards: list of FeedCardData parsed from the crabcard blocks.
            Returns [] if no crabcard blocks found.

    Crabcard format:
        ```crabcard
        type: <card_type>
        title: <title text>
        file: <optional file path>
        ---
        <body content>
        ```

    Parsing rules:
      1. Finds all ```crabcard ... ``` blocks (language must be exactly "crabcard")
      2. Header fields: key: value pairs before the first ```---```
      3. Body: everything after ```---``` (may be empty)
      4. Required header fields: type, title
      5. Optional header fields: file, additions, deletions, commit_sha, task_id
      6. source="agent", author=agent_name, timestamp=now, project_name=project_name

    If a crabcard block is malformed or missing required fields, it is
    returned as part of cleaned_text (not dropped) and logged as a warning.
    """
    cards: list[FeedCardData] = []

    # Match ```crabcard ... ``` (with optional language tag "crabcard")
    # The fence must be exactly ```crabcard (not ```crabcard-python or similar).
    # Content can be anything (including ``` inside), until the closing ```.
    fence_pattern = re.compile(
        r'^```crabcard\b[ \t]*\n(.*?)^```',
        re.DOTALL | re.MULTILINE,
        # re.DOTALL: . matches newline so --- can cross lines
        # re.MULTILINE: ^/$ match line boundaries (for ^``` and ^---)
    )

    def _parse_single_block(block_content: str, card_idx: int) -> FeedCardData | None:
        """Parse one crabcard block's header+body into FeedCardData."""
        # Split header from body at ---
        # The separator is a line that is EXACTLY --- (no extra spaces)
        parts = block_content.split("\n---\n", 1)
        header_section = parts[0]
        body = parts[1] if len(parts) > 1 else ""

        # Parse header fields from the header section
        fields: dict[str, str] = {}
        for line in header_section.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Skip the ```crabcard opening line already consumed
            if line.startswith("```"):
                continue
            # key: value — split on first colon
            colon = line.find(":")
            if colon < 0:
                continue
            key = line[:colon].strip().lower()
            val = line[colon + 1:].strip()
            fields[key] = val

        # Required fields
        card_type = fields.get("type", "").strip()
        title = fields.get("title", "").strip()
        if not card_type or not title:
            return None

        return FeedCardData(
            card_type=card_type,
            source="agent",
            title=title,
            body=body.strip(),
            author=agent_name,
            timestamp=datetime.now(timezone.utc),
            project_name=project_name,
            file_path=fields.get("file") or None,
            additions=int(fields["additions"]) if fields.get("additions", "").isdigit() else None,
            deletions=int(fields["deletions"]) if fields.get("deletions", "").isdigit() else None,
            commit_sha=fields.get("commit_sha") or None,
            task_id=fields.get("task_id") or None,
            metadata={},
        )

    # Scan for crabcard blocks and build cleaned text with placeholders
    cleaned_parts: list[str] = []
    last_end = 0
    card_idx = 0

    for m in fence_pattern.finditer(text):
        # Text before this crabcard block
        cleaned_parts.append(text[last_end:m.start()])

        parsed = _parse_single_block(m.group(1), card_idx)
        if parsed is not None:
            cards.append(parsed)
            # Replace with placeholder marker
            cleaned_parts.append(_CRABCARD_PLACEHOLDER % card_idx)
            card_idx += 1
        else:
            # Malformed block — keep original text (don't strip it)
            cleaned_parts.append(m.group(0))

        last_end = m.end()

    cleaned_parts.append(text[last_end:])
    cleaned_text = "".join(cleaned_parts)

    return cleaned_text, cards


def is_crabcards_placeholder(text: str) -> bool:
    """True if text is a crabcard placeholder marker."""
    return _CRABCARD_PLACEHOLDER[:10] in text


def get_placeholder_index(placeholder: str) -> int | None:
    """Extract the card index from a placeholder string. Returns None if invalid."""
    if _CRABCARD_PLACEHOLDER[:10] not in placeholder:
        return None
    try:
        # Format: \x00CRABCARD_REF:0\x00
        start = placeholder.find("CRABCARD_REF:") + len("CRABCARD_REF:")
        end = placeholder.find("\x00", start)
        return int(placeholder[start:end])
    except (ValueError, TypeError):
        return None