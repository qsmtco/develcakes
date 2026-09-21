# Phase 3 of 8 — Prompt-loader supervisor gate + context role fallback

**Master spec:** `docs/specs/SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS.md` §2.5 + §2.10.
**Closes:** Debugger's Phase-1 BUG #1 (supervisor prompt never wired into `compose_system_prompt`).

**Goal:** (1) gate the onboarding template on `agent_role == "supervisor"` instead of `"coder"`; (2) add an explicit `elif agent_role == "supervisor":` branch that loads `supervisor.md`; (3) add Supervisor to `agent/context.py`'s fallback role derivation as defense-in-depth.

## Rules
- Use the `prompts/steelFramedCodeWriter.md` prompt. Read `utils/prompt_loader.py` and `agent/context.py` in FULL before editing.
- Anchor edits to identifiers, not line numbers.
- Verify every claim with evidence (paste command output).

## Edit 1 — `utils/prompt_loader.py`: onboarding gate + supervisor branch

In `compose_system_prompt()`, the current onboarding gate (search for `is_project_onboarded`) is:

```python
if agent_role == "coder" and not is_project_onboarded(project_path):
    onboarding = load_prompt_template("project-onboarding")
    if onboarding:
        parts.append(onboarding)
```

**Change it to gate on `"supervisor"`** per master spec §2.5:

```python
if agent_role == "supervisor" and not is_project_onboarded(project_path):
    onboarding = load_prompt_template("project-onboarding")
    if onboarding:
        parts.append(onboarding)
```

Do **not** call `get_project_onboarding_agents()` for this gate (spec §2.5 explicit) — it's a pure role check. Preserve the existing non-fatal `try/except` that wraps the onboarding block (the import of `is_project_onboarded` is inside the `try`).

The docstring at the top of `compose_system_prompt` (which lists the selection logic, e.g. "4. If agent_role == 'coder': coder.md") must be updated to reflect (a) the supervisor onboarding gate and (b) the new supervisor branch. Read the docstring and update the relevant lines so it stays accurate.

Then, in the agent-specific templates section (currently `if agent_role == "coder":` ... `elif agent_role == "debugger":` ... `elif agent_role == "helper":`), **add a supervisor branch**:

```python
elif agent_role == "supervisor":
    st = load_prompt_template("supervisor")
    if st:
        parts.append(st)
```

Match the style of the existing coder/debugger/helper branches exactly (variable name, structure).

## Edit 2 — `agent/context.py`: Supervisor role fallback

In `build_system_prompt()`, the current fallback derivation (search for `agent_role or (`) is:

```python
agent_role=agent_role or (
    "coder" if "coder" in agent_name.lower() else
    "debugger" if "debugger" in agent_name.lower() else ""
),
```

**Add a Supervisor fallback** per master spec §2.5 (defense-in-depth — verify the conversation-construction path passes an explicit role; the fallback is still required):

```python
agent_role=agent_role or (
    "coder" if "coder" in agent_name.lower() else
    "debugger" if "debugger" in agent_name.lower() else
    "supervisor" if "supervisor" in agent_name.lower() else ""
),
```

## Edit 3 — Tests in `tests/test_prompt_loader.py`

Read `tests/test_prompt_loader.py` FIRST to understand fixtures and the existing onboarding-gate tests. Add tests covering (master spec §2.10):

1. **Supervisor + un-onboarded project gets BOTH prompts** — `compose_system_prompt(agent_role="supervisor", project_path=<temp un-onboarded project>, ...)` contains supervisor role content (e.g. a phrase from `prompts/system/supervisor.md`) AND onboarding content (a phrase from `prompts/system/project-onboarding.md`). Use a real temp project dir with `.crabcakes/project.md` containing only comment-only skeleton (so `is_project_onboarded` returns False). You can build the temp project with `utils.project_awareness.init_project_config(path, name)` — match how existing onboarding tests in the file set up state.
2. **Supervisor + onboarded project gets supervisor prompt but NOT onboarding** — same call but with an onboarded project (write real content into `.crabcakes/project.md`); assert supervisor content present AND onboarding content absent.
3. **Coder does NOT get onboarding template anymore** — `compose_system_prompt(agent_role="coder", project_path=<un-onboarded temp project>, ...)` must NOT contain project-onboarding content. This is the regression guard for the gate change (old behavior was `agent_role == "coder"`).
4. **Non-supervisor role does NOT get supervisor prompt** — e.g. `agent_role="coder"` must not contain supervisor role content; `agent_role="debugger"` likewise; a gateway/empty role likewise.
5. **Onboarding-check failure is non-fatal** — if the project-state check import/raises, prompt composition still succeeds without the onboarding template. (Existing pattern — verify or add a test where `project_path` points to a path that triggers the except branch.)

Match the file's existing fixture style. Each test must use isolated temp dirs (no `~/.config` pollution). Note the autouse-fixture lesson from Phase 2 — if your tests touch agent_defs/project_awareness, redirect config dirs to temp.

## Edit 4 — Tests in `tests/test_agent_context.py` (create if absent) OR existing context test file

Check whether `tests/test_agent_context.py` exists. If it does, add to it; if not, grep for the existing context-role test location and add there. Add ONE test: `build_system_prompt(agent_name="Supervisor", ...)` with no explicit `agent_role` resolves the role to `"supervisor"` (assert the composed prompt contains supervisor role content). If `build_system_prompt` is hard to unit-test because of file-context side effects, you may instead unit-test the fallback expression by calling `build_system_prompt(agent_name="Supervisor", project_path=<temp>, tools=[], agent_role="")` and asserting the output contains a supervisor phrase.

## Verification (run and paste output)

```bash
# Gate is now supervisor, not coder
grep -n 'agent_role == "supervisor"' utils/prompt_loader.py   # must show the onboarding gate
grep -n 'agent_role == "coder" and not is_project_onboarded' utils/prompt_loader.py  # MUST return 0 matches
grep -n 'load_prompt_template("supervisor")' utils/prompt_loader.py  # must show the new branch

# Fallback recognizes supervisor
grep -n '"supervisor" in agent_name.lower()' agent/context.py  # must show the new branch

# Functional proof
python3 -c "
import sys; sys.path.insert(0,'.')
from utils.prompt_loader import compose_system_prompt
# Use a temp un-onboarded project
import tempfile, os
from utils.project_awareness import init_project_config
p = tempfile.mkdtemp()
init_project_config(p, 'TestProj')
out = compose_system_prompt(agent_role='supervisor', project_path=p, agent_name='Supervisor')
print('supervisor role prompt present:', 'Plan then delegate' in out or 'orchestrator' in out.lower())
print('onboarding template present:', 'onboarding' in out.lower() or 'interview' in out.lower())
out_coder = compose_system_prompt(agent_role='coder', project_path=p, agent_name='Coder')
print('coder onboarding (must be False):', 'onboarding' in out_coder.lower() and 'interview' in out_coder.lower())
"

# Tests
XDG_CONFIG_HOME=/tmp/cctest_home/.config python3 -m pytest tests/test_prompt_loader.py -q 2>&1 | tail -10
XDG_CONFIG_HOME=/tmp/cctest_home/.config python3 -m pytest tests/test_agent_context.py -q 2>&1 | tail -10 || echo "(context test file may be named differently — report what you find)"
```

## COMPLETENESS (mandatory)

```
COMPLETENESS:
- [ ] Edit 1: prompt_loader onboarding gate supervisor + supervisor branch + docstring — evidence: grep outputs
- [ ] Edit 2: context.py supervisor fallback — evidence: grep output
- [ ] Edit 3: test_prompt_loader.py tests (5) — evidence: pytest output
- [ ] Edit 4: context fallback test — evidence: pytest output or note on file location
- [ ] Functional proof pasted
- [ ] Any related issue found, not silently fixed (report here)
```
