# SPEC-07 Subphases — R4 Feedbar Removal

| SP | Scope | Files | Status | Commit |
|----|-------|-------|--------|--------|
| SP1 | Pill adapter + surface seam (`set_activity_status`, `ActivityPillAdapter`, `surface_for_key`) + tests red-first | chat_surface.py, chat_render_handler.py, +tests/test_activity_pill_adapter.py | ✅ done (audit: 1 HIGH + 1 LOW + 1 fold-in, all fixed) | 7103640f |
| SP2 | Handler repoint (ctor/attr rename, `_update_status` plain text, progress render calls deleted) + window.py lazy resolver + 4 test files repointed | activity_handler.py, window.py, 4 test files | pending | — |
| SP3 | feedbar.py deleted; window unwired; uirsp3 widget tests retired WITH dispositions; SP1 docstring scrub; ARCHITECTURE.md note | feedbar.py (del), window.py, test_uirsp3_phase2.py, chat_surface.py docstrings, ARCHITECTURE.md | pending | — |
| SP4 | Close-out: full suite + ruff + pyright, 11-section post-mortem, commit, push | post-mortem file | pending (supervisor-owned) | — |

Loop rules in force: every code-bearing turn gets the Debugger adversarial audit
(implementationLoop.md §3.1a) BEFORE commit; Coder gets steelFramedCodeWriter.md in
every build delegation; Debugger gets adversarialDebugger.md in every audit delegation.

Pre-flight verification (2026-09-25, HEAD 3972ac9e) corrected 5 spec-vs-code drifts —
see SPEC-07 §2 AMENDED. Registers carried into this spec: subject-alive-grep-before-
retire (SP3 Edit 4), unfiltered-file-runs preference (no -k gates).
