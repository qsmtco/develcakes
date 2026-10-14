# SPEC-31: Repo map v2 — dirty-first ranking, edit-anchored signatures, freshness, guidance preambles

**Date:** 2026-10-15
**Author:** Supervisor (PM direction 2026-10-15: "all dirty files in, recency next, map must refresh as we work; signatures must follow the edit site")
**Status:** Draft — for implementation
**Depends on:** SPEC-23 (context working set), SPEC-24 (compiled turn + facts board), SPEC-25 (repo map original), SPEC-30 (aggressive context tiers — masking pressure makes map freshness load-bearing)
**Target branch:** main

> Architecture compliance: `agent/repo_map.py` owns signature extraction (ARCHITECTURE.md §context subsystem). This spec keeps repo_map pure-Python + tree-sitter local, never raises, and adds no LLM calls. Git metadata access is read-only subprocess. Cache invalidation hooks live in `agent/runtime.py` per the existing `_repo_map_block_for` pattern.

---

## 1. Overview

### Problem

The repo map injected into agent prompts ranks by bag-of-words PageRank seeded from the user message. Observed failure (2026-10-15 paste): of 29 entries, 11 are test-file mock stubs (`def open`, `def set`, `class Thread`, `Label(*args, **kwargs)`) that win on name collisions with message words, while the 3 files actively being edited in that very session were absent or misrepresented (`agent/runtime.py` shown as an exception's `__new__` while the real work sat ~3,000 lines away). The map is cached per user message (`(session_key, project, hash(goal))`) and never refreshed mid-turn, so edits during a long turn leave the map pointing at the pre-edit world.

Under SPEC-30's aggressive masking, this matters more: masked tool results push agents to re-read files, and the map is the only always-present, never-masked orientation signal. A stale or noise-ranked map trains agents to ignore it — 2,000 tokens billed per call for negative value.

### Solution

Four changes, one round:

1. **Dirty-first ranking** — every dirty file (git status) enters the map, ranked above clean files. No cap (PM ruling: dirty files ARE the work).
2. **Recency bonus** — files touched in recent commits rank above stale files among the clean entries.
3. **Edit-anchored signature selection** — for dirty files, the shown signature is the def whose span touches the diff hunks; for untracked files, symbol-match fallback; clean files keep bag-of-words PageRank as today.
4. **Freshness** — cache key gains a `tree_version` counter bumped on every successful write-tool call (`write_file`, `edit_file`), so the map rebuilds when files actually change mid-turn. Zero polling.

Plus **Task 2**: three one-line guidance preambles (map header, facts fence, compiled packet) so agents know how to treat injected blocks — this session demonstrated agents misreading them as noise or instructions (Supervisor audit 2026-10-14; two Audit Reports filed: duplicate-injection fixed in `21b0c60f`, unlabeled-section).

### Scope

| In | Out |
|---|---|
| `agent/repo_map.py` ranking + anchoring | No LLM calls, no decision model (removed 2026-10-12 for blocking every message) |
| `agent/runtime.py` cache key + tree_version bump | No changes to `_walk` parse format (line data already present) |
| Guidance preamble strings (3 sites) | No UI changes |
| Tests for ranking/anchoring/invalidation/preambles | No battery beyond Tier-1 (new-loop rules) |

---

## 2. Changes by File

### 2.1 `agent/repo_map.py`

**New constants (near `_TOP_N` at :19):**

```python
_RECENCY_COMMITS = 50        # commits considered "recent" for the recency bonus
_RECENCY_WEIGHT = 3.0        # personalization weight for recently-committed files
_STUB_PENALTY = 0.1          # multiplier for mock/stub defs in test files
```

**New helper `_git_metadata(project_path)` (place after `_language()`):**

```python
def _git_metadata(project_path: str) -> tuple[set[str], set[str]]:
    """(dirty_paths, recent_paths) relative POSIX paths. Read-only git.
    Never raises; returns (set(), set()) if git is absent or the tree is clean."""
    dirty: set[str] = set()
    recent: set[str] = set()
    try:
        status = subprocess.run(
            ["git", "-C", project_path, "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        for row in status.stdout.splitlines():
            path = row[3:].strip().strip('"').rsplit(" -> ", 1)[-1]
            if path.endswith(".py"):
                dirty.add(path)
        log = subprocess.run(
            ["git", "-C", project_path, "log", f"-{_RECENCY_COMMITS}", "--name-only", "--pretty=format:"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        for path in log.stdout.splitlines():
            path = path.strip()
            if path.endswith(".py"):
                recent.add(path)
    except (OSError, subprocess.SubprocessError):
        pass
    return dirty, recent
```

Verified against source: `subprocess` import needed at top of `repo_map.py` (currently absent — `os`, `math`, `re`, `logging` only). `row[3:]` handles both `XY path` (2-char status + space) and `XY ORIG -> NEW` rename forms; `.rsplit(" -> ", 1)[-1]` takes the post-rename path.

**New helper `_diff_anchors(project_path, dirty_paths)` returning `{rel_path: [line_numbers]}`:**

```python
def _diff_anchors(project_path: str, dirty_paths: set[str]) -> dict[str, list[int]]:
    """Changed line numbers per dirty file, from `git diff --unified=0`.
    Untracked files get no hunks (absent from the dict). Never raises."""
    anchors: dict[str, list[int]] = {}
    if not dirty_paths:
        return anchors
    try:
        diff = subprocess.run(
            ["git", "-C", project_path, "diff", "--unified=0", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        current: str | None = None
        for line in diff.stdout.splitlines():
            if line.startswith("+++ b/"):
                current = line[6:]
                anchors.setdefault(current, [])
            elif line.startswith("@@") and current is not None:
                m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                if m:
                    anchors[current].append(int(m.group(1)))
    except (OSError, subprocess.SubprocessError):
        pass
    return anchors
```

Verified: `re` is already imported in `repo_map.py`. The `@@` regex matches `git diff` hunk headers (`@@ -12,0 +13,4 @@`); `+13` is the new-file start line. `--unified=0` emits per-hunk headers only.

**Edit `_rank(defs, refs, goal)` → `_rank(defs, refs, goal, git_meta=None, anchors=None)`:**

Backward-compatible signature (existing callers/tests pass positionally today). Changes inside:

1. Personalization: goal-name match stays `10.0`. Add: `if nodes[node][0] (the rel path) in dirty: personal[node] = max(personal[node], 50.0)` — every def in every dirty file gets boosted, guaranteeing dirty files dominate the top (PM: no cap). 
2. `elif rel in recent: personal[node] = max(personal[node], _RECENCY_WEIGHT)` — recency among the clean.
3. Stub demotion: after scoring, `if rel.startswith("tests/") and _looks_like_stub(name): score[node] *= _STUB_PENALTY`.

**New helper `_looks_like_stub(name: str) -> bool`:**

```python
_STUB_NAMES = {"open", "set", "get", "emit", "wait", "join", "start", "stop",
               "set_markup", "set_child", "set_label", "get_next_sibling",
               "get_text", "show_all", "queue_draw", "Thread", "Label", "Box", "Mock"}
def _looks_like_stub(name: str) -> bool:
    return name in _STUB_NAMES
```

Rationale: these exact names dominated the failure paste (11/29 entries). The check is name-based (defs carry only `(rel, name, line)` — no body text), applied only under `tests/`, so product code named `start()` (e.g. `utils/stt.py`) is untouched.

**Edit `_render(ranked)` → `_render(ranked, anchors=None, dirty=None)`:**

For each entry whose `rel` has anchors: replace the shown signature line with the def **nearest the smallest-anchor distance** — distance = `min(abs(def_line - hunk_line) for hunk_line in anchors[rel])`, preferring defs whose span contains a hunk line (a def contains a hunk when `def_line <= hunk_line <= def_line + estimated span`; estimate span via the next def's line in the same file — sort that file's defs by line, span = next_line - def_line, last def = 200). Tie-break by distance then name. If a file has anchors but no def within 300 lines of any hunk, keep the ranked signature (fallback, never blank).

**Edit `build_repo_map(project_path, goal)`:**

```python
dirty, recent = _git_metadata(project_path)          # before _walk (early-out if git slow: both empty)
anchors = _diff_anchors(project_path, dirty)
ranked = _rank(defs, refs, goal, git_meta=(dirty, recent), anchors=anchors)
return _render(ranked, anchors=anchors, dirty=dirty)
```

Docstring gains: "Dirty files rank first (no cap); signature anchored to diff hunks when available. Untracked files fall back to symbol match."

### 2.2 `agent/runtime.py`

**Tree-version counter** (instance attr in `__init__`, near other caches):

```python
self._repo_map_tree_version = 0
```

**Bump site:** in the write-tool success path — the same place `record_fact` fires post-write (the enforced-write hook in the tool dispatcher; grep `record_fact(` in `agent/runtime.py` for the exact site). Add one line after the record:

```python
self._repo_map_tree_version += 1
```

**Cache key change** at `:3450` (inside `_repo_map_block_for`):

```python
key = (session_key, project, hash(goal), self._repo_map_tree_version)
```

(Rule 5 verification: the current key is a 3-tuple `(session_key, project, hash(goal))`; appending the counter keeps prior fields positional — old keys simply never match, one stale entry max, LRU bounded as today.)

**Preambles (Task 2):**

- `REPO_MAP_HEADER` (:386): append two sentences — `"Entries may be stale — confirm with git/grep before acting. Product code ranks above test stubs; signatures are anchored to current edits."` (Keep the existing "not a task / not an error" sentence intact — verified present in the current paste.)
- PROJECT_MEMORY facts fence (`utils/project_awareness.py:1256` region): prepend one line inside the fenced blob, before fact rows — `"Live facts, sha-scoped, self-invalidate when files change — verify before acting."`
- Compiled packet (`agent/runtime.py` `_compiled_messages`, before the `## Write` heading): one line — `"Replay of the last enforced write and its checks — already executed, do not redo."`

### 2.3 Tests — new file `tests/test_repo_map_v2.py`

All follow existing conventions (see `tests/test_spec24_compiled_turn.py` for `_make_cfg`-style helpers; repo_map tests use direct calls with `tmp_path` repos — no GTK needed, no xvfb):

1. `test_dirty_file_ranks_first` — tmp git repo (init, commit a clean file, dirty a second file); assert the dirty file's entry is the first non-header line and present.
2. `test_edit_anchored_signature_selection` — dirty file with defs A (:10) and B (:100); hunk at :100; assert B is the shown signature, not A.
3. `test_untracked_falls_back_to_symbol_match` — untracked file with a def whose name appears in `goal`; assert entry present (no anchors exist for it).
4. `test_clean_file_stub_demoted` — `tests/test_fake.py` defining `def open(self)`; assert it ranks below product entries or is absent under TOP_N pressure.
5. `test_tree_version_invalidates_cache` — build block, bump counter (simulate write), rebuild; assert map reflects the new dirty file. Requires exposing the counter on the runtime or testing `_repo_map_block_for` via a runtime instance (follow `test_spec24_compiled_turn.py:133` scaffolding: `_make_cfg`, `_conv`, call `runtime._repo_map_block_for` twice).
6. `test_preambles_present` — assert the three new strings appear in: `REPO_MAP_HEADER`, a built PROJECT_MEMORY fence blob (`utils/project_awareness.build_parts` or direct fence call), and a compiled packet body (`_compiled_messages` output containing the Write-replay line).
7. `test_git_absent_never_raises` — `_git_metadata("/nonexistent")` → `({set()}, {set()})` and `build_repo_map` returns `""` or today's behavior. (Rule 4: `subprocess.run(check=False)` + `except (OSError, subprocess.SubprocessError)` covers `FileNotFoundError` for a missing git binary and `TimeoutExpired`.)

### 2.4 Files NOT changed (Rule 8)

- `agent/context.py` — `_is_ignored`/`_load_gitignore_patterns` reused as-is by `_walk`.
- `agent/llm/jev_client.py` — no JEV in the map; SPEC-30 owns `classify_mask_candidates`.
- `agent/context_strategy.py` — masking/pruning untouched; this spec only feeds better prefixes.
- `ui/` — no UI surface changes.
- `models/providers.py`, `utils/providers_store.py` — ceiling work was SPEC-30, already landed.

---

## 3. Data Flow

```
user message
  → runtime._repo_map_block_for(session_key, conv)
      cache key = (session_key, project, hash(goal), tree_version)      [CHANGED: +tree_version]
      on miss → build_repo_map(project, goal)
          _git_metadata  → (dirty: set[rel], recent: set[rel])           [NEW: 2 subprocess calls, 5s timeout each]
          _diff_anchors  → {rel: [hunk_lines]}                           [NEW: 1 subprocess call]
          _walk          → defs [(rel, name, line)], refs                [UNCHANGED]
          _rank          → personalization: goal-name 10.0, dirty 50.0,  [CHANGED]
                           recent 3.0; stub penalty in tests/ ×0.1
          _render        → per dirty file, anchor-nearest def selected    [CHANGED]
  → REPO_MAP_HEADER (now with 2 guidance sentences) + block               [CHANGED]
  → injected at :3478 (message prefix) and :3488 (compiled packet)

write-tool success (any write_file/edit_file that lands)
  → record_fact(...) as today
  → self._repo_map_tree_version += 1                                     [NEW]
  → next _repo_map_block_for call misses cache → rebuild with new dirty set + anchors
```

Latency budget: 3 git subprocesses (~5–20ms each warm) + `_walk` (mtime-cached parse) + PageRank (already runs per message today). No new blocking LLM call anywhere.

## 4. File Change Summary

| File | Change | Est. lines | Risk |
|---|---|---|---|
| `agent/repo_map.py` | git metadata, diff anchors, ranking boost, stub penalty, anchored render | +120 | Medium (ranking math) |
| `agent/runtime.py` | tree_version attr + bump + cache key + Write-replay preamble | +8 | Low |
| `agent/runtime.py` `REPO_MAP_HEADER` | 2 sentences | +2 | Low |
| `utils/project_awareness.py` | 1 preamble line in fence | +3 | Low |
| `tests/test_repo_map_v2.py` | 7 tests | +180 | — |

## 5. Implementation Order

1. `repo_map.py`: `_git_metadata` + `_diff_anchors` + `_looks_like_stub` (pure functions, testable standalone).
2. `_rank`/`_render`/`build_repo_map` signature extensions (backward-compatible defaults).
3. `runtime.py`: counter + bump + cache key.
4. Preambles (3 strings).
5. Tests file (test design first per new loop — Coder writes, Debugger executes Tier-1).

## 6. Acceptance Criteria

- [ ] Dirty files all present in map, ranked above clean files, no cap
- [ ] Shown signature for a dirty file is the def nearest a diff hunk
- [ ] Untracked files appear via symbol match (no hunk anchors)
- [ ] `tests/` stub names (`open`, `set`, `Thread`, `Label`…) demoted or absent
- [ ] Map rebuilds after a write-tool success (tree_version bump)
- [ ] Three preambles present at their sites
- [ ] `build_repo_map` never raises (git absent, clean tree, empty repo)
- [ ] Tier-1 tests green; no battery required

## 7. Edge Cases

| Case | Behavior |
|---|---|
| Git absent / not a repo | Empty sets → ranking falls back to today's PageRank; never raises |
| Rename in status (`R  old -> new`) | `rsplit(" -> ", 1)[-1]` takes new path; both dirty |
| Hunk before first def | Nearest-def distance resolves to first def |
| File with defs but hunk in comment region | Distance fallback keeps ranked signature if >300 lines from any def |
| Monster merge dirties >30 files | All enter (no cap); clean entries squeezed out — acceptable (dirty IS the work) |
| Untracked file | In `dirty` via `--untracked-files=all` but no diff hunks → symbol-match fallback |
| Same def name in dirty and clean file | Dirty file's def outranks via 50.0 personalization |
| `git status` timeout (5s) | Empty dirty set; today's ranking; log at debug |
| Concurrent write during `_walk` | mtime cache catches it next build; tree_version forces the rebuild |

## 8. ARCHITECTURE.md Updates

- Context subsystem section: repo map v2 behavior (dirty-first, edit-anchored, tree-version freshness), replacing the "graph order only / decision-model removed" note at `repo_map.py:247-249`.
- Prompt-injection surfaces table: the three guidance preambles.

## 9. Self-Audit (Rule 9)

1. Code samples traced: `row[3:]` against real `git status --porcelain` output format ✓; `@@` regex against `git diff --unified=0` headers ✓; cache key structure read at `runtime.py:3450` ✓; def records carry `line` (verified `_parse_file` → `node.start_point[0]` at `:124`) ✓.
2. Exceptions: `subprocess.run(check=False)` never raises on exit code; `TimeoutExpired` and `OSError` caught; `build_repo_map`'s existing blanket `except Exception` retained ✓.
3. Key structures: verified actual 3-tuple key, appended counter keeps positional compatibility ✓.
4. Data flow traced end-to-end user message → cache → build → render → both injection sites ✓.
5. Implementer following this exactly produces working code — all signatures verified against source at `agent/repo_map.py:19,88,124,135,170,214,226` and `agent/runtime.py:386,3443,3450,3457-3460,3478,3488` ✓.

One deliberate simplification documented: multi-hunk edits anchor to the most-covered/nearest def — a single representative signature per file, not per hunk.
