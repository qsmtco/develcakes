# SPEC-06 Sub-Phase 1 Instructions — render/sanitize.py + XSS Battery

**Spec:** docs/specs/SPEC-06-R2A-HTML-CHAT.md §2 render/sanitize.py + §6 battery
**Parent plan:** docs/specs/phases/SPEC-06-SUBPHASES.md
**MICRO-phase. Scope: exactly 2 NEW files** — `render/__init__.py` + `render/sanitize.py`
+ `tests/test_sanitize.py` (3 paths; treat __init__ as mechanical). Tool budget ~12.
Security-critical: this is the fail-closed layer everything rides on.

## API REALITY (supervisor-probed 2026-09-24 — supersedes the spec's sketch)

- nh3 version installed: **0.3.7** (spec's ">=2.0" does not exist; pyproject pins
  `nh3>=0.3.7` — SP6 lands the pin)
- `nh3.clean(html, tags=, attribute_filter=, link_rel=, url_schemes=, ...)` —
  there is NO `url_policy` param. Scheme restriction = `attribute_filter` callback
  (probe-verified): returns the value to keep or None to strip, per (element, attr, value).
- Event handlers (`onerror=` etc.) stripped by default (probe-verified).
- `nh3.clean(None)` raises TypeError — the try/except fail-closed wrapper is REQUIRED.
- `strip_content` does not exist; content stripping is via `clean_content_tags`.

## Task 1 — render/sanitize.py

```python
_ALLOWED_TAGS = frozenset({"h1","h2","h3","h4","h5","h6","ul","ol","li","p","br",
    "hr","blockquote","pre","code","em","strong","del","a","table","thead","tbody",
    "tr","th","td","img"})
_SAFE_ATTRS = frozenset({"href","title","alt","src"})  # src: http(s)-only via filter

def _attribute_filter(element, attribute, value):
    # href/src: http(s) only (spec §2 url policy, ported to 0.3.7 API).
    # Everything else in _SAFE_ATTRS passes; unknown attrs return None (stripped).
    ...

def sanitize_html(html: str) -> str:
    """Fail-closed nh3 wrapper. Any policy violation strips content; any internal
    error returns '' — never passes raw through (SPEC-06 fail-closed contract)."""
    try:
        return nh3.clean(html, tags=_ALLOWED_TAGS, attribute_filter=_attribute_filter,
                         link_rel="noopener noreferrer nofollow", url_schemes={"http","https"})
    except Exception:
        return ""
```
render/__init__.py: docstring-only (package marker, SPEC-05 transport/ pattern).
img src note: the filter restricts to http(s); `data:` URIs die at the filter
(battery case).

## Task 2 — tests/test_sanitize.py (the XSS battery, spec §6)

Minimum 18 probes, each its own test (parametrize where natural):
1. `<script>alert(1)</script>` stripped entirely
2. `<iframe src=...>` stripped
3. `<img onerror=>` handler stripped, img kept
4. `javascript:` URL → href stripped, link text kept
5. `file://` link → stripped
6. `data:text/html,<script>` URI → stripped
7. Nested-tag smuggling (`<scr<script>ipt>`) → neutralized
8. mXSS fragment (`<svg><p>` style mutations) → sanitized output re-sanitizes
   identically (idempotence probe)
9. `style=` attribute stripped (no inline styles — SP2 emits classes)
10. `form`/`input`/`button` stripped (not in allowlist)
11. `onclick`/`onmouseover` on <a> stripped
12. http link PASSES with href intact
13. https link PASSES + rel="noopener noreferrer nofollow" added
14. All allowlist tags survive (h1..h6, lists, table, pre/code, em/strong/del, a, img)
15. **Fail-closed**: sanitize_html(None) == "" (no raise)
16. Fail-closed: sanitize_html(non-str garbage e.g. 123) == ""
17. Fail-closed: a filter that would raise (simulate via monkeypatched nh3.clean
    raising) → "" — probe the except path explicitly
18. Idempotence: sanitize_html(sanitize_html(x)) == sanitize_html(x) for the whole
    probe corpus (mutation-XSS resistance)

Falsifier (required): temporarily narrow _ALLOWED_TAGS to exclude "table" → test 14
must FAIL. Restore, re-run green.

## Verification (paste ALL, real runs)

```
.venv/bin/python -m pytest tests/test_sanitize.py -q
.venv/bin/python -m pytest tests/ --collect-only -q 2>&1 | tail -1
.venv/bin/python -m ruff check render/ tests/test_sanitize.py
.venv/bin/pyright render/sanitize.py 2>&1 | tail -1
```

New files: ruff 0 / pyright 0 required (greenfield, zero-excuse — SPEC-05 SP1 standard).

## COMPLETENESS
- [ ] Both files + __init__; corrected API used (no url_policy)
- [ ] 18+ probes incl. 3 fail-closed + idempotence
- [ ] Falsifier run and SAID
- [ ] 4 outputs pasted
- [ ] Deviations flagged
