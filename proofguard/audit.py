"""Durable content-addressed owner records, not remote authorization tokens.

JSON here is a canonical archive format. The gateway's authorization bindings
use PGW1 binary framing; a JSON status field can never authorize an effect.
"""

import hashlib
import os
from pathlib import Path
import tempfile
import stat

from bendspec.model import canonical
from bendspec.workspace import read_regular


def sync_directory(path: Path) -> None:
    """Flush published directory entries before acknowledging owner records."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def overlaps(left: Path, right: Path) -> bool:
    """Report whether either path contains the other."""
    return left.is_relative_to(right) or right.is_relative_to(left)


def durable_directory(path: Path) -> None:
    """Create and flush each ancestor before publishing durable owner records."""
    path = path.absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        child = current / part
        try:
            child.mkdir(mode=0o700)
        except FileExistsError:
            if not stat.S_ISDIR(child.lstat().st_mode):
                raise ValueError("owner directory hierarchy contains a non-directory")
        sync_directory(current)
        sync_directory(child)
        current = child


def publish(path: Path, data: bytes) -> None:
    """Atomically create a complete immutable record or verify an identical one."""
    fd, temporary = tempfile.mkstemp(prefix=".audit-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if read_regular(path.parent, path.name, 16 * 1024 * 1024) != data:
                raise ValueError("existing owner record corrupted")
        # An existing link may have been published by a concurrent process that
        # has not flushed its directory yet. Both branches must establish durability.
        sync_directory(path.parent)
    finally:
        os.unlink(temporary)


class Audit:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("owner audit directory required")

    def store(self, record: object) -> str:
        """Persist canonical JSON by SHA-256 and return its owner-audit identifier."""
        data = canonical(record)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("audit record exceeds limit")
        identity = hashlib.sha256(data).hexdigest()
        path = self.root / f"{identity}.json"
        publish(path, data)
        return identity
