# Phase 5 Instructions — UI Image Viewer Hardening (LOW-7)

**Phase:** 5 of 5
**Finding (original review):** LOW-7
**Master spec:** `docs/specs/SPEC-LOW-FOLLOWUP-PHASE-4.md` §4.6
**Authority chain:** Captain → `docs/ARCHITECTURE.md` → spec → this file → code

---

## READ FIRST

1. **Read the master spec** — `docs/specs/SPEC-LOW-FOLLOWUP-PHASE-4.md` §4.6
2. **Read these files in full** before editing:
   - `ui/views/chat_bubble.py` (focus on lines 50-58 `_open_in_viewer` and 405-415 `_build_image_block`)
   - Any project-context getters that provide the active project path
3. **Use the steelFramedCodeWriter prompt** at `prompts/steelFramedCodeWriter.md`

## Edits to make (1 file)

### `ui/views/chat_bubble.py` — LOW-7

The current `_open_in_viewer` (lines 50-58) is:

```python
def _open_in_viewer(file_path: str) -> None:
    """Open file_path in the system's default image viewer."""
    import subprocess, shutil
    if not os.path.isfile(file_path):
        return
    opener = shutil.which("xdg-open") or shutil.which("open") or "xdg-open"
    try:
        subprocess.Popen([opener, file_path])
    except Exception:
        pass
```

The fix introduces a path-scope check. The allowed roots are:
- the active project path (passed via env var, since `ui/views/` cannot import from `ui/handlers/`)
- the user's home directory
- `/tmp` (for ephemeral files)

Refactor to:

```python
import os
import shutil
import subprocess

# LOW-7: paths outside these roots are rejected to prevent the LLM from
# tricking the user into opening /etc/passwd or similar sensitive files.
_ALLOWED_ROOTS_FALLBACK = (
    os.path.expanduser("~"),
    "/tmp",
)


def _get_allowed_roots() -> tuple[str, ...]:
    """LOW-7: compute the set of allowed root paths for the image viewer.

    Reads CRABCAKES_ACTIVE_PROJECT_PATH env var if set, plus the fallback
    roots (home + /tmp). The active project path is passed by the handler
    that owns the chat bubble, via os.environ.
    """
    roots: list[str] = []
    project = os.environ.get("CRABCAKES_ACTIVE_PROJECT_PATH", "").strip()
    if project:
        roots.append(project)
    roots.extend(_ALLOWED_ROOTS_FALLBACK)
    return tuple(roots)


def _is_path_in_allowed_roots(file_path: str) -> bool:
    """LOW-7: return True if file_path is under one of the allowed roots.

    Resolves symlinks via realpath. If the resolved path is not under any
    allowed root, returns False.
    """
    try:
        resolved = os.path.realpath(file_path)
    except OSError:
        return False
    for root in _get_allowed_roots():
        try:
            root_resolved = os.path.realpath(root)
        except OSError:
            continue
        # Use os.path.commonpath for safe prefix check (handles trailing slashes)
        try:
            common = os.path.commonpath([resolved, root_resolved])
        except ValueError:
            # Different drives on Windows
            continue
        if common == root_resolved:
            return True
    return False


def _open_in_viewer(file_path: str) -> None:
    """Open file_path in the system's default image viewer.

    LOW-7: only opens files inside an allowed root (active project, home, /tmp).
    Logs a warning if the path is rejected.
    """
    if not file_path or not os.path.isfile(file_path):
        return
    if not _is_path_in_allowed_roots(file_path):
        import logging
        logging.getLogger(__name__).warning(
            "LOW-7: refusing to open %r — not under any allowed root",
            file_path,
        )
        return
    opener = shutil.which("xdg-open") or shutil.which("open") or "xdg-open"
    try:
        # Use list-form (no shell), and pass only the file path (no extra args)
        # to avoid argument injection.
        subprocess.Popen([opener, file_path])
    except (OSError, ValueError):
        pass
```

**Note on env-var-based project path:** `ui/views/` cannot import from `ui/handlers/` per ARCHITECTURE.md §2 ("Critical rule"). The env-var bridge is the documented escape hatch — `ui/handlers/chat_handler.py` sets `os.environ["CRABCAKES_ACTIVE_PROJECT_PATH"]` when a project is active, and unsets it on project close. **This is a pre-existing pattern in the codebase** — verify by grepping for `os.environ.setdefault` or `os.environ["CRABCAKES_` in `ui/handlers/`. If the pattern is not already established, add a short comment in `chat_handler.py` near the project-open/close path that sets/unsets the env var.

If the env-var pattern is not already used, report it in the COMPLETENESS checklist as a related issue and let the supervisor decide whether to add it in a follow-up.

## Tests to add

Add to `tests/test_icons.py` (existing) or new `tests/test_low7_image_viewer.py`:

1. `test_low7_open_in_viewer_rejects_etc_passwd` — patch `subprocess.Popen`, call `_open_in_viewer("/etc/passwd")`, assert `Popen` is NOT called.
2. `test_low7_open_in_viewer_allows_project_path` — set `os.environ["CRABCAKES_ACTIVE_PROJECT_PATH"]` to a temp dir, create a file there, call `_open_in_viewer(<that file>)`, assert `Popen` IS called.
3. `test_low7_open_in_viewer_allows_tmp` — call `_open_in_viewer("/tmp/somefile.png")` (create it first), assert `Popen` IS called.
4. `test_low7_open_in_viewer_rejects_symlink_outside_root` — create a symlink inside an allowed root that points to `/etc/passwd`, call `_open_in_viewer(<symlink>)`, assert `Popen` is NOT called.
5. `test_low7_open_in_viewer_rejects_nonexistent` — call `_open_in_viewer("/tmp/does-not-exist.png")`, assert `Popen` is NOT called.
6. `test_low7_path_in_allowed_roots_no_env_var` — unset `CRABCAKES_ACTIVE_PROJECT_PATH`, call `_is_path_in_allowed_roots("/tmp/foo")`, assert True. Call with `/etc/passwd`, assert False.

## Verification commands

```bash
# 1. Confirm the allowed-roots check is in place
git grep -nE "_is_path_in_allowed_roots|_get_allowed_roots" ui/views/chat_bubble.py

# 2. Confirm the env-var pattern is wired (or report it as a related issue)
git grep -nE "CRABCAKES_ACTIVE_PROJECT_PATH" ui/handlers/ 2>&1
git grep -nE "CRABCAKES_ACTIVE_PROJECT_PATH" ui/views/ 2>&1

# 3. Run tests
python -m pytest tests/test_icons.py tests/test_low7_image_viewer.py -v 2>&1 | tail -40

# 4. Full suite
python -m pytest tests/ -x --ignore=tests/test_agent_runtime.py -q 2>&1 | tail -20
```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] Edit 1: ui/views/chat_bubble.py — _ALLOWED_ROOTS_FALLBACK, _get_allowed_roots, _is_path_in_allowed_roots — evidence: <file:line>
- [x/not done] Edit 2: ui/views/chat_bubble.py — _open_in_viewer uses the check — evidence: <file:line>
- [x/not done] Edit 3: env-var pattern verification (or reported as related issue) — evidence: <git grep>
- [x/not done] Tests 1-6: pytest output — evidence: <paste>

RELATED ISSUES FOUND (do not silently fix — report them):
- <e.g., "CRABCAKES_ACTIVE_PROJECT_PATH is not set anywhere — needs follow-up wiring in chat_handler.py">
```

## Word marker

Include "please proceed" in your reply.

## Important reminders

- **Use list-form `subprocess.Popen([...])`.** Never use `subprocess.Popen(f"{opener} {file_path}", shell=True)`.
- **The env-var pattern may not be wired yet.** If it isn't, report it as a related issue. The supervisor will decide whether to add a follow-up phase to wire it.
- **The `/etc/passwd` rejection is the key test.** The whole point of LOW-7 is that an LLM-controlled path can't escape the allowed roots.
- **realpath is the right tool.** Don't use `os.path.abspath` — it doesn't resolve symlinks.
