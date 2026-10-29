# tests/test_streaming.py
# Tests for models/streaming.py — StreamingBubble dataclass.
#
# SPEC-58 §2i: class TestStreamingBubbleHigh6 deleted with its subject —
# build_streaming_bubble died with ui/views/event_cards.py. The surviving
# classes pin the StreamingBubble dataclass, which is live.

import pytest
from models import StreamingBubble


class TestStreamingBubbleDefaults:
    def test_plain_text_defaults_to_empty_string(self):
        """plain_text must default to '' — streaming starts with no accumulated text."""
        sb = StreamingBubble(container="c", label="l", role="Agent")
        assert sb.plain_text == ""

    def test_bubble_defaults_to_none(self):
        """bubble must default to None — not set until bubble widget is created."""
        sb = StreamingBubble(container="c", label="l", role="Agent")
        assert sb.bubble is None


class TestStreamingBubbleFields:
    def test_all_fields_can_be_set(self):
        """Every field accepts a value without error."""
        sb = StreamingBubble(
            container="container_obj",
            label="label_obj",
            role="Agent",
            plain_text="hello",
            bubble="bubble_obj",
        )
        assert sb.container == "container_obj"
        assert sb.label == "label_obj"
        assert sb.role == "Agent"
        assert sb.plain_text == "hello"
        assert sb.bubble == "bubble_obj"

    def test_plain_text_can_be_mutated_in_place(self):
        """plain_text is mutable — used by update_streaming to track cumulative text."""
        sb = StreamingBubble(container="c", label="l", role="Agent")
        sb.plain_text = "partial response"
        assert sb.plain_text == "partial response"
        sb.plain_text = "full response"
        assert sb.plain_text == "full response"

    def test_role_accepts_you(self):
        """role field accepts "You" for outgoing streaming bubbles."""
        sb = StreamingBubble(container="c", label="l", role="You")
        assert sb.role == "You"
