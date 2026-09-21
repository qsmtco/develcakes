# Phase 3 Follow-up — 3 doc/test fixes from Debugger's audit

All in `utils/prompt_loader.py`, `agent/context.py`, and `tests/test_prompt_loader.py`. Production behavior (the gate change, supervisor branch, context fallback) is signed off — these are docstring and test-strength fixes only.

## BUG #1 (LOW) — `compose_system_prompt` docstring numbering contradicts code

The docstring selection-logic list (near the top of `compose_system_prompt` in `utils/prompt_loader.py`) currently numbers items 1-9 but (a) omits collab / crabcakes-context / crabcakes-commands templates that actually load, (b) lists supervisor.md (step 7) before project-onboarding (step 8) when the code actually appends project-onboarding BEFORE supervisor.md, and (c) lists code-review as step 3 when it runs after the onboarding template.

Rewrite the list as grouped bullets (not a fragile numbered list). Use this shape:

```
    Selection logic (grouped; within a group order matters):
    - Identity (always): default.md, collab.md, crabcakes-context.md
    - Project (when active): project-awareness.md, crabcakes-commands.md,
      project-onboarding.md (only when agent_role == "supervisor" and
      project not yet onboarded)
    - Review (when review_mode != "off"): code-review.md
    - Role (exactly one): coder.md / debugger.md / auxilium.md / supervisor.md
    - Self-improvement (project active + role): {role}-bugs.md, {role}-rules.md
```

Read the actual code order first and make sure the grouping accurately reflects what the code does.

## BUG #2 (LOW) — `build_system_prompt` docstring example

In `agent/context.py`, the `build_system_prompt` docstring's `agent_role:` arg description (search for `Explicit role override`) says `(e.g. "coder")`. Update to list all valid options:

```
        agent_role: Explicit role override (one of "coder", "debugger",
            "helper", "supervisor"; or "" for gateway agents). When empty,
            derives from agent_name as a fallback.
```

## BUG #5 (LOW) — strengthen `test_onboarding_check_failure_is_non_fatal`

In `tests/test_prompt_loader.py`, the test `test_onboarding_check_failure_is_non_fatal` currently only asserts negatives (no "ONBOARDING phase", prompt is non-empty string). Add a **positive assertion** that the supervisor role template still loaded despite the onboarding-check failure. Append after the existing assertions:

```python
    # Positive guard: supervisor role template must still load even when
    # the onboarding check raised (the onboarding except branch must not
    # swallow the rest of compose_system_prompt).
    assert "orchestrator" in prompt.lower() or "Plan then delegate" in prompt, \
        "supervisor.md should still load when the onboarding check fails"
```

(Confirm the phrase matches supervisor.md content — read prompts/system/supervisor.md; "orchestrator" or "Plan then delegate" both appear there.)

## Verification

```bash
# Docstring updated
grep -n "Identity (always)\|grouped" utils/prompt_loader.py   # must show the new grouping
grep -n '"supervisor"' agent/context.py | head -3             # must show the docstring update

# Test strengthened
grep -n "orchestrator" tests/test_prompt_loader.py            # must show the positive assertion

# All tests still pass
XDG_CONFIG_HOME=/tmp/cctest_home/.config python3 -m pytest tests/test_prompt_loader.py tests/test_context.py -q 2>&1 | tail -6
```

## COMPLETENESS (mandatory)

```
COMPLETENESS:
- [ ] BUG #1: compose_system_prompt docstring rewritten as grouped bullets — evidence: grep output
- [ ] BUG #2: build_system_prompt docstring lists all 4 roles + gateway — evidence: grep output
- [ ] BUG #5: test_onboarding_check_failure_is_non_fatal has positive assertion — evidence: grep + pytest pass
- [ ] All tests pass — evidence: pytest -q output
- [ ] Any related issue found, not silently fixed (report here)
```
