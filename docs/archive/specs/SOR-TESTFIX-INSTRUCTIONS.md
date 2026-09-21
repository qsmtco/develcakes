# SOR Test-Fix Instructions — local-kb Provider Validation

## Context

16 tests fail across `tests/test_special_agents.py` (7) and `tests/test_agent_defs.py` (9).
12 of these are **pre-existing** (fail on clean HEAD `3a081d4`); 4 are new from SOR work
(supervisor.yaml also uses `local-kb`).

**Single root cause:** `validate_agent_def()` in `utils/agent_defs.py` (around line 437)
rejects `llm_name: "local-kb"` because the test environment's `providers.yaml` only has
`openrouter`. But `local-kb` is a **built-in provider** seeded by
`ensure_kb_provider()` in `utils/providers_store.py` — it should always be valid
regardless of what's in `providers.yaml`.

## Files to Change

### 1. `utils/agent_defs.py` — Whitelist `local-kb` in validation

**Function:** `validate_agent_def` (starts ~line 375)

**Current code** (~line 437):
```python
        providers = get_available_providers()
        valid_ids = set()
        display_names = set()
        for p in providers:
            display_names.add(p["name"])
            valid_ids.add(p["name"])
            if p.get("default_model") and "/" in p["default_model"]:
                valid_ids.add(p["default_model"].split("/")[0])
        if display_names and llm_name not in valid_ids:
            errors.append(
                f"Unknown provider: {llm_name}. Available: {', '.join(sorted(display_names))}"
            )
```

**Required change:** Add `"local-kb"` to `valid_ids` before the membership check.
`local-kb` is the built-in knowledge-base provider seeded by
`ensure_kb_provider()` — it must always pass validation even when not
explicitly listed in `providers.yaml`.

```python
        # local-kb is a built-in provider seeded by ensure_kb_provider();
        # always valid regardless of providers.yaml contents.
        valid_ids.add("local-kb")
```

Insert this line AFTER the `for` loop builds `valid_ids` from providers and
BEFORE the `if display_names and llm_name not in valid_ids:` check.

**That's it. One file, one 2-line addition.**

## Verification

After the fix, run:
```bash
XDG_CONFIG_HOME=/tmp/cctest_home/.config python3 -m pytest \
    tests/test_special_agents.py tests/test_agent_defs.py -v
```

**Expected:** 0 failures (all 46 tests pass). The test `test_unknown_provider`
(which intentionally tests that a bogus provider name IS rejected) must still
pass — `local-kb` should be whitelisted, but genuinely unknown providers
must still be rejected.

Also run the full SOR-related suite to confirm no regressions:
```bash
XDG_CONFIG_HOME=/tmp/cctest_home/.config python3 -m pytest \
    tests/test_special_agents.py tests/test_agent_defs.py \
    tests/test_prompt_loader.py tests/test_context.py \
    tests/test_project_awareness.py tests/test_project_handler.py -v
```

## COMPLETENESS Checklist

The builder MUST include this in the response:

```
COMPLETENESS:
- [x/not done] Edit 1: Added "local-kb" to valid_ids whitelist in validate_agent_def — evidence: [paste the diff hunk]
- [x/not done] All 16 previously-failing tests now pass — evidence: [paste pytest summary line]
- [x/not done] test_unknown_provider still passes (genuine unknowns still rejected) — evidence: [paste that test's output]
- [x/not done] No regressions in full SOR suite — evidence: [paste full suite summary]
- [x/not done] Related issues flagged: [note any other bugs found in the same function]
```
