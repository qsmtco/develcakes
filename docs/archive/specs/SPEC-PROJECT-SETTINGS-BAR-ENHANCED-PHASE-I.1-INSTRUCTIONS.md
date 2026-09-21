# Implementation Phase I.1 — CSS (project settings bar interactive elements)

**Spec:** `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-3.md` §2.4 (CSS section)
**Prompt to load:** `prompts/steelFramedCodeWriter.md`
**Working dir:** `/path/to/projects/crabcakes`

## Task

Add the CSS rules for the three new interactive elements in the project settings bar to `ui/styles.py`. This is the lowest-risk phase: pure CSS, no Python logic, no cross-file dependencies.

## File to change

**`ui/styles.py`** — add the new CSS rules. The spec (§2.4) defines:

```css
/* Project settings bar — interactive elements */
.project-bar-agent { ... }
.project-bar-agent:hover { ... }
.project-bar-agent:active { ... }
.project-bar-autoaccept { ... }
.project-bar-autoaccept:hover { ... }
.project-bar-autoaccept:active { ... }
.project-bar-gear { ... }
.project-bar-gear:hover { ... }
```

## Rules

- Use the `steelFramedCodeWriter.md` prompt at `prompts/steelFramedCodeWriter.md`
- Read `ui/styles.py` in full before editing. The spec's §2.4 cites the existing `.project-feed-bar` rule near the project settings bar styling. Anchor your edit to that rule, not a line number.
- Run `grep -n "project-bar" ui/styles.py` before AND after the edit. Confirm: before = 0 matches (these are new classes); after = the count matches the spec's CSS block.
- Run `python3 -c "import ui.styles"` (or the project's import smoke test) to confirm no CSS parse errors.
- Per `steelFramedCodeWriter.md` Step 6.6, scan the surrounding CSS for any related-but-stale rules (e.g., a `.project-feed-bar` rule that references the old label-only design).

## COMPLETENESS checklist (required)

```
COMPLETENESS:
- [x] Edit 1: Added CSS rules for .project-bar-agent (+ :hover, :active) — evidence
- [x] Edit 2: Added CSS rules for .project-bar-autoaccept (+ :hover, :active) — evidence
- [x] Edit 3: Added CSS rules for .project-bar-gear (+ :hover) — evidence
- [x] grep "project-bar" ui/styles.py before: 0 matches — output
- [x] grep "project-bar" ui/styles.py after: [N] matches — output
- [x] python3 -c "import ui.styles" — no parse errors — output
```

Report back with COMPLETENESS + verification evidence. Please write when done.
