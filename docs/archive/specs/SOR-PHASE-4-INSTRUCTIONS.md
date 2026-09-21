# Phase 4 of 8 — Manifest skeleton cleanup helper

**Master spec:** `docs/specs/SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS.md` §2.8 + §2.10.
**This phase is pure-Python only (no UI, no GTK).**

**Goal:** Add `clean_manifest_skeleton(project_path: str) -> bool` to `utils/project_awareness.py`. It removes any manifest section whose body contains only whitespace and HTML comments, while preserving the title, non-comment content, and unrelated sections. Called at onboarding completion (wired in Phase 5).

## Rules
- Use the `prompts/steelFramedCodeWriter.md` prompt. Read `utils/project_awareness.py` in FULL before editing — especially `is_project_onboarded` (line ~220), `generate_project_skeleton` (line ~173), and `load_project_manifest` (line ~206).
- Anchor edits to identifiers, not line numbers.
- Verify every claim with evidence (paste command output). Probe edge cases empirically before writing assertions.

## The parsing hazard (read carefully)

The master spec §2.8 **forbids** a whole-file `re.sub(r'<!--.*?-->', '', manifest, flags=re.DOTALL)` for cleanup, because **a comment can contain `## ` text** (e.g. `<!-- see ## Notes above -->`). A whole-file approach that splits on `^## ` after stripping comments would let such a comment swallow later sections.

Instead, you must split the file into sections by line-boundary `^## ` first, then strip comments **within each section independently**, then decide per-section whether the remaining body is whitespace-only.

## Edit 1 — `utils/project_awareness.py`: add `clean_manifest_skeleton`

Add a public function `clean_manifest_skeleton(project_path: str) -> bool`. Requirements (spec §2.8):

- Operate only on `.crabcakes/project.md` (use `get_crabcakes_dir` + `MANIFEST_FILENAME`).
- Split the file into the top-level preamble/title plus sections using line boundaries matching `^## `.
- For each section independently: strip HTML comments (`<!-- ... -->`, possibly multi-line) from that section's body (NOT the heading), then test whether the remaining body (after the heading line) is whitespace-only. If so, drop the entire section (heading + body). If any real non-comment, non-whitespace content remains, preserve the section verbatim (unchanged — do NOT strip its comments).
- **Do not remove the top-level `# Title`** (the preamble before the first `## `).
- Comments and `## ` text inside a comment must remain confined to that section's parsing — i.e. when splitting on `^## ` lines, a `## ` that appears INSIDE a `<!-- ... -->` comment block in a prior section's body must NOT be treated as a section boundary. (Use a line-by-line scan that tracks whether you're inside a comment block, similar to how `_split_entries` in the same file tracks code-block fences.)
- Handle missing/unreadable files as a safe no-op returning `False` (do NOT raise into the onboarding flow).
- Write the file only when content actually changed; return whether a change occurred. Preserve file newline structure as far as practical (end with a trailing newline like the skeleton does).
- This is a **read + conditional write**; do not modify `build_awareness_dict()` (spec §2.8 explicit — it must remain read-only). Do not call this on every awareness read.

**Implementation approach (recommended):**
1. `manifest = load_project_manifest(project_path)`; if None, return False.
2. Split into lines.
3. Walk lines, tracking `in_comment` (toggled by `<!--` and `-->`, possibly on the same or different lines). A line is a section heading ONLY if it `startswith("## ")` AND `not in_comment`.
4. Group lines into: preamble (before first heading), then sections (each starts at a heading and runs until the next heading or EOF).
5. For each section: extract heading + body. Compute `stripped_body = re.sub(r'<!--.*?-->', '', body, flags=re.DOTALL).strip()` on the BODY ONLY (not the heading). If `stripped_body` is empty, mark the section for removal.
6. Reassemble: preamble + kept sections (in original order, verbatim including their comments). If the result equals the original (no sections removed), return False without writing. Else write + return True.

Edge cases to handle (probe these empirically before finalizing):
- **Comment-only body:** `## Purpose\n<!-- foo -->\n` → section removed.
- **Mixed (comment + real text):** `## Purpose\n<!-- foo -->\nReal content\n` → section KEPT verbatim (do not strip its comment).
- **Empty body:** `## Purpose\n\n` → section removed.
- **`## ` inside a comment:** `## Purpose\n<!-- see ## Notes -->\n` → the `## Notes` inside the comment must NOT start a new section; the whole Purpose section is comment-only → removed.
- **Multi-line comment spanning the `## ` boundary:** `## Purpose\n<!-- start of note\n## Fake\nend -->\n` → the `## Fake` line is inside an open comment → not a section boundary. Purpose section body is all-comment → removed.
- **Already-clean manifest:** a manifest with real content in every section → no change, return False, do not write.
- **Missing/empty/malformed file:** return False, do not raise.
- **No `## ` sections at all (only a title):** return False (nothing to clean).

Use `re` (already imported at top of the file). Match the file's logging style (`_logger`).

## Edit 2 — Tests in `tests/test_project_awareness.py`

Read `tests/test_project_awareness.py` FIRST for fixture patterns. Add a test class `TestCleanManifestSkeleton` (match the file's style). Each test uses an isolated temp project dir (look at how existing tests in the file create `.crabcakes/project.md`). Required tests (spec §2.10):

1. `test_removes_comment_only_sections` — skeleton manifest (use `generate_project_skeleton`) → all 5 sections are comment-only → `clean_manifest_skeleton` returns True, and the result keeps ONLY the `# Title` line (all `## ` sections gone).
2. `test_preserves_sections_with_real_content` — manifest with one section having real text → that section is preserved VERBATIM (including any comments it had); comment-only sections are removed.
3. `test_no_change_when_already_clean` — manifest where every section has real content → returns False, file unchanged (byte-for-byte).
4. `test_hash_inside_comment_not_treated_as_section` — manifest where a section body contains `<!-- see ## Notes -->` → that `## Notes` does not start a new section; the comment-only section is removed as a whole.
5. `test_multiline_comment_spanning_hash_boundary` — a `<!-- ...` comment that spans a line containing `## Fake` → `## Fake` is not a section boundary.
6. `test_missing_file_is_noop` — no `.crabcakes/project.md` → returns False, no exception.
7. `test_malformed_or_empty_manifest` — empty file or file with only `# Title` → returns False, no write, no exception.
8. `test_does_not_touch_title` — the `# Title` line is always preserved even when all sections are removed.

Also add ONE read-only invariant test: `test_build_awareness_dict_does_not_write_snapshot` — after calling `build_awareness_dict(project_path)`, assert that `awareness.json` was NOT written/modified (stat the mtime before and after, or assert the file doesn't exist if it didn't before). Spec §2.8: `build_awareness_dict()` must remain read-only.

## Verification (run and paste output)

```bash
# Function exists and is public
grep -n "def clean_manifest_skeleton" utils/project_awareness.py

# Functional proof: skeleton → cleaned
python3 -c "
import sys, tempfile, os; sys.path.insert(0,'.')
from utils.project_awareness import generate_project_skeleton, clean_manifest_skeleton, load_project_manifest
p = tempfile.mkdtemp()
generate_project_skeleton(p, 'Demo')
print('BEFORE:'); print(load_project_manifest(p))
changed = clean_manifest_skeleton(p)
print('changed:', changed)
print('AFTER:'); print(load_project_manifest(p))
"

# Hash-in-comment safety
python3 -c "
import sys, tempfile, os; sys.path.insert(0,'.')
from utils.project_awareness import clean_manifest_skeleton, load_project_manifest, _ensure_crabcakes_dir
p = tempfile.mkdtemp(); _ensure_crabcakes_dir(p)
mp = os.path.join(p, '.crabcakes', 'project.md')
with open(mp,'w') as f:
    f.write('# T\n\n## Purpose\n<!-- see ## Notes -->\n\n## Notes\nreal content\n')
clean_manifest_skeleton(p)
print('RESULT:'); print(repr(load_project_manifest(p)))
print('expect: # T + ## Notes preserved, ## Purpose removed, ## Notes inside comment did NOT split')
"

# Tests
XDG_CONFIG_HOME=/tmp/cctest_home/.config python3 -m pytest tests/test_project_awareness.py -q 2>&1 | tail -10
```

## COMPLETENESS (mandatory)

```
COMPLETENESS:
- [ ] Edit 1: clean_manifest_skeleton added (section-aware, comment-block tracking, no whole-file DOTALL sub) — evidence: grep + functional proof (skeleton → title only)
- [ ] Edit 2: TestCleanManifestSkeleton tests (8) + read-only invariant test — evidence: pytest output
- [ ] hash-in-comment safety verified — evidence: functional proof output
- [ ] build_awareness_dict remains read-only — evidence: invariant test passes
- [ ] Any related issue found, not silently fixed (report here)
```
