# PHASE 7 of 7 — agent file context includes the per-project prompt library

**Spec:** `docs/specs/SPEC-PROJECT-PROMPTS-DIRECTORY.md` (§2.9 as re-titled; §8 ARCHITECTURE updates)
**Scope:** exactly 3 files — `agent/context.py`, `tests/test_agent_context_prompts.py` (NEW), `docs/ARCHITECTURE.md` (docs section only). Nothing else.

## Why

The root problem this spec fixes: an agent briefed to read `prompts/steelFramedCodeWriter.md` hits the sandbox (`agent/tools.py:_resolve_project_path`) because the app-level prompts dir is outside any open project. After Phases 1–6 the library lives at `<project>/.crabcakes/prompts/` — inside the sandbox. This phase also makes the library VISIBLE in the agent's file context so it doesn't need to know a specific filename to discover prompts.

## Changes

### File 1: `agent/context.py`

Read the file first — especially `DOC_NAMES` (~line 162) and `build_file_context` (~line 269). Then:

1. Module constants near other caps:
```python
_PROJECT_PROMPTS_CONTEXT_CAP = 20 * 1024   # 20KB per prompt file
_PROJECT_PROMPTS_MAX_FILES = 30
```

2. New function (place near the other `.crabcakes/` doc loaders):
```python
def _load_project_prompts_context(project_path: str) -> str:
    """
    Read .crabcakes/prompts/*.md into the agent's file context.
    Each file becomes a ``## .crabcakes/prompts/{stem}`` section, capped at
    20KB per file and 30 files total. Subdirectories (e.g. default_agents/)
    are NOT included here — those load via other paths; including them would
    double context size. Returns '' if the dir does not exist or is unreadable.
    """
```
Behavior: sorted top-level `.md` FILES only; skip files over cap; read with `encoding="utf-8", errors="replace"`; wrap ALL filesystem access in try/except OSError returning `""`/skipping per-file (match the defensive style of the existing `.crabcakes/` doc loaders); section header uses `fname[:-3]` stem.

3. Wire into `build_file_context`: append the result AFTER the standard `.crabcakes/{architecture,...}.md` docs block and BEFORE whatever follows (project tree etc.). Add one comment line at the wiring point:
```python
    # SPEC-PROJECT-PROMPTS-DIRECTORY: project prompt library after the core
    # docs — methodology first, then reference material, then the tree.
```
Find the exact insertion anchor yourself by reading build_file_context; do not guess offsets.

### File 2 (NEW): `tests/test_agent_context_prompts.py`

Follow existing agent/context test conventions (find them — likely tests/test_context* or similar; if none exist, plain pytest with tmp_path). Cases:

1. Project WITHOUT `.crabcakes/prompts/` → `_load_project_prompts_context(p)` returns `''`.
2. Project WITH two small .md files → both stems appear as `## .crabcakes/prompts/<stem>` sections with their content; sorted order stable.
3. Oversized file (> cap) skipped: create a file >20KB, assert its stem NOT in output, small files still present.
4. Subdir excluded: `<prompts>/default_agents/x.yaml` present in fixture → never appears in output.
5. Cap of 30: create 35 small files, assert exactly 30 sections (and they are the alphabetically-first 30).
6. Non-.md file ignored.
7. Integration: `build_file_context(project)` on a tmp project with seeded prompts returns a string containing BOTH an existing core-doc marker (whatever DOC_NAMES produces when those files exist — create one, e.g. architecture.md) AND `## .crabcakes/prompts/<stem>`; ordering: core docs BEFORE prompts sections.
8. Unreadable dir (monkeypatch os.listdir to raise OSError inside agent.context's namespace) → returns `''`, no raise.

### File 3: `docs/ARCHITECTURE.md` — new subsection per spec §8

Near the existing "Prompt library" mention (~line 40 area) add a compact subsection "Per-project prompt library" covering:
- Two-tier split: app-level `<app>/prompts/system/` (+ claude-code-clean/, human-reference-only) vs per-project `<project>/.crabcakes/prompts/`.
- `seed_project_prompts()` copy-only-if-missing semantics (project branches after local edits).
- `get_project_prompts_dir()` three-clause resolution + `ensure_project_prompts_dir()` write-side counterpart (unseeded-project writes CREATE the project dir rather than falling back to app).
- Favorites keyed by stem (one-time path→stem migration).
- Agent context injection caps (30 files / 20KB each).
Also update the file index (~lines 218–219 region): add `utils/prompt_paths.py` and note `seed_project_prompts` under `utils/project_awareness.py`. Keep it tight — match surrounding doc density.

## Rules

- Use the **steelFramedCodeWriter** prompt at `prompts/steelFramedCodeWriter.md`.
- Flag ANY deviation explicitly.
- Do NOT touch agent/tools.py sandbox logic (spec §9).

## Verification (paste what you can; state explicitly if exec-gated)

```bash
cd /path/to/projects/crabcakes && python3 -B -m pytest tests/test_agent_context_prompts.py -v -p no:cacheprovider
cd /path/to/projects/crabcakes && python3 -B -m pytest tests/test_prompt_paths.py tests/test_seed_project_prompts.py tests/test_favorites.py -q -p no:cacheprovider
grep -n "_load_project_prompts_context\|_PROJECT_PROMPTS" agent/context.py
```

COMPLETENESS:
- [x/not done] Edit 1: constants + _load_project_prompts_context + build_file_context wiring — evidence (grep + anchor description)
- [x/not done] Tests 1–8 written/passing — full pytest output or explicit gated statement
- [x/not done] ARCHITECTURE.md subsection + file index updated — evidence
