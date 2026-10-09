# tests/test_live_bridge_readonly.py — SPEC-20a read-only read_file on the SP4 bridge.
#
# Layer 1: LiveBridge routing (no GTK) + the file-policy reader.
# Layer 2: ChatSurface drain delivers a synchronous read on the SAME
#          develcakes:result seam the approval resolver uses.
# Layer 3: real-WebKit round trip (skipped when WebKit is absent).

import base64
import json
import os
from unittest.mock import patch

import pytest

from utils.live_bridge import (
    CONSEQUENTIAL_METHODS,
    READ_ONLY_METHODS,
    LiveBridge,
)
from utils.live_reader import MAX_IMAGE_BYTES, read_local_image

# PNG signature. base64(b"\x89PNG\r") == "iVBOR".
_PNG = b"\x89PNG\r\n\x1a\n" + b"spec20a"


class SpyApprover:
    def __init__(self):
        self.calls = []

    def __call__(self, method, params, call_id):
        self.calls.append((method, params, call_id))


def _clear_project_root(monkeypatch) -> None:
    monkeypatch.delenv("DEVELCAKES_ACTIVE_PROJECT_PATH", raising=False)
    monkeypatch.delenv("CRABCAKES_ACTIVE_PROJECT_PATH", raising=False)


def _bridge(reader=read_local_image):
    spy = SpyApprover()
    bridge = LiveBridge(approver=spy)
    if reader is not None:
        bridge.set_reader(reader)
    return bridge, spy


def _dispatch(bridge, path):
    return bridge.dispatch("read_file", {"path": path})


# ── registry + routing ───────────────────────────────────────────────────


def test_read_only_registry_is_exactly_read_file():
    assert READ_ONLY_METHODS == frozenset({"read_file"})
    assert READ_ONLY_METHODS.isdisjoint(CONSEQUENTIAL_METHODS)
    assert CONSEQUENTIAL_METHODS == frozenset(
        {"exec_command", "write_file", "edit_file", "approve_exec"}
    )


def test_cap_matches_spec20():
    from render.html import _MAX_IMAGE_BYTES
    assert MAX_IMAGE_BYTES == _MAX_IMAGE_BYTES == 8 * 1024 * 1024


def test_read_file_png_round_trip(tmp_path, monkeypatch):
    """integration shape: wired reader, real PNG bytes, no approver."""
    _clear_project_root(monkeypatch)
    png = tmp_path / "x.png"
    png.write_bytes(_PNG)
    bridge, spy = _bridge()
    result = _dispatch(bridge, str(png))
    assert result["status"] == "ok"
    assert result["id"]
    assert result["data"]["mime"] == "image/png"
    assert result["data"]["base64"].startswith("iVBOR")
    assert result["data"]["base64"] == base64.b64encode(_PNG).decode("ascii")
    assert spy.calls == []
    assert bridge.pending_ids() == []


def test_read_file_does_not_call_approver_or_fill_pending(tmp_path, monkeypatch):
    _clear_project_root(monkeypatch)
    png = tmp_path / "x.png"
    png.write_bytes(_PNG)
    bridge, spy = _bridge()
    for _ in range(3):
        result = _dispatch(bridge, str(png))
        assert result["status"] == "ok"
    assert spy.calls == []
    assert bridge.pending_ids() == []


def test_reader_is_called_once(tmp_path, monkeypatch):
    _clear_project_root(monkeypatch)
    png = tmp_path / "x.png"
    png.write_bytes(_PNG)
    seen = []

    def reader(params):
        seen.append(params)
        return read_local_image(params)

    bridge, spy = _bridge(reader)
    _dispatch(bridge, str(png))
    assert len(seen) == 1
    assert seen[0] == {"path": str(png)}
    assert spy.calls == []


def test_no_reader_wired_errors_cleanly():
    bridge, spy = _bridge(reader=None)
    result = bridge.dispatch("read_file", {"path": "/tmp/x.png"})
    assert result["status"] == "error"
    assert result["id"]
    assert result["data"] == {"reason": "no reader wired"}
    assert spy.calls == []
    assert bridge.pending_ids() == []


def test_reader_exception_is_isolated():
    def boom(_params):
        raise RuntimeError("disk on fire")

    bridge, spy = _bridge(boom)
    result = bridge.dispatch("read_file", {"path": "/tmp/x.png"})
    assert result["status"] == "error"
    assert result["data"]["reason"] == "reader failed"
    assert spy.calls == []


def test_reader_non_dict_is_isolated():
    bridge, _spy = _bridge(lambda _params: "nope")
    result = bridge.dispatch("read_file", {"path": "/tmp/x.png"})
    assert result["status"] == "error"
    assert "base64" not in result["data"]


def test_dispatch_never_raises():
    bridge, _spy = _bridge(lambda _params: (_ for _ in ()).throw(RuntimeError("x")))
    bridge.dispatch("read_file", None)  # must not raise
    bridge.dispatch("read_file", "not-a-dict")
    bridge.dispatch("nope", {"path": "x"})


def test_unknown_method_unchanged():
    bridge, spy = _bridge()
    result = bridge.dispatch("launch_missiles", {"x": 1})
    assert result["status"] == "error"
    assert "unknown method" in result["data"]["reason"]
    assert spy.calls == []


def test_exec_command_approval_flow_unchanged():
    bridge, spy = _bridge()
    result = bridge.dispatch("exec_command", {"cmd": "ls"})
    assert result["status"] == "pending"
    assert result["id"]
    assert len(spy.calls) == 1
    assert spy.calls[0][0] == "exec_command"
    assert spy.calls[0][1] == {"cmd": "ls"}
    assert result["id"] in bridge.pending_ids()


def test_none_params_reach_reader_as_empty_dict():
    seen = []

    def reader(params):
        seen.append(params)
        return {"reason": "missing path"}

    bridge, _spy = _bridge(reader)
    result = bridge.dispatch("read_file", None)
    assert seen == [{}]
    assert result["status"] == "error"


# ── file policy (each gate is load-bearing) ──────────────────────────────


def test_etc_passwd_errors_and_is_not_read(monkeypatch):
    _clear_project_root(monkeypatch)
    bridge, spy = _bridge()
    with patch("utils.live_reader.open") as mocked_open:
        result = _dispatch(bridge, "/etc/passwd")
    mocked_open.assert_not_called()
    assert result["status"] == "error"
    assert result["data"] == {"reason": "path not allowed"}
    assert "base64" not in result["data"]
    assert spy.calls == []


def test_png_outside_roots_is_not_read(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    secret = outside / "secret.png"
    secret.write_bytes(_PNG)
    monkeypatch.setattr(
        "utils.image_paths.get_allowed_roots", lambda: (str(allowed),)
    )
    bridge, _spy = _bridge()
    with patch("utils.live_reader.open") as mocked_open:
        result = _dispatch(bridge, str(secret))
    mocked_open.assert_not_called()
    assert result["status"] == "error"
    assert result["data"]["reason"] == "path not allowed"


def test_traversal_out_of_root_refused(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    secret = outside / "secret.png"
    secret.write_bytes(_PNG)
    monkeypatch.setattr(
        "utils.image_paths.get_allowed_roots", lambda: (str(allowed),)
    )
    escaped = os.path.join(
        str(allowed), "nest", "..", "..", outside.name, "secret.png"
    )
    assert os.path.realpath(escaped) == os.path.realpath(str(secret))
    bridge, _spy = _bridge()
    with patch("utils.live_reader.open") as mocked_open:
        result = _dispatch(bridge, escaped)
    mocked_open.assert_not_called()
    assert result["status"] == "error"
    assert result["data"]["reason"] == "path not allowed"


def test_benign_dotdot_inside_root_still_reads(tmp_path, monkeypatch):
    """`..` is not a banned substring — realpath containment decides."""
    _clear_project_root(monkeypatch)
    nested = tmp_path / "sub"
    nested.mkdir()
    png = tmp_path / "photo.png"
    png.write_bytes(_PNG)
    via = str(nested / ".." / "photo.png")
    bridge, _spy = _bridge()
    result = _dispatch(bridge, via)
    assert result["status"] == "ok"
    assert result["data"]["base64"].startswith("iVBOR")


def test_symlink_to_etc_passwd_refused(tmp_path, monkeypatch):
    _clear_project_root(monkeypatch)
    link = tmp_path / "evil.png"
    link.symlink_to("/etc/passwd")
    bridge, spy = _bridge()
    with patch("utils.live_reader.open") as mocked_open:
        result = _dispatch(bridge, str(link))
    mocked_open.assert_not_called()
    assert result["status"] == "error"
    assert result["data"]["reason"] == "path not allowed"
    assert spy.calls == []


def test_symlink_to_outside_png_refused(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    secret = outside / "secret.png"
    secret.write_bytes(b"OUTSIDE-PNG-BYTES")
    link = allowed / "link.png"
    link.symlink_to(secret)
    monkeypatch.setattr(
        "utils.image_paths.get_allowed_roots", lambda: (str(allowed),)
    )
    bridge, _spy = _bridge()
    with patch("utils.live_reader.open") as mocked_open:
        result = _dispatch(bridge, str(link))
    mocked_open.assert_not_called()
    assert result["status"] == "error"
    assert result["data"]["reason"] == "path not allowed"
    assert "OUTSIDE" not in json.dumps(result)


def test_symlink_to_dotfile_png_inside_root_refused(tmp_path, monkeypatch):
    """A normal name must not launder a dotfile that sits inside the root."""
    _clear_project_root(monkeypatch)
    hidden = tmp_path / ".secret.png"
    hidden.write_bytes(_PNG)
    link = tmp_path / "ok.png"
    link.symlink_to(hidden)
    bridge, _spy = _bridge()
    with patch("utils.live_reader.open") as mocked_open:
        result = _dispatch(bridge, str(link))
    mocked_open.assert_not_called()
    assert result["status"] == "error"
    assert result["data"]["reason"] == "dotfile not allowed"


def test_symlink_to_svg_inside_root_refused(tmp_path, monkeypatch):
    """A png-named link must not launder a .svg that sits inside the root."""
    _clear_project_root(monkeypatch)
    svg = tmp_path / "evil.svg"
    svg.write_bytes(b"<svg>nope</svg>")
    link = tmp_path / "ok.png"
    link.symlink_to(svg)
    bridge, _spy = _bridge()
    with patch("utils.live_reader.open") as mocked_open:
        result = _dispatch(bridge, str(link))
    mocked_open.assert_not_called()
    assert result["status"] == "error"
    assert result["data"]["reason"] == "extension not allowed"


@pytest.mark.parametrize("name", ["pic.svg", "pic.txt", "notes", "Pic.SVG"])
def test_extension_refused_and_not_read(tmp_path, monkeypatch, name):
    _clear_project_root(monkeypatch)
    target = tmp_path / name
    target.write_bytes(b"not-an-image")
    bridge, _spy = _bridge()
    with patch("utils.live_reader.open") as mocked_open:
        result = _dispatch(bridge, str(target))
    mocked_open.assert_not_called()
    assert result["status"] == "error"
    assert result["data"]["reason"] == "extension not allowed"


def test_dotenv_refused_and_not_read(tmp_path, monkeypatch):
    _clear_project_root(monkeypatch)
    target = tmp_path / ".env"
    target.write_text("SECRET=1")
    bridge, _spy = _bridge()
    with patch("utils.live_reader.open") as mocked_open:
        result = _dispatch(bridge, str(target))
    mocked_open.assert_not_called()
    assert result["status"] == "error"
    assert result["data"]["reason"] == "dotfile not allowed"
    assert "SECRET" not in json.dumps(result)


def test_dotfile_png_refused_and_not_read(tmp_path, monkeypatch):
    _clear_project_root(monkeypatch)
    target = tmp_path / ".secret.png"
    target.write_bytes(_PNG)
    bridge, _spy = _bridge()
    with patch("utils.live_reader.open") as mocked_open:
        result = _dispatch(bridge, str(target))
    mocked_open.assert_not_called()
    assert result["status"] == "error"
    assert result["data"]["reason"] == "dotfile not allowed"


def test_oversize_refused_and_not_read(tmp_path, monkeypatch):
    _clear_project_root(monkeypatch)
    big = tmp_path / "big.png"
    with open(big, "wb") as fh:
        fh.truncate(MAX_IMAGE_BYTES + 1)
    bridge, _spy = _bridge()
    with patch("utils.live_reader.open") as mocked_open:
        result = _dispatch(bridge, str(big))
    mocked_open.assert_not_called()
    assert result["status"] == "error"
    assert result["data"]["reason"] == "file too large"


def test_exact_cap_is_allowed(tmp_path, monkeypatch):
    _clear_project_root(monkeypatch)
    monkeypatch.setattr("utils.live_reader.MAX_IMAGE_BYTES", len(_PNG))
    png = tmp_path / "exact.png"
    png.write_bytes(_PNG)
    bridge, _spy = _bridge()
    result = _dispatch(bridge, str(png))
    assert result["status"] == "ok"
    assert result["data"]["base64"].startswith("iVBOR")


@pytest.mark.parametrize(
    "ext,mime,payload",
    [
        (".png", "image/png", _PNG),
        (".jpg", "image/jpeg", b"\xff\xd8\xff\xd9"),
        (".jpeg", "image/jpeg", b"\xff\xd8\xff\xd9"),
        (".gif", "image/gif", b"GIF89a"),
        (".webp", "image/webp", b"RIFF"),
        (".PNG", "image/png", _PNG),
    ],
)
def test_allowlist_extensions_read(tmp_path, monkeypatch, ext, mime, payload):
    _clear_project_root(monkeypatch)
    target = tmp_path / f"img{ext}"
    target.write_bytes(payload)
    bridge, _spy = _bridge()
    result = _dispatch(bridge, str(target))
    assert result["status"] == "ok"
    assert result["data"]["mime"] == mime
    if ext.lower() == ".png":
        assert result["data"]["base64"].startswith("iVBOR")


def test_missing_file_errors(tmp_path, monkeypatch):
    _clear_project_root(monkeypatch)
    bridge, _spy = _bridge()
    result = _dispatch(bridge, str(tmp_path / "nope.png"))
    assert result["status"] == "error"
    assert result["data"]["reason"] == "not a file"


def test_missing_path_errors():
    bridge, _spy = _bridge()
    result = bridge.dispatch("read_file", {})
    assert result["status"] == "error"
    assert result["data"]["reason"] == "missing path"


# ── delivery seam (same develcakes:result channel; no second pipe) ──────


def _surface_with_fake_eval(queue_items, approver):
    pytest.importorskip("gi")
    import gi
    gi.require_version("Gtk", "4.0")
    from ui.views.chat_surface import ChatSurface

    surface = ChatSurface(live_bridge_approver=approver)
    surface._live_js = True
    surface._webview = object()
    scripts = []

    def fake_eval(script, callback):
        scripts.append(script)
        if "dcBridgeQueue" in script:
            callback(json.dumps(queue_items))
        else:
            callback(None)

    surface._document_eval_main = fake_eval
    return surface, scripts


def test_drain_delivers_read_file_on_result_seam(tmp_path, monkeypatch):
    _clear_project_root(monkeypatch)
    png = tmp_path / "x.png"
    png.write_bytes(_PNG)
    spy = SpyApprover()
    surface, scripts = _surface_with_fake_eval(
        [{"method": "read_file", "params": {"path": str(png)}, "id": "dc1"}],
        spy,
    )
    try:
        surface._drain_bridge_queue()
    finally:
        surface.destroy()
    assert spy.calls == []
    delivered = [s for s in scripts if "develcakes:result" in s]
    assert len(delivered) == 1
    assert '"status": "ok"' in delivered[0]
    assert "iVBOR" in delivered[0]
    assert '"id": "dc1"' in delivered[0]


def test_drain_delivers_read_file_error_on_result_seam(monkeypatch):
    _clear_project_root(monkeypatch)
    spy = SpyApprover()
    surface, scripts = _surface_with_fake_eval(
        [{"method": "read_file", "params": {"path": "/etc/passwd"}, "id": "dc1"}],
        spy,
    )
    try:
        surface._drain_bridge_queue()
    finally:
        surface.destroy()
    assert spy.calls == []
    delivered = [s for s in scripts if "develcakes:result" in s]
    assert len(delivered) == 1
    assert '"status": "error"' in delivered[0]
    assert "iVBOR" not in delivered[0]


def test_drain_does_not_resolve_exec_command(monkeypatch):
    """Consequential calls stay pending — the approval resolver delivers later."""
    _clear_project_root(monkeypatch)
    spy = SpyApprover()
    surface, scripts = _surface_with_fake_eval(
        [{"method": "exec_command", "params": {"cmd": "ls"}, "id": "dc1"}],
        spy,
    )
    try:
        surface._drain_bridge_queue()
    finally:
        surface.destroy()
    assert len(spy.calls) == 1
    assert spy.calls[0][0] == "exec_command"
    assert not any("develcakes:result" in s for s in scripts)


def test_drain_does_not_resolve_unknown_method():
    spy = SpyApprover()
    surface, scripts = _surface_with_fake_eval(
        [{"method": "launch_missiles", "params": {}, "id": "dc1"}],
        spy,
    )
    try:
        surface._drain_bridge_queue()
    finally:
        surface.destroy()
    assert spy.calls == []
    assert not any("develcakes:result" in s for s in scripts)


# ── real-WebKit page round trip ──────────────────────────────────────────

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import GLib, Gtk
except (ValueError, ImportError):  # pragma: no cover
    pytest.skip("WebKit 6.0 unavailable", allow_module_level=True)

import ui.views.chat_surface as cs_module
from ui.views.chat_surface import ChatSurface


def _present(surface):
    win = Gtk.Window()
    win.set_child(surface)
    win.set_default_size(640, 480)
    win.present()
    return win


def _pump(sec):
    ctx = GLib.MainContext.default()
    import time
    end = time.time() + sec
    while time.time() < end:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.02)


def _eval_main(surface, code, timeout=6):
    box = {}
    loop = GLib.MainLoop()

    def cb(v, r, data):
        try:
            box["v"] = v.evaluate_javascript_finish(r).to_string()
        except Exception:  # noqa: BLE001
            box["v"] = None
        loop.quit()

    view = surface._ensure_webview()
    view.evaluate_javascript(code, -1, None, None, None, cb, None)
    GLib.timeout_add_seconds(timeout, lambda: (loop.quit(), False)[1])
    loop.run()
    return box.get("v")


def test_page_read_file_resolves_without_approval(tmp_path, monkeypatch):
    """A live section awaits read_file and paints from the resolved base64.
    No approval card, no approver call."""
    # bwrap SIGTRAPs under this host's userns restriction (chat_surface.py
    # header). Must be set before the first WebView.
    os.environ["WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS"] = "1"
    _clear_project_root(monkeypatch)
    monkeypatch.setattr(cs_module, "_live_js_enabled", lambda: True)
    png = tmp_path / "x.png"
    png.write_bytes(_PNG)
    spy = SpyApprover()
    surface = ChatSurface(live_bridge_approver=spy)
    win = _present(surface)
    call = (
        "window.__img=null;"
        "window.develcakes.call('read_file', {path: "
        + json.dumps(str(png))
        + "}).then(function(r){"
        "var b=(r.data&&r.data.base64)||'';"
        "window.__img=r.status+':'+b.slice(0,5);"
        "});"
    )
    try:
        surface.append_live(
            "<div id='out'>waiting</div>\n<script>" + call + "</script>",
            "Coder",
        )
        surface._drain_renders()
        got = None
        for _ in range(25):
            _pump(0.3)
            got = _eval_main(surface, "window.__img || ''")
            if got and got.startswith("ok:"):
                break
        assert spy.calls == [], f"read_file must not hit the approver: {spy.calls}"
        assert got == "ok:iVBOR", f"page promise did not resolve: {got}"
    finally:
        win.close()
        surface.destroy()
