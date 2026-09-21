# PHASE 1 of 1 — Tier 1.3: Agent-Role Gating for Project Onboarding

**Master spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-AGENT-ROLE-GATING-FIX.md` — read this in full before doing anything.
**Prompt template:** `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md` — use its Discovery/Design/Tests/Risks/Files structure for your report.
**Word marker:** "please write" (required in your acknowledgment)

This is **one phase, one focused change**. One line in one production file. One new test.

---

## What to change

### File 1: `utils/prompt_loader.py` — 1 line changed

At **line 184** in `compose_system_prompt()`, change the onboarding gate.

**Before (line 184):**
```python
            if not is_project_onboarded(project_path):
```

**After:**
```python
            if agent_role == "coder" and not is_project_onboarded(project_path):
```

That's the entire production change. The `if project_path:` outer guard at line 181 stays. The `try/except` wrapping at line 182-189 stays. The template loading at line 185 stays. Only the inner condition at line 184 changes.

### File 2: `tests/test_prompt_loader.py` — 1 new test added

Add this test to the existing `TestComposeSystemPrompt` class. The natural insertion point is **after `test_default_template_loaded` (line ~58) and before `test_coder_template_included` (line ~70)**, so role-gating tests stay grouped together. Read the file first to confirm the exact insertion point.

```python
    def test_onboarding_only_loaded_for_coder(self, tmp_path):
        """Onboarding template loads for coder agents only, not for gateway/debugger.

        Per BUG_REPORT-identity-override.md Bug #2: the onboarding interview
        should not be injected into every agent's prompt in a fresh project.
        """
        # Helper: does this prompt contain the onboarding template's content?
        def has_onboarding(prompt: str) -> bool:
            return "Onboarding" in prompt or "onboard" in prompt.lower()

        # Coder in an unonboarded project → onboarding IS loaded
        coder_prompt = compose_system_prompt(
            agent_name="Coder", agent_role="coder", project_path=str(tmp_path),
        )
        assert has_onboarding(coder_prompt), (
            "Coder should get onboarding template in unonboarded project"
        )

        # Debugger in same project → onboarding is NOT loaded
        debugger_prompt = compose_system_prompt(
            agent_name="Debugger", agent_role="debugger", project_path=str(tmp_path),
        )
        assert not has_onboarding(debugger_prompt), (
            "Debugger should NOT get onboarding template (Bug #2 fix)"
        )

        # Gateway (empty agent_role) in same project → onboarding is NOT loaded
        gateway_prompt = compose_system_prompt(
            agent_name="Gateway", agent_role="", project_path=str(tmp_path),
        )
        assert not has_onboarding(gateway_prompt), (
            "Gateway agent should NOT get onboarding template (Bug #2 fix)"
        )
```

**Test design notes:**
- A fresh `tmp_path` has no `.crabcakes/` directory, so `is_project_onboarded()` returns `False`. No mocking needed.
- The check for "Onboarding" or "onboard" in the prompt catches the template's distinctive content. The onboarding template's title is "Project Onboarding" — that's a stable marker.
- If the onboarding template text ever changes and breaks this test, that's a signal the test is too coupled to template internals — but for now, the marker is stable.

---

## What NOT to do (out of scope — flag in report if you see them, do NOT fix)

- Do NOT modify the onboarding template content (`prompts/system/project-onboarding.md`)
- Do NOT change `is_project_onboarded()` detection logic in `utils/project_awareness.py`
- Do NOT add a more sophisticated agent-role allowlist (e.g. `["coder", "debugger"]`)
- Do NOT change other template gating (default, project-awareness, collab, crabcakes-context, crabcakes-commands, code-review, coder, debugger all stay as-is)
- Do NOT add a regression test for the BUG_REPORT-identity-override attack vector — separate Tier 2/3 item
- Do NOT change the function signature of `compose_system_prompt` — `agent_role` is already a parameter
- Do NOT silently fix related issues — flag them in your report under "Related issues found, not fixed"

---

## Verification (run yourself and paste the actual output)

Run these commands and paste the **full output** (not a summary):

1. `cd /path/to/projects/crabcakes && python3 -c "from utils.prompt_loader import compose_system_prompt; print('import OK')"` — confirms the file imports cleanly
2. `cd /path/to/projects/crabcakes && pytest tests/test_prompt_loader.py::TestComposeSystemPrompt::test_onboarding_only_loaded_for_coder -v` — runs the new test
3. `cd /path/to/projects/crabcakes && pytest tests/test_prompt_loader.py -v` — confirms all prompt_loader tests pass
4. `cd /path/to/projects/crabcakes && pytest tests/test_context.py -v` — confirms context tests (which call compose_system_prompt transitively) pass
5. `cd /path/to/projects/crabcakes && grep -n "agent_role == .coder. and not is_project_onboarded" utils/prompt_loader.py` — confirms the gating is in place
6. `cd /path/to/projects/crabcakes && wc -l utils/prompt_loader.py tests/test_prompt_loader.py` — show the line counts

---

## What to report back (required format)

### 1. Diff per file

Run `cd /path/to/projects/crabcakes && git diff utils/prompt_loader.py tests/test_prompt_loader.py` and show the actual diff. The diff for `utils/prompt_loader.py` should be exactly 1 line. The diff for `tests/test_prompt_loader.py` should add the new test (one hunk with the function definition + preceding blank line).

### 2. Test outputs

Paste the full output of the 6 verification commands above. No summaries.

### 3. COMPLETENESS checklist

```
COMPLETENESS:
- [x| ] Edit 1: Changed line 184 of utils/prompt_loader.py to add `agent_role == "coder" and` gate — evidence: <file:line>
- [x| ] Edit 2: Added `test_onboarding_only_loaded_for_coder` to TestComposeSystemPrompt in tests/test_prompt_loader.py — evidence: <file:line>
- [x| ] Verification: New test passes — evidence: <paste test output>
- [x| ] Verification: All prompt_loader tests pass — evidence: <paste test output>
- [x| ] Verification: All context tests pass — evidence: <paste test output>
- [x| ] Verification: Files import cleanly — evidence: <paste import output>
- [x| ] Verification: Gating confirmed by grep — evidence: <paste grep output>
```

### 4. Related issues found, not fixed

List any other inconsistencies you noticed in the surrounding code (e.g., other ungated template loads, dead `crabcakes-commands` template reference, missing test coverage for the `try/except` around `is_project_onboarded`) but did NOT fix.

### 5. Independent check

Trace the path you verified to confirm the fix works:
1. Coder agent in fresh project → `agent_role="coder"` → gate matches → onboarding template loaded
2. Debugger agent in fresh project → `agent_role="debugger"` → gate does NOT match → onboarding template NOT loaded
3. Gateway agent in fresh project → `agent_role=""` → gate does NOT match → onboarding template NOT loaded

---

## Reference files

- Master spec: `/path/to/projects/crabcakes/docs/specs/SPEC-AGENT-ROLE-GATING-FIX.md`
- Proposal: `/path/to/projects/crabcakes/docs/proposals/PROPOSAL-project-onboarding.md`
- Bug report: `/path/to/projects/crabcakes/docs/bugs/BUG_REPORT-identity-override.md` (Bug #2, lines 47-49)
- Existing test class to extend: `/path/to/projects/crabcakes/tests/test_prompt_loader.py` (`TestComposeSystemPrompt` class)
- Existing call sites: `/path/to/projects/crabcakes/agent/context.py:418-421`, `/path/to/projects/crabcakes/utils/prompt_loader.py:117-200`
- Prompt template: `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md`

---

## Acknowledgment

Begin your response with "please write" (the word marker) to confirm you received this delegation.
