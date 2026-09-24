# SPEC-05 Sub-Phase 3b Instructions — Delete gateway/ + Handlers + Tests

**Spec:** docs/specs/SPEC-05-R1-GATEWAY-STRIP.md §2 DELETE table
**Parent plan:** SPEC-05-SUBPHASES.md (re-carved 2026-09-23)
**MICRO-phase. Scope: 8 deletions + 1 test-file edit.** Tool budget ~10 calls.
A 2nd non-test source file edit = STOP and report.

## Pre-verified (supervisor grep 2026-09-23 — trust, don't re-derive)

- Zero live `from gateway` / `import gateway` imports remain outside the delete set
  (every other hit is a comment/docstring — transport/openclaw.py:61's "kept verbatim
  from gateway/client.py" provenance note STAYS, it's history not dependency)
- SP3a already killed window.py's GatewayHandler/ConnectionSync construction
- The SP2 pin file still EXCLUDES gateway_handler.py + connection_sync_handler.py
  from its sweep (that exclusion ends THIS phase)

## DELETE (8 paths)

1. `gateway/` (client.py + __init__.py; leave __pycache__ — untracked)
2. `ui/handlers/gateway_handler.py`
3. `ui/handlers/connection_sync_handler.py`
4. `tests/test_gateway.py`
5. `tests/test_gateway_handler.py`
6. `tests/test_connection_sync_handler.py`
7. `tests/test_low345_gateway_hardening.py`
8. (Nothing else. If pyproject/pytest.ini mention any of these — check — report
  instead of editing.)

## EDIT — tests/test_no_gateway_residuals.py ONLY

1. DELETE `SP3_DELETE_TARGETS` and `test_excluded_files_still_exist_for_now` — the
   files are gone; the sweep now covers the whole ui/handlers/ dir with no exclusions.
2. The needle sweep stays as-is otherwise (10 pins total after: 9 sweep/behavioral
   + window pin). Verify `test_all_handlers_match_no_needles` still passes now that
   gateway_handler.py is swept — it should (file is deleted, not present-but-clean).
3. ADD one pin: `test_gateway_package_is_gone` —
   `importlib.util.find_spec("gateway")` is None AND
   `not (REPO / "ui/handlers" / "gateway_handler.py").exists()` AND same for
   connection_sync_handler.py.

## Verification (paste ALL, real runs)

```
.venv/bin/python -m pytest tests/test_no_gateway_residuals.py -q          # 11 expected (10 + 1 new − wait: 10 stays 10? count: sweep(1)+sendraw(1)+excluded(−1)+on_send(1)+window_shims(1)+window_construction(1)+remote_key(1)+broadened(1)+hasattr(1)+sync_source(1) = 10 → −1 excluded +1 gone = 10; report actual)
.venv/bin/python -m pytest tests/ --collect-only -q 2>&1 | tail -1         # 0 errors; count drops ~85 (4 deleted files)
.venv/bin/python -m ruff check tests/test_no_gateway_residuals.py
```

Baselines: pin file ruff 0 → 0.

## COMPLETENESS
- [ ] 8 paths deleted (git status shows D lines)
- [ ] Pin file: exclusions folded, gone-pin added
- [ ] 3 outputs pasted (pin count explained)
- [ ] Any straggler importer = collection error = STOP and report
- [ ] Deviations flagged
