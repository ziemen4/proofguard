#!/usr/bin/env python3
"""Run foundation/checker suites and retain source-bound local test evidence."""

import argparse
import codecs
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import signal
import selectors
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "bend-spec"), str(ROOT / "scripts")]
from bendspec.backend import Toolchain
from qualification import TestProfile, plugin_fingerprints, run_suite


def stream_suite(command, cwd, environment, timeout=3600):
    """Stream and retain output while bounding the complete process group."""
    chunks = []
    decoder = codecs.getincrementaldecoder("utf-8")()
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, cwd=cwd, env=environment,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=True) as process:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            timed_out = False
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    timed_out = True
                # Drain only ready bytes after timeout; a descendant retaining
                # stdout must not extend the suite's deadline.
                ready = selector.select(0 if timed_out else min(0.1, remaining))
                for key, _ in ready:
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    text = decoder.decode(data)
                    chunks.append(text)
                    print(text, end="", flush=True)
                if timed_out:
                    break
            status = 124 if timed_out else process.returncode
        final = decoder.decode(b"", final=True)
        chunks.append(final)
        print(final, end="", flush=True)
    return status, "".join(chunks)


def get_source_hashes():
    """Hash the foundation, shared checker and their tests for this run."""
    paths = []
    for directory in ("proofguard", "examples/foundation", "tests/core", "tests/fixtures",
                      "bend-spec/bendspec", "bend-spec/backend", "bend-spec/tests"):
        paths.extend(path for path in (ROOT / directory).rglob("*")
                     if path.suffix in (".py", ".mjs", ".bend", ".json"))
    for relative in ("tests/loopback.py", "scripts/review-tests.py", "scripts/qualification.py",
                     "bend-spec/scripts/setup-toolchain.py", "bend-spec/toolchain.lock.json"):
        path = ROOT / relative
        if path.is_file():
            paths.append(path)
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths) if path.is_file()}


def qualify_plugin(path):
    """Qualify a declared suite only against current foundation evidence."""
    profile = TestProfile.load(ROOT, path)
    baseline = ROOT / "artifacts/review-stack/foundation.json"
    raw = baseline.read_bytes()
    evidence = json.loads(raw)
    foundation = get_source_hashes()
    toolchain = Toolchain.installed().validate()
    if (not evidence["passed"] or not evidence["source_unchanged"]
            or evidence["source_sha256"] != foundation or evidence["toolchain"] != toolchain):
        raise ValueError("qualify the current foundation before the plugin")
    before = plugin_fingerprints(ROOT, profile, foundation)
    run = run_suite(ROOT, profile)
    print(run.log, end="", flush=True)
    unchanged = (before == plugin_fingerprints(ROOT, profile, get_source_hashes())
                 and toolchain == Toolchain.installed().validate())
    passed = run.exit_code == 0 and run.result["passed"] and unchanged
    cases = dict(profile.cases)
    record = {"schema": 1, "layer": "plugin", "plugin": profile.name,
              "time": datetime.now(timezone.utc).isoformat(), "passed": passed,
              "source_unchanged": unchanged, "source_sha256": before, "toolchain": toolchain,
              "foundation_evidence_sha256": hashlib.sha256(raw).hexdigest(),
              "exit_code": run.exit_code, "test_count": run.result["test_count"], "log": run.log,
              "case_map": cases, "missing_tests": run.result["missing_tests"],
              "qualified_cases": len(cases) if passed else 0, "full_plugin_qualified": passed,
              "live_inference": False, "zkp": False, "tee": False}
    destination = ROOT / "artifacts/review-stack/plugins" / f"{profile.name}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(record, indent=2) + "\n")
    print("Evidence:", destination.relative_to(ROOT), flush=True)
    return 0 if passed else 1


def main():
    """Run the installed checker suites and record source/toolchain stability."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layer", choices=("foundation", "plugin"))
    parser.add_argument("profile", nargs="?")
    args = parser.parse_args()
    if args.layer == "plugin":
        if args.profile is None:
            parser.error("plugin requires a repository-relative test profile")
        return qualify_plugin(args.profile)
    if args.profile is not None:
        parser.error("foundation does not accept a plugin profile")
    before, toolchain = get_source_hashes(), Toolchain.installed().validate()
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT / "bend-spec"), str(ROOT))))
    commands = [
        (ROOT, [sys.executable, "-m", "unittest", "discover", "-s", "tests/core", "-v"]),
        (ROOT / "bend-spec", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]),
    ]
    results = []
    for cwd, command in commands:
        print("+", " ".join(command), flush=True)
        status, log = stream_suite(command, cwd, environment)
        results.append({"cwd": str(cwd.relative_to(ROOT)), "command": command,
                        "exit_code": status,
                        "log": log.replace(str(ROOT), "<checkout>")})
    unchanged = before == get_source_hashes() and toolchain == Toolchain.installed().validate()
    record = {"schema": 1, "layer": args.layer, "time": datetime.now(timezone.utc).isoformat(),
              "passed": unchanged and all(result["exit_code"] == 0 for result in results),
              "source_unchanged": unchanged, "source_sha256": before, "toolchain": toolchain,
              "toolchain_provisioning_retested": False, "live_inference": False,
              "zkp": False, "tee": False, "runs": results}
    destination = ROOT / "artifacts/review-stack" / f"{args.layer}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(record, indent=2) + "\n")
    print("Evidence:", destination.relative_to(ROOT), flush=True)
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
