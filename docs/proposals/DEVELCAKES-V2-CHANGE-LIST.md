# DevelCakes — v2 Change List

**Date:** 2026-09-17
**Author:** Lt. Qrusher (per PM direction)
**Status:** Superseded 2026-10-21 by `PROPOSAL-META.md` Program 5. Only §1.3 and §1.4 remain scheduled. The rest of this list has shipped or was dropped. Do not treat the status line below the revisions as current.
**Revisions:** 2026-09-17 (Supervisor) — additions and corrections marked **[SUP-REV]** inline:
R1 blast-radius fix (local send path already exists; 11 rewiring sites enumerated); decision #1 gated
ahead of R1; R2 staged to chat-surface-first; removals folded into §2 ordering; stop-all + per-coder
worktrees promoted to gates; P11-before-tag stated; transcript store refined (lost-update framing,
SQLite+WAL leading); STT payload cap + Azure retention; improve.py redirect-handler dedupe.
**Source repo:** `/home/q/projects/crabcakes` (HEAD at time of writing)
**Successor:** DevelCakes — a hard fork, own repository, v1 preserved

> Consolidates a working session: the PM's change list, the code-grounded commentary on each
> item, prerequisites, and suggested additions. This is the input for a spec, not a spec.

---

## 0. Fork mechanics (do this first)

| Step | Action |
|---|---|
| 1 | `git tag -a v1.0 -m "Crabcakes v1" && git push origin v1.0`, then cut a GitHub **Release** — freezes v1 immutably |
| 2 | Create the new repo (`repo slug: develcakes`, display name "DevelCakes") |
| 3 | `git checkout -b v2 v1.0 && git remote add devl git@github.com:qsmtco/develcakes.git && git push devl v2:main` |
| 4 | Archive the Crabcakes repo once DevelCakes deploys (read-only, permanent) |

**[SUP-REV] P11 gates the tag.** The memory-ratchet work (`SPEC-MEMORY-WIDGET-RATCHET` — P11 still
pending at time of writing) must land **before** step 1. Otherwise the v1.0 tag freezes the leak
(+4.5 MB/min, ~56 KB per retained card widget) into the immutable baseline, and "v1 preserved" means
"v1 preserved with the leak." Sequence: P11 merged → tag → fork.

**Name — decided: `develcakes`.** `devel` is the established abbreviation for development
(Debian/RPM `-devel` packages, FreeBSD `devel/`, Gentoo `dev-*`). `devl` drops a vowel and reads
as a typo of "devil" or "dev". The `-cakes` suffix signals lineage without implying the same tool.

**What does NOT carry over to a new repo:** GitHub Actions secrets, branch protection, webhooks,
deploy keys, environments, issues/PRs/discussions, stars. PAT allowlist may 403 until the new repo
is added (prior precedent: API push ≠ git push).

**Rename footprint — the name is in runtime paths, not just git.** These must diverge at the fork
point or v1 and v2 will fight over the same files:

- `com.crabcakes.app` — GTK application id + desktop entry
- `~/.config/crabcakes/` — conversations, audit log, agent YAMLs
- `<repo>/.crabcakes/` — feed, tasks, prompts, secrets
- Package/manifest names, CI references, docs, agent skills/memory referencing the old path

**[SUP-REV] Config migration is a step, not a footnote.** `~/.config/crabcakes/` holds conversations,
the audit log, agent YAMLs, and the OpenRouter credential store. The fork needs a one-time migration
(copy → new path, with a first-run banner saying what moved), or DevelCakes launches as a factory
reset for the one user who already matters.

**[SUP-REV] Check what's already in history.** If `.crabcakes/tmp/uiresp2-audit/` (full tree copy) was
ever committed, deleting it from the working tree still leaves it inside the v1.0 tag and the pushed v2
history. Decide before `git push devl v2:main`: accept the bloat, or filter it out of v2's history —
it cannot be removed from v1's tag once cut.

---

## 1. The change list

### 1.1 Multi-coder / multi-debugger (parallelism)

**Already documented as blocked.** `agent/special_agents.py` states it directly: session keys
default to `special:<role>`, so same-role agents **collide and one replaces the other** — and
`role` selects the prompt template (`agent/context.py` `build_system_prompt`), so role cannot be
changed to dodge it. The documented fix is an explicit `session_key:` per agent YAML.

Deltas:

- Roster (`team.json` + per-agent YAML): distinct `session_key`s (`special:coder-1`, `coder-2`, …);
  roles unchanged so prompts still resolve.
- **Per-agent turn concurrency** in the runtime. Today a single "turn in flight" gate exists (the
  CLI nudge refuses on it). N agents needs a scheduler plus per-session locks.
- **Work claiming** — a claim/lease on work units, or N coders collide on the same spec.
- **N× the memory ratchet.** The widget-ratchet work (`SPEC-MEMORY-WIDGET-RATCHET`) becomes a
  prerequisite, not a nice-to-have: measured +4.5 MB/min and ~56 KB per retained card widget,
  never returned. N agents multiplies it.
- **N× feed volume** — already 15k cards with one team; retention/filtering becomes mandatory.
- Adjudication must attribute findings to a source agent, or re-litigation risk multiplies.

### 1.2 HTML chat surface

- Not a new idea: `docs/proposals/WEBKIT-RENDER-SURFACE_PROPOSAL.md` ("the agent speaks HTML")
  already exists. **Adopt or amend it — do not re-invent.**
- Replaces/extends `ui/views/chat_bubble.py` + `ui/handlers/chat_render_handler.py`; the GTK feed
  can remain.
- **Injection becomes a new risk class** — HTML from agent output needs sanitization and
  JS-off-by-default before it ships.
- Prereq: the ratchet fix (the proposal explicitly supersedes feed virtualization).
- **[SUP-REV] Stage it — chat surface first.** "The GTK feed can remain" needs a number attached:
  Phase A converts only the chat transcript surface — `chat_bubble.py` (33 sites) +
  `chat_render_handler.py` (10) + `markdown.py` (Pango path → HTML) + `escaping.py` (gains an
  HTML-sanitize path; Pango escapes remain for untouched surfaces) — roughly 90 of the 242 sites.
  That also localizes the sanitization rebuild to one surface. Feed cards, file tree, session menu,
  toolbar, and diff cards stay Pango until Phase A proves out. Phase B — whether HTML subsumes the
  rest — is open decision #5, decided on evidence instead of a bet placed before any code exists
  (see R2).

### 1.3 JEV routing → group chat

- Today routing is caller-decided: `ui/handlers/collab_handler.py` owns `ask` / `delegate` /
  `stop` / `tell` as pure pass-through; `ChatHandler` performs the routing. JEV turns that into a
  **Choice over the roster** (255 options — ample), with a confidence threshold and a fallback.
- **Group chat is the largest structural change in this list.** Conversations are per-session
  files today (`special:<agent>.json`). A shared transcript with per-agent attribution is a new
  data model, not a tweak.
- Needs: addressing/@mention semantics, an HTTP client for `/api/alpha/decisions`, a cost/latency
  budget, and **fail-open-to-human** on 429/529 **[SUP-REV] and on timeout (2–3 s)** — the decisions
  endpoint is alpha with no SLA, and at group-chat volume it becomes the hottest call in the system;
  a hung request must degrade exactly like a refused one.

### 1.4 JEV completion detection (is a conversation between two agents done?)

- Feasible, but requires a **definition of "complete" per conversation type** — coder↔debugger
  audit loop, coder↔supervisor sendback — or the detector has no ground truth.
- Wiring: on complete → close the loop, write the audit row, notify Supervisor/PM.
- **Advisory only.** A false "complete" ends an audit loop early, which is the most expensive
  failure available. Sort, don't decide.

### 1.5 Telegram remote-in to the Supervisor

- `gateway/client.py` already exists — but it is a **WebSocket client for the OpenClaw gateway**
  with Ed25519 device auth. Decision required: reuse that transport, or build a first-class
  Telegram bot (Hermes already runs one that can be mirrored).
- **Model the contract on the CLI nudge**: strict argument shape, defined exit codes, delivery
  verified by audit row + feed card (hash-matched).
- **Security is the gating item** — this is remote command execution into a system that runs shell
  commands. Single-user allowlist, no free-form relay, kill switch, audited identically.

---

## 2. Prerequisites and ordering

Dependencies, not phases:

```
P11 (ratchet)  →  v1.0 tag + fork/rename  →  R5 Auxilium removal (independent — no dependency on any decision)
→  decision #1 (Telegram transport) → R1 gateway removal
→  R2 Phase A: chat-surface HTML (+ sanitizer rebuilt in the same change)
→  R4 feedbar removal (activity state surfaces in the HTML chat)
→  transcript store (decision #2, SQLite/WAL) + feed retention
→  stop-all + per-coder worktrees + work claiming
→  multi-agent roster + per-agent concurrency
→  group chat data model  →  JEV routing  →  JEV completion  →  Telegram
```

Rationale: multiplying agents before fixing the ratchet multiplies the leak; group chat before
feed retention makes the primary UI unusable; JEV routing before the roster exists has nothing to
route over.

**[SUP-REV] Amendments folded into the chain above (each was previously implicit or missing):**

- **P11 before the tag** — the tag is immutable; the leak must not be frozen into it (§0).
- **Decision #1 (Telegram transport) before R1 completes** — if Telegram reuses the OpenClaw
  gateway client, R1's "complete removal" deletes the transport you intend to keep. Decide the
  transport first, then R1's delete-list is deterministic and R1 never gets done twice.
- **R5 before R1** — R5 deletes `ui/views/auxilium_wizard.py` and `ui/handlers/auxilium_wizard_handler.py`
  wholesale, which are two of R1's residual-OpenClaw-reference files, and deletes `test_kb_lookup.py`,
  which R1 lists as needing partial edits. Doing R1 first means editing files that are about to vanish.
  R5 is also the one removal with no upstream dependency — it can go first after the fork.
- **R2 before R4** — feedbar's activity machine (streaming label, 250 ms ticker, `_live_update`
  bucket) needs somewhere to surface before the widget dies; the HTML chat surface is that place.
  Removals are not parallel work — R1–R4 each ride the migration that obsoletes them. **R5 is the
  exception: it rides nothing, which is why it can go first.**
- **Stop-all + worktrees + work claiming before roster expansion** — previously §3 suggestions;
  now gates. N autonomous writers with only per-command `/stop` and one shared working tree is
  one runaway loop away from a wrecked repo. These are safety prerequisites for multi-agent,
  not nice-to-haves (see §3.1 #3, §3.3 #9).
- **Transcript store before group chat** — decision #2 was unsequenced; group chat is a new data
  model that must not be built on per-session whole-file rewrites (see §3.2 #5).

---

## 3. Suggested additions

### 3.1 Do these three — cheap now, expensive later

1. **Log every JEV verdict** — decision, confidence, input hash, timestamp, and what the system did
   with it. Costs fractions of a cent; without it thresholds cannot be tuned and automated routing
   decisions cannot be explained later. With it, you have a dataset.
2. **Per-agent cost metering** — N coders × N debuggers × LLM × JEV loses the thread of spend
   within a day. Per-agent counters in the UI plus a daily rollup.
3. **A `git worktree` per coder** — multiple agents in one working tree will clobber each other.
   One worktree per agent; merges through a controlled gate. **[SUP-REV] Promoted to a §2 gate** —
   multi-agent cannot ship without it, so it is no longer optional.

### 3.2 Decide before building

4. **Agent attribution in git** — commit trailers (`Agent: coder-2`) plus feed card origin. Manual
   attribution is already expensive (the change-order exercise); with N agents it is untenable.
5. **Transcript durability** — `agent/persistence.py` uses `open(path,"w")` + `json.dump`
   (non-atomic; readers already tolerate torn files). A shared transcript makes that a daily event.
   Move to append-only JSONL or SQLite first. **[SUP-REV] The sharper problem is lost updates, not
   torn reads.** Whole-file rewrite means last-writer-wins: two concurrent writers to a shared
   transcript silently drop entire turns — worse than the torn-file tolerance readers already have.
   SQLite with WAL mode is the leading option (concurrent readers + single-writer serialization for
   free, no lock broker to invent); JSONL-append needs a lock coordinator you would have to build
   and test. **[SUP-REV] Sequenced in §2 and on the critical path** — decision #2 gates group chat;
   put a deadline on it.
6. **Feed retention + per-agent filtering before group chat** — 15k cards with 3 agents and card
   updates already being dropped. The feed is the product.
7. **A fallback router** — `/api/alpha/decisions` is alpha and versioned by date; JEV is now in the
   routing path. Keep a dumb fallback (round-robin/keyword) and the thresholds in a versioned
   config file, so degradation is graceful.

### 3.3 Nice to have

8. **A replay harness** — re-run a recorded transcript against a new prompt/roster/model and diff
   the outcome. The only way to tune prompts and JEV thresholds without shipping blind.
9. **A stop-all** — one action halting every agent. Per-command `stop` exists; N autonomous
   writers on a live repo need a big red button. **[SUP-REV] Promoted to a §2 gate** ahead of
   roster expansion — it is a safety prerequisite, not a convenience. Note `runtime.cancel()` and
   the review layer's own commit hooks both matter here; stop-all must cover agents mid-tool-call
   and any in-flight turn, not just idle ones.
10. **One-writer-per-file as a stated invariant** (alongside "one auditor per artifact").
    Multi-agent systems fail on resource contention, not intelligence.
11. **Telegram as a digest console, not a firehose** — route only JEV-triaged NEEDS-PM items, with
    the CLI-nudge-style delivery contract. A raw feed mirror gets muted within a week.

**Caution:** group chat is a new artifact class and artifacts accumulate. Set a transcript
retention policy on day one, or DevelCakes inherits Crabcakes' doc sprawl plus a chat log.

---

## 4. Open decisions

| # | Decision | Notes |
|---|---|---|
| 1 | Telegram transport: reuse OpenClaw gateway client vs. first-class bot | `gateway/client.py` exists but targets OpenClaw + Ed25519 device auth. **[SUP-REV] This decision gates R1's final shape — make it before gateway removal starts (§2), or R1's delete-list is provisional** |
| 2 | Group-chat transcript model: shared log w/ attribution vs. per-session + index | Determines the migration cost. **[SUP-REV] On the critical path (gates group chat → gates JEV routing); leading candidate SQLite+WAL (§3.2 #5). Deadline it** |
| 3 | "Complete" definitions per conversation type | Required before JEV completion detection is meaningful |
| 4 | JEV confidence thresholds + what happens below them | Policy, not code — belongs in versioned config |
| 5 | Does v2 keep the GTK feed, or does the HTML surface subsume it? | Affects how much of `ui/views/` survives. **[SUP-REV] Reframed by R2's staging: decide after chat-surface Phase A ships, on evidence** |
| 6 | Fresh-install onboarding after R5 deletes Auxilium and `local-kb` | `agent/config.py` defaults a new install to `local-kb/local-kb` and the wizard is the "no provider configured" catcher — removing both leaves a new install with nothing. Pick: first-run Settings → Providers prompt / a real default provider / accept an inert install. **Also decides whether `knowledge/` survives as plain docs** (R5) |

---

## 5. Removals (PM directive)

Five things go. Counts below are from the live tree, excluding the `.crabcakes/tmp/uiresp2-audit/`
snapshot (a full copy of the tree that will mirror every removal and pollute every grep — delete it).

**R1–R4 remove dead or superseded surface. R5 is different in kind:** it
deletes a subsystem that currently works and ships — the only removal here that takes a feature away
from the user rather than replacing it. Treat its three open questions (§4 #6, and the two notes in
R5) as decisions, not cleanup.

### R1. OpenClaw WebSocket / gateway client — **complete removal**

Delete the transport and everything that exists only to serve it:

| Target | Why |
|---|---|
| `gateway/client.py`, `gateway/__init__.py` | The WebSocket client itself (OpenClaw v3 device-auth protocol, Ed25519) — delete the package |
| `ui/handlers/gateway_handler.py` | Owns `GatewayClient` + `AgentManager` + connection lifecycle |
| `ui/handlers/connection_sync_handler.py` | Owns the post-connect wiring that injects live `GatewayClient`/`AgentManager` into dependent handlers — the indirection dies with it |
| `ui/handlers/chat_handler.py` | `_gw`, `set_gateway_client()`, `gateway_client` ctor param; sending currently goes through `self._gw.send_message()` — needs a local send path |
| `ui/handlers/review_handler.py` | Holds a gateway client for sending messages to agents |
| `ui/window.py` | GatewayHandler wiring, `set_gateway_client(None)`, the lambda-to-avoid-stale-None shim |
| `utils/config.py` | `get_gateway_url()`, `get_identity_dir()` (OpenClaw Ed25519 identity) |
| `models/activity.py` | Activity types are documented as "the exhaustive list from the gateway event catalog" — redefine locally |
| `agent/runtime.py` | `app_title` documented as flowing from gateway `displayName` |
| `agent/tools.py`, `ui/views/agent_builder.py`, `ui/views/auxilium_wizard.py`, `ui/handlers/auxilium_wizard_handler.py`, `ui/views/left_panel.py`, `utils/project_awareness.py`, `utils/prompt_loader.py` | Residual OpenClaw references |
| `tests/test_gateway.py`, `test_gateway_handler.py`, `test_connection_sync_handler.py`, `test_low345_gateway_hardening.py` | Retire with the code; `test_config.py`, `test_tools.py`, `test_kb_lookup.py` need partial edits |

**[SUP-REV] The survivor list is longer than the table, and the work is smaller than it reads.**
The table lists what *dies*; what needs *rewiring* is a different set. Eleven `send_message` call
sites route through `_gw` today and must move to a local send path:

| File | Sites | Note |
|---|---|---|
| `ui/handlers/chat_handler.py` | 8 (`:95`, `:243`, `:272`, `:383`, `:418`, `:467`, `:496`, `:499`) | routing + forward dispatch |
| `ui/handlers/review_handler.py` | 1 (`:573`) | sends to group members |
| `ui/handlers/forward_handler.py` | 1 (`:168`) | forward routing |
| `ui/handlers/agent_command_handler.py` | 1 (`:503`) | agent-issued commands |

The good news the original missed: **the local send path already exists.** `agent/runtime.py:948`
`send_message(session_key, text, prepare=...)` is the runtime's own turn engine, and
`agent_runtime_handler.py:1039` already demonstrates the exact rewiring pattern. R1's core work is
not building a transport — it is repointing ~13 call sites across 6 handler files at the path that
already runs local turns. That is a materially smaller, lower-risk change than "complete removal"
suggests. `command_handler.py` also takes `gateway_client` as a constructor arg (line 52) and
documents `send_message` in its header — that dependency moves with the rest.

**RETAIN — the Connect button.** `ui/toolbar.py` keeps `self._connect_btn` (label "Connect"), its tooltip,
the connection status label, and the state machine that renders `● Connecting` / `● Connected`. It is
**repurposed as the Telegram on/off control** — relabel and rewire the action to the Telegram transport
(§1.5). Do not delete the widget or its states; only the thing it talks to changes.

### R2. Pango markup → HTML

242 markup call sites across ~20 source files. Densest:

| File | Sites |
|---|---|
| `ui/views/chat_bubble.py` | 33 |
| `utils/escaping.py` | 32 |
| `ui/views/feed_card.py` | 22 |
| `utils/markdown.py` | 14 |
| `utils/gtk_safe_link.py` | 14 |
| `ui/views/main_content.py` | 11 |
| `ui/handlers/chat_render_handler.py` | 10 |
| `ui/views/session_menu.py`, `file_tree.py`, `chat_input_toolbar.py` | 6 each |
| `utils/syntax_highlight.py`, `ui/views/feedbar.py`, `ui/toolbar.py` | 5 each |
| `ui/views/diff_card.py` | 3 |

This is not a find-and-replace — it replaces the **back half of the render pipeline**:
`markdown → escaping → Pango markup → GtkLabel.set_markup` becomes `markdown → sanitize → HTML → WebKit`.
`utils/markdown.py`, `utils/escaping.py`, `utils/gtk_safe_link.py` and `utils/syntax_highlight.py` are
either replaced or reduced to sanitizers. Tests to retire/replace: `test_pango_guard_sites.py` (47
sites), `test_escaping.py`, `test_markdown.py`, `test_gtk_safe_link.py`, `test_chat_render_handler.py`,
`test_diff_viewer.py`, `test_main_content_settings_bar.py`, `test_uirsp3_phase2.py`.

**Risk, stated plainly:** `test_pango_guard_sites.py` exists because markup injection was a real bug
class here. Swapping to HTML **moves** that risk, it does not remove it — the equivalent guard
(HTML sanitization, JS off by default) must be rebuilt in the same change, not after.

**[SUP-REV] R2 is staged, not all-at-once.** The 242-site figure is the full catalog; the conversion
is sequenced by §1.2's staging. Phase A (chat surface: `chat_bubble.py`, `chat_render_handler.py`,
`markdown.py`, `escaping.py`) covers ~90 sites and localizes the sanitization rebuild to one surface.
`test_pango_guard_sites.py`'s 47 guarded sites retire only as their surfaces convert — the guard
tests for still-Pango surfaces must keep passing throughout Phase A, or the migration is deleting
protection ahead of the replacement. Phase B (the remaining ~150 sites — feed cards, file tree,
session menu, toolbar, diff cards, feedbar until R4) is open decision #5, decided on Phase A evidence.

### R3. Slash commands — remove most

Current surface:

- `ui/handlers/collab_handler.py` — `/ask`, `/delegate`, `/stop`, `/tell` (all pure pass-through; routing
  is done by `ChatHandler`)
- `ui/handlers/command_handler.py` — dispatch
- `ui/handlers/agent_command_handler.py` — agent-issued commands (max 3 per response)
- `models/command.py` — `Command` / `CommandResult`

`/nudge` is a **CLI argument, not a slash command** — it stays (it is the verified relay contract).

Decision to make: with a group chat, `@coder-2 do X` in natural language makes `/ask`, `/delegate` and
`/tell` redundant — the JEV router (§1.3) replaces their addressing role. **`/stop` should survive**
as the safety path (and pairs with the stop-all in §3.3). Removing the four collapses `collab_handler.py`
plus much of `test_command_handler.py` and `test_agent_command_handler.py`.

### R4. feedbar — remove

- `ui/views/feedbar.py` — the Response Status bar (`set_status_text` → `set_markup`,
  `set_progress_fraction`, `set_progress_hidden`)
- `ui/handlers/activity_handler.py` — the 6-state activity machine takes feedbar as a constructor
  dependency and calls `_update_feedbar()` throughout; its rendering half goes with the widget
- `ui/window.py` — `from ui.views.feedbar import FeedBar`, `self._response_status = FeedBar()`, and the
  `feedbar=` construction argument

**Not purely subtractive.** The activity state machine also drives the streaming label, the 250 ms ticker
and the `_live_update` elapsed bucket — the exact surfaces `SPEC-MEMORY-WIDGET-RATCHET` touches. Removing
feedbar means deciding where activity state surfaces instead (presumably the HTML surface). Decide that
before deleting, or the ticker is left writing to a widget that no longer exists.

### R5. Auxilium (helper agent) + its KB stack — **complete removal**

Auxilium is the always-on "helper" agent (`prompts/default_agents/auxilium.yaml`, role `helper`). It is
not an LLM agent — it is a **RAG stack wearing an agent costume**, and that is why removing it touches
more files than any other item in this section.

**What it actually is:**

- `agent/kb_lookup.py` — embeds the question, cosine search over `knowledge/*.md`
- `agent/kb_server.py` — a localhost OpenAI-compatible HTTP server (**127.0.0.1:18790**) that impersonates
  a provider named **`local-kb`** so the runtime "needs zero changes"
- An optional synthesis layer that POSTs the question to a **third-party, unauthenticated** worker
  endpoint (`devtoolbox-api.devtoolbox-api.workers.dev/ai/generate`, 3.0 s timeout) to phrase the answer
- The **model**: `BAAI/bge-small-en-v1.5` (~130 MB, 384-dim) via `sentence-transformers`, which pulls in
  PyTorch (~700 MB installed)
- A first-run **wizard** that configures a provider on a fresh install

**Delete — pure Auxilium (~2,300 lines + assets):**

| Target | Lines |
|---|---|
| `agent/kb_lookup.py` | 273 |
| `agent/kb_server.py` | 457 |
| `scripts/rebuild_kb_index.py` | 246 |
| `ui/handlers/auxilium_wizard_handler.py` | 452 |
| `ui/views/auxilium_wizard.py` | 452 |
| `prompts/system/auxilium.md` | 95 |
| `prompts/default_agents/auxilium.yaml` | 23 |
| `tests/test_auxilium_tier1.py`, `test_auxilium_tier2.py`, `test_kb_server.py`, `test_kb_lookup.py`, `test_kb_integration.py`, `test_kb_provider_registration.py` | 2,288 |
| `knowledge/.index/` (chunks.json + embeddings.npy) | — |

Also drops: the `sentence-transformers` + numpy + PyTorch dependency, the ~130 MB model download, and
`knowledge/*.md` (672 KB) as a *retrieval corpus*.

**Rewire — surgical edits, ~19 files (not deletions):**

| Target | What |
|---|---|
| `agent/runtime.py` | `KB_OUT_OF_SCOPE` sentinel import (:61-65), synthesis helper (:317), `_inject_kb_context` (:1037), `_prepare_kb_synthesis` (:1178-1233), the per-turn cache (:1354-1355), tool-loop hook (:1471-1476), and the `KB_OUT_OF_SCOPE` → fallback retry (:1622-1660) |
| `agent/config.py` | `default_provider` / `default_model` defaults (:239-240) and `_create_default_config` (:253-272) — **see decision below** |
| `utils/providers_store.py` | `ensure_kb_provider()` (:350-398), `_ensure_auxilium_uses_kb()` (:401-446), and the startup call site |
| `utils/agent_defs.py` | helper-role exemption (:223-240), `local-kb` in the valid-id set (:440-442) |
| `utils/prompt_loader.py` | `helper` → `auxilium.md` branch (:165, :174, :245-246) |
| `ui/window.py` | auto-open tab wiring + wizard hooks (:195-250), wizard-complete handler (:1083-1109) |
| `ui/handlers/agent_runtime_handler.py` | KB server start (:190-193), key-check skip for `local-kb` (:927-928), the "helper needs no active project" exemption (:968-969), KB server stop (:1169-1171) |
| `ui/handlers/project_handler.py` | `special:auxilium` mapping (:374) |
| `ui/views/agent_builder.py` | `local-kb` exclusion in the fallback dropdown (:389-395) |
| `models/conversation.py` | `agent_role == "helper"` comment (:148) |
| `agent/__init__.py` | `kb_lookup` / `is_index_available` references (:10, :18, :32-34, :54, :73) |
| `knowledge/*.md`, `README.md` | install/troubleshooting steps for the KB index and its embedding model |

**Tests needing edits, not deletion (~14):** `test_agent_defs`, `test_special_agents`,
`test_settings_dialog`, `test_settings_handler`, `test_agent_builder_dialog`,
`test_agent_builder_fallback`, `test_agent_builder_handler`, `test_agent_config_yaml_fallback`,
`test_bug_fixes`, `test_mcp_integration`, `test_mcp_tool_naming`, `test_provider_test`,
`test_runtime_caller_resolution`, `test_runtime_fallback`.

**Two wins worth recording.** Removal (a) eliminates an outbound call to a third-party unauthenticated
endpoint — a question typed into the component labelled *local* currently leaves the machine; and
(b) drops PyTorch/sentence-transformers entirely.

**Three things to decide, not assume:**

1. **A fresh install would boot with no provider and no onboarding.** `agent/config.py:239-240` defaults
   `default_provider`/`default_model` to `local-kb/local-kb`, and `_create_default_config` **writes that
   into a new install's `agent.json`**. The wizard exists precisely to catch "no provider configured."
   Remove both and a new install has nothing. Pick one: a first-run Settings → Providers prompt, a real
   default provider, or accepting an inert install until configured. This is decision #6 (§4).
2. **The `KB_OUT_OF_SCOPE` → `fallback_provider` retry lives in the generic tool loop**
   (`agent/runtime.py:1622-1660`), and `fallback_provider` is a field on **every** agent. Only the helper
   uses `local-kb`, so it becomes dead in practice — but it is shared runtime code. Decide: rip it out,
   or leave it inert for a future use.
3. **It changes app startup visibly.** `auto_open: true` opens an Auxilium tab on *every* launch
   (`ui/window.py:199`). Intended, but not invisible.

**Cross-references — R5 resolves parts of R1.** R1's residual-reference row lists
`ui/views/auxilium_wizard.py` and `ui/handlers/auxilium_wizard_handler.py` for OpenClaw cleanup; R5
deletes both wholesale, which settles that row. R1's note that `test_kb_lookup.py` "needs partial edits"
also becomes moot — R5 deletes it. **Sequence R5 before R1** so the gateway work never touches files that
are about to disappear.

**Sub-decision: does `knowledge/` survive as plain docs?** The `.md` files are also user documentation
that happens to be indexed. Deleting the *index* is the reduction; deleting the files is a separate call.
(The corpus is 672 KB — small enough to hand to a model directly, which is the cheap way to keep the
"help answers from the docs" idea at ~5% of the code.)

**Pre-existing inconsistency, made moot by removal:** `ui/handlers/project_handler.py:374` maps
`special:auxilium`, but `agent/special_agents.py` derives the session key from the role → `special:helper`,
which is what `ui/window.py` actually uses. Not caused by this change.

---

## 6. Provider changes — dictation STT and the Improve button

Both verified live against OpenRouter (not read from docs).

### 6.1 Dictation ("prompt") button → OpenRouter STT, remove all local STT

**Target: `microsoft/mai-transcribe-2`** — chosen for **keyword biasing**, which is the only thing that
makes dictation usable for this project (see below). Served by a single endpoint, provider tag
**`azure`** (model `microsoft/mai-transcribe-2-20260903`). Measured **$0.00044 per 15.6 s** of audio.

**Endpoint and shape** (`format` is required):

```
POST https://openrouter.ai/api/v1/audio/transcriptions
{ "model": "microsoft/mai-transcribe-2",
  "input_audio": { "data": "<base64 audio>", "format": "wav" } }
→ { "text": "…", "usage": { "seconds": 15.6, "cost": 0.00044 } }
```

#### Keyword biasing — the recipe (verified)

Provider-specific options go under **`provider.options`**, keyed by the **provider slug** from the
endpoints API (`GET /api/v1/models/<slug>/endpoints` → `endpoints[].tag`; for this model: `azure`).
Only the options for the serving provider are forwarded, under that provider's own field names.

```json
{
  "model": "microsoft/mai-transcribe-2",
  "input_audio": { "data": "<base64>", "format": "wav" },
  "provider": {
    "options": {
      "azure": {
        "phraseList": { "phrases": ["DevelCakes", "Crabcakes", "Salinas Valley Solid Waste Authority"] }
      }
    }
  }
}
```

**Verified 3 runs with / 3 runs without on the same clip — deterministic, not variance:**

| | DevelCakes | Crabcakes |
|---|---|---|
| without `phraseList` (3/3 runs) | ✗ "Devil Cakes" | ✗ "Crab Cakes" |
| with `phraseList` (3/3 runs) | ✓ **"DevelCakes"** | ✓ **"Crabcakes"** |

With biasing: *"Okay, so DevelCakes is the new version of Crabcakes. Truck 15 is dumping 12,340 pounds
at the Salinas Valley Solid Waste Authority, and the ticket photo still needs to be uploaded before the
job can move to reviewed."*

**Shape rules — the accepted form is an object whose `phrases` is an array of plain strings:**

| Shape | Result |
|---|---|
| `{"phrases": ["DevelCakes"]}` | ✓ accepted, biasing works |
| `{"phrases": ["..."], "mode": "boost"}` | ✓ tolerated |
| `{"phrases": [{"phrase": "..."}]}` | 400 |
| `{"phrases": [{"text": "..."}]}` | 400 |
| `{"phrases": [{"phrase": "...", "weight": 2}]}` | 400 |
| bare array `[{...}]` | 400 |

**Diarization and timestamps also work by the same route** — add `response_format: "verbose_json"` plus
`timestamp_granularities: ["segment", "word"]` and:

```json
"provider": { "options": { "azure": { "diarization": { "enabled": true } } } }
```

Response then carries `language`, `duration`, `segments[]` and `words[]`, each with a `speaker` index.

**Correction worth recording.** An earlier draft of this document claimed keyword biasing and
diarization were unavailable through OpenRouter. **That was wrong.** It came from testing *top-level*
field names (`keywords`, `prompt`, `customVocabulary`, `vocabulary`, `word_boost`, `hints`, `bias`,
`phrases`) — all of which return **200 and are silently dropped**, which looks identical to "unsupported".
The real mechanism is `provider.options`, documented at
`openrouter.ai/docs/guides/overview/multimodal/stt`. A field that is *accepted and ignored* and a field
that is *unsupported* are indistinguishable from the response alone; the tell is that the one correct
field name (`phraseList`) returned a **400 from the provider** when mis-shaped, because Azure forwards
most fields as-is and validates them. **Lesson: an ignored parameter is not evidence of an unsupported
feature — read the provider-option mechanism first.**

#### Model selection — measured, 20 models, same 15.6 s clip

The catalog does not list STT models; enumerate them with
`GET /api/v1/models?output_modalities=transcription` (21 models at time of writing).

| Model | Wall | Cost | Verdict |
|---|---|---|---|
| **microsoft/mai-transcribe-2** | 0.73–4.23 s | $0.00044 | **CHOSEN** — only one here that does keyword biasing + diarization |
| mistralai/voxtral-mini-3b-2507 | 0.99–2.97 s | $0.00026 | Cheapest accurate; no biasing |
| mistralai/voxtral-small-24b-2507-stt | 1.42 s | $0.00078 | Original pick; no biasing |
| openai/gpt-4o-transcribe | 1.64 s | $0.00090 | Accurate; no biasing |
| openai/whisper-large-v3 | 2.49 s | $0.00012 | Cheap — **but see traps** |
| meta/muse-voice-transcribe-1.0 | 3.57–3.70 s | $0.00078 | Slowest; **WAV-only** (400s on ogg); needs 18+ account verification; biasing advertised but not reachable |

**Traps to avoid (all measured):**

- **`openai/whisper-large-v3-turbo` and `nvidia/parakeet-tdt-0.6b-v3` reproduce the local-whisper bug** —
  both returned **"£12,340"** with "pounds" dropped. Cheapest models on the board; do not pick on price.
- **`qwen/qwen3-asr-1.7b` took 59.7 s and `qwen3-asr-0.6b` took 94.9 s** for a 15-second clip. Unusable
  for push-to-talk.
- **`deepgram/nova-3` and `x-ai/grok-stt-1.0`** are accurate but render numbers as words ("twelve
  thousand three hundred and forty pounds") — wrong for dictating figures.
- **`google/chirp-3`** emits "lb" instead of "pounds" and costs ~16× mai-transcribe-2.
- **`nvidia/nemotron-3.5-asr`** misheard "Salinas" as "Selenas".
- **`meta/muse-voice-transcribe-1.0` requires WAV** ("input is not a RIFF/WAVE container") — it rejects ogg.

**Latency caveat:** measured wall time for identical requests ranged 0.73 s → 4.23 s. Budget ~1 s
typical, tolerate ~4 s worst case. Costs are exact; timings are not.

**Also note:** every local whisper model (tiny/base/small) transcribed a weight as **"£12,340"**, dropping
"pounds" entirely; every cloud model except the two in the traps list produced "12,340 pounds". That is
the strongest single argument for moving dictation to the API.

**Remove:**

| Target | Note |
|---|---|
| `utils/stt.py` | faster-whisper engine, `arecord`/ALSA capture, Hugging Face model download, `STT_MODEL_SIZE` allowlist + `_VALID_MODEL_SIZES` |
| `ui/handlers/media_handler.py` | the `from utils.stt import STTEngine` call site and its GLib dispatch |
| `tests/test_low6_8_9_10_11.py` | the LOW-6 model-size validation tests become moot |
| `tests/test_architecture.py` | drop the `stt.py` entry |

**Capture stays.** The microphone path (`arecord` / PipeWire at 16 kHz mono) is still needed — audio is
captured locally and then uploaded. What disappears is the local *transcription* engine and its model
download, not the capture. **Note the format:** send WAV (or convert to it) — some providers reject other
containers, and WAV is what the transcription endpoints are most consistent about.

**[SUP-REV] Cap clip length.** 16 kHz mono WAV is ~32 KB/s (~500 KB per 15.6 s clip, ~667 KB base64).
A 60 s hold is a ~2.5 MB upload and a proportionally slower and costlier transcription. Cap the
dictation hold (30–60 s) in the handler, not just the HTTP client — push-to-talk UIs that allow
unbounded holds will eventually produce a request that times out mid-upload with no partial result.

**Two consequences to accept deliberately:**

1. **Audio leaves the machine.** Local STT meant voice never left the box; this sends it to OpenRouter →
   **Azure/Microsoft**. That is a privacy posture change and belongs in the threat model, not just the
   changelog. **[SUP-REV] Two items belong beside it: (a) Azure data-retention terms for uploaded
   audio — check what Microsoft retains and for how long before dictation becomes a habit; (b) the
   vocabulary `phraseList` itself flows to the provider — it will contain project/customer names, so
   it inherits the same classification as the audio.**
2. **Dictation now depends on network + key + quota.** Needs a clear failure path (no network, 429,
   invalid key, provider 400) and reuses the existing OpenRouter credential store rather than a new secret.
   Note that failure modes now include **provider-level 400s** surfaced as a generic
   `{"error":{"message":"Provider returned 400"}}` — the upstream detail is not forwarded, so a
   mis-shaped `provider.options` payload is hard to debug from the response alone.

**Vocabulary list is operational data, not code.** The `phraseList` should be configurable (dumpster
sizes, truck names, customer names, project names like DevelCakes/Crabcakes) rather than hard-coded, so
new terms can be added without a release.

### 6.2 "Improve" button → `google/gemini-2.5-flash` on OpenRouter

**Target:** `google/gemini-2.5-flash` — confirmed present in the OpenRouter catalog (alongside
`-lite`, `-image`, and `:batch` variants).

**Change:** `utils/improve.py` currently calls the **MiniMax** API — it reads
`~/.config/crabcakes/config.json` (`apiKey`, `baseUrl`, `model`) and the system template from
`prompts/system/improve.md`. Repoint it at OpenRouter chat completions with `google/gemini-2.5-flash`
and the existing OpenRouter key; the prompt template and the improve callback contract stay as they are.

**Retain:** the `_NoAuthRedirectHandler` in `utils/improve.py` that strips the `Authorization` header on
cross-host redirects — that is a real protection and must survive the provider swap.
**[SUP-REV] And deduplicate it while you're in there:** the identical class also exists at
`utils/provider_test.py:25`. Extract to one shared util (e.g. `utils/http_safe.py`) and import from both —
the provider swap is the natural moment, and leaving two copies means the next provider change patches
one and not the other.

---

## 7. Reference — JEV facts established this session

- Model: `typesafe/jev-1.13` on OpenRouter (versioned id required; `jev-latest` is rejected there).
  Resolved build observed: `typesafe/jev-1.13-20260917`.
- Endpoint: `POST https://openrouter.ai/api/alpha/decisions` — **not** chat/completions, which
  rejects it with "this is a decisions model".
- Body: `{ model, state: string|object|array, questions: { <id>: { type: choice|score|noul,
  instructions, criteria } } }`.
- Returns typed answers with probabilities and `confidence`, plus usage and cost.
- Primitives: **Choice** (one of a set), **Score** (ordered rubric), **Noul** (P(yes), 0–1).
- Measured live: 0.24 s and $0.0000178 for a 3-question call (424 input tokens) — ~18× faster and
  ~96× cheaper than the recorded Gemini baseline on the same questions, and it reported confidence
  0.43 where the LLM claimed 0.82.
- Boundaries: no text generation at all, text-only input, 64k context, English strongest; TypeSafe's
  own docs say explicitly "not agents". Calibration is not externally proven — tune thresholds
  before trusting a value.
