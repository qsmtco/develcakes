# SPEC-09 SP2 — Worktree Manager (v3 — post second pre-build probe; supersedes v1/v2)

**Spec:** SPEC-09 §2 + pre-flight D2, AS AMENDED by the Debugger's pre-build probe
(probes I/J, 2026-10-02) and the Supervisor's BLOCKING-3 ruling.
**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Depends on:** SP0 (6b3beb72), SP1 (19c680aa).

---

## RULINGS (read first — these supersede v1)

1. **BLOCKING-3 (a): PROJECT-PARAMETERIZED worktrees.** `WorktreeManager(repo_path)`
   where repo_path = the ACTIVE PROJECT's path (ARH resolves it). Worktrees at
   `<project>/.worktrees/<worktree_id>`. The SP0 gate's app-worktree membership is
   a SELF-HOST special case only: when the active project IS the app repo, gate
   benefits apply; a foreign project's worktrees get no app benefits (correct —
   they never wanted them). Both branches pinned by tests.
2. **BLOCKING-2: session→worktree-id derivation is explicit:**
   `worktree_id = session_key.replace(":", "-")` (the LOW-2 filesystem sanitization
   already used for workspaces — `special:coder` → `special-coder`). Validation
   then rejects anything NOT matching `[a-zA-Z0-9._-]+` after substitution.
3. **BLOCKING-1: explicit dot-segment rejection.** After substitution, reject
   `worktree_id in (".", "..")` and any id containing `/` or `\0` — ValueError.

## Edit 1 — `utils/worktree_manager.py` (NEW, v3 — post second pre-build probe)

**Probe-corrected mechanics (Debugger probes K/L/M; these supersede naive git):**
- **ensure is list-first:** check `git worktree list --porcelain` (GitPython 3.1.62
  has NO `Repo.worktrees` attr — parse the porcelain output) BEFORE any add;
  existing → return path. Second-add rc128 is a bug, not idempotence.
- **Revival after external rm -rf:** stale registration + existing branch both
  break naive add (rc128/rc255). Correct sequence: `git worktree prune` first,
  then branch-existence check → add WITHOUT `-b` when the branch exists (reattach),
  WITH `-b` when new.
- **Non-git repo_path:** ctor does NOT raise — degrade to a disabled manager
  (ensure/path_for return None; list {}); log once. `_prepare_turn_conversation`
  must never crash for a writer+lease on a non-git project (B3, the
  never-crash-project-open contract).
- **.gitignore append is per-line exact:** a line reading `.worktrees` (or
  `.worktrees/`) — NOT substring match (`.worktrees-old/` must not satisfy it).

```python
class WorktreeManager:
    """One git worktree per writer agent IN A GIVEN REPO (the active project).
    Self-host note: when repo_path IS the app repo, worktrees land under the
    app's .worktrees and gain SP0 gate membership; foreign projects' worktrees
    are ordinary git worktrees with no app-venv benefits (by design)."""

    def __init__(self, repo_path: str):   # realpath-normalized once; NON-GIT → disabled manager (B3: ensure/path_for → None, list → {}, log once — NEVER raises; the never-crash-project-open contract)
    def ensure_worktree(self, worktree_id: str) -> str   # REAL path; branch agent/<worktree_id>; idempotent
    def path_for(self, worktree_id: str) -> str | None
    def remove_worktree(self, worktree_id: str) -> None
    def list_worktrees(self) -> dict[str, str]
    @staticmethod
    def worktree_id_for_session(session_key: str) -> str  # ':'->'-' + validate (BLOCKING-1/2)
```

- `.worktrees/` MUST be added to the repo's `.gitignore` (create/append) at ensure
  time if not present — worktrees are never committed (MED-1).
- Realpath discipline unchanged (SP0's BUG#14 obligation stands).
- External deletion → ensure recreates (document branch reuse semantics).

## Edit 2 — ARH integration (v2)

At `_prepare_turn_conversation` (the seam is `_active_project`,
agent_runtime_handler.py:369; writer = `agent_def.can_write`):
- Resolve the ACTIVE PROJECT path via the existing mechanism (how ARH learns the
  project today — do not invent; if ARH has a project/active-project attr or
  getter, use it; report the exact seam you used).
- Writer + live lease → `conv.project_path = WorktreeManager(active_project).
  ensure_worktree(worktree_id_for_session(sk))`. Manager cached per project path
  (a small dict on ARH; managers are cheap but not free).
- **Lease-expiry reset (MED-3):** writer WITHOUT a live lease whose conv.project_path
  points at a worktree → RESET to the project's normal path (a worktree without a
  lease is a stale claim; the agent must not keep editing there).
- Non-writers unchanged.

## Edit 3 — tests (v2; tmp git repos)

WorktreeManager (per v1's 8, adapted): creation+branch+realpath; idempotent;
absent/remove/external-deletion; **BLOCKING-1 trio** (`'..'`, `'.'`, `a/b` →
ValueError); **session derivation** (`special:coder` → `special-coder`, valid;
`'..'` raw rejected); two agents distinct; symlinked-repo realpath discipline;
**gitignore append** (ensure → `.worktrees` in .gitignore; existing content
preserved).

ARH (per v1's 4, adapted): writer+lease → worktree under the ACTIVE PROJECT;
non-writer unchanged; **expired-lease reset** (stale worktree path reset to
project); **the D2 pin, both branches**: app-repo project → `_is_app_worktree
(worktree)` True; tmp FOREIGN project → `_is_app_worktree(worktree)` False (and
that is correct-by-design — assert False, not error).

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
xvfb-run -a .venv/bin/python -m pytest tests/test_worktree_manager.py -v
xvfb-run -a .venv/bin/python -m pytest tests/test_agent_runtime_handler.py tests/test_enforcement.py -q
.venv/bin/python -m pyright utils/worktree_manager.py ui/handlers/agent_runtime_handler.py
python -m ruff check utils/worktree_manager.py ui/handlers/agent_runtime_handler.py tests/test_worktree_manager.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] Edit 1: v2 manager (project-parameterized; session-id derivation; dot-segment rejection; gitignore append; realpath discipline)
- [ ] Edit 2: ARH via the ACTIVE PROJECT seam (named in report) + lease-expiry reset
- [ ] Edit 3: BLOCKING-1/2 tests + D2 both-branch pin + gitignore + expired-reset
- [ ] Full battery green incl. pyright + full suite
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
