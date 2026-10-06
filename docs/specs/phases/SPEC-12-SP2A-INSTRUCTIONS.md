# SPEC-12 SP2a — chat_render_handler: display-keyed surfaces (source only)

**Spec:** `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` §2b (authoritative)
**Pre-flight:** `docs/specs/phases/SPEC-12-PREFLIGHT-DECISIONS.md` (D1 key-domain table)
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** ONE file — `ui/handlers/chat_render_handler.py`. **No test edits this
sub-phase** (the 3 test files are SP2b, next sub-phase — they will go RED here,
which is expected and tracked). No other source file.

Read the file in full before editing (steelFramed read-before-touch).

---

## The one rule (spec §2b, D1)

Two key domains. Every surface-lifecycle structure is **display-keyed**; streaming
stays **session-keyed**. Every method resolves `display_key = mount_key or session_key`
ONCE and uses that variable. Resolution happened already inside `_mount_surface`; now
it moves to the surface CACHE.

Display-keyed: `_surfaces`, `_closed_sessions`, `_mounted_box_keys`, `_welcome_shown`,
`_mount_misses`. Session-keyed (UNCHANGED): `_stream_text`, `_streaming`, `_stream_role`,
`_reentrancy`. `_surfaces_by_parent` stays id(box)-keyed (unchanged).

---

## Edit 1 — `_surface_for` (spec §2b, exact target)

Replace the whole method body (keep the leading docstring intent, update it):

```python
    def _surface_for(self, session_key: str, mount_key: str | None = None):
        """Lazy per-PROJECT surface (SPEC-12: display-keyed).

        SPEC-12 BUG#1 fix: the cache is keyed by the DISPLAY key
        (`mount_key or session_key`), NOT the raw session key — one surface
        per project box. Mount retries (FIX 1) and eviction (FIX 11) are
        tracked on the same display key. Streaming stays session-keyed
        (see _stream_text/_streaming) — do not touch those here.
        """
        display_key = mount_key or session_key
        surface = self._surfaces.get(display_key)
        if surface is None:
            surface = create_chat_surface()
            self._surfaces[display_key] = surface
        mounted = self._mount_surface(display_key, surface, display_key)
        if mounted:
            self._mount_misses.pop(display_key, None)
        elif surface.get_parent() is None and self._container_getter is not None:
            self._mount_misses[display_key] = self._mount_misses.get(display_key, 0) + 1
        if (not mounted
                and surface.get_parent() is None
                and self._container_getter is not None
                and self._mount_misses.get(display_key, 0) >= self._MOUNT_MISS_LIMIT):
            surface.destroy()
            self._surfaces.pop(display_key, None)          # BUG#1b: no KeyError
            self._mount_misses.pop(display_key, None)
            self._welcome_shown.discard(display_key)
            _logger.warning(
                "chat surface evicted after %d consecutive mount misses — "
                "dropped render for display_key=%r (dead getter / closed tab)",
                self._MOUNT_MISS_LIMIT, display_key)
            return None
        return surface
```

(Keep the existing warning/log text substance; the key point is every structure
uses `display_key`. `del self._surfaces[session_key]` becomes
`self._surfaces.pop(display_key, None)`.)

## Edit 2 — `_mount_surface` (rename first param only)

Signature: `def _mount_surface(self, key: str, surface, mount_key: str | None = None) -> bool:`
Body unchanged — it already does `getter(mount_key or session_key)`; change that read
to `getter(mount_key or key)`. All other logic (parent guard, `_surfaces_by_parent`
write, bool contract) UNCHANGED. Update the docstring's first line to say the first arg
is the display key.

## Edit 3 — `_append_to_surface` (spec §2b BUG#1a, exact target)

The tombstone check MUST use the display key:

```python
    def _append_to_surface(self, role: str, text: str, session_key: str | None, agent_name=None,
                           mount_key: str | None = None):
        try:
            html_fragment = render_document(text)
        except Exception:
            _logger.exception("render_document failed — appending escaped raw text")
            html_fragment = _html.escape(text) + "<!-- fallback: escaped raw -->"
        display_key = mount_key or session_key or ""      # SPEC-12 BUG#1a
        if self._closed_sessions.get(display_key):
            return
        surface = self._surface_for(session_key, mount_key=mount_key)
        if surface is None:
            _logger.debug("render dropped: surface evicted after mount misses display_key=%r", display_key)
            return
        surface.append_message(_surface_role(role), html_fragment, agent_name=agent_name)
```

## Edit 4 — `render_welcome` (gate on display key)

`render_welcome(session_key)` is called with the tab's key, which IS its display key
(project tab → `project:<name>`). Add at the top: `display_key = session_key or ""`
and replace every `key`-based read/write (`key in self._welcome_shown`,
`self._closed_sessions.get(key)`, `self._surface_for(key)`, `self._welcome_shown.add(key)`)
with `display_key`. `_surface_for(display_key)` (no mount_key — display key is the key).
Behavior otherwise UNCHANGED.

## Edit 5 — `close_session` (spec §2b)

`session_key` here is the closing TAB's key — for a project tab it IS the display key.
Change:
- `self._closed_sessions[session_key] = True` → tombstone the display key (= session_key).
  Keep it (the tab key IS the display key under this model).
- `surface = self._surfaces.pop(session_key, None)` → `self._surfaces.pop(session_key, None)`
  (unchanged text, but now correctly hits the display-keyed cache).
- The fan-out loop `for sk, s in list(self._surfaces.items()): if s.get_parent() is resolved_box:`
  → keep it DEFENSIVE (spec: "plus any surface mounted in the passed box"). For each victim
  `sk`, tombstone `sk` (its display key) and pop. Since one box now holds one surface, this
  is N≤1 in the normal case. Keep `self._mounted_box_keys[sk] = session_key` and
  `self._welcome_shown.discard(sk)`.
- `self._streaming`/`_stream_text`/`_stream_role` discards stay session-keyed — UNCHANGED.
- `self._welcome_shown.discard(session_key)` at the end — keep (session_key == display key
  for the closed tab).

Do NOT change `pop_tombstones_for_box` / `pop_tombstone` logic — they operate on tab/display
keys already.

## Edit 6 — `render_async` (spec §2b BUG#4+#10)

Signature: add `mount_key: str | None = None` (last param):
```python
    def render_async(self, role: str, text: str, session_key: str, on_bubble_ready, on_forward_click=None, on_error=None, agent_name: str = None, agent_color: str = None, mount_key: str | None = None):
```
Thread it into `_append_on_main` → the surface read:
```python
                        surface = self._surface_for(session_key, mount_key=mount_key)
```
and the tombstone check inside `_append_on_main`: use
`display_key = mount_key or session_key` for `self._closed_sessions.get(display_key)`.
The fallback `self._surface_for(session_key).append_message(...)` inside the except →
`self._surface_for(session_key, mount_key=mount_key)`.
Add to the docstring: *"pass mount_key for any non-project caller; the surface cache
is display-keyed."*

## Edit 7 — `surface_for_key` docstring

Keep `return self._surfaces.get(session_key)`. Update the docstring: the arg is now a
**display key** (the cache is display-keyed). No code change.

## Edit 8 — `render_sync` / `end_streaming` docstrings

Update the `mount_key:` docstring line in both: the surface CACHE is now display-keyed
(not "stays session-keyed"). `render_sync` already threads mount_key → `_append_to_surface`.
`end_streaming` already threads mount_key → `_finalize` → `_append_to_surface`. No
signature changes.

---

## What must NOT change
- `_stream_text`/`_streaming`/`_stream_role` (session-keyed) and `_reentrancy`.
- `_MOUNT_MISS_LIMIT` value, `surface_for_box`, `_surfaces_by_parent` semantics.
- `render_event_card` / `render_task_card` (Pango, untouched).

## Expected intermediate state
`tests/test_chat_render_handler.py` + `test_welcome_html.py` + `test_activity_pill_adapter.py`
will go **RED** (they assert the session-keyed model) — that is SP2b's job, next. Do NOT
edit tests in SP2a. Just report the RED count as evidence of the semantic change.

## Verification battery (paste all outputs)
- `cd /home/mushy/projects/develcakes && xvfb-run -a .venv/bin/python -m pytest tests/test_chat_render_handler.py -q` → report pass/fail counts (RED expected)
- `~/.local/bin/ruff check ui/handlers/chat_render_handler.py` → **BASELINE IS NOT 0**:
  this file has **12 pre-existing findings on HEAD** (1 BLE001, 3 I001, 8 RUF013 —
  verified by running ruff on `git show HEAD:ui/handlers/chat_render_handler.py`).
  The brief's "0" from earlier phases was a stale/under-counted baseline (documented
  in SPEC-10's post-mortem). Requirement: **no NEW findings** — the count must be
  ≤12 and no finding may point at a line you changed. Do NOT fix the pre-existing
  12 (out of scope; they are RUF013 implicit-Optional / import-sort noise).
- `.venv/bin/pyright ui/handlers/chat_render_handler.py` → 0 errors (verified 0 on HEAD)
- `wc -l ui/handlers/chat_render_handler.py` → 972 baseline

## Report back (mandatory COMPLETENESS)
```
COMPLETENESS:
- [x/not done] Edit 1: _surface_for display-keyed — evidence: diff hunk
- [x/not done] Edit 2: _mount_surface param rename — evidence: diff hunk
- [x/not done] Edit 3: _append_to_surface display-key tombstone — evidence: diff hunk
- [x/not done] Edit 4: render_welcome display key — evidence: diff hunk
- [x/not done] Edit 5: close_session display key — evidence: diff hunk
- [x/not done] Edit 6: render_async(mount_key=) — evidence: diff hunk
- [x/not done] Edit 7/8: docstrings — evidence: diff hunk
- [x/not done] RED count from test_chat_render_handler.py (expected) — pasted
- [x/not done] ruff / pyright / wc outputs
- [x/not done] Related issues found, NOT fixed
```

Invoke `prompts/steelFramedCodeWriter.md` before writing. Please write the change per
this brief and report when done.