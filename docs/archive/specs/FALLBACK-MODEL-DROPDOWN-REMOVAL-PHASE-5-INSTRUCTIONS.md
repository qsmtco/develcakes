PHASE 5 of 6 — Documentation: update ARCHITECTURE.md to drop `fallback_model` references

Files to change:
1. `/path/to/projects/crabcakes/docs/ARCHITECTURE.md` — update 4 references to drop `fallback_model`

Spec reference:
- Read the master spec at `/path/to/projects/crabcakes/docs/specs/SPEC-AGENT-FALLBACK-MODEL-DROPDOWN-REMOVAL.md` §2.10 for the exact diff.
- The spec gives line numbers, but treat them as approximate — the file may have drifted. Use grep to locate the actual lines.

Target edits (use grep to find the exact text, then edit with the apply_patch tool):

A. The Conversation dataclass summary (one-liner starting with `@dataclass Conversation: ...`):
   - Find: `@dataclass Conversation: agent_name, project_path, system_prompt, messages, model, fallback_provider, fallback_model, created_at, total_tokens, total_cost, step_count`
   - Replace with: `@dataclass Conversation: agent_name, project_path, system_prompt, messages, model, fallback_provider, created_at, total_tokens, total_cost, step_count`

B. The AgentConfig dataclass summary (one-liner starting with `@dataclass AgentConfig: ...`):
   - Find: `@dataclass AgentConfig: providers, default_provider, default_model, max_tool_iterations, tool_timeout_seconds, auto_save_conversations, cost_limit, step_limit, enforcement, fallback_provider, fallback_model`
   - Replace with: `@dataclass AgentConfig: providers, default_provider, default_model, max_tool_iterations, tool_timeout_seconds, auto_save_conversations, cost_limit, step_limit, enforcement, fallback_provider`

C. The SpecialAgentDef summary (the two-line block in the field list, lines ~1485-1486):
   - Find:
     ```
         fallback_provider: str | None,        # KB fallback provider (e.g. "openrouter")
         fallback_model: str | None,           # KB fallback model (e.g. "openrouter/owl-alpha")
     ```
   - Replace with:
     ```
         fallback_provider: str | None,        # KB fallback provider (e.g. "openrouter")
         # fallback_model removed in 2026-06-15 — see SPEC-AGENT-FALLBACK-MODEL-DROPDOWN-REMOVAL.md
     ```
   - NOTE: Watch the indentation carefully. The existing line uses spaces, and the new comment must use the same indentation to stay aligned in the rendered code block. Verify with a read of the surrounding 3 lines before editing.

D. The "Per-agent model" paragraph (a prose paragraph mentioning both `fallback_provider` and `fallback_model`):
   - Find:
     ```
     **Per-agent model:** `llm_name` field specifies the provider card name for this agent (None → global default). `fallback_provider` and `fallback_model` specify the KB fallback — when the KB returns `[KB_OUT_OF_SCOPE]`, the runtime retries with this provider. Resolved in `AgentRuntimeHandler._resolve_agent_model()` and wired through `create_conversation()` → `Conversation` → runtime fallback chain.
     ```
   - Replace with:
     ```
     **Per-agent model:** `llm_name` field specifies the provider card name for this agent (None → global default). `fallback_provider` specifies the KB fallback — when the KB returns `[KB_OUT_OF_SCOPE]`, the runtime retries with this provider. The model is derived from the selected provider card's `default_model` (same derivation as the primary path in `AgentRuntimeHandler._resolve_agent_model()`). Wired through `create_conversation()` → `Conversation` → runtime fallback chain. See `docs/specs/SPEC-AGENT-FALLBACK-MODEL-DROPDOWN-REMOVAL.md`.
     ```

What NOT to change:
- Line 1370 (KB Provider integration paragraph) — it does not mention `fallback_model` (only `fallback_provider`). Leave it alone.
- Any other reference to `fallback_model` in ARCHITECTURE.md. If grep finds any, report it as a related issue rather than silently editing.

Rules:
- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- Read the file at each target line BEFORE editing — confirm the exact text to be replaced, with attention to trailing whitespace.
- Use the apply_patch tool (or your editor's exact-text replace) — do not rewrite the file.
- After all 4 edits, grep the file to confirm zero live `fallback_model` references remain (only the new comment from edit C is expected).

Verification commands (run all and paste output):
1. `cd /path/to/projects/crabcakes && grep -n "fallback_model" docs/ARCHITECTURE.md` — expect only the new comment line from edit C, and possibly the spec reference in edit D
2. `cd /path/to/projects/crabcakes && wc -l docs/ARCHITECTURE.md` — should be approximately the same as before (4 small edits, net 0 to +2 lines)
3. `cd /path/to/projects/crabcakes && git diff docs/ARCHITECTURE.md | head -60` — visually verify the 4 edits are correct (line-by-line)

Report back with:
- All three verification command outputs
- The full `git diff docs/ARCHITECTURE.md` output (so the supervisor can spot-check the edits)
- COMPLETENESS checklist:
  COMPLETENESS:
  - [done/not done] Edit A: dropped `fallback_model` from `@dataclass Conversation: ...` summary line — evidence: grep
  - [done/not done] Edit B: dropped `fallback_model` from `@dataclass AgentConfig: ...` summary line — evidence: grep
  - [done/not done] Edit C: replaced the two-line `fallback_provider` + `fallback_model` block with a single `fallback_provider` line and a comment — evidence: git diff
  - [done/not done] Edit D: rewrote the "Per-agent model" paragraph to drop `fallback_model` and reference the new spec — evidence: git diff
  - [done/not done] No other `fallback_model` references in ARCHITECTURE.md (other than the new comment and the spec reference) — evidence: grep
  - [done/not done] Line 1370 (KB Provider integration paragraph) untouched — evidence: git diff
- Any related issues found during the related-bug scan (read 3+ lines of context before flagging duplicates) — flag only, do not fix in this phase.

please write
