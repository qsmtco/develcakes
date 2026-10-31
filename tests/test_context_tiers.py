"""SPEC-59 §2.5: context tiers — offload early, trim in the middle, panic at the provider line.

Two layers:
  1. Strategy-unit tests — drive DefaultContextStrategy(offload=...) WITHOUT the
     runtime. These pass in the strategy worktree on their own.
  2. The 10 tier cases from §2.5 — these need Coder's runtime half.
"""
from __future__ import annotations

from agent.context_strategy import DefaultContextStrategy, OffloadRequest
from agent.runtime import AgentRuntime
from models.conversation import Conversation, MessageRole, ToolCall
from tests.test_agent_runtime import _make_cfg

_OFFLOAD_MARKER = "→ offloaded to .crabcakes/tool-outputs/"


# ═══════════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════════

def _tool(conv, call_id, body, tool_name="read_file", path="a.py"):
    """Append an assistant tool-call message plus its tool result."""
    args = {"path": path} if path else {"command": "echo hi"}
    tc = ToolCall(call_id=call_id, tool_name=tool_name, arguments=args, result=body)
    conv.add_assistant_message("go", [tc])
    conv.add_tool_result(call_id, body)


def _bodies(conv):
    return [m.content for m in conv.messages if m.role == MessageRole.TOOL_RESULT]


def _fake_offload(path=".crabcakes/tool-outputs/x-ab12cd34.txt"):
    """Fake runtime callback: records requests, returns a store path."""
    calls = []

    def offload(conv, request):
        calls.append(request)
        return {"path": path}

    offload.calls = calls
    return offload


def _conv(*, system_prompt="") -> Conversation:
    return Conversation(agent_name="Coder", model="openai/gpt-4o", system_prompt=system_prompt)


# ═══════════════════════════════════════════════════════════════════
#  Layer 1 — strategy-unit tests (no runtime). MUST pass pre-merge.
# ═══════════════════════════════════════════════════════════════════

class TestOffloadRequestField:
    def test_min_chars_defaults_to_none(self):
        req = OffloadRequest(tool_name="exec_command", content="body")
        assert req.min_chars is None

    def test_positional_construction_still_works(self):
        req = OffloadRequest("exec_command", "body")
        assert req.tool_name == "exec_command"
        assert req.content == "body"
        assert req.min_chars is None


class TestStubToolResult:
    def test_offloaded_meta_wins(self):
        strat = DefaultContextStrategy(offload=_fake_offload())
        conv = _conv()
        _tool(conv, "c0", "E" * 500)
        stub = strat._stub_tool_result(conv, conv.messages[1], "exec_command")
        assert _OFFLOAD_MARKER in stub

    def test_non_dict_meta_is_a_refusal(self):
        strat = DefaultContextStrategy(offload=lambda c, r: ["not", "a", "dict"])
        conv = _conv()
        _tool(conv, "c0", "E" * 500)
        stub = strat._stub_tool_result(conv, conv.messages[1], "exec_command", allow_lossy=False)
        assert stub is None

    def test_empty_path_is_a_refusal(self):
        strat = DefaultContextStrategy(offload=lambda c, r: {"path": ""})
        conv = _conv()
        _tool(conv, "c0", "E" * 500)
        assert strat._stub_tool_result(conv, conv.messages[1], "exec_command", allow_lossy=False) is None

    def test_missing_path_key_is_a_refusal(self):
        strat = DefaultContextStrategy(offload=lambda c, r: {"bytes": 5})
        conv = _conv()
        _tool(conv, "c0", "E" * 500)
        assert strat._stub_tool_result(conv, conv.messages[1], "exec_command", allow_lossy=False) is None

    def test_allow_lossy_false_returns_none_and_keeps_the_plain_stub_available(self):
        strat = DefaultContextStrategy(offload=lambda c, r: None)
        conv = _conv()
        _tool(conv, "c0", "E" * 500)
        assert strat._stub_tool_result(conv, conv.messages[1], "exec_command") == "[compacted \u2014 exec_command 500]"
        assert strat._stub_tool_result(conv, conv.messages[1], "exec_command", allow_lossy=False) is None

    def test_min_chars_rides_on_the_request(self):
        offload = _fake_offload()
        strat = DefaultContextStrategy(offload=offload)
        conv = _conv()
        _tool(conv, "c0", "E" * 500)
        strat._stub_tool_result(conv, conv.messages[1], "exec_command", min_chars=500)
        assert offload.calls[0].min_chars == 500


class TestPruneToolOutputsAllowLossy:
    def test_refused_offload_leaves_body_verbatim(self):
        strat = DefaultContextStrategy(offload=lambda c, r: None)
        conv = _conv()
        _tool(conv, "c0", "E" * 500)
        strat.prune_tool_outputs(conv, target_tokens=0, protect_turns=0, allow_lossy_stub=False)
        assert _bodies(conv)[0] == "E" * 500

    def test_default_still_writes_the_lossy_stub(self):
        strat = DefaultContextStrategy(offload=lambda c, r: None)
        conv = _conv()
        _tool(conv, "c0", "E" * 500, tool_name="exec_command", path="")
        strat.prune_tool_outputs(conv, target_tokens=0, protect_turns=0)
        assert _bodies(conv)[0] == "[compacted \u2014 exec_command 500]"


class TestCompactAllowLossyStub:
    def test_false_skips_prune_but_still_trims(self):
        strat = DefaultContextStrategy(offload=lambda c, r: None)
        conv = _conv(system_prompt="S" * 4000)
        conv.add_user_message("old request")
        conv.add_assistant_message("old answer " + ("A" * 800))
        conv.add_user_message("current request")
        conv.add_assistant_message("current answer " + ("C" * 800))
        # The trim floor is keep_first(2) + tail_preserve(4) = 6 messages, so
        # a 4-message conversation can never trim. Pad past the floor.
        for i in range(4):
            conv.add_user_message(f"older {i} " + ("U" * 300))
            conv.add_assistant_message(f"older ans {i} " + ("W" * 300))
        before = len(conv.messages)
        strat.compact(conv, token_budget=1, allow_lossy_stub=False)
        # No lossy stub anywhere.
        assert not any(b.startswith("[compacted \u2014") for b in _bodies(conv))
        # The trim loop still ran.
        assert len(conv.messages) < before


class TestOffloadToolResults:
    def test_target_tokens_zero_returns_zero(self):
        strat = DefaultContextStrategy(offload=_fake_offload())
        conv = _conv()
        _tool(conv, "c0", "E" * 500)
        assert strat.offload_tool_results(conv, target_tokens=0, keep_last=0) == 0

    def test_under_target_returns_zero(self):
        strat = DefaultContextStrategy(offload=_fake_offload())
        conv = _conv()
        _tool(conv, "c0", "E" * 500)
        assert strat.offload_tool_results(conv, target_tokens=10_000_000, keep_last=0) == 0

    def test_keeps_last_protects_everything(self):
        strat = DefaultContextStrategy(offload=_fake_offload())
        conv = _conv()
        _tool(conv, "c0", "E" * 500)
        assert strat.offload_tool_results(conv, target_tokens=1, keep_last=6) == 0
        assert _bodies(conv)[0] == "E" * 500

    def test_oldest_is_offloaded_and_counted(self):
        strat = DefaultContextStrategy(offload=_fake_offload())
        conv = _conv()
        _tool(conv, "c0", "E" * 500)
        _tool(conv, "c1", "F" * 500)
        n = strat.offload_tool_results(conv, target_tokens=1, keep_last=1)
        assert n == 1
        assert _OFFLOAD_MARKER in _bodies(conv)[0]
        assert _bodies(conv)[1] == "F" * 500

    def test_never_writes_a_lossy_stub(self):
        strat = DefaultContextStrategy(offload=lambda c, r: None)
        conv = _conv()
        _tool(conv, "c0", "E" * 500)
        strat.offload_tool_results(conv, target_tokens=1, keep_last=0)
        assert _bodies(conv)[0] == "E" * 500

    def test_already_stubbed_body_is_not_sent_to_the_callback(self):
        offload = _fake_offload()
        strat = DefaultContextStrategy(offload=offload)
        conv = _conv()
        _tool(conv, "c0", "[compacted \u2014 read_file 500]")
        strat.offload_tool_results(conv, target_tokens=1, keep_last=0)
        assert offload.calls == []

    def test_masked_body_is_skipped(self):
        offload = _fake_offload()
        strat = DefaultContextStrategy(offload=offload)
        conv = _conv()
        _tool(conv, "c0", "[masked \u2014 read_file 500]")
        strat.offload_tool_results(conv, target_tokens=1, keep_last=0)
        assert offload.calls == []

    def test_tool_names_filter_is_exact(self):
        offload = _fake_offload()
        strat = DefaultContextStrategy(offload=offload)
        conv = _conv()
        _tool(conv, "c0", "E" * 500, tool_name="exec_command", path="")
        strat.offload_tool_results(
            conv, target_tokens=1, keep_last=0, tool_names=("read_file", "search_files"),
        )
        assert offload.calls == []

    def test_tool_names_filter_allows_a_match(self):
        offload = _fake_offload()
        strat = DefaultContextStrategy(offload=offload)
        conv = _conv()
        _tool(conv, "c0", "E" * 500, tool_name="read_file", path="a.py")
        strat.offload_tool_results(
            conv, target_tokens=1, keep_last=0, tool_names=("read_file", "search_files"),
        )
        assert len(offload.calls) == 1

    def test_protected_path_is_skipped(self):
        offload = _fake_offload()
        strat = DefaultContextStrategy(offload=offload)
        conv = _conv()
        _tool(conv, "c0", "E" * 500, tool_name="read_file", path="a.py")
        strat.offload_tool_results(
            conv, target_tokens=1, keep_last=0, protect_paths=("a.py",),
        )
        assert offload.calls == []

    def test_empty_path_is_not_protected(self):
        offload = _fake_offload()
        strat = DefaultContextStrategy(offload=offload)
        conv = _conv()
        _tool(conv, "c0", "E" * 500, tool_name="exec_command", path="")
        strat.offload_tool_results(
            conv, target_tokens=1, keep_last=0, protect_paths=("a.py",),
        )
        assert len(offload.calls) == 1

    def test_min_index_skips_older_indexes_and_they_do_not_count_toward_keep_last(self):
        offload = _fake_offload()
        strat = DefaultContextStrategy(offload=offload)
        conv = _conv()
        # Three tools -> result indexes 1, 3, 5. min_index=2 excludes index 1
        # (which must stay verbatim AND must not consume keep_last); indexes
        # 3 and 5 are candidates, keep_last=1 protects index 5 -> index 3 goes.
        _tool(conv, "c0", "E" * 500, tool_name="read_file", path="a.py")
        _tool(conv, "c1", "F" * 500, tool_name="read_file", path="b.py")
        _tool(conv, "c2", "G" * 500, tool_name="read_file", path="c.py")
        n = strat.offload_tool_results(conv, target_tokens=1, keep_last=1, min_index=2)
        assert n == 1
        assert _bodies(conv)[0] == "E" * 500
        assert _OFFLOAD_MARKER in _bodies(conv)[1]
        assert _bodies(conv)[2] == "G" * 500

    def test_message_count_does_not_change(self):
        strat = DefaultContextStrategy(offload=_fake_offload())
        conv = _conv()
        _tool(conv, "c0", "E" * 500)
        _tool(conv, "c1", "F" * 500)
        before = len(conv.messages)
        strat.offload_tool_results(conv, target_tokens=1, keep_last=0)
        assert len(conv.messages) == before

    def test_last_result_is_not_touched(self):
        strat = DefaultContextStrategy(offload=_fake_offload())
        conv = _conv()
        _tool(conv, "c0", "E" * 500)
        strat.offload_tool_results(conv, target_tokens=1, keep_last=0)
        assert strat.last_result is None

    def test_user_message_is_never_a_candidate(self):
        strat = DefaultContextStrategy(offload=_fake_offload())
        conv = _conv()
        conv.add_user_message("keep me")
        _tool(conv, "c0", "E" * 500)
        strat.offload_tool_results(conv, target_tokens=1, keep_last=0)
        assert any(m.content == "keep me" for m in conv.messages if m.role == MessageRole.USER)

    def test_min_chars_is_forwarded(self):
        offload = _fake_offload()
        strat = DefaultContextStrategy(offload=offload)
        conv = _conv()
        _tool(conv, "c0", "E" * 500)
        strat.offload_tool_results(conv, target_tokens=1, keep_last=0, min_chars=500)
        assert offload.calls[0].min_chars == 500


# ═══════════════════════════════════════════════════════════════════
#  Layer 2 — the 10 tier cases from SPEC-59 §2.5.
#  These need Coder's runtime half (post-merge).
# ═══════════════════════════════════════════════════════════════════

def _rt_with_lines(tmp_path, *, context_window=10_000, threshold=0.80):
    """Runtime whose provider gives early/middle/panic lines."""
    cfg = _make_cfg()
    cfg.providers["openai"].context_window = context_window
    cfg.providers["openai"].compaction_threshold = threshold
    cfg.tool_output_offload_chars = 100
    rt = AgentRuntime(cfg)
    sk = "spec59:tiers"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    return rt, sk, rt.get_conversation(sk)


def _packet(conv, wire_from: int, goal: str = "small goal") -> dict:
    return {
        "goal": goal,
        "path": "a.py",
        "hunk": "hunk",
        "checks": [],
        "after": len(conv.messages),
        "wire_from": wire_from,
        "hot": (),
    }


def test_case1_under_the_early_line(tmp_path, monkeypatch):
    """Case 1: pressure at or under the early line — no offload, no panic."""
    rt, sk, conv = _rt_with_lines(tmp_path)
    conv.get_token_estimate = lambda: 1_000
    called = {"offload": 0, "panic": 0, "budget": None, "kwargs": None}

    def offload(*a, **k):
        called["offload"] += 1
        return 0

    def panic(*a, **k):
        called["panic"] += 1
        return 0

    def compact(_conv, budget, **kwargs):
        called["budget"] = budget
        called["kwargs"] = kwargs

    monkeypatch.setattr(rt._context_strategy, "offload_tool_results", offload)
    monkeypatch.setattr(rt._context_strategy, "panic_prune", panic)
    monkeypatch.setattr(rt._context_strategy, "compact", compact)
    soft, hard = rt._compute_compaction_threshold(conv)
    rt._compact_before_llm(sk, conv, soft, hard)
    assert called["offload"] == 0
    assert called["panic"] == 0
    assert called["budget"] == soft
    assert called["kwargs"] == {}


def test_case2_early_band_offloads_old_bodies(tmp_path, monkeypatch):
    """Case 2: pressure in (early, middle] — the old body is offloaded."""
    rt, sk, conv = _rt_with_lines(tmp_path)
    conv.get_token_estimate = lambda: 2_000
    conv.add_user_message("go")
    _tool(conv, "c0", "B" * 9_000)
    for i in range(1, 7):
        _tool(conv, f"n{i}", f"N{i}" * 10)
    before_len = len(conv.messages)
    rt._compact_before_llm(sk, conv, *rt._compute_compaction_threshold(conv))
    bodies = _bodies(conv)
    assert bodies[0].startswith("[compacted \u2014")
    assert ".crabcakes/tool-outputs/" in bodies[0]
    assert len(conv.messages) == before_len
    # The newest six bodies are unchanged.
    assert bodies[-6:] == [f"N{i}" * 10 for i in range(1, 7)]


def test_case3_store_disabled_leaves_the_body(tmp_path, monkeypatch):
    """Case 3: offload disabled — the 9_000-char body is unchanged."""
    rt, sk, conv = _rt_with_lines(tmp_path)
    rt._config.tool_output_offload_chars = 0
    conv.get_token_estimate = lambda: 2_000
    conv.add_user_message("go")
    _tool(conv, "c0", "B" * 9_000)
    for i in range(1, 7):
        _tool(conv, f"n{i}", f"N{i}" * 10)
    rt._compact_before_llm(sk, conv, *rt._compute_compaction_threshold(conv))
    assert _bodies(conv)[0] == "B" * 9_000


def test_case4_middle_band(tmp_path, monkeypatch):
    """Case 4: pressure in (middle, soft] — read_file over 500 chars goes."""
    rt, sk, conv = _rt_with_lines(tmp_path)
    conv.get_token_estimate = lambda: 3_000
    conv.add_user_message("go")
    _tool(conv, "c0", "R" * 600, tool_name="read_file", path="a.py")
    _tool(conv, "c1", "X" * 600, tool_name="exec_command", path="")
    for i in range(1, 7):
        _tool(conv, f"n{i}", f"N{i}" * 10)
    # Flagger HIGH #1: post-offload the estimate is far under
    # transcript_target, so the real compact no-ops and the kwargs could be
    # dropped silently. Record the call and pin the middle band's shape.
    soft, hard = rt._compute_compaction_threshold(conv)
    _msgs, pressure = rt._wire_messages_and_count(sk, conv, hard)
    transcript_target = max(
        1, rt._compute_landing_goal(hard) - max(0, pressure - 3_000),
    )
    assert 2_500 < pressure <= soft  # fixture is genuinely in the middle band
    compact_calls = []
    monkeypatch.setattr(
        rt._context_strategy, "compact",
        lambda c, budget, **kw: compact_calls.append((budget, kw)),
    )
    rt._compact_before_llm(sk, conv, soft, hard)
    bodies = _bodies(conv)
    assert "offloaded to" in bodies[0]
    # §2.1 second pass: ALL tools at the resolved threshold (100 chars in
    # this fixture) — exec_command (600) is offloaded too, never lossy-stubbed.
    assert "offloaded to" in bodies[1]
    assert not any(b == "[compacted \u2014 exec_command 600]" for b in bodies)
    # The delete form ran exactly once, with the §2.1 middle-band kwargs.
    assert len(compact_calls) == 1
    budget, kwargs = compact_calls[0]
    assert budget == transcript_target
    assert kwargs.get("preserve_current_turn") is True
    assert kwargs.get("allow_lossy_stub") is False
    assert kwargs.get("protect_paths") == ()


def test_case5_compile_packet_blocks_deletion(tmp_path, monkeypatch):
    """Case 5: a compile packet turns the middle compact into the no-op form."""
    rt, sk, conv = _rt_with_lines(tmp_path)
    conv.add_user_message("old request")
    conv.add_assistant_message("old answer " + ("A" * 600))
    # Flagger HIGH #2: the off-wire body. c0's 9,000-char tool result sits
    # BELOW wire_from, so a real packet must exclude it via min_index. The
    # old fixture kept every result on or above wire_from (so min_index=0
    # hardcoding was invisible) and its ~2400 estimate ran the EARLY band.
    _tool(conv, "c0", "B" * 9_000)
    wire_from = len(conv.messages)  # call msg 2, result msg 3 → wire_from 4
    conv.add_user_message("goal")
    # Chunky ON-wire bodies: the packet branch counts ONLY this slice
    # (get_token_estimate is bypassed for the band decision), so the band
    # is set here. 10 × 2401 chars ≈ 24k chars ≈ 6k tokens — above
    # transcript_target (4832 - goal/schemas) so the middle pass actually
    # fires, and inside (middle 2500, soft 8000] under both estimators.
    # Ten results so keep_last (6) cannot protect them all.
    _wire_body = "abcdefghij" * 240
    for i in range(1, 11):
        _tool(conv, f"n{i}", f"{i}:" + _wire_body)
    rt._compile[sk] = _packet(conv, wire_from)
    before_len = len(conv.messages)
    soft, hard = rt._compute_compaction_threshold(conv)
    _msgs, pressure = rt._wire_messages_and_count(sk, conv, hard)
    assert 2_500 < pressure <= soft  # genuinely the middle band, not early
    seen_indexes = []
    real_offload_tool_results = rt._context_strategy.offload_tool_results

    def spy_offload(c, **kwargs):
        min_index = kwargs.get("min_index", 0)
        for idx, msg in enumerate(c.messages):
            if (
                idx >= min_index
                and getattr(msg, "role", None) == MessageRole.TOOL_RESULT
                and isinstance(msg.content, str)
                and len(msg.content) > 100
            ):
                seen_indexes.append(idx)
        return real_offload_tool_results(c, **kwargs)

    monkeypatch.setattr(
        rt._context_strategy, "offload_tool_results", spy_offload,
    )
    compact_calls = []
    monkeypatch.setattr(
        rt._context_strategy, "compact",
        lambda c, budget, **kw: compact_calls.append((budget, kw)),
    )
    rt._compact_before_llm(sk, conv, soft, hard)
    bodies = _bodies(conv)
    # The off-wire result (index wire_from - 1) is never offered to the
    # offload callback and never rewritten: min_index is honored on every
    # tier pass, not just the hard-coded 0. Under a min_index=0 regression
    # this body is the oldest big candidate and both asserts below fail.
    assert wire_from - 1 not in seen_indexes
    assert bodies[0] == "B" * 9_000
    # The middle pass ran for real: the oldest unprotected ON-wire bodies
    # went to the store, while the off-wire one stayed verbatim.
    assert _OFFLOAD_MARKER in bodies[1]
    # keep_last still protects the newest six on-wire results.
    assert bodies[-6:] == [f"{i}:" + _wire_body for i in range(5, 11)]
    # Packet present → the deletion form never runs; §7 row 4: keyword-free
    # compact at max(soft, pre-offload estimate). Offload rewrites bodies in
    # place, so the transcript length is unchanged.
    assert len(compact_calls) == 1
    _budget, _kw = compact_calls[0]
    assert _kw == {}
    assert _budget >= soft
    assert len(conv.messages) == before_len


def test_case6_panic_still_panics(tmp_path, monkeypatch):
    """Case 6: over the panic line — panic_prune + compact(landing)."""
    rt, sk, conv = _rt_with_lines(tmp_path)
    conv.get_token_estimate = lambda: 10_000_000
    called = {"panic": 0, "budget": None, "preserve": None}

    def panic(*a, **k):
        called["panic"] += 1
        return 0

    def compact(_conv, budget, **kwargs):
        called["budget"] = budget
        called["preserve"] = kwargs.get("preserve_current_turn")

    monkeypatch.setattr(rt._context_strategy, "panic_prune", panic)
    monkeypatch.setattr(rt._context_strategy, "compact", compact)
    soft, hard = rt._compute_compaction_threshold(conv)
    rt._compact_before_llm(sk, conv, soft, hard)
    assert called["panic"] == 1
    assert called["budget"] == rt._compute_landing_goal(hard)
    assert called["preserve"] is True


def test_case7_protected_path(tmp_path, monkeypatch):
    """Case 7: a protected path is not offloaded; an empty path is."""
    rt, sk, conv = _rt_with_lines(tmp_path)
    rt._edited_paths[sk] = {"keep.py"}
    conv.get_token_estimate = lambda: 2_000
    conv.add_user_message("go")
    _tool(conv, "c0", "B" * 9_000, tool_name="read_file", path="keep.py")
    _tool(conv, "c1", "X" * 9_000, tool_name="exec_command", path="")
    for i in range(2, 8):
        _tool(conv, f"n{i}", f"N{i}" * 10)
    rt._compact_before_llm(sk, conv, *rt._compute_compaction_threshold(conv))
    bodies = _bodies(conv)
    assert bodies[0] == "B" * 9_000
    assert "offloaded to" in bodies[1]


def test_case8_idempotent(tmp_path, monkeypatch):
    """Case 8: an already-stubbed body is not sent to the offload callback."""
    offload = _fake_offload()
    strat = DefaultContextStrategy(offload=offload)
    conv = _conv()
    _tool(conv, "c0", "[compacted \u2014 read_file 500]")
    strat.offload_tool_results(conv, target_tokens=1, keep_last=0)
    assert offload.calls == []


def test_case9_collapsed_middle(tmp_path, monkeypatch):
    """Case 9: threshold 0.20 collapses the middle band."""
    rt, sk, conv = _rt_with_lines(tmp_path, threshold=0.20)
    called = {"panic": 0, "compact": []}

    def panic(*a, **k):
        called["panic"] += 1
        return 0

    def compact(_conv, budget, **kwargs):
        called["compact"].append((budget, kwargs))

    monkeypatch.setattr(rt._context_strategy, "panic_prune", panic)
    monkeypatch.setattr(rt._context_strategy, "compact", compact)
    soft, hard = rt._compute_compaction_threshold(conv)
    assert soft == 2_000
    conv.get_token_estimate = lambda: 1_800
    rt._compact_before_llm(sk, conv, soft, hard)
    assert called["panic"] == 0
    conv.get_token_estimate = lambda: 2_100
    rt._compact_before_llm(sk, conv, soft, hard)
    assert called["panic"] == 1
    # §7 panic shape: compact runs exactly once, aimed at the landing goal.
    panic_compacts = [c for c in called["compact"] if c[1]]
    assert len(panic_compacts) == 1
    assert panic_compacts[0][0] == rt._compute_landing_goal(hard)


def test_case10_manual_budget(tmp_path, monkeypatch):
    """Case 10: manual compact aims at the landing goal (19_200 on 128k)."""
    from agent.special_agents import SpecialAgentDef
    from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

    handler = AgentRuntimeHandler.__new__(AgentRuntimeHandler)
    handler._agents = {
        "agent:coder": SpecialAgentDef(
            conv_id_prefix="agent:coder",
            display_name="Coder",
            role="coder",
            emoji="X",
            tools=["read_file"],
            can_write=True,
        ),
    }
    rt, _sk, conv = _rt_with_lines(tmp_path, context_window=128_000)
    handler._runtimes = {"Coder": rt}
    # compact_conversation looks up rt.get_conversation("agent:coder"); the
    # fixture conversation lives under "spec59:tiers", so register it under
    # the key the handler will query.
    rt._conversations["agent:coder"] = conv
    seen = {}

    def force_compact(_conv, budget, **kwargs):
        seen["budget"] = budget

    def force_llm_compact(_conv, budget, *a, **kwargs):
        seen["budget"] = budget
        return {}

    rt.force_compact = force_compact
    rt.force_llm_compact = force_llm_compact
    handler.compact_conversation("agent:coder", "")
    assert seen["budget"] == 19_200
    assert seen["budget"] != 64_000


# ═══════════════════════════════════════════════════════════════════
#  Runtime-level _offload_tool_output (SPEC-59 §5 step 2)
# ═══════════════════════════════════════════════════════════════════

def test_runtime_min_chars_lowers_the_bar(tmp_path):
    """Flagger HIGH #3a: the 500-bar and the refusal, on the real store.

    §5 step 2: a body between the resolved threshold (100 in this fixture)
    and the §5 default 500 proves the bar moves — the same body is
    offloaded with no per-request override and refused when the request
    carries min_chars=500 > body. The conv body itself is never rewritten
    here: stubbing is the strategy's job, not the runtime callback's.
    """
    rt, _sk, conv = _rt_with_lines(tmp_path)
    conv.project_path = str(tmp_path)
    body = "M" * 150  # > resolved 100, < §5 bar 500
    _tool(conv, "c0", body)
    # min_chars=500 > body → refused.
    assert rt._offload_tool_output(
        conv, OffloadRequest(tool_name="read_file", content=body, min_chars=500),
    ) is None
    # No min_chars → the resolved 100-char bar governs → offloaded.
    meta = rt._offload_tool_output(
        conv, OffloadRequest(tool_name="read_file", content=body),
    )
    assert isinstance(meta, dict)
    assert meta["path"].startswith(".crabcakes/tool-outputs/")
    assert meta["bytes"] == 150
    assert (tmp_path / meta["path"]).read_text(encoding="utf-8") == body
    assert _bodies(conv)[0] == body


def test_runtime_disabled_store_refuses_min_chars(tmp_path):
    """Flagger HIGH #3b: tool_output_offload_chars=0 refuses min_chars=500.

    §5 step 2: the disabled check runs BEFORE min_chars is applied, so a
    positive per-request min_chars must not turn the store back on. The
    body (600) exceeds min_chars (500) so a re-enabling regression would
    write a real file; the store dir must not even exist afterwards.
    """
    rt, _sk, conv = _rt_with_lines(tmp_path)
    rt._config.tool_output_offload_chars = 0
    conv.project_path = str(tmp_path)
    body = "M" * 600
    _tool(conv, "c0", body)
    assert rt._offload_tool_output(
        conv, OffloadRequest(tool_name="read_file", content=body, min_chars=500),
    ) is None
    assert not (tmp_path / ".crabcakes" / "tool-outputs").exists()
    assert _bodies(conv)[0] == body  # stays verbatim

