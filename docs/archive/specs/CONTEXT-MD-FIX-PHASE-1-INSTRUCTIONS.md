# Phase 1 — Context.md Read Path (utils/project_awareness.py)

**Spec:** `docs/specs/SPEC-CONTEXT-MD-SYSTEM-FIX.md` §3.1a, §3.1b, §3.1c
**File:** `utils/project_awareness.py` (ONLY this file — do not touch any other file)
**Goal:** Increase read cap, add `get_current_task()`, wire `CURRENT_TASK` into the awareness dict.

Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`. Read this file in FULL before editing. Read `utils/project_awareness.py` in FULL before editing.

---

## Edit 1 — Add two new constants (§3.1a)

Find this line (near line 57):

```python
MAX_CONTEXT_SIZE = 50 * 1024  # 50 KB cap for context.md
```

Add TWO new constants immediately after it:

```python
MAX_CONTEXT_SIZE = 50 * 1024  # 50 KB cap for context.md (write side)
CONTEXT_READ_CAP = 8000       # Max chars injected into agent prompts (read side)
MAX_CONTEXT_ENTRIES = 50      # Max entries before FIFO eviction
```

---

## Edit 2 — Replace read cap at site 1: `build_awareness_block` (§3.1a)

In `build_awareness_block()`, find the "Persistent context" section. It currently has:

```python
        context_wrapped = _untrusted_fence(
            context[:3000], "context.md"
        )
        if len(context) > 3000:
            context_wrapped += "\n[... context memory truncated ...]"
```

Replace with:

```python
        context_wrapped = _untrusted_fence(
            context[:CONTEXT_READ_CAP], "context.md"
        )
        if len(context) > CONTEXT_READ_CAP:
            context_wrapped += "\n[... context memory truncated ...]"
```

---

## Edit 3 — Replace read cap at site 2: `build_awareness_dict` (§3.1a)

In `build_awareness_dict()`, find the "Project memory" section. It currently has:

```python
        context_wrapped = _untrusted_fence(context[:3000], "context.md")
        if len(context) > 3000:
            context_wrapped += "\n[... context memory truncated ...]"
```

Replace with:

```python
        context_wrapped = _untrusted_fence(context[:CONTEXT_READ_CAP], "context.md")
        if len(context) > CONTEXT_READ_CAP:
            context_wrapped += "\n[... context memory truncated ...]"
```

---

## Edit 4 — Add `get_current_task()` function (§3.1b)

Add this NEW function. Place it in the "Context memory" section, immediately AFTER `append_project_context()` and BEFORE the "Awareness snapshot" section header.

```python
def get_current_task(project_path: str) -> str:
    """Extract the most recent dated entry heading from context.md.

    Returns the heading text (e.g., "2026-07-20 — Phase B6 complete") or
    empty string if context.md is empty or has no '## ' headings.

    This is injected as a TRUSTED directive (outside the untrusted fence)
    so agents treat it as an operational instruction, not data.
    """
    context = load_project_context(project_path)
    if not context.strip():
        return ""
    # Find the last '## ' heading
    headings = [line for line in context.split("\n") if line.startswith("## ")]
    if not headings:
        return ""
    return headings[-1][4:].strip()  # strip "## " prefix
```

---

## Edit 5 — Add `CURRENT_TASK` to `build_awareness_dict` (§3.1c)

In `build_awareness_dict()`, find the "Project memory" block (the one you just edited in Edit 3). Immediately AFTER the entire Project memory block (after the `else: parts["PROJECT_MEMORY"] = ""` line), and BEFORE the "Workflow status" block, add:

```python
    # Current task — extracted from the latest context.md heading.
    # Injected as TRUSTED data (not in the untrusted fence) because it is
    # an operational directive the agent must follow.
    task = get_current_task(project_path)
    parts["CURRENT_TASK"] = task if task else "(no current task recorded)"
```

**CRITICAL:** This MUST be placed BEFORE the cache write block at the end of the function. The cache stores the `parts` dict — if `CURRENT_TASK` is added after the cache write, cache hits will not include it. The correct insertion point is between the PROJECT_MEMORY block and the WORKFLOW_STATUS block.

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- Do NOT add `import re` — it is already imported at module level (line 15).
- Do NOT touch `append_project_context` — that is Phase 2.
- Do NOT touch any other file. Only `utils/project_awareness.py`.

---

## Verification

Run these commands and paste the FULL output:

1. `grep -n "CONTEXT_READ_CAP" utils/project_awareness.py` — expect ≥ 5 matches (1 def + 2 uses in build_awareness_block + 2 uses in build_awareness_dict)
2. `grep -c "context\[:3000\]" utils/project_awareness.py` — expect **0**
3. `grep -n "get_current_task" utils/project_awareness.py` — expect ≥ 3 matches (1 def + 1 call in build_awareness_dict + possibly docstring refs)
4. `grep -n "CURRENT_TASK" utils/project_awareness.py` — expect ≥ 2 matches (in build_awareness_dict)
5. `python3 -c "from utils.project_awareness import get_current_task, CONTEXT_READ_CAP, MAX_CONTEXT_ENTRIES; print('OK', CONTEXT_READ_CAP, MAX_CONTEXT_ENTRIES)"`
6. `python3 -m pytest tests/test_project_awareness.py -v` — all existing tests must pass

---

## COMPLETENESS checklist (mandatory — report this block in your reply)

```
COMPLETENESS:
- [x/not done] Edit 1: Added CONTEXT_READ_CAP and MAX_CONTEXT_ENTRIES constants — evidence: <grep output>
- [x/not done] Edit 2: Replaced [:3000] in build_awareness_block — evidence: <grep output>
- [x/not done] Edit 3: Replaced [:3000] in build_awareness_dict — evidence: <grep output>
- [x/not done] Edit 4: Added get_current_task() function — evidence: <grep output>
- [x/not done] Edit 5: Added CURRENT_TASK to build_awareness_dict (before cache write) — evidence: <grep output>
- [x/not done] All existing tests pass — evidence: <pytest output>
```
