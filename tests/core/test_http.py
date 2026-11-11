"""HTTP effects cross the host boundary only under owner enrollment.

Every request goes to a loopback server this module starts, never the internet.
"""

import base64
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from bendspec.model import ContractError
from proofguard.effects import (EffectDenied, HttpFailure, HttpMethod, HttpRequest, HttpResponse, ProgramAborted,
                                Step, Write, decode_step)
from proofguard.gateway import EffectUncertain, ExecutionRefused, ExecutionStopped
from proofguard.http import (MAX_BODY_BYTES, AuthorizedCall, Endpoint, HttpTransport, is_permitted_address,
                             parse_endpoints)
from proofguard.plugin import Plugin
from proofguard.worker import OutboxWorker, Status, write_proposal
from tests.loopback import LoopbackServer

ROOT = Path(__file__).resolve().parents[2]
ZERO, ONE = "PGZero{}", "PGNext{PGZero{}}"
REPORT = '''def report(response: PGResponse) -> PGIO:
  match response:
    case PGResponse{status, body}: pg_write(PGZero{}, body)
    case PGHttpFailed{failure}: pg_write(PGZero{}, pg_text("failed"))

'''
STOP = '''def stop(response: PGResponse) -> PGIO:
  match response:
    case PGResponse{status, body}: PGAbort{}
    case PGHttpFailed{failure}: PGDone{}

'''


def get(url: str, then: str = "report") -> str:
    return f'PGHttp{{pg_get({ZERO}, pg_text("{url}")), {then}}}'


def request(url: bytes, method=HttpMethod.GET, headers=(), body=b"") -> HttpRequest:
    return HttpRequest(0, method, url, headers, body)


def get_endpoint(**settings) -> Endpoint:
    return Endpoint.parse({"origin": "https://api.example.com", "path_prefix": "/v1/",
                           "methods": ["GET", "POST"], **settings})


class EndpointTests(unittest.TestCase):
    def test_only_the_exact_origin_prefix_and_method_pass(self):
        target = get_endpoint().authorize(request(b"https://api.example.com/v1/items?q=1&r=%20"))
        self.assertEqual((target.host, target.port, target.secure, target.path),
                         ("api.example.com", 443, True, "/v1/items?q=1&r=%20"))
        for url in (b"http://api.example.com/v1/x", b"https://api.example.com:8443/v1/x",
                    b"https://other.example.com/v1/x", b"https://api.example.com.evil.test/v1/x",
                    b"https://api.example.com@evil.test/v1/x", b"https://user:pw@api.example.com/v1/x",
                    b"https://API.example.com/v1/x", b"https://api.example.com./v1/x",
                    b"https://api.example.com/v2/x", b"https://api.example.com/v1/../admin",
                    b"https://api.example.com/v1/%2e%2e/admin", b"https://api.example.com/v1/..%2fadmin",
                    b"https://api.example.com/v1/x%5cy", b"https://api.example.com/v1/x%zz",
                    b"https://api.example.com/v1/..;/admin", b"https://api.example.com/v1/.;/x",
                    b"https://api.example.com/v1/%252e%252e/admin", b"https://api.example.com/v1/x%00y",
                    b"https://api.example.com/v1/x%0d%0ay",
                    b"https://api.example.com/v1/x#fragment", b"https://api.example.com\\@evil.test/v1/x",
                    b"https://api.example.com/v1/x y", b"https://api.example.com/v1/\r\nHost: evil",
                    b"ftp://api.example.com/v1/x", b"//api.example.com/v1/x", b"/v1/x", b"", b"\xff"):
            with self.subTest(url=url), self.assertRaises(EffectDenied):
                get_endpoint().authorize(request(url))
        with self.assertRaises(EffectDenied):
            get_endpoint().authorize(request(b"https://api.example.com/v1/x", HttpMethod.DELETE))

    def test_programs_cannot_present_identity_or_oversized_requests(self):
        url = b"https://api.example.com/v1/x"
        target = get_endpoint().authorize(request(url, headers=((b"Content-Type", b"text/plain"),)))
        self.assertEqual(target.headers, (("content-type", "text/plain"),))
        for name in (b"Authorization", b"Cookie", b"Proxy-Authorization", b"X-Api-Key", b"Host",
                     b"Content-Length", b"Transfer-Encoding"):
            with self.subTest(header=name), self.assertRaises(EffectDenied):
                get_endpoint().authorize(request(url, headers=((name, b"value"),)))
        for headers in (((b"Accept", b"a\r\nCookie: b"),), ((b"Bad Name", b"a"),), ((b"Accept", b"\xff"),),
                        ((b"Accept", b"a"), (b"accept", b"b"))):
            with self.subTest(headers=headers), self.assertRaises(EffectDenied):
                get_endpoint().authorize(request(url, headers=headers))
        with self.assertRaises(EffectDenied):
            get_endpoint().authorize(request(b"https://api.example.com/v1/" + b"x" * 4096))
        with self.assertRaises(EffectDenied):
            get_endpoint().authorize(request(url, body=b"x" * (MAX_BODY_BYTES + 1)))

    def test_enrollment_syntax_is_closed_and_round_trips(self):
        endpoint = get_endpoint()
        self.assertEqual(Endpoint.parse(endpoint.data()), endpoint)
        self.assertEqual(parse_endpoints([]), ())
        for change in ({"origin": "https://api.example.com/path"}, {"origin": "https://*.example.com"},
                       {"origin": "ftp://api.example.com"}, {"origin": "https://user@api.example.com"},
                       {"path_prefix": "v1/"}, {"path_prefix": "/v1/../"}, {"methods": []}, {"methods": ["TRACE"]},
                       {"private_network": 1}, {"unknown": True},
                       {"credential": {"header": "Accept", "file": "/owner/token"}},
                       {"credential": {"header": "Authorization", "file": "relative"}}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                get_endpoint(**change)
        with self.assertRaises(ContractError):
            parse_endpoints([endpoint.data(), endpoint.data()])

    def test_private_addresses_need_a_grant_and_metadata_never_passes(self):
        for address in ("127.0.0.1", "10.0.0.8", "192.168.1.1", "::1", "::ffff:127.0.0.1", "fd00::1"):
            with self.subTest(address=address):
                self.assertFalse(is_permitted_address(address, False))
                self.assertTrue(is_permitted_address(address, True))
        for address in ("169.254.169.254", "fe80::1", "0.0.0.0", "::", "224.0.0.1", "240.0.0.1",
                        "::ffff:169.254.169.254"):
            with self.subTest(address=address):
                self.assertFalse(is_permitted_address(address, True))
        self.assertTrue(is_permitted_address("93.184.216.34", False))

    def test_replay_must_reproduce_every_dispatched_step(self):
        encoded = {"endpoint": 0, "method": "GET", "url_base64": "aHR0cA==", "headers": [], "body_base64": ""}
        sent = Step((), request(b"http"))
        replayed = {"schema": 1, "completed": [{"schema": 1, "writes": [], "http": encoded}], "writes": []}
        self.assertEqual(decode_step(replayed, (sent,)), ())
        with self.assertRaises(ContractError):
            decode_step(replayed, (Step((), request(b"other")),))
        with self.assertRaises(ContractError):
            decode_step({"schema": 1, "writes": []}, (sent,))
        with self.assertRaises(ProgramAborted):
            decode_step({"schema": 1, "abort": True}, ())
        self.assertEqual(decode_step({"schema": 1, "writes": [{"destination": 0, "bytes_base64": ""}]}, ()),
                         (Write(0, b""),))


class TransportTests(unittest.TestCase):
    def send(self, server, path, *, timeout=2.0, port=None, private_network=True, host="127.0.0.1"):
        call = AuthorizedCall(host, port or server.server.server_port, False, private_network, HttpMethod.GET, path,
                              (), b"")
        return HttpTransport(timeout=timeout).send(call)

    def test_redirects_are_returned_and_never_followed(self):
        with LoopbackServer() as target:
            def redirect(handler):
                handler.reply(302, b"moved", (("Location", target.origin + "/elsewhere"),))

            with LoopbackServer({"/moved": redirect}) as server:
                delivery = self.send(server, "/moved")
            self.assertEqual(delivery.result, HttpResponse(302, b"moved"))
            self.assertEqual(target.requests, [])

    def test_oversized_slow_and_broken_responses_become_failures(self):
        def oversized(handler):
            handler.reply(200, b"x" * (MAX_BODY_BYTES + 1))

        def endless(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", str(MAX_BODY_BYTES))
            handler.end_headers()
            try:
                for _ in range(200):
                    handler.wfile.write(b"x")
                    handler.wfile.flush()
                    time.sleep(0.02)
            except OSError:
                pass

        def truncated(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            handler.wfile.write(b"short")
            handler.close_connection = True

        def silent(handler):
            handler.close_connection = True

        with LoopbackServer({"/big": oversized, "/drip": endless, "/cut": truncated, "/silent": silent}) as server:
            self.assertEqual(self.send(server, "/big").result, HttpFailure.OVERSIZED)
            started = time.monotonic()
            drip = self.send(server, "/drip", timeout=0.4)
            self.assertEqual(drip.result, HttpFailure.TIMEOUT)
            self.assertTrue(drip.ambiguous)
            self.assertLess(time.monotonic() - started, 2)
            cut = self.send(server, "/cut")
            self.assertEqual(cut.result, HttpFailure.BROKEN)
            self.assertTrue(cut.ambiguous)
            self.assertEqual(self.send(server, "/silent").result, HttpFailure.BROKEN)

    def test_a_refused_connection_sends_nothing(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        delivery = self.send(None, "/", port=port)
        self.assertEqual((delivery.result, delivery.sent, delivery.ambiguous), (HttpFailure.UNREACHABLE, False, False))

    def test_blocked_addresses_are_never_contacted(self):
        with LoopbackServer() as server:
            delivery = self.send(server, "/", private_network=False)
            self.assertEqual((delivery.result, delivery.sent, delivery.blocked), (HttpFailure.UNREACHABLE, False, True))
            # A permitted name that resolves to a blocked address is the rebinding case.
            with patch("proofguard.http.socket.getaddrinfo",
                       return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]):
                rebound = self.send(server, "/", host="api.example.com", private_network=False)
            self.assertEqual((rebound.result, rebound.blocked), (HttpFailure.UNREACHABLE, True))
            self.assertEqual(server.requests, [])


@unittest.skipUnless(shutil.which("openssl"), "openssl is needed to issue a test certificate")
class TlsTests(unittest.TestCase):
    """TLS runs over a socket connected to a checked address, so verify it by name."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.certificate, self.key = self.root / "certificate.pem", self.root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                        "-keyout", str(self.key), "-out", str(self.certificate), "-days", "1",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                       check=True, capture_output=True, timeout=30)

    def serve(self, server):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.certificate, self.key)
        server.server.socket = context.wrap_socket(server.server.socket, server_side=True,
                                                   do_handshake_on_connect=False)
        # Refused handshakes are what these tests provoke; keep them off stderr.
        server.server.handle_error = lambda *arguments: None

    def send(self, server, host, *, trusted):
        transport = HttpTransport(timeout=2.0)
        if trusted:
            transport.context = ssl.create_default_context(cafile=str(self.certificate))
        port = server.server.server_port
        return transport.send(AuthorizedCall(host, port, True, True, HttpMethod.GET, "/x", (), b""))

    def test_a_trusted_certificate_answers_for_its_name_only(self):
        server = LoopbackServer()
        self.serve(server)
        with server:
            self.assertEqual(self.send(server, "localhost", trusted=True).result, HttpResponse(200, b"public answer"))
            # The address is the same, so only certificate checks refuse these.
            for host, trusted in (("127.0.0.1", True), ("localhost", False)):
                with self.subTest(host=host, trusted=trusted):
                    delivery = self.send(server, host, trusted=trusted)
                    self.assertEqual((delivery.result, delivery.sent), (HttpFailure.UNREACHABLE, False))
            self.assertEqual(len(server.requests), 1)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.owner = self.root / "owner"
        self.owner.mkdir()
        self.first, self.second = self.root / "first", self.root / "second"

    def enroll(self, endpoints, *, inputs=()):
        self.enrollment = Plugin.load(ROOT / "tests/fixtures/http").enroll(
            self.owner, inputs_base64=[base64.b64encode(item).decode() for item in inputs],
            session={"identifier": "http", "expires": int(time.time()) + 300,
                     "destinations": [str(self.first), str(self.second)], "endpoints": endpoints})
        return self.enrollment.build_gateway(self.owner)

    def records(self, gateway, kind):
        rows = [json.loads(path.read_text()) for path in gateway.audit.root.glob("*.json")]
        return [row for row in rows if row["kind"] == kind]

    def test_response_reaches_the_program_and_the_audit_records_the_call(self):
        with LoopbackServer() as server:
            gateway = self.enroll([server.endpoint()])
            receipt = gateway.submit(REPORT + f'def main(inputs):\n  {get(server.origin + "/v1/items?page=1")}\n')
            self.assertEqual(self.first.read_bytes(), b"public answer")
            self.assertEqual([(row.method, row.path) for row in server.requests], [("GET", "/v1/items?page=1")])
        self.assertEqual([call[2] for call in receipt.calls], ["200"])
        started, finished = self.records(gateway, "http_started"), self.records(gateway, "http_finished")
        self.assertEqual((len(started), len(finished)), (1, 1))
        self.assertEqual((finished[0]["outcome"], finished[0]["sent"], finished[0]["ambiguous"]), ("200", True, False))
        self.assertEqual(finished[0]["response_bytes"], len(b"public answer"))
        prepared = [row for row in self.records(gateway, "prepared_effects") if "call" in row]
        self.assertEqual(prepared[0]["call"]["request"], started[0]["request"])
        for path in gateway.audit.root.glob("*.json"):
            self.assertNotIn("public answer", path.read_text())

    def test_effects_dispatch_in_program_order(self):
        seen = []

        def observe(handler):
            seen.append(self.first.exists())
            handler.reply(200, b"done")

        with LoopbackServer({"/check": observe}) as server:
            gateway = self.enroll([server.endpoint()])
            gateway.submit(REPORT.replace("PGZero{}", ONE) + "def main(inputs):\n  "
                           f'PGWrite{{{ZERO}, pg_text("before"), {get(server.origin + "/check")}}}\n')
        self.assertEqual(seen, [True])
        self.assertEqual((self.first.read_bytes(), self.second.read_bytes()), (b"before", b"done"))

    def test_abort_stops_only_what_comes_after_a_call(self):
        late = 'def late(response: PGResponse) -> PGIO:\n  PGWrite{PGNext{PGZero{}}, pg_text("late"), PGAbort{}}\n\n'
        with LoopbackServer() as server:
            gateway = self.enroll([server.endpoint()])
            url = server.origin + "/step"
            with self.assertRaises(ExecutionStopped) as stopped:
                gateway.submit(late + f'def main(inputs):\n  PGWrite{{{ZERO}, pg_text("kept"), {get(url, "late")}}}\n')
            self.assertEqual(self.first.read_bytes(), b"kept")
            self.assertFalse(self.second.exists())
            self.assertEqual(len(stopped.exception.completed), 1)
            self.assertEqual(len(stopped.exception.calls), 1)
            self.assertIsInstance(stopped.exception.__cause__, ProgramAborted)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(len(self.records(gateway, "stopped")), 1)

    def test_abort_before_the_first_call_cancels_its_step(self):
        with LoopbackServer() as server:
            gateway = self.enroll([server.endpoint()])
            with self.assertRaises(ExecutionRefused):
                gateway.submit(f'def main(inputs):\n  PGWrite{{{ZERO}, pg_text("never"), PGAbort{{}}}}\n')
            self.assertFalse(self.first.exists())
            self.assertEqual(server.requests, [])

    def test_a_response_can_decide_abort_and_the_outcome_says_so(self):
        with LoopbackServer() as server:
            gateway = self.enroll([server.endpoint()])
            with self.assertRaises(ExecutionStopped):
                gateway.submit(STOP + f'def main(inputs):\n  {get(server.origin + "/x", "stop")}\n')

    def test_an_unsent_call_is_a_value_and_leaves_no_uncertainty(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        gateway = self.enroll([{"origin": f"http://127.0.0.1:{port}", "path_prefix": "/", "methods": ["GET"],
                                "private_network": True}])
        receipt = gateway.submit(REPORT + f'def main(inputs):\n  {get(f"http://127.0.0.1:{port}/x")}\n')
        self.assertEqual(self.first.read_bytes(), b"failed")
        self.assertEqual(receipt.calls[0][2], "unreachable")
        with self.assertRaises(ExecutionRefused):
            gateway.submit(STOP.replace("PGResponse{status, body}: PGAbort{}", "PGResponse{status, body}: PGDone{}")
                           .replace("PGHttpFailed{failure}: PGDone{}", "PGHttpFailed{failure}: PGAbort{}")
                           + f'def main(inputs):\n  {get(f"http://127.0.0.1:{port}/x", "stop")}\n')

    def test_a_timeout_reaches_the_program_and_stays_uncertain(self):
        def slow(handler):
            time.sleep(1)

        with LoopbackServer({"/slow": slow}) as server:
            gateway = self.enroll([server.endpoint()])
            gateway.transport = HttpTransport(timeout=0.3)
            with self.assertRaises(EffectUncertain) as uncertain:
                gateway.submit(REPORT + f'def main(inputs):\n  {get(server.origin + "/slow")}\n')
            self.assertEqual(self.first.read_bytes(), b"failed")
            self.assertEqual(uncertain.exception.calls[0][2], "timeout")
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(self.records(gateway, "http_finished")[0]["ambiguous"], True)
            self.assertEqual(len(self.records(gateway, "uncertain")), 1)

    def test_status_helpers_read_the_response(self):
        decide = '''def decide(ok: Bool) -> PGIO:
  match ok:
    case True{}: pg_write(PGZero{}, pg_text("good"))
    case False{}: pg_write(PGZero{}, pg_text("bad"))

def check(response: PGResponse) -> PGIO:
  decide(pg_http_ok(response))

'''
        def missing(handler):
            handler.reply(404, b"nothing")

        with LoopbackServer({"/missing": missing}) as server:
            gateway = self.enroll([server.endpoint()])
            gateway.submit(decide + f'def main(inputs):\n  {get(server.origin + "/x", "check")}\n')
            self.assertEqual(self.first.read_bytes(), b"good")
            gateway.submit(decide + f'def main(inputs):\n  {get(server.origin + "/missing", "check")}\n')
            self.assertEqual(self.first.read_bytes(), b"bad")

    def test_computed_urls_are_checked_at_dispatch(self):
        with LoopbackServer() as server, LoopbackServer() as other:
            source = (f'def main(inputs):\n  PGHttp{{pg_get({ZERO}, pg_input({ZERO}, inputs)), response => PGDone{{}}}}\n')
            denied = self.enroll([server.endpoint()], inputs=((other.origin + "/x").encode(),))
            with self.assertRaises(ExecutionRefused) as refused:
                denied.submit(source)
            self.assertIsInstance(refused.exception.__cause__, EffectDenied)
            self.assertEqual((server.requests, other.requests), ([], []))
            allowed = self.enroll([server.endpoint()], inputs=((server.origin + "/computed").encode(),))
            allowed.submit(source)
            self.assertEqual([row.path for row in server.requests], ["/computed"])

    def test_a_later_denied_call_stops_the_run_and_cancels_its_step(self):
        next_call = ('def next_call(response: PGResponse) -> PGIO:\n  PGWrite{PGNext{PGZero{}}, pg_text("unsent"), '
                     'PGHttp{pg_get(PGZero{}, pg_text("http://denied.example.com/")), response => PGDone{}}}\n\n')
        with LoopbackServer() as server:
            gateway = self.enroll([server.endpoint()])
            with self.assertRaises(ExecutionStopped) as stopped:
                gateway.submit(next_call + f'def main(inputs):\n  {get(server.origin + "/x", "next_call")}\n')
            self.assertIsInstance(stopped.exception.__cause__, EffectDenied)
            self.assertFalse(self.second.exists())
            self.assertEqual(len(server.requests), 1)

    def test_no_endpoint_means_no_call(self):
        gateway = self.enroll([])
        with self.assertRaises(ExecutionRefused) as refused:
            gateway.submit(REPORT + f'def main(inputs):\n  {get("http://127.0.0.1:9/")}\n')
        self.assertIsInstance(refused.exception.__cause__, EffectDenied)

    def test_method_and_program_credentials_are_denied(self):
        header = 'PGHeader{pg_text("Authorization"), pg_text("Bearer forged"), PGNoHeaders{}}'
        with LoopbackServer() as server:
            gateway = self.enroll([server.endpoint("GET")])
            for call in (f'pg_post({ZERO}, pg_text("{server.origin}/x"), pg_empty())',
                         f'PGRequest{{{ZERO}, PGGet{{}}, pg_text("{server.origin}/x"), {header}, pg_empty()}}'):
                with self.subTest(call=call), self.assertRaises(ExecutionRefused):
                    gateway.submit(f'def main(inputs):\n  PGHttp{{{call}, response => PGDone{{}}}}\n')
            self.assertEqual(server.requests, [])

    def test_owner_credentials_are_added_by_the_host_and_never_archived(self):
        secret = self.root / "credential"
        secret.write_bytes(b"Bearer owner-secret\n")
        with LoopbackServer() as server:
            gateway = self.enroll([server.endpoint(credential={"header": "Authorization", "file": str(secret)})])
            gateway.submit(REPORT + f'def main(inputs):\n  {get(server.origin + "/x")}\n')
            self.assertEqual(server.requests[0].headers["authorization"], "Bearer owner-secret")
        task = json.dumps(self.enrollment.get_task(self.owner))
        self.assertIn("PGHttp", self.enrollment.get_task(self.owner)["instructions"])
        self.assertNotIn("owner-secret", task)
        self.assertNotIn(str(secret), task)
        for path in (*gateway.audit.root.glob("*.json"), *self.owner.glob("*.json")):
            self.assertNotIn("owner-secret", path.read_text())

    def test_calls_are_bounded_and_each_request_is_sent_once(self):
        steps = "PGDone{}"
        for index in range(9):
            steps = f'PGHttp{{pg_get({ZERO}, url()), response => {steps}}}'
        with LoopbackServer() as server:
            gateway = self.enroll([server.endpoint()])
            source = f'def url() -> PGBytes:\n  pg_text("{server.origin}/x")\n\ndef main(inputs):\n  {steps}\n'
            with self.assertRaises(ExecutionStopped) as stopped:
                gateway.submit(source)
            self.assertEqual(len(stopped.exception.calls), 8)
            self.assertEqual(len(server.requests), 8)

    def test_writes_stay_bounded_across_calls(self):
        writes = "PGDone{}"
        for _ in range(9):
            writes = f'PGWrite{{{ZERO}, pg_empty(), {writes}}}'
        with LoopbackServer() as server:
            gateway = self.enroll([server.endpoint()])
            first = 'def after(response: PGResponse) -> PGIO:\n  ' + writes + '\n\n'
            source = first + f'def main(inputs):\n  {writes.replace("PGDone{}", get(server.origin + "/x", "after"))}\n'
            with self.assertRaises(ExecutionStopped) as stopped:
                gateway.submit(source)
            self.assertEqual(len(stopped.exception.completed), 9)

    def test_expiry_stops_a_pending_call(self):
        with LoopbackServer() as server:
            gateway = self.enroll([server.endpoint()])
            start = time.time()
            clock = iter((start, start, start))

            def expire():
                return next(clock, gateway.session.expires)

            with patch("proofguard.gateway.time.time", side_effect=expire):
                with self.assertRaises(ExecutionRefused):
                    gateway.submit(REPORT + f'def main(inputs):\n  {get(server.origin + "/x")}\n')
            self.assertEqual(server.requests, [])


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.owner = self.root / "owner"
        self.owner.mkdir()
        self.target = self.root / "output"

    def build_worker(self, server, *, credential=None):
        endpoint = server.endpoint(**({"credential": credential} if credential else {}))
        enrollment = Plugin.load(ROOT / "tests/fixtures/http").enroll(
            self.owner, inputs_base64=[], session={"identifier": "http-worker", "expires": int(time.time()) + 300,
                                                   "destinations": [str(self.target)], "endpoints": [endpoint]})
        return OutboxWorker(enrollment.build_gateway(self.owner), self.root / "outbox", self.root / "results")

    def test_the_agent_reads_a_status_and_never_a_response(self):
        def reply(handler):
            handler.reply(200, b"injected instructions")

        with LoopbackServer({"/page": reply}) as server:
            worker = self.build_worker(server)
            proposal = write_proposal(worker.outbox, REPORT + f'def main(inputs):\n  {get(server.origin + "/page")}\n')
            result = worker.process(proposal)
            published = (worker.results / f"{proposal}.json").read_text()
        self.assertEqual(result.status, Status.COMPLETED)
        self.assertNotIn("injected", published)
        self.assertEqual(self.target.read_bytes(), b"injected instructions")

    def test_stopped_and_uncertain_runs_tell_the_agent_not_to_assume_nothing_happened(self):
        def slow(handler):
            time.sleep(0.8)

        with LoopbackServer({"/slow": slow}) as server:
            worker = self.build_worker(server)
            worker.gateway.transport = HttpTransport(timeout=0.3)
            aborted = worker.process(write_proposal(
                worker.outbox, STOP + f'def main(inputs):\n  {get(server.origin + "/x", "stop")}\n'))
            self.assertEqual((aborted.status, aborted.reason), (Status.STOPPED, "stopped_after_effects"))
            timed_out = worker.process(write_proposal(
                worker.outbox, STOP + f'def main(inputs):\n  {get(server.origin + "/slow", "stop")}\n'))
            self.assertEqual((timed_out.status, timed_out.reason), (Status.UNCERTAIN, "owner_reconciliation_required"))

    def test_a_crash_after_sending_never_sends_again(self):
        def crash(audit_store, record):
            if record.get("kind") == "http_finished":
                raise KeyboardInterrupt("process died after the request left")
            return audit_store(record)

        with LoopbackServer() as server:
            worker = self.build_worker(server)
            proposal = write_proposal(worker.outbox, REPORT + f'def main(inputs):\n  {get(server.origin + "/x")}\n')
            store = worker.gateway.audit.store
            with patch.object(worker.gateway.audit, "store", side_effect=lambda record: crash(store, record)):
                with self.assertRaises(KeyboardInterrupt):
                    worker.process(proposal)
            self.assertEqual(len(server.requests), 1)
            self.assertFalse((worker.results / f"{proposal}.json").exists())
            self.assertIsNone(worker.process(proposal))
            self.assertEqual(worker.poll(), ())
            self.assertEqual(len(server.requests), 1)
            rows = [json.loads(path.read_text()) for path in worker.gateway.audit.root.glob("*.json")]
            self.assertEqual([row["kind"] for row in rows if row["kind"].startswith("http_")], ["http_started"])

    def test_credential_files_cannot_overlap_agent_mounts(self):
        outbox = self.root / "outbox"
        outbox.mkdir()
        with LoopbackServer() as server:
            with self.assertRaises(ContractError):
                self.build_worker(server, credential={"header": "Authorization", "file": str(outbox / "token")})


if __name__ == "__main__":
    unittest.main()
