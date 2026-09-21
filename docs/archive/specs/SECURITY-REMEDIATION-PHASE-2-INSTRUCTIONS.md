# Phase 2 of 4 — Medium Severity Findings (MED-1 through MED-13)

**Master spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-SECURITY-REMEDIATION.md` (1,211 lines)

**Phase 0 + 1 status:** ✅ SHIPPED (commits `b5dcccc`, `9943740` on `main`, pushed to `origin/main`)
- 7 of 46 findings complete: CRIT-1, CRIT-2, HIGH-1, HIGH-3, HIGH-5, HIGH-6, A-1
- 2 bug-fix cycles (one per phase, both with substantive deviations)

**Scope of this phase:** §2.1-2.16 of the spec. **13 findings: MED-1 through MED-13.** Largest phase by finding count.

---

## ⚠️ MANDATORY: Read every file in full before writing any code

Per `prompts/steelFramedCodeWriter.md` Rule 1, before writing ANY code, read EVERY file you will touch. ALL of it. Not snippets — the whole file. The spec's line numbers were accurate as of the audit (2026-06-10, HEAD `4fc79c1`); current HEAD is `9943740` (Phase 0+1 changes may have shifted line numbers). Anchor edits to identifiers, NOT line numbers.

**Files to read in full before writing any code:**

1. `agent/tools.py` (read full) — find `_approval_callback` (line 66), `_BLOCKLIST` (line 102), `web_fetch` tool def, `search_files` tool def, `git checkout` tool def
2. `agent/runtime.py` (read full, 1501+ LOC) — find tool loop (line 1147+), streaming usage (line 614, 631)
3. `utils/markdown.py` (read full) — find `format_markdown` (line 50), `****` normalization loop at line 86-90
4. `ui/handlers/review_handler.py` (read full) — find `reject_changes` flow (line 264-301), `state.last_check_files`
5. `ui/handlers/feed_handler.py` (read full) — find the feed-card reject path (line 617-621)
6. `utils/improve.py` (read full) — find config.json read (line 80), base_url handling
7. `utils/provider_test.py` (read full) — find base_url validation
8. `utils/mcp_config.py` (read full) — find `${VAR}` substitution (line 60-63), mcp-servers.json read (line 116)
9. `utils/feedback_processor.py` (read full) — find the journal write at line 130-147
10. `utils/agent_defs.py` (read full) — find `save_provider` at line 516-531, `load_agent_defs` at line 197-223
11. `utils/git_ops.py` (read full) — find `checkout_paths` at line 116-123
12. `ui/handlers/chat_render_handler.py` (read full) — find `set_markup` calls at line 695, 709, 716
13. `ui/views/session_menu.py` (read full) — find `set_markup` calls
14. `ui/views/main_content.py` (read full) — find `set_markup` calls at line 215, 255, 309
15. `tests/test_*.py` — read existing test patterns
16. `docs/ARCHITECTURE.md` — Section 3.x for each module

**Output a discovery block before writing code** (per steelFramedCodeWriter Step 0).

---

## The 13 findings (high-level)

| Finding | File(s) | Effort | Risk |
|---|---|---|---|
| MED-1 | `agent/tools.py` + `agent/runtime.py` | High | Medium (refactor of approval callback) |
| MED-2 | `agent/tools.py` (docstring only) | Trivial | None |
| MED-3 | `agent/tools.py` (opt-in per Q3) | Medium | Low |
| MED-4 | `ui/handlers/review_handler.py` + `feed_handler.py` | Medium | Medium (changes reject flow) |
| MED-5 | `utils/improve.py` + `utils/provider_test.py` + new `utils/provider_url.py` | Medium | Low |
| MED-6 | `utils/mcp_config.py` + `utils/improve.py` + `gateway/client.py` | Low | Low |
| MED-7 | `utils/feedback_processor.py` | Low | Low |
| MED-8 | `utils/agent_defs.py` | Low | Low |
| MED-9 | `ui/handlers/chat_render_handler.py` + 2 view files | Low | Low |
| MED-10 | `utils/markdown.py` | Low | None (bug fix) |
| MED-11 | `agent/tools.py` + `utils/git_ops.py` + `ui/handlers/feed_handler.py` | Medium | Low |
| MED-12 | `utils/mcp_config.py` | Low | Low |
| MED-13 | `agent/runtime.py` | Medium | Low |

---

## Edits (13 findings, ~12 files)

### Edit 1: MED-2 — `_BLOCKLIST` docstring fix (trivial)

In `agent/tools.py` at line 96-100, update the comment to remove "safety tier" framing per spec §2.2 edit 3. **Single line change.** Defense-in-depth only, not authoritative.

### Edit 2: MED-1 — Per-instance approval callback (substantial)

**a) `agent/runtime.py`:**
- Add `self._approval_callback: Callable[[str, str, dict], bool] | None = None` to `AgentRuntime.__init__`
- Add `def set_approval_callback(self, cb)` instance method
- In the tool loop at line 1456-1462, replace the global-swap pattern with per-call callback passed through `execute_tool`
- The instance callback takes precedence over the global

**b) `agent/tools.py`:**
- Add per-call approval callback parameter to `execute_tool(tool_name, args, project_path, session_key, approval_callback=None)`
- In each tool that calls `_get_approval` (line 78 area), use the per-call callback if provided, fall back to global
- Keep the global `_approval_callback` for backward compat (deprecated)

**Per Q9 decision:** per-`AgentRuntime` instance state. Pattern follows existing class-instance conventions.

### Edit 3: MED-3 — web_fetch host allowlist (opt-in per Q3)

**Per Q3 decision:** ship disabled by default, opt-in via config. Default behavior unchanged (web_fetch works as before). When enabled (`CRABCAKES_WEB_FETCH_RESTRICT=1` env var or similar config flag), web_fetch rejects requests to:
- Private IP ranges (RFC 1918: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
- Loopback (127.0.0.0/8, ::1)
- Link-local (169.254.0.0/16, fe80::/10)
- Hostnames without a public DNS resolution

Use the `ipaddress` module. Reject before making the request, log the denial.

### Edit 4: MED-4 — Scope `/reject` to specific files

**a) `ui/handlers/review_handler.py`:** At lines 264-301, the `reject_changes` flow does `git checkout <sha> -- .` which reverts all tracked files. Scope to `state.last_check_files` (the list of files the agent modified in this turn). Show a confirmation dialog listing exactly which files will be reverted.

**b) `ui/handlers/feed_handler.py`:** At line 617-621, the feed-card reject path uses `["."]` — scope to the card's `file_path` (single file).

### Edit 5: MED-5 — https-only base_url + drop Authorization on cross-host redirects

**a) New file: `utils/provider_url.py`:** Add `validate_provider_url(url)` shared helper (per spec §2.10):
```python
from urllib.parse import urlparse

def validate_provider_url(url: str) -> None:
    """Raise ValueError if `url` has a non-HTTPS scheme (except loopback)."""
    parsed = urlparse(url)
    is_loopback = parsed.hostname in ("localhost", "127.0.0.1", "::1", None)
    if not is_loopback and parsed.scheme != "https":
        raise ValueError(
            f"Provider URL must use https:// for non-loopback hosts: {url}"
        )
```

**b) `utils/improve.py`:** At line 80 (config.json read), add permission check. At lines 85, 129-131, call `validate_provider_url()`. Configure the HTTP client to drop `Authorization` header on cross-host redirects.

**c) `utils/provider_test.py`:** At lines 100, 107-110, 149-152, call `validate_provider_url()`. Same Authorization handling.

### Edit 6: MED-6 — Permission/ownership check on config files

Add `os.stat` + mode check to 3 files:
- `utils/mcp_config.py:116` (mcp-servers.json)
- `utils/improve.py:80` (config.json)
- `gateway/client.py:148` (identity file — wait, that was removed in Phase 1 A-1; this is for an ED25519 key file at line 148)

**Pattern (use a shared helper in a new file `utils/secure_file.py` or add to `utils/file_security.py`):**
```python
def assert_secure_file(path: str, expected_owner: bool = True) -> None:
    """Raise PermissionError if file is not owned by current user or has unsafe perms."""
    st = os.stat(path)
    if expected_owner and st.st_uid != os.getuid():
        raise PermissionError(f"{path} not owned by current user: uid={st.st_uid}")
    if st.st_mode & 0o077:
        raise PermissionError(f"{path} has unsafe permissions: {oct(st.st_mode)}")
```

### Edit 7: MED-7 — Sanitize feedback_processor writes

In `utils/feedback_processor.py` at line 130-147, before writing `entry_text` to the bug journal, strip:
- Lines starting with `#` (markdown headings)
- Fence-break sequences (` ``` `)
- Instruction-like lines (regex: `(?i)(ignore|disregard|forget)\s+(previous|prior|above|all)` and `new instructions:`)

### Edit 8: MED-8 — Atomic+0600 for `save_provider`

In `utils/agent_defs.py` at lines 516-531, copy the pattern from `utils/providers_store.py:save_providers` (line 127):
- Write to `.tmp`
- `os.rename(tmp, path)` (atomic)
- `os.chmod(path, 0o600)`

Also do the same for `agent/config.py` agent.json writes (per spec §2.29).

### Edit 9: MED-9 — Escape interpolated values in set_markup

**a) `ui/handlers/chat_render_handler.py`:** At lines 695, 709, 716, wrap interpolated values (`task_id`, `assigned_to`, etc.) in `GLib.markup_escape_text()` or `escape_for_pango()`.

**b) `ui/views/session_menu.py`:** At lines 49, 79, 139, 185, 191, wrap interpolated values in `GLib.markup_escape_text()`.

**c) `ui/views/main_content.py`:** At lines 215, 255, wrap interpolated values in `escape_for_pango()`. Line 309 already uses `GLib.markup_escape_text()` correctly — leave it alone.

### Edit 10: MED-10 — ReDoS fix in `****` normalization

In `utils/markdown.py` at lines 86-90, replace the `while text != prev: prev = text; text = text.replace('****', f'**{_ZWSP}**')` quadratic loop with a single non-overlapping regex pass:
```python
text = re.sub(r'\*\*(?=\*\*)', '**' + _ZWSP, text)
```

Also add a cap on input length (e.g., 100KB) — if exceeded, truncate and append a truncation marker. This prevents the ReDoS on adversarial input.

### Edit 11: MED-11 — Validate commit_sha + prepend `--` in grep

**a) `agent/tools.py`:** For `git checkout`, validate `commit_sha` against `^(HEAD|[0-9a-fA-F]{4,40})$` before passing. For `search_files`'s underlying grep call, prepend `--` before the pattern in the argv list.

**b) `utils/git_ops.py`:** At `checkout_paths` (line 116-123), add the same `commit_sha` validation. The existing call already has `"--"` before paths; the validation prevents argument injection.

**c) `ui/handlers/feed_handler.py`:** At line 619 (the `git_ops.checkout_paths` call site), verify the `commit_sha` parameter passes the regex. The feed handler should already be passing a valid sha, but add a defensive check.

### Edit 12: MED-12 — MCP env var allowlist

In `utils/mcp_config.py` at lines 60-63 (the `${VAR}` substitution block):
- If `os.environ.get(var, "")` returns `""` AND the var is not in the process env, log a warning
- Add an allowlist of forwardable env var names: `{"PATH", "HOME", "LANG", "VIRTUAL_ENV", "PYTHONPATH"}`
- Refuse to forward any other var (raise or log + skip)

### Edit 13: MED-13 — Parse streaming usage

In `agent/runtime.py` at line 614 and 631 (where `usage: {}` appears in the streaming path):
- Add `stream_options: {"include_usage": True}` to the OpenAI-compatible API call params
- Parse `usage` from the streaming response chunks (the final chunk includes it)
- Track tokens for cost limit enforcement

---

## Rules

- Use the `prompts/steelFramedCodeWriter.md` prompt
- Read every file in full before editing
- Anchor edits to identifiers, NOT line numbers
- Scope is exactly the 13 findings above. Do NOT touch any other file.
- Do NOT silently expand scope. If you find a related issue, note it in the COMPLETENESS checklist.
- Do NOT touch Phase 0+1 work (CRIT-1/2, HIGH-1/3/5/6, A-1) — those are already done.
- Do NOT touch Phase 3 (Architectural) work.
- Do NOT add new dependencies unless absolutely necessary (likely not needed).
- Do NOT add MED-3 strict mode by default — opt-in only per Q3.

## Verification commands to run (in order)

After all 13 edits, run a comprehensive test suite. The test count will grow significantly (~150-200 new tests across 13 findings). Key verifications:

**1. MED-1 — Per-instance callback (per-runtime state):**
```bash
cd /path/to/projects/crabcakes && python3 -c "
import sys; sys.path.insert(0, '.')
# Two concurrent AgentRuntime instances with different callbacks don't interfere
# (Test in tests/test_runtime_callbacks.py — verify it exists and passes)
"
```

**2. MED-2 — `_BLOCKLIST` docstring updated:**
```bash
cd /path/to/projects/crabcakes && grep -A 5 "_BLOCKLIST" agent/tools.py | head -10
```
Expect: comment doesn't say "safety tier"

**3. MED-3 — web_fetch opt-in (default off):**
```bash
cd /path/to/projects/crabcakes && python3 -c "
import os
# Default behavior: web_fetch not restricted
os.environ.pop('CRABCAKES_WEB_FETCH_RESTRICT', None)
from agent.tools import _is_web_fetch_restricted
assert _is_web_fetch_restricted() is False, 'MED-3 default should be off'
# Opt-in: web_fetch restricted
os.environ['CRABCAKES_WEB_FETCH_RESTRICT'] = '1'
assert _is_web_fetch_restricted() is True, 'MED-3 opt-in should work'
print('MED-3 opt-in: PASS')
"
```

**4. MED-4 — `/reject` scope:**
```bash
cd /path/to/projects/crabcakes && grep -n "last_check_files" ui/handlers/review_handler.py
```
Expect: ≥ 1 match

**5. MED-5 — https-only base_url:**
```bash
cd /path/to/projects/crabcakes && python3 -c "
import sys; sys.path.insert(0, '.')
from utils.provider_url import validate_provider_url
validate_provider_url('https://api.openai.com')  # should pass
try:
    validate_provider_url('http://api.openai.com')  # should raise
    print('FAIL')
except ValueError:
    print('MED-5 https-only: PASS')
validate_provider_url('http://localhost:11434')  # should pass (loopback)
"
```

**6. MED-6 — File permission check:**
```bash
cd /path/to/projects/crabcakes && grep -rn "st_mode & 0o077\|st.st_uid != os.getuid" utils/mcp_config.py utils/improve.py gateway/client.py
```
Expect: ≥ 3 matches (one per file)

**7. MED-7 — feedback_processor sanitization:**
```bash
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_feedback_processor.py -q 2>&1 | tail -3
```

**8. MED-8 — Atomic+0600 for save_provider:**
```bash
cd /path/to/projects/crabcakes && grep -B 1 -A 5 "def save_provider" utils/agent_defs.py | head -15
```
Expect: write to `.tmp`, `os.rename`, `os.chmod(path, 0o600)`

**9. MED-9 — escape_for_pango in set_markup:**
```bash
cd /path/to/projects/crabcakes && grep -B 1 -A 2 "set_markup" ui/handlers/chat_render_handler.py | head -20
```
Expect: interpolated values wrapped in `escape_for_pango()` or `GLib.markup_escape_text()`

**10. MED-10 — ReDoS fix:**
```bash
cd /path/to/projects/crabcakes && python3 -c "
import sys; sys.path.insert(0, '.')
from utils.markdown import format_markdown
# Adversarial input: 1000 stars should not hang
import time
text = '*' * 1000
start = time.monotonic()
result = format_markdown(text)
elapsed = time.monotonic() - start
assert elapsed < 0.5, f'MED-10 ReDoS: {elapsed:.2f}s for 1000 stars'
print(f'MED-10 ReDoS fix: PASS ({elapsed:.3f}s)')
"
```

**11. MED-11 — commit_sha validation:**
```bash
cd /path/to/projects/crabcakes && python3 -c "
import sys; sys.path.insert(0, '.')
from utils.git_ops import _validate_sha
import re
assert _validate_sha('HEAD') is True
assert _validate_sha('abc1234') is True
assert _validate_sha('--force') is False
assert _validate_sha('-f') is False
print('MED-11 sha validation: PASS')
"
```

**12. MED-12 — MCP env allowlist:**
```bash
cd /path/to/projects/crabcakes && grep -B 2 -A 10 "def _substitute_env" utils/mcp_config.py
```
Expect: env var allowlist check

**13. MED-13 — streaming usage:**
```bash
cd /path/to/projects/crabcakes && grep -n "stream_options\|include_usage" agent/runtime.py
```
Expect: ≥ 1 match

**14. New tests pass:**
```bash
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_*.py -q 2>&1 | tail -5
```

**15. Full test suite (sanity):**
```bash
cd /path/to/projects/crabcakes && python3 -m pytest -x -q 2>&1 | tail -3
```
Expect: ≥ 194 + new Phase 2 tests

**16. No accidental scope creep:**
```bash
cd /path/to/projects/crabcakes && git diff HEAD --stat
```

---

## Report

When done, send back a completion report with:
- Files changed with actual line numbers
- Output of all 16 verification commands
- Full pytest output
- COMPLETENESS checklist (per steelFramedCodeWriter Step 6.5) — list each of 13 findings with `[x]`
- Any related issues found (flagged, not silently fixed)

**Required word marker for /ask acknowledgment: "please write"**

**Do not skip the COMPLETENESS checklist.** Include every finding with `[x]` or `[NOT DONE] WHY`.

**LESSON REINFORCED (2 prior bug-fix cycles):** This is the largest phase by count. QTR's deviation instinct is to add "what I think is needed" rather than follow the spec. **For each of the 13 findings, the spec's edit description is authoritative. If you find yourself adding extra patterns, extra files, or extra tests beyond the spec, STOP and flag it.** Re-read each edit description before writing its code.

**TEST COVERAGE NOTE:** Phase 0 had a test coverage gap on `_untrusted_fence` (no direct test, just runtime exercise). For Phase 2, write DIRECT tests for each of the 13 findings. Especially for:
- MED-1 (per-instance callback — needs concurrency test)
- MED-4 (scope /reject — needs file-list test)
- MED-7 (feedback sanitization — needs adversarial input test)
- MED-10 (ReDoS — needs long-input timing test)
- MED-11 (sha validation — needs injection attempt test)

**DEFERRED (NOT IN SCOPE):** Per Q3 decision, MED-3 (web_fetch host allowlist) ships opt-in. Do NOT enable by default. Do NOT add a UI toggle — env var is sufficient.
