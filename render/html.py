# render/html.py — markdown → sanitizer-ready HTML (SPEC-06 R2A §2).
#
# PORT OF utils/markdown.py (inline) + utils/block_parser.py (blocks) with an
# INVERTED escaping contract:
#
#   Pango path:  caller escapes FIRST (escape_for_pango), then format_markdown.
#   HTML path:   markdown_to_html takes RAW markdown — ESCAPING IS THE
#                EMITTER'S JOB. Every text node passes html.escape() before
#                tag emission; no raw input reaches the output. The sanitizer
#                (render/sanitize.py) is defense-in-depth, not the first line.
#
# Order of operations mirrors utils/markdown.py: code-span placeholders →
# bold/italic/strike → shielded anchor placeholders → autolink → restore.
# (Pango Step 3b href-shielding is dropped: raw input here has no pre-existing
# href="..." attributes to double-link; anchors are still shielded so the
# autolink pass can't re-link URLs inside freshly emitted href attributes.)
#
# NO img is ever emitted (register ruling): markdown images render as
# "[alt]" text. js/data/file/relative link URLs render as plain text —
# the sanitizer would strip their href downstream; don't emit what dies.
#
# Pure functions; zero UI imports.

import html
import re

from render.syntax_html import highlight_html

_CODE_PLACEHOLDER_RE = re.compile(r"\x00CODE(\d+)\x00")
_ANCHOR_PLACEHOLDER_RE = re.compile(r"\x00ANCHOR(\d+)\x00")
_ZWSP = "\u200b"

# Port of utils/markdown.py _AUTO_LINK_RE (two alternatives: scheme URLs,
# bare hosts). Operates on ESCAPED text, same as the Pango path.
# Bare-host alternative KEEPS the & exclusion (the invented-https rule must
# not swallow "a & b" — see _AUTO_LINK_RE history in utils/markdown.py).
# The scheme-URL alternative now ALLOWS & (FIX C, audit): query strings
# (…?a=1&b=2) must link whole. Trailing-punct stripping still runs.
_AUTO_LINK_RE = re.compile(
    r"(?<![a-zA-Z0-9/:=&;])"
    r"([a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"'`\[\]()]+)"
    r"|"
    r"(?<![a-zA-Z0-9/:=&;])"
    r"(?<![\"'])"
    r"((?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s<>\"'`\[\]()&]+)?)",
    re.IGNORECASE,
)

_TRAILING_PUNCT = frozenset(".,;:!?")

# Schemes emittable as href. mailto: etc. render as plain text here (the
# Pango path warned; this pipeline STRIPS — sanitizer policy is http(s)-only).
_ALLOWED_LINK_SCHEMES: frozenset[str] = frozenset({"http", "https"})

_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def _link_scheme_ok(url: str) -> bool:
    """True if url's scheme is http/https — case-INSENSITIVE (SP1 FIX A mirror).

    Relative URLs (no scheme) → False: the chat surface has no base URL.
    """
    if not url:
        return False
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
        return False
    scheme = url.split(":", 1)[0].lower()
    return scheme in _ALLOWED_LINK_SCHEMES


def _strip_trailing_punct(url: str) -> str:
    """Strip common trailing punctuation from an auto-detected URL."""
    while url and url[-1] in _TRAILING_PUNCT:
        url = url[:-1]
    return url


# ── Step 1 machinery: inline code spans (port of Pango Step 1) ───────────

def _parse_code_span(text: str) -> tuple[str, int] | None:
    """GFM backtick code span at start of text → (content, num_backticks)."""
    m = re.match(r"^`+", text)
    if not m:
        return None
    num = len(m.group(0))
    rest = text[num:]
    closer = re.compile(rf"(?<=[^`])`{{{num}}}(?=[^`]|$)")
    m2 = closer.search(rest)
    if m2:
        return rest[: m2.start()], num
    return None


def _collect_code_spans(t: str) -> tuple[str, list[str]]:
    """Scan text; replace code spans with null-byte placeholders.

    Returns (protected_text, collected_spans). Fenced blocks (3+ backticks
    with a closing fence) pass through untouched — block_parser handles them.
    """
    result_parts: list[str] = []
    code_spans: list[str] = []
    i = 0
    while i < len(t):
        chunk = t[i:]
        if chunk.startswith("> "):
            result_parts.append(t[i])
            i += 1
            continue
        if chunk.startswith("```") and len(chunk) >= 4 and chunk[3] not in ("`", "'"):
            num = len(chunk) - len(chunk.lstrip("`"))
            rest = chunk[num:]
            fence = "`" * num
            close_pos = rest.find("\n" + fence)
            if close_pos >= 0:
                block_end = num + close_pos + 1 + num
                result_parts.append(t[i : i + block_end])
                i += block_end
                continue
        parsed = _parse_code_span(chunk)
        if parsed is not None:
            content, num = parsed
            code_spans.append(content)
            result_parts.append(f"\x00CODE{len(code_spans) - 1}\x00")
            i += num + len(content) + num
        else:
            result_parts.append(t[i])
            i += 1
    return "".join(result_parts), code_spans


# ── Inline emitter (RAW text in → HTML out) ──────────────────────────────

def _inline(text: str) -> str:
    """Inline markdown → HTML. Takes RAW text; escapes every text node."""
    if not text:
        return ""

    # MED-10 port: cap input (ReDoS guard), same 100 KB as the Pango path.
    _MAX_INPUT_LEN = 100 * 1024
    if len(text) > _MAX_INPUT_LEN:
        text = text[:_MAX_INPUT_LEN] + "\n[... input truncated at 100 KB ...]"

    # ESCAPE FIRST — the contract inversion (see module docstring).
    text = html.escape(text)

    # Step 0a: isolate adjacent bold boundaries (ZWSP trick, MED-10 port).
    text = re.sub(r"\*\*(?=\*\*)", f"**{_ZWSP}", text)

    # Step 1: protect inline code spans (placeholder trick).
    text, code_spans = _collect_code_spans(text)

    # Step 1a: markdown images → [alt] text. NO img is ever emitted.
    text = re.sub(r"!\[([^\]]*)\]\((?:[^()]|\([^()]*\))+\)", r"[\1]", text)

    # Step 2: bold+italic, bold, italic, strike — Pango order.
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)([^*]+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"~~(.+?)~~", r"<del>\1</del>", text)

    # Step 3: [text](url) → shielded anchor placeholders.
    anchor_spans: list[str] = []

    def _resolve_code_in_label(m: re.Match) -> str:
        idx = int(m.group(1))
        if idx < len(code_spans):
            # Spans were collected from ALREADY-ESCAPED text (escape-first
            # contract) — emit as-is; re-escaping here produced &amp;amp;
            # (FIX A, audit).
            return f"<code>{code_spans[idx]}</code>"
        return m.group(0)

    def _link_replace(m: re.Match) -> str:
        label = m.group(1)
        url = m.group(2)
        label = _CODE_PLACEHOLDER_RE.sub(_resolve_code_in_label, label)
        if _link_scheme_ok(url):
            anchor = f'<a href="{url}">{label}</a>'
        else:
            # js:/data:/file:/relative → plain text (sanitizer would strip
            # the href; don't emit what dies downstream).
            anchor = f"<span>{label}</span>"
        anchor_spans.append(anchor)
        return f"\x00ANCHOR{len(anchor_spans) - 1}\x00"

    text = re.sub(r"\[([^\]]+)\]\(((?:[^()]|\([^()]*\))+)\)", _link_replace, text)

    # Step 4: bare-URL autolink (port of Pango Step 4).
    def _auto_link(m: re.Match) -> str:
        url = m.group(1) or m.group(2)
        if not url:
            return m.group(0)
        url = _strip_trailing_punct(url)
        if _link_scheme_ok(url):
            return f'<a href="{url}">{url}</a>'
        return m.group(0)

    text = _AUTO_LINK_RE.sub(_auto_link, text)

    # Step 5: restore code spans (already escaped at collection time — the
    # escape happened BEFORE collection, so no raw pass-through is possible).
    def _restore_code(m: re.Match) -> str:
        idx = int(m.group(1))
        if idx < len(code_spans):
            return f"<code>{code_spans[idx]}</code>"
        return m.group(0)

    text = _CODE_PLACEHOLDER_RE.sub(_restore_code, text)

    # Step 6: restore shielded anchors.
    def _restore_anchor(m: re.Match) -> str:
        idx = int(m.group(1))
        if idx < len(anchor_spans):
            return anchor_spans[idx]
        return m.group(0)

    text = _ANCHOR_PLACEHOLDER_RE.sub(_restore_anchor, text)

    # Step 7: drop the ZWSP.
    return text.replace(_ZWSP, "")


# ── Block emitter ─────────────────────────────────────────────────────────

def _emit_list_group(lines: list[str], ordered: bool) -> str:
    """Emit consecutive list lines as <ul>/<ol> with <li> children."""
    tag = "ol" if ordered else "ul"
    pattern = _ORDERED_RE if ordered else _BULLET_RE
    items = []
    for line in lines:
        m = pattern.match(line)
        items.append(f"<li>{_inline(m.group(1) if m else line)}</li>")
    return f"<{tag}>{''.join(items)}</{tag}>"


def _emit_text_block(content: str) -> str:
    """Emit a text segment: group list lines, else a <p> paragraph."""
    lines = content.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _BULLET_RE.match(line):
            group = []
            while i < len(lines) and _BULLET_RE.match(lines[i]):
                group.append(lines[i])
                i += 1
            out.append(_emit_list_group(group, ordered=False))
            continue
        if _ORDERED_RE.match(line):
            group = []
            while i < len(lines) and _ORDERED_RE.match(lines[i]):
                group.append(lines[i])
                i += 1
            out.append(_emit_list_group(group, ordered=True))
            continue
        if line.strip():
            out.append(f"<p>{_inline(line)}</p>")
        i += 1
    return "".join(out)


def _emit_block(seg: dict) -> str:
    """Emit one block_parser segment dict as HTML."""
    btype = seg["type"]
    if btype == "code":
        lang = seg.get("lang", "")
        body = highlight_html(seg["content"], lang)
        cls = f' class="lang-{lang}"' if lang else ""
        return f"<pre><code{cls}>{body}</code></pre>"
    if btype == "heading":
        level = min(max(int(seg.get("level", 1)), 1), 6)
        return f"<h{level}>{_inline(seg['content'])}</h{level}>"
    if btype == "quote":
        return f"<blockquote>{_inline(seg['content'])}</blockquote>"
    if btype == "terminal":
        return f'<pre class="terminal"><code>{html.escape(seg["content"])}</code></pre>'
    if btype == "table":
        head = "".join(f"<th>{_inline(h)}</th>" for h in seg["headers"])
        rows = "".join(
            "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
            for row in seg["rows"]
        )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"
    if btype == "task":
        items = []
        for line in seg["content"].split("\n"):
            checked = line.startswith("[x]")
            glyph = "[x]" if checked else "[ ]"
            label = line[3:].strip() if line.startswith("[") else line
            cls = ' class="task-checked"' if checked else ""
            items.append(f"<li{cls}>{glyph} {_inline(label)}</li>")
        return f'<ul class="task-list">{"".join(items)}</ul>'
    return _emit_text_block(seg["content"])


# ── Public API ────────────────────────────────────────────────────────────

def markdown_to_html(text: str) -> str:
    """RAW markdown → HTML. Escaping is this module's job (see module doc).

    Pipeline: block_parser.extract_blocks for structure, _inline for inline
    rules, highlight_html for fenced code.
    """
    if not text:
        return ""
    from utils.block_parser import extract_blocks

    return "".join(_emit_block(seg) for seg in extract_blocks(text))


def render_document(text: str) -> str:
    """THE composition entry point: markdown_to_html → sanitize_html.

    Every chat-surface call site uses this one function (SP4's guard pins it).
    """
    from render.sanitize import sanitize_html

    return sanitize_html(markdown_to_html(text))
