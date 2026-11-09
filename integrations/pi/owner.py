"""Owner-side supervision for a networkless pi author and its pull worker."""

from dataclasses import dataclass
import ctypes
from enum import Enum
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import time

from proofguard.session import AuthoringSession, ContractError, canonical

ROOT = Path(__file__).resolve().parents[2]
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "TMPDIR": "/tmp"}
MAX_FRAME = 8 * 1024 * 1024
MAX_STREAM = 128 * 1024 * 1024


class SessionFailure(str, Enum):
    DEADLINE = "session_deadline"
    CANCELLED = "owner_cancelled"
    PROTOCOL = "invalid_harness_protocol"
    UNAVAILABLE = "child_unavailable"


class SupervisionError(RuntimeError):
    def __init__(self, reason: SessionFailure):
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class Provider:
    """Owner-only inference selection; no endpoint or credentials enter the guest."""
    auth_dir: Path
    provider: str
    model: str
    reasoning: str = ""
    template: str = ""


def link_or_copy(source, destination):
    """Share installed public dependencies without copying hundreds of MiB."""
    try:
        return os.link(source, destination)
    except OSError:
        return shutil.copy2(source, destination)


def stage_runtime(destination: Path):
    """Stage only guest code and installed dependencies, preserving internal links."""
    source = ROOT / "integrations/pi"
    destination.mkdir()
    for path in source.glob("*.mjs"):
        if path.name != "provider-relay.mjs":
            shutil.copy2(path, destination / path.name)
    for name in ("package.json", "package-lock.json"):
        shutil.copy2(source / name, destination / name)
    shutil.copytree((source / "node_modules").resolve(strict=True), destination / "node_modules",
                    symlinks=True, copy_function=link_or_copy)


class PiSession:
    def __init__(self, authority: Path, enrollment: str, root: Path, brief: str,
                 *, scenario: object | None = None, provider: Provider | None = None,
                 deadline: float = 300, files: dict[str, bytes] | None = None):
        """Prepare a fresh public guest view, retaining authority outside the namespace."""
        if (scenario is None) == (provider is None):
            raise ContractError("select one explicit scenario or owner inference provider")
        if not 0 < deadline <= 600:
            raise ContractError("session deadline must be within 600 seconds")
        public = dict(files or {})
        if scenario is not None:
            public["scenario.json"] = canonical(scenario)
            if len(public["scenario.json"]) > 262144:
                raise ContractError("scenario exceeds the public fixture bound")
        self.provider, self.scenario, self.deadline = provider, scenario, deadline
        self.session = AuthoringSession(authority, enrollment, root, brief, stage_runtime=stage_runtime, files=public)
        self.root, self.worker = self.session.root, self.session.worker
        (self.root / "owner-tmp").mkdir(mode=0o700)
        if provider is not None:
            self.session.require_private(provider.auth_dir)
        self.children = {}
        self.buffers, self.sizes = {}, {}
        self.outgoing = {}
        self.selector = selectors.DefaultSelector()
        self.pending = None
        self.next_inference = 1
        self.complete = None

    def _start(self, name: str, command: list[str], *, parent_death=False):
        """Close owner descriptors and supervise every child from its first instruction."""
        parent = os.getpid()

        def bind_parent():
            if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
                os._exit(1)

        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 close_fds=True, env=dict(CLEAN_ENV, TMPDIR=str(self.root / "owner-tmp")),
                                 cwd=str(self.root), start_new_session=True,
                                 preexec_fn=bind_parent if parent_death else None)
        self.children[name] = child
        self.outgoing[name] = bytearray()
        os.set_blocking(child.stdin.fileno(), False)
        for stream_name, stream in ((name, child.stdout), (name + "_stderr", child.stderr)):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ, stream_name)
            self.buffers[stream_name] = bytearray()
            self.sizes[stream_name] = 0
        self._record("owner_start", {"role": name, "pid": child.pid})
        return child

    def _record(self, kind: str, value: object):
        self.trace.write(canonical({"kind": kind, "value": value}) + b"\n")
        self.trace.flush()

    def _forward(self, child: str, frame: object):
        data = canonical(frame) + b"\n"
        if len(data) > MAX_FRAME:
            raise SupervisionError(SessionFailure.PROTOCOL)
        queue = self.outgoing[child]
        if len(queue) + len(data) > MAX_FRAME:
            raise SupervisionError(SessionFailure.PROTOCOL)
        if not queue:
            self.selector.register(self.children[child].stdin, selectors.EVENT_WRITE, (child, "write"))
        queue.extend(data)

    def _write(self, child: str):
        """Keep stalled guest/provider pipes from blocking the owner deadline."""
        stream = self.children[child].stdin
        queue = self.outgoing[child]
        try:
            count = os.write(stream.fileno(), queue)
        except BlockingIOError:
            return
        del queue[:count]
        if not queue:
            self.selector.unregister(stream)

    def _frame(self, role: str, frame: object):
        """Route inference only; gateway authority never travels over this channel."""
        if not isinstance(frame, dict):
            raise SupervisionError(SessionFailure.PROTOCOL)
        self._record(role, frame)
        if role == "worker":
            return
        if role == "guest":
            match frame.get("kind"):
                case "trace":
                    if set(frame) != {"kind", "event"}:
                        raise SupervisionError(SessionFailure.PROTOCOL)
                case "inference":
                    if (self.provider is None or self.pending is not None or
                            set(frame) != {"kind", "id", "context"} or type(frame["id"]) is not int or
                            frame["id"] != self.next_inference or self.next_inference > 12):
                        raise SupervisionError(SessionFailure.PROTOCOL)
                    self.pending = frame["id"]
                    self.next_inference += 1
                    self._forward("relay", frame)
                case "complete":
                    expected = {"kind", "answer", "tools", "activeTools", "completed", "mode"}
                    if frame.get("completed") is False:
                        expected.add("error")
                    if (self.complete is not None or set(frame) != expected or
                            type(frame["completed"]) is not bool or frame["activeTools"] != [] or
                            frame["mode"] != ("scenario" if self.scenario is not None else "relay") or
                            frame.get("error") not in (None, "guest_failed") or not isinstance(frame["tools"], list) or
                            sorted(frame["tools"]) != ["read", "write_program"] or
                            not isinstance(frame["answer"], str) or len(frame["answer"].encode()) > 65536):
                        raise SupervisionError(SessionFailure.PROTOCOL)
                    self.complete = frame
                case _:
                    raise SupervisionError(SessionFailure.PROTOCOL)
        elif role == "relay":
            if self.pending is None or frame.get("id") != self.pending:
                raise SupervisionError(SessionFailure.PROTOCOL)
            if frame.get("kind") == "inference_error" and set(frame) == {"kind", "id"}:
                self.pending = None
            elif (frame.get("kind") == "inference_event" and set(frame) == {"kind", "id", "event"}
                  and isinstance(frame["event"], dict)):
                if frame["event"].get("type") == "done":
                    self.pending = None
            else:
                raise SupervisionError(SessionFailure.PROTOCOL)
            self._forward("guest", frame)

    def _read(self, stream, role: str):
        """Read bounded JSONL while recording stderr separately from protocol frames."""
        block = os.read(stream.fileno(), 65536)
        if not block:
            self.selector.unregister(stream)
            if self.buffers[role]:
                raise SupervisionError(SessionFailure.PROTOCOL)
            return
        self.sizes[role] += len(block)
        if self.sizes[role] > (1048576 if role.endswith("_stderr") else MAX_STREAM):
            raise SupervisionError(SessionFailure.PROTOCOL)
        if role.endswith("_stderr"):
            self._record(role, block.decode("utf-8", errors="replace"))
            return
        buffer = self.buffers[role]
        buffer.extend(block)
        while b"\n" in buffer:
            end = buffer.index(b"\n")
            if end + 1 > MAX_FRAME:
                raise SupervisionError(SessionFailure.PROTOCOL)
            line = bytes(buffer[:end])
            del buffer[:end + 1]
            self._frame(role, json.loads(line.decode("utf-8")))
        if len(buffer) >= MAX_FRAME:
            raise SupervisionError(SessionFailure.PROTOCOL)

    def close(self):
        """Terminate owned processes and reap them; published proposals stay consumed."""
        for child in self.children.values():
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for child in self.children.values():
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=3)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()
        self.selector.close()

    def run(self) -> dict:
        """Archive complete SDK traffic and report it separately from actual gate outcomes."""
        failure = None
        self.trace = (self.root / "trace.jsonl").open("xb")
        os.chmod(self.root / "trace.jsonl", 0o600)
        try:
            self._start("worker", self.session.worker_command(os.getpid()))
            if self.provider is not None:
                self._start("relay", ["/usr/bin/node", "--use-openssl-ca", str(ROOT / "integrations/pi/provider-relay.mjs"),
                                      str(self.provider.auth_dir.resolve(strict=True)), self.provider.model,
                                      self.provider.provider, self.provider.reasoning, self.provider.template],
                            parent_death=True)
            command = ["/usr/bin/node", "/runtime/guest.mjs", "/task/task.json", "/task/brief.txt"]
            if self.scenario is not None:
                command.append("/task/scenario.json")
            guest = self._start("guest", self.session.agent_command(command))
            until = time.monotonic() + self.deadline
            while True:
                if time.monotonic() >= until:
                    raise SupervisionError(SessionFailure.DEADLINE)
                for key, _ in self.selector.select(timeout=0.1):
                    if isinstance(key.data, tuple):
                        self._write(key.data[0])
                    else:
                        self._read(key.fileobj, key.data)
                guest_streams = any(key.data in ("guest", "guest_stderr") for key in self.selector.get_map().values())
                if guest.poll() is not None and not guest_streams:
                    break
                if any(child.poll() is not None for name, child in self.children.items() if name != "guest"):
                    raise SupervisionError(SessionFailure.UNAVAILABLE)
        except SupervisionError as exc:
            failure = exc.reason
        except (OSError, ValueError, TypeError):
            failure = SessionFailure.PROTOCOL
        finally:
            self.close()
            self.trace.flush()
            os.fsync(self.trace.fileno())
            self.trace.close()
        outcomes = self.session.outcomes()
        guest = self.children.get("guest")
        report = {"schema": 1, "mode": "scenario" if self.scenario is not None else "provider",
                  "remote_inference": self.provider is not None,
                  "sdk_completed": failure is None and self.complete is not None and self.complete["completed"] and guest.returncode == 0,
                  "error": failure.value if failure else None, "guest_exit_code": guest.returncode if guest else None,
                  "enrollment_id": self.session.enrollment, "answer": self.complete["answer"] if self.complete else None,
                  "tools": self.complete["tools"] if self.complete else None, "gateway_outcomes": outcomes,
                  "reconciliation_required": self.session.needs_reconciliation(outcomes)}
        self.session.publish_report(report)
        return report
