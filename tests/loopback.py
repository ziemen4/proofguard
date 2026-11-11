"""A recording HTTP server on 127.0.0.1 for tests; nothing here leaves the host."""

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading


@dataclass(frozen=True)
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_request(self):
        """Record first, so a test sees a request even if its route misbehaves."""
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.requests.append(Recorded(self.command, self.path,
                                             {key.lower(): value for key, value in self.headers.items()}, body))
        route = self.server.routes.get(self.path.split("?")[0])
        if route is not None:
            route(self)
        else:
            self.reply(200, b"public answer")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request

    def reply(self, status: int, body: bytes, headers: tuple[tuple[str, str], ...] = ()):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *arguments):
        pass


class LoopbackServer:
    """Answer every path with a fixed body unless a test installs a route."""

    def __init__(self, routes: dict | None = None):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.requests = []
        self.server.routes = routes or {}
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def requests(self) -> list[Recorded]:
        return self.server.requests

    def __enter__(self) -> "LoopbackServer":
        self.thread.start()
        return self

    def __exit__(self, *arguments):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def endpoint(self, *methods: str, path_prefix: str = "/", **settings) -> dict:
        """Enroll this server. Loopback needs the owner's explicit network grant."""
        return {"origin": self.origin, "path_prefix": path_prefix, "methods": list(methods or ("GET", "POST")),
                "private_network": True, **settings}
