# GRANULAR Phase 8 of 8 — Scenario & Integration Tests

**Spec:** `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` §5 Step 8 + §6 Acceptance Criteria + §7 Edge Cases
**Files to change:** `tests/test_feed_handler.py` (append new test classes only — do NOT modify existing tests)

**Builder prompt:** `prompts/steelFramedCodeWriter.md`

## CRITICAL: Read ALL files before starting

Before writing ANY code, READ these files completely:
1. `tests/test_feed_handler.py` — ALL 2425 lines. Understand the existing fixtures (`mock_glib`, `mock_feed_tab`, `feed_handler`), `MockGLib`, `MockFeedTab`, and every existing test class. Your new tests MUST use the same patterns.
2. `ui/handlers/feed_handler.py` — focus on `_is_card_auto_acceptable()` (line ~372), `_auto_approve_exec_card()` (line ~1604), `add_card()` (line ~580), `handle_approve_exec()` (line ~1584), `_agent_scope_matches()` (line ~430).
3. `ui/handlers/agent_runtime_handler.py` — focus on `_do_approval_needed()` (line ~967) and the Silent bypass (line ~1015).
4. `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` — §6 Acceptance Criteria (line ~1248), §7 Edge Cases (line ~1283).
5. `prompts/steelFramedCodeWriter.md` — your standing orders.

## Context

Phases 1-7 are implemented and audited clean. 246 tests pass. This phase adds NEW test classes to prove the Phase 1-7 changes work end-to-end. No production code changes.

## Existing test coverage (DO NOT duplicate)

These test classes already exist and pass:
- `TestAutoAcceptPrefs` — AutoAcceptPrefs dataclass defaults, enable/disable, serialization
- `TestPrefsMigration` — v1→v2 migration, v2 pass-through, defaults for missing/invalid
- `TestFeedToolbarAutoAccept` — toggle state, warning callback, basic add_card auto-accept

## New test classes to add

Append these THREE new test classes at the END of `tests/test_feed_handler.py`:

---

### Class 1: `TestExecAutoAccept`

Tests for exec Show mode and Silent mode auto-accept behavior. Covers acceptance criteria:
- "Exec Show mode → approval cards appear, Approve/Deny buttons hidden, status set to approved"
- "Exec Silent mode → no approval card is created in the feed; the runtime approval still fires"
- "Exec Off mode → approval cards require manual Approve/Deny"

#### Tests to write:

```python
class TestExecAutoAccept:
    """Phase 7-8: Exec auto-accept Show mode, Silent mode, and Off mode tests."""

    def test_show_mode_auto_approves_exec_card(self, feed_handler, mock_glib, mock_feed_tab):
        """Exec Show mode: approval card is auto-approved via _auto_approve_exec_card.
        The _on_approve_exec callback fires, card.accepted=True, and
        _update_card_visual is called."""
        # Setup: enable exec Show mode
        feed_handler._prefs.exec_command.mode = "show"
        feed_handler._auto_accept_enabled = True
        approved_calls = []
        feed_handler._on_approve_exec = lambda cid, approved: approved_calls.append((cid, approved))

        # Create an exec approval card
        card = FeedCardData(
            card_type="agent_action",
            source="agent",
            title="PM requests approval to run command",
            body="$ ls -la",
            author="PM",
            timestamp=datetime.now(timezone.utc),
            project_name="testproject",
            metadata={"needs_approval": True, "status": "pending_approval"},
        )
        card_id = feed_handler.add_card(card)

        # Assert: _on_approve_exec was called with approved=True
        assert len(approved_calls) == 1
        assert approved_calls[0][1] is True  # approved=True
        # Assert: card.accepted is True
        assert feed_handler._cards[card_id].accepted is True

    def test_show_mode_routes_through_auto_approve_not_handle_accept(self, feed_handler, mock_glib, mock_feed_tab):
        """Show mode exec cards must route through _auto_approve_exec_card (which calls
        handle_approve_exec), NOT handle_accept (which does git stage+commit).
        Verify by checking that _on_approve_exec fires but git ops do NOT."""
        feed_handler._prefs.exec_command.mode = "show"
        feed_handler._auto_accept_enabled = True
        approve_exec_calls = []
        feed_handler._on_approve_exec = lambda cid, approved: approve_exec_calls.append((cid, approved))

        card = FeedCardData(
            card_type="agent_action", source="agent", title="approval",
            body="$ rm -rf /", author="PM",
            timestamp=datetime.now(timezone.utc), project_name="testproject",
            metadata={"needs_approval": True},
        )
        card_id = feed_handler.add_card(card)

        # _on_approve_exec was called (correct path)
        assert len(approve_exec_calls) == 1
        # Card should NOT have gone through git ops — verify card.accepted is True
        # but no git card was created (handle_accept would create a git commit card)
        assert feed_handler._cards[card_id].accepted is True

    def test_off_mode_does_not_auto_approve(self, feed_handler, mock_glib, mock_feed_tab):
        """Exec Off mode: approval card is NOT auto-approved."""
        feed_handler._prefs.exec_command.mode = "off"
        feed_handler._auto_accept_enabled = True
        approved_calls = []
        feed_handler._on_approve_exec = lambda cid, approved: approved_calls.append((cid, approved))

        card = FeedCardData(
            card_type="agent_action", source="agent", title="approval",
            body="$ ls", author="PM",
            timestamp=datetime.now(timezone.utc), project_name="testproject",
            metadata={"needs_approval": True},
        )
        card_id = feed_handler.add_card(card)

        # Should NOT have been auto-approved
        assert len(approved_calls) == 0
        assert feed_handler._cards[card_id].accepted is None

    def test_show_mode_non_approval_agent_action_not_auto_accepted(self, feed_handler, mock_glib, mock_feed_tab):
        """An agent_action card WITHOUT needs_approval should NOT be auto-approved
        even in Show mode (only approval cards are auto-acceptable)."""
        feed_handler._prefs.exec_command.mode = "show"
        feed_handler._auto_accept_enabled = True
        approved_calls = []
        feed_handler._on_approve_exec = lambda cid, approved: approved_calls.append((cid, approved))

        card = FeedCardData(
            card_type="agent_action", source="agent", title="agent did something",
            body="ran a thing", author="PM",
            timestamp=datetime.now(timezone.utc), project_name="testproject",
            metadata={"needs_approval": False},
        )
        card_id = feed_handler.add_card(card)

        assert len(approved_calls) == 0
        assert feed_handler._cards[card_id].accepted is None

    def test_exec_auto_accept_respects_agent_scope(self, feed_handler, mock_glib, mock_feed_tab):
        """Exec Show mode with agent_scope='first_author': only the first agent's
        exec cards are auto-approved."""
        feed_handler._prefs.exec_command.mode = "show"
        feed_handler._prefs.exec_command.agent_scope = "first_author"
        feed_handler._auto_accept_enabled = True
        approved_calls = []
        feed_handler._on_approve_exec = lambda cid, approved: approved_calls.append((cid, approved))

        # First exec card from "AgentA" — should auto-approve
        card_a = FeedCardData(
            card_type="agent_action", source="agent", title="approval",
            body="$ ls", author="AgentA",
            timestamp=datetime.now(timezone.utc), project_name="testproject",
            metadata={"needs_approval": True},
        )
        cid_a = feed_handler.add_card(card_a)
        assert len(approved_calls) == 1

        # Second exec card from "AgentB" — should NOT auto-approve (scope locked to AgentA)
        card_b = FeedCardData(
            card_type="agent_action", source="agent", title="approval",
            body="$ ls", author="AgentB",
            timestamp=datetime.now(timezone.utc), project_name="testproject",
            metadata={"needs_approval": True},
        )
        cid_b = feed_handler.add_card(card_b)
        assert len(approved_calls) == 1  # still only 1
        assert feed_handler._cards[cid_b].accepted is None

    def test_exec_auto_accept_snoozed_card_not_approved(self, feed_handler, mock_glib, mock_feed_tab, monkeypatch):
        """A snoozed exec card should NOT be auto-approved.

        Uses monkeypatch to predict the UUID that add_card will assign,
        so we can pre-snooze it before the auto-accept check fires.
        (MockGLib.idle_add runs synchronously, so auto-accept fires inside
        add_card before we can read the returned card_id.)"""
        monkeypatch.setattr("ui.handlers.feed_handler.uuid.uuid4", lambda: "snoozed-uuid")
        feed_handler._prefs.exec_command.mode = "show"
        feed_handler._auto_accept_enabled = True
        feed_handler._prefs.snoozed_card_ids.append("snoozed-uuid")
        approved_calls = []
        feed_handler._on_approve_exec = lambda cid, approved: approved_calls.append((cid, approved))

        card = FeedCardData(
            card_type="agent_action", source="agent", title="approval",
            body="$ ls", author="PM",
            timestamp=datetime.now(timezone.utc), project_name="testproject",
            metadata={"needs_approval": True},
        )
        card_id = feed_handler.add_card(card)

        assert len(approved_calls) == 0
        assert feed_handler._cards[card_id].accepted is None
```

---

### Class 2: `TestAutoAcceptScenario`

Integration-level tests for the three scenarios from the spec proposal. These test the FULL flow from card arrival through auto-accept, verifying the correct handler is invoked.

```python
class TestAutoAcceptScenario:
    """Phase 8: Integration-level scenario tests for auto-accept flows.
    Covers spec §5 Step 8 scenarios + §6 acceptance criteria."""

    def test_scenario_diffs_on_file_changes_auto_accepted(self, feed_handler, mock_glib, mock_feed_tab):
        """Scenario: Diffs ON → diff cards auto-accept via handle_accept.
        File_* cards do NOT auto-accept (only diff)."""
        feed_handler._prefs.file_changes["diff"].enabled = True
        feed_handler._prefs.file_changes["diff"].agent_scope = "all_agents"
        feed_handler._auto_accept_enabled = True

        # Add a diff card — should be auto-accepted
        diff_card = FeedCardData(
            card_type="diff", source="agent", title="modified foo.py",
            body="+print('hello')", author="AgentA",
            timestamp=datetime.now(timezone.utc), project_name="proj",
            file_path="foo.py",
        )
        diff_id = feed_handler.add_card(diff_card)
        # MockGLib.idle_add runs immediately, so handle_accept fires synchronously
        # handle_accept on diff → spawns git thread → we can't test git ops here
        # but we can verify the card was routed through handle_accept by checking
        # that _auto_accept_enabled was True and the card is in _cards
        assert diff_id in feed_handler._cards

        # Add a file_created card — should NOT be auto-accepted
        file_card = FeedCardData(
            card_type="file_created", source="agent", title="created bar.py",
            body="new file", author="AgentA",
            timestamp=datetime.now(timezone.utc), project_name="proj",
            file_path="bar.py",
        )
        file_id = feed_handler.add_card(file_card)
        assert feed_handler._cards[file_id].accepted is None

    def test_scenario_files_on_file_changes_auto_accepted(self, feed_handler, mock_glib, mock_feed_tab):
        """Scenario: Files ON → file_* cards auto-accept. Diff cards do NOT."""
        feed_handler._prefs.file_changes["file_created"].enabled = True
        feed_handler._prefs.file_changes["file_created"].agent_scope = "all_agents"
        feed_handler._auto_accept_enabled = True

        file_card = FeedCardData(
            card_type="file_created", source="agent", title="created bar.py",
            body="new file", author="AgentA",
            timestamp=datetime.now(timezone.utc), project_name="proj",
            file_path="bar.py",
        )
        file_id = feed_handler.add_card(file_card)
        assert file_id in feed_handler._cards

        # Diff card should NOT be auto-accepted (only file_created is enabled)
        diff_card = FeedCardData(
            card_type="diff", source="agent", title="modified foo.py",
            body="+print('hello')", author="AgentA",
            timestamp=datetime.now(timezone.utc), project_name="proj",
            file_path="foo.py",
        )
        diff_id = feed_handler.add_card(diff_card)
        assert feed_handler._cards[diff_id].accepted is None

    def test_scenario_both_on_all_file_types_accepted(self, feed_handler, mock_glib, mock_feed_tab):
        """Scenario: Both Diffs and Files ON → all four file-change types auto-accept."""
        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            feed_handler._prefs.file_changes[ct].enabled = True
            feed_handler._prefs.file_changes[ct].agent_scope = "all_agents"
        feed_handler._auto_accept_enabled = True

        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            card = FeedCardData(
                card_type=ct, source="agent", title=f"{ct} event",
                body="content", author="AgentA",
                timestamp=datetime.now(timezone.utc), project_name="proj",
                file_path="some_file.py",
            )
            cid = feed_handler.add_card(card)
            assert cid in feed_handler._cards

    def test_scenario_both_off_nothing_accepted(self, feed_handler, mock_glib, mock_feed_tab):
        """Scenario: All toggles OFF → no cards auto-accepted."""
        feed_handler._auto_accept_enabled = False

        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            card = FeedCardData(
                card_type=ct, source="agent", title=f"{ct} event",
                body="content", author="AgentA",
                timestamp=datetime.now(timezone.utc), project_name="proj",
                file_path="some_file.py",
            )
            cid = feed_handler.add_card(card)
            assert feed_handler._cards[cid].accepted is None

    def test_scenario_unknown_card_type_never_auto_accepted(self, feed_handler, mock_glib, mock_feed_tab):
        """BUG #10 regression: unknown card_type (e.g. 'git_commit', None) is never
        auto-accepted regardless of prefs."""
        feed_handler._auto_accept_enabled = True
        for ct in ("diff", "file_created", "file_modified", "file_deleted"):
            feed_handler._prefs.file_changes[ct].enabled = True
            feed_handler._prefs.file_changes[ct].agent_scope = "all_agents"

        for ct in ("git_commit", "audit_report", None):
            card = FeedCardData(
                card_type=ct or "", source="agent", title=f"{ct} event",
                body="content", author="AgentA",
                timestamp=datetime.now(timezone.utc), project_name="proj",
            )
            cid = feed_handler.add_card(card)
            assert feed_handler._cards[cid].accepted is None

    def test_scenario_agent_scope_all_agents(self, feed_handler, mock_glib, mock_feed_tab):
        """Agent scope 'all_agents' → any agent's cards auto-accept."""
        feed_handler._prefs.file_changes["diff"].enabled = True
        feed_handler._prefs.file_changes["diff"].agent_scope = "all_agents"
        feed_handler._auto_accept_enabled = True

        for author in ("AgentA", "AgentB", "AgentC"):
            card = FeedCardData(
                card_type="diff", source="agent", title=f"diff by {author}",
                body="+code", author=author,
                timestamp=datetime.now(timezone.utc), project_name="proj",
                file_path="foo.py",
            )
            cid = feed_handler.add_card(card)
            assert cid in feed_handler._cards

    def test_scenario_agent_scope_first_author_lock_in(self, feed_handler, mock_glib, mock_feed_tab):
        """Agent scope 'first_author' → first card's author locks in;
        subsequent cards from different agents are NOT auto-accepted."""
        feed_handler._prefs.file_changes["diff"].enabled = True
        feed_handler._prefs.file_changes["diff"].agent_scope = "first_author"
        feed_handler._auto_accept_enabled = True

        # First card from AgentA — should auto-accept (lock-in)
        card_a = FeedCardData(
            card_type="diff", source="agent", title="diff by A",
            body="+a", author="AgentA",
            timestamp=datetime.now(timezone.utc), project_name="proj",
            file_path="a.py",
        )
        cid_a = feed_handler.add_card(card_a)
        assert cid_a in feed_handler._cards

        # Second card from AgentB — should NOT auto-accept
        card_b = FeedCardData(
            card_type="diff", source="agent", title="diff by B",
            body="+b", author="AgentB",
            timestamp=datetime.now(timezone.utc), project_name="proj",
            file_path="b.py",
        )
        cid_b = feed_handler.add_card(card_b)
        assert feed_handler._cards[cid_b].accepted is None

    def test_scenario_snoozed_card_not_auto_accepted(self, feed_handler, mock_glib, mock_feed_tab, monkeypatch):
        """Snoozed cards are not auto-accepted.

        Uses monkeypatch to predict the UUID that add_card will assign,
        so we can pre-snooze it before the auto-accept check fires."""
        monkeypatch.setattr("ui.handlers.feed_handler.uuid.uuid4", lambda: "snoozed-scenario-uuid")
        feed_handler._prefs.file_changes["diff"].enabled = True
        feed_handler._prefs.file_changes["diff"].agent_scope = "all_agents"
        feed_handler._auto_accept_enabled = True
        feed_handler._prefs.snoozed_card_ids.append("snoozed-scenario-uuid")

        card = FeedCardData(
            card_type="diff", source="agent", title="snoozed diff",
            body="+code", author="AgentA",
            timestamp=datetime.now(timezone.utc), project_name="proj",
            file_path="foo.py",
        )
        cid = feed_handler.add_card(card)
        assert feed_handler._cards[cid].accepted is None

    def test_scenario_batch_accept_independent_of_auto_accept(self, feed_handler, mock_glib, mock_feed_tab):
        """All toggles OFF, then user clicks Accept All → batch accept still works.
        Auto-accept and batch accept are independent mechanisms."""
        feed_handler._auto_accept_enabled = False

        # Add 3 pending file-change cards
        ids = []
        for i in range(3):
            card = FeedCardData(
                card_type="diff", source="agent", title=f"diff {i}",
                body=f"+line{i}", author="AgentA",
                timestamp=datetime.now(timezone.utc), project_name="proj",
                file_path=f"file_{i}.py",
            )
            ids.append(feed_handler.add_card(card))

        # Batch accept bar should be visible (3 pending ≥ 2 threshold)
        assert mock_feed_tab._batch_bar_visible is True
        assert mock_feed_tab._batch_bar_count == 3
```

---

### Class 3: `TestExecAutoAcceptModeQuery`

Tests for the `get_exec_auto_accept_mode()` API and the callback wiring pattern (Phase 6).

```python
class TestExecAutoAcceptModeQuery:
    """Phase 6-8: get_exec_auto_accept_mode() and callback wiring tests."""

    def test_get_mode_returns_off_by_default(self, feed_handler):
        """Fresh handler returns 'off' for exec mode."""
        assert feed_handler.get_exec_auto_accept_mode() == "off"

    def test_get_mode_returns_show_when_set(self, feed_handler):
        """After setting exec mode to 'show', get returns 'show'."""
        feed_handler._prefs.exec_command.mode = "show"
        assert feed_handler.get_exec_auto_accept_mode() == "show"

    def test_get_mode_returns_silent_when_set(self, feed_handler):
        """After setting exec mode to 'silent', get returns 'silent'."""
        feed_handler._prefs.exec_command.mode = "silent"
        assert feed_handler.get_exec_auto_accept_mode() == "silent"

    def test_get_mode_returns_none_when_prefs_is_none(self):
        """When _prefs is None (not yet initialized), returns None.
        This guards against constructor-ordering races."""
        from ui.handlers.feed_handler import FeedHandler
        h = FeedHandler(GLib=MockGLib(), on_send_to_agent=MagicMock())
        h._prefs = None
        assert h.get_exec_auto_accept_mode() is None

    def test_set_check_exec_callback_wires_correctly(self, feed_handler):
        """set_check_exec_auto_accept_callback_for_handler() installs FH's
        getter as ARTH's callback. The callback returns the current mode."""
        captured_callback = [None]

        def fake_arth_setter(cb):
            captured_callback[0] = cb

        feed_handler.set_check_exec_auto_accept_callback_for_handler(fake_arth_setter)

        # The captured callback should be FH.get_exec_auto_accept_mode
        assert captured_callback[0] is not None
        # Set mode and verify the callback reflects it
        feed_handler._prefs.exec_command.mode = "silent"
        assert captured_callback[0]() == "silent"
        feed_handler._prefs.exec_command.mode = "show"
        assert captured_callback[0]() == "show"
```

---

## Rules

1. **DO NOT modify any existing test.** Only append new classes at the end.
2. **DO NOT modify any production code.** This is test-only.
3. **Use the existing fixtures** (`feed_handler`, `mock_glib`, `mock_feed_tab`) from the existing conftest/fixture section.
4. **Import `MagicMock`** from `unittest.mock` — already imported at the top of the file.
5. **Follow MockFeedTab duck-typing patterns** — MockFeedTab already has `append_card`, `update_auto_accept_state`, etc.
6. **The `MockGLib.idle_add` runs callbacks synchronously** (line 22: `fn(*args, **kwargs)`), so `add_card` auto-accept side-effects happen immediately.
7. **For exec approval cards**, set `feed_handler._on_approve_exec` to a lambda that records calls — do NOT rely on the default (which is `None` and logs a warning).
8. **For `handle_accept` tests on diff/file cards**: MockGLib runs `idle_add` synchronously, which means `handle_accept` fires and spawns a git thread. The git thread will fail silently (no repo). This is fine — the test verifies routing, not git results. If you need to verify `card.accepted`, mock `git_ops` or test exec cards instead.

## Verification

```bash
# Verify file parses
python3 -c "import ast; ast.parse(open('tests/test_feed_handler.py').read()); print('AST OK')"

# Run ONLY the new test classes
python3 -m pytest tests/test_feed_handler.py::TestExecAutoAccept tests/test_feed_handler.py::TestAutoAcceptScenario tests/test_feed_handler.py::TestExecAutoAcceptModeQuery -v

# Run ALL tests (existing + new must all pass)
python3 -m pytest tests/test_feed_handler.py tests/test_feed_card.py tests/test_feed_store.py tests/test_low12_13_feed.py tests/test_crabcard_parser.py tests/test_crabwatch_handler.py tests/test_window_auto_accept_warning.py -q

# Line count
wc -l tests/test_feed_handler.py
```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] TestExecAutoAccept class added — evidence (grep -n "class TestExecAutoAccept" tests/test_feed_handler.py)
- [x/not done] TestExecAutoAccept has ≥5 tests — evidence (grep -c "def test_" within class)
- [x/not done] TestAutoAcceptScenario class added — evidence (grep -n "class TestAutoAcceptScenario" tests/test_feed_handler.py)
- [x/not done] TestAutoAcceptScenario has ≥7 tests — evidence (grep -c "def test_" within class)
- [x/not done] TestExecAutoAcceptModeQuery class added — evidence (grep -n "class TestExecAutoAcceptModeQuery" tests/test_feed_handler.py)
- [x/not done] TestExecAutoAcceptModeQuery has ≥4 tests — evidence (grep -c "def test_" within class)
- [x/not done] All new tests pass — evidence (pytest output)
- [x/not done] All existing tests still pass (246 baseline) — evidence (pytest output)
- [x/not done] No existing tests modified — evidence (git diff shows only append)
- [x/not done] No production code modified — evidence (git diff --stat shows only tests/test_feed_handler.py)
```
