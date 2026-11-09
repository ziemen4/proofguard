"""Agnostic core qualification without importing the Meijer plugin."""

import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from bendspec.model import ContractError
from proofguard.admission import AdmissionRejected, BendAdmission
from proofguard.gateway import EffectUncertain, ExecutionRefused, GatewayError
from proofguard.packages import Enrollment
from proofguard.plugin import Plugin
from proofguard.worker import OutboxWorker, write_proposal

ROOT = Path(__file__).resolve().parents[2]
GOOD = "def main(inputs):\n  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}\n"
BAD = "def main(inputs):\n  PGDone{}\n"


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.authority = self.root / "owner"
        self.authority.mkdir()
        self.destination = self.root / "report"
        self.configuration = Plugin.load(ROOT / "tests/fixtures/selection").enrollment_config(
            inputs_base64=[base64.b64encode(b"owner report").decode()],
            session={"identifier": "report", "expires": int(time.time()) + 120,
                     "destinations": [str(self.destination)]})
        self.enrollment = Enrollment.create(self.configuration, self.authority)

    def run_owner_cli(self, *arguments):
        """Invoke the real owner CLI from outside the checkout with a clean environment."""
        return subprocess.run(
            [sys.executable, "-I", str(ROOT / "proofguard/cli.py"), *arguments],
            capture_output=True, text=True, cwd=self.root,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}, timeout=90)

    def test_owner_cli_enroll_and_task_export(self):
        path = self.root / "configuration.json"
        path.write_text(json.dumps({"schema": 1, "plugins": [{"path": str(ROOT / "examples/foundation/report")}],
                                    "inputs_base64": self.configuration["inputs_base64"],
                                    "session": self.configuration["session"]}))
        enrolled = self.run_owner_cli("enroll", "--authority", str(self.authority), "--config", str(path))
        self.assertEqual(enrolled.returncode, 0, enrolled.stderr)
        identifier = json.loads(enrolled.stdout)["enrollment_id"]
        exported = self.run_owner_cli("task", "--authority", str(self.authority), "--enrollment", identifier)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        task = json.loads(exported.stdout)
        self.assertEqual(task["enrollment_id"], identifier)
        self.assertEqual(task["entrypoint"], "main")
        self.assertNotIn("inputs_base64", exported.stdout)
        self.assertNotIn(base64.b64encode(b"owner report").decode(), exported.stdout)
        self.assertFalse(self.destination.exists())

    def test_owner_cli_task_and_watch_forbid_configuration_override(self):
        for command in ("task", "watch"):
            with self.subTest(command=command):
                result = self.run_owner_cli(command, "--authority", str(self.authority),
                                            "--enrollment", self.enrollment.identity,
                                            "--config", str(self.root / "ignored.json"))
                self.assertEqual(result.returncode, 2)
                self.assertIn("forbid --config", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_owner_cli_enroll_forbids_existing_enrollment_selection(self):
        result = self.run_owner_cli("enroll", "--authority", str(self.authority),
                                    "--enrollment", self.enrollment.identity,
                                    "--config", str(self.root / "ignored.json"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("forbids --enrollment", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_session_accepts_multiple_verified_writes_across_restart(self):
        gateway = self.enrollment.build_gateway(self.authority)
        gateway.check(GOOD)
        self.assertFalse(self.destination.exists())
        gateway.submit(GOOD)
        self.assertEqual(self.destination.read_bytes(), b"owner report")
        self.destination.write_bytes(b"intervening bytes")
        restored = Enrollment.load(self.authority, self.enrollment.identity)
        restored.build_gateway(self.authority).submit(GOOD)
        self.assertEqual(self.destination.read_bytes(), b"owner report")
        records = [json.loads(path.read_text()) for path in gateway.audit.root.glob("*.json")]
        completed = [row["receipt"] for row in records if row["kind"] == "completed"]
        self.assertEqual(len(completed), 2)
        self.assertEqual(len({row["intent_id"] for row in completed}), 2)
        self.assertEqual(len({row["check_id"] for row in completed}), 2)

    def test_bad_program_rejects_without_disabling_the_session(self):
        gateway = self.enrollment.build_gateway(self.authority)
        with self.assertRaises(AdmissionRejected):
            gateway.submit(BAD)
        self.assertFalse(self.destination.exists())
        gateway.submit(GOOD)
        self.assertEqual(self.destination.read_bytes(), b"owner report")

    def test_execute_rechecks_program_after_preview(self):
        gateway = self.enrollment.build_gateway(self.authority)
        gateway.check(GOOD)
        with self.assertRaises(AdmissionRejected):
            gateway.submit(BAD)
        self.assertFalse(self.destination.exists())
        gateway.submit(GOOD)
        self.assertEqual(self.destination.read_bytes(), b"owner report")

    def test_expired_session_has_no_effect(self):
        configuration = dict(self.configuration, session={"identifier": "expired", "expires": int(time.time()) - 1,
                                                         "destinations": [str(self.destination)]})
        expired = Enrollment.create(configuration, self.authority)
        with self.assertRaisesRegex(GatewayError, "expired"):
            expired.build_gateway(self.authority).submit(GOOD)
        self.assertFalse(self.destination.exists())

    def test_worker_cannot_override_owner_configuration(self):
        worker = OutboxWorker(self.enrollment.build_gateway(self.authority), self.root / "outbox", self.root / "results")
        with self.assertRaises(ContractError):
            worker.process("../../attacker")
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / "attacker").exists())

    def test_effect_failure_is_recorded_and_a_fresh_submission_is_allowed(self):
        # A real rename failure, not a mocked gateway or checker response.
        self.destination.mkdir()
        gateway = self.enrollment.build_gateway(self.authority)
        with self.assertRaises(EffectUncertain) as failure:
            gateway.submit(GOOD)
        self.assertTrue(failure.exception.archive_durable)
        self.assertEqual(failure.exception.completed, ())
        restored = Enrollment.load(self.authority, self.enrollment.identity)
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(list(self.destination.iterdir()), [])
        self.destination.rmdir()
        restored.build_gateway(self.authority).submit(GOOD)
        self.assertEqual(self.destination.read_bytes(), b"owner report")

    def test_agent_cannot_supply_the_managed_proof(self):
        with self.assertRaises(AdmissionRejected):
            self.enrollment.build_gateway(self.authority).submit(
                GOOD + "\ndef owner_report():\n  {==}\n")
        self.assertFalse(self.destination.exists())

    def test_non_bend_source_has_no_effect(self):
        with self.assertRaises(AdmissionRejected):
            self.enrollment.build_gateway(self.authority).submit(
                f"open({str(self.destination)!r}, 'w').write('forbidden')")
        self.assertFalse(self.destination.exists())

    def test_owner_snapshot_is_not_mutable_through_export(self):
        exported = self.enrollment.record
        exported["inputs_base64"][0] = base64.b64encode(b"other").decode()
        self.enrollment.build_gateway(self.authority).submit(GOOD)
        self.assertEqual(self.destination.read_bytes(), b"owner report")

    def test_real_worker_uses_packaged_checker_in_isolated_python(self):
        outbox, results = self.root / "outbox", self.root / "results"
        outbox.mkdir()
        proposal = write_proposal(outbox, GOOD)
        result = subprocess.run(
            [sys.executable, "-I", str(ROOT / "proofguard/cli.py"),
             "watch", "--authority", str(self.authority), "--outbox", str(outbox),
             "--results", str(results), "--once", "--enrollment", self.enrollment.identity],
            text=True, capture_output=True,
            cwd=self.root, env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
            timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(replies[0]["proposal"], proposal)
        self.assertEqual(replies[0]["status"], "completed")
        self.assertEqual(json.loads((results / (proposal + ".json")).read_text())["status"], "completed")
        self.assertEqual(self.destination.read_bytes(), b"owner report")

    def test_enrollment_rejects_corrupted_index(self):
        index = self.authority / "enrollments" / f"{self.enrollment.identity}.json"
        index.write_text('{"record_id":"' + "0" * 64 + '"}')
        with self.assertRaises((ContractError, OSError)):
            Enrollment.load(self.authority, self.enrollment.identity)

    def test_preview_never_launches_application_or_exports_private_data(self):
        gateway = self.enrollment.build_gateway(self.authority)
        with patch("proofguard.admission.execute", side_effect=AssertionError("preview executed application")):
            gateway.check(GOOD)
        for record in gateway.audit.root.glob("*.json"):
            self.assertNotIn(base64.b64encode(b"owner report").decode(), record.read_text())
        self.assertFalse(self.destination.exists())

    def test_application_starts_only_after_execution_attempt_is_archived(self):
        gateway = self.enrollment.build_gateway(self.authority)
        original = BendAdmission.execute_program
        calls = []

        def observed(admission, program, *args, **kwargs):
            records = [json.loads(path.read_text()) for path in gateway.audit.root.glob("*.json")]
            started = [row for row in records if row["kind"] == "execution_started"]
            self.assertEqual(len(started), 1)
            self.assertTrue((gateway.audit.root / (started[0]["check_id"] + ".json")).is_file())
            calls.append(program.source)
            return original(admission, program, *args, **kwargs)

        with patch.object(BendAdmission, "execute_program", observed):
            gateway.submit(GOOD)
        self.assertEqual(calls, [GOOD])
        self.assertEqual(self.destination.read_bytes(), b"owner report")
        self.assertFalse(hasattr(gateway, "runtime"))

    def test_binary_kibibyte_private_input_executes_in_bend(self):
        data = bytes(range(256)) * 4
        configuration = json.loads(json.dumps(self.configuration))
        configuration["session"]["identifier"] = "binary-report"
        configuration["inputs_base64"] = [base64.b64encode(data).decode()]
        enrolled = Enrollment.create(configuration, self.authority)
        self.assertNotIn(configuration["inputs_base64"][0], json.dumps(enrolled.get_task(self.authority)))
        enrolled.build_gateway(self.authority).submit(GOOD)
        self.assertEqual(self.destination.read_bytes(), data)

    def test_unenrolled_later_destination_blocks_the_entire_typed_batch(self):
        configuration = json.loads(json.dumps(self.configuration))
        for source in configuration["plugins"][0]["contract"]["sources"]:
            source["content"] = source["content"].replace(
                "PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}",
                "PGWrite{PGZero{}, PGByte{PGHex4{}, PGHex1{}, PGEndBytes{}}, "
                "PGWrite{PGNext{PGZero{}}, PGEndBytes{}, PGDone{}}}")
        selected = Enrollment.create(configuration, self.authority)
        self.destination.write_bytes(b"unchanged")
        with self.assertRaises(ExecutionRefused):
            selected.build_gateway(self.authority).submit(
                "def main(inputs):\n  PGWrite{PGZero{}, PGByte{PGHex4{}, PGHex1{}, PGEndBytes{}}, "
                "PGWrite{PGNext{PGZero{}}, PGEndBytes{}, PGDone{}}}\n")
        self.assertEqual(self.destination.read_bytes(), b"unchanged")

    def test_maximum_private_input_size_executes_in_bend(self):
        data = bytes(range(256)) * 256
        configuration = json.loads(json.dumps(self.configuration))
        configuration["session"]["identifier"] = "maximum-binary-report"
        configuration["session"]["expires"] = int(time.time()) + 240
        configuration["inputs_base64"] = [base64.b64encode(data).decode()]
        enrolled = Enrollment.create(configuration, self.authority)
        enrolled.build_gateway(self.authority).submit(GOOD)
        self.assertEqual(self.destination.read_bytes(), data)

    def test_maximum_input_and_write_batch_executes_in_bend(self):
        configuration = json.loads(json.dumps(self.configuration))
        configuration["session"]["identifier"] = "maximum-batch"
        configuration["session"]["expires"] = int(time.time()) + 240
        outputs = [self.root / f"output-{index}" for index in range(16)]
        configuration["session"]["destinations"] = [str(path) for path in outputs]
        data = [bytes([index]) * 65536 for index in range(16)]
        configuration["inputs_base64"] = [base64.b64encode(item).decode() for item in data]
        body = "PGDone{}"
        for index in reversed(range(16)):
            key = "PGNext{" * index + "PGZero{}" + "}" * index
            body = f"PGWrite{{{key}, pg_input({key}, inputs), {body}}}"
        sources = configuration["plugins"][0]["contract"]["sources"]
        sources[-1]["content"] = (
            "def expected_batch(+inputs: PGInputs) -> PGIO:\n  " + body + "\n\n"
            "def expected_report(inputs: PGInputs) -> PGIO:\n  expected_batch(inputs)\n\n"
            "law owner_report:\n  {main == expected_report : PGInputs -> PGIO}\n")
        configuration["plugins"][0]["contract"]["project"]["proofs"] = ["owner_report"]
        enrolled = Enrollment.create(configuration, self.authority)
        enrolled.build_gateway(self.authority).submit(
            "def make_batch(+inputs: PGInputs) -> PGIO:\n  " + body + "\n\ndef main(inputs):\n  make_batch(inputs)\n")
        self.assertEqual([path.read_bytes() for path in outputs], data)

    def test_agent_cannot_replace_the_owner_reference(self):
        gateway = self.enrollment.build_gateway(self.authority)
        with self.assertRaises(AdmissionRejected):
            gateway.submit(GOOD + "\ndef expected_report(inputs):\n  PGDone{}\n")
        self.assertFalse(self.destination.exists())
        records = [json.loads(path.read_text()) for path in gateway.audit.root.glob("*.json")]
        self.assertFalse(any(row["kind"] == "execution_started" for row in records))

    def test_native_plugin_configuration_is_rejected(self):
        with self.assertRaises(ContractError):
            Enrollment.create({"schema": 1, "plugin": "untrusted.module:factory", "configuration": {},
                               "entrypoint": "plan", "session": self.configuration["session"]}, self.authority)
        self.assertFalse(self.destination.exists())

    def test_wrong_entrypoint_signature_rejects_before_execution(self):
        configuration = json.loads(json.dumps(self.configuration))
        configuration["session"]["identifier"] = "invalid-execution-call"
        enrolled = Enrollment.create(configuration, self.authority)
        gateway = enrolled.build_gateway(self.authority)
        with self.assertRaises(AdmissionRejected):
            gateway.submit("def main():\n  PGDone{}\n")
        self.assertFalse(self.destination.exists())
        enrolled.build_gateway(self.authority).submit(GOOD)
        records = [json.loads(path.read_text()) for path in gateway.audit.root.glob("*.json")]
        self.assertEqual(sum(row["kind"] == "execution_started" for row in records), 1)
        self.assertEqual(sum(row["kind"] == "uncertain" for row in records), 0)
