"""Owner configuration types; candidate analysis and execution remain in Bend."""

from dataclasses import dataclass
from enum import Enum

from bendspec.model import ContractError, fields
from proofguard.context import decode_snapshot


class Label(Enum):
    PUBLIC = "Public"
    PRIVATE = "Private"


class Operation(Enum):
    INPUT = "input"
    WRITE = "write"


def bounded(value: object, lower: int, upper: int, name: str) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ContractError(f"{name} must be an integer in {lower}..{upper}")
    return value


def sequence(value: object, lower: int, upper: int, name: str) -> list:
    if type(value) is not list or not lower <= len(value) <= upper:
        raise ContractError(f"{name} must be a list of {lower}..{upper} entries")
    return value


def indexes(value: object, size: int, name: str) -> frozenset[int]:
    items = sequence(value, 0, size, name)
    result = frozenset(bounded(item, 0, size - 1, name) for item in items)
    if len(result) != len(items):
        raise ContractError(f"duplicate {name}")
    return result


@dataclass(frozen=True)
class Input:
    data: bytes
    label: Label

    def __post_init__(self):
        if not isinstance(self.data, bytes) or len(self.data) > 65536 or not isinstance(self.label, Label):
            raise ContractError("invalid owner input/classification")


@dataclass(frozen=True)
class AnyPayload:
    pass


@dataclass(frozen=True)
class NonEmpty:
    pass


@dataclass(frozen=True)
class EqualInput:
    index: int


@dataclass(frozen=True)
class AllOf:
    conditions: tuple["Condition", ...]


Condition = AnyPayload | NonEmpty | EqualInput | AllOf


def validate_condition(value: Condition, inputs: int, depth: int = 0) -> None:
    """Bound owner condition syntax without evaluating any payload predicate."""
    if depth > 4:
        raise ContractError("excessively nested precondition")
    match value:
        case AnyPayload() | NonEmpty():
            return
        case EqualInput(index):
            bounded(index, 0, inputs - 1, "precondition input")
        case AllOf(children):
            if type(children) is not tuple or not 1 <= len(children) <= 4:
                raise ContractError("preconditions require 1..4 immutable children")
            for child in children:
                validate_condition(child, inputs, depth + 1)
        case _:
            raise ContractError("unsupported precondition")


def parse_condition(value: object, inputs: int, depth: int = 0) -> Condition:
    """Decode the finite owner condition grammar; reject extra fields."""
    if depth > 4 or type(value) is not dict:
        raise ContractError("invalid or excessively nested precondition")
    match value.get("kind"):
        case "any" | "nonempty" as kind:
            fields(value, {"kind"}, "precondition")
            return AnyPayload() if kind == "any" else NonEmpty()
        case "equals_input":
            row = fields(value, {"kind", "input"}, "precondition")
            return EqualInput(bounded(row["input"], 0, inputs - 1, "precondition input"))
        case "all":
            row = fields(value, {"kind", "conditions"}, "precondition")
            return AllOf(tuple(parse_condition(child, inputs, depth + 1) for child in
                               sequence(row["conditions"], 1, 4, "preconditions")))
        case _:
            raise ContractError("unsupported precondition")


@dataclass(frozen=True)
class Approval:
    sink: int
    payload: bytes

    def __post_init__(self):
        bounded(self.sink, 0, 15, "approval sink")
        if not isinstance(self.payload, bytes) or len(self.payload) > 65536:
            raise ContractError("approval requires bounded exact payload bytes")


@dataclass(frozen=True)
class Automaton:
    initial: int
    accepting: frozenset[int]
    transitions: tuple[tuple[int | None, ...], ...]

    def __post_init__(self):
        if type(self.transitions) is not tuple or not 1 <= len(self.transitions) <= 8:
            raise ContractError("automaton requires 1..8 immutable states")
        bounded(self.initial, 0, len(self.transitions) - 1, "initial state")
        if type(self.accepting) is not frozenset:
            raise ContractError("immutable accepting states required")
        for state in self.accepting:
            bounded(state, 0, len(self.transitions) - 1, "accepting state")
        width = len(self.transitions[0]) if type(self.transitions[0]) is tuple else 0
        if not 1 <= width <= 16:
            raise ContractError("automaton requires 1..16 sinks")
        for row in self.transitions:
            if type(row) is not tuple or len(row) != width:
                raise ContractError("automaton row arity mismatch")
            for target in row:
                if target is not None:
                    bounded(target, 0, len(self.transitions) - 1, "target state")


@dataclass(frozen=True)
class Guardrails:
    operations: frozenset[Operation]
    writable_sinks: frozenset[int]
    preconditions: tuple[Condition, ...]
    max_writes: int
    max_write_bytes: int
    max_total_bytes: int
    approval_required: frozenset[int]
    approvals: tuple[Approval, ...]
    automata: tuple[Automaton, ...]

    @classmethod
    def defaults(cls, sinks: int) -> "Guardrails":
        bounded(sinks, 1, 16, "sink count")
        return cls(frozenset(Operation), frozenset(range(sinks)), (AnyPayload(),) * sinks,
                   16, 65536, 1048576, frozenset(), (), ())

    def validate_shape(self, sinks: int, inputs: int) -> None:
        """Validate owner metadata before rendering; never accept a candidate here."""
        if type(self.operations) is not frozenset or any(not isinstance(op, Operation) for op in self.operations):
            raise ContractError("immutable supported operations required")
        for values, name in ((self.writable_sinks, "writable sinks"), (self.approval_required, "approval sinks")):
            if type(values) is not frozenset:
                raise ContractError(f"immutable {name} required")
            for value in values:
                bounded(value, 0, sinks - 1, name)
        if type(self.preconditions) is not tuple or len(self.preconditions) != sinks:
            raise ContractError("preconditions must match enrolled sink count")
        for value in self.preconditions:
            validate_condition(value, inputs)
        bounded(self.max_writes, 0, 16, "write budget")
        bounded(self.max_write_bytes, 0, 65536, "per-write byte budget")
        bounded(self.max_total_bytes, 0, 1048576, "total byte budget")
        if (type(self.approvals) is not tuple or len(self.approvals) > 64
                or any(type(item) is not Approval for item in self.approvals)):
            raise ContractError("at most 64 immutable exact approvals required")
        if len(set(self.approvals)) != len(self.approvals):
            raise ContractError("duplicate approvals")
        for item in self.approvals:
            bounded(item.sink, 0, sinks - 1, "approval sink")
        if (type(self.automata) is not tuple or len(self.automata) > 4
                or any(type(item) is not Automaton for item in self.automata)):
            raise ContractError("at most four immutable automata required")
        if any(len(machine.transitions[0]) != sinks for machine in self.automata):
            raise ContractError("automaton must match enrolled sink count")

    @classmethod
    def from_config(cls, value: object, sinks: int, inputs: int) -> "Guardrails":
        """Parse owner policy only; raw approved bytes require explicit enrollment."""
        row = fields(value, {"operations", "writable_sinks", "preconditions", "max_writes",
                             "max_write_bytes", "max_total_bytes", "approval_required",
                             "approvals", "automata"}, "Meijer guardrails")
        ops = sequence(row["operations"], 0, len(Operation), "operations")
        try:
            operations = frozenset(Operation(item) for item in ops)
        except (ValueError, TypeError) as error:
            raise ContractError("unsupported operation") from error
        if len(operations) != len(ops):
            raise ContractError("duplicate operations")
        approvals = []
        for value in sequence(row["approvals"], 0, 64, "approvals"):
            item = fields(value, {"sink", "payload_base64"}, "exact approval")
            approvals.append(Approval(bounded(item["sink"], 0, sinks - 1, "approval sink"),
                                      decode_snapshot(item["payload_base64"], 65536)))
        automata = []
        for value in sequence(row["automata"], 0, 4, "automata"):
            item = fields(value, {"initial", "accepting", "transitions"}, "automaton")
            rows = sequence(item["transitions"], 1, 8, "automaton states")
            matrix = tuple(tuple(None if target is None else bounded(target, 0, len(rows) - 1, "target state")
                                 for target in sequence(edge, sinks, sinks, "transition row")) for edge in rows)
            automata.append(Automaton(bounded(item["initial"], 0, len(rows) - 1, "initial state"),
                                      indexes(item["accepting"], len(rows), "accepting states"), matrix))
        result = cls(operations, indexes(row["writable_sinks"], sinks, "writable sinks"),
                     tuple(parse_condition(item, inputs) for item in
                           sequence(row["preconditions"], sinks, sinks, "preconditions")),
                     row["max_writes"], row["max_write_bytes"], row["max_total_bytes"],
                     indexes(row["approval_required"], sinks, "approval destinations"),
                     tuple(approvals), tuple(automata))
        result.validate_shape(sinks, inputs)
        return result
