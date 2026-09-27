# SP6 Phase Instructions — Phase 1: guards + stale-guard dispositions + packaging pins

**SPEC-06 close-out · SP6 Phase 1 of 3. Env: project `.venv` (THE env).**
**HEAD at delegation: 0d9f4285 (clean). Builder: steelFramedCodeWriter.md every turn.**

## Context

SP5 complete (a/b/c1/c2/c3). SP6 = guards + packaging (this phase) → 13-red triage
(Phase 2) → post-mortem + docs + push (Phase 3, supervisor-owned).

## Item 1 — create tests/test_html_guard_sites.py (the architecture's guard pattern)

The v2 render pipeline needs its Pango-guard analogue: a source-catalog test that
pins the fail-closed posture at every converted call site. Catalog (verify each by
reading the current source first; extend if you find more):

1. `ui/views/chat_surface.py` — WebKit settings: `set_enable_javascript(False)`
   pinned (already pinned in test_chat_surface.py:149 — reference, don't
   duplicate; catalog asserts the SETTING exists in source).
2. `render/sanitize.py` — `_ALLOWED_TAGS` contains NO script/iframe/style/form;
   `_ATTRIBUTES` admits `src` ONLY with http(s) url policy; the class-token
   allowlist gates every `class` value; `p`'s additive `class` entry documented.
3. `render/html.py` — escape-first: no raw HTML emission path from text input.
4. `ui/handlers/chat_render_handler.py` — every surface append goes through
   `sanitize_html` (the render_welcome re-sanitize is belt-and-braces).
5. `ui/views/event_cards.py` — `_build_code_from_markup` Pango guard present
   (already covered by TestEventCardsCodeLabelGuard — reference it).

Style: follow test_pango_guard_sites.py's source-catalog pattern (read file text,
assert guard strings). Red-first where feasible: demonstrate one catalog entry
failing against a mutated source (then restore byte-identical).

## Item 2 — stale-guard disposition (test_gtk_container_membership.py:171)

`test_is_in_container_imported_in_chat_render` asserts an import SP4 deleted.
Disposition: the subject DIED (not moved — verify: grep `is_in_container` in CRH
= 0) → RETIRE the test, leave a one-line lineage comment (subject died with the
Pango bubble path in SP4 2a057adc; feed_tab twin guard at :176 stays live).
The feed_tab twin stays untouched.

## Item 3 — packaging pins (pyproject.toml)

- Add `nh3` (sanitizer) and `pygments` (syntax_html) to project dependencies with
  minimum bounds matching the venv: check installed versions first
  (`.venv/bin/python -c "import nh3, pygments; print(nh3.__version__, pygments.__version__)"`)
  and pin `>=` those.
- pyrightconfig.json: add `"venvPath": "."` + `"venv": ".venv"` so pyright binds
  the project env deterministically.

## Gates

1. `xvfb-run -a .venv/bin/python -m pytest tests/test_html_guard_sites.py
   tests/test_gtk_container_membership.py -q` → green; report counts.
2. Red-first evidence for at least one catalog entry (mutation → fail → restore).
3. `pip-audit`-style sanity: `.venv/bin/python -c "import nh3, pygments"` works;
   ruff on new/edited files clean (0) or exact baselines.
4. `git status` sanctioned only. No commit — Debugger audits.

## Report format
COMPLETENESS checklist mandatory; catalog-entry verification notes (each site
confirmed by reading source); related issues flagged not fixed.
