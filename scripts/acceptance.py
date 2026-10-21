#!/usr/bin/env python3
"""SPEC-47 Acceptance Gate — make "done" mechanically re-provable.

Subcommands:
  rows     — every registered row (mutated-fail, then clean-pass) + per-row evidence
  suite    — full pytest; the fail-set is diffed against tests/known_failures.txt
            (shrink-only: any new failure refuses)
  smokes   — the committed smoke nodes (R-3 window smoke, R-5 clean-checkout)
  all      — everything above + .acceptance/last-run.json + a summary table

Normative semantics (SPEC-47 §2.3):
  * Every row runs mutated-then-clean. The MUTATED run MUST fail — a pass is
    vacuous and the gate fails loudly. The CLEAN run must pass.
  * Mutations apply with `git apply` inside a throwaway temp copy built from
    `git archive HEAD` plus an overlay of uncommitted/untracked working-tree
    files (excluding .git/.venv/__pycache__/.pytest_cache/.acceptance/.debug).
    The live tree is never mutated.
  * A diff that does not apply fails the row loudly with git's output.
  * A skipped pytest node fails the row unless the row sets skip_ok: true.
  * Every subprocess gets XDG_CONFIG_HOME pointed at a fresh temp dir.
  * stdlib + pytest only; no framework; no GTK import at module scope.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.yaml_load import safe_load_yaml

_EXCLUDED_DIRS = {
    ".git", ".venv", "__pycache__", ".pytest_cache",
    ".acceptance", ".debug",
}
_DEFAULT_ROWS = ROOT / "docs" / "acceptance" / "rows.yaml"
_DEFAULT_MUTATIONS = ROOT / "tests" / "mutations"
_KNOWN_FAILURES = ROOT / "tests" / "known_failures.txt"
_LAST_RUN = ROOT / ".acceptance" / "last-run.json"
_PYTHON = sys.executable


def _isolated_env(xdg_home: str) -> dict:
    """Environment for every app subprocess: config resolution is forced into
    a throwaway temp dir so gate runs can never write live state."""
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = xdg_home
    return env


def load_rows(path=None) -> list[dict]:
    """Read the row registry YAML. Raises loudly if missing/malformed."""
    path = Path(path) if path else _DEFAULT_ROWS
    if not path.exists():
        raise FileNotFoundError(f"acceptance rows registry not found: {path}")
    with open(path, encoding="utf-8") as fh:
        data = safe_load_yaml(fh)
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a YAML list of rows")
    return [r for r in data if isinstance(r, dict)]


def row_node_id(row: dict) -> str | None:
    """The row's runnable node id; None for placeholders (node null)."""
    node = row.get("node")
    if isinstance(node, str) and node.strip() and node.strip().lower() != "null":
        return node.strip()
    return None


def registered_rows(rows=None) -> list[dict]:
    """Rows carrying a runnable node (placeholders like R-4 are excluded)."""
    rows = rows if rows is not None else load_rows()
    return [r for r in rows if row_node_id(r) is not None]


def _is_pytest_node(node: str) -> bool:
    return "::" in node or node.startswith("tests/")


def make_temp_copy(root=None, base=None) -> str:
    """Throwaway copy: `git archive HEAD` + overlay of the working tree.

    The overlay copies uncommitted/untracked working-tree files (excluding
    .git/.venv/__pycache__/.pytest_cache/.acceptance/.debug) so the temp copy
    mirrors the live tree while the LIVE tree itself is never touched.
    """
    root = Path(root) if root else ROOT
    tmp = Path(tempfile.mkdtemp(prefix="acceptance-gate-", dir=base))
    archive = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if archive.returncode != 0:
        raise RuntimeError(
            f"git archive HEAD failed ({archive.returncode}): "
            f"{archive.stderr.decode(errors='replace')}"
        )
    extract = subprocess.run(["tar", "-x", "-C", str(tmp)], input=archive.stdout)
    if extract.returncode != 0:
        raise RuntimeError("tar extraction of git archive failed")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIRS)
        rel = Path(dirpath).relative_to(root)
        target_dir = tmp / rel
        for fn in filenames:
            src = Path(dirpath) / fn
            dst = target_dir / fn
            if dst.exists() and os.path.samefile(src, dst):
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return str(tmp)


def apply_mutation(temp_copy: str, diff_path) -> subprocess.CompletedProcess:
    """Apply a mutation diff with git apply.

    Returns the CompletedProcess; a non-zero returncode means the diff did
    not apply — the row fails loudly, with git's output in stderr.
    """
    diff_path = Path(diff_path)
    proc = subprocess.run(
        ["git", "apply", "--verbose", "--check", str(diff_path)],
        cwd=temp_copy,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode == 0:
        proc = subprocess.run(
            ["git", "apply", str(diff_path)],
            cwd=temp_copy,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    return proc


def _launch(cmd, cwd, env, timeout):
    try:
        return subprocess.run(
            cmd, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd, 124, exc.stdout or b"", exc.stderr or b"")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, b"", str(exc).encode())


def run_node(node: str, cwd: str, runner: str = "plain", timeout: int = 1800):
    """Run one acceptance node.

    Returns (proc, kind): kind is 'pytest' for pytest node ids and 'script'
    for committed scripts (e.g. the window/clean-checkout smokes). Every
    subprocess is env-isolated (XDG_CONFIG_HOME → fresh temp dir).
    """
    xdg = tempfile.mkdtemp(prefix="xdg-config-gate-")
    env = _isolated_env(xdg)
    if _is_pytest_node(node):
        cmd = [_PYTHON, "-m", "pytest", "-q", "-rN", node]
        kind = "pytest"
    else:
        cmd = [_PYTHON, str(Path(cwd) / node)]
        kind = "script"
    if runner == "xvfb":
        cmd = ["xvfb-run", "-a"] + cmd
    return _launch(cmd, cwd, env, timeout), kind


def classify_run(proc, kind: str) -> dict:
    """Parse a CompletedProcess into passed/failed/skipped/errors counts."""
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0, "kind": kind}
    if kind == "script":
        counts["passed"] = 1 if proc.returncode == 0 else 0
        counts["failed"] = 0 if proc.returncode == 0 else 1
        return counts
    text = ""
    if proc.stdout:
        text += proc.stdout.decode(errors="replace")
    if proc.stderr:
        text += proc.stderr.decode(errors="replace")
    for key in ("passed", "failed", "skipped", "errors"):
        m = re.search(rf"(\d+) {key}", text)
        if m:
            counts[key] = int(m.group(1))
    return counts


def check_row(row: dict, diffs_dir=_DEFAULT_MUTATIONS, root=None) -> dict:
    """Run one row mutated-then-clean; returns evidence + verdict."""
    row_id = row.get("id", "?")
    node = row_node_id(row)
    out = {
        "id": row_id,
        "claim": row.get("claim", ""),
        "node": node,
        "runner": row.get("runner", "plain"),
        "mutated": None,
        "clean": None,
        "ok": False,
        "notes": [],
    }
    if node is None:
        out["notes"].append("placeholder row (node null) — skipped")
        out["ok"] = True
        return out

    diff_path = Path(diffs_dir if diffs_dir else _DEFAULT_MUTATIONS) / f"{row_id}.diff"
    if not diff_path.exists():
        out["notes"].append(f"mutation diff missing: {diff_path}")
        return out

    tmp = make_temp_copy(root=root)
    ap = apply_mutation(tmp, diff_path)
    if ap.returncode != 0:
        out["mutated"] = {
            "applied": False,
            "stderr": ap.stderr.decode(errors="replace")[-4000:],
        }
        out["notes"].append("mutation did not apply — row fails loudly (git output above)")
        return out
    out["mutated"] = {"applied": True}

    mrun, kind = run_node(node, cwd=tmp, runner=row.get("runner", "plain"))
    msum = classify_run(mrun, kind)
    out["mutated"]["summary"] = msum
    out["mutated"]["returncode"] = mrun.returncode
    mutated_failed = msum["failed"] > 0 or msum["errors"] > 0
    if not mutated_failed and msum["passed"] == 0 and mrun.returncode != 0:
        mutated_failed = True  # collection/launch errors also count as failures
    if not mutated_failed:
        out["notes"].append(f"MUTATED run PASSED (vacuous) — gate refuses row {row_id}")
        return out

    crun, ckind = run_node(node, cwd=str(root) if root else str(ROOT), runner=row.get("runner", "plain"))
    csum = classify_run(crun, ckind)
    out["clean"] = {"summary": csum, "returncode": crun.returncode}
    skip_ok = bool(row.get("skip_ok"))
    clean_ok = (
        csum["failed"] == 0
        and csum["errors"] == 0
        and (csum["passed"] > 0 or (csum["skipped"] > 0 and skip_ok))
        and (ckind != "script" or crun.returncode == 0)
    )
    if not clean_ok:
        out["notes"].append(f"clean run did not pass for {row_id}: {csum}")
        return out
    out["ok"] = True
    return out


def extract_fail_set(proc) -> list[str]:
    """Collect FAILED node ids from a pytest -q run."""
    text = ""
    if proc.stdout:
        text += proc.stdout.decode(errors="replace")
    if proc.stderr:
        text += proc.stderr.decode(errors="replace")
    fails = set()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("FAILED"):
            fails.add(line[len("FAILED"):].strip())
    return sorted(fails)


def run_suite(pytest_args=None, root=None, timeout: int = 3600):
    """Full pytest in the live tree, env-isolated. Returns (proc, None)."""
    root = Path(root) if root else ROOT
    xdg = tempfile.mkdtemp(prefix="xdg-config-suite-")
    env = _isolated_env(xdg)
    cmd = [_PYTHON, "-m", "pytest", "-q"] + list(pytest_args or [])
    return _launch(cmd, str(root), env, timeout), None


def diff_fail_set(current, baseline_path=_KNOWN_FAILURES) -> dict:
    """Diff the current fail-set against known_failures.txt (shrink-only)."""
    baseline_path = Path(baseline_path)
    baseline = set()
    if baseline_path.exists():
        for line in baseline_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                baseline.add(line)
    return {
        "current": sorted(set(current)),
        "baseline": sorted(baseline),
        "new_failures": sorted(set(current) - baseline),
        "shrunk": sorted(baseline - set(current)),
        "allowed": not bool(set(current) - baseline),
    }


def collect_rows(ids: str = "") -> list[dict]:
    """Build per-row evidence for the requested (or all registered) rows."""
    rows = load_rows()
    if ids:
        wanted = {x.strip() for x in ids.split(",") if x.strip()}
        rows = [r for r in rows if r.get("id") in wanted]
    return [check_row(r) for r in registered_rows(rows)]


def _print_rows(results) -> None:
    for r in results:
        status = "PASS" if r["ok"] else "FAIL"
        print(f"[{status}] {r['id']}  node={r['node']}")
        for n in r["notes"]:
            print(f"        note: {n}")
        if r["mutated"] and "summary" in r["mutated"]:
            s = r["mutated"]["summary"]
            print(f"        mutated: applied, rc={r['mutated'].get('returncode')}, "
                  f"passed={s['passed']} failed={s['failed']} "
                  f"skipped={s['skipped']} errors={s['errors']}")
        if r["clean"]:
            s = r["clean"]["summary"]
            print(f"        clean: rc={r['clean']['returncode']}, "
                  f"passed={s['passed']} failed={s['failed']} "
                  f"skipped={s['skipped']} errors={s['errors']}")


def cmd_rows(args) -> int:
    results = collect_rows(args.ids)
    ok = all(r["ok"] for r in results)
    if args.json:
        print(json.dumps(results, indent=2))
        return 0 if ok else 1
    _print_rows(results)
    return 0 if ok else 1


def cmd_suite(args) -> int:
    proc, _fails = run_suite()
    fail_set = extract_fail_set(proc)
    diff = diff_fail_set(fail_set)
    if args.json:
        print(json.dumps({"returncode": proc.returncode, **diff}, indent=2))
        return 0 if diff["allowed"] else 1
    print(f"pytest exit={proc.returncode}; failures={len(diff['current'])} "
          f"(baseline={len(diff['baseline'])})")
    for f in diff["new_failures"]:
        print(f"  NEW FAILURE (refuses): {f}")
    for f in diff["shrunk"]:
        print(f"  shrunk (fixed): {f}")
    print("gate: ALLOWED" if diff["allowed"] else "gate: REFUSED (new failures)")
    return 0 if diff["allowed"] else 1


def cmd_smokes(args) -> int:
    rows = [r for r in load_rows()
            if row_node_id(r) and not _is_pytest_node(row_node_id(r))]
    results = [check_row(r) for r in rows]
    ok = all(r["ok"] for r in results)
    if args.json:
        print(json.dumps(results, indent=2))
        return 0 if ok else 1
    _print_rows(results)
    return 0 if ok else 1


def cmd_all(args) -> int:
    rows = collect_rows()
    suite_proc, _fails = run_suite()
    suite_diff = diff_fail_set(extract_fail_set(suite_proc))
    smoke_rows = [r for r in load_rows()
                  if row_node_id(r) and not _is_pytest_node(row_node_id(r))]
    smoke_results = [check_row(r) for r in smoke_rows]
    ok = (
        all(r["ok"] for r in rows)
        and suite_diff["allowed"]
        and all(r["ok"] for r in smoke_results)
    )
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "rows": rows,
        "suite": {"returncode": suite_proc.returncode, **suite_diff},
        "smokes": smoke_results,
        "ok": ok,
    }
    _LAST_RUN.parent.mkdir(parents=True, exist_ok=True)
    _LAST_RUN.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0 if ok else 1
    print("=== acceptance gate: ALL ===")
    _print_rows(rows)
    print(f"suite: exit={suite_proc.returncode} "
          f"failures={len(suite_diff['current'])} "
          f"new={len(suite_diff['new_failures'])}")
    _print_rows(smoke_results)
    print(f"overall: {'PASS' if ok else 'FAIL'} → .acceptance/last-run.json written")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="acceptance", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_rows = sub.add_parser("rows", help="per-row evidence (mutated-fail + clean-pass)")
    p_rows.add_argument("--ids", default="", help="comma-separated row ids, e.g. R-1,R-2")
    p_rows.add_argument("--json", action="store_true")
    p_suite = sub.add_parser("suite", help="full pytest, fail-set vs known_failures.txt")
    p_suite.add_argument("--json", action="store_true")
    p_smokes = sub.add_parser("smokes", help="committed smoke nodes (window/clean-checkout)")
    p_smokes.add_argument("--json", action="store_true")
    p_all = sub.add_parser("all", help="rows + suite + smokes + last-run.json")
    p_all.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "rows":
        return cmd_rows(args)
    if args.command == "suite":
        return cmd_suite(args)
    if args.command == "smokes":
        return cmd_smokes(args)
    return cmd_all(args)


if __name__ == "__main__":
    sys.exit(main())