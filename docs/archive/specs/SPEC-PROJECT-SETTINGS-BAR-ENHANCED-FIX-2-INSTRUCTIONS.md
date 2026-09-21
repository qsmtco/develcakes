# Spec Fix Round 2 — Instructions for Coder

**Spec to revise:** `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-1.md` (Round 1 fix)
**Round 2 findings to address:** `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-1-FINDINGS.md`
**Prompt to load:** `prompts/steelFramedSpecWriter.md`
**Working dir:** `/path/to/projects/crabcakes`

## Task

Revise the spec to address the **7 Round 2 findings** (1 CRITICAL, 3 HIGH, 3 MEDIUM). Produce a new spec at `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-2.md` that supersedes `...-FIX-1.md`. Each finding must have a concrete code-level fix in the spec.

## Findings to address (in priority order)

### CRITICAL — BUG #1: branch scheduling condition is dead code
The check `if self._pending_branch_refresh is None` will never be true when `_pending_branch_refresh` is initialized as `False`. The branch worker never runs.

**Fix required in the new spec:**
- Change the check to `not self._pending_branch_refresh`
- Initialize `_pending_branch_refresh` to a clear sentinel value OR change the truthy/falsy check consistently
- Add a regression test that proves the worker is started when the field is False/0

### HIGH — BUG #2: async branch results can cross project boundaries
The worker captures `project_name` but reads the path dynamically; on completion the path may point to a different project (or none). The bar can display project B's branch on project A's tab, or update a closed project.

**Fix required in the new spec:**
- Capture `project_path` and a `request_token` (monotonic int) when scheduling the worker
- In the completion callback, compare the captured token/path against the current `_pending_branch_token` and the active project's path
- If they don't match, discard the result silently
- Add a regression test: start a refresh for project A, switch to project B, complete the worker, assert project B's bar is updated with project B's branch and project A's bar (if still open) is not touched

### HIGH — BUG #3: auto-accept callback clobbers member count to 0
`_on_feed_bar_update` is called with `member_count=0` after every auto-accept click, making the bar show "0 members" until the next project event.

**Fix required in the new spec:**
- In the auto-accept cycle callback, read `self._project_handler.get_project_members(project_name)` before calling `_on_feed_bar_update`
- Pass the actual `len(members)` instead of `0`
- Add a regression test: click auto-accept, assert member count is unchanged

### HIGH — BUG #4: solo-target callback section is incomplete
The Round 1 fix introduced a new `set_on_solo_target_changed` callback in `ProjectHandler` but the spec's §2.5 (or whatever the file-list section is) does not include the actual `ProjectHandler` code sample. The existing `set_solo_target` only assigns `_solo_targets[project_name] = member_session_key` — there is no callback fire.

**Fix required in the new spec:**
- Add a complete code sample for `project_handler.py` showing:
  - `self._on_solo_target_changed: Callable[[str], None] | None = None` initialization
  - `set_on_solo_target_changed(self, cb)` setter
  - Modified `set_solo_target` that fires the callback when set
- Add the `window.py` wiring sample
- Add a regression test: set solo target, assert the bar's agent name updates

### MEDIUM — BUG #5: legacy `set_project_settings_text` and `set_feed_bar_text` clear the gear button
The new spec adds `_clear_settings_bar()` to the legacy methods, but they only re-append a label, not `self._settings_btn`. The gear button disappears.

**Fix required in the new spec:**
- Either re-append the gear button after the legacy label in both methods
- OR explicitly document that legacy calls replace the entire bar (and the gear must be re-added by the next `update_project_settings`)
- Pick the safer option (re-append the gear) and add a regression test

### MEDIUM — BUG #6: `escape_for_pango` preserves Pango tags → injection risk
`escape_for_pango()` intentionally preserves known Pango tags like `<b>` and `<span>`. A project named `<b>oops</b>` is interpreted, not escaped. The spec uses `escape_for_pango(project_name)` for plain project names.

**Fix required in the new spec:**
- Use `xml_escape_text(project_name)` for the project name (not `escape_for_pango`)
- Audit all other places in the spec that interpolate plain text into Pango markup and use `xml_escape_text` consistently
- Add a regression test: project named `<b>injected</b>` should render literally, not bolded

### MEDIUM — BUG #7: `_pending_branch_refresh` is not thread-safe
The boolean is read/written from GTK and worker threads without a lock or token. A second refresh can start before the first result is applied, and an older result can overwrite a newer cached branch.

**Fix required in the new spec:**
- Replace the boolean with an integer `request_token` (monotonically increasing)
- All state transitions on the GTK thread; the worker just dispatches the result
- On completion, compare the captured token to the current token; if they don't match, discard
- (This is essentially the same fix as BUG #2 — combine the token + path approach)

## Process requirements

1. **Read every file you reference.** Per `steelFramedSpecWriter.md` Rule 1.
2. **Verify every claim empirically.** Don't trust memory; use `read_file`, `search_files`, `exec_command`.
3. **Update §9 (traceability table)** to map each of the 7 Round 2 findings to the fix in the new spec.
4. **Section structure:** keep the same 10-section template from FIX-1.
5. **File name:** write to `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-2.md`. Do NOT write to a different path.
6. **Update `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-LOOP-STATE.md`** with Round 2 status.

## What to skip

- Do NOT modify `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED.md` (original, kept for diff reference).
- Do NOT modify `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-1.md` (Round 1 fix, also kept for diff reference).
- Do NOT implement any code. Spec only.

## Deliverable

- `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-2.md` (new spec, supersedes FIX-1)
- `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-LOOP-STATE.md` (updated)
- COMPLETENESS checklist with verification evidence
- Empirical probes for: `escape_for_pango` vs `xml_escape_text` semantics, `ProjectHandler` `set_solo_target` current state, `set_pending_branch` lifecycle

## COMPLETENESS format (required)

```
COMPLETENESS:
- [x] Edit 1: [description] — evidence
- [x] Edit 2: [description] — evidence
...
- [x] Section 9 updated with 7-finding traceability — [link/anchor]
- [x] Empirical probe: escape_for_pango preserves <b> in plain text — output
- [x] Empirical probe: xml_escape_text produces safe plain text — output
```

Please write when done.
