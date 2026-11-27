# CONSOLIDATED Fix Round — Re-audit

**Date:** 2026-11-23
**Auditor:** Debugger
**Baseline:** `main` @ `75d8e677` + the **uncommitted working tree** (the fix round, now +2867/-246 across 8 files)
**Subject of this re-audit:** my own report `docs/audits/2026-11-19-CONSOLIDATED-FIXROUND-ADVERSARIAL-AUDIT.md`
**Probes:** `.crabcakes/scratch/reaudit/` (13 probes + `reaudit_plugin.py`)
**Read-only on source.** The working tree is byte-identical to the dispatch state; nothing was stashed, checked out, or modified.

---

## 0. Verdict

**The fix round closed four of the six claims made to me, and closed them properly.** C-B, C-C,
M-A and M-E are now closed by execution, and every one of them has a real regression test that
**reddens when the fix is reverted** (probe 10). The refusal-is-a-visible-FAILED contract is
real too. That is a materially better round than the one I audited before.

**But C-A is closed only in its weaker half.** The claim was *"C-A test-file writes are gated at
both gates for every project."* That part is **true** — the write gate is unconditional and fires
at both gates. But **C-A was never only about writes.** The finding was that the *enforcement
tier imports and executes project-supplied code*. Gating the write stops the **agent** from
planting the payload; it does nothing about the **clone** that already shipped it. My original
C-A reproduction — the clone-shipped variant — **still fires**, unchanged, against the current
tree.

And the C-B/C-C work, while excellent, **narrowed the vector without closing the class**: the
per-binary grammar now checks **flags** and **python/node script positionals**, but a **pytest
positional** — a file path pytest will import — is still admitted through all three
project-supplied command fields, with a **clone-shipped** payload and **zero approval cards**.

| Severity | Count |
|---|---|
| **CRITICAL** | **2** |
| **MAJOR** | **3** |
| **MEDIUM** | **3** |
| **LOW / NOTE** | **4** |
| **TOTAL FINDINGS** | **12** |
| Refuted vectors (documented with commands) | 31 |

**Per-claim verdicts:**

| Claim | Verdict |
|---|---|
| C-A: test-file writes gated at both gates, every project | **PARTIALLY CLOSED** — the write gate is real and unconditional; the **clone-shipped** execution chain is **still open** |
| C-B: `extra_args` validated, flag rules key on the `-m` module | **CLOSED** |
| C-C: python positional script paths refused by PM ruling | **CLOSED** |
| M-A: the alias scan resolves a linked worktree gitdir file | **CLOSED** |
| M-E: `full_suite_command` is live on its own | **CLOSED** |
| Every refused command field is a visible FAILED tests check | **CLOSED** |

---

## 1. The claims, verified by execution

### 1.1 C-B — `extra_args` validated, flag rules keyed on the `-m` module — **CLOSED**

Both halves are real, and the module-keying works exactly as claimed.

**`extra_args` is now validated** (`agent/enforcement.py:474-495` `_validate_test_extra_args`,
called at `:1876`). Re-running my original C-B probe **unchanged**
(`.crabcakes/scratch/fixround/probe_4_p_plugin_chain.py`):

```
  P1 extra_args='-p evilplugin' (python3 -m pytest base): marker=False
     tiers=[('tests', False, "REFUSED: test.extra_args — '-p evilplugin' (pytest: plugin/config flag refused (-p/--plugi")]
[closed] P1 extra_args='-p evilplugin' (python3 -m pytest base)
  P2 full_suite_command='python3 -m pytest -p evilplugin': marker=False
     tiers=[('tests', False, "REFUSED: test.full_suite_command — ... (pytest: plugin/confi")]
[closed] P2 full_suite_command='python3 -m pytest -p evilplugin'
  P3 command='python3 -m pytest -p evilplugin': marker=False
     tiers=[('tests', False, "REFUSED: test.command — ... (pytest: plugin/config flag refu")]
[closed] P3 command='python3 -m pytest -p evilplugin'
```

And the **clone-shipped** form (probe 5, part A) — the agent authors nothing:

```
  A1 clone SHIPS enforcement.json with extra_args='-p evilplugin'
      agent write asked=0 success=True
      marker=False tiers=[('syntax', True, ...), ('tests', False, "REFUSED: test.extra_args — ...")]
[closed] A1
  A2 clone SHIPS enforcement.json with command='python3 -m pytest -p evilplugin'
      marker=False tiers=[('syntax', True, ...), ('tests', False, "REFUSED: test.command — ...")]
[closed] A2
```

**The module-keying is the right fix.** `_module_base` (`agent/enforcement.py:335-354`) resolves
`python3 -m pytest` → base `pytest`, so the tier's **own** argv shape now hits the pytest rules.
Verified:

```
  _validate_test_command('python3 -m pytest -p evilplugin') = False   (was True)
  _validate_test_command('pytest -p evilplugin')            = False
```

and the resolution is robust:

```
  python3 -q -m pytest  -> base='pytest'   (a flag BEFORE -m does not break it)
  python3 -m PYTEST     -> base='pytest'   (case-insensitive)
  python -m pytest      -> base='pytest'
  py -m pytest          -> base='pytest'
```

**Regression test is real.** Reverting `_module_base` to the identity makes **29 tests fail**;
reverting `_validate_test_extra_args` makes **3 fail** (probe 10).

---

### 1.2 C-C — python positional script paths refused by PM ruling — **CLOSED**

`agent/enforcement.py:357-396` (`_python_positional_refusal`), applied at `:430-433`.

```
  C1  python3 script.py              -> "python3: script positional refused ('script.py') — use `-m <allowlisted module>`"
  C4  python3 -m unknownmodule       -> "python3: script positional refused ('unknownmodule') — use `-m <allowlisted module>`"
  C5  python3 -W error script.py     -> "python3: script positional refused ('error') — use `-m <allowlisted module>`"
  C7  python3 -ic -m pytest          -> "python3: eval flag (-c) refused"
  C8  python3 -m pytest -p evilplugin-> "pytest: plugin/config flag refused (-p/--plugin/-c/--config)"
  C9  python -m pytest -p evil       -> "pytest: plugin/config flag refused (-p/--plugin/-c/--config)"
  C10 python3.11 -m pytest -p evil   -> "pytest: plugin/config flag refused (-p/--plugin/-c/--config)"
  C14 python3 -c 'x'                 -> "python3: eval flag (-c) refused"
```

The two documented decisions both hold: the interpreter's **own** flags are checked *before*
module resolution (so `-ic -m pytest` cannot launder), and a **value-taking flag's value is not
skipped** (so `python3 -W error script.py` is refused rather than modelled against Python's flag
arity).

**The surviving spellings are the tier's own** — `python3 -m pytest` and `python3 -m pytest -q`
are allowed, so the tier still works.

**The syntax tier is unaffected**, as documented: `_check_syntax` (`agent/enforcement.py:1054`)
builds its argv from `SYNTAX_CHECKERS` and never calls `argv_refusal`. (`argv_refusal` *would*
refuse `python3 -m py_compile` — "script positional refused ('py_compile')" — but the syntax tier
is not routed through it. Probe 13 confirms the tier's other argv shapes are all admitted:
the tests argv, the tests argv + a target, the lint argv, the go argv, the eslint argv.)

**Regression test is real.** Reverting `_python_positional_refusal` makes **6 tests fail**,
including a dedicated revert-guard (`test_python_positional_rule_reddens_if_reverted`).

---

### 1.3 M-A — the alias scan resolves a linked worktree gitdir file — **CLOSED**

This is the fix I was most worried about, because the fix round's own note said *"The returned
paths are used for the SCAN only — nothing here widens what a write may target."* — and that is
exactly right.

**The chain resolves correctly** (`agent/tools.py:898-938` `_worktree_git_dirs`):

```
  W1 _worktree_git_dirs(worktree) = ['/tmp/.../.git/worktrees/wt', '/tmp/.../.git']
```

The **worktree** git dir **and** the **common** dir (where `hooks/` actually lives) are both
resolved — the second hop comes from the `commondir` file, which is git's own chain.

**The scan now sees the shared hooks** (probe 13 of the previous round, re-run unchanged):

```
  T3 main repo entries  = 45
  T3 worktree entries   = 52      (was 0)
[closed] T3 the linked worktree's alias walk yields .git entries
  T1 linked worktree: shared .git/hooks alias: asked=1 success=False runtime_gate=True rel='../.git/hooks/pre-commit' landed=False
[closed] T1 linked worktree: shared .git/hooks alias
  T2 linked worktree: .git/worktrees/<n>/config.worktree alias: asked=1 landed=False
[closed] T2
```

**The pointer is bounded and cannot be steered to an arbitrary directory** (probe 8, W2/W3):

```
  P1 pointer -> /usr : dirs=[]      (/usr has no HEAD -> refused)
  P3 scan entries=0 in 0.1 ms
[closed] P3 a pointer to a huge directory does not blow up the scan
  W3 pointer loop -> [] (len=0)
[closed] W3 a gitdir pointer loop is bounded
```

`_looks_like_git_dir` (`agent/tools.py:878-895`) requires `HEAD` or `commondir`, the chain is
bounded at `_GITDIR_CHAIN_LIMIT = 4`, and a repeated realpath breaks the loop.

**The one widened-scan behaviour is safe.** A pointer to a dir that *does* have a `HEAD` is
followed, and an alias inside it is then reported as sensitive:

```
  P2 sensitive_reason -> '../reaudit_pointer_dir/sensitive.cfg'
```

That is **over-refusal** (the write asks), which is the documented safe direction — and the
sandbox is independent of the scan:

```
  P5 write outside the sandbox: success=False err='Path escapes project sandbox: ../escape.txt'
[closed] P5 the sandbox still refuses a write outside the project
```

**Regression test is real.** Reverting `_worktree_git_dirs` makes **3 tests fail**.

---

### 1.4 M-E — `full_suite_command` is live on its own — **CLOSED**

`agent/enforcement.py:1788` — the branch is now entered by either field:

```python
if test_config.command or (test_config.run_full_suite and test_config.full_suite_command):
```

Verified by execution (probe 3, M1):

```
  M1 full_suite_command alone: tiers=[('syntax', True, ...), ('tests', False,
      "REFUSED: test binary — binary not found via scrubbed PATH: 'pytest' — PATH shadowing refused")]
[closed] M1 full_suite_command is live on its own (a tests check ran)
```

The tests check **ran** — it reached the resolved-binary gate and was refused there (no `pytest`
on the scrubbed PATH in that probe). That is the liveness proof: the field is now read, the
command is validated, the binary is resolved, and only then is it refused.

**Regression test is real.** Reverting the token allowlist makes **16 tests fail**, including
`test_full_suite_command_runs_with_no_command_field`.

---

### 1.5 The refusal-is-a-visible-FAILED contract — **CLOSED**

`agent/enforcement.py:540-567` (`_tests_refused_check`), used at all three refusal sites
(`:1803`, `:1833`, `:1845`) plus the `extra_args` site (`:1880`).

All three surfaces produce **exactly one** `tests` check, `passed=False`, with a `REFUSED:`
detail naming the field and the rule — and **no subprocess runs** (probe 3):

```
  V1 test.command refused: n_tests_checks=1 passed=[False]
     detail="REFUSED: test.command — 'python3 script.py' (python3: script positional refused ('script.py') — use `-m <allowlisted module>`)"
[closed] V1 test.command refused -> a VISIBLE FAILED tests check
[closed] V1 test.command refused -> no subprocess ran (no payload executed)

  V2 test.full_suite_command refused: n_tests_checks=1 passed=[False]
     detail="REFUSED: test.full_suite_command — 'python3 script.py' (...)"
[closed] V2 ... (both)

  V3 test.extra_args refused: n_tests_checks=1 passed=[False]
     detail="REFUSED: test.extra_args — '-p evilplugin' (pytest: plugin/config flag refused (-p/--plugin/-c/--config))"
[closed] V3 ... (both)
```

This is the correct fix for the false-PASS shape I flagged: a refusal is **never** a silent skip
(which would read as "nothing to run" and let the write through), and **never** a pass.

**Regression test is real.** Reverting `_tests_refused_check` to return `None` makes **6 tests
fail**.

---

### 1.6 C-A — test-file writes gated at both gates, every project — **PARTIALLY CLOSED**

**The write gate is real and unconditional.**

`agent/tools.py:429` (`_TEST_FILE_PATTERNS = ("test_*.py", "*_test.py")`),
`:432-445` (`test_file_write_gate_applies` — always `True`, the PM ruling),
`:448-459` (`is_test_file_name`), applied at `:516`.

```
  X1 test_file_write_gate_applies() = True (unconditional)
  X1 test_file_write_gate_applies('/any/project') = True
[closed] X1 the C-A gate scope is unconditional (PM ruling)

  G5 write_file asked=1 success=False runtime_gate='tests/test_feature.py'
[closed] G5 the runtime gate fires for a test file
  G6 edit_file  asked=1 success=False
[closed] G6 edit_file of a test file asks for approval

  X2 the APP's own tests/test_foo.py -> runtime gate = 'tests/test_foo.py'
[closed] X2 the app's own test files are gated (PM ruling: every project)

  X4 worktree test-file write: asked=1 success=False runtime='tests/test_feature.py'
[closed] X4 a test-file write in a linked worktree is gated
```

The write gate fires at the tool gate **and** the runtime gate, in the app's tree and in a
worktree, for `write_file` and `edit_file`.

**The agent-written variant of C-A is closed** (probe 15, re-run unchanged):

```
  T2 agent-written poisoned test file
      step1 (test file write) asked=1 success=False
            err='write_file blocked: tests/test_feature.py is a sensitive path ...'
      step2 (source write) asked=1 success=True
      marker=False tiers=[('syntax', True, 'Syntax check passed for feature.py')]
[closed] T2 agent writes a poisoned test file (no card) -> the tier runs it
```

**Regression test is real.** Reverting `is_test_file_name` makes **7 tests fail**.

**BUT the clone-shipped variant is still open** — see Finding C-A-1 below.

---

## 2. CRITICAL findings

### C-A-1 — The clone-shipped poisoned test file is still imported and executed by the tier

**Severity: CRITICAL.** The clone ships the payload; the agent authors **nothing**; **no
approval card exists anywhere in the chain**; the tier reports **PASSED**.

**Root cause.** The C-A fix is a **write** gate. `agent/tools.py:420-424` says so plainly:

> it cannot isolate the CODE layer, because the test file IS the code under test. So the WRITE
> is gated instead: a write/edit of a test file asks the PM, exactly like the toolchain-config
> names above. What the tier READS is unchanged — this is a gate on the write, not a change to
> collection.

And `agent/enforcement.py:1894-1895` still points pytest at the project's own test file:

```python
abs_test = os.path.join(project_path, related_test)
argv = list(argv) + [abs_test] + test_config.extra_args.split() + ["--tb=short"]
```

A clone-shipped `tests/test_feature.py` is never *written* by the agent, so the write gate never
fires — and the tier imports it anyway.

**Reproduction** (probe 1, `probe_1_ca_test_file.py`):

```
  A1 clone ships tests/test_feature.py
      agent write asked=0 success=True
      marker=True tiers=[('syntax', True, 'Syntax check passed for feature.py'),
                         ('tests', True, 'tests/test_feature.py: passed')]
[*** OPEN ***] A1 clone ships tests/test_feature.py -> the tier executes the clone-shipped test file

  A2 clone ships root test_feature.py
      marker=True tiers=[('syntax', True, ...), ('tests', True, 'test_feature.py: passed')]
[*** OPEN ***] A2

  A3 clone ships tests/feature_test.py
      marker=True tiers=[('syntax', True, ...), ('tests', True, 'tests/feature_test.py: passed')]
[*** OPEN ***] A3
```

The payload is three lines:

```python
# tests/test_feature.py  — SHIPPED BY THE CLONE
import pathlib
pathlib.Path('/tmp/.../MARKER_RAN').write_text('payload ran')

def test_x():
    assert True
```

**This is exactly my original C-A reproduction, unchanged.** The fix round moved the gate from
"nowhere" to "the write" — and the clone-shipped chain never involved a write.

**Scope note (honest):** this is **not a regression** — it is the same exposure my first report
documented, and the fix round did not make it worse. It is a finding because **the C-A claim as
presented to me reads as "the C-A chain is closed"**, and it is not.

**How it would close.** The gate is on the wrong side of the operation. Either:
- the tier refuses to run pytest against a test file it has not seen the PM approve
  (a per-project "approved test files" set), or
- the tier runs pytest with `--import-mode=importlib` **and** a collect-only step that does not
  import, or
- the tier is documented as a QUALITY control that is **not** allowed to execute untrusted
  repository code at all — which is the Fix 12 posture taken to its logical conclusion. **This
  last one is the honest one**: Fix 12 already ruled that the tiers are "a QUALITY control, not
  a security control against a hostile repository". Under that ruling, a tier that **imports and
  executes** a cloned repository's file is not a quality control at all — it is a remote code
  execution primitive wearing a quality-control label.

---

### C-B-1 — A pytest **positional** path is admitted through all three command fields; the payload file is not gated

**Severity: CRITICAL.** The C-B/C-C work closed the **flag** half and the **python/node
positional** half. **pytest's positional was not covered**, and pytest **imports the file it is
pointed at** — exactly as it imports the test file.

**Root cause — the grammar checks flags and python/node positionals, but not a pytest
positional.**

`agent/enforcement.py:435-448` bans a node positional. `:426-433` bans a python positional.
But the **pytest** branch (`:454-455`) only checks flags:

```python
if base == "pytest" and _has_forbidden_flag(flags, ("-p", "--plugin", "-c", "--config")):
    return f"{base}: plugin/config flag refused (-p/--plugin/-c/--config)"
```

`pytest evil.py` names a file to import. `argv_refusal(["pytest", "evil.py"])` → **`None`**
(admitted).

And the payload file itself is **not gated**: `is_sensitive_path("evil.py")` → `False`
(probe 5, N3-j). The C-A gate covers `test_*.py` / `*_test.py` only.

**Reproduction** (probe 4, N3; probe 5; probe 6):

```
  N3 extra_args='evil.py': marker=True
     tiers=[('syntax', True, ...), ('tests', True, 'tests/test_feature.py: passed')]
[*** OPEN ***] N3 extra_args='evil.py' -> the payload does NOT run
```

**Through all three project-supplied fields:**

```
  N3-e extra_args='evil.py' (relative):                        marker=True  OPEN
  N3-f command='python3 -m pytest {test_file} evil.py':         marker=True  OPEN
  N3-g full_suite_command='python3 -m pytest evil.py':          marker=True  OPEN
  N3-d extra_args='/tmp/reaudit_evil.py' (ABSOLUTE, outside):   marker=True  OPEN
  Q5  command='python3 -m pytest evil.py':                      marker=True  OPEN
```

**The full clone-shipped chain** (probe 6, Q2) — the agent writes one ordinary source file:

```
  Q2 clone-shipped extra_args='evil.py'
      agent write asked=0 success=True
      marker=True tiers=[('syntax', True, 'Syntax check passed for feature.py'),
                         ('tests', True, 'tests/test_feature.py: passed')]
[*** OPEN ***] Q2 the clone-shipped positional chain executes the payload
```

The clone ships three files:

```
  evil.py                       <- the payload (imported by pytest)
  pytest.ini
  .crabcakes/enforcement.json   <- {"test": {"extra_args": "evil.py", "run_full_suite": true}}
```

The agent's only action is writing `feature.py`. **Zero approval cards. The tier reports
PASSED.**

**The mechanism** (probe 5, N3-a) — the tier's argv:

```
  argv AFTER isolation = ['python3', '-m', 'pytest',
                          '-c', '/home/mushy/.config/develcakes/tier/pytest.ini',
                          '--confcutdir', '/home/mushy/.config/develcakes/tier',
                          '--noconftest', 'evil.py', '--tb=short']
```

`--noconftest` does not stop pytest importing the file it is **told** to collect.

**Is this a regression?** **No** — HEAD also admitted it (probe 6, Q1: `HEAD: pytest evil.py ->
True`, `python3 -m pytest evil.py -> True`). But the fix round's C-B work **closed the flag half
of this surface** and **left the positional half as the only remaining hole in it** — so the
surface is now *narrower but still open*, and the remaining hole is the one that executes code.

**Reachability limits (honest).** The Coder's shell refuses a pytest positional
(`Q3: _coder_shell_allowed('pytest tests/evil.py') -> REFUSED`), so the agent cannot reach this
through `exec_command`. It is reached through the **enforcement tier's own argv**, on the
project's own `enforcement.json`, which is **project-supplied by definition**.

**How it would close.** Extend the pytest branch of `argv_refusal` with the same positional rule
the python and node branches have:

```python
if base == "pytest":
    if _has_forbidden_flag(flags, ("-p", "--plugin", "-c", "--config")):
        return f"{base}: plugin/config flag refused (-p/--plugin/-c/--config)"
    # a positional is a file pytest will IMPORT
    positional = [tok for tok in flags if not tok.startswith("-")]
    if positional:
        return f"{base}: import target refused ({positional[0]!r})"
```

**with one carve-out**: the tier's **own** argv appends the related test path as a positional
(`agent/enforcement.py:1895`). So the rule must be applied to the **project-supplied fragment
only**, exactly as `_validate_test_extra_args` already does for `extra_args` — the base argv
(`["python3", "-m", "pytest"]`) is app-chosen, the appended target is project-chosen. If the
rule is applied to the whole argv, the tier stops working; if it is applied to the fragment, it
closes the vector.

**Note the interaction with C-A-1.** If the tier refuses to import any positional at all (rather
than only the project-supplied ones), the C-A-1 clone-shipped chain closes too — because the
related test path **is** a positional. That is the cheaper fix: one rule closes both findings.

---

## 3. MAJOR findings

### M-1 — The `__tests__/{module}.py` convention is a gate bypass

**Severity: MAJOR.** The C-A gate matches `test_*.py` and `*_test.py`. But
`_find_related_test` (`agent/enforcement.py:1264-1274`) has **four** candidates, and the fourth
is named `{module}.py`:

```python
candidates = [
    os.path.join(project_path, test_dir, pattern),              # test_{module}.py
    os.path.join(project_path, test_dir, f"{basename}_test.py"),  # {module}_test.py
    os.path.join(project_path, src_dir, pattern),                # <src_dir>/test_{module}.py
    os.path.join(project_path, src_dir, "__tests__", f"{basename}.py"),  # <src_dir>/__tests__/{module}.py
]
```

The fourth — `src/__tests__/feature.py` — matches **neither** gate pattern. It is a `.py` file
whose name is the module name.

**Reproduction** (probe 12):

```
  _find_related_test('src/feature.py') = 'src/__tests__/feature.py'
  is_sensitive_path('src/__tests__/feature.py') = False
  marker=True tiers=[('syntax', True, 'Syntax check passed for src/feature.py'),
                     ('tests', True, 'src/__tests__/feature.py: passed')]
[*** OPEN ***] the __tests__/{module}.py convention is not a gate bypass
```

The clone ships `src/__tests__/feature.py`; the agent writes `src/feature.py`; the tier imports
the payload; the tier reports PASSED. **Zero approval cards.**

(Probe 11 hit the same thing and reported it as closed — but only because
`tests/test_feature.py` matched *first* and shadowed the `__tests__` candidate. Probe 12 forces
candidate 4 by removing the earlier candidates. The order-dependence is itself a property worth
noting: the gate's coverage depends on which candidate happens to match first.)

**How it would close.** Gate on the **convention**, not on the name: the gate should ask *"is
this path one the tier could point pytest at?"* — which is exactly
`_find_related_test`'s candidate set — rather than *"does this basename look like a test file?"*.
The two are not the same question, and the gate picked the wrong one.

---

### M-2 — A nested `.gitattributes` / `.gitignore` / `.git/config` alias is still ungated

**Severity: MAJOR.** **Unchanged from my previous report** (M-B). Fix 1 closed the **root**
`.gitattributes`/`.gitignore` alias direction (`_ROOT_GIT_CONFIG_FILES`,
`agent/tools.py:838`); the **subdirectory** forms are still open.

**Reproduction** (probe 8, W4):

```
  W4a subdir .gitattributes alias: asked=0 landed=True
[*** OPEN ***] W4a subdir .gitattributes alias is gated
  W4b subdir .gitignore alias: asked=0 landed=True
[*** OPEN ***] W4b subdir .gitignore alias is gated
  W4c subdir .git/config alias: asked=0 landed=True
[*** OPEN ***] W4c subdir .git/config alias is gated
```

And git **does** execute a subdirectory `.gitattributes` — proved by execution in my previous
round (`S0 git add with a SUBDIR .gitattributes filter: filter_ran=True rc=0`).

**How it would close.** Change `("exact", ".gitattributes")` / `("exact", ".gitignore")` to
**basename globs** (`("glob", ".gitattributes")`, `("glob", ".gitignore")`), which match at any
depth by construction — the same mechanism Fix 2 used for the toolchain files. Or walk the
project root for git-executed config.

---

### M-3 — The `objects/` exclusion's justification is still false, and the object store is still corruptible with no approval

**Severity: MAJOR.** **Unchanged from my previous report** (M-D). The hash-name argument at
`agent/tools.py:758-765` is still asserted and still false: a hard link's **plain** name is
arbitrary, so the alias direction **can** be constructed from the working tree.

**Reproduction** (probe 8, W5):

```
  W5 object corrupted=True fsck_rc=3 'missing blob f70f10e4db19068f79bc19068f79bc43844b49f3eece45c4e8'
[*** OPEN ***] W5 the objects/ corruption vector is closed
[*** OPEN ***] W5 the objects/ hash-name justification was corrected
```

The fix round **did not touch this**, and the new test `test_git_objects_stays_excluded` still
**pins the exclusion** — i.e. it pins the false claim, making it harder to correct.

**Mitigating fact (unchanged):** the agent has no hard-link primitive — the Coder shell refuses
`ln`, `cp`, `chmod`, `git hash-object` (my previous report, R14). So the vector is not directly
reachable today. The finding is that the **stated justification is false** and the new test
**cements it**.

---

## 4. MEDIUM findings

### D-1 — The `__tests__` finding is the same class as M-1's naming-pattern sibling: the gate matches names, the tier follows conventions

**Severity: MEDIUM.** M-1 is the concrete instance. The general form is worth recording because
it will recur: the C-A gate asks *"does this basename look like a test file?"*, but the tier
asks *"which file does the project's convention point at?"*. Those are different questions, and
the project answers the second one.

Two project-supplied knobs decide the second question, and **neither** is in the gate's model:

| Knob | Field | Effect on what the tier imports |
|---|---|---|
| `test.naming_pattern` | `TestConfig.naming_pattern` | The file name the tier points pytest at |
| `test.test_dir` | `TestConfig.test_dir` | The directory it looks in |

**Reproduction** (probe 7, G11) — a project sets `naming_pattern: "check_{module}.py"`:

```
  G11 naming_pattern='check_{module}.py'
      the payload file is_sensitive_path = False
      marker=True tiers=[('syntax', True, ...), ('tests', True, 'tests/check_feature.py: passed')]
[*** OPEN ***] G11 a naming_pattern the gate does not match is refused
```

The clone ships `tests/check_feature.py`; the tier imports it; the tier reports PASSED; the
agent wrote nothing sensitive.

**This is a clone-shipped CRITICAL with one more spelling.** It is filed as MEDIUM here only
because it is the same root cause as M-1 and the fix is the same fix; if the PM wants it ranked
separately, it should be ranked as CRITICAL.

---

### D-2 — The `full_suite_command` liveness fix widens the project-supplied surface to a field that was previously dead

**Severity: MEDIUM.** M-E's fix is correct — but note the **net effect** on attack surface:
`full_suite_command` is now **live**, so it is now a project-supplied command that reaches
`argv_refusal`. That is the right thing (it was a latent hole: a field that *looked* inert but
would have become live the moment anyone touched the branch structure), and the C-B/C-C work
covers it. But it means the **`command` field is no longer the only live project-supplied
command surface** — there are now four (`command`, `full_suite_command`, `extra_args`, and the
lint tier's command), and each needs the same treatment.

Verified as **covered** (probe 3, V1/V2; probe 4, N1/N2): both `full_suite_command` spellings
refuse correctly.

---

### D-3 — The docs still say `.develcakes/enforcement.json`; the code reads `.crabcakes/enforcement.json`

**Severity: MEDIUM (documentation).** The fix round edited `docs/ARCHITECTURE.md:2426`, `:2437`,
`:2444` and `:2457` — the lines describing the config loader, the command rules, the refusal
contract and the schema — and **all four still name the wrong directory**:

```
  docs/ARCHITECTURE.md:2426: def _load_test_config(project_path) -> TestConfig | None   # content-hash-cached loader from .develcakes/enforcement.json test section
  docs/ARCHITECTURE.md:2437: **Per-project override (§F):** `.develcakes/enforcement.json` overrides global enforcement config.
  docs/ARCHITECTURE.md:2444: **Project-supplied command rules ...** A `.develcakes/enforcement.json` command is untrusted input
  docs/ARCHITECTURE.md:2457: **Test config schema (`.develcakes/enforcement.json` → `test` section):**
```

The code (`agent/enforcement.py:672`) reads:

```python
return os.path.join(project_path, ".crabcakes", "enforcement.json")
```

This was flagged as L-G in my previous report. The rename is repo-wide and pre-existing, so it
is not *introduced* here — but the fix round **touched exactly these lines** and left the wrong
directory name in them, and the fix round is the one that describes the **security contract**
for reading that file.

---

## 5. LOW / NOTE findings

### L-1 — The worktree gitdir pointer can leak an out-of-project path into the approval card

**NOTE.** `_worktree_git_dirs` follows a **project-supplied** `gitdir:` pointer to any directory
that has a `HEAD` (probe 9, P2):

```
  P2 sensitive_reason -> '../reaudit_pointer_dir/sensitive.cfg'
```

The **behaviour is safe** (the write asks — over-refusal is the documented direction), but the
**approval card now names a path outside the project** (`../reaudit_pointer_dir/sensitive.cfg`),
and the PM is asked to approve a write to a file in a directory they did not choose. Worth a
note in the card's wording, not worth a fix.

### L-2 — The C-A gate's blast radius on this repo is 254/255 of its own test files

**NOTE (workflow cost, by ruling).** The PM ruled the gate unconditional, so:

```
  Y7 this repo's tests/: 254/255 .py files are now gated
```

Every test-file write in this repo now needs a PM approval card. That is the **intended**
posture ("the app's own tree is the Coder's default working directory, so an app exemption would
leave that chain reachable in the one place the agent works every day"), and it is recorded here
only so the cost is on the record.

### L-3 — The C-A gate's over-refusal is narrow

**NOTE.** The patterns are anchored (`test_*.py`, `*_test.py`), so ordinary source names are not
caught:

```
  Y1 is_sensitive_path('src/testkit.py')  = False
  Y1 is_sensitive_path('src/testing.py')  = False
  Y1 is_sensitive_path('src/latest.py')   = False
  Y1 is_sensitive_path('lib/contest.py')  = False
  Y1 is_sensitive_path('app/mytest.py')   = False
  Y1 is_sensitive_path('src/protest.py')  = False
```

Only `src/test_utils.py` and `tests/test_helpers.py` — names that genuinely are test files —
are caught.

### L-4 — The `objects/` and `.gitattributes` findings are **pre-existing, not regressions**

**NOTE.** M-2 and M-3 are unchanged from my previous report. Nothing in this fix round made
them worse; they are simply not addressed by it. Recorded so the next round does not treat them
as new.

---

## 6. Refuted vectors (with the command that refutes them)

| # | Vector | Refuted by |
|---|--------|------------|
| R1 | `extra_args = "-p evilplugin"` | probe 4 → `[closed] P1` — `REFUSED: test.extra_args` |
| R2 | `full_suite_command = "python3 -m pytest -p evilplugin"` | probe 4 → `[closed] P2` — `REFUSED: test.full_suite_command` |
| R3 | `command = "python3 -m pytest -p evilplugin"` | probe 4 → `[closed] P3` — `REFUSED: test.command` |
| R4 | The clone-shipped `extra_args` variant | probe 5 → `[closed] A1` — marker=False |
| R5 | The clone-shipped `command` variant | probe 5 → `[closed] A2` — marker=False |
| R6 | `python3 script.py` (the C-C vector) | probe 3 → `[closed] C1` — "script positional refused" |
| R7 | `python3 -m unknownmodule` | probe 3 → `[closed] C4` |
| R8 | `python3 -W error script.py` (value not skipped) | probe 3 → `[closed] C5` |
| R9 | `python3 -ic -m pytest` (eval flag laundered by module resolution) | probe 3 → `[closed] C7` |
| R10 | `python3 -m pytest -p evilplugin` | probe 3 → `[closed] C8` |
| R11 | `python -m pytest -p evil` | probe 3 → `[closed] C9` |
| R12 | `python3.11 -m pytest -p evil` (versioned interpreter) | probe 3 → `[closed] C10` |
| R13 | `python3 -c 'x'` | probe 3 → `[closed] C14` |
| R14 | The agent writing a poisoned test file | probe 15 → `[closed] T2` — asked=1, blocked |
| R15 | The Coder shell running a poisoned test file | probe 15 → `[closed] T3` |
| R16 | A poisoned conftest (the class Fix 3 targeted) | probe 15 → `[closed] T4` |
| R17 | The linked-worktree `.git` FILE making the scan inert | probe 13 → `[closed] T3` — 52 entries (was 0) |
| R18 | The linked-worktree shared-hooks alias | probe 13 → `[closed] T1` — asked=1 |
| R19 | The linked-worktree `config.worktree` alias | probe 13 → `[closed] T2` — asked=1 |
| R20 | A gitdir pointer loop | probe 8 → `[closed] W3` — `dirs=[]` |
| R21 | A gitdir pointer to a huge directory (`/usr`) | probe 9 → `[closed] P3` — refused, 0.1 ms |
| R22 | A gitdir pointer widening the scan to an arbitrary dir | probe 9 → `[closed] P2` — **over-refusal**, the write asks |
| R23 | The pointer changing the sandbox | probe 9 → `[closed] P5` — "Path escapes project sandbox" |
| R24 | `full_suite_command` alone being dead | probe 3 → `[closed] M1` — a tests check ran |
| R25 | A refused `test.command` being a silent skip | probe 3 → `[closed] V1` — 1 check, passed=False |
| R26 | A refused `test.full_suite_command` being a silent skip | probe 3 → `[closed] V2` |
| R27 | A refused `test.extra_args` being a silent skip | probe 3 → `[closed] V3` |
| R28 | A refusal running a subprocess | probe 3 → `[closed] V1/V2/V3` — marker=False in all three |
| R29 | The C-A write gate being app-exempt | probe 11 → `[closed] X1/X2` — unconditional |
| R30 | The C-A write gate missing the runtime gate | probe 7 → `[closed] G5` |
| R31 | The C-A write gate missing `edit_file` | probe 7 → `[closed] G6` |
| R32 | The C-A write gate missing worktrees | probe 11 → `[closed] X4` |
| R33 | A project-supplied `--confcutdir` re-admitting a conftest | probe 4 → `[closed] N6` |
| R34 | The syntax tier being broken by the new grammar | probe 13 → the syntax tier does not call `argv_refusal`; the tier's other argv shapes are all admitted |
| R35 | The module resolution breaking on a flag before `-m` | probe 13 → `[closed] Y5` |
| R36 | The module resolution breaking on an uppercase module name | probe 13 → `[closed] Y6` |
| R37 | `naming_pattern` escaping the project | probe 8 → `[closed] W6` — `_find_related_test` returns None |
| R38 | The Coder shell reaching a pytest positional | probe 6 → `[closed] Q3` — REFUSED |

---

## 7. Test-suite state

**Full suite** (excluding the two GTK suites that segfault without a display — `test_chat_surface.py`
and `test_spec66_live_js.py`, both of which segfault identically on the pristine HEAD tree):

```
$ .venv/bin/python -m pytest tests/ -q --ignore=tests/test_chat_surface.py --ignore=tests/test_spec66_live_js.py
9 failed, 4001 passed, 1822 skipped, 9 warnings in 201.40s (0:03:21)
```

**All 9 failures are pre-existing**, proven against a pristine HEAD export at `/tmp/head_tree`:

| Failing test | HEAD |
|---|---|
| `test_mvp_integration.py::TestN2CompiledWireAdjacency::test_hot_windows_byte_and_line_mode` | **FAILS at HEAD** |
| `test_spec24_facts.py::test_no_new_tool_name` | **FAILS at HEAD** |
| `test_spec40_wire_aware_guard.py` ×3 | **FAILS at HEAD** |
| `test_timer_tool.py::test_delay_parser` | **FAILS at HEAD** |
| `test_tools.py::TestToolSchemaSizeBudget::…[coder\|debugger\|supervisor]` ×3 | **FAILS at HEAD** |

```
$ cd /tmp/head_tree && ... -m pytest tests/test_mvp_integration.py::…::test_hot_windows_byte_and_line_mode \
    tests/test_spec24_facts.py::test_no_new_tool_name tests/test_spec40_wire_aware_guard.py \
    tests/test_timer_tool.py::test_delay_parser -q
6 failed, 29 passed in 1.37s

$ cd /tmp/head_tree && ... -m pytest tests/test_tools.py::TestToolSchemaSizeBudget -q
3 failed in 1.17s
```

The fix-round suites specifically:

```
$ .venv/bin/python -m pytest tests/test_enforcement.py tests/test_spec66_approval_and_eval.py tests/test_tools.py -q
3 failed, 413 passed, 1 skipped, 1 warning in 8.62s
```

The 3 failures are the `TestToolSchemaSizeBudget` ones above — **pre-existing**, not caused by
the fix round.

### Test-vacuity analysis (probe 10 — in-process reverts via `reaudit_plugin.py`)

Each fix was reverted **inside the pytest process** (a parent-process monkeypatch does not reach
a pytest subprocess) and the corresponding suite re-run.

| Fix | Baseline | Reverted | Verdict |
|-----|----------|-----------|---------|
| **C-A** — the test-file write gate | rc=0 (123 passed) | rc=1 (**7 failed**) | **REAL** |
| **C-B** — the `extra_args` validation | rc=0 (154 passed) | rc=1 (3 failed) | **REAL** |
| **C-B** — the module-keyed flag rules | rc=0 (277 passed) | rc=1 (**29 failed**) | **REAL** |
| **C-C** — the python positional refusal | rc=0 (277 passed) | rc=1 (6 failed) | **REAL** |
| **M-E** — the `full_suite_command` liveness | rc=0 (154 passed) | rc=1 (16 failed) | **REAL** |
| **M-A** — the worktree gitdir resolution | rc=0 (123 passed) | rc=1 (3 failed) | **REAL** |
| **P3** — the visible FAILED refusal | rc=0 (154 passed) | rc=1 (6 failed) | **REAL** |

**Every new fix has a real regression test.** This is a genuine improvement over the previous
round, where **Fix 5 and Fix 8 had zero coverage** and the fix round's own brief listed them as
shipped. The revert-guard pattern (`test_python_positional_rule_reddens_if_reverted`) is a good
habit and should be the norm.

---

## 8. Ranked list of what remains

**P0 — code execution with no approval card anywhere**

1. **Refuse a pytest import target in the project-supplied fragment.** Extend the pytest branch
   of `argv_refusal` with the positional rule the python and node branches already have, applied
   to the **project-supplied fragment only** (so the tier's own appended related-test path still
   works). Closes **C-B-1** and, if extended to the whole argv, **C-A-1** too.
2. **Make the C-A gate follow the tier's CONVENTIONS, not file names.** Gate on "could the tier
   point pytest at this path?" — i.e. `_find_related_test`'s candidate set, including the
   `__tests__/{module}.py` convention and the project-supplied `naming_pattern` / `test_dir` —
   not on `test_*.py` / `*_test.py`. Closes **M-1** and **D-1**.
3. **Decide the C-A-1 posture explicitly.** Fix 12 already ruled the tiers a QUALITY control.
   A tier that **imports and executes** a cloned repository's file is not a quality control. If
   the ruling stands, the honest closure is: **the tier must not execute untrusted repository
   code at all** — run the project's tests in a mode that does not import them, or refuse the
   tests tier for a project whose test files the PM has not approved. If the ruling is amended,
   say so in the same place.

**P1 — the alias walk is still incomplete**

4. **Gate the subdirectory git-executed config** (M-2): change `("exact", ".gitattributes")` /
   `("exact", ".gitignore")` to basename globs, or walk the project root.
5. **Correct the `objects/` justification** (M-3): the hash-name argument is false. Either walk
   `objects/` (bounded — `scandir` the 256 fan-out dirs, do not stat every object) or replace the
   comment with the **true** argument (the corruption vector) and **amend
   `test_git_objects_stays_excluded`** so it stops pinning a false claim.

**P2 — hygiene**

6. **Fix the `.develcakes/` → `.crabcakes/`** in the four `docs/ARCHITECTURE.md` lines the fix
   round edited (D-3).
7. **Note the out-of-project path in the worktree approval card** (L-1), or clamp the reported
   alias to a project-relative form.

---

## 9. One sentence

**The fix round closed C-B, C-C, M-A, M-E and the visible-refusal contract properly — every one
with a real revert-guarded regression test — but C-A was closed only on the write side, so the
clone-shipped test-file execution chain is still open, and the pytest *positional* the C-B work
did not cover still executes a project-supplied file with zero approval cards.**

---

## 10. What is NOT in this report

- **No product code was changed.** Every finding comes from probes under
  `.crabcakes/scratch/reaudit/`; the working tree is byte-identical to the dispatch state.
- The working tree was **never** stashed, checked out, or otherwise altered. The HEAD baseline
  used for comparison is a pristine export at `/tmp/head_tree`
  (`git archive HEAD | tar -x -C /tmp/head_tree`).
- **M-2, M-3, D-3** are **carried over unchanged** from my previous report — they were not
  addressed by this fix round and are not regressions. They are re-verified here so the record
  is current.
- The probes need a Python with `pytest` importable for the tier to actually run it.
  `/tmp/spec66venv` is a real venv with pytest 9.2.0; the probes build a project-local `.venv`
  from it. The app venv (`.venv`) has no `pygments`, so a venv built by symlinking only the app
  venv's pytest packages fails to import.
- `mypy` and `eslint` are not installed on this host, so the flag rules for those binaries were
  verified by the validator's return value, not by execution.
- Windows/OneDrive path semantics remain **out of scope** (the alias/inode model assumes POSIX
  inode semantics).
- The full-suite run completed: **9 failed, 4001 passed, 1822 skipped** — all 9 failures
  reproduce identically on a pristine HEAD export, so none is caused by the fix round.
