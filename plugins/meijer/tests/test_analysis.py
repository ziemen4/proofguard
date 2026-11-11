"""Real source/kernel checks for Meijer dependency extraction."""

import base64
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from proofguard.admission import AdmissionRejected
from proofguard.context import ExecutionContext
from proofguard.encoding import commitment
from bendspec.model import ContractError
from proofguard.packages import Enrollment
from proofguard.plugin import Plugin

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / 'plugins/meijer/tests/fixtures/report'
MEIJER = ROOT / 'plugins/meijer'


COUNT = '''def count(bytes: PGBytes, total: Nat) -> Nat:
  match bytes:
    case PGEndBytes{}: total
    case PGByte{high, low, tail}: count(tail, Succ{total})
    case PGChunks{left, right}: count(right, count(left, total))

def render(total: Nat) -> PGBytes:
  match total:
    case Zero{}: PGEndBytes{}
    case Succ{pred}: PGByte{PGHex7{}, PGHex8{}, render(pred)}

'''
PRIVATE = 'pg_input(PGNext{PGZero{}}, inputs)'


class MeijerAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.owner = self.root / 'owner'
        self.owner.mkdir()
        self.target = self.root / 'report'
        package = self.root / 'package'
        package.mkdir()
        for name in ('plugin.json', 'checks.bend'):
            (package / name).write_bytes((PACKAGE / name).read_bytes())
        (package / 'summary.bend').write_bytes((MEIJER / 'analysis/summary.bend').read_bytes())
        (package / 'check.mjs').write_bytes((MEIJER / 'analysis/check.mjs').read_bytes())
        self.plugin = Plugin.load(package)
        self.enrolled = self.plugin.enroll(self.owner, inputs_base64=[
            base64.b64encode(b'public').decode(), base64.b64encode(b'private').decode()],
            session={'identifier': 'analysis', 'expires': int(time.time()) + 300,
                     'destinations': [str(self.target)]})
        self.gateway = self.enrolled.build_gateway(self.owner)

    def reject(self, source, *, definition=None):
        with patch('proofguard.admission.execute', side_effect=AssertionError('rejected application ran')):
            with self.assertRaises(AdmissionRejected) as caught:
                self.gateway.submit(source)
        if definition is not None:
            diagnostic = json.loads(caught.exception.receipt['source_check']['diagnostics'])
            self.assertEqual(diagnostic['phase'], 'check_definitions', diagnostic)
            self.assertEqual(diagnostic['definition'], definition, diagnostic)
        self.assertFalse(self.target.exists())

    def test_public_interface_has_only_shared_types_and_main(self):
        restored = Enrollment.load(self.owner, self.enrolled.identity)
        task = restored.get_task(self.owner)
        self.assertEqual([source['path'] for source in task['interface']],
                         ['proofguard/effects.bend', 'proofguard/program.bend'])
        self.assertNotIn('pg_program_summary', json.dumps(task))
        self.assertEqual(restored.record['plugins'][0]['checker'], self.plugin.checker)

    def test_public_input_through_helper_executes_original_bend(self):
        source = '''def copy(value: PGBytes) -> PGBytes:
  value

def main(inputs):
  PGWrite{PGZero{}, copy(pg_input(PGZero{}, inputs)), PGDone{}}
'''
        self.gateway.check(source)
        self.assertFalse(self.target.exists())
        self.gateway.submit(source)
        self.assertEqual(self.target.read_bytes(), b'public')

    def test_helper_transformation_keeps_private_dependency(self):
        self.reject('''def transform(+value: PGBytes) -> PGBytes:
  pg_append(value, value)

def main(inputs):
  PGWrite{PGZero{}, transform(pg_input(PGNext{PGZero{}}, inputs)), PGDone{}}
''', definition='report_safe')

    def test_pattern_matching_inputs_cannot_bypass_dependency_check(self):
        self.reject('''def main(inputs):
  match inputs:
    case PGNoInputs{}: PGDone{}
    case PGInput{public, rest}:
      match rest:
        case PGNoInputs{}: PGDone{}
        case PGInput{private, tail}:
          PGWrite{PGZero{}, private, PGDone{}}
''', definition='report_safe')

    def test_private_branch_cannot_select_public_write_occurrence(self):
        self.reject('''def decide(value: PGBytes) -> PGIO:
  match value:
    case PGEndBytes{}: PGDone{}
    case PGByte{high, low, tail}:
      PGWrite{PGZero{}, PGEndBytes{}, PGDone{}}
    case PGChunks{left, right}: PGDone{}

def main(inputs):
  decide(pg_input(PGNext{PGZero{}}, inputs))
''', definition='report_safe')

    def test_private_match_in_helper_cannot_select_public_constant(self):
        self.reject('''def pick(value: PGBytes) -> PGBytes:
  match value:
    case PGEndBytes{}: pg_empty()
    case PGByte{high, low, tail}: pg_byte(PGHex4{}, PGHex1{}, pg_empty())
    case PGChunks{left, right}: pg_empty()

def main(inputs):
  pg_write(PGZero{}, pick(pg_input(PGNext{PGZero{}}, inputs)))
''', definition='report_safe')

    def test_private_data_in_custom_constructor_keeps_its_dependency(self):
        self.reject('''type Box is Data:
  Packed{value: PGBytes}

def unpack(value: Box) -> PGBytes:
  match value:
    case Packed{bytes}: bytes

def main(inputs):
  wrapped = {Packed{pg_input(PGNext{PGZero{}}, inputs)} : Box}
  pg_write(PGZero{}, unpack(wrapped))
''', definition='report_safe')

    def test_candidate_cannot_forge_analysis_binding_or_private_law(self):
        for source in (
            'def pg_program_summary():\n  PGProgramSummary{PGNoEffects{}, PGNoDependencies{}}\n\ndef main(inputs):\n  PGDone{}\n',
            'def report_safe():\n  {==}\n\ndef main(inputs):\n  PGDone{}\n',
        ):
            with self.subTest(source=source):
                self.reject(source)

    def test_unknown_destination_and_out_of_range_input_reject(self):
        self.reject('''def main(inputs):
  PGWrite{PGZero{}, pg_input(PGNext{PGNext{PGZero{}}}, inputs), PGDone{}}
''')

    def test_checker_is_bound_to_execution_receipt(self):
        source = 'def main(inputs):\n  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}\n'
        prepared = self.gateway.check(source)
        receipt = json.loads(prepared.program.receipt_bytes)['plugins'][0]
        self.assertEqual(receipt['inputs']['plugin_check'], {'module': self.plugin.checker, 'input_count': 2})
        self.assertEqual(receipt['kernel_check']['diagnostics'].strip(), 'ALL PROOFS CHECK')

    def test_unselected_forbidden_write_is_still_checked(self):
        self.reject('''def choose(value: PGBytes) -> PGIO:
  match value:
    case PGEndBytes{}: PGWrite{PGNext{PGZero{}}, PGEndBytes{}, PGDone{}}
    case PGByte{high, low, tail}: PGDone{}
    case PGChunks{left, right}: PGDone{}

def main(inputs):
  choose(pg_input(PGZero{}, inputs))
''', definition='report_safe')

    def test_unknown_recursive_effects_reject_instead_of_assuming_no_writes(self):
        self.reject('''def repeat(value: PGBytes) -> PGIO:
  match value:
    case PGEndBytes{}: PGDone{}
    case PGByte{high, low, tail}: PGWrite{PGZero{}, PGEndBytes{}, repeat(tail)}
    case PGChunks{left, right}: PGDone{}

def main(inputs):
  repeat(pg_input(PGZero{}, inputs))
''')

    def test_shared_helpers_need_no_plugin_authoring_library(self):
        self.gateway.submit('''def main(inputs):
  bytes = pg_byte(PGHex4{}, PGHex1{}, pg_empty())
  pg_write(PGZero{}, bytes)
''')
        self.assertEqual(self.target.read_bytes(), b'A')

    def test_private_source_inside_a_helper_cannot_hide_behind_summary(self):
        self.reject('''def secret(inputs: PGInputs) -> PGBytes:
  pg_input(PGNext{PGZero{}}, inputs)

def main(inputs):
  pg_write(PGZero{}, secret(inputs))
''', definition='report_safe')

    def test_nested_input_lookup_is_not_skipped_when_summarizing_helpers(self):
        self.reject('''def inner(inputs: PGInputs) -> PGBytes:
  pg_input(PGNext{PGNext{PGZero{}}}, inputs)

def outer(inputs: PGInputs) -> PGBytes:
  inner(inputs)

def main(inputs):
  pg_write(PGZero{}, outer(inputs))
''')

    def test_recursive_helper_returning_plain_data_executes(self):
        self.gateway.submit(COUNT + '''def main(inputs):
  PGWrite{PGZero{}, render(count(pg_input(PGZero{}, inputs), Zero{})), PGDone{}}
''')
        self.assertEqual(self.target.read_bytes(), b'xxxxxx')

    def test_plain_summary_keeps_private_dependency_in_written_value(self):
        self.reject(COUNT + f'''def main(inputs):
  PGWrite{{PGZero{{}}, render(count({PRIVATE}, Zero{{}})), PGDone{{}}}}
''', definition='report_safe')

    def test_plain_summary_keeps_private_dependency_in_branch(self):
        self.reject(COUNT + f'''def decide(total: Nat) -> PGIO:
  match total:
    case Zero{{}}: PGDone{{}}
    case Succ{{pred}}: PGWrite{{PGZero{{}}, PGEndBytes{{}}, PGDone{{}}}}

def main(inputs):
  decide(count({PRIVATE}, Zero{{}}))
''', definition='report_safe')

    def test_plain_summary_keeps_private_dependency_in_abort(self):
        self.reject(COUNT + f'''def decide(total: Nat) -> PGIO:
  match total:
    case Zero{{}}: PGAbort{{}}
    case Succ{{pred}}: PGDone{{}}

def main(inputs):
  PGWrite{{PGZero{{}}, PGEndBytes{{}}, decide(count({PRIVATE}, Zero{{}}))}}
''', definition='report_safe')

    def test_plain_summary_keeps_dependency_in_custom_datatype(self):
        self.reject(COUNT + f'''type Tally is Data:
  Tally{{total: Nat, bytes: PGBytes}}

def tally(bytes: PGBytes, total: Nat) -> Tally:
  match bytes:
    case PGEndBytes{{}}: Tally{{total, PGEndBytes{{}}}}
    case PGByte{{high, low, tail}}: tally(tail, Succ{{total}})
    case PGChunks{{left, right}}: tally(right, total)

def show(value: Tally) -> PGBytes:
  match value:
    case Tally{{total, bytes}}: render(total)

def main(inputs):
  PGWrite{{PGZero{{}}, show(tally({PRIVATE}, Zero{{}})), PGDone{{}}}}
''', definition='report_safe')

    def test_plain_summary_keeps_dependency_of_captured_closure(self):
        self.reject(COUNT + f'''def twist(transform: PGBytes -> Nat, value: PGBytes) -> Nat:
  transform(value)

def main(inputs):
  secret = {PRIVATE}
  PGWrite{{PGZero{{}}, render(twist(value => count(secret, Zero{{}}), PGEndBytes{{}})), PGDone{{}}}}
''', definition='report_safe')

    def test_type_holding_effects_is_followed_instead_of_summarized(self):
        plan = '''type Plan is Type:
  Plan{io: PGIO}

def plan(value: PGBytes) -> Plan:
  Plan{PGWrite{PGZero{}, value, PGDone{}}}

def run(value: Plan) -> PGIO:
  match value:
    case Plan{io}: io

def main(inputs):
  run(plan(%s))
'''
        self.reject(plan % PRIVATE, definition='report_safe')
        self.gateway.submit(plan % 'pg_input(PGZero{}, inputs)')
        self.assertEqual(self.target.read_bytes(), b'public')

    def test_main_may_restate_its_law_type(self):
        body = '  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}\n'
        for header in ('def main(inputs: PGInputs) -> PGIO:\n', 'def main(inputs: PGInputs):\n'):
            with self.subTest(header=header):
                # The agent matches its answer by this id, so it names the submitted text.
                self.assertEqual(self.gateway.check(header + body).source_id,
                                 commitment('source', (header + body).encode()))
        self.gateway.submit('def main(inputs: PGInputs) -> PGIO:\n' + body)
        self.assertEqual(self.target.read_bytes(), b'public')

    def test_execution_context_cannot_add_inputs_after_analysis(self):
        prepared = self.gateway.check('def main(inputs):\n  PGDone{}\n')
        with patch('proofguard.admission.execute', side_effect=AssertionError('wrong context executed')):
            with self.assertRaisesRegex(ContractError, 'input count'):
                self.gateway.admission.execute_program(prepared.program, context=ExecutionContext((b'extra',)))

    def test_program_cannot_read_gateway_derived_summary(self):
        self.reject('''def main(inputs):
  summary = pg_program_summary()
  PGDone{}
''')
        self.reject('''def choose(summary: PGProgramSummary, value: PGBytes) -> PGBytes:
  match summary:
    case PGProgramSummary{effects, reads}: value

def mask(value: PGBytes) -> PGBytes:
  choose(pg_program_summary(), value)

def main(inputs):
  pg_write(PGZero{}, mask(pg_input(PGZero{}, inputs)))
''')


if __name__ == '__main__':
    unittest.main()
