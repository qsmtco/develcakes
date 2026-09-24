# render/sanitize.py — fail-closed HTML sanitization (SPEC-06 R2 Phase A §2).
#
# CONTRACT (fail-closed, SPEC-06): unsafe HTML must never reach WebKit because
# of this module. Anything not explicitly allowed is stripped; ANY internal
# error returns "" — the raw input is never passed through.

import nh3

# Markdown-render vocabulary (spec §2): headings, lists, code, emphasis,
# links, tables, images. No script/iframe/style/form anywhere.
_ALLOWED_TAGS = frozenset({
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "p", "br", "hr", "blockquote",
    "pre", "code", "em", "strong", "del",
    "a", "img",
    "table", "thead", "tbody", "tr", "th", "td",
})

# Attributes allowed through _attribute_filter. href/src are additionally
# scheme-restricted to http(s) inside the filter; src data:/file: URIs die there.
_SAFE_ATTRS = frozenset({"href", "title", "alt", "src"})


def _attribute_filter(element: str, attribute: str, value: str) -> str | None:
    """Per-(element, attribute, value) gate — nh3 0.3.7 API (no url_policy).

    Returns the value to keep, or None to strip.

    nh3 0.3.7 behavior (probe-verified 2026-09-24): ammonia injects link_rel
    BEFORE this filter runs, so "rel" must pass or the injection dies. No
    other rel exists in this policy (_SAFE_ATTRS has none), so allowing it
    here only ever admits ammonia's own "noopener noreferrer nofollow".
    """
    if attribute == "rel":
        return value
    if attribute in ("href", "src"):
        if value.startswith(("http://", "https://")):
            return value
        return None
    if attribute in _SAFE_ATTRS:
        return value
    return None


def sanitize_html(html: str) -> str:
    """Sanitize untrusted HTML. Fail-closed: returns "" on any internal error.

    Scheme policy: href/src restricted to http(s) — javascript:, file:,
    data:, and relative URIs are stripped (relative dies at the filter;
    url_schemes is the second layer).
    """
    try:
        return nh3.clean(
            html,
            tags=_ALLOWED_TAGS,
            attribute_filter=_attribute_filter,
            link_rel="noopener noreferrer nofollow",
            url_schemes={"http", "https"},
        )
    except Exception:  # noqa: BLE001 — deliberate: fail-closed contract means
        # ANY error shape (TypeError from None, Rust panics surfaced as
        # exceptions, filter bugs) must yield "", never raw passthrough.
        return ""
