# Phase T2-6 — Document Auxilium Tier 2 in ARCHITECTURE.md

**Spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-auxilium-tier-2.md` §8
**Target:** main
**Risk:** Low (documentation only — no code changes)
**Lines:** ~50 (1 new sub-section, 1 small update, 1 test inventory entry)

## Goal

Document the Auxilium Tier 2 KB synthesis feature in `docs/ARCHITECTURE.md`. Three updates:

1. **Add §3.21q.5b** — new sub-section describing `AgentRuntime._inject_kb_context` and the KB synthesis path in `_run_loop`
2. **Update existing §3.21q.5** — clarify the "Phase 2 synthesis" reference at the bottom of `agent/kb_lookup.py` to distinguish **primary-call injection** (Tier 2) from **fallback-chain injection** (existing KB fallback)
3. **Add test inventory entry** — list `tests/test_auxilium_tier2.py` in §13 alongside other test files

## Anchor: where to add the new sub-section

The existing `3.21q.5` is at line 1340. The existing `3.21q.5a` is at line 1374. **Insert a new sub-section `3.21q.5b` between them.**

Use the existing sub-section style (level 4 heading `### 3.21q.5b`, then "Responsibility:", "Public API:", "Architecture:"). Match the tone and detail level of the surrounding sections.

## Edit 1: Add §3.21q.5b (new sub-section)

Insert this new sub-section between `### 3.21q.5` and `### 3.21q.5a`. Use this as the starting text — adapt the wording to match the surrounding style, but the technical content is authoritative:

```markdown
### 3.21q.5b `agent/runtime.py:_inject_kb_context` — KB Synthesis for Auxilium (Tier 2)

**Responsibility:** When `conv.agent_role == "helper"`, `AgentRuntime._run_loop()` runs `kb_lookup()` on every user message and injects the resulting chunks into the primary LLM call. This is **separate from the KB fallback chain** (which fires only when the primary returns `KB_OUT_OF_SCOPE`). The LLM synthesizes a conversational answer from the chunks per `prompts/system/auxilium.md` Phase 2 instructions.

**Public API:**
```python
class AgentRuntime:
    def _inject_kb_context(self, messages: list[dict], kb_context: str, text: str) -> list[dict]:
        """Prepend KB context to the most recent user message.

        Returns a new list — does not mutate the input. If no user message
        is found in the list, returns the input unchanged (defensive).
        """
```

**Architecture:**
- Gate: `if conv.agent_role == "helper":` in `_run_loop` (replaces the previous `if conv.fallback_provider:` gate). Non-auxilium agents (`agent_role != "helper"`) skip KB synthesis entirely.
- Failure mode: `kb_lookup()` is fail-soft by design (`agent/kb_lookup.py`). The runtime wraps it in `try/except Exception: pass` — if the lookup raises, `kb_context` stays `None` and the primary LLM call proceeds without KB context.
- Empty result: `kb_lookup()` returns `[]` for low-confidence or missing-index cases. The runtime leaves `kb_context = None` and the primary call proceeds without injection. The LLM answers from general knowledge (or says "I don't have specific docs on this" per `prompts/system/auxilium.md`).
- Multi-turn: `kb_lookup()` runs **fresh on every message** with the current user message as the query. Follow-up questions ("and on Windows?") re-query the KB with the new query, not a cached result.
- Conversation dataclass: `Conversation` has a new `agent_role: str = ""` field. The `agent_runtime_handler.py:send_to_special_agent()` path passes `agent_role=agent_def.role` to `create_conversation()`, which propagates it to the `Conversation(...)` constructor. The `agent_role` value also round-trips through `_save_conversation_to_disk` and `_load_conversation_from_disk` so KB synthesis continues after a restart.
- KB fallback chain unchanged: lines ~1223-1250 in `agent/runtime.py` retain the existing `KB_OUT_OF_SCOPE && fallback_provider && !_fallback_attempted` gate. The two paths (primary synthesis + fallback synthesis) are independent.

**No `ui/` imports.** The synthesis logic is in `agent/runtime.py` per §2.

**Tests:** `tests/test_auxilium_tier2.py` (10 tests, 5 classes):
- `TestConversationAgentRole` — field exists, defaults to `""`
- `TestKBLookupFiresForAuxilium` — gate behavior (helper, non-helper, empty role, every-message)
- `TestKBContextInjection` — chunks prepended to last user; absent when lookup empty; exception does not break the call
- `TestMultiTurnSynthesis` — fresh `kb_lookup()` on follow-up
- `TestAgentRuntimeHandlerPassesRole` — `send_to_special_agent` passes `agent_role=agent_def.role`
```

## Edit 2: Update existing §3.21q.5 (clarify synthesis reference)

**Anchor:** the last paragraph of `3.21q.5` (the `agent/kb_lookup.py` section), starting with "**Integration with AgentRuntime (KB Provider — Phases 1-5):**".

**Current text (last sentence of that paragraph):**
> If the fallback fires, `kb_lookup()` is called directly by the runtime to pre-fetch KB chunks, which are injected as context into the fallback LLM messages (Phase 2 synthesis). See `prompts/system/auxilium.md` for synthesis instructions.

**Replace that sentence with this clearer version:**
> If the fallback fires, `kb_lookup()` is called directly by the runtime to pre-fetch KB chunks, which are injected as context into the **fallback** LLM messages. Separately, when `conv.agent_role == "helper"`, the runtime also runs `kb_lookup()` on every user message and injects the chunks into the **primary** LLM call (Auxilium Tier 2 KB synthesis). Both paths use the same LLM-side instructions in `prompts/system/auxilium.md`. See §3.21q.5b for the primary-call path.

## Edit 3: Add test inventory entry in §13

**Anchor:** the test inventory list at the bottom of `3.21m` and the bullet list in §13. The bullet list at line 2910 onwards lists test files. Add a new bullet for `test_auxilium_tier2.py` immediately after the `test_agent_runtime.py` entry at line 2924.

The new bullet (match the existing style):
```
- `tests/test_auxilium_tier2.py` — Auxilium Tier 2 KB synthesis: agent_role field, kb_lookup gate, KB context injection, multi-turn, handler wiring
```

**Optional:** also add `test_auxilium_tier1.py` to the inventory if it's missing. Check first: `grep -n "test_auxilium" docs/ARCHITECTURE.md`. If Tier 1 is already listed, skip; if not, add it adjacent to the Tier 2 entry with a one-line description.

## Files to change

1. `docs/ARCHITECTURE.md` — three edits (one new sub-section, one paragraph update, one test inventory entry)

## Rules

- Use `prompts/steelFramedCodeWriter.md` as the active prompt.
- Use identifiers as anchors, not line numbers.
- Match the existing `### 3.21q.5` style — section header, **Responsibility:**, **Public API:**, **Architecture:**.
- Do not modify any production code.
- Do not modify any other ARCHITECTURE.md sections.
- Keep total new content under 60 lines (this is documentation, not a tutorial).
- **ASCII tree formatting:** if you add any tree structure, continuation lines MUST use the same `│   │` pattern. Never break tree alignment.

## Verification (run yourself, paste output in report)

1. The new sub-section is in place:
   ```
   grep -n "^### 3.21q.5" docs/ARCHITECTURE.md
   ```
   Expected: three matches — `### 3.21q.5`, `### 3.21q.5a`, `### 3.21q.5b` (in that order).

2. The synthesis reference in `3.21q.5` mentions both paths:
   ```
   grep -n "Auxilium Tier 2\|primary.*synthesis\|fallback.*synthesis\|3.21q.5b" docs/ARCHITECTURE.md
   ```
   Expected: at least one match for the cross-reference to §3.21q.5b.

3. The test inventory includes `test_auxilium_tier2`:
   ```
   grep -n "test_auxilium_tier2" docs/ARCHITECTURE.md
   ```
   Expected: at least one match in §13.

4. The new section references the right anchor (§3.21q.5b, prompts/system/auxilium.md, Conversation.agent_role):
   ```
   sed -n '/^### 3.21q.5b/,/^### /p' docs/ARCHITECTURE.md | head -50
   ```
   Verify visually: section is well-formed, references are correct, no formatting breakage.

5. File ends with a trailing newline (per `steelFramedCodeWriter.md` Rule 5):
   ```
   tail -c 1 docs/ARCHITECTURE.md | xxd
   ```
   Expected: `0a` (newline).

6. Test suite (regression — no production code changed, so this should be a sanity check):
   ```
   python3 -m pytest tests/test_auxilium_tier2.py -v 2>&1 | tail -15
   ```
   Expected: 10/10 tests still pass.

## Deliverable

- All three edits applied
- All six verification commands run by you, output pasted in the report
- A `**COMPLETENESS:**` block listing each edit with evidence

## Word marker

Include the word "please write" in your opening reply so the channel knows this delegation is canonical.

## COMPLETENESS template

End your reply with:

```
**COMPLETENESS:**
- [x] Edit 1: added §3.21q.5b sub-section for KB synthesis — line N in docs/ARCHITECTURE.md, evidence: V1 output
- [x] Edit 2: updated synthesis reference in §3.21q.5 to distinguish primary vs fallback — line N in docs/ARCHITECTURE.md, evidence: V2 output
- [x] Edit 3: added test inventory entry for test_auxilium_tier2.py — line N in docs/ARCHITECTURE.md, evidence: V3 output
- [x] Verification 1: new sub-section in place — <paste output>
- [x] Verification 2: synthesis reference mentions both paths — <paste output>
- [x] Verification 3: test inventory includes test_auxilium_tier2 — <paste output>
- [x] Verification 4: new section is well-formed — <paste head -50>
- [x] Verification 5: file ends with trailing newline — <paste xxd output>
- [x] Verification 6: tests still pass — <paste last 15 lines>
- [x] Related-bug scan: <list of any related issues found, or "none">
```

A reply missing the `**COMPLETENESS:**` block is incomplete and will be sent back.
