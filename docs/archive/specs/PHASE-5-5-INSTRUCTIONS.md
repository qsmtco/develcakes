# Phase 5-5 — Cleanup + Tests + Update Existing Tests

> Part of FEED-CARD-UX-PHASE-5 — Persistent Feed Toolbar + Auto-Accept Toggle
> Implements spec Steps 8, 9, 10, 11.

## Before Starting

1. Read the full master spec: `docs/specs/FEED-CARD-UX-PHASE-5-INSTRUCTIONS.md` (especially §3.3, §5.1)
2. Read the steelFramedCodeWriter prompt: `prompts/steelFramedCodeWriter.md`
3. Read the full files you will edit before touching them.

## Files to Change

1. `tests/test_feed_handler.py` — Steps 9 + 10 (add new test class, update existing assertions)
2. `ui/views/feed_tab.py` — Step 8 (verify `_batch_bar` is fully removed)

## Step 8 — Verify `_batch_bar` removal in `ui/views/feed_tab.py`

Run: `grep -n "_batch_bar" ui/views/feed_tab.py`

Expected: no matches. If any matches remain (e.g., in comments or dead code), remove them. If grep is clean, no edit needed.

## Step 9 — Add `TestFeedToolbarAutoAccept` class to `tests/test_feed_handler.py`

Append at the END of the file (after line 1821, before the final newline). The file currently ends with `)` — add a newline, then the new class.

### New test class: `TestFeedToolbarAutoAccept`

```python

# ═══════════════════════════════════════════════════════════════════
#  TestFeedToolbarAutoAccept — Phase 5
#  Tests the auto-accept toggle, warning dialog, and auto-accept card hook.
#  Uses mock_glib + mock_feed_tab fixtures (defined at top of file).
# ═══════════════════════════════════════════════════════════════════

class TestFeedToolbarAutoAccept:
    """Phase 5: auto-accept toggle state, warning dialog, and card hook."""

    def test_default_auto_accept_is_off(self, feed_handler, mock_feed_tab):
        """Fresh handler — auto-accept toggle is OFF."""
        assert mock_feed_tab._auto_accept_active is False

    def test_set_feed_tab_wires_auto_accept_callback(self, feed_handler, mock_feed_tab):
        """set_feed_tab() installs the auto-accept toggle callback on FeedTab."""
        assert mock_feed_tab._auto_accept_callback is not None
        assert callable(mock_feed_tab._auto_accept_callback)

    def test_enable_auto_accept_sets_state(self, feed_handler, mock_glib, mock_feed_tab):
        """Toggling ON without warning callback → _auto_accept_enabled = True."""
        # No set_show_auto_accept_warning wired → falls through to enable path
        feed_handler._on_auto_accept_toggled(True)
        assert feed_handler._auto_accept_enabled is True

    def test_disable_auto_accept_sets_state(self, feed_handler, mock_feed_tab):
        """Toggling OFF → _auto_accept_enabled = False."""
        feed_handler._auto_accept_enabled = True
        feed_handler._on_auto_accept_toggled(False)
        assert feed_handler._auto_accept_enabled is False

    def test_cancel_auto_accept_resets_toggle(self, feed_handler, mock_glib, mock_feed_tab):
        """Warning dialog cancel → toggle snaps back to OFF."""
        # Mock warning callback that immediately invokes on_cancel
        def mock_warning(agent_name, on_confirm, on_cancel):
            on_cancel()

        feed_handler.set_show_auto_accept_warning(mock_warning)
        feed_handler._on_auto_accept_toggled(True)
        # _cancel_auto_accept idle_adds update_auto_accept_state(False)
        # Drain the idle queue
        for fn, args, kwargs in mock_glib._pending:
            fn(*args, **kwargs)
        assert mock_feed_tab._auto_accept_active is False

    def test_add_card_with_auto_accept_on_invokes_handle_accept(
        self, feed_handler, mock_glib, mock_feed_tab, monkeypatch
    ):
        """Auto-accept ON + actionable diff card → handle_accept called via idle_add."""
        feed_handler._auto_accept_enabled = True
        feed_handler._auto_accept_agent = None  # match any author
        feed_handler._active_project_name = "testproj"

        # Track handle_accept calls
        accepted_ids = []
        original_handle_accept = feed_handler.handle_accept
        def mock_handle_accept(card_id):
            accepted_ids.append(card_id)
        monkeypatch.setattr(feed_handler, "handle_accept", mock_handle_accept)

        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="Auto card",
            body="", author="coder", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(card)

        # The auto-accept check runs inside _append (idle_add).
        # handle_accept is also called via idle_add inside _append.
        # Drain the idle queue repeatedly until empty (nested idle_add).
        for _ in range(10):  # max 10 drain rounds
            pending = list(mock_glib._pending)
            mock_glib._pending.clear()
            if not pending:
                break
            for fn, args, kwargs in pending:
                fn(*args, **kwargs)

        assert len(accepted_ids) == 1, f"Expected 1 accept, got {len(accepted_ids)}"

    def test_auto_accept_only_for_actionable_cards(
        self, feed_handler, mock_glib, mock_feed_tab, monkeypatch
    ):
        """Auto-accept ON + tool_result card → handle_accept NOT called."""
        feed_handler._auto_accept_enabled = True
        feed_handler._auto_accept_agent = None
        feed_handler._active_project_name = "testproj"

        accepted_ids = []
        def mock_handle_accept(card_id):
            accepted_ids.append(card_id)
        monkeypatch.setattr(feed_handler, "handle_accept", mock_handle_accept)

        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="tool_result", source="agent", title="Tool result",
            body="", author="coder", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(card)

        for _ in range(10):
            pending = list(mock_glib._pending)
            mock_glib._pending.clear()
            if not pending:
                break
            for fn, args, kwargs in pending:
                fn(*args, **kwargs)

        assert len(accepted_ids) == 0, f"Expected 0 accepts for tool_result, got {len(accepted_ids)}"

    def test_auto_accept_only_for_matching_author_when_persisted(
        self, feed_handler, mock_glib, mock_feed_tab, monkeypatch
    ):
        """Auto-accept ON with agent='coder' → only coder cards auto-accepted."""
        feed_handler._auto_accept_enabled = True
        feed_handler._auto_accept_agent = "coder"
        feed_handler._active_project_name = "testproj"

        accepted_ids = []
        def mock_handle_accept(card_id):
            accepted_ids.append(card_id)
        monkeypatch.setattr(feed_handler, "handle_accept", mock_handle_accept)

        ts = datetime.now(timezone.utc)
        # Card from wrong author → NOT auto-accepted
        card_qa = FeedCardData(
            card_type="diff", source="agent", title="QA card",
            body="", author="qa", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(card_qa)

        # Card from matching author → auto-accepted
        card_coder = FeedCardData(
            card_type="diff", source="agent", title="Coder card",
            body="", author="coder", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(card_coder)

        for _ in range(10):
            pending = list(mock_glib._pending)
            mock_glib._pending.clear()
            if not pending:
                break
            for fn, args, kwargs in pending:
                fn(*args, **kwargs)

        assert len(accepted_ids) == 1, f"Expected 1 accept (coder only), got {len(accepted_ids)}"

    def test_add_card_without_auto_accept_is_passive(
        self, feed_handler, mock_glib, mock_feed_tab, monkeypatch
    ):
        """Auto-accept OFF + actionable diff card → handle_accept NOT called (regression guard)."""
        # _auto_accept_enabled defaults to False
        feed_handler._active_project_name = "testproj"

        accepted_ids = []
        def mock_handle_accept(card_id):
            accepted_ids.append(card_id)
        monkeypatch.setattr(feed_handler, "handle_accept", mock_handle_accept)

        ts = datetime.now(timezone.utc)
        card = FeedCardData(
            card_type="diff", source="agent", title="Normal card",
            body="", author="coder", timestamp=ts, project_name="testproj",
        )
        feed_handler.add_card(card)

        for _ in range(10):
            pending = list(mock_glib._pending)
            mock_glib._pending.clear()
            if not pending:
                break
            for fn, args, kwargs in pending:
                fn(*args, **kwargs)

        assert len(accepted_ids) == 0, f"Expected 0 accepts (auto-accept OFF), got {len(accepted_ids)}"
```

## Step 10 — Update existing `TestBatchAccept` tests

In `tests/test_feed_handler.py`, the `TestBatchAccept` class (starts ~line 1019) has 4 tests that assert on `mock_feed_tab._batch_bar_visible` and `mock_feed_tab._batch_bar_count`. These attrs are still set by MockFeedTab (Phase 2 left them for backward compat), so the tests should still pass as-is.

**Verify first:** Run `pytest tests/test_feed_handler.py -k "TestBatchAccept" -q --tb=short`. If all pass, no update needed (the MockFeedTab still sets those attrs). If any fail, update the failing assertions to use `_batch_button_visible` and `_batch_button_label` instead.

## Step 11 — Run full test suite

Run: `pytest tests/ -q --tb=short`

Expected: all green. The spec says 182 original + 9 new = 191 total in `test_feed_handler.py`.

## Rules

- Use the steelFramedCodeWriter prompt at `prompts/steelFramedCodeWriter.md`
- Do NOT modify `tests/test_low2_file_sandbox.py`
- Do NOT modify any source files other than `ui/views/feed_tab.py` (Step 8 only if grep finds stale refs)
- Report: file locations with line numbers, grep evidence, test results
- Include a COMPLETENESS checklist with evidence for each step

## Verify

1. `grep -n "_batch_bar" ui/views/feed_tab.py` — clean (no matches)
2. `pytest tests/test_feed_handler.py -k "TestFeedToolbarAutoAccept" -q --tb=short` — 9 passed
3. `pytest tests/test_feed_handler.py -k "TestBatchAccept" -q --tb=short` — all pass
4. `pytest tests/test_feed_handler.py -q --tb=short` — all pass (191 total)
5. `pytest tests/ -q --tb=short` — all green
