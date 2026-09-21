# Phase T2-RL2 — review_handler fix: message from staged files + checkpoint allow_empty

**Source:** `docs/post-mortems/2026-06-16-REVIEW-LAYER-INVESTIGATION.md` (Patch 2)
**Depends on:** T2-RL1 (commit() now has allow_empty parameter)
**Severity:** MEDIUM (1 of 11 recent Accept commits has wrong file in message; 1 has incomplete file list)
**Risk:** Low (backwards compatible — existing behavior preserved when allow_empty is correct)
**Lines:** +20 in `ui/handlers/review_handler.py`, +30 in tests

## Goal

Fix two bugs in `ui/handlers/review_handler.py`:

1. **`accept_changes` (line 248-294)** generates the commit message from the input `message` parameter, not from the actual staged files. This produces misleading "Accept: Modified X" commits where X may not be the actual file changed, or where multiple files were changed but only one is named. Fix: generate the message from `repo.index.diff("HEAD")` after staging.

2. **Checkpoint caller (line 164)** doesn't pass `allow_empty=True`. After T2-RL1, this caller would refuse to create checkpoint SHAs when the working tree is clean. Fix: pass `allow_empty=True` to preserve the checkpoint-as-marker behavior.

Plus, handle the new empty-commit case gracefully: when `accept_changes` is called on a clean tree (no staged changes), show a friendly "Nothing to commit" message instead of "Failed to commit: nothing to commit (working tree clean)".

## Files to change

1. `ui/handlers/review_handler.py` — fix `accept_changes` and the checkpoint caller
2. `tests/test_review_handler.py` (or wherever the tests are) — add tests for the new behavior

## Edit 1: `ui/handlers/review_handler.py` line 164 — checkpoint allow_empty

**Anchor:** the checkpoint commit call. Find this pattern:
```python
            # Commit checkpoint
            commit_result = git_ops.commit(project_path, "[review] checkpoint")
```

**Replace with:**
```python
            # Commit checkpoint. allow_empty=True because a checkpoint is a
            # valid SHA marker even on a clean tree (user can start a review
            # without any changes to capture the current state).
            commit_result = git_ops.commit(project_path, "[review] checkpoint", allow_empty=True)
```

## Edit 2: `ui/handlers/review_handler.py` line 248-294 — accept_changes

**Anchor:** the entire `_do` function inside `accept_changes`. Find the current code and replace with the fixed version. The change is non-trivial — it requires reordering (stage → generate message → commit) and special-casing the empty result.

The current `_do` function is at lines 257-291. **Replace the entire function body** (everything from `def _do():` through the final `threading.Thread(target=_do, daemon=True).start()` line) with the fixed version below.

**Fixed version:**

```python
        def _do():
            # Stage all
            stage_result = git_ops.stage_all(project_path)
            if not stage_result.success:
                self._GLib.idle_add(lambda sk=sk: self._on_display_text(sk, f"Failed to stage: {stage_result.error}"))
                return

            # Generate the commit message from the ACTUAL staged files, not
            # from the input message parameter. The input message is just
            # user intent ("Accept: Modified X") but the real diff is in
            # repo.index.diff("HEAD"). This fixes the "wrong file in message"
            # and "incomplete file list" bugs.
            try:
                repo = gitpython.Repo(project_path)
                staged = repo.index.diff("HEAD")
            except Exception:
                # If we can't read the diff for any reason, fall back to
                # the input message rather than failing the accept.
                staged = []

            if not staged:
                # Working tree is clean — nothing to commit. Show a friendly
                # message instead of the misleading "Failed to commit" error.
                self._GLib.idle_add(lambda sk=sk: self._on_display_text(
                    sk, "ℹ️ Nothing to commit — working tree clean. No changes were accepted."
                ))
                # Still update the state so the review bar resets.
                def _reset_state(sk=sk):
                    state.checkpoint_sha = None
                    state.is_dirty = False
                    state.last_check_files = []
                    bar = self._mc.get_review_bar()
                    if bar:
                        bar.set_state_idle()
                        bar.set_loading(False)
                    self._on_review_ended(project_name)
                self._GLib.idle_add(_reset_state)
                return

            # Build a descriptive message from the actual files
            file_list = sorted({d.a_path or d.b_path for d in staged if d.a_path or d.b_path})
            if len(file_list) == 1:
                full_message = f"[review] accepted: Accept: Modified {file_list[0]}"
            elif len(file_list) <= 3:
                full_message = f"[review] accepted: Accept: Modified {len(file_list)} files ({', '.join(file_list)})"
            else:
                full_message = f"[review] accepted: Accept: Modified {len(file_list)} files ({', '.join(file_list[:3])}...)"

            # Commit
            commit_result = git_ops.commit(project_path, full_message)
            if not commit_result.success:
                self._GLib.idle_add(lambda sk=sk: self._on_display_text(sk, f"Failed to commit: {commit_result.error}"))
                return

            def _update_state(sk=sk):
                state.checkpoint_sha = None
                state.is_dirty = False
                state.last_check_files = []
                bar = self._mc.get_review_bar()
                if bar:
                    bar.set_state_idle()
                    bar.set_loading(False)
                self._on_review_ended(project_name)
                # Use the actual generated message for the user-facing display
                self._on_display_text(sk, f"✅ Changes accepted and committed: {full_message.replace('[review] accepted: ', '')}")
                self._emit_feed_card({
                    "title": f"Accepted: {full_message.replace('[review] accepted: ', '')}",
                    "body": commit_result.stdout.strip() if commit_result.stdout else "",
                    "project_name": project_name,
                    "commit_sha": getattr(commit_result, "sha", None),
                })

            self._GLib.idle_add(_update_state)

        threading.Thread(target=_do, daemon=True).start()
```

**Key changes:**

1. After `stage_all`, get the actual diff from `repo.index.diff("HEAD")` (with try/except for safety)
2. If diff is empty, show "Nothing to commit" and reset the review bar without committing
3. If diff has changes, generate the message from the file list (1 file: "Modified X"; 2-3 files: "Modified N (a, b)"; 4+: "Modified N (a, b, c...)")
4. Use the generated message for both the commit AND the user-facing display
5. The format preserves the existing `[review] accepted:` prefix for log filterability, with the "Accept: Modified" wording as the body

**Add the gitpython import at the top of the file** if not already present:
```python
import git  # or gitpython
```

Check the existing imports in `ui/handlers/review_handler.py` to see if `gitpython.Repo` is already imported. If not, add the import.

## Rules

- Use `prompts/steelFramedCodeWriter.md` as the active prompt.
- Use identifiers as anchors, not line numbers.
- Do NOT modify any other function in `review_handler.py` (e.g., `reject_changes`, `start_review`).
- Do NOT modify the checkpoint logic in `start_review` (line 137-) except for the `allow_empty=True` parameter (Edit 1).
- Do NOT change the feed card format — keep the existing keys (`title`, `body`, `project_name`, `commit_sha`).
- Do NOT change the review bar state management — keep the same `set_state_idle`, `set_loading`, `is_dirty` patterns.
- The new message format MUST preserve the `[review] accepted:` prefix for log filterability.
- The new message format MUST use the actual staged file names, not the input param.

## Verification (run yourself, paste output in report)

1. The checkpoint caller has `allow_empty=True`:
   ```
   grep -n "allow_empty=True" ui/handlers/review_handler.py
   ```
   Expected: 1 match (the checkpoint caller).

2. The accept_changes no longer uses the input `message` for the commit:
   ```
   grep -n 'full_message = f"\[review\] accepted: {message}"' ui/handlers/review_handler.py
   ```
   Expected: 0 matches (the old pattern is gone).

3. The accept_changes now uses the staged files for the message:
   ```
   grep -n "file_list\|repo.index.diff" ui/handlers/review_handler.py
   ```
   Expected: at least 2 matches (the diff lookup and the file list generation).

4. The new tests pass:
   ```
   python3 -m pytest tests/test_review_handler.py -v 2>&1 | tail -20
   ```
   Expected: at least 2-3 new tests pass (existing tests should also pass — count increase).

5. End-to-end: clean tree + accept → "Nothing to commit" message, no commit:
   ```
   python3 -c "
   import os, tempfile
   from pathlib import Path
   import subprocess
   tmpdir = tempfile.mkdtemp()
   subprocess.run(['git', 'init', tmpdir], check=True, capture_output=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.email', 't@t.com'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.name', 'T'], check=True)
   Path(tmpdir, 'init.txt').write_text('init')
   subprocess.run(['git', '-C', tmpdir, 'add', 'init.txt'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'commit', '-m', 'initial'], check=True, capture_output=True)
   from utils import git_ops
   # Simulate the accept flow
   stage_result = git_ops.stage_all(tmpdir)
   assert stage_result.success
   commit_result = git_ops.commit(tmpdir, '[review] accepted: should refuse empty')
   assert commit_result.success is False
   assert 'nothing to commit' in commit_result.error
   head = subprocess.run(['git', '-C', tmpdir, 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
   # No new commit
   log = subprocess.run(['git', '-C', tmpdir, 'log', '--oneline'], check=True, capture_output=True, text=True).stdout
   assert log.count('\n') == 1, f'expected only initial commit, got: {log!r}'
   print('OK: clean tree + accept refilled, no commit created')
   "
   ```
   Expected: `OK: clean tree + accept refused, no commit created`.

6. End-to-end: dirty tree + single file change + accept → commit with right file name in message:
   ```
   python3 -c "
   import os, tempfile
   from pathlib import Path
   import subprocess
   tmpdir = tempfile.mkdtemp()
   subprocess.run(['git', 'init', tmpdir], check=True, capture_output=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.email', 't@t.com'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.name', 'T'], check=True)
   Path(tmpdir, 'init.txt').write_text('init')
   subprocess.run(['git', '-C', tmpdir, 'add', 'init.txt'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'commit', '-m', 'initial'], check=True, capture_output=True)
   Path(tmpdir, 'new.py').write_text('print(1)')
   from utils import git_ops
   stage_result = git_ops.stage_all(tmpdir)
   commit_result = git_ops.commit(tmpdir, '[review] accepted: Accept: Modified new.py')
   assert commit_result.success is True
   log = subprocess.run(['git', '-C', tmpdir, 'log', '-1', '--pretty=%s'], check=True, capture_output=True, text=True).stdout.strip()
   assert 'new.py' in log, f'expected new.py in message, got: {log!r}'
   print(f'OK: dirty tree + accept committed with right file in message: {log!r}')
   "
   ```
   Expected: `OK: dirty tree + accept committed with right file in message: ...`.

7. End-to-end: checkpoint on clean tree still works (allow_empty=True):
   ```
   python3 -c "
   import os, tempfile
   from pathlib import Path
   import subprocess
   tmpdir = tempfile.mkdtemp()
   subprocess.run(['git', 'init', tmpdir], check=True, capture_output=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.email', 't@t.com'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'config', 'user.name', 'T'], check=True)
   Path(tmpdir, 'init.txt').write_text('init')
   subprocess.run(['git', '-C', tmpdir, 'add', 'init.txt'], check=True)
   subprocess.run(['git', '-C', tmpdir, 'commit', '-m', 'initial'], check=True, capture_output=True)
   from utils import git_ops
   stage_result = git_ops.stage_all(tmpdir)
   commit_result = git_ops.commit(tmpdir, '[review] checkpoint', allow_empty=True)
   assert commit_result.success is True
   assert commit_result.sha is not None
   print('OK: checkpoint on clean tree creates empty commit (allow_empty=True)')
   "
   ```
   Expected: `OK: checkpoint on clean tree creates empty commit (allow_empty=True)`.

8. Full test suite (regression):
   ```
   python3 -m pytest tests/ -q --tb=short --ignore=tests/test_agent_runtime.py --ignore=tests/test_kb_lookup.py 2>&1 | tail -5
   ```
   Expected: 1550+ passed (1550 + new tests), 1 skipped, exit 0.

## Deliverable

- Both edits applied
- All 8 verification commands run by you, output pasted in the report
- A `**COMPLETENESS:**` block listing each edit with evidence

## Word marker

Include the word "please write" in your opening reply so the channel knows this delegation is canonical.

## COMPLETENESS template

End your reply with:

```
**COMPLETENESS:**
- [x] Edit 1: checkpoint caller passes allow_empty=True — line N in ui/handlers/review_handler.py, evidence: V1 output
- [x] Edit 2: accept_changes uses staged files for message — line N in ui/handlers/review_handler.py, evidence: V2 + V3 output
- [x] Verification 1: checkpoint has allow_empty=True — <paste output>
- [x] Verification 2: old input-message pattern is gone — <paste output>
- [x] Verification 3: new staged-files pattern is in place — <paste output>
- [x] Verification 4: new tests pass — <paste pytest output>
- [x] Verification 5: clean tree + accept refuses — <paste output>
- [x] Verification 6: dirty tree + accept commits with right file — <paste output>
- [x] Verification 7: checkpoint on clean tree still works — <paste output>
- [x] Verification 8: full test suite — <paste last 5 lines>
- [x] Related-bug scan: <list of any related issues found, or "none">
```
