# Context mask read-loop — agents churn on masked results and never write

**Date:** 2026-10-14 (evening)
**Scope:** develcakes repo — HEAD `c30cb5e5` (SPEC-32), plus the two uncommitted working-tree edits noted below
**Files modified: none** — read-only audit (trace + live session/feed evidence)

---

## Verdict

The periodic observation-mask sweep introduced by SPEC-30 is the loop generator. Every four tool
results it masks every older result down to the newest `keep_last` — **with no current-turn
guard** — so a read-heavy turn keeps losing its own working set. The two nets that are supposed
to catch this do not cover the observed usage pattern:

- **Pins** only arm on the *third* successful read of a path; read-once/read-twice files are never
  protected, and the pin runs *after* the mask decision (`runtime.py:2451` vs `:3742`).
- **SPEC-32 recovery** only covers `read_file` with an exact `(path, offset, limit)` fingerprint;
  `exec_command` outputs — the coder's *primary* read tool — are masked with no recovery path.

Two amplifiers make it worse:

- **JEV failure = mask-all.** When the DecisionModel call cannot answer,
  `classify_mask_candidates` returns `None` and `apply_mask_verdicts(..., None)` masks **every**
  candidate. A dead decision card turns "maybe mask" into "mask everything".
- **The stubs are not model-actionable.** `[masked — ...]` tells the model nothing about how to
  get the body back; the pre-SPEC-32 `[unchanged]` notice claimed the bytes were fine while the
  body was gone.

The repo map is **not** the cause. Its generation/ranking is clean; the only contribution is the
header line "confirm with git/grep before acting", which spawns verification reads that the mask
then deletes.

---

## Evidence (live, this evening)

### Coder — `special:coder.json.migrated` (234 messages), 18:52–18:59

The saved session is full of masked stubs mid-turn:

- 18:52:51 `[masked — exec_command offset=0 (1856 chars removed)]`
- 18:53:50/53:58 `[masked — exec_command ... (3737 / 5435 chars removed)]`
- 18:54:54 `[masked — read_file utils/stt.py offset=0 (3574 chars removed)]`
- 18:56:15 assistant: "Tool outputs are being masked. Let me try smaller outputs."
- Final message 18:59:38: "Stopped — I'm halting the round here without writing anything."

Census over the session: **100 `exec_command`, 40 `read_file`, 0 `write_file`/`edit_file`**.
No code was written in a 143-tool-call turn.

### Supervisor — feed cards 19:20–19:27 (UTC 02:20–02:27 next day)

` .crabcakes/feed.json` shows the same couple of commands re-issued every ~40 seconds for
7+ minutes: "git status --short && git log --oneline -3 / -5 / -8", "git diff --stat", and
repeated "Supervisor is reading .crabcakes/context.md" cards. The user called it out at
19:28:48 ("look like you got stuck in a read loop"); the supervisor admitted re-issuing
near-duplicate commands because probes kept returning `[masked —]`.

### State

- App restarted **19:28:15** — it runs HEAD plus the uncommitted edits below.
- Working tree: `M agent/context_strategy.py`, `M agent/runtime.py` (both from the supervisor's
  keep_last investigation at 19:08–19:18; facts board shows its lint-fail entries).
- No writes were made by this audit.

---

## Mechanism trace

### F1 — The sweep runs mid-turn with no current-turn guard

- Cadence: `runtime.py:1858-1863` — every `_obs_since_mask >= 4`, call `_maybe_mask_observations`.
- Body: `runtime.py:3742-3758` — band gate `landing_goal < estimate <= soft_ceiling`
  (`:3744-3748`); landing goal = `min(15% of hard ceiling, 24_000)` (`:1649-1660`, `:376-379`).
- Selection: `context_strategy.py:476-507` — newest `keep_last` results exempt; everything older,
  ≥500 chars, not already stubbed, not protected is a candidate. `keep_last` default is now 6
  (uncommitted change; was 3).
- Stubs: `context_strategy.py:529-576` — `[masked — {tool} {path} offset={offset} ({n} chars removed)]`;
  a `summarize` verdict keeps the mask line plus `content[:200]`.
- Contrast: `panic_prune` already protects the current turn via `_last_user_index`
  (`context_strategy.py` ~`:583-604`); the periodic sweep has no equivalent guard. SPEC-30's
  design table never mentions one.

### F2 — Results can be masked before the model ever sees them

Results land at the end of iteration N; the sweep runs at the top of iteration N+1, before the
next LLM call. With `keep_last=3`, a batch of 4 loses its oldest element in that same sweep —
the model's next request never contains it. At `keep_last=6` a single batch is safe, but the
turn-scoped bound remains: in a 100-result turn everything older than the newest 6 is masked.
Cadence math: ~25 sweeps per 100 results.

### F3 — Pins protect the wrong moment

`_note_tool_path` (`runtime.py:3647`) / `_protect_paths` (`:3688`) arm only on the third
successful read while mtime+size match; the call site is `:2451`, *after* the dedup decision and
*far after* the sweep that produced the mask. The coder's files were each read once or twice —
nothing pinned. Pins are future-mask protection, not recovery.

### F4 — SPEC-32 recovers `read_file` only

`_key_body_in_transcript` (`runtime.py:~3834-3868`) + the `_dedup_read_output` match branch
(`:3874-3916`) return the full body when no live `read_file` copy for the key remains. Correct as
specified — but the spec explicitly scoped out exec: "no exec-command policy … once a masked read
can be read again, the shell workaround dies on its own" (SPEC-32 §1). Tonight's coder used
`exec_command` as its *primary* reader, not a workaround; masked exec outputs have no net at all.

### F5 — JEV failure masks everything

`agent/llm/jev_client.py:226-248` — `classify_mask_candidates` "Returns `None` when the whole
call fails" (`:229`, `:238`, `:248`). `_maybe_mask_observations` passes those verdicts straight
into `apply_mask_verdicts`; `None` means mask-all candidates. `panic_prune` also calls
`apply_mask_verdicts(conv, indices, None)` (`context_strategy.py` ~`:604`). A dead DecisionModel
card therefore converts the tier from "curate" to "delete".

### F6 — The stubs give the model no next action

`[masked — ...]` has no "re-read to restore" hint; the model reads it as the system withholding
content and fragments its reads (smaller outputs) instead of re-reading. The pre-SPEC-32
`[unchanged]` notice was worse: it asserted the bytes were fine while the body was masked
(gaslighting; only the `read_file` half is fixed by SPEC-32).

### F7 — The stuck detector cannot see the loop

`_check_stuck` (`runtime.py:3387-3431`) fires on the same tool + *exact same args hash*, 3+ times
in 10 calls. The loop's re-issues vary slightly (`git log -3` vs `-5`), so it never fires; and its
advice text — "Consider: re-reading the file" — sends the agent straight back into the mask.

### F8 — Repo map: not the generator, one aggravating line

SPEC-31 (`e8911868`) ranking/anchoring is sound; `tests/test_repo_map_v2.py` 22 green. The header
(`runtime.py:386-393`) now says "Entries may be stale — confirm with git/grep before acting",
which instructs verification reads that masking then deletes. Keep the map; fix the mask.

---

## Ruled out

- **Repo-map generation/ranking** — clean; not implicated in the loop.
- **SPEC-32 implementation** — matches spec; 13 tests green; gap is scope (read_file only).
- **Compiled-turn / facts injection** — no loop evidence in the sampled sessions.
- **`keep_last` 3→6 bump** — directionally right but insufficient: turn-scoped bound remains,
  exec path uncovered, JEV failure still masks all. Not a cure.
- **Live-config leak (SPEC-22 era)** — fixed in the prior unit (autouse HOME+XDG isolation;
  store .bak + empty-overwrite guard); no new leak observed in these runs.
- **Agents/providers loss** — prior incident; restored; not implicated here.

---

## Recommended fixes (ranked)

1. **Turn immunity for the periodic sweep.** Do not mask results produced since the last user
   message — mirror `panic_prune`'s `_last_user_index` guard. Smallest correct change: filter
   candidate indices in `_maybe_mask_observations`, or pass a floor into `select_maskable`.
2. **Decide the JEV failure contract.** `verdicts is None` should mask **nothing** (fail-keep)
   and log loudly; or fall back to a deterministic keep-newest-N. Review `panic_prune`'s
   unconditional mask-all through the same lens.
3. **`exec_command` retention parity.** Either extend recovery to exec outputs or exclude
   current-turn exec outputs from masking (item 1 covers live turns; cross-turn parity remains
   open).
4. **Make the stubs actionable.** Append a recovery instruction to the mask stub text in
   `apply_mask_verdicts` / `mask_old_observations`; align the SPEC-32 notice wording.
5. **Pins (optional).** Consider pinning on the first successful read with a size cap — or leave
   pins as-is once item 1 lands.
6. **Stuck detector.** Add near-duplicate detection (same tool + same path/args prefix), revise
   the advice text, and consider a "masked-result streak" signal.

---

## Notes on current state

- The two uncommitted edits (`keep_last` 6 + the call-site change) are in the running app but not
  committed — commit hygiene for whoever lands the real fix.
- Masked stubs are permanent in saved transcripts: the affected sessions (coder, supervisor) stay
  shredded on resume. `/clear` or fresh sessions are cleaner than resuming them.
- Method: read-only trace at HEAD `c30cb5e5`; live transcripts (`special:coder.json.migrated`,
  `special:supervisor.json`), `.crabcakes/feed.json` cards, and the runtime/strategy source.
  No files modified by this audit.

---

## Resolution (2026-10-15)

- **Item 1** (turn immunity), **item 2** (JEV fail-keep), and **item 4** (actionable stubs) closed by SPEC-33 (`3a24d8c7`).
- **Item 3** (exec retention parity) closed by SPEC-33b. A masked `exec_command` result is detectable as dead. The stub says re-run. The next run returns the new output. The synthetic `<exec:session>` pin is dropped on stat failure, so a shell result stays maskable.
- **Item 5** (pin on first read) closed by SPEC-33b. One successful `read_file` pins the path. A changed file is dropped and re-pinned at count 1.
- **Item 6** (near-duplicate stuck detection) is explicitly deferred. This round adds no `post_decision` call site.
