# SPEC-AUDIT-CLEANUP-1 Phase 1 — Crash-Class Bug Fixes (Class A)

**Spec:** `docs/specs/SPEC-AUDIT-CLEANUP-1-LATENT-BUG-FIXES.md` — READ IN FULL before writing any code. §"Class A" is authoritative for this phase.
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load it fresh, start with the Discovery block, follow every rule.
**Supervisor:** special:supervisor | **Builder:** special:coder | **Auditor:** special:debugger

## Scope — 5 crash-class bugs, 5 files

| # | File:line | Bug |
|---|---|---|
| A1 | `gateway/client.py:118` | bare `logger` used; module defines `_logger` (line 30). Crashes when device-auth.json is missing keys. |
| A2 | `ui/handlers/feed_handler.py:978` | bare `logger` used; module defines `_logger` (line 31). Crashes on update_card for unknown card id. |
| A3 | `ui/handlers/review_handler.py:297-299` | `except Exception as e:` → deferred lambda body uses bare `e` (deleted at except-exit) → NameError inside `idle_add` callback. The f-string `f"{type(e).__name__}: {e}"` must use the captured `err` default-arg instead. |
| A4 | `ui/handlers/chat_render_handler.py:281` | `except Exception as exc:` → deferred `lambda: on_error(str(exc))` — bare `exc` deleted at except-exit. Fix: `lambda err=exc: on_error(str(err))`. |
| A5 | `ui/handlers/chat_render_handler.py:3 nested lambda, same pattern as A4. |

**Anchors are identifiers + line numbers as of `383bcc5`.** If a file drifted >10 lines, flag "Spec drift" in COMPLETENESS and proceed with identifier-anchored edits.

## Rules for this phase

1. **One commit per bug, five commits.** Each commit must include its regression test.
2. **Red-before-green (Rule 4):** for each bug, FIRST write a regression test that exercises the deferred-callback/error-path crash and CONFIRM it fails on unfixed code (paste the failure output), then fix, then confirm green. For A3/A4/A5: the test must trigger the exception path that schedules the deferred callback — not just call the error handler directly. For A1/A2: the test must call the guarded path (update_card with unknown id; device-auth missing keys path) and assert no NameError.
3. **Rule 8 — minimal diffs.** Only the buggy lines change. No import reordering, no reformatting, no comment improvements.
4. Read each file in full before editing (Rule 1). Discovery block required.
5. Evidence in the delivery: per-bug (a) the failing-test output on unfixed code, (b) the fix diff, (c) passing-test output, (d) `pytest tests/ -B` full-suite result for the affected suites.
6. Existing tests must remain green. Any test that legitimately asserted old (crashing) behavior must be updated with justification in the commit message.
7. **5 exact commits:** `fix(gateway): use _logger in device-auth schema check`, `fix(feed): use _logger in update_card not-found warning`, `fix(review): capture exception in deferred lambda`, `fix(render): capture exc in deferred error callbacks (2 sites)`, — wait, A4 and A5 are both in chat_render_handler; that's 4 commits total. Use these messages: (1) `fix(gateway): use _logger in device-auth schema check` (2) `fix(feed): use _logger in update_card not-found warning` (3) `fix(review): capture exception variable in deferred idle_add lambda` (4) `fix(render): capture exc in deferred error callbacks (2 sites)`.

## Deliverable (COMPLETENESS checklist)

- [ ] Discovery block (5 files read in full, one line each on what was learned)
- [ ] 4 commits, each with red-before-green evidence
- [ ] All 5 crash sites fixed (A4+A5 in one commit)
- [ ] `pytest tests/ -B` on affected suites: test_gateway_client, test_feed_handler, test_review_handler (or the closest existing suites — flag if none exist), test_chat_render
- [ ] Spec-drift check for each file
- [ ] Related issues flagged (not silently fixed)
- [ ] pyflakes on the 5 files: no NEW findings vs baseline
- [ ] Audit-hook: the enforcement:tests hook will run automatically on commit; if it fails, fix forward and re-commit

**Report back with the full COMPLETENESS checklist and all command outputs.** When the delivery arrives, the phase goes to Debugger for the adversarial audit (11 sections), then back to Coder for any fixes, then re-audit until clean.
