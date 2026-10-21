#!/usr/bin/env python3
"""R-5 clean-checkout smoke (SPEC-47 §2.4).

Proves HEAD is self-contained: ``git archive HEAD`` into a temp dir, then —
in a SUBPROCESS with ``cwd=<tmp>`` and ``PYTHONPATH=<tmp>`` — imports the
app's entry modules and calls ``utils.agent_defs.load_agent_defs()``. This
catches untracked files the live tree masks (the F-1 class that shipped
"129 passed" while the app was un-importable from a clean checkout).

Rules:
  * The probe runs in a fresh interpreter against the EXTRACTED tree only.
  * ``main`` is never imported in-process; the probe imports the modules the
    clean-checkout claim covers (the probe subprocess is itself free to
    import whatever the tree provides).
  * Exit 0 = pass; non-zero = fail (subprocess stdout/stderr echoed).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = (
    "import sys; "
    "import ui.window, agent.special_agents; "
    "import utils.agent_defs as agent_defs; "
    "agent_defs.load_agent_defs(); "
    "print('clean-checkout import OK')"
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="clean-checkout-") as tmp:
        # 1. Archive HEAD (committed tree only).
        archive_path = os.path.join(tmp, "head.tar")
        with open(archive_path, "wb") as fh:
            archive = subprocess.run(
                ["git", "archive", "--format=tar", "HEAD"],
                cwd=REPO_ROOT,
                stdout=fh,
                stderr=subprocess.PIPE,
                text=True,
            )
        if archive.returncode != 0:
            print("clean-checkout: FAIL — git archive HEAD failed", file=sys.stderr)
            print(archive.stderr, file=sys.stderr)
            return 1

        # 2. Extract into the temp tree.
        try:
            with tarfile.open(archive_path, "r") as tf:
                tf.extractall(tmp)
        except (tarfile.TarError, OSError) as exc:  # pragma: no cover
            print(f"clean-checkout: FAIL — extraction failed: {exc}", file=sys.stderr)
            return 1

        # 3. Probe the extracted tree in a subprocess.
        env = dict(os.environ)
        env["PYTHONPATH"] = tmp
        env["HOME"] = tmp
        env["XDG_CONFIG_HOME"] = os.path.join(tmp, ".config")
        probe = subprocess.run(
            [sys.executable, "-c", PROBE],
            cwd=tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if probe.returncode != 0:
            print("clean-checkout: FAIL — probe import failed", file=sys.stderr)
            if probe.stdout:
                print(probe.stdout, end="", file=sys.stderr)
            if probe.stderr:
                print(probe.stderr, end="", file=sys.stderr)
            return 1

        sys.stdout.write(probe.stdout)
        print("clean-checkout: PASS — committed tree is self-contained")
        return 0


if __name__ == "__main__":
    sys.exit(main())
