"""Command effects run only owner-enrolled templates, with no shell between them.

Every command here is a local system binary given a fixed script or arguments.
"""

import json
from pathlib import Path
import tempfile
import time
import unittest

from bendspec.model import ContractError
from proofguard.commands import (MAX_OUTPUT_BYTES, AuthorizedRun, Command, CommandRunner, Slot, parse_commands)
from proofguard.effects import CommandRun, EffectDenied, ProgramAborted, RunExit, RunFailure
from proofguard.gateway import EffectUncertain, ExecutionRefused, ExecutionStopped
from proofguard.plugin import Plugin
from proofguard.sandbox import AuthorSandbox
from proofguard.worker import OutboxWorker

ROOT = Path(__file__).resolve().parents[2]
ZERO, ONE = "PGZero{}", "PGNext{PGZero{}}"
REPORT = '''def report(result: PGExit) -> PGIO:
  match result:
    case PGExited{PGHex0{}, PGHex0{}, output}: pg_write(PGZero{}, output)
    case PGExited{high, low, output}: pg_write(PGZero{}, pg_text("failed"))
    case PGRunFailed{failure}: pg_write(PGZero{}, pg_text("not run"))

'''


def script(text: str, *slots) -> dict:
    """A fixed sh script; its slots arrive as $1, $2… and never as code."""
    return {"argv": ["/bin/sh", "-c", text, "sh", *({"slot": i} for i in range(len(slots)))],
            "slots": list(slots), "timeout": 5}


def run(command: str, *args: str) -> str:
    values = "PGNoArgs{}"
    for value in reversed(args):
        values = f'PGArg{{pg_text("{value}"), {values}}}'
    return f"PGRun{{{command}, {values}, report}}"


def authorized(*argv: str, timeout=5.0, environment=(), directory=Path("/")) -> AuthorizedRun:
    return AuthorizedRun(tuple(item.encode() for item in argv), environment, directory, timeout)


class TemplateTests(unittest.TestCase):
    def test_enrollment_syntax_is_closed_and_round_trips(self):
        template = {"argv": ["/usr/bin/printf", "%s", {"slot": 0}],
                    "slots": [{"chars": "a-z.", "max_bytes": 64}], "timeout": 3,
                    "cwd": "/tmp", "env": {"LANG": "C"}}
        command = Command.parse(template)
        self.assertEqual(Command.parse(command.data()), command)
        self.assertEqual(Command.parse({"argv": ["/bin/true"]}).data(),
                         {"argv": ["/bin/true"], "timeout": 10, "cwd": "/", "env": {}})
        for bad in ({**template, "shell": True}, {**template, "argv": []}, {**template, "argv": ["printf"]},
                    {**template, "argv": [{"slot": 0}, "x"]}, {**template, "argv": ["/usr/bin/printf"]},
                    {**template, "argv": ["/usr/bin/printf", {"slot": 0}, {"slot": 0}]},
                    {**template, "argv": ["/usr/bin/printf", {"slot": 1}]}, {**template, "timeout": 0},
                    {**template, "timeout": 61}, {**template, "cwd": "tmp"}, {**template, "env": {"A=B": "c"}},
                    {**template, "argv": ["/usr/bin/printf", "a\0b", {"slot": 0}]},
                    {**template, "slots": [{"chars": "z-a", "max_bytes": 4}]},
                    {**template, "slots": [{"chars": "", "max_bytes": 4}]},
                    {**template, "slots": [{"chars": "a\tb", "max_bytes": 4}]},
                    {**template, "slots": [{"chars": "é", "max_bytes": 4}]},
                    {**template, "slots": [{"chars": "x", "max_bytes": 0}]},
                    {**template, "slots": [{"pattern": "[a-z]+", "max_bytes": 4}]},
                    {**template, "slots": [{"one_of": []}]}, {**template, "slots": [{"one_of": ["a", "a"]}]}):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                Command.parse(bad)

    def test_programs_that_run_their_arguments_cannot_take_free_slots(self):
        free = {"chars": "a-z", "max_bytes": 8}
        for program in ("/bin/sh", "/usr/bin/env", "/usr/bin/python3", "/usr/bin/xargs", "/usr/bin/ionice",
                        "/lib64/ld-linux-x86-64.so.2"):
            with self.subTest(program=program), self.assertRaises(ContractError):
                parse_commands([{"argv": [program, "-c", {"slot": 0}], "slots": [free]}], ())
        parse_commands([script('printf %s "$1"', {"one_of": ["a", "b"]})], ())
        parse_commands([{"argv": ["/usr/bin/printf", "%s", {"slot": 0}], "slots": [free]}], ())

    def test_a_template_cannot_run_or_interpret_a_write_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "script"
            with self.assertRaises(ContractError):
                parse_commands([{"argv": [str(target)]}], (target,))
            for argv, slots in ((["/bin/sh", str(target)], []),
                                (["/bin/sh", {"slot": 0}], [{"one_of": [str(target)]}]),
                                (["/usr/bin/ionice", "/bin/sh", str(target)], []),
                                (["/lib64/ld-linux-x86-64.so.2", str(target)], []),
                                (["/bin/bash", f"--rcfile={target}", "-i"], []),
                                (["/usr/bin/awk", f"-f{target}"], [])):
                with self.subTest(argv=argv), self.assertRaises(ContractError):
                    parse_commands([{"argv": argv, "slots": slots}], (target,))
            # A relative script is read from the command's directory.
            with self.assertRaises(ContractError):
                parse_commands([{"argv": ["/bin/sh", "script"], "cwd": directory}], (target,))
            # A destination a program only reads, such as a configuration file, is allowed.
            parse_commands([{"argv": ["/bin/cat", str(target)]}], (target,))

    def test_arguments_must_fit_their_slots(self):
        choice, text = Slot.parse({"one_of": ["example.com"]}), Slot.parse({"chars": "a-z.-", "max_bytes": 12})
        self.assertTrue(choice.accepts(b"example.com"))
        self.assertTrue(text.accepts(b"example.org"))
        self.assertTrue(text.accepts(b"my-site.org"))
        for value in (b"example.co", b"example.com ", b"EXAMPLE.COM"):
            self.assertFalse(choice.accepts(value))
        for value in (b"", b"-rf", b"--output=x", b"a\0b", b"a" * 13, b"a b", b"a\nb", b"a\xffb"):
            with self.subTest(value=value):
                self.assertFalse(text.accepts(value))
        # Checking is linear: a long value an owner's set rejects late is still fast.
        started = time.monotonic()
        self.assertFalse(Slot.parse({"chars": "a", "max_bytes": 4096}).accepts(b"a" * 4095 + b"b"))
        self.assertLess(time.monotonic() - started, 0.1)
        command = Command.parse({"argv": ["/usr/bin/printf", "%s", {"slot": 0}],
                                 "slots": [{"chars": "a-z", "max_bytes": 8}]})
        self.assertEqual(command.authorize(CommandRun(0, (b"ok",)), 99).argv, (b"/usr/bin/printf", b"%s", b"ok"))
        self.assertEqual(command.authorize(CommandRun(0, (b"ok",)), 99).timeout, 10)
        # A command the run's deadline would cut short never starts.
        for args, remaining in (((), 99), ((b"ok", b"extra"), 99), ((b"-x",), 99), ((b"ok",), 9.9)):
            with self.subTest(args=args, remaining=remaining), self.assertRaises(EffectDenied):
                command.authorize(CommandRun(0, args), remaining)


class RunnerTests(unittest.TestCase):
    def test_exit_status_and_combined_output_are_returned(self):
        finish = CommandRunner().run(authorized("/bin/sh", "-c", "printf out; printf err >&2; exit 7"))
        self.assertEqual((finish.result, finish.started, finish.ambiguous), (RunExit(7, b"outerr"), True, False))

    def test_the_command_gets_only_its_template_environment(self):
        finish = CommandRunner().run(authorized("/bin/sh", "-c", 'printf "%s|%s|" "$LANG" "$HOME"; pwd; cat',
                                                environment=(("LANG", "C"),), directory=Path("/tmp")))
        # HOME is unset, cwd is the template's, and stdin is empty so cat ends at once.
        self.assertEqual(finish.result, RunExit(0, b"C||/tmp\n"))

    def test_oversized_output_and_signals_are_known_failures(self):
        oversized = CommandRunner().run(authorized("/usr/bin/head", "-c", str(MAX_OUTPUT_BYTES + 1), "/dev/zero"))
        self.assertEqual((oversized.result, oversized.ambiguous), (RunFailure.OVERSIZED, False))
        signalled = CommandRunner().run(authorized("/bin/sh", "-c", "kill -9 $$"))
        self.assertEqual((signalled.result, signalled.ambiguous), (RunFailure.SIGNALLED, False))

    def test_a_missing_program_never_starts(self):
        finish = CommandRunner().run(authorized("/nonexistent/program"))
        self.assertEqual((finish.result, finish.started, finish.ambiguous), (RunFailure.UNSTARTED, False, False))

    def test_a_timeout_kills_the_group_and_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "late"
            started = time.monotonic()
            finish = CommandRunner().run(authorized("/bin/sh", "-c", f"(sleep 1; touch {marker}) & sleep 5",
                                                    timeout=0.3))
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual((finish.result, finish.started, finish.ambiguous), (RunFailure.TIMEOUT, True, True))
            time.sleep(1.5)
            self.assertFalse(marker.exists())

    def test_a_left_running_descendant_is_killed_and_makes_the_outcome_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "late"
            started = time.monotonic()
            finish = CommandRunner().run(authorized("/bin/sh", "-c", f"(sleep 1; touch {marker}) & printf done",
                                                    timeout=5))
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual((finish.result, finish.started, finish.ambiguous), (RunExit(0, b"done"), True, True))
            time.sleep(1.5)
            self.assertFalse(marker.exists())


class GatewayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.owner = self.root / "owner"
        self.owner.mkdir()
        self.first, self.second, self.log = self.root / "first", self.root / "second", self.root / "log"

    def enroll(self, commands):
        enrollment = Plugin.load(ROOT / "tests/fixtures/run").enroll(
            self.owner, inputs_base64=[],
            session={"identifier": "run", "expires": int(time.time()) + 300,
                     "destinations": [str(self.first), str(self.second)], "commands": commands})
        return enrollment.build_gateway(self.owner)

    def records(self, gateway, kind):
        rows = [json.loads(path.read_text()) for path in gateway.audit.root.glob("*.json")]
        return [row for row in rows if row["kind"] == kind]

    def test_output_reaches_the_program_and_the_audit_records_the_command(self):
        gateway = self.enroll([script('printf "hello %s" "$1"', {"one_of": ["world", "there"]})])
        receipt = gateway.submit(REPORT + f'def main(inputs):\n  {run(ZERO, "world")}\n')
        self.assertEqual(self.first.read_bytes(), b"hello world")
        self.assertEqual(receipt.commands[0][2], "exit 0")
        started, finished = self.records(gateway, "run_started"), self.records(gateway, "run_finished")
        self.assertEqual((len(started), len(finished)), (1, 1))
        self.assertEqual((finished[0]["outcome"], finished[0]["output_bytes"]), ("exit 0", len(b"hello world")))
        prepared = [row for row in self.records(gateway, "prepared_effects") if "run" in row]
        self.assertEqual(prepared[0]["run"]["arguments"], started[0]["arguments"])
        for path in gateway.audit.root.glob("*.json"):
            self.assertNotIn("hello world", path.read_text())

    def test_writes_before_a_command_happen_first_and_it_runs_once(self):
        # The command sees the earlier write, and replays after it never run it again.
        gateway = self.enroll([script(f'cat {self.first} >> {self.log}; printf ok')])
        gateway.submit(REPORT.replace("PGZero{}, output", f"{ONE}, output") + "def main(inputs):\n  "
                       f'PGWrite{{{ZERO}, pg_text("before"), {run(ZERO)}}}\n')
        self.assertEqual((self.log.read_bytes(), self.second.read_bytes()), (b"before", b"ok"))

    def test_the_program_branches_on_the_exit_status(self):
        gateway = self.enroll([script("exit 3")])
        gateway.submit(REPORT + f"def main(inputs):\n  {run(ZERO)}\n")
        self.assertEqual(self.first.read_bytes(), b"failed")

    def test_an_argument_outside_its_slot_stops_the_step_before_any_effect(self):
        gateway = self.enroll([script(f'printf x >> {self.log}', {"one_of": ["a"]})])
        with self.assertRaises(ExecutionRefused) as refused:
            gateway.submit(REPORT + f'def main(inputs):\n  PGWrite{{{ONE}, pg_text("w"), {run(ZERO, "b")}}}\n')
        self.assertIsInstance(refused.exception.__cause__, EffectDenied)
        self.assertFalse(self.log.exists() or self.second.exists())

    def test_no_enrolled_command_means_no_command(self):
        gateway = self.enroll([])
        with self.assertRaises(ExecutionRefused):
            gateway.submit(REPORT + f"def main(inputs):\n  {run(ZERO)}\n")

    def test_abort_after_a_command_stops_and_keeps_it(self):
        stop = "def stop(result: PGExit) -> PGIO:\n  PGWrite{PGZero{}, pg_text(\"late\"), PGAbort{}}\n\n"
        gateway = self.enroll([script(f"printf x >> {self.log}")])
        with self.assertRaises(ExecutionStopped) as stopped:
            gateway.submit(stop + f"def main(inputs):\n  PGRun{{{ZERO}, PGNoArgs{{}}, stop}}\n")
        self.assertIsInstance(stopped.exception.__cause__, ProgramAborted)
        self.assertEqual(len(stopped.exception.commands), 1)
        self.assertEqual(stopped.exception.stopped_at, (2, 1))
        self.assertEqual(self.log.read_bytes(), b"x")
        self.assertFalse(self.first.exists())

    def test_a_timed_out_command_reaches_the_program_and_stays_uncertain(self):
        gateway = self.enroll([{**script("sleep 5"), "timeout": 1}])
        with self.assertRaises(EffectUncertain) as uncertain:
            gateway.submit(REPORT + f"def main(inputs):\n  {run(ZERO)}\n")
        self.assertEqual(self.first.read_bytes(), b"not run")
        self.assertEqual(uncertain.exception.commands[0][2], "timeout")
        self.assertEqual(uncertain.exception.in_doubt, ((1, 1),))
        self.assertEqual(self.records(gateway, "run_finished")[0]["ambiguous"], True)

    def test_programs_stay_visible_to_the_agent_and_never_writable(self):
        gateway = self.enroll([{"argv": ["/usr/bin/printf", "x"]}])
        for name in ("runtime", "task"):
            (self.root / name).mkdir()
        worker = OutboxWorker(gateway, self.root / "outbox", self.root / "results")
        AuthorSandbox(worker, runtime=self.root / "runtime", task=self.root / "task")
        program = self.root / "outbox" / "tool"
        with self.assertRaises(ContractError):
            OutboxWorker(self.enroll([{"argv": [str(program)]}]), self.root / "outbox", self.root / "results")

    def test_a_command_that_cannot_start_is_no_effect(self):
        gateway = self.enroll([{"argv": [str(self.root / "missing")]}])
        receipt = gateway.submit(REPORT + f"def main(inputs):\n  {run(ZERO)}\n")
        self.assertEqual((self.first.read_bytes(), receipt.commands[0][2]), (b"not run", "unstarted"))


if __name__ == "__main__":
    unittest.main()
