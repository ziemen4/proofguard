"""Real Bend checks establish the prerequisite for application execution."""

from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from bendspec.backend import Toolchain
from bendspec.model import Bundle, ContractError, Project, Source
from proofguard.admission import AdmissionRejected, BendAdmission
from proofguard.context import ExecutionContext
from proofguard.effects import Write
from proofguard.plugin import Plugin


SOURCE = "def main(inputs):\n  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}\n"


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        package = Plugin.load(Path(__file__).resolve().parents[2] / "tests/fixtures/selection").contract
        self.admission = BendAdmission(package, "main", Toolchain.installed(), owner_argument=False)

    def test_verify_does_not_launch_application_evaluator(self):
        with patch("proofguard.admission.execute", side_effect=AssertionError("application ran before acceptance")):
            program = self.admission.verify_program(SOURCE)
        result = self.admission.execute_program(program, context=ExecutionContext((b"report",)))
        self.assertEqual(result, (Write(0, b"report"),))

    def test_rejected_program_never_reaches_application_evaluator(self):
        with patch("proofguard.admission.execute", side_effect=AssertionError("rejected application executed")):
            with self.assertRaises(AdmissionRejected):
                self.admission.verify_program("def main(inputs):\n  PGDone{}\n")

    def test_candidate_cannot_form_a_live_cycle(self):
        source = """def main(inputs):
  main(inputs)
"""
        with self.assertRaises(AdmissionRejected) as failure:
            self.admission.verify_program(source)
        self.assertEqual(failure.exception.receipt["status"], "rejected")

    def test_source_substitution_and_cross_gate_capabilities_reject(self):
        program = self.admission.verify_program(SOURCE)
        other = BendAdmission(self.admission.contract, "main", self.admission.toolchain, owner_argument=False)
        with patch("proofguard.admission.execute", side_effect=AssertionError("unverified application executed")):
            with self.assertRaisesRegex(ContractError, "binding mismatch"):
                self.admission.execute_program(replace(program, source="def main(inputs):\n  PGDone{}\n"))
            with self.assertRaisesRegex(ContractError, "this gate"):
                other.execute_program(program)
            with self.assertRaisesRegex(ContractError, "this gate"):
                self.admission.execute_program(SOURCE)

    def test_application_requires_the_fixed_one_argument_io_signature(self):
        variants = (
            "def main(inputs: PGOwnerContext, owner: PGOwnerContext) -> PGIO:\n  PGDone{}\n",
            "def main(inputs: PGInputs, owner: PGInputs) -> PGIO:\n  PGDone{}\n",
            "def main(inputs: PGInputs, owner: PGOwnerContext) -> PGInputs:\n  inputs\n",
            "def main() -> PGIO:\n  PGDone{}\n",
            "def main(inputs: PGInputs, owner: PGOwnerContext, extra: PGInputs) -> PGIO:\n  PGDone{}\n",
        )
        for body in variants:
            with self.subTest(body=body):
                with self.assertRaises(AdmissionRejected):
                    self.admission.verify_program(body)

    def test_constructed_private_data_also_passes_independent_term_inference(self):
        # Cross-check the trusted constructor path against the actual source
        # typechecker on binary values, independently of its large-input fast path.
        import subprocess
        script = """import {pathToFileURL} from 'node:url';
import fs from 'node:fs';
const [source, codec, library] = process.argv.slice(1);
const B = await import(pathToFileURL(source + '/bend.ts'));
const {runtimeInputTerm,runtimeOwnerTerm} = await import(pathToFileURL(codec));
const book=B.book_nil();
B.parse_book(book,'',fs.readFileSync(library,'utf8'),'',Object.create(null));
B.parse_book(book,'','def runner(inputs: PGInputs, owner: PGOwnerContext) -> PGIO:\\n  PGDone{}\\n','',Object.create(null));
B.book_valid(book);
const input=runtimeInputTerm(B,['AP8=','']);
const owner=runtimeOwnerTerm(B,{schema:1,data:{pair:[{bytes_base64:'AP8='},null]}});
const call=B.App(B.App(B.Ref('runner'),input),owner);
B.term_infer(book,{t:B.Ref('__probe'),n:0,def:'__probe',qs:[]},call,B.Lone(),B.ctx_nil(),0);
if(book.hols!==0)throw Error('holes');
"""
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run([str(self.admission.toolchain.node), "--experimental-strip-types",
                                 "--input-type=module", "-e", script,
                                 str(self.admission.toolchain.source), str(root / "bend-spec/backend/io.mjs"),
                                 str(root / "proofguard/stdlib/effects.bend")],
                                text=True, capture_output=True, timeout=30,
                                env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_flatten_removes_chunks_and_keeps_bytes_in_order(self):
        library = Path(__file__).resolve().parents[2] / "proofguard/stdlib/effects.bend"
        sources = (Source("spec/effects.bend", library.read_text()), Source("spec/flat.bend", """
law main:
  PGInputs -> PGIO

def expected_output(inputs: PGInputs) -> PGIO:
  PGWrite{PGZero{}, pg_unflatten(pg_flatten(pg_input(PGZero{}, inputs))), PGDone{}}

law approved_output:
  {main == expected_output : PGInputs -> PGIO}
"""))
        contract = Bundle(Project("flat-report", "Flatten keeps every byte in order",
                                  tuple(source.path for source in sources), ("main",), ("approved_output",)), sources)
        admission = BendAdmission(contract, "main", Toolchain.installed(), owner_argument=False)
        source = "def main(inputs):\n  PGWrite{PGZero{}, pg_unflatten(pg_flatten(pg_input(PGZero{}, inputs))), PGDone{}}\n"
        data = bytes(index % 251 for index in range(32000))
        result = admission.execute_program(admission.verify_program(source), context=ExecutionContext((data,)))
        self.assertEqual(result, (Write(0, data),))

    def test_bend_executes_payload_construction_without_python_application_code(self):
        library = Path(__file__).resolve().parents[2] / "proofguard/stdlib/effects.bend"
        sources = (Source("spec/effects.bend", library.read_text()), Source("spec/report.bend", """
law main:
  PGInputs -> PGIO

def expected_output(inputs: PGInputs) -> PGIO:
  PGWrite{PGZero{}, PGByte{PGHex4{}, PGHex1{}, PGByte{PGHex4{}, PGHex2{}, PGEndBytes{}}}, PGDone{}}

law approved_output:
  {main == expected_output : PGInputs -> PGIO}
"""))
        contract = Bundle(Project("bend-report", "Bend owns the payload computation",
                                  tuple(source.path for source in sources), ("main",), ("approved_output",)), sources)
        admission = BendAdmission(contract, "main", Toolchain.installed(), owner_argument=False)
        source = """def main(inputs):
  PGWrite{PGZero{}, pg_append(PGByte{PGHex4{}, PGHex1{}, PGEndBytes{}}, PGByte{PGHex4{}, PGHex2{}, PGEndBytes{}}), PGDone{}}
"""
        program = admission.verify_program(source)
        result = admission.execute_program(program, context=ExecutionContext(()))
        self.assertEqual(result, (Write(0, b"AB"),))
