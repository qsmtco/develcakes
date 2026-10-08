# SPEC-19 SP2 — Phase Instructions: The ` ```live ` tier end-to-end

**Spec:** `docs/specs/SPEC-19-LIVE-CHAT-SURFACE.md` (amended) — the contract.
**Depends on:** SP1 (commit `de88ffe1`) — LiveGuard, injection prototype, probe harness.
**Files in scope:** `ui/views/chat_surface.py` (primary), `utils/live_guard.py` (only if
a shim/flatten helper belongs there), `render/html.py` (**fence detection ONLY** — read
below), `tests/test_live_guard.py` (extend), `tests/test_chat_surface.py` (extend).
Do NOT touch: the sanitizer policy (`render/sanitize.py`), SPEC-17 scroll logic, feed.
**Word marker:** please write.

---

## 0. Baseline (record verbatim)

```bash
xvfb-run -a .venv/bin/python -m pytest tests/test_live_guard.py tests/test_chat_surface.py -q
.venv/bin/python -m ruff check ui/views/chat_surface.py utils/live_guard.py
```
NOTE: if the pre-existing order-dependent SPEC-17 test (`test_two_consecutive_stable_frames_consumes`)
fails when the files run in this order, run them separately and note it — it is a REGISTERED
pre-existing flake (fails alone at clean HEAD too), NOT yours.

## 1. SP1 API facts (carry over — verified)

- `evaluate_javascript` (not run_javascript); does NOT await Promises → flag-and-poll.
- Injection path exists behind `DEVELCAKES_LIVE_JS=1` (SP1, default OFF): appends only
  new rows to `#transcript` via main-world eval, `json.dumps`-safed.
- LiveGuard: blanket filter + nav lock + detach-on-destroy (audit-fixed).
- `<script>` injected via `<template>.innerHTML` does NOT auto-execute (HTML5) — the
  SP1 probe pinned this; SP2 is where resurrection semantics become REAL.

## 2. The change

### 2.1 Fence detection (render/html.py — MINIMAL touch)

Add a whole-message ` ```live ` fence detector mirroring `_whole_message_html_fence`
(html.py:352): `live_fence(text) -> str | None` returning the payload. **Pure function,
no rendering, no sanitizer changes.** The chat surface calls it; anything that is not a
whole-message live fence renders exactly as today (T1/T2 paths untouched).

### 2.2 The live append path (chat_surface)

When a message IS a live fence AND the live flag is ON:

1. **Raw append** (T3 — bypasses nh3 entirely, spec §2): append a
   `<section class="live-section" data-live-id="N">` to `#transcript` via the injection
   path (json.dumps-safed HTML, same as SP1).
2. **E3 script resurrection (Phosphor-faithful):** after appending, run a resurrection
   script over the new section: find `script` nodes; NEVER resurrect one with `src`
   (remove it entirely — blocked anyway by E1, but remove so it can't appear in DOM);
   preserve `type`; IIFE-wrap the body (`(function(){\n...\n})();`) so sections can't
   collide; re-create the node so it EXECUTES. Resurrection runs scoped to the section
   (querySelectorAll inside the section, not document-wide).
3. **Timer shims (F9):** BEFORE the first live append, install per-surface shims once:
   ```js
   window.__dcTimers = {seq: 0, handles: {}};   // liveId -> {timeout: [], interval: [], raf: []}
   const _st = window.setTimeout, _si = window setInterval, _raf = window.requestAnimationFrame;
   window.setTimeout = (fn, ms, ...a) => { const id = _st(() => { delete ...; fn(...a) }, ms); record(id, 'timeout'); return id; };
   // same for setInterval / rAF; a section registers its live-id via a closure set
   // right before its scripts run (the resurrection wrapper sets window.__dcCurrentLive = N).
   ```
   Design constraint: handles must be attributable to a SECTION so flatten can clear
   exactly that section's timers without killing others. The resurrection wrapper sets
   `__dcCurrentLive = N` immediately before executing the section's scripts and clears
   it after — shims read it to attribute the handle.
4. **Cap 10:** when appending the 11th live section, FLATTEN the oldest (§2.3) first.
5. **Kill-switch default flips ON:** `DEVELCAKES_LIVE_JS` now defaults ENABLED;
   `=0` disables (renders live fences as T2 static via sanitize_agent_html — degrade,
   never error).

### 2.3 Flatten = full neutralization (F8+F9)

`flatten(liveId)`:
- Remove all `script` nodes in the section.
- Strip every `on*` attribute from every element in the section (one JS pass:
  `[...sec.querySelectorAll('*')].forEach(el => [...el.attributes].forEach(a => { if
  (/^on/i.test(a.name)) el.removeAttribute(a.name) }))`).
- Clear the section's recorded timers: its timeouts/intervals/rafs via the ORIGINAL
  (shim-captured) clear functions.
- Remove the section from the live registry (it becomes plain static DOM).
- Callers: cap overflow, windowed-DOM eviction compaction (the `load_html` rebuild must
  emit the FLATTENED html for evicted live sections — i.e. the compaction document
  builder strips scripts/on* for any section no longer in the live registry).

### 2.4 Streaming caveat (honest scope)

Agent messages may STREAM (deltas). A live fence is only recognizable COMPLETE. Rule:
the live path triggers only at message COMPLETION (the same point crabcards extract);
during streaming a live-fence message renders as plain code block, then REPLACES with
the live section on completion. Replacement = remove the streamed placeholder row,
append the live section. Pin this with a test.

## 3. Tests (RED-first; extend tests/test_live_guard.py + test_chat_surface.py)

Real-WebKit (xvfb) where behavior is DOM/JS:

| Test | Assert |
|---|---|
| `live` fence detection | pure fn: whole-message fence → payload; prose around it → None; ````html```` fence still T2 (not live) |
| Live append executes script | section script sets `window.__t3sp2` → readback 42 |
| E3: `<script src>` | NEVER resurrects — node REMOVED from DOM; a data: src too |
| E3: IIFE wrap | script defines `function collides(){}` → does NOT leak to window (`window.collides === undefined`) |
| E3: type preserved | `<script type="module">` keeps its type attribute on the recreated node |
| Timer attribution | section A sets an interval incrementing a flag; section B does too; flatten(A) stops ONLY A's (B keeps ticking — readback after N poll cycles) |
| Flatten neutralizes | section with `<button onclick=window.__boom=1>` + interval + script; flatten; click the button (synthetic) → no `__boom`; interval flag stops advancing |
| Cap 10 | append 12 live sections → exactly 10 in registry; oldest 2 flattened (no scripts in DOM) |
| Eviction compaction flattens | force the windowed-eviction rebuild with live sections present → rebuilt document contains their text but no script nodes |
| Kill-switch OFF | flag=0 → live fence renders via sanitize_agent_html (static, script stripped by T2 policy) — no error |
| Default ON | no env set → live path active (the SP2 default flip) |
| Streaming replacement | streaming placeholder (code block) replaced by live section at completion |
| F10 follow | live section grows height (interval appending <br>) → follow-scroll does not thrash (assert final at-bottom after growth settles; and scrolled-up case preserves position) |
| T1/T2 regression | existing suites green — especially sanitize guard tests + `render_message` behavior |

## 4. Verification (paste ALL outputs)

```bash
xvfb-run -a .venv/bin/python -m pytest tests/test_live_guard.py -q
xvfb-run -a .venv/bin/python -m pytest tests/test_chat_surface.py -q
xvfb-run -a .venv/bin/python -m pytest tests/test_sanitize.py tests/test_render_html.py -q
.venv/bin/python -m ruff check ui/views/chat_surface.py utils/live_guard.py render/html.py tests/test_live_guard.py
.venv/bin/python -m pyright ui/views/chat_surface.py utils/live_guard.py render/html.py 2>&1 | tail -3
```

## 5. Report format (mandatory)

Baseline vs after counts; ALL outputs verbatim; per-test-row RED proof (neuter → fail →
restore, sha-verified); the timer-shim design as actually implemented (a short snippet);
any deviation from these instructions with justification. Related-bug scan.
Do NOT git add/commit/push — Supervisor owns commits.
