"""Immutable, owner-only data passed to the checked Bend entrypoint."""

import base64
from dataclasses import dataclass

from bendspec.model import ContractError
from .encoding import commitment

MAX_OWNER_CONTEXT = 4 * 1024 * 1024 + 4096
MAX_PRIVATE_NODES = 1024
MAX_PRIVATE_DEPTH = 128


@dataclass(frozen=True)
class PrivateBlob:
    data: bytes


@dataclass(frozen=True)
class PrivatePair:
    left: "PrivateData"
    right: "PrivateData"


PrivateData = PrivateBlob | PrivatePair | None


def decode_snapshot(value: object, limit: int) -> bytes:
    """Decode canonical owner bytes within the remaining allocation budget."""
    if not isinstance(value, str) or len(value) > 4 * ((limit + 2) // 3):
        raise ContractError("bounded base64 snapshot required")
    try:
        data = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise ContractError("invalid private snapshot") from error
    if len(data) > limit or base64.b64encode(data).decode() != value:
        raise ContractError("noncanonical or oversized private snapshot")
    return data


def private_nodes(value: PrivateData):
    """Walk immutable private data within independent byte and shape limits."""
    pending = [(value, 0)]
    nodes = size = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_PRIVATE_NODES or depth > MAX_PRIVATE_DEPTH:
            raise ContractError("private data exceeds shape limit")
        if item is None:
            pass
        elif type(item) is PrivateBlob and isinstance(item.data, bytes):
            size += len(item.data)
            if size > MAX_OWNER_CONTEXT:
                raise ContractError("private data exceeds byte limit")
        elif type(item) is PrivatePair:
            pending.extend(((item.right, depth + 1), (item.left, depth + 1)))
        else:
            raise ContractError("invalid private execution data")
        yield item


def decode_owner_data(value: object) -> PrivateData:
    """Freeze the generic owner tree without interpreting application records."""
    nodes = size = 0

    def decode(item, depth):
        nonlocal nodes, size
        nodes += 1
        if nodes > MAX_PRIVATE_NODES or depth > MAX_PRIVATE_DEPTH:
            raise ContractError("private data exceeds shape limit")
        if item is None:
            return None
        if type(item) is dict and set(item) == {"bytes_base64"}:
            data = decode_snapshot(item["bytes_base64"], MAX_OWNER_CONTEXT - size)
            size += len(data)
            return PrivateBlob(data)
        if (type(item) is dict and set(item) == {"pair"}
                and type(item["pair"]) is list and len(item["pair"]) == 2):
            return PrivatePair(decode(item["pair"][0], depth + 1), decode(item["pair"][1], depth + 1))
        raise ContractError("invalid private execution data")

    return decode(value, 0)


def encode_owner_data(value: PrivateData) -> object:
    """Serialize only the closed private-data algebra accepted by the codec."""
    # Validate before recursive encoding so malformed or oversized trees fail
    # within the same limits as enrollment and the evaluator-side transport.
    for _ in private_nodes(value):
        pass

    def encode(item):
        if item is None:
            return None
        if type(item) is PrivateBlob:
            return {"bytes_base64": base64.b64encode(item.data).decode()}
        return {"pair": [encode(item.left), encode(item.right)]}

    return encode(value)


def private_identity(value: PrivateData) -> str:
    """Commit to the PGV1 prefix tree: empty, length-framed blob, or pair."""
    chunks = [b"PGV1"]
    for item in private_nodes(value):
        if item is None:
            chunks.append(b"\x00")
        elif type(item) is PrivateBlob:
            chunks.extend((b"\x01", len(item.data).to_bytes(4, "big"), item.data))
        else:
            chunks.append(b"\x02")
    return commitment("private-data-v1", b"".join(chunks))


@dataclass(frozen=True)
class ExecutionContext:
    """Application snapshots and each owner-data plugin's private tree."""

    inputs: tuple[bytes, ...]
    owners: tuple[PrivateData, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.inputs, tuple) or len(self.inputs) > 16
                or any(not isinstance(value, bytes) or len(value) > 65536 for value in self.inputs)):
            raise ContractError("invalid private execution context")
        if not isinstance(self.owners, tuple):
            raise ContractError("invalid private execution context")
        for owner in self.owners:
            for _ in private_nodes(owner):
                pass

    @property
    def identity(self) -> str:
        """Bind application snapshots and owner data with separate framed domains."""
        application = commitment("application-inputs-v1", *self.inputs)
        return commitment("private-execution-v1", bytes.fromhex(application),
                          *(bytes.fromhex(private_identity(owner)) for owner in self.owners))
