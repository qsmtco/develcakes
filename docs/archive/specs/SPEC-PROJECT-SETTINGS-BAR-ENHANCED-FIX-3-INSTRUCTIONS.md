# Spec Fix Round 3 — Instructions for Coder

**Spec to revise:** `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-2.md` (Round 2 fix)
**Round 3 findings to address:** `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-2-FINDINGS.md` (7 new bugs: 1 CRIT, 3 HIGH, 3 MED)
**Prompt to load:** `prompts/steelFramedSpecWriter.md`
**Working dir:** `/path/to/projects/crabcakes`

## Task

Revise the spec to address the **7 Round 3 findings**. Produce a new spec at `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-3.md` that supersedes `...-FIX-2.md`. Each finding must have a concrete code-level fix in the spec.

## Critical process note

**CRITICAL FINDING (BUG #1) IS A SYNTAX ERROR IN THE PREVIOUS SPEC.** FIX-2 instructed the implementer to add assignment statements inside an existing tuple-returning lambda — that's a `SyntaxError`. The new spec must contain **only valid, executable Python** in every code sample. Per `steelFramedSpecWriter.md` Rule 1, every line of sample code must be runnable. Add a verification step at the end: parse every code block in the spec with `ast.parse()` and confirm zero errors.

## Findings to address (in priority order)

### CRITICAL — BUG #1: project-close invalidation cannot be inserted into existing tuple lambda

**Fix required in the new spec:**

Replace the lambda-insertion instructions with one of:
- A named `_on_project_closed(name)` method on `MainWindow` that the existing `set_on_project_closed(...)` registration calls as a single callable. The named method can contain the assignment statements.
- A dedicated `ProjectHandler` close-callback helper that performs invalidation as a method (not a lambda).
- If the spec needs to keep the existing tuple-lambda structure, the invalidation must happen **before** the lambda is registered, not inside it.

Show the exact existing callback registration site and the new wiring that calls the named method. Add a regression test: open project A, close it, assert `_branch_active_token is None` and `_cached_branch is None`.

### HIGH — BUG #2: stale branch results can write `_cached_branch` before active-project check

**Fix required in the new spec:**

Restructure `_on_branch_result()` to perform the active-project identity check **before** mutating any state:

```python
def _on_branch_result(self, project_name, project_path, token, branch):
    # Drop stale results first
    if token != self._branch_request_token:
        return
    if project_path != self._branch_request_path:
        return
    current_name = self._project_handler.get_active_project_name()
    if current_name != project_name:
        return
    current_path = self._project_handler.get_active_project_path()
    if current_path != project_path:
        return
    # All checks pass — safe to commit
    self._cached_branch_for_path[project_path] = branch
    self._branch_active_token = None
    self._on_feed_bar_update(current_name, ...)
```

And: add explicit token/cache invalidation on **project open** and **project switch**, not just on project close. Show the exact `_on_project_opened` lifecycle wiring.

### HIGH — BUG #3: `set_solo_target()` doesn't validate the project name

**Fix required in the new spec:**

The implementation should match its stated contract. Pick one and document:

**Option A — strict (recommended):** validate the project exists before mutating:
```python
def set_solo_target(self, project_name, member_session_key):
    if project_name not in self._projects:
        return  # unknown project
    old = self._solo_targets.get(project_name)
    if old == member_session_key:
        return
    self._solo_targets[project_name] = member_session_key
    if self._on_solo_target_changed is not None:
        self._on_solo_target_changed(project_name)
```

**Option B — loose:** explicitly state the contract is "any named project" and have the window callback guard against non-active projects:
```python
# In ProjectHandler.set_solo_target — no validation
# In window callback:
def _on_solo_target_changed(self, project_name):
    if self._project_handler.get_active_project_name() != project_name:
        return
    # ... refresh bar
```

Pick A. Add a regression test: `set_solo_target("nonexistent", "agent:x")` is a no-op (or returns False).

### HIGH — BUG #4: bar doesn't update after async auto-accept warning confirmation

**Fix required in the new spec:**

Add an `on_auto_accept_level_changed` callback to `FeedHandler`. The commit path (`_commit_auto_accept_level`) fires it after `_refresh_auto_accept_state()`. The window wires it to refresh the bar:

```python
# In FeedHandler
self._on_auto_accept_level_changed: Callable[[str], None] | None = None

def set_on_auto_accept_level_changed(self, cb):
    self._on_auto_accept_level_changed = cb

def _commit_auto_accept_level(self, level):
    # ... existing commit logic
    self._refresh_auto_accept_state()
    if self._on_auto_accept_level_changed is not None:
        self._on_auto_accept_level_changed(level)
```

And in window.py:
```python
self._feed_handler.set_on_auto_accept_level_changed(
    lambda level: self._on_feed_bar_update(
        self._project_handler.get_active_project_name() or "",
        len(self._project_handler.get_project_members(
            self._project_handler.get_active_project_name() or ""
        )) if self._project_handler.get_active_project_name() else 0,
        auto_accept_level=level,
    )
)
```

Important: `_on_autoaccept_cycle_clicked` should **not** optimistically rebuild the bar before confirmation. It only calls `set_auto_accept_level(next_level)` (which displays the warning and waits). The bar update happens in the callback after the user confirms.

Add a regression test: click `off → files`, assert bar still shows "off" until confirmation, then assert bar shows "files" after confirmation.

### MEDIUM — BUG #5: `_cached_branch` not keyed by project

**Fix required in the new spec:**

Replace the single `_cached_branch` field with `_cached_branch_by_path: dict[str, str]`. On project open, clear the relevant entries. The `get_active_project_path()` lookup determines which cache entry to display.

Add a regression test: open A (resolves to "main"), open B (resolves to "feature"), assert A's bar still shows "main" if you switch back.

### MEDIUM — BUG #6: branch refresh condition doesn't check cache ownership

**Fix required in the new spec:**

Add a cache check to the scheduling condition:

```python
cached_for_active = self._cached_branch_by_path.get(
    self._project_handler.get_active_project_path() or ""
)
should_refresh = branch_name is None and (
    cached_for_active is None
    or self._branch_active_token is None
)
```

But the `and self._branch_active_token is None` is the "is a worker already running" check, not the "is cache valid" check. Separate them:

```python
needs_resolution = branch_name is None and cached_for_active is None
already_running = self._branch_active_token is not None
if needs_resolution and not already_running:
    self._schedule_branch_refresh(...)
```

Add a regression test: after branch is cached, no new worker is scheduled for the same project.

### MEDIUM — BUG #7: special-agent fallback returns `None` for empty values

**Fix required in the new spec:**

Use `.get()` with truthiness check:

```python
def _resolve_agent_display_name(self, session_key: str) -> str:
    if self._agent_mgr is not None:
        name = self._agent_mgr.get_name(session_key)
        if name:
            return name
    if self._agent_runtime_handler is not None:
        special = self._agent_runtime_handler.get_special_agents()
        name = special.get(session_key)
        if name:
            return name
    return session_key
```

Add a regression test: special-agent mapping with `{"special:x": ""}` returns `"special:x"` (not `""`).

## Process requirements

1. **Read every file you reference.** Per `steelFramedSpecWriter.md` Rule 1.
2. **Verify every claim empirically.** Don't trust memory; use `read_file`, `search_files`, `exec_command`.
3. **CRITICAL: every code sample must be syntactically valid Python.** Add a verification step: `python3 -c "import ast; [ast.parse(block) for block in code_blocks_in_spec]"` (or equivalent) and confirm zero errors. Report the verification output in COMPLETENESS.
4. **Update §9 (traceability table)** to map each of the 7 Round 3 findings to the fix.
5. **Section structure:** keep the same 10-section template from FIX-2.
6. **File name:** write to `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-3.md`. Do NOT write to a different path.
7. **Update `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-LOOP-STATE.md`** with Round 3 status.

## What to skip

- Do NOT modify the original spec, FIX-1, or FIX-2.
- Do NOT implement any code. Spec only.

## Deliverable

- `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-3.md` (new spec, supersedes FIX-2)
- `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-LOOP-STATE.md` (updated)
- COMPLETENESS checklist with verification evidence
- **`ast.parse()` verification of every code block** in the new spec

## COMPLETENESS format (required)

```
COMPLETENESS:
- [x] Edit 1: [description] — evidence
...
- [x] Section 9 updated with 7-finding traceability — [link/anchor]
- [x] ast.parse() verification of all code blocks: zero SyntaxError — [output]
- [x] Empirical probe: ProjectHandler set_on_project_closed current signature — [output]
- [x] Empirical probe: AgentRuntimeHandler.get_special_agents() return shape — [output]
- [x] Empirical probe: FeedHandler._refresh_auto_accept_state current behavior — [output]
```

Please write when done.
