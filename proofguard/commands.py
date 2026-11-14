"""Owner-enrolled command templates and the bounded runner that executes them.

A program never builds a command line: it names a template and gives one byte
string per slot, and the foundation checks each value against its slot at
dispatch. What a template can reach is the owner's decision; enrollment refuses
only combinations that plainly hand the agent arbitrary execution.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time

from bendspec.model import ContractError, fields, text
from .effects import CommandRun, EffectDenied, RunExit, RunFailure, RunResult

MAX_COMMANDS = 16
MAX_ARGV = 64
MAX_SLOTS = 16
MAX_ARG_BYTES = 4096
MAX_CHOICES = 64
MAX_CHARS = 256
MAX_ENVIRONMENT = 32
MAX_OUTPUT_BYTES = 65536
MAX_TIMEOUT_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 10
ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
# Programs that run their arguments as code, directly or through a wrapper. A
# free slot on one of these, or a destination passed to one, would let the agent
# choose what executes. The list cannot be complete; it catches the common cases
# at enrollment, and the owner remains responsible for what a template can reach.
EXECUTORS = re.compile(r"(?:ba|da|z|k|c|tc|fi|a|r)?sh|busybox|toybox|env|xargs|parallel|nice|ionice|chrt"
                       r"|taskset|nohup|timeout|stdbuf|setsid|setpriv|capsh|flock|unshare|nsenter|chroot"
                       r"|bwrap|firejail|proot|systemd-run|script|watch|time|strace|ltrace|gdb|valgrind"
                       r"|sudo|su|doas|runuser|pkexec|ssh|scp|rsync|find|awk|gawk|mawk|nawk|sed|ed|m4"
                       r"|vim?|nvim|ex|emacs|less|more|man|git|tar|zip|make|cmake|perl|ruby|irb|php|lua"
                       r"|tclsh|wish|node|nodejs|deno|bun|python[0-9.]*|pypy[0-9.]*|R|Rscript|julia|java"
                       r"|osascript|expect|sqlite3|psql|mysql|gnuplot|ld\.so|ld-.*\.so.*")
PRINTABLE = range(0x20, 0x7F)


def parse_chars(value: str) -> frozenset[int]:
    """Read a character set such as "a-z0-9._-": ranges, and literal characters.

    A dash is literal at either end. Only printable ASCII is allowed, so a slot
    can never carry control characters.
    """
    data = value.encode()
    if not data or len(data) > MAX_CHARS or any(byte not in PRINTABLE for byte in data):
        raise ContractError(f"slot chars must be 1..{MAX_CHARS} printable ASCII characters")
    allowed, index = set(), 0
    while index < len(data):
        if index + 2 < len(data) and data[index + 1] == ord("-"):
            if data[index] > data[index + 2]:
                raise ContractError("slot chars range runs backwards")
            allowed.update(range(data[index], data[index + 2] + 1))
            index += 3
        else:
            allowed.add(data[index])
            index += 1
    return frozenset(allowed)


def encode_argument(value: str, name: str) -> bytes:
    data = os.fsencode(text(value, name))
    if b"\0" in data or len(data) > MAX_ARG_BYTES:
        raise ContractError(f"{name} must be at most {MAX_ARG_BYTES} bytes without NUL")
    return data


@dataclass(frozen=True)
class Slot:
    """One program-chosen argument: an exact choice, or bounded text from a set.

    A character set, not a regular expression: checking a value then takes
    linear time whatever the owner enrolled.
    """

    choices: tuple[bytes, ...] = ()
    chars: str | None = None
    max_bytes: int = 0

    @classmethod
    def parse(cls, value: object) -> "Slot":
        if type(value) is dict and "one_of" in value:
            row = fields(value, {"one_of"}, "command slot")
            if not isinstance(row["one_of"], list) or not 1 <= len(row["one_of"]) <= MAX_CHOICES:
                raise ContractError(f"one_of lists 1..{MAX_CHOICES} values")
            choices = tuple(encode_argument(item, "slot choice") for item in row["one_of"])
            if len(set(choices)) != len(choices):
                raise ContractError("slot choices must be distinct")
            return cls(choices=choices)
        row = fields(value, {"chars", "max_bytes"}, "command slot")
        chars = text(row["chars"], "slot chars")
        parse_chars(chars)
        if type(row["max_bytes"]) is not int or not 1 <= row["max_bytes"] <= MAX_ARG_BYTES:
            raise ContractError(f"max_bytes must be 1..{MAX_ARG_BYTES}")
        return cls(chars=chars, max_bytes=row["max_bytes"])

    def data(self) -> dict:
        if self.chars is None:
            return {"one_of": [os.fsdecode(item) for item in self.choices]}
        return {"chars": self.chars, "max_bytes": self.max_bytes}

    def accepts(self, value: bytes) -> bool:
        if self.chars is None:
            return value in self.choices
        # A leading dash would let the value act as an option of the command.
        allowed = parse_chars(self.chars)
        return (1 <= len(value) <= self.max_bytes and not value.startswith(b"-")
                and all(byte in allowed for byte in value))


@dataclass(frozen=True)
class AuthorizedRun:
    """A command line that passed its template's checks, ready for the runner."""

    argv: tuple[bytes, ...]
    environment: tuple[tuple[str, str], ...]
    directory: Path
    timeout: float


@dataclass(frozen=True)
class Command:
    """One allow-list entry: a fixed program and arguments with numbered slots."""

    # Each item is fixed bytes or the number of the slot that fills it.
    argv: tuple[bytes | int, ...]
    slots: tuple[Slot, ...]
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    directory: Path = Path("/")
    environment: tuple[tuple[str, str], ...] = ()

    @classmethod
    def parse(cls, value: object) -> "Command":
        optional = {"slots", "timeout", "cwd", "env"} & set(value) if type(value) is dict else set()
        row = fields(value, {"argv"} | optional, "command template")
        if not isinstance(row["argv"], list) or not 1 <= len(row["argv"]) <= MAX_ARGV:
            raise ContractError(f"argv lists 1..{MAX_ARGV} items")
        raw_slots = row.get("slots", [])
        if not isinstance(raw_slots, list) or len(raw_slots) > MAX_SLOTS:
            raise ContractError(f"a template has at most {MAX_SLOTS} slots")
        argv, used = [], []
        for item in row["argv"]:
            if type(item) is dict:
                slot = fields(item, {"slot"}, "argv slot")["slot"]
                if type(slot) is not int or not 0 <= slot < len(raw_slots):
                    raise ContractError("argv names a slot that is not declared")
                argv.append(slot)
                used.append(slot)
            else:
                argv.append(encode_argument(item, "argv item"))
        if sorted(used) != list(range(len(raw_slots))):
            raise ContractError("each slot fills exactly one argv item")
        if not isinstance(argv[0], bytes) or not argv[0].startswith(b"/"):
            raise ContractError("a template's program is a fixed absolute path")
        timeout = row.get("timeout", DEFAULT_TIMEOUT_SECONDS)
        if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            raise ContractError(f"command timeout must be 1..{MAX_TIMEOUT_SECONDS} seconds")
        directory = Path(text(row.get("cwd", "/"), "command directory"))
        if not directory.is_absolute():
            raise ContractError("command directory must be absolute")
        environment = row.get("env", {})
        if type(environment) is not dict or len(environment) > MAX_ENVIRONMENT:
            raise ContractError(f"env maps at most {MAX_ENVIRONMENT} names")
        for name, setting in environment.items():
            if not ENVIRONMENT_NAME.fullmatch(name):
                raise ContractError("invalid environment name")
            encode_argument(setting, "environment value")
        return cls(tuple(argv), tuple(Slot.parse(item) for item in raw_slots), timeout, directory,
                   tuple(sorted(environment.items())))

    @property
    def program(self) -> Path:
        return Path(os.fsdecode(self.argv[0]))

    def data(self) -> dict:
        result = {"argv": [{"slot": item} if isinstance(item, int) else os.fsdecode(item) for item in self.argv],
                  "timeout": self.timeout, "cwd": str(self.directory), "env": dict(self.environment)}
        if self.slots:
            result["slots"] = [slot.data() for slot in self.slots]
        return result

    def names_path(self, value: bytes, written: set[Path]) -> bool:
        """Whether an argument can name a written file: whole, after "=", or glued to a short option."""
        candidates = [value, value.partition(b"=")[2]]
        if value.startswith(b"-") and not value.startswith(b"--"):
            candidates.append(value[2:])
        for candidate in candidates:
            if candidate and b"\0" not in candidate:
                # A relative path is read from the command's directory.
                if (self.directory / os.fsdecode(candidate)).resolve() in written:
                    return True
        return False

    def check_reach(self, destinations: tuple[Path, ...]) -> None:
        """Refuse templates that plainly let the agent choose the code that runs."""
        written = {path.resolve() for path in destinations}
        program = self.program.resolve()
        if program in written:
            raise ContractError("a template cannot run a write destination")
        names = {self.program.name, program.name}
        if not any(EXECUTORS.fullmatch(name) for name in names):
            return
        if any(slot.chars is not None for slot in self.slots):
            raise ContractError("a program that runs its arguments cannot take a free slot")
        values = [item for item in self.argv[1:] if isinstance(item, bytes)]
        values += [choice for slot in self.slots for choice in slot.choices]
        if any(self.names_path(value, written) for value in values):
            raise ContractError("a program that runs its arguments cannot be given a write destination")

    def authorize(self, run: CommandRun, remaining: float) -> AuthorizedRun:
        """Fill the slots from the program's arguments after checking each one."""
        if len(run.args) != len(self.slots):
            raise EffectDenied("command arguments do not match the template's slots")
        if any(not slot.accepts(value) for slot, value in zip(self.slots, run.args)):
            raise EffectDenied("a command argument does not fit its slot")
        # Never start a command the run's deadline would cut short: a kill
        # partway would leave its effect unknown.
        if remaining < self.timeout:
            raise EffectDenied("too little of the run's time is left for this command")
        argv = tuple(run.args[item] if isinstance(item, int) else item for item in self.argv)
        return AuthorizedRun(argv, self.environment, self.directory, float(self.timeout))


def parse_commands(value: object, destinations: tuple[Path, ...]) -> tuple[Command, ...]:
    if not isinstance(value, list) or len(value) > MAX_COMMANDS:
        raise ContractError(f"owner may enroll at most {MAX_COMMANDS} commands")
    commands = tuple(Command.parse(item) for item in value)
    for command in commands:
        command.check_reach(destinations)
    return commands


@dataclass(frozen=True)
class Finish:
    """What one command did. started is False only if nothing was executed."""

    result: RunResult
    started: bool
    ambiguous: bool


def has_exited(pid: int) -> bool:
    """Check for exit without reaping, so the process ID stays reserved."""
    return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def has_other_members(group: int, leader: int) -> bool:
    """Whether any live process other than the leader is in a process group."""
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdigit() or int(entry.name) == leader:
                continue
            try:
                # Fields after the command name: state, parent, process group.
                state, _, member_group = Path(entry.path, "stat").read_bytes().rpartition(b")")[2].split()[:3]
            except (OSError, ValueError):
                continue
            if int(member_group) == group and state != b"Z":
                return True
    return False


class CommandRunner:
    """Run one authorized command with no shell, empty stdin and bounded output.

    The command runs in its own process group. When it ends, anything it left
    running in that group is killed and the outcome becomes ambiguous: what
    that process had done is unknown. A descendant that leaves the group
    (setsid) escapes this; the owner enrolls programs that do not daemonize.
    """

    def run(self, call: AuthorizedRun) -> Finish:
        try:
            process = subprocess.Popen(list(call.argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, env=dict(call.environment), cwd=call.directory,
                                       close_fds=True, start_new_session=True)
        except OSError:
            # Popen reports a failed exec before returning, so nothing ran.
            return Finish(RunFailure.UNSTARTED, False, False)
        output, oversized, timed_out = bytearray(), False, False
        deadline = time.monotonic() + call.timeout
        with process, selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                exited = has_exited(process.pid)
                remaining = deadline - time.monotonic()
                if not exited and remaining <= 0:
                    timed_out = True
                    break
                # After exit, read only what is already buffered: a background
                # descendant that keeps the pipe open must not hold the run.
                ready = selector.select(0 if exited else min(0.1, remaining))
                if exited and not ready:
                    break
                for key, _ in ready:
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    if len(output) + len(data) > MAX_OUTPUT_BYTES:
                        oversized = True
                    else:
                        output += data
            while not timed_out and not has_exited(process.pid):
                timed_out = time.monotonic() >= deadline
                time.sleep(0.01)
            # The unreaped leader keeps the group's ID reserved, so the kill
            # below cannot reach an unrelated process.
            lingering = not timed_out and has_other_members(process.pid, process.pid)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        if timed_out:
            # Killed partway, so what the command changed is unknown.
            return Finish(RunFailure.TIMEOUT, True, True)
        if process.returncode < 0:
            return Finish(RunFailure.SIGNALLED, True, lingering)
        if oversized:
            return Finish(RunFailure.OVERSIZED, True, lingering)
        return Finish(RunExit(process.returncode, bytes(output)), True, lingering)
