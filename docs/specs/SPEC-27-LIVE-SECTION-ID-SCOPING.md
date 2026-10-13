# SPEC-27: Section-scoped `getElementById` for live sections

Status: ready-for-implementation
Files: ui/views/chat_surface.py (+ tests)
Depends on: SPEC-19 (live sections, shim layer, resurrection)

## Problem

Live sections are appended into ONE shared transcript document. A section's
script calling `document.getElementById('chart')` resolves against the whole
document, so a bare author-chosen id binds the **first** matching node in the
transcript — typically an earlier message's section. The later card renders
empty while the earlier card silently becomes controlled by the later script.
No error, no warning.

Observed this session: two live sections both using `id="q"` / `id="t"`; the
second's script bound the first's elements.

## Design decision — scope, do not rewrite

Two candidate approaches were considered:

- **A. Auto-prefix injected ids** (rewrite markup + script strings with a
  per-section prefix). Rejected: it is a source-rewriting transformer with a
  large correctness surface (must rewrite ids in markup AND every lookup string
  in the script, must avoid double-prefixing already-namespaced sections like
  `r7-chart`, and drifts if prefix derivation is unstable across re-renders).
- **B. Section-scoped `getElementById` shim** (chosen). The host already owns a
  per-section identity (`window.__dcCurrentLive`, set around script execution at
  `chat_surface.py:347`/`:355`) and already installs window-guarded shims
  (`_LIVE_SHIM_JS`, consumed at `:320` via the `__SHIMS__` token). Extend that
  layer with one more shim. No markup rewriting, no script rewriting, no
  author-visible change to any existing section.

**Contract: existing sections must behave identically.** A section that already
scopes with `document.currentScript.parentElement` and uses unique prefixes keeps
working. A section using a bare id keeps working *and* now resolves to its own
node.

## Implementation

### 1. New constant `_LIVE_ID_SHIM_JS` in `ui/views/chat_surface.py`

Installed ONCE per WebView, window-guarded exactly like `_LIVE_SHIM_JS`
(`if (!window.__dcIdShimInstalled) { ... }`).

Within a live section's script, `document.getElementById(id)` must resolve
against the section that owns the currently-executing script. Implementation:

- Capture the section root at call time. Preferred: the executing script's
  `parentElement`, falling back to `document.currentScript && document.currentScript.parentElement`.
  Do NOT assume a fixed host element.
- Lookup order inside the section root:
  1. `root.querySelector('#' + CSS.escape(id))` — the id as authored, so
     hand-prefixed sections (`r7-chart`) resolve unchanged;
  2. if not found, `root.querySelector('[id="' + id.replace(/"/g, '\\"') + '"]')`
     for ids that are not valid CSS identifiers;
  3. if still not found, fall back to the original document-global
     `getElementById` (preserves the singleton `transcript` lookup at `:186` and
     any deliberate cross-section reference).
- Must return `null`, never throw, for a missing id.
- Must not recurse: keep a reference to the original bound method and call that.

### 2. Wire into the shim slot

Add `_LIVE_ID_SHIM_JS` to the `__SHIMS__` substitution at `chat_surface.py:405`
alongside `_LIVE_SHIM_JS` and `_LIVE_BRIDGE_INIT_JS`. Order: install the id shim
BEFORE any section script executes (the `__SHIMS__` slot runs at document
install time, ahead of the resurrection wrapper, so any position in the list is
acceptable; keep it adjacent to the timer shims for readability).

### 3. Identity source

`window.__dcCurrentLive` is the section's `data-live-id` at execution time. Use
it only for diagnostics/`data-` lookups, NOT as a CSS selector — it may contain
characters that are not selector-safe. The DOM root from the script's
`parentElement` is the authority for scoping.

## Tests

Add to `tests/test_chat_surface.py` (JS-generation assertions, matching that
file's existing style — assert on the generated JS string):

1. `test_live_id_shim_is_installed` — `_LIVE_ID_SHIM_JS` is non-empty and the
   `__SHIMS__` slot output contains it.
2. `test_id_shim_is_window_guarded` — contains the install guard; installing
   twice does not double-wrap (assert the guard string present).
3. `test_id_shim_scopes_lookup_to_section` — the generated JS looks up inside
   the script's parent element before the document.
4. `test_id_shim_falls_back_to_document` — fallback to the original
   `getElementById` is present (singleton `transcript` lookups keep working).
5. `test_id_shim_does_not_recurse` — the shim calls the captured original, not
   `document.getElementById` by name.
6. `test_existing_shims_unchanged` — `_LIVE_RESURRECT_JS` and `_LIVE_SHIM_JS`
   still generated identically (no regression to the timer/bridge shims).

## Acceptance

- `tests/test_chat_surface.py` green; `tests/test_live_guard.py`,
  `tests/test_live_bridge.py` green.
- No change to markup generation, no change to `_LIVE_RESURRECT_JS`, no change
  to the bridge API, no change to the static `html` sanitizer path.
- ruff clean on touched files.

## Non-goals

- Auto-prefixing / markup rewriting (approach A) — explicitly rejected above.
- Changing the timer or bridge shims.
- Any behaviour change for sections that already scope correctly.

## Known Limitations

### Async callback lookups are document-global

The scope context comes from `document.currentScript.parentElement`, and
`document.currentScript` is non-null only during the **synchronous** prefix of
a section's script. By the time a deferred continuation runs — a
`setTimeout`/`requestAnimationFrame` callback, a resolved-Promise `.then`, an
event handler — `currentScript` is `null` again, the shim cannot determine the
injecting section, and every lookup degrades to the document-global
`getElementById` original.

Consequence: with duplicate ids across two live sections, async code in either
section resolves to the **first** section's node. The scoping guarantee holds
for synchronous code only.

This behaviour is pinned, not fixed: see
`test_id_shim_async_fallback_is_document_global_known_limitation` in
`tests/test_chat_surface.py` (`TestLiveSectionIdShim`), which asserts the
document-global resolution through the deferred path and is marked as the RED
target for the follow-up spec.

**Follow-up spec:** capture section identity at *scheduling* time — wrap timer
and promise-scheduling entry points while `currentScript` is still live, stash
the section root on the scheduled callback, and restore it for the
continuation — so deferred lookups scope to the scheduling section rather than
the document.
