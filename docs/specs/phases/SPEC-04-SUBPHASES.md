# SPEC-04 Sub-Phase Plan — R5 Auxilium Removal (chunked per PM direction)

**Spec:** docs/specs/SPEC-04-R5-AUXILIUM-REMOVAL.md (authoritative; §2 tables verified
against the fork 2026-09-22: all 13 delete-targets exist; 90 rewiring points across 15
source files; 14 test files carry KB references; sentence_transformers confined to the
3 deleted files — grep-verified)

## Survey results (2026-09-22, this fork)

- DELETE set (13 items): agent/kb_lookup.py (273) + agent/kb_server.py (457) +
  scripts/rebuild_kb_index.py + ui/handlers/auxilium_wizard_handler.py +
  ui/views/auxilium_wizard.py + prompts/system/auxilium.md +
  prompts/default_agents/auxilium.yaml + knowledge/.index/ (chunks.json+embeddings.npy)
  + 6 test files (test_auxilium_tier1/tier2, test_kb_server/lookup/integration/
  provider_registration — 4,286 total lines incl. tests)
- REWIRE set (15 source files, 90 points): runtime.py, __init__.py, config.py,
  window.py, agent_builder.py, project_handler.py, auxilium files (die in delete),
  agent_runtime_handler.py, agent_defs.py, prompt_loader.py, providers_store.py,
  provider_test.py
- TEST-EDIT set: spec's 14 + test_config_invalidation.py (SPEC-01's file — light touch,
  only KB-specific cases removed, nothing else)
- knowledge/*.md: STAY as plain docs (spec §7); only .index/ dies

## Sub-phases

### SP1 — Deletion + import hygiene (collect-clean)
Delete all 13 + cut agent/__init__.py refs FIRST (spec §5.1's ImportError guard), plus
the minimal import-cutting edits so `pytest --collect-only` is clean suite-wide.
No behavior rewires yet. Gate: collection clean, non-KB tests still pass.

### SP2 — Core runtime + config rewires (the heart)
agent/runtime.py (sentinel import :61-65, synthesis :317, _inject_kb_context :1037,
_prepare_kb_synthesis :1178-1233, per-turn cache :1354-5, tool-loop hook :1471-6,
KB_OUT_OF_SCOPE→fallback retry :1622-1660), agent/config.py (local-kb defaults
:239-240, _create_default_config seeding :253-272 → default_provider:"" per decision
#6), utils/providers_store.py (ensure_kb_provider :350-398, _ensure_auxilium_uses_kb
:401-446, startup call). Gate: runtime/config/store targeted tests green.

### SP3 — Remaining source rewires (surgical)
utils/agent_defs.py (:223-240 helper exemption, :440-442 valid-id set),
utils/prompt_loader.py (:165/:174/:245-246 helper→auxilium.md branches),
ui/window.py (:195-250 auto-open + wizard hooks, :1083-1109 wizard-complete),
ui/handlers/agent_runtime_handler.py (:190-193 server start, :927-8 key-check skip,
:968-9 helper exemption, :1169-71 server stop), ui/handlers/project_handler.py
(:374 special:auxilium mapping), ui/views/agent_builder.py (:389-395 fallback
dropdown), models/conversation.py (:148 comment), utils/provider_test.py (1 ref),
knowledge/README.md (index steps). Gate: full grep = zero source matches;
targeted tests green.

### SP4 — Test-file edits (the 14+1) + full-suite green
Each file loses only its KB cases; everything else stays green. Gate: full suite
(minus 6 deleted) green, ruff clean on touched files, pyright clean, final
grep zero everywhere including tests.

### SP5 — Close-out
Spec status → REMOVED, ARCHITECTURE.md §agent note, post-mortem, context.md,
commit+push per loop convention.

## Commit plan
- SP1: `refactor(spec-04): delete Auxilium subsystem — 13 files, import hygiene`
- SP2: `refactor(spec-04): core rewires — runtime sentinel/synthesis/retry, config defaults, provider store`
- SP1.5 (if needed): wizard de-Auxilium-ization — folded into SP3's window.py work
- SP3: `refactor(spec-04): surgical rewires — defs/loader/UI/handler/views`
- SP4: `test(spec-04): strip KB cases from 15 test files — suite green`
- SP5: `docs(spec-04): close SPEC-04 — post-mortem + architecture`

## Rules
Same as SPEC-03: briefs via file, .venv is THE env, probe-before-adjudicate,
falsifiers on fix rounds, baselines zero-new, supervisor owns commits.
GTK-segfault files (test_feed_card/test_feed_handler/test_activity_*) are banked —
run files separately, don't chase.
