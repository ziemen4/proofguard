"""A fixed agent interface and private checks meet at the real Bend gateway."""

import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from proofguard.admission import AdmissionRejected
from proofguard.plugin import Plugin
from proofguard.packages import Enrollment
from proofguard.worker import OutboxWorker, Status, write_proposal

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "examples/foundation/report"
GOOD = (PACKAGE / "program.bend").read_text()
BAD = "def main(inputs):\n  PGDone{}\n"


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.owner = self.root / "owner"
        self.owner.mkdir()
        self.target = self.root / "report"
        self.session = {"identifier": "plugin", "expires": int(time.time()) + 240,
                        "destinations": [str(self.target)]}
        self.plugin = Plugin.load(PACKAGE)
        self.selected = self.plugin.enroll(self.owner, inputs_base64=[base64.b64encode(b"private report").decode()],
                                           session=self.session)

    def test_fixed_interface_excludes_plugin_sources_and_private_configuration(self):
        task = self.selected.get_task(self.owner)
        exported = json.dumps(task)
        self.assertIn("law main", exported)
        self.assertEqual([source['path'] for source in task['interface']],
                         ['proofguard/effects.bend', 'proofguard/program.bend'])
        for private in ("write_report", "owner_report", "checks.bend", "private report", str(self.target), "gate_managed_laws"):
            self.assertNotIn(private, exported)
        self.assertNotIn("contract", task)
        self.assertFalse(self.target.exists())

    def test_agent_helpers_and_datatypes_execute_after_private_law_check(self):
        source = """type Choice is Data:
  Publish{}

def choose(value: Choice, inputs: PGInputs) -> PGIO:
  match value:
    case Publish{}:
      pg_write(PGZero{}, pg_input(PGZero{}, inputs))

def main(inputs):
  choose(Publish{}, inputs)
"""
        gateway = self.selected.build_gateway(self.owner)
        gateway.check(source)
        self.assertFalse(self.target.exists())
        gateway.submit(source)
        self.assertEqual(self.target.read_bytes(), b"private report")

    def test_agent_uses_foundation_io_without_plugin_helpers(self):
        source = "def main(inputs):\n  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}\n"
        self.selected.build_gateway(self.owner).submit(source)
        self.assertEqual(self.target.read_bytes(), b"private report")

    def test_missing_law_in_submission_does_not_remove_gateway_check(self):
        gateway = self.selected.build_gateway(self.owner)
        with patch("proofguard.admission.execute", side_effect=AssertionError("rejected program executed")):
            with self.assertRaises(AdmissionRejected):
                gateway.submit(BAD)
        self.assertFalse(self.target.exists())
        restored = Enrollment.load(self.owner, self.selected.identity)
        restored.build_gateway(self.owner).submit(GOOD)
        self.assertEqual(self.target.read_bytes(), b"private report")

    def test_agent_cannot_replace_private_helper_or_supply_private_proof(self):
        gateway = self.selected.build_gateway(self.owner)
        for source in (GOOD + "\ndef owner_report():\n  {==}\n",
                       "def write_report(inputs):\n  PGDone{}\n" + BAD):
            with self.subTest(source=source), self.assertRaises(AdmissionRejected):
                gateway.submit(source)
        self.assertFalse(self.target.exists())

    def test_rejection_feedback_keeps_private_checker_diagnostics_in_owner_audit(self):
        gateway = self.selected.build_gateway(self.owner)
        worker = OutboxWorker(gateway, self.root / "outbox", self.root / "results")
        proposal = write_proposal(worker.outbox, BAD)
        result = worker.process(proposal)
        self.assertEqual(result.status, Status.REJECTED)
        self.assertNotIn("owner_report", json.dumps(result.data()))
        self.assertIn("owner_report", "".join(path.read_text() for path in gateway.audit.root.glob("*.json")))
        self.assertFalse(self.target.exists())

    def test_declarative_plugin_cli(self):
        config = self.root / "session.json"
        config.write_text(json.dumps({"schema": 1, "plugins": [{"path": str(PACKAGE)}],
                                     "inputs_base64": [], "session": self.session}))
        result = subprocess.run([sys.executable, "-I", str(ROOT / "proofguard/cli.py"),
                                 "enroll", "--authority", str(self.owner), "--config", str(config)],
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = Enrollment.load(self.owner, json.loads(result.stdout)["enrollment_id"])
        self.assertEqual(selected.get_task(self.owner)["entrypoint"], "main")
