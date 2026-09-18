#!/usr/bin/env python3
"""
tools/doctor.py — unified entrypoint for FirmArbiter's three separate
diagnostic scripts.

This was always part of the original onboarding-reliability design (a
single `firmarbiter doctor host/candidate/adapter/all` command) but had
never actually been built -- until now you had to know all three
scripts' names and call each by hand:
  - firmarbiter_selfcheck.py      (host environment)
  - tools/assess_candidate.py     (a candidate BEFORE an adapter exists)
  - tools/test_adapter.py         (a real adapter's contract compliance)

This file adds NO new checking logic of its own -- it's a thin
dispatcher over those three, already-tested scripts, run as real
subprocesses with their normal output streamed straight through, so
none of their own behavior or test coverage changes. Exit code is 0
only if every stage that ran succeeded; a real, meaningful stage
failure isn't lost in dispatcher logic.

Usage:
    python3 tools/doctor.py host
    python3 tools/doctor.py candidate <path_or_git_url>
    python3 tools/doctor.py adapter <adapter_dir> [--timeout SECONDS]
    python3 tools/doctor.py all <path_or_git_url>
        (runs host, then candidate against the given target -- there is
        no adapter to test yet at this stage by definition, run
        'doctor adapter' separately once one exists)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SELFCHECK_SCRIPT = PROJECT_ROOT / "firmarbiter_selfcheck.py"
ASSESS_SCRIPT = PROJECT_ROOT / "tools" / "assess_candidate.py"
TEST_ADAPTER_SCRIPT = PROJECT_ROOT / "tools" / "test_adapter.py"


def run_stage(title: str, command: list[str]) -> bool:
    print(f"\n{'=' * 70}")
    print(f"[doctor] {title}")
    print(f"{'=' * 70}\n")
    completed = subprocess.run(command)
    ok = completed.returncode == 0
    print(
        f"\n[doctor] {title}: "
        f"{'PASSED' if ok else f'FAILED (exit {completed.returncode})'}"
    )
    return ok


def doctor_host() -> bool:
    return run_stage(
        "Host environment self-check",
        [sys.executable, str(SELFCHECK_SCRIPT)],
    )


def doctor_candidate(target: str) -> bool:
    return run_stage(
        f"Candidate assessment: {target}",
        [sys.executable, str(ASSESS_SCRIPT), target],
    )


def doctor_adapter(adapter_dir: str, timeout: int | None) -> bool:
    command = [sys.executable, str(TEST_ADAPTER_SCRIPT), adapter_dir]
    if timeout is not None:
        command += ["--timeout", str(timeout)]
    return run_stage(f"Adapter smoke test: {adapter_dir}", command)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified FirmArbiter doctor -- dispatches to the "
        "existing host/candidate/adapter check scripts.",
    )
    subparsers = parser.add_subparsers(dest="stage", required=True)

    subparsers.add_parser("host", help="Check the host environment")

    candidate_parser = subparsers.add_parser(
        "candidate", help="Assess a candidate before an adapter exists",
    )
    candidate_parser.add_argument(
        "target", help="Local path or git URL of the candidate tool's repo",
    )

    adapter_parser = subparsers.add_parser(
        "adapter", help="Smoke-test a real adapter's contract compliance",
    )
    adapter_parser.add_argument("adapter_dir", help="Path to the adapter directory")
    adapter_parser.add_argument(
        "--timeout", type=int, default=None,
        help="Container run timeout in seconds (passed through to test_adapter.py)",
    )

    all_parser = subparsers.add_parser(
        "all", help="Run host + candidate together",
    )
    all_parser.add_argument(
        "target", help="Local path or git URL of the candidate tool's repo",
    )

    args = parser.parse_args()

    if args.stage == "host":
        ok = doctor_host()
    elif args.stage == "candidate":
        ok = doctor_candidate(args.target)
    elif args.stage == "adapter":
        ok = doctor_adapter(args.adapter_dir, args.timeout)
    elif args.stage == "all":
        host_ok = doctor_host()
        candidate_ok = doctor_candidate(args.target)
        ok = host_ok and candidate_ok
        print(
            f"\n[doctor] Note: 'all' only covers host + candidate -- "
            f"there is no adapter to test yet at this stage by "
            f"definition. Run 'doctor adapter <dir>' separately once "
            f"one exists."
        )
    else:
        parser.print_help()
        return 2

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
