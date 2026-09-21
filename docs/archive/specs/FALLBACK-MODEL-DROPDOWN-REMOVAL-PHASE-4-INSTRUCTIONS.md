PHASE 4 of 6 — Tests: update existing tests for the new contract, add new derivation test and backward-compat test

Files to change:
1. `/path/to/projects/crabcakes/tests/test_agent_builder_fallback.py` — drop `fallback_model` assertions, add new "does not emit" and "old YAML loads" tests
2. `/path/to/projects/crabcakes/tests/test_runtime_fallback.py` — drop `fallback_model` parameter, add new derivation test
3. `/path/to/projects/crabcakes/tests/test_kb_integration.py` — drop `fallback_model` parameter from the integration test

Spec reference:
- Read the master spec at `/path/to/projects/crabcakes/docs/specs/SPEC-AGENT-FALLBACK-MODEL-DROPDOWN-REMOVAL.md` §2.8 (test_agent_builder_fallback), §2.9 (test_runtime_fallback), and the small note about test_kb_integration.
- The spec is identifier-anchored — do not rely on line numbers.

Changes in `tests/test_agent_builder_fallback.py`:

A. `TestHandlerCreateNew::test_create_new_includes_fallback_fields` (around line 38-46):
   - Rename to `test_create_new_includes_fallback_provider`
   - Update the body to assert only `fallback_provider` is in the template
   - Add an assertion: `assert "fallback_model" not in template`

B. `TestNormalizeFallbackFields` class (around line 49-62):
   - Drop `test_preserves_existing_keys` (it asserts `fallback_model` is preserved)
   - Keep `test_adds_missing_keys` BUT rename to `test_adds_missing_provider_key` and update assertions to only check `fallback_provider`
   - Add a new test `test_does_not_add_fallback_model` that asserts the normalize function does not add `fallback_model` to a dict that lacks it
   - Add a new test `test_preserves_existing_provider` that asserts existing `fallback_provider` values are preserved (but does not touch `fallback_model`)

C. `TestYamlRoundTrip` class (around line 65-118):
   - In `test_save_load_fallback_fields`: drop all `fallback_model` assertions. Keep `fallback_provider` round-trip.
   - `test_save_load_without_fallback_fields`: leave alone (it doesn't assert `fallback_model`).
   - `test_save_load_fallback_none`: drop `fallback_model` assertion. Keep `fallback_provider`.
   - Add a new test `test_save_does_not_emit_fallback_model` that:
     - Creates a dict with `fallback_provider` set but no `fallback_model`
     - Calls `save_agent_def(agent_def)`
     - Reads the raw YAML file and asserts `"fallback_model"` is not in the file content
   - Add a new test `test_old_yaml_with_fallback_model_loads` that:
     - Writes a legacy YAML directly to the agents dir with `fallback_model` set
     - Calls `load_agent_def("LegacyAgent")`
     - Asserts it returns a dict and the agent loads without error
     - Does NOT assert on the presence/absence of `fallback_model` in the loaded dict (tolerant read)

D. `TestHandlerSaveLoad::test_handler_save_load_round_trip` (around line 121-148):
   - Drop the `fallback_model` assertion at the end. Keep the `fallback_provider` assertion.

Changes in `tests/test_runtime_fallback.py`:

A. `_make_runtime()` helper (around line 66-83):
   - Drop the `fallback_model` parameter
   - Update the `AgentConfig(...)` construction to omit `fallback_model`
   - Update the docstring to note the parameter was removed

B. `_setup_conversation()` helper (around line 86-105):
   - Drop the `fallback_model=rt._config.fallback_model,` line
   - Update the docstring to note the parameter was removed

C. `TestFallbackOnOutOfScope::test_fallback_on_out_of_scope` (around line 110-133):
   - Change the `_make_runtime` call to pass only `fallback_provider` (no `fallback_model`)
   - The test's other assertions remain the same

D. `TestFallbackOneShot::test_fallback_one_shot` (around line 152-175):
   - Same change: pass only `fallback_provider` to `_make_runtime`

E. `TestFallbackResetOnNewMessage::test_fallback_reset_on_new_message` (around line 178-199):
   - Same change

F. Add a new class `TestFallbackModelDerivation` with one test:
   - `test_derives_from_provider_default_model`: when `fallback_provider="openrouter"` and the openrouter card has `default_model="openrouter/test-model"`, the runtime sets `conv.model = "openrouter/test-model"` for the fallback call
   - Use a similar mock pattern to the existing tests
   - Capture `conv.model` at the moment of each `_call_llm` invocation

Changes in `tests/test_kb_integration.py`:

A. `TestIntegrationRuntimeFallback::test_fallback_chain_end_to_end` (around line 130-180):
   - In the `AgentConfig` construction, drop `fallback_model="openrouter/owl-alpha"`
   - In the `Conversation` construction, drop `fallback_model="openrouter/owl-alpha"`
   - The test's assertions remain the same (the integration test verifies the chain fires; the new derivation is what produces the model string)

What NOT to change:
- Don't touch other tests in test_kb_integration.py
- Don't touch the `kb_lookup` tests
- Don't add new tests beyond what's specified (keep scope tight)

Rules:
- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- Read all three test files in full before editing.
- Hard-part-first: do test_agent_builder_fallback.py first (most changes), then test_runtime_fallback.py (mechanical), then test_kb_integration.py (1 method).
- Verify with grep that the only `fallback_model` references remaining in the tests are intentional (e.g., the "old YAML loads" test references the key in a comment or string).

Verification commands (run all and paste output):
1. `cd /path/to/projects/crabcakes && timeout 90 xvfb-run -a python3 -m pytest tests/test_agent_builder_fallback.py tests/test_runtime_fallback.py tests/test_kb_integration.py -v 2>&1 | tail -50` — expect all tests pass (old + new)
2. `grep -n "fallback_model" /path/to/projects/crabcakes/tests/test_agent_builder_fallback.py /path/to/projects/crabcakes/tests/test_runtime_fallback.py /path/to/projects/crabcakes/tests/test_kb_integration.py` — expect:
   - test_agent_builder_fallback.py: 0 matches (or only in the new "old YAML loads" test where the key is being tested for tolerance)
   - test_runtime_fallback.py: 0 matches (or only in the new "derives from provider default_model" test where the model string is the expected value)
   - test_kb_integration.py: 0 matches
3. `cd /path/to/projects/crabcakes && python3 -c "import ast; ast.parse(open('tests/test_agent_builder_fallback.py').read()); ast.parse(open('tests/test_runtime_fallback.py').read()); ast.parse(open('tests/test_kb_integration.py').read())"` — expect no SyntaxError
4. Confirm the new derivation test actually exercises the runtime derivation (not just the helper): the test should mock `_call_llm` and assert `conv.model == "openrouter/test-model"` (the provider card's default_model) at the moment of the fallback call, not `"openrouter/owl-alpha"` (which was the old hard-coded value).

Report back with:
- Files changed (with `wc -l` output before and after for each)
- All four verification command outputs
- COMPLETENESS checklist:
  COMPLETENESS:
  - [done/not done] `test_create_new_includes_fallback_provider` (renamed) — evidence: read
  - [done/not done] `test_does_not_add_fallback_model` (new) — evidence: read
  - [done/not done] `test_preserves_existing_provider` (new) — evidence: read
  - [done/not done] `test_save_does_not_emit_fallback_model` (new) — evidence: read
  - [done/not done] `test_old_yaml_with_fallback_model_loads` (new) — evidence: read
  - [done/not done] Dropped all `fallback_model` assertions from existing tests in test_agent_builder_fallback.py — evidence: grep
  - [done/not done] `_make_runtime` and `_setup_conversation` no longer take `fallback_model` — evidence: read
  - [done/not done] `TestFallbackModelDerivation::test_derives_from_provider_default_model` (new) — evidence: read
  - [done/not done] All existing `test_runtime_fallback.py` tests still pass (with the dropped parameter) — evidence: pytest
  - [done/not done] `test_kb_integration.py::test_fallback_chain_end_to_end` updated to drop `fallback_model` — evidence: read
  - [done/not done] All tests in the three test files pass — evidence: pytest output
  - [done/not done] The new derivation test actually exercises the runtime (asserts the model string at call time, not just sets a parameter) — evidence: read
- Any related issues found during the related-bug scan (read 3+ lines of context before flagging duplicates) — flag only, do not fix in this phase.

please write
