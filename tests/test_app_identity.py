# tests/test_app_identity.py — SPEC-11 SP3: app identity + migration wiring.
#
# RED-first: written before SP3's edits — pyproject still says crabcakes,
# the app id still com.crabcakes.app, the desktop entry doesn't exist, and
# main() runs no v1 migration (tests 4/5 RED on the wiring's absence).
#
# Contract (D1/D5/D3-ordering, docs/specs/phases/SPEC-11-PREFLIGHT-DECISIONS.md):
#   - pyproject: name == develcakes; scripts has develcakes, NOT crabcakes.
#   - main.py: DevelcakesApp + application_id com.develcakes.app; zero
#     CrabcakesApp / com.crabcakes.app anywhere.
#   - Icon: theme-name mechanism preserved (GTK4 has no set_default_icon);
#     name 'develcakes' + theme-named PNGs shipped via pyproject data-files.
#   - Desktop entry: data/com.develcakes.app.desktop (D5, new file).
#   - migrate_v1_config() at top of main() BEFORE DevelcakesApp() (D3
#     ordering), banner deferred to on_activate where the feed handler
#     exists; failure card names failure + v1-untouched.

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAIN = (REPO / "main.py").read_text(encoding="utf-8")


class TestPyprojectIdentity:
    def test_pyproject_name_and_script(self):
        """D1: package name develcakes; develcakes script present; the old
        crabcakes script entry REMOVED (spec explicit — the venv entry
        disappears on reinstall)."""
        data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        assert data["project"]["name"] == "develcakes"
        scripts = data["project"]["scripts"]
        assert "develcakes" in scripts
        assert scripts["develcakes"] == "main:main"
        assert "crabcakes" not in scripts

    def test_icon_theme_packaging_wired(self):
        """D5: the icon ships as theme-named hicolor PNGs via pyproject
        data-files, so set_default_icon_name('develcakes') resolves once
        installed. STRENGTHENED (SP3 fix round, BUG#1): the previous
        ``"icons" in str(...)`` shape was false assurance — it passed while
        data-files installed 16.png..256.png, whose BASENAMES can never
        satisfy a theme lookup for 'develcakes' (auditor wheel-verified:
        zero entries resolve). Now: resolve every data-files source path,
        assert each EXISTS on disk and its BASENAME is develcakes.png."""
        import os

        data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        data_files = (
            data.get("tool", {}).get("setuptools", {}).get("data-files", {})
        )
        # data-files shape: {target_dir: [source, ...]}. Pick mappings whose
        # TARGET is an hicolor/apps install dir — those are the only ones
        # that can resolve a theme lookup for 'develcakes'.
        icon_sources = [
            Path(src)
            for target, sources in data_files.items()
            if "hicolor" in target and "apps" in target
            for src in sources
        ]
        # Every hicolor/apps source must be a real, theme-named PNG.
        assert icon_sources, "no hicolor apps icons mapped in data-files"
        for p in icon_sources:
            assert p.is_file(), f"mapped icon source missing on disk: {p}"
            assert os.path.basename(p) == "develcakes.png", (
                f"theme lookup needs basename develcakes.png, got {p} — "
                "a size-named source cannot satisfy Icon=develcakes"
            )
        # And the tree must mirror what the mapping claims (in-repo copies,
        # not references to the size-named originals).
        for p in icon_sources:
            assert str(p).startswith("icons/hicolor/"), (
                f"icon source must live in the in-repo theme tree: {p}"
            )


class TestMainIdentity:
    def test_main_class_and_app_id(self):
        """D1: DevelcakesApp + com.develcakes.app; zero old-name identity
        strings remain anywhere in main.py."""
        assert "class DevelcakesApp" in MAIN
        assert "application_id='com.develcakes.app'" in MAIN
        assert "CrabcakesApp" not in MAIN
        assert "com.crabcakes.app" not in MAIN

    def test_icon_mechanism_preserved_with_new_name(self):
        """D5 (mechanism-preserving): the theme-NAME mechanism stays (GTK4
        has no Gtk.Window.set_default_icon — verified); the name is the new
        identity. Guards against an accidental GTK3-API revert or an
        old-name icon string."""
        assert "Gtk.Window.set_default_icon_name('develcakes')" in MAIN
        assert "set_default_icon_name('crabcakes')" not in MAIN
        assert "set_default_icon(" not in MAIN.replace(
            "set_default_icon_name", ""
        )


class TestDesktopEntry:
    def test_desktop_entry_exists_and_wellformed(self):
        """D5: data/com.develcakes.app.desktop exists with the required
        keys; Exec/Icon reference the new identity."""
        desk = REPO / "data" / "com.develcakes.app.desktop"
        assert desk.is_file(), "desktop entry missing"
        text = desk.read_text(encoding="utf-8")
        for needle in (
            "[Desktop Entry]",
            "Name=Develcakes",
            "Exec=develcakes",
            "Icon=develcakes",
            "StartupWMClass=develcakes",
        ):
            assert needle in text, f"desktop entry missing {needle!r}"


class TestMigrationWiring:
    def test_migration_wired_before_window(self):
        """D3 ordering (source-pin): the migrate_v1_config CALL STATEMENT
        sits at the top of main() before DevelcakesApp() is constructed.
        STRENGTHENED (SP3 fix round, BUG#2): the previous pin matched the
        COMMENT at main.py:115 ("# ... migrate_v1_config() runs in main()
        ..."), so a reordered pair or a DELETED call both passed — the
        comment survives every mutation. This pin matches the exact
        statement string, asserts the substring exists first (a deleted
        call fails the exists-check with a clear message, not an
        unrelated ValueError), then asserts order."""
        assert (
            "report = migrate_v1_config()" in MAIN
        ), "migration call deleted from main() — the one-time v1 copy is gone"
        assert (
            "app = DevelcakesApp()" in MAIN
        ), "app construction missing"
        call_idx = MAIN.index("report = migrate_v1_config()")
        app_idx = MAIN.index("app = DevelcakesApp()")
        assert call_idx < app_idx, (
            "D3: migration must run BEFORE the app/window build"
        )
        assert "v1 untouched" in MAIN  # failure banner contract
        assert "deferred" in MAIN.lower()  # feed-not-up disclosure in comments

    def test_banner_emitted_on_migration(self):
        """Startup-shaped: a successful report produces a banner card via
        the deferred-emit path (feed not up pre-window); a marker no-op
        (None) produces nothing. Exercises the REAL production entry —
        main(): migrate → park report → DevelcakesApp() → on_activate —
        with only the externals faked (migrate_v1_config, MainWindow
        construction, App.run). The parking, deferral, and emission paths
        are the real production code."""
        import types

        import main as main_mod
        from models.feed_card import FeedCardData

        emitted: list[FeedCardData] = []
        sentinel_window = types.SimpleNamespace(
            present=lambda: None,
            _feed_handler=types.SimpleNamespace(
                add_card=lambda card, persist=True: emitted.append(card)
            ),
        )

        def fake_run(self, argv):
            self.on_activate(self)  # GTK would call this; fake drives it
            return 0

        report = {
            "copied": ["agent.json", "conversations"],
            "skipped": [],
            "failed": [],
        }

        import unittest.mock as um

        with (
            um.patch.object(main_mod, "migrate_v1_config", return_value=report),
            um.patch.object(
                main_mod, "MainWindow", create=True,
                side_effect=lambda *a, **k: sentinel_window,
            ),
            um.patch.object(main_mod.DevelcakesApp, "run", fake_run),
        ):
            main_mod.main()

        assert len(emitted) == 1, "success report must emit exactly one card"
        card = emitted[0]
        assert card.card_type == "system"
        assert card.title == "Config migrated from v1"
        assert "2 copied" in card.body

        # Second run: marker present → migrate returns None → NO new card.
        emitted.clear()
        with (
            um.patch.object(main_mod, "migrate_v1_config", return_value=None),
            um.patch.object(
                main_mod, "MainWindow", create=True,
                side_effect=lambda *a, **k: sentinel_window,
            ),
            um.patch.object(main_mod.DevelcakesApp, "run", fake_run),
        ):
            main_mod.main()

        assert len(emitted) == 0, "no-op migration must not emit a card"

    def test_banner_failure_reports_v1_untouched(self):
        """Failure report → the card names the failure AND says v1 is
        untouched (D3: non-destructive is part of the user-facing contract).
        Driven through main() like the success shape (real parking +
        deferral), externals faked."""
        import types

        import main as main_mod

        emitted: list = []
        sentinel_window = types.SimpleNamespace(
            present=lambda: None,
            _feed_handler=types.SimpleNamespace(
                add_card=lambda card, persist=True: emitted.append(card)
            ),
        )

        def fake_run(self, argv):
            self.on_activate(self)
            return 0

        report = {
            "copied": [],
            "skipped": [],
            "failed": ["config.json: byte-count mismatch after copy: x -> y"],
        }

        import unittest.mock as um

        with (
            um.patch.object(main_mod, "migrate_v1_config", return_value=report),
            um.patch.object(
                main_mod, "MainWindow", create=True,
                side_effect=lambda *a, **k: sentinel_window,
            ),
            um.patch.object(main_mod.DevelcakesApp, "run", fake_run),
        ):
            main_mod.main()

        assert len(emitted) == 1
        body = emitted[0].body
        assert "config.json" in body
        assert "v1 untouched" in body
