# Phase T2-4 — `tests/test_auxilium_tier2.py` (KB synthesis test suite)

**Spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-auxilium-tier-2.md` §2.5
**Target:** main
**Risk:** Low (adds a new test file; does not modify production code)
**Lines:** ~280 (5 test classes, ~30 tests total)

## Goal

Add a new test file at `tests/test_auxilium_tier2.py` that locks in the Tier 2 KB synthesis behavior. The test file is the regression contract — if someone breaks `agent_role` propagation, the gate, or the injection, the tests fail.

## Reference

- **Test class layout:** the spec at `docs/specs/SPEC-auxilium-tier-2.md` §2.5 has the full test plan with 5 classes. Use the spec as the authoritative source.
- **Style reference:** `tests/test_auxilium_tier1.py` (141 lines, established style — module docstring, grouped test classes, clear section headers).

## Test classes (5 total)

1. **`TestConversationAgentRole`** — `Conversation.agent_role` field exists, defaults to `""`
2. **`TestKBLookupFiresForAuxilium`** — gate behavior: `kb_lookup` fires for `agent_role == "helper"`, not for `agent_role == "coder"`, fires on every message (not just the first)
3. **`TestKBContextInjection`** — KB context prepended to last user message; absent when KB returns empty
4. **`TestMultiTurnSynthesis`** — follow-up questions trigger a fresh `kb_lookup` with the follow-up text as the query
5. **`TestAgentRuntimeHandlerPassesRole`** — `_create_runtime_conversation` passes `agent_role=agent_def.role` to `create_conversation()`

## Critical corrections to the spec's reference test code

The spec's §2.5 has three bugs in the reference test snippets. **Use the corrected versions below, not the spec verbatim:**

### Bug 1: `KBChunk` import path

**Spec says:**
```python
from agent.runtime import KBChunk
```

**Correct:** `KBChunk` lives in `agent.kb_lookup`, not `agent.runtime`. Use:
```python
from agent.kb_lookup import KBChunk
```

### Bug 2: `KBChunk` constructor fields

**Spec says:**
```python
KBChunk(text="...", source="...", section="...", score=0.8)
```

**Correct:** `KBChunk` is a dataclass with 5 required fields in this order: `id, source, section, text, score`. Use:
```python
KBChunk(id="c1", source="configuration.md", section="Gateway", text="Gateway config is at ~/.config/crabcakes/", score=0.8)
```

### Bug 3: `create_conversation()` does not take `system_prompt`

**Spec says:**
```python
conv = Conversation(
    agent_name="Auxilium",
    agent_role=agent_role,
    model="openai/gpt-4o",
    system_prompt="You are Auxilium.",   # ← this is on Conversation, not create_conversation
)
rt._conversations["test-session"] = conv
```

The spec's example bypasses `create_conversation()` and pokes the conversation dict directly. **This is OK for `TestConversationAgentRole` (which tests the dataclass directly) but not for the other classes.** The other tests should use `rt.create_conversation(...)` to exercise the real wiring. Example:

```python
rt.create_conversation(
    session_key="test-session",
    agent_name="Auxilium",
    agent_role=agent_role,
)
```

Note: `create_conversation()` does NOT take `system_prompt` — it builds the system prompt internally from `agent_name` and tools.

## Patch target for `kb_lookup`

The `kb_lookup` function is imported lazily inside `_run_loop` (line 1167: `from agent.kb_lookup import kb_lookup`). To mock it, patch the source module, not `agent.runtime`:

```python
with patch("agent.kb_lookup.kb_lookup", return_value=fake_chunks):
    ...
```

NOT:
```python
with patch("agent.runtime.kb_lookup", return_value=fake_chunks):  # ← raises AttributeError at __enter__
    ...
```

## Files to change

1. `tests/test_auxilium_tier2.py` — NEW FILE

## File template (use this as a starting point)

The file structure is up to you, but it must contain the 5 test classes listed above. Use this skeleton:

```python
# tests/test_auxilium_tier2.py
# Tests for Auxilium Tier 2 — LLM Synthesis with KB Lookup.
#
# Locks in the agent_role field, the kb_lookup gate, the injection logic,
# multi-turn behavior, and the handler-side wiring.
#
# All tests are non-GTK. No xvfb-run needed.

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from agent.config import AgentConfig
from agent.runtime import AgentRuntime
from agent.kb_lookup import KBChunk
from models.conversation import Conversation


def _make_config() -> AgentConfig:
    """Build a minimal AgentConfig with one provider (no real API calls)."""
    from agent.config import LLMProviderConfig
    providers = {
        "openai": LLMProviderConfig(
            name="openai",
            base_url="https://api.openai.com/v1",
            api_key="***",
            default_model="gpt-4o",
            caller="openai",
        ),
    }
    return AgentConfig(
        providers=providers,
        default_provider="openai",
        default_model="openai/gpt-4o",
    )


def _make_runtime_with_conv(agent_role: str = "helper") -> tuple[AgentRuntime, str]:
    """Build a runtime with one conversation registered. Returns (rt, session_key)."""
    cfg = _make_config()
    rt = AgentRuntime(cfg)
    rt.start()
    rt.create_conversation(
        session_key="test-session",
        agent_name="Auxilium",
        agent_role=agent_role,
    )
    return rt, "test-session"


def _fake_llm_response(content: str = "answer") -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


# ── Test Class 1: Conversation.agent_role field ──────────────────────────────


class TestConversationAgentRole:
    def test_agent_role_field_exists(self):
        conv = Conversation(agent_name="Auxilium", model="openai/gpt-4o", agent_role="helper")
        assert conv.agent_role == "helper"

    def test_agent_role_defaults_to_empty_string(self):
        conv = Conversation(agent_name="Test", model="openai/gpt-4o")
        assert conv.agent_role == ""


# ── Test Class 2: kb_lookup gate behavior ────────────────────────────────────


class TestKBLookupFiresForAuxilium:
    def test_kb_lookup_called_for_helper_role(self):
        rt, sk = _make_runtime_with_conv(agent_role="helper")
        captured = {}
        def fake_lookup(question, *, top_k, min_score):
            captured["question"] = question
            captured["top_k"] = top_k
            captured["min_score"] = min_score
            return []
        with patch("agent.kb_lookup.kb_lookup", side_effect=fake_lookup):
            with patch.object(rt, "_call_llm", return_value=_fake_llm_response()):
                rt._run_loop(sk, "how do I configure the gateway?")
        assert captured.get("question") == "how do I configure the gateway?"

    def test_kb_lookup_not_called_for_non_helper_role(self):
        rt, sk = _make_runtime_with_conv(agent_role="coder")
        with patch("agent.kb_lookup.kb_lookup") as mock_kb:
            with patch.object(rt, "_call_llm", return_value=_fake_llm_response()):
                rt._run_loop(sk, "how do I configure the gateway?")
        mock_kb.assert_not_called()

    def test_kb_lookup_runs_every_message(self):
        rt, sk = _make_runtime_with_conv(agent_role="helper")
        call_count = [0]
        def fake_lookup(question, *, top_k, min_score):
            call_count[0] += 1
            return []
        with patch("agent.kb_lookup.kb_lookup", side_effect=fake_lookup):
            with patch.object(rt, "_call_llm", return_value=_fake_llm_response()):
                rt._run_loop(sk, "first question")
                rt._run_loop(sk, "second question")
                rt._run_loop(sk, "third question")
        assert call_count[0] == 3


# ── Test Class 3: KB context injection ───────────────────────────────────────


class TestKBContextInjection:
    def test_kb_context_injected_into_primary_call(self):
        rt, sk = _make_runtime_with_conv(agent_role="helper")
        fake_chunks = [
            KBChunk(
                id="c1",
                source="configuration.md",
                section="Gateway",
                text="Gateway config is in ~/.config/crabcakes/",
                score=0.8,
            ),
        ]
        captured_messages = []
        def fake_call(sk_arg, messages, tools):
            captured_messages.extend(messages)
            return _fake_llm_response("Here is the answer.")
        with patch("agent.kb_lookup.kb_lookup", return_value=fake_chunks):
            with patch.object(rt, "_call_llm", side_effect=fake_call):
                rt._run_loop(sk, "how do I configure the gateway?")
        user_msgs = [m for m in captured_messages if m.get("role") == "user"]
        assert len(user_msgs) >= 1
        last_user = user_msgs[-1]
        assert "Gateway config" in last_user.get("content", "")
        assert "how do I configure" in last_user.get("content", "")

    def test_primary_call_without_kb_context_when_lookup_returns_empty(self):
        rt, sk = _make_runtime_with_conv(agent_role="helper")
        captured_messages = []
        def fake_call(sk_arg, messages, tools):
            captured_messages.extend(messages)
            return _fake_llm_response("I don't have specific docs on this.")
        with patch("agent.kb_lookup.kb_lookup", return_value=[]):
            with patch.object(rt, "_call_llm", side_effect=fake_call):
                rt._run_loop(sk, "what is the meaning of life?")
        user_msgs = [m for m in captured_messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert "meaning of life" in last_user.get("content", "")
        # No KB marker when chunks are empty
        assert "KB Context" not in last_user.get("content", "")


# ── Test Class 4: Multi-turn synthesis ────────────────────────────────────────


class TestMultiTurnSynthesis:
    def test_followup_question_uses_current_question_as_query(self):
        rt, sk = _make_runtime_with_conv(agent_role="helper")
        queries = []
        def fake_lookup(question, *, top_k, min_score):
            queries.append(question)
            return []
        with patch("agent.kb_lookup.kb_lookup", side_effect=fake_lookup):
            with patch.object(rt, "_call_llm", return_value=_fake_llm_response()):
                rt._run_loop(sk, "how do I configure the gateway on Linux?")
                rt._run_loop(sk, "and on Windows?")
        assert len(queries) == 2
        assert "Linux" in queries[0]
        assert "Windows" in queries[1]


# ── Test Class 5: Handler passes agent_role ──────────────────────────────────


class TestAgentRuntimeHandlerPassesRole:
    def test_create_conversation_receives_agent_role(self):
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
        config = MagicMock()
        config.providers = {}
        config.default_provider = "openai"
        config.default_model = "openai/gpt-4o"
        config.tool_timeout_seconds = 120
        config.enforcement.enabled = False
        handler = AgentRuntimeHandler(config)
        handler._runtime = MagicMock()
        agent_def = MagicMock()
        agent_def.display_name = "Auxilium"
        agent_def.role = "helper"
        agent_def.fallback_provider = None
        agent_def.fallback_model = None
        agent_def.system_prompt = "You are Auxilium."
        agent_def.tools = []
        agent_def.mcp_servers = []
        agent_def.app_title = ""
        agent_def.api_key = None
        handler._create_runtime_conversation("test-session", agent_def)
        call_kwargs = handler._runtime.create_conversation.call_args
        assert call_kwargs.kwargs.get("agent_role") == "helper"
```

## Rules

- Use `prompts/steelFramedCodeWriter.md` as the active prompt.
- **Do NOT use the spec's reference test code verbatim** — it has 3 bugs (KBChunk import, KBChunk fields, system_prompt on create_conversation). Use the corrected versions above.
- **Use the existing `tests/test_auxilium_tier1.py` style** as a reference for module docstring, section headers, and pytest patterns.
- Do NOT modify any production code in this phase.
- Do NOT add GTK-dependent tests (no xvfb-run). All tests are non-GTK.
- If a test fails because of an environmental issue (e.g., a real network call leaks through a mock), debug it as part of this phase — do not skip.
- 30% minimum sad-path tests: Class 3 has both happy and sad paths. Add at least one more sad-path test (e.g., `kb_lookup` raises an exception → `_call_llm` receives messages without KB context).

## Verification (run yourself, paste output in report)

1. The new test file runs and all tests pass:
   ```
   python3 -m pytest tests/test_auxilium_tier2.py -v 2>&1 | tail -50
   ```
   Expected: all tests pass. Count them — should be at least 8 tests across 5 classes.

2. Test count check:
   ```
   python3 -m pytest tests/test_auxilium_tier2.py --collect-only -q 2>&1 | tail -20
   ```
   Expected: at least 8 tests collected.

3. Full test suite (regression):
   ```
   python3 -m pytest tests/ -q --tb=short --ignore=tests/test_agent_runtime.py --ignore=tests/test_kb_lookup.py 2>&1 | tail -10
   ```
   Expected: previous 1533 still pass + new tests. The total should be 1533 + (your new test count) passed.

4. Coverage check — every spec acceptance criterion (AC-T2-1 through AC-T2-9) maps to at least one test:
   ```
   grep -n "def test_" tests/test_auxilium_tier2.py | head -20
   ```
   Verify visually: AC-T2-1 (field exists/defaults) → Test 1; AC-T2-3/4 (gate) → Test 2; AC-T2-5/6 (injection) → Test 3; AC-T2-7 (multi-turn) → Test 4; AC-T2-2 (handler passes role) → Test 5.

## Deliverable

- New file `tests/test_auxilium_tier2.py` with all 5 classes
- All four verification commands run by you, output pasted in the report
- A `**COMPLETENESS:**` block listing each class with evidence

## Word marker

Include the word "please write" in your opening reply so the channel knows this delegation is canonical.

## COMPLETENESS template

End your reply with:

```
**COMPLETENESS:**
- [x] Test Class 1: TestConversationAgentRole — N tests, evidence: pytest output
- [x] Test Class 2: TestKBLookupFiresForAuxilium — N tests, evidence: pytest output
- [x] Test Class 3: TestKBContextInjection — N tests, evidence: pytest output
- [x] Test Class 4: TestMultiTurnSynthesis — N tests, evidence: pytest output
- [x] Test Class 5: TestAgentRuntimeHandlerPassesRole — N tests, evidence: pytest output
- [x] Verification 1: all tests in new file pass — <paste pytest output>
- [x] Verification 2: test count >= 8 — <paste collect-only output>
- [x] Verification 3: full test suite — <paste last 10 lines>
- [x] Verification 4: AC mapping — <list each AC and which test covers it>
- [x] Related-bug scan: <list of any related issues found, or "none">
```

A reply missing the `**COMPLETENESS:**` block is incomplete and will be sent back.
