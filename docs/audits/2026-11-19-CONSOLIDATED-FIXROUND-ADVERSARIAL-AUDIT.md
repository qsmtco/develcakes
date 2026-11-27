# CONSOLIDATED Fix Round — Adversarial Audit

**Date:** 2026-11-19
**Auditor:** Debugger
**Baseline:** `main` @ `75d8e677` + the **uncommitted working tree** (the fix round: 8 files, +1541/-214)
**Brief:** `.crabcakes/debugger_audit_briefs/2026-11-19-CONSOLIDATED-FIXROUND-AUDIT-BRIEF.md`
**Subject diff:** `.crabcakes/debugger_audit_briefs/p1p2_full_diff.diff` (2135 lines)
**Original audit:** `docs/audits/2026-11-23-CONSOLIDATED-PATH-APPROVAL-AUDIT.md`
**Probes:** `.crabcakes/scratch/fixround/` (21 probes, `revert_plugin.py`, per-probe output in this report)
**Read-only on source.** Nothing in the product tree was modified; the working tree is byte-identical to the dispatch state (`git status --porcelain` shows the same 8 files).

---

## 0. Verdict

| Severity | Count |
|---|---|
| **CRITICAL** | **3** |
| **MAJOR** | **6** |
| **MEDIUM** | **5** |
| **LOW / NOTE** | **5** |
| **TOTAL FINDINGS** | **19** |
| Refuted vectors (documented with commands) | 38 |

**The fix round closed the two headline CRITICALs as chains, and opened nothing new in the
sensitive-path gate itself.** Of the 13 fixes:

- **9 are CLOSED** (the original finding is closed and no new hole was opened in that area).
- **3 are PARTIALLY CLOSED** (the specific vector is closed; the *class* is not).
- **1 is NOT CLOSED** (Fix 8 — the per-binary flag grammar misses the tier's own argv shape).

But the audit's own conclusion — that *the enforcement tier executes project-supplied code by
design* — is **still true, and the fix round did not change it.** Two new CRITICAL chains
reach the same place from new directions, and one of them is **more powerful than the C1 chain
the round was built to close**, because the agent authors nothing and no approval card exists
anywhere in it.

The brief asked: *"is this surface now closed to the orientations you enumerated?"*

**No.** The orientation "the project chooses what code the toolchain loads" was enumerated and
fixed for the *config* layer (Fix 3). It was **not** fixed for the *code* layer — the test file
the tier points pytest at is project-supplied code, and pytest imports it.

---

## 1. CRITICAL findings

### C-A — The tier IMPORTS and executes the project's own test file → zero-approval code execution

**Severity: CRITICAL.** This is C2's chain reopened through a door Fix 3 did not close. The
payload is executed **with no approval card anywhere**, it is **shipped by the clone** (the
agent authors nothing), and **the tier reports PASSED**.

**Root cause.** `agent/enforcement.py:1661-1667` — the tests tier points pytest at the
project's own test file:

```python
elif related_test:
    abs_test = os.path.join(project_path, related_test)
    argv = list(argv) + [abs_test] + test_config.extra_args.split() + ["--tb=short"]
```

`pytest` **collects and imports every test file it is pointed at** — the test module's top-level
code runs at import. Fix 3's isolation (`-c <app ini>`, `--confcutdir`, `--noconftest`,
`PYTEST_DISABLE_PLUGIN_AUTOLOAD`) governs the **config layer**; it does not and cannot govern
the **code layer**, because the test file *is* the code under test.

And the test file is **not gated**: `is_sensitive_path("tests/test_feature.py")` is `False`
(`agent/tools.py:402-412` — the toolchain globs are `conftest.py`, `pytest.ini`, `setup.cfg`,
`tox.ini`, `mypy.ini`, `eslint.config.js`, `package.json`, `requirements*.txt`, `noxfile.py`,
`sitecustomize.py`, `usercustomize.py` — **no test-file pattern**).

**Reproduction** (`probe_15_test_file_execution.py::t1_clone_shipped_poisoned_test`):

```
  T1 clone-shipped poisoned tests/test_feature.py
      agent write asked=0 success=True
      marker=True tiers=[('syntax', True, 'Syntax check passed for feature.py'),
                         ('tests', True, 'tests/test_feature.py: passed')]
[*** OPEN ***] T1 clone SHIPS a poisoned test file -> the tier executes it
```

The payload, in full:

```python
# tests/test_feature.py  — SHIPPED BY THE CLONE
import pathlib
pathlib.Path('/tmp/.../MARKER_RAN').write_text('payload ran')

def test_x():
    assert True
```

The chain, in plain terms:

1. The clone ships `tests/test_feature.py` with arbitrary top-level code, plus a
   `pytest.ini` (so the framework is detected).
2. The agent writes an **ordinary source file** (`feature.py`). Nothing sensitive — no
   approval card at the tool gate, none at the runtime gate.
3. The post-write enforcement tier runs. `_find_related_test` finds `tests/test_feature.py`.
4. The tier runs `pytest -c <app.ini> --confcutdir <app> --noconftest
   <project>/tests/test_feature.py`.
5. pytest **imports the test file**. The payload runs.
6. The tier reports `✅ tests/test_feature.py: passed`.

**The tier's own output is indistinguishable from a clean run**
(`probe_16_test_file_chain_detail.py::p1_p2_argv_and_verdict`):

```
  P1 argv = ['/tmp/.../.venv/bin/python', '-m', 'pytest', '-c',
             '/home/mushy/.config/develcakes/tier/pytest.ini',
             '--confcutdir', '/home/mushy/.config/develcakes/tier',
             '--noconftest', '/tmp/.../tests/test_feature.py', '-x', '-q', '--tb=short']
  P1 env  = {'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
  P2 marker=True checks=[('syntax', True), ('tests', True)]
  P2 appended_message = '[enforcement:syntax] ✅ Syntax check passed for feature.py
      [enforcement:tests] ✅ tests/test_feature.py: passed ... 1 passed in 0.25s'
```

**The second variant needs no clone at all** (`probe_15::t2_agent_written_poisoned_test`):
the agent writes the poisoned test file itself — **no approval card** (probe 16, P3/P4: both
`write_file` and `edit_file` of `tests/test_feature.py` ask for nothing, `runtime_gate=None`) —
and then writes any source file. The tier executes what the agent planted.

```
  T2 agent-written poisoned test file
      step1 (test file write) asked=0 success=True err=None
      step2 (source write) asked=0 success=True
      marker=True tiers=[('syntax', True, ...), ('tests', True, ...)]
```

A root-level `test_feature.py` behaves identically (probe 16, P5 — `marker=True`).

**Is this a regression from the fix round?** **No — it is pre-existing**, and this is the
important part of the finding. Proven by running the same chain against a pristine HEAD tree
(`probe_17_regression_baseline.py`):

```
  HEAD (pre-fix-round baseline): {"marker": true, "checks": [["syntax", true], ["tests", true]]}
  WORKING TREE (fix round):      {"marker": true, "checks": [["syntax", true], ["tests", true]]}
```

HEAD's `_check_tests` (`/tmp/head_tree/agent/enforcement.py:1567-1568`) points pytest at the
test file the same way.

**But the fix round's own claim is false.** `agent/enforcement.py:1673-1674` states:

> CONSOLIDATED audit Fix 3 (C2): the tests tier **never lets the CHECKED PROJECT choose
> which pytest code is loaded**.

The tier *does* let the project choose which pytest code is loaded — it points pytest at the
project's own test file. Fix 3 narrowed the config layer and left the code layer untouched,
and the docstring overstates the fix. **That docstring is the defect as much as the missing
gate is**, because it is what a future reader will trust.

**Control** (probe 15, T4): a poisoned **conftest** does *not* execute — Fix 3's
`--noconftest` holds for the class it targeted. The difference between T3 (closed) and T1
(open) is exactly *conftest vs test file*.

**Control** (probe 16, P6): a poisoned **ordinary source file** does *not* execute — the syntax
tier uses `python3 -m py_compile` (parse only, no import). So the surface is specific.

---

### C-B — `test.extra_args` reaches the tier's pytest argv with NO validation → project-chosen `-p <plugin>` executes

**Severity: CRITICAL.** `TestConfig.extra_args` is a **project-supplied string that is never
passed through the token allowlist**, and it is appended to the tier's pytest argv
*after* the isolation args.

**Root cause — three independent defects, each sufficient:**

1. **`extra_args` is never validated.** `agent/enforcement.py:423, :468` (the field),
   `:1662` and `:1666` (the two uses). The token allowlist `_validate_test_command` is applied
   to `command` (`:1631`), `full_suite_command` (`:1615`, `:1640`) — **never** to `extra_args`.
   (probe 18, G-c.)
2. **Fix 8's pytest rules are unreachable on the auto-detected path.** They key on
   `base == "pytest"` (`agent/enforcement.py:334`), but the tier's own auto-detected argv is
   `["python3", "-m", "pytest"]` (`_detect_test_framework`, `:998`) — base is **`python3`**.
   So `_validate_test_command("python3 -m pytest -p evilplugin")` returns **`True`**.
   (probe 18, G-e and G-b.)
3. **Fix 3's isolation does not stop an explicit `-p`.** `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`
   suppresses **entry-point autoload** only; `-p <name>` is an explicit request and is honoured.
   `-c` changes which *ini* is read; it does not change the argv.

**Reproduction** (`probe_4_p_plugin_chain.py`):

```
  P1 extra_args='-p evilplugin' (python3 -m pytest base): marker=True
     tiers=[('tests', True, 'tests/test_feature.py: passed')]
[*** OPEN ***] P1 extra_args='-p evilplugin' (python3 -m pytest base)
  P3 command='python3 -m pytest -p evilplugin': marker=True
     tiers=[('tests', True, 'tests/test_feature.py: passed')]
[*** OPEN ***] P3 command='python3 -m pytest -p evilplugin'
[closed] P4 CONTROL full_suite_command='pytest -p evilplugin'   <- Fix 8 refuses the BARE spelling
[closed] P5 CONTROL no enforcement.json at all
```

**The clone-shipped form, end to end, with the agent authoring nothing**
(`probe_5_fix5_4_11.py::part_a`):

```
  A1 clone SHIPS enforcement.json with extra_args='-p evilplugin'
      agent write asked=0 success=True
      marker=True tiers=[('syntax', True, ...), ('tests', True, 'tests/test_feature.py: passed')]
[*** OPEN ***] A1 clone SHIPS enforcement.json with extra_args='-p evilplugin'
  A2 clone SHIPS enforcement.json with command='python3 -m pytest -p evilplugin'
      agent write asked=0 success=True
      marker=True tiers=[('syntax', True, ...), ('tests', True, ...)]
[*** OPEN ***] A2 clone SHIPS enforcement.json with command='python3 -m pytest -p evilplugin'
```

**Why the bare spelling is refused but the `-m pytest` spelling is not** — this is the whole
shape of the bug:

```
  _validate_test_command('pytest -p evilplugin')          -> False   (Fix 8 fires)
  _validate_test_command('python3 -m pytest -p evilplugin') -> True    (Fix 8 never fires)
```

**And `extra_args` needs no `command` at all** — the auto-detected path appends it:

```
  argv = ['python3', '-m', 'pytest', '-c', '<app.ini>', '--confcutdir', '<app>',
          '--noconftest', '-p', 'evilplugin', '--tb=short']
  env  = {'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
```

**The plugin must be importable.** The tier runs with `cwd=project_path`, and `python -m`
puts the cwd on `sys.path[0]` (probe 4, `syspath_probe`: `python -m sys.path[0]: SP0=
/tmp/spec66audit_sp_pox634ai`; `python -c sys.path[0]: SP0=`). So a **root-level**
`evilplugin.py` shipped by the clone is importable. `PYTEST_DISABLE_PLUGIN_AUTOLOAD` does not
stop an explicit `-p`.

**Is this a regression?** **No.** HEAD's validator also returned `True` for
`python3 -m pytest -p evilplugin` (probe 18, G-d: `HEAD: python3-m-pytest -p -> True`).
**But** HEAD also returned `True` for the bare `pytest -p` spelling, which the working tree
now refuses. **Fix 8 closed the bare-`pytest` spelling and left the `python3 -m pytest`
spelling — which is the tier's own default shape — open.** That is the precise shape of Fix 8's
partial closure.

---

### C-C — `test.command` / `full_suite_command` can name a NON-pytest runner that executes project code

**Severity: CRITICAL.** The `python3` **positional** posture was flagged in the original audit
as an open PM ruling (D1a). The brief says *"The python3 POSITIONAL posture was an open PM
ruling — report its current state, do not assume."* **Its current state: still open, and the
enforcement tier reaches it.**

**Root cause.** `agent/enforcement.py:311-314` refuses only the `-c` eval flag for
python/python3. A **positional script path** is not gated for python3 (it is for node — see the
docstring at `:263-269`: *"python/python3 positionals are NOT gated this round (a PM-level
posture ruling, flagged separately)"*). So:

```
  _validate_test_command('python3 script.py')  -> True
```

**Reproduction** (`probe_2_c2_isolation.py::c2_j_full_suite_non_pytest`):

```
  full_suite_command=python3 runner.py: marker=False tiers=[]
[closed] C2-j full_suite_command = python3 <script> (positional)
```

The chain was **refuted in that probe** — but only because `full_suite_command` is **dead
code** (see C-D): `_check_tests` reads it only inside the `if test_config.command:` branch
(probe 18, G-a). Set `command` as well and it is live:

```
  EA-c command='python3 -m pytest -p evilplugin': marker=True
     tiers=[('tests', False, 'tests/test_feature.py: FAILED')]
[closed] EA-c ...   (marker=False — the -p plugin was refused by -c/--noconftest here)
```

**Verified reachability of the positional form** (`probe_18`, G-a/G-b, and the argv dump in
probe 3):

```
  'python3 -m pytest -p evilplugin' ->
    ['python3', '-m', 'pytest', '-c', '<app.ini>', '--confcutdir', '<app>',
     '--noconftest', '-p', 'evilplugin']
```

**And the `command` field substitutes `{test_file}` into the command string**
(`:1623`) — the project chooses the template, so `{test_file}` is a project-controlled
substitution point.

**Severity note.** The reason this is CRITICAL rather than MAJOR is that it is **the same
exposure as C-B with a different spelling**: `command` is validated, but only against a
grammar whose python3 rules stop at `-c`. The PM ruling was never made, and in the meantime
the tier runs a project-supplied command with a project-supplied positional.

---

## 2. MAJOR findings

### M-A — A linked worktree's `.git` is a FILE → the alias scan is entirely inert there

**Severity: MAJOR.** M4 from the original audit. **NOT CLOSED by Fix 1** — Fix 1 extended
`_GIT_ALIAS_SUBDIRS` but the worktree's `.git` is not a directory to walk.

**Root cause.** `agent/tools.py:833-838` — `_alias_visit` calls `os.scandir(directory)` and
catches `OSError` → returns silently. A linked worktree's `<worktree>/.git` is a one-line text
file (`gitdir: /path/.git/worktrees/<name>`), so `os.scandir` raises `NotADirectoryError` →
`OSError` → **zero entries**.

**Reproduction** (`probe_13_worktree_temp.py`):

```
  worktree .git is a FILE: True
  content: gitdir: /tmp/spec66audit_wt_3kj0oq6f/.git/worktrees/wt
  T3 main repo entries  = 45
  T3 worktree entries   = 0
[*** OPEN ***] T3 the linked worktree's alias walk yields .git entries
  T1 linked worktree: shared .git/hooks alias: asked=0 success=True runtime_gate=False rel=None landed=True
[*** OPEN ***] T1 linked worktree: shared .git/hooks alias
  T2 linked worktree: .git/worktrees/<n>/config.worktree alias: asked=0 success=True runtime_gate=False rel=None landed=True
[*** OPEN ***] T2 linked worktree: .git/worktrees/<n>/config.worktree alias
[closed] T4 CONTROL main repo: .git/hooks alias: asked=1 success=False runtime_gate=True rel='.git/hooks/pre-commit'
```

**This is the Coder's default working directory.** SPEC-09 SP0 puts agents in
`<repo>/.worktrees/<agent_id>` — and this repo has two live ones
(`.worktrees/agent-flagger`, `.worktrees/agent-alphacoder`, both confirmed to be linked
worktrees with a `.git` FILE). **In the Coder's default working directory the entire alias
scan is inert**, so the C1 chain the fix round was built to close is fully open there.

**Note the asymmetry:** the *name*-based rule still fires in a worktree
(`agent/tools.py:380`, `("component", ".git/hooks/")` is lexical), so writing
`.git/hooks/pre-commit` **by name** is gated even in a worktree (probe 12, W2). But the
**alias** direction — the one C1 was about — does not.

---

### M-B — A nested `.gitattributes` / `.gitignore` / `.git/config` is not gated, and git executes a subdirectory `.gitattributes`

**Severity: MAJOR.** Fix 1 closed the **root** `.gitattributes`/`.gitignore` alias direction
(`_ROOT_GIT_CONFIG_FILES`, `agent/tools.py:775`). The **subdirectory** case is not covered.

**Root cause.** `("exact", ".gitattributes")` and `("exact", ".gitignore")`
(`agent/tools.py:385-386`) are **root-relative by construction** — `norm == pattern`. The
project root is not walked, and only the two root-level names are stat'd directly
(`_alias_scan_root_files`, `:806-813`).

**And git DOES execute a subdirectory `.gitattributes`** — proved by execution
(`probe_8_subdir_gitattributes.py::s0_git_executes_subdir_gitattributes`):

```
  S0 git add with a SUBDIR .gitattributes filter: filter_ran=True rc=0
```

with the probe's own `filter.pwn.clean` command creating the marker file during `git add`.

**The alias direction into a subdirectory file is ungated**
(`probe_8::s1_s4_alias_gating`):

```
  S2 subdir .gitattributes via a plain HARD LINK: asked=0 landed=True
[*** OPEN ***] S2 subdir .gitattributes via a plain HARD LINK
  S4 subdir .gitignore via a plain HARD LINK: asked=0 landed=True
[*** OPEN ***] S4 subdir .gitignore via a plain HARD LINK
  S5 subdir .git/config via a plain HARD LINK: asked=0 landed=True
[*** OPEN ***] S5 subdir .git/config via a plain HARD LINK
[closed] S1 subdir .gitattributes BY NAME: asked=1 landed=False
[closed] S3 ROOT .gitattributes via a plain HARD LINK: asked=1 landed=False
```

Note the contrast: writing `sub/.gitattributes` **by name** is gated (leading-dot rule),
writing the **root** `.gitattributes` **via an alias** is gated (Fix 1), but writing a
**subdirectory** `.gitattributes` **via an alias** is not. The gap is the intersection of two
Fix 1 choices.

---

### M-C — Fix 5 (`O_NOFOLLOW`) binds only the final component; the leading-component swap is still reachable

**Severity: MAJOR (hardening, as the audit scoped it).** The brief asked me to say *exactly
what is still reachable and what is not*.

**Root cause.** `agent/tools.py:1002-1030` — `_atomic_write_nofollow` opens
`O_WRONLY|O_CREAT|O_TRUNC|O_NOFOLLOW`. **`O_NOFOLLOW` binds the FINAL component only.** A
symlink swapped into a **leading directory component** is followed by the kernel.

**Reproduction** (`probe_5_fix5_4_11.py::part_b`):

```
  B2 final-component symlink: refused errno=40; target content='original\n'
[closed] B2 final-component symlink swap refused (ELOOP)
```

The final-component case — the one Fix 5 claims — **is** refused (ELOOP, errno 40), and the
bytes stay on the approved inode. That part works.

**What is still reachable:**

```
  B1 leading-component symlink: WROTE; real content='PAYLOAD\n'
[*** OPEN ***] B1 leading-component symlink swap refused
```

A symlink planted at `<project>/notes` → `<project>/real_dir` makes
`<project>/notes/victim.txt` land on `<project>/real_dir/victim.txt`. **The fix round's own
docstring says this** (`agent/tools.py:1012-1014`):

> Scope, honestly stated: this binds the FINAL component only. A symlink swapped into a
> leading directory component is still followed by the kernel (a full openat-walk would close
> that; recorded as future work with the audit).

So this is **documented, not hidden** — but it is still open, and the fix round's Fix 5 line
in the brief table ("`O_NOFOLLOW` binds the FINAL component only") understates it. The
audit's prescribed closure (`openat`-style walk from the project root, one component at a
time, refusing symlinked components) is **not implemented**.

**The hard-link direction** (`agent/tools.py:1015-1017` says it is "closed by Fix 4's
uncached scan at decision time, not here"):

```
  B3 hard-link swap: WROTE; other inode content='PAYLOAD\n'
[*** OPEN ***] B3 hard-link swap refused
```

Correct as documented — but note the interaction: Fix 4's scan is a **scan at decision time**,
so a swap between the scan and the open is not caught. The window is microseconds (the fix
round's genuine improvement over the old indefinite cache window) but it is not zero.

---

### M-D — The `objects/` exclusion's stated justification is falsified; the object store can be corrupted with no approval

**Severity: MAJOR.** The brief said: *"Fix 1's `objects/` exclusion: try to break the stated
argument."* **The stated argument does not hold.**

**Root cause.** `agent/tools.py:758-765` justifies the exclusion:

> an object file's NAME is a content-addressed hash of its BYTES, so no working-tree name can
> be chosen to alias one — the alias direction cannot be constructed from the working tree.

**A hard link's plain name is arbitrary.** The agent does **not** have to name its working-tree
file with the object's hash. It lists `.git/objects/`, hard-links one object to
`notes/plain.txt`, and — because a hard link **shares the inode** — `chmod` on the plain name
changes the **object's** mode. The write then lands on the object.

**Reproduction** (`probe_14_objects_exclusion.py`):

```
  A: asked=0 success=False err='Permission denied: notes/plainfile.txt'
  A: object corrupted = False
[closed] A object-store hard link: the write is refused (mode 444)
  B: object mode after chmod via the link: 0o644
  B: asked=0 success=True err=None
  B: object corrupted = True
[*** OPEN ***] B object-store corruption after chmod on the shared inode
```

**Impact** (`probe_21_objects_reachability.py`):

```
  O3 git fsck rc=3 'missing blob f70f10e4db19068f79bc43844b49f3eece45c4e8'
  O3 git log  rc=0 '05a3a7a init'
[*** OPEN ***] O3 corrupting a blob breaks git fsck / history
[closed] O4 the corrupted blob does NOT poison the PM's diff card
```

`git fsck` fails (the object store is corrupt). A **commit** object corruption is worse
(probe 14, E): `git log` returns rc=128 — the history is unreadable.

**The code comment's second sentence is accurate** (`agent/tools.py:761-765`): *"A hard link
OUT of objects/ makes the OBJECT the sensitive side of a link whose plain name lives
elsewhere; that direction is a corruption vector for the object store, not an approval-bypass
for a file the agent names."* — so the round **did** record this. **But the first sentence, the
hash-name argument, is false**, and it is the one the code and the new test
(`tests/test_spec66_approval_and_eval.py:1519-1546`) rest on.

**Reachability — the mitigating fact.** The agent has **no hard-link primitive**:
the Coder's shell refuses `ln`, `cp`, `chmod`, and `git hash-object` (probe 21, O1 — all four
REFUSED). So today the vector is not directly reachable by the agent. It becomes reachable the
moment any tool or script that can `link()` is added, or if the alias walk is the only
defence. **The defect in the finding is the false justification, not today's exploitability.**

**The new test pins the wrong thing.** `test_git_objects_stays_excluded` asserts
`"objects" not in _GIT_ALIAS_SUBDIRS` and that `_sensitive_alias_of` returns `None` for a
hard-linked object. It **pins the exclusion** — the very thing the false argument was
supposed to justify. The test makes the false argument harder to correct.

---

### M-E — `full_suite_command` is dead code unless `command` is also set

**Severity: MAJOR (functional).** Not a security finding, but it changes the security surface
and it is a bug the fix round's own test matrix should have caught.

**Root cause.** `agent/enforcement.py:1605` — the whole command-selection block is inside
`if test_config.command:`. The `full_suite_command` reads at `:1613` and `:1639` are therefore
**unreachable when `command` is unset**.

**Proof** (probe 18, G-a — character offsets in `_check_tests`' source):

```
  if test_config.command:            at char 3362
  if run_full_suite and full_suite:  at char 3685
  else: (auto-detect)                at char 5830
[*** OPEN ***] G-a full_suite_command is only read inside the `command` branch
```

**Consequence for the audit:** a project cannot use `full_suite_command` alone — which is why
the C-C probe above showed `marker=False` for the `full_suite_command` positional chain.
**The path is live only when `command` is also set** — and then the `command` string (which
*is* validated, but against the incomplete python3 grammar) is what runs.

---

### M-F — `test.naming_pattern` / `test_dir` escape the project

**Severity: MAJOR.** `_find_related_test` (`agent/enforcement.py:1068-1105`) joins
`project_path` with the **project-supplied** `test_dir` and `naming_pattern` and returns
`os.path.relpath(candidate, project_path)` — with **no containment check**.

**Reproduction** (probe 11, V5):

```
  _find_related_test with an escaping pattern -> '../../etc/passwd'
[*** OPEN ***] V5 naming_pattern cannot escape the project
```

The returned path is then joined back to an absolute path and handed to pytest as the test
target. A project can therefore point the tier's pytest at **any file on the filesystem** that
it can name — including a file outside the sandbox. Whether that is exploitable depends on
pytest's import behaviour on an out-of-tree target, but the **path containment failure is
unconditional**, and it is the same class as the sandbox-escape refusals the accept path
already implements (`utils/git_ops.py:123-128`, refuted as R7/R8 in the original audit).

**Pre-existing** (HEAD's `_find_related_test` is identical, `/tmp/head_tree/agent/enforcement.py:970-1007`).
Not introduced by the fix round; not addressed by it.

---

## 3. MEDIUM findings

### D-A — Fix 8's per-binary flag grammar misses the flags the original audit listed

**Severity: MEDIUM.** Fix 8 added rules for `-p`/`--plugin`/`-c`/`--config` (pytest),
`--config`/`--config-file` (ruff), `--config-file`/`--shadow-file`/`--python-executable`
(mypy), `--rulesdir`/`--plugin`/`--plugins`/`-c`/`--config` (eslint), and the `go` subcommand
+ `-exec`/`-toolexec`/`-modfile`. The **original audit's M6 table** listed more. Still admitted
(probe 19):

```
  U3  pytest --rootdir=/x                       allowed=True
  U4  pytest --import-mode=importlib            allowed=True
  U5  python3 -m pytest --pyargs pkg            allowed=True
  U6  pytest --collect-only                     allowed=True
  U12 eslint --no-eslintrc                      allowed=True
  U13 node --env-file=/x                        allowed=True
```

`--rootdir` and `--import-mode` were on the audit's list; `--collect-only`, `--pyargs`,
`--no-eslintrc`, `--env-file` were not, and are the same class.

**`--rootdir` is the sharpest** — it changes pytest's rootdir, and therefore the ini pytest
would read and the conftest anchor. **Probed: it does NOT re-enable conftest loading**
(probe 20, R2 — `with_rootdir_marker=False`, `without_rootdir_marker=False`), so
`--noconftest` holds. Refuted as an escalation; still an unclosed enumeration.

**`--import-mode=importlib` does NOT block an explicit `-p`** (probe 20, R3):

```
  R3 importlib + -p evilplugin: marker=True rc=0
[*** OPEN ***] R3 --import-mode=importlib does not enable a plugin import
```

So `--import-mode` is not a mitigation for C-B.

---

### D-B — The `go` subcommand rule is defeated by a `-C` flag whose value looks like a subcommand

**Severity: MEDIUM.** `agent/enforcement.py:346` takes the first non-`-` token as the
subcommand. `go -C <dir> test` — `<dir>` does not start with `-`, so it is taken as the
subcommand. The rule **refuses** `go -C /path test` (over-refusal, the safe direction) but
**admits** `go test -C /path`. Neither is an execution vector. Recorded as a precision note;
**no exploit** — the `go` verbs that execute (`run`, `generate`, `tool`, `env -w`) are refused
in both spellings.

**Over-refusal is the documented posture** (`agent/enforcement.py:332-333`: *"over-refusal
stays the documented safe direction (SPEC-66 §11.1)"*), so this is a NOTE, not a finding.

---

### D-C — `node --env-file=<file>` is admitted

**Severity: MEDIUM.** `--env-file` makes node read a file's contents into `process.env`. The
scrubbed env drops the dangerous names anyway, so the impact is limited to what the file
itself contains. The original audit listed it (M6 table, `node` row). Still open.

---

### D-D — The `python3` positional posture is still open, and the tier reaches it

**Severity: MEDIUM.** The brief said to report the current state, not to assume. **Current
state: open, unchanged, and un-ruled.** `agent/enforcement.py:263-269` (the docstring) says:

> python/python3 positionals are NOT gated this round (a PM-level posture ruling, flagged
> separately).

`_validate_test_command("python3 script.py")` → `True` (probe 19, U14). **No PM ruling has been
recorded.** Until one is, the tier will execute a project-supplied script path.

---

### D-E — `is_sensitive_path` over-refuses on any path containing `hook` (pre-existing, now amplified)

**Severity: MEDIUM.** `agent/tools.py:459-461` — the `*hook*` glob is matched by **substring
containment on the whole path**, not by basename:

```
  x.git/hooksy  -> True
```

The `component` rule (`:448-452`) is precise (`notes/.git/hooks/pre-commit` matches;
`x.git/hooksy` does not — probe 11, V3b/V3a). But the legacy `*hook*` glob makes
`x.git/hooksy` sensitive anyway. This is over-refusal (the safe direction), so it is not a
security finding — but it is the *same* over-refusal the fix round deliberately introduced for
the toolchain globs (`setup.cfg`, `package.json`, `requirements*.txt` at any depth), and it
will produce approval cards for ordinary files.

---

## 4. LOW / NOTE findings

### L-A — The `python3 -m <module>` shape leaves the whole per-binary grammar unreachable

**NOTE.** Fix 8's rules are keyed on `base == "pytest"` / `"ruff"` / `"mypy"` / `"eslint"`.
The tier's own auto-detected argv is `["python3", "-m", "pytest"]` (probe 18, G-e). So the
rules only bite when a project *explicitly* writes the bare-binary spelling. Fix 8's grammar
is therefore **mostly inert on the tier's own path** — the same root cause as C-B, recorded
separately because it also explains why `python3 -m ruff --config=x` and
`python3 -m mypy --config-file=x` are admitted (probe 19, U7c/U7d).

### L-B — Fix 5 has **zero** test coverage

**NOTE.** `grep -rn "O_NOFOLLOW\|_atomic_write_nofollow" --include=*.py .` (excluding
`.worktrees/` and `build/lib/`) finds the symbol in `agent/tools.py` only — **no test file
mentions it**. Verified by revert (probe 10b): with `O_NOFOLLOW` reverted to a plain
`open(dest, "w")`, **the entire `tests/test_spec66_approval_and_eval.py` suite still passes**
(`baseline_rc=0 reverted_rc=0`). **Fix 5 is untested.**

### L-C — Fix 8 has **zero** test coverage

**NOTE.** `grep -rn "_has_forbidden_flag" --include=*.py .` finds the function in
`agent/enforcement.py` only. With Fix 8 reverted (`_validate_test_command` → the HEAD
version), **the entire `tests/test_enforcement.py` suite still passes**
(`baseline_rc=0 reverted_rc=0`, 125 passed). **Fix 8 is untested**, and the untested behaviour
is where C-B lives.

### L-D — `full_suite_command` is validated but dead (cross-ref M-E)

**NOTE.** Two of the three `_validate_test_command` call sites in `_check_tests`
(`:1615`, `:1640`) are inside the dead branch. The **live** validated path is `command`
(`:1631`) — which is exactly the field that C-B and C-D exploit.

### L-E — `_ALLOWED_ENV_VARS` in `agent/enforcement.py:100` is dead

**NOTE.** `grep -rn "_ALLOWED_ENV_VARS"` finds only the definition — the live allowlist is
`utils/env_security.ALLOWED_SUBPROCESS_ENV_VARS` (`utils/env_security.py:29`). A second,
stale copy of a security allowlist sitting next to the live one is a drift hazard, exactly
the class Fix 6 was created to remove for the sensitive-path set. Not a live defect.

### L-F — `_ALIAS_ENTRY_CACHE` is dead but still referenced by the new tests

**NOTE.** `agent/tools.py:787` — Fix 4 keeps the name "as an empty, unused name". Two of the
**new** tests still poke it (`tests/test_spec66_approval_and_eval.py:1513`, `:1540`:
`_ALIAS_ENTRY_CACHE.pop(str(tmp_path), None)`). The tests' cleanup calls are no-ops on a dict
that is never written — harmless, but it means the tests carry the *appearance* of cache
invalidation without exercising any.

### L-G — `docs/ARCHITECTURE.md` uses `.develcakes/enforcement.json`; the code uses `.crabcakes/`

**NOTE.** `docs/ARCHITECTURE.md:2423` and `:2436` (both touched by this fix round) say
`.develcakes/enforcement.json`. The code reads `os.path.join(project_path, ".crabcakes",
"enforcement.json")` (`agent/enforcement.py:497`). The rename is repo-wide and pre-existing
(the doc uses `.develcakes/prompts/` throughout), so this is **not** introduced here — but the
fix round edited exactly those two lines and left the wrong directory name in them.

---

## 5. Per-fix verdict

| Fix | Closes | Verdict | One line |
|-----|--------|---------|-----------|
| **1** | C1, M1, M2, M7 | **PARTIALLY CLOSED** | The `.git` walk and the root `.gitattributes`/`.gitignore` alias direction are closed in a **non-worktree** project (probe 1: v1–v9, v12 all closed), but the walk is **entirely inert in a linked worktree** (M-A) and the **subdirectory** `.gitattributes`/`.gitignore` alias direction is open (M-B). |
| **2** | C3, D1, D2, L1 | **CLOSED** | All 11 toolchain-file globs gate at **both** the tool and runtime gates, at any depth (probe 1 / probe 10b revert → 19 failed). The test files the tier *executes* are a separate class and are not gated (C-A). |
| **3** | C2 | **PARTIALLY CLOSED** | The **config** layer is closed — shipped `pytest.ini`/`setup.cfg`/`pyproject.toml` addopts and shipped `conftest.py` at every depth do **not** execute (probe 2: C2-a…C2-n all closed; revert → 8 failed). The **code** layer is open: the tier imports the project's own test file (C-A) and honours an explicit `-p` (C-B). The docstring at `:1673-1674` overstates the fix. |
| **4** | M3 | **CLOSED** | The mtime-restoration cache defeat is dead — the scan is uncached and the `os.utime` sequence still gates (probe 10b revert → 2 failed). No cache to defeat. |
| **5** | hardening | **CLOSED (as scoped), untested** | The final-component symlink swap **is** refused with ELOOP and the bytes stay on the approved inode (probe 5 B2). The leading-component swap is **still reachable** (M-C) — the docstring says so. **Zero test coverage** (L-B). |
| **6** | M1 | **CLOSED** | One derivation; the tool gate, the runtime gate and `bypass_approval` agree on all 7 shapes and 7 non-string inputs, and no consumer crashes (probe 9: 16/16 closed). `_sensitive_relpath` now returns `None` for a non-sensitive path — the only consumers are `runtime.py:2916` and `:2956`, both `None`-safe. |
| **7** | M5 | **CLOSED** | `npx` is gone from **both** the lint tier and the jest/vitest detection, and the lint tier's argv now goes through `_validate_test_command` (revert → 1 failed). The lint tier still executes a shipped `eslint.config.js` — but that is now Fix 2's *name* gate (the file itself is sensitive), so it is the accepted posture. |
| **8** | M6 | **NOT CLOSED** | The per-binary rules are real and refuse the bare spellings (`pytest -p`, `ruff --config`, `mypy --config-file`, `eslint --plugin`, `go run/generate/tool/env`, `go test -exec`), but they key on `base == "<binary>"` and the tier's own argv is `python3 -m pytest` — so **the rules are unreachable on the tier's own path**, and `extra_args` is never validated at all (C-B). **Zero test coverage** (L-C). |
| **9** | M7 | **PARTIALLY CLOSED** | Root-level `.gitattributes`/`.gitignore` aliases gate (probe 1 v4 closed). The **subdirectory** forms do not, and git executes a subdirectory `.gitattributes` (M-B). |
| **10** | D3 | **CLOSED** | The four kinds are documented with their exact semantics and the comment no longer claims "any depth" for prefixes. The implementation matches the comment. |
| **11** | D4 | **CLOSED** | The content-hash cache replaces the TTL — an edit takes effect on the next call, and the fail-soft paths (missing / malformed / directory) all return `None` and cache it (probe 5 D1–D4; revert → 2 failed). |
| **12** | D5 | **CLOSED (by PM ruling)** | Per the brief: documentation only, no behaviour change, **not reported as a finding**. The docs state the QUALITY-control posture in both `agent/enforcement.py` (header) and `docs/ARCHITECTURE.md`. |
| **13** | L2, L3 | **CLOSED** | The group-tool write surfaces are documented as by-design in the header, in `sensitive_reason`'s docstring, and in `docs/ARCHITECTURE.md` §3.21u. |
| **—** | **M6 (P1)** | **NOT CLOSED** | `extra_args` — a project-supplied string appended to the tier's pytest argv — is **never** passed through the token allowlist (C-B). This was not in the brief's fix table but is the live half of M6. |
| **—** | **D1a** | **STILL OPEN (PM ruling)** | The `python3` positional posture was never ruled. `_validate_test_command("python3 script.py")` → `True`. The tier reaches it via `test.command` (C-C, D-D). |

---

## 6. Refuted vectors (with the command that refutes them)

| # | Vector | Refuted by |
|---|--------|------------|
| R1 | `.git/hooks/pre-commit` alias — hard link | probe 1 → `[closed] C1-v1` — asked=1, payload_landed=False |
| R2 | `.git/info/exclude` alias | probe 1 → `[closed] C1-v2` |
| R3 | `.git/hooks/pre-commit` alias — **symlink** | probe 1 → `[closed] C1-v3` |
| R4 | root `.gitattributes` alias | probe 1 → `[closed] C1-v4` |
| R5 | `.git/worktrees/agent-x/config.worktree` alias | probe 1 → `[closed] C1-v5` |
| R6 | `.git/modules/sub/config` alias | probe 1 → `[closed] C1-v6` |
| R7 | `.git/logs/HEAD` alias | probe 1 → `[closed] C1-v7` |
| R8 | `.git/branches/legacy` alias | probe 1 → `[closed] C1-v8` |
| R9 | `.git/hooks/pre-commit` alias via a **deeper** plain path | probe 1 → `[closed] C1-v9` |
| R10 | `.git/config` alias (`core.hooksPath`) | probe 1 → `[closed] C1-v12` |
| R11 | The `objects/` **symlink** direction | probe 14 → `[closed] C` — the target path is caught by the resolved/lexical forms; asked=1 |
| R12 | The `objects/` hard link **without** a chmod | probe 14 → `[closed] A` — git object files are mode 444 → `Permission denied` |
| R13 | The corrupted object poisoning the PM's **diff card** | probe 21 → `[closed] O4` — `git diff` is empty; the corrupted blob was not in the index |
| R14 | The agent having a hard-link primitive | probe 21 → `[closed] O1` — the Coder shell refuses `ln`, `cp`, `chmod`, `git hash-object` |
| R15 | Shipped `pytest.ini` with `addopts = -p evilplugin` | probe 2 → `[closed] C2-a` — marker=False |
| R16 | Shipped `setup.cfg` with `[tool:pytest] addopts` | probe 2 → `[closed] C2-b` |
| R17 | Shipped `tests/conftest.py` | probe 2 → `[closed] C2-c` |
| R18 | Shipped `pyproject.toml` `[tool.pytest.ini_options] addopts` | probe 2 → `[closed] C2-h` |
| R19 | Shipped `conftest.py` in a **subdir** of the test | probe 2 → `[closed] C2-i` |
| R20 | Shipped root `sitecustomize.py` | probe 2 → `[closed] C2-m` |
| R21 | `pytest.ini` in a subdir | probe 2 → `[closed] C2-n` |
| R22 | `PYTEST_ADDOPTS` reaching the tier subprocess | probe 2 → `[closed] C2-f` — the scrubbed env drops it |
| R23 | `PYTEST_PLUGINS` reaching the tier subprocess | probe 2 → `[closed] C2-f/g` |
| R24 | `full_suite_command = "python3 <script>"` executing | probe 2 → `[closed] C2-j` — the field is dead (M-E); it is live only with `command` set |
| R25 | A foreign project posing as an app worktree | probe 2 → `[closed] C2-l` (the plain case) — `_is_app_worktree("/tmp/foreign") = False`; the symlink-into-`.worktrees` case **is** admitted but is documented in `_is_app_worktree`'s own docstring and is not agent-reachable (the sandbox is the project) |
| R26 | `extra_args = "-p evilplugin"` on its own (no `pytest.ini`) | probe 3 → `[closed] EA-a` — the framework is not detected without a `pytest.ini`/`setup.cfg`/`pyproject.toml`; probe 4 P5 is the same control |
| R27 | `full_suite_command = "pytest -p evilplugin"` | probe 3 → `[closed] EA-e`, probe 4 → `[closed] P4` — Fix 8 refuses the bare spelling |
| R28 | The lint tier executing a shipped `eslint.config.js` | probe 3 → `[closed] LINT-a` — the marker was not created; the shim's `node` fallback is not on PATH in the tier env, and Fix 2 gates the file by name |
| R29 | The `make test` runner executing a clone's Makefile recipe | probe 6, probe 7 → `[closed] M1` — the tier builds `make test <abs_test> -x -q --tb=short`; GNU make rejects `-x` and `--tb=short` **before** running any recipe (`make: invalid option -- 'x'`) |
| R30 | `--rootdir` re-enabling conftest loading (defeating `--noconftest`) | probe 20 → `[closed] R2` — marker=False with and without `--rootdir` |
| R31 | A poisoned **ordinary source file** executing | probe 16 → `[closed] P6` — the syntax tier uses `py_compile` (parse only, no import) |
| R32 | A poisoned **conftest** executing (the class Fix 3 targeted) | probe 15 → `[closed] T4` — `--noconftest` holds |
| R33 | The Coder's allowed shell running a poisoned test file | probe 15 → `[closed] T3` — the shell refusal fired first (`test_write_asked=1`) |
| R34 | Fix 6's `None`-returning contract crashing a consumer | probe 9 → 16/16 closed — `None`/`123`/`[]`/`{}`/`b"x"`/empty-project all return `None` |
| R35 | Fix 11's digest cache going stale | probe 5 → `[closed] D1` — an edit takes effect on the next call |
| R36 | Fix 11's fail-soft paths | probe 5 → `[closed] D2/D3/D4` — missing / malformed / a directory all return `None` and cache it |
| R37 | The Fix 4 walk stalling the tool loop | probe 5 → `[note]` — 500 walked entries → **4.4 ms** per gate call; 3000 refs → **13.0 ms**. No bound is enforced, but the measured cost is small |
| R38 | The `component` rule's precision | probe 11 → `[closed] V3b` — `notes/.git/hooks/pre-commit` matches, `x.git/hooksy` does not (the legacy `*hook*` glob does, separately — D-E) |

---

## 7. Test-suite state

**Fix-round suites are green.**

```
$ .venv/bin/python -m pytest tests/test_enforcement.py tests/test_spec66_approval_and_eval.py -q
228 passed, 1 skipped, 1 warning in 4.93s
```

Adding `tests/test_tools.py` adds 3 failures, all in
`TestToolSchemaSizeBudget::test_default_agent_tool_payload_under_budget`
(coder/debugger/supervisor) — **pre-existing**, proven by running the same test against a
pristine HEAD tree:

```
$ cd /tmp/head_tree && ... -m pytest tests/test_tools.py::TestToolSchemaSizeBudget -q
3 failed in 0.80s
```

**Full suite** (excluding the GTK suites that segfault without a display, in both HEAD and the
working tree):

```
6 failed, 3816 passed, 1822 skipped, 9 warnings in 190.13s
```

All 6 failures reproduce identically on a pristine HEAD tree
(`test_mvp_integration.py::TestN2CompiledWireAdjacency::test_hot_windows_byte_and_line_mode`,
`test_spec24_facts.py::test_no_new_tool_name`,
`test_spec40_wire_aware_guard.py` ×3, `test_timer_tool.py::test_delay_parser`) —
**pre-existing, not caused by the fix round.**

`tests/test_chat_surface.py` and `tests/test_spec66_live_js.py` segfault in GTK
(`ui/views/chat_surface.py:914`) — also reproduced on the pristine HEAD tree, so pre-existing.

### Test-vacuity analysis (probe 10b — in-process reverts)

Each fix was reverted **inside the pytest process** (a parent-process monkeypatch does not
reach a subprocess) and the corresponding suite re-run.

| Fix | Baseline | Reverted | Verdict |
|-----|----------|-----------|---------|
| Fix 1 — the `.git` alias walk | rc=0 (13 passed) | rc=1 (13 failed) | **REAL** |
| Fix 2 — the toolchain-file patterns | rc=0 (19 passed) | rc=1 (19 failed) | **REAL** |
| Fix 3 — the tests-tier isolation | rc=0 (12 passed) | rc=1 (8 failed, 4 passed) | **REAL** |
| Fix 4 — the uncached alias scan | rc=0 (2 passed) | rc=1 (2 failed) | **REAL** |
| **Fix 5 — `O_NOFOLLOW`** | rc=0 (103 passed) | **rc=0 (103 passed)** | **VACUOUS — untested** |
| Fix 7 — the lint tier | rc=0 (4 passed) | rc=1 (1 failed, 3 passed) | **REAL** |
| **Fix 8 — the flag grammar** | rc=0 (125 passed) | **rc=0 (125 passed)** | **VACUOUS — untested** |
| Fix 11 — the content-hash cache | rc=0 (4 passed) | rc=1 (2 failed, 2 passed) | **REAL** (partial — 2 of 4 tests do not fail on revert) |

**Fix 7 was re-verified per half** (probe 22) because the single revert masks one of the two:

```
  scope: tests/test_enforcement.py::TestConsolidatedFix7aLintTier::test_lint_pass_through_validate_test_command
    REVERT_FIX=fix7  (npx emitted again):              1 passed    <- MASKED (the test monkeypatches _detect_linter itself)
    REVERT_FIX=fix7b (token allowlist made a no-op):    1 failed    <- CAUGHT
  scope: tests/test_enforcement.py::TestConsolidatedFix7aLintTier
    REVERT_FIX=fix7:  1 failed, 3 passed
    REVERT_FIX=fix7b: 1 failed, 3 passed
```

**Both halves of Fix 7 are genuinely tested.** The `fix7` revert is invisible to
`test_lint_pass_through_validate_test_command` only because that test monkeypatches
`_detect_linter` itself — a property of the test, not a defect in the fix.

---

## 8. Ranked fix list (what remains)

**P0 — a code-execution chain with no approval card anywhere**

1. **Gate the test files the tier executes.** Add `("glob", "test_*.py")` and
   `("glob", "*_test.py")` to `_SENSITIVE_PATH_PATTERNS` (basename globs → any depth), **or**
   refuse to run the tests tier for a project whose test files are not app-approved. This
   closes C-A (both the clone-shipped and the agent-written variants) and is the single
   highest-value change on this list. *Note:* the pattern must be scoped — the tier also runs
   the app's own `tests/`, so the gate should fire on the **write**, not on the tier's read.
2. **Validate `test.extra_args`** through `_validate_test_command` (or a dedicated
   pytest-argv validator) before it is appended — `agent/enforcement.py:1662` and `:1666`.
   Closes the half of M6 that Fix 8 left open, and the C-B `extra_args` chain.
3. **Key Fix 8's rules on the MODULE, not the binary.** Normalise `python3 -m pytest` →
   `pytest` before the per-binary rules run, so the tier's **own** argv shape is covered.
   Extend the forbidden set with the flags the original M6 table listed and that are still
   admitted: `--rootdir`, `--import-mode`, `--pyargs`, `--collect-only`, `--no-eslintrc`,
   `node --env-file`.
4. **Close the `python3` positional posture** (D-D / C-C). Either rule it (the audit's
   recommendation: shrink `_ALLOWED_BINARIES` to `{"python3"}` and require `-m <module>` with
   no positionals) or record the PM ruling that positionals stay open — and then make the
   docstring match the ruling.

**P1 — the alias walk is still incomplete**

5. **Resolve the linked worktree's `.git` FILE** (M-A). Parse the `gitdir:` line and walk
   *that* directory (plus the shared `<main>/.git/hooks/`). This is the Coder's **default**
   working directory under SPEC-09 SP0, so the alias scan is inert exactly where the agent
   works.
6. **Walk the project root for git-executed config** (M-B) — or add the subdirectory forms to
   the name patterns (`("glob", ".gitattributes")`, `("glob", ".gitignore")` as basename globs
   rather than `("exact", …)`), so the **alias** direction into a subdirectory file gates the
   same way the root one does.
7. **Fix the `objects/` justification** (M-D). The hash-name argument is false — a hard link's
   plain name is arbitrary. Either walk `objects/` (bounded: `os.scandir` the 256 fan-out
   dirs, do not stat every object) or replace the comment with the **true** argument (the
   corruption vector, not the approval-bypass) and **amend `test_git_objects_stays_excluded`**
   so it stops pinning a false claim.

**P2 — hardening and hygiene**

8. **Bind the check to the use for leading components** (M-C): an `openat`-style walk from
   the project root, one component at a time, refusing symlinked components — the closure the
   original audit prescribed and Fix 5 explicitly deferred.
9. **Contain `naming_pattern` / `test_dir`** (M-F): reject a candidate that does not resolve
   inside the project, the same way `utils/git_ops.py:123-128` does for the accept path.
10. **Give Fix 5 and Fix 8 test coverage** (L-B, L-C). Both are currently untested, and Fix 8
    is where C-B lives.
11. **Fix `full_suite_command`** (M-E) — read it independently of `command`, or delete the
    field and its two validation call sites.
12. **Remove the dead `_ALLOWED_ENV_VARS`** in `agent/enforcement.py:100` (L-E) and the dead
    `_ALIAS_ENTRY_CACHE` name + its no-op calls in the new tests (L-F).
13. **Correct `.develcakes/` → `.crabcakes/`** in the two `docs/ARCHITECTURE.md` lines this
    round edited (L-G).
14. **Correct the Fix 3 docstring** at `agent/enforcement.py:1673-1674` — "never lets the
    CHECKED PROJECT choose which pytest code is loaded" is false for the test file (C-A) and
    for an explicit `-p` (C-B).

---

## 9. One sentence

**No — the sensitive-path gate surface is closed to the orientations the fix round enumerated,
but the enforcement tier still executes project-supplied code by design (the tier's own test
file, and an explicit `-p` that Fix 8's grammar cannot see), so this needs a design-level
change: the tier must not be pointed at project-supplied code, and the token allowlist must
be applied to every project-supplied argv fragment — not to two of the three.**

---

## 10. What is NOT in this report

- The seven closed reports from the original audit are not re-litigated, except where the
  fix round's changes bear on them (M4 → M-A, M2 → M-B/M-D, M6 → C-B/D-A).
- Fix 12 (the `skip_patterns` posture) is **not** reported as a finding, per the brief's
  explicit instruction (PM ruling option B, documentation only).
- **No product code was changed.** All findings come from probes under
  `.crabcakes/scratch/fixround/`; the product tree is byte-identical to the dispatch state.
- The probes need a Python with `pytest` importable for the tier to actually run it.
  `/tmp/spec66venv` is a real venv with pytest 9.2.0; the probes build a project-local
  `.venv` from it. The app venv (`.venv`) has no `pygments`, so a venv built by symlinking
  only the app venv's pytest packages fails to import.
- The HEAD baseline for comparison is a pristine export of `HEAD` at `/tmp/head_tree`
  (`git archive HEAD | tar -x -C /tmp/head_tree`). The repository's own working tree was
  never stashed, checked out, or otherwise altered.
- Windows/OneDrive path semantics remain **out of scope** (the alias/inode model assumes
  POSIX inode semantics).
- `mypy` and `eslint` are not installed on this host, so the Fix 8 rules for those binaries
  were verified by the validator's return value, not by execution.
