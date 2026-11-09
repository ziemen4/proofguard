"""Real program-only mandatory law checking."""

import json
from pathlib import Path
import tempfile
import unittest

from bendspec.backend import Toolchain, verify
from bendspec.model import Bundle, ContractError, Project, Source
from fixtures import capture_project, write_identity_contract


CONTRACT = """type Choice is Data:
  Good{}
  Bad{}

law plan:
  Choice

law permitted:
  {plan() == Good{} : Choice}
"""
PROGRAM = "def plan():\n  Good{}\n"


class AutomaticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.toolchain = Toolchain.installed()
        self.bundle = Bundle(Project("choice", "Only Good may execute", ("spec/choice.bend",),
                                     ("plan",), ("permitted",)),
                             (Source("spec/choice.bend", CONTRACT),))

    def check(self, source=PROGRAM, *, automatic=True):
        return verify(self.bundle, (Source("candidate/program.bend", source),), self.toolchain,
                      automatic_obligations=automatic)

    def test_program_only_passes_real_source_and_kernel(self):
        result = self.check()
        self.assertEqual(result["status"], "accepted", result)
        self.assertIs(result["inputs"]["automatic_obligations"], True)
        self.assertEqual(result["inputs"]["candidate"][0]["content"], PROGRAM)
        self.assertEqual(result["kernel_check"]["diagnostics"].strip(), "ALL PROOFS CHECK")
        self.assertIn("permitted", result["translation"]["text"])

    def test_false_program_rejects_without_author_proof_or_check_call(self):
        result = self.check(PROGRAM.replace("Good", "Bad"))
        self.assertEqual(result["status"], "rejected")
        stage = result["source_check"]
        self.assertFalse(stage["timed_out"])
        diagnostic = json.loads(stage["diagnostics"])
        self.assertEqual(diagnostic["phase"], "check_definitions")
        self.assertEqual(diagnostic["definition"], "permitted")
        self.assertNotIn("kernel_check", result)

    def test_candidate_cannot_author_or_replace_managed_proof(self):
        for definition in ("def permitted():\n  {==}\n", "law permitted:\n  Choice\n",
                           "def permitted() -> Choice:\n  Good{}\n"):
            with self.subTest(definition=definition):
                result = self.check(PROGRAM + definition)
                self.assertEqual(result["status"], "rejected")
                diagnostic = json.loads(result["source_check"]["diagnostics"])
                self.assertEqual(diagnostic["phase"], "parse_candidate")
                if definition.startswith("def permitted():"):
                    self.assertIn("Gate-managed obligation", diagnostic["message"])

    def test_helpers_remain_program_code(self):
        source = "def choose() -> Choice:\n  Good{}\n\ndef plan():\n  choose()\n"
        self.assertEqual(self.check(source)["status"], "accepted")

    def test_unsafe_and_holes_reject_in_automatic_mode(self):
        for source in (PROGRAM.replace("def plan", "@unsafe def plan"),
                       PROGRAM.replace("Good{}", "?missing")):
            with self.subTest(source=source):
                self.assertEqual(self.check(source)["status"], "rejected")

    def test_manual_mode_still_requires_an_author_proof(self):
        self.assertEqual(self.check(automatic=False)["status"], "rejected")
        result = self.check(PROGRAM + "\ndef permitted():\n  {==}\n", automatic=False)
        self.assertEqual(result["status"], "accepted", result)
        self.assertNotIn("automatic_obligations", result["inputs"])

    def test_quantified_laws_are_not_silently_solved(self):
        project = self.root / "quantified"
        write_identity_contract(project)
        result = verify(capture_project(project), (Source("candidate/code.bend", "def implement(n):\n  n\n"),),
                        self.toolchain, automatic_obligations=True)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("nullary equality laws", result["source_check"]["diagnostics"])

    def test_malformed_modes_cannot_bypass_verification(self):
        for mode in (1, "true", None):
            with self.subTest(mode=mode), self.assertRaises(ContractError):
                verify(self.bundle, (Source("candidate/program.bend", PROGRAM),),
                       self.toolchain, automatic_obligations=mode)
