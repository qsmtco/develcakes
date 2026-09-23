# SPEC-04 Sub-Phase 3 Instructions — Surgical Rewires: defs/loader/UI/handler/views

**Spec:** docs/specs/SPEC-04-R5-AUXILIUM-REMOVAL.md §2 REWIRE (remaining rows)
**Scope: exactly these files** — `utils/agent_defs.py`, `utils/prompt_loader.py`,
`ui/window.py`, `ui/handlers/agent_runtime_handler.py`,
`ui/handlers/project_handler.py`, `ui/views/agent_builder.py`,
`models/conversation.py`, `agent/special_agents.py` (comment-only, audit #4).
**NOT in scope:** the `fallback_provider` field surface — Supervisor ruling 2026-09-22:
fields stay as inert plumbing (deleting = scope creep across builder/validation/
persistence; implementing = new feature). Only the audit riders below touch it.

## Rulings

**R6 — the fallback dropdown survives but with honest labels** (audit #1/#2
disposition). `agent_builder.py`'s fallback row stays; if its option list references
local-kb or KB semantics, strip just those. `agent_builder_handler.py:70`'s
`fallback_provider: None` seed stays as-is (validation's `must have a fallback`
requirement at agent_defs.py:392 also STAYS — both are out of scope; noted for a
future spec).
**R7 — window.py wizard hooks:** the Auxilium wizard is DELETED (SP1); window.py's
auto-open + wizard-complete handlers (:195-250, :1083-1109 per spec — re-locate by
pattern: `auxilium`, `wizard`) must go, including `is_auxilium_wizard_needed` and
`_config_dir` scaffolding if it exists only for the wizard. The wizard's other
purpose (provider-setup catcher per decision #6) is FUTURE work — decision #6 said
"keep the wizard" but the auxilium wizard's actual code IS the auxilium stack; the
provider-configuration catcher does not exist yet. Do NOT build it. Leave the boot
flow as v1-without-wizard (document in report).
**R8 — ARH KB server start/stop:** :182-199 (calls ensure_kb_provider — now
nonexistent — and starts the KB server) + :1280-1284 stop: delete the blocks.
The try/except guards die too (R4: no tombstones). The helper no-project exemption
and key-check skip for local-kb (:927-8, :968-9 per spec) — re-locate by pattern,
delete.
**R9 — comments referencing KB on live fields die** (audit #4):
models/conversation.py:174-175, agent/special_agents.py:43 + any similar
(agent_defs.py:223-232, :440 region comment references per Coder hand-off).
**R10 — BUG #5 rider (audit):** agent_runtime_handler.py:927
`RuntimeError(f"No provider configured for {config.default_provider}")` → reword to
`"No provider configured — add one in Settings → Providers."` (no interpolation).
**R11 — project_handler.py:374 `special:auxilium` mapping:** delete the entry.
prompt_loader.py helper→auxilium.md branches (:165/:174/:245-246 pattern `auxilium`):
delete branches. NOTE: `helper` role may still exist as a concept — only the
auxilium.md file branches die; if the loader needs a fallback for helper-role agents
without auxilium.md, use the standard default prompt path and note it in the report.
**R12 — knowledge/README.md row in the spec is PHANTOM** (retro-audit BUG #2: file
never existed in git history). Nothing to do — ignore the spec row.

## Tests

Add to `tests/test_no_kb_residuals.py` (it's the durable pin file):
1. `test_source_tree_free_of_kb_strings` — grep agent/ ui/ utils/ models/ main.py
   source for `auxilium`, `kb_server`, `kb_lookup`, `local-kb` → assert zero matches
   (whitelist nothing; if a hit is legitimately needed, report instead of whitelisting).
2. `test_special_auxilium_session_unknown` — the special-agents registry does not
   contain special:auxilium (import and check, mirroring test_special_agents.py's
   pattern — but keep it cheap/no-yaml-iteration to avoid the FileNotFoundError class).
3. `test_window_has_no_wizard_refs` — ui/window.py source contains neither
   `auxilium` nor `is_auxilium_wizard_needed`.

## Verification (paste ALL, real runs)

```
.venv/bin/python -m pytest tests/test_no_kb_residuals.py -q
.venv/bin/python -m pytest tests/test_special_agents.py -q                # the 5 interim errors from retro-audit BUG #5 should go green
.venv/bin/python -m pytest tests/test_provider_test.py tests/test_runtime_caller_resolution.py -q
.venv/bin/python -m ruff check utils/agent_defs.py utils/prompt_loader.py ui/window.py ui/handlers/agent_runtime_handler.py ui/handlers/project_handler.py ui/views/agent_builder.py models/conversation.py agent/special_agents.py
.venv/bin/pyright ui/handlers/agent_runtime_handler.py ui/window.py 2>&1 | tail -1
```

Measure + report each file's ruff/pyright BEFORE editing (ARH ruff baseline 29 /
pyright 0; others measure). Zero new allowed; drops expected.
NOTE: test_special_agents.py's fixture iterates default_agents/*.yaml — after the
auxilium.yaml deletion (SP1) it currently errors (5); your R11/R7 work should fix it
without touching the test file. If it needs a test-file fix instead, flag it (SP4 owns
test edits).

## COMPLETENESS
- [ ] R6–R12 each addressed (R12 = no-op, say so)
- [ ] 3 pin tests added
- [ ] All 5 outputs pasted + baselines
- [ ] Deviations flagged (especially R7's boot-flow judgment and R11's loader fallback)
