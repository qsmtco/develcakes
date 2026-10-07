# SPEC-14 (System Chrome for Agent Cards) Post-Mortem

**Date:** 2026-10-07
**Supervisor:** Supervisor
**Builder:** Coder
**Auditor:** Debugger
**Commits:** 1 (<close-out commit; SP1+SP2 landed together>)
**Phases:** 3 (SP1 surface chrome → SP2 handler threading + prompts → SP3 audit/close-out)
**Findings:** SP1 audit CLEAN (29-probe style-injection matrix blocked; 15-probe escape matrix clean); 1 SUGGESTION applied (overflow clip) + 1 spec regex amendment (fullmatch); 0 bugs
**PM requirement delivered:** chat header color == agent-list avatar color BY CONSTRUCTION (shared 3-tier source, Tier-3 aligned to #6366f1, registry parity pin)

---

## 1. Code Quality Grade: A (95/100)

| Category | Score | Notes |
|---|---|---|
| Correctness | 19/20 | Gate survived 29 injection probes; kill-proofs 3/3 sha-verified |
| Architecture compliance | 10/10 | Platform-chrome/agent-content split exactly per PM ruling |
| Test coverage | 9/10 | 26 new tests + 6 threading + 5 parity; 2 vacuous tests caught by builder's own RED check and hardened |
| Documentation | 10/10 | Spec amended for fullmatch; resolver docstring corrected; no stale claims |
| Maintainability | 10/10 | One gate fn, one DOM builder, colors frozen at append |
| DX | 9/10 | -1: the torn-import collision (below) cost a re-run |
| **Total** | **95/100** | A |

---

## 2. What's Good

1. **The gate is the whole security story and it is airtight.** `re.fullmatch(r"#[0-9a-fA-F]{6}")` — the auditor's 29-shape matrix (semicolons, braces, quotes, backslashes, unicode digits, trailing newlines, named colors, expressions) returns `""` on every one. The spec originally drafted `^…$`; the BUILDER caught that `$` matches before a trailing newline and enforced the intent with fullmatch. Spec corrected post-build.
2. **Colors frozen at append.** Row dicts carry the gated color; `_document` stays pure (no color-map lookup at render). Auditor verified: mutate a stored row's color → re-render shows the stored value verbatim.
3. **Parity by construction.** Chat resolver and agent-list resolver are the same 3-tier chain ending in `#6366f1`; the parametrized pin over the special-agent registry makes divergence a test failure, not a visual bug someone notices later.
4. **"You" gate.** User echoes resolve no color — their identity stays CSS (`role-user` green), so the platform chrome never paints the user with an agent color.

## 3. What's Bad

1. **The `overflow: hidden` suggestion was auditor-found, not spec'd.** The spec's §2a.4 CSS block didn't consider full-bleed payload backgrounds escaping the rounded corners. Supervisor applied the one-liner + pin directly (no delegation round). Lesson: CSS additions in a spec should state the clipping contract explicitly.

## 4. Bugs Found During Audit

| # | Severity | Description | Disposition |
|---|---|---|---|
| — | — | No bug/issue findings | — |
| S1 | suggestion | `.agent-card` lacked `overflow: hidden` (full-bleed payload bg escapes rounded corners) | Applied + pinned (`test_chrome_css_defaults_present`) |
| A1 | spec-amendment | Spec's `^…$` regex text admitted trailing-newline (builder enforced intent via fullmatch) | Spec §2a.2 corrected |

## 5/6. Process: What Worked / What Didn't

**Worked:** builder's own RED-first discipline caught 2 of its tests as vacuous (negative-only asserts) pre-audit and hardened them with positive preconditions; auditor independently reproduced all kill-proofs sha-verified; the fullmatch deviation was flagged as a spec bug, not silently patched.

**Didn't — the torn-import collision:** supervisor and builder ran full-suite batteries concurrently (builder's landed mid-supervisor's run); 5 tests failed on imports that half-saw the new `agent_color` kwarg. Root cause: no single-owner rule for the 15-minute gate. **Rule now in force: the full pytest battery is SUPERVISOR-ONLY at close-out; builders deliver targeted batteries + kill-proofs. No sleep-polling — report when a command returns.**

## 7. End-User Impact

Every agent's chat card now renders with the platform chrome: avatar initial + name header in the agent's stable color (matching the left-hand agent tab exactly), card frame around their content. Markdown and HTML payloads (SPEC-13) both live inside it. No token cost, no per-agent prompt discipline needed for the frame.

## 8. Pre-Existing Issues (untouched)

- ruff baseline on `chat_render_handler.py` (12 findings) — pre-SPEC-06, byte-stable, Tier-2
- pygments pyright warnings in `syntax_html.py` — pre-existing

## 9. Evolution Suggestions

| Suggestion | Effort | Impact |
|---|---|---|
| Agent-set accent variants (e.g. colored left border on hover) | small | Richer chrome without touching payload space |
| `details`/`summary` theming hooks in `_BASE_CSS` | small | Agents get native toggles that match the chrome |
| Per-agent font-size scaling pref | med | Accessibility |

## 10. Lessons Learned

1. **Regex gates: fullmatch, never `^…$`.** Python's `$` matches before a trailing newline; a "strict" anchor regex can admit `\n`-suffixed payloads. Any future gate regex gets `fullmatch` (or `\A…\Z`).
2. **Spec CSS blocks must state the clipping contract.** Rounded corners + child backgrounds = decide `overflow` in the spec, not in an audit.
3. **One owner per long-running gate.** Concurrent full-suite runs collide (torn imports) and produce phantom failures that cost more than they verify.
4. **Builder-side RED honesty caught its own vacuous tests** — that's the steelFramedCodeWriter loop working; keep the "RED-first + name the mutation" requirement.

## 11. Sign-off

- [x] SP1+SP2 delivered by Coder (targeted batteries + shas)
- [x] Debugger audit CLEAN (29-probe injection matrix, 15-probe escape matrix, kill-proofs sha-verified)
- [x] Auditor suggestion applied + pinned by Supervisor
- [x] PM requirement (header == agent-tab color, all tiers) pinned by registry parity test
- [x] PM visual acceptance: chrome confirmed live in the running app ("I see the new chat boxes") — 2026-10-07, uncommitted-tree instance
- [x] Full-suite close-out battery: 4600 passed / 3 skipped / 0 failed (804.57s xvfb, quiet box, single-owner run)
- [observation] PM reported transient text pixelation in the chat box (Wayland fractional scaling 125% / 3840×2160, WebKitGTK 2.52.6); self-resolved without change — diagnosed as compositor buffer-scale renegotiation (WebKitGTK renders at integer scale under wp_fractional_scale). Plan B on file if persistent: WEBKIT_FORCE_COMPLEX_TEXT=1 + explicit font stack/-webkit-font-smoothing in _BASE_CSS (env-var inventory verified against the installed binary). No code change made.
- [x] PM notified
