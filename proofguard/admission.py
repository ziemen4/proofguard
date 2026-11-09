"""Reuse bend-spec's provisioned source checker and BendTT kernel.

This module is loaded by the trusted gateway, not installed as an agent tool.
The operator must install bend-spec on PYTHONPATH. Candidate source cannot select
the backend, entry point, contract or toolchain. There is no ZK/TEE claim here.
"""

from dataclasses import dataclass, field
import base64
import hashlib
from pathlib import Path
import re
import tempfile
import time

from bendspec.backend import ROOT, PluginCheck, Toolchain, execute, verify
from bendspec.model import Bundle, ContractError, Source, canonical, decode, fields, names
from .context import ExecutionContext, PrivateData, encode_owner_data
from .effects import Write, decode_writes


ISOLATED_NAME = re.compile(r"\bp[0-9a-f]{12}_")
# The law fixes main's type, so Bend rejects a filling that restates it.
ANNOTATED_MAIN = re.compile(r"^def main\([ \t]*(\w+)[ \t]*:[ \t]*PGInputs[ \t]*\)(?:[ \t]*->[ \t]*PGIO)?[ \t]*:", re.M)


class AdmissionRejected(ContractError):
    """A failed obligation, retaining source-bound diagnostics for owner audit."""

    def __init__(self, receipt: dict, feedback: str | None = None):
        super().__init__("mandatory Bend source/kernel obligations rejected")
        self.receipt = receipt
        self.feedback = feedback


@dataclass(frozen=True)
class VerifiedProgram:
    """Process-local execution capability issued only after owner-law acceptance."""

    source: str
    receipt_bytes: bytes
    issuer: object = field(repr=False, compare=False)
    receipt_sha256: str = field(repr=False)
    components: tuple["VerifiedProgram", ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class BendAdmission:
    contract: Bundle
    entrypoint: str
    toolchain: Toolchain
    owner_argument: bool = False
    checker: str | None = None
    input_count: int = 0
    owner: PrivateData = field(default=None, repr=False, compare=False)
    _issuer: object = field(default_factory=object, repr=False, compare=False, init=False)

    def __post_init__(self):
        names([self.entrypoint], "execution entrypoint")
        if type(self.owner_argument) is not bool:
            raise ContractError("owner argument selection must be boolean")
        if self.checker is not None:
            self.plugin_check()

    def verify_program(self, source: str, timeout: float = 60) -> VerifiedProgram:
        """Check the frozen laws without invoking the application evaluator."""
        if not isinstance(source, str) or not source or len(source.encode()) > 65536:
            raise ContractError("candidate must be 1..65536 bytes of Bend source")
        candidate = (Source("candidate/program.bend", source),)
        # The pinned backend closes the owner's concrete law obligations after
        # parsing the candidate. The model authors programs, never these proofs.
        receipt = verify(self.contract, candidate, self.toolchain, timeout,
                         automatic_obligations=True, execution_entrypoint=self.entrypoint,
                         plugin_check=self.plugin_check())
        if receipt["status"] != "accepted":
            # Owner audit shows plugin names as written, not their isolated form.
            if "diagnostics" in receipt.get("source_check", {}):
                receipt["source_check"]["diagnostics"] = ISOLATED_NAME.sub("", receipt["source_check"]["diagnostics"])
            raise AdmissionRejected(receipt)
        receipt_bytes = canonical(receipt)
        return VerifiedProgram(source, receipt_bytes, self._issuer, hashlib.sha256(receipt_bytes).hexdigest())

    def plugin_check(self) -> PluginCheck | None:
        """Bind the owner checker and public input arity to each verification."""
        return None if self.checker is None else PluginCheck(self.checker, self.input_count)

    def validate_program(self, program: VerifiedProgram) -> dict:
        """Check a process-local approval against this frozen checking context."""
        if not isinstance(program, VerifiedProgram) or program.issuer is not self._issuer:
            raise ContractError("execution requires this gate's verified program")
        if hashlib.sha256(program.receipt_bytes).hexdigest() != program.receipt_sha256:
            raise ContractError("verified program binding mismatch")
        receipt = decode(program.receipt_bytes)
        expected = [{"path": "candidate/program.bend", "content": program.source}]
        if (receipt["status"] != "accepted" or receipt["contract_id"] != self.contract.identity
                or receipt["inputs"]["candidate"] != expected
                or receipt["inputs"].get("execution_entrypoint") != self.entrypoint
                or receipt["inputs"].get("plugin_check") != (self.plugin_check().data() if self.checker is not None else None)):
            raise ContractError("verified program binding mismatch")
        if self.toolchain.validate() != receipt["toolchain"]:
            raise ContractError("toolchain changed after verification")
        return receipt

    def execute_program(self, program: VerifiedProgram, timeout: float = 60,
                        *, context: ExecutionContext | None = None) -> tuple[Write, ...]:
        """Execute accepted Bend source and decode its bounded primitive writes."""
        receipt = self.validate_program(program)
        if self.checker is not None and (context is None or len(context.inputs) != self.input_count):
            raise ContractError("execution input count differs from plugin verification")
        # Evaluate exactly the in-memory sources already checked. Never reread an
        # agent-controlled file and never execute the candidate as native code.
        # Only application source and the selected entrypoint cross into execution.
        # Native verifier code and verification facts stay in the owner receipt.
        application = {key: receipt["inputs"][key]
                       for key in ("contract", "candidate", "execution_entrypoint")}
        inputs = {"inputs": application,
                  "call": {"schema": 1, "function": self.entrypoint}}
        if context is not None:
            if not isinstance(context, ExecutionContext):
                raise ContractError("typed private execution context required")
            inputs["call"]["private_inputs"] = [base64.b64encode(data).decode() for data in context.inputs]
            if self.owner_argument:
                inputs["call"]["owner_context"] = {"schema": 1, "data": encode_owner_data(self.owner)}
        with tempfile.TemporaryDirectory(prefix="proofguard-evaluate-") as directory:
            result = execute([
                str(self.toolchain.node), "--experimental-strip-types",
                "--max-old-space-size=4096", "--stack-size=7000", str(ROOT / "backend/evaluate.mjs"),
                str(self.toolchain.source.resolve()),
            ], canonical(inputs), Path(directory), timeout, output_limit=2 * 1024 * 1024)
        if result.status != 0 or result.timed_out:
            raise ContractError(f"checked source evaluation failed (status={result.status}, timeout={result.timed_out}): "
                                + result.output)
        reply = fields(decode(result.output.encode()), {"schema", "status", "value"}, "evaluator response")
        if type(reply["schema"]) is not int or reply["schema"] != 1 or reply["status"] != "evaluated":
            raise ContractError("invalid evaluator response")
        if self.toolchain.validate() != receipt["toolchain"]:
            raise ContractError("toolchain changed during evaluation")
        return decode_writes(reply["value"])


@dataclass(frozen=True)
class ProgramAdmission:
    """Require independent law packages before running the foundation program."""

    runtime: BendAdmission
    plugins: tuple[BendAdmission, ...]
    _issuer: object = field(default_factory=object, repr=False, compare=False, init=False)

    def __post_init__(self):
        if (type(self.plugins) is not tuple or not 1 <= len(self.plugins) <= 16
                or any(type(plugin) is not BendAdmission for plugin in self.plugins)):
            raise ContractError("enroll 1..16 law plugins")
        if self.runtime.entrypoint != "main" or self.runtime.owner_argument:
            raise ContractError("foundation runtime requires main: PGInputs -> PGIO")
        if any(plugin.toolchain != self.runtime.toolchain for plugin in self.plugins):
            raise ContractError("plugin and runtime toolchains must match")

    @property
    def contract(self) -> Bundle:
        return self.runtime.contract

    @property
    def entrypoint(self) -> str:
        return self.runtime.entrypoint

    @property
    def toolchain(self) -> Toolchain:
        return self.runtime.toolchain

    def verify_program(self, source: str, timeout: float = 60) -> VerifiedProgram:
        """Check the foundation-only context first, then each private law context."""
        deadline = time.monotonic() + timeout
        source = ANNOTATED_MAIN.sub(r"def main(\1):", source)
        checked = []
        for gate in (self.runtime, *self.plugins):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdmissionRejected({"schema": 1, "status": "rejected", "reason": "verification deadline exceeded"})
            try:
                checked.append(gate.verify_program(source, remaining))
            except AdmissionRejected as error:
                error.receipt["completed_checks"] = len(checked)
                # The foundation context holds no plugin source or private data,
                # so its errors describe only the agent's own program.
                error.feedback = error.receipt.get("source_check", {}).get("diagnostics") if not checked else None
                raise
        receipt = {"schema": 1, "status": "accepted", "toolchain": self.toolchain.validate(),
                   "plugins": [decode(program.receipt_bytes) for program in checked[1:]],
                   "runtime": decode(checked[0].receipt_bytes)}
        raw = canonical(receipt)
        return VerifiedProgram(source, raw, self._issuer, hashlib.sha256(raw).hexdigest(), tuple(checked))

    def execute_program(self, program: VerifiedProgram, timeout: float = 60,
                        *, context: ExecutionContext | None = None) -> tuple[Write, ...]:
        """Run original main; plugin guards may veto but cannot replace its batch."""
        if not isinstance(program, VerifiedProgram) or program.issuer is not self._issuer:
            raise ContractError("execution requires this gate's verified program")
        if hashlib.sha256(program.receipt_bytes).hexdigest() != program.receipt_sha256:
            raise ContractError("verified program binding mismatch")
        gates = (self.runtime, *self.plugins)
        if len(program.components) != len(gates):
            raise ContractError("missing plugin acceptance")
        receipts = []
        for gate, part in zip(gates, program.components):
            if part.source != program.source:
                raise ContractError("plugin accepted different source")
            if gate.checker is not None and (context is None or len(context.inputs) != gate.input_count):
                raise ContractError("execution input count differs from plugin verification")
            receipts.append(gate.validate_program(part))
        expected = {"schema": 1, "status": "accepted", "toolchain": self.toolchain.validate(),
                    "plugins": receipts[1:], "runtime": receipts[0]}
        if program.receipt_bytes != canonical(expected):
            raise ContractError("verified program binding mismatch")
        deadline = time.monotonic() + timeout
        writes = self.runtime.execute_program(program.components[0], timeout, context=context)
        for gate, part in zip(self.plugins, program.components[1:]):
            if gate.entrypoint == "main":
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ContractError("execution deadline exceeded")
            # Bend guard runners compute main and check concrete values.
            # Comparing the complete batch prevents a runner from substituting
            # its own effect. All evaluation remains pure until Gateway dispatch.
            if gate.execute_program(part, remaining, context=context) != writes:
                raise ContractError("plugin guard changed the program's effect batch")
        return writes
