# SPEC-04 Sub-Phase 1 Instructions — Deletion + Import Hygiene

**Spec:** docs/specs/SPEC-04-R5-AUXILIUM-REMOVAL.md §2 DELETE table + §5.1
**Parent plan:** docs/specs/phases/SPEC-04-SUBPHASES.md
**Scope: deletions + agent/__init__.py ONLY** (plus any import-errors that block
collection — report them, don't fix beyond the minimum). NO behavior rewires yet.

## Delete (13 items, all verified to exist)

1. `agent/kb_lookup.py`
2. `agent/kb_server.py`
3. `scripts/rebuild_kb_index.py`
4. `ui/handlers/auxilium_wizard_handler.py`
5. `ui/views/auxilium_wizard.py`
6. `prompts/system/auxilium.md`
7. `prompts/default_agents/auxilium.yaml`
8. `knowledge/.index/chunks.json`
9. `knowledge/.index/embeddings.npy` (whole `knowledge/.index/` dir)
10. `tests/test_auxilium_tier1.py`
11. `tests/test_auxilium_tier2.py`
12. `tests/test_kb_server.py`
13. `tests/test_kb_lookup.py`
14. `tests/test_kb_integration.py`
15. `tests/test_kb_provider_registration.py`

(Counted as items: 6 test files + 9 non-test paths.)

## Import hygiene (cut FIRST, before deleting — spec §5.1)

`agent/__init__.py` references kb_lookup/is_index_available (:10, :18, :32-34, :54,
:73 per spec — re-verify each line against the current tree). Cut those refs so the
package imports without the deleted modules.

Then delete, then run collection. EXPECT remaining ImportErrors/errors from files that
import the deleted modules — e.g. `agent/runtime.py` (KB_OUT_OF_SCOPE import), possibly
`utils/providers_store.py`, `ui/window.py`, `ui/views/auxilium_wizard.py` consumers.
For each: make the MINIMUM cut that restores collection (comment out / delete the
import + the direct usage lines), tag each with `# SPEC-04 SP1:` so SP2/SP3 see the
stumps. DO NOT attempt full rewires — that's SP2/SP3 work.

## Probes (report numbers BEFORE and AFTER)

1. `grep -rn "kb_lookup\|kb_server\|KB_OUT_OF_SCOPE" --include="*.py" agent/ ui/
   utils/ models/ main.py | wc -l` — before (expect ~30+) and after (expect small
   residue: only SP1-tagged stumps inside rewiring-target files)
2. Collection: `.venv/bin/python -m pytest tests/ --collect-only -q 2>&1 | tail -3`
   — must be CLEAN (no errors) post-SP1

## Verification (paste ALL, real runs)

```
.venv/bin/python -m pytest tests/ --collect-only -q 2>&1 | tail -5
.venv/bin/python -m pytest tests/test_agent_runtime.py::TestTurnStateMachine tests/test_agent_runtime.py::TestStreamErrorIntegration -q
.venv/bin/python -m pytest tests/test_error_surfacing.py tests/test_config_invalidation.py -q   # config_invalidation may need its KB case stubbed — if it fails collection, minimum-cut it too and tag
.venv/bin/python -m ruff check agent/__init__.py
```

Baselines: report ruff state of every file you touched. Zero new allowed.

## COMPLETENESS
- [ ] All 15 paths deleted (git status shows D lines)
- [ ] agent/__init__.py refs cut BEFORE deletion
- [ ] Collection clean suite-wide
- [ ] SP1-tagged stumps listed with file:line
- [ ] Probe numbers before/after
- [ ] All outputs pasted; deviations flagged
