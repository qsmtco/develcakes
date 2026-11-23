# SPEC-66 Wave 3 — Post-Implementation Adversarial Audit

**Date:** 2026-11-22
**Auditor:** Debugger
**Subject:** the uncommitted working tree on main @ `55e5dce0` — SPEC-66 Wave 3 implementation + Supervisor recovery
**Spec:** `docs/specs/SPEC-66-FIX-PRIORITY-PASS.md` (v4; §12 wins on conflict)
**Brief:** `.crabcakes/debugger_audit_briefs/2026-11-22-SPEC-66-WAVE3-AUDIT-BRIEF.md`
**Pre-flight audit (mine):** `docs/audits/2026-11-18-SPEC-66-PREFLIGHT-ADVERSARIAL-AUDIT.md`
**Probe artifacts:** `.crabcakes/audit_scratch/w3_*.py` (all re-runnable)

---

## VERDICT

**CONDITIONAL PASS — 0 CRITICAL / 3 HIGH / 5 MEDIUM / 4 LOW.**
Do not accept as-is: the HIGH findings are a spec-mandated test-update omission
(§2.10), a functional fail-open hole in the chat surface (§2.11), and a
gate-integrity defect that hides crashes (§2.3 of SPEC-47). The two
hard-audit sections the brief names — the launch-crash reorder and the
mutation-diff integrity — are **CLEAN** and are answered explicitly below.

Everything else the brief claims green is verified green: 22/22 gate rows PASS,
all eight mutation diffs apply, every mutant fails its node for the *intended*
reason, the /stop seams are wired exactly once, packaging produces a correct
wheel, and the coder `test_run` un-force is complete.

---

## Severity counts

| Severity | Count |
|---|---|
| CRITICAL | 0 |
| HIGH | 3 |
| MEDIUM | 5 |
| LOW | 4 |
| **Total** | **12** |

---

# HIGH findings

## HIGH #1 — The 54 pre-existing Telegram tests are now racy; 17 of them fail with any realistic transport latency

**Severity:** HIGH
**Spec clause:** §2.10 — *"Tests that assert a fake transport received the text call `flush_outbound()` first. `tests/test_telegram_bridge_handler.py` is the file. Update each assertion that reads the fake immediately after a send."*
**Location:** `ui/handlers/telegram_bridge_handler.py:737-778` (`_deliver` / `flush_outbound`), `tests/test_telegram_bridge_handler.py` (unchanged by this tree)

### Root cause

`_deliver` now sends on a worker thread. The spec required every assertion in
`tests/test_telegram_bridge_handler.py` that reads the fake immediately after a
send to call `flush_outbound()` first. **That file was not touched by this
tree.** `git status` shows it unmodified. The 21 tests in that file that read
`.sent` now assert against a thread that has not been scheduled yet.

### Evidence

```
$ cd /home/mushy/projects/develcakes && .venv/bin/python .crabcakes/audit_scratch/w3_telegram_flush_audit.py
tests that read `sent`: 21
  WITH flush_outbound(): 0 -> []
  WITHOUT flush_outbound(): 21
      test_foreign_chat_text_refused_never_routed
      test_routing_when_arh_missing_refuses
      test_feed_card_is_not_sent_to_phone
      test_supervisor_reply_forwards_to_phone
      ... (21 total)
```

The vacuity is not visible with a zero-latency fake. With a 3 ms delay
(a realistic socket), 17 of the 54 tests fail:

```
$ .venv/bin/python .crabcakes/audit_scratch/w3_telegram_vacuity.py
=== 1. flake detection: 20 consecutive runs of the 54-test file ===
  0/20 runs failed

=== 2. with a 3 ms transport delay (models a real socket) ===
  rc=1
  summary: ======================== 17 failed, 37 passed in 0.53s =========================
  failed tests (17):
      FAILED tests/test_telegram_bridge_handler.py::test_foreign_chat_text_refused_never_routed
      FAILED tests/test_telegram_bridge_handler.py::test_routing_when_arh_missing_refuses
      FAILED tests/test_telegram_bridge_handler.py::test_supervisor_reply_forwards_to_phone
      FAILED tests/test_telegram_bridge_handler.py::test_reply_over_4096_hard_split
      FAILED tests/test_telegram_bridge_handler.py::test_reply_splits_on_paragraph_boundary
      FAILED tests/test_telegram_bridge_handler.py::test_html_card_reply_flattened_to_text
      FAILED tests/test_telegram_bridge_handler.py::test_plain_text_reply_passes_through_unchanged
      FAILED tests/test_telegram_bridge_handler.py::test_mixed_html_reply_flattens_fence_keeps_prose
      FAILED tests/test_telegram_bridge_handler.py::test_mixed_html_reply_crlf_and_angle_prose
      FAILED tests/test_telegram_bridge_handler.py::test_unknown_slash_command_refused_not_forwarded
      FAILED tests/test_telegram_bridge_handler.py::test_status_command_handled_not_forwarded
      FAILED tests/test_telegram_bridge_handler.py::test_status_without_provider_is_honest
      FAILED tests/test_telegram_bridge_handler.py::test_help_lists_allowlist
      FAILED tests/test_telegram_bridge_handler.py::test_stop_prompts_and_does_not_fire_on_first_command
      FAILED tests/test_telegram_bridge_handler.py::test_stop_wrong_word_aborts
      FAILED tests/test_telegram_bridge_handler.py::test_stop_unwired_is_honest_no_crash
      FAILED tests/test_telegram_bridge_handler.py::test_foreign_refusal_targets_foreign_chat_not_paired
```

**Answering the brief's question directly: yes — the 54 pre-existing tests now
pass vacuously against the threaded path.** They pass today only because the
fake transport returns in microseconds, i.e. they are asserting a thread-start
race, not a send.

### Fix direction

Add `flush_outbound()` immediately before every `.sent` read in
`tests/test_telegram_bridge_handler.py` (21 sites), exactly as §2.10 mandates.
This is a test-only change; no product behaviour needs to move. It should be
part of the Wave 3 commit, not deferred.

---

## HIGH #2 — The chat surface renders NOTHING when the LiveGuard fails to attach (fail-closed → blank chat)

**Severity:** HIGH
**Spec clause:** §2.11 / §11.6 — *"JavaScript is off whenever the filter is not attached."*
**Location:** `ui/views/chat_surface.py:1012-1027` (`_ensure_webview`), `ui/views/chat_surface.py:1067-1105` (`_on_live_guard_compiled`), `ui/views/chat_surface.py:1178-1189` (`_schedule_render` branch), `ui/views/chat_surface.py:1680-1687` (`is_live_enabled`)

### Root cause

Two different pieces of state decide two halves of the same decision:

* **JS enablement** is now driven by the *guard* (`attach()` returned True).
* **Render path selection** is still driven by the *env flag* `self._live_js`.

When the guard fails (compile failure, `add_filter` failure, `decide-policy`
connect failure — all of which `LiveGuard.attach` can return `False` for),
JS stays off, but `_live_js` is still `True`. The chat surface therefore keeps
choosing the **incremental injection** path
(`_inject_new_rows` → `evaluate_javascript`) instead of the full-document
`load_html` path. Injection into a JS-off view is a no-op. The transcript
never renders.

The feed surface has an explicit, documented degrade for this
(`ui/views/feed_surface.py` — *"a static feed … is the correct degrade"*).
The chat surface has **no such degrade**.

### Evidence

```
$ timeout 60 xvfb-run -a .venv/bin/python .crabcakes/audit_scratch/w3_chat_guard_fail_probe.py
live_guard: content-filter compile FAILED — disabling live JS (never run the transcript without the enforcement boundary)
live_js flag = True
JS enabled after guard failure = False
_live_guard = None
row_seq=2 dirty=False _load_in_flight=False
_injected_seq=2 _dom_row_count=2
document bytes loaded? _document_cache_len=0
DOM readback: {}
```

The surface believes it injected 2 rows (`_injected_seq=2`,
`_dom_row_count=2`) and reports `dirty=False`, `_load_in_flight=False` — i.e.
it is idle and satisfied — while the DOM is **empty**. The user sees a blank
chat with no error, no log beyond the guard's one line, and no way to
recover except restarting with a working filter store.

The same state split makes `is_live_enabled()` (`chat_surface.py:1680`) lie:
it returns `True` while JS is off, so `chat_render_handler` keeps taking the
live-append branch and emitting live sections that can never be resurrected.

### Fix direction

Make the render-path decision read the *effective* live state, not the env
flag. Either:

1. Set `self._live_js = False` (or add `self._live_active = False`) in every
   `_on_live_guard_compiled` failure branch, so the surface falls back to the
   full-document `load_html` path — the same degrade the feed surface uses; or
2. Gate the `_schedule_render` branch and `is_live_enabled()` on
   `self._live_guard is not None` (i.e. "the guard attached") rather than on
   `self._live_js`.

Option 1 also fixes `is_live_enabled()` for the render handler. Either way,
add a regression test: *guard compile fails → a message appended afterwards is
visible in the DOM.*

---

## HIGH #3 — The acceptance `suite` gate reads a CRASHED pytest run as "0 failures, 9 fixed" and reports `gate: ALLOWED`

**Severity:** HIGH
**Spec clause:** SPEC-47 §2.3 (gate semantics), §2.5 (shrink-only baseline)
**Location:** `scripts/acceptance.py:267-279` (`extract_fail_set`), `scripts/acceptance.py:282-288` (`run_suite`), `scripts/acceptance.py:344-358` (`cmd_suite`)

### Root cause

`extract_fail_set` parses **only** lines beginning with `FAILED`. A pytest run
that dies with `Fatal Python error: Segmentation fault` produces no `FAILED`
lines for the tests that never ran. `cmd_suite` then compares an empty fail-set
against the 9-entry baseline, reports every baseline entry as "shrunk (fixed)",
and prints `gate: ALLOWED`. **The pytest exit code is discarded entirely.**

### Evidence

The suite crashes in `tests/test_chat_surface.py` — **pre-existing at HEAD**,
not a Wave 3 regression (verified by running the same file against a clean
`git archive HEAD` tree; identical SIGSEGV at `chat_surface.py:914`).

```
$ .venv/bin/python .crabcakes/audit_scratch/w3_suite_gate_probe.py
pytest exit code = -11   (0=all pass, 1=tests failed, 3=INTERNAL_ERROR/crash, 5=no tests)
'Fatal Python error' in output: True
'Segmentation fault' in output: True
extract_fail_set() -> 0 entries: []
diff_fail_set() -> allowed=True new_failures=[]
shrunk (reported as FIXED): 9
    tests/test_awareness_memory_index.py::test_index_is_separate_from_facts
    tests/test_enforcement.py::TestV1ResolvedBinaryGate::test_dot_in_path_refused
    ... (9 total)
=> the gate reports a CRASHED suite as 'gate: ALLOWED' with 9 'fixed' tests.
```

And the gate's own output:

```
$ .venv/bin/python scripts/acceptance.py suite
pytest exit=3; failures=0 (baseline=9)
  shrunk (fixed): tests/test_awareness_memory_index.py::test_index_is_separate_from_facts
  shrunk (fixed): tests/test_enforcement.py::TestV1ResolvedBinaryGate::test_dot_in_path_refused
  ... (9 entries) ...
gate: ALLOWED
```

The same output appears at HEAD (`/tmp/head-clean-96x10mbp`), so this is a
**pre-existing gate defect**, but it is inside the acceptance machinery this
audit is being run through, and it directly contradicts the brief's claim
that the suite is green. **"9 shrunk (fixed)" is false — those 9 tests never
ran.**

### Fix direction

In `cmd_suite` / `run_suite`, treat a non-`0`/`1` pytest exit code as a hard
refusal:

```python
if suite_proc.returncode not in (0, 1):
    print(f"gate: REFUSED — pytest exited {suite_proc.returncode} "
          f"(crash / collection error / no tests)")
    return 1
```

and make `extract_fail_set` surface `Fatal Python error` / `Segmentation
fault` lines as a distinct `crashed` flag rather than an empty fail-set.
Separately: `tests/test_chat_surface.py` segfaulting at
`ui/views/chat_surface.py:914` (`ChatSurface.__init__`) is its own pre-existing
bug and should be tracked — it means the entire chat-surface family is
unexercised by the suite.

---

# MEDIUM findings

## MEDIUM #1 — `flush_outbound()` joins only the LAST started delivery; earlier pending deliveries are unsurfaced

**Severity:** MEDIUM
**Spec clause:** §2.10 — *"flush_outbound joins the last thread this method started."* (the spec sanctions this wording, but §8 and the R-21 row claim it as a general drain seam)
**Location:** `ui/handlers/telegram_bridge_handler.py:767-775`

### Evidence

```
$ .venv/bin/python .crabcakes/audit_scratch/w3_telegram_order_probe.py
=== probe 3: flush_outbound coverage of an EARLIER pending delivery ===
  flush_outbound returned after 5.00s
  transport.sent after flush = []
  -> flush_outbound RETURNED while a delivery was still pending (it joins only the LAST started thread)
```

The probe blocks the *first* delivery on an event and starts a second. Because
`_send_thread` is overwritten by the second `_deliver`, `flush_outbound` joins
the second and returns — while the first is still queued behind the shared
`_send_lock`. The 5-second figure is the first delivery's own socket stall
being observed through the lock, not a bounded join.

### Fix direction

Track pending deliveries in a `set` (or a `queue.Queue` + a single worker
thread) and have `flush_outbound` drain *all* of them:

```python
self._pending: set[threading.Thread] = set()
# in _deliver, after start():
self._pending.add(thread)
# in the worker's finally:
self._pending.discard(threading.current_thread())
# in flush_outbound:
deadline = time.monotonic() + 5.0
while self._pending and time.monotonic() < deadline:
    for t in list(self._pending):
        t.join(timeout=max(0.0, deadline - time.monotonic()))
```

This also removes the need for the `_send_lock` ordering argument below
(MEDIUM #3).

---

## MEDIUM #2 — R-23 does not exist: the registry is out of spec-sync, and R-23 is double-claimed by SPEC-67

**Severity:** MEDIUM
**Spec clause:** §12.8 — *"R-22 covers packaging discovery ONLY … New R-23: stale strings — left_panel placeholder no longer says 'Connect', knowledge/setup.md has no `local-kb`/`CrabCakes`."*
**Location:** `docs/acceptance/rows.yaml` (no R-23), `docs/specs/SPEC-67-FEATURE-PASS.md:547`

### Evidence

```
$ .venv/bin/python -c "...load_rows()..."
row count: 23
ids: ['R-1','R-2','R-3','R-4','R-5','R-6','R-7','R-8','R-9','R-10','R-11',
      'R-12','R-13','R-14','R-16','R-17','R-18','R-18b','R-15','R-19','R-20',
      'R-21','R-22']
DUPLICATE ids: []
registered rows: 22
R-23 present? False
```

The stale-string claims were instead folded into R-22's node
(`tests/test_spec66_package.py:94` asserts `"local-kb" not in setup`).
That is defensible as an implementation choice, but it is not what §12.8 says,
and §12 wins on conflict.

The collision matters: SPEC-67 §7 already claims **R-23** for a different
claim —

```
docs/specs/SPEC-67-FEATURE-PASS.md:547:
| R-23 | Saved window and pane ints reload, and a wrong-typed width is ignored.
        | tests/test_spec67_ui_state.py::test_round_trip_and_bad_width
        | tests/mutations/R-23.diff | 2026-11-18 |
```

so when SPEC-67 lands, either R-23 is a stale-string row (SPEC-66) or a
window-state row (SPEC-67) — not both. Today the id is free, but the
registry has no owner for it.

### Fix direction

Either (a) add the R-23 stale-strings row now (node + mutation), moving the
stale-string assertions out of R-22, and renumber SPEC-67's R-23..R-30 to
R-24..R-31; or (b) amend §12.8 to record that the stale strings ride R-22
and that SPEC-67 owns R-23. Option (a) is closer to the spec; option (b) is
one line. Either way, record the decision in the spec so the two documents
stop disagreeing.

Note (LOW severity, no action needed): the R-22 **node** does catch every
sub-claim, even though the registered **mutation** only touches the include
list:

```
$ .venv/bin/python .crabcakes/audit_scratch/w3_r22_coverage.py
  (b) package-data section DELETED              rc=1  CAUGHT (node fails)
  (b) package-data glob WRONG (system/*.txt)    rc=1  CAUGHT (node fails)
  (c) left_panel stale string RESTORED          rc=1  CAUGHT (node fails)
  (c) setup.md local-kb RESTORED                rc=1  CAUGHT (node fails)
```

---

## MEDIUM #3 — `knowledge/setup.md` now contradicts the other 11 `knowledge/` files (config path + a removed provider)

**Severity:** MEDIUM
**Spec clause:** §2.12 — *"Leave the rest of `knowledge/` unchanged."* (the tension is spec-created, not implementation-created)
**Location:** `knowledge/setup.md` (edited this tree) vs. `knowledge/configuration.md`, `knowledge/agents.md`, `knowledge/commands.md`, `knowledge/features.md`, `knowledge/gateway.md`, `knowledge/install.md`, `knowledge/kb-provider.md`, `knowledge/overview.md`, `knowledge/projects.md`, `knowledge/providers.md`, `knowledge/troubleshooting.md`

### Evidence

```
$ grep -rn "local-kb\|CrabCakes" knowledge/*.md | wc -l
169
$ grep -rln "local-kb\|CrabCakes" knowledge/*.md
knowledge/agents.md
knowledge/commands.md
knowledge/configuration.md
knowledge/features.md
knowledge/gateway.md
knowledge/install.md
knowledge/kb-provider.md
knowledge/overview.md
knowledge/projects.md
knowledge/providers.md
knowledge/troubleshooting.md
```

`knowledge/configuration.md` still documents the **removed** `local-kb`
provider in detail — §"The local-kb Provider" (line 234), the seeding
behaviour (line 267), the `Auxilium.llm_name` patch (line 268) — and the
`~/.config/crabcakes/` path (lines 3, 7, 9, 38, 93, 103, 196) that no longer
exists (`utils/config.py:15-24` resolves `~/.config/develcakes`).

`knowledge/setup.md` now says `~/.config/develcakes/providers.yaml`. Two
knowledge files, one app, two different config paths. `knowledge/kb-provider.md`
documents a provider whose code is gone (`ensure_kb_provider` no longer exists
in `utils/providers_store.py` — confirmed by `tests/test_no_kb_residuals.py`).

### Fix direction

The spec said to leave the rest of `knowledge/` alone, so this is not an
implementation defect — but the resulting user-facing contradiction should be
closed, either by (a) a follow-up row that sweeps `knowledge/` for
`crabcakes`/`local-kb` (the same class as the R-23 stale strings), or by (b)
an explicit spec amendment recording `knowledge/` as accepted stale debt.
Do **not** silently accept the contradiction: `knowledge/configuration.md`
tells a user to edit a path that the app does not read.

---

## MEDIUM #4 — R-21's row claim says "a SINGLE worker"; the implementation is one thread per delivery

**Severity:** MEDIUM
**Spec clause:** §2.10 — *"`_send` and `forward_to_phone` enqueue one daemon worker."*
**Location:** `ui/handlers/telegram_bridge_handler.py:737-778`, `docs/acceptance/rows.yaml:181-187`

### Evidence

`_deliver` constructs a **new** `threading.Thread` per call:

```
$ .venv/bin/python .crabcakes/audit_scratch/w3_telegram_order_probe.py
=== probe 4: is there ONE worker thread, or one PER delivery? ===
  distinct worker thread names for 4 _send calls: ['telegram-send']
  -> 1 worker threads for 4 sends (ONE worker queue)
```

The probe cannot distinguish the two designs because every thread is named
`telegram-send` — but the source is unambiguous: `threading.Thread(...)` is
constructed inside `_deliver`, so 4 `_send` calls create 4 threads. The
"single worker" is a *name*, not a worker.

Consequences that follow from thread-per-delivery rather than a queue:

* `flush_outbound` can only join one (MEDIUM #1).
* Ordering across calls rests on `threading.Lock`, which is **not FIFO** — the
  last-started thread may win the lock first. I could **not** reproduce an
  inversion (0/300 runs out of order at 0 ms and at 2 ms delay), so this is
  recorded as an unproven structural risk, not a finding.

### Fix direction

Make the claim true rather than softening the claim: use one long-lived
daemon worker fed by a `queue.Queue`, which fixes MEDIUM #1 (drain the queue),
removes the FIFO hazard, and makes `flush_outbound` a queue drain. Then the
R-21 claim ("a single worker") becomes accurate and `flush_outbound` becomes
a real barrier.

---

## MEDIUM #5 — The chat surface's first document load can occur while JS is still off (unproven WebKit runtime behaviour)

**Severity:** MEDIUM (flagged as unproven — I could not execute a definitive check in this environment)
**Spec clause:** §2.11, §4 risk table (*"A frame of no-JS before the callback."*)
**Location:** `ui/views/chat_surface.py:1012-1027` (creation + async `compile`), `ui/views/chat_surface.py:1189` (`_issue_load` on the first append)

### Analysis

`LiveGuard.compile` is asynchronous (`store.save(..., _saved, None)` — the
callback fires from the WebKit store). `_ensure_webview` starts the compile and
returns; the first append then issues `load_html` on the very next idle tick.
Whether the store callback has fired by then is a race. If the document is
loaded while `set_enable_javascript(False)` is still in effect and the setting
is only honoured at page-load time, the document's own inline scripts (live
section resurrection, the copy-button bridge) would never run even though the
guard later attaches and flips the setting to `True`.

The spec's §4 table explicitly accepts "a frame of no-JS before the callback",
so this is *within* spec — but the spec does not say what happens to a
document loaded during that window. **I did not verify WebKit's runtime
behaviour for a setting flipped after load; treat this as a risk to close, not
a proven defect.**

### Fix direction

Either (a) defer the first `_issue_load` until the guard callback has fired
(hold the pending document and load it from the callback), or (b) reload the
document once after a successful attach if the first load happened before it.
Add a test that pins the post-attach document actually executes script.

---

# LOW findings

## LOW #1 — R-15's registered node exercises only the feed half of a claim that names both surfaces

**Severity:** LOW
**Location:** `docs/acceptance/rows.yaml:157-163` (node = `…::TestFeedSurfaceFailClosed::test_attach_false_leaves_js_off`)

The row's claim text says *"JS is enabled on the chat **and feed** WebViews only
after LiveGuard compiles AND attaches"*, but the node is the feed half. The
chat half is covered by `tests/test_spec66_live_js.py::TestChatSurfaceFailClosed::test_attach_false_leaves_js_off`,
which is **not** a registered node.

The registered mutation is fine — it breaks both halves:

```
$ .venv/bin/python .crabcakes/audit_scratch/w3_r15_strength.py
=== run the CHAT half of the live-JS test on the mutated tree ===
tests/test_spec66_live_js.py F...F..                                     [100%]
E   AssertionError: JS stayed enabled after attach() returned False — the transcript is running unbounded
E   AssertionError: the feed document is running script without the guard — the pre-fix §11.6 hole
2 failed, 5 passed in 0.28s
```

**Fix direction:** register the chat half as its own row (or make the R-15
node a class node, the R-8/R-9/R-10 pattern), so a future regression that
breaks only the chat surface turns a row red.

---

## LOW #2 — R-3's mutation is unrelated to the launch-crash fix; the crash is caught only incidentally

**Severity:** LOW
**Location:** `tests/mutations/R-3.diff` (removes the ARH construction — a *different* crash class), `docs/acceptance/rows.yaml:29-35`

The Supervisor's fix (moving the `/stop` seam out of `_wire_feed`) is
correctly verified — see the hard-audit section below. But no registered
mutation targets *that* ordering, and R-3's own mutation removes a different
construction. The crash is caught only because R-3's node
(`scripts/window-smoke.py`) constructs `MainWindow`:

```
$ .venv/bin/python .crabcakes/audit_scratch/w3_prefix_crash_caught.py
Re-introduced the pre-fix ordering (agent-cmd /stop seam in _wire_feed).

  R-3    rc=1  CAUGHT (fails)   node=scripts/window-smoke.py
        AttributeError: 'MainWindow' object has no attribute '_agent_command_handler'. Did you mean: '_command_handler'?
  R-15   rc=0  *** NOT CAUGHT (passes) ***   node=…TestFeedSurfaceFailClosed::test_attach_false_leaves_js_off
  R-18   rc=0  *** NOT CAUGHT (passes) ***   node=…TestStopIsACancel::test_stop_returns_cancel_session_key_and_does_not_forward
  R-18b  rc=0  *** NOT CAUGHT (passes) ***   node=…TestCancelAgent::test_cancel_agent_kills_process_group
```

**Fix direction:** optional. Consider an R-3b mutation that re-introduces the
seam ordering, so the ordering rule — not just "the window constructs" — is
pinned by a mutation.

---

## LOW #3 — `tests/test_chat_surface.py` segfaults at HEAD; the chat-surface family is unexercised by the suite

**Severity:** LOW (pre-existing; recorded because it interacts with HIGH #3)
**Location:** `ui/views/chat_surface.py:914` (`ChatSurface.__init__`), `tests/test_chat_surface.py:51`

```
$ .venv/bin/python -m pytest -q tests/test_chat_surface.py
tests/test_chat_surface.py Fatal Python error: Segmentation fault
Current thread ...
  File ".../ui/views/chat_surface.py", line 914 in __init__
  File ".../tests/test_chat_surface.py", line 51 in test_window_is_fifo
```

Reproduced identically against a clean `git archive HEAD` tree — **not** a
Wave 3 regression. It matters here because it is the crash the acceptance
`suite` gate silently swallows (HIGH #3), and because it means the chat
surface — the surface HIGH #2 lives in — has no suite coverage.

**Fix direction:** track the segfault separately; it should not block the
Wave 3 acceptance, but the suite must stop reporting it as "9 fixed".

---

## LOW #4 — The knowledge copy still advertises the removed OpenClaw "Connect" affordance and the Auxilium KB flow

**Severity:** LOW
**Location:** `knowledge/setup.md` (§"How do I connect to the OpenClaw gateway?" — the step "Click **Connect** in CrabCakes toolbar" was correctly removed; the section header remains), `knowledge/kb-provider.md` (whole file documents the removed provider)

The `left_panel.py` placeholder was correctly changed to `No agents yet` and
`setup.md` correctly dropped the `local-kb` auto-seed sentence and the
toolbar "Click Connect" step. The residual is that `setup.md` still keeps a
gateway section whose only actionable instruction was removed, and
`knowledge/kb-provider.md` still documents a removed product surface.

**Fix direction:** fold into the MEDIUM #3 `knowledge/` sweep.

---

# HARD-AUDIT SECTION 1 — The launch-crash reorder (ANSWERED EXPLICITLY)

**Verdict: CLEAN.**

The brief asked: *"is the /stop seam wired exactly once and are there other
same-class ordering bugs in _build?"*

### 1a. The crash was real and is fixed

At clean HEAD `55e5dce0` the app **crashed on construction**:

```
$ cd /tmp/head-clean-96x10mbp && xvfb-run -a .../python scripts/window-smoke.py
Traceback (most recent call last):
  File ".../scripts/window-smoke.py", line 43, in main
    win = MainWindow(application=app)
  File ".../ui/window.py", line 105, in __init__
    self._build()
  File ".../ui/window.py", line 145, in _build
    self._wire_feed(left_panel)
  File ".../ui/window.py", line 746, in _wire_feed
    self._agent_command_handler.set_on_cancel_session(
AttributeError: 'MainWindow' object has no attribute '_agent_command_handler'. Did you mean: '_command_handler'?
```

After the fix, on the working tree:

```
$ xvfb-run -a .venv/bin/python scripts/window-smoke.py
window-smoke: PASS — MainWindow constructed, ARH wired to dependents
```

This is a **pre-existing crash on `origin/main`** (Wave 2 shipped the broken
ordering), now repaired in this tree.

### 1b. The seam is wired exactly once, on the same handler, with the same callback

```
$ timeout 120 xvfb-run -a .venv/bin/python .crabcakes/audit_scratch/w3_stop_seam_probe.py
=== static: set_on_cancel_session call sites in ui/window.py ===
  line 746: self._chat_handler.set_on_cancel_session(
  line 820: self._agent_command_handler.set_on_cancel_session(
=== static: construction site of _agent_command_handler ===
  line 814: self._agent_command_handler = AgentCommandHandler(GLib_module=GLib)
=== runtime: construct MainWindow and inspect the seams ===
  chat_handler._on_cancel_session  = <bound method AgentRuntimeHandler.cancel_agent of <…>>
  agent_command_handler._on_cancel_session = <bound method AgentRuntimeHandler.cancel_agent of <…>>
  agent_runtime_handler.cancel_agent = <bound method AgentRuntimeHandler.cancel_agent of <…>>
  agent-cmd seam is arh.cancel_agent? True
  both seams set (not None)?         True
  wiring calls during construction: [('chat', 'cancel_agent'), ('agent_command', 'cancel_agent')]
```

Exactly **two** call sites, **one per handler**, both bound to the *same*
`AgentRuntimeHandler.cancel_agent` bound method, and the agent-command seam
is now at line 820 — **after** the construction at line 814.

### 1c. No other same-class ordering bug in `_build`

A static analyser walked the construction path
(`_build` → `_wire_chat` → `_wire_projects` → `_wire_agents` → `_wire_feed`)
in call order, tracking every `self._X = …` assignment, and flagged any
`self._X.method(` use whose attribute had not yet been constructed:

```
$ .venv/bin/python .crabcakes/audit_scratch/w3_build_order_probe.py
=== construction path order ===
  _wire_chat: lines 147..189
  _wire_projects: lines 190..206
  _wire_agents: lines 207..489
  _wire_feed: lines 490..890
=== findings ===
  (none — every self._X.method() use is preceded by its construction)
=== /stop seam ordering ===
  _agent_command_handler constructed at line 814
  chat seam wired at line 746
  agent-cmd seam wired at line 820
  agent-cmd seam AFTER construction? True
```

**No further same-class bug found.** The NOTE comments at both sites are
accurate and useful.

### 1d. Why the gate did not catch this before

The three hand-written mutation diffs (R-17/R-18b/R-3) shipped **broken** on
main and have been silently failing since Wave 2. Verified against the HEAD
tree:

```
$ cd /tmp/head-clean-96x10mbp
$ git apply --check /tmp/R-17_HEAD.diff  ; echo rc=$?
rc=1
$ git apply --check /tmp/R-18b_HEAD.diff ; echo rc=$?
rc=1
$ git apply --check /tmp/R-3_HEAD.diff   ; echo rc=$?
rc=1
error: patch failed: ui/feed/review.py:64
error: patch failed: agent/runtime.py:1703
error: patch failed: ui/handlers/agent_runtime_handler.py:472
error: patch failed: ui/window.py:220
```

And the gate at HEAD:

```
$ cd /tmp/head-clean-96x10mbp && python scripts/acceptance.py rows --ids R-3,R-17,R-18b
[FAIL] R-3  node=scripts/window-smoke.py
        note: mutation did not apply — row fails loudly (git output above)
[FAIL] R-17 …
[FAIL] R-18b …
```

So the gate *did* report the broken diffs — but the rows were left in a
failing state on `origin/main` rather than being treated as a blocker, and
R-3's failing mutation is exactly why the launch crash went unnoticed.

---

# HARD-AUDIT SECTION 2 — Mutation-diff integrity (ANSWERED EXPLICITLY)

**Verdict: CLEAN. All eight diffs apply; every mutant fails its node for the intended reason; none is self-defeating.**

### 2a. Every diff applies to the current tree

```
$ for id in R-15 R-19 R-20 R-21 R-22 R-17 R-18b R-3; do git apply --check --verbose tests/mutations/$id.diff; done
=== R-15 ===
Checking patch ui/views/chat_surface.py...
Checking patch ui/views/feed_surface.py...
rc=0
=== R-19 ===
Checking patch ui/handlers/agent_command_handler.py...
rc=0
=== R-20 ===
Checking patch agent/runtime.py...
rc=0
=== R-21 ===
Checking patch ui/handlers/telegram_bridge_handler.py...
rc=0
=== R-22 ===
Checking patch pyproject.toml...
rc=0
=== R-17 ===
Checking patch ui/feed/review.py...
rc=0
=== R-18b ===
Checking patch agent/runtime.py...
Checking patch ui/handlers/agent_runtime_handler.py...
rc=0
=== R-3 ===
Checking patch ui/window.py...
rc=0
```

All eight regenerate cleanly with trailing context (the defect class the brief
flagged is gone).

### 2b. Every mutant fails for the INTENDED reason, not an incidental error

```
$ .venv/bin/python .crabcakes/audit_scratch/w3_mutant_purpose.py
--- R-15 (rc=1) node=…TestFeedSurfaceFailClosed::test_attach_false_leaves_js_off
    E   AssertionError: the feed document is running script without the guard — the pre-fix §11.6 hole
    E   assert True is False
--- R-19 (rc=1) node=tests/test_spec66_think.py::test_prose_think_keeps_later_ask
    E   AssertionError: a later unclosed think block ate the command: []
    E   assert [] == ['verify the lexer']
--- R-20 (rc=1) node=tests/test_spec66_coder_tests.py::test_coder_does_not_disable_test_run
    E   AssertionError: the coder branch disabled test_run again …
    E   assert False is True
    E    +  where False = EnforcementConfig(enabled=True, syntax_check=True, test_run=False, lint_check=False, …)
--- R-21 (rc=1) node=tests/test_spec66_telegram.py::test_send_leaves_the_caller_thread
    E   AssertionError: send_message ran on the caller thread — the GTK main thread would block for the length of the socket call
    E   assert <_MainThread(…)> is not <_MainThread(…)>
--- R-22 (rc=1) node=tests/test_spec66_package.py::test_render_and_prompts_are_packages
    E   AssertionError: packages.find does not declare render*: 'include = ["ui*", "agent*", "utils*", "models*", "prompts*", "transport*"]'
--- R-17 (rc=1) node=tests/test_spec66_accept.py::TestNormalizedIndexComparison::test_stage_all_is_never_called_on_accept
    E   AssertionError: handle_accept must not call stage_all
--- R-18b (rc=1) node=tests/test_spec66_stop.py::TestCancelAgent::test_cancel_agent_kills_process_group
    E   AssertionError: assert 'cancelled' == 'cancelled+killed:2'
```

Every mutant trips the **specific assertion the row is about** — no
collection errors, no `ImportError`, no `AttributeError` masquerading as a
pass. This is the strongest form of the check: it demonstrates the node
actually *pins* the claimed behaviour.

### 2c. The R-21 mutant is NOT self-defeating (the defect the brief flagged)

The brief noted the *original* R-21 "still started the worker thread AND ran
inline, so the node stayed green". The regenerated R-21 replaces the whole
thread-start block with an inline `_work()` call — the send runs on the caller
thread, and `_send_thread` stays `None`. The node
(`test_send_leaves_the_caller_thread`) then fails on the *thread identity*
assertion, not on an incidental error (output above). **Fixed.**

### 2d. The R-19 mutant is a faithful reproduction of the pre-fix behaviour

Behaviourally verified on the mutated tree:

```
=== does the mutant still satisfy the SPEC-65 pinned assertion? ===
leading-unclosed swallows -> []
balanced pair removed    -> [ParsedCommand(command='ask', agent='@Debugger', payload='real', …)]
prose + unclosed         -> []
```

The mutant swallows a leading unclosed block (SPEC-65 pinned assertion
preserved) *and* swallows a mid-string unclosed block (the pre-fix bug) — i.e.
it is exactly the pre-fix function, not an over-mutation.

### 2e. Full gate: 22/22 rows PASS

```
$ .venv/bin/python scripts/acceptance.py rows
[PASS] R-1 … [PASS] R-22     (22 rows, every one: mutated rc=1 failed=1, clean rc=0 passed=1)
```

Confirmed independently in two batches (`R-15,R-19,R-20,R-21,R-22` and
`R-3,R-17,R-18b`) and once for the full set.

---

# Vector-by-vector results

## §2.8 think parser — CLEAN (one low-severity note)

The guard runs after balanced-pair removal; the `lstrip().startswith` probe is
exactly as specified; SPEC-65's pinned assertions still hold.

```
$ .venv/bin/python .crabcakes/audit_scratch/w3_think_probe.py
=== SPEC-65 pinned assertions ===
  cmds("<think>\n/ask @Coder \"never closed")           -> []
  cmds("<think>draft</think>\n/ask @Debugger \"real\"")  -> [('@Debugger', 'real')]
=== vector 3: balanced-pair removal leaves a newline ===
  strip(balanced only)                            -> '\n'
  strip(balanced + later unclosed opener)         -> '\n\nAnswer\n<think>more'
=== vector 4: fence unwrap happens AFTER think-strip ===
  cmds(fence inside think block)                  -> [('@Coder', 'x')]
  cmds(think opener inside an html fence)         -> [('@Coder', 'smuggled')]
```

On the brief's specific question — *"can a think block smuggle a fence or vice
versa?"* — the answer is **no, the fence cannot smuggle a command out**:
`_strip_fenced_blocks` runs last, so a fenced block's contents are removed
before the command scan. The `think opener inside an html fence` case is
pre-existing behaviour (the raw, unscanned text already yielded the same
command), not a §2.8 regression.

One structural note (not a finding): the balanced-pair substitution replaces
the block with `"\n"`, so a message that is *only* a think block becomes
`"\n"` — the leading guard then sees a blank string and does not fire. That is
harmless (there is nothing left to scan) and is pinned by
`test_balanced_pair_is_removed_even_when_unclosed_follows`.

## §2.11 JS gating — HIGH #2 (chat surface), otherwise CLEAN

* **JS is off at webview creation in both surfaces.** Verified by runtime probe:

```
$ xvfb-run -a .venv/bin/python .crabcakes/audit_scratch/w3_js_gate_probe.py
=== chat surface ===
  chat compile='filter': at creation JS=False after callback JS=False live_js_flag=True
  chat compile='none':   at creation JS=False after callback JS=False live_js_flag=True
=== feed surface ===
  feed compile='filter': JS=False live_guard=None
  feed compile='none':   JS=False live_guard=None
```

(The chat surface's "after callback JS=False" in this probe is expected: the
probe's fake `compile` passes a non-`LiveGuard` filter object, so `attach()`
correctly refuses. With a real guard both surfaces reach JS=True — verified
separately, below.)

* **No other `set_enable_javascript` call site exists anywhere** in the
  product code — a whole-tree grep returns only `ui/views/chat_surface.py`
  and `ui/views/feed_surface.py`.

* **The kill-switch still wins:**

```
  DEVELCAKES_LIVE_JS unset -> _live_js_enabled() = True
  DEVELCAKES_LIVE_JS=0    -> _live_js_enabled() = False
```

* **Two LiveGuards do not collide** — both surfaces use the same filter
  identifier in the same store, and both compile and attach successfully:

```
$ xvfb-run -a .venv/bin/python .crabcakes/audit_scratch/w3_two_guards_probe.py
chat JS enabled  = True
chat _live_guard = <utils.live_guard.LiveGuard object at 0x…>
feed JS enabled  = True
feed _live_guard = <utils.live_guard.LiveGuard object at 0x…>
```

* **But:** see HIGH #2 — when the guard fails, the chat surface's *render
  path selection* still follows the env flag, and the transcript renders
  nothing.

## §2.10 Telegram threading — HIGH #1, MEDIUM #1, MEDIUM #4

* **The send genuinely leaves the caller thread** — verified:

```
  assert work_thread is not caller  → passes
  assert bridge._send_thread.daemon is True  → passes
```

* **Exceptions do not propagate** — verified by
  `test_send_errors_do_not_propagate` and by the `_Boom` transport in
  `tests/test_spec66_telegram.py`.

* **Ordering inside one call holds** — the chunk list goes out on one worker,
  in order (0/300 runs out of order at 0 ms and at 2 ms delay).

* **But:** the 54 pre-existing tests are vacuous (HIGH #1), `flush_outbound`
  does not drain (MEDIUM #1), and the "single worker" claim is inaccurate
  (MEDIUM #4).

## §4 coder `test_run` un-force — CLEAN

* **No other path forces `test_run=False` for coders.** A whole-tree grep of
  `test_run` in `agent/`, `utils/`, `ui/`, `models/` returns exactly three
  product sites:
  * `agent/runtime.py:2980` — the coder branch, now
    `dataclasses.replace(enf, lint_check=False)` (only `lint_check` forced off);
  * `agent/enforcement.py:1650` — a **project-level** override
    (`project_override.get("test_run", True)`), not a role-based force;
  * `agent/config.py:60,284` — the config default (`True`).

* **The enforcement object is built per turn, not cached stale.** `agent/runtime.py:2971`
  reads `self._config.enforcement` fresh on every tool batch and the coder
  branch produces a new object via `dataclasses.replace`. There is no cache.

* **The node is a real end-to-end turn** (it drives `_run_loop` with a mocked
  LLM and records the config the enforcement tier actually received), and it
  has a control case:

```
$ .venv/bin/python -m pytest -q tests/test_spec66_coder_tests.py
3 passed   (test_coder_does_not_disable_test_run,
            test_coder_still_disables_lint_check,
            test_non_coder_role_keeps_config_defaults)
```

## §5 Rows / mutations coherence — MEDIUM #2, LOW #1, LOW #2

* 23 rows, **no duplicate ids**, 22 registered (R-4 is the `skip_ok`
  placeholder with `node: null`).
* **R-23 does not exist** (MEDIUM #2) and is double-claimed by SPEC-67.
* R-15's node covers only the feed half (LOW #1).
* R-3's mutation targets a different crash class (LOW #2).

## §6 Test quality — HIGH #1 (the vacuity), otherwise good

* The 54 pre-existing Telegram tests read the fake without a flush (HIGH #1).
* The five new Wave 3 test files are otherwise substantive: 41 tests, real
  end-to-end paths (a full `_run_loop` for the coder row, a real
  `LiveGuard`/WebKit view for the JS rows, a real thread-identity check for
  the Telegram row), and every mutant breaks them for the right reason.
* `tests/test_html_guard_sites.py` was correctly rewritten from
  "JS follows the flag" to "JS gated on the attach" — 23 passed.

## Packaging — CLEAN

A **real wheel** was built from the working tree and inspected:

```
$ .venv/bin/python .crabcakes/audit_scratch/w3_wheel_probe.py
pip wheel rc=0
wheel: develcakes-0.1.0-py3-none-any.whl  (1045416 bytes)

  OK    render/__init__.py
  OK    render/feed_html.py
  OK    render/sanitize.py
  OK    prompts/__init__.py
  OK    prompts/system/*.md
  OK    utils/prompt_loader.py
  OK    main.py

  prompts/system entries (14)
```

`setuptools.find_packages` discovers 13 packages including `render` and
`prompts`. `utils.prompt_loader.SYSTEM_DIR`
(`dirname(dirname(__file__))/prompts/system`) resolves to
`site-packages/prompts/system` in that wheel. The `prompts = ["system/*.md"]`
package-data mapping is **required and effective** — without it, 14 system
prompts would be missing from a non-editable install.

---

# Family test results (verified)

```
tests/test_spec66_live_js.py + think + telegram + coder_tests + package   27 passed
tests/test_html_guard_sites.py                                              23 passed
tests/test_spec65_ask_command_hardening.py + test_spec66_stop.py            26 passed
tests/test_telegram_bridge_handler.py + test_telegram_transport.py          92 passed
tests/test_window_telegram_bridge.py + settings_telegram + telegram_store   74 passed
tests/test_live_guard.py                                                     37 skipped (no WebKit filter store)
xvfb scripts/window-smoke.py                                          window-smoke: PASS
```

Note `tests/test_live_guard.py` is **37 skipped** without WebKit — the
`LiveGuard` unit tests never run in the plain environment. That is why the
chat-surface segfault (LOW #3) matters: the surface's own guard integration
has no coverage in the suite.

---

# Recommended disposition

**Do not accept as-is.** Minimum bar for acceptance:

1. **HIGH #1** — update the 21 `.sent` reads in
   `tests/test_telegram_bridge_handler.py` to call `flush_outbound()` first.
   Test-only; ~20 lines; must be in this commit.
2. **HIGH #2** — make the chat surface's render-path decision follow the
   guard state, not the env flag, and add a regression test (guard fails →
   message still visible).
3. **HIGH #3** — make the acceptance `suite` gate refuse a non-`0`/`1` pytest
   exit code, and stop reporting a crash as "9 fixed".

Strongly recommended before merge:

4. **MEDIUM #1 + #4** — move Telegram delivery to a single queue-backed
   worker; make `flush_outbound` a real drain; then the R-21 claim becomes
   true.
5. **MEDIUM #2** — resolve the R-23 ownership (SPEC-66 vs SPEC-67) in the
   spec, and align the registry with §12.8.

Can be tracked separately:

6. **MEDIUM #3 / LOW #4** — the `knowledge/` sweep.
7. **MEDIUM #5** — pin the post-attach document behaviour.
8. **LOW #1 / LOW #2** — register the chat half of the JS row; consider an
   R-3b seam-ordering mutation.
9. **LOW #3** — the `test_chat_surface.py` segfault (pre-existing).

---

*Every finding above was reproduced by execution against the real tree. Probe
scripts are in `.crabcakes/audit_scratch/w3_*.py` and are re-runnable; each
command line is quoted verbatim in the finding it supports.*
