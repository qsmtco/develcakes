# SPEC-11: Rename Divergence + Config Migration

**Date:** 2026-09-20
**Author:** Supervisor (develcakes v2)
**Status:** Draft — for implementation
**Implements:** .crabcakes/architecture.md §Modules/Config & identity;
docs/proposals/DEVELCAKES-V2-CHANGE-LIST.md §0 rename footprint
**Depends on:** SPEC-10 (last in the MVP line — v2 self-hosts before the state-dir rename)
**Target branch:** main

> Architecture compliance: ruling #1 — `<repo>/.crabcakes/` keeps its name until v2
> self-hosts; the rename lands HERE, in this unit, last.

---

## 1. Overview

**Problem.** The v2 identity is still v1's: package `crabcakes`, app id
`com.crabcakes.app`, config dir `~/.config/crabcakes/`, state dir `<repo>/.crabcakes/`.
Two apps sharing those paths fight over files.

**Solution.** Full divergence in one unit:
1. Package/launcher/app-id/desktop-entry → `develcakes` / `com.develcakes.app`;
2. `~/.config/develcakes/` with **one-time copy-with-verify migration** from
   `~/.config/crabcakes/` + first-run banner;
3. `<repo>/.crabcakes/` → `<repo>/.develcakes/` (safe now: v2 is self-hosted at this
   point in the line);
4. Env-var rename: `CRABCAKES_*` → `DEVELCAKES_*` (back-compat read of old vars for one
   release).

**Scope**

| In | Out |
|---|---|
| pyproject/main.py/app-id/desktop/icons rename | GitHub repo rename (URL stays) |
| utils/config.py path divergence | v1 maintenance |
| Config migration + banner | Secrets re-entry (keys copy across) |
| .crabcakes/ → .develcakes/ (repo state dir) | |
| docs/README sweep | |

## 2. Changes by File

### pyproject.toml

`name = "develcakes"`; `[project.scripts] develcakes = "main:main"` (console script
renamed; old `crabcakes` entry dropped). Dep list unchanged (nh3 landed with SPEC-06).

### main.py

`application_id='com.develcakes.app'`; `Gtk.Window.set_default_icon_name('develcakes')`;
class rename `CrabcakesApp → DevelcakesApp`; `CRABCAKES_DEBUG → DEVELCAKES_DEBUG`
(read old var as fallback for one release — verified both are read only in main.py).

### utils/config.py

`get_config_dir()` → `~/.config/develcakes/` honoring `$XDG_CONFIG_HOME`; all
`CRABCAKES_*` env vars → `DEVELCAKES_*` (fallback read: old name if new unset —
verified var list: CRABCAKES_PROJECTS_DIR, CRABCAKES_GATEWAY_URL (dies with SPEC-05
anyway), CRABCAKES_DEBUG in main.py). New helper:

```python
def migrate_v1_config() -> dict | None:
    """One-time copy-with-verify from ~/.config/crabcakes to develcakes.

    Copies: agent.json, providers.yaml, config.json, conversations/, agents/,
    audit-log.jsonl, projects/. Verifies byte-counts post-copy. Non-destructive:
    v1 dir untouched. Returns report {copied: [...], skipped: [...]} or None
    if nothing to migrate. Guarded by marker file <new>/MIGRATED_FROM_V1.
    """
```

Marker file prevents re-migration. First-run banner = feed card (SPEC-02 pattern) +
chat notice listing what moved.

### Desktop entry / icons

`com.develcakes.app.desktop` (new file, installed name change); icon renamed
`develcakes`; README badges/titles swept (`grep -rin crabcakes README.md` → prose-only
mentions, lineage section kept deliberately).

### Repo state dir

`.crabcakes/` → `.develcakes/`: utils-level constant + git mv of the live dir + sweep of
code references (`grep -rn "\.crabcakes" --include="*.py"` — includes
`review_staging_dirname`, scratch paths, feed/work/prompt paths in utils/config.py and
handlers). The **desktop file env** (`GIO_LAUNCHED_DESKTOP_FILE`,
`CRABCAKES_PROJECTS_DIR` in the running app's environ — verified) updates with the new
launcher.

## 3. Data Flow

First v2 launch → `migrate_v1_config()` → copy+verify → marker file → banner card →
app runs fully on `~/.config/develcakes/`. Repo state writes land in `.develcakes/`.

## 4. File Change Summary

| File | Change | ~Lines | Risk |
|---|---|---|---|
| pyproject.toml / main.py | rename | ~30 | low |
| utils/config.py | divergence + migration | +120 | med |
| desktop/icons/README | sweep | ~60 | low |
| repo state dir move + sweep | ~15 files | ~80 | med |
| tests/test_config_migration.py | new | ~180 | — |

## 5. Implementation Order

1. `migrate_v1_config()` + tests (copy/verify/marker/non-destructive).
2. config-dir + env-var divergence (new names, old fallback).
3. App identity (main.py, pyproject, desktop, icons).
4. Repo state dir move + code sweep.
5. Docs sweep; full suite + ruff + pyright.

## 6. Acceptance Criteria

- [ ] `develcakes` launcher boots the app with new app id
- [ ] First run migrates v1 config: conversations, agents, providers, audit log all
      present in new dir; v1 dir untouched; banner reports the move
- [ ] Second run: no re-migration (marker); no banner
- [ ] `.develcakes/` state dir; zero `.crabcakes` path references in code
- [ ] Old `CRABCAKES_*` env vars still honored for one release (fallback)
- [ ] Full pytest green, ruff clean, pyright clean

## 7. Edge Cases

| Case | Behavior |
|---|---|
| v1 config dir absent (fresh install) | No migration; straight to wizard (SPEC-04 behavior) |
| Partial copy failure (disk full) | Atomic-ish: verify fails → rollback new dir → banner reports failure, app continues with fresh config |
| Both dirs exist with divergent content | v2 dir wins (already migrated marker or content); v1 never overwritten |
| User still runs v1 app concurrently | Migration is copy (not move) — v1 keeps working on its own dir; document "don't run both against one repo" |
| `.crabcakes/` gitignored history | `git mv` preserves; .gitignore entry updated to `.develcakes/` |

## 8. ARCHITECTURE.md Updates

§Modules/Config & identity — mark implemented; record final paths + env-var names.
