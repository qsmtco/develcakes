PHASE 1 of 6 — UI removal in `ui/views/agent_builder.py`

Files to change:
1. `/path/to/projects/crabcakes/ui/views/agent_builder.py` — remove the Fallback Model dropdown widget and all its plumbing

Spec reference:
- Read the master spec at `/path/to/projects/crabcakes/docs/specs/SPEC-AGENT-FALLBACK-MODEL-DROPDOWN-REMOVAL.md` §2.1 for the exact code changes.
- The spec is identifier-anchored — do not rely on line numbers from the spec; they may have drifted. Use the symbol/attribute names listed below as anchors.

Symbols to REMOVE (must result in zero grep matches after the phase):
- `self._fallback_model_dropdown` (attribute initialization)
- `self._fallback_model_labeled` (attribute initialization)
- `self._get_selected_fallback_model` (method definition)
- `"fallback_model": self._get_selected_fallback_model() or None,` (in `get_values()`)
- The block that creates `_fallback_model_dropdown` inside `_build_fallback_provider_row` (the 4-line block beginning with `# Fallback model dropdown`)
- The model-population branch inside `_on_fallback_provider_changed` (the block that sets `_fallback_model_dropdown.set_model(...)` and `set_sensitive(...)`)
- The model-restoration loop inside `_fill_form` (the loop that iterates `models` to call `_fallback_model_dropdown.set_selected(...)`)
- `self._provider_models: dict[str, list[tuple[str, str]]] = {}` (in `__init__`)
- The `self._provider_models = {...}` assignment block inside `set_provider_options`

Symbols to KEEP (do not touch):
- `_fallback_dropdown` (the provider dropdown)
- `_fallback_labeled` (its label)
- `_fallback_providers` (the parallel list)
- `_populate_fallback_provider_dropdown` (the population method)
- `_update_fallback_visibility` (the visibility toggle)
- `_get_selected_fallback_provider` (the accessor for the kept field)
- `fallback_provider` in `get_values()` (the kept field)
- The fallback **provider** restoration loop in `_fill_form` (keep this — only the model restoration loop is removed)

Required behavior after the change:
- `_on_fallback_provider_changed` body simplifies to: `self._update_save_button()`. Update its docstring to explain the unified contract.
- `_build_fallback_provider_row` no longer creates a model dropdown. It returns a row with only the "Fallback Provider" labeled dropdown.
- `_fill_form` restores `fallback_provider` only (no model restoration).
- `get_values()` returns a dict without a `fallback_model` key.
- `set_provider_options` no longer assigns to `self._provider_models` (the dict is no longer used anywhere).

Also check for and remove:
- Any `agent-builder-fallback-model` CSS rules in `ui/styles.py` (run `grep -n "fallback-model" /path/to/projects/crabcakes/ui/styles.py` first; remove any matches).

Rules:
- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md` — invoke it before writing any code.
- Read the entire current `ui/views/agent_builder.py` before making edits. Output a DISCOVERY block as steelFramedCodeWriter requires.
- Hard-part-first: remove the widget creation first, then the accessor method, then the get_values field, then the dict initialization. Verify after each removal.
- Verify every removal with grep before reporting done.

Verification commands (run all and paste output):
1. `grep -n "fallback_model\|_fallback_model_dropdown\|_fallback_model_labeled" /path/to/projects/crabcakes/ui/views/agent_builder.py` — expect zero matches
2. `grep -n "fallback-model" /path/to/projects/crabcakes/ui/styles.py` — expect zero matches
3. `cd /path/to/projects/crabcakes && python -c "import ast; ast.parse(open('ui/views/agent_builder.py').read())"` — expect no SyntaxError
4. `cd /path/to/projects/crabcakes && python -c "from ui.views.agent_builder import AgentBuilderDialog; print('imports OK')"` — expect "imports OK"

Report back with:
- Files changed (with `wc -l` output before and after)
- All four verification command outputs (full, not summarized)
- COMPLETENESS checklist:
  COMPLETENESS:
  - [done/not done] Removed `_fallback_model_dropdown` widget creation in `_build_fallback_provider_row` — evidence: line range deleted
  - [done/not done] Removed `_get_selected_fallback_model` method — evidence: method body gone
  - [done/not done] Removed `"fallback_model"` from `get_values()` return dict — evidence: dict inspection
  - [done/not done] Removed `_provider_models` initialization in `__init__` and assignment in `set_provider_options` — evidence: grep
  - [done/not done] Simplified `_on_fallback_provider_changed` body to `self._update_save_button()` — evidence: method read
  - [done/not done] Removed model-restoration loop in `_fill_form` — evidence: section read
  - [done/not done] No `fallback-model` CSS rules remain — evidence: grep
  - [done/not done] All four verification commands passed — evidence: outputs pasted
  - [done/not done] Module imports cleanly — evidence: python -c output
- Any related issues found during the related-bug scan (read 3+ lines of context before flagging duplicates) — flag only, do not fix in this phase.

please write
