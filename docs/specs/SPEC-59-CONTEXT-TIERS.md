# SPEC-59: Context tiers — offload early, trim in the middle, panic at the provider line

**Date:** 2026-10-30
**Author:** Micah
**Status:** Draft — for implementation
**Implements:** none (no proposal doc)
**Depends on:** SPEC-48 (tool-output store and `read_tool_output`), SPEC-WIRE-CONTEXT-PRESSURE (wire count is the trigger), SPEC-34 (the observation-mask sweep stays deleted)
**Target branch:** main

> Architecture: window math stays in `agent/runtime.py`. Body replacement and message deletion stay in `agent/context_strategy.py`. Disk writes stay in the runtime offload callback (`AgentRuntime._offload_tool_output` → `utils/tool_output_store.offload_tool_output`). The strategy still mutates only `conv.messages` and the token-estimate cache. `models/conversation.py` is not modified. The settings spin is not modified. This spec does not restore `mask_old_observations`, `select_maskable`, or the every-four-observations sweep deleted by SPEC-34.

---

DISCOVERY:

- Read `agent/runtime.py`: `_LANDING_FRACTION = 0.15` and `_LANDING_CAP = 24_000` at lines 312–313. `_compute_model_max` (line 1811) returns `provider.context_window` when it is an `int > 0`, else `max_tokens` when `> 0`, else `caller_default_max_tokens`, else `128_000`. No 200k cap. `_compute_compaction_threshold` (line 1850) returns `(soft_ceiling, hard_ceiling)` with `soft_ceiling = int(hard_ceiling * threshold)`, threshold from `LLMProviderConfig.compaction_threshold` when in `(0, 1]`, else `0.80`. `_compute_landing_goal` (line 1887) returns `0` for a non-int, a `bool`, or `<= 0`, else `min(int(hard_ceiling * 0.15), 24_000)`. `_compact_before_llm` (line 4334) counts pressure with `_wire_messages_and_count`, and when `pressure <= soft_ceiling` calls `compact(conv, max(soft_ceiling, conv.get_token_estimate()))` with no keyword arguments. Over the line, and only when `soft_ceiling > landing`, it calls `panic_prune(conv, protect_paths=protect)` then `compact(conv, landing, protect_paths=protect, preserve_current_turn=True)`. `_compile[session_key]` is a dict with `wire_from` (line 4197). `_offload_tool_output(self, conv, request)` (line 4406) returns `None` when `project_path` is missing, the resolved threshold is `<= 0`, `len(request.content) <= threshold`, or `offload_tool_output` raises. It catches `Exception`. `_resolve_offload_threshold` (line 4376) uses the provider value when `> 0`, else `AgentConfig.tool_output_offload_chars` (default `8192`), else `0`.
- Read `agent/context_strategy.py`: `OffloadRequest` is `tool_name: str`, `content: str` (line 72). `compact(conv, token_budget, *, keep_first=2, protect_is_summary=True, protect_paths=(), preserve_current_turn=False)` returns immediately when `token_budget <= 0` and records no event. Otherwise it always calls `prune_tool_outputs` then maybe the trim loop, and always assigns `last_result`. `prune_tool_outputs` returns `0` when `get_token_estimate() <= target_tokens`. It skips the `protect_turns` newest `TOOL_RESULT`s (`protect_turns < 0` is clamped to `0`), skips `_kept_from_heavy`, skips bodies whose content `startswith("[compacted —")` or `startswith("[masked —")` (the dash is U+2014), then assigns `_stub_tool_result`, sets the matching `tc.result = None`, sets `tokens_used = len(stub) // 4`, and calls `conv.invalidate_token_estimate()`. `_stub_tool_result` calls `self._offload(conv, OffloadRequest(tool_name, content))` and, when `meta.get("path")` is truthy, returns the offloaded stub; otherwise the lossy stub `[compacted — {tool_name} {n_chars}]`. `panic_prune(conv, protect_paths=())` returns `0` when `_last_user_index` is `None`. It stubs `TOOL_RESULT`s with index `< last` user index, skipping a non-empty path that is in `protect_paths`. `_kept_from_heavy` returns `True` for every index `>= last_user` when `preserve_current_turn` is true. `LLMSummarizeStrategy` does not override `compact`; it inherits it.
- Read `utils/tool_output_store.py`: `offload_tool_output(project_path, tool_name, content) -> dict | None`. Success dict keys are `path`, `bytes`, `lines`, `preview`. `OSError` during the write returns `None` after a best-effort unlink of the temp file. A store directory that realpaths outside the project returns `None`. The relative path shape is `.crabcakes/tool-outputs/<safe-name>-<8 hex>.txt`.
- Read `agent/tools.py`: `_read_tool_output` reads only that store, windowed with `LINE_WINDOW` (200). A tier stub that names `meta["path"]` is readable with the existing `read_tool_output` tool. No change.
- Read `models/conversation.py`: `MessageRole.TOOL_RESULT` is `"tool"`. `invalidate_token_estimate` sets `_token_estimate_cache = None` and increments `_token_gen`. `get_token_estimate` counts system prompt, task card, message content, and `str(tc.arguments)`. It does not count `tc.result`.
- Read `ui/agent_runtime/session.py`: `compact_conversation(..., protect_open_turn: bool = False)` sets `target_budget = max(4_000, hard_ceiling // 2)` and passes that to `force_llm_compact` or `force_compact`. Both end in `compact`. `ui/handlers/project_handler.py::cmd_compact` only documents that budget; it does not compute it.
- Read `agent/config.py`: `AgentConfig.tool_output_offload_chars` defaults to `8192`. `0` disables offload. A saved provider with `tool_output_offload_chars: 0` does not disable it, because `_resolve_offload_threshold` only overrides when the provider value is `> 0`.
- Read tests that lock the current trigger: `tests/test_wire_context_pressure.py::test_plain_under_ceiling_is_unchanged` and `tests/test_context_strategy_tier0.py::test_compact_under_threshold_uses_soft_ceiling` require `compact(conv, soft)` with `kwargs == {}` when the wire is under the ceiling and the estimate is `1_000`. `test_compiled_wire_under_ceiling_does_not_panic` requires `compact(conv, 10_000_000)` and no `panic_prune` when the transcript estimate is faked high but the compiled wire is small. `test_over_threshold_lands_on_the_goal` and `test_compiled_wire_over_ceiling_still_panics` require `panic_prune` then `compact(conv, _compute_landing_goal(hard), preserve_current_turn=True)`. `test_over_ceiling_stubs_previous_turn_and_keeps_the_current_turn` has three tool results and expects the previous-turn unpinned body to start with `[compacted —` while `now` and `hot` stay verbatim. `test_fat_max_tokens_is_uncapped_without_context_window` and `test_minimax_zero_max_tokens_stays_on_caller_table` expect the 24_000 cap.
- Architecture owner: trigger and tier lines — `AgentRuntime`. Stub text and which messages move — `DefaultContextStrategy`. Store — `utils/tool_output_store.py`, already correct.
- Existing pattern to copy: `_stub_tool_result` plus `invalidate_token_estimate` after each body write. The wire-pressure early return in `_compact_before_llm`. Do not copy the deleted sweep.

---

## 1. Overview

### Problem

On a 1,300,000-token window the only automatic trim is the provider line. GLM 5.3 sets that line at 0.50, so nothing runs until 650,000 tokens. The landing goal is then `min(15% of the window, 24_000)`, which is 24,000 tokens, about 2% of that window. The turn that is running is kept whole, so a long tool loop cannot shrink itself. Manual `/compact` aims at `hard_ceiling // 2`, the same 650,000, so at 40% it frees nothing.

The Supervisor transcript measured on 2026-10-30 was 527,977 tokens (40.6% of 1,300,000) with zero offload stubs. That size is real. It sits in the band this spec calls the middle tier, and today that band is a no-op.

SPEC-34 deleted the earlier mask sweep because it replaced tool bodies with lossy stubs every four observations, including shell output that could not be read back. This spec does not put that sweep back.

### Solution

Three lines on the real window, chosen from the wire count `_wire_messages_and_count` already returns. Fractions are code constants. There is no new setting.

| Line | Fraction of `hard_ceiling` | GLM 5.3 at 1,300,000 and threshold 0.50 | What runs |
|---|---:|---:|---|
| Early | 0.15 | 195,000 | Offload tool bodies over the resolved offload threshold. Keep the newest 6 tool results and any protected path. No message deletion. No lossy stub. |
| Middle | 0.25 | 325,000 | Same offload, plus `read_file` and `search_files` bodies over 500 characters. Then delete oldest messages down toward the early line. The current user message and everything after it stay. Still no lossy stub. |
| Panic | provider `compaction_threshold` | 650,000 | Offload as in the middle tier, then today's `panic_prune` and `compact` toward the early line. Lossy stubs are allowed only on this path, and only for results from before the latest user message. |

The early line is the landing goal. `_LANDING_CAP` is removed. 15% of 128,000 stays 19,200. 15% of 1,300,000 becomes 195,000.

Worked lines, `int(hard * fraction)`:

| Window | Threshold | Early | Middle | Panic (`soft_ceiling`) |
|---|---:|---:|---:|---:|
| 128,000 | 0.80 | 19,200 | 32,000 | 102,400 |
| 200,000 | 0.80 | 30,000 | 50,000 | 160,000 |
| 1,048,576 | 0.80 | 157,286 | 262,144 | 838,860 |
| 1,300,000 | 0.50 | 195,000 | 325,000 | 650,000 |

A line that is not strictly below the panic line does not get its own band. See §7.

Offload uses the store that already exists. The stub prefix stays `[compacted —` (U+2014) so `_body_live_in_transcript` and the idempotence checks keep matching. The offloaded form already tells the model to call `read_tool_output`. A body the store refuses is left in the transcript on the early and middle paths. It is not replaced with the one-line lossy stub. That is the difference from the sweep SPEC-34 removed.

### Scope

| In | Out |
|---|---|
| Tier selection in `_compact_before_llm` | Restoring the every-four-observations mask sweep |
| Uncapped 15% landing goal | A new settings control, or writing 0.15 into `compaction_threshold` |
| In-place offload of old tool bodies, including inside the current turn, except the newest 6 | Lossy-stubbing the current turn |
| Middle-tier message deletion only when `self._compile.get(session_key)` is `None` | Repairing `wire_from` after a panic deletion (pre-existing; panic stays as it is) |
| Manual compact target becomes the landing goal | Changing `LLMSummarizeStrategy._summary` |
| Tests listed in §6 | Dashboard warning levels 80 and 95 |
| ARCHITECTURE.md paragraphs that still say 24,000 and "under the soft ceiling, compact is a no-op" | System-prompt budget (§4.4b) |

### Architecture principles

- The wire count picks the tier. The transcript estimate is only the stop line inside a tier.
- A compile packet's `wire_from` is an absolute index. Early and middle tiers must not `pop` or `insert` while that dict is present.
- The strategy does not open files. The runtime callback does, and it already swallows `Exception`.
- Lossy stubs are a panic tool for previous turns. Early and middle never write one.
- Under the early line, the call stays `compact(conv, max(soft_ceiling, estimate))` with no keyword arguments. Two existing tests require `kwargs == {}`.

---

## 2. Changes by File

### 2.1 `agent/runtime.py`

#### Constants

Replace the landing pair at lines 311–313.

```python
# Early line and landing budget: 15% of the real window. Not a provider setting.
_LANDING_FRACTION = 0.15
# Middle line. Collapses into panic when it is not strictly below soft_ceiling.
_MIDDLE_FRACTION = 0.25
# Newest tool results left verbatim by an offload pass.
_TIER_KEEP_LAST = 6
# Middle-tier floor for read_file and search_files only. The resolved
# offload threshold still applies to every other tool.
_MIDDLE_MIN_CHARS = 500
_MIDDLE_TOOLS = ("read_file", "search_files")
```

Delete `_LANDING_CAP`. Do not reintroduce it under another name.

#### `_compute_landing_goal`

Same guards. Drop the cap. `bool` stays excluded because `bool` is a subclass of `int` and `True` would otherwise become `0`.

```python
def _compute_landing_goal(self, hard_ceiling: int) -> int:
    """15% of the real window. The early line, and where heavy compact lands.

    Not the provider threshold. Not capped at 24_000.
    """
    if not isinstance(hard_ceiling, int) or isinstance(hard_ceiling, bool):
        return 0
    if hard_ceiling <= 0:
        return 0
    return int(hard_ceiling * _LANDING_FRACTION)
```

`int(1_300_000 * 0.15)` is `195_000`. `int(128_000 * 0.15)` is `19_200`. `int(1_048_576 * 0.15)` is `157_286`.

#### `_compute_middle_line`

New method, same guards, `int(hard_ceiling * _MIDDLE_FRACTION)`. `hard_ceiling <= 0` returns `0`. This number is not stored on the provider.

#### `_offload_tool_output`

The resolved threshold stays the on/off switch. A per-request `min_chars` may only lower the size bar while the switch is on. It must not turn a disabled store back on.

Current order is: resolve threshold, return `None` when `threshold <= 0` or `len(content) <= threshold`, else write. Insert the override between those two checks:

```python
threshold = self._resolve_offload_threshold(conv)
if threshold <= 0:
    return None
requested = getattr(request, "min_chars", None)
if isinstance(requested, int) and not isinstance(requested, bool) and requested > 0:
    threshold = requested
if len(request.content) <= threshold:
    return None
```

`getattr` is required so an `OffloadRequest` built before this field existed still works. `True` must not pass the `int` test. The rest of the method, including `except Exception: return None`, stays. The store function is still `offload_tool_output(project_path, request.tool_name, request.content)`. Its return dict is returned unchanged. Callers that need the path read `meta["path"]`. A missing path is the strategy's problem, not this method's.

#### `_compact_before_llm`

Signature stays:

```python
def _compact_before_llm(self, session_key, conv, soft_ceiling, hard_ceiling, tools=None) -> None
```

Replace the body. `tools` is still forwarded to `_wire_messages_and_count`. One pressure sample decides the tier. Do not re-sample after offload and jump to another tier. The tool loop already recounts the wire for the meter after this method returns (`runtime.py` around the second `_wire_messages_and_count` call). That recount is what moves `usage_percent`. This method does not dispatch the breakdown.

```python
landing = self._compute_landing_goal(hard_ceiling)
middle = self._compute_middle_line(hard_ceiling)
_messages, pressure = self._wire_messages_and_count(
    session_key, conv, hard_ceiling, tools,
)
estimate = conv.get_token_estimate()
addon = pressure - estimate
if addon < 0:
    addon = 0

def _noop() -> None:
    # Keyword-free on purpose. test_compact_under_threshold_uses_soft_ceiling
    # asserts kwargs == {}.
    ceiling = max(soft_ceiling, estimate)
    self._context_strategy.compact(conv, ceiling)

# Provider line at or below 15%: keep today's two-way behavior.
if landing <= 0 or landing >= soft_ceiling:
    if pressure <= soft_ceiling:
        _noop()
        return
    if soft_ceiling > landing:
        protect = self._protect_paths(session_key, conv)
        self._context_strategy.panic_prune(conv, protect_paths=protect)
        self._context_strategy.compact(
            conv, landing, protect_paths=protect, preserve_current_turn=True,
        )
        return
    self._context_strategy.compact(conv, soft_ceiling)
    return

if middle >= soft_ceiling:
    middle = soft_ceiling

if pressure <= landing:
    _noop()
    return

protect = self._protect_paths(session_key, conv)
transcript_target = landing - addon
if transcript_target <= 0:
    transcript_target = 1
packet = self._compile.get(session_key)
can_delete = packet is None
# wire_from is the first on-wire message. Early and middle must not
# rewrite the off-wire audit log. Panic still walks the whole transcript.
min_index = 0
if isinstance(packet, dict):
    raw_from = packet.get("wire_from", 0)
    if isinstance(raw_from, int) and not isinstance(raw_from, bool) and raw_from > 0:
        min_index = raw_from

if pressure <= middle:
    self._context_strategy.offload_tool_results(
        conv,
        target_tokens=transcript_target,
        keep_last=_TIER_KEEP_LAST,
        protect_paths=protect,
        tool_names=None,
        min_chars=None,
        min_index=min_index,
    )
    _noop()
    return

if pressure <= soft_ceiling:
    self._context_strategy.offload_tool_results(
        conv,
        target_tokens=transcript_target,
        keep_last=_TIER_KEEP_LAST,
        protect_paths=protect,
        tool_names=_MIDDLE_TOOLS,
        min_chars=_MIDDLE_MIN_CHARS,
        min_index=min_index,
    )
    self._context_strategy.offload_tool_results(
        conv,
        target_tokens=transcript_target,
        keep_last=_TIER_KEEP_LAST,
        protect_paths=protect,
        tool_names=None,
        min_chars=None,
        min_index=min_index,
    )
    if can_delete:
        self._context_strategy.compact(
            conv,
            transcript_target,
            protect_paths=protect,
            preserve_current_turn=True,
            allow_lossy_stub=False,
        )
    else:
        _noop()
    return

# Panic. Offload first so a long current turn can shrink. panic_prune
# still only stubs results from before the latest user message.
self._context_strategy.offload_tool_results(
    conv,
    target_tokens=landing,
    keep_last=_TIER_KEEP_LAST,
    protect_paths=protect,
    tool_names=_MIDDLE_TOOLS,
    min_chars=_MIDDLE_MIN_CHARS,
)
self._context_strategy.offload_tool_results(
    conv,
    target_tokens=landing,
    keep_last=_TIER_KEEP_LAST,
    protect_paths=protect,
    tool_names=None,
    min_chars=None,
)
if soft_ceiling > landing:
    self._context_strategy.panic_prune(conv, protect_paths=protect)
    self._context_strategy.compact(
        conv, landing, protect_paths=protect, preserve_current_turn=True,
    )
    return
self._context_strategy.compact(conv, soft_ceiling)
```

`_messages` stays unused. That matches the current call. `estimate` is captured once, before offload, and `_noop` uses that captured value. After an early offload the real estimate is lower, and `compact` is still called with `max(soft_ceiling, old_estimate)`, which is above the new estimate, so `prune_tool_outputs` returns `0` at its existing `tokens_before <= target_tokens` check. Do not re-read the estimate inside `_noop`. The under-ceiling tests replace `get_token_estimate` with a lambda; a second read is the same number, but the early-band path must not depend on a second read seeing the post-offload total.

`addon` is the repo map plus tool schemas on the plain branch (`count = estimate + map + schema` in `_wire_messages_and_count`). Subtracting it from the landing goal makes the transcript stop low enough that the later wire recount is about 15%. When a test fakes `get_token_estimate` to `10_000_000`, `pressure` includes that fake, `pressure > soft_ceiling`, and the method takes the panic branch. Those tests never enter the middle band. Do not "fix" that by tiering on the transcript estimate. The wire count is the trigger, including a monkeypatched estimate, because the plain branch adds the estimate into the wire count.

Panic still passes `landing`, not `transcript_target`, to `compact`. `test_over_threshold_lands_on_the_goal` asserts `budget == rt._compute_landing_goal(hard)`.

The return value of `offload_tool_results` is ignored. The meter does not read it. Ignoring it is safe.

An offload-only pass (early band, or middle with a compile packet) then calls `_noop`, whose `compact` records `tokens_freed == 0` because it snapshots the estimate after the bodies were already replaced. `trimmed_this_turn` stays false. `usage_percent` still drops, because the breakdown's wire recount happens after this method returns. Do not stash a baseline on the strategy object. One `DefaultContextStrategy` is shared by every session on the runtime, and `last_result` is already racy across sessions.

#### What `_compile` forbids

`self._compile.get(session_key)` returns the packet dict or `None`. A missing key is `None`. `wire_from` is an absolute index into `conv.messages`; the on-wire tail is `messages[wire_from:]` (`tests/test_wire_context_pressure.py::_packet`). A `bool` is rejected because `bool` is an `int`. A missing or non-positive `wire_from` means `0`.

When a packet is present, early and middle pass that `min_index` into `offload_tool_results` and the middle tier calls `_noop()` instead of `compact(..., transcript_target, ...)`. Offload only assigns `msg.content` at indexes `>= min_index`. It does not `pop` or `insert`, so `wire_from` and `after` stay valid, and the off-wire audit log is not rewritten. Panic passes `min_index=0` (the default) because `panic_prune` already rewrites previous turns. That deletion can still move indexes. This spec does not repair them.

### 2.2 `agent/context_strategy.py`

#### `OffloadRequest`

Add one field with a default, so existing positional constructions keep working:

```python
@dataclass
class OffloadRequest:
    tool_name: str
    content: str
    min_chars: int | None = None
```

`None` means "use the resolved threshold". The strategy does not read the provider. The runtime callback does.

#### `_stub_tool_result`

Extend the signature. Default `allow_lossy=True` so `panic_prune` and `prune_tool_outputs` stay lossy when the store refuses.

```python
def _stub_tool_result(
    self, conv, msg, tool_name, *, min_chars: int | None = None, allow_lossy: bool = True,
) -> str | None:
    content = msg.content or ""
    n_chars = len(content)
    if self._offload is not None:
        meta = self._offload(
            conv,
            OffloadRequest(tool_name=tool_name, content=content, min_chars=min_chars),
        )
        if isinstance(meta, dict):
            path = meta.get("path")
            if isinstance(path, str) and path:
                return self._offloaded_stub(tool_name, n_chars, path)
    if not allow_lossy:
        return None
    return self._compacted_stub(tool_name, n_chars)
```

A non-dict `meta` is a refusal. An empty `path` is a refusal. `conv` is still passed into the callback and not otherwise used here. That matches today. The callback's `Exception` handler is what stops a disk error from reaching this method. This method does not add its own `try`. A callback that raises is a test double's bug.

The lossy string stays `f"[compacted \u2014 {tool_name} {n_chars}]"`. The offloaded string stays the existing `_offloaded_stub`. Do not change either prefix.

#### `prune_tool_outputs` and `compact`

Add `allow_lossy_stub: bool = True` as a keyword-only argument on both.

`compact` passes it through to `prune_tool_outputs`. When `allow_lossy_stub` is `False`, `compact` does not call `prune_tool_outputs` at all. The trim loop, the orphan sweep, and the summary injection run as they do today. Default `True` keeps every existing `compact()` caller byte-identical, including the `protect_turns=2` prune.

`prune_tool_outputs` passes `allow_lossy=allow_lossy_stub` into `_stub_tool_result`. When that returns `None`, `continue` without assigning `msg.content`, without clearing `tc.result`, and without invalidating. When it returns a string, the existing assign / `tc.result = None` / `tokens_used` / `invalidate_token_estimate` block stays.

`token_budget <= 0` on `compact` still returns before any of this and still records no `CompactionEvent`.

#### `offload_tool_results`

New method on `DefaultContextStrategy`. `LLMSummarizeStrategy` inherits it.

```python
def offload_tool_results(
    self,
    conv: Conversation,
    *,
    target_tokens: int,
    keep_last: int,
    protect_paths: tuple[str, ...] = (),
    tool_names: tuple[str, ...] | None = None,
    min_chars: int | None = None,
    min_index: int = 0,
) -> int:
```

Returns the number of bodies replaced. The runtime ignores that int.

Behavior:

1. If `target_tokens <= 0`, return `0`.
2. If `keep_last < 0`, treat it as `0`. If `min_index < 0`, treat it as `0`.
3. If `conv.get_token_estimate() <= target_tokens`, return `0`.
4. Collect indexes whose `role == MessageRole.TOOL_RESULT` and whose index is `>= min_index`, in ascending order. The last `keep_last` of those indexes are protected. `keep_last == 0` protects nothing. Fewer than `keep_last` results protects all of them, and the method returns `0`. Indexes below `min_index` are not candidates and do not count toward `keep_last`.
5. Walk the unprotected indexes from oldest to newest. At the start of each index, if `get_token_estimate() <= target_tokens`, stop and return the count so far.
6. Skip when `content` is not a `str`. Skip when `content.startswith("[compacted \u2014")` or `content.startswith("[masked \u2014")`. Same dash as `prune_tool_outputs`.
7. Resolve `(tool_name, path, _offset, parent)` with `_tool_result_meta(conv, idx)`. If `tool_names` is not `None` and `tool_name` is not in that tuple, skip. Comparison is exact. `"[unknown tool]"` does not match `_MIDDLE_TOOLS`.
8. If `path` is a non-empty `str` and `path in protect_paths`, skip. An empty path is not protected. Do not absolutize either side. This is the same rule as `panic_prune`.
9. Call `_stub_tool_result(..., min_chars=min_chars, allow_lossy=False)`. `None` means skip. Do not write a lossy stub.
10. On a string, assign `msg.content`, clear the matching parent `tc.result`, set `msg.tokens_used = len(stub) // 4`, call `conv.invalidate_token_estimate()`, and increment the count. Same cache rule as `prune_tool_outputs`: the cache key ignores body text, so a missed invalidate makes the stop check lie and the walk over-stub.

This method does not look at `_last_user_index`. The newest `keep_last` tool results are protected whether they sit in the current turn or an older one. A user message is never selected, because its role is not `TOOL_RESULT`. A current turn with 20 tool results can lose the oldest 14 bodies. The latest user text stays because it is not a tool result. `panic_prune` is a different function and still stubs previous-turn results that this method left alone, including ones inside the newest 6, when the runtime is on the panic path.

`protect_paths` comes from `AgentRuntime._protect_paths`. This method does not stat files.

#### What this method must not do

- Must not `pop` or `insert`.
- Must not set `self._last_result`.
- Must not call `panic_prune` or `compact`.
- Must not import `utils.tool_output_store`.

### 2.3 `ui/agent_runtime/session.py`

In `compact_conversation`, replace the budget line:

```python
_, hard_ceiling = rt._compute_compaction_threshold(conv)
landing = rt._compute_landing_goal(hard_ceiling)
target_budget = landing if landing > 0 else max(4_000, hard_ceiling // 2)
```

The `except` branch that sets `hard_ceiling = 128_000` stays. After that assignment, compute `landing` from the same `hard_ceiling` variable, so the fallback also lands at `int(128_000 * 0.15)` which is `19_200`, not `64_000`.

`force_llm_compact` and `force_compact` already pass `target_budget` into `compact`. They do not grow new parameters. Manual compact does not run `offload_tool_results` first. It uses `compact`, whose default `allow_lossy_stub=True` still offloads through `prune_tool_outputs` when the store accepts and lossy-stubs when it does not. `preserve_current_turn` stays `protect_open_turn`. A manual compact of a 528k transcript on a 1,300,000 window now aims at 195,000 instead of 650,000.

### 2.4 `ui/handlers/project_handler.py`

`cmd_compact` does not compute the budget. Update its docstring sentence that says `compact(conv, model_max // 2)` so it says the budget is `AgentRuntime._compute_landing_goal` (15% of the window). No logic change.

### 2.5 Tests

#### Update in place

`tests/test_context_strategy_tier0.py`

- `test_fat_max_tokens_is_uncapped_without_context_window`: delete the `min(..., 24_000) == 24_000` lines. Assert `_compute_landing_goal(hard) == 195_000` for `hard == 1_300_000`.
- `test_minimax_zero_max_tokens_stays_on_caller_table`: assert `_compute_landing_goal(hard) == 157_286`. Drop the `_LANDING_CAP` import if nothing else in the file uses it.
- `test_default_128k_threshold_and_landing_goal`: keep `19_200`.
- `test_landing_goal_rejects_bad_ceiling`: keep the `0` / `-5` / `True` cases.
- `test_compact_under_threshold_uses_soft_ceiling`: keep `budget == soft` and `kwargs == {}`.

`tests/test_wire_context_pressure.py` and `tests/test_panic_prune.py`: do not change the assertions. The panic tests compare against `_compute_landing_goal(hard)`, not the literal `24_000`. The three-result fixture in `test_over_ceiling_stubs_previous_turn_and_keeps_the_current_turn` is inside `keep_last=6`, so the new offload pass must not touch those bodies before `panic_prune` stubs `old`. If that test fails, the keep-last set is wrong. Do not special-case the test.

#### New file `tests/test_context_tiers.py`

No GTK. Build the runtime the way `tests/test_panic_prune.py` does (`_make_cfg`, `create_conversation`, `tmp_path`). Give the provider `context_window=10_000` and `compaction_threshold=0.80` so the lines are early `1_500`, middle `2_500`, panic `8_000`. A 1,300,000-token fixture is not required. Assert the tier by monkeypatching `offload_tool_results`, `panic_prune`, and `compact` where the test is about which function ran, and by letting the real strategy run where the test is about the body that remains.

`AgentConfig.tool_output_offload_chars` defaults to `8192`. Tests that need a real file use `tmp_path` as the project, which `create_conversation` already stores on the conversation. Tests that need offload disabled set `rt._config.tool_output_offload_chars = 0` before the call. Do not write a provider YAML.

Cases:

1. **Under the early line.** Wire pressure at or under `1_500`. `offload_tool_results` is not called. `panic_prune` is not called. `compact` is called once with `budget == soft` and no keyword arguments.
2. **Early band.** Pressure in `(1_500, 2_500]`. One tool result older than the newest 6, body length `9_000`, is replaced with a stub that `startswith("[compacted \u2014")` and contains `.crabcakes/tool-outputs/`. The newest 6 bodies are unchanged. `len(conv.messages)` is unchanged. A 100-character body is unchanged. `panic_prune` is not called. The following `compact` is the no-op form (`budget >=` the pre-offload estimate, no `preserve_current_turn`).
3. **Store disabled.** Same fixture as case 2 with `tool_output_offload_chars = 0`. The 9_000-character body is unchanged. It does not start with `[compacted —`.
4. **Middle band.** Pressure in `(2_500, 8_000]`. A previous-turn `read_file` body of `600` characters is offloaded. A previous-turn `exec_command` body of `600` characters is not offloaded by the 500-character rule. No message's content is the lossy one-line stub (the offloaded form contains `offloaded to`). With no compile packet, `compact` is called with `preserve_current_turn=True` and `allow_lossy_stub=False`. The latest user message is still present after a real `compact` to `transcript_target`.
5. **Compile packet blocks deletion.** Set `rt._compile[sk]` with `_packet` from `tests/test_wire_context_pressure.py`. Middle-band pressure. `compact` is the no-op call, not the `transcript_target` call. Message count is unchanged. A tool result at an index below `wire_from` is unchanged. A tool result at or after `wire_from`, older than the newest 6 on-wire results, and over the tier's size bar, is offloaded.
6. **Panic still panics.** Pressure over `8_000`. `panic_prune` is called. `compact` is called with `budget == rt._compute_landing_goal(hard)` and `preserve_current_turn=True`. A current-turn `read_file` body of `9_000` characters that is older than the newest 6 is offloaded. The latest user message remains. The newest tool result remains verbatim.
7. **Protected path.** A path returned by `_protect_paths` is not offloaded on the early path. An empty-path `exec_command` result over the threshold is offloaded.
8. **Idempotent.** A body that already `startswith("[compacted \u2014")` is not sent to the offload callback.
9. **Collapsed middle.** `compaction_threshold=0.20` on a `10_000` window makes `soft_ceiling == 2_000` and `middle == 2_500`. Pressure `1_800` takes the early offload, not the middle `compact(..., allow_lossy_stub=False)`. Pressure `2_100` takes `panic_prune`.
10. **Manual budget.** `compact_conversation` on a 128,000 window passes `19_200` into `force_compact` / `force_llm_compact`, not `64_000`. Monkeypatch those two methods and assert the budget argument.

Case 2's "pre-offload estimate" is whatever `get_token_estimate()` returned before `_compact_before_llm`. The test can record it. The no-op budget is `max(soft, that number)`.

### 2.6 `docs/ARCHITECTURE.md`

Two paragraphs are now false.

Around line 1867, replace the sentences that say under the soft ceiling `compact` is a no-op, and that over it `compact` aims at `min(15% of the hard ceiling, 24_000)`. State the three lines from §1, that early and middle offload through the SPEC-48 store and do not write a lossy stub, that message deletion in the middle tier is skipped while a compile packet exists, and that panic still calls `panic_prune` then `compact(landing, preserve_current_turn=True)`.

The SPEC-34 paragraph near line 1970 says the same 24,000 landing and "under that line, `compact(soft_ceiling)` does not stub." Replace that landing clause with the uncapped 15% goal, and say the under-ceiling path now offloads once wire pressure is over 15%, and trims messages once it is over 25%, provided those lines sit below `soft_ceiling`. Leave the pin paragraph (`_hot_pins`, first successful `read_file`, `drop_read_cache`) as it is. Pins still feed `_protect_paths`, and this spec still honors that tuple.

In §3.21p.5, add `offload_tool_results` to the `DefaultContextStrategy` API list. Note that `compact(..., allow_lossy_stub=False)` skips `prune_tool_outputs`.

Do not rewrite §4.4b. The system-prompt budget is a different 15%.

**Files NOT changed** (already correct, or deliberately left):

- `utils/tool_output_store.py` — `offload_tool_output(project_path, tool_name, content)` already returns the path dict or `None` on `OSError` and on a store that escapes the project.
- `agent/tools.py` — `read_tool_output` already windows the stored body.
- `models/conversation.py` — `invalidate_token_estimate` already bumps `_token_gen`.
- `agent/llm/jev_client.py` — `classify_mask_candidates` stays unused. Do not wire it back.
- `ui/handlers/agent_runtime_handler.py` — the 80% and 95% warning levels stay. They are not tier lines.
- `ui/views/settings_dialog.py` — the compaction spin stays the panic line.
- `agent/context.py` — system-prompt file context is not a tier.

---

## 3. Data Flow

Every tool-loop iteration, before the LLM call (`agent/runtime.py` inside `_run_loop`):

1. `_compute_compaction_threshold(conv)` → `(soft_ceiling, hard_ceiling)`.
2. `_compute_landing_goal(hard_ceiling)` → early line. `_compute_middle_line(hard_ceiling)` → middle line. If `middle >= soft_ceiling`, the middle band is empty. If `landing <= 0` or `landing >= soft_ceiling`, the method uses only the pre-SPEC-59 under/over split.
3. `_wire_messages_and_count(session_key, conv, hard_ceiling, tools)` → `pressure`. Plain branch: `get_token_estimate() + repo-map tokens + schema tokens`. Compiled branch: `_compile_token_count`.
4. Tier from `pressure` only:
   - `pressure <= landing` → `compact(conv, max(soft_ceiling, estimate))` with no keywords. Return.
   - `landing < pressure <= middle` → `offload_tool_results` (all tools, `min_chars=None`, `keep_last=6`, `protect_paths`) then the same no-op `compact`. Return.
   - `middle < pressure <= soft_ceiling` → two offload passes limited to indexes `>= wire_from` when a compile packet exists (middle tools at 500 characters, then all tools at the resolved threshold), then either `compact(conv, transcript_target, protect_paths=..., preserve_current_turn=True, allow_lossy_stub=False)` or the no-op when a packet exists. Return.
   - `pressure > soft_ceiling` → the same two offload passes with `min_index=0`, aimed at `landing`, then `panic_prune(conv, protect_paths=protect)`, then `compact(conv, landing, protect_paths=protect, preserve_current_turn=True)`.
5. The loop builds the wire again and dispatches `on_token_breakdown`. `usage_percent` is that second count divided by `hard_ceiling`.

`offload_tool_results` asks `_stub_tool_result` for a string. That calls `AgentRuntime._offload_tool_output`. The callback returns `None` without writing when the store is disabled or the body is at or under the size bar. Otherwise it calls `offload_tool_output`, which writes `.crabcakes/tool-outputs/<tool>-<hash>.txt` or returns `None` on `OSError`. The strategy writes the offloaded stub onto `msg.content` only when `meta["path"]` is a non-empty string. The model reads it back with `read_tool_output`, which windows 200 lines. The bytes are not pasted back in full.

`transcript_target = max(1, landing - max(0, pressure - estimate))`. `compact` refuses `token_budget <= 0`, which is why the floor is `1` and not `0`.

Manual path: `/compact` → `ProjectHandler.cmd_compact` → `AgentRuntimeHandler.compact_conversation` → `session.compact_conversation` → `force_compact` or `force_llm_compact` → `compact(conv, landing)`. It does not consult wire pressure and does not call `offload_tool_results`.

---

## 4. File Change Summary

| File | Change | Risk |
|---|---|---|
| `agent/runtime.py` | Delete `_LANDING_CAP`. Add middle constants. Uncap landing. Tier `_compact_before_llm`. Honor `OffloadRequest.min_chars` only when offload is already on. | High. The trigger tests are exact about call shape. |
| `agent/context_strategy.py` | `min_chars` on `OffloadRequest`. `allow_lossy` on the stub helper. `allow_lossy_stub` on `prune_tool_outputs` and `compact`. New `offload_tool_results`. | High. A lossy stub on the early path is a spec failure. |
| `ui/agent_runtime/session.py` | Manual budget is the landing goal. | Medium. Changes what `/compact` aims at. |
| `ui/handlers/project_handler.py` | Docstring only. | Low. |
| `tests/test_context_strategy_tier0.py` | Landing numbers for 1,300,000 and 1,048,576. | Low. |
| `tests/test_context_tiers.py` | New. | Low. |
| `docs/ARCHITECTURE.md` | Replace the 24,000 / no-op-under-ceiling paragraphs. | Low. |

---

## 5. Implementation Order

1. Landing goal and constants in `agent/runtime.py`. Update the two tier0 assertions. Run `tests/test_context_strategy_tier0.py`. `test_default_128k_threshold_and_landing_goal` must still show `19_200`. The 1,300,000 case must show `195_000`.
2. `OffloadRequest.min_chars` and the callback change in `_offload_tool_output`. A disabled store (`tool_output_offload_chars == 0`) still returns `None` when `min_chars` is `500`.
3. `_stub_tool_result`, `allow_lossy_stub`, and `offload_tool_results`. Unit-test the method on a `DefaultContextStrategy(offload=...)` without the runtime: lossy stub is not written when `allow_lossy=False` and the callback returns `None`.
4. `_compact_before_llm`. Run `tests/test_wire_context_pressure.py` and `tests/test_panic_prune.py` before adding the new file. Those assertions stay green.
5. `compact_conversation` budget and the `cmd_compact` docstring.
6. `tests/test_context_tiers.py`.
7. ARCHITECTURE.md.

After step 4, grep the touched Python files for `_LANDING_CAP`. The only legal hits are comments that say it was removed, and there should be none of those either. Zero matches.

---

## 6. Acceptance Criteria

- [ ] `_compute_landing_goal(1_300_000) == 195_000`, `_compute_landing_goal(128_000) == 19_200`, `_compute_landing_goal(1_048_576) == 157_286`, and `0`, `-5`, and `True` still return `0`.
- [ ] Wire pressure at or under the early line calls `compact` once with no keyword arguments and does not call `offload_tool_results` or `panic_prune`.
- [ ] Wire pressure between the early and middle lines offloads bodies over the resolved threshold, leaves the newest 6 tool results and short bodies in place, does not change `len(messages)`, and does not call `panic_prune`.
- [ ] A refused offload (disabled store, body at or under the bar, `OSError`, non-dict meta, empty path) leaves the original body in place on the early and middle paths.
- [ ] Wire pressure between the middle line and `soft_ceiling` offloads `read_file` and `search_files` over 500 characters, then compacts toward `transcript_target` with `preserve_current_turn=True` and `allow_lossy_stub=False`, unless a compile packet is present, in which case it does not pop or insert.
- [ ] Wire pressure over `soft_ceiling` still calls `panic_prune` and `compact(conv, landing, protect_paths=..., preserve_current_turn=True)`.
- [ ] The current turn's newest tool results and its user message survive every tier. Older tool results in that same turn are offloaded when they match the tier's size rule. They are never replaced with the lossy one-line stub.
- [ ] A protected path from `_protect_paths` is not offloaded. `exec_command`, whose path is empty, can be offloaded.
- [ ] `/compact` aims at the landing goal. On a 128,000 window that is `19_200`, not `64_000`.
- [ ] `tests/test_wire_context_pressure.py`, `tests/test_panic_prune.py`, `tests/test_context_strategy_tier0.py`, and `tests/test_context_tiers.py` pass.
- [ ] ARCHITECTURE.md no longer says the automatic landing goal is `min(15%, 24_000)` or that under the soft ceiling compact does not stub.

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| `landing <= 0` or `landing >= soft_ceiling` (threshold at or below 0.15) | Today's under/over split only. No early offload under the panic line. The settings spin cannot set this (its floor is 0.50). YAML can. |
| Middle line `>= soft_ceiling` (threshold at or below 0.25) | The `(middle, soft]` band does not exist. Pressure up to `soft_ceiling` uses the early offload only. Over `soft_ceiling` is panic. |
| `pressure == landing` or `pressure == middle` or `pressure == soft_ceiling` | The lower tier. Comparisons are `<=`. |
| Compile packet present, early or middle band | Offload only at indexes `>= wire_from`, then keyword-free `compact` at `max(soft, pre-offload estimate)`. Off-wire bodies and `wire_from` stay. |
| `wire_from` missing, not an `int`, or a `bool` | Treated as `0`. |
| Compile packet present, panic | Offload in place, then existing `panic_prune` + `compact(landing, preserve_current_turn=True)`. That compact can still delete messages below `wire_from`. Pre-existing. Do not add index repair in this spec. |
| Fewer than 6 tool results | Offload replaces nothing. Panic can still stub previous-turn results. |
| Body already starts with `[compacted —` or `[masked —` | Skipped. The dash is U+2014. An ASCII hyphen does not match. |
| `tool_output_offload_chars == 0` and provider value `0` | Store stays off, including when `min_chars` is 500. Early and middle leave bodies. Panic may still lossy-stub previous turns. A single turn can still grow to the window. |
| Provider `tool_output_offload_chars > 0` | That value is the early-tier size bar, not 8192. |
| No `project_path` | Callback returns `None`. Early and middle leave the body. |
| Store realpath escapes the project, or the write raises `OSError` | `offload_tool_output` returns `None`. Early and middle leave the body. |
| `OffloadRequest` without `min_chars` | `getattr(..., None)`. Resolved threshold applies. |
| `min_chars=True` | Ignored. `bool` is excluded. |
| Protected path, including a pinned `read_file` | Not offloaded. `panic_prune` also skips it. A pin can hold the meter up. This spec does not unpin. |
| `exec_command` / empty path | Not path-protected. Eligible for offload. The bytes stay in the store. |
| Current turn has the only copies of a huge search | Results older than the newest 6 are offloaded once the matching tier runs. The newest 6 stay verbatim. The user message stays. |
| `token_budget <= 0` passed to `compact` | Returns immediately, no `CompactionEvent`. The runtime never passes `0`. `transcript_target` floors at `1`. |
| Fake `get_token_estimate` of `10_000_000` | Pressure is over the panic line because the plain wire count includes that estimate. Panic tests stay on the panic path. |
| Fake `get_token_estimate` of `1_000` and a tiny transcript | Pressure is under the early line. `compact(soft)` with no keywords. |
| Manual compact while a turn is open | `preserve_current_turn` follows `protect_open_turn`, as today. Target is the landing goal, not half the window. |
| Summary inserted by middle-tier `compact` | Existing `_fit_summary` rule: skip the insert if it would put the estimate back over `transcript_target`. |
| Two sessions compacting on one runtime | Do not store the pre-offload estimate on `DefaultContextStrategy`. `_noop` closes over the local `estimate`. |

---

## 8. ARCHITECTURE.md Updates Required

- The context-discovery paragraph that describes `_compact_before_llm` (about line 1867).
- The SPEC-34 paragraph (about line 1970): landing goal and the under-ceiling sentence.
- §3.21p.5 public API: `offload_tool_results`, and `compact(..., allow_lossy_stub=False)`.

No environment variable. No settings key. No new tool.
