"""Source-bound proof checking with two independent checker stages.

The operator provisions the pinned sources and Lean-built kernel. Candidate data
cannot select commands or environment. Native compilation is outside this gate.
"""

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import resource
import subprocess
import tempfile
import time
import ctypes

from .model import Bundle, ContractError, Source, canonical, decode, digest, names, text

ROOT = Path(__file__).resolve().parents[1]
_PRCTL = ctypes.CDLL(None, use_errno=True).prctl
_PRCTL.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
_PRCTL.restype = ctypes.c_int


def sha(path: Path) -> str:
    """Hash a local toolchain file without loading the whole file into memory."""
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


@dataclass(frozen=True)
class ProcessResult:
    status: int
    timed_out: bool
    output: str


def execute(command: list[str], data: bytes, cwd: Path, timeout: float,
            kernel: bool = False, *, output_limit: int = 65536) -> ProcessResult:
    """Run a pinned checker/evaluator with bounded output and process lifetime."""
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(cwd),
           "TMPDIR": str(cwd), "LEAN_STACK_SIZE_KB": "8192", "LEAN_NUM_THREADS": "1"}
    parent_pid = os.getpid()

    def limits():
        """Bind the child to its supervisor and bound CPU, files and kernel memory."""
        # Each backend starts its own session. Parent-death SIGKILL is independent
        # of the supervisor's Python handlers and covers supervisor SIGKILL too.
        if _PRCTL(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "cannot supervise checker parent death")
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGKILL)
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
        # The wall-clock timeout is the real bound. RLIMIT_CPU counts every thread
        # (Node's GC and compiler threads too), so it gets headroom over the wall
        # time and only stops a runaway process that the wall timer missed.
        cpu = 2 * math.ceil(timeout) + 1
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        if kernel:
            # Lean's allocator reserves virtual arenas even for tiny proofs.
            # 1–2 GiB prevents startup; 4 GiB bounds the process and permits it.
            resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))

    with tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile() as outgoing:
        incoming.write(data)
        incoming.seek(0)
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=incoming,
                                stdout=outgoing, stderr=outgoing, start_new_session=True,
                                preexec_fn=limits)
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        outgoing.seek(0)
        output = outgoing.read(output_limit + 1)
        if len(output) > output_limit:
            return ProcessResult(1, timed_out, "Verifier output exceeded its limit")
        return ProcessResult(proc.returncode, timed_out, output.decode("utf-8", errors="replace"))


@dataclass(frozen=True)
class Toolchain:
    source: Path
    kernel: Path
    kernel_sha256: str
    node: Path = Path("/usr/bin/node")
    libraries: tuple[tuple[Path, str], ...] = ()

    @classmethod
    def installed(cls) -> "Toolchain":
        """Read the owner's provisioned executable, source and runtime locations."""
        path = ROOT / ".cache/runtime.json"
        try:
            configuration = decode(path.read_bytes())
            return cls(ROOT / configuration["source"], ROOT / configuration["kernel"],
                       configuration["kernel_sha256"], Path(configuration["node"]),
                       tuple((Path(path), value) for path, value in configuration["libraries"].items()))
        except (OSError, KeyError, TypeError) as exc:
            raise ContractError("Bend proof toolchain is not provisioned; see scripts/setup-toolchain.py") from exc

    def validate(self) -> dict:
        """Verify pinned source/runtime hashes and return their checking identity."""
        lock = decode((ROOT / "toolchain.lock.json").read_bytes())
        for name, expected in lock["files"].items():
            path = self.source / name
            if path.is_symlink() or sha(path) != expected:
                raise ContractError(f"toolchain source mismatch: {name}")
        if self.kernel.is_symlink() or sha(self.kernel) != self.kernel_sha256:
            raise ContractError("proof kernel binary mismatch")
        for path, expected in self.libraries:
            if sha(path) != expected:
                raise ContractError("Lean shared runtime mismatch")
        return {"bend_revision": lock["revision"], "sources": lock["files"],
                "kernel_sha256": self.kernel_sha256, "node_sha256": sha(self.node),
                "adapter_sha256": sha(ROOT / "backend/check.mjs"),
                "evaluator_sha256": sha(ROOT / "backend/evaluate.mjs"),
                "supervisor_sha256": sha(Path(__file__)),
                "model_sha256": sha(ROOT / "bendspec/model.py"),
                "workspace_sha256": sha(ROOT / "bendspec/workspace.py"),
                "package_sha256": sha(ROOT / "bendspec/__init__.py"),
                "obligations_sha256": sha(ROOT / "backend/obligations.mjs"),
                "program_adapter_sha256": sha(ROOT / "backend/program.mjs"),
                "io_codec_sha256": sha(ROOT / "backend/io.mjs"),
                "shared_runtime": {path.name: value for path, value in self.libraries},
                "profile": "pure-bendtt-v1"}


@dataclass(frozen=True)
class PluginCheck:
    """Captured plugin verifier and non-secret execution-interface metadata."""

    module: str
    input_count: int

    def __post_init__(self):
        text(self.module, "owner checker")
        if len(self.module.encode()) > 1024 * 1024:
            raise ContractError("owner checker exceeds source limit")
        if type(self.input_count) is not int or not 0 <= self.input_count <= 16:
            raise ContractError("owner checker requires a bounded input count")

    def data(self) -> dict:
        return {"module": self.module, "input_count": self.input_count}


def verify(bundle: Bundle, candidate: tuple[Source, ...], toolchain: Toolchain,
           timeout: float = 60.0, *, automatic_obligations: bool = False,
           execution_entrypoint: str | None = None,
           plugin_check: PluginCheck | None = None) -> dict:
    """Require source and kernel acceptance for one immutable contract/candidate."""
    if type(automatic_obligations) is not bool:
        raise ContractError("automatic obligation mode must be a trusted Boolean")
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 3600:
        raise ContractError("timeout must be between 0 and 3600 seconds")
    identity = toolchain.validate()
    inputs = {"contract": bundle.data(), "candidate": [source.data() for source in candidate]}
    if automatic_obligations:
        inputs["automatic_obligations"] = True
    if execution_entrypoint is not None:
        names([execution_entrypoint], "execution entrypoint")
        inputs["execution_entrypoint"] = execution_entrypoint
    if plugin_check is not None:
        if type(plugin_check) is not PluginCheck:
            raise ContractError("typed plugin verification required")
        inputs["plugin_check"] = plugin_check.data()
    receipt = {"schema": 1, "status": "rejected", "contract_id": bundle.identity,
               "candidate_id": digest(inputs["candidate"]), "toolchain": identity,
               "guarantee": "Bend source obligations checked through BendTT; translation and native compilation are not proved",
               "inputs": inputs}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="bendspec-") as temporary:
        work = Path(temporary)
        kernel_input = work / "candidate.bendtt"
        command = [str(toolchain.node), "--experimental-strip-types", "--max-old-space-size=512", "--stack-size=7000",
                   str(ROOT / "backend/check.mjs"), str(toolchain.source.resolve()), str(kernel_input)]
        checked = execute(command, canonical(inputs), work, timeout)
        receipt["source_check"] = {"exit_code": checked.status, "timed_out": checked.timed_out,
                                   "diagnostics": checked.output}
        if checked.status != 0 or checked.timed_out:
            return receipt
        # Both source check and kernel run must succeed; a quiet zero exit or an
        # unsupported translation is not proof evidence.
        try:
            reply = json.loads(checked.output)
        except json.JSONDecodeError:
            receipt["reason"] = "invalid source checker protocol"
            return receipt
        if (reply.get("status") != "source_checked" or not kernel_input.is_file()
                or type(reply.get("bindings")) is not list):
            receipt["reason"] = "missing source checker evidence"
            return receipt
        receipt["bindings"] = reply["bindings"]
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            receipt["reason"] = "verification deadline exceeded"
            return receipt
        translation = kernel_input.read_bytes()
        if len(translation) > 8 * 1024 * 1024:
            receipt["reason"] = "kernel translation exceeds limit"
            return receipt
        checked = execute([str(toolchain.kernel.resolve()), str(kernel_input)], b"", work, remaining, kernel=True)
        receipt["kernel_check"] = {"exit_code": checked.status, "timed_out": checked.timed_out,
                                   "diagnostics": checked.output}
        receipt["translation"] = {"sha256": hashlib.sha256(translation).hexdigest(),
                                  "text": translation.decode("utf-8")}
        if checked.status == 0 and not checked.timed_out and checked.output.strip() == "ALL PROOFS CHECK":
            if toolchain.validate() != identity:
                raise ContractError("toolchain changed during verification")
            receipt["status"] = "accepted"
    return receipt
