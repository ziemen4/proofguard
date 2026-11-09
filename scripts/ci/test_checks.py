"""Fast regression tests for CI acceptance and streaming, without running suites."""

import contextlib
import copy
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("ci_checks", Path(__file__).with_name("check-foundation.py"))
CHECKS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKS)


class ChecksTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "schema": 1, "layer": "foundation", "passed": True, "source_unchanged": True,
            "source_sha256": {"source.py": "abc"}, "toolchain": {"kernel": "def"},
            "runs": [{"cwd": cwd, "exit_code": 0, "log": "Ran 2 tests in 0.01s\n\nOK\n"}
                     for cwd in (".", "bend-spec")],
        }

    def check(self, record):
        CHECKS.check_report(record, self.record["source_sha256"], self.record["toolchain"])

    def test_complete_report(self):
        self.check(self.record)

    def test_rejects_bad_or_stale_reports(self):
        changes = {"schema": 2, "layer": "other", "passed": False, "source_unchanged": False,
                   "source_sha256": {}, "toolchain": {}, "runs": self.record["runs"][:1]}
        for key, value in changes.items():
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.check({**self.record, key: value})

    def test_rejects_empty_skipped_expected_failure_and_failed_suites(self):
        for log in ("Ran 0 tests in 0s\nOK\n", "Ran 2 tests in 0s\nOK (skipped=1)\n",
                    "test (suite) ... skipped 'missing toolchain'\nRan 2 tests in 0s\nOK\n",
                    "Ran 2 tests in 0s\nOK (expected failures=1)\n", "FAILED\n", ""):
            record = copy.deepcopy(self.record)
            record["runs"][0]["log"] = log
            with self.subTest(log=log), self.assertRaises(RuntimeError):
                self.check(record)
        self.record["runs"][0]["exit_code"] = 1
        with self.assertRaises(RuntimeError):
            self.check(self.record)

    def test_namespace_failure_is_not_skipped(self):
        with patch.object(CHECKS.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "bwrap")):
            with self.assertRaises(subprocess.CalledProcessError):
                CHECKS.namespace_probe()

    def test_missing_toolchain_fails_before_launching_suites(self):
        with patch.object(CHECKS.Toolchain, "installed", side_effect=RuntimeError("not provisioned")), \
                patch.object(CHECKS.Path, "unlink"), patch.object(CHECKS.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "not provisioned"):
                CHECKS.main()
            run.assert_not_called()

    def test_streaming_retains_stdout_and_stderr_before_exit(self):
        runner = CHECKS.load_runner()
        # The child waits for the collector to acknowledge its first line. A
        # buffered implementation would time out rather than print it early.
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "observed"

            class Observed(io.StringIO):
                def write(self, value):
                    if "progress" in value:
                        marker.touch()
                    return super().write(value)

            output = Observed()
            source = ("import pathlib,time,sys; print('progress',flush=True); "
                      f"p=pathlib.Path({str(marker)!r}); "
                      "\nwhile not p.exists(): time.sleep(.01)\n"
                      "print('diagnostic',file=sys.stderr,flush=True)")
            with contextlib.redirect_stdout(output):
                status, log = runner.stream_suite([sys.executable, "-c", source], CHECKS.ROOT, dict(os.environ), 5)
        self.assertEqual(status, 0)
        self.assertEqual(log, "progress\ndiagnostic\n")
        self.assertEqual(output.getvalue(), log)

    def test_deadline_retains_partial_log(self):
        with contextlib.redirect_stdout(io.StringIO()):
            status, log = CHECKS.load_runner().stream_suite(
                [sys.executable, "-c", "import time; print('started',flush=True); time.sleep(30)"],
                CHECKS.ROOT, dict(os.environ), .2)
        self.assertEqual(status, 124)
        self.assertEqual(log, "started\n")

    def test_descendant_holding_stdout_does_not_extend_deadline(self):
        source = ("import subprocess,sys; "
                  "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                  "print('parent exited',flush=True)")
        with contextlib.redirect_stdout(io.StringIO()):
            status, log = CHECKS.load_runner().stream_suite(
                [sys.executable, "-c", source], CHECKS.ROOT, dict(os.environ), .2)
        self.assertEqual(status, 124)
        self.assertEqual(log, "parent exited\n")

    def test_runner_imports_in_isolated_python_from_another_directory(self):
        source = ("import importlib.util; "
                  f"s=importlib.util.spec_from_file_location('runner',{str(CHECKS.ROOT / 'scripts/review-tests.py')!r}); "
                  "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                  "assert m.run_suite.__module__=='qualification'; "
                  "assert m.run_suite is not m.stream_suite")
        completed = subprocess.run([sys.executable, '-I', '-c', source], cwd='/tmp',
                                   text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_complete_plugin_report_and_source_binding(self):
        profile = SimpleNamespace(name='sample', cases=(('case', 'suite.test_case'),))
        baseline = b'foundation fixture'
        record = {'schema': 1, 'layer': 'plugin', 'plugin': profile.name, 'passed': True,
                  'full_plugin_qualified': True, 'source_unchanged': True, 'exit_code': 0,
                  'source_sha256': {'program.bend': 'abc'}, 'toolchain': {'kernel': 'def'},
                  'foundation_evidence_sha256': hashlib.sha256(baseline).hexdigest(),
                  'case_map': dict(profile.cases), 'qualified_cases': 1, 'missing_tests': [], 'test_count': 1}
        def check(value, selected_baseline=baseline):
            CHECKS.check_plugin_report(value, profile, record['source_sha256'], record['toolchain'], selected_baseline)
        check(record)
        for key, value in {'passed': False, 'full_plugin_qualified': False, 'source_unchanged': False,
                           'exit_code': 1, 'plugin': 'other', 'case_map': {}, 'qualified_cases': 0,
                           'source_sha256': {}, 'toolchain': {}, 'missing_tests': ['suite.test_case'],
                           'test_count': 0, 'foundation_evidence_sha256': 'old'}.items():
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                check({**record, key: value})
        with self.assertRaises(RuntimeError):
            check(record, b'changed foundation')


if __name__ == "__main__":
    unittest.main()
