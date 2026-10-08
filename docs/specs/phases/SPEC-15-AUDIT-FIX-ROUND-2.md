# SPEC-15 — Audit Fix Round 2 (Debugger re-audit regressions)

**Source:** Debugger's re-audit of commit `7915c669`. Two of the fixes introduced
regressions. Fix ALL items below. Files in scope:
`ui/handlers/telegram_bridge_handler.py` (primary), and tests.

Word marker: **please write**.

---

## MUST FIX (gate DONE)

### R1 — F1a regression: stale `on_disconnect` clobbers state  [bug, stale-callback-after-teardown]
`start_bridge` now calls `self.stop_bridge()` first. `stop_bridge()` →
`transport.disconnect()` → the transport fires `on_disconnect` → under production's
DEFERRED dispatch (`GLib.idle_add`, ui/window.py:461) that callback is QUEUED, not run
inline. `start_bridge` then synchronously sets CONNECTING (or ERROR on a failed
connect); the queued `_on_transport_disconnect` lands AFTERWARD and calls
`_set_state(DISCONNECTED)`, clobbering the correct state:

- failed reconnect (revoked token) → ends `disconnected` (should be `error`); the
  toolbar lies and no error card fires.
- healthy reconnect → transiently flips to `disconnected` before `connected`.

The tests miss it because `tests/test_telegram_bridge_handler.py` injects INLINE
dispatch (`lambda fn,*a: fn(*a)`), which runs `on_disconnect` synchronously DURING
`stop_bridge` — so the ordering bug is invisible.

**Fix (choose ONE, robust under deferred dispatch):** make transport callbacks carry a
session GENERATION so a superseded transport's callbacks are ignored. E.g. an
`int` generation counter incremented on every `start_bridge`/`stop_bridge`; capture it
in each transport lambda (`lambda r, g=gen: self._dispatch(self._on_transport_disconnect, r, g)`)
and have `_on_*` early-return when `g != self._generation`. Alternatively detach/suppress
the old transport's `on_disconnect` before calling `disconnect()` (e.g. a `self._stopping`
guard flag checked inside `_on_transport_disconnect`). Requirement: a callback from a
superseded transport must NEVER mutate the state of the current one. Do NOT rely on
`self._transport is not None` alone — after start_bridge creates the NEW transport,
that check would let the OLD transport's queued disconnect through.

**Test (REQUIRED):** add a DEFERRED-dispatch harness — a `dispatch` that QUEUES
callbacks into a list plus an explicit `drain()` — and assert:
(a) a failed reconnect ends in `ERROR` (not `disconnected`);
(b) a healthy reconnect never observes `DISCONNECTED` between `CONNECTING` and `CONNECTED`.
The inline-dispatch fakes cannot catch this; the new harness must.

### R2 — F5 regression: quadratic tag-strip on the main thread  [issue, regex-redos]
`_TAG_RE = re.compile(r"""<(?:[^>"']|"[^"]*"|'[^']*')*>""")` is ~22–32× slower than the
old `<[^>]+>` and ≈O(N²) on `<`-dense input (measured 16 K `<` → ~4 s; 32 K → ~15 s).
`_telegram_text` runs on the GTK MAIN thread (window._on_agent_response → on_supervisor_reply),
so a large angle-bracket-dense Supervisor reply freezes the UI.

**Fix:** replace the regex strip with a LINEAR approach. Preferred: stdlib
`html.parser.HTMLParser` to collect text nodes (linear, correctly handles attributes and
entities). If you keep a regex, it must be linear-cost and bounded. Add a scale guard
test (e.g. 16 K `<` finishes well under 100 ms).

### R3 — F5 regression: over-strip of legitimate decoded text  [issue, entity-decode-overstrip]
Strip-after-unescape eats real content that merely looks like a tag:
`<p>5 &lt; 6 &gt; 4</p>` → `"5  4"` (loses "6"); `git log &lt;branch&gt;` → `git log branch`.
An `HTMLParser`-based text extraction fixes this correctly (it unescapes entities in
text nodes, where `&lt;` becomes a literal `<` that is NOT re-interpreted as a tag).

**Test (REQUIRED):** assert the angle-bracket content SURVIVES:
- `<p>5 &lt; 6 &gt; 4</p>` → contains `5 < 6 > 4`
- `<p>use &lt;value&gt; here</p>` → contains `use <value> here`
- `<p>git log &lt;branch&gt; --oneline</p>` → contains `git log <branch> --oneline`
- keep the existing cases (attribute with `>`, entity-encoded `<script>` tag STRIPPED).

Correctness bar: a real tag is stripped; a decoded `&lt;...&gt;` in TEXT is preserved.

---

## FOLD IN (cheap)

### R4 — state-callback spam on a flapping link  [suggestion, state-callback-spam]
`_set_state` fires `on_state_change` unconditionally; window emits an "offline" card per
`error`. F1b makes a flapping link oscillate connected↔error → repeated "offline" cards.
Early-return in `_set_state` when `state == self._state` (dedup). Add a test: a no-op
re-set does not re-fire the callback.

---

## Verification (paste REAL output)

```bash
.venv/bin/python -m pytest tests/test_telegram_bridge_handler.py tests/test_telegram_store.py tests/test_telegram_transport.py -q
xvfb-run -a .venv/bin/python -m pytest tests/test_window_telegram_bridge.py tests/test_settings_telegram_section.py tests/test_toolbar.py -q
.venv/bin/python -m ruff check ui/handlers/telegram_bridge_handler.py tests/test_telegram_bridge_handler.py
.venv/bin/python -m pyright ui/handlers/telegram_bridge_handler.py 2>&1 | tail -3
```

RED-first for R1 (the deferred harness must fail before the fix). No new ruff/pyright.

## NOT in this round (Supervisor follow-ups — do NOT implement)
- Blocking Telegram sends on the GTK main thread.
- `ui/window.py:751` dead response path.