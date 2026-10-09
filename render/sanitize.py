# render/sanitize.py — fail-closed HTML sanitization (SPEC-06 R2 Phase A §2).
#
# CONTRACT (fail-closed, SPEC-06): unsafe HTML must never reach WebKit because
# of this module. Anything not explicitly allowed is stripped; ANY internal
# error returns "" — the raw input is never passed through.

import re

import nh3

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
# `attributes` REPLACES ammonia's entire default tag_attributes map. DELTA vs
# ammonia defaults (BUG #6, register): width/height/hreflang/target/colspan/
# rowspan/usemap/ismap/enctype/etc. are now DROPPED — the SP2 emitter emits
# none of them, so nothing is lost today. REGISTER for future emitters
# (table alignment wants colspan/rowspan; these must be re-admitted here
# deliberately, with filter gates, if ever emitted).
# "class" is admitted ONLY for the tags the chat surface styles; the filter
# below owns the class VALUE gate.
#
# SP5c-1: `welcome-row` joins the vocabulary — the welcome row's stable
# CSS hook (SPEC-06 SP5c-1 constraint 1: text-only welcome + class hook
# for later styling). Additive allowlist entry; NO policy weakening (src
# stays http(s)-only, no new tags/attrs).
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
    # SP5c-1-audit round 3 (BUG #4 ruling, option a): block-level class hook
    # — the welcome row carries `welcome-row` on its emitted <p> (no span
    # wrapper). Additive, same precedent as span/pre/code/ul/li above; the
    # VALUE stays gated by the class-token allowlist (no policy weakening).
    "p": {"class"},
}

# Class-token allowlist (chat-surface styling vocabulary, SP2/SP3 contract).
# Whole class attribute is split on whitespace; each token must match.
# SP6 Phase 1 (supervisor tighten, audit finding): the suffix quantifier is `+`
# — bare "tok-"/"lang-" (empty suffix) are stripped. All real emitters produce
# non-empty suffixes (render/html.py lang-{lang} only when truthy; syntax_html
# tokens are hardcoded like tok-kw), so nothing legitimate is affected.
_CLASS_TOKEN_RE = re.compile(r"^(?:terminal|task-list|task-checked|welcome-row|(?:tok|lang)-[a-z0-9+#_-]+)$")

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

    FIX B (SP3 audit r2): the filter is SELF-FAIL-CLOSED. pyo3 does not
    propagate filter exceptions as errors — it logs them and RETAINS the
    attribute (probe-verified), so a raising filter would leak values past
    every gate above. The wrapper converts ANY internal failure (Exception
    or BaseException-derived) to None = strip. The outer catch in
    sanitize_html covers the different shape: ammonia's Rust-side PANIC
    (PanicException from nh3.clean itself, not from the filter).
    """
    try:
        return _attribute_filter_inner(element, attribute, value)
    except BaseException:  # noqa: BLE001 — fail-closed: strip, never retain
        # Sanitizing is not interrupt-critical: stripping on Ctrl-C is
        # acceptable; retaining an unvetted attribute is not.
        return None


def _attribute_filter_inner(element: str, attribute: str, value: str) -> str | None:
    """The gate logic proper — wrapped by _attribute_filter (FIX B)."""
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


# ── Agent-author policy (SPEC-13): the agent is the AUTHOR ──────────────
#
# Rich vocabulary for agent-authored HTML payloads (whole-message ```html
# fence). Security invariants UNCHANGED from sanitize_html: no
# script/iframe/object/embed/form-action, no event handlers, href/src
# http(s)-only, link_rel forced, fail-closed.
#
# <style> ELEMENT IS DELIBERATELY ABSENT (SP1 probe verdict, 2026-10-06,
# nh3 0.3.7 / ammonia 4.1.4): passing "style" in `tags=` PANICS the process
# unconditionally — ammonia has "style" in its default `clean_content_tags`
# and asserts `style appears in clean_content_tags and in tags at the same
# time` (pyo3_runtime.PanicException, NOT catchable as Exception). Leaving
# "style" OUT of tags strips the element AND its CSS content cleanly (no
# panic — probe-verified). Therefore per-message <style> blocks are a
# REGISTER item: SP1 relies on inline `style=` (CSS property allowlist
# below) + the surface's own classes. Do NOT add "style" here.
_AGENT_AUTHOR_TAGS = _ALLOWED_TAGS | frozenset({
    # containers
    "div", "section", "article", "header", "footer", "main", "aside", "nav",
    "figure", "figcaption", "details", "summary", "hgroup",
    # text/inline
    "b", "i", "u", "s", "small", "sub", "sup", "mark", "abbr", "cite", "q",
    "time", "wbr", "font", "label",
    # lists (def)
    "dl", "dt", "dd",
    # inert-without-JS interactive (Phosphor-style cards: details/summary
    # toggles NATIVELY, no JS). No <form> — form submissions navigate; keep
    # the surface non-navigating. No JS (webview JS stays off, SPEC-06 R2).
    "button",
    # media (src gated http(s) by the SAME url_schemes + attribute filter)
    "img", "video", "audio", "source", "picture",
    # vector (static; JS-off webview cannot script SVG events)
    "svg", "path", "circle", "rect", "line", "polyline", "polygon", "g",
    "defs", "stop", "use", "symbol",
})

# CSS properties an agent payload may set. Deny-by-omission kills every
# url()-bearing property (background/background-image/list-style-image/
# behavior/filter/…): nh3's filter_style_properties takes a SET of allowed
# property names and strips all others (probe-verified nh3 0.3.7 — passing a
# callable raises TypeError). A frozenset is accepted by nh3 (probe-verified);
# we still hand it a set at the call site to be explicit.
_AGENT_CSS_PROPERTIES = frozenset({
    "color", "background-color", "font-size", "font-family", "font-weight",
    "font-style", "line-height", "letter-spacing", "text-align",
    "text-decoration", "text-transform", "text-shadow", "white-space",
    "border", "border-color", "border-radius", "border-width", "border-style",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "width", "max-width", "min-width", "height", "max-height", "min-height",
    "display", "flex-direction", "flex-wrap", "gap", "justify-content",
    "align-items", "align-content", "opacity", "overflow", "box-sizing",
    "box-shadow", "cursor", "border-collapse", "vertical-align", "float",
    # ── SPEC-19 SP3 (§5): inline-capable chart/layout properties ──────────
    # The agent's static cards want gradients / filters / transforms / grid.
    # ★ §2 PROBE (nh3 0.3.7, 2026-10-08): filter_style_properties filters
    # property NAMES only — url() VALUES pass through VERBATIM. Adding
    # background/mask/border-image therefore re-opens url() reachability, and
    # that is SAFE because E1 (the compiled block-all-remote content filter,
    # SPEC-19 §3) BLOCKS the CSS-driven load in the transcript webview where
    # T2 cards render (probe: control background:url() +1 hit; filtered +0).
    # url() is INERT on T2 — the enforcement test lives in test_live_guard.py.
    # background shorthand (keeps gradients: linear/radial/conic-gradient)
    "background", "background-image", "background-size",
    "background-position", "background-repeat", "background-clip",
    "filter", "backdrop-filter",
    "transform", "transform-origin",
    "transition", "transition-property", "transition-duration",
    "transition-timing-function", "transition-delay",
    "object-fit", "object-position", "aspect-ratio",
    "position", "top", "right", "bottom", "left", "z-index", "inset",
    "grid-template-columns", "grid-template-rows", "grid-column", "grid-row",
    "grid-auto-flow", "grid-auto-columns", "grid-auto-rows", "grid-area",
    "place-items", "place-content", "place-self",
    "clip-path",
    "mask", "mask-image", "mask-size", "mask-position", "mask-repeat",
    "border-image", "border-image-source", "border-image-slice",
    "border-image-width", "border-image-outset", "border-image-repeat",
    "columns", "column-count", "column-width", "column-gap",
    "resize", "user-select", "pointer-events", "visibility",
    "content-visibility", "contain",
    "scroll-margin", "scroll-padding",
    # animation-*/@keyframes are DELIBERATELY ABSENT (SPEC-19 §5 SP3 amended:
    # T3-only — they require <style>, which nh3 can never pass).
})


def _agent_attributes_map() -> dict[str, set[str]]:
    """Per-tag attribute map for the agent-author policy.

    DELTA (probe-verified, same as sanitize_html's BUG #6 note): passing
    `attributes` REPLACES ammonia's entire default tag_attributes map, so
    width/height/colspan/rowspan/name must be listed DELIBERATELY or they
    are dropped. `rel` is NEVER listed — link_rel manages it (ammonia raises
    on a managed attribute; the filter's rel gate is what survives author
    overrides). `data-*` rides generic_attribute_prefixes, not this map.
    """
    admitted = {
        "style", "class", "id", "title", "alt", "src", "href",
        "width", "height", "colspan", "rowspan", "name",
    }
    return {tag: set(admitted) for tag in _AGENT_AUTHOR_TAGS}


# Presentation/inert attributes the author policy admits VERBATIM, beyond the
# markdown path's _SAFE_ATTRS (href/title/alt/src). width/height/colspan/
# rowspan/name are inert (no URL, no script) — the deliberate re-admission the
# attributes-map DELTA requires. href/src do NOT appear here: they stay behind
# the http(s) gate in _attribute_filter_inner.
#
# `name` (BUG#6): on <a> this is an ANCHOR TARGET — a document-fragment id,
# NOT a URL. URL-shaped values (e.g. name="foo") are therefore SAFE here:
# `name` is never dereferenced by the browser (navigation rides `href`, which
# IS gated); it only labels the fragment `#foo`. The one historical `<a
# name>`/`<img name>` form is inert in a JS-off webview, so no scheme gate is
# needed — admitting it verbatim cannot introduce a fetch or a navigation.
_AGENT_INERT_ATTRS = frozenset({"title", "alt", "width", "height", "colspan", "rowspan", "name"})


def _agent_attribute_filter(element: str, attribute: str, value: str) -> str | None:
    """Agent-author gate. Self-fail-closed like _attribute_filter (FIX B):
    pyo3 does not propagate filter exceptions — it retains the attribute —
    so ANY internal failure converts to None (strip)."""
    try:
        return _agent_attribute_filter_inner(element, attribute, value)
    except BaseException:  # noqa: BLE001 — fail-closed: strip, never retain
        return None


def _agent_attribute_filter_inner(element: str, attribute: str, value: str) -> str | None:
    """Author additions first, then delegate to the markdown gate.

    style  → admitted here; nh3 applies filter_style_properties (the CSS
             property-name allowlist) AFTER this returns the value.
    class/id → any value (author namespace — the author styles their own card).
    data-*   → any value (generic_attribute_prefixes also admits the name).
    inert presentation attrs → verbatim.
    href/src/rel/everything else → _attribute_filter (http(s)-only, rel gate).
    """
    if attribute == "style":
        return value
    if attribute in ("class", "id"):
        return value
    if attribute.startswith("data-"):
        return value
    if attribute in _AGENT_INERT_ATTRS:
        return value
    return _attribute_filter(element, attribute, value)


def sanitize_agent_html(html: str) -> str:
    """Agent-authored HTML payload (SPEC-13): author vocabulary + CSS
    property allowlist. Fail-closed identical to sanitize_html — ANY internal
    error (including the Rust-side PanicException shape) returns "".

    The agent is the AUTHOR here (untrusted TEXT does not travel this path —
    tool results, fetched content and user rows keep sanitize_html).
    """
    try:
        return nh3.clean(
            html,
            # sets (not tuples): nh3 requires set instances for these params
            # (probe-verified — a tuple raises TypeError). frozenset works too.
            tags=_AGENT_AUTHOR_TAGS,
            attributes=_agent_attributes_map(),
            attribute_filter=_agent_attribute_filter,
            filter_style_properties=_AGENT_CSS_PROPERTIES,
            generic_attribute_prefixes={"data-"},
            link_rel="noopener noreferrer nofollow",
            url_schemes={"http", "https"},
        )
    except BaseException:  # noqa: BLE001 — sanctioned fail-closed (see sanitize_html)
        # Widest net: nh3 failure shapes include pyo3 PanicException (Rust
        # assertion panics), which does NOT derive from Exception. "" on
        # Ctrl-C is acceptable; raw passthrough is not.
        return ""


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
    except BaseException:  # noqa: BLE001 — sanctioned fail-closed (see below)
        # Fail-closed contract: the WIDEST net is correct here, not lazy.
        # nh3's failure shapes include pyo3 PanicException (Rust-side
        # assertion panics), which does NOT derive from Exception — and the
        # import-guard tuple approach (SP3 audit BUG #1) was inert on this
        # box: pyo3_runtime is unimportable standalone, so it degraded to
        # (Exception,) and a panic would have propagated. Sanitizing is not
        # interrupt-critical: "" on Ctrl-C is acceptable; raw passthrough
        # is not.
        return ""
