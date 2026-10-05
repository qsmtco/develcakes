# SPEC-11 Pre-flight Decisions (Supervisor, 2026-10-04)

Spec: `docs/specs/SPEC-11-RENAME-MIGRATION.md` (dated 2026-09-20; verified against
HEAD 195426fc). This spec's footprint is the largest sweep in the MVP line: 131 py
files carry `crabcakes` strings, 69 test files reference `.crabcakes`, 13 read
`CRABCAKES_*` envs. **The spec's own claims have drifted — verification notes below.**

## The operational hazard the spec's premise missed (BLOCKING-1)

Ruling #1 (architecture, 2026-09-20): "`<repo>/.crabcakes/` keeps its name until v2
self-hosts — **v1 is the build host and reads `.crabcakes/`**. Rename lands in this
work unit, last in the MVP line."

**Verified:** this loop runs *inside* CrabCakes v1, whose agents read/write
`<repo>/.crabcakes/` — `context.md` (this file), `work.json` (the `/work` system
reading `docs/specs/phases/`), the review layer's `.crabcakes/feed.json`,
awareness.json, prompts, tmp/probes. SPEC-09's post-mortem even registered
"worktree .gitignore for scratch/probe dirs" as repo hygiene.

**If we `git mv .crabcakes .develcakes` now**, the PM's own `/work`, `/review`,
context memory, and the agents' spec-reading (phase instructions live in
`docs/specs/` — safe) break mid-loop, and every running v1 session loses its state
dir on next write. **Ruling BLOCKING-1: the repo state-dir rename is DEFERRED.**
The MVP line completes with `.crabcakes/` intact; the rename becomes the first
post-MVP unit (v2 self-hosted, launched by its own `develcakes` launcher, reading
`.develcakes/`). What ships NOW: everything else — package/app-id/config-dir/env
divergence + migration. The `.crabcakes/` → `.develcakes/` code sweep is PREPARED
(see D6) but not executed.

## Spec-claim drift found in verification

### GAP-1 — Env vars: spec says "verified var list: 3". Reality: 7+
`CRABCAKES_PROJECTS_DIR` (utils/config.py:46), `CRABCAKES_DEBUG` (main.py:14 +
feed_handler.py:1175), `CRABCAKES_MIGRATE_STORE` (main.py:30, runtime.py:116,572),
`CRABCAKES_INCLUDE_DOCS` (context.py:260), `CRABCAKES_WEB_FETCH_RESTRICT`
(tools.py:883), `CRABCAKES_ACTIVE_PROJECT_PATH` (ui/wiring.py:22,
event_cards.py:67), `CRABCAKES_NO_WEBKIT` (chat_surface.py:44). The spec's
"verified: both are read only in main.py" is wrong. **Ruling D2:** every site
moves to `DEVELCAKES_*` with old-name fallback read (one release), centralized
via a helper (D2b) — no site left on the old name.

### GAP-2 — Fresh-install path: the wizard lives in the v1-config absence, but
migration must not fire when BOTH dirs are absent. Spec §7 row 1 covers this;
adding the marker check ordering: migration runs BEFORE any config read that
would create the new dir (get_config_dir does NOT create — verified — but
ensure_dirs callers might). **Ruling D3:** `migrate_v1_config()` runs once at app
start (main.py, before window build); it is a no-op when the marker exists OR the
new dir already has content (both-dirs-divergent case: new wins, no migration).

### GAP-3 — audit-log path: `agent/audit.py` writes into the config dir
(verified: `~/.config/crabcakes/audit-log.jsonl`). Migration's copy list must
include it — spec §2 already lists `audit-log.jsonl`. Confirmed present.

### GAP-4 — Icon/desktop: `icons/` has PNGs named by size (16..256.png); the
desktop file — **none found in-repo** (spec claims `com.develcakes.app.desktop`
as "new file"). Icons install via pyproject `[tool...]?` — verify at build time;
if no desktop file exists today, SP3 creates one NEW (not a rename).
**Ruling D5:** desktop entry created fresh (`com.develcakes.app.desktop` under
`data/`), icons stay size-named (no `develcakes.png` rename needed —
`set_default_icon_name('develcakes')` refers to the *icon theme name*; we keep
the PNG set and add the theme name mapping if the packaging installs one).
Actually — `Gtk.Window.set_default_icon_name` expects an installed theme icon;
the in-repo PNGs are app-bundled. **Ruling D5 revised:** icon handling = keep
bundled PNGs; `set_default_icon_name('develcakes')` only if we ship an installed
theme icon (packaging step, out of scope) — else keep loading the bundled PNG
via `set_default_icon` (file-based). Builder verifies which mechanism main.py
uses today and preserves it under the new name.

## Decisions

### D1 — Package/app identity (final)
`pyproject.toml`: `name = "develcakes"`, `[project.scripts] develcakes =
"main:main"` (old `crabcakes` entry REMOVED — spec explicit). `main.py`:
`DevelcakesApp`, `application_id='com.develcakes.app'`. Docstring/comment sweep
in main.py. Console-script rename means the venv's `crabcakes` entry disappears
on reinstall — acceptable (v2 is develcakes now).

### D2 — Env-var divergence with fallback (helper-centralized)
New helper in utils/config.py:

```python
def get_env(name: str) -> str | None:
    """DEVELCAKES_<name> if set, else CRABCAKES_<name> (one-release fallback).

    Reads the NEW name first; the old name is honored only when the new one is
    unset — never mixed. Sites must not read os.environ directly for these.
    """
    new = os.environ.get(f"DEVELCAKES_{name}")
    if new is not None:
        return new
    return os.environ.get(f"CRABCAKES_{name}")
```

All 7 sites (GAP-1 list) route through it. `CRABCAKES_MIGRATE_STORE` →
`DEVELCAKES_MIGRATE_STORE` (main.py's setdefault + runtime's reads +
conftest/test fixtures that set it — 13 test files flagged). Fallback horizon:
one release; removal is a post-MVP chore registered in the post-mortem.

### D2b — The bool/flag variants
`get_env` returns str|None; sites doing `== "1"` / truthiness keep their
comparison at the call site (`get_env("DEBUG")` truthy check unchanged in
semantics). No new parsing layer.

### D3 — Migration contract (utils/config.py) *(REV 2 — post-audit, 2026-10-04)*
`migrate_v1_config() -> dict | None` per spec §2, plus:
- **Ordering:** called from main.py BEFORE the window build; runs before any
  config-file read that could mutate state.
- **No-op guards (REV 2):** marker file exists OR new dir contains a
  copy-list entry that is a FILE or a NON-EMPTY directory → return None. An
  EMPTY directory is not content — migration proceeds (the copy's
  makedirs(exist_ok=True) absorbs it; the both-dirs case is about content
  divergence, and an empty placeholder carries nothing to diverge). Guard
  order: v1-absent → v1-unreadable (failed report, never the marker) →
  marker → content. **REV 3 (closure audit):** the empty-dir carve-out
  applies ONLY to directory copy-list names; ANY entry (of any type) at a
  FILE/DB name is content → no-op. **REV 3 (symlinked subdirs):** the walk
  FOLLOWS symlinked subdirectories (matching the file-following behavior) —
  a user's `archive -> /store` organizes data they expect migrated. Cycle
  guard: a visited-realpath set fails the migration closed on revisit
  (followlinks=True alone can hang on `a/link -> a`; a cycle is pathological
  — failing loudly beats hanging or silently looping).
- **Copy list:** agent.json, providers.yaml, config.json, conversations/,
  agents/, audit-log.jsonl, projects/, transcript.db (+ -wal/-shm sidecars if
  present), feed-prefs.json if present at config level. **transcript.db is
  load-bearing** (SPEC-08: conversations live there; the JSON conversations/
  dir is the pre-store era) — byte-count verify, never open the DB.
- **Verify:** byte-counts per file post-copy; dirs recursed with per-file
  counts. Any mismatch → delete the partial new-dir CONTENTS (rollback),
  report failure, app continues fresh.
- **Non-destructive:** v1 dir untouched, always.
- **Returns:** `{"copied": [...], "skipped": [...], "failed": [...]}` or None.
- **Banner:** feed card + chat notice via the SPEC-02 pattern; failure reports
  what failed and that v1 is untouched.
- **Second run:** marker present → None, no banner (AC#3).

### D4 — Config-dir divergence
`get_config_dir()` → `$XDG_CONFIG_HOME/develcakes` else `~/.config/develcakes/`.
No creation in the getter (unchanged). `get_projects_dir()` reads via get_env
(D2). Docstrings swept (`~/.config/crabcakes` → `develcakes` mentions).

### D5 — Icons/desktop (revised per GAP-4)
Desktop entry: NEW file `data/com.develcakes.app.desktop` (spec's "installed
name change" — no existing file to rename; verify no other packaging path).
Icons: builder verifies main.py's current icon mechanism and preserves it under
the `develcakes` name; no PNG renames unless the mechanism requires them.

### D6 — Repo state-dir rename: PREPARED, NOT EXECUTED (BLOCKING-1)
- **Now:** `.crabcakes/` stays. Zero code changes to state-dir paths.
- **Prepared:** a `docs/specs/phases/SPEC-11-STATE-DIR-DEFERRED.md` note
  recording the deferral + the exact sweep list (the 15+ `.crabcakes` path
  sites verified: work_persistence, persistence tmp, context docs/prompts,
  feed_store prefs, agent/config staging dirname, models/team docstring,
  runtime comment, ui/wiring env name — post-MVP unit runs this list).
- **Rationale recorded in ARCHITECTURE.md** at close-out: v1-host hazard.

### D7 — Tests
New `tests/test_config_migration.py` (~200 lines): copy/verify/marker/
non-destructive/fresh-install/both-dirs/partial-failure-rollback (RED-first).
Env-fallback tests in a new `tests/test_env_divergence.py` (or appended where
natural): new-wins, old-fallback, neither → default. Existing suites keep
passing (they set `CRABCAKES_MIGRATE_STORE` etc. — old names still work via
fallback; the 13 test files do NOT need edits this unit; sweep them post-MVP
with the fallback removal).

### D8 — Phasing
- SP1: `migrate_v1_config()` + tests (utils/config.py only).
- SP2: config-dir + env divergence (utils/config.py get_env + 7 sites) + tests.
- SP3: identity (pyproject, main.py, desktop, icons) + docs sweep.
- SP4: close-out (ARCHITECTURE.md, deferred-rename note, post-mortem, battery).

### D9 — README/docs sweep scope
README badges/titles to develcakes; **lineage section kept** (spec explicit);
post-mortems/specs/proposals history files NEVER touched (they are records).
`grep -rin crabcakes README.md` → prose-only mentions stay where they describe
v1 lineage.

### D10 — Naming
`DevelcakesApp` (main.py), `migrate_v1_config` (utils/config.py), `get_env`
(utils/config.py), `DEVELCAKES_*` env family, `com.develcakes.app` app id,
`data/com.develcakes.app.desktop`. No renames of user-facing files beyond these.

## Standing verification commands

- `python -m pytest tests/test_config_migration.py tests/test_env_divergence.py -q`
- Full battery at SP4 (nohup, ~14 min).
- ruff/pyright vs measured baselines on every touched file.
- Launcher smoke: `pip install -e .` → `develcakes` on PATH → app id check
  (SP3; may need the venv reinstall — disclose if the venv entry lags).
