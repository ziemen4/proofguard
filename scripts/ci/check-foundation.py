#!/usr/bin/env python3
"""Require real prerequisites and complete, stable, skip-free qualification."""

import hashlib
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "bend-spec")]
from bendspec.backend import Toolchain


def load_runner():
    spec = importlib.util.spec_from_file_location("review_tests", ROOT / "scripts/review-tests.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def check_report(record, source_hashes, toolchain):
    """Do not accept empty discovery, skipped tests, or stale source identities."""
    if (record["schema"] != 1 or record["layer"] != "foundation"
            or record["passed"] is not True or record["source_unchanged"] is not True
            or record["source_sha256"] != source_hashes or record["toolchain"] != toolchain):
        raise RuntimeError("qualification failed or source/toolchain identity changed")
    runs = record["runs"]
    if [run["cwd"] for run in runs] != [".", "bend-spec"]:
        raise RuntimeError("foundation and shared suites are both required")
    for run in runs:
        log = run["log"]
        summaries = re.findall(r"^Ran (\d+) tests? in .+$", log, re.MULTILINE)
        if (run["exit_code"] != 0 or len(summaries) != 1 or int(summaries[0]) == 0
                or not re.search(r"^OK$", log, re.MULTILINE)
                or re.search(r"\bskipped\b|\bexpected failure\b", log, re.IGNORECASE)):
            raise RuntimeError(f"suite failed, empty, or skipped: {run['cwd']}")


def namespace_probe():
    """Exercise the same bwrap user/PID/network namespace flags as AuthorSandbox."""
    subprocess.run([
        "/usr/bin/bwrap", "--die-with-parent", "--new-session", "--unshare-all",
        "--unshare-user", "--disable-userns", "--cap-drop", "ALL", "--clearenv",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
        "--proc", "/proc", "--dev", "/dev", "--", "/usr/bin/true",
    ], check=True, timeout=20)


def check_plugin_report(record, profile, source_hashes, toolchain, baseline):
    """Require complete declared plugin coverage bound to this foundation run."""
    cases = dict(profile.cases)
    if (record.get("schema") != 1 or record.get("layer") != "plugin"
            or record.get("plugin") != profile.name or record.get("passed") is not True
            or record.get("full_plugin_qualified") is not True
            or record.get("source_unchanged") is not True or record.get("exit_code") != 0
            or record.get("source_sha256") != source_hashes or record.get("toolchain") != toolchain
            or record.get("foundation_evidence_sha256") != hashlib.sha256(baseline).hexdigest()
            or record.get("case_map") != cases or record.get("qualified_cases") != len(cases)
            or record.get("missing_tests") != [] or type(record.get("test_count")) is not int
            or record["test_count"] < max(1, len(cases))):
        raise RuntimeError("plugin qualification is incomplete or stale")


def check_plugins():
    """Run each package's test profile after verifying current foundation evidence."""
    runner = load_runner()
    baseline = (ROOT / "artifacts/review-stack/foundation.json").read_bytes()
    toolchain = Toolchain.installed().validate()
    foundation = runner.get_source_hashes()
    check_report(json.loads(baseline), foundation, toolchain)
    profiles = sorted(ROOT.glob("plugins/*/tests.json"))
    if not profiles:
        raise RuntimeError("no plugin test profiles in this checkout")
    for path in profiles:
        relative = str(path.relative_to(ROOT))
        profile = runner.TestProfile.load(ROOT, relative)
        before = runner.plugin_fingerprints(ROOT, profile, foundation)
        subprocess.run([sys.executable, str(ROOT / "scripts/review-tests.py"), "plugin", relative],
                       cwd=ROOT, check=True, timeout=1850)
        record = json.loads((ROOT / "artifacts/review-stack/plugins" / f"{profile.name}.json").read_bytes())
        check_plugin_report(record, profile, before, toolchain, baseline)
        if (before != runner.plugin_fingerprints(ROOT, profile, runner.get_source_hashes())
                or toolchain != Toolchain.installed().validate()):
            raise RuntimeError("plugin source or toolchain changed during CI")
        print(f"Plugin {profile.name}: {record['test_count']} tests; {len(profile.cases)} declared cases passed.", flush=True)


def main(plugins=False):
    if plugins:
        return check_plugins()
    runner = load_runner()
    destination = ROOT / "artifacts/review-stack/foundation.json"
    # A failed prerequisite must never leave a previous successful report in place.
    destination.unlink(missing_ok=True)
    toolchain = Toolchain.installed().validate()
    namespace_probe()
    before = runner.get_source_hashes()
    support = sorted((ROOT / "scripts/ci").glob("*.*")) + [
        ROOT / ".github/workflows/foundation.yml"]
    support_hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in support}
    logs = ROOT / ".tmp/ci-logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "source.json").write_text(json.dumps({
        "source_sha256": before, "ci_sha256": support_hashes, "toolchain": toolchain,
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
    }, indent=2) + "\n")
    subprocess.run([sys.executable, str(ROOT / "scripts/review-tests.py"), "foundation"],
                   cwd=ROOT, check=True, timeout=3650)
    check_report(json.loads(destination.read_text()), before, toolchain)
    if (before != runner.get_source_hashes() or toolchain != Toolchain.installed().validate()
            or any(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest
                   for name, digest in support_hashes.items())):
        raise RuntimeError("source or toolchain changed during CI")
    print("Both real suites passed without skips; source and toolchain identities match.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugins", action="store_true", help="qualify plugin profiles after foundation")
    main(parser.parse_args().plugins)
