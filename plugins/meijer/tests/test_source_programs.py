"""Meijer laws check agent programs through the foundation's fixed interface."""

from dataclasses import replace
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
from plugins.meijer.owner.configuration import (Approval, AnyPayload, Automaton, EqualInput, Guardrails, Input, Label,
                                                NonEmpty, Operation)
from plugins.meijer.owner.enrollment import LAWS, ROOT, MeijerPlugin
from proofguard.admission import AdmissionRejected
from proofguard.gateway import ExecutionRefused
from proofguard.plugin import Plugin
from proofguard.worker import write_proposal

PUBLIC = 'def main(inputs):\n  pg_write(PGZero{}, pg_input(PGZero{}, inputs))\n'
PRIVATE = 'def main(inputs):\n  pg_write(PGZero{}, pg_input(PGNext{PGZero{}}, inputs))\n'
GATE = '''def gate(value: PGBytes) -> PGIO:
  match value:
    case PGEndBytes{}: PGDone{}
    case PGByte{high, low, tail}: PGAbort{}
    case PGChunks{left, right}: PGDone{}
'''
BOTH_SINKS = '''def write_both(+inputs: PGInputs) -> PGIO:
  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGWrite{PGNext{PGZero{}}, pg_input(PGNext{PGZero{}}, inputs), PGDone{}}}

def main(inputs):
  write_both(inputs)
'''


class SourceProgramTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.serial = 0

    def enroll(self, *, policy=None, clearances=(Label.PUBLIC,), context=Label.PUBLIC,
               inputs=(Input(b'public', Label.PUBLIC), Input(b'private', Label.PRIVATE))):
        self.serial += 1
        root = self.root / str(self.serial)
        root.mkdir()
        owner = root / 'owner'
        owner.mkdir()
        plugin = MeijerPlugin(inputs, clearances, context, policy)
        selected = plugin.enroll(owner, {'identifier': 'source-meijer', 'expires': int(time.time()) + 600,
                                        'destinations': [str(root / f'output-{i}') for i in range(len(clearances))]})
        return selected, owner, selected.build_gateway(owner)

    def reject(self, gateway, source, law):
        with patch('proofguard.admission.execute', side_effect=AssertionError('rejected source executed')):
            with self.assertRaises(AdmissionRejected) as caught:
                gateway.submit(source)
        diagnostic = json.loads(caught.exception.receipt['source_check']['diagnostics'])
        self.assertEqual(diagnostic.get('definition'), law, diagnostic)
        self.assertTrue(all(not path.exists() for path in gateway.session.destinations))

    def test_only_foundation_interface_is_exported_and_original_program_executes(self):
        selected, owner, gateway = self.enroll()
        task = selected.get_task(owner)
        self.assertEqual([source['path'] for source in task['interface']],
                         ['proofguard/effects.bend', 'proofguard/program.bend'])
        for hidden in ('Action', 'Expr', 'law plan', 'pg_program_summary', *LAWS):
            self.assertNotIn(hidden, json.dumps(task))
        prepared = gateway.check(PUBLIC)
        self.assertEqual(json.loads(prepared.program.receipt_bytes)['plugins'][0]['kernel_check']['diagnostics'].strip(), 'ALL PROOFS CHECK')
        self.assertFalse(gateway.session.destinations[0].exists())
        gateway.submit(PUBLIC)
        self.assertEqual(gateway.session.destinations[0].read_bytes(), b'public')

    def test_private_dependency_is_rejected_by_data_law(self):
        _, _, gateway = self.enroll()
        self.reject(gateway, PRIVATE, 'data_safe')

    def test_meijer_composes_with_an_independent_law_package(self):
        owner = self.root / 'composed-owner'
        owner.mkdir()
        setup = MeijerPlugin((Input(b'public', Label.PUBLIC), Input(b'private', Label.PRIVATE)),
                             (Label.PUBLIC,))
        report = Plugin.load(ROOT.parents[1] / 'examples/foundation/report')
        selected = Plugin.enroll_all((setup.load(), report), owner,
            inputs_base64=['cHVibGlj', 'cHJpdmF0ZQ=='],
            session={'identifier': 'composed-meijer', 'expires': int(time.time()) + 300,
                     'destinations': [str(self.root / 'composed-output')]})
        gateway = selected.build_gateway(owner)
        self.reject(gateway, PRIVATE, 'data_safe')
        checked = json.loads(gateway.check(PUBLIC).program.receipt_bytes)
        self.assertEqual(len(checked['plugins']), 2)
        for proof in (*checked['plugins'], checked['runtime']):
            self.assertEqual(proof['kernel_check']['diagnostics'].strip(), 'ALL PROOFS CHECK')
        gateway.submit(PUBLIC)
        self.assertEqual(gateway.session.destinations[0].read_bytes(), b'public')

    def test_meijer_runs_beside_a_guard_with_a_different_signature(self):
        package = self.root / 'second'
        package.mkdir()
        (package / 'plugin.json').write_text(json.dumps({
            'schema': 1, 'name': 'second', 'intent': 'One-argument guard', 'sources': ['guard.bend'],
            'bindings': [], 'laws': ['always'], 'entrypoint': 'guard'}))
        (package / 'guard.bend').write_text('''type Fact is Data:
  Fact{}

law always:
  {Fact{} == Fact{} : Fact}

def guard(+inputs: PGInputs) -> PGIO:
  main(inputs)
''')
        owner = self.root / 'mixed-owner'
        owner.mkdir()
        policy = replace(Guardrails.defaults(1), approval_required=frozenset((0,)), approvals=(Approval(0, b'public'),))
        setup = MeijerPlugin((Input(b'public', Label.PUBLIC),), (Label.PUBLIC,), guardrails=policy)
        selected = Plugin.enroll_all((setup.load(), Plugin.load(package)), owner, inputs_base64=['cHVibGlj'],
                                     session={'identifier': 'mixed-guards', 'expires': int(time.time()) + 300,
                                              'destinations': [str(self.root / 'mixed-output')]})
        gateway = selected.build_gateway(owner)
        gateway.submit(PUBLIC)
        self.assertEqual(gateway.session.destinations[0].read_bytes(), b'public')

    def test_package_checker_works_through_independent_owner_worker(self):
        """Exercise generic enrollment and pulling without plugin Python at runtime."""
        package = self.root / 'package'
        package.mkdir()
        manifest = json.loads((ROOT / 'plugin.json').read_text())
        (package / 'plugin.json').write_text(json.dumps(manifest))
        for name in (*manifest['sources'], manifest['checker']):
            source = ROOT / name
            if source.exists():
                target = package / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        owner = self.root / 'worker-owner'
        target = self.root / 'worker-output'
        setup = MeijerPlugin((Input(b'public', Label.PUBLIC), Input(b'private', Label.PRIVATE)),
                             (Label.PUBLIC,))
        config = {'schema': 1, 'plugins': [{'path': str(package), 'bindings': [source.data() for source in setup.bindings()],
                                            'owner_data': None}],
                  'inputs_base64': [base64.b64encode(item.data).decode() for item in setup.inputs],
                  'session': {'identifier': 'source-worker', 'expires': int(time.time()) + 600,
                                                  'destinations': [str(target)]}}
        path = self.root / 'session.json'
        path.write_text(json.dumps(config))
        command = [sys.executable, '-I', str(ROOT.parents[1] / 'proofguard/cli.py')]
        enrolled = subprocess.run(command + ['enroll', '--authority', str(owner), '--config', str(path)],
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(enrolled.returncode, 0, enrolled.stderr)
        identifier = json.loads(enrolled.stdout)['enrollment_id']
        outbox = self.root / 'outbox'
        outbox.mkdir()
        write_proposal(outbox, PUBLIC)
        write_proposal(outbox, PRIVATE)
        self.assertFalse(target.exists())
        pulled = subprocess.run(command + ['watch', '--authority', str(owner), '--enrollment', identifier,
                                           '--outbox', str(outbox), '--results', str(self.root / 'results'), '--once'],
                                cwd=self.root, capture_output=True, text=True, timeout=180)
        self.assertEqual(pulled.returncode, 0, pulled.stderr)
        self.assertCountEqual([json.loads(line)['status'] for line in pulled.stdout.splitlines()],
                              ['completed', 'rejected'])
        self.assertEqual(target.read_bytes(), b'public')

    def test_private_data_can_reach_private_destination(self):
        _, _, gateway = self.enroll(clearances=(Label.PRIVATE,))
        gateway.submit(PRIVATE)
        self.assertEqual(gateway.session.destinations[0].read_bytes(), b'private')

    def test_planner_context_applies_to_generated_constants(self):
        _, _, gateway = self.enroll(context=Label.PRIVATE)
        self.reject(gateway, 'def main(inputs):\n  pg_write(PGZero{}, pg_empty())\n', 'flow_safe')

    def test_helper_transformation_preserves_private_dependency(self):
        _, _, gateway = self.enroll()
        self.reject(gateway, '''def transform(+value: PGBytes) -> PGBytes:
  pg_append(value, value)

def main(inputs):
  pg_write(PGZero{}, transform(pg_input(PGNext{PGZero{}}, inputs)))
''', 'data_safe')

    def test_input_pattern_matching_preserves_private_dependency(self):
        _, _, gateway = self.enroll()
        self.reject(gateway, '''def main(inputs):
  match inputs:
    case PGNoInputs{}: PGDone{}
    case PGInput{first, rest}:
      match rest:
        case PGNoInputs{}: PGDone{}
        case PGInput{private, tail}: pg_write(PGZero{}, private)
''', 'data_safe')

    def test_private_condition_cannot_choose_public_write_occurrence(self):
        _, _, gateway = self.enroll()
        self.reject(gateway, '''def decide(value: PGBytes) -> PGIO:
  match value:
    case PGEndBytes{}: PGDone{}
    case PGByte{high, low, tail}: pg_write(PGZero{}, pg_empty())
    case PGChunks{left, right}: PGDone{}

def main(inputs):
  decide(pg_input(PGNext{PGZero{}}, inputs))
''', 'data_safe')

    def test_private_abort_cannot_cancel_a_public_write(self):
        _, _, gateway = self.enroll()
        self.reject(gateway, GATE + '''
def main(inputs):
  PGWrite{PGZero{}, pg_byte(PGHex4{}, PGHex1{}, pg_empty()), gate(pg_input(PGNext{PGZero{}}, inputs))}
''', 'outcome_safe')

    def test_private_abort_cannot_signal_through_the_visible_outcome(self):
        _, _, gateway = self.enroll()
        self.reject(gateway, GATE + '\ndef main(inputs):\n  gate(pg_input(PGNext{PGZero{}}, inputs))\n', 'outcome_safe')

    def test_public_abort_stays_available(self):
        _, _, gateway = self.enroll()
        gateway.check(GATE + '\ndef main(inputs):\n  gate(pg_input(PGZero{}, inputs))\n')

    def test_concrete_checks_cannot_depend_on_private_payloads(self):
        clearances = (Label.PUBLIC, Label.PRIVATE)
        defaults = Guardrails.defaults(2)
        for policy in (replace(defaults, preconditions=(AnyPayload(), NonEmpty())),
                       replace(defaults, max_write_bytes=8),
                       replace(defaults, max_total_bytes=64),
                       replace(defaults, approval_required=frozenset((1,)), approvals=(Approval(1, b'private'),))):
            _, _, gateway = self.enroll(policy=policy, clearances=clearances)
            with self.subTest(policy=policy):
                self.reject(gateway, BOTH_SINKS, 'outcome_safe')

    def test_condition_comparing_a_private_input_rejects_public_payloads(self):
        policy = replace(Guardrails.defaults(1), preconditions=(EqualInput(1),))
        _, _, gateway = self.enroll(policy=policy)
        self.reject(gateway, PUBLIC, 'outcome_safe')

    def test_known_nonempty_private_payload_keeps_its_condition(self):
        policy = replace(Guardrails.defaults(1), preconditions=(NonEmpty(),))
        _, _, gateway = self.enroll(policy=policy, clearances=(Label.PRIVATE,))
        gateway.submit('''def main(inputs):
  pg_write(PGZero{}, PGByte{PGHex4{}, PGHex1{}, pg_input(PGNext{PGZero{}}, inputs)})
''')
        self.assertEqual(gateway.session.destinations[0].read_bytes(), b'Aprivate')

    def test_text_output_executes_and_counts_as_a_known_nonempty_payload(self):
        policy = replace(Guardrails.defaults(1), preconditions=(NonEmpty(),))
        _, _, gateway = self.enroll(policy=policy)
        text = '<h1>Cat café 😀</h1>' * 40
        gateway.submit(f'def main(inputs):\n  pg_write(PGZero{{}}, pg_text("{text}"))\n')
        self.assertEqual(gateway.session.destinations[0].read_bytes(), text.encode())

    def test_private_data_cannot_select_text(self):
        _, _, gateway = self.enroll()
        self.reject(gateway, '''def pick(value: PGBytes) -> PGBytes:
  match value:
    case PGEndBytes{}: pg_text("no")
    case PGByte{high, low, tail}: pg_text("yes")
    case PGChunks{left, right}: pg_text("no")

def main(inputs):
  pg_write(PGZero{}, pick(pg_input(PGNext{PGZero{}}, inputs)))
''', 'data_safe')

    def test_missing_destination_fails_scope_law(self):
        _, _, gateway = self.enroll()
        self.reject(gateway, 'def main(inputs):\n  pg_write(PGNext{PGZero{}}, pg_empty())\n', 'scope_safe')

    def test_effect_operation_restrictions_remain_mandatory(self):
        for operation in (Operation.INPUT, Operation.WRITE):
            policy = replace(Guardrails.defaults(1), operations=frozenset(Operation) - {operation})
            _, _, gateway = self.enroll(policy=policy)
            with self.subTest(operation=operation):
                self.reject(gateway, PUBLIC, 'tools_safe')

    def test_enrolled_but_unwritable_destination_fails_frame_law(self):
        policy = replace(Guardrails.defaults(2), writable_sinks=frozenset((0,)))
        _, _, gateway = self.enroll(policy=policy, clearances=(Label.PUBLIC, Label.PUBLIC))
        self.reject(gateway, 'def main(inputs):\n  pg_write(PGNext{PGZero{}}, pg_empty())\n', 'frame_safe')

    def test_known_empty_payload_fails_static_precondition(self):
        policy = replace(Guardrails.defaults(1), preconditions=(NonEmpty(),))
        _, _, gateway = self.enroll(policy=policy)
        self.reject(gateway, 'def main(inputs):\n  pg_write(PGZero{}, pg_empty())\n', 'preconditions_safe')

    def test_late_precondition_rejects_complete_batch_before_dispatch(self):
        policy = replace(Guardrails.defaults(2), preconditions=(NonEmpty(), NonEmpty()))
        _, _, gateway = self.enroll(policy=policy, clearances=(Label.PUBLIC, Label.PUBLIC),
                                   inputs=(Input(b'', Label.PUBLIC),))
        source = '''def main(inputs):
  prefix = pg_byte(PGHex4{}, PGHex1{}, pg_empty())
  PGWrite{PGZero{}, prefix, PGWrite{PGNext{PGZero{}}, pg_input(PGZero{}, inputs), PGDone{}}}
'''
        gateway.check(source)
        with self.assertRaises(ExecutionRefused):
            gateway.submit(source)
        self.assertTrue(all(not path.exists() for path in gateway.session.destinations))

    def test_missing_approval_fails_static_law(self):
        policy = replace(Guardrails.defaults(1), approval_required=frozenset((0,)))
        _, _, gateway = self.enroll(policy=policy)
        self.reject(gateway, PUBLIC, 'approval_safe')

    def test_exact_approval_checks_actual_bend_payload(self):
        policy = replace(Guardrails.defaults(1), approval_required=frozenset((0,)),
                         approvals=(Approval(0, b'approved'),))
        for payload in (b'wrong', b'approved'):
            _, _, gateway = self.enroll(policy=policy, inputs=(Input(payload, Label.PUBLIC),))
            with self.subTest(payload=payload):
                gateway.check(PUBLIC)
                if payload == b'approved':
                    gateway.submit(PUBLIC)
                    self.assertEqual(gateway.session.destinations[0].read_bytes(), payload)
                else:
                    with self.assertRaises(ExecutionRefused):
                        gateway.submit(PUBLIC)
                    self.assertFalse(gateway.session.destinations[0].exists())

    def test_every_enrollable_approval_decodes(self):
        for count in (32, 33, 64):
            approvals = tuple(Approval(0, b'other-%d' % index) for index in range(count - 1)) + (Approval(0, b'approved'),)
            policy = replace(Guardrails.defaults(1), approval_required=frozenset((0,)), approvals=approvals)
            _, _, gateway = self.enroll(policy=policy, inputs=(Input(b'approved', Label.PUBLIC),))
            with self.subTest(count=count):
                gateway.submit(PUBLIC)
                self.assertEqual(gateway.session.destinations[0].read_bytes(), b'approved')

    def test_worst_case_write_budget_checks_both_branch_successors(self):
        policy = replace(Guardrails.defaults(1), max_writes=1)
        _, _, gateway = self.enroll(policy=policy)
        self.reject(gateway, '''def choose(value: PGBytes) -> PGIO:
  match value:
    case PGEndBytes{}:
      PGWrite{PGZero{}, PGEndBytes{}, PGWrite{PGZero{}, PGEndBytes{}, PGDone{}}}
    case PGByte{high, low, tail}: PGDone{}
    case PGChunks{left, right}: PGDone{}

def main(inputs):
  choose(pg_input(PGZero{}, inputs))
''', 'budget_safe')

    def test_byte_budget_uses_concrete_payload_length(self):
        policy = replace(Guardrails.defaults(1), max_write_bytes=5)
        _, _, gateway = self.enroll(policy=policy)
        gateway.check(PUBLIC)
        with self.assertRaises(ExecutionRefused):
            gateway.submit(PUBLIC)
        self.assertFalse(gateway.session.destinations[0].exists())

    def test_ordering_automaton_checks_all_source_branches(self):
        machine = Automaton(0, frozenset((2,)), ((1, None), (None, 2), (None, None)))
        policy = replace(Guardrails.defaults(2), automata=(machine,))
        _, _, gateway = self.enroll(policy=policy, clearances=(Label.PUBLIC, Label.PUBLIC))
        good = 'PGWrite{PGZero{}, PGEndBytes{}, PGWrite{PGNext{PGZero{}}, PGEndBytes{}, PGDone{}}}'
        bad = 'PGWrite{PGNext{PGZero{}}, PGEndBytes{}, PGWrite{PGZero{}, PGEndBytes{}, PGDone{}}}'
        self.reject(gateway, 'def main(inputs):\n  ' + bad + '\n', 'sequence_safe')
        self.reject(gateway, f'''def choose(value: PGBytes) -> PGIO:
  match value:
    case PGEndBytes{{}}: {bad}
    case PGByte{{high, low, tail}}: {good}
    case PGChunks{{left, right}}: {good}

def main(inputs):
  choose(pg_input(PGZero{{}}, inputs))
''', 'sequence_safe')
        gateway.submit('def main(inputs):\n  ' + good + '\n')
        self.assertEqual(tuple(path.read_bytes() for path in gateway.session.destinations), (b'', b''))

    def test_sixteen_writes_remain_within_structural_analysis_bounds(self):
        _, _, gateway = self.enroll()
        batch = 'PGDone{}'
        for _ in range(16):
            batch = 'PGWrite{PGZero{}, PGEndBytes{}, ' + batch + '}'
        receipt = gateway.submit('def main(inputs):\n  ' + batch + '\n')
        self.assertEqual(len(receipt.writes), 16)

    def test_many_way_public_choice_stays_within_analysis_bounds(self):
        _, _, gateway = self.enroll()
        arms = ''.join(f'    case PGHex{digit}{{}}: pg_write(PGZero{{}}, pg_empty())\n' for digit in '0123456789ABCDEF')
        gateway.check(f'''def pick(value: PGNibble) -> PGIO:
  match value:
{arms}
def first(value: PGBytes) -> PGIO:
  match value:
    case PGEndBytes{{}}: PGDone{{}}
    case PGByte{{high, low, tail}}: pick(high)
    case PGChunks{{left, right}}: PGDone{{}}

def main(inputs):
  first(pg_input(PGZero{{}}, inputs))
''')

    def test_seventeen_writes_exceed_the_static_budget(self):
        _, _, gateway = self.enroll()
        batch = 'PGDone{}'
        for _ in range(17):
            batch = 'PGWrite{PGZero{}, PGEndBytes{}, ' + batch + '}'
        self.reject(gateway, 'def main(inputs):\n  ' + batch + '\n', 'budget_safe')

    def test_finite_source_recursion_produces_checked_writes(self):
        _, _, gateway = self.enroll()
        receipt = gateway.submit('''def write_each(count: PGIndex) -> PGIO:
  match count:
    case PGZero{}: PGDone{}
    case PGNext{rest}: PGWrite{PGZero{}, PGEndBytes{}, write_each(rest)}

def main(inputs):
  write_each(PGNext{PGNext{PGZero{}}})
''')
        self.assertEqual(len(receipt.writes), 2)

    def test_agent_cannot_replace_private_analysis_plan(self):
        _, _, gateway = self.enroll()
        with self.assertRaises(AdmissionRejected):
            gateway.submit(PUBLIC + '\ndef pg_program_summary():\n  PGProgramSummary{PGNoEffects{}, PGNoDependencies{}}\n')
        self.assertFalse(gateway.session.destinations[0].exists())

    def test_unknown_operation_settings_reject(self):
        policy = replace(Guardrails.defaults(1), operations=frozenset({'encoded'}))
        with self.assertRaisesRegex(ContractError, 'supported operations'):
            self.enroll(policy=policy)

    def test_generic_loading_rejects_unknown_operation_constructors(self):
        setup = MeijerPlugin((Input(b'', Label.PUBLIC),), (Label.PUBLIC,))
        bindings = tuple(replace(source, content=source.content.replace('case OpInput{}:', 'case OpEncoded{}:'))
                         for source in setup.bindings())
        package = Plugin.load(ROOT, bindings=bindings)
        owner = self.root / 'generic-owner'
        owner.mkdir()
        selected = package.with_owner_data(None).enroll(owner, inputs_base64=[''],
                                  session={'identifier': 'generic-source', 'expires': int(time.time()) + 300,
                                           'destinations': [str(self.root / 'generic-output')]})
        with self.assertRaises(AdmissionRejected):
            selected.build_gateway(owner).submit('def main(inputs):\n  PGDone{}\n')
        self.assertFalse((self.root / 'generic-output').exists())


if __name__ == '__main__':
    unittest.main()
