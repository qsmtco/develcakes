# tests/test_error_surfacing.py
# SPEC-02 Phase 1 — provider error surfacing + empty-message rollback.
#
# Covers:
#   1. AgentRuntimeHandler._do_error publishes a Project Feed card (type
#      "system", metadata kind=turn_error) for every non-stale, non-duplicate
#      turn error — with provider/model/exception_type context when the
#      runtime attached _crabcakes_context to the exception, and no card when
#      no feed handler is wired or the error is stale/duplicate.
#      Fix round (audit #2/#3): the WHOLE card block is best-effort — a
#      non-dict context attachment degrades to absent metadata (card still
#      emits) and a raising add_card is swallowed so the end-of-turn
#      lifecycle callback always fires.
#   2. AgentRuntime.cancel's dispatched message is NOT a failure (fix round,
#      audit #4): _do_error skips the turn-error card for CANCEL_MESSAGE.
#   3. AgentRuntime._run_loop's exception path rolls back trailing EMPTY
#      assistant messages (no content, no tool_calls) — up to 5, regardless
#      of which turn added them — while keeping partial-content and
#      tool-call messages, and no-ops cleanly when the conversation is gone.
#      Fix round (audit #5): comment truth pinned — the strip is
#      origin-agnostic, so a legacy trailing empty is shielded by this
#      turn's user message but DELETED when the turn dies before adding it.
#   4. End-to-end: a failed turn persists a conversation JSON whose trailing
#      message is the user message (no empty assistant entry) via
#      _terminate_turn's FAILED _auto_save.
#
# No GTK — handler logic (AgentRuntimeHandler(None, None)) and real
# AgentRuntime loops with a patched _call_llm, mirroring the patterns in
# tests/test_agent_runtime.py (TestStreaming) and tests/test_config_invalidation.py.

import json
import os
import tempfile
import unittest.mock
import uuid

import pytest

from agent.callbacks import OnError
from agent.runtime import CANCEL_MESSAGE, AgentRuntime, TurnStatus
from models.conversation import Message, MessageRole
from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
from utils.config import get_config_dir


def _uniq():
    return f"rt{uuid.uuid4().hex[:8]}"


def _config_dir():
    """The config dir the production write path actually consults —
    get_config_dir() prefers XDG_CONFIG_HOME over HOME, so assertions must
    read the same path the runtime wrote (not assume which env var wins)."""
    return get_config_dir()


class _MakeError(Exception):
    """Exception carrying the runtime's _crabcakes_context attachment."""

    def __init__(self, message, context=None):
        super().__init__(message)
        if context is not None:
            self._crabcakes_context = context


def _make_cfg():
    from agent.config import AgentConfig, LLMProviderConfig

    return AgentConfig(
        providers={
            "openai": LLMProviderConfig(
                name="openai",
                base_url="https://api.openai.com/v1",
                api_key="test-key",
                default_model="gpt-4o",
            )
        },
        default_provider="openai",
        default_model="openai/gpt-4o",
        max_tool_iterations=5,
        tool_timeout_seconds=30,
        auto_save_conversations=True,
    )


class _RecordingFeedHandler:
    """Test double for FeedHandler — records add_card calls."""

    def __init__(self):
        self.cards = []

    def add_card(self, card, persist=True):
        self.cards.append(card)
        return f"card-{len(self.cards)}"


class _ExplodingFeedHandler(_RecordingFeedHandler):
    """add_card raises — audit #3: emission failures must be swallowed."""

    def add_card(self, card, persist=True):
        raise RuntimeError("feed store locked (mid-shutdown)")


class _StubAgentDef:
    """Minimal SpecialAgentDef stand-in — only display_name is read."""

    def __init__(self, display_name):
        self.display_name = display_name


def _make_handler(feed_handler=None):
    """AgentRuntimeHandler constructs with None deps (headless-testable)."""
    h = AgentRuntimeHandler(main_content=None, chat_render_handler=None)
    if feed_handler is not None:
        h.set_feed_handler(feed_handler)
    return h


_fixture_config_root: str | None = (
    None  # set by _isolated_config_home; read by the isolation sentinel test
)


@pytest.fixture(scope="module", autouse=True)
def _isolated_config_home():
    """Fix round (audit #1, HIGH): redirect XDG_CONFIG_HOME to a temp dir for
    the whole module. Every _make_runtime() construction runs
    migrate_conversation_files() and each FAILED turn's _auto_save writes a
    conversation JSON — without this the suite wrote fixture conversations
    into the REAL ~/.config/crabcakes/conversations (supervisor measured
    DELTA=4 per run).

    Module-scoped, so monkeypatch (function-scoped) can't be used — manual
    patch/restore, mirroring tests/test_agent_runtime.py:1299-1321. utils/
    config.get_config_dir() consults XDG_CONFIG_HOME lazily on every call,
    so env set here covers construction, migration, and _auto_save alike.

    The tmp root is also stashed in _fixture_config_root so the sentinel
    test (TestIsolationSentinel, round-2 audit #8a) can prove the isolation
    is ACTIVE at test time — previously the suite could not detect its own
    isolation regressing (autouse removed → all tests still green)."""
    global _fixture_config_root
    tmp = tempfile.mkdtemp(prefix="spec02-test-config-")
    _fixture_config_root = tmp
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = tmp
    yield
    _fixture_config_root = None
    if old_xdg is None:
        os.environ.pop("XDG_CONFIG_HOME", None)
    else:
        os.environ["XDG_CONFIG_HOME"] = old_xdg


@pytest.fixture(scope="module", autouse=True)
def _hermetic_audit_flush():
    """_terminate_turn auto-flushes each runtime's AuditLog; the rollback
    tests below exercise real _run_loop → _terminate_turn paths, so without
    this guard a full-suite run would append test data to the REAL
    ~/.config/crabcakes/audit-log.jsonl. Same guard as
    tests/test_agent_runtime.py::_hermetic_audit_flush (module-scoped:
    manual patch/restore, since monkeypatch is function-scoped)."""
    from agent.audit import AuditLog

    original = AuditLog.flush_audit_log
    AuditLog.flush_audit_log = unittest.mock.MagicMock(return_value=None)
    yield
    AuditLog.flush_audit_log = original


def _make_runtime(on_error: OnError | None = None) -> AgentRuntime:
    def _ignore_error(
        session_key: str,
        message: str | BaseException,
        *,
        _turn_token: object | None = None,
    ) -> None:
        return None

    def _ignore_delta(
        session_key: str, text: str, *, _turn_token: object | None = None
    ) -> None:
        return None

    rt = AgentRuntime(
        _make_cfg(),
        on_text_delta=_ignore_delta,
        on_error=on_error or _ignore_error,
    )
    rt.start()
    return rt


# ═══════════════════════════════════════════════════════════════════
#  Isolation sentinel (round-2 audit #8a)
# ═══════════════════════════════════════════════════════════════════


class TestIsolationSentinel:
    def test_isolation_active_config_dir_inside_fixture_tmp(self):
        """Round-2 audit #8a: the module's config isolation must be
        self-falsifiable. Mutation evidence: disabling autouse on
        _isolated_config_home left every other test green — only an
        out-of-CI real-dir delta caught it. This sentinel reads
        get_config_dir() LIVE (same lazy resolution the production write
        path uses) and asserts it sits inside the fixture's tmp root. If
        the fixture stops running, _fixture_config_root is None or the
        env var stops redirecting — either way this test FAILS.

        Reads the stash as this module's own global (NOT via
        `import tests.test_error_surfacing` — pytest imports test files
        top-level when tests/ has no __init__.py, so that path can bind a
        SECOND module object whose globals never see the fixture write)."""
        assert _fixture_config_root is not None, (
            "_isolated_config_home fixture did not run — test isolation is "
            "OFF and this suite would write into the REAL config dir"
        )
        live_dir = get_config_dir()
        assert live_dir.startswith(_fixture_config_root), (
            f"get_config_dir() resolved to {live_dir!r}, outside the "
            f"fixture tmp root {_fixture_config_root!r} — isolation "
            "is not active"
        )


# ═══════════════════════════════════════════════════════════════════
#  Feed card in _do_error
# ═══════════════════════════════════════════════════════════════════


class TestDoErrorFeedCard:
    def test_do_error_publishes_feed_card(self):
        fh = _RecordingFeedHandler()
        h = _make_handler(feed_handler=fh)
        sk = _uniq()
        h._agents[sk] = _StubAgentDef("Coder")

        h._do_error(sk, "boom")

        assert len(fh.cards) == 1
        card = fh.cards[0]
        assert card.card_type == "system"
        assert card.source == "agent"
        assert card.title == "Turn failed: Coder"
        assert card.body == "boom"
        assert card.metadata["kind"] == "turn_error"
        assert card.metadata["session_key"] == sk

    def test_do_error_card_carries_provider_context(self):
        fh = _RecordingFeedHandler()
        h = _make_handler(feed_handler=fh)
        sk = _uniq()
        h._agents[sk] = _StubAgentDef("Coder")
        exc = _MakeError(
            "HTTP 401 Unauthorized",
            context={
                "provider": "openrouter",
                "model": "m1",
                "exception_type": "HTTPError",
            },
        )
        h._last_error_exception[sk] = exc

        # A BaseException message takes the friendly_error_message path —
        # for a plain non-network error that is str(exc), unchanged.
        h._do_error(sk, exc)

        assert len(fh.cards) == 1
        card = fh.cards[0]
        assert card.metadata["provider"] == "openrouter"
        assert card.metadata["model"] == "m1"
        assert card.metadata["exception_type"] == "HTTPError"
        # body is the friendly display message, not the repr of the exception
        assert card.body == "HTTP 401 Unauthorized"

    def test_do_error_card_body_capped_at_2000(self):
        """Round-2 audit #8b: the `display_msg[:2000]` slice does real work —
        FeedCardData's own cap is 200_000, so a regression deleting the
        slice previously went undetected (all tests still passed with an
        uncapped 2500-char body). Assert the slice lands: exact cap,
        single card, and metadata intact past the slice."""
        fh = _RecordingFeedHandler()
        h = _make_handler(feed_handler=fh)
        sk = _uniq()
        h._agents[sk] = _StubAgentDef("Coder")
        long_msg = "X" * 2500

        h._do_error(sk, long_msg)

        assert len(fh.cards) == 1
        card = fh.cards[0]
        assert len(card.body) == 2000
        assert card.body == "X" * 2000  # prefix preserved, tail sliced off
        # Metadata still intact despite the body slice.
        assert card.metadata["session_key"] == sk
        assert card.metadata["kind"] == "turn_error"
        assert card.metadata["provider"] is None
        assert card.metadata["model"] is None
        assert card.metadata["exception_type"] is None

    def test_do_error_non_dict_context_degrades_to_none_card_still_emits(self):
        """Audit #2: a truthy NON-dict _crabcakes_context attachment must not
        escape _do_error as AttributeError. Card still emits; provider/model/
        exception_type degrade to None; lifecycle end_cb still fires."""
        fh = _RecordingFeedHandler()
        end_calls = []
        h = _make_handler(feed_handler=fh)
        h._on_agent_end_cb = end_calls.append
        sk = _uniq()
        h._agents[sk] = _StubAgentDef("Coder")
        h._last_error_exception[sk] = _MakeError("corrupt ctx", context="openrouter")
        exc = h._last_error_exception[sk]
        assert exc is not None  # narrow dict value type for _do_error(sk, exc)

        h._do_error(sk, exc)  # must not raise

        assert len(fh.cards) == 1
        card = fh.cards[0]
        assert card.metadata["kind"] == "turn_error"
        assert card.metadata["provider"] is None
        assert card.metadata["model"] is None
        assert card.metadata["exception_type"] is None
        assert end_calls == [sk]

    def test_do_error_raising_add_card_swallowed_end_cb_still_fires(self):
        """Audit #3: add_card raising (lock contention / mid-shutdown) must
        not skip the _on_agent_end_cb lifecycle fire — the activity drawer
        would otherwise stay stuck on "running" forever."""
        fh = _ExplodingFeedHandler()
        end_calls = []
        h = _make_handler(feed_handler=fh)
        h._on_agent_end_cb = end_calls.append
        sk = _uniq()
        h._agents[sk] = _StubAgentDef("Coder")

        h._do_error(sk, "boom")  # must not raise

        assert fh.cards == []  # emission failed — swallowed, not recorded
        assert end_calls == [sk]  # lifecycle fired anyway

    def test_do_error_cancel_message_no_card(self):
        """Audit #4: a deliberate user cancel is not a turn-fatal provider
        error — no "Turn failed" card (stop-all would spray one per agent).
        The lifecycle end_cb still fires (drawer must return to idle)."""
        fh = _RecordingFeedHandler()
        end_calls = []
        h = _make_handler(feed_handler=fh)
        h._on_agent_end_cb = end_calls.append
        sk = _uniq()
        h._agents[sk] = _StubAgentDef("Coder")

        h._do_error(sk, CANCEL_MESSAGE)

        assert fh.cards == []
        assert end_calls == [sk]

    def test_do_error_no_feed_handler_no_crash(self):
        h = _make_handler(feed_handler=None)
        sk = _uniq()
        h._agents[sk] = _StubAgentDef("Coder")

        h._do_error(sk, "boom")  # must not raise

        # No card could have been recorded anywhere: _fh is None.

    def test_do_error_stale_token_no_card(self):
        fh = _RecordingFeedHandler()
        h = _make_handler(feed_handler=fh)
        sk = _uniq()
        h._agents[sk] = _StubAgentDef("Coder")
        current_token = object()
        stale_token = object()
        h._turn_tokens[sk] = current_token

        h._do_error(sk, "stale boom", error_token=stale_token)

        assert fh.cards == []

    def test_do_error_duplicate_completion_no_card(self):
        fh = _RecordingFeedHandler()
        h = _make_handler(feed_handler=fh)
        sk = _uniq()
        h._agents[sk] = _StubAgentDef("Coder")
        h._session_completed.add(sk)

        h._do_error(sk, "duplicate boom")

        assert fh.cards == []


# ═══════════════════════════════════════════════════════════════════
#  Empty-message rollback in _run_loop's exception path
# ═══════════════════════════════════════════════════════════════════


def _seed_conversation(rt, sk, pre_history):
    """Create the conversation; returns it. Pre-history stays BELOW the
    message the turn itself appends — _run_loop appends the user message
    at the end, so pre-history + [user] is the shape at _call_llm time.
    The turn-added empty assistants are appended by the patched _call_llm
    (production adds them mid-turn, after _call_llm returns at :1486)."""
    rt.create_conversation("Coder", sk, "/tmp")
    conv = rt._conversations[sk]
    conv.messages.extend(pre_history)
    return conv


def _boom_side_effect(rt, turn_messages=()):
    """side_effect for the patched _call_llm reproducing the production
    failure shape: the turn's own assistant messages are appended mid-turn
    (same call sites the loop uses), THEN the turn dies."""

    def _boom(session_key, messages, tools, turn_token=None):
        conv = rt._conversations.get(session_key)
        if conv is not None:
            for m in turn_messages:
                conv.messages.append(m)
        raise RuntimeError("kaboom")

    return _boom


def _user(text="hello"):
    return Message(role=MessageRole.USER, content=text)


def _empty_assistant():
    return Message(role=MessageRole.ASSISTANT, content="", tool_calls=[])


class TestEmptyMessageRollback:
    def test_rollback_removes_trailing_empty_assistant(self):
        errors = []

        def _collect(
            session_key: str,
            message: str | BaseException,
            *,
            _turn_token: object | None = None,
        ) -> None:
            errors.append(message)

        rt = _make_runtime(on_error=_collect)
        sk = _uniq()
        conv = _seed_conversation(rt, sk, [_user("prior answer")])

        with unittest.mock.patch.object(
            rt,
            "_call_llm",
            side_effect=_boom_side_effect(rt, turn_messages=[_empty_assistant()]),
        ):
            rt._run_loop(sk, "hello")  # user added, turn-empty appended, boom

        assert rt.get_turn_state(sk) is TurnStatus.FAILED
        # The turn's empty assistant is rolled back; user messages survive.
        assert [m.role for m in conv.messages] == [
            MessageRole.USER,
            MessageRole.USER,
        ]
        assert conv.messages[0].content == "prior answer"
        assert conv.messages[-1].content == "hello"
        assert len(errors) == 1
        rt.stop()

    def test_rollback_keeps_partial_content(self):
        rt = _make_runtime()
        sk = _uniq()
        conv = _seed_conversation(rt, sk, [_user("q")])
        partial = Message(role=MessageRole.ASSISTANT, content="partial text")

        with unittest.mock.patch.object(
            rt,
            "_call_llm",
            side_effect=_boom_side_effect(rt, turn_messages=[partial]),
        ):
            rt._run_loop(sk, "hello")

        assert rt.get_turn_state(sk) is TurnStatus.FAILED
        # Partial content is real progress — must NOT be rolled back.
        assert conv.messages[-1] is partial
        assert conv.messages[-1].content == "partial text"
        rt.stop()

    def test_rollback_keeps_tool_call_messages(self):
        from models.conversation import ToolCall

        rt = _make_runtime()
        sk = _uniq()
        conv = _seed_conversation(rt, sk, [_user("q")])
        tool_caller = Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(call_id="c1", tool_name="read_file", arguments={})],
        )

        with unittest.mock.patch.object(
            rt,
            "_call_llm",
            side_effect=_boom_side_effect(rt, turn_messages=[tool_caller]),
        ):
            rt._run_loop(sk, "hello")

        assert rt.get_turn_state(sk) is TurnStatus.FAILED
        # Empty content but real tool_calls → NOT empty → kept.
        assert conv.messages[-1] is tool_caller
        assert conv.messages[-1].tool_calls[0].call_id == "c1"
        rt.stop()

    def test_rollback_shields_legacy_empty_below_turn_user_message(self):
        """Fix round, audit #5 (comment truth), Debugger probe A: the strip
        is origin-agnostic, but a LEGACY trailing empty sitting BELOW the
        turn's own user message is unreachable — the user message shields
        it. A failed turn must not delete pre-existing history it never
        owned."""
        rt = _make_runtime()
        sk = _uniq()
        # Legacy corrupt trailing empty from some earlier bad write.
        conv = _seed_conversation(rt, sk, [_user("q"), _empty_assistant()])

        with unittest.mock.patch.object(
            rt,
            "_call_llm",
            side_effect=_boom_side_effect(rt),
        ):
            rt._run_loop(sk, "hello")  # turn adds its user msg ABOVE the legacy empty

        assert rt.get_turn_state(sk) is TurnStatus.FAILED
        # Trailing message is the turn's user "hello" → strip stops there.
        assert [m.content for m in conv.messages] == ["q", "", "hello"]
        assert conv.messages[1].role is MessageRole.ASSISTANT
        assert conv.messages[1].content == ""  # legacy empty survives — shielded
        rt.stop()

    def test_rollback_deletes_unshielded_legacy_empty_when_user_message_add_fails(self):
        """A pre-existing trailing empty exposed at the tail IS deleted when
        the turn fails before its user message lands — the strip is
        origin-agnostic. Modeled by add_user_message raising.

        NOTE (round-2 audit #9 rename): the prepare-failed path returns at
        runtime :1310, BEFORE the rollback block — no strip runs there — so
        the former name `..._when_prepare_fails` mislabeled this scenario.
        The mechanism under test is the add_user_message raise landing in
        the mid-loop exception path (where the rollback DOES run)."""
        rt = _make_runtime()
        sk = _uniq()
        conv = _seed_conversation(rt, sk, [_user("q"), _empty_assistant()])

        def _user_msg_boom(session_key, text):
            raise RuntimeError("user message write failed")

        with unittest.mock.patch.object(
            conv, "add_user_message", side_effect=_user_msg_boom
        ):
            rt._run_loop(
                sk, "hello"
            )  # user msg add raises → mid-loop except → rollback

        assert rt.get_turn_state(sk) is TurnStatus.FAILED
        result = rt.get_last_turn_result(sk)
        assert result is not None  # narrow Optional — proven by FAILED above
        assert result.metadata["reason"] == "exception"
        # No user message was added this turn; the trailing empty was
        # exposed → origin-agnostic strip removed it.
        assert [m.role for m in conv.messages] == [MessageRole.USER]
        assert conv.messages[0].content == "q"
        rt.stop()

    def test_rollback_caps_at_five(self):
        rt = _make_runtime()
        sk = _uniq()
        conv = _seed_conversation(rt, sk, [_user("q")])
        empties = [_empty_assistant() for _ in range(6)]

        with unittest.mock.patch.object(
            rt,
            "_call_llm",
            side_effect=_boom_side_effect(rt, turn_messages=empties),
        ):
            rt._run_loop(sk, "hello")

        assert rt.get_turn_state(sk) is TurnStatus.FAILED
        # Exactly 5 of the 6 turn-added empties removed (cap), 1 remains —
        # proves the loop terminates instead of draining unbounded.
        assert len(conv.messages) == 3  # user q + user hello + 1 survivor
        assert conv.messages[0].role is MessageRole.USER
        assert conv.messages[1].role is MessageRole.USER
        assert conv.messages[2].role is MessageRole.ASSISTANT
        assert conv.messages[2].content == ""
        assert conv.messages[2].tool_calls == []
        rt.stop()

    def test_rollback_no_conversation_noop(self):
        errors = []

        def _collect(
            session_key: str,
            message: str | BaseException,
            *,
            _turn_token: object | None = None,
        ) -> None:
            errors.append(message)

        rt = _make_runtime(on_error=_collect)
        sk = _uniq()  # never registered in _conversations

        # Must complete without raising: the early no-conversation path
        # fires (FAILED) and the rollback never touches a missing conv.
        with unittest.mock.patch.object(
            rt, "_call_llm", side_effect=RuntimeError("kaboom")
        ):
            rt._run_loop(sk, "hello")

        assert rt.get_turn_state(sk) is TurnStatus.FAILED
        result = rt.get_last_turn_result(sk)
        assert result is not None
        assert result.metadata["reason"] == "no_conversation"
        assert len(errors) == 1
        rt.stop()


# ═══════════════════════════════════════════════════════════════════
#  Mid-loop cancel dispatches use the CANCEL_MESSAGE constant (round-2 audit #6)
# ═══════════════════════════════════════════════════════════════════


class TestMidLoopCancelDispatchConstants:
    """SPEC-02 fix round 2, audit #6: the two MID-LOOP cancel sites in
    _run_loop (the ``_cancel_requested`` shutdown check and the
    ``session_key in _cancelled`` user check) previously dispatched the
    short form ``error="Cancelled"``, which bypasses the handler's
    CANCEL_MESSAGE skip and emits a spurious "Turn failed" card on every
    deliberate mid-loop cancel (supervisor probe: "Cancelled" → 1 card,
    "Cancelled by user" → 0 cards). Both sites must emit the constant —
    these tests FAIL against the bare literals (falsifier) and pin the
    exact per-site metadata so the two sites stay distinguishable.

    Deterministic by construction: both checks run at the top of the tool
    loop BEFORE the first _call_llm, so pre-seeding the signal and calling
    _run_loop directly needs no threads and no real cancel() dispatch."""

    @staticmethod
    def _collector(errors):
        def _collect(
            session_key: str,
            message: str | BaseException,
            *,
            _turn_token: object | None = None,
        ) -> None:
            errors.append(message)

        return _collect

    def test_cancel_requested_site_dispatches_cancel_message(self):
        errors = []
        rt = _make_runtime(on_error=self._collector(errors))
        sk = _uniq()
        _seed_conversation(rt, sk, [_user("q")])

        call_llm = unittest.mock.MagicMock(return_value={})
        rt._cancel_requested = True  # pre-seed: first loop check fires
        with unittest.mock.patch.object(rt, "_call_llm", call_llm):
            rt._run_loop(sk, "hello")

        assert rt.get_turn_state(sk) is TurnStatus.CANCELLED
        result = rt.get_last_turn_result(sk)
        assert result is not None
        assert result.metadata["reason"] == "shutdown"
        # The constant, never the short form — this is the audit #6 pin.
        assert errors == [CANCEL_MESSAGE]
        # The cancel check precedes the first LLM call: the turn died
        # before any provider traffic.
        call_llm.assert_not_called()
        rt.stop()

    def test_cancelled_set_site_dispatches_cancel_message(self):
        errors = []
        rt = _make_runtime(on_error=self._collector(errors))
        sk = _uniq()
        _seed_conversation(rt, sk, [_user("q")])

        call_llm = unittest.mock.MagicMock(return_value={})
        rt._cancelled.add(sk)  # pre-seed: per-session cancel check fires
        with unittest.mock.patch.object(rt, "_call_llm", call_llm):
            rt._run_loop(sk, "hello")

        assert rt.get_turn_state(sk) is TurnStatus.CANCELLED
        result = rt.get_last_turn_result(sk)
        assert result is not None
        assert result.metadata["reason"] == "user"
        assert errors == [CANCEL_MESSAGE]
        call_llm.assert_not_called()
        rt.stop()


# ═══════════════════════════════════════════════════════════════════
#  End-to-end persistence: failed turns save no empty assistant messages
# ═══════════════════════════════════════════════════════════════════


class TestFailedTurnPersistence:
    def test_failed_turn_persists_no_empty_assistant(self):
        """The original failure mode, end to end: a turn that dies mid-loop
        must not leave trailing empty assistant messages in the persisted
        conversation JSON (the placeholder-masking poison on reload).

        Composition note (fix round): the module-level _isolated_config_home
        fixture redirects XDG_CONFIG_HOME, and _auto_save resolves the write
        dir via utils.config.get_config_dir() lazily at save time — so this
        test asserts against get_config_dir() itself (whatever env wins)
        rather than assuming HOME or XDG precedence."""
        rt = _make_runtime()
        sk = _uniq()
        _seed_conversation(rt, sk, [_user("q")])

        with unittest.mock.patch.object(
            rt,
            "_call_llm",
            side_effect=_boom_side_effect(rt, turn_messages=[_empty_assistant()]),
        ):
            rt._run_loop(sk, "hello")  # FAILED → _terminate_turn → _auto_save

        path = os.path.join(_config_dir(), "conversations", f"{sk}.json")
        assert os.path.exists(path), f"expected persisted conversation at {path}"
        assert os.path.exists(path), f"expected persisted conversation at {path}"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # user "q" (pre-history) + user "hello" (added by _run_loop) — no
        # empty assistant entry survived the rollback before _auto_save ran.
        assert [m["role"] for m in data["messages"]] == ["user", "user"]
        assert data["messages"][-1]["content"] == "hello"
        rt.stop()
