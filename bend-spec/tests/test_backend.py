"""Real Bend and Lean-compiled BendTT admission tests; no fake proof backend."""

import json
from pathlib import Path
import tempfile
import unittest

from bendspec.backend import Toolchain, execute, verify
from bendspec.model import ContractError, Source
from fixtures import capture_project, write_identity_contract


GOOD = "def implement(n):\n  n\n\ndef refines(n):\n  {==}\n"


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Missing tooling is a failure, not a skipped test reported as qualification.
        cls.toolchain = Toolchain.installed()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        write_identity_contract(self.project)
        self.bundle = capture_project(self.project)

    def check(self, source):
        return verify(self.bundle, (Source("candidate/code.bend", source),), self.toolchain, 10)

    def test_universal_identity_proof_passes_both_checkers(self):
        receipt = self.check(GOOD)
        self.assertEqual(receipt["status"], "accepted", receipt)
        self.assertEqual(receipt["kernel_check"]["diagnostics"].strip(), "ALL PROOFS CHECK")
        self.assertIn("refines", receipt["translation"]["text"])
        self.assertEqual(receipt["inputs"]["candidate"][0]["content"], GOOD)

    def test_false_implementation_rejected(self):
        receipt = self.check(GOOD.replace("  n\n", "  Zero{}\n"))
        self.assertEqual(receipt["status"], "rejected")

    def test_missing_proof_rejected(self):
        receipt = self.check("def implement(n):\n  n\n")
        self.assertEqual(receipt["status"], "rejected")
        self.assertIn("Unfilled definition: refines", receipt["source_check"]["diagnostics"])

    def test_missing_implementation_rejected(self):
        self.assertEqual(self.check("def refines(n):\n  {==}\n")["status"], "rejected")

    def test_reference_override_rejected(self):
        self.assertEqual(self.check("def reference(n: Natural) -> Natural:\n  Zero{}\n" + GOOD)["status"], "rejected")

    def test_datatype_override_rejected(self):
        self.assertEqual(self.check("type Natural is Data:\n  Broken{}\n" + GOOD)["status"], "rejected")

    def test_law_override_rejected(self):
        self.assertEqual(self.check("law refines:\n  Natural\n" + GOOD)["status"], "rejected")

    def test_unsafe_both_syntax_forms_rejected(self):
        for changed in (GOOD.replace("def implement", "@unsafe def implement"),
                        GOOD.replace("implement(n)", "implement?(n)")):
            with self.subTest(source=changed):
                result = self.check(changed)
                self.assertEqual(result["status"], "rejected")
                self.assertIn("Unsafe", result["source_check"]["diagnostics"])

    def test_hole_rejected(self):
        self.assertEqual(self.check(GOOD.replace("{==}", "?TODO"))["status"], "rejected")

    def test_extra_open_law_rejected(self):
        self.assertEqual(self.check(GOOD + "\nlaw loophole:\n  Natural\n")["status"], "rejected")

    def test_import_does_not_read_host_file(self):
        secret = self.root / "secret.bend"
        secret.write_text("do not read")
        result = self.check(f'import "{secret}"\n' + GOOD)
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("do not read", json.dumps(result["source_check"]))

    def test_helpers_are_permitted(self):
        helper = "def keep(n: Natural) -> Natural:\n  n\n\n"
        result = self.check(helper + GOOD.replace("  n\n", "  keep(n)\n"))
        self.assertEqual(result["status"], "accepted", result)

    def test_candidate_cannot_supply_protected_law_dependency(self):
        path = self.project / "spec/system.bend"
        path.write_text(path.read_text().replace("== reference(n)", "== policy(n)"))
        self.bundle = capture_project(self.project)
        candidate = "def policy(n: Natural) -> Natural:\n  Zero{}\n\n" + GOOD.replace("  n\n", "  Zero{}\n")
        result = self.check(candidate)
        self.assertEqual(result["status"], "rejected", result)
        self.assertIn("Undeclared protected dependency: policy", result["source_check"]["diagnostics"])

    def test_candidate_cannot_supply_protected_datatype(self):
        path = self.project / "spec/system.bend"
        path.write_text(path.read_text() + "\nlaw dependent:\n  for value: Hidden\n  Hidden\n")
        manifest = self.project / "contract.json"
        data = json.loads(manifest.read_text())
        data["implementations"].append("dependent")
        manifest.write_text(json.dumps(data))
        self.bundle = capture_project(self.project)
        result = self.check("type Hidden is Data:\n  Secret{}\n\ndef dependent(value):\n  value\n" + GOOD)
        self.assertEqual(result["status"], "rejected", result)
        self.assertIn("Undeclared protected dependency: Hidden", result["source_check"]["diagnostics"])

    def test_undeclared_contract_slot_is_rejected(self):
        path = self.project / "spec/system.bend"
        path.write_text(path.read_text() + "\nlaw omitted:\n  Natural\n")
        self.bundle = capture_project(self.project)
        result = self.check(GOOD + "\ndef omitted():\n  Zero{}\n")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("slots", result["source_check"]["diagnostics"])

    def test_toolchain_substitution_rejected(self):
        wrong = Toolchain(self.toolchain.source, self.toolchain.kernel, "0" * 64)
        with self.assertRaisesRegex(ContractError, "kernel binary mismatch"):
            verify(self.bundle, (Source("candidate/code.bend", GOOD),), wrong)

    def test_timeout_is_not_acceptance(self):
        result = verify(self.bundle, (Source("candidate/code.bend", GOOD),), self.toolchain, 0.000001)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(result["source_check"]["timed_out"])

    def test_nonfinite_timeout_rejected(self):
        with self.assertRaises(ContractError):
            verify(self.bundle, (Source("candidate/code.bend", GOOD),), self.toolchain, float("nan"))

    def test_external_environment_cannot_replace_kernel(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {"BENDTT": "/bin/true", "NODE_OPTIONS": "--invalid"}):
            self.assertEqual(self.check(GOOD)["status"], "accepted")

    def test_kernel_rejects_false_raw_certificate(self):
        raw = self.root / "bad.bendtt"
        raw.write_text("false_proof : <> = ()\n")
        result = execute([str(self.toolchain.kernel), str(raw)], b"", self.root, 10, kernel=True)
        self.assertNotEqual(result.status, 0)


if __name__ == "__main__":
    unittest.main()
