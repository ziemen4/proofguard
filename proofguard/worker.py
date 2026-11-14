"""Owner-side pull worker for untrusted Bend proposal files.

The agent cannot select a gateway, session, destination, backend or operation.
Durable request claims prevent retries after either a crash or a completed run.
"""

from dataclasses import asdict, dataclass
from enum import Enum
from itertools import islice
import os
from pathlib import Path
import re
import stat
import time
import uuid

from bendspec.model import ContractError, canonical, text
from .admission import AdmissionRejected
from .audit import durable_directory, overlaps, publish, sync_directory
from .encoding import commitment
from .gateway import EffectUncertain, ExecutionRefused, ExecutionStopped, Gateway, GatewayError

MAX_PROGRAM = 16384
MAX_OUTBOX_ENTRIES = 128
PROPOSAL_ID = re.compile(r"[0-9a-f]{32}")


class Status(str, Enum):
    COMPLETED = "completed"
    REJECTED = "rejected"
    STOPPED = "stopped"
    UNCERTAIN = "uncertain"


class OutboxCapacityError(ContractError):
    """Owner must clean or rotate an overfull outbox before restarting."""


@dataclass(frozen=True)
class ProposalResult:
    schema: int
    proposal: str
    status: Status
    reason: str | None = None
    source_id: str | None = None
    check_id: str | None = None
    diagnostics: str | None = None
    archive_durable: bool | None = None

    def data(self) -> dict:
        return asdict(self)


def capture_program(root: Path, proposal: str) -> str:
    """Snapshot bounded regular source, rejecting links and concurrent mutation."""
    if not isinstance(proposal, str) or not PROPOSAL_ID.fullmatch(proposal):
        raise ContractError("invalid proposal identifier")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(proposal + ".bend", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as incoming:
            before = os.fstat(incoming.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_PROGRAM:
                raise ContractError("proposal must be a bounded unlinked regular file")
            data = incoming.read(MAX_PROGRAM + 1)
            after = os.fstat(incoming.fileno())
            stamps = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns, row.st_nlink)
            if len(data) > MAX_PROGRAM or stamps(before) != stamps(after):
                raise ContractError("proposal changed during capture")
            return text(data.decode("utf-8"), "Bend program")
    except (OSError, UnicodeError) as exc:
        raise ContractError("cannot capture regular UTF-8 proposal") from exc
    finally:
        os.close(directory)


def write_proposal(outbox: Path, source: str) -> str:
    """Atomically publish source under a random ID, without executing it."""
    source = text(source, "Bend program")
    if len(source.encode("utf-8")) > MAX_PROGRAM:
        raise ContractError("program exceeds outbox limit")
    proposal = uuid.uuid4().hex
    temporary = outbox / ("." + proposal + ".upload")
    try:
        with temporary.open("xb") as output:
            output.write(source.encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
        os.rename(temporary, outbox / (proposal + ".bend"))
        sync_directory(outbox)
    finally:
        temporary.unlink(missing_ok=True)
    return proposal


class OutboxWorker:
    def __init__(self, gateway: Gateway, outbox: Path, results: Path):
        """Select disjoint owner paths before an agent receives writable mounts."""
        self.gateway = gateway
        for path in (outbox, results):
            if not path.is_absolute():
                raise ContractError("outbox and results must be absolute owner-selected paths")
        self.outbox, self.results = outbox.resolve(), results.resolve()
        code = Path(__file__).resolve().parents[1]
        protected = (gateway.authority, code, *gateway.session.get_protected_paths(), *gateway.session.get_programs(),
                     gateway.admission.toolchain.source.resolve(),
                     gateway.admission.toolchain.kernel.resolve())
        if overlaps(self.outbox, self.results) or any(overlaps(self.outbox, path) for path in protected):
            raise ContractError("agent outbox overlaps protected owner resources")
        if any(overlaps(self.results, path) for path in protected):
            raise ContractError("result directory overlaps protected owner resources")
        for path in (outbox, results):
            durable_directory(path)
        self.claims = gateway.state / "proposals"
        durable_directory(self.claims)

    def _claim(self, proposal: str) -> bool:
        """Reserve one request durably; an existing claim is never re-executed."""
        try:
            fd = os.open(self.claims / (proposal + ".claim"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as record:
            # The outbox ties the claim to one authoring session, since sessions
            # that share an enrollment identifier share this claims directory.
            record.write(canonical({"schema": 1, "proposal": proposal, "outbox": str(self.outbox)}))
            record.flush()
            os.fsync(record.fileno())
        sync_directory(self.claims)
        return True

    def process(self, proposal: str) -> ProposalResult | None:
        """Capture and execute one fresh proposal through the mandatory gate."""
        if not isinstance(proposal, str) or not PROPOSAL_ID.fullmatch(proposal):
            raise ContractError("invalid proposal identifier")
        if not self._claim(proposal):
            return None
        source_id = None
        try:
            source = capture_program(self.outbox, proposal)
            source_id = commitment("source", source.encode("utf-8"))
            self.gateway.audit.store({"schema": 1, "kind": "outbox_capture", "proposal": proposal,
                                      "source_id": source_id, "source": source})
            try:
                receipt = self.gateway.submit(source, proposal=proposal)
                result = ProposalResult(1, proposal, Status.COMPLETED,
                                        source_id=receipt.source_id, check_id=receipt.check_id)
            except AdmissionRejected as exc:
                result = ProposalResult(1, proposal, Status.REJECTED, "mandatory_obligation_failed",
                                        source_id=source_id,
                                        diagnostics=exc.feedback or "Program rejected by gateway verification.")
            except ExecutionRefused:
                result = ProposalResult(1, proposal, Status.REJECTED, "execution_refused", source_id=source_id)
            except ExecutionStopped:
                result = ProposalResult(1, proposal, Status.STOPPED, "stopped_after_effects", source_id=source_id)
            except EffectUncertain as exc:
                result = ProposalResult(1, proposal, Status.UNCERTAIN, "owner_reconciliation_required",
                                        source_id=source_id, archive_durable=exc.archive_durable)
            except (ContractError, GatewayError):
                result = ProposalResult(1, proposal, Status.REJECTED,
                                        "session_unavailable", source_id=source_id)
        except ContractError:
            result = ProposalResult(1, proposal, Status.REJECTED, "invalid_program_source", source_id=source_id)
        publish(self.results / (proposal + ".json"), canonical(result.data()))
        return result

    def poll(self) -> tuple[ProposalResult, ...]:
        """Reject overflow before claiming anything, rather than hiding proposals."""
        with os.scandir(self.outbox) as entries:
            batch = list(islice(entries, MAX_OUTBOX_ENTRIES + 1))
        if len(batch) > MAX_OUTBOX_ENTRIES:
            raise OutboxCapacityError("outbox capacity exceeded; owner cleanup or rotation required")
        selected = sorted(entry.name[:-5] for entry in batch
                          if entry.name.endswith(".bend") and PROPOSAL_ID.fullmatch(entry.name[:-5]))
        return tuple(result for proposal in selected if (result := self.process(proposal)) is not None)

    def watch(self, interval: float = 0.1):
        """Pull proposals until the owner-selected session expires."""
        while time.time() < self.gateway.session.expires:
            yield from self.poll()
            time.sleep(interval)
