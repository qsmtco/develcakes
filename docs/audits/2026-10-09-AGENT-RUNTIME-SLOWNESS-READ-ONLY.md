# Investigation Report — Agent runtime feels slow (READ-ONLY)

**Date:** 2026-10-09
**Author:** Grok 4.7
**Scope:** develcakes working tree @ HEAD `4eb710de`. Read-only code reading of the tool loop, compaction, prompt build, and HTTP call path, plus isolated timings with `.venv/bin/python` on this machine. No GTK, no live provider calls, nothing persisted.
**Status:** DIAGNOSTIC ONLY — no code changed by this investigation; this report is the only artifact.
**Related:** `docs/audits/2026-09-01-core-loop-performance.md` (UI lag: GIL + per-delta `idle_add` flood). That report is about the window feeling stuck while a loop runs. This one is about wall time before the model answers, inside `AgentRuntime._run_loop`.

---

## TL;DR — VERDICT

The wait the user feels before the first token is mostly the provider: prefill of a large system prompt, plus a fresh TLS handshake on every model request. Local Python work is smaller, but two of the local costs are pure waste and show up on every tool iteration even when nothing changed.

Three tweaks, in the order I would do them:

1. **Stop clearing the tiktoken cache on a no-op compact.** `DefaultContextStrategy.compact()` wipes `conv._token_estimate_cache` at the start and again after the orphan sweep, even when it removed nothing. That forces two full encodes per LLM call. Clear the cache only after a real mutation, and return early when `get_token_estimate()` is already under `token_budget`.
2. **Stop counting lines in the file index, or cache the index the way `build_file_context` already caches.** Default `include_line_counts=True` opens up to 200 files. Measured **284 ms** on this repo. `build_file_context` is mtime-cached; `build_file_index` is not.
3. **Reuse HTTPS connections per host** inside `urlopen_with_ssl_retry`. Providers call `urllib.request.urlopen` with no keep-alive, so every tool round pays a new TLS handshake. This is the change that would show up before first token on a tool loop.

Deprioritized (measured, not worth touching first): `load_providers()` ~6 ms per call; tool-schema rebuild ~0 ms. The system prompt can be up to 25% of the context window and is resent on every tool iteration with no prompt-cache header — that is provider prefill, a product choice, not a local bug.

`tool_timeout_seconds` default **120** (`agent/config.py`) is the HTTP timeout, not a delay. It does not make a successful call wait 120 seconds.

---

## 1. Where each turn spends time

`AgentRuntime._run_loop` (`agent/runtime.py`, def at line 1452) does this on every tool iteration, before the model is asked anything:

| Step | Site | What it costs |
|---|---|---|
| Compact | `runtime.py:1619-1621` → `DefaultContextStrategy.compact` | Token recount even when already under budget. Measured no-op **~27 ms**. |
| Tool schemas | `runtime.py:1662-1663` `get_tool_definitions_for_api` | Negligible. 9 tools, **9,005 bytes** JSON, **~0.00 ms**. |
| Live provider card | `_call_llm` `runtime.py:2391-2397` `load_providers()` | YAML parse every call. Measured **~5.7 ms**. |
| HTTPS | `urlopen_with_ssl_retry` → `urllib.request.urlopen` (`agent/llm/streaming.py:330`) | New connection, new TLS, every call. Dominant perceived wait once the prompt is large. |

Prompt construction (file index, core files, system-prompt budget) happens when the turn's conversation is prepared, not inside the tight tool loop. `_prepare_turn_conversation` is `ui/handlers/agent_runtime_handler.py:969`. That path used to run on the GTK thread and was historically 300–500 ms; it now runs on the loop thread. `_rebuild_conversation_context` short-circuits when `project_path` and `system_prompt` already match, so a long tool loop does not rebuild the prompt every iteration. The file-index cost hits when the prompt is built or rebuilt, not on every tool round.

---

## 2. No-op compact recounts tokens twice

`Conversation.get_token_estimate` (`models/conversation.py:310`) uses tiktoken when it can (`cl100k_base` fallback, `conversation.py:24`). The cache key is `(len(messages), hash(system_prompt))` (`conversation.py:332`). The docstring still says a 100k-character prompt used to cost **~6 s** per encode before caching (`conversation.py:318-320`). On this machine that comment is historical: two encodes of 80k characters took **18 ms**, and `chars // 4` is essentially free.

`compact()` (`agent/context_strategy.py:122`) does not use that cache on the no-op path:

1. Line 145 reads `get_token_estimate()` (a hit if the cache was warm).
2. Line 146 sets `conv._token_estimate_cache = None` unconditionally, before any mutation.
3. `prune_tool_outputs` (`context_strategy.py:383-385`) estimates again. If already under `target_tokens` it returns 0 and stubs nothing. That second call is a full encode because step 2 wiped the cache.
4. The trim `while` (`context_strategy.py:171`) then sees a warm cache and does not enter.
5. The orphan sweep (`context_strategy.py:270-275`) rewrites `messages` and sets `_token_estimate_cache = None` again, even when the list is unchanged.
6. Back in the loop, `post_trim_estimate = conv.get_token_estimate()` (`runtime.py:1687`) encodes a third time... the compact measurement itself is the first forced encode; the post-trim read is the second full encode of the same unchanged conversation.

Line 258 also clears the cache, but only after a message is actually popped. That one is correct.

**Measured no-op compact:** average **27 ms**. Setup was a `Conversation` with an 80k-character system prompt, model `openrouter/deepseek`, 80 messages, and a budget of 4× the estimate (so compact had nothing to remove).

`prune_tool_outputs` already knows how to stop (`tokens_before <= target_tokens` → return 0). That early return is unreachable as a savings until `compact()` stops wiping the cache before calling it, and until `compact()` itself returns before the sweep when the estimate is already under budget.

**Tweak:** if `get_token_estimate() <= token_budget` and nothing else in this call will mutate messages, return without clearing the cache and without the orphan sweep. Clear `_token_estimate_cache` only after a pop, a stub, or a sweep that actually dropped a message.

---

## 3. File index line counts are uncached I/O

`build_file_index` (`agent/context.py:472`) defaults `include_line_counts=True`. For each of up to 200 files under 1 MB it opens the file and counts lines (`context.py:575-579`):

```python
with open(full_path, "rb") as f:
    lc = sum(1 for _ in f)
```

`build_file_context_with_core_files` calls it with the default for `jit` and `hybrid` (`context.py:674`). `resolve_context_mode` (`context.py:435`) maps `auto` to `hybrid` for a typical 128k window (the `return "hybrid"` at line 469). Default `context_mode` auto therefore pays this walk whenever the prompt is built.

`build_file_context` (`context.py:340`) caches on project path, `max_chars`, and root mtime (`context.py:365-371`). `build_file_index` has no equivalent cache. The docstring already says setting `include_line_counts=False` "reduces I/O for very large projects" (`context.py:488`).

**Measured** on `/home/mushy/projects/develcakes`: `build_file_index` returned **14,255 characters in 284 ms**.

An older audit (`docs/audits/2026-06-27-P10-ADVERSARIAL-AUDIT.md`, BUG #10) already recorded ~250 ms of line-count latency on a 787-file tree. Same function, same default.

**Tweak:** pass `include_line_counts=False` from the prompt builder, or mtime-cache the index the same way `build_file_context` caches. Line counts are display decoration in the system prompt; they are not required for the model to find a file.

---

## 4. Every model request opens a new HTTPS connection

`urlopen_with_ssl_retry` (`agent/llm/streaming.py:290`) retries SSL and DNS failures, then returns `urllib.request.urlopen(req, timeout=timeout)` (`streaming.py:330`). There is no connection pool. `urllib` does not keep the socket for the next request. Each tool iteration in `_run_loop` calls `_call_llm` / `_call_llm_streaming` (`runtime.py:2317` and `:2508`), and each of those pays DNS (if uncached), TCP, and TLS before the provider starts prefill.

The timeout passed in is `self._config.tool_timeout_seconds` (`runtime.py:2468`, `:2497`, and the summary call at `:3070`). Default is **120** (`agent/config.py:78`). That bounds a hung socket. It is not a sleep on the success path.

**Tweak:** a small per-host keep-alive pool inside `urlopen_with_ssl_retry` (or an `http.client.HTTPSConnection` reused across calls to the same origin). This is the local change that changes how fast a tool loop feels. The 2026-08-21 SSL note (`docs/investigations/2026-08-21-PANGO-WARNINGS-AND-SSL-RETRY.md`) already records that a reused connection can surface `SSLV3_ALERT_BAD_RECORD_MAC` after a network change; if reuse is added, a failed reused socket should be dropped and retried once on a fresh connection, which the existing retry loop is shaped to do.

---

## 5. Measured, and not the problem

Timings below are single-process, sandbox, `.venv/bin/python`, this repo, 2026-10-09. They are not a profile of a live OpenRouter round trip.

| What | Result |
|---|---|
| `build_file_index(this repo)` | 14,255 chars, **284 ms** |
| tiktoken `cl100k_base` on `README.md` | 49,404 chars, **11,972 tokens**, **5 ms** |
| tiktoken on `docs/ARCHITECTURE.md` | 283,441 chars, **69,183 tokens**, **27 ms** |
| two encodes of an 80k-char string | **18 ms** |
| `chars // 4` | ~0 ms |
| no-op `compact()` (80k prompt, 80 messages, budget 4× estimate) | **27 ms** average |
| `load_providers()` | **5.7 ms** average |
| `get_tool_definitions_for_api(None)` | **~0.00 ms**, JSON **9,005 bytes**, **9 tools** |

`load_providers` exists so a Settings save reaches a running agent without rebuilding it (`runtime.py:2378-2397` copies `base_url`, `caller`, and `api_key` from the live card onto the frozen provider config). 6 ms is not the slowness. Caching the parsed YAML on mtime would be correct and would not be felt.

Tool schemas are rebuilt every iteration and cost nothing at this size.

---

## 6. System prompt size is provider prefill, not a local loop bug

`_apply_system_prompt_budget` (`utils/prompt_loader.py:380`) caps templates plus file context at **25%** of the context window (`prompt_loader.py:409-419`: floor 15%, grows with template size, capped at 0.25). That block is resent on every tool iteration. Nothing in the request path sets a provider prompt-cache header.

`CORE_FILES` (`agent/context.py:427`) is `README.md`, `AGENTS.md`, `CONVENTIONS.md`, `ARCHITECTURE.md` at the **project root**. On this repo `ARCHITECTURE.md` lives at `docs/ARCHITECTURE.md` (**288,872 bytes** on disk) and is not inlined by that list. Root `README.md` is what gets inlined: the encode above is **~12k tokens / 5 ms** locally. The cost of resending it is on the provider, during prefill, every round.

Shrinking the prompt or turning on provider prompt caching would move first-token time. That is a product choice. It is not the wasted local work in sections 2 and 3.

---

## 7. What I would change, and what I would leave

Do these first. They are safe and they are pure waste:

1. Cache fix in `agent/context_strategy.py` `compact()` — do not clear `_token_estimate_cache` on the no-op path; early-return when already under budget (section 2).
2. Line counts — `include_line_counts=False` at the `build_file_index` call in `build_file_context_with_core_files`, or an mtime cache parallel to `_FILE_CONTEXT_CACHE` (section 3).

Do this next if tool loops still feel slow after the first token is the only remaining wait:

3. Per-host HTTP keep-alive in `urlopen_with_ssl_retry`, with one fresh-connection retry if the reused socket fails (section 4).

Leave alone until those are done: providers.yaml parse, tool-schema rebuild, system-prompt budget, `tool_timeout_seconds`.

---

## 8. What this investigation did not do

- No code changes.
- No live call to OpenRouter, Anthropic, Z.ai, or MiniMax, so TLS and prefill were not timed against a real provider. The "fresh TLS + prefill dominates" claim is from the call path (`urlopen` with no pool, full messages resent every iteration), not from a packet capture.
- No GTK profiling. UI-thread lag is the 2026-09-01 report, not this one.
- The 6-second tiktoken figure in `get_token_estimate`'s docstring was not reproduced. Current encodes on this machine are tens of milliseconds.
