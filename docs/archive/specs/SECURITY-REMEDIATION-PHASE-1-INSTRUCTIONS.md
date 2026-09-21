# Phase 1 of 4 — Close the High Findings (HIGH-3, HIGH-6, A-1)

**Master spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-SECURITY-REMEDIATION.md` (1,211 lines)

**Phase 0 status:** ✅ SHIPPED (commit `b5dcccc` on `main`).
- CRIT-1, CRIT-2, HIGH-1, HIGH-5 all complete
- 1 bug-fix cycle: HIGH-1 wrong patterns + HIGH-5 wrong fix
- 143 targeted tests pass
- Working tree clean, pushed to `origin/main`

**Scope of this phase:** §2.3 (runtime.py — HIGH-3 api_key removal), §2.4 (markdown.py — HIGH-6 warn-but-render), §2.8 (gateway/client.py — A-1 lazy identity loading). **3 findings: HIGH-3, HIGH-6, A-1.**

---

## ⚠️ MANDATORY: Read every file in full before writing any code

Per `prompts/steelFramedCodeWriter.md` Rule 1, before writing ANY code, read EVERY file you will touch. ALL of it. Not snippets — the whole file. The spec's line numbers were accurate as of the audit (2026-06-10, HEAD `4fc79c1`); current HEAD is `b5dcccc` (Phase 0 changes may have shifted line numbers slightly). Anchor edits to identifiers, NOT line numbers.

**Files to read in full before writing any code:**

1. `agent/runtime.py` (1501+ LOC, the god object) — find:
   - `_conversations_dir` (line 771)
   - `_save_conversation_to_disk` (line 779) — has `api_key` in data dict at line 783
   - `_load_conversation_from_disk` (line 824)
   - The auto-save call sites (search for `self._auto_save(`)
   - The `AgentRuntime.__init__` method (for the one-time migration hook)

2. `models/conversation.py` — find the `Conversation` dataclass. The `api_key` field is at line 51. Look at how it's set and how it's re-resolved on load (the spec wants api_key re-resolved from `providers.yaml` keyed by `provider`/`model`).

3. `utils/markdown.py` (240 lines) — find:
   - `format_markdown` (line 50)
   - `_link_replace_and_protect` (line 191-200) — the markdown link replacer
   - `_auto_link` (line 209-216) — the auto-link path
   - `_restore_anchor` (line 226-235)
   - The `link_spans` and `anchor_spans` lists

4. `gateway/client.py` — find:
   - The `__init__` method (around line 184-185 where `_load_identity()` is called)
   - The `connect()` method (where identity loading should be deferred to)
   - `_load_identity()` itself (so you know what to defer)

5. `utils/providers_store.py` — find `get_providers()` and the `ProviderConfig` dataclass. This is where api_key is re-resolved from.

6. `tests/test_conversation.py` — read existing test patterns.
7. `tests/test_markdown.py` — read existing test patterns.
8. `tests/test_gateway.py` (may or may not exist; if not, create new file in `tests/`) — read existing test patterns if present.

**Output a discovery block before writing code** (per steelFramedCodeWriter Step 0):

```
DISCOVERY:
- Read agent/runtime.py: [what you learned — _save_conversation_to_disk at line 779, api_key in data dict at line 783, _load_conversation_from_disk at line 824, no os.chmod in _conversations_dir at line 771]
- Read models/conversation.py: [what you learned — Conversation dataclass with api_key field at line 51, how provider/model are stored]
- Read utils/markdown.py: [what you learned — _link_replace_and_protect at line 191, _auto_link at line 209, both produce <a href="..."> tags via urllib.parse.quote]
- Read gateway/client.py: [what you learned — __init__ at line ~180 calls _load_identity which raises if file missing, connect() is where deferred loading should happen]
- Read utils/providers_store.py: [what you learned — ProviderConfig dataclass, get_providers() returns list, look up by name to find api_key]
- Architecture owner: per ARCHITECTURE.md §3.x
- Existing patterns: [atomic+0600 in providers_store.save_providers, GLib.idle_add for thread→UI, dataclasses.replace for immutable updates]
```

---

## The bugs

**HIGH-3 — api_key persisted in plaintext conversation files.** When conversations are auto-saved, the `api_key` is written to `~/.config/crabcakes/conversations/{session_key}.json` with default umask (often 0644 = world-readable on multi-user systems). On a multi-user host, other local users can read the keys. The `providers.yaml` is carefully chmod 0600, but conversation files silently undo that hardening.

**HIGH-6 — Markdown link rendering allows arbitrary URI schemes.** `[click](file:///etc/passwd)` becomes a live `<a href>` in a `GtkLabel`. Default GTK behavior launches the system handler for any scheme on click, opening arbitrary local files, triggering SMB/FTP fetches, or invoking custom URI-scheme handlers. (User's design: render the link with a red ⚠ prefix, link still clickable, user decides.)

**A-1 — Gateway identity loaded eagerly at GatewayClient construction.** Importing `gateway.client` and constructing `GatewayClient()` raises if `~/.openclaw/identity/device-auth.json` is absent. Contradicts the "runs standalone, no account required" promise. Should be deferred to first `connect()` call.

---

## Edits

### Edit 1: `agent/runtime.py` — Remove `api_key` from conversation files (HIGH-3)

**a) Modify `_save_conversation_to_disk` at line 779** to NOT include `api_key`:

**Before (line 779-808 area, current behavior):**
```python
def _save_conversation_to_disk(conv: "Conversation", session_key: str) -> str:
    """Save a conversation to <conversations_dir>/<session_key>.json."""
    path = os.path.join(_conversations_dir(), f"{session_key}.json")
    data = {
        "session_key": session_key,
        "agent_name": conv.agent_name,
        "model": conv.model,
        "messages": ...,  # message history
        "api_key": conv.api_key,  # <-- HIGH-3: remove this line
        ...
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return path
```

**After (HIGH-3 spec):**
```python
def _save_conversation_to_disk(conv: "Conversation", session_key: str) -> str:
    """Save a conversation to <conversations_dir>/<session_key>.json.

    HIGH-3: api_key is NOT serialized. The api_key is re-resolved on load
    from providers.yaml (atomic+0600) keyed by conv.model/conv.provider.
    Conversation files should never contain raw secrets.
    """
    path = os.path.join(_conversations_dir(), f"{session_key}.json")
    data = {
        "session_key": session_key,
        "agent_name": conv.agent_name,
        "model": conv.model,
        "provider": conv.provider if hasattr(conv, "provider") else None,  # NEW
        "messages": ...,  # message history
        # NO "api_key" key — re-resolved on load
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    # HIGH-3: chmod 0600 after write
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # non-POSIX filesystem
    return path
```

**b) Modify `_conversations_dir` at line 771** to chmod 0o700:

**Before:**
```python
def _conversations_dir() -> str:
    """Return the conversations directory, creating it if needed."""
    from utils.config import get_config_dir
    d = os.path.join(get_config_dir(), "conversations")
    os.makedirs(d, exist_ok=True)
    return d
```

**After:**
```python
def _conversations_dir() -> str:
    """Return the conversations directory, creating it if needed.

    HIGH-3: parent dir is chmod 0o700 (owner only). Each conversation file
    is chmod 0o600 after write (in _save_conversation_to_disk).
    """
    from utils.config import get_config_dir
    d = os.path.join(get_config_dir(), "conversations")
    # Create with mode 0o700 if not present
    parent_existed = os.path.isdir(d)
    os.makedirs(d, exist_ok=True)
    if not parent_existed:
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return d
```

**c) Modify `_load_conversation_from_disk` at line 824** to re-resolve api_key:

**Before:**
```python
def _load_conversation_from_disk(session_key: str) -> tuple["Conversation", dict] | None:
    """Load a conversation from disk. Returns (Conversation, raw_data) or None."""
    path = os.path.join(_conversations_dir(), f"{session_key}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Build Conversation from data
        conv = Conversation(
            api_key=data.get("api_key"),  # <-- HIGH-3: don't use saved value
            ...
        )
        return conv, data
    except (OSError, json.JSONDecodeError):
        return None
```

**After (HIGH-3 spec):**
```python
def _load_conversation_from_disk(session_key: str) -> tuple["Conversation", dict] | None:
    """Load a conversation from disk. Returns (Conversation, raw_data) or None.

    HIGH-3: api_key is re-resolved from providers.yaml (atomic+0600) keyed
    by conv.model. Saved api_key in old files is ignored (and stripped
    on next save by the one-time migration).
    """
    path = os.path.join(_conversations_dir(), f"{session_key}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Re-resolve api_key from providers_store (NOT from saved data)
        api_key = _resolve_api_key_for_conversation(data)
        # Build Conversation from data
        conv = Conversation(
            api_key=api_key,
            ...
        )
        return conv, data
    except (OSError, json.JSONDecodeError):
        return None


def _resolve_api_key_for_conversation(data: dict) -> str | None:
    """Resolve the api_key for a loaded conversation from providers.yaml.

    HIGH-3: never read api_key from the saved file. Re-resolve from the
    provider store keyed by `provider` field (or extracted from `model`).
    Returns None if no matching provider configured.

    Args:
        data: The raw JSON data dict loaded from the conversation file.
              Expected to have a "model" key (e.g., "openai/gpt-4o") and
              optionally a "provider" key.
    """
    try:
        from utils.providers_store import get_providers
        providers = get_providers()
        if not providers:
            return None
        # Prefer explicit provider field
        provider_name = data.get("provider")
        if not provider_name:
            model = data.get("model", "")
            if "/" in model:
                provider_name = model.split("/")[0]
        if not provider_name:
            return None
        # Look up matching provider
        for p in providers:
            if p.name == provider_name:
                return p.api_key
        return None
    except Exception:
        logger.exception("[runtime] failed to resolve api_key for conversation")
        return None
```

**d) Add `_migrate_conversation_files()` for one-time cleanup of existing files:**

```python
# Module-level flag — migration runs once per process
_CONVERSATION_MIGRATION_DONE: bool = False


def _migrate_conversation_files() -> int:
    """One-time sweep: remove api_key from existing conversation files.

    HIGH-3: scans ~/.config/crabcakes/conversations/*.json, removes the
    "api_key" field if present, writes back atomically with chmod 0600.
    New saves never include api_key. Idempotent — safe to call multiple times.

    Returns the number of files migrated.
    """
    global _CONVERSATION_MIGRATION_DONE
    if _CONVERSATION_MIGRATION_DONE:
        return 0
    _CONVERSATION_MIGRATION_DONE = True

    d = _conversations_dir()
    count = 0
    try:
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            path = os.path.join(d, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if "api_key" in data:
                    del data["api_key"]
                    # Atomic write
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
                    os.replace(tmp, path)
                    # Ensure 0600
                    try:
                        os.chmod(path, 0o600)
                    except OSError:
                        pass
                    count += 1
            except (OSError, json.JSONDecodeError):
                # Skip unreadable files — don't crash the migration
                continue
    except OSError:
        pass
    if count > 0:
        logger.info(
            "[runtime] HIGH-3 migration: removed api_key from %d conversation file(s)",
            count,
        )
    return count
```

**e) Call the migration from `AgentRuntime.__init__`** (find the `__init__` method, add at the top):

```python
    def __init__(self, ...):
        # ... existing init code ...

        # HIGH-3: one-time migration on startup
        try:
            _migrate_conversation_files()
        except Exception:
            logger.exception("[runtime] conversation migration failed (non-fatal)")
```

### Edit 2: `utils/markdown.py` — Warn-but-render for non-allowlisted link schemes (HIGH-6)

**Per the user's revised design (2026-06-18):** Render the link normally, but prepend a red ⚠ prefix. Link is still clickable. User agency preserved.

**a) Add module-level constants** (after the existing `_AUTO_LINK_RE` at line 24):

```python
# HIGH-6: Schemes that are safe to render as clickable links without warning.
# All other schemes (file://, smb://, ftp://, ssh://, javascript:, data:,
# custom URI schemes) render with a red warning prefix but stay clickable.
# Per CptJAQx 2026-06-18 — preserves user agency.
_ALLOWED_LINK_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})

# HIGH-6: Warning prefix shown in front of non-allowlisted links.
# U+26A0 = WARNING SIGN, rendered in red bold.
_WARNING_PREFIX: str = '<span foreground="red" weight="bold">\u26a0</span> '


def _validate_link_url(url: str) -> bool:
    """Return True if `url`'s scheme is in _ALLOWED_LINK_SCHEMES (or is relative).

    HIGH-6: relative URLs (no scheme) are allowed without warning.
    """
    if not url:
        return False
    # Allow relative URLs (no scheme)
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', url):
        return True
    scheme = url.split(":", 1)[0].lower()
    return scheme in _ALLOWED_LINK_SCHEMES
```

**b) Modify the `_link_replace_and_protect` function at line 191-200** to prepend the warning for non-allowlisted schemes:

**Before (current code):**
```python
def _link_replace_and_protect(m):
    label = m.group(1)
    url = m.group(2)
    safe_url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=-_.~")
    # Produce <a> tag, then immediately protect it with a placeholder
    anchor_html = f'<a href="{safe_url}"><u>{label}</u></a>'
    anchor_spans.append(anchor_html)
    return f'\x00ANCHOR{len(anchor_spans) - 1}\x00'
```

**After (HIGH-6 spec):**
```python
def _link_replace_and_protect(m):
    label = m.group(1)
    url = m.group(2)
    safe_url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=-_.~")
    # Produce <a> tag, then immediately protect it with a placeholder
    anchor_html = f'<a href="{safe_url}"><u>{label}</u></a>'
    # HIGH-6: prepend red warning prefix for non-allowlisted schemes
    if not _validate_link_url(url):
        anchor_html = _WARNING_PREFIX + anchor_html
    anchor_spans.append(anchor_html)
    return f'\x00ANCHOR{len(anchor_spans) - 1}\x00'
```

**c) Modify the `_auto_link` function at line 209-216** the same way:

**Before:**
```python
def _auto_link(m):
    url = m.group(1)
    url = _strip_trailing_punct(url)
    safe_url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=-_.~")
    return f'<a href="{safe_url}"><u>{url}</u></a>'
```

**After:**
```python
def _auto_link(m):
    url = m.group(1)
    url = _strip_trailing_punct(url)
    safe_url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=-_.~")
    anchor_html = f'<a href="{safe_url}"><u>{url}</u></a>'
    # HIGH-6: prepend red warning prefix for non-allowlisted schemes
    if not _validate_link_url(url):
        anchor_html = _WARNING_PREFIX + anchor_html
    return anchor_html
```

### Edit 3: `gateway/client.py` — Lazy identity loading (A-1)

**a) Modify `__init__` to NOT call `_load_identity`** (find the init method around line 180-190):

**Before (current eager load):**
```python
def __init__(self, ...):
    # ... other init code ...
    self._load_identity()  # <-- A-1: raises if ~/.openclaw/identity/ missing
    # ... more init code ...
```

**After (A-1 spec):**
```python
def __init__(self, ...):
    # ... other init code ...
    # A-1: identity is loaded lazily on first connect(), not here.
    # Importing the module and constructing GatewayClient is now safe
    # even if ~/.openclaw/identity/device-auth.json is missing.
    self._identity_loaded: bool = False
    # ... more init code ...
```

**b) Modify `connect()` (find the method) to load identity on first call:**

**Before:**
```python
def connect(self) -> None:
    # ... existing connect code ...
    # No identity load here — it was done in __init__
```

**After (A-1 spec):**
```python
def connect(self) -> None:
    # A-1: lazy load identity on first connect
    if not self._identity_loaded:
        self._load_identity()
        self._identity_loaded = True
    # ... existing connect code ...
```

**c) Update the toolbar error state** (if any UI side expects an error from the constructor): the error now surfaces on first `connect()` call, not on construction. Per the audit's recommendation, surface as a toolbar error state — **verify by reading `window.py` and `gateway_handler.py` to see what error handling exists, and adjust if needed**.

> **Implementation note:** The exact signature of `_load_identity()` may differ. Read the current code to confirm. The key change is: don't call it in `__init__`, do call it (once) at the start of `connect()`.

---

## Rules

- Use the `prompts/steelFramedCodeWriter.md` prompt
- Read every file in full before editing
- Anchor edits to identifiers, NOT line numbers
- Scope is exactly the 3 edits above. Do NOT touch any other file.
- Do NOT silently expand scope. If you find a related issue, note it in the COMPLETENESS checklist.
- Do NOT touch CRIT-1, CRIT-2, HIGH-1, HIGH-5 (Phase 0) — those are already done.
- Do NOT touch Phase 2 (Mediums) or Phase 3 (Architectural) work.
- Do NOT add the `_strip_template_vars` re-introduction QTR suggested in the Phase 0 bug-fix report — track it for a separate spec if desired.
- Do NOT add `import re` if it's not needed in `agent/tools.py` (Phase 0 cleanup deferred to Phase 3 LOW).
- Do NOT change the spec's `[x]` for any Phase 0 finding. Phase 0 is done.

## Verification commands to run (in order)

**1. HIGH-3 — `api_key` removed from conversation files (Edit 1a):**

```bash
cd /path/to/projects/crabcakes && grep -n '"api_key"' agent/runtime.py
```
Expect: 0 matches in `_save_conversation_to_disk` (the line at 783 should be removed)

**2. HIGH-3 — `_conversations_dir` chmod 0o700 (Edit 1b):**

```bash
cd /path/to/projects/crabcakes && grep -B 1 -A 5 "def _conversations_dir" agent/runtime.py
```
Expect: `os.chmod(d, 0o700)` in the function body (when parent is newly created)

**3. HIGH-3 — `_load_conversation_from_disk` re-resolves api_key (Edit 1c):**

```bash
cd /path/to/projects/crabcakes && grep -B 1 -A 3 "_resolve_api_key_for_conversation" agent/runtime.py
```
Expect: function defined and called from `_load_conversation_from_disk`

**4. HIGH-3 — Migration function (Edit 1d):**

```bash
cd /path/to/projects/crabcakes && grep -n "_migrate_conversation_files\|_CONVERSATION_MIGRATION_DONE" agent/runtime.py
```
Expect: function defined + called from `AgentRuntime.__init__`

**5. HIGH-3 — End-to-end test:**

```bash
cd /path/to/projects/crabcakes && python3 -c "
import os, sys, json
sys.path.insert(0, '.')
# Simulate a save+load cycle with api_key
from agent.runtime import _save_conversation_to_disk, _load_conversation_from_disk
from models.conversation import Conversation
# Find a test session_key
import re
SESSION_KEY_RE = re.compile(r'^[A-Za-z0-9_:-]+$')
test_key = 'test:high-3-validation'
assert SESSION_KEY_RE.match(test_key), 'session_key must match regex'
# Use a real Conversation-like object (use mocks if needed)
# Just verify the function doesn't include api_key in the data
import inspect
src = inspect.getsource(_save_conversation_to_disk)
assert 'api_key' not in src or 're-resolve' in src.lower() or 'not include' in src.lower(), \
    '_save_conversation_to_disk should NOT serialize api_key'
print('HIGH-3 source check: PASS')
"
```

**6. HIGH-6 — `_WARNING_PREFIX` and `_ALLOWED_LINK_SCHEMES` (Edit 2a):**

```bash
cd /path/to/projects/crabcakes && grep -n "_ALLOWED_LINK_SCHEMES\|_WARNING_PREFIX\|_validate_link_url" utils/markdown.py
```
Expect: ≥ 3 matches (constants + helper function)

**7. HIGH-6 — Warn-but-render behavior (Edit 2b, 2c):**

```bash
cd /path/to/projects/crabcakes && python3 -c "
import sys; sys.path.insert(0, '.')
from utils.markdown import _validate_link_url
# Spec test cases
test_cases = [
    ('http://example.com', True, 'http allowed'),
    ('https://example.com', True, 'https allowed'),
    ('mailto:x@y.com', True, 'mailto allowed'),
    ('file:///etc/passwd', False, 'file not allowed'),
    ('smb://server/share', False, 'smb not allowed'),
    ('javascript:alert(1)', False, 'javascript not allowed'),
    ('data:text/html,<script>', False, 'data not allowed'),
    ('ssh://user@host', False, 'ssh not allowed'),
    ('', False, 'empty URL not allowed'),
    ('relative/path', True, 'relative URL allowed'),
    ('#anchor', True, 'anchor allowed'),
    ('/absolute/path', True, 'absolute path allowed'),
]
failures = 0
for url, expected, desc in test_cases:
    actual = _validate_link_url(url)
    status = '✓' if actual == expected else '✗'
    if actual != expected:
        failures += 1
    print(f'  {status} _validate_link_url({url!r}) = {actual} (expected {expected}) — {desc}')
print()
if failures == 0:
    print('HIGH-6 scheme validation: ALL 12 TEST CASES PASS')
else:
    print(f'HIGH-6 scheme validation: {failures} of 12 FAILED')
    sys.exit(1)
"
```

**8. HIGH-6 — Full format_markdown test (warning visible in output):**

```bash
cd /path/to/projects/crabcakes && python3 -c "
import sys; sys.path.insert(0, '.')
from utils.markdown import format_markdown
# Spec test cases — verify warning is in output for non-allowlisted, absent for allowlisted
test_cases = [
    ('See [example](http://example.com)', 'http://example.com', True, 'http link no warning'),
    ('See [example](https://example.com)', 'https://example.com', True, 'https link no warning'),
    ('See [example](mailto:x@y.com)', 'mailto:x@y.com', True, 'mailto no warning'),
    ('See [example](file:///etc/passwd)', 'file:///etc/passwd', False, 'file link WITH warning'),
    ('See [example](smb://server/share)', 'smb://server/share', False, 'smb link WITH warning'),
]
# Note: format_markdown expects already-escaped text per the docstring
# Use the helpers directly for clarity
from utils.escaping import escape_for_pango
for text, url, should_be_allowed, desc in test_cases:
    escaped = escape_for_pango(text)
    result = format_markdown(escaped)
    has_warning = '\u26a0' in result
    if should_be_allowed:
        status = '✓' if not has_warning else '✗'
    else:
        status = '✓' if has_warning else '✗'
    print(f'  {status} {desc}: warning={has_warning} (expected {\"NO\" if should_be_allowed else \"YES\"})')
    print(f'    Output: {result[:100]}...')
"
```

**9. A-1 — `_load_identity` deferred (Edit 3a, 3b):**

```bash
cd /path/to/projects/crabcakes && python3 -c "
import sys; sys.path.insert(0, '.')
# Construct GatewayClient without identity file present
# Should NOT raise on construction
from gateway.client import GatewayClient
try:
    # Use a minimal config; the goal is just to verify __init__ doesn't call _load_identity
    # If the constructor requires complex args, mock them
    import inspect
    src = inspect.getsource(GatewayClient.__init__)
    assert '_load_identity' not in src, '__init__ should NOT call _load_identity (A-1)'
    print('A-1 lazy load: __init__ does NOT call _load_identity — PASS')
except AssertionError as e:
    print(f'A-1 lazy load: FAIL — {e}')
    sys.exit(1)
except Exception as e:
    print(f'A-1 lazy load: test setup issue — {e}')
"
```

**10. A-1 — `_identity_loaded` flag present:**

```bash
cd /path/to/projects/crabcakes && grep -n "_identity_loaded" gateway/client.py
```
Expect: ≥ 2 matches (init sets it to False, connect() checks/sets it to True)

**11. New tests pass:**

```bash
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_conversation.py tests/test_markdown.py -v 2>&1 | tail -20
```
Expect: all existing tests + new HIGH-3 + HIGH-6 tests pass

**12. Targeted test run (no regressions):**

```bash
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_enforcement.py tests/test_tools.py tests/test_conversation.py tests/test_markdown.py tests/test_prompt_loader.py tests/test_project_awareness.py -q 2>&1 | tail -5
```
Expect: ≥ 143 (Phase 0 baseline) + new Phase 1 tests

**13. Full test suite (sanity):**

```bash
cd /path/to/projects/crabcakes && python3 -m pytest -x -q 2>&1 | tail -3
```
Expect: ≥ 1750 passed (Feed Card UX baseline) + 143 (Phase 0) + new Phase 1 tests

> **KNOWN ISSUE:** The full test suite OOMs at ~17% on long runs in some environments. Targeted test runs always pass. If the full suite OOMs, document it in the COMPLETENESS checklist and don't fail the phase.

**14. No accidental scope creep:**

```bash
cd /path/to/projects/crabcakes && git diff HEAD --stat
```
Expect: only these files changed:
- `agent/runtime.py` (Edit 1)
- `models/conversation.py` (if api_key handling is here, per Edit 1c)
- `utils/markdown.py` (Edit 2)
- `gateway/client.py` (Edit 3)
- `tests/test_conversation.py` (HIGH-3 tests)
- `tests/test_markdown.py` (HIGH-6 tests)
- `tests/test_gateway.py` (NEW, A-1 tests) OR add to existing test file

If `agent/enforcement.py` or `agent/tools.py` (Phase 0) is modified, that is scope creep — revert it.

**15. No `api_key` in conversation serialization:**

```bash
cd /path/to/projects/crabcakes && grep -n '"api_key"' agent/runtime.py
```
Expect: 0 matches in the data dict construction (the line at 783 should be gone)

---

## Report

When done, send back a completion report with:
- Files changed with actual line numbers
- Output of all 15 verification commands
- Full pytest output for `test_conversation.py` and `test_markdown.py`
- COMPLETENESS checklist (per steelFramedCodeWriter Step 6.5)
- Any related issues found (flagged, not silently fixed)

**Required word marker for /ask acknowledgment: "please write"** — include it in your response.

**Do not skip the COMPLETENESS checklist.** Include every edit with `[x]` or `[NOT DONE] WHY` and paste the evidence.

**LESSON FROM PHASE 0:** The Phase 0 work had 2 deviations from spec (HIGH-1 wrong patterns, HIGH-5 wrong fix) that required a bug-fix delegation. This phase has 3 findings that are more straightforward (HIGH-3 is data serialization, HIGH-6 is regex/formatting, A-1 is method call deferral). **Follow the spec exactly** — every code sample in this instructions file is verified against the source where possible. If you find yourself adding patterns or code that isn't in the spec, STOP and flag it.

**TEST COVERAGE NOTE:** Phase 0 had a test coverage gap on `_untrusted_fence` (no direct test, just runtime exercise). For Phase 1, write DIRECT tests for each new function:
- `test_save_conversation_does_not_include_api_key` (HIGH-3)
- `test_load_conversation_resolves_api_key_from_providers` (HIGH-3)
- `test_migrate_conversation_files_removes_api_key` (HIGH-3)
- `test_markdown_warn_but_render_for_non_allowlisted_scheme` (HIGH-6)
- `test_markdown_no_warning_for_allowlisted_scheme` (HIGH-6)
- `test_gateway_lazy_identity_loading` (A-1)
