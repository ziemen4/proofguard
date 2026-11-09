"""Declarative packages use the same capture, enrollment and execution boundary."""

import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from bendspec.model import ContractError, Source
from proofguard.admission import AdmissionRejected
from proofguard.packages import Enrollment
from proofguard.plugin import Plugin

ROOT = Path(__file__).resolve().parents[2]
GOOD = "def main(inputs):\n  pg_write(PGZero{}, pg_input(PGZero{}, inputs))\n"


class PluginPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "package"
        self.package.mkdir()
        self.manifest = {"schema": 1, "name": "selected-report", "intent": "Publish selected bytes",
                         "sources": ["settings.bend", "checks.bend", "runner.bend"],
                         "bindings": ["settings.bend"],
                         "laws": ["owner_rule"], "entrypoint": "dispatch"}
        self.write_manifest()
        (self.package / "checks.bend").write_text(
            "law owner_rule:\n  {main == expected : PGInputs -> PGIO}\n")
        (self.package / "runner.bend").write_text(
            "def dispatch(inputs: PGInputs, owner: PGOwnerContext) -> PGIO:\n"
            "  main(inputs)\n")
        self.binding = Source("settings.bend", "def expected(inputs: PGInputs) -> PGIO:\n  pg_write(PGZero{}, pg_input(PGZero{}, inputs))\n")
        self.owner = self.root / "owner"
        self.owner.mkdir()
        self.target = self.root / "result"
        self.session = {"identifier": "manifest-runner", "expires": int(time.time()) + 240,
                        "destinations": [str(self.target)]}

    def write_manifest(self):
        self.package.joinpath("plugin.json").write_text(json.dumps(self.manifest))

    def load(self):
        return Plugin.load(self.package, bindings=(self.binding,))

    def test_manifest_owns_private_source_order_and_runner(self):
        plugin = self.load()
        self.assertEqual(plugin.contract.project.sources,
                         ("proofguard/effects.bend", "proofguard/program.bend", *self.manifest["sources"]))
        self.assertEqual(plugin.contract.project.implementations, ("main",))
        self.assertFalse(hasattr(plugin, 'public_sources'))
        self.assertTrue(plugin.entrypoint.endswith("_dispatch"))
        self.assertIn(self.binding.path, plugin.contract.project.sources)

    def test_owner_context_is_optional_even_for_explicit_packages(self):
        plugin = self.load()
        self.assertNotIn("owner_data", plugin.enrollment_config(inputs_base64=[], session=self.session))
        self.assertIsNone(plugin.with_owner_data(None).get_selection()["owner_data"])

    def test_capture_and_bindings_have_source_bound_identities(self):
        plugin = self.load()
        identity = plugin.contract.identity
        changed = Source("settings.bend", self.binding.content.replace("pg_write(PGZero{}, pg_input(PGZero{}, inputs))", "PGAbort{}"))
        self.assertNotEqual(identity, Plugin.load(self.package, bindings=(changed,)).contract.identity)
        (self.package / "checks.bend").write_text("law owner_rule:\n  {0 == 0 : U32}\n")
        self.assertEqual(plugin.contract.identity, identity)
        self.assertNotEqual(self.load().contract.identity, identity)

    def test_missing_extra_or_duplicate_bindings_reject(self):
        for bindings in ((), (self.binding, self.binding),
                         (Source("other.bend", self.binding.content),), [self.binding]):
            with self.subTest(bindings=bindings), self.assertRaises(ContractError):
                Plugin.load(self.package, bindings=bindings)

    def test_export_selectors_custom_slots_and_reserved_paths_reject(self):
        for changes in ({"libraries": ["settings.bend"]},
                        {"sources": ["proofguard/effects.bend"]},
                        {"sources": ["proofguard/program.bend"]},
                        {"bindings": ["../settings.bend"]},
                        {"libraries": ["missing.bend"]},
                        {"implementations": ["owner_rule"]}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                original = self.manifest
                try:
                    self.manifest = dict(original, **changes)
                    self.write_manifest()
                    self.load()
                finally:
                    self.manifest = original
                    self.write_manifest()

    def test_combined_file_and_binding_size_is_bounded(self):
        binding = Source("settings.bend", "x" * (1024 * 1024))
        with self.assertRaises(ContractError):
            Plugin.load(self.package, bindings=(binding,))

    def test_source_symlinks_reject(self):
        path = self.package / "runner.bend"
        saved = self.root / "runner.bend"
        path.rename(saved)
        path.symlink_to(saved)
        with self.assertRaises(ContractError):
            self.load()

    def test_package_without_bindings_rejects_extra_owner_sources(self):
        with self.assertRaises(ContractError):
            Plugin.load(ROOT / "examples/foundation/report", bindings=(self.binding,))

    def test_generic_enrollment_checks_law_then_runs_frozen_entrypoint(self):
        selected = self.load().with_owner_data(None).enroll(
            self.owner, inputs_base64=[base64.b64encode(b"report").decode()], session=self.session)
        exported = json.dumps(selected.get_task(self.owner))
        for private in ("settings.bend", "runner.bend", "owner_rule", "expected"):
            self.assertNotIn(private, exported)
        gateway = selected.build_gateway(self.owner)
        with self.assertRaises(AdmissionRejected):
            gateway.submit("def main(inputs):\n  PGAbort{}\n")
        self.assertFalse(self.target.exists())
        restored = Enrollment.load(self.owner, selected.identity)
        restored.build_gateway(self.owner).submit(GOOD)
        self.assertEqual(self.target.read_bytes(), b"report")

    def test_agent_helper_can_share_a_private_plugin_name_without_changing_it(self):
        selected = self.load().with_owner_data(None).enroll(
            self.owner, inputs_base64=[base64.b64encode(b"report").decode()], session=self.session)
        gateway = selected.build_gateway(self.owner)
        gateway.submit(GOOD + "\ndef expected(inputs: PGInputs) -> PGIO:\n  PGAbort{}\n")
        self.assertEqual(self.target.read_bytes(), b"report")
        self.target.unlink()
        with self.assertRaises(AdmissionRejected):
            gateway.submit("def expected(inputs: PGInputs) -> PGIO:\n  PGDone{}\n\ndef main(inputs):\n  expected(inputs)\n")
        self.assertFalse(self.target.exists())

    def test_enrollment_rejects_removed_export_configuration(self):
        configuration = self.load().enrollment_config(inputs_base64=[], session=self.session)
        with self.assertRaises(ContractError):
            Enrollment.create(dict(configuration, public_sources=['settings.bend']), self.owner)

    def test_manifest_requires_an_integer_format_tag(self):
        for schema in (True, "1", None):
            self.manifest['schema'] = schema
            self.write_manifest()
            with self.subTest(schema=schema), self.assertRaisesRegex(ContractError, 'invalid plugin schema'):
                self.load()

    def test_private_wrapper_is_not_exposed_as_agent_entrypoint(self):
        selected = self.load().with_owner_data(None).enroll(self.owner, inputs_base64=[], session=self.session)
        task = selected.get_task(self.owner)
        self.assertEqual(task['schema'], 1)
        self.assertEqual(task['entrypoint'], 'main')
        self.assertNotIn('def dispatch', json.dumps(task))
        self.assertNotIn('libraries', task)

    def test_cli_accepts_explicit_package_and_private_bindings(self):
        config = self.root / "session.json"
        config.write_text(json.dumps({"schema": 1, "plugins": [{"path": str(self.package),
                                      "bindings": [self.binding.data()], "owner_data": None}],
                                      "inputs_base64": [], "session": self.session}))
        result = subprocess.run([sys.executable, "-I", str(ROOT / "proofguard/cli.py"),
                                 "enroll", "--authority", str(self.owner), "--config", str(config)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = Enrollment.load(self.owner, json.loads(result.stdout)["enrollment_id"])
        self.assertIn("owner_data", selected.record["plugins"][0])
        selected.build_gateway(self.owner).submit(GOOD)
        self.assertEqual(self.target.read_bytes(), b"")
