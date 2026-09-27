# SPEC-07 (R4 Feedbar Removal) Post-Mortem

**Date:** 2026-09-26 (loop start; SP1-SP3 landed 2026-09-27)
**Supervisor:** Supervisor
**Builder:** Coder
**Auditor:** Debugger
**Commits:** 3 (7103640f SP1, cba48c85 SP2, aedff869 SP3)
**Phases:** 3 delegated subphases + 1 supervisor-owned close-out (SP1 adapter/seam → SP2 handler repoint → SP3 deletion/retirement → SP4 close-out)
**Total bugs found:** 9 (1 HIGH, 2 issue, 6 suggestion/LOW-class)
**Process:** implementationLoop.md §3.1a in full — every code-bearing turn Debugger-audited pre-commit; Coder carried steelFramedCodeWriter.md every build round; Debugger carried adversarialDebugger.md every audit round; fix rounds re-audited before commit.

---

## 1. Code Quality Grade: A- (91/100)

### Justification

The loop's core value showed: three adversarial audits caught a wrong-stylesheet defect that would have shipped a color-blind status pill, a coverage false-negative where the phase's entire deliverable (the text+state mapping) was unverified by any assertion, and a tracked-doc self-contradiction introduced by the supervisor's own fold-in. Production code landed with zero shipped defects; every finding was fixed pre-commit. The grade is held below A by two supervisor brief errors (below) and one latent bug faithfully inherited from SPEC-06 (the pill CSS rules were webview-side from day one — invisible because the pill had no callers).

| Category | Score | Notes |
|-----------------------|-------|-------|
| Correctness | 18/20 | Zero shipped defects; GTK-dedupe + layout probes by auditor; −2 for the inherited wrong-stylesheet latent |
| Architecture compliance | 10/10 | status_target duck-type per spec §2 AMENDED; surface_for_key read-only seam; settings-bar guard held |
| Test coverage | 9/10 | Args-level (text,state) pin + mutation-proof discipline; −1 for call_count-only survivors needing the audit to catch |
| Documentation | 8/10 | docs/ARCHITECTURE.md fully reconciled (§3.22 RETIRED, §3.23, §4.10, §13); features.md + research doc updated; −2 for the two rounds of doc drift the audits had to catch |
| Maintainability | 9/10 | −159 lines net; orphaned helper excised; disposition table audited |
| DX (Developer Exp.) | 9/10 | Test files gained true bare-safe claims; xvfb convention documented in-test |
| **Total** | **91/100** | **A-** |

Deducted points:
- 2 Correctness: latent SPEC-06 mismatch (pill classes in webview CSS) inherited and shipped-invisible until SP1's audit
- 1 Test coverage: 172 tests could pass with the deliverable payload corrupted (caught by audit, fixed pre-commit)
- 2 Documentation: ARCHITECTURE.md self-contradiction + knowledge/features.md stale-live claims (both fixed, but only after audit rounds)
- 1 Maintainability: orphaned `_compute_progress_fraction` + write-only `_progress_start_time` left behind by Edit 3's scoped deletions

---

## 2. What's Good About the Code

1. **The duck-type seam (`status_target` + `ActivityPillAdapter`):** the handler's render half is now target-agnostic — the SP1 adapter (ui/views/chat_surface.py:415) satisfies the exact 5-method surface the handler called, with cached catch-up on surface identity change. This makes the next render-target change (e.g. an in-webview pill, if open decision #5 goes HTML) a one-file swap.
2. **The read-only lookup (`surface_for_key`):** ui/handlers/chat_render_handler.py:272 — deliberately NOT `_surface_for()` (which creates/mounts on miss), because the 250ms status tick must be side-effect-free. The distinction is documented at the seam and pinned by a no-creation-on-miss test.
3. **Disposition discipline on deletions:** every retired test carried a subject-alive verification BEFORE retirement (the standing SPEC-06 rule), and the markup-dedupe invariant's non-re-pinning was probe-verified (auditor's GTK probe: identical set_text → +0 allocations) rather than asserted.
4. **Mutation-proof teeth:** every load-bearing pin was proven can-fail in both directions — Coder's delete-rule/duplicate-color sims, the supervisor's independent mutation reproduction (6/1/5 red), the auditor's third-mutation catch (thinking↔idle collision).

---

## 3. What's Bad About the Code

1. **Multi-surface pill display on project tabs:** N agent surfaces can be mounted in one project tab; only the project's primary surface's pill renders activity (per the Edit-4 ruling); the others sit on "Idle" until focused. Known, ruled, and registered — but a real UX wart until SP-post-MVP. Evolution: per-surface status row or pill aggregation at the tab level.
2. **The `error` pill state is production-unreachable:** `_ACTIVITY_STATE_TO_CSS["error"]` exists but ActivityHandler never enters an error state (on_agent_error → idle) — inherited v1 behavior, pinned in the map for completeness. Evolution: decide whether the machine gains an error state or the map entry dies.
3. **Ruff baselines carried:** 1458 findings repo-wide (pre-existing; this spec's files all at baseline, net −4). Evolution: a dedicated lint round (SP6-pattern) if the PM wants.

---

## 4. Bugs Found During Audit

| # | Phase | Severity | Bug | Found by | Fixed by |
|---|-------|----------|-----|----------|----------|
| 1 | SP1 | HIGH | Pill CSS rules landed in `_BASE_CSS` (webview-only) — the pill label is a GTK widget; no GTK provider had the rules; all states rendered uncolored | Debugger (probe: live GTK color check) | Coder (fix round: rules → APP_CSS) |
| 2 | SP1 | LOW | Test-file header claimed bare-safe while test 8 built real GTK (segfault on env -u DISPLAY) | Debugger | Coder (pure-fake conversion) |
| 3 | SP1-fix | issue | Distinctness pin excluded pill-thinking — docstring said six, code checked five | Debugger (re-audit) | Supervisor (fold-in) |
| 4 | SP2 | issue | The (text, state) mapping — the phase deliverable — had zero args-level assertions; 3 payload mutations survived 172 green tests | Debugger (mutation proof) | Coder (parametrized contract test) |
| 5 | SP2 | suggestion | Orphaned `_compute_progress_fraction` + write-only `_progress_start_time` | Debugger | Coder (fix round) |
| 6 | SP2 | suggestion | Three stale bar/markup docstrings | Debugger | Coder (fix round) |
| 7 | SP2-fix | suggestion | `_status_tick` branch table still said "(progress pulse)" | Debugger (re-audit) | Supervisor (fold-in) |
| 8 | SP3 | issue | docs/ARCHITECTURE.md self-contradiction: tree line said "deleted" while §3.22/§3.23/§4.10/§13 documented the module live | Debugger (Section 9 sweep) | Supervisor (in-commit fold-in) |
| 9 | SP3 | suggestion | knowledge/features.md + research doc still described FeedBar as live | Debugger | Supervisor (SP4) |

Two of nine (BUG#1-SP1, BUG#8-SP3) traced to **supervisor brief errors**, not builder work. None compounded downstream; all were caught at their phase boundary.

### Bug patterns

| Pattern | Count | Description |
|---------|-------|-------------|
| `wrong-stylesheet-target` | 1 | CSS rules placed in a document the widget never renders into |
| `untested-deliverable-mapping` | 1 | Args-level payload unverified; call-count assertions pass regardless |
| `orphaned-helper` | 1 | Deletions scoped to call sites left the computation behind |
| `stale-refactor-comment` | 3 | Docstrings/comments narrating deleted machinery |
| `stale-refactor-doc` | 2 | Tracked docs asserting a deleted module is live |
| `test-mutation-gap` | 1 | A pin whose stated contract exceeds its checked set |
| `false-test-env-claim` | 1 | Docstring asserting an environment property the file lacks |

---

## 5. Process: What Worked

1. **Pre-flight spec verification before phasing:** reading the actual code against SPEC-07's original sketch found 5 drifts (5-method vs 3-method duck-type, markup vs plain text, no-progress pill, stale line numbers, settings-bar collision) BEFORE any delegation — the spec was amended (Rule 4: fix the spec, not the code) and the phases built on true facts.
2. **The mandatory audit on every code-bearing turn:** each of the three build rounds + two fix rounds got the full adversarial probe pre-commit. The streak: SP1 caught a shipped-blind HIGH; SP2 caught the coverage false-negative; SP3 caught the doc self-contradiction. Zero shipped defects across the whole spec is the direct product of §3.1a.
3. **Mutation-proof discipline (three-way):** Coder proved pins red-first, the supervisor independently reproduced mutations, and the auditor probed for gaps the proofs missed (BUG#3: the thinking-exclusion). No pin entered the tree on assertion alone.
4. **Disposition tables on deletions:** the subject-alive-grep-before-retire rule (SPEC-06 lesson) ran as designed — all 5 retired tests verified, the dedupe invariant probe-verified rather than assumed.

---

## 6. Process: What Didn't Work

1. **Supervisor brief errors (2 shipped to Coder):** (a) SP1 Edit 1 specified `_BASE_CSS` as the stylesheet target for a GTK widget's classes — the root cause of BUG#1 (HIGH); (b) SP3 Edit 6 named a section ("§Modules / Chat surface") that does not exist in docs/ARCHITECTURE.md, causing the misplaced note and the BUG#8 self-contradiction. Lesson: briefs must name exact file paths + section anchors verified by reading the actual file — a 5-minute read before delegating would have prevented both.
2. **`/clear` failed every round** ("a tool loop is currently running") for both Coder and Debugger despite pairing with `/ask` per §9.9. Compensated by keeping payloads self-contained (file-based briefs, full context in every delegation), but context bleed risk persisted across phases. Lesson: when /clear is unavailable, briefs must carry the complete contract (they did — this is why file-based delegation is the default).
3. **The stale-pyc trap:** a same-second sed-mutate+restore of ui/styles.py reused mutated bytecode ((mtime-sec, size) validation), making a clean-restored file fail pytest. Cost ~2 min, caught before any bad conclusion. Lesson (now in context.md for all agents): same-second mutation testing requires `rm __pycache__` + `touch` between runs.

---

## 7. What the Code Actually Does (End-User Impact)

1. **Activity status now renders per-tab in the chat header pill.** Each chat tab's surface carries a pill (plain text + CSS class) that shows the agent's live state — `● Idle`, `⬡ Pre Flight Check`, `◉ Reasoning…`, `⬇ Generating… · N tokens · V tok/s · Ts`, `⚙ <tool>`, `✓ Done` — colored per state (idle gray, pre-flight/reasoning amber, streaming blue, tool violet-blue, done green). Code path: gateway/runtime events → `ActivityHandler._set_state` → `_update_status` (activity_handler.py) → `ActivityPillAdapter.set_status_text` → `surface.set_activity_status` → `Gtk.Label` swap (ui/views/chat_surface.py:282).
2. **The Response Status bar is gone.** The right pane that held the 40px FeedBar above the chat is deleted; main content now fills the pane (auditor's layout probe verified the reclaimed allocation). Progress-bar semantics (fraction/pulse/hidden) retired with it — the pill is text+color only. Code path: window.py right_box assembly (single child now) + the deleted ui/views/feedbar.py.
3. **Tab switching keeps the pill honest:** the adapter caches the last (text, state) and re-applies on surface identity change, so a tab reopened mid-stream shows the current status rather than a stale "Idle". Code path: `ActivityPillAdapter.set_status_text` catch-up branch (chat_surface.py:445-452).

---

## 8. Pre-Existing Issues Flagged (Not Caused by This Implementation)

1. **Pill CSS rules webview-side since SPEC-06:** the 4 original `.pill-*` rules lived in `_BASE_CSS` from the day the pill was built (SPEC-06); invisible because `set_activity_pill` had zero production callers. Verified pre-existing at 3972ac9e. Fixed as part of SP1-fix (rules moved to APP_CSS) because SP2 would have exposed it.
2. **`error` state unreachable in the machine:** inherited from v1 FeedBar behavior (on_agent_error → idle). Pre-existing; noted in §3.2 above.
3. **Ruff baselines (1458 repo-wide) + 12 chat_render_handler findings + ruff-format drift in chat_surface.py:** all pre-existing at HEAD, verified by stash-diff each round.
4. **test_enforcement (3) + test_mcp_config (9) env-broken reds:** dispositioned in SPEC-06 SP6 P2; carried, not caused here.

---

## 9. Evolution Suggestions (Tier 2+)

| Suggestion | Effort | Impact |
|------------|--------|--------|
| Per-surface status row or tab-level pill aggregation for project tabs (N mounted surfaces) | ~1 day | Fixes the "only primary surface's pill renders" wart |
| Decide + implement (or delete) the `error` pill state | ~2h | Either a real error surface or one less dead map entry |
| Autoscroll restore on the WebKit surface (register from SPEC-06 SP5a) | ~1-2 days | Streaming UX: keep scroll position across re-renders |
| Repo lint round (1458 findings, 805 auto-fixable) | ~0.5 day | Baseline hygiene; unblocks future per-file-delta gating |

---

## 10. Lessons Learned / Process Rules to Carry Forward

1. **Brief-anchors-verified rule:** every file path + section anchor in a delegation brief must be verified by reading the actual target file before the brief ships.
   - Trigger: writing any Edit-N that names a file/section/line.
   - Action: read the target; if the anchor doesn't exist, fix the brief, not the builder's output.
2. **Stylesheet-target rule:** when a phase styles a widget, the brief must name WHICH stylesheet the rules belong in (GTK `APP_CSS` vs webview `_BASE_CSS`) — matching where the widget actually renders.
   - Trigger: any brief adding CSS classes/rules.
   - Action: name the stylesheet + the provider that loads it.
3. **Args-level pin rule:** a render-path repoint's deliverable (the payload mapping) needs at least one args-level assertion (call_args / recording fake), not only call-count survivors.
   - Trigger: any phase whose deliverable is "X now renders Y".
   - Action: require a parametrized (payload) contract test + mutation proof in the same phase.
4. **Same-second mutation hygiene:** mutation testing that edits + restores a file within the same mtime-second must `rm -rf <pkg>/__pycache__` and `touch` the source between runs, or pytest may reuse mutated bytecode.
   - Trigger: any sed-based mutation proof.
   - Action: clear caches + touch; verify restore with `diff` against a backup.

---

## 11. Sign-off

- [x] Code committed to main (7103640f, cba48c85, aedff869) and pushed with this post-mortem
- [x] All post-loop verification commands run and pasted (repo greps 0; 3980 passed/2 skipped full suite; ruff at deletions-only baseline; pyright clean on scope files)
- [x] Captain notified with summary
- [x] Tier 2+ backlog updated (§9 table; register items in context.md)
