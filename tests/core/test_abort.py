"""Real Bend batch aborts cannot publish a prefix or disable future submissions."""

import base64
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest

from bendspec.model import Source
from proofguard.effects import ProgramAborted
from proofguard.gateway import ExecutionRefused
from proofguard.plugin import Plugin
from proofguard.worker import OutboxWorker, Status, write_proposal

ROOT = Path(__file__).resolve().parents[2]


class AbortTests(unittest.TestCase):
    def test_bend_abort_and_late_abort_never_publish_prefix_or_disable_session(self):
        for body in ("PGAbort{}", "PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGAbort{}}"):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                owner = root / "owner"
                owner.mkdir()
                target = root / "protected"
                target.write_bytes(b"original")
                plugin = Plugin.load(ROOT / "tests/fixtures/selection")
                sources = tuple(Source(source.path, source.content.replace(
                    "PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}", body))
                    for source in plugin.contract.sources)
                plugin = replace(plugin, contract=replace(plugin.contract, sources=sources))
                selected = plugin.enroll(owner,
                    inputs_base64=[base64.b64encode(b"must never be published").decode()],
                    session={"identifier": "abort", "expires": int(time.time()) + 120,
                             "destinations": [str(target)]})
                source = "def main(inputs):\n  " + body + "\n"
                gateway = selected.build_gateway(owner)
                gateway.check(source)
                self.assertEqual(target.read_bytes(), b"original")
                with self.assertRaises(ExecutionRefused) as aborted:
                    gateway.submit(source)
                self.assertIsInstance(aborted.exception.__cause__, ProgramAborted)
                self.assertEqual(target.read_bytes(), b"original")
                with self.assertRaises(ExecutionRefused) as again:
                    gateway.submit(source)
                self.assertIsInstance(again.exception.__cause__, ProgramAborted)
                self.assertEqual(target.read_bytes(), b"original")

    def test_owner_worker_does_not_retry_aborted_id_but_accepts_a_new_proposal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner = root / "owner"
            owner.mkdir()
            plugin = Plugin.load(ROOT / "tests/fixtures/selection")
            sources = tuple(Source(source.path, source.content.replace(
                "PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}", "PGAbort{}"))
                for source in plugin.contract.sources)
            plugin = replace(plugin, contract=replace(plugin.contract, sources=sources))
            target = root / "protected"
            selected = plugin.enroll(owner, inputs_base64=[],
                session={"identifier": "worker-abort", "expires": int(time.time()) + 120,
                         "destinations": [str(target)]})
            source = "def main(inputs):\n  PGAbort{}\n"
            worker = OutboxWorker(selected.build_gateway(owner), root / "outbox", root / "results")
            proposal = write_proposal(worker.outbox, source)
            result = worker.process(proposal)
            self.assertEqual((result.status, result.reason), (Status.REJECTED, "execution_refused"))
            self.assertFalse(target.exists())
            self.assertIsNone(worker.process(proposal))
            again = write_proposal(worker.outbox, source)
            self.assertEqual(worker.process(again).status, Status.REJECTED)
            self.assertFalse(target.exists())
