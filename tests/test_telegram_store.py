# tests/test_telegram_store.py — SPEC-15 SP2 (utils/telegram_store.py).
#
# Pure store: telegram_bridge.yaml under the config dir, atomic write, 0600,
# token never logged. No GTK (runs bare).

import logging
import os
import stat

import pytest
import utils.telegram_store as store


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Point get_config_dir at an isolated tmp dir."""
    monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
    return tmp_path


def test_load_missing_returns_defaults(cfg):
    data = store.load_bridge_config()
    assert data == {"bot_token": "", "chat_id": None, "paired_handle": ""}


def test_save_then_load_roundtrip(cfg):
    store.save_bridge_config(bot_token="123:abc", chat_id=42, paired_handle="@me")
    data = store.load_bridge_config()
    assert data["bot_token"] == "123:abc"
    assert data["chat_id"] == 42
    assert data["paired_handle"] == "@me"


def test_saved_file_is_0600(cfg):
    store.save_bridge_config(bot_token="123:abc", chat_id=42)
    path = os.path.join(str(cfg), "telegram_bridge.yaml")
    assert os.path.isfile(path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_save_tightens_preexisting_parent_dir(cfg):
    """BUG#2/#9 (SP2 audit): the parent dir holds the credential file and must
    be 0700 on EVERY save — not just on first create. A pre-existing 0755 dir
    (earlier install / another tool) must be tightened."""
    os.chmod(str(cfg), 0o755)
    assert stat.S_IMODE(os.stat(str(cfg)).st_mode) == 0o755  # precondition
    store.save_bridge_config(bot_token="123:abc", chat_id=42)
    mode = stat.S_IMODE(os.stat(str(cfg)).st_mode)
    assert mode == 0o700, f"expected parent 0700, got {oct(mode)}"


def test_atomic_write_leaves_no_tmp(cfg):
    store.save_bridge_config(bot_token="123:abc")
    assert not os.path.exists(os.path.join(str(cfg), "telegram_bridge.yaml.tmp"))


def test_token_never_logged_on_save(cfg, caplog):
    secret = "999:SUPER-SECRET-TOKEN"
    with caplog.at_level(logging.DEBUG, logger="utils.telegram_store"):
        store.save_bridge_config(bot_token=secret, chat_id=1)
        store.load_bridge_config()
    assert secret not in caplog.text
    assert "SUPER-SECRET-TOKEN" not in caplog.text


def test_token_never_logged_on_malformed_yaml(cfg, caplog):
    path = os.path.join(str(cfg), "telegram_bridge.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write("bot_token: 111:LEAKY-SECRET\n: : : not yaml: [")
    with caplog.at_level(logging.DEBUG, logger="utils.telegram_store"):
        store.load_bridge_config()
    assert "LEAKY-SECRET" not in caplog.text


def test_malformed_yaml_returns_defaults(cfg):
    path = os.path.join(str(cfg), "telegram_bridge.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write("::: not valid : [")
    assert store.load_bridge_config() == {
        "bot_token": "", "chat_id": None, "paired_handle": "",
    }


def test_chat_id_none_roundtrips(cfg):
    store.save_bridge_config(bot_token="123:abc", chat_id=None, paired_handle="")
    data = store.load_bridge_config()
    assert data["chat_id"] is None


def test_clear_bridge_config(cfg):
    store.save_bridge_config(bot_token="123:abc", chat_id=42, paired_handle="@me")
    store.save_bridge_config(bot_token="", chat_id=None, paired_handle="")
    data = store.load_bridge_config()
    assert data["bot_token"] == ""
    assert data["chat_id"] is None


def test_is_configured(cfg):
    assert store.is_configured(store.load_bridge_config()) is False
    store.save_bridge_config(bot_token="123:abc", chat_id=42)
    assert store.is_configured(store.load_bridge_config()) is True


def test_is_configured_requires_both_token_and_chat(cfg):
    store.save_bridge_config(bot_token="123:abc", chat_id=None)
    assert store.is_configured(store.load_bridge_config()) is False
    store.save_bridge_config(bot_token="", chat_id=42)
    assert store.is_configured(store.load_bridge_config()) is False