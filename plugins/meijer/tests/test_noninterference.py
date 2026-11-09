"""Compare analysis facts with real execution across different private inputs.

Accepted programs run twice, differing only in one private input. The outcome
the agent reads and the public output must match, because the extractor's
correspondence to Bend execution is otherwise only trusted.
"""

from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest

from plugins.meijer.owner.configuration import Guardrails, Input, Label, NonEmpty, AnyPayload
from plugins.meijer.owner.enrollment import MeijerPlugin
from proofguard.admission import AdmissionRejected
from proofguard.gateway import GatewayError

SECRET = 'pg_input(PGNext{PGZero{}}, inputs)'
PUBLIC = 'pg_input(PGZero{}, inputs)'
CONSTANT = 'pg_byte(PGHex4{}, PGHex1{}, pg_empty())'
GATE = '''def gate(value: PGBytes) -> PGIO:
  match value:
    case PGEndBytes{}: PGDone{}
    case PGByte{high, low, tail}: PGAbort{}
    case PGChunks{left, right}: PGDone{}

'''
PICK = '''def pick(value: PGBytes) -> PGBytes:
  match value:
    case PGEndBytes{}: pg_empty()
    case PGByte{high, low, tail}: %s
    case PGChunks{left, right}: pg_empty()

''' % CONSTANT
BOX = '''type Box is Data:
  Box{value: PGBytes}

def unbox(box: Box) -> PGBytes:
  match box:
    case Box{value}: value

'''
COUNT = '''def count(value: PGBytes, total: Nat) -> Nat:
  match value:
    case PGEndBytes{}: total
    case PGByte{high, low, tail}: count(tail, Succ{total})
    case PGChunks{left, right}: count(right, count(left, total))

def render(total: Nat) -> PGBytes:
  match total:
    case Zero{}: pg_empty()
    case Succ{pred}: pg_byte(PGHex4{}, PGHex1{}, render(pred))

def gate_count(total: Nat) -> PGIO:
  match total:
    case Zero{}: PGDone{}
    case Succ{pred}: PGAbort{}

'''
FLAT_COUNT = COUNT + '''def flat_count(value: PGFlat, total: Nat) -> Nat:
  match value:
    case PGFlatEnd{}: total
    case PGFlatByte{high, low, tail}: flat_count(tail, Succ{total})

'''
ACCEPTED = {
    'public-copy': f'def main(inputs):\n  pg_write(PGZero{{}}, {PUBLIC})\n',
    'constant': f'def main(inputs):\n  pg_write(PGZero{{}}, {CONSTANT})\n',
    'public-abort': GATE + f'def main(inputs):\n  gate({PUBLIC})\n',
    'unconditional-abort': f'def main(inputs):\n  PGWrite{{PGZero{{}}, {CONSTANT}, PGAbort{{}}}}\n',
    'public-wrapper': BOX + f'def main(inputs):\n  pg_write(PGZero{{}}, unbox(Box{{{PUBLIC}}}))\n',
    'public-flatten': f'def main(inputs):\n  pg_write(PGZero{{}}, pg_unflatten(pg_flatten(pg_append({PUBLIC}, {CONSTANT}))))\n',
    'public-flat-count': FLAT_COUNT + f'def main(inputs):\n  pg_write(PGZero{{}}, render(flat_count(pg_flatten({PUBLIC}), Zero{{}})))\n',
    'public-count': COUNT + f'def main(inputs):\n  pg_write(PGZero{{}}, render(count({PUBLIC}, Zero{{}})))\n',
}
REJECTED = {
    'direct-leak': f'def main(inputs):\n  pg_write(PGZero{{}}, {SECRET})\n',
    'flatten-leak': f'def main(inputs):\n  pg_write(PGZero{{}}, pg_unflatten(pg_flatten({SECRET})))\n',
    'flat-count-leak': FLAT_COUNT + f'def main(inputs):\n  pg_write(PGZero{{}}, render(flat_count(pg_flatten({SECRET}), Zero{{}})))\n',
    'count-leak': COUNT + f'def main(inputs):\n  pg_write(PGZero{{}}, render(count({SECRET}, Zero{{}})))\n',
    'count-abort': COUNT + f'def main(inputs):\n  PGWrite{{PGZero{{}}, {CONSTANT}, gate_count(count({SECRET}, Zero{{}}))}}\n',
    'abort-after-write': GATE + f'def main(inputs):\n  PGWrite{{PGZero{{}}, {CONSTANT}, gate({SECRET})}}\n',
    'abort-without-write': GATE + f'def main(inputs):\n  gate({SECRET})\n',
    'selected-constant': PICK + f'def main(inputs):\n  pg_write(PGZero{{}}, pick({SECRET}))\n',
    'wrapped-leak': BOX + f'def main(inputs):\n  pg_write(PGZero{{}}, unbox(Box{{{SECRET}}}))\n',
    'ignored-by-helper': f'def drop(value: PGBytes) -> PGBytes:\n  pg_empty()\n\ndef main(inputs):\n  pg_write(PGZero{{}}, drop({SECRET}))\n',
}
BOTH_SINKS = f'''def write_both(+inputs: PGInputs) -> PGIO:
  PGWrite{{PGZero{{}}, {PUBLIC}, PGWrite{{PGNext{{PGZero{{}}}}, {SECRET}, PGDone{{}}}}}}

def main(inputs):
  write_both(inputs)
'''


class NoninterferenceTests(unittest.TestCase):
    def observe(self, source, secret, *, clearances=(Label.PUBLIC,), policy=None):
        """Return what the agent reads and what a public observer sees."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner = root / 'owner'
            owner.mkdir()
            plugin = MeijerPlugin((Input(b'public', Label.PUBLIC), Input(secret, Label.PRIVATE)), clearances,
                                  guardrails=policy)
            output = root / 'public'
            destinations = [str(output), *(str(root / f'private-{i}') for i in range(1, len(clearances)))]
            gateway = plugin.enroll(owner, {'identifier': 'compare', 'expires': int(time.time()) + 300,
                                            'destinations': destinations}).build_gateway(owner)
            try:
                gateway.submit(source)
                outcome = 'completed'
            except AdmissionRejected:
                outcome = 'rejected'
            except GatewayError as error:
                outcome = type(error).__name__
            return outcome, output.read_bytes() if output.exists() else None

    def compare(self, source, **settings):
        first = self.observe(source, b'', **settings)
        self.assertEqual(first, self.observe(source, b'K\xff', **settings))
        return first

    def test_accepted_programs_give_identical_observations_for_any_secret(self):
        for name, source in ACCEPTED.items():
            with self.subTest(program=name):
                self.assertNotEqual(self.compare(source)[0], 'rejected')

    def test_flows_from_the_secret_reject_with_either_secret(self):
        for name, source in REJECTED.items():
            with self.subTest(program=name):
                self.assertEqual(self.compare(source)[0], 'rejected')

    def test_concrete_checks_cannot_couple_a_private_sink_to_a_public_write(self):
        defaults = Guardrails.defaults(2)
        for policy in (replace(defaults, preconditions=(AnyPayload(), NonEmpty())),
                       replace(defaults, max_write_bytes=1)):
            with self.subTest(policy=policy):
                self.assertEqual(self.compare(BOTH_SINKS, clearances=(Label.PUBLIC, Label.PRIVATE), policy=policy)[0],
                                 'rejected')

    def test_private_sink_does_not_change_public_output(self):
        observed = self.compare(BOTH_SINKS, clearances=(Label.PUBLIC, Label.PRIVATE))
        self.assertEqual(observed, ('completed', b'public'))


if __name__ == '__main__':
    unittest.main()
