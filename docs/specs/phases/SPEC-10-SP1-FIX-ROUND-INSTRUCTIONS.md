# SPEC-10 SP1 Fix Round — splitlines sanitization + non-str guard

**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `utils/git_ops.py` `commit()` guard ONLY + tests appended to
`tests/test_git_ops.py`. Nothing else changes.

---

## BUG #1 (MEDIUM) — 8 of 10 Python line boundaries pass the guard

Debugger probe + Supervisor reproduction: `agent_trailer =
"special:coder\u2028Agent: evil-agent"` commits successfully; Python
`splitlines()` consumers see TWO `Agent:` lines. The current 4-char set
(`\r \n \x00 \x1f`) misses `\v \f \x1c \x1d \x1e \x85 U+2028 U+2029`.

### Fix

Replace the character-set condition with Python's own boundary definition
plus the non-boundary controls:

```python
    if agent_trailer is not None:
        if not isinstance(agent_trailer, str):
            return GitResult(
                success=False,
                stdout="",
                error="agent_trailer rejected: must be a string",
                sha=None,
            )
        trailer_value = agent_trailer.strip()
        # SPEC-10 SP1 fix (BUG#1): gate on Python's OWN line-boundary
        # definition — str.splitlines() recognises TEN boundaries (\n \r
        # \r\n \v \f \x1c \x1d \x1e \x85 U+2028 U+2029); the previous 4-char
        # set let 8 of them forge additional trailer lines for any
        # Python-side body consumer. NUL and \x1f are NOT splitlines
        # boundaries, so they stay explicitly checked.
        if (
            not trailer_value
            or len(trailer_value.splitlines()) != 1
            or any(c in trailer_value for c in ("\x00", "\x1f"))
        ):
            return GitResult(
                success=False,
                stdout="",
                error=(
                    "agent_trailer rejected: must be a non-empty single line "
                    "without control characters or line separators"
                ),
            )
        message = f"{message}\n\nAgent: {trailer_value}"
```

Also update the block comment above the guard (the one referencing D1) to
state the splitlines guarantee accurately — no overstated claims.

### Tests (append)

- `test_commit_agent_trailer_rejects_unicode_and_vertical_separators` —
  parametrized over `["\u2028", "\u2029", "\x0b", "\x0c", "\x1c", "\x1d",
  "\x1e", "\x85"]` as `agent_trailer` values (standalone and embedded mid-value,
  e.g. `"special:coder\u2028Agent: evil"`): assert `success is False`,
  `"reject" in result.error.lower()`, and HEAD sha unchanged (no commit).
- `test_commit_agent_trailer_rejects_non_str` — parametrized over
  `[123, 3.14, None, ["k"], b"key"]` — wait: `None` is the documented default
  (no trailer). Exclude `None`. Over `[123, 3.14, ["k"], b"key"]`: assert a
  `GitResult` is RETURNED (no exception), `success is False`, HEAD unchanged.
  Use `pytest.raises(None)`-style guard: wrap the call so any raise fails the
  test explicitly.

### RED proof

- BUG#1 test RED pre-fix: the separator values pass the current guard →
  commit succeeds → `success is False` assertion fails (and HEAD-unchanged
  fails). Paste it.
- BUG#2 test RED pre-fix: `123` raises AttributeError → test fails via the
  raise. Paste it.

## What must NOT change

- `agent_trailer=None` path byte-identical (test 3 from SP1 still guards).
- Error-message contract for the original 4-char rejects stays compatible:
  the shared error string may change wording — tests assert `"reject" in
  error.lower()`, so wording changes are safe; do not break existing tests.
- No changes to any other function or file.

## Battery (paste all outputs)

- `python -m pytest tests/test_git_ops.py -q` (use `.venv/bin/python`)
- ruff profile vs HEAD (rule-for-rule identity)
- pyright `utils/git_ops.py` → 0
- RED proofs pre-fix

## COMPLETENESS (mandatory)

- [ ] BUG#1 fix + comment correction — evidence: diff hunk
- [ ] BUG#2 fix (isinstance guard) — evidence: diff hunk
- [ ] New tests appended + count — evidence: pytest output
- [ ] RED proofs — evidence: pasted failures
- [ ] ruff/pyright — evidence: outputs
- [ ] Related issues found, not fixed (flagged)

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
