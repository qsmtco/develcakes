# SPEC-04 Post-Mortem — R5 Auxilium Wholesale Removal

**Date:** 2026-09-22 · **Commits:** plan `b15fd4af` → SP1 `cde9a1f6` → SP2 `548942a0`+`3ff78b6a`
→ SP3 `ad9c496b`+`381e9968` → SP4 `69ebd5ea`+`9ed04aab`+`eddc78c4` → close-out (this)
**Spec:** docs/specs/SPEC-04-R5-AUXILIUM-REMOVAL.md · **Status:** COMPLETE — 4 sub-phases

## What shipped

**Deleted (5,551 lines in SP1):** agent/kb_lookup.py, agent/kb_server.py,
scripts/rebuild_kb_index.py, auxilium wizard handler+view, prompts/system/auxilium.md,
prompts/default_agents/auxilium.yaml, knowledge/.index/ (chunks + embeddings),
6 KB/auxilium test files. sentence-transformers/PyTorch imports: zero remain (grep).

**Rewired:** runtime (sentinel import, `_format_chunks_for_llm`, `_inject_kb_context`,
`_prepare_kb_synthesis`, per-turn cache, tool-loop hook, the entire KB fallback retry
chain — orig :1645-1699), config (fresh-install defaults EMPTY at all three layers:
raw.get, seeds, dataclass fields — falsifier proved the dataclass layer was required),
providers_store (ensure_kb_provider, _ensure_auxilium_uses_kb, banner; generic
migration kept), agent_defs (helper exemption rationale, local-kb from valid_ids),
prompt_loader (helper→auxilium.md branch), window (wizard auto-open + complete
handlers + scaffolding), ARH (KB server start/stop WITH guards; local-kb key-check
skip — key check now unconditional), project_handler (special:auxilium), agent_builder
(local-kb dropdown exclusion), provider_test/conversation/special_agents (KB comments).

**Tests:** 9 residual pins (tests/test_no_kb_residuals.py); 14 legacy test files
stripped; test_special_agents fixture fixed (22/22); test_runtime_fallback deleted
(all 5 classes tested only the excised gate — adjudicated).

## Acceptance criteria (§6) — final state

- [x] Zero KB references in source (grep: 0 auxilium/kb_server/kb_lookup/KB_OUT_OF_SCOPE)
- [x] App boots with no KB server/auxilium tab (guards deleted, not swallowed)
- [x] Fresh install: empty defaults, wizard de-Auxilium-ized (v1-minus-wizard boot;
      provider-catcher is FUTURE work — decision #6's "keep the wizard" refers to a
      provider-setup wizard that does not exist yet; the auxilium wizard's code WAS
      the auxilium stack)
- [x] sentence-transformers/numpy: no runtime imports
- [x] knowledge/*.md kept (SUPERVISOR RULING 2026-09-22: plain docs, 172 KB,
      code-reference-free; gateway.md goes stale after SPEC-05 — flag it then);
      .index/ deleted
- [x] Full suite under xvfb: **3,708 passed / 12 failed (pre-existing, proven at
      pre-SP4 twice) / 3 skipped**; the bare-file ignores are the OOM-rule
      (test_agent_runtime) + reaudit-fixes fixture file; xvfb DISSOLVES the
      headless-segfault class (feed_card 79/79, feed_handler 225/225, activity_bubbles,
      chat_heading, chat_input_toolbar, chat_render_handler, chat_task_segment all
      green under xvfb) — testing phase should standardize on xvfb-run
- [x] ruff: all drops, zero new (runtime 26→23, ARH 29→24, window 14→11,
      providers_store 3→1, __init__ 7→3 net of phantom F822s)

## Findings adjudicated (13 + 1 retro + 1 latent)

- **SP1 retro-audit (ACCEPT, 0 defects in commit):** sentinel literal trace (dead-code
  carrier, cut unit identified); knowledge/README.md PHANTOM scope (never existed in
  git history — spec row struck); orphaned .pyc strays (untracked); residue-arithmetic
  undercount in commit message; interim red window (special_agents fixture) accepted
  per spec §5 ordering. **Tool lesson:** search_files returned zero hits for the KB
  sweep (false-negative) — grep -rn is the trusted sweep for deletion audits.
- **SP2 (ACCEPT, 5 findings):** fallback_provider/fallback_model now WRITE-ONLY
  plumbing (the deleted chain was the sole reader) — SUPERVISOR RULING: fields stay
  as inert plumbing (deletion = scope creep; implementing = new feature; candidate
  for a future spec); builder template seeds fallback_provider: None + validation
  requires it (same ruling, noted); pin test 5 under-powered (covers dataclass
  defaults, not _create_default_config — rides future test-hardening); stale KB
  comments (cleaned in SP3); fresh-install error string interpolation (reworded SP3).
- **SP3 (verified, no audit round flagged):** 9th file (provider_test comment)
  approved as R4-territory; pin-test #2 made machine-independent (pins by
  construction, not live config state); test_special_agents errors correctly
  refused as SP4-owned.
- **SP4 micro-fix (the big one):** SPEC-03 SP2 LATENT BUG found by SPEC-04's wider
  gate — the R1 clamp sat OUTSIDE the R5 guard, so a garbage RETURN from mocked
  get_live_window raised TypeError at the clamp, killing 15 test_feed_handler
  load-path tests since 077bdd64. Fixed (clamp inside guard, any failure shape →
  120), falsifier-proven, regression test added. LESSON: both SP2 verifications
  exercised the RAISE path; nobody probed the garbage-RETURN path. Fallback shapes
  need shape-matrix probing, not just exception probing.
- **12 pre-existing failures:** test_mcp_config (9) + test_enforcement (3) —
  environmental (enforcement's subprocess pytest needs env pytest visible; identical
  at 69ebd5ea and at HEAD). Not SPEC-04. Banked for the testing phase.

## Sub-phase pattern retrospective (PM's chunking directive)

5 sub-phases, ~10 commits, zero turn-limit failures in the build phases (one SP2
cap-hit recovered cleanly with a precise state report + resume brief). The
instructions-file pattern (pre-adjudicated rulings baked in) kept audit rounds short.
The one process miss (SP1 committed without an audit round — supervisor judged
pure-deletion as non-code-bearing) was caught by the PM and repaired with a retro-audit
(ACCEPT); the loop convention (audit every code-bearing turn) is reaffirmed.

## Register additions

- fallback_provider inert-plumbing surface (builder seeds None + validation requires
  a fallback that does nothing) — future-spec candidate
- knowledge/gateway.md staleness after SPEC-05
- test_mcp_config + test_enforcement env-dependency (12 failures, pre-existing)
- Pin-test 5 under-power (dataclass vs _create_default_config coverage)
- xvfb-run as the standard for GTK-touching suites (banked above)

## Next

**SPEC-05 (R1 gateway strip)** — all send sites repoint to the local runtime path,
transport core retained/cleaned per decision #1, Connect button survives.
