"""Owner-enrolled HTTP endpoints and the bounded client that reaches them.

The foundation enforces this allow-list on the concrete request at dispatch, so
a URL computed at runtime gets the same check as a constant. Labels and flow
policy belong to plugins. Every limit here is independent of plugin decisions.
"""

from dataclasses import dataclass, field
import http.client
import ipaddress
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import stat
import threading
import time

from bendspec.model import ContractError, fields, text
from .effects import EffectDenied, HttpFailure, HttpMethod, HttpRequest, HttpResponse, HttpResult

MAX_ENDPOINTS = 16
MAX_CALLS = 8
MAX_URL_BYTES = 4096
MAX_HEADER_BYTES = 8192
MAX_BODY_BYTES = 65536
MAX_CREDENTIAL_BYTES = 4096
CALL_TIMEOUT_SECONDS = 10.0
DEFAULT_PORTS = {"http": 80, "https": 443}
# Programs describe content. Anything that could carry identity stays owner-side.
PROGRAM_HEADERS = frozenset({"accept", "accept-language", "content-type", "if-match", "if-none-match"})

HOST = r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*|\[[0-9a-f:.]+\])"
ORIGIN = re.compile(rf"(http|https)://({HOST})(?::([0-9]{{1,5}}))?")
# RFC 3986 path and query characters only: no fragment, backslash or whitespace.
URL = re.compile(rf"(http|https)://({HOST})(?::([0-9]{{1,5}}))?(/[A-Za-z0-9\-._~!$&'()*+,;=:@%/]*)?(\?[A-Za-z0-9\-._~!$&'()*+,;=:@%/?]*)?")
HEADER_NAME = re.compile(rb"[A-Za-z0-9-]{1,64}")
HEADER_VALUE = re.compile(rb"[\x20-\x7e]{0,1024}")
CREDENTIAL_VALUE = re.compile(rb"[\x20-\x7e]{1,%d}" % MAX_CREDENTIAL_BYTES)
ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
# Servers and proxies decode paths differently, so a path carries no encoded
# separator, control byte or second layer of encoding, and no "..;" segment.
UNSAFE_ESCAPE = re.compile(r"%2f|%5c|%25|%[01][0-9a-f]|%7f", re.IGNORECASE)
DOT_SEGMENT = re.compile(r"(?:^|/)(?:\.|%2e){1,2}(?:/|;|$)", re.IGNORECASE)


class AddressBlocked(OSError):
    """A permitted name resolved to an address the endpoint may not reach."""


def get_port(scheme: str, port: str | None) -> int:
    number = DEFAULT_PORTS[scheme] if port is None else int(port)
    if not 1 <= number <= 65535:
        raise ValueError("port out of range")
    return number


@dataclass(frozen=True)
class Credential:
    """Owner secret added to a request at dispatch; only its path is enrolled."""

    header: str
    file: Path

    @classmethod
    def parse(cls, value: object) -> "Credential":
        row = fields(value, {"header", "file"}, "endpoint credential")
        header, path = text(row["header"], "credential header"), Path(text(row["file"], "credential file"))
        if not HEADER_NAME.fullmatch(header.encode()) or header.lower() in PROGRAM_HEADERS:
            raise ContractError("credential requires a valid owner-only header")
        if not path.is_absolute():
            raise ContractError("credential file must be absolute")
        return cls(header, path)

    def data(self) -> dict:
        return {"header": self.header, "file": str(self.file)}

    def read_value(self) -> str:
        """Read the secret at dispatch; it never enters enrollment or audit records."""
        fd = os.open(self.file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise OSError("credential must be a regular file")
            value = source.read(MAX_CREDENTIAL_BYTES + 1).rstrip(b"\r\n")
        if not CREDENTIAL_VALUE.fullmatch(value):
            raise OSError("credential is not a bounded header value")
        return value.decode("ascii")


@dataclass(frozen=True)
class AuthorizedCall:
    """A request that passed its endpoint's checks, ready for the client."""

    host: str
    port: int
    secure: bool
    private_network: bool
    method: HttpMethod
    path: str
    # Holds the owner credential, so it stays out of reprs and audit records.
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    body: bytes = field(repr=False)


@dataclass(frozen=True)
class Endpoint:
    """One allow-list entry: an exact origin, a path prefix and permitted methods."""

    scheme: str
    host: str
    port: int
    path_prefix: str
    methods: frozenset[HttpMethod]
    private_network: bool = False
    credential: Credential | None = None

    @classmethod
    def parse(cls, value: object) -> "Endpoint":
        """Decode an owner entry. Only a default-deny exact match is expressible."""
        optional = {"private_network", "credential"} & set(value) if type(value) is dict else set()
        row = fields(value, {"origin", "path_prefix", "methods"} | optional, "HTTP endpoint")
        origin = ORIGIN.fullmatch(text(row["origin"], "endpoint origin"))
        prefix = text(row["path_prefix"], "endpoint path prefix")
        if origin is None or not prefix.startswith("/") or DOT_SEGMENT.search(prefix) or "?" in prefix:
            raise ContractError("endpoint requires an exact origin and a plain path prefix")
        if type(row["methods"]) is not list or not row["methods"]:
            raise ContractError("endpoint must list its permitted methods")
        try:
            port = get_port(origin[1], origin[3])
            methods = frozenset(HttpMethod(item) for item in row["methods"])
        except ValueError as error:
            raise ContractError("invalid endpoint port or method") from error
        private = row.get("private_network", False)
        if type(private) is not bool:
            raise ContractError("private_network must be boolean")
        credential = Credential.parse(row["credential"]) if "credential" in row else None
        return cls(origin[1], origin[2].strip("[]"), port, prefix, methods, private, credential)

    def data(self) -> dict:
        """Describe this entry for the frozen enrollment record."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        record = {"origin": f"{self.scheme}://{host}:{self.port}", "path_prefix": self.path_prefix,
                  "methods": sorted(method.value for method in self.methods)}
        if self.private_network:
            record["private_network"] = True
        if self.credential is not None:
            record["credential"] = self.credential.data()
        return record

    def authorize(self, request: HttpRequest) -> AuthorizedCall:
        """Check concrete program bytes against this entry; deny on any doubt."""
        if len(request.url) > MAX_URL_BYTES or len(request.body) > MAX_BODY_BYTES:
            raise EffectDenied("HTTP request exceeds its size limit")
        try:
            found = URL.fullmatch(request.url.decode("ascii"))
        except UnicodeDecodeError as error:
            raise EffectDenied("URL must be ASCII") from error
        if found is None:
            raise EffectDenied("URL is not in the supported form")
        scheme, host, port, path, query = found[1], found[2].strip("[]"), found[3], found[4] or "/", found[5] or ""
        try:
            number = get_port(scheme, port)
        except ValueError as error:
            raise EffectDenied("URL port is invalid") from error
        if (scheme, host, number) != (self.scheme, self.host, self.port):
            raise EffectDenied("URL does not match its enrolled origin")
        if (not path.startswith(self.path_prefix) or DOT_SEGMENT.search(path) or UNSAFE_ESCAPE.search(path)
                or "%" in ESCAPE.sub("", path + query)):
            raise EffectDenied("URL path is outside its enrolled prefix")
        if request.method not in self.methods:
            raise EffectDenied("method is not enrolled for this endpoint")
        headers = self._check_headers(request)
        if self.credential is not None:
            headers += ((self.credential.header, self.credential.read_value()),)
        return AuthorizedCall(host, number, scheme == "https", self.private_network, request.method,
                              path + query, headers, request.body)

    def _check_headers(self, request: HttpRequest) -> tuple[tuple[str, str], ...]:
        """Allow descriptive headers only; a program can never present identity."""
        headers = []
        for name, value in request.headers:
            if not HEADER_NAME.fullmatch(name) or not HEADER_VALUE.fullmatch(value):
                raise EffectDenied("malformed request header")
            lowered = name.decode("ascii").lower()
            if lowered not in PROGRAM_HEADERS:
                raise EffectDenied("programs cannot set this request header")
            if lowered in {header for header, _ in headers}:
                raise EffectDenied("duplicate request header")
            headers.append((lowered, value.decode("ascii")))
        if sum(len(name) + len(value) for name, value in request.headers) > MAX_HEADER_BYTES:
            raise EffectDenied("request headers exceed their size limit")
        return tuple(headers)


def parse_endpoints(value: object) -> tuple[Endpoint, ...]:
    """Decode the owner's fixed endpoint table; a position selects one entry."""
    if type(value) is not list or len(value) > MAX_ENDPOINTS:
        raise ContractError("enroll at most sixteen HTTP endpoints")
    endpoints = tuple(Endpoint.parse(item) for item in value)
    if len(set(endpoints)) != len(endpoints):
        raise ContractError("HTTP endpoints must be distinct")
    return endpoints


def is_permitted_address(value: str, private_network: bool) -> bool:
    """Refuse addresses a public hostname must not reach, including rebinding."""
    address = ipaddress.ip_address(value.split("%")[0])
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return private_network
    # Link-local covers cloud metadata services, so no enrollment reaches it.
    if address.is_multicast or address.is_unspecified or address.is_reserved or address.is_link_local:
        return False
    # Deprecated site-local (fec0::/10) is still routed privately on some
    # networks, and ipaddress counts it as global.
    private = address.is_private or (address.version == 6 and address.is_site_local)
    return (address.is_global and not private) or (private_network and private)


def resolve_addresses(host: str, port: int, timeout: float) -> list[str]:
    """Resolve within the call's deadline; libc gives getaddrinfo no timeout."""
    results: queue.SimpleQueue = queue.SimpleQueue()

    def lookup():
        try:
            results.put([row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)])
        except OSError as error:
            results.put(error)

    threading.Thread(target=lookup, daemon=True).start()
    try:
        found = results.get(timeout=timeout)
    except queue.Empty as error:
        raise TimeoutError("name resolution exceeded the call deadline") from error
    if isinstance(found, OSError):
        raise found
    return found


@dataclass(frozen=True)
class Delivery:
    """A call's result. sent is False only when no request byte left the host."""

    result: HttpResult
    sent: bool
    blocked: bool = False

    @property
    def ambiguous(self) -> bool:
        """The request went out and nothing shows whether the server acted on it."""
        return self.sent and self.result in (HttpFailure.TIMEOUT, HttpFailure.BROKEN)


class Deadline:
    """Shut a socket down at the deadline, whatever call is blocked on it.

    Socket timeouts bound one read, so a server dripping bytes could otherwise
    hold a call open indefinitely.
    """

    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds
        self.expired = threading.Event()
        self.sock: socket.socket | None = None
        self.timer = threading.Timer(seconds, self._expire)
        self.timer.daemon = True

    def _expire(self):
        self.expired.set()
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def watch(self, sock: socket.socket) -> None:
        """Bound this socket, covering a deadline that passed while it connected."""
        self.sock = sock
        if self.expired.is_set():
            self._expire()

    def get_remaining(self) -> float:
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("call deadline exceeded")
        return remaining


class HttpTransport:
    """One bounded exchange: no redirects, no proxies, pinned to a checked address."""

    def __init__(self, *, timeout: float = CALL_TIMEOUT_SECONDS):
        self.timeout = timeout
        self.context = ssl.create_default_context()

    def _connect(self, call: AuthorizedCall, deadline: Deadline) -> socket.socket:
        """Connect to a checked address so no later lookup can redirect the call."""
        addresses = resolve_addresses(call.host, call.port, deadline.get_remaining())
        if not addresses or not all(is_permitted_address(item, call.private_network) for item in addresses):
            raise AddressBlocked("endpoint resolves to a blocked address")
        failure: OSError = ConnectionRefusedError("no address accepted the connection")
        for address in addresses:
            try:
                sock = socket.create_connection((address, call.port), deadline.get_remaining())
            except TimeoutError:
                raise
            except OSError as error:
                failure = error
                continue
            deadline.watch(sock)
            if call.secure:
                sock = self.context.wrap_socket(sock, server_hostname=call.host, do_handshake_on_connect=False)
                deadline.watch(sock)
                sock.do_handshake()
            return sock
        raise failure

    def send(self, call: AuthorizedCall) -> Delivery:
        """Send once. Failures become values; the host never retries a call."""
        deadline = Deadline(self.timeout)
        connection = http.client.HTTPConnection(call.host, call.port, timeout=self.timeout)
        sent = False
        deadline.timer.start()
        try:
            try:
                connection.sock = self._connect(call, deadline)
                sent = True
                connection.request(call.method.value, call.path, body=call.body or None,
                                   headers={**dict(call.headers), "Connection": "close"})
                response = connection.getresponse()
                body = response.read(MAX_BODY_BYTES + 1)
            except AddressBlocked:
                return Delivery(HttpFailure.UNREACHABLE, False, blocked=True)
            except TimeoutError:
                return Delivery(HttpFailure.TIMEOUT, sent)
            except (OSError, http.client.HTTPException, ValueError):
                if deadline.expired.is_set():
                    return Delivery(HttpFailure.TIMEOUT, sent)
                return Delivery(HttpFailure.BROKEN if sent else HttpFailure.UNREACHABLE, sent)
            if deadline.expired.is_set():
                return Delivery(HttpFailure.TIMEOUT, True)
            if len(body) > MAX_BODY_BYTES:
                return Delivery(HttpFailure.OVERSIZED, True)
            # A read past a closed connection returns short without raising.
            if response.length or not 100 <= response.status <= 999:
                return Delivery(HttpFailure.BROKEN, True)
            return Delivery(HttpResponse(response.status, body), True)
        finally:
            deadline.timer.cancel()
            connection.close()
            # A failed handshake leaves a socket the connection never received.
            if deadline.sock is not None:
                deadline.sock.close()
