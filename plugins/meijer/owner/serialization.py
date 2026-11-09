"""Serialize owner configuration into frozen Bend values, without evaluating plans."""

from .configuration import AllOf, AnyPayload, EqualInput, Guardrails, Label, NonEmpty, Operation


def constructor(name: str, *arguments: str) -> str:
    return name + "{" + ", ".join(arguments) + "}"


def linked(values, item: str, end: str) -> str:
    result = constructor(end)
    for value in reversed(tuple(values)):
        result = constructor(item, value, result)
    return result


def index(value: int) -> str:
    return "PGNext{" * value + "PGZero{}" + "}" * value


def count(value: int) -> str:
    return "MoreCount{" * value + "ZeroCount{}" + "}" * value


def natural(value: int) -> str:
    result = "mj_Zero{}"
    for bit in bin(value)[2:] if value else "":
        result = constructor("mj_Bit" + bit, result)
    return result


def decision(value: bool) -> str:
    return "Yes{}" if value else "No{}"


def condition(value) -> str:
    match value:
        case AnyPayload():
            return "AnyPayload{}"
        case NonEmpty():
            return "NonEmpty{}"
        case EqualInput(i):
            return constructor("EqualInput", index(i))
        case AllOf(children):
            result = "AnyPayload{}"
            for child in reversed(children):
                result = constructor("AllOf", condition(child), result)
            return result
    raise ValueError("unsupported owner condition")


def labels(values: tuple[Label, ...]) -> str:
    return linked((constructor(item.value) for item in values), "LabelItem", "EndLabels")


def render_policy(policy: Guardrails) -> str:
    """Expose static metadata, retaining approval payloads in private context."""
    lines = ["def operation_allowed(operation: Operation) -> Decision:", "  match operation:"]
    for operation in Operation:
        name = "Op" + "".join(part.title() for part in operation.value.split("_"))
        lines += [f"    case {name}{{}}:", "      " + decision(operation in policy.operations)]
    sinks = range(len(policy.preconditions))
    terms = {
        "writable_sinks": ("Decisions", linked((decision(i in policy.writable_sinks) for i in sinks), "DecisionItem", "EndDecisions")),
        "preconditions": ("Conditions", linked((condition(item) for item in policy.preconditions), "ConditionItem", "EndConditions")),
        "approval_required": ("Decisions", linked((decision(i in policy.approval_required) for i in sinks), "DecisionItem", "EndDecisions")),
        "approval_available": ("Decisions", linked((decision(i not in policy.approval_required or any(a.sink == i for a in policy.approvals)) for i in sinks), "DecisionItem", "EndDecisions")),
        "write_budget": ("Count", count(policy.max_writes)),
        "per_write_budget": ("mj_Natural", natural(policy.max_write_bytes)),
        "total_byte_budget": ("mj_Natural", natural(policy.max_total_bytes)),
        "concrete_write_budget": ("mj_Natural", natural(policy.max_writes)),
        "payload_limit": ("mj_Natural", natural(65536)),
        "byte_budget_restricted": ("Decision", decision(policy.max_write_bytes < 65536 or policy.max_total_bytes < 1048576)),
    }
    machines = []
    for machine in policy.automata:
        rows = linked((linked(("DeniedEdge{}" if target is None else constructor("NextEdge", index(target))
                               for target in row), "EdgeItem", "EndEdges") for row in machine.transitions),
                      "RowItem", "EndRows")
        accepting = linked((decision(i in machine.accepting) for i in range(len(machine.transitions))),
                           "DecisionItem", "EndDecisions")
        machines.append(constructor("Machine", index(machine.initial), accepting, rows))
    terms["security_machines"] = ("Machines", linked(machines, "MachineItem", "EndMachines"))
    for name, (kind, term) in terms.items():
        lines += ["", f"def {name}() -> {kind}:", "  " + term]
    return "\n".join(lines) + "\n"
