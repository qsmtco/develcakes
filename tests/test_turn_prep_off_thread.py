# tests/test_turn_prep_off_thread.py
# SPEC-UI-RESPONSIVENESS-2 §2.4 Phase 4 Part B — turn preparation off the
# GTK main thread.
#
# Before this phase, AgentRuntimeHandler.send_to_special_agent() ran the
# conversation disk load, the project reconciliation and the per-agent state
# sync INLINE on the caller (GTK main) thread — ~300-500 ms per send.
#
# After: send_to_special_agent() hands a `prepare` callback to
# rt.send_message(), which the runtime runs on the loop thread
# (AgentRuntime._run_loop → prepare()) before the conversation lookup.
#
# These tests assert the main-thread contract (returns without waiting on
# disk I/O) and the concurrency contract (per-session prep lock ordering +
# RACE-FIX v4 token guard against a superseded send).

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest.mock
import uuid

import pytest

from agent.runtime import AgentRuntime, TurnStatus
from agent.special_agents import SpecialAgentDef


@pytest.fixture(scope="module", autouse=True)
def _hermetic_audit_flush():
    """Hermeticity guard (same rationale as tests/test_agent_runtime.py).

    _terminate_turn auto-flushes the runtime's AuditLog; without this the
    real-conversation tests below would append to the REAL
    ~/.config/crabcakes/audit-log.jsonl. Manual patch/restore because
    monkeypatch is function-scoped and cannot be used here.
    """
    from agent.audit import AuditLog
    original = AuditLog.flush_audit_log
    AuditLog.flush_audit_log = unittest.mock.MagicMock(return_value=None)
    yield
    AuditLog.flush_audit_log = original


def _uniq(prefix: str = "agent:prep") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


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
        auto_save_conversations=False,  # hermeticity: never persist
    )


def _resp(content="Done."):
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5},
    }


class _FakeConv:
    """Minimal Conversation stand-in for the handler's sync writes."""

    def __init__(self, project_path=None):
        # Default to a real EMPTY temp dir, never a bare "/tmp": the fake's
        # conv lands in real-runtime paths too (test #9's
        # create_conversation project_path), and a bare "/tmp" makes the
        # runtime's repo-map walk parse every .py file under /tmp (2.1 GB on
        # this box) — a multi-minute stall that reads as a hang.
        if project_path is None:
            project_path = tempfile.mkdtemp(prefix="prep-conv-")
        self.project_path = project_path
        self._owned_path = project_path
        self.agent_role = "stale"
        self.system_prompt = "stale prompt"
        self.api_key = None
        self.model = None
        self.app_title = ""
        self.fallback_provider = None
        self.mcp_servers = []
        self.si_enforcement = None
        self.step_count = 7

    def cleanup(self):
        shutil.rmtree(self._owned_path, ignore_errors=True)


class _FakeRuntime:
    """Mirrors AgentRuntime's send contract (SPEC-WIRE-CONTEXT-PRESSURE
    §2.3): the caller CLAIMS the session via begin_send(), then starts the
    loop directly with start_claimed_loop(session_key, text, turn_token,
    prepare) — send_message() is only the convenience wrapper around those
    two and is NOT on the handler's path.

    Every fake method records its calls so the tests can assert what
    happened on which thread.
    """

    def __init__(self, *, conv_exists=True, auto_run_prepare=True):
        self.conv = _FakeConv()
        self.conv_exists = conv_exists
        self.auto_run_prepare = auto_run_prepare
        self.prepares = []          # every prepare callback handed to send_message
        self.prep_completed = []    # index of each prepare that ran to completion
        self.prep_errors = []
        self.load_calls = []
        self.create_calls = []
        self.rebuild_calls = []
        self.cancel_calls = 0
        self.send_calls = []
        self.load_started = threading.Event()
        self.load_finished = threading.Event()
        self._get_calls = 0
        self.get_gate = None        # blocks the FIRST get_conversation call
        self.get_gate_entered = threading.Event()
        self.load_gate = None       # blocks load_conversation
        self.turn_token = object()
        self.loops = []             # (sk, text, token, prepare) per start_claimed_loop
        self._active_loops = set()  # sessions claimed by begin_send, per real runtime

    # ── contract used by _prepare_turn_conversation ───────────────────────

    def get_conversation(self, session_key):
        self._get_calls += 1
        if self.get_gate is not None and self._get_calls == 1:
            self.get_gate_entered.set()
            self.get_gate.wait(timeout=5)
        return self.conv if self.conv_exists else None

    def load_conversation(self, session_key):
        self.load_calls.append(session_key)
        self.load_started.set()
        if self.load_gate is not None:
            self.load_gate.wait(timeout=5)
        self.load_finished.set()
        return True

    def create_conversation(self, **kwargs):
        self.create_calls.append(kwargs)
        self.conv_exists = True
        return kwargs.get("session_key")

    def _rebuild_conversation_context(self, session_key, project_path, agent_role=""):
        self.rebuild_calls.append((project_path, agent_role))

    # ── send contract: claim + loop start, mirroring the real runtime ─────

    def begin_send(self, session_key, text):
        """Claim the session like AgentRuntime.begin_send (SPEC-WIRE-
        CONTEXT-PRESSURE §2.3): True + _active_loops.add when the caller
        must start the loop; False (text queued) when one is already
        running. Each claim stores its text so asserts can read it back."""
        self.send_calls.append((session_key, text))
        if session_key in self._active_loops:
            return False
        self._active_loops.add(session_key)
        return True

    def start_claimed_loop(self, session_key, text, turn_token, prepare=None):
        """Spawn the loop thread like the real runtime: `prepare` runs on
        that thread before the conversation lookup."""
        self.loops.append((session_key, text, turn_token, prepare))
        idx = len(self.prepares)
        self.prepares.append(prepare)

        if not self.auto_run_prepare:
            return

        def _run():
            # NOTE: no try/else here — `else` would fire even when there is no
            # prepare at all, which would make the RED assertions pass on the
            # pre-Phase-4 code (where the loop was started without one).
            if prepare is None:
                return
            try:
                prepare()
            except Exception as exc:  # noqa: BLE001 — mirrors _run_loop's
                # guard (runtime.py:1985): record, do not re-raise on the
                # loop thread; BaseException (SystemExit/KeyboardInterrupt)
                # would crash the daemon thread and hang the test.
                self.prep_errors.append(exc)
            else:
                self.prep_completed.append(idx)

        threading.Thread(target=_run, daemon=True).start()

    def send_message(self, session_key, text, prepare=None):
        """Convenience wrapper on the real runtime; kept for fidelity even
        though the handler path never calls it."""
        token = self.turn_token
        if not self.begin_send(session_key, text):
            return
        self.start_claimed_loop(session_key, text, token, prepare)

    def run_prepare(self, idx):
        """Run a stored prepare synchronously (for the superseded-send test)."""
        prepare = self.prepares[idx]
        if prepare is None:
            return
        try:
            prepare()
        except Exception as exc:  # noqa: BLE001 — mirrors _run_loop's guard
            # (runtime.py:1985): record the failure so the test can assert
            # on it instead of crashing the thread.
            self.prep_errors.append(exc)
        else:
            self.prep_completed.append(idx)

    def cancel(self, session_key):
        self.cancel_calls += 1


def _make_handler(fake_rt, *, agent_role="coder", project=("proj", "/tmp/proj")):
    """AgentRuntimeHandler with the fake runtime pre-registered (no real one)."""
    from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

    handler = AgentRuntimeHandler(unittest.mock.MagicMock(), unittest.mock.MagicMock())
    agent_def = SpecialAgentDef(
        conv_id_prefix="agent:coder",
        display_name="Coder",
        role=agent_role,
        emoji="🛠️",
        tools=["read_file"],
        can_write=True,
    )
    handler._agents["agent:coder"] = agent_def
    handler._runtimes["Coder"] = fake_rt
    handler._active_project = project
    return handler, agent_def


# ═══════════════════════════════════════════════════════════════════
#  Main-thread contract
# ═══════════════════════════════════════════════════════════════════

class TestSendIsNonBlocking:
    def test_send_returns_before_load_conversation_completes(self):
        """send_to_special_agent must not wait on the conversation disk load.

        RED pre-Phase-4: the load ran inline on the caller thread, so the
        call blocked until the (gated) fake load returned.
        """
        fake_rt = _FakeRuntime(conv_exists=False)
        fake_rt.load_gate = threading.Event()  # never set until the test says so
        handler, _ = _make_handler(fake_rt)

        t0 = time.perf_counter()
        handler.send_to_special_agent("agent:coder", "hello")
        elapsed = time.perf_counter() - t0

        assert elapsed < 0.5, (
            f"send_to_special_agent blocked for {elapsed:.2f}s — the conversation "
            "preparation must run on the loop thread"
        )
        assert fake_rt.load_started.wait(timeout=2), (
            "the loop thread should have started the preparation"
        )
        assert not fake_rt.load_finished.is_set(), "fixture precondition: load is gated"

        fake_rt.load_gate.set()
        assert fake_rt.load_finished.wait(timeout=2)

    def test_prepare_still_loads_and_creates_the_conversation(self):
        """Moving the prep off-thread must not change WHAT it does."""
        fake_rt = _FakeRuntime(conv_exists=False)
        handler, _ = _make_handler(fake_rt)

        handler.send_to_special_agent("agent:coder", "hello")
        deadline = time.monotonic() + 2.0
        while not fake_rt.prep_completed and time.monotonic() < deadline:
            time.sleep(0.01)

        assert fake_rt.prep_completed == [0], f"prep errors: {fake_rt.prep_errors!r}"
        assert fake_rt.load_calls == ["agent:coder"]
        assert len(fake_rt.create_calls) == 1
        kwargs = fake_rt.create_calls[0]
        assert kwargs["agent_role"] == "coder"
        assert kwargs["allowed_tools"] == ["read_file"]
        assert kwargs["project_path"] == "/tmp/proj"
        assert kwargs["defer_prompt_build"] is True
        # step-count reset still happens on the freshly prepared conversation
        assert fake_rt.conv.step_count == 0

    def test_send_assigns_the_turn_token_before_starting_the_loop(self):
        """The token the prep validates must be the one handed to the runtime."""
        fake_rt = _FakeRuntime()
        handler, _ = _make_handler(fake_rt)

        handler.send_to_special_agent("agent:coder", "hello")

        token = handler._turn_tokens["agent:coder"]
        assert token is not None
        assert fake_rt._turn_token is token, (
            "RACE-FIX v4b: the runtime must carry the new turn token BEFORE "
            "the loop thread (and its prepare callback) starts"
        )
        deadline = time.monotonic() + 2.0
        while not fake_rt.prep_completed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fake_rt.prep_completed == [0], "prep ran for its own (current) turn"
        assert fake_rt.prep_errors == []


# ═══════════════════════════════════════════════════════════════════
#  Concurrency contract
# ═══════════════════════════════════════════════════════════════════

class TestPrepTConcurrency:
    def test_cancel_during_prep_does_not_crash(self):
        """A cancel racing an in-flight prep must not raise from the prep."""
        fake_rt = _FakeRuntime(conv_exists=False)
        fake_rt.load_gate = threading.Event()
        handler, _ = _make_handler(fake_rt)

        handler.send_to_special_agent("agent:coder", "hello")
        assert fake_rt.load_started.wait(timeout=2)

        # Cancel while the prep is mid-load (the UI cancel path).
        fake_rt.cancel("agent:coder")

        fake_rt.load_gate.set()
        assert fake_rt.load_finished.wait(timeout=2)
        deadline = time.monotonic() + 2.0
        while not fake_rt.prep_completed and time.monotonic() < deadline:
            time.sleep(0.01)

        assert fake_rt.prep_errors == [], f"prep raised: {fake_rt.prep_errors!r}"
        assert fake_rt.prep_completed == [0]
        assert fake_rt.cancel_calls == 1

    def test_newer_send_is_the_last_writer_when_stale_prep_is_in_flight(self):
        """Queued-send contract (SPEC-WIRE-CONTEXT-PRESSURE §2.3): a second
        send while the session is CLAIMED is queued by begin_send — it
        creates no prep and does NOT rotate the turn token. The single
        in-flight prep (send #1's) is therefore the only and last writer
        of agent state: it closed over the def captured at
        session.py:409, so a PRODUCTION-style mid-flight edit never
        reaches that conversation. (The agent builder REPLACES the
        registry entry — window.py:259 → reload_agents_and_mcp →
        _agents.clear() + re-register fresh defs — it never mutates the
        old def in place. The new def is what the NEXT turn's own prep
        captures.) The mid-flight role edit lands in the registry but is
        NOT synced to the in-flight conversation.
        """
        fake_rt = _FakeRuntime(conv_exists=True)
        fake_rt.get_gate = threading.Event()  # parks the ONLY get_conversation
        handler, original_def = _make_handler(fake_rt)

        handler.send_to_special_agent("agent:coder", "first")
        assert fake_rt.get_gate_entered.wait(timeout=2), "prep should be parked"

        # The agent is edited mid-flight, PRODUCTION-style: the registry
        # entry is replaced wholesale, the old def object is never
        # touched. The in-flight prep keeps the def it closed over at
        # session.py:409.
        handler._agents["agent:coder"] = SpecialAgentDef(
            conv_id_prefix="agent:coder",
            display_name="Coder",
            role="debugger",
            emoji="🛠️",
            tools=["read_file"],
            can_write=True,
        )
        # Send #2 lands on a CLAIMED session: queued — no prep, no
        # rotation. _turn_token is the attr the REAL handler rotates
        # (session.py:497); the fake deliberately does NOT define it at
        # init, so the handler's `rt._turn_token = new_token` write is
        # the only thing that creates it — the token asserts below fail
        # closed (AttributeError) if a rotation goes missing. (The fake's
        # bare `turn_token` is its own init-time convenience token,
        # never moved by anything.)
        token_after_first = fake_rt._turn_token
        handler.send_to_special_agent("agent:coder", "second")
        # Send #2 DID reach the runtime (begin_send recorded both sends)
        # and was QUEUED there — claimed → False → return at
        # session.py:440-441, before any rotation:
        assert fake_rt.send_calls == [
            ("agent:coder", "first"),
            ("agent:coder", "second"),
        ], "send #2 must reach the runtime before being queued"

        fake_rt.get_gate.set()
        deadline = time.monotonic() + 2.0
        while not fake_rt.prep_completed and time.monotonic() < deadline:
            time.sleep(0.01)

        assert fake_rt.prep_errors == [], f"prep raised: {fake_rt.prep_errors!r}"
        assert fake_rt.prep_completed == [0], (
            "the claim owner's prep is the ONLY prep that runs")
        assert len(fake_rt.loops) == 1, "no second loop for a claimed session"
        assert fake_rt._turn_token == token_after_first, (
            "a queued send must not rotate the turn token")
        assert fake_rt.loops[0][2] == token_after_first, (
            "the one loop still runs under the un-rotated token")
        # The in-flight prep synced the def it CLOSED OVER (captured at
        # session.py:409), not whatever the registry holds now — so the
        # mid-flight replacement must NOT reach the conversation.
        assert original_def.role == "coder", (
            "the ORIGINAL def object is untouched — the builder replaces "
            "registry entries, it never mutates a live def in place"
        )
        assert fake_rt.conv.agent_role == "coder", (
            "the in-flight prep must sync the def it closed over, not the "
            "mid-flight registry replacement "
            f"(conv.agent_role={fake_rt.conv.agent_role!r})"
        )
        # The replacement DID land in the registry — it is what the NEXT
        # turn's own prep would capture.
        assert handler._agents["agent:coder"].role == "debugger", (
            "the production-style replacement must land in the registry"
        )
        assert fake_rt.conv.step_count == 0, (
            "the prep resets step_count for the new task "
            f"(step_count={fake_rt.conv.step_count})"
        )

    def test_superseded_prep_is_skipped_entirely(self):
        """Queued-send contract (SPEC-WIRE-CONTEXT-PRESSURE §2.3): with the
        session claimed by send #1, send #2 is QUEUED — it creates no prep
        and does not rotate the token, so there is no newer send to
        supersede anything. The claim owner's stored prep runs when its
        loop starts; a replay of it stays a no-op (existence guard).
        Falsifier: a second prep + token rotation at send #2 →
        prep_completed would be [1] and the token would move.

        Scope note (audit follow-up): despite the name, this does NOT pin the
        RACE-FIX v4 token guard — the sends here are serialized by begin_send's
        claim, so no token rotation can ever supersede the stored prep, and the
        guard is unreachable. What the replay asserts below prove is the
        EXISTENCE guard (:1037/:1051, "conversation already exists → skip").
        Deleting the token guard leaves this test green; the token guard is
        pinned by test_prepare_skips_mutation_when_the_turn_was_superseded.
        """
        fake_rt = _FakeRuntime(conv_exists=False, auto_run_prepare=False)
        handler, _ = _make_handler(fake_rt)

        # Send #1 claims the session — its prep is stored, not run
        # (simulates a late-starting loop thread).
        handler.send_to_special_agent("agent:coder", "first")
        assert len(fake_rt.prepares) == 1

        # Send #2 lands on a CLAIMED session: queued behind the claim —
        # no second prep, no token rotation. _turn_token is the attr the
        # REAL handler rotates (session.py:497); the fake's bare turn_token
        # is its own init-time token and never moves.
        token_after_first = fake_rt._turn_token
        handler.send_to_special_agent("agent:coder", "second")
        assert fake_rt._turn_token == token_after_first, (
            "a queued send must not rotate the turn token")
        assert len(fake_rt.loops) == 1, "send #2 is queued — no second loop"

        # The ONLY prep (send #1's, the claim owner's) runs.
        fake_rt.run_prepare(0)
        assert fake_rt.prep_completed == [0]
        assert len(fake_rt.load_calls) == 1
        assert len(fake_rt.create_calls) == 1

        # A replay of the stored prep is still a no-op (existence guard).
        fake_rt.run_prepare(0)
        assert len(fake_rt.load_calls) == 1, "replay must not load again"
        assert len(fake_rt.create_calls) == 1, "replay must not create again"
        assert fake_rt.prep_errors == []

    def test_prepare_skips_mutation_when_the_turn_was_superseded(self):
        """The RACE-FIX v4 token guard itself (agent_runtime_handler.py:1016):
        a prep whose token no longer matches _turn_tokens performs NO mutation.

        Scope note — why this is a direct call and not a send: on the handler
        path the guard is UNREACHABLE. _turn_tokens[sk] has one writer
        (session.py:496) and only runs when begin_send returned True; a send
        that finds the session claimed returns at session.py:440-441 BEFORE
        that rotation. So no send can ever rotate the token under an in-flight
        prep, and test_superseded_prep_is_skipped_entirely's "no-op replay"
        proves the EXISTENCE guard (:1037/:1051), not this one — deleting
        :1016 leaves that whole suite green. This test pins the guard as the
        unit it actually is; it would fail if :1016 were removed.

        Differential: the SAME fixture, the ONLY difference being which token
        is current. Stale -> zero loads/creates/rebuilds/syncs. Current ->
        normal preparation. A "skip" that skips in both cases would not pass.
        """
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

        def _run(current_is_stale: bool):
            fake_rt = _FakeRuntime(conv_exists=True)
            handler, agent_def = _make_handler(fake_rt)
            current = object()
            stale = object()
            handler._turn_tokens["agent:coder"] = current
            handler._prepare_turn_conversation(
                rt=fake_rt,
                session_key="agent:coder",
                agent_def=agent_def,
                project_path="/tmp/proj",
                project_name="proj",
                agent_model=None,
                si_enforcement=None,
                turn_token=stale if current_is_stale else current,
            )
            return handler, fake_rt

        _h, stale_rt = _run(current_is_stale=True)
        assert stale_rt.load_calls == [], "superseded prep must not load"
        assert stale_rt.create_calls == [], "superseded prep must not create"
        assert stale_rt.rebuild_calls == [], "superseded prep must not rebuild"
        assert stale_rt.conv.agent_role == "stale", (
            "superseded prep must not sync agent state "
            f"(agent_role={stale_rt.conv.agent_role!r})")
        assert stale_rt.conv.step_count == 7, (
            "superseded prep must not reset step_count "
            f"(step_count={stale_rt.conv.step_count})")

        _h2, current_rt = _run(current_is_stale=False)
        assert current_rt.conv.agent_role == "coder", (
            "control: a CURRENT token must prepare normally — otherwise the "
            "stale assertions above would pass vacuously")
        assert current_rt.conv.step_count == 0, "control: step_count reset"
        assert AgentRuntimeHandler is not None  # import used for the contract

    def test_prepare_takes_the_prep_lock(self):
        """The other half of RACE-FIX v4 (:1014): a prep HOLDS the per-session
        lock for its whole body, so a second prep for the same session cannot
        interleave with it and observe a half-prepared conversation.

        Differential: hold the lock on the test thread and call the prep from
        a worker. Locked -> the prep must not complete until the lock is
        released. Delete `with self._prep_lock(session_key)` and the prep
        returns immediately, so the "must not complete" assert fails. (This is
        what test_superseded_prep_is_skipped_entirely does NOT prove: its
        sends are serialized by begin_send's claim, so the lock is inert
        there and its removal leaves that test green.)
        """
        fake_rt = _FakeRuntime(conv_exists=True)
        handler, agent_def = _make_handler(fake_rt)
        handler._turn_tokens["agent:coder"] = object()
        lock = handler._prep_lock("agent:coder")
        finished = threading.Event()

        def _prep():
            handler._prepare_turn_conversation(
                rt=fake_rt,
                session_key="agent:coder",
                agent_def=agent_def,
                project_path="/tmp/proj",
                project_name="proj",
                agent_model=None,
                si_enforcement=None,
                turn_token=handler._turn_tokens["agent:coder"],
            )
            finished.set()

        lock.acquire()
        try:
            threading.Thread(target=_prep, daemon=True).start()
            assert not finished.wait(timeout=0.25), (
                "prep must block while another prep holds the session lock")
            assert fake_rt.load_calls == [], (
                "no I/O may start before the lock is acquired")
        finally:
            lock.release()

        assert finished.wait(timeout=2), "prep must proceed once the lock frees"
        assert fake_rt.prep_errors == [], "the freed prep must complete cleanly"

    def test_set_active_project_skips_rebuild_while_prep_is_in_flight(self):
        """The main-thread reconcile must never block on the prep lock."""
        fake_rt = _FakeRuntime(conv_exists=True)
        fake_rt.conv.project_path = "/tmp/old-proj"
        handler, _ = _make_handler(fake_rt)

        lock = handler._prep_lock("agent:coder")
        lock.acquire()  # stand in for an in-flight prep
        try:
            t0 = time.perf_counter()
            handler.set_active_project("proj", "/tmp/new-proj")
            elapsed = time.perf_counter() - t0
        finally:
            lock.release()

        assert elapsed < 0.1, f"set_active_project blocked for {elapsed:.2f}s on the prep lock"
        assert fake_rt.rebuild_calls == [], "skipped while the prep lock is held"

        # Lock free again → the eager reconcile runs.
        handler.set_active_project("proj", "/tmp/new-proj")
        assert fake_rt.rebuild_calls == [("/tmp/new-proj", "coder")]


# ═══════════════════════════════════════════════════════════════════
#  Runtime seam — AgentRuntime._run_loop(prepare=...)
# ═══════════════════════════════════════════════════════════════════

class TestRuntimePrepareHook:
    def test_prepare_runs_before_the_conversation_lookup(self):
        """A prepare that creates the conversation lets the turn proceed."""
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        events = []
        # EMPTY temp dir, never a bare "/tmp": the runtime's repo-map walk
        # (runtime.py:3855) parses every .py under conv.project_path, and a
        # bare "/tmp" walks 2.1 GB / ~19k files on this box — a multi-minute
        # stall that reads as a hang.
        proj = tempfile.mkdtemp(prefix="prep-runtime-")
        try:
            def prepare():
                events.append("prepare")
                rt.create_conversation(
                    "Coder", sk, proj,
                    allowed_tools=["read_file"],
                    defer_prompt_build=True,
                )
                events.append("created")

            with unittest.mock.patch.object(
                rt, "_call_llm", lambda sk_, msgs, tools, **kw: _resp("Done.")
            ):
                rt._run_loop(sk, "hello", prepare=prepare)

            assert events == ["prepare", "created"]
            assert rt.get_conversation(sk) is not None, "prepare must run before the lookup"
            assert rt.get_turn_state(sk) == TurnStatus.COMPLETED
        finally:
            shutil.rmtree(proj, ignore_errors=True)
            rt.stop()

    def test_prepare_failure_terminates_the_turn_failed(self):
        """A raising prepare fails the turn instead of crashing the thread."""
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()

        def broken_prepare():
            raise RuntimeError("conversation store unavailable")

        rt._run_loop(sk, "hello", prepare=broken_prepare)

        result = rt.get_last_turn_result(sk)
        assert result is not None, "the turn must reach a terminal state"
        assert result.status == TurnStatus.FAILED
        assert result.metadata.get("reason") == "prepare_failed"
        assert isinstance(result.error, RuntimeError)
        assert rt.get_conversation(sk) is None
        rt.stop()
