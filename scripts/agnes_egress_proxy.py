"""A local CONNECT proxy with a dedicated upstream only for Agnes.

The process is intentionally small and has no business data access.  Hermes
uses it as its HTTPS proxy: only ``apihub.agnes-ai.com`` takes the configured
upstream proxy; every other HTTPS destination remains a direct server egress.
It never logs proxy credentials, request bodies, API keys, or response bodies.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import os
from pathlib import Path
import select
import socket
import socketserver
import ssl
import threading
from urllib.parse import unquote, urlsplit


AGNES_HOST = "apihub.agnes-ai.com"
MAX_HEADER_BYTES = 16 * 1024
CONNECT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class UpstreamProxy:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""


class ProxyConfigurationError(ValueError):
    """Safe configuration error whose text never includes a proxy URL."""


def parse_upstream_proxy(value: str) -> UpstreamProxy:
    raw = str(value or "").strip()
    if not raw:
        raise ProxyConfigurationError("upstream_proxy_missing")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProxyConfigurationError("upstream_proxy_scheme_or_host_invalid")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ProxyConfigurationError("upstream_proxy_must_be_origin_only")
    try:
        port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    except ValueError as exc:
        raise ProxyConfigurationError("upstream_proxy_port_invalid") from exc
    if not (1 <= port <= 65535):
        raise ProxyConfigurationError("upstream_proxy_port_invalid")
    return UpstreamProxy(
        scheme=parsed.scheme,
        host=str(parsed.hostname).lower(),
        port=port,
        username=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
    )


def read_secret_file(path: Path) -> str:
    stat = path.stat()
    # Production runs on Linux where mode 0600 is enforceable.  Windows uses
    # ACLs instead of POSIX mode bits, so rejecting its compatibility mode
    # would make local tests falsely fail without adding real protection.
    if os.name != "nt" and stat.st_mode & 0o077:
        raise ProxyConfigurationError("upstream_secret_permissions_not_600")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ProxyConfigurationError("upstream_secret_empty")
    return value


def parse_connect_target(request_line: bytes) -> tuple[str, int]:
    try:
        method, authority, version = request_line.decode("ascii").strip().split(" ", 2)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid_connect_request") from exc
    if method.upper() != "CONNECT" or not version.startswith("HTTP/"):
        raise ValueError("https_connect_required")
    host, separator, port_text = authority.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        raise ValueError("connect_authority_invalid")
    port = int(port_text)
    if not (1 <= port <= 65535):
        raise ValueError("connect_port_invalid")
    return host.rstrip(".").lower(), port


def build_upstream_connect_request(target_host: str, target_port: int, upstream: UpstreamProxy) -> bytes:
    headers = [
        f"CONNECT {target_host}:{target_port} HTTP/1.1",
        f"Host: {target_host}:{target_port}",
        "Proxy-Connection: Keep-Alive",
    ]
    if upstream.username or upstream.password:
        encoded = base64.b64encode(f"{upstream.username}:{upstream.password}".encode("utf-8")).decode("ascii")
        headers.append(f"Proxy-Authorization: Basic {encoded}")
    return ("\r\n".join(headers) + "\r\n\r\n").encode("ascii")


def _read_headers(connection: socket.socket) -> bytes:
    chunks = bytearray()
    while b"\r\n\r\n" not in chunks:
        chunk = connection.recv(1024)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > MAX_HEADER_BYTES:
            raise ValueError("headers_too_large")
    return bytes(chunks)


def _is_success_response(headers: bytes) -> bool:
    try:
        first = headers.split(b"\r\n", 1)[0].decode("ascii")
        return len(first.split()) >= 2 and first.split()[1] == "200"
    except UnicodeDecodeError:
        return False


def _connect_direct(host: str, port: int) -> socket.socket:
    return socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS)


def _connect_through_upstream(host: str, port: int, upstream: UpstreamProxy) -> socket.socket:
    connection = socket.create_connection((upstream.host, upstream.port), timeout=CONNECT_TIMEOUT_SECONDS)
    try:
        if upstream.scheme == "https":
            connection = ssl.create_default_context().wrap_socket(connection, server_hostname=upstream.host)
        connection.settimeout(CONNECT_TIMEOUT_SECONDS)
        connection.sendall(build_upstream_connect_request(host, port, upstream))
        headers = _read_headers(connection)
        if not _is_success_response(headers):
            raise ConnectionError("upstream_connect_rejected")
        connection.settimeout(None)
        return connection
    except Exception:
        connection.close()
        raise


def _relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while True:
        readable, _writable, _errors = select.select(sockets, [], [], 30)
        if not readable:
            continue
        for source in readable:
            payload = source.recv(64 * 1024)
            if not payload:
                return
            destination = right if source is left else left
            destination.sendall(payload)


class _ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: "AgnesEgressProxy" = self.server  # type: ignore[assignment]
        self.request.settimeout(CONNECT_TIMEOUT_SECONDS)
        try:
            headers = _read_headers(self.request)
            request_line = headers.split(b"\r\n", 1)[0]
            host, port = parse_connect_target(request_line)
            if port != 443:
                raise ValueError("only_https_connect_allowed")
            if host == AGNES_HOST:
                upstream = server.upstream
                if upstream is None:
                    raise ConnectionError("agnes_upstream_unconfigured")
                remote = _connect_through_upstream(host, port, upstream)
                route = "agnes_upstream"
            else:
                remote = _connect_direct(host, port)
                route = "direct"
            try:
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                server.record_route(route)
                _relay(self.request, remote)
            finally:
                remote.close()
        except ValueError:
            self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
            server.record_route("rejected")
        except Exception:
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            server.record_route("failed")


class AgnesEgressProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host: str, port: int, *, upstream: UpstreamProxy | None) -> None:
        self.upstream = upstream
        self._metrics_lock = threading.Lock()
        self.route_counts = {"agnes_upstream": 0, "direct": 0, "rejected": 0, "failed": 0}
        super().__init__((host, port), _ConnectHandler)

    def record_route(self, route: str) -> None:
        with self._metrics_lock:
            self.route_counts[route] = int(self.route_counts.get(route, 0)) + 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Restricted local HTTPS CONNECT egress for Agnes")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=18889)
    parser.add_argument("--upstream-secret-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        upstream = parse_upstream_proxy(read_secret_file(args.upstream_secret_file))
    except (OSError, ProxyConfigurationError) as exc:
        print(f"agnes_egress_proxy configuration_error={exc}")
        return 2
    server = AgnesEgressProxy(str(args.listen_host), int(args.listen_port), upstream=upstream)
    print(f"agnes_egress_proxy listening={args.listen_host}:{args.listen_port} route=agnes_upstream")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
