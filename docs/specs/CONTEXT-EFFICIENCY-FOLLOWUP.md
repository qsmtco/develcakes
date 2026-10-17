# Context efficiency — follow-up

**Date:** 2026-10-16
**Status:** Ready to hand to the team
**Depends on:** `docs/specs/CONTEXT-EFFICIENCY-INSTRUCTIONS.md`
**Kind:** Paste-ready implementation prompt. Do not re-investigate. This file puts six items in scope that the first prompt listed under Out of scope. Where the two files disagree, this file wins, and only for the six items below.

The first prompt still stands for the file index, tool-description length, HTML dedup, command catalog, `read_file` window, repo-map cache, preload budget, token check, and `WORKFLOW_STATUS` cap. Finish that work first. This follow-up starts after it.

Do these six in the order below. That order is payoff versus effort. Items 5 and 6 are in scope only in the bounded form written here. The unbounded form makes the prompt larger or the turn slower, and it is forbidden.

## What changed since the first prompt

The first prompt said:

- Do not change `resolve_context_mode` thresholds.
- Do not remove the task card or its 2,000-char cap.
- Do not merge `search_files` and `file_search`.
- Do not change compaction stub text.
- Do not rebuild the system prompt every turn.
- Do not add the project prompt library to hybrid or JIT.

Those six are now in scope, with the limits in the sections below.

## Hard rules

- Explicit `context_mode` values still win. `resolve_context_mode("preload"|"jit"|"hybrid", ...)` returns that mode for any window size. Only `"auto"` changes.
- `model_max_tokens` of `None`, `0`, or negative still resolves to `hybrid`. An unknown window keeps the index.
- The task card stays. It is still prefixed onto the last user message by `Conversation.to_api_messages`. Only the cap changes.
- `file_search` remains a callable name inside `execute_tool`. Remove it from the schema sent to the model and from `prompts/default_agents/supervisor.yaml`. Old transcripts that already stored a `file_search` call must still execute.
- The three stub prefixes stay byte-for-byte, including the em dash: `[compacted —`, `[masked —`, `[unchanged —`. `agent/runtime.py` `_body_live_in_transcript` and `agent/context_strategy.py` match those prefixes with `startswith`. Renaming them is still forbidden.
- A system-prompt rebuild runs at most once per user turn, at the start of `_run_loop`, before the first model call. It does not run again inside the tool loop. If the fingerprint matches the prompt already on the conversation, do not call `build_system_prompt`.
- Hybrid and JIT gain a name list of `.crabcakes/prompts/*.md`. They do not gain file bodies. Preload is the only mode that may inline prompt bodies, and the first prompt’s budget rules for that mode still apply.
- Do not touch the trust gate, `<untrusted-project-data>` fences, or compaction’s choice of which messages to stub.

## 1. `auto` on a 128k window resolves to JIT

**File:** `agent/context.py`, `resolve_context_mode`.

**Current behavior.** `"auto"` with a window `<= 32_000` returns `"jit"`. Every larger window returns `"hybrid"`. `None` is treated as `128_000` and then returns `"hybrid"`. A negative or zero window returns `"hybrid"` before that default. Hybrid on this repo was a 14,334-character file index plus a one-line README stub. JIT is the index alone. The stub was the only difference, about 75 characters.

**Required behavior.**

- `"auto"` and `1 <= window <= 128_000` returns `"jit"`.
- `"auto"` and `window > 128_000` returns `"hybrid"`.
- `"auto"` and `None`, `0`, or negative stays `"hybrid"`. Do not run `None` through the `128_000` default and then classify it as JIT.
- Update the docstring that says 128k and larger resolve to hybrid.
- Update `tests/test_jit_context_discovery.py`:
  - `test_resolve_context_mode_small_window_jit`: 16k and 32k stay JIT. Add 128k to this test.
  - `test_resolve_context_mode_typical_window_hybrid`: 128k is no longer hybrid. 200k stays hybrid.
  - `test_resolve_context_mode_large_window_is_hybrid`: 500k and 1_000k stay hybrid. Explicit `"preload"` still returns `"preload"`.
  - `test_resolve_context_mode_auto_no_model_returns_hybrid`: `None`, `0`, and negative stay hybrid.
  - `test_resolve_context_mode_explicit_override`: unchanged.

**Done when** `resolve_context_mode("auto", 128_000) == "jit"` and `resolve_context_mode("auto", 200_000) == "hybrid"` and `resolve_context_mode("auto", None) == "hybrid"`.

## 2. Task card cap is 500 characters

**File:** `agent/runtime.py`, `_note_task`.

**Current behavior.** Each successful read, edit, write, or exec appends one line. The card is cut to the last 2,000 characters on a newline boundary, then prefixed onto the last user message of every later model call in the turn. `tests/test_spec23_context_working_set.py::test_note_task_trims_to_last_2000_on_a_newline` writes two 1,500-character lines and asserts `len(conv.task_card) <= 2000`, that the card starts with `b`, and that it does not start with a newline.

**Required behavior.**

- Cut at 500 characters, still on a newline boundary, still keeping the tail. The most recent lines are the ones that prevent a repeated read.
- Leave the empty-line skip, the missing-conversation skip, and the single-line shape (`_task_line`) alone.
- Update that test to `<= 500`. Keep the assertions that the trimmed card starts with `b` and does not start with a newline. Use line lengths that still force a trim under the new cap.
- Grep for the literal `2000` next to `task_card` and update comments in `_note_task` and `models/conversation.py`. Do not change the unrelated `output[:2000]` cap on enforcement-check text in `_maybe_compile`.

**Done when** a card that would have grown past 500 is stored at `<= 500`, the newest line is intact, and `to_api_messages` still prefixes whatever remains onto the last user message only.

## 3. One search tool in the schema

**Files:** `agent/tools.py` (`_search_files`, `_file_search`, `_register_tools`), `prompts/default_agents/supervisor.yaml`.

**Current behavior.** `search_files` is grep and returns matching lines. `file_search` does a filename walk plus the same grep, groups hits by file, and reads each hit to EOF to count lines. Coder and Debugger are given `search_files` only. Supervisor is given both, so both schemas ride on every Supervisor call. The first prompt already tells you to shorten both descriptions and to stop the EOF line-count read. This item goes further: the model sees one tool.

**Required behavior.**

- `search_files` is the only search tool in `get_tool_definitions_for_api` output. One description, one schema.
- Its behavior is the union, without a second code path that re-greps:
  - Content matches come from the existing `_run_grep` call.
  - Filename matches come from `_find_matching_files` when the query is a name fragment.
  - Output is grouped by file: path, size from `stat`, and at most 5 preview lines from the grep hit. No line-count read of the file body.
  - A query that matches nothing returns `(no matches)`.
  - Sandbox, timeout, and `file_type` behavior stay as they are on `search_files`.
- `file_search` stays registered for `execute_tool`. Its handler calls the same function `search_files` calls. It is absent from `get_all_tools()` results that feed the API schema. Filter it out in `get_tool_definitions_for_api`, or stop putting it in the list that function reads. Do not leave a second JSON entry under either name.
- Remove `file_search` from the `tools:` list in `prompts/default_agents/supervisor.yaml`. Coder and Debugger yaml stay as they are.
- Delete `test_file_search_tool_description_includes_when_to_use`’s requirement that a model-facing description exists for `file_search`. Assert instead that `get_tool_definitions_for_api()` contains `search_files` once and does not contain `file_search`, and that `execute_tool("file_search", ...)` still returns the same shape as `execute_tool("search_files", ...)` for one shared fixture.
- Role templates that name `file_search` (`debugger.md` mentions `search_files` already) should name `search_files` only. Grep `prompts/system/` for `file_search`.

**Done when** Supervisor’s tool JSON contains one search function, a `file_search` call still runs, and a search result does not open hit files to count lines.

## 4. Shorter stub tails, same prefixes

**Files:** `agent/context_strategy.py` (the two `[compacted — …]` assignments in `prune_tool_outputs` and `panic_prune`), `agent/runtime.py` (`_dedup_read_output`).

**Current behavior.** A compacted tool result is one line:

```text
[compacted — {tool_name} output, {N} chars removed]
```

A masked result, where tests construct it, looks like:

```text
[masked — read_file a.py offset=0 (12 chars removed)]
```

An unchanged read is:

```text
[unchanged — read_file {path} offset={offset} limit={limit}; same mtime and size as the earlier read this turn]
```

The recovery walk treats a body as dead when it starts with `[masked —` or `[compacted —`, and it skips `[unchanged —`. Several tests assert the compacted sentence exactly, including `tests/test_context_strategy.py` (`[compacted — exec_command output, 5000 chars removed]`) and `tests/test_context_strategy_audit_fixes.py`.

**Required behavior.**

- Keep the three prefixes exactly. Everything that uses `startswith` on them keeps working without an edit.
- Shorten only the tail.
  - Compacted becomes `[compacted — {tool_name} {N}]`.
  - Unchanged becomes `[unchanged — read_file {path} offset={offset} limit={limit}]`. Drop the clause about mtime and size. The numbers stay, because the recovery key is `(path, offset, limit)`.
  - If a writer in this repo builds `[masked — …]`, shorten the parenthetical the same way, to `[masked — {tool} {path} {N}]`, and keep the `[masked —` prefix. Do not invent a new masked writer if the only producers are tests.
- Update every test that asserts the old tail. Tests that only check `startswith("[compacted —")` can stay.
- Both compacted assignment sites must emit the same string. `panic_prune`’s docstring says it uses the same line as `prune_tool_outputs`. Keep that true.

**Done when** a compacted stub is the shorter line, `startswith("[compacted —")` is still true, and the recovery walk still treats that stub as “no live body.”

## 5. Rebuild the system prompt when the turn’s inputs changed

**Files:** `agent/runtime.py` (`_run_loop`, `_ensure_system_prompt`, `rebuild_conversation_context`).

**Current behavior.** `create_conversation` calls `build_system_prompt` once and stores the string. A comment at that site (`TODO: P10.8`) says the prompt is never rebuilt mid-session. `rebuild_conversation_context` rebuilds when the active project path changes, and returns immediately when `project_path` and `system_prompt` are already in sync. Building the prompt walks the tree and, on a cold cache, spends about 0.4s in awareness.

**Required behavior.**

- At the start of each user turn, before the first model call, compute a fingerprint of the inputs that change the prompt: project path, resolved context mode, model window used for the budget, agent role, allowed tool names, review mode, and the awareness cache token (`build_awareness_dict` already invalidates on `.crabcakes` mtime plus a content hash). Include the file-context cache mtime from `_project_root_mtime`.
- If the fingerprint equals the one stored when the current `system_prompt` was built, leave the string alone. Do not call `build_system_prompt`.
- If it differs, call `build_system_prompt` with the same arguments `create_conversation` uses, assign `conv.system_prompt`, and store the new fingerprint. Clear `conv._token_estimate_cache`.
- Do not rebuild between tool iterations of the same turn. The system prompt must be stable for the whole tool loop so a provider prompt cache can hit.
- An empty `system_prompt` (the `defer_prompt_build` path) still goes through `_ensure_system_prompt` first. The fingerprint check runs after that, so the first turn builds once, not twice.
- A failed rebuild keeps the previous string and the previous fingerprint. `rebuild_conversation_context` already does this. Match it.
- Add a test that two turns with no project change call `build_system_prompt` once, and that a test which bumps the awareness fingerprint (write `.crabcakes/context.md`) causes exactly one more build on the next turn.

**Done when** a second turn on an unchanged project does not call `build_system_prompt`, and a real change to `.crabcakes/context.md` does.

## 6. Hybrid and JIT list project prompt names

**File:** `agent/context.py`, `build_file_context_with_core_files`, reusing `_load_project_prompts_context` only for the file list.

**Current behavior.** Hybrid returns core-file stubs plus `build_file_index`. JIT returns the index only. Prompt bodies are loaded only on the preload path, inside `build_file_context` → `_load_project_prompts_context`, which reads up to 30 markdown files at 20KB each. On this repo that read was 124,557 characters, and preload then threw most of it away.

**Required behavior.**

- After the index (and after the hybrid stubs, when in hybrid), append a section:

```text
## Project prompts
{stem}
{stem}
```

- One stem per `.crabcakes/prompts/*.md` file that passes the existing size filter (`_PROJECT_PROMPTS_CONTEXT_CAP`). No file body. No `## .crabcakes/prompts/{stem}` content block.
- Cap the section at 30 names and 2,000 characters. If you cut it, keep the alphabetically first names and append one line: `[… {n} more prompts; open .crabcakes/prompts/ with read_file]`.
- Build the list from `os.listdir` and `stat`. Do not `open()` the prompt files on the hybrid or JIT path.
- JIT and hybrid both get the section. A project with no `prompts/` directory gets no section and no header.
- Preload is unchanged by this item. It still inlines bodies under the first prompt’s budget rules.
- Add a test with three small `.crabcakes/prompts/*.md` files: hybrid and JIT output contain the three stems and do not contain a unique sentence from inside one of the files. A fourth file over the 20KB cap does not appear.

**Done when** a hybrid prompt for a project with prompt files contains the stems, contains none of the prompt bodies, and the builder does not read those bodies.

## Out of scope

- Do not inline prompt bodies, `.crabcakes` docs, or README contents into hybrid or JIT.
- Do not rebuild the system prompt on every tool iteration.
- Do not delete the task card, and do not set its cap below 500.
- Do not remove the `file_search` handler.
- Do not rename `[compacted —`, `[masked —`, or `[unchanged —`.
- Do not change `resolve_context_mode` for an explicit mode, or for an unknown window.

## Verification

After the first prompt’s suite is green, run:

```bash
pytest tests/test_jit_context_discovery.py tests/test_spec23_context_working_set.py tests/test_tools.py tests/test_context_strategy.py tests/test_context_strategy_audit_fixes.py tests/test_dedup_stub_recovery.py tests/test_prompt_loader.py tests/test_agent_context_prompts.py -q
```

**Accept when all of these are true:**

- `resolve_context_mode("auto", 128_000)` is `jit`, and `resolve_context_mode("auto", 200_000)` is `hybrid`.
- A task card over 500 characters is stored at `<= 500`.
- `get_tool_definitions_for_api()` for Supervisor’s tool list includes `search_files` once and does not include `file_search`.
- A compacted stub starts with `[compacted —` and does not contain `chars removed`.
- Two turns against an unchanged project build the system prompt once.
- Hybrid file context for a fixture with prompt files lists the stems and not the bodies.
- The pytest command above passes.

Report the resolved mode for a 128k auto window, the new task-card cap, and whether a second unchanged turn called `build_system_prompt`.
