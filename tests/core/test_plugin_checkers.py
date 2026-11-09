"""Captured owner checking code stays mandatory and source-bound."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from bendspec.backend import PluginCheck
from bendspec.model import ContractError, canonical
from bendspec.backend import execute as run_backend
from proofguard.admission import AdmissionRejected
from proofguard.context import ExecutionContext
from proofguard.packages import Enrollment
from proofguard.plugin import Plugin

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'def main(inputs):\n  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}\n'
CHECKER = '''export function verify(context) {
  if (context.input_count !== 1 || context.program[0].content.indexOf('def main') < 0) throw new Error('wrong captured context');
  if ('private_inputs' in context || 'owner_context' in context) throw new Error('private context exposed');
  if ('book' in context || 'ast' in context) throw new Error('foundation supplied an analysis representation');
  return [{ name: 'pg_owner_fact', source: 'def pg_owner_fact() -> PGGateDecision:\\n  PGGateYes{}\\n' }];
}
'''


class PluginCheckerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.owner = self.root / 'owner'
        self.owner.mkdir()
        self.package = self.root / 'package'
        self.package.mkdir()
        original = ROOT / 'examples/foundation/report'
        manifest = json.loads((original / 'plugin.json').read_text())
        manifest['laws'].append('owner_fact_safe')
        manifest['checker'] = 'verify.mjs'
        (self.package / 'plugin.json').write_text(json.dumps(manifest))
        (self.package / 'checks.bend').write_text((original / 'checks.bend').read_text() + '''
type PGGateDecision is Data:
  PGGateNo{}
  PGGateYes{}

def pg_owner_fact() -> PGGateDecision:
  PGGateNo{}

law owner_fact_safe:
  {pg_owner_fact() == PGGateYes{} : PGGateDecision}
''')
        (self.package / 'verify.mjs').write_text(CHECKER)
        self.target = self.root / 'output'
        self.plugin = Plugin.load(self.package)
        self.selected = self.plugin.enroll(self.owner, inputs_base64=['eA=='],
            session={'identifier': 'checker', 'expires': int(time.time()) + 300,
                     'destinations': [str(self.target)]})
        self.gateway = self.selected.build_gateway(self.owner)
        # These tests exercise the single law-checker protocol. Composition and
        # the independent runtime are covered in test_plugin_composition.
        self.gateway.admission = self.gateway.admission.plugins[0]

    def test_captured_checker_survives_package_change_and_stays_private(self):
        (self.package / 'verify.mjs').write_text('throw new Error("changed file");')
        restored = Enrollment.load(self.owner, self.selected.identity)
        self.assertEqual(restored.record['plugins'][0]['checker'], CHECKER)
        self.assertNotIn('pg_owner_fact', json.dumps(restored.get_task(self.owner)))
        self.assertNotIn('export function verify', json.dumps(restored.get_task(self.owner)))
        restored.build_gateway(self.owner).submit(SOURCE)
        self.assertEqual(self.target.read_bytes(), b'x')

    def test_receipt_binds_checker_and_arity(self):
        prepared = self.gateway.check(SOURCE)
        receipt = json.loads(prepared.program.receipt_bytes)
        self.assertEqual(receipt['inputs']['plugin_check'], {'module': CHECKER, 'input_count': 1})
        receipt['inputs']['plugin_check']['module'] = 'export function verify() {}'
        forged = replace(prepared.program, receipt_bytes=canonical(receipt))
        with patch('proofguard.admission.execute', side_effect=AssertionError('mismatch executed')):
            with self.assertRaisesRegex(ContractError, 'binding mismatch'):
                self.gateway.admission.execute_program(forged, context=self.gateway.context)

    def test_changed_input_count_rejects_before_evaluation(self):
        prepared = self.gateway.check(SOURCE)
        with patch('proofguard.admission.execute', side_effect=AssertionError('wrong inputs executed')):
            with self.assertRaisesRegex(ContractError, 'input count'):
                self.gateway.admission.execute_program(prepared.program, context=ExecutionContext(()))

    def test_checker_failure_or_missing_export_cannot_run_application(self):
        for checker in ('export function verify() { throw new Error("denied"); }',
                        'export const nothing = true;', 'not valid JavaScript'):
            gate = replace(self.gateway.admission, checker=checker)
            with self.subTest(checker=checker):
                with patch('proofguard.admission.execute', side_effect=AssertionError('application ran')):
                    with self.assertRaises(AdmissionRejected) as rejected:
                        gate.verify_program(SOURCE)
                diagnostic = json.loads(rejected.exception.receipt['source_check']['diagnostics'])
                self.assertEqual(diagnostic['phase'], 'plugin_verification')
        self.assertFalse(self.target.exists())

    def test_checker_timeout_rejects(self):
        gate = replace(self.gateway.admission, checker='export function verify() { while (true) {} }')
        with self.assertRaises(AdmissionRejected) as rejected:
            gate.verify_program(SOURCE, timeout=1)
        self.assertTrue(rejected.exception.receipt['source_check']['timed_out'])
        self.assertFalse(self.target.exists())

    def test_omitting_checker_does_not_satisfy_required_fact(self):
        gate = replace(self.gateway.admission, checker=None)
        with self.assertRaises(AdmissionRejected):
            gate.verify_program(SOURCE)

    def test_candidate_cannot_replace_fact_or_law(self):
        for extra in ('def pg_owner_fact():\n  PGGateYes{}\n',
                      'def owner_fact_safe():\n  {==}\n'):
            with self.subTest(extra=extra), self.assertRaises(AdmissionRejected):
                self.gateway.check(SOURCE + '\n' + extra)
        self.assertFalse(self.target.exists())

    def test_execution_uses_original_source_without_verifier_artifacts(self):
        marker = self.root / 'verifier-calls'
        checker = ("import fs from 'node:fs';\n" + CHECKER).replace(
            "  return [{", "  fs.appendFileSync(" + json.dumps(str(marker)) + ", 'v');\n  return [{")
        gate = replace(self.gateway.admission, checker=checker)
        program = gate.verify_program(SOURCE)
        self.assertEqual(marker.read_text(), 'v')
        with patch('proofguard.admission.execute', wraps=run_backend) as evaluator:
            writes = gate.execute_program(program, context=self.gateway.context)
        request = json.loads(evaluator.call_args.args[1])
        self.assertEqual(set(request['inputs']), {'contract', 'candidate', 'execution_entrypoint'})
        self.assertNotIn('bindings', request['call'])
        self.assertNotIn('plugin_check', request['inputs'])
        self.assertEqual(writes[0].data, b'x')
        self.assertEqual(marker.read_text(), 'v')

    def test_execution_cannot_depend_on_verification_only_facts(self):
        for body in ('pg_owner_fact()', 'hidden_fact()'):
            source = ('def hidden_fact() -> PGGateDecision:\n  pg_owner_fact()\n\n'
                      'def main(inputs):\n  decision = ' + body + '\n  PGDone{}\n')
            with self.subTest(body=body), self.assertRaises(AdmissionRejected) as rejected:
                self.gateway.check(source)
            diagnostic = json.loads(rejected.exception.receipt['source_check']['diagnostics'])
            self.assertEqual(diagnostic['phase'], 'execution_dependencies')
            self.assertIn('verification-only', diagnostic['message'])
        self.assertFalse(self.target.exists())

    def test_changed_verification_facts_reject_before_evaluation(self):
        program = self.gateway.admission.verify_program(SOURCE)
        receipt = json.loads(program.receipt_bytes)
        self.assertEqual(receipt['bindings'], ['pg_owner_fact'])
        receipt['bindings'] = []
        forged = replace(program, receipt_bytes=canonical(receipt))
        with patch('proofguard.admission.execute', side_effect=AssertionError('changed facts executed')):
            with self.assertRaisesRegex(ContractError, 'binding mismatch'):
                self.gateway.admission.execute_program(forged, context=self.gateway.context)

    def test_verifier_cannot_return_candidate_implementation_as_a_fact(self):
        checker = "export function verify() { return [{name:'main', source:'def main(inputs):\\n  PGDone{}\\n'}]; }"
        gate = replace(self.gateway.admission, checker=checker)
        with self.assertRaises(AdmissionRejected):
            gate.verify_program(SOURCE)

    def test_assertion_only_verifier_needs_no_derived_bindings(self):
        gate = replace(self.gateway.admission, contract=Plugin.load(ROOT / 'examples/foundation/report').contract,
                       checker="export function verify({program}) { if (!program[0].content.includes('def main')) throw new Error('missing'); return []; }")
        program = gate.verify_program(SOURCE)
        self.assertEqual(json.loads(program.receipt_bytes)['bindings'], [])
        self.assertEqual(gate.execute_program(program, context=self.gateway.context)[0].data, b'x')

    def test_verifier_cannot_replace_program_through_derived_source(self):
        checker = "export function verify() { return [{name:'pg_owner_fact', source:'def main(inputs):\\n  PGDone{}\\n'}]; }"
        gate = replace(self.gateway.admission, checker=checker)
        with self.assertRaises(AdmissionRejected) as rejected:
            gate.verify_program(SOURCE)
        self.assertIn('duplicate declaration: main', rejected.exception.receipt['source_check']['diagnostics'])

    def test_verifier_mutating_its_source_copy_does_not_change_execution(self):
        checker = CHECKER.replace("  return [{", "  context.program[0].content = 'def main(inputs):\\n  PGDone{}\\n';\n  return [{")
        gate = replace(self.gateway.admission, checker=checker)
        program = gate.verify_program(SOURCE)
        self.assertEqual(gate.execute_program(program, context=self.gateway.context)[0].data, b'x')

    def test_derived_fact_cannot_change_its_type_or_add_a_definition(self):
        for source in ('def pg_owner_fact() -> PGIO:\n  PGDone{}\n',
                       'def pg_owner_fact() -> PGGateDecision:\n  PGGateYes{}\n\ndef extra() -> PGGateDecision:\n  PGGateYes{}\n'):
            checker = 'export function verify() { return [{name:"pg_owner_fact", source:' + json.dumps(source) + '}]; }'
            gate = replace(self.gateway.admission, checker=checker)
            with self.subTest(source=source), self.assertRaises(AdmissionRejected):
                gate.verify_program(SOURCE)
        self.assertFalse(self.target.exists())

    def test_invalid_checker_metadata_rejects(self):
        for module, count in (('', 1), ('x', True), ('x', 17), (None, 1)):
            with self.subTest(module=module, count=count), self.assertRaises(ContractError):
                PluginCheck(module, count)
        manifest = json.loads((self.package / 'plugin.json').read_text())
        for path in ('../outside.mjs', 'checks.bend', '/absolute.mjs'):
            manifest['checker'] = path
            (self.package / 'plugin.json').write_text(json.dumps(manifest))
            with self.subTest(path=path), self.assertRaises(ContractError):
                Plugin.load(self.package)


if __name__ == '__main__':
    unittest.main()
