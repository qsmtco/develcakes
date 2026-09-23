# SPEC-04: R5 — Auxilium/KB Wholesale Removal

**Date:** 2026-09-20
**Author:** Supervisor (develcakes v2)
**Status:** REMOVED 2026-09-22 (sub-phases 1-4; see docs/post-mortems/2026-09-22-SPEC-04-POST-MORTEM.md)
**Implements:** docs/proposals/DEVELCAKES-V2-CHANGE-LIST.md §5 R5 (with [SUP-REV] notes)
**Depends on:** none (parallel with SPEC-03)
**Target branch:** main

> Architecture compliance: KB stack deleted; `KB_OUT_OF_SCOPE` fallback removed with it
> (PM ruling #4, 2026-09-20). Wizard survives per discovery decision #6.

---

## 1. Overview

**Problem.** Auxilium is a RAG stack wearing an agent costume: `agent/kb_lookup.py` (273
lines), `agent/kb_server.py` (457; localhost OpenAI-compatible server impersonating
provider "local-kb"), a synthesis layer POSTing to a third-party **unauthenticated**
endpoint, `BAAI/bge-small-en-v1.5` via sentence-transformers (pulls PyTorch ~700 MB), and
a first-run wizard wiring it up.

**Solution.** Delete the whole subsystem (~2,300 lines + 6 test files + assets), rewire
~19 files surgically, keep the onboarding wizard **with local-kb steps removed**
(decision #6: user configures a provider).

**Scope**

| In | Out |
|---|---|
| All Delete/Rewire tables below | knowledge/*.md deletion (see §7 — they stay as docs) |
| Wizard de-Auxilium-ization | R1 gateway work (SPEC-05) |
| KB_OUT_OF_SCOPE retry removal from runtime | |

## 2. Changes by File

### DELETE (pure Auxilium)

| File | Verified exists |
|---|---|
| agent/kb_lookup.py | ✓ |
| agent/kb_server.py | ✓ |
| scripts/rebuild_kb_index.py | ✓ |
| ui/handlers/auxilium_wizard_handler.py | ✓ |
| ui/views/auxilium_wizard.py | ✓ |
| prompts/system/auxilium.md | ✓ (prompts/system/) |
| prompts/default_agents/auxilium.yaml | ✓ |
| tests/test_auxilium_tier1.py, test_auxilium_tier2.py, test_kb_server.py, test_kb_lookup.py, test_kb_integration.py, test_kb_provider_registration.py | ✓ |
| knowledge/.index/ (chunks.json + embeddings.npy) | ✓ |

### REWIRE (surgical edits)

| File | What comes out |
|---|---|
| agent/runtime.py | `KB_OUT_OF_SCOPE` import (:61-65), synthesis helper (:317), `_inject_kb_context` (:1037), `_prepare_kb_synthesis` (:1178-1233), per-turn cache (:1354-1355), tool-loop hook (:1471-1476), `KB_OUT_OF_SCOPE`→fallback retry (:1622-1660) |
| agent/config.py | `default_provider`/`default_model` `local-kb` defaults (:239-240), `_create_default_config` seeding (:253-272) |
| utils/providers_store.py | `ensure_kb_provider()` (:350-398), `_ensure_auxilium_uses_kb()` (:401-446), startup call site |
| utils/agent_defs.py | helper-role llm/fallback exemption (:223-240), `local-kb` in valid-id set (:440-442) |
| utils/prompt_loader.py | `helper`→auxilium.md branches (:165, :174, :245-246) |
| ui/window.py | auto-open tab + wizard hooks (:195-250), wizard-complete handler (:1083-1109) |
| ui/handlers/agent_runtime_handler.py | KB server start (:190-193), key-check skip for local-kb (:927-928), helper no-project exemption (:968-969), KB server stop (:1169-1171) |
| ui/handlers/project_handler.py | `special:auxilium` mapping (:374) |
| ui/views/agent_builder.py | `local-kb` exclusion in fallback dropdown (:389-395) |
| models/conversation.py | `agent_role == "helper"` comment (:148) |
| agent/__init__.py | kb_lookup/is_index_available refs (:10, :18, :32-34, :54, :73) |
| knowledge/README.md | KB index install/troubleshooting steps |

Line numbers are from the v1 change list §5 R5 tables, verified against the fork's tree
(all files exist at stated paths; builder re-verifies each edit site before cutting —
fork may have drifted a few lines from v1).

### Tests needing edits (~14)

test_agent_defs, test_special_agents, test_settings_dialog, test_settings_handler,
test_agent_builder_dialog, test_agent_builder_fallback, test_agent_builder_handler,
test_agent_config_yaml_fallback, test_bug_fixes, test_mcp_integration,
test_mcp_tool_naming, test_provider_test, test_runtime_caller_resolution,
test_runtime_fallback — each loses its local-kb/auxilium cases; keep the rest green.

### Fresh-install behavior (decision #6)

`_create_default_config` writes `default_provider: ""` / `default_model: ""`; the wizard
survives as the provider-setup catcher (its Auxilium steps removed; it now lands the user
in Settings → Providers). No auto-seeded provider. `ensure_kb_provider()` call removed
from startup; `migrate_from_agent_json()` (generic, non-KB) may remain wired.

## 3. Data Flow

Post-R5 send path: input → runtime.send_message → context build (no `_inject_kb_context`)
→ provider stream (no kb fallback retry; `fallback_provider` on agents remains a **plain
provider fallback** if user sets one — only the KB-sentinel-triggered retry dies).
Startup: no KB server start, no auxilium tab auto-open, no ensure_kb_provider.

## 4. File Change Summary

~26 deletions + ~19 rewires + ~14 test edits. Est. **−2,300 source lines**, −2,288 test
lines. Risk: med-high breadth, low depth (pure removal + wiring cleanup).

## 5. Implementation Order

1. Delete the six test files + pure-Auxilium files; suite still collects (imports in
   `agent/__init__.py` cut first to avoid ImportError).
2. Rewire agent/runtime.py (sentinel + synthesis + retry) — run full suite.
3. Rewire config/providers_store/agent_defs/prompt_loader (defaults + seeding).
4. Rewire UI (window, agent_runtime_handler, project_handler, agent_builder).
5. `grep -rn "kb_lookup\|kb_server\|KB_OUT_OF_SCOPE\|local-kb\|auxilium" --include="*.py"`
   → zero matches outside tests being edited; then zero in tests.
6. Full suite + ruff + pyright; `pip uninstall`-class check: nothing imports
   sentence-transformers/numpy outside deleted files (grep `sentence_transformers`).

## 6. Acceptance Criteria

- [ ] Zero `kb_lookup`/`kb_server`/`KB_OUT_OF_SCOPE` references in source
- [ ] App boots with no KB server, no auxilium tab, no auto-seeded provider
- [ ] Fresh install lands in wizard → provider configuration (no local-kb option)
- [ ] `sentence-transformers`/`numpy` imports gone from runtime code
- [ ] knowledge/*.md retained as plain docs; `.index/` deleted
- [ ] Full pytest green (minus the 6 deleted KB test files), ruff clean, pyright clean

## 7. Edge Cases

| Case | Behavior |
|---|---|
| Existing install has `local-kb` card in providers.yaml | Stays (harmless inert card); wizard doesn't remove user data |
| Agent YAML still says `llm_name: local-kb` (user-created) | Load-time validation warns "Unknown provider" (existing behavior once valid-id set shrinks); agent stays inert until user fixes |
| knowledge/*.md | **Stay** as plain documentation (PM call recorded in change list §5 sub-decision — cheap to keep; delete only `.index/`) |
| Conversations referencing helper role | Load fine; role no longer special-cased (comment-only change) |
| pyproject deps | faster-whisper stays (STT removal is §6.1 post-MVP); sentence-transformers was never declared — nothing to remove from pyproject |

## 8. ARCHITECTURE.md Updates

§Modules/agent — strike KB bug-fix deltas referencing KB paths; note removal done.
