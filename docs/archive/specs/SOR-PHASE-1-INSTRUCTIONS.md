# Phase 1 of 8 — Supervisor prompt, YAML, and Auxilium auto-add flag

**Master spec:** `docs/specs/SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS.md` (§2.1, §2.2, §2.3, §2.10).

**Goal:** Add the Supervisor built-in agent definition + role prompt, flip Auxilium's project auto-add flag to false, and add tests. This is the foundation phase — nothing depends on code yet; it is pure config + prompt content + tests.

## Rules
- Use the `prompts/steelFramedCodeWriter.md` prompt. Read every file you touch BEFORE editing.
- Verify every claim with evidence (paste command output). No fabricated content.
- Anchor edits to identifiers, not line numbers.
- Run the exact test commands and paste full output.

## Edit 1 — Create `prompts/system/supervisor.md` (NEW)

Write the Supervisor role prompt. Requirements (master spec §2.2):
- Define Supervisor as **(1)** the project onboarding agent that conducts the interview and completes only setup during onboarding; **(2)** the implementation orchestrator that plans/delegates work to Coder and Debugger after onboarding; **(3)** an agent that follows the project manifest, workflow, team roster, and project rules; **(4)** an agent that does NOT claim onboarding completion until manifest/context/team/workflow updates are complete.
- Do **NOT** duplicate the full interview template — `project-onboarding.md` is appended separately by the loader when the project is un-onboarded. Keep this prompt focused on role identity and responsibilities, ~40–80 lines.
- Reference the `{{AGENT_NAME}}` template variable so the loader can fill the name (see `prompts/system/coder.md` and `prompts/system/debugger.md` for the established style — read both first).
- Mention that it plans/delegates using `/ask`, `/delegate`, `/tell`, and the implementation loop; that it reads `.crabcakes/` artifacts; and that it must update manifest/team/context/workflow before marking onboarding done.

## Edit 2 — Create `prompts/default_agents/supervisor.yaml` (NEW)

Valid built-in definition per master spec §2.1. Exact required fields:
- `name: Supervisor`
- `emoji:` an orchestration-appropriate emoji (e.g. `🎩` or `📋` — pick one)
- `role: supervisor`
- `prompts: [system/supervisor.md]`
- `tools:` must be a subset of the verified tool list: `read_file, write_file, edit_file, exec_command, list_files, search_files` (the 6 required) PLUS optionally `file_search` (justified: spec discovery needs it). Do **NOT** include `web_search`/`web_fetch` unless you justify each — Supervisor is an orchestrator, not a researcher. Default: include the 6 required + `file_search`.
- `llm_name: local-kb`
- `fallback_provider: openrouter`
- `auto_open: false`
- `auto_add_to_projects: false`  ← **CRITICAL — Supervisor is manually added by the user. Setting true would break the creation-bubble instruction.**
- `self_improvement:` block appropriate for a write-capable orchestrator (bug_journal true, project_rules true, enforcement true — it has write tools; structured_feedback false; dream_consolidation false). Mirror the structure in `prompts/default_agents/coder.yaml`.

Read `prompts/default_agents/coder.yaml` and `prompts/default_agents/auxilium.yaml` FIRST and match their structure/comment style.

## Edit 3 — Edit `prompts/default_agents/auxilium.yaml`

Change ONLY the line `auto_add_to_projects: true` → `auto_add_to_projects: false`. Preserve `auto_open: true` and all other settings (tools, prompts, llm_name, etc.) unchanged.

## Edit 4 — Add tests to `tests/test_special_agents.py`

Read the existing `tests/test_special_agents.py` FIRST to understand fixtures and the registry-loading pattern. Add a test class `TestSupervisorDef` (or extend an existing class — match the file's style) covering:

1. **Supervisor loads** — after `reload_registry()`, `get_special_agent("special:supervisor")` returns a non-None `SpecialAgentDef` with `role == "supervisor"`, `display_name == "Supervisor"`, and `auto_add_to_projects is False`.
2. **Supervisor can_write derived from tools** — `can_write` is True iff `write_file`/`edit_file` in tools (verify the derived value matches the tool list in supervisor.yaml).
3. **Supervisor prompt exists** — `prompts/system/supervisor.md` is a readable, non-empty file (guard against the loader silently skipping it).
4. **Auxilium auto_add_to_projects is False** — `get_special_agent("special:helper").auto_add_to_projects is False`.
5. **Auxilium auto_open still True** — `get_special_agent("special:helper").auto_open is True` (verify the flag flip didn't clobber auto_open).

Use the existing fixtures/patterns in the file. The registry may already be loaded from a prior test in the same session — call `reload_registry()` at the top of your test (it's a public function in `agent.special_agents`).

## Verification (run and paste output)

```bash
# YAML parses and all tools are registered
python3 -c "
import sys; sys.path.insert(0,'.')
from agent.special_agents import reload_registry, get_special_agent
reload_registry()
sup = get_special_agent('special:supervisor')
print('supervisor:', sup)
aux = get_special_agent('special:helper')
print('auxilium auto_add_to_projects:', aux.auto_add_to_projects)
print('auxilium auto_open:', aux.auto_open)
from agent.tools import get_all_tools
known = {t.name for t in get_all_tools()}
print('all supervisor tools known:', set(sup.tools) <= known)
"

# Prompt file exists
test -f prompts/system/supervisor.md && echo "supervisor.md EXISTS" || echo "MISSING"

# Flag flip confirmed
grep -n "auto_add_to_projects" prompts/default_agents/auxilium.yaml

# Tests
XDG_CONFIG_HOME=/tmp/cctest_home/.config python3 -m pytest tests/test_special_agents.py -q 2>&1 | tail -20
```

## COMPLETENESS checklist (mandatory — fill in with evidence)

```
COMPLETENESS:
- [ ] Edit 1: prompts/system/supervisor.md created — evidence: line count + first/last line
- [ ] Edit 2: prompts/default_agents/supervisor.yaml created — evidence: grep auto_add_to_projects output
- [ ] Edit 3: auxilium.yaml auto_add_to_projects flipped to false — evidence: grep output showing the single changed line
- [ ] Edit 4: test_special_agents.py tests added — evidence: pytest -q output (note the 2 pre-existing Debugger failures are ENVIRONMENTAL — they must still be present, not newly fixed)
- [ ] Verification commands pasted in full
- [ ] Any related issue found, not silently fixed (report here)
```

Report files changed with line numbers, full test output, and any issues.
