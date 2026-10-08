# tests/test_log_redaction.py — SPEC-15b Part A (token-log redaction).
#
# The httpx/httpcore libraries log the request URL at INFO/DEBUG; the bot token
# rides in the URL path (/bot<token>/...). These tests pin the redaction filter
# (utils/log_redaction.RedactingFilter) that scrubs those records. Pure logging,
# no GTK, no network.

import logging

from utils.log_redaction import RedactingFilter, install

TOKEN = "8741404818:AAFsuperSECRETtokenXYZ"
URL = f"https://api.telegram.org/bot{TOKEN}/getUpdates"


class _Capture(logging.Handler):
    """Collect emitted (formatted) lines."""

    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(self.format(record))


def _logger_with_filter(name: str) -> tuple[logging.Logger, _Capture]:
    lg = logging.getLogger(name)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    for h in list(lg.handlers):
        lg.removeHandler(h)
    cap = _Capture()
    cap.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(cap)
    lg.addFilter(RedactingFilter())
    return lg, cap


def test_token_in_msg_is_scrubbed():
    lg, cap = _logger_with_filter("test.redact.msg")
    lg.info("HTTP Request: POST %s", "x")  # warm (no-op)
    lg.info(f"POST {URL}")
    assert cap.lines
    joined = "\n".join(cap.lines)
    assert TOKEN not in joined, f"token leaked: {joined!r}"
    assert "/bot***" in joined


def test_token_in_args_only_is_scrubbed():
    lg, cap = _logger_with_filter("test.redact.args")
    lg.info("HTTP Request: POST %s", URL)
    joined = "\n".join(cap.lines)
    assert TOKEN not in joined, f"token leaked via args: {joined!r}"


def test_token_in_both_msg_and_args_is_scrubbed():
    lg, cap = _logger_with_filter("test.redact.both")
    lg.info("POST %s from %s", URL, f"origin {URL}")
    joined = "\n".join(cap.lines)
    assert TOKEN not in joined, f"token leaked: {joined!r}"


def test_filter_never_drops_record():
    lg, cap = _logger_with_filter("test.redact.keep")
    lg.warning("a benign line")
    assert cap.lines == ["a benign line"]


def test_filter_swallows_scrub_exception(monkeypatch):
    """If redaction raises, the record must still pass through unchanged."""
    import utils.log_redaction as lr

    def boom(_s):
        raise RuntimeError("scrub exploded")

    monkeypatch.setattr(lr, "_scrub", boom)
    lg, cap = _logger_with_filter("test.redact.boom")
    lg.warning("survives the boom %s", URL)
    assert cap.lines, "record was dropped when scrub raised"
    # Unchanged passthrough (no scrub) — the record still emitted.
    assert "survives the boom" in cap.lines[0]


def test_install_attaches_to_httpx_and_httpcore():
    install()
    for name in ("httpx", "httpcore"):
        assert any(isinstance(f, RedactingFilter)
                   for f in logging.getLogger(name).filters), name


def test_install_idempotent():
    install()
    install()
    for name in ("httpx", "httpcore"):
        n = sum(isinstance(f, RedactingFilter)
                for f in logging.getLogger(name).filters)
        assert n == 1, f"{name} got {n} filters (install not idempotent)"


def test_httpx_url_log_is_scrubbed_end_to_end():
    """Acceptance: with DEBUG on, a real httpx-style record emits no token."""
    install()
    lg = logging.getLogger("httpx")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    for h in list(lg.handlers):
        lg.removeHandler(h)
    cap = _Capture()
    cap.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(cap)
    lg.info('HTTP Request: POST %s "HTTP/1.1 200 OK"', URL)
    joined = "\n".join(cap.lines)
    assert TOKEN not in joined, f"token leaked from httpx log: {joined!r}"