# utils/speech_config.py — talk-tool speech endpoint persistence.
#
# Mirrors utils/stt_config.py: <config_dir>/speech.yaml, atomic write, mode 0o600.
# The token is a credential. Never log it.
#
# Manifest:
#   Reads:  <config_dir>/speech.yaml
#   Writes: <config_dir>/speech.yaml (atomic, chmod 0o600)
#   Network: none

from __future__ import annotations

import json
import logging
import os

_logger = logging.getLogger(__name__)

_FILENAME = "speech.yaml"
_KEYS = ("endpoint", "model", "token")
_FORMATS = ("mp3", "pcm")


def get_speech_path() -> str:
    """Absolute path to speech.yaml. Does not create it."""
    from utils.config import get_config_dir
    return os.path.join(get_config_dir(), _FILENAME)


def _read_raw(path: str) -> tuple[dict | None, str | None]:
    """Return (mapping, error). Missing file is (None, None)."""
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return None, f"failed to read: {e}"
    try:
        from utils.yaml_load import safe_load_yaml
        raw = safe_load_yaml(text)
    except ImportError:
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            return None, f"parse error: {e}"
    except Exception as e:  # noqa: BLE001 — malformed YAML is a degraded store
        return None, f"YAML parse error: {e}"
    if raw is None:
        return None, "empty file"
    if not isinstance(raw, dict):
        return None, f"expected mapping, got {type(raw).__name__}"
    return raw, None


def load_speech_config() -> tuple[dict | None, str | None]:
    """(config, error). Missing file is (None, None) — not configured.

    A present file must contain non-blank endpoint, model, and token.
    ``voice`` is optional: ``""`` when absent or blank (providers that
    don't need one). A present-but-non-string voice is a store error.
    A missing or blank format defaults to mp3 (legacy
    stores). A present-but-invalid format is a store error, not a silent
    default. The returned dict includes the token. Callers must not log it.
    """
    raw, error = _read_raw(get_speech_path())
    if error is not None or raw is None:
        return None, error
    out: dict[str, str] = {}
    for key in _KEYS:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            return None, f"{key} is missing or blank"
        out[key] = value.strip()
    voice = raw.get("voice")
    if voice is None:
        out["voice"] = ""
    elif isinstance(voice, str):
        out["voice"] = voice.strip()
    else:
        return None, f"voice must be a string, got {type(voice).__name__}"
    fmt = raw.get("format")
    if fmt is None or (isinstance(fmt, str) and not fmt.strip()):
        out["format"] = _FORMATS[0]
    else:
        fmt = fmt.strip() if isinstance(fmt, str) else fmt
        if fmt not in _FORMATS:
            return None, f"format must be one of {', '.join(_FORMATS)}; got {fmt!r}"
        out["format"] = fmt
    return out, None


def save_speech_config(
    endpoint: str, model: str, token: str, voice: str = "", fmt: str = _FORMATS[0]
) -> None:
    """Persist endpoint, model, token, voice, and format. Blank token keeps
    a stored token. Blank voice is allowed (providers that don't need one).

    Raises ValueError on a blank endpoint or model, an unknown format, or
    a blank token when nothing is stored yet; TypeError on a non-string
    voice. OSError propagates. The previous file survives a failed write.
    """
    if not isinstance(voice, str):
        raise TypeError(
            f"speech_config: voice must be a string, got {type(voice).__name__}"
        )
    if not isinstance(fmt, str) or fmt.strip() not in _FORMATS:
        raise ValueError(
            f"speech_config: format must be one of {', '.join(_FORMATS)}, got {fmt!r}"
        )
    fmt = fmt.strip()
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError(
            f"speech_config: endpoint must be a non-blank string, got {endpoint!r}"
        )
    if not isinstance(model, str) or not model.strip():
        raise ValueError(
            f"speech_config: model must be a non-blank string, got {model!r}"
        )
    if not isinstance(token, str):
        raise TypeError(
            f"speech_config: token must be a string, got {type(token).__name__}"
        )
    endpoint = endpoint.strip()
    model = model.strip()
    token = token.strip()
    if not token:
        existing, _error = load_speech_config()
        if existing is None or not existing.get("token"):
            raise ValueError("speech_config: token is required on the first save")
        token = existing["token"]

    path = get_speech_path()
    parent = os.path.dirname(path)
    parent_existed = os.path.isdir(parent)
    if not parent_existed:
        os.makedirs(parent, exist_ok=True)

    data = {"endpoint": endpoint, "model": model, "token": token, "format": fmt}
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
        pass
    if not parent_existed:
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
    host = endpoint.split("/")[2] if "://" in endpoint else endpoint
    _logger.info("speech_config: saved speech endpoint host %s", host)
