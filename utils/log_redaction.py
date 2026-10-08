# utils/log_redaction.py — logging redaction for the Telegram bot token (SPEC-15b A2).
#
# WHY: the bot token rides in the request URL PATH (/bot<token>/method). The
# httpx/httpcore libraries log that URL themselves at INFO/DEBUG, so with
# DEBUG on, every poll printed the raw token — redact_log_preview() (SPEC-15)
# only scrubs APP-AUTHORED strings and never sees those records.
#
# DEFENSE IN DEPTH: main.py floors httpx/httpcore to WARNING (the primary
# suppression); this filter is the belt — it rewrites record.msg AND every str
# in record.args through the EXISTING transport.telegram.redact_log_preview
# before any handler formats the record. Never drops a record; never raises.
#
# No second redactor: the scrub delegates to transport.telegram.redact_log_preview.

from __future__ import annotations

import logging
import re
from typing import Any

_logger = logging.getLogger(__name__)

# The httpx/httpcore loggers that emit request URLs.
_HTTP_LOGGER_NAMES = ("httpx", "httpcore", "httpcore.http11", "httpcore.connection")

# Same URL-path shape the transport redactor scrubs; applied here as a cheap
# always-on net (the authoritative scrub is redact_log_preview below).
_TOKEN_IN_PATH_RE = re.compile(r"/bot[^/\s\"']+")


def _scrub(value: str) -> str:
    """Rewrite a string through the EXISTING transport redactor.

    Imported lazily so this module has no import-time dependency on
    transport/ (avoids any cycle risk if transport ever imports utils/).
    Falls back to the local URL-path scrub if the import fails.
    """
    try:
        from transport.telegram import redact_log_preview
        return redact_log_preview(value)
    except Exception:  # noqa: BLE001 — never break logging for a redaction miss
        return _TOKEN_IN_PATH_RE.sub("/bot***", value)


class RedactingFilter(logging.Filter):
    """Rewrite record.msg/record.args through the token redactor.

    Contract:
    - ALWAYS returns True (a logging.Filter must never drop a record).
    - NEVER raises: any scrub failure leaves the record unchanged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = _scrub(record.msg)
            args = record.args
            if isinstance(args, tuple):
                record.args = tuple(
                    _scrub(a) if isinstance(a, str) else a for a in args
                )
            elif isinstance(args, dict):
                record.args = {
                    k: (_scrub(v) if isinstance(v, str) else v)
                    for k, v in args.items()
                }
        except Exception as e:  # noqa: BLE001 — redaction must never break logging
            _logger.debug("log redaction failed (record passed unchanged): %s", e)
        return True


def install() -> None:
    """Attach a single RedactingFilter to the httpx/httpcore loggers.

    Idempotent: re-installing does not stack filters (one per logger).
    """
    for name in _HTTP_LOGGER_NAMES:
        lg = logging.getLogger(name)
        if not any(isinstance(f, RedactingFilter) for f in lg.filters):
            lg.addFilter(RedactingFilter())
    _ = Any  # typing import kept for downstream annotation use


__all__ = ["RedactingFilter", "install"]