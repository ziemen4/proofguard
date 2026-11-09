"""A harness needs only this seam: a stand-in runs here with no agent runtime."""

import base64
import json
from pathlib import Path
import tempfile
import time
import unittest

from proofguard.plugin import Plugin
from proofguard.session import AuthoringSession, ContractError

ROOT = Path(__file__).resolve().parents[2]


def stage_guest(destination: Path):
    destination.mkdir()
    (destination / "guest.py").write_text("print('guest')\n")


class SessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.owner = self.base / "owner"
        self.owner.mkdir()
        self.selected = Plugin.load(ROOT / "tests/fixtures/selection").enroll(
            self.owner, inputs_base64=[base64.b64encode(b"BEND_APPROVED").decode()],
            session={"identifier": "seam", "expires": int(time.time()) + 180,
                     "destinations": [str(self.base / "output")]})

    def open(self, name="session", brief="Do the task.", **options):
        return AuthoringSession(self.owner, self.selected.identity, self.base / name, brief,
                                stage_runtime=stage_guest, **options)

    def test_public_view_holds_the_task_brief_and_extra_files(self):
        session = self.open(files={"extra.txt": b"public"})
        task = json.loads((session.task / "task.json").read_bytes())
        self.assertEqual(task["enrollment_id"], self.selected.identity)
        self.assertEqual((session.task / "brief.txt").read_text(), "Do the task.")
        self.assertEqual((session.task / "extra.txt").read_bytes(), b"public")

    def test_agent_runs_harness_code_confined_to_public_mounts(self):
        session = self.open()
        source = ("import json, pathlib\n"
                  f"print(json.dumps([pathlib.Path({str(self.owner)!r}).exists(),"
                  " pathlib.Path('/runtime/guest.py').exists(), pathlib.Path('/task/brief.txt').read_text()]))")
        result = session.sandbox.run(["/usr/bin/python3", "-I", "-c", source])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [False, True, "Do the task."])
        self.assertEqual(session.agent_command(["/usr/bin/true"])[0], "/usr/bin/bwrap")

    def test_worker_command_names_this_enrollment_and_parent(self):
        command = self.open().worker_command(4242)
        self.assertEqual(command[command.index("--enrollment") + 1], self.selected.identity)
        self.assertEqual(command[command.index("--parent") + 1], "4242")

    def test_owner_paths_visible_to_the_agent_are_refused(self):
        session = self.open()
        for path in (session.task, session.runtime, session.worker.outbox, session.worker.results):
            with self.assertRaises(ContractError):
                session.require_private(path)
        session.require_private(self.owner)

    def test_extra_task_files_need_plain_unreserved_names(self):
        for name in ("../outbox/x.bend", "a/b", ".hidden", "task.json", "brief.txt", ""):
            with self.subTest(name=name), self.assertRaises(ContractError):
                self.open(name="unused", files={name: b"x"})
        self.assertFalse((self.base / "unused").exists())

    def test_session_requires_a_bounded_brief_and_a_new_root(self):
        for brief in ("", "x" * 32769):
            with self.assertRaises(ContractError):
                self.open(name="unused", brief=brief)
        self.assertFalse((self.base / "unused").exists())
        self.open()
        with self.assertRaises(FileExistsError):
            self.open()

    def test_report_is_published_and_a_fresh_session_needs_no_reconciliation(self):
        session = self.open()
        self.assertEqual(session.outcomes(), [])
        self.assertFalse(session.needs_reconciliation(session.outcomes()))
        session.publish_report({"schema": 1})
        self.assertEqual(json.loads((session.root / "report.json").read_bytes()), {"schema": 1})

    def test_claims_from_another_session_on_the_same_enrollment_are_ignored(self):
        first = self.open(name="first")
        claim = {"schema": 1, "proposal": "a" * 32, "outbox": str(first.worker.outbox)}
        (first.worker.claims / ("a" * 32 + ".claim")).write_bytes(json.dumps(claim).encode())
        self.assertTrue(first.needs_reconciliation([]))
        second = self.open(name="second")
        self.assertEqual(second.worker.claims, first.worker.claims)
        self.assertFalse(second.needs_reconciliation([]))

    def test_uncertain_outcome_requires_reconciliation(self):
        session = self.open()
        self.assertTrue(session.needs_reconciliation([{"status": "uncertain"}]))
        self.assertFalse(session.needs_reconciliation([{"status": "completed"}]))


if __name__ == "__main__":
    unittest.main()
