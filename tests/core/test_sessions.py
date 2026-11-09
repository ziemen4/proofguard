"""Reusable sessions retain per-submission verification and durable audit."""

import base64
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from proofguard.admission import AdmissionRejected, BendAdmission
from proofguard.audit import Audit
from proofguard.gateway import ExecutionRefused
from proofguard.packages import Enrollment
from proofguard.plugin import Plugin
from proofguard.worker import OutboxWorker, Status, write_proposal

ROOT = Path(__file__).resolve().parents[2]
GOOD = "def main(inputs):\n  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}\n"
BAD = "def main(inputs):\n  PGDone{}\n"


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.owner = self.root / "owner"
        self.owner.mkdir()
        self.target = self.root / "protected"
        self.configuration = Plugin.load(ROOT / "tests/fixtures/selection").enrollment_config(
            inputs_base64=[base64.b64encode(b"approved bytes").decode()],
            session={"identifier": "reusable", "expires": int(time.time()) + 180,
                     "destinations": [str(self.target)]})
        self.selected = Enrollment.create(self.configuration, self.owner)
        self.gateway = self.selected.build_gateway(self.owner)

    def records(self):
        return [json.loads(path.read_text()) for path in self.gateway.audit.root.glob("*.json")]

    def test_preview_and_later_invalid_submission_never_start_an_execution(self):
        self.gateway.check(GOOD)
        self.assertFalse(any(row["kind"] == "execution_started" for row in self.records()))
        self.gateway.submit(GOOD)
        with self.assertRaises(AdmissionRejected):
            self.gateway.submit(BAD)
        self.assertEqual(sum(row["kind"] == "execution_started" for row in self.records()), 1)
        self.assertEqual(self.target.read_bytes(), b"approved bytes")

    def test_failed_start_archive_prevents_application_without_disabling_session(self):
        store = Audit.store

        def fail_start(audit, record):
            if record.get("kind") == "execution_started":
                raise OSError("injected start archive failure")
            return store(audit, record)

        with patch.object(Audit, "store", fail_start), patch.object(
                BendAdmission, "execute_program", side_effect=AssertionError("unarchived application ran")):
            with self.assertRaises(ExecutionRefused):
                self.gateway.submit(GOOD)
        self.assertFalse(self.target.exists())
        self.gateway.submit(GOOD)
        self.assertEqual(self.target.read_bytes(), b"approved bytes")
        self.assertEqual(sum(row["kind"] == "refused" for row in self.records()), 1)

    def test_expiry_during_start_archive_prevents_application(self):
        store = Audit.store
        clock = [time.time()]

        def expire_after_start(audit, record):
            result = store(audit, record)
            if record.get("kind") == "execution_started":
                clock[0] = self.gateway.session.expires
            return result

        with patch("proofguard.gateway.time.time", side_effect=lambda: clock[0]), patch.object(
                Audit, "store", expire_after_start), patch.object(
                BendAdmission, "execute_program", side_effect=AssertionError("expired application ran")):
            with self.assertRaises(ExecutionRefused):
                self.gateway.submit(GOOD)
        self.assertFalse(self.target.exists())

    def test_failure_before_the_first_rename_is_refused_not_uncertain(self):
        mkstemp = tempfile.mkstemp

        def full_destination(*args, **kwargs):
            if Path(kwargs.get("dir", "")) == self.target.parent:
                raise OSError("injected disk full")
            return mkstemp(*args, **kwargs)

        with patch("proofguard.gateway.tempfile.mkstemp", side_effect=full_destination):
            with self.assertRaises(ExecutionRefused):
                self.gateway.submit(GOOD)
        self.assertFalse(self.target.exists())
        kinds = [row["kind"] for row in self.records()]
        self.assertEqual(kinds.count("refused"), 1)
        self.assertNotIn("uncertain", kinds)

    def test_partial_failure_keeps_audit_and_new_proposal_can_execute(self):
        other = self.root / "second"
        other.mkdir()
        configuration = json.loads(json.dumps(self.configuration))
        configuration["session"]["identifier"] = "partial"
        configuration["session"]["destinations"].append(str(other))
        source = configuration["plugins"][0]["contract"]["sources"][-1]
        body = """def expected_batch(+inputs: PGInputs) -> PGIO:
  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGWrite{PGNext{PGZero{}}, pg_input(PGZero{}, inputs), PGDone{}}}

def expected_report(inputs: PGInputs) -> PGIO:
  expected_batch(inputs)
"""
        source["content"] = body + "\nlaw owner_report:\n  {main == expected_report : PGInputs -> PGIO}\n"
        configuration["plugins"][0]["contract"]["project"]["proofs"] = ["owner_report"]
        program = body.split("def expected_report(")[0].replace("expected_batch", "make_batch") + (
            "\ndef main(inputs):\n  make_batch(inputs)\n")
        selected = Enrollment.create(configuration, self.owner)
        self.gateway = selected.build_gateway(self.owner)
        worker = OutboxWorker(self.gateway, self.root / "outbox", self.root / "results")
        first = write_proposal(worker.outbox, program)
        self.assertEqual(worker.process(first).status, Status.UNCERTAIN)
        self.assertEqual(self.target.read_bytes(), b"approved bytes")
        self.assertTrue(other.is_dir())
        uncertain = [row for row in self.records() if row["kind"] == "uncertain"]
        self.assertEqual(len(uncertain), 1)
        self.assertEqual(uncertain[0]["proposal"], first)
        self.assertEqual(len(uncertain[0]["completed"]), 1)
        self.assertIsNone(worker.process(first))
        other.rmdir()
        second = write_proposal(worker.outbox, program)
        self.assertEqual(worker.process(second).status, Status.COMPLETED)
        self.assertEqual(other.read_bytes(), b"approved bytes")
        self.assertEqual([row for row in self.records() if row["kind"] == "uncertain"], uncertain)
        starts = [row for row in self.records() if row["kind"] == "execution_started"]
        self.assertEqual({row["proposal"] for row in starts}, {first, second})
        self.assertEqual(len({row["intent_id"] for row in starts}), 2)
