"""Hostile filesystem proposals exercise the real owner worker and Bend gate."""

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from bendspec.model import ContractError
from proofguard.admission import BendAdmission
from proofguard.plugin import Plugin
from proofguard.worker import OutboxCapacityError, OutboxWorker, Status, capture_program, write_proposal

ROOT = Path(__file__).resolve().parents[2]
GOOD = "def main(inputs):\n  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}\n"
BAD = "def main(inputs):\n  PGDone{}\n"


class OutboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.owner = self.root / "owner"
        self.owner.mkdir()
        self.target = self.root / "target"
        self.selected = Plugin.load(ROOT / "tests/fixtures/selection").enroll(
            self.owner, inputs_base64=[base64.b64encode(b"approved bytes").decode()],
            session={"identifier": "outbox", "expires": int(time.time()) + 180,
                     "destinations": [str(self.target)]})
        self.outbox, self.results = self.root / "outbox", self.root / "results"
        self.worker = OutboxWorker(self.selected.build_gateway(self.owner), self.outbox, self.results)

    def raw_proposal(self, data):
        proposal = uuid.uuid4().hex
        (self.outbox / (proposal + ".bend")).write_bytes(data)
        return proposal

    def test_outbox_submission_is_inert_until_owner_pulls_and_checks_it(self):
        proposal = write_proposal(self.outbox, GOOD)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.worker.poll()[0].status, Status.COMPLETED)
        self.assertEqual(self.target.read_bytes(), b"approved bytes")
        result = json.loads((self.results / (proposal + ".json")).read_text())
        self.assertEqual(result["proposal"], proposal)
        self.assertNotIn(base64.b64encode(b"approved bytes").decode(), json.dumps(result))

    def test_rejection_allows_a_distinct_corrected_program(self):
        bad = write_proposal(self.outbox, BAD)
        self.assertEqual(self.worker.process(bad).status, Status.REJECTED)
        self.assertFalse(self.target.exists())
        good = write_proposal(self.outbox, GOOD)
        self.assertEqual(self.worker.process(good).status, Status.COMPLETED)
        self.assertEqual(self.target.read_bytes(), b"approved bytes")

    def test_foundation_errors_reach_the_agent_and_law_failures_stay_generic(self):
        syntax = self.worker.process(write_proposal(self.outbox, "def main(inputs):\n  pg_write(PGZero{}, oops\n"))
        self.assertEqual(syntax.status, Status.REJECTED)
        self.assertEqual(json.loads(syntax.diagnostics)["phase"], "parse_candidate")
        unknown = self.worker.process(write_proposal(self.outbox, "def main(inputs):\n  missing_helper(inputs)\n"))
        self.assertIn("missing_helper", unknown.diagnostics)
        law = self.worker.process(write_proposal(self.outbox, BAD))
        self.assertEqual(law.diagnostics, "Program rejected by gateway verification.")

    def test_many_rejections_do_not_silently_disable_the_worker(self):
        for _ in range(26):
            result = self.worker.process(self.raw_proposal(b""))
            self.assertIsNotNone(result)
            self.assertEqual(result.status, Status.REJECTED)
        good = write_proposal(self.outbox, GOOD)
        results = self.worker.poll()
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0].proposal, results[0].status), (good, Status.COMPLETED))
        self.assertEqual(self.target.read_bytes(), b"approved bytes")
        self.assertIsNone(self.worker.process(good))

    def test_partial_or_non_bend_sources_never_execute(self):
        for data in (b"def plan():", b"print('unverified')", b"\xff", b"x" * 16385):
            with self.subTest(size=len(data)):
                proposal = self.raw_proposal(data)
                self.assertEqual(self.worker.process(proposal).status, Status.REJECTED)
                self.assertFalse(self.target.exists())

    def test_temporary_uploads_are_not_picked_up(self):
        (self.outbox / ".unfinished.upload").write_text(GOOD)
        self.assertEqual(self.worker.poll(), ())
        self.assertFalse(self.target.exists())

    def test_exact_capacity_still_processes_a_valid_proposal(self):
        for index in range(127):
            (self.outbox / f".ignored-{index}").touch()
        proposal = write_proposal(self.outbox, GOOD)
        self.assertEqual(self.worker.poll()[0].proposal, proposal)
        self.assertEqual(self.target.read_bytes(), b"approved bytes")

    def test_capacity_overflow_stops_before_any_claim_or_effect(self):
        # The valid source comes first, detecting processing before the bound.
        write_proposal(self.outbox, GOOD)
        for index in range(128):
            (self.outbox / f".ignored-{index}").touch()
        with self.assertRaises(OutboxCapacityError):
            next(self.worker.watch())
        self.assertEqual(list(self.worker.claims.iterdir()), [])
        self.assertFalse(any(path.is_file() for path in self.worker.gateway.state.iterdir()))
        self.assertFalse(any(self.worker.gateway.audit.root.iterdir()))
        self.assertFalse(self.target.exists())
        result = subprocess.run([sys.executable, "-I", str(ROOT / "proofguard/cli.py"),
                                "watch", "--authority", str(self.owner), "--enrollment", self.selected.identity,
                                "--outbox", str(self.outbox), "--results", str(self.results)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("capacity exceeded", result.stderr)
        self.assertIn("clean or rotate", result.stderr)
        self.assertEqual(list(self.worker.claims.iterdir()), [])
        self.assertFalse(self.target.exists())

    def test_symlink_hardlink_directory_and_fifo_sources_reject_without_disclosure(self):
        private = self.owner / "private-canary"
        private.write_text("OWNER_ONLY_CANARY")
        for kind in ("symlink", "hardlink", "directory", "fifo"):
            with self.subTest(kind=kind):
                proposal = uuid.uuid4().hex
                path = self.outbox / (proposal + ".bend")
                if kind == "symlink":
                    path.symlink_to(private)
                elif kind == "hardlink":
                    os.link(private, path)
                elif kind == "directory":
                    path.mkdir()
                else:
                    os.mkfifo(path)
                started = time.monotonic()
                result = self.worker.process(proposal)
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(result.status, Status.REJECTED)
                self.assertEqual(result.reason, "invalid_program_source")
                self.assertNotIn("OWNER_ONLY_CANARY", json.dumps(result.data()))
                self.assertFalse(self.target.exists())

    def test_replay_and_source_replacement_after_verification_cannot_change_execution(self):
        proposal = write_proposal(self.outbox, GOOD)
        original = BendAdmission.verify_program

        def replace_file(admission, source, *args, **kwargs):
            verified = original(admission, source, *args, **kwargs)
            (self.outbox / (proposal + ".bend")).write_text(BAD)
            return verified

        with patch.object(BendAdmission, "verify_program", replace_file):
            result = self.worker.process(proposal)
        self.assertEqual(result.status, Status.COMPLETED)
        self.assertEqual(self.target.read_bytes(), b"approved bytes")
        self.assertIsNone(self.worker.process(proposal))
        restored = OutboxWorker(self.selected.build_gateway(self.owner), self.outbox, self.results)
        self.assertIsNone(restored.process(proposal))
        self.assertEqual(restored.process(write_proposal(self.outbox, GOOD)).status, Status.COMPLETED)

    def test_unfinished_durable_claim_is_not_retried_on_restart(self):
        proposal = write_proposal(self.outbox, GOOD)
        self.assertTrue(self.worker._claim(proposal))
        restored = OutboxWorker(self.selected.build_gateway(self.owner), self.outbox, self.results)
        self.assertIsNone(restored.process(proposal))
        self.assertFalse((self.results / (proposal + ".json")).exists())
        self.assertFalse(self.target.exists())

    def test_overlapping_owner_mount_paths_are_rejected(self):
        for outbox, results in ((self.owner / "bad", self.root / "public"),
                                (self.outbox, self.outbox / "results"),
                                (self.outbox, self.owner / "contracts"),
                                (self.target, self.root / "public"),
                                (ROOT / "bad-outbox", self.root / "public")):
            with self.subTest(outbox=outbox, results=results):
                with self.assertRaises(ContractError):
                    OutboxWorker(self.selected.build_gateway(self.owner), outbox, results)

    def test_concurrent_owner_processes_do_not_execute_one_proposal_twice(self):
        proposal = write_proposal(self.outbox, GOOD)
        command = [sys.executable, "-I", str(ROOT / "proofguard/cli.py"),
                   "watch", "--authority", str(self.owner), "--enrollment", self.selected.identity,
                   "--outbox", str(self.outbox), "--results", str(self.results), "--once"]

        def run():
            return subprocess.run(command, capture_output=True, text=True, timeout=90,
                                  env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}, cwd=self.root)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run(), range(2)))
        self.assertEqual([row.returncode for row in results], [0, 0], [row.stderr for row in results])
        self.assertEqual(self.target.read_bytes(), b"approved bytes")
        self.assertEqual(json.loads((self.results / (proposal + ".json")).read_text())["status"], "completed")
        records = [json.loads(path.read_text()) for path in self.worker.gateway.audit.root.glob("*.json")]
        self.assertEqual(sum(row["kind"] == "completed" for row in records), 1)
        self.assertEqual(sum(row["kind"] == "accepted_check" for row in records), 1)

    def test_snapshot_rejects_traversal_identifiers(self):
        with self.assertRaises(ContractError):
            capture_program(self.outbox, "../owner/private-canary")

    def test_concurrent_fresh_proposals_share_session_with_distinct_audits(self):
        proposals = {write_proposal(self.outbox, GOOD), write_proposal(self.outbox, GOOD)}
        command = [sys.executable, "-I", str(ROOT / "proofguard/cli.py"),
                   "watch", "--authority", str(self.owner), "--enrollment", self.selected.identity,
                   "--outbox", str(self.outbox), "--results", str(self.results), "--once"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = list(pool.map(lambda _: subprocess.run(command, capture_output=True, text=True, timeout=90,
                                                         env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
                                                         cwd=self.root), range(2)))
        self.assertEqual([run.returncode for run in runs], [0, 0], [run.stderr for run in runs])
        records = [json.loads(path.read_text()) for path in self.worker.gateway.audit.root.glob("*.json")]
        starts = [row for row in records if row["kind"] == "execution_started"]
        self.assertEqual({row["proposal"] for row in starts}, proposals)
        self.assertEqual(len({row["intent_id"] for row in starts}), 2)
        self.assertEqual(sum(row["kind"] == "completed" for row in records), 2)
        self.assertEqual(sum(row["kind"] == "accepted_check" for row in records), 2)
        self.assertEqual(self.target.read_bytes(), b"approved bytes")
        for proposal in proposals:
            self.assertEqual(json.loads((self.results / (proposal + ".json")).read_text())["status"], "completed")
