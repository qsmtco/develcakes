# SPEC-AUDIT-CLEANUP-2 Phase 1 — Root Scratch-Script Sweep (17 files)

**Spec:** `docs/specs/SPEC-AUDIT-CLEANUP-2-DEAD-CODE-SWEEP.md` — READ IN FULL. §"Phase 1" is authoritative.
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load fresh, Discovery block first, every rule.
**Supervisor:** special:supervisor | **Builder:** special:coder | **Auditor:** special:debugger
**Baseline:** tree clean at `7a2f97f` (pushed). Unit #1 lessons in force — see "Standing rules" below.

## Standing rules (carried from AC1 post-mortem — mandatory)

1. **NO `git reset` / `git reset --soft` during delivery.** If verification requires reverting a file, use `git checkout <pinned_sha> -- <file>` and verify content identity at the end with `git diff <pinned_sha> -- <file>` (empty). If the review layer auto-commits mid-work, leave the Accept commits alone — the supervisor scrubs history at unit close (PM-authorized reset is a supervisor-only, end-of-unit operation).
2. **Test invocation:** `PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3 -m pytest <suite> -q`. Never `pytest -B` (not a pytest flag). GTK suites run under `xvfb-run -a` (supersedes the segfault workaround).
3. **Deletion rule (spec):** every deletion must be re-grepped immediately before deleting. Zero-reference = delete. ANY hit outside the file itself = do not delete, report.
4. **Rule 8 (minimal diffs):** this phase deletes files; do not modify any surviving file except where the instructions explicitly say so.

## Scope — delete exactly these 17 root-level scratch scripts (all re-verified 2026-09-07: present, zero importers)

```
_audit_verify.py        _test_htmlescape.py     _verify_phase1.py
diagnose_drawer_gap.py inspect_filetree_drawer.py
extract_architecture.py extract_context.py       extract_create_project.py
extract_prompt_section.py extract_sections.py   extract_window_lines.py
find_inventory.py       find_lines.py            find_section.py
read_arch_section.py    read_bytes.py            read_section.py
```

`main.py` is NOT in this list — it is the entry point. Do NOT touch it (the `main.py` comment cleanup is Phase 5).

## Procedure

1. **Per-file re-grep before deletion (spec's hard rule):** for each of the 17 basenames, run `grep -rn "<basename>" --include='*.py' .` excluding the file itself and `.git`. Expected: zero hits. Any hit = report, don't delete that file.
2. **Delete** all 17 (rm). If any file fails the grep gate, delete only the clean ones and report the failure.
3. **Verify nothing broke:**
   - `python3 -c "import main"` — wait, main.py imports Gtk; use `PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile main.py` and `PYTHONDONTWRITEBYTECODE=1 python3 -c "import agent.runtime"` (a heavy production import that would catch any accidental dependency on scratch files).
   - Run these suites (they cover the app's import surface): `tests/test_architecture.py` (expect the SAME 2 red-by-design/pre-existing failures: left_panel.py:15 real violation, gtk_containers.py carve-out — NOT new failures), `tests/test_gateway.py`, `tests/test_command_handler.py`. Paste full output.
   - **pyflakes sanity:** `/tmp/pf-venv/bin/pyflakes agent ui models utils gateway scripts main.py | grep -c "undefined name"` → must stay **0** (AC1 gate must not regress).
4. **LOC accounting:** report `git diff --stat HEAD~1` (or the commit's stat) showing net deletions; the expected ballpark is ~591 LOC minus the header comments of any file you couldn't delete.

## Commit (1)

`chore(cleanup): remove 17 root-level scratch scripts (591 LOC, zero references — SPEC-AUDIT-CLEANUP-2 Phase 1)`

If any file failed the grep gate: same commit for the clean subset, and the failure report goes in your delivery message (do NOT force the deletion).

## COMPLETENESS checklist

- [ ] Discovery block (what the 17 files are, one line each — read enough of each to confirm it's a one-off diagnostic, not a tool; if any file turns out to be imported at runtime somewhere the grep missed, STOP and report)
- [ ] Per-file grep outputs (17 greps — can be a single scripted loop with the output pasted once)
- [ ] Commit with the exact message above
- [ ] Verification outputs pasted: py_compile/import smoke, 3 suites, pyflakes count
- [ ] LOC accounting pasted
- [ ] Related issues flagged, not silently fixed (e.g. if you find ANOTHER scratch script not on the list — flag, don't delete)

**STOP and report after this phase.** Debugger audits, then Phase 2 (task-system retirement) is delegated separately.
