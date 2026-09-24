# SPEC-05 Sub-Phase 3c Instructions — Config Strip + Connect-Button Transport Stub

**Spec:** docs/specs/SPEC-05-R1-GATEWAY-STRIP.md §2 (utils/config.py strip + toolbar Connect)
**Parent plan:** SPEC-05-SUBPHASES.md (re-carved; this is the c1+c2 fold — both pieces are
tiny, survey shows ~10 refs total, ONE round is right-sized)
**MICRO-phase. Scope: exactly 3 files** — `utils/config.py`,
`ui/toolbar.py`, `tests/test_no_gateway_residuals.py` (pin additions only).
Tool budget ~12 calls. A 4th file = STOP and report.

## Pre-verified (supervisor grep 2026-09-23)

- `get_gateway_url` (config.py:51) + `get_identity_dir` (:59) — ONLY other referencer is
  `tests/test_config.py` (their own unit tests). Zero production callers post-SP3b.
- toolbar.py: Connect button at :51-56, tooltip :55 says "gateway server" (LOW-9),
  status label machinery at :43+. The button handler is already SP3a's honest no-op
  (window.py) — toolbar.py only carries the WIDGET + tooltip text.
- SPEC-05 §6 acceptance: "Connect button present, toggles, honest 'no transport' state."

## Task 1 — utils/config.py: delete both functions

Delete `get_gateway_url()` and `get_identity_dir()` (incl. their docstrings). No
tombstones (R3). If either is referenced in a config.py docstring elsewhere, reword.

## Task 2 — tests/test_config.py: delete the dead unit tests

Find the test cases covering the two deleted functions (grep the file; ~6 gateway refs
from the survey). Delete ONLY those cases. Enumerate them in the report.

## Task 3 — ui/toolbar.py: honest transport-toggle state

1. Tooltip :55: "Connect to the gateway server" →
   "Toggle remote transport (none configured — Telegram arrives post-MVP)".
2. The status label (:43 region): if it has gateway-state strings ("● Connected" etc.),
   repoint to a single honest state: "no transport" displayed when the button's
   callback reports no transport (SP3a's no-op already logs; the label should show
   the user something). KEEP the state machinery — SP3c keeps it minimal: label text
   set to "no transport" on connect-click when no transport exists. Do NOT build
   toggle logic beyond that (the button stays effectively inert; the label is the
   honest surface).
3. If the button has connect-state styling (sensitive/css classes tied to gateway),
   verify it doesn't reference dead state; adjust minimally.

## Task 4 — pin additions (tests/test_no_gateway_residuals.py)

1. `test_config_has_no_gateway_functions` — utils/config.py source contains neither
   `get_gateway_url` nor `get_identity_dir`.
2. `test_toolbar_tooltip_honest` — ui/toolbar.py source does not contain "gateway".

## Verification (paste ALL, real runs)

```
.venv/bin/python -m pytest tests/test_no_gateway_residuals.py -q            # 11 expected (9+2)
.venv/bin/python -m pytest tests/test_config.py tests/test_transport_package.py -q
.venv/bin/python -m ruff check utils/config.py ui/toolbar.py tests/test_no_gateway_residuals.py tests/test_config.py
.venv/bin/pyright utils/config.py ui/toolbar.py 2>&1 | tail -1
```

Baselines (measure first, report old→new; deletions should only drop):
config.py + toolbar.py ruff/pyright — measure. test_config.py ruff — measure.

## COMPLETENESS
- [ ] Both functions deleted; dead test cases enumerated
- [ ] Tooltip honest; label shows "no transport" on click
- [ ] 2 new pins; 11/11 green
- [ ] 4 outputs pasted with old→new
- [ ] Deviations flagged (4th file = STOP)
