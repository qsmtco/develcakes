# Investigation: Debugger agent turn failure — transient DNS resolution failure

**Date:** 2026-09-12
**Investigator:** Supervisor (read-only investigation, per PM request)
**Affected:** `special:debugger` turn (model=minimax/Minimax-M3, msg_count=69)
**App version:** post-UIRESP2 P1–3 + P4/5 worktree (`bf0fe54` local)
**Severity:** LOW (transient, self-recovered, designed failure path worked) — but exposes a retry-coverage gap
**Status:** DNS healthy at time of writing; turn resumable; app requires no restart

---

## 1. Symptom

PM reported: "debugger just crashed" with terminal output showing:

```
model=minimax/Minimax-M3 msg_count=69
agent.runtime ERROR Error in tool loop for special:debugger
...
socket.gaierror: [Errno -3] Temporary failure in name resolution
...
ui.handlers.agent_runtime_handler DEBUG [handler] _do_error: sk=special:debugger
  msg=<urlopen error [Errno -3] Temporary failure in name resolution>
```

## 2. Failure chain (from traceback, verified against source)

1. Debugger turn reached `_call_llm_streaming` → `stream_with_ssl_retry`
   (`agent/llm/streaming.py:392`)
2. → `minimax_provider.py:170 stream()` → `urlopen_with_ssl_retry`
   (`agent/llm/streaming.py:302`)
3. → `urllib.request.urlopen` → `socket.getaddrinfo` raised
   **`gaierror [Errno -3]`** — `EAI_AGAIN`, the canonical *transient* DNS failure
4. urllib wrapped it in `URLError`; `urlopen_with_ssl_retry` did **not** retry it
   (DNS errors are not in its retryable set — see §4)
5. Exception propagated as the stream's first event → `_run_loop`'s handler
   (`agent/runtime.py:1433` region) caught it, logged
   `Error in tool loop`, and routed to `AgentRuntimeHandler._do_error`

## 3. Verdict: not an app crash

- The turn was marked **FAILED** and the error surfaced to the UI — this is the
  **designed** error path (`_do_error`), not a hang or a process crash
- No data loss; the 69-message conversation persists on disk and is resumable
  by simply re-sending the message
- **No restart needed.** Nothing in the app is wedged

## 4. Root cause + the real finding: retry layer doesn't cover DNS

### Environment at time of writing (all verified live)

| Check | Result |
|---|---|
| `api.minimax.io` resolves | OK (23.212.62.91) |
| `api.minimax.chat` resolves | OK (47.79.2.234) |
| `api.anthropic.com` resolves | OK |
| Raw IP connectivity (1.1.1.1) | OK, 14.6ms |
| `/etc/resolv.conf` | `nameserver 127.0.0.53` (systemd-resolved), search domain `tail209328.ts.net` (Tailscale MagicDNS) |

The failure was a **momentary systemd-resolved / MagicDNS hiccup** — classic
`EAI_AGAIN`, gone by the time of investigation. Exact trigger unknowable
post-hoc; Tailscale DNS re-probing is the most likely suspect on this box.

### Gap 1 — DNS errors are not retryable in `urlopen_with_ssl_retry`

`agent/llm/streaming.py` retryable set:

- `ssl.SSLError` variants (`RETRYABLE_SSL_ERRORS`)
- `RETRYABLE_OSERROR_TYPES` = `(ConnectionResetError, BrokenPipeError, TimeoutError)`
- `URLError` — but only when its unwrapped reason matches the above

**`socket.gaierror` is not covered.** A 2-second DNS blip therefore kills an
entire agent turn (here: an in-flight audit delegation) instead of being
absorbed by the backoff machinery that already exists
(`MAX_SSL_RETRIES = 3`, 500ms × 2^attempt exponential backoff — unused for DNS).

### Gap 2 — fallback-provider path doesn't fire on connection errors

`agent/runtime.py:1621`: the `fallback_provider` retry triggers **only** on
`KB_OUT_OF_SCOPE` content — not on transport exceptions. (Also not configured
in this deployment's `providers.yaml`; its 4 tests are among the 41 documented
baseline failures.)

## 5. Recommended fix (small, surgical, for a future unit)

Extend `urlopen_with_ssl_retry` to treat DNS-transient as retryable:

- Add `socket.gaierror` to the retryable exception types
- In the `URLError` branch, unwrap the reason: retry when it is a `gaierror`
  with errno `-2` (EAI_NONAME can be permanent; `-3` EAI_AGAIN is transient —
  safest: retry any `gaierror`, budget-capped at the existing 3 attempts)
- Ride the existing exponential backoff unchanged
- Tests: red-first — mock `urlopen` raising `URLError(gaierror(-3, ...))`
  twice then succeeding → assert success + 3 urlopen calls; and a
  permanent-failure case (e.g. errno -2) to prove the budget/cap behavior

Hardens **every** provider, not just minimax. ~5 lines + 2 tests.

**Not** a substitute for: fixing the 4 baseline `test_runtime_fallback`
failures so the provider-fallback chain actually works (separate unit).

## 6. Collateral impact on project work

The P4/5 audit delegation was in flight to Debugger when the turn died —
**the audit never started**. No work lost; delegation reissued to a fresh
Debugger session (same contract, `bf0fe54`).

## 7. Actions taken

1. This report written (read-only investigation; no code changed)
2. Audit delegation reissued to Debugger
3. DNS-retry fix logged as follow-up candidate (§5) — awaiting PM go
