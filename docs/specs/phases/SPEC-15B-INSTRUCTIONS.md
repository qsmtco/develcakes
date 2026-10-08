# SPEC-15b — Phase Instructions (single phase)

**Spec:** `docs/specs/SPEC-15b-TELEGRAM-HARDENING.md` — read it in full FIRST; the spec
is the contract, this file is the phase plan.
**Baseline (record verbatim):**
```bash
.venv/bin/python -m pytest tests/test_telegram_transport.py tests/test_telegram_bridge_handler.py -q
.venv/bin/python -m ruff check main.py transport/telegram.py
```

Word marker for this delegation: **please write**.

---

## Part A — token-log redaction (SECURITY)

Problem: `httpx`/`httpcore` log the request URL at INFO/DEBUG; the token rides in the
URL path (`/bot<token>/method`), so with `DEBUG` set every poll prints the raw token.
`redact_log_preview()` never sees those records (it only scrubs app-authored strings).

### A1. Floor the noisy libraries (`main.py`)
After the existing `logging.basicConfig(...)` block (`main.py:16-20`), add:
```python
for _noisy in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
```

### A2. Redacting filter (defense in depth)
Create `utils/log_redaction.py` — a small module exposing:
- `class RedactingFilter(logging.Filter)` whose `filter(record)` rewrites
  `record.msg` and every `str` in `record.args` through
  `transport.telegram.redact_log_preview`, then returns True (ALWAYS pass the record —
  a logging filter must never drop a record).
- Wrap the scrub in try/except: if redaction raises, pass the record unchanged and
  return True (never break logging).
- `def install() -> None` that attaches the filter to the `httpx`
  and `httpcore` loggers (call it from `main.py` right after A1).
Import `redact_log_preview` lazily INSIDE the filter's scrub (deferred import) if a
top-level import creates a cycle — `utils/` must not import `transport/` at module
import time if that pulls in `ui/`. Verify no cycle; if one appears, import inside
`filter()`.

### A3. Transport header truthfulness
`transport/telegram.py`'s module header claims the token "must NEVER appear in logs".
Make it true in fact — append a line noting the URL-path leak is covered by
`utils/log_redaction` (attached to httpx/httpcore by `main.py`).

### A4. Tests (RED-first)
- Capture-handler test: build a real `httpx`-logged record (or emit one via
  `logging.getLogger("httpx").info("POST %s", "/bot<TOKEN>/getUpdates")`) with the
  filter installed → assert the token does NOT appear in any emitted line (must FAIL
  before A2).
- Filter unit test: a record with the token in `record.msg` AND a separate record with
  it only in `record.args` → both scrubbed.
- Filter-raise safety: monkeypatch the scrub to raise → record still passes through.

---

## Part B — 409 Conflict surfacing (DIAGNOSABILITY)

### B1. Detect it in `_get_updates` (`transport/telegram.py:390`)
Add an internal `_Conflict(Exception)` mirroring `_RetryAfter`. In `_get_updates`,
before the generic non-ok branch:
- `resp.status_code == 409` → raise `_Conflict(...)`;
- an `ok:false` body with `error_code == 409` → raise `_Conflict(...)`.
Keep 429 / ≥500 behavior UNCHANGED.

### B2. Surface it in `_poll_loop`
Catch `_Conflict` alongside the other handlers:
- re-arm `announced = False` (same as other soft errors — link is down);
- fire `on_error` with a DISTINCT, actionable message, e.g.:
  `"getUpdates conflict (409) — another consumer is polling this bot; close other apps or a stale pairing poller"`;
- `self._sleep(retry_delay)` and grow the backoff (do NOT spin at full rate);
- keep the loop alive (never crash the thread).

### B3. Tests (RED-first)
- `_get_updates` raises `_Conflict` on HTTP 409 and on `ok:false` + `error_code==409`
  (must FAIL before B1).
- `_poll_loop` on `_Conflict` fires `on_error` with the conflict message, does not
  raise, and backs off (assert `_sleep` called with a growing delay).
- Regression: 429 → `_RetryAfter`; ≥500 → `None` (unchanged).

---

## Verification (paste ALL outputs)

```bash
.venv/bin/python -m pytest tests/test_telegram_transport.py tests/test_telegram_bridge_handler.py tests/test_telegram_store.py -q
.venv/bin/python -m ruff check main.py transport/telegram.py utils/log_redaction.py tests/test_telegram_transport.py
.venv/bin/python -m pyright main.py transport/telegram.py utils/log_redaction.py 2>&1 | tail -3
```

## Scope guard
- Do NOT touch the bridge handler, settings dialog, or pairing controller.
- Do NOT redesign pairing (the 409 message only NAMES the possibility).
- Reuse `redact_log_preview`; do not write a second redactor.
- Do NOT git add/commit/push (the Supervisor owns commits). Do NOT touch `scratch/`.
