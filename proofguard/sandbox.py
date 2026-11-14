"""Linux authoring sandbox with a single writable host mount: the outbox."""

from pathlib import Path
import os
import stat
import subprocess

from bendspec.model import ContractError
from .audit import overlaps
from .worker import OutboxWorker


def public_tree(path: Path) -> Path:
    """Require an owner-selected directory without device or IPC endpoints."""
    root = path.resolve(strict=True)
    if not path.is_absolute() or not root.is_dir():
        raise ContractError("sandbox mounts require absolute directories")
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in (*directories, *files):
            mode = (Path(parent) / name).lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                raise ContractError("public sandbox mounts cannot contain IPC or device endpoints")
    return root


TMPFS_BYTES = {"/tmp": 256 << 20, "/home": 64 << 20, "/run": 8 << 20}

class AuthorSandbox:
    def __init__(self, worker: OutboxWorker, *, runtime: Path, task: Path):
        """Mount public runtime/task trees; keep the entire owner authority outside."""
        self.worker = worker
        protected = (worker.gateway.authority, worker.outbox, worker.results,
                     *worker.gateway.session.get_protected_paths())
        if any(overlaps(Path("/usr"), path) for path in protected):
            raise ContractError("protected sandbox paths overlap the fixed /usr mount")
        self.runtime, self.task = public_tree(runtime), public_tree(task)
        if any(overlaps(path, secret) for path in (self.runtime, self.task) for secret in protected):
            raise ContractError("public sandbox mounts overlap protected or mutable resources")

    def command(self, argv: list[str]) -> list[str]:
        """Build the owner launch command; the agent never selects these mounts."""
        if not argv or not argv[0].startswith("/"):
            raise ContractError("sandbox command requires an absolute executable")
        return [
            "/usr/bin/bwrap", "--die-with-parent", "--new-session", "--unshare-all", "--unshare-user",
            "--disable-userns", "--cap-drop", "ALL", "--clearenv",
            "--ro-bind", "/usr", "/usr",
            "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--proc", "/proc", "--dev", "/dev",
            # Private scratch space, capped so the agent cannot exhaust host memory.
            "--size", str(TMPFS_BYTES["/tmp"]), "--tmpfs", "/tmp",
            "--size", str(TMPFS_BYTES["/home"]), "--tmpfs", "/home",
            "--size", str(TMPFS_BYTES["/run"]), "--tmpfs", "/run",
            "--dir", "/home/agent", "--dir", "/etc",
            "--ro-bind", str(self.runtime), "/runtime",
            "--ro-bind", str(self.task), "/task",
            "--ro-bind", str(self.worker.results), "/results",
            "--bind", str(self.worker.outbox), "/outbox",
            "--setenv", "PATH", "/usr/bin:/bin",
            "--setenv", "HOME", "/home/agent", "--setenv", "TMPDIR", "/tmp",
            "--chdir", "/task", "--remount-ro", "/", "--", *argv,
        ]

    def run(self, argv: list[str], *, timeout: float = 20, input: str | None = None):
        """Run a bounded owner-selected harness/probe without inherited descriptors."""
        return subprocess.run(self.command(argv), input="" if input is None else input, capture_output=True,
                              text=True, timeout=timeout, close_fds=True,
                              env={"PATH": "/usr/bin:/bin"}, cwd="/")
