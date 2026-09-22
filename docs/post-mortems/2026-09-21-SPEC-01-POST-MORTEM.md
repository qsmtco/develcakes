# SPEC-01 Post-Mortem — Runtime Provider-Config Invalidation

**Date:** 2026-09-21
**Spec:** docs/specs/SPEC-01-CONFIG-INVALIDATION.md
**Commits:** 4ebd2a4b (Phase 1), 9b48dc30 (Phase 2), d3068651 (chore)
**Loop:** Coder 3 build rounds + 1 fix round; Debugger 3 adversarial audits
(6+6 findings Ph1, 10 findings Ph2); all adjudicated by supervisor with
independent probes.

## Outcome

SPEC-01 is COMPLETE — both phases committed and pushed. A provider edit in
Settings now reaches every cached runtime **on the next call**, no restart:

- **Phase 1 (refresh path):** `SettingsHandler` save → `on_providers_changed` →
  `MainWindow._on_providers_changed` → `AgentRuntimeHandler.refresh_provider_config()`
  swaps each cached runtime's provider dict for per-key clones (strict-parse guard;
  missing/corrupt yaml keeps old snapshots; valid-empty applies).
- **Phase 2 (live lookup):** `_call_llm` resolves the live provider card once per
  call and refreshes `base_url` (stripped) and `caller` (strip+lower+validated)
  in place before the streaming/non-streaming split. The original incident
  (corrected z.ai base_url never reaching running runtimes) is fixed for ALL
  agents, including per-agent-key agents the old gate skipped.

## What the audit process caught

1. **Cross-runtime dict sharing (Ph1 round 2, HIGH):** one shared `providers`
   dict would have leaked Phase 2's in-place mutations across agents — caught
   BEFORE Phase 2 wrote the mutation that would have exposed it. Fixed via
   per-key `dataclasses.replace`/deepcopy clones.
2. **Truthy-but-invalid caller poisoning (Ph2, HIGH/MED):** `"   "` or `"WXYZ"`
   in a hand-edited yaml would overwrite a valid frozen caller and brick the
   runtime until restart. Supervisor probes A/B confirmed; fix = validate
   against `_PROVIDER_CALLERS`, warn + keep frozen on invalid. Two
   regression tests would have failed pre-fix.
3. **A false finding, correctly rejected (Ph2 #4):** the auditor claimed
   earlier prefix-match entries shadow later name-matches. Supervisor probe
   proved the two-pass scan is order-independent — behavior now pinned by
   `test_name_match_wins_regardless_of_live_list_order` so a *real* regression
   of that property would fail loudly.

## Process lessons (banked)

- **The adjudication lesson compounds:** across Ph1+Ph2 audits, 3 of 22 findings
  rested on misread code (arity misread; spec-seeder cited as registry write;
  loop-structure misread). Every accepted/rejected verdict this spec was
  evidence-probed by the supervisor before routing fixes. Keep doing this.
- **Phase interleaving paid off:** the Phase 1 clone fix created the safety
  premise for Phase 2's mutation; sequencing refresh-then-mutation let the
  audit verify the premise before the dangerous code existed.
- **Fix-round payloads truncate:** the Fixes 2–4 brief arrived cut off; Coder
  correctly stopped at the boundary instead of guessing. Resend protocol worked.
- **Truncation of spec sketches is a spec-drift vector:** the instructions'
  line ranges drifted twice (+20 then +2 lines); describe blocks by sentinel
  as well as range when writing future phase instructions.

## Deferred / banked (pre-existing, not this spec)

| Item | Where noted |
|---|---|
| Fallback branch `:2119–2126` precedence undocumented (first-provider-frozen key wins, live never consulted) | audit #8; hardening register |
| Duplicate provider names in yaml: first-wins, undocumented | audit #3; hardening register |
| `refresh_provider_config` propagates invalid callers from yaml into snapshots (per-call path now validates; refresh path inherits safety only after yaml is fixed) | supervisor probe A2; hardening register |
| ARH:1882 `agent_def.runtime_id` AttributeError (5c87189a) | pre-existing register |
| Full-suite GTK segfault (`test_activity_bubbles`, gi/cairo) — reproduces at clean HEAD under `/tmp/spec01venv2` | testing phase; environment debt |
| Repo-wide `ruff format` drift + no `[tool.ruff]` config — format gate unenforceable per-file | PM decision needed |
| No develcakes-dedicated venv (tests ran via `/tmp/spec01venv2`) | tooling debt |
| BUG 8 clear/update race (older banked) | hardening register |

## Next

SPEC-02 (error surfacing + empty-assistant-message rollback) — the second
banked incident fix from 2026-09-20 provider forensics.
