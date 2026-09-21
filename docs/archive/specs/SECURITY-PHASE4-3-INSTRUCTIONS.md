# Phase 3 Instructions — Utility Hardening (LOW-6, LOW-8, LOW-9, LOW-10, LOW-11)

**Phase:** 3 of 5
**Findings (original review):** LOW-6, LOW-8, LOW-9, LOW-10, LOW-11
**Master spec:** `docs/specs/SPEC-LOW-FOLLOWUP-PHASE-4.md` §4.5, §4.7, §4.8, §4.9, §4.10
**Authority chain:** Captain → `docs/ARCHITECTURE.md` → spec → this file → code

---

## READ FIRST

1. **Read the master spec** — `docs/specs/SPEC-LOW-FOLLOWUP-PHASE-4.md` §4.5, §4.7, §4.8, §4.9, §4.10
2. **Read these files in full** before editing:
   - `utils/stt.py` (full — 167 lines)
   - `utils/icons.py` (full — ~160 lines)
   - `utils/git_ops.py` (full — 240+ lines, focus on lines 42-234 for the 14 `error=str(e)` sites)
   - `utils/provider_test.py` (around line 245)
   - `utils/mcp_client.py` (around line 273)
   - `utils/diff_parser.py` (lines 144-160, 211-212, 226-227)
   - `utils/agent_defs.py` (lines 190-235, 345-400)
3. **Use the steelFramedCodeWriter prompt** at `prompts/steelFramedCodeWriter.md`

## Edits to make (7 files)

### `utils/stt.py` — LOW-6

At line 16, replace the security manifest:

```python
# Security Manifest:
#   Reads: ALSA device ("default" via PipeWire ALSA plugin)
#   Writes: none
#   Network: faster-whisper downloads model files on first transcription
#            (one-time, from Hugging Face Hub)
#   External: Hugging Face Hub (model download)
#   STT_MODEL_SIZE must be in _VALID_MODEL_SIZES — invalid values
#   fall back to "tiny.en" with a WARNING log (LOW-6).
```

Add the valid-model set near the top (after the SAMPLE_RATE constants):

```python
# LOW-6: allowlist of valid faster-whisper model sizes
_VALID_MODEL_SIZES: frozenset[str] = frozenset({
    "tiny.en", "tiny",
    "base.en", "base",
    "small.en", "small",
    "medium.en", "medium",
    "large-v1", "large-v2", "large-v3",
    "distil-large-v3",
})
_DEFAULT_MODEL_SIZE = "tiny.en"
```

In `__init__` (around line 45-58), replace the `_model_size` assignment:

```python
# LOW-6: validate model_size against allowlist; fall back to default if invalid
self._model_size = self._resolve_model_size(model_size)

@staticmethod
def _resolve_model_size(model_size: str | None) -> str:
    """LOW-6: validate and resolve the model size. Falls back to _DEFAULT_MODEL_SIZE on invalid input."""
    candidate = model_size or os.environ.get("STT_MODEL_SIZE", _DEFAULT_MODEL_SIZE)
    if candidate in _VALID_MODEL_SIZES:
        return candidate
    _logger.warning(
        "LOW-6: invalid STT_MODEL_SIZE %r — falling back to %r (valid sizes: %s)",
        candidate, _DEFAULT_MODEL_SIZE, sorted(_VALID_MODEL_SIZES),
    )
    return _DEFAULT_MODEL_SIZE
```

Make sure `import logging` and `_logger = logging.getLogger(__name__)` are present (they are not currently — you may need to add them at the top).

### `utils/icons.py` — LOW-8

Add helpers at the top of the file (after the imports):

```python
import re

_SVG_ATTR_SAFE_RE = re.compile(r"[^a-zA-Z0-9#.,\- ]")
_VALID_COLOR_HEX_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_SAFE_FALLBACK_COLOR = "#6366f1"
_SAFE_FALLBACK_INITIALS = "??"


def _escape_svg_attr(value: str) -> str:
    """LOW-8: escape a value for safe inclusion as an SVG attribute.

    Strips anything outside a conservative allowlist of common SVG-attr chars.
    """
    if not isinstance(value, str):
        return ""
    return _SVG_ATTR_SAFE_RE.sub("", value)


def _validate_color_hex(value: str) -> str:
    """LOW-8: validate that `value` is a hex color string. Return the value if valid, else a safe fallback."""
    if isinstance(value, str) and _VALID_COLOR_HEX_RE.match(value):
        return value
    _logger = _get_icons_logger()
    _logger.warning("LOW-8: invalid color_hex %r — using fallback", value)
    return _SAFE_FALLBACK_COLOR


def _validate_initials(value: str) -> str:
    """LOW-8: validate that `value` is 1-2 alphanumerics. Return the value if valid, else a safe fallback."""
    if isinstance(value, str) and 1 <= len(value) <= 2 and value.isalnum():
        return value
    return _SAFE_FALLBACK_INITIALS


def _get_icons_logger():
    import logging
    return logging.getLogger(__name__)
```

Update `render_folder_icon` to use the validators:

- `letter` (around line 39): `_validate_initials(letter)` before interpolation
- `color_hex` (around line 64, 84, 85, 95, 105, 110, 119): `_validate_color_hex(color_hex)` once at the top, then use the returned value

Update `render_agent_icon` similarly:

- `initials` (around line 132): `_validate_initials(initials)` before interpolation
- `color_hex` (around line 150, 159): `_validate_color_hex(color_hex)` once at the top

Make sure the `letter_x`, `letter_y` etc. calculations still work after validation. The SVG `f""` template should use the validated (escaped) value, not the raw input.

### `utils/git_ops.py`, `utils/provider_test.py`, `utils/mcp_client.py` — LOW-9

In `utils/git_ops.py`, add a helper at the top (after the imports):

```python
import re as _re_low9

_PATH_PATTERNS = [
    _re_low9.compile(r"/home/[^/\s'\"\\]+"),
    _re_low9.compile(r"/Users/[^/\s'\"\\]+"),
    _re_low9.compile(r"C:\\Users\\[^\\\s'\"\\]+"),
    _re_low9.compile(r"/tmp/[^/\s'\"\\]+"),
]


def _safe_error(e: Exception, *, max_len: int = 200) -> str:
    """LOW-9: convert an exception to a safe, truncated error string.

    - Uses the exception class name + a sanitized message
    - Strips absolute paths (replaces with ~ or ...)
    - Truncates to max_len
    - Never includes the full repr/args
    """
    cls = type(e).__name__
    msg = str(e) or ""
    for pat in _PATH_PATTERNS:
        msg = pat.sub("~", msg)
    # Remove newlines and control chars
    msg = "".join(c for c in msg if c.isprintable() and c not in "\r\n\t")
    if len(msg) > max_len:
        msg = msg[: max_len - 3] + "..."
    return f"{cls}: {msg}" if msg else cls
```

Replace ALL 14 `error=str(e)` sites in `git_ops.py` with `error=_safe_error(e)`. The sites are at lines 42, 55, 66, 77, 115, 126, 136, 146, 162, 172, 192, 207, 224, 234.

In `utils/provider_test.py:245`, replace `error=str(e)[:200]` with `error=_safe_error(e)`. Add the same `_safe_error` helper (copy from git_ops.py or import it: `from utils.git_ops import _safe_error`).

In `utils/mcp_client.py:273`, replace `error=str(e)` with `error=_safe_error(e)`. Add the same helper (or import).

**Important:** the helper should live in ONE place (`utils/git_ops.py`) and be imported by the other two files. Avoid duplication.

### `utils/diff_parser.py` — LOW-10

At lines 149-150, replace `parts[2].lstrip("a/")` with `parts[2].removeprefix("a/")` (Python 3.9+).

At lines 211-212, replace `path_part.lstrip("a/")` with `path_part.removeprefix("a/")`.

At lines 226-227, replace `path_part.lstrip("b/")` with `path_part.removeprefix("b/")`.

Add a `from __future__ import annotations` if needed (Python 3.9 compat is the floor; the project uses 3.11+ per `pyproject.toml`).

### `utils/agent_defs.py` — LOW-11

In `load_agent_defs` (around line 190-235), after parsing each agent_def and before appending to `defs`, call `validate_agent_def`:

```python
agent_def = _parse_agent_file(filepath)
if agent_def is None:
    continue

# Ensure role field is populated
if "role" not in agent_def:
    agent_def["role"] = _derive_role(agent_def)

# LOW-11: validate at load time; skip invalid defs with a WARNING
errors = validate_agent_def(agent_def)
if errors:
    logger.warning(
        "LOW-11: skipping invalid agent def %s (%s): %s",
        fname, agent_def.get("name", "?"), "; ".join(errors),
    )
    continue

# Track the source file
agent_def["_source_file"] = fname
...
```

## Tests to add

Add to existing test files (one per module) or new `tests/test_low6_8_9_10_11.py`:

**LOW-6 (utils/stt.py):**

1. `test_low6_invalid_model_size_falls_back` — `STTEngine(model_size="../../../etc/passwd")` → `engine._model_size == "tiny.en"`, WARNING logged.
2. `test_low6_valid_model_sizes_pass_through` — `STTEngine(model_size="medium.en")` → `engine._model_size == "medium.en"`, no WARNING.
3. `test_low6_env_var_invalid_falls_back` — set `STT_MODEL_SIZE=garbage`, construct `STTEngine()`, assert fallback.
4. `test_low6_manifest_does_not_claim_no_network` — read the source file, assert the manifest no longer says "no network calls".

**LOW-8 (utils/icons.py):**

5. `test_low8_malicious_color_hex_falls_back` — `render_agent_icon("#6366f1</path><script>alert(1)</script><path fill=", "Qr")` → result is non-None and the SVG (read from the tmp file path) does NOT contain `<script>`.
6. `test_low8_malicious_initials_stripped` — `render_agent_icon("#6366f1", "<script>")` → result is non-None, initials rendered as `??` or similar.
7. `test_low8_valid_color_passes_through` — `render_agent_icon("#abcdef", "Qr")` → non-None.

**LOW-9 (utils/git_ops.py, provider_test.py, mcp_client.py):**

8. `test_low9_safe_error_strips_paths` — `_safe_error(Exception("/home/user/secret/file"))` → result does not contain `/home/user`, contains `~`.
9. `test_low9_safe_error_truncates` — `_safe_error(Exception("x" * 1000))` → result length ≤ 200.
10. `test_low9_safe_error_uses_class_name` — `_safe_error(ValueError("foo"))` → result starts with `ValueError`.
11. `test_low9_git_ops_uses_safe_error` — patch `_safe_error` to return "REDACTED", trigger a git failure, assert the `GitResult.error == "REDACTED"`.

**LOW-10 (utils/diff_parser.py):**

12. `test_low10_lstrip_bug_fixed` — `parse_diff("diff --git a/apple.py b/apple.py\n")` → `old_path == "apple.py"`, `new_path == "apple.py"`.
13. `test_low10_apple_file_not_mangled` — feed a diff with `diff --git a/ab.txt b/ab.txt` and assert `old_path == "ab.txt"`.
14. `test_low10_afile_with_leading_a` — feed `diff --git a/afile.txt b/afile.txt`, assert `old_path == "afile.txt"`.

**LOW-11 (utils/agent_defs.py):**

15. `test_low11_load_skips_invalid_def` — write an agent def with an unknown tool name to a temp agents dir, call `load_agent_defs()`, assert the invalid def is NOT in the result.
16. `test_low11_load_includes_valid_def` — write a valid def, assert it IS in the result.
17. `test_low11_load_warns_on_invalid` — write an invalid def, assert a WARNING is logged with the agent name and the errors.

## Verification commands

```bash
# 1. Confirm STT manifest no longer claims "no network"
git grep -nE "no network calls" utils/stt.py
# Expected: no output

# 2. Confirm stt model_size validation
git grep -nE "_VALID_MODEL_SIZES|_resolve_model_size" utils/stt.py

# 3. Confirm icons validators
git grep -nE "_validate_color_hex|_validate_initials|_escape_svg_attr" utils/icons.py

# 4. Confirm _safe_error used everywhere
git grep -nE "error=str\(e\)" utils/git_ops.py utils/provider_test.py utils/mcp_client.py
# Expected: no output

# 5. Confirm lstrip("a/") and lstrip("b/") are gone
git grep -nE 'lstrip\("a/"\)|lstrip\("b/"\)' utils/diff_parser.py
# Expected: no output

# 6. Confirm validate_agent_def called in load_agent_defs
git grep -nE "validate_agent_def\(agent_def\)" utils/agent_defs.py

# 7. Run tests
python -m pytest tests/ -x --ignore=tests/test_agent_runtime.py -q -k "low6 or low8 or low9 or low10 or low11" 2>&1 | tail -40

# 8. Full suite
python -m pytest tests/ -x --ignore=tests/test_agent_runtime.py -q 2>&1 | tail -20
```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] Edit 1: utils/stt.py — manifest updated — evidence: <file:line>
- [x/not done] Edit 2: utils/stt.py — _VALID_MODEL_SIZES + _resolve_model_size — evidence: <file:line>
- [x/not done] Edit 3: utils/icons.py — _validate_color_hex, _validate_initials, _escape_svg_attr — evidence: <file:line>
- [x/not done] Edit 4: utils/icons.py — render_folder_icon uses validators — evidence: <file:line>
- [x/not done] Edit 5: utils/icons.py — render_agent_icon uses validators — evidence: <file:line>
- [x/not done] Edit 6: utils/git_ops.py — _safe_error helper added — evidence: <file:line>
- [x/not done] Edit 7: utils/git_ops.py — all 14 error=str(e) sites replaced — evidence: <git grep>
- [x/not done] Edit 8: utils/provider_test.py — _safe_error used — evidence: <file:line>
- [x/not done] Edit 9: utils/mcp_client.py — _safe_error used — evidence: <file:line>
- [x/not done] Edit 10: utils/diff_parser.py — lstrip → removeprefix (3 sites) — evidence: <git grep>
- [x/not done] Edit 11: utils/agent_defs.py — validate_agent_def called in load_agent_defs — evidence: <file:line>
- [x/not done] Tests 1-17: pytest output — evidence: <paste>
```

## Word marker

Include "please proceed" in your reply.

## Important reminders

- **LOW-9 helper must be defined once.** Use `utils/git_ops.py` as the source of truth, then `from utils.git_ops import _safe_error` in the other two files. Don't duplicate.
- **LOW-8 SVG escaping is defense-in-depth.** The current callers don't pass malicious input, but the validator prevents future callers from accidentally (or maliciously) injecting SVG.
- **LOW-11 may break existing tests.** If a test in `test_agent_defs.py` relies on `load_agent_defs` returning a def that has an unknown tool name, you'll need to fix the test fixture to be valid. Don't change the test's assertion to make it pass — fix the fixture.
