# tests/test_stt_settings_page.py — SPEC-37 ROUND 2 (audit BUG #3).
#
# Page-level tests for the STT Model section of ui/views/settings_dialog.py.
# GTK-xvfb style, same as tests/test_settings_dialog.py: construct the real
# SettingsDialog under tmp_config_dir and assert on the widget tree — the
# suite is run under xvfb-run (Gtk.Window needs a display; the module-level
# skipif keeps GTK-less environments green).
#
# Covers the three audit requirements:
#   (i)   selecting a KNOWN dropdown index persists it to stt.yaml on disk
#         (the dropdown is the primary control — no Apply click needed);
#   (ii)  fresh open with a known model on disk → Apply button NOT visible
#         (BUG #2 regression guard: both custom-row widgets share one flag);
#   (iii) degraded store (corrupt yaml) → dropdown, entry and Apply all
#         insensitive (honest inert section, Telegram pattern).

import os

import pytest

try:
    from gi.repository import Gtk

    GTK_AVAILABLE = True
except (ImportError, ValueError):
    GTK_AVAILABLE = False

from ui.handlers.settings_handler import SettingsHandler
from ui.views.settings_dialog import SettingsDialog
from utils import stt_config
from utils.yaml_load import safe_load_yaml

pytestmark = pytest.mark.skipif(not GTK_AVAILABLE, reason="GTK not available")

# STT_MODEL_CHOICES[1] → "mistralai/voxtral-mini-3b-2507"; its dropdown index
# equals its position in the choices list (Custom… is the trailing entry).
KNOWN_INDEX = 1
KNOWN_MODEL = stt_config.STT_MODEL_CHOICES[KNOWN_INDEX][0]


def _yaml_path(config_dir) -> str:
    return os.path.join(str(config_dir), "stt.yaml")


def _clean_stt_env(monkeypatch) -> None:
    """STT_MODEL env override would shadow the store — remove it."""
    monkeypatch.delenv("STT_MODEL", raising=False)


class TestKnownSelectionPersists:
    def test_selecting_known_index_persists_to_disk(self, tmp_config_dir, monkeypatch):
        _clean_stt_env(monkeypatch)
        d = SettingsDialog(parent=None, handler=SettingsHandler())
        assert not os.path.exists(_yaml_path(tmp_config_dir))  # first-run state
        d._stt_dropdown.set_selected(KNOWN_INDEX)
        assert os.path.isfile(_yaml_path(tmp_config_dir))
        with open(_yaml_path(tmp_config_dir), encoding="utf-8") as f:
            raw = safe_load_yaml(f.read())
        assert raw["model"] == KNOWN_MODEL

    def test_known_selection_reports_saved_status(self, tmp_config_dir, monkeypatch):
        _clean_stt_env(monkeypatch)
        d = SettingsDialog(parent=None, handler=SettingsHandler())
        d._stt_dropdown.set_selected(KNOWN_INDEX)
        label = d._stt_status_label
        assert label.get_text().startswith("✓ Saved:")
        assert label.has_css_class("settings-status-ok")
        assert not label.has_css_class("settings-status-fail")

    def test_selecting_known_index_hides_custom_row(self, tmp_config_dir, monkeypatch):
        _clean_stt_env(monkeypatch)
        d = SettingsDialog(parent=None, handler=SettingsHandler())
        d._stt_dropdown.set_selected(KNOWN_INDEX)
        assert d._stt_custom_entry.get_visible() is False
        assert d._stt_apply_btn.get_visible() is False


class TestFreshOpenKnownModelOnDisk:
    def test_apply_button_not_visible_on_fresh_open(self, tmp_config_dir, monkeypatch):
        _clean_stt_env(monkeypatch)
        with open(_yaml_path(tmp_config_dir), "w", encoding="utf-8") as f:
            f.write(f"model: {KNOWN_MODEL}\n")
        d = SettingsDialog(parent=None, handler=SettingsHandler())
        # Known id on disk → dropdown pre-selected to it (not Custom…), and
        # BOTH custom-row widgets hidden — no stray Apply (audit BUG #2).
        assert isinstance(d._stt_dropdown, Gtk.DropDown)
        assert d._stt_dropdown.get_selected() == KNOWN_INDEX
        assert d._stt_apply_btn.get_visible() is False
        assert d._stt_custom_entry.get_visible() is False


class TestDegradedStore:
    def test_corrupt_yaml_renders_all_controls_insensitive(
        self, tmp_config_dir, monkeypatch
    ):
        _clean_stt_env(monkeypatch)
        with open(_yaml_path(tmp_config_dir), "w", encoding="utf-8") as f:
            f.write("model: {unclosed\n")  # yaml parse error → degraded store
        d = SettingsDialog(parent=None, handler=SettingsHandler())
        assert d._stt_dropdown.get_sensitive() is False
        assert d._stt_custom_entry.get_sensitive() is False
        assert d._stt_apply_btn.get_sensitive() is False
        assert "unusable" in d._stt_status_label.get_text()
