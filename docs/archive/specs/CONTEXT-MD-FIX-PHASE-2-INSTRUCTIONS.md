# Phase 2 — CURRENT_TASK wiring (prompt_loader + template)

**Spec:** `docs/specs/SPEC-CONTEXT-MD-SYSTEM-FIX.md` §3.2, §3.3
**Files:** `utils/prompt_loader.py` + `prompts/system/project-awareness.md` (2 files)
**Goal:** Wire `CURRENT_TASK` from `build_awareness_dict()` into the system prompt as a TRUSTED directive (outside the untrusted fence).

**FIRST ACTION: Load the steelFramedCodeWriter prompt fresh.** Read `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md` IN FULL before writing any code. Activate it. Begin your response with "Starting Discovery Phase — reading all relevant files before writing any code." and output a DISCOVERY block. Follow all 8 core rules.

Read BOTH files in full before editing.

---

## Edit 1 — `utils/prompt_loader.py`: add `CURRENT_TASK` to variables dict

In `compose_system_prompt()`, find the `variables = {...}` block (around line 295-310). It currently has:

```python
    variables = {
        "AGENT_NAME": agent_name or "",
        "AGENT_TYPE": agent_type,
        "AGENT_TYPE_DESC": agent_type_desc,
        "PROJECT_PATH": project_path or "(no project open)",
        "PROJECT_NAME": awareness.get("PROJECT_NAME", ""),
        "TEAM_ROSTER": awareness.get("TEAM_ROSTER", ""),
        "CURRENT_STATE": awareness.get("CURRENT_STATE", ""),
        "PROJECT_MEMORY": awareness.get("PROJECT_MEMORY", ""),
        "WORKFLOW_STATUS": awareness.get("WORKFLOW_STATUS", ""),
        "REVIEW_MODE": review_mode,
        "TOOL_LIST": tool_list_str,
    }
```

Add `"CURRENT_TASK": awareness.get("CURRENT_TASK", ""),` to the dict. Place it AFTER `"PROJECT_MEMORY"` and BEFORE `"WORKFLOW_STATUS"` (mirroring the order in `build_awareness_dict`):

```python
    variables = {
        "AGENT_NAME": agent_name or "",
        "AGENT_TYPE": agent_type,
        "AGENT_TYPE_DESC": agent_type_desc,
        "PROJECT_PATH": project_path or "(no project open)",
        "PROJECT_NAME": awareness.get("PROJECT_NAME", ""),
        "TEAM_ROSTER": awareness.get("TEAM_ROSTER", ""),
        "CURRENT_STATE": awareness.get("CURRENT_STATE", ""),
        "PROJECT_MEMORY": awareness.get("PROJECT_MEMORY", ""),
        "CURRENT_TASK": awareness.get("CURRENT_TASK", ""),
        "WORKFLOW_STATUS": awareness.get("WORKFLOW_STATUS", ""),
        "REVIEW_MODE": review_mode,
        "TOOL_LIST": tool_list_str,
    }
```

This is a 1-line addition. Do NOT change anything else in the file.

---

## Edit 2 — `prompts/system/project-awareness.md`: add Current Task section

The template currently ends with:

```markdown
## Current State
{{CURRENT_STATE}}

{{PROJECT_MEMORY}}
```

Add a new "Current Task" section BEFORE the "## Project Memory" section (NOT before "## Current State"). The `{{CURRENT_TASK}}` variable is a TRUSTED directive and should be visually distinct from `{{PROJECT_MEMORY}}` (which is untrusted data inside the fence).

**The "## Project Memory" header is a SECTION HEADER in the template** — look carefully. The template's structure is:
- `## Project Memory` (header) followed by the "You can read and write to `.crabcakes/context.md`..." text
- ...later...
- `## Current State` then `{{CURRENT_STATE}}` then `{{PROJECT_MEMORY}}`

The `{{PROJECT_MEMORY}}` at the END of the file is where the untrusted-data-fenced block gets injected. We want the `## Current Task` section to appear BETWEEN `{{CURRENT_STATE}}` and `{{PROJECT_MEMORY}}` — so the trusted directive appears after the current state but before the untrusted memory block.

Replace the last 3 lines:

```markdown
## Current State
{{CURRENT_STATE}}

{{PROJECT_MEMORY}}
```

with:

```markdown
## Current State
{{CURRENT_STATE}}

## Current Task
{{CURRENT_TASK}}

## Project Memory
{{PROJECT_MEMORY}}
```

This places the trusted `{{CURRENT_TASK}}` directive in its own labeled section, outside the untrusted fence that wraps `{{PROJECT_MEMORY}}`.

---

## Verification (paste full output)

1. `grep -n "CURRENT_TASK" utils/prompt_loader.py` — expect 1 match in the variables dict
2. `grep -n "CURRENT_TASK\|Current Task" prompts/system/project-awareness.md` — expect 2 matches (the `## Current Task` header and the `{{CURRENT_TASK}}` variable)
3. `python3 -c "import ast; ast.parse(open('utils/prompt_loader.py').read()); print('parses OK')"`
4. **Functional test:** verify the variable is filled when composing a system prompt. Paste output of:
```bash
python3 -c "
import tempfile
from utils.project_awareness import init_project_config, save_project_context, build_awareness_dict
from utils.prompt_loader import compose_system_prompt, load_prompt_template

# Verify the template loads and has the new section
tpl = load_prompt_template('project-awareness')
assert '{{CURRENT_TASK}}' in tpl, 'template missing CURRENT_TASK'
assert '## Current Task' in tpl, 'template missing Current Task section'
print('template OK — has Current Task section')

# Verify compose_system_prompt fills the variable
with tempfile.TemporaryDirectory() as d:
    init_project_config(d, 'testproj')
    save_project_context(d, '## 2026-07-20 — Phase 2 in progress\n')
    awareness = build_awareness_dict(d)
    prompt = compose_system_prompt(agent_name='Coder', agent_role='coder', project_path=d, project_awareness=awareness)
    assert '2026-07-20 — Phase 2 in progress' in prompt, f'CURRENT_TASK not filled in composed prompt'
    print('compose OK — CURRENT_TASK filled:', repr(awareness['CURRENT_TASK']))
"
```

5. `python3 -m pytest tests/test_prompt_loader.py -q 2>&1 | tail -5` — all existing tests pass (if file exists; if not, skip)
6. `python3 -m pytest tests/test_project_awareness.py -q 2>&1 | tail -5` — all 37 tests still pass (no regression)

---

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] Edit 1: Added CURRENT_TASK to variables dict in prompt_loader.py — evidence: <grep>
- [x/not done] Edit 2: Added Current Task section to project-awareness.md template — evidence: <grep>
- [x/not done] Functional test: CURRENT_TASK filled in composed prompt — evidence: <output>
- [x/not done] No regression in test_project_awareness.py — evidence: <pytest tail>
```
