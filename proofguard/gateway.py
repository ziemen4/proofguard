"""Generic effect gateway for local writes and HTTP; no application-specific flow rules.

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
from .effects import EffectDenied, Exchange, HttpRequest, HttpResponse, ProgramAborted, Step, Write
from .encoding import commitment
from .http import MAX_CALLS, AuthorizedCall, Delivery, Endpoint, HttpTransport

MAX_RUN_WRITES = 16
STEP_SECONDS = 60
RUN_SECONDS = 300


class GatewayError(ValueError):
    pass


class ExecutionRefused(GatewayError):
    """Evaluation or a guard stopped the run before any effect began."""


class ExecutionStopped(GatewayError):
    """The run stopped after effects; each of them has a known outcome and stands."""

    def __init__(self, intent_id: str, request_id: str | None, completed: tuple[tuple[int, str], ...],
                 calls: tuple[tuple[int, str, str], ...]):
        super().__init__("execution stopped after completed effects")
        self.intent_id, self.request_id = intent_id, request_id
        self.completed, self.calls = completed, calls


class EffectUncertain(GatewayError):
    """An execution failed; some effects may already have happened."""

    def __init__(self, intent_id: str, request_id: str | None,
                 completed: tuple[tuple[int, str], ...], archive_durable: bool,
                 calls: tuple[tuple[int, str, str], ...] = ()):
        super().__init__("execution failed; owner reconciliation may be required")
        self.request_id, self.completed = request_id, completed
        self.intent_id = intent_id
        self.archive_durable = archive_durable
        self.calls = calls


@dataclass(frozen=True)
class Session:
    identifier: str
    expires: int
    context_id: str
    destinations: tuple[Path, ...]
    endpoints: tuple[Endpoint, ...] = ()

    def get_protected_paths(self) -> tuple[Path, ...]:
        """List host paths an agent-writable mount must never reach."""
        return (*self.destinations,
                *(endpoint.credential.file for endpoint in self.endpoints if endpoint.credential is not None))


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


@dataclass(frozen=True)
class Prepared:
    intent_id: str
    source_id: str
    check_id: str
    program: VerifiedProgram


@dataclass
class Run:
    """The effects of one execution in dispatch order.

    history is the only copy of the responses. Each later step replays it, so a
    call that already happened is never sent a second time.
    """

    request_id: str | None = None
    history: list[Exchange] = field(default_factory=list)
    completed: list[tuple[int, str]] = field(default_factory=list)
    calls: list[tuple[int, str, str]] = field(default_factory=list)
    writes: int = 0
    ambiguous: bool = False
    dispatched: bool = False


def commit_request(request: HttpRequest) -> str:
    """Bind everything the program sent; owner credentials are never part of it."""
    headers = (part for pair in request.headers for part in pair)
    return commitment("http-request", struct.pack(">Q", request.endpoint), request.method.value.encode(),
                      request.url, bytes.fromhex(commitment("http-headers", *headers)), request.body)


def get_outcome(delivery: Delivery) -> str:
    """Name a finished call for audit: its status code or its failure."""
    return str(delivery.result.status) if isinstance(delivery.result, HttpResponse) else delivery.result.value


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
                                *(canonical(endpoint.data()) for endpoint in session.endpoints))
        return Prepared(intent_id, source_id, check_id, program)

    def check(self, source: str) -> Prepared:
        """Preview only. A later submit rechecks its own captured source."""
        return self._prepare(source)

    def _authorize_step(self, writes: tuple[Write, ...], step: Step | None, run: Run) -> AuthorizedCall | None:
        """Check one whole step, so a denied effect stops it before its first write."""
        session = self.session
        for write in writes:
            if write.capability >= len(session.destinations):
                raise EffectDenied("write destination is not enrolled")
        if run.writes + len(writes) > MAX_RUN_WRITES:
            raise EffectDenied("execution exceeds its write limit")
        if step is None:
            return None
        if len(run.history) >= MAX_CALLS:
            raise EffectDenied("execution exceeds its call limit")
        if step.request.endpoint >= len(session.endpoints):
            raise EffectDenied("HTTP endpoint is not enrolled")
        return session.endpoints[step.request.endpoint].authorize(step.request)

    def _archive_step(self, writes: tuple[Write, ...], step: Step | None, run: Run, prepared: Prepared) -> None:
        """Archive the step's intent and concrete actions BEFORE external IO."""
        action_fields = tuple(struct.pack(">Q", w.capability) + bytes.fromhex(
            commitment("payload", w.data)) for w in writes)
        record = {"schema": 1, "kind": "prepared_effects", "session": self.session.identifier,
                  "intent_id": prepared.intent_id, "check_id": prepared.check_id,
                  "actions": [(w.capability, commitment("payload", w.data)) for w in writes]}
        if step is not None:
            request = commit_request(step.request)
            action_fields += (b"http" + bytes.fromhex(request),)
            record["call"] = {"sequence": len(run.history), "endpoint": step.request.endpoint,
                              "method": step.request.method.value, "request": request}
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
            run.dispatched = True
            os.replace(temporary, destination)
            sync_directory(destination.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        run.completed.append((write.capability, commitment("payload", write.data)))

    def _dispatch_call(self, step: Step, call: AuthorizedCall, run: Run, prepared: Prepared) -> None:
        """Send one call under a durable record; its response stays in this run."""
        if time.time() >= self.session.expires:
            raise GatewayError("session expired before effect")
        sequence = len(run.history)
        request = commit_request(step.request)
        # A crash leaves this record without a finish: delivery is then unknown,
        # and the worker's durable claim prevents any repeat of the proposal.
        self.audit.store({"schema": 1, "kind": "http_started", "session": self.session.identifier,
                          "intent_id": prepared.intent_id, "request_id": run.request_id, "sequence": sequence,
                          "endpoint": step.request.endpoint, "request": request})
        wrote = run.dispatched
        run.dispatched = True
        delivery = self.transport.send(call)
        response = delivery.result if isinstance(delivery.result, HttpResponse) else None
        self.audit.store({"schema": 1, "kind": "http_finished", "session": self.session.identifier,
                          "intent_id": prepared.intent_id, "request_id": run.request_id, "sequence": sequence,
                          "endpoint": step.request.endpoint, "request": request, "outcome": get_outcome(delivery),
                          "sent": delivery.sent, "ambiguous": delivery.ambiguous, "blocked": delivery.blocked,
                          "response": None if response is None else commitment("http-response", response.body),
                          "response_bytes": None if response is None else len(response.body)})
        run.history.append(Exchange(step, delivery.result))
        run.calls.append((step.request.endpoint, request, get_outcome(delivery)))
        run.ambiguous = run.ambiguous or delivery.ambiguous
        # A call that never left the host is no effect to reconcile.
        run.dispatched = wrote or delivery.sent

    def _execute(self, prepared: Prepared, run: Run) -> None:
        """Evaluate until an effect, dispatch it, and resume until the program ends.

        Each step's writes dispatch before its call, in program order. A program
        without PGHttp is a single step, so it dispatches nothing until all of
        it has been evaluated and every guard has accepted.
        """
        deadline = time.monotonic() + RUN_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GatewayError("execution deadline exceeded")
            result = self.admission.execute_program(prepared.program, min(STEP_SECONDS, remaining),
                                                    context=self.context, history=tuple(run.history))
            step = result if isinstance(result, Step) else None
            writes = result if step is None else step.writes
            call = self._authorize_step(writes, step, run)
            self._archive_step(writes, step, run, prepared)
            for write in writes:
                self._replace_file(write, run)
            run.writes += len(writes)
            if step is None:
                return
            self._dispatch_call(step, call, run, prepared)

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
            if run.ambiguous:
                raise GatewayError("a sent call may have reached its server")
            receipt = WriteReceipt(run.request_id, session.identifier, self.admission.contract.identity,
                                   prepared.source_id, session.context_id, tuple(run.completed), prepared.check_id,
                                   intent_id, tuple(run.calls))
            self.audit.store({"schema": 1, "kind": "completed", "receipt": asdict(receipt)})
            return receipt
        except (OSError, GatewayError, ValueError) as exc:
            # A program or policy stop leaves every effect's outcome known. Any
            # other failure after dispatch began leaves at least one unknown.
            stopped = run.dispatched and not run.ambiguous and isinstance(exc, (ProgramAborted, EffectDenied))
            archive_durable = True
            try:
                self.audit.store({"schema": 1, "request_id": run.request_id, "intent_id": intent_id,
                                  "kind": "stopped" if stopped else "uncertain" if run.dispatched else "refused",
                                  "session": session.identifier, "check_id": prepared.check_id,
                                  "proposal": proposal, "completed": run.completed, "calls": run.calls})
            except (OSError, ValueError):
                archive_durable = False
            if not run.dispatched:
                raise ExecutionRefused("execution stopped before any effect") from exc
            if stopped and archive_durable:
                raise ExecutionStopped(intent_id, run.request_id, tuple(run.completed), tuple(run.calls)) from exc
            raise EffectUncertain(intent_id, run.request_id, tuple(run.completed), archive_durable,
                                  tuple(run.calls)) from exc
