"""Fixed IO transport codecs; application computation has already run in Bend."""

import base64
from dataclasses import dataclass

from bendspec.model import ContractError, fields


class ProgramAborted(ContractError):
    """A checked Bend computation cancelled its complete IO batch."""


@dataclass(frozen=True)
class Write:
    capability: int
    data: bytes


def decode_writes(value: object) -> tuple[Write, ...]:
    """Validate the compact primitive frame before dispatching any IO."""
    if isinstance(value, dict) and set(value) == {"schema", "abort"}:
        if type(value["schema"]) is not int or value["schema"] != 1 or value["abort"] is not True:
            raise ContractError("invalid primitive abort frame")
        raise ProgramAborted("Bend program aborted before IO")
    row = fields(value, {"schema", "writes"}, "primitive IO frame")
    if type(row["schema"]) is not int or row["schema"] != 1:
        raise ContractError("unsupported primitive IO frame")
    if not isinstance(row["writes"], list) or len(row["writes"]) > 16:
        raise ContractError("expected at most sixteen primitive writes")
    writes = []
    for item in row["writes"]:
        write = fields(item, {"destination", "bytes_base64"}, "primitive write")
        if type(write["destination"]) is not int or not 0 <= write["destination"] < 16:
            raise ContractError("primitive destination index out of range")
        encoded = write["bytes_base64"]
        if not isinstance(encoded, str) or len(encoded) > 87384:
            raise ContractError("oversized primitive byte string")
        data = base64.b64decode(encoded, validate=True)
        if len(data) > 65536 or base64.b64encode(data).decode() != encoded:
            raise ContractError("noncanonical or oversized primitive byte string")
        writes.append(Write(write["destination"], data))
    return tuple(writes)
