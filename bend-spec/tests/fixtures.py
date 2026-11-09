"""Small source fixtures for real checker tests."""

import json

from bendspec.model import Bundle, Project, decode
from bendspec.workspace import capture, read_regular


def capture_project(root):
    """Capture a contract manifest and its ordered source files."""
    project = Project.parse(decode(read_regular(root, "contract.json")))
    return Bundle(project, capture(root, project.sources))


def write_identity_contract(root):
    """Write the quantified identity contract used by mutation/rejection tests."""
    (root / "spec").mkdir(parents=True)
    (root / "contract.json").write_text(json.dumps({
        "schema": 1, "name": "identity", "intent": "Preserve a natural number",
        "sources": ["spec/types.bend", "spec/system.bend"],
        "implementations": ["implement"], "proofs": ["refines"],
    }))
    (root / "spec/types.bend").write_text("type Natural is Data:\n  Zero{}\n  Next{pred: Natural}\n")
    (root / "spec/system.bend").write_text("""def reference(n: Natural) -> Natural:
  n

law implement:
  for n: Natural
  Natural

law refines:
  for +n: Natural
  {implement(n) == reference(n) : Natural}
""")
