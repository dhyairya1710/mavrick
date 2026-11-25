"""Bounded, cancellable NDJSON over local sockets and worker pipes."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import hmac
import json
import os
import re
import select
import socket
import tempfile
import time
from pathlib import Path


MAX_FRAME_BYTES = 2 * 1024 * 1024


class FrameError(ValueError):
    pass


class RequestCancelled(Exception):
    pass


def check_wait(deadline, cancelled=None):
    if cancelled and cancelled():
        raise RequestCancelled("AI request cancelled.")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("AI I/O deadline exceeded.")
    return min(remaining, 0.1)


def _wait_ready(endpoint, deadline, cancelled, *, writing=False):
    while True:
        interval = check_wait(deadline, cancelled)
        readable, writable, _ = select.select(
            [] if writing else [endpoint], [endpoint] if writing else [], [], interval)
        if readable or writable:
            return


class FrameReader:
    def __init__(self, endpoint, max_bytes=MAX_FRAME_BYTES):
        self.endpoint = endpoint
        self.max_bytes = max_bytes
        self.buffer = bytearray()

    def read(self, deadline, cancelled=None):
        endpoint = self.endpoint
        scanned = 0
        while True:
            check_wait(deadline, cancelled)
            end = self.buffer.find(b"\n", scanned)
            size = len(self.buffer) if end < 0 else end
            if size > self.max_bytes:
                raise FrameError("JSON frame exceeds the size limit.")
            if end >= 0:
                frame = bytes(self.buffer[:end])
                del self.buffer[:end + 1]
                if not frame.strip():
                    scanned = 0
                    continue
                try:
                    message = json.loads(frame.decode("utf-8"))
                except (UnicodeDecodeError, ValueError, RecursionError) as error:
                    raise FrameError("Invalid UTF-8 JSON frame.") from error
                if not isinstance(message, dict):
                    raise FrameError("Expected a JSON object.")
                return message
            scanned = size
            _wait_ready(endpoint, deadline, cancelled)
            try:
                # Allow one extra byte so an exact-limit frame can terminate with a newline.
                amount = min(4096, self.max_bytes + 1 - size)
                chunk = os.read(endpoint, amount) if isinstance(endpoint, int) else endpoint.recv(amount)
            except BlockingIOError:
                continue
            if not chunk:
                if self.buffer:
                    raise FrameError("Connection closed during a JSON frame.")
                raise EOFError("Connection closed.")
            self.buffer.extend(chunk)


def write_frame(endpoint, message, deadline, cancelled=None):
    try:
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError as error:
        raise FrameError("JSON frame contains invalid Unicode.") from error
    if len(data) > MAX_FRAME_BYTES:
        raise FrameError("JSON frame exceeds the size limit.")
    remaining = memoryview(data + b"\n")
    while remaining:
        _wait_ready(endpoint, deadline, cancelled, writing=True)
        try:
            size = os.write(endpoint, remaining) if isinstance(endpoint, int) else endpoint.send(remaining)
        except BlockingIOError:
            continue
        if not size:
            raise EOFError("Connection closed during write.")
        remaining = remaining[size:]


@dataclass(frozen=True)
class LocalEndpoint:
    transport: str
    address: str | tuple[str, int]
    token: str = field(default="", repr=False)
    pid: int = 0

    def request(self, message: dict) -> dict:
        return {**message, "token": self.token} if self.token else message


def _private_descriptor_directory(path: Path, *, create=False):
    from .platform import get_platform

    adapter = get_platform()
    directory = path.parent
    if adapter.name == "windows" and not directory.resolve().is_relative_to(adapter.runtime_dir().resolve()):
        raise FrameError("TCP descriptor must stay in the Windows user runtime directory.")
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = directory.lstat()
    except OSError as error:
        raise FrameError("TCP descriptor directory must be private.") from error
    if not adapter.is_private(info, directory=True):
        raise FrameError("TCP descriptor directory must be private.")
    if create and adapter.name == "windows":
        adapter.restrict_permissions(directory, directory=True)
    if not adapter.verify_private_path(directory, directory=True):
        raise FrameError("TCP descriptor directory must be private.")


def read_tcp_descriptor(path: str) -> LocalEndpoint:
    """Read a bounded, private descriptor and accept only numeric loopback."""
    from .platform import get_platform

    location = Path(path)
    _private_descriptor_directory(location)
    adapter = get_platform()
    if not adapter.verify_private_path(location):
        raise FrameError("TCP descriptor file must be private.")
    fd = os.open(location, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as handle:
        if not adapter.verify_private_descriptor(handle.fileno()):
            raise FrameError("TCP descriptor file must be private.")
        data = handle.read(4097)
    try:
        if len(data) > 4096:
            raise ValueError
        record = json.loads(data)
        host, port, token, pid = (record[key] for key in ("host", "port", "token", "pid"))
        if (host != "127.0.0.1" or type(port) is not int or not 0 < port < 65536 or
            not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token) or
            type(pid) is not int or pid <= 0):
            raise ValueError
    except (KeyError, TypeError, ValueError, RecursionError):
        raise FrameError("Invalid TCP runtime descriptor.") from None
    return LocalEndpoint("tcp-token", (host, port), token, pid)


def prepare_descriptor_directory(path: str) -> None:
    _private_descriptor_directory(Path(path), create=True)


def publish_tcp_descriptor(path: str, port: int, token: str) -> None:
    from .platform import get_platform

    location = Path(path)
    _private_descriptor_directory(location)
    fd, temp = tempfile.mkstemp(prefix=".mavrickd-", dir=location.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"host": "127.0.0.1", "port": port, "token": token, "pid": os.getpid()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        get_platform().restrict_permissions(temp)
        os.replace(temp, location)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def local_endpoint(paths: tuple[str, str]) -> LocalEndpoint | None:
    from .config import load_ipc_settings

    config = load_ipc_settings()
    if config.transport == "tcp-token":
        try:
            return read_tcp_descriptor(config.descriptor)
        except FileNotFoundError:
            return None
    path = next((path for path in paths if os.path.exists(path)), None)
    return LocalEndpoint("unix-socket", path) if path else None


@contextmanager
def connect_local(endpoint: LocalEndpoint, deadline, cancelled=None):
    family = socket.AF_INET if endpoint.transport == "tcp-token" else socket.AF_UNIX
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        connection.settimeout(min(1.5, max(0.01, deadline - time.monotonic())))
        check_wait(deadline, cancelled)
        connection.connect(endpoint.address)
        connection.setblocking(False)
        yield connection


def authorized_request(message: dict, secret: str) -> bool:
    supplied = message.get("token")
    supplied = supplied.encode("ascii", errors="replace") if isinstance(supplied, str) else b""
    return hmac.compare_digest(supplied, secret.encode("ascii"))


def peer_disconnected(connection, *, tcp=False):
    """Detect a departed client while preserving Unix write-half-close behavior."""
    if not tcp and hasattr(select, "poll"):
        poll = select.poll()
        poll.register(connection, select.POLLHUP | select.POLLERR)
        return lambda: bool(poll.poll(0))

    def disconnected():
        try:
            ready, _, errors = select.select([connection], [], [connection], 0)
            return bool(errors) or bool(ready and not connection.recv(1, socket.MSG_PEEK))
        except BlockingIOError:
            return False
        except OSError:
            return True
    return disconnected
