# SPEC-19 SP1 — Phase Instructions: Enforcement core + incremental injection

**Spec:** `docs/specs/SPEC-19-LIVE-CHAT-SURFACE.md` — read it IN FULL first (the amended
version, post-audit). The spec is the contract; this file is the phase plan.
**Files in scope:** `ui/views/chat_surface.py` (primary), NEW `utils/live_guard.py`
(enforcement module), `tests/test_live_guard.py` (NEW), `tests/test_chat_surface.py`
(extend). Do NOT touch SPEC-17's chat_surface code paths beyond what this phase names.
**Word marker:** please write.

---

## 0. Baseline (record verbatim)

```bash
xvfb-run -a .venv/bin/python -m pytest tests/test_chat_surface.py -q
.venv/bin/python -m ruff check ui/views/chat_surface.py
```

## 1. Verified API facts (probed by Supervisor 2026-10-08 — do not re-derive)

- `WebKit.UserContentFilterStore.new(storage_path)` — store at a cache dir (e.g.
  `~/.cache/develcakes/content-filters/`; create it 0700).
- `store.save(identifier, GLib.Bytes(json_rules), callback)` → `save_finish(result)` →
  `WebKit.UserContentFilter`. The JSON ruleset is the WebKit content-blocker format:
  `[{"trigger": {"url-filter": ".*", "resource-type": ["..."]}, "action": {"type": "block"}}]`.
- `WebKit.UserContentManager.add_filter(filter)` — attach to the view's content manager.
  The view's manager: `webview.get_user_content_manager()`.
- Content-blocker `resource-type` values include `document`, `script`, `image`, `style-sheet`,
  `raw` (= "untyped loads like XHR"). WebSocket/EventSource/sendBeacon have NO named type —
  a blanket `url-filter: ".*"` must cover them; that is precisely what G1 proves.
- Nav policy: `webview.connect("decide-policy", handler)` — `decision` types
  `WebKit.PolicyDecision` → check `WebKit.NavigationPolicyDecision` + `navigation_action`;
  call `decision.download()`/`decision.use()`/`decision.ignore()`.
- JS execution from the app: `webview.run_javascript(js, cancellable, callback)` →
  `run_javascript_finish` (result `get_js_value()`).

## 2. Part A — `utils/live_guard.py` (pure enforcement module)

A small module, no GTK widget imports, WebKit via lazy `gi` import (testable with fakes):

1. `BLOCK_ALL_REMOTE_RULESET: list[dict]` — the JSON ruleset constant:
   `[{"trigger": {"url-filter": ".*", "resource-type": ["raw", "script", "image",
   "style-sheet", "font", "media", "websocket"?, ...]}, ...]` — NOTE: `websocket` is NOT
   a documented resource-type; the blanket is the mechanism. Emit ONE rule:
   `{"trigger": {"url-filter": ".*"}, "action": {"type": "block"}}` PLUS a second rule
   EXEMPTING the local document: `{"trigger": {"url-filter": "^about:blank$",
   "resource-type": ["document"]}, "action": {"type": "ignore-previous-rules"}}`? —
   NO. `ignore-previous-rules` order is fragile. **Simpler, decided:** the blanket rule
   EXCLUDES local schemes via the filter itself —
   `"url-filter": "^(?!about:)"` (negative lookahead is NOT supported — content
   blockers use RE2, no lookaround) → **so: blanket `.*` block + `about:` allow via
   `ignore-previous-rules` ORDERED AFTER (allow wins because the LAST matching rule
   applies... verify!)**. RESEARCH STEP (§4 P1): empirically determine the correct
   ruleset shape (blanket+exemption ordering, or per-resource-type rules) on this box.
   Pin the working shape in the module with a comment citing the probe.
2. `class LiveGuard:` — owns store+filter lifecycle:
   - `compile(callback)` → async save → returns the `UserContentFilter` (idempotent;
     reuses a saved filter by identifier via `store.load`).
   - `attach(view)` — `view.get_user_content_manager().add_filter(self._filter)` +
     connect `decide-policy`: `decision.ignore()` for every navigation EXCEPT
     app-initiated loads where `navigation_action.get_navigation_type()` is
     `WEBKIT_NAVIGATION_TYPE_OTHER` AND the URI is the surface's own `about:blank`
     (this is the load_html/injection allowance — E2 precise).
   - `detach(view)` — remove filter, disconnect handler (destroy hygiene).
3. Unit-testable with fakes (no real WebKit needed for the ruleset-shape logic).

## 3. Part B — chat_surface: JS-on + injection prototype (behind the kill-switch)

**Feature-flagged; default OFF in SP1** (`DEVELCAKES_LIVE_JS=1` to enable — the spec's
kill-switch inverted for the prototype phase; flips default-on at SP2 once proven):

1. When the flag is ON: `settings.set_enable_javascript(True)` on the transcript view
   (the `_ensure_webview` site, chat_surface.py:~378) + `LiveGuard` attach.
2. When the flag is ON: appends become **incremental injection** — `_do_render` calls
   `run_javascript` with an IIFE that appends the new row's HTML to `#transcript`
   (the document root div the surface already renders) instead of `load_html` of the
   full document. HTML for injection is escaped into a JS string literal safely
   (json.dumps the HTML string — never string-interpolate markup into JS).
   - The INITIAL document load still uses `load_html` once (first render / after
     compaction rebuild).
   - Compaction (windowed eviction) still rebuilds via `load_html` — unchanged path.
3. When the flag is OFF: everything exactly as today (the default; no behavior change
   for anyone not opted in — SP1 ships dark).
4. Scroll: the SPEC-17 follow/settle logic listens to the vadjustment; injection
   changes height WITHOUT a document reload — verify the existing handlers behave
   (at-bottom follow should still work: `changed` fires when upper grows). Add ONE
   test: inject 3 rows at bottom → follow held; inject while scrolled up → position
   preserved. If follow BREAKS in a way the existing handlers can't absorb, STOP and
   report (spec §1a HALT discipline) — do not fork SPEC-17's model.

## 4. P1 — the probe harness (THE load-bearing deliverable)

A real-WebKit xvfb test file `tests/test_live_guard.py` (module-skipif WebKit absent):

- **G1 matrix (8 rows)** — load a document containing scripts that attempt each
  channel and report success/failure back via `console` capture or a polled DOM flag:
  1. `fetch("http://127.0.0.1:1/x")` → must FAIL (blocked)
  2. `XMLHttpRequest` to same → must FAIL
  3. `new WebSocket("ws://127.0.0.1:1/")` → must FAIL (connection never opens)
  4. `new EventSource(...)` → must FAIL
  5. `navigator.sendBeacon(...)` → must return false / not deliver
  6. `<img src="http://...">` subresource → must not load (onerror or blocked)
  7. `<script src="http://...">` → must not load
  8. **INVERSE:** the initial `about:blank` load + `run_javascript` injection → must
     SUCCEED (the surface itself works)
- Probe technique: `127.0.0.1:1` (discard port — nothing listens; a blocked-vs-timeout
  distinction is what we assert: with the filter, the promise rejects FAST with a
  network error vs hangs). A null HTTP server via `http.server` on a random port with
  the filter EXEMPTING nothing gives the positive control (unblocked → connects).
- **If ANY of rows 1-7 fails to block (or row 8 blocks): the unit HALTS.** Report the
  failing row verbatim; JS stays off; no fallback. (Spec §3 halt rule.)
- **P2 — real-render witness for chat_surface:** one test that does NOT monkeypatch
  `_load_html` — builds the real surface, appends a message, lets the real
  `load_html` run under xvfb, asserts the document contains the message text. This
  fixes the audit's "no real-render witness" blind spot.
- **P3 — injection state test:** flag ON, append msg A (contains a script setting
  `window.__t3 = 42`), append msg B, then read back `window.__t3` via
  `run_javascript("window.__t3")` → 42 — live state SURVIVES appends (the F1 payoff).

## 5. Verification (paste ALL outputs)

```bash
xvfb-run -a .venv/bin/python -m pytest tests/test_live_guard.py -q
xvfb-run -a .venv/bin/python -m pytest tests/test_chat_surface.py -q
.venv/bin/python -m ruff check ui/views/chat_surface.py utils/live_guard.py tests/test_live_guard.py
.venv/bin/python -m pyright utils/live_guard.py ui/views/chat_surface.py 2>&1 | tail -3
```

## 6. Report format (mandatory)

Baseline vs after counts, ALL outputs verbatim, the G1 matrix as a table (row →
blocked?/evidence), the chosen ruleset shape + why (cite the probe), scroll-test
outcome, RED-first proofs (each new test fails before its fix), related-bug scan.
HALT report format if any G1 row fails: the row, the observed behavior, the exact
ruleset tried — nothing else attempted past that point.
