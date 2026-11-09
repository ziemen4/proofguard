"""Actual namespace probes, with real gated execution as the positive control."""

import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from bendspec.model import ContractError
from proofguard.plugin import Plugin
from proofguard.sandbox import AuthorSandbox
from proofguard.worker import OutboxWorker, Status

ROOT = Path(__file__).resolve().parents[2]
GOOD = "def main(inputs):\n  PGWrite{PGZero{}, pg_input(PGZero{}, inputs), PGDone{}}\n"


class SandboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.owner = self.root / "owner"
        self.owner.mkdir()
        self.target = self.root / "protected-output"
        self.target.write_bytes(b"original")
        self.canary = self.owner / "credential"
        self.canary.write_text("SYNTHETIC_OWNER_ONLY")
        selected = Plugin.load(ROOT / "tests/fixtures/selection").enroll(
            self.owner, inputs_base64=[base64.b64encode(b"BEND_APPROVED").decode()],
            session={"identifier": "sandbox", "expires": int(time.time()) + 180,
                     "destinations": [str(self.target)]})
        self.worker = OutboxWorker(selected.build_gateway(self.owner), self.root / "outbox", self.root / "results")
        self.runtime, self.task = self.root / "runtime", self.root / "task"
        self.runtime.mkdir()
        self.task.mkdir()
        (self.task / "task.json").write_text(json.dumps(selected.get_task(self.owner)))
        self.sandbox = AuthorSandbox(self.worker, runtime=self.runtime, task=self.task)

    def probe(self, source):
        result = self.sandbox.run(["/usr/bin/python3", "-I", "-c", source])
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_owner_state_environment_and_host_processes_are_absent(self):
        source = f"""
import os, pathlib, json
p = pathlib.Path({str(self.canary)!r})
print(json.dumps({{'canary':p.exists(), 'env':os.getenv('PROOFGUARD_TEST_CREDENTIAL'),
 'home':pathlib.Path({str(Path.home())!r}).exists(),
 'pid_namespace':os.readlink('/proc/self/ns/pid'),
 'owner_root':pathlib.Path('/proc/{os.getpid()}/root{self.canary}').exists()}}))
"""
        with patch.dict(os.environ, {"PROOFGUARD_TEST_CREDENTIAL": "SYNTHETIC_ENV_ONLY"}):
            result = self.probe(source)
        self.assertNotEqual(result.pop("pid_namespace"), os.readlink('/proc/self/ns/pid'))
        self.assertEqual(result, {"canary": False, "env": None, "home": False, "owner_root": False})
        self.assertEqual(self.canary.read_text(), "SYNTHETIC_OWNER_ONLY")

    def test_protected_target_and_public_mounts_cannot_be_modified(self):
        (self.worker.results / "existing.json").write_text("owner result")
        source = f"""
from pathlib import Path
import json
outcomes = []
for name in [{str(self.target)!r}, '/task/task.json', '/results/existing.json', '/runtime/injected.py']:
 try:
  Path(name).write_text('unverified')
  outcomes.append('written')
 except OSError:
  outcomes.append('denied')
print(json.dumps(outcomes))
"""
        self.assertEqual(self.probe(source), ["denied"] * 4)
        self.assertEqual(self.target.read_bytes(), b"original")
        self.assertEqual((self.worker.results / "existing.json").read_text(), "owner result")
        self.assertFalse((self.runtime / "injected.py").exists())

    def test_private_scratch_space_is_capped(self):
        source = """
import errno, json, os
outcomes = {}
for directory, limit in (('/tmp', 256 << 20), ('/home', 64 << 20), ('/run', 8 << 20)):
 written = 0
 try:
  with open(os.path.join(directory, 'fill'), 'wb') as output:
   block = bytes(1 << 20)
   while written <= limit:
    output.write(block)
    output.flush()
    written += len(block)
  outcomes[directory] = 'unbounded'
 except OSError as error:
  outcomes[directory] = 'full' if error.errno == errno.ENOSPC and written <= limit else 'other'
 os.remove(os.path.join(directory, 'fill'))
print(json.dumps(outcomes))
"""
        self.assertEqual(self.probe(source), {"/tmp": "full", "/home": "full", "/run": "full"})

    def test_host_loopback_network_is_unreachable(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            # Positive control proves the same host listener really is reachable.
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                connection, _ = server.accept()
                connection.close()
            source = f"""
import json, socket
try:
 s=socket.create_connection(('127.0.0.1',{port}),timeout=1)
 s.sendall(b'unverified')
 s.close()
 print(json.dumps('connected'))
except OSError:
 print(json.dumps('denied'))
"""
            self.assertEqual(self.probe(source), "denied")
            server.settimeout(0.1)
            with self.assertRaises(TimeoutError):
                server.accept()

    def test_unverified_submission_is_inert_then_bend_executes_outside_sandbox(self):
        proposal = uuid.uuid4().hex
        source = f"""
import json, os
from pathlib import Path
temporary=Path('/outbox/.{proposal}.upload')
temporary.write_text({GOOD!r})
os.rename(temporary, '/outbox/{proposal}.bend')
print(json.dumps({{'task':Path('/task/task.json').exists(), 'proposal':{proposal!r}}}))
"""
        self.assertEqual(self.probe(source), {"task": True, "proposal": proposal})
        self.assertEqual(self.target.read_bytes(), b"original")
        self.assertEqual(self.worker.poll()[0].status, Status.COMPLETED)
        self.assertEqual(self.target.read_bytes(), b"BEND_APPROVED")
        self.assertEqual(self.probe(f"import pathlib,json; print(pathlib.Path('/results/{proposal}.json').read_text())")["status"], "completed")

    def test_python_outbox_source_cannot_become_an_effect(self):
        proposal = uuid.uuid4().hex
        source = f"""
from pathlib import Path
import json
Path('/outbox/{proposal}.bend').write_text("open('protected-output','w').write('unverified')")
print(json.dumps('submitted'))
"""
        self.assertEqual(self.probe(source), "submitted")
        self.assertEqual(self.worker.poll()[0].status, Status.REJECTED)
        self.assertEqual(self.target.read_bytes(), b"original")

    def test_owner_authority_and_ipc_endpoints_cannot_be_public_mounts(self):
        with self.assertRaises(ContractError):
            AuthorSandbox(self.worker, runtime=self.owner, task=self.task)
        with self.assertRaises(ContractError):
            AuthorSandbox(self.worker, runtime=self.runtime, task=self.worker.outbox)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(self.runtime / "host.sock"))
            with self.assertRaises(ContractError):
                AuthorSandbox(self.worker, runtime=self.runtime, task=self.task)

    def test_fixed_system_mount_cannot_expose_protected_paths(self):
        # /usr is read-only, but would still disclose enrolled private data.
        for attribute in ("authority", "session"):
            value = (Path("/usr/local/private-owner") if attribute == "authority" else
                     replace(self.worker.gateway.session, destinations=(Path("/usr/local/protected-output"),)))
            with self.subTest(attribute=attribute), patch.object(self.worker.gateway, attribute, value):
                with self.assertRaisesRegex(ContractError, "fixed /usr mount"):
                    AuthorSandbox(self.worker, runtime=self.runtime, task=self.task)
        for attribute in ("outbox", "results"):
            with self.subTest(attribute=attribute), patch.object(self.worker, attribute, Path("/usr/local/proposals")):
                with self.assertRaisesRegex(ContractError, "fixed /usr mount"):
                    AuthorSandbox(self.worker, runtime=self.runtime, task=self.task)

    def test_timeout_terminates_the_sandbox_and_its_children(self):
        child = """
import time
with open('/outbox/heartbeat', 'ab', buffering=0) as output:
 while True:
  output.write(b'x')
  time.sleep(.02)
"""
        source = f"""
import subprocess, sys, time
subprocess.Popen([sys.executable, '-I', '-c', {child!r}])
time.sleep(30)
"""
        with self.assertRaises(subprocess.TimeoutExpired):
            self.sandbox.run(["/usr/bin/python3", "-I", "-c", source], timeout=2)
        heartbeat = self.worker.outbox / "heartbeat"
        self.assertTrue(heartbeat.exists(), "sandbox child must actually start")
        time.sleep(0.2)
        size = heartbeat.stat().st_size
        self.assertGreater(size, 0)
        time.sleep(0.2)
        self.assertEqual(heartbeat.stat().st_size, size)
