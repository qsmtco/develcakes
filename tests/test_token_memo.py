"""SPEC-21 SP0: per-string token-count memo + encode_ordinary."""
from __future__ import annotations

from agent.context_strategy import DefaultContextStrategy
from models.conversation import (
    Conversation,
    ToolCall,
    _tiktoken_encoding_for,
)


def _conv(n_rounds: int = 5, *, system_prompt: str = "SYS " * 200) -> Conversation:
    conv = Conversation(
        agent_name="Coder",
        system_prompt=system_prompt,
        model="openrouter/deepseek",
    )
    body = "tool output " * 80
    for i in range(n_rounds):
        conv.add_user_message("go")
        conv.add_assistant_message(
            "ok",
            tool_calls=[
                ToolCall(
                    call_id=f"c{i}",
                    tool_name="read_file",
                    arguments={"path": f"x{i}"},
                )
            ],
        )
        conv.add_tool_result(f"c{i}", body + str(i))
    return conv


def test_endoftext_literal_does_not_raise():
    """Regression: encode() raised ValueError on <|endoftext|>. encode_ordinary must not."""
    conv = Conversation(
        agent_name="Coder",
        system_prompt="sys",
        model="openrouter/deepseek",
    )
    conv.add_tool_result("c1", "tokenizer docs: <|endoftext|> marks the end")
    conv.get_token_estimate()
    DefaultContextStrategy().compact(conv, 10**9)
    bd = conv.get_token_breakdown(128_000)
    assert bd["total_used_tokens"] > 0


def test_system_prompt_encoded_once_across_rounds(monkeypatch):
    real = _tiktoken_encoding_for("openrouter/deepseek")
    assert real is not None
    calls: list[str] = []

    class Wrap:
        name = real.name

        def encode_ordinary(self, text):
            calls.append(text)
            return real.encode_ordinary(text)

    monkeypatch.setattr(
        "models.conversation._tiktoken_encoding_for", lambda model: Wrap()
    )
    prompt = "PROMPTUNIQUE " * 50
    conv = Conversation(agent_name="Coder", system_prompt=prompt, model="openrouter/deepseek")
    for i in range(5):
        conv.add_tool_result(f"r{i}", f"result {i}")
        conv.get_token_estimate()
    assert calls.count(prompt) == 1


def test_real_compaction_encodes_only_new_strings(monkeypatch):
    real = _tiktoken_encoding_for("openrouter/deepseek")
    assert real is not None
    encoded: list[str] = []

    class Wrap:
        name = real.name

        def encode_ordinary(self, text):
            encoded.append(text)
            return real.encode_ordinary(text)

    monkeypatch.setattr(
        "models.conversation._tiktoken_encoding_for", lambda model: Wrap()
    )
    prompt = "S" * 2000
    conv = _conv(40, system_prompt=prompt)
    conv.get_token_estimate()
    encoded.clear()
    target = max(100, int(conv.get_token_estimate() * 0.4))
    encoded.clear()
    DefaultContextStrategy().compact(conv, target)
    assert prompt not in encoded, "compaction re-encoded the unchanged system prompt"


def test_breakdown_matches_independent_count_and_is_cached(monkeypatch):
    conv = _conv(8)
    bd = conv.get_token_breakdown(128_000)
    enc = _tiktoken_encoding_for(conv.model)
    assert enc is not None
    sys_t = len(enc.encode_ordinary(conv.system_prompt))
    conv_t = 0
    for msg in conv.messages:
        conv_t += len(enc.encode_ordinary(msg.content or ""))
        for tc in msg.tool_calls:
            conv_t += len(enc.encode_ordinary(str(tc.arguments)))
            if tc.result:
                conv_t += len(enc.encode_ordinary(tc.result))
    assert bd["system_prompt_tokens"] == sys_t
    assert bd["conversation_tokens"] == conv_t
    assert bd["total_used_tokens"] == sys_t + conv_t

    real = enc
    calls = []

    class Wrap:
        name = real.name

        def encode_ordinary(self, text):
            calls.append(text)
            return real.encode_ordinary(text)

    monkeypatch.setattr(
        "models.conversation._tiktoken_encoding_for", lambda model: Wrap()
    )
    conv.get_token_estimate()
    calls.clear()
    conv.get_token_breakdown(128_000)
    assert calls == []


def test_model_switch_does_not_reuse_counts(monkeypatch):
    real_a = _tiktoken_encoding_for("openrouter/deepseek")
    assert real_a is not None

    class OtherEnc:
        name = "other-test-enc"

        def encode_ordinary(self, text):
            calls.append((self.name, text))
            return [1] * max(1, len(text) // 3)

    calls = []

    def factory(model):
        if "deepseek" in (model or ""):
            class Wrap:
                name = real_a.name

                def encode_ordinary(self, text):
                    calls.append((real_a.name, text))
                    return real_a.encode_ordinary(text)

            return Wrap()
        return OtherEnc()

    monkeypatch.setattr("models.conversation._tiktoken_encoding_for", factory)
    prompt = "SWITCHME " * 20
    conv = Conversation(agent_name="Coder", system_prompt=prompt, model="openrouter/deepseek")
    conv.get_token_estimate()
    assert calls.count((real_a.name, prompt)) == 1
    conv.model = "other/model"
    conv._token_estimate_cache = None
    conv.get_token_estimate()
    assert calls.count(("other-test-enc", prompt)) == 1
