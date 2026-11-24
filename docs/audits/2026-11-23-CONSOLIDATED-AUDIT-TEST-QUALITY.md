# Flagger test-quality audit — Path/Approval/Enforcement surface

**Date:** 2026-11-23
**Auditor:** Flagger (test-quality angle), in parallel with Debugger (code angle)
**Baseline:** main @ b12feb74 (clean), brief at `.crabcakes/CONSOLIDATED-AUDIT-BRIEF.md`
**Scope:** `tests/test_spec66_approval_and_eval.py`, `tests/test_spec66_accept.py`,
`tests/test_enforcement.py`, `tests/test_spec66_fence.py`, `tests/mutations/R-12.diff`,
`tests/mutations/R-13.diff`, `tests/mutations/R-14.diff`, `tests/mutations/R-17.diff`,
`docs/acceptance/rows.yaml`
**Read-only:** no product file was modified. This document is the only file written.

---

## 0. Headline answers (the brief's five questions)

**(1) Do the seven closed reports' regression tests pin their fixes?**
Six of seven are pinned by at least one named test that would fail under a
reverted product change. **Three are pinned only by tests that are NOT the
acceptance-row node** — the row node's own mutation file (R-12.diff) covers
only two of the three code changes in commit b12feb74, so the row's "mutated
must fail" property is weaker than the row's own claim text asserts. Details:
BUG #1 (per-file below). The critical gap is **BUG #4 (node positional rule)
and BUG #3 (cluster rule) have NO mutation file at all** — they are pinned by
unit tests but are outside the acceptance gate's mechanical proof.

**(2) Vacuous tests.** One confirmed vacuity class in
`test_spec66_accept.py::TestNormalizedIndexComparison` — the four "spelling"
cases re-implement `handle_accept`'s normalization **in the test body** and
never call `handle_accept`, so they would still pass if `review.py`'s
normalization were deleted. Two more near-vacuous cases (R-14's env assert on
the fallback site; the R-12 row's section (d) runtime assertion).

**(3) Coverage gaps for attack vectors A–G.** Six of the seven vectors have
**NO test at all**: A (TOCTOU), B (rename/move), C (`.git` walk completeness:
`.git/hooks/`, `objects/`, `worktrees`, `modules/`), D (flag grammars for
`go`/`pytest`/`mypy`/`eslint`/`ruff`; env-var vectors), E (cross-device,
deep chains, `..` inside symlink targets), G (TTL cache staleness). Vector F
is partially covered — the `card.file_path` normalization *is* pinned by R-17's
mutation, but the "foreign file staged between `index.diff` and `commit`"
TOCTOU is not.

**(4) Acceptance-row gaps.** R-12's node has grown sections (a)–(d), but its
`claim` string and `mutation` file were never updated to match: the claim
mentions BUG #2 and BUG #1, the mutation deletes only the BUG #1 alias code.
The alias cache, the cluster rule, and node positionals have **no row at all**.

**(5) Is a deny-list-of-orientations testable?** From the test side: **no, not
by example-based tests.** The current suite is 100% example-based (fixed
fixtures per orientation). A deny-list is unbounded by construction — every
new spelling is a new test. The only sound approach is a **property-based /
generative test**: generate random path spellings and random alias trees and
assert the invariant "no write to a sensitive byte escapes approval." The
repo has no property-based dependency (`hypothesis` is absent from
`pyproject.toml` and from the whole tree), so this needs either a new
dependency or a hand-rolled deterministic generator (seeded `random`, fixed
seed) — the latter is preferable given the acceptance gate's "stdlib + pytest
only" constraint in `scripts/acceptance.py:21`.

---

## BUG #1 — CRITICAL — R-12's mutation file does not delete the R-12 row's own claim

**File:line:** `tests/mutations/R-12.diff:1-32` (runtime hunk),
`tests/mutations/R-12.diff:33-234` (tools hunk);
`docs/acceptance/rows.yaml:101-107`.

**Evidence.** The R-12 row's claim (`docs/acceptance/rows.yaml:102`) asserts
three properties: (i) resolved-path gating at both gates, (ii) a symlinked
sensitive name still requires approval (BUG #2 — the *lexical* disjunct),
(iii) an inode/symlink alias requires approval (BUG #1 — the *alias*
disjunct).

`tests/mutations/R-12.diff` deletes:
- `agent/runtime.py` `_sensitive_relpath`'s alias disjunct (`:27-29`),
- `agent/tools.py` `_sensitive_alias_of` + `_alias_scan` + `_alias_signature`
  + `_alias_scan_roots` (`:47-202`),
- `agent/tools.py` `_write_file`'s alias block (`:212-218`),
- `agent/tools.py` `_edit_file`'s alias block (`:226-231`).

It **does NOT delete the lexical disjunct** (`_lexical_relpath` +
`is_sensitive_path(lexical)`) at either site — `tests/mutations/R-12.diff:24-26`
keeps it as context:
```
     lexical = _lexical_relpath(path_arg, project_path)
     if is_sensitive_path(lexical):
         return lexical
```

So the mutation reverts **BUG #1 (report #6)** only. Yet the row's claim text
says the row covers BUG #2 too. The row's mutated run therefore still passes
the BUG #2 half of the claim. The mutation is *consistent* with the row
mechanically (mutated run must fail — it does, via the alias half), but the
row's **claim/§2.1 mapping is over-stated**: the acceptance artifact asserts
coverage it does not mechanically verify.

**Why this matters for the seven-round pinning question.** The brief says
report #3 (BUG #2, symlink target) was fixed in 761c6508 and report #7
(BUG #1/r6, inode alias) in b12feb74 — two separate commits, two separate
orientations. The gate has **one** mutation file covering only the second.
Commit 761c6508's product change has **no mutation file**, so it is not
mechanically pinned by the acceptance gate at all.

**Reverted-fix failure check.** Reverting 761c6508 (deleting the lexical
disjunct at `agent/runtime.py:501-503` and `agent/tools.py:850-851` /
`:1067-1068`) **does** fail named tests:
- `tests/test_spec66_approval_and_eval.py::test_symlink_sensitive_name_to_plain_target_asks_approval` (`:730`)
- `::test_symlink_sensitive_name_to_plain_target_asks_approval_edit` (`:783`)
- `::test_symlink_sensitive_name_asks_approval_at_runtime_gate` (`:818`)
- `::test_symlinked_crabcakes_dir_asks_approval` (`:910`)

So the *product* fix is pinned by unit tests; it is the *acceptance row
artifact* that is under-covering. Recommend a new row (e.g. R-12b) with a
mutation file that deletes the lexical disjunct, or a second hunk set in
R-12.diff.

---

## BUG #2 — HIGH — BUG #3 (cluster rule) and BUG #4 (node positional rule) have NO mutation file, hence no acceptance row

**File:line:** `agent/enforcement.py:198-215` (`_is_clustered_eval_flag`),
`agent/enforcement.py:294-301` (node positional rule);
`docs/acceptance/rows.yaml` (no row references either);
`tests/mutations/` (26 files — none touches `agent/enforcement.py`).

**Evidence.** `grep -r '^--- a/' tests/mutations/` returns 26 diffs; **none**
targets `agent/enforcement.py`. The two SPEC-66 fixes that live there are:

- **BUG #3** — `_is_clustered_eval_flag` (`agent/enforcement.py:198-215`),
  consumed at `:285` (python) and `:290` (node).
- **BUG #4** — the node-positional rule (`agent/enforcement.py:294-301`),
  which refuses any node command with a positional unless `--check`/`-c` is
  present.

Both are pinned by unit tests:
- `tests/test_spec66_approval_and_eval.py::test_clustered_eval_flag_matcher` (`:491`)
- `::test_node_positional_requires_check_flag` (`:512`)
- `::test_node_eval_flags_are_refused` (`:440`, parametrized `:417-438`)
- `::test_python_eval_flags_are_refused` (`:408`, parametrized `:389-406`)

**Reverted-fix failure check (mutation-style, no execution).**
- Reverting BUG #3 (deleting the `_is_clustered_eval_flag` disjunct from
  `agent/enforcement.py:285` and `:290`) → `test_clustered_eval_flag_matcher`
  fails at `tests/test_spec66_approval_and_eval.py:499`
  (`_is_clustered_eval_flag("-ic", ("c",)) is True` → the function itself
  would be gone → ImportError/AttributeError) **and**
  `test_python_eval_flags_are_refused["python3 -ic 'import os'"]` fails
  (`:400`). Also `test_resolved_sensitive_path_and_python_c` fails at `:383`
  (`assert _validate_test_command("python3 -ic 'import os'") is False`).
  → **PINNED by three named tests.**
- Reverting BUG #4 (deleting the node-positional block at
  `agent/enforcement.py:297-301`) → `test_node_positional_requires_check_flag`
  fails at `:518` (`_validate_test_command("node x.js") is False` → True),
  and `test_node_eval_flags_are_refused["node evil.js"]` / `["node ./scripts/run.js"]`
  fail (`:436-437`). → **PINNED by two named tests.**

**But:** these are *unit* pins. The acceptance gate (`scripts/acceptance.py`)
has no row for them, so a future refactor that breaks them fails `pytest`
but does not fail `scripts/acceptance.py rows`. The brief asks "is anything
load-bearing still un-rowed" — this is the answer: **the two enforcement-layer
grammar rules (cluster + node positional) are the only load-bearing SPEC-66
fixes with no acceptance row**, and they are exactly the two that were
reported as external findings (reports #4 and #5 in the brief's table).

---

## BUG #3 — HIGH — Vacuous: `test_spec66_accept.py`'s spelling matrix re-implements the normalization instead of driving it

**File:line:** `tests/test_spec66_accept.py:84-106`
(`TestNormalizedIndexComparison::test_each_spelling_matches_the_repo_relative_index_entry`).

**Evidence.** The test computes the expected `staged_rel` **inside the test
body**, using the same three lines of logic the product uses
(`tests/test_spec66_accept.py:94-98`):
```python
resolved = os.path.realpath(os.path.join(os.path.realpath(str(repo)), card_path))
staged_rel = os.path.relpath(resolved, os.path.realpath(str(repo))).replace("\\", "/")
assert staged_rel == "b.txt", label
```
It then asserts on that self-computed value and on the git index — it **never
calls `handle_accept`**. The product's normalization lives at
`ui/feed/review.py:67-68`.

**Reverted-fix failure check.** Delete `ui/feed/review.py:67-68` (replace
`staged_rel` with `card.file_path`, i.e. exactly the R-17 mutation's first
half at `tests/mutations/R-17.diff:7-9`) → this test **still passes**: it
computes its own `staged_rel`, and `others == []` because only `b.txt` is
staged. The R-17 mutation is caught by the *other* test in the same class
(`test_stage_all_is_never_called_on_accept`, `:128-142`), which is a
**source-text assertion** (`inspect.getsource` + `".stage_all(" not in
handle_accept`), not a behavioral one.

So the four-spelling matrix (the exact §11.3 requirement:
`b.txt` / `./b.txt` / absolute / `sub/../b.txt`) is pinned by **no behavioral
test**. The product's `staged_rel` for a card whose `file_path` is
`./b.txt` or an absolute path is never exercised end-to-end.

**Second vacuity in the same file.** `test_a_second_staged_file_is_detected`
(`:108-126`) has the same shape: it computes `staged_rel` in the test body
(`:118-119`) and asserts `others == ["c.txt"]` on its own computation. It
never drives `handle_accept`, so the product's "index has other staged files"
refusal at `ui/feed/review.py:128-135` has **no test**. Verified by
grep: the string `"index has other staged files"` appears **nowhere** in
`tests/` except as the product's own log/fail text (`ui/feed/review.py:132-134`).

**Fix direction.** Drive the real `handle_accept` (the file already has the
machinery: `_AcceptHost` at `:186-200` + `_accept_in` at `:203-221` + the
`_SyncThreading` shim at `:161-183`) with each of the four spellings, and
assert on `card.metadata["accept_error"]` — i.e. that the four spellings do
**not** produce `"index has other staged files"`, and that a second staged
file **does** produce it.

---

## BUG #4 — MEDIUM — R-17's row node is a source-text assertion, not a behavioral one; the mutation's second half is pinned only by grep

**File:line:** `tests/test_spec66_accept.py:128-142`;
`tests/mutations/R-17.diff:13-14`.

**Evidence.** `test_stage_all_is_never_called_on_accept` reads the source
text of `ui/feed/review.py` and asserts substrings:
```python
assert ".stage_all(" not in handle_accept, "handle_accept must not call stage_all"
assert ".stage_paths(" in handle_accept
```
This is a **source-shape** pin. It will pass if `handle_accept` calls
`stage_all` through an alias, a `getattr`, a facade indirection, or a
`_mod().git_ops` variant spelled differently (`.stage_all (` with a space,
`stage_all (` without the dot). It is also brittle in the other direction: a
legitimate comment containing `.stage_all(` would fail the row.

**Reverted-fix failure check.** The R-17 mutation replaces
`stage_paths(project_path, [card.file_path])` with `stage_all(project_path)` —
the substring `.stage_all(` appears, so the assertion fails. → the mutation
**is** caught, but only by the textual check. A behavioral rewrite (stage a
second file in the working tree, assert `handle_accept` refuses with
`"index has other staged files"` and does not commit) would catch the same
mutation *and* the "index has other staged files" contract — which today has
no test at all (BUG #3).

---

## BUG #5 — HIGH — Vector A (TOCTOU): zero tests; the alias scan → `open()` gap is unguarded and unpinned

**File:line:** `agent/tools.py:855-858` (alias scan) → `agent/tools.py:900`
(`open(resolved, "w")`); `agent/tools.py:1067-1074` → `agent/tools.py:1042+`
(read-modify-write); `ui/feed/review.py:98` (`index.diff("HEAD")`) →
`ui/feed/review.py:146` (`commit`).

**Evidence.** The alias scan happens at `agent/tools.py:856`
(`_sensitive_alias_of(resolved, project_path)`), and the file is opened for
writing at `agent/tools.py:900`. Between those two points the process can
create the alias — most trivially, the **agent's own `exec_command`** (which
runs before/alongside the write in the same turn), or any concurrent process.
The same gap exists in `_edit_file` (`agent/tools.py:1072` scan → the
read-modify-write that follows) and on the accept path
(`ui/feed/review.py:98` diff → `:146` commit — a second `stage` between them
sweeps foreign files into the commit, exactly as the brief's vector F asks).

**Test coverage:** none. Grep for TOCTOU-shaped fixtures across `tests/`
returns nothing: no test creates an alias *between* the gate and the write.
`tests/test_spec66_approval_and_eval.py:1224-1251`
(`test_nested_alias_and_cache_invalidation`) pins that the **mtime cache
notices** an alias created *between two separate calls* — that is the cache
axis, not the intra-call TOCTOU axis.

**Assertion.** Vector A is the one vector in the brief where the *product*
is provably racy today and the *tests* say nothing about it. This is a
product finding (Debugger's column) but the test-quality finding is:
**there is no test that would fail if the TOCTOU window were widened**, and
no test that documents the window as accepted.

---

## BUG #6 — HIGH — Vector C (`.git` walk completeness): the walk skips `.git/hooks/`, `objects/`, `worktrees/`, `modules/` and no test asserts the *limit* of the walk

**File:line:** `agent/tools.py:690-691`
(`_SENSITIVE_ALIAS_DIRS = (".crabcakes", ".git", ".github")`,
`_GIT_ALIAS_SUBDIRS = ("refs",)`), `agent/tools.py:702-715`
(`_alias_scan_roots`), `agent/tools.py:743-766` (`visit`).

**Evidence.** `.git/` is walked at the **top level only**, plus `refs/`.
`.git/hooks/` is therefore **never walked** — and hooks are executed by git
on push/checkout/commit. The comment at `agent/tools.py:686-689` justifies
skipping `objects/` ("content-addressed hash names") but says nothing about
`hooks/`.

Test coverage: `tests/test_spec66_approval_and_eval.py:1092-1113`
(`test_write_via_git_alias_asks_approval`) creates `.git/config` and hard-links
it to `notes/x.txt` — that is the **top level**, which *is* walked. There is
**no test** for `.git/hooks/pre-commit`, `.git/hooks/post-checkout`,
`.git/refs/heads/` (which *is* walked — untested), `.git/objects/` (skipped —
untested), a **linked worktree's `.git` FILE** (a worktree's `.git` is a file
containing `gitdir: /path/to/.git/worktrees/x` — it is not a directory, so
`os.scandir` on it fails and it is silently skipped at `agent/tools.py:750-751`),
or `.git/modules/` (submodules — skipped, untested).

**Reverted-fix failure check.** If `_GIT_ALIAS_SUBDIRS` were changed from
`("refs",)` to `("refs", "hooks")`, **no test fails** — no test asserts the
walk's current limits. The test suite pins the *positive* (a `.git/config`
alias is caught) but not the *negative* (`.git/hooks/x` is not caught).
That asymmetry is the classic deny-list signature (see §5 of this report).

---

## BUG #7 — HIGH — Vector D (flag grammars for the unchecked allowlisted binaries): `go`, `pytest`, `mypy`, `eslint`, `ruff` have no grammar rules at all, and the env-var vectors are untested

**File:line:** `agent/enforcement.py:67-69` (`_ALLOWED_BINARIES`),
`agent/enforcement.py:284-301` (only python/node have rules).

**Evidence.** `_ALLOWED_BINARIES` contains `python3, pytest, ruff, mypy,
eslint, node, go`. The grammar rules exist **only** for `python`/`python3`
(`:284-287`) and `node` (`:288-301`). The other five binaries are accepted
with **arbitrary arguments**. Concretely accepted today (verified by reading
the code path — no branch can refuse them):
- `go run ./x.go`, `go test ./...`, `go generate ./...` (`generate` executes
  code), `go run -exec /bin/sh ./x.go` (`-exec` is a program-execution flag).
- `pytest --pyargs pkg`, `pytest -p myplugin` (a `-p` plugin is imported and
  executed), `pytest --import-mode` with a `conftest.py` in the project
  (pytest **always** executes the project's `conftest.py`).
- `mypy --config-file ./x.ini` (a config can point at plugins),
  `mypy --shadow-file`.
- `eslint --rulesdir ./rules`, `eslint --plugin ./p.js` (both load and
  execute project-supplied JS).
- `ruff --config ./x.toml` (a ruff config can name a `preview`/plugin; ruff
  plugins are not a thing but `--config` selects a project file).

**Test coverage:** none of these is refused, and **no test asserts they are
refused** — because the product does not refuse them. Grep in `tests/` for
`go run`, `go test`, `go generate`, `--plugin`, `--pyargs`, `pytest -p`,
`--rulesdir` returns **zero hits**. The only `_validate_test_command`
tests are `tests/test_spec66_approval_and_eval.py:389-476`, which cover
python/node/`npx` only.

**Env-var vectors.** `utils/env_security.py:26-35` allows only
`PATH, HOME, LANG, LC_ALL, LANGUAGES, TZ, TMPDIR, PWD`. That set is clean of
`PYTEST_ADDOPTS`, `MYPY_CONFIG`, `NODE_OPTIONS`, `GOFLAGS`, `GOPATH`,
`ESLINT_USE_FLAT_CONFIG`, `RUSTFLAGS` — **good**. The pin quality differs by
axis:
- The allowlist *contents* are pinned exactly:
  `tests/test_tools.py:719-724` asserts
  `ALLOWED_SUBPROCESS_ENV_VARS == frozenset({...})` — adding any var,
  `PYTEST_ADDOPTS` included, **fails** this test. `tests/test_tools.py:739-750`
  additionally asserts secret-shaped names are absent, and `:752-755` asserts
  the shell basics are present. Solid.
- But **no test asserts the absence of the code-executing vars by name**
  (`PYTEST_ADDOPTS`, `NODE_OPTIONS`, `MYPY_CONFIG`, `GOFLAGS`) — the
  exact-set assertion covers them incidentally, but a reader of the test
  suite would not learn that these specific vectors matter.
- The grep-path assertions (`tests/test_spec66_approval_and_eval.py:555`,
  `:562`, `:625`) compare the subprocess kwargs against
  `get_scrubbed_env()`'s own output — see BUG #12.
- `agent/enforcement.py:73-75` still defines a **second, divergent copy**
  `_ALLOWED_ENV_VARS` that is **dead code** (grep: the only hits are its own
  definition and the archived Phase-0 doc). It is not asserted equal to
  `utils.env_security.ALLOWED_SUBPROCESS_ENV_VARS` anywhere and is not
  covered by the exact-set pin. A future edit to the dead copy looks like an
  edit to the live policy. See BUG #11.

---

## BUG #8 — MEDIUM — Vector E (identity edge cases): cross-device, deep symlink chains, `..` inside symlink targets — no test

**File:line:** `agent/tools.py:805-809` (inode compare uses `st_dev`+`st_ino`),
`agent/tools.py:822-829` (symlink-target compare).

**Evidence.** Three specific edge cases the brief asks about:

1. **Cross-device.** `dest_key = (dest_st.st_dev, dest_st.st_ino)` — the
   tuple includes `st_dev`, so a bind-mount/overlayfs case where the same
   inode number appears on two devices is *handled* by the tuple (correctly).
   But there is **no test** for it, and the brief's scenario (same inode,
   different device) is not exercised.
2. **Deep symlink chains (A→B→sensitive).** The symlink-target compare at
   `agent/tools.py:822-827` uses `os.path.realpath(entry)` for an existing
   link — that resolves transitively, so a chain is caught **by the path
   compare, not by the inode compare**. The brief asks to "verify" this; the
   code path does, but **no test** covers a 2- or 3-hop chain. The existing
   alias tests (`tests/test_spec66_approval_and_eval.py:985-1026`, `:1028-1054`,
   `:1057-1090`) are all single-hop.
3. **`..` inside a symlink target.** `agent/tools.py:825-827` computes
   `os.path.normpath(os.path.join(os.path.dirname(entry), link))` for a
   **dangling** link — `normpath` collapses `..` lexically, but it does
   **not** resolve the result against the *target's* real directory when the
   target's parent is itself a symlink. A dangling link whose target is
   `../.crabcakes/x` (escaping one level) is compared **lexically**, while
   the destination `dest_norm` is a **realpath** — the two are in different
   namespaces and will not compare equal. **No test** covers a `..`-bearing
   symlink target.

**Reverted-fix failure check.** Changing the dangling-link branch
(`agent/tools.py:825-827`) from `normpath(join(dirname, link))` to
`realpath(join(dirname, link))` → **no test fails** (the dangling test at
`:1028-1054` uses a plain relative target with no `..`).

---

## BUG #9 — MEDIUM — Vector G (30s TTL cache): the enforcement config cache is never invalidated by a write, and no test pins the staleness window

**File:line:** `agent/enforcement.py:405-410` (caches + `_ENFORCEMENT_CONFIG_TTL = 30.0`),
`agent/enforcement.py:449-477` (`_load_project_enforcement_config`),
`agent/enforcement.py:413-446` (`_load_test_config`).

**Evidence.** The 30s TTL caches the *parsed* `.crabcakes/enforcement.json`.
An agent that writes a new `enforcement.json` (through an approved write) and
then triggers a post-write enforcement check will read the **stale** cached
config for up to 30 seconds. This matters because the config controls the
*test command* that gets executed.

Test coverage: `tests/test_enforcement.py:243-295` (`TestSpec61B6ParseCache`)
covers the **FEED-CARD parse cache** (`utils/feed_store.py`), **not** the
enforcement TTL cache. The enforcement caches are imported into the test
module (`tests/test_enforcement.py:37-38`) and used for cleanup, but **no
test** asserts: (a) a second load within 30s returns the cached value, (b) a
write to `enforcement.json` is not observed until the TTL expires, (c) there
is any invalidation hook on write.

---

## BUG #10 — MEDIUM — R-12's row node sections (a)–(d) are over-claimed relative to what the node asserts

**File:line:** `docs/acceptance/rows.yaml:101-107`;
`tests/test_spec66_approval_and_eval.py:198-383`.

**Evidence.** The R-12 node `test_resolved_sensitive_path_and_python_c` now
contains four sections:
- (a) tool gate, three spellings — `:225-241`
- (b) runtime gate, three spellings — `:243-266`
- (c) BUG #2 symlinked sensitive name — `:268-313`
- (d) BUG #1 alias direction — `:315-377`
- plus the eval-flag assertions — `:379-383`

The row's `claim` (`docs/acceptance/rows.yaml:102`) is a single sentence
listing all of it. Two concrete weaknesses:

1. **Section (d)'s runtime half asserts the WRONG thing's outcome.**
   `:359-360` asserts
   `from agent.runtime import _sensitive_relpath as _alias_gate; assert _alias_gate("notes/enforcement.json", alias_project) == ".crabcakes/enforcement.json"`.
   This asserts a **helper's return value**, not the gate's behavior. The
   behavioral half (`:366-377`) asserts `alias_rt_asked` is non-empty and the
   target is unchanged — that part is real. But the `_alias_gate` assertion
   duplicates the helper's contract and would pass even if the runtime gate
   stopped consulting the alias disjunct, **provided** `_sensitive_relpath`
   kept returning the alias (i.e. the R-12 mutation's *first* hunk). Since
   the R-12 mutation removes the alias disjunct **from `_sensitive_relpath`
   itself** (`tests/mutations/R-12.diff:27-29`), the mutation is caught — but
   only because of that first hunk. A **different** revert (keep
   `_sensitive_relpath` intact but stop the runtime gate from calling it)
   would be caught only by the behavioral assertion at `:376`.
2. **Section (c) asserts `result.success is False` but never asserts that
   an approval was requested** (`:288-290`: only `assert result.success is
   False`). A gate that refused the write for an unrelated reason (e.g. a
   sandbox error) would satisfy the assertion. Compare section (a) at
   `:240` (`assert asked, ...`) and section (d) at `:344`. Section (c) has
   the weaker form.

---

## BUG #11 — LOW — `_ALLOWED_ENV_VARS` in `agent/enforcement.py` is a dead, unpinned duplicate of the live env policy

**File:line:** `agent/enforcement.py:73-75`.

**Evidence.** `_ALLOWED_ENV_VARS` is defined and **never read** — grep across
the repo returns only the definition and the archived Phase-0 doc
(`docs/archive/specs/SECURITY-REMEDIATION-PHASE-0-INSTRUCTIONS.md:83-95`).
The live policy is `utils/env_security.ALLOWED_SUBPROCESS_ENV_VARS`
(`utils/env_security.py:26-35`), which `tests/test_tools.py:719-724` pins
exactly.

The dead copy is a **drift hazard**: it still lists `PWD` and would look like
the policy to a future reader. `tests/test_tools.py:724` pins the *live*
set; nothing pins the *dead* set, and nothing asserts the two are equal or
that the dead one is unused. The correct fix is deletion, not a pin.

---

## BUG #12 — LOW — The R-14 node's fallback-site assertion is a tautology against `get_scrubbed_env()`

**File:line:** `tests/test_spec66_approval_and_eval.py:552-562`.

**Evidence.**
```python
monkeypatch.setattr("agent.tools.subprocess.run", fake_run)
_run_grep("needle", str(tmp_path))
assert calls[0][1].get("env") == get_scrubbed_env()
```
The assertion compares the kwargs the *product* passed to the *function the
product defines* (`utils/env_security.get_scrubbed_env`). It pins that the
site passes **the scrubbed env** rather than `os.environ` — which is the
correct thing to pin. However it does **not** pin that the env is actually
scrubbed *of the specific vectors* (see BUG #7). The same tautology shape
applies at `:555`, `:562`, `:625`. The stronger, non-tautological pin is
`tests/test_tools.py:719-733` (asserts a secret var is absent from the
returned dict) — but there is no such assertion on the **grep** path.

**Reverted-fix failure check.** Replacing `env=get_scrubbed_env()` with
`env=None` at `agent/tools.py:1429`/`:1444` → the assertion at `:555` fails
(`calls[0][1].get("env")` is `None` ≠ the dict) → **PINNED**. Good.

---

## §4 — Acceptance-row gap table (the brief's question 4)

| Load-bearing behavior | Pinned by a named test? | Acceptance row? | Mutation file? |
|---|---|---|---|
| R-12 resolved-path gate, both gates | Yes (`:84`, `:136`, `:198`) | **R-12** | `R-12.diff` |
| R-12 **BUG #2 lexical disjunct** | Yes (`:730`, `:783`, `:818`, `:910`) | claimed by R-12 | **MISSING** — not in `R-12.diff` |
| R-12 **BUG #1 alias disjunct** | Yes (`:985`, `:1028`, `:1057`, `:1144`, `:1201`) | claimed by R-12 | `R-12.diff` ✔ |
| **Alias cache** (`_ALIAS_ENTRY_CACHE` mtime signature) | Yes (`:1224`, `:1254`) | **NO ROW** | **MISSING** |
| **Cluster rule** (BUG #3) | Yes (`:491`, `:400`, `:383`) | **NO ROW** | **MISSING** |
| **Node positional rule** (BUG #4) | Yes (`:512`, `:436-437`) | **NO ROW** | **MISSING** |
| R-13 fence | Yes (`test_spec66_fence.py:182`) | R-13 | `R-13.diff` ✔ |
| R-14 scrubbed env + fetch switch | Yes (`:529`) | R-14 | `R-14.diff` ✔ (one hunk) |
| R-17 accept path normalization | **Only by a source-text grep** (`:128`) | R-17 | `R-17.diff` ✔ |
| R-17 "index has other staged files" refusal | **NO** | claimed by R-17 | **MISSING** (no behavioral test) |
| BUG #5 silent accept exits → `accept_error` | Yes (`test_spec66_accept.py:227`, `:241`) | **NO ROW** | **MISSING** |
| BUG #6 dead-feature docs | Yes (`test_spec66_package.py:122`) | R-66S | `R-66S.diff` ✔ |
| `.git/hooks/` walk limit | **NO** | **NO ROW** | — |
| `go`/`pytest`/`mypy`/`eslint` grammar | **NO** | **NO ROW** | — |
| Enforcement TTL cache staleness | **NO** | **NO ROW** | — |

Note: **BUG #5 (silent accept exits) has no acceptance row at all** even
though it is one of the seven closed reports — `tests/test_spec66_accept.py:227`
and `:241` are real behavioral tests, but the gate does not mechanically
prove them.

---

## §5 — The design question (the brief's last line), from the test side

**Question:** is a deny-list of orientations testable at all, or does it need
a property-based/generative test?

**Answer: from the test side, a deny-list of orientations is NOT soundly
testable by examples. It needs a generative/property test.**

Evidence from this audit:

1. **The surface is a deny-list by construction.** `is_sensitive_path`
   (`agent/tools.py:360-409`) enumerates prefix/glob patterns; the alias walk
   (`agent/tools.py:690-691`) enumerates *directories*; `_validate_test_command`
   (`agent/enforcement.py:284-301`) enumerates *flag spellings*. Every fix in
   the seven rounds added one more entry to a list of orientations.
2. **Examples do not cover a list.** The seven rounds produced seven
   orientations and (by the brief's own table) each external report found a
   NEW orientation of the same gap. The current suite has 20+ fixtures for
   the path gate; the brief's own seven rounds prove the list was incomplete
   each time.
3. **The suite is 100% example-based.** Grep confirms **no property-based
   framework** is present: `hypothesis` has **zero hits** outside
   `docs/proposals/`; `pyproject.toml:36-38` lists only `pytest>=8.0` in
   extras. `scripts/acceptance.py:21` states the gate runs on
   "stdlib + pytest only".
4. **A property-based test IS feasible for this surface, and is the only
   formulation that would have caught the seven rounds.** The invariant to
   assert is *stateful and simple*:

   > For any project tree T containing a sensitive file S, and any path
   > spelling P of any file F in T: if F and S are the same inode, or F is
   > reachable from S by a symlink chain, then a write to P must request
   > approval naming S.

   A generator over (random path spellings × random alias topologies) with
   that invariant catches *new* orientations without a new fixture per
   orientation. The same shape works for the flag grammar: generate token
   spellings from an alphabet of short/long flags and assert the invariant
   "no spelling of an eval/exec flag is accepted".

5. **Constraint.** Because the acceptance gate is stdlib+pytest only
   (`scripts/acceptance.py:21`), the generator should be a **hand-rolled
   seeded `random` generator** with a fixed seed (deterministic, reproducible,
   no new dependency). `hypothesis` would also be acceptable as a dev-only
   extra, but it would need to be excluded from the gate's environment — a
   new dependency in a security-gate test path is itself a supply-chain
   surface.

6. **Recommendation to the Supervisor (design-level, not a test fix):**
   the deny-list has already produced seven rounds of one-orientation-at-a-time
   patching. Two candidate design changes make the surface *finite*:
   - **(a) A mandatory-approval-list model** — invert the rule: nothing is
     sensitive by default; instead, every path **inside** the sensitive
     directories (`.crabcakes/`, `.git/`, `.github/`) and every path matching
     a small positive set requires approval. This makes the sensitive set
     *closed under the filesystem*, so the alias walk's job shrinks to
     "is the destination inside a sensitive directory", which is a single
     containment check rather than a name-identity search.
   - **(b) `O_NOFOLLOW` + exclusive-directory opens** — open the destination
     with `O_NOFOLLOW` and open the sensitive directories with
     `O_DIRECTORY|O_NOFOLLOW` to *prove* they are directories, then compare
     device+inode of the opened destination against the opened sensitive
     entries. This collapses the *name* problem into an *inode* problem and
     removes the TOCTOU window (vector A) as well.
   Either change makes the test surface finite — and a finite surface is
   what an example-based suite can soundly pin.

---

## §6 — Vector coverage summary (the brief's question 3)

| Vector | Any test? | Notes |
|---|---|---|
| **A. TOCTOU** | **NONE** | Scan→`open()` gap (`tools.py:856`→`:900`), edit RMW, accept diff→commit (`review.py:98`→`:146`). Unpinned, unguarded, unacknowledged. |
| **B. Rename/move** | **NONE** | No tool moves/renames inside the project (`write_file`/`edit_file` are the only mutators; `os.replace` sites are `agent/persistence.py:132` and `agent/config.py:423`, both outside the project tree). **Vector closes** — but no test asserts it, so a future rename tool would reopen it silently. |
| **C. `.git` walk completeness** | **NONE** for hooks/objects/worktrees/modules | Only `.git/config` top-level is pinned (`test_spec66_approval_and_eval.py:1092`). `refs/` walked but untested. Worktree `.git` FILE silently skipped (`tools.py:750`). |
| **D. Flag grammars (go/pytest/mypy/eslint/ruff)** | **NONE** | No rules exist for these five binaries; no tests. |
| **D. Env-var vectors** | **PARTIAL** | Allowlist pinned exactly (`tests/test_tools.py:724`); the *code-executing* vars are absent by construction but not asserted as such; `agent/enforcement.py:73-75` dead duplicate. |
| **E. Identity edge cases** | **NONE** | Cross-device, deep chains, `..` in symlink targets untested. |
| **F. Accept path** | **PARTIAL** | Normalization pinned only by source-text grep (BUG #4); `card.file_path` semantics untested; "others" refusal untested; commit-message injection from filenames untested (`review.py:138-144` builds the message from `d.a_path`). |
| **G. Enforcement loader / TTL** | **NONE** for staleness | Load-time content is the validator's job (per brief); the 30s TTL staleness window is untested. |

---

## §7 — Priority

| # | Sev | One-line |
|---|---|---|
| BUG #1 | CRITICAL | R-12's mutation covers only BUG #1; BUG #2's product change has no mutation file, so the row over-claims its own coverage. |
| BUG #3 | HIGH | The four-spelling accept matrix re-implements the normalization in the test body — it cannot fail when the product's normalization is deleted. The "index has other staged files" refusal has **no test at all**. |
| BUG #2 | HIGH | BUG #3 (cluster) and BUG #4 (node positional) — two external-report fixes — have no acceptance row and no mutation file. |
| BUG #5 | HIGH | Vector A (TOCTOU) has zero tests; the scan→`open()` window is real today. |
| BUG #6 | HIGH | Vector C: `.git/hooks/` is never walked and the walk's *limits* are unpinned — a test asserting the current limits would fail the moment someone widens them. |
| BUG #7 | HIGH | Vector D: `go`/`pytest`/`mypy`/`eslint`/`ruff` have no grammar rules and no tests; the grep-path env pins compare the product against the product's own function (BUG #12), and the env allowlist pin misses the code-executing-var axis by name. |
| BUG #4 | MEDIUM | R-17's node is a source-text grep, not a behavioral pin. |
| BUG #8 | MEDIUM | Vector E: cross-device, deep chains, `..` in symlink targets — three untested edges of the inode/target compare. |
| BUG #9 | MEDIUM | Vector G: the 30s TTL cache is never invalidated on write and never tested. |
| BUG #10 | MEDIUM | R-12 section (c) asserts only `success is False`, not that approval was requested. |
| BUG #11 | LOW | `_ALLOWED_ENV_VARS` is a dead, unpinned duplicate of the live env policy. |
| BUG #12 | LOW | R-14's env assertions compare the product against the product's own function. |

**Read-only confirmation:** no file outside this document was created,
modified, or deleted in this audit.
