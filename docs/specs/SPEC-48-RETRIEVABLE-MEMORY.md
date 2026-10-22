# SPEC-48: Retrievable Memory — Offload-Then-Stub + Typed Digest

**Date:** 2026-10-21
**Author:** Supervisor (Lieutenant Qrusher)
**Status:** Draft — for implementation
**Implements:** `docs/proposals/PROPOSAL-META.md` §3 (Program 2 — Retrievable memory)
**Also implements:** T1.2 (typed digest) + T1.3 (tool-output offloading) of `docs/proposals/PROPOSAL-context-management-phase-2.md` §3.2–§3.4
**Depends on:** SPEC-47 (acceptance gate — this spec registers rows R-8..R-10), SPEC-34/SPEC-30 (compaction layers), SPEC-19 (live chat surface)
**Target branch:** main
**Base at time of writing:** `a69dd830` (tree clean)

> **Architecture compliance.** This spec changes `agent/context_strategy.py` (ARCHITECTURE.md §3.21p.5), `agent/tools.py` (§3.21n), `agent/config.py` (§3.21o), `models/conversation.py` (§3.21l), `utils/providers_store.py` (§3.21o sibling), and `ui/views/settings_dialog.py`. Layering rules preserved: `models/` stays pure data with no I/O and stdlib-only imports; `agent/` gains no GTK imports; `utils/` gains no GTK imports. The new on-disk store lives in `utils/tool_output_store.py` (new module, stdlib only, no GTK, no network) so that `agent/context_strategy.py` does not own file I/O it did not own before.

---

## 1. Overview

### 1.1 Problem statement

Program 1 (the acceptance gate) is shipped. Program 2 is the next in `PROPOSAL-META.md` §1's order. Its hole, stated verbatim by the proposal:

> `prune_tool_outputs` replaces an old tool result with `[compacted — {tool} {N}]`. The method comment calls this lossless. The bytes are not kept. An exec can be run again (SPEC-33). Every other tool result is gone, and the summary that replaces trimmed turns is one free-text string.

Two independent failures, both of which this spec closes:

**Failure A — tool output is lossy.** When a tool result crosses the size threshold, `DefaultContextStrategy.prune_tool_outputs` (`agent/context_strategy.py:359`) and `DefaultContextStrategy.panic_prune` (`:485`) replace `msg.content` with the one-line stub from `_compacted_stub` (`:477`). The bytes are gone. A 50 KB `exec_command` result — a test run, a `search_files` sweep, a `web_fetch` — cannot be recovered without re-running the tool, and re-running a mutating tool is not always safe or possible. The stub is *not* lossless; the docstring at `:369` calls it "Cheap lossless Layer 1 compaction", which is false.

**Failure B — the summary is free text.** When the trim loop removes messages, `compact()` injects a summary `Message` (`is_summary=True`, `agent/context_strategy.py:310`). The textual `_summary` (`:855`) produces `"Conversation so far (N prior turns): 1. … 2. …"` — a preview of the first five user messages. The LLM variant `LLMSummarizeStrategy._summary` (`:981`) produces nine `<tag>` sections, which is better but still prose. Neither exposes *decisions*, *constraints*, *open questions*, *paths touched*, or *attempts rejected* as queryable fields. The agent cannot ask "what did we decide about auth?" without re-reading prose and hoping.

### 1.2 Solution summary

1. **Offload, then stub (Failure A).** When a tool result exceeds a configurable size threshold, write its full text to a per-project store under `<project>/.crabcakes/tool-outputs/`, then put a *stub with a path* into the conversation. The agent reads it back by path with one new read-only tool. The bytes survive; the conversation stays small.

2. **Typed digest (Failure B).** When compaction injects a summary, also compute a structured digest — `arc`, `decisions`, `constraints`, `open_questions`, `paths_touched`, `attempts_rejected` — and persist it alongside the summary so it survives a restart and is machine-queryable.

3. **Both thresholds are configuration, not magic numbers.** The offload size threshold has a default *and* a persisted override, so it is not the same mistake as a hardcoded 0.80. The **compaction trigger threshold is not touched by this spec** — it already resolves per-provider from `LLMProviderConfig.compaction_threshold` via `AgentRuntime._compute_compaction_threshold` (`agent/runtime.py:1699`), and the Settings dialog already exposes it (`ui/views/settings_dialog.py:159`). That existing chain stays exactly as it is.

### 1.3 Scope

| In scope | Out of scope (explicitly not built here) |
|---|---|
| Offload of oversized tool results to `<project>/.crabcakes/tool-outputs/` | T1.1 hierarchical summary-of-summaries (deferred by PROPOSAL-META §3) |
| A path-carrying stub in the conversation | T1.5 per-tool retention tables (deferred) |
| One new read-only tool to read an offloaded output back | Changing the compaction *trigger* threshold (already configurable) |
| Typed digest fields for the injected summary | T1.1-style multi-layer digests |
| Configurable offload size threshold (global, with per-provider override possible later) | `README`/KB ingestion of the store |
| Acceptance rows R-8, R-9, R-10 | Changing `read_file` semantics |
| | Live-section / WebKit rendering of the digest |

### 1.4 Architecture principles that apply

- **The strategy owns the conversation, not the filesystem.** `ContextStrategy.compact` is documented (ARCHITECTURE.md §3.21p.5, and the `ContextStrategy` Protocol docstring at `agent/context_strategy.py:76-82`) as mutating "only `conv.messages` and `conv._token_estimate_cache`". Writing files from inside `compact()` would break that contract. The offload therefore happens through a **callback** the runtime supplies, and the strategy only ever mutates the conversation. This is the single most important design decision in this spec.
- **Stdlib only.** No new third-party dependency. `hashlib`, `json`, `os`, `pathlib`, `re`, `shutil` — all stdlib.
- **No GTK below `ui/`.** The new store module and the new tool are GTK-free.
- **`.crabcakes/` is the project-local state directory.** It is gitignored (`.gitignore:37`) and is already the home of `context.md`, `feed.json`, `prompts/`, `awareness.json`.

---

## 2. Changes by file

### 2.1 `utils/tool_output_store.py` — NEW

**What changes:** a new module owning the on-disk store of offloaded tool outputs.

**Manifest (follow the house style of `utils/feed_store.py` and `utils/project_awareness.py`):**
```
# utils/tool_output_store.py
# Offloaded tool-output store (SPEC-48).
#
# Manifest:
#   - Reads: <project>/.crabcakes/tool-outputs/*.txt
#   - Writes: <project>/.crabcakes/tool-outputs/*.txt
#   - Network: none
#   - Imports: stdlib only (hashlib, json, os, re, shutil)
#   - No GTK, no LLM calls
```

**Public API (exact signatures):**

```python
def tool_outputs_dir(project_path: str) -> str:
    """Absolute path of <project>/.crabcakes/tool-outputs (not created)."""


def offload_tool_output(
    project_path: str,
    tool_name: str,
    content: str,
) -> dict | None:
    """Write ``content`` to the store; return its metadata, or None on failure.

    Returns a dict with keys:
        path      (str)  — project-relative path, e.g.
                           ".crabcakes/tool-outputs/exec_command-ab12cd34.txt"
        bytes     (int)  — size of the written file
        lines     (int)  — number of lines written
        preview   (str)  — first PREVIEW_CHARS chars of the content
    Returns None when the content could not be written (the caller then
    falls back to the plain lossy stub).
    """
```


**The read must be line-aware, not body-truncated (Debugger BUGs #1/#2, fix-round 2026-10-21).** The Phase-1 shape — read the first `max_bytes`, then window the truncated body — makes every byte past 16 KB unreachable *silently*: the continuation check compares against the already-truncated body, so it never fires, and a `line` past the truncation point returns `success=True` with an empty body, indistinguishable from an empty file. The store therefore owns line-windowed reads:

```python
def read_tool_output(
    project_path: str,
    rel_path: str,
    *,
    line: int = 1,
    max_lines: int = 200,
    max_bytes: int = 16384,
) -> dict:
    """Read a line window of an offloaded output back.

    Returns a dict with keys:
        found    (bool)  — the file exists and is inside the store
        output   (str)   — the windowed, unnumbered body
        line     (int)   — the first line returned (echoes the request)
        lines    (int)   — number of lines returned
        total_lines (int) — total lines in the file
        truncated (bool) — the window did not reach the end of the file
        bytes    (int)   — total size of the file on disk
        error    (str)   — non-empty when the read failed
    """
    store = _store_realdir_inside_project(project_path)
    if store is None:
        return _miss(f"Store dir escapes the project: {rel_path}")
    if not _is_store_path(rel_path):
        return _miss(f"Not a store path: {rel_path}")
    name = rel_path.replace("\\", "/").split("/")[-1]
    target = os.path.realpath(os.path.join(store, name))
    if os.path.dirname(target) != store:
        return _miss(f"Path escapes the store: {rel_path}")
    if not os.path.isfile(target):
        return _miss(f"Not a file in the store: {rel_path}")
    try:
        with open(target, "rb") as fh:
            raw = fh.read()          # store files are offloaded bodies:
                                     # written once by us, capped by the
                                     # offload threshold — reading whole is
                                     # bounded and keeps line math exact
        size = len(raw)
        text = raw.decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        total = len(all_lines)
        if total == 0:
            return {"found": True, "output": "", "line": line, "lines": 0,
                    "total_lines": 0, "truncated": False, "bytes": size, "error": ""}
        if line < 1 or line > total:
            return _miss(f"line {line} is past the end of {rel_path} ({total} lines)")
        window = min(max_lines, 200) if max_lines and max_lines > 0 else 200
        got = all_lines[line - 1: line - 1 + window]
        return {"found": True, "output": "\n".join(got), "line": line,
                "lines": len(got), "total_lines": total,
                "truncated": line - 1 + len(got) < total, "bytes": size, "error": ""}
    except OSError as exc:
        return _miss(f"Cannot read {rel_path}: {exc}")


def _miss(error: str) -> dict:
    return {"found": False, "output": "", "line": 0, "lines": 0,
            "total_lines": 0, "truncated": False, "bytes": 0, "error": error}
```

**Why reading the file whole is correct here (and not in the tool).** `read_file`'s 16 KB cap exists because it reads arbitrary project files — a 50 MB file must not enter memory. A store file's size is bounded by the offload path itself: it was written once by `offload_tool_output`, from a body the runtime chose to offload. If offloading a 50 KB body is acceptable, reading that same body back whole is the same order of cost — and it makes `total_lines` exact, so past-the-end and continuation are precise. The *tool* layer still windows by lines (200 max), so the conversation never receives the blob.

**The tool body becomes:**

```python
def _read_tool_output(
    path: str,
    project_path: str,
    line: int | None = None,
    max_lines: int | None = None,
) -> ToolResult:
    """Read an offloaded tool output, windowed exactly like read_file.

    Past-the-end starts error (mirroring read_file, agent/tools.py:533-537);
    continuations come from total_lines, not a truncated body.
    """
    from utils.tool_output_store import read_tool_output

    if not project_path:
        return ToolResult(success=False, error="No active project for this conversation")
    start = 1 if line is None else line
    if isinstance(start, bool) or not isinstance(start, int) or start < 1:
        return ToolResult(success=False, error="line must be an integer >= 1")
    window = LINE_WINDOW
    if max_lines is not None:
        if isinstance(max_lines, bool) or not isinstance(max_lines, int) or max_lines < 1:
            return ToolResult(success=False, error="max_lines must be an integer >= 1")
        window = min(max_lines, LINE_WINDOW)
    result = read_tool_output(project_path, path, line=start, max_lines=window)
    if not result.get("found"):
        return ToolResult(success=False, error=result.get("error") or f"Not an offloaded output: {path}")
    if result.get("total_lines") == 0:
        return ToolResult(success=True, output=f"[line {start} of {path}]\n(empty)")
    numbered = _numbered_line_block(result["output"].splitlines(), start)
    out = f"[line {start} of {path}]\n" + numbered
    if result.get("truncated"):
        out += f"\n[... truncated; continue with line={start + result['lines']} ...]"
    return ToolResult(success=True, output=out)
```

**Byte-cap note.** `max_bytes` remains a parameter for callers that want a hard cap; the store may honor it by refusing files larger than it (returning an error), not by silently truncating. The tool does not pass it.

**Storage layout.** One file per offloaded result:

```
<project>/.crabcakes/tool-outputs/<tool_name>-<8 hex chars>.txt
```

The 8 hex chars are `sha256(content_bytes).hexdigest()[:8]`, so **identical content offloaded twice shares one file** (idempotent, no duplicate growth). The filename is `<tool_name>` sanitized to `[A-Za-z0-9_.-]` with anything else replaced by `_`, so a tool name can never escape the directory.

**Write guard (Debugger BUG #3, fix-round 2026-10-21).** `makedirs(exist_ok=True)` follows a symlink: if `.crabcakes/tool-outputs` (or `.crabcakes` itself) is a symlink to a directory outside the project, the write lands outside and the read guard passes anyway (its comparison target is `realpath(store_dir)` — the outside dir). Both functions must therefore verify containment:

```python
def _store_realdir_inside_project(project_path: str) -> str | None:
    """Realpath of the store dir when it stays inside the project, else None.

    A symlinked .crabcakes/ or tool-outputs/ pointing outside the project
    is a misconfiguration: refuse rather than follow (Debugger BUG #3).
    """
    proj = os.path.realpath(os.path.abspath(project_path))
    store = os.path.realpath(tool_outputs_dir(project_path))
    if store != proj and not store.startswith(proj + os.sep):
        return None
    return store
```

`offload_tool_output` returns `None` and `read_tool_output` returns an error when this returns `None`. The write also verifies the *final file's* realpath parent equals the store realpath before `os.replace`.

**A note on the sanitization invariant (verified by the spec audit, 2026-10-21).** The sanitized name may still contain `..` — e.g. the tool name `weird/../name` becomes `weird_.._name`. That is **correct and safe**, and the naive check "the name contains no `..` substring" is the wrong invariant to assert. The real invariants are:

1. the name contains no `/` (so it is a flat filename, never a path), and
2. the resolved file's parent directory is the real store directory.

A `.` is a legal filename character; a flat name with `..` in it cannot traverse. The spec audit confirmed both invariants hold for the sample in §2.1:

```
PASS name sanitized
PASS resolved path stays in the store dir
PASS resolved file exists
```

**The implementer's test must assert the real invariant**, not the `..`-substring one. A test that asserts "no `..` in the basename" would force the implementation to strip `.` from tool names, which is a *different* (and unnecessary) behavior — and it would be testing a property the design never claimed.

**Module constants (define them in the new module):**

```python
# The tool name as it appears in the filename. Sanitized to [A-Za-z0-9_.-].
_SAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]")

# How much of the body the stub's preview carries. Small on purpose: the
# stub lives in the conversation, so it must stay cheap.
PREVIEW_CHARS = 200

# The store lives under the project's .crabcakes/ directory.
_STORE_SUBDIR = os.path.join(".crabcakes", "tool-outputs")
```

**The write is atomic.** Write to a temp file in the same directory, then `os.replace` it onto the final name. A torn write would leave a half-written file that the agent later reads back as a complete-looking body. The write is **refused** when the store dir is not inside the project (BUG #3), and the final file's realpath is verified before the replace (the name could itself be a symlink planted earlier).

```python
def offload_tool_output(project_path, tool_name, content):
    """Write ``content`` to the store; return its metadata, or None on failure."""
    store = _store_realdir_inside_project(project_path)
    if store is None:
        return None          # symlinked store dir escapes the project — refuse
    data = content.encode("utf-8", errors="replace")
    digest = hashlib.sha256(data).hexdigest()[:8]
    safe = _SAFE_NAME_CHARS.sub("_", tool_name or "tool")
    name = f"{safe}-{digest}.txt"
    rel_path = f".crabcakes/tool-outputs/{name}"
    try:
        os.makedirs(store, exist_ok=True)
        final = os.path.join(store, name)
        if os.path.lexists(final) and os.path.realpath(final) != final:
            return None      # final name is a symlink — refuse
        tmp = os.path.join(store, f".{name}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
    except OSError:
        return None
    return {
        "path": rel_path,
        "bytes": len(data),
        "lines": content.count("\n") + (0 if content.endswith("\n") or not content else 1),
        "preview": content[:PREVIEW_CHARS],
    }
```

**Return-value handling (Rule 6):** the dict's four keys are consumed by the strategy, which uses only `meta["path"]` to build the stub. `bytes`, `lines`, and `preview` are for tests and for a future UI listing. The `path` value is **project-relative** (not absolute) so it is stable across machines and safe to put in a conversation.

**Why `.crabcakes/tool-outputs/`:** it is inside the project sandbox (so `read_file` can reach it), gitignored (`.gitignore:37` — `.crabcakes/`), and already the app's project-local state directory.

**Path safety.** `read_tool_output` MUST reject any `rel_path` that is not exactly of the form `.crabcakes/tool-outputs/<name>.txt`. Concretely:

```python
_STORE_PREFIX = ".crabcakes/tool-outputs/"

def _is_store_path(rel_path: str) -> bool:
    if not isinstance(rel_path, str) or not rel_path:
        return False
    norm = rel_path.replace("\\", "/")
    if not norm.startswith(_STORE_PREFIX):
        return False
    name = norm[len(_STORE_PREFIX):]
    if not name or "/" in name or name in (".", ".."):
        return False
    return name.endswith(".txt")
```

Then resolve with `os.path.realpath` and confirm the result's parent is the real store directory. This mirrors the sandbox discipline of `agent/tools.py:_resolve_project_path` (`:406`) but is stricter: a path outside the store is not merely "escaping the sandbox", it is **not a store path at all**, and the tool must refuse it even when it points at a legitimate project file.

**Exceptions:** the store catches `OSError` and returns `None` / an `error` string. It never raises. Callers in `agent/` must not be able to crash a turn because a directory was not writable.

**Line estimate:** ~200 lines including docstrings and the manifest header (up from ~150 — the fix-round added the containment guard and the line-windowed read).

---

### 2.2 `agent/tools.py` — one new tool

**What changes:** add a `read_tool_output` tool next to `read_file`.

**Registration (follow the `read_file` pattern at `agent/tools.py:1846-1871`):**

```python
    # read_tool_output — SPEC-48. Reads back a tool result that compaction
    # offloaded to disk. The body is windowed exactly like read_file, so a
    # read cannot paste a whole blob back into the conversation.
    _TOOLS["read_tool_output"] = (
        ToolDefinition(
            name="read_tool_output",
            description=(
                "Read back a tool output that was offloaded to disk by "
                "compaction. path is the .crabcakes/tool-outputs/... path "
                "from the stub. The body is windowed like read_file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Store-relative path from the stub"},
                    "line": {"type": "integer", "description": "1-based start line (default 1)"},
                    "max_lines": {"type": "integer", "description": "Line count, 1-200 (default 200)"},
                },
                "required": ["path"],
            },
            requires_approval=False,
        ),
        lambda path, project_path, line=None, max_lines=None, **kwargs:  # type: ignore
            _read_tool_output(path, project_path, line, max_lines),
    )
```

**Implementation (fix-round 2026-10-21 — this replaces the Phase-1 shape, which made bytes past 16 KB unreachable silently; see §2.1 and Debugger BUGs #1/#2):**

```python
def _read_tool_output(
    path: str,
    project_path: str,
    line: int | None = None,
    max_lines: int | None = None,
) -> ToolResult:
    """Read an offloaded tool output, windowed exactly like read_file.

    Only paths under .crabcakes/tool-outputs/ are readable. The body is
    windowed exactly like read_file: a read cannot paste a whole blob
    back into the conversation. A start line past the end ERRORS, the
    same way read_file does (agent/tools.py:533-537) — it does not
    return an empty success.
    """
    from utils.tool_output_store import read_tool_output

    if not project_path:
        return ToolResult(success=False, error="No active project for this conversation")
    start = 1 if line is None else line
    if isinstance(start, bool) or not isinstance(start, int) or start < 1:
        return ToolResult(success=False, error="line must be an integer >= 1")
    window = LINE_WINDOW
    if max_lines is not None:
        if isinstance(max_lines, bool) or not isinstance(max_lines, int) or max_lines < 1:
            return ToolResult(success=False, error="max_lines must be an integer >= 1")
        window = min(max_lines, LINE_WINDOW)
    result = read_tool_output(project_path, path, line=start, max_lines=window)
    if not result.get("found"):
        return ToolResult(success=False, error=result.get("error") or f"Not an offloaded output: {path}")
    if not result.get("total_lines"):
        return ToolResult(success=True, output=f"[line {start} of {path}]\n(empty)")
    numbered = _numbered_line_block(result["output"].splitlines(), start)
    out = f"[line {start} of {path}]\n" + numbered
    if result.get("truncated"):
        out += f"\n[... truncated; continue with line={start + result['lines']} ...]"
    return ToolResult(success=True, output=out)
```

**What changed from the Phase-1 shape, and why:**

| Phase-1 shape | Problem | Fixed shape |
|---|---|---|
| `read_tool_output(..., max_bytes=MAX_READ_SIZE)` — read the first 16 KB, then window *that* body | Every byte past 16 KB was unreachable **silently**: the continuation check compared against the already-truncated body, so it never fired (BUG #1) | The store returns a **line window** with `total_lines`; the continuation is computed from the real line count |
| A `line` past the truncation point returned `success=True` with an empty body | Indistinguishable from a genuinely empty file (BUG #2) | The store returns `found=False` with "line N is past the end of …"; the tool surfaces it as `success=False` |
| `if not body: return "(empty)"` | Also fired for a start line past the end | The empty case is now `total_lines == 0` — a genuinely empty file only |

**Verified facts this code depends on** (read from source, not assumed):

- `MAX_READ_SIZE = 16 * 1024` and `LINE_WINDOW = 200` exist at `agent/tools.py:282` and `:290`. The comment at `:282-288` says `MAX_READ_SIZE` "matches CONV_OUTPUT_CAP" and that `read_file` is in `_ALREADY_WINDOWED` so its window is never head/tail-bounded.
- `_numbered_line_block(lines, start)` exists at `agent/tools.py:468` and renders `N|text` — the number and bar are not file content.
- `ToolResult` is a dataclass with `success: bool`, `output: str = ""`, `error: str | None = None` (`agent/tools.py:46-57`).
- The tool lambda receives `project_path` as a keyword — `_read_file` is called the same way at `agent/tools.py:1869-1870` (`lambda path, project_path, limit=None, line=None, max_lines=None, **kwargs`).

**Return-value handling (Rule 6):** `_read_tool_output` returns a `ToolResult`. On a refused path it returns `success=False` with an `error` string and an empty `output` — never a partially-read body. On success it returns the windowed body. The caller (`execute_tool`) serializes via `ToolResult.to_dict()` (`agent/tools.py:59`), which emits `success`, `output`, `error`.

**Exceptions:** `read_tool_output` swallows `OSError` internally and returns `error`; `_read_tool_output` therefore does not need a `try`. The `from utils.tool_output_store import read_tool_output` import is **local (function-level)**, matching the house pattern in `agent/tools.py` (e.g. `from utils.config import get_env` is module-level, but `is_sensitive_path` is called with a local import in `agent/tools.py:585`).

**Files NOT changed in `agent/tools.py`:** `_API_SCHEMA_HIDDEN` (`:2366`) stays exactly `frozenset({"file_search"})`. The new tool is visible in the API schema and in the Agent Builder checkbox list (`utils/agent_defs.py:696` derives the list from `get_all_tools()`).

**Required: add the tool to `_ALREADY_WINDOWED`** (`agent/tools.py:324`, currently `frozenset({"read_file"})` → `frozenset({"read_file", "read_tool_output"})`). `bound_tool_output` (`:2403-2415`) head/tail-bounds any tool output over `CONV_OUTPUT_CAP` (16 KB) unless the tool name is in that set — for a windowed tool that would destroy the continuation offset (`[... truncated; continue with line=N ...]`), which is exactly what the agent needs to page through an offloaded body. `read_file` is exempt for the same reason (`:284-288`). Nothing pins `_ALREADY_WINDOWED`'s membership (only `_API_SCHEMA_HIDDEN` is pinned), so this is not a deviation from a pinned contract. *Verified in Phase 1: `tests/test_tools.py::TestToolSchemaSizeBudget` fails identically before and after (same 3 agents, same char counts) — the YAMLs do not grant the tool until Phase 4, so the payload is unchanged.*

---

### 2.3 `agent/config.py` — the offload threshold

**What changes:** add a global, persisted offload-size threshold to `AgentConfig`, and thread it into the provider config so a per-provider override is possible later without a schema change.

**`AgentConfig` (currently `agent/config.py:76-101`):** add one field:

```python
    # SPEC-48: size threshold (chars) above which a tool result is offloaded
    # to <project>/.crabcakes/tool-outputs/ instead of being lossily stubbed.
    # 0 disables offloading (the pre-SPEC-48 lossy behavior).
    tool_output_offload_chars: int = 8192
```

**`LLMProviderConfig` (currently `agent/config.py:31-49`):** add one field with a per-provider override:

```python
    # SPEC-48: per-provider override of the offload size threshold.
    # 0 = "use the global value". Not exposed in the UI in this phase.
    tool_output_offload_chars: int = 0
```

**Loading (`load_agent_config`, `agent/config.py:200`):** one line added to the `AgentConfig(...)` construction (`:257-277`):

```python
        tool_output_offload_chars=int(raw.get("tool_output_offload_chars", 8192) or 0),
```

**Provider conversion (`_to_llm_provider`, `agent/config.py:148-167`):** one line added:

```python
        tool_output_offload_chars=getattr(p, "tool_output_offload_chars", 0) or 0,
```

**Default value rationale.** 8192 chars ≈ 2 KB of tokens ≈ one `read_file` window (16 KB) and well under `CONV_OUTPUT_CAP` (16 KB). It is a *default*, overridable in `agent.json`, and per-provider overridable. It is **not** a hardcoded behavior: `raw.get("tool_output_offload_chars", 8192)` reads the persisted value, and a per-provider value overrides the global.

**Note on the 0.80 threshold (the PM's standing objection to magic numbers):** this spec does **not** touch `compaction_threshold`. It already resolves per-provider (`agent/runtime.py:1724-1726`), it is already editable in the dialog (`ui/views/settings_dialog.py:159-167`), and it round-trips through `utils/providers_store.py:67` and `:170`. The *new* knob this spec introduces (the offload size) is written the same way the existing one already is: default + persisted override + per-provider override. The PM's ruling — "don't hardcode it, make it configurable" — is satisfied for the new knob without touching the existing knob.

**Files NOT changed in `agent/config.py`:** `EnforcementConfig` (unrelated), `_create_default_config` (the seeded `agent.json` example is intentionally minimal — the field defaults on load).

---

### 2.4 `models/providers.py` — the per-provider field

**What changes:** add the same field to `ProviderConfig` (the `providers.yaml`-side dataclass) so the per-provider override can be persisted and read back.

```python
    # SPEC-48: per-provider override of the offload size threshold.
    # 0 = "use the global value from agent.json".
    tool_output_offload_chars: int = 0
```

`ProviderConfig` currently lives at `models/providers.py:39-58`. The file is pure data with stdlib-only imports — adding an `int` field preserves that.

---

### 2.5 `utils/providers_store.py` — persist and load the field

**What changes:** two one-line additions, matching the existing `compaction_threshold` handling exactly.

**Serialize (`_to_dict`, `utils/providers_store.py:55-75`):** add after the `compaction_threshold` line (`:67`):

```python
        "tool_output_offload_chars": p.tool_output_offload_chars,
```

**Load (`_from_dict` — the `ProviderConfig(...)` construction at `utils/providers_store.py:136`):** add after the `compaction_threshold` line (`:170`):

```python
        tool_output_offload_chars=int(d.get("tool_output_offload_chars", 0) or 0),
```

**Verified:** `_to_dict` is the exact inverse of the `ProviderConfig(...)` construction — the existing `compaction_threshold` appears once in each with matching defaults (`0.80`). The new field follows the same shape with a `0` default meaning "inherit".

**Files NOT changed in `utils/providers_store.py`:** nothing else. The Settings dialog's provider card is NOT wired to this field in this phase (see §2.9).

---

### 2.6 `agent/context_strategy.py` — the offload hook and the digest

This is the heart of the spec. Two changes, both in `DefaultContextStrategy`.

#### 2.6.1 The offload hook

**The contract problem.** `ContextStrategy.compact` may only mutate `conv.messages` and `conv._token_estimate_cache` (Protocol docstring, `agent/context_strategy.py:76-82`; ARCHITECTURE.md §3.21p.5). The strategy must not write files.

**The solution.** The strategy gains an **optional offload callback** that the runtime supplies. The strategy calls it; the runtime does the I/O.

```python
@dataclass
class OffloadRequest:
    """One tool result the strategy wants offloaded to disk.

    Deliberately minimal: neither stubbing site (prune_tool_outputs,
    panic_prune) has a session key in scope, and the store's filename is
    content-derived, not session-derived.
    """
    tool_name: str
    content: str
```

```python
class DefaultContextStrategy:
    def __init__(
        self,
        offload: Callable[[Conversation, OffloadRequest], dict | None] | None = None,
    ) -> None:
        self._last_result: CompactionEvent | None = None
        self._offload = offload
```

**`LLMSummarizeStrategy.__init__` must also take the parameter.** It currently reads (verified at `agent/context_strategy.py:972-979`):

```python
    def __init__(
        self,
        llm_provider: Callable[[str, str], str] | None = None,
        llm_model_override: str | None = None,
    ) -> None:
        super().__init__()
        self._llm_provider = llm_provider
        self._llm_model_override = llm_model_override
```

`super().__init__()` passes nothing, so `self._offload` would be `None` on the LLM strategy and the forced-compact path would never offload. Add the parameter and forward it:

```python
    def __init__(
        self,
        llm_provider: Callable[[str, str], str] | None = None,
        llm_model_override: str | None = None,
        offload: Callable[[Conversation, OffloadRequest], dict | None] | None = None,
    ) -> None:
        super().__init__(offload=offload)
        self._llm_provider = llm_provider
        self._llm_model_override = llm_model_override
```

**This is a required edit, not optional** — the forced-compact path (`AgentRuntime.force_llm_compact`, `agent/runtime.py:4703-4711`) constructs `LLMSummarizeStrategy` and would otherwise silently lose the offload capability. Both call sites are then:

```python
# agent/runtime.py:791
self._context_strategy = DefaultContextStrategy(offload=self._offload_tool_output)

# agent/runtime.py:4703-4711
strat = LLMSummarizeStrategy(
    offload=self._offload_tool_output,
    llm_provider=lambda sys_p, user_p, model_id=None: ...,
)
```

**Backward compatibility:** `offload` is keyword-only-ish (a defaulted parameter), so every existing construction — `DefaultContextStrategy()` in ~40 test files, `LLMSummarizeStrategy(llm_provider=None)` in `tests/test_llm_summarize_strategy.py:13,25,37,56,74,84` — keeps working unchanged, with `self._offload = None` and therefore the plain stub. **That is the property that keeps the compatibility set (§3) green.**

The callback returns a metadata dict (`{"path": ..., "bytes": ..., "lines": ..., "preview": ...}`) or `None` when the offload did not happen. `None` ⇒ the strategy falls back to the current lossy stub.

**The new stub format.** When an offload succeeds, the conversation gets a stub that carries the path. The existing `_compacted_stub` (`agent/context_strategy.py:477`) stays **unchanged** — it is the fallback and it is pinned by exact-equality tests (§3, Compatibility). The new format is a separate writer:

```python
    @staticmethod
    def _offloaded_stub(tool_name: str, n_chars: int, rel_path: str) -> str:
        """The stub written when a tool result was offloaded to disk.

        The ``[compacted —`` prefix is matched with ``startswith`` in
        agent/runtime.py (_body_live_in_transcript, _last_transcript_body_index)
        and in agent/context_strategy.py (prune_tool_outputs, panic_prune),
        so the prefix MUST NOT change. The path after the marker is what the
        agent reads back with the read_tool_output tool.
        """
        return (
            f"[compacted — {tool_name} {n_chars} → offloaded to "
            f"{rel_path} (read it back with read_tool_output)]"
        )
```

**Critical compatibility point.** The runtime's liveness checks classify a body as dead by `content.startswith("[compacted —")` (`agent/runtime.py:4205-4206` and `:4255-4256`). The offloaded stub **keeps that prefix**, so an offloaded body is still correctly treated as *not live* — the agent must read it back rather than assume it has the bytes. This is intentional and must be preserved.

**The two call sites that stub a tool result.** Both must go through one helper so the behavior is uniform (there are exactly two — `compact()` reaches the first through `prune_tool_outputs`):

1. `prune_tool_outputs` — `agent/context_strategy.py:455-457` (currently `original_len = len(msg.content); stub = self._compacted_stub(tool_name, original_len)`)
2. `panic_prune` — `agent/context_strategy.py:511-513` (currently `n_chars = len(content); stub = self._compacted_stub(tool_name, n_chars)`)

**The shared helper — the final form.** (An earlier draft of this spec had the
threshold check inside the strategy; that was wrong and is not to be
implemented. The threshold lives in the runtime. See the note below.)

```python
    def _stub_tool_result(
        self,
        conv: Conversation,
        msg,
        tool_name: str,
    ) -> str:
        """Return the stub that replaces one tool result's body.

        The body is offloaded to disk when the runtime's callback accepts
        it; otherwise the plain lossy stub is returned. The caller writes
        the returned text to ``msg.content`` and updates the parent call
        and the token cache exactly as it does today.
        """
        content = msg.content or ""
        n_chars = len(content)
        stub = self._compacted_stub(tool_name, n_chars)
        if self._offload is not None:
            meta = self._offload(
                conv,
                OffloadRequest(
                    tool_name=tool_name,
                    content=content,
                ),
            )
            if meta and meta.get("path"):
                stub = self._offloaded_stub(tool_name, n_chars, meta["path"])
        return stub
```

**Why the threshold is NOT in the strategy.** The strategy does not read
`AgentConfig` (that would be a new dependency direction, and
`agent/context_strategy.py` imports only `logging`, `dataclasses`, `typing`,
and `models.conversation` — C21). The runtime's callback resolves the
threshold itself and returns `None` when the body is under it. The strategy
then only needs to know one thing: "did an offload happen?" — so the
threshold logic lives in exactly one place, the runtime, where
`AgentConfig` and `LLMProviderConfig` already live.

**No `session_key` parameter.** `prune_tool_outputs` (`:359`) and
`panic_prune` (`:485`) do not receive a session key, so there is nothing to
forward. The store's filename is content-derived (§2.1), not session-derived.
`OffloadRequest` therefore carries only `tool_name` and `content`.

**The two call sites become:**

```python
# agent/context_strategy.py:455-457 (prune_tool_outputs)
stub = self._stub_tool_result(conv, msg, tool_name)

# agent/context_strategy.py:511-513 (panic_prune)
stub = self._stub_tool_result(conv, msg, tool_name)
```

**Do NOT keep the old `original_len` / `n_chars` locals.** They are used only to build the stub, and the stub builder computes the length itself — leaving them in place is ruff **F841** (unused variable). *The Debugger caught this in the spec's own sample.* Delete them, or keep them only if you use them (the stub builder does not need them).

Both sites keep their existing surrounding lines unchanged — the parent
`tc.result = None` (`:460-463`, `:514-517`), the `msg.tokens_used =
len(stub) // 4` (`:466`, `:518`), and the
`conv._token_estimate_cache = None` (`:470`, `:519`). **Note C8:** the
offloaded stub is longer than the plain stub, so `tokens_used` must be
recomputed from the *actual* stub text at those lines, not copied from
`n_chars // 4`.

#### 2.6.2 The typed digest

**What changes:** the summary injected by `compact()` gains a companion typed digest.

**Where the summary is injected** — `agent/context_strategy.py:296-316`:

```python
        messages_removed = messages_count_before - len(conv.messages)
        if messages_removed > 0 and len(conv.messages) >= min_messages:
            summary = self._summary(conv, token_budget=token_budget, keep_first=keep_first)
            if summary:
                ...
                summary_msg = Message(
                    role=MessageRole.ASSISTANT,
                    content=fitted,
                    is_summary=True,
                )
                insert_at = max(keep_first, len(conv.messages) - tail_preserve)
                conv.messages.insert(insert_at, summary_msg)
```

**The new data model.** `ConversationDigest` is pure data, so it belongs in `models/conversation.py` (§3.21l — pure data, stdlib only):

```python
@dataclass
class ConversationDigest:
    """Typed summary of a compacted conversation (SPEC-48).

    Pure data. The strategy fills it; the runtime persists it; the agent
    queries it. No prose — each field is queryable.
    """
    arc: str = ""
    decisions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    paths_touched: list[str] = field(default_factory=list)
    attempts_rejected: list[str] = field(default_factory=list)
    token_estimate: int = 0

    def to_dict(self) -> dict:
        return {
            "arc": self.arc,
            "decisions": list(self.decisions),
            "constraints": list(self.constraints),
            "open_questions": list(self.open_questions),
            "paths_touched": list(self.paths_touched),
            "attempts_rejected": list(self.attempts_rejected),
            "token_estimate": self.token_estimate,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConversationDigest":
        """Rebuild a digest from persisted data. Unknown keys are ignored."""
        if not isinstance(data, dict):
            return cls()
        return cls(
            arc=str(data.get("arc", "") or ""),
            decisions=[str(x) for x in (data.get("decisions") or [])],
            constraints=[str(x) for x in (data.get("constraints") or [])],
            open_questions=[str(x) for x in (data.get("open_questions") or [])],
            paths_touched=[str(x) for x in (data.get("paths_touched") or [])],
            attempts_rejected=[str(x) for x in (data.get("attempts_rejected") or [])],
            token_estimate=int(data.get("token_estimate", 0) or 0),
        )
```

`models/conversation.py` already imports `dataclass, field` from `dataclasses` (`:15`) and `datetime` (`:16`) — no new import needed.

**Where the digest is stored.** On the `Conversation` — one field, so it survives the trim (the trim removes *messages*, not conversation-level state):

```python
    # SPEC-48: the latest typed digest. Set by the compaction strategy when
    # it injects a summary; survives message trimming; persisted on disk.
    digest: ConversationDigest | None = None
```

**How the digest is computed.** The digest must be cheap and must not add an LLM call to every compaction. Two tiers:

- **Tier 2 (when the LLM strategy is in use):** `LLMSummarizeStrategy` already receives nine `<tag>` sections from the LLM (`SUMMARY_PROMPT_TEMPLATE`, `agent/context_strategy.py:949-970`): `task`, `progress`, `files`, `decisions`, `constraints`, `errors`, `open_questions`, `next_steps`, `user_preferences`. The digest is parsed from those tags — no new LLM call, no new prompt. **This is the rich tier, and it is free.**

- **Tier 1 (fallback, no LLM):** derived from the messages in the **trimmable region** — see the snapshot rule below. The trim loop pops messages in place (`:198`, `:209`, `:222`, `:267`, `:268`, `:270`) and never collects them, so the digest is computed from a snapshot taken before the trim.

**The implementer must snapshot the trimmable region, not the whole conversation (Debugger BUG #4, fix-round 2026-10-21).** The first draft snapshotted all assistant messages — but `prune_tool_outputs` removes **no messages** (it rewrites TOOL_RESULT bodies in place, `agent/context_strategy.py:425-470`), and the trim loop only removes messages in the *trimmable* region (between `keep_first` and `len - tail_preserve`). A whole-conversation snapshot describes the conversation that was **kept** — the head and the tail — which is "the opposite of what a digest is for" (the spec's own words, and exactly what its first draft did). Scope the snapshot the way `_select_prune_candidate` scopes its scan:

```python
        # SPEC-48: snapshot the messages the trim can actually remove —
        # the region between keep_first and len - tail_preserve. The head
        # and the tail survive the trim, so they are not digest material.
        # prune_tool_outputs rewrites bodies in place and removes nothing,
        # so this snapshot is taken AFTER it, from the surviving list.
        trim_end = len(conv.messages) - tail_preserve
        digest_source = [
            m
            for m in conv.messages[keep_first:trim_end]
            if m.role == MessageRole.ASSISTANT and not m.is_summary
        ]
```

The snapshot holds **assistant** messages only — their tool-call arguments are what `paths_touched` needs, and the tool results are already stubbed by Layer 1. It is a list of *references*, so it costs no copying of bodies.

**The tag parser** (in `agent/context_strategy.py`, next to `LLMSummarizeStrategy`):

```python
def _digest_from_tagged_summary(text: str, conv: Conversation) -> ConversationDigest:
    """Parse the nine-tag LLM summary into a ConversationDigest.

    Tags that are missing or empty leave the corresponding field empty.
    A tag whose body is a single line becomes one list item; a multi-line
    body becomes one item per non-empty line, with leading "- " stripped.
    """
```

**Tag → digest field mapping** (verified against `SUMMARY_PROMPT_TEMPLATE`, `agent/context_strategy.py:958-966`):

| Tag | Digest field |
|---|---|
| `<task>` | `arc` (the one-sentence task statement) |
| `<decisions>` | `decisions` |
| `<constraints>` | `constraints` |
| `<open_questions>` | `open_questions` |
| `<files>` | `paths_touched` (comma-separated → one item per path) |
| `<errors>` | `attempts_rejected` |
| `<progress>`, `<next_steps>`, `<user_preferences>` | not represented in the digest (the summary text itself keeps them) |

**Tier assignment is decided by the summary's shape, not by the strategy class (Debugger, axis 4).** A Tier-2 strategy whose LLM call fails falls back to the **textual** summary (`LLMSummarizeStrategy._summary` → `super()._summary` on no-provider, empty response, or exception — `agent/context_strategy.py:997`, `:1031`, `:1038`), and the textual summary has no tags. So the tier is not "which class am I?" but "does the summary carry the tags?":

```python
def _digest_for_summary(summary_text: str, snapshot: list) -> ConversationDigest:
    """Tier 2 when the summary carries the tags; Tier 1 otherwise.

    The tag check IS the tier check: an LLM strategy that fell back to
    the textual summary must not be treated as Tier 2, or the digest
    silently empties on every LLM failure.
    """
    if "<task>" in summary_text and "</task>" in summary_text:
        return _digest_from_tagged_summary(summary_text)
    return _digest_from_snapshot(snapshot, summary_text)
```

**Tier 1 is honest about what a textual summary can know.** It populates `arc` (the first line of the textual summary), `paths_touched`, and `attempts_rejected` — and leaves `decisions`, `constraints`, and `open_questions` **empty**. A textual summary carries no signal for those three fields, and inventing them from prose would be guessing.

**Determinism.** The digest must be reproducible from the same conversation state. `paths_touched` collects the `path` argument of every tool call on the snapshotted messages **in first-appearance order, deduplicated, capped at 32 entries**; `attempts_rejected` collects snapshotted assistant text that starts with `REFUSED:` or contains a line beginning `FAILED ` — again in order, capped at 16. Caps keep a huge conversation from producing a digest larger than its summary. No heuristics beyond these two literal markers — a deterministic rule beats a clever one.

```python
    def _digest_from_snapshot(
        self,
        snapshot: list,
        summary_text: str,
    ) -> ConversationDigest:
        """Tier 1 — deterministic fields only, no LLM call, no guessing.

        arc = first line of the textual summary; paths_touched = path args
        of tool calls on the snapshotted messages (first-appearance order,
        deduped, cap 32); attempts_rejected = snapshotted assistant text
        starting REFUSED: or containing a 'FAILED ' line (cap 16).
        decisions/constraints/open_questions stay EMPTY in Tier 1.
        """
```

**Compute from the raw summary, before fitting.** `compact()` calls `self._summary(...)` then `self._fit_summary(...)`. The digest must be computed from the **raw** summary text (tags intact) *before* `_fit_summary` truncates it — otherwise a small budget strips the tags and the tier check fails. The order is: `summary = self._summary(...)` → `digest = _digest_for_summary(summary, digest_source)` → `fitted = self._fit_summary(...)` → inject → `conv.digest = digest`.

**Where it is set.** In `compact()`, right where the summary is injected:

```python
                conv.messages.insert(insert_at, summary_msg)
                conv.digest = digest
```

**Crucially:** the digest is set **only when a summary was actually injected**. A no-op compaction must not overwrite a good digest with an empty one.

**Files NOT changed in `models/conversation.py`:** `Message` (the `is_summary` field already exists at `:131`), the token-estimate machinery, `serialize_messages`.

---

### 2.7 `agent/runtime.py` — wire the offload callback and resolve the threshold

**What changes:** three edits.

**Edit 1 — the strategy construction** (`agent/runtime.py:790-791`):

```python
        from agent.context_strategy import DefaultContextStrategy
        self._context_strategy = DefaultContextStrategy()
```

becomes:

```python
        from agent.context_strategy import DefaultContextStrategy
        self._context_strategy = DefaultContextStrategy(offload=self._offload_tool_output)
```

**Edit 2 — the offload closure + threshold resolution.** New method on `AgentRuntime`, placed next to `_compact_before_llm` (`agent/runtime.py:4067`):

```python
    def _resolve_offload_threshold(self, conv: "Conversation") -> int:
        """Resolve the offload size threshold for this conversation.

        Order:
          1. the conversation's provider's tool_output_offload_chars
             (when > 0)
          2. the global AgentConfig.tool_output_offload_chars
          3. 0 (offloading disabled)

        Mirrors _compute_compaction_threshold's resolution order: a
        per-provider value wins, the global is the fallback, and 0 means
        "off". The compaction TRIGGER threshold is a different knob and
        is not consulted here.
        """
        threshold = int(getattr(self._config, "tool_output_offload_chars", 0) or 0)
        try:
            provider_name = (
                conv.model.split("/")[0]
                if conv.model and "/" in conv.model
                else self._config.default_provider
            )
            if provider_name:
                provider_cfg = self._config.providers.get(provider_name)
                if provider_cfg is not None:
                    per_provider = int(
                        getattr(provider_cfg, "tool_output_offload_chars", 0) or 0
                    )
                    if per_provider > 0:
                        threshold = per_provider
        except Exception:
            logger.debug(
                "_resolve_offload_threshold: per-provider lookup failed; "
                "using global value", exc_info=True,
            )
        return threshold

    def _offload_tool_output(self, conv, request) -> dict | None:
        """Offload one tool result to the project store.

        Called by the context strategy through the offload callback. The
        strategy stays free of file I/O (ContextStrategy may only mutate
        conv.messages and conv._token_estimate_cache).

        Returns the store metadata dict, or None when the body is under
        the threshold, the conversation has no project, or the write
        failed. The strategy then falls back to the plain lossy stub.
        """
        project_path = getattr(conv, "project_path", None)
        if not project_path:
            return None
        threshold = self._resolve_offload_threshold(conv)
        if threshold <= 0 or len(request.content) <= threshold:
            return None
        from utils.tool_output_store import offload_tool_output
        try:
            return offload_tool_output(
                project_path,
                request.tool_name,
                request.content,
            )
        except Exception:
            logger.exception("offload_tool_output failed")
            return None
```

**Why the callback takes the conversation.** The project path comes from
`conv.project_path` (the `Conversation` field, `models/conversation.py:267`)
— no new accessor is invented, and the store lands inside the same sandbox
the tool calls are confined to. `conv.project_path` is documented at
`models/conversation.py:259-266` as the directory the agent is working in,
re-applied in memory by `_rebuild_conversation_context`
(`agent/runtime.py:4511`) on project switch and on load.

**Why `conv.project_path` is safe here.** The *persisted* value may be
stale, but the in-memory value is what the runtime already uses to sandbox
tool calls (`agent/tools.py` receives `project_path` from the runtime), so
using the same value for the offload store keeps the store inside the
sandbox.

**Edit 3 — `panic_prune` and `prune_tool_outputs` call the new helper.** Both are in `agent/context_strategy.py`; the runtime is untouched by this part. See §2.6.1.

**Edit 4 — the global threshold must reach a running runtime (Debugger #9, fix-round 2026-10-21).** `ui/agent_runtime/provider.py:refresh_provider_config` (`:82-151`) reloads `providers.yaml` and swaps **only** `cfg.providers` (`:142-143`) — it never replaces `cfg` itself. A global `tool_output_offload_chars` hand-edited in `agent.json` therefore never reaches an already-running runtime; it only takes effect after an app restart. The **per-provider** value works (it rides inside `cfg.providers`), so the failure is specific to the global.

Two acceptable fixes — pick one and say which in the report:

- **(a) Recommended.** In `refresh_provider_config`, after the providers swap, copy the global scalar onto each runtime's config: `rt._config.tool_output_offload_chars = fresh.tool_output_offload_chars`. `AgentConfig` is a plain mutable dataclass (`agent/config.py:75-99`), so this is safe and does not disturb the per-runtime provider clones that function goes to such lengths to isolate.
- **(b) Not recommended.** Resolve the global lazily at threshold-resolution time by re-reading `agent.json` — but that puts file I/O on the compaction hot path, which this spec otherwise avoids.

With (a), the §2.3 global value is authoritative *and* reachable without a restart.

---

### 2.8 `agent/persistence.py` — persist the digest

**What changes:** the digest must survive a restart, or "a compacted session's summary exposes decisions and open questions as fields" is false after a restart.

**Save (`agent/persistence.py:60-89`):** the message loop writes a `messages` list; the conversation-level fields are written as siblings. Add one key next to `"allowed_tools"` (`:81`):

```python
        "digest": conv.digest.to_dict() if getattr(conv, "digest", None) is not None else None,
```

**Load (`_conversation_from_data`, `agent/persistence.py:150`):** add one argument:

```python
    digest = ConversationDigest.from_dict(data.get("digest") or {})
    ...
    return Conversation(
        agent_name=data["agent_name"],
        ...
        digest=digest if digest.arc or any(digest.decisions) or any(digest.open_questions) else None,
    )
```

The `if` keeps a conversation that never compacted from carrying an empty digest object.

**Files NOT changed in `agent/persistence.py`:** `_message_from_data` (`:117`) — the digest is conversation-level, not message-level. The `is_summary` flag is **not** persisted today (a saved summary message loads back as a plain assistant message); that is pre-existing and out of scope.

---

### 2.9 `ui/views/settings_dialog.py` + `ui/handlers/settings_handler.py` — the threshold UI

**What changes:** one spin button on the Providers page, next to the existing "Compaction threshold" spin (`:159-167`).

**The spin** (in `_ProviderCard._build_widgets`, after the compaction-threshold row):

```python
        # SPEC-48 — Tool output offload threshold (chars).
        # Above this size a tool result is written to
        # .crabcakes/tool-outputs/ and replaced by a path-carrying stub,
        # instead of being lossily discarded. 0 disables offloading.
        self._offload_chars_spin = Gtk.SpinButton.new_with_range(0, 1_000_000, 1024)
        self._offload_chars_spin.set_value(
            self._provider.tool_output_offload_chars or 0
        )
        self._offload_chars_spin.set_hexpand(True)
        offload_row = self._labeled(
            "Tool output offload (chars)", self._offload_chars_spin
        )
        vbox.append(offload_row)
```

**The load** (where the compaction threshold is re-set, `:285`):

```python
        self._offload_chars_spin.set_value(self._provider.tool_output_offload_chars or 0)
```

**The save** (where the compaction threshold is saved, `:312` and `:352-353`): add the field to the change-detection comparison and to the `replace(...)` call:

```python
        or int(self._offload_chars_spin.get_value()) != (self._provider.tool_output_offload_chars or 0)
```

```python
            tool_output_offload_chars=int(self._offload_chars_spin.get_value()),
```

**Three more places the field must be carried (Debugger #8, fix-round 2026-10-21).** `SettingsHandler.add_or_update` (`ui/handlers/settings_handler.py:151-155`) **replaces the whole provider entry** — it filters the list by name and appends the new card. Any field not carried by the form therefore resets to its default on every save, silently. So the field must be carried by:

1. `_collect_from_form` (`ui/views/settings_dialog.py:312`) — builds the `ProviderConfig` the handler saves.
2. `ui/views/agent_builder.py:310` — constructs `ProviderConfig` from a dict (the agent builder's provider list). Without the field here, a provider configured in the Builder loses the setting.
3. `utils/providers_store.py:441` — the legacy `agent.json` → `ProviderConfig` migration. Without the field here, a migrated provider loses the setting.

All three build `ProviderConfig(...)` field-by-field, so each needs `tool_output_offload_chars=...` added.

**Why per-provider and not global:** the existing "Compaction threshold" is per-provider, and the new threshold has the same resolution order (per-provider wins, global falls back). Putting it in the same card keeps the two knobs side by side, where a user can reason about them together. The **global** value lives in `agent.json` and is edited by hand in this phase — a global UI row is deferred.

**Files NOT changed in `ui/views/settings_dialog.py`:** the STT/speech/telegram/decision pages; `_ProviderCard` stays a pure view (no I/O — the handler persists).

---

### 2.10 `prompts/default_agents/*.yaml` — grant the new tool

**What changes:** add `read_tool_output` to the three default agent definitions so the agents can actually read back what compaction offloaded.

```yaml
tools:
  - read_file
  - read_tool_output
  - ...
```

- `prompts/default_agents/coder.yaml` — after `read_file`
- `prompts/default_agents/debugger.yaml` — after `read_file`
- `prompts/default_agents/supervisor.yaml` — after `read_file`

**Migration for existing installs.** `utils/agent_defs.py:_seed_defaults()` (`:169-179`) calls `ensure_builtin_tool` (`:189`) for a fixed list of tools. Add `"read_tool_output"` to that tuple so an existing install gains the tool the same way `feed_card` was migrated (see `tests/test_builtin_tool_migration.py:76`):

```python
    for tool in (
        "diary",
        "project_memory",
        "jev_ask",
        "canvas",
        "talk",
        "agent_context",
        "feed_card",
        "read_tool_output",
    ):
```

**Tool-schema budget — a known-red test.** `tests/test_tools.py:2169-2188` asserts each default agent's serialized tool-schema JSON is under 4000 chars. It is **already failing** on all three agents (coder 8690, debugger 8255, supervisor 7786) and is recorded in `tests/known_failures.txt:22-24` as known debt. Adding a tool makes it worse.

**This spec does not fix that test** (it needs a deliberate budget revisit, per the note in `tests/known_failures.txt:8-10`). But it MUST NOT add a *new* failure. The new tool's schema is deliberately minimal — one required string parameter plus two optional ones — and the implementer must measure the delta:

```
before: coder 8690, debugger 8255, supervisor 7786
after:  must be measured and reported
```

**Constraint:** the new tool's `description` and parameter descriptions must stay short. The house rule at `agent/tools.py:1866` is explicit: "descriptions below stay one line each; do not expand."

---

### 2.11 `docs/ARCHITECTURE.md` — update required

**§3.21p.5 `agent/context_strategy.py`** — add the offload callback to the public API block and describe the offload-then-stub behavior.

**§3.21n `agent/tools.py`** — add `read_tool_output` to the public API list.

**New section** — add `utils/tool_output_store.py` to Section 2 (directory structure) and Section 11 (file inventory), with the same shape as `utils/feed_store.py`'s entry (§3.22d).

**§3.21l `models/conversation.py`** — add `ConversationDigest` and the `Conversation.digest` field.

**§3.21o `agent/config.py`** — add the two new threshold fields.

---

## 3. Compatibility — the contracts this spec must not break

Every item below was found by reading the code and the tests, not assumed. **These are the things an implementer will break if they change a format without checking.**

**Count: 26 contracts.** C1–C22 were found while writing the spec; **C23–C26 were added by the Debugger's adversarial review (2026-10-21)**, four of which (C23, C24, C25, C26) the spec's first draft had missed entirely.

| # | Pinned contract | Where | Why it matters |
|---|---|---|---|
| C1 | Stub text is exactly `[compacted — {tool_name} {n_chars}]` | `tests/test_context_strategy.py:370`, `tests/test_context_strategy_audit_fixes.py:166,178,195`, `tests/test_context_strategy_audit_fixes2.py:132` | **Exact-equality** assertions. The plain stub path must stay byte-identical. |
| C2 | The plain-stub fixtures build `Conversation` **without** `project_path` | same tests — `Conversation(agent_name="Coder", model="openai/gpt-4o")` ⇒ `project_path is None` | With no project there is no store, so the offload callback returns `None` and the plain stub is written. **The offload path must not change the stub when there is no project.** This is what keeps C1 green. |
| C3 | `panic_prune` stub contains no `"chars removed"`, no `"re-run to restore"`, no `"re-read to restore"` | `tests/test_spec33b_exec_retention.py:119-121` | The new offloaded stub must not reintroduce these phrases. |
| C4 | `panic_prune` stub **startswith** `"[compacted — exec_command "` | `tests/test_spec33b_exec_retention.py:118`, `tests/test_panic_prune.py:40` | `startswith`, so a longer stub is fine — as long as the prefix and the tool name come first. The offloaded stub's shape `[compacted — {tool} {n} → offloaded to {path} …]` satisfies this. |
| C5 | The stub prefix `[compacted —` marks a body as **dead** | `agent/runtime.py:4205-4206`, `:4255-4256` (`_body_live_in_transcript`, `_last_transcript_body_index`) | The offloaded stub keeps the prefix ⇒ still dead ⇒ the agent must read it back. **Do not change the prefix.** |
| C6 | `prune_tool_outputs` is idempotent — already-stubbed messages are skipped | `agent/context_strategy.py:434`, `:506` | The offloaded stub starts with `[compacted —`, so it is skipped on a second pass. C5's prefix is what makes this work. |
| C7 | `prune_tool_outputs` invalidates `conv._token_estimate_cache` after each mutation | `agent/context_strategy.py:470` | The offload path must invalidate the cache the same way. The body is replaced by a *shorter* stub, so the estimate changes. |
| C8 | `msg.tokens_used` is set to the stub's own token footprint (`len(stub) // 4`) | `agent/context_strategy.py:466`, `:518` | The offloaded stub is longer than the plain stub; its `tokens_used` must be recomputed from the actual stub text, not copied. |
| C9 | `parent.tool_calls[].result = None` when the body is stubbed | `agent/context_strategy.py:460-463`, `:514-517` | The duplicate copy of the body on the parent call must also be dropped, or the bytes stay in memory. |
| C10 | `compact()` injects a summary `Message` with `is_summary=True` | `agent/context_strategy.py:310-316`, `tests/test_context_strategy.py:1116-1136` | The digest is set alongside it, never instead of it. |
| C11 | A summary is injected only when `messages_removed > 0 and len(conv.messages) >= min_messages` | `agent/context_strategy.py:297` | The digest follows the same condition. A no-op compaction must not clear an existing digest. |
| C12 | `LLMSummarizeStrategy._summary` returns the LLM response **verbatim** | `tests/test_llm_summarize_strategy.py:69`, `:95` | The digest is parsed *alongside* the returned text. The returned text is not rewritten. |
| C13 | `_fit_summary` may truncate the summary; the summary may become the stub `"[Context reset — …]"` | `agent/context_strategy.py:773` | When the fitted summary is that stub, the digest still holds the structured fields — that is the point of the digest. |
| C14 | `ContextStrategy` may only mutate `conv.messages` and `conv._token_estimate_cache` | `agent/context_strategy.py:76-82` (Protocol docstring) | The offload callback is what keeps this true. The strategy writes no files. |
| C15 | The `allowed_tools` gate denies any tool not in the list | `agent/tools.py:2440-2445` | `read_tool_output` must be granted in the three default agent YAMLs (§2.10) or the agents cannot call it. |
| C16 | `_API_SCHEMA_HIDDEN` is exactly `frozenset({"file_search"})` | `agent/tools.py:2366`; `tests/test_agent_defs.py:454-460` | The new tool is **not** schema-hidden. |
| C17 | The tool-schema budget test is **already red** on all three agents | `tests/test_tools.py:2169-2188`; `tests/known_failures.txt:22-24` | Do not add a new failure. The budget is known debt; this spec must not make it worse without measurement. |
| C18 | `ensure_builtin_tool` appends `- {tool}` in the YAML's own style | `tests/test_builtin_tool_migration.py:39-44` | The migration entry (§2.10) uses the existing helper; no new YAML style. |
| C19 | `Conversation.project_path` may be `None` | `models/conversation.py:267` | The offload must be a no-op when it is `None` (this is C2's runtime face). |
| C20 | The stored `project_path` on disk is **not** authoritative | `models/conversation.py:259-266` | Use the in-memory `conv.project_path`, which `_rebuild_conversation_context` re-applies. |
| C21 | `agent/context_strategy.py` imports only `logging`, `dataclasses`, `typing`, and `models.conversation` | `agent/context_strategy.py:11-22` | The digest dataclass lives in `models/conversation.py` (pure data), not in the strategy. |
| C22 | A test asserts the *absence* of a specific import in the strategy | `tests/test_context_strategy_audit_fixes2.py:246-251` | Read that test before adding any import to `agent/context_strategy.py`. |
| C23 | The API tool-name set is pinned to an exact 16-name list | `tests/test_spec24_facts.py:164` (`test_no_new_tool_name`) | **Added by the tool — this is a tripwire, not a coincidence.** The test's own comment: "Adding a new tool must update this set deliberately — otherwise the tripwire goes RED." Registering `read_tool_output` makes it fail until the set is updated deliberately. **The implementer must add `read_tool_output` to that set in the same change that registers the tool.** *Confirmed by execution: this test went red in Phase 1 — the Supervisor's Phase 1 verification missed it.* |
| C24 | `_ALREADY_WINDOWED` controls whether `bound_tool_output` head/tail-bounds a tool's output | `agent/tools.py:324`, `:2414` | A windowed tool must be exempt or its continuation offset is destroyed. `read_file` is exempt for this reason; `read_tool_output` must be too. Nothing pins the set's membership, so this is an addition, not a deviation. |
| C25 | `add_or_update` replaces the **whole** provider card | `ui/handlers/settings_handler.py:151-155` | Any field not carried by `_collect_from_form` (`ui/views/settings_dialog.py:312`) resets to its default on every save. A new provider field must therefore be carried by `_collect_from_form` **and** by `ui/views/agent_builder.py:310` and `utils/providers_store.py:441` — or it silently resets. |
| C26 | The token-estimate cache key is `(len(messages), hash(system_prompt), hash(task_card))` | `agent/context_strategy.py:379-384` | The digest is conversation-level state, not message content, so it is **not** in the cache key — no cache interaction, and no invalidation needed when the digest is set. But adding the digest must not change `len(conv.messages)`, or the cache key changes and the estimate is invalidated (harmless, but note it). |

---

## 4. Data flow

### 4.1 The offload path (a tool result is stubbed)

```
1. A tool result lands in the conversation.
   agent/runtime.py: tool batch → conv.add_tool_result(call_id, body)

2. The next LLM call is prepared.
   agent/runtime.py:1967  soft_ceiling, hard_ceiling = self._compute_compaction_threshold(conv)
   agent/runtime.py:1969  self._compact_before_llm(session_key, conv, soft_ceiling, hard_ceiling)

3. The conversation is over the line.
   agent/runtime.py:4081  if estimate > soft_ceiling and soft_ceiling > landing_goal:
   agent/runtime.py:4083      self._context_strategy.panic_prune(conv, protect_paths=protect)

4. panic_prune walks the tool results before the last user message.
   agent/context_strategy.py:502  for i, msg in enumerate(conv.messages):

5. The stub is built. NEW: _stub_tool_result asks the runtime to offload.
   agent/context_strategy.py:512  stub = self._stub_tool_result(conv, msg, tool_name)

6. The runtime's callback decides whether the body is big enough.
   agent/runtime.py:_offload_tool_output(conv, request)
     - project_path = conv.project_path           (None → return None)
     - threshold   = self._resolve_offload_threshold(conv)
     - threshold <= 0 or len(content) <= threshold → return None
     - utils/tool_output_store.py:offload_tool_output(project_path, tool, content)
         → {"path": ".crabcakes/tool-outputs/exec_command-ab12cd34.txt", "bytes": …, "lines": …, "preview": …}

7. The conversation gets the path-carrying stub.
   agent/context_strategy.py:  "[compacted — exec_command 51200 → offloaded to
                               .crabcakes/tool-outputs/exec_command-ab12cd34.txt
                               (read it back with read_tool_output)]"

8. The body is dropped from the parent call too.
   agent/context_strategy.py:514-517  tc.result = None
   agent/context_strategy.py:518      msg.tokens_used = len(stub) // 4
   agent/context_strategy.py:519      conv._token_estimate_cache = None

9. Later, the agent wants the bytes back.
   agent/tools.py:_read_tool_output(path, project_path, line, max_lines)
     → utils/tool_output_store.py:read_tool_output(project_path, path, line=…, max_lines=…)
     → the line window (at most LINE_WINDOW lines), with total_lines
     → the tool numbers the lines and appends the continuation notice
```

### 4.2 The digest path (a summary is injected)

```
1. compact() removes messages.
   agent/context_strategy.py:180-271  the trim loop

2. A summary is generated.
   agent/context_strategy.py:298  summary = self._summary(conv, token_budget=…, keep_first=…)
   - DefaultContextStrategy._summary  → the textual preview (agent/context_strategy.py:855)
   - LLMSummarizeStrategy._summary     → the nine-tag LLM output (agent/context_strategy.py:981)

3. The digest is computed. NEW.
   - LLMSummarizeStrategy: _digest_from_tagged_summary(summary, conv)   ← parses the nine tags
   - DefaultContextStrategy:  self._digest_from_snapshot(snapshot, summary)  ← no LLM call

4. The summary is fitted and injected; the digest is stored beside it.
   agent/context_strategy.py:310-316  conv.messages.insert(insert_at, summary_msg)
   agent/context_strategy.py:          conv.digest = digest

5. The conversation is saved.
   agent/persistence.py:  "digest": conv.digest.to_dict()

6. On load, the digest comes back.
   agent/persistence.py:  ConversationDigest.from_dict(data.get("digest") or {})
```

---

## 5. File change summary

| File | Change | Lines | Risk |
|---|---|---|---|
| `utils/tool_output_store.py` | **NEW** — the offload store | ~150 | Medium (new I/O path) |
| `agent/tools.py` | New `read_tool_output` tool + `_read_tool_output` | ~75 | Low |
| `agent/context_strategy.py` | Offload callback, `_stub_tool_result`, `_offloaded_stub`, digest parsing | ~120 | **High** (compaction core) |
| `models/conversation.py` | `ConversationDigest` dataclass + `Conversation.digest` | ~55 | Low |
| `agent/runtime.py` | `_resolve_offload_threshold`, `_offload_tool_output`, strategy wiring | ~55 | Medium |
| `agent/config.py` | 2 threshold fields + 2 load lines | ~12 | Low |
| `models/providers.py` | 1 field | ~4 | Low |
| `utils/providers_store.py` | 2 lines (serialize + load) | ~4 | Low |
| `agent/persistence.py` | Digest save + load | ~12 | Low |
| `ui/views/settings_dialog.py` | Offload-threshold spin (view) | ~20 | Low |
| `ui/handlers/settings_handler.py` | Persist the new field | ~6 | Low |
| `utils/agent_defs.py` | `read_tool_output` in the migration tuple | ~2 | Low |
| `prompts/default_agents/*.yaml` | 3 files, +1 line each | ~3 | Low |
| `docs/ARCHITECTURE.md` | §2, §3.21l/n/o/p.5, §11 | ~60 | Low |
| `docs/acceptance/rows.yaml` | Rows R-8, R-9, R-10 | ~20 | Low |
| `tests/mutations/R-8.diff`, `R-9.diff`, `R-10.diff` | 3 mutation diffs | ~60 | Low |
| `tests/test_spec48_retrievable_memory.py` | **NEW** — the tests | ~250 | Medium |

**Total: ~18 files, ~940 lines** (excluding the spec itself).

---

## 6. Implementation order

Each step is independently verifiable. **Do not start step N+1 until step N's verification passes.**

### Phase 1 — the store and the read-back tool (no compaction changes)

1. `utils/tool_output_store.py` — new module, exactly as §2.1 specifies.
2. `agent/tools.py` — the `read_tool_output` tool and `_read_tool_output`, exactly as §2.2 specifies, **including the `_ALREADY_WINDOWED` addition (C24)**.
3. `tests/test_spec48_retrievable_memory.py` — the store + tool tests (rows R-8, R-9 below).
4. **Fix C23** — add `read_tool_output` to the pinned tool-name set in `tests/test_spec24_facts.py:164`. *The Debugger's review found this tripwire red after the first Phase-1 attempt; it must be green.*
5. **Verify:** `.venv/bin/python -m pytest tests/test_spec48_retrievable_memory.py tests/test_spec24_facts.py -q` — all green; `ruff check` clean on every file touched (no new findings — `agent/tools.py` has 18 pre-existing ones that are not this spec's to fix).

### Phase 1b — the fix-round on Phase 1 (Debugger BUGs #1, #2, #3)

**The first Phase-1 attempt shipped three blocking bugs** and the Debugger found them by execution. This phase rewrites the store's read and the tool's body per §2.1/§2.2 as amended:

6. `utils/tool_output_store.py` — the read becomes **line-windowed** (BUG #1/#2), and both read and write get the **containment guard** `_store_realdir_inside_project` (BUG #3).
7. `agent/tools.py` — `_read_tool_output` mirrors `read_file`: it errors on a start line past the end, and the continuation notice comes from `total_lines`, not from a truncated body.
8. `tests/test_spec48_retrievable_memory.py` — new tests: a body **larger than 16 KB** is fully reachable; a start line past the end errors; a **symlinked store dir** is refused on write and on read; a symlinked file inside the store is refused on read.
9. **Re-run the spec harness with the two blind spots closed** — a >16 KB body and a symlinked store dir. *The original harness tested a 5 KB body and never symlinked anything, which is why BUGs #1 and #3 survived it (§11 row 6).*
10. **Verify:** `.venv/bin/python -m pytest tests/test_spec48_retrievable_memory.py tests/test_spec24_facts.py -q` — all green; `ruff check` clean on the files touched.

### Phase 2 — the offload in the compaction path

5. `agent/context_strategy.py` — `OffloadRequest`, the `offload` constructor parameter, `_stub_tool_result`, `_offloaded_stub`, and the two call sites (`:455`, `:511`).
6. `agent/runtime.py` — `_resolve_offload_threshold`, `_offload_tool_output`, the strategy wiring at `:790-791`.
7. `agent/config.py` + `models/providers.py` + `utils/providers_store.py` — the two threshold fields and their persistence.
8. **Verify:**
   - `.venv/bin/python -m pytest tests/test_context_strategy.py tests/test_context_strategy_audit_fixes.py tests/test_context_strategy_audit_fixes2.py tests/test_context_strategy_audit_fixes3.py tests/test_panic_prune.py tests/test_spec33b_exec_retention.py tests/test_spec33b_read_pins.py tests/test_compact_cache_discipline.py -q` — **all green** (this is the C1–C9 compatibility set; any failure here is a broken contract, not a flaky test).
   - The new offload tests — green.

### Phase 3 — the digest

9. `models/conversation.py` — `ConversationDigest` + `Conversation.digest`.
10. `agent/context_strategy.py` — `_digest_from_tagged_summary`, `_digest_from_snapshot`, `_digest_for_summary` (the tag-based tier decision), the **trimmable-region** snapshot in `compact()`, the digest computed from the **raw** summary before `_fit_summary`, and the `conv.digest = digest` assignment next to the summary injection (`:316`).
11. `agent/persistence.py` — save + load.
12. **Verify:** `.venv/bin/python -m pytest tests/test_conversation.py tests/test_llm_summarize_strategy.py tests/test_context_strategy.py -q` — all green.

### Phase 4 — the surface

13. `ui/views/settings_dialog.py` + `ui/handlers/settings_handler.py` — the spin and its persistence. **Per C25:** the field must be carried by `_collect_from_form` (`ui/views/settings_dialog.py:312`), by `ui/views/agent_builder.py:310`, and by `utils/providers_store.py:441` — `add_or_update` (`ui/handlers/settings_handler.py:151-155`) replaces the whole card, so a field missing from any of those resets to its default on every save. **The spin is a per-provider field; the global in `agent.json` is not surfaced in the UI in this phase** (§2.9).
14. `ui/agent_runtime/provider.py` — **Edit 4 (§2.7):** the global threshold must reach a running runtime without an app restart.
15. `utils/agent_defs.py` + `prompts/default_agents/*.yaml` — grant the tool.
16. **Verify:** `.venv/bin/python -m pytest tests/test_agent_defs.py tests/test_builtin_tool_migration.py tests/test_tools.py tests/test_spec24_facts.py -q` — **the same failures as before the change, and no new ones.** The tool-schema budget test is already red (C17); it must not get *new* failures, and the schema-size delta must be measured and reported. `tests/test_spec24_facts.py` must be **green** (C23, fixed in Phase 1).

### Phase 5 — the gate

17. `docs/acceptance/rows.yaml` — add R-8, R-9, R-10, R-11, R-12 (see §8). *R-11 and R-12 were added by the fix-round; they are the rows that keep BUGs #1/#2/#3 from returning.*
18. `tests/mutations/R-8.diff`, `R-9.diff`, `R-10.diff`, `R-11.diff`, `R-12.diff` — regenerate them the canonical way: **make the change in a scratch clone and `git diff` it.** Do not hand-transcribe — a hand-transcribed diff drifted on 3 of 6 rows in SPEC-47.
19. `docs/ARCHITECTURE.md` — the documentation updates.
20. **Verify:** `.venv/bin/python scripts/acceptance.py rows` — R-8, R-9, R-10, R-11, R-12 pass, and **the existing rows still pass**. A row whose mutation does not make its node fail is refused by the gate (that is the gate working).

**Phase 6 — the audit (not the implementer).** Hand the whole change to the Debugger for an adversarial audit, then to the PM.

---

## 7. Acceptance criteria

### 7.1 The build must satisfy these

- [ ] A tool result over the threshold is written to `<project>/.crabcakes/tool-outputs/` and replaced by a stub carrying its path.
- [ ] The stub still starts with `[compacted —` (C4, C5, C6).
- [ ] `read_tool_output` returns the offloaded body, windowed like `read_file` (at most 200 lines per call, any number of lines reachable by paging).
- [ ] `read_tool_output` refuses any path outside `.crabcakes/tool-outputs/`.
- [ ] A read of an offloaded output does **not** return the whole blob into the conversation (200-line window).
- [ ] A tool result **under** the threshold is stubbed exactly as before, byte-identical (C1).
- [ ] With `project_path=None`, the stub is byte-identical to the pre-SPEC-48 stub (C2, C19).
- [ ] With the threshold set to 0, offloading never happens and the behavior is exactly pre-SPEC-48.
- [ ] The per-provider threshold wins over the global; the global wins over 0 (off).
- [ ] A compacted conversation carries a `ConversationDigest` with the six fields.
- [ ] The digest survives a save/load round trip.
- [ ] A no-op compaction does **not** clear an existing digest (C11).
- [ ] `LLMSummarizeStrategy._summary` still returns the LLM response verbatim (C12).
- [ ] The tool-schema budget test has the same failure set as before the change (C17).
- [ ] `tests/test_spec24_facts.py::test_no_new_tool_name` is **green** — the new tool is in the pinned set (C23).

### 7.1a The fix-round criteria (Debugger review, 2026-10-21)

These were the five blocking findings. All are now **required**:

- [ ] **Bytes past 16 KB are reachable.** A body larger than 16 KB is fully readable: `read_tool_output` with `line=N` returns the lines at N, and the continuation notice names the next line. The old shape (read 16 KB, then window the truncated body) is gone (BUG #1).
- [ ] **A start line past the end errors**, mirroring `read_file` (`agent/tools.py:533-537`): `success=False`, "line N is past the end of …". It does **not** return `success=True` with an empty body (BUG #2).
- [ ] **The store cannot be written outside the project.** With `.crabcakes/tool-outputs` symlinked to a directory outside the project, `offload_tool_output` returns `None` and `read_tool_output` returns an error — the write does not land outside (BUG #3).
- [ ] **The digest describes the compacted-away region.** The snapshot covers the trimmable region only (`keep_first` .. `len - tail_preserve`), not the kept head and tail (BUG #4).
- [ ] **The digest tier is decided by the summary's tags, not the strategy class.** An LLM strategy that falls back to the textual summary produces a Tier-1 digest (`arc` + `paths_touched` + `attempts_rejected`), not an empty one.
- [ ] **Tier 1 never guesses.** `decisions`, `constraints`, and `open_questions` stay empty in Tier 1.
- [ ] **The digest is computed from the raw summary**, before `_fit_summary` truncates it.
- [ ] The per-provider threshold survives a Settings save (C25: `_collect_from_form`, `agent_builder.py:310`, `providers_store.py:441`).
- [ ] The global threshold reaches a running runtime without an app restart (Debugger #9).

### 7.2 The verification must be runnable

```bash
# Targeted suites (the files this spec touches)
.venv/bin/python -m pytest tests/test_spec48_retrievable_memory.py -q
.venv/bin/python -m pytest tests/test_context_strategy.py tests/test_context_strategy_audit_fixes.py \
    tests/test_context_strategy_audit_fixes2.py tests/test_context_strategy_audit_fixes3.py \
    tests/test_panic_prune.py tests/test_spec33b_exec_retention.py tests/test_spec33b_read_pins.py \
    tests/test_compact_cache_discipline.py tests/test_conversation.py \
    tests/test_llm_summarize_strategy.py tests/test_agent_defs.py \
    tests/test_builtin_tool_migration.py -q

# The tool-name tripwire (C23) — must be GREEN
.venv/bin/python -m pytest tests/test_spec24_facts.py -q

# The gate
.venv/bin/python scripts/acceptance.py rows

# Lint
.venv/bin/python -m ruff check utils/tool_output_store.py agent/tools.py agent/context_strategy.py \
    models/conversation.py agent/runtime.py agent/config.py models/providers.py \
    utils/providers_store.py agent/persistence.py ui/views/settings_dialog.py \
    ui/handlers/settings_handler.py utils/agent_defs.py
```

**Full-suite runs require explicit PM permission** (standing rule in `.crabcakes/context.md`). The implementer does not run the full suite.

---

## 8. Acceptance rows (SPEC-47)

Registered at spec-write time, per SPEC-47 §7: "a spec that cannot name its mutation has not defined its claim."

| Row | Claim | Node | Mutation | Registered |
|---|---|---|---|---|
| R-8 | "A tool result over the threshold is offloaded to disk and replaced by a stub that carries the path; the bytes are recoverable by that path." | `tests/test_spec48_retrievable_memory.py::TestOffloadRoundTrip::test_oversized_result_is_offloaded_and_recoverable_by_path` | `tests/mutations/R-8.diff` | 2026-10-21 |
| R-9 | "A tool result under the threshold (or with no project) is stubbed byte-identically to the pre-SPEC-48 stub — offloading never changes the plain-stub contract." | `tests/test_spec48_retrievable_memory.py::TestOffloadThreshold::test_under_threshold_keeps_the_plain_stub` | `tests/mutations/R-9.diff` | 2026-10-21 |
| R-10 | "A compacted conversation exposes its digest fields (decisions, open_questions, …) and the digest survives a save/load round trip." | `tests/test_spec48_retrievable_memory.py::TestDigest::test_digest_survives_save_load_round_trip` | `tests/mutations/R-10.diff` | 2026-10-21 |
| R-11 | "Every byte of an offloaded body is reachable: a body larger than 16 KB can be paged line by line, and a start line past the end is an error — never an empty success." | `tests/test_spec48_retrievable_memory.py::TestReadBack::test_large_body_is_fully_reachable_by_line` | `tests/mutations/R-11.diff` | 2026-10-21 |
| R-12 | "The store cannot be written outside the project: a symlinked store dir is refused on write and on read, and nothing lands outside." | `tests/test_spec48_retrievable_memory.py::TestStoreContainment::test_symlinked_store_dir_is_refused` | `tests/mutations/R-12.diff` | 2026-10-21 |

**Row semantics (from `scripts/acceptance.py:204-264`):** each row runs its node **mutated-then-clean** inside a throwaway temp copy. The **mutated** run MUST fail — a pass means the node cannot see the behavior, and the gate refuses the row. The **clean** run MUST pass. A skipped node fails the row unless the row sets `skip_ok: true`.

**Runner:** all five rows are `runner: plain` — no GTK, no display needed. (A GTK-dependent row node would need `runner: xvfb`, because `tests/conftest.py` skips widget-building modules when no display is present.)

**Mutation guidance (the SPEC-47 lesson):**
- **R-8** — delete the offload call in the runtime closure (make `_offload_tool_output` always return `None`). The node must fail because the stub would carry no path.
- **R-9** — make the offload path also apply under the threshold (drop the size check). The node must fail because the plain stub would be replaced by a path-carrying one.
- **R-10** — drop the digest from the persistence payload (remove the `"digest"` key from the saved dict). The node must fail because the digest would not survive the round trip.
- **R-11** — revert the read to the Phase-1 shape (read the first 16 KB, then window the truncated body). The node must fail because the lines past 16 KB become unreachable and the past-the-end read returns an empty success. *This row is the guard against BUG #1/#2 returning.*
- **R-12** — delete the containment guard (`_store_realdir_inside_project` returns the store unconditionally). The node must fail because the write lands outside the project. *This row is the guard against BUG #3 returning.*

**Regenerate the diffs canonically** — edit in a scratch clone, then `git diff`. A hand-transcribed diff drifted on 3 of 6 SPEC-47 rows.

---

## 9. Edge cases

| Case | Expected behavior |
|---|---|
| `conv.project_path` is `None` | No offload. The plain stub is written (C2, C19). |
| The project directory is not writable | `offload_tool_output` returns `None` (it catches `OSError`). The plain stub is written. The turn does not fail. |
| `.crabcakes/tool-outputs/` does not exist | Created on demand by `offload_tool_output` (`os.makedirs(..., exist_ok=True)`). |
| The same content is offloaded twice | One file (sha256-prefixed name). The second call overwrites the same path. |
| The body is exactly at the threshold | **Not** offloaded. The rule is `len(content) > threshold` (strictly greater). |
| The threshold is 0 | Offloading is disabled entirely — the pre-SPEC-48 behavior. |
| The threshold is negative | Treated as 0 (off). `int(... or 0)` in the loader and `threshold <= 0` in the resolver. |
| `read_tool_output` is given a path outside the store | Refused. `success=False`, error naming the path. **Even a legitimate project file is refused** — it is not a store path. |
| `read_tool_output` is given `../` traversal | Refused by `_is_store_path` (the name may not contain `/`). |
| The offloaded file was deleted by hand | `read_tool_output` returns `found=False` with an error naming the path. |
| The offloaded file is larger than 16 KB | **Fully readable, line by line.** The read is line-windowed, not byte-capped: `line=N` returns the lines at N, and the continuation notice names the next line. The tool never returns more than 200 lines at once, and the conversation never receives the whole blob. |
| `read_tool_output` is called with `line=0` or `line=-1` | Refused: "line must be an integer >= 1" (same rule as `read_file`). |
| `read_tool_output` is called with `max_lines=0` | Refused: "max_lines must be an integer >= 1". |
| `read_tool_output` is called with `max_lines=5000` | Clamped to `LINE_WINDOW` (200). |
| The strategy is constructed without the offload callback | `DefaultContextStrategy()` still works — `self._offload is None` ⇒ the plain stub. **All existing tests construct it this way**, so they must keep passing unchanged. |
| `LLMSummarizeStrategy` is used | Its `__init__` must gain the `offload` parameter and forward it to `super().__init__(offload=offload)` (§2.6.1) — it currently calls `super().__init__()` with no args (`:977`). `runtime.py:4703` must also pass `offload=self._offload_tool_output`, or the forced-compact path silently loses offloading. **Both edits are required, not optional.** |
| The LLM summary is missing some tags | Those digest fields are empty. The summary text itself is unchanged (C12). |
| The LLM summary is the fallback stub `[Context reset — …]` | The digest still holds the structured fields (C13). |
| The trimmed messages contained no assistant text | `decisions` and `constraints` are empty; `paths_touched` may still have entries. |
| The trim removed messages but no summary was injected (budget too small) | The digest is **not** set (C11). The previous digest is left alone. |
| Two agents compact the same project concurrently | Both write to the same store dir. Filenames are content-hashed, so identical content is one file; different content gets different files. No lock is needed for correctness of the *name*; a torn write is possible in theory, so the store writes to a temp file and `os.replace`s it (atomic on POSIX). |
| The tool name contains `..` or `/` | The name is sanitized to `[A-Za-z0-9_.-]` with everything else replaced by `_`. The result is a flat filename, and the resolved file's parent is the real store dir. A `..` **substring** in the name is harmless and is not treated as an error — the invariant is "stays inside the store dir", not "contains no `..`". |
| The project is deleted / moved | The store goes with it. `read_tool_output` then reports `found=False`. |
| The store dir is a **symlink** to a directory outside the project | **Refused.** `offload_tool_output` returns `None` (the plain stub is written); `read_tool_output` returns an error. The write must not land outside the project (BUG #3). |
| The offloaded file is a **symlink** pointing outside the store | Refused on read (the resolved parent is not the store dir) and on write (the final name's realpath is checked before `os.replace`). |
| The offloaded body is **larger than 16 KB** | Fully readable. The read is line-windowed, not byte-capped: `line=N` returns the lines at N, and the continuation notice names the next line. The tool never returns more than 200 lines. |
| `read_tool_output` is given a `line` past the end of the file | `success=False`, "line N is past the end of …" — mirroring `read_file` (`agent/tools.py:533-537`). Not an empty success. |
| The LLM summary fell back to the **textual** summary (no provider, empty response, or an exception) | Tier 1: `arc` + `paths_touched` + `attempts_rejected` are populated; `decisions`/`constraints`/`open_questions` stay empty. The digest is **not** silently empty. |
| The LLM summary is truncated by `_fit_summary` | The digest was computed from the raw summary, before fitting — the tags were still present. |

---

## 10. ARCHITECTURE.md updates required

| Section | What to add |
|---|---|
| §2 (directory structure) | `utils/tool_output_store.py` |
| §3.21l `models/conversation.py` | The `ConversationDigest` dataclass and the `Conversation.digest` field |
| §3.21n `agent/tools.py` | `read_tool_output` in the public API list |
| §3.21o `agent/config.py` | `tool_output_offload_chars` on both `AgentConfig` and `LLMProviderConfig` |
| §3.21p.5 `agent/context_strategy.py` | The `offload` callback parameter on `DefaultContextStrategy.__init__`; the offload-then-stub behavior of `_stub_tool_result`; the digest assignment next to the summary injection; the `OffloadRequest` dataclass; **the trimmable-region snapshot scope and the tag-based tier decision (`_digest_for_summary`)** |
| §3.21p.6 `ui/agent_runtime/provider.py` | **Edit 4 (§2.7):** the global `tool_output_offload_chars` is copied onto each runtime's config on refresh, so it takes effect without an app restart |
| §3.22 (new, after `utils/feed_store.py`'s §3.22d) | `utils/tool_output_store.py` — responsibility, manifest, public API |
| §11 (file inventory) | `utils/tool_output_store.py` |

**Rule (ARCHITECTURE.md §0):** "If the diff of your code change doesn't have a corresponding update to this file, the change is **incomplete**."

---

## 11. Self-audit (Rule 9)

| # | Question | Answer |
|---|---|---|
| 1 | Does every code sample actually work against the current codebase? | Every signature was read from source: `prune_tool_outputs` (`:359`), `panic_prune` (`:485`), `_compacted_stub` (`:477`), `compact` (`:122`), `_summary` (`:855`, `:981`), `SUMMARY_PROMPT_TEMPLATE` (`:949`), `_fit_summary` (`:728`), `_compute_compaction_threshold` (`runtime.py:1699`), `_compact_before_llm` (`runtime.py:4067`), `AgentConfig` (`config.py:76`), `LLMProviderConfig` (`config.py:31`), `ProviderConfig` (`models/providers.py:39`), `_to_dict`/`_from_dict` (`providers_store.py:55`, `:136`), `Message` (`models/conversation.py:123`), `Conversation` (`:251`), `ToolResult` (`tools.py:46`), `ToolDefinition` (`tools.py:37`), `_read_file` (`tools.py:476`), `_numbered_line_block` (`tools.py:468`), `_resolve_project_path` (`tools.py:406`), `get_tool_definitions_for_api` (`tools.py:2369`), `get_all_tools` (`:2359`), `ensure_builtin_tool` (`utils/agent_defs.py:189`), `save_conversation_to_disk` (`persistence.py:60`), `_conversation_from_data` (`persistence.py:150`). The `OffloadRequest` and `ConversationDigest` dataclasses are new and were written to match the house style. **Corrections from the Debugger's review (2026-10-21): `_provider_from_dict` → `_from_dict` (`providers_store.py:136`); `agent_defs.py:169-179` → `ensure_builtin_tool` (`:189`) as the function to cite; and the spec's claim that `_ALREADY_WINDOWED` is unchanged was wrong (C24) — it must gain the new tool.** |
| 2 | Did I catch all exception types for every function I call? | `offload_tool_output` catches `OSError` internally and never raises; `_offload_tool_output` also wraps the call in `try/except Exception` and logs. `read_tool_output` returns an `error` string instead of raising. `_read_tool_output` therefore has no unhandled path. `_resolve_offload_threshold` wraps the provider lookup in `try/except Exception` with a `logger.debug` — mirroring `_compute_compaction_threshold`'s own defensive shape (`runtime.py:1727-1737`). |
| 3 | Did I verify key structures, not assume them? | Yes. The `Conversation` field list was read (`models/conversation.py:251-290`); `project_path` is `str | None = None`. The `ProviderConfig` field list was read (`models/providers.py:39-58`). The `providers_store` serialize/load pair was read (`:55-75`, `:158-176`) and the new field mirrors `compaction_threshold` exactly. The `providers.yaml` key names are the `_to_dict` keys. **The Debugger additionally verified the provider-key resolution live: `agent/config.py:186-193` registers each provider under both its model-prefix id and its display name (`'M3' → pid='minimax'`), so `conv.model="minimax/..."` → `providers.get("minimax")` resolves. Caveat: two cards sharing a prefix (`'GLM 5.3'`, `'GLM 5.3 Flash'` → both `'zai'`) collide and the last wins — pre-existing for `compaction_threshold`; SPEC-48 inherits it and does not fix it.** |
| 4 | Did I trace the data flow end-to-end? | Yes — §4.1 and §4.2 trace both paths from the tool result to the stub to the read-back, and from the trim to the digest to the disk round trip. |
| 5 | Would an implementer who follows this spec exactly produce working code? | Yes, with the fix-round applied. The compatibility table (§3) lists **26** contracts a naive implementation would break, each with its file:line and the test that pins it. **The Debugger's review proved the point empirically: the first draft's Phase-1 code shipped BUGs #1/#2/#3 and made `tests/test_spec24_facts.py::test_no_new_tool_name` (C23) red — a contract the spec had not listed.** The phase order (§6) puts the highest-risk change (the compaction core) in Phase 2 with the compatibility test set named in the verification command. |
| 6 | Are the code samples lint-clean? | **Re-verified by execution after the fix-round.** The amended §2.1 store and §2.2 tool body were extracted verbatim into `scratch/spec48_spec_audit2.py` and run: **47 checks, all PASS** — including the two cases round 1 missed: a **500-line / >16 KB body** (lines 400–500 are reachable; a start line past the end **errors**; the continuation names line 201) and a **symlinked store dir** (the write is refused, nothing lands outside, the read is refused). A symlinked *file* inside the store is refused too. The round-1 harness (`scratch/spec48_spec_audit.py`, 45 checks) tested a 5 KB body and never symlinked anything, which is why BUGs #1 and #3 survived it — the lesson is recorded in §6 Phase 1b step 9. The amended samples are lint-clean (ruff, py_compile). The spec's Phase-2 samples leave `original_len`/`n_chars` unused (ruff **F841**) — the implementer must delete those names (§2.6.1). Known repo rules: `EXE001` (shebang needs the executable bit) and `PLW1510` (`subprocess.run` needs an explicit `check=`) — neither applies. The `→` character in the offloaded stub is inside a string literal, so no encoding rule applies. |
| 6a | Did the audit find defects in this spec? | **Yes — eight, all now fixed.** (a) The first draft put the threshold check *inside* the strategy and invented two runtime accessors (`_active_project_path`, `_current_conv`) that do not exist; the final design keeps the threshold in the runtime and passes `conv` to the callback. (b) `LLMSummarizeStrategy.__init__` (`:972-979`) calls `super().__init__()` with no args — the spec now requires the `offload` parameter to be added and forwarded, or the forced-compact path silently loses offloading. (c) `OffloadRequest` and `offload_tool_output` carried a phantom `session_key` that no caller has in scope — removed. (d) The digest's tier-1 assumed a `removed` list that does not exist — the trim loop pops in place (`:198`, `:209`, `:222`, `:267`, `:268`, `:270`) and never collects them. **Debugger review (fix-round 2026-10-21) added four more:** (e) **BUG #1/#2** — the Phase-1 read shape made every byte past 16 KB unreachable *silently*; the store now owns line-windowed reads and the tool mirrors `read_file`'s past-the-end error. (f) **BUG #3** — a symlinked store dir let the write land outside the project; both write and read now verify containment. (g) **BUG #4** — the digest snapshot covered the whole conversation, describing what was *kept*; it is now scoped to the trimmable region. (h) The tier assignment was by strategy class, so an LLM fallback to the textual summary silently emptied the digest; the tier is now decided by the summary's tags, and Tier 1 populates only the fields it can honestly derive. |
| 6b | Did the audit's own checks have bugs? | **Yes — two in the audit, and later two in the spec's claims, all now fixed.** `len(x) > threshold is False` is a *chained* comparison (`>` and `is`), so the threshold-boundary checks were silently vacuous; rewritten with explicit parens. And the "no `..` in the basename" check asserted an invariant the design never claimed — `.` is a legal filename character and a flat name cannot traverse; rewritten to assert the real invariant (the resolved file's parent is the store dir). **The Debugger's review then showed the harness had two blind spots** (a sub-16 KB body, no symlink case) — the lesson is recorded in §2.1 and §11 row 6: the re-run must use a >16 KB body and a symlinked store. |
| 7 | Is the spec's own claim set honest? | The spec says the tool-schema budget test is **already failing** and must not get *new* failures — it does not claim the budget is fixed. It does not claim the forced-compact path offloads until §9 says explicitly that `runtime.py:4703` must also be updated. It does not claim a global UI for the threshold — §2.9 says the global lives in `agent.json` in this phase. |
| 8 | Is the mutation guidance sound? | Each of R-8, R-9, R-10 names a specific deletion that must make its node fail. R-9's mutation (drop the size check) is the one that also proves the *threshold* is honored, not just that offloading works — it is the row that would catch a hardcoded "always offload". **R-11 and R-12 (fix-round) are the rows that keep the shipped bugs from returning: R-11's mutation reverts the read to the Phase-1 shape (bytes past 16 KB unreachable again), R-12's mutation removes the containment guard (the write lands outside the project again).** A row whose mutation does not make its node fail is refused by the gate — that is the gate working, not a nuisance. |

---

## 12. What this spec does not decide

- **The global-vs-per-provider UI.** The per-provider spin ships; the global lives in `agent.json` in this phase.
- **Whether the store should be indexed.** A directory listing of the store, or a tool to search it, is out of scope. The agent reads back by exact path from the stub.
- **Whether the digest should ever be shown to the user.** It is machine-facing in this phase.
- **T1.1 and T1.5.** Deferred by `PROPOSAL-META.md` §3 ("Left behind").
- **The tool-schema budget.** Known debt, tracked in `tests/known_failures.txt`; a deliberate revisit, not this spec.
- **Whether offloaded outputs should expire.** No TTL in this phase; the store is per-project and gitignored.
