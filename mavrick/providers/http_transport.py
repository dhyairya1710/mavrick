"""Bounded HTTP/TLS transport with cancellable, nonblocking socket operations."""

import errno
import http.client
import io
import ipaddress
import json
import select
import socket
import ssl
import subprocess
import sys
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from .base import Cancelled, ProviderError

_MAX_RESPONSE = 1024 * 1024
_POLL_INTERVAL = 0.05
_RESOLVE = (
    "import json,socket,sys;"
    "print(json.dumps(socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),"
    "type=socket.SOCK_STREAM,proto=socket.IPPROTO_TCP)))"
)


class RequestBudget:
    def __init__(self, timeout: float, cancelled: Cancelled | None):
        self.deadline = time.monotonic() + timeout
        self.cancelled = cancelled

    def interval(self) -> float:
        if self.cancelled and self.cancelled():
            raise ProviderError("cancelled", "AI request cancelled.")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("timeout", "Provider request deadline exceeded.")
        return min(_POLL_INTERVAL, remaining)

    def wait(self, wire: socket.socket, *, writing: bool = False) -> None:
        while True:
            readers, writers, _ = select.select(
                [] if writing else [wire], [wire] if writing else [], [], self.interval()
            )
            if readers or writers:
                return


def _addresses(host: str, port: int, budget: RequestBudget) -> list[tuple]:
    budget.interval()
    if host.lower() == "localhost":
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port)),
                (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", port, 0, 0))]
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)

    # The platform resolver can block in libc. A short-lived helper lets Stop cancel
    # and reap that operation even while DNS is waiting on the network.
    with subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", _RESOLVE, host, str(port)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as process:
        try:
            while True:
                try:
                    output, _ = process.communicate(timeout=budget.interval())
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                raise OSError("Provider DNS resolution failed.")
            return json.loads(output)
        finally:
            if process.poll() is None:
                process.kill()


class _ResponseReader(io.RawIOBase):
    def __init__(self, owner: "_DeadlineSocket"):
        self.owner = owner

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        return self.owner.perform(lambda: self.owner.wire.recv_into(buffer))

    def close(self) -> None:
        if not self.closed:
            self.owner.release_reader()
        super().close()


class _DeadlineSocket:
    """Give http.client a socket and buffered reader sharing one request budget."""

    def __init__(self, wire: socket.socket, budget: RequestBudget):
        self.wire = wire
        self.budget = budget
        self.reader_open = False
        self.closing = False

    def perform(self, operation: Callable, *, writing: bool = False):
        while True:
            self.budget.interval()
            try:
                return operation()
            except ssl.SSLWantReadError:
                self.budget.wait(self.wire)
            except ssl.SSLWantWriteError:
                self.budget.wait(self.wire, writing=True)
            except BlockingIOError:
                self.budget.wait(self.wire, writing=writing)

    def sendall(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            sent = self.perform(lambda: self.wire.send(remaining), writing=True)
            if not sent:
                raise OSError("Provider connection closed while sending.")
            remaining = remaining[sent:]

    def makefile(self, mode: str) -> io.BufferedReader:
        self.reader_open = True
        return io.BufferedReader(_ResponseReader(self))

    def release_reader(self) -> None:
        self.reader_open = False
        if self.closing:
            self.wire.close()

    def close(self) -> None:
        self.closing = True
        if not self.reader_open:
            self.wire.close()


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, tls: bool, budget: RequestBudget):
        super().__init__(host, port)
        self.tls = tls
        self.budget = budget

    def connect(self) -> None:
        for family, kind, proto, _, address in _addresses(self.host, self.port, self.budget):
            self.budget.interval()
            wire = socket.socket(family, kind, proto)
            try:
                wire.setblocking(False)
                status = wire.connect_ex(tuple(address))
                if status in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR):
                    self.budget.wait(wire, writing=True)
                    status = wire.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if status:
                    raise OSError(status, "Could not connect to provider.")
            except OSError:
                wire.close()
                continue
            except BaseException:
                wire.close()
                raise
            try:
                if self.tls:
                    wire = ssl.create_default_context().wrap_socket(
                        wire, server_hostname=self.host, do_handshake_on_connect=False
                    )
                transport = _DeadlineSocket(wire, self.budget)
                if self.tls:
                    transport.perform(wire.do_handshake)
                self.sock = transport
            except BaseException:
                wire.close()
                raise
            return
        raise OSError("Could not connect to provider.")


class JSONEndpoint:
    def __init__(self, base_url: str, api_key: str):
        try:
            url = urlsplit(base_url)
            host = url.hostname
            if (url.scheme not in ("http", "https") or not host or url.username is not None or
                url.password is not None or url.query or url.fragment or
                any(ord(char) < 33 for char in base_url)):
                raise ValueError
            self.host = host
            self.tls = url.scheme == "https"
            self.port = url.port or (443 if self.tls else 80)
            self.path = url.path.rstrip("/")
        except (ValueError, UnicodeError):
            raise ProviderError("configuration", "Use an HTTP(S) base URL without credentials, query, or fragment.") from None
        if api_key and not self.tls and not self._loopback():
            raise ProviderError("configuration", "API keys require HTTPS for non-loopback endpoints.")
        self.headers = {"Accept": "application/json"}
        if api_key:
            self.headers["Authorization"] = "Bearer " + api_key

    def _loopback(self) -> bool:
        if self.host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

    def request(self, path: str, timeout: float, *, payload: dict | None = None,
                cancelled: Cancelled | None = None, budget: RequestBudget | None = None,
                detect_unsupported_format: bool = False) -> dict:
        budget = budget or RequestBudget(timeout, cancelled)
        connection = _HTTPConnection(self.host, self.port, self.tls, budget)
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {**self.headers, "Content-Type": "application/json"} if body is not None else self.headers
        try:
            connection.request("POST" if body is not None else "GET", path, body, headers)
            with connection.getresponse() as response:
                if not 200 <= response.status < 300:
                    if detect_unsupported_format and response.status in (400, 422):
                        detail = response.read(4096).lower()
                        if b"response_format" in detail and any(
                            marker in detail for marker in (b"not support", b"unsupported", b"unrecognized", b"unknown")
                        ):
                            raise ProviderError("unsupported_format", "Provider does not support JSON response mode.")
                    raise ProviderError("http", f"Provider returned HTTP {response.status}.")
                raw = response.read(_MAX_RESPONSE + 1)
                if response.length and len(raw) <= _MAX_RESPONSE:
                    raise ProviderError("invalid_response", "Provider response was truncated.")
            budget.interval()
            if len(raw) > _MAX_RESPONSE:
                raise ProviderError("invalid_response", "Provider response exceeds the size limit.")
            content = json.loads(raw)
            if not isinstance(content, dict):
                raise ProviderError("invalid_response", "Provider response must be a JSON object.")
            return content
        except (ValueError, RecursionError):
            raise ProviderError("invalid_response", "Provider returned malformed JSON.") from None
        except ssl.SSLCertVerificationError:
            raise ProviderError("transport", "Provider TLS certificate verification failed.") from None
        except (OSError, http.client.HTTPException, EOFError):
            raise ProviderError("transport", "Could not complete the provider connection.") from None
        finally:
            connection.close()
