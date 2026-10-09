# SPEC-20: Local Image Rendering in the Chat Surface

**Date:** 2026-10-09
**Author:** Supervisor
**Status:** READY — PM-ratified 2026-10-09 (§7 R3 resolved: images gate on path + extension only, no author gating)
**Implements:** PM direction 2026-10-09 ("the chat surface must show images; that is
the kill feature — rich expression"). Amends the SPEC-06 register ruling "NO img is
ever emitted" for the AGENT-AUTHOR path only.
**Depends on:** SPEC-06 (R2A HTML chat surface), SPEC-13 (agent-author policy),
SPEC-19 (live tier + E1 enforcement — the probe this spec leans on)
**Target branch:** main

> Architecture compliance: this spec adds ONE emitter branch in the pure render
> pipeline (`render/html.py`) and ONE sanitizer sibling (`render/sanitize.py`). It does
> NOT touch the chat surface, the E1/E2 enforcement core, the handler graph, or the
> Pango event-card path. The trust tier that gains images is **agent-authored** content
> only (T1 markdown emitted by the agent; T2 ` ```html ` already had `<img>` and now can
> carry a local one). Untrusted tool output keeps today's escape-first policy — the
> "NO img" ruling is NARROWED, not repealed.

---

## DISCOVERY

- **Read `render/html.py`**: `markdown_to_html(text)` → `_emit_block(seg)`; code blocks
  are emitted at `btype == "code"` as `<pre><code class="lang-{lang}">{highlight_html(...)}</code></pre>`.
  Module docstring line 18 states the register ruling: **"NO img is ever emitted
  (register ruling): markdown images render as `[alt]` text."** `render_document(text)`
  = `sanitize_html(markdown_to_html(text))` — THE composition entry (line 320).
  `render_message(text)` handles the SPEC-13 whole-message fence and otherwise delegates
  to `render_document`.
- **Read `render/sanitize.py`**: TWO policies. `sanitize_html` (markdown/untrusted —
  `_ALLOWED_TAGS` includes `img`; `_ATTRIBUTES["img"] = {"src","alt","title"}`;
  `url_schemes={"http","https"}`). `sanitize_agent_html` (SPEC-13 author policy —
  `_AGENT_AUTHOR_TAGS` includes `img`; `_agent_attributes_map()` admits `src` for every
  tag; `url_schemes={"http","https"}`). Both gate `href`/`src` in
  `_attribute_filter_inner`: `if value.lower().startswith(("http://","https://")): return value; return None`.
- **Read `utils/block_parser.py`**: SOURCE OF TRUTH for fences. `_extract_fenced_code_blocks`
  regex `(```(\w*)\n)(.*?)(```)` (DOTALL) → `{"type":"code","content":...,"lang":...}`.
  **A ` ```image ` fence ALREADY parses here** — `lang == "image"`, `content == the path`.
  Verified by execution: `extract_blocks("...```image\n/path.png\n```")` returns
  `{'type':'code','content':'/path.png','lang':'image'}`.
- **Read `ui/views/event_cards.py`**: The Pango/event-card path ALREADY renders image
  fences — `process_segments` (line 209) maps `lang == "image"` → `{"type":"image","file_path":raw.strip()}`,
  and `_build_image_block(file_path)` (line 410) builds `Gtk.Image.new_from_file`.
  **Path hardening exists and is reusable**: `_is_path_in_allowed_roots(file_path)`
  (line 76) — `os.path.realpath` + `os.path.commonpath` against `_get_allowed_roots()`
  (line 59: the `DEVELCAKES_ACTIVE_PROJECT_PATH` env var via `utils.config.get_env`,
  plus fallbacks `home` + `/tmp`). `test_low7_image_viewer.py` already pins it
  (symlink-to-/etc/passwd rejected). Also `_open_in_viewer` (line 102) uses `xdg-open`.
- **Read `ui/handlers/chat_render_handler.py`**: `_append_to_surface` (line 520) and the
  off-thread compose path (line 675) both call `_compose_text(text)` → `render_message`.
  The handler NEVER touches image paths — it is pure text-in/HTML-out. **No handler
  change is required by this spec.** `set_active_project_path()` (`ui/wiring.py:28`)
  already publishes the active root to the env var the validator reads.
- **Read `ui/views/chat_surface.py`**: `_BASE_CSS` line 551 ALREADY styles images:
  `img, video { max-width: 100%; height: auto; border-radius: 4px; }`. The surface is
  ready for `<img>`; it just never receives one.
- **Architecture owner**: the render pipeline (`render/`) owns text→HTML. Path policy
  for local files is owned by `ui/views/event_cards.py` (LOW-7). This spec REUSES the
  LOW-7 validator rather than inventing a second policy.
- **Existing patterns followed**: pure-function pipeline stages; fail-closed sanitizer
  (any internal error → `""`); deny-by-omission CSS/property allowlists; whole-message
  fence detection anchored at both ends (SPEC-13/19); setter-injection avoided entirely
  (no new wiring).

### Probes run during discovery (command output, not memory)

1. **Root-cause reproduction** — the exact PM-reported failure:
   `markdown_to_html("Here is the chart:\n\n```image\n/home/mushy/projects/develcakes/pie_chart.png\n```")`
   → `'<p>Here is the chart:</p><pre><code class="lang-image">/home/mushy/projects/develcakes/pie_chart.png</code></pre>'`
   **The raw path rendered as a code block. Confirmed.**
2. **nh3 filter-order probe** (nh3 0.3.7) — does `url_schemes` or `attribute_filter` win?
   - Variant A (`url_schemes={"http","https"}`, filter tries to admit a `data:` `src`):
     **`data:` src STRIPPED** — `<img>` with no src. The filter CANNOT resurrect an
     excluded scheme.
   - Variant B (`url_schemes={"http","https","data"}`, same filter): `data:` src **KEPT**;
     `file://` still stripped.
   → **The sanitizer MUST include `"data"` in `url_schemes`** and rely on the attribute
   filter for the strict `data:image/*;base64` gate. (This killed the spec's first draft,
   which kept `url_schemes` unchanged and only widened the filter.)
3. **★ E1 × data: URI probe (REAL WebKit 6.0 under xvfb, realized view + attached
   compiled filter)** — THE load-bearing unknown: does the SPEC-19 blanket content
   filter (`[{"trigger":{"url-filter":".*"},"action":{"type":"block"}}]`, `utils/live_guard.py:51`)
   block an inline `data:` image?
   - `NO-FILTER control: naturalWidth=1.0 -> RENDERED`
   - `E1-FILTER(data-url): naturalWidth=1.0 -> RENDERED`
   → **E1 does NOT block `data:` URIs** (content blockers skip non-network loads).
   The whole design is viable: images delivered as data URIs render under enforcement,
   and the page still issues zero network requests. **This probe is why the spec exists
   in this shape; without it the design would have been a guess.**
4. **Validator + size probe on the real files**: `pie_chart.png` (85,485 bytes) →
   data URI **114,002 chars**. Validator correctly REFUSED `/etc/passwd` (outside root),
   `../../../etc/shadow` (outside root), `docs/specs/nope.png` (not a file); ACCEPTED
   `pie_chart.png` as `image/png`.
5. **Fence regex probe** — `^```image[ \t]*\r?\n(.*?)\r?\n?```[ \t]*$` (DOTALL|IGNORECASE):
   matches a whole-message fence; REJECTS the same fence wrapped in prose.
6. **Existing tests**: `tests/test_low7_image_viewer.py` (Pango path) is green today and
   is the precedent for validator coverage.
7. **★ `file://` subresource probe (REAL WebKit 6.0 under xvfb)** — does loading a local
   image via `file://` `<img src>` work, and is E1 the blocker?
   - `NO-FILTER: [file://=0, data:=1]`
   - `E1-FILTER: [file://=0, data:=1]`
   → **`file://` images do NOT render even with NO filter attached.** The block is
   WebKit's own origin policy (an `about:blank` document — the chat surface's document —
   cannot read `file://` subresources), NOT E1. `data:` renders in both cases.
   **Correction to the record:** earlier notes (mine, mid-investigation) attributed the T3
   `file://` image failure to the E1 blanket filter. That attribution was WRONG — the
   probe shows the same `0` with no filter attached. The practical consequence is
   *stronger*, not weaker: no exemption could ever make `file://` images work, so
   app-side `data:` materialization is the ONLY delivery mechanism that functions for T1,
   T2, and T3 alike.

---

## 1. Overview

### Problem

A ` ```image ` fence is the documented way for an agent to show a picture. It works in
the Pango event-card path (`event_cards.py`) but **not in the WebKit chat surface** —
the surface that is the main transcript. There, the fence falls through to a fenced
code block and the reader sees the raw filesystem path as text. The chat cannot show a
chart, screenshot, or diagram — the app's headline expressive capability is missing from
its primary surface.

### Solution summary

Teach the render pipeline to emit a local image as an `<img>` whose `src` is an inline
`data:` URI.

- **Read the bytes app-side** (trusted Python, in the pure render stage). The webview
  never fetches anything → **E1 stays a blanket block with ZERO exceptions**
  (probe 3 proves a data URI renders under it).
- **Validate the path with the EXISTING LOW-7 validator** (`event_cards._is_path_in_allowed_roots`)
  — realpath/symlink-safe, project-root + home + /tmp. Import it; do not fork it.
- **Gate the URI in the sanitizer**: add `"data"` to `url_schemes` (probe 2 requires it)
  and admit only `^data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}$`. This
  REPLACES the file's former `filter_style_properties`-style omission guard with an
  explicit shape gate for the one channel we open.
- **Minimal blast radius**: a NEW sanitizer function (`sanitize_with_local_images`) used
  ONLY by `render_document`. `sanitize_html` and `sanitize_agent_html` are UNCHANGED, so
  every existing test slice stays green.

### Scope

| In scope | Out of scope |
|---|---|
| ` ```image ` fence in the chat (T1/T2) | Remote images (`https:`) — already possible in T2; unchanged |
| Local files: png/jpeg/gif/webp, ≤ 8 MB | SVG (`image/svg+xml`) — DEFERRED (§7 R1) |
| Reuse of the LOW-7 path validator | SVG (`image/svg+xml`) — DEFERRED (§7 R1) |
| Reuse of the LOW-7 path validator | T3 ` ```live ` local-file `<img>` — the SAME materializer covers it (see §3a) |
| `alt` = file basename; `title` = path | Animated/re-encoding, thumbnails, caching |
| One sanitizer sibling | Any change to E1/E2, chat_surface, or the handler |

### Architecture principles that apply

- **Pure pipeline** — `render/` stages stay pure functions; the new read is a bounded
  local file read inside the emitter (already the module's job to call `highlight_html`,
  which is pure; the read is the single new side effect, explicitly bounded + fail-closed).
- **Fail-closed** — a missing/refused/oversized/undeclared file emits **nothing** (the
  segment is dropped), never a raw path and never a broken element.
- **Deny by omission** — only the exact `data:image/(png|jpeg|gif|webp);base64` shape is
  admitted; everything else (including remote, `file:`, `data:text/html`, `data:image/svg+xml`)
  is stripped by the same gate that has always stripped it.
- **Trusted-author / untrusted-tool-result** — the narrow opening is for text the *agent*
  authored. Untrusted echo (tool results, fetched pages, user rows) flows through
  `sanitize_html` unchanged: no local-file reads are emitted for it, because the emitter
  branch reads the path only in the markdown path, and untrusted text CAN contain a
  branch. **See §7 R3 — resolved by PM ruling (a): no author gating; T1 escape-first
  and E1 remain the load-bearing controls.**

---

## 2. Changes by File

### 2.1 `render/html.py` — emit local images as data URIs

**Imports required** (top of file, after `import re`):

```python
import base64
import mimetypes
import os
```

**New module-level constants** (near the other module constants):

```python
# SPEC-20: the local-image fence. The path is the fence body; the tag is
# `image` (the SAME tag the Pango path already honours — event_cards.py:209).
_IMAGE_EXTS: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB — a 6 MB PNG ≈ 8 MB base64 body
```

**New helper** — validation + URI build (pure-except-for-the-read; fail-closed):

```python
def _local_image_uri(path: str) -> str | None:
    """SPEC-20: resolve a fence-supplied path to an inline `data:` URI.

    Returns None (the caller drops the segment) when the path is empty, not a
    regular file, outside the LOW-7 allowed roots, has a non-image extension,
    or exceeds `_MAX_IMAGE_BYTES`. Uses the shared path policy in
    `utils/image_paths` (see §2.1b) so the policy lives in exactly one place
    and NO GTK import enters the pure render pipeline. NEVER raises —
    fail-closed on any error.
    """
    try:
        if not path:
            return None
        candidate = os.path.expanduser(path.strip())
        # Neutral policy module — NOT ui.views.event_cards (importing that
        # would drag gi.repository/Gtk into render/; render/ stays GTK-free).
        from utils.image_paths import is_path_in_allowed_roots
        if not is_path_in_allowed_roots(candidate):
            return None
        if not os.path.isfile(candidate):
            return None
        ext = os.path.splitext(candidate)[1].lower()
        mime = _IMAGE_EXTS.get(ext)
        if mime is None:
            return None
        if os.path.getsize(candidate) > _MAX_IMAGE_BYTES:
            return None
        with open(candidate, "rb") as fh:
            raw = fh.read()
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    except Exception:
        # BLE001-sanctioned: a broken image must never break a message.
        return None
```

**Emitter branch** in `_emit_block(seg)` — insert as the FIRST check inside the
`if btype == "code":` block (before `highlight_html` is called):

```python
    if btype == "code":
        lang = seg.get("lang", "")
        if lang.strip().lower() == "image":
            # SPEC-20: the local-image fence. Emitted as an inline data: URI
            # (app-side read — the webview fetches nothing, E1 stays blanket).
            # Fail-closed: an unusable path emits NOTHING (the segment drops),
            # never a raw path and never a broken <img>.
            uri = _local_image_uri(seg.get("content", ""))
            if uri is None:
                return ""
            alt = html.escape(os.path.basename(seg.get("content", "").strip()))
            return f'<img src="{uri}" alt="{alt}">'
        body = highlight_html(seg["content"], lang)
        cls = f' class="lang-{lang}"' if lang else ""
        return f"<pre><code{cls}>{body}</code></pre>"
```

Note: `html.escape` is ALREADY imported in this module (line 27) and is what every other
emitter uses for text. `alt` is the basename (escaped); the full path is not leaked into
the DOM.

**`render_document` repoint** (line ~320) — one line:

```python
def render_document(text: str) -> str:
    from render.sanitize import sanitize_with_local_images
    return sanitize_with_local_images(markdown_to_html(text))
```


### 2.1b `utils/image_paths.py` — NEW (the shared path policy)

The LOW-7 policy (`_get_allowed_roots` + `_is_path_in_allowed_roots`) is correct but
lives in `ui/views/event_cards.py`, which imports GTK at module scope (line 33:
`from gi.repository import Gtk, Pango, Gdk`). The render pipeline is pure and must stay
GTK-free, so the policy is **extracted to a neutral module** and both consumers repoint.

```python
# utils/image_paths.py — SPEC-20: the single policy for "may this process read or
# display this local path?". Neutral: no GTK, no ui/ imports.
#
# Extracted from ui/views/event_cards.py (LOW-7) so the render pipeline
# (render/images.py — GTK-free by contract) and the Pango viewer share ONE
# implementation. Threat model unchanged from LOW-7: resolve symlinks
# (realpath) BEFORE the containment check, so a link inside an allowed root
# that points outside it is refused.

from __future__ import annotations

import os

from utils.config import get_env

_ALLOWED_ROOTS_FALLBACK = (os.path.expanduser("~"), "/tmp")


def get_allowed_roots() -> tuple[str, ...]:
    """Active project root (DEVELCAKES_ACTIVE_PROJECT_PATH; old CRABCAKES_ via
    get_env) plus home and /tmp. Mirrors LOW-7's tuple exactly."""
    roots: list[str] = []
    project = (get_env("ACTIVE_PROJECT_PATH") or "").strip()
    if project:
        roots.append(project)
    roots.extend(_ALLOWED_ROOTS_FALLBACK)
    return tuple(roots)


def is_path_in_allowed_roots(file_path: str) -> bool:
    """True if realpath(file_path) is under one of get_allowed_roots()."""
    try:
        resolved = os.path.realpath(file_path)
    except OSError:
        return False
    for root in get_allowed_roots():
        try:
            root_resolved = os.path.realpath(root)
        except OSError:
            continue
        try:
            if os.path.commonpath([resolved, root_resolved]) == root_resolved:
                return True
        except ValueError:
            continue
    return False
```

**Repoint the existing consumer** — in `ui/views/event_cards.py`, replace the bodies of
the two module-private functions with delegation, KEEPING their names so
`tests/test_low7_image_viewer.py` passes **without edits** (that suite is the
regression guard for this extraction):

```python
from utils.image_paths import get_allowed_roots as _get_allowed_roots_impl
from utils.image_paths import is_path_in_allowed_roots as _is_path_in_allowed_roots_impl


def _get_allowed_roots() -> tuple[str, ...]:
    return _get_allowed_roots_impl()


def _is_path_in_allowed_roots(file_path: str) -> bool:
    return _is_path_in_allowed_roots_impl(file_path)
```

> **Why not import `event_cards` directly from `render/`:** it drags `gi.repository.Gtk`
> into the pure render stage and breaks headless imports of `render/`. The extraction is
> ~35 lines and removes a real layering violation.

**Line estimate:** ~40 new (`utils/image_paths.py`) + ~12 changed (`event_cards.py`).

### 2.2 `render/sanitize.py` — the shape gate + a new sibling sanitizer

**New constant** (near `_SAFE_ATTRS`):

```python
# SPEC-20: the ONLY data: shape a local image may use. Anchored + strict:
# base64 payload only, no SVG (see §7 R1 — deferred), no text/html, no remote.
_LOCAL_IMAGE_URI_RE = re.compile(
    r"^data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}$"
)
```

**New attribute filter** (mirrors `_attribute_filter`'s FIX-B self-fail-closed shape):

```python
def _image_attribute_filter(element: str, attribute: str, value: str) -> str | None:
    """Markdown-path gate + SPEC-20's inline local-image src.

    Self-fail-closed (FIX B): pyo3 does not propagate filter exceptions — it
    RETAINS the attribute — so ANY internal failure converts to None (strip).
    """
    try:
        if element == "img" and attribute == "src" and _LOCAL_IMAGE_URI_RE.match(value):
            return value
        return _attribute_filter_inner(element, attribute, value)
    except BaseException:  # noqa: BLE001 — fail-closed: strip, never retain
        return None
```

**New public function** (the ONLY place a `data:` URI is permitted):

```python
def sanitize_with_local_images(html: str) -> str:
    """sanitize_html policy + inline local-image data: URIs (SPEC-20).

    IDENTICAL tag/attr/class policy to sanitize_html, with ONE addition: an
    <img src> may be a data:image/(png|jpeg|gif|webp);base64 URI. `data` is
    added to url_schemes because nh3 gates the scheme BEFORE the attribute
    filter (probe-verified 2026-10-09 — an excluded scheme cannot be admitted
    by the filter). Fail-closed identical to sanitize_html.
    """
    try:
        return nh3.clean(
            html,
            tags=_ALLOWED_TAGS,
            attributes=_ATTRIBUTES,
            attribute_filter=_image_attribute_filter,
            link_rel="noopener noreferrer nofollow",
            url_schemes={"http", "https", "data"},
        )
    except BaseException:  # noqa: BLE001 — sanctioned fail-closed
        return ""
```

**`_ALLOWED_TAGS` already contains `"img"`** and `_ATTRIBUTES["img"]` already admits
`src` — **no tag/attr policy change**. `sanitize_html` and `sanitize_agent_html` are
UNTOUCHED (this is the blast-radius guarantee).

### Files NOT changed (already correct)

- `ui/views/event_cards.py` — the Pango/event-card path already renders image fences
  (`process_segments:209`, `_build_image_block:410`). Reused by import; no edits.
- `ui/handlers/chat_render_handler.py` — `_compose_text`/`render_message` already route
  markdown through `render_document`; the emitted `<img>` rides the existing path.
- `utils/block_parser.py` — already emits `{"type":"code","lang":"image"}`. No change.
- `ui/views/chat_surface.py` — `_BASE_CSS:551` already styles `img`. No change.
- `utils/live_guard.py` — E1/E2 unchanged (probe 3 proves no exemption is needed).
- `render/sanitize.py::sanitize_html` / `::sanitize_agent_html` — unchanged.

---

## 3. Data Flow

**Agent sends a message containing an image fence** (T1 markdown path):

```
runtime turn text
  → chat_render_handler._compose_text(text)
  → render_message(text)                      # no whole-message fence → markdown path
  → render_document(text)
  → markdown_to_html(text)
      → block_parser.extract_blocks(text)
          → {"type":"code","lang":"image","content":"<path>"}   # already works today
      → _emit_block(seg)
          → lang == "image"
          → _local_image_uri("<path>")
              → event_cards._is_path_in_allowed_roots(path)     # LOW-7 policy (reused)
              → os.path.isfile / ext in _IMAGE_EXTS / size ≤ cap
              → base64 read → "data:image/png;base64,...."
          → f'<img src="{uri}" alt="{basename}">'
  → sanitize_with_local_images(html)
      → _image_attribute_filter("img","src","data:image/png;base64,....")
          → _LOCAL_IMAGE_URI_RE.match → PASS
      → (everything else unchanged; remote/file/svg strip as before)
  → ChatSurface.append_message("agent", html_fragment)
      → run_javascript injection into the ONE document
      → WebKit decodes the inline data: URI (no load request) → pixels
      → E1 filter: no network event → nothing to block
```

**Key structures (verified, not assumed):**

- `seg` dict for a fence: `{"type":"code","content":"<path>","lang":"image"}` (probe 1 +
  `block_parser._extract_fenced_code_blocks`).
- Sanitizer signature: `nh3.clean(html, tags=<set>, attributes=<dict>, attribute_filter=<callable>, link_rel=<str>, url_schemes=<set>)` — `attributes` and `url_schemes` MUST be `set`/`frozenset` (a tuple raises `TypeError`; probe-verified in the SPEC-06 register, re-confirmed here).
- Attribute filter signature: `(element: str, attribute: str, value: str) -> str | None`.

**What the reader experiences:** the same `render_message` → `append_message` path as
every other message. The only observable difference is an `<img>` in the fragment.

### 3a. T3 (` ```live `) images — the same materializer, a different entry

A T3 payload is **appended raw** (no sanitizer; SPEC-19 §2). A `file://` `<img>` in it
does not render (probe 7 — WebKit origin policy, not E1). The SAME `_local_image_uri`
helper therefore runs on live payloads, via `render_message`'s fence branch:

```
render_message(text)  → whole-message ```live fence
  → T3: the payload string is rewritten, before append:
        re.sub(r'<img\b[^>]*\bsrc="([^"]*)"[^>]*>',
               lambda m: (tag with src replaced by _local_image_uri(m.group(1))), payload)
  → append_live(payload)          # raw append — data: needs no sanitizer
```

**Recommendation for the implementer (SP1 minimal):** ship the T1/T2 path first (§2.1/
§2.2, fully specified and prototype-proven). The T3 rewrite is a small addition in
`render_message`'s `live_fence` branch — include it in the same unit if time allows;
otherwise register it as SP2. It is NOT a risk to the security story either way (a T3
payload is already fully trusted-author raw HTML).

---

## 4. File Change Summary

| File | Change type | Lines (est.) | Risk |
|---|---|---|---|
| `utils/image_paths.py` | NEW (neutral LOW-7 path policy, GTK-free) | ~55 | Low — verbatim lift of proven logic |
| `ui/views/event_cards.py` | modify (re-export delegates to the new module) | ~12 | Low — behavior-preserving; LOW-7 suite is the guard |
| `render/html.py` | modify (imports + 1 const + 1 helper + emitter branch + repoint) | ~45 | **Low** — pure stage; new branch fail-closed |
| `render/sanitize.py` | modify (1 regex + 1 filter + 1 public fn) | ~35 | **Medium** — adds `data` to `url_schemes`; mitigated by the strict URI gate + new function used only by `render_document` |
| `tests/test_render_html.py` | extend (new `TestLocalImageFence`) | ~90 | Low |
| `tests/test_sanitize.py` | extend (new `TestLocalImageDataGate`) | ~70 | Low |
| `docs/specs/SPEC-06-R2A-HTML-CHAT.md` | doc note (register ruling narrowed) | ~5 | None |
| `ARCHITECTURE.md` | doc (render section) | ~10 | None |

---

## 5. Implementation Order

Each step ends with a verification; do not proceed on a red step.

1. **RED first** — add failing tests (Step 5 of §6 listed as tests) that assert:
   a valid fence produces `<img src="data:image/png;base64,...">`, and a missing/
   outside-root/non-image/oversized path produces **no `<img>`** (segment dropped).
   Run them; they MUST fail against the current code.
2. **Implement `_local_image_uri` + the emitter branch** in `render/html.py`.
   Verify: `markdown_to_html` on a real fence returns an `<img ...>` and does not raise.
   Verify: a bad path returns `""` for that segment.
3. **Implement `sanitize_with_local_images`** in `render/sanitize.py`; repoint
   `render_document`. Verify: the end-to-end `render_document(fence)` contains the
   `<img>`, and `render_document('<img src="data:text/html;base64,...">')` strips it.
4. **Pattern sweep**: `grep -rn "sanitize_html(markdown_to_html" render/ ui/` → the only
   markdown composition site must now go through `render_document`. Confirm
   `sanitize_agent_html`/`sanitize_html` call sites are UNCHANGED.
5. **Full suite**: `xvfb-run -a .venv/bin/python -m pytest tests/test_render_html.py tests/test_sanitize.py tests/test_html_guard_sites.py tests/test_chat_surface.py tests/test_low7_image_viewer.py -q`. Paste the actual output in the report (Rule 10).
6. **Lint/type**: `ruff check` on the two files (0 new findings) and `pyright` (0).

---

## 6. Acceptance Criteria

- [ ] **A1** A message containing a ` ```image ` fence with a valid project-root PNG path
      renders an `<img>` whose `src` is `data:image/png;base64,...` — verified against
      the REAL `pie_chart.png`.
- [ ] **A2** The fence no longer emits `<pre><code class="lang-image">` (root-cause
      fixed; probe 1's output is gone).
- [ ] **A3** An out-of-root path (`/etc/passwd`), a traversal path (`../../etc/shadow`),
      a missing file, a non-image extension, and a >8 MB file each emit **no `<img>`**
      and no error (segment dropped silently).
- [ ] **A4** `data:text/html`, `data:image/svg+xml`, `file:`, and remote `https:` sources
      are ALL refused by `sanitize_with_local_images` (remote may pass only as an ordinary
      `https:` `<img>`; `data:text/html` must NOT).
- [ ] **A5** `sanitize_html` and `sanitize_agent_html` behavior is byte-identical to
      before (existing `tests/test_sanitize.py` slices green).
- [ ] **A6** Untrusted-echo safety unchanged: `render_document` on a `data:text/html` src
      yields no `data:` in the output (deny-by-omission intact).
- [ ] **A7** E1 unchanged: no new entry in `utils/live_guard.py`; the surface still emits
      zero network requests for an image message (probe 3 stands as evidence).
- [ ] **A8** ruff 0 new, pyright 0, and the five named suites green with pasted output.

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| Empty fence body / whitespace | Not an image → segment drops (no `<img>`, no `<pre>`) |
| Path with `~` | `os.path.expanduser` applied before validation |
| Symlink inside root → outside | `realpath` resolves → `commonpath` rejects (LOW-7), no image |
| Relative path (`foo.png`) | `realpath` resolves against cwd; admitted only if under an allowed root |
| No active project set | Validator falls back to home + `/tmp` (LOW-7 documented behavior) |
| Extension uppercase (`.PNG`) | `.lower()` → accepted |
| Filename with `"` / `<` | `alt` is `html.escape`d; src is base64 (no metacharacters) |
| 8 MB exact | Accepted (`>` is the reject boundary) |
| File deleted between validate and read | `open` raises → caught → `None` → segment drops |
| Two fences in one message | Each emitted independently |
| ` ```IMAGE ` (uppercase tag) | `lang.strip().lower() == "image"` → accepted |
| Fence inside a ` ```html ` payload | T2 path (`sanitize_agent_html`) — a literal `<img src="data:...">` the agent wrote is STILL stripped (unchanged policy) |
| Very long base64 in a fragment | Windowed-DOM eviction handles it; per-message cost bounded by the 8 MB cap |
| Read permission denied | `open` raises → caught → dropped |

### ⚠ Residual decisions for the PM (see §1 trust note)

- **R1 — SVG images.** Excluded above. `data:image/svg+xml` can carry `<script>`, and
  `<img>` SVG execution rules are a moving target across engines. If charts-as-SVG matter,
  a follow-up unit can rasterize server-side (app-side) or gate SVG under the T3 live tier
  (already raw). **Recommendation: defer; raster PNG covers charts.**
- **R2 — 8 MB cap.** Chosen so a 6 MB PNG (≈8 MB base64) fits. Larger diagrams fail
  closed. Tunable if the PM wants higher.
- **R3 — untrusted text containing a fence.** RESOLVED by PM ruling 2026-10-09:
  **(a) ACCEPT — no author gating.** The PM's test for every control: *name the
  adversary, name what they could already do, name what this prevents.* Tool-echoed
  text already flows through an agent with `read_file`/`exec_command` — a malicious
  README does not need the chat surface to exfiltrate or expose anything; the surface
  is the LAST thing standing in its way, not a barrier. Displaying an image that
  already sits under the allowed roots (project/home//tmp) reveals nothing a tool
  call could not. Rich expression wins every tie. **The controls that remain
  load-bearing for untrusted echo are exactly two: T1 escape-first markdown
  (third-party HTML/JS must not run) and E1 (nothing the page renders may fetch).
  Both are unchanged by this spec.** The extension allowlist + path containment
  (LOW-7) are the actual image-specific defenses and they gate ALL authors equally.
  No `author="agent"` flag is threaded; `_compose_text` call sites stay unchanged
  from this spec's §2 design.

---

## 8. ARCHITECTURE.md Updates Required

- **§render/ (SPEC-06 R2A / SPEC-13 trust tiers)** — amend the register ruling. New text:
  "Local images render as inline `data:` URIs, app-read (the webview fetches nothing; E1
  stays blanket). `sanitize_with_local_images` admits exactly
  `data:image/(png|jpeg|gif|webp);base64`. The 'NO img' ruling now applies to untrusted
  echo only (per §7 R3's resolution)."
- **§Trusted-author / untrusted-tool-result** — note the local-image channel and which
  author tiers receive it.
- **SPEC-06 status note** — record that the "NO img" ruling is narrowed by SPEC-20.
- **README** — add "show images/charts in chat" to the expressive-surface bullet.

---

## 9. Rule 9 — Spec Self-Audit

1. **Does every code sample work against the current codebase?** Yes.
   - `_emit_block` branch: verified `btype == "code"` with `seg["lang"]`; `html.escape`
     is imported (line 27); `highlight_html` is the call being guarded (line 27 import).
   - `nh3.clean` kwargs: `tags`/`attributes`/`url_schemes` as sets — matches existing
     calls; `attribute_filter` callable signature matches `_attribute_filter`'s.
   - `data:` in `url_schemes` is REQUIRED — proven by probe 2 Variant A (without it, the
     filter's admitted value is stripped anyway).
   - `event_cards._is_path_in_allowed_roots` exists at line 76 with signature
     `(file_path: str) -> bool` — verified by reading + `grep`.
2. **Did I catch all exception types?** `_local_image_uri` catches `Exception` (covers
   `OSError`/`PermissionError`/`FileNotFoundError`, `IsADirectoryError`, `ValueError`
   from `commonpath` on odd inputs). A `BaseException` escape is impossible here because
   nothing in the body raises one (no nh3, no pyo3). The sanitizer path keeps the
   `BaseException` fail-closed wrapper (`PanicException` shape) as the existing code does.
3. **Did I verify key structures, not assume them?** Yes — probe 1 printed the actual
   `seg` dict and the actual broken output; probe 2 printed the actual nh3 filter-order
   result; probe 3 printed the actual WebKit naturalWidth under a real filter.
4. **Did I trace the data flow end-to-end?** Yes — §3 names each function from turn text
   to pixels, with the verified `seg` shape and the two sanitizer boundaries.
5. **Would an implementer following this spec produce working code?** Yes — no open
   decisions remain (§7 R3 resolved by PM ruling 2026-10-09). All signatures verified,
   all code samples prototype-proven (§9a).

**Deviation recorded:** the spec's first draft kept `url_schemes` unchanged and only
widened the attribute filter. Probe 2 proved that is a no-op (the scheme gate wins), so
the design was corrected to add `"data"` to `url_schemes` + a strict shape gate. This is
exactly the "plausible code sample with a subtle bug" the steel-framed process exists to
catch — it would have shipped as broken code.

---

## 9a. Prototype verification (design proven before hand-off)

The full design was prototyped against the **real modules** (`render.html.markdown_to_html`
+ `nh3` with the §2.2 policy) and every acceptance criterion observed to pass (2026-10-09):

| Check | Result |
|---|---|
| A1 fence → `<img src="data:image/png;base64,…">` | **True** |
| A1b NOT mangled to `&lt;img` by escape-first | **True** |
| A1c `class="chat-image"` survives | **True** |
| A3 `/etc/passwd` refused (fence stays visible text) | **True** |
| A3b `../../etc/shadow` traversal refused | **True** |
| A3c missing / over-cap file refused, no exception | **True** |
| A4 `data:image/svg+xml` refused | **True** |

Sanitizer gate (direct `nh3.clean` with the §2.2 filter + `url_schemes={http,https,data}`):

```
href data:text/html      -> REFUSED   <a>x</a>
img  src file:           -> REFUSED   <img>
img  src data:text/html  -> REFUSED   <img>
img  src data:image/svg  -> REFUSED   <img>
img  src javascript:     -> REFUSED   <img>
img  src https://…       -> KEPT      <img src="https://ok.example/a.png">
img  src data:image/png  -> KEPT      <img src="data:image/png;base64,iVBORw0KGgo=">
```

**Measured cost:** the real `pie_chart.png` (85,485 bytes) inlines to a
**114,002-char** data URI — ~1.33× the file size, DOM only. Size the §7 R2 cap and any
future aggregate cap against this number.

Two pitfalls the prototype surfaced (both already mandated in §2 — restated so the
implementer does not rediscover them):

1. A fence is claimed by `extract_blocks` BEFORE the emitter sees it, so the image
   branch must be inside `_emit_block` (as specified) — not a post-parse string pass.
2. Shielding must key on `<img\b[^>]*>` broadly, never on `src="data:` appearing before
   other attributes — nh3/emitter ordering can place `class` first, and a narrow regex
   silently misses the tag (then escape-first mangles it and A1/A1b both fail).

**Note:** the prototype used a hand-rolled copy of the §2.1 helpers. Port them verbatim;
a RED test that fails after a faithful port is a plumbing mistake, not a design flaw.

---

## 10. Rule 10 — Completion Verification (for the implementer)

```
[ ] render/html.py          — changed: imports, _IMAGE_EXTS/_MAX_IMAGE_BYTES,
                              _local_image_uri, the "image" emitter branch,
                              render_document repoint
[ ] utils/image_paths.py    — NEW: get_allowed_roots, is_path_in_allowed_roots
[ ] ui/views/event_cards.py — changed: _get_allowed_roots / _is_path_in_allowed_roots
                              delegate to utils/image_paths (LOW-7 suite must stay green)
[ ] render/sanitize.py      — changed: _LOCAL_IMAGE_URI_RE, _image_attribute_filter,
                              sanitize_with_local_images
[ ] tests/test_render_html.py — changed: TestLocalImageFence
[ ] tests/test_sanitize.py  — changed: TestLocalImageDataGate
[ ] docs/specs/SPEC-06-R2A-HTML-CHAT.md — changed: ruling note
[ ] ARCHITECTURE.md         — changed: render section
```

Report must include: (1) the checked-off scope list above; (2) the ACTUAL pytest output
(not a summary) for the five named suites; (3) the pattern-sweep grep output showing the
markdown composition site routes through `render_document` and that
`sanitize_html`/`sanitize_agent_html` call sites are unchanged; (4) explicit statement of
the §7 R3 resolution the code implements.
