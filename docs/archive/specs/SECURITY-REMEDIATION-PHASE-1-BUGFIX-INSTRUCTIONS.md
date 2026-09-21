# Phase 1 BUG FIX — HIGH-6 (auto-link regex + paren handling) + A-1 (module-level load)

**Original instructions:** `/path/to/projects/crabcakes/docs/specs/SECURITY-REMEDIATION-PHASE-1-INSTRUCTIONS.md`

**Master spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-SECURITY-REMEDIATION.md` §2.3 (HIGH-3), §2.4 (HIGH-6), §2.8 (A-1)

**Source audit:** `/path/to/projects/crabcakes/docs/SECURITY_ARCHITECTURE_REVIEW.md` §4 (HIGH-3, HIGH-6, A-1)

**Audit verdict (Qaster, 2026-06-18 20:22 PDT):** ❌ NEEDS BUG FIX
- HIGH-3: ✅ correct (api_key removed from serialization, _conversations_dir chmod 0o700, _resolve_api_key_for_conversation defined)
- HIGH-6: ❌ 3 tests failing — implementation bugs in `_AUTO_LINK_RE` regex and link regex
- A-1: ❌ module-level `_load_identity()` call at line 185 still raises on import

**Three specific deviations to fix:**

---

## Bug 1: `_AUTO_LINK_RE` regex too narrow (HIGH-6)

### The problem

The spec's HIGH-6 fix is meant to render the link with a red ⚠ prefix. The test `test_auto_link_file_with_warning` asserts that bare `file:///etc/passwd` in plain text gets a warning. But:

```python
# Current regex at utils/markdown.py:38
_AUTO_LINK_RE = re.compile(
    r'(?<![a-zA-Z0-9/:])'
    r'(https?://[^\s<>"`\'\[\]()]+)'  # ← only http/https
    , re.IGNORECASE
)
```

Bare `file://`, `javascript:`, `data:`, `smb://`, `ssh://`, custom schemes — none of them match. The URL stays as plain text, no `<a>` tag, no warning.

### The fix

Broaden the regex to match any URL scheme:

```python
_AUTO_LINK_RE = re.compile(
    r'(?<![a-zA-Z0-9/:])'  # not preceded by alphanum or ://
    r'([a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>"`\'\[\]()]+)'
    r'|'
    r'(?<!["\'])'
    r'((?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s<>"`\'\[\]()]*)?)'
    , re.IGNORECASE
)
```

This matches:
- `http://`, `https://`, `file://`, `javascript:`, `data:`, `smb://`, `ssh://`, `myapp://`, etc.
- Bare domains like `example.com` (no scheme)

**Verify** the change in `_auto_link` at line 230: the function already prepends `_WARNING_PREFIX` for non-allowlisted schemes via `_validate_link_url`. So once the regex matches more schemes, the warning logic fires correctly.

> **Note:** If you want to be conservative (only auto-link known schemes, not bare domains), use a tighter regex like `(?:https?|file|javascript|data|smb|ssh|ftp|myapp)://[^\s...]+`. The spec's intent is "any URL gets rendered as a link with appropriate warning," so the broader regex is the right fix.

---

## Bug 2: Markdown link regex doesn't handle URLs with parens (HIGH-6)

### The problem

The markdown link regex `\[([^\]]+)\]\(([^)]+)\)` (line 227) stops at the first `)`. URLs with parens break:

```
input:  [click](javascript:alert(1))
output: <a href="javascript:alert(1)">click</a>)   ← trailing ")" leaks

input:  [data](data:text/html,<script>alert(1)</script>)
output: <a href="data:text/html,%3Cscript%3Ealert(1)">data</a></script>)   ← broken
```

The test `test_javascript_link_with_warning` and `test_data_uri_with_warning` assert the link is correct, so they fail.

### The fix

Replace the regex with one that handles parens. The standard approach is "match anything except `)` that isn't preceded by a balanced paren." For practical purposes, a simpler approach works:

```python
# Replace the regex at line 227
LINK_RE = re.compile(
    r'\[([^\]]+)\]\(((?:[^()]|\([^()]*\))+)\)'  # balanced parens, 1 level deep
)
```

This handles up to 1 level of nested parens. For deeper nesting (rare in URLs), it would fail — but that's an acceptable trade-off.

**Update the call site** (around line 227):

**Before:**
```python
    protected = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _link_replace_and_protect, protected)
```

**After:**
```python
    protected = LINK_RE.sub(_link_replace_and_protect, protected)
```

**Update the inner function signature** if needed — the new regex still produces 2 groups (label, url), so `_link_replace_and_protect` should work unchanged. **Verify by running the test suite.**

---

## Bug 3: Module-level `_load_identity()` call still raises on import (A-1)

### The problem

QTR's A-1 fix moved the `_load_identity()` call from `__init__` to `start()`. But the module-level call at line 185 still fires on import:

```python
# gateway/client.py:185
# Preload identity on module import (catches errors immediately)
_load_identity()  # ← THIS STILL RAISES IF IDENTITY FILE IS MISSING
```

This means:
- `import gateway.client` → raises (if identity file missing)
- A-1 spec: "Importing the module and constructing GatewayClient is now safe even if ~/.openclaw/identity/device-auth.json is missing"

QTR's fix only made construction safe, not import. A-1 is partially fixed.

### The fix

Delete the module-level `_load_identity()` call at line 185. The lazy load in `start()` (line 248-250) handles the actual loading. The comment "catches errors immediately" is not a valid reason to fail the import.

**Before (line 183-186):**
```python
_IDENTITY_CACHE = None
    return _load_identity()


# Preload identity on module import (catches errors immediately)
_load_identity()
```

**After:**
```python
_IDENTITY_CACHE = None
    return _load_identity()


# A-1: identity is loaded lazily on first start(). Importing the module
# is now safe even if ~/.openclaw/identity/device-auth.json is missing.


# Auth scopes
```

(Just delete the `_load_identity()` call and replace the comment.)

> **Note:** The `_load_identity` function at line 75 still has the docstring "catches errors immediately" and presumably raises. That's fine — the function should raise when called, just not at import time. The lazy loading in `start()` catches and surfaces the error.

---

## Rules

- Use the `prompts/steelFramedCodeWriter.md` prompt
- Read every file in full before editing
- Anchor edits to identifiers, NOT line numbers
- Scope is exactly the 3 bug fixes above. Do NOT touch any other file.
- Do NOT silently expand scope. If you find a related issue, note it in the COMPLETENESS checklist.
- Do NOT touch HIGH-3 — it was correct in Phase 1 (the test suite confirms 60/60 in test_conversation.py).
- Do NOT touch Phase 0 work (`agent/enforcement.py`, `agent/tools.py`).
- Do NOT add new dependencies or change the architecture.

## Verification commands to run (in order)

**1. HIGH-6 — Auto-link regex broadened (Bug 1):**

```bash
cd /path/to/projects/crabcakes && python3 -c "
import sys; sys.path.insert(0, '.')
from utils.markdown import _AUTO_LINK_RE
# Test that the regex matches various schemes
test_urls = [
    'http://example.com',
    'https://example.com',
    'file:///etc/passwd',
    'javascript:alert(1)',
    'data:text/html,<x>',
    'smb://server/share',
    'ssh://user@host',
    'myapp://action',
]
for url in test_urls:
    match = _AUTO_LINK_RE.search(f'See {url} now')
    status = '✓' if match else '✗'
    print(f'  {status} regex matches {url!r}: {bool(match)}')
"
```

Expect: all 8 URLs match.

**2. HIGH-6 — Markdown link paren handling (Bug 2):**

```bash
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_markdown.py -q 2>&1 | tail -5
```

Expect: 58/58 passed (no failures). Specifically, these 3 tests should now pass:
- `test_javascript_link_with_warning`
- `test_data_uri_with_warning`
- `test_auto_link_file_with_warning`

**3. A-1 — Module-level _load_identity call removed (Bug 3):**

```bash
cd /path/to/projects/crabcakes && grep -n "^_load_identity()" gateway/client.py
```

Expect: 0 matches at module level (the function definition `_load_identity()` at line 75 doesn't match this pattern; only standalone calls).

**4. A-1 — Importing gateway.client is safe:**

```bash
cd /path/to/projects/crabcakes && python3 -c "
import sys; sys.path.insert(0, '.')
# Simulate missing identity file by patching
import os
identity_path = os.path.expanduser('~/.openclaw/identity/device-auth.json')
if os.path.exists(identity_path):
    print('Identity file exists — A-1 test inconclusive. Move the file aside to test.')
    sys.exit(0)
# If identity file is missing, import should still work
try:
    import gateway.client
    print('A-1: import gateway.client succeeded even with missing identity — PASS')
except (FileNotFoundError, OSError) as e:
    print(f'A-1: import raised {type(e).__name__}: {e} — FAIL')
    sys.exit(1)
"
```

Expect: PASS (or "test inconclusive" if identity file happens to be present).

**5. Full Phase 1 test run (no regressions):**

```bash
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_markdown.py tests/test_conversation.py tests/test_tools.py tests/test_enforcement.py -q 2>&1 | tail -5
```

Expect: ≥ 200 passed (143 Phase 0 + 60 conversation + 58 markdown after fix, with overlap).

**6. No accidental scope creep:**

```bash
cd /path/to/projects/crabcakes && git diff HEAD --stat
```

Expect: only `utils/markdown.py` (Bugs 1+2) and `gateway/client.py` (Bug 3) changed. NO changes to:
- `agent/runtime.py` (HIGH-3 was correct)
- `models/conversation.py` (HIGH-3 was correct)
- `agent/enforcement.py` or `agent/tools.py` (Phase 0 — do not touch)

**7. Specific grep checks for the 3 fixes:**

```bash
cd /path/to/projects/crabcakes && grep -n "Preload identity on module" gateway/client.py
```
Expect: comment gone (was at line 184)

```bash
cd /path/to/projects/crabcakes && grep -n "LINK_RE\|balanced parens" utils/markdown.py
```
Expect: 1+ match (the new regex)

```bash
cd /path/to/projects/crabcakes && grep -c "https?://" utils/markdown.py
```
Expect: 0 matches (the old `https?://` only regex is replaced with broader scheme match)

---

## Report

When done, send back a completion report with:
- Files changed with actual line numbers
- Output of all 7 verification commands
- Full pytest output for `test_markdown.py`
- COMPLETENESS checklist (per steelFramedCodeWriter Step 6.5) — list each bug fix with `[x]`
- Any related issues found (flagged, not silently fixed)

**Required word marker for /ask acknowledgment: "please write"** — include it in your response.

**Do not skip the COMPLETENESS checklist.** Include every bug fix with `[x]` or `[NOT DONE] WHY` and paste the evidence.

**LESSON REINFORCED:** This is the 2nd bug-fix delegation in 2 phases. The pattern is clear: QTR's deviation instinct is to add "what I think is needed" rather than follow the spec exactly. The protocol rule is **follow the spec exactly — deviation requires explicit user approval, not silent substitution.** The Phase 1 spec explicitly said "Importing the module and constructing GatewayClient is now safe" — QTR's fix made construction safe but not import, missing half the requirement. **Re-read the spec line-by-line before writing code.**
