# ui/views/agent_dashboard.py — collapsible agent dashboard (SPEC-AGENT-DASHBOARD §2.2)
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ui.agent_dashboard_model import collapsed_summary

_COLLAPSED_LABEL = "▶ Agents"
_EXPANDED_LABEL = "▼ Agents"
_ZONE_HIGH = "context-meter-high"
_ZONE_MEDIUM = "context-meter-medium"
_ZONE_LOW = "context-meter-low"


def _percent_display(pct: Any) -> tuple[str, str | None, float]:
    """(label, zone css class or None, fraction 0..1) for a percent value."""
    if (
        pct is None
        or isinstance(pct, bool)
        or not isinstance(pct, (int, float))
        or pct != pct  # noqa: PLR0124 — NaN check
        or pct < 0
    ):
        return "—", None, 0.0
    fraction = min(pct / 100.0, 1.0)
    if pct >= 90.0:
        zone = _ZONE_HIGH
    elif pct >= 70.0:
        zone = _ZONE_MEDIUM
    else:
        zone = _ZONE_LOW
    return f"{pct:.0f}%", zone, fraction


class AgentDashboard(Gtk.Box):
    """Collapsible agent panel; collapsed by default."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("agent-dashboard")

        self._expanded = False
        self._on_compact: Callable[[str], None] | None = None
        self._on_clear: Callable[[str], None] | None = None

        self._toggle_btn = Gtk.Button(label=_COLLAPSED_LABEL)
        self._toggle_btn.add_css_class("flat")
        self._toggle_btn.set_focus_on_click(False)
        self._toggle_btn.connect("clicked", lambda _b: self.toggle())

        self._summary_lbl = Gtk.Label(label="No agents", xalign=0)
        self._summary_lbl.set_hexpand(True)
        self._summary_lbl.set_halign(Gtk.Align.START)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.add_css_class("agent-dashboard-header")  # SPEC §2.2
        header.append(self._toggle_btn)
        header.append(self._summary_lbl)
        self.append(header)

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_max_content_height(200)
        self._scroll.set_propagate_natural_height(True)
        self._scroll.set_visible(False)
        self._inner_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=4
        )
        self._scroll.set_child(self._inner_box)
        self.append(self._scroll)

    # -- callbacks --

    def set_on_compact(self, cb: Callable[[str], None] | None) -> None:
        self._on_compact = cb

    def set_on_clear(self, cb: Callable[[str], None] | None) -> None:
        self._on_clear = cb

    # -- state --

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._apply_expanded_state()

    def _apply_expanded_state(self) -> None:
        self._toggle_btn.set_label(
            _EXPANDED_LABEL if self._expanded else _COLLAPSED_LABEL
        )
        self._scroll.set_visible(self._expanded)

    def set_summary(self, text: str) -> None:
        self._summary_lbl.set_text(text)

    # -- rows --

    def set_rows(self, rows: Iterable[dict]) -> None:
        while (child := self._inner_box.get_first_child()) is not None:
            self._inner_box.remove(child)
        rows = [r for r in rows if isinstance(r, dict)]
        self.set_summary(collapsed_summary(rows))
        for row in rows:
            self._inner_box.append(self._build_row(row))

    def _build_row(self, row: dict) -> Gtk.Box:
        name = str(row.get("name", "Agent"))
        session_key = row.get("session_key", "")
        busy = bool(row.get("busy"))

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row_box.add_css_class("agent-dashboard-row")  # SPEC §2.2

        name_lbl = Gtk.Label(label=name, xalign=0)
        name_lbl.set_size_request(120, -1)
        name_lbl.set_halign(Gtk.Align.START)
        row_box.append(name_lbl)

        pct_text, zone, fraction = _percent_display(row.get("usage_percent"))
        pct_lbl = Gtk.Label(label=pct_text, xalign=0)
        pct_lbl.set_size_request(48, -1)
        row_box.append(pct_lbl)

        progress = Gtk.ProgressBar()
        progress.set_size_request(80, 6)
        progress.set_fraction(fraction)
        if zone is not None:
            progress.add_css_class(zone)
        row_box.append(progress)

        # SPEC §2.2: state derives from `busy`, never a row key.
        state_lbl = Gtk.Label(
            label="working" if busy else "idle", xalign=0
        )
        state_lbl.set_size_request(64, -1)
        row_box.append(state_lbl)

        # SPEC §2.2: Compact stays clickable while busy — the window's
        # handler derives protect_open_turn from the loop state.
        compact_btn = Gtk.Button(label="Compact")
        compact_btn.add_css_class("flat")
        compact_btn.set_focus_on_click(False)
        compact_btn.connect(
            "clicked",
            lambda _b: self._emit_cb(self._on_compact, session_key),
        )
        row_box.append(compact_btn)

        clear_btn = Gtk.Button(label="Clear")
        clear_btn.add_css_class("flat")
        clear_btn.set_focus_on_click(False)
        clear_btn.set_sensitive(not busy)
        clear_btn.connect(
            "clicked",
            lambda _b: self._emit_cb(self._on_clear, session_key),
        )
        row_box.append(clear_btn)

        return row_box

    def _emit_cb(self, cb: Callable[[str], None] | None, key: str) -> None:
        if cb is not None:
            cb(key)