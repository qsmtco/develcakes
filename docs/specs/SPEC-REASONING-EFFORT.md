# SPEC: Provider Reasoning Effort — Off / Low / Medium / High (dropdown on the provider card)

**Date:** 2026-10-09
**Author:** Hermes (external audit session; every code claim below was verified against HEAD `fce06aac` unless marked TO-VERIFY)
**Status:** Draft — SP1 ready to implement
**Supersedes:** `docs/specs/SPEC-THINKING-EFFORT.md` (unimplemented per-agent design — folded and re-homed here, see §1.3)
**Depends on:** nothing
**Target branch:** main
**Line anchors:** as of `fce06aac` — re-grep before editing (this repo moves)

**Changelog (folded into the contract below — do not reconstruct rules from banners):**
- 2026-10-09 Grok 4.7 — live-card overlay (Settings save must affect the next message without restart); summary call stays `"off"`; invalid levels are coerced at load, not kept like a bad `caller`.
- 2026-10-09 Hermes — live overlay: valid levels including `"off"` apply; a non-empty invalid live value keeps the frozen level (never clobber a good snapshot).
- 2026-10-09 Grok 4.6 / PM — `supports_reasoning` checkbox on the card (default False). Reasoning support is a model property; a provider card is 1:1 with `default_model`, so the flag is the model-grain guard. OpenRouter does not reliably ignore unknown reasoning (concrete slugs 400; OpenClaw hit this on `x-ai/grok-*`).

> **Premise.** The user wants to request model reasoning ("thinking") from the provider settings card. The LLM APIs all differ: OpenAI uses `reasoning_effort`, OpenRouter uses `reasoning: {effort}`, Anthropic uses `thinking: {type, budget_tokens}`, GLM/MiniMax use their own keys. Design principle: **one abstract value chosen and stored by the user; every wire-format difference lives in the API adapters, never in the form.** One `Off / Low / Medium / High` dropdown covers all callers. A **Supports reasoning** checkbox is the send-side guard: the dropdown is inert unless the user opts that card in. Caller is auto-detected at save time so the controls are stable before and after detection.

> **§3 is the contract. §2 is rationale.** Implement from the checklist.

> **SP1 live reach (state this plainly).** SP1 maps **openai** and **openrouter**. The live install has **no openai card**. The dropdown only does anything on an OpenRouter card whose **Supports reasoning** box is checked. zai / minimax / anthropic / local-kb omit. The openai mapping is real code, unused until someone adds that card.

---

## 1. Context

### 1.1 Verified state of the code (HEAD `fce06aac`)

- **Provider card** — `ui/views/settings_dialog.py` `_ProviderCard` (:29). Rows: Name, Base URL, Default Model, API Key, Caller (read-only label, :94-97), Context Window (:100-107), Compaction threshold (:109-117). Methods: `_labeled()` (:149), `_populate_from_provider` (:160), `_is_dirty` (:172), `_collect_from_form` (:197 — builds a FRESH `ProviderConfig` from scratch), save → `_on_save_clicked` (:222) → `settings_handler.add_or_update` (:79; caller auto-detect :99-109). Test Connection reconstructs `ProviderConfig` in `settings_handler.py` (:258, :273, :329) and `settings_dialog.py` (`_on_test_result` :294, :313) — those sites strip any field they do not copy.
- **Mirror chain** — `models/providers.py` `ProviderConfig` (:43-55) → `utils/providers_store.py` `_to_dict` (:46) / `_from_dict` (:65) → `agent/config.py` `LLMProviderConfig` (:29-42) / `_to_llm_provider` (:134-149).
- **Provider protocol** — `agent/llm/protocol.py` `LLMProvider.call` (:27) / `stream` (:38): exact kwargs `(base_url, api_key, model, messages, tools, timeout, x_title)`. **No extra-args slot.**
- **Adapters (registry singletons)** — `agent/llm/registry.py`: `OpenAIProvider("openai"/"openrouter"/"zai")`, `MiniMaxProvider()`, `AnthropicProvider()`. The instance knows its caller id (`openai_provider.py:31-36`). Payload builders: `openai_provider.py` :54 (call) / :97 (stream); `minimax_provider.py` :103 / :151; `anthropic_provider.py` — `"max_tokens": 4096` hardcoded at :64 (call) and :117 (stream).
- **Runtime** — `_call_llm` (:2317) → streaming via `_call_llm_streaming` (:2508-2519; the kwarg contract is enforced by the `StreamingCallKwargs` TypedDict at :66, exported :89, and pinned by `TestStreamingSignature` at `tests/test_agent_runtime.py:1892-1925` which derives `expected_params` from the TypedDict) → `stream_with_ssl_retry(streamer, **kwargs)` forwards kwargs verbatim (:2552-2561) → `provider.stream`. Non-streaming `provider.call` site :2497. Summary path `_call_for_summary` (:3006) → `provider.call` :3070. `_call_llm` already overlays `live_card` from `providers.yaml` onto `provider_cfg` for `base_url` / `caller` / `api_key` (~2412-2443) because Settings does not rebuild a running agent. `reasoning_effort` and `supports_reasoning` join that overlay (§2.5). The summary call does not.
- **Reasoning deltas today** — `parse_sse_delta` consumes only `content` / `tool_calls`; reasoning fields are silently ignored. Send-only phase 1 therefore has zero UI impact — no rendering paths change.
- **Live install callers** (for prioritisation): openrouter ×3 (DeepSeek v4.1 Flash, GLM 5.3 Flash, space bunny), zai ×2, minimax ×1, local-kb. **No openai provider. No anthropic provider.**

### 1.2 The field-strip-on-save pattern (why §3's checklist is exhaustive)

`_to_dict`/`_from_dict` enumerate fields; `_collect_from_form` builds a fresh dataclass; `_to_llm_provider` copies a subset; Test Connection rebuilds a fresh dataclass. A field that stops at any one layer silently vanishes on save or on Test. **Two live casualties prove the class:** `context_mode` (`models/providers.py:55`) is written by neither `_to_dict` nor `_from_dict` nor `_collect_from_form`; `default_max_tokens` (`models/providers.py:53`) is stored but never mirrored into `LLMProviderConfig`.

### 1.3 Relationship to SPEC-THINKING-EFFORT.md

The old spec (unimplemented) put the control on the **agent** (agent builder), level set off/low/high, plus a `supports_thinking` provider flag. This spec re-homes the control to the **provider card** per PM direction; keeps its good parts (off = omit entirely; hardcoded budget table; backwards compat; "don't bundle effort with visibility"). The capability flag ships in SP1 as `supports_reasoning` (checkbox, default False) — not YAML-only, not Phase 2. Per-agent override stays Phase 2. `SPEC-THINKING-EFFORT.md` is marked superseded in the same change.

---

## 2. Design

### 2.1 The stored values

Two fields on `ProviderConfig` and `LLMProviderConfig`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `reasoning_effort` | `str` | `"off"` | `off \| low \| medium \| high` |
| `supports_reasoning` | `bool` | `False` | Send-side guard. Default False: existing YAML has no key, payloads stay byte-identical, a non-reasoning OpenRouter model cannot 400 on first Save. |

**`reasoning_effort` normalization**

- Missing key or `""` → `"off"` (backwards compat). No YAML migration.
- Invalid values are coerced, on purpose. An unrecognized level (`"turbo"`, `"max"`, a non-string) becomes `"off"` and `_from_dict` logs a warning. This is **not** the caller-validation precedent at `providers_store.py:65-75`. That path keeps a bad `caller` string and only warns, because a later save re-detects it. A bad reasoning level has no such repair, and forwarding it would 400 the request. Coerce at load so the adapter never sees anything outside `off|low|medium|high`.
- Add `validate_provider_reasoning_effort()` next to `validate_provider_context_mode` (`models/providers.py:63`), plus a `_VALID_REASONING_LEVELS` frozenset mirroring `_VALID_CONTEXT_MODES`. LOAD and SAVE call the coercing helper (`anything else → "off"`). The live-card copy uses the frozenset directly instead — it must distinguish "invalid" from `"off"` BEFORE normalizing, so it can keep the frozen level on garbage (§2.5). Save must not persist an unknown level.

**`supports_reasoning` normalization**

- Missing key → `False`.
- LOAD: only an actual JSON/YAML bool `true` is True; anything else → `False` (do not treat the string `"true"` as True — keep the load path boring).
- LIVE overlay: a real bool (including `False`) applies immediately. A non-bool live value warns and keeps the frozen flag.

### 2.2 The card widgets

Two rows after "Compaction threshold" (`settings_dialog.py:117`), built with `_labeled()` (:149):

1. **Supports reasoning** — `Gtk.CheckButton`, default unchecked. This is the opt-in. Persist via `_populate_from_provider` / `_is_dirty` / `_collect_from_form`.
2. **Reasoning** — `Gtk.DropDown.new_from_strings(["Off", "Low", "Medium", "High"])`. Index ↔ value: `0=off, 1=low, 2=medium, 3=high` (`"off"`/`""` → 0 on populate). **Insensitive unless the checkbox is active.** Persist the selected level even when the box is unchecked (re-checking must not lose High).

**Default Model change resets the flag.** When the Default Model entry text differs from the stored `default_model`, uncheck **Supports reasoning** (and therefore disable the dropdown). A card that was "DeepSeek, High" must not stay opted-in after the user points it at GLM. Re-checking is a deliberate second action. Guard the handler so `_populate_from_provider` does not trip this reset.

No per-caller widget logic: the caller label stays read-only and the caller is auto-detected at save.

### 2.3 Adapter mapping — the wire formats

Runtime computes an **effective** level and passes that single kwarg into `call` / `stream`:

```
effective = provider_cfg.reasoning_effort
           if provider_cfg.supports_reasoning
           else "off"
```

Mapping keys off the adapter instance's caller id. **`off`/`""`/unknown caller → the field is OMITTED ENTIRELY** — never send explicit "off" values (OpenAI doesn't honor `reasoning_effort: "off"`; an unsupported key can 400). The adapter does not need the checkbox; runtime already collapsed it.

| Caller | Request field | Status |
|---|---|---|
| openai | `"reasoning_effort": "low"\|"medium"\|"high"` | Documented OpenAI parameter — ship in SP1 (unused until an openai card exists) |
| openrouter | `"reasoning": {"effort": "low"\|"medium"\|"high"}` (OpenRouter unified `reasoning` object; aliases `reasoning_effort` — use the object form) | Ship in SP1 — the only live mapped caller |
| zai | GLM thinking key — **candidate** `"thinking": {"type": "enabled"}` (binary; any non-off level → enabled) | **TO-VERIFY** against the live zai provider (SP2 gate) — omit until probed |
| minimax | vendor key unknown. The old spec's research claims MiniMax-M3 accepts `"reasoning": "off"\|"low"\|"high"` — **UNVERIFIED** | **TO-VERIFY** (SP2 gate) — omit until probed |
| anthropic | `"thinking": {"type": "enabled", "budget_tokens": N}` — budget table low 1024 / medium 4096 / high 8192 (API minimum 1024) | **GATED** — SP3, ships only with both sub-items in §2.4 |

OpenRouter does **not** reliably ignore `reasoning` on non-reasoning models (concrete slugs are strict; OpenClaw #32054). The checkbox is the guard, not "the caller will drop it."

### 2.4 The Anthropic unit (SP3 — deferred with an explicit gate)

Two requirements beyond the mapping, both verified in code. Item 2 is a **message-schema change**, not an adapter tweak.

1. **`max_tokens` must rise.** Hardcoded 4096 (`anthropic_provider.py:64`/:117). Thinking tokens count against it and `budget_tokens` must be `< max_tokens`. When thinking is enabled, raise `max_tokens` to **16384** (budget ≤ 8192 leaves ≥ 8192 for the answer). This adjustment lives in the **adapter**, never in the form.
2. **Thinking-block round-trip.** Multi-turn tool use with extended thinking requires the exact signed thinking blocks echoed back in the assistant turn. Today the stream parser drops `thinking_delta`/`signature_delta` (the `anthropic_provider.py` stream handles only text/tool_use block types) and `convert_messages_for_anthropic` has no channel for them (assistant `tool_calls` → `[text?, tool_use...]` only — `convert.py:18-64`). Required: capture thinking blocks + signatures per assistant message at stream time, store them beside the message, re-emit them for that assistant turn in the converter.

**Gate:** the anthropic mapping ships ONLY together with (1)+(2). Until then the anthropic adapter keeps omitting it. There is no anthropic provider in the live install today — this unit exists for when one is added. The gate costs nothing.

### 2.5 Resolution at call time (v1)

Level source: the provider default is the v1 semantics (one switch per provider card). Per-agent override = Phase 2.

`_call_llm` already re-reads `providers.yaml` on every call (`agent/runtime.py` around the `live_card` block, currently ~2412-2443) and copies `base_url`, `caller`, and `api_key` onto `provider_cfg`, because a Settings save does not rebuild a running agent. **Both** `reasoning_effort` and `supports_reasoning` copy in that **same block**. If they are read only from the startup `provider_cfg`, Save will update the card and the YAML and the next message will still go out as `off` / unflagged until restart.

**`reasoning_effort` copy** (matches the block's own discipline — *"Invalid or empty live values never clobber the snapshot"* at `agent/runtime.py:2412-2417`):

- Take `live_card.reasoning_effort` when `live_card` is present.
- Missing / empty / non-string → `"off"` (same as load). A **valid** level — **including `"off"`** — applies immediately; this is what makes a Settings save affect the next message without a restart.
- A **non-empty invalid** value (hand-edited YAML that somehow bypassed load coerce, e.g. `"turbo"`) warns and **keeps the frozen level** — never forward garbage, never downgrade a good level on a typo.
- The load-path difference is deliberate: `_from_dict` **coerces** invalid → `"off"` because there is no prior value to protect there.

**`supports_reasoning` copy:**

- A real bool — **including `False`** — applies immediately (unchecking must silence the next message).
- A non-bool live value warns and keeps the frozen flag.

Pass the **effective** level (`off` when the flag is False) into both the streaming and non-streaming branches of `_call_llm`.

**`_call_for_summary` stays `"off"`.** Do not pass the provider level into the summary `provider.call` (~3070). A compaction summary does not need a thinking budget, and an unsupported key there can fail the summary. The kwarg still exists on `call`; the summary site passes `"off"` explicitly so a future default change cannot turn it on by accident.

---

## 3. Files Changed — the exhaustive touch-point checklist

*If a box is not done, the field silently dies (§1.2). This section is the contract.*

- [x] **`models/providers.py`** — `ProviderConfig.reasoning_effort: str = "off"` + `ProviderConfig.supports_reasoning: bool = False` + `validate_provider_reasoning_effort()` + `_VALID_REASONING_LEVELS`.
- [x] **`utils/providers_store.py`** — `_to_dict` **and** `_from_dict`: write + read + normalize both fields. Invalid level → `"off"` + warning. Missing flag → `False`.
- [x] **`agent/config.py`** — `LLMProviderConfig.reasoning_effort: str = "off"` + `LLMProviderConfig.supports_reasoning: bool = False` + copy both in `_to_llm_provider`.
- [x] **`ui/views/settings_dialog.py`** — `_ProviderCard`: checkbox + dropdown after Compaction threshold; dropdown sensitive only when checked; Default Model change unchecks; `_populate_from_provider` + `_is_dirty` + `_collect_from_form` (the from-scratch builder). `_on_test_result` reconstructions must not strip the new fields (`dataclasses.replace` preferred).
- [x] **`ui/handlers/settings_handler.py`** — the three `ProviderConfig(...)` reconstructions on Test Connection (:258, :273, :329) must preserve both new fields (`dataclasses.replace` preferred). Caller auto-detect stays untouched.
- [x] **`agent/llm/protocol.py`** — `call` + `stream`: add optional kwarg `reasoning_effort: str = "off"`.
- [x] **`agent/llm/registry.py`** — no change (instances already carry caller ids).
- [x] **`agent/llm/minimax_provider.py`** / **`agent/llm/anthropic_provider.py`** — accept the new kwarg and ignore it (SP2/SP3). A missing param is a TypeError on every MiniMax/Anthropic call.
- [x] **`agent/runtime.py`**:
  - [x] `StreamingCallKwargs` TypedDict — add `reasoning_effort`, or `TestStreamingSignature` **fails by design**.
  - [x] `_call_llm` — in the existing `live_card` block, copy `reasoning_effort` (valid incl. `"off"` apply; non-empty invalid warns and keeps frozen) and `supports_reasoning` (bool incl. `False` applies; non-bool keeps frozen). Pass **effective** level (`off` when flag is False) to both branches.
  - [x] `_call_llm_streaming` signature + docstring + `stream_with_ssl_retry(...)` kwargs + `provider.stream(...)`.
  - [x] Non-streaming `provider.call` site.
  - [x] `_call_for_summary` → `provider.call` — pass `reasoning_effort="off"` explicitly. Do not inherit the provider level.
- [x] **`agent/llm/openai_provider.py`** — `call` payload + `stream` payload: map by `self._id` (openai / openrouter only).
- [x] **`docs/specs/SPEC-THINKING-EFFORT.md`** — superseded banner (already present; leave it).

Adapter sketch (openai_provider):

```python
def _apply_reasoning(payload: dict, caller: str, level: str) -> None:
    """Map the abstract level to this caller's wire format.
    off / "" / unknown caller → omit the field entirely."""
    if not level or level == "off":
        return
    if caller == "openai":
        payload["reasoning_effort"] = level                 # low | medium | high
    elif caller == "openrouter":
        payload["reasoning"] = {"effort": level}            # unified reasoning object
    # "zai": SP2 — add ONLY after the live probe confirms the key.
```

Runtime sketch (`_call_llm`, inside the existing `if live_card is not None:` block):

```python
# Next to the live base_url / caller overrides. A Settings save must affect
# the next message without restarting the agent.
live_level = getattr(live_card, "reasoning_effort", "off") or "off"
live_level = live_level.strip().lower() if isinstance(live_level, str) else "off"
if live_level in _VALID_REASONING_LEVELS:
    provider_cfg.reasoning_effort = live_level
else:
    logger.warning(
        "[call-llm] live reasoning_effort %r for %s is not a valid level; "
        "keeping frozen %r", live_level, provider_name,
        provider_cfg.reasoning_effort,
    )
live_flag = getattr(live_card, "supports_reasoning", None)
if isinstance(live_flag, bool):
    provider_cfg.supports_reasoning = live_flag
else:
    logger.warning(
        "[call-llm] live supports_reasoning %r for %s is not a bool; "
        "keeping frozen %r", live_flag, provider_name,
        getattr(provider_cfg, "supports_reasoning", False),
    )
effective_effort = (
    provider_cfg.reasoning_effort
    if getattr(provider_cfg, "supports_reasoning", False)
    else "off"
)
```

Summary sketch (`_call_for_summary`):

```python
response_dict = provider.call(
    ...,
    reasoning_effort="off",  # compaction stays cheap; never inherit the provider level
)
```

---

## 4. Phase plan

- **SP1 — Infra + openai/openrouter + supports_reasoning checkbox** (the unit that ships): everything in §3 except the zai/minimax/anthropic mappings. openai + openrouter mapped; every other caller omits. Live effect is OpenRouter-only, and only on a card whose checkbox is on. Tests T1–T4b.
- **SP2 — zai + minimax mappings, probe-gated:** capture the actual request for each live provider at each level (Test Connection seam, or a raw capture with that provider's own key), confirm the exact key + accepted values, then enable each mapping. Paste the captured evidence into the commit message. Until a probe passes for a caller, omission is the correct shipping behavior for it. Optional follow-up: Test Connection reads `reasoning` / `supported_efforts` from the existing `/v1/models` GET and offers to set the checkbox.
- **SP3 — anthropic unit (deferred, gated):** §2.4 (1)+(2) together. Item 2 is a message-schema change.
- **Phase-2 candidates (out of scope here):** per-agent override (`SpecialAgentDef.reasoning_effort` — the old spec's home for this), `/think <level>` runtime command, reasoning **visibility** (rendering thinking blocks in chat — a separate unit; keep the old spec's rule: don't bundle effort with visibility).

---

## 5. Tests & Acceptance Criteria

Mechanism notes: adapters import `urlopen_with_ssl_retry` into their own namespaces — payload tests patch `agent.llm.<adapter>.urlopen_with_ssl_retry` and read `req.data` (no network). GTK tests run under xvfb (`xvfb-run -a aa-exec -p develcakes-python -- .venv/bin/python3 -m pytest ...`); pure tests run plain.

| # | Test | Asserts |
|---|---|---|
| T1 | Payload mapping per caller, per level (off/low/medium/high × openai/openrouter; monkeypatched transport) | exact JSON key + value; the correct wire shape per caller; zai still omits |
| T2 | OFF byte-identical | with `reasoning_effort="off"`, each adapter's payload equals the pre-change payload (same key set) |
| T3 | Card round-trip: populate → collect → `add_or_update` → reload from store | both fields survive every mirror layer; `_is_dirty` flips on checkbox or dropdown change; dropdown is insensitive when unchecked; changing Default Model unchecks the box |
| T4 | Store defaults/back-compat | YAML without the keys loads as `"off"` / `False`; invalid level → `"off"` + warning; `TestStreamingSignature` updated and green |
| T4b | Live card wins over the frozen snapshot | Frozen `off` + `supports_reasoning=False`, live card `high` + `True` → `_call_llm` payload carries the high wire value. Same live card but `supports_reasoning=False` → omit (flag is the guard). `_call_for_summary` payload does **not** inherit. Live `"turbo"` keeps the frozen level and warns. |
| T5 | (SP2) Live probe per mapped provider | recorded request/response evidence per caller in the commit message |

Acceptance-gate rows (when the proposed acceptance gate lands, `PROPOSAL-acceptance-gate.md`): RE-1 = T1/openrouter; RE-2 = T3; RE-3 = T2.

Manual acceptance (PM): Settings → Providers → on an openrouter provider check **Supports reasoning**, set **Reasoning = High** → Save → reopen (both persist) → send a message to an agent on that provider **without restarting the app** → the outgoing request carries `reasoning.effort: "high"` (debug log or capture); uncheck **Supports reasoning** or set **Off** → the key disappears. Change Default Model → the checkbox clears. A compaction/summary call from that same provider still omits the key.

---

## 6. Out of Scope

- Per-agent override; thinking VISIBILITY in chat; `/think` command.
- Level sets beyond `off|low|medium|high`.
- zai/minimax keys BEFORE their probes — omission is the shipping behavior for unprobed callers.
- Auto-detecting the checkbox from OpenRouter `GET /api/v1/models` (`reasoning.supported_efforts`) — correct grain, not needed to ship SP1.
- Any change to reasoning-delta parsing (send-only phase 1; `parse_sse_delta` already ignores reasoning fields, verified).

---

## 7. Implementer verification checklist

- [x] Re-grep every anchor in §3 (line numbers are `fce06aac`-era).
- [x] Run: payload tests (plain), card/settings suite + `tests/test_agent_runtime.py` (xvfb), `TestStreamingSignature`.
- [x] ruff + pyright on changed files (pyright gate scope: gateway/models/utils — `ui/` stays outside, per `pyrightconfig.json`). Pre-existing ruff on untouched import style in openai/settings left alone.
- [x] After the change: `scripts/window-smoke.py` is NOT in the settings path (settings dialog is built on demand) — no smoke run needed; note this so nobody "fixes" it.
- [ ] SP2 probes: paste captured payloads into the commit message; flip each caller's mapping only on evidence.
