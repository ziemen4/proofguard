#!/usr/bin/env python3
"""Owner CLI. Run Python with -I; configuration paths never come from proposals."""

import argparse
import json
from pathlib import Path
import sys
import os
import signal
import ctypes

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "bend-spec")]

from bendspec.model import ContractError
from proofguard.packages import Enrollment
from proofguard.plugin import enroll_configuration
from proofguard.worker import OutboxCapacityError, OutboxWorker


def stop_worker(signum, _frame):
    """Bend supervisor children own process groups; terminate them on shutdown."""
    children = Path(f"/proc/self/task/{os.getpid()}/children")
    for child in children.read_text().split():
        try:
            os.killpg(int(child), signal.SIGKILL)
        except ProcessLookupError:
            try:
                os.kill(int(child), signal.SIGKILL)
            except ProcessLookupError:
                pass
    raise SystemExit(128 + signum)


def load_selected_enrollment(args, parser) -> Enrollment:
    """Require an existing owner selection and forbid request-time configuration."""
    if not args.enrollment or args.config:
        parser.error("task/watch require --enrollment and forbid --config")
    return Enrollment.load(args.authority, args.enrollment)


def watch_proposals(selected: Enrollment, args):
    """Supervise checker children while pulling proposals from fixed owner paths."""
    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)
    if args.parent is not None:
        # Linux notification also covers a SIGKILLed harness runner.
        if os.getppid() != args.parent:
            raise SystemExit(1)
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise OSError("parent-death supervision unavailable")
        if os.getppid() != args.parent:
            stop_worker(signal.SIGTERM, None)
    worker = OutboxWorker(selected.build_gateway(args.authority), args.outbox, args.results)
    for result in worker.poll() if args.once else worker.watch():
        print(json.dumps(result.data()), flush=True)


def main():
    """Dispatch owner setup, task export and proposal pulling with fixed paths."""
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("enroll", "task", "watch"))
    parser.add_argument("--authority", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--enrollment")
    parser.add_argument("--parent", type=int)
    parser.add_argument("--outbox", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    match args.command:
        case "enroll":
            if not args.config or args.enrollment:
                parser.error("enroll requires --config and forbids --enrollment")
            args.authority.mkdir(mode=0o700, parents=True, exist_ok=True)
            selected = enroll_configuration(args.config, args.authority)
            print(json.dumps({"schema": 1, "enrollment_id": selected.identity,
                              "contract_id": selected.record["contract_id"]}))
        case "task":
            selected = load_selected_enrollment(args, parser)
            print(json.dumps(selected.get_task(args.authority)))
        case "watch":
            selected = load_selected_enrollment(args, parser)
            if args.outbox is None or args.results is None:
                parser.error("watch requires --outbox and --results")
            watch_proposals(selected, args)


if __name__ == "__main__":
    try:
        main()
    except OutboxCapacityError:
        print("ProofGuard outbox capacity exceeded; owner must clean or rotate it before restarting.", file=sys.stderr)
        sys.exit(1)
    except (ContractError, ValueError, OSError, ImportError, AttributeError):
        # Owner config can contain private data. Do not echo native exceptions.
        print("ProofGuard owner command failed; no success is recorded.", file=sys.stderr)
        sys.exit(1)
