# tests/test_spec48_phase2_offload.py
# SPEC-48 Phase 2: the offload hook in the compaction path.
#
# Phase 1 shipped the store (utils/tool_output_store.py) and the
# read_tool_output tool; Phase 2 wires offloading into the two stubbing
# sites (prune_tool_outputs, panic_prune) through the runtime-supplied
# offload callback, plus the config fields that resolve the threshold.
#
# Contracts exercised here:
#   - over-threshold body → written under <project>/.crabcakes/tool-outputs/
#     and replaced by the path-carrying stub (the [compacted — prefix kept)
#   - under-threshold and no-project stubs are byte-identical to the
#     pre-SPEC-48 plain stub (C1, C2, C19)
#   - threshold 0 disables offloading entirely
#   - per-provider > global > 0 resolution order
from __future__ import annotations

import os

from agent.context_strategy import (
    DefaultContextStrategy,
    LLMSummarizeStrategy,
    OffloadRequest,
)
from agent.runtime import AgentRuntime
from models.conversation import Conversation, MessageRole, ToolCall
from tests.test_agent_runtime import _make_cfg
from utils.tool_output_store import read_tool_output

_OFFLOAD_MARKER = "→ offloaded to .crabcakes/tool-outputs/"


def _exec(conv: Conversation, call_id: str, body: str, tool_name: str = "exec_command") -> None:
    tc = ToolCall(
        call_id=call_id,
        tool_name=tool_name,
        arguments={"command": "echo hi"},
        result=body,
    )
    conv.add_assistant_message("go", [tc])
    conv.add_tool_result(call_id, body)


def _tool_bodies(conv):
    return [m.content for m in conv.messages if m.role == MessageRole.TOOL_RESULT]


def _tool_result_msg(conv):
    return next(m for m in conv.messages if m.role == MessageRole.TOOL_RESULT)


def _runtime(tmp_path, threshold: int = 8192, provider_threshold: int = 0):
    cfg = _make_cfg()
    cfg.tool_output_offload_chars = threshold
    if provider_threshold > 0:
        cfg.providers["openai"].tool_output_offload_chars = provider_threshold
    rt = AgentRuntime(cfg)
    sk = "spec48:phase2"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    return rt, sk, rt.get_conversation(sk)


# ═══════════════════════════════════════════════════════════════════
#  The dataclass and the strategy helper
# ═══════════════════════════════════════════════════════════════════

class TestOffloadRequest:
    def test_carries_tool_name_and_content(self):
        req = OffloadRequest(tool_name="exec_command", content="body")
        assert req.tool_name == "exec_command"
        assert req.content == "body"


class TestOffloadedStub:
    def test_prefix_and_path_marker(self):
        stub = DefaultContextStrategy._offloaded_stub(
            "exec_command", 500, ".crabcakes/tool-outputs/exec_command-ab12cd34.txt"
        )
        assert stub.startswith("[compacted \u2014 exec_command 500 ")
        assert "→ offloaded to .crabcakes/tool-outputs/exec_command-ab12cd34.txt" in stub
        assert "(read it back with read_tool_output)" in stub


class TestStubToolResultHelper:
    def test_offload_meta_wins_over_plain_stub(self):
        calls = []
        def offload(conv, request):
            calls.append(request)
            return {"path": ".crabcakes/tool-outputs/exec_command-ab12cd34.txt"}
        strat = DefaultContextStrategy(offload=offload)
        conv = Conversation(agent_name="Coder", model="openai/gpt-4o")
        _exec(conv, "c0", "E" * 500)
        msg = conv.messages[1]
        stub = strat._stub_tool_result(conv, msg, "exec_command")
        assert _OFFLOAD_MARKER in stub
        assert calls and calls[0].tool_name == "exec_command"
        assert calls[0].content == "E" * 500

    def test_none_meta_falls_back_to_plain_stub(self):
        strat = DefaultContextStrategy(offload=lambda conv, request: None)
        conv = Conversation(agent_name="Coder", model="openai/gpt-4o")
        _exec(conv, "c0", "E" * 500)
        stub = strat._stub_tool_result(conv, conv.messages[1], "exec_command")
        assert stub == "[compacted \u2014 exec_command 500]"

    def test_no_offload_callback_uses_plain_stub(self):
        strat = DefaultContextStrategy()
        assert strat._offload is None
        conv = Conversation(agent_name="Coder", model="openai/gpt-4o")
        _exec(conv, "c0", "E" * 500)
        stub = strat._stub_tool_result(conv, conv.messages[1], "exec_command")
        assert stub == "[compacted \u2014 exec_command 500]"

    def test_llm_strategy_forwards_offload(self):
        calls = []
        def offload(conv, request):
            calls.append(request)
            return {"path": ".crabcakes/tool-outputs/exec_command-ab12cd34.txt"}
        strat = LLMSummarizeStrategy(llm_provider=None, offload=offload)
        conv = Conversation(agent_name="Coder", model="openai/gpt-4o")
        _exec(conv, "c0", "E" * 500)
        stub = strat._stub_tool_result(conv, conv.messages[1], "exec_command")
        assert _OFFLOAD_MARKER in stub
        assert calls and calls[0].tool_name == "exec_command"


# ═══════════════════════════════════════════════════════════════════
#  The runtime callback and threshold resolution
# ═══════════════════════════════════════════════════════════════════

class TestOffloadCallback:
    def test_oversized_body_is_offloaded(self, tmp_path):
        rt, _sk, conv = _runtime(tmp_path, threshold=100)
        req = OffloadRequest(tool_name="exec_command", content="E" * 500)
        meta = rt._offload_tool_output(conv, req)
        assert meta is not None
        assert meta["path"].startswith(".crabcakes/tool-outputs/exec_command-")
        full = os.path.join(str(tmp_path), meta["path"])
        assert os.path.isfile(full)
        with open(full, encoding="utf-8") as fh:
            assert fh.read() == "E" * 500

    def test_under_threshold_returns_none(self, tmp_path):
        rt, _sk, conv = _runtime(tmp_path, threshold=100)
        req = OffloadRequest(tool_name="exec_command", content="E" * 50)
        assert rt._offload_tool_output(conv, req) is None

    def test_no_project_returns_none(self):
        cfg = _make_cfg()
        cfg.tool_output_offload_chars = 100
        rt = AgentRuntime(cfg)
        conv = Conversation(agent_name="Coder", model="openai/gpt-4o")
        req = OffloadRequest(tool_name="exec_command", content="E" * 5000)
        assert rt._offload_tool_output(conv, req) is None

    def test_zero_threshold_returns_none(self, tmp_path):
        rt, _sk, conv = _runtime(tmp_path, threshold=0)
        req = OffloadRequest(tool_name="exec_command", content="E" * 5000)
        assert rt._offload_tool_output(conv, req) is None

    def test_write_failure_falls_back_to_none(self, tmp_path):
        # A regular file blocking the store dir makes the write fail; the
        # callback must return None (never raise).
        rt, _sk, conv = _runtime(tmp_path, threshold=100)
        (tmp_path / ".crabcakes").mkdir()
        (tmp_path / ".crabcakes" / "tool-outputs").write_text("blocker")
        req = OffloadRequest(tool_name="exec_command", content="E" * 500)
        assert rt._offload_tool_output(conv, req) is None


class TestResolveOffloadThreshold:
    def test_global_default_used_when_no_provider_override(self, tmp_path):
        rt, _sk, conv = _runtime(tmp_path, threshold=8192, provider_threshold=0)
        assert rt._resolve_offload_threshold(conv) == 8192

    def test_per_provider_beats_global(self, tmp_path):
        rt, _sk, conv = _runtime(tmp_path, threshold=8192, provider_threshold=50)
        assert rt._resolve_offload_threshold(conv) == 50

    def test_global_beats_zero(self, tmp_path):
        rt, _sk, conv = _runtime(tmp_path, threshold=100, provider_threshold=0)
        assert rt._resolve_offload_threshold(conv) == 100

    def test_zero_global_disables(self, tmp_path):
        rt, _sk, conv = _runtime(tmp_path, threshold=0, provider_threshold=0)
        assert rt._resolve_offload_threshold(conv) == 0


# ═══════════════════════════════════════════════════════════════════
#  The compaction call sites (prune_tool_outputs, panic_prune)
# ═══════════════════════════════════════════════════════════════════

class TestOffloadInPruneToolOutputs:
    def test_oversized_result_is_offloaded_and_stub_carries_path(self, tmp_path):
        rt, _sk, conv = _runtime(tmp_path, threshold=100)
        _exec(conv, "c0", "E" * 500)
        rt._context_strategy.prune_tool_outputs(conv, target_tokens=0, protect_turns=0)
        body = _tool_bodies(conv)[0]
        assert body.startswith("[compacted \u2014 exec_command 500 ")
        assert _OFFLOAD_MARKER in body
        rel = _rel_from_stub(body)
        result = read_tool_output(str(tmp_path), rel)
        assert result["found"] is True
        assert result["output"] == "E" * 500
        # The parent call's duplicate body string is dropped (C9).
        assert conv.messages[0].tool_calls[0].result is None
        # tokens_used reflects the ACTUAL stub text (C8), not the old body.
        msg = _tool_result_msg(conv)
        assert msg.tokens_used == len(body) // 4
        assert msg.tokens_used != len("E" * 500) // 4

    def test_under_threshold_keeps_plain_stub_byte_identical(self, tmp_path):
        rt, _sk, conv = _runtime(tmp_path, threshold=8192)
        _exec(conv, "c0", "E" * 50)
        rt._context_strategy.prune_tool_outputs(conv, target_tokens=0, protect_turns=0)
        body = _tool_bodies(conv)[0]
        assert body == "[compacted \u2014 exec_command 50]"
        assert not os.path.exists(os.path.join(str(tmp_path), ".crabcakes", "tool-outputs"))

    def test_no_project_keeps_plain_stub_byte_identical(self):
        cfg = _make_cfg()
        cfg.tool_output_offload_chars = 100
        rt = AgentRuntime(cfg)
        sk = "spec48:phase2:noproject"
        rt.create_conversation("Coder", sk, None, defer_prompt_build=True)
        conv = rt.get_conversation(sk)
        assert conv.project_path is None
        _exec(conv, "c0", "E" * 5000)
        rt._context_strategy.prune_tool_outputs(conv, target_tokens=0, protect_turns=0)
        body = _tool_bodies(conv)[0]
        assert body == "[compacted \u2014 exec_command 5000]"

    def test_zero_threshold_disables_offloading(self, tmp_path):
        rt, _sk, conv = _runtime(tmp_path, threshold=0)
        _exec(conv, "c0", "E" * 50000)
        rt._context_strategy.prune_tool_outputs(conv, target_tokens=0, protect_turns=0)
        body = _tool_bodies(conv)[0]
        assert body == "[compacted \u2014 exec_command 50000]"
        assert not os.path.exists(os.path.join(str(tmp_path), ".crabcakes", "tool-outputs"))

    def test_per_provider_threshold_controls_offload(self, tmp_path):
        # global 8192, per-provider 100 → a 500-char body offloads even
        # though it is well under the global threshold.
        rt, _sk, conv = _runtime(tmp_path, threshold=8192, provider_threshold=100)
        _exec(conv, "c0", "E" * 500)
        rt._context_strategy.prune_tool_outputs(conv, target_tokens=0, protect_turns=0)
        body = _tool_bodies(conv)[0]
        assert _OFFLOAD_MARKER in body

    def test_plain_stub_path_byte_identical_to_default_strategy(self, tmp_path):
        rt, _sk, conv = _runtime(tmp_path, threshold=8192)
        _exec(conv, "c0", "E" * 50)
        rt._context_strategy.prune_tool_outputs(conv, target_tokens=0, protect_turns=0)
        offloaded_path_body = _tool_bodies(conv)[0]

        baseline = Conversation(agent_name="Coder", model="openai/gpt-4o")
        _exec(baseline, "c0", "E" * 50)
        DefaultContextStrategy().prune_tool_outputs(baseline, target_tokens=0, protect_turns=0)
        assert offloaded_path_body == _tool_bodies(baseline)[0]
        assert offloaded_path_body == "[compacted \u2014 exec_command 50]"


class TestOffloadInPanicPrune:
    def test_panic_prune_offloads_previous_turn_result(self, tmp_path):
        rt, _sk, conv = _runtime(tmp_path, threshold=100)
        conv.add_user_message("old")
        _exec(conv, "c0", "E" * 500)
        conv.add_user_message("new")
        n = rt._context_strategy.panic_prune(conv, protect_paths=())
        assert n == 1
        bodies = _tool_bodies(conv)
        assert bodies[0].startswith("[compacted \u2014 exec_command 500 ")
        assert _OFFLOAD_MARKER in bodies[0]
        result = read_tool_output(str(tmp_path), _rel_from_stub(bodies[0]))
        assert result["found"] is True
        assert result["output"] == "E" * 500
        # The plain-stub phrases stay out (C3).
        assert "chars removed" not in bodies[0]
        assert "re-run to restore" not in bodies[0]
        assert "re-read to restore" not in bodies[0]

    def test_panic_prune_under_threshold_plain_stub(self, tmp_path):
        rt, _sk, conv = _runtime(tmp_path, threshold=8192)
        conv.add_user_message("old")
        _exec(conv, "c0", "E" * 50)
        conv.add_user_message("new")
        n = rt._context_strategy.panic_prune(conv, protect_paths=())
        assert n == 1
        bodies = _tool_bodies(conv)
        assert bodies[0] == "[compacted \u2014 exec_command 50]"
        assert not os.path.exists(os.path.join(str(tmp_path), ".crabcakes", "tool-outputs"))


def _rel_from_stub(stub: str) -> str:
    """Extract the project-relative store path from an offloaded stub."""
    marker = "→ offloaded to "
    assert marker in stub
    tail = stub.split(marker, 1)[1]
    return tail.split(" (read it back with read_tool_output)]", 1)[0]
