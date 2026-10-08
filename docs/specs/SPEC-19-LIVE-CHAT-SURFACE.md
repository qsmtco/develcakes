# SPEC-19: Live Chat Surface — agent-authored JS with an enforced outbound boundary

**Date:** 2026-10-08 (amended same day post-audit — 10 findings folded in; F1 decided)
**Author:** Supervisor
**Status:** READY (PM decision 2026-10-08: JS = inline-live **1c**, enforcement = **2a**, scope = **chat transcript only 3a**)
**Amends:** SPEC-06 (R2 "JS off by default" — superseded for the chat document), SPEC-13 (adds a LIVE payload tier)
**Ratifies:** `docs/proposals/WEBKIT-RENDER-SURFACE_PROPOSAL.md` §5 — its two rules become enforced code, not prose
**Reference:** Phosphor (github.com/qsmtco/Phosphor) — `setScreen` script-resurrection pattern, `sanitize_screen_html` passthrough rationale
**Target branch:** main

---

## 1. Decision (PM, 2026-10-08)

Agent-authored JavaScript MAY execute in the chat transcript. The outbound channel is
closed by **enforcement** (compiled content filter + navigation lock), not convention.
Scope: the chat transcript only — the Project Feed stays GTK/Pango.

**Implementation shape:** ONE WebKit document per chat surface, JS **on**. Live agent
messages are inline sections of that document. Not one webview per message (bounded-memory
invariant, SPEC-03).

## 1a. THE F1 DECISION — appends become incremental DOM injection (decided)

The current surface rebuilds the whole document on every append (`chat_surface.py:8`;
SPEC-17:39). That is incompatible with live sections: state resets on every message, and a
literal "deny every navigation" E2 would block `load_html` appends entirely.

**Decision:** with JS on, appends migrate to **incremental DOM injection** via
`run_javascript` (this was SPEC-06's original §2/§6 design, deferred only because JS was
off — the deferral note is in `chat_surface.py:3-5`). Full-document `_load_html` rebuild
survives ONLY as the **compaction path** (windowed-DOM eviction — rare, bounded), which
doubles as the flatten point for evicted live sections.

**Cross-spec impact (must be flagged, not hidden):** SPEC-17's follow/settle scroll model
is built on full rebuilds. Incremental append needs its own scroll path — appended content
below the fold naturally preserves reading position; at-bottom follow must be re-derived
for the injection path. SP1 proves scroll behavior under injection; **if the follow
invariant cannot be preserved, the unit HALTS and reports to the PM with evidence —
"re-launched live" (re-run semantics) is a PM decision, not a silent fallback.**

## 2. Trust model — three tiers (explicit)

| Tier | Content | Path | JS |
|---|---|---|---|
| T1 untrusted | tool results, file contents, fetched text, user text | escape-first markdown → `render_document` (unchanged) | never |
| T2 agent static | ` ```html ` fence (SPEC-13) | `sanitize_agent_html` (unchanged) | stripped |
| T3 agent LIVE | ` ```live ` fence (NEW) | **RAW APPEND — bypasses nh3 entirely** (F3) | **runs** |

**T3 permission set (stated, F3):** raw append. This grants the FULL CSS surface —
`<style>` blocks, `@keyframes`, gradients, `data:` URIs — with no sanitizer in the path.
That is deliberate: E1+E2 (§3) are the boundary, and T3 gets the complete expressive
surface Phosphor screens have. The cost: T3 relies entirely on enforcement, not on
tag stripping. (nh3 cannot be configured to pass `<style>` — ammonia panics
uncatchably when "style" is in tags=; documented probe verdict in `render/sanitize.py:127`.)

**E4 restated honestly (F5):** nothing mechanically prevents the agent from pasting
T1-shaped text into a ` ```live ` fence — it authors the payload. Tier separation is a
**spoofing-surface reducer** (a live script can redraw the page to mislead the reader),
NOT the exfil story. The exfil story is E1+E2, full stop.

## 3. Enforcement (the whole security story — mirrors proposal §5)

- **E1 — Compiled content filter:** `UserContentFilterStore`-compiled filter blocking
  ALL remote loads, attached via `UserContentManager`. **Probe matrix (F4), all in G1:**
  (a) subresource loads (img/script/css); (b) `fetch()`; (c) `XMLHttpRequest`;
  (d) `WebSocket`; (e) `EventSource`; (f) `navigator.sendBeacon`. Mechanism note:
  resource-type `raw` covers "untyped loads like XHR"; WebSocket/EventSource/sendBeacon
  are covered only by a blanket `url-filter: ".*"` — which must be proven to (g) NOT block
  the surface's own initial `about:blank` load and `run_javascript` injection.
  **Any probe failure → unit STOPS, JS stays off. No fallback theater.**
- **E2 — Navigation lock, precisely (F1):** deny every navigation EXCEPT app-initiated
  loads of the surface's own initial document (`load_html` with `about:blank`, navigation
  type OTHER from the app). No link, redirect, or page-initiated navigation ever lands.
- **E3 — Script resurrection, Phosphor-faithful:** re-create `<script>` nodes from T3
  payloads (innerHTML does not execute them): never re-create a script with `src`;
  preserve `type`; IIFE-wrap.
- **E4 — tier separation = spoofing reducer (see §2).**

With E1+E2, injected script is inert: it can redraw the page, it cannot phone home.

## 4. Lifecycle & memory (bounded, SPEC-03 invariant)

- Live sections capped at **10 per transcript**; oldest is **flattened** on overflow.
- **Flatten is neutralization, not just node removal (F8):** remove script nodes AND strip
  `on*` handler attributes; otherwise handlers still fire and throw.
- **Timers are wrapped, not "captured" (F9):** the append bridge shims
  `setTimeout/setInterval/requestAnimationFrame` per section and records handles; flatten
  clears them. (Node removal captures nothing — audit F9.)
- Windowed-DOM eviction (the compaction rebuild) flattens live sections before dropping
  their nodes.
- Kill-switch `DEVELCAKES_LIVE_JS=0` (default on) degrades ` ```live ` to T2 static.
- **Live height mutation (F10):** a live section that animates its height interacts with
  the follow-scroll logic — SP2 test row required (follow must not thrash).

## 5. Phases

| Phase | Deliverable | Gate |
|---|---|---|
| **SP1** | Enforcement core + injection decision: filter compile/attach with the FULL F4 probe matrix; nav lock per E2; **incremental-injection prototype** proving (i) appends without rebuild, (ii) scroll follow/reading-position preserved, (iii) live state survives an append; real-render test harness (fixes the monkeypatched-loader blind spot) | G1 all eight probe rows green (fetch/XHR/WS/ES/beacon blocked; about:blank + injection NOT blocked); scroll invariant held or HALT→PM |
| **SP2** | The ` ```live ` tier end-to-end: fence parse → raw append with E3 resurrection; timer shims; flatten (scripts + on* + timers); cap; eviction compaction; kill-switch; F10 follow test | G2 `<script src>` never resurrects; G5 50 live messages → ≤10 live, flat memory, flattened sections inert (no on* fires, no timers tick); T1/T2 suites unchanged |
| **SP3** | **T2-only** CSS property allowlist extension, scoped to inline-capable properties (F7): `background`, `background-image`, `filter`, `transform`, `transition` (+ their longhands). **`animation-*`/`@keyframes` are T3-only** — inert in T2 without `<style>`, which nh3 can never pass. `url()` stays dead by omission. | guard tests updated; no new exfil surface |
| **SP4** | Action bridge, **two-phase (F6)**: `window.develcakes.call(method, params)` returns `{status: "pending", id}` immediately; resolution arrives as a DOM event later — approvals take minutes, a 30s Promise timeout would eat them. Consequential methods route through the **existing exec-approval card** (`feed_card.py` needs_approval → `agent_runtime_handler.approve_exec`); the page learns the outcome via the bridge event, never self-approves | G6 consequential call without approval = refused; approval path = the same card `exec_command` uses |
| Feed (3a) | **excluded** — its own future unit if ever | — |

## 6. Acceptance criteria

- [ ] G1: E1 probe matrix — fetch/XHR/WebSocket/EventSource/sendBeacon/subresources ALL blocked (real-WebKit); initial load + injection NOT blocked
- [ ] G1b: incremental append preserves live state across appends; scroll follow + reading position held (or HALT reported to PM)
- [ ] G2: `<script src>` never resurrects; inline scripts IIFE-wrapped; `type` preserved
- [ ] G3: T1 content with `<script>` renders inert (existing suites green)
- [ ] G4: navigations denied except app-initiated initial load
- [ ] G5: 50 live messages → ≤10 live sections, flat memory; flattened = no scripts, no on* handlers, no timers
- [ ] F10: live height animation does not thrash follow-scroll
- [ ] Kill-switch degrades ` ```live ` to static without error
- [ ] SP4: two-phase bridge; consequential calls gated by the existing approval card; no self-approval
- [ ] ruff 0 new, pyright 0, full suite green; RED-first + kill-proofs per phase

## 7. Open items (honest)

1. The E1 probe matrix is load-bearing (see §3). Halt discipline applies.
2. SP1's scroll-under-injection work touches SPEC-17 territory — coordinate, don't
   silently fork. If SPEC-17's model can't extend, that's part of the HALT report.
3. Timer-shim completeness (worker threads? `requestIdleCallback`?) — enumerate in SP2;
   the cap bounds residual cost either way.

## 8. Documents to update on completion (F2 — including the prompts)

ARCHITECTURE.md (§Chat surface trust tiers, E1–E4, injection model), SPEC-06 status note
(R2 superseded for chat), SPEC-13 (T3 tier cross-ref), **`prompts/system/coder.md`,
`prompts/system/debugger.md`, `prompts/system/supervisor.md` — a "Live sections
(SPEC-19)" section mirroring the SPEC-13 HTML-protocol blocks: when to emit ` ```live `,
inline-only scripts, IIFE rule, no external resources — otherwise the feature ships
invisible and T3 is dead code**, the WEBKIT proposal (status: ratified by SPEC-19),
README (live chat bullet), context.md.
