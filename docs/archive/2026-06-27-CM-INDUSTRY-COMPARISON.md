# Crabcakes Context Management vs. Industry Comparison

**Date:** 2026-06-27 (updated 2026-06-29 — P10 JIT context discovery shipped)
**Author:** Qaster
**Sources:** Web research (Claude Code, Cursor Composer 2, GitHub Copilot, Cline, Aider, Windsurf) + internal research docs (`docs/research/context-management-survey-2026.md`, `docs/research/context-management-comparison.md`, `docs/research/crabcakes-future-context-strategies.md`)

---

## Architecture: Pluggable & Modular

Crabcakes is the **only** platform with a formally pluggable context management architecture. The `ContextStrategy` protocol (`agent/context_strategy.py`) decouples compaction policy from the data model (`Conversation`) and the runtime (`AgentRuntime`). This means:

- **Swap strategies without surgery** — implement a new `ContextStrategy`, wire it in one line (`self._context_strategy = MyStrategy()`), done
- **Data model stays pure** — `Conversation` remains a stdlib-only dataclass with zero policy logic
- **Runtime stays agnostic** — `AgentRuntime` calls `strategy.compact(conv, budget)` and reads `strategy.last_result` without knowing *how* compaction works
- **Future-proof** — as the industry evolves (RL summarization, temporal graphs, agentic memory), crabcakes can adopt new approaches by implementing a new strategy class, not rewriting the runtime

See `docs/proposals/PROPOSAL-pluggable-context-strategy.md` for the full architecture rationale.

| Architecture Property | Crabcakes | Claude Code | Cursor | Copilot | Cline | Aider | Windsurf |
|---|---|---|---|---|---|---|---|
| **Pluggable strategy protocol** | ✅ `ContextStrategy` Protocol | ❌ Hardcoded | ❌ Hardcoded | ❌ Hardcoded | ❌ Hardcoded | ❌ Hardcoded | ❌ Hardcoded |
| **Strategy decoupled from data model** | ✅ `Conversation` has zero policy logic | ❌ Embedded | ❌ Embedded | ❌ Embedded | ❌ Embedded | ❌ Embedded | ❌ Embedded |
| **Strategy decoupled from runtime** | ✅ Runtime calls protocol, not concrete class | ❌ Inline | ❌ Inline | ❌ Inline | ❌ Inline | ❌ Inline | ❌ Inline |
| **Can swap without code surgery** | ✅ One-line wire change | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

---

## Feature Matrix — vs. Major Platforms

| Feature | Crabcakes | Claude Code | Cursor (Composer 2) | GitHub Copilot | Cline | Aider | Windsurf (Cascade) |
|---|---|---|---|---|---|---|---|
| **Tiered / Layered Compaction** | ✅ 3 layers (prune → trim → summary) | ❌ Single-tier summarize | ✅ Self-summarization (1 layer, RL-trained) | ❌ Single-tier compact-then-truncate | ❌ Single-tier auto-compact at 80% | ❌ Single-tier summarize | ❌ Retrieval-filtered (no layered compaction) |
| **Layer 1: Tool Result Stubbing** | ✅ In-place stubbing of old TOOL_RESULT content | ⚠️ Preserves decisions, drops tool output (coarse) | ❌ No separate layer | ❌ No separate layer | ❌ | ❌ | ❌ |
| **Layer 2: Message Trimming** | ✅ CB-6-aware pair removal with candidate selector | ⚠️ Truncates old turns (no pair awareness) | ❌ | ⚠️ Truncates by relevance/recency | ❌ | ⚠️ `/drop` removes files manually | ❌ |
| **Layer 3: Summary Injection** | ✅ Token-budgeted summary of removed messages | ✅ Summarizes removed turns | ✅ RL-trained self-summary (~1k tokens) | ⚠️ Converts to "checkpoints" | ✅ Summarizes conversation | ⚠️ History file selection | ❌ |
| **CB-6: Tool Call Pairing Invariant** | ✅ ASSISTANT+tool_calls always paired with TOOL_RESULT through all layers | ❌ No pairing invariant | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Per-Session Compaction Isolation** | ✅ `session_key` tagging, local var capture, thread-safe `_compaction_lock` | ❌ Not multi-session | ❌ Not multi-session | ❌ Not multi-session | ❌ Not multi-session | ❌ Not multi-session | ⚠️ Shared context protocol (different approach) |
| **Per-Provider Token Ceilings** | ✅ Soft + hard ceiling computed per provider/model | ⚠️ Uses model's window directly | ⚠️ Uses model's window directly | ⚠️ Uses model's window directly | ⚠️ Uses model's window directly | ⚠️ Uses model's window directly | ⚠️ Uses model's window directly |
| **Compaction Telemetry / Observability** | ✅ 14-field `CompactionEvent` (layer, tokens_before/after, messages_removed, summary_tokens, provider, model, session_key) + `on_token_breakdown` callback | ❌ `/context` shows usage only | ❌ | ❌ Shows % usage | ❌ Shows % usage | ⚠️ `/tokens` shows usage | ❌ |
| **Thread-Safe Multi-Session Runtime** | ✅ `send()` spawns daemon thread per session; shared state guarded by lock + local capture | ❌ Single session | ❌ Single session | ❌ Single session | ❌ Single session | ❌ Single session | ⚠️ Shared timeline (not thread-isolated) |
| **Adversarial Audit (spec → impl → audit cycle)** | ✅ 2-phase: 25 spec fixes + 8 audit-discovered bugs, 150 tests | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Negative / Pathological Input Guards** | ✅ Negative budget, negative protect_turns, zero tokens, whitespace-only, duplicate IDs | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Split Boundary Detection** | ✅ `_find_split_index` with CB-6 forward/backward scan, iteration cap, visited-set dedup | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Token-Based Summary Truncation** | ✅ `_fit_summary` truncates to fit budget (not char-based) | ⚠️ Approximate | ✅ RL-trained to ~1k tokens | ⚠️ Approximate | ⚠️ | ❌ | ❌ |
| **Adaptive Compaction Threshold** | ✅ Provider-specific percentage of model max (15–25%) | ⚠️ Fixed at ~80% window | ⚠️ Fixed token trigger | ⚠️ Fixed at ~80% window | ⚠️ Fixed at ~80% | ❌ Manual | ❌ |
| **Dynamic Context Discovery** | ✅ `context_mode` (preload/jit/hybrid) + `file_search` tool (P10) | ❌ | ✅ Tool lookup on demand | ✅ Selective file fetch | ❌ | ❌ | ✅ M-Query similarity retrieval |
| **RL-Trained Self-Summarization** | ❌ Summary is rule-based | ❌ | ✅ Model learns what to keep | ❌ | ❌ | ❌ | ❌ |
| **Workspace Memory / Rules Files** | ❌ (handled by OpenClaw layer) | ✅ `CLAUDE.md` | ❌ | ✅ `.github/copilot-instructions.md` | ✅ `.clinerules` | ✅ `.aider.conf.yml` | ✅ `.windsurfrules` |
| **Persistent Project Index** | ❌ (handled by OpenClaw layer) | ❌ | ❌ | ✅ Repo-aware | ❌ | ❌ | ✅ Full codebase index |
| **Manual Compaction Trigger** | ❌ Automatic only | ✅ `/compact` | ❌ | ✅ `/compact` (CLI only) | ✅ Auto + manual | ✅ `/clear`, `/drop` | ⚠️ Pinning + selection |
| **JIT Context Modes (per-provider)** | ✅ `ProviderConfig.context_mode`: auto/preload/jit/hybrid, resolved by model window size | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **No-Op Compaction Detection** | ✅ Guards against recording events when nothing was freed | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

---

## Feature Matrix — vs. Academic / Frontier Research

Crabcakes' research docs (`docs/research/`) survey 30+ academic papers and production frameworks. Here's how crabcakes' implemented features map to the frontier techniques identified in that research:

| Technique (from research) | Crabcakes | Status in Industry |
|---|---|---|
| **Tool-call/result atomicity (CB-6)** | ✅ Implemented + tested | ⚠️ Production consensus (LangChain, Anthropic) but nobody formalizes or tests it |
| **Tiered compaction (stub → trim → summarize)** | ✅ 3-layer pipeline | ⚠️ LangChain proposes Write/Select/Compress/Isolate; nobody layers compaction itself |
| **State-triggered compaction at token threshold** | ✅ Soft + hard ceiling per provider | ✅ Universal (80% threshold is standard) |
| **Tool-result clearing / stubbing** | ✅ Layer 1 `prune_tool_outputs` | ⚠️ Anthropic & LangChain recommend it; few implement it as a distinct layer |
| **Token-accurate counting (not chars÷4)** | ✅ tiktoken + cache | ⚠️ Most use chars÷4 approximation |
| **Recursive hierarchical summarization** | ❌ Future (T1.1 in research doc) | ⚠️ Wang et al. proven it works; Cursor's self-summarization approximates it |
| **Structured summary schemas (PRISM)** | ❌ Future (T1.2) | ⚠️ PRISM shows 4× compression with better performance |
| **Tool-output offloading to disk** | ❌ Future (T1.3) | ⚠️ Anthropic/Claude Code pattern; LangChain Deep Agents |
| **Just-in-time retrieval over preloading** | ✅ Implemented (P10) — `context_mode` + `file_search` tool | ✅ Cursor, Copilot, Windsurf all do this |
| **Per-tool retention policy** | ❌ Future (T1.5) | ⚠️ OpenCode `PRUNE_PROTECTED_TOOLS` |
| **Conversation checkpointing** | ❌ Future (T2.1) | ⚠️ LangGraph MemorySaver, Letta MemFS |
| **Position-aware context reordering** | ❌ Future (T2.3) | ⚠️ Anthropic recommends; nobody automates |
| **Conversation spine extraction** | ❌ Future (T2.4) | ❌ Nobody (novel crabcakes idea) |
| **RL-trained self-summarization** | ❌ Future (T3.1) | ✅ Cursor Composer 2 (only one) |
| **Agentic memory (A-Mem/AgeMem)** | ❌ Future (T3.1) | ⚠️ Academic frontier (NeurIPS 2025) |
| **Temporal knowledge graph (Zep)** | ❌ Future (T3.2) | ⚠️ Zep/Graphiti production |
| **Token-eviction proxy (LLMLingua-2)** | ❌ Future (T3.3) | ⚠️ Academic frontier |

---

## Feature Matrix — vs. Open-Source Hidden Gems

From `docs/research/context-management-comparison.md` — crabcakes vs. smaller open-source projects:

| Capability | Crabcakes | agent-context-system | ACE (FCA) | agents-md | Context-Engine-AI |
|---|---|---|---|---|---|
| **Budget-aware system prompt** | ✅ 15% of CX | ❌ | ✅ 40-60% target | ❌ | ❌ |
| **Smart truncation with invariants** | ✅ Core files kept + CB-6 | ✅ 9:1 compression | ❌ | ❌ | ❌ |
| **Token-accurate counting** | ✅ tiktoken + cache | ❌ chars only | ❌ | ❌ | ❌ |
| **Summary-on-trim** | ✅ Budget-aware | ✅ Consolidation | ✅ Compaction | ❌ | ❌ |
| **Mid-conversation compaction** | ✅ Layer 1+2+3 | ✅ Auto-consolidation | ✅ FCA protocol | ❌ | ❌ |
| **Byte-cap output control** | ⚠️ Line-based (future: T1.3 offload) | ❌ | ❌ | ✅ `head -c 4000` | ❌ |
| **Semantic file partial reads** | ⚠️ Partial — `file_search` does name + grep content matching (P10); full symbol graph still future | ❌ | ❌ | ❌ | ✅ MCP symbol graph |
| **Self-improvement loop** | ✅ 5-layer stack | ❌ | ❌ | ❌ | ❌ |
| **Untrusted-data fences** | ✅ `<untrusted-project-data>` | ❌ | ❌ | ❌ | ❌ |
| **Multi-agent context coordination** | ✅ Thread-safe per-session isolation | ❌ | ❌ | ❌ | ❌ |
| **Pluggable strategy** | ✅ `ContextStrategy` protocol | ❌ | ❌ | ❌ | ❌ |
| **Offline-first / zero deps** | ⚠️ Needs tiktoken | ✅ Zero deps | ❌ | ✅ | ❌ |

---

## Summary

**Crabcakes leads in:**
- **Pluggable strategy architecture** — the only platform with a formal `ContextStrategy` protocol decoupling compaction policy from data model and runtime. Future strategies (RL-based, graph-based, agentic) can be swapped in without code surgery.
- **Layered compaction architecture** — the only platform with an explicit 3-layer pipeline (stub → trim → summarize)
- **CB-6 tool-call pairing** — the only platform that guarantees ASSISTANT+tool_calls and TOOL_RESULT are never separated during compaction, with formal tests
- **Multi-session thread safety** — the only platform that isolates compaction state across concurrent sessions on a shared runtime (lock + locals + session_key)
- **Compaction telemetry** — the most detailed observability (14-field events, per-turn breakdown callback)
- **Input hardening** — the only platform with explicit guards against pathological inputs (negative budgets, duplicate IDs, whitespace, zero tokens)
- **Audit rigor** — the only platform with a formal spec → implementation → adversarial audit → fix cycle (25 spec fixes + 8 audit-discovered bugs, 150 tests)
- **Untrusted-data fences** — `<untrusted-project-data>` pattern for prompt injection defense (unique among coding agents)
- **Self-improvement stack** — 5-layer learning system (bug journal → project rules → enforcement → structured feedback → dream consolidation)
- **JIT context discovery (P10)** — per-provider `context_mode` (auto/preload/jit/hybrid) with `file_search` tool and compact file index, resolving the upfront preload problem. Only platform with configurable per-provider context strategy.

**Cursor (Composer 2) leads in:**
- **RL-trained self-summarization** — the model itself learns what to keep/drop during compaction, achieving ~80% token reduction with 50% fewer compaction errors. This is the one area where Crabcakes' rule-based summary is weaker.

**Windsurf (Cascade) leads in:**
- **Codebase indexing + retrieval** — M-Query similarity search over indexed project
- **Multi-agent shared context protocol** — shared timeline across agents (different approach to multi-session)

**Claude Code leads in:**
- **Manual control** — `/compact`, `/clear`, `/context`, `/rewind` give the user direct power
- **Mature UX** — server-side compaction is battle-tested at scale

---

## What Crabcakes Could Add Next

| Feature | Source Inspiration | Effort | Impact |
|---|---|---|---|
| RL-trained self-summarization | Cursor Composer 2 | High | 🔴 High — would make summaries smarter |
| ~~Dynamic tool/context discovery~~ | ~~Cursor, Copilot~~ | ~~Medium~~ | ✅ **Done (P10)** — `context_mode` + `file_search` tool shipped |
| Manual compaction API | Claude Code | Low | 🟢 Nice-to-have — user control |
| Codebase indexing | Windsurf | High | 🟡 Medium — already handled by OpenClaw layer |
| Workspace rules file | All competitors | Low | 🟢 Nice-to-have — already handled by OpenClaw AGENTS.md |

---

## Research Backlog (from `docs/research/crabcakes-future-context-strategies.md`)

The research docs identify a phased roadmap for future context management improvements. The pluggable architecture makes all of these implementable as new strategies without runtime changes:

| Phase | Technique | Effort | Impact |
|---|---|---|---|
| **P8** | Recursive hierarchical summarization + structured summary schemas | Low | 🔴 High — stratified memory |
| **P9** | Tool-output offloading to disk + per-tool retention policy | Low-Med | 🔴 High — lossless compaction |
| **P10** | ~~Just-in-time file context retrieval~~ | ~~Medium~~ | ✅ **Done** — `context_mode` + `file_search` tool + `build_file_index` (P10)
| **P11** | Conversation checkpointing + adaptive context pressure thresholds | Low-Med | 🟡 Medium — debugging + tuning |
| **P12** | Conversation spine + position-aware context reordering | Medium | 🟡 Medium — persistent self-model |
| **P13** | Dream consolidation (cross-session synthesis) | High | 🟡 Medium — ChatGPT-style memory |
| **Future** | Agentic memory tools (A-Mem) | High | 🔴 High — frontier |
| **Future** | Temporal knowledge graph (Zep) | Very High | 🔴 High — frontier |

All phases are implementable within the `ContextStrategy` protocol — no runtime surgery needed.
