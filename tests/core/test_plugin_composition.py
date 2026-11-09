"""Independent Bend law contexts authorize one fixed foundation runtime."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from bendspec.backend import ROOT as CHECKER_ROOT, sha
from bendspec.model import ContractError
from proofguard.admission import AdmissionRejected, ProgramAdmission
from proofguard.gateway import ExecutionRefused, Gateway, GatewayError
from proofguard.packages import Enrollment
from proofguard.packages import runtime_package
from proofguard.plugin import Plugin

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'def main(inputs):\n  pg_write(PGZero{}, pg_input(PGZero{}, inputs))\n'


class PluginCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.owner = self.root / 'owner'
        self.owner.mkdir()
        self.target = self.root / 'result'
        self.session = {'identifier': 'composition', 'expires': int(time.time()) + 300,
                        'destinations': [str(self.target)]}
        self.report = Plugin.load(ROOT / 'examples/foundation/report')

    def package(self, name, law, *, runner=None, checker=None):
        """Create a real law package with deliberately colliding private names."""
        root = self.root / name
        root.mkdir()
        source = ('type Flag is Data:\n  Allowed{}\n  Denied{}\n\n'
                  'def private_flag() -> Flag:\n  Allowed{}\n\nlaw additional_safe:\n  ' + law + '\n')
        if runner is not None:
            source += '\ndef dispatch(inputs: PGInputs) -> PGIO:\n  ' + runner + '\n'
        (root / 'checks.bend').write_text(source)
        manifest = {
            'schema': 1, 'name': name, 'intent': 'Composition fixture', 'sources': ['checks.bend'],
            'bindings': [], 'laws': ['additional_safe'], 'entrypoint': 'dispatch' if runner else 'main'}
        if checker is not None:
            manifest['checker'] = 'verify.mjs'
            (root / 'verify.mjs').write_text(checker)
        (root / 'plugin.json').write_text(json.dumps(manifest))
        return Plugin.load(root)

    def enroll(self, *plugins):
        return Plugin.enroll_all(plugins, self.owner, inputs_base64=['eA=='], session=self.session)

    def test_all_law_packages_accept_before_original_main_executes(self):
        first = self.package('first', '{private_flag() == Allowed{} : Flag}')
        second = self.package('second', '{private_flag() == Allowed{} : Flag}')
        selected = self.enroll(self.report, first, second)
        gateway = Enrollment.load(self.owner, selected.identity).build_gateway(self.owner)
        self.assertIsInstance(gateway.admission, ProgramAdmission)
        with patch('proofguard.admission.execute', side_effect=AssertionError('preview ran application')):
            checked = gateway.check(SOURCE)
        receipt = json.loads(checked.program.receipt_bytes)
        self.assertEqual(len(receipt['plugins']), 3)
        self.assertTrue(all(item['status'] == 'accepted' for item in receipt['plugins']))
        self.assertNotIn('private_flag', json.dumps(selected.get_task(self.owner)))
        self.assertNotIn('private_flag', json.dumps(gateway.admission.runtime.contract.data()))
        gateway.submit(SOURCE)
        self.assertEqual(self.target.read_bytes(), b'x')

    def test_one_failed_law_blocks_every_effect(self):
        deny = self.package('deny', '{private_flag() == Denied{} : Flag}')
        gateway = self.enroll(self.report, deny).build_gateway(self.owner)
        with patch('proofguard.admission.execute', side_effect=AssertionError('denied program executed')):
            with self.assertRaises(AdmissionRejected):
                gateway.submit(SOURCE)
        self.assertFalse(self.target.exists())

    def test_missing_acceptance_and_changed_source_block_execution(self):
        gate = self.enroll(self.report).build_gateway(self.owner).admission
        program = gate.verify_program(SOURCE)
        for forged in (replace(program, components=program.components[:-1]),
                       replace(program, source='def main(inputs):\n  PGDone{}\n')):
            with self.subTest(forged=forged), patch('proofguard.admission.execute', side_effect=AssertionError('forgery ran')):
                with self.assertRaises(ContractError):
                    gate.execute_program(forged)

    def test_plugin_private_helpers_cannot_become_agent_runtime(self):
        helper = self.package('helper', '{private_flag() == Allowed{} : Flag}')
        gateway = self.enroll(helper).build_gateway(self.owner)
        source = 'def main(inputs):\n  ignored = private_flag()\n  PGDone{}\n'
        with self.assertRaises(AdmissionRejected):
            gateway.submit(source)
        self.assertFalse(self.target.exists())

    def test_plugin_guard_cannot_substitute_a_different_effect_batch(self):
        guard = self.package('guard', '{private_flag() == Allowed{} : Flag}', runner='PGDone{}')
        gateway = self.enroll(self.report, guard).build_gateway(self.owner)
        with self.assertRaises(ExecutionRefused):
            gateway.submit(SOURCE)
        self.assertFalse(self.target.exists())

    def test_empty_or_duplicate_plugin_selection_rejects(self):
        for plugins in ((), (self.report, self.report)):
            with self.subTest(plugins=plugins), self.assertRaises(ContractError):
                self.enroll(*plugins)

    def test_owner_cli_enrolls_multiple_private_packages(self):
        second = self.package('second', '{private_flag() == Allowed{} : Flag}')
        configuration = self.root / 'session.json'
        configuration.write_text(json.dumps({'schema': 1,
            'plugins': [{'path': str(ROOT / 'examples/foundation/report')},
                        {'path': str(self.root / 'second')}],
            'inputs_base64': ['eA=='], 'session': self.session}))
        command = [sys.executable, '-I', str(ROOT / 'proofguard/cli.py'),
                   'enroll', '--authority', str(self.owner), '--config', str(configuration)]
        completed = subprocess.run(command, text=True, capture_output=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        selected = Enrollment.load(self.owner, json.loads(completed.stdout)['enrollment_id'])
        self.assertEqual(len(selected.record['plugins']), 2)
        self.assertEqual(selected.record['plugins'][1]['contract_id'], second.contract.identity)
        selected.build_gateway(self.owner).submit(SOURCE)
        self.assertEqual(self.target.read_bytes(), b'x')

    def test_concrete_guard_can_accept_but_not_replace_main(self):
        guard = self.package('guard', '{private_flag() == Allowed{} : Flag}', runner='main(inputs)')
        gateway = self.enroll(self.report, guard).build_gateway(self.owner)
        gateway.submit(SOURCE)
        self.assertEqual(self.target.read_bytes(), b'x')

    def test_owner_cli_rejects_raw_runtime_without_plugins(self):
        configuration = self.root / 'raw.json'
        configuration.write_text(json.dumps({'schema': 1, 'contract': runtime_package().data(),
            'entrypoint': 'main', 'inputs_base64': [], 'session': self.session}))
        completed = subprocess.run([sys.executable, '-I', str(ROOT / 'proofguard/cli.py'),
            'enroll', '--authority', str(self.owner), '--config', str(configuration)],
            text=True, capture_output=True, timeout=30)
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse((self.owner / 'enrollments').exists())
        self.assertFalse(self.target.exists())

    def test_python_api_rejects_raw_or_mixed_enrollment_formats(self):
        configuration = self.report.enrollment_config(inputs_base64=[], session=self.session)
        raw = {'schema': 1, 'contract': self.report.contract.data(), 'entrypoint': 'main',
               'inputs_base64': [], 'session': self.session}
        for value in (raw, dict(configuration, contract=runtime_package().data()),
                      dict(configuration, entrypoint='dispatch'),
                      dict(configuration, checker='export function verify() {}'),
                      dict(configuration, plugins=[])):
            with self.subTest(keys=list(value)), self.assertRaises(ContractError):
                Enrollment.create(value, self.owner)
        self.assertFalse((self.owner / 'enrollments').exists())

    def test_gateway_rejects_a_single_checker_as_its_execution_pipeline(self):
        gateway = self.enroll(self.report).build_gateway(self.owner)
        with self.assertRaisesRegex(GatewayError, 'complete plugin admission'):
            Gateway(gateway.admission.plugins[0], gateway.session, gateway.context,
                    gateway.state, gateway.audit, authority=self.owner)

    def test_owner_cli_rejects_singular_plugin_configuration(self):
        configuration = self.root / 'singular.json'
        configuration.write_text(json.dumps({'schema': 1, 'plugin': str(ROOT / 'examples/foundation/report'),
            'inputs_base64': [], 'session': self.session}))
        completed = subprocess.run([sys.executable, '-I', str(ROOT / 'proofguard/cli.py'),
            'enroll', '--authority', str(self.owner), '--config', str(configuration)],
            text=True, capture_output=True, timeout=30)
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse((self.owner / 'enrollments').exists())

    def test_enrollment_pins_checker_serialization_and_capture_helpers(self):
        selected = self.enroll(self.report)
        pinned = selected.record['toolchain']
        self.assertEqual(pinned['model_sha256'], sha(CHECKER_ROOT / 'bendspec/model.py'))
        self.assertEqual(pinned['workspace_sha256'], sha(CHECKER_ROOT / 'bendspec/workspace.py'))
        self.assertEqual(pinned['package_sha256'], sha(CHECKER_ROOT / 'bendspec/__init__.py'))
        original_sha = sha
        def changed(path):
            return '0' * 64 if path == CHECKER_ROOT / 'bendspec/model.py' else original_sha(path)
        with patch('bendspec.backend.sha', side_effect=changed):
            with self.assertRaisesRegex(ContractError, 'toolchain changed'):
                selected.build_gateway(self.owner)
        self.assertFalse(self.target.exists())

    def test_failing_plugin_does_not_reach_later_plugins_or_execution(self):
        failing = self.package('failing', '{private_flag() == Allowed{} : Flag}',
                               checker="export function verify() { throw Error('owner-only detail'); }")
        gate = self.enroll(failing, self.report).build_gateway(self.owner).admission
        with patch('proofguard.admission.execute', side_effect=AssertionError('application ran')):
            with self.assertRaises(AdmissionRejected) as rejected:
                gate.verify_program(SOURCE)
        self.assertEqual(rejected.exception.receipt['completed_checks'], 1)
        self.assertEqual(rejected.exception.receipt['status'], 'rejected')
        self.assertIn('owner-only detail', rejected.exception.receipt['source_check']['diagnostics'])
        self.assertIsNone(rejected.exception.feedback)
        self.assertFalse(self.target.exists())

    def test_plugin_timeout_cannot_issue_an_execution_handle(self):
        looping = self.package('looping', '{private_flag() == Allowed{} : Flag}',
                               checker='export function verify() { while (true) {} }')
        gate = self.enroll(looping, self.report).build_gateway(self.owner).admission
        with patch('proofguard.admission.execute', side_effect=AssertionError('application ran')):
            with self.assertRaises(AdmissionRejected) as rejected:
                gate.verify_program(SOURCE, timeout=5)
        self.assertEqual(rejected.exception.receipt['completed_checks'], 1)
        self.assertTrue(rejected.exception.receipt['source_check']['timed_out'])
        self.assertFalse(self.target.exists())


if __name__ == '__main__':
    unittest.main()
