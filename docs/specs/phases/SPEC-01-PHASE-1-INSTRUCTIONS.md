# SPEC-01 Phase 1 Instructions — Provider-Config Refresh Path

**Spec:** docs/specs/SPEC-01-CONFIG-INVALIDATION.md (read it IN FULL first)
**Architecture:** .crabcakes/architecture.md §Modules/agent (bug-fix delta #1)
**Phase 1 of 2** — handler refresh path + window wiring + tests. Phase 2 (runtime
`_call_llm` live-lookup extension) follows separately. DO NOT implement Phase 2 now.

## Scope — exactly these 3 files

1. `ui/handlers/agent_runtime_handler.py` — add `refresh_provider_config()`
2. `ui/window.py` — wire `on_providers_changed` + add `_on_providers_changed()`
3. `tests/test_config_invalidation.py` — NEW file, tests for this phase

Nothing else. No scope creep.

## Task 1 — AgentRuntimeHandler.refresh_provider_config()

Add a public method to `AgentRuntimeHandler` (class starts ~line 33; method placement
near the lifecycle block ~line 840–950 is natural, after `_get_runtime`):

```python
def refresh_provider_config(self) -> None:
    """Reload providers.yaml and update every cached runtime's config in place.

    Called when Settings saves a provider (on_providers_changed). Conversations
    and runtimes are NOT recreated — only the provider dict is swapped so
    base_url/caller/max_tokens edits take effect on the next call without an
    app restart. (SPEC-01: stale-cache-divergence fix.)
    """
    from agent.config import load_agent_config
    fresh = load_agent_config()
    for name, rt in self._runtimes.items():
        # preserve per-agent default_provider override set at creation
        default_provider = rt._config.default_provider
        rt._config.providers = fresh.providers
        if default_provider in fresh.providers or default_provider == "local-kb":
            rt._config.default_provider = default_provider
    if self._runtimes:
        logger.info("refresh_provider_config: updated %d runtime(s)", len(self._runtimes))
```

Verified facts you can rely on:
- `self._runtimes: dict[str, Any]` — dict of display_name → AgentRuntime (line 77)
- Runtimes store their config as `rt._config` (an `AgentConfig` with `.providers`
  dict and `.default_provider` string) — verified via `_call_llm` reading
  `config = self._config` in agent/runtime.py
- `load_agent_config()` re-reads providers.yaml from disk on every call
- Iterating `self._runtimes.items()` matches existing patterns (lines 844, 1183)

## Task 2 — ui/window.py wiring

Current state (verified): `SettingsHandler` constructed ~line 277 with
`on_providers_changed=None` (comment says "wired via wire_settings_handler below" —
grep for `wire_settings_handler` to find the actual wiring call; if that method exists
and already assigns the callback later, wire THERE instead of the ctor, keeping the
existing pattern).

Wire it so a provider save reaches the runtime handler:

```python
def _on_providers_changed(self) -> None:
    arh = getattr(self, "_agent_runtime_handler", None)
    if arh is not None:
        arh.refresh_provider_config()
```

Guard with `getattr(..., None)` — settings can save before the runtime handler is
constructed (first-run wizard ordering).

## Task 3 — tests/test_config_invalidation.py (NEW)

Model on existing handler-test patterns (see tests/test_settings_handler.py fixtures:
`tmp_config_dir` monkeypatching `utils.config.get_config_dir` to a temp dir). Tests:

1. `test_refresh_updates_cached_runtime_providers` — build handler with a fake/real
   runtime in `_runtimes` (a minimal stub object with `_config` is fine — set
   `rt._config = SimpleNamespace(providers={...old...}, default_provider="p1")`),
   rewrite providers.yaml in tmp dir with a changed base_url, call
   `refresh_provider_config()`, assert stub's `_config.providers` reflects the new
   card and `default_provider` preserved.
2. `test_refresh_preserves_default_provider_not_in_new_set` — default_provider="gone"
   after refresh: per spec, condition keeps it only if in new providers or local-kb;
   assert the field is unchanged when not found (spec says keep — no crash).
3. `test_refresh_with_no_runtimes` — empty `_runtimes`: no-op, no exception.
4. `test_on_providers_changed_routes_to_runtime_handler` — MainWindow-level: too heavy
   to construct; instead unit-test the wiring by asserting the method exists on
   MainWindow and calls through with a mock (inspect signature + mock attribute).
   If MainWindow construction is required by existing test patterns, follow
   tests/test_window_settings_wiring.py conventions (read it first).
5. `test_settings_save_fires_callback` — SettingsHandler.add_or_update with a valid
   provider card fires `on_providers_changed` once (follow
   tests/test_settings_handler.py::_make_provider fixture shape).

## Verification (run + paste outputs)

```
python -m pytest tests/test_config_invalidation.py -v
python -m pytest tests/test_settings_handler.py tests/test_window_settings_wiring.py -q
ruff check ui/handlers/agent_runtime_handler.py ui/window.py tests/test_config_invalidation.py
ruff format --check ui/handlers/agent_runtime_handler.py ui/window.py tests/test_config_invalidation.py
pyright ui/handlers/agent_runtime_handler.py ui/window.py 2>&1 | tail -5
```

## COMPLETENESS report (required in your reply)

- [ ] File 1 changed (list added method + line range)
- [ ] File 2 changed (list wiring point)
- [ ] File 3 created (test count)
- [ ] All 5 verification outputs pasted
- [ ] Spec drift noted (any line-number drift >10 lines from spec)

Report "SPEC-01 PHASE-1 COMPLETE" with the checklist and outputs, or report blockers
with exact reproduction. Flag related issues — do NOT fix them silently.
