# tests/test_stt_config.py — SPEC-37: configurable STT model.
#
# Unit only — no GTK, no network. Covers the utils/stt_config.py store
# (round-trip, env override, degraded-file fallbacks) and the utils/stt.py
# integration through _build_request_body with the real engine (model resolved
# per call; phraseList gated on is_bias_supported).
#
# Every test asserts exact values so a mutation (hardcoded model, removed env
# precedence, ungated phraseList) fails loudly rather than vacuously.

import json
import logging
import os
import stat

import pytest

from utils import stt_config
from utils.yaml_load import safe_load_yaml

BIAS_MODEL = "microsoft/mai-transcribe-2"
OTHER_KNOWN = "mistralai/voxtral-mini-3b-2507"
CUSTOM_MODEL = "acme/whisper-turbo-custom"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Isolated config dir + clean STT_* environment for every test."""
    monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
    monkeypatch.delenv("STT_MODEL", raising=False)
    monkeypatch.delenv("STT_PHRASES", raising=False)
    return tmp_path


def _yaml_path(cfg):
    return os.path.join(str(cfg), "stt.yaml")


def _write_yaml(cfg, text):
    with open(_yaml_path(cfg), "w", encoding="utf-8") as f:
        f.write(text)


def _make_engine(monkeypatch, api_key="test-key"):
    """Real STTEngine — only _build_request_body is exercised (pure, no I/O)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", api_key)
    from utils.stt import STTEngine

    return STTEngine(on_result=lambda _text: None)


def _config_warnings(caplog):
    return [
        r for r in caplog.records
        if r.name == "utils.stt_config" and r.levelno == logging.WARNING
    ]


# ── get_stt_model: resolution order ──────────────────────────────────────────

def test_unset_resolves_to_default_no_warning(cfg, caplog):
    with caplog.at_level(logging.DEBUG, logger="utils.stt_config"):
        assert stt_config.get_stt_model() == stt_config.DEFAULT_STT_MODEL
    assert _config_warnings(caplog) == []  # first run: silent


def test_default_constant_is_measured_bias_model():
    assert stt_config.DEFAULT_STT_MODEL == BIAS_MODEL


def test_env_overrides_yaml(cfg, monkeypatch):
    _write_yaml(cfg, "model: from-yaml/model\n")
    monkeypatch.setenv("STT_MODEL", "env/model")
    assert stt_config.get_stt_model() == "env/model"


def test_env_overrides_missing_file(cfg, monkeypatch):
    monkeypatch.setenv("STT_MODEL", "env/model")
    assert stt_config.get_stt_model() == "env/model"


def test_blank_env_is_ignored_not_an_override(cfg, monkeypatch):
    _write_yaml(cfg, "model: from-yaml/model\n")
    monkeypatch.setenv("STT_MODEL", "   ")
    assert stt_config.get_stt_model() == "from-yaml/model"


def test_yaml_value_wins_over_default(cfg):
    _write_yaml(cfg, "model: from-yaml/model\n")
    assert stt_config.get_stt_model() == "from-yaml/model"


# ── get_stt_model: degraded stores → default + WARNING ──────────────────────

def test_blank_yaml_model_falls_back_with_warning(cfg, caplog):
    _write_yaml(cfg, 'model: ""\n')
    with caplog.at_level(logging.WARNING, logger="utils.stt_config"):
        assert stt_config.get_stt_model() == stt_config.DEFAULT_STT_MODEL
    assert len(_config_warnings(caplog)) == 1


def test_non_string_yaml_model_falls_back_with_warning(cfg, caplog):
    _write_yaml(cfg, "model: [a, b]\n")
    with caplog.at_level(logging.WARNING, logger="utils.stt_config"):
        assert stt_config.get_stt_model() == stt_config.DEFAULT_STT_MODEL
    assert len(_config_warnings(caplog)) == 1


def test_malformed_yaml_falls_back_with_warning(cfg, caplog):
    _write_yaml(cfg, "::: not valid : [")
    with caplog.at_level(logging.WARNING, logger="utils.stt_config"):
        assert stt_config.get_stt_model() == stt_config.DEFAULT_STT_MODEL
    assert len(_config_warnings(caplog)) == 1


def test_non_mapping_yaml_falls_back_with_warning(cfg, caplog):
    _write_yaml(cfg, "- just\n- a\n- list\n")
    with caplog.at_level(logging.WARNING, logger="utils.stt_config"):
        assert stt_config.get_stt_model() == stt_config.DEFAULT_STT_MODEL
    assert len(_config_warnings(caplog)) == 1


def test_missing_file_is_silent_first_run(cfg, caplog):
    assert not os.path.exists(_yaml_path(cfg))
    with caplog.at_level(logging.DEBUG, logger="utils.stt_config"):
        assert stt_config.get_stt_model() == stt_config.DEFAULT_STT_MODEL
    assert _config_warnings(caplog) == []


# ── set_stt_model: round-trip, atomicity, validation ────────────────────────

def test_set_then_get_roundtrip(cfg):
    stt_config.set_stt_model(OTHER_KNOWN)
    assert stt_config.get_stt_model() == OTHER_KNOWN


def test_saved_file_is_exactly_model_key(cfg):
    stt_config.set_stt_model(CUSTOM_MODEL)
    with open(_yaml_path(cfg), "r", encoding="utf-8") as f:
        assert json.dumps(safe_load_yaml(f.read()), sort_keys=True) == \
            json.dumps({"model": CUSTOM_MODEL}, sort_keys=True)


def test_saved_file_is_0600(cfg):
    stt_config.set_stt_model(CUSTOM_MODEL)
    mode = stat.S_IMODE(os.stat(_yaml_path(cfg)).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_set_leaves_no_tmp_file(cfg):
    stt_config.set_stt_model(CUSTOM_MODEL)
    assert not os.path.exists(_yaml_path(cfg) + ".tmp")


def test_set_strips_surrounding_whitespace(cfg):
    stt_config.set_stt_model(f"  {CUSTOM_MODEL}  ")
    with open(_yaml_path(cfg), "r", encoding="utf-8") as f:
        assert safe_load_yaml(f.read()) == {"model": CUSTOM_MODEL}


def test_set_rejects_blank_and_non_string(cfg):
    for bad in ("", "   ", None, 42):
        with pytest.raises(ValueError):
            stt_config.set_stt_model(bad)


def test_failed_set_preserves_previous_value(cfg):
    stt_config.set_stt_model(OTHER_KNOWN)
    with pytest.raises(ValueError):
        stt_config.set_stt_model("   ")
    assert stt_config.get_stt_model() == OTHER_KNOWN
    assert not os.path.exists(_yaml_path(cfg) + ".tmp")


def test_atomic_failure_preserves_old_file(cfg, monkeypatch):
    stt_config.set_stt_model(OTHER_KNOWN)
    with open(_yaml_path(cfg), "r", encoding="utf-8") as f:
        old_content = f.read()

    real_rename = os.rename

    def exploding_rename(src, dst, *a, **kw):
        if str(dst) == _yaml_path(cfg):
            raise OSError("disk exploded")
        return real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(os, "rename", exploding_rename)
    with pytest.raises(OSError):
        stt_config.set_stt_model(CUSTOM_MODEL)

    with open(_yaml_path(cfg), "r", encoding="utf-8") as f:
        assert f.read() == old_content
    assert not os.path.exists(_yaml_path(cfg) + ".tmp")


def test_set_overwrites_previous_choice(cfg):
    stt_config.set_stt_model(OTHER_KNOWN)
    stt_config.set_stt_model(CUSTOM_MODEL)
    assert stt_config.get_stt_model() == CUSTOM_MODEL


# ── load_stt_model_state: (model, error) contract for the settings UI ───────

def test_state_missing_file_is_clean_default(cfg):
    assert stt_config.load_stt_model_state() == (
        stt_config.DEFAULT_STT_MODEL, None)


def test_state_valid_yaml_is_clean(cfg):
    stt_config.set_stt_model(CUSTOM_MODEL)
    assert stt_config.load_stt_model_state() == (CUSTOM_MODEL, None)


def test_state_env_override_is_clean(cfg, monkeypatch):
    _write_yaml(cfg, "::: broken [")
    monkeypatch.setenv("STT_MODEL", "env/model")
    assert stt_config.load_stt_model_state() == ("env/model", None)


def test_state_broken_yaml_reports_error(cfg):
    _write_yaml(cfg, "::: broken [")
    model, error = stt_config.load_stt_model_state()
    assert model == stt_config.DEFAULT_STT_MODEL
    assert isinstance(error, str) and error  # UI renders the section inert


def test_state_blank_model_reports_error(cfg):
    _write_yaml(cfg, 'model: ""\n')
    model, error = stt_config.load_stt_model_state()
    assert model == stt_config.DEFAULT_STT_MODEL
    assert isinstance(error, str) and error


# ── is_bias_supported: truth table ───────────────────────────────────────────

def test_bias_supported_default_true():
    assert stt_config.is_bias_supported(BIAS_MODEL) is True


def test_bias_supported_other_known_false():
    for model_id, _label in stt_config.STT_MODEL_CHOICES:
        if model_id != BIAS_MODEL:
            assert stt_config.is_bias_supported(model_id) is False, model_id


def test_bias_supported_custom_and_garbage_false():
    for bad in (CUSTOM_MODEL, "", "   ", None, 42, ["microsoft/mai-transcribe-2"]):
        assert stt_config.is_bias_supported(bad) is False, repr(bad)


def test_bias_supported_strips_whitespace():
    assert stt_config.is_bias_supported(f"  {BIAS_MODEL}  ") is True


def test_choices_cover_default_and_are_unique_ids():
    ids = [m for m, _ in stt_config.STT_MODEL_CHOICES]
    assert stt_config.DEFAULT_STT_MODEL in ids
    assert len(ids) == len(set(ids))
    assert len(ids) == 5  # the measured table


# ── utils/stt.py integration: per-call model + gated phraseList ─────────────

DEFAULT_PHRASES = ["DevelCakes", "Crabcakes", "crabcakes"]


def test_request_body_uses_selected_model(cfg, monkeypatch):
    stt_config.set_stt_model(OTHER_KNOWN)
    engine = _make_engine(monkeypatch)
    body = engine._build_request_body("QUJD")
    assert body["model"] == OTHER_KNOWN  # NOT the hardcoded default
    assert body["input_audio"] == {"data": "QUJD", "format": "wav"}


def test_model_resolved_per_call_settings_apply_next_dictation(cfg, monkeypatch):
    """SPEC-37 core: no caching — a set_stt_model() between dictations takes
    effect on the SAME engine instance."""
    engine = _make_engine(monkeypatch)
    stt_config.set_stt_model(OTHER_KNOWN)
    assert engine._build_request_body("QUJD")["model"] == OTHER_KNOWN
    stt_config.set_stt_model(CUSTOM_MODEL)
    assert engine._build_request_body("QUJD")["model"] == CUSTOM_MODEL


def test_phraselist_present_for_bias_supported_model(cfg, monkeypatch):
    engine = _make_engine(monkeypatch)
    body = engine._build_request_body("QUJD")
    assert body["model"] == BIAS_MODEL  # no stt.yaml → default
    phrases = body["provider"]["options"]["azure"]["phraseList"]["phrases"]
    assert phrases == DEFAULT_PHRASES


def test_phraselist_absent_for_custom_model(cfg, monkeypatch):
    stt_config.set_stt_model(CUSTOM_MODEL)
    engine = _make_engine(monkeypatch)
    body = engine._build_request_body("QUJD")
    assert body["model"] == CUSTOM_MODEL
    assert "provider" not in body  # ungated phraseList would 400 on OpenRouter


def test_phraselist_absent_for_other_known_model(cfg, monkeypatch):
    stt_config.set_stt_model(OTHER_KNOWN)
    engine = _make_engine(monkeypatch)
    body = engine._build_request_body("QUJD")
    assert body["model"] == OTHER_KNOWN
    assert "provider" not in body


def test_phraselist_absent_when_env_phrases_empty(cfg, monkeypatch):
    """STT_PHRASES='' wins even for a bias-supported model — env override."""
    monkeypatch.setenv("STT_PHRASES", "")
    engine = _make_engine(monkeypatch)
    body = engine._build_request_body("QUJD")
    assert body["model"] == BIAS_MODEL
    assert "provider" not in body


def test_phraselist_absent_for_bias_model_with_env_model_override(
        cfg, monkeypatch):
    """env STT_MODEL=custom → that model goes out AND never gets phraseList."""
    monkeypatch.setenv("STT_MODEL", CUSTOM_MODEL)
    engine = _make_engine(monkeypatch)
    body = engine._build_request_body("QUJD")
    assert body["model"] == CUSTOM_MODEL
    assert "provider" not in body


def test_explicit_model_argument_beats_config(cfg, monkeypatch):
    stt_config.set_stt_model(OTHER_KNOWN)
    engine = _make_engine(monkeypatch)
    body = engine._build_request_body("QUJD", CUSTOM_MODEL)
    assert body["model"] == CUSTOM_MODEL
    assert "provider" not in body
