# SPEC-12 (Project-First Group Chat) Post-Mortem

**Date:** 2026-10-05
**Supervisor:** Supervisor
**Builder:** Coder
**Commits:** 8 (44a2d211 SP1 · 464dea2b SP2 · 2fa9c37d SP3 · 906797d6 SP4a · c13825bd SP4b+c · 4bb4c84f SP5 · ea0c7b6c SP6 · <SP7>)
**Phases:** 8 (SP1 → SP2 → SP3 → SP4a/b/c → SP5 → SP6 → SP7 close-out; SP2 split into
2a/2b, SP3 into 3a/3b/3c, SP4 into 4a/b/c)
**Total bugs found:** 20 audit findings (2 MEDIUM, 9 issue/probe findings, 9 suggestion) + 4 spec bugs caught in pre-flight
**Process:** supervisor (phase + verify) → coder (steelFramedCodeWriter) → debugger (adversarialDebugger, every code-bearing turn) → supervisor independent verification (tests/diffs/greps/mutations)

---

## 1. Code Quality Grade: A- (91/100)

### Justification

SPEC-12 is the cleanest large surface-keying migration in the MVP line so far. The
spec's line-level inventory was verified against HEAD in pre-flight and matched almost
exactly; the four spec defects found were corrected before any code was written, so no
phase wasted a round on a broken contract. The adversarial audit earned its keep: it
caught a real MEDIUM attribution bug (an agent named "You" merging with the user's rows)
in SP1 and a real MEDIUM race (an unconditional terminal `finally` clobbering a nested
same-key send's reply slot) in SP3b — both in exactly the feature being added, both with
reproductions. Every phase ended green with a mutation-proven discrimination check, and
the final full suite is 4469 passed / 0 failed. Points were lost for repeated
test-quality gaps (tautological or unpinned assertions) that forced six fix rounds, and
for baseline drift in the briefs (recurring across the line — see §6).

| Category              | Score | Notes |
|-----------------------|-------|-------|
| Correctness           | 19/20 | Full suite green; 2 real MEDIUM bugs caught + fixed in-phase; no defects reached SP7 |
| Architecture compliance | 10/10 | Display-key model per architecture; handlers GTK-boundary preserved; R7 routing clean |
| Test coverage         | 8/10 | Strong mutation-proofs, but 6 fix rounds came from unpinned/tautological asserts |
| Documentation         | 9/10 | ARCHITECTURE.md updated; stale provenance docstrings fixed; a few sweep candidates remain |
| Maintainability       | 9/10 | Two clean key domains; `_reply_key`/`_ensure_target_tab` helpers avoid duplication |
| DX (Developer Exp.)   | 9/10 | clear key-domain table; well-commented invariants |
| **Total**             | **91/100** | A- (strong, with recurring test-rigor misses) |

Deducted points:
- 2 Test coverage: six fix rounds for unpinned/tautological test assertions (SP1-BUG#2, SP2b-T3, SP3c-BUG#18, SP4a/SP4b+c coverage gaps)
- 1 Correctness: the SP3a guard-2 stale-slot drop and the SP1 "You" collision were both reachable
- 1 Documentation: baseline numbers in briefs were stale (ruff "0" claims) — but not introduced here

---

## 2. What's Good About the Code

1. **One display key, resolved once (SPEC-12 §2b key-domain table):** every
   surface-lifecycle structure uses `display_key = mount_key or session_key`, and
   streaming (`_stream_text`/`_streaming`) + reentrancy stay session-keyed.
   `chat_render_handler.py:_surface_for` — one variable, one domain; the auditor could
   not find a mixed-domain path.
2. **Turn-scoped reply target with a documented clear discipline:**
   `agent_runtime_handler._turn_reply_target` + `_reply_key` — set on every send entry
   after both guards, read at 11 render sites, and popped in a **turn-guarded** `finally`
   (`if token is not None and _turn_tokens.get(sk) is my_token`). The nested-send fix
   (SP3b-BUG#1) and the None-token residual are both precisely scoped.
3. **R7 "never dropped" routing:** `_resolve_mount_key`/`_resolve_chat_box` fall back to
   the ACTIVE project (agent's project → active project → raw session key), so an
   unrouted agent (e.g. Supervisor before it is added) renders into the open project
   surface instead of dropping — closing the class the old auto-open had masked.
4. **Mutation-proven discrimination on every phase:** each fix round ships a RED proof
   that the mutant is killed and the source is sha-restored (e.g. SP4b+c's six per-site
   mount_key proofs; SP4a's create_chat_tab-removal proof).

---

## 3. What's Bad About the Code

1. **The test-rigor gap is process, not code, but it cost six rounds.** Six fix rounds
   (SP1, SP2b, SP3b, SP3c, SP4a, SP4b+c) were driven by assertions that did not
   discriminate — a tautological T3 (SP2b-BUG#1), a vacuous idempotency half
   (SP4a-BUG#1 residual), an unpinned send-path normalization (SP3c-BUG#18), and 5 of 6
   unpinned mount_key sites (SP4b+c-BUG#2).
   - Evolution suggestion: bake a "does this assert fail against a revert?" step into
     the builder's delivery checklist for any pin of new behavior, before the auditor
     has to find it.
2. **Two clean-up orphans from the removal.** `get_auto_open_agents` is now dead
   (docstring corrected, removal deferred) and `chat_render_handler.render()` is a
   zero-caller legacy shim.
   - Evolution suggestion: a small dead-export sweep unit deletes both.

---

## 4. Bugs Found During Audit

| # | Phase | Severity | Bug | Found by | Fixed by |
|---|-------|----------|-----|----------|----------|
| 1 | SP1 | MEDIUM | Agent named "You" merged with the user's rows (name-keyed grouping) | Debugger (probe) | Coder (1 round) |
| 2 | SP1 | LOW | `role-agent` box class unpinned (always-role-user mutant passes) | Debugger (mutation) | Coder |
| 3 | SP1 | SUGGEST | CSS comment mis-modeled descendant matching | Debugger | Coder |
| 4 | SP2b | MEDIUM | T3 passed under a session-keyed revert (tautological) | Debugger (mutation) | Coder |
| 5 | SP2b | LOW | fan-out loop lost coverage after the rewrite | Debugger | Coder |
| 6 | SP2b-fix | ISSUE | the rewritten idempotency assert was vacuous | Debugger | Supervisor |
| 7 | SP3a | MEDIUM | guard-2 (no project) left a stale slot → its error dropped/misrouted | Debugger (probe) | Supervisor |
| 8 | SP3a | SUGGEST | `_resolve_mount_key` annotation `str|None` stale | Debugger | Supervisor |
| 9 | SP3a | SUGGEST | `or session_key` idiom dead | Debugger | scope-deferred |
| 10 | SP3b | MEDIUM | unconditional terminal pop clobbered a nested same-key send's slot | Debugger (probe) | Supervisor |
| 11 | SP3b | SUGGEST | class docstring implied unconditional pop | Debugger | Supervisor |
| 12 | SP3b-fix | LOW | None-token path degenerated the guard (deferred/legacy pop) | Debugger (probe) | Supervisor |
| 13 | SP3c | ISSUE | BUG#18 send-path normalization unpinned | Debugger (mutation) | Coder |
| 14 | SP3c-fix | SUGGEST | SP3a guard-2 clear + SP3b None-token skip unpinned | Debugger | Supervisor |
| 15 | SP4a | ISSUE | private-tab-open behavior unpinned | Debugger (mutation) | Supervisor |
| 16 | SP4a | SUGGEST | ARH-None fallback comment overstated delivery | Debugger | Supervisor |
| 17 | SP4a | SUGGEST | closure late-read `result.forward_text` | Debugger | Supervisor |
| 18 | SP4b+c | ISSUE | solo-DM reply_target unpinned | Debugger (mutation) | Coder |
| 19 | SP4b+c | SUGGEST | 5 of 6 mount_key sites unpinned | Debugger (mutation) | Coder |
| 20 | SP5/SP6 | SUGGEST | dead `get_auto_open_agents` + stale provenance docstrings | Debugger | Supervisor |

Summary: 3 MEDIUM + 8 ISSUE/probe + 9 SUGGESTION across the loop, plus 4 spec bugs
(BUG#27–30) caught by the Supervisor's pre-flight before any phase. **No bug reached a
later phase unfixed** — each was caught at its own phase's audit and closed before the
next delegation. The two MEDIUM code bugs (SP1's "You" merge, SP3b's nested-send
clobber) were both caught by the auditor's adversarial probe, not by the happy-path
tests — validating the mandatory-audit rule.

### Bug patterns

| Pattern | Count | Description |
|---------|-------|-------------|
| `partial-test-run` | 6 | a changed/added behavior not covered by any discriminating assert |
| `tautological-assert` | 3 | an assert that passes under the reverted source |
| `misleading-docstring` | 5 | docstring/comment contradicts the code after a change |
| `race-condition` | 3 | nested/deferred same-key send clobbering a slot |
| `type-confusion` | 1 | display-name string used as an identity discriminator |

---

## 5. Process: What Worked

1. **Pre-flight spec verification caught 4 spec defects before any code.**
   BUG#27 (missing R7 fallback in `_resolve_chat_box`), BUG#28 (forward ordering),
   BUG#29 (prose-only branch edit), BUG#30 (test-churn scope). Zero phases wasted on a
   broken contract — contrast with a line where a spec bug surfaces mid-build.
2. **Mandatory adversarial audit on every code-bearing turn.** The auditor found both
   MEDIUM bugs (SP1 "You" merge, SP3b nested-send race) via probes that no happy-path
   test exercised. The 11-section discipline paid off precisely where it matters.
3. **Sub-phasing integration.** SP2 (surface cache respec) split into 2a (source) / 2b
   (tests); SP3 into 3a (mechanism) / 3b (read sites) / 3c (tests); SP4 into a/b/c.
   Each sub-phase had a clean audit point; the display-key migration landed without a
   single cross-domain regression.
4. **Supervisor fixed the small stuff itself.** 9 of the 20 findings were 1–2 line fixes
   with clearly-correct intent (comments, annotations, stale slots) — fixed directly,
   disclosed, without a full builder round-trip. The two MEDIUM bugs got spec amendments
   (rule 4) plus a minimal source fix.

---

## 6. Process: What Didn't Work

1. **Baseline drift in the briefs (recurring, pre-existing).** My phase briefs initially
   asserted "ruff 0" for files that had 9–25 pre-existing findings on HEAD
   (`chat_render_handler` 12, `agent_runtime_handler` 25, `chat_handler` 9, `window` 11,
   `forward_handler` 3). SPEC-10's post-mortem had already flagged this exact class.
   - Lesson: capture the TRUE ruff/pyright/wc baseline with a HEAD-version run before
     writing any brief; state "no NEW findings," never "0."
2. **Test-quality gaps drove six fix rounds.** The builder repeatedly wrote pins that did
   not fail against a revert (tautological/vacuous/unpinned), and the auditor caught each.
   - Lesson: require a per-pin revert-check in the builder delivery (the auditor's
     mutation discipline is the fix, but it costs a round each time it catches one).
3. **A truncation incident in SP3c.** Long `write_file` payloads truncated twice and a
   tail-completion script sliced a test file to EOF, deleting a 605-line section;
   recovered via `git show HEAD:` and detected by the test-count drop. No tests lost
   (auditor confirmed HEAD 231 → work 236 = −2 renamed + 7 added).
   - Lesson: never slice test files to EOF; insert-before-anchor with count asserts.

---

## 7. What the Code Actually Does (End-User Impact)

1. **A project tab is ONE group transcript.** Opening `project:alpha` and sending a
   message fans out to all members; each member's reply renders into the SAME project
   surface, grouped under a named `.agent-box` header (one webview, one scrollbar), not
   one surface (and scrollbar) per agent. Code path:
   `chat_handler.on_send` fan-out (`reply_target=project:<name>`) →
   `agent_runtime_handler._reply_key` → `chat_render_handler._surface_for(display_key)` →
   `chat_surface._document` grouped boxes.
2. **`/ask` and `/delegate` open a private view; everything else is the group chat.**
   Typing `/ask @Coder "..."` opens (or focuses) Coder's own tab and renders the reply
   there; the member's LATER group replies still land in the project surface (the reply
   target is per-send, not a session mark). Code path:
   `chat_handler.on_send` forward_to branch (SP4a) → `send_to_special_agent(reply_target=target)`
   → `_do_response_complete` reads `_reply_key` → the agent-keyed surface.
3. **Launch is quiet, and no agent output is ever dropped.** No agent tabs auto-open at
   boot; the agent-list "Chat" button opens the active project's group tab for members.
   An agent not yet added to the project still renders into the open project surface
   (R7). Code path: `window._on_agent_selected` (R3) + `_resolve_mount_key` active-project
   fallback.

---

## 8. Pre-Existing Issues Flagged (Not Caused by This Implementation)

1. **Pre-existing ruff findings** on all six touched source files (12/25/9/11/3) and the
   test files — verified identical to HEAD via per-file `git show HEAD:` runs. Not fixed
   (out of scope); requirement per phase was "no NEW findings."
2. **`get_auto_open_agents` / `get_project_onboarding_agents` dead-export surface** in
   `agent/special_agents.py` — `get_auto_open_agents` lost its only caller in SP5
   (docstring corrected); removal deferred to a sweep unit (touches a file outside SP5's
   declared scope).
3. **`chat_render_handler.render()` zero-caller legacy shim** — noted in SP2a, untouched.
4. **SyntaxWarning at `tests/test_agent_runtime.py:3763`** (`invalid escape sequence`)
   — pre-existing, untouched.

---

## 9. Evolution Suggestions (Tier 2+)

| Suggestion | Effort | Impact |
|------------|--------|--------|
| Dead-export sweep (`get_auto_open_agents`, `render()` shim, onboarding-helper check) | ~1h | Removes 3 dead surfaces + 2 stale docstrings |
| Builder-side per-pin revert-check in the delivery template | ~2h (prompt) | Cuts the recurring test-quality fix round |
| Fix the 5 pre-existing ruff profiles (RUF013/I001/SIM102 etc.) on the touched files | ~3h | Brings the six files to a clean baseline |
| `mount_key`/`reply_target` divergence note for out-of-scope branches (SP4 audit BUG#3) | ~20m | Documents the echo-mount vs reply-route distinction |

---

## 10. Lessons Learned / Process Rules to Carry Forward

1. **Brief baselines: measure HEAD, never assume 0.**
   - Trigger: writing any build-brief "verification battery."
   - Action: run ruff/pyright/wc on the HEAD version of the target file(s) first; state
     the real number and "no NEW findings."
2. **Every new-behavior pin must fail against a revert.**
   - Trigger: any test asserting a changed/added behavior.
   - Action: builder states, per pin, which source revert makes it RED; supervisor's
     audit confirms independently.
3. **Never tail-slice a test file to EOF.**
   - Trigger: programmatic test-file edits.
   - Action: insert-before-anchor with a count assert + `py_compile` gate; verify the
     test-def count is unchanged (+ additions).
4. **Sub-phase integration, always.**
   - Trigger: any phase touching a key-domain/cache migration or ≥3 edits in one file.
   - Action: split source / wiring / tests into separate audited sub-phases.

---

## 11. Sign-off

- [x] Code committed and pushed to `main` (8 commits: SP1–SP6 + close-out)
- [x] All post-loop verification commands run and pasted (full suite 4469 passed / 3 skipped / 0 failed)
- [x] Captain notified with summary
- [x] Tier 2+ backlog updated (dead-export sweep, per-pin revert-check)