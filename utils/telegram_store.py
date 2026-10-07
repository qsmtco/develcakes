# utils/telegram_store.py — Telegram bridge config persistence (SPEC-15 SP2).
#
# Pure functions — no GTK, no state, no network. Mirrors utils/providers_store
# discipline: <config_dir>/telegram_bridge.yaml, ATOMIC write (.tmp → rename),
# chmod 0o600 (the bot token is a credential), tolerant load, and the token is
# NEVER logged (redact_log_preview from transport.telegram scrubs /bot<token>).
#
# Manifest:
#   - Reads:  <config_dir>/telegram_bridge.yaml
#   - Writes: <config_dir>/telegram_bridge.yaml (atomic, chmod 0o600)
#   - Network: none
#   - Imports: stdlib (yaml if available, else json); transport.telegram for
#     the redactor only (no transport instantiation).

from __future__ import annotations

import json
import logging
import os
from typing import Any

from transport.telegram import redact_log_preview

_logger = logging.getLogger(__name__)

_FILENAME = "telegram_bridge.yaml"

# Shape returned by load_bridge_config(); chat_id is int | None (unpaired).
_DEFAULTS: dict[str, Any] = {
    "bot_token": "",
    "chat_id": None,
    "paired_handle": "",
}


def get_bridge_path() -> str:
    """Absolute path to telegram_bridge.yaml under the config dir. Does NOT
    create the file."""
    from utils.config import get_config_dir
    return os.path.join(get_config_dir(), _FILENAME)


def load_bridge_config() -> dict[str, Any]:
    """Read telegram_bridge.yaml → {bot_token, chat_id, paired_handle}.

    Missing/malformed file → defaults (empty token, unpaired). The token is
    never logged: parse warnings pass text through redact_log_preview.
    """
    path = get_bridge_path()
    if not os.path.isfile(path):
        return dict(_DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        _logger.warning("telegram_store: failed to read: %s", redact_log_preview(str(e)))
        return dict(_DEFAULTS)
    try:
        import yaml

        raw = yaml.safe_load(text)
    except ImportError:
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            _logger.warning("telegram_store: parse error: %s", redact_log_preview(str(e)))
            return dict(_DEFAULTS)
    except Exception as e:  # noqa: BLE001 — any YAML error is a malformed-file case
        _logger.warning("telegram_store: YAML parse error: %s", redact_log_preview(str(e)))
        return dict(_DEFAULTS)

    if not isinstance(raw, dict):
        if raw is not None:
            _logger.warning("telegram_store: expected mapping, got %s", type(raw).__name__)
        return dict(_DEFAULTS)
    chat_id = raw.get("chat_id")
    if chat_id is not None:
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            chat_id = None
    return {
        "bot_token": str(raw.get("bot_token") or ""),
        "chat_id": chat_id,
        "paired_handle": str(raw.get("paired_handle") or ""),
    }


def save_bridge_config(bot_token: str, chat_id: int | None = None,
                       paired_handle: str = "") -> None:
    """Write the bridge config atomically with mode 0o600. Creates the parent
    dir (0700) if needed. The token is NEVER logged."""
    path = get_bridge_path()
    parent = os.path.dirname(path)
    parent_existed = os.path.isdir(parent)
    if not parent_existed:
        os.makedirs(parent, exist_ok=True)

    data = {
        "bot_token": bot_token or "",
        "chat_id": chat_id,
        "paired_handle": paired_handle or "",
    }
    tmp_path = path + ".tmp"
    try:
        try:
            import yaml
            text = yaml.dump(data, default_flow_style=False, allow_unicode=True)
        except ImportError:
            text = json.dumps(data, indent=2, ensure_ascii=False)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
        os.rename(tmp_path, path)
    except Exception:
        if os.path.isfile(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise

    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # non-POSIX filesystem
    # The parent holds the credential file, so tighten it to 0700 on EVERY
    # save — not just when created. A pre-existing 0755 dir (e.g. created by
    # an earlier install or another tool) would otherwise leave the token
    # file readable to other users of the machine.
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass  # non-POSIX filesystem
    # Deliberately log NOTHING here — even the fact of a save, with the token
    # in scope, is a redaction hazard. Callers log their own non-secret events.


def is_configured(data: dict[str, Any]) -> bool:
    """True when both the token and a paired chat_id are present."""
    return bool(data.get("bot_token")) and data.get("chat_id") is not None