PHASE 2 of 6 — Handler template + agent_defs simplification

Files to change:
1. `/path/to/projects/crabcakes/ui/handlers/agent_builder_handler.py` — remove `"fallback_model": None,` from `create_new()` template
2. `/path/to/projects/crabcakes/utils/agent_defs.py` — simplify `_normalize_fallback_fields()` to only check `fallback_provider`

Spec reference:
- Read the master spec at `/path/to/projects/crabcakes/docs/specs/SPEC-AGENT-FALLBACK-MODEL-DROPDOWN-REMOVAL.md` §2.2 (handler) and §2.3 (utils).
- The spec is identifier-anchored — do not rely on line numbers.

Changes in `ui/handlers/agent_builder_handler.py`:
- In `create_new()` (the method that returns a fresh agent template dict), remove the line `"fallback_model": None,`. The dict should retain `"fallback_provider": None,` and all other fields.

Changes in `utils/agent_defs.py`:
- In `_normalize_fallback_fields(data: dict)`, remove the block that checks for `"fallback_model"`. The function should only add `fallback_provider` if missing. Add a comment in the docstring explaining that `fallback_model` was removed on 2026-06-15 and old YAMLs retain the key in the loaded dict but it is ignored by the runtime.

What NOT to change:
- `validate_agent_def` — it does not validate `fallback_model` (verified — search confirms no `fallback_model` reference in the function). Leave it alone.
- `save_agent_def` — it writes whatever fields are in the dict. Don't add any filtering.
- `load_agent_def` / `load_agent_defs` — they call `_normalize_fallback_fields`. The behavior of these functions changes only because `_normalize_fallback_fields` no longer adds `fallback_model`. Don't touch the loaders.

Rules:
- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- Read both files in full before editing.
- Hard-part-first: do `agent_builder_handler.py` first (1-line change), then `utils/agent_defs.py` (small function simplification).
- Verify with grep that no `fallback_model` references remain in either file.

Verification commands (run all and paste output):
1. `grep -n "fallback_model" /path/to/projects/crabcakes/ui/handlers/agent_builder_handler.py /path/to/projects/crabcakes/utils/agent_defs.py` — expect zero matches
2. `cd /path/to/projects/crabcakes && python3 -c "import ast; ast.parse(open('ui/handlers/agent_builder_handler.py').read()); ast.parse(open('utils/agent_defs.py').read())"` — expect no SyntaxError
3. `cd /path/to/projects/crabcakes && python3 -c "from ui.handlers.agent_builder_handler import AgentBuilderHandler; from utils.agent_defs import _normalize_fallback_fields; t = AgentBuilderHandler().create_new(); assert 'fallback_model' not in t, t; assert 'fallback_provider' in t, t; print('create_new OK, fallback_model absent, fallback_provider present')"`
4. `cd /path/to/projects/crabcakes && python3 -c "from utils.agent_defs import _normalize_fallback_fields; d = {'name': 'X'}; _normalize_fallback_fields(d); assert d.get('fallback_provider') is None; assert 'fallback_model' not in d; print('normalize OK')"`
5. `cd /path/to/projects/crabcakes && timeout 60 xvfb-run -a python3 -m pytest tests/test_agent_builder_fallback.py tests/test_agent_builder_handler.py -v 2>&1 | tail -30` — expect passing (the fallback test still tests `fallback_provider` round-trip)

Report back with:
- Files changed (with `wc -l` output before and after)
- All five verification command outputs
- COMPLETENESS checklist:
  COMPLETENESS:
  - [done/not done] Removed `"fallback_model": None,` from `create_new()` — evidence: grep
  - [done/not done] Removed `fallback_model` check from `_normalize_fallback_fields()` — evidence: function read
  - [done/not done] Added docstring note in `_normalize_fallback_fields()` about the 2026-06-15 removal — evidence: docstring read
  - [done/not done] `create_new()` returns dict without `fallback_model` — evidence: python -c output
  - [done/not done] `_normalize_fallback_fields()` does not add `fallback_model` — evidence: python -c output
  - [done/not done] All existing tests still pass (test_agent_builder_fallback.py, test_agent_builder_handler.py) — evidence: pytest output
  - [done/not done] No regressions in unrelated tests — evidence: full pytest output
- Any related issues found during the scan (read 3+ lines of context before flagging duplicates) — flag only, do not fix in this phase.

please write
