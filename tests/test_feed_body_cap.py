# tests/test_feed_body_cap.py
# SPEC-UI-RESPONSIVENESS-2-T2 Phase 1 (Unit D) — rendered body cap + reveal control.
#
# Origin: external live profile (Qrusher 2026-09-12) put pango text measurement at
# 60.1% of main-thread burn; 4 approval cards at 16–39 KB bodies were the worst
# offenders. The fix separates STORAGE from RENDER:
#
#   * storage: full text up to MAX_STORED_BODY (200_000) — feed.json keeps the
#     text, so copy / feed export / audit are lossless within that bound;
#   * render: at most RENDERED_BODY_LIMIT (2_000) chars per card, with an
#     additive `… N more characters` reveal control.
#
# GTK: this suite builds real widgets — run under
# `PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3 -m pytest tests/test_feed_body_cap.py`.

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from models.feed_card import MAX_STORED_BODY, FeedCardData


def _long_body(n: int, marker: str = "") -> str:
    """Deterministic body of EXACTLY `n` characters (newlines included)."""
    unit = "line %05d: echo hello world\n"
    out = []
    total = 0
    i = 0
    while total < n:
        piece = unit % i
        out.append(piece)
        total += len(piece)
        i += 1
    text = "".join(out)[:n]
    if marker:
        text = text[: n - len(marker)] + marker
    return text


# ── Harness (mirrors tests/test_feed_handler.py MockGLib/MockFeedTab) ────────

class MockGLib:
    """GLib double that runs idle_add callbacks immediately."""

    def idle_add(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return 0


class MockFeedTab:
    def __init__(self):
        self.cards = []
        self.replaced = []
        # MEMRATCHET P3 hygiene (audit follow-up): eviction-pass surface.
        # Mirrors tests/test_feed_handler.py MockFeedTab's contract so this
        # duplicated double cannot AttributeError when eviction wires in.
        self._near_bottom = True
        self._above_viewport = True
        self._card_spacing = 8

    def is_near_bottom(self, slack: int = 80) -> bool:
        return self._near_bottom

    def is_above_viewport(self, widget) -> bool:
        return self._above_viewport

    def get_vadjustment(self):
        return None

    def get_card_container(self):
        # Eviction only reads get_spacing() via the handler's
        # _card_container_spacing() helper (which guards AttributeError).
        return self

    def get_spacing(self) -> int:
        return self._card_spacing

    def append_card(self, widget, card_id=None):
        self.cards.append((widget, card_id))

    prepend_card = append_card

    def remove_card(self, card_id):
        self.cards = [(w, c) for w, c in self.cards if c != card_id]

    def show_empty_state(self):
        pass

    def replace_card(self, card_id, new_widget):
        self.replaced.append((card_id, new_widget))
        for i, (w, c) in enumerate(self.cards):
            if c == card_id:
                self.cards[i] = (new_widget, card_id)
                break

    def schedule_scroll_to_bottom(self):
        pass

    def schedule_smart_scroll_to_bottom(self):
        pass

    def update_batch_bar(self, pending_count):
        pass

    def set_batch_accept_callback(self, callback):
        pass

    def update_auto_accept_state(self, active):
        pass

    def set_auto_accept_callback(self, callback):
        pass


def _make_feed_handler():
    from ui.handlers.feed_handler import FeedHandler
    h = FeedHandler(GLib=MockGLib(), on_send_to_agent=MagicMock())
    tab = MockFeedTab()
    h.set_feed_tab(tab)
    return h, tab


def _card(body: str, **overrides) -> FeedCardData:
    fields = dict(
        card_type="agent_action",
        source="agent",
        title="Coder is calling read_file",
        body=body,
        author="Coder",
        timestamp=datetime.now(timezone.utc),
        project_name="proj",
        metadata={"status": "running"},
    )
    fields.update(overrides)
    return FeedCardData(**fields)


# ═══════════════════════════════════════════════════════════════════
#  1. Render cap + exact hidden count + reveal retrieves the full text
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════
#  2. Storage is untouched by rendering
# ═══════════════════════════════════════════════════════════════════

class TestStorageUnaffectedByRender:
    def test_stored_body_unchanged_by_widget_build_and_update(self):
        h, tab = _make_feed_handler()
        full = _long_body(22_714)
        card = _card(full)
        card_id = h.add_card(card)

        stored = h.get_card(card_id)
        assert len(stored.body) == 22_714, "widget build must not touch storage"

        # in-place refresh of the same oversized body (the tool-result shape)
        h.update_card(card_id, stored)
        assert len(h.get_card(card_id).body) == 22_714, (
            "in-place update must not write the rendered/truncated text to storage"
        )
        assert h.get_card(card_id).body == full


# ═══════════════════════════════════════════════════════════════════
#  3. Approval cards: reachable pre-decision, affordance untouched
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════
#  4. In-place update re-evaluates truncation
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════
#  5 + 6. Storage-side cap (the RED-first anchor for Edit C)
# ═══════════════════════════════════════════════════════════════════

def _make_runtime_handler(feed_handler):
    from agent.special_agents import SpecialAgentDef
    from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

    class _FakeGLib:
        def idle_add(self, fn, *args, **kwargs):
            fn(*args, **kwargs)
            return 0

    rt = AgentRuntimeHandler(MagicMock(), MagicMock(), GLib_module=_FakeGLib())
    rt._fh = feed_handler
    rt._active_project = ("proj", "/tmp/proj")
    rt._agents = {
        "agent:coder": SpecialAgentDef(
            conv_id_prefix="agent:coder", display_name="Coder", role="coder",
            emoji="🛠️", tools=["read_file"], can_write=True,
        )
    }
    rt._pending_tool_args = {}
    rt._ended_sessions = set()
    rt._pending_exec_commands = {}
    return rt


class TestStorageCap:
    def test_tool_result_stores_full_output_not_2000(self):
        """RED anchor: today `_do_tool_call_result` stores `output[:2000]`."""
        h, _tab = _make_feed_handler()
        rt = _make_runtime_handler(h)

        card_id = h.add_card(_card("⏳ Running...",
                                   metadata={"status": "running",
                                             "tool_name": "read_file"}))
        rt._tool_card_ids["agent:coder"] = card_id

        output = "O" * 5_000
        rt._do_tool_call_result("agent:coder", "read_file", output, success=True)

        stored = h.get_card(card_id)
        assert len(stored.body) == 5_000, (
            f"tool-result storage still truncates: {len(stored.body)} chars stored"
        )
        assert stored.body == output

    def test_tool_result_storage_cap_200k_with_warning(self, caplog):
        h, _tab = _make_feed_handler()
        rt = _make_runtime_handler(h)

        card_id = h.add_card(_card("⏳ Running...",
                                   metadata={"status": "running",
                                             "tool_name": "read_file"}))
        rt._tool_card_ids["agent:coder"] = card_id

        with caplog.at_level(logging.WARNING):
            rt._do_tool_call_result("agent:coder", "read_file", "Z" * 250_000,
                                    success=True)

        stored = h.get_card(card_id)
        assert len(stored.body) == MAX_STORED_BODY == 200_000
        assert any(
            "storage" in r.getMessage().lower() and "200000" in r.getMessage().replace(",", "")
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_model_storage_cap_applies_to_every_construction_site(self, caplog):
        """The sweep is enforced at the model, so a new `body=` site cannot
        bypass the cap (audit-report / git-stdout / feed_card paths all funnel here)."""
        with caplog.at_level(logging.WARNING):
            card = FeedCardData(
                card_type="audit_report", source="agent", title="Report",
                body="R" * 250_000, author="Debugger",
                timestamp=datetime.now(timezone.utc), project_name="proj",
            )
        assert len(card.body) == MAX_STORED_BODY
        assert any("storage" in r.getMessage().lower() for r in caplog.records)

    def test_approval_card_body_is_capped_at_storage(self):
        card = FeedCardData(
            card_type="agent_action", source="agent",
            title="⚠️ approval", body="$ " + "C" * 250_000, author="Coder",
            timestamp=datetime.now(timezone.utc), project_name="proj",
            metadata={"needs_approval": True},
        )
        assert len(card.body) == MAX_STORED_BODY


# ═══════════════════════════════════════════════════════════════════
#  7. Copy keeps the FULL stored body (Edit D)
# ═══════════════════════════════════════════════════════════════════

class TestCopyPreserved:
    def test_copy_callback_returns_full_stored_body(self):
        h, _tab = _make_feed_handler()
        copied = []
        h.handle_copy = copied.append

        full = _long_body(22_714, marker="FINAL-LINE")
        card = _card(full)
        cb = h._make_copy_cb(card)

        cb()
        assert copied == [full], "copy must carry the stored body, not the rendered text"
        assert len(copied[0]) == 22_714 and copied[0].endswith("FINAL-LINE")


# ═══════════════════════════════════════════════════════════════════
#  Sweep: every other body-bearing card type is capped too
#
#  Ported from the retired GTK widget suite (WU feed-surface chunk 3b). The
#  cap now lives in the pure renderer (render/feed_html.py), so these assert
#  on the rendered HTML instead of a Gtk.Label tree — same contract, no GTK.
# ═══════════════════════════════════════════════════════════════════


def _render(card: FeedCardData) -> str:
    from render.feed_html import render_feed_card

    return render_feed_card(card)


@pytest.mark.parametrize("card_type", [
    "diff", "git_commit", "agent_action", "system", "audit_report",
    "unknown_type", "task", "file_modified",
])
def test_every_body_bearing_card_type_is_capped(card_type):
    """Each body-bearing type truncates at FEED_RENDERED_BODY_LIMIT and offers
    the reveal control, with the EXACT hidden count."""
    from render.feed_html import FEED_RENDERED_BODY_LIMIT

    full = _long_body(6_000)
    out = _render(_card(full, card_type=card_type, file_path="src/foo.py",
                        author="system"))
    hidden = len(full) - FEED_RENDERED_BODY_LIMIT

    assert full[:FEED_RENDERED_BODY_LIMIT] in out, (
        f"{card_type}: the visible body must be the head of the text"
    )
    assert full not in out, f"{card_type}: the full body must not be rendered"
    assert f"… {hidden} more characters" in out, (
        f"{card_type}: an oversized body must offer the reveal control"
    )


def test_git_commit_stdout_body_is_capped():
    """`feed_handler._add_git_card` stores raw `result.stdout` — the unbounded
    producer found by the original sweep."""
    full = _long_body(30_000)
    out = _render(_card(full, card_type="git_commit", author="PM"))
    assert f"… {30_000 - 2000} more characters" in out
    assert full not in out


def test_reveal_round_trips_the_full_body_in_html():
    """The reveal path re-renders the COMPLETE body (escaped), not the slice."""
    from render.feed_html import render_feed_card

    full = _long_body(9_000, marker="FINAL-LINE")
    out = render_feed_card(_card(full), revealed=True)
    assert "more characters" not in out
    assert full.splitlines()[-1] in out
    assert "FINAL-LINE" in out

