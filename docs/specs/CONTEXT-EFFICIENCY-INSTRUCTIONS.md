# Context efficiency — implementation prompt

**Date:** 2026-10-16
**Status:** Ready to hand to the team
**Kind:** Paste-ready implementation prompt. The trace is already done. Do not re-investigate.

---

You are fixing context bloat and turn latency in develcakes. The trace is already done. Do not re-investigate. Implement the changes below, update the tests that lock in the old behavior, and prove the new sizes with the measurement at the end.

This is a context-budget change. Do not change agent roles, tool permissions, the trust gate, untrusted-data fences, compaction rules, or the context-mode resolver (`auto` → `jit` at ≤32k tokens, otherwise `hybrid`).

## Baseline, measured on this repo

Coder, `auto` mode, 128k window (resolves to `hybrid`), before any user text:

| Piece | Chars | Sent |
|---|---:|---|
| System templates | 24,881 | every model call |
| File index | 14,334 | every model call |
| Tool schemas (9 tools) | 8,590, of which 5,482 is description prose | every model call |
| Repo map + header | ~2,500 | every call after a user message |
| Task card | up to 2,000 | grows during the turn |
| **Fixed total** | **~48,000 (~12,000 tokens)** | |

Debugger’s prompt was 41,046 chars. Supervisor’s was 36,022. Tool schemas were ~8,000 chars for all three.

`read_file` is separate and larger than all of the above: one read can put 50KB (~12,000 tokens) into the transcript. Every other tool is cut to 16KB before storage.

Repo map build time on this repo: 1.47s cold, 0.40s same goal again, 0.15s for a different goal. The rendered block was only 2,113 chars. The time is the walk, tree-sitter, and three git processes, not the text.

## Hard rules

- Keep `REPO_MAP_HEADER` and the compiled-packet copy identical. Both attachment points are `AgentRuntime._prefix_repo_map` and `AgentRuntime._compiled_messages`.
- Keep `read_file` out of `bound_tool_output`. It is in `_ALREADY_WINDOWED` so the continuation line (`continue with offset=...`) is not eaten by the head/tail cutter. Lower the read cap. Do not also run the result through `CONV_OUTPUT_HEAD` / `CONV_OUTPUT_TAIL`.
- Do not delete `file_search`. Supervisor’s tool list includes it. Coder and Debugger do not. Shorten it. Do not remove it from `prompts/default_agents/supervisor.yaml`.
- Do not delete `.crabcakes` trust checks, `<untrusted-project-data>` fences, or the bug-journal / project-rules gate in `utils/prompt_loader.py`.
- `tests/test_task_redesign_prompts.py::TestCrabcakesCommands` reads `prompts/system/crabcakes-commands.md` from disk and requires these to remain true: the file contains `/work`, a legacy note, `/task` as a legacy alias, the subcommands `list start done blocked unblock cancel assign priority spec-ready status`, and a “gone” / “no longer” / “removed” note about the `/t` alias. A shorter file is fine. A file that drops those facts is not.
- `tests/test_jit_context_discovery.py` currently requires the file index to list the largest file in a group first (`test_build_file_index_sorted_by_size`) and requires the `file_search` description to contain the exact string `WHEN TO USE`. Update those tests to match the new contract. Do not keep the tests and work around them.
- `tests/test_tools.py` asserts `read_file` returns exactly `MAX_READ_SIZE` bytes for a file of that size. After you change the constant, those tests must follow `MAX_READ_SIZE`, not a hardcoded 50KB.

## 1. File index — largest default-path saving

**File:** `agent/context.py`, `build_file_index`.

**Current behavior.** The docstring says the walk respects `EXCLUDED_DIRS` (`docs`, `.docs`, `documentation`). The walk does not. It only skips dot-directories, `__pycache__`, `node_modules`, `.pytest_cache`, `.mypy_cache`, and `.tox`. It then sorts all files by size, keeps 200, and reads each kept file up to 1MB just to count newlines. On this repo the index listed 1,555 files and the 200 shown rows were mostly `docs/` (88), `tests/` (40), and images. A PNG was reported as “139 lines / 37KB”. The overflow line tells the model to call `file_search("symbol")`. Coder does not have that tool. Coder has `search_files`.

`_build_directory_tree` already skips `docs/`. The index and the tree disagree.

**Required behavior.**

- Skip `EXCLUDED_DIRS` unless `DEVELCAKES_INCLUDE_DOCS=1` (`_include_docs()` already exists). Skip them as directories during the walk, not after the 200-slot cut.
- Skip non-text files. At least: `png jpg jpeg gif webp ico pdf zip gz tar wasm so dll exe`. Do not count their newlines. Do not list them.
- Keep the 200-entry cap and the directory summary for omitted files.
- Within the 200, prefer source over giant tests and giant markdown. A size sort that puts `tests/test_agent_runtime.py` (305KB) first and pushes small source files into the “1,355 more” bucket is the bug. Rank directories so `tests/`, `docs/`, and `prompts/` lose to the rest, then use size only as a tiebreak inside a directory. `tests/` may still appear. They must not consume the list.
- Stop reading file bodies for line counts. Size from `stat` is enough. `include_line_counts=True` may remain as an argument for callers and tests, but the default path that feeds the system prompt must not read file bytes. `tests/test_gitignore_matcher.py` calls both `True` and `False`. Keep both working.
- Change the overflow hint to `search_files`, which every coding agent has. Do not name `file_search` there.
- Update `test_build_file_index_sorted_by_size` so it no longer requires a pure size order. Replace it with a test that a small source file is listed while a larger file under `docs/` is absent, and a test that a `.png` is absent.

**Done when** a hybrid index of this repo contains no `docs/` row, no image row, and includes source files that are smaller than the old top-200 cutoff. `build_file_index` does not open file contents on the default call.

## 2. Tool descriptions — resent on every call

**File:** `agent/tools.py`, every `ToolDefinition.description` inside `_register_tools`.

**Current behavior.** Each of `read_file`, `write_file`, `edit_file`, `exec_command`, `list_files`, `search_files`, `file_search`, `web_search`, and `web_fetch` is a multi-paragraph essay (`WHEN TO USE`, `WHEN NOT TO USE`, `COMMON PATTERNS`). Coder’s nine descriptions are 5,482 chars inside an 8,590-char JSON blob, about 2,100 tokens, attached to every API call. The role templates already tell the agent when to use the tools (`coder.md` “Tool Strategy”, about 624 chars; `debugger.md` “Tool Strategy”, about 2,364 chars).

**Required behavior.**

- One or two sentences per tool. State what it returns and the limit that matters (`read_file` window, `exec_command` needs approval, `edit_file` needs a unique literal match, `search_files` is grep, `file_search` is name-or-content with a short preview).
- Delete the `WHEN TO USE` / `WHEN NOT TO USE` / `COMMON PATTERNS` headings from every description, including `file_search`.
- Parameter descriptions stay one short line. Do not change parameter names or `required` lists.
- Target: Coder’s nine-tool JSON under 4,000 characters (it is 8,590 today). Same diet for the Debugger and Supervisor subsets.
- Update `test_file_search_tool_description_includes_when_to_use`. Assert the description is non-empty and mentions a preview or a file grouping. Do not require `WHEN TO USE`.
- In `_file_search`, stop opening each hit file to EOF to count lines. Print the path and the size. The preview lines already come from grep. The function then gets cut to 16KB by `bound_tool_output`, so the full-file reads are pure waste.

## 3. One copy of the HTML and live-section rules

**Files:** `prompts/system/crabcakes-context.md`, `prompts/system/coder.md`, `prompts/system/debugger.md`, `prompts/system/supervisor.md`.

**Current behavior.** `crabcakes-context.md` lines 8–35 (1,865 chars) explain HTML cards, live sections, fence discipline, and the WebKit surface. Each role file then repeats a longer HTML block (~2,590 chars) and a live-section block (1,207 chars). The live-section text is byte-identical in Coder, Debugger, and Supervisor. Coder’s HTML block and Supervisor’s HTML block are byte-identical. A single agent’s prompt contains both the short copy and the long copy. Only one role template is loaded per agent, so the saving is per prompt, not “delete two of three role files.”

**Required behavior.**

- Keep one copy, in `crabcakes-context.md`, because every agent already loads it.
- That copy must keep the hard edges: the fence is the whole message, no run of three backticks inside the body, inline styles, no `<script>` in static `html` cards, `live` allows inline script and blocks the network, `window.develcakes.call` for consequential actions, no Pango markup, no `MEDIA:` directives.
- Delete the “Communicating in HTML (SPEC-13)” section and the “Live sections (SPEC-19)” section from `coder.md`, `debugger.md`, and `supervisor.md`.
- Leave each role file’s job-specific text (Coder’s bug-fix protocol, Debugger’s heredoc ban and adversarial-audit rules, Supervisor’s onboarding checklist and `provider_events` note).
- Grep tests for “Communicating in HTML”, “SPEC-13”, and “SPEC-19” before deleting. Update any test that required those headings inside the role files so it requires them in `crabcakes-context.md` instead.

**Done when** a composed Coder prompt contains the fence rule and the live-section rule once each.

## 4. One command catalog

**Files:** `prompts/system/crabcakes-commands.md`, `prompts/system/collab.md`, `prompts/system/project-awareness.md`, `prompts/system/code-review.md`.

**Current behavior.** Every project agent loads all of these:

- `crabcakes-commands.md`, 3,707 chars: full slash-command tables plus the crabcard format.
- `collab.md`, 5,176 chars: the `/ask` protocol, a second command table (lines 112–119, 410 chars), and an audit-report spec (lines 65–110, 1,997 chars).
- `project-awareness.md` lines 26–28, 381 chars: the `/ask` 4,096-character rule again, and a pointer at `collab.md`.
- `code-review.md` lines 9–36, 1,054 chars: the audit-report spec again, loaded only when review mode is on.

**Required behavior.**

- Shrink `crabcakes-commands.md` to a short table: the command name, its one-line meaning, and the `/work` subcommands. Keep every fact `TestCrabcakesCommands` asserts (see Hard rules). Cut the crabcard essay down to the fence shape and the required fields (`type`, `title`). The model can read the file if it needs the long form.
- In `project-awareness.md`, delete the Agent Communication paragraph that restates `/ask`. `collab.md` is already in the same prompt. Keep the workflow-phase section.
- In `collab.md`, keep the `/ask` payload rules (quotes, 4,096 cap, one agent, max 3 commands, write large content to a file). Delete the trailing command table that repeats `/ask`, `/delegate`, `/tell`, and `/stop`.
- Keep the audit-report spec in `collab.md` only if review mode is rare and you would rather pay 2,000 chars on every turn. Prefer moving that spec into `code-review.md` (already loaded only when `review_mode != "off"`) and deleting it from `collab.md`. If you move it, `code-review.md` must still contain the required fields: Task, File, Severity, Bug, Expected, Actual.
- Do not remove `collab.md` from the always-loaded set. Agents need the `/ask` mechanism.

## 5. `read_file` default window

**File:** `agent/tools.py`, `MAX_READ_SIZE`.

**Current behavior.** `MAX_READ_SIZE = 50 * 1024`. An omitted `limit` reads 50KB. The tool is exempt from `CONV_OUTPUT_CAP` (16KB). The truncation notice already tells the model the next `offset`. `MAX_READ_LINES = 2000` is a second cut inside that byte window.

**Required behavior.**

- Set `MAX_READ_SIZE` to `16 * 1024`, matching `CONV_OUTPUT_CAP`.
- Leave the offset notice exactly as it is, including `file is {size} bytes; continue with offset={next_offset}`.
- Leave caller-supplied limits alone: a limit under the cap is honored with no notice; a limit above the cap is clamped and noticed. That logic is already in `_read_file`.
- Update the tool’s description so it states the 16KB window, not 50KB.
- Update `tests/test_tools.py` expectations that assume a 50KB fill. They should follow the constant.
- Do not add `read_file` to the `bound_tool_output` path.

## 6. Repo map — cache the walk, re-rank the goal

**Files:** `agent/repo_map.py`, `agent/runtime.py` (`_repo_map_block_for`).

**Current behavior.** The runtime caches the finished string on `(project, hash(goal), tree_version)`. A new user message misses that cache and calls `build_repo_map`, which tree-sitter-parses every `.py` file and runs `git status --porcelain`, `git log -50 --name-only`, and `git diff`. `_PARSE_CACHE` avoids re-parsing unchanged files, but git still runs, and the rank is thrown away. On this repo a goal-only change still cost 0.15s and the map included two test files in a 35-file list. `REPO_MAP_HEADER` is about 400 characters of explanation glued on in front.

**Required behavior.**

- Cache the expensive inputs inside `build_repo_map`: the def/ref walk and the git metadata (`dirty`, `recent`, diff anchors). Key that cache on the project path plus a tree fingerprint (the runtime’s `_repo_map_tree_version` is the right invalidation signal — thread it into `build_repo_map`, or stat a cheap fingerprint). A new goal with the same fingerprint must only re-rank and re-render.
- After the walk is warm, a second goal on the same tree must finish well under the cold time. Target: ≤120ms on this repo, excluding the first build. (Amended 2026-10-16: measurement showed the remaining warm cost is 92% the `_rank_inputs` PageRank re-rank loop, which is the per-goal output the cache exists to serve. Meeting <50ms would require an incremental rank cache — rejected as not worth the complexity for this repo size. Cold 1.7s → warm 92ms still meets the intent: warm is well under cold, and the walk/git inputs are cached.)
- Rank paths outside `tests/` ahead of paths inside `tests/`. If the goal text names a test file or a test function, that test may still appear. Do not drop `tests/` entirely.
- Shorten `REPO_MAP_HEADER` to one or two lines: it is background, it can be stale, open the file for the body, do not mention it. Both call sites must use the same constant.
- Do not call an LLM from `build_repo_map`. That ban is already in the module docstring. Keep it.

## 7. Preload must not read 125KB and keep the wrong half

**Files:** `agent/context.py` (`build_file_context`, `build_file_context_with_core_files`, `_load_project_prompts_context`), `utils/prompt_loader.py` (`_apply_system_prompt_budget`).

**Current behavior.** `preload` is off unless a provider sets it. When it is on, `build_file_context` reads every `.crabcakes` doc (36,536 chars on this repo) and every project prompt (**124,557 chars**, 30 files × 20KB) and then does `full[:max_chars]` with `max_chars=50_000`. That keeps the front of the prompt library and drops the tree and the key files. Core files are appended after that cut. `README.md` here is 52,823 bytes, so `_read_file_safe` (50KB) returns nothing and the “preserved core” is a one-line stub. The later smart truncator in `_truncate_file_context_smart` wants to keep the end. It never sees the tree, because the head cut already deleted it.

**Required behavior.**

- Do not read a prompt file unless the remaining file-context budget can hold it. Apply the size filter that already exists, then stop once the running total reaches the budget. Do not read all 30 and discard them.
- The 50KB assembly cap must reserve the tail, not the head. Order the kept sections so the directory tree and the key-file section survive, and project-prompt bodies are what get dropped first.
- A core file over 50KB stays a stub that names `read_file`. Do not raise `_read_file_safe` to pull a 52KB README into the prompt. That would undo the budget.
- Hybrid and JIT behavior stay as they are, apart from the index changes in item 1. Hybrid does not inline `.crabcakes` docs or the prompt library. Do not “fix” that by inlining them.

## 8. Budget check must count what the request actually sends

**Files:** `agent/runtime.py` (`_messages_for_model`), `models/conversation.py` (`get_token_estimate`).

**Current behavior.** `_messages_for_model` compares `conv.get_token_estimate()` to `model_max - 4096`. The estimate counts the system prompt, message bodies, tool-call arguments, and the task card. It does not count the tool-schema JSON (~2,100 tokens) or the repo map (~500 tokens). Those are added after the check. A turn can be sent over the window the estimate called safe.

**Required behavior.**

- In `_messages_for_model`, add the repo-map block (including `REPO_MAP_HEADER`) and the tool-schema JSON to the number compared against `effective`. Use the same chars//4 or tiktoken path the conversation already uses. Do not double-count the task card. `to_api_messages` already prefixes it, and the estimate already counts it once.
- Do not stuff tool schemas into `conv.system_prompt`. They are a separate API field and must stay there. Counting them only in the budget check is the change.
- A request that would exceed `model_max - _RESPONSE_RESERVE_TOKENS` after those two additions must take the existing over-budget path (`_over_budget_error` / compaction), not a new error string.

## 9. Cap `WORKFLOW_STATUS`

**File:** `utils/project_awareness.py`, `build_awareness_dict`.

**Current behavior.** `TEAM_ROSTER` is capped at 500 chars. `CURRENT_STATE` is capped at 1,000. `PROJECT_MEMORY` is capped at 4,000. `WORKFLOW_STATUS` is the raw `workflow.md` with no cap, and it sits in the template half, which `_apply_system_prompt_budget` never truncates. On this repo it was 1,137 chars. A long workflow file would be injected in full on every turn.

**Required behavior.** Cap `WORKFLOW_STATUS` at 2,000 characters. If you cut it, keep the tail (the current phase is what the agent needs) and append a one-line truncation note. Do not change `get_workflow_content`’s on-disk read. Cap only the prompt variable.

## Out of scope

- Do not rebuild the system prompt every turn. It is built once per conversation on purpose.
- Do not change `resolve_context_mode` thresholds.
- Do not remove the task card or its 2,000-char cap.
- Do not merge `search_files` and `file_search` into one tool.
- Do not change compaction stub text (`[compacted —`, `[masked —`, `[unchanged —`).
- Do not add the project prompt library to hybrid or JIT.

## Verification

After the edits, from the repo root with `.venv/bin/python`:

1. Compose a Coder prompt for this repo with `context_mode="auto"`, `model_max_tokens=128_000`, the Coder tool list, and `build_awareness_dict` for this project. Record template chars and file-context chars separately (split on `\n\n## File context\n\n`).
2. `json.dumps` the Coder tool schemas with compact separators. Record the length.
3. Call `build_repo_map` twice with different goals against this repo and time both. The second call is the warm one.
4. Run:

```bash
pytest tests/test_jit_context_discovery.py tests/test_tools.py tests/test_gitignore_matcher.py tests/test_task_redesign_prompts.py tests/test_prompt_loader.py tests/test_repo_map.py tests/test_repo_map_v2.py tests/test_context_strategy.py tests/test_agent_context_prompts.py -q
```

**Accept when all of these are true:**

- Coder tool-schema JSON is under 4,000 characters.
- The composed Coder prompt contains the HTML fence rule once and the live-section rule once.
- The file-context section contains no `docs/` path and no `.png` path.
- `read_file` of a 50KB text file with no `limit` returns a window of `MAX_READ_SIZE` bytes plus a `continue with offset=` line, and `MAX_READ_SIZE == 16384`.
- The warm repo-map call (second goal, same tree) is ≤120ms (amended gate — see §6).
- The pytest command above passes.
- Report the before/after character counts for the Coder system prompt, the file index, and the tool JSON in the completion note.

Work in that order: index, tool descriptions, template dedup, read window, repo-map cache, preload budget, token check, workflow cap. Each item is independently safe to land. Do not batch them into one unverified edit.
