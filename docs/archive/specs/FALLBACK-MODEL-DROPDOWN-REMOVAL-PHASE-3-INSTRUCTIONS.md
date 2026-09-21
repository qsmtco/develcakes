PHASE 3 of 6 — Runtime derivation: drop `conv.fallback_model` reads, derive from provider card

Files to change:
1. `/path/to/projects/crabcakes/agent/runtime.py` — replace the `fallback_model = conv.fallback_model or conv.fallback_provider` derivation with provider-card-based resolution
2. `/path/to/projects/crabcakes/ui/handlers/agent_runtime_handler.py` — drop the `fallback_model` passthroughs in two places

Spec reference:
- Read the master spec at `/path/to/projects/crabcakes/docs/specs/SPEC-AGENT-FALLBACK-MODEL-DROPDOWN-REMOVAL.md` §2.4 (runtime) and §2.7 (handler).
- The spec is identifier-anchored — do not rely on line numbers.

Changes in `agent/runtime.py`:
- Locate the line `fallback_model = conv.fallback_model or conv.fallback_provider` inside the `if (text_content == KB_OUT_OF_SCOPE and conv.fallback_provider and not getattr(conv, "_fallback_attempted", False))` block in `_run_loop`.
- Replace it with provider-card-based resolution. The replacement logic is:

```python
# Resolve fallback model the same way the primary path does:
#   f"{provider_name}/{provider.default_model}"
# See AgentRuntimeHandler._resolve_agent_model() at ui/handlers/agent_runtime_handler.py
# (the spec reference there is the canonical implementation).
fallback_provider_name = conv.fallback_provider
fallback_provider_cfg = self._config.providers.get(fallback_provider_name) if fallback_provider_name else None
if fallback_provider_cfg and fallback_provider_cfg.default_model:
    default_model = fallback_provider_cfg.default_model
    if "/" in default_model:
        fallback_model = default_model
    else:
        fallback_model = f"{fallback_provider_name}/{default_model}"
else:
    # Provider not configured — fall back to provider name (runtime will error clearly)
    fallback_model = fallback_provider_name
```

- Do NOT remove the `Conversation.fallback_model` dataclass field. It is kept for backward-read tolerance.
- Do NOT change the `create_conversation()` function's `fallback_model` parameter (line ~1015). Out of scope.
- Do NOT change the `agent/config.py:AgentConfig.fallback_model` field. Out of scope.

Changes in `ui/handlers/agent_runtime_handler.py`:
- In the `create_conversation` call (around line 415-417), drop the `fallback_model=agent_def.fallback_model,` line. Add a comment: `# fallback_model removed in 2026-06-15 — runtime derives from provider card. See SPEC-AGENT-FALLBACK-MODEL-DROPDOWN-REMOVAL.md.`
- In the in-memory sync block (around line 432-433), drop the `conv.fallback_model = agent_def.fallback_model` line. Add a similar comment.

Rules:
- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- Read both files in full before editing. Output DISCOVERY block.
- Hard-part-first: do `agent/runtime.py` derivation first (this is the behavioral change), then the two `agent_runtime_handler.py` passthrough drops (mechanical).
- Verify with grep that the only `fallback_model` references remaining in `agent/runtime.py` and `ui/handlers/agent_runtime_handler.py` are in comments (the one at line 1015 is the create_conversation passthrough — out of scope, KEEP).

Verification commands (run all and paste output):
1. `grep -n "fallback_model" /path/to/projects/crabcakes/agent/runtime.py /path/to/projects/crabcakes/ui/handlers/agent_runtime_handler.py` — expect:
   - `agent/runtime.py:1015` (the kept create_conversation passthrough parameter, in a comment context)
   - Optionally a comment referencing the spec in the new derivation block
   - ZERO matches in `agent_runtime_handler.py` (or only comment matches)
2. `cd /path/to/projects/crabcakes && python3 -c "import ast; ast.parse(open('agent/runtime.py').read()); ast.parse(open('ui/handlers/agent_runtime_handler.py').read())"` — expect no SyntaxError
3. `cd /path/to/projects/crabcakes && python3 -c "from agent.runtime import AgentRuntime; from ui.handlers.agent_runtime_handler import AgentRuntimeHandler; print('imports OK')"` — expect "imports OK"
4. `cd /path/to/projects/crabcakes && timeout 60 xvfb-run -a python3 -m pytest tests/test_runtime_fallback.py tests/test_kb_integration.py -v 2>&1 | tail -40` — expect all existing tests still pass (Phase 4 will update them for the new contract, but the existing tests should still pass because the provider card default_model happens to be what the tests set fallback_model to)
5. **Behavioral smoke test** — simulate the fallback chain end-to-end with the new derivation:
   ```
   cd /path/to/projects/crabcakes && python3 << 'EOF'
   from agent.config import AgentConfig, LLMProviderConfig
   from agent.runtime import AgentRuntime, KB_OUT_OF_SCOPE
   from models.conversation import Conversation
   providers = {
       "local-kb": LLMProviderConfig(name="local-kb", base_url="http://localhost:18790/v1", api_key="***", default_model="local-kb", caller="openai", supports_tools=False, supports_streaming=False),
       "openrouter": LLMProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1", api_key="***", default_model="openrouter/owl-alpha", caller="openai"),
   }
   config = AgentConfig(providers=providers, default_provider="local-kb", default_model="local-kb/local-kb", fallback_provider="openrouter")
   rt = AgentRuntime(config)
   rt.start()
   conv = Conversation(agent_name="T", model="local-kb/local-kb", system_prompt="x", fallback_provider="openrouter")
   rt._conversations["t"] = conv
   from unittest.mock import patch
   captured = []
   def fake_call(sk, msgs, tools):
       captured.append(("call", conv.model))
       return {"choices": [{"message": {"content": KB_OUT_OF_SCOPE if len(captured) == 1 else "fallback answer", "tool_calls": []}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
   rt._on_response_complete = lambda sk, t: captured.append(("final", t))
   with patch.object(rt, "_call_llm", side_effect=fake_call):
       rt._run_loop("t", "hi")
   assert captured[0] == ("call", "local-kb/local-kb"), f"primary call wrong model: {captured[0]}"
   assert captured[1] == ("call", "openrouter/owl-alpha"), f"fallback call wrong model: {captured[1]}"
   assert captured[2][0] == "final" and "fallback answer" in captured[2][1]
   assert conv.model == "local-kb/local-kb", f"model not restored: {conv.model}"
   print("BEHAVIORAL SMOKE OK — fallback derived as 'openrouter/owl-alpha' from provider card")
   EOF
   ```
   Expect: "BEHAVIORAL SMOKE OK — fallback derived as 'openrouter/owl-alpha' from provider card"

Report back with:
- Files changed (with `wc -l` output before and after)
- All five verification command outputs
- COMPLETENESS checklist:
  COMPLETENESS:
  - [done/not done] Replaced `fallback_model = conv.fallback_model or conv.fallback_provider` with provider-card-based derivation in `agent/runtime.py` — evidence: read the new block
  - [done/not done] Dropped `fallback_model=agent_def.fallback_model,` from `create_conversation` call in `ui/handlers/agent_runtime_handler.py` — evidence: read
  - [done/not done] Dropped `conv.fallback_model = agent_def.fallback_model` in sync block in `ui/handlers/agent_runtime_handler.py` — evidence: read
  - [done/not done] Did NOT change `Conversation.fallback_model` field or `create_conversation` parameter — evidence: grep shows line 1015 unchanged
  - [done/not done] Behavioral smoke test passes (fallback model derived as "openrouter/owl-alpha") — evidence: pasted output
  - [done/not done] All existing test_runtime_fallback.py and test_kb_integration.py tests still pass — evidence: pytest output
  - [done/not done] No regressions — evidence: pytest output
- Any related issues found during the related-bug scan (read 3+ lines of context before flagging duplicates) — flag only, do not fix in this phase.

please write
