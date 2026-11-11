"""Fixed IO transport codecs; application computation has already run in Bend."""

import base64
from dataclasses import dataclass
from enum import Enum

from bendspec.model import ContractError, fields


class ProgramAborted(ContractError):
    """A checked Bend computation cancelled its current step of IO."""


class EffectDenied(ValueError):
    """The host refused one concrete effect; it was never dispatched."""


class HttpMethod(Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class HttpFailure(Enum):
    """Why a call produced no usable response; the program receives this value."""

    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    BROKEN = "broken"
    OVERSIZED = "oversized"


@dataclass(frozen=True)
class Write:
    capability: int
    data: bytes


@dataclass(frozen=True)
class HttpRequest:
    """Concrete request bytes; the program's values, unchecked until dispatch."""

    endpoint: int
    method: HttpMethod
    url: bytes
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


HttpResult = HttpResponse | HttpFailure


@dataclass(frozen=True)
class Step:
    """A pending call and the writes the program computed before it."""

    writes: tuple[Write, ...]
    request: HttpRequest


# A finished program yields its last writes; one that reached a call yields a Step.
Evaluation = tuple[Write, ...] | Step


@dataclass(frozen=True)
class Exchange:
    """One dispatched step and the response its call produced."""

    step: Step
    response: HttpResult


def encode_response(result: HttpResult) -> dict:
    """Serialize a recorded result for the evaluator's closed response codec."""
    if isinstance(result, HttpFailure):
        return {"failure": result.value}
    return {"status": result.status, "body_base64": base64.b64encode(result.body).decode()}


def decode_bytes(encoded: object) -> bytes:
    """Decode one canonical byte string within the primitive payload limit."""
    if not isinstance(encoded, str) or len(encoded) > 87384:
        raise ContractError("oversized primitive byte string")
    data = base64.b64decode(encoded, validate=True)
    if len(data) > 65536 or base64.b64encode(data).decode() != encoded:
        raise ContractError("noncanonical or oversized primitive byte string")
    return data


def decode_request(value: object) -> HttpRequest:
    """Decode the request's shape; Endpoint.authorize judges its meaning."""
    row = fields(value, {"endpoint", "method", "url_base64", "headers", "body_base64"}, "primitive request")
    if type(row["endpoint"]) is not int or not 0 <= row["endpoint"] < 16:
        raise ContractError("primitive endpoint index out of range")
    if not isinstance(row["headers"], list) or len(row["headers"]) > 16:
        raise ContractError("expected at most sixteen request headers")
    headers = []
    for item in row["headers"]:
        header = fields(item, {"name_base64", "value_base64"}, "primitive header")
        headers.append((decode_bytes(header["name_base64"]), decode_bytes(header["value_base64"])))
    try:
        method = HttpMethod(row["method"])
    except ValueError as error:
        raise ContractError("unsupported request method") from error
    return HttpRequest(row["endpoint"], method, decode_bytes(row["url_base64"]), tuple(headers),
                       decode_bytes(row["body_base64"]))


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
        writes.append(Write(write["destination"], decode_bytes(write["bytes_base64"])))
    return tuple(writes)


def decode_call(value: dict) -> Step:
    """Decode a frame that ends at a call, using the write frame's own rules."""
    return Step(decode_writes({key: item for key, item in value.items() if key != "http"}),
                decode_request(value["http"]))


def decode_step(value: object, dispatched: tuple[Step, ...]) -> Evaluation:
    """Return what follows the dispatched steps, after requiring replay to reproduce them.

    Each run restarts from main, so a replayed call was already sent. Bend is
    pure and the interpreter is pinned, so a difference here means the trusted
    evaluator misbehaved and nothing it computed afterward may be dispatched.
    """
    if not isinstance(value, dict):
        raise ContractError("invalid primitive IO frame")
    frame = dict(value)
    completed = frame.pop("completed", [])
    if not isinstance(completed, list) or any(not isinstance(item, dict) or "http" not in item for item in completed):
        raise ContractError("invalid primitive replay")
    if tuple(decode_call(item) for item in completed) != dispatched:
        raise ContractError("replay differs from dispatched effects")
    return decode_call(frame) if "http" in frame else decode_writes(frame)
