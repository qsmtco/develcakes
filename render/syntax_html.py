# render/syntax_html.py — Pygments → class-based HTML spans (SPEC-06 R2A §2).
#
# Port of utils/syntax_highlight.py (Pango colors) to HTML: token colors
# become CSS class names; the stylesheet lives in the chat surface (SP3).
# NO inline styles (spec §2). Pygments optional — degrade to escaped plain
# text. Pure function, no GTK imports.

import html

try:
    from pygments.lexers import get_lexer_by_name
    from pygments.token import Token
    _PYGMENTS_AVAILABLE = True
except ImportError:  # pragma: no cover - environment-dependent
    _PYGMENTS_AVAILABLE = False

# Tokyo Night palette → semantic class names. Same key set as the Pango
# port's _TOKEN_COLORS; the color VALUES are defined by SP3's stylesheet,
# not here.
_TOKEN_CLASSES: dict = {
    Token.Keyword: "tok-kw",
    Token.Keyword.Constant: "tok-kw-const",
    Token.Keyword.Declaration: "tok-kw",
    Token.Keyword.Namespace: "tok-kw",
    Token.Keyword.Pseudo: "tok-kw",
    Token.Keyword.Reserved: "tok-kw",
    Token.Keyword.Type: "tok-type",
    Token.Name: "tok-name",
    Token.Name.Class: "tok-type",
    Token.Name.Exception: "tok-err",
    Token.Name.Function: "tok-fn",
    Token.Name.Decorator: "tok-decorator",
    Token.Name.Variable: "tok-text",
    Token.Name.Builtin: "tok-fn",
    Token.Name.Builtin.Pseudo: "tok-fn",
    Token.Literal: "tok-lit",
    Token.String: "tok-str",
    Token.String.Doc: "tok-str",
    Token.String.Affix: "tok-str",
    Token.String.Backtick: "tok-str",
    Token.String.Char: "tok-str",
    Token.String.Double: "tok-str",
    Token.String.Escape: "tok-escape",
    Token.String.Heredoc: "tok-str",
    Token.String.Interpol: "tok-escape",
    Token.String.Other: "tok-str",
    Token.String.Regex: "tok-escape",
    Token.String.Single: "tok-str",
    Token.String.Symbol: "tok-str",
    Token.Number: "tok-num",
    Token.Number.Bin: "tok-num",
    Token.Number.Float: "tok-num",
    Token.Number.Hex: "tok-num",
    Token.Number.Integer: "tok-num",
    Token.Number.Integer.Long: "tok-num",
    Token.Number.Oct: "tok-num",
    Token.Operator: "tok-op",
    Token.Operator.Word: "tok-kw",
    Token.Punctuation: "tok-op",
    Token.Comment: "tok-com",
    Token.Comment.Multiline: "tok-com",
    Token.Comment.Preproc: "tok-pre",
    Token.Comment.PreprocFile: "tok-pre",
    Token.Comment.Single: "tok-com",
    Token.Comment.Special: "tok-com",
    Token.Generic: "tok-text",
    Token.Generic.Deleted: "tok-err",
    Token.Generic.Emph: "tok-text",
    Token.Generic.Error: "tok-err",
    Token.Generic.Heading: "tok-fn",
    Token.Generic.Inserted: "tok-lit",
    Token.Generic.Strong: "tok-text",
    Token.Generic.Subheading: "tok-fn",
    Token.Generic.Traceback: "tok-err",
    Token.Token: "tok-text",
    Token.Text: "tok-text",
}

_DEFAULT_CLASS = "tok-text"


def _token_class(ttype) -> str:
    """Walk the token type hierarchy upward to find a mapped class."""
    while ttype:
        if ttype in _TOKEN_CLASSES:
            return _TOKEN_CLASSES[ttype]
        ttype = ttype.parent
    return _DEFAULT_CLASS


def highlight_html(code: str, lang: str = "") -> str:
    """Convert source code to HTML with token-class spans.

    Token TEXT is escaped here; the emitted <span class=...> wrappers are
    this module's own output — callers must NOT escape the return value.

    No lexer (unknown lang / pygments missing / empty lang) → escaped
    plain text (the Pango port's degrade contract).
    """
    if not code:
        return ""
    if not _PYGMENTS_AVAILABLE:  # pragma: no cover - environment-dependent
        return html.escape(code)

    lang_lower = lang.lower().strip()
    if not lang_lower:
        return html.escape(code)
    try:
        lexer = get_lexer_by_name(lang_lower)
    except Exception:  # noqa: BLE001 — any lexer-lookup failure degrades to
        # plain escaped code (Pango-port contract; ClassNotFound and friends)
        return html.escape(code)

    result: list[str] = []
    for ttype, value in lexer.get_tokens(code):
        if not value:
            continue
        cls = _token_class(ttype)
        escaped_val = html.escape(value)
        if cls != _DEFAULT_CLASS:
            result.append(f'<span class="{cls}">{escaped_val}</span>')
        else:
            result.append(escaped_val)
    return "".join(result)
