"""One fresh authoring session: the seam every harness adapter builds on.

A harness brings an agent runtime and supervises its processes. This module owns
what must not vary between harnesses: loading the enrollment, building the gateway
and worker, publishing the public task view, confining the agent, and reporting
outcomes. It names no plugin and no harness, and harnesses need no other foundation
module.
"""

from collections.abc import Callable
import json
from pathlib import Path
import re
import sys

from bendspec.model import ContractError, canonical
from .audit import overlaps, publish
from .packages import Enrollment
from .sandbox import AuthorSandbox
from .worker import OutboxWorker

__all__ = ["AuthoringSession", "ContractError", "canonical"]

MAX_BRIEF = 32768
TASK_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class AuthoringSession:
    def __init__(self, authority: Path, enrollment: str, root: Path, brief: str, *,
                 stage_runtime: Callable[[Path], None], files: dict[str, bytes] | None = None):
        """Prepare a public guest view in a new root, keeping authority outside it.

        `stage_runtime` writes the harness's guest code to the directory it receives.
        `files` become extra public task files.
        """
        if not isinstance(brief, str) or not brief or len(brief.encode()) > MAX_BRIEF:
            raise ContractError("bounded public brief required")
        for name in files or {}:
            if (not isinstance(name, str) or not TASK_FILE.fullmatch(name)
                    or name in ("task.json", "brief.txt")):
                raise ContractError("extra task files need plain, unreserved names")
        self.authority = authority.resolve(strict=True)
        self.enrollment = enrollment
        root = root.absolute()
        root.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.root = root.resolve(strict=True)
        selected = Enrollment.load(self.authority, enrollment)
        self.worker = OutboxWorker(selected.build_gateway(self.authority), self.root / "outbox", self.root / "results")
        self.runtime, self.task = self.root / "runtime", self.root / "task"
        stage_runtime(self.runtime)
        self.task.mkdir()
        (self.task / "task.json").write_bytes(canonical(selected.get_task(self.authority)))
        (self.task / "brief.txt").write_text(brief)
        for name, data in (files or {}).items():
            (self.task / name).write_bytes(data)
        self.sandbox = AuthorSandbox(self.worker, runtime=self.runtime, task=self.task)

    def require_private(self, path: Path) -> None:
        """Refuse an owner path, such as credentials, that the agent's mounts would expose."""
        resolved = path.resolve(strict=True)
        mounts = (Path("/usr"), self.runtime, self.task, self.worker.outbox, self.worker.results)
        if any(overlaps(resolved, mount) for mount in mounts):
            raise ContractError("private owner path overlaps public sandbox mounts")

    def agent_command(self, argv: list[str]) -> list[str]:
        """Wrap the harness's guest command in the fixed sandbox."""
        return self.sandbox.command(argv)

    def worker_command(self, parent: int) -> list[str]:
        """Build the owner worker command; it exits with the process `parent`."""
        return [sys.executable, "-I", str(Path(__file__).resolve().with_name("cli.py")), "watch",
                "--authority", str(self.authority), "--enrollment", self.enrollment,
                "--outbox", str(self.worker.outbox), "--results", str(self.worker.results),
                "--parent", str(parent)]

    def outcomes(self) -> list[dict]:
        """Read the gateway result recorded for each proposal, oldest name first."""
        return [json.loads(path.read_bytes()) for path in sorted(self.worker.results.glob("*.json"))]

    def needs_reconciliation(self, outcomes: list[dict]) -> bool:
        """Report an uncertain outcome, or a claimed proposal that never got a result."""
        return (any(row["status"] == "uncertain" for row in outcomes)
                or any(not (self.worker.results / (proposal + ".json")).exists()
                       for proposal in self._claimed()))

    def _claimed(self) -> list[str]:
        """Proposal IDs the worker claimed from this session's outbox.

        Claims live with the enrollment's session state, which other authoring
        sessions on the same enrollment share; each claim records its outbox.
        """
        claimed = []
        for path in self.worker.claims.glob("*.claim"):
            record = json.loads(path.read_bytes())
            if record.get("outbox") == str(self.worker.outbox):
                claimed.append(record["proposal"])
        return claimed

    def publish_report(self, report: dict) -> None:
        """Record the session's final report atomically in its root."""
        publish(self.root / "report.json", canonical(report))
