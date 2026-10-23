"""Tests for the conftest GTK headless guard (tests/conftest.py).

Covers the two hardening behaviors:

  * dynamic ui imports (``importlib.import_module("ui...")`` /
    ``__import__("ui...")``) are detected like static ones — a module that
    pulls widgets in dynamically is skipped when headless unless it opts out;
  * the ``GTK_HEADLESS_SAFE = True`` opt-out marker exempts a module from the
    widget-scan entirely (pytest still collects it).

The guard is exercised directly against this repo's conftest.py. The temp
modules analysed below are written into the per-test tmp dir and never
imported, so nothing here can construct a Gtk widget.
"""

import importlib.util
import os
import unittest.mock

import pytest

_CONFTEST_PATH = os.path.join(os.path.dirname(__file__), "conftest.py")


def _load_conftest():
    spec = importlib.util.spec_from_file_location("tests_conftest", _CONFTEST_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_CF = _load_conftest()


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


DYNAMIC_IMPORTLIB_NO_MARKER = '''\
import importlib

def get_window_cls():
    return importlib.import_module("ui.window").MainWindow
'''

DYNAMIC_DUNDER_IMPORT_NO_MARKER = '''\
def get_window_cls():
    return __import__("ui.window").MainWindow
'''

DYNAMIC_IMPORTLIB_WITH_MARKER = '''\
GTK_HEADLESS_SAFE = True

import importlib

def get_window_cls():
    return importlib.import_module("ui.window").MainWindow
'''

DYNAMIC_NON_UI_IMPORT = '''\
def f():
    return __import__("os")
'''

STATIC_GTK_CONSTRUCTOR = '''\
def build():
    return Gtk.Window()
'''


class TestImportsWidgetConstruction:
    def test_dynamic_importlib_ui_import_is_detected(self, tmp_path):
        p = _write(tmp_path / "dyn_importlib.py", DYNAMIC_IMPORTLIB_NO_MARKER)
        assert _CF._imports_widget_construction(p) is True

    def test_dynamic_dunder_import_ui_is_detected(self, tmp_path):
        p = _write(tmp_path / "dyn_dunder.py", DYNAMIC_DUNDER_IMPORT_NO_MARKER)
        assert _CF._imports_widget_construction(p) is True

    def test_dynamic_ui_import_with_headless_marker_is_exempt(self, tmp_path):
        p = _write(tmp_path / "dyn_marked.py", DYNAMIC_IMPORTLIB_WITH_MARKER)
        assert _CF._imports_widget_construction(p) is False

    def test_dynamic_non_ui_import_not_detected(self, tmp_path):
        p = _write(tmp_path / "dyn_os.py", DYNAMIC_NON_UI_IMPORT)
        assert _CF._imports_widget_construction(p) is False

    def test_static_gtk_constructor_still_detected(self, tmp_path):
        p = _write(tmp_path / "static_gtk.py", STATIC_GTK_CONSTRUCTOR)
        assert _CF._imports_widget_construction(p) is True


class _FakeItem:
    def __init__(self, path):
        self.path = str(path)
        self._markers = []

    def add_marker(self, marker):
        self._markers.append(marker)

    def iter_markers(self, name=None):
        if name is None:
            return iter(self._markers)
        return iter([m for m in self._markers if m.name == name])


class TestCollectionModifyItems:
    def test_skips_dynamic_ui_module_when_headless(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_CF, "_has_display", lambda: False)
        p = _write(tmp_path / "dynamic_ui.py", DYNAMIC_IMPORTLIB_NO_MARKER)
        item = _FakeItem(p)
        _CF.pytest_collection_modifyitems(unittest.mock.MagicMock(), [item])
        skips = list(item.iter_markers("skip"))
        assert len(skips) == 1, f"expected skip marker, got {item._markers}"
        assert "display" in skips[0].kwargs.get("reason", "")

    def test_does_not_skip_dynamic_ui_module_with_marker(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_CF, "_has_display", lambda: False)
        p = _write(tmp_path / "dynamic_ui_marked.py", DYNAMIC_IMPORTLIB_WITH_MARKER)
        item = _FakeItem(p)
        _CF.pytest_collection_modifyitems(unittest.mock.MagicMock(), [item])
        assert list(item.iter_markers("skip")) == []

    def test_no_skip_when_display_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_CF, "_has_display", lambda: True)
        p = _write(tmp_path / "dynamic_ui.py", DYNAMIC_IMPORTLIB_NO_MARKER)
        item = _FakeItem(p)
        _CF.pytest_collection_modifyitems(unittest.mock.MagicMock(), [item])
        assert list(item.iter_markers("skip")) == []
