# Phase 4 Instructions — Feed Store + Dead Code (LOW-12, LOW-13, A-10)

**Phase:** 4 of 5
**Findings (original review):** LOW-12, LOW-13, A-10 (1 of 4 sub-items)
**Master spec:** `docs/specs/SPEC-LOW-FOLLOWUP-PHASE-4.md` §4.11, §4.12, §4.13
**Authority chain:** Captain → `docs/ARCHITECTURE.md` → spec → this file → code

---

## READ FIRST

1. **Read the master spec** — `docs/specs/SPEC-LOW-FOLLOWUP-PHASE-4.md` §4.11, §4.12, §4.13
2. **Read these files in full** before editing:
   - `utils/feed_store.py` (full — focus on lines 122-128 save_feed, append_feed_card, update_feed_card)
   - `utils/image_utils.py` (full — 2745 bytes, no importers)
   - `utils/review_log.py` (full, especially line 19)
   - `tests/test_feed_store.py` (existing patterns)
3. **Use the steelFramedCodeWriter prompt** at `prompts/steelFramedCodeWriter.md`

## Edits to make (3 files + 1 deletion)

### `utils/feed_store.py` — LOW-12 and LOW-13

Add two helpers near the top of the file (after the imports):

```python
import stat


def _atomic_write_json(path: str, data) -> None:
    """LOW-13: write JSON atomically — write to .tmp, then os.replace.

    Sets permissions to 0o600 (matches the security pattern in
    agent/runtime.py:1069-1072). Caller is responsible for the lock.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # some filesystems don't support chmod


def _ensure_gitignore_entry(project_path: str, entry: str) -> None:
    """LOW-12: ensure `entry` is in `<project_path>/.gitignore`.

    Creates the file if it doesn't exist. If the file exists, checks for
    the entry (whole-line match, ignoring trailing comments) and appends
    if missing. The write is atomic via _atomic_write_text.
    """
    gitignore = os.path.join(project_path, ".gitignore")
    lines: list[str] = []
    if os.path.isfile(gitignore):
        try:
            with open(gitignore, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
    # Check if entry is already present (ignore trailing comments)
    for line in lines:
        stripped = line.split("#", 1)[0].strip()
        if stripped == entry:
            return  # already present
    # Append
    lines.append(entry)
    _atomic_write_text(gitignore, "\n".join(lines) + "\n")


def _atomic_write_text(path: str, content: str) -> None:
    """Atomic write of a text file. Uses .tmp + os.replace + 0o644 permissions."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
```

Now update the three write functions:

**`save_feed` (lines 122-128):**

```python
def save_feed(project_path: str, cards: list[FeedCardData]) -> None:
    """LOW-12 + LOW-13: ensure gitignore + atomic write."""
    try:
        _ensure_crabcakes_dir(project_path)
        path = _feed_path(project_path)
        _ensure_gitignore_entry(project_path, ".crabcakes/feed.json")
        _atomic_write_json(path, [c.to_dict() for c in cards])
    except OSError as e:
        _logger.error("save_feed: failed to write %s: %s", path, e)
```

**`append_feed_card` (lines 132-152):**

```python
def append_feed_card(project_path: str, card: FeedCardData) -> None:
    """LOW-12 + LOW-13: ensure gitignore + atomic write."""
    path = _feed_path(project_path)
    _ensure_crabcakes_dir(project_path)
    _ensure_gitignore_entry(project_path, ".crabcakes/feed.json")
    fd, lock_path = _acquire_lock(path)
    try:
        raw_cards = []
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw_cards = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                _logger.warning("append_feed_card: failed to read %s: %s", path, e)
                raw_cards = []
        cards = []
        for item in (raw_cards if isinstance(raw_cards, list) else []):
            if isinstance(item, dict):
                try:
                    cards.append(FeedCardData.from_dict(item))
                except (KeyError, TypeError):
                    continue
        cards.append(card)
        _atomic_write_json(path, [c.to_dict() for c in cards])
    finally:
        _release_lock(fd, lock_path)
```

**`update_feed_card` (lines 155-186):**

Same change: replace the `with open(path, "w", encoding="utf-8") as f: json.dump(...)` with `_atomic_write_json(path, ...)`, and add the `_ensure_gitignore_entry` call at the top.

### `utils/image_utils.py` — A-10 (sub-item 1)

This file has **zero importers** (confirmed by `git grep "image_utils" --include="*.py"`). Delete the file:

```bash
git rm utils/image_utils.py
```

Verify the deletion does not break any test:

```bash
python -m pytest tests/ -x --ignore=tests/test_agent_runtime.py -q 2>&1 | tail -20
```

If a test fails because it imports `image_utils`, report it in the COMPLETENESS checklist — **do not** add a stub or shim.

### `utils/review_log.py` — A-10 (sub-item 3)

At line 19, the comment says `# Shared with agent/dream_engine.py`. But `agent/dream_engine.py` does not exist. Update the comment to be honest:

```python
DREAM_LOG_FILENAME = "dream-log.jsonl"  # LOW-A10: dream-engine subsystem is deferred; constant kept for future use
```

Keep the constant itself — it's used (or will be used) by the dream log persistence even though the engine itself is deferred.

## Tests to add

Add to `tests/test_feed_store.py` or new `tests/test_low12_13_feed.py`:

**LOW-12 tests:**

1. `test_low12_gitignore_created_on_first_save` — call `save_feed` on a project with no `.gitignore`; assert `.gitignore` exists and contains `.crabcakes/feed.json`.
2. `test_low12_gitignore_no_duplicate` — call `save_feed` twice; assert `.gitignore` contains the entry exactly once.
3. `test_low12_gitignore_existing_file_respected` — pre-create `.gitignore` with `node_modules/`, call `save_feed`, assert `.gitignore` contains BOTH `node_modules/` and `.crabcakes/feed.json`.
4. `test_low12_gitignore_comment_line_not_treated_as_entry` — pre-create `.gitignore` with `# .crabcakes/feed.json` (commented out), call `save_feed`, assert the entry is now uncommented (or appended on a new line).
5. `test_low12_gitignore_atomic_write` — patch the write to raise mid-write, assert `.gitignore` is still valid (not corrupted).

**LOW-13 tests:**

6. `test_low13_save_feed_atomic` — patch `json.dump` to raise mid-write, call `save_feed`, assert `feed.json` does NOT exist (or contains the previous valid content).
7. `test_low13_append_feed_card_atomic` — same as above for `append_feed_card`.
8. `test_low13_update_feed_card_atomic` — same as above for `update_feed_card`.
9. `test_low13_save_feed_permissions` — call `save_feed`, assert `feed.json` has mode `0o600` (or `stat.S_IMODE` to mask type bits).

**A-10 tests:**

10. `test_a10_image_utils_deleted` — assert `os.path.exists("utils/image_utils.py") is False`.
11. `test_a10_review_log_no_dream_engine_ref` — read `utils/review_log.py`, assert the string `"agent/dream_engine.py"` is not in the file (the comment has been updated).

## Verification commands

```bash
# 1. Confirm image_utils is deleted
ls utils/image_utils.py 2>&1
# Expected: No such file

# 2. Confirm dream_engine reference is gone from review_log
git grep -n "agent/dream_engine" utils/review_log.py
# Expected: no output

# 3. Confirm gitignore and atomic write
git grep -nE "_ensure_gitignore_entry|_atomic_write_json" utils/feed_store.py

# 4. Run feed_store tests
python -m pytest tests/test_feed_store.py tests/test_low12_13_feed.py -v 2>&1 | tail -40

# 5. Full suite
python -m pytest tests/ -x --ignore=tests/test_agent_runtime.py -q 2>&1 | tail -20
```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] Edit 1: utils/feed_store.py — added _atomic_write_json, _ensure_gitignore_entry, _atomic_write_text — evidence: <file:line>
- [x/not done] Edit 2: utils/feed_store.py — save_feed uses both helpers — evidence: <file:line>
- [x/not done] Edit 3: utils/feed_store.py — append_feed_card uses both helpers — evidence: <file:line>
- [x/not done] Edit 4: utils/feed_store.py — update_feed_card uses both helpers — evidence: <file:line>
- [x/not done] Edit 5: utils/image_utils.py — DELETED — evidence: <ls output>
- [x/not done] Edit 6: utils/review_log.py — comment updated, no dream_engine reference — evidence: <git grep>
- [x/not done] Tests 1-11: pytest output — evidence: <paste>
```

## Word marker

Include "please proceed" in your reply.

## Important reminders

- **LOW-13 atomicity is real, not theoretical.** Use the existing `agent/runtime.py:1063-1066` pattern (`tmp + os.replace`) as the reference. Do NOT use `os.rename` directly — it doesn't work across filesystem mounts on some systems.
- **LOW-12 gitignore permission is 0o644** (not 0o600) — `.gitignore` is meant to be world-readable.
- **A-10 deletion is a real deletion.** If you find a test imports `image_utils`, report it. Do NOT add a stub.
