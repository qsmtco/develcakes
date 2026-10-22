"""SPEC-48 Phase 3 — typed digest tests.

Covers the ConversationDigest data model (models/conversation.py), the
two-tier digest computation and the trimmable-region snapshot
(agent/context_strategy.py), and save/load persistence
(agent/persistence.py).
"""

from agent.context_strategy import (
    DefaultContextStrategy,
    LLMSummarizeStrategy,
    _digest_for_summary,
    _digest_from_snapshot,
    _digest_from_tagged_summary,
)
from models.conversation import (
    Conversation,
    ConversationDigest,
    Message,
    MessageRole,
    ToolCall,
)


def _asst(content: str, tool_calls=None) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=content, tool_calls=tool_calls or [])


def _big_conv(trim_turns: int = 8) -> Conversation:
    """A conversation with a small protected head/tail and a large trimmable
    region, so compact() trims and injects a summary.

    Structure (the tests compact with keep_first=3, tail_preserve=4):
      [0] user                                   — kept head (summary material)
      [1] assistant (tool call, path=head_path.txt) — kept head
      [2] tool result of [1]                       — kept head
      [3..] trimmable turns: user / assistant(tool, path=trim_i.txt) / result
      [-4..] tail assistants (tool calls, path=tail_path.txt) — kept tail
    """
    conv = Conversation(agent_name="test", model="test/x")
    conv.add_user_message("head user task")
    conv.add_assistant_message(
        "head done",
        tool_calls=[
            ToolCall(call_id="ch", tool_name="read_file", arguments={"path": "head_path.txt"})
        ],
    )
    conv.add_tool_result("ch", "ok")
    for i in range(trim_turns):
        conv.add_user_message(f"task {i}: " + "u" * 1000)
        conv.add_assistant_message(
            f"done {i}: " + "d" * 1000,
            tool_calls=[
                ToolCall(call_id=f"c{i}", tool_name="read_file", arguments={"path": f"trim_{i}.txt"})
            ],
        )
        conv.add_tool_result(f"c{i}", "ok")
    conv.add_assistant_message(
        "tail a",
        tool_calls=[
            ToolCall(call_id="ct1", tool_name="read_file", arguments={"path": "tail_path.txt"})
        ],
    )
    conv.add_tool_result("ct1", "ok")
    conv.add_assistant_message(
        "tail b",
        tool_calls=[
            ToolCall(call_id="ct2", tool_name="read_file", arguments={"path": "tail_path.txt"})
        ],
    )
    conv.add_tool_result("ct2", "ok")
    return conv


# ── ConversationDigest model ───────────────────────────────────────────────────


class TestConversationDigestModel:
    def test_unknown_keys_ignored(self):
        d = ConversationDigest.from_dict(
            {"arc": "x", "bogus": 1, "decisions": None, "token_estimate": "7"}
        )
        assert d.arc == "x"
        assert d.decisions == []
        assert d.token_estimate == 7

    def test_non_dict_returns_empty_digest(self):
        for bad in (None, [1, 2], "nope", 42):
            d = ConversationDigest.from_dict(bad)
            assert d.arc == ""
            assert d.decisions == []
            assert d.constraints == []
            assert d.open_questions == []
            assert d.paths_touched == []
            assert d.attempts_rejected == []
            assert d.token_estimate == 0

    def test_to_dict_roundtrip(self):
        d = ConversationDigest(
            arc="a",
            decisions=["b"],
            constraints=["c"],
            open_questions=["d"],
            paths_touched=["e.py"],
            attempts_rejected=["f"],
            token_estimate=5,
        )
        assert ConversationDigest.from_dict(d.to_dict()) == d


# ── Tier 2: the tagged parser ─────────────────────────────────────────────────


class TestDigestTier2:
    def test_nine_tag_summary_maps_all_six_fields(self):
        summary = (
            "<task>Build the auth flow</task>\n"
            "<progress>- did X\n- did Y</progress>\n"
            "<files>src/auth.py, src/login.py</files>\n"
            "<decisions>- Use JWT\n- Token expiry 30m</decisions>\n"
            "<constraints>- Must support offline mode</constraints>\n"
            "<errors>- Network timeout\n- 401 on refresh</errors>\n"
            "<open_questions>- Should we support SSO?</open_questions>\n"
            "<next_steps>- Write tests</next_steps>\n"
            "<user_preferences>- Prefer minimal deps</user_preferences>"
        )
        d = _digest_from_tagged_summary(summary)
        assert d.arc == "Build the auth flow"
        assert d.decisions == ["Use JWT", "Token expiry 30m"]
        assert d.constraints == ["Must support offline mode"]
        assert d.open_questions == ["Should we support SSO?"]
        assert d.paths_touched == ["src/auth.py", "src/login.py"]
        assert d.attempts_rejected == ["Network timeout", "401 on refresh"]

    def test_missing_or_empty_tags_give_empty_fields(self):
        d = _digest_from_tagged_summary("<task>Only task</task>")
        assert d.arc == "Only task"
        assert d.decisions == []
        assert d.constraints == []
        assert d.open_questions == []
        assert d.paths_touched == []
        assert d.attempts_rejected == []
        d2 = _digest_from_tagged_summary("<task></task><decisions></decisions>")
        assert d2.arc == ""
        assert d2.decisions == []

    def test_single_line_body_one_item(self):
        d = _digest_from_tagged_summary("<decisions>- Use JWT</decisions>")
        assert d.decisions == ["Use JWT"]

    def test_files_comma_split_one_item_per_path(self):
        d = _digest_from_tagged_summary("<files>a.py,b.py, c.py</files>")
        assert d.paths_touched == ["a.py", "b.py", "c.py"]


# ── Tier 1: snapshot-derived fields ────────────────────────────────────────────


class TestDigestTier1:
    def test_textual_summary_populates_arc_only_list_fields(self):
        textual = "Conversation so far (3 prior turns):\n  1. fix login\n  2. add tests"
        msgs = [_asst("ok")]
        d = _digest_from_snapshot(msgs, textual)
        assert d.arc == "Conversation so far (3 prior turns):"
        assert d.paths_touched == []
        assert d.attempts_rejected == []
        assert d.decisions == []
        assert d.constraints == []
        assert d.open_questions == []

    def test_tier_check_by_tags_not_strategy(self):
        # no tags → Tier 1 (even when called through _digest_for_summary)
        textual = "Conversation so far (1 prior turns):\n  1. task one"
        msgs = [_asst("ok")]
        d = _digest_for_summary(textual, msgs)
        assert d.arc == "Conversation so far (1 prior turns):"
        assert d.decisions == [] and d.constraints == [] and d.open_questions == []
        # tags present → Tier 2
        tagged = "<task>Do the thing</task><decisions>- go</decisions>"
        d2 = _digest_for_summary(tagged, msgs)
        assert d2.arc == "Do the thing"
        assert d2.decisions == ["go"]

    def test_paths_first_appearance_order_dedup(self):
        msgs = [
            _asst("a", [ToolCall(call_id="c1", tool_name="read_file", arguments={"path": "one.py"})]),
            _asst("b", [ToolCall(call_id="c2", tool_name="read_file", arguments={"path": "one.py"})]),
            _asst("c", [ToolCall(call_id="c3", tool_name="read_file", arguments={"path": "two.py"})]),
        ]
        d = _digest_from_snapshot(msgs, "")
        assert d.paths_touched == ["one.py", "two.py"]

    def test_paths_non_string_or_missing_skipped(self):
        msgs = [
            _asst("a", [ToolCall(call_id="c1", tool_name="read_file", arguments={"path": 123})]),
            _asst("b", [ToolCall(call_id="c2", tool_name="read_file", arguments={})]),
        ]
        d = _digest_from_snapshot(msgs, "")
        assert d.paths_touched == []

    def test_paths_cap_32(self):
        msgs = [
            _asst("x", [ToolCall(call_id=f"p{i}", tool_name="read_file", arguments={"path": f"p{i}.py"})])
            for i in range(40)
        ]
        d = _digest_from_snapshot(msgs, "")
        assert len(d.paths_touched) == 32
        assert d.paths_touched == [f"p{i}.py" for i in range(32)]

    def test_attempts_rejected_markers(self):
        msgs = [
            _asst("REFUSED: cannot run mutating tool"),
            _asst("did work\nFAILED compile step 1\nmore text"),
            _asst("normal"),
            _asst("FAILED test suite"),
        ]
        d = _digest_from_snapshot(msgs, "")
        assert d.attempts_rejected == [
            "REFUSED: cannot run mutating tool",
            "FAILED compile step 1",
            "FAILED test suite",
        ]

    def test_attempts_rejected_cap_16(self):
        msgs = [_asst(f"REFUSED: {i}") for i in range(20)]
        d = _digest_from_snapshot(msgs, "")
        assert len(d.attempts_rejected) == 16


# ── Integration: compact() digest wiring ──────────────────────────────────────


class TestDigestCompactIntegration:
    def test_snapshot_scope_excludes_head_and_tail(self):
        conv = _big_conv()
        DefaultContextStrategy().compact(conv, token_budget=1500, keep_first=3)
        assert conv.digest is not None, "expected a digest after a real trim"
        assert conv.digest.paths_touched == [f"trim_{i}.txt" for i in range(8)]
        assert "head_path.txt" not in conv.digest.paths_touched
        assert "tail_path.txt" not in conv.digest.paths_touched
        # textual summary (no tags) → Tier 1: list fields stay empty
        assert conv.digest.decisions == []
        assert conv.digest.constraints == []
        assert conv.digest.open_questions == []

    def test_llm_fallback_no_provider_is_tier1_not_empty(self):
        conv = _big_conv()
        LLMSummarizeStrategy(llm_provider=None).compact(
            conv, token_budget=1500, keep_first=3
        )
        assert conv.digest is not None
        assert conv.digest.arc != ""
        assert conv.digest.paths_touched  # Tier 1 is not empty
        assert conv.digest.decisions == []
        assert conv.digest.open_questions == []

    def test_noop_compact_preserves_existing_digest_c11(self):
        conv = Conversation(agent_name="test", model="test/x")
        conv.add_user_message("hi")
        conv.add_assistant_message("there", [])
        conv.digest = ConversationDigest(arc="existing", decisions=["keep"])
        DefaultContextStrategy().compact(conv, token_budget=10_000)
        assert conv.digest is not None
        assert conv.digest.arc == "existing"
        assert conv.digest.decisions == ["keep"]

    def test_token_estimate_is_compacted_conversation_estimate(self):
        conv = _big_conv()
        DefaultContextStrategy().compact(conv, token_budget=1500, keep_first=3)
        assert conv.digest is not None
        assert conv.digest.token_estimate == conv.get_token_estimate()

    def test_raw_summary_computed_before_fitting(self):
        raw_summary = (
            "<task>Fix the flaky test</task>\n"
            "<decisions>- Use pytest-timeout " + "x" * 20000 + "</decisions>\n"
            "<files>tests/test_x.py, src/x.py</files>\n"
            "<open_questions>- Windows CI?</open_questions>"
        )
        conv = _big_conv()
        LLMSummarizeStrategy(llm_provider=lambda sys_p, user_p: raw_summary).compact(
            conv, token_budget=1200, keep_first=3
        )
        assert conv.digest is not None
        assert conv.digest.arc == "Fix the flaky test"
        assert conv.digest.decisions
        assert conv.digest.decisions[0].startswith("Use pytest-timeout")
        assert conv.digest.paths_touched == ["tests/test_x.py", "src/x.py"]
        assert conv.digest.open_questions == ["Windows CI?"]
        # the fitted text lost the tags (budget too small); the digest was
        # computed from the raw summary so it kept the structured fields
        summaries = [m for m in conv.messages if m.is_summary]
        assert summaries
        assert "<decisions>" not in summaries[0].content

    def test_llm_summary_returned_verbatim_c12(self):
        summary = "<task>Fix the bug</task><progress>Step 1 done</progress>"
        strat = LLMSummarizeStrategy(llm_provider=lambda sys_p, user_p: summary)
        conv = _big_conv()
        assert strat._summary(conv, token_budget=1000, keep_first=2) == summary


# ── Persistence round trip ────────────────────────────────────────────────────


class TestDigestPersistence:
    def test_roundtrip_digest_survives(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        from agent.persistence import (
            load_conversation_from_disk,
            save_conversation_to_disk,
        )

        conv = Conversation(agent_name="Coder", model="openai/gpt-4o", provider="openai")
        conv.digest = ConversationDigest(
            arc="fix auth",
            decisions=["use jwt"],
            open_questions=["SSO?"],
            paths_touched=["src/auth.py"],
            attempts_rejected=["FAILED build"],
            token_estimate=123,
        )
        save_conversation_to_disk(conv, "special:digest")
        loaded, data = load_conversation_from_disk("special:digest")
        assert loaded is not None
        assert data["digest"]["arc"] == "fix auth"
        assert loaded.digest is not None
        assert loaded.digest.arc == "fix auth"
        assert loaded.digest.decisions == ["use jwt"]
        assert loaded.digest.open_questions == ["SSO?"]
        assert loaded.digest.paths_touched == ["src/auth.py"]
        assert loaded.digest.attempts_rejected == ["FAILED build"]
        assert loaded.digest.token_estimate == 123

    def test_empty_digest_does_not_load_as_object(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        from agent.persistence import (
            load_conversation_from_disk,
            save_conversation_to_disk,
        )

        conv = Conversation(agent_name="Coder", model="openai/gpt-4o", provider="openai")
        conv.digest = ConversationDigest()  # empty
        save_conversation_to_disk(conv, "special:emptydigest")
        loaded, _data = load_conversation_from_disk("special:emptydigest")
        assert loaded is not None
        assert loaded.digest is None

    def test_no_digest_field_loads_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        from agent.persistence import (
            load_conversation_from_disk,
            save_conversation_to_disk,
        )

        conv = Conversation(agent_name="Coder", model="openai/gpt-4o", provider="openai")
        save_conversation_to_disk(conv, "special:nodigest")
        loaded, data = load_conversation_from_disk("special:nodigest")
        assert loaded is not None
        assert loaded.digest is None
        assert data.get("digest") is None


# ── SPEC-48 Phase 3 fix tests (F1–F4) ─────────────────────────────────────────


class TestSpec48Phase3Fixes:
    def test_f1_load_gate_only_constraints_populated(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        from agent.persistence import (
            load_conversation_from_disk,
            save_conversation_to_disk,
        )

        conv = Conversation(agent_name="Coder", model="openai/gpt-4o", provider="openai")
        conv.digest = ConversationDigest(constraints=["must support offline mode"])
        save_conversation_to_disk(conv, "fix:f1onlyconstraints")
        loaded, data = load_conversation_from_disk("fix:f1onlyconstraints")
        assert loaded is not None
        assert loaded.digest is not None
        assert loaded.digest.constraints == ["must support offline mode"]
        assert loaded.digest.arc == ""
        assert data["digest"]["constraints"] == ["must support offline mode"]

    def test_f1_load_gate_only_token_estimate_populated(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        from agent.persistence import (
            load_conversation_from_disk,
            save_conversation_to_disk,
        )

        conv = Conversation(agent_name="Coder", model="openai/gpt-4o", provider="openai")
        conv.digest = ConversationDigest(token_estimate=321)
        save_conversation_to_disk(conv, "fix:f1onlytokens")
        loaded, _data = load_conversation_from_disk("fix:f1onlytokens")
        assert loaded is not None
        assert loaded.digest is not None
        assert loaded.digest.token_estimate == 321

    def test_f2_from_dict_bad_token_estimate_returns_zero(self):
        d = ConversationDigest.from_dict({"token_estimate": "abc"})
        assert d.token_estimate == 0
        assert d.arc == ""

    def test_f2_from_dict_none_token_estimate_returns_zero(self):
        d = ConversationDigest.from_dict({"token_estimate": None})
        assert d.token_estimate == 0

    def test_f3_save_plain_dict_digest_writes_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
        from agent.persistence import (
            load_conversation_from_disk,
            save_conversation_to_disk,
        )

        conv = Conversation(agent_name="Coder", model="openai/gpt-4o", provider="openai")
        conv.digest = {"arc": "x"}  # plain dict, not a ConversationDigest
        save_conversation_to_disk(conv, "fix:f3plainict")
        loaded, data = load_conversation_from_disk("fix:f3plainict")
        assert loaded is not None
        assert data["digest"] is None
        assert loaded.digest is None

    def test_f4_nested_tag_literal_cannot_corrupt_arc(self):
        summary = "<decisions>we discussed the <task> marker</decisions><task>real task</task>"
        d = _digest_from_tagged_summary(summary)
        assert d.arc == "real task"
        assert d.decisions == ["we discussed the <task> marker"]

    def test_f4_all_nine_tags_still_parse(self):
        summary = (
            "<task>T</task>"
            "<progress>P</progress>"
            "<files>f.py</files>"
            "<decisions>D</decisions>"
            "<constraints>C</constraints>"
            "<errors>E</errors>"
            "<open_questions>Q</open_questions>"
            "<next_steps>N</next_steps>"
            "<user_preferences>U</user_preferences>"
        )
        d = _digest_from_tagged_summary(summary)
        assert d.arc == "T"
        assert d.decisions == ["D"]
        assert d.constraints == ["C"]
        assert d.open_questions == ["Q"]
        assert d.paths_touched == ["f.py"]
        assert d.attempts_rejected == ["E"]
