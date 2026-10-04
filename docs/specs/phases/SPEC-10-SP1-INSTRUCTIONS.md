# SPEC-10 SP1 — git_ops trailer support

**Spec:** `docs/specs/SPEC-10-REVIEW-QUEUES.md`
**Pre-flight:** `docs/specs/phases/SPEC-10-PREFLIGHT-DECISIONS.md` (D1)
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** ONE file, ONE focused change — `utils/git_ops.py`: `commit()` gains
`agent_trailer` support. No UI, no handler changes.

---

## 1. The change

Anchor: `def commit(` in `utils/git_ops.py` (currently at line ~130; anchor by
identifier, not line). Read the file in full before editing (steelFramed
read-before-touch rule).

Current signature:

```python
def commit(project_path: str, message: str, allow_empty: bool = False) -> GitResult:
```

New signature:

```python
def commit(project_path: str, message: str, allow_empty: bool = False,
           agent_trailer: str | None = None) -> GitResult:
```

Behavior when `agent_trailer` is set — insert at the TOP of the function body,
before the existing `try:`:

```python
    if agent_trailer is not None:
        trailer_value = agent_trailer.strip()
        if not trailer_value or any(
            c in trailer_value for c in ("\r", "\n", "\x00", "\x1f")
        ):
            return GitResult(
                success=False,
                stdout="",
                error=(
                    "agent_trailer rejected: must be a non-empty single line "
                    "without control characters"
                ),
                sha=None,
            )
        message = f"{message}\n\nAgent: {trailer_value}"
```

Docstring: extend the existing `Args:` block with:

```
        agent_trailer: Optional session key appended as a literal
            "Agent: <key>" line in the commit message body. The value must
            be a single line without control characters — values containing
            newlines/NUL/unit-separator (or empty after strip) are REJECTED
            fail-closed (success=False, error explains the rejection) rather
            than silently dropping attribution.
```

## 2. Sanitization rationale (D1)

- Rejected chars: `\r`, `\n` (could forge additional trailer lines or break
  message format), `\x00` (NUL — git plumbing hazard), `\x1f` (ASCII unit
  separator — mirrors git_ops' existing separator concerns, see file_log
  BUG #1 family).
- Empty-after-strip rejected: an `Agent: ` line with no value is worse than
  no trailer (ambiguous attribution).
- Fail-closed: return `success=False`, NO commit attempted. Never silently
  drop the trailer — silent attribution loss is the bug class this spec exists
  to close.

## 3. What must NOT change

- Behavior when `agent_trailer=None` (default): byte-identical to today.
- `allow_empty` logic, empty-check, GitPython commit call, GitResult shapes.
- Any other function in `utils/git_ops.py`.
- All existing tests in `tests/test_git_ops.py` must pass unmodified.

## 4. Tests (append to `tests/test_git_ops.py`)

RED-first (steelFramed rule 4): write tests that fail against current HEAD,
prove the failure, then land the edit.

1. `test_commit_agent_trailer_in_log` — init repo in tmp_path, write file,
   stage via `stage_all`, commit with `agent_trailer="special:coder"`;
   assert success, and assert `git log --format=%B -1` output contains the
   exact line `Agent: special:coder`.
2. `test_commit_agent_trailer_rejects_newline` — value
   `"bad\nAgent: fake"` → success=False, error mentions rejection; verify NO
   new commit was created (HEAD sha unchanged / repo still clean).
3. `test_commit_no_trailer_no_agent_line` — commit without the param: assert
   log message contains no "Agent:" line (guard against accidental default).
4. `test_commit_agent_trailer_rejects_empty_after_strip` — `"   "` → reject,
   no commit.
5. `test_commit_agent_trailer_rejects_nul` — `"\x00"` → reject, no commit.
6. `test_commit_agent_trailer_rejects_unit_separator` — `"\x1f"` → reject,
   no commit.
7. `test_commit_agent_trailer_strips_surrounding_whitespace` —
   `"  special:coder  "` → committed trailer line is exactly
   `Agent: special:coder`.

## 5. Verification battery (paste all outputs)

- `python -m pytest tests/test_git_ops.py -q` → all green (existing + 7 new)
- `ruff check utils/git_ops.py tests/test_git_ops.py` → 0 findings
- `pyright utils/git_ops.py` → 0 errors (file has 0 today; keep it 0)
- `wc -l utils/git_ops.py` → 382 → ~400 (+15–20 lines expected)

## 6. Report back (mandatory COMPLETENESS)

- [ ] Edit 1: `commit()` signature + docstring + trailer guard — evidence: diff hunk pasted
- [ ] Edit 2: 7 tests appended — evidence: pytest output with count (7 new)
- [ ] RED proofs: each new test's pre-edit failure pasted
- [ ] ruff / pyright / wc -l outputs pasted
- [ ] Related issues found, NOT fixed in this phase (flagged for supervisor)

Invoke `prompts/steelFramedCodeWriter.md` before writing anything. Please
write the change per this brief and report when done.
