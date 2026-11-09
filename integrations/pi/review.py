#!/usr/bin/env python3
"""Qualify the confined pi layer against its previously tested foundation."""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "bend-spec")]
from bendspec.backend import Toolchain


def qualification_module(name):
    """Load an owner-side layer qualifier for its source inventory."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fingerprints() -> dict:
    """Bind the foundation, every plugin the tests may load, and this harness."""
    result = qualification_module("review-tests").get_source_hashes()
    paths = [path for path in (ROOT / "plugins").rglob("*") if path.is_file() and "__pycache__" not in path.parts]
    for relative in ("integrations/pi", "integrations/pi/tests", "tests/composition"):
        paths.extend(path for path in (ROOT / relative).iterdir()
                     if path.suffix in (".mjs", ".json", ".py", ".md") and path.is_file())
    for path in paths:
        result[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(result.items()))


def main():
    """Run both pi suites and retain source-bound evidence without live inference."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    baseline = ROOT / "artifacts/review-stack/foundation.json"
    evidence = json.loads(baseline.read_bytes())
    if (not evidence["passed"] or not evidence["source_unchanged"] or
            evidence["source_sha256"] != qualification_module("review-tests").get_source_hashes()):
        raise ValueError("qualify the current foundation before the pi layer")
    before, toolchain = fingerprints(), Toolchain.installed().validate()
    if toolchain != evidence["toolchain"]:
        raise ValueError("foundation toolchain changed")
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT / "bend-spec"), str(ROOT))))
    commands = [[sys.executable, "-m", "unittest", "discover", "-s", "integrations/pi/tests", "-v"],
                [sys.executable, "-m", "unittest", "discover", "-s", "tests/composition", "-v"],
                ["/usr/bin/node", "--test", "integrations/pi/tests/proofguard.test.mjs"]]
    runs = []
    for command in commands:
        print("+", " ".join(command), flush=True)
        completed = subprocess.run(command, cwd=ROOT, env=environment, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
        print(completed.stdout, end="", flush=True)
        skipped = bool(re.search(r"\bskipped=[1-9]|# skipped [1-9]", completed.stdout))
        runs.append({"command": command, "exit_code": completed.returncode, "skipped": skipped,
                     "log": completed.stdout.replace(str(ROOT), "<checkout>")})
    unchanged = before == fingerprints() and toolchain == Toolchain.installed().validate()
    result = {"schema": 1, "layer": "pi", "time": datetime.now(timezone.utc).isoformat(),
              "passed": unchanged and all(run["exit_code"] == 0 and not run["skipped"] for run in runs),
              "source_unchanged": unchanged, "source_sha256": before, "toolchain": toolchain,
              "foundation_evidence_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
              "model_source": "explicit_scripted_sdk_fixture", "live_inference": False,
              "claim": "Actual SDK, sandbox, outbox, Bend verification and effects; not a live-model benchmark",
              "runs": runs}
    destination = ROOT / "artifacts/review-stack/pi.json"
    destination.write_text(json.dumps(result, indent=2) + "\n")
    print("Evidence:", destination.relative_to(ROOT), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
