# SPEC-25: Repo map — reference graph, reranked by the Decision Model card

**Date:** 2026-10-11
**Author:** Micah
**Status:** Draft — for implementation
**Implements:** PM direction 2026-10-11 (Aider-style signature map, with the configured decision model as the reranker)
**Depends on:** `agent/llm/jev_client.py` `load_decision_card` and the `/v1/systemone` wire. SPEC-23 Stage 1 is not required. SPEC-24 is not required: when no compile packet exists, the map is prefixed onto the last user message of the wire copy.
**Target branch:** main

> **Architecture compliance:** the graph lives in a new `agent/repo_map.py`. It does not import GTK. The HTTP call lives in `agent/llm/jev_client.py` and reads only the Decision Model card in `providers.yaml` (`name == "DecisionModel"`): `base_url`, `api_key`, `default_model`. No vendor host and no model slug are written in the new call path. `conv.system_prompt` is not modified, so conversation creation stays off the network. The map is applied when the tool-loop thread builds the wire list.

---

## DISCOVERY

- Read `agent/llm/jev_client.py`: `DECISION_MODEL_PROVIDER = "DecisionModel"`. `load_decision_card()` lazy-imports `utils.providers_store.load_providers` and returns the `ProviderConfig` whose `name` matches, or `None`. It catches `Exception` and does not raise. `classify_unknown` is the only runtime caller. It POSTs `base_url.rstrip("/") + "/v1/systemone"` with `Authorization: Bearer {api_key}`, body `{"model", "state", "questions"}`. `model` is `card.default_model` or, if that string is empty, the literal `"jev-latest"` (line 132). Timeout is `_JEV_TIMEOUT_S = 2.0`. Confidence below `_JEV_MIN_CONFIDENCE = 0.6` returns `None`. Any network error, non-choice answer, or shape drift returns `None` via `_outage`. `probe_decision_model` repeats that URL and the same `"jev-latest"` fallback (line 225).
- Read `tests/test_failure_classifier.py` `test_keyed_card_classifies_via_mock_endpoint` (line 432): a card with `default_model="typesafe/system-one"` must send that string as `body["model"]` and a URL ending in `/v1/systemone`. The test does not require the `"jev-latest"` fallback.
- Read `models/providers.py`: `ProviderConfig` fields used here are `name`, `base_url`, `api_key`, `default_model`. There is no endpoint-path field. `ui/handlers/settings_handler.py` `add_or_update` already rejects an empty `default_model` (line 96). The Decision Model card is saved with `allow_empty_caller=True` so its model slug does not have to be `vendor/model`.
- Read `ui/views/settings_dialog.py` `_build_decision_page` (line 743): the card name is fixed. The intro text tells the user the endpoint, key, and model are theirs and to point the card at any compatible service.
- Read `utils/prompt_loader.py` `compose_system_prompt`: file context is appended inside the system prompt at conversation-build time (line 336). That function runs on the thread that creates the conversation. A decision-model call must not be added there.
- Read `agent/runtime.py`: `messages = conv.to_api_messages()` at line 1848, on the tool-loop thread, once per iteration. SPEC-24, when implemented, replaces that list with a packet. This spec hooks the same assignment.
- Read `agent/context.py`: `_load_gitignore_patterns` and `_is_ignored` are the gitignore used by `build_file_index`. `agent/tools.py` `_file_search` already imports `_load_gitignore_patterns` from `agent.context`. `build_file_index` skips dot directories, `__pycache__`, `node_modules`, `.pytest_cache`, `.mypy_cache`, `.tox`, and files larger than 1_000_000 bytes for line counts.
- Read `pyproject.toml` dependencies: no `tree-sitter` package is declared.
- Architecture owner: system-prompt file context is `agent/context.py` / `utils/prompt_loader.py`. This map is not that block. The decision-model transport is `agent/llm/jev_client.py`.
- Existing pattern to copy: `classify_unknown`'s fail-open. A missing card, a keyless card, a timeout, or a bad body must leave the map in graph order. The turn must not fail.

---

## 1. Overview

### Problem

The model learns the shape of the repo by reading files. A signature map ranked only by how many times a name is mentioned, or by raw popularity, still shows the wrong symbols for the task in front of the agent. The decision model can score a short list, but the only caller hardcodes a model slug when the card's `default_model` is empty, and the POST is not reusable.

### Solution

Build a Python reference graph and take the top 30 symbols for the current user goal. Ask the Decision Model card, in one request of the same shape `classify_unknown` uses, whether each symbol is needed. Reorder by those probabilities. Render at most 2,000 tokens of signatures into the wire prompt. If the card is absent or the call fails, send the graph order. The model slug, host, and key come from the card. Empty `default_model` does not substitute `jev-latest`.

### Scope

| In | Out |
|---|---|
| Shared `post_decision` used by `classify_unknown`, `probe_decision_model`, and the reranker | A second provider card. A hardcoded host or model slug on the new path |
| Python definitions and references, personalized rank, top 30, one rerank per user goal | Embeddings. A vector index. Parsing every language. Rebuilding the map on every tool call |
| Injecting the rendered block into the wire copy of the user message, or into SPEC-24's packet when that spec is present | Writing the map into `conv.system_prompt` |

### Principles

- Settings → Decision Model is the only switch. Changing `base_url`, `api_key`, or `default_model` on the card named `DecisionModel` changes what this spec calls. No code edit.
- The wire path stays `card.base_url` with `/v1/systemone` appended, which is what `classify_unknown` does today. A new model that speaks that request and answer shape is selected by editing the card.
- Fail open to the graph. Never fail the turn.
- One decision call per distinct user goal per session, not per tool iteration.

---

## 2. Changes by file

### 2.1 `agent/llm/jev_client.py` — shared post

Add:

```python
def post_decision(state: str, questions: dict) -> dict | None:
    """POST state + questions to the Decision Model card.

    Returns the ``answers`` object, or None.
    None covers: no card, empty base_url, empty api_key, empty
    default_model, timeout, non-2xx, unparseable body, missing
    ``answers``, or any exception. Never raises.
    Never inserts a model slug. The ``model`` field is
    ``card.default_model.strip()`` exactly.
    Endpoint is ``base_url.rstrip("/") + "/v1/systemone"``, the same
    concatenation classify_unknown uses today.
    Timeout is the module constant, 2.0 seconds.
    """
```

`classify_unknown` builds its existing `failure_class` question and calls `post_decision`. It keeps the choice / confidence checks it has now (`type == "choice"`, choice in the five classes, confidence `>= 0.6`). Delete the `or "jev-latest"` expression at line 132.

`probe_decision_model` calls `post_decision` with the canned 429 state and its `probe` question. If `post_decision` returns `None` because `default_model` is empty, return `TestResult(ok=False, error="default_model is required", ...)`. Delete the `or "jev-latest"` expression at line 225. A successful probe still reports the choice and confidence it parsed. The probe may keep its own `urlopen` only if `post_decision` cannot tell "HTTP 401" from "low confidence". Preferred: `post_decision` stays fail-open, and the probe duplicates nothing except the empty-model check before the call. Low confidence on the canned 429 is still `ok=True` today (the probe does not apply the 0.6 floor). Keep that: the probe's success path reads `answers["probe"]["choice"]` from a dedicated probe request. Implement the probe as a thin wrapper that uses the same endpoint, headers, timeout, and model resolution as `post_decision` by calling it, and treats `None` as failure with `error="decision model unavailable"`. That changes the probe's error string for HTTP failures from `HTTP {code}` to a generic unavailable message. To keep the HTTP detail, split the helper:

```python
def post_decision(state: str, questions: dict) -> tuple[dict | None, str]:
    """Returns (answers, error). error is "" on success.
    answers is None whenever error is non-empty.
    """
```

`classify_unknown` ignores the error string and returns `None` when answers is `None`. `probe_decision_model` puts `error` on `TestResult.error`. Empty `default_model` yields `error="default_model is required"` and does not open a socket.

Rename `_JEV_TIMEOUT_S` to `_DECISION_TIMEOUT_S` and `_JEV_MIN_CONFIDENCE` to `_DECISION_MIN_CONFIDENCE`. Same values, 2.0 and 0.6. `_outage` logs `Decision model unavailable (...)` instead of `JEV classifier unavailable`. The five failure-class strings stay. They are this app's labels, not a vendor name.

Do not add a function whose name contains `jev` for the rerank. `classify_unknown` may keep its name. Callers of the map use `post_decision` only.

**Line count:** about +80, −30.

### 2.2 `pyproject.toml`

Add:

```text
"tree-sitter>=0.23",
"tree-sitter-python>=0.23",
```

No `networkx`. The rank is a short power iteration in `repo_map.py`.

### 2.3 `agent/repo_map.py` — new

Public:

```python
def build_repo_map(project_path: str, goal: str) -> str:
    """Signature block for ``goal``, or "" if there is nothing to show.

    Never raises. A missing grammar, an empty tree, or a decision-model
    failure still returns the graph's own top signatures, or "" when
    the tree has no Python definitions.
    """
```

**Walk.** `os.walk` from `project_path`. Prune directories the way `build_file_index` does: names starting with `.`, plus `__pycache__`, `node_modules`, `.pytest_cache`, `.mypy_cache`, `.tox`. Skip a file or directory for which `_is_ignored(rel_path, project_path, patterns)` is true, using `_load_gitignore_patterns` from `agent.context`. Skip files larger than 1_000_000 bytes. v1 parses only paths ending in `.py`.

**Extract.** Load `tree_sitter_python.language()` once. On `ImportError` or any exception during language load, log once and return `""` from `build_repo_map`.

Query the tree with these patterns. A unit test on the fixture below is the check that the installed grammar uses these node names. If the test fails, fix the query to the grammar's names. Do not ship a query the test does not pass.

```
(function_definition name: (identifier) @def)
(class_definition name: (identifier) @def)
(call function: (identifier) @ref)
(call function: (attribute attribute: (identifier) @ref))
```

A definition records `(rel_path, name, line_text)` where `line_text` is the source line of the node, stripped, cut at 200 characters. A reference records `(rel_path, name)`.

Cache per absolute path: `(mtime, defs, refs)`. Reuse the cache entry when `os.stat` `st_mtime` matches. The cache is a module dict. Cap it at 20_000 paths by clearing it when it exceeds that, the same blunt cap `_GITIGNORE_MATCHERS` uses a size limit for.

**Rank.** Nodes are `f"{rel_path}:{name}"` for definitions. An edge runs from the file that contains a reference to the node whose `name` matches, when exactly one definition in the project has that name. Ambiguous names (two defs) get no edge. Self-edges are not added.

Personalized PageRank, 20 iterations, damping 0.85:

```python
out_degree[src] = number of edges leaving src
personal[node] = 10.0 if the node's name is in goal_names else 1.0
normalize personal to sum to 1
score starts as personal
repeat 20 times:
    new[node] = (1 - 0.85) * personal[node]
    for each edge src -> dst:
        new[dst] += 0.85 * score[src] / out_degree[src]
    score = new
```

`goal_names` is the set of regex matches of `[A-Za-z_][A-Za-z0-9_]*` in `goal` whose length is at least 3. Nodes with no incoming edge and personal weight 1 still receive the teleport term, so an unmentioned symbol with no refs ranks below a mentioned one.

Take the 30 highest scores. Ties break by node id, ascending, so the order is stable.

**Rerank.** Build `state` as the goal sliced to 2_000 characters, then one line per candidate: `c0 {rel_path}:{name}`. Questions, one per candidate, keys `c0` … `c29`:

```python
{
    "type": "noul",
    "instructions": (
        "The state is a coding task and a list of symbols. "
        "Answer yes if this symbol is needed to carry out the task."
    ),
}
```

The instructions string is identical for every question. The symbol each question refers to is the matching line in `state` (`c0`, `c1`, …). One `post_decision(state, questions)` call.

When `answers` is `None`, keep the graph order.

When `answers` is present, each candidate gets a sort key:

- Confident: `answer.get("type") == "noul"`, `noul` is an int or float, `confidence` is an int or float and `>= 0.6`. Key `(0, -float(noul), original_index)`.
- Anything else, including a missing key: `(1, original_index)`.

Sort by that key. Confident symbols come first, highest probability first. The rest stay in graph order under them. A low confidence does not delete a symbol. The token budget does.

**Render.** Walk the sorted candidates. For each, emit:

```text
{rel_path}
  {line_text}
```

Stop before adding a symbol that would push `len(text) // 4` over 2_000. Always include the first symbol if the list is non-empty, even if that one line exceeds 2_000 (it cannot: the line is cut at 200). Return the string. Empty candidate list returns `""`.

`build_repo_map` calls `post_decision`. It does not read `providers.yaml` itself.

**Line count:** about +280.

### 2.4 `agent/runtime.py` — inject on the wire

On the tool-loop thread, immediately before the line that assigns `messages` (line 1848, or SPEC-24's compiled list when that spec has landed):

- Goal text is the last `MessageRole.USER` content, or `""` if there is none.
- If `conv.project_path` is empty or the goal is empty, the map block is `""`.
- Otherwise call `build_repo_map(conv.project_path, goal)` only when the session's stored `(project_path, hash(goal))` differs from the last call. Store the returned string on `self._repo_map_block[session_key]`.
- Pop `_repo_map_block[session_key]` inside `_cleanup_tool_history` is wrong for this: the next user message in the same session has a new goal and must recompute, but a tool iteration must reuse the string. Do not pop it per tool call. Pop it in `clear_conversation` via `AgentRuntime.drop_repo_map(session_key)`, next to SPEC-24's `drop_compile` if that method exists, or beside the cache invalidation in `ui/agent_runtime/session.py`. A missing key is a no-op.

Injection, wire copy only. Do not assign `conv.system_prompt` and do not edit `conv.messages`.

- If a SPEC-24 compile packet is being built, put `## Repo map\n` + block above the fact lines, when the block is non-empty.
- Otherwise, after `messages = conv.to_api_messages()`, find the last dict with `role == "user"` and replace that dict with a new dict whose content is `block + "\n\n" + original` when the block is non-empty.

`build_repo_map` returning `""` leaves the wire unchanged.

The call sits on the tool-loop thread. `create_conversation` and `compose_system_prompt` do not call it.

**Line count:** about +45.

### 2.5 Settings copy

In `ui/views/settings_dialog.py` the Decision Model intro (the string that begins `The Decision Model (JEV)`), replace the sentence that says JEV features degrade to the deterministic floor with: the failure classifier and the repo-map reranker both use this card, and either one falls back to its non-network behavior when the card is missing or the call fails. Do not say the card is JEV-only. Keep the fixed name `DecisionModel`.

**Line count:** about +5.

### Files NOT changed

- `utils/prompt_loader.py` — the map is not part of the system prompt built at conversation create.
- `models/providers.py` — no new field. Host and model are the existing card fields.
- `agent/enforcement.py` — the map does not read checks.
- SPEC-23's file index — `build_file_index` stays for jit/hybrid until someone removes it in a later spec. This spec does not delete it.

---

## 3. Data flow

The user message is on `conv.messages`. The tool loop, on its background thread, hashes that content. On a miss, `build_repo_map` walks `.py` files, extracts defs and refs, ranks 30 nodes, and calls `post_decision`. `post_decision` loads the card named `DecisionModel`. It POSTs to that card's `base_url` plus `/v1/systemone` with that card's `default_model`. The answers reorder the 30. The rendered block is stored on the session.

`to_api_messages()` runs as today. The runtime then prefixes the block onto the last user dict in the list it is about to send. `conv.messages` and `conv.system_prompt` are unchanged. The next tool iteration sees the same goal hash and does not call the decision model again.

A new user message changes the hash, so the next iteration recomputes. `clear_conversation` drops the stored block.

If `post_decision` returns `(None, error)`, the 30 stay in graph order and the turn continues. `_outage` logs once.

---

## 4. File change summary

| File | Change | Risk |
|---|---|---|
| `agent/llm/jev_client.py` | `post_decision`; drop `jev-latest` fallback | Medium — failure classifier and probe share it |
| `agent/repo_map.py` | Graph, rank, rerank, render | Medium — first-turn walk cost |
| `agent/runtime.py` | Cache the block per goal; prefix the wire copy | Medium — prompt contents change |
| `pyproject.toml` | `tree-sitter`, `tree-sitter-python` | Low |
| `ui/views/settings_dialog.py` | Intro text | Low |
| `tests/test_failure_classifier.py` | Empty-model test; existing keyed-card test must stay green | Low |
| `tests/test_repo_map.py` | New | Low |

---

## 5. Implementation order

1. **`post_decision`.** Move the POST out of `classify_unknown`. Run `tests/test_failure_classifier.py::TestDecisionModelLookup::test_keyed_card_classifies_via_mock_endpoint`. It must still see `body["model"] == "typesafe/system-one"` and a URL ending in `/v1/systemone`.
2. **Empty model.** A card with `default_model=""` must not call `urlopen`. `classify_unknown` returns `None`. The probe's `TestResult.ok` is false and `error` contains `default_model`.
3. **Graph without network.** Fixture: `a.py` contains `def foo():\n    bar()\n`, `b.py` contains `def bar():\n    pass\n`. Goal `fix bar`. `bar` ranks above `foo`. Monkeypatch `post_decision` to return `(None, "down")`. The rendered text lists `bar` before `foo`.
4. **Rerank.** Same fixture. Monkeypatch `post_decision` so `c` for `foo` has `type=noul`, `noul=0.95`, `confidence=0.9`, and `bar` has `noul=0.1`, `confidence=0.9`. Rendered text lists `foo` first. Assert the POST body `model` equals the card's `default_model` and the URL ends with `/v1/systemone`.
5. **Low confidence.** `confidence=0.2` on `foo` leaves `foo` in graph order, not deleted, and not moved above a confident higher score.
6. **Wire.** A conversation whose system prompt is `SYS` and whose last user message is `goal` sends `goal` prefixed with the map, and `conv.system_prompt` is still `SYS`.
7. **Second iteration.** Two loop iterations with the same goal call `build_repo_map` once.

---

## 6. Acceptance criteria

- [ ] No new source file contains the string `jev-latest`.
- [ ] `classify_unknown` with a card whose `default_model` is `typesafe/system-one` sends that model. With `default_model=""` it does not open a socket and returns `None`.
- [ ] The repo-map POST uses `load_decision_card()`, that card's `base_url`, `api_key`, and `default_model`, and the path `/v1/systemone`.
- [ ] A decision-model failure still returns a signature block in graph order.
- [ ] A confident `noul` reorders the top 30. A confidence below 0.6 does not.
- [ ] The rendered block is at most 2_000 tokens by `len // 4`, and it is not written into `conv.system_prompt`.
- [ ] Two tool iterations with the same user goal issue one `post_decision` call.
- [ ] A project with no `.py` files yields `""` and the wire user message is unchanged.
- [ ] `tests/test_failure_classifier.py` and `tests/test_repo_map.py` pass.

---

## 7. Edge cases

| Case | Expected behavior |
|---|---|
| No Decision Model card | `post_decision` returns `(None, ...)`. Map is graph order. No log spam beyond `_outage` once. |
| Card saved, then the user changes `default_model` in Settings | Next goal hash miss loads the card again via `load_providers`. The new slug is sent. No restart required beyond the next map build. |
| `base_url` already ends in `/v1` | Concatenation stays `rstrip("/") + "/v1/systemone"`, matching today's `classify_unknown`. Do not strip a trailing `/v1`. |
| `post_decision` answers a question with `type: choice` | That symbol is unscored and stays in the graph section of the sort. |
| `noul` is a string | Unscored. |
| Two defs share a name | No edge for that name. Both defs can still be nodes and can still match `goal_names`. |
| File changes on disk mid-session | The mtime cache misses and that file is reparsed on the next goal change. A tool iteration with the same goal does not rescan. |
| `tree-sitter` missing | `build_repo_map` returns `""`. The turn proceeds. |
| Goal has no identifiers of length ≥ 3 | Personalization is flat. Rank is ordinary PageRank. Rerank still runs if the card exists. |
| SPEC-24 compile is active | The map block is inserted by the runtime above the fact lines. This spec's "last user message" prefix is skipped when the compile list is used, so the map is not applied twice. |
| `project_path` is `None` | No walk, no POST. |

---

## 8. ARCHITECTURE.md updates required

- `agent/llm/jev_client.py` is the Decision Model transport for every caller. `post_decision(state, questions)` returns `(answers, error)`. Model and host come from the `DecisionModel` card. There is no baked-in model slug.
- `agent/repo_map.py` builds a Python def/ref graph, keeps 30 symbols, reranks them with `post_decision`, and renders at most 2,000 tokens. The runtime attaches that block to the wire user message for the current goal. It is not part of `conv.system_prompt`.

---

## 9. Self-audit

1. `post_decision` uses the same URL concatenation, headers, timeout, and JSON keys as `classify_unknown` lines 131–160. The model value is the card field the existing test already asserts (`typesafe/system-one`), with the empty-string fallback removed.
2. `load_decision_card` returns `None` and does not raise. Samples check for `None` before reading `base_url`.
3. `noul` is the field name from the decision API's yes/no answer. A `choice` answer is treated as unscored rather than parsed as a probability.
4. The map call is specified on the tool-loop thread at the `messages =` assignment, not inside `compose_system_prompt`, which runs at conversation create and would block that thread on the walk and the POST.
5. PageRank's update uses `score[src] / out_degree[src]` into the destination. The spec's loop does that. It does not split the destination's incoming list the other way.
6. An implementer who changes only the Decision Model card's `base_url` and `default_model` changes both failure classification and the reranker, and does not edit Python to do it.
