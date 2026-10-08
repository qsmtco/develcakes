# SPEC-19: Live Chat Surface — agent-authored JS with an enforced outbound boundary

**Date:** 2026-10-08
**Author:** Supervisor
**Status:** READY (PM decision recorded 2026-10-08: JS = inline-live **1c**, enforcement = **2a**, scope = **chat transcript only 3a**)
**Amends:** SPEC-06 (R2 "JS off by default" ruling — superseded for the chat document), SPEC-13 (adds a LIVE payload tier)
**Ratifies:** `docs/proposals/WEBKIT-RENDER-SURFACE_PROPOSAL.md` §5 — its two rules become enforced code, not prose
**Reference:** Phosphor (github.com/qsmtco/Phosphor) — `setScreen` script-resurrection pattern, `sanitize_screen_html` passthrough rationale
**Target branch:** main

---

## 1. Decision (PM, 2026-10-08)

Agent-authored JavaScript MAY execute in the chat transcript. The outbound channel is
closed by **enforcement** (compiled content filter + navigation lock), not convention.
Scope: the chat transcript only — the Project Feed stays GTK/Pango.

**Implementation shape:** ONE WebKit document per chat surface (the existing view),
JS **on**, with live agent messages as inline sections of that document. Not one
webview per message — that violates the bounded-memory invariant (SPEC-03/§Patterns).

## 2. Trust model — three tiers (explicit)

| Tier | Content | Path | JS |
|---|---|---|---|
| T1 untrusted | tool results, file contents, fetched text, user text | escape-first markdown → `render_document` (unchanged) | never (script stripped today; stays stripped) |
| T2 agent static | ` ```html ` fence (SPEC-13) | `sanitize_agent_html` (unchanged) | stripped |
| T3 agent LIVE | ` ```live ` fence (NEW) | appended to the transcript DOM **unsanitized-as-to-script**, subject to §3 enforcement | **runs** |

**Binding rule (E4):** T3 payloads contain ONLY agent-authored content. No T1 content
ever enters the DOM through the live path — the existing tier separation is what makes
JS-on survivable; it is not optional.

## 3. Enforcement (the whole security story — mirrors proposal §5)

- **E1 — Compiled content filter:** a `UserContentFilterStore`-compiled filter blocking
  ALL remote loads is attached to the transcript view via `UserContentManager`. Must
  cover subresources AND `fetch()`/XHR raw loads (WebKit content-blocker `raw` trigger).
  **SP1 must prove this with a real-WebKit probe before anything else ships.**
- **E2 — Navigation lock:** navigation policy denies every navigation; the view never
  leaves its initial `about:blank` document.
- **E3 — Script resurrection, Phosphor-faithful:** the append bridge re-creates
  `<script>` nodes (innerHTML does not execute them): never re-create a script with
  `src`; preserve `type`; IIFE-wrap so screens cannot collide with each other or the
  host document.
- **E4 — Tier separation** (§2 binding rule; existing suites already pin T1 inertness).

With E1+E2, injected script is inert: it can redraw the page, it cannot phone home.

## 4. Lifecycle & memory (bounded, SPEC-03 invariant)

- Live sections are capped: at most **10 live sections per transcript**; the oldest is
  **flattened to static HTML** (script nodes removed) when the cap is exceeded.
- Windowed-DOM eviction ALSO flattens a live section (node removal alone does not stop
  timers): eviction path neutralizes scripts before dropping nodes.
- A per-app kill-switch (`DEVELCAKES_LIVE_JS=0` env; default on) renders ` ```live `
  fences as T2 static — the surface degrades, never breaks.

## 5. Phases

| Phase | Deliverable | Gate |
|---|---|---|
| **SP1** | Enforcement core: filter compile+attach, nav lock, real-render test harness (fixes the monkeypatched-loader blind spot in `tests/test_chat_surface.py`) | G1 fetch() from inside a live view FAILS (real WebKit); G4 nav denied; if the `raw`-load block proves unavailable, the unit STOPS and reports — JS stays off |
| **SP2** | The ` ```live ` tier: fence parse → T3 append with script resurrection; flatten-on-cap + flatten-on-eviction; kill-switch | G2 `<script src>` never resurrects; G5 memory flat under 50 live messages (windowed harness); T1/T2 suites unchanged-green |
| **SP3** | CSS property allowlist extension (T2 path): `background`, `background-image`, `filter`, `transform` + animation family — cheap no-JS charts; `url()` stays dead by omission | guard tests updated; no new exfil surface (E1 already covers the document) |
| **SP4** | Action bridge: `window.develcakes.call(method, params)` → Promise, mapped onto existing handler surface; anything consequential routes through the **existing exec-approval card** — the page can never approve itself (proposal §5 rule 2) | G6 bridge call to a consequential method without approval = refused; approve path = same card as `exec_command` |
| Feed (3a) | **excluded** — revisit as its own unit if ever | — |

## 6. Acceptance criteria

- [ ] G1: a page-level `fetch()` inside the transcript document fails (real-WebKit test)
- [ ] G2: `<script src>` never resurrects; inline scripts IIFE-wrapped; `type` preserved
- [ ] G3: T1 content with `<script>` renders inert (existing suites stay green)
- [ ] G4: all navigations denied; view stays on its initial document
- [ ] G5: 50 live messages → bounded live sections (cap 10), flat memory, evicted live sections flattened
- [ ] Kill-switch degrades ` ```live ` to static without error
- [ ] SP4: consequential bridge calls gated by the existing approval card; no self-approval
- [ ] ruff 0 new, pyright 0, full suite green; RED-first + kill-proofs per phase

## 7. Open items (honest)

1. **The E1 probe is load-bearing.** If WebKitGTK 6.0's content filter cannot block
   `raw` fetch/XHR loads, SP1 halts: the design's safety collapses to nav-lock only,
   and JS stays off (kill-switch default flips). No fallback theater.
2. Script teardown on flatten is best-effort (`setInterval` handles captured by removal);
   the cap bounds residual cost. Measure in SP2.
3. `UserContentFilterStore` compile path (JSON ruleset format, store location) has no
   in-repo precedent — SP1 probes it first.

## 8. Documents to update on completion

ARCHITECTURE.md (§Chat surface trust tiers + E1-E4), SPEC-06 status note (R2 superseded
for chat), SPEC-13 (T3 tier cross-ref), the WEBKIT proposal (status: ratified by SPEC-19),
README (live chat bullet), context.md.
