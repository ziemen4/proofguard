"""Generic qualification rejects incomplete suites and binds declared inputs."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from qualification import TestProfile, plugin_fingerprints, run_suite

GOOD = "import unittest\nclass Checks(unittest.TestCase):\n    def test_example(self):\n        self.assertTrue(True)\n"
IDENTIFIER = "test_checks.Checks.test_example"


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.suite = self.root / "tests"
        self.suite.mkdir()
        self.source = self.root / "library.bend"
        self.source.write_text("type Example is Data:\n  Example{}\n")
        self.test = self.suite / "test_checks.py"
        self.test.write_text(GOOD)
        self.path = self.root / "tests.json"
        self.row = {"schema": 1, "name": "example", "tests": "tests", "sources": ["library.bend"],
                    "cases": {"example-case": IDENTIFIER}}
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.row))

    def load(self):
        return TestProfile.load(self.root, "tests.json")

    def test_success_records_actual_ids_counts_and_coverage(self):
        run = run_suite(self.root, self.load())
        self.assertEqual(run.exit_code, 0, run.log)
        self.assertTrue(run.result["passed"])
        self.assertEqual(run.result["test_count"], 1)
        self.assertEqual(run.result["ids"], [IDENTIFIER])
        self.assertEqual(run.result["missing_tests"], [])

    def test_failed_skipped_and_expected_failure_tests_cannot_qualify(self):
        variants = (GOOD.replace("assertTrue(True)", "assertTrue(False)"),
                    GOOD.replace("    def test_example", "    @unittest.skip('not executed')\n    def test_example"),
                    GOOD.replace("    def test_example", "    @unittest.expectedFailure\n    def test_example")
                        .replace("assertTrue(True)", "assertTrue(False)"))
        for content in variants:
            with self.subTest(content=content):
                self.test.write_text(content)
                run = run_suite(self.root, self.load())
                self.assertNotEqual(run.exit_code, 0)
                self.assertFalse(run.result["passed"])

    def test_case_mapping_must_match_discovered_tests(self):
        self.row["cases"] = {"missing": "test_checks.Checks.test_missing"}
        self.save()
        run = run_suite(self.root, self.load())
        self.assertNotEqual(run.exit_code, 0)
        self.assertEqual(run.result["missing_tests"], ["test_checks.Checks.test_missing"])
        self.assertEqual(run.result["test_count"], 0)

    def test_empty_or_broken_suite_cannot_qualify(self):
        self.row["cases"] = {}
        self.save()
        for content in ("", "raise RuntimeError('broken discovery')\n"):
            with self.subTest(content=content):
                self.test.write_text(content)
                run = run_suite(self.root, self.load())
                self.assertNotEqual(run.exit_code, 0)
                self.assertFalse(run.result["passed"])

    def test_timeout_is_not_success(self):
        self.test.write_text(GOOD.replace("self.assertTrue(True)", "__import__('time').sleep(10)"))
        run = run_suite(self.root, self.load(), timeout=0.05)
        self.assertEqual(run.exit_code, 124)
        self.assertFalse(run.result["passed"])

    def test_fingerprints_include_profile_tests_sources_and_foundation(self):
        profile = self.load()
        before = plugin_fingerprints(self.root, profile, {"foundation.py": "fixed"})
        self.assertEqual(set(before), {"tests.json", "tests/test_checks.py", "library.bend", "foundation.py"})
        self.source.write_text(self.source.read_text() + "# changed\n")
        self.assertNotEqual(before, plugin_fingerprints(self.root, profile, {"foundation.py": "fixed"}))

    def test_profiles_without_case_inventory_are_supported(self):
        self.row["cases"] = {}
        self.row["name"] = "different-product"
        self.save()
        self.assertTrue(run_suite(self.root, self.load()).result["passed"])

    def test_declared_source_types_are_not_limited_to_bend_or_python(self):
        (self.root / "policy.yaml").write_text("enabled: true\n")
        self.row["sources"] = ["policy.yaml"]
        self.save()
        self.assertIn("policy.yaml", self.load().fingerprints(self.root))

    def test_malformed_or_duplicate_configuration_rejects(self):
        variants = ({"schema": True}, {"name": "../outside"}, {"tests": "../tests"},
                    {"sources": []}, {"sources": ["library.bend", "library.bend"]},
                    {"cases": {"one": IDENTIFIER, "two": IDENTIFIER}}, {"extra": True})
        for changed in variants:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.path.write_text(json.dumps(dict(self.row, **changed)))
                self.load()
        self.path.write_text('{"schema":1,"schema":1}')
        with self.assertRaises(ValueError):
            self.load()

    def test_source_paths_cannot_escape_or_follow_symlinks(self):
        for source in ("../outside", "/etc/passwd", "./library.bend", "missing.bend"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.path.write_text(json.dumps(dict(self.row, sources=[source])))
                self.load()
        self.save()
        self.source.rename(self.root / "actual.bend")
        self.source.symlink_to(self.root / "actual.bend")
        with self.assertRaises(ValueError):
            self.load()

    def test_duplicate_discovered_ids_reject(self):
        self.test.write_text(GOOD + "\ndef load_tests(loader, tests, pattern):\n"
                            "    return unittest.TestSuite([Checks('test_example'), Checks('test_example')])\n")
        run = run_suite(self.root, self.load())
        self.assertFalse(run.result["passed"])
        self.assertEqual(run.result["test_count"], 0)
