# SPEC-03 — Sub-Phase Plan (PM direction 2026-09-22: smaller chunks)

**Spec:** docs/specs/SPEC-03-MEMORY-RATCHET.md
**Driver:** turn/length limits hit on SPEC-02's single-phase bulk — phases now sized so
each Coder round is ONE focused file-set with a small test footprint, each round
individually auditable and committable.

## Existing machinery (verified 2026-09-22 — read before building)

The v1 MEMRATCHET phases already landed the live window IN THE HANDLER:
- `MAX_LIVE_CARD_WIDGETS = 120` (feed_handler.py:38) — hard cap on retained card widgets
- `KEEP_NEWEST_CARDS = 40` (:39) — newest K never evicted
- `_evict_surplus_card_widgets` (:1944) — victim selection by seq_num, viewport-aware
  (`is_above_viewport` F10 guard), scroll-compensated, backlog push-back, Load-More
  rebuild, thread-locked (`self._lock`), well-commented (round-2/3/4/5 bug history)
- `_backlog` (:137) + `_build_load_more_widget`/`_load_more` — interactive re-hydration
- Eviction is wired into both add paths (:834-836, :974-976)
- The VIEW (feed_tab.py) is passive — handler owns the window

**What SPEC-03 actually adds on top:** (1) a configurable window (feed-prefs.json),
(2) the 2,000-card measurement harness (post-mortem §13 rows), (3) hardening around
config coupling + docs. NOT a re-architecture — the spec's §2 view-side pseudocode is
superseded by the landed handler-side mechanism (adjudicated during sub-phase drafting;
spec's §8 architecture note will be updated to reflect reality).

## Sub-phases

### Sub-phase 1 — Config accessors + clamp tests (SMALL, zero UI risk)
Files: `utils/feed_store.py` (+~25), `tests/test_feed_retention.py` (NEW).
`get_live_window()`/`set_live_window(n)` on the existing feed-prefs.json; clamp 50–5000;
`LIVE_WINDOW_DEFAULT = 300`. Tests: default/round-trip/clamps/rejects-non-int/
rejects-bool (bool-is-int trap)/corrupt-file/sibling-key-preservation/isolation sentinel
(XDG pattern from test_error_surfacing.py incl. `_fixture_config_root` stash).
Instructions: `docs/specs/phases/SPEC-03-SUBPHASE-1-INSTRUCTIONS.md` (written).

### Sub-phase 2 — Wire the window: config → handler cap (SMALL)
Files: `ui/handlers/feed_handler.py`, `tests/test_feed_retention.py` (+tests).
Read `live_window` at eviction-call time (mirror the MEMRATCHET §2.1 "read at call
time" comment at :36-38) — MAX_LIVE_CARD_WIDGETS becomes config-driven
(`max(MAX_LIVE_CARD_WIDGETS, get_live_window())`? No — replace semantics ruled in
sub-phase-2 instructions; keep KEEP_NEWEST_CARDS as an internal floor ratio, e.g.
max(40, live_window // 8), floor at 40). Tests: eviction respects configured cap;
update-card guard interplay; config-change-takes-effect-on-next-pass. NO UI changes
beyond the handler. Instructions written after SP1 lands.

### Sub-phase 3 — 2,000-card measurement harness (MEDIUM)
Files: `tests/test_feed_retention.py` (+~150 lines, the big test).
Synthetic append of 2,000 cards through the real FeedHandler.add_card with GTK
main-loop stubs (mirror tests/test_feed_handler.py's mock patterns :45-180); assert
(a) live widget count ≤ window+1, (b) disk store holds all 2,000 via
`load_all_cards`, (c) backlog accounting: window + backlog + removed ≈ 2000,
(d) no leak: handler._card_widgets length bound. Guard test runs headless (no display
needed). Instructions written after SP2 lands.

### Sub-phase 2.5 — Sort out feed.json cross-project bleed (if probe shows it)
Optional micro-phase, only if SP3's harness reveals cross-project bleed (feed.json
shared across projects — each project should have its own feed store; probe during
SP3 drafting). NOT in SPEC-03 scope, banked if not needed.

### Sub-phase 4 — Docs + spec close-out (TINY)
Files: `docs/specs/SPEC-03-MEMORY-RATCHET.md` (status → implemented; §8 architecture
note reflecting the handler-side mechanism + config key), `ARCHITECTURE.md` §Modules/Feed
note (`.crabcakes/architecture.md`), post-mortem, context.md update. No code.

## Commit plan

- SP1: feat(spec-03): config accessors + retention test scaffold
- SP2: feat(spec-03): configurable live window wired into eviction
- SP3: test(spec-03): 2k-card retention harness — invariant proven
- SP4: docs(spec-03): close-out + architecture note

## Rules carried forward

- /ask 4,096-char cap: all briefs via file path reference
- Briefs → `.crabcakes/audit-targets/` or `docs/specs/phases/`
- .venv/bin/python is THE env; never bare test_agent_runtime.py
- Falsifier-by-mutation on fix rounds; supervisor probes before adjudication
- Baselines measured before each change; zero new ruff/pyright allowed
