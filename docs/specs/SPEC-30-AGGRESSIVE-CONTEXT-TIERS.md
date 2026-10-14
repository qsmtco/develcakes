# SPEC-30: Aggressive context tiers — landing goal, provider trigger, batched JEV

**Date:** 2026-10-14
**Author:** Supervisor (PM direction 2026-10-14, revised the same day)
**Status:** Draft — Stage 1 for implementation; Stage 2 after Stage 1 lands; Stage 3 interface pinned, delivery gated
**Implements:** PM direction 2026-10-14 — aggressive context, with the later decision that the provider compaction threshold and the heavy prune are one trigger, and the shrink-to size is a separate small goal.
**Depends on:** `DefaultContextStrategy.mask_old_observations` and `prune_tool_outputs` (`agent/context_strategy.py`), the compaction trigger in `agent/runtime.py`, and the decision-model client (`agent/llm/jev_client.py`). The hot-path pin does not use the facts board.
**Target branch:** main

> **Architecture compliance:** Window math stays in `agent/runtime.py` (`_compute_model_max`, `_compute_compaction_threshold`, and the new `_compute_landing_goal`). Masking stays in `agent/context_strategy.py::DefaultContextStrategy`; the strategy protocol is extended, not replaced. The decision-model wire format stays in `agent/llm/jev_client.py::post_decision`. Hot-path pins live on the runtime for the conversation, next to `_read_fingerprints`, and are not written to the facts board. The provider the runtime actually sees is `agent.config.LLMProviderConfig`, copied from `models.providers.ProviderConfig` by `agent/config.py::_to_llm_provider`. The context meter already reads `usage_percent` from `get_token_breakdown`. `agent/llm/convert.py` is not modified. The settings spin is not modified. `_collect_from_form` copies `context_window` off the loaded provider so a Save does not wipe it.

**Revision (PM, 2026-10-14):** Two numbers, two jobs.

1. **Trigger.** The per-provider `compaction_threshold` (settings spin, default `0.80`) is when the heavy compact runs. The hardcoded 85% panic line is dropped. `DEFAULT_THRESHOLD` stays `0.80`.
2. **Landing goal.** After that heavy compact, the conversation is cut toward `min(int(hard_ceiling * 0.15), 24_000)` tokens. The same goal is when ordinary masking starts. It is not stored in `compaction_threshold`, and it is not `DEFAULT_THRESHOLD`.

`get_token_estimate()` counts `system_prompt` + `task_card` + message content + tool-call arguments (`models/conversation.py`). The goal is compared to that total. The system prompt is a separate budget (`docs/ARCHITECTURE.md` §4.4b, 15% of the window with a 16k-token cap) and is not shrunk by this spec.

---

## DISCOVERY

- Read `agent/runtime.py`: `_compute_model_max` (line 1537) resolves (1) provider `max_tokens` when `> 0`, (2) `caller_default_max_tokens(provider.caller)` (MiniMax = 1_048_576, Anthropic = 200_000), (3) `128_000`. Step 1 is the misconfiguration vector. The caller table must stay uncapped.
- Read `agent/runtime.py`: `_compute_compaction_threshold` (line 1577) returns `(soft_ceiling, hard_ceiling)`. The fraction is the provider's `compaction_threshold` when set and in `(0, 1]`, else `DEFAULT_THRESHOLD = 0.80`. `soft_ceiling = int(hard_ceiling * threshold)`. Fallback math in the docstring is `(102_400, 128_000)`. `tests/test_runtime_compaction.py::TestCompactionThreshold` locks both the `0.80` default (`soft == int(128_000 * 0.80)`) and a per-provider `0.90`. Those tests construct `agent.config.LLMProviderConfig`, which is the object the runtime reads.
- Read `agent/runtime.py` tool loop (line 1816): every four tool results, `mask_old_observations(conv, keep_last=3)` runs with no budget check. Then every iteration calls `compact(conv, soft_ceiling)` (line 1834). `compact` is a no-op while `get_token_estimate() <= soft_ceiling`. Over the line, `prune_tool_outputs` stubs until the estimate is back under that same line (`protect_turns=2`), then the trim loop deletes messages (`keep_first=2`, `tail_preserve=4`) and may inject a summary. One fraction is both the trigger and the target. This spec splits those two jobs.
- Read `agent/runtime.py`: `_obs_since_mask` is initialized at line 712, popped at the start of every user message (line 1768) and in `_cleanup_tool_history` (line 3687), incremented only inside `_count_tool_observation` (line 3594). The path and tool name are not available there. They are available at the successful-result site (line 2405), after `read_file` dedup and before `add_tool_result`. Denied `exec_command` / sensitive-write paths (lines 2308 and 2331) also increment the observation counter; they must not count as reads or edits.
- Read `agent/context_strategy.py`: `compact(conv, token_budget, *, keep_first=2, protect_is_summary=True)` returns immediately when `token_budget <= 0` and records no `CompactionEvent`. Otherwise it always writes `last_result`, with `soft_ceiling=token_budget` (the budget passed in, not the provider fraction). `prune_tool_outputs` returns `0` when `get_token_estimate() <= target_tokens`. It does not know about paths. `_select_prune_candidate(conv, keep_first, tail_preserve, protect_is_summary)` (line 674) only skips `is_summary` messages and the `keep_first` / `tail_preserve` ends. It returns `None` when that region is empty, and the trim loop breaks. `mask_old_observations(conv, keep_last=3)` is unconditional. The mask string is `"[masked — {tool_name} {path} offset={offset} ({n} chars removed)]"`. Already-masked messages are detected by the `"[masked —"` / `"[compacted —"` prefixes. `MessageRole.USER` is `"user"` (`models/conversation.py`). There is no turn id on a message. The current turn is the messages after the last `USER` message.
- Read `agent/config.py`: `AgentConfig.providers` holds `LLMProviderConfig`, not `models.providers.ProviderConfig`. `_to_llm_provider` (line 144) copies a fixed field list. A field added only on `ProviderConfig` never reaches `_compute_model_max`. `LLMProviderConfig.compaction_threshold` defaults to `0.80`.
- Read `utils/providers_store.py`: `_from_dict` sets `compaction_threshold=d.get("compaction_threshold", 0.80)`. Every loaded provider has a threshold in `(0, 1]` unless the YAML value is outside that range. Changing `DEFAULT_THRESHOLD` does not change a saved provider.
- Read `ui/views/settings_dialog.py`: the spin (line 137) is `Gtk.SpinButton.new_with_range(0.50, 0.95, 0.05)`, seeded from `compaction_threshold or 0.80`. It cannot store `0.15`. The spin stays the heavy-compact trigger and gains no new control. `_collect_from_form` (line 287) builds a new `ProviderConfig` from an explicit keyword list. Fields it does not pass (`caller`, `default_max_tokens`, `last_error`, and others) are copied off `self._provider`. A field that is not copied is replaced by the dataclass default on Save. `context_window` has to be copied the same way.
- Read `ui/agent_runtime/session.py` (line 195): manual `compact_conversation` unpacks `_, hard_ceiling = rt._compute_compaction_threshold(conv)` and calls `force_compact(conv, max(4_000, hard_ceiling // 2))`. That entry has its own budget. It is not the tool-loop path and is not changed. The `(soft, hard)` return contract stays a 2-tuple so this unpack keeps working.
- Read `agent/llm/jev_client.py`: `post_decision(state, questions) -> tuple[dict | None, str]` never raises. Empty `default_model` returns before any socket. `classify_unknown` validates `type == "choice"`, choice membership, and `confidence >= _DECISION_MIN_CONFIDENCE` (`0.6`), and returns `None` on failure.
- Read `agent/runtime.py::_dedup_read_output` (line 3645): a read fingerprint is `(st.st_mtime_ns, st.st_size)` from `os.stat(os.path.join(project_path, path))`. `OSError` is logged and the read is not fingerprinted. An empty `project_path` skips the stat. The dict is `_read_fingerprints`, cleared at the start of every user message (line 1767) and in `drop_read_cache` (line 3585). That dict is per turn. A session pin must not be stored in it and must not be popped at line 1767.
- Read `drop_read_cache` (line 3585): `clear_conversation` in `ui/agent_runtime/session.py` (line 88) calls it after emptying `conv.messages`. That is the conversation reset. `_cleanup_tool_history` (line 3682) runs on `FAILED` / `CANCELLED` and on `cancel()`; it resets the stuck detector, and it is not the pin lifetime.
- Read `utils/project_awareness.py` and `utils/git_ops.py`: `load_live_facts` drops a fact whose path is dirty against `get_head_sha`. A file with uncommitted edits cannot stay on the board. This spec does not call `record_fact` for pins.
- Read `models/conversation.py`: `conv.task_card` is a truncated progress string (max 2000 chars, written by `_note_task`). It is not a path list. Do not scrape paths out of it.
- Read `docs/ARCHITECTURE.md` §4.4b and §3.21p.5: system-prompt file context is capped separately. Compaction's public API is `compact` + `prune_tool_outputs`. This spec extends that section; it does not re-own SPEC-23 or SPEC-24.
- Measured against this tree: `max_tokens: 1_300_000` makes `_compute_model_max` return ~1.04M. The ceiling cap has to land before either fraction means anything. `tests/test_runtime_compaction.py::test_custom_threshold_per_provider` today expects `hard == 1_048_576` for `max_tokens=1_048_576` on a minimax caller. Stage 1a changes that case to the cap unless `context_window` is set. The test is updated in Stage 1a, not left red.

---

## 1. Overview

### Problem

The tool loop holds the conversation at the provider's compaction threshold. The default is 80% of the window. Once the chat crosses that line, compaction trims it back to the same line, and every later turn is billed at that size.

A second problem sits underneath: a provider `max_tokens` of `1_300_000` is a misread output cap. Fractions of that number are fractions of a fake window.

Masking is also path-blind. `mask_old_observations` stubs every old tool result, the agent re-reads the file, and the mask causes the churn it was meant to stop.

### Goal

Two numbers:

| Number | What it is | Where it lives |
|---|---|---|
| Provider threshold | When the heavy compact runs | Existing `compaction_threshold`. Per provider. Settings spin unchanged (`0.50`–`0.95`, default `0.80`). |
| Landing goal | How small the chat should get, and when ordinary masking starts | `min(int(hard_ceiling * 0.15), 24_000)`. Code constants. Not a setting. |

- **Ordinary masking (Tiers 1–2)** runs on the existing four-observation cadence, and only while `landing_goal < estimate <= soft_ceiling`. Tier 1 keeps the last 3 tool results, protected paths, results under 500 characters, and anything already masked. Tier 2 is one batched decision-model call for the leftovers: keep / mask / summarize, `confidence >= 0.6`, fail-open per candidate.
- **Heavy compact (Tier 3)** runs when `estimate > soft_ceiling` and `soft_ceiling > landing_goal`. It stubs tool results from before the latest user message, keeps that turn and protected paths, then calls `compact` toward the landing goal with those same keeps. No judgment. The 85% line is gone.
- **Hot-path pin:** the third successful `read_file` of a path stores that path with its current modification time and size, in memory, for the rest of the conversation. Masking keeps the path while those two still match, including when the file was already dirty. A later change drops the pin. Nothing is written to the facts board.

Below the landing goal, the mask cycle is a no-op and `compact(conv, soft_ceiling)` is the existing no-op.

### Non-goals

- Not a replacement for `mask_old_observations`, `prune_tool_outputs`, or `compact`.
- Not a new settings control, and not a change to the spin range. `0.15` is not stuffed into `compaction_threshold`.
- Not a change to manual `compact_conversation` (`ui/agent_runtime/session.py`), which keeps its own `hard_ceiling // 2` budget.
- Not a shrink of the system prompt. §4.4b already budgets that.
- Not a facts-board row. Pins are conversation memory. `record_fact` is not called.

---

## 2. Stage 1 — Real ceiling, landing goal, rules, pins

### 2.1 Fix the denominator (`agent/runtime.py`, `models/providers.py`, `agent/config.py`, `utils/providers_store.py`)

`_compute_model_max` resolution order becomes:

1. `provider_cfg.context_window` when set and `> 0` — uncapped. This is how a real window past 200k opts in.
2. `provider_cfg.max_tokens` when set and `> 0` — capped: `min(int(max_tokens), _MAX_SANE_WINDOW)` with `_MAX_SANE_WINDOW = 200_000`.
3. `caller_default_max_tokens(provider.caller)` — uncapped. The BUG #3 caller table stays intact.
4. `128_000`.

The runtime reads `LLMProviderConfig`. Add `context_window: int = 0` to both `models/providers.py::ProviderConfig` and `agent/config.py::LLMProviderConfig`. This is not `TestResult.context_window` in `utils/provider_test.py`. That probe value is what Test Connection writes into the `max_tokens` spin. The new field is a persisted override the operator sets in YAML. `utils/providers_store.py::_from_dict` parses optional `context_window` (int, default `0`; a non-int is `0`). `agent/config.py::_to_llm_provider` copies it. `ui/views/settings_dialog.py::_collect_from_form` adds one keyword, copied like `default_max_tokens`:

```python
context_window=getattr(existing, "context_window", 0) if existing else 0,
```

No new spin. Missing the dataclass, the store parse, the runtime copy, or this keyword drops the opt-in. A Save without the keyword writes `0`.

`_compute_compaction_threshold` is not given a new return value. `DEFAULT_THRESHOLD` stays `0.80`. `soft_ceiling` stays `int(hard_ceiling * threshold)` and stays the heavy-compact trigger. Existing `TestCompactionThreshold.test_soft_ceiling_is_80_percent` and `test_fallback_when_no_provider` keep passing, because `128_000` is under the cap.

`test_custom_threshold_per_provider` uses `max_tokens=1_048_576` and expects `hard == 1_048_576`. After the cap, that case is `hard == 200_000` and `soft == int(200_000 * 0.90)`. A minimax provider that wants `1_048_576` sets `context_window` (or leaves `max_tokens` at `0` and uses the caller table). Update the test in this stage.

### 2.2 Landing goal (`agent/runtime.py`)

Add module constants next to the other context constants:

```python
_LANDING_FRACTION = 0.15
_LANDING_CAP = 24_000
```

Add `AgentRuntime._compute_landing_goal(self, hard_ceiling: int) -> int`:

```python
def _compute_landing_goal(self, hard_ceiling: int) -> int:
    if hard_ceiling <= 0:
        return 0
    return min(int(hard_ceiling * _LANDING_FRACTION), _LANDING_CAP)
```

`hard_ceiling <= 0` does not occur on the fallback path (`128_000`). The `<= 0` guard matches `compact`'s existing `token_budget <= 0` return. Do not put this number in `_compute_compaction_threshold`. Callers that unpack `(soft, hard)` stay valid.

Worked values, after the §2.1 ceiling:

| Window | Landing goal | Default trigger (`0.80`) |
|---|---|---|
| `128_000` | `19_200` | `102_400` |
| `200_000` (capped fat `max_tokens`) | `24_000` | `160_000` |
| `1_048_576` (`context_window`, or caller table with `max_tokens == 0`) | `24_000` | `838_860` |

On a 1M window the early mask still starts at 24k. The heavy compact waits for the provider's own line, then cuts to 24k. That is why the two numbers are separate.

### 2.3 Tier 1 rules (`agent/context_strategy.py`)

Add `DefaultContextStrategy.select_maskable(self, conv, keep_last: int = 3, protect_paths: tuple[str, ...] = ()) -> list[int]`.

A `TOOL_RESULT` is kept when any of these hold:

- It is one of the `keep_last` most recent `TOOL_RESULT`s.
- The `path` argument recovered from the parent assistant tool call is in `protect_paths`. Compare the argument string as stored. Do not absolutize either side.
- `len(msg.content) < 500`.
- Content already starts with `"[masked —"` or `"[compacted —"`.

Everything else is maskable. `mask_old_observations(self, conv, keep_last: int = 3, protect_paths: tuple[str, ...] = ()) -> int` gains the kwarg and masks `select_maskable`'s indices. Mask string, `tc.result = None`, and cache invalidation stay as they are. Existing callers that omit `protect_paths` keep today's behavior except that results under 500 characters survive.

### 2.4 What is protected

`protect_paths` is the union of:

- `self._edited_paths[session_key]` — paths from successful `edit_file` / `write_file` in this conversation.
- Paths in `self._hot_pins[session_key]` whose stored `(st_mtime_ns, st_size)` still matches a fresh `os.stat`.

Do not parse `conv.task_card`. It is a 2000-character progress string, and the tail is dropped on overflow. Do not read the facts board.

New runtime fields, initialized next to `_read_fingerprints` (line 709):

- `self._read_counts: dict[str, dict[str, int]]`
- `self._hot_pins: dict[str, dict[str, tuple[int, int]]]` — session, then path, then `(st_mtime_ns, st_size)`
- `self._edited_paths: dict[str, set[str]]`

They last for the conversation. Do not pop them at line 1767. That line clears `_read_fingerprints` because those fingerprints are per turn. Pop all three inside `drop_read_cache` (line 3585), under `self._tool_history_lock`, the same lock `_dedup_read_output` holds. `clear_conversation` already calls `drop_read_cache`.

Hook reads and edits at the successful tool-result site (line 2405), where `tool_name` and `args` exist. Not inside `_count_tool_observation`. Not on the denial `continue` paths (lines 2308 and 2331).

- Successful `edit_file` or `write_file`: add `args["path"]` to `_edited_paths[session]` when it is a non-empty `str`.
- Successful `read_file`: §2.5.

### 2.5 Hot-path pin

A pin is the path plus the file's modification time and size at the read that created it. Dirty files pin the same way as clean ones. Nothing is written to `.crabcakes/facts.json`.

On a successful `read_file`, take `path` from `args.get("path")`. Skip the pin update when `path` is not a non-empty `str`, or `conv.project_path` is empty. Stat `os.path.join(conv.project_path, path)`, the same call `_dedup_read_output` uses. `OSError` means this read does not count and does not pin. Hold `self._tool_history_lock` around the dict updates.

The same path at different offsets is one path. The fingerprint is the whole file, not a window.

- No pin stored, and the stat succeeded: increment `_read_counts[session][path]`. When the count reaches 3, store `_hot_pins[session][path] = (st.st_mtime_ns, st.st_size)`. Later reads do not overwrite that pair.
- Pin stored, and the new stat matches: leave the pin and the count alone.
- Pin stored, and the new stat differs: drop the pin and set the count to 1. This read is the first look at the new bytes. Three reads of the new bytes can pin again.

Building `protect_paths` stats each pinned path again. A match keeps the path. A mismatch or `OSError` drops the pin and sets that path's count to 0, so the old reads can be masked and three later reads can pin the new file. Edited paths are included with no stat. Compare path strings as the tool stored them. Do not absolutize.

### 2.6 Mask-cycle gate

On `n_obs >= 4`, always reset the counter to `0` (including when the body no-ops). Otherwise the counter stays `>= 4` and every later iteration re-enters.

```text
soft_ceiling, hard_ceiling = _compute_compaction_threshold(conv)
landing_goal = _compute_landing_goal(hard_ceiling)
estimate = conv.get_token_estimate()
if landing_goal < estimate <= soft_ceiling:
    mask via select_maskable(protect_paths=...)
    # Stage 2 fills the JEV step here
# estimate > soft_ceiling: do not mask here; the heavy path is stricter
```

Re-read `get_token_estimate()` after this block before choosing the compact budget. Masking invalidates the estimate cache.

While `estimate <= soft_ceiling`, the existing call stays `compact(conv, soft_ceiling)`. Under the provider line that call stubs nothing and deletes nothing. Do not pass `landing_goal` on this path. Passing it would pull the chat down to 24k on the first turn that crossed the goal, and the provider threshold would never be reached.

### Stage 1 acceptance

- `max_tokens: 1_300_000`, no `context_window`, caller not in the trusted table → `hard_ceiling == 200_000`, `soft_ceiling == int(200_000 * 0.80) == 160_000`, landing goal `24_000`.
- `context_window: 1_000_000` → `hard_ceiling == 1_000_000`. `max_tokens: 1_300_000` beside it does not win.
- `max_tokens: 0`, caller `minimax` → `hard_ceiling == 1_048_576`. Landing goal `24_000`. Trigger stays `int(1_048_576 * threshold)`.
- Default provider, `max_tokens: 128_000`, threshold `0.80` → `(soft, hard) == (102_400, 128_000)`. Landing goal `19_200`. `test_soft_ceiling_is_80_percent` still passes.
- `context_window` set in YAML is still set on the `LLMProviderConfig` the runtime reads (copy through `_to_llm_provider`). Saving the settings dialog does not change that value. The spin range is still `0.50`–`0.95`.
- Estimate at or under the landing goal: the mask cycle replaces nothing.
- Estimate between the landing goal and `soft_ceiling`: a 480-character tool result is kept; a 520-character result whose path is not protected is masked. The mask string is byte-identical to today's SPEC-23 format.
- Three successful stats of one path store one pin: that path and `(st_mtime_ns, st_size)`. A fourth read of the unchanged file does not overwrite the pair. No `record_fact` call.
- A file that is already dirty against HEAD still pins. The facts board is not consulted.
- After that third read, a mask run keeps that path's results while the stat still matches.
- Changing the file (mtime or size) drops the pin and resets the count. The next mask may stub that path's old results. Three further reads of the new bytes store a new pin.
- A new user message does not clear `_hot_pins` or `_edited_paths`. `drop_read_cache` does.
- A path this conversation wrote stays in `_edited_paths` after the pin drops.
- `compact` is still called with `soft_ceiling` while the estimate is at or under that line. It is not called with the landing goal on that path.
- Manual `compact_conversation` still uses `max(4_000, hard_ceiling // 2)`.

---

## 3. Stage 2 — Batched JEV adjudication

### 3.1 One call per mask cycle

Add `jev_client.classify_mask_candidates(state: str, candidates: list[dict]) -> dict[str, str] | None`.

The candidates are the indices `select_maskable` returns, in that order. Tier 1 has already removed the last `keep_last` results, protected paths, results under 500 characters, and anything already masked. Those are not sent. There is no second "ambiguous" filter.

One `post_decision` per mask cycle. `questions` is a dict of `candidate_{i}` entries, one per candidate, in order:

```python
{
    "type": "choice",
    "instructions": (
        "Decide what to do with this old tool result. "
        "keep: the path is still needed. "
        "mask: the body can go. "
        "summarize: keep a short excerpt of the body."
    ),
    "criteria": {
        "keep": "The path or result is still needed for the task.",
        "mask": "The body is a dump that can be dropped.",
        "summarize": "A short excerpt is enough.",
    },
}
```

`state` carries path, size, tool name, and a one-line preview per candidate. It does not carry full tool bodies. The answer is only a choice. Do not add a text question and do not read a gist off the answer. `classify_unknown` only validates `type`, `choice`, and `confidence`.

Validate each answer the same way: `type == "choice"`, `choice in {"keep", "mask", "summarize"}`, `confidence >= 0.6`. A candidate that fails validation is masked. That is the Tier 1 action for anything `select_maskable` returned. `post_decision` returning `(None, error)` — no card, keyless, empty model, timeout, HTTP error, shape drift — returns `None` for the whole batch, and every candidate is masked. `post_decision` does not raise. Timeout stays 2.0s.

The call runs only in the band from §2.6: `landing_goal < estimate <= soft_ceiling`, on the `n_obs >= 4` cadence, before the LLM call. No call at or under the landing goal. No call above the provider threshold. An empty candidate list does not call `post_decision`.

### 3.2 Verdicts

- `keep` — leave the tool result, and add its path to `protect_paths` for the rest of this cycle.
- `mask` — existing mask string. The message must still start with `"[masked —"`.
- `summarize` — the same mask string, then a newline, then `original[:200]` of the tool result body from before the replacement. The message must still start with `"[masked —"`. No model-written text.

### Stage 2 acceptance

- Six `select_maskable` indices produce one `post_decision` call. A result Tier 1 kept (short, recent, or protected) is not in that call.
- One candidate at confidence `0.4` is masked. The other five verdicts are applied.
- Whole-batch `None` masks every candidate. Nothing raises.
- A `summarize` message starts with `"[masked —"` and then contains the first 200 characters of the original body. No extra model field is read.
- Estimate `<= landing_goal`: no `post_decision`. Estimate `> soft_ceiling`: no `post_decision`.

---

## 4. Stage 3 — Heavy compact at the provider threshold

### 4.1 Trigger and landing

This replaces the old "pull back under the same line" behavior, and it replaces the hardcoded 85%.

After the mask cycle, re-read the estimate:

```text
if estimate > soft_ceiling and soft_ceiling > landing_goal:
    panic_prune(conv, protect_paths=protect_paths)
    compact(
        conv,
        landing_goal,
        protect_paths=protect_paths,
        preserve_current_turn=True,
    )
else:
    compact(conv, soft_ceiling)
```

`soft_ceiling > landing_goal` holds for every value the settings spin can store (`0.50`–`0.95`) at every window this runtime resolves. A hand-edited YAML threshold that puts `soft_ceiling` at or under the landing goal takes the `else` branch: today's `compact(conv, soft_ceiling)`, no panic stub, `preserve_current_turn` left false. The JEV band is empty in that case because it requires `estimate > landing_goal` and `estimate <= soft_ceiling`.

The current turn is every message after the last message with `role == MessageRole.USER`. There is no turn id on `Message`. `panic_prune(self, conv, protect_paths: tuple[str, ...] = ()) -> int` takes no `keep_last_turns`.

- If there is no `USER` message, stub nothing.
- Otherwise stub every `TOOL_RESULT` whose index is less than that last `USER` message, unless its recovered `path` is in `protect_paths`.
- Leave `TOOL_RESULT`s after that `USER` message alone. Leave non-tool messages alone.
- Recover `path` with the same parent-walk `mask_old_observations` uses (`arguments["path"]`). Compare the argument string. Do not absolutize.
- Use the Tier 1 mask string. The message must start with `"[masked —"`.
- Skip messages that already start with `"[masked —"` or `"[compacted —"`. A second call in the same cycle frees nothing.
- No token target and no JEV.

`compact` gains two keyword arguments, both defaulting so existing callers stay on today's behavior:

```python
def compact(
    self,
    conv,
    token_budget: int,
    *,
    keep_first: int = 2,
    protect_is_summary: bool = True,
    protect_paths: tuple[str, ...] = (),
    preserve_current_turn: bool = False,
) -> None:
```

On the heavy call, `prune_tool_outputs` and `_select_prune_candidate` both skip the same set panic kept: `TOOL_RESULT`s after the last `USER` message, and `TOOL_RESULT`s whose path is in `protect_paths`. An assistant message is skipped when any of its following tool results is skipped, so the pair is not split. The ordinary `compact(conv, soft_ceiling)` call passes neither keyword. `prune_tool_outputs` there still uses `protect_turns=2` and does not know about paths.

The trim loop already breaks when `_select_prune_candidate` returns `None`. Do not shrink `keep_first` or `tail_preserve` to force the landing goal. If the system prompt, the current turn, and the protected tool results are still over the goal, stop.

`CompactionEvent.soft_ceiling` is the `token_budget` argument (`context_strategy.py` sets it to that argument). On the heavy path the event records the landing goal. The provider threshold is still the first element of `_compute_compaction_threshold`. Do not overwrite that function to return the landing goal.

The protected floor can sit above the goal. `get_token_estimate` includes the system prompt, which §4.4b may already hold near 16k tokens. Panic will not stub the current turn or a protected path, and the heavy `compact` will not delete them either. When that floor is already over the landing goal, compaction stops. That is `_select_prune_candidate` returning `None`, not a new loop.

### 4.2 Ordering

Each tool-loop iteration, before the LLM call:

1. Mask cadence, if `n_obs >= 4` and the estimate is inside `(landing_goal, soft_ceiling]`: Tier 1, then Tier 2.
2. If the (refreshed) estimate is over `soft_ceiling` and that line is above the landing goal: `panic_prune`, then `compact(conv, landing_goal, protect_paths=..., preserve_current_turn=True)`.
3. Otherwise: `compact(conv, soft_ceiling)`, which does no work while the estimate is under the line.

`prune_tool_outputs` inside `compact` remains the budget backstop. Panic does not replace the trim/summary layers.

### Stage 3 acceptance

- Estimate over the provider threshold, no protected paths, and a `USER` message present: tool results after that message survive. Tool results before it are stubbed. `compact` is called with the landing goal, `protect_paths`, and `preserve_current_turn=True`.
- No `USER` message: `panic_prune` stubs nothing.
- A path whose pin still matches `os.stat`, and a path in `_edited_paths`, survive the stub and the `compact` that follows, including when those tool results sit before the last `USER` message. A pin whose mtime or size has changed does not.
- The ordinary call under the provider threshold is still `compact(conv, soft_ceiling)` with `preserve_current_turn` false.
- `panic_prune` twice in one cycle frees nothing further.
- Estimate under the provider threshold: `panic_prune` is not called, and `compact` is called with `soft_ceiling`.
- No `0.85` constant and no second fraction. Grep of `agent/runtime.py` and `agent/context_strategy.py` finds none added by this spec.

---

## 5. Risks

| Risk | What the spec does about it |
|---|---|
| A 1M window makes 15% mean ~157k tokens | The landing goal caps at 24_000. The provider threshold still scales with the real window, so the heavy compact still fires before the window fills. |
| Saved providers already store `0.80`, so a new default of `0.15` would never run | `0.15` is not written into `compaction_threshold` or `DEFAULT_THRESHOLD`. |
| Heavy stub removes a file the next turn needs | Last turn, `_edited_paths`, and pins whose mtime and size still match are kept. |
| A dirty file could not be pinned on the facts board | The pin is `(mtime_ns, size)` in memory. Dirty and clean files use the same rule. |
| System prompt already near the landing goal | Compaction does not shrink the system prompt. It stops at the protected floor. |
| SPEC-23 mask tests | The mask string and the default `keep_last=3` stay. Short results are an intentional extra keep. |

---

## 6. Open questions

1. Should the 200-character excerpt also be written to the facts board? Recommendation: no. It stays on the message.
2. Should the heavy path keep the previous user turn as well as the current one? Recommendation: no. The current turn is the messages after the last `USER` message. Older tool results are stubbed unless their path is protected.

Resolved this revision: the settings spin stays the trigger. The landing size is the capped 15% goal, not "back under the spin's line," and not a hardcoded 85%.

---

## 7. Changes by file

### Stage 1

**`agent/runtime.py`**
- `_MAX_SANE_WINDOW = 200_000`, `_LANDING_FRACTION = 0.15`, `_LANDING_CAP = 24_000`.
- `_compute_model_max` — four-step order in §2.1. Docstring updated.
- `_compute_compaction_threshold` — docstring stays "trigger." `DEFAULT_THRESHOLD` stays `0.80`. Return type stays `tuple[int, int]`.
- `_compute_landing_goal(self, hard_ceiling: int) -> int` — §2.2.
- `_read_counts`, `_hot_pins`, and `_edited_paths` init next to `_read_fingerprints`. Clear them in `drop_read_cache` only.
- Successful-result site (~2405): §2.5 for reads, `_edited_paths` for writes.
- Mask site (~1819): gate on the landing goal vs `soft_ceiling` (§2.6). Reset `_obs_since_mask` on every `n_obs >= 4` entry.
- Compact site (~1834): still `compact(conv, soft_ceiling)` in Stage 1. Stage 3 changes the over-threshold call.

**`agent/context_strategy.py`**
- `select_maskable` and the `protect_paths` argument on `mask_old_observations` (§2.3).

**`models/providers.py`**
- `ProviderConfig.context_window: int = 0`.

**`agent/config.py`**
- `LLMProviderConfig.context_window: int = 0`.
- `_to_llm_provider` copies `context_window` (default `0` when the source has no such attribute).

**`utils/providers_store.py`**
- `_from_dict` reads optional `context_window`. Non-int → `0`. Include it in `_to_dict` so a save round-trips.

**`ui/views/settings_dialog.py`**
- `_collect_from_form` passes `context_window=getattr(existing, "context_window", 0) if existing else 0`. The spin is unchanged.

**`tests/test_runtime_compaction.py`**
- `test_custom_threshold_per_provider`: `max_tokens=1_048_576` with no `context_window` expects `hard == 200_000` and `soft == int(200_000 * 0.90)`. Add a case with `context_window=1_048_576` that keeps `hard == 1_048_576`.
- Do not change `test_soft_ceiling_is_80_percent` or `test_fallback_when_no_provider`.

**`tests/test_context_strategy_tier0.py`** (new)
- Ceiling cases in §2.1, landing-goal table in §2.2, `select_maskable` keeps, pin on the third stat, dirty file pins, mtime change drops the pin and the count, pins survive a new user message, `drop_read_cache` clears them, edit-set keep, mask-string regression, and "under the provider line, `compact` receives `soft_ceiling`."

### Stage 2

**`agent/llm/jev_client.py`**
- `classify_mask_candidates` (§3.1). `post_decision`, `classify_unknown`, and the outage guards stay.

**`agent/runtime.py`**
- Inside the §2.6 band only: one `classify_mask_candidates` call, then apply §3.2.

**`tests/test_jev_mask_adjudication.py`** (new)
- Stage 2 acceptance.

### Stage 3

**`agent/context_strategy.py`**
- `panic_prune(self, conv, protect_paths=())` — current turn is after the last `USER` message (§4.1). No `keep_last_turns`.
- `compact` gains `protect_paths=()` and `preserve_current_turn=False`. The heavy call sets both. `prune_tool_outputs` and `_select_prune_candidate` skip the current turn and protected paths only when those arguments say so.

**`agent/runtime.py`**
- The branch in §4.1. Stage 1's unconditional `compact(conv, soft_ceiling)` becomes that branch.

**`tests/test_panic_prune.py`** (new)
- Stage 3 acceptance, including "no panic under the provider threshold" and "budget passed to `compact` is the landing goal."

---

## 8. Data flow

```text
successful read_file
  └─ os.stat(project_path + path) → (mtime_ns, size)
        OSError or empty project_path → no count, no pin
        pin missing → count += 1; at 3, store _hot_pins[session][path]
        pin matches → leave it
        pin differs → drop pin, count = 1
successful edit_file / write_file
  └─ _edited_paths[session].add(path)

tool-loop iteration, before the LLM call
  └─ n_obs >= 4
        └─ reset counter
        └─ landing_goal < estimate <= soft_ceiling
              └─ Tier 1 select_maskable(protect_paths)
              └─ Tier 2 one classify_mask_candidates   # Stage 2
        └─ estimate > soft_ceiling
              └─ skip Tiers 1 and 2
  └─ re-read estimate
        └─ estimate > soft_ceiling and soft_ceiling > landing_goal
              └─ panic_prune(last turn + protect_paths)   # Stage 3
              └─ compact(conv, landing_goal, protect_paths, preserve_current_turn=True)
        └─ else
              └─ compact(conv, soft_ceiling)   # no work while under the line
```

`protect_paths` = `_edited_paths[session]` ∪ pinned paths whose fresh stat still equals the stored `(mtime_ns, size)`.

No new file. The pin dict dies with `drop_read_cache`.

## 9. Edge cases

1. **Third read of a file that was already dirty.** The pin stores that dirty file's mtime and size. Masking keeps the path. The facts board is not involved.
2. **File changes after the pin.** The next `protect_paths` build, or the next successful read, sees a different stat, drops the pin, and resets the count. Old results for that path may be masked. Three new reads can pin the new bytes. If this conversation wrote the file, `_edited_paths` still keeps it.
3. **Two sessions, one path.** `_hot_pins`, `_read_counts`, and `_edited_paths` are keyed by `session_key`.
4. **`read_file` with `offset`.** One path for counting. The fingerprint is the whole file. The pin protects every result for that path.
5. **No protected paths, estimate between the goal and the threshold.** Tier 1 still keeps the last 3 results and anything under 500 characters.
6. **Decision-model card removed.** `post_decision` returns `(None, error)` before any socket when `default_model` is empty. The batch is `None`. Tier 1 stands.
7. **Estimate stays over the landing goal because of the system prompt and the last turn.** Panic and the trim loop stop at that floor. They do not delete the system prompt.
8. **`max_tokens: 1_300_000`, `context_window: 0`.** Hard ceiling `200_000`. Landing goal `24_000`. Default trigger `160_000`.
9. **YAML threshold `0.10` on a 128k window.** `soft_ceiling` (12_800) is under the landing goal (19_200). No panic stub. `compact(conv, soft_ceiling)` only. The settings spin cannot produce this value.
10. **`os.stat` raises `OSError`.** That read does not increment the count and does not pin. A later read whose stat succeeds still counts toward 3. A pin whose stat fails at `protect_paths` time is dropped and its count reset to 0.
11. **Conversation cleared.** `drop_read_cache` drops pins, counts, and edited paths. The next reads start at 0. A new user message in the same conversation does not.
12. **Mask cycle during an open tool-call pair.** Masking already clears `tc.result` on the parent call. `select_maskable` uses those same messages, so the pair stays intact.

## 10. Implementation order

1. **Stage 1a** — ceiling cap, `context_window` on both provider dataclasses, store parse, `_to_llm_provider` copy, and the `test_runtime_compaction.py` update. The fraction is meaningless until the ceiling is real.
2. **Stage 1b** — landing goal, `select_maskable`, mask-cycle gate, read counts, edit set, hot-path pin. `compact` is still called with `soft_ceiling`.
3. **Stage 2** — `classify_mask_candidates` inside the band from 1b.
4. **Stage 3** — `panic_prune` and the branch that calls `compact(..., landing_goal, protect_paths=..., preserve_current_turn=True)` only above the provider threshold.

Each stage has its own test file (1a updates an existing file) and can be one Coder round.

## 11. ARCHITECTURE.md updates

- §3.21p.5: the tool-loop trigger is the provider `compaction_threshold`. The landing goal is `min(15% of the hard ceiling, 24_000)`. Ordinary masking runs between those two. The heavy pass (`panic_prune` then `compact` toward the landing goal) runs only above the provider threshold. `prune_tool_outputs` stays the budget backstop inside `compact`. A hot-path pin is conversation memory (`_hot_pins`: path plus `st_mtime_ns` and `st_size`), cleared by `drop_read_cache`, not a facts-board row.
- Context-window resolution: the four-step `_compute_model_max` order, the `_MAX_SANE_WINDOW` cap on `max_tokens` only, and the `context_window` opt-in copied onto `LLMProviderConfig`.
- Decision-model section: `classify_mask_candidates` is a second consumer of `post_decision`, with the same fail-open contract, and it does not run above the provider threshold.

## 12. Files NOT changed

- `ui/views/settings_dialog.py` spin — still `0.50`–`0.95`, still the trigger, no new control. The file is touched only so `_collect_from_form` copies `context_window`.
- `ui/agent_runtime/session.py` — `compact_conversation` keeps `_, hard_ceiling` and `max(4_000, hard_ceiling // 2)`.
- `agent/llm/convert.py` — no conversion-path changes.
- `agent/persistence.py` — masks are message content, already persisted as content.
- `ui/*` meter — already reads `usage_percent`.
- `utils/project_awareness.py`, `utils/git_ops.py` — pins do not call `record_fact` or `get_head_sha`.
- `utils/prompt_loader.py` — system-prompt budget is untouched.
- `agent/llm/streaming.py`, `agent/llm/openai_provider.py` — JEV reuses `post_decision`.

---

## 13. Spec self-audit (Rule 9)

- Signatures checked in this tree: `_compute_model_max`, `_compute_compaction_threshold` (`DEFAULT_THRESHOLD = 0.80`, return `(soft, hard)`), `compact(conv, token_budget)` including the `token_budget <= 0` return and `CompactionEvent.soft_ceiling=token_budget`, `prune_tool_outputs` early return, `mask_old_observations`, `post_decision` / `classify_unknown` / `_DECISION_MIN_CONFIDENCE = 0.6`, `_dedup_read_output`'s `os.stat` → `(st_mtime_ns, st_size)` and its `OSError` / empty-`project_path` skips, `_read_fingerprints` popped at line 1767 on every new user message, `drop_read_cache` called from `clear_conversation`, `_to_llm_provider` field list, settings spin `new_with_range(0.50, 0.95, 0.05)`, `_collect_from_form` building `ProviderConfig` from an explicit keyword list, `_select_prune_candidate` returning `None` when the trimmable region is empty, `MessageRole.USER == "user"`, `compact_conversation`'s `_, hard_ceiling` unpack, `get_token_estimate` counting system prompt + task card + messages. `load_live_facts` drops dirty paths, which is why the pin is not a fact.
- Corrected from earlier drafts: (1) `DEFAULT_THRESHOLD` is not changed to `0.15`. (2) There is no 85% line. (3) The pin is `(st_mtime_ns, st_size)` in `_hot_pins`, not a fact. (4) `_read_fingerprints` is per turn. Pins clear in `drop_read_cache`. (5) `context_window` is copied in `_to_llm_provider` and in `_collect_from_form`, or Save writes `0`. (6) Task-card text is not a path source. (7) Read/edit accounting is at the successful-result site (~2405). (8) JEV candidates are `select_maskable`'s indices. `summarize` appends `original[:200]`. There is no gist field on the choice answer. (9) The current turn is the messages after the last `MessageRole.USER`. `panic_prune` has no `keep_last_turns`. The heavy `compact` skips that turn and `protect_paths` in both `prune_tool_outputs` and `_select_prune_candidate`.
- `test_custom_threshold_per_provider` will fail if Stage 1a lands without updating it. The spec includes that update.
- New names (`select_maskable`, `panic_prune`, `classify_mask_candidates`, `_compute_landing_goal`, `_MAX_SANE_WINDOW`, `_LANDING_FRACTION`, `_LANDING_CAP`, `_read_counts`, `_hot_pins`, `_edited_paths`) are not in `agent/`, `models/`, or `utils/` today. `context_window` already exists on `utils/provider_test.py::TestResult` — that is the Test Connection probe, and the settings dialog copies it into the `max_tokens` spin. It is not a field on `ProviderConfig` or `LLMProviderConfig`. The field this spec adds is the persisted override, a different object.
- `keep_last` is reused on purpose so it matches `mask_old_observations`.
