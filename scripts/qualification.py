"""Declarative plugin test profiles and isolated unittest execution."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import unittest


def relative_path(value: object) -> str:
    """Keep profile paths inside the checkout with canonical relative spelling."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("repository-relative path required")
    path = PurePosixPath(value)
    if value == "." or path.is_absolute() or str(path) != value or any(part in (".", "..") for part in path.parts):
        raise ValueError("canonical repository-relative path required")
    return value


@dataclass(frozen=True)
class TestProfile:
    name: str
    path: str
    tests: str
    sources: tuple[str, ...]
    cases: tuple[tuple[str, str], ...]

    @classmethod
    def load(cls, root: Path, path: str) -> "TestProfile":
        """Read owner test configuration without a plugin-specific adapter."""
        from bendspec.model import decode, fields
        path = relative_path(path)
        selected = root / path
        if selected.is_symlink() or not selected.resolve().is_relative_to(root.resolve()):
            raise ValueError("test profile must stay inside the checkout")
        row = fields(decode(selected.read_bytes()), {"schema", "name", "tests", "sources", "cases"},
                     "plugin test profile")
        if type(row["schema"]) is not int or row["schema"] != 1:
            raise ValueError("unsupported test profile schema")
        if not isinstance(row["name"], str) or not re.fullmatch(r"[a-z][a-z0-9-]*", row["name"]):
            raise ValueError("test profile name must be a lowercase slug")
        if type(row["sources"]) is not list or not row["sources"]:
            raise ValueError("test profile requires source paths")
        sources = tuple(relative_path(item) for item in row["sources"])
        if len(set(sources)) != len(sources):
            raise ValueError("duplicate test profile source")
        if type(row["cases"]) is not dict or any(
            not isinstance(case, str) or not case or not isinstance(test, str) or not test
            for case, test in row["cases"].items()
        ) or len(set(row["cases"].values())) != len(row["cases"]):
            raise ValueError("case IDs must name distinct tests")
        result = cls(row["name"], path, relative_path(row["tests"]), sources, tuple(row["cases"].items()))
        if not (root / result.tests).is_dir():
            raise ValueError("test suite directory is missing")
        result.fingerprints(root)
        return result

    def fingerprints(self, root: Path) -> dict[str, str]:
        """Bind the profile, test suite and declared sources to the run."""
        result = {}
        for relative in (self.path, self.tests, *self.sources):
            selected = root / relative
            if selected.is_symlink() or not selected.resolve().is_relative_to(root.resolve()):
                raise ValueError("test sources must stay inside the checkout without symlinks")
            if not selected.exists():
                raise ValueError("declared test source is missing: " + relative)
            paths = selected.rglob("*") if selected.is_dir() else (selected,)
            for path in paths:
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError("test sources cannot contain symlinks or leave the checkout")
                if (path.is_file() and "__pycache__" not in path.parts
                        and path.suffix not in (".pyc", ".pyo")):
                    result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return dict(sorted(result.items()))


def test_ids(suite):
    """Capture discovered IDs before unittest consumes its suite."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_ids(item)
        else:
            yield item.id()


def execute_suite(request: Path, report: Path) -> int:
    """Collect and run tests in a fresh process, rejecting incomplete coverage."""
    row = json.loads(request.read_bytes())
    suite = unittest.defaultTestLoader.discover(row["tests"])
    identifiers = list(test_ids(suite))
    missing = sorted(set(row["cases"].values()) - set(identifiers))
    if missing or len(set(identifiers)) != len(identifiers) or not identifiers:
        report.write_text(json.dumps({"passed": False, "ids": identifiers, "missing_tests": missing,
                                      "test_count": 0}))
        return 1
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    complete = not (result.skipped or result.expectedFailures or result.unexpectedSuccesses)
    passed = result.wasSuccessful() and complete and result.testsRun == len(identifiers)
    report.write_text(json.dumps({"passed": passed, "ids": identifiers, "missing_tests": [],
                                  "test_count": result.testsRun, "skipped": len(result.skipped),
                                  "expected_failures": len(result.expectedFailures),
                                  "failures": len(result.failures), "errors": len(result.errors)}))
    return 0 if passed else 1


@dataclass(frozen=True)
class SuiteRun:
    exit_code: int
    log: str
    result: dict


def run_suite(root: Path, profile: TestProfile, timeout: float = 1800) -> SuiteRun:
    """Keep test imports out of the qualification supervisor's process."""
    with tempfile.TemporaryDirectory(prefix="proofguard-tests-") as temporary:
        request, report = Path(temporary) / "request.json", Path(temporary) / "result.json"
        request.write_text(json.dumps({"tests": str(root / profile.tests), "cases": dict(profile.cases)}))
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root), str(root / "bend-spec"))))
        command = [sys.executable, str(Path(__file__).resolve()), str(request), str(report)]
        try:
            completed = subprocess.run(command, cwd=root, env=environment, text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
            status, log = completed.returncode, completed.stdout
        except subprocess.TimeoutExpired as error:
            status, log = 124, error.stdout or b""
            if isinstance(log, bytes):
                log = log.decode(errors="replace")
        result = json.loads(report.read_bytes()) if report.is_file() else {
            "passed": False, "test_count": 0, "missing_tests": [], "ids": []}
        return SuiteRun(status, log.replace(str(root), "<checkout>"), result)


def plugin_fingerprints(root: Path, profile: TestProfile, foundation: dict) -> dict:
    """Combine foundation and plugin identities without application-specific rules."""
    return dict(sorted({**foundation, **profile.fingerprints(root)}.items()))


if __name__ == "__main__":
    raise SystemExit(execute_suite(Path(sys.argv[1]), Path(sys.argv[2])))
