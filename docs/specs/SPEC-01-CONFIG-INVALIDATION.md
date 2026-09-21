# SPEC-01: Runtime Provider-Config Invalidation

**Date:** 2026-09-20
**Author:** Supervisor (develcakes v2)
**Status:** Draft — for implementation
**Implements:** .crabcakes/architecture.md (agent/ bug-fix delta #1)
**Depends on:** none
**Target branch:** main

> Architecture compliance: agent/ owns the turn engine; provider config must resolve
> live or cached runtimes must invalidate on provider save (architecture.md §Modules/agent).

---

## 1. Overview

**Problem.** `AgentRuntime` instances are cached in
`ui/handlers/agent_runtime_handler.py:_runtimes` (keyed by agent display name, created in
`_get_runtime` at line ~905) and hold a **frozen** `AgentConfig` snapshot from creation
time. `_call_llm` (agent/runtime.py ~2131) re-reads **only `api_key`** live from
providers.yaml per call; `base_url`, `caller`, `supports_streaming`, and `max_tokens` come
from the frozen snapshot forever. Two incidents (2026-09-20) proved the cost: a stale
credential produced 401s that Test Connection (which reads the live file) called healthy,
and a corrected `base_url` (z.ai coding endpoint) didn't reach running runtimes.

**Solution.** Two complementary changes:
1. **Live resolution in `_call_llm`** — extend the existing live-lookup block to refresh
   `base_url` and `caller` from providers.yaml per call (api_key already does).
2. **Cache invalidation on save** — `SettingsHandler.add_or_update` fires
   `on_providers_changed`; wire that signal to a new
   `AgentRuntimeHandler.refresh_provider_config()` that updates each cached runtime's
   provider snapshot in place (conversations stay; no runtime restart).

**Scope**

| In | Out |
|---|---|
| agent/runtime.py `_call_llm` live-lookup extension | Reworking provider resolution into a new module |
| settings_handler.py signal wiring | Changing providers.yaml format |
| agent_runtime_handler.py `refresh_provider_config()` | Agent YAML changes (llm_name flows are separate) |
| Regression tests | |

## 2. Changes by File

### agent/runtime.py — `_call_llm` (~line 2125–2160)

Current block resolves `effective_api_key` from live providers.yaml (display-name match,
then default_model-prefix match) with frozen-snapshot fallback. **Extend** the same loop
to also refresh `provider_cfg.base_url` and `provider_cfg.caller` when the matched live
card differs:

```python
# after resolving effective_api_key from live card `p`:
if p.base_url and p.base_url != provider_cfg.base_url:
    logger.info("[call-llm] live base_url override for %s: %s -> %s",
                provider_name, provider_cfg.base_url, p.base_url)
    provider_cfg.base_url = p.base_url
if p.caller and p.caller != provider_cfg.caller:
    provider_cfg.caller = p.caller.lower()
```

Verified structure: the live loop iterates `live_providers` from
`utils.providers_store.load_providers()` returning `list[ProviderConfig]` with fields
`name`, `base_url`, `api_key`, `default_model`, `caller` (utils/providers_store.py
`_from_dict`). `provider_cfg` here is the frozen `LLMProviderConfig` — mutating the
runtime's own snapshot object is safe (per-runtime copy from `load_agent_config()`; no
cross-runtime sharing).

### ui/handlers/settings_handler.py — `add_or_update` (line 79)

After the existing save (`update_provider(...)` → `save_providers`), the handler already
fires `self._on_providers_changed()`. No change needed in settings_handler itself **if**
the signal is wired at construction. Verify current state: `SettingsHandler.__init__`
takes `on_providers_changed: Callable | None`. Window currently passes `None` (wired
later via `wire_settings_handler`).

### ui/window.py — wire the signal

In the wiring section (~line 277):

```python
self._settings_handler = SettingsHandler(
    ...
    on_providers_changed=self._on_providers_changed,   # was None
)
```

Add method on MainWindow:

```python
def _on_providers_changed(self) -> None:
    arh = getattr(self, "_agent_runtime_handler", None)
    if arh is not None:
        arh.refresh_provider_config()
```

### ui/handlers/agent_runtime_handler.py — new method

```python
def refresh_provider_config(self) -> None:
    """Reload providers.yaml and update every cached runtime's config in place.

    Called when Settings saves a provider (on_providers_changed). Conversations
    and runtimes are NOT recreated — only the provider dict is swapped so
    base_url/caller/max_tokens edits take effect on the next call without an
    app restart.
    """
    from agent.config import load_agent_config
    fresh = load_agent_config()
    for name, rt in self._runtimes.items():
        # preserve per-agent default_provider override set at creation
        default_provider = rt._config.default_provider
        rt._config.providers = fresh.providers
        if default_provider in fresh.providers or default_provider == "local-kb":
            rt._config.default_provider = default_provider
```

Verified: `self._runtimes: dict[str, Any]` (line 77), iterated similarly at line 844 and
1183; `rt._config` is the `AgentConfig` passed at construction (runtime stores it as
`self._config` — referenced in `_call_llm` as `config = self._config`). In-flight turns
are unaffected (their `_call_llm` resolves per call anyway).

**Files NOT changed:**
- `utils/providers_store.py` — load/save correct; source of truth unchanged
- `agent/config.py` — `load_agent_config()` already re-reads yaml on each call
- `utils/provider_test.py` — Test Connection already reads live file

## 3. Data Flow

Save in Settings → `SettingsHandler.add_or_update` → `save_providers` (atomic yaml write)
→ `on_providers_changed` → `MainWindow._on_providers_changed` →
`AgentRuntimeHandler.refresh_provider_config()` → each cached runtime's
`_config.providers` swapped → next `send_message` → `_call_llm` reads updated snapshot
(and live-resolves api_key as today).

## 4. File Change Summary

| File | Change | ~Lines | Risk |
|---|---|---|---|
| agent/runtime.py | live base_url/caller refresh | +8 | med (hot path; guarded, logged) |
| ui/window.py | wire signal + method | +10 | low |
| ui/handlers/agent_runtime_handler.py | refresh_provider_config | +18 | low |
| tests/test_config_invalidation.py | new | ~120 | — |

## 5. Implementation Order

1. `refresh_provider_config()` + window wiring; test: create runtime → edit yaml →
   refresh → snapshot updated.
2. `_call_llm` live base_url/caller extension; test: frozen runtime with stale base_url
   resolves live card's URL on next call.
3. Full suite + ruff + pyright.

## 6. Acceptance Criteria

- [ ] Editing a provider's base_url in Settings takes effect on the **next agent turn
      without app restart** (regression test proves it)
- [ ] Editing api_key/caller/max_tokens likewise
- [ ] In-flight turns are not disturbed by a save (no exceptions in tool loop)
- [ ] Runtime with per-agent default_provider keeps that default after refresh
- [ ] Full pytest green, ruff clean, pyright clean

## 7. Edge Cases

| Case | Behavior |
|---|---|
| providers.yaml deleted mid-session | `load_providers()` returns `[]` → refresh keeps old snapshot, logs warning |
| Provider renamed while an agent's llm_name points at old name | Snapshot updated; agent's next turn falls to global default (existing behavior), log warning |
| Save during in-flight turn | Only `self._config.providers` dict swapped; turn already resolved its config — unaffected |
| `local-kb` card (placeholder key) | Unchanged semantics; live loop requires `p.api_key` truthy for key refresh only — `***` counts as truthy, fine |

## 8. ARCHITECTURE.md Updates

None required (implements the recorded delta).
