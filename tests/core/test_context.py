"""Owner context remains private until the accepted Bend application runs."""

import base64
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from bendspec.model import ContractError, Source
from proofguard.context import ExecutionContext, MAX_OWNER_CONTEXT, PrivateBlob
from proofguard.admission import BendAdmission
from proofguard.gateway import Gateway, GatewayError
from proofguard.packages import Enrollment
from proofguard.plugin import Plugin

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "def main(inputs):\n  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}\n"


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.owner = self.root / "owner"
        self.owner.mkdir()
        self.target = self.root / "protected"
        plugin = Plugin.load(ROOT / "tests/fixtures/selection")
        # Private owner data is inspected by the guard, never passed to main.
        helper = """def guard_private(value: PGPrivate, batch: PGIO) -> PGIO:
  match value:
    case PGEmpty{}:
      batch
    case PGBlob{bytes}:
      batch
    case PGPair{left, right}:
      PGAbort{}

def guard_context(inputs: PGInputs, owner: PGOwnerContext) -> PGIO:
  match owner:
    case PGOwnerContext{value}:
      guard_private(value, main(inputs))

"""
        sources = (*plugin.contract.sources[:-1], Source(plugin.contract.sources[-1].path,
                    plugin.contract.sources[-1].content + helper))
        plugin = replace(plugin, contract=replace(plugin.contract, sources=sources), entrypoint="guard_context",
                         owner_data={"bytes_base64": base64.b64encode(b"owner-only\x00\xff").decode()})
        self.configuration = plugin.enrollment_config(
            inputs_base64=[base64.b64encode(b"application data").decode()],
            session={"identifier": "context", "expires": int(time.time()) + 180,
                     "destinations": [str(self.target)]})

    def test_bend_receives_distinct_context_only_after_execution_attempt_is_archived(self):
        enrolled = Enrollment.create(self.configuration, self.owner)
        gateway = enrolled.build_gateway(self.owner)
        with patch("proofguard.admission.execute", side_effect=AssertionError("preview evaluated private context")):
            gateway.check(SOURCE)
        for record in gateway.audit.root.glob("*.json"):
            self.assertNotIn(self.configuration["plugins"][0]["owner_data"]["bytes_base64"], record.read_text())
        task = json.dumps(enrolled.get_task(self.owner))
        self.assertNotIn("owner_data", task)
        self.assertNotIn(self.configuration["plugins"][0]["owner_data"]["bytes_base64"], task)
        self.assertNotIn(enrolled.record["context_id"], task)
        self.assertFalse(self.target.exists())
        execute = BendAdmission.execute_program

        def observed(admission, program, *args, **kwargs):
            records = [json.loads(path.read_text()) for path in gateway.audit.root.glob("*.json")]
            self.assertEqual(sum(row["kind"] == "execution_started" for row in records), 1)
            self.assertEqual(kwargs["context"], gateway.context)
            return execute(admission, program, *args, **kwargs)

        with patch.object(BendAdmission, "execute_program", observed):
            gateway.submit(SOURCE)
        self.assertEqual(self.target.read_bytes(), b"application data")

    def test_private_context_changes_binding_without_changing_contract_or_preview(self):
        first = Enrollment.create(self.configuration, self.owner)
        other = json.loads(json.dumps(self.configuration))
        other["plugins"][0]["owner_data"] = {"bytes_base64": base64.b64encode(b"other owner-only bytes").decode()}
        second = Enrollment.create(other, self.owner)
        self.assertNotEqual(first.identity, second.identity)
        self.assertNotEqual(first.record["context_id"], second.record["context_id"])
        self.assertEqual(first.record["contract_id"], second.record["contract_id"])
        before = first.build_gateway(self.owner).check(SOURCE)
        after = second.build_gateway(self.owner).check(SOURCE)
        self.assertEqual(before.program.receipt_bytes, after.program.receipt_bytes)
        exported = first.record
        exported["plugins"][0]["owner_data"] = other["plugins"][0]["owner_data"]
        self.assertNotEqual(exported, first.record)
        gateway = first.build_gateway(self.owner)
        with self.assertRaisesRegex(GatewayError, "context"):
            Gateway(gateway.admission, gateway.session, ExecutionContext(gateway.context.inputs, (PrivateBlob(b"substituted"),)),
                    gateway.state, gateway.audit, authority=self.owner)
        self.assertFalse(self.target.exists())

    def test_context_boundaries_and_canonical_configuration(self):
        for value in (PrivateBlob(b"x" * (MAX_OWNER_CONTEXT + 1)), "not bytes", b"untyped"):
            with self.assertRaises(ContractError):
                ExecutionContext((), (value,))
        self.assertNotEqual(ExecutionContext((b"ab",), (PrivateBlob(b"c"),)).identity,
                            ExecutionContext((b"a",), (PrivateBlob(b"bc"),)).identity)
        with self.assertRaises((ContractError, ValueError)):
            Enrollment.create(dict(self.configuration, schema=True), self.owner)
        for value in ({"bytes_base64": "AB=="}, True,
                      {"bytes_base64": "A" * (4 * ((MAX_OWNER_CONTEXT + 2) // 3) + 4)}):
            configuration = json.loads(json.dumps(self.configuration))
            configuration["plugins"][0]["owner_data"] = value
            with self.subTest(value=str(value)[:32]), self.assertRaises((ContractError, ValueError)):
                Enrollment.create(configuration, self.owner)

    def test_maximum_owner_context_and_all_application_inputs_execute_together(self):
        # Exercise actual construction, typechecking and full traversal of more
        # than 4 MiB of context alongside the existing 1 MiB application bound.
        helper = """type PGCheck is Data:
  PGValid{}
  PGInvalid{}

def both_checks(left: PGCheck, right: PGCheck) -> PGCheck:
  match left:
    case PGValid{}:
      right
    case PGInvalid{}:
      PGInvalid{}

def zero_digit(value: PGNibble) -> PGCheck:
  match value:
""" + "".join(f"    case PGHex{digit}{{}}:\n      {'PGValid' if digit == '0' else 'PGInvalid'}{{}}\n"
                for digit in "0123456789ABCDEF") + """
def check_zero_bytes(bytes: PGBytes) -> PGCheck:
  match bytes:
    case PGEndBytes{}:
      PGValid{}
    case PGByte{high, low, tail}:
      both_checks(both_checks(zero_digit(high), zero_digit(low)), check_zero_bytes(tail))
    case PGChunks{left, right}:
      both_checks(check_zero_bytes(left), check_zero_bytes(right))

def checked_batch(check: PGCheck, +inputs: PGInputs) -> PGIO:
  match check:
    case PGValid{}:
      BATCH
    case PGInvalid{}:
      PGAbort{}

def check_private(value: PGPrivate) -> PGCheck:
  match value:
    case PGEmpty{}:
      PGValid{}
    case PGBlob{bytes}:
      check_zero_bytes(bytes)
    case PGPair{left, right}:
      both_checks(check_private(left), check_private(right))

def write_checked_context(owner: PGOwnerContext, inputs: PGInputs) -> PGIO:
  match owner:
    case PGOwnerContext{value}:
      checked_batch(check_private(value), inputs)

"""
        body = "PGDone{}"
        for index in reversed(range(16)):
            key = "PGNext{" * index + "PGZero{}" + "}" * index
            body = f"PGWrite{{{key}, pg_input({key}, inputs), {body}}}"
        configuration = json.loads(json.dumps(self.configuration))
        source = configuration["plugins"][0]["contract"]["sources"][-1]
        source["content"] = (
            "def expected_batch(+inputs: PGInputs) -> PGIO:\n  " + body + "\n\n"
            "def expected_report(inputs: PGInputs) -> PGIO:\n  expected_batch(inputs)\n\n"
            "law owner_report:\n  {main == expected_report : PGInputs -> PGIO}\n\n"
        ) + helper.replace("BATCH", "expected_report(inputs)") + (
            "def guard_context(inputs: PGInputs, owner: PGOwnerContext) -> PGIO:\n"
            "  write_checked_context(owner, inputs)\n")
        configuration["plugins"][0]["contract"]["project"]["proofs"] = ["owner_report"]
        configuration["plugins"][0]["owner_data"] = {"pair": [
            {"bytes_base64": base64.b64encode(b"\x00" * (MAX_OWNER_CONTEXT - 1)).decode()},
            {"bytes_base64": "AA=="}]}
        values = [bytes([index]) * 65536 for index in range(16)]
        configuration["inputs_base64"] = [base64.b64encode(value).decode() for value in values]
        outputs = [self.root / f"output-{index}" for index in range(16)]
        configuration["session"]["destinations"] = [str(path) for path in outputs]
        configuration["session"]["expires"] = int(time.time()) + 300
        selected = Enrollment.create(configuration, self.owner)
        restored = Enrollment.load(self.owner, selected.identity)
        self.assertEqual(restored.snapshot, selected.snapshot)
        restored.build_gateway(self.owner).submit(
            "def make_batch(+inputs: PGInputs) -> PGIO:\n  " + body + "\n\ndef main(inputs):\n  make_batch(inputs)\n")
        self.assertEqual([path.read_bytes() for path in outputs], values)
