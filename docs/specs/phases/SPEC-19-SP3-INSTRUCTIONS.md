# SPEC-19 SP3 — Phase Instructions: T2 CSS property allowlist extension

**Spec:** `docs/specs/SPEC-19-LIVE-CHAT-SURFACE.md` §5 SP3 row (amended: scoped to
inline-capable properties; `animation-*`/`@keyframes` are T3-only — they need `<style>`,
which nh3 can never pass).
**Depends on:** SP1+SP2 (done, audited). This phase touches ONLY the T2 static path —
no live-tier code, no enforcement code.
**Files in scope:** `render/sanitize.py` (`_AGENT_CSS_PROPERTIES` + its tests),
`tests/test_sanitize.py` (extend). Nothing else.
**Word marker:** please write.

---

## 0. Baseline (record verbatim)

```bash
.venv/bin/python -m pytest tests/test_sanitize.py tests/test_render_html.py -q
.venv/bin/python -m ruff check render/sanitize.py
```

## 1. The change

`_AGENT_CSS_PROPERTIES` (render/sanitize.py, the frozenset currently 49 properties)
gains the inline-capable chart/layout properties, per SPEC-19 §5 SP3:

**Add:**
- `background` (shorthand — enables gradients: `linear-gradient`, `radial-gradient`,
  `conic-gradient`; NOTE: it also admits `url(...)` VALUES — see §2, the url-deny test)
- `background-image`
- `background-size`, `background-position`, `background-repeat`, `background-clip`
- `filter`
- `transform`, `transform-origin`
- `transition`, `transition-property`, `transition-duration`, `transition-timing-function`, `transition-delay`
- `backdrop-filter`
- `object-fit`, `object-position`
- `aspect-ratio`
- `position`, `top`, `right`, `bottom`, `left`, `z-index`
- `inset`
- `grid-template-columns`, `grid-template-rows`, `grid-column`, `grid-row`,
  `grid-auto-flow`, `grid-auto-columns`, `grid-auto-rows`, `grid-area`,
  `place-items`, `place-content`, `place-self`
- `clip-path`
- `mask`, `mask-image`, `mask-size`, `mask-position`, `mask-repeat`
- `border-image`, `border-image-source`, `border-image-slice`, `border-image-width`, `border-image-outset`, `border-image-repeat`
- `columns`, `column-count`, `column-width`, `column-gap`
- `resize`, `user-select`, `pointer-events`
- `visibility`
- `content-visibility`, `contain`
- `scroll-margin`, `scroll-padding` (+ longhands not needed — shorthands only)

**Do NOT add:** `animation`/`animation-*` (T3-only per the amendment), `@keyframes`
(not a property — impossible anyway without `<style>`), any `url()`-only properties
beyond what background/mask/border-image already carry (they stay in with the value
denied — §2).

## 2. The url() question (load-bearing — read twice)

The deny-by-omission design kills `url()` by property NAME. Adding `background` etc.
RE-OPENS url() values on those properties. Determine the actual behavior:

1. **Probe first:** does `nh3` with `filter_style_properties` strip `url(...)` VALUES,
   or pass them? Write the probe BEFORE deciding the fix (a test asserting the answer
   you didn't verify is a lie).
2. If url() values pass through: WebKit will attempt to LOAD `url(http://...)` from a
   T2 static card. Verify whether the E1 filter covers the T2 document too — check
   where the T2 path renders: if it renders in the SAME transcript webview (it does —
   T2 rows are part of `_document`), then E1 already blocks the load and a url() is
   inert. If that's confirmed by a probe, document it and ADD THE TEST pinning it
   (a T2 `background:url(http://127.0.0.1:PORT/x)` in the transcript → 0 hits).
3. If E1 does NOT cover it (e.g. T2 renders in a different view, or the filter misses
   CSS-driven loads), then STOP and report — do not ship url() reachability on T2.

## 3. Tests (RED-first; extend tests/test_sanitize.py)

| Test | Assert |
|---|---|
| `background` survives | `sanitize_agent_html('<div style="background: conic-gradient(red 0 30%, blue 30% 100%)">x</div>')` keeps the style |
| gradient longhand | `background-image: radial-gradient(...)` kept |
| `filter`/`transform`/`transition` kept | inline values preserved verbatim |
| grid properties kept | `display` was already allowed; `grid-template-columns: 1fr 2fr` kept |
| **url() denial** | per §2 outcome: either the property+value is stripped by nh3, or E1 blocks the load (probe-pinned, test added at the right layer) |
| `animation` NOT admitted | `style="animation: spin 2s"` → property stripped (T3-only rule) |
| markdown path unchanged | the T1 escape-first path never had these properties — `render_document` output for a user message with `style=` shows no style (already true; pin it) |

## 4. Verification (paste ALL outputs)

```bash
.venv/bin/python -m pytest tests/test_sanitize.py tests/test_render_html.py tests/test_html_guard_sites.py -q
xvfb-run -a .venv/bin/python -m pytest tests/test_live_guard.py -q   # unchanged — must stay green
.venv/bin/python -m ruff check render/sanitize.py tests/test_sanitize.py
.venv/bin/python -m pyright render/sanitize.py 2>&1 | tail -3
```

## 5. Report format (mandatory)

The §2 probe RESULT first (what does nh3 do with url() values — quote the probe
output). Then: baseline vs after counts, all outputs verbatim, per-test RED proofs,
the final property list as a diff summary (names only). Related-bug scan.
Do NOT git add/commit/push.
