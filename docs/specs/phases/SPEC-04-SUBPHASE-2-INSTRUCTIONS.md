# SPEC-04 Sub-Phase 2 Instructions — Core Rewires: runtime + config + providers_store

**Spec:** docs/specs/SPEC-04-R5-AUXILIUM-REMOVAL.md §2 REWIRE (runtime/config/store rows)
**Scope: exactly 3 files** — `agent/runtime.py`, `agent/config.py`,
`utils/providers_store.py`. Nothing else. SP3 owns defs/loader/UI/handler/views.

## Rulings (binding)

**R1 — KB_OUT_OF_SCOPE retry dies completely** (PM ruling #4: fallback retry removed
with the KB stack). Spec §3: "`fallback_provider` on agents remains a plain provider
fallback if user sets one — only the KB-sentinel-triggered retry dies." The
:1622-1660 region: delete the sentinel branch entirely; keep any NON-KB fallback
mechanism intact. Read carefully — do not over-delete.

**R2 — Fresh install = empty provider defaults** (decision #6). `agent/config.py`:
`default_provider: ""` / `default_model: ""`; `_create_default_config` seeds nothing
(no local-kb, no auto-seeded provider). The wizard (de-Auxilium-ized in SP3) is the
catcher.

**R3 — providers_store:** `ensure_kb_provider()` + `_ensure_auxilium_uses_kb()` and
their startup call die. `migrate_from_agent_json()` stays (generic, non-KB).
Existing `local-kb` cards in user providers.yaml STAY (inert — spec §7).

**R4 — Dead code leaves no tombstones.** Delete the lazy try/except sentinel import
(:61-66) AND its fallback literal, `_inject_kb_context` (:1037 region),
`_prepare_kb_synthesis` (:1178-1233), per-turn cache (:1354-1355), tool-loop hook
(:1471-1476). Comments describing KB behavior die with the code. No
"# removed KB here" markers — git history is the record.

**R5 — Line numbers drifted.** Spec's tables are v1-era. Re-locate each site by
pattern (grep `KB_OUT_OF_SCOPE`, `_inject_kb_context`, `_prepare_kb_synthesis`,
`_kb_cache_for_turn`, `local-kb`, `ensure_kb_provider`). Report actual line ranges
in your report.

## Task order

1. `agent/runtime.py`: R4 deletions + R1 retry-region excision. After: grep
   `KB_OUT_OF_SCOPE\|kb_` in runtime.py = 0.
2. `agent/config.py`: R2 defaults + seeding removal.
3. `utils/providers_store.py`: R3 removals.
4. Re-run residue grep on all three files = 0.

## Tests (NEW file `tests/test_no_kb_residuals.py` — small, ~6 tests)

The grep-as-regression-suite (SP4 will strip KB cases from the 14 legacy files; this
file is the durable pin):
1. `test_runtime_source_has_no_kb_refs` — read agent/runtime.py source text; assert
   no "KB_OUT_OF_SCOPE", "_inject_kb_context", "_prepare_kb_synthesis", "kb_server".
2. `test_config_source_has_no_local_kb` — same for agent/config.py + "local-kb".
3. `test_providers_store_has_no_ensure_kb` — same for utils/providers_store.py.
4. `test_no_kb_modules_exist` — `importlib.util.find_spec("agent.kb_lookup")` and
   `("agent.kb_server")` are None.
5. `test_fresh_config_has_empty_provider_defaults` — construct via
   `_create_default_config`-equivalent path (or the real default constructor):
   default_provider == "" and default_model == "".
6. `test_runtime_imports_clean` — `import agent.runtime` in a fresh subprocess
   (timeout=30), exit 0.

## Verification (paste ALL, real runs)

```
.venv/bin/python -m pytest tests/test_no_kb_residuals.py -q
.venv/bin/python -m pytest "tests/test_agent_runtime.py::TestTurnStateMachine" "tests/test_agent_runtime.py::TestStreamErrorIntegration" tests/test_error_surfacing.py tests/test_config_invalidation.py -q
.venv/bin/python -m pytest tests/test_providers_store.py tests/test_llm_providers.py -q
.venv/bin/python -m ruff check agent/runtime.py agent/config.py utils/providers_store.py tests/test_no_kb_residuals.py
.venv/bin/python -m pyright agent/config.py utils/providers_store.py 2>&1 | tail -1
.venv/bin/pyright agent/runtime.py 2>&1 | tail -1
```

Baselines (measure + report BEFORE editing): runtime.py ruff 26 / pyright 18;
config.py + providers_store.py — measure. NEW: ruff counts will DROP (deleted code
carried findings) — that's expected, report old→new. pyright likewise (18 → fewer;
the deleted regions carried some). Zero NEW findings is the rule.

## COMPLETENESS
- [ ] R1–R5 each addressed; actual line ranges reported (R5)
- [ ] 6 residual-pin tests green
- [ ] All 6 outputs pasted
- [ ] Baselines before/after reported
- [ ] Deviations flagged (especially any R1 over/under-deletion judgment calls)
