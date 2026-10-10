# SPEC-21: Runtime Hot-Path Performance — stop paying for work that already happened

**Date:** 2026-10-09
**Author:** Supervisor
**Status:** READY — implements the three changes from the read-only investigation
**Implements:** `docs/audits/2026-10-09-AGENT-RUNTIME-SLOWNESS-READ-ONLY.md` (Grok 4.7), sections 2–4
**Depends on:** nothing (pure hot-path; no behavior change to model-visible content)
**Target branch:** main
**Amended:** 2026-10-09 by Claude Opus 5.5 — verification pass with fresh probes on this machine; every change is marked inline as `EDIT (Claude Opus 5.5)`. Why: (1) the largest local cost, a real compaction that recounts the whole conversation after every stub and pop (~1.1 s), was out of scope, and so was a second uncached full encode every tool round in `get_token_breakdown`; both get fixed by a new SP0 token-count memo. (2) The file-index walk is slow because of the `.gitignore` matcher, not line counts, and an mtime cache would serve the model a stale index; SP2 now fixes the matcher instead. (3) SP3 as written would share one socket between concurrent agent threads, return HTTP 4xx/5xx bodies as successful responses, and never reuse a connection on the streaming path the app always uses; SP3 now lists those as required fixes. (4) SP1 shrinks to a two-line change; the early-exit branch is not needed to reach one encode.

> Architecture compliance: touches `agent/context_strategy.py` (compaction layers — unchanged
> semantics, cheaper no-op), `agent/context.py` (adds a cache parallel to the existing
> `_FILE_CONTEXT_CACHE`), `agent/llm/streaming.py` (the single choke point all providers already
> use). No handler graph, no UI, no transport changes.
>
> **EDIT (Claude Opus 5.5):** `agent/context.py` now gets a compiled `.gitignore` matcher instead
> of a cache. Also touched: `models/conversation.py` (SP0, still pure data; tiktoken is already its
> dependency) and the five YAML call sites (SP4). `agent/runtime.py` still needs no edit.

---

## DISCOVERY

- **Read `agent/context_strategy.py`**: `DefaultContextStrategy.compact(self, conv, token_budget,
  *, keep_first=2, protect_is_summary=True) -> None` (line ~126). Line 146:
  `conv._token_estimate_cache = None` — executed UNCONDITIONALLY after the `tokens_before` read,
  before any mutation. The trim loop invalidates per-pop (line 258); `prune_tool_outputs`
  invalidates per-stub (line 438) and already early-returns when under target; the post-trim
  orphan sweep (lines 266–276) rebuilds `conv.messages` via filter and invalidates at line 275
  UNCONDITIONALLY — even when it removed nothing. Telemetry is recorded once at the end
  (lines 305–338) into `self._last_result` as a `CompactionEvent` (layer 0 = "no compaction
  occurred — report honestly", per the in-code comment).
- **Read `models/conversation.py`**: `get_token_estimate()` caches on the key
  `(len(self.messages), hash(self.system_prompt))` (line 332). In a live tool loop the key changes
  every iteration (assistant + user messages appended), so the FIRST estimate read of each
  compact() is an unavoidable full encode; every additional read of the SAME key inside compact()
  is recoverable waste. `Conversation.__init__` REQUIRES `agent_name` (probe-learned — the audit's
  repro sketch omits it). `add_user_message` does NOT bump `step_count`; `add_assistant_message`
  does (line 208).
- **PROBED (this machine, this repo, 2026-10-09)**: tiktoken encode of an ~88k-char conversation =
  **26.3 ms** steady-state (first-ever encode 168 ms — one-time BPE load). A true no-op compact
  costs **35 ms** and performs **three** full encodes of the same cache key: (1) `tokens_before`,
  (2) `prune_tool_outputs`' internal estimate after the line-146 wipe, (3) the final telemetry
  read after the line-275 wipe. Two of the three are pure waste. The audit measured 27 ms no-op;
  consistent.
- **Read `agent/runtime.py`**: `compact()` is called at line 1621 inside `_run_loop`; telemetry is
  consumed at line 1636 via `ev = self._context_strategy.last_result` and gated on
  `ev.messages_removed > 0 or ev.tokens_freed > 0`. **Hazard found (not in the audit):** if
  compact() ever returned WITHOUT recording `_last_result`, the runtime would read the previous
  turn's — or another session's — stale event and mis-attribute a compaction that did not happen.
  Any early-return path MUST record a fresh no-op event. Also consumed at `runtime.py:2991` and
  `ui/agent_runtime/session.py:224` — same contract.
- **Read `agent/context.py`**: `build_file_index(project_path, max_entries=200,
  include_line_counts=True)` (line 472) opens and line-counts up to 200 files ≤ 1 MB
  (lines 575–581). The only production caller is `build_file_context_with_core_files` line 674:
  `build_file_index(project_path)` — default True — on the jit/hybrid paths; `resolve_context_mode`
  maps `auto` → `hybrid` for typical windows, so the default config pays this on every prompt
  build. `build_file_context` (line 340) already caches on `f"{project_path}::{max_chars}"` +
  root mtime via `_FILE_CONTEXT_CACHE` (line 314) and the helper `_project_root_mtime`;
  `build_file_index` has NO cache. **PROBED**: 14,255 chars in **181 ms** with line counts,
  15,538→11,730 chars in **159 ms** without (audit: 284 ms cold — same order).
- **Read `agent/llm/streaming.py`**: `urlopen_with_ssl_retry(req, timeout, *,
  max_retries=MAX_SSL_RETRIES)` (line 290) is the single choke point — every provider call routes
  through it (openai_provider.py:77/121, anthropic_provider.py:83/136, minimax_provider.py:121/170,
  and the SSE streaming path). Line 330: `urllib.request.urlopen(req, timeout=timeout)` — no
  connection pool; every model request pays DNS+TCP+TLS. Callers consume the response via
  `resp.read()` or iteration, inside `with` blocks or plain assignment — so the replacement must
  expose read/readline/iteration/context-manager. The module's own docs record that a reused
  connection can surface `SSLV3_ALERT_BAD_RECORD_MAC` after a network change — reuse must drop
  the pooled socket and retry fresh (the existing retry loop is shaped for this).
- **Read `tests/test_context_strategy.py`**: lines 364–377 pin that `prune_tool_outputs` leaves the
  cache WARM and reflecting post-prune counts. The spec's changes must keep these green (they
  will — prune's own invalidation is untouched).
- **Architecture owner**: compaction semantics live in `agent/context_strategy.py` (per
  ARCHITECTURE.md §Compaction); prompt building in `agent/context.py`; the HTTP boundary in
  `agent/llm/streaming.py` (all providers, no exceptions).

### Probe table (mine, 2026-10-09 — supersedes inherited numbers where they differ)

| What | Result |
|---|---|
| tiktoken encode, ~88k-char conversation | **26.3 ms** steady (168 ms cold incl. BPE load) |
| no-op `compact()` (80k system prompt, 80 msgs, huge budget) | **35 ms**, 3 encodes, cache warm at exit |
| `build_file_index(this repo, line counts)` | 14,255 chars / **181 ms** |
| `build_file_index(this repo, no line counts)` | 11,730 chars / **159 ms** |
| encodes wasted per no-op compact | **2 × 26 ms ≈ 52 ms per tool iteration** |

### EDIT (Claude Opus 5.5) — verification probes, 2026-10-09

All probes ran on this machine against HEAD `4eb710de` with `.venv/bin/python`, nothing persisted. The source tree was not modified; variants were tested by monkeypatching or by exec'ing a patched copy of a function. Conversations below use an 80k-char system prompt (README ×2), model `openrouter/deepseek` (`cl100k_base`), and tool rounds whose results are 6,000 chars of `agent/context.py`.

| What | Result |
|---|---|
| True no-op tool round today (20 rounds, ~48k tokens): `compact()` + post-trim read + `get_token_breakdown` | **3** full encodes in `compact()` (sites `context_strategy.py:145`, `:383`, `:303`) + **1** uncached encode in `get_token_breakdown` = **~115 ms**. The DISCOVERY mechanism is confirmed. |
| `get_token_breakdown` (`runtime.py:1732`) | Runs every tool round in the app (the context meter is wired: `ui/window.py:782` → `agent_runtime_handler.py:879`). It re-encodes the system prompt and every message with **no cache**: **~21 ms** per round on top of `compact()`. The original spec leaves this untouched. |
| Real compaction (40 rounds, budget = 40% of estimate) | **39 full encodes, 1,109 ms.** Every stub (`:438`) and pop (`:258`) clears the cache, and the loop guard (`:406`, `:172`) recounts the entire conversation. This fires again on every round that crosses the soft ceiling. SP1 as written does not touch it. |
| Same two cases with a per-string token-count memo (SP0 prototype) | No-op round: 3 recounts in ~1 ms (dictionary lookups). Real compaction: **4 ms**. Messages, estimates, and `CompactionEvent` telemetry are identical to today. |
| Two-line SP1 (delete `:146`, clear after the sweep only if it removed something), exec'd as a patched copy | No-op: **1** encode (today 3). Real compaction: identical messages and telemetry. Under-budget orphan TOOL_RESULT: still swept. No early-exit branch needed. |
| tiktoken `encode()` on text containing `<\|endoftext\|>` | **Raises `ValueError`.** `get_token_estimate()` and `compact()` both raise. A tool result that quotes that literal (any tokenizer or LLM source file) would fail the turn, and because the message stays in history, every later turn too until `/clear`. `encode_ordinary()` returns identical counts on normal text, never raises, and is ~12% faster. Sites: `conversation.py:354-360`, `:382-389`; `context_strategy.py:290`, `:585`. |
| `build_file_index` profile (this repo, line counts off) | 1,567 `_is_ignored` → 4,859 `_match_gitignore` → **184,574 `fnmatch` calls ≈ 90% of the time.** Line counts add only ~25–30 ms. The walk itself (`os.walk` + `getsize`, no matching) is **9.6 ms**. |
| Compiled `.gitignore` matcher + per-name memo (SP2 replacement prototype) | `build_file_index`: **174 → 41 ms** with line counts, **145 → 14 ms** without. Output byte-identical. Truth table: 4,000 random pattern lists × 20 names, **0 mismatches** against today's `_match_gitignore` (negation, directory rules, char classes, `$`, `?` all exercised). |
| Line counting via `bytes.count(b"\n")` instead of `sum(1 for _ in f)` | 200 largest files: **29 → 12 ms**, identical counts (a final line with no trailing newline still counts). |
| `build_system_prompt` (this repo, `auto` → hybrid), min of 4 | **205 → 50 ms** with the compiled matcher and the C YAML loader; prompt byte-identical. |
| `file_search` tool name walk (`agent/tools.py:770` → `_find_matching_files`, `context.py:697`) | **38 → 3 ms** with the same matcher. This runs inside the tool loop on every `file_search` call. An index cache would not help it. |
| How often the prompt is built | Once per conversation: deferred first build (`runtime.py:1116-1162`), rebuild after load from disk (`:2790`; short-circuits at `:2837` when path and prompt match), or a project switch. **Not** per send or per round. SP2 is first-send latency. |
| YAML, libyaml installed (`yaml.CSafeLoader` present) | `load_providers()` (every `_call_llm`, `runtime.py:2397`): **4.65 → 0.69 ms**. `load_agent_defs()` (every prompt build): **28.6 → 5.2 ms**. Parsed results identical. |
| New connection (DNS + TCP + TLS), median of 4, from this machine | `openrouter.ai` **~80 ms**, `api.z.ai` **~138 ms**, `api.minimax.io` **~175 ms**. This is what SP3 saves per round once it works. The configured `localhost:18790` gateway is plain http; the pool skips it and there is no TLS to save. |

**Net per tool round on this repo, after SP0 + SP1:** token counting goes from ~115 ms to ~1 ms. A round that compacts goes from ~1.1 s to a few ms. SP3, when it works, removes another 80–175 ms per model request.

---

## 1. Overview

### Problem

Every tool iteration pays measurable local work that produces nothing: compaction re-encodes the
same unchanged conversation up to three times, prompt builds re-walk and re-count the whole file
tree on every rebuild, and every model request re-pays DNS+TCP+TLS because the HTTP layer keeps
no connections alive. The provider's prefill is the dominant wall-clock cost (a product choice,
per the audit §6) — but ~50 ms/iteration of encode waste and ~180 ms per prompt build are local,
and the TLS handshake sits directly in front of every first token.

**EDIT (Claude Opus 5.5):** the measured local waste is larger than this. A no-op round costs ~115 ms of token counting, because `get_token_breakdown` adds its own uncached full encode. A round that actually compacts costs ~1.1 s, because the trim and prune loops recount the whole conversation after every stub and pop. The prompt-build cost is the `.gitignore` matcher (~90% of the index walk), and it is paid once per conversation, not per round. The same matcher also slows every `file_search` tool call.

### Solution summary

~~Three~~ Five independent changes, ordered by audit priority and risk (**EDIT (Claude Opus 5.5):** SP0 and SP4 added; SP1 and SP2 rewritten; SP3 gets required fixes):

0. **SP0 — per-string token-count memo (EDIT, new).** `Conversation` remembers the token count
   of every string it has encoded, keyed by (encoding, length, hash). A recount after an append,
   stub, or pop becomes dictionary lookups plus the new strings. `get_token_breakdown` reuses it.
   `encode()` becomes `encode_ordinary()`, which removes the `<|endoftext|>` crash. Real
   compaction: 1,109 → 4 ms. No-op round: ~115 → ~1 ms of counting (with SP1).
1. **SP1 — compact() cache discipline.** Never wipe `_token_estimate_cache` speculatively.
   ~~Early-return (after recording a fresh no-op `CompactionEvent`) when already under budget.~~
   Run the orphan sweep always (defense in depth) but invalidate only when it actually dropped
   messages. Net: 3 encodes → 1 on the no-op path. **EDIT (Claude Opus 5.5):** that is the whole
   change: delete line 146 and guard the line-275 clear. Probed on a patched copy, it reaches
   1 encode with identical outputs. The early exit, `_strip_orphan_tool_results`, and
   `_record_event` extractions are dropped (§2.1).
2. **SP2 — ~~file-index mtime cache~~ compiled `.gitignore` matcher (EDIT).** ~~Mirror
   `_FILE_CONTEXT_CACHE`: key on path + max_entries + include_line_counts + root mtime.~~
   Compile the patterns once into one regex predicate with `_match_gitignore`'s exact truth
   table, plus a per-name memo, behind the unchanged `_match_gitignore` signature. Every
   walker gets faster, including the `file_search` tool, and there is nothing to invalidate.
   Line counts stay, now counted with `bytes.count`. Prompt content stays byte-identical
   (we do NOT drop line counts — that would change what the model sees).
3. **SP3 — per-host HTTP keep-alive, gated OFF by default.** A small pooled-connection fast path
   inside `urlopen_with_ssl_retry`; any pooled-connection failure drops the socket and falls
   through to the existing fresh-`urlopen` retry loop. Behind `DEVELCAKES_HTTP_KEEPALIVE=1`
   until measured against a live provider (the audit explicitly did not time TLS on the wire) —
   flipping the default is a follow-up decision with evidence. **EDIT (Claude Opus 5.5):** a new
   connection measures 80–175 ms per request to the configured hosts. §2.3 now lists the six
   fixes required before the gate can be turned on (thread checkout, streaming drain, HTTPError
   mapping, redirect fallback, idle expiry, silent fallback only for stale reused sockets). The
   gate is also read through `get_env`.
4. **SP4 — C YAML loader (EDIT, new, small).** Parse YAML with `yaml.CSafeLoader` when libyaml
   is present. `load_providers()` runs on every `_call_llm`: 4.65 → 0.69 ms. `load_agent_defs()`
   runs on every prompt build: 28.6 → 5.2 ms. Results identical.

### Scope

| In | Out |
|---|---|
| No-op compact encode elimination | Compaction layer semantics (untouched — same messages in, same messages out) |
| Orphan sweep kept, made invalidate-only-on-change | Removing the sweep (it stays — safety net) |
| ~~File-index mtime cache~~ Compiled `.gitignore` matcher + `bytes.count` line counts (EDIT) | Dropping line counts (would change model-visible prompt); an index cache (EDIT: stale-index risk, §2.2) |
| Per-string token-count memo; `encode_ordinary` everywhere tiktoken counts (EDIT) | Removing `_token_estimate_cache` and its manual clears (registered follow-up) |
| C YAML loader for the five `yaml.safe_load` sites (EDIT) | Caching parsed YAML |
| Pooled keep-alive behind an env gate, default OFF | Flipping the default (needs live-provider evidence) |
| Proxy-bypass in the pool (urllib honors proxies; http.client does not) | SOCKS/proxy support generally |

### Principles applied

- **No model-visible behavior change** — same prompts, same compaction outcomes, same wire
  payloads. Only wall time and CPU move.
- **Telemetry honesty preserved** — every compact() records a fresh `CompactionEvent`; runtime's
  stale-event hazard (Discovery) is explicitly closed.
- **Fail-open perf features** — cache and pool bugs degrade to today's behavior (recompute /
  fresh connection), never to wrong data.

---

## 2. Changes by File

### 2.0 `models/conversation.py` — SP0: per-string token-count memo (EDIT (Claude Opus 5.5), new)

**Why:** `_token_estimate_cache` holds one number for the whole conversation, so any change
(an append, a stub, a pop) throws all of it away and the next read re-encodes every string,
including the 80k-char system prompt that has not changed. The trim and prune loops change the
conversation once per step and read the estimate after each step, which is where the 39 full
encodes and 1.1 s of a real compaction come from. `get_token_breakdown` never used the cache.
Counting each string once fixes all three without touching compaction logic.

**New field** next to `_token_estimate_cache` (line 184), and a module constant:

```python
_TOKEN_MEMO_MAX = 20_000

    _token_count_memo: dict = field(default_factory=dict, repr=False, compare=False)
```

**New helper**, used by every tiktoken count in the class:

```python
    def _count_text(self, encoding, text: str) -> int:
        """Token count for one string, encoded at most once per conversation.

        encode_ordinary, not encode: encode() raises ValueError on any text
        containing a special-token literal such as "<|endoftext|>".
        """
        if not text:
            return 0
        key = (encoding.name, len(text), hash(text))
        count = self._token_count_memo.get(key)
        if count is None:
            if len(self._token_count_memo) >= _TOKEN_MEMO_MAX:
                self._token_count_memo.clear()
            count = len(encoding.encode_ordinary(text))
            self._token_count_memo[key] = count
        return count
```

**`_count_tokens_accurate` (lines 342–361)** becomes the same loop through `_count_text`:

```python
        total = self._count_text(encoding, self.system_prompt)
        for msg in self.messages:
            total += self._count_text(encoding, msg.content or "")
            for tc in msg.tool_calls:
                total += self._count_text(encoding, str(tc.arguments))
                if tc.result:
                    total += self._count_text(encoding, tc.result)
        return total
```

**`get_token_breakdown` (lines 380–389)**, tiktoken branch, stops re-encoding:

```python
            system_tokens = self._count_text(encoding, self.system_prompt)
            conversation_tokens = self.get_token_estimate() - system_tokens
```

This gives the same numbers as today: `get_token_estimate` counts exactly the system prompt plus
the pieces the old breakdown loop counted. At `runtime.py:1732` the estimate is already warm from
the post-trim read at `:1687`, so the breakdown costs two lookups.

**What stays:** `_token_estimate_cache`, its key, and every existing clear. The memo sits under
it, so the clears become cheap instead of being removed. The `chars // 4` fallback path is unchanged.

**Safety notes (verified):**
- The memo is not persisted: `agent/persistence.py` writes an explicit field list (no `asdict`,
  `fields()`, or `__dict__`), so the private field never reaches disk.
- The key includes the encoding name, so a model switch that changes the encoding cannot reuse
  counts. It includes the length, so a wrong count would need a 64-bit hash collision between two
  different strings of the same length. The value is a budgeting estimate.
- `str` hashes are cached by CPython, so repeated lookups of the same message are O(1). The
  per-call `str(tc.arguments)` is re-hashed, which is still far cheaper than BPE.
- Memory is one small tuple and int per distinct string, cleared at 20,000 entries.

**Also `encode` → `encode_ordinary` at `agent/context_strategy.py:290` (summary tokens) and
`:585` (`_fit_summary`).** An LLM-written summary can contain the literal too.

**Line estimate:** ~30.

### 2.1 `agent/context_strategy.py` — SP1: cache discipline in compact()

**EDIT (Claude Opus 5.5) — SP1 is now only the old Change 3.** The original Change 1
(extract `_strip_orphan_tool_results`) and Change 2 (early exit with `_record_event`) are
dropped. I exec'd a patched copy of `compact()` with only the two edits below. It reaches
**1** encode on a true no-op (today 3), produces identical messages and telemetry on a real
compaction, and still sweeps an under-budget orphan. On the single path the cache stays warm from
`tokens_before`: `prune_tool_outputs` returns on a hit at `:383`, the trim-loop guard hits, the
sweep removes nothing, and the telemetry read at `:303` hits. So the early exit buys nothing,
and it would cost a second code path, two method extractions (~55 lines), and one behavior
change the original spec did not list. Today, an under-budget sweep that drops an orphan sets
`messages_removed > 0`, and summary injection runs (`:281-282`), which is an LLM call under
`LLMSummarizeStrategy`. The early exit would silently skip that. The A2 stale-event concern is
already met, because the single path always records `_last_result` at `:323`.

**Change 3 — the full path keeps its shape, minus the two wipes:**

- Delete line 146 (`conv._token_estimate_cache = None`) entirely.
- Layer 1 (`prune_tool_outputs`), trim loop, summary injection: UNCHANGED.
- The end-of-compact sweep (lines 266–276) keeps its body and only guards the clear
  (**EDIT (Claude Opus 5.5):** inline, no helper):

```python
        before_sweep = len(conv.messages)
        conv.messages[:] = [
            m
            for m in conv.messages
            if m.role != MessageRole.TOOL_RESULT or m.tool_call_id in valid_call_ids
        ]
        if len(conv.messages) != before_sweep:
            conv._token_estimate_cache = None
```

`prune_tool_outputs` is UNTOUCHED (its per-stub invalidation at line 438 is already correct, and
tests/test_context_strategy.py:364–377 pin it).

**Imports required:** none beyond what the module already has.

**Line estimate:** ~~~55 (net)~~ ~4 (EDIT).

### 2.2 `agent/context.py` — SP2: ~~mtime cache for build_file_index~~ compiled `.gitignore` matcher (EDIT (Claude Opus 5.5))

**Why the mtime cache was replaced.** (1) It caches the wrong thing. 90% of the walk is
`_match_gitignore` calling `fnmatch` once per pattern per path segment (184,574 calls for 1,567
paths). The walk itself is 9.6 ms. (2) It would show the model a stale index. The root
directory's mtime changes only when an entry directly in the root is added, removed, or renamed.
Agents mostly edit inside `agent/`, `ui/`, `tests/`, and `docs/`, so a cached index would miss new
files and keep old sizes and line counts for the whole app session. That breaks this spec's own
"no model-visible behavior change" principle. (3) It helps only repeat builds. The prompt is built
once per conversation, so the first send pays in full either way, and `file_search` and
`_build_directory_tree` would not benefit at all. A faster matcher fixes every call, keeps output
byte-identical, and has nothing to invalidate.

**New helper** (above `_match_gitignore`, line 54). It compiles the patterns once into a predicate
with the same truth table as today's loop: first matching rule wins, a `!` rule returns False,
`x/` rules match the bare segment, and `a/b/` rules never match:

```python
def _compile_gitignore(patterns: list[str]) -> Callable[[str], bool]:
    """Compile .gitignore patterns into one predicate equal to _match_gitignore.

    fnmatch() normalizes with os.path.normcase, the identity on POSIX, so
    re.match(translate(p)) is the same test it performed per pattern.
    """
    rules: list[tuple[bool, str]] = []
    for pattern in patterns:
        negated = pattern.startswith("!")
        active = pattern[1:] if negated else pattern
        if active.endswith("/"):
            active = active[:-1]
            if "/" in active:
                continue
        rules.append((negated, active))
    if not any(negated for negated, _ in rules):
        if not rules:
            return lambda name: False
        combined = re.compile("|".join(translate(active) for _, active in rules))
        return lambda name: combined.match(name) is not None
    compiled = [(negated, re.compile(translate(active))) for negated, active in rules]

    def match(name: str) -> bool:
        for negated, rx in compiled:
            if rx.match(name):
                return not negated
        return False

    return match
```

**`_match_gitignore` keeps its signature** so `_is_ignored` and every caller change with zero
call-site edits: the directory tree (`context.py:130`), key files (`:294`), the index
(`:504/:519`), and the `file_search` name walk (`:712/:725`):

```python
_GITIGNORE_MATCHERS: dict[tuple[str, ...], tuple[Callable[[str], bool], dict[str, bool]]] = {}


def _match_gitignore(name: str, patterns: list[str], anchored: bool = False) -> bool:
    key = tuple(patterns)
    entry = _GITIGNORE_MATCHERS.get(key)
    if entry is None:
        entry = _GITIGNORE_MATCHERS[key] = (_compile_gitignore(patterns), {})
    matcher, seen = entry
    hit = seen.get(name)
    if hit is None:
        if len(seen) >= 50_000:
            seen.clear()
        hit = seen[name] = matcher(name)
    return hit
```

`anchored` was already unused by the body; keep it for the signature. Imports: `from fnmatch import
translate` replaces `from fnmatch import fnmatch` (lines 74 and 78 are its only users). The
keying follows the `.gitignore` content, so editing `.gitignore` produces a new key and a new
matcher. A concurrent first use on two threads can compile twice, which is harmless.

**Line counting, same output, ~2.4× faster** (lines 578–579):

```python
                    with open(full_path, "rb") as f:
                        data = f.read()
                    lc = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
```

**Measured:** `build_file_index` 174 → 41 ms with line counts (145 → 14 ms without),
`build_system_prompt` 205 → 50 ms (with SP4), `file_search` name walk 38 → 3 ms. All outputs
are byte-identical.

**Line estimate:** ~~~20~~ ~45 (EDIT).

### 2.3 `agent/llm/streaming.py` — SP3: gated per-host keep-alive

**New env gate + pool** (module level):

```python
import http.client
import threading
from urllib.parse import urlsplit

from utils.config import get_env

def _keepalive_enabled() -> bool:
    """SPEC-21 SP3: DEVELCAKES_HTTP_KEEPALIVE=1 enables the pooled fast path.
    Default OFF until measured against a live provider (the 2026-10-09 audit
    did not time TLS on the wire). =1 pools; anything else = today's behavior."""
    return get_env("HTTP_KEEPALIVE") == "1"

_POOL: dict[tuple[str, str, int], list[tuple["http.client.HTTPSConnection", float]]] = {}
_POOL_LOCK = threading.Lock()
_POOL_MAX_IDLE = 8          # idle connections kept, all origins
_POOL_IDLE_S = 30.0         # never reuse a socket idle longer than this
```

**EDIT (Claude Opus 5.5):** the env read goes through `get_env` (`utils/config.py:44`: "Sites must
not read os.environ directly for the renamed family"; `agent/context.py` already imports it). The
pool holds idle connections only. A connection is **removed** from `_POOL` while a request uses it,
and goes back only on a clean close (fix 1 below). `_POOL_IDLE_S` is fix 5.

**The pooled fast path** — attempted ONCE before the existing retry loop; on ANY failure it
drops the socket and falls through to today's `urlopen` path unchanged.
**EDIT (Claude Opus 5.5):** it now runs inside the loop through `_open` (see Wire-in), and the
"NEVER raises" contract below is replaced by fixes 3 and 6. It raises `HTTPError` for ≥ 400, and
passes non-stale failures to the existing except clauses.

```python
class _PooledResponse:
    """Minimal HTTPResponse-shaped wrapper so callers keep their existing
    read()/readline()/iteration/with usage. On close(): if the body was fully
    consumed, the connection returns to the pool; otherwise it is discarded."""
    ...

def _try_pooled(req: urllib.request.Request, timeout: float):
    """One pooled attempt. Returns a _PooledResponse, or None to fall back
    (pool disabled, proxy in play, unknown URL shape, any error). NEVER raises."""
```

Semantics (all verified against the call sites):

- Only when `_keepalive_enabled()`, method is POST (all provider calls are), scheme is https,
  and `urllib.request.getproxies()` has NO entry for the origin — **urllib honors proxies via
  `urlopen`; `http.client` does not. A proxied environment must bypass the pool or requests
  silently leave the machine by the wrong path.**
- Build `http.client.HTTPSConnection(host, port, timeout=timeout)`, `request(method, path,
  body=req.data, headers=dict(req.headers))`, `getresponse()`.
- `timeout` applies to connect + up to the first byte, exactly as callers pass it today.
  **EDIT (Claude Opus 5.5):** it is a socket timeout, so it applies to every blocking read,
  including between SSE chunks. That is what `stream_with_ssl_retry`'s `TimeoutError` branch
  (`streaming.py:429`) relies on. On a reused connection, set it again per request with
  `conn.sock.settimeout(timeout)`, because the socket keeps whatever the previous caller passed.
- ANY exception → close/~~discord~~ discard the connection, `_POOL.pop(origin)`, log at DEBUG, return None.
  The existing loop then performs a normal fresh `urlopen` attempt — retry accounting unchanged.
  **EDIT (Claude Opus 5.5):** narrowed by fixes 3, 4, and 6 below. "ANY exception → silent
  re-send" would re-POST a 429 and double a 120 s timeout.

**EDIT (Claude Opus 5.5) — required before the gate may be turned on.** The §9a prototype
proved reuse with full-body reads on one thread. The app does not run that way: it streams,
runs several agent sessions on separate loop threads, and relies on `HTTPError` for every
non-2xx. Each fix below closes a case where SP3 would break a request or never help:

1. **Thread checkout, never sharing.** Under `_POOL_LOCK`, pop an idle connection for the origin
   or open a new one. The request runs with the connection outside the pool, and only a clean
   close returns it. Concurrent sessions on the same origin (Supervisor and Coder both on
   `openrouter.ai`) then each get their own socket. Without this, two threads can write to and
   read from one `HTTPSConnection`, and one agent can receive the other's completion.
2. **Drain before returning a streamed connection.** Every streaming consumer stops reading at
   `data: [DONE]` or a `finish_reason` (`openai_provider.py:137-139`, `:183-184`), before the
   chunked terminator. The UI always streams (`runtime.py:2451-2454`), so "return only if fully
   consumed" alone means the main path **never** reuses. On `close()`: if the response is not
   `isclosed()`, read it to EOF with a 1 s socket timeout and a 64 KB cap. If it reaches EOF,
   return the connection to the pool; otherwise close it. After `[DONE]` only the `0\r\n\r\n`
   terminator remains, so the drain is one short read.
3. **Status ≥ 400 → raise `urllib.error.HTTPError(req.full_url, status, reason, headers, fp=resp)`.**
   `http.client` returns error statuses as normal responses; urllib raises. Five of the six call
   sites catch `HTTPError` and read `e.code` and `e.read()` (`openai_provider.py:79`, `:122`;
   `minimax_provider.py:133`, `:171`; `anthropic_provider.py:85`), and the Anthropic stream lets
   it propagate to the runtime. A pooled 429 returned as a response would be parsed
   as an empty completion, or as an SSE stream with no events. Raise it and do **not** fall
   back, because a fallback re-POSTs a rate-limited or oversized request. Discard the connection
   once the caller closes the error.
4. **Status 3xx → discard and fall back to `urlopen`.** urllib follows redirects; `http.client`
   does not. Nothing was processed, so re-sending is safe and gives today's behavior.
5. **Idle expiry.** Do not reuse a connection idle longer than `_POOL_IDLE_S` (store
   `time.monotonic()` on return). Tool rounds that run a long `exec_command` would otherwise
   mostly pick up sockets the server has already closed.
6. **Silent fallback only for a stale reused socket.** Fall back to a fresh `urlopen` without
   counting a retry only when a **reused** connection fails before any response byte with a
   stale-socket signature: `RemoteDisconnected`, `BrokenPipeError`, `ConnectionResetError`,
   `CannotSendRequest`, `BadStatusLine`, or `ssl.SSLError`/`SSLEOFError`. Any other failure,
   including every failure on a freshly opened connection and every `TimeoutError`, goes into the
   existing except clauses so retry accounting and backoff stay as they are. Match urllib's
   `do_open` exception shape: an `OSError` raised while sending becomes `URLError(err)`, and
   exceptions from `getresponse()` propagate raw. That keeps `is_retryable_ssl_error` and
   `friendly_error_message` classifying exactly as today.

**Wire fidelity:** send `req.header_items()`, which merges `headers` and `unredirected_hdrs`
(`dict(req.headers)` drops the latter). Add `User-Agent: Python-urllib/<major>.<minor>` when it is
absent, because urllib's opener sends it today and the providers sit behind CDNs that look at it.
Never send `Connection: close`, which urllib's `do_open` adds and is the reason it cannot reuse.
`http.client` adds `Host`, `Content-Length`, and `Accept-Encoding: identity` like urllib does.
Proxy check: skip the pool when `urllib.request.getproxies()` has an `https` entry at all. That is
conservative, and `no_proxy` subtleties are out of scope.

**Wire-in** — ~~one stanza at the top of `urlopen_with_ssl_retry`, before `last_exc = None`:~~
**EDIT (Claude Opus 5.5):** inside the loop, so fix 6's non-stale failures land in the existing
except clauses. Line 330 changes from `return urllib.request.urlopen(req, timeout=timeout)` to:

```python
            return _open(req, timeout)
```

where `_open` tries the pooled path when enabled and eligible, and otherwise (or on a stale
reused socket) returns `urllib.request.urlopen(req, timeout=timeout)`. With the gate off, `_open`
is exactly today's call.

The original stanza (`pooled = _try_pooled(req, timeout)` / `if pooled is not None: return pooled`
before the loop) is withdrawn.

~~**Eviction:** on insert, if `len(_POOL) >= _POOL_MAX_HOSTS`, pop an arbitrary entry and close it
(bounded state; agents talk to a handful of origins).~~ **EDIT (Claude Opus 5.5):** cap the total
idle connections at `_POOL_MAX_IDLE`; close the oldest idle connection when returning one would
exceed it. Connections in use are not in the pool, so they never count against the cap and are
never evicted mid-request.

**Line estimate:** ~~~110~~ ~170 (EDIT — checkout, drain, HTTPError mapping, idle expiry).

### 2.4 YAML sites — SP4: C loader when available (EDIT (Claude Opus 5.5), new)

One helper, in a `utils/` module the five sites can import:

```python
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def safe_load_yaml(stream):
    return yaml.load(stream, Loader=_YAML_LOADER)
```

Replace `yaml.safe_load(...)` at `utils/providers_store.py:120` (read by `load_providers()` on
every `_call_llm`, `runtime.py:2397`), `utils/agent_defs.py:68` and `:318` (every prompt build
through `_get_agent_self_improvement_config`), `ui/agent_runtime/provider.py:66`, and
`utils/telegram_store.py:61`. Measured: `load_providers()` 4.65 → 0.69 ms, `load_agent_defs()`
28.6 → 5.2 ms, parsed results identical. Both loaders are safe loaders, so nothing beyond plain
YAML types can be constructed. The one difference is error-message text on malformed files.
No test asserts YAML error strings (grepped 2026-10-09).

**Line estimate:** ~12.

### Files NOT changed (verified correct)

- ~~`models/conversation.py` — the cache key `(len(messages), hash(system_prompt))` and every
  `add_*` invalidation stay as-is; the loop-appended messages already change the key per turn.~~
  **EDIT (Claude Opus 5.5):** now changed by SP0 (§2.0). The cache key and every `add_*`
  invalidation still stay as they are.
- `agent/runtime.py` — the telemetry consumer (line 1636) needs no edit because every compact()
  path still records a fresh event.
- `tests/test_context_strategy.py` — untouched; its prune-cache pins (364–377) stay green.
- `agent/llm/openai_provider.py` / `anthropic_provider.py` / `minimax_provider.py` — zero edits;
  they already route everything through the single choke point.

---

## 3. Data Flow

**No-op compact (the hot path — every tool iteration under budget):**

**EDIT (Claude Opus 5.5):** redrawn for the single-path SP1 plus SP0.

```
_run_loop iteration N
  → _compute_compaction_threshold(conv)          # unchanged
  → DefaultContextStrategy.compact(conv, soft_ceiling)
      → tokens_before = get_token_estimate()      # key changed since last turn → recount; SP0 makes it
                                                  #   lookups + the 1–2 new strings (system prompt not re-encoded)
      → prune_tool_outputs → :383 cache hit → return 0
      → trim-loop guard → cache hit → loop not entered
      → orphan sweep → 0 removed → no clear
      → tokens_after = get_token_estimate()       # cache hit
      → _last_result = CompactionEvent(layer 0)   # same single recording site as today (:323)
  → post_trim_estimate (:1687)                    # cache hit
  → get_token_breakdown (:1732)                   # SP0: two lookups (was a full uncached encode)
```

Token counting per no-op round: ~115 ms today → ~1 ms.

**Over-budget compact:** identical to today except the line-146 wipe is gone (Layer 1's first
read hits the encode-#1 cache) and the sweep invalidates only on removal. **EDIT (Claude Opus 5.5):**
each stub or pop still clears `_token_estimate_cache`, and the loop guards still recount after
each step. With SP0 each recount is lookups plus the one new stub string: 1,109 ms → 4 ms on the
probe conversation, with identical messages removed and identical stubs.

**Prompt build (project open / context rebuild):**

```
build_file_context_with_core_files
  → build_file_index(project_path)               # EDIT: compiled matcher → ~41 ms (was ~174 ms), every call
  → unchanged index text → byte-identical system prompt
```

**Model request (SP3, when DEVELCAKES_HTTP_KEEPALIVE=1):**

```
urlopen_with_ssl_retry(req, timeout)          # EDIT (Claude Opus 5.5): pooled path runs INSIDE the loop
  → loop attempt k → _open(req, timeout)
      → eligible (gate on, https, POST, no proxy) → checkout idle conn ≤ 30 s old, or open new
          → request/getresponse
              → 2xx  → _PooledResponse (close: drain ≤ 64 KB / 1 s → back to pool, else discard)
              → 3xx  → discard → urllib.request.urlopen (follows redirects as today)
              → ≥400 → raise HTTPError(fp=resp) → providers' existing handlers
          → stale reused socket before any byte → discard → urllib.request.urlopen (no retry counted)
          → any other error → urllib's exception shape → existing except clauses (backoff unchanged)
      → not eligible → urllib.request.urlopen (today's call)
```

---

## 4. File Change Summary

**EDIT (Claude Opus 5.5):** rows for SP0, SP2, SP3, and SP4 revised; the original rows are struck.

| File | Change | Lines | Risk |
|---|---|---|---|
| `models/conversation.py` | SP0: `_token_count_memo` + `_count_text`; `_count_tokens_accurate` and `get_token_breakdown` use it; `encode_ordinary` | ~30 | **Medium** — every budget decision reads it; mitigated by identical-count tests and the unchanged cache layer above it |
| `agent/context_strategy.py` | SP1: delete line 146, guard the line-275 clear; `encode_ordinary` at :290 and :585 | ~6 | Low — single path unchanged; probed identical outputs |
| ~~`agent/context_strategy.py`~~ | ~~early-exit + sweep extraction + telemetry helper; delete two wipes~~ | ~~~55~~ | dropped (§2.1 EDIT) |
| `agent/context.py` | SP2: `_compile_gitignore` + memoized `_match_gitignore`; `bytes.count` line counts | ~45 | Low — truth table pinned by a randomized equivalence test |
| ~~`agent/context.py`~~ | ~~`_FILE_INDEX_CACHE` + wrap~~ | ~~~20~~ | replaced (§2.2 EDIT) |
| `agent/llm/streaming.py` | SP3: `_open` + checkout pool + `_PooledResponse` (drain) + HTTPError/3xx mapping + idle expiry, env-gated OFF | ~170 | **Medium-High when enabled** — concurrency and error mapping are where it breaks; default OFF |
| `utils/` YAML helper + 5 call sites | SP4: `safe_load_yaml` with `CSafeLoader` fallback | ~12 | Low |
| `tests/test_token_memo.py` | NEW (SP0) | ~90 | — |
| `tests/test_compact_cache_discipline.py` | NEW (SP1) | ~~~120~~ ~60 | — |
| `tests/test_gitignore_matcher.py` | NEW (SP2; replaces `test_file_index_cache.py`) | ~70 | — |
| ~~`tests/test_file_index_cache.py`~~ | ~~NEW~~ | ~~~70~~ | replaced |
| `tests/test_http_keepalive.py` | NEW (local `http.server` control, test_live_guard pattern) | ~~~110~~ ~180 | — |
| `tests/conftest.py` | extend: autouse fixture that unsets `DEVELCAKES_HTTP_KEEPALIVE` / `CRABCAKES_HTTP_KEEPALIVE` outside `test_http_keepalive.py` | ~8 | — |
| `docs/ARCHITECTURE.md` | perf paragraph | ~4 | — |

---

## 5. Implementation Order

**EDIT (Claude Opus 5.5):** SP0 goes first. It is the biggest win, and SP1's tests count encodes
through the same code. SP2 RED/GREEN is replaced, SP3 gains the cases that decide whether it
helps or breaks, and SP4 is last.

0. **SP0 RED → GREEN (EDIT).** In `tests/test_token_memo.py`:
   (a) a tool result containing `<|endoftext|>` → `get_token_estimate()` and `compact()` do not
   raise (red today: `ValueError`);
   (b) across five appended tool rounds with an unchanged system prompt, the system prompt is
   passed to `encode_ordinary` **once** (wrap the encoding object and record inputs);
   (c) a real compaction (40 rounds, budget 40% of the estimate) only encodes strings it created,
   meaning stubs and summary (no input longer than the longest stub or summary);
   (d) `get_token_breakdown` returns the same three numbers as an independent full
   `encode_ordinary` count, and encodes nothing when the estimate is warm;
   (e) changing `conv.model` to one with a different encoding does not reuse counts.
   Implement §2.0, then run the compaction suites below: all green, with identical
   compaction outcomes.
1. **SP1 RED:** encode-count harness — monkeypatch
   `Conversation._count_tokens_accurate` with a counting wrapper; assert a no-op compact performs
   **≤ 1** encode (today: 3). Assert the fresh no-op `CompactionEvent` (layer 0, freed 0,
   `turn == conv.step_count`) and the stale-event guard (compact-with-trim on conv A, then
   under-budget compact on conv B → `last_result` describes B). Run: red.
2. **SP1 GREEN:** implement §2.1. Re-run with the three existing compaction suites
   (`test_context_strategy*.py`, `test_runtime_compaction.py`, `test_compact_command.py`) — all
   green, prune pins intact.
   **EDIT (Claude Opus 5.5):** with the single-path SP1, the stale-event test passes today as well
   (the path always records), so it pins rather than goes red. The RED is the ≤ 1 encode assertion.
3. ~~**SP2 RED:** second `build_file_index` call with unchanged root must not re-walk
   (monkeypatch `os.walk` to raise on the second call) and must return a byte-identical string;
   mtime bump (`os.utime`) must invalidate; `max_entries`/`include_line_counts` are part of the
   key. Run: red.~~
   **SP2 RED (EDIT):** `tests/test_gitignore_matcher.py` imports `_compile_gitignore` (red:
   ImportError). It embeds a verbatim copy of today's `_match_gitignore` loop as the reference and
   asserts equal results over randomized pattern lists (negation, `x/`, `a/b/`, `[...]`, `[!...]`,
   `?`, `*`, `$`) × names, plus this repo's real `.gitignore` × every name the walk visits. Also:
   `build_file_index` on a tmp tree, with and without line counts, equals the output produced
   with the reference matcher patched in; and line counts are unchanged for files with and
   without a trailing newline, and for empty files.
4. **SP2 GREEN:** implement §2.2. Verify `tests/test_jit_context_discovery.py` stays green.
5. **SP3 RED/GREEN (gated):** local `http.server` control (the test_live_guard pattern);
   with `DEVELCAKES_HTTP_KEEPALIVE=1`, two requests reuse one connection (server counts
   connections); any pooled failure falls back to a fresh `urlopen` transparently; gate off →
   zero pooling. Proxy-bypass unit: `getproxies` patched → `_try_pooled` returns None.
   **EDIT (Claude Opus 5.5) — add:** (a) a chunked SSE response read only up to `data: [DONE]`,
   then a second request reuses the connection (the app's real path); (b) two threads issuing
   requests at once each get their own body (tag bodies per request) over two connections;
   (c) a 429 raises `HTTPError` with a readable body, and the server sees exactly one request;
   (d) a 302 is followed by the urllib fallback; (e) a connection idle past `_POOL_IDLE_S`
   (patched `time.monotonic`) is not reused; (f) the server closes a pooled socket, and the next
   call succeeds without a backoff sleep (patch `time.sleep` to raise); (g) a pooled
   `TimeoutError` reaches the existing retry loop and is not silently re-sent first.
   `tests/conftest.py` autouse fixture unsets both gate names everywhere else, so an exported
   gate in a developer shell never routes the existing `urlopen`-patched tests through a real
   `HTTPSConnection`.
5b. **SP4 (EDIT):** swap the five sites; run `tests/test_providers_store.py`,
   `tests/test_agent_defs.py`, and `tests/test_telegram_store.py`. (Grepped 2026-10-09: no test
   asserts YAML error-message text.)
6. **Full gate:** `xvfb-run -a .venv/bin/python -m pytest tests/test_context_strategy.py
   tests/test_context_strategy_audit_fixes.py tests/test_context_strategy_audit_fixes2.py
   tests/test_context_strategy_audit_fixes3.py tests/test_runtime_compaction.py
   tests/test_compact_command.py tests/test_jit_context_discovery.py
   tests/test_llm_streaming.py tests/test_streaming.py tests/test_compact_cache_discipline.py
   ~~tests/test_file_index_cache.py~~ tests/test_gitignore_matcher.py tests/test_token_memo.py
   tests/test_http_keepalive.py -q` — paste actual output;
   ruff 0 new on touched files; pyright 0.

---

## 6. Acceptance Criteria

- [ ] **A1** A no-op `compact()` performs **≤ 1** tiktoken encode (count-instrumented), down
      from 3. On an 88k-char conversation this is ~52 ms saved per tool iteration.
      **EDIT (Claude Opus 5.5):** count `_count_tokens_accurate` calls for this criterion. After
      SP0 each of those calls is mostly lookups, so A9 covers the actual encode work.
- [ ] **A2** Every `compact()` path records a FRESH `CompactionEvent`; the stale-event scenario
      (real trim on A, then under-budget call on B) reports B, not A.
- [ ] **A3** Orphan TOOL_RESULTs are still stripped when under budget (sweep retained); a sweep
      that removes messages invalidates the cache; one that removes none does not.
- [ ] **A4** Over-budget compaction outcomes are byte-identical to today (same messages removed,
      same stubs, same summary injection) — asserted by the existing suites staying green.
- [ ] ~~**A5** Second `build_file_index` call (unchanged root mtime, same args) returns a
      byte-identical string without re-walking; `os.utime` on the root invalidates; args are
      part of the key.~~
- [ ] **A5 (EDIT (Claude Opus 5.5))** `_compile_gitignore` matches today's `_match_gitignore` on
      every randomized and real-repo case; `build_file_index` output is byte-identical with and
      without line counts; `fnmatch` is no longer imported by `agent/context.py`.
- [ ] **A6** With `DEVELCAKES_HTTP_KEEPALIVE=1`, two sequential provider-shaped POSTs to a local
      server reuse one TCP connection; any pooled error falls back to a fresh connection within
      the same call; with the gate unset, behavior is byte-identical to today (pool never used).
      **EDIT (Claude Opus 5.5):** "any pooled error" is narrowed to stale reused sockets (§2.3
      fix 6), and the SSE-stopped-at-`[DONE]` reuse case is part of A6.
- [ ] **A7** Proxied environments never touch the pool (`getproxies` bypass unit).
- [ ] **A8** ruff 0 new, pyright 0, full gate suite green with pasted output.
- [ ] **A9 (EDIT)** SP0: no `ValueError` on `<|endoftext|>` in any message; the system prompt is
      encoded once across rounds while it is unchanged; a real compaction encodes only the
      strings it created; `get_token_breakdown` numbers equal an independent full count.
- [ ] **A10 (EDIT)** SP3 concurrency: two simultaneous requests to one origin never share a
      connection, and each receives its own body.
- [ ] **A11 (EDIT)** SP3 errors: a pooled ≥ 400 raises `urllib.error.HTTPError` with a readable
      body and causes no second request; a 3xx follows the urllib path.
- [ ] **A12 (EDIT)** SP4: the five sites parse identically under `CSafeLoader`, and fall back to
      `SafeLoader` when libyaml is absent (patch `yaml.CSafeLoader` away).

---

## 7. Edge Cases

| Case | Expected |
|---|---|
| `token_budget <= 0` | Existing guard (return before any read) — untouched, still first |
| Under budget but orphans exist (pathological) | Sweep strips them; cache invalidated; one re-encode paid; event reports the removal honestly. **EDIT (Claude Opus 5.5):** summary injection then runs exactly as today (`messages_removed > 0`); the single-path SP1 does not change this |
| `keep_first`/`protect_is_summary` interactions | Unchanged — ~~early exit happens before any layer runs;~~ full path identical (EDIT: there is no early exit) |
| Two runtimes sharing a conversation | Not a thing (per-session convs); cache fields are per-conversation as today |
| ~~Index cache and a file changed *inside* the tree~~ | ~~Root mtime is the invalidator …~~ EDIT: no index cache; every build walks the current tree |
| `.gitignore` edited mid-session (EDIT) | New pattern tuple → new compiled matcher on the next walk; the old entry is unused |
| Message text contains `<\|endoftext\|>` or another special-token literal (EDIT) | Counted as ordinary text by `encode_ordinary`; no exception (today: `ValueError` fails the turn, and every later turn while the message stays in history) |
| Conversation model switched to a different encoding (EDIT) | Memo key includes `encoding.name`; counts recomputed |
| Memo reaches 20,000 entries (EDIT) | Cleared; the next recount re-encodes once, same as today's cold cost |
| tiktoken missing | Fallback `chars//4` path — encode counting is moot; changes are still correct (no-ops) |
| Server closes the pooled connection (`Connection: close`) | Read/`close` marks it unconsumable → discarded, not returned to pool |
| SSE stream interrupted mid-body | Connection discarded on close (not fully consumed) — never reused mid-stream |
| SSE stream finished at `[DONE]`, terminator unread (EDIT) | Bounded drain on close (≤ 64 KB, 1 s); EOF → back to pool; otherwise discarded |
| Two sessions call the same origin at once (EDIT) | Each checks out its own connection; a second socket opens if the pool is empty |
| Pooled request gets 429/5xx (EDIT) | `HTTPError` raised from the pooled response; no fallback re-send; connection discarded |
| Pooled request gets 3xx (EDIT) | Discarded; urllib re-sends and follows the redirect as today |
| Connection idle > 30 s (EDIT) | Closed instead of reused |
| `DEVELCAKES_HTTP_KEEPALIVE=1` exported in a dev shell during tests (EDIT) | `tests/conftest.py` unsets it outside the keep-alive tests |
| Plain-http local gateway (`localhost:18790`) (EDIT) | Not eligible (https only); no TLS to save |
| Provider behind a proxy | Pool bypassed entirely (A7) |
| Pool grows unbounded | ~~Capped at 8 origins; eviction closes the dropped connection~~ EDIT: at most 8 idle connections in total; in-use connections are outside the pool |

### Registered follow-ups (NOT this spec)

- Flip `DEVELCAKES_HTTP_KEEPALIVE` default to ON after one live session of evidence.
  **EDIT (Claude Opus 5.5):** and only after A6, A10, and A11 are green. Expected saving per
  model request, measured from this machine: ~80 ms OpenRouter, ~138 ms Z.ai, ~175 ms MiniMax.
- ~~`load_providers()` mtime cache (~6 ms/call — measured, not felt; do it opportunistically).~~
  **EDIT:** superseded by SP4. The C loader gets the same call to 0.69 ms with nothing to invalidate.
- Provider prompt-cache headers for the resent system prompt (product decision, audit §6).
- **EDIT (Claude Opus 5.5):** once SP0 has shipped, consider deleting `_token_estimate_cache`
  and its manual clears (`context_strategy.py:258`, `:275`, `:438`; `conversation.py:193`,
  `:209`, `:220`; `ui/agent_runtime/session.py:85`). A memoized recount is cheap enough that the
  whole-conversation cache stops paying for its invalidation rules. Several tests pin the field,
  so this is its own change.

---

## 8. ARCHITECTURE.md Updates

One paragraph under the agent section: "Hot-path caches (SPEC-21): compaction keeps the token
estimate cache warm on no-op paths (fresh no-op CompactionEvent always recorded); the file index
is mtime-cached like file context; optional per-host HTTP keep-alive behind
DEVELCAKES_HTTP_KEEPALIVE (default off) with automatic fresh-connection fallback."

**EDIT (Claude Opus 5.5)** — use this text instead: "Hot-path costs (SPEC-21): `Conversation`
counts each string's tokens once (`_count_text`, keyed by encoding, length, and hash, using
`encode_ordinary`), so recounts after an append, stub, or pop cost lookups; compaction no longer
wipes the estimate cache speculatively. `.gitignore` patterns are compiled once into one
predicate shared by every project walker. YAML is parsed with libyaml when present. Optional
per-host HTTP keep-alive sits behind DEVELCAKES_HTTP_KEEPALIVE (default off): connections are
checked out per thread, drained before reuse, and errors map to urllib's exceptions."

---

## 9. Self-Audit (steel-framed Rule 9)

1. **Every sample traced?** Yes — compact() (126–338), the sweep (266–276), the wipes (146,
   258, 275, 438), `get_token_estimate` (310–339), `build_file_index` (472–600), the caller
   (674), `_FILE_CONTEXT_CACHE` (314) + `_project_root_mtime` usage (368),
   `urlopen_with_ssl_retry` (290–330), all six provider call sites, the telemetry consumer
   (runtime 1636, session.py 224, runtime 2991).
2. **All exception types?** Reader-side: `OSError` (index walk already guards); pool:
   `http.client` raises `HTTPException`/`OSError`/`ssl.SSLError` — all inside
   `_try_pooled`'s catch-all-to-None; the existing retry loop's exception surface is unchanged.
   **EDIT (Claude Opus 5.5):** the catch-all is replaced by §2.3 fixes 3, 4, and 6. Only stale
   reused sockets fall back silently, ≥ 400 raises `HTTPError`, and everything else takes
   urllib's `do_open` shape into the existing except clauses.
3. **Key structures verified?** Cache key tuple read from source (conversation.py:332);
   `CompactionEvent` field list read from source (context_strategy.py:29–45+);
   `Conversation.__init__` requires `agent_name` (probe-learned — the audit's sketch was wrong).
4. **End-to-end flow traced?** §3, all three paths.
5. **Honest corrections to the audit, found by probing:** (a) the no-op compact ends with a WARM
   cache on a synthetic no-op (the telemetry read re-warms it) — the real waste is that during
   live loops the intermediate reads re-encode after each wipe: 3 encodes where 1 suffices;
   (b) an early return that skips recording `_last_result` would hand the runtime a STALE event
   — the spec mandates the fresh no-op event; (c) measured numbers are 181/159 ms (index) and
   26.3 ms/encode on today's tree — same order as the audit's, substituted where they differ.
6. **EDIT (Claude Opus 5.5) — second verification pass, by probe.** (d) The original "Files NOT
   changed" list missed the second per-round encode: `get_token_breakdown` (`runtime.py:1732`,
   wired in the app) re-encodes everything without a cache. (e) A real compaction costs 39 full
   encodes and ~1.1 s, because every stub and pop clears the whole-conversation cache. SP1 alone
   leaves that untouched; SP0 fixes it. (f) The index cost is the `.gitignore` matcher (~90%),
   not line counts. An mtime cache keyed on the root directory would hide changes inside
   subdirectories from the model, so SP2 compiles the matcher instead. (g) tiktoken's `encode()`
   raises on special-token literals; that is a latent turn-killing bug on the same lines SP0
   touches. (h) SP1's early exit is not needed to reach one encode; the two-line version was
   probed with identical outputs. (i) SP3's catch-all fallback, shared single connection, and
   "return only if fully consumed" rule would have re-sent 429s, crossed responses between
   threads, and never reused a streamed connection; §2.3 fixes 1–6 replace them.
   (j) `_keepalive_enabled` read `os.environ` directly, against the `get_env` rule in
   `utils/config.py:44`.
   The audit's §2 also has an error: it says the post-trim read at `runtime.py:1687` is a full
   encode. It is a cache hit, because the telemetry read at `:303` re-warms the cache. The
   two wasted encodes are at `:383` and `:303`. The audit file was not edited.

---

## 9a. SP3 prototype proof (the riskiest change, validated before hand-off)

The 2.3 pooled fast path was prototyped against a live https host on 2026-10-09. Two runs:

**(1) Reuse works - one connection for two calls:**

    call1 fresh-connect: True | call2 fresh-connect: False (False = REUSED)
    connections opened: 1 | both bodies: True

**(2) Stale pooled socket recovers - no user-visible error:**

    stale socket raised: OSError | evicted: True
    fresh retry succeeded: True | total connections: 2

This validates both load-bearing requirements: the pool actually saves the TLS handshake, and a
server-closed socket is evicted so the caller's retry lands on a fresh connection - the exact
behavior the module's own SSL note (2026-08-21) demands. The wire-in is safe to implement; the
env gate (default OFF) is the only thing between this and production.

**EDIT (Claude Opus 5.5):** the proof is real but narrow. It shows reuse with complete reads on
one thread, and recovery from one stale socket. It does not cover the three cases that decide
whether SP3 helps or breaks in this app: a stream the consumer stops reading at `[DONE]` (the
path the UI always takes), two agent threads on one origin, and a non-2xx status. "The env gate
is the only thing between this and production" is therefore not yet true. §2.3 fixes 1–6 and
acceptance criteria A6, A10, and A11 come first. I measured the handshake it would save: DNS + TCP +
TLS at ~80 ms to `openrouter.ai`, ~138 ms to `api.z.ai`, ~175 ms to `api.minimax.io` (median of 4).
That is worth doing, once those cases are covered.
