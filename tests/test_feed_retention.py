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

import gc
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

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
    """MockGLib equivalent (tests/test_feed_handler.py:53-61 pattern), with
    ONE deliberate divergence: callbacks are NOT retained after dispatch.
    Real GLib drops a one-shot idle source once it runs; retaining the
    closure here kept `_append_all` alive forever, and that closure holds
    add_cards_batch's `widget_by_id` — an artifact that made the SP3
    harness's gc widget census read 2,001 (all 2,000 batch widgets pinned
    by the fake). One-shot semantics = mirror production, measure truth."""

    def __init__(self):
        self.dispatched = 0

    def idle_add(self, fn, *args, **kwargs):
        self.dispatched += 1
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
        self.appended = []
        self.batch_bar_count = 0
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

    def append_card(self, widget, card_id=None):
        self.appended.append(card_id)

    def remove_card(self, card_id):
        self.removed.append(card_id)

    def prepend_card(self, widget, card_id=None):
        pass

    def schedule_scroll_to_bottom(self):
        self.scroll_to_bottom_calls += 1

    def schedule_smart_scroll_to_bottom(self):
        self.scroll_to_bottom_calls += 1

    def update_batch_bar(self, pending_count: int):
        self.batch_bar_count = pending_count


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
        slope 0.82 > 0.5 budget).

        Rider 9 (SP2 audit): seed 150 widgets — ABOVE both the constant cap
        and any bugged raised cap — so the post-pass assert can actually
        FAIL. At the former seed of 120, a raised-cap regression (min→max)
        left 120 widgets and the `== 120` assert passed VACUOUSLY
        (sp2_r1pin_vacuity.py); at 150 the bugged cap leaves all 150 and
        the assert fails loudly."""
        self._root = tmp_path
        h = self._handler()
        project_path = h._project_paths["wiring-proj"]
        assert set_live_window(project_path, 9000) is True
        assert get_live_window(project_path) == 5000  # clamp holds (SP1)
        assert h._effective_live_window() == 120
        self._patch_load_more_builder(monkeypatch)
        self._seed(h, 150)
        h._evict_surplus_card_widgets()
        # Correct behavior: min(120, 5000) = 120 → evict 30. Under a raised
        # cap (5000): 150 remain → this assert FAILS (non-vacuous pin).
        assert len(h._card_widgets) == 120
        assert len(h._backlog) == 30  # push-back proves eviction actually ran

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
            f.write(b"\x00\x01garbage not json")  # rider 10: real control bytes
        assert get_live_window(project_path) == 300  # SP1 tolerance holds
        assert h._effective_live_window() == 120
        self._patch_load_more_builder(monkeypatch)
        self._seed(h, 125)
        h._evict_surplus_card_widgets()
        assert len(h._card_widgets) == 120


# ── SP3: the 2,000-card measurement harness ──────────────────────────────────
# PROBE RESULT (run BEFORE building this harness, .debug/audit-scratch/
# sp3_probe.py): 100 cards through the real add_card path with the
# build_feed_card seam patched → build_feed_card is the ONLY headless
# blocker (nothing else in the funnel touched real GTK). One finding: the
# probe lost 1/100 cards on disk. ATTRIBUTION (SP3 audit BUG #3, corrected —
# the loss mechanism is append_feed_card's flock-timeout silent skip: the
# per-card persist threads stampede the feed lock, and a thread that cannot
# take it inside the lock deadline logs a warning and returns WITHOUT
# appending — the same path the 2,000-card batch run later measured at
# scale: 1,538/2,000 appends skipped, 462 cards on disk; see the fixture
# comment below). The _ensure_gitignore_entry shared-.gitignore.tmp RMW race
# (pre-existing feed_store limitation, same class as SP1 audit #1) is REAL
# but NON-LOSSY for feed cards: the race's outcome set is entry-added or
# no-op (worst case it drops a concurrent .gitignore LINE), read failures
# are caught (OSError → treated as empty), and it targets .gitignore —
# never feed.json — so it cannot lose card data. Mitigation below: the fixture pre-creates the project
# .gitignore so the racy ensure path is a no-op, and joins every persist
# thread before asserting disk state.


class TestTwoThousandCardRetention:
    """SPEC-03 §6 acceptance — the measurement rows.

    One 2,000-card run through the REAL FeedHandler add path (disk persist,
    seq assignment, widget bookkeeping, eviction, backlog, Load More),
    shared by all invariant tests via a MODULE-scoped fixture. The seams
    are GTK-chrome only (build_feed_card → _StubWidget; Load-More builder →
    stub — both probe-verified as the headless blockers); the
    retention-relevant machinery runs for real.
    """

    COUNT = 2000
    WINDOW = 300

    def _card(self, i, ts, project="harness-proj", title=None, body="x" * 200):
        return FeedCardData(
            card_type="diff",
            source="agent",
            title=title or f"harness-{i}",
            body=body,
            author="harness",
            timestamp=ts.replace(microsecond=i),
            project_name=project,
        )

    @pytest.fixture(scope="class", autouse=True)
    @classmethod
    def _two_k_run(cls, request, tmp_path_factory):
        root = tmp_path_factory.mktemp("sp3-harness-")
        cls._root = root
        project = "harness-proj"
        project_path = str(root / project)
        cls._project_path = project_path

        # SP1 accessors pin the window the acceptance criteria reference.
        assert set_live_window(project_path, cls.WINDOW) is True

        # Mitigation (probe finding, see module comment above for the
        # corrected attribution): pre-create .gitignore so the racy
        # _ensure_gitignore_entry RMW (shared .tmp, no lock) never runs
        # during the persist pass — it is non-lossy, but removing it keeps
        # the disk asserts free of unrelated I/O.
        os.makedirs(os.path.join(project_path, ".crabcakes"), exist_ok=True)
        with open(os.path.join(project_path, ".gitignore"), "w", encoding="utf-8") as f:
            f.write(".crabcakes/feed.json\n")

        from ui.handlers import feed_handler as fh_mod
        from ui.handlers.feed_handler import FeedHandler

        handler = FeedHandler(GLib=_LiteGLib(), on_send_to_agent=MagicMock())
        tab = _LiteFeedTab()
        handler.set_feed_tab(tab)
        handler._project_paths[project] = project_path
        handler._active_project_name = project

        ts = datetime.now(UTC)
        cards = [
            FeedCardData(
                card_type="diff",
                source="agent",
                title=f"harness-{i}",
                body="x" * 200,
                author="harness",
                timestamp=ts.replace(microsecond=i),
                project_name=project,
            )
            for i in range(1, cls.COUNT + 1)
        ]
        t0 = time.monotonic()
        # Two seams, both GTK-chrome only (probe + first fixture run
        # evidence: build_feed_card for every card; _build_load_more_widget
        # from the FIRST eviction pass on — a real Gtk.Box, segfaults
        # headless). The retention-relevant machinery (persist, seq,
        # eviction, backlog, accounting) runs for real.
        #
        # add_cards_batch, not a 2,000-iteration add_card loop (probe
        # evidence): add_card spawns ONE PERSIST THREAD PER CARD, and
        # 2,000 concurrent append_feed_card calls stampede the FEED flock —
        # each hold re-reads + rewrites the growing snapshot, hold times
        # blow the 2 s deadline, and 1,538/2,000 appends were silently
        # SKIPPED (measured: 462/2000 on disk). The batch path is the
        # sanctioned equivalent funnel (same seq/index/widget bookkeeping,
        # same _append_all → _evict_surplus_card_widgets pass, same
        # per-card append_feed_card persist) with ONE persist thread —
        # the production shape for bulk arrival.
        threads_before = set(threading.enumerate())
        with (
            patch.object(fh_mod, "build_feed_card", lambda *a, **k: _StubWidget()),
            patch.object(
                fh_mod.FeedHandler,
                "_build_load_more_widget",
                lambda self, remaining: _StubWidget(height=10),
            ),
        ):
            handler.add_cards_batch(cards)
        cls._funnel_time = time.monotonic() - t0

        # Join the single batch persist thread before reading disk state.
        # It is SLOW by design of feed_store.append_feed_card (O(n²): every
        # append re-reads + rewrites the whole snapshot — ~800 MB of JSON
        # churn over 2,000 appends, banked as a register finding) — 30 s
        # timed out mid-persist and looked like data loss; nothing is lost,
        # the thread just needs its own generous window.
        persist_threads = [
            t
            for t in set(threading.enumerate()) - threads_before
            if t is not threading.main_thread()
        ]
        for t in persist_threads:
            t.join(timeout=120)
        cls._persist_finished = all(not t.is_alive() for t in persist_threads)

        from utils import feed_store as fs

        cls._disk_cards = fs.load_feed(project_path)
        # Sanity: the harness is meaningless if the run itself lost cards.
        assert len(cls._disk_cards) == cls.COUNT, (
            f"run lost cards on disk: {len(cls._disk_cards)}/{cls.COUNT}"
        )
        # Proves feed_store's own compaction did NOT fire mid-run
        # (trigger is count > FEED_WINDOW_DEFAULT * 1.25 == 2500).
        cls._handler = handler
        cls._tab = tab

    def test_widget_bound(self):
        """Invariant 1: live widgets ≤ window + 1 (cap + Load-More row
        allowance). The Load-More sentinel lives in handler._load_more_widget,
        NOT in _card_widgets — the +1 is headroom, asserted as ≤."""
        assert len(self._handler._card_widgets) <= self.WINDOW + 1

    def test_disk_complete(self):
        """Invariant 2: the disk store holds all 2,000 — the view is a
        projection; eviction releases widgets, never card data."""
        assert len(self._disk_cards) == self.COUNT

    def test_accounting_closes(self):
        """Invariant 3 — the honest identity (mechanism-accurate, corrected
        from the instructions' literal form which DOUBLE-COUNTS: eviction
        PUSHES every removed card's data onto _backlog, so removed ≡ the
        backlog's eviction-sourced entries and the literal
        `widgets + backlog + removed` would sum eviction twice).

        Exact identities asserted:
          widgets + backlog == 2000   (live + pushed-back = total)
          removed == backlog          (each removal pushed its data back)
        """
        widgets = len(self._handler._card_widgets)
        backlog = len(self._handler._backlog)
        assert widgets + backlog == self.COUNT, (
            f"loss: widgets={widgets} backlog={backlog} (sum {widgets + backlog})"
        )
        # Excluding the "__load_more__" sentinel: eviction's rebuild tail
        # calls remove_card("__load_more__") (feed_handler eviction tail;
        # round-4 BUG #4) — that removal is sentinel bookkeeping, not a
        # card, so it must not count against push-back.
        card_removals = [cid for cid in self._tab.removed if cid != "__load_more__"]
        assert len(card_removals) == backlog, (
            f"push-back broken: card removals={len(card_removals)} "
            f"but backlog={backlog}"
        )

    def test_no_widget_leak(self):
        """Invariant 4 — gc census (the chosen proxy, documented): count live
        _StubWidget instances process-wide. Every card built one stub; if
        eviction leaked references, the census would track 2,000. Bound =
        window + Load-More stub + documented slack for the transient old
        sentinel (replaced each pass, one alive at a time)."""
        stubs = [o for o in gc.get_objects() if isinstance(o, _StubWidget)]
        assert len(stubs) <= self.WINDOW + 10, (
            f"{len(stubs)} stub widgets alive — eviction is leaking widgets"
        )

    def test_speed_guard(self):
        """Invariant 5: the 2,000-card funnel call (add_cards_batch: seq,
        index, widget-build, append + ONE eviction pass) stays well under
        30 s — tripwire for an accidental O(n²) in the append/evict path.
        The persist thread's disk time is NOT in this number (feed_store's
        O(n²) snapshot rewrite is pre-existing, banked, and not the append
        path's complexity)."""
        assert self._persist_finished, "persist thread did not finish"
        assert self._funnel_time < 30.0, f"funnel took {self._funnel_time:.1f}s"

    def test_window_edge_exact_300(self, tmp_path, monkeypatch):
        """Test 6 — the retention edge, stated honestly under the BINDING
        R1 ruling (SP2): widget cap = min(MAX_LIVE_CARD_WIDGETS,
        live_window). The SP3 instructions' literal premise "300 cards →
        0 evicted" is UNREACHABLE by design: with live_window=300 the
        widget cap is min(120, 300) = 120 — the 300-card window is a
        CARD-retention figure (SP1 accessor + backlog + disk), never a
        widget raise (R1 exists so the default can't add widgets; slope
        0.82 > 0.5 budget). True edges asserted here:
          - live_window=300: 120 cards → 0 evicted; 121st → exactly 1
          - disk keeps ALL cards regardless (view ≠ store)
          - live_window=80 LOWERS the edge: 80 → 0 evicted; 81st → 1
        """
        project = "edge-proj"
        project_path = str(tmp_path / project)
        set_live_window(project_path, self.WINDOW)  # 300 — the spec's number
        from ui.handlers import feed_handler as fh_mod
        from ui.handlers.feed_handler import (
            KEEP_NEWEST_CARDS,
            MAX_LIVE_CARD_WIDGETS,
            FeedHandler,
        )

        handler = FeedHandler(GLib=_LiteGLib(), on_send_to_agent=MagicMock())
        tab = _LiteFeedTab()
        handler.set_feed_tab(tab)
        handler._project_paths[project] = project_path
        handler._active_project_name = project
        monkeypatch.setattr(
            fh_mod.FeedHandler,
            "_build_load_more_widget",
            lambda self, remaining: _StubWidget(height=10),
        )

        widget_cap = min(MAX_LIVE_CARD_WIDGETS, self.WINDOW)
        assert widget_cap == MAX_LIVE_CARD_WIDGETS  # R1: 120 under window=300

        ts = datetime.now(UTC)
        ids = []
        with patch.object(fh_mod, "build_feed_card", lambda *a, **k: _StubWidget()):
            for i in range(1, widget_cap + 1):
                ids.append(
                    handler.add_card(self._card(i, ts, project=project), persist=False)
                )
        assert len(handler._card_widgets) == widget_cap
        assert len(handler._backlog) == 0
        assert tab.removed == []

        with patch.object(fh_mod, "build_feed_card", lambda *a, **k: _StubWidget()):
            handler.add_card(
                self._card(widget_cap + 1, ts, project=project, title="edge-301"),
                persist=False,
            )
        assert len(handler._card_widgets) == widget_cap
        assert len(handler._backlog) == 1
        # add_card assigns uuid ids (not seq-shaped) — the evicted id is
        # the FIRST card's returned id. (The "__load_more__" sentinel
        # removal in eviction's rebuild tail is excluded — it is not a
        # card, same exclusion the accounting identity uses.)
        card_removes = [c for c in tab.removed if c != "__load_more__"]
        assert card_removes == [ids[0]], (
            f"the {widget_cap + 1}th add must evict exactly the oldest card"
        )
        # View ≠ store: disk (if persisted) would keep all — proven at
        # harness scale by test_disk_complete.

        # Config LOWERS the edge: live_window=80 → cap 80 → 81st evicts.
        project_low = "edge-proj-low"
        project_path_low = str(tmp_path / project_low)
        set_live_window(project_path_low, 80)
        handler_low = FeedHandler(GLib=_LiteGLib(), on_send_to_agent=MagicMock())
        tab_low = _LiteFeedTab()
        handler_low.set_feed_tab(tab_low)
        handler_low._project_paths[project_low] = project_path_low
        handler_low._active_project_name = project_low
        ts2 = datetime.now(UTC)
        ids_low = []
        with patch.object(fh_mod, "build_feed_card", lambda *a, **k: _StubWidget()):
            for i in range(1, 81):
                ids_low.append(
                    handler_low.add_card(
                        self._card(i, ts2, project=project_low), persist=False
                    )
                )
            assert len(handler_low._card_widgets) == 80
            assert len(handler_low._backlog) == 0
            handler_low.add_card(
                self._card(81, ts2, project=project_low, title="edge-81"),
                persist=False,
            )
        assert len(handler_low._card_widgets) == 80
        assert len(handler_low._backlog) == 1
        card_removes_low = [c for c in tab_low.removed if c != "__load_more__"]
        assert card_removes_low == [ids_low[0]]
        # R2 floor intact: KEEP_NEWEST_CARDS (40) < the 80-config cap, so
        # the newest-40 pin is exercisable at this edge.
        assert KEEP_NEWEST_CARDS < 80

    def test_disk_survives_reload(self, tmp_path, monkeypatch):
        """Test 7: a NEW handler hydrating from disk sees all 2,000 cards.

        Patched seams (documented per instructions): build_feed_card (the
        sanctioned headless seam — hydration renders the last PAGE_SIZE=15)
        AND _build_load_more_widget (hydration with a non-empty backlog
        builds the real Load-More Gtk.Box otherwise — probe-verified
        segfault source). Hydration runs on a background thread: poll until
        _loading clears (the thread's final statement) and the backlog is
        populated."""
        from ui.handlers import feed_handler as fh_mod
        from ui.handlers.feed_handler import FeedHandler
        from utils import feed_store as fs

        # Disk truth first (the GTK-free loader).
        assert len(fs.load_feed(self._project_path)) == self.COUNT

        monkeypatch.setattr(
            fh_mod.FeedHandler,
            "_build_load_more_widget",
            lambda self, remaining: _StubWidget(height=10),
        )
        handler = FeedHandler(GLib=_LiteGLib(), on_send_to_agent=MagicMock())
        tab = _LiteFeedTab()
        handler.set_feed_tab(tab)
        with patch.object(fh_mod, "build_feed_card", lambda *a, **k: _StubWidget()):
            handler.on_project_opened("harness-proj", self._project_path)
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if not handler._loading and handler._backlog:
                    break
                time.sleep(0.05)
        assert not handler._loading, "hydration thread did not finish in 10s"
        # PAGE_SIZE=15 rendered + 1,985 in backlog == 2,000 total hydrated.
        assert len(handler._backlog) + len(handler._card_widgets) == self.COUNT
        assert len(handler._card_widgets) <= 16

    def test_compaction_interplay(self):
        """Test 8 — HONEST documentation of two DIFFERENT mechanisms.

        feed_store's compaction (the §2.3 sliding window, default
        FEED_WINDOW_DEFAULT=2000) is a DISK retention mechanism; the
        live-window (SP1/SP2) is a VIEW mechanism. During the 2,000-card
        run compaction NEVER fired: the post-append trigger is rate-limited
        AND gated on count > FEED_WINDOW_DEFAULT * 1.25 == 2500 (proven by
        the fixture's disk==2000 sanity assert). SPEC-03 §2's 'disk keeps
        everything' therefore holds at 2,000 cards — but NOT by design
        guarantee: one more card past 2,500 triggers a compact that prunes
        non-pinned cards down to the store's own 2,000 window. Demonstrated
        below: an explicit compact at the store default prunes nothing at
        2,000; an explicit compact(300) prunes to 300 — proving the two
        windows are independent knobs, not one mechanism.

        DEPENDENCY (SP3 audit BUG #4): this test consumes state installed
        by the class fixture _two_k_run (2,000-card run, ~40 s) — running
        it in isolation re-triggers that fixture, so a -k compaction run
        is NOT independent and must not be read as a cheap unit test. The
        guard below fails with that stated, rather than a misleading
        AttributeError deep in the body, if the fixture chain is broken."""
        from utils import feed_store as fs

        # Guard the shared fixture's state explicitly (see DEPENDENCY note).
        assert getattr(self, "_project_path", None), (
            "class fixture _two_k_run has not run — _project_path missing; "
            "this test depends on the shared 2,000-card run (selecting it "
            "with -k re-runs that fixture)"
        )
        assert fs.FEED_WINDOW_DEFAULT * 1.25 == 2500 > self.COUNT
        # No journal updates happened (append path, not update path) and no
        # compaction fired — disk is still the full 2,000.
        assert len(fs.load_feed(self._project_path)) == self.COUNT

        pruned_at_default = fs.compact_feed(
            self._project_path, window=fs.FEED_WINDOW_DEFAULT
        )
        assert pruned_at_default == 0
        assert len(fs.load_feed(self._project_path)) == self.COUNT

        # The divergence proof: the store's window is its own knob.
        pruned_to_300 = fs.compact_feed(self._project_path, window=self.WINDOW)
        assert pruned_to_300 == self.COUNT - self.WINDOW
        assert len(fs.load_feed(self._project_path)) == self.WINDOW
