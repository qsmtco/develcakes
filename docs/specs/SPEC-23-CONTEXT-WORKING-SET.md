# SPEC-23: Context working set — real caps, one token copy, mask old observations

**Date:** 2026-10-11
**Author:** Micah
**Status:** Draft — Stage 1 for implementation; Stage 2 after Stage 1 lands; Stage 3 interface pinned, delivery gated
**Amended:** 2026-10-11 — SPEC-24 owns the compiled turn after a write, and the sha-scoped fact records. This spec's Stage 2 `task_card` stays a per-session scratch. Do not grow it into project memory. Stage 3's repo map stays gated; it is not the fact board.
**Implements:** PM direction 2026-10-11 (context fills too fast; plan recorded in the investigation the same day)
**Depends on:** `DefaultContextStrategy.compact` and `resolve_context_mode` (extend them; do not replace the strategy protocol)
**Target branch:** main

> **Architecture compliance:** tool output caps live in `agent/tools.py` (the module that already owns `ToolResult` and `execute_tool`). The token estimate stays on `Conversation` in `models/conversation.py` and must count the same strings `to_api_messages()` sends. Compaction stays in `agent/context_strategy.py`. Session scratch files go through the existing `resolve_session_workspace` (`agent/persistence.py`). The context meter already reads `usage_percent` from `get_token_breakdown`; no new UI surface. `agent/llm/convert.py` is not modified.

---

## DISCOVERY

- Read `agent/tools.py`: `_read_file(path, project_path, offset=None, limit=None)` at line 359. `offset` / `limit` are **bytes** (schema text at line 1040: "Byte offset to start reading from"). `f.read(limit)` with `limit is None` reads the whole file. The 50KB branch at lines 387–388 **appends** a notice and does not slice. `MAX_READ_SIZE = 50 * 1024` (line 238). Files over 50MB return `success=False` (line 370). `PermissionError`, `UnicodeDecodeError`, and `OSError` are the three handlers (lines 392–397). `execute_tool` (line 1398) injects `project_path`, `session_key`, `approval_callback`, `scratch_dir` into handler kwargs (lines 1482–1486). The `read_file` lambda (line 1047) accepts `**kwargs` and drops `session_key`. `list_files` (line 652) joins every entry and returns it. `_exec_command` prefix-slices stdout, stderr, and `output` at `MAX_EXEC_OUTPUT` (100KB, lines 598–606). `ToolResult.output` is what the model sees; `stdout` / `stderr` are separate fields (lines 45–53).
- Read `tests/test_tools.py`: `test_read_truncates_at_50kb` (line 137) only asserts `"truncated" in r.output`. `test_read_with_offset_and_limit` (line 155) asserts `r.output == "56789"` with **no** suffix, for a 20-byte file read with `offset=5, limit=5`.
- Read `tests/test_process_kill.py`: `test_exec_output_truncation_intact` (line 110) asserts the `MAX_EXEC_OUTPUT` marker is in `r.stdout` and `len(r.stdout) <= MAX_EXEC_OUTPUT + 100`. That contract stays.
- Read `models/conversation.py`: `_count_char_tokens` (line 305) and `_count_tokens_accurate` (line 366) both add `msg.content` **and** `tc.result`. `to_api_messages` (line 232) serializes tool calls as id/name/arguments and the tool message as `content` only. It does not send `ToolCall.result`. Cache key is `(len(self.messages), hash(self.system_prompt))` (line 356). `encode_ordinary` is required (`encode` raises `ValueError` on special-token literals).
- Read `agent/persistence.py`: `save_conversation_to_disk` writes tool calls as `call_id` / `tool_name` / `arguments` only (lines 101–107). `ToolCall.result` is not persisted. `resolve_session_workspace(project_path, session_key)` (line 946) returns `{project}/.crabcakes/tmp/{session_key with ':' → '-'}` created at `0o700`. It raises `ValueError` when `project_path` is empty, `session_key` is empty or has whitespace, `session_key` contains `..`, or `session_key` fails `[a-zA-Z0-9._:-]+`.
- Read `agent/runtime.py`: `_execute_tool_batch` calls `tc.mark_completed(...)` then `conv.add_tool_result(call_id, tc.result)` (lines 2335–2338). `compact` runs at the top of each tool-loop iteration (line 1773) **before** the LLM call. `_cleanup_tool_history` runs from `_terminate_turn` (line 996) and `cancel` (line 1408).
- Read `agent/context_strategy.py`: `prune_tool_outputs` (line 345) returns 0 when `get_token_estimate() <= target_tokens`. It stubs `msg.content` and does not clear `tc.result`. Already-stubbed messages match the prefix `"[compacted \u2014"` (line 413). `protect_turns` defaults to 2; the runtime calls `prune_tool_outputs(conv, token_budget, protect_turns=2)` from `compact` (line 160).
- Read `agent/context.py`: `resolve_context_mode` (line 463) maps `auto` to `preload` when `window >= 500_000`, `jit` when `window <= 32_000`, otherwise `hybrid`. `build_file_context_with_core_files` inlines core file bodies in `preload` and `hybrid`.
- Read `agent/llm/convert.py`: `convert_messages_for_anthropic` turns a `system` message into a `user` message (line 28) and passes other roles through. Two consecutive `user` messages are not merged.
- Read `ui/agent_runtime/session.py`: `clear_conversation` empties `conv.messages` and sets `_token_estimate_cache = None` (lines 78–85). The comment names the two-part cache key.
- Read `tests/test_token_memo.py`: `test_breakdown_matches_independent_count_and_is_cached` (line 97) re-implements the count and **includes** `tc.result` (lines 108–109).
- Read `.gitignore` line 37: `.crabcakes/` is already ignored. Spill files under `.crabcakes/tmp/` do not need a new ignore rule.
- Architecture owner: `Conversation` owns messages and the token estimate (`docs/ARCHITECTURE.md` §3.21l). `agent/context.py` owns the system-prompt file context (§3.21p). `DefaultContextStrategy` owns prune/trim. `execute_tool` owns what a tool returns to the runtime.
- Existing pattern to copy: `_exec_command` slices **then** appends the notice (`stdout[:max_stream] + notice`). `_read_file` must do that. `prune_tool_outputs` invalidates `_token_estimate_cache` after every in-place content edit because the cache key does not include content.

Measured 2026-10-11 with `.venv/bin/python` against this tree: a 100_000-byte file read returned 100_035 bytes; a 500_000-byte file returned 500_035; `limit=200000` returned 200_035. A 40_000-character tool result estimated at 40_015 tokens; after stubbing only the tool message, the wire content was 51 characters and the estimate fell to 20_029 because `tc.result` was still 40_000 characters.

---

## 1. Overview

### Problem

One `read_file` of a large file puts the whole file (up to the 50MB reject) into the conversation, then adds a notice that claims it was truncated at 50KB. The context meter counts that body twice, because `ToolCall.result` and the tool message hold the same string and both are counted. Layer-1 compaction stubs the message and leaves `ToolCall.result` in memory, so the meter only drops about half. `list_files(recursive=True)` has no cap. The `write_file` tool text tells the model to read the whole file and write it back.

### Solution

Stage 1 makes every cap true and makes the meter count the wire payload once. Stage 2 keeps a small working set: the last three raw tool results, older ones replaced with a one-line mask, plus a short task card that compaction does not delete. Stage 3 (gated) replaces inlined file bodies with a signature map.

### Scope

| In | Out |
|---|---|
| Stage 1: slice `read_file`, cap `list_files`, bound every tool's `output` before it returns from `execute_tool`, count tokens once, clear `ToolCall.result` when a tool message is stubbed, dedup identical reads inside one turn, fix the `write_file` instructions | Changing `MAX_EXEC_OUTPUT` or the stdout/stderr prefix slice in `_exec_command` |
| Stage 2: mask old tool results in batches of 4, persist a task card, stop `auto` from selecting `preload`, stop `hybrid` from inlining core file bodies | A new message role. A new meter widget. LLM summarization as the first compaction step |
| Stage 3: `build_repo_map` interface only | Implementing the map, adding tree-sitter, deleting `preload` |

### Principles

- The string in the tool message is the only copy the model and the meter see.
- `offset` and `limit` on `read_file` stay byte offsets. The schema already says so. A line window is an additional cut inside that byte window.
- A caller-supplied `limit` smaller than 50KB is honored exactly, with no truncation suffix (`test_read_with_offset_and_limit`).
- A caller-supplied `limit` larger than 50KB is clamped to `MAX_READ_SIZE`. The model cannot opt out of the cap.
- Spill files are for command and search output. A `read_file` result points back at the source path. Do not copy the source file into tmp.
- Masking rewrites history in batches so the prefix is stable across the calls in between. Tell the model in the stub what was removed.
- The task card is spliced into the last `user` message at serialization time. It is not a second `user` message (`convert_messages_for_anthropic` would not merge them) and it is not appended to `system_prompt` (that busts the prefix).

---

## 2. Changes by file

### 2.1 `agent/tools.py` — Stage 1

**Constants** (next to `MAX_READ_SIZE` at line 238):

```python
MAX_READ_LINES = 2000
MAX_LIST_ENTRIES = 400
# Conversation cap for exec/search/list/mcp output. Process capture stays
# at MAX_EXEC_OUTPUT (100KB) so test_exec_output_truncation_intact holds.
CONV_OUTPUT_CAP = 16 * 1024
CONV_OUTPUT_HEAD = 2 * 1024
CONV_OUTPUT_TAIL = 8 * 1024
```

`MAX_EXEC_OUTPUT` stays `100 * 1024`.

**`_read_file`** — replace the read-and-append block (lines 379–390). Keep the sandbox, 50MB, binary, and the three `except` clauses. `f.seek` of a negative offset already raises `OSError` (`negative seek position`) and the existing handler returns `Cannot read {path}: ...`. Leave that path.

```python
start = 0 if offset is None else offset
if limit is not None and limit < 0:
    return ToolResult(success=False, error="limit must be >= 0")
# A caller-supplied limit smaller than the cap is honored with no notice.
# A larger limit is clamped. An omitted limit uses the cap.
clamped = limit is not None and limit > MAX_READ_SIZE
stopped_by_us = limit is None or clamped
cap = MAX_READ_SIZE if limit is None else min(limit, MAX_READ_SIZE)
with open(resolved, "rb") as f:
    f.seek(start)
    raw = f.read(cap)
content = raw.decode("utf-8", errors="replace")
lines = content.splitlines(keepends=True)
cut_lines = len(lines) > MAX_READ_LINES
if cut_lines:
    content = "".join(lines[:MAX_READ_LINES])
hit_byte_cap = len(raw) == cap and (start + len(raw)) < size
if (hit_byte_cap or cut_lines) and stopped_by_us:
    next_offset = start + len(raw) if not cut_lines else start + len(content.encode("utf-8"))
    content += (
        f"\n[... truncated at {len(content)} bytes; "
        f"file is {size} bytes; continue with offset={next_offset} ...]"
    )
return ToolResult(success=True, output=content)
```

Return value: `ToolResult`. Callers already use `.output` / `.success` / `.error`. The notice is inside `output` so it is what `add_tool_result` stores.

`stopped_by_us` is the difference between "the caller asked for 5 bytes" and "we refused to return the rest." `test_read_with_offset_and_limit` uses `offset=5, limit=5` on a 20-byte file and asserts `r.output == "56789"`. There `limit` is 5, `clamped` is false, `stopped_by_us` is false, and the notice is absent. With no `limit` on a 100KB file, `stopped_by_us` is true and the notice is present. With `limit=200000`, `clamped` is true, `cap` is 51200, and the notice is present.

Line-cut next offset uses `len(content.encode("utf-8"))`. A multibyte character split by `f.read` is decoded with `errors="replace"` (`U+FFFD` is 3 bytes). The continuation offset can skip or repeat a few bytes at that boundary. The notice still names a byte `offset`, which matches the schema. Do not switch `offset` to a line number.

**`_list_files`** — stop the walk. In both the recursive and flat loops, after appending an entry, if `len(lines) >= MAX_LIST_ENTRIES`, append one notice line `"[... truncated at {MAX_LIST_ENTRIES} entries ...]"` and break. On the recursive path, also `dirs[:] = []` before breaking so `os.walk` does not continue. Return `"\n".join(lines)` as today. Empty directory still returns `"(empty)"`.

**`bound_tool_output(result: ToolResult) -> ToolResult`** — new function in this module.

- If `result.output` is shorter than or equal to `CONV_OUTPUT_CAP`, return `result` unchanged.
- Otherwise keep `output[:CONV_OUTPUT_HEAD]` and `output[-CONV_OUTPUT_TAIL:]`, with a middle line `[... {n} chars omitted ...]`.
- Do not modify `stdout`, `stderr`, `error`, or `exit_code`. `_exec_command`'s 100KB prefix slice and `test_exec_output_truncation_intact` stay on those fields.
- Return `dataclasses.replace(result, output=bounded)`. `ToolResult` is a dataclass (the file already imports `dataclasses`; `execute_tool` already calls `dataclasses.replace` at line 1491).

Call it on the two returns that carry model-facing output:

1. The MCP `return ToolResult(...)` around line 1464.
2. The handler `return result` at line 1492, **after** the `duration_ms` replace.

Do not run it on the allowed-tools deny, the unknown-tool error, or the `TypeError` / generic `Exception` returns. Those strings are short and already formatted.

`read_file` output is already ≤ ~50KB plus a one-line notice, which is above `CONV_OUTPUT_CAP` (16KB). Running the bounder on it would head/tail a file window and hide the continuation `offset=`. Skip tools whose output was already windowed:

```python
_ALREADY_WINDOWED = frozenset({"read_file"})
```

Pass `name` into the bounder. If `name in _ALREADY_WINDOWED`, return `result` unchanged. `search_files`, `file_search`, `exec_command`, `list_files`, `web_fetch`, `web_search`, and MCP names go through the head/tail cap.

**`write_file` description** (the string starting at line 1055). Replace the two sentences that tell the model to round-trip the file:

- Remove: "Always read a file before overwriting it."
- Remove: "IMPORTANT: This replaces the ENTIRE file. Always read_file first, then write back the full content with your changes applied."
- Add: "To change part of an existing file, use edit_file. write_file replaces the entire file and is for new files or a full rewrite you already have in hand. read_file returns a window, not the whole file."

Leave `requires_approval`, the parameter schema (`path`, `content`), and `_write_file`'s signature as they are.

**Line count:** about +80 in `agent/tools.py`.

### 2.2 `models/conversation.py` — Stage 1

In `_count_char_tokens` delete the `if tc.result: conv_chars += len(tc.result)` block (lines 313–314).

In `_count_tokens_accurate` delete the `if tc.result: total += ...` block (lines 383–384). Update the docstring so it says the count is system prompt, message content, and `str(tc.arguments)`. It does not count `tc.result`, because that string is the same body already stored on the tool message and `to_api_messages` does not send it.

`get_token_breakdown` uses `_count_text(system_prompt)` and `get_token_estimate() - system_tokens` on the tiktoken path, and `_count_char_tokens` on the fallback path. No separate edit there once both counters drop `tc.result`.

Do not add `tool_call.name`. The existing comment is right: the name is not a separate body field beyond the arguments JSON.

**Line count:** about −8.

### 2.3 `agent/context_strategy.py` — Stage 1

Inside `prune_tool_outputs`, immediately after `msg.content = stub` (line 434), clear the duplicate string on the parent call when this loop has already found it:

```python
for tc in candidate.tool_calls:
    if tc.call_id == msg.tool_call_id:
        tc.result = None
```

`candidate` is the parent assistant message the loop just walked (the `for parent_idx` loop above the stub). If no parent was found, there is nothing to clear. `tc.result = None` does not change `len(messages)` or `hash(system_prompt)`. The cache invalidation already on the next line stays. After Stage 1 the estimate no longer reads `tc.result`; clearing it drops the in-memory duplicate so a 500KB string is not retained on the assistant message for the rest of the session.

Also treat an already-masked body as already short. Extend the idempotence check:

```python
if msg.content.startswith("[compacted \u2014") or msg.content.startswith("[masked \u2014"):
    continue
```

Stage 2 writes the `[masked \u2014` prefix. Stage 1 only adds the check so a later mask is not immediately rewritten.

**Line count:** about +12.

### 2.4 `agent/runtime.py` — Stage 1 dedup

Add `self._read_fingerprints: dict[str, dict[tuple, str]] = {}` next to `self._tool_history` in `__init__`.

Key: `(path, offset or 0, limit)`. Value: `(mtime_ns, size)`. Store it only for a successful `read_file`.

In `_execute_tool_batch`, after `execute_tool` returns and before `add_tool_result`:

- Only for `tool_name == "read_file"` and `result.success`.
- `path = args.get("path", "")`, `offset = args.get("offset")`, `limit = args.get("limit")`.
- Stat the file with `os.stat` on the path joined to `conv.project_path`. If `project_path` is empty or `os.stat` raises `OSError`, skip dedup and store the real output (the tool already sandboxed the read).
- If the same key exists for this `session_key` and `st_mtime_ns` and `st_size` match, set `tool_result_text` to `"[unchanged — read_file {path} offset={offset or 0} limit={limit}; same mtime and size as the earlier read this turn]"`. Do not call `add_tool_result` with the body again.
- Otherwise store the fingerprint and use `result.output` as today.

`mark_completed` currently runs before this decision and would store the full body on `tc.result`. Set `tc.result = None` after copying `tool_result_text`, for every tool, not only reads. The tool message is the copy that is kept. `mark_completed` may still run so `status` and `completed_at` are set; the spec requires `tc.result` to be cleared before `add_tool_result`. Denial paths at lines 2246 and 2266 pass `tc.result or "denied"` into `add_tool_result`. On those paths set a local `text = tc.result or "denied"`, then `tc.result = None`, then `add_tool_result(call_id, text)`. The denial string is short and lives only on the tool message.

Clear the session entry inside `_cleanup_tool_history` (line 3157), beside the existing `self._tool_history.pop`. That function already runs at turn end and on cancel, so the fingerprint does not survive into the next user message. A cleared conversation must not answer "unchanged" for a file the new transcript never saw. `ui/agent_runtime/session.py` `clear_conversation` does not call `_cleanup_tool_history`. Add `AgentRuntime.drop_read_cache(self, session_key: str) -> None` that pops `_read_fingerprints[session_key]`, and call it from `clear_conversation` next to `_token_estimate_cache = None`. Return value is `None`. A missing session key is a no-op (`dict.pop(session_key, None)`).

**Line count:** about +55.

### 2.5 `tests/test_tools.py` and `tests/test_token_memo.py` — Stage 1

`test_read_truncates_at_50kb`: keep the 100_000-byte file. Assert `r.success`, `"truncated" in r.output`, `len(r.output) <= MAX_READ_SIZE + 200`, and `output.count("x") <= MAX_READ_SIZE`. Assert `offset=51200` appears in the notice (`start 0 + cap 51200`).

Add `test_read_explicit_limit_cannot_exceed_cap`: `limit=200_000` on a 500_000-byte file returns at most `MAX_READ_SIZE + 200` bytes and includes `truncated`.

Add `test_read_small_limit_has_no_notice`: the existing `test_read_with_offset_and_limit` stays `assert r.output == "56789"`. Do not weaken it.

Add `test_list_files_recursive_caps`: create `MAX_LIST_ENTRIES + 50` files in one directory, call `list_files` with `recursive=True`, assert the notice and that the output has at most `MAX_LIST_ENTRIES + 1` lines.

`tests/test_token_memo.py` lines 108–109: delete the `if tc.result` addition so the independent count matches the wire. The assertion `bd["conversation_tokens"] == conv_t` then locks the single-copy rule.

Add a regression in `tests/test_conversation.py` (or `test_token_memo.py` if that file already builds a `ToolCall`): one assistant message whose `ToolCall.result` is 4_000 `"Z"` characters and one tool message with the same body. `get_token_estimate()` equals the estimate of a copy of the conversation whose `ToolCall.result` is `None`. That is the double-count regression.

**Line count:** about +70 across the test files.

### 2.6 `agent/context_strategy.py` — Stage 2

New method on `DefaultContextStrategy`:

```python
def mask_old_observations(self, conv: Conversation, keep_last: int = 3) -> int:
```

Returns the number of messages whose content was replaced.

- Collect `TOOL_RESULT` indexes from the end, same scan as `prune_tool_outputs` (lines 391–394).
- `keep_last < 0` is treated as 0 (same clamp as `protect_turns` at line 383).
- The first `keep_last` indexes in that most-recent-first list are kept verbatim.
- Every older `TOOL_RESULT` whose content does not already start with `"[masked \u2014"` or `"[compacted \u2014"` is replaced with:

```text
[masked — {tool_name} {path} offset={offset} ({n} chars removed)]
```

`tool_name`, `path`, and `offset` come from the parent assistant `ToolCall` (`arguments` is a `dict`; `path = tc.arguments.get("path", "")`, `offset = tc.arguments.get("offset", 0)`). Missing parent: `tool_name` is `"[unknown tool]"` and `path` is `""`. Then `tc.result = None` and `_token_estimate_cache = None` after each replacement, same as prune.

This method does **not** consult `token_budget`. It runs even when the conversation is under the soft ceiling. It does not insert a summary message and does not pop messages. Tool-call pairing stays intact.

**Line count:** about +70.

### 2.7 `agent/runtime.py` — Stage 2 mask batching

Add `self._obs_since_mask: dict[str, int] = {}` in `__init__`. Pop it inside `_cleanup_tool_history`.

In the tool loop, **after** tools for this iteration have been appended and **before** the next LLM call (the top of the `while` loop, next to `compact`):

```python
n = self._obs_since_mask.get(session_key, 0)
if n >= 4:
    self._context_strategy.mask_old_observations(conv, keep_last=3)
    self._obs_since_mask[session_key] = 0
```

Increment `_obs_since_mask[session_key]` by the number of `add_tool_result` calls in that iteration (including denials). The model sees each new result raw on the following LLM call. Masking waits until four results have landed, then stubs everything older than the last three. The prefix is unchanged on the iterations in between.

Do not call `mask_old_observations` from `compact()`. Budget prune and observation masking are separate. `compact()` still runs every iteration.

**Line count:** about +20.

### 2.8 `models/conversation.py` and `agent/persistence.py` — Stage 2 task card

This card is one session's scratch list (files touched this conversation). SPEC-24 Stage B stores project evidence in `.crabcakes/facts.json` and injects it through `build_awareness_dict`. When that stage lands, stop calling `_note_task`. Until then, build the card as specified here.

Add on `Conversation`:

```python
task_card: str = ""
```

`to_api_messages`: build the list exactly as today. If `task_card` is non-empty, walk `result` from the end and replace the last `role == "user"` dict with a **new** dict (do not mutate `self.messages`):

```python
{"role": "user", "content": self.task_card + "\n\n" + (entry.get("content") or "")}
```

If there is no user message yet, do not invent one. The card waits until the first user message exists.

Token cache key becomes:

```python
cache_key = (len(self.messages), hash(self.system_prompt), hash(self.task_card))
```

`_count_char_tokens` adds `len(self.task_card)` to `conv_chars`. `_count_tokens_accurate` adds `self._count_text(encoding, self.task_card)`. One count, because the card is not also stored as a message.

`save_conversation_to_disk` writes `"task_card": conv.task_card`. `_conversation_from_data` reads `data.get("task_card", "")`. Old JSON without the key loads as `""`.

`clear_conversation` in `ui/agent_runtime/session.py` sets `conv.task_card = ""` in the same block that empties `messages`. Update the comment on line 82 to name the three-part cache key.

**Who writes the card.** `AgentRuntime._note_task(session_key, line: str) -> None` appends `line` to `conv.task_card`, then if `len(conv.task_card) > 2000` keeps the last 2000 characters (drop from the front, on a newline if one exists in the kept tail). Sets `_token_estimate_cache = None` because the cache key includes the card only after the field changes; invalidating is cheaper than relying on the new key alone if a caller forgets. Call `_note_task` from `_execute_tool_batch` after a successful tool:

| Tool | Line |
|---|---|
| `read_file` | `read {path} offset={offset or 0}` |
| `edit_file` or `write_file` | `{tool} {path}` |
| `exec_command` | `exec exit={result.exit_code} {command}` with `command` trimmed to 80 characters |

Failures are not recorded. The card is a progress list, not a log of errors. The error text is already the tool message.

**Line count:** about +45 across the three files.

### 2.9 `agent/context.py` — Stage 2 system prompt

`resolve_context_mode`, the `auto` branch (lines 493–497):

```python
window = model_max_tokens or 128_000
if window <= 32_000:
    return "jit"
return "hybrid"
```

Delete the `window >= 500_000: return "preload"` branch. Explicit `context_mode="preload"` still returns `"preload"` at line 486. Only `auto` changes.

`build_file_context_with_core_files`, the `hybrid` arm (lines 708–723): do not call `_read_file_safe` and do not append file bodies. For each name in `CORE_FILES`, if the file exists, append one line:

```text
## README.md
[N bytes — use read_file("README.md") to read a window]
```

Then append `build_file_index(project_path)` as today. `preload` is unchanged for callers who set it explicitly.

**Line count:** about +15 / −20.

### 2.10 Stage 3 — gated. The repo map is SPEC-25, not this section

Do not implement with Stage 1 or 2. Do not build the regex signature list described below as a stand-in. SPEC-25 is the reference graph plus the Decision Model rerank.

When Stage 3 is scheduled, add to `agent/context.py`:

```python
def build_repo_map(project_path: str, mentioned: list[str], max_tokens: int = 2000) -> str:
    """Signature map ranked by `mentioned` identifiers. No file bodies.

    Stage 3 only. Until then this function does not exist and nothing calls it.
    """
```

No tree-sitter dependency in Stage 1 or 2. `build_file_index` remains the index hybrid/jit already use.

### Files NOT changed

- `agent/llm/convert.py` — already omits `ToolCall.result`. The task card rides inside the existing last user message, so Anthropic conversion does not grow a new branch.
- `agent/llm/*` providers — they send `to_api_messages()` output.
- `ui/views/main_content.py` context meter — it already displays `usage_percent`.
- `_exec_command` stdout/stderr slicing and `MAX_EXEC_OUTPUT` — process-capture contract.
- `.gitignore` — `.crabcakes/` is already ignored (line 37).
- `prompts/system/*.md` — the round-trip instruction lives in the `write_file` tool description, not in those templates.

---

## 3. Data flow

### Stage 1 read

User message → `_run_loop` → `add_user_message` → tool loop → model returns `read_file` with `path`, optional byte `offset`, optional byte `limit` → `_execute_tool_batch` → `execute_tool` → `_read_file`.

`_read_file` seeks to `offset or 0`, reads `min(limit, MAX_READ_SIZE)` bytes (or `MAX_READ_SIZE` when `limit` is omitted), decodes UTF-8 with `errors="replace"`, cuts to 2000 lines if needed, and appends a continuation notice only when the cap (not the caller's short `limit`) stopped the read.

`execute_tool` does not head/tail `read_file` (`_ALREADY_WINDOWED`). Other tools' `output` is head/tailed to 16KB. `stdout` on `exec_command` is still the 100KB prefix.

Runtime copies `result.output` into the tool message, stores a `(path, offset, limit) → (mtime_ns, size)` fingerprint, and sets `tc.result = None`. Next identical successful read in the same turn becomes the one-line unchanged notice. `_cleanup_tool_history` and `drop_read_cache` drop the fingerprint.

`get_token_estimate` counts system prompt + each `msg.content` + `str(tc.arguments)`. The tool body is counted once.

### Stage 2 mask and card

Every four tool results, before the next `compact` + LLM call, `mask_old_observations` replaces older tool **messages** with the one-line stub and clears `tc.result`. The last three tool messages stay raw. `compact` still prunes when over the soft ceiling and skips bodies that already start with `[masked —` or `[compacted —`.

`_note_task` appends a short line onto `conv.task_card` (max 2000 characters). `to_api_messages` prefixes that string onto the last user message's serialized copy. `system_prompt` bytes are unchanged, so the provider prefix for the system message stays stable when the card changes.

`auto` on a 128k or 1M window resolves to `hybrid`. Hybrid sends the file index plus one stub line per core file. The model calls `read_file` for a window.

---

## 4. File change summary

| File | Change | Stage | Risk |
|---|---|---|---|
| `agent/tools.py` | Real read slice, list cap, `bound_tool_output`, write_file text | 1 | Medium — read contract; small-limit test must stay exact |
| `models/conversation.py` | Stop counting `tc.result`; later `task_card` + cache key | 1 then 2 | Medium — every compaction threshold moves |
| `agent/context_strategy.py` | Clear `tc.result` on stub; `mask_old_observations` | 1 then 2 | Low |
| `agent/runtime.py` | Dedup, clear `tc.result`, mask batch, task notes | 1 then 2 | Medium — tool loop |
| `tests/test_tools.py`, `tests/test_token_memo.py`, `tests/test_conversation.py` | Caps and single-copy regression | 1 | Low |
| `agent/persistence.py`, `ui/agent_runtime/session.py` | Persist and clear `task_card`; drop read cache on clear | 2 | Low |
| `agent/context.py` | `auto` never returns `preload`; hybrid has no file bodies | 2 | Medium — prompt size changes for every new conversation |
| `docs/ARCHITECTURE.md` §3.21p | Document the `auto` and hybrid change | 2 | Low |

---

## 5. Implementation order

1. **Stage 1 read slice and tests.** Change `_read_file`. Run `tests/test_tools.py::TestReadFile`. Confirm `test_read_with_offset_and_limit` is still `56789` and the 100KB file is ≤ `MAX_READ_SIZE + 200`.
2. **Stage 1 list cap and bounder.** Add the list break and `bound_tool_output`. Run `tests/test_process_kill.py::TestExecContract::test_exec_output_truncation_intact`. It must still pass without editing `MAX_EXEC_OUTPUT`.
3. **Stage 1 single token copy.** Edit both counters and `test_token_memo.py`. Add the double-count regression. Run `tests/test_token_memo.py` and `tests/test_conversation.py`.
4. **Stage 1 clear `tc.result` and dedup.** Edit `prune_tool_outputs` and `_execute_tool_batch`. Run `tests/test_context_strategy.py` (prune idempotence) and `tests/test_compact_cache_discipline.py`.
5. **Stage 1 write_file text.** Grep `agent/tools.py` for `Always read_file first`. Zero matches.
6. **Stop.** Stage 2 starts only after Stage 1 tests are green.
7. **Stage 2 mask + batch counter.** Unit-test `mask_old_observations`: five tool messages, `keep_last=3`, the oldest two start with `[masked`, the last three bodies are unchanged, `len(messages)` is unchanged.
8. **Stage 2 task card.** Round-trip through `save_conversation_to_disk` / `load_conversation_from_disk`. Assert a missing `task_card` key loads as `""`. Assert `to_api_messages` does not add a message: `len` equals the no-card length, and the last user content starts with the card. Assert `convert_messages_for_anthropic` is not called with two leading user messages beyond what the system-prompt conversion already produces (system becomes one user, the card is inside the next user, they are still adjacent — that adjacency already exists today for system+user; do not insert a third). Cache key: change `task_card` and assert `get_token_estimate` changes.
9. **Stage 2 context mode.** A test with `model_max_tokens=1_000_000` and `explicit_mode="auto"` returns `"hybrid"`. Explicit `"preload"` still returns `"preload"`. Hybrid output does not contain a second copy of a small `README.md` body (the stub line is present, the file text is not).
10. **Stage 3** is not part of this implementation. Do not add `build_repo_map`.

---

## 6. Acceptance criteria

Stage 1:

- [ ] `read_file` of a 100_000-byte file returns ≤ `MAX_READ_SIZE + 200` bytes, includes `truncated` and `offset=51200`, and does not include 100_000 copies of the fill character.
- [ ] `read_file` with `limit=200000` on a larger file is clamped the same way.
- [ ] `read_file` with `offset=5, limit=5` on `0123456789abcdefghij` returns exactly `56789`.
- [ ] `list_files(recursive=True)` stops at `MAX_LIST_ENTRIES` and says so.
- [ ] `exec_command` stdout truncation test still passes at `MAX_EXEC_OUTPUT` (100KB). `ToolResult.output` for a huge command is ≤ `CONV_OUTPUT_HEAD + CONV_OUTPUT_TAIL + 80`.
- [ ] A tool message and a `ToolCall.result` with the same 4_000-character body estimate the same as that conversation with `tc.result is None`.
- [ ] After `prune_tool_outputs` stubs a tool message, that parent `ToolCall.result is None`.
- [ ] Two successful `read_file` calls with the same path, offset, limit, mtime, and size in one turn: the second tool message is the unchanged notice.
- [ ] `write_file`'s description does not contain `Always read_file first`.
- [ ] `tests/test_tools.py`, `tests/test_token_memo.py`, `tests/test_conversation.py`, `tests/test_context_strategy.py`, `tests/test_process_kill.py` pass.

Stage 2:

- [ ] After four tool results, older than the last three are one-line `[masked` stubs. Message count is unchanged. The next three LLM calls do not rewrite those stubs (the counter was reset).
- [ ] `task_card` survives save/load, clears on `clear_conversation`, and is prefixed onto the last user message without increasing the message count.
- [ ] Changing `task_card` changes `get_token_estimate`. Changing it does not change `system_prompt`.
- [ ] `resolve_context_mode("auto", 1_000_000)` is `"hybrid"`. `resolve_context_mode("preload", 1_000_000)` is `"preload"`.
- [ ] Hybrid file context for a project whose `README.md` is under 50KB does not contain that file's body.

Stage 3: no acceptance criteria until it is scheduled. Absence of `build_repo_map` is correct.

---

## 7. Edge cases

| Case | Expected behavior |
|---|---|
| `offset` negative | `f.seek` raises `OSError`. Existing handler returns `success=False`, error `Cannot read {path}: ...`. |
| `limit` negative | `success=False`, error `limit must be >= 0`. Do not seek. |
| `limit=0` | `f.read(0)` is `b""`. Success, empty output, no notice (`stopped_by_us` is false because the caller set a short limit). |
| `limit` omitted, file ≤ 50KB and ≤ 2000 lines | Full file, no notice. |
| `limit` omitted, file exactly 51200 bytes | `hit_byte_cap` is false (`start + len(raw) < size` is false). No notice. |
| UTF-8 character split at the byte cap | `errors="replace"`. Continuation `offset` may be off by a few bytes. Notice still uses a byte offset. |
| Binary file (`\x00` in the first 512 bytes) | Unchanged: `success=False`, error contains `Binary`. |
| File > 50MB | Unchanged: `success=False`, error contains `50MB`. |
| `project_path` empty during dedup | Skip dedup. The tool's own sandbox error is what gets stored. |
| `session_key` rejected by `resolve_session_workspace` | Stage 1 does not call it. Stage 2 mask does not write a spill file. The stub names the source path from the tool arguments. |
| Parent assistant message missing when stubbing | Stub uses `[unknown tool]`. No `tc.result` to clear. |
| `keep_last` greater than the number of tool messages | `mask_old_observations` replaces nothing and returns 0. |
| `task_card` missing in old JSON | Loads as `""`. |
| `clear_conversation` during an active tool loop | Existing refusal stays (`session.py` returns `False` before the reset). Do not drop the read cache on the refusal path. |
| Explicit `context_mode=preload` | Still inlines file context. Only `auto` stops selecting it. |
| MCP tool with a 1MB `output` | `bound_tool_output` head/tails it. `stdout` is not involved. |

---

## 8. ARCHITECTURE.md updates required

After Stage 2, update §3.21p:

- `resolve_context_mode` `auto` returns `jit` at ≤ 32_000 tokens and `hybrid` otherwise. It does not return `preload`. Explicit `preload` remains.
- `hybrid` is core-file stub lines plus `build_file_index`. It does not inline file bodies.
- Token estimate counts each tool body once (the tool message). `ToolCall.result` is an execution record and is cleared before the next serialization.
- Observation masking keeps the last three tool messages and replaces older ones in batches of four tool results.

No new module. No new UI surface.

---

## 9. Self-audit

1. Read-slice sample traced against `_read_file`'s real signature, the byte schema, `f.read(None)` behavior, and `test_read_with_offset_and_limit`'s exact string. The notice is gated on `stopped_by_us` so that test stays valid.
2. Exceptions on `_read_file`: `PermissionError`, `UnicodeDecodeError`, `OSError` stay. Added `limit < 0` as a `ToolResult` error, not a new exception class. `resolve_session_workspace`'s `ValueError` is not on the Stage 1 path.
3. `ToolCall.result` is not a persistence key. Clearing it does not change the JSON shape.
4. Task card is not a new `user` message, because `convert_messages_for_anthropic` does not merge consecutive users and already turns `system` into a user.
5. Cache key gain of `hash(task_card)` is required. The prune comment in `context_strategy.py` documents that content-only edits do not change `(len, hash(system_prompt))`. The card is a third component, and Stage 2 still assigns `_token_estimate_cache = None` on write.
6. `bound_tool_output` skips `read_file` so the continuation offset is not eaten by the 16KB head/tail.
7. An implementer who follows Stage 1 then Stage 2, and does not add `build_repo_map`, matches this contract.
