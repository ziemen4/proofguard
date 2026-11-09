"""The pi layer exercises the complete Bend plugin through the SDK and sandbox.

The provider is an explicit scripted fixture; this is execution-boundary
qualification, not a live-model prompt-injection benchmark.
"""

import base64
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest

from integrations.pi.owner import PiSession
from plugins.meijer.owner.configuration import Approval, Guardrails, Input, Label
from plugins.meijer.owner.enrollment import MeijerPlugin
from proofguard.encoding import commitment


def call(source):
    return {"toolCalls": [{"name": "write_program", "arguments": {"program": source}}]}


class MeijerPiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.owner = self.root / "owner"
        self.owner.mkdir()

    def session(self, plugin, sources):
        targets = tuple(self.root / f"output-{i}" for i in range(len(plugin.clearances)))
        for target in targets:
            target.write_bytes(b"unchanged")
        enrolled = plugin.enroll(self.owner, {"identifier": "meijer-pi", "expires": int(time.time()) + 300,
                                             "destinations": [str(path) for path in targets]})
        scenario = {"schema": 1, "turns": [*(call(source) for source in sources),
                                             {"text": "The owner reported the program outcomes."}]}
        session = PiSession(self.owner, enrolled.identity, self.root / "session", "Submit Bend source using typed input references.",
                            scenario=scenario, deadline=90)
        return session, targets

    def test_static_private_flow_rejects_then_corrected_bend_program_executes(self):
        secret = b"MJ_PRIVATE_CANARY_\x00\xff"
        plugin = MeijerPlugin((Input(b"public", Label.PUBLIC), Input(secret, Label.PRIVATE)),
                              (Label.PUBLIC, Label.PRIVATE))
        bad = "def main(inputs):\n  pg_write(PGZero{}, pg_input(PGNext{PGZero{}}, inputs))\n"
        good = "def main(inputs):\n  pg_write(PGNext{PGZero{}}, pg_input(PGNext{PGZero{}}, inputs))\n"
        session, targets = self.session(plugin, (bad, good))
        report = session.run()
        self.assertTrue(report["sdk_completed"], report)
        self.assertFalse(report["remote_inference"])
        self.assertEqual(report["tools"], ["read", "write_program"])
        bad_id, good_id = (commitment("source", source.encode()) for source in (bad, good))
        outcomes = report["gateway_outcomes"]
        self.assertEqual(len(outcomes), 2)
        self.assertEqual({row["source_id"]: row["status"] for row in outcomes},
                         {bad_id: "rejected", good_id: "completed"})
        denied = next(row for row in outcomes if row["source_id"] == bad_id)
        self.assertEqual(denied["diagnostics"], "Program rejected by gateway verification.")
        self.assertEqual(targets[0].read_bytes(), b"unchanged")
        self.assertEqual(targets[1].read_bytes(), secret)
        trace = (session.root / "trace.jsonl").read_bytes()
        self.assertNotIn(secret, trace)
        self.assertNotIn(base64.b64encode(secret), trace)
        self.assertNotIn(b"data_safe", trace)
        self.assertNotIn(b"owner-policy", trace)
        self.assertNotIn(b"input_labels", trace)
        frames = [json.loads(line) for line in trace.splitlines()]
        events = [frame["value"]["event"] for frame in frames if frame["kind"] == "guest"
                  and frame["value"].get("kind") == "trace"]
        exchanges = [event for event in events if event.get("type") in ("proposal", "proposal_result")]
        self.assertEqual([event["type"] for event in exchanges],
                         ["proposal", "proposal_result", "proposal", "proposal_result"])
        self.assertEqual([event["source_id"] for event in exchanges[::2]], [bad_id, good_id])
        self.assertEqual([event["result"]["status"] for event in exchanges[1::2]], ["rejected", "completed"])
        for proposed, returned in zip(exchanges[::2], exchanges[1::2]):
            self.assertEqual(proposed["proposal"], returned["proposal"])
            self.assertIn(returned["result"], outcomes)
        records = [json.loads(path.read_text()) for path in session.worker.gateway.audit.root.glob("*.json")]
        rejected = next(row for row in records if row["kind"] == "rejected_check")
        self.assertEqual(json.loads(rejected["receipt"]["source_check"]["diagnostics"])["definition"], "data_safe")
        self.assertEqual(sum(row["kind"] == "execution_started" for row in records), 1)
        self.assertEqual(sum(row["kind"] == "completed" for row in records), 1)

    def test_exact_approval_failure_cannot_publish_a_computed_prefix(self):
        approval = b"MJ_APPROVAL_CANARY_\x00\xff"
        policy = replace(Guardrails.defaults(2), approval_required=frozenset((1,)),
                         approvals=(Approval(1, approval),))
        plugin = MeijerPlugin((Input(b"wrong", Label.PUBLIC),), (Label.PUBLIC, Label.PUBLIC), guardrails=policy)
        source = ("def write_both(+inputs: PGInputs) -> PGIO:\n"
                  "  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGWrite{PGNext{PGZero{}}, pg_input(PGZero{}, inputs), PGDone{}}}\n\n"
                  "def main(inputs):\n  write_both(inputs)\n")
        session, targets = self.session(plugin, (source,))
        report = session.run()
        self.assertTrue(report["sdk_completed"], report)
        self.assertFalse(report["reconciliation_required"])
        self.assertEqual([(row["status"], row["reason"]) for row in report["gateway_outcomes"]],
                         [("rejected", "execution_refused")])
        self.assertEqual(tuple(path.read_bytes() for path in targets), (b"unchanged", b"unchanged"))
        trace = (session.root / "trace.jsonl").read_bytes()
        self.assertNotIn(approval, trace)
        self.assertNotIn(base64.b64encode(approval), trace)
        records = [json.loads(path.read_text()) for path in session.worker.gateway.audit.root.glob("*.json")]
        self.assertEqual(sum(row["kind"] == "accepted_check" for row in records), 1)
        failure = next(row for row in records if row["kind"] == "refused")
        self.assertEqual(failure["completed"], [])
        self.assertIsNone(failure["request_id"])
        self.assertFalse(any(row["kind"] == "prepared_effects" for row in records))
