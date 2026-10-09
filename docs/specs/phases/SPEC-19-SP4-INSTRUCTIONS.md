# SPEC-19 SP4 — Phase Instructions: Two-phase action bridge + agent prompt updates

**Spec:** `docs/specs/SPEC-19-LIVE-CHAT-SURFACE.md` §5 SP4 (two-phase, F6) + §8 (F2 prompts).
**Depends on:** SP1–SP3 (done, audited). Final phase — the live tier becomes *usable by the agents*.
**Files in scope:** `ui/views/chat_surface.py` (bridge eval + event dispatch), NEW
`utils/live_bridge.py` (method registry + approval routing), `ui/handlers/chat_render_handler.py`
(bridge wiring seam only), `prompts/system/coder.md`, `prompts/system/debugger.md`,
`prompts/system/supervisor.md` (the F2 protocol blocks), tests (NEW
`tests/test_live_bridge.py` + extensions).
**Word marker:** please write.

---

## 0. Baseline (record verbatim)

```bash
xvfb-run -a .venv/bin/python -m pytest tests/test_live_guard.py -q
.venv/bin/python -m pytest tests/test_sanitize.py -q
.venv/bin/python -m ruff check ui/views/chat_surface.py prompts 2>/dev/null || true
```

## 1. Part A — the two-phase bridge (spec §5 SP4, F6)

### A1. `utils/live_bridge.py` (pure, testable without GTK)

- `CONSEQUENTIAL_METHODS: frozenset[str]` — methods that REQUIRE approval:
  `{"exec_command", "write_file", "edit_file", "approve_exec"}`. (Read-only methods
  are non-consequential: the surface already has them via the app; the bridge only
  needs the consequential class routed.)
- `class BridgeResult` (or plain dict): `{"status": "pending"|"ok"|"error"|"refused",
  "id": str, "data"?: Any}`.
- `class LiveBridge`:
  - `__init__(self, approver)` — `approver: Callable[[str, dict, str], None]` =
    (method, params, call_id) → routes to the EXISTING approval machinery (the
    exec-approval card via ARH/feed handler; window wires this — the bridge itself
    NEVER imports ARH/feed; setter-injected callback, house pattern).
  - `dispatch(method, params) -> dict` — the ONLY entry:
    * unknown method → `{"status":"error","id":..., "data":{"reason":"unknown method"}}`
    * non-consequential (none exist yet — the registry starts consequential-only;
      leave the branch for future read-only adds) → immediate ok
    * CONSEQUENTIAL → ALWAYS `{"status":"pending","id":call_id}` + hand to
      `self._approver(method, params, call_id)`. **The bridge NEVER executes
      anything itself.** The approver decides; resolution arrives later (A3).
  - `resolve(call_id, ok: bool, data=None)` — called by the resolution path
    (approval card handler / window) when the human approves/denies. Records the
    outcome for dispatch to the DOM (A3). Refuses unknown call_ids silently.
  - Calls are BOUNDED: a dict registry capped at 50 pending (FIFO, oldest dropped
    as failed) — no unbounded state from page-driven calls.
- The approver callback receives everything it needs to raise the EXISTING approval
  card (method/params/call_id). Window wires `approver` to the ARH path the same way
  `set_feed_handler` etc. are wired — the bridge stays module-isolated.

### A2. The page-side API (chat_surface, injection-world eval)

- Before the FIRST resurrection (same one-time init as the timer shims), install:
  ```js
  window.develcakes = {
    _pending: {},
    call: function (method, params) {
      var id = 'dc' + (++window.develcakes._seq);
      var evName = 'develcakes:result:' + id;
      var p = new Promise(function (resolve) { window.develcakes._pending[id] = resolve; });
      // native side picks this up (A2b) and resolves via a CustomEvent
      window.__dcBridgeQueue = window.__dcBridgeQueue || [];
      window.__dcBridgeQueue.push({method: method, params: params, id: id});
      return p;
    },
    _seq: 0
  };
  ```
- A2b. **Python polls the queue** after every injection/resurrection eval AND on a
  bounded idle timer (e.g. every 500ms while a live section exists — cheap:
  `evaluate_javascript('window.__dcBridgeQueue.length||0')`): if > 0, drain
  (`splice`), `LiveBridge.dispatch` each, keep the call_id.
  - NO `register_script_message_handler` (that requires a handler in the content
    manager — permissible, but polling is simpler and needs no new WebKit surface;
    NOTE in the module docstring why polling was chosen).
- A3. **Resolution → DOM event:** on `resolve()`, the surface evals:
  ```js
  (function(){ var r = window.develcakes._pending["ID"];
    if (r) { delete window.develcakes._pending["ID];
             document.dispatchEvent(new CustomEvent("develcakes:result", {detail:{id:"ID", ...DATA}})); } })();
  ```
  The PAGE listens: `document.addEventListener('develcakes:result', e => ...)`.
  The Promise resolves on the event (wrap: resolve inside the listener, then
  removeEventListener). Two-phase per spec — no timeout; approvals take minutes.

### A3. Approval routing (the spec's rule: the page can never approve itself)

- `approve_exec`-style consequential methods go through `approver` → the window
  raises the SAME feed approval card the toolbar/ARH path uses. On card resolution:
  `bridge.resolve(call_id, ok)` → A3 event.
- G6 gate test: a bridge call WITHOUT the card → stays `pending` forever (never
  auto-executes); WITH the card + Approve → resolves ok; Deny → resolves refused.
  The bridge code path has NO branch that executes a method — prove it by reading +
  a test that dispatch("exec_command", ...) returns pending and touches nothing.

## 2. Part B — F2: the agent prompts (spec §8)

Add a "## Live sections (SPEC-19)" block to `prompts/system/coder.md`,
`prompts/system/debugger.md`, `prompts/system/supervisor.md` — mirroring the SPEC-13
HTML-protocol blocks' structure. Content (keep it tight, ~20 lines each):

- When: interactive output the PM can click/toggle/watch (a calculator, a live chart,
  a checklist) → ONE whole-message ` ```live ` fenced block.
- Rules: self-contained HTML+CSS+inline JS; **inline `<script>` only — NEVER
  `<script src>`** (stripped); wrap script bodies in an IIFE; **NO external
  resources** (all network from the section is blocked by design); timers are
  section-scoped (flatten stops them); keep it under ~100 lines; static cards stay
  ` ```html ` (SPEC-13) — `live` is for interactivity only.
- The bridge: `await window.develcakes.call(method, params)` returns a Promise;
  resolves ONLY after human approval for consequential methods (exec/write/edit);
  never poll for approval — listen for the `develcakes:result` event or await.
- Non-interactive rich cards remain ` ```html ` — do not put prose in `live`.

## 3. Tests (RED-first; NEW tests/test_live_bridge.py + test_live_guard extensions)

| Test | Assert |
|---|---|
| unknown method | dispatch → error, no approver call |
| consequential dispatch | returns pending + approver called with (method, params, id) — and NOTHING executed (spy: no side effects) |
| resolve approve | bridge.resolve(id, True) → surface eval fires the CustomEvent; page Promise resolves (real-WebKit: a live section awaits call() and sets a flag) |
| resolve deny | resolves refused |
| G6 no self-approval | with NO approver wired (None), dispatch → pending, no execution, no crash — stays pending forever (bounded registry) |
| pending cap | 55 calls → oldest 5 dropped as failed |
| resolution of unknown id | silently ignored |
| e2e approval | real-WebKit: live section calls exec_command → pending; simulate the card Approve (call the window's resolution path) → flag set |
| prompts present | source-shape guard: all three prompt files contain "SPEC-19" + "```live" + "develcakes.call" |

## 4. Verification (paste ALL outputs)

```bash
xvfb-run -a .venv/bin/python -m pytest tests/test_live_bridge.py -q
xvfb-run -a .venv/bin/python -m pytest tests/test_live_guard.py -q
.venv/bin/python -m pytest tests/test_sanitize.py tests/test_chat_surface.py -q
.venv/bin/python -m ruff check utils/live_bridge.py ui/views/chat_surface.py prompts
.venv/bin/python -m pyright utils/live_bridge.py ui/views/chat_surface.py 2>&1 | tail -3
```

## 5. Report format (mandatory)

Baseline vs after; all outputs verbatim; per-test RED proofs; the approver wiring
diagram (bridge → window → ARH card) as implemented; prompt diffs (the blocks).
Related-bug scan. Do NOT git add/commit/push.

## 6. Carry-over lessons (distilled — your context was cleared)
- WebKit 6.0: `evaluate_javascript` (NOT run_javascript); does not await Promises.
- The one-time init JS goes with the timer shims (`_LIVE_SHIM_JS` shape).
- Poll pattern for the queue: bounded (500ms, only while live sections exist).
- House style: setter-injection, no cross-module imports in utils/, BLE001 guards.
