# tests/test_process_kill.py
# SPEC-09 SP3 — tools.py Popen + process registry + group-kill tests.
#
# Bare-safe: NO gi import anywhere in this module — runs without a display
# (the whole point of the tools layer: no GTK, no state, no network).
#
# Coverage (spec Edit 5, tests 1-4):
#   1. test_exec_still_works          — exec contract byte-identical
#   2. test_timeout_group_kills_children — timeout kills the WHOLE group
#   3. test_cancel_all_kills_running  — stop-all kill path unblocks the tool thread
#   4. test_escalation_sigkill        — SIGTERM-ignoring leader dies via SIGKILL
#
# Process-liveness checks scan /proc directly (pid → cmdline bytes) instead
# of pgrep: pgrep -f matches the CALLER's own cmdline too when the pattern
# text appears in a wrapping shell's arguments (probe-verified artifact),
# while a /proc cmdline scan cannot self-match the pytest process.

import os
import signal
import subprocess
import threading
import time
from unittest.mock import patch

import pytest

from agent import tools
from utils.env_security import get_scrubbed_env


def _marker_procs(marker: str) -> list[int]:
    """PIDs whose /proc cmdline contains marker (never self-matches: the
    pytest process cmdline is 'python -m pytest ...', no marker text).

    Markers MUST be numeric (valid `sleep` durations) — they ride INSIDE
    the sleep argument so each child's own cmdline carries its marker;
    a non-numeric arg would make sleep exit(1) instantly and the test
    would pass vacuously against an already-dead group.
    """
    hits = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue  # raced with process death
        if marker in cmd:
            hits.append(int(entry))
    return hits


def _wait_for(cond, timeout=5.0, poll=0.05):
    """Poll cond() until truthy or timeout; returns final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = cond()
        if val:
            return val
        time.sleep(poll)
    return cond()


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate the module registry per test — no cross-test kill targets."""
    with tools._PROCESS_REGISTRY_LOCK:
        tools._PROCESS_REGISTRY.clear()
    yield
    # Kill anything a test left behind so the suite never leaks processes.
    tools.cancel_all_processes(None)
    with tools._PROCESS_REGISTRY_LOCK:
        tools._PROCESS_REGISTRY.clear()


@pytest.fixture(autouse=True)
def _auto_approve():
    """Approve exec_command for every test in this module (approval gating
    is tested elsewhere; these tests target the process lifecycle)."""
    tools.set_approval_callback(lambda *a, **k: True)
    yield
    tools.set_approval_callback(None)


class TestExecContract:
    """Test 1 — the exec CONTRACT survives the subprocess.run → Popen swap."""

    def test_exec_still_works(self):
        r = tools._exec_command(
            "echo hello; echo oops >&2; exit 3", "/tmp", timeout=15,
            session_key="pk-contract",
        )
        assert r.success is False          # exit 3
        assert r.exit_code == 3
        assert "hello" in r.stdout
        assert "oops" in r.stderr
        assert r.error == "Exit 3"
        assert r.duration_ms >= 0

    def test_exec_success_shape(self):
        r = tools._exec_command("printf 'abc'", "/tmp", timeout=15,
                                session_key="pk-contract")
        assert r.success is True
        assert r.stdout == "abc"
        assert r.stderr == ""
        assert r.exit_code == 0
        assert r.output == "abc"

    def test_exec_output_truncation_intact(self):
        # 1-byte-over stream truncation: the MAX_EXEC_OUTPUT marker suffix
        # must still appear (byte-identical contract with subprocess.run).
        r = tools._exec_command(
            f"python3 -c \"print('x' * {tools.MAX_EXEC_OUTPUT + 100})\"",
            "/tmp", timeout=30, session_key="pk-contract",
        )
        assert r.success is True
        assert "[... truncated at" in r.stdout
        assert len(r.stdout) <= tools.MAX_EXEC_OUTPUT + 100  # cap + suffix

    def test_blocked_command_refused_without_process(self):
        r = tools._exec_command("rm -rf /", "/tmp", timeout=5,
                                session_key="pk-block")
        assert r.success is False
        assert "blocked by safety policy" in r.error
        assert tools._PROCESS_REGISTRY == {}


class TestTimeoutGroupKill:
    """Test 2 — timeout now kills the whole process GROUP (old code killed
    only the shell; children survived as orphans)."""

    def test_timeout_group_kills_children(self):
        # Two distinct background sleeps + wait: the group leader alone is
        # NOT enough — killpg must reach both children. Markers are
        # distinctive NUMERIC durations (valid sleep args, greppable cmdlines).
        marker_a, marker_b = "7.03", "9.07"
        start = time.monotonic()
        r = tools._exec_command(
            f"sleep 30 {marker_a} & sleep 30 {marker_b} & wait",
            "/tmp", timeout=1, session_key="pk-timeout",
        )
        elapsed = time.monotonic() - start
        assert r.success is False
        assert r.error == "Command timed out after 1s"
        assert r.exit_code is None and r.stdout == "" and r.stderr == ""
        # SP3 fix round 2: tightened from <8.0 — the auditor called 8.0
        # "defensible bound, wrong reason": the 4s of slack was EXCUSED by
        # group escalation but actually CAUSED by the zombie-leader spin
        # (BUG#1). 1s timeout + confirmed kill fits comfortably under 2.0.
        assert elapsed < 2.0  # 1s timeout + kill confirmation (no zombie spin)
        # No surviving process from the group (poll: killpg is synchronous,
        # but SIGTERM handling on the children can lag a beat).
        dead = _wait_for(
            lambda: not _marker_procs(marker_a) and not _marker_procs(marker_b),
            timeout=3.0,
        )
        assert dead, (
            f"orphaned children survived timeout: "
            f"A={_marker_procs(marker_a)} B={_marker_procs(marker_b)}"
        )
        assert tools._PROCESS_REGISTRY == {}  # unregistered on the timeout path


class TestGroupLivenessEscalation:
    """SP3 fix round (audit BUG#1) — escalation gated on GROUP liveness
    (killpg 0-probe), never on the leader's reaping; and the poll() gate
    removal in cancel_all_processes. RED shapes: scratch/probe_sp3fix_b/c/i/h."""

    def test_term_ignoring_child_leader_honors(self):
        """Auditor shape 1 (probe C / AC#7 end-to-end): a TERM-IGNORING child
        under a TERM-HONORING leader. The old leader-gated code reaped the
        leader, skipped escalation, and orphaned the child holding the pipe
        (turn thread alive +6s, never CANCELLED). Group-liveness probing
        must escalate to SIGKILL and leave NO survivor under /proc."""
        marker = "8.51"
        # Outer sh honors TERM (dies); inner bash ignores it (trap "" TERM
        # inherited by the backgrounded sleep). The sleep holds the exec
        # pipe open — a survivor keeps the tool thread blocked.
        cmd = 'bash -c \'trap "" TERM; sleep 300 ' + marker + ' & wait\' & wait'
        results = []

        def run():
            results.append(tools._exec_command(
                cmd, "/tmp", timeout=120, session_key="pk-grp-escalate",
            ))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert _wait_for(
            lambda: tools._PROCESS_REGISTRY.get("pk-grp-escalate"), timeout=5.0,
        ), "process never registered — kill target would be vacuous"
        assert _wait_for(lambda: _marker_procs(marker), timeout=5.0), (
            "immune child never spawned — shape would be vacuous"
        )

        t0 = time.monotonic()
        killed, unkillable = tools.cancel_all_processes("pk-grp-escalate")
        elapsed = time.monotonic() - t0

        assert (killed, unkillable) == (1, 0)
        # grace (2s) + SIGKILL propagation — NOT the old instant return that
        # proved only the leader was signalled.
        assert elapsed >= 1.5, (
            f"returned in {elapsed:.2f}s — group probe never ran the grace"
        )
        assert elapsed <= 3.0, (
            f"group still alive {elapsed:.2f}s after stop — escalation too slow"
        )
        t.join(timeout=5.0)
        assert not t.is_alive(), "tool thread still blocked — a survivor holds the pipe"
        assert results, "exec never returned"
        assert results[0].exit_code == -15  # leader died on the SIGTERM
        assert _wait_for(lambda: not _marker_procs(marker), timeout=3.0), (
            f"TERM-immune child orphaned: {_marker_procs(marker)}"
        )

    def test_unkillable_real_path_sibling(self):
        """Fix round 2 (BUG#4): the stop-all card test pins the CARD
        GRAMMAR via a mocked cancel_all_processes; this is the REAL-path
        sibling — an actually unkillable process-group, built WITHOUT
        mocks, that _group_kill reports False on. A process cannot ignore
        SIGKILL, so the portable way to build a SIGKILL-surviving group is
        mock-at-Popen-level: a Popen subclass whose poll()/wait() never
        reaps (the no-reaper shape) plus a killpg-SIGKILL that a stopped
        (SIGSTOPped) group defers... which is NOT portable either — SIGSTOP
        is SIGKILL-interruptible. Documented choice (spec: "or documented
        Popen-level choice"): mock os.killpg at the boundary so SIGKILL
        and the 0-probe both report the group alive — the ONE surface a
        real unkillable process cannot be built portably from Python.
        Asserts the FULL real path: registry → cancel_all_processes →
        _group_kill → False → (killed, unkillable) == (0, 1)."""
        marker = "8.57"
        proc = subprocess.Popen(
            f"sleep 300 {marker}", shell=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=get_scrubbed_env(),
            start_new_session=True,
        )
        session_key = "pk-unkillable-real"
        with tools._PROCESS_REGISTRY_LOCK:
            tools._PROCESS_REGISTRY.setdefault(session_key, []).append(proc)
        assert _wait_for(lambda: _marker_procs(marker), timeout=5.0), (
            "sleep never spawned — shape vacuous"
        )

        real_killpg = os.killpg

        def frozen_killpg(pgid, sig):
            if pgid == proc.pid and sig == signal.SIGKILL:
                return  # SIGKILL "lost" — the unkillable pretense
            return real_killpg(pgid, sig)

        def never_reap_poll(self, *a, **k):
            return None  # the no-reaper shape: poll never reaps the leader

        try:
            with patch.object(os, "killpg", side_effect=frozen_killpg), \
                 patch.object(subprocess.Popen, "poll", never_reap_poll), \
                 patch.object(subprocess.Popen, "wait", never_reap_poll):
                killed, unkillable = tools.cancel_all_processes(session_key)
        finally:
            # Cleanup on the REAL primitives (with block exits first).
            real_killpg(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        assert (killed, unkillable) == (0, 1), (
            f"expected (0, 1) unkillable; got {(killed, unkillable)} — "
            f"the card grammar's producer (BUG#3) is not exercised by a "
            f"real kill path anywhere else"
        )
        assert not _marker_procs(marker) or _wait_for(
            lambda: not _marker_procs(marker), timeout=3.0,
        ), "the process must ACTUALLY die in cleanup"

    def test_leader_exits_child_lingers(self):
        """Auditor shape 2 (probe I — the dropped poll-gate case): the leader
        exits IMMEDIATELY while its child lingers. `proc.poll() is None` was
        False → the old code never signalled at all. The group (orphan keeps
        the leader's pgid) must be killed by cancel_all_processes."""
        marker = "8.53"
        cmd = f"sleep 300 {marker} & echo started"
        results = []

        def run():
            results.append(tools._exec_command(
                cmd, "/tmp", timeout=60, session_key="pk-grp-linger",
            ))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert _wait_for(
            lambda: tools._PROCESS_REGISTRY.get("pk-grp-linger"), timeout=5.0,
        ), "process never registered — kill target would be vacuous"
        # The exact dropped shape: leader REAPED (poll() is not None) while
        # the child is still alive under /proc.
        leader_done = _wait_for(
            lambda: tools._PROCESS_REGISTRY.get("pk-grp-linger")[0].poll() is not None,
            timeout=5.0,
        )
        assert leader_done, "leader never exited — not the linger shape"
        assert _marker_procs(marker), "no lingering child — shape would be vacuous"

        killed, unkillable = tools.cancel_all_processes("pk-grp-linger")
        assert (killed, unkillable) == (1, 0)
        t.join(timeout=5.0)
        assert not t.is_alive()
        assert _wait_for(lambda: not _marker_procs(marker), timeout=3.0), (
            f"orphan survived the poll-gate: {_marker_procs(marker)}"
        )
        assert tools._PROCESS_REGISTRY.get("pk-grp-linger") in (None, [])

    def test_timeout_group_kills_term_immune_child_under_term_honoring_leader(self):
        """Auditor shape 3 (probe H): the exec tool's OWN timeout path with
        the probe-C group shape. The timeout kill is group-liveness gated
        too — a TERM-immune child under a TERM-honoring leader cannot
        outlive its command's timeout."""
        marker = "8.55"
        cmd = 'bash -c \'trap "" TERM; sleep 300 ' + marker + ' & wait\' & wait'
        results = []

        def run():
            results.append(tools._exec_command(
                cmd, "/tmp", timeout=1, session_key="pk-grp-timeout",
            ))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=15.0)
        assert not t.is_alive(), "timeout path never returned"
        assert results and results[0].success is False
        assert results[0].error == "Command timed out after 1s"
        assert results[0].exit_code is None
        assert _wait_for(lambda: not _marker_procs(marker), timeout=5.0), (
            f"TERM-immune child survived its own timeout: {_marker_procs(marker)}"
        )
        assert tools._PROCESS_REGISTRY == {}


class TestZombieLeaderLiveness:
    """SP3 fix round 2 (BUG#1, HIGH) — an UNREAPED ZOMBIE leader keeps
    killpg(pgid, 0) truthy, so _group_kill's liveness-gated windows spin
    BOTH 2s waits on an actually-empty group. Reaping runs only in the
    declared-dead branch today, and the no-reaper windows (exec timeout
    path; registry→communicate gap) never reap at all.

    RED shapes (auditor probes): dbg_sp3fix_timeout (5.01s), zrate
    (12/12 false unkillable), causation (poll() before probe → 0.05s).
    Fix under test: proc.poll() at the TOP of every window iteration,
    BEFORE the _group_alive probe.
    """

    def test_timeout_path_no_zombie_spin(self):
        """BUG#1(i): a 1s-timeout exec on a simple command must return
        shortly after its timeout — NOT +4s of dead-group spinning. The
        shell here exits immediately once killed (no TERM trap): after the
        SIGTERM the group is genuinely empty, but the tool thread (which
        owns the Popen) never reaps it until the post-kill communicate(),
        so the leader sits a zombie and killpg(pgid,0) kept succeeding.
        Auditor's causation probe: 5.01s pre-fix → 0.05s post-fix."""
        t0 = time.monotonic()
        r = tools._exec_command("sleep 300 6.61", "/tmp", timeout=1,
                                session_key="pk-zombie-timeout")
        elapsed = time.monotonic() - t0
        assert r.success is False
        assert r.error == "Command timed out after 1s"
        assert r.exit_code is None
        assert elapsed < 2.0, (
            f"timeout path took {elapsed:.2f}s — zombie-leader spin "
            f"(BUG#1) not fixed (pre-fix: ~5.01s)"
        )

    def test_self_exiting_leader_reports_killed_fast(self):
        """BUG#1(ii): a leader that self-exits leaves a zombie (nobody
        reaps during the registry→communicate window). _group_kill must
        reap-via-poll and report (1, 0) FAST — not spin 4s and report
        (0, 1)."""
        proc = subprocess.Popen(
            "exit 0", shell=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=get_scrubbed_env(),
            start_new_session=True,
        )
        time.sleep(0.4)  # leader exits → zombie (deliberately never reaped)
        t0 = time.monotonic()
        killed = tools._group_kill(proc)
        elapsed = time.monotonic() - t0
        assert killed is True, f"_group_kill must confirm death, got {killed!r}"
        assert elapsed < 2.0, (
            f"_group_kill took {elapsed:.2f}s on a zombie-only group — "
            f"poll-before-probe missing"
        )
        # poll() reaps; the Popen must now be joinable without error.
        proc.wait(timeout=1)

    def test_no_reaper_zombie_group_reads_dead(self):
        """BUG#1(iii): the auditor's zrate shape — 12/12 FALSE-unkillable.
        TERM-immune child under a self-exited (zombie) leader, registered
        with NO reaper running, killed via cancel_all_processes: the
        result must be (1, 0) — killed once, ZERO unkillable — and the
        child must really be gone from /proc."""
        marker = "4501"
        cmd = f'bash -c \'trap "" TERM; sleep 300 {marker} & wait\' & exit 0'
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=get_scrubbed_env(), start_new_session=True,
        )
        session_key = "pk-zombie-zrate"
        with tools._PROCESS_REGISTRY_LOCK:
            tools._PROCESS_REGISTRY.setdefault(session_key, []).append(proc)
        time.sleep(0.6)  # leader exits → unreaped zombie; child lingers

        assert _marker_procs(marker), "immune child never spawned — vacuous"
        killed, unkillable = tools.cancel_all_processes(session_key)
        assert (killed, unkillable) == (1, 0), (
            f"expected (1, 0); got {(killed, unkillable)} — the zombie "
            f"leader kept the group probe truthy (BUG#1)"
        )
        assert _wait_for(
            lambda: not _marker_procs(marker), timeout=3.0,
        ), f"TERM-immune child survived: {_marker_procs(marker)}"


class TestCancelAllProcesses:
    """Test 3 — the stop-all kill path: registered process killed, tool
    thread unblocked, registry drained."""

    def test_cancel_all_kills_running(self):
        marker = "8.11"
        results = []

        def run():
            results.append(tools._exec_command(
                f"sleep 300 {marker}", "/tmp", timeout=120,
                session_key="pk-cancel",
            ))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        # Wait until the process is REALLY running and registered.
        registered = _wait_for(
            lambda: tools._PROCESS_REGISTRY.get("pk-cancel"), timeout=5.0,
        )
        assert registered, "process never registered — kill target would be vacuous"
        assert _marker_procs(marker), "sleep never appeared under /proc"

        killed = tools.cancel_all_processes("pk-cancel")
        assert killed == (1, 0)
        t.join(timeout=5.0)
        assert not t.is_alive(), "tool thread still blocked after group kill"
        # The exec RETURNS (killed) — Exit -15 (SIGTERM), not a hang.
        assert results, "exec never returned"
        assert results[0].success is False
        assert results[0].exit_code == -15
        assert tools._PROCESS_REGISTRY.get("pk-cancel") in (None, [])

    def test_cancel_all_unknown_session_returns_zero(self):
        assert tools.cancel_all_processes("pk-nope") == (0, 0)

    def test_cancel_all_none_sweeps_all_sessions(self):
        marker = "8.13"
        t = threading.Thread(target=lambda: tools._exec_command(
            f"sleep 300 {marker}", "/tmp", timeout=60, session_key="pk-sweep",
        ), daemon=True)
        t.start()
        assert _wait_for(
            lambda: tools._PROCESS_REGISTRY.get("pk-sweep"), timeout=5.0,
        ), "process never registered"
        killed, unkillable = tools.cancel_all_processes(None)
        assert (killed, unkillable) == (1, 0)
        t.join(timeout=5.0)
        assert not t.is_alive()
        assert _wait_for(lambda: not _marker_procs(marker), timeout=3.0), (
            f"process survived global sweep: {_marker_procs(marker)}"
        )


class TestEscalationSigkill:
    """Test 4 — SIGTERM-ignoring leader: the 2s grace runs, then SIGKILL."""

    def test_escalation_sigkill(self):
        marker = "pk-term-proof-5d7e"
        killed, unkillable = tools.cancel_all_processes("pk-never")  # control: empty
        assert (killed, unkillable) == (0, 0)

        results = []

        def run():
            # trap "" TERM: the SHELL (group leader + registered proc)
            # ignores SIGTERM — only the SIGKILL escalation can stop it.
            # The sleep is a CHILD of the trapped shell: it dies instantly
            # on SIGTERM, so the marker proves the CHILD kill, while the
            # `while` loop keeps the TERM-immune LEADER alive for the
            # SIGKILL escalation. (A foreground `sleep 30` under a trapped
            # shell would exit early via the child's death — not a real
            # SIGKILL case.)
            results.append(tools._exec_command(
                f"trap '' TERM; sleep 30 {marker} & while true; do true; done",
                "/tmp", timeout=60,
                session_key="pk-escalate",
            ))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert _wait_for(
            lambda: tools._PROCESS_REGISTRY.get("pk-escalate"), timeout=5.0,
        ), "process never registered"

        t0 = time.monotonic()
        n, n_unkillable = tools.cancel_all_processes("pk-escalate")
        elapsed = time.monotonic() - t0
        assert (n, n_unkillable) == (1, 0)
        # The grace window PROVES SIGTERM was delivered and ignored: a bare
        # kill would return in <0.5s; the trap holds the leader for ~2s.
        assert elapsed >= 1.5, (
            f"escalation returned in {elapsed:.2f}s — SIGTERM grace never ran"
        )
        assert elapsed <= 6.0, f"escalation took {elapsed:.2f}s — SIGKILL too late"
        t.join(timeout=5.0)
        assert not t.is_alive()
        assert _wait_for(lambda: not _marker_procs(marker), timeout=3.0), (
            f"SIGTERM-immune leader survived SIGKILL: {_marker_procs(marker)}"
        )
