# SPEC-22: Provider Failure Lifecycle — detect, notify, decide, fail over

**Date:** 2026-10-09
**Author:** Supervisor
**Status:** READY — Stage 1 for implementation; Stages 2–3 interfaces pinned, delivery gated
**Implements:** PM direction 2026-10-09 (provider-failure lifecycle discussion; PM ratified the
chain-walk policy, the 400 rule, and the JEV role with corrections recorded inline)
**Depends on:** SPEC-21 (the `_open`/`urlopen_with_ssl_retry` choke point — read, do not modify)
**Supersedes:** nothing; **resurrects** the dead `fallback_provider` plumbing (see DISCOVERY §0)
**Target branch:** main

> **Architecture compliance:** detection and policy live at the existing LLM choke points in
> `agent/runtime.py` + `agent/llm/`; the failure store is a new `utils/` module (pure, no GTK);
> UI fan-out rides the EXISTING feed-card and chat-error paths (no new surfaces); the agent-facing
> surface is one new read-only tool. The turn engine's concurrency contract (per-session locks,
> `_terminate_turn` subsumed dispatch) is not renegotiated here.

---

## 0. The problem, verified in code (not assumed)

**Provider failures are detected inconsistently, surfaced partially, and never acted on.**

Three concrete proofs from this codebase:

1. **The fallback feature is dead.** `fallback_provider`/`fallback_model` are authored in every
   live agent YAML (`coder.yaml: fallback_provider: M3`, `supervisor.yaml: M3`,
   `debugger.yaml: GLM 5.3 Full`, `auxilium.yaml: openrouter`), loaded
   (`special_agents.py:116`), edited via a mandatory-feeling UI (`agent_builder.py:345` —
   *"every agent must have a fallback provider configured"*), stored on the Conversation
   (`conversation.py:178`), and persisted (`persistence.py:124,395`). **No code anywhere reads
   them to make a decision.** There is no `if conv.fallback_provider` in the repo. When a
   provider dies, the turn fails. Full stop. The UI promises resilience that does not exist.
   (`agent_runtime_handler.py:1108` records a partial removal on 2026-06-15 — this is finishing
   a job someone started, not inventing one.)
2. **Notification reaches exactly one place, sometimes.** The runtime's failure paths all
   funnel into `_terminate_turn(FAILED)` → `_dispatch(self._on_error, sk, err_msg)`
   (`runtime.py:~908-918`) → `AgentRuntimeHandler._on_error` (`:1901`) → `_do_error`
   (`turn_ui.py:641`) → **a chat bubble `[Error] …` in the failing agent's tab.** No feed card.
   No Supervisor visibility. If the PM isn't looking at Coder's tab, nobody knows. And some
   paths don't even reach `_on_error` (see §0.1).
3. **The recovery loop is manual detective work.** The PM's only recourse today: notice
   silence → ask the Supervisor to "check for signs of life" → the Supervisor guesses from file
   edits and feed activity. This is the workflow this spec deletes.

### 0.1 Detection inventory — every path, its current fate

READ from source (runtime.py line anchors at HEAD `9645e89b`):

| # | Failure path | Where | Today's behavior |
|---|---|---|---|
| D1 | HTTP/SSL/DNS at request send | `urlopen_with_ssl_retry` (streaming.py:290) retries transient 4× with backoff, then raises | Exception propagates to D2 |
| D2 | `_call_llm` call/stream raises | `runtime.py:2504,2533` `except (IndexError, KeyError, TypeError, ValueError)` — attaches `_crabcakes_context` (provider/model), re-raises | Caught by `_run_loop`'s handler → `_terminate_turn(FAILED)` → chat bubble ONLY. **HTTPError/URLError/TimeoutError are NOT in that tuple — they ride the generic handler at `runtime.py:1956`** |
| D3 | Mid-stream error event (OpenRouter `finish_reason="error"`) | captured as `_stream_error` (`runtime.py:2620-2710`), surfaced at `:1792` (empty content) and `:1842` (partial content) | `_terminate_turn(FAILED)` → chat bubble ONLY, **string-mangled provider code** (`Provider error (code=429): …`) — machine-readable class is discarded |
| D4 | Empty/whitespace content | `runtime.py:~1780-1840` — placeholder assistant message + FAILED | chat bubble ONLY; cause indistinguishable from real provider error |
| D5 | JSON-decode of bad body | inside provider `call()` (`openai_provider.py:77`) `json.loads` | raises ValueError → D2 |
| D6 | Stream ends with no `done` event | `runtime.py:~2700` stream-fallback path | **Silently treated as a complete response** (possibly empty) — NO error at all |
| D7 | Compaction/summary call fails | `_call_for_summary` → `provider.call` (`runtime.py:~3104`) | raises into compact → caught where invoked; compaction silently no-ops on some paths |

**Verdict: two paths (D2-partial, D3) notify chat-only; D6 notifies nobody; D7 sometimes
nobody. Zero paths notify the feed or the Supervisor. Zero paths retry or fail over.**

### 0.2 The incident that proves the class (2026-10-09, this project)

The `web_search` tool was misconfigured (`BRAVE_API_KEY` absent from the app's environment).
The app had no idea until a call was made and failed. The PM discovered it by attempting a
task. Same disease, smaller organ: **configuration faults are silent until probed.** This spec
adds a startup sanity check for exactly this class (§2.5).

---

## 1. PM-ratified policy (the contract this spec implements)

From the 2026-10-09 discussion, including the PM's correction (recorded because I initially
proposed the opposite):

**The provider chain is `[primary, fallback]`, walked in order. Per provider, per error
class:**

| Class | Same-provider retry | Then |
|---|---|---|
| **transient** — 429, 5xx, timeout, network/SSL/DNS | YES — up to 2 policy-level retries with backoff (on top of the existing transport-level retries in `urlopen_with_ssl_retry`) | chain advances to fallback |
| **permanent-auth** — 401, 402, 403 (dead key, out of credit, banned) | NO — same key, same failure | **chain advances IMMEDIATELY** (★ PM correction: "a 401 on the main provider SHOULD failover; if the fallback also 401s then that's a legit failure and the work halts" — the fallback is a different provider with a different key; a 401 says nothing about it) |
| **permanent-model** — 404 model, 400 model-specific | NO | chain advances immediately |
| **bad-request** — 400 malformed (OUR bug: bad payload, schema violation) | NO | **HALT + loud report. No failover** — a lenient fallback provider accepting our broken payload would MASK the bug (★ PM ratified) |
| **unknown** — everything unclassifiable | NO | JEV classifier, deterministic fail-open (§2.4) |

**Chain outcomes:**

- **Primary fails → fallback succeeds:** the turn continues on the fallback; a feed card
  records the failover ("OpenRouter 401 — failed over to M3"); the chat shows a one-line note.
- **Both providers fail (chain exhausted):** **LEGITIMATE HALT** (★ PM's words). The turn ends
  FAILED with a typed report; the event fans out (feed + chat + Supervisor); any held work
  claim is RELEASED (a halted agent must not hold a lease it cannot finish — touches
  `utils/work_persistence.py`, §2.6). If BOTH failed with auth-class errors, the report says
  so explicitly — dual-auth-failure is an account-level signal (billing/expired keys), the fix
  is human.
- **bad-request halt:** turn ends FAILED with metadata naming it OUR bug — the error card says
  "request rejected as malformed — this is a develcakes bug, do not retry; report it."

**Mid-stream failover — PM DECISION (2026-10-09): option (b), abort-clean-and-restart.**
A failure AFTER tokens have been rendered to chat is handled by: (1) aborting the visible
turn cleanly — the partially-streamed content is closed with a visible marker
(`[stream interrupted — provider failed; retrying on <fallback>]`), NOT silently truncated;
(2) discarding the partial assistant message from conversation state (it must never be sent
back to a provider — a truncated assistant turn poisons context); (3) re-issuing the SAME
request against the next provider in the chain as a FRESH request. This is the PM's explicit
choice over the simpler "halt on mid-stream" floor, accepted WITH its cost: the reader sees a
cut-off answer, so the marker and the failover feed card are load-bearing, not cosmetic.
Implementer MUST NOT silently truncate or silently re-issue without the marker (both are
context-corruption bugs).

---

## DISCOVERY (steel-framed)

- **Read `agent/runtime.py`**: `_run_loop` (def :1453); `_call_llm` (:2317) with its two
  narrow `except` clauses (:2504 streaming, :2533 blocking) that attach
  `_crabcakes_context = {"provider","model"}` and RE-RAISE; the generic turn-level handler
  (:1956 `except Exception`) → `_terminate_turn`; `_terminate_turn` dispatch contract
  (:~899-935: COMPLETED → `_on_response_complete`, FAILED/CANCELLED → `_on_error`, then
  `_auto_save`); `_call_llm_streaming` (:2550) event loop with `error`/`usage`/`done` handling
  and the no-done fallback (:~2700); `_stream_error` surface points (:1792 empty-content,
  :1842 partial-content — both already build `error_text` strings and terminate); empty-content
  placeholder path (:~1780); `friendly_error_message` import surface at `turn_ui.py:697`.
- **Read `agent/llm/streaming.py`**: `urlopen_with_ssl_retry` (:290, three except families,
  backoff sleeps); `friendly_error_message` (:508 — user-facing strings, loses the machine
  class); `is_retryable_ssl_error` (:434). SPEC-21's `_open`/`_try_pooled` sit inside the
  retry loop — read-only dependency for this spec.
- **Read `ui/agent_runtime/turn_ui.py` `_do_error` (:641-710)**: the ONLY consumer of
  `on_error` — ends streaming, renders a chat bubble `[Error] {msg}` with
  `_crabcakes_context` enrichment, stores `_last_error_exception`. **No feed, no bus.**
- **Read `ui/handlers/agent_runtime_handler.py`**: `_on_error` (:1901) → GLib idle →
  `_do_error`; runtime registration `on_error=self._on_error` (:880). The SSE-hardening
  `_last_error_exception` registry (:227).
- **Read the fallback fossil** (§0.1): `special_agents.py:43-44,116-117`;
  `config.py:86-87,253-254,279-280`; `conversation.py:178-179`; `persistence.py:124-125,395-396`;
  `runtime.py:1029-1030,1107-1108` (store-only); `agent_builder.py:121,191,345-418`.
- **Verified via live web (2026-10-09, the PM insisted — correctly)**: JEV is real.
  TypeSafe AI "System One" model; 70–500 ms end-to-end; $0.042/MTok input, output free;
  typed structured outputs (no type errors by construction); calibrated
  confidence/probabilities on every answer; RLCD training; marketed exactly for "smart
  if-statements… classify, route… where hand-written logic is too brittle."
  Source: typesafe.ai/blog/introducing-system-one-models-and-jev (fetched via `web_fetch`).
- **TO-VERIFY at implementation (not read in this discovery pass — re-grep before wiring):**
  the `/status` command's handler and its data sources; `feed_card.py`'s exact
  `FeedCardData` construction API for a new card type (the SPEC-19 SP4 approval-card pattern
  at `agent_runtime_handler.request_live_bridge_approval` is the model to copy);
  `utils/work_persistence.py` claim/release signatures.

### Existing patterns followed

- **Choke-point instrumentation** (SPEC-21 did this for sockets; we do it for failures).
- **Bounded in-memory registry + disk append** (the SPEC-15 telegram-store pattern; the
  compaction-events cap at `runtime.py:~1650`).
- **Handler-callback fan-out with GLib idle dispatch** (`_on_error`'s exact shape).
- **Fail-open floors** (E1/E2 precedent: a degraded path must never be worse than today).

---

## 2. Design

### 2.1 `agent/llm/failure_classifier.py` — NEW (pure, the single classification authority)

```python
class FailureClass(str, Enum):
    TRANSIENT = "transient"            # 429, 5xx, timeout, network/SSL/DNS
    PERMANENT_AUTH = "permanent_auth"  # 401, 402, 403
    PERMANENT_MODEL = "permanent_model"# 404, model-specific 400
    BAD_REQUEST = "bad_request"        # generic 400 — OUR payload bug
    UNKNOWN = "unknown"

@dataclass
class ProviderFailure:
    session_key: str
    provider: str          # card name, from _crabcakes_context / provider_name
    model: str
    failure_class: FailureClass
    status_code: int | None    # HTTP status when known (incl. OpenRouter _stream_error code)
    detail: str                # sanitized, human-readable, ≤200 chars
    stage: str                 # "request" | "stream" | "empty_content" | "summary"
    attempt: int               # which chain attempt (1=primary, 2=fallback)
    timestamp: float

def classify_exception(exc: BaseException, provider: str, model: str) -> FailureClass:
    """Map a raised exception to a class. Order matters:
    HTTPError.code 401/402/403 → PERMANENT_AUTH
    HTTPError.code 404 → PERMANENT_MODEL
    HTTPError.code 400 → BAD_REQUEST (subject to §2.4 JEV refinement)
    HTTPError.code 429 or >=500 → TRANSIENT
    TimeoutError / socket.gaierror / ssl.SSLError / ConnectionError / URLError
      → TRANSIENT (walks the chain like is_retryable_ssl_error does)
    anything else → UNKNOWN
    NEVER raises; NEVER returns None."""

def classify_stream_error(error_payload: dict) -> FailureClass:
    """Map an OpenRouter-style _stream_error dict ({code, message}) to a class.
    code 429 → TRANSIENT; 401/402/403 → PERMANENT_AUTH; 404 → PERMANENT_MODEL;
    400 → BAD_REQUEST; >=500 → TRANSIENT; message-text heuristics only for
    UNKNOWN refinement (e.g. 'rate limit' in message with code 0 → TRANSIENT)."""
```

**Redaction rule (SPEC-15 lesson):** `detail` is built through the EXISTING
`utils/log_redaction`-family sanitization if imported cheaply, else by never including raw
exception bodies beyond status+reason — URLs with credentials must never enter the store.

### 2.2 Detection wiring — three sites in `agent/runtime.py`, zero new try-blocks

1. **D2 (request-boundary raise):** inside the two existing narrow `except` clauses
   (:2504, :2533) AND the generic `:1956` handler — classify FIRST, attach
   `exc._failure = ProviderFailure(...)` (the `_crabcakes_context` precedent), then re-raise /
   terminate as today. The classifier is additive: existing behavior is unchanged until the
   policy layer (Stage 2) reads `exc._failure`.
2. **D3/D4 (stream-error & empty-content):** at the two `_stream_error` surface points
   (:1792, :1842) and the empty-content branch — classify via `classify_stream_error`; stash
   the `ProviderFailure` into `TurnResult.metadata["provider_failure"]` (metadata already
   flows through `_terminate_turn` untouched).
3. **D6 (no-done stream end):** the stream-fallback path currently treats a done-less end as
   success. Add: if `not full_content and not tool_calls` → classify UNKNOWN, metadata
   attached, FAILED terminate (instead of a phantom empty COMPLETED). If content exists, leave
   today's behavior (a trailing-usage-missing provider must not become an error).

**D7 (summary path):** classify inside `_call_for_summary`'s caller; on failure, emit the
event but do NOT halt the turn (compaction failure is already non-fatal today — keep that).

### 2.3 `utils/provider_failure_store.py` — NEW (the bus every consumer reads)

Bounded deque (100 events, same cap discipline as `_compaction_events`) + append to
`audit-log.jsonl` (one line per event, the existing audit shape). Pure Python, thread-safe
via one lock, no GTK. API:

```python
def record(failure: ProviderFailure) -> None
def recent(limit: int = 20, session_key: str | None = None) -> list[ProviderFailure]
def summary() -> dict   # {"last_24h": int, "by_class": {...}, "agents_affected": [...]}
```

### 2.4 Notification fan-out (Stage 1's deliverable)

**One event, three surfaces, all riding existing seams:**

1. **Feed card (PM visibility even with the tab closed):** window wires a new
   `runtime → handler` callback `on_provider_failure(failure)` (the exact shape of
   `set_on_agent_end` wiring at `window.py:~730`); the handler emits a `FeedCardData`
   (`card_type="provider_error"`, author=failing agent, body = class + provider + detail,
   metadata carries the full dataclass). **Copy the SPEC-19 SP4 approval-card construction
   pattern; do not invent card plumbing.**
2. **Chat (unchanged):** `_do_error` keeps doing what it does; it additionally renders the
   class line ("[Provider failure — auth] OpenRouter 401") instead of the raw string when
   `metadata["provider_failure"]` is present.
3. **Supervisor awareness (kills the detective loop):**
   - a new read-only agent tool `provider_events` (`agent/tools.py`, registered for supervisor
     and coder roles) — returns `recent()` formatted; the Supervisor's next "is Coder alive?"
     becomes one tool call with a factual answer;
   - `/status` compiles `summary()` into its output (TO-VERIFY the handler's shape at
     implementation);
   - Stage-2 option (registered, not shipped): push-inject a system note into the
     Supervisor's conversation on dual-failure halts.

**Startup sanity check (§0.2's lesson):** on app activate (after `migrate_v1_config`), one
non-blocking check — providers.yaml parses and has ≥1 enabled card; for every agent card with
web tools, `BRAVE_API_KEY`/`OPENCLAW_BRAVE_API_KEY` present (warning card, not fatal); per
enabled provider, `fallback_provider` (if set) resolves to a real card (the fossil becomes
LOAD-BEARING in Stage 2 — misconfiguration must be visible at startup, not at 2 a.m.).
Results render as ONE feed card ("startup checks: n ok / n warnings").

### 2.5 Stage 2 — `agent/llm/failure_policy.py` (NEW, pure; interface ships in Stage 1's PR as types only)

```python
@dataclass
class Decision:
    action: Literal["retry", "advance_chain", "halt"]
    provider: str            # the provider to use for retry/advance
    backoff_s: float
    reason: str              # audit line, e.g. "401 on OpenRouter (attempt 1) → advance"

def decide(failure: ProviderFailure, attempt: int, chain: list[str],
           cfg: RetryConfig) -> Decision: ...
```

The §1 table is the whole function — five literal rows, exhaustively unit-tested, no model
call. `RetryConfig` (new, `agent/config.py`): `provider_retries=2`, `backoff_base_s=1.0`,
both overridable in agent.json. **The `decide()` signature is the JEV seam:** Stage 2.5 may
route ONLY the `UNKNOWN` row through JEV (`classify via JEV first, heuristic fallback`) under
a `jev_enabled` config flag default OFF, with the hard contract: JEV unreachable / >800 ms /
confidence < 0.6 → deterministic floor (UNKNOWN at attempt 1 → TRANSIENT-style single retry;
UNKNOWN at attempt ≥2 → halt). JEV's real specs (70–500 ms, typed outputs, calibrated
confidence — §DISCOVERY) fit this seam; the enumerated rows NEVER consult it.

### 2.6 Stage 3 — actuation (gated; not in Stage 1's diff)

In `_call_llm`, wrap the provider call in the chain-walk: on Decision, re-resolve
provider/model from the fallback CARD (the SPEC-20/21 `live_card` overlay machinery already
rebuilds caller/base_url/key per call — reuse it), re-issue at the request boundary, emit the
failover feed card. Halt path: `_terminate_turn(FAILED)` with the typed report +
**work-claim release** (`work_persistence` release-if-held; TO-VERIFY the lease API) +
`stop-all` integration check (the SPEC-10 turn registry must see a failed-over turn as ONE
turn). `conv.model` switching mid-turn must update `_compute_model_max` (the compaction
budget follows the provider — verified those helpers read `conv.model` live).

### 2.8 The `DecisionModel` provider — JEV configured as a first-class provider (PM design 2026-10-09)

**The problem this solves:** JEV needs an endpoint + key + model slug that the user controls,
but the CODE needs a stable handle it can look up without depending on a user-chosen name.
A bespoke `jev.json` would be a second config system with its own save/validate/migrate bugs
(the reasoning-effort "field-strip-on-save" class). Instead: **JEV is an ordinary
`ProviderConfig` in the existing `providers.yaml`, reached through a hard-coded name.**

**Design:**

1. **Module constant** (greppable, single source of truth):
   `agent/llm/jev_client.py::DECISION_MODEL_PROVIDER = "DecisionModel"`. Every lookup uses the
   constant; nothing hardcodes the endpoint, key, or model slug.

2. **A dedicated Settings section** — a **"Decision Model"** page in the settings stack, beside
   "Telegram Bridge" (`settings_dialog.py:498` `add_titled(...)` is the exact pattern to copy).
   It hosts ONE provider card pre-named `DecisionModel`, **name field fixed/non-editable**,
   all other fields (Base URL, API Key, model slug, context window, etc.) user-editable.

3. **It is a real `ProviderConfig`** — same `providers.yaml` store, same `_to_dict`/`_from_dict`
   round-trip, same validation, same Test Connection machinery. It carries a **new caller id**
   (`"typesafe"` / `"jev"`) so its adapter speaks JEV's structured-decision API, not chat
   completions. The user sets base_url; the adapter knows the request/response shape.

4. **It must NOT appear in the general Providers list / agent-provider dropdowns.** It is not a
   chat model; offering it as an agent's provider is nonsense. Filter it out of those lists by
   name (the `DecisionModel` constant) while keeping it a real stored card.

5. **Test Connection for this card probes a SAMPLE TYPED DECISION** (a canned
   classify-this-fixture call returning a structured result), not a chat completion — so the
   user gets an honest "JEV works" signal.

6. **`DECISION_MODEL_ENABLED` / caller wiring**: `jev_client.classify_unknown` resolves the card
   by `DECISION_MODEL_PROVIDER` at call time. Config sources (held in module constants):
   `DECISION_MODEL_PROVIDER = "DecisionModel"`; the card provides base_url + api_key + model.
   The env-var path (`DEVELCAKES_JEV_API_KEY`/`BASE_URL`) from §2.4 becomes the FALLBACK when no
   card exists — the card is the primary, human-editable source.

7. **Optional by construction.** No `DecisionModel` card → `classify_unknown` returns `None` →
   deterministic floor. The entire failure lifecycle works without JEV. Startup sanity check
   (§2.7) warns if a `DecisionModel` card exists but is keyless/unreachable — visible at launch,
   not at 2 a.m.

> **Why hard-coded name + user-editable everything-else is the right split:** the user can
> repoint JEV at a proxy, a new TypeSafe endpoint, a different model slug, or a self-hosted
> compatible service, and the code keeps working because it asks for "the card named
> `DecisionModel`" — never for a URL. Configuration decouples from code; the identity the code
> depends on does not move. Same philosophy as the Telegram Bridge (one configured thing, its
> name structural, its contents user data).

**Files touched (Stage 2):** `agent/llm/jev_client.py` (constant + card lookup),
`agent/llm/registry.py` (new `typesafe` caller → a JEV/decision adapter), `utils/providers_store.py`
(no new fields — reuses `ProviderConfig`), `ui/views/settings_dialog.py` (Decision Model page +
name-lock + filtered provider list), `ui/handlers/settings_handler.py` (exclude `DecisionModel`
from agent-provider selection). **Stage 1 ships the constant + a card-lookup stub returning
`None`, so the seam exists and the UI can land in Stage 2 without a code change.**

---

## 3. Data flow (Stage 1)

```
provider call fails (D1–D7)
  → classify at the site (exception or _stream_error or empty-content)
  → ProviderFailure dataclass attached (exc._failure / TurnResult.metadata)
  → utils/provider_failure_store.record()        [bus + audit-log]
  → _terminate_turn(FAILED) as today
  → on_error → _do_error                          [chat, now with class line]
  → on_provider_failure → handler → FeedCardData  [feed — PM sees it]
  → provider_events tool / /status                [Supervisor answers factually]
startup: sanity check → one feed card
```

Stage 3 adds: `decide()` between classify and terminate — retry/advance re-enters
`_call_llm`'s request path; halt releases claims.

---

## 4. File change summary

| File | Change | Stage | Risk |
|---|---|---|---|
| `agent/llm/failure_classifier.py` | NEW ~120 | 1 | Low (pure) |
| `utils/provider_failure_store.py` | NEW ~80 | 1 | Low (pure) |
| `agent/runtime.py` | classify at 3 sites + metadata + `on_provider_failure` callback | 1 | **Medium** — hot paths; additive only |
| `agent/tools.py` | `provider_events` tool (~40) | 1 | Low |
| `ui/window.py` + `agent_runtime_handler` | wire callback + feed card (~60) | 1 | Low (copies SP4 pattern) |
| `ui/agent_runtime/turn_ui.py` | class line in `_do_error` (~10) | 1 | Low |
| startup check module (in `main.py` or `utils/`) | ~60 | 1 | Low |
| `agent/llm/failure_policy.py` | NEW ~90 (types in S1, logic in S2) | 2 | Low (pure) |
| `agent/runtime.py` chain-walk + claim release | ~120 | 3 | **High** — mid-turn provider switch; separately reviewed |
| tests: `test_failure_classifier.py`, `test_provider_failure_store.py`, runtime fan-out tests, policy table tests, actuation tests | ~400 total | 1–3 | — |

**Files NOT changed:** `agent/llm/streaming.py` (SPEC-21 owns it; the classifier consumes its
exceptions), the agent-builder fallback UI (Stage 3 makes it honest; until then it stays as-is
— removing it now would churn config for no behavior change), `friendly_error_message`
(superseded for classified paths, kept for unclassified ones).

---

## 5. Implementation order

1. **RED:** classifier table tests (every class × every carrier: exception, `_stream_error`
   dict, both) + store tests (cap, filtering, audit append). All fail (modules absent).
2. **GREEN:** implement both modules. Pure, plain pytest, no xvfb.
3. **RED→GREEN:** runtime wiring — a failing fake provider drive: assert `metadata.provider_failure`
   present, store has the event, `on_provider_failure` fired, `_do_error` shows the class line,
   feed card constructed. xvfb for the UI assertions.
4. Startup sanity check + its card.
5. `provider_events` tool + registration.
6. Stage 2 PR: policy module + exhaustive table tests (incl. the PM's 401-advances case as a
   NAMED test: `test_pm_correction_401_advances_chain`).
7. Stage 3 PR: actuation, behind a `provider_failover_enabled` config default OFF for the
   first release; Debugger adversarial audit (mid-turn model switch × compaction budget ×
   stop-all × claims) BEFORE default-on. Mid-stream policy: PM decision at review.

## 6. Acceptance criteria

- [ ] A1 Every path D1–D7 produces a classified `ProviderFailure` (table-driven test, all seven rows).
- [ ] A2 A provider failure produces ALL THREE surfaces: feed card, chat bubble with class line, store event — with one code path, no duplicates on dual-dispatch.
- [ ] A3 The Supervisor, asked "is Coder alive?", answers from `provider_events` in one call (integration test: drive failure → call tool → factual answer).
- [ ] A4 `/status` includes the failure summary; startup check emits its card (incl. the missing-BRAVE_KEY case from §0.2 as a named test).
- [ ] A5 No behavior change when no failure occurs: payload shapes, turn statuses, and existing test suites byte-identical (the classifier is additive).
- [ ] A6 Policy table: all five classes × attempts 1–2 × chain positions — exhaustively tested, including `test_pm_correction_401_advances_chain` and `test_400_never_fails_over`.
- [ ] A7 Stage 3 (gated): primary-401 → fallback continues the SAME turn with a failover card; dual-auth-failure halts with the account-level report and releases the work claim. **Mid-stream failure (PM ruling b): partial content closed with the visible interruption marker, partial assistant message DISCARDED from conversation state, request re-issued fresh on the fallback** — asserted by a test that fails if the partial message persists into the next provider call.
- [ ] A8 Redaction: no credential-bearing URL or raw key ever appears in store, card, or audit line (probe with a poisoned base_url).
- [ ] A9 `DecisionModel` (Stage 2): a card named `DecisionModel` is looked up by the
  `DECISION_MODEL_PROVIDER` constant; changing its base_url/key/model does not require a code
  change; it is excluded from agent-provider dropdowns; Test Connection probes a typed decision;
  no card present → JEV path returns `None` (no-op).
- [ ] A10 ruff 0 new, pyright 0 new, named suites green with pasted output.

## 7. Edge cases

| Case | Expected |
|---|---|
| Fallback card == primary card | startup check flags it; policy treats chain length 1 (failover to self is a retry — refuse, halt) |
| Fallback provider not in providers.yaml | startup warning; at runtime chain = [primary] |
| 429 with `Retry-After` | Stage 3 reads the header when present; backoff = max(policy, header) |
| Failure during fallback's own call | that's chain exhaustion → halt path, second event recorded with attempt=2 |
| Compaction-summary failure (D7) | event recorded, turn continues (today's non-fatal contract kept) |
| Two agents failing simultaneously | per-session events; store lock-ordered; feed shows both, no interleaving corruption |
| CANCELLED mid-retry | stop-all wins — retries check the turn token before re-issuing (SPEC-10 registry) |
| `_stream_error` with code 0 + useless message | UNKNOWN → JEV seam (if enabled) else deterministic floor |
| JEV timeout/unreachable | deterministic floor, logged once per process (no spam) |
| Failure store replay after restart | in-memory deque cold; audit-log.jsonl is the durable record (tool reads memory + falls back to tail of audit log) |
| No `DecisionModel` card configured | JEV features degrade to deterministic floor; lifecycle fully functional (JEV is optional) |
| `DecisionModel` card present but keyless/unreachable | startup sanity check emits a warning card; `classify_unknown` returns None at runtime |
| User renames/points the card elsewhere | code looks it up by the `DECISION_MODEL_PROVIDER` constant — works regardless of endpoint/model changes |
| `DecisionModel` appears in an agent-provider dropdown | filtered out by name — it is not a chat provider |

### PM decisions resolved (2026-10-09)

- **OQ-1 (mid-stream failover) = (b)** — abort-clean-and-restart with a visible interruption
  marker; partial assistant message discarded from conversation state. See §1.
- **OQ-2 (Supervisor delivery) = BOTH** — (i) an injected system note into the Supervisor's
  conversation on dual-failure halts (proactive), AND (ii) the readable `provider_events`
  queue/tool it can consult on demand. Ship (ii) in Stage 1; (i) in Stage 2.
- **DecisionModel** — JEV configured as a first-class provider through a hard-coded name
  (§2.8), user-editable endpoint/key/model, hidden from chat-provider selection.

## 8. Docs to update
ARCHITECTURE.md (§agent engine: failure lifecycle; §work claiming: halt-release rule);
README (provider-failure bullet); `prompts/system/supervisor.md` (the `provider_events` tool
exists — document when to call it); SPEC-15's redaction note (extend to failure details).

## 9. Rule 9 self-audit
1. Every runtime line anchor read at HEAD `9645e89b`; the D1–D7 table is from source, not
memory. 2. Exception carriers enumerated (the narrow tuple at :2504/:2533 MISSES
HTTPError — that's why the generic :1956 handler is also instrumented). 3. All new modules
pure; the only UI touch copies an existing wiring pattern. 4. Flow traced end-to-end §3.
5. Honest TO-VERIFYs: `/status` handler, feed-card constructor, work-claim API — named, not
hidden. 6. PM corrections are IN the contract (§1) as named tests (A6), so they can't be
lost in implementation. 7. JEV claims re-grounded in fetched source after my initial
wrong assumption — the seam design reflects the real specs.

**Deviations recorded:** my 401-no-failover position was wrong and is reversed per PM; JEV
moved from "Phase-2 garnish" to "UNKNOWN-class classifier with deterministic floor" after
source verification; mid-stream failover is PM-ruled (b) abort-and-restart with an explicit
context-integrity requirement (discard the partial assistant message).

**PM decisions folded in (2026-10-09):** OQ-1 = (b) abort-clean-and-restart; OQ-2 = both
(injected note + readable queue); DecisionModel = a first-class provider reached by a
hard-coded name, user-editable endpoint/key/model, hidden from chat-provider selection (§2.8).
