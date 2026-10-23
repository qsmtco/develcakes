# SPEC-55: Improve Model — configurable provider card + settings page

**Date:** 2026-10-23
**Author:** Supervisor
**Status:** Draft — for implementation
**Implements:** PM directive 2026-10-23 (improve button configurable; no hardcoded provider; reuse existing config machinery)
**Depends on:** SPEC-22 §2.8 (DecisionModel fixed-name card precedent), SPEC-37 (stt.yaml settings-page precedent), utils/providers_store (providers.yaml)
**Target branch:** main
**Base at time of writing:** d714914 (clean)

---

## 1. Problem

The Improve ✦ button is dead on a clean install. `utils/improve.py` reads a flat
`apiKey` from `~/.config/develcakes/config.json` — a file that no longer exists and
that nothing in the app ever writes. The app's real credential store is
`providers.yaml` (per-provider `api_key`/`base_url`/`caller`), populated by the
Settings dialog. Improve was never repointed, and its failure is silent
(`media_handler.py:99-102` logs a warning and returns — the user sees a brief
"Improving…" flash, then nothing).

PM requirements (verbatim intent):

1. A settings-dialog entry ("Improve Model", grouped with the Agents/Decision
   Model neighborhood) where the user sets which model the improve button uses.
2. **No hardcoded provider.** No MiniMax default, no MiniMax env var, no
   MiniMax anything. The card's endpoint/key/model are entirely the user's.
3. **Reuse the existing config machinery.** No new config file, no special
   store. The pattern to copy is the DecisionModel card: one fixed-name
   `ProviderConfig` in the same `providers.yaml`.
4. Unconfigured must be VISIBLE (PM confirmed option (a)): clicking Improve
   with no card shows an inline hint pointing at Settings, not silence.

## 2. Discovery (verified, not assumed)

- `ui/views/settings_dialog.py:604-605` — the DecisionModel page is
  `self._build_decision_page()` + `self._stack.add_titled(page, "decision-model",
  "Decision Model")`. `_build_decision_page` (line 853) reuses `_ProviderCard`
  with `name=DECISION_MODEL_PROVIDER, fixed_name=True` — the name is locked,
  every other field is user-editable. The PM wants the new page in the Agents
  neighborhood; the stack order today is Providers, Telegram, Decision Model,
  STT, Speech, Agents (lines 597-617). Add "Improve Model" after "Agents".
- `agent/llm/jev_client.py:86-100` — `load_decision_card()` is the lookup
  pattern: lazy `from utils.providers_store import load_providers`, scan for
  the fixed name, never raise, degrade to None. `DECISION_MODEL_PROVIDER =
  "DecisionModel"` (line 47).
- `utils/improve.py` — the module to change. Today: `_load_config()` (line 70)
  reads `get_config_file()` (config.json), caches the empty dict FOREVER on
  FileNotFoundError; `improve_prompt` (line 85) uses `cfg.get("apiKey")`,
  `DEFAULT_BASE_URL = "https://api.minimax.io/..."` (line 36),
  `DEFAULT_MODEL = "MiniMax-M2.5-Lightning"` (line 37). The error string at
  line 101 references a `MINIMAX_API_KEY` env var that no code reads.
- `ui/views/agent_builder.py:438-443` — the provider dropdown FILTERS OUT the
  DecisionModel card ("it is not a chat model"). The ImproveModel card needs
  the same treatment (SPEC-22 §2.8 pattern).
- `ui/handlers/media_handler.py:72-104` — `on_improve_click` / `_on_improve_result`.
  The error path (99-102) restores the button, logs, returns. This is where the
  visible-unconfigured requirement lands.
- `utils/stt.py:52-61` — `_resolve_api_key`: env first, then
  `agent.config.get_api_key("openrouter")`. The env-first shape is the
  precedent for an optional override (here: none required; the card is the
  only source — keep it simpler).
- `models/providers.py` — `ProviderConfig` dataclass; fields used here:
  `name`, `base_url`, `api_key`, `default_model`, `caller`.
- `_ProviderCard` save path (`settings_handler.add_or_update`) auto-detects
  `caller` — no code change needed for a card saved from the new page.

## 3. Solution

### 3a. `utils/improve.py` — card-driven, MiniMax gone

- New constant: `IMPROVE_MODEL_PROVIDER = "ImproveModel"`.
- New function `load_improve_card()` mirroring `load_decision_card` exactly:
  lazy providers_store import, scan for the fixed name, never raise, None on
  miss/bad store.
- `improve_prompt(raw_text, callback, GLib=None)` resolves from the card:
  - card missing → error string `Improve model not configured — Settings → Improve Model`
  - card present but `api_key`, `base_url`, or `default_model` blank → error
    naming the missing field(s)
  - otherwise POST to `base_url`, `Authorization: Bearer {api_key}`,
    `model = default_model` — the existing request/thread/dispatch body is
    otherwise unchanged (keep `_NoAuthRedirectHandler`, `validate_provider_url`,
    `assert_secure_file` checks, template loading, `_dispatch`).
- DELETE: `_load_config`, the module-level `_config`/`_config_lock` cache,
  `DEFAULT_BASE_URL`, `DEFAULT_MODEL`, and the `get_config_file` import. No
  config.json read of any kind remains. No env var is introduced.
- Keep `DEFAULT_SYSTEM_PROMPT` and the `prompts/system/improve.md` override —
  that is prompt text, not provider config, and it is provider-agnostic.
- Update the module Security Manifest header accordingly.

### 3b. `ui/views/settings_dialog.py` — the page

- `_build_improve_page()` cloned from `_build_decision_page` (line 853):
  intro label (plain-language: "The Improve ✦ button rewrites your input box
  text before you send it. This card is the model it uses — point it at any
  OpenAI-compatible chat endpoint. The name is fixed; the rest is yours."),
  then `_ProviderCard(self, name=IMPROVE_MODEL_PROVIDER, fixed_name=True)`
  pre-filled from the stored card when present, empty placeholder otherwise.
- Register: `self._stack.add_titled(self._improve_page, "improve-model", "Improve Model")`
  immediately after the Agents page (line 617), and add it to the appropriate
  `_SETTINGS_GROUPS` cluster so the group-button navigation reaches it.
- Import `IMPROVE_MODEL_PROVIDER` from `utils.improve` (same shape as the
  `_DECISION_MODEL_PROVIDER` import at line 25).

### 3c. `ui/views/agent_builder.py` — dropdown filter

- Extend the SPEC-22 §2.8 filter (lines 438-443) to drop BOTH non-chat cards:
  `DECISION_MODEL_PROVIDER` and `IMPROVE_MODEL_PROVIDER`. Same comment updated.

### 3d. `ui/handlers/media_handler.py` — visible failure (option (a))

- `_on_improve_result` error path: in addition to the log line, surface the
  error to the user. Smallest honest surface: reuse the pattern the STT path
  already has for state text — add a small transient status label mechanism to
  the button bar (or an existing equivalent on MainContent — Coder picks the
  one that exists; do not invent a toast system). Requirement: the error string
  from improve_prompt is readable in the UI for at least a few seconds, and the
  next improve click or input edit clears it.
- Tests keep using the fake `_mc` shape — the new label call must be a method
  on MainContent (`set_improve_status(text)` or equivalent) so fakes can stub it.

## 4. File changes

| File | Change | Est. |
|---|---|---|
| `utils/improve.py` | card lookup, delete config.json/MiniMax code, new errors | ~60 net |
| `ui/views/settings_dialog.py` | `_build_improve_page` + stack/group registration | ~60 |
| `ui/views/agent_builder.py` | dropdown filter extension | ~6 |
| `ui/handlers/media_handler.py` | visible error surface | ~20 |
| `ui/views/main_content.py` | status-label method (if no equivalent exists) | ~20 |
| tests | new + updated (below) | ~150 |

## 5. Tests

- `tests/test_improve.py` — rewrite the config-based fixtures:
  - no card → callback error contains "not configured"
  - card missing api_key / base_url / default_model → error names the field
  - happy path (monkeypatched opener): URL/headers/model come from the card;
    assert NO reference to minimax anywhere in the request
  - `load_improve_card` never raises on a corrupt store
  - grep-style source test: `utils/improve.py` contains no "minimax" (any
    case) and no `get_config_file`
- `tests/test_media_handler.py` — error path now calls the status method with
  the error text; success path clears it
- settings-dialog test (follow the DecisionModel page test's shape): page
  builds, card is fixed-name, save round-trips through providers_store
- agent-builder test: ImproveModel and DecisionModel both absent from the
  dropdown list

## 6. Acceptance

- [ ] Click Improve with no card → visible inline "not configured" hint (not silence)
- [ ] Settings → Improve Model page exists, reuses `_ProviderCard`, saves to
      providers.yaml as a real `ProviderConfig`
- [ ] Card's base_url/api_key/default_model drive the request; zero MiniMax
      references left in improve code
- [ ] ImproveModel + DecisionModel filtered from agent builder dropdowns
- [ ] `config.json` is not read or written anywhere in the improve path
- [ ] ruff clean, pyright clean, full suite green
