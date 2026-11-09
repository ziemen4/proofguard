"""Strict serialized boundaries for owner contracts and agent submissions.

Content identities use a versioned canonical JSON encoding of these structures.
Source text is retained exactly: normalizing whitespace could change a program.
"""

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import PurePosixPath
import re


class ContractError(ValueError):
    """Invalid, incomplete, or substituted contract/candidate data."""


def canonical(value: object) -> bytes:
    """Serialize identities and records using the versioned canonical JSON profile."""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ContractError("value cannot be encoded as canonical JSON") from exc


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _unique(pairs):
    """Reject duplicate object keys before constructing decoded boundary values."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode(data: bytes) -> object:
    """Decode JSON while rejecting duplicate keys and non-finite numbers."""
    try:
        return json.loads(data, object_pairs_hook=_unique,
                          parse_float=_finite_float,
                          parse_constant=lambda value: _invalid_constant(value))
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("invalid UTF-8 JSON") from exc


def _invalid_constant(value):
    raise ContractError(f"non-JSON numeric constant: {value}")


def _finite_float(value):
    """Reject decimal encodings that overflow to non-finite floating-point values."""
    number = float(value)
    if not math.isfinite(number):
        raise ContractError("JSON number exceeds finite range")
    return number


def fields(value: object, expected: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ContractError(f"{label} must contain exactly: {', '.join(sorted(expected))}")
    return value


def text(value: object, label: str) -> str:
    """Require nonempty UTF-8 text without NUL at a serialized boundary."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ContractError(f"{label} must be nonempty text without NUL")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ContractError(f"{label} must be valid Unicode") from exc
    return value


def source_path(value: object) -> str:
    """Require a canonical relative Bend path without traversal or hidden segments."""
    value = text(value, "source path")
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value or "\\" in value
            or any(part in (".", "..") or part.startswith(".") for part in path.parts)
            or path.suffix != ".bend"):
        raise ContractError(f"not a canonical relative Bend path: {value}")
    return value


def names(value: object, label: str) -> tuple[str, ...]:
    """Validate a nonempty, distinct list of supported declaration names."""
    if not isinstance(value, list) or not value:
        raise ContractError(f"{label} must be a nonempty list")
    for name in value:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name):
            raise ContractError(f"invalid {label} name")
    if len(set(value)) != len(value):
        raise ContractError(f"duplicate {label}")
    return tuple(value)


def paths(value: object) -> tuple[str, ...]:
    """Validate an ordered source list and reject duplicate paths."""
    if not isinstance(value, list) or not value:
        raise ContractError("sources must be a nonempty ordered list")
    result = tuple(source_path(path) for path in value)
    if len(result) != len(set(result)):
        raise ContractError("duplicate source path")
    return result


def version(value: object) -> None:
    if type(value) is not int or value != 1:
        raise ContractError("unsupported schema version")


@dataclass(frozen=True)
class Project:
    name: str
    intent: str
    sources: tuple[str, ...]
    implementations: tuple[str, ...]
    proofs: tuple[str, ...]

    @classmethod
    def parse(cls, value: object) -> "Project":
        """Validate the contract manifest and disjoint implementation/proof slots."""
        row = fields(value, {"schema", "name", "intent", "sources", "implementations", "proofs"}, "contract")
        version(row["schema"])
        result = cls(text(row["name"], "name"), text(row["intent"], "intent"),
                     paths(row["sources"]), names(row["implementations"], "implementations"),
                     () if row["proofs"] == [] else names(row["proofs"], "proofs"))
        if set(result.implementations) & set(result.proofs):
            raise ContractError("implementation and proof slots must be distinct")
        return result

    def data(self) -> dict:
        return {"schema": 1, "name": self.name, "intent": self.intent,
                "sources": list(self.sources), "implementations": list(self.implementations),
                "proofs": list(self.proofs)}


@dataclass(frozen=True)
class Source:
    path: str
    content: str

    @classmethod
    def parse(cls, value: object) -> "Source":
        row = fields(value, {"path", "content"}, "source")
        return cls(source_path(row["path"]), text(row["content"], "source content"))

    def data(self) -> dict:
        return {"path": self.path, "content": self.content}


@dataclass(frozen=True)
class Bundle:
    project: Project
    sources: tuple[Source, ...]

    @classmethod
    def parse(cls, value: object) -> "Bundle":
        """Validate the contract's ordered source closure."""
        row = fields(value, {"schema", "project", "sources"}, "bundle")
        version(row["schema"])
        project = Project.parse(row["project"])
        if not isinstance(row["sources"], list):
            raise ContractError("bundle sources must be a list")
        sources = tuple(Source.parse(source) for source in row["sources"])
        if tuple(s.path for s in sources) != project.sources:
            raise ContractError("bundle sources do not match the contract's ordered closure")
        return cls(project, sources)

    def data(self) -> dict:
        return {"schema": 1, "project": self.project.data(),
                "sources": [source.data() for source in self.sources]}

    @property
    def identity(self) -> str:
        return digest(self.data())


def contract_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ContractError("contract ID must be a full SHA-256 digest")
    return value
