#!/usr/bin/env python3
"""Launch pi inside the authoring sandbox with an owner-side pull worker."""

import argparse
import json
from pathlib import Path
import signal
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "bend-spec")]

from integrations.pi.owner import PiSession, Provider, SessionFailure, SupervisionError
from proofguard.session import ContractError


def main():
    """Select owner paths and supervise one explicit offline or provider session."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authority", type=Path, required=True)
    parser.add_argument("--enrollment", required=True)
    parser.add_argument("--root", type=Path, required=True, help="fresh owner-only session directory")
    parser.add_argument("--brief", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, help="explicit offline scripted SDK fixture, not a benchmark")
    parser.add_argument("--auth-dir", type=Path)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--deadline", type=float, default=300)
    args = parser.parse_args()
    provider = None
    if args.scenario:
        if any((args.auth_dir, args.provider, args.model)):
            parser.error("scenario mode forbids inference credentials/provider/model")
        scenario = json.loads(args.scenario.read_bytes())
    else:
        if not all((args.auth_dir, args.provider, args.model)):
            parser.error("provider mode requires owner --auth-dir, --provider and --model")
        scenario = None
        provider = Provider(args.auth_dir, args.provider, args.model)
    session = PiSession(args.authority, args.enrollment, args.root, args.brief.read_text(),
                        scenario=scenario, provider=provider, deadline=args.deadline)

    def cancel(_signum, _frame):
        raise SupervisionError(SessionFailure.CANCELLED)

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    report = session.run()
    print(json.dumps({"sdk_completed": report["sdk_completed"], "report": str(session.root / "report.json"),
                      "reconciliation_required": report["reconciliation_required"]}))
    return 0 if report["sdk_completed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContractError, ValueError, OSError):
        print("ProofGuard pi setup failed; no successful session is recorded.", file=sys.stderr)
        raise SystemExit(1)
