# SP5c-3 Phase Instructions — register cleanup + _Spy drift fix

**SPEC-06 · SP5c-3 (final SP5c round). Env: project `.venv` (THE env).**
**HEAD at delegation: 40a16e2f (clean). Builder: steelFramedCodeWriter.md every turn.**

## Context

SP5c-2 closed the chat_bubble retirement; the carried register has 3 items. This
round clears them. Small scope — do not expand.

## Item 1 — _Spy get_parent drift (2 known failures)

`tests/test_agent_runtime.py` classes `TestEndStreamingExplicitNameTakesPriority`
(:3322) and `TestEndStreamingFallbackForGatewayAgents` (:3378) fail because their
local `_Spy` doubles (defined at :3341 and :3391) lack `get_parent()`. SP5a's
mount-once guard in `chat_render_handler.py` (~:233, `_surface_for` path) calls
`s.get_parent()` when deciding whether the surface is already mounted — the double
must answer.

**Fix:** add to BOTH `_Spy` classes:
```python
def get_parent(self):
    return None  # unmounted — mirror a fresh surface (mount path taken)
```
Rationale: these tests assert append_message content, not mount semantics; None =
"not mounted" is the neutral double behavior that reaches the append path.

## Item 2 — dead API deletion (CRH set_on_crabcard_extracted)

`chat_render_handler.py:620-626` (`set_on_crabcard_extracted` def + the
`self._on_crabcard_extracted = cb` assignment) — 0 callers repo-wide (verified
across audits). Also remove the `self._on_crabcard_extracted` initialization in
`__init__` (grep `_on_crabcard_extracted` in the file — take ALL hits; leave a
one-line lineage comment at the former def site: died with SP5c-2 B.2's window
callback removal).

## Item 3 — event_cards F401 pair

`ui/views/event_cards.py:39` — `is_crabcards_placeholder, get_placeholder_index`
imported, unused since B.0 made the placeholder static. Delete the import line
(the parser functions themselves stay in utils/crabcard_parser.py — they are its
public API; only the event_cards import dies). Confirm ruff event_cards 13 → 11
(2 F401 gone).

## Gates

1. Targeted: the two test classes green — `xvfb-run -a .venv/bin/python -m pytest
   tests/test_agent_runtime.py -k "TestEndStreamingExplicitNameTakesPriority or
   TestEndStreamingFallbackForGatewayAgents" -q` → 2 passed.
2. Full `tests/test_agent_runtime.py` under xvfb → **218 passed, 0 failed** (the
   round's headline: first fully-green agent_runtime since SP5a).
3. CRH suite green (`tests/test_chat_render_handler.py`); grep proofs:
   `_on_crabcard_extracted` and `set_on_crabcard_extracted` → 0 hits repo-wide;
   `is_crabcards_placeholder` → 0 hits in ui/.
4. Ruff/pyright exact baselines on touched files (CRH 13, event_cards 13→11,
   test file 4).
5. `git status` = sanctioned files only. No commit — Debugger audits.

## Report format

Per item: fix, file:line, gate outputs. COMPLETENESS checklist mandatory.
Related issues flagged, not fixed.
