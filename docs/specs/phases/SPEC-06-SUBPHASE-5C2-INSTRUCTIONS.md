# SP5c-2 Phase Instructions — chat_bubble.py deletion + event-card relocation

**SPEC-06 R2A HTML-CHAT · sub-phase SP5c-2. Env: project `.venv` (THE env).**
**HEAD at delegation: b67c3e41 (clean). Builder: steelFramedCodeWriter.md on every turn.**

## Background (verified at HEAD)

- chat_bubble.py (1,112 lines) has exactly TWO production import sites left:
  1. `ui/window.py:504-512` — `_on_crabcards_extracted` callback + `_set_crabcards_registry`
     import. **DEAD since SP4** (CRH no longer invokes `set_on_crabcard_extracted`) —
     confirmed by Debugger probes (sp4_probe.py §F, sp5b audit BUG#3).
  2. `ui/handlers/chat_render_handler.py:820-835` — five builders for
     `render_event_card` (the Pango event-card pipeline SP4 explicitly preserved:
     build_role_bubble, create_file_card, create_edit_card, create_tool_card,
     create_error_bubble).
- main_content.py's welcome import was already deleted in SP5c-1.
- Known residuals to fold (from retro-audit #3 +Debugger): `agent/runtime.py:796`
  comment example, `ui/handlers/project_handler.py:180` comment.

## Phase A — relocate the five builders to `ui/views/event_cards.py` (no deletion yet)

1. Create `ui/views/event_cards.py`: move the five builders + their private helpers
   VERBATIM (copy bodies, do not rewrite). Preserve `_set_crabcards_registry` in the
   module only if Phase C needs it — survey first; it is dead in production, so
   default = leave it behind in chat_bubble.py for Phase B deletion.
2. `ui/handlers/chat_render_handler.py:820-835`: repoint the lazy import to
   `ui.views.event_cards`. Nothing else in the file changes.
3. Run: `xvfb-run -a .venv/bin/python -m pytest tests/test_chat_render_handler.py
   tests/test_presentation_injection.py tests/test_streaming.py -q` — paste output.
4. Grep proof: `grep -n "chat_bubble" ui/handlers/chat_render_handler.py` → 0 hits.

## Phase B — delete chat_bubble.py + sweep the two comments

1. `git rm ui/views/chat_bubble.py`.
2. `ui/window.py:504-512`: delete the dead `_on_crabcards_extracted` block INCLUDING
   the `set_on_crabcard_extracted` registration call — but first grep
   `set_on_crabcard_extracted` across the tree; if any OTHER live caller exists,
   STOP and report instead of deleting.
3. Fold residuals: `agent/runtime.py:796` example → `Coder:develcakes`;
   `project_handler.py:180` comment → drop the "🦀 CrabCakes" app-name phrasing.
4. Grep proofs (each must be 0): `grep -rn "chat_bubble" agent/ ui/ utils/ main.py
   transport/ render/ models/ scripts/`; `grep -rn "from ui.views.chat_bubble"
   tests/` (tests handled in Phase C).

## Phase C — test dispositions

Per file (12 census files), DISPOSITION TABLE — apply exactly:
- **RELOCATE-REPOINT** (import from `ui.views.event_cards` instead):
  test_activity_bubbles.py, test_streaming.py, test_presentation_injection.py
  (DONE in B), test_gtk_safe_link.py (imports DONE in B; comment :91 still
  sweeps), test_low7_image_viewer.py, test_chat_heading.py, test_chat_task_segment.py,
  test_chat_terminal_segment.py.
- **DISPOSITION CORRECTION (supervisor, post-survey):** test_chat_heading /
  test_chat_task_segment / test_chat_terminal_segment were originally ruled RETIRE
  ("bubble-internals, subject dies with the file") — that premise is WRONG: their
  subjects (_build_heading/task/terminal_segment) were relocated VERBATIM to
  event_cards in Phase A (:696/:778/:752). The files are pure moved-subject
  coverage (zero welcome/registry content — verified). RELOCATE-REPOINT them.
- **CATALOG PRUNE** (remove chat_bubble entries from the guard catalog, keep the
  rest): test_pango_guard_sites.py.
- **NO-OP:** test_agent_runtime.py (test-name hit only); test_escaping.py +
  test_wiring_low7_project_path.py (comments already swept in B.1).
- **COMMENT SWEEP (fold-in from Phase-B audit):** test_gtk_safe_link.py:91 +
  test_low7_image_viewer.py:3 module-name mentions.
- Also: `tests/test_welcome_bubble.py` was already retired in SP5c-1 (gone);
  confirm no references to it remain anywhere.

## Gates (all phases; report exact numbers)

1. Phase A: targeted trio green; grep proof window.py untouched except Phase B block.
2. Phase B: both grep proofs 0; full-suite relevant slice green.
3. Phase C: full test batch (all disposition files + chat_render_handler +
   chat_surface + welcome_html + left_panel) green under xvfb; report before/after
   counts per file; the 2 known pre-existing `_Spy` failures in test_agent_runtime
   are EXPECTED (SP5c test-drift — do not fix here, do not count as failures of
   this round; NOTE: if your disposition work obsoletes those failing tests, retire
   them and say so).
4. Ruff/pyright: exact per-file baselines vs HEAD (measure first; the moved code
   keeps its lint character — expect event_cards.py to inherit chat_bubble's
   counts; document the mapping).
5. `git status` = sanctioned files only. No commit — Debugger audits, supervisor
   verifies, then commits.

## Report format

Per phase: files changed (line numbers), grep outputs, test outputs, COMPLETENESS
checklist (mandatory — missing checklist = rejected delivery), related issues
flagged not fixed.

---

# PHASE B ADDENDUM (post Phase-A audit — Debugger cleared, 3 LOW items folded)

Audit source: project chat (SP5c-2 Phase A audit); probes .debug/audit-scratch/sp5c2-*.py.

## B.0 SUPERVISOR RULING — crabcard registry (audit BUG #1): DELETE the machinery

The registry is split-brain after Phase A (reader in event_cards, writer dying with
chat_bubble). Ruling: in `ui/views/event_cards.py`, DELETE `_crabcards_registry`
(:45) and the registry-reading branch of the placeholder segment (:509/:516 area)
— the placeholder ALWAYS renders its static fallback label (identical to today's
behavior: the registry is never populated; no test populates it — 0 consumers,
grep-verified). `_set_crabcards_registry`/`_clear_crabcards_registry` die with the
donor. Leave a one-line lineage comment: crabcard interception wiring died with
SP4; placeholder is static since. Expect event_cards ruff to shift if the deleted
branch carried findings — document the new count.

## B.1 Comment-sweep additions (audit BUG #2 + #3 + Coder's Phase-A flags)

All comment-only; production dirs must reach 0 hits on the B gate grep:
- `ui/views/event_cards.py:~478-482` — correct the print-constant provenance note:
  the path is REACHABLE (CRH:851 thinking-branch builds role bubbles with no
  forward handler); the rename was grep hygiene, not dead-code cleanup.
- `tests/test_escaping.py:347` — stale "chat_bubble.py" comment (file being deleted).
- `tests/test_wiring_low7_project_path.py:6/:52` — same.
- `ui/window.py:540`, `ui/wiring.py:17/:33`, `ui/handlers/agent_runtime_handler.py:2025`,
  `ui/views/diff_card.py:130`, `utils/crabcard_parser.py:20` — comment references to
  chat_bubble; reword to event_cards (or drop the module name) per what each
  comment actually describes.
- Plus the two residuals already in Phase B scope: `agent/runtime.py:796`
  (example → `Coder:develcakes`), `ui/handlers/project_handler.py:180`.

## B.2 Phase-C census corrections (audit-confirmed, apply in Phase C)

- `test_escaping.py` + `test_wiring_low7_project_path.py` = NO-OP (comment hits
  only — comments swept in B.1 above).
- `test_agent_runtime.py` = NO-OP (test-name hit only).

## Phase B gates (unchanged + one addition)

All Phase B gates stand. ADDITION: after the registry deletion, run
`xvfb-run -a .venv/bin/python -m pytest tests/test_chat_render_handler.py
tests/test_presentation_injection.py tests/test_gtk_safe_link.py -q` — the
placeholder rendering path must stay green (it is exercised here). Report the
event_cards.py ruff delta with the deleted-branch accounting.
