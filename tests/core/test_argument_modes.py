"""Entrypoint modes remain fixed by owner enrollment."""

import base64
from pathlib import Path
import tempfile
import time
import unittest

from bendspec.backend import Toolchain
from bendspec.model import ContractError
from proofguard.admission import BendAdmission
from proofguard.packages import Enrollment
from proofguard.plugin import Plugin


class ArgumentModeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_report_plugin_distinguishes_omitted_and_explicit_empty_owner_context(self):
        """Owner context selection never changes the application's fixed ABI."""
        root = Path(__file__).resolve().parents[2]
        plugin = Plugin.load(root / "examples/foundation/report")
        owner = self.root / "owner"
        owner.mkdir()
        target = self.root / "report"
        arguments = {"inputs_base64": [base64.b64encode(b"report").decode()],
                     "session": {"identifier": "argument-mode", "expires": int(time.time()) + 180,
                                 "destinations": [str(target)]}}
        source = "def main(inputs):\n  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}\n"
        explicit = plugin.with_owner_data(None).enroll(owner, **arguments)
        self.assertIn("owner_data", explicit.record["plugins"][0])
        explicit.build_gateway(owner).submit(source)
        self.assertEqual(target.read_bytes(), b"report")
        target.unlink()
        omitted = plugin.enroll(owner, **arguments)
        self.assertNotIn("owner_data", omitted.record["plugins"][0])
        restored = Enrollment.load(owner, omitted.identity)
        restored.build_gateway(owner).submit(source)
        self.assertEqual(target.read_bytes(), b"report")

    def test_one_argument_mode_rejects_wrong_input_return_and_extra_arguments(self):
        """Exercise actual signature checking without the private-owner argument."""
        root = Path(__file__).resolve().parents[2]
        bundle = Plugin.load(root / "tests/fixtures/selection").contract
        for body in (
            "def main(inputs: PGOwnerContext) -> PGIO:\n  PGDone{}\n",
            "def main(inputs: PGInputs) -> PGInputs:\n  inputs\n",
            "def main(inputs: PGInputs, owner: PGOwnerContext) -> PGIO:\n  PGDone{}\n",
        ):
            with self.subTest(body=body):
                admission = BendAdmission(bundle, "main", Toolchain.installed(), owner_argument=False)
                with self.assertRaises(ContractError):
                    admission.verify_program(body)
