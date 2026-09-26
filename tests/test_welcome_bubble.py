# tests/test_welcome_bubble.py — SPEC-11 cosmetic rename round 3.
# Pins the welcome bubble's app-identity title (window.py builds chat tabs
# with this bubble; the bubble builder lives in chat_bubble.py, which
# retires in SP5 — this pin moves with it then). Monkeypatch-free: the
# logo asset ships in-repo (icons/logo-rounded.png), so the builder takes
# its real happy path. Needs a display for widget construction: run under
# xvfb-run like the rest of the GUI suite. No gi import needed — the title
# is located by its CSS class (a GtkWidget method), not by type check,
# which also keeps this file free of the repo's gi/pyright stub artifact.

from ui.views.chat_bubble import build_welcome_bubble


def test_welcome_bubble_renders_develcakes_title():
    """SPEC-11 r3: the welcome bubble's title label carries the new app
    identity. Fails on the pre-rename tree (label was "Crabcakes")."""
    bubble = build_welcome_bubble()
    assert bubble is not None, "logo asset missing — builder took its None path"
    titles = []
    child = bubble.get_first_child()
    while child is not None:
        if "welcome-bubble-title" in child.get_css_classes():
            titles.append(child)
        child = child.get_next_sibling()
    assert len(titles) == 1, f"expected exactly one title label, got {titles}"
    assert titles[0].get_text() == "DevelCakes"
