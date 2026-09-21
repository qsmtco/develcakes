# Phase 2 Instructions — Gateway Client Hardening (LOW-3, LOW-4, LOW-5)

**Phase:** 2 of 5
**Findings (original review):** LOW-3, LOW-4, LOW-5
**Master spec:** `docs/specs/SPEC-LOW-FOLLOWUP-PHASE-4.md` §4.2, §4.3, §4.4
**Authority chain:** Captain → `docs/ARCHITECTURE.md` → spec → this file → code

---

## READ FIRST

1. **Read the master spec** — `docs/specs/SPEC-LOW-FOLLOWUP-PHASE-4.md` §4.2-4.4 in full
2. **Read these files in full** before editing:
   - `gateway/client.py` (especially lines 188, 223, 280-300, 440, 451-453, 468, 506, 525-526, 532)
   - `tests/test_gateway.py` (existing test patterns; especially `TestLazyIdentityLoading` and any other constructor tests)
3. **Use the steelFramedCodeWriter prompt** at `prompts/steelFramedCodeWriter.md`

## Edits to make (1 file)

### `gateway/client.py`

**LOW-3 — `operator.admin` scope as constructor parameter**

At the module level, keep `ALL_SCOPES` (used as default). Add a new constant:

```python
DEFAULT_SCOPES = ["operator.admin", "operator.approvals", "operator.pairing"]
```

In `GatewayClient.__init__` (around line 240), add a new parameter:

```python
def __init__(
    self,
    url: str,
    on_connect: Callable[[], None],
    on_error: Callable[[str], None],
    on_event: Callable[[str, dict[str, Any]], None],
    on_tick: Callable[[], None] | None = None,
    scopes: list[str] | None = None,  # LOW-3
) -> None:
    ...
    # LOW-3: scopes is a constructor parameter; default to all 3 if None
    self._scopes = list(scopes) if scopes is not None else list(DEFAULT_SCOPES)
    self._scopes_str = ",".join(self._scopes)
```

In the handshake (around line 468), replace `ALL_SCOPES.split(",")` with `self._scopes`. The "v3_payload" string at line 440 currently uses `ALL_SCOPES` — replace with `self._scopes_str` so the signed payload uses the same scope set the constructor was given.

**LOW-4 — Raw gateway log dump redaction**

At line 506, add a redaction pass before logging:

```python
# LOW-4: redact sensitive keys from the raw preview before logging
_redacted_raw = _redact_gateway_log_preview(raw[:300])
_logger.debug("[gateway>>] %s", _redacted_raw)
```

Add the helper near the top of the file (after `_validate_snapshot`):

```python
# LOW-4: sensitive keys to redact from gateway log previews
_GATEWAY_REDACT_KEYS = (
    "apiKey", "apikey", "api_key",
    "token", "deviceToken", "device_token",
    "password", "secret",
)


def _redact_gateway_log_preview(raw: str) -> str:
    """Replace sensitive values with *** in a gateway log preview.

    Matches both JSON-style and shell-style occurrences of known keys.
    Truncation is the caller's responsibility (raw[:N] is the input).
    """
    out = raw
    for key in _GATEWAY_REDACT_KEYS:
        # Match "key":"value" or "key": "value" or "key":value
        pattern = re.compile(
            rf'("{re.escape(key)}"\s*:\s*)("[^"]*"|[^\s,}}]+)',
            re.IGNORECASE,
        )
        out = pattern.sub(r'\1"***"', out)
    return out
```

Add `import re` at the top of the file (verify it isn't already imported; if so, skip).

At line 525-526, the malformed-JSON warning is:
```python
_logger.warning("Gateway sent malformed JSON: %r", raw[:200])
```
Change to truncate to 80 chars and not include the body:
```python
# LOW-4: don't leak raw body in malformed-JSON warning
_logger.warning(
    "Gateway sent malformed JSON (first 80 chars redacted): %s",
    _redact_gateway_log_preview(raw[:80]),
)
```

**LOW-5 — Unvalidated event payloads**

Add the validator helper near the top of the file (after `_redact_gateway_log_preview`):

```python
# LOW-5: known event names (used only for DEBUG logging — unknown events still
# pass through, but we log a warning when payload type is wrong)
_KNOWN_EVENT_NAMES = frozenset({
    "agent", "agent.start", "agent.end", "agent.thinking",
    "chat", "chat.final", "chat.delta",
    "message", "message.received",
    "tick",
    "approve.required", "approve.resolved",
})


def _validate_event(name: object, payload: object) -> bool:
    """LOW-5: return True if (name, payload) is a valid gateway event.

    Validation rules:
      - name must be a non-empty str
      - payload must be a dict (events are JSON objects)
    Unknown event names pass through (we don't break new events) but are
    logged at DEBUG. Malformed (wrong types) returns False and the event
    is dropped.
    """
    if not isinstance(name, str) or not name:
        _logger.warning("LOW-5: dropping event with non-string/empty name: %r", name)
        return False
    if not isinstance(payload, dict):
        _logger.warning(
            "LOW-5: dropping event %r with non-dict payload (type=%s)",
            name, type(payload).__name__,
        )
        return False
    if name not in _KNOWN_EVENT_NAMES:
        _logger.debug("LOW-5: passing through unknown event name: %r", name)
    return True
```

In the listen loop (around line 451-453), wrap the `GLib.idle_add(self.on_event, ...)` call:

```python
if msg.get("type") == "event":
    evt_name = msg.get("event", "")
    payload = msg.get("payload", {})
    # LOW-5: validate before dispatch
    if _validate_event(evt_name, payload):
        GLib.idle_add(self.on_event, evt_name, payload)
```

## Tests to add

Add to `tests/test_gateway.py` (or a new `tests/test_low345_gateway_hardening.py`):

**LOW-3 tests:**

1. `test_low3_constructor_default_scopes` — construct `GatewayClient(url, on_connect, on_error, on_event)` with no `scopes` arg, assert `client._scopes == DEFAULT_SCOPES`.
2. `test_low3_constructor_custom_scopes` — construct with `scopes=["operator.pairing"]`, assert `client._scopes == ["operator.pairing"]`.
3. `test_low3_handshake_uses_constructor_scopes` — assert the handshake payload (line 468) uses the constructor's scope list, not `ALL_SCOPES.split(",")`. (You may need to mock `_handshake` or just inspect the `_scopes` attribute used in the payload.)

**LOW-4 tests:**

4. `test_low4_redact_apikey` — call `_redact_gateway_log_preview('{"apiKey":"secret123","other":"x"}')` and assert `"secret123"` is not in the result, `"***"` is.
5. `test_low4_redact_token_case_insensitive` — call with `{"DeviceToken":"abc"}` and assert the value is redacted.
6. `test_low4_redact_truncation_respected` — call with a 1000-char input, assert output length ≤ 1000.
7. `test_low4_malformed_json_warning_truncated` — patch `_logger.warning` and call the listen loop with malformed JSON; assert the logged preview is ≤ 80 chars and contains `"***"`-style redaction if sensitive keys are present.

**LOW-5 tests:**

8. `test_low5_validate_event_string_name` — call `_validate_event("chat.final", {"x":1})`, assert True.
9. `test_low5_validate_event_empty_name` — call `_validate_event("", {"x":1})`, assert False.
10. `test_low5_validate_event_non_string_name` — call `_validate_event(123, {"x":1})`, assert False.
11. `test_low5_validate_event_non_dict_payload` — call `_validate_event("chat.final", "not-a-dict")`, assert False.
12. `test_low5_validate_event_unknown_name_passes` — call `_validate_event("new.event.v2", {"x":1})`, assert True (unknown names pass through).
13. `test_low5_listen_drops_malformed_event` — patch the listen loop with `{"type":"event","event":"","payload":{}}`; assert `on_event` is NOT called.

## Verification commands

```bash
# 1. Confirm ALL_SCOPES is no longer hardcoded into the handshake
git grep -nE "scopes.*ALL_SCOPES|ALL_SCOPES.split" gateway/client.py
# Expected: only the module-level definition and the _scopes_str default

# 2. Confirm redaction helper is used at log site
git grep -nE "redact_gateway_log_preview|_logger\.warning.*malformed" gateway/client.py

# 3. Confirm validator wraps the event dispatch
git grep -nE "_validate_event\(" gateway/client.py

# 4. Run tests
python -m pytest tests/test_gateway.py tests/test_low345_gateway_hardening.py -v 2>&1 | tail -50

# 5. Full suite
python -m pytest tests/ -x --ignore=tests/test_agent_runtime.py -q 2>&1 | tail -20
```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] Edit 1: gateway/client.py — added DEFAULT_SCOPES, _scopes constructor param, replaced handshake — evidence: <file:line>
- [x/not done] Edit 2: gateway/client.py — added _redact_gateway_log_preview + import re — evidence: <file:line>
- [x/not done] Edit 3: gateway/client.py — redaction at line 506, malformed-JSON warning truncated — evidence: <git grep>
- [x/not done] Edit 4: gateway/client.py — added _validate_event + _KNOWN_EVENT_NAMES — evidence: <file:line>
- [x/not done] Edit 5: gateway/client.py — wrapped on_event dispatch in _validate_event — evidence: <file:line>
- [x/not done] Tests 1-13: pytest output — evidence: <paste>
```

## Word marker

Include "please proceed" in your reply.

## Important reminders

- **Read every file before editing.** The handshake code at line 440-470 is subtle — the v3_payload string must use `self._scopes_str` (not `ALL_SCOPES`), and the JSON params block at line 468 must use `self._scopes` (a list).
- **Don't break backward compat.** `GatewayClient(...)` with no `scopes` arg must still request all 3 scopes (test #1 enforces this).
- **Flag related issues.** If you see other places that log raw gateway data, report them; do not silently fix.
