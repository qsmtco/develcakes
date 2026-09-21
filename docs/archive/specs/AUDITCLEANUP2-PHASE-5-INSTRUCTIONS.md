# SPEC-AUDIT-CLEANUP-2 Phase 5 — Scripts, Comments, Pycache (final housekeeping)

**Spec:** `docs/specs/SPEC-AUDIT-CLEANUP-2-DEAD-CODE-SWEEP.md` — READ IN FULL. §"Phase 5" is authoritative.
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load fresh, Discovery block first, every rule.
**Supervisor:** special:supervisor | **Builder:** special:coder | **Auditor:** special:debugger
**Baseline:** tree clean at `fef2f05`. Standing rules in force (no resets, xvfb-run -a, pyflakes gate, re-grep before every deletion, ARCHITECTURE.md same-commit rule).

## Supervisor-verified surface (2026-09-07, this phase — trust but re-grep)

| Item | Verified state |
|---|---|
| `scripts/audit_attack_scenarios.py` | exists (128 LOC), zero `.py` refs outside itself |
| `scripts/audit_streaming_scenarios.py` | exists (287 LOC), zero `.py` refs outside itself |
| `scripts/bulk_repair_empty_assistant.py` | exists (194 LOC) — **SPEC GAP FOUND: has its own 21-test suite** `tests/test_bulk_repair_empty_assistant.py` (imports + tests the script). Delete BOTH. |
| `docs/ARCHITECTURE.md:204-205` | directory-tree entry lists both audit scripts — **must be updated same-commit** (doc-lie rule; there are TWO tree listings: ~:204 and ~:4366 — check both) |
| `main.py:59-62` | 4 trailing dev comments (`# test change`, `# actual test change`, `# new uncommitted change`, `# another test`) |
| `__pycache__` dirs | purge repo-wide (not committed — gitignored; the point is preventing stale-.pyc traps) |

## Procedure

1. **Re-grep each script basename** (exclude self + `.git`). Expected: zero `.py` hits. (Docs/specs/post-mortems retain historical mentions — flag, don't edit.)
2. **Delete:** `rm scripts/audit_attack_scenarios.py scripts/audit_streaming_scenarios.py scripts/bulk_repair_empty_assistant.py tests/test_bulk_repair_empty_assistant.py`
3. **`docs/ARCHITECTURE.md`:** remove the two directory-tree lines for the audit scripts in BOTH listings (~:204-205 area and ~:4366-4367 area — locate by grep `audit_attack_scenarios`; the tree entries under `scripts/` should end with `rebuild_kb_index.py` after this). Check also for any `bulk_repair` mention (verified: none).
4. **`main.py`:** delete the 4 trailing dev comment lines at the file end. Read the file tail first; keep the `sys.exit(app.run(None))` line and everything above it.
5. **Pycache purge:** `find . -name __pycache__ -type d -not -path "./.git/*" -exec rm -rf {} +` (not part of the commit — housekeeping).
6. **ENV NOTE:** `exec_command` may be PM-gated this session. If your shell is blocked, use your file tools for edits and ask the supervisor to run deletions/verifications. Report which channel you used.

## Verification (all pasted)

1. Post-deletion greps for the 4 deleted paths → zero in `.py` (docs may retain history).
2. `docs/ARCHITECTURE.md` grep `audit_attack_scenarios\|audit_streaming_scenarios\|bulk_repair` → zero hits.
3. `PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile main.py` OK + `python3 -c "import agent.runtime"` smoke.
4. pyflakes full-tree: undefined-name **0** (regression gate); total findings may drop slightly (the deleted files carried their own findings — paste before/after counts).
5. `xvfb-run -a` suites: `tests/test_gateway.py`, `tests/test_command_handler.py` (sanity that nothing imported the scripts), and the full-suite delta check: the deleted `test_bulk_repair_empty_assistant.py` had ~21 tests — the new full-suite collected count should drop accordingly (paste the count; no new failures).
6. LOC accounting pasted.

## Commit (1)

`chore(cleanup): remove 3 one-off scripts + their test suite, main.py dev comments, ARCHITECTURE.md tree entries (SPEC-AUDIT-CLEANUP-2 Phase 5)`

## COMPLETENESS checklist
- [ ] Discovery block
- [ ] Re-greps pasted (4 paths)
- [ ] Both ARCHITECTURE.md tree listings updated (grep-proven)
- [ ] main.py tail cleaned (py_compile OK)
- [ ] Pycache purged (paste the find output count)
- [ ] All verification outputs pasted
- [ ] Related issues flagged, not silently fixed

**This closes Phase 5 — the final phase of the sweep.** After your report + audit: supervisor runs the unit-acceptance gates (full-suite baseline comparison, pyflakes, content-identity), then post-mortem + push.
