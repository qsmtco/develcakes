# utils/config.py — Centralized configuration path resolution
#
# Manifest: reads environment variables only, no file I/O, no network
# Single source of truth for all config and data directory paths.
#
# Architecture: this module is intentionally dependency-free. No GTK, no network.
# Any package that needs a config path should call helpers from here instead
# of computing paths inline. If the config root location ever changes, update
# this module and all callers are automatically correct.

import os
import shutil


def get_config_dir() -> str:
    """Return the develcakes config directory.

    Respects $XDG_CONFIG_HOME if set, otherwise ~/.config/develcakes.
    Does NOT create the directory.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return os.path.join(xdg, "develcakes")
    return os.path.join(os.path.expanduser("~"), ".config", "develcakes")


def get_v1_config_dir() -> str:
    """The v1 (crabcakes) config dir — migration SOURCE only, never written.

    Used exclusively by migrate_v1_config(). The v1 app keeps owning this
    directory; nothing in develcakes reads it for runtime config.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return os.path.join(xdg, "crabcakes")
    return os.path.join(os.path.expanduser("~"), ".config", "crabcakes")


# Marker file name: written inside the NEW config dir on full migration
# success; its presence makes migrate_v1_config() a permanent no-op.
MIGRATION_MARKER = "MIGRATED_FROM_V1"


# The one-time migration copy list (D3): bare file names inside the config
# dir, and directories that are recursed with per-file byte verification.
_MIGRATION_FILES = (
    "agent.json",
    "providers.yaml",
    "config.json",
    "audit-log.jsonl",
    "feed-prefs.json",
)
_MIGRATION_DB_FILES = ("transcript.db", "transcript.db-wal", "transcript.db-shm")
_MIGRATION_DIRS = ("conversations", "agents", "projects")


def _copy_file_verified(src: str, dst: str) -> None:
    """Copy one regular file, then byte-count verify. Raises on any mismatch
    or IO error; the caller tracks rollback.

    BUG#5: the verify reference is len(data) — the bytes WE READ — not a
    re-read of the source, which a live writer may have mutated between our
    read and our verify (a false mismatch strands the migration). What we
    read is what we wrote IS the integrity invariant.
    """
    if os.path.islink(dst):
        raise OSError(f"copy destination is a symlink — refusing: {dst}")
    with open(src, "rb") as fh:
        data = fh.read()
    with open(dst, "wb") as fh:
        fh.write(data)
    if os.path.getsize(dst) != len(data):
        raise OSError(f"byte-count mismatch after copy: {src} -> {dst}")


def _resolved_chain_has_cycle(src: str, dirpath: str) -> str | None:
    """Realpath of each component from src to dirpath; return the first
    repeated real path (a true cycle) or None.

    BUG#1 (round 3): the round-2 GLOBAL visited-realpath set false-positived
    legitimate DAGs — a diamond (`a -> store`, `b -> store`) arrives at the
    store's realpath twice and the global set aborted the whole migration.
    Chain-scoped detection instead asks: does THIS dirpath's ancestor chain,
    resolved component by component from the copy-list source, repeat a real
    path? A shared target reached via two DIFFERENT links (diamond) or a
    linear chain (`a -> b -> c -> store`) produces distinct linear chains —
    no repeat within either — and is NOT a cycle. A true cycle (self-link or
    mutual `a -> b -> a`) repeats within its own chain and is caught.
    """
    chain = []
    cur = os.path.realpath(src)
    chain.append(cur)
    rel = os.path.relpath(dirpath, src)
    if rel == ".":
        return None
    for part in rel.split(os.sep):
        cur = os.path.realpath(os.path.join(cur, part))
        if cur in chain:
            return cur
        chain.append(cur)
    return None


def _copy_dir_verified(src: str, dst: str, forbidden_root: str) -> None:
    """Recursively copy a directory with per-file byte verification.

    BUG#1 (round 1): the destination is created for EVERY walked level
    (relpath '.' handled explicitly) — not only inside `for d in dirnames:`,
    which silently required a nested/ subdir to exist and broke the real
    FLAT v1 shape (`FileNotFoundError .../conversations/./s1.json`).

    BUG#3 (round 1): os.walk onerror rethrows — an unreadable SUBDIR inside
    a readable v1 becomes the copy failure it is, never a silent skip.

    BUG#6 (round 1, dir half): a symlinked DESTINATION DIRECTORY is
    refused — makedirs(exist_ok=True) would otherwise follow the link and
    copy the whole tree OUTSIDE the config dir.

    BUG#1 (round 2, D3 REV 3): symlinked SUBDIRECTORIES are FOLLOWED
    (followlinks=True — matching the file-following behavior; a user's
    `archive -> /store` organizes data they expect migrated), with a
    CHAIN-SCOPED cycle guard (round 3): each walked dirpath's ancestor
    chain from the source is resolved component-by-component; a repeat
    WITHIN one chain raises, so `a/link -> a` and mutual `a -> b -> a`
    fail the migration closed instead of hanging or silently looping,
    while diamonds and linear chains (legitimate DAGs) pass.

    BUG#4 (round 4): a symlink pointing INTO the destination being written
    (`conversations/loop -> <new config dir>`, dangling at plant time, valid
    once migration creates the dir) drove self-amplifying recursion — the
    chain guard cannot fire because every descended dirpath is genuinely
    new (94.8s runaway to ENAMETOOLONG, ~600 nested dirs). Forbidden-root
    rejection: any walked dirpath whose realpath IS `forbidden_root` (the
    new config dir) or lands under it raises — bounded, fires on FIRST
    reentry, covers link-into-own-dst / into-the-config-root / into-another-
    entry's-dst. No false positive: legit walked paths live under the v1
    src; only a bridging symlink reaches the new config dir — which IS the
    bug.
    """
    if os.path.islink(dst):
        raise OSError(f"copy destination is a symlink — refusing: {dst}")
    for dirpath, dirnames, filenames in os.walk(
        src, followlinks=True, onerror=lambda e: (_ for _ in ()).throw(e)
    ):
        hit = _resolved_chain_has_cycle(src, dirpath)
        if hit is not None:
            raise RuntimeError(f"symlink cycle detected at {dirpath}")
        real_dp = os.path.realpath(dirpath)
        if real_dp == forbidden_root or real_dp.startswith(
                forbidden_root + os.sep):
            raise RuntimeError(
                f"copy destination re-entered at {dirpath} "
                f"(under {forbidden_root})"
            )
        rel_base = os.path.relpath(dirpath, src)
        dst_base = os.path.join(dst, "" if rel_base == "." else rel_base)
        os.makedirs(dst_base, exist_ok=True)
        for f in filenames:
            s = os.path.join(dirpath, f)
            if not os.path.isfile(s):
                raise OSError(f"expected regular file in {src}: {s}")
            _copy_file_verified(s, os.path.join(dst_base, f))


def migrate_v1_config() -> dict | None:
    """One-time copy-with-verify from the v1 config dir to the develcakes one.

    D3 contract (docs/specs/phases/SPEC-11-PREFLIGHT-DECISIONS.md):
    - Copy list: agent.json, providers.yaml, config.json, conversations/,
      agents/, audit-log.jsonl, projects/, transcript.db (+ -wal/-shm
      sidecars if present), feed-prefs.json if present.
    - No-op guards (return None, no banner): marker file exists OR the new
      dir already contains ANY copy-list entry (both-dirs case: new wins).
    - Verify: per-file byte counts post-copy; directories recursed.
    - Failure (any mismatch / IO error): delete the PARTIAL new-dir contents
      (only entries this migration created — never the marker, never
      pre-existing files), report {"failed": [...]}, app continues fresh.
      The v1 dir is NEVER touched (non-destructive, always).
    - Success: write the marker; return {"copied": [...], "skipped": [...],
      "failed": []} (skipped = copy-list entries absent from v1).

    Implementation notes:
    - Pure os/shutil (utils layer — no git, no GTK, no agent imports).
    - transcript.db and its sidecars are copied as opaque bytes and
      byte-count verified ONLY — the DB is never opened (a live WAL
      mid-checkpoint is fine for a file copy; opening it is not).
    - Everything can raise internally; this function NEVER raises to its
      caller — failures return the report dict with "failed" populated
      (the caller decides banner text; this is utils, no UI).
    """
    new_dir = get_config_dir()
    v1_dir = get_v1_config_dir()

    # Initialized before `try` so the rollback handler can never hit an
    # unbound name regardless of where the failure fires.
    created: list[str] = []
    made_new_dir = False
    # BUG#5 (round 4): marker paths are pre-initialized so the except block
    # can clean up a partial marker/tmp no matter WHERE the failure fired
    # (the marker step itself may never be reached).
    marker_path = os.path.join(new_dir, MIGRATION_MARKER)
    marker_tmp = marker_path + ".tmp"
    # BUG#2 (round 3): catch-site label for the failed report — set by each
    # per-entry try/except to the IN-FLIGHT entry before it re-raises. The
    # only code that can reach the handler without a label is the new-dir
    # makedirs (no copy-list entry is in flight there), so the default is
    # the dir that failed to create — never a function pseudo-name.
    report_failed_name = new_dir
    report: dict = {"copied": [], "skipped": [], "failed": []}

    try:
        # No-op guard 1: v1 dir absent (fresh install — not even the marker
        # check can apply, since the marker lives inside the new dir).
        if not os.path.isdir(v1_dir):
            return None
        # Guard 2 (BUG#3): v1 dir unreadable → FAILED report naming it.
        # Never the marker (a silent no-op here would strand the user's
        # data behind a permissions problem forever).
        if not os.access(v1_dir, os.R_OK | os.X_OK):
            report["failed"] = [f"v1 config dir unreadable: {v1_dir}"]
            return report
        # No-op guard 3: already migrated.
        if os.path.isfile(os.path.join(new_dir, MIGRATION_MARKER)):
            return None
        # No-op guard 4 (D3 REV 3): fires on any PRESENT copy-list entry —
        # lexists, so a DANGLING symlink counts as present (it plants a
        # write-outside hazard os.path.exists would miss).
        # Carve-out (D3 REV 2, narrowed by REV 3): an EMPTY directory at a
        # DIRECTORY copy-list name is not content — migration proceeds and
        # the copy's makedirs(exist_ok=True) absorbs it. ANY entry (file,
        # dir, link, fifo) at a FILE/DB name IS content — an empty dir at
        # `config.json` would otherwise fail every retry with
        # IsADirectoryError forever (no marker, no recovery).
        for name in _MIGRATION_FILES + _MIGRATION_DB_FILES + _MIGRATION_DIRS:
            p = os.path.join(new_dir, name)
            if not os.path.lexists(p):
                continue
            if name in _MIGRATION_DIRS and os.path.isdir(p) and not os.listdir(p):
                continue  # empty placeholder at a DIR name: not content
            return None

        # Guards passed — now (and only now) may the new dir be created.
        if not os.path.isdir(new_dir):
            os.makedirs(new_dir)
            made_new_dir = True

        for name in _MIGRATION_FILES + _MIGRATION_DB_FILES:
            src = os.path.join(v1_dir, name)
            if not os.path.lexists(src):
                report["skipped"].append(name)
                continue
            if not os.path.isfile(src):
                # Malformed v1 state: copy-list entry exists but is not a
                # regular file (a directory named like a file, or a FIFO —
                # open() on a FIFO would block forever). Fail closed. Name
                # the entry: this raise fires BEFORE created.append, so the
                # catch-site label must be set here (the old end-of-run
                # relabeling could not name it at all).
                report_failed_name = name
                raise OSError(f"{name}: copy-list entry is not a regular file: {src}")
            try:
                _copy_file_verified(src, os.path.join(new_dir, name))
            except Exception:
                report_failed_name = name  # BUG#2 (round 3): catch-site label
                raise
            created.append(name)

        for name in _MIGRATION_DIRS:
            src = os.path.join(v1_dir, name)
            if not os.path.lexists(src):
                report["skipped"].append(name)
                continue
            if not os.path.isdir(src):
                report_failed_name = name
                raise OSError(f"{name}: copy-list dir entry is not a directory: {src}")
            # BUG#2: track BEFORE copying — a mid-recursion failure must roll
            # back the partial tree (rmtree), not leave a straggler that
            # poisons guard-4 on the repaired retry. Pre-failure append is
            # safe: rollback only removes what exists.
            created.append(name)
            try:
                _copy_dir_verified(
                    src, os.path.join(new_dir, name),
                    forbidden_root=os.path.realpath(new_dir),
                )
            except Exception:
                report_failed_name = name  # BUG#2 (round 3): catch-site label
                raise

        # Success: marker written ONLY after every entry copied + verified.
        # BUG#5 (round 4): ATOMIC — write to a tmp then os.replace. The old
        # open('w')-then-write created the marker file BEFORE the write; an
        # ENOSPC at write() left a 0-byte marker that survived rollback (the
        # marker is never in `created`), silently no-oping every future run
        # and stranding the user's data forever.
        report_failed_name = MIGRATION_MARKER  # in-flight if the write fails
        with open(marker_tmp, "w", encoding="utf-8") as fh:
            fh.write("migrated from v1 (crabcakes)\n")
        os.replace(marker_tmp, marker_path)

        report["copied"] = created
        return report

    except Exception as exc:  # noqa: BLE001 — contract: never raise to caller
        # Rollback: remove ONLY entries this migration created. Never
        # pre-existing files (they were rejected by the no-op guard); a
        # PARTIAL marker/tmp from a failed marker write is cleaned below
        # (BUG#5, round 4). If this migration conjured the new dir itself,
        # drop it when left empty.
        for name in created:
            path = os.path.join(new_dir, name)
            try:
                # A symlink at a created-path is PRE-EXISTING (the migration
                # only ever writes regular files and real dirs) — preserve
                # it; os.remove/os.rmtree would delete the user's link.
                if os.path.islink(path):
                    continue
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
            except OSError:
                pass  # rollback is best-effort; report already records failure
        # BUG#5 (round 4): a failed/cancelled marker cleanup — neither the
        # tmp nor any partial marker may survive the rollback: a surviving
        # marker poisons the one-shot (every future run a silent no-op, data
        # stranded forever). Runs BEFORE the conjured-dir rmdir so a dir left
        # empty by this cleanup is actually dropped.
        try:
            if os.path.exists(marker_tmp):
                os.remove(marker_tmp)
            if os.path.exists(marker_path):
                os.remove(marker_path)
        except OSError:
            pass
        try:
            if made_new_dir and os.path.isdir(new_dir) and not os.listdir(new_dir):
                os.rmdir(new_dir)
        except OSError:
            pass
        # BUG#2 (round 3): the failed report is labeled AT THE CATCH SITE —
        # `report_failed_name` was set by the per-entry try/except to the
        # IN-FLIGHT entry (or the marker/new-dir makedirs context), with the
        # failing step's OWN exception. The old end-of-run relabeling
        # (`[f"{name}: {exc}" for name in created] or [pseudo-name]`) named
        # prior SUCCESSES and invented a `migrate_v1_config:` pseudo-entry.
        report["failed"] = [f"{report_failed_name}: {exc}"]
        return report


def get_config_file() -> str:
    """Return path to config.json (API keys, base URLs, etc.)."""
    return os.path.join(get_config_dir(), "config.json")


def get_projects_config_dir() -> str:
    """Return path to projects config directory (members.json files live here).

    Located inside the develcakes config dir, NOT inside the browsable projects root.
    """
    return os.path.join(get_config_dir(), "projects")


def get_projects_dir() -> str:
    """Return the browsable projects directory (actual project folders).

    Controlled by $CRABCAKES_PROJECTS_DIR, defaults to ~/projects.
    This is the root that the FileTree widget navigates.
    """
    return os.environ.get(
        "CRABCAKES_PROJECTS_DIR",
        os.path.join(os.path.expanduser("~"), "projects"),
    )


def get_project_root() -> str:
    """Return the develcakes repository root (the directory containing main.py).

    Derived from this file's location (utils/config.py -> parent of utils/), so
    it is correct regardless of the current working directory and regardless of
    where the checkout lives. Use this instead of hardcoding an absolute path
    to the repo when locating bundled assets (icons/, prompts/, knowledge/).

    Note: an editable install (pip install -e .) keeps the repo layout, so this
    resolves to the checkout. A non-editable install resolves to the installed
    package directory, where bundled data files are only present if declared as
    package data.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Command system configuration
# Backtick prefix — triggers command parsing in ChatHandler.on_send().
# Distinct from slash commands (/approve, /status, etc.) which use "/".
COMMAND_PREFIX = "/"

