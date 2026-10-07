# SPEC-13 (Agent-Native HTML Chat) Post-Mortem

**Date:** 2026-10-06
**Supervisor:** Supervisor
**Builder:** Coder
**Auditor:** Debugger
**Commits:** 3 (b1bb14ad SP1 pipeline · c1d9bd98 SP2 wiring · <SP3/SP4 close-out>)
**Phases:** 4 (SP1 pipeline → SP1 fix round → SP2 wiring → SP3 prompts/docs → SP4 battery)
**Total findings:** SP1 audit 11 (4 MEDIUM, 7 LOW — all fixed round 1); SP2 audit CLEAN (8 attack surfaces refuted); 1 process incident (stale battery claim)
**Acceptance evidence:** PM manual test 4/4 in the running app (2026-10-06 21:03 launch): (1) markdown unchanged, (2) HTML status card renders styled, (3) mixed prose+fence stays a code block, (4) code-question fence renders as source, not UI.

---

## 1. Code Quality Grade: A (94/100)

### Justification

SPEC-13 is the smallest unit in the line by diff (3 commits, ~700 source lines incl.
tests) and the highest-leverage UX change since SPEC-06: the agent went from
*source-escapee* to *HTML author* without weakening a single sanitizer invariant —
script/iframe/event-handlers/js:/data: die exactly as before; the CSS property-name
allowlist kills every url()-bearing property by omission; `<style>` stays OUT of the
vocabulary because admitting it panics ammonia (probe-pinned, F6). The audit chain
worked as designed: SP1's 11 findings were real and all landed in one fix round; SP2
came back CLEAN with 8 refutations, not hand-waving. Points lost for the process
incident (a stale battery claim that turned out to be external tree contamination —
see §6) and for the falsifier initially mutating the wrong function (BUG#8's
falsifier-mismatch).

| Category | Score | Notes |
|---|---|---|
| Correctness | 19/20 | 4563/3sk full suite; 15/15 mutants caught post-fix; PM 4/4 manual |
| Architecture compliance | 10/10 | Pure pipeline; fail-closed preserved; JS-off untouched |
| Test coverage | 9/10 | +86 net tests; kill-proofs 15/15; gap: CRLF pin was added only on audit |
| Documentation | 10/10 | Spec/ARCHITECTURE/SPEC-06/12/README all current; no stale claims (verified by grep) |
| Maintainability | 9/10 | Two entries, one rule set (render_message); AST guard pins the entry |
| DX | 9/10 | sha-pinned reports now standard; kill-proof scripts reusable |
| **Total** | **94/100** | A |

---

## 2. What's Good About the Code

1. **The trust boundary is drawn where it belongs.** `render_message` (render/html.py)
   is a single rule set: whole-message fence → author policy; everything else → the
   unchanged markdown path. User rows, tool results, and fetched web text NEVER see
   the author vocabulary — they still get escape-first. The boundary is enforced
   structurally (one entry point) and pinned by an AST guard test, not by convention.
2. **Deny-by-omission for CSS.** The 49-property allowlist means `background`,
   `background-image`, `behavior`, `filter` — every url()-bearing property — is dead
   without listing it. F7 proved the trap: adding `background` to the set is caught
   by a test. New CSS exfiltration vectors arrive as failures, not surprises.
3. **The `<style>` probe verdict is load-bearing and recorded.** Admitting `style`
   as a tag panics ammonia (collides with clean_content_tags) — and the panic is
   swallowed by fail-closed into "" = silent policy death. F6 proves that the suite
   catches that death. The probe's verdict is in the code comment AND a test.
4. **CRLF fences promote.** Agents on Windows/autocrlf don't lose HTML cards to a
   line-ending artifact; the ruling was made during the fix round and pinned with
   3 tests + an LF-regression test.
5. **The AST compose-entry guard** (test_html_guard_sites.py) asserts
   `rd_calls == ["render_welcome"]` exactly — the welcome path is the ONLY legal
   render_document site, and both transcript sites must call render_message. K1/K2
   prove the guard kills both reverts.

---

## 3. What's Bad About the Code

1. **`render_message(123)` needed an audit to become fail-closed.** The initial
   implementation had a truthy guard (`if not text:`), which is the mock-truthiness
   anti-pattern in miniature: None → "", int → AttributeError at `.strip()`. Fixed
   with isinstance; the lesson (§10) generalizes.
2. **Per-message `<style>` blocks are not possible.** The ammonia collision means
   themed cards must carry inline styles on every element. Verbose but safe; a
   register item (spec §2a) for a future policy revisit — NOT a defect.
3. **One-sided AST guard.** render_message proliferation at new compose sites passes
   the >=2 pin. Auditor-ruled correct (the failure mode to catch is REVERSION, not
   adoption) — recorded here so nobody "tightens" it into over-fitting later.

---

## 4. Bugs Found During Audit

| # | Phase | Severity | Description | Found by | Fixed by |
|---|---|---|---|---|---|
| 1 | SP1 | MEDIUM | Fence regex greedy (.*), not spec's (.*?); 2 tests red | Debugger | Coder R1 |
| 2 | SP1 | MEDIUM | Non-str input raises at .strip() (truthy guard) | Debugger | Coder R1 |
| 3 | SP1 | MEDIUM | colspan/rowspan/name/title/alt survival unpinned (M14b/M18 GAPs) | Debugger | Coder R1 |
| 4 | SP1 | MEDIUM | CRLF fence behavior unspecified/unpinned | Debugger | Coder R1 (promote) |
| 5 | SP1 | LOW | Comment described spec's non-greedy, code was greedy | Debugger | Coder R1 |
| 6 | SP1 | LOW | name attr admits URL-shaped values, unjustified | Debugger | Coder R1 (doc) |
| 7 | SP1 | LOW | Agent filter self-fail-closed wrapper untested | Debugger | Coder R1 |
| 8 | SP1 | LOW | Falsifier M1 mutates markdown filter, not agent filter | Debugger | Coder R1 |
| 9 | SP1 | LOW | Spec said 46 CSS props, actual 49 | Debugger | Supervisor (spec) |
| 10 | SP1 | LOW | No falsifier case for greediness | Debugger | Coder R1 (F8) |
| 11 | SP2 | — | CLEAN (8 surfaces refuted, 8/8 kill-proofs) | Debugger | n/a |
| — | SP1 | incident | Stale "165 passed" claim; tree mutated externally (greedy regex + isalnum gate) | Supervisor verification | Coder R1 (repair + gap close) |

All MEDIUMs were 1–2 line fixes with pins; none was a security hole — the sanitizer
held on every probe (script/iframe/js:/data:/background:url all die, end to end).

---

## 5. Process: What Worked

1. **Pre-flight probes before the spec.** nh3 API shape (filter_style_properties is
   a SET; a callable raises TypeError), generic_attribute_prefixes set-vs-tuple,
   block_parser fence segmentation, and the attributes-map REPLACES-defaults delta
   were all probed BEFORE the spec froze — so SP1 inherited no API surprises.
2. **The supervisor's independent battery.** The stale-claim incident was caught
   because the supervisor re-runs tests before accepting. The claim was honest-at-
   the-time but the tree moved; the re-run is what mattered. (Now standard: sha pins.)
3. **Kill-proof scripts.** scratch/sp1_falsifier.py (15/15) and
   scratch/audit_sp2_killproofs.py (8/8) made every "RED" claim reproducible.
4. **PM manual acceptance in the real app.** 4/4 in the running WebKit surface —
   the unit's true exit gate, and the one test the automation cannot replace
   (WebKit rendering).

## 6. Process: What Didn't

1. **The false pass.** Coder reported 165 passed; the tree then changed under it
   (two mutations appeared: greedy regex + an isalnum class/id gate, from external
   tooling, not the builder's harness). The root cause was procedural: no sha pin
   at report time. **Rule adopted: every report pins source shas; the supervisor
   re-verifies shas + battery before any accept.**
2. **The falsifier mutated the wrong function** (BUG#8) — overclaimed coverage.
   Fix-round added the direct agent-filter pins; falsifiers now must name the exact
   function they pivot.
3. **Spec drift on a count (46 vs 49 props).** Trivial, but the count was written
   twice (spec + report) without re-deriving from the code. Fix: counts are
   asserted by test (len(_AGENT_CSS_PROPERTIES)), never quoted.

---

## 7. End-User Impact

- **Agents can now answer with designed output:** status cards, side-by-side
  comparisons, styled summaries, details/summary toggles (native, no JS), inert
  buttons. The chat reads like a product surface instead of an ASCII terminal.
- **Markdown is untouched:** plain conversation, code answers, and tool output
  render exactly as SPEC-06 left them (byte-identity pinned).
- **No new attack surface users can touch:** the author policy is agent-only; user
  input and tool results still escape. The WebView stays JS-off and non-navigating.
- **Degradation:** no-WebKit boxes get TextViewFallback tag-stripping (readable
  text, pinned).

## 8. Pre-Existing Issues (left alone deliberately)

- The ruff baseline on ui/handlers/chat_render_handler.py (12 findings: 8 RUF013,
  3 I001, 1 BLE001) predates SPEC-06 and was byte-stable through SP2 (verified
  stash-vs-work). Tier-2 cleanup, unchanged.
- pyright's 2 reportMissingModuleSource warnings on render/syntax_html.py (pygments
  stubs) — pre-existing, unrelated.
- The 42/53 pytest rm_rf tmpdir-cleanup warnings — environment noise, pre-existing.

## 9. Evolution Suggestions (Tier 2+)

| Suggestion | Effort | Impact |
|---|---|---|
| Per-message `<style>` blocks (needs a post-filter wrapper, not tag admission) | med | Themed cards without per-element inline styles |
| Agent-payload color-token policy (design-system classes like `ds-card`) | small | Consistent theming + smaller payloads |
| Streaming-render a fence once complete (currently: whole message at once) | med | Progressive card rendering on long replies |
| Dead-export sweep from SPEC-12 §9 (`get_auto_open_agents`, `render()` shim) | small | Carried over; unchanged |

## 10. Lessons Learned / Process Rules to Carry Forward

1. **Sha-pin every report; supervisor re-verifies shas before accept.** (The false
   pass is now structurally impossible to miss.)
2. **Truthy guards are type-confusion in disguise.** Any `if not text:` on an
   external entry point gets an isinstance sibling.
3. **Falsifiers must name the exact function mutated** — shared-name mutations
   (two functions, same call shape) overclaim coverage.
4. **Counts are tests, not quotes.** Any "N properties/tags/tests" claim in a spec
   or report gets a `len(...)` assert or it drifts.
5. **Probe the library's API shape before freezing the spec** (SET-vs-tuple-vs-
   callable cost one spec amendment and zero build rounds).

## 11. Sign-off

- [x] Code committed and pushed to main (b1bb14ad SP1 · c1d9bd98 SP2 · SP3/SP4 this commit)
- [x] Supervisor independent full battery: 4563 passed / 3 skipped / 0 failed (790.77s xvfb)
- [x] SP1 falsifier 15/15 · SP2 kill-proofs 8/8 (sha-verified restores)
- [x] PM manual acceptance: 4/4 in the running app (21:03 launch, verified process-newer-than-code)
- [x] Captain notified with summary
