"""Optional owner configuration helper for the declarative Meijer package."""

import base64
from dataclasses import dataclass
from pathlib import Path

from bendspec.model import ContractError, Source, fields
from proofguard.context import PrivateBlob, PrivatePair, decode_snapshot, encode_owner_data
from proofguard.packages import Enrollment
from proofguard.plugin import Plugin

from .configuration import Guardrails, Input, Label, sequence
from .serialization import labels, render_policy

LAWS = ("scope_safe", "flow_safe", "data_safe", "tools_safe", "frame_safe",
        "preconditions_safe", "approval_safe", "budget_safe", "sequence_safe", "outcome_safe")
ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class MeijerPlugin:
    inputs: tuple[Input, ...]
    clearances: tuple[Label, ...]
    context_label: Label = Label.PUBLIC
    guardrails: Guardrails | None = None

    def __post_init__(self):
        if (type(self.inputs) is not tuple or not 1 <= len(self.inputs) <= 16
                or any(type(item) is not Input for item in self.inputs)
                or type(self.clearances) is not tuple or not 1 <= len(self.clearances) <= 16
                or any(not isinstance(item, Label) for item in self.clearances)
                or not isinstance(self.context_label, Label)):
            raise ContractError("owner must configure 1..16 immutable inputs and sink classifications")
        if self.guardrails is None:
            object.__setattr__(self, "guardrails", Guardrails.defaults(len(self.clearances)))
        if type(self.guardrails) is not Guardrails:
            raise ContractError("typed owner guardrails required")
        self.guardrails.validate_shape(len(self.clearances), len(self.inputs))

    @classmethod
    def from_config(cls, value: object) -> "MeijerPlugin":
        """Decode owner setup; candidates have no configuration channel."""
        keys = {"inputs", "clearances", "context_label"}
        if type(value) is dict and "guardrails" in value:
            keys.add("guardrails")
        row = fields(value, keys, "Meijer configuration")
        inputs = []
        for value in sequence(row["inputs"], 1, 16, "inputs"):
            item = fields(value, {"data_base64", "label"}, "Meijer input")
            try:
                label = Label(item["label"])
            except (ValueError, TypeError) as error:
                raise ContractError("invalid input classification") from error
            inputs.append(Input(decode_snapshot(item["data_base64"], 65536), label))
        try:
            clearances = tuple(Label(item) for item in sequence(row["clearances"], 1, 16, "clearances"))
            context = Label(row["context_label"])
        except (ValueError, TypeError) as error:
            raise ContractError("invalid owner classification") from error
        policy = Guardrails.from_config(row["guardrails"], len(clearances), len(inputs)) if "guardrails" in row else None
        return cls(tuple(inputs), clearances, context, policy)

    def bindings(self) -> tuple[Source, ...]:
        """Render owner classifiers and policy as private Bend definitions."""
        classifications = ("def input_labels() -> Labels:\n  " + labels(tuple(item.label for item in self.inputs))
                           + "\n\ndef sink_labels() -> Labels:\n  " + labels(self.clearances)
                           + "\n\ndef context_label() -> Label:\n  " + self.context_label.value + "{}\n")
        return (Source("owner/classifications.bend", classifications),
                Source("owner/policy.bend", render_policy(self.guardrails)))

    def load(self) -> Plugin:
        """Capture laws and private approval records for this owner's policy."""
        records = None
        for approval in reversed(self.guardrails.approvals):
            sink = None
            for _ in range(approval.sink):
                sink = PrivatePair(None, sink)
            records = PrivatePair(PrivatePair(sink, PrivateBlob(approval.payload)), records)
        return Plugin.load(ROOT, bindings=self.bindings()).with_owner_data(encode_owner_data(records))

    def _arguments(self, session: object) -> dict:
        """Encode application inputs and check sink counts."""
        row = fields(session, {"identifier", "expires", "destinations"}, "Meijer session")
        if type(row["destinations"]) is not list or len(row["destinations"]) != len(self.clearances):
            raise ContractError("sink classifications must match enrolled destinations")
        return {"inputs_base64": [base64.b64encode(item.data).decode() for item in self.inputs], "session": row}

    def enrollment_config(self, session: object) -> dict:
        """Use the foundation's enrollment format with this owner's settings."""
        return self.load().enrollment_config(**self._arguments(session))

    def enroll(self, authority: Path, session: object) -> Enrollment:
        """Activate through the same foundation interface as other plugins."""
        return self.load().enroll(authority, **self._arguments(session))
