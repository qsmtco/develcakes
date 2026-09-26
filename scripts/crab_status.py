#!/usr/bin/env python3
"""crab_status.py — command-line shim for the DevelCakes status reporter.

AGENTCTRL1 Phase 1b (SPEC-AGENT-CONTROL-1 §2.2 CLI, §2.3 invariants, §2.4 alert
path). Pure Python over `utils.status_report`: no GTK, no network, no import of
`ui/` or `agent/`, and no scheduler. Correct with the app running or closed —
that is the point of the tool.

Usage:
    crab_status.py [--project PATH] [--json] [--full] [--no-feed] [--watch N]
                   [--auto-resume]

Exit codes (§2.2): 0 healthy · 2 attention needed · 3 app not running ·
10 reserved flag (`--auto-resume`, Phase 3 — not implemented yet).
Three codes outside that set exist on purpose: 64 usage/argv error (argparse's
convention narrowed to a code the §2.4 cron cannot mistake for "attention" —
audit BUG #6), and 1 for an unexpected reporter failure with a traceback on
stderr (§1.4: failure is loud, never a silent no-op). `--help` exits 0 for a
human but 64 when `--json` was requested, since help text is not a report.

Read-only contract (§2.3.6 / §2.3.2): this process never mutates app state. Its
only writes are inside the reporter's own cache directory
(`<XDG_CACHE_HOME or ~/.cache>/crabcakes`): the feed-summary cache and the §2.4
alert-dedupe state file (`status-state.json`, 0600 — written only when an alert
actually fires; a healthy run writes nothing).

§2.4 alert-dedupe (Phase 1b+): when a stall class fires, the CLI computes the
episode id (`stall_episode_id`), consults `should_alert` against the persisted
state, and sets `report["alert"]`. On True it stamps and saves the state
(`mark_alerted` is applied inside `should_alert`); on False (same episode within
the re-alert floor) `alert` stays False and the state file is not rewritten.
Healthy runs are silence by default: `alert=False`, no state write. Usage
errors exit 64 before any of this runs, so an argv typo can never alert
(audit BUG #6).
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
import traceback

# scripts/crab_status.py is one level deep: make `utils` importable when the
# script is run directly (`python3 scripts/crab_status.py`) from any cwd.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from utils import status_report  # imported after the sys.path bootstrap

EXIT_HEALTHY = 0           # §2.2
EXIT_FAILURE = 1           # §1.4 unexpected reporter failure (loud)
EXIT_ATTENTION = 2         # §2.2
EXIT_APP_DOWN = 3          # §2.2
EXIT_NOT_IMPLEMENTED = 10  # reserved flag handled by a later phase
EXIT_USAGE = 64            # argv/usage error — never the §2.2 attention code

NOT_IMPLEMENTED_MSG = "not implemented (Phase 3)"
USAGE_EPILOG = (
    "For cron/alerting run `crab_status.py --json`; the exit code is the alert "
    "condition (§2.4) and the report is printed as JSON on stdout."
)


class _UsageExit(Exception):
    """Raised by `_ArgumentParser.exit` instead of SystemExit.

    Keeps `main()` a plain `argv -> int` function (no SystemExit escaping), so
    the exit-code mapping is unit-testable in-process.
    """

    def __init__(self, status):
        super().__init__(int(status))
        self.status = int(status)


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that reports --help/usage errors as return codes."""

    def exit(self, status=0, message=None):
        if message:
            self._print_message(message, sys.stderr)
        raise _UsageExit(status)


def _positive_seconds(value):
    """`--watch N`: a finite number of seconds > 0 (Rule 6: validate argv)."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"invalid seconds value: {value!r}")
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError(
            f"--watch seconds must be a positive number, got {value!r}")
    return seconds


def build_parser():
    """The §2.2 argv surface. Nothing else is accepted (no `--nudge` here)."""
    parser = _ArgumentParser(
        prog="crab_status.py",
        description="Read-only DevelCakes status report (AGENTCTRL1 Phase 1).",
        epilog=USAGE_EPILOG,
    )
    parser.add_argument("--project", default=".", metavar="PATH",
                        help="project directory to report on (default: .)")
    parser.add_argument("--json", action="store_true",
                        help="emit the machine-readable report (cron/alerting)")
    parser.add_argument("--full", action="store_true",
                        help="raise the message-body cap to 2000 chars "
                             "(bodies only; tool arguments and command output "
                             "are never rendered)")
    parser.add_argument("--no-feed", action="store_true",
                        help="skip the feed-summary section")
    parser.add_argument("--watch", type=_positive_seconds, metavar="N",
                        default=None,
                        help="re-render every N seconds until interrupted "
                             "(Ctrl-C exits 0)")
    parser.add_argument("--auto-resume", action="store_true",
                        help="Phase 3 (not implemented) — reserved flag")
    return parser


def _apply_alert_dedupe(report):
    """§2.4 wiring: set `report["alert"]` from the dedupe helpers.

    Worst-class selection matches assess(): the first stall in the
    (already-sorted) stall list. Healthy/no-episode → alert stays False and
    NO state is written (silence is the default). Any helper failure degrades
    to alert=False — an alert bookkeeping error must never invent an alert.

    Returns nothing; mutates the report in place (the caller owns it).
    """
    try:
        stalls = report.get("stalls") or []
        worst = stalls[0] if stalls else None
        if worst is None or not isinstance(worst, dict):
            report["alert"] = False
            return
        episode = status_report.stall_episode_id(worst)
        state = status_report.load_alert_state(status_report.state_path())
        now = time.time()
        should, updated_state = status_report.should_alert(
            state, episode, now, status_report.ALERT_REALERT_HOURS)
        if should:
            # updated_state already carries the mark_alerted stamp (§2.4
            # contract: should_alert stamps on True).
            status_report.save_alert_state(status_report.state_path(),
                                           updated_state)
        report["alert"] = bool(should)
    except Exception as exc:  # noqa: BLE001 — bookkeeping must never alert-spam
        print(f"crab_status: alert dedupe failed ({type(exc).__name__}: {exc}); "
              "suppressing alert", file=sys.stderr)
        report["alert"] = False


def _run_once(args, include_feed, body_cap):
    """Collect, dedupe-alert, render, print. Returns the §2.2 exit code (or EXIT_FAILURE)."""
    try:
        report = status_report.collect(args.project, include_feed=include_feed,
                                       body_cap=body_cap)
        # §2.4: the dedupe decision happens BEFORE rendering so the printed
        # report (JSON for cron, text for humans) carries the final alert flag.
        # Exit code comes from assess() at run time — the §2.2 contract the
        # suite pins (collect() also stamps summary/exit_code; assess is pure,
        # so recomputing here is the same answer on real reports and stays
        # correct when a caller substitutes a partial report).
        _apply_alert_dedupe(report)
        code = status_report.assess(report)[1]
        if args.json:
            output = status_report.render_json(report) + "\n"
        else:
            output = status_report.render_text(report, show_content=args.full)
    except Exception as exc:  # KeyboardInterrupt is BaseException: not caught
        print(f"crab_status: report failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        traceback.print_exc()
        return EXIT_FAILURE
    sys.stdout.write(output)
    sys.stdout.flush()
    return code


def main(argv=None) -> int:
    """Run the CLI. Returns the process exit code; never raises SystemExit.

    Usage errors return `EXIT_USAGE` (64), not `EXIT_ATTENTION` (2): the §2.4 cron
    reads 2 as "a stall class fired", so an argv typo must never look like a
    stall (audit BUG #6). `--help` keeps the conventional 0 for a human, but
    returns 64 when `--json` was requested — help text on stdout is not a report
    and must not read as a successful run to a parser.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageExit as exc:
        if exc.status == 0 and "--json" not in argv:
            return EXIT_HEALTHY          # plain --help/-h: normal success
        return EXIT_USAGE

    # Reserved Phase-3 flag: refuse before collecting anything. §2.2/§2.3.6 —
    # a reserved flag must never be a silent no-op, and must never act.
    if args.auto_resume:
        print(NOT_IMPLEMENTED_MSG, file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED

    include_feed = not args.no_feed
    body_cap = (status_report.BODY_CAP_FULL if args.full
                else status_report.BODY_CAP_DEFAULT)

    if args.watch is None:
        return _run_once(args, include_feed, body_cap)

    # --watch N: re-collect and re-render every N seconds until interrupted.
    #
    # TODO(AGENTCTRL1 P1 audit BUG #9): each iteration writes a complete
    # document, so `--json --watch` produces concatenated JSON and stdout is not
    # a parseable stream (a scheduler must use single-shot `--json`, which is the
    # §2.4 contract). Deferred: revisit only if a streaming consumer is wired.
    try:
        while True:
            code = _run_once(args, include_feed, body_cap)
            if code == EXIT_FAILURE:
                return code
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return EXIT_HEALTHY


if __name__ == "__main__":
    sys.exit(main())
