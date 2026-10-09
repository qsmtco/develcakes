# SPEC: Provider Reasoning Effort — Off / Low / Medium / High (dropdown on the provider card)

**Date:** 2026-10-09
**Author:** Hermes (external audit session; every code claim below was verified against HEAD `fce06aac` unless marked TO-VERIFY)
**Amended:** 2026-10-09 by Grok 4.7 — three review fixes, marked inline as `EDIT (Grok 4.7)`. Why: a Settings save does not rebuild a running agent, so reading the level only from the frozen `provider_cfg` would leave the next message at `off` until restart; the summary call should not inherit a thinking budget; and §2.1 told the implementer to both coerce invalid values and follow a precedent that does not coerce.
**Amended:** 2026-10-09 by Hermes — one fix to the §2.5 normalization rule, marked `FIX (Hermes)`: the Grok draft said "warns and stores `off`" while also saying "must not clobber a good frozen level", which contradict each other, and its snippet always clobbered. Replaced with an explicit three-path rule that matches the `live_card` block's own discipline ("Invalid or empty live values never clobber the snapshot").
**Status:** Draft — for PM review
**Supersedes:** `docs/specs/SPEC-THINKING-EFFORT.md` (unimplemented per-agent design — folded and re-homed here, see §1.3)
**Depends on:** nothing
**Target branch:** main
**Line anchors:** as of `fce06aac` — re-grep before editing (this repo moves)

> **Premise.** The user wants to request model reasoning ("thinking") from the provider settings card. The LLM APIs all differ: OpenAI uses `reasoning_effort`, OpenRouter uses `reasoning: {effort}`, Anthropic uses `thinking: {type, budget_tokens}`, GLM/MiniMax use their own keys. Design principle: **one abstract value chosen and stored by the user; every wire-format difference lives in the API adapters, never in the form.** A free-text field would push three JSON shapes onto the user, and a wrong value fails the request. One `Off / Low / Medium / High` dropdown covers all callers, and the caller is auto-detected at save time so the control is stable before and after detection.

---

## 1. Context

### 1.1 Verified state of the code (HEAD `fce06aac`)

- **Provider card** — `ui/views/settings_dialog.py` `_ProviderCard` (:29). Rows: Name, Base URL, Default Model, API Key, Caller (read-only label, :94-97), Context Window (:100-107), Compaction threshold (:109-117). Methods: `_labeled()` (:149), `_populate_from_provider` (:160), `_is_dirty` (:172), `_collect_from_form` (:197 — builds a FRESH `ProviderConfig` from scratch), save → `_on_save_clicked` (:222) → `settings_handler.add_or_update` (:79; caller auto-detect :99-109).
- **Mirror chain** — `models/providers.py` `ProviderConfig` (:43-55) → `utils/providers_store.py` `_to_dict` (:46) / `_from_dict` (:65) → `agent/config.py` `LLMProviderConfig` (:29-42) / `_to_llm_provider` (:134-149).
- **Provider protocol** — `agent/llm/protocol.py` `LLMProvider.call` (:27) / `stream` (:38): exact kwargs `(base_url, api_key, model, messages, tools, timeout, x_title)`. **No extra-args slot.**
- **Adapters (registry singletons)** — `agent/llm/registry.py`: `OpenAIProvider("openai"/"openrouter"/"zai")`, `MiniMaxProvider()`, `AnthropicProvider()`. The instance knows its caller id (`openai_provider.py:31-36`). Payload builders: `openai_provider.py` :54 (call) / :97 (stream); `minimax_provider.py` :103 / :151; `anthropic_provider.py` — `"max_tokens": 4096` hardcoded at :64 (call) and :117 (stream).
- **Runtime** — `_call_llm` (:2317) → streaming via `_call_llm_streaming` (:2508-2519; the kwarg contract is enforced by the `StreamingCallKwargs` TypedDict at :66, exported :89, and pinned by `TestStreamingSignature` at `tests/test_agent_runtime.py:1892-1925` which derives `expected_params` from the TypedDict) → `stream_with_ssl_retry(streamer, **kwargs)` explicit kwarg list at :2556-2564 → `provider.stream`. Non-streaming `provider.call` site :2497. Summary path `_call_for_summary` (:3006) → `provider.call` :3070. **EDIT (Grok 4.7):** the same function already overlays `live_card` from `providers.yaml` onto `provider_cfg` for `base_url` / `caller` / `api_key` (~2412-2443) because Settings does not rebuild a running agent. `reasoning_effort` joins that overlay (§2.5). The summary call does not.
- **Reasoning deltas today** — `parse_sse_delta` consumes only `content` / `tool_calls`; reasoning fields are silently ignored. Send-only phase 1 therefore has zero UI impact — no rendering paths change.
- **Live install callers** (for prioritisation): openrouter ×3 (DeepSeek v4.1 Flash, GLM 5.3 Flash, space bunny), zai ×2, minimax ×1, local-kb. **No anthropic provider configured today.**

### 1.2 The field-strip-on-save pattern (why §3's checklist is exhaustive)

`_to_dict`/`_from_dict` enumerate fields; `_collect_from_form` builds a fresh dataclass; `_to_llm_provider` copies a subset. A field that stops at any one layer silently vanishes on save. **Two live casualties prove the class:** `context_mode` (`models/providers.py:55`) is written by neither `_to_dict` nor `_from_dict` nor `_collect_from_form`; `default_max_tokens` (`models/providers.py:53`) is stored but never mirrored into `LLMProviderConfig`.

### 1.3 Relationship to SPEC-THINKING-EFFORT.md

The old spec (unimplemented) put the control on the **agent** (agent builder), level set off/low/high, plus a `supports_thinking` provider flag. This spec re-homes the control to the **provider card** per PM direction; keeps its good parts (off = omit entirely; hardcoded budget table; backwards compat; "don't bundle effort with visibility"); defers per-agent override + the capability flag to Phase 2 (out of scope here). `SPEC-THINKING-EFFORT.md` is marked superseded in the same change.

---

## 2. Design

### 2.1 The stored value

`reasoning_effort: str = "off"` on `ProviderConfig` — values `off | low | medium | high`.

- Default `"off"` keeps every existing payload byte-identical — **no YAML migration**.
- Normalization: missing key or `""` → `"off"` (backwards compat).
- **EDIT (Grok 4.7) — invalid values are coerced, on purpose.** An unrecognized level (`"turbo"`, `"max"`, a non-string) becomes `"off"` and `_from_dict` logs a warning. This is **not** the caller-validation precedent at `providers_store.py:65-75`. That path keeps a bad `caller` string and only warns, because a later save re-detects it. A bad reasoning level has no such repair, and forwarding it would 400 the request. Coerce at load so the adapter never sees anything outside `off|low|medium|high`. Do not cite the caller path as the template for this field.
- **EDIT (Grok 4.7) — the helper is required, not optional.** Add `validate_provider_reasoning_effort()` next to `validate_provider_context_mode` (`models/providers.py:63`), plus a `_VALID_REASONING_LEVELS` frozenset mirroring `_VALID_CONTEXT_MODES`. **FIX (Hermes):** LOAD and SAVE call the coercing helper (`anything else → "off"`). The live-card copy uses the frozenset directly instead — it must distinguish "invalid" from `"off"` BEFORE normalizing, so it can keep the frozen level on garbage (§2.5). Save must not persist an unknown level.

### 2.2 The card widget

A `Gtk.DropDown.new_from_strings(["Off", "Low", "Medium", "High"])` labeled **"Reasoning"**, placed after "Compaction threshold" (`settings_dialog.py:117`), built with the existing `_labeled()` row helper (:149). Index ↔ value: `0=off, 1=low, 2=medium, 3=high` (`"off"`/`""` → 0 on populate). Add the field to `_populate_from_provider`, `_is_dirty`, and `_collect_from_form`.

No per-caller widget logic: the caller label stays read-only and the caller is auto-detected at save (verified: `settings_dialog.py:94-97` + `settings_handler.py:99-109`), so one uniform control is correct before and after detection.

### 2.3 Adapter mapping — the wire formats

Mapping keys off the adapter instance's caller id. **`off`/`""`/unknown caller → the field is OMITTED ENTIRELY** — never send explicit "off" values (OpenAI doesn't honor `reasoning_effort: "off"`; an unsupported key can 400).

| Caller | Request field | Status |
|---|---|---|
| openai | `"reasoning_effort": "low"\|"medium"\|"high"` | Documented OpenAI parameter — ship in SP1 |
| openrouter | `"reasoning": {"effort": "low"\|"medium"\|"high"}` (OpenRouter unified `reasoning` object; aliases `reasoning_effort` — use the object form) | Ship in SP1 — highest value (covers 3 of the 6 live providers) |
| zai | GLM thinking key — **candidate** `"thinking": {"type": "enabled"}` (binary; any non-off level → enabled) | **TO-VERIFY** against the live zai provider (SP2 gate) — omit until probed |
| minimax | vendor key unknown. The old spec's research claims MiniMax-M3 accepts `"reasoning": "off"\|"low"\|"high"` — **UNVERIFIED** | **TO-VERIFY** (SP2 gate) — omit until probed |
| anthropic | `"thinking": {"type": "enabled", "budget_tokens": N}` — budget table low 1024 / medium 4096 / high 8192 (API minimum 1024) | **GATED** — SP3, ships only with both sub-items in §2.4 |

### 2.4 The Anthropic unit (SP3 — deferred with an explicit gate)

Two requirements beyond the mapping, both verified in code:

1. **`max_tokens` must rise.** Hardcoded 4096 (`anthropic_provider.py:64`/:117). Thinking tokens count against it and `budget_tokens` must be `< max_tokens`. When thinking is enabled, raise `max_tokens` to **16384** (budget ≤ 8192 leaves ≥ 8192 for the answer). This adjustment lives in the **adapter**, never in the form.
2. **Thinking-block round-trip.** Multi-turn tool use with extended thinking requires the exact signed thinking blocks echoed back in the assistant turn. Today the stream parser drops `thinking_delta`/`signature_delta` (the `anthropic_provider.py` stream handles only text/tool_use block types) and `convert_messages_for_anthropic` has no channel for them (assistant `tool_calls` → `[text?, tool_use...]` only — `convert.py:18-64`). Required: capture thinking blocks + signatures per assistant message at stream time, store them beside the message, re-emit them for that assistant turn in the converter.

**Gate:** the anthropic mapping ships ONLY together with (1)+(2). Until then the anthropic adapter keeps omitting it. There is no anthropic provider in the live install today — this unit exists for when one is added.

### 2.5 Resolution at call time (v1)

Level source: the provider default is the v1 semantics (one switch per provider). Per-agent override = Phase 2.

**EDIT (Grok 4.7) — read the live card, not only the frozen snapshot.** `_call_llm` already re-reads `providers.yaml` on every call (`agent/runtime.py` around the `live_card` block, currently ~2412-2443) and copies `base_url`, `caller`, and `api_key` onto `provider_cfg`, because a Settings save does not rebuild a running agent. `reasoning_effort` has to be copied in that **same block**. If it is read only from the startup `provider_cfg`, Save will update the card and the YAML and the next message will still go out as `off` until restart.

Rules for that copy (**FIX (Hermes)** — the earlier draft said "warns and stores `off`" and "must not clobber a good frozen level"; those contradict each other, so this is the unambiguous rule. It matches the block's own discipline at `agent/runtime.py:2412-2417` — *"Invalid or empty live values never clobber the snapshot"*):

- Take `live_card.reasoning_effort` when `live_card` is present.
- Missing / empty / non-string → `"off"` (same as load). A **valid** level — **including `"off"`** — applies immediately; this is what makes a Settings save affect the next message without a restart.
- A **non-empty invalid** value (hand-edited YAML, e.g. `"turbo"`) warns and **keeps the frozen level** — never forward garbage, never downgrade a good level on a typo.
- The load-path difference is deliberate: `_from_dict` **coerces** invalid → `"off"` because there is no prior value to protect there. (The asymmetry vs the `caller` path is also deliberate: a bad caller string is repaired at save by auto-detect; a bad reasoning level has no repair path.)
- Pass the resulting level into both the streaming and non-streaming branches of `_call_llm`.

**EDIT (Grok 4.7) — `_call_for_summary` stays `"off"`.** Do not pass the provider level into the summary `provider.call` (~3070). A compaction summary does not need a thinking budget, and an unsupported key there can fail the summary. The kwarg still exists on `call`; the summary site passes `"off"` explicitly so a future default change cannot turn it on by accident.

---

## 3. Files Changed — the exhaustive touch-point checklist

*If a box is not done, the field silently dies (§1.2).*

- [ ] **`models/providers.py`** — `ProviderConfig.reasoning_effort: str = "off"` + `validate_provider_reasoning_effort()` + `_VALID_REASONING_LEVELS` (both required; EDIT Grok 4.7 + FIX Hermes, §2.1).
- [ ] **`utils/providers_store.py`** — `_to_dict` (:46) **and** `_from_dict` (:65): write + read + normalize.
- [ ] **`agent/config.py`** — `LLMProviderConfig.reasoning_effort: str = "off"` + copy in `_to_llm_provider` (:134-149).
- [ ] **`ui/views/settings_dialog.py`** — `_ProviderCard`: build the dropdown (in the :109-147 row area), + `_populate_from_provider` (:160) + `_is_dirty` (:172) + `_collect_from_form` (:197 — the from-scratch builder!).
- [ ] **`agent/llm/protocol.py`** — `call` (:27) + `stream` (:38): add optional kwarg `reasoning_effort: str = "off"`.
- [ ] **`agent/llm/registry.py`** — no change (instances already carry caller ids).
- [ ] **`agent/runtime.py`**:
  - [ ] `StreamingCallKwargs` TypedDict (:66; exported :89) — add the field, or `TestStreamingSignature` (`tests/test_agent_runtime.py:1892`) **fails by design**.
  - [ ] `_call_llm` (:2317) — **EDIT (Grok 4.7):** in the existing `live_card` block (~2412-2443), copy `live_card.reasoning_effort` onto `provider_cfg` — valid levels (incl. `"off"`) apply; a non-empty invalid string warns and keeps the frozen level (**FIX Hermes**, §2.5). Pass that level to both branches. Do not read only the frozen snapshot.
  - [ ] `_call_llm_streaming` signature (:2508-2519) + its docstring contract + **the `stream_with_ssl_retry(streamer, **kwargs)` kwarg list (:2556-2564)** + the `provider.stream(...)` call.
  - [ ] Non-streaming `provider.call` site (:2497).
  - [ ] `_call_for_summary` (:3006) → `provider.call` (:3070) — **EDIT (Grok 4.7):** pass `reasoning_effort="off"` explicitly. Do not inherit the provider level.
- [ ] **`agent/llm/openai_provider.py`** — `call` payload (:54) + `stream` payload (:97): map by `self._id`.
- [ ] **`agent/llm/minimax_provider.py`** — `call` (:103) + `stream` (:151): SP2 mapping after its probe.
- [ ] **`agent/llm/anthropic_provider.py`** — SP3 only (§2.4); until then: omit.
- [ ] **`ui/handlers/settings_handler.py`** — no change expected (pass-through; the caller auto-detect there is untouched) — verify, don't assume.
- [ ] **`docs/specs/SPEC-THINKING-EFFORT.md`** — superseded banner (done in this change).

Adapter sketch (openai_provider; same shape for the others):

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

Runtime sketch (`_call_llm`, inside the existing `if live_card is not None:` block — **EDIT (Grok 4.7)**, same reason as §2.5):

```python
# Next to the live base_url / caller overrides. A Settings save must affect
# the next message without restarting the agent. FIX (Hermes): valid levels
# (incl. "off") apply; a non-empty invalid string warns and keeps the frozen
# level — same discipline as the caller override above.
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
...
return self._call_llm_streaming(
    ..., reasoning_effort=provider_cfg.reasoning_effort, ...
)
```

Summary sketch (`_call_for_summary` — **EDIT (Grok 4.7)**):

```python
response_dict = provider.call(
    ...,
    reasoning_effort="off",  # compaction stays cheap; never inherit the provider level
)
```

---

## 4. Phase plan

- **SP1 — Infra + openai/openrouter** (the unit that ships): everything in §3 except the zai/minimax/anthropic mappings. openai + openrouter mapped; every other caller omits (safe no-op — the dropdown exists but only the live-mapped providers act on it). Tests T1-T4.
- **SP2 — zai + minimax mappings, probe-gated:** capture the actual request for each live provider at each level (Test Connection seam, or a raw capture with that provider's own key), confirm the exact key + accepted values, then enable each mapping. Paste the captured evidence into the commit message. Until a probe passes for a caller, omission is the correct shipping behavior for it.
- **SP3 — anthropic unit (deferred, gated):** §2.4 (1)+(2) together.
- **Phase-2 candidates (out of scope here):** per-agent override (`SpecialAgentDef.reasoning_effort` — the old spec's home for this), `supports_thinking` capability flag (YAML-only), `/think <level>` runtime command, reasoning **visibility** (rendering thinking blocks in chat — a separate unit; keep the old spec's rule: don't bundle effort with visibility).

---

## 5. Tests & Acceptance Criteria

Mechanism notes: adapters import `urlopen_with_ssl_retry` into their own namespaces — payload tests patch `agent.llm.<adapter>.urlopen_with_ssl_retry` and read `req.data` (no network). GTK tests run under xvfb (`xvfb-run -a aa-exec -p develcakes-python -- .venv/bin/python3 -m pytest ...`); pure tests run plain.

| # | Test | Asserts |
|---|---|---|
| T1 | Payload mapping per caller, per level (off/low/medium/high × openai/openrouter; monkeypatched transport) | exact JSON key + value; the correct wire shape per caller |
| T2 | OFF byte-identical | with `reasoning_effort="off"`, each adapter's payload equals the pre-change payload (same key set) |
| T3 | Card round-trip: populate → collect → `add_or_update` → reload from store | the value survives every mirror layer (§1.2 class); `_is_dirty` flips on change |
| T4 | Store defaults/back-compat | YAML without the key loads as `"off"`; invalid value → `"off"` + warning (**coerced at load** — no prior value to protect; FIX Hermes, §2.1); `TestStreamingSignature` updated and green (TypedDict ↔ method params) |
| T4b | Live card wins over the frozen snapshot (EDIT Grok 4.7, §2.5) | With a runtime whose `provider_cfg.reasoning_effort` is `"off"` and a `providers.yaml` card of `"high"` for that provider, the next `_call_llm` payload carries the high wire value. `_call_for_summary` payload does **not**. With a card of `"turbo"` instead, the payload keeps the frozen level and warns — a bad live value never forwards, never downgrades (FIX Hermes) |
| T5 | (SP2) Live probe per mapped provider | recorded request/response evidence per caller in the commit message |

Acceptance-gate rows (when the proposed acceptance gate lands, `PROPOSAL-acceptance-gate.md`): RE-1 = T1/openrouter; RE-2 = T3; RE-3 = T2.

Manual acceptance (PM): Settings → Providers → set **Reasoning = High** on an openrouter provider → Save → reopen (persists) → send a message to an agent on that provider **without restarting the app** (EDIT Grok 4.7: this is the live-card case) → the outgoing request carries `reasoning.effort: "high"` (debug log or capture); set back to **Off** → the key disappears from the payload. A compaction/summary call from that same provider still omits the key.

---

## 6. Out of Scope

- Per-agent override; `supports_thinking` capability flag; thinking VISIBILITY in chat; `/think` command.
- Level sets beyond `off|low|medium|high`.
- zai/minimax keys BEFORE their probes — omission is the shipping behavior for unprobed callers.
- Any change to reasoning-delta parsing (send-only phase 1; `parse_sse_delta` already ignores reasoning fields, verified).

---

## 7. Implementer verification checklist

- [ ] Re-grep every anchor in §3 (line numbers are `fce06aac`-era).
- [ ] Run: payload tests (plain), card/settings suite + `tests/test_agent_runtime.py` (xvfb), `TestStreamingSignature`.
- [ ] ruff + pyright on changed files (pyright gate scope: gateway/models/utils — `ui/` stays outside, per `pyrightconfig.json`).
- [ ] After the change: `scripts/window-smoke.py` is NOT in the settings path (settings dialog is built on demand) — no smoke run needed; note this so nobody "fixes" it.
- [ ] SP2 probes: paste captured payloads into the commit message; flip each caller's mapping only on evidence.
