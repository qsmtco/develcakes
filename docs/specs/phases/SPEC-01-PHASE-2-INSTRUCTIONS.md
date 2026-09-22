# SPEC-01 Phase 2 Instructions — Runtime Live-Lookup Extension

**Spec:** docs/specs/SPEC-01-CONFIG-INVALIDATION.md (read it IN FULL first)
**Phase 2 of 2** — `agent/runtime.py` `_call_llm` live base_url/caller refresh + tests.
Phase 1 (refresh path + wiring, commit 4ebd2a4b) is DONE — do not touch it.

## Scope — exactly these 2 files

1. `agent/runtime.py` — `_call_llm` live-lookup block ONLY (~lines 2126–2157)
2. `tests/test_config_invalidation.py` — APPEND Phase 2 tests (do not modify Phase 1 tests)

Nothing else. No scope creep. Do not touch `refresh_provider_config`, window wiring,
settings_handler, or providers_store.

## The defect

`_call_llm` resolves `api_key` live from providers.yaml, but `base_url` and `caller`
come from the frozen `AgentConfig` snapshot forever. Incident (2026-09-20): corrected
z.ai base_url never reached running runtimes.

**Phase 2 defect on top of the spec's original sketch:** the entire live-lookup block
is gated by `if not effective_api_key:` — an agent with a per-agent key
(`conv.api_key`) skips live resolution entirely, so a base_url fix would never apply
to it. Phase 2 MUST decouple: base_url/caller refresh runs unconditionally; api_key
precedence stays exactly as today (per-agent key > live card > frozen snapshot).

## Current code (verified 2026-09-20, lines 2132–2157)

```python
effective_api_key = conv.api_key  # per-agent override wins (authoritative)
if not effective_api_key:
    try:
        from utils.providers_store import load_providers
        # Match live provider by display name (p.name) OR by the
        # provider-prefix derived from its default_model (e.g. name
        # 'glm5.2' with default_model 'zai/glm-5.2' → prefix 'zai').
        # So both provider_name='zai' (model prefix) and a display-name
        # match resolve to the live key.
        live_providers = load_providers()
        for p in live_providers:
            if p.name == provider_name and p.api_key:
                effective_api_key = p.api_key
                break
        if not effective_api_key:
            for p in live_providers:
                pm = (p.default_model or "")
                live_prefix = pm.split("/")[0] if "/" in pm else pm
                if live_prefix == provider_name and p.api_key:
                    effective_api_key = p.api_key
                    break
    except Exception as e:
        logger.warning("Cannot load providers.yaml for %s: %s", provider_name, e)
    if not effective_api_key:
        # Last resort: the (possibly stale) frozen runtime snapshot.
        effective_api_key = provider_cfg.api_key
```

## Required restructure

Match the live CARD first (display name → default_model prefix, no api_key condition
on the match), then derive everything from it:

```python
effective_api_key = conv.api_key  # per-agent override wins (authoritative)
# SPEC-01 Phase 2: resolve the live provider CARD once — base_url/caller
# refresh must run even when a per-agent key is set (the old block was gated
# on `not effective_api_key`, so a corrected base_url never reached agents
# with per-agent keys). api_key precedence is UNCHANGED.
live_card = None
try:
    from utils.providers_store import load_providers
    # Match by display name (p.name) OR by the provider-prefix derived from
    # its default_model — same semantics as the old key-only loops.
    live_providers = load_providers()
    for p in live_providers:
        if p.name == provider_name:
            live_card = p
            break
    if live_card is None:
        for p in live_providers:
            pm = (p.default_model or "")
            live_prefix = pm.split("/")[0] if "/" in pm else pm
            if live_prefix == provider_name:
                live_card = p
                break
except Exception as e:
    logger.warning("Cannot load providers.yaml for %s: %s", provider_name, e)

if live_card is not None:
    # Live base_url/caller override (SPEC-01). provider_cfg is this runtime's
    # OWN clone (Phase 1 BUG 6 fix), so in-place mutation cannot leak across
    # runtimes. Empty live values never clobber the snapshot.
    if live_card.base_url and live_card.base_url != provider_cfg.base_url:
        logger.info("[call-llm] live base_url override for %s: %s -> %s",
                    provider_name, provider_cfg.base_url, live_card.base_url)
        provider_cfg.base_url = live_card.base_url
    if live_card.caller and live_card.caller.lower() != provider_cfg.caller:
        logger.info("[call-llm] live caller override for %s: %s -> %s",
                    provider_name, provider_cfg.caller, live_card.caller)
        provider_cfg.caller = live_card.caller.lower()
    if not effective_api_key and live_card.api_key:
        effective_api_key = live_card.api_key
if not effective_api_key:
    # Last resort: the (possibly stale) frozen runtime snapshot.
    effective_api_key = provider_cfg.api_key
```

Semantics guards:
- The card match must NOT require `p.api_key` (a keyless card still carries a valid
  base_url fix). Key precedence itself is untouched.
- Mutation happens BEFORE the streaming/non-streaming branch split — both branches
  read `provider_cfg.base_url` (~2169/2195) after this block. Do NOT move it.
- `caller` assignment lowercases (matches `_resolve_caller_key` at :2053).
- One `load_providers()` call per `_call_llm` — do not add a second.

## Facts you can rely on (verified)

- `load_providers()` returns `list[ProviderConfig]` (utils/providers_store.py:149) with
  fields `name, base_url, api_key, default_model, caller` (`_from_dict` :65; caller is
  validated against `_VALID_CALLERS`, normalized `""` for None)
- `provider_cfg` is `LLMProviderConfig` (agent/config.py:29) — a mutable dataclass with
  `base_url: str`, `caller: str = ""`; each runtime owns its own values (Phase 1 clones)
- `AgentRuntime(config=AgentConfig(...))` is headless-constructible: all callbacks
  optional, `migrate_conversation_files()` failure is caught (non-fatal); under the
  `tmp_config_dir` fixture HOME is isolated
- `_call_llm` needs `self._conversations[session_key]` to exist with `.api_key`,
  `.model`, `.app_title` — inject a `SimpleNamespace` directly
- Test seam for assertions WITHOUT network: leave `caller=""` on the frozen card so
  `_call_llm` raises ValueError("No caller…") at :2197 — the mutation already happened,
  so assert on `config.providers[...]` afterwards

## Tests to append (class `TestCallLlmLiveRefresh`)

Fixture pattern: build `AgentConfig(providers={...}, default_provider="p1")` with
frozen cards; construct `AgentRuntime(config=cfg)`; inject conversation
`rt._conversations["s1"] = SimpleNamespace(api_key=None, model="p1/m1", app_title="t")`;
write live cards to the tmp config dir via `save_providers` (see Phase 1 tests'
`_make_provider` helper — reuse it).

1. `test_stale_base_url_refreshed_next_call` — frozen base_url "old", live card
   base_url "new" → `_call_llm` raises (no caller) → assert frozen card's `base_url
   == "new"` (mutation landed pre-branch)
2. `test_per_agent_key_does_not_skip_base_url_refresh` — conv.api_key="sk-override",
   stale base_url → after raise: base_url refreshed AND assert the resolved key…
   use the streaming capture (test 6) or monkeypatch `_get_provider` to capture
   `api_key == "sk-override"` — per-agent key must still win over the live card's key
3. `test_caller_refreshed_lowercased` — live caller "ZAI" → frozen `caller == "zai"`
4. `test_no_live_match_leaves_snapshot_untouched` — live list has only unrelated
   provider → frozen base_url/caller unchanged after raise
5. `test_load_providers_failure_uses_frozen_values` — monkeypatch
   `utils.providers_store.load_providers` to raise → warning logged, frozen values
   unchanged, call still proceeds to its normal failure/raise (no new exception type)
6. `test_streaming_path_uses_refreshed_base_url` — set `rt._on_text_delta = lambda…`,
   frozen card `supports_streaming=True` + valid caller; monkeypatch
   `rt._call_llm_streaming` (SimpleNamespace/method) to capture kwargs → captured
   `base_url == live_url`
7. `test_live_card_empty_api_key_falls_back_to_frozen_key` — live card key "" (or
   absent), no per-agent key → frozen snapshot key used (existing last-resort path
   intact)

All tests use the existing `tmp_config_dir` fixture. No GTK.

## Verification (run + paste outputs)

```
python -m pytest tests/test_config_invalidation.py -v
python -m pytest tests/test_settings_handler.py -q
ruff check agent/runtime.py tests/test_config_invalidation.py
ruff format --check agent/runtime.py tests/test_config_invalidation.py
pyright agent/runtime.py 2>&1 | tail -5
```

## COMPLETENESS report (required in your reply)

- [ ] runtime.py block replaced (quote final line range)
- [ ] tests appended (count) — all 7 present
- [ ] All 5 verification outputs pasted
- [ ] Spec drift noted (line drift >10 lines)
- [ ] api_key precedence unchanged (test 2 proves it)

Report "SPEC-01 PHASE-2 COMPLETE" with checklist + outputs, or blockers with exact
reproduction. Flag related issues — do NOT fix them silently.
