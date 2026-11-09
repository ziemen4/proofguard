"""Approved source snapshots remain independent of mutable owner project files."""

from pathlib import Path
import tempfile
import unittest

from bendspec.backend import Toolchain, verify
from bendspec.model import Source
from bendspec.workspace import Authority
from fixtures import capture_project, write_identity_contract


class WorkspaceTests(unittest.TestCase):
    def test_source_changes_require_a_new_owner_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            write_identity_contract(project)
            authority = Authority(Path(directory) / "authority")
            bundle = capture_project(project)
            identity = authority.approve(bundle)
            toolchain = Toolchain.installed()
            source = "def implement(n):\n  n\n\ndef refines(n):\n  {==}\n"
            candidate = (Source("candidate/code.bend", source),)
            self.assertEqual(verify(authority.load(identity), candidate, toolchain)["status"], "accepted")
            spec = project / "spec/system.bend"
            spec.write_text(spec.read_text().replace("  n\n", "  Next{n}\n"))
            self.assertEqual(authority.load(identity), bundle)
            revised = capture_project(project)
            new_identity = authority.approve(revised)
            self.assertNotEqual(new_identity, identity)
            self.assertEqual(verify(authority.load(new_identity), candidate, toolchain)["status"], "rejected")
            repaired = (Source("candidate/code.bend", source.replace("  n\n", "  Next{n}\n")),)
            self.assertEqual(verify(authority.load(new_identity), repaired, toolchain)["status"], "accepted")
