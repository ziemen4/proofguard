"""Generic effect gateway for local writes, HTTP and commands; no application-specific flow rules.

Prototype assumptions: the host and pinned Bend interpreter are trusted, and
the agent cannot write the gateway's code, state, destinations or configuration.
This class runs outside AuthorSandbox. The owner pull worker supplies captured
source; the agent never receives this object or its authority paths.
"""

from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
import struct
import time
import tempfile
import uuid

from bendspec.model import canonical, decode
from .admission import AdmissionRejected, ProgramAdmission, VerifiedProgram
from .audit import Audit, durable_directory, sync_directory
from .context import ExecutionContext
from .commands import AuthorizedRun, Command, CommandRunner, Finish
from .effects import CommandRun, EffectDenied, Exchange, HttpRequest, HttpResponse, RunExit, Step, Write
from .encoding import commitment
from .http import MAX_CALLS, AuthorizedCall, Delivery, Endpoint, HttpTransport

MAX_RUN_WRITES = 16
STEP_SECONDS = 60
RUN_SECONDS = 300


class GatewayError(ValueError):
    pass


class ExecutionRefused(GatewayError):
    """Evaluation or a guard stopped the run before any effect began."""


# Where an effect sits in a run: (step, effect), both from 1. A step's effects
# are its writes in program order, then its call or command.
Position = tuple[int, int]


class ExecutionStopped(GatewayError):
    """The run stopped after effects; each of them has a known outcome and stands.

    stopped_at is the first effect that was not performed. Owner-side only: the
    agent's result does not say where a run stopped.
    """

    def __init__(self, intent_id: str, request_id: str | None, completed: tuple[tuple[int, str], ...],
                 calls: tuple[tuple[int, str, str], ...], stopped_at: Position,
                 commands: tuple[tuple[int, str, str], ...] = ()):
        super().__init__("execution stopped after completed effects")
        self.intent_id, self.request_id = intent_id, request_id
        self.completed, self.calls, self.commands = completed, calls, commands
        self.stopped_at = stopped_at


class EffectUncertain(GatewayError):
    """An execution failed; some effects may already have happened.

    in_doubt lists the effects whose outcome is unknown. It is empty when only
    the archive of a known outcome failed. Owner-side only, like stopped_at.
    """

    def __init__(self, intent_id: str, request_id: str | None,
                 completed: tuple[tuple[int, str], ...], archive_durable: bool,
                 calls: tuple[tuple[int, str, str], ...] = (), in_doubt: tuple[Position, ...] = (),
                 commands: tuple[tuple[int, str, str], ...] = ()):
        super().__init__("execution failed; owner reconciliation may be required")
        self.request_id, self.completed = request_id, completed
        self.intent_id = intent_id
        self.archive_durable = archive_durable
        self.calls, self.commands = calls, commands
        self.in_doubt = in_doubt


@dataclass(frozen=True)
class Session:
    identifier: str
    expires: int
    context_id: str
    destinations: tuple[Path, ...]
    endpoints: tuple[Endpoint, ...] = ()
    commands: tuple[Command, ...] = ()

    def get_protected_paths(self) -> tuple[Path, ...]:
        """List host paths the agent must neither see nor write."""
        return (*self.destinations,
                *(endpoint.credential.file for endpoint in self.endpoints if endpoint.credential is not None))

    def get_programs(self) -> tuple[Path, ...]:
        """List the enrolled programs: the agent may see them but never write them."""
        return tuple(path for command in self.commands for path in (command.program, command.program.resolve()))


@dataclass(frozen=True)
class WriteReceipt:
    request_id: str
    session: str
    contract_id: str
    source_id: str
    context_id: str
    writes: tuple[tuple[int, str], ...]
    check_id: str
    intent_id: str
    calls: tuple[tuple[int, str, str], ...] = ()
    commands: tuple[tuple[int, str, str], ...] = ()


@dataclass(frozen=True)
class Prepared:
    intent_id: str
    source_id: str
    check_id: str
    program: VerifiedProgram


@dataclass
class Run:
    """The effects of one execution in dispatch order.

    history is the only copy of the results. Each later step replays it, so a
    call or command that already happened is never performed a second time.
    """

    request_id: str | None = None
    history: list[Exchange] = field(default_factory=list)
    completed: list[tuple[int, str]] = field(default_factory=list)
    calls: list[tuple[int, str, str]] = field(default_factory=list)
    commands: list[tuple[int, str, str]] = field(default_factory=list)
    writes: int = 0
    dispatched: bool = False
    # True from the moment an effect may have begun until its outcome is
    # recorded; a failure in that window leaves that effect unknown.
    in_flight: bool = False
    # The step being run and how many of its effects are done, from which the
    # position of the next effect follows.
    step: int = 0
    done: int = 0
    # Effects that happened with an unknown outcome, such as a timed-out call.
    in_doubt: list[Position] = field(default_factory=list)

    @property
    def next_effect(self) -> Position:
        return (self.step, self.done + 1)

    def finish_effect(self, ambiguous: bool = False) -> None:
        if ambiguous:
            self.in_doubt.append(self.next_effect)
        self.done += 1
        self.in_flight = False


def commit_request(request: HttpRequest) -> str:
    """Bind everything the program sent; owner credentials are never part of it."""
    headers = (part for pair in request.headers for part in pair)
    return commitment("http-request", struct.pack(">Q", request.endpoint), request.method.value.encode(),
                      request.url, bytes.fromhex(commitment("http-headers", *headers)), request.body)


def commit_run(run: CommandRun) -> str:
    """Bind the template index and every argument the program chose."""
    return commitment("command-run", struct.pack(">Q", run.command), *run.args)


def get_outcome(delivery: Delivery) -> str:
    """Name a finished call for audit: its status code or its failure."""
    return str(delivery.result.status) if isinstance(delivery.result, HttpResponse) else delivery.result.value


def get_exit(finish: Finish) -> str:
    """Name a finished command for audit: its exit status or its failure."""
    return f"exit {finish.result.code}" if isinstance(finish.result, RunExit) else finish.result.value


class Gateway:
    def __init__(self, admission: ProgramAdmission, session: Session,
                 context: ExecutionContext, state: Path, audit: Audit, *, authority: Path):
        """Check owner context and establish durable state, audit and output paths."""
        if type(admission) is not ProgramAdmission:
            raise GatewayError("gateway requires the complete plugin admission pipeline")
        if not session.identifier or len(session.identifier.encode()) > 1024:
            raise GatewayError("invalid owner session")
        self.admission, self.session = admission, session
        self.context = context
        self.audit = audit
        self.transport = HttpTransport()
        self.runner = CommandRunner()
        self.authority = authority.resolve(strict=True)
        if not isinstance(context, ExecutionContext) or context.identity != session.context_id:
            raise GatewayError("owner input snapshot does not match enrolled context")
        self.toolchain_id = admission.toolchain.validate()
        self.state = state.resolve(strict=True)
        if not self.state.is_dir():
            raise GatewayError("owner state directory required")
        durable_directory(self.state)
        durable_directory(self.audit.root)
        for destination in session.destinations:
            if not destination.is_absolute() or destination.is_symlink():
                raise GatewayError("owner destinations must be absolute, non-symlink paths")
            destination.parent.resolve(strict=True)
            durable_directory(destination.parent)
        normalized = tuple(path.parent.resolve(strict=True) / path.name for path in session.destinations)
        if len(set(normalized)) != len(normalized):
            raise GatewayError("write destinations must identify distinct resources")

    def _prepare(self, source: str) -> Prepared:
        """Verify and archive the captured source without application execution."""
        session = self.session
        if time.time() >= session.expires:
            raise GatewayError("session expired")
        try:
            program = self.admission.verify_program(source)
        except AdmissionRejected as exc:
            self.audit.store({"schema": 1, "kind": "rejected_check", "session": session.identifier,
                              "receipt": exc.receipt})
            raise
        receipt = decode(program.receipt_bytes)
        if receipt["toolchain"] != self.toolchain_id:
            raise GatewayError("checker changed after enrollment")
        source_id = commitment("source", source.encode())
        check_id = self.audit.store({"schema": 1, "kind": "accepted_check", "session": session.identifier,
                                    "attempt": uuid.uuid4().hex,
                                    "receipt": receipt})
        intent_id = commitment("intent-v1", session.identifier.encode(),
                                struct.pack(">Q", session.expires),
                                session.context_id.encode(), self.admission.contract.identity.encode(),
                                source_id.encode(), self.admission.entrypoint.encode(), check_id.encode(),
                                bytes.fromhex(commitment("destinations", *(str(p).encode() for p in session.destinations))),
                                *(canonical(endpoint.data()) for endpoint in session.endpoints),
                                # Absent without commands, so earlier intents keep their identity.
                                *((bytes.fromhex(commitment("commands", *(canonical(command.data())
                                                                          for command in session.commands))),)
                                  if session.commands else ()))
        return Prepared(intent_id, source_id, check_id, program)

    def check(self, source: str) -> Prepared:
        """Preview only. A later submit rechecks its own captured source."""
        return self._prepare(source)

    def _authorize_step(self, writes: tuple[Write, ...], step: Step | None, run: Run,
                        remaining: float) -> AuthorizedCall | AuthorizedRun | None:
        """Check one whole step, so a denied effect stops it before its first write."""
        session = self.session
        for write in writes:
            if write.capability >= len(session.destinations):
                raise EffectDenied("write destination is not enrolled")
        if run.writes + len(writes) > MAX_RUN_WRITES:
            raise EffectDenied("execution exceeds its write limit")
        if step is None:
            return None
        # Calls and commands share one limit: each one adds a replay of the program.
        if len(run.history) >= MAX_CALLS:
            raise EffectDenied("execution exceeds its call limit")
        if isinstance(step.action, CommandRun):
            if step.action.command >= len(session.commands):
                raise EffectDenied("command is not enrolled")
            return session.commands[step.action.command].authorize(step.action, remaining)
        if step.action.endpoint >= len(session.endpoints):
            raise EffectDenied("HTTP endpoint is not enrolled")
        return session.endpoints[step.action.endpoint].authorize(step.action)

    def _archive_step(self, writes: tuple[Write, ...], step: Step | None, run: Run, prepared: Prepared) -> None:
        """Archive the step's intent and concrete actions BEFORE external IO."""
        action_fields = tuple(struct.pack(">Q", w.capability) + bytes.fromhex(
            commitment("payload", w.data)) for w in writes)
        record = {"schema": 1, "kind": "prepared_effects", "session": self.session.identifier,
                  "intent_id": prepared.intent_id, "check_id": prepared.check_id,
                  "actions": [(w.capability, commitment("payload", w.data)) for w in writes]}
        if step is not None and isinstance(step.action, CommandRun):
            command = commit_run(step.action)
            action_fields += (b"run" + bytes.fromhex(command),)
            record["run"] = {"sequence": len(run.history), "command": step.action.command, "arguments": command}
        elif step is not None:
            request = commit_request(step.action)
            action_fields += (b"http" + bytes.fromhex(request),)
            record["call"] = {"sequence": len(run.history), "endpoint": step.action.endpoint,
                              "method": step.action.method.value, "request": request}
        actions = bytes.fromhex(commitment("actions", *action_fields))
        # A run without calls has one step, whose identity covers its whole batch.
        run.request_id = (commitment("request-v1", bytes.fromhex(prepared.intent_id), actions)
                          if run.request_id is None else
                          commitment("request-step-v1", bytes.fromhex(run.request_id), actions))
        self.audit.store({**record, "request_id": run.request_id})

    def _replace_file(self, write: Write, run: Run) -> None:
        """Publish one complete payload through an atomic replacement."""
        if time.time() >= self.session.expires:
            raise GatewayError("session expired before effect")
        destination = self.session.destinations[write.capability]
        # Each replacement publishes one complete payload. Concurrent
        # authorized submissions have last-writer-wins semantics, not CAS.
        fd, temporary = tempfile.mkstemp(prefix=".proofguard-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(write.data)
                output.flush()
                os.fsync(output.fileno())
            # The rename is the first change to a destination; a failure while
            # preparing the temporary file leaves every file unchanged.
            run.dispatched = run.in_flight = True
            os.replace(temporary, destination)
            sync_directory(destination.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        run.completed.append((write.capability, commitment("payload", write.data)))
        run.finish_effect()

    def _dispatch_call(self, step: Step, call: AuthorizedCall, run: Run, prepared: Prepared) -> None:
        """Send one call under a durable record; its response stays in this run."""
        if time.time() >= self.session.expires:
            raise GatewayError("session expired before effect")
        sequence = len(run.history)
        request = commit_request(step.action)
        # A crash leaves this record without a finish: delivery is then unknown,
        # and the worker's durable claim prevents any repeat of the proposal.
        self.audit.store({"schema": 1, "kind": "http_started", "session": self.session.identifier,
                          "intent_id": prepared.intent_id, "request_id": run.request_id, "sequence": sequence,
                          "endpoint": step.action.endpoint, "request": request})
        wrote = run.dispatched
        run.dispatched = run.in_flight = True
        delivery = self.transport.send(call)
        response = delivery.result if isinstance(delivery.result, HttpResponse) else None
        self.audit.store({"schema": 1, "kind": "http_finished", "session": self.session.identifier,
                          "intent_id": prepared.intent_id, "request_id": run.request_id, "sequence": sequence,
                          "endpoint": step.action.endpoint, "request": request, "outcome": get_outcome(delivery),
                          "sent": delivery.sent, "ambiguous": delivery.ambiguous, "blocked": delivery.blocked,
                          "response": None if response is None else commitment("http-response", response.body),
                          "response_bytes": None if response is None else len(response.body)})
        run.history.append(Exchange(step, delivery.result))
        run.calls.append((step.action.endpoint, request, get_outcome(delivery)))
        # A call that never left the host is no effect to reconcile.
        run.dispatched = wrote or delivery.sent
        run.finish_effect(delivery.ambiguous)

    def _dispatch_run(self, step: Step, command: AuthorizedRun, run: Run, prepared: Prepared) -> None:
        """Run one command under a durable record; its output stays in this run."""
        if time.time() >= self.session.expires:
            raise GatewayError("session expired before effect")
        sequence = len(run.history)
        arguments = commit_run(step.action)
        record = {"schema": 1, "session": self.session.identifier, "intent_id": prepared.intent_id,
                  "request_id": run.request_id, "sequence": sequence, "command": step.action.command,
                  "arguments": arguments}
        # As with a call, a crash leaves this record without a finish, and the
        # worker's durable claim prevents any repeat of the proposal.
        self.audit.store({**record, "kind": "run_started"})
        wrote = run.dispatched
        run.dispatched = run.in_flight = True
        finish = self.runner.run(command)
        output = finish.result.output if isinstance(finish.result, RunExit) else None
        self.audit.store({**record, "kind": "run_finished", "outcome": get_exit(finish), "started": finish.started,
                          "ambiguous": finish.ambiguous,
                          "output": None if output is None else commitment("command-output", output),
                          "output_bytes": None if output is None else len(output)})
        run.history.append(Exchange(step, finish.result))
        run.commands.append((step.action.command, arguments, get_exit(finish)))
        # A command that never started is no effect to reconcile.
        run.dispatched = wrote or finish.started
        run.finish_effect(finish.ambiguous)

    def _execute(self, prepared: Prepared, run: Run) -> None:
        """Evaluate until an effect, dispatch it, and resume until the program ends.

        Each step's writes dispatch before its call or command, in program
        order. A program without PGHttp or PGRun is a single step, so it
        dispatches nothing until all of it has been evaluated and every guard
        has accepted.
        """
        deadline = time.monotonic() + RUN_SECONDS
        while True:
            run.step, run.done = run.step + 1, 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GatewayError("execution deadline exceeded")
            result = self.admission.execute_program(prepared.program, min(STEP_SECONDS, remaining),
                                                    context=self.context, history=tuple(run.history))
            step = result if isinstance(result, Step) else None
            writes = result if step is None else step.writes
            action = self._authorize_step(writes, step, run, deadline - time.monotonic())
            self._archive_step(writes, step, run, prepared)
            for write in writes:
                self._replace_file(write, run)
            run.writes += len(writes)
            if step is None:
                return
            if isinstance(action, AuthorizedRun):
                self._dispatch_run(step, action, run, prepared)
            else:
                self._dispatch_call(step, action, run, prepared)

    def submit(self, source: str, *, proposal: str | None = None) -> WriteReceipt:
        """Verify every submission, archive its attempt, then execute its effects."""
        prepared = self._prepare(source)
        session = self.session
        intent_id = prepared.intent_id
        if time.time() >= session.expires:
            raise GatewayError("session expired during checking")
        run = Run()
        try:
            self.audit.store({"schema": 1, "kind": "execution_started", "session": session.identifier,
                              "intent_id": intent_id, "check_id": prepared.check_id, "proposal": proposal})
            if time.time() >= session.expires:
                raise GatewayError("session expired before application execution")
            self._execute(prepared, run)
            if run.in_doubt:
                raise GatewayError("a sent call or interrupted command may have had effects")
            receipt = WriteReceipt(run.request_id, session.identifier, self.admission.contract.identity,
                                   prepared.source_id, session.context_id, tuple(run.completed), prepared.check_id,
                                   intent_id, tuple(run.calls), tuple(run.commands))
            self.audit.store({"schema": 1, "kind": "completed", "receipt": asdict(receipt)})
            return receipt
        except (OSError, GatewayError, ValueError) as exc:
            # A failure between effects leaves every outcome known: the run
            # stopped. One while an effect was in flight leaves that one unknown.
            stopped = run.dispatched and not run.in_doubt and not run.in_flight
            in_doubt = (*run.in_doubt, *([run.next_effect] if run.in_flight else []))
            position = {"stopped_at": list(run.next_effect)} if stopped else {
                "in_doubt": [list(item) for item in in_doubt]} if run.dispatched else {}
            archive_durable = True
            try:
                self.audit.store({"schema": 1, "request_id": run.request_id, "intent_id": intent_id,
                                  "kind": "stopped" if stopped else "uncertain" if run.dispatched else "refused",
                                  "session": session.identifier, "check_id": prepared.check_id,
                                  "proposal": proposal, "completed": run.completed, "calls": run.calls,
                                  "commands": run.commands, **position})
            except (OSError, ValueError):
                archive_durable = False
            if not run.dispatched:
                raise ExecutionRefused("execution stopped before any effect") from exc
            if stopped and archive_durable:
                raise ExecutionStopped(intent_id, run.request_id, tuple(run.completed), tuple(run.calls),
                                       run.next_effect, tuple(run.commands)) from exc
            raise EffectUncertain(intent_id, run.request_id, tuple(run.completed), archive_durable,
                                  tuple(run.calls), in_doubt, tuple(run.commands)) from exc
