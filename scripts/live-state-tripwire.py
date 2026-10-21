#!/usr/bin/env python3
"""R-6 live-state tripwire (SPEC-47 §2.4).

Proves the gate run does not write into LIVE user state: snapshots the live
config dir (path / mtime / size / content hash per file, covering the dir
and its ``conversations/`` subdir) BEFORE and AFTER running a representative
row subset (the R-1 and R-2 nodes, which touch config-dir state).

Rules:
  * ANY change to the live config dir fails the tripwire.
  * A missing dir is not a change (an absent ``<config_dir>`` snapshots
    empty both sides and passes).
  * The measured action's own exit code is irrelevant — only the snapshot
    comparison decides.
  * Exit 0 = pass; non-zero = fail.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The LIVE config root the tripwire guards. Overridable so the gate can point
# the tripwire at a scratch "live" tree when exercising the R-6 mutation.
LIVE_CONFIG = Path(
    os.environ.get("DEVELCAKES_LIVE_CONFIG", Path.home() / ".config" / "develcakes")
)

# Representative row subset: the R-1 and R-2 nodes.
MEASURED_NODES = [
    "tests/test_live_bridge.py::test_clear_conversation_deletes_json_only",
    "tests/test_live_guard.py::TestInjectionScroll",
]


def _file_fingerprint(path: Path):
    st = path.stat()
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    return (st.st_mtime_ns, st.st_size, digest)


def snapshot(root: Path) -> dict[str, tuple]:
    """Return {relpath: fingerprint} for root and its conversations/ subdir.

    Only the immediate files of ``root`` and the immediate files of
    ``root/conversations`` are covered — anything deeper is unrelated machine
    activity and must not trip the wire.
    """
    out: dict[str, tuple] = {}
    if not root.is_dir():
        return out
    top = root.resolve()
    for dirpath, dirnames, filenames in os.walk(top):
        rel = os.path.relpath(dirpath, top)
        if rel not in (".", "conversations"):
            dirnames[:] = []
            continue
        for name in filenames:
            p = Path(dirpath) / name
            try:
                out[os.path.relpath(p, top)] = _file_fingerprint(p)
            except OSError:
                continue
    return out


def run_measured_action() -> None:
    """Run the gate itself on the representative row subset (R-1, R-2).

    The gate's row runs spawn env-isolated subprocesses; the tripwire's job is
    to catch ANY write that escapes isolation into LIVE_CONFIG. Outcome of the
    gate run is ignored; config-dir side effects are what matter.
    """
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "acceptance.py"),
           "rows", "--ids", "R-1,R-2"]
    subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)


def main() -> int:
    before = snapshot(LIVE_CONFIG)
    run_measured_action()
    after = snapshot(LIVE_CONFIG)

    changed = {
        k for k in set(before) | set(after) if before.get(k) != after.get(k)
    }
    if changed:
        print("live-state-tripwire: FAIL — live config dir changed", file=sys.stderr)
        for rel in sorted(changed):
            print(f"  changed: {rel}", file=sys.stderr)
        return 1

    state = "present" if bool(before) else "absent (no change)"
    print(f"live-state-tripwire: PASS — live config dir {state}, {len(before)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
