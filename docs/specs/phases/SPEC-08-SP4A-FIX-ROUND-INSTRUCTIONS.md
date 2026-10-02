# SPEC-08 SP4A FIX ROUND — audit BUG #1–#3 (Debugger, 2026-10-01)

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.

## BUG #1 (bug, blocking) — stale closure capture kills the production banner

`receiver = getattr(self, "_on_store_migration_callback", None)` runs inside
`__init__` (before ARH wires it) and captures None into the closure; dispatch
always falls to the log fallback. The adjacent `_on_progress_guarded` reads at
fire time — correct pattern.

**Fix:** in `_on_complete_guarded`, read the attribute AT DISPATCH TIME:

```python
receiver = getattr(self, "_on_store_migration_callback", None) or _log_store_migration_banner
receiver(stats)
```

Remove the launch-time snapshot entirely. Reconcile `set_on_store_migration`'s
docstring if it implies pre-wire capture.

**Test** `test_production_order_banner_fires` (the currently-impossible-to-fail
gap): fake the `_migrate_store_async` seam to capture `on_complete`; construct
AgentRuntime (flag path inert); call `set_on_store_migration(receiver)` AFTER
construction; fire the captured `on_complete(stats)` → receiver fired with the
stats, `_log_store_migration_banner` NOT called (patch it to raise/record so the
test fails if the fallback wins). Prove it red against the current code first.

## BUG #2 (issue) — tz-aware Z timestamps mix into naive conversations

`_TS_NOW` writes `...%fZ` → `fromisoformat` yields AWARE; JSON path is naive.
**Fix:** in `_message_from_data` (the store-path hydration): after parse,
`if ts.tzinfo is not None: ts = ts.replace(tzinfo=None)` — parse-side normalization
(covers legacy rows; no DB migration). One-line + comment (the naive-parity rule).
**Test:** `test_store_path_timestamps_are_naive` — hydrate ≥2 messages → every
`messages[i].timestamp.tzinfo is None`; include one hand-planted aware-form row.

## BUG #3 (suggestion) — stale `__init__` comment

runtime.py:562-565 still says the flag is "default OFF" — rewrite: default ON in
production (main.py setdefault, =0 override), pinned OFF suite-wide by conftest.

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
env -u DISPLAY .venv/bin/python -m pytest tests/test_agent_persistence.py tests/test_migration.py tests/test_transcript_store.py -v
.venv/bin/python -m pyright agent/persistence.py agent/runtime.py ui/handlers/agent_runtime_handler.py tests/test_migration.py tests/test_agent_persistence.py
python -m ruff check agent/persistence.py agent/runtime.py ui/handlers/agent_runtime_handler.py tests/test_migration.py tests/test_agent_persistence.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q --ignore=tests/test_enforcement.py --ignore=tests/test_mcp_config.py -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] BUG#1: dispatch-time receiver read + production-order test (RED-then-GREEN proof)
- [ ] BUG#2: tzinfo normalization + naive test (incl. planted aware row)
- [ ] BUG#3: comment rewrite
- [ ] Full battery green incl. pyright + full suite
- Related issues found, not fixed: <list or none>
