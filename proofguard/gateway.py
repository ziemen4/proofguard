"""Generic local write gateway; no application-specific flow rules.

Prototype assumptions: the host and pinned Bend interpreter are trusted, and
the agent cannot write the gateway's code, state, destinations or configuration.
This class runs outside AuthorSandbox. The owner pull worker supplies captured
source; the agent never receives this object or its authority paths.
"""

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import struct
import time
import tempfile
import uuid

from bendspec.model import decode
from .admission import AdmissionRejected, ProgramAdmission, VerifiedProgram
from .audit import Audit, durable_directory, sync_directory
from .context import ExecutionContext
from .encoding import commitment


class GatewayError(ValueError):
    pass


class ExecutionRefused(GatewayError):
    """Evaluation or a guard stopped the batch before any write began."""


class EffectUncertain(GatewayError):
    """An execution failed; some effects may already have happened."""

    def __init__(self, intent_id: str, request_id: str | None,
                 completed: tuple[tuple[int, str], ...], archive_durable: bool):
        super().__init__("execution failed; owner reconciliation may be required")
        self.request_id, self.completed = request_id, completed
        self.intent_id = intent_id
        self.archive_durable = archive_durable


@dataclass(frozen=True)
class Session:
    identifier: str
    expires: int
    context_id: str
    destinations: tuple[Path, ...]


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


@dataclass(frozen=True)
class Prepared:
    intent_id: str
    source_id: str
    check_id: str
    program: VerifiedProgram


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
                                bytes.fromhex(commitment("destinations", *(str(p).encode() for p in session.destinations))))
        return Prepared(intent_id, source_id, check_id, program)

    def check(self, source: str) -> Prepared:
        """Preview only. A later submit rechecks its own captured source."""
        return self._prepare(source)

    def submit(self, source: str, *, proposal: str | None = None) -> WriteReceipt:
        """Verify every submission, archive its attempt, then execute its IO batch."""
        prepared = self._prepare(source)
        session = self.session
        intent_id = prepared.intent_id
        request_id = None
        if time.time() >= session.expires:
            raise GatewayError("session expired during checking")
        completed = []
        dispatching = False
        try:
            self.audit.store({"schema": 1, "kind": "execution_started", "session": session.identifier,
                              "intent_id": intent_id, "check_id": prepared.check_id, "proposal": proposal})
            if time.time() >= session.expires:
                raise GatewayError("session expired before application execution")
            writes = self.admission.execute_program(prepared.program, context=self.context)
            for write in writes:
                if write.capability >= len(session.destinations):
                    raise GatewayError("write destination is not enrolled")
            action_fields = tuple(struct.pack(">Q", w.capability) + bytes.fromhex(
                commitment("payload", w.data)) for w in writes)
            request_id = commitment("request-v1", bytes.fromhex(intent_id),
                                    bytes.fromhex(commitment("actions", *action_fields)))
            self.audit.store({"schema": 1, "kind": "prepared_effects", "session": session.identifier,
                              "intent_id": intent_id, "request_id": request_id, "check_id": prepared.check_id,
                              "actions": [(w.capability, commitment("payload", w.data)) for w in writes]})
            # Both intent and concrete actions are archived BEFORE external IO.
            for write in writes:
                if time.time() >= session.expires:
                    raise GatewayError("session expired before effect")
                destination = session.destinations[write.capability]
                # Each replacement publishes one complete payload. Concurrent
                # authorized submissions have last-writer-wins semantics, not CAS.
                fd, temporary = tempfile.mkstemp(prefix=".proofguard-", dir=destination.parent)
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(write.data)
                        output.flush()
                        os.fsync(output.fileno())
                    # The rename is the first change to a destination; a failure
                    # while preparing the temporary file leaves every file unchanged.
                    dispatching = True
                    os.replace(temporary, destination)
                    sync_directory(destination.parent)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                completed.append((write.capability, commitment("payload", write.data)))
            receipt = WriteReceipt(request_id, session.identifier, self.admission.contract.identity,
                                   prepared.source_id, session.context_id, tuple(completed), prepared.check_id, intent_id)
            self.audit.store({"schema": 1, "kind": "completed", "receipt": asdict(receipt)})
            return receipt
        except (OSError, GatewayError, ValueError) as exc:
            archive_durable = True
            try:
                self.audit.store({"schema": 1, "kind": "uncertain" if dispatching else "refused", "request_id": request_id,
                                  "intent_id": intent_id, "session": session.identifier, "check_id": prepared.check_id,
                                  "proposal": proposal, "completed": completed})
            except (OSError, ValueError):
                archive_durable = False
            if not dispatching:
                raise ExecutionRefused("execution stopped before any write") from exc
            raise EffectUncertain(intent_id, request_id, tuple(completed), archive_durable) from exc
