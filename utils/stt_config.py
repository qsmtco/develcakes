# utils/stt_config.py — STT model setting persistence (SPEC-37).
#
# Pure functions — no GTK, no state, no network. Mirrors utils/telegram_store
# discipline: <config_dir>/stt.yaml, ATOMIC write (.tmp → rename), chmod 0o600,
# tolerant load. The model id itself is not a credential, but the config dir
# holds credential files, so the same owner-only discipline is kept (uniform
# with providers.yaml / telegram_bridge.yaml).
#
# Manifest:
#   - Reads:  <config_dir>/stt.yaml
#   - Writes: <config_dir>/stt.yaml (atomic, chmod 0o600)
#   - Network: none
#   - Imports: stdlib only at module level (yaml/json lazy, utils.config lazy)
#
# Environment:
#   STT_MODEL — non-blank value overrides stt.yaml and the default for THIS
#               process (debug/testing); it is never written back to disk.

from __future__ import annotations

import json
import logging
import os

_logger = logging.getLogger(__name__)

_FILENAME = "stt.yaml"

DEFAULT_STT_MODEL = "microsoft/mai-transcribe-2"

# Measured OpenRouter audio-transcription choices (PM session 2026-10-16).
# (model_id, human label) — the label is what the settings dropdown shows.
STT_MODEL_CHOICES: list[tuple[str, str]] = [
    ("microsoft/mai-transcribe-2", "keyword biasing + diarization (default)"),
    ("mistralai/voxtral-mini-3b-2507", "cheapest accurate"),
    ("mistralai/voxtral-small-24b-2507-stt", "larger voxtral"),
    ("openai/gpt-4o-transcribe", "accurate"),
    ("openai/whisper-large-v3", "cheap; number-format quirks"),
]

# Models measured to accept provider.options.azure.phraseList. Anything not
# listed here must never receive the azure options block — OpenRouter 400s or
# silently drops it (SPEC-37).
_BIAS_SUPPORTED_MODELS = frozenset({"microsoft/mai-transcribe-2"})


def get_stt_path() -> str:
    """Absolute path to stt.yaml under the config dir. Does NOT create it."""
    from utils.config import get_config_dir
    return os.path.join(get_config_dir(), _FILENAME)


def _resolve_stored_model(path: str) -> tuple[str, str | None]:
    """Read the stored model from `path` → (model, error).

    error is None on a clean read — including a missing file (first-run
    state, default, no warning). Any malformed/degraded store returns
    (DEFAULT_STT_MODEL, reason) so callers choose: fall back silently
    (hot path) or surface the degradation (settings UI).
    """
    if not os.path.isfile(path):
        return DEFAULT_STT_MODEL, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return DEFAULT_STT_MODEL, f"failed to read: {e}"
    try:
        from utils.yaml_load import safe_load_yaml

        raw = safe_load_yaml(text)
    except ImportError:
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            return DEFAULT_STT_MODEL, f"parse error: {e}"
    except Exception as e:  # noqa: BLE001 — any YAML error is a malformed-file case
        return DEFAULT_STT_MODEL, f"YAML parse error: {e}"

    if not isinstance(raw, dict):
        if raw is not None:
            return DEFAULT_STT_MODEL, f"expected mapping, got {type(raw).__name__}"
        return DEFAULT_STT_MODEL, None  # empty file → first-run default
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip():
        return DEFAULT_STT_MODEL, f"{type(model).__name__} model value {model!r}"
    return model.strip(), None


def get_stt_model() -> str:
    """Resolve the dictation model: env STT_MODEL → stt.yaml `model` → default.

    Never raises. A missing file is first-run state (default, no warning);
    a present-but-blank, non-string, or malformed value falls back to the
    default with a WARNING — a degraded store must not silently pick a model.
    """
    env_raw = os.environ.get("STT_MODEL")
    if env_raw is not None and env_raw.strip():
        return env_raw.strip()

    path = get_stt_path()
    model, error = _resolve_stored_model(path)
    if error is not None:
        _logger.warning(
            "stt_config: %s unusable (%s) — using default %s",
            path, error, DEFAULT_STT_MODEL,
        )
    return model


def load_stt_model_state() -> tuple[str, str | None]:
    """(model, error) for the settings UI — same resolution as get_stt_model.

    error is None on a clean read (env override, missing file, valid yaml);
    otherwise a short human-readable reason the stored value was rejected.
    The settings page renders the section inert when error is not None
    (SPEC-37: honest UI — no dead controls on a degraded store).
    """
    env_raw = os.environ.get("STT_MODEL")
    if env_raw is not None and env_raw.strip():
        return env_raw.strip(), None
    return _resolve_stored_model(get_stt_path())


def set_stt_model(model_id: str) -> None:
    """Persist the dictation model to stt.yaml (atomic write, mode 0o600).

    Creates the parent dir (0700) if needed. Raises ValueError on blank or
    non-string input; OSError propagates if the write fails (callers surface
    it). The previous file always survives a failed write.
    """
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError(
            f"stt_config: model id must be a non-blank string, got {model_id!r}"
        )
    model_id = model_id.strip()

    path = get_stt_path()
    parent = os.path.dirname(path)
    parent_existed = os.path.isdir(parent)
    if not parent_existed:
        os.makedirs(parent, exist_ok=True)

    data = {"model": model_id}
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
        # Clean up tmp on failure — the previous file must stay intact.
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
    if not parent_existed:
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass  # non-POSIX filesystem
    _logger.info("stt_config: saved dictation model %s", model_id)


def is_bias_supported(model_id: object) -> bool:
    """True when `model_id` is measured to accept the Azure phraseList option.

    Custom/unknown models → False: they must never receive
    provider.options.azure (OpenRouter 400s or silently drops it).
    """
    return (
        isinstance(model_id, str) and model_id.strip() in _BIAS_SUPPORTED_MODELS
    )
