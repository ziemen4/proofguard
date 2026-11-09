"""Capture source once and keep owner authority outside candidate workspaces."""

import os
from pathlib import Path
import stat
import tempfile

from .model import Bundle, ContractError, Source, canonical, contract_id, decode

MAX_SOURCE_BYTES = 1024 * 1024


def read_regular(root: Path, relative: str, limit: int = MAX_SOURCE_BYTES) -> bytes:
    """Read bounded bytes through directory descriptors, rejecting symlinks."""
    if Path(relative).is_absolute() or any(x in ("", ".", "..") for x in relative.split("/")):
        raise ContractError("invalid relative file path")
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ContractError("cannot open source root") from exc
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ContractError("source must be a regular file")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ContractError("source size limit exceeded")
            return data
    except OSError as exc:
        raise ContractError(f"cannot read regular file: {relative}") from exc
    finally:
        os.close(directory)


def capture(root: Path, selected: tuple[str, ...]) -> tuple[Source, ...]:
    """Read the selected UTF-8 sources once under a shared total-byte limit."""
    remaining = MAX_SOURCE_BYTES
    result = []
    for path in selected:
        data = read_regular(root, path, remaining)
        remaining -= len(data)
        try:
            content = data.decode("utf-8")
        except UnicodeError as exc:
            raise ContractError(f"non-UTF-8 source: {path}") from exc
        result.append(Source.parse({"path": path, "content": content}))
    return tuple(result)


def atomic_write(path: Path, data: bytes) -> None:
    """Replace an owner-selected output atomically; never follow its final symlink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Authority:
    """Operator-owned approved bundles; directory permissions remain the trust root."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def load(self, identity: str) -> Bundle:
        """Read the approved contract and verify its content identity."""
        contract_id(identity)
        bundle = Bundle.parse(decode(read_regular(self.root, f"contracts/{identity}.json", 8 * MAX_SOURCE_BYTES)))
        if bundle.identity != identity:
            raise ContractError("approved contract digest mismatch")
        return bundle

    def approve(self, bundle: Bundle) -> str:
        """Persist an owner-approved bundle."""
        identity = bundle.identity
        path = self.root / "contracts" / f"{identity}.json"
        if path.exists() or path.is_symlink():
            self.load(identity)
        else:
            atomic_write(path, canonical(bundle.data()) + b"\n")
        return identity
