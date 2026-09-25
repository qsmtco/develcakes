# render/sanitize.py — fail-closed HTML sanitization (SPEC-06 R2 Phase A §2).
#
# CONTRACT (fail-closed, SPEC-06): unsafe HTML must never reach WebKit because
# of this module. Anything not explicitly allowed is stripped; ANY internal
# error returns "" — the raw input is never passed through.

import re

import nh3

# pyo3 (nh3's runtime) raises PanicException for Rust-side assertion panics.
# It is NOT an Exception subclass — the fail-closed catch must name it
# explicitly or a panic would propagate instead of yielding "".
_EXC_TYPES: tuple = (Exception,)
try:
    from pyo3_runtime import PanicException  # type: ignore[import-not-found]

    _EXC_TYPES = (Exception, PanicException)
except ImportError:  # pragma: no cover - runtime-dependent
    pass

# Markdown-render vocabulary (spec §2): headings, lists, code, emphasis,
# links, tables, images. No script/iframe/style/form anywhere.
_ALLOWED_TAGS = frozenset({
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "p", "br", "hr", "blockquote",
    "pre", "code", "span", "em", "strong", "del",
    "a", "img",
    "table", "thead", "tbody", "tr", "th", "td",
})

# Per-tag attribute admission. NOTE (probe-verified 2026-09-24): passing
# `attributes` REPLACES ammonia's entire default tag_attributes map, so this
# map re-admits every legacy attribute (href/src/title/alt) explicitly.
# "class" is admitted ONLY for the tags the chat surface styles; the filter
# below owns the class VALUE gate.
#
# NEVER add "rel" for "a" here while link_rel is set — ammonia raises
# ValueError ("rel" managed by link_rel; the filter still sees the injected
# value, which is how SP1's rel gate works).
# NEVER add "class" via ammonia's allowed_classes param — combining
# allowed_classes with an attributes map admitting class for the same tag
# triggers an ammonia Rust assertion PANIC (PanicException, not catchable as
# Exception). The filter gate is the only safe mechanism.
_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title", "class"},
    "img": {"src", "alt", "title"},
    "pre": {"class"},
    "code": {"class"},
    "span": {"class"},
    "ul": {"class"},
    "li": {"class"},
}

# Class-token allowlist (chat-surface styling vocabulary, SP2/SP3 contract).
# Whole class attribute is split on whitespace; each token must match.
_CLASS_TOKEN_RE = re.compile(r"^(?:terminal|task-list|task-checked|(?:tok|lang)-[a-z0-9+#_-]*)$")

_SAFE_ATTRS = frozenset({"href", "title", "alt", "src"})


def _attribute_filter(element: str, attribute: str, value: str) -> str | None:
    """Per-(element, attribute, value) gate — nh3 0.3.7 API (no url_policy).

    Returns the value to keep, or None to strip.

    nh3 0.3.7 behavior (probe-verified 2026-09-24): ammonia injects link_rel
    BEFORE this filter runs, so the injection dies without a rel pass — but
    this filter OWNS the rel gate: only the EXACT injected value
    ("noopener noreferrer nofollow") passes. Any author-supplied rel
    (e.g. rel="opener") is stripped here, closing the reverse-tabnabbing
    hole — this does NOT rely on ammonia's attribute allowlist.

    Scheme check (SP1 FIX A): case-normalized test, ORIGINAL value returned.

    Class gate (SP3 ruling): class arrives as ONE space-separated string
    (probe-verified); each token is individually allowlisted, allowed tokens
    are re-joined, and the attribute is stripped if none survive.
    """
    if attribute == "class":
        toks = [t for t in value.split() if _CLASS_TOKEN_RE.fullmatch(t)]
        return " ".join(toks) if toks else None
    if attribute == "rel":
        return value if value == "noopener noreferrer nofollow" else None
    if attribute in ("href", "src"):
        if value.lower().startswith(("http://", "https://")):
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

    Class policy (SP3 ruling): tok-*/lang-* and surface-state classes
    (terminal/task-list/task-checked) survive on code/span/pre/ul/li/a/img;
    everything else is stripped token-wise.
    """
    try:
        return nh3.clean(
            html,
            tags=_ALLOWED_TAGS,
            attributes=_ATTRIBUTES,
            attribute_filter=_attribute_filter,
            link_rel="noopener noreferrer nofollow",
            url_schemes={"http", "https"},
        )
    except _EXC_TYPES:
        # Fail-closed contract: ANY error shape (TypeError from None, Rust
        # panics via PanicException, filter bugs) must yield "", never raw
        # passthrough. (Not a blind Exception catch — _EXC_TYPES is an
        # explicit tuple; PanicException is named for the panic case.)
        return ""
