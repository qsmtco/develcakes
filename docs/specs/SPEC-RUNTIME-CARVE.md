# SPEC: Carve AgentRuntime into mixins, then strip stale comments

**Date:** 2026-10-16
**Author:** Micah
**Status:** Draft — for implementation
**Implements:** Review of `agent/runtime.py` (2026-10-16). No proposal file.
**Depends on:** Phases B1–B6, Phases 4–8, and `docs/specs/SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION.md` are already in the tree. This spec does not reopen them.
**Target branch:** current working tree. Anchor every edit to an identifier. Line numbers below were read on 2026-10-16 and will drift.

> **Architecture compliance:** `docs/ARCHITECTURE.md` §3.21m says `AgentRuntime` owns the tool loop, and §14 says the agent layer does not import `ui/`. This spec keeps that ownership. It moves method bodies into two mixin modules and a types module under `agent/`. `AgentRuntime` remains the class the UI and the tests construct. No new public turn type, no change to cancel, failover, wire-format, or callback behavior.
>
> `ARCHITECTURE.md` §3.21m currently says `runtime.py` is 2,714 lines and that Phase 2b has not yet wired `_terminate_turn`. Both sentences are false against the file that was read. Section 8 of this spec is the required doc update.

---

## DISCOVERY

```
DISCOVERY:
- Read agent/runtime.py (4,269 lines). AgentRuntime is lines 412–4269
  (85 methods). __init__ assigns the per-session dicts. There is no
  Session type.
- Read the method list. The loop pile stays: _run_loop (1580),
  _execute_tool_batch (2174), _terminate_turn (703), send_message (1258),
  cancel (1296), stop_all (1358), start/stop (1006/1012).
- Read the provider pile that moves: _call_llm (2497) through
  _call_llm_streaming (3099), plus the failover helpers between them,
  _resolve_caller_key (2477), set_on_provider_failover (2488),
  _attach_provider_failure (900), _classified_stream_error (955),
  _record_attached_failure (982).
- Read the wire pile that moves: drop_compile (3318) through
  _dedup_read_output (3861), plus _compact_before_llm (3742),
  _compute_landing_goal (1539), _task_line (3768), _note_task.
  force_compact (4103), force_llm_compact (4117), and _call_for_summary
  (4190) stay in runtime.py (see §2.3).
- Read tests that bind the surface that must not move:
  patch.object(rt, "_call_llm") in tests/test_agent_runtime.py and
  tests/test_provider_failover.py, tests/test_stop_all.py,
  tests/test_error_surfacing.py, tests/test_tool_middleware.py,
  tests/test_spec23_context_working_set.py, tests/test_repo_map.py.
  inspect.signature(AgentRuntime._call_llm_streaming) compared to
  StreamingCallKwargs in TestStreamingSignature (tests/test_agent_runtime.py:1914).
  That test also opens the file agent/runtime.py and searches for the
  literal self._call_llm_streaming(.
  inspect.getsource(AgentRuntime._run_loop) asserts the literal
  breakdown["trimmed_this_turn"] = _compaction_happened
  (tests/test_context_strategy_audit_fixes3.py:142).
  from agent.runtime import REPO_MAP_HEADER, WRITE_REPLAY_PREAMBLE
  (tests/test_repo_map.py, tests/test_repo_map_v2.py, tests/test_spec24_compiled_turn.py).
  from agent.runtime import _LANDING_CAP (tests/test_context_strategy_tier0.py).
  from agent.runtime import CANCEL_MESSAGE, TurnStatus, TurnInactive, TurnResult,
  get_valid_callers, SSEEvent, StreamingCallKwargs, _is_empty_content.
  rt._compile[sk], rt._read_fingerprints, rt._hot_pins, rt._read_counts,
  rt._turn_tokens, rt._cancelled_epochs, rt._turn_state are indexed by tests.
  rt._cancel_requested = True is the seed in
  tests/test_error_surfacing.py:706.
  runtime._compaction_this_iteration = False is assigned in four test files
  and is not read back. The attribute is written once in __init__ (line 544)
  and the comments at 1793 say it is no longer meaningful.
- Read ui/agent_runtime/session.py:88-90 (drop_read_cache, drop_compile,
  drop_repo_map), 188-200 (force_llm_compact, force_compact),
  322-324 (rt._turn_token = new_token before send_message).
  send_message passes self._turn_token into the thread (runtime.py:1291).
- Read agent/llm/failure_policy.py: decide(failure, attempt, chain, cfg) -> Decision
  with action in {"retry", "advance_chain", "halt"}. _call_llm_chain imports
  RetryConfig and decide inside the method (runtime.py:2552).
- Read agent/llm/__init__.py: exports get_provider, list_providers,
  LLMProvider, LLMResponse. Does not export a chain.
- Read agent/context_strategy.py compact() (122): token_budget <= 0 returns
  immediately (143). panic_prune(conv, protect_paths=...) is what
  _compact_before_llm calls. The strategy module imports models.conversation
  only. It must not import agent.runtime.
- Read docs/ARCHITECTURE.md §2 (runtime.py still described as 2,344 lines),
  §3.21m (2,714 lines; Phase 2a note that _terminate_turn does not own
  terminals), §3.21m.2 (llm package has no chain module), §8, §14.
- Architecture owner: AgentRuntime still owns the loop, the locks, and the
  dicts. WireContext owns wire assembly methods. LlmChain owns the provider
  attempt. turn_types owns the turn enums the loop and the chain both name.
- Existing pattern this copies: Phase B moved provider.call/stream into
  agent/llm and left AgentRuntime as the caller. This spec does the same
  with inheritance instead of a re-exported underscore alias, because
  patch.object(rt, "_call_llm") patches the instance.
```

---

## 1. Overview

### Problem statement

`AgentRuntime` is one class of 85 methods. Three piles of behavior share it: the turn loop, the provider attempt (failover, one-shot call, streaming), and the wire context (repo map, compiled packet, read dedup, task card, pre-call budget). New context work keeps landing as more methods on the class.

The file is also hard to read because comments describe migrations that already finished. The class docstring still says Phase 2a only registers turn state and that `_terminate_turn` does not own the exits. Every exit in `_run_loop` calls `_terminate_turn`.

A from-scratch rewrite would discard `_terminate_turn`, the cancel epoch, and the tests that lock those races. A new `Session` object that owns `rt._compile` and `rt._turn_tokens` would force every test and `ui/agent_runtime/session.py` onto new attribute paths. That is a second project.

### Solution summary

Split the class by mixin. Methods move. The instance dicts stay where `__init__` assigns them. `AgentRuntime` subclasses the mixins, so `rt._call_llm`, `rt._messages_for_model`, `rt._compile[session_key]`, and `rt.drop_read_cache(session_key)` keep working.

After the move is green, delete migration comments from the moved methods and replace the stale class docstring. Delete the dead `_compaction_this_iteration` attribute and the four test lines that assign it.

### Scope

| In scope | Out of scope |
|---|---|
| New `agent/turn_types.py`, `agent/wire_context.py`, `agent/llm/chain.py` | A `Session` type, and any move of the dicts off `AgentRuntime` |
| `AgentRuntime` subclasses `WireContext, LlmChain` | Rewriting `_run_loop`, `_terminate_turn`, `cancel`, or `stop_all` |
| Re-export the names tests and UI already import from `agent.runtime` | Removing `_cancel_requested` or `_turn_token` |
| Move `_PROVIDER_CALLERS` / `_RESPONSE_FORMAT` / `get_valid_callers` to `agent/llm/registry.py` and bind the same objects in `runtime.py` | Changing `StreamingCallKwargs` or the `_call_llm_streaming` parameter list |
| Comment rewrite on moved methods, the class docstring, and the dead attribute | A sweep of comments inside methods that stay in `runtime.py` |
| `TestStreamingSignature` opens `agent/llm/chain.py` for the call-site search | Retargeting `patch.object(rt, "_call_llm")` |
| `docs/ARCHITECTURE.md` §2 and §3.21m / §3.21m.2 | `agent/context_strategy.py`, `agent/context.py`, `agent/tools.py`, `agent/enforcement.py` |
| Delete `_compaction_this_iteration` and the four assignments | Changing stub prefixes, `CANCEL_MESSAGE`, or `REPO_MAP_HEADER` bytes |

### Architecture principles

- §14.1 / agent layer: new modules do not import `ui/`.
- §3.21m: one class still owns the loop. Mixins are method groups, not a second runtime.
- §0: `ARCHITECTURE.md` is updated in the same change as the move.
- Callbacks stay on `AgentRuntime` and still go through `_dispatch`.

---

## 2. Changes by File

### 2.1 `agent/turn_types.py` (new)

Owns the names both the loop and the chain have to share, so `agent/llm/chain.py` does not import `agent.runtime`.

Move these definitions verbatim from `agent/runtime.py`. Do not change fields or string values.

| Name | Current location | Notes |
|---|---|---|
| `CANCEL_MESSAGE` | module level, value `"Cancelled by user"` | Handler compares this exact string. Do not shorten it. |
| `TurnInactive` | class, `reason: str` | `super().__init__(reason)` and `self.reason = reason`. |
| `TurnStatus` | enum | `RUNNING`, `STREAMING`, `COMPLETED`, `FAILED`, `CANCELLED`. Values are the lowercase strings already on the enum. |
| `TurnResult` | dataclass | Fields: `status`, `session_key`, `turn_token`, `text=""`, `error=None`, `metadata` default factory `dict`. |

`_FailureMessage` stays in `runtime.py`. Only `_terminate_turn` constructs it.

`runtime.py` re-exports, after the import of the types module:

```python
from agent.turn_types import CANCEL_MESSAGE, TurnInactive, TurnResult, TurnStatus
```

`__all__` already lists `TurnStatus`, `TurnResult`, and `CANCEL_MESSAGE`. Leave those entries. Existing imports keep working:

- `from agent.runtime import CANCEL_MESSAGE, TurnStatus` (`tests/test_error_surfacing.py`, `ui/agent_runtime/turn_ui.py`)
- `from agent.runtime import CANCEL_MESSAGE, AgentRuntime, TurnInactive, TurnStatus` (`tests/test_provider_failover.py`)
- `from agent.runtime import AgentRuntime, TurnResult, TurnStatus` (`tests/test_failure_classifier.py`)

No other module imports these names from a new path. Do not add them to `agent/__init__.py`.

Estimated size: about 160 lines, most of it the current docstrings. The comment pass in §2.6 shortens those docstrings.

### 2.2 `agent/llm/registry.py`

`_PROVIDER_CALLERS` and `_RESPONSE_FORMAT` are defined in `runtime.py` today (lines 254 and 292). `_call_llm_once` and `_call_for_summary` read `_PROVIDER_CALLERS`. `_run_loop` reads `_RESPONSE_FORMAT`. `get_valid_callers()` returns `frozenset(_PROVIDER_CALLERS.keys())`. `utils/providers_store.py` documents that its duplicated set must match `agent.runtime._PROVIDER_CALLERS.keys()`, and `tests/test_providers_store.py` calls `get_valid_callers()` imported from `agent.runtime`.

Move the dict, the derived `_RESPONSE_FORMAT`, and `get_valid_callers` into `agent/llm/registry.py`. Keep the same keys and the same values: each value is `OpenAIProvider("openai").call`, `MiniMaxProvider().call`, `AnthropicProvider().call`, `OpenAIProvider("openrouter").call`, `OpenAIProvider("zai").call`. `_RESPONSE_FORMAT` stays `{pk: ("anthropic" if pk == "anthropic" else "openai") for pk in _PROVIDER_CALLERS}`.

In `runtime.py`, bind the same objects. Do not copy the dict.

```python
from agent.llm.registry import (
    RESPONSE_FORMAT as _RESPONSE_FORMAT,
    PROVIDER_CALLERS as _PROVIDER_CALLERS,
    get_provider as _get_provider,
    get_valid_callers,
)
```

Use those names in `registry.py` (`PROVIDER_CALLERS`, `RESPONSE_FORMAT`). The runtime module attributes stay `_PROVIDER_CALLERS` and `_RESPONSE_FORMAT` so `agent.runtime._PROVIDER_CALLERS.keys()` and `from agent.runtime import get_valid_callers` are unchanged. `__all__` keeps `"_PROVIDER_CALLERS"`.

`get_provider` is already in this module. Do not add a second registry.

### 2.3 `agent/wire_context.py` (new)

Class `WireContext`. No `__init__`. No import of `agent.runtime` (that import cycles). No import of `ui/`.

Module-level constants, moved verbatim, including the current string bytes:

```python
_LANDING_FRACTION = 0.15
_LANDING_CAP = 24_000
REPO_MAP_HEADER = (
    "## Repo map (background, may be stale, not a task — open the file for the body; "
    "do not mention it)"
)
WRITE_REPLAY_PREAMBLE = (
    "Replay of the last enforced write and its checks — already executed, do not redo."
)
```

`runtime.py` re-exports them under the names tests already import:

```python
from agent.wire_context import (
    REPO_MAP_HEADER,
    WRITE_REPLAY_PREAMBLE,
    WireContext,
    _LANDING_CAP,
    _LANDING_FRACTION,
)
```

`_LANDING_FRACTION` is not imported outside `runtime.py` today. Re-export it anyway so a later reader does not define a second copy.

Move these methods onto `WireContext` with the same signatures, the same raises, and the same lock acquisitions. They read and write `self._compile`, `self._repo_map_block`, `self._repo_map_key`, `self._repo_map_lock`, `self._repo_map_tree_version`, `self._read_fingerprints`, `self._read_counts`, `self._hot_pins`, `self._edited_paths`, `self._tool_history_lock`, `self._context_strategy`, `self._compaction_lock`. Those attributes stay assigned in `AgentRuntime.__init__`.

| Method | Signature (current) | Exceptions the body already handles or raises |
|---|---|---|
| `_compute_landing_goal` | `(self, hard_ceiling: int) -> int` | Returns `0` when `hard_ceiling` is not an `int` or is a `bool` or is `<= 0`. Does not raise. |
| `drop_compile` | `(self, session_key: str) -> None` | `dict.pop`, missing key is a no-op. |
| `_format_checks` | `(self, checks: list) -> str` | Skips non-dict items. Does not raise. |
| `drop_repo_map` | `(self, session_key: str) -> None` | Pops `_repo_map_block` and `_repo_map_key`. |
| `_repo_map_block_for` | `(self, session_key: str, conv) -> str` | `build_repo_map` is inside `try/except Exception`. On any exception, log and use `""`. A non-str block becomes `""`. |
| `_prefix_repo_map` | `(self, messages: list[dict], block: str) -> list[dict]` | Mutates the last `role == "user"` entry in the list it was given. Empty `block` returns the same list. |
| `_compiled_messages` | `(self, conv, packet: dict, map_block: str = "") -> list[dict]` | Reads `packet["goal"]`, `packet["after"]`, `packet["hunk"]`, `packet["checks"]`. Missing keys raise `KeyError`. Do not add defaults. |
| `_wire_char_tokens` | `(self, messages: list[dict]) -> int` | `len // 4` over content and tool-call argument strings. |
| `_wire_text` | `(self, messages: list[dict]) -> str` | Same bytes `_wire_char_tokens` counts. Keep the two functions in step. |
| `_compile_token_count` | current signature on the method | Do not change the estimator split (tiktoken vs `// 4`). |
| `_over_budget_error` | `(self, used: int, model_max: int) -> RuntimeError` | Returns a `RuntimeError`. The caller raises it. |
| `_addon_tokens` | `(self, conv, text: str) -> int` | `""` returns `0`. |
| `_schema_tokens` | `(self, conv, tools: list \| None) -> int` | `None` or empty returns `0`. |
| `_messages_for_model` | `(self, session_key: str, conv, model_max: int, tools: list \| None = None) -> list[dict]` | Raises the `RuntimeError` from `_over_budget_error` when the post-compaction count is `>= model_max - 4096` (`_RESPONSE_RESERVE_TOKENS` stays on `AgentRuntime`). Does not catch it. |
| `_maybe_compile` | `(self, session_key: str, conv, tool_name: str, args, result) -> None` | Returns with no write when the tool is not `write_file`/`edit_file`, or `enforcement_checks` is missing/empty, or there is no user message. `record_enforcement_fact(...)` stays inside `try/except Exception` that logs and continues. |
| `drop_read_cache` | `(self, session_key: str) -> None` | Under `_tool_history_lock`, pops `_read_fingerprints`, `_read_counts`, `_hot_pins`, `_edited_paths`. |
| `_note_tool_path` | `(self, session_key: str, conv, tool_name: str, args) -> None` | Non-dict `args` returns. `os.stat` `OSError` is logged at debug and returns. |
| `_note_exec_pin` | `(self, session_key: str, conv) -> None` | Key string is `f"<exec:{session_key}>"`. Pin value `(0, 0)` only when the count reaches 3. |
| `_protect_paths` | `(self, session_key: str, conv) -> tuple[str, ...]` | Keep the current stat-and-drop behavior. |
| `_compact_before_llm` | `(self, session_key: str, conv, soft_ceiling: int, hard_ceiling: int) -> None` | See the call below. |
| `_task_line` | `(self, tool_name: str, args: dict, result) -> str` | `""` means skip. |
| `_note_task` | current signature | Cap stays 500 characters, tail kept, cut on a newline. |
| `_key_body_in_transcript` | `(self, conv: Any, path: str, offset: int \| None, limit: int \| None) -> bool` | Delegates with key `(path, offset or 0, limit)`. |
| `_body_live_in_transcript` | `(self, conv: Any, tool: str, key: tuple) -> bool` | Any exception returns `False`. Prefix tests stay the byte strings below. |
| `_dedup_read_output` | `(self, session_key: str, conv, args: dict, output: str) -> str` | Key `(path, offset or 0, limit)`. Value `(st_mtime_ns, st_size)`. Failed stat or empty `project_path` returns `output` unchanged. |

`_compact_before_llm` calls the strategy like this today. Copy it. Do not drop `protect_paths` or `preserve_current_turn`.

```python
landing_goal = self._compute_landing_goal(hard_ceiling)
estimate = conv.get_token_estimate()
if estimate > soft_ceiling and soft_ceiling > landing_goal:
    protect = self._protect_paths(session_key, conv)
    self._context_strategy.panic_prune(conv, protect_paths=protect)
    self._context_strategy.compact(
        conv,
        landing_goal,
        protect_paths=protect,
        preserve_current_turn=True,
    )
    return
self._context_strategy.compact(conv, soft_ceiling)
```

`panic_prune` and `compact` live on `DefaultContextStrategy`. `compact(..., token_budget)` returns immediately when `token_budget <= 0` and records no `CompactionEvent`. The mixin does not catch exceptions from those two calls.

Compiled-packet dict written by `_maybe_compile`:

```python
self._compile[session_key] = {
    "goal": goal,
    "path": path,
    "hunk": hunk,
    "checks": stored,
    "after": len(conv.messages),
}
```

Each checks entry is `{"tier", "passed", "detail", "output"}` with `output` sliced to `[:2000]`. `_format_checks` reads those four keys. Do not rename them.

`_body_live_in_transcript` matches these prefixes with `startswith`. The em dash is U+2014. Do not change the bytes:

- `"[unchanged —"` is skipped
- `"[masked —"` and `"[compacted —"` make the walk return `False`
- any other content for that key returns `True`

Repo-map cache key, already in `_repo_map_block_for`: `(project, hash(goal), tree_version)` where `tree_version` is read under `_repo_map_lock`.

**Stay in `runtime.py`:** `force_compact`, `force_llm_compact`, `_call_for_summary`, `_budget_window_and_mode`, `_compute_model_max`, `_compute_compaction_threshold`, `_last_trim_removed`. `force_llm_compact` swaps `self._context_strategy` to `LLMSummarizeStrategy` under `_compaction_lock` and calls `_call_for_summary`. `_call_for_summary` reads `_PROVIDER_CALLERS` and calls `_attach_provider_failure` / `_record_attached_failure`. Moving those three methods into `wire_context.py` forces that module to import `agent.runtime` or to reimplement the caller dict. Leave them. They call mixin methods through `self` once `AgentRuntime` subclasses `WireContext`.

`_call_for_summary` raises, and must keep raising:

- `RuntimeError` when `model_id` is missing and no default is configured
- `RuntimeError` when `model_id` has no `/`
- `RuntimeError` when the provider name is not in `self._config.providers`
- `ValueError` when `_PROVIDER_CALLERS.get(caller_key)` is `None`
- the provider exception after `_attach_provider_failure` and `_record_attached_failure` (the `except Exception` logs nothing itself; it records, then `raise`)

Return value is the string from `extract_text_content`. Callers of `force_llm_compact` use the `dict` that method already returns. Do not change that return.

Estimated size: about 900 lines.

### 2.4 `agent/llm/chain.py` (new)

Class `LlmChain`. No `__init__`. Do not import `agent.runtime` at module level. Import `TurnInactive` from `agent.turn_types`. Import `decide` and `RetryConfig` from `agent.llm.failure_policy` inside `_call_llm_chain`, which is where the import sits today. Import `get_provider` from `agent.llm.registry`. Import `ssl_retry_budget` from `agent.llm.streaming`. Import `PROVIDER_CALLERS` and `RESPONSE_FORMAT` from `agent.llm.registry` inside the methods that use them today (`_call_llm_once`, and any helper that reads the dict). A module-level import of `registry` is also fine: `registry` does not import `runtime`.

Move `_FAILOVER_ATTEMPT_FLOOR_S = 1.0` with this module. Nothing outside `runtime.py` imports it. Do not re-export it.

Move these methods with the same signatures:

| Method | Signature |
|---|---|
| `_resolve_caller_key` | `@staticmethod (provider_cfg, model: str) -> str` |
| `set_on_provider_failover` | `(self, cb) -> None` |
| `_call_llm` | `(self, session_key: str, messages: list[dict], tools: list[dict], turn_token: object \| None = None) -> dict` |
| `_failover_limits` | `(self) -> tuple[float, float]` |
| `_chain_socket_timeout` | `(self, budget_started: float \| None, budget: float, cap: float) -> float \| None` |
| `_call_llm_chain` | `(self, session_key: str, messages: list[dict], tools: list[dict], turn_token) -> dict` |
| `_apply_chain_decision` | current 11-parameter signature, returns `"retry"`, `"advance"`, or `"raise"` |
| `_failover_action` | current signature, returns `"retry"`, `"advance"`, or `"halt"` |
| `_failover_sleep` | `(self, budget_started, budget, retry_config_cls) -> None` |
| `_inactive_turn_reason` | `(self, session_key: str, turn_token) -> str \| None` |
| `_turn_still_active` | `(self, session_key: str, turn_token) -> bool` |
| `_switch_to_fallback` | `(self, session_key: str) -> str` |
| `_note_failover` | current signature |
| `_discard_partial_assistant` | current signature |
| `_stream_failure_exception` | current signature |
| `_release_session_lease` | `(self, session_key: str) -> None` |
| `_call_llm_once` | `(self, session_key, messages, tools, turn_token=None, request_timeout=None) -> dict` |
| `_call_llm_streaming` | parameters after `self` are exactly the keys of `StreamingCallKwargs`, in the same order |
| `_attach_provider_failure` | current signature |
| `_classified_stream_error` | current signature |
| `_record_attached_failure` | current signature |

`_exit_inactive_turn` stays on `AgentRuntime`. It calls `_terminate_turn` and builds a `TurnResult`. The chain raises `TurnInactive`; the loop catches it and calls `_exit_inactive_turn`. That split is already the code.

`_call_llm` body stays:

```python
if not getattr(self._config, "provider_failover_enabled", True):
    return self._call_llm_once(session_key, messages, tools, turn_token=turn_token)
return self._call_llm_chain(session_key, messages, tools, turn_token)
```

`_call_llm_chain` raises, and must keep raising:

- `TurnInactive(reason)` when `_inactive_turn_reason` returns a string. `reason` is `"superseded"` or `"cancelled"`.
- `RuntimeError("provider failover budget cannot cover another attempt")` when the socket timeout is `None` and `pending` is `None`.
- the stored provider exception when the decision is not retry/advance, and when a retry is requested with no time left (after `_release_session_lease` in that retry branch).
- `RuntimeError("provider failover stopped after 8 steps")` when the `for _step in range(8)` loop ends.

`_inactive_turn_reason` returns `"superseded"` when `_turn_tokens` contains `session_key`, `turn_token is not None`, and `tokens[session_key] is not turn_token`. It returns `"cancelled"` when `session_key` is in `_cancelled_epochs`. A replaced token wins over an armed epoch. `turn_token is None` is not treated as superseded (the `is not None` guard is the current code).

`_call_llm_once` raises `ValueError` in three places, with the current messages:

- `"No conversation found"` when `self._conversations.get(session_key)` is `None`
- the `"Provider '{name}' is not configured..."` message when the model contains `/` and that prefix is missing from `config.providers`
- `"No LLM provider configured for {model}"` when `config.providers` is empty
- `"No caller for provider ..."` when `PROVIDER_CALLERS.get(caller_key)` is `None`

One inner `except (IndexError, KeyError, TypeError, ValueError)` attaches `_crabcakes_context`, calls `_attach_provider_failure(e, session_key, "request_boundary")`, and re-raises. Do not narrow that tuple. Do not add `Exception` to it.

`_call_llm_streaming` raises `ValueError` when `provider.stream` is `None`, with the current `"No streaming caller for caller_key=..."` message. The parameter list must remain, in order:

`session_key`, `base_url`, `api_key`, `model`, `caller_key`, `messages`, `tools`, `timeout`, `x_title=""`, `turn_token=None`, `reasoning_effort="off"`.

`StreamingCallKwargs` stays in `runtime.py`. `TestStreamingSignature` reads it from there. The inherited method's signature is what `inspect.signature(AgentRuntime._call_llm_streaming)` sees.

`_switch_to_fallback` lazy-imports `agent.config._to_llm_provider` and `utils.providers_store.load_providers` inside the method. Leave that import inside the method.

`_call_llm_once` still calls `self._call_llm_streaming(...)`. After the move, that call text lives in `agent/llm/chain.py`. The keyword arguments at the call site stay `session_key=`, `base_url=`, `api_key=`, `model=`, `caller_key=`, `messages=`, `tools=`, `timeout=`, `reasoning_effort=`.

Estimated size: about 1,100 lines.

### 2.5 `agent/runtime.py`

Class line:

```python
class AgentRuntime(WireContext, LlmChain):
```

Both bases have no `__init__`, so `AgentRuntime.__init__` remains the only constructor. Do not add `__init__` to either mixin. A second `__init__` would skip the dict assignments tests rely on.

Before the class is considered done, the three method-name sets (`AgentRuntime` body, `WireContext`, `LlmChain`) must be disjoint. A name on two bases is a bug. `_run_loop` stays on `AgentRuntime` and calls `self._compact_before_llm`, `self._messages_for_model`, `self._call_llm`, `self._dedup_read_output`, `self._note_tool_path`, `self._maybe_compile`. Those resolve through the MRO. Do not wrap them in new forwarder methods.

Delete the moved `def` blocks from this file. Delete the moved constants (`_LANDING_FRACTION`, `_LANDING_CAP`, `REPO_MAP_HEADER`, `WRITE_REPLAY_PREAMBLE`, `_FAILOVER_ATTEMPT_FLOOR_S`, `_PROVIDER_CALLERS`, `_RESPONSE_FORMAT`, `get_valid_callers`, `CANCEL_MESSAGE`, `TurnInactive`, `TurnStatus`, `TurnResult`).

Delete `self._compaction_this_iteration: bool = False` from `__init__`. Nothing in `runtime.py` reads it. The breakdown uses the local `_compaction_happened`. `tests/test_context_strategy_audit_fixes3.py::test_runtime_attribute_no_longer_source_of_truth` asserts that local assignment is in `inspect.getsource(AgentRuntime._run_loop)`. Keep the line:

```python
breakdown["trimmed_this_turn"] = _compaction_happened
```

Delete the comments in `_run_loop` that exist only to explain `_compaction_this_iteration` (the blocks around the current lines 1793 and 1901). Leave the local variables and the breakdown dict as they are.

Do not delete `_cancel_requested`, `_turn_token`, `_cancelled`, or `_cancelled_epochs`. `cancel` sets all of `_cancelled`, `_cancelled_epochs`, and `_cancel_requested`. The loop checks `_cancel_requested` first (metadata `reason` `"shutdown"`, then clears the flag) and `_cancelled` second (metadata `reason` `"user"`). `tests/test_error_surfacing.py` seeds `rt._cancel_requested = True`. `send_message` still passes `self._turn_token`, which `ui/agent_runtime/session.py` assigns before the call.

Do not change `StreamingCallKwargs` or `__all__` except to keep the names that are now imported rather than defined here.

### 2.6 Comment pass (after the move is green)

Do this only after the tests in §6 pass on the verbatim move. It is a separate commit from the move so a logic mistake is not buried in a comment diff.

**Delete**, from the moved methods and from the module banners in `runtime.py` that only say where a symbol used to live:

- "Phase 2a", "Phase 2b", "Phase B1" … "Phase B6", "extracted to", "retained for backward compatibility", "removed in", "this line left in place".
- Repeated ticket stamps: "SP3 fix round 2 (BUG#2)", "Audit-Fix-26 (Bug #3)", "BUG #13", when the same fact is already stated once next to the attribute.

**Keep one sentence at the attribute**, not a copy at every use:

| Attribute | The sentence |
|---|---|
| `_cancelled_epochs` | The loop discards `_cancelled` on the way out. This set is what a late `_dispatch_approval` reads. A `CANCELLED` terminal and `cancel()` add the session. The next turn's start, and a `COMPLETED` or `FAILED` terminal, discard it. |
| `_cancel_requested` | One bool for the whole runtime. The first loop that sees it clears it and ends that session with metadata `reason` `"shutdown"`. Per-session cancel is `_cancelled`. |
| `_state_lock` | Held only around the read-decide-write of `_turn_state` / `_turn_results` / `_turn_tokens`. Not held across `_dispatch`. |
| `_turn_tokens` | `session_key -> turn_token`. A stored token of `None` is a registered token. Membership (`session_key in _turn_tokens`) is the check. A truthiness check on the token is wrong. |
| `_context_strategy.last_result` | One slot on the runtime. `_run_loop` copies it into `_ev_for_breakdown` before any other session can compact. |
| `CANCEL_MESSAGE` | The handler skips the failure card by comparing against this constant. |
| `REPO_MAP_HEADER` / stub prefixes | Both attachment points use this constant. The stub prefixes are matched with `startswith` in this module and in `context_strategy.py`. |

**Replace the `AgentRuntime` class docstring** (the threading essay that still says Phase 2a has not wired `_terminate_turn`). The replacement is the current machine:

- Two threads: the caller (`send_message`, `cancel`, `approve_exec`, the accessors) and one background thread per `_run_loop`.
- `_lock` guards `_conversations`, `_cancelled`, `_cancel_requested`, `_cancelled_epochs` writes that `cancel` does while also holding it, `_active_loops`, `_pending_approvals`, `_running`.
- `_state_lock` guards `_turn_tokens`, `_turn_state`, `_turn_results`. `_terminate_turn` is the only writer of a terminal status. It rejects a token that is not `_turn_tokens[session_key]`, and it rejects a second terminal for the same `(session_key, turn_token)`.
- State key is the tuple `(session_key, turn_token)`.
- `_terminate_turn` dispatches outside the lock: `on_response_complete` for `COMPLETED`, `on_error` for `FAILED` and `CANCELLED`. `FAILED` and `COMPLETED` always `_auto_save`. `CANCELLED` saves only when `metadata.get("persist", False)` is true.
- `cancel` also dispatches `on_error` with `CANCEL_MESSAGE` itself, using `_turn_tokens.get(session_key, self._turn_token)`. The loop's later `_terminate_turn(CANCELLED)` is the duplicate and is ignored.

Delete the paragraph that says terminal call sites still fire `_dispatch(self._on_*)` directly. They do not.

**Do not edit** comments inside `_run_loop`, `_execute_tool_batch`, `_terminate_turn`, `cancel`, or `stop_all` beyond the `_compaction_this_iteration` deletions in §2.5 and the class docstring. Those methods did not move. A full comment sweep of them is a different spec.

The `force_compact` docstring cites `context_strategy.py:130` for the `token_budget <= 0` guard. That guard is the `if token_budget <= 0: return` inside `DefaultContextStrategy.compact`. Do not "fix" the line number in this spec. The method is not moving, and a line number will drift again.

### 2.7 `tests/test_agent_runtime.py`

`TestStreamingSignature.test_streaming_method_signature_matches_caller_interface` opens `agent/runtime.py` and searches for `self._call_llm_streaming(`. After the move that call is in `agent/llm/chain.py`.

Change the file it reads to `agent/llm/chain.py`. Keep the rest of the test: `inspect.signature(AgentRuntime._call_llm_streaming)` still sees the inherited method, and `StreamingCallKwargs` is still imported from `agent.runtime`. The required kwargs in the call chunk stay `session_key=`, `base_url=`, `api_key=`, `model=`, `caller_key=`, `messages=`, `tools=`, `timeout=`, `reasoning_effort=`.

The test's own docstring says the production caller is `_call_llm` in `agent/runtime.py`. The call it searches for is inside `_call_llm_once`. Update that sentence to name `_call_llm_once` in `agent/llm/chain.py`.

### 2.8 Tests that assign the dead attribute

Delete the single assignment `runtime._compaction_this_iteration = False` from each of:

- `tests/test_context_strategy_audit_fixes.py` (the helper near line 59)
- `tests/test_context_strategy_audit_fixes2.py` (near line 67)
- `tests/test_runtime_compaction.py` (near line 33)
- `tests/test_context_strategy_audit_fixes3.py` (near line 59)

Do not delete `test_runtime_attribute_no_longer_source_of_truth` or change its assertion.

### 2.9 Files NOT changed

| File | Why |
|---|---|
| `agent/context_strategy.py` | Compaction stays here. The wire mixin calls it. Stub prefixes stay byte-for-byte. |
| `agent/context.py` | System prompt and file index. Not the wire packet. |
| `agent/tools.py` | Tool execution. Approval stays inline in `_execute_tool_batch`. |
| `agent/enforcement.py` | Unchanged. `_maybe_compile` still reads `result.enforcement_checks`. |
| `agent/tool_middleware.py` | Chain stays constructed in `AgentRuntime.__init__`. |
| `agent/persistence.py` | Save/load stay on `AgentRuntime`. |
| `agent/callbacks.py` | Callback protocols stay. `_dispatch` stays on `AgentRuntime`. |
| `agent/__init__.py` | Does not export the mixins or `turn_types`. |
| `ui/agent_runtime/session.py` | Still calls `drop_read_cache`, `drop_compile`, `drop_repo_map`, `force_compact`, `force_llm_compact`, and still assigns `rt._turn_token`. |
| `ui/handlers/agent_runtime_handler.py` | Still imports `AgentRuntime` and `get_valid_callers` from `agent.runtime`. |
| `utils/providers_store.py` | Still duplicates the caller set. It compares via `get_valid_callers()`, which remains imported from `agent.runtime`. |
| `models/conversation.py` | The task-card comment names `AgentRuntime._note_task`. The method is still reached as `rt._note_task`. Leave the comment. |

---

## 3. Data Flow

Unchanged. The move does not add a step.

1. `ui/agent_runtime/session.py` `send_to_special_agent` sets `rt._turn_token = new_token` and calls `rt.send_message(session_key, text, prepare=...)`.
2. `send_message` starts a daemon thread running `_run_loop(session_key, text, self._turn_token, prepare)`.
3. `_run_loop` registers `_turn_tokens[session_key] = turn_token` and `_turn_state[(session_key, turn_token)] = RUNNING` under `_state_lock`.
4. Each iteration calls `self._compact_before_llm` (now `WireContext`) and `self._messages_for_model` (now `WireContext`).
5. `self._call_llm` (now `LlmChain`) either calls `_call_llm_once` or walks `_call_llm_chain`. A dead turn raises `TurnInactive`. `_run_loop` catches it and calls `_exit_inactive_turn`, which stays on `AgentRuntime`.
6. Tool results go through `_execute_tool_batch`, which calls `_dedup_read_output`, `_note_tool_path`, and `_maybe_compile` on `self`.
7. Every terminal path calls `_terminate_turn(TurnResult(...))`. Key in `_turn_state` and `_turn_results` is `(session_key, turn_token)`.

`drop_read_cache` / `drop_compile` / `drop_repo_map` are still called from `session.py` on the runtime instance during clear. They pop the same dicts `__init__` created.

---

## 4. File Change Summary

| File | Change | Risk |
|---|---|---|
| `agent/turn_types.py` | New. Types moved verbatim. | Low, if re-exported from `runtime.py`. |
| `agent/llm/registry.py` | Caller dict and `get_valid_callers` move here. Same dict object rebound in `runtime.py`. | Medium. A copied dict would split `get_valid_callers` from the chain. |
| `agent/wire_context.py` | New mixin. Wire methods and four constants. | Medium. Packet keys and stub bytes are load-bearing. |
| `agent/llm/chain.py` | New mixin. Provider attempt methods. | High. Failover raises and the streaming signature are locked by tests. |
| `agent/runtime.py` | Subclass the mixins. Delete moved defs. Re-export names. Drop dead attribute. Rewrite class docstring in the second commit. | High, because it is the integration point. |
| `tests/test_agent_runtime.py` | `TestStreamingSignature` reads `agent/llm/chain.py`. | Low. |
| Four compaction test files | Delete one assignment each. | Low. |
| `docs/ARCHITECTURE.md` | §2 tree and §3.21m / §3.21m.2. | Low. Required by §0. |

---

## 5. Implementation Order

1. **Types and registry.** Add `turn_types.py`. Move the caller dict into `registry.py`. Re-bind names in `runtime.py`. Run `tests/test_providers_store.py::TestValidCallersDuplicationInvariant` and `tests/test_provider_failover.py`. `get_valid_callers()` must still return `frozenset({"anthropic", "minimax", "openai", "openrouter", "zai"})`.
2. **Wire mixin, verbatim.** Move the methods and constants. Subclass `WireContext`. Do not rewrite comments yet. Run `tests/test_repo_map.py`, `tests/test_repo_map_v2.py`, `tests/test_spec24_compiled_turn.py`, `tests/test_spec23_context_working_set.py`, `tests/test_dedup_stub_recovery.py`, `tests/test_spec33b_read_pins.py`, `tests/test_spec33b_exec_retention.py`, `tests/test_panic_prune.py`, `tests/test_context_strategy_tier0.py`.
3. **Chain mixin, verbatim.** Move the methods. Subclass `LlmChain` as the second base. Update `TestStreamingSignature` to open `agent/llm/chain.py`. Run `tests/test_agent_runtime.py`, `tests/test_provider_failover.py`, `tests/test_failure_classifier.py`, `tests/test_error_surfacing.py`, `tests/test_stop_all.py`, `tests/test_reasoning_effort.py`.
4. **Disjoint-name check.** A method defined on both `WireContext` and `LlmChain`, or on `AgentRuntime` and either mixin, fails this step. Fix by keeping the method on the class named in §2.
5. **Comment commit.** Apply §2.6. Delete `_compaction_this_iteration` and the four assignments. Re-run the tests from steps 2 and 3, plus `tests/test_context_strategy_audit_fixes.py`, `tests/test_context_strategy_audit_fixes2.py`, `tests/test_context_strategy_audit_fixes3.py`, `tests/test_runtime_compaction.py`.
6. **ARCHITECTURE.md.** Section 8 of this spec.

Each step is green before the next starts. Do not combine step 5 with steps 2–3.

---

## 6. Acceptance Criteria

- [ ] `class AgentRuntime(WireContext, LlmChain)` and neither mixin defines `__init__`.
- [ ] `rt._compile[session_key]`, `rt._hot_pins[session_key]`, `rt._turn_tokens[session_key]`, `rt._cancelled`, and `rt._cancel_requested` still exist on the instance after `__init__`.
- [ ] `from agent.runtime import REPO_MAP_HEADER, WRITE_REPLAY_PREAMBLE, _LANDING_CAP, CANCEL_MESSAGE, TurnStatus, TurnResult, TurnInactive, get_valid_callers, StreamingCallKwargs, SSEEvent, _is_empty_content` still works.
- [ ] `agent.runtime._PROVIDER_CALLERS is` the registry dict (same object, not a copy).
- [ ] `inspect.signature(AgentRuntime._call_llm_streaming)` parameter names, excluding `self`, equal `list(StreamingCallKwargs.__annotations__)`.
- [ ] `patch.object(rt, "_call_llm", ...)` still replaces the method `_run_loop` calls.
- [ ] `REPO_MAP_HEADER`, `WRITE_REPLAY_PREAMBLE`, `CANCEL_MESSAGE`, and the three stub prefixes are byte-identical to the pre-change strings.
- [ ] `_messages_for_model` still raises `RuntimeError` when the wire count is at or over `model_max - 4096`.
- [ ] `_call_llm_chain` still raises `TurnInactive`, the budget `RuntimeError`, and the 8-step `RuntimeError` in the cases in §2.4.
- [ ] `_body_live_in_transcript` still returns `False` on any exception from the walk.
- [ ] `_compaction_this_iteration` is gone from `AgentRuntime.__init__` and from the four test assignments.
- [ ] `inspect.getsource(AgentRuntime._run_loop)` still contains `breakdown["trimmed_this_turn"] = _compaction_happened`.
- [ ] The class docstring does not say Phase 2a has not wired `_terminate_turn`.
- [ ] Moved methods do not contain "Phase 2a", "Phase 2b", "retained for backward compatibility", or "extracted to".
- [ ] `docs/ARCHITECTURE.md` §2 and §3.21m name `wire_context.py`, `llm/chain.py`, and `turn_types.py`, and do not quote 2,344 or 2,714 as the current line count.

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| `turn_token` is `None` and `_turn_tokens[session_key]` is `None` | Not superseded. `_inactive_turn_reason` requires `turn_token is not None` before the identity check. |
| `turn_token` is `None` and a later send stored a real token | Superseded, because `None is not <new token>`. |
| Session in `_cancelled_epochs` and the token was already replaced | `"superseded"`, not `"cancelled"`. `_exit_inactive_turn` returns without `_terminate_turn`. |
| Second `_terminate_turn` for the same `(session_key, turn_token)` | Returns `None`. No second callback. |
| `build_repo_map` raises | `_repo_map_block_for` logs and caches `""` for that key. |
| `os.stat` raises `OSError` in `_note_tool_path` | Debug log, return. No pin. |
| Walk in `_body_live_in_transcript` raises | Returns `False`. The caller treats that as "no live body" and returns the file body again. |
| `record_enforcement_fact` raises | Logged. The compiled packet is already stored. |
| `token_budget <= 0` passed to `force_compact` | `DefaultContextStrategy.compact` returns without a `CompactionEvent`. |
| `provider.stream` is `None` | `_call_llm_streaming` raises `ValueError`. |
| Failover flag missing on config | `getattr(..., "provider_failover_enabled", True)` defaults to on. |
| Two sessions compact at once | Each `_run_loop` keeps its own `_compaction_happened` and `_ev_for_breakdown`. There is no shared bool. |
| Clear during an active loop | `drop_read_cache` is not the active-loop guard. The guard stays `is_loop_active` / `_active_loops`. This spec does not change that path. |
| Tests construct `AgentRuntime` and never call a new initializer | Mixin has no `__init__`, so the dicts exist. |

---

## 8. ARCHITECTURE.md Updates Required

**§2 directory tree.** Replace the `runtime.py` description that says "2344 lines; cost/LLM/SSE/extractors extracted to llm/". State that `runtime.py` is the tool loop and the conversation store, and add:

- `turn_types.py` — `TurnStatus`, `TurnResult`, `TurnInactive`, `CANCEL_MESSAGE`
- `wire_context.py` — `WireContext` mixin: repo map, compiled packet, read dedup, task card, pre-call budget
- `llm/chain.py` — `LlmChain` mixin: `_call_llm`, failover, `_call_llm_once`, `_call_llm_streaming`
- `llm/registry.py` also owns `PROVIDER_CALLERS`, `RESPONSE_FORMAT`, and `get_valid_callers`. `agent.runtime` binds those objects under the existing underscore names.

**§3.21m.** Delete the sentence that `runtime.py` went from 3,297 to 2,714 lines, and delete any claim that `_terminate_turn` is not yet the owner of terminal exits. Replace with: `AgentRuntime(WireContext, LlmChain)` owns the loop, the locks, and the per-session dicts. Method bodies for the wire and the provider attempt live on the mixins. Names tests and the UI import (`REPO_MAP_HEADER`, `WRITE_REPLAY_PREAMBLE`, `_LANDING_CAP`, `CANCEL_MESSAGE`, `TurnStatus`, `TurnResult`, `TurnInactive`, `get_valid_callers`) are defined in the new modules and bound in `runtime.py`.

**§3.21m repo-map table.** The header constant's home is `agent/wire_context.py`. Both attachment points (`_prefix_repo_map`, `_compiled_messages`) are methods on `WireContext`. `runtime.py` re-exports `REPO_MAP_HEADER`.

**§3.21m.2.** Add `chain.py` and the registry's caller dict to the sub-module list.

**Do not** document a `Session` type. This spec does not add one.

---

## 9. Self-audit

Checked against the source read on 2026-10-16:

1. Samples that call `_compact_before_llm`, `_call_llm`, `_maybe_compile`, and `_inactive_turn_reason` match the current control flow, including the `token_budget <= 0` return inside `compact` and the `turn_token is not None` guard.
2. Exception sets in §2.3 and §2.4 are the `raise` / `except` clauses in those methods: `ValueError`, `RuntimeError`, `TurnInactive`, `OSError`, `KeyError` on a short packet, and the `(IndexError, KeyError, TypeError, ValueError)` boundary. `build_repo_map` and `record_enforcement_fact` stay under `except Exception` because that is what the methods do now.
3. Keys checked in source: `_turn_state[(session_key, turn_token)]`, repo-map `(project, hash(goal), tree_version)`, read-dedup `(path, offset or 0, limit)`, compile packet `goal/path/hunk/checks/after`, exec pin `"<exec:{session_key}>"`.
4. Return values: `_terminate_turn` returns the `TurnResult` or `None`; `_call_llm` returns a response `dict`; `_over_budget_error` returns a `RuntimeError` and the caller raises it; `_body_live_in_transcript` returns `False` on failure; `get_valid_callers` returns a `frozenset`.
5. The streaming test opens a file by path. §2.7 changes that path because the call site moves. Leaving the test pointed at `runtime.py` would fail a correct move.

Not verified by executing pytest. This document is the spec, not the implementation.
