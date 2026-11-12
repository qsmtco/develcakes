# SPEC-63: Provider setup without YAML — presets, test-before-save, model autocomplete, env detection

**Date:** 2026-11-04
**Author:** Supervisor (PM-directed, from discussion of "make setup easier out of the box")
**Status:** COMPLETE — Waves A–D shipped 2026-11-11/12 (pre-req 068b2f0c, Wave A d5a33ce9, Wave B 5ae8b208, Wave C d1ff4522, Wave D docs this commit). Full-suite gate: 16 failures, all proven pre-existing on clean HEAD 5ae8b208. Deferred out of v1: MiniMax + Z.ai presets (unverifiable endpoints/IDs), Gemini `/models` on the `/v1beta/openai` path (live-check pending; fallback `supports_models_list=False`).
**Implements:** ideas 1, 2, 3, 5 from the 2026-11-04 setup discussion
**Explicitly OUT of scope:** idea 4 (first-run wizard), idea 6 (Ollama/local preset), "download a starter model" UX, `${VAR}` indirection in stored keys, **MiniMax + Z.ai presets (deferred pending live-key verification — see §2.1)**
**Depends on:** SPEC-62 fix-round 4 (`bd472833`, pushed — `save_providers` single-owner default maintenance, bare stored models, `_is_chat_card` gating)
**Target branch:** main, after `bd472833`
**Spec authoring prompt:** `.crabcakes/prompts/steelFramedSpecWriter.md` (rules 1–10 applied; discovery block + self-audit below)

> Architecture compliance: `utils/provider_presets.py`, `utils/model_catalog.py`, and `utils/env_provider_keys.py` are new pure modules — stdlib + `urllib` only, no `gi`/GTK, no `agent/*` imports at module scope (utils layer rule, `utils/providers_store.py:9-10,35-41`). All network I/O stays off the GTK main thread via the existing `SettingsHandler.test_provider` daemon-thread pattern (`docs/ARCHITECTURE.md` §3.11a "Threading"). All persistence flows through `save_providers()` — SPEC-62 fix-round 4 RULING 3's single-owner choke point must NOT grow a second owner. The catalog is static in the binary; no phoning home. Every smart default remains overridable (Custom path is permanent).

---

## DISCOVERY (steelFramedSpecWriter Rule 1 — every claim below traced to source read this session)

- Read `ui/views/settings_dialog.py` (1660 lines): `_ProviderCard.__init__(dialog, provider, *, fixed_name=False, allow_empty_caller=False, test_runner=None)` at :71-92 builds a blank `ProviderConfig(name="", base_url="", api_key="", default_model="", caller="")` when `provider is None` (:87-89) — FOUR free-text fields (:105-130, :133-148), a read-only caller label (:150-156, populated at :292-293), and buttons Test | Save | Remove (:217-235). `SettingsDialog._on_add_provider_clicked` (:760-766) appends exactly such a blank card; `+ Add Provider` button is built at :586-597 with label `"+ Add Provider"`. `_collect_from_form()` (:343-375) builds the `ProviderConfig` from entries and preserves `caller=existing.caller` (:351). `_populate_from_provider()` (:284-308) fills all fields. `_on_test_clicked` (:392-404) collects form values, sets "Testing…", then calls `self._dialog._handler.test_provider(provider, self._on_test_result)` — **it tests WITHOUT saving** (contrary to my earlier user-facing claim in chat; the spec corrects this: idea 2 is about *error translation + listing*, not introducing test-before-save). `_on_test_result` (:446-501) runs on the GTK main thread, updates status + context-window spin from `TestResult`. `refresh_providers()` (:693-756) filters DecisionModel/ImproveModel cards out of the general list and matches cards by name.
- Read `ui/handlers/settings_handler.py` (405 lines): `add_or_update(provider, *, allow_empty_caller=False)` (:80-168) requires non-empty name/base_url/api_key/default_model (:90-97), auto-detects caller from the `default_model` prefix (:99-149), and calls `save_providers(providers, prefer=provider.name)` (:162) — RULING 3's single-owner choke. **`test_provider(provider, on_result)` (:227-359)** duplicates caller auto-detect in its worker (:238-278), calls `test_connection(base_url=..., api_key=..., model=..., caller=... or None)` (:281-286), catches `Exception` (:287-294), stamps `last_verified_at`/`last_error` into the stored provider (:296-342), `save_providers(providers)` (:342), and dispatches `on_result(result)` via `GLib.idle_add` when `_GLib` is wired (:345-356) — synchronous fallback in tests. `_GLib_module` default is `None` (:53,58); `ui/window.py:278-283` passes `GLib_module=GLib` in production.
- Read `utils/provider_test.py` (307 lines): `TestResult(ok, latency_ms, error, model_used, context_window=None)` (:50-57). `test_connection(base_url, api_key, model, timeout_seconds=8.0, caller=None)` (:80-123) — returns `TestResult`, raises `ValueError(f"No adapter for provider: {provider}")` for unknown caller (:123). Errors are STRINGS: `HTTPError` → `f"HTTP {e.code}: {error_body}"` (:278-290); other `Exception` → `_safe_error(e)` (:292-299); invalid JSON → `f"Invalid JSON response: …"` (:210-216); MiniMax body-level → `f"status_code=…: …"` (:218-230); non-HTTPS → `f"Provider URL is not HTTPS…"` (:112-118). The OpenAI-compatible `/v1/models` GET probe ALREADY EXISTS (:232-268) — it parses `models_body["data"]` `id` fields and is best-effort (broad `except` :265-267 → `pass`). It currently only extracts a context-window field for ONE matching model; it does not return the id list.
- Read `utils/providers_store.py` (after `bd472833`): `_VALID_CALLERS` (:42-44) = `{anthropic, minimax, openai, openrouter, zai}` — duplication invariant with `agent/runtime.py::_PROVIDER_CALLER_NAMES` (:239-244, verified) and `get_valid_callers()` (:249). `save_providers(providers, *, allow_empty=False, prefer=None)` (:364-366) — RULING 3 branches (:436-522). `_effective_caller(p)` (:49-88) validates stored caller against `_VALID_CALLERS` and derives from a recognized `<prefix>/<model>` default_model. `_is_chat_card(p)` (:116-119) = effective caller ≠ "" AND name not in `_NON_CHAT_NAMES`. Branch 4 (:508-520) refreshes `default_model` from the LIVE default card via `_bare_model()` — **stored model must be BARE** (RULING 2), because `agent/runtime.py:5379` composes `f"{self._config.default_provider}/{self._config.default_model}"`.
- Read `models/providers.py`: `ProviderConfig.__init__` signature (via `inspect.signature`): `(name, base_url, api_key, default_model, caller='', enabled=True, supports_tools=True, supports_streaming=True, max_tokens=128000, default_max_tokens=0, context_window=0, compaction_threshold=0.8, last_verified_at=None, last_error=None, context_mode='auto', reasoning_effort='off', supports_reasoning=False, tool_output_offload_chars=0)`. `CALLER_DEFAULT_MAX_TOKENS` (:21-27) — anthropic 200_000, minimax 1_048_576, others 128_000.
- Read `agent/runtime.py` model-string semantics: `:5310-5320` (resolved_model uses `prov_cfg.default_model` verbatim if it contains `/`, else prefixes `f"{llm_name}/{prov_cfg.default_model}"`). **FIX-ROUND 1 CORRECTION (Debugger #6):** v1 cited `:3355-3362` as "registration" — it is the FAILOVER path (inside `_switch_to_fallback`, `:3333`). The primary registration is `agent/config.py:214-231`: name key (`:218`), full-model key (`:221-223`), bare-prefix key only when `prefix_counts[prefix] == 1` (`:227-230`). A card whose name ≠ caller (google row) resolves via its NAME key — `_resolve_provider_cfg` → `config.providers.get(conv.provider)`. `utils/provider_test.py::_model_id(model)` (:60-67) strips the FIRST segment (`"openrouter/qwen/qwen3.7-max"` → `"qwen/qwen3.7-max"`) before putting `model` in the request body. **Consequence (verified data flow):** preset `default_model` strings may be either bare (`gpt-4o`) or vendor-slash-prefixed (`openrouter/auto`); a prefixed model's first segment must be a valid caller or the runtime registers the wrong routing key AND `test_connection`'s `_model_id` chops a real model-name segment.
- Read `utils/config.py::get_env(name)` (:44-57): reads `DEVELCAKES_<name>` first, `CRABCAKES_<name>` fallback; "Presence, not truthiness". `utils/startup_checks.py::startup_warnings()` (:10-) is pure, returns warning strings, called from `ui/window.py::_emit_startup_check_card` (:1728-1746) which emits ONE feed card when warnings exist. `utils/agent_defs.py::_seed_defaults()` (:182) seeds built-ins; no env-key logic anywhere (`grep` for `OPENAI_API_KEY|ANTHROPIC_API_KEY|os.environ` in `agent/config.py` → zero; `get_api_key(provider_name)` at :506-510 reads only from stored config).
- Read `knowledge/providers.md` for documented endpoints/models: OpenRouter `https://openrouter.ai/api/v1` + `openrouter/auto` (:59-63) + key page `https://openrouter.ai/keys` (:84); OpenAI `https://api.openai.com/v1` + `gpt-4o` (:203-205, :220) + `https://platform.openai.com/api-keys` (:227); Anthropic `https://api.anthropic.com` (:261) + `https://console.anthropic.com/settings/keys` (:284); Gemini `https://generativelanguage.googleapis.com/v1beta` + `caller: openai` (:316-331) + `https://aistudio.google.com` (:298); MiniMax `https://api.minimax.chat/v1` + `MiniMax-M2.7` (`knowledge/configuration.md:503-511`); Z.ai `https://api.z.ai/api/coding/paas/v4` (`docs/proposals/PROPOSAL-llm-provider-settings-dialogue.md:82`). Note: `knowledge/providers.md:136` documents Ollama `http://localhost:11434/v1` — OUT of scope for this spec (idea 6).
- Read `ui/views/chat_input_toolbar.py::_build_save_menu_button` (:300-315): GTK4 `PopoverMenu` MUST be built from `Gio.Menu` (`menu.append(label, "toolbar.action")`) — manual `Box+Button` children cause GTK-CRITICAL assertions on GTK 4.14. Verified `Gtk.get_major_version()=4, minor=14`, `Gtk.UriLauncher` available in this environment.
- Read `ui/window.py:276-299`: real `SettingsHandler(GLib_module=GLib, parent_window=self, …)` and real `SettingsDialog(parent=self, handler=…)` construction; `wire_settings_handler` (`ui/wiring.py:68-99`) is idempotent (`_wired` flag).
- Read tests: `tests/test_settings_dialog.py` (57 `def test_`, constructs `SettingsDialog(parent=None, handler=h)` with real handler and `tmp_config_dir`, GTK-skipped via `pytestmark`), `tests/test_settings_handler.py` (61), `tests/test_provider_test.py` (34 — mocks at `utils.provider_test.urllib.request.urlopen` via `patch`, with `_mock_response(body)` and `capture_urlopen(req, **kwargs)` patterns).
- Architecture owner (per `docs/ARCHITECTURE.md` §3.11a :780-827): `utils/provider_test.py` owns the HTTP request lifecycle, `_OPENAI_COMPATIBLE`, and context-window discovery; `ui/handlers/settings_handler.py` owns Test Connection orchestration/threading; `ui/views/settings_dialog.py` is pure view delegating to handler.
- Existing patterns to copy: `SettingsHandler.test_provider`'s daemon-thread + `GLib.idle_add` dispatch; `utils/provider_test.py`'s `_NoAuthRedirectHandler` + saved/restored `urllib.request._opener` (:200-202, :300-307) — the model-list fetch MUST use the same opener discipline (audit BUG #1 documented there); `test_provider_test.py`'s urlopen patching; `ui/styles.py::.settings-provider-card` CSS class family (:1169-1206).

---

## 1. Overview

### Problem

`knowledge/setup.md:16-29` tells a new user to hand-edit `~/.config/crabcakes/providers.yaml` with `nano`. The Settings → Providers page does have a form, but `+ Add Provider` (:592) opens a card with FOUR empty free-text fields — Name, Base URL, Default Model, API Key — and a caller that only resolves on save. A new user must know their vendor's exact base URL, an exact model ID, and the `<vendor>/<model>` prefix convention (`settings_handler.py:110-121` **rejects** unprefixed models with an empty caller). Errors surface late and cryptically (`"HTTP 401: …"`, `"Cannot auto-detect caller: …"`, `"test_connection raised: …"`). Setup is the single largest out-of-box cliff after SPEC-62 fixed fresh-install agent gating.

### Solution summary

1. **Presets** — replace `+ Add Provider` with a `Gtk.MenuButton` popover listing vendor presets (static catalog). Choosing one creates a card pre-filled with name, base_url, default_model, context window, caller, and a "Get a key" link. The API key is the ONLY required user input. `Custom…` keeps the old blank-card path.
2. **Error translation on Test Connection** — map `TestResult.error` strings to actionable guidance (401/403 → key rejected + key-page link; 404 → wrong base URL; timeout/URLError → connectivity; 429 → rate limit; "model not found" → model typo). Also relax the "test only existing stored cards" limitation: testing an UNSAVED preset card must work without writing to disk.
3. **Model autocomplete** — a "Load models" button fetches `<base_url>/models` off-thread, turns the model field into a dropdown (editable free text fallback), and picks from real IDs. Reuses the existing `/v1/models` GET probe discipline in `utils/provider_test.py`.
4. **Env detection** — on first run (no provider cards), detect `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` and offer a one-click "Create card from environment key" chip in the Providers page; also extend `startup_warnings()` with one informational line so the offer is discoverable via the existing startup feed card.

### Scope table

| In | Out |
|----|-----|
| Static preset catalog (6 vendors + Custom) | First-run wizard / new window/screen (idea 4) |
| Test Connection error translation + unsaved-card test | Changing `test_connection` wire behavior or error strings |
| `/models` listing + model dropdown | Persisting model lists, caching to disk, any new provider caller |
| Env-var detection + one-click card creation | `${VAR}` indirection in stored `api_key` fields |
| Offline-first (catalog compiled in, zero network at startup) | Auto-fetching vendor catalogs, telemetry, phoning home |
| Ollama / localhost preset (idea 6) — **deferred by PM** | Model download/pull UX |
| GTK main-thread discipline (all I/O off-thread) | New CSS-only visual redesign beyond 3 small classes |

### Architecture principles that apply
- `utils/*` never imports `agent/*` or `gi` at module scope (duplication invariant at `providers_store.py:35-41`).
- `save_providers()` is the single owner of global-default maintenance (SPEC-62 fix-round 4 RULING 3) — no new default writes.
- Stored `default_model` is BARE (RULING 2); `agent/runtime.py:5379` composes `f"{default_provider}/{default_model}"`.
- Pure view / handler split: `settings_dialog.py` renders and delegates; `settings_handler.py` orchestrates I/O and threading.

---

## 2. Changes by File

### 2.1 NEW `utils/provider_presets.py` (~120 lines) — pure, stdlib only

Owns the static catalog. No imports from `agent/*`, `ui/*`, or `gi`.

```python
# utils/provider_presets.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPreset:
    key: str                  # stable internal id, e.g. "openai"
    label: str                # menu text, e.g. "OpenAI"
    name: str                 # default ProviderConfig.name (user-editable)
    base_url: str
    default_model: str        # BARE or "<valid-caller>/<model>" — see §2.1 note
    caller: str               # must be in _VALID_CALLERS
    api_key_url: str          # "Get a key" link target
    key_prefix_hint: str      # placeholder, e.g. "sk-…"
    context_window: int       # from models/providers.CALLER_DEFAULT_MAX_TOKENS
    supports_models_list: bool  # False for anthropic (no /models endpoint)
```

**V2 CATALOG RULING (fix-round 1, 2026-11-11 — adjudicated from Debugger #9/#10/#22 and Flagger #4):**

MiniMax and Z.ai are **REMOVED from v1**. Reasons, both verified against the repo's own tests:
- **MiniMax host split (Flagger #4, confirmed):** the runtime adapter POSTs `{base_url}/text/chatcompletion_v2` (`agent/llm/minimax_provider.py:103,152`) and its tests pin `https://api.minimax.chat/v1` (`tests/test_llm_providers.py:193,203`); Test Connection POSTs `{base_url}/chat/completions` (`utils/provider_test.py:139`) and its tests pin `https://api.minimax.io/v1` (`tests/test_provider_test.py:147,163,174,542`). Same caller, two wire paths, two different hosts' infrastructure. **A wrong preset ships a card that Tests green and 404s on every agent message.** Either host choice is broken for one of the two paths. Until a live key verifies which host serves BOTH endpoints, no MiniMax preset ships. (This also subsumes Debugger #10 — the v1 "Use .io" resolution was by fiat; and #22 — `https://www.minimax.io/` was a homepage, not a key page.)
- **Z.ai model ID (Debugger #9):** `glm-4.6` appears nowhere in the repo; the proposal records `glm-5.1` (`docs/proposals/PROPOSAL-llm-provider-settings-dialogue.md:84`); `models/providers.py:20` documents "GLM-4.5+, glm-5 series". No live key to verify against. DEFERRED (the spec's own Open Question #2 default was already "ship fewer, verified").

**V1 ships FOUR presets: OpenAI, Anthropic, OpenRouter, Google Gemini.** MiniMax + Z.ai rejoin only after live-key verification (§9 follow-up). §6's criterion updated accordingly ("exactly 4 vendor presets").

**V2 CATALOG CORRECTION — Google row (Flagger #2/#3 adjudicated): `default_model` → `openai/gemini-2.0-flash`** (was bare `gemini-2.0-flash`). The bare form left the §2.1 prefix invariant vacuous for the one row whose name≠caller split creates resolution risk. The prefixed form is self-verifying: prefix `openai` ∈ taxonomy, equals `caller`, `agent/config.py:221-223` registers the full-model key, and `_resolve_agent_model` (`ui/agent_runtime/provider.py:40-49`) returns it verbatim. Debugger's wire-path proof (the bare model DID reach the correct endpoint on the live tree — `_resolve_caller_key` prefers the stored `caller`, `agent/runtime.py:3053-3058`, verified returning `openai` under both model forms) is noted; the prefixed form is strictly safer and costs nothing. **Wire note:** the request body's model stays BARE — every stripper (`provider_test.py:60-67` and the runtime's) removes exactly one leading segment, so `openai/gemini-2.0-flash` → wire `gemini-2.0-flash`. The prefix exists ONLY for taxonomy/registration; do not "simplify" it away, and do not ship the bare form.

| key | label | name | base_url | default_model | caller | api_key_url | key_prefix_hint | context_window | supports_models_list |
|-----|-------|------|----------|---------------|--------|-------------|-----------------|----------------|----------------------|
| `openai` | OpenAI | `openai` | `https://api.openai.com/v1` | `gpt-4o` | `openai` | `https://platform.openai.com/api-keys` | `sk-…` | 128_000 | True |
| `anthropic` | Anthropic | `anthropic` | `https://api.anthropic.com` | `claude-3-5-sonnet-20241022` | `anthropic` | `https://console.anthropic.com/settings/keys` | `sk-ant-…` | 200_000 | False |
| `openrouter` | OpenRouter | `openrouter` | `https://openrouter.ai/api/v1` | `openrouter/auto` | `openrouter` | `https://openrouter.ai/keys` | `sk-or-v1-…` | 128_000 | True |
| `google` | Google Gemini | `google` | `https://generativelanguage.googleapis.com/v1beta/openai` | `openai/gemini-2.0-flash` | `openai` | `https://aistudio.google.com` | `AIza…` | 128_000 | True |

**Verified notes / deliberate choices (v2):**
- `google`: `knowledge/providers.md:331` documents the OpenAI-compatible endpoint as `base_url` **with `/openai/` appended**; `caller: openai`; `test_connection` POSTs to `<base_url>/chat/completions` (`provider_test.py:139`) — the appended path is required. Verified `provider_test.py::_OPENAI_COMPATIBLE` includes `openai`. `/models` on this path is unverified live — if it 404s at implementation time, set `supports_models_list=False` for google (button hidden, no error surface).
- `openrouter`: `default_model="openrouter/auto"` is prefixed with a VALID caller, which `settings_handler.py:129-142` keeps consistent (`prefix == caller`), `_model_id()` strips to `auto` on the wire (:60-67), and `agent/config.py:227-230` may register the bare-prefix key `openrouter` (guard: `prefix_counts == 1`). This is the one non-google preset whose model is deliberately prefixed.
- `anthropic`: base_url has NO `/v1` (`knowledge/providers.md:261`); `_test_anthropic` POSTs `<base_url>/messages` (`provider_test.py:163`); `AnthropicProvider` does the same (`agent/llm/anthropic_provider.py:65`). `supports_models_list=False` because `/v1/models` is skipped for anthropic (`provider_test.py:237-238,242`). Context 200_000 from `models/providers.py:23`.
- All `caller` values are in `_VALID_CALLERS` — a precondition for `_is_chat_card()` adoption (`providers_store.py:116-119`) and `add_or_update`'s taxonomy check (`settings_handler.py:143-149`).
- **Taxonomy duplication (Flagger #14 adjudication — supersedes the earlier two-option text):** `provider_presets.py` contains NO copy of the caller taxonomy. Enforcement is test-side ONLY: `tests/test_provider_presets.py::TestPresetCallerInvariant` asserts `preset.caller in agent.runtime.get_valid_callers()` for every preset (test files may import `agent.*`), and the §2.1 model-prefix invariant holds **unconditionally**: `if "/" in default_model: default_model.split("/", 1)[0] == caller`. The repo precedent for duplication+test (`tests/test_providers_store.py`'s `TestValidCallersDuplicationInvariant`) guards a *utils-module copy*; here there is no copy, so there is nothing to drift.

Public API:

```python
PRESETS: tuple[ProviderPreset, ...]  # in the table order above

def get_preset(key: str) -> ProviderPreset | None: ...
def presets_from_env(env: dict[str, str]) -> list[ProviderPreset]: ...  # §2.5
```

**`default_model` invariant to enforce in a unit test** (because a wrong prefixed model routes to the wrong adapter): for every preset, if `"/" in default_model` then `default_model.split("/", 1)[0] == caller` AND `caller` is in `_VALID_CALLERS`. Copy the taxonomy as a module-level frozenset with the same duplication-invariant comment as `providers_store.py:35-41`, OR assert it in the test only via `from agent.runtime import get_valid_callers` (test-side import is allowed; the utils module itself stays agent-free).

### 2.2 NEW `utils/model_catalog.py` (~110 lines) — pure network I/O, stdlib only

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelListResult:
    ok: bool
    model_ids: tuple[str, ...]   # ordered as returned; deduplicated, blanks dropped
    error: str | None            # already translated? NO — raw-safe string; see §2.3


def fetch_model_ids(
    base_url: str,
    api_key: str,
    caller: str,
    timeout_seconds: float = 8.0,
) -> ModelListResult: ...
```

Behavior (traced against `utils/provider_test.py:232-268`):
- If `caller not in _OPENAI_COMPATIBLE_LOCAL` (local copy of `{"openai","openrouter","zai","minimax"}` — duplication invariant with `provider_test.py:43`; do NOT import it, it is private), return `ModelListResult(ok=False, model_ids=(), error="This provider does not expose a /models endpoint")`. Anthropic preset hides the button anyway, but the function must be safe if called.
- Build `url = base_url.rstrip("/") + "/models"`, `urllib.request.Request(url, method="GET")`, `add_header("Authorization", f"Bearer {api_key}")`.
- **Opener discipline (v3 — §9's fix-round-1 footnote is now THE body text; the v2 body's "prefer import" guidance is RETIRED):** use a LOCAL opener, never `install_opener`. `model_catalog.py` defines its OWN `_NoAuthRedirectHandler` (do NOT import `utils.provider_test`'s private name — cross-module private coupling; and do NOT import `utils.git_ops` at all: it pulls GitPython at module scope — measured 127 modules — into a "stdlib only" module). A regression test asserts this module's handler behaves identically to `provider_test.py`'s on a cross-host redirect (the two must not drift). **Never mutate `urllib.request._opener`.** (audit BUG #1 in that file.)
- Parse `json.loads(resp.read().decode("utf-8"))`, take `body.get("data", [])`, collect `obj.get("id", "")` for dict entries, dedupe preserving order, cap at `MAX_MODELS = 500` (a runaway list must not freeze GTK).
- Exceptions to catch (enumerated from `provider_test.py:265-267` + stdlib): `urllib.error.URLError`, `urllib.error.HTTPError` (subclass of URLError — catch it FIRST if separate handling), `TimeoutError`, `socket.timeout`, `OSError`, `json.JSONDecodeError` (subclass of `ValueError`), `ValueError`, `KeyError`, `UnicodeDecodeError` (from `.decode("utf-8")` — NOT in the existing probe's list; add it). Return `ok=False` with `f"{type(e).__name__}: {e}"`-style safe error, NEVER the raw key. For the generic error string, write a LOCAL key-redacting helper (~8 lines: str(e), strip absolute paths, then redact `(sk-[A-Za-z0-9_-]{8,}|Bearer \S+)`-shaped substrings). **Do NOT use `utils.git_ops._safe_error` — measured: it strips paths but does NOT redact keys (`sk-SECRET-123` survives verbatim), and importing it drags GitPython in.** The v2 claim that `_safe_error` prevented credential leaks was false.
- On HTTP 401/403/404 the error string MUST contain the status code so §2.3's translator can classify it: `f"HTTP {e.code}: …"` — mirror `provider_test.py:285-290` (read at most 200 chars of body, `errors="replace"`).
- No logging of keys; no writes; no GTK.

Also: `utils/provider_test.py`'s existing `/v1/models` probe stays UNCHANGED (it discovers `context_window`; this new module discovers IDs). Do not refactor the probe into it — scope discipline (Rule 8).

### 2.3 NEW `utils/provider_errors.py` (~70 lines) — pure string mapping

```python
def explain_test_error(error: str | None, *, preset: ProviderPreset | None = None) -> str: ...
def error_class(error: str | None) -> str: ...
# ("auth" | "url" | "rate" | "network" | "model" | "minimax" | "caller" | "https" | "other")
```

**v2 (fix-round 1 — Debugger #16):** `error_class` is now DECLARED here (v1 referenced it from §2.6(c) without declaring it). ONE classification implementation shared by both functions — the message and the class can never disagree.

Pure function, no I/O. Input is `TestResult.error` (always a string or None — `provider_test.py:55`). Classification table (strings verified in DISCOVERY):

| Substring in `error` (case-insensitive) | Human message |
|---|---|
| `HTTP 401`, `HTTP 403`, `Invalid API key`, `Incorrect API key`, `unauthorized` | "Key rejected — check or regenerate it." (v3: NO URL suffix — the link is the view's LinkButton; a persisted `last_error` must not embed URLs) |
| **MODEL row evaluates BEFORE this row** (fix-round 1 — Debugger #17; OpenAI-compatible APIs return HTTP 404 with body "The model `x` does not exist", and v1's order classified exactly that as a URL error) | (see algorithm note) |
| `HTTP 404` (only after the model row has NOT matched) | "Base URL looks wrong — this endpoint doesn't exist. Expected <preset.base_url>." |
| `HTTP 429` | "Rate limit hit. Wait a minute (free tiers are ~20 req/min) and retry." |
| `timed out`, `timeout`, `urlopen error`, `NameResolutionError`, `Connection refused` | "Couldn't reach the provider — check your network / firewall and the Base URL." |
| **EVALUATED FIRST.** `model` + (`not found` OR `does not exist`), `model_not_found`, `invalid_model`, `unknown model`, `no such model` — case-insensitive substring over the WHOLE error string | "Model ID not accepted. Use 'Load models' to pick a real one, or check the default model." |
| `status_code=` (MiniMax body-level) | "Provider rejected the request: <error>. Check the key and model." |
| `No adapter for provider`, `Cannot auto-detect caller`, `Invalid caller` | "Caller/model mismatch — pick a preset or use a `<vendor>/<model>` model ID." |
| `Provider URL is not HTTPS` | "Base URL must use https:// (except localhost)." |
| anything else / `None` | Return the original `error` (or "Unknown error") — **never swallow information**. |

Rule: the function returns a single display string; the "Get a key" link is rendered by the VIEW as a separate `Gtk.LinkButton` when `preset is not None`, so no HTML/markup is embedded in the string. Unit-test every row with the exact error strings from `provider_test.py` (including `f"HTTP 401: {body}"` and `_safe_error` outputs).

### 2.4 `ui/handlers/settings_handler.py` (405 → ~470 lines) — two new methods

(a) **`test_provider` change: handler-owned persistence for ALL THREE exit paths** (fix-round 1 — supersedes the v1 view-side `persist_result=not self._is_new` design, which Debugger #1 proved permanently disabled the verified-stamp, and Debugger #3 proved missed the two early-return paths):

The v1 design is WITHDRAWN. Two bugs, both adjudicated:
- `_is_new` is assigned once (`settings_dialog.py:83`) and never flipped — not after Save, not by `refresh_providers` (`:747` updates `_provider` in place). A view-side flag derived from it is permanently `False` → preset cards never get `last_verified_at` → the red dot (`left_panel.py:186-188`) never clears.
- The worker has THREE exit paths, not two: the empty-caller early return (`:250-263`) and the invalid-caller early return (`:277`) both route through `_dispatch_test_result` (`:361-405`), which performs its OWN `load_providers()` (`:380`) + `save_providers(providers)` (`:388`) — OUTSIDE the `:296-342` block v1 proposed to skip. On a fresh install the saved list is EMPTY, creating a stray `providers.yaml`.

**v2 design — the handler decides "is this name in the store?", no new parameter at all:**

```python
def test_provider(
    self,
    provider: ProviderConfig,
    on_result: Callable[[TestResult], None],
) -> None:   # signature UNCHANGED from current code
```

Inside `_worker`, persist ONLY when a stored card matches by name — and route all three paths through one helper:

**v3 NOTE (delta #2/#3, both auditors — the v2 sample was WRONG twice):** the v2 sample dropped the context-window pre-fill, the BUG #7 sentinel, and the dual-write (`settings_handler.py:310-328`) — silently failing `tests/test_settings_handler.py:410-425`'s `max_tokens == 500_000` assertion — AND referenced an undefined `preset_for`. **The sample is DELETED.** The normative instruction is extraction, verbatim:

```python
def _persist_test_result(self, provider: "ProviderConfig", result: TestResult) -> None:
    """Stamp the result on the STORED card and save. No-op when no stored
    card matches by name (unsaved card) — never creates an empty
    providers.yaml. Single owner of test-result persistence."""
    providers = load_providers()
    for i, p in enumerate(providers):
        if p.name == provider.name:
            break
    else:
        return  # not stored: nothing to stamp, nothing to save
    # ↓↓↓ THE BODY OF settings_handler.py:296-341 MOVES HERE **UNCHANGED** —
    # including: the context-window discovery pre-fill (result.context_window),
    # the BUG #7 user_has_customized sentinel (:310-313), the SPEC-38 dual-write
    # (:314-328), last_verified_at/last_error stamping (:329-335), and the
    # caller round-trip comments. ONE intentional change ONLY: a failed test
    # stamps the EXPLAINED error (§2.6c) —
    #   last_error=explain_test_error(result.error, preset=self._preset_for(p))
    # where p is the STORED card (base_url match, below). Do not re-type the
    # block from this comment; MOVE the code.
    ...
    save_providers(providers)
```

**`preset_for` is now DECLARED (delta #3):** in `utils/provider_presets.py` —
```python
def preset_for(p: "ProviderConfig") -> ProviderPreset | None:
    """Resolve a card's preset by base_url (NOT name — names are user-editable
    and renamed cards would otherwise lose their preset). None for Custom cards."""
    for preset in PRESETS:
        if p.base_url.rstrip("/") == preset.base_url.rstrip("/"):
            return preset
    return None
```
base_url (not name) is the join key everywhere a card→preset lookup is needed (§2.4c vendor guard, §2.6(c) explanation, §2.6(d) re-prefix gate). The worker's three exit paths each call `_persist_test_result(provider, result)` then the SAME `_dispatch` closure (unchanged). `test_provider`'s public signature stays unchanged.

**`preset_vendor_present` — ONE helper, three sites (delta #5/#7):**
```python
def preset_vendor_present(providers: list["ProviderConfig"], preset: ProviderPreset) -> bool:
    """True when a stored card already serves this vendor — base_url match OR
    exact preset-name match. Used by create_from_env, startup_checks, and the
    env chip. SINGLE definition; all three sites MUST call this, not restate it."""
    return any(
        p.base_url.rstrip("/") == preset.base_url.rstrip("/")
        or p.name == preset.name
        for p in providers
    )
```

**Acceptance additions (§6 updated):** (i) a Test on an UNSAVED card (valid caller) writes NO file and stamps nothing; (ii) a Test on an unsaved card with an INVALID caller also writes NO file (probe all three exit paths with a `save_providers` spy); (iii) after Save, a green Test DOES stamp `last_verified_at` on the stored card and `has_any_verified_provider` flips True (the red-dot criterion).

(b) **NEW `list_models`** — off-thread fetch with the SAME dispatch discipline:

```python
def list_models(
    self,
    base_url: str,
    api_key: str,
    caller: str,
    on_result: Callable[[ModelListResult], None],
) -> None:
    """Fetch /models off the GTK main thread; dispatch via GLib.idle_add.
    Mirrors test_provider's thread + dispatch pattern. Never persists."""
    def _worker():
        try:
            result = fetch_model_ids(base_url, api_key, caller)
        except Exception as e:            # defensive: fetch_model_ids is total, but a thread must not swallow
            result = ModelListResult(ok=False, model_ids=(), error=f"{type(e).__name__}: {e}")
        def _dispatch(result=result):   # fix-round 1 (Debugger #12): bind explicitly — never rely on late closure capture
            try:
                on_result(result)
            except Exception:
                logger.exception("list_models on_result callback raised")
        if self._GLib is not None and hasattr(self._GLib, "idle_add"):
            self._GLib.idle_add(_dispatch)
        else:
            _dispatch()                  # test path: synchronous
    threading.Thread(target=_worker, daemon=True, name="list-models").start()
```

`threading` is already imported (:17); `Callable` (:20); `logger` exists. Add `from utils.model_catalog import ModelListResult, fetch_model_ids` at module scope (utils→utils, no cycle: verified `model_catalog.py` imports only stdlib).

(c) **NEW `create_from_env`** — one-click env card (idea 5):

```python
def create_from_env(self, preset_key: str, api_key: str) -> ProviderConfig | None:
    """Build + save a card from an environment-supplied key. Returns the saved
    ProviderConfig, or None if the preset is unknown / the name is taken.
    All default maintenance stays in save_providers (prefer=...)."""
    preset = get_preset(preset_key)
    if preset is None:
        return None
    providers = load_providers()
    # v2 (fix-round 1 — Debugger #18): guard on the VENDOR, not the name. A
    # renamed card ("my-openai") freed the name "openai" and v1's guard let a
    # second same-vendor card silently appear AND adopt the global default.
    if any(
        (p.base_url == preset.base_url) or (p.name == preset.name)
        for p in providers
    ):
        return None                     # never clobber an existing vendor card
    card = ProviderConfig(
        name=preset.name, base_url=preset.base_url, api_key=api_key.strip(),
        default_model=preset.default_model, caller=preset.caller,
        max_tokens=preset.context_window, context_window=preset.context_window,
    )
    try:
        self.add_or_update(card)        # validation + prefer=card.name + callbacks
    except ValueError:
        # fix-round 1 (Flagger #6): add_or_update's ValueError must not escape
        # into a GTK callback (chip click). Log and report failure via None.
        logger.warning("create_from_env: add_or_update rejected preset %r", preset.key)
        return None
    return card
```

Traced: `add_or_update` (:80-168) validates all four fields, checks caller taxonomy (:143-149), and calls `save_providers(providers, prefer=provider.name)` (:162) → RULING 3 branch (3) adopts this card as global default on a fresh install. **Do NOT call `save_providers` directly here** (single owner). `max_tokens` + `context_window` dual-write matches `_collect_from_form`/SPEC-38 (`settings_handler.py:319-328`; `settings_dialog.py:343-375`). `default_model` stays exactly as the preset catalog says — RULING 2's `_bare_model()` is applied INSIDE `save_providers` when it writes `agent.json` (branch 3, :495-505); the CARD itself holds the catalog value. (Verified: `save_providers` does `set_agent_setting("default_model", _bare_model(chosen.default_model))`; the YAML card keeps its own `default_model`.)

### 2.5 NEW `utils/env_provider_keys.py` (~50 lines) — pure detection

```python
ENV_KEY_MAP: dict[str, str] = {
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENROUTER_API_KEY": "openrouter",
}

def detect_env_keys(env: dict[str, str]) -> list[tuple[str, str, str]]:
    """Return (env_var_name, preset_key, key_value) for present, non-blank vars.
    Presence rule: a variable set to "" is treated as absent (matches
    utils.config.get_env's presence semantics but we deliberately require a
    usable value here). NEVER log or return the value to any UI string — the
    caller puts it directly into ProviderConfig.api_key; the UI shows only
    the env var NAME and the preset label."""
    ...
```

No `os.environ` read inside the function (pass `os.environ` in from the caller for testability — matches `tests/test_settings_dialog.py`'s `tmp_config_dir` pattern). Do NOT use `utils.config.get_env()` for these vendor-standard names: that helper prefixes `DEVELCAKES_`/`CRABCAKES_` (:44-57) and would look for `DEVELCAKES_OPENAI_API_KEY` — the whole point is to honor the industry-standard names users already have. Document that deviation in the module docstring.

**Security invariant (test it):** the detected key VALUE must never appear in the startup warning text, the chip label, any log, or the feed card. Chip shows `OPENAI_API_KEY` (the name) + preset label only.

**v2 (fix-round 1 — Debugger #11): trigger rewritten; v1 collided with the EXISTING warning and asserted a false baseline.** `startup_checks.py:21-22` ALREADY emits "no enabled provider card" on a fresh install — v1's condition ("zero enabled provider cards") would put two contradictory lines in one card, and v1's §6 criterion "clean install emits nothing" was false TODAY. v2 rules:
1. The env line fires when keys are detected AND no stored card matches that key's VENDOR (`caller`+`base_url`, per §2.4c's vendor guard) — independent of the enabled-card count.
2. **v3 (delta #4/#9 — REPLACE rule withdrawn):** the env line is ADDED alongside the generic line, and the generic line stays untouched. Flagger's counter-case: a DISABLED card for vendor X + an env key for vendor Y — the v1 REPLACE rule would delete the only line telling the user their disabled card is the problem (the two conditions are independent: zero-enabled vs missing-vendor). To keep the card coherent, the ENV line itself carries both facts when the store is empty: `f"Found {n} provider key(s) in your environment (e.g. {first_env_name}) — but no provider card is configured yet; open Settings → Providers to add one in a click."` (when at least one card exists, drop the "but no provider card is configured yet" clause).
3. Text: `f"Found {n} provider key(s) in your environment (e.g. {first_env_name}) — open Settings → Providers to add one in a click."` — no values, no URLs.
4. §6 criterion (v3, resolves the :555-vs-§2.5 contradiction Flagger found): *"a clean install with no env keys emits the SAME warnings as today — verified by asserting the returned list contains NO 'Found … provider key(s)' line"* (the literal "emits nothing" is false today and is retired).
5. **v3 (delta #8):** `startup_warnings()` gains an optional parameter — `startup_warnings(env: Mapping[str, str] | None = None)` defaulting to `os.environ` at call time — so the new tests are hermetic (`monkeypatch.setenv`/`delenv` also works, but the parameter makes the dependency explicit). Pure function, still no I/O.
Keep `startup_warnings()` pure; wrap the detection import in `try/except ImportError`.

### 2.6 `ui/views/settings_dialog.py` (1660 → ~1790 lines) — the view

**(a) Preset menu replacing `+ Add Provider`** (:586-597). Change to a `Gtk.MenuButton(label="+ Add Provider")` with a `Gtk.PopoverMenu.new_from_model(Gio.Menu())` — **must** use `Gio.Menu` (verified GTK4.14 assertion trap in `chat_input_toolbar.py:306-315`). Import `from gi.repository import Gio, Gtk` (:22 — currently only `Gtk`). **Action naming (fix-round 1 — Debugger #4, CRITICAL: v1 was self-contradictory and its literal reading produced a dead menu).** The prefix arithmetic: a `Gio.SimpleAction` named `"settings.add-preset.openai"` inside a group inserted as `insert_action_group("settings", …)` resolves to `settings.settings.add-preset.openai` — the menu string never matches and every item is silently inert. The rule, stated once: **group action names carry NO prefix; the prefix lives in `insert_action_group(name, …)` and in the menu string.**

```python
group = Gio.SimpleActionGroup()
for preset in PRESETS:
    action = Gio.SimpleAction.new(f"add-preset.{preset.key}", None)   # NO prefix
    action.connect("activate", lambda _a, _p, k=preset.key: self._on_add_preset_clicked(k))
    group.add_action(action)
custom = Gio.SimpleAction.new("add-custom", None)                     # NO prefix
custom.connect("activate", lambda _a, _p: self._on_add_custom_clicked())
group.add_action(custom)
self._stack.insert_action_group("settings", group)   # prefix lives HERE — on a REAL Gtk.Widget (SettingsDialog is not a widget; Flagger #17)

menu = Gio.Menu()
for preset in PRESETS:
    menu.append(preset.label, f"settings.add-preset.{preset.key}")   # prefix HERE
menu.append("Custom…", "settings.add-custom")
popover = Gtk.PopoverMenu.new_from_model(menu)
popover.set_autohide(True)
self._add_btn = Gtk.MenuButton(label="+ Add Provider")
self._add_btn.set_popover(popover)   # popover resolves the group via widget ancestry (Debugger #20 verified: works in a left-panel-hosted box)
```

The popover's ancestor toplevel is the main window — the Settings pane is hosted in the left-panel notebook (`left_panel.py:154-165`, `window.py:290-295`), so popover positioning holds. **No `Gtk.ListBox`/`Box` children in the PopoverMenu** (GTK 4.14 GTK-CRITICAL trap, `chat_input_toolbar.py:306-315`). §6 gains: *every preset menu item, when activated via `group.lookup_action("add-preset.openai").activate(None)`, creates a card with that preset's name* — the test that catches a dead menu.

Handlers:
```python
def _on_add_preset_clicked(self, key: str) -> None:
    preset = get_preset(key)
    if preset is None:
        return
    card = _ProviderCard(self, None, preset=preset)   # new kwarg, see (c)
    self._cards.append(card)
    self._list_box.append(card.get_widget())
    self._empty_state.set_visible(False)

def _on_add_custom_clicked(self, *args) -> None:
    self._on_add_provider_clicked()   # EXACT old behavior, unchanged
```
Preserve `_on_add_provider_clicked` (:760-766) as the Custom path — existing tests that call it directly or click `_add_btn` must keep working. If any test grabs `self._add_btn` by attribute, keep the attribute name `_add_btn` pointing at the new `Gtk.MenuButton` (verified no test references `_add_btn` via grep of `tests/` — zero hits; still keep the name for safety).

**(b) Preset fields applied in `_ProviderCard`.** Add `preset: ProviderPreset | None = None` keyword to `__init__` (:71-73) and store `self._preset`. At the END of `_build_widgets()` (after :235, before :236) — so all widgets exist — call `self._apply_preset()` when `self._is_new and self._preset is not None`:
- `_is_dirty()` (:310-330) compares the spin AND all four entry texts against `_provider` (:316-324) — see the **EXECUTED TRACE** below for the single-`replace` design that keeps a fresh preset card clean.
- **Caller must land in `_provider`, not just the label:** `_collect_from_form()` (:351) reads `existing.caller`, so `_provider.caller` MUST be set (the `replace(...)` below does it). Without it, an unprefixed preset model (`gpt-4o`, `claude-…`, `gemini-…`) hits `settings_handler.py:113-121`'s `raise ValueError("Cannot auto-detect caller: …")` on Save. `_populate_from_provider()` (:292-293) then renders the read-only label from `p.caller` — no separate `set_text` needed.

**EXECUTED TRACE (2026-11-04, xvfb-run, real `_ProviderCard`) — this replaces the earlier draft's per-widget `set_text` design:**

```
blank new card                                  _is_dirty() == False
set_text on name/url/model/key only            _is_dirty() == True   ← preset card would be dirty before the user typed anything
… + spin 200k, _provider untouched             _is_dirty() == True
```

**Therefore `_apply_preset()` MUST update `_provider` with ALL preset fields in ONE `dataclasses.replace`, then call the existing `_populate_from_provider()` to push them into the widgets.** Verified shape (executed, result below):

```python
from dataclasses import replace          # already imported at settings_dialog.py:15
self._provider = replace(
    self._provider,
    name=preset.name,
    base_url=preset.base_url,
    default_model=preset.default_model,
    caller=preset.caller,
    max_tokens=preset.context_window,
    context_window=preset.context_window,
    # api_key deliberately NOT set — the key is the user's one required input.
)
self._populate_from_provider()           # :284-308 — sets every entry; _populating guard :286-308
```

Executed result: `_is_dirty() == False` immediately after; after `self._api_key_entry.set_text("sk-test")` → `_is_dirty() == True`; `_collect_from_form().caller == "openai"`.

**Sentinel interaction (fix-round 1 — Debugger #8):** seeding `max_tokens` to the preset's window makes `settings_handler.py:310-313`'s `user_has_customized` (`p.max_tokens != 128_000`) TRUE for every preset whose window ≠ 128k (Anthropic 200_000) — so Test Connection will NOT overwrite the window with a discovered value. This is INTENDED (the preset declares the window) — the implementation must not "fix" it, and §6's window criterion must not assume probe-wins behavior. All v1 preset windows are multiples of the spin's 1_000 step. Note `_populate_from_provider()` also sets the caller label from `p.caller` (:292-293) — so the explicit `_caller_label.set_text` above becomes redundant and should be dropped. It sets the spin from `_effective_context_window(p)` (:297), which is `context_window`-first (:249-251) — the dual-write above is what makes that equal the preset value. The `_model_entry` "changed" signal fires during populate but `_populating=True` suppresses `_on_model_changed_reset_reasoning` (:264-267) — verified in source.
- `self._api_key_entry.set_placeholder_text(preset.key_prefix_hint or "API key")` — **placeholder ONLY**, never a value. Call AFTER `_populate_from_provider()` (it sets the entry's TEXT to `p.api_key or ""` at :291; changing the placeholder does not affect `_is_dirty()`, which compares TEXT only at :319).
- **"Get a key" row (fix-round 1 — Debugger #13 scope bug + #21):** `Gtk.LinkButton.new_with_label(preset.api_key_url, f"Get a key at {urlparse(preset.api_key_url).netloc}")` — **LinkButton MANDATED; the `Gtk.UriLauncher` alternative is REMOVED** (async, no assertable `get_uri()`, fails silently without a browser handler). Built inside `_build_widgets()` (v1 said "append to `vbox`" from `_apply_preset()`, but `vbox` is a local of `_build_widgets` — out of scope there): `_build_widgets` constructs the link + the "Load models" button when `self._preset` is set; `_apply_preset()` only sets VALUES (the executed-trace `replace` + `_populate_from_provider`). `from urllib.parse import urlparse` (stdlib).
- Add a "Load models" `Gtk.Button` next to the model row ONLY when `preset.supports_models_list` is True; wire to (d).
- `_preset` must survive `refresh_providers()` — for a saved card the catalog metadata is no longer needed except the key URL; store `self._preset_key` and look up `get_preset()` lazily. Simplest: keep `self._preset` on the card object; `refresh_providers` updates `_provider` in place (:737-753), never replaces the card object for existing names — so the attribute persists. Document this.

**(b2) Unsaved-card survival in `refresh_providers` (fix-round 1 — Debugger #2, CRITICAL: silent loss of the user's typed API key).**

`refresh_providers` (`settings_dialog.py:721-735`) deletes any card whose name is absent from the stored list. A blank Custom card survives only via the `if name:` guard (`:725`). A preset card ALWAYS carries a name — so the moment any other provider is saved (firing `on_providers_changed` → `refresh_providers`), the unsaved preset card is destroyed **along with the API key the user just typed**. Executed probe: `.crabcakes/scratch/spec63/probe_lifecycle.py` P3b (Debugger's).

Required changes, BOTH of them:
1. `_ProviderCard` grows an explicit saved-flag: `self._saved = provider is not None` in `__init__`, and in `_on_save_clicked` set `self._saved = True` **BEFORE calling `add_or_update` — not after** (v3, delta #4/#5, both auditors executed the bug: `add_or_update` fires `on_providers_changed` → `refresh_providers` SYNCHRONOUSLY inside the call (`settings_handler.py:165-166` → `ui/wiring.py:121-126`); a flag set after the call leaves the just-saved card invisible to `current_by_name`, so the loop's else-branch creates a DUPLICATE card and the original object is orphaned forever). If `add_or_update` raises `ValueError`, reset `self._saved = False` in the except branch. Do NOT overload `_is_new` (its one existing read at `:420` is the Remove-button branch for unsaved cards and must keep its current meaning — rename-free).
2. `refresh_providers`'s reconciliation loop only considers saved cards:
```python
current_by_name: dict[str, _ProviderCard] = {}
for card in self._cards:
    name = card._provider.name
    if name and card._saved:      # unsaved cards are NEVER reconciled away
        current_by_name[name] = card
```
§6 gains the criterion: *"saving provider B while an unsaved preset card A (with a typed key) exists leaves A's widgets and text intact after the refresh fires."*

**(c) Error translation.** In `_on_test_clicked` (:392-404), the handler call is UNCHANGED (`test_provider(provider, self._on_test_result)` — fix-round 1 withdrew the v1 `persist_result` parameter; persistence is handler-owned per §2.4a):`
**v2 (fix-round 1 — Debugger #7): translate at the BOUNDARY, not only in the view.** v1's claim "the EXPLAINED message is what the card shows after a refresh" was FALSE — the handler stamps the RAW `result.error` to disk (`settings_handler.py:337-340`) and `refresh_providers` re-derives the label from the STORED `last_error` (`settings_dialog.py:338-339`), so the explained message was transient. v2: `_persist_test_result` (§2.4a) stores the EXPLAINED string — the handler imports `explain_test_error` (utils→ui direction is legal) and stamps `last_error=explain_test_error(result.error, preset=preset_for(provider.name))`. The view then does NOT re-translate on refresh; `_on_test_result` still calls `explain_test_error` only for the immediate label. §6 criterion updated to assert the PERSISTED `last_error` content.
and set the "Get a key" link visible for the `error_class(error) == "auth"` class (helper declared in §2.3). **v3 (delta #11/#12):** the handler (`_persist_test_result`) is the SINGLE writer of `last_error`; the view NO LONGER writes `last_error` into `self._provider` at `:498-501` with a different string than the handler used — its status label is set from the same explained string. And `explain_test_error` returns the MESSAGE WITHOUT any URL (§2.3's auth-row "+ Get a key: <url>" suffix clause is RETIRED) — the URL is rendered only by the view's LinkButton, never embedded in a persisted string.

**(d) Model dropdown (v2 — fully rewritten in fix-round 1: Debugger #5 proved `Gtk.ComboBoxText.set_active()` clobbers the re-prefix and the class is deprecated + un-idiomatic; Flagger flagged `get_active_text()`'s `None` contract).**

Use the house idiom: **`Gtk.DropDown` + `Gtk.StringList`**, with FREE TEXT preserved via the entry that already exists — do NOT use `Gtk.ComboBoxText` (zero uses repo-wide; every dropdown in `ui/` is a `Gtk.DropDown`: `settings_dialog.py:195,1151,1227,1363`; `agent_builder.py:139,279,483,516`; `feed_tab.py:138`). Design:

- The "Load models" button stays (hidden for `supports_models_list=False`, i.e. the Anthropic preset).
- While a list is loaded, the model row shows a `Gtk.DropDown` bound to a `Gtk.StringList` of RAW ids, plus a small "edit manually" toggle that re-hides the dropdown and restores the free-text `Gtk.Entry` with the current value (never blanked).
- **`_current_model_text()` is the ONLY read path** (used by BOTH `_collect_from_form()` and `_is_dirty()`), and it applies the re-prefix rule AT READ TIME — not at write time (v1's write-time rule was destroyed by `set_active()`; Debugger #5's executed probe):

```python
def _current_model_text(self) -> str:
    raw = ""
    if self._model_dropdown.get_visible():
        sel = self._model_dropdown.get_selected_item()
        raw = (sel.get_string() if sel is not None else "")
    else:
        raw = self._model_entry.get_text()
    raw = (raw or "").strip()
    if not raw:
        return ""
    # Re-prefix rule (v3 — DELTA CRITICAL fix, both auditors executed the same
    # failure): OpenRouter /models ids are VENDOR-qualified ("qwen/qwen3-max",
    # "meta-llama/llama-4", "anthropic/claude-x"). The v2 condition (first
    # segment must be a taxonomy caller) failed for every vendor prefix that is
    # NOT a caller: no re-prefix → stored "qwen/qwen3-max" → agent.json
    # "qwen3-max" (RULING 2 chop) → WIRE "qwen3-max" ≠ OpenRouter's required
    # "qwen/qwen3-max", and the bare-prefix key "qwen" gets registered
    # (agent/config.py:227-230) as a phantom provider key. v3: ALWAYS anchor to
    # the card's caller unless already anchored. Gated on the card's BASE_URL,
    # not on self._preset — a Custom card pointed at OpenRouter and a RENAMED
    # preset card (which loses _preset) get the same treatment (delta #7).
    if self._provider.base_url.rstrip("/").endswith("openrouter.ai/api/v1"):
        if raw.split("/", 1)[0] != "openrouter":
            return f"openrouter/{raw}"
    return raw
```

(`get_valid_callers` is importable from `agent.runtime` in the view — `settings_handler.py:23` already imports it.)

- On list arrival (main thread): populate the StringList with raw ids; if the current `_current_model_text()` equals some id (after stripping this card's own re-prefix), select it; else select nothing and keep the free-text entry visible with the user's value.
- Button state machine (Debugger #12): disabled from click until the callback fires, re-enabled in BOTH ok and error paths; a monotonically increasing per-card request token discards stale results (a late return from a superseded click must not overwrite a newer list).
- `changed`-signal wiring for the reasoning reset (`_on_model_changed_reset_reasoning`, `:264-271`) connects to the DropDown's `notify::selected` too — the `_populating` guard (`:266-267`) already suppresses programmatic changes.

§6 criteria updated (see the OpenRouter rows there — v3 adds the `qwen/qwen3-max` failing-branch case).

**Dropdown/entry authority (v3 — delta #8: v2 left the entry and the dropdown holding different strings, so `_is_dirty()` read permanently dirty after a refresh because `_populate_from_provider()` writes the PREFIXED model into the entry while the dropdown holds the RAW id):** the DROPDOWN is display-only; the ENTRY is the single stored-value authority. When a list arrives and a selection is made, write the RAW id into the entry (`self._model_entry.set_text(raw)`) — the prefix exists ONLY at `_collect_from_form()` time via `_current_model_text()`. `_populate_from_provider()` continues to write the card's stored model into the entry (prefixed form — that is what lives in providers.yaml); the dropdown, when visible, re-selects the matching RAW id if present, else selects nothing and defers to the entry. This keeps both widgets consistent and the card non-dirty after refresh.

**(e) Env chip.** In `SettingsDialog.__init__` (after the Providers page is built, near :599), call `detect_env_keys(dict(os.environ))` and, for each hit where **`preset_vendor_present(load_providers(), preset)` is False** (v3, delta #5/#7 — the ONE helper from §2.4a; v2's name-only text here was the incomplete edit that let a renamed `my-openai` card resurrect the chip, and clicking it would silently no-op because `create_from_env`'s vendor guard refuses), append a chip row above the `_list_box`:
`Gtk.Button(label=f"Add {preset.label} from {env_name} (found in environment)")`, wired to:
```python
self._handler.create_from_env(preset.key, key_value)
```
The `key_value` lives only as a closure argument — never in widget text/labels/logs. `on_providers_changed` wiring (`ui/wiring.py:104-122`) calls `refresh_providers()`, so ALSO hide/remove chips whose preset name now exists — implement `refresh_providers()` (:693) to call a new `_sync_env_chips(providers)` at its end (:756). `import os` at module scope. Tests patch `os.environ` (or pass a dict) — construct with `monkeypatch.setenv`.

**(f) CSS** — `ui/styles.py` add 3 classes near `.settings-provider-card` (:1169): `.settings-preset-link` (dim, small), `.settings-env-chip` (suggested-action variant), `.settings-model-row` (horizontal box spacing). No new keyframes, no structural CSS changes.

### 2.7 Files NOT changed (Rule 8)

- `utils/providers_store.py` — `save_providers()` is already the single owner (SPEC-62 fix-round 4). NO new default-maintenance logic anywhere.
- `utils/provider_test.py` — `test_connection` and its `/v1/models` context-window probe stay byte-identical; error strings are CONSUMED by the translator, not changed.
- `agent/config.py`, `models/providers.py` — no schema/taxonomy/model-composition changes. The preset catalog conforms to the existing taxonomy.
- `agent/runtime.py` — **ONE pre-req fix moves it out of the not-changed list (v3, delta #1b — executed fix-verification, 6 card shapes):** `create_conversation`'s model resolution at `:1269` sets `conv.model = self._config.default_model` (the BARE agent.json value). For any card whose stored `default_model` carries a prefix (OpenRouter picks, the google preset), `_match_provider_cfg_by_model` then finds no card → `ValueError("Provider 'X' is not configured")`. Fix (the only one of three measured candidates that passes all 6 shapes — the naive `f"{dp}/{dm}"` regresses 3 of 6 via the duplicate-objects `len(matches)==1` guard at `:1796`):
```python
# agent/runtime.py:1268-1269 (create_conversation)
if model is None:
    card = self._config.providers.get(self._config.default_provider)
    dm = getattr(card, "default_model", "") if card is not None else ""
    model = dm or self._config.default_model
```
This makes `:1269` consistent with `:5319` (which already reads the card's full value). `:5379` (`_call_for_summary`) composes from the bare agent.json value and is CORRECT as-is — change nothing there. **This fix lands FIRST, in its own commit, with a regression test covering the 6 measured shapes (openrouter/`openrouter/qwen/qwen3-max`, openrouter/`openrouter/anthropic/claude-x`, openrouter/`openrouter/auto`, openai/`gpt-4o`, google/`openai/gemini-2.0-flash`, anthropic/`claude-3-5-sonnet-20241022`) BEFORE Wave A.**
- `ui/window.py`, `ui/wiring.py` — existing `SettingsHandler(GLib_module=GLib, …)` and `wire_settings_handler` need no changes; `create_from_env` fires the existing `on_providers_changed` callback via `add_or_update`.
- `utils/agent_defs.py`, `prompts/default_agents/*` — SPEC-62 fresh-install seeding untouched.
- `docs/specs/SPEC-62-*` — historical, untouched.
- `knowledge/setup.md`, `knowledge/providers.md`, `knowledge/configuration.md` — see §8; doc updates are separate from code but listed there.

---

## 3. Data Flow

**Preset path:**
`user clicks + Add Provider → Gio.Menu action "settings.add-preset.openai" → SettingsDialog._on_add_preset_clicked("openai") → get_preset("openai") → _ProviderCard(self, None, preset=…) → _build_widgets() → _apply_preset() sets name/base_url/model/caller/context + shows "Get a key" link + "Load models" → user pastes key → clicks Test Connection → _on_test_clicked → _collect_from_form() → ProviderConfig(name="openai", base_url="https://api.openai.com/v1", api_key="sk-…", default_model="gpt-4o", caller="openai", max_tokens=128000, context_window=128000) → handler.test_provider(provider, self._on_test_result) → daemon thread → _persist_test_result finds no stored match → no write → test_connection(...) → _test_openai_compat POST /chat/completions → TestResult(ok=False, error="HTTP 401: …") → GLib.idle_add(_dispatch) → _on_test_result → explain_test_error → "Key rejected — check or regenerate it." + visible Get-a-key link → user fixes key → Test again → ok=True → user clicks Save → _on_save_clicked → handler.add_or_update(card) → validation passes (caller present, no prefix in model) → save_providers(providers, prefer="openai") → RULING 3 branch (3) adopts `default_provider="openai"`, `default_model=_bare_model("gpt-4o")="gpt-4o"` in agent.json → on_providers_changed → refresh_providers() + agent builder dropdown + runtime refresh (all existing wiring).`

**Model-list path:** `Load models → handler.list_models(base_url, api_key, caller, cb) → daemon thread → utils.model_catalog.fetch_model_ids() → local opener GET <base_url>/models (Authorization: Bearer) → parse data[].id → ModelListResult → GLib.idle_add → _on_model_list_result → Gtk.DropDown (StringList) populated with raw ids → user picks → _current_model_text() feeds BOTH _collect_from_form() and _is_dirty() → Save as above.`

**Env path (fresh install):** `app start → startup_warnings() → detect_env_keys(dict(os.environ)) → one warning line (names only) → _emit_startup_check_card → feed card. User opens Settings → Providers → SettingsDialog.__init__ → detect_env_keys → chip "Add OpenAI from OPENAI_API_KEY" → click → handler.create_from_env("openai", value) → load_providers() guard (no clobber) → ProviderConfig(…) → add_or_update(card) → save_providers(prefer="openai") → on_providers_changed → refresh_providers() creates a real card + _sync_env_chips() removes the chip.`

**Threading:** no network call on the GTK main thread; both worker methods use `_GLib.idle_add` when wired (`ui/window.py:279`), synchronous dispatch in tests (`SettingsHandler()` default `_GLib=None`, `settings_handler.py:53,58,353-356`). `startup_warnings()` is pure/synchronous but performs NO network — only `os.environ` + `load_providers()`.

---

## 4. File Change Summary

| File | Change | Est. lines | Risk |
|------|--------|-----------|------|
| `utils/provider_presets.py` | NEW catalog | +120 | Low (static data) |
| `utils/model_catalog.py` | NEW `/models` fetch | +110 | **Medium** (network + opener discipline + exception coverage) |
| `utils/provider_errors.py` | NEW translator | +70 | Low (pure) |
| `utils/env_provider_keys.py` | NEW detection | +50 | Low (pure) |
| `utils/startup_checks.py` | +1 warning line | +12 | Low (pure) |
| `ui/handlers/settings_handler.py` | `_persist_test_result` extraction (all 3 paths), `list_models`, `create_from_env` | +75 | **Medium** (refactor of live stamp logic; behavior byte-identical for stored cards) |
| `ui/views/settings_dialog.py` | MenuButton/Gio.Menu, preset apply, translator, combo, env chips | +130 | **High** (GTK4 popover trap, `_is_dirty` symmetry, `_collect_from_form` dual read) |
| `ui/styles.py` | 3 CSS classes | +15 | Low |
| `tests/test_provider_presets.py` | NEW | +90 | — |
| `tests/test_model_catalog.py` | NEW (urlopen patch) | +130 | — |
| `tests/test_provider_errors.py` | NEW | +90 | — |
| `tests/test_env_provider_keys.py` | NEW | +70 | — |
| `tests/test_settings_dialog.py` | preset/chip/combo tests | +140 | — |
| `tests/test_settings_handler.py` | persist-helper all-3-paths spy tests, list_models, create_from_env vendor-guard | +140 | — |
| `tests/test_startup_checks.py` | env warning line | +40 | — |
| `docs/ARCHITECTURE.md` | new §3.11e | +25 | — |
| `knowledge/setup.md`, `knowledge/providers.md` | no-YAML setup docs | +30/−15 | — |

Total ≈ +1,180 / −15 across 17 files. Implementation should be split into 3 waves (see §5) — do not land in one commit.

---

## 5. Implementation Order (per-wave verification)

1. **Wave A — pure utils + tests.** `provider_presets.py`, `provider_errors.py`, `env_provider_keys.py`, `model_catalog.py`, `startup_checks.py` + their 4 new test files. Verify: `.venv/bin/python -m pytest tests/test_provider_presets.py tests/test_provider_errors.py tests/test_env_provider_keys.py tests/test_model_catalog.py tests/test_startup_checks.py -q`. Zero GTK, zero network in CI (patch `urlopen`). Verify the preset caller/model invariant test passes for all 4 presets.
2. **Wave B — handler.** `settings_handler.py` `_persist_test_result` extraction (all three exit paths), `list_models`, `create_from_env` + tests. Verify: `.venv/bin/python -m pytest tests/test_settings_handler.py tests/test_settings_dialog.py -q` — every pre-existing test must still pass (extraction, not rewrite); assert via spy that an unsaved-card test writes NOTHING on all three exit paths and a saved-card test DOES stamp. Verify: `.venv/bin/python -m pytest tests/test_settings_handler.py tests/test_settings_dialog.py -q` — **every pre-existing test must still pass** (extraction, not rewrite; `test_provider`'s signature is UNCHANGED). Assert via spy: an unsaved-card test writes NOTHING on all three exit paths; a saved-card test DOES stamp.
3. **Wave C — view.** `settings_dialog.py`, `styles.py` + tests (requires `xvfb-run -a`). Verify: `xvfb-run -a .venv/bin/python -m pytest tests/test_settings_dialog.py tests/test_agent_builder_dialog.py -q` and a manual smoke: open Settings → Providers, click `+ Add Provider` → OpenAI, confirm the popover opens with NO GTK-CRITICAL in stderr, fields are pre-filled, Test Connection on an unsaved card does not create `providers.yaml`, Save adopts the default (`get_agent_setting("default_provider") == "openai"`).
4. **Wave D — docs + full regression.** §8 updates, then `.venv/bin/python -m pytest tests/ -q` (or the project's standard full-suite command) with the known pre-existing failures documented; ruff parity check per changed file vs HEAD (the same per-file `ruff check` diff used for SPEC-62 fix-round 4). Do not declare done on a summary — paste actual output (steelFramed Rule 10).

---

## 6. Acceptance Criteria

- [ ] `+ Add Provider` opens a popover with exactly **4** vendor presets (fix-round 1: MiniMax + Z.ai deferred) + `Custom…`; `Custom…` reproduces the old blank card byte-for-byte in behavior.
- [ ] **Menu activation via the RESOLUTION path (v3 — delta #10/#9, both auditors: `group.lookup_action(...)` bypasses GTK name resolution and cannot detect the dead-menu bug it was written for):** `button.activate_action("settings.add-preset.openai", None)` returns True AND fires the handler AND the menu model's item action attribute equals `"settings.add-preset.openai"`. (`lookup_action` may be asserted as a secondary check only.)
- [ ] **[xvfb]** Red dot (fix-round 1 — Debugger #1):** after Save + a green Test, `last_verified_at` is stamped on the stored card and `has_any_verified_provider` is True — for a card created from a preset.
- [ ] **Unsaved survival (fix-round 1 — Debugger #2):** saving provider B while an unsaved preset card A (typed key present) exists leaves A intact after `refresh_providers` fires.
- [ ] **No-write on unsaved Test, all three exit paths (v3 wording — delta #8: `SettingsHandler.__init__` CREATES an empty `providers.yaml` via `ensure_providers_yaml_exists` (`settings_handler.py:68-72` → `agent/config.py:477-495`), so "no providers.yaml exists" is FALSE whenever a handler was constructed):** a `save_providers` spy stays uncalled for valid-caller, empty-caller, and invalid-caller tests on an unsaved card, and the `providers.yaml` CONTENT is byte-identical before/after (assert on content, not existence).
- [ ] **Persisted `last_error` is the EXPLAINED string (fix-round 1 — Debugger #7):** after a 401 Test on a SAVED preset card, `providers.yaml`'s `last_error` contains "Key rejected", and a `refresh_providers` keeps showing it.
- [ ] **OpenRouter nested ids — the FAILING branch from v2 (v3, delta #1):** picking `qwen/qwen3-max` (vendor prefix NOT a taxonomy caller) yields stored `default_model == "openrouter/qwen/qwen3-max"`, stored `caller == "openrouter"`, `agent.json` `default_model == "qwen/qwen3-max"`, and WIRE model `qwen/qwen3-max` (assert the payload sent to a mocked urlopen). The v2 criterion (`anthropic/claude-x`) exercised only the already-working branch — it is RETAINED as a second case but is no longer sufficient alone. Also assert NO phantom bare-prefix key: `load_agent_config().providers` contains no `"qwen"` key.
- [ ] **conv.model resolution (v3, delta #1b):** after saving the OpenRouter preset with a nested id, `create_conversation(model=None)` yields `conv.model` that resolves to the OpenRouter card (no `"Provider 'qwen' is not configured"`).
- [ ] Choosing a preset pre-fills Name, Base URL, Default Model, Caller (label AND `_collect_from_form()` result), and Context Window; a fresh preset card is NOT dirty before the user types (`_is_dirty() is False`).
- [ ] With only an API key pasted, Test Connection runs on the unsaved card WITHOUT writing `providers.yaml` and WITHOUT stamping `last_verified_at` on any other card; a 401 shows "Key rejected — check or regenerate it." plus a working "Get a key" link.
- [ ] All error classes in §2.3 map to a human message; unknown errors pass through unchanged. Every row unit-tested against the EXACT error strings `utils/provider_test.py` produces.
- [ ] "Load models" fetches off-thread, populates an editable combo, never blanks the current model text, and the picked value reaches `_collect_from_form()` AND `_is_dirty()`.
- [ ] Anthropic preset has NO "Load models" button; other presets do.
- [ ] `fetch_model_ids` uses a LOCAL opener (never `install_opener`), strips auth on cross-host redirects, caps at 500 IDs, and catches every exception in §2.2 (test each).
- [ ] Env chip appears only when a mapped var is set AND no card with that preset name exists; chip label shows the VAR NAME + preset label, never the value; one click creates a saved card via `add_or_update` (prefer path) and the chip disappears after `refresh_providers()`.
- [ ] `startup_warnings()` emits exactly one informational line with no key values; a clean install with no env keys emits nothing (existing behavior preserved).
- [ ] No new `save_providers` call site outside `add_or_update` for card creation; `agent.json`'s `default_model` stays BARE after a preset save. **Fix-round 1 specifics (Debugger #23): the OpenRouter preset (`openrouter/auto`) and the google preset (`openai/gemini-2.0-flash`) are the ONLY prefixed rows — they alone exercise `_bare_model`; assert `agent.json` holds `default_model == "auto"` / `== "gemini-2.0-flash"` respectively after their saves.** (RULING 2 regression: `.venv/bin/python -m pytest tests/test_config_invalidation.py tests/test_spec62_fresh_install.py -q`).
- [ ] No `agent/*` or `gi` import at module scope in any new `utils/*` module (enforced by a test that reads the module source or inspects `sys.modules` after import).
- [ ] Full pre-existing suites `tests/test_settings_dialog.py`, `tests/test_settings_handler.py`, `tests/test_provider_test.py`, `tests/test_providers_store.py`, `tests/test_config_invalidation.py`, `tests/test_spec62_fresh_install.py` pass unchanged.

---

## 7. Edge Cases

| Case | Expected behavior |
|------|-------------------|
| User picks preset, then hand-edits Base URL / model | All fields stay editable; no re-application of preset on refresh. |
| RENAMED preset card (v3 — delta #7, executed: rename spawns a NEW card object with `_preset` LOST) | The card keeps a resolvable preset via base_url (`preset_for(p)` matches on base_url, not name): Get-a-key link and the re-prefix rule survive a rename. A genuinely Custom card (base_url not in catalog) gets no link and no re-prefix — correct, it is not a catalog vendor. |
| Preset name collides with an existing card | Card is created unsaved with the same name; Save replaces that card (`add_or_update` :151-154 semantics). Show a warning label "A card named X already exists — Save will replace it." |
| `OPENAI_API_KEY=""` (present but empty) | Treated as absent; no chip, no warning. |
| `DEVELCAKES_OPENAI_API_KEY` set, standard name absent | NOT detected (documented deviation); user uses Custom or pastes key. |
| Env key found but a card for that VENDOR exists — any name; matched by `base_url` or exact name | No chip; no clobber. Fix-round 1 — Debugger #18: v1's name-only guard let a rename resurrect the chip and create a duplicate same-vendor card that silently adopted the global default. |
| `/models` returns 401 | Free-text entry stays, explained auth message + Get-a-key link, button re-enabled. |
| `/models` returns non-JSON / huge list | Non-fatal failure; cap 500; no crash; `UnicodeDecodeError` handled. |
| OpenRouter `/models` ID contains `/` (e.g. `anthropic/claude-x`) | `_current_model_text()` re-prefixes AT READ TIME (fix-round 1 — the v1 write-time rule was clobbered by `set_active()`); stored model `openrouter/anthropic/claude-x`, wire model `anthropic/claude-x`, caller stays `openrouter`. |
| Anthropic `/models` attempted programmatically | `fetch_model_ids` returns `ok=False` with "does not expose" message; no request made. |
| `Gtk.PopoverMenu` built with Box children | Forbidden by spec — `Gio.Menu` only (GTK 4.14 GTK-CRITICAL, verified). |
| Test Connection on a new card while another card is dirty | Only the target card's fields are collected; `refresh_providers()` leaves dirty cards alone (:741-744). |
| Z.ai preset | **DEFERRED out of v1** (fix-round 1): no verified model ID exists in-repo (`glm-5.1` per the proposal vs `glm-4.6` invented by v1); re-add only after live-key verification. |
| `Gtk.LinkButton` in a headless test | No actual launch; assert `get_uri()` equals `preset.api_key_url` — LinkButton is MANDATED, no UriLauncher alternative (Debugger #21). |
| Preset card saved, then `refresh_providers()` runs | Card matched by name (:738-748); `_preset` attribute persists on the object; Get-a-key link remains. |
| User picks a model from the combo then edits it to a typo | Combo has an entry; `_current_model_text()` returns the edited text; Save fails with the explained "Model ID not accepted" on the next Test. |

---

## 8. ARCHITECTURE.md / docs Updates Required

- `docs/ARCHITECTURE.md` — add **§3.11e "Provider setup helpers (SPEC-63)"** immediately after §3.11b (:829+): list the 4 new utils modules, their public APIs, the "static catalog, offline-first" invariant, the LOCAL-opener discipline for `/models`, the handler-owned no-write test path (unsaved cards never touch the store), and the explicit statement that `save_providers()` remains the single owner of global-default maintenance (cross-ref SPEC-62 fix-round 4 RULING 3). Update the §3.11a line "Used by Settings dialog Test Connection button and the auxilium wizard" to mention that error strings are now consumed by `utils/provider_errors.explain_test_error` — without changing `test_connection` itself.
- `knowledge/setup.md` — replace §"How do I configure providers?" (:16-29): delete the `nano ~/.config/crabcakes/providers.yaml` instruction as the PRIMARY path; document Settings → Providers → preset → paste key → Test → Save, plus the env-var one-click and the Custom escape hatch. Keep the YAML path as an advanced note.
- `knowledge/providers.md` — add a short "Presets" table at the top (name → base_url → key page) sourced from `utils/provider_presets.py`; correct the MiniMax `.chat` → `.io` discrepancy between `knowledge/configuration.md:505` and the proposal (flag, don't silently rewrite history).
- `docs/CONVENTIONS.md` — no change needed (no new convention; duplication invariant already documented).
- No `README.md` change required for a UI-only setup aid (last full rework at `6167b1c9`); PM may request one later.

---

## SPEC SELF-AUDIT (steelFramedSpecWriter Rule 9)

1. **Does every code sample work against the current codebase?** Verified: `ProviderConfig(...)` kwargs against `inspect.signature`; `save_providers(providers, prefer=…)` against :364-366 + :162; `test_connection` kwargs against :80-86; `TestResult` fields against :50-57; `add_or_update` validation branches against :90-149; `_GLib.idle_add` dispatch against :353-356; `_is_dirty`/`_effective_context_window` against :239-251,:310-330; `_collect_from_form` against :343-375; `Gio.Menu` popover against `chat_input_toolbar.py:300-315`. The one deliberately-conditional literal (`glm-4.6`) is FLAGGED as unverified with an explicit fallback. No invented function names: all NEW APIs are declared in this spec as new.
2. **Exception types** — enumerated from `utils/provider_test.py:265-267` plus `UnicodeDecodeError`; `HTTPError` is a subclass of `URLError` (caught first); `_safe_error` from `utils/git_ops` (already imported at `provider_test.py:22`). `SettingsHandler` workers catch `Exception` defensively (daemon thread must not swallow silently — logs via `logger.exception`). `startup_warnings` wraps its new import in `try/except ImportError`.
3. **Key structures** — `ProviderConfig` is a dataclass (not a dict); primary registration traced at `agent/config.py:214-231` (corrected in fix-round 1 — v1 wrongly cited the failover path `runtime.py:3355-3362`); `models_body["data"][].id` traced at `provider_test.py:248-263`; `get_agent_setting("default_provider")`/`save_providers` branch keys traced at `providers_store.py:449-520`. No assumed keys.
4. **Data flow end-to-end** — §3 traces all three paths through real function names, including the name-in-store persistence guard and the exact `GLib.idle_add` dispatch.
5. **Would an implementer produce working code?** Yes per wave order. Four traps are called out with citations, and the most subtle one was **executed, not just reasoned**: (a) preset caller MUST land in `_collect_from_form`'s `existing.caller` or unprefixed models raise at `settings_handler.py:113-121`; (b) a fresh preset card MUST update `_provider` with ALL fields in one `replace` then call `_populate_from_provider()` — the §2.6(b) EXECUTED TRACE proves per-widget `set_text` leaves `_is_dirty() == True` (an earlier draft of this spec got this wrong and was corrected); (c) the `/models` opener must be LOCAL (audit BUG #1); (d) nested-slash OpenRouter IDs need the one-place re-prefix rule.
6. **Deviations from the original chat proposal, documented:** "test-before-save" already exists (`_on_test_clicked` tests the form without saving) — idea 2 therefore becomes error translation + no-write guarantee for unsaved cards, NOT a new test-before-save feature. Idea 5's `${VAR}` indirection is explicitly out of scope. Idea 6 deferred by PM.
7. **Files not changed** — enumerated in §2.7 with reasons.

**Mantra:** a spec is a contract. Fix-round 1 resolved both v1-unverified literals by DEFERRAL rather than guess: MiniMax (host split between the two wire paths) and Z.ai (no verifiable model ID) are out of v1. The remaining live-verification item is Gemini's `/models` on the `/v1beta/openai` path (fallback: `supports_models_list=False`).

---

## 9. FIX-ROUND 1 ADJUDICATION RECORD (2026-11-11)

**Audits:** Debugger `.crabcakes/debugger_audit_spec63.md` (23 findings: 6C/6H/7M/4L, 11 executable probes) + Flagger `.crabcakes/flagger_audit_spec63.md` (34 findings: 11C/12H/8M/3L). **Substrate note:** Flagger's worktree was one accept behind (`a23a0d5b`, stale fix-round-3 remnants blocking ff-merge) — synced to `bd472833` post-audit; their substrate-dependent findings (#1, #7, parts of #8) were re-derived against the live tree by Supervisor and either confirmed as tree-independent, re-attributed, or dropped. Both auditors' tree-independent findings were adjudicated individually against source by Supervisor (not accepted on report faith): the six Debugger CRITICALs all reproduced (`_is_new` grep, `_dispatch_test_result`'s own save, `startup_checks.py:20-22`, `agent/config.py:214-231` vs the failover citation).

| Finding(s) | Ruling | Spec change |
|---|---|---|
| Debugger #1 (`_is_new` never flips) + #3 (3 exit paths) + Flagger #6/#12 | CONFIRMED — handler-owned `_persist_test_result`, no new parameter, no view flag | §2.4a rewritten |
| Debugger #2 (refresh deletes unsaved card + typed key) | CONFIRMED — explicit `_saved` flag + reconciliation guard | §2.6(b2) new |
| Debugger #4 (dead menu arithmetic) + Flagger #17 (action group on a real widget) | CONFIRMED — no-prefix group names; group on `_stack`; activation test | §2.6(a) rewritten |
| Debugger #5 (set_active clobber; ComboBoxText deprecated/unidiomatic) + Flagger #3 (None contract) | CONFIRMED — `Gtk.DropDown` house idiom; re-prefix at READ time in `_current_model_text()` | §2.6(d) rewritten |
| Debugger #6 (wrong registration citation) | CONFIRMED — `agent/config.py:214-231` primary; `runtime.py:3355-3362` is failover | Discovery + audit-item 3 corrected |
| Debugger #7 (raw last_error vs explained label) | CONFIRMED — translate at the boundary; persisted `last_error` is the explained string | §2.6(c) + §6 |
| Debugger #8 (user_has_customized sentinel) | CONFIRMED as INTENDED behavior — documented, not changed | §2.6(b) note |
| Debugger #9 (`glm-4.6` invented) + #10 (MiniMax by fiat) + #22 (homepage links) + Flagger #4 (host split, runtime-vs-test endpoints) | CONFIRMED — MiniMax + Z.ai REMOVED from v1; live-key verification is the re-entry gate | §2.1 ruling |
| Debugger #11 (startup collision + false baseline) | CONFIRMED — vendor-scoped trigger; env line REPLACES the generic line; §6 re-worded | §2.5 |
| Debugger #12 (closure + re-entrancy) | CONFIRMED — explicit `result=result` bind; button state machine + stale-token discard | §2.4(b)/(d) |
| Debugger #13 (vbox scope) + #21 (UriLauncher trap) | CONFIRMED — widgets in `_build_widgets`, values in `_apply_preset`; LinkButton mandated | §2.6(b) |
| Debugger #14 (`_safe_error` drags GitPython; private cross-module import) | CONFIRMED — `model_catalog.py` defines its own handler + local safe-error; NO `utils.git_ops` import; regression test keeps the two handlers behaviorally identical | §2.2 amended below* |
| Debugger #15 (taxonomy duplication unenforced) + Flagger #14 (third copy) | CONFIRMED — NO copy in the module; test-side `TestPresetCallerInvariant` against `agent.runtime.get_valid_callers()`; prefix invariant unconditional | §2.1 note |
| Debugger #16 (error_class undeclared) | CONFIRMED — declared in §2.3 with the shared-classification rule | §2.3 |
| Debugger #17 (404-with-model-body misclassified) | CONFIRMED — model row evaluates FIRST; extended matchers; algorithm stated | §2.3 |
| Debugger #18 (name guard → duplicate on rename) | CONFIRMED — vendor guard (`base_url` OR name) in BOTH `create_from_env` and the chip | §2.4(c), §2.6(e), §7 |
| Debugger #19 (auxilium dead) + Flagger (§3.11c taken) | CONFIRMED — §3.11e; drop the auxilium clause | §8 |
| Debugger #20 (left-panel popover) | NOT A DEFECT — one ancestry sentence added | §2.6(a) |
| Debugger #23 (bare-model criterion vacuous) | CONFIRMED — OpenRouter + google rows named explicitly | §6 |
| Flagger #2 (Gemini registry KeyError) | **REFUTED on the live tree** — executed check: `_resolve_caller_key` returns `openai` (stored caller wins, `runtime.py:3053-3058`); Debugger's independent wire-path proof agrees. The risk direction was real, so the google row is STILL prefixed (defense in depth) | §2.1 correction |
| Flagger #1/#7 (substrate absent; self-audit false attestation) | ARTIFACT of the stale worktree — substrate present at `bd472833`; the specific `:477` line they quote does verify on the live tree. Process fix: worktree sync is now part of the dispatch checklist | this §9 |
| Flagger #8 (test_provider fresh-install ValueError, pre-existing) | CONFIRMED as PRE-EXISTING (not this spec's bug) — noted; §2.4a's refactor naturally routes all paths through the name-in-store check, which sidesteps the empty-save case too | §2.4a |

*§2.2 amendment: `model_catalog.py` imports ONLY stdlib + `utils.provider_test` is NOT used for the handler — define a local `_NoAuthRedirectHandler` (behavior-identical; regression test compares both classes' redirect behavior) and a local ~8-line safe-error; do NOT import `utils.git_ops` (it pulls GitPython at module scope, `git_ops.py:9`).

**Open follow-ups (not blockers):** (1) live-key verification for MiniMax host + Z.ai model ID, then re-admit as catalog rows; (2) Gemini `/models` reachability on `/v1beta/openai`; (3) `knowledge/configuration.md:505` `.chat`-vs-`.io` correction rides with the MiniMax re-admission, not before.

---

## Open questions for the PM (not blockers for Waves A–B)

1. Include the **Google Gemini** preset? It uses `caller: openai` against a vendor-specific `/openai/` path — verified in `knowledge/providers.md:331`, but I have not verified the `/models` endpoint exists on that path. If unavailable, set `supports_models_list=False` for it.
2. Include **Z.ai** in v1 given the unverified `glm-4.6` ID? Default recommendation: ship 5 presets, add Z.ai in a follow-up once a real key confirms a model ID.
3. Should the env chip also appear when providers ALREADY exist (for a second provider), or only on a fresh install? Spec currently says only when no card with that preset NAME exists (which allows a second provider) — slightly broader than "fresh install". Adjustable.


---

## 9.2 FIX-ROUND 2 (DELTA) ADJUDICATION RECORD (2026-11-11)

**Delta audits:** Debugger `.crabcakes/debugger_audit_spec63_delta.md` (12 findings: 1C/4H/4M/3L, 17 probes, all executed) + Flagger `.crabcakes/flagger_audit_spec63_delta.md` (14 findings: 4C/4H/4M/2L, source-derived — no exec in that session). Baselines verified green by both Supervisor and Debugger: 149 passed/56 skipped + 63 passed.

| Delta finding | Ruling | v3 change |
|---|---|---|
| D#1/FLG-D1 re-prefix inverted (Supervisor-flagged in the brief, confirmed by BOTH with executed traces: wire `qwen3-max` ≠ required `qwen/qwen3-max`; phantom bare-prefix key `qwen`; §6 tested only the working branch) | CONFIRMED — rule is always-anchor, gated on base_url not `_preset` (covers Custom + renamed cards, delta #7) | §2.6(d) rewritten |
| D#1b `runtime.py:1269` bare-model → "Provider not configured" (Debugger-only; 6-shape fix matrix: card-own-default_model passes 6/6, naive composition regresses 3/6) | CONFIRMED — pre-req fix in its OWN commit before Wave A, with the 6-shape regression test | §2.7 runtime entry |
| D#2/FLG-D2 `_persist_test_result` sample dropped context-window pre-fill + sentinel + dual-write (breaks `test_settings_handler.py:410-425`) | CONFIRMED — sample DELETED; instruction is verbatim MOVE of `:296-341` with exactly one intentional change (explained `last_error`) | §2.4a rewritten |
| D#3/FLG-D3 `preset_for` undefined; name-based lookup wrong for renamed cards | CONFIRMED — declared in §2.1, base_url-joined | §2.1 |
| D#4/FLG-D5 `_saved` after `add_or_update` → synchronous refresh creates DUPLICATE card (both executed; one-word fix verified: before + reset-on-ValueError) | CONFIRMED | §2.6(b2) |
| D#5/FLG-D7 chip guard name-only; three different predicates across §2.4c/§2.5/§2.6e | CONFIRMED — ONE helper `preset_vendor_present` at all three sites | §2.4a/§2.5/§2.6(e) |
| D#6/FLG-D4 startup REPLACE rule deletes the disabled-card warning; independent conditions | CONFIRMED — ADD + env line carries both facts; env parameter for hermetic tests; "emits nothing" criterion retired | §2.5 |
| D#7 re-prefix gated on `_preset` (Custom/renamed keep the bug) | CONFIRMED — base_url gate | §2.6(d) |
| D#8 entry/dropdown divergence → permanently dirty | CONFIRMED — entry is the single authority; dropdown display-only | §2.6(d) |
| D#9/FLG-D9 `lookup_action` bypasses name resolution (criterion can't catch the dead menu) | CONFIRMED — criterion now `button.activate_action(...)` + menu-model attribute; `lookup_action` secondary | §6 |
| D#10/FLG-D8 `ensure_providers_yaml_exists` makes "no providers.yaml" criteria false; xvfb-only criteria unmarked | CONFIRMED (Supervisor verified `settings_handler.py:68-72`) — content-identity assertions; `[xvfb]` markers | §6 |
| D#11 two `last_error` writers (raw vs explained) | CONFIRMED — handler is the single writer | §2.6(c) |
| D#12 URL embedded in persisted `last_error` + LinkButton duplicate | CONFIRMED — message without URL; LinkButton renders it | §2.3/§2.6(c) |
| D#6(body)/FLG-D11 §2.2 body vs §9 footnote conflict; `_safe_error` does NOT redact keys (measured: `sk-SECRET-123` survives) | CONFIRMED — footnote folded into body; local key-redacting helper; no GitPython | §2.2 |
| FLG-D10 `_OPENAI_COMPATIBLE_LOCAL` copy unpinned | CONFIRMED — `model_catalog.py` keeps a LOCAL copy with the same duplication-invariant comment + regression test as `providers_store.py:35-41` | §2.2 (this row is the pin) |
| FLG-D6 §9 cross-reference numbering drift vs Flagger's v1 report | CONFIRMED — cosmetic; §9 rows are keyed by ruling text, and Flagger's re-read of v1 findings #2/#14 is captured here: v1 #2 (Gemini KeyError) stands REFUTED (executed, both sessions agree); v1 #14 (persist blast radius) was Debugger-overlapping and is closed by the §2.4a v3 rewrite | §9 (this note) |
| Flagger "no exec" caveat | ACCEPTED — their file:line anchors were spot-verified by Supervisor where load-bearing (ensure_providers_yaml_exists; three-predicate divergence); execution-based confirmation came from Debugger's parallel probes | — |

**Net effect:** v3 moves `agent/runtime.py` out of the not-changed list (one probe-verified pre-req fix, own commit, before Wave A). All remaining sections are implementable as written; §6 criteria now cover both OpenRouter branches, the resolution path for menu activation, content-identity (not existence) for no-write, and carry `[xvfb]` markers.
