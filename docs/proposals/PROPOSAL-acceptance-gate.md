# PROPOSAL: The Acceptance Gate — make "done" mechanically re-provable

**Date:** 2026-10-09
**Author:** Hermes (external audit session; read-only investigation over 2026-10-08/09)
**Status:** Adopted whole as Program 1 of `PROPOSAL-META.md` (2026-10-21). That file is the priority. This file is the design.
**Trigger:** PM question 2026-10-09 ("if you could fix one thing about develcakes, what would it be?") following the `/clear`, smart-scroll-follow and stale-delta investigations of 2026-10-08.
**Depends on:** nothing to land first — SP1 is self-contained and additive.
**Related:** `docs/proposals/PROPOSAL-post-responsiveness-priorities.md` (2026-09-11 — the diagnosis this operationalizes for behavioural claims); `docs/specs/SPEC-TEST-DEBT-1.md` (fail-baseline burn-down); `docs/audits/2026-10-08-CLEAR-STORE-RESURRECTION-READ-ONLY.md`; `docs/audits/2026-10-08-STALE-DELTA-SUPERSEDE-FLOOD-READ-ONLY.md`; post-mortem `2026-09-07-SPEC-AUDIT-CLEANUP-2` (`tautological-assertion` pattern).

> **Premise.** The 2026-09-11 proposal already named the root weakness — *"not craftsmanship: verification"* — and proposed detection infrastructure (CI, perf bounds, typing, burn-down). This proposal is the **behavioural half of that program**, and it exists because the September items, even if fully landed, would not have caught this week's failures. CI that runs a vacuous test reports green; typing cannot see a wrong-object assertion; a perf bound cannot see a scroll-follow that silently never fires. What was missing is narrower and mechanical: **proof that each load-bearing claim CAN fail, and a binding between the claim and a runnable artifact that re-executes it.** Two failure modes, both observed live this week:
>
> 1. **Vacuity** — the scroll-follow tests asserted the OUTER ScrolledWindow adjustment (a degenerate object that cannot move): the suite stayed green while at-bottom follow was broken in the shipped default. Recurrence, not a new class — `TestSmartScroll`'s docstring already warned about the exact wrong object; SPEC-19 SP1 re-made the mistake months later. A lesson is not a mechanism until something can fail on it.
> 2. **Claim drift** — acceptance criteria live as prose checkboxes in specs; nothing re-runs them. The `/clear` fix commit shipped "129 passed" as its evidence while being un-importable from a clean checkout (untracked `live_bridge.py`, uncommitted prompts; fixed later in `4f736884`). The commit's own evidence mechanism could not detect its own defect class.

---

## 1. Evidence — what this would have caught (2026-10-08/09 alone)

| # | Incident | How it escaped | Row that catches it |
|---|---|---|---|
| 1 | Smart-scroll follow dead under the live tier (default ON since `acc3b497`; PM noticed by feel) | Vacuous tests: outer vadjustment (degenerate: `upper == page_size`); zero scroll rows in the SP1/SP2 probe files; SP1's sign-off claimed the scroll invariant held — the acceptance box stayed unchecked and nothing re-ran it | R-2 |
| 2 | `/clear` fix not self-contained at HEAD | `git ls-tree` vs imports checked by a human auditor, not a mechanism | R-5 |
| 3 | Ghost-turn execution — superseded turns keep streaming, run tool batches, make further LLM calls; approvals ungated (probe-verified in the 2026-10-08 audit) | Behaviour never probed in either direction; only found by external audit | R-4 (registers with the fix) |
| 4 | Launch crash from half-wired `_build` (SP4 wiring above ARH construction, 2026-10-08) | No test constructs `MainWindow`; the crash passed lint and the whole suite | R-3 |
| 5 | Fixture sessions in the live `transcript.db` (~93 % of 61k turns under test keys, Oct 2–8, per the /clear audit) | Live-state leaks are fixed per-incident (2026-10-01 class) but nothing detects a new one | R-6 |

Context numbers: **74 commits in the last 8 days, 41 fix-labelled** — more than half the daily motion is rework of things previously declared done. **179 test files / ~4.7k test functions** — quantity is not the gap; provability is. The known-failures baseline (40 in the September records) is used as the regression bar ("fail-set byte-identical" is the no-regression proof) — a bar this gate makes machine-checked instead of remembered.

## 2. Design

### 2.1 Acceptance rows — the unit of proof

An acceptance row binds ONE behavioural claim to TWO runnable things: the node that proves it, and the mutation that proves the node can fail.

```yaml
# docs/acceptance/rows.yaml (excerpt)
- id: R-2
  claim: "Injected rows follow the bottom; reading position holds when scrolled up."
  source: docs/specs/SPEC-19-LIVE-CHAT-SURFACE.md §6 G1b/F10 (2026-10-08 re-derivation)
  node: >-
    tests/test_live_guard.py::TestInjectionScroll::test_injected_rows_follow_at_bottom
  mutation: tests/mutations/R-2.diff     # deletes the pin line in _inject_script
  added: 2026-10-08
```

**Run semantics — "no red, no green":** the gate runs each row **twice**:

1. **Mutated** — the `mutation` diff applied in a throwaway tree copy; the node MUST FAIL.
   If it passes, the row is vacuous and the gate refuses. This is the structural fix for
   failure mode 1, and it cannot silently rot: an unappliable diff fails the gate loudly.
2. **Clean** — the working tree; the node MUST PASS.

Details:
- Mutations run in a temp copy (`git archive HEAD` + working-tree overlay for uncommitted
  files, or rsync excluding `.venv/.git`) — the live tree is never mutated. This mirrors
  the repo's existing throwaway-`/tmp`-copy and worktree-baseline-diff patterns.
- `node` may be a pytest node id or a committed script (e.g. `scripts/probes/*.py` for
  WebKit/GUI rows — **gitignored `.debug/` artifacts do not qualify**; acceptance evidence
  must live in the tree).
- Per-row runner wrap declared in the row (`plain` / `xvfb` / `xvfb+aa-exec`) — the same
  conventions the tests already use.

### 2.2 The runner — one command

`scripts/acceptance.py` (committed; no framework — a few hundred lines, stdlib + pytest):

| Subcommand | Runs |
|---|---|
| `rows` | every registered row (mutated-fail, then clean-pass); per-row evidence lines |
| `suite` | full pytest (fold in the existing chunked-run habit), fail-set diffed against `tests/known_failures.txt` — may only SHRINK, a new failure refuses |
| `smokes` | R-3 window smoke + R-5 clean-checkout import smoke (below) |
| `all` | everything above + `.acceptance/last-run.json` + a stdout summary table |

- **R-3 window smoke:** commit the existing headless `MainWindow` construction smoke as
  `scripts/window-smoke.py` (it currently exists only in an external audit toolchain, not
  in the repo). Runs under xvfb with an isolated `XDG_CONFIG_HOME`; no WebKit load → no
  aa-exec needed. Mutation: move the ARH wiring above construction — the exact 2026-10-08
  crash class — and the smoke must fail.
- **R-5 clean-checkout smoke:** `git archive HEAD | tar -x -C <tmp>` → import `main` and
  construct the agent registry headlessly. Fails if any import target or test dependency
  is untracked (the F-1 class). Mutation: delete (`git rm --cached` in the copy) a required
  file.
- **R-6 live-state tripwire:** mtime/hash snapshot of `~/.config/develcakes/` taken before
  and after the gate run; ANY change fails the gate. One mechanism against the entire
  "tests write into live stores" class.

### 2.3 First row batch (register what this week produced)

| Row | Claim | Node (exists today) | Mutation |
|---|---|---|---|
| R-1 | `/clear` durably clears: next load → None; a refused clear leaves store rows AND the in-memory conversation intact | `tests/test_live_bridge.py::test_arh_clear_conversation_deletes_store_rows_behaviorally` (+ the two real-surface clear tests in `test_live_guard.py`, landed `5a17aaf2`) | remove the `delete_session_rows` call in `clear_conversation` |
| R-2 | Injected rows follow the bottom; reading holds; button-then-append follows | `tests/test_live_guard.py::TestInjectionScroll` (3 tests, landed `600dfd42`) | delete `el.scrollTop = el.scrollHeight;` in `_inject_script` |
| R-3 | `MainWindow` constructs; handler wiring present | `scripts/window-smoke.py` (committed with this unit) | re-order the ARH wiring above construction |
| R-4 | *(registers with the stale-turn fix)* a superseded/cancelled turn executes nothing further | new node written with that fix | remove the staleness check |
| R-5 | HEAD is self-contained — the app imports from a clean archive | clean-checkout smoke | `git rm --cached` a required file in the copy |
| R-6 | No gate-run writes reach live state | tripwire (self-checking) | run one known-leaky node with isolation disabled |

### 2.4 Loop-protocol wiring (the part that makes it stick)

- **Spec template:** add an "Acceptance rows" table to the spec skeleton; rows are
  registered **at spec-write time** (node + mutation), not after implementation. A spec
  that cannot name its mutation has not defined its claim precisely enough.
- **Supervisor:** refuses phase acceptance without a gate artifact covering the rows the
  phase touched (`scripts/acceptance.py rows --ids R-1,R-2` output attached).
- **Debugger:** standing first task per phase = run the gate; adversarial work starts from
  a green gate.
- **Row economy:** rows prefer small sub-second nodes; the gate is not a coverage metric.
  `rows` should stay in the minutes; `suite` is for phase boundaries.

### 2.5 Non-goals

- Not a coverage target and not "test everything" — only load-bearing claims get rows.
- Not a CI service. It runs locally/in-session first; once the September P0.2 CI lands,
  CI runs THIS gate (the gate is the thing worth running). `ls .github` is still empty and
  `pyrightconfig.json` still excludes `ui/` as of 2026-10-09 — the September P0 items
  remain open; this proposal does not duplicate them.
- Not retroactive — existing specs register rows lazily, next time a phase touches them.

## 3. Effort, risk, what could break

- **SP1** (runner + `rows.yaml` + `tests/mutations/` + R-1/R-2/R-3/R-5/R-6 + the
  `known_failures.txt` artifact): 1–2 days.
- **SP2** (spec template + protocol wiring + R-4 when its fix lands): ~1 day.
- **Risk:** LOW–MEDIUM. Main costs: mutation diffs need upkeep when files move — the gate
  fails loudly on an unappliable diff, by design; temp-copy runs add wall-clock — bounded
  by small rows; `known_failures.txt` needs the same shrink-only governance the team
  already practices, now machine-checked.
- **Built-in abuse filter:** rows asserting trivia cannot be registered — trivia cannot be
  broken by a mutation, so its mutated run passes and the row is refused.

## 4. Relationship to existing decisions

- `PROPOSAL-post-responsiveness-priorities.md` (2026-09-11) — unchanged as the program's
  P0; this is its behavioural layer (claim detection, not only perf/type detection).
- `SPEC-TEST-DEBT-1` / the 40-failure baseline — continues as-is; `known_failures.txt`
  merely turns the "byte-identical set" ritual into an artifact the gate checks.
- `DEFERRED-ITEMS.md` — re-opens nothing parked.

## 5. Recommendation

Land SP1 next: it is additive, self-contained, and pays for itself immediately by
re-running this week's three fixes as proofs. Then SP2's wiring so every future phase
arrives with its rows. Success is one sentence: **the next time something is declared
done, a single command in the repo can disagree.**

Open questions for the PM:

1. Approve SP1 scope as written (runner + first batch; new files under `scripts/`, `docs/acceptance/`, `tests/mutations/` only)?
2. Rows for the SPEC-16/19 work already shipped: register now (this batch) or lazily?
3. Keep CI out of this unit (September's P0.2 owns CI) — agreed?

---

**Provenance.** Derived from a read-only survey (2026-10-09); no code was changed. Evidence
cites live in the Related audits and the commit history named above. This document is the
only artifact of the survey.
