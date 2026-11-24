# CONSOLIDATED Adversarial Audit — Path / Approval / Enforcement Surface

**Date:** 2026-11-23
**Auditor:** Debugger (code pass)
**Baseline:** `main` @ `b12feb74` (clean) — every finding below was reproduced by execution against this tree
**Brief:** `.crabcakes/debugger_audit_briefs/2026-11-23-CONSOLIDATED-PATH-APPROVAL-AUDIT-BRIEF.md`
**Probes:** `.crabcakes/scratch/spec66audit/` (16 probe files, `run_all.sh`, per-probe output in `out/`)
**Scope:** `agent/tools.py`, `agent/enforcement.py`, `agent/runtime.py` (gates only), `ui/feed/review.py` (accept path), `utils/git_ops.py`, `utils/env_security.py`, plus the group-tool write surfaces.

---

## 0. Verdict

| Severity | Count |
|---|---|
| **CRITICAL** | **3** |
| **MAJOR** | **7** |
| **MEDIUM** | **5** |
| **LOW / NOTE** | **3** |
| **TOTAL FINDINGS** | **18** |
| Refuted vectors (documented with commands) | 20 |

Two of the three CRITICALs are **complete, end-to-end chains with no approval card anywhere in them**, and one of those two is triggered by a *cloned repository's own committed files* — the agent only has to write an ordinary source file.

The seven closed reports are **not re-litigated**. All seven are confirmed still closed (see §5). The pattern the brief identified is confirmed and is now quantified: **the deny-list-of-orientations design is not enumerable** (§6).

---

## 1. CRITICAL findings

### C1 — `.git/hooks/` alias → zero-approval write → git executes the poisoned hook

**Severity: CRITICAL.** Arbitrary code execution as the user running the app, reached with **no approval card at any gate**, and fired by the app's *own* accept path.

**Root cause.** `agent/tools.py:690-691`:

```python
_SENSITIVE_ALIAS_DIRS = (".crabcakes", ".git", ".github")
_GIT_ALIAS_SUBDIRS = ("refs",)
```

`.git/` is walked at the **top level only**, plus `refs/`. `.git/hooks/` is not walked, so a hook file is invisible to the alias scan.

**Reproduction** (`probe_a2_git_walk.py::probe_git_hooks`, `probe_i1_detail.py::probe_i1_detail`, `probe_i1d_accept.py::probe_i1d_accept_triggers_hook`, `probe_final.py::final_hook_chain`):

```
$ .venv/bin/python .crabcakes/scratch/spec66audit/probe_a2_git_walk.py
[*** OPEN ***] A2b .git/hooks/pre-commit (hooks/ NOT in the walk)
          NO approval asked (the plain name is not sensitive and the
          sensitive side is OUTSIDE the walk). success=True err=None
          source now = 'PAYLOAD\n'
```

Full chain, from a clean state (`probe_final.py`):

```
  step1 write: approval_asked=[] success=True
  step2 runtime gate fires: False (rel='notes/plainfile.txt')
  step3 accept commit: success=True sha=f39eb57fe06335388577f3596d055a9cdd75e41e
  step4 hook executed: True
[*** OPEN ***] FINAL-1 hooks-alias chain
```

The chain in plain terms:

1. `os.link(<project>/.git/hooks/pre-commit, <project>/notes/plainfile.txt)` — a hard link, one inode, two names.
2. The agent calls `write_file` on `notes/plainfile.txt`. The resolved form (`notes/plainfile.txt`) and the lexical form (`notes/plainfile.txt`) are both non-sensitive. The alias scan does not visit `.git/hooks/`. **No approval is requested.**
3. The write lands on the hook's inode. `.git/hooks/pre-commit` now contains the agent's bytes.
4. The PM clicks Accept on the diff card. `ui/feed/review.py:72` calls `stage_paths`, `:146` calls `commit` — `git commit` runs `pre-commit`.
5. The agent's script executes.

Notes:
- The hook retains mode `0755` across the write — the hard link preserves it, so git runs it directly.
- `git commit` returns 0 and reports success; nothing surfaces.
- The poisoning is **local to the clone** (`.git/` is never tracked), so this is a local RCE, not a supply-chain one. Probed and documented: `probe_i1d_accept.py::probe_i1e_leaked_into_commit`.
- The **runtime gate does not fire either** — see M1. There is no approval card anywhere.

**Second instance of the same root cause, also CRITICAL-adjacent:** the same write can poison any `.git/hooks/*` file (`pre-commit`, `post-commit`, `prepare-commit-msg`, `post-merge`, `post-checkout`, `pre-push`, `pre-receive`, `update`, …). `prepare-commit-msg` in particular fires on **every** commit.

**Fix.** See Fix 1 in §7.

---

### C2 — A cloned repository's own `pytest.ini` / `setup.cfg` → the enforcement tier executes arbitrary code on the agent's first ordinary write

**Severity: CRITICAL.** The strongest vector in the audit: **no agent-authored file, no exec_command, no approval card, no agent-authored config.** The repository ships the payload; the app executes it.

**Root cause, two halves.**

1. `agent/tools.py:360-373` — `_SENSITIVE_PATH_PATTERNS` does not include `pytest.ini`, `setup.cfg`, `tox.ini`, `conftest.py`. The files the **enforcement tier itself reads** are ungated.
2. `agent/enforcement.py:1255-1461` — `_check_tests` runs `python3 -m pytest <file>` with `cwd=project_path` and **no `-c`/`--confcutdir`/`-p no:…` isolation**. pytest therefore honours the *project's* `pytest.ini`, including `addopts`.

**Reproduction** (`probe_m_cloned_repo.py::probe_m1_shipped_pytest_ini_addopts`, `::probe_m2_shipped_setup_cfg_addopts`, `::probe_m1b_why_the_plugin_was_importable`):

```
  the agent's write: asked=[] success=True err=None
  TIER syntax passed=True detail=Syntax check passed for feature.py
  TIER tests passed=True detail=tests/test_feature.py: passed
[*** OPEN ***] M1 SHIPPED pytest.ini with addopts=-p <plugin> -> code executes
                on the agent's first write
          the agent's ordinary write asked for approval: False
          marker exists (plugin executed) = True
```

`M2` is identical with `setup.cfg`:

```
[tool:pytest]
addopts = -p evilplugin
```

Why `-p evilplugin` resolves: pytest inserts the rootdir into `sys.path` when it collects a rootdir `conftest.py`; here the shipped `conftest.py` is at the root, so `evilplugin` at the root is importable. Confirmed by `M1b`.

The tier reported **PASSED** — the enforcement tier's success output is indistinguishable from a clean run.

**Reproduction with no plugin at all** (`probe_m3_detail.py::probe_m3b_conftest_next_to_test`): a shipped `tests/conftest.py` is imported by pytest → arbitrary code, same result.

**Fix.** See Fixes 2 and 3 in §7.

---

### C3 — The agent writes `conftest.py` with zero approval → the enforcement tier imports and runs it on the next write

**Severity: CRITICAL.** This is C2's payload delivered by the agent instead of the clone — and it needs no `pytest.ini` trick, no plugin, and no project venv trickery beyond "pytest is importable".

**Root cause.** `conftest.py` is not in `_SENSITIVE_PATH_PATTERNS`, and pytest imports every `conftest.py` on the collection path.

**Reproduction** (`probe_m3_detail.py::probe_m3c_agent_writes_conftest_next_to_test`, `probe_e6_conftest_rce.py::probe_conftest_rce_with_project_venv`):

```
  agent write of tests/conftest.py: asked=[] success=True
  TIER syntax passed=True detail=Syntax check passed for feature.py
  TIER tests passed=True detail=tests/test_feature.py: passed
[*** OPEN ***] M3c AGENT-WRITTEN tests/conftest.py -> executed on the next write
                with NO approval on the conftest write
          approval asked for the conftest write = []
          marker exists (conftest executed) = True
```

`probe_e6_conftest_rce.py` shows the same for a **root-level** `conftest.py` written by the agent (marker written by the tier's own pytest run).

The two-step shape — plant the conftest, then write anything — is exactly what an agent does naturally when it needs a fixture. The approval gate never fires because `conftest.py` is not on the list.

**Fix.** See Fix 2 in §7.

---

## 2. MAJOR findings

### M1 — The runtime gate misses the same aliases the tool gate misses

`agent/runtime.py:460-507` `_sensitive_relpath` delegates its third disjunct to `agent/tools.py:775` `_sensitive_alias_of`, which relies on the same incomplete walk. Therefore the runtime gate — the one that fires the approval card — is blind to every alias outside the walked subtrees.

```
$ .venv/bin/python .crabcakes/scratch/spec66audit/probe_j_runtime_feed.py
[*** OPEN ***] J1a runtime gate catches the .git/hooks alias
          _sensitive_relpath -> 'notes/plainfile.txt'; is_sensitive_path -> False
[*** OPEN ***] J1b runtime gate catches the .git/objects alias
          _sensitive_relpath -> 'notes/plainfile.txt'; is_sensitive_path -> False
[*** OPEN ***] J1c runtime gate catches the .gitattributes alias
          _sensitive_relpath -> 'notes/plainfile.txt'; is_sensitive_path -> False
```

Consequence: `agent/runtime.py:2978-2982` computes `bypass_approval` from the same `rel_path`, so the runtime is *internally consistent* — it consistently does not gate. There is no approval card for C1.

**Fix.** Fixed transitively by Fix 1 (complete the walk) + Fix 6 (single source of truth).

### M2 — The `.git` walk covers the top level and `refs/` only; seven more subtrees are invisible

`agent/tools.py:702-715` `_alias_scan_roots`. Reproduced individually with neutral plain names (an earlier run reported a false positive on `.git/hooks/` because the plain filename contained the substring `hook` and matched the `*hook*` glob — corrected in `probe_a2_git_walk.py`, which uses `notes/plainfile.txt` and asserts `is_sensitive_path(plain) is False` before the write):

| Sensitive location | In the walk? | Result |
|---|---|---|
| `.git/config`, `.git/HEAD`, `.git/packed-refs`, `.git/description` | yes (top level) | **closed** — gated |
| `.git/refs/heads/main` | yes (recursed) | **closed** — gated |
| `.git/hooks/pre-commit` | **no** | **OPEN** (C1) |
| `.git/info/attributes` | **no** | **OPEN** — git-executed config |
| `.git/objects/ab/cdef` | **no** | **OPEN** — object store |
| `.git/modules/sub/config` | **no** | **OPEN** — submodule git dir |
| `.git/worktrees/agent-x/config.worktree` | **no** | **OPEN** — linked-worktree config |
| `.git/logs/HEAD` | **no** | **OPEN** — reflog (history rewrite) |
| `.git/branches/legacy` | **no** | **OPEN** — legacy branch config |

```
$ .venv/bin/python .crabcakes/scratch/spec66audit/probe_a2_git_walk.py
[*** OPEN ***] A2b .git/hooks/pre-commit (hooks/ NOT in the walk)
[*** OPEN ***] A2d .git/info/attributes (info/ NOT in the walk)
[*** OPEN ***] A2e .git/objects/ab/cdef (objects/ NOT in the walk)
[*** OPEN ***] A2f .git/modules/sub/config (modules/ NOT in the walk)
[*** OPEN ***] A2g .git/worktrees/agent-x/config.worktree (worktrees/ NOT in the walk)
[*** OPEN ***] A2i .git/logs/HEAD (logs/ NOT in the walk)
[*** OPEN ***] A2j .git/branches/legacy (branches/ NOT in the walk)
[closed] A2a .git/config (top level, IN the walk)
[closed] A2h .git/refs/heads/main (refs/ IS in the walk)
```

The `objects/` exclusion is deliberate and documented in the code ("content-addressed hash names"), and the reasoning is sound — but `info/`, `hooks/`, `worktrees/`, `modules/`, `logs/`, `branches/` are all **name-aliasable and behaviour-bearing**, and none of them are excluded on any documented grounds.

### M3 — The alias-scan cache is defeatable by restoring the walked directory's mtime

`agent/tools.py:718-728` `_alias_signature` keys the cache on `(mtime_ns, dev, ino)` per visited directory. `os.utime(dir, ns=(old, old))` restores the mtime, the signature is unchanged, and the cache keeps serving the stale entry list — so the alias is invisible.

**Reproduction** (`probe_h_toctou.py::probe_mtime_restoration_cache_bypass`):

```
  warm entries: 1
[*** OPEN ***] H2a alias cache bypass by restoring the walked dir's mtime
          signature unchanged after utime: True
          entries before=1 after=1
          _sensitive_alias_of(plain) = None (None means the alias is INVISIBLE)
[*** OPEN ***] H2b end-to-end: write through the mtime-hidden alias
          approval asked=[] success=True err=None
          the aliased sensitive file now = 'PAYLOAD\n'
```

This is the **executable form of the brief's TOCTOU question**: the check and the use are not bound together, and the cache widens the window from "microseconds" to "indefinitely".

Refuted sub-cases (all correctly invalidate): adding an entry (`F5a`), renaming into the dir (`F5b`), hardlinking into the dir (`F5c`), and a sensitive dir *appearing* after warm-up (`L5a`). Only mtime *restoration* defeats it.

### M4 — A linked worktree's `.git` is a FILE, so the alias scan sees nothing at all

`<project>/.git` in a linked worktree is a one-line text file (`gitdir: …`). `_alias_scan` calls `os.scandir` on it, which raises `NotADirectoryError` → `OSError` → the `except` at `agent/tools.py:749-750` returns silently. The walk yields **zero entries** for that project.

```
[*** OPEN ***] A2m linked-worktree .git FILE: worktree gitdir alias
          NO approval asked. success=True.
          config.worktree now = '[core]\n\thooksPath=.\n'
```

This matters for this project specifically: SPEC-09 SP0 puts agents in `<repo>/.worktrees/<agent_id>`, so the Coder's own working directory is a linked worktree. The alias scan is **entirely inert** in the Coder's default working directory.

### M5 — The lint tier runs `npx eslint …`; SPEC-66 §11.2's "npx is removed" decision was never applied to it

`agent/enforcement.py:1502-1508` `_detect_linter` returns `("eslint", ["npx", "eslint", file_path])`. `_check_lint` (`agent/enforcement.py:1537-1628`) **never calls `_validate_test_command`** — the token allowlist is applied to the tests tier only (3 call sites, confirmed by counting occurrences in `_check_tests`).

```
[note] I5a _detect_linter returns npx argv for a JS file
          detected = ('eslint', ['npx', 'eslint', 'src/a.js'])
[*** OPEN ***] I5b the resolved-binary gate admits the npx lint argv
          allowed=True detail=/home/mushy/.local/bin/npx
[*** OPEN ***] I6a the lint tier applies the token allowlist (_validate_test_command)
          _check_lint body mentions _validate_test_command: False
```

`npx` resolves into the trusted HOME root, so `_validate_resolved_binary` admits it. `npx` can **download and execute a package from the npm registry**. SPEC-66 §11.2 removed `npx` from `_ALLOWED_BINARIES` with the explicit note "Do not re-add it" — the lint path was missed.

### M6 — `_validate_test_command` covers two of the seven allowlisted binaries

`agent/enforcement.py:67-69` allows `python3, pytest, ruff, mypy, eslint, node, go`. `agent/enforcement.py:218-302` applies eval-flag rules to **python3 and node only**. Every other binary's config-loading flags are unrestricted.

`probe_d_flags.py` — 28 OPEN, 48 CLOSED, 1 NOTE. The OPEN set (all confirmed by execution against the live validator):

| Binary | Unrestricted flags that load or execute code |
|---|---|
| `pytest` (first token) | `-p <plugin>`, `--rootdir`, `--import-mode=importlib`, `--collect-only` |
| `python3 -m pytest` | `--pyargs`, `-p <plugin>`, `--rootdir`/`--confcutdir` |
| `python3` | `-m pip install …` |
| `python3 <script>` | positional script — the PM ruling the brief flagged as open is still open (**D1a**) |
| `ruff` | `--config <file>` |
| `mypy` | `--config-file`, `--python-executable`, `--shadow-file` |
| `eslint` | `--rulesdir`, `--plugin`, `--config` |
| `go` | `run`, `generate`, `test`, `run -exec`, `test -exec`, `tool compile`, `env -w`, `run /dev/stdin` |
| `node` | `--env-file=<file>` (the `--experimental-loader` / `--import` spellings happen to be caught by the *positional* rule, not the flag rule) |

The `go` set is the sharpest: `go generate` and `go run` execute code by definition, and `-exec` substitutes the binary that runs the test.

### M7 — Root `.gitattributes` and `.gitignore` are git-executed config outside the alias scan's sensitive set

`_SENSITIVE_ALIAS_DIRS` is `(".crabcakes", ".git", ".github")` — the project **root** is not walked at all, so a root-level config file has no alias scan.

```
[*** OPEN ***] A2k root .gitattributes alias
          NO approval asked. success=True. .gitattributes now = '*.py filter=pwn\n'
[*** OPEN ***] A2l root .gitignore alias
          NO approval asked. success=True. .gitignore now = 'x\n'
```

`.gitattributes` is git-executed config: a `filter=`/`diff=` line names a **command git runs** when a matching file is checked out or diffed. Note that `.gitattributes`' own name *is* sensitive (leading-dot basename) — but its **alias** is not, which is the whole point of the alias scan.

---

## 3. MEDIUM findings

### D1 — `eslint.config.js` is plain JavaScript that eslint executes, and it is ungated

Not a dotfile, not in the glob list:

```
[*** OPEN ***] E5 writing eslint.config.js asks for approval
          NO approval asked (not a dotfile, not in the glob list). success=True
```

### D2 — Five more tier-read files are ungated

`probe_e_sensitive_set.py` enumerates every file the enforcement tier reads and marks each gated/ungated:

```
  UNGATED  conftest.py          pytest IMPORTS conftest.py -> arbitrary code
  UNGATED  tests/conftest.py    pytest IMPORTS conftest.py -> arbitrary code
  UNGATED  pytest.ini           pytest config: rootdir/ini options
  UNGATED  setup.cfg            pytest + mypy config
  UNGATED  mypy.ini             mypy config
  UNGATED  tox.ini              tox config
  UNGATED  package.json         jest/vitest detector reads it
  UNGATED  eslint.config.js     eslint config — plain JS, executed by eslint
  UNGATED  requirements.txt     pip installs it
  UNGATED  noxfile.py           nox imports it
  UNGATED  sitecustomize.py     imported by EVERY python (if on sys.path)
  UNGATED  usercustomize.py     imported by every python
```

(`ruff.toml` / `.ruff.toml`, `Makefile`, `pyproject.toml`, `.eslintrc`, `.github/**`, `.git/**`, `.crabcakes/**` **are** gated.)

### D3 — The sensitive-path prefix rules are root-anchored, contradicting their own comment

`agent/tools.py:361-362` says "path components sensitive at any depth", but the implementation is `norm.startswith(pattern)` — root-anchored.

```
$ .venv/bin/python .crabcakes/scratch/spec66audit/probe_l_precision.py
  OK  sensitive=True  .crabcakes/enforcement.json          root .crabcakes
  OK  sensitive=False sub/.crabcakes/enforcement.json      NESTED .crabcakes
  OK  sensitive=False sub/.git/config                      NESTED .git
  MISMATCH sensitive=True  sub/.github/workflows/ci.yml     NESTED .github
```

`sub/.github/…` is gated only because the basename `ci.yml` matches the `*.yml` glob — not because of the prefix. Nested `.crabcakes/` and `.git/` are **not** sensitive. Whether that matters depends on whether any consumer reads a nested `.git/`; git does not. But the comment is wrong, and a future reader will trust the comment.

### D4 — The enforcement loader's 30-second TTL serves a stale `enforcement.json` after a write

`agent/enforcement.py:407` `_ENFORCEMENT_CONFIG_TTL = 30.0`; `:449-477` `_load_project_enforcement_config`.

```
[note] H4a enforcement loader TTL cache serves a stale file
          first={'syntax_check': True} second={'syntax_check': True}
```

Swapping the file's contents does not take effect for up to 30 seconds. Fail-open in the sense that the tier keeps running the *old* configuration.

### D5 — A project-supplied `skip_patterns` silences all three tiers

`agent/enforcement.py:1701-1718` merges the project's `skip_patterns` additively over the global list.

```
[note] K6a project-supplied skip_patterns disables the syntax tier
          checks = [('syntax', True, 'SKIPPED: x.py matches skip pattern (syntax tier)'),
                    ('tests',  True, 'SKIPPED: x.py matches skip pattern (tests tier)'),
                    ('lint',   True, 'SKIPPED: x.py matches skip pattern (lint tier)')]
```

By design, and the project's own `skip_patterns` is a legitimate feature. But it means the enforcement tiers **cannot be relied on as a security control against a hostile project** — which is exactly the role §G of the brief hoped they played.

---

## 4. LOW / NOTE findings

### L1 — `sitecustomize.py` / `usercustomize.py` are ungated (conditionally reachable)

```
[*** OPEN ***] L1a sitecustomize.py in the project root is AUTO-imported by the
                interpreter when cwd is on sys.path
          with PYTHONPATH -> True; cwd-only -m -> False
[closed] L1b sitecustomize.py auto-imported by `python -m pytest` with cwd=project
          marker exists = False
```

`python -c` with the project root on `PYTHONPATH` imports it. The **scrubbed env drops `PYTHONPATH`** (`utils/env_security.py:26-35`), so the enforcement tier is *not* reachable through this today. Refuted for the `python -m pytest` cwd-only case: `site` runs before `sys.path[0]` is set for `-m`. Still worth gating.

### L2 — Agent-controlled transcript content is written into `.crabcakes/` with no approval

`agent/persistence.py:132` — `os.replace(tmp, path)`, atomic write of the conversation transcript under the project's `.crabcakes/`. The path is the app's own; the *content* is agent-controlled. By design, but it is an agent-controlled write into a `.crabcakes/` path that never passes the sensitive-path gate, and it is not mentioned in the gate's threat model.

### L3 — The group-tool write surfaces are outside the gate entirely

The brief's scope list did not include them. `diary` → `utils/project_awareness.append_project_context` writes `.crabcakes/context.md` with agent content and no approval; `write_file` has a *special append-only branch* for that same file (`agent/tools.py:871-886`). `project_memory` was probed for path escape and **refuted** (see §5, R15); `feed_card`'s `file` field is **validated** as project-relative (R16).

---

## 5. Refuted vectors (with the command that refutes them)

These are recorded so the next round does not re-walk them.

| # | Vector | Refuted by |
|---|---|---|
| R1 | `.git/config` alias | `probe_a2_git_walk.py` → `[closed] A2a .git/config (top level, IN the walk)` — top level **is** walked |
| R2 | `.git/refs/heads/main` alias | `[closed] A2h` — `refs/` **is** recursed |
| R3 | `.git/HEAD`, `.git/packed-refs`, `.git/description` alias | `probe_l_precision.py` → `[closed] L3a` ×4 — top level is walked |
| R4 | **Shipped** symlink `.crabcakes/enforcement.json → plain target` (the clone ships the alias) | `probe_g_shipped.py` → `[closed] G1a` — the **lexical** form `.crabcakes/…` is sensitive (BUG #2's dual check) |
| R5 | Shipped **dangling** `.crabcakes/enforcement.json` symlink | `[closed] G2` — same lexical check |
| R6 | `.crabcakes` / `.git` / `.github` is **itself** a symlink to a plain dir | `[closed] G4`, `[closed] A9`, `[closed] A10`, `[closed] G5`, `[closed] G6` — `os.scandir` follows a symlinked *root*, and the target-path compare in `_sensitive_alias_of` catches the plain name inside it |
| R7 | `stage_paths` with a path escaping the project | `probe_f_accept.py` → `[closed] F1a` — `utils/git_ops.py:123-128` refuses it |
| R8 | `stage_paths` through a symlink out of the project | `[closed] F1b` — same check, realpath-based |
| R9 | The accept path's `others` refusal (a foreign staged file swept into the commit) | `[closed] F2a` / `[closed] F2b` — `ui/feed/review.py:123-135` correctly lists `others=['foreign.txt']` |
| R10 | **Commit-message injection via a newline in a filename** | `probe_f_accept.py` → `[closed] F3a` — GitPython's diff parser truncates the path at the newline; the resulting commit body was `'Accept: 1 files (evil)\n'` — a single line, no injection |
| R11 | `card.file_path` staging a *different* file than the card names | `[closed]/[note] J3a` — git stages the symlink **as a symlink entry**; the target's content is not what is staged |
| R12 | Env-var execution vectors (`PYTEST_ADDOPTS`, `PYTHONPATH`, `PYTHONSTARTUP`, `NODE_OPTIONS`, `MYPY_CONFIG_FILE`, `RUFF_CONFIG`, `GOFLAGS`, `GOENV`, `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, `BASH_ENV`, `ENV`, `LD_PRELOAD`, `PYTHONWARNINGS`, `COVERAGE_PROCESS_START`) | `probe_d_flags.py` → `[closed] D9b` ×15 — **the scrubbed env is clean**: the allowlist is exactly `{PATH, HOME, LANG, LC_ALL, LANGUAGES, TZ, TMPDIR, PWD}` (`utils/env_security.py:26-35`) |
| R13 | `shlex` oddities (`-c'x'`, `-ic`, `-c=x`, `-''c`, `--config`, quoted binary, unbalanced quote, `-m x -c y`) | `[closed] D7a`–`[closed] D7j` — the prefix matcher + the cluster matcher are both correct |
| R14 | A path-qualified binary (`/usr/bin/python3 -c x`, `pytest.exe`) | `[closed] D8a`, `[closed] D8c` — `os.path.basename` strips the path; `.exe` is not allowlisted |
| R15 | `project_memory` `name` escaping the memory root (`../../../../tmp/…`) | `probe_k_group_tools.py` → `[closed] K2a` — the name is slugified; `../../../../tmp/spec66audit_escape` became `tmp-spec66audit-escape.md` inside the memory root |
| R16 | `feed_card`'s `file` field escaping the project | `probe_k_group_tools.py` → `[note] K3a` — validated as a project-relative path by `agent/group_tools.py` |
| R17 | The coder role disabling the tests tier | `probe_m_cloned_repo.py` → `[closed] M4` — tiers run: `['syntax', 'tests']`. SPEC-66's fix is live |
| R18 | `sitecustomize.py` auto-imported by `python -m pytest` with cwd-only | `[closed] L1b` — `site` runs before `sys.path[0]` is set for `-m` |
| R19 | The alias cache failing to invalidate on add / rename / hardlink / sensitive-dir-appears | `[closed] F5a`, `[closed] F5b`, `[closed] F5c`, `[closed] L5a` — only *mtime restoration* defeats it (M3) |
| R20 | A sensitive name whose symlink target escapes the project | `probe_g_shipped.py` → `[note] G3` — the sandbox refusal (`Path escapes project sandbox`) fires **before** the approval gate, so the write is blocked. Note: no approval *card* is shown for this case, but the write does not happen |

**Two probe corrections worth recording, because they nearly produced false findings:**

- **A1 was a false positive.** The first run of `probe_a_git_walk.py` reported `.git/hooks/*` as *closed* — because the plain filename `notes/hookfile` contains the substring `hook` and matched the `*hook*` glob in `_SENSITIVE_PATH_PATTERNS`. `probe_a2_git_walk.py` replaces it with `notes/plainfile.txt` and **asserts** `is_sensitive_path(plain) is False` before writing. The hook vector is open.
- **M3 was a false negative.** The first run of `probe_m_cloned_repo.py` reported the shipped root `conftest.py` as *closed* — because the marker was not created, as the test lived in `tests/` and pytest did not collect the root `conftest.py` for it. `probe_m3_detail.py` places the conftest next to the test (`M3b`, open) and tests the agent-written case (`M3c`, open).

---

## 6. The design question: enumerable, or design-level change?

**Answer: the current design is not enumerable, and a design-level change is required. The deny-list of orientations cannot be completed.**

This is not a guess; it is the arithmetic of the seven closed reports plus the eighteen findings here.

**1. The unit of protection is wrong.** Each of the seven closed reports found a new *spelling* of a path, a flag, an identity, or an error path. The gate's unit is "does this string look sensitive?" — and there are unbounded spellings of unbounded strings. Fixing a spelling closes one orientation and leaves the family open. That is precisely the seven-report pattern.

**2. The alias scan is an attempted inversion, and inversion does not scale.** To answer "does any sensitive name point at my destination?", the gate must enumerate the sensitive names. It therefore inherits: (a) an **incomplete enumeration** (M2: seven `.git` subtrees; M7: the project root; M4: linked worktrees make it zero), (b) a **mutable tree** (the check and the use are not bound — M3 makes the window indefinite), and (c) a **cache** that an adversary can defeat by restoring an mtime. Each of these is another orientation.

**3. The token allowlist is the same mistake one level down.** `_ALLOWED_BINARIES` is a list of seven binaries, and `_validate_test_command` writes per-binary flag rules for two of them (M6). The flag grammar of `go`, `pytest`, `eslint`, `ruff`, and `mypy` is unbounded and versioned.

**4. The deepest problem is the one the brief's §G gesture pointed at, but stopped short of.** The brief said the enforcement loader's validator "is the last line". It is not. **The last line is the enforcement tier, and the tier executes project-supplied code by design** (C2, C3). A deny-list of *paths* cannot defend against a program that reads configuration files — because the set of configuration files a program reads is that program's own property, not ours. `pytest` reads `pytest.ini`, `setup.cfg`, `tox.ini`, `conftest.py`, `pyproject.toml`, and every `-p`-loadable plugin. `eslint` reads `eslint.config.js` (and *executes* it). `go` reads `go.mod` and runs `go:generate` directives. `ruff` reads `ruff.toml`. **There is no fixed list.** We would be maintaining a moving target for every tool the tier supports.

### What the design-level change should be

Two inversions, both cheap, both structural:

**Inversion 1 — from a deny-list of sensitive paths to an allow-list of what the agent may write.**
The question stops being *"is this path sensitive?"* and becomes *"is this path one the PM has authorised for this project?"*. Concretely: the agent may write only to source paths (a narrow extension allow-list) plus paths the PM has explicitly approved; **everything else** requires approval. This makes the unknown default *ask*, not *allow* — which is the only posture that survives a list we cannot enumerate.

**Inversion 2 — from "run the project's toolchain" to "run the project's toolchain under our configuration".**
The tier must not let the project choose what it loads. Run pytest with an **app-owned** config (`-c <app-owned ini>`), `--confcutdir` pointing at an app-owned directory, and the plugin autoload disabled (`-p no:cacheprovider -p no:randomly` plus `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` in the scrubbed env). Same for `go` (drop `generate`; refuse `-exec`), `eslint` (refuse `--rulesdir`/`--plugin`; prefer the project's own config only if it is not JS), `ruff`/`mypy` (refuse `--config`/`--config-file`/`--shadow-file`). **The project's configuration is data; the tool's *loading* of code is ours to control.**

**Optional hardening, if Inversion 2 is rejected:** the tier refuses to run at all for a project whose `.crabcakes/enforcement.json` has not been approved by the PM, and the PM's approval of a project is one click that says "this repo's config may drive the tier".

**On the brief's specific suggestion (`O_NOFOLLOW` / exclusive opens):** it is worth doing but it is **not** the answer to the dominant finding. `O_NOFOLLOW` + exclusive opens fix the *final-component* symlink case (which BUG #2 already handles at the name level) and it hardens the check→use binding (M3). It does **not** address C2/C3, because the problem there is not a symlink — it is that the program under test reads configuration that we never gated. Nor does it address M2/M4 (the enumeration is incomplete) or M6 (the flag grammar is unbounded). It should be done as defence-in-depth, in Fix 3 and Fix 5 below, not treated as the design answer.

---

## 7. Ranked fix list

Ordered by (exploitability × blast radius) ÷ cost. Each is a delegation-sized unit.

### P0 — ship now

**Fix 1 — Complete the `.git` alias walk.** *(closes C1, M1, M2, M7)*
- Extend `_GIT_ALIAS_SUBDIRS` (`agent/tools.py:691`) to cover `hooks`, `info`, `worktrees`, `modules`, `logs`, `branches`. `objects/` may stay excluded, but only if the exclusion is justified: an object file is content-addressed and its *name* is a hash, so a working-tree file cannot be name-aliased to one — but note a hard link *out of* `objects/` is a real alias, so either walk it or exclude it with the hash-name argument written down.
- Walk the **project root** for the git-executed config files (`.gitattributes`, `.gitignore`) — or add them to `_SENSITIVE_ALIAS_DIRS` handling as root-level entries.
- Add `.git/hooks/*` to `_SENSITIVE_PATH_PATTERNS` as a prefix, so the *name* is sensitive on its own and the alias scan is a second line, not the only line.
- **Regression test:** pin the full C1 chain — hard link, write the plain name, assert an approval was requested *and* that the hook's bytes did not change.

**Fix 2 — Gate the files the toolchain imports.** *(closes C3, D1, D2, L1)*
- Add to `_SENSITIVE_PATH_PATTERNS`: `conftest.py` (at any depth — the current pattern set has no "any depth" mechanism, see D3), `pytest.ini`, `setup.cfg`, `tox.ini`, `mypy.ini`, `eslint.config.js`, `package.json`, `requirements*.txt`, `noxfile.py`, `sitecustomize.py`, `usercustomize.py`.
- Fix the "any depth" mismatch (D3) at the same time: either implement it or correct the comment.
- **Regression test:** assert that writing `conftest.py`, `tests/conftest.py`, `pytest.ini`, and `eslint.config.js` each request approval at the tool gate *and* at the runtime gate.

**Fix 3 — Isolate the enforcement tier from the project's configuration.** *(closes C2)*
- Run the tests tier with an **app-owned** pytest config: `-c <app-owned ini>`, `--confcutdir=<app-owned dir>`, and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` added to the scrubbed env.
- Add `PYTEST_DISABLE_PLUGIN_AUTOLOAD` to `ALLOWED_SUBPROCESS_ENV_VARS` (it is a *suppression* variable, so allowing it is safe and it is the correct direction).
- **Regression test:** ship a `pytest.ini` with `addopts = -p evilplugin` and a shipped `conftest.py`, then assert that after an ordinary write the marker file does **not** exist.

### P1 — next round

**Fix 4 — Make the alias scan's cache non-defeatable.** *(closes M3)*
- Replace the `(mtime_ns, dev, ino)` signature with a check that cannot be restored cheaply, **or** — cheaper and simpler — **re-stat the destination's inode at open time** and re-verify against a fresh (uncached) scan of the sensitive set for that one destination. The latter is O(1) for the common case and removes the cache from the security decision entirely.
- Cheapest acceptable alternative: drop the cache for `.git/` (small) and give `.crabcakes/` a 1-second TTL.
- **Regression test:** the `os.utime` restoration sequence must still gate.

**Fix 5 — Bind the check to the use.** *(hardening; pairs with Fix 4)*
- Open the destination with `O_NOFOLLOW | O_CREAT | O_EXCL` semantics (or an `openat`-style walk from the project root, one component at a time, refusing symlinked components) so the bytes land on the inode that was checked.
- This is the `O_NOFOLLOW` suggestion from the brief — worth doing, but as hardening, not as the fix for C2/C3.

**Fix 6 — One source of truth for the sensitive set.** *(closes M1)*
- `_sensitive_relpath` (`agent/runtime.py:460`) and `_write_file`/`_edit_file` (`agent/tools.py:848`, `:1065`) each re-derive the gate decision. Extract a single `sensitive_reason(project_path, path_arg) -> str | None` and have both call it. Today the runtime's `bypass_approval` (`agent/runtime.py:2978`) is derived from the same value as the approval, so the two cannot disagree — but they can both be *blind*, and they are.
- **Regression test:** assert the tool gate and the runtime gate return the same sensitive name for a representative corpus (including every alias shape in Fix 1).

**Fix 7 — Apply the token allowlist to the lint tier and drop `npx`.** *(closes M5)*
- `_detect_linter` (`agent/enforcement.py:1507`) must not emit `npx`. Use a resolved `eslint` path or drop the JS lint tier.
- `_check_lint` must call `_validate_test_command` on its argv, exactly as `_check_tests` does.

**Fix 8 — Extend the command validator to every allowlisted binary.** *(closes M6)*
- Either write per-binary flag rules for `pytest`, `ruff`, `mypy`, `eslint`, `go` (refusing `--config`, `--config-file`, `--rulesdir`, `--plugin`, `-p`, `--python-executable`, `--shadow-file`, `run`, `generate`, `-exec`, `tool`, `env -w`), **or** shrink `_ALLOWED_BINARIES` to `{"python3"}` and require the `-m pytest` form with no positionals and no plugin flags.
- The shrink is the better trade: one binary, one grammar, one set of rules. It also gives the PM the ruling the brief asked for on the python3 positional posture (D1a) — with the allowlist shrunk to `python3 -m <module>`, positionals disappear by construction.

### P2 — hardening and hygiene

**Fix 9 — Root `.gitattributes`/`.gitignore` handling.** *(M7; can ride with Fix 1)*
**Fix 10 — Correct the "any depth" comment or implement it.** *(D3; can ride with Fix 2)*
**Fix 11 — Reduce or invalidate the enforcement TTL.** *(D4)* — 30 s of stale `enforcement.json` after a write. A content hash in the cache key (read the file, hash it, compare) closes it at the cost of one read.
**Fix 12 — Decide the `skip_patterns` posture.** *(D5)* — either refuse project-supplied `skip_patterns` for an unapproved project, or document explicitly that the tiers are a *quality* control and not a security control against a hostile repository.
**Fix 13 — Document the group-tool write surfaces.** *(L2, L3)* — `diary` → `.crabcakes/context.md`, `project_memory` → `.crabcakes/memory/`, `agent/persistence` → the transcript. All are by design; none is in the gate's threat model.

### P3 — note, no action required

**L1** — `sitecustomize.py` is only reachable via `PYTHONPATH`, which the scrubbed env drops. Gate it anyway (free, inside Fix 2).

---

## 8. Probe inventory

All under `.crabcakes/scratch/spec66audit/`. Run with the app venv: `.venv/bin/python <probe>.py`. `run_all.sh` runs all of them and writes per-probe output to `out/`.

| Probe | Covers |
|---|---|
| `probe_lib.py` | shared helpers, the `verdict()` printer, the summary |
| `probe_a2_git_walk.py` | the `.git` walk's completeness (A2a–A2m), with the neutral-filename correction |
| `probe_g_shipped.py` | the shipped-symlink vectors for the three sensitive dirs (G1–G6) |
| `probe_d_flags.py` | the flag grammars for all seven allowlisted binaries + the scrubbed env (D1–D10) |
| `probe_e_sensitive_set.py` | the sensitive-set coverage of tier-touched files (E1–E5) |
| `probe_e6_conftest_rce.py` | the agent-written root `conftest.py` → tier execution (E6) |
| `probe_f_accept.py` | the accept path: staging, the `others` refusal, commit-message injection, the cache mtime (F1–F6) |
| `probe_h_toctou.py` | the mtime-restoration cache bypass, the atomic-write inventory, the loader TTL (H1–H4) |
| `probe_i_chains.py` | the end-to-end chains (I1–I6) |
| `probe_i1_detail.py` | the hook chain with git identity configured (I1b, I1c) |
| `probe_i1d_accept.py` | the hook chain **through the app's accept path** (I1d, I1e) |
| `probe_j_runtime_feed.py` | the runtime gate's blindness, the feed-supplied card path (J1–J4) |
| `probe_k_group_tools.py` | the group-tool write surfaces (K1–K6) |
| `probe_l_precision.py` | sitecustomize, the "any depth" mismatch, the `.git` top level (L1–L5) |
| `probe_m_cloned_repo.py` | the cloned-repo chains (M1–M4) |
| `probe_m3_detail.py` | the conftest placements, with the M3 false-negative correction (M3b, M3c, M1b) |
| `probe_final.py` | clean-state re-verification of the two headline chains (FINAL-1, FINAL-2) |

**Environment note:** the probes need a Python with `pytest` importable for the tier to actually run it. `/tmp/spec66venv` is a real venv with pytest 9.2.0; the probes build a project-local `.venv` from it. The app venv (`/home/mushy/projects/develcakes/.venv`) has no `pygments`, so a venv built by symlinking only the app venv's pytest packages fails to import — the probes use the real venv instead.

---

## 9. What is NOT in this report

- The seven closed reports were read, verified still closed, and are not re-litigated (§5 covers the refutations).
- No product code was changed. All findings are from probes under `.crabcakes/scratch/spec66audit/`; the product tree is untouched.
- Windows/OneDrive path semantics are **out of scope** (the brief asked for this to be stated explicitly): `os.path.commonpath` raises `ValueError` on different drives, which the code catches and treats as fail-closed, but the whole alias/inode model assumes POSIX inode semantics and was not tested on Windows.
- The `Object`/feed-supplied card path was probed but the acceptance surface (`ui/feed/review.py`) was not exercised through the GTK event loop — the probes drive `git_ops.stage_paths` / `commit` directly, which is the same code the accept path calls.
