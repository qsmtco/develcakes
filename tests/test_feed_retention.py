# tests/test_feed_retention.py
# SPEC-03 — P11 memory-ratchet: bounded feed surfaces.
#
# This file is the 3-track retention harness:
#   1. CONFIG track (this sub-phase): prove the live-window config accessors —
#      default, persistence, clamps, type rejection, corrupt-file tolerance,
#      sibling-key preservation, XDG/isolation discipline.
#   2. HARNESS track (sub-phase 3): synthetic 2,000-card append through the
#      real handler; live widgets ≤ window, disk store keeps all cards,
#      backlog accounting closes.
#   3. INVARIANT track (post-harness): bounded retention on every
#      append-driven surface (architecture.md §Modules/Feed).
#
# ISOLATION: module-scoped autouse fixture redirects XDG_CONFIG_HOME to a
# fresh temp dir and stashes it in _fixture_config_root so the sentinel test
# can prove isolation is ACTIVE (the suite must falsify itself if the fixture
# is removed — it then fails, never silently writes the real config).
# Pattern mirrored from tests/test_error_surfacing.py :119–148.
#
# Scope note (probed): feed-prefs.json is PROJECT-scoped
# (<project>/.crabcakes/feed-prefs.json via utils/feed_store._prefs_path),
# so the accessors are project_path-taking — the "config dir" from the
# instruction sheet is the project .crabcakes dir here; XDG still governs
# ~/.config/crabcakes elsewhere and is exercised by the isolation fixture.
# get_live_window/set_live_window resolve the prefs path at CALL time (no
# import-time resolution), which test_persisted_value_survives_new_process
# guards against regressing.

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from models.feed_card import AutoAcceptPrefs, FeedCardData
from utils import feed_store
from utils.feed_store import (
    LIVE_WINDOW_DEFAULT,
    LIVE_WINDOW_MAX,
    LIVE_WINDOW_MIN,
    _prefs_path,
    get_live_window,
    set_live_window,
)


def _write_prefs(project_path: str, payload) -> str:
    path = _prefs_path(project_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(payload, bytes):
        with open(path, "wb") as f:
            f.write(payload)
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    return path


def _project(tmp_root: str) -> str:
    """Make a throwaway project dir (its .crabcakes is created on demand)."""
    path = os.path.join(tmp_root, "proj")
    os.makedirs(path, exist_ok=True)
    return path


class TestLiveWindowConfig:
    def test_default_window_returns_300(self, tmp_path):
        project = _project(str(tmp_path))
        assert get_live_window(project) == 300 == LIVE_WINDOW_DEFAULT

    def test_get_returns_persisted_value(self, tmp_path):
        project = _project(str(tmp_path))
        set_live_window(project, 120)
        assert get_live_window(project) == 120

    def test_persisted_value_survives_new_process(self, tmp_path):
        """Spawn a fresh interpreter — no shared module state — and prove the
        value comes back from DISK. Guards against a future regression to
        import-time path/env resolution or an in-memory-only cache."""
        project = _project(str(tmp_path))
        set_live_window(project, 120)
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = (
            f"import sys; sys.path.insert(0, r'{repo_root}'); "
            f"from utils import feed_store; "
            f"print(feed_store.get_live_window(r'{project}'))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert result.stdout.strip() == "120"

    def test_set_clamps_low(self, tmp_path):
        project = _project(str(tmp_path))
        set_live_window(project, 25)
        assert get_live_window(project) == 50

    def test_set_clamps_high(self, tmp_path):
        project = _project(str(tmp_path))
        set_live_window(project, 9000)
        assert get_live_window(project) == 5000

    @pytest.mark.parametrize("bad", ["300", 300.5, None])
    def test_set_rejects_non_int(self, tmp_path, bad):
        project = _project(str(tmp_path))
        with pytest.raises(ValueError):
            set_live_window(project, bad)

    @pytest.mark.parametrize("bad", [True, False])
    def test_set_rejects_bool(self, tmp_path, bad):
        """bool is an int subclass — True would coerce to 1 (→ clamp 50) and
        False to 0 (→ clamp 50) without the explicit isinstance-bool guard."""
        project = _project(str(tmp_path))
        with pytest.raises(ValueError):
            set_live_window(project, bad)

    @pytest.mark.parametrize(
        "corrupt",
        [
            b"\x00\x01garbage not json",
            pytest.param(
                ("9" * 5000).encode("ascii"),
                id="huge-int-literal",
                marks=pytest.mark.skipif(
                    sys.version_info < (3, 11),
                    reason="int-max-str-digits limit added in 3.11",
                ),
            ),
        ],
    )
    def test_get_corrupt_prefs_returns_default(self, tmp_path, corrupt):
        """Corrupt bytes AND a >4300-digit int literal (json.loads raises a
        BARE ValueError for the latter — the int-max-str-digits limit — not
        JSONDecodeError; SP1 audit #2, probe G). Both must read as default,
        never escape an exception."""
        project = _project(str(tmp_path))
        _write_prefs(project, corrupt)
        assert get_live_window(project) == 300

    def test_get_non_dict_prefs_returns_default(self, tmp_path):
        project = _project(str(tmp_path))
        _write_prefs(project, [1, 2, 3])
        assert get_live_window(project) == 300

    def test_get_rejects_float_persisted_value(self, tmp_path):
        project = _project(str(tmp_path))
        _write_prefs(project, {"version": 2, "live_window": 250.5})
        assert get_live_window(project) == 300

    def test_get_rejects_bool_persisted_value(self, tmp_path):
        project = _project(str(tmp_path))
        _write_prefs(project, {"version": 2, "live_window": True})
        assert get_live_window(project) == 300

    def test_get_clamps_out_of_range_persisted_value(self, tmp_path):
        project = _project(str(tmp_path))
        _write_prefs(project, {"version": 2, "live_window": 90000})
        assert get_live_window(project) == 5000

    def test_preserves_sibling_keys(self, tmp_path):
        """Read-modify-write, never blind overwrite: a fully-shaped v2
        auto_accept blob must survive a live_window write identical. (A
        PARTIAL blob would be normalized by load_feed_prefs' existing
        defaults-merge on every read — pre-existing module behavior, not a
        live_window concern; see tests/test_feed_handler.py:3821.)"""
        project = _project(str(tmp_path))
        auto = {
            "file_changes": {
                ct: {"enabled": False, "agent_scope": "first_author"}
                for ct in ("diff", "file_created", "file_modified", "file_deleted")
            },
            "exec_command": {"mode": "off", "agent_scope": "first_author"},
            "snoozed_card_ids": ["c1", "c2"],
        }
        _write_prefs(project, {"version": 2, "auto_accept": auto})
        set_live_window(project, 200)
        with open(_prefs_path(project), "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk["auto_accept"] == auto
        assert on_disk["live_window"] == 200

    def test_set_then_load_feed_prefs_still_v2_shaped(self, tmp_path):
        """The write path goes through save_feed_prefs, so the file must stay
        loadable by the auto-accept machinery (version 2 intact)."""
        project = _project(str(tmp_path))
        set_live_window(project, 300)
        assert feed_store.load_feed_prefs(project)["version"] == 2

    def test_handler_save_preserves_live_window(self, tmp_path):
        """SP1 audit #1 REGRESSION test (the clobber: probes A/A2/F).

        set_live_window(150), then simulate the handler's debounced save —
        feed_handler.py:686 writes AutoAcceptPrefs.to_dict(), which emits
        ONLY {version, auto_accept} (probe F verified) — and the window
        must survive. With the pre-fix code this FAILED: save_feed_prefs
        was a blind overwrite, so the handler payload wiped live_window
        off disk. Self-falsifying by design (see falsifier run in report).
        """
        project = _project(str(tmp_path))
        assert set_live_window(project, 150) is True
        handler_payload = AutoAcceptPrefs.from_dict(
            feed_store.load_feed_prefs(project)
        ).to_dict()
        feed_store.save_feed_prefs(project, handler_payload)
        assert get_live_window(project) == 150

    def test_concurrent_set_and_handler_save_no_corruption(self, tmp_path):
        """SP1 audit #1 concurrency probe as a permanent test: one thread
        loops set_live_window, one loops handler-shaped saves. The old
        unlocked writers (shared .tmp path) corrupted the file 1/5 probe
        runs. Post-fix every write lands under the prefs flock, so the file
        must always parse, and any persisted live_window must be a legal
        in-range int (never a bool, never garbage)."""
        project = _project(str(tmp_path))
        errors: list[Exception] = []

        def setter():
            try:
                for i in range(50):
                    if not set_live_window(project, 200 + (i % 100)):
                        errors.append(AssertionError("set_live_window returned False"))
            except Exception as exc:  # noqa: BLE001 — recorded, not raised
                errors.append(exc)

        def handler_saver():
            try:
                for _ in range(50):
                    payload = AutoAcceptPrefs.from_dict(
                        feed_store.load_feed_prefs(project)
                    ).to_dict()
                    feed_store.save_feed_prefs(project, payload)
            except Exception as exc:  # noqa: BLE001 — recorded, not raised
                errors.append(exc)

        t1 = threading.Thread(target=setter)
        t2 = threading.Thread(target=handler_saver)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert errors == []
        with open(_prefs_path(project), "rb") as f:
            raw = f.read()
        on_disk = json.loads(raw)  # parseability IS the corruption assert
        if "live_window" in on_disk:
            value = on_disk["live_window"]
            assert isinstance(value, int) and not isinstance(value, bool)
            assert LIVE_WINDOW_MIN <= value <= LIVE_WINDOW_MAX

    def test_seed_writes_carry_version_marker(self, tmp_path):
        """Micro-fix #6: a fresh-project set_live_window must seed the prefs
        file v2-shaped. Pre-fix it wrote {"live_window": 150} with no
        version key — a shape load_feed_prefs rejects as unknown-version and
        resets, silently discarding the window (existing files keep their
        own version marker: setdefault never overwrites)."""
        project = _project(str(tmp_path))
        assert set_live_window(project, 150) is True
        with open(_prefs_path(project), "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk["version"] == 2
        assert on_disk["live_window"] == 150

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="running as root: file modes are not enforced, the negative "
        "permission test cannot exercise a read-only lock file",
    )
    def test_readonly_lock_file_returns_false_no_raise(self, tmp_path):
        """Micro-fix #7a: an unwritable lock file (or read-only parent) must
        NOT blow through set_live_window's bool contract — PermissionError
        escaped pre-fix (probe-confirmed); post-fix it is an attempt
        failure → 3 attempts → logged error → False."""
        project = _project(str(tmp_path))
        crabcakes = os.path.join(project, ".crabcakes")
        os.makedirs(crabcakes, exist_ok=True)
        lock_file = os.path.join(crabcakes, "feed-prefs.json.lock")
        with open(lock_file, "w", encoding="utf-8"):
            pass
        os.chmod(lock_file, 0o444)
        os.chmod(crabcakes, 0o555)
        try:
            result = set_live_window(project, 150)
        finally:
            os.chmod(crabcakes, 0o755)  # restore BEFORE touching the file
            os.chmod(lock_file, 0o644)
        assert result is False

    def test_project_path_is_a_file_save_skips_no_raise(self, tmp_path):
        """Micro-fix #7b: project_path pointing at a FILE (not a directory)
        makes dir-ensure fail — save_feed_prefs must log + skip, never
        raise (its OSError path now covers the setup stage too)."""
        blocked = os.path.join(str(tmp_path), "blocked-proj")
        with open(blocked, "w", encoding="utf-8") as f:
            f.write("not a directory")
        feed_store.save_feed_prefs(blocked, feed_store._default_prefs())
        assert not os.path.isdir(os.path.join(blocked, ".crabcakes"))

    def test_lock_filename_has_single_suffix(self, tmp_path):
        """Micro-fix #8: the prefs flock file must be feed-prefs.json.lock —
        the pre-fix double suffix (feed-prefs.json.lock.lock) came from
        passing an already-suffixed path to _acquire_lock, which appends
        .lock itself. A stale .lock.lock from earlier runs is deliberately
        NOT migrated or deleted by prod code (harmless unused inode)."""
        project = _project(str(tmp_path))
        set_live_window(project, 150)
        entries = os.listdir(os.path.join(project, ".crabcakes"))
        assert "feed-prefs.json.lock" in entries
        assert "feed-prefs.json.lock.lock" not in entries


class TestIsolationSentinel:
    def test_isolation_active(self):
        """The XDG redirect must be live RIGHT NOW — if the autouse fixture
        is removed, this fails instead of the suite silently writing the real
        ~/.config/crabcakes (self-falsifiable isolation, per instructions)."""
        root = _fixture_config_root
        assert root is not None, (
            "isolation fixture not running — tests would touch real config"
        )
        xdg = os.environ.get("XDG_CONFIG_HOME")
        assert xdg is not None and os.path.isdir(xdg)
        assert os.path.realpath(xdg).startswith(os.path.realpath(root)), (
            f"XDG_CONFIG_HOME={xdg!r} is outside fixture root {root!r}"
        )

    def test_accessor_honors_xdg_env(self, tmp_path):
        """Round-trip within an arbitrary project root: set → read-back →
        on-disk assert. Path resolution is pinned project-scoped by the
        negative pin (test_prefs_path_is_project_scoped); this test makes
        no claim about XDG following either way."""
        second_root = tempfile.mkdtemp(prefix="sp1-xdg-probe-")
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        try:
            os.environ["XDG_CONFIG_HOME"] = second_root
            project = _project(second_root)
            assert get_live_window(project) == 300  # nothing persisted yet
            set_live_window(project, 250)
            with open(_prefs_path(project), "r", encoding="utf-8") as f:
                assert json.load(f)["live_window"] == 250
            assert get_live_window(project) == 250
        finally:
            shutil.rmtree(second_root, ignore_errors=True)  # FIX 5
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg

    def test_prefs_path_is_project_scoped(self):
        """NEGATIVE PIN (SP1 audit #3, inverting the vacuous XDG test): the
        prefs path must be PROJECT-scoped — byte-identical with
        XDG_CONFIG_HOME set and unset. Fails if the accessors ever become
        config-dir-resolved (the original SP1 instruction premise that the
        codebase reality refuted)."""
        project_root = tempfile.mkdtemp(prefix="sp1-negpin-")  # FIX 10
        project = _project(project_root)
        expected = _prefs_path(project)
        assert ".crabcakes" in expected
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        try:
            os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="sp1-negpin-")
            assert _prefs_path(project) == expected
        finally:
            xdg_dir = os.environ["XDG_CONFIG_HOME"]
            shutil.rmtree(project_root, ignore_errors=True)
            shutil.rmtree(xdg_dir, ignore_errors=True)  # FIX 5
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


@pytest.fixture(scope="module", autouse=True)
def _isolated_config_home():
    """Redirect XDG_CONFIG_HOME to a temp dir for the whole module.

    Module-scoped (so monkeypatch can't be used — it is function-scoped);
    manual patch/restore, mirroring tests/test_error_surfacing.py :119–148.
    utils.config.get_config_dir() consults XDG_CONFIG_HOME lazily on every
    call, so setting the env var here covers every accessor call. The tmp
    root is stashed in _fixture_config_root for the sentinel test (round-2
    audit pattern: the suite must be able to detect its own isolation
    regressing — the sentinel fails if the fixture is removed)."""
    global _fixture_config_root
    tmp = tempfile.mkdtemp(prefix="spec03-sp1-test-config-")
    _fixture_config_root = tmp
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = tmp
    yield
    _fixture_config_root = None
    shutil.rmtree(tmp, ignore_errors=True)  # FIX 5: fixture-owned dir, rm it
    if old_xdg is None:
        os.environ.pop("XDG_CONFIG_HOME", None)
    else:
        os.environ["XDG_CONFIG_HOME"] = old_xdg


_fixture_config_root: str | None = (
    None  # set by _isolated_config_home; read by the isolation sentinel test
)


# ── SP2: headless doubles for eviction-cap wiring tests ──────────────────────


class _LiteGLib:
    """MockGLib equivalent (tests/test_feed_handler.py:53-61 pattern):
    idle_add dispatches synchronously so add-driven eviction runs inline."""

    def __init__(self):
        self._pending = []

    def idle_add(self, fn, *args, **kwargs):
        self._pending.append((fn, args, kwargs))
        fn(*args, **kwargs)
        return 0


class _StubWidget:
    """Widget double for the eviction pass: only get_height() is read
    (scroll compensation) and is_above_viewport() takes it opaquely."""

    def __init__(self, height=56):
        self._height = height

    def get_height(self):
        return self._height


class _LiteFeedTab:
    """FeedTab double covering ONLY what set_feed_tab + the eviction pass
    touch. SP2 tests must run headless: the repo's gi/cairo segfault makes
    tests/test_feed_handler.py unrunnable in this env (exit 139 at clean
    HEAD, re-verified this round), so its MockFeedTab cannot be imported
    (that module imports feed_card → real GTK widget builds). _LiteFeedTab
    avoids build_feed_card entirely — no widget is ever constructed."""

    def __init__(self):
        self.removed = []
        self._near_bottom = True
        self._above_viewport = True
        self._card_spacing = 8
        self._vadjustment = None
        self.scroll_to_bottom_calls = 0
        self.batch_callback = None

    # set_feed_tab surface
    def set_batch_accept_callback(self, cb):
        self.batch_callback = cb

    def set_auto_accept_callback(self, cb):
        pass

    # eviction-pass surface
    def is_near_bottom(self, slack: int = 80) -> bool:
        return self._near_bottom

    def is_above_viewport(self, widget) -> bool:
        return self._above_viewport

    def get_vadjustment(self):
        return self._vadjustment

    def get_card_container(self):
        return None  # handler catches (AttributeError, TypeError) → spacing 0

    def remove_card(self, card_id):
        self.removed.append(card_id)

    def prepend_card(self, widget, card_id=None):
        pass

    def schedule_scroll_to_bottom(self):
        self.scroll_to_bottom_calls += 1


class TestEvictionWindowWiring:
    """SPEC-03 SP2 — the eviction cap resolves from the SP1 accessors.

    Headless by construction: _LiteFeedTab + stub widgets + a patched
    Load-More builder, so no real GTK widget is ever built. The eviction
    pass itself is UNCHANGED code under test — only its cap source is
    wired to config (R1/R3/R5)."""

    def _handler(self, project="wiring-proj", register=True):
        from ui.handlers.feed_handler import FeedHandler

        h = FeedHandler(GLib=_LiteGLib(), on_send_to_agent=MagicMock())
        h.set_feed_tab(_LiteFeedTab())
        if register:
            # Direct attribute registration: the R3/R5 helper reads only
            # _project_paths + _active_project_name; driving the full
            # on_project_opened path would drag in loads/prefs/seq-migration
            # (and the GTK-heavy surface this env cannot run).
            h._project_paths[project] = str(self._root / project)
            h._active_project_name = project
        return h

    def _seed(self, h, count, project="wiring-proj", seq_start=1):
        """Seed N live widgets + card data directly (mirrors
        test_feed_handler.TestEvictionSurplus._seed) so the over-cap state
        exists WITHOUT the append path's own eviction running first."""
        ts = datetime.now(UTC)
        ids = []
        for seq in range(seq_start, seq_start + count):
            cid = f"{project}-c{seq}"
            h._cards[cid] = FeedCardData(
                card_type="diff",
                source="agent",
                title=cid,
                body="",
                author="x",
                timestamp=ts.replace(microsecond=seq),
                project_name=project,
                card_id=cid,
                seq_num=seq,
            )
            h._project_cards.setdefault(project, []).insert(0, cid)
            h._card_widgets[cid] = _StubWidget(height=56)
            ids.append(cid)
        h._project_seq[project] = seq_start + count - 1
        return ids

    def _patch_load_more_builder(self, monkeypatch):
        """Replace _build_load_more_widget (real one builds Gtk widgets —
        impossible headless). The patch is an aknowledged test seam: the
        eviction tail's bookkeeping (widget reset, prepend, backlog label)
        still runs against the stub return value."""
        from ui.handlers import feed_handler as fh_mod

        monkeypatch.setattr(
            fh_mod.FeedHandler,
            "_build_load_more_widget",
            lambda self, remaining: _StubWidget(height=10),
        )

    def test_default_effective_cap_is_120(self, tmp_path, monkeypatch):
        """R1 pin (lower bound side): no prefs file → get_live_window
        returns 300 → min(120, 300) = 120. The retention default must
        NEVER raise the widget cap (post-mortem budget)."""
        self._root = tmp_path
        h = self._handler()
        assert feed_store.get_live_window(h._project_paths["wiring-proj"]) == 300
        assert h._effective_live_window() == 120

    def test_configured_below_reduces_cap(self, tmp_path, monkeypatch):
        """R1: a config BELOW the constant lowers the cap — set 80, seed
        120 live widgets (the old constant cap), one pass evicts down
        toward 80 and the post-pass count never exceeds 80."""
        self._root = tmp_path
        h = self._handler()
        project_path = h._project_paths["wiring-proj"]
        assert set_live_window(project_path, 80) is True
        self._patch_load_more_builder(monkeypatch)
        self._seed(h, 120)
        assert len(h._card_widgets) == 120
        h._evict_surplus_card_widgets()
        assert len(h._card_widgets) <= 80
        assert get_live_window(project_path) == 80

    def test_configured_above_never_raises_cap(self, tmp_path, monkeypatch):
        """THE R1 pin: set_live_window(9000) clamps to 5000, yet the widget
        cap STAYS 120 — config can only lower, never raise (post-mortem
        slope 0.82 > 0.5 budget)."""
        self._root = tmp_path
        h = self._handler()
        project_path = h._project_paths["wiring-proj"]
        assert set_live_window(project_path, 9000) is True
        assert get_live_window(project_path) == 5000  # clamp holds (SP1)
        assert h._effective_live_window() == 120
        self._patch_load_more_builder(monkeypatch)
        self._seed(h, 120)
        h._evict_surplus_card_widgets()
        assert len(h._card_widgets) == 120  # at cap, not raised by config

    def test_missing_project_falls_back(self, tmp_path, monkeypatch):
        """R3/R5: no active project path → the helper returns the constant
        without ever touching the accessor."""
        self._root = tmp_path
        h = self._handler(register=False)
        calls = []
        monkeypatch.setattr(
            feed_store,
            "get_live_window",
            lambda p: calls.append(p) or 1,
        )
        assert h._effective_live_window() == 120
        assert calls == [], "accessor must not be consulted without a path"

    def test_get_live_window_exception_falls_back(self, tmp_path, monkeypatch):
        """R5: an accessor explosion must not break the eviction pass —
        cap falls back to 120 and eviction proceeds normally."""
        self._root = tmp_path
        h = self._handler()
        monkeypatch.setattr(
            feed_store,
            "get_live_window",
            lambda p: (_ for _ in ()).throw(RuntimeError("disk smoke")),
        )
        assert h._effective_live_window() == 120
        self._patch_load_more_builder(monkeypatch)
        self._seed(h, 130)
        h._evict_surplus_card_widgets()  # must not raise
        assert len(h._card_widgets) <= 120

    def test_cap_read_per_pass_not_cached(self, tmp_path, monkeypatch):
        """R3: the cap resolves at eviction-call time — lower the config
        BETWEEN passes and the second pass must honor the new value."""
        self._root = tmp_path
        h = self._handler()
        project_path = h._project_paths["wiring-proj"]
        self._patch_load_more_builder(monkeypatch)
        set_live_window(project_path, 100)
        self._seed(h, 110, seq_start=1)
        h._evict_surplus_card_widgets()
        after_first = len(h._card_widgets)
        assert after_first <= 100
        # Re-seed above the NEW cap so the second pass has work to do.
        self._seed(h, 10, seq_start=after_first + 1)
        set_live_window(project_path, 90)
        h._evict_surplus_card_widgets()
        assert len(h._card_widgets) <= 90

    def test_eviction_still_respects_keep_newest(self, tmp_path, monkeypatch):
        """R2: KEEP_NEWEST_CARDS is untouched by config wiring — with cap
        60 (below the KEEP floor of 40's headroom), the newest 40 by
        seq_num must survive every pass."""
        from ui.handlers.feed_handler import KEEP_NEWEST_CARDS

        self._root = tmp_path
        h = self._handler()
        project_path = h._project_paths["wiring-proj"]
        self._patch_load_more_builder(monkeypatch)
        set_live_window(project_path, 60)
        self._seed(h, 100, seq_start=1)
        h._evict_surplus_card_widgets()
        newest = {
            c.card_id
            for c in sorted(
                h._cards.values(), key=lambda c: c.seq_num or 0, reverse=True
            )[:KEEP_NEWEST_CARDS]
        }
        assert newest <= set(h._card_widgets), (
            "the newest KEEP_NEWEST_CARDS must never be evicted (R2)"
        )
        assert len(h._card_widgets) <= 60

    def test_prefs_file_corrupt_falls_back(self, tmp_path, monkeypatch):
        """R3 tolerance: a corrupt prefs file reads as default 300 →
        min(120, 300) = 120; eviction proceeds at the constant cap."""
        self._root = tmp_path
        h = self._handler()
        project_path = h._project_paths["wiring-proj"]
        path = _prefs_path(project_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\\x00\\x01garbage not json")
        assert get_live_window(project_path) == 300  # SP1 tolerance holds
        assert h._effective_live_window() == 120
        self._patch_load_more_builder(monkeypatch)
        self._seed(h, 125)
        h._evict_surplus_card_widgets()
        assert len(h._card_widgets) == 120
